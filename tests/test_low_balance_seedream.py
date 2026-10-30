"""A low-balance exception admits only a priced Seedream Pro 5 image task."""
from copy import deepcopy
from unittest.mock import patch

import pytest

from app.account_policy import account_accepts_task, low_balance_image_probe
from app.catalog import normalize_request
from app.image_client import resolve_image_model
from app.image_request import normalize_image_request
from test_canvas_execution import context, fake_client, owner


def image_task(model='seedream-5.0-pro'):
    body = {'model': model, 'prompt': 'Synthetic landscape'}
    return {'normalized': normalize_image_request(body, resolve_image_model(model))}


def auto_low(store, *, name='low', balance=40, priority=0):
    account = owner(store, name, balance=balance, priority=priority)
    store.update_account(account['id'], {'enabled': False, 'status': 'disabled_low_balance'})
    return store.account(account['id'], private=True)


@pytest.mark.parametrize('model', ['seedream-5.0-pro', 'seedream-5.0-pro-1.5k',
                                  'seedream-5.0-pro-2k', 'seedream-5.0-pro-4k'])
@pytest.mark.parametrize('disabled', [False, True])
def test_low_active_and_automatic_disabled_accounts_accept_only_supported_pro5(context, model, disabled):
    store, service, _ = context
    store.update_settings({'low_balance_disable_threshold': 50})
    account = auto_low(store) if disabled else owner(store, balance=40)
    task = image_task(model)
    assert account_accepts_task(account, task, threshold=50)
    assert service._funded_accounts(0, set(), {}, task=task)[0]['id'] == account['id']
    video = {'normalized': normalize_request({'model': 'sd-2-0-mini', 'prompt': 'Synthetic', 'duration': 4})}
    assert not service._funded_accounts(0, set(), {}, task=video)
    assert not service._funded_accounts(0, set(), {}, task=image_task('gpt-image-2.5-flare'))
    invalid = deepcopy(task)
    invalid['normalized']['upstream_model'] = 'seedream_5.0'
    assert not service._funded_accounts(0, set(), {}, task=invalid)


@pytest.mark.parametrize('balance', [None, 0, -1])
@pytest.mark.parametrize('threshold', [0, 50])
def test_unknown_empty_or_negative_automatic_disabled_balance_is_not_a_free_task(context, balance, threshold):
    store, service, _ = context
    store.update_settings({'low_balance_disable_threshold': threshold})
    auto_low(store, balance=balance)
    assert not service._funded_accounts(0, set(), {}, task=image_task())


def test_threshold_zero_and_exact_boundary_preserve_existing_ordinary_active_policy(context):
    store, service, _ = context
    account = owner(store, balance=40)
    video = {'normalized': normalize_request({'model': 'sd-2-0-mini', 'prompt': 'Synthetic', 'duration': 4})}
    assert service._funded_accounts(10, set(), {}, task=video)[0]['id'] == account['id']
    store.update_settings({'low_balance_disable_threshold': 50})
    assert not service._funded_accounts(10, set(), {}, task=video)
    store.update_account(account['id'], {'balance': 50})
    assert service._funded_accounts(10, set(), {}, task=video)[0]['id'] == account['id']
    store.update_settings({'low_balance_disable_threshold': 60})
    assert not service._funded_accounts(10, set(), {}, task=video)


@pytest.mark.parametrize('threshold', [0, 50, 60])
@pytest.mark.parametrize('balance,eligible', [(49.99, True), (50, False), (50.01, False),
                                           (0, False), (None, False), (float('nan'), False)])
def test_auto_disabled_pro5_rescue_has_an_independent_literal_50_limit(threshold, balance, eligible):
    account = {'enabled': False, 'status': 'disabled_low_balance',
               'login_enable_after': None, 'balance': balance}
    task = image_task()
    assert account_accepts_task(account, task, threshold=threshold) is eligible
    assert low_balance_image_probe(account, task, threshold) is eligible


def test_disabled_low_pool_with_threshold_off_cannot_supply_other_models(context):
    store, service, _ = context
    low = auto_low(store)
    assert service.settings()['low_balance_disable_threshold'] == 0
    assert service._funded_accounts(0, set(), {}, task=image_task())[0]['id'] == low['id']
    video = {'normalized': normalize_request({'model': 'sd-2-0-mini', 'prompt': 'Synthetic', 'duration': 4})}
    assert not service._funded_accounts(0, set(), {}, task=video)
    assert not service._funded_accounts(0, set(), {}, task=image_task('gpt-image-2.5-flare'))


@pytest.mark.parametrize('method', ['patch_intent', 'store_toggle', 'post_upsert'])
@pytest.mark.parametrize('threshold', [0, 50])
def test_manual_disable_of_an_auto_low_account_cannot_be_bypassed(context, method, threshold):
    store, service, _ = context
    store.update_settings({'low_balance_disable_threshold': threshold})
    account = auto_low(store)
    if method == 'post_upsert':
        store.upsert_account({'name': account['name'], 'enabled': False})
    elif method == 'patch_intent':
        store.update_account(account['id'], {'enabled': False, 'login_enable_after': False})
    else:
        store.update_account(account['id'], {'enabled': False})
    assert store.account(account['id'], private=True)['login_enable_after'] == 0
    assert not service._funded_accounts(0, set(), {}, task=image_task())


def test_internal_authorization_updates_preserve_existing_enable_after_intent(context):
    store, _, _ = context
    account = owner(store)
    store.update_account(account['id'], {'enabled': False, 'status': 'cli_auth_required', 'login_enable_after': True})
    store.update_account(account['id'], {'enabled': True, 'status': 'active'})
    assert store.account(account['id'], private=True)['login_enable_after'] == 1
    store.update_account(account['id'], {'enabled': False, 'status': 'disabled_low_balance', 'balance': 40})
    assert store.account(account['id'], private=True)['login_enable_after'] == 1


@pytest.mark.parametrize('changes', [
    {'proxy_url': ''}, {'cli_auth_status': 'pending'}, {'agreement_ready': False},
    {'watermark_ready': False}, {'status': 'login_required'},
])
def test_low_balance_exception_does_not_bypass_identity_proxy_or_setup(context, changes):
    store, service, _ = context
    store.update_settings({'low_balance_disable_threshold': 50})
    account = auto_low(store)
    store.update_account(account['id'], {'login_email': 'synthetic@example.test'})
    store.update_account(account['id'], {'enabled': False, 'status': 'disabled_low_balance',
        'watermark_ready': True, 'agreement_ready': True, 'login_user_id': account['login_user_id'],
        'cli_user_id': account['cli_user_id'], 'cli_auth_status': 'ready'})
    assert service._funded_accounts(0, set(), {}, task=image_task())
    store.update_account(account['id'], changes)
    assert not service._funded_accounts(0, set(), {}, task=image_task())


def test_low_account_unknown_hold_blocks_even_a_read_only_quote_probe(context):
    store, service, _ = context
    store.update_settings({'low_balance_disable_threshold': 50})
    account = auto_low(store)
    assert not service._funded_accounts(0, set(), {'unknown': (account['id'], 0)}, task=image_task())
    assert not service._funded_accounts(0, set(), {'reserved': (account['id'], 40)}, task=image_task())


@pytest.mark.parametrize('threshold', [0, 50])
def test_previous_accounts_estimate_does_not_prevent_an_account_specific_quote(context, threshold):
    store, service, _ = context
    store.update_settings({'low_balance_disable_threshold': threshold})
    account = auto_low(store)
    task = image_task()
    store.create_task('new-image', {'model': 'seedream-5.0-pro'}, task['normalized'])
    selected = service._reserve_account(90, task_id='new-image')
    assert selected['id'] == account['id']
    assert service._credit_reservations['new-image'] == (account['id'], 40)
    assert service._reserve_account(0, task_id='parallel') is None
    assert not service._submission_funded(selected, 'new-image', 41, 40)
    assert service._submission_funded(selected, 'new-image', 40, 40)


@pytest.mark.parametrize('inherited_estimate', [2, 90])
@pytest.mark.parametrize('threshold', [0, 50])
def test_inherited_estimate_cannot_admit_parallel_preparations(context, inherited_estimate, threshold):
    store, service, _ = context
    store.update_settings({'low_balance_disable_threshold': threshold})
    account = auto_low(store)
    normalized = image_task()['normalized']
    for identifier in ('first-probe', 'second-probe'):
        store.create_task(identifier, {'model': 'seedream-5.0-pro'}, normalized)
        store.update_task(identifier, {'estimated_cost': inherited_estimate})
    selected = service._reserve_account(inherited_estimate, task_id='first-probe')
    store.update_task('first-probe', {'account_id': account['id'], 'status': 'preparing'})
    assert service._credit_reservations['first-probe'] == (account['id'], 40)
    assert service._reserve_account(inherited_estimate, task_id='second-probe') is None
    assert store.task('first-probe')['estimated_cost'] == inherited_estimate
    assert store.task('first-probe')['actual_cost'] is None
    assert service._submission_funded(selected, 'first-probe', 40, 40)


def test_low_balance_quote_must_fit_credits_left_after_other_known_holds(context):
    store, service, _ = context
    store.update_settings({'low_balance_disable_threshold': 50})
    account = auto_low(store)
    store.create_task('new-image', {'model': 'seedream-5.0-pro'}, image_task()['normalized'])
    service._credit_reservations['existing-paid-task'] = (account['id'], 25)
    selected = service._reserve_account(90, task_id='new-image')
    assert selected['id'] == account['id']
    assert service._credit_reservations['new-image'] == (account['id'], 15)
    assert not service._submission_funded(selected, 'new-image', 16, 40)
    assert service._submission_funded(selected, 'new-image', 15, 40)


@pytest.mark.parametrize('mixed_unready_pool', [False, True])
@pytest.mark.parametrize('threshold', [0, 50])
def test_only_auto_low_ready_pool_runs_pro5_once_without_enabling_account(context, mixed_unready_pool, threshold):
    store, service, _ = context
    store.update_settings({'low_balance_disable_threshold': threshold})
    low = auto_low(store)
    if mixed_unready_pool:
        owner(store, 'unready', balance=100, authorized=False)
        assert service._cli_authorization_missing()
    response = service.create_image({'model': 'seedream-5.0-pro', 'prompt': 'Synthetic landscape'})
    client = fake_client(store, service, low, quote=32, kind='image')
    with patch.object(service, 'canvas_client', return_value=client):
        service._run_image(response['id'])
    result = store.task(response['id'])
    assert result['status'] == 'succeeded', result['error_message']
    client.submit.assert_called_once()
    stored = store.account(low['id'], private=True)
    assert not stored['enabled'] and stored['status'] == 'disabled_low_balance'
    assert not service._credit_reservations


@pytest.mark.parametrize('manual_disable', [False, True])
@pytest.mark.parametrize('threshold', [0, 50])
def test_actual_quote_or_new_manual_disable_prevents_paid_dispatch(context, manual_disable, threshold):
    store, service, _ = context
    store.update_settings({'low_balance_disable_threshold': threshold})
    low = auto_low(store)
    response = service.create_image({'model': 'seedream-5.0-pro', 'prompt': 'Synthetic landscape'})
    client = fake_client(store, service, low, quote=32 if manual_disable else 41, kind='image')
    prepare = client.prepare.side_effect

    def prepared(*args, **kwargs):
        value = prepare(*args, **kwargs)
        if manual_disable:
            store.update_account(low['id'], {'enabled': False})
        return value

    client.prepare.side_effect = prepared
    with patch.object(service, 'canvas_client', return_value=client):
        service._run_image(response['id'])
    row = store.task(response['id'])
    assert row['status'] == 'failed'
    assert row['actual_cost'] is None and not row['run_id']
    client.submit.assert_not_called()
    client.poll.assert_not_called()
