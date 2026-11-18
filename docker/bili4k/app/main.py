"""FastAPI HTTP 层：扫码登录、取流、MPD、健康检查。"""

import hashlib
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import auth, bili, config, mpd


app = FastAPI(
    title="Bili 4K Service",
    version=config.VERSION,
    description="B 站高画质取流 + DASH MPD 生成，供浏览器扩展拉起 PotPlayer。",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=config.CORS_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)

play_cache = bili.PlayCache(config.PLAY_CACHE_TTL)

# PotPlayer 分片代理的会话表：token -> 分片表（进程内，2 小时过期，最多 32 个）
SEG_TTL = 2 * 3600
SEG_MAX = 32
_seg_sessions: Dict[str, Dict[str, Any]] = {}
_seg_lock = threading.Lock()
SEG_NAME_RE = re.compile(r"^([va])-(\d+)\.m4s$")


def ok(data: Any) -> Dict[str, Any]:
    return {"ok": True, "data": data}


def _status_for(code: str) -> int:
    if code in ("session_not_found", "video_info_failed", "page_out_of_range"):
        return 404
    if code in ("stream_unavailable", "quality_unavailable", "login_required"):
        return 409
    return 400


@app.exception_handler(bili.BiliError)
def handle_bili_error(request: Request, exc: bili.BiliError):
    return JSONResponse(
        status_code=_status_for(exc.code),
        content={
            "ok": False,
            "error": {
                "code": exc.code,
                "message": exc.message,
                "path": request.url.path,
                "method": request.method,
            },
        },
    )


# 404 / 405 默认返回 {"detail": "Not Found"}，扩展只能显示「请求失败（HTTP 404）」，
# 分不清是「接口不存在（镜像旧）」还是「分片会话过期」。这里统一成 ok/error 结构，
# 并把请求路径与方法带回去。
_HTTP_CODES = {404: "not_found", 405: "method_not_allowed"}


@app.exception_handler(StarletteHTTPException)
def handle_http_error(request: Request, exc: StarletteHTTPException):
    code = _HTTP_CODES.get(exc.status_code, "http_error")
    if exc.status_code == 404:
        message = "接口不存在：%s %s（后端镜像可能是旧版本，缺少该路由）" % (
            request.method,
            request.url.path,
        )
    elif exc.status_code == 405:
        message = "请求方法不允许：%s %s（该路由只接受其他方法）" % (
            request.method,
            request.url.path,
        )
    else:
        message = str(exc.detail)
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "ok": False,
            "error": {
                "code": code,
                "message": message,
                "path": request.url.path,
                "method": request.method,
            },
        },
    )


@app.exception_handler(RequestValidationError)
def handle_validation_error(request: Request, exc: RequestValidationError):
    """422 也带出具体字段，避免只看到「请求失败（HTTP 422）」。"""
    errors = exc.errors()
    first = errors[0] if errors else {}
    loc = ".".join(str(item) for item in first.get("loc", ()) if item != "body")
    message = "参数校验失败（%s）：%s" % (
        loc or "-",
        first.get("msg") or "invalid",
    )
    return JSONResponse(
        status_code=422,
        content={
            "ok": False,
            "error": {
                "code": "bad_request",
                "message": message,
                "path": request.url.path,
                "method": request.method,
            },
        },
    )


# ---------------------------------------------------------------------------
# 基础
# ---------------------------------------------------------------------------

@app.get("/api/health")
def health():
    status = auth.current_account()
    return ok(
        {
            "status": "ok",
            "version": config.VERSION,
            "logged_in": status["logged_in"],
            "time": int(time.time()),
        }
    )


@app.get("/")
def index():
    return ok(
        {
            "service": "bili-4k-service",
            "version": config.VERSION,
            "endpoints": [
                "POST /api/auth/qr",
                "GET  /api/auth/qr/{session_id}",
                "GET  /api/auth/status",
                "POST /api/auth/logout",
                "GET  /api/video/info?bvid=...&page=1",
                "POST /api/play/resolve",
                "GET  /api/play/mpd?bvid=...&qn=120",
                "GET  /api/play/direct?bvid=...&qn=120",
                "GET  /api/play/potplayer.mpd?bvid=...&qn=120",
                "GET  /api/play/seg/{token}/{name}",
                "GET  /api/health",
            ],
        }
    )


# ---------------------------------------------------------------------------
# 登录
# ---------------------------------------------------------------------------

@app.post("/api/auth/qr")
def auth_qr_start():
    return ok(auth.login_manager.start())


@app.get("/api/auth/qr/{session_id}")
def auth_qr_poll(session_id: str):
    return ok(auth.login_manager.poll(session_id))


@app.get("/api/auth/status")
def auth_status():
    return ok(auth.current_account())


@app.post("/api/auth/logout")
def auth_logout():
    return ok(auth.logout())


# ---------------------------------------------------------------------------
# 视频信息
# ---------------------------------------------------------------------------

@app.get("/api/video/info")
def video_info(
    bvid: Optional[str] = Query(None),
    aid: Optional[int] = Query(None),
    page: int = Query(1, ge=1),
):
    return ok(bili.fetch_video_info(bvid=bvid, aid=aid, page=page))


# ---------------------------------------------------------------------------
# 取流
# ---------------------------------------------------------------------------

class ResolveBody(BaseModel):
    bvid: Optional[str] = None
    aid: Optional[int] = None
    cid: Optional[int] = None
    page: int = 1
    qn: Optional[int] = None


_play_locks: Dict[str, threading.Lock] = {}
_play_locks_guard = threading.Lock()


def _play_lock(key: str) -> threading.Lock:
    """同一取流 key 的单飞锁，避免 resolve/direct 并发时重复 gRPC。"""
    with _play_locks_guard:
        lock = _play_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _play_locks[key] = lock
        return lock


def _get_play(
    aid: int,
    cid: int,
    qn: int,
    metrics: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    access_key = auth.access_key()
    key = "%s:%s:%s:%s" % (
        aid,
        cid,
        qn,
        hashlib.sha1(access_key.encode()).hexdigest()[:12],
    )
    cached = play_cache.get(key)
    if cached is not None:
        if metrics is not None:
            metrics["cache_hit"] = True
        return cached

    with _play_lock(key):
        # 等锁期间可能已被同 key 的请求填好
        cached = play_cache.get(key)
        if cached is not None:
            if metrics is not None:
                metrics["cache_hit"] = True
            return cached
        grpc_started = time.perf_counter()
        try:
            reply = bili.call_playview(
                aid, cid, qn, access_key, bili.fetch_buvid()
            )
        except bili.BiliError as exc:
            # buvid 偶发被风控时换一个再试一次，避免一次抖动就让播放失败
            if exc.code != "grpc_error":
                raise
            bili.invalidate_buvid()
            reply = bili.call_playview(
                aid, cid, qn, access_key, bili.fetch_buvid(force=True)
            )
        if metrics is not None:
            metrics["cache_hit"] = False
            metrics["grpc_calls"] = metrics.get("grpc_calls", 0) + 1
            metrics["grpc_ms"] = metrics.get("grpc_ms", 0) + int(
                (time.perf_counter() - grpc_started) * 1000
            )
        play = bili.parse_playview(reply)
        play_cache.put(key, play)
        return play


def _find_quality(play: Dict[str, Any], qn: int) -> Optional[Dict[str, Any]]:
    for item in play["qualities"]:
        if item["qn"] == qn:
            return item
    return None


def _best_quality(play: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """优先返回下发了视频流的最高档位。"""
    for item in play["qualities"]:
        if item.get("video"):
            return item
    return play["qualities"][0] if play["qualities"] else None


def _resolve(
    bvid: Optional[str],
    aid: Optional[int],
    cid: Optional[int],
    page: int,
    qn: Optional[int],
    base_url: str,
) -> Dict[str, Any]:
    if not bvid and not aid:
        raise bili.BiliError("bad_request", "必须提供 bvid 或 aid")

    resolve_started = time.perf_counter()
    timings: Dict[str, Any] = {"cache_hit": None, "grpc_calls": 0, "grpc_ms": 0}

    # 视频信息与 buvid 并行预取：buvid 有 1 小时缓存，热路径几乎零成本
    pool = ThreadPoolExecutor(max_workers=2)
    try:
        info_future = pool.submit(
            bili.fetch_video_info, bvid=bvid, aid=aid, page=page
        )
        pool.submit(bili.fetch_buvid)
        info = info_future.result()
    finally:
        pool.shutdown(wait=False)
    aid_v = info["aid"]
    cid_v = int(cid) if cid else info["cid"]
    if not cid_v:
        raise bili.BiliError("bad_request", "无法解析 cid，请显式传入")

    qn = max(1, int(qn or config.DEFAULT_QN))
    requested_qn = qn
    probe_qn = max(qn, config.DEFAULT_QN)

    play = _get_play(aid_v, cid_v, probe_qn, metrics=timings)
    selected = _find_quality(play, qn)

    # 探测档位里没有该清晰度的流时，按目标 qn 再请求一次
    if (selected is None or not selected.get("video")) and probe_qn != qn:
        retry = _get_play(aid_v, cid_v, qn, metrics=timings)
        if _find_quality(retry, qn) is not None:
            play = retry
            selected = _find_quality(play, qn)

    fallback_from = None
    if selected is None:
        # 视频本身没有该档位（例如非 4K 视频默认请求 120），回退到最高可用档
        selected = _best_quality(play)
        if selected is None:
            raise bili.BiliError("quality_unavailable", "该视频没有可用清晰度")
        fallback_from = requested_qn
        qn = selected["qn"]

    video = selected.get("video")
    audio = bili.pick_audio(play, video)

    notice = ""
    trial = play.get("trial")
    if not video:
        reason = selected.get("limit_msg") or ""
        if selected.get("need_login"):
            notice = "该清晰度需要登录，请先扫码登录。"
        elif selected.get("need_vip"):
            notice = "该清晰度需要大会员，当前账号未下发视频流。"
        elif reason:
            notice = reason
        else:
            notice = "服务端未下发该清晰度的视频流。"
    elif trial and trial.get("trial_able") and selected.get("need_vip"):
        notice = (
            "未开通大会员时，该档位以试看形式下发（约 %d 秒，剩余 %d 次）。"
            % (trial.get("time_length", 0), trial.get("remaining_times", 0))
        )

    if fallback_from:
        notice = "该视频未提供 qn=%d，已自动选择 qn=%d。%s" % (
            fallback_from,
            qn,
            notice,
        )

    mpd_query = urllib.parse.urlencode(
        {"bvid": info["bvid"], "cid": cid_v, "qn": qn}
    )
    mpd_url = "%s/api/play/mpd?%s" % (base_url.rstrip("/"), mpd_query)
    pot_url = "%s/api/play/potplayer.mpd?%s" % (base_url.rstrip("/"), mpd_query)

    quality_list = []
    for item in play["qualities"]:
        row = {k: v for k, v in item.items() if k != "video"}
        stream = item.get("video") or {}
        row.update(
            {
                "codecid": stream.get("codecid"),
                "width": stream.get("width"),
                "height": stream.get("height"),
                "frame_rate": stream.get("frame_rate"),
                "bandwidth": stream.get("bandwidth"),
            }
        )
        quality_list.append(row)

    timings["resolve_ms"] = int(
        (time.perf_counter() - resolve_started) * 1000
    )

    return {
        "video": info,
        "requested_qn": requested_qn,
        "effective_qn": qn,
        "probe_qn": probe_qn,
        "actual_qn": play.get("quality"),
        "format": play.get("format"),
        "duration_ms": play.get("duration_ms", 0),
        "timings": timings,
        "selected": {
            "qn": selected["qn"],
            "label": selected["label"],
            "description": selected["description"],
            "new_description": selected["new_description"],
            "display_desc": selected["display_desc"],
            "superscript": selected["superscript"],
            "need_vip": selected["need_vip"],
            "need_login": selected["need_login"],
            "available": bool(video),
            "err_code": selected["err_code"],
            "limit_msg": selected.get("limit_msg", ""),
            "video": video,
            "audio": audio,
        },
        "qualities": quality_list,
        "trial": trial,
        "mpd_url": mpd_url,
        "potplayer_url": pot_url,
        "notice": notice,
        "logged_in": auth.current_account()["logged_in"],
    }


def _base_url(request: Request) -> str:
    return config.PUBLIC_BASE_URL or str(request.base_url).rstrip("/")


@app.post("/api/play/resolve")
def play_resolve(body: ResolveBody, request: Request):
    return ok(
        _resolve(
            bvid=body.bvid,
            aid=body.aid,
            cid=body.cid,
            page=body.page,
            qn=body.qn,
            base_url=_base_url(request),
        )
    )


@app.get("/api/play/mpd")
def play_mpd(
    request: Request,
    bvid: Optional[str] = Query(None),
    aid: Optional[int] = Query(None),
    cid: Optional[int] = Query(None),
    page: int = Query(1, ge=1),
    qn: int = Query(config.DEFAULT_QN),
):
    data = _resolve(
        bvid=bvid, aid=aid, cid=cid, page=page, qn=qn, base_url=_base_url(request)
    )
    selected = data["selected"]
    if not selected.get("available") or not selected.get("video"):
        raise bili.BiliError(
            "stream_unavailable",
            data.get("notice") or "该清晰度未下发视频流，无法生成 MPD",
        )

    try:
        xml = mpd.build_mpd(
            selected["video"],
            selected.get("audio"),
            data.get("duration_ms", 0),
            qn=selected["qn"],
        )
    except RuntimeError as exc:
        raise bili.BiliError("stream_unavailable", str(exc))
    return Response(
        content=xml,
        media_type="application/dash+xml; charset=utf-8",
        headers={
            # 不要返回 Content-Disposition：PotPlayer 会把 filename 当成相对路径，
            # 去请求 /"bili-xxx.mpd" 并 404，导致一直重试、最终报“服务器已关闭”。
            "Cache-Control": "no-store",
        },
    )


# ---------------------------------------------------------------------------
# PotPlayer 兼容：分片式 MPD + 分片代理
#
# PotPlayer 不认 SegmentBase/indexRange 的 on-demand 单文件 MPD（会反复重拉
# manifest、不请求子资源）。这里改为 BaseURL + AdaptationSet 级 SegmentTemplate，
# 子分片由后端按 sidx 计算出的字节范围回源转发。
# ---------------------------------------------------------------------------

def _seg_put(session: Dict[str, Any]) -> str:
    token = uuid.uuid4().hex
    now = time.time()
    with _seg_lock:
        expired = [k for k, v in _seg_sessions.items() if now - v["created"] > SEG_TTL]
        for key in expired:
            _seg_sessions.pop(key, None)
        while len(_seg_sessions) >= SEG_MAX:
            oldest = min(_seg_sessions, key=lambda k: _seg_sessions[k]["created"])
            _seg_sessions.pop(oldest, None)
        session["created"] = now
        _seg_sessions[token] = session
    return token


def _seg_get(token: str) -> Optional[Dict[str, Any]]:
    with _seg_lock:
        session = _seg_sessions.get(token)
    if not session:
        return None
    if time.time() - session["created"] > SEG_TTL:
        return None
    return session


def _proxy_range(table: Dict[str, Any], start: int, end: int) -> StreamingResponse:
    last_error = None
    for url in [table["url"]] + list(table.get("backup_urls") or []):
        req = urllib.request.Request(
            url, headers=mpd.stream_headers(url, "bytes=%d-%d" % (start, end))
        )
        try:
            upstream = urllib.request.urlopen(req, timeout=30)
        except urllib.error.HTTPError as exc:
            last_error = "HTTP %s" % exc.code
            continue
        except Exception as exc:  # noqa: BLE001
            last_error = "%s: %s" % (type(exc).__name__, exc)
            continue

        def iterator(resp=upstream):
            try:
                while True:
                    chunk = resp.read(1048576)
                    if not chunk:
                        break
                    yield chunk
            finally:
                resp.close()

        return StreamingResponse(
            iterator(),
            status_code=200,
            media_type="video/mp4",
            headers={
                # 每个分片对播放器来说都是独立资源，必须按整段返回；
                # 若回 206 + Content-Range（带原文件绝对偏移），ffmpeg/PotPlayer
                # 会报 "Unexpected offset: expected 0" 并拒绝解析。
                "Content-Length": str(end - start + 1),
                "Cache-Control": "no-store",
            },
        )
    raise bili.BiliError(
        "stream_unavailable", "CDN 拒绝分片请求（%s），请稍后重试" % last_error
    )


@app.get("/api/play/potplayer.mpd")
def play_pot_mpd(
    request: Request,
    bvid: Optional[str] = Query(None),
    aid: Optional[int] = Query(None),
    cid: Optional[int] = Query(None),
    page: int = Query(1, ge=1),
    qn: int = Query(config.DEFAULT_QN),
):
    data = _resolve(
        bvid=bvid, aid=aid, cid=cid, page=page, qn=qn, base_url=_base_url(request)
    )
    selected = data["selected"]
    if not selected.get("available") or not selected.get("video"):
        raise bili.BiliError(
            "stream_unavailable",
            data.get("notice") or "该清晰度未下发视频流，无法生成 MPD",
        )

    video_table = mpd.fetch_segment_table(selected["video"])
    if not video_table:
        raise bili.BiliError(
            "stream_unavailable",
            "无法解析视频分片索引（CDN 可能拒绝或超时），请稍后重试",
        )
    audio_table = None
    if selected.get("audio"):
        audio_table = mpd.fetch_segment_table(selected["audio"])

    token = _seg_put(
        {
            "video": video_table,
            "audio": audio_table,
            "created": time.time(),
        }
    )
    xml = mpd.build_pot_mpd(
        selected["video"],
        selected.get("audio"),
        video_table,
        audio_table,
        data.get("duration_ms", 0),
        _base_url(request),
        token,
    )
    return Response(
        content=xml,
        media_type="application/dash+xml; charset=utf-8",
        headers={"Cache-Control": "no-store"},
    )


@app.get("/api/play/seg/{token}/{name}")
def play_seg(token: str, name: str):
    session = _seg_get(token)
    if not session:
        raise bili.BiliError("session_not_found", "播放会话不存在或已过期，请重新取流")

    if name.endswith("-init.mp4"):
        table = session["video"] if name.startswith("v") else session["audio"]
        if not table:
            raise bili.BiliError("session_not_found", "该播放会话没有对应音轨")
        return _proxy_range(table, 0, int(table["init_end"]))

    match = SEG_NAME_RE.match(name)
    if not match:
        raise bili.BiliError("bad_request", "未知分片名称")
    key, index = match.group(1), int(match.group(2)) - 1
    table = session["video"] if key == "v" else session["audio"]
    if not table or index < 0 or index >= len(table["segments"]):
        raise bili.BiliError("bad_request", "分片序号越界")
    start, end, _ = table["segments"][index]
    return _proxy_range(table, start, end)


# ---------------------------------------------------------------------------
# 直连模式：只下发 CDN 直链，播放器（mpv）直连，不再经过 NAS 转发
#
# 与 lib/plugin/pl_player/controller.dart 的做法一致：视频、音频各一条直链，
# 用 mpv 的 edl:// 把两条流合成一个播放列表，带宽路径是「B 站 → 播放器」。
# ---------------------------------------------------------------------------

def _url_deadline(url: str) -> int:
    """从直链的 deadline 参数取过期时间戳（秒），取不到返回 0。"""
    try:
        query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        return int(query.get("deadline", ["0"])[0])
    except (ValueError, TypeError):
        return 0


PROBE_CHUNK = 64 * 1024
_probe_cache = bili.TTLCache(512)


def _probe_key(url: str) -> str:
    """node+path 作为测量缓存键：签名 query 与 deadline 不影响吞吐。"""
    parts = urllib.parse.urlsplit(url)
    return "%s%s" % (parts.netloc, parts.path)


def _probe_url(
    url: str,
    bandwidth: int,
    budget: float,
    stop_event: threading.Event,
    partial: Dict[str, Any],
) -> Dict[str, Any]:
    """读一段数据测吞吐，并折算成相对该档位 bandwidth 的倍数。

    预算内分块读取（最多 PROBE_MAX_BYTES），检查 stop_event 以便有快节点时
    立即收敛。测量值按节点缓存：达标缓存久、未达标只缓存片刻。
    """
    host = urllib.parse.urlsplit(url).netloc or url
    key = _probe_key(url)
    cached = _probe_cache.get(key)
    if cached is not None:
        return {
            "host": cached["host"],
            "url": url,
            "ok": cached["ok"],
            "bytes": cached["bytes"],
            "ms": cached["ms"],
            "bps": cached["bps"],
            "error": cached["error"],
            "cached": True,
            "budget_hit": False,
        }

    started = time.monotonic()
    deadline = started + budget
    got = 0
    error = ""
    req = urllib.request.Request(
        url,
        headers=mpd.stream_headers(url, "bytes=0-%d" % (config.PROBE_MAX_BYTES - 1)),
    )
    try:
        with urllib.request.urlopen(req, timeout=config.PROBE_SOCKET_TIMEOUT) as resp:
            while got < config.PROBE_MAX_BYTES:
                if stop_event.is_set() or time.monotonic() >= deadline:
                    break
                chunk = resp.read(min(PROBE_CHUNK, config.PROBE_MAX_BYTES - got))
                if not chunk:
                    break
                got += len(chunk)
                partial[key] = (got, started)
    except urllib.error.HTTPError as exc:
        error = "HTTP %d" % exc.code
    except Exception as exc:
        error = "%s: %s" % (type(exc).__name__, str(exc)[:120])

    elapsed = max(time.monotonic() - started, 0.001)
    if not error and got == 0:
        error = "no_data"
    bps = round(got * 8 / elapsed, 1) if got else 0.0
    budget_hit = bool(got) and time.monotonic() >= deadline
    result = {
        "host": host,
        "url": url,
        "ok": got > 0,
        "bytes": got,
        "ms": int(elapsed * 1000),
        "bps": bps,
        "error": error,
        "cached": False,
        "budget_hit": budget_hit,
    }
    if got:
        ratio = bps / bandwidth if bandwidth > 0 else None
        ttl = config.PROBE_CACHE_TTL
        if ratio is None or ratio < config.PROBE_MIN_RATIO:
            ttl = config.PROBE_CACHE_TTL_SLOW
        _probe_cache.put(
            key,
            {
                "host": host,
                "ok": True,
                "bytes": got,
                "ms": result["ms"],
                "bps": bps,
                "error": "",
            },
            ttl,
        )
    return result


def _probe_detail(detail: Dict[str, Any], bandwidth: int) -> Dict[str, Any]:
    """补上相对码率的倍数、Mbps、是否达标，供排序与展示。"""
    bps = float(detail.get("bps") or 0)
    ratio = bps / bandwidth if bandwidth > 0 and bps > 0 else None
    detail["ratio"] = round(ratio, 2) if ratio is not None else None
    detail["speed_mbps"] = round(bps / 1e6, 2)
    detail["meets_threshold"] = bool(
        ratio is not None and ratio >= config.PROBE_MIN_RATIO
    )
    return detail


def _pick_urls(urls: List[str], bandwidth: int):
    """并发探测候选节点，按实测吞吐择优。

    返回 (候选顺序, 是否至少一个可用, 探活明细, 择优摘要)。
    - 预算 PROBE_BUDGET 秒；有节点达到 PROBE_FAST_RATIO 时提前返回；
    - 达标节点排在未达标节点之前，未达标节点不能排第一（除非全都未达标）；
    - 全部失败也不抛异常，仍按原顺序返回，由调用方决定兜底。
    """
    urls = [u for u in urls if u]
    if not urls:
        return [], False, [], {}

    stop_event = threading.Event()
    partial: Dict[str, Any] = {}
    started = time.monotonic()
    deadline = started + config.PROBE_BUDGET
    results: Dict[str, Dict[str, Any]] = {}

    pool = ThreadPoolExecutor(max_workers=len(urls))
    try:
        futures = {
            pool.submit(
                _probe_url, url, bandwidth, config.PROBE_BUDGET, stop_event, partial
            ): url
            for url in urls
        }
        pending = set(futures)
        while pending:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            done, pending = wait(
                pending, timeout=remaining, return_when=FIRST_COMPLETED
            )
            fast = False
            for future in done:
                detail = _probe_detail(future.result(), bandwidth)
                results[detail["url"]] = detail
                if detail["ok"] and (
                    detail["ratio"] is not None
                    and detail["ratio"] >= config.PROBE_FAST_RATIO
                ):
                    fast = True
            if fast:
                stop_event.set()
                break

        if pending:
            stop_event.set()
            # 给已被唤醒的线程一点时间落盘，不宜把预算再拉长
            wait(pending, timeout=0.2)
            for future in pending:
                if future.done() and not future.cancelled():
                    detail = _probe_detail(future.result(), bandwidth)
                    results[detail["url"]] = detail
        for url in urls:
            if url in results:
                continue
            key = _probe_key(url)
            got, begin = partial.get(key, (0, started))
            elapsed = max(time.monotonic() - begin, 0.001) if got else 1
            bps = round(got * 8 / elapsed, 1) if got else 0.0
            detail = _probe_detail(
                {
                    "host": urllib.parse.urlsplit(url).netloc or url,
                    "url": url,
                    "ok": got > 0,
                    "bytes": got,
                    "ms": int((time.monotonic() - started) * 1000),
                    "bps": bps,
                    "error": "" if got else "budget_exhausted",
                    "cached": False,
                    "budget_hit": True,
                },
                bandwidth,
            )
            results[url] = detail
    finally:
        # 不在请求线程里等慢节点收尾（它们会自己看到 stop_event 退出）
        pool.shutdown(wait=False)

    index = {url: i for i, url in enumerate(urls)}
    details = [results[url] for url in urls]
    ordered_details = sorted(
        details,
        key=lambda d: (
            0 if d["meets_threshold"] else (1 if d["ok"] else 2),
            -(d["ratio"] or 0),
            index.get(d["url"], 0),
        ),
    )
    alive = any(d["ok"] for d in details)
    best = ordered_details[0] if ordered_details else {}
    selection = {
        "host": best.get("host", ""),
        "ratio": best.get("ratio"),
        "meets_threshold": bool(best.get("meets_threshold")),
        "threshold": config.PROBE_MIN_RATIO,
        "budget": config.PROBE_BUDGET,
        "budget_hit": any(d.get("budget_hit") for d in details),
        "cached": bool(details) and all(d.get("cached") for d in details),
        "degraded": alive and not any(d["meets_threshold"] for d in details),
    }
    return [d["url"] for d in ordered_details], alive, details, selection


def _probe_summary(details: List[Dict[str, Any]]) -> str:
    """把探活明细压成一行，例如 upos-sz-mirrorcoso1 134.5Mbps 9.8x ✓。"""
    if not details:
        return "无候选地址"
    parts = []
    for d in details:
        if d["ok"]:
            speed = d.get("speed_mbps") or 0
            text = "%s %.1fMbps" % (d["host"], speed)
            if d.get("ratio") is not None:
                text += " %.1fx" % d["ratio"]
            if d.get("meets_threshold"):
                text += " ✓"
            if d.get("budget_hit"):
                text += " ~"
            if d.get("cached"):
                text += " cache"
            parts.append(text)
        else:
            parts.append("%s(%s)" % (d["host"], d["error"] or "fail"))
    return "、".join(parts)


def _edl(video_url: str, audio_url: str) -> str:
    """mpv 的 edl:// 多流写法，%N% 里的 N 是 URL 的 UTF-8 字节长度。"""

    def part(url: str) -> str:
        return "%%%d%%%s" % (len(url.encode("utf-8")), url)

    return "edl://!no_chapters;%s;!new_stream;!no_chapters;%s" % (
        part(video_url),
        part(audio_url),
    )


def _mpv_args(edl: str, url: str) -> list:
    """App 流不传 --referrer：upos-sz 带 B 站 Referer 会判成网页盗链而 403。"""
    args = []
    if not mpd.is_app_stream(url):
        args.append("--referrer=%s" % mpd.WEB_REFERER)
    args.extend(
        [
            "--user-agent=%s" % mpd.APP_UA,
            "--cache=yes",
            "--demuxer-max-bytes=512MiB",
            # 音频由 EDL 第二个流提供，禁止 mpv 再去找外部音轨
            "--audio-file-auto=no",
            edl,
        ]
    )
    return args


@app.get("/api/play/direct")
def play_direct(
    request: Request,
    bvid: Optional[str] = Query(None),
    aid: Optional[int] = Query(None),
    cid: Optional[int] = Query(None),
    page: int = Query(1, ge=1),
    qn: int = Query(config.DEFAULT_QN),
):
    direct_started = time.perf_counter()
    base_url = _base_url(request)
    data = _resolve(
        bvid=bvid, aid=aid, cid=cid, page=page, qn=qn, base_url=base_url
    )
    selected = data["selected"]
    video = selected.get("video")
    audio = selected.get("audio")
    if not selected.get("available") or not video:
        raise bili.BiliError(
            "stream_unavailable",
            data.get("notice") or "该清晰度未下发视频流，无法直连播放",
        )
    if not audio:
        raise bili.BiliError(
            "stream_unavailable", "服务端未下发音频流，无法拼出完整直连播放"
        )

    deadlines = [d for d in (_url_deadline(video["url"]), _url_deadline(audio["url"])) if d]
    expires_at = min(deadlines) if deadlines else 0
    now = int(time.time())

    # 候选顺序对齐 PiliPlus getCdnUrl：upos 优先、mcdn 其次；
    # 同一档位内按实测吞吐择优（档位不变），视频/音频两组探测并发。
    video_bw = int(video.get("bandwidth") or 0)
    audio_bw = int(audio.get("bandwidth") or 0)
    probe_started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=2) as pool:
        video_future = pool.submit(
            _pick_urls, mpd.candidate_urls(video), video_bw
        )
        audio_future = pool.submit(
            _pick_urls, mpd.candidate_urls(audio), audio_bw
        )
        video_urls, video_ok, video_probe, video_sel = video_future.result()
        audio_urls, audio_ok, audio_probe, audio_sel = audio_future.result()
    probe_ms = int((time.perf_counter() - probe_started) * 1000)

    if not video_urls:
        raise bili.BiliError(
            "stream_unavailable",
            "该档位（qn=%d %s）没有可用的视频 CDN 地址。"
            % (selected["qn"], selected.get("label") or ""),
        )
    if not audio_urls:
        raise bili.BiliError("stream_unavailable", "该档位没有可用的音频 CDN 地址")

    edl = _edl(video_urls[0], audio_urls[0])
    args = _mpv_args(edl, video_urls[0])

    notice = data.get("notice", "")
    hints = []
    blocked = [d for d in video_probe + audio_probe if not d["ok"]]
    if blocked:
        hints.append("部分 CDN 节点探测失败（%s）。" % _probe_summary(blocked))
    degraded = [s for s in (video_sel, audio_sel) if s.get("degraded")]
    if degraded:
        hosts = "、".join(
            "%s %.1fx" % (s.get("host", ""), s.get("ratio") or 0)
            for s in degraded
        )
        hints.append(
            "没有节点达到 %.1f 倍码率门槛，已选择相对最快的 %s，可能仍需缓冲。"
            % (config.PROBE_MIN_RATIO, hosts)
        )
    if not video_ok or not audio_ok:
        hints.append(
            "CDN 探测全部失败，已回退首选地址，可能无法播放（%s）。"
            % _probe_summary(video_probe + audio_probe)
        )
    if hints:
        notice = ("%s %s" % (notice, "".join(hints))).strip()
    direct_ok = True

    query = urllib.parse.urlencode(
        {"bvid": data["video"]["bvid"], "cid": data["video"]["cid"], "qn": selected["qn"]}
    )
    api_url = "%s/api/play/direct?%s" % (base_url.rstrip("/"), query)
    proxy_url = "%s/api/play/potplayer.mpd?%s" % (base_url.rstrip("/"), query)
    launch_url = (
        "pili://play?mode=mpv&api=%s" % urllib.parse.quote(api_url, safe="")
        if direct_ok
        else ""
    )

    return ok(
        {
            "video": data["video"],
            "selected": selected,
            "duration_ms": data.get("duration_ms", 0),
            "timings": {
                **(data.get("timings") or {}),
                "probe_ms": probe_ms,
                "total_ms": int((time.perf_counter() - direct_started) * 1000),
            },
            "qualities": data["qualities"],
            "trial": data.get("trial"),
            "notice": notice,
            "direct": {
                "ok": direct_ok,
                "video_url": video_urls[0],
                "video_backup_urls": video_urls[1:],
                "audio_url": audio_urls[0],
                "audio_backup_urls": audio_urls[1:],
                "user_agent": mpd.APP_UA,
                # App 流不带 Referer：带上反而被 upos-sz-* 判成网页盗链而 403
                "referer": ""
                if mpd.is_app_stream(video_urls[0])
                else mpd.WEB_REFERER,
                "codecid": video.get("codecid"),
                "codecs": mpd._codec_string(
                    int(video.get("codecid") or 7),
                    int(video.get("height") or 0),
                    str(video.get("frame_rate") or ""),
                ),
                "width": video.get("width"),
                "height": video.get("height"),
                "frame_rate": video.get("frame_rate"),
                "bandwidth": video.get("bandwidth"),
                "audio_id": audio.get("id"),
                "audio_bandwidth": audio.get("bandwidth"),
                "expires_at": expires_at,
                "expires_in": max(0, expires_at - now) if expires_at else 0,
                # 每个节点的实测吞吐（host/ok/ms/bps/speed_mbps/ratio/...），
                # 便于事后判断为什么选了这个节点
                "probe": {
                    "video": [{k: v for k, v in d.items() if k != "url"} for d in video_probe],
                    "audio": [{k: v for k, v in d.items() if k != "url"} for d in audio_probe],
                },
                "selection": {
                    # 档位始终是请求（或服务端最高可用）档，不因吞吐自动降级
                    "quality_qn": selected["qn"],
                    "threshold": config.PROBE_MIN_RATIO,
                    "budget": config.PROBE_BUDGET,
                    "video": video_sel,
                    "audio": audio_sel,
                },
            },
            "mpv": {
                "available": direct_ok,
                "edl": edl,
                "args": args,
                "command": "mpv " + " ".join(
                    '"%s"' % a if " " in a else a for a in args
                ),
            },
            "launch_url": launch_url,
            "proxy_url": proxy_url,
            "api_url": api_url,
        }
    )
