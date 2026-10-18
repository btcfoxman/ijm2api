"""Print only active IJM task counts before a service restart."""

import sqlite3


with sqlite3.connect("file:/app/data/ijm2api.db?mode=ro", uri=True, timeout=10) as db:
    rows = db.execute(
        "SELECT status, COUNT(*) FROM tasks WHERE status IN "
        "('queued','preparing','submitted','running') GROUP BY status"
    ).fetchall()
print(dict(rows))
