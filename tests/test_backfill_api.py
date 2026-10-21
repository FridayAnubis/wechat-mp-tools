"""管理 API 与真实 Worker 的离线集成；不导入会自动启动调度器的应用入口。"""
from concurrent.futures import ThreadPoolExecutor

import pytest
from flask import Flask

from backend import backfill_store as bs
from backend import history_backfill_api as api
from backfill_fakes import FakeUpstream
from test_history_backfill import make_service, FAKEID


@pytest.fixture
def api_env(tmp_path, monkeypatch):
    store = bs.BackfillStore(tmp_path / 'backfill.db')
    svc = make_service(store, tmp_path, FakeUpstream(65))
    monkeypatch.setattr(api, 'backfill_service', svc)
    app = Flask(__name__)
    app.register_blueprint(api.backfill_bp)
    yield app, svc, store
    store.close()


def test_api_async_idempotent_and_real_worker(api_env):
    app, svc, store = api_env
    with app.test_client() as client:
        ids = []
        for _ in range(5):
            r = client.post(f'/api/backfill/{FAKEID}/run-once', json={'scan_count': 10})
            assert r.status_code == 202
            ids.append(r.json['task_id'])
        assert len(set(ids)) == 1
        assert not svc.adapter.calls  # HTTP 请求只入队
        assert svc.tick()
        status = client.get(f'/api/backfill/{FAKEID}/status').json
        assert status['downloaded_count'] == 10 and not status['rss_subscribed']
        r = client.post(f'/api/backfill/{FAKEID}/run-once', json={})
        assert r.status_code == 202 and r.json['state'] == 'cooldown'
        assert not svc.tick()
        assert client.get('/api/backfill/accounts').json['accounts'][0]['fakeid'] == FAKEID
        rows = client.get(f'/api/backfill/{FAKEID}/articles?limit=3&offset=5').json
        assert len(rows['articles']) == 3 and rows['total'] == 10
        assert client.post(f'/api/backfill/{FAKEID}/pause', json={}).status_code == 200
        assert client.post(f'/api/backfill/{FAKEID}/run-once', json={}).status_code == 409
        assert client.post(f'/api/backfill/{FAKEID}/reset-cursor', json={}).status_code == 400
        assert client.post(f'/api/backfill/{FAKEID}/reset-cursor', json={'confirm': True}).status_code == 200
        assert store.count_articles(FAKEID)['downloaded'] == 10
        assert client.post(f'/api/backfill/{FAKEID}/resume', json={}).status_code == 200


@pytest.mark.parametrize('body', [[], 3, 'bad', {'scan_count': True}, {'scan_count': 4}, {'scan_count': 31}, {'scan_count': '10'}, {'scan_count': 1.5}])
def test_invalid_payloads(api_env, body):
    app, _, _ = api_env
    assert app.test_client().post(f'/api/backfill/{FAKEID}/run-once', json=body).status_code == 400


def test_auth_and_settings_validation(api_env, monkeypatch):
    app, _, _ = api_env
    client = app.test_client()
    monkeypatch.setenv('BACKFILL_ADMIN_TOKEN', 'offline-admin')
    url = f'/api/backfill/{FAKEID}/run-once'
    assert client.post(url, json={}).status_code == 401
    assert client.post(url, json={}, headers={'X-Admin-Token': 'wrong'}).status_code == 401
    headers = {'X-Admin-Token': 'offline-admin'}
    assert client.post(url, json={}, headers=headers).status_code == 202
    for value in (float('inf'), float('nan'), -1, 31, True):
        assert client.put('/api/backfill/settings', json={'backfill_scan_batch_size': value}, headers=headers).status_code == 400
    assert client.put('/api/backfill/settings', json={'unknown': 1}, headers=headers).status_code == 400
    assert client.put('/api/backfill/settings', json={'backfill_auto_enabled': True}, headers=headers).status_code == 200
    assert client.get('/api/backfill/unknown/status').status_code == 404
    assert client.get(f'/api/backfill/{FAKEID}/articles?limit=abc').status_code == 400
    assert client.post(url, data='oops', headers={**headers, 'Content-Type': 'application/json'}).status_code == 400
    assert client.post(url, data='oops', headers=headers).status_code == 415


def test_concurrent_submissions_create_one_run(api_env):
    app, _, store = api_env
    def submit(_):
        with app.test_client() as client:
            return client.post(f'/api/backfill/{FAKEID}/run-once', json={}).json['task_id']
    with ThreadPoolExecutor(max_workers=5) as pool:
        assert len(set(pool.map(submit, range(5)))) == 1
    assert len(store.list_runs(FAKEID)) == 1
