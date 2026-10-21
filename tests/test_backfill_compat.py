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
