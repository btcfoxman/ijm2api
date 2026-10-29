"""A positive quote waits for another admitted task's uncertain credit hold."""
from copy import deepcopy
import threading
from unittest.mock import patch

import pytest

from test_canvas_execution import context, task
from test_mixed_execution_admission import mixed, virtual_clock


def quoted_with_concurrent_unknown_hold(mixed, kind, *, quote=40, balance=46897):
    account = mixed.add('quoted-cli', mode='canvas_cli', balance=balance, priority=9, kind=kind, quote=quote)
    identifier = mixed.create(kind)
    client = mixed.clients[account['id']]
    prepared = client.prepare.side_effect
    original = []

    def prepare_then_admit_other(*args, **kwargs):
        value = prepared(*args, **kwargs)
        original.append(deepcopy(value))
        task(mixed.store, 'other-admitted')
        mixed.store.update_task('other-admitted', {'account_id': account['id'], 'status': 'preparing',
                                                  'estimated_cost': 0})
        return value

    client.prepare.side_effect = prepare_then_admit_other
    return account, identifier, client, original


@pytest.mark.parametrize('kind', ['video', 'image'])
def test_quoted_task_waits_without_holding_global_lock_and_runs_once_when_other_quote_is_known(mixed, kind):
    account, identifier, client, original = quoted_with_concurrent_unknown_hold(mixed, kind)
    progressed, errors = threading.Event(), []

    def update_other_from_another_worker():
        try:
            with mixed.service._lock:
                assert mixed.service._credit_reservations[identifier] == (account['id'], 40)
                row = mixed.store.task(identifier)
                assert row['estimated_cost'] == 40 and row['upstream_context']['phase'] == 'quoted'
                assert not row['attempted_account_ids'] and not client.submit.called
                mixed.store.update_task('other-admitted', {'estimated_cost': 25})
                progressed.set()
        except Exception as exc:
            errors.append(exc)

    workers = []

    def wait_for_other(delay):
        worker = threading.Thread(target=update_other_from_another_worker)
        workers.append(worker)
        worker.start()
        assert progressed.wait(2), 'a quoted task held the service lock while waiting for another quote'
        worker.join(2)
        return False

    mixed.service._stop.wait.side_effect = wait_for_other
    with patch.object(mixed.service, '_canvas_local_assets', return_value={'images': [], 'videos': [], 'audios': []}) as download:
        mixed.service._run(identifier)
    for worker in workers:
        worker.join(2)
    assert not errors
    result = mixed.store.task(identifier)
    assert result['status'] == 'succeeded', result['error_message']
    assert progressed.is_set() and mixed.service._stop.wait.call_count == 1
    assert result['upstream_context']['submit_id'] == original[0]['submit_id']
    assert result['upstream_context']['node_id'] == original[0]['node_id']
    assert result['account_id'] == account['id'] and result['attempted_account_ids'] == [account['id']]
    client.prepare.assert_called_once()
    client.submit.assert_called_once()
    download.assert_called_once()


@pytest.mark.parametrize('kind', ['video', 'image'])
def test_uncertain_hold_wait_ends_at_original_deadline_without_dispatch_or_repreparation(mixed, monkeypatch, kind):
    account, identifier, client, original = quoted_with_concurrent_unknown_hold(mixed, kind)
    mixed.store.update_settings({'task_timeout_seconds': 5})
    clock, started_mono, _ = virtual_clock(monkeypatch)
    delays = []

    def wait(delay):
        delays.append(delay)
        clock['elapsed'] += delay
        return False

    mixed.service._stop.wait.side_effect = wait
    mixed.service._run(identifier)
    result = mixed.store.task(identifier)
    assert result['status'] == 'failed' and result['error_code'] == 'CLI_PREPARATION_TIMEOUT'
    assert clock['elapsed'] == pytest.approx(5) and sum(delays) == pytest.approx(5)
    assert result['account_id'] == account['id'] and not result['attempted_account_ids']
    assert result['upstream_context']['submit_id'] == original[0]['submit_id']
    assert result['estimated_cost'] == 40 and result['actual_cost'] is None
    assert client.prepare.call_args.kwargs['deadline'] == pytest.approx(started_mono + 5)
    client.prepare.assert_called_once()
    client.submit.assert_not_called()


@pytest.mark.parametrize('kind', ['video', 'image'])
def test_operator_stop_while_waiting_preserves_terminal_context_and_never_dispatches(mixed, kind):
    _, identifier, client, _ = quoted_with_concurrent_unknown_hold(mixed, kind)
    stopped = []

    def stop_task(delay):
        old = mixed.store.task(identifier)
        stopped.append(mixed.store.update_task(identifier, {'status': 'failed',
            'error_code': 'OPERATOR_CONTENT_RESTRICTED', 'error_message': 'operator stopped',
            'upstream_context': {**old['upstream_context'], 'manual_termination': {'reason': 'operator'}}}))
        return False

    mixed.service._stop.wait.side_effect = stop_task
    mixed.service._run(identifier)
    assert stopped and mixed.store.task(identifier) == stopped[0]
    client.prepare.assert_called_once()
    client.submit.assert_not_called()


@pytest.mark.parametrize('kind', ['video', 'image'])
def test_resolved_hold_and_true_insufficient_balance_still_switch_to_configured_browser(mixed, kind):
    account, identifier, client, _ = quoted_with_concurrent_unknown_hold(mixed, kind, balance=100)
    backup = mixed.add('browser-backup', mode='browser', balance=100, priority=8, kind=kind)

    def resolve_hold_with_low_balance(delay):
        mixed.store.update_task('other-admitted', {'status': 'succeeded'})
        mixed.store.update_account(account['id'], {'balance': 5})
        return False

    mixed.service._stop.wait.side_effect = resolve_hold_with_low_balance
    mixed.service._run(identifier)
    result = mixed.store.task(identifier)
    assert result['status'] == 'succeeded' and result['account_id'] == backup['id']
    assert result['execution_backend'] == 'browser' and result['attempted_account_ids'] == [backup['id']]
    client.prepare.assert_called_once()
    client.submit.assert_not_called()
    mixed.browser[backup['id']].submit.assert_called_once()


def test_zero_quote_does_not_wait_on_another_zero_hold(mixed):
    _, identifier, client, _ = quoted_with_concurrent_unknown_hold(mixed, 'video', quote=0)
    mixed.service._run(identifier)
    mixed.service._stop.wait.assert_not_called()
    client.prepare.assert_called_once()
    client.submit.assert_not_called()


@pytest.mark.parametrize('kind', ['video', 'image'])
@pytest.mark.parametrize('cached_balance_drops', [False, True])
def test_raw_insufficient_balance_does_not_wait_for_hold_that_cannot_fund_quote(mixed, kind, cached_balance_drops):
    account, identifier, client, _ = quoted_with_concurrent_unknown_hold(mixed, kind,
        balance=100 if cached_balance_drops else 30, quote=40)
    backup = mixed.add('browser-backup', mode='browser', balance=100, priority=8, kind=kind)
    prepare = client.prepare.side_effect

    def prepare_with_known_hold(*args, **kwargs):
        prepared = prepare(*args, **kwargs)
        mixed.store.update_task('other-admitted', {'estimated_cost': 5})
        if cached_balance_drops:
            mixed.store.update_account(account['id'], {'balance': 30})
        return prepared

    client.prepare.side_effect = prepare_with_known_hold
    mixed.service._run(identifier)
    result = mixed.store.task(identifier)
    assert result['status'] == 'succeeded' and result['account_id'] == backup['id']
    assert result['execution_backend'] == 'browser' and result['attempted_account_ids'] == [backup['id']]
    assert mixed.store.task('other-admitted')['estimated_cost'] == 5
    assert mixed.store.account(account['id'])['balance'] == 30
    mixed.service._stop.wait.assert_not_called()
    client.prepare.assert_called_once()
    client.submit.assert_not_called()
    mixed.browser[backup['id']].submit.assert_called_once()
