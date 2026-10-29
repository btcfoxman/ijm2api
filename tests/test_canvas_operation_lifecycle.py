"""Original operation lifecycle evidence is distinct from run admission."""
from copy import deepcopy

import pytest

from app.canvas_cli import CanvasCLIError
from test_canvas_transport import prepare


RESOURCE = '11111111-1111-4111-8111-111111111111'


def operation_reply(tmp_path, *, state='succeeded', summary='completed', resource_state='succeeded', resources=None):
    runner, client, context = prepare(tmp_path)
    context.update(phase='accepted', submission_phase='submitted', resource_ids=[RESOURCE])
    data = {'operationRef': context['submit_id'], 'state': state,
            'submission': {'state': summary, 'resubmittable': False, 'adviceCode': 'none', 'advice': ''},
            'resources': [{'resourceId': RESOURCE, 'state': resource_state}] if resources is None else resources}
    original = runner.run
    runner.calls.clear()

    def execute(account, args, timeout=None, deadline=None):
        if args[:2] == ['operation', 'status']:
            runner.calls.append(args)
            assert args[2] == context['submit_id']
            assert args[-1] == context['project_id']
            return {'ok': True, 'data': deepcopy(data)}
        return original(account, args, timeout=timeout, deadline=deadline)

    runner.run = execute
    return runner, client, context, data


@pytest.mark.parametrize('summary', ['completed', 'pending', 'processing', 'unknown', 'absent'])
def test_real_succeeded_operation_is_not_hidden_by_lifecycle_summary(tmp_path, summary):
    runner, client, context, _ = operation_reply(tmp_path, summary=summary)
    result = client.poll(context)
    assert result['status'] == 'succeeded' and len(result['result_files']) == 1
    assert result['result_files'][0]['resource_id'] == RESOURCE
    assert result['credit_facts'][0]['settled'] is False
    assert not any(args[:2] in (['node', 'run'], ['node', 'confirm'], ['node', 'create']) for args in runner.calls)
    assert sum(args[:2] == ['operation', 'status'] for args in runner.calls) == 1


@pytest.mark.parametrize('summary', ['pending', 'completed', 'unknown'])
def test_explicit_original_generation_failure_is_not_hidden_by_summary(tmp_path, summary):
    runner, client, context, _ = operation_reply(tmp_path, state='failed', resource_state='failed', summary=summary)
    runner.resource_error_code = 20114
    result = client.poll(context)
    assert result['status'] == 'failed' and result['error_code'] != 'UPSTREAM_SUBMIT_OUTCOME_UNKNOWN'
    assert not any(args[:2] in (['node', 'run'], ['node', 'confirm'], ['resource', 'download']) for args in runner.calls)


@pytest.mark.parametrize('summary', ['pending', 'processing', 'completed', 'unknown', 'absent'])
def test_matching_original_running_resource_is_positive_generation_evidence(tmp_path, summary):
    runner, client, context, _ = operation_reply(tmp_path, state='running', resource_state='running', summary=summary)
    assert client.poll(context)['status'] == 'running'
    assert len(runner.calls) == 1 and runner.calls[0][:3] == ['operation', 'status', context['submit_id']]


@pytest.mark.parametrize('state', ['unknown', 'absent'])
def test_true_unknown_top_level_operation_stays_unknown_even_with_resources(tmp_path, state):
    runner, client, context, _ = operation_reply(tmp_path, state=state)
    assert client.poll(context)['status'] == 'unknown'
    assert len(runner.calls) == 1


@pytest.mark.parametrize('summary', ['unknown', 'absent', 'RUN_UNKNOWN'])
def test_bare_unresolved_summary_without_resources_is_not_claimed_running(tmp_path, summary):
    runner, client, context, _ = operation_reply(tmp_path, state='pending', summary=summary, resources=[])
    assert client.poll(context)['status'] == 'unknown'
    assert len(runner.calls) == 1


@pytest.mark.parametrize('mismatch', ['operation_ref', 'foreign_resource', 'duplicate_resource', 'invalid_resource', 'bad_context_binding'])
def test_original_identity_and_resource_binding_mismatches_fail_closed(tmp_path, mismatch):
    runner, client, context, data = operation_reply(tmp_path)
    if mismatch == 'operation_ref':
        data['operationRef'] = 'not-the-original-operation'
    elif mismatch == 'foreign_resource':
        data['resources'][0]['resourceId'] = '22222222-2222-4222-8222-222222222222'
    elif mismatch == 'duplicate_resource':
        data['resources'] *= 2
    elif mismatch == 'invalid_resource':
        data['resources'][0].pop('state')
    else:
        context['resource_ids'] = 'not-a-list'
    with pytest.raises(CanvasCLIError) as mismatch_error:
        client.poll(context)
    assert mismatch_error.value.outcome_unknown
    assert len(runner.calls) == 1
    assert not any(args[:2] in (['node', 'run'], ['node', 'confirm'], ['resource', 'download']) for args in runner.calls)


def test_terminal_without_an_original_output_does_not_claim_success(tmp_path):
    runner, client, context, _ = operation_reply(tmp_path, resources=[])
    assert client.poll(context)['status'] != 'succeeded'
    assert len(runner.calls) == 1
