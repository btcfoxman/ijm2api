"""Administrator import responses keep credentials private and queue login only."""
import gc
import importlib
import json
from unittest.mock import Mock

from fastapi.testclient import TestClient


def test_batch_import_authentication_privacy_and_login_ownership(tmp_path, monkeypatch):
    monkeypatch.setenv('IJM_API_KEY', 'synthetic-api')
    monkeypatch.setenv('IJM_ADMIN_TOKEN', 'synthetic-admin')
    monkeypatch.setenv('IJM_DATA_DIR', str(tmp_path))
    main = importlib.import_module('app.main')
    main.auto_login._pool.shutdown()
    main.auto_login._pool = Mock()
    api = TestClient(main.app)  # No lifespan: no browser or generation background work.
    body = {'text': 'example@example.test|Synthetic Password|socks5://proxy.test:20003'}
    assert api.post('/api/accounts/batch-import', json=body).status_code == 401
    assert main.store.accounts() == []
    headers = {'X-Admin-Token': 'synthetic-admin'}
    assert api.post('/api/accounts', headers=headers, json={'name': 'unsafe', 'login_password': 'private'}).status_code == 422
    assert api.post('/api/accounts', headers=headers, json={'name': 'unsafe', 'login_user_id': 'forged-user'}).status_code == 422
    response = api.post('/api/accounts/batch-import', json=body, headers=headers)
    assert response.status_code == 200
    value = response.json()
    assert value['count'] == value['login_started_count'] == 1
    identifier = value['results'][0]['account_id']
    listed = api.get('/api/accounts', headers=headers).json()
    jobs = api.get('/api/accounts/login-results', headers=headers).json()['jobs']
    assert listed[0]['status'] == jobs[0]['status'] == 'logging_in'
    assert jobs[0]['phase'] == 'queued'
    serialized = json.dumps({'import': value, 'accounts': listed, 'jobs': jobs})
    assert 'Synthetic Password' not in serialized and 'example@example.test' not in serialized
    assert 'login_password' not in serialized
    assert api.post(f'/api/accounts/{identifier}/auto-login', headers=headers).status_code == 202
    assert main.auto_login._pool.submit.call_count == 1
    assert api.patch(f'/api/accounts/{identifier}', headers=headers, json={'enabled': True}).status_code == 409
    assert api.delete(f'/api/accounts/{identifier}', headers=headers).status_code == 409
    assert api.post('/api/accounts', headers=headers, json={
        'name': listed[0]['name'], 'cdp_port': 9999}).status_code == 409
    main.auto_login._pending.clear()
    assert api.patch(f'/api/accounts/{identifier}', headers=headers, json={'watermark_ready': True}).status_code == 422
    assert api.patch(f'/api/accounts/{identifier}', headers=headers, json={'login_user_id': 'forged-user'}).status_code == 422
    main.store.update_account(identifier, {'login_user_id': 'synthetic-private-id'})
    assert 'login_user_id' not in api.get('/api/accounts', headers=headers).text
    assert 'synthetic-private-id' not in api.get('/api/accounts', headers=headers).text
    main.store.update_account(identifier, {'status': 'setup_required', 'login_stage': 'setup_required', 'login_enable_after': False})
    assert api.patch(f'/api/accounts/{identifier}', headers=headers, json={'enabled': True}).status_code == 200
    assert main.store.account(identifier, private=True)['login_enable_after'] is None
    assert api.patch(f'/api/accounts/{identifier}', headers=headers, json={'enabled': False}).status_code == 200
    assert main.store.account(identifier, private=True)['login_enable_after'] == 0
    assert api.post(f'/api/accounts/{identifier}/auto-login', headers=headers).status_code == 202
    assert main.auto_login._pool.submit.call_args.args[2] is False
    main.auto_login._pending.clear()
    main.store.update_account(identifier, {'status': 'disabled', 'enabled': False, 'login_stage': 'ready'})
    monkeypatch.setattr(main, 'clear_managed_profile', lambda *args: None)
    response = api.post(f'/api/accounts/{identifier}/profile/reset', headers=headers, json={})
    assert response.status_code == 200
    assert main.store.account(identifier, private=True)['login_user_id'] == ''
    assert 'login_user_id' not in response.json()
    main.auto_login.close()
    main.service.stop()
    gc.collect()
