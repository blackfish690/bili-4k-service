"""B 站协议层：复用已验证的 gRPC PlayViewUnite + TV 端扫码登录实现。

只做协议调用与结构化解析，不涉及 HTTP 框架。
"""

import hashlib
import json
import random
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import config

# 复用已有生成好的 protobuf / grpc stub
GEN_DIR = Path(__file__).resolve().parent / "gen"
if str(GEN_DIR) not in sys.path:
    sys.path.insert(0, str(GEN_DIR))

import grpc
from bilibili import metadata_pb2, playershared_pb2 as shared_pb2
from bilibili.metadata import device_pb2, fawkes_pb2, locale_pb2, network_pb2
from bilibili.app.playerunite import v1_pb2, v1_pb2_grpc

GRPC_TARGET = "grpc.biliapi.net:443"
PHONE_BUILD = 8090300
PHONE_VERSION = "8.9.0"
PHONE_MODEL = "22081212C"

UA_PHONE = (
    "bili-universal/8090300 os/android model/22081212C "
    "mobi_app/android build/8090300 osVer/14"
)
UA_WEB = (
    "Mozilla/5.0 BiliDroid/8.9.0 (bbcallen@gmail.com) os/android "
    "model/22081212C mobi_app/android build/8090300 channel/master "
    "innerVer/8090300 osVer/14 network/2"
)

PASSPORT = "https://passport.bilibili.com"
API_AUTH_CODE = PASSPORT + "/x/passport-tv-login/qrcode/auth_code"
API_POLL = PASSPORT + "/x/passport-tv-login/qrcode/poll"

# 与 PiliPlus lib/common/constants.dart 一致
APP_KEY = "dfca71928277209b"
APP_SEC = "b5475a8825547a4fc26c7d518eaaa02e"

QN_LABELS = {
    127: "8K",
    126: "杜比视界",
    125: "HDR",
    120: "4K",
    116: "1080P60",
    112: "1080P+",
    80: "1080P",
    74: "720P60",
    64: "720P",
    32: "480P",
    16: "360P",
}


class BiliError(Exception):
    """带错误码的业务异常，HTTP 层统一转换。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class TTLCache:
    """线程安全的 TTL 缓存，每条记录可带独立 TTL（满足慢节点短缓存）。"""

    def __init__(self, max_items: int = 512):
        self.max_items = max_items
        self._data: Dict[str, Any] = {}
        self._lock = threading.Lock()

    def get(self, key: str) -> Any:
        now = time.time()
        with self._lock:
            item = self._data.get(key)
            if not item:
                return None
            if now >= item[1]:
                self._data.pop(key, None)
                return None
            return item[0]

    def put(self, key: str, value: Any, ttl: float) -> None:
        if ttl <= 0:
            return
        with self._lock:
            self._data[key] = (value, time.time() + ttl)
            if len(self._data) > self.max_items:
                # 优先清过期项，仍然超限再淘汰最早到期的
                now = time.time()
                for old_key in [k for k, v in self._data.items() if v[1] <= now]:
                    self._data.pop(old_key, None)
                overflow = len(self._data) - self.max_items
                if overflow > 0:
                    oldest = sorted(self._data.items(), key=lambda kv: kv[1][1])
                    for old_key, _ in oldest[:overflow]:
                        self._data.pop(old_key, None)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()


# ---------------------------------------------------------------------------
# 通用 HTTP
# ---------------------------------------------------------------------------

def http_json(url: str, headers: Optional[Dict[str, str]] = None,
              timeout: int = 15) -> Dict[str, Any]:
    req = urllib.request.Request(
        url, headers={"User-Agent": UA_WEB, **(headers or {})}
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def _fetch_buvid_remote() -> str:
    try:
        data = http_json("https://api.bilibili.com/x/frontend/finger/spi")
        buvid = (data.get("data") or {}).get("b_3")
        if buvid:
            return buvid
    except Exception:
        pass
    return uuid.uuid4().hex.upper() + "infoc"


_buvid_cache = TTLCache(8)
_buvid_lock = threading.Lock()


def fetch_buvid(force: bool = False) -> str:
    """匿名获取 buvid3 并缓存；并发未命中时只发一次请求。"""
    if not force:
        cached = _buvid_cache.get("buvid3")
        if cached:
            return cached
    with _buvid_lock:
        if not force:
            cached = _buvid_cache.get("buvid3")
            if cached:
                return cached
        buvid = _fetch_buvid_remote()
        _buvid_cache.put("buvid3", buvid, config.BUVID_TTL)
        return buvid


def invalidate_buvid() -> None:
    """gRPC 失败后强制刷新 buvid，下一次 fetch_buvid() 重新取。"""
    _buvid_cache.clear()


_video_info_cache = TTLCache(256)


def fetch_video_info(bvid: Optional[str] = None, aid: Optional[int] = None,
                     page: int = 1) -> Dict[str, Any]:
    """通过 web 接口解析 bvid/aid -> aid/cid/分P信息。匿名可用。"""
    params: Dict[str, str] = {}
    if bvid:
        params["bvid"] = bvid
    elif aid:
        params["aid"] = str(aid)
    else:
        raise BiliError("bad_request", "必须提供 bvid 或 aid")

    page = max(1, int(page or 1))
    cache_key = "%s|%s|%d" % (bvid or "", aid or "", page)
    cached = _video_info_cache.get(cache_key)
    if cached is not None:
        return cached

    data = http_json(
        "https://api.bilibili.com/x/web-interface/view?"
        + urllib.parse.urlencode(params)
    )
    if data.get("code") != 0:
        raise BiliError(
            "video_info_failed",
            "获取视频信息失败: %s %s" % (data.get("code"), data.get("message")),
        )

    view = data["data"]
    pages = view.get("pages") or []
    if pages and page > len(pages):
        raise BiliError(
            "page_out_of_range",
            "分P超出范围：共 %d P，请求第 %d P" % (len(pages), page),
        )
    cid = pages[page - 1]["cid"] if pages else view.get("cid")

    result = {
        "bvid": view.get("bvid") or bvid,
        "aid": view["aid"],
        "cid": cid,
        "title": view.get("title", ""),
        "pic": view.get("pic", ""),
        "owner": (view.get("owner") or {}).get("name", ""),
        "duration": view.get("duration", 0),
        "page": page,
        "page_count": len(pages) or 1,
        "page_title": pages[page - 1].get("part", "") if pages else "",
        "pages": [
            {
                "page": item.get("page"),
                "cid": item.get("cid"),
                "part": item.get("part", ""),
                "duration": item.get("duration", 0),
            }
            for item in pages
        ],
    }
    _video_info_cache.put(cache_key, result, config.VIDEO_INFO_TTL)
    return result


# ---------------------------------------------------------------------------
# gRPC PlayViewUnite
# ---------------------------------------------------------------------------

def build_metadata(buvid: str, access_key: Optional[str] = None):
    trace_id = "%s:%s:0:0" % (uuid.uuid4().hex, uuid.uuid4().hex[:16])

    device = device_pb2.Device(
        app_id=1,
        build=PHONE_BUILD,
        buvid=buvid,
        mobi_app="android",
        platform="android",
        channel="master",
        brand="Xiaomi",
        model=PHONE_MODEL,
        osver="14",
        version_name=PHONE_VERSION,
    )
    network = network_pb2.Network(type=network_pb2.WIFI)
    locale = locale_pb2.Locale(
        c_locale=locale_pb2.LocaleIds(language="zh", region="CN", script="Hans"),
        s_locale=locale_pb2.LocaleIds(language="zh", region="CN", script="Hans"),
        timezone="Asia/Shanghai",
    )
    fawkes = fawkes_pb2.FawkesReq(
        appkey="android", env="prod", session_id="%08x" % random.getrandbits(32)
    )
    meta = metadata_pb2.Metadata(
        mobi_app="android",
        device="android",
        build=PHONE_BUILD,
        channel="master",
        buvid=buvid,
        platform="android",
    )

    md = [
        ("gzip-accept-encoding", "gzip,identity"),
        ("user-agent", UA_PHONE),
        ("x-bili-gaia-vtoken", ""),
        ("x-bili-aurora-zone", ""),
        ("x-bili-trace-id", trace_id),
        ("buvid", buvid),
        ("bili-http-engine", "cronet"),
        ("x-bili-device-bin", device.SerializeToString()),
        ("x-bili-network-bin", network.SerializeToString()),
        ("x-bili-locale-bin", locale.SerializeToString()),
        ("x-bili-exps-bin", b""),
        ("x-bili-fawkes-req-bin", fawkes.SerializeToString()),
    ]
    if access_key:
        meta.access_key = access_key
        md.append(("authorization", "identify_v1 " + access_key))
    md.append(("x-bili-metadata-bin", meta.SerializeToString()))
    return md


def build_request(aid: int, cid: int, qn: int, fnval: int = 4048):
    req = v1_pb2.PlayViewUniteReq()
    req.vod.aid = aid
    req.vod.cid = cid
    req.vod.qn = qn
    req.vod.fnver = 0
    req.vod.fnval = fnval
    req.vod.download = 0
    req.vod.force_host = 2
    req.vod.fourk = True
    req.vod.prefer_codec_type = shared_pb2.NOCODE
    req.vod.voice_balance = 1
    req.vod.is_need_trial = True

    req.spmid = "main.ugc-video-detail.0.0"
    req.from_spmid = "main.ugc-video-detail.0.0"
    req.play_ctrl = shared_pb2.PLAY_CTRL_DEFAULT
    req.from_scene = "normal"
    req.ad_extra = "vod_common"
    return req


def call_playview(aid: int, cid: int, qn: int, access_key: str,
                  buvid: str) -> Any:
    req = build_request(aid, cid, qn)
    md = build_metadata(buvid, access_key or None)

    channel = grpc.secure_channel(GRPC_TARGET, grpc.ssl_channel_credentials())
    try:
        stub = v1_pb2_grpc.PlayerStub(channel)
        try:
            return stub.PlayViewUnite(
                req, metadata=md, timeout=30, compression=grpc.Compression.Gzip
            )
        except grpc.RpcError as exc:
            code = exc.code().name if exc.code() else ""
            detail = exc.details() or ""
            # access_key 失效时 B 站不回 HTTP 401，只给 gRPC 鉴权错误；
            # 这里翻译成 login_required，避免用户只看到一句 grpc_error。
            if code in ("UNAUTHENTICATED", "PERMISSION_DENIED") or "未登录" in detail:
                raise BiliError(
                    "login_required",
                    "B 站鉴权失败（%s %s）：access_key 可能已失效，请重新扫码登录。"
                    % (code, detail),
                )
            raise BiliError(
                "grpc_error",
                "PlayViewUnite 调用失败: %s %s" % (code, detail),
            )
    finally:
        channel.close()


# ---------------------------------------------------------------------------
# 响应解析
# ---------------------------------------------------------------------------

def _dash_video(video) -> Dict[str, Any]:
    return {
        "url": video.base_url,
        "backup_urls": list(video.backup_url),
        "codecid": video.codecid,
        "width": video.width,
        "height": video.height,
        "frame_rate": video.frame_rate,
        "bandwidth": video.bandwidth,
        "size": video.size,
        "md5": video.md5,
        "audio_id": video.audio_id,
    }


def _dash_audio(audio) -> Dict[str, Any]:
    return {
        "id": audio.id,
        "url": audio.base_url,
        "backup_urls": list(audio.backup_url),
        "bandwidth": audio.bandwidth,
        "size": audio.size,
        "md5": audio.md5,
    }


def parse_playview(reply) -> Dict[str, Any]:
    """把 gRPC 响应转成扩展/MPD 需要的结构。"""
    vod = reply.vod_info

    qualities: List[Dict[str, Any]] = []
    for stream in vod.stream_list:
        info = stream.stream_info
        videos = []
        if stream.HasField("dash_video"):
            videos.append(stream.dash_video)
        if stream.HasField("multi_dash_video"):
            videos.extend(stream.multi_dash_video.dash_videos)

        # 优先 AVC（codecid=7），PotPlayer / mpv 兼容性最好；同编码取高码率
        videos.sort(key=lambda v: (0 if v.codecid == 7 else 1, -v.bandwidth))
        video = _dash_video(videos[0]) if videos else None

        segments = 0
        if stream.HasField("segment_video"):
            segments = len(stream.segment_video.segment)

        label = (
            QN_LABELS.get(info.quality)
            or info.display_desc
            or info.new_description
            or info.description
        )
        qualities.append(
            {
                "qn": info.quality,
                "label": label or ("qn%d" % info.quality),
                "description": info.description,
                "new_description": info.new_description,
                "display_desc": info.display_desc,
                "superscript": info.superscript,
                "format": info.format,
                "need_vip": info.need_vip,
                "need_login": info.need_login,
                "intact": info.intact,
                "available": bool(video) or segments > 0,
                "err_code": info.err_code,
                "limit_title": info.limit.title,
                "limit_msg": info.limit.msg,
                "segments": segments,
                "video": video,
            }
        )

    qualities.sort(key=lambda q: -q["qn"])

    audios = [_dash_audio(item) for item in vod.dash_audio]
    audios.sort(key=lambda a: -a["bandwidth"])

    trial = None
    if reply.HasField("qn_trial_info"):
        qt = reply.qn_trial_info
        trial = {
            "trial_able": qt.trial_able,
            "remaining_times": qt.remaining_times,
            "time_length": qt.time_length,
            "start_toast": qt.start_toast.text,
            "end_toast": qt.end_toast.text,
        }

    duration_ms = vod.timelength or 0
    if not duration_ms and reply.HasField("play_arc"):
        duration_ms = reply.play_arc.duration_ms

    return {
        "quality": vod.quality,
        "format": vod.format,
        "duration_ms": duration_ms,
        "video_codecid": vod.video_codecid,
        "qualities": qualities,
        "audios": audios,
        "trial": trial,
        "play_arc": {
            "aid": reply.play_arc.aid,
            "cid": reply.play_arc.cid,
            "duration": reply.play_arc.duration,
            "is_preview": reply.play_arc.is_preview,
        }
        if reply.HasField("play_arc")
        else None,
    }


def pick_audio(play: Dict[str, Any], video: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """按视频的 audio_id 选音轨，找不到则取最高码率。"""
    audios = play.get("audios") or []
    if not audios:
        return None
    audio_id = (video or {}).get("audio_id")
    for item in audios:
        if audio_id and item["id"] == audio_id:
            return item
    return audios[0]


# ---------------------------------------------------------------------------
# 扫码登录（TV 端 auth_code 流程）
# ---------------------------------------------------------------------------

def app_sign(params: Dict[str, str]) -> Dict[str, str]:
    params = dict(params)
    params["appkey"] = APP_KEY
    params["ts"] = str(int(time.time()))
    items = sorted(params.items())
    query = "&".join(
        "%s=%s" % (
            urllib.parse.quote(str(key), safe=""),
            urllib.parse.quote(str(value), safe=""),
        )
        if value
        else urllib.parse.quote(str(key), safe="")
        for key, value in items
    )
    params["sign"] = hashlib.md5((query + APP_SEC).encode()).hexdigest()
    return params


def _post_signed(url: str, params: Dict[str, str], buvid: str) -> Dict[str, Any]:
    signed = app_sign(params)
    full = url + "?" + urllib.parse.urlencode(signed)
    headers = {
        "buvid": buvid,
        "env": "prod",
        "app-key": "android_hd",
        "user-agent": UA_PHONE,
        "x-bili-trace-id": "%s:%s:0:0" % (uuid.uuid4().hex, uuid.uuid4().hex[:16]),
        "x-bili-aurora-eid": "",
        "x-bili-aurora-zone": "",
        "bili-http-engine": "cronet",
        "content-type": "application/x-www-form-urlencoded; charset=utf-8",
    }
    req = urllib.request.Request(full, data=b"", headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def request_qr_code(buvid: str) -> Dict[str, str]:
    data = _post_signed(
        API_AUTH_CODE,
        {"local_id": "0", "platform": "android", "mobi_app": "android_hd"},
        buvid,
    )
    if data.get("code") != 0:
        raise BiliError(
            "qr_request_failed",
            "申请二维码失败: %s %s" % (data.get("code"), data.get("message")),
        )
    return {"url": data["data"]["url"], "auth_code": data["data"]["auth_code"]}


def poll_qr_code(auth_code: str, buvid: str) -> Dict[str, Any]:
    return _post_signed(API_POLL, {"auth_code": auth_code, "local_id": "0"}, buvid)


def fetch_uname(access_key: str) -> str:
    """尽力获取昵称，失败不影响登录。"""
    try:
        data = http_json(
            "https://api.bilibili.com/x/web-interface/nav?"
            + urllib.parse.urlencode({"access_key": access_key})
        )
        if data.get("code") == 0:
            return (data.get("data") or {}).get("uname") or ""
    except Exception:
        pass
    return ""


class PlayCache:
    """带 TTL 的取流结果缓存，减少对 gRPC 与会员试看次数的消耗。"""

    def __init__(self, ttl: int):
        self.ttl = ttl
        self._data: Dict[str, Any] = {}
        self._lock = threading.Lock()

    def get(self, key: str) -> Optional[Dict[str, Any]]:
        now = time.time()
        with self._lock:
            item = self._data.get(key)
            if not item:
                return None
            if now - item[0] > self.ttl:
                self._data.pop(key, None)
                return None
            return item[1]

    def put(self, key: str, value: Dict[str, Any]) -> None:
        with self._lock:
            self._data[key] = (time.time(), value)
            if len(self._data) > 200:
                oldest = sorted(self._data.items(), key=lambda kv: kv[1][0])[:50]
                for old_key, _ in oldest:
                    self._data.pop(old_key, None)
