"""Only positive zero-run preparation failures can consume bounded retries."""
from copy import deepcopy
import time
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from app.canvas_cli import CanvasCLIError
from app.canvas_client import CanvasClient
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


def test_long_initial_preparation_gets_a_retry_window_after_its_first_transient_failure(context, monkeypatch):
    store, service, _ = context
    store.update_settings({'task_timeout_seconds': 3000})
    account = owner(store, balance=200)
    task(store)
    client = fake_client(store, service, account, quote=170)
    original = client.prepare.side_effect
    clock = {'wall': time.time(), 'mono': time.monotonic()}
    started = clock['wall']
    monkeypatch.setattr('app.canvas_service.time.time', lambda: clock['wall'])
    monkeypatch.setattr('app.canvas_service.time.monotonic', lambda: clock['mono'])
    def tick(delay):
        clock['wall'] += delay
        clock['mono'] += delay
        return False
    service._stop.wait.side_effect = tick
    calls = []
    def prepare(*args, **kwargs):
        calls.append(kwargs['deadline'])
        result = original(*args, **kwargs)
        if len(calls) == 1:
            tick(240)
            raise transient()
        return result
    client.prepare.side_effect = prepare
    with patch.object(service, 'canvas_client', return_value=client):
        service._run('task')
    row = store.task('task')
    budget = row['upstream_context']['preparation_retry_budget']
    assert row['status'] == 'succeeded' and client.submit.call_count == 1
    assert budget['version'] == 2 and budget['admitted_at'] == started
    assert budget['started_at'] == started + 240 and budget['deadline'] == started + 420
    assert budget['task_deadline'] == started + 3000
    assert len(budget['attempts']) == 2 and calls[1] <= calls[0]


def test_expired_legacy_budget_is_not_replaced_even_after_long_initial_work(context, monkeypatch):
    store, service, _ = context
    account = owner(store)
    task(store)
    budget = new_budget('task', admitted_at=time.time() - 240)
    store.update_task('task', {'account_id': account['id'], 'status': 'preparing',
        'upstream_context': {'preparation_retry_budget': budget}})
    client = fake_client(store, service, account)
    with patch.object(service, 'canvas_client', return_value=client):
        service._execute_canvas('task', account, time.monotonic() + 500)
    assert store.task('task')['upstream_context']['preparation_retry_budget'] == budget
    assert store.task('task')['error_code'] == 'CLI_PREPARATION_RETRY_EXHAUSTED'
    client.identity.assert_not_called()
    client.prepare.assert_not_called()
    client.submit.assert_not_called()


def upload_candidate(account, task_id='task'):
    return {'version': 1, 'backend': 'canvas_cli', 'binding': {'task_id': task_id,
        'account_id': account['id'], 'profile_sha256': '1' * 64, 'user_id_sha256': '2' * 64,
        'proxy_sha256': '3' * 64, 'project_id': 'private-project', 'request_sha256': '4' * 64,
        'media_sha256': '5' * 64}, 'refs': [{'group': 'images', 'index': 0,
            'resource_id': 'private-uploaded-input', 'source_kind': 'url',
            'source_sha256': '6' * 64, 'verified': True}]}


def test_same_account_retry_retains_only_upload_candidate_without_execution_injection(context):
    store, service, _ = context
    account = owner(store, balance=200)
    task(store)
    client = fake_client(store, service, account, quote=170)
    original = client.prepare.side_effect
    checkpoint = upload_candidate(account)
    received = []
    def prepare(normalized, task_id, assets, *, persist, deadline, checkpoint=None):
        assert '_execution' not in normalized
        received.append(deepcopy(checkpoint))
        result = original(normalized, task_id, assets, persist=persist, deadline=deadline)
        persist({'upload_checkpoint': upload_candidate(account)})
        if len(received) == 1:
            raise transient()
        return result
    client.prepare.side_effect = prepare
    with patch.object(service, 'canvas_client', return_value=client):
        service._run('task')
    assert received == [None, checkpoint]
    assert store.task('task')['status'] == 'succeeded'
    client.submit.assert_called_once()


def test_replacement_account_never_receives_the_previous_accounts_material_binding(context):
    store, service, _ = context
    first = owner(store, 'first', balance=200, priority=2)
    second = owner(store, 'second', balance=200)
    task(store)
    clients = {a['id']: fake_client(store, service, a, quote=170) for a in (first, second)}
    received = {first['id']: [], second['id']: []}
    for account in (first, second):
        original = clients[account['id']].prepare.side_effect
        def prepare(normalized, task_id, assets, *, persist, deadline, checkpoint=None,
                    account=account, original=original):
            assert '_execution' not in normalized
            received[account['id']].append(deepcopy(checkpoint))
            result = original(normalized, task_id, assets, persist=persist, deadline=deadline)
            persist({'upload_checkpoint': upload_candidate(account)})
            if account['id'] == first['id']:
                raise transient()
            return result
        clients[account['id']].prepare.side_effect = prepare
    with patch.object(service, 'canvas_client', side_effect=lambda account: clients[account['id']]):
        service._run('task')
    row = store.task('task')
    assert row['status'] == 'succeeded' and row['account_id'] == second['id']
    assert received[first['id']] == [None, upload_candidate(first)]
    assert received[second['id']] == [None]
    clients[first['id']].submit.assert_not_called()
    clients[second['id']].submit.assert_called_once()


def test_cancel_during_retry_wait_preserves_checkpoint_without_another_preparation(context):
    store, service, _ = context
    account = owner(store)
    task(store)
    client = fake_client(store, service, account)
    original = client.prepare.side_effect
    def prepare(*args, **kwargs):
        original(*args, **kwargs)
        kwargs['persist']({'upload_checkpoint': upload_candidate(account)})
        raise transient()
    client.prepare.side_effect = prepare
    def stop_wait(_):
        current = store.task('task')
        store.update_task('task', {'status': 'failed', 'error_code': 'MANUAL_TERMINATION',
            'upstream_context': {**current['upstream_context'], 'manual_termination': {'at': time.time()}}})
        return False
    service._stop.wait.side_effect = stop_wait
    with patch.object(service, 'canvas_client', return_value=client):
        service._run('task')
    row = store.task('task')
    assert row['error_code'] == 'MANUAL_TERMINATION'
    assert row['upstream_context']['upload_checkpoint'] == upload_candidate(account)
    client.prepare.assert_called_once()
    client.submit.assert_not_called()


def test_low_credit_first_account_then_transient_replacement_uses_three_total_tickets(context):
    store, service, _ = context
    first = owner(store, 'low-credit', balance=100, priority=2)
    second = owner(store, 'replacement', balance=200)
    task(store)
    clients = {a['id']: fake_client(store, service, a, quote=170) for a in (first, second)}
    # Record an authoritative successful quote in the first completed audit,
    # without invoking the CLI. This is the evidence required to import it.
    clients[first['id']].__class__ = CanvasClient
    first_prepare = clients[first['id']].prepare.side_effect
    def quoted(*args, **kwargs):
        result = first_prepare(*args, **kwargs)
        clients[first['id']].preparation_timing.command(SimpleNamespace(), first,
            ['node', 'quote'], {}, 1, lambda: {'totalMaxCredits': 170})
        return result
    clients[first['id']].prepare.side_effect = quoted
    prepare_failure(clients[second['id']], successes_after=1)
    with patch.object(service, 'canvas_client', side_effect=lambda account: clients[account['id']]):
        service._run('task')
    row = store.task('task')
    assert row['status'] == 'succeeded', row['error_message']
    budget = row['upstream_context']['preparation_retry_budget']
    assert [entry['account_id'] for entry in budget['attempts']] == [first['id'], second['id'], second['id']]
    assert row['upstream_context']['preparation_attempt_count'] == 3
    assert clients[first['id']].prepare.call_count == 1
    clients[first['id']].submit.assert_not_called()
    assert clients[second['id']].prepare.call_count == 2
    clients[second['id']].submit.assert_called_once()


@pytest.mark.parametrize('replacement', [False, True])
def test_retry_admission_cutoff_does_not_replace_original_generation_observation_deadline(context, monkeypatch, replacement):
    store, service, _ = context
    store.update_settings({'task_timeout_seconds': 3000})
    first = owner(store, 'first', balance=200, priority=2)
    second = owner(store, 'second', balance=200) if replacement else first
    task(store)
    clock = {'wall': time.time(), 'mono': time.monotonic()}
    started = dict(clock)
    monkeypatch.setattr('app.canvas_service.time.time', lambda: clock['wall'])
    monkeypatch.setattr('app.canvas_service.time.monotonic', lambda: clock['mono'])
    def tick(delay):
        clock['wall'] += delay
        clock['mono'] += delay
        return False
    service._stop.wait.side_effect = tick
    accounts = {first['id']: first, second['id']: second}
    clients = {a['id']: fake_client(store, service, a, quote=170) for a in accounts.values()}
    original = clients[first['id']].prepare.side_effect
    preparations = []
    def prepare(*args, **kwargs):
        preparations.append(kwargs['deadline'])
        result = original(*args, **kwargs)
        if len(preparations) == 1:
            tick(240)
        if len(preparations) <= (2 if replacement else 1):
            raise transient()
        return result
    clients[first['id']].prepare.side_effect = prepare
    if replacement:
        other = clients[second['id']].prepare.side_effect
        def prepare_second(*args, **kwargs):
            preparations.append(kwargs['deadline'])
            return other(*args, **kwargs)
        clients[second['id']].prepare.side_effect = prepare_second
    observed = []
    def poll(task_id, client, deadline):
        observed.append(deadline)
        store.update_task(task_id, {'status': 'succeeded', 'progress': 100})
    monkeypatch.setattr(service, '_poll_canvas', poll)
    with patch.object(service, 'canvas_client', side_effect=lambda account: clients[account['id']]):
        service._run('task')
    row = store.task('task')
    budget = row['upstream_context']['preparation_retry_budget']
    assert row['status'] == 'succeeded'
    assert budget['task_deadline'] == started['wall'] + 3000
    assert observed == [pytest.approx(started['mono'] + 3000)]
    assert row['upstream_context']['observation_deadline'] == pytest.approx(budget['task_deadline'])
    retry_limit = started['mono'] + 420
    assert all(deadline <= retry_limit for deadline in preparations[1:])
    final_submit = clients[second['id']].submit
    final_submit.assert_called_once()
    assert final_submit.call_args.kwargs['deadline'] <= retry_limit


def pending_candidate(account):
    checkpoint = upload_candidate(account)
    ref = checkpoint['refs'].pop()
    ref.pop('verified')
    checkpoint['pending_refs'] = [{**ref, 'observe_only': True, 'registration': 'unknown'}]
    return checkpoint


def test_pending_upload_exhausts_original_account_preparations_without_switching_accounts(context):
    store, service, _ = context
    first = owner(store, 'original', balance=200, priority=2)
    second = owner(store, 'other', balance=200)
    task(store)
    clients = {a['id']: fake_client(store, service, a, quote=170) for a in (first, second)}
    original = clients[first['id']].prepare.side_effect
    received = []
    def prepare(*args, checkpoint=None, **kwargs):
        received.append(deepcopy(checkpoint))
        original(*args, **kwargs)
        kwargs['persist']({'upload_checkpoint': pending_candidate(first)})
        raise transient()
    clients[first['id']].prepare.side_effect = prepare
    with patch.object(service, 'canvas_client', side_effect=lambda account: clients[account['id']]):
        service._run('task')
    row = store.task('task')
    assert row['status'] == 'failed' and row['error_code'] == 'CLI_PREPARATION_RETRY_EXHAUSTED'
    assert row['account_id'] == first['id'] and row['actual_cost'] is None
    assert received == [None, pending_candidate(first)]
    assert len(row['upstream_context']['preparation_retry_budget']['attempts']) == 2
    assert row['upstream_context']['upload_checkpoint'] == pending_candidate(first)
    clients[second['id']].identity.assert_not_called()
    clients[second['id']].prepare.assert_not_called()
    for client in clients.values(): client.submit.assert_not_called()


@pytest.mark.parametrize('case', ['foreign', 'shape', 'paid'])
def test_pending_upload_entry_cannot_discard_binding_or_change_paid_recovery(context, case):
    store, service, _ = context
    first = owner(store, 'original')
    other = owner(store, 'different')
    task(store)
    checkpoint = pending_candidate(first)
    context_value = {'upload_checkpoint': checkpoint}
    account = other if case == 'foreign' else first
    if case == 'shape': checkpoint['pending_refs'] = None
    if case == 'paid':
        context_value.update(submission_phase='unknown', submit_error={'outcome_unknown': True})
    store.update_task('task', {'account_id': account['id'], 'status': 'submitted' if case == 'paid' else 'preparing',
        'execution_backend': 'canvas_cli', 'error_code': 'UPSTREAM_SUBMIT_OUTCOME_UNKNOWN' if case == 'paid' else '',
        'upstream_context': context_value})
    before = store.task('task')
    factory = Mock(side_effect=AssertionError('An unsafe pending binding cannot run client commands'))
    with patch.object(service, 'canvas_client', factory):
        service._execute_canvas('task', account, time.monotonic() + 60)
    row = store.task('task')
    if case == 'paid': assert row == before
    else: assert row['status'] == 'failed' and row['error_code'] == 'CLI_PREPARATION_RETRY_EXHAUSTED'
    assert row['upstream_context']['upload_checkpoint'] == checkpoint
    factory.assert_not_called()


@pytest.mark.parametrize('stage', ['identity', 'model'])
def test_identity_or_model_failure_cannot_reselect_away_from_pending_uploads(context, stage):
    store, service, _ = context
    first = owner(store, 'original', priority=2)
    second = owner(store, 'different')
    task(store)
    store.update_task('task', {'upstream_context': {'upload_checkpoint': pending_candidate(first)}})
    clients = {a['id']: fake_client(store, service, a) for a in (first, second)}
    if stage == 'identity':
        clients[first['id']].identity.side_effect = CanvasCLIError('CLI_COMMAND_TIMEOUT', 'identity read timed out')
    else:
        clients[first['id']].preflight_model.side_effect = CanvasCLIError('CLI_CAPABILITY_UNAVAILABLE', 'model unavailable',
            details={'error': {'scope': 'account', 'reason': 'model_not_in_catalog',
                               'requested_model': 'sd-2-0-mini', 'kind': 'video'}})
    with patch.object(service, 'canvas_client', side_effect=lambda account: clients[account['id']]):
        service._run('task')
    row = store.task('task')
    assert row['status'] == 'failed' and row['account_id'] == first['id']
    assert row['upstream_context']['upload_checkpoint'] == pending_candidate(first)
    assert not row['upstream_context'].get('preflight_failures')
    assert not row['upstream_context'].get('model_eligibility_exclusions')
    clients[second['id']].identity.assert_not_called()
    for client in clients.values():
        client.prepare.assert_not_called()
        client.submit.assert_not_called()
