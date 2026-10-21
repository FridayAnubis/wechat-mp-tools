"""
历史回溯离线测试用的假上游与工具（仅测试层使用，生产代码不引用）

FakeUpstream 模拟一个按发布时间倒序的公众号文章列表，并按 mp/chapters 的协议切片；
可在测试中途插入 / 删除文章模拟分页漂移，或注入错误响应。所有数据都是本地生成的假数据。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from backend.backfill_upstream import ChapterPage, parse_chapters_response

BASE_TIME = 1_700_000_000  # 2023-11-14，假数据的最新发布时间


def make_article(i: int, *, uin: str = "3090566065", title: str | None = None, ts: int | None = None,
                 with_review: bool = True) -> dict:
    """第 i 篇（0 = 最新）。reviewId 带 22 位短链 id，可直接拼出 https://mp.weixin.qq.com/s/<id>"""
    sid = f"fakeSid{i:015d}"
    return {
        "reviewId": f"MP_WXS_{uin}_{sid}" if with_review else "",
        "createTime": ts if ts is not None else BASE_TIME - i * 3600,
        "mpInfo": {
            "title": title if title is not None else f"历史文章 #{i}",
            "pic_url": f"https://example.invalid/cover/{i}.jpg",
            "digest": f"摘要 {i}",
            "author": "测试作者",
            **({} if with_review else {"doc_url": f"https://mp.weixin.qq.com/s/{sid}"}),
        },
    }


class FakeResponse:
    def __init__(self, status_code: int, payload=None, text: str | None = None, headers: dict | None = None):
        self.status_code = status_code
        self.text = text if text is not None else json.dumps(payload, ensure_ascii=False)
        self.headers = headers or {}

    def json(self):
        return json.loads(self.text)


class FakeUpstream:
    """可脚本化的 mp/chapters。"""

    def __init__(self, total: int = 0, *, has_more_mode: str = "explicit"):
        self.items = [make_article(i) for i in range(total)]
        self.has_more_mode = has_more_mode  # explicit / missing
        self.calls: list[tuple[int, int]] = []
        self.resolve_calls: list[str] = []
        self.scripted: list = []            # 依次弹出：FakeResponse 或 callable(offset, count) -> FakeResponse
        self.visible_limit: int | None = None  # 模拟上游只开放前 N 篇（之后返回空页且无 hasMore=false）

    def insert_new(self, n: int, start_id: int = 100_000):
        new = [make_article(start_id + k, ts=BASE_TIME + (n - k) * 60) for k in range(n)]
        self.items = new + self.items

    def delete_at(self, positions: list[int]):
        for p in sorted(positions, reverse=True):
            del self.items[p]

    def response(self, offset: int, count: int) -> FakeResponse:
        if self.scripted:
            nxt = self.scripted.pop(0)
            return nxt(offset, count) if callable(nxt) else nxt
        total = len(self.items)
        if self.visible_limit is not None and offset >= self.visible_limit:
            # 上游可见边界：真实还有更早的文章，但只返回 200 空列表、不给 hasMore
            return FakeResponse(200, {"data": []})
        end = offset + count if self.visible_limit is None else min(offset + count, self.visible_limit)
        chunk = self.items[offset:end]
        payload = {"data": chunk}
        if self.has_more_mode == "explicit":
            payload["hasMore"] = offset + len(chunk) < total
        return FakeResponse(200, payload)

    # WereadHistoryAdapter 兼容接口
    def fetch_chapters(self, book_id: str, offset: int, count: int) -> ChapterPage:
        self.calls.append((offset, count))
        resp = self.response(offset, count)
        page = parse_chapters_response(resp.status_code, resp.text, offset=offset, count=count, headers=resp.headers)
        page.account_id = "acc_fake"
        return page

    def resolve_link(self, review_id: str) -> str:
        self.resolve_calls.append(review_id)
        return ""


class FakeDownloader:
    """模拟 download_single_article：写出离线 HTML 目录；可按标题注入失败。"""

    def __init__(self):
        self.calls: list[str] = []
        self.fail_titles: dict[str, dict] = {}   # 标题 → 返回值（失败）
        self.fail_times: dict[str, int] = {}     # 标题 → 剩余失败次数（之后成功）

    def __call__(self, url: str, out_dir: Path, title: str, review_id: str = "") -> dict:
        self.calls.append(title)
        remaining = self.fail_times.get(title, 0)
        if remaining > 0:
            self.fail_times[title] = remaining - 1
            return {"success": False, "title": title, "error": "模拟网络超时", "is_permanent": False}
        if title in self.fail_titles:
            return dict(self.fail_titles[title])
        art_dir = Path(out_dir) / title.replace("/", "_")
        counter = 1
        while art_dir.exists():
            art_dir = Path(out_dir) / f"{title}_{counter}"
            counter += 1
        art_dir.mkdir(parents=True)
        (art_dir / f"{art_dir.name}.html").write_text(f"<div id=\"js_content\">{title}</div>", encoding="utf-8")
        (art_dir / "metadata.json").write_text(json.dumps({"title": title, "url": url}), encoding="utf-8")
        return {"success": True, "title": title, "path": str(art_dir), "source": "测试号",
                "author": "测试作者", "cover_url": "", "digest": "", "publish_time": 0,
                "has_markdown": False, "has_pdf": False}


class FakeClock:
    def __init__(self, start: float | None = None):
        self.now = float(start if start is not None else time.time())

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float):
        self.now += seconds


def open_admission_coordinator(clock=None, settings=None, **probes):
    """准入全部放行、请求间隔为 0 的协调器（单测里专注于回溯逻辑本身）"""
    from backend.request_coordinator import RequestCoordinator

    base = {"backfill_request_interval_seconds": 0, "backfill_rss_guard_seconds": 120,
            "backfill_start_hour": 0, "backfill_end_hour": 24}
    base.update(settings or {})
    return RequestCoordinator(
        settings_provider=lambda: base,
        rss_probe=probes.get("rss_probe") or (lambda: {"running": False, "next_due_in": None}),
        account_probe=probes.get("account_probe") or (lambda: {"available": True}),
        waterwall_probe=probes.get("waterwall_probe") or (lambda: {"active": False}),
        clock=clock,
    )
