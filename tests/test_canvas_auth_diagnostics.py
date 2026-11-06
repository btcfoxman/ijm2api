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
