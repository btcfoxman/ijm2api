"""Known insufficient Canvas budgets stop before another reference upload."""
from copy import deepcopy
from unittest.mock import Mock, patch

import pytest

from test_canvas_execution import context, owner, task, fake_client
from test_canvas_quote_hints import epoch, new_hint, quoted


def work(store, identifier='credit-request', duration=17):
    return task(store, identifier, model='sd-2-5-480p-preview', duration=duration,
        image_urls=['https://fixture.example/a.png', 'https://fixture.example/b.png'],
        audio_urls=['https://fixture.example/a.mp3'])


@pytest.mark.parametrize('required,duration', [(323, 17), (551, 29)])
def test_task_with_known_required_quote_never_assigns_any_226_credit_account(context, required, duration):
    store, service, _ = context
    accounts = [owner(store, 'first', balance=226), owner(store, 'second', balance=226)]
    row = work(store, duration=duration)
    request = deepcopy(row['request'])
    store.update_task(row['id'], {'estimated_cost': required, 'execution_backend': 'canvas_cli'})
    service.canvas_client = Mock(side_effect=AssertionError('Unfunded pool cannot run CLI'))
    service._canvas_local_assets = Mock(side_effect=AssertionError('Unfunded pool cannot download references'))
    service._run(row['id'])
    saved = store.task(row['id'])
    assert saved['status'] == 'failed' and saved['error_code'] == 'INSUFFICIENT_CREDITS'
    assert saved['account_id'] is None and not saved['attempted_account_ids']
    assert saved['request'] == request and saved['actual_cost'] is None
    service.canvas_client.assert_not_called()
    service._canvas_local_assets.assert_not_called()
    assert not service._credit_reservations and not any(service._active.values())


def test_first_complete_quote_stops_all_underfunded_reselection_instead_of_reupload(context):
    store, service, _ = context
    first = owner(store, 'first', balance=226, priority=2)
    second = owner(store, 'second', balance=226, priority=1)
    row = work(store)
    service._canvas_local_assets = Mock(return_value={'images': [], 'videos': [], 'audios': []})
    clients = {a['id']: fake_client(store, service, a, quote=323) for a in (first, second)}
    with patch.object(service, 'canvas_client', side_effect=lambda a: clients[a['id']]):
        service._run(row['id'])
    saved = store.task(row['id'])
    assert saved['status'] == 'failed' and saved['error_code'] == 'INSUFFICIENT_CREDITS'
    assert saved['estimated_cost'] == 323 and saved['actual_cost'] is None
    clients[first['id']].prepare.assert_called_once()
    clients[second['id']].identity.assert_not_called()
    clients[second['id']].prepare.assert_not_called()
    assert all(not client.submit.called for client in clients.values())
    assert not service._credit_reservations and not any(service._active.values())


def test_fresh_lower_balance_is_rejected_before_materials_and_next_funded_owner_can_run(context):
    store, service, _ = context
    first = owner(store, 'stale-balance', balance=400, priority=2)
    second = owner(store, 'funded', balance=500, priority=1)
    row = work(store)
    service._canvas_local_assets = Mock(return_value={'images': [], 'videos': [], 'audios': []})
    store.update_task(row['id'], {'estimated_cost': 323, 'execution_backend': 'canvas_cli'})
    clients = {a['id']: fake_client(store, service, a, quote=323) for a in (first, second)}
    clients[first['id']].identity.side_effect = lambda **kwargs: {
        'user_id': first['login_user_id'], 'available_balance': 226}
    with patch.object(service, 'canvas_client', side_effect=lambda a: clients[a['id']]):
        service._run(row['id'])
    saved = store.task(row['id'])
    assert saved['status'] == 'succeeded' and saved['account_id'] == second['id']
    clients[first['id']].preflight_model.assert_not_called()
    clients[first['id']].prepare.assert_not_called()
    clients[first['id']].submit.assert_not_called()
    clients[second['id']].submit.assert_called_once()


def test_exact_account_quote_can_be_lower_than_another_owners_task_estimate(context):
    store, service, _ = context
    account = owner(store, 'discounted', balance=226)
    row = work(store)
    epoch(service, account)
    context_quote = quoted(account, 200, model='seedance_2.5_draft')
    hint = new_hint(account, service.canvas_cli.data_dir, row['normalized'], context_quote, account['login_user_id'])
    assert hint is not None
    store.save_canvas_quote_hint(hint)
    selected = service._reserve_account(323, task_id=row['id'])
    assert selected['id'] == account['id'] and selected['_scheduling_quote_amount'] == 200
    service._release_account(account['id'], row['id'])
