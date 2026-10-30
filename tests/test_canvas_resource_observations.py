"""Readiness diagnostics cannot change the returned object or generation policy."""
import copy
import json
from unittest.mock import patch

import pytest

from app.canvas_cli import CanvasCLIError
from app.canvas_timing import PreparationTiming


def observe(timing, result, args=None):
    return timing.command(object(), {'id': 19, 'proxy_url': 'secret-proxy'},
        args or ['resource', 'get', 'private-resource-id'], {}, 1, lambda: result)


def test_pending_then_success_is_observed_without_changing_readiness_or_result_identity():
    timing = PreparationTiming('task-readiness')
    pending = {'resourceId': 'private-resource-id', 'status': 'processing', 'type': 'image', 'source': 'uploaded'}
    ready = {**pending, 'status': 'success', 'image': {'url': 'https://secret/signed'}}
    before = copy.deepcopy(ready)
    assert observe(timing, pending) is pending
    assert observe(timing, ready) is ready and ready == before
    facts = [row['resource_facts'] for row in timing.snapshot()['recent_commands']]
    assert [x['status'] for x in facts] == ['processing', 'success']
    assert [x['image_metadata_present'] for x in facts] == [False, True]
    assert all(x['resource_id_matches'] for x in facts)


def test_only_fixed_facts_are_retained_and_all_arbitrary_secret_fields_are_omitted():
    timing = PreparationTiming('task-secrets')
    result = {'resourceId': 'private-resource-id', 'status': 'success', 'type': 'video', 'source': 'generated',
        'video': {'url': 'https://private/signed?token=private-token'}, 'errorCode': 20115,
        'creditsAmount': 120, 'prompt': 'private-prompt', 'errorMessage': 'private-upstream-message',
        'audit': {'token': 'private-auth'}, 'accessToken': 'private-token'}
    observe(timing, result)
    snapshot = timing.snapshot();facts = snapshot['recent_commands'][0]['resource_facts']
    assert facts == {'resource_id_matches': True, 'status': 'success', 'type': 'video', 'source': 'generated',
        'image_metadata_present': False, 'video_metadata_present': True, 'error_code': 20115}
    assert not any(secret in json.dumps(snapshot) for secret in ['private-resource-id', 'private-token',
        'private-auth', 'private-prompt', 'private-upstream-message', 'https://private', 'creditsAmount', 'refund'])


@pytest.mark.parametrize('code', [True, 2**64, 'opaque-credential', 'a' * 32,
    '12345678-1234-1234-1234-123456789012', 'https://private/?token=x', {'secret': 'credential'}])
def test_untrusted_error_codes_are_not_retained(code):
    timing = PreparationTiming('task-codes')
    observe(timing, {'status': 'secret-status', 'type': 'private-type', 'source': 'private-source', 'errorCode': code})
    facts = timing.snapshot()['recent_commands'][0]['resource_facts']
    assert 'error_code' not in facts
    assert facts['status'] is facts['type'] is facts['source'] is None
    assert facts['resource_id_matches'] is False


def test_safe_short_error_enum_is_retained_without_inventing_a_financial_conclusion():
    timing = PreparationTiming('task-enum')
    observe(timing, {'status': 'FAILED', 'type': 'audio', 'errorCode': 'POLICY_REJECTED'})
    facts = timing.snapshot()['recent_commands'][0]['resource_facts']
    assert facts['status'] == 'failed' and facts['type'] == 'audio' and facts['error_code'] == 'policy_rejected'
    assert not any('cost' in key or 'credit' in key or 'refund' in key for key in facts)


def test_failed_invocation_keeps_original_error_and_does_not_fabricate_resource_success():
    timing = PreparationTiming('task-failure');error = CanvasCLIError('cli.transport_failed', 'network')
    def failure():
        raise error
    with pytest.raises(CanvasCLIError) as caught:
        timing.command(object(), {'id': 19}, ['resource', 'get', 'private-resource-id'], {}, 1, failure)
    assert caught.value is error
    row = timing.snapshot()['recent_commands'][0]
    assert row['outcome'] == 'failed' and 'resource_facts' not in row


def test_observation_failure_does_not_replace_the_original_success_object():
    timing = PreparationTiming('task-telemetry');result = {'status': 'success'}
    with patch('app.canvas_timing._resource_observation', side_effect=RuntimeError('observer failed')):
        assert observe(timing, result) is result
    row = timing.snapshot()['recent_commands'][0]
    assert row['outcome'] == 'success' and 'resource_facts' not in row


def test_other_commands_are_not_observed_or_retried_and_financial_values_stay_unchanged():
    timing = PreparationTiming('task-run');result = {'creditsAmount': 32, 'state': 'ACCEPTED'};calls = []
    def run():
        calls.append(True)
        return result
    assert timing.command(object(), {'id': 19}, ['node', 'run'], {}, 1, run) is result
    assert calls == [True] and result == {'creditsAmount': 32, 'state': 'ACCEPTED'}
    row = timing.snapshot()['recent_commands'][0]
    assert 'resource_facts' not in row and timing.snapshot()['commands']['node run']['count'] == 1


def test_resource_observations_use_only_the_existing_bounded_recent_events():
    timing = PreparationTiming('task-bounded')
    for index in range(70):
        observe(timing, {'resourceId': 'private-resource-id', 'status': 'success'})
    value = timing.snapshot()
    assert value['command_count'] == 70 and len(value['recent_commands']) == 64
    assert value['dropped_command_samples'] == 6
