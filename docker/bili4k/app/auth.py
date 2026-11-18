"""扫码登录会话管理：申请二维码、轮询状态、成功后落盘凭证。"""

import base64
import io
import threading
import time
import uuid
from typing import Any, Dict, Optional

import qrcode
from qrcode.constants import ERROR_CORRECT_M

from . import bili, config
from .store import store


def qr_png_data_url(text: str) -> str:
    img = qrcode.make(text, error_correction=ERROR_CORRECT_M, box_size=8, border=2)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def _save_login(data: Dict[str, Any]) -> Dict[str, Any]:
    token = data.get("token_info") or data
    access_key = (
        token.get("access_token")
        or token.get("access_key")
        or data.get("access_key")
        or ""
    )
    cookies = {
        item["name"]: item["value"]
        for item in (data.get("cookie_info") or {}).get("cookies", [])
        if item.get("name")
    }
    expires_in = token.get("expires_in")
    now = int(time.time())
    creds = {
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "saved_at": now,
        "mid": token.get("mid") or data.get("mid"),
        "access_key": access_key,
        "refresh_token": token.get("refresh_token"),
        "expires_in": expires_in,
        "expires_at": now + int(expires_in) if expires_in else None,
        "cookies": cookies,
        "uname": bili.fetch_uname(access_key) if access_key else "",
        "source": "passport-tv-login/qrcode/poll",
    }
    store.save(creds)
    return creds


class QrLoginManager:
    """单进程内的扫码会话表。二维码内容与 auth_code 仅存内存。"""

    MAX_AGE = 900  # 15 分钟后清理僵尸会话

    def __init__(self):
        self._sessions: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()

    def start(self) -> Dict[str, Any]:
        self._cleanup()
        buvid = bili.fetch_buvid()
        code = bili.request_qr_code(buvid)
        session_id = uuid.uuid4().hex
        now = int(time.time())
        with self._lock:
            self._sessions[session_id] = {
                "auth_code": code["auth_code"],
                "buvid": buvid,
                "created": now,
                "status": "pending",
                "message": "等待扫码",
            }
        return {
            "session_id": session_id,
            "qr_url": code["url"],
            "qr_image": qr_png_data_url(code["url"]),
            "expires_in": config.QR_TTL,
            "status": "pending",
        }

    def poll(self, session_id: str) -> Dict[str, Any]:
        with self._lock:
            session = self._sessions.get(session_id)
        if not session:
            raise bili.BiliError("session_not_found", "扫码会话不存在或已清理")
        if session["status"] in ("confirmed", "expired", "error"):
            return self._view(session_id, session)

        if int(time.time()) - session["created"] > config.QR_TTL:
            with self._lock:
                session["status"] = "expired"
                session["message"] = "二维码已过期，请重新获取"
            return self._view(session_id, session)

        try:
            res = bili.poll_qr_code(session["auth_code"], session["buvid"])
        except Exception as exc:  # 网络抖动按未扫码处理
            return {
                "session_id": session_id,
                "status": "pending",
                "message": "轮询失败，请稍后重试: %s" % exc,
                "logged_in": False,
            }

        code = res.get("code")
        if code == 0:
            creds = _save_login(res["data"])
            with self._lock:
                session["status"] = "confirmed"
                session["message"] = "登录成功"
            status = store.status()
            return {
                "session_id": session_id,
                "status": "confirmed",
                "message": "登录成功",
                "logged_in": True,
                "mid": creds.get("mid"),
                "uname": creds.get("uname") or "",
                "account": status,
            }

        if code == 86090:
            state, message = "scanned", "已扫码，请在手机上确认"
        elif code in (86101, 86039):
            state, message = "pending", "等待扫码"
        elif code == 86038:
            state, message = "expired", "二维码已过期，请重新获取"
        else:
            state, message = "error", "登录失败: %s %s" % (code, res.get("message"))

        with self._lock:
            session["status"] = state
            session["message"] = message
        return {
            "session_id": session_id,
            "status": state,
            "message": message,
            "logged_in": False,
            "code": code,
        }

    @staticmethod
    def _view(session_id: str, session: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "session_id": session_id,
            "status": session["status"],
            "message": session.get("message", ""),
            "logged_in": session["status"] == "confirmed",
            "account": store.status(),
        }

    def _cleanup(self) -> None:
        cutoff = int(time.time()) - self.MAX_AGE
        with self._lock:
            for key in [k for k, v in self._sessions.items() if v["created"] < cutoff]:
                self._sessions.pop(key, None)


login_manager = QrLoginManager()


def current_account() -> Dict[str, Any]:
    return store.status()


def logout() -> Dict[str, Any]:
    store.clear()
    return {"logged_in": False}


def access_key() -> str:
    return store.access_key()
