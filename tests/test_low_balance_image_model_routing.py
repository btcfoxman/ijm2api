"""Configured substitution must change the real Canvas plan and its own quote."""
from copy import deepcopy
import time
from unittest.mock import Mock, patch

import pytest

from app.account_policy import account_accepts_task
from app.canvas_cli import CanvasCLIError
from app.canvas_client import CanvasClient
from app.image_client import resolve_image_model
from app.image_model_routing import plan_image_execution
from app.image_request import normalize_image_request
from test_canvas_execution import context, fake_client, owner
from test_canvas_transport import FakeRunner


def normalized(**extra):
    body = {'model': 'seedream-5.0-pro', 'prompt': 'Synthetic landscape', **extra}
    return normalize_image_request(body, resolve_image_model(body['model']))


def setup(store, service, *, balance=40, auto=False, target='seedream-5.0-lite'):
    service.update_settings({'low_balance_image_rescue_threshold': 50,
                             'low_balance_seedream_target_model': target})
    account = owner(store, balance=balance)
    if auto:
        store.update_account(account['id'], {'enabled': False, 'status': 'disabled_low_balance'})
        account = store.account(account['id'], private=True)
    return account


def track_client(store, service, account, expected, *, quote=7):
    client = fake_client(store, service, account, quote=quote, actual=quote, kind='image')
    original = client.prepare.side_effect

    def prepare(value, *args, **kwargs):
        assert value['model'] == expected
        assert value['upstream_model'] == resolve_image_model(expected)
        result = original(value, *args, **kwargs)
        result['model'] = value['upstream_model']
        kwargs['persist'](deepcopy(result))
        return result

    client.prepare.side_effect = prepare
    client.preflight_model.side_effect = lambda value, **kwargs: (
        {'model': value['upstream_model']} if value['model'] == expected else
        (_ for _ in ()).throw(AssertionError('Wrong actual preflight model')))
    return client


@pytest.mark.parametrize('target', ['seedream-5.0-lite', 'gpt-image-2.5-flare'])
@pytest.mark.parametrize('alias', ['seedream-5.0-pro', 'seedream-5.0-pro-4k'])
def test_actual_preflight_draft_quote_submit_and_record_use_target(context, target, alias):
    store, service, _ = context
    account = setup(store, service, auto=True, target=target)
    body = {'model': alias, 'prompt': 'Synthetic landscape'}
    response = service.create_image(body)
    client = track_client(store, service, account, target)
    with patch.object(service, 'canvas_client', return_value=client):
        service._run_image(response['id'])
    row = store.task(response['id'])
    assert row['status'] == 'succeeded', row['error_message']
    assert row['request'] == body and row['model'] == alias
    assert row['normalized']['model'] == alias
    assert row['normalized']['upstream_model'] == 'seedream_5.0_pro'
    assert row['upstream_request']['model'] == resolve_image_model(target)
    assert row['image_model_route']['effective_model'] == target
    assert row['upstream_context']['model'] == resolve_image_model(target)
    assert row['estimated_cost'] == 7 and row['actual_cost'] == 7
    assert row['upstream_context']['quoted_credits'] == 7
    assert client.submit.call_args.args[0]['model'] == resolve_image_model(target)
    client.submit.assert_called_once()
    assert not store.account(account['id'])['enabled']


def test_ordinary_account_and_default_identity_never_silently_change_models(context):
    store, service, _ = context
    account = setup(store, service, balance=100)
    task = {'normalized': normalized()}
    effective, audit = plan_image_execution(task, account, service.settings())
    assert effective['upstream_model'] == 'seedream_5.0_pro' and not audit['mapped']
    service.update_settings({'low_balance_seedream_target_model': 'seedream-5.0-pro'})
    effective, audit = plan_image_execution(task, {**account, 'balance': 5}, service.settings())
    assert effective['upstream_model'] == 'seedream_5.0_pro' and not audit['mapped']


def test_fresh_identity_balance_controls_mapping_not_old_cached_balance(context):
    store, service, _ = context
    account = setup(store, service)
    response = service.create_image({'model': 'seedream-5.0-pro', 'prompt': 'Synthetic'})
    client = track_client(store, service, account, 'seedream-5.0-pro')
    client.identity.side_effect = lambda **kwargs: {'user_id': account['login_user_id'],
        'available_balance': 80 if not client.submit.called else 73}
    with patch.object(service, 'canvas_client', return_value=client):
        service._run_image(response['id'])
    row = store.task(response['id'])
    assert row['status'] == 'succeeded'
    assert row['image_model_route']['source'] == 'requested_model'


def test_unfunded_low_quote_reselects_normal_account_and_restores_pro(context):
    store, service, _ = context
    low = setup(store, service, auto=True)
    store.update_account(low['id'], {'priority': 9})
    high = owner(store, 'normal', balance=100, priority=0)
    response = service.create_image({'model': 'seedream-5.0-pro', 'prompt': 'Synthetic'})
    clients = {low['id']: track_client(store, service, low, 'seedream-5.0-lite', quote=41),
               high['id']: track_client(store, service, high, 'seedream-5.0-pro', quote=19)}
    with patch.object(service, 'canvas_client', side_effect=lambda account: clients[account['id']]):
        service._run_image(response['id'])
    row = store.task(response['id'])
    assert row['status'] == 'succeeded' and row['account_id'] == high['id']
    assert row['image_model_route']['effective_model'] == 'seedream-5.0-pro'
    assert row['upstream_context']['model'] == 'seedream_5.0_pro'
    assert row['estimated_cost'] == 19 and row['actual_cost'] == 19
    clients[low['id']].submit.assert_not_called()
    clients[high['id']].submit.assert_called_once()


def test_missing_actual_model_skips_only_the_unaccepted_account_and_restores_pro(context):
    store, service, _ = context
    low = setup(store, service, auto=True)
    store.update_account(low['id'], {'priority': 9})
    high = owner(store, 'normal', balance=100, priority=0)
    response = service.create_image({'model': 'seedream-5.0-pro', 'prompt': 'Synthetic'})
    clients = {low['id']: track_client(store, service, low, 'seedream-5.0-lite'),
               high['id']: track_client(store, service, high, 'seedream-5.0-pro', quote=19)}
    clients[low['id']].preflight_model.side_effect = CanvasCLIError('CLI_CAPABILITY_UNAVAILABLE',
        'Actual model is missing from this account catalog', details={'error': {
            'scope': 'account', 'reason': 'model_not_in_catalog', 'kind': 'image',
            'requested_model': 'seedream-5.0-lite'}})
    with patch.object(service, 'canvas_client', side_effect=lambda account: clients[account['id']]):
        service._run_image(response['id'])
    row = store.task(response['id'])
    assert row['status'] == 'succeeded' and row['account_id'] == high['id']
    assert row['image_model_route']['effective_model'] == 'seedream-5.0-pro'
    assert row['attempted_account_ids'] == [high['id']]
    assert row['upstream_context']['model_eligibility_exclusions'][0]['account_id'] == low['id']
    clients[low['id']].prepare.assert_not_called()
    clients[low['id']].submit.assert_not_called()


@pytest.mark.parametrize('unknown', [False, True])
def test_accepted_or_unknown_operation_keeps_durable_target_after_settings_change(context, unknown):
    store, service, _ = context
    account = setup(store, service, auto=True)
    response = service.create_image({'model': 'seedream-5.0-pro', 'prompt': 'Synthetic'})
    client = track_client(store, service, account, 'seedream-5.0-lite')
    with patch.object(service, 'canvas_client', return_value=client), patch.object(service, '_poll_canvas'):
        service._run_image(response['id'])
    before = store.task(response['id'])
    assert before['status'] == 'submitted'
    if unknown:
        store.update_task(response['id'], {'error_code': 'UPSTREAM_SUBMIT_OUTCOME_UNKNOWN',
            'upstream_context': {**before['upstream_context'], 'phase': 'unknown',
                                 'submit_error': {'outcome_unknown': True}}})
    service.update_settings({'low_balance_image_rescue_threshold': 0,
                             'low_balance_seedream_target_model': 'gpt-image-2.5-flare'})
    with patch('app.canvas_service.plan_image_execution', side_effect=AssertionError('Re-planned accepted work')):
        service._poll_canvas(response['id'], client, time.monotonic() + 3)
    after = store.task(response['id'])
    assert after['status'] == 'succeeded', after['error_message']
    assert after['upstream_context']['submit_id'] == before['upstream_context']['submit_id']
    assert after['image_model_route'] == before['image_model_route']
    assert after['upstream_request']['model'] == 'seedream_5.0_lite'
    client.submit.assert_called_once()


def catalog(native, *, limit, resolutions, count):
    return {'model': native, 'modes': [{'name': mode, 'flags': [
        {'flag': '--resolution', 'values': resolutions}, {'flag': '--ratio', 'values': ['1:1']},
        {'flag': '--count', 'min': 1, 'max': count}],
        'references': {'min': 0, 'max': limit, 'types': [{'mediaType': 'image', 'min': 0, 'max': limit}]}}
        for mode in ('t2i', 'i2i')]}


@pytest.mark.parametrize('target,extra', [
    ('seedream-5.0-lite', {'resolution': '1.5k'}),
    ('seedream-5.0-lite', {'image_urls': ['https://source.test/image.png'] * 7}),
    ('gpt-image-2.5-flare', {'n': 2}),
])
def test_incompatible_target_is_rejected_before_upload(context, tmp_path, target, extra):
    store, service, _ = context
    account = setup(store, service, target=target)
    if 'image_urls' in extra:
        extra = {'image_urls': [f'https://source.test/{i}.png' for i in range(7)]}
    body = {'model': 'seedream-5.0-pro', 'prompt': 'Synthetic', **extra}
    runner = FakeRunner(tmp_path)
    client = CanvasClient(runner, account)
    native = resolve_image_model(target)
    client._capabilities['image'] = [catalog(native, limit=6 if target.endswith('lite') else 16,
        resolutions=['2K', '4K'] if target.endswith('lite') else ['1K', '2K', '4K'],
        count=8 if target.endswith('lite') else 1)]
    client.identity = Mock(return_value={'user_id': account['login_user_id'], 'available_balance': 40})
    response = service.create_image(body)
    with patch.object(service, 'canvas_client', return_value=client), \
            patch.object(service, '_canvas_local_assets') as assets:
        service._run_image(response['id'])
    row = store.task(response['id'])
    assert row['status'] == 'failed' and row['error_code'] == 'CLI_CAPABILITY_UNAVAILABLE'
    assets.assert_not_called()
    assert runner.calls == []
    assert row['actual_cost'] is None and not row['run_id']


def test_settings_metadata_and_validation_do_not_accept_unverified_flash(context):
    store, service, _ = context
    values = service.public_settings()
    assert values['low_balance_image_rescue_threshold'] == 50
    assert values['low_balance_seedream_target_model'] == 'seedream-5.0-pro'
    assert 'seedream-5.0-lite' in values['low_balance_seedream_target_options']
    service.update_settings({'low_balance_image_rescue_threshold': 0,
                             'low_balance_seedream_target_options': ['untrusted']})
    assert service.settings()['low_balance_image_rescue_threshold'] == 0
    assert 'low_balance_seedream_target_options' not in store.get_settings()
    for invalid in [True, -1, float('nan'), float('inf'), None]:
        with pytest.raises(ValueError):
            service.update_settings({'low_balance_image_rescue_threshold': invalid})
    for invalid in ['seedream-5.0-flash', 'dola-seedream-5-0-flash-260915', 'seedream-5.0-lite-4k']:
        with pytest.raises(ValueError):
            service.update_settings({'low_balance_seedream_target_model': invalid})


def test_rescue_zero_closes_active_global_low_exception_but_global_zero_is_normal():
    account = {'id': 1, 'enabled': True, 'status': 'active', 'balance': 20}
    task = {'normalized': normalized()}
    assert not account_accepts_task(account, task, threshold=50, rescue_threshold=0)
    assert account_accepts_task(account, task, threshold=0, rescue_threshold=0)
    assert not account_accepts_task({**account, 'enabled': False, 'status': 'disabled_low_balance'},
                                    task, threshold=0, rescue_threshold=0)
    effective, audit = plan_image_execution(task, account, {
        'low_balance_image_rescue_threshold': 0,
        'low_balance_seedream_target_model': 'seedream-5.0-lite'})
    assert effective['upstream_model'] == 'seedream_5.0_pro'
    assert audit['source'] == 'requested_model' and not audit['mapped']
