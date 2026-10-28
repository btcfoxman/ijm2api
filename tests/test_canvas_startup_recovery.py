"""Upgrade old parked submissions without replaying their paid operation."""
from unittest.mock import Mock, patch

import pytest

from app.catalog import normalize_request
from app.service import Service
from app.store import Store


@pytest.mark.parametrize('reason,closed,expected', [
    ('operation_submission_absent', False, True),
    ('observation_deadline_reached', False, True),
    ('result_pending', False, False),
    ('result_transfer_failed', False, False),
    ('operation_submission_absent', True, False),
])
def test_startup_resumes_only_unclosed_missing_receipts(tmp_path, reason, closed, expected):
    store = Store(str(tmp_path / 'tasks.db'))
    body = {'model': 'sd-2-0-mini', 'prompt': 'Landscape', 'duration': 4}
    store.create_task('task', body, normalize_request(body), execution_backend='canvas_cli')
    context = {'submission_phase': 'run_dispatching', 'submit_id': 'original-operation',
        'observation_deadline': 100, 'reconciliation_paused': True,
        'reconciliation_pause_reason': reason, 'reconciliation_closed': closed}
    store.update_task('task', {'status': 'submitted', 'error_code': 'UPSTREAM_SUBMIT_OUTCOME_UNKNOWN',
                              'upstream_context': context})
    service = Service(store)
    service.canvas_cli.verify_runtime = Mock(return_value={'available': True})
    service._pool.submit = Mock()
    try:
        with patch('app.service.threading.Thread'):
            service.start()
        if expected:
            service._pool.submit.assert_called_once_with(service._resume_canvas_poll, 'task')
            after = store.task('task')['upstream_context']
            assert not after['reconciliation_paused']
            assert after['observation_deadline'] == 100
            assert after['submit_id'] == 'original-operation'
        else:
            service._pool.submit.assert_not_called()
            assert store.task('task')['upstream_context'] == context
    finally:
        service.stop()
