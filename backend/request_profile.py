"""
请求画像模块 —— 统一管理「请求节奏」与「身份隔离」。

设计参考 JoeanAmier/XHS-Downloader 的三条经验：

1. 双 client 分离
   带身份的会话只用于需要会话的站点（mp.weixin.qq.com / i.weread.qq.com）；
   CDN（mmbiz.qpic.cn / mpvideo.qpic.cn）走独立会话，不挂 Cookie、不发 Referer，
   让海量资源请求无法被关联回账号身份。

2. 对数正态节流
   真人浏览间隔是右偏长尾（多数几秒，偶尔几十秒走神），对数正态正好是这个形状；
   「固定 sleep」或「固定下限 + 均匀抖动」下限过于整齐，机械感明显。

3. 魔数校验后缀
   微信图片同一 URL 可能返回 jpg/png/webp，Content-Type 与 URL 的 wx_fmt 参数都不完全可信，
   下载完成后按文件头真实字节纠正扩展名。

本模块只提供纯函数与惰性创建的 Session，不依赖任何业务模块，可独立测试。
"""

from __future__ import annotations

import logging
import math
import os
import random
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger(__name__)

__all__ = [
    "FILE_SIGNATURES",
    "SIGNATURE_WINDOW",
    "CDN_HOSTS",
    "detect_suffix",
    "normalize_ext",
    "resolve_suffix_path",
    "correct_suffix",
    "get_wait_time",
    "sleep_throttled",
    "backoff_delay",
    "sleep_backoff",
    "is_cdn_url",
    "get_cdn_client",
    "get_data_client",
    "reset_clients",
]

# ══════════════════════════════════════════════════════════════
#  一、魔数表：按文件头真实字节判断后缀
# ══════════════════════════════════════════════════════════════
# 结构：(偏移量, 十六进制签名, 后缀)
# 参考 https://en.wikipedia.org/wiki/List_of_file_signatures
FILE_SIGNATURES: tuple[tuple[int, bytes, str], ...] = (
    (0, b"\xff\xd8\xff", "jpg"),
    (0, b"\x89PNG\r\n\x1a\n", "png"),
    (0, b"GIF87a", "gif"),
    (0, b"GIF89a", "gif"),
    (8, b"WEBP", "webp"),
    (4, b"ftypavif", "avif"),
    (4, b"ftypavis", "avif"),
    (4, b"ftypheic", "heic"),
    (4, b"ftypheix", "heic"),
    (4, b"ftypmif1", "heic"),
    (4, b"ftypisom", "mp4"),
    (4, b"ftypmp42", "mp4"),
    (4, b"ftypMSNV", "mp4"),
    (4, b"ftypM4V", "m4v"),
    (4, b"ftypM4A", "m4a"),
    (4, b"ftypqt  ", "mov"),
    (0, b"\x1a\x45\xdf\xa3", "webm"),
    (0, b"ID3", "mp3"),
    (0, b"\xff\xfb", "mp3"),
    (0, b"\xff\xf3", "mp3"),
    (0, b"\x00\x00\x01\xba", "mpg"),
    (0, b"FLV\x01", "flv"),
)

# 读取文件头时需要的最少字节数（最长签名 = 偏移 8 + 4 = 12）
SIGNATURE_WINDOW: int = max(offset + len(sig) for offset, sig, _ in FILE_SIGNATURES)

# 同一格式的不同写法，避免 jpeg -> jpg 这类无意义的改名
_EXT_EQUIV = {
    "jpeg": "jpg",
    "jpe": "jpg",
    "jfif": "jpg",
    "tif": "tiff",
    "heif": "heic",
}


def normalize_ext(ext: str) -> str:
    """把扩展名归一化到同一格式族，用于比较是否需要改名。"""
    ext = (ext or "").lower().lstrip(".")
    return _EXT_EQUIV.get(ext, ext)


def detect_suffix(head: bytes) -> str | None:
    """按文件头魔数判断真实后缀；无法识别时返回 None。"""
    if not head:
        return None
    for offset, signature, suffix in FILE_SIGNATURES:
        if head[offset:offset + len(signature)] == signature:
            return suffix
    return None


def resolve_suffix_path(path: Path, head: bytes) -> Path:
    """根据文件头魔数计算最终落盘路径（不移动文件，由调用方负责改名）。

    识别失败、或只是 jpeg/jpg 这类同族写法差异时，原样返回 path。
    """
    real = detect_suffix(head)
    if not real:
        return path
    if normalize_ext(real) == normalize_ext(path.suffix):
        return path
    return path.with_suffix("." + real)


def correct_suffix(path: Path, head: bytes) -> Path:
    """根据魔数就地纠正文件后缀，返回最终路径（供已落盘文件使用）。"""
    target = resolve_suffix_path(path, head)
    if target == path:
        return path
    try:
        os.replace(path, target)
    except OSError as exc:  # 目标被占用等，保留原文件即可
        logger.debug("后缀纠正失败 %s -> %s: %s", path.name, target.name, exc)
        return path
    logger.debug("后缀纠正: %s -> %s", path.name, target.name)
    return target


# ══════════════════════════════════════════════════════════════
#  二、对数正态节流 + 指数退避
# ══════════════════════════════════════════════════════════════
def get_wait_time(
    avg_delay: float = 1.0,
    sigma: float = 0.6,
    min_delay: float | None = None,
    max_delay: float | None = None,
) -> float:
    """按对数正态分布采样一个等待时长，数学期望恰好等于 avg_delay。

    mu = ln(avg) - sigma²/2 使 E[X] = avg；sigma 越大长尾越明显。
    默认截断到 [0.2 × avg, 5 × avg]。风控真正盯的是**最小间隔**而不是平均值，
    所以对「相邻请求」这类场景应当显式传 min_delay 抬高下沿，
    否则 sigma 带来的左尾会让一部分请求比原来更密。
    """
    try:
        avg = max(0.05, float(avg_delay))
    except (TypeError, ValueError):
        avg = 0.05
    mu = math.log(avg) - (sigma ** 2) / 2
    try:
        delay = random.lognormvariate(mu, sigma)
    except (ValueError, OverflowError):
        delay = avg
    lo = avg * 0.2 if min_delay is None else max(0.0, float(min_delay))
    hi = avg * 5 if max_delay is None else max(lo, float(max_delay))
    return max(lo, min(hi, delay))


def sleep_throttled(
    avg_delay: float = 1.0,
    sigma: float = 0.6,
    min_delay: float | None = None,
    max_delay: float | None = None,
) -> float:
    """按对数正态节流睡一段时间，返回实际睡眠秒数。"""
    delay = get_wait_time(avg_delay, sigma, min_delay, max_delay)
    time.sleep(delay)
    return delay


def backoff_delay(attempt: int, base: float = 1.0, cap: float = 30.0, jitter: float = 0.3) -> float:
    """第 attempt 次失败后的退避时长（attempt 从 1 开始）。

    指数增长 + 对称抖动：2 的幂保证快速脱离瞬时故障，
    抖动避免多个任务在同一时刻齐步重试形成尖峰。
    """
    attempt = max(1, int(attempt))
    raw = min(cap, base * (2 ** (attempt - 1)))
    return max(0.0, raw * (1 + random.uniform(-jitter, jitter)))


def sleep_backoff(attempt: int, base: float = 1.0, cap: float = 30.0, jitter: float = 0.3) -> float:
    """指数退避睡眠，返回实际睡眠秒数。"""
    delay = backoff_delay(attempt, base=base, cap=cap, jitter=jitter)
    time.sleep(delay)
    return delay


# ══════════════════════════════════════════════════════════════
#  三、双 client 分离
# ══════════════════════════════════════════════════════════════
# 这些域名的资源是纯静态分发，与账号身份无关，不需要也不应该携带 Cookie / Referer
CDN_HOSTS: tuple[str, ...] = (
    "mmbiz.qpic.cn",
    "mmbiz.qlogo.cn",
    "mpvideo.qpic.cn",
    "res.wx.qq.com",
    "wx.qlogo.cn",
)

_local = threading.local()

# CDN 请求只保留最小必要头部：不带 Referer，不带 Cookie
_CDN_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)


def is_cdn_url(url: str) -> bool:
    """判断是否为微信静态资源 CDN 地址。"""
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return False
    if not host:
        return False
    return any(host == h or host.endswith("." + h) for h in CDN_HOSTS)


def _build_cdn_session() -> requests.Session:
    session = requests.Session()
    session.headers.clear()
    session.headers.update({"User-Agent": _CDN_UA})
    session.cookies.clear()
    try:
        retry = Retry(
            total=2,
            connect=2,
            read=2,
            backoff_factor=0.5,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset({"GET", "HEAD"}),
            raise_on_status=False,
        )
    except TypeError:  # 老版本 urllib3
        retry = Retry(total=2, backoff_factor=0.5)
    adapter = HTTPAdapter(max_retries=retry, pool_connections=8, pool_maxsize=16)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


def _build_data_session() -> requests.Session:
    session = requests.Session()
    adapter = HTTPAdapter(pool_connections=4, pool_maxsize=8)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


def get_cdn_client() -> requests.Session:
    """身份无关的 CDN 客户端（线程内单例）。

    与数据会话完全隔离：不共享 CookieJar、不发 Referer、不复用连接池。
    即使数据会话已经登录，CDN 流量也不会带上任何身份信息。

    按线程持有而非全局单例：Flask 请求线程、批量下载线程、RSS 调度线程
    会并发调用，`requests.Session` 的 CookieJar 与连接池都不是为跨线程共享设计的。
    """
    session = getattr(_local, "cdn", None)
    if session is None:
        session = _build_cdn_session()
        _local.cdn = session
    return session


def get_data_client() -> requests.Session:
    """身份相关的数据客户端（mp.weixin.qq.com / i.weread.qq.com 等），线程内单例。

    只做连接池复用，不注入 Cookie —— 多账号场景下由调用方在每次请求前
    显式 clear() CookieJar，避免 A 账号的会话被带到 B 账号的请求上。
    按线程隔离，使不同线程之间的 CookieJar 天然互不干扰。
    不挂 transport 层重试：业务侧已有 401 续期 / 499 轮换账号的完整状态机，
    传输层再重试会打乱这套判定。
    """
    session = getattr(_local, "data", None)
    if session is None:
        session = _build_data_session()
        _local.data = session
    return session


def reset_clients() -> None:
    """关闭并丢弃**当前线程**缓存的 Session，主要供测试与配置热更新使用。

    因为会话按线程持有，本函数只影响调用它的线程；其他线程会在下次取用时
    自动重建（并读取最新配置）。
    """
    for attr in ("cdn", "data"):
        session = getattr(_local, attr, None)
        if session is not None:
            try:
                session.close()
            except Exception:  # noqa: BLE001
                pass
            setattr(_local, attr, None)
