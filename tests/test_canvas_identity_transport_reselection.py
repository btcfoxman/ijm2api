"""Typed read-only auth transport failures reselect, never replay paid work."""
from copy import deepcopy
import time
from unittest.mock import patch

import pytest

from app.canvas_cli import CanvasCLIError
from app.canvas_client import identity_network_error
from app.canvas_service import CanvasServiceMixin
from test_canvas_execution import context, fake_client, owner, preflight_task


def transport(*, fields=None, details=None, unknown=False, code='cli.transport_failed'):
    return CanvasCLIError(code, 'Read-only identity transport failed', outcome_unknown=unknown,
        details={'error': {'code': code, 'class': 'retryable_failure',
                          'requiredAction': 'retry', 'retryable': True, **(fields or {})}, **(details or {})})


@pytest.mark.parametrize('reason', [None, 'transport_error'])
def test_exact_reasonless_official_transport_shape_is_identity_retryable(reason):
    assert identity_network_error(transport(fields={} if reason is None else {'reason': reason}))


@pytest.mark.parametrize('fields,details,unknown,code', [
    ({'class': 'human_intervention', 'reason': 'network_error'}, {}, False, 'cli.transport_failed'),
    ({'class': 'authentication_failure', 'reason': 'network_error'}, {}, False, 'cli.transport_failed'),
    ({'class': 'business_failure', 'reason': 'network_error'}, {}, False, 'cli.transport_failed'),
    ({'requiredAction': 'login', 'reason': 'network_error'}, {}, False, 'cli.transport_failed'),
    ({'requiredAction': 'human_intervention'}, {}, False, 'cli.transport_failed'),
    ({'requiredAction': 'fix_input'}, {}, False, 'cli.transport_failed'),
    ({'retryable': False, 'reason': 'network_error'}, {}, False, 'cli.transport_failed'),
    ({'reason': 'invalid_input'}, {}, False, 'cli.transport_failed'),
    ({'serviceCode': 20115}, {}, False, 'cli.transport_failed'),
    ({}, {'phase': 'run_dispatching'}, False, 'cli.transport_failed'),
    ({}, {'receipt': {'accepted': True}}, False, 'cli.transport_failed'),
    ({}, {'partialData': {'operationRef': 'original-paid-operation'}}, False, 'cli.transport_failed'),
    ({}, {}, True, 'cli.transport_failed'),
    ({}, {}, False, 'CLI_AUTH_REQUIRED'),
    ({}, {}, False, 'cli.upload_state_unknown'),
])
def test_typed_transport_vetoes_cannot_be_bypassed_by_generic_network_reason(fields, details, unknown, code):
    assert not identity_network_error(transport(fields=fields, details=details, unknown=unknown, code=code))


@pytest.mark.parametrize('binding', ['run_id', 'thread_id', 'execution', 'project'])
def test_identity_reselection_requires_a_clean_preparation_call_stage(binding):
    task = {'normalized': {}, 'upstream_context': {}}
    if binding in {'run_id', 'thread_id'}:
        task[binding] = 'original-binding'
    elif binding == 'execution':
        task['normalized']['_execution'] = {'submit_id': 'original-binding'}
    else:
        task['upstream_context']['project_id'] = 'original-project'
    before = deepcopy(task)
    assert not CanvasServiceMixin._canvas_identity_network_retry(task, transport(), time.monotonic() + 30)
    assert task == before


@pytest.mark.parametrize('kind', ['video', 'image'])
def test_reasonless_identity_transport_immediately_excludes_bad_owner_before_one_paid_submit(context, kind):
    store, service, _ = context
    accounts = [owner(store, 'unreachable', priority=5), owner(store, 'healthy', priority=4)]
    identifier = preflight_task(store, service, kind)
    clients = {a['id']: fake_client(store, service, a, kind=kind) for a in accounts}
    clients[accounts[0]['id']].identity.side_effect = transport()
    with patch.object(service, 'canvas_client', side_effect=lambda a: clients[a['id']]):
        service._run(identifier)
    row = store.task(identifier)
    assert row['status'] == 'succeeded' and row['account_id'] == accounts[1]['id']
    clients[accounts[0]['id']].identity.assert_called_once()
    clients[accounts[0]['id']].prepare.assert_not_called()
    clients[accounts[0]['id']].submit.assert_not_called()
    clients[accounts[1]['id']].prepare.assert_called_once()
    clients[accounts[1]['id']].submit.assert_called_once()
    assert 'preparation_retry_budget' not in row['upstream_context']
    failures = row['upstream_context']['preflight_failures']
    assert len(failures) == 1 and failures[0]['phase'] == 'identity'
    assert failures[0]['dispatch_started'] is False and failures[0]['account_id'] == accounts[0]['id']
    assert row['attempted_account_ids'] == [a['id'] for a in accounts]
    bad = store.account(accounts[0]['id'])
    assert bad['status'] == 'active' and bad['cli_auth_status'] == 'ready'
    assert not service._credit_reservations and sum(service._active.values()) == 0


@pytest.mark.parametrize('kind', ['video', 'image'])
def test_two_reasonless_auth_failures_do_not_open_a_third_account_or_create_a_preparation_window(context, kind):
    store, service, _ = context
    accounts = [owner(store, f'owner-{i}', priority=5-i) for i in range(3)]
    identifier = preflight_task(store, service, kind)
    clients = {a['id']: fake_client(store, service, a, kind=kind) for a in accounts}
    for account in accounts[:2]:
        clients[account['id']].identity.side_effect = transport()
    with patch.object(service, 'canvas_client', side_effect=lambda a: clients[a['id']]):
        service._run(identifier)
    row = store.task(identifier)
    assert row['status'] == 'failed' and row['error_code'] == 'cli.transport_failed'
    assert row['attempted_account_ids'] == [a['id'] for a in accounts[:2]]
    assert len(row['upstream_context']['preflight_failures']) == 2
    assert 'preparation_retry_budget' not in row['upstream_context']
    clients[accounts[2]['id']].identity.assert_not_called()
    assert all(not c.prepare.called and not c.submit.called for c in clients.values())
    assert row['actual_cost'] is None and not service._credit_reservations
