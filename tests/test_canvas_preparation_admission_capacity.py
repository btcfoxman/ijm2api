"""A spent preparation ledger cannot bind a third account or another run."""
from copy import deepcopy
import time
from unittest.mock import patch

import pytest

from app.canvas_retry_budget import budget_token, claim_attempt, new_retry_budget
from test_canvas_execution import context, owner, task, fake_client
from test_canvas_preparation_retry_integration import transient


def create_task(store, service, kind):
    store.update_settings({'task_timeout_seconds': 3000})
    return (service.create_image({'model': 'gpt-image-2.5-flare', 'prompt': 'Synthetic landscape'})['id']
            if kind == 'image' else task(store)['id'])


def restore_budget(store, identifier, owners, *, error='INSUFFICIENT_CREDITS'):
    now = time.time()
    budget = new_retry_budget(identifier, admitted_at=now - 2, retry_started_at=now - 1,
                              task_deadline=now + 3000)
    proof = {'stage': 'preparation', 'node_run_count': 0, 'generation_request_sent': False,
             'dispatch_started': False, 'outcome_unknown': False, 'prior_dispatch_evidence': False}
    for account_id in owners:
        budget = claim_attempt(budget, task_id=identifier, account_id=account_id, now=now,
            expected_token=budget_token(budget, task_id=identifier), proof=proof)
    store.update_task(identifier, {'execution_backend': 'canvas_cli', 'error_code': error,
        'error_message': 'Canvas quote exceeds available unreserved credits',
        'upstream_context': {'preparation_retry_budget': budget, 'preparation_admission_at': now - 2}})
    return deepcopy(budget)


@pytest.mark.parametrize('kind', ['video', 'image'])
def test_real_scheduler_stops_after_first_first_second_without_binding_third(context, kind):
    store, service, _ = context
    accounts = [owner(store, name, balance=200, priority=priority)
                for name, priority in [('first', 3), ('second', 2), ('third', 1)]]
    first, second, third = accounts
    identifier = create_task(store, service, kind)
    request = deepcopy(store.task(identifier)['request'])
    clients = {a['id']: fake_client(store, service, a, quote=170, kind=kind) for a in accounts}
    for account in (first, second):
        clients[account['id']].identity.side_effect = (
            lambda _owner=account, **kwargs: {'user_id': _owner['login_user_id'], 'available_balance': 100})
    original = clients[first['id']].prepare.side_effect
    calls = []
    def prepare(*args, **kwargs):
        result = original(*args, **kwargs)
        calls.append(result)
        if len(calls) == 1:
            raise transient()
        return result
    clients[first['id']].prepare.side_effect = prepare
    reserved = []
    reserve = service._reserve_account
    def record_reservation(*args, **kwargs):
        account = reserve(*args, **kwargs)
        if account:
            reserved.append(account['id'])
        return account
    with patch.object(service, 'canvas_client', side_effect=lambda a: clients[a['id']]), \
            patch.object(service, '_reserve_account', side_effect=record_reservation):
        service._run(identifier)
    row = store.task(identifier)
    assert row['status'] == 'failed' and row['error_code'] == 'INSUFFICIENT_CREDITS'
    assert row['error_message'] == ('Fresh Canvas balance is below the known quote' if kind == 'video'
                                    else 'Canvas quote exceeds available unreserved credits')
    assert [entry['account_id'] for entry in row['upstream_context']['preparation_retry_budget']['attempts']] == [
        first['id'], first['id'], second['id']]
    assert [entry['reason'] for entry in row['upstream_context']['preparation_attempts']] == [
        'command_failed', 'insufficient_credits', 'insufficient_credits']
    assert reserved == [first['id'], second['id']] and row['account_id'] != third['id']
    assert row['request'] == request and row['actual_cost'] is None
    assert service._canvas_public_pre_dispatch_facts(row)['generation_request_sent'] is False
    assert not service._credit_reservations and all(value == 0 for value in service._active.values())
    assert sum(client.prepare.call_count for client in clients.values()) == (1 if kind == 'video' else 3)
    if kind == 'video':
        clients[second['id']].prepare.assert_not_called()
    for client in clients.values():
        client.submit.assert_not_called()
        client.poll.assert_not_called()
    clients[third['id']].identity.assert_not_called()


@pytest.mark.parametrize('kind', ['video', 'image'])
def test_restored_two_owner_ledger_only_admits_the_last_owner_remaining_ticket(context, kind):
    store, service, _ = context
    first = owner(store, 'old-owner', priority=3)
    last = owner(store, 'last-owner', priority=1)
    third = owner(store, 'foreign-owner', priority=4)
    identifier = create_task(store, service, kind)
    old_budget = restore_budget(store, identifier, [first['id'], last['id']])
    clients = {a['id']: fake_client(store, service, a, kind=kind) for a in (first, last, third)}
    assert service._has_eligible_account(0, set(), identifier)
    assert not service._has_eligible_account(0, {last['id']}, identifier)
    assert not service._has_waiting_credit_account(0, {last['id']}, identifier)
    assert service._reserve_account(0, exclude_ids={last['id']}, task_id=identifier) is None
    with patch.object(service, 'canvas_client', side_effect=lambda a: clients[a['id']]):
        service._run(identifier)
    row = store.task(identifier)
    assert row['status'] == 'succeeded' and row['account_id'] == last['id']
    budget = row['upstream_context']['preparation_retry_budget']
    assert [entry['account_id'] for entry in budget['attempts']] == [first['id'], last['id'], last['id']]
    assert budget['attempts'][:2] == old_budget['attempts']
    assert budget['deadline'] == old_budget['deadline'] and budget['started_at'] == old_budget['started_at']
    clients[last['id']].submit.assert_called_once()
    clients[first['id']].prepare.assert_not_called()
    clients[third['id']].prepare.assert_not_called()


@pytest.mark.parametrize('kind', ['video', 'image'])
def test_restored_last_owner_funding_rejection_never_migrates_to_third_owner(context, kind):
    store, service, _ = context
    first = owner(store, 'first', priority=3)
    last = owner(store, 'last', priority=1)
    third = owner(store, 'third', priority=4)
    identifier = create_task(store, service, kind)
    old_budget = restore_budget(store, identifier, [first['id'], last['id']])
    clients = {a['id']: fake_client(store, service, a, quote=170, kind=kind) for a in (first, last, third)}
    with patch.object(service, 'canvas_client', side_effect=lambda a: clients[a['id']]):
        service._run(identifier)
    row = store.task(identifier)
    assert row['status'] == 'failed' and row['error_code'] == 'INSUFFICIENT_CREDITS'
    budget = row['upstream_context']['preparation_retry_budget']
    assert [entry['account_id'] for entry in budget['attempts']] == [first['id'], last['id'], last['id']]
    assert budget['deadline'] == old_budget['deadline']
    clients[last['id']].prepare.assert_called_once()
    clients[first['id']].prepare.assert_not_called()
    clients[third['id']].prepare.assert_not_called()
    for client in clients.values(): client.submit.assert_not_called()


@pytest.mark.parametrize('kind', ['video', 'image'])
def test_exhausted_restored_ledger_fails_before_any_reservation(context, kind):
    store, service, _ = context
    first, last = owner(store, 'first'), owner(store, 'last')
    identifier = create_task(store, service, kind)
    budget = restore_budget(store, identifier, [first['id'], first['id'], last['id']])
    with patch.object(service, '_reserve_account', side_effect=AssertionError('spent ledger reserved an owner')), \
            patch.object(service, 'canvas_client', side_effect=AssertionError('spent ledger touched CLI')):
        service._run(identifier)
    row = store.task(identifier)
    assert row['status'] == 'failed' and row['error_code'] == 'INSUFFICIENT_CREDITS'
    assert row['upstream_context']['preparation_retry_budget'] == budget
    assert row['account_id'] is None and row['actual_cost'] is None
    assert service._canvas_public_pre_dispatch_facts(row) == {}  # History alone is not proof.


@pytest.mark.parametrize('kind', ['video', 'image'])
def test_concurrent_admission_preserves_original_preparing_owner_and_releases_only_waiter(context, kind):
    store, service, _ = context
    original_owner = owner(store, 'original', priority=1)
    waiting_owner = owner(store, 'waiter', priority=2)
    identifier = create_task(store, service, kind)
    original_context = {'backend': 'canvas_cli', 'account_id': original_owner['id'], 'task_id': identifier,
                        'upload_checkpoint': {'pending_refs': [{'resource_id': 'original-input'}]}}
    reserve = service._reserve_account
    admitted = []
    def concurrent_reservation(*args, **kwargs):
        account = reserve(*args, **kwargs)
        store.update_task(identifier, {'status': 'preparing', 'account_id': original_owner['id'],
            'execution_backend': 'canvas_cli', 'upstream_context': original_context})
        admitted.append(deepcopy(store.task(identifier)))
        return account
    with patch.object(service, '_reserve_account', side_effect=concurrent_reservation), \
            patch.object(service, '_execute_canvas', side_effect=AssertionError('stale dispatcher prepared again')):
        service._run(identifier)
    assert store.task(identifier) == admitted[0]
    assert service._active[waiting_owner['id']] == 0 and not service._credit_reservations


@pytest.mark.parametrize('kind', ['video', 'image'])
def test_capacity_spent_between_reservation_and_binding_never_reassigns_owner(context, kind):
    store, service, _ = context
    first = owner(store, 'first', priority=1)
    last = owner(store, 'last', priority=2)
    identifier = create_task(store, service, kind)
    restore_budget(store, identifier, [first['id'], first['id']])
    reserve = service._reserve_account
    committed_budget = []
    def concurrent_reservation(*args, **kwargs):
        account = reserve(*args, **kwargs)
        committed_budget.append(restore_budget(store, identifier, [first['id'], first['id'], last['id']]))
        return account
    with patch.object(service, '_reserve_account', side_effect=concurrent_reservation), \
            patch.object(service, '_execute_canvas', side_effect=AssertionError('spent ledger prepared again')):
        service._run(identifier)
    row = store.task(identifier)
    assert row['status'] == 'failed' and row['error_code'] == 'INSUFFICIENT_CREDITS'
    assert row['account_id'] is None and row['upstream_context']['preparation_retry_budget'] == committed_budget[0]
    assert service._active[last['id']] == 0 and not service._credit_reservations


@pytest.mark.parametrize('kind', ['video', 'image'])
def test_queued_original_dispatch_recovers_original_poll_without_budget_or_binding_change(context, kind):
    store, service, _ = context
    account = owner(store)
    identifier = create_task(store, service, kind)
    restore_budget(store, identifier, [account['id'], account['id'], owner(store, 'other')['id']])
    original_context = store.task(identifier)['upstream_context']
    original_context.update({'backend': 'canvas_cli', 'account_id': account['id'], 'task_id': identifier,
                             'submission_phase': 'run_dispatching', 'submit_id': 'original-submit',
                             'profile': 'original-profile', 'project_id': 'original-project',
                             'node_id': 'original-node'})
    store.update_task(identifier, {'account_id': account['id'], 'upstream_context': original_context})
    before = store.task(identifier)
    with patch.object(service, '_resume_canvas_poll') as resume, \
            patch.object(service, '_reserve_account', side_effect=AssertionError('original operation re-admitted')):
        service._run(identifier)
    resume.assert_called_once_with(identifier)
    assert store.task(identifier) == before


@pytest.mark.parametrize('kind', ['video', 'image'])
@pytest.mark.parametrize('has_budget', [False, True])
def test_old_rejected_history_is_never_mistaken_for_a_current_polling_operation(context, kind, has_budget):
    store, service, _ = context
    account = owner(store)
    identifier = create_task(store, service, kind)
    budget = restore_budget(store, identifier, [account['id']]) if has_budget else None
    original_context = store.task(identifier)['upstream_context']
    original_context['previous_submissions'] = [{'submit_id': 'rejected-old-operation', 'phase': 'rejected'}]
    store.update_task(identifier, {'execution_backend': 'canvas_cli', 'upstream_context': original_context})
    client = fake_client(store, service, account, kind=kind)
    with patch.object(service, '_resume_canvas_poll', side_effect=AssertionError('rejected history was polled')), \
            patch.object(service, 'canvas_client', return_value=client):
        service._run(identifier)
    row = store.task(identifier)
    assert row['status'] in {'failed', 'succeeded'}  # Never silently leave it queued.
    if has_budget:
        assert row['status'] == 'failed' and row['error_code'] == 'CLI_PREPARATION_RETRY_EXHAUSTED'
        assert row['upstream_context'] == original_context
        assert row['upstream_context']['preparation_retry_budget'] == budget
        client.prepare.assert_not_called()
        client.submit.assert_not_called()
        assert service._canvas_public_pre_dispatch_facts(row) == {}
    else:
        assert row['status'] == 'succeeded'
        client.prepare.assert_called_once()
        client.submit.assert_called_once()  # Existing explicit rejection fallback contract.


def submission_marker(**values):
    return {'version': 1, 'accepted': False, 'output_resources': False, 'receipt': False,
            'identity_conflict': False, 'ambiguous_partial': False, **values}


@pytest.mark.parametrize('kind', ['video', 'image'])
@pytest.mark.parametrize('marker', [
    {'operation_ref': 'original-operation'}, {'operationRef': 'original-operation'},
    {'operation_id': 'original-operation'}, {'operationId': 'original-operation'},
    {'submit_error': {'outcome_unknown': True}}, {'receipt': True}, {'submission_receipt_evidence': True},
    {'receipt': {'run_id': 'original-run'}}, {'run_receipt': {'run_id': 'original-run'}},
    {'preparation_timing': {'commands': {'node run': {'count': 1}}}},
    {'preparation_timing': {'current_command': {'command_pair': ['node', 'run']}}},
    *({'submission_evidence': submission_marker(**{key: True})}
      for key in ('accepted', 'output_resources', 'receipt', 'identity_conflict', 'ambiguous_partial')),
    {'submission_evidence': {'version': 1, 'accepted': 'invalid-marker'}},
])
def test_current_operation_markers_without_retry_budget_only_resume_original_query(context, kind, marker):
    store, service, _ = context
    account = owner(store)
    identifier = create_task(store, service, kind)
    bound = {'backend': 'canvas_cli', 'task_id': identifier, 'account_id': account['id'],
             'profile': 'original-profile', 'project_id': 'original-project', 'node_id': 'original-node',
             'submit_id': 'original-submit', **deepcopy(marker)}
    store.update_task(identifier, {'account_id': account['id'], 'execution_backend': 'canvas_cli',
                                   'upstream_context': bound})
    before = store.task(identifier)
    assert service._canvas_dispatch_evidence(before)
    with patch.object(service, '_resume_canvas_poll') as resume, \
            patch.object(service, '_reserve_account', side_effect=AssertionError('current operation reserved again')), \
            patch.object(service, 'canvas_client', side_effect=AssertionError('current operation prepared again')):
        service._run(identifier)
    resume.assert_called_once_with(identifier)
    assert store.task(identifier) == before


@pytest.mark.parametrize('kind', ['video', 'image'])
@pytest.mark.parametrize('field', ['result_urls', 'upstream_result_urls'])
def test_current_result_fields_only_resume_original_without_another_preparation(context, kind, field):
    store, service, _ = context
    account = owner(store)
    identifier = create_task(store, service, kind)
    store.update_task(identifier, {'account_id': account['id'], 'execution_backend': 'canvas_cli',
        field: ['https://results.example.test/original-output'],
        'upstream_context': {'account_id': account['id'], 'task_id': identifier,
            'profile': 'original-profile', 'project_id': 'original-project', 'node_id': 'original-node',
            'submit_id': 'original-submit'}})
    before = store.task(identifier)
    with patch.object(service, '_resume_canvas_poll') as resume, \
            patch.object(service, '_reserve_account', side_effect=AssertionError('completed result reserved again')):
        service._run(identifier)
    resume.assert_called_once_with(identifier)
    assert store.task(identifier) == before


def test_current_dispatch_delegation_ignores_only_the_two_historical_containers(context):
    store, service, _ = context
    identifier = create_task(store, service, 'video')
    history = {'previous_submissions': [{'submit_id': 'old-rejected-operation'}],
        'preparation_attempts': [{'timing': {'commands': {'node run': {'count': 1}}}}],
        'submission_evidence': submission_marker()}
    store.update_task(identifier, {'upstream_context': history})
    before = store.task(identifier)
    assert service._canvas_dispatch_evidence(before)
    assert not service._canvas_current_dispatch(before)
    assert store.task(identifier) == before


@pytest.mark.parametrize('kind', ['video', 'image'])
@pytest.mark.parametrize('has_budget', [False, True])
def test_all_false_submission_marker_does_not_turn_preparation_into_current_dispatch(context, kind, has_budget):
    store, service, _ = context
    account = owner(store)
    identifier = create_task(store, service, kind)
    if has_budget:
        restore_budget(store, identifier, [account['id']])
    bound = store.task(identifier)['upstream_context']
    bound.update({'submission_evidence': submission_marker(), 'receipt': False,
                  'run_receipt': {}, 'submission_receipt_evidence': False,
                  'submit_error': {'outcome_unknown': False}})
    store.update_task(identifier, {'execution_backend': 'canvas_cli', 'upstream_context': bound})
    assert not service._canvas_current_dispatch(store.task(identifier))
    assert not service._canvas_dispatch_evidence(store.task(identifier))
    client = fake_client(store, service, account, kind=kind)
    with patch.object(service, '_resume_canvas_poll', side_effect=AssertionError('false marker was polled')), \
            patch.object(service, 'canvas_client', return_value=client):
        service._run(identifier)
    assert store.task(identifier)['status'] == 'succeeded'
    client.prepare.assert_called_once()
    client.submit.assert_called_once()


@pytest.mark.parametrize('kind', ['video', 'image'])
@pytest.mark.parametrize('state', ['queued_retry', 'preparing_pending', 'submitted_original'])
def test_startup_dispatch_preserves_original_recovery_contract_and_retry_capacity(context, kind, state):
    store, service, _ = context
    first = owner(store, 'first', priority=2)
    last = owner(store, 'last', priority=1)
    identifier = create_task(store, service, kind)
    budget = restore_budget(store, identifier, [first['id'], first['id']])
    original_context = store.task(identifier)['upstream_context']
    if state != 'queued_retry':
        original_context.update({'backend': 'canvas_cli', 'account_id': first['id'], 'task_id': identifier,
            'profile': 'original-profile', 'project_id': 'original-project', 'node_id': 'original-node',
            'submit_id': 'original-submit', 'submission_phase': 'confirming' if state == 'preparing_pending'
            else 'run_dispatching', 'upload_checkpoint': {'pending_refs': [{'resource_id': 'pending-input'}]}})
        store.update_task(identifier, {'status': 'preparing' if state == 'preparing_pending' else 'submitted',
            'account_id': first['id'], 'upstream_context': original_context})
    clients = {a['id']: fake_client(store, service, a, kind=kind) for a in (first, last)}
    # Use the real startup dispatcher and real task/poll entrypoints, with no
    # maintenance threads or external CLI/network operations.
    service._pool.submit.side_effect = lambda fn, *args: fn(*args)
    with patch.object(service.canvas_cli, 'verify_runtime', return_value={'available': True}), \
            patch('app.service.threading.Thread'), \
            patch.object(service, 'canvas_client', side_effect=lambda a: clients[a['id']]):
        service.start()
    row = store.task(identifier)
    if state == 'preparing_pending':
        assert row['status'] == 'failed' and row['error_code'] == 'CLI_PREPARATION_INTERRUPTED'
        assert row['upstream_context'] == original_context
        assert row['actual_cost'] is None
        for client in clients.values():
            client.prepare.assert_not_called()
            client.poll.assert_not_called()
    else:
        assert row['status'] == 'succeeded', row['error_message']
        if state == 'queued_retry':
            assert row['account_id'] == last['id']
            clients[last['id']].prepare.assert_called_once()
            clients[last['id']].submit.assert_called_once()
            after = row['upstream_context']['preparation_retry_budget']
            assert [entry['account_id'] for entry in after['attempts']] == [first['id'], first['id'], last['id']]
            assert after['attempts'][:2] == budget['attempts'] and after['deadline'] == budget['deadline']
        else:
            assert row['account_id'] == first['id']
            assert row['upstream_context']['submit_id'] == original_context['submit_id']
            assert row['upstream_context']['preparation_retry_budget'] == budget
            clients[first['id']].poll.assert_called_once()
            clients[first['id']].prepare.assert_not_called()
            clients[first['id']].submit.assert_not_called()
            clients[last['id']].prepare.assert_not_called()
    assert service._credit_reservations == {} and all(value == 0 for value in service._active.values())
