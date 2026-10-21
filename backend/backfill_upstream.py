"""
历史回溯专用的上游接口适配层（微信读书 mp/chapters）

与文章列表页使用的 articles._fetch_articles_page() 刻意区分：
  - 不合并 mp/cover 置顶推文 → 原始章节 offset 不会多算一条
  - 不降级到 article/list（它只返回最新文章，会把「上游受限」伪装成「历史已到底」）
  - 不在同一次请求里轮换账号重试、不并发解析链接 → 每次上游调用都经过请求协调器
  - 结果显式区分 ok / end / temporary_error / auth_error / blocked，绝不靠「空列表」推断终点

上游协议假设（未在真实账号上验证，见 docs/history_backfill.md「上游假设」）：
  - mp/chapters?bookId=&count=&offset= 按发布时间倒序返回章节，列表字段为 data / chapters / list 之一
  - hasMore 显式为 false / 0 的 HTTP 200 合法响应才视为「可信末页」
  - reviewId 形如 MP_WXS_<uin>_<短链 id>，短链 id 可直接拼出 https://mp.weixin.qq.com/s/<id>
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

STATUS_OK = "ok"
STATUS_END = "end"
STATUS_TEMPORARY = "temporary_error"
STATUS_AUTH = "auth_error"
STATUS_BLOCKED = "blocked"

CHAPTERS_URL = "https://i.weread.qq.com/mp/chapters"
REVIEW_URL = "https://i.weread.qq.com/review/single"

_RESTRICT_CODES = {-2013, -2014, 200013}


@dataclass
class ChapterPage:
    status: str
    offset: int
    count: int
    articles: list = field(default_factory=list)   # 按上游顺序；每项含 position（原始绝对偏移）
    raw_count: int = 0                             # 上游原始章节条目数（游标推进依据）
    has_more: bool | None = None                   # None = 上游未给出 / 无法解析
    http_status: int | None = None
    error_code: int | None = None
    reason: str = ""
    retry_after: float | None = None
    account_id: str = ""


def short_link_from_review_id(review_id: str) -> str:
    if review_id and "_" in review_id:
        sid = review_id.rsplit("_", 1)[-1]
        if len(sid) >= 15:
            return f"https://mp.weixin.qq.com/s/{sid}"
    return ""


def _parse_has_more(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str) and value.strip().lower() in ("0", "1", "true", "false"):
        return value.strip().lower() in ("1", "true")
    return None


def _int_or_none(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def parse_chapters_response(status_code: int, text: str, *, offset: int, count: int,
                            headers: dict | None = None) -> ChapterPage:
    """把一次 mp/chapters HTTP 响应解析为 ChapterPage（纯函数，便于离线测试）。"""
    from backend.waterwall import detect_waterwall

    page = ChapterPage(STATUS_TEMPORARY, offset, count, http_status=status_code)
    text = text or ""
    marker = detect_waterwall(status_code, text)
    if marker:
        page.status, page.reason = STATUS_BLOCKED, "captcha"
        return page

    data = None
    try:
        data = json.loads(text) if text else None
    except ValueError:
        data = None
    if isinstance(data, dict):
        page.error_code = _int_or_none(data.get("errcode", data.get("errCode")))
    code = page.error_code or 0

    if status_code == 401 or code in (401, -2012):
        page.status, page.reason = STATUS_AUTH, "auth_expired"
        return page
    if code == -2041 or (status_code == 499 and "2041" in text):
        page.status, page.reason = STATUS_BLOCKED, "session_inactive"
        return page
    if status_code in (403, 429, 499) or code in _RESTRICT_CODES:
        page.status, page.reason = STATUS_BLOCKED, "upstream_restricted"
        ra = (headers or {}).get("Retry-After") if headers else None
        try:
            page.retry_after = float(ra) if ra else None
        except (TypeError, ValueError):
            page.retry_after = None
        return page
    if status_code >= 500:
        page.reason = "server_error"
        return page
    if status_code != 200:
        page.reason = f"http_{status_code}"
        return page
    if not isinstance(data, dict):
        page.reason = "invalid_json"
        return page
    if code:
        page.reason = "upstream_errcode"
        return page

    items = None
    for key in ("data", "chapters", "list"):
        if key in data:
            items = data[key]
            break
    if not isinstance(items, list):
        page.reason = "invalid_payload"
        return page

    page.has_more = _parse_has_more(data.get("hasMore"))
    if not items:
        if page.has_more is False:
            page.status, page.reason = STATUS_END, "trusted_last_page"
        else:
            # 200 但空集合、又没有明确 hasMore=false：可能是临时空页或上游可见范围边界，不能当终点
            page.reason = "empty_page"
        return page

    articles = []
    for idx, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        info = item.get("mpInfo") if isinstance(item.get("mpInfo"), dict) else {}
        review_id = item.get("reviewId") if isinstance(item.get("reviewId"), str) else ""
        title = str(info.get("title") or item.get("title") or "").strip()
        link = info.get("doc_url") if isinstance(info.get("doc_url"), str) else ""
        link = short_link_from_review_id(review_id) or link
        publish_time = _int_or_none(item.get("createTime") or info.get("time")) or 0
        articles.append({
            "id": review_id,
            "review_id": review_id,
            "link": link,
            "title": title,
            "cover": info.get("pic_url") or info.get("thumbUrl") or item.get("cover") or "",
            "digest": info.get("digest") or "",
            "author": info.get("author") or "",
            "update_time": publish_time,
            "publish_time": publish_time,
            "position": offset + idx,
        })
    if not any(a["review_id"] or a["link"] or (a["title"] and a["publish_time"] > 0) for a in articles):
        page.reason = "invalid_chapter"
        return page
    page.articles = articles
    page.raw_count = len(items)
    page.status = STATUS_END if page.has_more is False else STATUS_OK
    page.reason = "trusted_last_page" if page.status == STATUS_END else ""
    return page


class WereadHistoryAdapter:
    """生产实现：严格借号、不轮换重试、命中防水墙记录并上报。测试可整体替换为 FakeAdapter。"""

    def __init__(self, *, session_getter=None, borrow=None, report=None, timeout: float = 10):
        self._session_getter = session_getter
        self._borrow = borrow
        self._report = report
        self.timeout = timeout

    def _session(self):
        if self._session_getter:
            return self._session_getter()
        from backend.account_pool import get_weread_session

        return get_weread_session()

    def _borrow_strict(self):
        if self._borrow:
            return self._borrow()
        from backend.account_pool import borrow_weread_session

        return borrow_weread_session(strict=True)

    def _do_report(self, account_id: str, **kwargs):
        try:
            if self._report:
                self._report(account_id, **kwargs)
                return
            from backend.account_pool import account_pool

            account_pool.report(account_id, **kwargs)
        except Exception as exc:  # noqa: BLE001
            logger.debug("账号状态上报失败: %s", exc)

    def fetch_chapters(self, book_id: str, offset: int, count: int) -> ChapterPage:
        if not book_id:
            return ChapterPage(STATUS_TEMPORARY, offset, count, reason="invalid_book_id")
        try:
            account_id, _vid, _tok, headers = self._borrow_strict()
        except RuntimeError:
            return ChapterPage(STATUS_BLOCKED, offset, count, reason="no_available_account")
        try:
            resp = self._session().get(
                CHAPTERS_URL,
                params={"bookId": book_id, "count": int(count), "offset": int(offset), "pf": "android"},
                headers=headers,
                timeout=self.timeout,
            )
        except Exception as exc:  # noqa: BLE001 —— 网络异常一律视为临时错误
            logger.info("历史回溯章节请求网络异常: %s", type(exc).__name__)
            return ChapterPage(STATUS_TEMPORARY, offset, count, reason="network_error", account_id=account_id)

        text = getattr(resp, "text", "") or ""
        page = parse_chapters_response(resp.status_code, text, offset=offset, count=count,
                                       headers=dict(getattr(resp, "headers", {}) or {}))
        page.account_id = account_id
        if page.reason == "captcha":
            try:
                from backend.waterwall import detect_waterwall, record_failure, waterwall_hit

                marker = detect_waterwall(resp.status_code, text) or "captcha"
                record_failure(CHAPTERS_URL, resp.status_code, getattr(resp, "headers", {}), text, marker)
                waterwall_hit(CHAPTERS_URL, resp.status_code, marker)
            except Exception:  # noqa: BLE001
                pass
        if page.status in (STATUS_OK, STATUS_END):
            self._do_report(account_id, is_success=True)
        elif page.status == STATUS_AUTH:
            self._do_report(account_id, status_code=401, error_msg="历史回溯：登录态过期", allow_activation=False)
        elif page.status == STATUS_BLOCKED and page.reason != "captcha":
            self._do_report(account_id, status_code=page.http_status, ret=page.error_code,
                            error_msg=f"历史回溯：上游限制 ({page.reason})", allow_activation=False)
        return page

    def resolve_link(self, review_id: str) -> str:
        """通过 review/single 解析原文链接（只在章节数据既无短链 id 也无 doc_url 时才调用）。"""
        if not review_id:
            return ""
        try:
            account_id, _vid, _tok, headers = self._borrow_strict()
        except RuntimeError:
            return ""
        try:
            resp = self._session().get(REVIEW_URL, params={"reviewId": review_id}, headers=headers,
                                       timeout=self.timeout)
            if resp.status_code != 200:
                return ""
            mp_info = (resp.json() or {}).get("review", {}).get("mpInfo", {})
            orig_id = mp_info.get("originalId", "")
            if orig_id and len(orig_id) >= 15:
                return f"https://mp.weixin.qq.com/s/{orig_id}"
            return mp_info.get("doc_url", "") or ""
        except Exception:  # noqa: BLE001
            return ""
