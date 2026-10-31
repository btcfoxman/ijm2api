"""Configured browser/CLI accounts share only proven-unsubmitted admission retries."""
from copy import deepcopy
import time
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from app.browser import BrowserRejected
from app.canvas_cli import CanvasCLIError
from test_canvas_execution import context, fake_client, owner


@pytest.fixture
def mixed(context, monkeypatch):
    store, service, _ = context
    store.update_settings({'task_timeout_seconds': 30})
    clients, browser_clients, selected, poll_deadlines = {}, {}, [], []
    monkeypatch.setattr(service.canvas_cli, 'run', Mock(side_effect=AssertionError('real CLI forbidden')))
    monkeypatch.setattr(service.browser_runtime, 'acquire', Mock(side_effect=lambda a: (a, False)))
    monkeypatch.setattr(service, 'canvas_client', lambda a: clients[a['id']])
    monkeypatch.setattr('app.service.DreaminaClient', lambda a, **_: browser_clients[a['id']])
    monkeypatch.setattr('app.service.DreaminaImageClient',
        lambda port: next(client for aid, client in browser_clients.items()
                          if store.account(aid)['cdp_port'] == port))

    def browser_poll(identifier, client, deadline):
        poll_deadlines.append(deadline)
        store.update_task(identifier, {'status': 'succeeded', 'progress': 100,
            'result_urls': ['https://synthetic.test/original.mp4'], 'completed_at': time.time()})

    monkeypatch.setattr(service, '_poll', browser_poll)
    monkeypatch.setattr(service, '_poll_image', browser_poll)

    def add(name, *, mode, balance=100, priority=0, fresh=None, kind='video', quote=10):
        account = owner(store, name, balance=balance, priority=priority, mode=mode)
        store.update_account(account['id'], {'cdp_port': 19500 + account['id']})
        account = store.account(account['id'], private=True)
        client = fake_client(store, service, account, kind=kind, quote=quote)
        clients[account['id']] = client
        browser = Mock(account=account)
        browser.account_state.return_value = {'available_balance': balance if fresh is None else fresh}
        browser.upload_inputs.return_value = {'images': [], 'videos': [], 'audios': []}

        def browser_submit(*args, **kwargs):
            selected.append(account['id'])
            return {'run_id': f'run-{account["id"]}', 'thread_id': f'thread-{account["id"]}'}

        browser.submit.side_effect = browser_submit
        browser_clients[account['id']] = browser
        return account

    def create(kind):
        if kind == 'image':
            return service.create_image({'model': 'gpt-image-2.5-flare', 'prompt': 'Synthetic landscape'})['id']
        return service.create({'model': 'sd-2-0-mini', 'duration': 4, 'prompt': 'Synthetic landscape'})['id']

    monkeypatch.setattr(service, '_cost_estimate', Mock(return_value=(10, {'source': 'synthetic-history'})))
    yield SimpleNamespace(store=store, service=service, clients=clients, browser=browser_clients,
                          add=add, create=create, selected=selected, poll_deadlines=poll_deadlines)
    service.canvas_cli.run.assert_not_called()
    assert sum(service._active.values()) == 0
    assert not service._credit_reservations
    assert service._worker_slots._value == 50


@pytest.mark.parametrize('kind', ['video', 'image'])
@pytest.mark.parametrize('first_mode', ['canvas_cli', 'browser'])
def test_proven_credit_rejection_selects_other_mode_with_original_admission_policy(mixed, kind, first_mode):
    other_mode = 'browser' if first_mode == 'canvas_cli' else 'canvas_cli'
    first = mixed.add('first', mode=first_mode, priority=9, kind=kind)
    larger = mixed.add('larger', mode=other_mode, balance=30, priority=5, kind=kind)
    fitting = mixed.add('fitting', mode=other_mode, balance=20, priority=5, kind=kind)
    lower = mixed.add('lower-priority', mode=first_mode, balance=15, priority=4, kind=kind)
    identifier = mixed.create(kind)
    assert mixed.store.task(identifier)['execution_backend'] == ''
    if first_mode == 'canvas_cli':
        def confirmed_rejection(context, ceiling, *, persist, deadline):
            persist({'submission_phase': 'run_dispatching'})
            raise CanvasCLIError('UPSTREAM_REJECTED', 'not enough credits left',
                outcome_unknown=False, details={'dispatch_started': True,
                    'error': {'serviceCode': '1006', 'reason': 'insufficient_balance'}})
        mixed.clients[first['id']].submit.side_effect = confirmed_rejection
    else:
        mixed.browser[first['id']].submit.side_effect = BrowserRejected('Dreamina ret=1006: Not enough credits left')
    mixed.service._run(identifier)
    result = mixed.store.task(identifier)
    assert result['status'] == 'succeeded', result['error_message']
    # Browser-image rejection has no measured quote: its unchanged early
    # policy probes the larger balance. Keep that account once its quote fits.
    expected = larger if kind == 'image' and first_mode == 'browser' else fitting
    unused = fitting if expected is larger else larger
    assert result['execution_backend'] == other_mode and result['account_id'] == expected['id']
    assert result['attempted_account_ids'] == [first['id'], expected['id']]
    mixed.clients[unused['id']].prepare.assert_not_called()
    mixed.clients[unused['id']].submit.assert_not_called()
    mixed.browser[unused['id']].submit.assert_not_called()
    mixed.clients[lower['id']].submit.assert_not_called()
    mixed.browser[lower['id']].submit.assert_not_called()
    if other_mode == 'browser':
        assert set(result['upstream_context']) == {
            'preparation_admission_at', 'preparation_attempts', 'preparation_attempt_count'}
        assert result['upstream_context']['preparation_attempt_count'] == 1
        assert not result['normalized']['_cost_evidence'].get('canvas_quote')
    else:
        assert result['upstream_context']['account_id'] == expected['id']


@pytest.mark.parametrize('kind', ['video', 'image'])
@pytest.mark.parametrize('first_mode', ['canvas_cli', 'browser'])
def test_unfunded_quote_or_fresh_balance_can_choose_other_mode_without_paid_retry(mixed, kind, first_mode):
    other_mode = 'browser' if first_mode == 'canvas_cli' else 'canvas_cli'
    first = mixed.add('first', mode=first_mode, priority=9,
        balance=5 if first_mode == 'canvas_cli' else 100, fresh=0 if first_mode == 'browser' else None, kind=kind)
    fitting = mixed.add('fitting', mode=other_mode, balance=15, priority=4, kind=kind)
    identifier = mixed.create(kind)
    mixed.service._run(identifier)
    result = mixed.store.task(identifier)
    assert result['status'] == 'succeeded', result['error_message']
    assert result['execution_backend'] == other_mode and result['account_id'] == fitting['id']
    assert result['attempted_account_ids'] == [fitting['id']]
    mixed.clients[first['id']].submit.assert_not_called()
    mixed.browser[first['id']].submit.assert_not_called()


@pytest.mark.parametrize('kind', ['video', 'image'])
def test_prepared_funded_cli_does_not_switch_to_smaller_same_priority_browser(mixed, kind):
    first = mixed.add('large-cli', mode='canvas_cli', balance=100, kind=kind)
    fitting = mixed.add('small-browser', mode='browser', balance=15, kind=kind)
    identifier = mixed.create(kind)
    mixed.service._run(identifier)
    result = mixed.store.task(identifier)
    assert result['status'] == 'succeeded' and result['account_id'] == first['id']
    assert result['attempted_account_ids'] == [first['id']]
    mixed.clients[first['id']].prepare.assert_called_once()
    mixed.clients[first['id']].submit.assert_called_once()
    mixed.browser[fitting['id']].submit.assert_not_called()


@pytest.mark.parametrize('kind', ['video', 'image'])
def test_cross_mode_credit_rejections_share_two_attempt_limit_and_preserve_audit(mixed, kind):
    first = mixed.add('first-cli', mode='canvas_cli', priority=9, kind=kind)
    second = mixed.add('second-browser', mode='browser', priority=8, kind=kind)
    third = mixed.add('third-cli', mode='canvas_cli', priority=7, kind=kind)
    identifier = mixed.create(kind)
    audit = {'previous_submissions': [{'submit_id': 'old-unresolved-submit'}],
             'recovery_resolution': {'attempt_started_at': time.time()},
             'preflight_failures': [{'account_id': 99, 'task_deadline': time.time() + 20}]}
    mixed.store.update_task(identifier, {'upstream_context': deepcopy(audit)})
    mixed.clients[first['id']].submit.side_effect = CanvasCLIError('INSUFFICIENT_CREDITS',
        'not enough credits left on first account', outcome_unknown=False)
    mixed.browser[second['id']].submit.side_effect = BrowserRejected('Dreamina ret=1006: Not enough credits left')
    mixed.service._run(identifier)
    result = mixed.store.task(identifier)
    assert result['status'] == 'failed' and result['error_code'] == 'INSUFFICIENT_CREDITS'
    assert result['attempted_account_ids'] == [first['id'], second['id']]
    assert {key:result['upstream_context'][key] for key in audit} == audit
    assert set(result['upstream_context']) == set(audit) | {
        'preparation_admission_at', 'preparation_attempts', 'preparation_attempt_count'}
    assert result['upstream_context']['preparation_attempt_count'] == 1
    mixed.clients[third['id']].identity.assert_not_called()
    mixed.browser[third['id']].submit.assert_not_called()


@pytest.mark.parametrize('kind', ['video', 'image'])
def test_unknown_acceptance_never_falls_back_to_browser(mixed, kind):
    first = mixed.add('first-cli', mode='canvas_cli', priority=9, kind=kind)
    backup = mixed.add('browser-backup', mode='browser', priority=8, kind=kind)
    identifier = mixed.create(kind)
    mixed.clients[first['id']].submit.side_effect = CanvasCLIError('CLI_COMMAND_TIMEOUT',
        'lost submission receipt', outcome_unknown=True)
    with patch.object(mixed.service, '_poll_canvas') as poll:
        mixed.service._run(identifier)
    result = mixed.store.task(identifier)
    assert result['execution_backend'] == 'canvas_cli' and result['account_id'] == first['id']
    assert result['error_code'] == 'UPSTREAM_SUBMIT_OUTCOME_UNKNOWN'
    assert result['upstream_context']['submit_id']
    assert mixed.store.active_account_count(first['id']) == 1
    poll.assert_called_once()
    mixed.browser[backup['id']].submit.assert_not_called()


@pytest.mark.parametrize('binding', ['unknown', 'submission_unknown', 'accepted', 'output',
    'result_urls', 'upstream_result_urls', 'run_id', 'thread_id', '_execution', 'manual',
    'financial_outcome', 'reconciliation_paused', 'reconciliation_closed', 'automatic_resubmission_blocked'])
def test_rejection_handler_cannot_release_any_accepted_unknown_or_terminated_binding(mixed, binding):
    account = mixed.add('bound-cli', mode='canvas_cli')
    mixed.add('browser-backup', mode='browser')
    identifier = mixed.create('video')
    changes = {'status': 'preparing', 'account_id': account['id'], 'execution_backend': 'canvas_cli',
               'upstream_context': {'submit_id': 'original-submit', 'phase': 'quoted'}}
    if binding == 'unknown':
        changes.update(status='submitted', error_code='UPSTREAM_SUBMIT_OUTCOME_UNKNOWN')
    elif binding == 'submission_unknown':
        changes['status'] = 'submission_unknown'
    elif binding == 'accepted':
        changes['upstream_context'].update(phase='accepted', submission_phase='accepted')
    elif binding == 'output':
        changes['upstream_context']['resource_ids'] = ['accepted-output']
    elif binding in {'result_urls', 'upstream_result_urls'}:
        changes[binding] = ['https://synthetic.test/original.mp4']
    elif binding in {'run_id', 'thread_id'}:
        changes[binding] = 'accepted-original-id'
    elif binding == '_execution':
        with mixed.store._connect() as database:
            import json
            normalized = mixed.store.task(identifier)['normalized']
            normalized['_execution'] = {'submit_id': 'original-submit'}
            database.execute('UPDATE tasks SET normalized_json=? WHERE id=?', (json.dumps(normalized), identifier))
    elif binding in {'financial_outcome', 'reconciliation_paused', 'reconciliation_closed', 'automatic_resubmission_blocked'}:
        changes['upstream_context'][binding] = 'unconfirmed' if binding == 'financial_outcome' else True
    else:
        changes.update(status='failed', error_code='OPERATOR_CONTENT_RESTRICTED')
        changes['upstream_context']['manual_termination'] = {'reason': 'operator'}
    before = mixed.store.update_task(identifier, changes)
    assert not mixed.service._handle_credit_rejection(identifier, account['id'], 'not enough credits left')
    assert mixed.store.task(identifier) == before
    assert not mixed.service._requeue_unaccepted_task(identifier)
    assert mixed.store.task(identifier) == before


def virtual_clock(monkeypatch):
    started_mono, started_wall = time.monotonic(), time.time()
    clock = {'elapsed': 0.0}
    fake_time = SimpleNamespace(monotonic=lambda: started_mono + clock['elapsed'],
        time=lambda: started_wall + clock['elapsed'],
        sleep=lambda delay: clock.update(elapsed=clock['elapsed'] + delay))
    monkeypatch.setattr('app.service.time', fake_time)
    monkeypatch.setattr('app.canvas_service.time', fake_time)
    return clock, started_mono, started_wall


@pytest.mark.parametrize('kind', ['video', 'image'])
@pytest.mark.parametrize('upload_exhausts_budget', [False, True])
def test_identity_retry_into_browser_keeps_wall_deadline_and_checks_before_paid_submit(mixed, monkeypatch, kind, upload_exhausts_budget):
    first = mixed.add('first-cli', mode='canvas_cli', priority=9, kind=kind)
    backup = mixed.add('browser-backup', mode='browser', priority=8, kind=kind)
    identifier = mixed.create(kind)
    mixed.store.update_settings({'task_timeout_seconds': 5})
    clock, started_mono, started_wall = virtual_clock(monkeypatch)

    def identity_timeout(**kwargs):
        clock['elapsed'] += 4
        raise CanvasCLIError('CLI_COMMAND_TIMEOUT', 'identity network timeout')

    mixed.clients[first['id']].identity.side_effect = identity_timeout
    browser = mixed.browser[backup['id']]
    uploads = browser.upload_inputs.return_value
    browser.upload_inputs.side_effect = lambda *_: (clock.update(elapsed=6) or uploads) if upload_exhausts_budget else uploads
    mixed.service._run(identifier)
    result = mixed.store.task(identifier)
    assert result['upstream_context']['preflight_failures'][0]['task_deadline'] == pytest.approx(started_wall + 5)
    assert result['execution_backend'] == 'browser' and result['account_id'] == backup['id']
    mixed.clients[first['id']].prepare.assert_not_called()
    if upload_exhausts_budget:
        assert result['status'] == 'failed' and result['error_code'] == 'CLI_PREPARATION_TIMEOUT'
        browser.submit.assert_not_called()
    else:
        assert result['status'] == 'succeeded'
        browser.submit.assert_called_once()
        assert mixed.poll_deadlines == pytest.approx([started_mono + 5])


@pytest.mark.parametrize('kind', ['video', 'image'])
@pytest.mark.parametrize('during_slot_wait', [False, True])
def test_restarted_preflight_budget_expires_without_selecting_account_or_running_any_client(mixed, monkeypatch, kind, during_slot_wait):
    account = mixed.add('browser-ready', mode='browser', kind=kind)
    mixed.add('cli-ready', mode='canvas_cli', kind=kind)
    identifier = mixed.create(kind)
    clock, _, started_wall = virtual_clock(monkeypatch)
    audit = {'previous_submissions': [{'submit_id': 'older-submit'}],
             'preflight_failures': [{'account_id': 99, 'dispatch_started': False,
                                    'task_deadline': started_wall + (2 if during_slot_wait else -1)}]}
    mixed.store.update_task(identifier, {'upstream_context': deepcopy(audit), 'execution_backend': ''})
    if during_slot_wait:
        acquire = Mock(side_effect=lambda **_: clock.update(elapsed=3) or False)
        monkeypatch.setattr(mixed.service._worker_slots, 'acquire', acquire)
    mixed.service._run(identifier)
    result = mixed.store.task(identifier)
    assert result['status'] == 'failed' and result['error_code'] == 'CLI_PREPARATION_TIMEOUT'
    assert result['upstream_context'] == audit and result['execution_backend'] == ''
    assert not result['account_id'] and not result['attempted_account_ids']
    mixed.service.browser_runtime.acquire.assert_not_called()
    mixed.browser[account['id']].submit.assert_not_called()
    assert all(not client.identity.called for client in mixed.clients.values())
    if during_slot_wait:
        acquire.assert_called_once()


@pytest.mark.parametrize('with_preflight', [False, True])
def test_browser_credit_retry_only_renews_original_video_budget_without_preflight_history(mixed, monkeypatch, with_preflight):
    first = mixed.add('first-browser', mode='browser', priority=9)
    backup = mixed.add('cli-backup', mode='canvas_cli', priority=8)
    identifier = mixed.create('video')
    mixed.store.update_settings({'task_timeout_seconds': 5})
    clock, started_mono, started_wall = virtual_clock(monkeypatch)
    audit = {'previous_submissions': [{'submit_id': 'historical-submit'}]}
    if with_preflight:
        audit['preflight_failures'] = [{'account_id': 99, 'task_deadline': started_wall + 5}]
    mixed.store.update_task(identifier, {'upstream_context': deepcopy(audit)})

    def rejected(*args, **kwargs):
        clock['elapsed'] += 4
        raise BrowserRejected('Dreamina ret=1006: Not enough credits left')

    mixed.browser[first['id']].submit.side_effect = rejected
    second = mixed.clients[backup['id']]
    identity = second.identity.side_effect
    passed_deadlines = []

    def check_deadline(*, deadline):
        passed_deadlines.append(deadline)
        return identity()

    second.identity.side_effect = check_deadline
    mixed.service._run(identifier)
    result = mixed.store.task(identifier)
    assert result['status'] == 'succeeded', result['error_message']
    assert result['account_id'] == backup['id']
    assert result['upstream_context']['previous_submissions'] == audit['previous_submissions']
    assert bool(result['upstream_context'].get('preflight_failures')) == with_preflight
    expected = started_mono + (5 if with_preflight else 9)
    assert passed_deadlines and passed_deadlines[0] == pytest.approx(expected)
    assert result['upstream_context']['observation_deadline'] == pytest.approx(started_wall + (5 if with_preflight else 9))
