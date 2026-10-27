"""Login jobs must never borrow an active generation account or publish secrets."""
import gc
import json
from pathlib import Path
import tempfile
import threading
from unittest.mock import Mock, patch

import pytest

from app.auto_login import AutoLoginManager
from app.service import Service
from app.store import Store


@pytest.fixture
def context():
    with tempfile.TemporaryDirectory() as directory:
        store = Store(str(Path(directory) / 'data.db'))
        service = Service(store)
        manager = AutoLoginManager(store, service)
        yield store, service, manager
        manager.close()
        service.stop()
        gc.collect()


def account(store, index=1):
    return store.upsert_account({'name': f'synthetic-{index}', 'proxy_url': f'socks5://proxy.test:{20000 + index}',
        'login_email': f'person{index}@example.test', 'login_password': 'Synthetic private password'})


def queued(manager):
    pool = manager._pool
    pool.shutdown()
    manager._pool = Mock()
    return manager._pool


def test_login_is_deduplicated_and_excluded_from_video_reservations(context):
    store, service, manager = context
    item = account(store)
    pool = queued(manager)
    first = manager.start(item['id'])
    second = manager.start(item['id'])
    assert first['status'] == second['status'] == 'logging_in'
    assert not first['enabled'] and manager.busy(item['id'])
    assert service._reserve_account() is None
    assert pool.submit.call_count == 1
    assert 'Synthetic private password' not in json.dumps(first)
    assert 'login_email' not in first and 'login_password' not in first


@pytest.mark.parametrize('busy', ['running-task', 'reserved-task', 'manual-browser', 'leased-browser'])
def test_in_use_accounts_reject_profile_takeover_before_queueing(context, busy):
    store, service, manager = context
    item = account(store)
    pool = queued(manager)
    if busy == 'running-task':
        request = {'model': 'sd-2-0-mini', 'prompt': 'synthetic landscape', 'duration': 4}
        from app.catalog import normalize_request
        store.create_task('ijm_synthetic', request, normalize_request(request))
        store.update_task('ijm_synthetic', {'account_id': item['id'], 'status': 'running'})
    elif busy == 'reserved-task':
        service._active[item['id']] = 1
    elif busy == 'manual-browser':
        manager.manual_active = lambda: {'account_id': item['id']}
    else:
        service.browser_runtime.assert_idle = Mock(side_effect=ValueError('browser in use'))
    with pytest.raises(ValueError):
        manager.start(item['id'])
    pool.submit.assert_not_called()
    assert store.account(item['id'])['status'] == 'pending'


def test_success_requires_identity_preferences_and_credit_read(context):
    store, service, manager = context
    item = account(store)
    pool = queued(manager)
    manager.start(item['id'])
    fn, identifier, enable = pool.submit.call_args.args
    ready = {'identity_verified': True, 'watermark_ready': True, 'agreement_ready': True,
             'identity': {'user_id': 'synthetic-user-1'}}
    client = Mock()
    client.account_state.return_value = {'available_balance': 20, 'buckets': {'gift_credit': 20}}
    with patch('app.auto_login.start_managed_browser', return_value=19301), \
         patch('app.auto_login.AccountAutomation') as automation, \
         patch('app.auto_login.DreaminaClient', return_value=client):
        automation.return_value.run.return_value = ready
        fn(identifier, enable)
    saved = store.account(item['id'])
    assert saved['enabled'] and saved['status'] == 'active' and saved['login_stage'] == 'ready'
    assert saved['watermark_ready'] and saved['agreement_ready'] and saved['balance'] == 20
    assert store.account(item['id'], private=True)['login_user_id'] == 'synthetic-user-1'
    assert 'login_user_id' not in saved and 'synthetic-user-1' not in json.dumps(saved)
    assert not manager.busy(item['id'])
    assert client.submit.call_count == 0


@pytest.mark.parametrize('identity', [None, {}, {'user_id': ''}, {'user_id': '0'}, {'user_id': True},
                                     {'user_id': 123}, {'user_id': ' user-1'}, {'user_id': 'user\n1'},
                                     {'user_id': 'x' * 257}])
def test_missing_or_invalid_sdk_identity_never_enables_the_login(context, identity):
    store, service, manager = context
    item = account(store)
    pool = queued(manager)
    manager.start(item['id'])
    ready = {'identity_verified': True, 'watermark_ready': True, 'agreement_ready': True, 'identity': identity}
    with patch('app.auto_login.start_managed_browser', return_value=19301), \
         patch('app.auto_login.AccountAutomation') as automation, patch('app.auto_login.DreaminaClient') as client:
        automation.return_value.run.return_value = ready
        fn, identifier, enable = pool.submit.call_args.args
        fn(identifier, enable)
    saved = store.account(item['id'])
    assert saved['status'] == 'login_required' and not saved['enabled']
    assert store.account(item['id'], private=True)['login_user_id'] == ''
    client.assert_not_called()


@pytest.mark.parametrize('missing', ['identity_verified', 'watermark_ready', 'agreement_ready'])
def test_missing_setup_state_never_marks_account_active(context, missing):
    store, service, manager = context
    item = account(store)
    pool = queued(manager)
    manager.start(item['id'])
    ready = {'identity_verified': True, 'watermark_ready': True, 'agreement_ready': True, missing: False}
    with patch('app.auto_login.start_managed_browser', return_value=19301), \
         patch('app.auto_login.AccountAutomation') as automation, patch('app.auto_login.DreaminaClient') as client:
        automation.return_value.run.return_value = ready
        fn, identifier, enable = pool.submit.call_args.args
        fn(identifier, enable)
    assert store.account(item['id'])['status'] == 'setup_required'
    assert not store.account(item['id'])['enabled']
    client.assert_not_called()


def test_raw_browser_failure_never_exposes_credentials(context):
    store, service, manager = context
    item = account(store)
    pool = queued(manager)
    manager.start(item['id'])
    with patch('app.auto_login.start_managed_browser', side_effect=RuntimeError('password=private; cookie=private')):
        fn, identifier, enable = pool.submit.call_args.args
        fn(identifier, enable)
    saved = store.account(item['id'])
    assert saved['status'] == 'login_failed'
    assert 'private' not in json.dumps(saved)


def test_only_two_login_jobs_execute_concurrently(context):
    store, service, manager = context
    entered = threading.Event()
    release = threading.Event()
    guard = threading.Lock()
    running, maximum, calls = 0, 0, 0

    def run(identifier, enabled):
        nonlocal running, maximum, calls
        with guard:
            running += 1
            calls += 1
            maximum = max(maximum, running)
            if running == 2:
                entered.set()
        release.wait(3)
        with guard:
            running -= 1
            manager._pending.discard(identifier)

    manager._run = run
    for index in range(1, 4):
        manager.start(account(store, index)['id'])
    assert entered.wait(2)
    with guard:
        assert calls == maximum == 2
    release.set()
    manager._pool.shutdown(wait=True)
    assert maximum == 2 and calls == 3


def test_restart_marks_only_interrupted_automatic_login_for_retry(context):
    store, service, manager = context
    first, second = account(store, 1), account(store, 2)
    store.update_account(first['id'], {'status': 'logging_in', 'login_stage': 'password'})
    store.update_account(second['id'], {'status': 'logging_in', 'login_stage': 'manual_login'})
    manager.recover_interrupted()
    assert store.account(first['id'])['status'] == 'login_required'
    assert store.account(second['id'])['status'] == 'logging_in'


def test_disabled_account_retains_disabled_intent_across_restart_and_retry(context):
    store, service, manager = context
    item = account(store)
    store.update_account(item['id'], {'status': 'disabled', 'enabled': False})
    pool = queued(manager)
    manager.start(item['id'])
    assert pool.submit.call_args.args[2] is False
    assert store.account(item['id'], private=True)['login_enable_after'] == 0
    manager._pending.clear()
    manager.recover_interrupted()
    manager.start(item['id'])
    assert pool.submit.call_args.args[2] is False


def test_shutdown_cancels_queued_jobs_and_worker_does_not_open_a_browser(context):
    store, service, manager = context
    item = account(store)
    pool = queued(manager)
    manager.start(item['id'])
    manager.close()
    pool.shutdown.assert_called_with(wait=False, cancel_futures=True)
    with patch('app.auto_login.start_managed_browser') as browser:
        manager._run(item['id'], True)
    browser.assert_not_called()


@pytest.mark.parametrize('status', ['challenge_required', 'verification_required', 'login_failed', 'setup_required'])
def test_failed_disabled_login_retry_preserves_disabled_intent(context, status):
    store, service, manager = context
    item = account(store)
    store.update_account(item['id'], {'status': 'disabled', 'enabled': False})
    pool = queued(manager)
    manager.start(item['id'])
    manager._pending.clear()
    store.update_account(item['id'], {'status': status, 'login_stage': status})
    manager.start(item['id'])
    assert pool.submit.call_args.args[2] is False


def test_balance_check_cannot_promote_unprepared_credentials(context):
    store, service, manager = context
    item = account(store)
    state = {'available_balance': 100, 'buckets': {}}
    with patch('app.service.browser_alive', return_value=True), patch('app.service.DreaminaClient') as client:
        client.return_value.account_state.return_value = state
        client.return_value.browser.project_id.return_value = ''
        saved = service._check_account_live(store.account(item['id'], private=True))
    assert saved['status'] == 'setup_required' and not saved['enabled']
    store.update_account(item['id'], {'enabled': True, 'status': 'active'})
    assert service._reserve_account() is None
