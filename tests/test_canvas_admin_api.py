"""Canvas administration cannot forge authorization or take over busy accounts."""
import importlib
from unittest.mock import AsyncMock, Mock

from fastapi.testclient import TestClient
import pytest

from app.catalog import normalize_request
from app.service import Service
from app.store import Store


@pytest.fixture
def context(tmp_path, monkeypatch):
    monkeypatch.setenv('IJM_API_KEY', 'synthetic-canvas-api')
    monkeypatch.setenv('IJM_ADMIN_TOKEN', 'synthetic-canvas-admin')
    monkeypatch.setenv('IJM_DATA_DIR', str(tmp_path / 'module'))
    main = importlib.import_module('app.main')
    store = Store(str(tmp_path / 'local.db'))
    service = Service(store)
    service.canvas_cli = Mock()
    auth = Mock()
    auth.busy.return_value = False
    login = Mock(active=None)
    login.close_account = AsyncMock()
    auto = Mock()
    auto.busy.return_value = False
    service.auto_login_busy = auto.busy
    service.canvas_auth = auth
    for key, value in {'store': store, 'service': service, 'canvas_auth': auth,
                       'browser_login': login, 'auto_login': auto,
                       'ADMIN_TOKEN': 'synthetic-canvas-admin', 'API_KEY': 'synthetic-canvas-api'}.items():
        monkeypatch.setattr(main, key, value)
    monkeypatch.setattr(main, 'clear_managed_profile', Mock())
    monkeypatch.setattr(main, 'stop_managed_browser', Mock())
    monkeypatch.setattr('app.browser_runtime.stop_managed_browser', Mock())
    account = store.upsert_account({'name': 'synthetic-cli', 'proxy_url': 'socks5://proxy.test:20001',
        'execution_mode': 'canvas_cli'})
    store.update_account(account['id'], {'status': 'active', 'enabled': True, 'managed_profile': True,
        'cdp_port': 19300 + account['id'], 'login_user_id': 'private-sdk-id',
        'watermark_ready': True, 'agreement_ready': True, 'cli_auth_status': 'ready', 'cli_user_id': 'private-sdk-id'})
    api = TestClient(main.app)  # No lifespan and no real browser/CLI subprocess.
    headers = {'X-Admin-Token': 'synthetic-canvas-admin'}
    try:
        yield main, api, headers, account['id']
    finally:
        api.close()
        service.stop()


@pytest.mark.parametrize('method,suffix', [('POST', ''), ('GET', ''), ('DELETE', ''), ('POST', '/browser')])
def test_authorization_endpoints_require_admin_not_a_caller_key(context, method, suffix, monkeypatch):
    main, api, headers, identifier = context
    endpoint = f'/api/accounts/{identifier}/cli-auth{suffix}'
    expected = {'account_id': identifier, 'state': 'authorizing', 'status': 'authorizing', 'message': ''}
    main.canvas_auth.start.return_value = expected
    main.canvas_auth.status.return_value = expected
    main.canvas_auth.cancel.return_value = expected
    main.service.browser_view = Mock(return_value=main.store.account(identifier, private=True))
    frame = {'image': 'synthetic-frame', 'width': 1280, 'height': 960}
    monkeypatch.setattr(main, '_frame', Mock(return_value=frame))
    assert api.request(method, endpoint).status_code == 401
    assert api.request(method, endpoint, headers={'X-API-Key': 'synthetic-canvas-api'}).status_code == 401
    main.canvas_auth.start.assert_not_called()
    main.canvas_auth.status.assert_not_called()
    main.canvas_auth.cancel.assert_not_called()
    main.canvas_auth.open_assist.assert_not_called()
    response = api.request(method, endpoint, headers=headers)
    assert response.status_code == 200
    assert response.json() == (frame if suffix else expected)
    assert 'private-sdk-id' not in response.text


def test_force_reauthorization_is_explicit_and_cancellation_only_cancels_local_wait(context):
    main, api, headers, identifier = context
    main.canvas_auth.start.return_value = {'state': 'queued'}
    main.canvas_auth.cancel.return_value = {'state': 'cancelled'}
    endpoint = f'/api/accounts/{identifier}/cli-auth'
    assert api.post(endpoint, headers=headers, json={'force': True}).status_code == 200
    main.canvas_auth.start.assert_called_once_with(identifier, force=True)
    assert api.delete(endpoint, headers=headers).status_code == 200
    main.canvas_auth.cancel.assert_called_once_with(identifier)
    main.service.canvas_cli.invalidate.assert_not_called()
    main.browser_login.close_account.assert_not_called()


@pytest.mark.parametrize('field,value', [('cli_auth_status', 'ready'), ('cli_auth_error', ''),
    ('cli_user_id', 'forged-cli-id'), ('login_user_id', 'forged-sdk-id'), ('watermark_ready', True)])
@pytest.mark.parametrize('method', ['PATCH', 'POST'])
def test_setup_and_cli_identity_cannot_be_forged_in_account_payload(context, field, value, method):
    main, api, headers, identifier = context
    before = main.store.account(identifier, private=True)
    endpoint = f'/api/accounts/{identifier}' if method == 'PATCH' else '/api/accounts'
    body = {field: value, 'name': before['name']}
    response = api.request(method, endpoint, headers=headers, json=body)
    assert response.status_code == 422
    assert main.store.account(identifier, private=True) == before
    main.browser_login.close_account.assert_not_called()


@pytest.mark.parametrize('busy', ['authorization', 'upstream-task'])
@pytest.mark.parametrize('operation', ['proxy', 'execution-mode', 'reset', 'delete'])
def test_busy_accounts_are_rejected_before_browser_or_credential_cleanup(context, busy, operation):
    main, api, headers, identifier = context
    if busy == 'authorization':
        main.canvas_auth.busy.return_value = True
    else:
        body = {'model': 'sd-2-0-mini', 'prompt': 'Synthetic landscape', 'duration': 4}
        main.store.create_task('in-flight', body, normalize_request(body))
        main.store.update_task('in-flight', {'account_id': identifier, 'status': 'running', 'run_id': 'accepted-upstream-id'})
        main.service._active[identifier] = 1
    before = main.store.account(identifier, private=True)
    if operation == 'proxy':
        response = api.patch(f'/api/accounts/{identifier}', headers=headers,
            json={'proxy_url': 'socks5://proxy.test:20002'})
    elif operation == 'execution-mode':
        response = api.patch(f'/api/accounts/{identifier}', headers=headers, json={'execution_mode': 'browser'})
    elif operation == 'reset':
        response = api.post(f'/api/accounts/{identifier}/profile/reset', headers=headers, json={})
    else:
        response = api.delete(f'/api/accounts/{identifier}', headers=headers)
    assert response.status_code == 409
    assert main.store.account(identifier, private=True) == before
    main.browser_login.close_account.assert_not_called()
    main.clear_managed_profile.assert_not_called()
    main.stop_managed_browser.assert_not_called()
    main.service.canvas_cli.invalidate.assert_not_called()


def test_cli_operator_switch_is_admin_only_and_does_not_open_browsers(context):
    main, api, headers, identifier = context
    main.service.browser_runtime.acquire = Mock(side_effect=AssertionError('setting opened a browser'))
    assert api.patch('/api/settings', json={'canvas_cli_enabled': True}).status_code == 401
    assert api.patch('/api/settings', headers={'X-API-Key': 'synthetic-canvas-api'},
                     json={'canvas_cli_enabled': True}).status_code == 401
    assert api.get('/api/settings', headers=headers).json()['canvas_cli_enabled'] is False
    response = api.patch('/api/settings', headers=headers, json={'canvas_cli_enabled': True})
    assert response.status_code == 200 and response.json()['canvas_cli_enabled'] is True
    assert api.get('/api/settings', headers=headers).json()['canvas_cli_enabled'] is True
    main.service.browser_runtime.acquire.assert_not_called()


def test_authorization_assist_uses_account_view_without_restarting_login_or_changing_proxy(context, monkeypatch):
    main, api, headers, identifier = context
    account = main.store.account(identifier, private=True)
    main.service.browser_view = Mock(return_value=account)
    main.service.open_browser_view = Mock(side_effect=AssertionError('route must use authorization-owned view'))
    frame = {'image': 'synthetic-frame', 'width': 1280, 'height': 960}
    monkeypatch.setattr(main, '_frame', Mock(return_value=frame))
    monkeypatch.setattr(main, 'CDPBrowser', Mock())
    response = api.post(f'/api/accounts/{identifier}/cli-auth/browser', headers=headers)
    assert response.status_code == 200 and response.json() == frame
    main.canvas_auth.open_assist.assert_called_once_with(identifier)
    main.service.browser_view.assert_called_once_with(identifier)
    main.CDPBrowser.assert_called_once_with(account['cdp_port'])
    main.browser_login.start.assert_not_called()
    main.auto_login.start.assert_not_called()
    assert main.store.account(identifier, private=True)['proxy_url'] == 'socks5://proxy.test:20001'
    main.service.canvas_cli.run.assert_not_called()


def test_missing_accounts_fail_before_starting_any_authorization(context):
    main, api, headers, identifier = context
    endpoint = f'/api/accounts/{identifier + 100}/cli-auth'
    for method in ['GET', 'POST', 'DELETE']:
        assert api.request(method, endpoint, headers=headers).status_code == 404
    main.canvas_auth.start.assert_not_called()
    main.canvas_auth.status.assert_not_called()
    main.canvas_auth.cancel.assert_not_called()
