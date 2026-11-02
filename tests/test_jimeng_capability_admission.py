"""Pure Jimeng capability checks cannot block a compatible international route."""

from copy import deepcopy
from unittest.mock import Mock

import pytest

from app.jimeng_client import JimengProtocolUnavailable
from app.catalog import MODELS
from app.region_policy import DEFAULT_REGION_GENERATION_POLICY
from test_canvas_execution import context
from test_mixed_execution_admission import mixed


def allow_cn(store, model):
    policy = deepcopy(DEFAULT_REGION_GENERATION_POLICY)
    policy['cn']['models'][model] = {'min_duration': 4, 'max_duration': MODELS[model].max_duration}
    store.update_settings({'region_generation_policy': policy})


@pytest.mark.parametrize('international_mode', ['browser', 'canvas_cli'])
def test_high_priority_jimeng_unsupported_480p_preview_is_skipped_for_compatible_international_account(mixed, monkeypatch, international_mode):
    mainland = mixed.add('mainland-unsupported', mode='jimeng_web', priority=9)
    international = mixed.add('international-supported', mode=international_mode, priority=1)
    mixed.store.update_account(international['id'], {'region': 'us'})
    allow_cn(mixed.store, 'sd-2-5-480p-preview')
    construct = Mock(side_effect=AssertionError('unsupported Jimeng must not construct a client'))
    monkeypatch.setattr('app.service.JimengWebClient', construct)
    identifier = mixed.service.create({'model': 'sd-2-5-480p-preview', 'duration': 4,
        'prompt': 'Synthetic landscape'})['id']
    mixed.service._run(identifier)
    result = mixed.store.task(identifier)
    assert result['status'] == 'succeeded', result['error_message']
    assert result['account_id'] == international['id']
    assert result['attempted_account_ids'] == [international['id']]
    construct.assert_not_called()
    mixed.browser[mainland['id']].upload_inputs.assert_not_called()
    mixed.browser[mainland['id']].submit.assert_not_called()


def test_a_pool_with_only_unsupported_jimeng_accounts_rejects_before_task_or_client_creation(mixed, monkeypatch):
    mixed.add('mainland-only', mode='jimeng_web')
    allow_cn(mixed.store, 'sd-2-5-480p-preview')
    construct = Mock(side_effect=AssertionError('pure capability validation constructed a client'))
    monkeypatch.setattr('app.service.JimengWebClient', construct)
    with pytest.raises(ValueError, match='Jimeng'):
        mixed.service.create({'model': 'sd-2-5-480p-preview', 'duration': 4, 'prompt': 'Synthetic landscape'})
    assert mixed.store.tasks() == []
    construct.assert_not_called()
    mixed.service.browser_runtime.acquire.assert_not_called()


def test_queued_task_rechecks_pure_capability_before_account_startup(mixed, monkeypatch):
    mixed.add('mainland-mini', mode='jimeng_web')
    allow_cn(mixed.store, 'sd-2-0-mini')
    identifier = mixed.service.create({'model': 'sd-2-0-mini', 'duration': 4,
        'prompt': 'Synthetic landscape'})['id']
    monkeypatch.setattr('app.service.jimeng_preflight_model',
        Mock(side_effect=JimengProtocolUnavailable('Jimeng model capability is unavailable')))
    construct = Mock(side_effect=AssertionError('unavailable Jimeng cannot construct a client'))
    monkeypatch.setattr('app.service.JimengWebClient', construct)
    mixed.service._run(identifier)
    result = mixed.store.task(identifier)
    assert result['error_code'] == 'JIMENG_PROTOCOL_UNAVAILABLE'
    assert result['attempted_account_ids'] == [] and result['run_id'] == ''
    construct.assert_not_called()
    mixed.service.browser_runtime.acquire.assert_not_called()
