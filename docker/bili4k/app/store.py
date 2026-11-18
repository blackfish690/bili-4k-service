"""凭证存储：落盘到可挂载目录，读写全程脱敏，不打印明文 access_key。"""

import json
import os
import threading
import time
from typing import Any, Dict, Optional

from . import config


def mask_secret(value: Optional[str], keep: int = 4) -> str:
    """脱敏：只保留首尾少量字符，其余用 * 替代。"""
    if not value:
        return ""
    if len(value) <= keep * 2:
        return "*" * len(value)
    return "%s%s%s" % (value[:keep], "*" * 8, value[-keep:])


class CredStore:
    """creds.json 的线程安全读写封装。"""

    def __init__(self, path=None):
        self.path = path or config.CREDS_FILE
        self._lock = threading.RLock()

    def load(self) -> Dict[str, Any]:
        with self._lock:
            try:
                with open(self.path, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                return data if isinstance(data, dict) else {}
            except FileNotFoundError:
                return {}
            except (OSError, ValueError):
                return {}

    def save(self, creds: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(creds, fh, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass
            return creds

    def clear(self) -> None:
        with self._lock:
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass

    def access_key(self) -> str:
        return str(self.load().get("access_key") or "")

    def status(self) -> Dict[str, Any]:
        creds = self.load()
        access_key = str(creds.get("access_key") or "")
        expires_at = creds.get("expires_at")
        expired = bool(expires_at and expires_at < int(time.time()))
        return {
            "logged_in": bool(access_key) and not expired,
            "expired": expired,
            "mid": creds.get("mid"),
            "uname": creds.get("uname") or "",
            "access_key_masked": mask_secret(access_key),
            "created_at": creds.get("created_at"),
            "expires_at": expires_at,
            "source": creds.get("source", ""),
        }


store = CredStore()
