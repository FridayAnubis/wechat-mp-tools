"""历史扫描的章节分页：不合并 cover，不降级到最新文章，不轮换账号重试。"""

from dataclasses import dataclass, field

import requests

from backend.account_pool import account_pool, borrow_weread_session, get_weread_session
from backend.accounts import biz_to_book_id
from backend.config import normalize_wechat_url
from backend.waterwall import detect_waterwall, is_waterwall_active, waterwall_hit


@dataclass
class HistoryPage:
    # end 仅表示本次接口明确返回末页，不代表公众号的全部历史可见。
    state: str
    offset: int
    next_offset: int
    articles: list = field(default_factory=list)
    raw_count: int = 0
    has_more: bool | None = None
    http_status: int | None = None
    error_code: int | None = None
    reason: str = ""


def parse_history_page(response, offset: int, count: int) -> HistoryPage:
    """仅可信非空章节页可推进游标；错误消息不保留上游响应正文。"""
    result = HistoryPage("temporary_error", offset, offset,
                         http_status=response.status_code)
    marker = detect_waterwall(response.status_code, response.text)
    if marker:
        result.state, result.reason = "blocked", "captcha"
        return result
    try:
        data = response.json()
    except ValueError:
        data = None
    if isinstance(data, dict):
        code = data.get("errcode", data.get("errCode", 0))
        try:
            result.error_code = int(code or 0)
        except (ValueError, TypeError):
            result.reason = "invalid_error_code"
            return result

    if response.status_code == 401 or result.error_code == -2012:
        result.state, result.reason = "auth_error", "auth_expired"
    elif result.error_code == -2041:
        result.state, result.reason = "blocked", "session_inactive"
    elif response.status_code in (403, 429, 499) or result.error_code in (200013, -2013):
        result.state, result.reason = "blocked", "upstream_restricted"
    elif response.status_code != 200:
        result.reason = "http_error"
    elif not isinstance(data, dict) or result.error_code:
        result.reason = "invalid_response"
    else:
        items = next((data[key] for key in ("data", "chapters", "list") if key in data), None)
        if not isinstance(items, list) or not items:
            result.reason = "empty_or_invalid_page"
            return result
        has_more = data.get("hasMore")
        if has_more is not None and (type(has_more) not in (bool, int) or has_more not in (0, 1)):
            result.reason = "invalid_has_more"
            return result
        seen = set()
        articles = []
        for item in items:
            if not isinstance(item, dict) or not isinstance(item.get("mpInfo", {}), dict):
                result.reason = "invalid_chapter"
                return result
            info = item.get("mpInfo", {})
            rid = item.get("reviewId") or ""
            link = info.get("doc_url") or ""
            if not isinstance(rid, str) or not isinstance(link, str):
                result.reason = "invalid_article_identity"
                return result
            if not link and "_" in rid and len(rid.rsplit("_", 1)[-1]) >= 15:
                link = "https://mp.weixin.qq.com/s/" + rid.rsplit("_", 1)[-1]
            key = "review:" + rid if rid else "url:" + normalize_wechat_url(link)
            if not rid and not link:
                result.reason = "missing_article_identity"
                return result
            if key in seen:
                continue
            seen.add(key)
            articles.append({
                "id": rid, "link": link,
                "title": info.get("title") or item.get("title") or "",
                "cover": info.get("pic_url") or info.get("thumbUrl") or item.get("cover") or "",
                "digest": info.get("digest", ""), "author": info.get("author", ""),
                "update_time": item.get("createTime") or info.get("time") or 0,
            })
        result.articles = articles
        result.raw_count = len(items)
        result.next_offset = offset + len(items)
        # 缺失 hasMore 时保留未知；不能从不足 count 条推断末页。
        result.has_more = bool(has_more) if has_more is not None else None
        result.state = "end" if result.has_more is False else "ok"
    return result


def fetch_history_page(fakeid: str, offset: int, count: int = 10) -> HistoryPage:
    """获取一页原始章节；调用方负责请求间隔和任务调度。"""
    if offset < 0 or not 5 <= count <= 30:
        raise ValueError("offset 必须非负，count 必须介于 5–30")
    book_id = biz_to_book_id(fakeid)
    if not book_id:
        raise ValueError("公众号标识不能为空")
    if is_waterwall_active():
        return HistoryPage("blocked", offset, offset, reason="captcha")
    try:
        account_id, _, _, headers = borrow_weread_session(strict=True)
    except RuntimeError:
        return HistoryPage("blocked", offset, offset, reason="no_available_account")
    url = "https://i.weread.qq.com/mp/chapters"
    try:
        response = get_weread_session().get(
            url, params={"bookId": book_id, "offset": offset, "count": count, "pf": "android"},
            headers=headers, timeout=10,
        )
    except requests.RequestException:
        return HistoryPage("temporary_error", offset, offset, reason="network_error")
    result = parse_history_page(response, offset, count)
    if result.reason == "captcha":
        waterwall_hit(url, response.status_code, "captcha")
    if result.state in ("ok", "end"):
        account_pool.report(account_id, is_success=True)
    elif result.state in ("blocked", "auth_error"):
        # 避免 report 的旧行为触发续期 / mobileSync 等额外请求。
        account_pool.report(account_id, status_code=response.status_code,
                            ret=result.error_code, error_msg=result.reason, allow_refresh=False)
    return result
