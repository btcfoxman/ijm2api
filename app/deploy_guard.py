"""Read-only deployment guard; parked reconciliation retains its credit hold."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3


def active_work(database: str | Path, proc_root: Path = Path('/proc')) -> int:
    with sqlite3.connect(Path(database).resolve().as_uri() + '?mode=ro', uri=True, timeout=5) as db:
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA query_only=ON')
        columns = {r[1] for r in db.execute('PRAGMA table_info(tasks)')}
        optional = [key for key in ('execution_backend', 'upstream_context_json', 'account_id') if key in columns]
        count = 0
        for row in db.execute('SELECT id,status,error_code' + ''.join(',' + k for k in optional)
                              + " FROM tasks WHERE status IN ('queued','preparing','submitting','submitted','running')"):
            parked = False
            if set(optional) == {'execution_backend', 'upstream_context_json', 'account_id'}:
                try:
                    context = json.loads(row['upstream_context_json'])
                    parked = (row['execution_backend'] == 'canvas_cli'
                        and row['status'] in {'submitted', 'running'}
                        and row['error_code'] == 'UPSTREAM_SUBMIT_OUTCOME_UNKNOWN'
                        and context.get('reconciliation_paused') is True
                        and context.get('task_id') == row['id']
                        and context.get('account_id') == row['account_id']
                        and bool(context.get('submit_id')))
                except (ValueError, TypeError, AttributeError):
                    pass
            count += not parked
        count += db.execute("SELECT count(*) FROM accounts WHERE status='logging_in'").fetchone()[0]
        columns = {r[1] for r in db.execute('PRAGMA table_info(accounts)')}
        if 'cli_auth_status' in columns:
            count += db.execute("SELECT count(*) FROM accounts WHERE cli_auth_status IN ('queued','authorizing','manual_required')").fetchone()[0]
    # A stale database flag must never permit a restart during paid dispatch.
    for process in proc_root.glob('[0-9]*'):
        try:
            args = (process / 'cmdline').read_bytes().split(b'\0')
            if args and Path(os.fsdecode(args[0])).name == 'dreamina-canvas':
                count += any(args[i:i + 2] == [b'node', b'run'] for i in range(len(args) - 1))
        except (FileNotFoundError, ProcessLookupError):
            pass
        except PermissionError:
            count += 1
    return int(count)


if __name__ == '__main__':
    print(active_work(Path(os.environ.get('IJM_DATA_DIR', '/app/data')) / 'ijm2api.db'))
