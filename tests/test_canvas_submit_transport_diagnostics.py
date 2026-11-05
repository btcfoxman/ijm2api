"""Submission errors retain safe transport facts without changing paid retry."""
import hashlib
import json

import pytest

from app.canvas_cli import CanvasCLIError, MAX_OUTPUT_BYTES, safe_transport_diagnostic
from app.canvas_client import safe_cli_diagnostic
from app.canvas_service import CanvasServiceMixin
from test_canvas_preparation_diagnostics import captured_error
from test_canvas_transport import prepare


def test_real_exit22_fingerprints_survive_run_error_persistence_without_replay(tmp_path):
    stderr = (b"ptrace(PTRACE_POKEUSER): No such process\n"
              b"private-prompt https://user:private-password@proxy.example/private-token")
    error = captured_error(tmp_path / 'capture', b'', stderr, pair=('node', 'run'), exit_code=22)
    runner, client, context = prepare(tmp_path)
    runner.run_failure = error
    durable = []
    with pytest.raises(CanvasCLIError) as caught:
        client.submit(context, 20, persist=durable.append)

    diagnostic = durable[-1]['submit_error']
    assert diagnostic['code'] == 'CLI_INVALID_RESPONSE'
    assert diagnostic['exit_code'] == 22
    assert diagnostic['dispatch_started'] is diagnostic['outcome_unknown'] is True
    assert diagnostic['phase'] == 'run_dispatching'
    expected = {
        'command_pair': ['node', 'run'], 'stdout_bytes': 0,
        'stdout_sha256': hashlib.sha256(b'').hexdigest(),
        'stderr_bytes': len(stderr), 'stderr_sha256': hashlib.sha256(stderr).hexdigest(),
        'stderr_symptoms': ['ptrace_diagnostic'],
    }
    assert diagnostic['transport_diagnostic'] == expected
    assert caught.value.details['transport_diagnostic'] == expected
    assert caught.value.details['error']['transport_diagnostic'] == expected
    assert CanvasServiceMixin._canvas_diagnostic(caught.value, 'run_dispatching')['transport_diagnostic'] == expected
    assert context['submit_id'] == durable[-1]['submit_id']
    assert not context['resource_ids']
    with pytest.raises(CanvasCLIError) as replay:
        client.submit(context, 20)
    assert replay.value.outcome_unknown is True
    assert sum(args[:2] == ['node', 'run'] for args in runner.calls) == 1
    assert sum(args[:2] == ['node', 'confirm'] for args in runner.calls) == 1
    safe = json.dumps([durable, caught.value.details])
    for secret in ('private-prompt', 'private-password', 'private-token', 'proxy.example',
                   'PTRACE_POKEUSER', 'never-save-this-token'):
        assert secret not in safe


@pytest.mark.parametrize('value', [None, [], 'private text', {'command_pair': [['secret']]},
    {'stdout_bytes': True, 'stderr_bytes': MAX_OUTPUT_BYTES + 1,
     'stdout_sha256': 'secret', 'stderr_sha256': 'https://secret.example',
     'stderr_symptoms': 'ptrace_diagnostic'},
    {'command_pair': ['unknown', 'private-prompt'], 'raw': 'private-prompt'}])
def test_transport_diagnostic_rejects_unbounded_or_freeform_values(value):
    assert safe_transport_diagnostic(value) == {}


def test_submission_sanitizer_keeps_only_known_transport_diagnostics():
    error = CanvasCLIError('cli.transport_failed', 'safe message', exit_code=21, details={
        'transport_diagnostic': {
            'command_pair': ['node', 'run'], 'stderr_bytes': 15,
            'stderr_sha256': 'a' * 64,
            'stderr_symptoms': ['private-token', 'connection_reset', 'ptrace_diagnostic'],
            'stderr': 'private-prompt', 'proxy': 'https://private-password@example.invalid',
        },
    }, outcome_unknown=True)
    diagnostic = safe_cli_diagnostic(error)
    assert diagnostic['transport_diagnostic'] == {
        'command_pair': ['node', 'run'], 'stderr_bytes': 15, 'stderr_sha256': 'a' * 64,
        'stderr_symptoms': ['ptrace_diagnostic', 'connection_reset'],
    }
    assert diagnostic['outcome_unknown'] is True
    assert 'private' not in json.dumps(diagnostic)
