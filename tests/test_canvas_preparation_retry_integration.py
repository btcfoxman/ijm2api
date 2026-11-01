"""Only positive zero-run preparation failures can consume bounded retries."""
from copy import deepcopy
import time
from unittest.mock import Mock, patch

import pytest

from app.canvas_cli import CanvasCLIError
from app.canvas_retry_budget import budget_token, new_budget, claim_attempt
from app.errors import public_error
from test_canvas_execution import context, owner, task, fake_client


def transient():
    return CanvasCLIError('cli.invalid_service_response',
        'The service returned an invalid response. Contact support with traceId.',
        details={'error': {'code': 'cli.invalid_service_response', 'class': 'internal_failure',
                            'requiredAction': 'contact_support'}}, outcome_unknown=False)


def prepare_failure(client, successes_after=None):
    original = client.prepare.side_effect
    attempts = []
    def prepare(*args, **kwargs):
        attempts.append(kwargs['deadline'])
        if successes_after is None or len(attempts) <= successes_after:
            original(*args, **kwargs)
            raise transient()
        return original(*args, **kwargs)
    client.prepare.side_effect = prepare
    return attempts


def test_one_zero_run_failure_reprepares_same_reserved_account_then_runs_once(context):
    store, service, _ = context
    store.update_settings({'task_timeout_seconds': 3000})
    account = owner(store, balance=200)
    task(store)
    client = fake_client(store, service, account, quote=170)
    deadlines = prepare_failure(client, successes_after=1)
    with patch.object(service, 'canvas_client', return_value=client):
        service._run('task')
    row = store.task('task'); budget = row['upstream_context']['preparation_retry_budget']
    assert row['status'] == 'succeeded' and client.prepare.call_count == 2
    assert client.submit.call_count == 1 and [a['account_id'] for a in budget['attempts']] == [account['id']] * 2
    assert deadlines[1] <= deadlines[0] and budget['deadline'] - budget['started_at'] <= 180
    assert service._credit_reservations == {} and service._active[account['id']] == 0


def test_three_preparations_two_accounts_use_quote_and_stop_without_generation(context):
    store, service, _ = context
    store.update_settings({'task_timeout_seconds': 3000})
    first = owner(store, 'first', balance=200, priority=2)
    poor = owner(store, 'below-quote', balance=100, priority=1)
    second = owner(store, 'second', balance=180, priority=0)
    task(store)
    clients = {a['id']: fake_client(store, service, a, quote=170) for a in (first, poor, second)}
    for client in clients.values(): prepare_failure(client)
    with patch.object(service, 'canvas_client', side_effect=lambda account: clients[account['id']]):
        service._run('task')
    row = store.task('task'); budget = row['upstream_context']['preparation_retry_budget']
    assert row['status'] == 'failed' and row['error_code'] == 'CLI_PREPARATION_RETRY_EXHAUSTED'
    assert [a['account_id'] for a in budget['attempts']] == [first['id'], first['id'], second['id']]
    assert clients[poor['id']].prepare.call_count == 0
    assert sum(client.prepare.call_count for client in clients.values()) == 3
    assert all(client.submit.call_count == 0 for client in clients.values())
    assert row['actual_cost'] is None and row['estimated_cost'] == 170
    assert public_error(row['error_code'], row['error_message'])['message'] == '视频生成提交失败，请稍后重试~'
    assert service._credit_reservations == {} and all(value == 0 for value in service._active.values())


def test_dispatched_unknown_with_no_outputs_never_enters_preparation_retry(context):
    store, service, _ = context
    account = owner(store); task(store)
    client = fake_client(store, service, account)
    client.submit.side_effect = CanvasCLIError('cli.invalid_service_response', 'run result unavailable',
        details={'dispatch_started': True, 'phase': 'run_dispatching'}, outcome_unknown=True)
    client.poll.return_value = {'status': 'unknown'}
    with patch.object(service, 'canvas_client', return_value=client): service._run('task')
    row = store.task('task')
    assert client.prepare.call_count == 1 and client.submit.call_count == 1
    assert 'preparation_retry_budget' not in row['upstream_context']


def test_budget_commit_is_exclusive_even_when_task_clock_has_same_value(context):
    store, service, _ = context
    account = owner(store); row = task(store)
    store.update_task('task', {'account_id': account['id'], 'status': 'preparing'})
    row = store.task('task'); started = time.time()
    base = new_budget('task', admitted_at=started)
    proof = {'stage': 'preparation', 'node_run_count': 0, 'generation_request_sent': False,
             'dispatch_started': False, 'outcome_unknown': False, 'prior_dispatch_evidence': False}
    claimed = claim_attempt(base, task_id='task', account_id=account['id'], now=started,
                            expected_token=budget_token(base, task_id='task'), proof=proof)
    with patch('app.store.time.time', return_value=row['updated_at']):
        assert store.claim_canvas_preparation_budget('task', claimed, expected_updated_at=row['updated_at'],
            expected_token=None, account_id=account['id'], expected_context=row['upstream_context'])
        assert not store.claim_canvas_preparation_budget('task', claimed, expected_updated_at=row['updated_at'],
            expected_token=None, account_id=account['id'], expected_context=row['upstream_context'])


def test_failed_cas_never_starts_an_external_preparation(context):
    store, service, _ = context
    account = owner(store); task(store)
    store.update_task('task', {'account_id': account['id'], 'status': 'preparing',
        'upstream_context': {'preparation_retry_budget': new_budget('task', admitted_at=time.time())}})
    client = fake_client(store, service, account)
    with patch.object(store, 'claim_canvas_preparation_budget', return_value=False), \
            patch.object(service, 'canvas_client', return_value=client):
        service._execute_canvas('task', account, time.monotonic() + 60)
    client.identity.assert_not_called(); client.prepare.assert_not_called(); client.submit.assert_not_called()
    assert store.task('task')['error_code'] == 'CLI_PREPARATION_RETRY_EXHAUSTED'


def test_busy_replacement_account_wait_uses_durable_180_seconds_not_generation_timeout(context, monkeypatch):
    store, service, _ = context
    store.update_settings({'task_timeout_seconds': 3000})
    first = owner(store, 'used', balance=200); owner(store, 'temporarily-busy', balance=200)
    task(store)
    clock = {'wall': time.time(), 'mono': time.monotonic()}
    start = clock['wall']
    proof = {'stage': 'preparation', 'node_run_count': 0, 'generation_request_sent': False,
             'dispatch_started': False, 'outcome_unknown': False, 'prior_dispatch_evidence': False}
    budget = new_budget('task', admitted_at=start)
    for _ in range(2):
        budget = claim_attempt(budget, task_id='task', account_id=first['id'], now=start,
            expected_token=budget_token(budget, task_id='task'), proof=proof)
    store.update_task('task', {'execution_backend': 'canvas_cli', 'attempted_account_ids': [first['id']],
        'estimated_cost': 170, 'upstream_context': {'preparation_admission_at': start,
                                                 'preparation_retry_budget': budget}})
    monkeypatch.setattr('app.service.time.time', lambda: clock['wall'])
    monkeypatch.setattr('app.service.time.monotonic', lambda: clock['mono'])
    def tick(delay): clock['wall'] += delay; clock['mono'] += delay; return False
    service._stop.wait.side_effect = tick
    reserve = Mock(return_value=None)
    monkeypatch.setattr(service, '_reserve_account', reserve)
    monkeypatch.setattr(service, '_has_eligible_account', Mock(return_value=True))
    monkeypatch.setattr(service, '_has_waiting_credit_account', Mock(return_value=True))
    client_factory = Mock(side_effect=AssertionError('No funded slot was acquired'))
    monkeypatch.setattr(service, 'canvas_client', client_factory)
    service._run('task')
    row = store.task('task')
    assert row['error_code'] == 'CLI_PREPARATION_RETRY_EXHAUSTED' and row['status'] == 'failed'
    assert clock['wall'] - start == 180
    assert row['upstream_context']['preparation_retry_budget'] == budget
    assert all(first['id'] in call.kwargs['exclude_ids'] for call in reserve.call_args_list)
    client_factory.assert_not_called()
