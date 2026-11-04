"""Original accepted output observation cannot become another submission."""
from copy import deepcopy
import time

import pytest

from app.canvas_cli import CanvasCLIError
from app.canvas_resource_recovery import SOURCE, accepted_output_context, observe_bound_resources


PROJECT = '11111111-1111-4111-8111-111111111111'
SUBMIT = '22222222-2222-4222-8222-222222222222'
RID = '33333333-3333-4333-8333-333333333333'
OTHER = '44444444-4444-4444-8444-444444444444'
NODE = 'node_0123456789'


def context():
    return {'backend': 'canvas_cli', 'account_id': 19, 'task_id': 'ijm_original',
        'profile': 'original-profile', 'expected_user_id': 'original-user',
        'project_id': PROJECT, 'node_id': NODE, 'submit_id': SUBMIT, 'kind': 'video',
        'phase': 'accepted', 'submission_phase': 'submitted', 'output_count': 1,
        'resource_ids': [RID], 'submission_receipt_evidence': True,
        'submission_evidence': {'version': 1, 'accepted': True, 'output_resources': True,
            'receipt': False, 'identity_conflict': False, 'ambiguous_partial': True}}


class Reader:
    def __init__(self):
        self.account = {'id': 19, 'login_user_id': 'original-user', 'cli_user_id': 'original-user'}
        self.calls = []
        self.facts = {RID: {'resourceId': RID, 'type': 'video', 'status': 'succeeded'}}
        self.failure = None

    @staticmethod
    def _remaining(deadline, timeout=None):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise CanvasCLIError('CLI_TASK_DEADLINE_EXCEEDED', 'deadline')
        return remaining if timeout is None else min(remaining, timeout)

    def _check_context(self, bound, **kwargs):
        self._remaining(kwargs['deadline'])
        assert bound['profile'] == 'original-profile'
        assert bound['account_id'] == self.account['id']

    def _call(self, args, **kwargs):
        assert args[:2] == ['resource', 'get']
        assert kwargs['read_only'] is True
        assert args[-2:] == ['--project-id', PROJECT]
        assert 0 < kwargs['timeout'] <= 60
        assert kwargs['timeout'] <= kwargs['deadline'] - time.monotonic() + 0.01
        self.calls.append((list(args), dict(kwargs)))
        if self.failure:
            raise self.failure
        return deepcopy(self.facts[args[2]])


def observe(reader, bound, **kwargs):
    return observe_bound_resources(reader, bound, deadline=time.monotonic() + 60, **kwargs)


@pytest.mark.parametrize('phase', ['accepted', 'submitted'])
@pytest.mark.parametrize('partial', [False, True])
def test_ordinary_accepted_receipt_does_not_require_a_nonexistent_nested_receipt(phase, partial):
    bound = context(); bound['submission_phase'] = phase
    bound['submission_evidence']['ambiguous_partial'] = partial
    before = deepcopy(bound); reader = Reader()
    result = observe(reader, bound)
    assert result['state'] == 'completed' and result['source'] == SOURCE
    assert result['operationRef'] == SUBMIT and result['resources'][0]['resourceId'] == RID
    assert result['resource_recovery']['receipt_evidence'] == bound['submission_evidence']
    assert reader.calls[0][0] == ['resource', 'get', RID, '--project-id', PROJECT]
    assert reader.calls[0][1]['deadline'] <= time.monotonic() + 30
    assert bound == before


@pytest.mark.parametrize('code', ['CLI_COMMAND_TIMEOUT', 'CLI_INVALID_RESPONSE', 'cli.transport_failed',
    'cli.invalid_service_response', 'cli.operation_submission_absent', 'service.1'])
def test_transient_operation_read_can_observe_existing_original_outputs(code):
    error = CanvasCLIError(code, 'original query failure', details={'error': {
        'class': 'retryable_failure', 'requiredAction': 'retry'}})
    reader = Reader()
    result = observe(reader, context(), error=error)
    assert result['state'] == 'completed' and len(reader.calls) == 1


@pytest.mark.parametrize('facts', [
    {'requiredAction': 'login'}, {'requiredAction': 'fix_input'}, {'class': 'authorization_failure'},
    {'reason': 'identity_mismatch'}, {'reason': 'permission_denied'}, {'reason': 'auth_expired'},
    {'code': 'cli.account_mismatch'}, {'reason': 'content_moderation'}, {'identity_conflict': True}])
def test_network_wrapper_never_bypasses_an_explicit_auth_or_identity_veto(facts):
    reader = Reader()
    error = CanvasCLIError('cli.transport_failed', 'wrapped', details={'error': facts})
    assert observe(reader, context(), error=error) is None
    assert not reader.calls


@pytest.mark.parametrize('change', [
    {'submit_error': {'dispatch_started': True}}, {'manual_termination': {}},
    {'phase': 'quoted'}, {'submission_phase': 'run_dispatching'},
    {'submission_receipt_evidence': False}, {'resource_ids': []}, {'resource_ids': [RID, RID]},
    {'resource_ids': [{}]}, {'output_count': True}, {'output_count': 2},
    {'submit_id': 'not-a-uuid'}, {'node_id': 'foreign-node'}, {'backend': 'jimeng_web'},
    {'previous_submissions': [{'submit_id': OTHER}]}])
def test_partial_or_unbound_context_never_grants_original_resource_observation(change):
    bound = {**context(), **deepcopy(change)}; reader = Reader()
    assert accepted_output_context(bound) is False
    assert observe(reader, bound) is None
    assert not reader.calls


@pytest.mark.parametrize('field', ['accepted', 'output_resources'])
def test_ambiguous_bit_does_not_replace_explicit_acceptance_or_outputs(field):
    bound = context(); bound['submission_evidence'][field] = False; reader = Reader()
    assert observe(reader, bound) is None and not reader.calls


@pytest.mark.parametrize('state', ['pending', 'queued', 'accepted', 'running', 'in_progress', 'processing'])
def test_running_operation_summary_does_not_hide_fresh_completed_resources(state):
    operation = {'operationRef': SUBMIT, 'state': state,
        'resources': [{'resourceId': RID, 'state': 'running'}]}
    reader = Reader(); result = observe(reader, context(), operation=operation)
    assert result['state'] == 'completed' and len(reader.calls) == 1


@pytest.mark.parametrize('state', ['failed', 'rejected', 'canceled', 'completed', 'unknown', 'absent'])
def test_other_operation_verdicts_are_not_silently_overridden(state):
    reader = Reader()
    assert observe(reader, context(), operation={'operationRef': SUBMIT, 'state': state, 'resources': []}) is None
    assert not reader.calls


@pytest.mark.parametrize('field,value', [('projectId', OTHER), ('nodeId', 'node_abcdefghjk'),
    ('submitId', OTHER), ('resourceId', OTHER), ('type', 'image'), ('userId', 'foreign-user'),
    ('source', 'local_upload')])
def test_fresh_resource_identity_conflicts_never_become_a_delivery_binding(field, value):
    reader = Reader(); reader.facts[RID][field] = value
    with pytest.raises(CanvasCLIError) as caught:
        observe(reader, context())
    assert caught.value.code == 'CLI_RESOURCE_MISMATCH'
    assert len(reader.calls) == 1


def test_operation_identity_conflict_is_rejected_before_any_resource_read():
    reader = Reader()
    with pytest.raises(CanvasCLIError):
        observe(reader, context(), operation={'operationRef': OTHER, 'state': 'running', 'resources': []})
    assert not reader.calls


def test_input_resource_cannot_impersonate_generated_output():
    reader = Reader(); bound = context(); bound['resources'] = {'images': [RID]}
    with pytest.raises(CanvasCLIError):
        observe(reader, bound)
    assert not reader.calls


@pytest.mark.parametrize('state,expected', [('running', 'running'), ('queued', 'running'),
    ('failed', 'failed'), ('rejected', 'failed'), ('success', 'completed')])
def test_resource_lifecycle_is_preserved_without_inventing_a_financial_result(state, expected):
    reader = Reader(); reader.facts[RID].update(status=state, errorCode=20114)
    result = observe(reader, context())
    assert result['state'] == expected
    assert result['resources'][0]['errorCode'] == 20114
    assert 'financial_outcome' not in result and 'actual_cost' not in result


def test_batch_observation_is_complete_ordered_and_uses_one_shared_deadline():
    reader = Reader(); bound = context(); bound.update(resource_ids=[OTHER, RID], output_count=2)
    reader.facts[OTHER] = {'resourceId': OTHER, 'type': 'video', 'status': 'running'}
    result = observe(reader, bound)
    assert result['state'] == 'running'
    assert [item['resourceId'] for item in result['resources']] == [OTHER, RID]
    assert reader.calls[0][1]['deadline'] == reader.calls[1][1]['deadline']
    assert result['resource_recovery']['resource_ids'] == [OTHER, RID]
    assert set(result['_observed_resource_facts']) == {OTHER, RID}
    assert result['_observed_resource_facts'][RID] == reader.facts[RID]


def test_four_slow_output_reads_fit_the_shared_count_scaled_budget(monkeypatch):
    clock = {'now': 1000.0}
    monkeypatch.setattr('app.canvas_resource_recovery.time.monotonic', lambda: clock['now'])
    reader = Reader(); bound = context()
    identifiers = [RID, OTHER, PROJECT, SUBMIT]
    bound.update(output_count=4, resource_ids=identifiers)
    for identifier in identifiers:
        reader.facts[identifier] = {'resourceId': identifier, 'type': 'video', 'status': 'succeeded'}
    original_call = reader._call
    def slow_read(args, **kwargs):
        result = original_call(args, **kwargs)
        clock['now'] += 8
        return result
    reader._call = slow_read
    result = observe_bound_resources(reader, bound, deadline=1060.0)
    assert result['state'] == 'completed' and len(reader.calls) == 4
    assert {kwargs['deadline'] for _, kwargs in reader.calls} == {1060.0}
    assert clock['now'] == 1032.0
    assert list(result['_observed_resource_facts']) == identifiers


def test_single_cold_resource_read_can_finish_after_ten_seconds_within_one_shared_window(monkeypatch):
    clock = {'now': 1000.0}
    monkeypatch.setattr('app.canvas_resource_recovery.time.monotonic', lambda: clock['now'])
    reader = Reader()
    original = reader._call
    def cold_read(args, **kwargs):
        value = original(args, **kwargs)
        assert kwargs['timeout'] == 30 and kwargs['deadline'] == 1030
        clock['now'] += 18
        return value
    reader._call = cold_read
    assert observe_bound_resources(reader, context(), deadline=1100)['state'] == 'completed'
    assert clock['now'] == 1018 and len(reader.calls) == 1


def test_four_resource_reads_cannot_renew_the_shared_sixty_second_window(monkeypatch):
    clock = {'now': 1000.0}
    monkeypatch.setattr('app.canvas_resource_recovery.time.monotonic', lambda: clock['now'])
    reader, bound = Reader(), context()
    identifiers = [RID, OTHER, PROJECT, SUBMIT]
    bound.update(output_count=4, resource_ids=identifiers)
    reader.facts.update({identifier: {'resourceId': identifier, 'type': 'video', 'status': 'succeeded'}
                         for identifier in identifiers})
    original = reader._call
    def slow_read(args, **kwargs):
        value = original(args, **kwargs)
        clock['now'] += min(16, kwargs['timeout'])
        return value
    reader._call = slow_read
    with pytest.raises(CanvasCLIError) as found:
        observe_bound_resources(reader, bound, deadline=1200)
    assert found.value.code == 'CLI_TASK_DEADLINE_EXCEEDED' and clock['now'] == 1060
    assert {options['deadline'] for _, options in reader.calls} == {1060}
    assert [options['timeout'] for _, options in reader.calls] == [60, 44, 28, 12]


def test_slow_batch_cannot_extend_the_original_task_deadline(monkeypatch):
    clock = {'now': 1000.0}
    monkeypatch.setattr('app.canvas_resource_recovery.time.monotonic', lambda: clock['now'])
    reader = Reader(); bound = context(); bound.update(output_count=2, resource_ids=[RID, OTHER])
    reader.facts[OTHER] = {'resourceId': OTHER, 'type': 'video', 'status': 'succeeded'}
    original_call = reader._call
    def slow_read(args, **kwargs):
        result = original_call(args, **kwargs)
        clock['now'] += 8
        return result
    reader._call = slow_read
    with pytest.raises(CanvasCLIError) as caught:
        observe_bound_resources(reader, bound, deadline=1010.0)
    assert caught.value.code == 'CLI_TASK_DEADLINE_EXCEEDED'
    assert {kwargs['deadline'] for _, kwargs in reader.calls} == {1010.0}


def test_original_shared_deadline_is_never_renewed():
    reader = Reader()
    with pytest.raises(CanvasCLIError) as caught:
        observe_bound_resources(reader, context(), deadline=time.monotonic() - 1)
    assert caught.value.code == 'CLI_TASK_DEADLINE_EXCEEDED' and not reader.calls


def test_a_failed_resource_read_does_not_return_partial_outputs_or_change_context():
    reader = Reader(); bound = context(); before = deepcopy(bound)
    reader.failure = CanvasCLIError('cli.transport_failed', 'read unavailable')
    with pytest.raises(CanvasCLIError) as caught:
        observe(reader, bound)
    assert caught.value is reader.failure and bound == before
