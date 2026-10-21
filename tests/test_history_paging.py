import json
from unittest.mock import Mock

import pytest
import requests

from backend import account_pool as pool_module
from backend import history_paging as paging
from backend.config import save_json, load_json
from scripts.probe_history_pagination import probe


def response(data, status=200):
    result = requests.Response()
    result.status_code = status
    result._content = json.dumps(data).encode()
    return result


def chapter(rid="review-1", title="周报", timestamp=100):
    return {"reviewId": rid, "title": title, "createTime": timestamp}


@pytest.mark.parametrize("status,state,reason", [
    (401, "auth_error", "auth_expired"),
    (403, "blocked", "upstream_restricted"),
    (429, "blocked", "upstream_restricted"),
    (499, "blocked", "upstream_restricted"),
    (500, "temporary_error", "http_error"),
])
def test_http_errors_preserve_cursor(status, state, reason):
    page = paging.parse_history_page(response({}, status), 200, 10)
    assert (page.state, page.reason, page.next_offset) == (state, reason, 200)
    assert page.articles == []


@pytest.mark.parametrize("data", [
    {}, [], {"data": []}, {"data": [], "hasMore": False},
    {"data": "invalid"}, {"data": [None]}, {"data": [{"title": "无稳定标识"}]},
    {"data": [chapter()], "hasMore": "false"},
    {"data": [chapter()], "errcode": 1234},
    {"data": [chapter()], "errcode": "invalid"},
    {"data": [{"reviewId": 123}]},
])
def test_empty_or_malformed_is_not_end(data):
    page = paging.parse_history_page(response(data), 500, 10)
    assert page.state == "temporary_error"
    assert page.next_offset == 500


def test_nonempty_explicit_last_page_and_raw_cursor():
    items = [chapter(), chapter(), chapter("review-2")]
    page = paging.parse_history_page(response({"data": items, "hasMore": False}), 10, 10)
    assert page.state == "end"
    assert (page.raw_count, page.next_offset) == (3, 13)
    assert len(page.articles) == 2  # 重复标题保留，重复 ID 合并。


def test_short_page_without_has_more_does_not_imply_end():
    page = paging.parse_history_page(response({"chapters": [chapter()]}), 0, 10)
    assert (page.state, page.has_more, page.next_offset) == ("ok", None, 1)


def test_url_identity_is_normalized():
    url = "https://mp.weixin.qq.com/s/abc"
    items = [{"title": "周报", "mpInfo": {"doc_url": url + "?scene=1"}},
             {"title": "周报", "mpInfo": {"doc_url": url + "?scene=2"}}]
    page = paging.parse_history_page(response({"list": items, "hasMore": 1}), 0, 10)
    assert len(page.articles) == 1
    assert page.next_offset == 2


@pytest.mark.parametrize("data,status,state,reason", [
    ({"errcode": -2041}, 499, "blocked", "session_inactive"),
    ({"errcode": "-2012"}, 200, "auth_error", "auth_expired"),
    ({"errmsg": "tcaptcha"}, 200, "blocked", "captcha"),
])
def test_upstream_error_classification(data, status, state, reason):
    page = paging.parse_history_page(response(data, status), 50, 10)
    assert (page.state, page.reason, page.next_offset) == (state, reason, 50)


@pytest.fixture
def isolated_pool(monkeypatch, tmp_path):
    pool_file = tmp_path / "account_pool.json"
    monkeypatch.setattr(pool_module, "ACCOUNT_POOL_FILE", pool_file)
    pool = pool_module.AccountPool()
    monkeypatch.setattr(pool, "_try_find_local_session", lambda: None)
    refresh = Mock(return_value=False)
    monkeypatch.setattr(pool, "_refresh_account_locked", refresh)
    return pool, pool_file, refresh


def test_strict_acquire_never_borrows_cooling_account(isolated_pool):
    pool, path, refresh = isolated_pool
    save_json(path, [{"id": "a", "vid": "1", "status": "cooldown", "cooldown_until": 10**12}])
    assert pool.acquire(strict=True) is None
    refresh.assert_not_called()
    assert pool.acquire()["id"] == "a"  # 旧调用保留原行为。


def test_strict_acquire_does_not_refresh_invalid_account(isolated_pool):
    pool, path, refresh = isolated_pool
    save_json(path, [{"id": "a", "vid": "1", "status": "invalid", "refreshToken": "private"}])
    assert pool.acquire(strict=True) is None
    refresh.assert_not_called()


def test_strict_acquire_recovers_expired_cooldown(isolated_pool):
    pool, path, _ = isolated_pool
    save_json(path, [{"id": "a", "vid": "1", "token": "private",
                      "status": "cooldown", "cooldown_until": 1}])
    assert pool.acquire(strict=True)["status"] == "active"


@pytest.mark.parametrize("status,code,expected", [(401, None, "invalid"), (499, -2041, "cooldown")])
def test_history_report_does_not_make_refresh_requests(isolated_pool, monkeypatch, status, code, expected):
    pool, path, refresh = isolated_pool
    save_json(path, [{"id": "a", "vid": "1", "status": "active"}])
    activate = Mock()
    monkeypatch.setattr("backend.weread_signer.activate_terminal_session", activate)
    pool.report("a", status_code=status, ret=code, allow_refresh=False)
    refresh.assert_not_called()
    activate.assert_not_called()
    assert load_json(path)[0]["status"] == expected


def test_legacy_report_still_refreshes(isolated_pool):
    pool, path, refresh = isolated_pool
    save_json(path, [{"id": "a", "vid": "1", "status": "active"}])
    pool.report("a", status_code=401)
    refresh.assert_called_once()


def test_legacy_article_list_contract_is_unchanged(monkeypatch):
    from flask import Flask
    from backend import articles

    session = Mock()
    session.get.side_effect = [
        response({"reviewId": "MP_coverabcdefghijkl", "title": "置顶文章"}),
        response({"data": [chapter("MP_chapterabcdefghij", "列表文章")], "hasMore": True}),
    ]
    monkeypatch.setattr(articles, "get_weread_session", lambda: session)
    monkeypatch.setattr(articles, "borrow_weread_session", lambda: ("a", "vid", "private", {}))
    monkeypatch.setattr(articles.account_pool, "report", Mock())
    monkeypatch.setattr("backend.waterwall.is_waterwall_active", lambda: False)
    app = Flask(__name__)
    app.register_blueprint(articles.articles_bp)
    result = app.test_client().get("/api/articles/list/MP_WXS_1?count=10")
    assert result.status_code == 200
    data = result.get_json()
    assert set(data) == {"articles", "total", "can_msg_continue", "begin", "count"}
    assert [a["title"] for a in data["articles"]] == ["置顶文章", "列表文章"]
    assert data["can_msg_continue"] == 1
    assert data["total"] == 12
    assert session.get.call_count == 2


@pytest.fixture
def upstream(monkeypatch):
    session = Mock()
    monkeypatch.setattr(paging, "get_weread_session", lambda: session)
    monkeypatch.setattr(paging, "is_waterwall_active", lambda: False)
    borrow = Mock(return_value=("a", "vid", "private", {"accessToken": "private"}))
    monkeypatch.setattr(paging, "borrow_weread_session", borrow)
    monkeypatch.setattr(paging.account_pool, "report", Mock())
    return session, borrow


def test_fetch_only_chapters_not_cover_or_fallback(upstream):
    session, borrow = upstream
    session.get.return_value = response({"data": [chapter()], "hasMore": True})
    page = paging.fetch_history_page("MP_WXS_1", 0)
    assert page.next_offset == 1
    borrow.assert_called_once_with(strict=True)
    session.get.assert_called_once()
    assert session.get.call_args.args[0].endswith("/mp/chapters")
    assert session.get.call_args.kwargs["params"]["offset"] == 0


def test_fetch_timeout_preserves_cursor_without_fallback(upstream):
    session, _ = upstream
    session.get.side_effect = requests.Timeout("private response")
    page = paging.fetch_history_page("MP_WXS_1", 200)
    assert (page.state, page.next_offset, page.reason) == ("temporary_error", 200, "network_error")
    session.get.assert_called_once()
    assert "private" not in repr(page)


def test_waterwall_stops_before_borrow(upstream, monkeypatch):
    session, borrow = upstream
    monkeypatch.setattr(paging, "is_waterwall_active", lambda: True)
    assert paging.fetch_history_page("MP_WXS_1", 0).reason == "captcha"
    borrow.assert_not_called()
    session.get.assert_not_called()


def test_no_available_account_makes_no_request(upstream):
    session, borrow = upstream
    borrow.side_effect = RuntimeError("no account")
    assert paging.fetch_history_page("MP_WXS_1", 0).reason == "no_available_account"
    session.get.assert_not_called()


def test_probe_requires_distinct_older_articles_and_redacts():
    def fetch(fakeid, offset, count):
        return paging.parse_history_page(response({"data": [chapter(f"private-{offset}", timestamp=100-offset)],
                                                  "hasMore": True}), offset, count)
    report = probe(fetch, "MP_WXS_1", 10, [], 2, 10, sleep=lambda _: None)
    assert report["g0_passed"]
    assert [p["offset"] for p in report["pages"]] == [0, 1, 0, 1]
    assert "private" not in json.dumps(report)


def test_probe_repeated_first_page_cannot_pass_g0():
    def fetch(fakeid, offset, count):
        return paging.parse_history_page(response({"data": [chapter()], "hasMore": True}), offset, count)
    report = probe(fetch, "MP_WXS_1", 10, [50, 200, 500], 1, 10, sleep=lambda _: None)
    assert not report["g0_passed"]


def test_probe_stops_on_error_without_trying_more_offsets():
    fetch = Mock(return_value=paging.HistoryPage("blocked", 0, 0, reason="captcha"))
    report = probe(fetch, "MP_WXS_1", 10, [50, 200, 500], 2, 10)
    assert report["stopped_reason"] == "captcha"
    assert not report["g0_passed"]
    fetch.assert_called_once()
