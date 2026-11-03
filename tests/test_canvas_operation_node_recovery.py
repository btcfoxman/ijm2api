"""Only explicit original-submit node outputs can bridge an absent lookup."""
import copy
import time
from types import SimpleNamespace

import pytest

from app.canvas_cli import CanvasCLIError
from app.canvas_client import CanvasClient
from app.canvas_operation_recovery import SOURCE, recover_absent_operation


PROJECT = '11111111-1111-4111-8111-111111111111'
SUBMIT = '22222222-2222-4222-8222-222222222222'
RESOURCE = '33333333-3333-4333-8333-333333333333'
SECOND = '44444444-4444-4444-8444-444444444444'
INPUT = '55555555-5555-4555-8555-555555555555'
OTHER = '66666666-6666-4666-8666-666666666666'
NODE = 'node_0123456789'


def absent(**changes):
    facts = {'code': 'cli.operation_submission_absent', 'class': 'operation_incomplete',
             'requiredAction': 'resume', 'retryable': True, 'phase': 'query'}
    facts.update(changes)
    return CanvasCLIError('cli.operation_submission_absent', 'original lookup absent',
                          details={'error': facts}, exit_code=20)


def context(**changes):
    result = {'backend': 'canvas_cli', 'task_id': 'ijm_0123456789abcdef', 'account_id': 19,
              'expected_user_id': '12345', 'profile': 'original-profile', 'project_id': PROJECT,
              'node_id': NODE, 'submit_id': SUBMIT, 'kind': 'video', 'output_count': 1,
              'resource_ids': [], 'resources': {'images': [INPUT], 'videos': [], 'audios': []},
              'uploaded_resources': [], 'phase': 'quoted', 'submission_phase': 'run_dispatching',
              'submit_error': {'dispatch_started': True, 'outcome_unknown': True, 'phase': 'run_dispatching'}}
    result.update(changes)
    return result


def output(resource=RESOURCE, **changes):
    return {'resourceId': resource, 'type': 'video', 'status': 'success', 'submitId': SUBMIT, **changes}


def view(outputs=None, **changes):
    node = {'nodeId': NODE, 'type': 'video', 'resources': [output()] if outputs is None else outputs,
            'generation': {'references': [{'resource': {'resourceId': INPUT, 'submitId': SUBMIT}}]}}
    node.update(changes)
    return {'nodes': [{'nodeId': NODE, 'result': 'success', 'node': node}]}


def facts(resource=RESOURCE, **changes):
    return {'resourceId': resource, 'type': 'video', 'status': 'success', **changes}


def client(responses, account_changes=None):
    profile = ['original-profile']
    runner = SimpleNamespace(profile=lambda account: profile[0])
    account = {'id': 19, 'login_user_id': '12345', 'cli_user_id': '12345'}
    account.update(account_changes or {})
    instance = CanvasClient(runner, account)
    instance.calls = []
    responses = iter(responses)
    def call(args, **kwargs):
        assert tuple(args[:2]) in {('node', 'show'), ('resource', 'get')}
        assert kwargs['read_only'] is True
        assert 0 < kwargs['timeout'] <= 10
        assert kwargs['deadline'] == instance.original_deadline
        instance.calls.append((args, kwargs))
        response = next(responses)
        if isinstance(response, BaseException):
            raise response
        return copy.deepcopy(response)
    instance._call = call
    instance.original_deadline = time.monotonic() + 15
    instance.test_profile = profile
    return instance


def recover(instance, value=None, error=None):
    return recover_absent_operation(instance, context() if value is None else value,
                                    absent() if error is None else error,
                                    deadline=instance.original_deadline)


def test_exact_direct_original_submit_outputs_recover_with_honest_provenance_and_no_mutation():
    original = context()
    before = copy.deepcopy(original)
    instance = client([view(), facts()])
    result = recover(instance, original)
    assert original == before
    assert result['source'] == SOURCE
    assert result['operationRef'] == result['submitId'] == SUBMIT
    assert result['projectId'] == PROJECT and result['nodeId'] == NODE and result['state'] == 'completed'
    proof = result['recovery_provenance']
    assert proof['source'] == SOURCE and proof['operation_ref_origin'] == 'node.resources[].submitId'
    assert proof['resource_ids'] == [RESOURCE]
    assert proof['resource_bindings'][0]['node_submit_id'] == SUBMIT
    assert proof['resource_bindings'][0]['explicit_resource_bindings'] == {}
    assert result['recovery_source'] == SOURCE
    assert result['node_recovery'] == {'version': 1, 'project_id': PROJECT, 'node_id': NODE,
        'submit_id': SUBMIT, 'resource_ids': [RESOURCE], 'resource_submit_ids': {RESOURCE: SUBMIT}}
    assert [args for args, _ in instance.calls] == [
        ['node', 'show', '--project-id', PROJECT, '--node-id', NODE],
        ['resource', 'get', RESOURCE, '--project-id', PROJECT]]
    assert all(key not in proof for key in ('prompt', 'references', 'result_urls', 'credit_token'))


@pytest.mark.parametrize('accepted_phase', ['accepted', 'submitted'])
def test_accepted_receipt_without_lost_run_diagnostic_is_positive_dispatch(accepted_phase):
    instance = client([view(), facts()])
    value = context(phase='accepted', submission_phase=accepted_phase, submit_error={})
    assert recover(instance, value)['state'] == 'completed'


@pytest.mark.parametrize('account_changes', [{'login_user_id': ''}, {'login_user_id': None}, {'cli_user_id': ''}])
def test_single_nonempty_matching_account_identity_remains_valid(account_changes):
    instance = client([view(), facts()], account_changes=account_changes)
    assert recover(instance)['state'] == 'completed'


@pytest.mark.parametrize('account_changes', [{'login_user_id': '', 'cli_user_id': ''},
    {'login_user_id': 'other'}, {'cli_user_id': 'other'}])
def test_empty_or_conflicting_account_identities_cannot_recover(account_changes):
    instance = client([], account_changes=account_changes)
    with pytest.raises(CanvasCLIError) as caught:
        recover(instance)
    assert caught.value.code == 'CLI_RESOURCE_MISMATCH'
    assert instance.calls == []


@pytest.mark.parametrize('change', [{'manual_termination': True}, {'manual_termination': False},
    {'submission_evidence': {'identity_conflict': True}}])
def test_known_termination_or_identity_conflict_blocks_even_local_profile_reads(change):
    instance = client([])
    instance.runner.profile = lambda account: pytest.fail('blocked context must not read profile')
    assert recover(instance, context(**change)) is None
    assert instance.calls == []


def test_existing_output_order_is_preserved_after_complete_same_set_validation():
    value = context(kind='image', output_count=2, resource_ids=[RESOURCE, SECOND])
    responses = [view([output(SECOND, type='image'), output(type='image')], type='image'),
                 facts(type='image'), facts(SECOND, type='image')]
    instance = client(responses)
    result = recover(instance, value)
    assert [item['resourceId'] for item in result['resources']] == [RESOURCE, SECOND]


@pytest.mark.parametrize('resource_status,expected_state', [('running', 'running'), ('pending', 'running'),
    ('queued', 'running'), ('processing', 'running'), ('failed', 'failed'), ('cancelled', 'failed'),
    ('completed', 'completed')])
def test_fresh_resource_state_is_authoritative_without_inventing_completion(resource_status, expected_state):
    instance = client([view(), facts(status=resource_status, errorCode=20115)])
    result = recover(instance)
    assert result['state'] == expected_state
    assert result['resources'][0]['state'] == resource_status
    assert result['resources'][0]['errorCode'] == 20115


@pytest.mark.parametrize('change', [
    {'code': 'cli.operation_incomplete'}, {'class': 'internal_failure'},
    {'requiredAction': 'contact_support'}, {'requiredAction': 'human'}, {'phase': 'run_dispatching'},
])
def test_other_error_contracts_do_not_trigger_additional_reads(change):
    instance = client([])
    assert recover(instance, error=absent(**change)) is None
    assert instance.calls == []


def test_wrong_error_code_or_exit_and_no_deadline_do_not_trigger_reads():
    instance = client([])
    for error in [CanvasCLIError('cli.transport_failed', 'network'),
                  CanvasCLIError('cli.operation_submission_absent', 'absent', details={'error': {
                      'class': 'operation_incomplete', 'requiredAction': 'resume'}}, exit_code=1)]:
        assert recover(instance, error=error) is None
    assert recover_absent_operation(instance, context(), absent(), deadline=None) is None
    assert instance.calls == []


@pytest.mark.parametrize('changes', [
    {'submission_phase': 'confirming'}, {'phase': 'quoted', 'submission_phase': 'quoted'},
    {'submit_error': {}}, {'submit_error': {'dispatch_started': False, 'outcome_unknown': True, 'phase': 'run_dispatching'}},
])
def test_preparation_or_merely_allocated_submit_id_is_not_paid_dispatch(changes):
    instance = client([])
    assert recover(instance, context(**changes)) is None
    assert instance.calls == []


@pytest.mark.parametrize('changes', [
    {'account_id': 20}, {'profile': 'retired-profile'}, {'expected_user_id': '999'},
    {'project_id': ''}, {'submit_id': ''}, {'node_id': 'other'}, {'kind': 'audio'},
    {'resource_ids': [RESOURCE, SECOND]}, {'resource_ids': [RESOURCE, RESOURCE]}, {'output_count': True},
])
def test_original_context_binding_conflicts_stop_before_node_read(changes):
    instance = client([])
    with pytest.raises(CanvasCLIError) as caught:
        recover(instance, context(**changes))
    assert caught.value.code in {'CLI_RESOURCE_MISMATCH', 'CLI_ACCOUNT_MISMATCH'}
    assert instance.calls == []


@pytest.mark.parametrize('responses', [[view([])], [{'nodes': []}], [{'nodes': [{'result': 'not_found'}]}],
    [view([output(submitId=None)])], [view([output(status='unknown')])]])
def test_missing_or_unknown_association_stays_original_absence_without_resource_reads(responses):
    instance = client(responses)
    assert recover(instance) is None
    assert len(instance.calls) == 1


@pytest.mark.parametrize('response', [
    view([output(submitId=OTHER)]), view([output(), output(SECOND, submitId=OTHER)]),
    view([output(), output(SECOND)]), view(nodeId='node_abcdefghjk'), view(type='image'),
    view([output(resource=INPUT)]), view([output(source='local_upload')]),
    view([output(resource=OTHER)]),
])
def test_mixed_submissions_inputs_unexpected_outputs_or_wrong_node_never_selected(response):
    instance = client([response])
    with pytest.raises(CanvasCLIError, match='original task binding'):
        recover(instance, context(resource_ids=[RESOURCE]))
    assert len(instance.calls) == 1


@pytest.mark.parametrize('field,value', [('resourceId', OTHER), ('type', 'image'),
    ('projectId', OTHER), ('nodeId', 'node_abcdefghjk'), ('submitId', OTHER), ('source', 'local_upload')])
def test_conflicting_fresh_resource_fact_is_not_hidden(field, value):
    instance = client([view(), facts(**{field: value})])
    with pytest.raises(CanvasCLIError) as caught:
        recover(instance)
    assert caught.value.code == 'CLI_RESOURCE_MISMATCH'
    assert len(instance.calls) == 2


@pytest.mark.parametrize('where', ['node', 'resource'])
def test_auth_or_other_read_failure_is_propagated_instead_of_becoming_success(where):
    error = CanvasCLIError('cli.authentication_required', 'restore original login')
    instance = client([error] if where == 'node' else [view(), error])
    with pytest.raises(CanvasCLIError) as caught:
        recover(instance)
    assert caught.value is error


def test_original_deadline_is_never_replaced_or_extended(monkeypatch):
    now = [100.0]
    monkeypatch.setattr('app.canvas_client.time.monotonic', lambda: now[0])
    instance = client([view(), facts()])
    instance.original_deadline = 101.0
    actual = instance._call
    def late(args, **kwargs):
        value = actual(args, **kwargs)
        now[0] = 102.0
        return value
    instance._call = late
    with pytest.raises(CanvasCLIError) as caught:
        recover(instance)
    assert caught.value.code == 'CLI_TASK_DEADLINE_EXCEEDED'
    assert len(instance.calls) == 1


def test_profile_rotation_between_node_and_resource_read_stops_recovery():
    instance = client([view(), facts()])
    actual = instance._call
    def rotate(args, **kwargs):
        result = actual(args, **kwargs)
        instance.test_profile[0] = 'new-profile'
        return result
    instance._call = rotate
    with pytest.raises(CanvasCLIError) as caught:
        recover(instance)
    assert caught.value.code == 'CLI_ACCOUNT_MISMATCH'
    assert len(instance.calls) == 1
