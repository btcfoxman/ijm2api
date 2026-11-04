"""Original-node diagnostics contain useful absence reasons, never raw media."""
from copy import deepcopy
import time

import pytest

from app.canvas_cli import CanvasCLIError
from app.canvas_client import CanvasClient
from app.canvas_operation_recovery import safe_recovery_diagnostic
from test_canvas_operation_node_recovery import client, view, output, facts
from test_canvas_node_poll_recovery import original_node_case
from test_canvas_output_binding_persistence import durable, no_live_commands_or_network
from test_canvas_execution import context


@pytest.mark.parametrize('response,reason,extra', [
    ({'nodes': []}, 'node_count_invalid', {'node_count': 0}),
    ({'unexpected': 'private prompt https://secret.test'}, 'nodes_missing', {}),
    (view([]), 'outputs_empty', {'node_count': 1, 'output_count': 0,
        'node_id_matches': True, 'node_type_matches': True}),
    (view([output(submitId=None)]), 'submit_id_missing', {'output_count': 1,
        'submit_ids_present': False}),
    (view([output(status='unknown')]), 'output_state_unknown', {'output_count': 1,
        'submit_ids_present': True, 'submit_ids_match': True}),
])
def test_no_proof_reason_is_precise_and_does_not_add_provider_reads(response, reason, extra):
    instance = client([response])
    observed = []
    from app.canvas_operation_recovery import recover_absent_operation
    from test_canvas_operation_node_recovery import context, absent
    assert recover_absent_operation(instance, context(), absent(), deadline=instance.original_deadline,
                                    on_diagnostic=observed.append) is None
    assert len(instance.calls) == 1
    assert observed[-1]['reason'] == reason
    assert all(observed[-1][key] == value for key, value in extra.items())
    assert 'private' not in repr(observed) and 'https://' not in repr(observed)


def test_diagnostic_whitelist_drops_raw_values_and_bad_types():
    assert safe_recovery_diagnostic({'version': 1, 'reason': 'raw error'}) == {}
    assert safe_recovery_diagnostic({'version': 1, 'reason': 'outputs_empty',
        'prompt': 'private', 'url': 'https://private.test', 'nodeId': 'private',
        'node_count': True, 'output_count': -1, 'node_id_matches': 'true'}) == {
            'version': 1, 'reason': 'outputs_empty'}


def test_real_808_observed_empty_node_with_18_input_refs_is_not_an_output_or_retry_permission():
    # Shape observed by the authorized original-node read nine minutes after
    # failure. Only structural facts are reproduced; none of its private values.
    from test_canvas_operation_node_recovery import context, absent
    from app.canvas_operation_recovery import recover_absent_operation
    response = view([])
    node = response['nodes'][0]['node']
    node.update(status='empty', generation={'durationSeconds': 4, 'outputCount': 1,
        'writebackBlockers': [], 'references': [
            {'kind': 'resource', 'id': f'fixture-input-{i}',
             'type': 'image' if i < 15 else 'audio', 'writebackSupported': True} for i in range(18)]})
    instance = client([response]); observed = []; original = context()
    before = deepcopy(original)
    assert recover_absent_operation(instance, original, absent(), deadline=instance.original_deadline,
                                    on_diagnostic=observed.append) is None
    assert original == before and len(instance.calls) == 1
    assert observed[-1]['reason'] == 'outputs_empty' and observed[-1]['output_count'] == 0
    assert observed[-1]['node_id_matches'] is True


def test_empty_original_node_reason_is_durable_even_when_original_absence_propagates(original_node_case):
    store, service, account, runner, row, bound, node, original, _mode = original_node_case
    node['resources'] = []
    with pytest.raises(CanvasCLIError) as caught:
        CanvasClient(runner, account).poll(deepcopy(bound), deadline=time.monotonic() + 15,
            persist_node_recovery=lambda value: service._canvas_persist_node_recovery(row['id'], value))
    assert caught.value is original
    current = store.task(row['id'])
    saved = current['upstream_context']['node_recovery_diagnostic']
    assert saved['reason'] == 'outputs_empty' and saved['output_count'] == 0
    assert saved['node_id_matches'] is saved['node_type_matches'] is True
    assert isinstance(saved['at'], float)
    assert current['status'] == row['status'] and not current['upstream_context']['resource_ids']
    assert runner.count(('resource', 'get')) == runner.count(('resource', 'download')) == 0


def test_recovered_original_node_is_reported_without_resource_identifiers():
    instance = client([view(), facts()])
    observed = []
    from app.canvas_operation_recovery import recover_absent_operation
    from test_canvas_operation_node_recovery import context, absent
    result = recover_absent_operation(instance, context(), absent(), deadline=instance.original_deadline,
                                     on_diagnostic=observed.append)
    assert result['state'] == 'completed'
    assert observed[-1]['reason'] == 'recovered' and observed[-1]['verified_resource_count'] == 1
    assert all(not isinstance(value, str) or value in {'recovered'} for value in observed[-1].values())


def test_conflicting_submit_is_reported_and_still_fails_closed():
    instance = client([view([output(submitId='other')])])
    observed = []
    from app.canvas_operation_recovery import recover_absent_operation
    from test_canvas_operation_node_recovery import context, absent
    with pytest.raises(CanvasCLIError) as caught:
        recover_absent_operation(instance, context(), absent(), deadline=instance.original_deadline,
                                 on_diagnostic=observed.append)
    assert caught.value.code == 'CLI_RESOURCE_MISMATCH'
    assert observed[-1]['reason'] == 'binding_mismatch'
    assert observed[-1]['submit_binding_matches'] is False
    assert len(instance.calls) == 1
