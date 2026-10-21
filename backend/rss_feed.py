"""RSS 2.0 订阅输出

替代 backend/articles.py 里原来的 get_rss 内联实现。改动动机与效果见
deploy/rss/README.md，核心是四点：

1. rss_articles.json 每次请求只读一次并建索引。原实现按订阅数循环调用
   rss_scheduler.get_articles()，每个账号都把整个文件重新解析一遍（N 次全量
   JSON 解析），订阅越多越慢。
2. 单次输出有上限（limit / offset），按发布时间倒序。原实现把全部历史 + 全部
   订阅文章一次性塞进一个 feed，账号多了会生成几 MB 的 XML。
3. 进程内结果缓存 + ETag + 304。原实现每次轮询都要重新读文件、重新读
   content.txt、重新拼字符串。
4. 反向代理下用 X-Forwarded-* 或显式 rss_base_url 生成链接。原实现用
   request.host_url，容器/反代后面会生成 http://内网地址 的订阅链接。
"""

from __future__ import annotations

import hashlib
import hmac
import html
import os
import re
import threading
import time
from email.utils import formatdate
from pathlib import Path
from urllib.parse import quote

from flask import Response

from backend.config import (
    DATA_DIR,
    DOWNLOAD_HISTORY_FILE,
    get_settings,
    load_json,
    normalize_wechat_url,
)

RSS_ARTICLES_FILE = DATA_DIR / "rss_articles.json"

DEFAULT_LIMIT = 50
MAX_LIMIT = 500
DEFAULT_TTL = 300
MAX_CONTENT_CHARS = 300_000
EXCLUDED_ACCOUNTS = {"微信视频号"}

_cache: dict = {}
_cache_lock = threading.Lock()


# ---------------------------------------------------------------- 数据加载


def _file_mtimes() -> tuple:
    """两个数据文件的修改时间，作为缓存失效依据。"""
    out = []
    for path in (DOWNLOAD_HISTORY_FILE, RSS_ARTICLES_FILE):
        try:
            out.append(path.stat().st_mtime_ns)
        except OSError:
            out.append(0)
    return tuple(out)


def _load_all_articles() -> dict:
    """一次性读出 {公众号: [文章...]}，不按账号重复解析。"""
    data = load_json(RSS_ARTICLES_FILE, {})
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if isinstance(v, list)}


def _load_history() -> list:
    data = load_json(DOWNLOAD_HISTORY_FILE, [])
    return data if isinstance(data, list) else []


def _to_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _cfg(settings: dict, key: str, env: str, default):
    """环境变量优先，其次设置文件，最后默认值。

    环境变量只在部署时显式设置（Docker 的 .env），所以它代表部署级配置；
    桌面版不会设这些变量，自然走 app_settings.json。
    """
    env_val = os.environ.get(env)
    if env_val is not None and str(env_val).strip() != "":
        return env_val

    value = settings.get(key)
    if value not in (None, ""):
        return value
    return default


def _cfg_int(settings: dict, key: str, env: str, default: int) -> int:
    return int(_to_float(_cfg(settings, key, env, default), default))


def _item_key(link: str, account: str, title: str) -> str:
    normalized = normalize_wechat_url(link) if link else ""
    if normalized:
        return normalized
    return f"{account}\x00{title}"


def _normalize_item(raw: dict, account: str) -> dict | None:
    """把下载历史条目或 RSS 缓存条目统一成内部结构。"""
    if not isinstance(raw, dict):
        return None
    link = raw.get("link") or raw.get("url") or ""
    title = raw.get("title") or ""
    if not link and not title:
        return None
    published = (
        raw.get("publish_time")
        or raw.get("update_time")
        or raw.get("time")
        or 0
    )
    return {
        "title": str(title),
        "link": str(link),
        "account": str(raw.get("account") or raw.get("author") or account or ""),
        "published": _to_float(published),
        "digest": str(raw.get("digest") or ""),
        "cover": str(raw.get("cover_url") or raw.get("cover") or ""),
        "path": str(raw.get("path") or ""),
    }


def collect_items(account: str | None = None) -> list:
    """合并「手动下载历史」与「订阅抓取缓存」，按链接去重、按时间倒序。"""
    articles = _load_all_articles()
    history = _load_history()

    merged: dict = {}
    order: list = []

    def push(raw, acc):
        item = _normalize_item(raw, acc)
        if not item:
            return
        if item["account"] in EXCLUDED_ACCOUNTS:
            return
        if account and item["account"] != account:
            return
        key = _item_key(item["link"], item["account"], item["title"])
        if key in merged:
            # 同一篇：补全缺失字段，保留更早拿到的那条
            exist = merged[key]
            for field in ("digest", "cover", "path"):
                if not exist.get(field) and item.get(field):
                    exist[field] = item[field]
            if not exist.get("published") and item.get("published"):
                exist["published"] = item["published"]
            return
        merged[key] = item
        order.append(key)

    if account:
        for raw in articles.get(account, []):
            push(raw, account)
        for raw in history:
            if isinstance(raw, dict) and raw.get("success"):
                push(raw, raw.get("account") or account)
    else:
        for acc, arts in articles.items():
            for raw in arts:
                push(raw, acc)
        for raw in history:
            if isinstance(raw, dict) and raw.get("success"):
                push(raw, raw.get("account") or "")

    items = [merged[k] for k in order]
    items.sort(key=lambda x: x["published"], reverse=True)
    return items


# ---------------------------------------------------------------- XML 渲染


def _text(value: str) -> str:
    return html.escape(str(value or ""), quote=False)


def _attr(value: str) -> str:
    return html.escape(str(value or ""), quote=True)


def _cdata(value: str) -> str:
    # CDATA 内出现 ]]> 必须拆成 ]]]]><![CDATA[>
    return "<![CDATA[%s]]>" % str(value or "").replace("]]>", "]]]]><![CDATA[>")


_RSS_STRIP_BLOCKS = re.compile(r"<(script|style|iframe|noscript)\b[^>]*>.*?</\1\s*>", re.IGNORECASE | re.DOTALL)
_RSS_EVENT_ATTRS = re.compile(r"""\s+on[a-z]+\s*=\s*(?:"[^"]*"|'[^']*'|[^\s>]+)""", re.IGNORECASE)
_RSS_RELATIVE_URL = re.compile(r"""(\s(?:src|href|poster)\s*=\s*)(["'])(?!(?:[a-z][a-z0-9+.-]*:|//|#))([^"']+)\2""", re.IGNORECASE)
_RSS_RELATIVE_CSS_URL = re.compile(r"""(url\(\s*(?:&quot;|["'])?)(media/[^)"'&]+)""", re.IGNORECASE)


def _find_article_html(art_dir: Path) -> Path | None:
    """定位文章目录中实际保存的离线 HTML"""
    if not art_dir.is_dir():
        return None
    candidates = sorted(f for f in art_dir.glob("*.html") if not f.name.endswith(("_raw.html", ".bak")))
    for c in candidates:
        if c.stem == art_dir.name:
            return c
    return candidates[0] if candidates else None


def _rss_article_body(html_text: str, base_url: str) -> str:
    """提取离线 HTML 的 #js_content 正文，保留富文本格式与图片"""
    start = re.search(r'<div id="js_content"[^>]*>', html_text)
    if start:
        end = html_text.rfind("</div>")
        body = html_text[start.end():end if end > start.end() else None]
    else:
        m = re.search(r"<body[^>]*>(.*)</body>", html_text, re.IGNORECASE | re.DOTALL)
        body = m.group(1) if m else html_text
    body = _RSS_STRIP_BLOCKS.sub("", body)
    body = _RSS_EVENT_ATTRS.sub("", body)
    body = _RSS_RELATIVE_URL.sub(
        lambda m: f"{m.group(1)}{m.group(2)}{base_url}{quote(m.group(3), safe='/%?=&;')}{m.group(2)}",
        body,
    )
    body = _RSS_RELATIVE_CSS_URL.sub(lambda m: f"{m.group(1)}{base_url}{quote(m.group(2))}", body)
    return body.strip()


def _read_content(item: dict, base_url: str) -> str:
    path = item.get("path") or ""
    if not path:
        return ""
    try:
        p = Path(path)
        if not p.exists():
            return ""

        # 优先读取离线 HTML，保留完整富文本与图片排版
        html_file = _find_article_html(p)
        if html_file:
            parent, name = p.parent.name, p.name
            art_base_url = "%sapi/articles/serve-file/%s/%s/" % (
                base_url,
                quote(parent, safe=""),
                quote(name, safe=""),
            )
            body = _rss_article_body(html_file.read_text(encoding="utf-8", errors="replace"), art_base_url)
            if body:
                if len(body) > MAX_CONTENT_CHARS:
                    body = body[:MAX_CONTENT_CHARS]
                return body

        # 回落方案：读取 content.txt 并用段落包裹
        txt = p / "content.txt"
        if txt.is_file():
            text = txt.read_text(encoding="utf-8", errors="replace")
            lines = [f"<p>{html.escape(ln)}</p>" for ln in text.splitlines() if ln.strip()]
            body = "\n".join(lines)
            if len(body) > MAX_CONTENT_CHARS:
                body = body[:MAX_CONTENT_CHARS]
            return body
    except OSError:
        return ""
    return ""


def _local_link(base_url: str, item: dict) -> str:
    path = item.get("path") or ""
    if not path:
        return ""
    try:
        p = Path(path)
        if not p.exists():
            return ""
        html_file = _find_article_html(p)
        parent, name = p.parent.name, p.name
        file_name = html_file.name if html_file else f"{name}.html"
    except OSError:
        return ""
    return "%sapi/articles/serve-file/%s/%s/%s" % (
        base_url,
        quote(parent, safe=""),
        quote(name, safe=""),
        quote(file_name, safe=""),
    )


def render_feed(items: list, title: str, description: str,
                base_url: str, fulltext: bool = True) -> str:
    xml_items = []
    for item in items:
        # 本地有落盘文件就走本地预览链接，否则回落到公众号原始链接
        xml_link = _local_link(base_url, item) or item["link"]
        if not xml_link:
            continue

        pub_ts = item["published"] or time.time()
        parts = [
            "    <item>",
            "      <title>%s</title>" % _text(item["title"]),
            "      <link>%s</link>" % _text(xml_link),
            "      <guid isPermaLink=\"false\">%s</guid>" % _text(item["link"] or xml_link),
            "      <pubDate>%s</pubDate>" % formatdate(pub_ts, usegmt=True),
            "      <description>%s</description>" % _text(item["digest"]),
        ]
        if item["cover"]:
            parts.append(
                '      <enclosure url="%s" type="image/jpeg" length="0"/>'
                % _attr(item["cover"])
            )
        if fulltext:
            content = _read_content(item, base_url)
            if content:
                parts.append(
                    "      <content:encoded>%s</content:encoded>" % _cdata(content)
                )
        parts.append("    </item>")
        xml_items.append("\n".join(parts))

    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">\n'
        "  <channel>\n"
        "    <title>%s</title>\n"
        "    <link>%s</link>\n"
        "    <description>%s</description>\n"
        "    <language>zh-CN</language>\n"
        "    <generator>wechat-mp-tools</generator>\n"
        "    <lastBuildDate>%s</lastBuildDate>\n"
        "%s\n"
        "  </channel>\n"
        "</rss>"
    ) % (
        _text(title),
        _text(base_url),
        _text(description),
        formatdate(time.time(), usegmt=True),
        "\n".join(xml_items),
    )


# ---------------------------------------------------------------- 对外入口


def _resolve_base_url(request, settings: dict) -> str:
    explicit = str(_cfg(settings, "rss_base_url", "RSS_BASE_URL", "") or "").strip()
    if explicit:
        return explicit.rstrip("/") + "/"
    proto = request.headers.get("X-Forwarded-Proto") or request.scheme or "http"
    host = request.headers.get("X-Forwarded-Host") or request.host
    return "%s://%s/" % (proto.split(",")[0].strip(), host)


def _check_token(request, settings: dict):
    expected = str(_cfg(settings, "rss_token", "RSS_TOKEN", "") or "").strip()
    if not expected:
        return None
    supplied = request.args.get("token") or ""
    if not supplied:
        auth = request.headers.get("Authorization", "")
        if auth.lower().startswith("bearer "):
            supplied = auth[7:].strip()
    if not supplied or not hmac.compare_digest(supplied, expected):
        return Response(
            "RSS access denied",
            status=401,
            mimetype="text/plain",
            headers={"WWW-Authenticate": 'Bearer realm="rss"'},
        )
    return None


def build_feed(account: str | None, limit: int, offset: int,
               fulltext: bool, base_url: str) -> tuple[str, str]:
    """返回 (xml, etag)，结果按 (参数 + 文件 mtime) 缓存在进程内。"""
    settings = get_settings()
    ttl = _cfg_int(settings, "rss_cache_ttl", "RSS_CACHE_TTL", DEFAULT_TTL)
    key = (account or "", limit, offset, bool(fulltext), base_url, _file_mtimes())

    with _cache_lock:
        hit = _cache.get(key)
        if hit and (ttl <= 0 or time.time() - hit[0] < ttl):
            return hit[1], hit[2]

    items = collect_items(account)
    # limit <= 0 表示不限制（桌面版默认），返回 offset 之后的全部
    window = items[offset:] if limit <= 0 else items[offset:offset + limit]

    if account:
        title = "微信公众号 - %s RSS" % account
        description = "%s 微信公众号文章订阅" % account
    else:
        title = "微信公众号 RSS"
        description = "微信公众号文章订阅"

    xml = render_feed(window, title, description, base_url, fulltext=fulltext)
    etag = 'W/"%s"' % hashlib.sha1(xml.encode("utf-8")).hexdigest()[:20]

    with _cache_lock:
        if len(_cache) > 64:
            _cache.clear()
        _cache[key] = (time.time(), xml, etag)
    return xml, etag


def rss_response(request, account: str | None = None) -> Response:
    settings = get_settings()

    denied = _check_token(request, settings)
    if denied is not None:
        return denied

    raw_limit = request.args.get("limit")
    if raw_limit is not None and str(raw_limit).strip() != "":
        try:
            limit = max(1, min(int(raw_limit), MAX_LIMIT))
        except (TypeError, ValueError):
            limit = DEFAULT_LIMIT
    else:
        # 未显式指定时看配置：0 表示不限制，保持桌面版原有行为
        configured = _cfg_int(settings, "rss_max_items", "RSS_MAX_ITEMS", 0)
        limit = max(1, min(configured, MAX_LIMIT)) if configured > 0 else 0

    try:
        offset = max(0, int(request.args.get("offset") or 0))
    except (TypeError, ValueError):
        offset = 0

    fulltext = str(request.args.get("full", "1")).lower() not in ("0", "false", "no")

    base_url = _resolve_base_url(request, settings)
    xml, etag = build_feed(account, limit, offset, fulltext, base_url)

    ttl = _cfg_int(settings, "rss_cache_ttl", "RSS_CACHE_TTL", DEFAULT_TTL)
    mtime_ns = max(_file_mtimes()) or 0
    headers = {
        "ETag": etag,
        "Cache-Control": ("public, max-age=%d" % ttl) if ttl > 0 else "no-cache",
        "Last-Modified": formatdate(mtime_ns / 1e9 if mtime_ns else time.time(), usegmt=True),
    }

    if request.headers.get("If-None-Match") == etag:
        return Response(status=304, headers=headers)

    return Response(xml, mimetype="application/xml", headers=headers)
