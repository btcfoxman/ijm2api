"""A valid account catalog can disqualify an account, never a submitted task."""
from copy import deepcopy
import time
from unittest.mock import Mock, patch

import pytest

from app.browser import BrowserRejected
from app.canvas_cli import CanvasCLIError
from app.service import Service
from app.store import Store
from test_mixed_execution_admission import mixed, virtual_clock
from test_canvas_execution import context


def missing_model(model, kind, **overrides):
    marker = {'scope': 'account', 'reason': 'model_not_in_catalog', 'requested_model': model, 'kind': kind}
    marker.update(overrides)
    return CanvasCLIError('CLI_CAPABILITY_UNAVAILABLE', f'Canvas does not expose requested model {model}',
                          details={'error': marker})


@pytest.mark.parametrize('kind', ['video', 'image'])
def test_multiple_ineligible_accounts_are_skipped_before_download_without_consuming_credit_attempts(mixed, kind):
    skipped = [mixed.add(f'missing-{i}', mode='canvas_cli', priority=9-i, kind=kind) for i in range(3)]
    supported = mixed.add('supported', mode='canvas_cli', priority=1, kind=kind)
    identifier = mixed.create(kind)
    model = mixed.store.task(identifier)['model']
    for account in skipped:
        mixed.clients[account['id']].preflight_model.side_effect = missing_model(model, kind)
    with patch.object(mixed.service, '_canvas_local_assets', return_value={'images': [], 'videos': [], 'audios': []}) as download:
        mixed.service._run(identifier)
    result = mixed.store.task(identifier)
    assert result['status'] == 'succeeded', result['error_message']
    assert result['account_id'] == supported['id'] and result['attempted_account_ids'] == [supported['id']]
    exclusions = result['upstream_context']['model_eligibility_exclusions']
    assert [entry['account_id'] for entry in exclusions] == [account['id'] for account in skipped]
    assert all(entry['requested_model'] == model and entry['kind'] == kind
               and entry['phase'] == 'model_selection' and entry['dispatch_started'] is False for entry in exclusions)
    assert all(entry['task_deadline'] == pytest.approx(exclusions[0]['task_deadline'], abs=0.05)
               for entry in exclusions)
    download.assert_called_once()
    assert download.call_args.args[1]['id'] == supported['id']
    for account in skipped:
        mixed.clients[account['id']].prepare.assert_not_called()
        mixed.clients[account['id']].submit.assert_not_called()
        assert mixed.store.account(account['id'])['status'] == 'active'


@pytest.mark.parametrize('kind', ['video', 'image'])
def test_all_accounts_missing_model_fail_promptly_and_do_not_create_paid_or_prepared_work(mixed, kind):
    accounts = [mixed.add(f'missing-{i}', mode='canvas_cli', priority=9-i, kind=kind) for i in range(3)]
    identifier = mixed.create(kind)
    model = mixed.store.task(identifier)['model']
    for account in accounts:
        mixed.clients[account['id']].preflight_model.side_effect = missing_model(model, kind)
    with patch.object(mixed.service, '_canvas_local_assets') as download:
        mixed.service._run(identifier)
    result = mixed.store.task(identifier)
    assert result['status'] == 'failed' and result['error_code'] == 'CLI_CAPABILITY_UNAVAILABLE'
    assert 'does not expose requested model' in result['error_message']
    assert not result['attempted_account_ids'] and result['actual_cost'] is None
    assert len(result['upstream_context']['model_eligibility_exclusions']) == 3
    download.assert_not_called()
    mixed.service._stop.wait.assert_not_called()
    assert all(not client.prepare.called and not client.submit.called for client in mixed.clients.values())


@pytest.mark.parametrize('kind', ['video', 'image'])
def test_model_eligibility_is_request_local_and_does_not_negative_cache_account_permissions(mixed, kind):
    first = mixed.add('initially-missing', mode='canvas_cli', priority=9, kind=kind)
    backup = mixed.add('supported', mode='canvas_cli', priority=1, kind=kind)
    first_id = mixed.create(kind)
    model = mixed.store.task(first_id)['model']
    client = mixed.clients[first['id']]
    client.preflight_model.side_effect = missing_model(model, kind)
    mixed.service._run(first_id)
    assert mixed.store.task(first_id)['account_id'] == backup['id']
    client.preflight_model.side_effect = None
    client.preflight_model.return_value = {'model': 'newly-enabled-model'}
    second_id = mixed.create(kind)
    mixed.service._run(second_id)
    second = mixed.store.task(second_id)
    assert second['status'] == 'succeeded' and second['account_id'] == first['id']
    assert not second['upstream_context'].get('model_eligibility_exclusions')


@pytest.mark.parametrize('kind', ['video', 'image'])
def test_model_exclusion_does_not_relax_existing_two_credit_attempt_limit(mixed, kind):
    missing = mixed.add('missing-model', mode='canvas_cli', priority=9, kind=kind)
    first = mixed.add('credit-rejected-cli', mode='canvas_cli', priority=8, kind=kind)
    second = mixed.add('credit-rejected-browser', mode='browser', priority=7, kind=kind)
    unused = mixed.add('unused-third-credit-account', mode='canvas_cli', priority=6, kind=kind)
    identifier = mixed.create(kind)
    mixed.clients[missing['id']].preflight_model.side_effect = missing_model(mixed.store.task(identifier)['model'], kind)
    mixed.clients[first['id']].submit.side_effect = CanvasCLIError('INSUFFICIENT_CREDITS', 'not enough credits left')
    mixed.browser[second['id']].submit.side_effect = BrowserRejected('Dreamina ret=1006: Not enough credits left')
    mixed.service._run(identifier)
    result = mixed.store.task(identifier)
    assert result['status'] == 'failed' and result['error_code'] == 'INSUFFICIENT_CREDITS'
    assert result['attempted_account_ids'] == [first['id'], second['id']]
    assert [entry['account_id'] for entry in result['upstream_context']['model_eligibility_exclusions']] == [missing['id']]
    mixed.clients[missing['id']].prepare.assert_not_called()
    mixed.clients[unused['id']].identity.assert_not_called()


@pytest.mark.parametrize('kind', ['video', 'image'])
def test_model_exclusion_and_identity_retry_keep_one_credit_budget_and_original_deadline(mixed, kind):
    missing = mixed.add('missing-model', mode='canvas_cli', priority=9, kind=kind)
    interrupted = mixed.add('identity-network-failure', mode='canvas_cli', priority=8, kind=kind)
    supported = mixed.add('supported', mode='canvas_cli', priority=7, kind=kind)
    identifier = mixed.create(kind)
    mixed.clients[missing['id']].preflight_model.side_effect = missing_model(mixed.store.task(identifier)['model'], kind)
    mixed.clients[interrupted['id']].identity.side_effect = CanvasCLIError('CLI_COMMAND_TIMEOUT', 'identity read timed out')
    mixed.service._run(identifier)
    result = mixed.store.task(identifier)
    assert result['status'] == 'succeeded' and result['account_id'] == supported['id']
    assert result['attempted_account_ids'] == [interrupted['id'], supported['id']]
    audit = result['upstream_context']
    assert audit['model_eligibility_exclusions'][0]['account_id'] == missing['id']
    assert audit['preflight_failures'][0]['account_id'] == interrupted['id']
    assert audit['observation_deadline'] <= min(audit['model_eligibility_exclusions'][0]['task_deadline'],
                                              audit['preflight_failures'][0]['task_deadline'])
    mixed.clients[missing['id']].prepare.assert_not_called()
    mixed.clients[interrupted['id']].prepare.assert_not_called()


@pytest.mark.parametrize('kind', ['video', 'image'])
def test_model_skip_retains_initial_budget_selection_without_repreparing_funded_account(mixed, kind):
    missing = mixed.add('missing-model', mode='canvas_cli', priority=9, kind=kind)
    larger = mixed.add('larger', mode='canvas_cli', balance=30, priority=5, kind=kind)
    fitting = mixed.add('fitting', mode='canvas_cli', balance=20, priority=5, kind=kind)
    lower = mixed.add('lower-priority', mode='canvas_cli', balance=15, priority=4, kind=kind)
    identifier = mixed.create(kind)
    assert mixed.store.task(identifier)['estimated_cost'] == (10 if kind == 'video' else 0)
    mixed.clients[missing['id']].preflight_model.side_effect = missing_model(mixed.store.task(identifier)['model'], kind)
    with patch.object(mixed.service, '_canvas_local_assets',
                      return_value={'images': [], 'videos': [], 'audios': []}) as material:
        mixed.service._run(identifier)
    result = mixed.store.task(identifier)
    # Known video cost chooses the smallest sufficient balance up front;
    # unknown image cost probes the larger balance and keeps its funded quote.
    expected, unused = (fitting, larger) if kind == 'video' else (larger, fitting)
    assert result['status'] == 'succeeded' and result['account_id'] == expected['id']
    assert result['attempted_account_ids'] == [expected['id']]
    assert result['estimated_cost'] == result['actual_cost'] == 10
    assert result['upstream_context']['quoted_credits'] == 10
    assert [entry['account_id'] for entry in result['upstream_context']['model_eligibility_exclusions']] == [missing['id']]
    mixed.clients[missing['id']].preflight_model.assert_called_once()
    mixed.clients[missing['id']].prepare.assert_not_called()
    mixed.clients[missing['id']].submit.assert_not_called()
    material.assert_called_once()
    assert material.call_args.args[1]['id'] == expected['id']
    mixed.clients[expected['id']].prepare.assert_called_once()
    mixed.clients[expected['id']].submit.assert_called_once()
    assert mixed.clients[expected['id']].submit.call_args.args[0]['account_id'] == expected['id']
    mixed.clients[unused['id']].identity.assert_not_called()
    mixed.clients[unused['id']].prepare.assert_not_called()
    mixed.clients[unused['id']].submit.assert_not_called()
    mixed.clients[lower['id']].identity.assert_not_called()


@pytest.mark.parametrize('kind', ['video', 'image'])
@pytest.mark.parametrize('invalid', ['unmarked', 'wrong_model', 'wrong_kind', 'wrong_reason', 'unknown', 'prepare'])
def test_only_exact_readonly_model_catalog_marker_can_skip_account(mixed, kind, invalid):
    first = mixed.add('first', mode='canvas_cli', priority=9, kind=kind)
    backup = mixed.add('backup', mode='canvas_cli', priority=8, kind=kind)
    identifier = mixed.create(kind)
    error = missing_model(mixed.store.task(identifier)['model'], kind)
    if invalid == 'unmarked':
        error.details = {}
    elif invalid == 'wrong_model':
        error.details['error']['requested_model'] = 'different-public-model'
    elif invalid == 'wrong_kind':
        error.details['error']['kind'] = 'image' if kind == 'video' else 'video'
    elif invalid == 'wrong_reason':
        error.details['error']['reason'] = 'resolution_not_supported'
    elif invalid == 'unknown':
        error.outcome_unknown = True
    if invalid == 'prepare':
        mixed.clients[first['id']].prepare.side_effect = error
    else:
        mixed.clients[first['id']].preflight_model.side_effect = error
    mixed.service._run(identifier)
    result = mixed.store.task(identifier)
    assert result['status'] == 'failed' and result['account_id'] == first['id']
    assert not result['upstream_context'].get('model_eligibility_exclusions')
    mixed.clients[backup['id']].identity.assert_not_called()
    assert all(not client.submit.called for client in mixed.clients.values())


@pytest.mark.parametrize('kind', ['video', 'image'])
@pytest.mark.parametrize('expired', [False, True])
def test_restart_retains_model_exclusion_and_fixed_original_wall_deadline(mixed, kind, expired):
    first = mixed.add('previously-missing', mode='canvas_cli', priority=9, kind=kind)
    supported = mixed.add('supported', mode='canvas_cli', priority=1, kind=kind)
    identifier = mixed.create(kind)
    old_deadline = time.time() + (-1 if expired else 10)
    audit = {'model_eligibility_exclusions': [{'account_id': first['id'],
        'requested_model': mixed.store.task(identifier)['model'], 'kind': kind,
        'task_deadline': old_deadline, 'dispatch_started': False, 'phase': 'model_selection'}]}
    mixed.store.update_task(identifier, {'upstream_context': deepcopy(audit), 'execution_backend': ''})
    restarted = Service(Store(str(mixed.store.path)))
    try:
        restarted._cost_estimate = Mock(return_value=(10, {'source': 'synthetic-history'}))
        restarted.canvas_client = Mock(side_effect=lambda a: mixed.clients[a['id']])
        restarted.canvas_cli.run = Mock(side_effect=AssertionError('real CLI forbidden'))
        restarted.browser_runtime.acquire = Mock(side_effect=AssertionError('browser forbidden'))
        restarted.browser_runtime.release = Mock()
        restarted._stop.wait = Mock(return_value=False)
        restarted._run(identifier)
        result = mixed.store.task(identifier)
        assert result['upstream_context']['model_eligibility_exclusions'] == audit['model_eligibility_exclusions']
        mixed.clients[first['id']].identity.assert_not_called()
        if expired:
            assert result['status'] == 'failed' and result['error_code'] == 'CLI_PREPARATION_TIMEOUT'
            mixed.clients[supported['id']].identity.assert_not_called()
        else:
            assert result['status'] == 'succeeded' and result['account_id'] == supported['id']
            assert result['upstream_context']['observation_deadline'] <= old_deadline
        assert not restarted._credit_reservations and sum(restarted._active.values()) == 0
        assert restarted._worker_slots._value == 50
        restarted.canvas_cli.run.assert_not_called()
    finally:
        restarted.stop()


@pytest.mark.parametrize('kind', ['video', 'image'])
def test_model_catalog_skip_keeps_deadline_when_next_account_is_browser(mixed, monkeypatch, kind):
    first = mixed.add('missing-cli', mode='canvas_cli', priority=9, kind=kind)
    backup = mixed.add('browser', mode='browser', priority=8, kind=kind)
    identifier = mixed.create(kind)
    mixed.store.update_settings({'task_timeout_seconds': 5})
    clock, started_mono, started_wall = virtual_clock(monkeypatch)

    def unavailable(*args, **kwargs):
        clock['elapsed'] = 4
        raise missing_model(mixed.store.task(identifier)['model'], kind)

    mixed.clients[first['id']].preflight_model.side_effect = unavailable
    mixed.service._run(identifier)
    result = mixed.store.task(identifier)
    assert result['status'] == 'succeeded' and result['account_id'] == backup['id']
    assert result['attempted_account_ids'] == [backup['id']]
    assert result['upstream_context']['model_eligibility_exclusions'][0]['task_deadline'] == pytest.approx(started_wall + 5)
    assert mixed.poll_deadlines == pytest.approx([started_mono + 5])


@pytest.mark.parametrize('kind', ['video', 'image'])
def test_manual_termination_during_catalog_check_cannot_become_eligibility_retry(mixed, kind):
    first = mixed.add('first', mode='canvas_cli', priority=9, kind=kind)
    backup = mixed.add('backup', mode='canvas_cli', priority=8, kind=kind)
    identifier = mixed.create(kind)
    stopped = []

    def stop_then_report_missing(*args, **kwargs):
        stopped.append(mixed.store.update_task(identifier, {'status': 'failed',
            'error_code': 'OPERATOR_CONTENT_RESTRICTED', 'error_message': 'operator stopped',
            'upstream_context': {'manual_termination': {'reason': 'operator'}}}))
        raise missing_model(mixed.store.task(identifier)['model'], kind)

    mixed.clients[first['id']].preflight_model.side_effect = stop_then_report_missing
    mixed.service._run(identifier)
    assert mixed.store.task(identifier) == stopped[0]
    mixed.clients[first['id']].prepare.assert_not_called()
    mixed.clients[backup['id']].identity.assert_not_called()
