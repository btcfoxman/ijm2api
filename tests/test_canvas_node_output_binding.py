"""Original-node recovery cannot bypass output identity or billing boundaries."""
from copy import deepcopy
import time

import pytest

from app.store import Store, accepted_canvas_result, canvas_output_source_valid
from test_canvas_execution import context
from test_canvas_output_binding_persistence import (
    RID, OTHER, binding_receipt, durable, no_live_commands_or_network)


def node_receipt(context):
    receipt = binding_receipt(context)
    receipt['source'] = 'node_show_original_submission'
    receipt['node_recovery'] = {
        'version': 1, **{key: receipt[key] for key in ('project_id', 'node_id', 'submit_id')},
        'resource_ids': list(receipt['resource_ids']),
        'resource_submit_ids': {rid: receipt['submit_id'] for rid in receipt['resource_ids']}}
    return receipt


def test_strict_original_node_receipt_is_durable_and_restart_eligible_without_financial_settlement(durable):
    store, service, _account, _runner, before = durable
    receipt = node_receipt(before['upstream_context'])
    assert canvas_output_source_valid(receipt)
    assert service._canvas_persist_output_binding(before['id'], receipt)
    row = Store(str(store.path)).task(before['id'])
    assert accepted_canvas_result(row)
    assert row['upstream_context']['output_binding'] == {**receipt, 'durable': True}
    assert row['upstream_context']['resource_ids'] == [RID]
    for key in ('actual_cost', 'balance_before', 'balance_after', 'estimated_cost'):
        assert row.get(key) == before.get(key)
    claim = store.claim_canvas_result_delivery(row['id'], row['updated_at'],
        expected_binding=receipt, observer_claim_id=None, duration_seconds=900)
    assert claim is not None
    assert claim['upstream_context']['result_delivery_attempt']['deadline'] <= time.time() + 900


@pytest.mark.parametrize('mutation', ['missing', 'version', 'project', 'node', 'submit',
    'foreign_resource', 'missing_resource_submit', 'wrong_resource_submit', 'extra_resource_submit',
    'extra_proof_fields', 'source', 'duplicate_resources'])
def test_unverified_node_sources_do_not_bind_or_override_original_failure(durable, mutation):
    store, service, _account, _runner, before = durable
    receipt = node_receipt(before['upstream_context'])
    proof = receipt['node_recovery']
    if mutation == 'missing': receipt.pop('node_recovery')
    elif mutation == 'version': proof['version'] = True
    elif mutation == 'project': proof['project_id'] = 'foreign-project'
    elif mutation == 'node': proof['node_id'] = 'foreign-node'
    elif mutation == 'submit': proof['submit_id'] = 'foreign-submit'
    elif mutation == 'foreign_resource': proof['resource_ids'] = [OTHER]
    elif mutation == 'missing_resource_submit': proof['resource_submit_ids'] = {}
    elif mutation == 'wrong_resource_submit': proof['resource_submit_ids'][RID] = 'foreign-submit'
    elif mutation == 'extra_resource_submit': proof['resource_submit_ids'][OTHER] = receipt['submit_id']
    elif mutation == 'extra_proof_fields': proof['untrusted_flag'] = True
    elif mutation == 'source': receipt['source'] = 'unverified_dict'
    elif mutation == 'duplicate_resources':
        receipt['resource_ids'] = proof['resource_ids'] = [RID, RID]
    assert not canvas_output_source_valid(receipt)
    assert service._canvas_persist_output_binding(before['id'], receipt) is False
    assert store.task(before['id']) == before
    row = deepcopy(before)
    context = row['upstream_context']
    context.update(resource_ids=[RID], submit_error={'code': 'cli.invalid_service_response'},
        output_binding={**receipt, 'durable': True})
    context['submission_evidence'].update(output_resources=True, receipt=True)
    assert accepted_canvas_result(row) is False


def test_original_node_outputs_cannot_revive_an_already_failed_routed_task(durable):
    store, service, _account, _runner, before = durable
    receipt = node_receipt(before['upstream_context'])
    closed = store.update_task(before['id'], {'status': 'failed',
        'error_code': 'CLI_OUTPUT_RESOURCE_RECOVERY_EXHAUSTED', 'completed_at': time.time()})
    assert service._canvas_persist_output_binding(before['id'], receipt) is False
    assert store.task(before['id']) == closed
