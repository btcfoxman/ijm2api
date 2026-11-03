"""Original-node reads must join the existing durable delivery path."""
from copy import deepcopy
import time

import pytest

from app.canvas_cli import CanvasCLIError
from app.canvas_client import CanvasClient
from app.store import accepted_canvas_result
from test_canvas_execution import context
from test_canvas_operation_node_recovery import PROJECT, SUBMIT, NODE
from test_canvas_output_binding_persistence import (
    RID, binding_receipt, durable, no_live_commands_or_network)


@pytest.fixture
def original_node_case(durable):
    store, service, account, runner, before = durable
    bound = {**before['upstream_context'], 'project_id': PROJECT, 'node_id': NODE,
        'submit_id': SUBMIT, 'phase': 'quoted', 'submission_phase': 'run_dispatching',
        'submit_error': {'code': 'cli.invalid_service_response', 'phase': 'run_dispatching',
            'dispatch_started': True, 'outcome_unknown': True}}
    row = store.update_task(before['id'], {'upstream_context': bound})
    runner.bound = bound
    runner.facts[RID] = {'status': 'success'}
    run_original = runner.run
    error = CanvasCLIError('cli.operation_submission_absent', 'Original operation is absent',
        exit_code=20, details={'error': {'code': 'cli.operation_submission_absent',
            'class': 'operation_incomplete', 'requiredAction': 'resume', 'retryable': True}})
    node = {'nodeId': NODE, 'type': 'video', 'resources': [
        {'resourceId': RID, 'type': 'video', 'status': 'success', 'submitId': SUBMIT}]}
    mode = {'ordinary_operation': False}

    def run(owner, args, **kwargs):
        pair = tuple(args[:2])
        assert pair in {('operation', 'status'), ('node', 'show'), ('resource', 'get'), ('resource', 'download')}
        if pair == ('operation', 'status') and not mode['ordinary_operation']:
            runner.calls.append(list(args))
            assert args[2] == SUBMIT
            raise error
        if pair == ('node', 'show'):
            runner.calls.append(list(args))
            assert args == ['node', 'show', '--project-id', PROJECT, '--node-id', NODE]
            assert kwargs['deadline'] <= time.monotonic() + 15
            return {'ok': True, 'data': {'nodes': [{'node': deepcopy(node), 'result': 'success'}]}}
        value = run_original(owner, args, **kwargs)
        if pair == ('operation', 'status'):
            value['data'].update(recovery_source='node_show_original_submission',
                node_recovery={'untrusted_forged_field': True})
        return value

    runner.run = run
    return store, service, account, runner, row, bound, node, error, mode


def poll(case):
    store, service, account, runner, row, bound, *_ = case
    return CanvasClient(runner, account).poll(deepcopy(bound), deadline=time.monotonic() + 15,
        persist_output_binding=lambda receipt: service._canvas_persist_output_binding(row['id'], receipt))


def test_missing_operation_recovers_explicit_original_node_before_any_download(original_node_case):
    store, _service, _account, runner, row, _bound, *_ = original_node_case
    observed = []

    def before_download(resource):
        current = store.task(row['id'])
        binding = current['upstream_context']['output_binding']
        assert binding['source'] == 'node_show_original_submission'
        assert binding['node_recovery']['resource_submit_ids'] == {RID: SUBMIT}
        assert binding['durable'] is True and accepted_canvas_result(current)
        observed.append(resource)

    runner.before_download = before_download
    result = poll(original_node_case)
    assert result['status'] == 'succeeded' and result['resource_ids'] == [RID]
    assert observed == [RID]
    assert runner.count(('operation', 'status')) == runner.count(('node', 'show')) == 1
    assert runner.count(('resource', 'get')) == 2 and runner.count(('resource', 'download')) == 1
    assert store.task(row['id'])['actual_cost'] is None


def test_empty_original_node_preserves_original_exception_and_closed_identity(original_node_case):
    store, _service, _account, runner, row, _bound, node, original, _mode = original_node_case
    node['resources'] = []
    before = store.task(row['id'])
    with pytest.raises(CanvasCLIError) as caught:
        poll(original_node_case)
    assert caught.value is original
    assert store.task(row['id']) == before
    assert runner.count(('node', 'show')) == 1
    assert runner.count(('resource', 'get')) == runner.count(('resource', 'download')) == 0


def test_foreign_submission_is_never_downloaded_or_bound(original_node_case):
    store, _service, _account, runner, row, _bound, node, *_ = original_node_case
    node['resources'][0]['submitId'] = PROJECT
    before = store.task(row['id'])
    with pytest.raises(CanvasCLIError) as caught:
        poll(original_node_case)
    assert caught.value.code == 'CLI_RESOURCE_MISMATCH'
    assert store.task(row['id']) == before
    assert runner.count(('resource', 'get')) == runner.count(('resource', 'download')) == 0


def test_untrusted_operation_metadata_cannot_impersonate_node_recovery(original_node_case):
    store, _service, _account, runner, row, bound, _node, _original, mode = original_node_case
    mode['ordinary_operation'] = True
    result = poll(original_node_case)
    assert result['status'] == 'succeeded'
    assert runner.count(('node', 'show')) == 0
    saved = store.task(row['id'])['upstream_context']['output_binding']
    assert saved == {**binding_receipt(bound), 'durable': True}
