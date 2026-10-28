from unittest.mock import Mock

import pytest

from test_canvas_admin_api import context


def test_reconciliation_is_admin_only_and_only_calls_observation(context):
    main, api, headers, _ = context
    main.service.reconcile_canvas_task = Mock(return_value={'id': 'task', 'status': 'submitted',
        'execution_backend': 'canvas_cli', 'upstream_context': {'submit_id': 'original',
        'reconciliation_paused': True, 'private_identity': 'do-not-expose'}})
    assert api.post('/api/tasks/task/reconcile').status_code == 401
    assert api.post('/api/tasks/task/reconcile', headers={'X-API-Key': 'synthetic-canvas-api'}).status_code == 401
    response = api.post('/api/tasks/task/reconcile', headers=headers)
    assert response.status_code == 200
    assert response.json()['execution_diagnostics']['reconciliation_paused'] is True
    assert 'upstream_context' not in response.json()
    assert 'do-not-expose' not in response.text
    main.service.reconcile_canvas_task.assert_called_once_with('task')


def test_busy_reconciliation_and_stale_retry_are_conflicts(context):
    main, api, headers, _ = context
    main.service.reconcile_canvas_task = Mock(side_effect=RuntimeError('original operation is being observed'))
    assert api.post('/api/tasks/task/reconcile', headers=headers).status_code == 409
