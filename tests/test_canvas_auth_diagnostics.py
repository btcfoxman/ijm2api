"""Authorization diagnostics reveal bounded categories without changing outcomes."""
import logging
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.canvas_auth import CanvasAuthManager, _auth_diagnostic
from app.canvas_cli import CanvasCLIError


def manager():
    instance = CanvasAuthManager.__new__(CanvasAuthManager)
    instance._jobs = {60: {'cancel': threading.Event()}}
    instance._closed = False
    instance._publish = Mock()
    instance._accept_identity = Mock()
    return instance


@pytest.mark.parametrize('failure_phase', ['auth_wait', 'identity'])
def test_terminal_failure_retains_phase_without_sensitive_details(caplog, failure_phase):
    caplog.set_level(logging.WARNING, logger='app.canvas_auth')
    instance = manager()
    client = Mock()
    error = CanvasCLIError('cli.transport_failed', 'PRIVATE https://secret.invalid/?device=TOKEN UID-email@example.test')
    if failure_phase == 'auth_wait':
        client.auth_wait.side_effect = error
    else:
        client.auth_wait.return_value = {'status': 'authenticated'}
        client.identity.side_effect = error
    browser = Mock()
    browser.step.return_value = 'confirmed'
    instance._await_authorization(60, {}, client, 'PRIVATE_DEVICE_CODE', time.monotonic() + 10, browser)
    assert instance._publish.call_args.args[1] == 'failed'
    instance._accept_identity.assert_not_called()
    assert 'phase=' + failure_phase in caplog.text
    assert 'code=cli.transport_failed' in caplog.text and 'confirmed=True' in caplog.text
    assert all(value not in caplog.text for value in ('PRIVATE', 'TOKEN', 'secret.invalid', 'example.test'))


def test_expired_wait_reports_no_confirmation_without_dispatching(caplog):
    caplog.set_level(logging.WARNING, logger='app.canvas_auth')
    instance = manager()
    client = Mock()
    browser = Mock()
    instance._await_authorization(60, {}, client, 'SECRET_DEVICE', time.monotonic() - 1, browser)
    assert instance._publish.call_args.args[1] == 'expired'
    client.auth_wait.assert_not_called()
    browser.step.assert_not_called()
    assert 'phase=authorization_deadline' in caplog.text and 'confirmed=False' in caplog.text
    assert 'SECRET_DEVICE' not in caplog.text


def test_unknown_error_metadata_and_phase_are_never_logged_verbatim(caplog):
    caplog.set_level(logging.WARNING, logger='app.canvas_auth')
    error = type('PRIVATE_CLASS', (Exception,), {'code': 'PRIVATE_CODE'})('PRIVATE_MESSAGE')
    _auth_diagnostic(60, 'PRIVATE_PHASE', error)
    assert 'phase=flow code=other error_type=other' in caplog.text
    assert 'PRIVATE' not in caplog.text


@pytest.mark.parametrize('code', ['cli.authorization_exchange_failed',
    'CLI_AUTHORIZATION_EXCHANGE_FAILED', 'cli.' + 'a' * 76])
def test_new_official_symbolic_code_is_retained_without_exception_payload(caplog, code):
    caplog.set_level(logging.WARNING, logger='app.canvas_auth')
    error = CanvasCLIError(code, 'PRIVATE_MESSAGE https://secret.invalid/?nonce=TOKEN user@example.test',
                           details={'code': 'PRIVATE_DETAILS', 'deviceCode': 'PRIVATE_DEVICE_CODE'})
    _auth_diagnostic(60, 'auth_wait', error, confirmed=True, manual=False)
    assert 'code=' + code + ' error_type=CanvasCLIError confirmed=True manual=False' in caplog.text
    assert all(secret not in caplog.text for secret in ('PRIVATE', 'secret.invalid', 'TOKEN', 'example.test'))


@pytest.mark.parametrize('code', ['https://secret.invalid/?token=PRIVATE', 'user@example.test',
    'TOKEN_WITHOUT_NAMESPACE', 'cli.authorization_exchange_failed?token=PRIVATE',
    'cli.authorization exchange_failed', 'cli.authorization_exchange_failed\nPRIVATE',
    'cli.authorization_exchange_failed\x00PRIVATE', 'cli.授权失败', 'cli.exchange_échec',
    'Cli.authorization_exchange_failed', 'CLI_authorization_exchange_failed',
    'cli.authorization__exchange_failed', 'cli.authorization_exchange_failed_',
    'cli.' + 'a' * 77, 'CLI_' + 'A' * 77, None, 42, {'code': 'cli.exchange_failed'}])
def test_non_symbolic_or_unbounded_structured_codes_remain_other(caplog, code):
    caplog.set_level(logging.WARNING, logger='app.canvas_auth')
    error = CanvasCLIError(code, 'PRIVATE_MESSAGE', details={'code': 'cli.authorization_exchange_failed'})
    _auth_diagnostic(60, 'auth_wait', error)
    assert 'phase=auth_wait code=other error_type=CanvasCLIError' in caplog.text
    assert 'PRIVATE' not in caplog.text and 'cli.authorization_exchange_failed' not in caplog.text


def test_existing_known_code_normalization_and_no_error_diagnostic_are_preserved(caplog):
    caplog.set_level(logging.WARNING, logger='app.canvas_auth')
    _auth_diagnostic(60, 'auth_wait', CanvasCLIError('CLI_COMMAND_TIMEOUT', 'PRIVATE'))
    assert 'code=cli_command_timeout' in caplog.text
    caplog.clear()
    _auth_diagnostic(60, 'authorization_deadline')
    assert 'code=none error_type=none' in caplog.text


@pytest.mark.parametrize('exit_code', [-1, 0, 11, 255])
@pytest.mark.parametrize('error_class', ['invalid_input', 'retryable_failure', 'internal_failure', 'authentication_failure', 'service_error'])
def test_exit_code_and_fixed_error_class_retain_only_bounded_facts(caplog, exit_code, error_class):
    caplog.set_level(logging.WARNING, logger='app.canvas_auth')
    error = CanvasCLIError('cli.authorization_exchange_failed', 'PRIVATE_MESSAGE', exit_code=exit_code,
        details={'error': {'class': error_class, 'message': 'PRIVATE_ERROR', 'nonce': 'PRIVATE_NONCE'},
                 'deviceCode': 'PRIVATE_DEVICE', 'url': 'https://secret.invalid/?token=PRIVATE'})
    _auth_diagnostic(60, 'auth_wait', error)
    assert 'exit_code=' + str(exit_code) + ' error_class=' + error_class in caplog.text
    assert 'PRIVATE' not in caplog.text and 'secret.invalid' not in caplog.text


@pytest.mark.parametrize('exit_code', [-2, 256, True, 1.5, 'PRIVATE_EXIT'])
@pytest.mark.parametrize('details', [{'error': {'class': 'PRIVATE_CLASS', 'message': 'PRIVATE_MESSAGE'}},
    {'error': {'class': ['PRIVATE_CLASS']}}, {'error': 'PRIVATE_ERROR'}, 'PRIVATE_DETAILS'])
def test_invalid_exit_code_or_error_payload_is_never_logged_verbatim(caplog, exit_code, details):
    caplog.set_level(logging.WARNING, logger='app.canvas_auth')
    error = CanvasCLIError('CLI_COMMAND_FAILED', 'PRIVATE_MESSAGE', exit_code=exit_code, details=details)
    _auth_diagnostic(60, 'auth_wait', error)
    assert 'exit_code=none' in caplog.text
    assert 'error_class=other' in caplog.text or 'error_class=none' in caplog.text
    assert 'PRIVATE' not in caplog.text
