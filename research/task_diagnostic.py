"""Read a task's non-input diagnostics without exposing prompts or media."""

import json
import sqlite3
import sys

with sqlite3.connect("file:/app/data/ijm2api.db?mode=ro", uri=True, timeout=10) as db:
    db.row_factory = sqlite3.Row
    row = db.execute(
        "SELECT id,status,error_code,error_message,account_id,run_id,thread_id,progress "
        "FROM tasks WHERE id=?", (sys.argv[1],)
    ).fetchone()
    if row:
        result = dict(row)
        result["has_upstream_run"] = bool(result.pop("run_id"))
        result["has_history_id"] = bool(result.pop("thread_id"))
        print(json.dumps(result, ensure_ascii=False))
    else:
        print("task not found")
