"""旧数据迁移、SQL 分页、RSS 隔离、共享写入与下载器间接 HTTP 的回归。"""
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
import requests
from flask import Flask

from backend import backfill_store as bs, config, articles, rss_feed, rss_scheduler, downloader
from backend.request_coordinator import (CoordinatedSession, Preempted, backfill_network,
                                         PRIORITY_MANUAL_BACKFILL, RequestCoordinator)
from backfill_fakes import FakeClock, open_admission_coordinator, FakeDownloader
from test_history_backfill import FAKEID, NICK


@pytest.fixture
def env(tmp_path, monkeypatch):
    for mod in (config, articles, rss_feed, rss_scheduler, bs):
        for name in ('DOWNLOAD_HISTORY_FILE', 'ACCOUNTS_FILE', 'RSS_ARTICLES_FILE', 'RSS_SUBSCRIPTIONS_FILE', 'APP_SETTINGS_FILE'):
            if hasattr(mod, name):
                monkeypatch.setattr(mod, name, tmp_path / getattr(mod, name).name)
    config.save_json(config.ACCOUNTS_FILE, [{'fakeid': FAKEID, 'nickname': NICK}])
    config.save_settings({'download_dir': str(tmp_path / 'articles_full'), 'rss_local_avatar': False,
                          'rss_avatar_from_article': False, 'save_pdf': False, 'max_retries': 1})
    store = bs.BackfillStore(tmp_path / 'backfill.db')
    monkeypatch.setattr(bs, '_store', store)
    yield store, tmp_path
    store.close()


def legacy_record(i, **kwargs):
    return {'title': f'旧文章{i}', 'account': NICK, 'link': f'https://mp.weixin.qq.com/s/fake{i:019}',
            'time': 1700000000 + i, 'publish_time': 1600000000 + i, 'success': True,
            'path': f'/offline/articles/{i}', **kwargs}


def test_1001_migration_backups_idempotent_and_sql_paging(env, monkeypatch):
    store, path = env
    records = [legacy_record(i) for i in range(1001)]
    config.save_json(config.DOWNLOAD_HISTORY_FILE, records)
    config.save_json(bs.RSS_ARTICLES_FILE, {NICK: [{'title': r['title'], 'link': r['link'],
                                                  'path': r['path'], 'update_time': r['publish_time']} for r in records]})
    before = config.DOWNLOAD_HISTORY_FILE.read_bytes()
    result = bs.migrate_legacy_data(store, data_dir=path)
    assert result['history_items'] == 1001 and result['rss_items'] == 1001
    assert store.count_articles(FAKEID)['total'] == 1001
    assert Path(result['backup_dir'], 'download_history.json').read_bytes() == before
    assert bs.migrate_legacy_data(store, data_dir=path) == {'skipped': True}
    bs.migrate_legacy_data(store, force=True, data_dir=path)
    assert store.count_articles(FAKEID)['total'] == 1001
    store.sync_history_file()
    rows, total, accounts = store.history_page(limit=50, offset=500, account=NICK, publish=True)
    assert total == 1001 and len(rows) == 50 and rows[0]['title'] == '旧文章500'
    assert accounts == [NICK]
    # 文件签名未变化时，翻页不得重新全量解析旧历史。
    monkeypatch.setattr(Path, 'read_text', lambda *a, **k: (_ for _ in ()).throw(AssertionError('full JSON reread')))
    store.sync_history_file()
    assert len(store.history_page(limit=50, offset=550)[0]) == 50
    assert len(rss_feed.collect_items(NICK, limit=50, offset=500)) == 50
    assert config.DOWNLOAD_HISTORY_FILE.read_bytes() == before


def test_migration_failure_rolls_back_without_marking_done(env, monkeypatch):
    store, path = env
    config.save_json(config.DOWNLOAD_HISTORY_FILE, [legacy_record(i) for i in range(3)])
    original = store.upsert_article
    count = []
    def fail(*args, **kwargs):
        count.append(1)
        if len(count) == 2:
            raise RuntimeError('模拟断电')
        return original(*args, **kwargs)
    monkeypatch.setattr(store, 'upsert_article', fail)
    with pytest.raises(RuntimeError):
        bs.migrate_legacy_data(store, data_dir=path)
    assert not store.get_meta(bs.MIGRATION_META_KEY)
    assert store.count_articles(FAKEID)['total'] == 0
    assert len(json.loads(config.DOWNLOAD_HISTORY_FILE.read_text())) == 3


def test_malformed_legacy_json_is_not_silently_migrated(env):
    store, path = env
    config.DOWNLOAD_HISTORY_FILE.write_text('{broken')
    with pytest.raises(ValueError):
        bs.migrate_legacy_data(store, data_dir=path)
    assert not store.get_meta(bs.MIGRATION_META_KEY)


def test_rss_backfill_isolation_pagination_and_history_api(env):
    store, _ = env
    config.save_json(config.DOWNLOAD_HISTORY_FILE, [legacy_record(i, backfill=i < 1000) for i in range(1001)])
    app = Flask(__name__)
    app.register_blueprint(articles.articles_bp)
    client = app.test_client()
    assert len(rss_feed.collect_items(NICK)) == 1
    assert len(rss_feed.collect_items(NICK, include_backfill=True, limit=50, offset=500)) == 50
    xml, _ = rss_feed.build_feed(NICK, 50, 500, False, 'http://localhost/', include_backfill=True)
    assert xml.count('<item>') == 50
    r = client.get('/api/articles/history?view=reader&sort=publish&limit=50&offset=500').json
    assert r['total'] == 1001 and len(r['history']) == 50 and r['has_more']
    assert r['history'][0]['_index'] == 500
    scheduler = rss_scheduler.RssScheduler()
    data = json.loads(config.DOWNLOAD_HISTORY_FILE.read_text())
    assert scheduler.count_pending(data, NICK) == 1
    assert len(scheduler._collect_unuploaded(data, NICK)) == 1


def test_concurrent_history_writers_preserve_all_records(env):
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda i: config.append_download_history([legacy_record(i)]), range(100)))
    records = json.loads(config.DOWNLOAD_HISTORY_FILE.read_text())
    assert len(records) == 100 and len({r['link'] for r in records}) == 100


def test_rss_latest_preserves_large_archive_and_same_title(env, monkeypatch):
    store, path = env
    scheduler = rss_scheduler.RssScheduler()
    existing = [{'id': f'MP_WXS_1_old{i:019}', 'link': f'https://mp.weixin.qq.com/s/old{i:019}',
                 'title': '简报', 'update_time': 1600000000+i, 'path': f'/offline/{i}'} for i in range(1001)]
    scheduler._save_articles(NICK, existing, fakeid=FAKEID)
    newer = [{'id': f'MP_WXS_1_new{i:019}', 'link': f'https://mp.weixin.qq.com/s/new{i:019}',
              'title': '简报', 'update_time': 1700000000+i} for i in range(2)]
    monkeypatch.setattr(articles, '_fetch_articles_page', lambda *a, **kw: (newer, 2, False))
    fake = FakeDownloader()
    monkeypatch.setattr(downloader, 'download_single_article', fake)
    monkeypatch.setattr(rss_scheduler.time, 'sleep', lambda *_: None)
    scheduler._fetch_for_account({'fakeid': FAKEID, 'nickname': NICK})
    assert len(fake.calls) == 2
    assert len(scheduler.get_articles(NICK)) == 1003
    scheduler._fetch_for_account({'fakeid': FAKEID, 'nickname': NICK})
    assert len(fake.calls) == 2


def test_transport_gates_indirect_requests_and_stops_on_restriction(monkeypatch):
    clock = FakeClock()
    coord = open_admission_coordinator(clock)
    slots, sent = [], []
    original = coord.acquire_slot
    def slot(*a, **kw):
        slots.append(1)
        return original(*a, **kw)
    monkeypatch.setattr(coord, 'acquire_slot', slot)
    def send(self, request, **kwargs):
        sent.append(request.url)
        r = requests.Response()
        r.status_code = 429 if len(sent) == 2 else 200
        r._content = b'{}'
        r.headers['Content-Type'] = 'application/json'
        r.url = request.url
        return r
    monkeypatch.setattr(requests.Session, 'send', send)
    with backfill_network(coord, PRIORITY_MANUAL_BACKFILL, lambda: None):
        session = CoordinatedSession()
        session.get('https://example.invalid/review')
        with pytest.raises(Preempted):
            session.get('https://example.invalid/content')
        with pytest.raises(Preempted):
            session.get('https://example.invalid/media')
    assert len(sent) == 2 and len(slots) == 3
    assert coord.global_cooldown()[0] > clock()


def test_cooldown_persists_across_restart(env):
    store, _ = env
    kwargs = dict(settings_provider=lambda: {}, state_store=store)
    coord = RequestCoordinator(**kwargs)
    until = coord.note_upstream_restriction('429')
    restarted = RequestCoordinator(**kwargs)
    assert restarted.global_cooldown() == (until, '429')


def test_failed_same_title_download_keeps_successful_files(env, monkeypatch):
    _, path = env
    folder = path / 'articles_full' / '简报'
    folder.mkdir(parents=True)
    html = folder / '简报.html'
    html.write_text('previous success')
    monkeypatch.setattr(downloader, 'fetch_wechat_article_html', lambda *a, **kw: ('', '', 'offline failure'))
    assert not downloader.download_single_article('https://mp.weixin.qq.com/s/fake', folder.parent, '简报')['success']
    assert html.read_text() == 'previous success'


def test_manual_batch_preserves_concurrent_history_append(env, monkeypatch):
    store, path = env
    fake = FakeDownloader()
    def download(*args, **kwargs):
        # 模拟下载期间 RSS / 回溯线程写入另一条记录。
        config.append_download_history([legacy_record(999)])
        return fake(*args, **kwargs)
    monkeypatch.setattr(downloader, 'download_single_article', download)
    monkeypatch.setitem(articles._download_tasks, 'offline-batch', {
        'status': 'running', 'completed': 0, 'failed': 0, 'results': []})
    articles._do_batch_download('offline-batch', [{
        'id': 'MP_WXS_1_manualoffline000001', 'link': 'https://mp.weixin.qq.com/s/manualoffline000001',
        'title': '手动下载', 'update_time': 1700000000}], NICK)
    task = articles._download_tasks['offline-batch']
    assert task['status'] == 'completed' and task['completed'] == 1
    history = json.loads(config.DOWNLOAD_HISTORY_FILE.read_text())
    assert len(history) == 2 and {r['title'] for r in history} == {'旧文章999', '手动下载'}
    assert store.count_articles(FAKEID)['downloaded'] == 1


# 请求层使用真实下载函数和离线 HTTP 响应，覆盖 FakeDownloader 无法发现的正文链路错误。
def article_response(html, *, status=200, url='https://mp.weixin.qq.com/s/offlineArticle000000001',
                     content_type='text/html; charset=utf-8', encoding=None):
    response = requests.Response()
    response.status_code = status
    response.url = url
    response._content = html.encode('utf-8') if isinstance(html, str) else html
    response.headers['Content-Type'] = content_type
    if encoding:
        response.headers['Content-Encoding'] = encoding
    return response


@pytest.mark.parametrize('review_id,expected', [
    ('MP_WXS_3286016687_yvLKiOm~5JBieS0oLN~BMQ', 'yvLKiOm_5JBieS0oLN_BMQ'),
    ('MP_WXS_1_abcd_efgh_ijklmnopqr', 'abcd_efgh_ijklmnopqr'),
    ('MP_WXS_0_abcd~efgh~ijklmnopqr', 'abcd_efgh_ijklmnopqr'),
    ('other_abcdefghijklmnopq', ''),
    ('MP_WXS_1_abcdefghijklmnopq?token=secret', ''),
])
def test_shared_review_link_conversion(review_id, expected):
    from backend.backfill_upstream import short_link_from_review_id
    url = f'https://mp.weixin.qq.com/s/{expected}' if expected else ''
    assert short_link_from_review_id(review_id) == url
    if expected:
        assert downloader.resolve_canonical_short_url('', review_id) == url
        assert articles._resolve_review_doc_url(review_id, {}) == url


def test_parser_preserves_explicit_doc_url():
    from backend.backfill_upstream import parse_chapters_response
    original = 'https://mp.weixin.qq.com/s?__biz=offline&mid=1&idx=1&sn=offline'
    review = 'MP_WXS_1_abcdefghijkl~mnopqr'
    page = parse_chapters_response(200, json.dumps({'data': [
        {'reviewId': review, 'mpInfo': {'title': '文章', 'doc_url': original}}
    ], 'hasMore': False}), offset=0, count=10)
    assert page.articles[0]['link'] == page.articles[0]['doc_url'] == original
    assert page.articles[0]['derived_short_url'].endswith('/abcdefghijkl_mnopqr')


@pytest.mark.parametrize('html,expected', [
    ("<div data-x='1' id = 'js_content'><p>正文</p></div>", 'article'),
    ("<section class='extra rich_media_content'><p>正文</p></section>", 'article'),
    ("<div id='js_content'><img data-src='https://example.invalid/image.jpg'></div>", 'article'),
    ("<div id='js_content'><iframe data-mpvid='offline-video'></iframe></div>", 'article'),
    ("<div id='js_article'><p>正文</p></div>", 'article'),
    ("<div id='js_article'><h1>只有标题</h1></div>", 'invalid'),
    ("<div id='js_content'> &nbsp;<script>var x='非正文';</script></div>", 'invalid'),
    ('<html><p>参数错误</p></html>', 'invalid'),
    ('<html><input type="password">请登录</html>', 'invalid'),
    ('<div id="app">登录</div>', 'invalid'),
    ('<html><p>内容已被作者删除</p></html>', 'permanent'),
    ('<html>当前环境异常，完成验证后即可继续访问</html>', 'blocked'),
    ("<script>var picture_page_info_list=[{cdn_url:'https://example.invalid/pic.jpg'}];</script>", 'article'),
])
def test_article_response_requires_meaningful_body(html, expected):
    assert downloader._article_page_state(html)[0] == expected


def test_real_downloader_tries_derived_link_after_http_200_error(env, monkeypatch):
    _, path = env
    original = 'https://mp.weixin.qq.com/s?__biz=offline&mid=1&idx=1&sn=offline'
    review = 'MP_WXS_1_abcdefghijkl~mnopqr'
    called = []
    def get(url, **kwargs):
        called.append(url)
        assert kwargs['headers']['Accept-Encoding'] == 'gzip, deflate'
        html = '<html>参数错误</html>' if len(called) == 1 else "<html><div id = 'js_content'><p>离线正文</p></div></html>"
        return article_response(html, url=url)
    monkeypatch.setattr(downloader, 'coordinated_get', get)
    monkeypatch.setattr(downloader, 'fetch_via_weread_content', lambda *a: pytest.fail('正文成功后不应调用 Web'))
    monkeypatch.setattr(downloader, 'fetch_via_headless_browser', lambda *a, **k: pytest.fail('不应启动浏览器'))
    result = downloader.download_single_article(original, path/'articles', '链接还原', review_id=review)
    assert result['success'], result
    assert called == [original, 'https://mp.weixin.qq.com/s/abcdefghijkl_mnopqr']
    assert '离线正文' in next(p for p in Path(result['path']).glob('*.html') if not p.name.endswith('_raw.html')).read_text()
    assert '离线正文' in (Path(result['path'])/'content.txt').read_text()
    assert (Path(result['path'])/'metadata.json').exists()


def test_invalid_http_200_falls_through_to_web_with_full_cookie(env, monkeypatch):
    from backend import account_pool as ap
    cookie = 'wr_vid=offline; wr_skey=web-key; wr_rt=web-refresh; extra=preserved'
    monkeypatch.setattr(ap.account_pool, 'acquire', lambda **kw: {'id': 'fake', 'vid': 'offline', 'web_cookie': cookie})
    monkeypatch.setattr(downloader, 'coordinated_get', lambda *a, **k: article_response('<html>参数错误</html>'))
    calls = []
    class Session:
        def get(self, url, **kwargs):
            calls.append(kwargs)
            return article_response("<div class='rich_media_content'><p>Web正文</p></div>", url=url)
    monkeypatch.setattr(ap, 'get_weread_session', lambda: Session())
    html, _, error = downloader.fetch_wechat_article_html('https://mp.weixin.qq.com/s/abcdefghijklmnopqr',
                                                         'MP_WXS_1_abcdefghijklmnopqr')
    assert 'Web正文' in html and not error
    assert calls[0]['headers']['Cookie'] == cookie
    assert 'accessToken' not in calls[0]['headers']


@pytest.mark.parametrize('status,html', [(200, '<html>请登录</html>'), (401, '{}')])
def test_web_auth_failure_is_diagnosable_without_logging_secrets(env, monkeypatch, caplog, status, html):
    from backend import account_pool as ap
    cookie = 'wr_vid=offline; wr_skey=PRIVATE-COOKIE; wr_rt=PRIVATE-REFRESH'
    monkeypatch.setattr(ap.account_pool, 'acquire', lambda **kw: {'id': 'fake', 'web_cookie': cookie})
    class Session:
        def get(self, *a, **kw):
            return article_response(html, status=status, url='https://weread.qq.com/login?token=PRIVATE-TOKEN#PRIVATE-FRAGMENT')
    monkeypatch.setattr(ap, 'get_weread_session', lambda: Session())
    monkeypatch.setattr(downloader, 'coordinated_get', lambda *a, **kw: article_response('<html>参数错误</html>'))
    monkeypatch.setattr(downloader, 'in_backfill_network', lambda: True)
    monkeypatch.setattr(downloader, 'fetch_via_headless_browser', lambda *a, **kw: pytest.fail('历史模式禁用浏览器'))
    with caplog.at_level('INFO'):
        body, _, error = downloader.fetch_wechat_article_html('https://mp.weixin.qq.com/s/abcdefghijklmnopqr',
                                                              'MP_WXS_1_abcdefghijklmnopqr')
    assert not body and 'channel=weread_web' in error and f'http={status}' in error
    assert '参数错误' in error
    assert 'PRIVATE-' not in error + caplog.text


def test_mobile_credentials_do_not_impersonate_web_cookie(monkeypatch):
    from backend import account_pool as ap
    strict_calls = []
    monkeypatch.setattr(ap.account_pool, 'acquire', lambda strict=False: strict_calls.append(strict) or {
        'id': 'fake', 'accessToken': 'mobile-token', 'cookie_str': 'wr_vid=offline; wr_skey=mobile-token'})
    monkeypatch.setattr(ap, 'get_weread_session', lambda: pytest.fail('没有 Web 会话时不应请求接口'))
    diagnostics = []
    with ap.strict_borrow():
        assert downloader.fetch_via_weread_content('MP_WXS_1_abcdefghijklmnopqr', diagnostics) == ('', False)
    assert strict_calls == [True]
    assert 'auth_missing' in diagnostics[0] and 'Web Cookie' in diagnostics[0]


def test_brotli_and_wrong_content_type_are_not_valid_html():
    for response, message in (
        (article_response(b'\xff\x90\x81', encoding='br'), 'Brotli'),
        (article_response('{"data": "not html"}', content_type='application/json'), '响应不是 HTML'),
    ):
        diagnostics = []
        body, _ = downloader._validate_article_response(response, 'wechat_http', diagnostics)
        assert not body and message in diagnostics[-1]


def test_explicit_deleted_page_remains_permanent(env, monkeypatch):
    _, path = env
    monkeypatch.setattr(downloader, 'coordinated_get', lambda *a, **kw: article_response('内容已被作者删除'))
    monkeypatch.setattr(downloader, 'in_backfill_network', lambda: True)
    result = downloader.download_single_article('https://mp.weixin.qq.com/s/offlineArticle000000001', path, '已删除')
    assert not result['success'] and result['is_permanent'] and result['error'] == '作者已删除'


def test_web_cookie_is_preserved_through_account_update(env):
    from backend import account_pool as ap
    pool = ap.AccountPool()
    cookie = 'wr_vid=offline; wr_skey=WEB-KEY; wr_rt=WEB-REFRESH; extra=preserved'
    data = {'vid': 'offline', 'accessToken': 'mobile-token', 'deviceId': 'offline-device',
            'deviceName': 'offline', 'nickname': '离线账号', 'cookie_str': cookie}
    created = pool.add_or_update(data)
    updated = pool.add_or_update({k: v for k, v in data.items() if k != 'cookie_str'})
    assert created['web_cookie'] == updated['web_cookie'] == cookie
    assert 'web_cookie' not in pool.list_accounts()[0]


def test_empty_forwarded_article_still_follows_original(env, monkeypatch):
    _, path = env
    target = 'https://mp.weixin.qq.com/s/forwardedOffline00000001'
    calls = []
    def fetch(url, **kwargs):
        calls.append(url)
        html = (f"<div id='js_content'></div><a id='js_share_source' data-url='{target}'></a>"
                if len(calls) == 1 else "<div id='js_content'><p>转载正文</p></div>")
        return html, url, ''
    monkeypatch.setattr(downloader, 'fetch_wechat_article_html', fetch)
    result = downloader.download_single_article('https://mp.weixin.qq.com/s/sourceOffline00000001', path, '转载')
    assert result['success'] and calls[-1] == target


def test_invalid_200_uses_valid_browser_only_for_normal_download(env, monkeypatch):
    valid = "<div id='js_content'><p>浏览器正文</p></div>"
    monkeypatch.setattr(downloader, 'coordinated_get', lambda *a, **kw: article_response('<div id="js_content"></div>'))
    monkeypatch.setattr(downloader, 'fetch_via_headless_browser', lambda *a, **kw: (valid, ''))
    html, _, error = downloader.fetch_wechat_article_html('https://mp.weixin.qq.com/s/sourceOffline00000001')
    assert html == valid and not error


def test_pending_legacy_bad_link_can_retry_with_corrected_short_url(env, monkeypatch):
    from backfill_fakes import FakeUpstream
    from test_history_backfill import make_service
    store, path = env
    review = 'MP_WXS_1_abcdefghijkl~mnopqr'
    wrong = 'https://mp.weixin.qq.com/s/abcdefghijkl~mnopqr'
    correct = 'https://mp.weixin.qq.com/s/abcdefghijkl_mnopqr'
    store.ensure_state(FAKEID, NICK)
    store.commit_scan_page(FAKEID, articles=[{'id': review, 'link': wrong, 'title': '旧队列', 'account_name': NICK}],
                           cursor={'end_confirmed': 1}, enqueue=True)
    called = []
    def get(url, **kwargs):
        called.append(url)
        return article_response('<html>参数错误</html>' if url == wrong else "<div id='js_content'><p>旧任务恢复正文</p></div>", url=url)
    monkeypatch.setattr(downloader, 'coordinated_get', get)
    monkeypatch.setattr(downloader, 'fetch_via_headless_browser', lambda *a, **k: pytest.fail('历史任务不应调用浏览器'))
    svc = make_service(store, path, FakeUpstream(0))
    svc._downloader = lambda url, folder, title, review_id='': downloader.download_single_article(url, folder, title, review_id=review_id)
    result = svc.submit_run(FAKEID, 10)
    run = svc.execute_run(store.get_run(result['task_id']))
    assert run['downloaded'] == 1 and store.count_articles(FAKEID)['downloaded'] == 1
    assert called == [wrong, correct]
