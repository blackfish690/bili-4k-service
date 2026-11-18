"""DASH MPD 动态生成。

B 站 m4s 是 SegmentBase + indexRange 的形态：文件开头是 ftyp/moov（Initialization），
紧随其后是 sidx（indexRange）。这里在服务端取前 128KB 解析 box 边界，动态算 range，
这样对任意视频/清晰度都适用，不依赖写死的字节偏移。
"""

import logging
import time
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple
from xml.sax.saxutils import escape

log = logging.getLogger(__name__)

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)
# 部分 CDN（upos-sz-302ppio 等）对桌面浏览器 UA 返回 403，只放行 App UA；
# 反过来个别节点也认浏览器 UA，因此两种都试。
APP_UA = (
    "bili-universal/8090300 os/android model/22081212C "
    "mobi_app/android build/8090300 osVer/14"
)

WEB_REFERER = "https://www.bilibili.com/"

HEAD_BYTES = 131072
BoxRange = Tuple[int, int, int]  # (init_end, index_start, index_end)


def is_app_stream(url: str) -> bool:
    """是否 App 端签名流（PiliPlus 里 referer 置空的那一类）。

    与 lib/plugin/pl_player/controller.dart 的判定一致：URL 带
    platform=android / mobi_app=android / build=8090300 就是 App 流。
    upos-sz-* 会校验来源，给 App 流加 B 站 Referer 反而 403。
    """
    if not url:
        return False
    query = urllib.parse.urlsplit(url).query.lower()
    return (
        "platform=android" in query
        or "mobi_app=android" in query
        or "build=8090300" in query
    )


def stream_headers(
    url: str, range_value: Optional[str] = None, ua: Optional[str] = None
) -> Dict[str, str]:
    """按流的类型生成请求头：App 流不带 Referer，网页流才带。"""
    headers = {"User-Agent": ua or APP_UA}
    if not is_app_stream(url):
        headers["Referer"] = WEB_REFERER
    if range_value:
        headers["Range"] = range_value
    return headers


def _host(url: str) -> str:
    return (urllib.parse.urlsplit(url).netloc or "").lower()


def candidate_urls(stream: Dict[str, Any]) -> List[str]:
    """按 PiliPlus getCdnUrl 的口径给候选节点排序。

    顺序：[upos 首选, 其他 upos, mcdn, 其余原 base]，去重且保持原相对顺序。
    mcdn 是 P2P 节点，不校验来源（4K 常走它），但稳定性不如 upos，故不首选。
    """
    raw = [stream.get("url") or ""] + list(stream.get("backup_urls") or [])
    ordered: List[str] = []
    for url in raw:
        if url and url not in ordered:
            ordered.append(url)

    upos = [u for u in ordered if "upos-" in _host(u)]
    mcdn = [u for u in ordered if "mcdn" in _host(u)]
    rest = [u for u in ordered if u not in upos and u not in mcdn]
    return upos + mcdn + rest


def _iter_boxes(data: bytes):
    offset = 0
    total = len(data)
    while offset + 8 <= total:
        size = int.from_bytes(data[offset:offset + 4], "big")
        box_type = data[offset + 4:offset + 8].decode("latin-1", "replace")
        header = 8
        if size == 1:
            if offset + 16 > total:
                return
            size = int.from_bytes(data[offset + 8:offset + 16], "big")
            header = 16
        elif size == 0:
            return
        if size < header:
            return
        yield offset, box_type, size
        offset += size


def _read_head(url: str, timeout: int, ua: str = APP_UA) -> bytes:
    """读取文件头；中途超时也保留已收到的数据。

    请求头由 stream_headers 生成：App 流不带 Referer（带 Referer 会被
    upos-sz-* 判成网页盗链而 403），网页流仍带 B 站 Referer。
    """
    req = urllib.request.Request(
        url,
        headers=stream_headers(url, "bytes=0-%d" % (HEAD_BYTES - 1), ua),
    )
    chunks: List[bytes] = []
    received = 0
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        while received < HEAD_BYTES:
            try:
                chunk = resp.read(16384)
            except Exception as exc:  # 已收到的部分往往已包含 moov/sidx
                log.info("读取头部中断（已收 %d 字节）: %s", received, exc)
                break
            if not chunk:
                break
            chunks.append(chunk)
            received += len(chunk)
    return b"".join(chunks)


def _parse_ranges(data: bytes) -> Optional[BoxRange]:
    init_end = None
    for offset, box_type, size in _iter_boxes(data):
        if box_type == "moov":
            init_end = offset + size - 1
        elif box_type == "sidx":
            if init_end is None:
                return None
            return init_end, offset, offset + size - 1
    return None


def _parse_sidx(data: bytes):
    """解析 sidx，返回 (init_end, timescale, [(start, end, duration), ...])。"""
    init_end = None
    for offset, box_type, size in _iter_boxes(data):
        if box_type == "moov":
            init_end = offset + size - 1
            continue
        if box_type != "sidx" or init_end is None:
            continue
        sidx_end = offset + size - 1
        p = offset + 8
        version = data[p]
        p += 4  # version + flags
        p += 4  # reference_ID
        timescale = int.from_bytes(data[p:p + 4], "big")
        p += 4
        if version == 0:
            p += 4  # earliest_presentation_time
            first_offset = int.from_bytes(data[p:p + 4], "big")
            p += 4
        else:
            p += 8
            first_offset = int.from_bytes(data[p:p + 8], "big")
            p += 8
        p += 2  # reserved
        count = int.from_bytes(data[p:p + 2], "big")
        p += 2
        cur = sidx_end + 1 + first_offset
        segments = []
        for _ in range(count):
            if p + 12 > len(data):
                break
            ref = int.from_bytes(data[p:p + 4], "big")
            dur = int.from_bytes(data[p + 4:p + 8], "big")
            p += 12
            seg_size = ref & 0x7FFFFFFF
            if seg_size <= 0:
                break
            segments.append((cur, cur + seg_size - 1, dur))
            cur += seg_size
        if not segments:
            return None
        return init_end, timescale, segments
    return None


def _head_bytes(url: str, timeout: int = 20) -> Optional[bytes]:
    """两种 UA 各试两轮读取文件头，失败返回 None。"""
    for attempt in (1, 2):
        for ua in (APP_UA, UA):
            if attempt > 1 or ua is UA:
                time.sleep(0.5)
            try:
                data = _read_head(url, timeout, ua)
            except Exception as exc:
                log.warning("读取 m4s 头部失败（第 %d 次/%s）: %s",
                            attempt, "app" if ua is APP_UA else "web", exc)
                continue
            if data:
                return data
    return None


def fetch_mp4_ranges(url: str, timeout: int = 20) -> Optional[BoxRange]:
    """读取文件头，返回 (moov 结束偏移, sidx 起, sidx 止)。失败返回 None。"""
    data = _head_bytes(url, timeout)
    return _parse_ranges(data) if data else None


def fetch_segment_table(stream: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """读取某个流的 sidx，产出可供分片代理使用的分片表。

    候选节点顺序与 build_mpd / 直连模式一致（upos 优先，mcdn 其次）。
    """
    urls = candidate_urls(stream)
    tried = []
    for url in urls:
        if not url or url in tried:
            continue
        tried.append(url)
        data = _head_bytes(url)
        parsed = _parse_sidx(data) if data else None
        if not parsed:
            continue
        init_end, timescale, segments = parsed
        return {
            "url": url,
            "backup_urls": [u for u in urls if u != url][0:2],
            "init_end": init_end,
            "timescale": timescale,
            "segments": segments,
        }
    return None


def _ranges_for(stream: Dict[str, Any]) -> Optional[Tuple[str, BoxRange]]:
    """按候选顺序读取分片索引，返回 (命中节点 URL, ranges)。

    必须把命中的节点一起返回：MPD 的 BaseURL 必须指向真正读了 sidx 的
    那个节点，否则偏移量可能来自 A 节点而播放器去请求 B 节点。
    """
    for url in candidate_urls(stream):
        if not url:
            continue
        ranges = fetch_mp4_ranges(url)
        if ranges:
            return url, ranges
    return None


def _codec_string(codecid: int, height: int, frame_rate: str) -> str:
    try:
        fps = float(frame_rate or 0)
    except ValueError:
        fps = 0.0
    if codecid == 12:  # HEVC
        return "hvc1.1.6.L153.B0"
    if codecid == 13:  # AV1
        return "av01.0.08M.08"
    # AVC
    if height >= 2160:
        return "avc1.640033"
    if height >= 1080 and fps > 30:
        return "avc1.64002a"
    if height >= 1080:
        return "avc1.640028"
    if height >= 720:
        return "avc1.64001f"
    return "avc1.4d401e"


def _duration(ms: int) -> str:
    return "PT%.3fS" % (max(0, int(ms)) / 1000.0)


def _segment_base(ranges: Optional[BoxRange]) -> str:
    if not ranges:
        # 解析失败时退化为单段顺序播放（不支持拖动）
        return ""
    init_end, index_start, index_end = ranges
    return (
        '<SegmentBase indexRange="%d-%d">'
        '<Initialization range="0-%d"/>'
        "</SegmentBase>" % (index_start, index_end, init_end)
    )


def build_mpd(video: Dict[str, Any], audio: Optional[Dict[str, Any]],
              duration_ms: int, qn: int = 0) -> str:
    """根据选中的视频/音频流生成可直接交给 PotPlayer / mpv 的 MPD。"""
    height = int(video.get("height") or 0)
    frame_rate = str(video.get("frame_rate") or "")
    codec = _codec_string(int(video.get("codecid") or 7), height, frame_rate)

    duration = _duration(duration_ms)
    video_hit = _ranges_for(video)
    if not video_hit:
        # 没有 SegmentBase 的 MPD 播放器只会一直转圈（或直接报错），
        # 不如明确失败，让上层返回「该档位不可用」。
        raise RuntimeError(
            "无法读取视频分片索引（CDN 返回 403 或超时）：该档位无法生成可用清单，"
            "请换 4K 或稍后重试。"
        )
    video_url, video_range = video_hit

    lines: List[str] = [
        '<?xml version="1.0" encoding="utf-8"?>',
        '<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static"',
        '     profiles="urn:mpeg:dash:profile:isoff-on-demand:2011"',
        '     mediaPresentationDuration="%s" minBufferTime="PT2S">' % duration,
        '  <Period duration="%s">' % duration,
        '    <AdaptationSet mimeType="video/mp4" segmentAlignment="true" startWithSAP="1">',
        '      <Representation id="v-%d-%d" bandwidth="%d" codecs="%s" width="%d" height="%d"%s>'
        % (
            int(qn or 0),
            int(video.get("codecid") or 7),
            int(video.get("bandwidth") or 0),
            codec,
            int(video.get("width") or 0),
            height,
            ' frameRate="%s"' % escape(frame_rate) if frame_rate else "",
        ),
        "        <BaseURL>%s</BaseURL>" % escape(video_url),
        "        %s" % _segment_base(video_range),
        "      </Representation>",
        "    </AdaptationSet>",
    ]

    if audio:
        audio_hit = _ranges_for(audio)
        audio_url = audio_hit[0] if audio_hit else audio["url"]
        audio_range = audio_hit[1] if audio_hit else None
        lines.extend(
            [
                '    <AdaptationSet mimeType="audio/mp4" segmentAlignment="true" startWithSAP="1">',
                '      <Representation id="a-%d" bandwidth="%d" codecs="mp4a.40.2">'
                % (int(audio.get("id") or 0), int(audio.get("bandwidth") or 0)),
                "        <BaseURL>%s</BaseURL>" % escape(audio_url),
                "        %s" % _segment_base(audio_range),
                "      </Representation>",
                "    </AdaptationSet>",
            ]
        )

    lines.extend(["  </Period>", "</MPD>", ""])
    # 去掉空行（_segment_base 为空时会产生缩进空行）
    return "\n".join(
        line for line in "\n".join(lines).splitlines() if line.strip()
    ) + "\n"


def build_pot_mpd(
    video: Dict[str, Any],
    audio: Optional[Dict[str, Any]],
    video_table: Dict[str, Any],
    audio_table: Optional[Dict[str, Any]],
    duration_ms: int,
    base_url: str,
    token: str,
) -> str:
    """生成 PotPlayer 兼容的分片式 MPD：
    BaseURL + AdaptationSet 级 SegmentTemplate + 相对分片地址，
    子资源由后端的 /api/play/seg/{token}/... 代理。
    """
    height = int(video.get("height") or 0)
    frame_rate = str(video.get("frame_rate") or "")
    codec = _codec_string(int(video.get("codecid") or 7), height, frame_rate)
    duration = _duration(duration_ms)
    seg_base = "%s/api/play/seg/%s/" % (base_url.rstrip("/"), token)
    v_seg = video_table["segments"][0]

    def attrs(**kw) -> str:
        return " ".join(
            '%s="%s"' % (k.rstrip("_"), escape(str(v))) for k, v in kw.items() if v is not None
        )

    lines: List[str] = [
        '<?xml version="1.0" encoding="utf-8"?>',
        '<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static"',
        '     profiles="urn:mpeg:dash:profile:isoff-live:2011"',
        '     mediaPresentationDuration="%s" minBufferTime="PT2S">' % duration,
        '  <Period id="0">',
        "    <BaseURL>%s</BaseURL>" % escape(seg_base),
        '    <AdaptationSet id="1" mimeType="video/mp4" contentType="video"'
        ' segmentAlignment="true" startWithSAP="1">',
        "      <SegmentTemplate timescale=\"%d\" duration=\"%d\" startNumber=\"1\""
        ' initialization="v-init.mp4" media="v-$Number$.m4s"/>'
        % (int(video_table["timescale"]), int(v_seg[2])),
        "      <Representation %s/>"
        % attrs(
            id="v-%d-%d" % (int(video.get("id") or 0), int(video.get("codecid") or 7)),
            bandwidth=int(video.get("bandwidth") or 0),
            codecs=codec,
            width=int(video.get("width") or 0),
            height=height,
            frameRate=frame_rate or None,
        ),
        "    </AdaptationSet>",
    ]

    if audio and audio_table:
        a_seg = audio_table["segments"][0]
        lines.extend(
            [
                '    <AdaptationSet id="2" mimeType="audio/mp4" contentType="audio"'
                ' segmentAlignment="true" startWithSAP="1">',
                '      <Role schemeIdUri="urn:mpeg:dash:role:2011" value="main"/>',
                "      <SegmentTemplate timescale=\"%d\" duration=\"%d\" startNumber=\"1\""
                ' initialization="a-init.mp4" media="a-$Number$.m4s"/>'
                % (int(audio_table["timescale"]), int(a_seg[2])),
                "      <Representation %s/>"
                % attrs(
                    id="a-%d" % int(audio.get("id") or 0),
                    bandwidth=int(audio.get("bandwidth") or 0),
                    codecs="mp4a.40.2",
                    audioSamplingRate=int(audio.get("sampling_rate") or 48000),
                ),
                "    </AdaptationSet>",
            ]
        )

    lines.extend(["  </Period>", "</MPD>", ""])
    return "\n".join(lines)
