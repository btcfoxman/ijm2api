"""Deterministic shared-desktop menu ownership and private-data-free diagnosis."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
import json
import threading
import time
from unittest.mock import Mock, patch

import pytest

from app import account_automation as module
from app.account_automation import AccountAutomation, AutomationError, _IDENTITY, _VIEW, _WATERMARK, _SIGN_OUT
from app.browser import BrowserError


def automation(identifier):
    browser = Mock()
    browser.command_budget.side_effect = lambda *args, **kwargs: nullcontext()
    browser.evaluate.return_value = {'stage': 'waiting'}
    return AccountAutomation({'id': identifier, 'cdp_port': 20000 + identifier,
                              'proxy_url': 'socks5://proxy.example.test:10000'}, browser=browser)


def test_background_views_do_not_take_focus_or_wait_for_another_accounts_menu():
    instance = automation(1)
    with module._UI_LOCK:
        with ThreadPoolExecutor(max_workers=1) as pool:
            def read():
                return instance._view(), instance._watermark_action()
            result = pool.submit(read).result(timeout=.5)
    assert result == ({'stage': 'waiting'}, {'stage': 'waiting'})
    instance.browser.command.assert_not_called()
    instance.browser.post.assert_not_called()


def test_full_watermark_transactions_do_not_interleave_between_accounts():
    first, second = automation(1), automation(2)
    first_opened, release_first, second_started = threading.Event(), threading.Event(), threading.Event()
    operations = []
    def install(instance, name):
        state = {'stage': 'avatar', 'checked': False, 'saved': False}
        def action(command='view'):
            if command != 'view':
                operations.append((name, command))
            if command == 'open_avatar':
                state['stage'] = 'menu'
                if name == 'first':
                    first_opened.set()
                    assert release_first.wait(2)
                else:
                    second_started.set()
            elif command == 'open_setting':
                state['stage'] = 'modal'
            elif command == 'toggle':
                state['checked'] = True
            elif command == 'save':
                state.update(stage='waiting', saved=True)
            return state.copy()
        instance._watermark_action = action
        instance._settings = lambda: {'close_watermark': state['saved']}
    install(first, 'first')
    install(second, 'second')
    with patch.object(module.time, 'sleep', side_effect=lambda _: threading.Event().wait(.001)):
        with ThreadPoolExecutor(max_workers=2) as pool:
            a = pool.submit(first._enable_watermark)
            assert first_opened.wait(1)
            b = pool.submit(second._enable_watermark)
            try:
                assert not second_started.wait(.05)
                assert second._view() == {'stage': 'waiting'}
            finally:
                release_first.set()
            a.result(timeout=2)
            b.result(timeout=2)
    assert [name for name, _ in operations] == ['first'] * 4 + ['second'] * 4


def test_expired_credential_waiter_and_menu_action_never_send_input_or_take_focus():
    instance = automation(1)
    instance._login_deadline = time.monotonic() + .05
    with patch.object(instance, '_fill') as fill:
        with module._UI_LOCK:
            with ThreadPoolExecutor(max_workers=1) as pool:
                assert pool.submit(instance._action, 'password', 'fixture', 'fixture').result(timeout=.5) is False
        instance._login_deadline = time.monotonic() - 1
        assert instance._watermark_action('open_avatar') == {'stage': 'waiting'}
    fill.assert_not_called()
    instance.browser.command.assert_not_called()
    assert instance._ui_action_expired is True


def test_failure_is_snapshotted_while_menu_is_owned_and_lock_is_released():
    instance = automation(1)
    failure = AutomationError('setup_required', 'Dreamina watermark preference was not confirmed')
    def diagnose(error):
        assert error is failure
        assert module._UI_LOCK._is_owned()
        return {'reason': 'watermark_unconfirmed'}
    with (patch.object(instance, '_enable_watermark_locked', side_effect=failure),
          patch.object(instance, 'failure_diagnostic', side_effect=diagnose)):
        with pytest.raises(AutomationError) as caught:
            instance._enable_watermark()
    assert caught.value.safe_diagnostic == {'reason': 'watermark_unconfirmed'}
    with ThreadPoolExecutor(max_workers=1) as pool:
        assert pool.submit(lambda: module._UI_LOCK.acquire(timeout=.1)).result(timeout=.5)
        # Release in the owning worker thread.
        pool.submit(module._UI_LOCK.release).result(timeout=.5)


def test_failure_diagnostics_only_keep_enums_booleans_and_counts_without_sdk_or_input_reads():
    instance = automation(1)
    secret = 'private-uid-cookie-email-password'
    def evaluate(expression):
        assert expression != _IDENTITY
        if expression == _VIEW:
            return {'stage': 'waiting', 'email': secret}
        if expression == _WATERMARK.replace('__ACTION__', json.dumps('view')):
            return {'stage': 'modal', 'checked': False, 'user_id': secret}
        return {'modal_count': 1, 'save_button_count': 1, 'dom': secret, 'cookies': secret}
    instance.browser.evaluate.side_effect = evaluate
    instance._last_settings = {'close_watermark': False, 'aigc_compliance_confirmed': False, 'cookies': secret}
    result = instance.failure_diagnostic(AutomationError('setup_required', 'Dreamina watermark preference was not confirmed'))
    assert result['reason'] == 'watermark_unconfirmed'
    assert result['watermark_stage'] == 'modal' and result['watermark_checked'] is False
    assert result['modal_count'] == 1 and result['close_watermark'] is False
    assert secret not in json.dumps(result)
    assert 'user_id' not in result and 'cookies' not in result
    instance.browser.post.assert_not_called()
    instance.browser.command.assert_not_called()


def test_transport_identity_failure_cannot_keep_a_stale_verified_observation():
    instance = automation(1)
    instance._identity_observation = 'identity_verified'
    instance.browser.evaluate.side_effect = BrowserError('private transport detail')
    with pytest.raises(BrowserError):
        instance._identity()
    assert instance._identity_observation == 'identity_read_unavailable'


@pytest.mark.parametrize(('message', 'reason'), [
    ('Dreamina login did not finish before the deadline', 'login_deadline'),
    ('Dreamina login result was not confirmed', 'login_result_unconfirmed'),
    ('Dreamina did not accept the login', 'login_rejected'),
    ('Dreamina requires manual page review', 'unknown_page_review'),
])
def test_login_failures_keep_fixed_reasons_instead_of_collapsing_into_settings(message, reason):
    instance = automation(1)
    assert instance.failure_diagnostic(AutomationError('login_required', message))['reason'] == reason


def test_signout_cannot_mutate_after_its_read_crosses_the_deadline():
    instance = automation(1)
    clock = [10.]
    def evaluate(expression):
        if expression == _VIEW:
            return {'stage': 'waiting'}
        if expression == _WATERMARK.replace('__ACTION__', json.dumps('view')):
            clock[0] = 12.
            return {'stage': 'menu'}
        assert expression != _SIGN_OUT, 'expired menu must not send Sign out'
        return {}
    instance.browser.evaluate.side_effect = evaluate
    with patch.object(module.time, 'monotonic', side_effect=lambda: clock[0]):
        with pytest.raises(AutomationError, match='sign-out did not finish'):
            with instance._menu_transaction(11.) as deadline:
                instance._restart_unbound_session_locked(deadline)
    assert all(call.args[0] != _SIGN_OUT for call in instance.browser.evaluate.call_args_list)
    instance.browser.command.assert_not_called()


def test_menu_loop_uses_the_remaining_login_deadline():
    instance = automation(1)
    instance._login_deadline = 11.
    with (patch.object(module.time, 'monotonic', return_value=10.),
          patch.object(instance, '_enable_watermark_locked') as run):
        instance._enable_watermark()
    run.assert_called_once_with(11.)


def test_cold_browser_start_cannot_steal_focus_from_a_menu_transaction(tmp_path):
    from app import browser as transport
    assert transport._DESKTOP_UI_LOCK is module._UI_LOCK
    instance = automation(1)
    entered = threading.Event()
    checked = threading.Event()
    def start():
        entered.set()
        return transport.start_managed_browser(instance.account, chrome='fixture-chrome', profiles=tmp_path)
    with (patch.object(transport, 'browser_alive', side_effect=lambda *a, **k: checked.set() or True),
          patch.object(transport, 'ensure_resource_timing_capture'),
          patch.object(transport.subprocess, 'Popen') as spawn):
        with ThreadPoolExecutor(max_workers=1) as pool:
            with module._UI_LOCK:
                result = pool.submit(start)
                assert entered.wait(.5)
                assert not checked.wait(.05)
            assert result.result(timeout=.5) == 19301
        spawn.assert_not_called()
