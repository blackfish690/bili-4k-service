"""服务配置：全部通过环境变量覆盖，便于 Docker / 飞牛部署。"""

import os
from pathlib import Path

VERSION = "1.0.0"


def _env(name: str, default: str) -> str:
    value = os.environ.get(name)
    return value if value not in (None, "") else default


# 凭证等持久化数据目录，容器里挂载到 /data
DATA_DIR = Path(_env("BILI4K_DATA_DIR", "/data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
CREDS_FILE = DATA_DIR / "creds.json"

PORT = int(_env("PORT", "8000"))

# 默认探测的清晰度，120 = 4K。取流时会用 max(qn, DEFAULT_QN) 请求，
# 这样一次请求即可拿到完整的可用档位列表。
DEFAULT_QN = int(_env("BILI4K_DEFAULT_QN", "120"))

# 后端对外地址（可选）。反向代理 / 容器端口映射时用它生成 MPD 播放地址。
# 留空则根据请求的 Host / X-Forwarded-* 自动推断。
PUBLIC_BASE_URL = _env("BILI4K_PUBLIC_BASE_URL", "").rstrip("/")

# 扫码会话轮询超时（秒）
QR_TTL = int(_env("BILI4K_QR_TTL", "180"))

# 同一 (aid, cid, qn, 凭证) 的取流结果缓存秒数，避免重复消耗会员试看次数
PLAY_CACHE_TTL = int(_env("BILI4K_PLAY_CACHE_TTL", "300"))

# 允许的跨域来源，逗号分隔；扩展默认允许全部
CORS_ORIGINS = [
    origin.strip()
    for origin in _env("BILI4K_CORS_ORIGINS", "*").split(",")
    if origin.strip()
]

# ---- 直连节点择优（按实测吞吐折算为相对 bandwidth 的倍数） ----

# 单类（视频/音频）探活总预算秒数；超时后按已测到的部分结果选相对最好的
PROBE_BUDGET = float(_env("BILI4K_PROBE_BUDGET", "1.5"))
# 单节点单次 socket 操作超时秒数
PROBE_SOCKET_TIMEOUT = float(_env("BILI4K_PROBE_SOCKET_TIMEOUT", "0.75"))
# 单节点最多读取字节数（2 MB 量级，够算出稳定速率又不会拖太久）
PROBE_MAX_BYTES = int(_env("BILI4K_PROBE_MAX_BYTES", str(2 * 1024 * 1024)))
# 达标门槛：实测 bps >= 倍数 × bandwidth 才算达标，低于门槛的节点不能排第一
PROBE_MIN_RATIO = float(_env("BILI4K_PROBE_MIN_RATIO", "1.5"))
# 提前返回门槛：有节点实测 >= 该倍数时立即停止其余探测
PROBE_FAST_RATIO = float(_env("BILI4K_PROBE_FAST_RATIO", "2.0"))
# 探活测量缓存秒数：达标节点缓存久一些，未达标节点只缓存片刻，
# 避免把「刚才很慢的节点」长期固化，也避免每次播放都重新完整探测
PROBE_CACHE_TTL = int(_env("BILI4K_PROBE_CACHE_TTL", "60"))
PROBE_CACHE_TTL_SLOW = int(_env("BILI4K_PROBE_CACHE_TTL_SLOW", "15"))

# ---- 请求路径缓存 ----

# 视频信息（bvid/aid -> aid/cid/分P）缓存秒数
VIDEO_INFO_TTL = int(_env("BILI4K_VIDEO_INFO_TTL", "120"))
# buvid3 缓存秒数，避免每次 gRPC 都重新取
BUVID_TTL = int(_env("BILI4K_BUVID_TTL", "3600"))
