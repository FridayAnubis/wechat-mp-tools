"""RSS / Feed 对外接口（优化版）

相对上游 apis/rss.py 的改动（详见 rss-opt/README.md）：
1. /feed/{id}.{ext} 等读接口默认走缓存（上游默认 is_update=True，每次轮询都打库重建）
2. 缓存带 TTL（rss.cache_ttl，默认 300s），配合采集后的 clear_cache 双保险
3. 输出 ETag / Last-Modified / Cache-Control，支持 304，阅读器轮询成本降到几乎为 0
4. 生成失败时回退旧缓存（stale-if-error），没有缓存才 503，不再抛 UnboundLocalError
5. 修 rss.base_url 末尾斜杠导致的 //views/article 双斜杠
6. 去掉每次请求都执行但从未使用的 COUNT(*)
7. 正文缓存改为「缺失或为空才写」，不再每次全量重写
8. 新增可选访问令牌（rss.access_token），空值保持原行为不变
"""

import json
import secrets
from datetime import datetime, timedelta, timezone
from email.utils import formatdate

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status

from apis.base import format_search_kw
from core.config import cfg
from core.db import DB
from core.models.feed import Feed
from core.print import print_error
from core.rss import RSS

from .base import error_response

CST = timezone(timedelta(hours=8))


# ---------------------------------------------------------------- 基础工具


def _cache_ttl() -> int:
    try:
        return int(cfg.get("rss.cache_ttl", 300) or 0)
    except (TypeError, ValueError):
        return 300


def _rss_base_url(request: Request) -> str:
    base = str(cfg.get("rss.base_url") or "").strip()
    if not base:
        base = str(request.base_url)
    return base.rstrip("/")


def _abs_url(base: str, path: str) -> str:
    path = (path or "").strip()
    if not path:
        return ""
    if path.startswith(("http://", "https://")):
        return path
    if path.startswith("/"):
        return base + path
    return "%s/%s" % (base, path)


def clamp_rss_limit(limit: int) -> int:
    """按配置收敛单次返回条数，避免超长 feed 拖慢客户端。"""
    default_page_size = int(cfg.get("rss.page_size", 30) or 30)
    max_items = int(cfg.get("rss.max_items", default_page_size) or default_page_size)
    if max_items < 1:
        max_items = default_page_size if default_page_size > 0 else 30
    return max(1, min(limit, max_items))


def verify_rss_access(request: Request, token: str = Query(None, include_in_schema=False)):
    """可选令牌校验：rss.access_token 为空时不启用，行为与上游一致。"""
    expected = str(cfg.get("rss.access_token", "") or "").strip()
    if not expected:
        return True

    supplied = (token or "").strip()
    if not supplied:
        auth = request.headers.get("Authorization", "")
        if auth.lower().startswith("bearer "):
            supplied = auth[7:].strip()

    if not supplied or not secrets.compare_digest(supplied, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=error_response(code=40101, message="未授权的RSS访问"),
            headers={"WWW-Authenticate": "Bearer"},
        )
    return True


def _rss_response(request: Request, rss: RSS, body: str, media_type: str, max_age: int):
    """统一出口：Cache-Control + ETag + Last-Modified + 304。"""
    headers = {}
    if max_age and max_age > 0:
        headers["Cache-Control"] = "public, max-age=%d" % int(max_age)
    else:
        headers["Cache-Control"] = "no-cache"

    mtime = rss.cache_mtime()
    etag = None
    if mtime:
        etag = 'W/"%x-%x"' % (int(mtime), len(body or ""))
        headers["ETag"] = etag
        headers["Last-Modified"] = formatdate(mtime, usegmt=True)
        if request.headers.get("if-none-match") == etag:
            return Response(status_code=status.HTTP_304_NOT_MODIFIED, headers=headers)

    return Response(content=body, media_type=media_type, headers=headers)


def _serve_stale(request: Request, rss: RSS):
    stale = rss.get_cache()
    if stale is None:
        return None
    return _rss_response(request, rss, stale, rss.get_type(), 60)


# ---------------------------------------------------------------- 路由


router = APIRouter(prefix="/rss", tags=["Rss"], dependencies=[Depends(verify_rss_access)])
feed_router = APIRouter(prefix="/feed", tags=["Feed"], dependencies=[Depends(verify_rss_access)])


@router.get("/{feed_id}/api", summary="获取特定RSS源详情")
async def get_rss_source(
    feed_id: str,
    request: Request,
    limit: int = Query(100, ge=1, le=100),
    offset: int = Query(0, ge=0),
):
    return await get_mp_articles_source(
        request=request, feed_id=feed_id, limit=limit, offset=offset, is_update=False
    )


@router.get("/fresh", summary="更新并获取RSS订阅列表")
async def update_rss_feeds(
    request: Request,
    limit: int = Query(100, ge=1, le=100),
    offset: int = Query(0, ge=0),
):
    return await get_rss_feeds(request=request, limit=limit, offset=offset, is_update=True)


@router.get("", summary="获取RSS订阅列表")
async def get_rss_feeds(
    request: Request,
    limit: int = Query(10, ge=1, le=100),
    offset: int = Query(0, ge=0),
    is_update: bool = False,
):
    limit = clamp_rss_limit(limit)
    ttl = _cache_ttl()
    rss = RSS(name="all_%s_%s" % (limit, offset))

    if not is_update:
        cached = rss.get_cache(ttl)
        if cached is not None:
            return _rss_response(request, rss, cached, rss.get_type(), ttl)

    session = DB.get_session()
    try:
        feeds = (
            session.query(Feed)
            .order_by(Feed.created_at.desc())
            .limit(limit)
            .offset(offset)
            .all()
        )
        base = _rss_base_url(request)
        rss_list = [
            {
                "id": str(feed.id),
                "title": feed.mp_name,
                "link": "%s/rss/%s" % (base, feed.id),
                "description": feed.mp_intro or "",
                "image": feed.mp_cover or "",
                "updated": (
                    feed.created_at
                    if getattr(feed.created_at, "tzinfo", None) is not None
                    else feed.created_at.replace(tzinfo=CST)
                ).isoformat(),
            }
            for feed in feeds
        ]
        xml = rss.generate_rss(rss_list, title="WeRSS订阅", link=base)
        return _rss_response(request, rss, xml, rss.get_type(), ttl)
    except Exception as e:
        print_error("获取RSS订阅列表错误: %s" % e)
        fallback = _serve_stale(request, rss)
        if fallback is not None:
            return fallback
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=error_response(code=50001, message="获取RSS订阅列表失败"),
        )


@router.get("/content/{content_id}", summary="获取缓存的文章内容")
async def get_rss_feed(content_id: str):
    rss = RSS()
    content = rss.get_cached_content(content_id)
    if content is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=error_response(code=40402, message="文章内容未找到"),
        )

    html = """<!DOCTYPE html>
    <html lang="zh-CN">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>{title}</title>
    </head>
    <body>
    <center>
    <h1 style="text-align:center;">{title}</h1>
    <div class="author">来源:{source}</div>
    <div class="author">发布时间:{publish_time}</div>
    <div class="copyright">
        <p>本文章仅用于学习和交流目的，不代表本网站观点和立场，如涉及版权问题，请及时联系我们删除。</p>
    </div>
    <div id=content>{text}</div>
    </center>
    </body>
    </html>
    """
    text = rss.add_logo_prefix_to_urls(content.get("content") or "")
    html = html.format(
        title=content.get("title") or "",
        text=text,
        source=content.get("mp_name") or "",
        publish_time=content.get("publish_time") or "",
    )
    return Response(
        content=html,
        media_type="text/html; charset=utf-8",
        headers={"Cache-Control": "public, max-age=3600"},
    )


def UpdateArticle(art: dict):
    return DB.add_article(art)


@router.api_route("/{feed_id}/fresh", methods=["GET"], summary="更新并获取公众号文章RSS")
async def update_rss_feeds_by_feed(
    request: Request,
    feed_id: str,
    limit: int = Query(100, ge=1, le=100),
    offset: int = Query(0, ge=0),
):
    return await get_mp_articles_source(
        request=request, feed_id=feed_id, limit=limit, offset=offset, is_update=True
    )


@router.get("/{feed_id}", summary="获取公众号文章")
async def get_mp_articles_source(
    request: Request,
    feed_id: str = None,
    tag_id: str = None,
    ext: str = "xml",
    limit: int = Query(10, ge=1, le=100),
    offset: int = Query(0, ge=0),
    kw: str = "",
    is_update: bool = False,
    content_type: str = Query(None, alias="ctype"),
    template: str = None,
):
    limit = clamp_rss_limit(limit)
    ttl = _cache_ttl()
    rss = RSS(name="%s_%s_%s_%s" % (tag_id, feed_id, limit, offset), ext=ext)
    rss.set_content_type(content_type)

    if not is_update:
        cached = rss.get_cache(ttl)
        if cached is not None:
            return _rss_response(request, rss, cached, rss.get_type(), ttl)

    session = DB.get_session()
    try:
        from core.models.article import Article
        from core.models.tags import Tags

        base = _rss_base_url(request)
        query = session.query(Feed, Article).join(Article, Feed.id == Article.mp_id)

        if tag_id is not None:
            feed_link = "%s/feed/tag/%s.%s" % (base, tag_id, ext)
        elif kw != "":
            feed_link = "%s/feed/search/%s/%s.%s" % (base, kw, feed_id or "all", ext)
        else:
            feed_link = "%s/feed/%s.%s" % (base, feed_id or "all", ext)

        if feed_id not in ["all", None]:
            feed = session.query(Feed).filter(Feed.id == feed_id).first()
            query = query.filter(Article.mp_id == feed_id)
        else:
            feed = Feed()
            feed.id = "all"
            feed.mp_name = cfg.get("rss.title", "WeRss") or "WeRss"
            feed.mp_intro = cfg.get("rss.description") or "WeRss高效订阅我的公众号"
            feed.mp_cover = cfg.get("rss.cover") or _abs_url(base, "/static/logo.svg")
            if tag_id is not None:
                tags = session.query(Tags).filter(Tags.id == tag_id).first()
                if tags:
                    mps_ids = [str(mp["id"]) for mp in json.loads(tags.mps_id)] if tags.mps_id else []
                    query = query.filter(Feed.id.in_(mps_ids))
                    feed.mp_name = tags.name
                    feed.mp_intro = tags.intro
                    feed.mp_cover = _abs_url(base, tags.cover)

        if not feed:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=error_response(code=40401, message="公众号不存在"),
            )

        if kw != "":
            query = query.filter(format_search_kw(kw))

        articles = (
            query.order_by(Article.publish_time.desc())
            .limit(limit)
            .offset(offset)
            .all()
        )

        rss_list = []
        for _feed, article in articles:
            published = article.publish_time or article.create_time or 0
            rss_list.append({
                "id": str(article.id),
                "title": article.title or "",
                "link": (
                    _abs_url(base, "/views/article/%s" % article.id)
                    if cfg.get("rss.local", False)
                    else (article.url or "")
                ),
                "description": article.description if article.description else (article.title or ""),
                "content": article.content or "",
                "image": article.pic_url or "",
                "mp_name": _feed.mp_name or "",
                "updated": datetime.fromtimestamp(int(published), tz=CST),
                "feed": {
                    "id": _feed.id,
                    "name": _feed.mp_name,
                    "cover": _feed.mp_cover,
                    "intro": _feed.mp_intro,
                },
            })

            rss.cache_content_if_needed(article.id, {
                "id": article.id,
                "title": article.title,
                "content": article.content,
                "publish_time": article.publish_time,
                "mp_id": article.mp_id,
                "pic_url": article.pic_url,
                "mp_name": _feed.mp_name,
            })

        xml = rss.generate(
            rss_list,
            ext=ext,
            title=feed.mp_name,
            link=feed_link,
            description=feed.mp_intro,
            image_url=feed.mp_cover,
            template=template,
        )
        return _rss_response(request, rss, xml, rss.get_type(), ttl)

    except HTTPException:
        raise
    except Exception as e:
        print_error("获取RSS错误: %s" % e)
        fallback = _serve_stale(request, rss)
        if fallback is not None:
            return fallback
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=error_response(code=50002, message="RSS 生成失败，请稍后重试"),
        )


@feed_router.get("/{feed_id}.{ext}", summary="获取公众号文章源")
async def rss_feed(
    request: Request,
    feed_id: str,
    ext: str,
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    kw: str = "",
    content_type: str = Query(None, alias="ctype"),
    is_update: bool = False,
):
    return await get_mp_articles_source(
        request=request, feed_id=feed_id, limit=limit, offset=offset,
        is_update=is_update, ext=ext, kw=kw, content_type=content_type,
    )


@feed_router.get("/search/{kw}/{feed_id}.{ext}", summary="搜索公众号文章源")
async def rss_search(
    request: Request,
    feed_id: str,
    ext: str,
    kw: str = "",
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    content_type: str = Query(None, alias="ctype"),
    is_update: bool = False,
):
    return await get_mp_articles_source(
        request=request, feed_id=feed_id, limit=limit, offset=offset,
        is_update=is_update, ext=ext, kw=kw, content_type=content_type,
    )


@feed_router.get("/tag/{tag_id}.{ext}", summary="获取标签聚合文章源")
async def rss_tag(
    request: Request,
    tag_id: str = "",
    feed_id: str = None,
    ext: str = "jmd",
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    kw: str = "",
    content_type: str = Query(None, alias="ctype"),
    is_update: bool = False,
):
    return await get_mp_articles_source(
        request=request, feed_id=feed_id, tag_id=tag_id, limit=limit,
        offset=offset, is_update=is_update, ext=ext, kw=kw, content_type=content_type,
    )
