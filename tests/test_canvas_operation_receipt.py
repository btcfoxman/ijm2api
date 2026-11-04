"""A bound status receipt is durable before slow original-output observation."""
from contextlib import nullcontext
from copy import deepcopy
from types import SimpleNamespace

import pytest

from app.canvas_cli import CanvasCLIError
from app.canvas_client import CanvasClient
from app.canvas_resource_recovery import (
    OPERATION_RECEIPT_SOURCE, accepted_output_context, operation_receipt, operation_receipt_valid)
from test_canvas_resource_recovery import context as accepted_context, PROJECT, SUBMIT, RID, OTHER, NODE


def uncertain():
    value = accepted_context()
    value.update(phase='quoted', submission_phase='run_dispatching', resource_ids=[],
        resources={'images': [PROJECT], 'videos': [], 'audios': []},
        submit_error={'code': 'service.1', 'class': 'retryable_failure', 'requiredAction': 'retry',
                      'retryable': True, 'serviceCode': 1, 'outcome_unknown': True})
    value['submission_evidence'].update(accepted=False, output_resources=False)
    return value


def operation(*, state='succeeded', resources=None):
    return {'operationRef': SUBMIT, 'projectId': PROJECT, 'nodeId': NODE, 'state': state,
        'resources': [{'resourceId': RID, 'state': 'succeeded'}] if resources is None else resources}


def test_receipt_is_acceptance_and_original_locators_not_a_terminal_download_binding():
    bound = uncertain()
    receipt = operation_receipt(bound, operation())
    assert receipt['source'] == OPERATION_RECEIPT_SOURCE and receipt['resource_ids'] == [RID]
    assert operation_receipt_valid(receipt, bound) and not operation_receipt_valid(receipt, bound, durable=True)
    assert 'durable' not in receipt and 'output_binding' not in receipt
    assert bound['resource_ids'] == [] and bound['submit_error']['outcome_unknown'] is True


@pytest.mark.parametrize('state', ['running', 'succeeded', None])
def test_optional_resource_state_never_substitutes_for_terminal_resource_get(state):
    resource = {'resourceId': RID}
    if state is not None: resource['state'] = state
    receipt = operation_receipt(uncertain(), operation(resources=[resource]))
    assert receipt and receipt['resource_states'] == [{'resource_id': RID, 'state': state}]


def test_completed_accepted_submission_summary_can_supply_original_locators():
    value = {**operation(), 'submission': {'state': 'completed', 'resubmittable': False,
                                          'adviceCode': 'submission_accepted'}}
    assert operation_receipt(uncertain(), value)


@pytest.mark.parametrize('submission', [
    {'state': 'rejected'}, {'state': 'failed'}, {'state': 'partial'}, {'state': 'absent'},
    {'state': 'completed', 'resubmittable': True}, {'adviceCode': 'submission_rejected'},
    {'adviceCode': 'submission_absent'}, {'identity_conflict': True},
])
def test_contradictory_nested_submission_stops_instead_of_falling_back_to_delivery(submission):
    with pytest.raises(CanvasCLIError) as found:
        operation_receipt(uncertain(), {**operation(), 'submission': submission})
    assert found.value.code in {'CLI_INVALID_RESPONSE', 'CLI_RESOURCE_MISMATCH'}


@pytest.mark.parametrize('change', [
    {'operationRef': OTHER}, {'state': 'absent'}, {'state': 'unknown'}, {'state': 'failed'},
    {'submissionError': {'serviceCode': 20115}}, {'resources': []},
    {'resources': [{'resourceId': RID, 'state': 'failed'}]},
    {'resources': [{'resourceId': RID, 'state': 'unknown'}]},
    {'resources': [{'resourceId': RID, 'state': {}}]},
    {'resources': [{'resourceId': {}, 'state': 'succeeded'}]},
    {'resources': [{'resourceId': RID, 'state': 'succeeded'}, {'resourceId': RID, 'state': 'succeeded'}]},
])
def test_absent_unknown_partial_failed_or_malformed_status_cannot_grant_acceptance(change):
    assert operation_receipt(uncertain(), {**operation(), **deepcopy(change)}) is None


@pytest.mark.parametrize('change', [
    {'projectId': OTHER}, {'nodeId': 'node_0000000000'}, {'submitId': OTHER},
    {'userId': 'foreign-user'}, {'identity_conflict': True},
    {'resources': [{'resourceId': PROJECT, 'state': 'succeeded'}]},
    {'resources': [{'resourceId': RID, 'state': 'succeeded', 'type': 'image'}]},
])
def test_explicit_operation_identity_or_input_output_conflict_stops_before_persistence(change):
    with pytest.raises(CanvasCLIError) as found:
        operation_receipt(uncertain(), {**operation(), **deepcopy(change)})
    assert found.value.code == 'CLI_RESOURCE_MISMATCH'


@pytest.mark.parametrize('change', [
    {'manual_termination': {}}, {'previous_submissions': [{'submit_id': OTHER}]},
    {'submit_error': {'code': 'CLI_ACCOUNT_MISMATCH'}},
    {'submit_error': {'class': 'business_failure'}}, {'submit_error': {'requiredAction': 'login'}},
    {'output_count': True}, {'resource_ids': [OTHER]},
])
def test_original_context_conflicts_do_not_become_a_receipt(change):
    assert operation_receipt({**uncertain(), **deepcopy(change)}, operation()) is None


@pytest.mark.parametrize('change', [
    {'source': 'node_journal'}, {'version': True}, {'output_count': True}, {'account_id': True},
    {'operation_state': {}}, {'resource_ids': [{}]}, {'resource_states': [{'resource_id': RID, 'state': []}]},
    {'resource_states': [{'resource_id': RID, 'state': 'failed'}]}, {'untrusted': True},
])
def test_receipt_validator_rejects_forged_or_malformed_proof(change):
    bound = uncertain()
    receipt = operation_receipt(bound, operation())
    assert not operation_receipt_valid({**receipt, **deepcopy(change)}, bound)


@pytest.fixture
def case(monkeypatch):
    clock = {'now': 100.0}
    timer = SimpleNamespace(monotonic=lambda: clock['now'],
                            sleep=lambda seconds: clock.update(now=clock['now'] + seconds))
    monkeypatch.setattr('app.canvas_client.time', timer)
    monkeypatch.setattr('app.canvas_resource_recovery.time', timer)
    bound = uncertain()
    saved, terminal, downloaded = [], [], []

    class Runner:
        timeout = 120
        def __init__(self):
            self.calls = []
            self.operation_seconds = 10.2
            self.resource_seconds = 8
            self.resource_error = None
            self.response = operation()
            self.facts = {'resourceId': RID, 'type': 'video', 'status': 'succeeded'}
        def profile(self, account): return 'original-profile'
        def account_guard(self, account, **kwargs): return nullcontext()
        def run(self, account, args, **kwargs):
            pair = tuple(args[:2])
            assert pair in {('operation', 'status'), ('resource', 'get')}, 'No paid/write command'
            self.calls.append((list(args), dict(kwargs)))
            seconds = self.operation_seconds if pair == ('operation', 'status') else self.resource_seconds
            if pair == ('resource', 'get'):
                assert saved and accepted_output_context(bound), 'Acceptance must be durable before resource I/O'
            if seconds > kwargs['timeout']:
                clock['now'] += kwargs['timeout']
                raise CanvasCLIError('CLI_COMMAND_TIMEOUT', 'Synthetic process deadline')
            clock['now'] += seconds
            if pair == ('resource', 'get') and self.resource_error:
                raise self.resource_error
            value = deepcopy(self.response if pair == ('operation', 'status') else self.facts)
            if pair == ('resource', 'get'): value['resourceId'] = args[2]
            return {'ok': True, 'data': value}

    runner = Runner()
    account = {'id': 19, 'login_user_id': 'original-user', 'cli_user_id': 'original-user'}
    def client():
        result = CanvasClient(runner, account)
        def download(context, identifier, kind, **kwargs):
            assert terminal and identifier in context['resource_ids']
            downloaded.append(identifier)
            return {'path': '/synthetic/original.mp4'}
        result.download_result = download
        return result
    def persist(receipt):
        assert operation_receipt_valid(receipt, bound)
        assert not any(args[:2] == ['resource', 'get'] for args, _ in runner.calls)
        saved.append(deepcopy(receipt))
        return True
    def persist_terminal(receipt):
        terminal.append(deepcopy(receipt)); return True
    return SimpleNamespace(clock=clock, runner=runner, bound=bound, saved=saved, terminal=terminal,
        downloaded=downloaded, client=client, persist=persist, persist_terminal=persist_terminal)


def observe(case, **options):
    return case.client().poll(case.bound, deadline=200, persist_operation_receipt=case.persist,
        persist_output_binding=case.persist_terminal, **options)


def test_slow_status_persists_original_ids_then_gets_a_fresh_shared_resource_budget(case):
    before = deepcopy(case.bound['submit_error'])
    result = observe(case)
    assert result['status'] == 'succeeded' and case.clock['now'] == 118.2
    assert len(case.saved) == len(case.terminal) == len(case.downloaded) == 1
    assert case.bound['submit_error'] == before and accepted_output_context(case.bound)
    assert case.bound['submission_evidence']['receipt'] is True
    assert case.bound['operation_receipt'] == {**case.saved[0], 'durable': True}
    assert [options['deadline'] for _, options in case.runner.calls] == [115, 140.2]
    assert case.terminal[0]['source'] == 'accepted_receipt_resource_get'


@pytest.mark.parametrize('state', ['running', None])
def test_receipt_with_running_or_missing_item_state_still_requires_fresh_terminal_resource(case, state):
    resource = {'resourceId': RID}
    if state is not None: resource['state'] = state
    case.runner.response = operation(state='running', resources=[resource])
    result = observe(case)
    assert result['status'] == 'succeeded'
    assert len(case.saved) == len(case.terminal) == len(case.downloaded) == 1
    assert case.saved[0]['resource_states'] == [{'resource_id': RID, 'state': state}]
    assert sum(args[:2] == ['resource', 'get'] for args, _ in case.runner.calls) == 1


def test_later_resource_timeout_cannot_erase_receipt_and_next_poll_skips_operation(case):
    case.runner.resource_error = CanvasCLIError('CLI_COMMAND_TIMEOUT', 'Temporary original resource read')
    with pytest.raises(CanvasCLIError): observe(case)
    assert case.saved and case.bound['resource_ids'] == [RID] and not case.terminal and not case.downloaded
    original_operations = sum(args[:2] == ['operation', 'status'] for args, _ in case.runner.calls)
    case.runner.resource_error = None
    case.runner.resource_seconds = 1
    result = observe(case)
    assert result['status'] == 'succeeded' and len(case.saved) == 1
    assert sum(args[:2] == ['operation', 'status'] for args, _ in case.runner.calls) == original_operations == 1


@pytest.mark.parametrize('outcome', [False, RuntimeError('Synthetic store outage')])
def test_failed_acceptance_persistence_prevents_resource_reads_and_delivery(case, outcome):
    original = deepcopy(case.bound)
    def reject(receipt):
        if isinstance(outcome, Exception): raise outcome
        return outcome
    with pytest.raises(CanvasCLIError) as found:
        case.client().poll(case.bound, deadline=200, persist_operation_receipt=reject,
                           persist_output_binding=case.persist_terminal)
    assert found.value.code in {'CLI_STOPPING', 'CLI_OUTPUT_BINDING_PERSIST_FAILED'}
    assert case.bound == original and not case.terminal and not case.downloaded
    assert all(args[:2] == ['operation', 'status'] for args, _ in case.runner.calls)


def test_real_outer_deadline_is_not_extended_even_after_acceptance_is_durable(case):
    case.runner.resource_seconds = 20
    with pytest.raises(CanvasCLIError) as found:
        case.client().poll(case.bound, deadline=120, persist_operation_receipt=case.persist,
                           persist_output_binding=case.persist_terminal)
    assert found.value.code == 'CLI_TASK_DEADLINE_EXCEEDED' and case.clock['now'] == 120
    assert case.saved and case.bound['resource_ids'] == [RID] and not case.downloaded
    assert case.runner.calls[-1][1]['deadline'] == 120


def test_cold_original_resource_read_can_use_the_whole_remaining_shared_phase(case):
    case.runner.resource_seconds = 18
    assert observe(case)['status'] == 'succeeded'
    assert case.clock['now'] == 128.2
    assert case.runner.calls[-1][1]['timeout'] == pytest.approx(30, rel=0, abs=1e-9)


def test_four_outputs_share_one_fixed_sixty_second_resource_phase_after_status(case):
    identifiers = [RID, OTHER, '55555555-5555-4555-8555-555555555555', '66666666-6666-4666-8666-666666666666']
    case.bound['output_count'] = 4
    case.runner.response = operation(resources=[{'resourceId': value, 'state': 'succeeded'} for value in identifiers])
    case.runner.operation_seconds = 14
    case.runner.resource_seconds = 16
    with pytest.raises(CanvasCLIError) as found: observe(case)
    assert found.value.code == 'CLI_TASK_DEADLINE_EXCEEDED' and case.clock['now'] == 174
    assert case.saved[0]['resource_ids'] == identifiers and not case.terminal and not case.downloaded
    resource_calls = [options for args, options in case.runner.calls if args[:2] == ['resource', 'get']]
    assert {options['deadline'] for options in resource_calls} == {174}
    assert [options['timeout'] for options in resource_calls] == [60, 44, 28, 12]


def test_resource_auth_or_foreign_binding_is_not_hidden_by_a_valid_operation_receipt(case):
    case.runner.facts['submitId'] = OTHER
    with pytest.raises(CanvasCLIError) as found: observe(case)
    assert found.value.code == 'CLI_RESOURCE_MISMATCH' and case.saved and not case.downloaded


def test_contradictory_admission_summary_cannot_persist_or_download_even_with_successful_resources(case):
    case.runner.response['submission'] = {'state': 'absent', 'resubmittable': True,
                                         'adviceCode': 'submission_absent'}
    with pytest.raises(CanvasCLIError) as found: observe(case)
    assert found.value.code == 'CLI_INVALID_RESPONSE'
    assert not case.saved and not case.terminal and not case.downloaded
    assert all(args[:2] == ['operation', 'status'] for args, _ in case.runner.calls)
