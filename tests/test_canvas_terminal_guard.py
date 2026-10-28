import sqlite3

import pytest

from app.catalog import normalize_request
from app.store import Store


@pytest.fixture
def task(tmp_path):
    store = Store(str(tmp_path / 'guard.db'))
    body = {'model': 'sd-2-0-mini', 'prompt': 'Landscape', 'duration': 4}
    store.create_task('task', body, normalize_request(body), execution_backend='canvas_cli')
    return store


@pytest.mark.parametrize('status', ['failed', 'succeeded', 'expired', 'cancelled'])
def test_late_observation_cannot_replace_a_final_outcome(task, status):
    task.update_task('task', {'status': status, 'error_code': 'OPERATOR_CONTENT_RESTRICTED',
                            'error_message': 'explicit final message', 'progress': 100})
    before = task.task('task')
    after = task.update_task('task', {'status': 'submitted', 'error_code': 'UPSTREAM_SUBMIT_OUTCOME_UNKNOWN',
                                    'progress': 40, 'upstream_context': {}}, preserve_terminal=True)
    assert after == before
    assert task.task('task') == before


def test_manual_marker_survives_even_an_old_incorrect_unknown_timeout(task):
    task.update_task('task', {'status': 'expired', 'error_code': 'UPSTREAM_SUBMIT_OUTCOME_UNKNOWN',
        'upstream_context': {'manual_termination': {'source': 'explicit_user_instruction'}}})
    before = task.task('task')
    assert task.update_task('task', {'status': 'running', 'upstream_context': {}}, preserve_terminal=True) == before
    assert task.update_task('task', {'status': 'preparing', 'upstream_context': {}}) == before


def test_unknown_timeout_remains_recoverable_without_a_manual_stop(task):
    task.update_task('task', {'status': 'expired', 'error_code': 'UPSTREAM_SUBMIT_OUTCOME_UNKNOWN'})
    after = task.update_task('task', {'status': 'running', 'error_code': ''}, preserve_terminal=True)
    assert after['status'] == 'running'


def test_termination_from_another_connection_wins_over_a_stale_snapshot(task):
    stale = task.task('task')
    with sqlite3.connect(task.path) as db:
        db.execute("UPDATE tasks SET status='failed',error_code='OPERATOR_CONTENT_RESTRICTED',error_message='final' WHERE id='task'")
    result = task.update_task('task', {'status': 'running', 'upstream_context': stale['upstream_context']},
                              preserve_terminal=True)
    assert result['status'] == 'failed'
    assert result['error_message'] == 'final'
