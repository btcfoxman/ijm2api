import json
from pathlib import Path
import sqlite3

import pytest

from app.deploy_guard import active_work


@pytest.fixture
def guard(tmp_path):
    path = tmp_path / 'guard.db'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE tasks(id,status,error_code,execution_backend,upstream_context_json,account_id)')
        db.execute('CREATE TABLE accounts(status,cli_auth_status)')
    proc = tmp_path / 'proc'; proc.mkdir()
    return path, proc


def insert(path, *, status='submitted', backend='canvas_cli', error='UPSTREAM_SUBMIT_OUTCOME_UNKNOWN', **overrides):
    context = {'task_id': 'task', 'account_id': 19, 'submit_id': 'original', 'reconciliation_paused': True}
    context.update(overrides)
    with sqlite3.connect(path) as db:
        db.execute('INSERT INTO tasks VALUES(?,?,?,?,?,?)', ('task', status, error, backend, json.dumps(context), 19))


def test_explicitly_parked_unknown_is_read_only_deployable(guard):
    path, proc = guard; insert(path)
    before = path.read_bytes()
    assert active_work(path, proc) == 0
    assert path.read_bytes() == before


@pytest.mark.parametrize('overrides', [dict(reconciliation_paused=False), dict(task_id='other'),
    dict(account_id=18), dict(submit_id=''), dict(status='preparing'), dict(backend='browser'), dict(error='')])
def test_inconsistent_or_active_submission_blocks_deploy(guard, overrides):
    path, proc = guard; insert(path, **overrides)
    assert active_work(path, proc) == 1


def test_running_paid_process_overrides_a_stale_park_flag(guard):
    path, proc = guard; insert(path)
    process = proc / '32'; process.mkdir()
    (process / 'cmdline').write_bytes(b'/usr/local/bin/dreamina-canvas\0--profile\0account19\0node\0run\0')
    assert active_work(path, proc) == 1
    (process / 'cmdline').write_bytes(b'/usr/local/bin/dreamina-canvas\0operation\0status\0original\0')
    assert active_work(path, proc) == 0


def test_login_or_authorization_still_blocks_restart(guard):
    path, proc = guard
    with sqlite3.connect(path) as db:
        db.executemany('INSERT INTO accounts VALUES(?,?)', [('logging_in', 'ready'), ('active', 'authorizing')])
    assert active_work(path, proc) == 2
