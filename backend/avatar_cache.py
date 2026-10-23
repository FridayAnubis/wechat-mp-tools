"""公众号头像缓存与文章页头像补全，不提供任意 URL 下载接口。"""

from __future__ import annotations

import base64
import binascii
import hashlib
import html
import ipaddress
import os
import re
import socket
import tempfile
import threading
import time
from html.parser import HTMLParser
from urllib.parse import parse_qs, unquote, urljoin, urlsplit

import requests
from flask import abort, send_from_directory

from backend.config import DATA_DIR, get_default_wechat_ua

AVATAR_DIR = DATA_DIR / "avatars"
MAX_AVATAR_BYTES = 2 * 1024 * 1024
MAX_PAGE_BYTES = 2 * 1024 * 1024
RETRY_SECONDS = 600
REFRESH_SECONDS = 86400
IMAGE_TYPES = {"image/jpeg": ".jpg", "image/png": ".png",
               "image/gif": ".gif", "image/webp": ".webp"}
_locks = [threading.Lock() for _ in range(16)]
_failures: dict[str, float] = {}
_article_avatars: dict[str, str] = {}
_state_lock = threading.Lock()


def normalize_avatar_account_id(value: str) -> str:
    """统一头像归属使用的 fakeid、__biz 和微信读书 bookId。"""
    value = unquote(str(value or "")).strip()
    if not value or "${" in value:
        return ""
    if value.startswith("MP_WXS_"):
        return value
    if value.isascii() and value.isdigit():
        return "MP_WXS_" + value
    try:
        decoded = base64.b64decode(value + "=" * (-len(value) % 4), validate=True).decode("ascii")
        if decoded.isdigit():
            return "MP_WXS_" + decoded
    except (ValueError, UnicodeError, binascii.Error):
        pass
    return value


def avatar_account_ids(record: dict) -> set[str]:
    """保留所有归属证据，避免一个匹配字段掩盖另一个冲突字段。"""
    ids = {normalize_avatar_account_id(record.get(key))
           for key in ("avatar_account_id", "fakeid", "bookId", "biz", "__biz")
           if record.get(key)}
    for key in ("url", "link"):
        url = html.unescape(str(record.get(key) or "")).split("#", 1)[0]
        if _allowed_url(url, article=True):
            ids.update(normalize_avatar_account_id(value)
                       for value in parse_qs(urlsplit(url).query).get("__biz", []))
    return ids - {""}


def extract_avatar_account_id(page: str) -> str:
    """读取文章页自身的 biz 变量，不从正文中的其他文章链接推断归属。"""
    ids = set()
    for value in re.findall(r'''\b(?:biz|__biz)["']?\s*(?:=|:)\s*(["'])(.*?)\1''', page):
        identity = re.sub(r"\\x([0-9a-fA-F]{2})|\\u([0-9a-fA-F]{4})",
                          lambda m: chr(int(m.group(1) or m.group(2), 16)), value[1])
        identity = normalize_avatar_account_id(html.unescape(identity))
        if re.fullmatch(r"MP_WXS_[0-9]+", identity):
            ids.add(identity)
    return ids.pop() if len(ids) == 1 else ""


def _allowed_url(url: str, article: bool = False) -> bool:
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower()
        if (parsed.scheme not in ("http", "https") or parsed.username is not None
                or parsed.password is not None or parsed.fragment
                or parsed.port not in (None, 80 if parsed.scheme == "http" else 443)):
            return False
        if article:
            return host == "mp.weixin.qq.com" and (
                parsed.path == "/s" or parsed.path.startswith("/s/"))
        return (
            host in {"qpic.cn", "qlogo.cn", "pic.weread.qq.com",
                     "wfqqreader-1252317822.image.myqcloud.com"}
            or host.endswith((".qpic.cn", ".qlogo.cn"))
        )
    except (ValueError, TypeError):
        return False


def _check_public_host(url: str):
    parsed = urlsplit(url)
    addresses = socket.getaddrinfo(parsed.hostname, parsed.port or (
        443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(row[4][0]).is_global for row in addresses):
        raise ValueError("头像来源必须解析到公网地址")


def _fetch(url: str, *, article: bool = False) -> tuple[bytes, str]:
    """逐跳校验来源，流式读取，限制解压后的大小和总耗时。"""
    started = time.monotonic()
    limit = MAX_PAGE_BYTES if article else MAX_AVATAR_BYTES
    with requests.Session() as session:
        # 不携带账号凭证，也不使用环境中的代理或 netrc 凭证。
        session.trust_env = False
        for _ in range(4):
            if time.monotonic() - started > 15:
                raise ValueError("头像响应超时")
            if not _allowed_url(url, article):
                raise ValueError("不支持的头像来源")
            _check_public_host(url)
            with session.get(url, stream=True, allow_redirects=False, timeout=(3, 5),
                             headers={"User-Agent": get_default_wechat_ua(),
                                      "Referer": "https://mp.weixin.qq.com/"}) as response:
                if response.status_code in (301, 302, 303, 307, 308):
                    location = response.headers.get("Location")
                    if not location:
                        raise ValueError("缺少重定向地址")
                    url = urljoin(url, location)
                    continue
                if response.status_code != 200:
                    raise ValueError("头像来源响应失败")
                mime = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
                if mime not in ({"text/html", "application/xhtml+xml"} if article else IMAGE_TYPES):
                    raise ValueError("不支持的头像响应类型")
                length = response.headers.get("Content-Length")
                if length and (int(length) < 0 or int(length) > limit):
                    raise ValueError("头像响应过大")
                data = bytearray()
                for chunk in response.iter_content(32 * 1024):
                    if time.monotonic() - started > 15:
                        raise ValueError("头像响应超时")
                    data.extend(chunk)
                    if len(data) > limit:
                        raise ValueError("头像响应过大")
                return bytes(data), mime
    raise ValueError("头像重定向次数过多")


def _matches_image(data: bytes, mime: str) -> bool:
    return {
        "image/jpeg": data.startswith(b"\xff\xd8\xff"),
        "image/png": data.startswith(b"\x89PNG\r\n\x1a\n"),
        "image/gif": data.startswith((b"GIF87a", b"GIF89a")),
        "image/webp": data.startswith(b"RIFF") and data[8:12] == b"WEBP",
    }.get(mime, False)


def _can_retry(key: str) -> bool:
    with _state_lock:
        return time.monotonic() - _failures.get(key, float("-inf")) >= RETRY_SECONDS


def _record_failure(key: str):
    with _state_lock:
        if len(_failures) >= 1024:
            _failures.clear()
        _failures[key] = time.monotonic()


def cache_avatar(url: str) -> str:
    """返回缓存文件名；不支持的来源或下载失败时返回空串，由 RSS 回退。"""
    if not _allowed_url(url):
        return ""
    key = hashlib.sha256(url.encode("utf-8")).hexdigest()
    with _locks[int(key[:2], 16) % len(_locks)]:
        fallback = ""
        temporary = None
        try:
            cached = []
            for suffix in IMAGE_TYPES.values():
                target = AVATAR_DIR / (key + suffix)
                if target.is_file() and not target.is_symlink():
                    cached.append(target)
            existing = max(cached, key=lambda p: p.stat().st_mtime_ns) if cached else None
            fallback = existing.name if existing else ""
            if existing and time.time() - existing.stat().st_mtime < REFRESH_SECONDS:
                return fallback
            if not _can_retry(key):
                return fallback
            data, mime = _fetch(url)
            if not _matches_image(data, mime):
                raise ValueError("头像文件内容与类型不符")
            AVATAR_DIR.mkdir(parents=True, exist_ok=True)
            target = AVATAR_DIR / (key + IMAGE_TYPES[mime])
            with tempfile.NamedTemporaryFile(dir=AVATAR_DIR, delete=False) as output:
                temporary = output.name
                output.write(data)
            os.replace(temporary, target)
            return target.name
        except (OSError, ValueError, requests.RequestException):
            _record_failure(key)
            return fallback
        finally:
            if temporary:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass


def avatar_response(filename: str):
    """只提供已存在的图片，匿名可读，不接受远程 URL 或触发下载。"""
    if not re.fullmatch(r"[0-9a-f]{64}\.(?:jpg|png|gif|webp)", filename):
        abort(404)
    target = AVATAR_DIR / filename
    if target.is_symlink() or not target.is_file():
        abort(404)
    response = send_from_directory(AVATAR_DIR, filename, max_age=86400, conditional=True)
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


class _ProfileAvatarParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.avatar = ""

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = set((attrs.get("class") or "").split())
        if tag == "img" and (classes & {"wx_follow_avatar_pic", "profile_avatar"}
                             or attrs.get("id") == "js_profile_avatar"):
            self.avatar = attrs.get("data-src") or attrs.get("src") or self.avatar


def extract_account_avatar(page: str) -> str:
    """仅提取公众号头像，不能用 og:image（文章封面）替代。"""
    match = re.search(r'''\bround_head_img["']?\s*(?:=|:)\s*(["'])(.*?)\1''', page)
    if match:
        avatar = match.group(2)
    else:
        parser = _ProfileAvatarParser()
        parser.feed(page)
        avatar = parser.avatar
    avatar = re.sub(r"\\x([0-9a-fA-F]{2})|\\u([0-9a-fA-F]{4})",
                    lambda m: chr(int(m.group(1) or m.group(2), 16)), avatar)
    avatar = html.unescape(avatar.replace(r"\/", "/")).strip()
    if avatar.startswith("//"):
        avatar = "https:" + avatar
    return avatar if _allowed_url(avatar) else ""


def article_avatar(url: str, account_id: str) -> str:
    """仅从归属匹配的文章页补全头像；失败十分钟内不重试。"""
    account_id = normalize_avatar_account_id(account_id)
    link_ids = avatar_account_ids({"url": url})
    if (not account_id or not _allowed_url(url, article=True)
            or (link_ids and link_ids != {account_id})):
        return ""
    key = "article:" + hashlib.sha256((account_id + "\0" + url).encode("utf-8")).hexdigest()
    with _locks[int(key[-2:], 16) % len(_locks)]:
        with _state_lock:
            cached = _article_avatars.get(key)
        if cached:
            return cached
        if not _can_retry(key):
            return ""
        try:
            data, _ = _fetch(url, article=True)
            page = data.decode("utf-8", errors="replace")
            avatar = extract_account_avatar(page) if extract_avatar_account_id(page) == account_id else ""
            if avatar:
                with _state_lock:
                    if len(_article_avatars) >= 512:
                        _article_avatars.clear()
                    _article_avatars[key] = avatar
                return avatar
        except (OSError, ValueError, requests.RequestException):
            pass
        _record_failure(key)
        return ""
