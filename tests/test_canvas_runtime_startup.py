"""A transient local version/schema failure cannot disable CLI until restart."""
import hashlib
import io
import json
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

import pytest

from app.canvas_cli import CanvasCLI, CanvasCLIError
from app.catalog import normalize_request
from app.service import Service
from app.store import Store


RUNTIME = {'available': True, 'version': '1.2.0', 'distribution': 'global',
           'proxy_transport': 'graftcp', 'proxy_version': '0.8.3'}


def invalid_local_response(command='schema'):
    return CanvasCLIError('CLI_INVALID_RESPONSE', 'Canvas CLI returned no valid JSON result',
        exit_code=1, details={'transport_diagnostic': {'command_pair': [command],
            'stdout_bytes': 0, 'stdout_sha256': hashlib.sha256(b'').hexdigest(),
            'stderr_bytes': 5, 'stderr_sha256': hashlib.sha256(b'noise').hexdigest(),
            'stderr': 'private token=private-token https://private.test/signed?secret=value'}})


def start_service(tmp_path, results):
    store = Store(str(tmp_path / 'startup.db'))
    store.create_pending_account('original-account', 'socks5://127.0.0.1:20001')
    store.update_settings({'daily_checkin_enabled': False, 'task_timeout_seconds': 1800})
    body = {'model': 'sd-2-0-mini', 'prompt': 'Landscape', 'duration': 4}
    store.create_task('queued-original', body, normalize_request(body), execution_backend='canvas_cli')
    store.create_task('unknown-original', body, normalize_request(body), execution_backend='canvas_cli')
    original_context = {'submission_phase': 'run_dispatching', 'submit_id': 'original-submit',
                        'node_id': 'original-node', 'financial_outcome': 'unconfirmed'}
    store.update_task('unknown-original', {'status': 'submitted',
        'error_code': 'UPSTREAM_SUBMIT_OUTCOME_UNKNOWN', 'upstream_context': original_context})
    before = {'accounts': store.accounts(private=True), 'settings': store.get_settings(),
              'tasks': {tid: store.task(tid) for tid in ('queued-original', 'unknown-original')}}
    service = Service(store)
    service.canvas_cli.verify_runtime = Mock(side_effect=results)
    service.canvas_cli.run = Mock(side_effect=AssertionError('no account command may be started'))
    service._pool.submit = Mock()
    with patch('app.service.threading.Thread') as thread, patch('app.service.time.sleep') as sleep:
        service.start()
    return service, before, thread, sleep


def assert_original_recovery_unchanged(service, before):
    assert service._started is True
    assert service.store.accounts(private=True) == before['accounts']
    assert service.store.get_settings() == before['settings']
    for tid, old in before['tasks'].items():
        assert service.store.task(tid) == old
    assert service._pool.submit.call_count == 2
    service._pool.submit.assert_has_calls([
        call(service._run, 'queued-original'),
        call(service._resume_canvas_poll, 'unknown-original')], any_order=True)
    service.canvas_cli.run.assert_not_called()


def test_transient_local_failure_retries_once_and_preserves_recovery(tmp_path, caplog):
    service, before, thread, sleep = start_service(tmp_path, [invalid_local_response(), RUNTIME])
    try:
        assert service.canvas_cli.verify_runtime.call_count == 2
        assert service.canvas_runtime['available'] is True
        assert service.canvas_runtime['startup_attempts'] == 2
        assert service.canvas_runtime['last_error_code'] == 'CLI_INVALID_RESPONSE'
        diagnostic = service.canvas_runtime['startup_last_error']
        assert diagnostic['exit_code'] == 1
        assert diagnostic['transport_diagnostic']['command_pair'] == ['schema']
        assert diagnostic['transport_diagnostic']['stderr_bytes'] == 5
        assert diagnostic['transport_diagnostic']['stderr_sha256'] == hashlib.sha256(b'noise').hexdigest()
        assert set(diagnostic) == {'code', 'phase', 'exit_code', 'outcome_unknown', 'transport_diagnostic'}
        assert all(x not in json.dumps(service.canvas_runtime) + caplog.text for x in
                   ['private-token', 'private.test', 'secret=value'])
        sleep.assert_called_once_with(0.25)
        thread.return_value.start.assert_called_once()
        assert_original_recovery_unchanged(service, before)
    finally:
        service.stop()


def test_persistent_local_failure_is_bounded_and_keeps_browser_available(tmp_path):
    service, before, thread, sleep = start_service(tmp_path,
        [invalid_local_response('version'), invalid_local_response('version')])
    try:
        assert service.canvas_cli.verify_runtime.call_count == 2
        assert service.canvas_runtime['available'] is False
        assert service.canvas_runtime['error_code'] == 'CLI_INVALID_RESPONSE'
        assert service.canvas_runtime['startup_attempts'] == 2
        assert service.canvas_runtime['startup_last_error']['transport_diagnostic']['command_pair'] == ['version']
        sleep.assert_called_once_with(0.25)
        thread.return_value.start.assert_called_once()
        assert_original_recovery_unchanged(service, before)
    finally:
        service.stop()


@pytest.mark.parametrize('code', ['CLI_RUNTIME_INCOMPATIBLE', 'CLI_PROXY_WRAPPER_UNAVAILABLE',
    'CLI_PROXY_WRAPPER_REQUIRED', 'CLI_COMMAND_TIMEOUT', 'CLI_OUTPUT_LIMIT', 'cli.authentication_required'])
def test_other_startup_errors_are_not_retried(tmp_path, code):
    service, before, thread, sleep = start_service(tmp_path,
        [CanvasCLIError(code, 'local runtime unavailable')])
    try:
        service.canvas_cli.verify_runtime.assert_called_once_with()
        assert service.canvas_runtime['available'] is False
        assert service.canvas_runtime['startup_attempts'] == 1
        assert service.canvas_runtime['last_error_code'] == code
        sleep.assert_not_called()
        thread.return_value.start.assert_called_once()
        assert_original_recovery_unchanged(service, before)
    finally:
        service.stop()


def test_successful_first_probe_does_not_add_an_extra_invocation(tmp_path):
    service, before, _, sleep = start_service(tmp_path, [RUNTIME])
    try:
        service.canvas_cli.verify_runtime.assert_called_once_with()
        assert service.canvas_runtime == {**RUNTIME, 'startup_attempts': 1, 'last_error_code': None}
        sleep.assert_not_called()
        assert_original_recovery_unchanged(service, before)
    finally:
        service.stop()


@pytest.mark.parametrize('command', ['version', 'schema'])
def test_local_probe_error_retains_only_exact_command_and_output_fingerprints(tmp_path, command):
    raw = b'private prompt token=private-token https://private.test/signed?secret=value'
    process = SimpleNamespace(stdout=io.BytesIO(raw), stderr=io.BytesIO(b'noise'),
        poll=lambda: 1, returncode=1)
    runner = CanvasCLI(tmp_path, allow_unwrapped_for_tests=True)
    with patch.object(runner, 'environment', return_value={}), \
            patch.object(runner, 'profile', return_value='synthetic-profile'), \
            patch('app.canvas_cli.subprocess.Popen', return_value=process) as spawn:
        with pytest.raises(CanvasCLIError) as caught:
            runner.run({'id': 2147483647}, [command], timeout=5)
    spawn.assert_called_once()
    error = caught.value
    assert error.code == 'CLI_INVALID_RESPONSE' and error.exit_code == 1
    assert error.outcome_unknown is False
    assert error.details['transport_diagnostic'] == {'command_pair': [command],
        'stdout_bytes': len(raw), 'stdout_sha256': hashlib.sha256(raw).hexdigest(),
        'stderr_bytes': 5, 'stderr_sha256': hashlib.sha256(b'noise').hexdigest()}
    assert all(x not in json.dumps(error.details) for x in ['private-token', 'private.test', 'secret=value'])
