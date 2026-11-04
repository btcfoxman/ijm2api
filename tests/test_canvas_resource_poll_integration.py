"""An accepted original resource must be delivered despite a stale operation index."""
from copy import deepcopy
import time

import pytest

from app.canvas_cli import CanvasCLIError
from app.canvas_client import CanvasClient
from app.store import accepted_canvas_result, canvas_output_source_valid
from test_canvas_execution import context
from test_canvas_operation_node_recovery import PROJECT, SUBMIT, NODE
from test_canvas_output_binding_persistence import (
    RID, binding_receipt, durable, no_live_commands_or_network)


@pytest.fixture
def accepted_case(durable):
    store, service, account, runner, before = durable
    bound = {**before['upstream_context'], 'project_id': PROJECT, 'node_id': NODE,
        'submit_id': SUBMIT, 'resource_ids': [RID], 'submission_receipt_evidence': True,
        'submission_phase': 'submitted', 'phase': 'accepted'}
    bound['submission_evidence'].update(accepted=True, output_resources=True, ambiguous_partial=True)
    row = store.update_task(before['id'], {'upstream_context': bound})
    runner.bound = bound
    runner.state = 'running'
    runner.facts[RID] = {'status': 'success'}
    return store, service, account, runner, row, bound


def poll(case):
    store, service, account, runner, row, bound = case
    return CanvasClient(runner, account).poll(deepcopy(bound), deadline=time.monotonic() + 15,
        persist_output_binding=lambda receipt: service._canvas_persist_output_binding(row['id'], receipt))


def test_original_resource_success_bypasses_stale_operation_and_commits_before_download(accepted_case):
    store, service, account, runner, row, bound = accepted_case
    observed = []
    def before_download(resource):
        current = store.task(row['id'])
        binding = current['upstream_context']['output_binding']
        assert binding['source'] == 'accepted_receipt_resource_get'
        assert binding['resource_recovery']['receipt_evidence'] == bound['submission_evidence']
        assert binding['durable'] is True and accepted_canvas_result(current)
        assert current['upstream_context']['submission_phase'] == 'accepted'
        observed.append(resource)
    runner.before_download = before_download
    result = poll(accepted_case)
    assert result['status'] == 'succeeded' and result['resource_ids'] == [RID]
    assert observed == [RID]
    assert runner.count(('operation', 'status')) == 0
    assert runner.count(('resource', 'get')) == 1
    assert runner.count(('resource', 'download')) == 1
    assert store.task(row['id'])['actual_cost'] is None


def test_positive_running_resource_does_not_download_or_query_the_operation_index(accepted_case):
    store, service, account, runner, row, bound = accepted_case
    runner.facts[RID] = {'status': 'processing'}
    before = store.task(row['id'])
    result = poll(accepted_case)
    assert result['status'] == 'running'
    assert runner.count(('resource', 'get')) == 1
    assert runner.count(('operation', 'status')) == runner.count(('resource', 'download')) == 0
    assert store.task(row['id']) == before


def test_authoritative_operation_resources_outrank_a_stale_running_summary(accepted_case):
    store, service, account, runner, row, bound = accepted_case
    bound.pop('submission_receipt_evidence')  # Exercise the ordinary operation path.
    store.update_task(row['id'], {'upstream_context': bound})
    original = runner.run
    def run(owner, args, **kwargs):
        value = original(owner, args, **kwargs)
        if args[:2] == ['operation', 'status']:
            value['data']['resources'][0]['state'] = 'succeeded'
            value['data'].update(source='accepted_receipt_resource_get', resource_recovery={'forged': True})
        return value
    runner.run = run
    result = poll(accepted_case)
    assert result['status'] == 'succeeded'
    assert runner.count(('operation', 'status')) == 1
    saved = store.task(row['id'])['upstream_context']['output_binding']
    assert saved == {**binding_receipt(bound), 'durable': True}


@pytest.mark.parametrize('change', [
    {'resourceId': '22222222-2222-4222-8222-222222222222'}, {'type': 'image'},
    {'submitId': PROJECT}, {'nodeId': 'node_0000000000'}, {'projectId': SUBMIT},
])
def test_original_receipt_cannot_authorize_foreign_resource_facts(accepted_case, change):
    store, service, account, runner, row, bound = accepted_case
    runner.facts[RID].update(change)
    before = store.task(row['id'])
    with pytest.raises(CanvasCLIError) as error:
        poll(accepted_case)
    assert error.value.code == 'CLI_RESOURCE_MISMATCH'
    assert store.task(row['id']) == before
    assert runner.count(('resource', 'download')) == 0


def test_durable_CAS_rejects_synthetic_acceptance_not_present_in_the_original_context(accepted_case):
    store, service, account, runner, row, bound = accepted_case
    captured = []
    result = CanvasClient(runner, account).poll(deepcopy(bound), deadline=time.monotonic() + 15,
        persist_output_binding=lambda receipt: captured.append(receipt) or True)
    assert result['status'] == 'succeeded'
    receipt = captured[0]
    assert canvas_output_source_valid(receipt)
    changed = deepcopy(bound)
    changed['submission_evidence']['accepted'] = False
    before = store.update_task(row['id'], {'upstream_context': changed})
    assert service._canvas_persist_output_binding(row['id'], receipt) is False
    assert store.task(row['id']) == before
