"""
历史回溯离线测试：存储、游标推进、分页漂移、错误分类、失败重试、事务原子性、重启恢复、
优先级与冷却。全部使用本地假数据与 FakeUpstream / FakeDownloader，不访问任何真实接口。
"""

import json
import sqlite3
import threading
import time
from pathlib import Path

import pytest

from backend import backfill_store as bs
from backend.backfill_upstream import (
    STATUS_AUTH,
    STATUS_BLOCKED,
    STATUS_END,
    STATUS_OK,
    STATUS_TEMPORARY,
    WereadHistoryAdapter,
    parse_chapters_response,
)
from backend.history_backfill import (
    HistoryBackfillService,
    TRIGGER_IDLE,
    TRIGGER_MANUAL,
    normalize_backfill_settings,
    validate_backfill_settings_update,
)
from backend.request_coordinator import (
    PRIORITY_IDLE_BACKFILL,
    PRIORITY_MANUAL_BACKFILL,
    Preempted,
    RequestCoordinator,
)
from backfill_fakes import (
    BASE_TIME,
    FakeClock,
    FakeDownloader,
    FakeResponse,
    FakeUpstream,
    make_article,
    open_admission_coordinator,
)

FAKEID = "MzA5MDU2NjA2NQ=="
NICK = "测试号"


# ---------------------------------------------------------------- fixtures


@pytest.fixture
def store(tmp_path):
    s = bs.BackfillStore(tmp_path / "backfill.db")
    yield s
    s.close()


def make_service(store, tmp_path, upstream, *, settings=None, coordinator=None, downloader=None,
                 accounts=None, clock=None, history=None):
    base = {
        "download_dir": str(tmp_path / "articles_full"),
        "backfill_scan_batch_size": 10,
        "backfill_daily_download_limit": 1000,
        "backfill_min_interval_minutes": 10,
        "backfill_page_overlap": 5,
        "backfill_max_attempts": 3,
        "backfill_auto_enabled": False,
    }
    base.update(settings or {})
    clock = clock or FakeClock()
    history = history if history is not None else []
    svc = HistoryBackfillService(
        store=store,
        coordinator=coordinator or open_admission_coordinator(clock),
        adapter=upstream,
        downloader=downloader or FakeDownloader(),
        settings_provider=lambda: base,
        accounts_provider=lambda: accounts if accounts is not None else [{"fakeid": FAKEID, "nickname": NICK}],
        history_writer=history.append,
        clock=clock,
        owner="test-owner",
    )
    svc._test_history = history
    svc._test_settings = base
    return svc


def run_batches(svc, n, scan=10):
    """手动提交并执行 n 轮；每轮之间推进时钟越过最小间隔"""
    out = []
    for _ in range(n):
        res = svc.submit_run(FAKEID, scan)
        if not res.get("accepted"):
            assert res["state"] == bs.RUN_END_CONFIRMED
            break
        run = svc.store.get_run(res["task_id"])
        out.append(svc.execute_run(run))
        svc.clock.advance(3600)
    return out


def archived_titles(store):
    rows, _ = store.list_articles(FAKEID, limit=100000)
    return {r["title"] for r in rows}


# ---------------------------------------------------------------- 存储：无上限、去重、合并


@pytest.mark.parametrize("n", [201, 501, 1001])
def test_archive_has_no_fixed_cap(store, n):
    with store.transaction() as conn:
        for i in range(n):
            a = make_article(i)
            store.upsert_article(FAKEID, {"id": a["reviewId"], "title": a["mpInfo"]["title"],
                                          "publish_time": a["createTime"]}, source="backfill", conn=conn)
    assert store.count_articles(FAKEID)["total"] == n
    rows, total = store.list_articles(FAKEID, limit=50, offset=n - 10)
    assert total == n and len(rows) == 10
    # 分页读取覆盖全部，无遗漏无重复
    seen = set()
    for off in range(0, n, 100):
        page, _ = store.list_articles(FAKEID, limit=100, offset=off)
        seen.update(r["article_key"] for r in page)
    assert len(seen) == n


def test_identity_priority_review_id_over_url():
    k1, rid, _ = bs.article_identity({"id": "MP_WXS_1_abcdefghijklmnopq", "link": "https://mp.weixin.qq.com/s/x"})
    assert k1 == "review:MP_WXS_1_abcdefghijklmnopq" and rid
    k2, _, norm = bs.article_identity({"link": "https://mp.weixin.qq.com/s?__biz=A&mid=1&idx=1&sn=z&chksm=junk"})
    assert k2 == "url:" + norm and "chksm" not in norm
    k3, _, _ = bs.article_identity({"title": "只有标题"})
    assert k3 == ""  # 无法稳定标识时不入库
    k4, _, _ = bs.article_identity({"id": "MP_WXS_0_shortlinkonly12345", "link": "https://mp.weixin.qq.com/s/shortlinkonly12345"})
    assert k4.startswith("url:")  # 伪造的 MP_WXS_0_ 前缀不算稳定 reviewId


def test_same_title_different_articles_not_merged(store):
    """周报类：同名不同日期 / 不同 reviewId 的文章必须分别保存"""
    for i in range(5):
        store.upsert_article(FAKEID, {"id": f"MP_WXS_1_weekly{i:012d}", "title": "每周简报",
                                      "publish_time": BASE_TIME - i * 7 * 86400}, source="backfill")
    # 无 reviewId、只有链接的同名文章，日期相差一周
    store.upsert_article(FAKEID, {"link": "https://mp.weixin.qq.com/s/aaaaaaaaaaaaaaaaaaaaa", "title": "每周简报",
                                  "publish_time": BASE_TIME - 50 * 86400}, source="manual")
    assert store.count_articles(FAKEID)["total"] == 6


def test_similar_title_time_without_proven_identity_not_merged(store):
    store.upsert_article(FAKEID, {"id": "MP_WXS_1_samearticle0000001", "title": "同一篇",
                                  "publish_time": BASE_TIME}, source="backfill")
    _, created = store.upsert_article(FAKEID, {
        "link": "https://mp.weixin.qq.com/s?__biz=X&mid=9&idx=1&sn=abc", "title": "同一篇",
        "publish_time": BASE_TIME + 30}, source="manual")
    assert created  # 相似标题和时间不足以证明两个不同链接是同一篇文章
    assert store.count_articles(FAKEID)["total"] == 2


def test_renamed_account_keeps_single_fakeid(store):
    """公众号改名：存储主体是 fakeid，昵称只是展示字段"""
    store.upsert_article(FAKEID, {"id": "MP_WXS_1_renamecase000000001", "title": "A", "account_name": "旧名"},
                         source="rss_latest")
    store.upsert_article(FAKEID, {"id": "MP_WXS_1_renamecase000000001", "title": "A", "account_name": "新名"},
                         source="rss_latest")
    assert store.count_articles(FAKEID)["total"] == 1


def test_download_status_never_downgraded(store):
    key, _ = store.upsert_article(FAKEID, {"id": "MP_WXS_1_statuscase00000001", "title": "S"}, source="manual",
                                  download_status=bs.DL_DOWNLOADED, local_path="/ok/path")
    store.upsert_article(FAKEID, {"id": "MP_WXS_1_statuscase00000001", "title": "S"}, source="backfill",
                         download_status=bs.DL_FAILED, local_path="/other/path", last_error="boom")
    row = store.get_article(FAKEID, key)
    assert row["download_status"] == bs.DL_DOWNLOADED
    assert row["local_path"] == "/ok/path"
    assert row["last_error"] == ""
    assert row["discovery_source"] == "manual"


# ---------------------------------------------------------------- 上游响应解析


@pytest.mark.parametrize("status,payload,text,expected,reason", [
    (200, {"data": [make_article(0)], "hasMore": True}, None, STATUS_OK, ""),
    (200, {"data": [make_article(0)], "hasMore": False}, None, STATUS_END, "trusted_last_page"),
    (200, {"data": [], "hasMore": False}, None, STATUS_END, "trusted_last_page"),
    (200, {"data": []}, None, STATUS_TEMPORARY, "empty_page"),
    (200, {"data": [], "hasMore": True}, None, STATUS_TEMPORARY, "empty_page"),
    (200, None, "<html>not json</html>", STATUS_TEMPORARY, "invalid_json"),
    (200, None, "", STATUS_TEMPORARY, "invalid_json"),
    (200, {"foo": 1}, None, STATUS_TEMPORARY, "invalid_payload"),
    (200, {"errcode": -2010, "errmsg": "x"}, None, STATUS_TEMPORARY, "upstream_errcode"),
    (401, {"errcode": -2012}, None, STATUS_AUTH, "auth_expired"),
    (403, {}, None, STATUS_BLOCKED, "upstream_restricted"),
    (429, {}, None, STATUS_BLOCKED, "upstream_restricted"),
    (499, {"errcode": -2013}, None, STATUS_BLOCKED, "upstream_restricted"),
    (499, {"errcode": -2041, "errmsg": "need activate"}, None, STATUS_BLOCKED, "session_inactive"),
    (200, None, '{"errmsg":"请完成验证 tcaptcha"}', STATUS_BLOCKED, "captcha"),
    (502, None, "bad gateway", STATUS_TEMPORARY, "server_error"),
])
def test_parse_chapters_classification(status, payload, text, expected, reason):
    resp = FakeResponse(status, payload, text=text)
    page = parse_chapters_response(resp.status_code, resp.text, offset=50, count=10)
    assert page.status == expected
    assert page.reason == reason


def test_parse_keeps_raw_count_and_positions():
    items = [make_article(i) for i in range(10)]
    items[3] = {"bogus": True}  # 缺字段 / 无法识别的条目仍计入原始条数
    page = parse_chapters_response(200, json.dumps({"data": items, "hasMore": True}), offset=200, count=10)
    assert page.raw_count == 10
    assert [a["position"] for a in page.articles][:3] == [200, 201, 202]
    assert page.articles[0]["link"].startswith("https://mp.weixin.qq.com/s/fakeSid")


def test_adapter_uses_strict_borrow_and_does_not_merge_cover():
    calls = []

    class Sess:
        def get(self, url, params=None, headers=None, timeout=None):
            calls.append((url, dict(params or {})))
            return FakeResponse(200, {"data": [make_article(i) for i in range(10)], "hasMore": True})

    borrowed = []
    reports = []
    adapter = WereadHistoryAdapter(
        session_getter=lambda: Sess(),
        borrow=lambda: borrowed.append(1) or ("acc1", "vid", "tok", {"vid": "vid"}),
        report=lambda acc, **kw: reports.append((acc, kw)),
    )
    page = adapter.fetch_chapters("MP_WXS_1", 0, 10)
    assert page.status == STATUS_OK and page.raw_count == 10 and len(page.articles) == 10
    assert len(calls) == 1 and "mp/chapters" in calls[0][0]  # 没有额外的 mp/cover / article/list 请求
    assert calls[0][1]["offset"] == 0 and calls[0][1]["count"] == 10
    assert reports == [("acc1", {"is_success": True})]


def test_adapter_no_account_is_blocked_not_end():
    def boom():
        raise RuntimeError("no account")

    adapter = WereadHistoryAdapter(session_getter=lambda: None, borrow=boom)
    page = adapter.fetch_chapters("MP_WXS_1", 30, 10)
    assert page.status == STATUS_BLOCKED and page.reason == "no_available_account"


def test_adapter_network_error_is_temporary():
    class Sess:
        def get(self, *a, **k):
            raise ConnectionError("timeout")

    adapter = WereadHistoryAdapter(session_getter=lambda: Sess(), borrow=lambda: ("a", "v", "t", {}),
                                   report=lambda *a, **k: None)
    page = adapter.fetch_chapters("MP_WXS_1", 30, 10)
    assert page.status == STATUS_TEMPORARY and page.reason == "network_error"


# ---------------------------------------------------------------- 游标推进


@pytest.mark.parametrize("total", [250, 600, 1100])
def test_continuous_backfill_beyond_200_500_1000(store, tmp_path, total):
    up = FakeUpstream(total)
    svc = make_service(store, tmp_path, up)
    batches = 0
    while batches < 200:
        res = svc.submit_run(FAKEID, 30)
        if not res["accepted"]:
            break
        svc.execute_run(store.get_run(res["task_id"]))
        svc.clock.advance(3600)
        batches += 1
    st = store.get_state(FAKEID)
    assert st["end_confirmed"] == 1
    assert store.count_articles(FAKEID)["total"] == total
    assert store.pending_counts(FAKEID)[bs.PENDING] == 0
    assert store.count_articles(FAKEID)["downloaded"] == total
    # 每篇文章只下载一次
    assert len(svc._downloader.calls) == total
    assert len(svc._test_history) == total and all(h["backfill"] for h in svc._test_history)


def test_first_page_offset_starts_at_zero_and_uses_raw_count(store, tmp_path):
    up = FakeUpstream(100)
    svc = make_service(store, tmp_path, up)
    run_batches(svc, 1, scan=10)
    assert up.calls[0] == (0, 10)
    assert store.get_state(FAKEID)["next_offset"] == 10


def test_offset_advances_by_raw_count_even_when_items_unusable(store, tmp_path):
    """原始章节里有无法识别的条目：游标仍按上游原始条数推进，不按去重 / 过滤后的条数"""
    up = FakeUpstream(40)
    up.items[5] = {"garbage": 1}
    up.items[6] = {"reviewId": "", "mpInfo": {"title": ""}}
    svc = make_service(store, tmp_path, up)
    run_batches(svc, 1, scan=10)
    assert store.get_state(FAKEID)["next_offset"] == 10
    assert store.count_articles(FAKEID)["total"] == 8


def test_second_batch_reads_with_overlap_and_aligns_anchor(store, tmp_path):
    up = FakeUpstream(100)
    svc = make_service(store, tmp_path, up)
    run_batches(svc, 2, scan=10)
    assert up.calls[1] == (5, 15)  # 重叠 5 条 + 新的 10 条
    st = store.get_state(FAKEID)
    assert st["next_offset"] == 20
    assert store.count_articles(FAKEID)["total"] == 20


def test_new_articles_inserted_shift_offsets_no_gap_no_duplicate(store, tmp_path):
    up = FakeUpstream(200)
    svc = make_service(store, tmp_path, up)
    run_batches(svc, 2, scan=10)  # 已归档 #0..#19
    up.insert_new(3)              # 头部新增 3 篇 → 旧文章整体后移 3 位
    run_batches(svc, 2, scan=10)
    titles = archived_titles(store)
    expected_old = {f"历史文章 #{i}" for i in range(40)}
    assert expected_old.issubset(titles)
    # 新插入的「最新」文章不是回溯目标（由 RSS 最新同步负责），不应被当作历史补抓
    assert not any(t.startswith("历史文章 #1000") for t in titles)
    assert len(svc._downloader.calls) == len(set(svc._downloader.calls))


def test_many_new_articles_beyond_overlap_continue_with_dedupe(store, tmp_path):
    up = FakeUpstream(200)
    svc = make_service(store, tmp_path, up)
    run_batches(svc, 2, scan=10)
    up.insert_new(25)  # 远超重叠窗口
    run_batches(svc, 8, scan=10)
    titles = archived_titles(store)
    assert {f"历史文章 #{i}" for i in range(40)}.issubset(titles)
    assert len(svc._downloader.calls) == len(set(svc._downloader.calls))


def test_deleted_articles_shift_backward_relocates_without_gap(store, tmp_path):
    up = FakeUpstream(300)
    svc = make_service(store, tmp_path, up)
    run_batches(svc, 3, scan=10)  # 已归档 #0..#29，锚点 #29
    up.delete_at(list(range(0, 18)))  # 头部 18 篇被删除 → 锚点 #29 前移到位置 11
    run_batches(svc, 4, scan=10)
    titles = archived_titles(store)
    assert {f"历史文章 #{i}" for i in range(30, 60)}.issubset(titles), "删除导致的前移不能留下空档"
    assert len(svc._downloader.calls) == len(set(svc._downloader.calls))


def test_anchor_article_itself_deleted(store, tmp_path):
    up = FakeUpstream(100)
    svc = make_service(store, tmp_path, up)
    run_batches(svc, 1, scan=10)          # 锚点 #9
    pos = next(i for i, a in enumerate(up.items) if a["mpInfo"]["title"] == "历史文章 #9")
    up.delete_at([pos])
    run_batches(svc, 2, scan=10)
    titles = archived_titles(store)
    assert {f"历史文章 #{i}" for i in range(10, 30)}.issubset(titles)


def test_upstream_reorder_and_duplicate_items_are_deduped(store, tmp_path):
    up = FakeUpstream(60)
    dup = dict(up.items[3])
    up.items.insert(4, dup)  # 上游同一页重复返回一篇
    up.items[10], up.items[11] = up.items[11], up.items[10]  # 局部乱序
    svc = make_service(store, tmp_path, up)
    run_batches(svc, 4, scan=20)  # 61 个原始条目（含重复）；三批的预算只能扫描 60 个
    assert store.count_articles(FAKEID)["total"] == 60
    assert len(svc._downloader.calls) == 60


def test_missing_has_more_with_empty_page_is_suspected_limit_not_end(store, tmp_path):
    """上游只开放一段历史：空页且不给 hasMore=false → 疑似上游边界，绝不标记已确认终点"""
    up = FakeUpstream(500)
    up.visible_limit = 50
    svc = make_service(store, tmp_path, up)
    results = run_batches(svc, 8, scan=20)
    st = store.get_state(FAKEID)
    assert st["end_confirmed"] == 0
    assert st["suspected_limit"] == 1
    assert store.count_articles(FAKEID)["total"] == 50
    assert any(r["reason"] == "suspected_upstream_limit" for r in results)
    status = svc.status(FAKEID)
    assert status["end_confirmed"] is False and status["suspected_limit"] is True
    assert status["total_known"] is None


def test_single_empty_page_defers_without_advancing(store, tmp_path):
    up = FakeUpstream(100)
    svc = make_service(store, tmp_path, up)
    run_batches(svc, 1, scan=10)
    up.scripted.append(FakeResponse(200, {"data": []}))
    res = svc.submit_run(FAKEID, 10)
    run = svc.execute_run(store.get_run(res["task_id"]))
    assert run["state"] == bs.RUN_COOLDOWN and run["reason"] == "empty_page"
    st = store.get_state(FAKEID)
    assert st["next_offset"] == 10 and st["end_confirmed"] == 0


def test_trusted_last_page_marks_end_confirmed(store, tmp_path):
    up = FakeUpstream(25)
    svc = make_service(store, tmp_path, up)
    results = run_batches(svc, 3, scan=10)
    assert store.get_state(FAKEID)["end_confirmed"] == 1
    assert results[-1]["state"] in (bs.RUN_END_CONFIRMED, bs.RUN_COMPLETED_BATCH)
    res = svc.submit_run(FAKEID, 10)
    assert res["accepted"] is False and res["state"] == bs.RUN_END_CONFIRMED


@pytest.mark.parametrize("resp,expect_state,expect_reason", [
    (FakeResponse(401, {"errcode": -2012}), bs.RUN_COOLDOWN, "auth_error"),
    (FakeResponse(403, {}), bs.RUN_COOLDOWN, "upstream_restricted"),
    (FakeResponse(429, {}, headers={"Retry-After": "900"}), bs.RUN_COOLDOWN, "upstream_restricted"),
    (FakeResponse(499, {"errcode": -2013}), bs.RUN_COOLDOWN, "upstream_restricted"),
    (FakeResponse(499, {"errcode": -2041}), bs.RUN_COOLDOWN, "session_inactive"),
    (FakeResponse(200, None, text="{not json"), bs.RUN_COOLDOWN, "invalid_json"),
    (FakeResponse(200, {"unexpected": True}), bs.RUN_COOLDOWN, "invalid_payload"),
    (FakeResponse(500, None, text="oops"), bs.RUN_COOLDOWN, "server_error"),
    (FakeResponse(200, None, text='{"msg":"请完成验证","tcaptcha":1}'), bs.RUN_PAUSED, "captcha_required"),
])
def test_upstream_errors_do_not_advance_or_complete(store, tmp_path, resp, expect_state, expect_reason):
    up = FakeUpstream(100)
    svc = make_service(store, tmp_path, up)
    run_batches(svc, 1, scan=10)
    before = store.get_state(FAKEID)
    up.scripted.append(resp)
    res = svc.submit_run(FAKEID, 10)
    run = svc.execute_run(store.get_run(res["task_id"]))
    after = store.get_state(FAKEID)
    assert run["state"] == expect_state and run["reason"] == expect_reason
    assert after["next_offset"] == before["next_offset"] == 10
    assert after["end_confirmed"] == 0
    assert store.count_articles(FAKEID)["total"] == 10
    if expect_reason == "captcha_required":
        assert after["paused"] == 1 and after["pause_reason"] == "captcha_required"
    if expect_reason == "upstream_restricted":
        until, _ = svc.coordinator.global_cooldown()
        assert until > svc.clock() and run["retry_at"] >= until - 1
        if resp.headers.get("Retry-After"):
            assert until >= svc.clock() + 900


def test_global_cooldown_backoff_is_exponential_and_bounded():
    clock = FakeClock(1000)
    coord = open_admission_coordinator(clock)
    t1 = coord.note_upstream_restriction("x") - clock()
    t2 = coord.note_upstream_restriction("x") - clock()
    for _ in range(20):
        t = coord.note_upstream_restriction("x") - clock()
    assert t2 > t1 and t <= 6 * 3600
    coord.note_upstream_success()
    coord.clear_global_cooldown()
    assert coord.check_admission(PRIORITY_IDLE_BACKFILL).ok


def test_restricted_then_recover_continues_from_same_offset(store, tmp_path):
    up = FakeUpstream(60)
    svc = make_service(store, tmp_path, up)
    run_batches(svc, 1, scan=10)
    up.scripted.append(FakeResponse(429, {}))
    res = svc.submit_run(FAKEID, 10)
    run = svc.execute_run(store.get_run(res["task_id"]))
    assert run["state"] == bs.RUN_COOLDOWN
    # 冷却期内：同一任务不可执行
    assert store.next_runnable_run(svc.clock()) is None
    svc.clock.advance(7 * 3600)
    svc.coordinator.clear_global_cooldown()
    nxt = store.next_runnable_run(svc.clock())
    assert nxt and nxt["id"] == run["id"]
    final = svc.execute_run(nxt)
    assert final["state"] == bs.RUN_COMPLETED_BATCH
    assert {f"历史文章 #{i}" for i in range(20)}.issubset(archived_titles(store))


# ---------------------------------------------------------------- 正文下载失败与重试


def test_failed_article_requeued_others_continue(store, tmp_path):
    up = FakeUpstream(30)
    dl = FakeDownloader()
    dl.fail_titles["历史文章 #3"] = {"success": False, "error": "模拟 HTTP 500", "is_permanent": False}
    dl.fail_titles["历史文章 #4"] = {"success": False, "error": "作者已删除", "is_permanent": True}
    svc = make_service(store, tmp_path, up, downloader=dl, settings={"backfill_max_attempts": 2})
    run_batches(svc, 1, scan=10)
    counts = store.pending_counts(FAKEID)
    assert counts[bs.PENDING_DONE] == 8
    assert counts[bs.PENDING_PERMANENT] == 1
    assert counts[bs.PENDING] == 1  # #3 等待退避重试
    svc.clock.advance(3 * 3600)
    run_batches(svc, 1, scan=10)
    counts = store.pending_counts(FAKEID)
    assert counts[bs.PENDING_FAILED] == 1  # 达到最大次数
    assert store.get_state(FAKEID)["failed_count"] == 2
    # 「失败重试」把它重新排队，下载器恢复后成功
    del dl.fail_titles["历史文章 #3"]
    res = svc.retry_failed(FAKEID)
    assert res["requeued"] == 1
    svc.execute_run(store.get_run(res["task"]["task_id"]))
    row = store.find_article(FAKEID, {"id": make_article(3)["reviewId"]})
    assert row["download_status"] == bs.DL_DOWNLOADED
    assert store.get_state(FAKEID)["failed_count"] == 1  # 只剩永久失败的那篇


def test_transient_failure_then_success_downloads_once(store, tmp_path):
    up = FakeUpstream(10)
    dl = FakeDownloader()
    dl.fail_times["历史文章 #0"] = 1
    svc = make_service(store, tmp_path, up, downloader=dl)
    run_batches(svc, 3, scan=10)
    assert dl.calls.count("历史文章 #0") == 2
    assert store.count_articles(FAKEID)["downloaded"] == 10


def test_risk_control_error_in_download_triggers_global_cooldown(store, tmp_path):
    up = FakeUpstream(20)
    dl = FakeDownloader()
    dl.fail_titles["历史文章 #0"] = {"success": False, "error": "当前环境异常，需完成安全验证"}
    svc = make_service(store, tmp_path, up, downloader=dl)
    res = svc.submit_run(FAKEID, 10)
    run = svc.execute_run(store.get_run(res["task_id"]))
    assert run["state"] == bs.RUN_COOLDOWN and run["reason"] == "article_risk_control"
    assert len(dl.calls) == 1  # 遇风控后立即停手，不继续下载其他文章


def test_daily_download_quota(store, tmp_path):
    up = FakeUpstream(100)
    svc = make_service(store, tmp_path, up, settings={"backfill_daily_download_limit": 15})
    run_batches(svc, 3, scan=10)
    day = time.strftime("%Y-%m-%d", time.localtime(svc.clock()))
    assert len(svc._downloader.calls) <= 15 or store.daily_downloads(FAKEID, day) <= 15


# ---------------------------------------------------------------- 事务原子性与重启恢复


def test_scan_commit_is_atomic_on_crash(store, tmp_path, monkeypatch):
    up = FakeUpstream(50)
    svc = make_service(store, tmp_path, up)
    run_batches(svc, 1, scan=10)
    before_state = store.get_state(FAKEID)
    before_count = store.count_articles(FAKEID)["total"]
    before_pending = store.pending_counts(FAKEID)

    def crash(*a, **k):
        raise RuntimeError("模拟断电：游标提交前崩溃")

    monkeypatch.setattr(store, "_update_cursor", crash)
    res = svc.submit_run(FAKEID, 10)
    run = svc.execute_run(store.get_run(res["task_id"]))
    assert run["state"] == bs.RUN_FAILED
    assert store.get_state(FAKEID)["next_offset"] == before_state["next_offset"]
    assert store.count_articles(FAKEID)["total"] == before_count
    assert store.pending_counts(FAKEID) == before_pending


def test_restart_recovers_stale_running_task_once(tmp_path):
    db = tmp_path / "backfill.db"
    s1 = bs.BackfillStore(db)
    up = FakeUpstream(50)
    svc1 = make_service(s1, tmp_path, up)
    svc1.owner = "old-process"
    res = svc1.submit_run(FAKEID, 10)
    assert s1.claim_run(res["task_id"], "old-process", 120, svc1.clock())
    s1.close()  # 模拟容器被杀：running 租约遗留在库里

    s2 = bs.BackfillStore(db)
    svc2 = make_service(s2, tmp_path, up)
    svc2.owner = "new-process"
    assert svc2.recover() == 0  # 不能抢走仍有效的其他实例租约
    svc2.clock.advance(121)
    assert svc2.recover() == 1
    run = s2.get_run(res["task_id"])
    assert run["state"] == bs.RUN_QUEUED and run["reason"] == "recovered_after_restart"
    # 恢复后仍只有一条活跃任务，重复提交返回同一条
    again = svc2.submit_run(FAKEID, 10)
    assert again["duplicate"] is True and again["task_id"] == res["task_id"]
    svc2.execute_run(s2.get_run(res["task_id"]))
    assert s2.get_state(FAKEID)["next_offset"] == 10
    s2.close()


def test_scheduler_lease_single_instance(store):
    assert store.try_acquire_lease("backfill_scheduler", "A", 120, now=1000)
    assert not store.try_acquire_lease("backfill_scheduler", "B", 120, now=1010)
    assert store.try_acquire_lease("backfill_scheduler", "A", 120, now=1050)  # 续租
    assert store.try_acquire_lease("backfill_scheduler", "B", 120, now=1300)  # 过期后接管


def test_claim_run_prevents_parallel_execution(store):
    run, _ = store.create_run(FAKEID, TRIGGER_MANUAL, 10)
    assert store.claim_run(run["id"], "A", 120, now=1000)
    assert not store.claim_run(run["id"], "B", 120, now=1010)


def test_one_active_run_per_account_enforced_by_db(store):
    store.create_run(FAKEID, TRIGGER_MANUAL, 10)
    with pytest.raises(sqlite3.IntegrityError):
        with store.transaction() as conn:
            conn.execute("INSERT INTO backfill_runs (id, fakeid, trigger, state) VALUES ('x', ?, 'idle', 'queued')",
                         (FAKEID,))


def test_concurrent_submissions_create_single_task(store, tmp_path):
    svc = make_service(store, tmp_path, FakeUpstream(10))
    results = []

    def submit():
        results.append(svc.submit_run(FAKEID, 10))

    threads = [threading.Thread(target=submit) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len({r["task_id"] for r in results}) == 1
    assert sum(1 for r in results if not r["duplicate"]) == 1


# ---------------------------------------------------------------- 优先级 / 冷却 / 暂停


def test_foreground_task_preempts_backfill_between_requests(store, tmp_path):
    up = FakeUpstream(100)
    clock = FakeClock()
    coord = open_admission_coordinator(clock)
    svc = make_service(store, tmp_path, up, coordinator=coord, clock=clock)
    dl = svc._downloader
    entered = {}

    def downloader(url, out_dir, title, review_id=""):
        r = FakeDownloader.__call__(dl, url, out_dir, title, review_id)
        if len(dl.calls) == 3 and "cm" not in entered:
            # 第 3 篇下载完成后，手动下载 / RSS 更新开始
            entered["cm"] = coord.foreground("manual_download")
            entered["cm"].__enter__()
        return r

    svc._downloader = downloader
    res = svc.submit_run(FAKEID, 10)
    run = svc.execute_run(store.get_run(res["task_id"]))
    assert run["state"] == bs.RUN_WAITING_PRIORITY and run["reason"] == "foreground_task_running"
    assert len(dl.calls) == 3  # 正在写入的那篇完成，之后不再发请求
    assert store.pending_counts(FAKEID)[bs.PENDING] == 7  # 剩余待下载保持在队列中
    entered["cm"].__exit__(None, None, None)
    clock.advance(600)
    nxt = store.next_runnable_run(clock())
    final = svc.execute_run(nxt)
    assert final["state"] == bs.RUN_COMPLETED_BATCH and len(dl.calls) == 10


def test_rss_due_soon_blocks_backfill():
    clock = FakeClock(10_000)
    coord = open_admission_coordinator(clock, rss_probe=lambda: {"running": False, "next_due_in": 60})
    adm = coord.check_admission(PRIORITY_MANUAL_BACKFILL)
    assert not adm.ok and adm.reason == "rss_update_due_soon" and adm.run_state == "waiting_priority"
    coord2 = open_admission_coordinator(clock, rss_probe=lambda: {"running": True, "next_due_in": 999})
    assert coord2.check_admission(PRIORITY_IDLE_BACKFILL).reason == "rss_update_running"
    coord3 = open_admission_coordinator(clock, rss_probe=lambda: {"running": False, "next_due_in": 3600})
    assert coord3.check_admission(PRIORITY_IDLE_BACKFILL).ok


def test_all_accounts_cooling_backfill_waits_without_borrowing():
    clock = FakeClock(10_000)
    coord = open_admission_coordinator(
        clock, account_probe=lambda: {"available": False, "reason": "account_cooldown", "next_available_at": 10_100})
    adm = coord.check_admission(PRIORITY_IDLE_BACKFILL)
    assert not adm.ok and adm.reason == "account_cooldown" and adm.retry_at >= 10_100


def test_strict_acquire_never_lends_cooling_account(monkeypatch, tmp_path):
    from backend import account_pool as ap

    now = time.time()
    accounts = [
        {"id": "a1", "vid": "1", "token": "t1", "accessToken": "t1", "status": "cooldown", "cooldown_until": now + 300},
        {"id": "a2", "vid": "2", "token": "t2", "accessToken": "t2", "status": "invalid", "refreshToken": "rf"},
    ]
    saved = {}
    monkeypatch.setattr(ap, "load_json", lambda *a, **k: [dict(x) for x in accounts])
    monkeypatch.setattr(ap, "save_json", lambda path, data: saved.setdefault("data", data))
    refreshed = []
    pool = ap.AccountPool()
    monkeypatch.setattr(pool, "_refresh_account_locked", lambda acc: refreshed.append(acc) or True)
    assert pool.acquire(strict=True) is None
    assert refreshed == []  # 严格模式不会为了借号去刷新失效账号
    lenient = pool.acquire()  # 原有行为保持：兜底借出 / 刷新
    assert lenient is not None
    avail = pool.strict_availability()
    assert avail["available"] is False and avail["reason"] == "account_cooldown"


def test_strict_borrow_context_applies_to_indirect_calls(monkeypatch):
    from backend import account_pool as ap

    seen = []
    monkeypatch.setattr(ap.account_pool, "acquire", lambda strict=False: seen.append(strict) or None)
    with pytest.raises(RuntimeError):
        ap.borrow_weread_session()
    with ap.strict_borrow():
        with pytest.raises(RuntimeError):
            ap.borrow_weread_session()
    assert seen == [False, True]


def test_waterwall_active_blocks_and_outside_window_defers():
    clock = FakeClock(10_000)
    coord = open_admission_coordinator(clock, waterwall_probe=lambda: {"active": True, "blocked_until": 10_200})
    adm = coord.check_admission(PRIORITY_MANUAL_BACKFILL)
    assert adm.reason == "captcha_required" and adm.retry_at >= 10_200

    import datetime

    noon = datetime.datetime(2026, 10, 21, 12, 30).timestamp()
    clock2 = FakeClock(noon)
    coord2 = open_admission_coordinator(clock2, settings={"backfill_start_hour": 1, "backfill_end_hour": 6})
    adm2 = coord2.check_admission(PRIORITY_IDLE_BACKFILL)
    assert adm2.reason == "outside_allowed_hours"
    assert datetime.datetime.fromtimestamp(adm2.retry_at).hour == 1


def test_acquire_slot_enforces_min_interval():
    clock = FakeClock(10_000)
    slept = []

    def sleep(sec):
        slept.append(sec)
        clock.advance(sec)

    coord = open_admission_coordinator(clock, settings={"backfill_request_interval_seconds": 5})
    coord.acquire_slot(PRIORITY_IDLE_BACKFILL, sleep=sleep)
    t1 = clock()
    coord.acquire_slot(PRIORITY_IDLE_BACKFILL, sleep=sleep)
    assert clock() - t1 >= 5 - 1e-6


def test_acquire_slot_raises_when_paused():
    coord = open_admission_coordinator(FakeClock())
    with pytest.raises(Preempted) as ei:
        coord.acquire_slot(PRIORITY_MANUAL_BACKFILL, should_abort=lambda: "user_paused")
    assert ei.value.admission.reason == "user_paused"


def test_pause_mid_run_saves_progress_and_resume_continues(store, tmp_path):
    up = FakeUpstream(100)
    svc = make_service(store, tmp_path, up)
    dl = svc._downloader

    def downloader(url, out_dir, title, review_id=""):
        r = FakeDownloader.__call__(dl, url, out_dir, title, review_id)
        if len(dl.calls) == 2:
            svc.pause(FAKEID)
        return r

    svc._downloader = downloader
    res = svc.submit_run(FAKEID, 10)
    run = svc.execute_run(store.get_run(res["task_id"]))
    assert run["state"] == bs.RUN_PAUSED
    assert len(dl.calls) == 2
    blocked = svc.submit_run(FAKEID, 10)
    assert blocked["accepted"] is False and blocked["state"] == bs.RUN_PAUSED
    r = svc.resume(FAKEID)
    assert r["resumed_task"]["accepted"] is True
    svc.execute_run(store.get_run(r["resumed_task"]["task_id"]))
    assert store.count_articles(FAKEID)["downloaded"] >= 10


def test_reset_cursor_keeps_archive(store, tmp_path):
    up = FakeUpstream(50)
    svc = make_service(store, tmp_path, up)
    run_batches(svc, 2, scan=10)
    before = store.count_articles(FAKEID)
    svc.reset_cursor(FAKEID)
    st = store.get_state(FAKEID)
    assert st["next_offset"] == 0 and st["anchor_key"] == "" and st["end_confirmed"] == 0
    assert store.count_articles(FAKEID) == before
    run_batches(svc, 1, scan=10)
    assert store.count_articles(FAKEID)["total"] == before["total"]  # 去重，不重复建档
    assert len(svc._downloader.calls) == 20  # 已下载的不会再次下载


# ---------------------------------------------------------------- 空闲自动回溯


def test_idle_backfill_disabled_by_default(store, tmp_path):
    svc = make_service(store, tmp_path, FakeUpstream(50))
    store.ensure_state(FAKEID, NICK)
    store.update_state(FAKEID, enabled=1)
    assert svc.pick_idle_candidate() is None
    assert svc.tick() is False


def test_idle_backfill_fair_rotation(store, tmp_path):
    accounts = [{"fakeid": f"F{i}", "nickname": f"号{i}"} for i in range(3)]
    up = FakeUpstream(100)
    svc = make_service(store, tmp_path, up, accounts=accounts, settings={"backfill_auto_enabled": True})
    for a in accounts:
        svc.set_auto(a["fakeid"], True)
    order = []
    for _ in range(3):
        fid = svc.pick_idle_candidate()
        order.append(fid)
        assert svc.tick()
        svc.clock.advance(5)  # 远小于最小间隔：刚跑过的号不会被立刻再选
    assert order[:3] == sorted(set(order[:3])) and len(set(order[:3])) == 3
    assert svc.pick_idle_candidate() is None
    assert svc.tick() is False  # 三个号仍在批次间隔内
    svc.clock.advance(600)
    assert svc.pick_idle_candidate() == order[0]
    assert svc.tick()


def test_idle_run_cancelled_if_auto_disabled_meanwhile(store, tmp_path):
    svc = make_service(store, tmp_path, FakeUpstream(30), settings={"backfill_auto_enabled": True})
    svc.set_auto(FAKEID, True)
    run, _ = store.create_run(FAKEID, TRIGGER_IDLE, 10)
    svc._test_settings["backfill_auto_enabled"] = False
    out = svc.execute_run(run)
    assert out["state"] == bs.RUN_CANCELLED


def test_manual_works_when_auto_disabled_and_rss_not_subscribed(store, tmp_path):
    svc = make_service(store, tmp_path, FakeUpstream(30))
    res = svc.submit_run(FAKEID, 10)
    assert res["accepted"] and res["state"] == bs.RUN_QUEUED
    run = svc.execute_run(store.get_run(res["task_id"]))
    assert run["state"] == bs.RUN_COMPLETED_BATCH


def test_manual_submission_upgrades_queued_idle_run(store, tmp_path):
    svc = make_service(store, tmp_path, FakeUpstream(30), settings={"backfill_auto_enabled": True})
    run, _ = store.create_run(FAKEID, TRIGGER_IDLE, 10)
    res = svc.submit_run(FAKEID, 20)
    assert res["duplicate"] and res["task_id"] == run["id"] and res["trigger"] == TRIGGER_MANUAL


# ---------------------------------------------------------------- 设置校验


def test_settings_validation_and_normalization():
    clean, errors = validate_backfill_settings_update({"backfill_scan_batch_size": 100})
    assert "backfill_scan_batch_size" in errors
    clean, errors = validate_backfill_settings_update({"backfill_auto_enabled": "yes", "backfill_scan_batch_size": 20})
    assert not errors and clean == {"backfill_auto_enabled": True, "backfill_scan_batch_size": 20}
    _, errors = validate_backfill_settings_update({"unknown_key": 1})
    assert "unknown_key" in errors
    _, errors = validate_backfill_settings_update({"backfill_scan_batch_size": True})
    assert errors
    _, errors = validate_backfill_settings_update({"backfill_scan_batch_size": 10.5})
    assert errors
    _, errors = validate_backfill_settings_update([1, 2])
    assert errors
    norm = normalize_backfill_settings({"backfill_scan_batch_size": 999, "backfill_daily_download_limit": "x"})
    assert norm["backfill_scan_batch_size"] == 30 and norm["backfill_daily_download_limit"] == 40
    assert norm["backfill_auto_enabled"] is False and norm["rss_include_backfill"] is False


def test_backward_drift_still_respects_per_run_scan_budget(store, tmp_path):
    up = FakeUpstream(100)
    svc = make_service(store, tmp_path, up)
    run_batches(svc, 2, scan=10)
    up.delete_at([0, 1, 2])
    result = run_batches(svc, 1, scan=10)[0]
    assert result['scanned'] <= 10 and result['downloaded'] <= 10
    assert store.get_state(FAKEID)['next_offset'] == 27


def test_same_owner_cannot_claim_running_or_completed_run_twice(store):
    store.ensure_state(FAKEID)
    run, _ = store.create_run(FAKEID, TRIGGER_MANUAL, 10)
    assert store.claim_run(run['id'], 'one', 120)
    assert not store.claim_run(run['id'], 'one', 120)
    store.update_run(run['id'], state=bs.RUN_COMPLETED_BATCH)
    assert not store.claim_run(run['id'], 'one', 120)
