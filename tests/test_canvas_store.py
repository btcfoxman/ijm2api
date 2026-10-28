"""Durable backend identities survive a dual-transport migration safely."""
import sqlite3
import time

import pytest

from app.catalog import normalize_request
from app.store import Store


def create(store, identifier="task", **kwargs):
    body = {"model": "sd-2-0-mini", "prompt": "A synthetic landscape", "duration": 4}
    return store.create_task(identifier, body, normalize_request(body), **kwargs)


def test_existing_accounts_default_cli_but_existing_tasks_remain_browser(tmp_path):
    path = tmp_path / "legacy.db"
    store = Store(str(path))
    owner = store.upsert_account({"name": "legacy", "proxy_url": "socks5://proxy.test:20001"})
    create(store, execution_backend="canvas_cli")
    store.update_task("task", {"status": "running", "run_id": "accepted", "thread_id": "history"})
    # Emulate the schema before the rollout without modifying its actual data.
    with sqlite3.connect(path) as db:
        for column in ("execution_mode", "cli_auth_status", "cli_auth_error", "cli_user_id"):
            db.execute(f"ALTER TABLE accounts DROP COLUMN {column}")
        for column in ("execution_backend", "upstream_context_json"):
            db.execute(f"ALTER TABLE tasks DROP COLUMN {column}")
    migrated = Store(str(path))
    account = migrated.account(owner["id"])
    assert account["execution_mode"] == "canvas_cli"
    assert account["cli_auth_status"] == "pending"
    task = migrated.task("task")
    assert task["execution_backend"] == "browser"
    assert (task["status"], task["run_id"], task["thread_id"]) == ("running", "accepted", "history")
    assert task["upstream_context"] == {}
    assert Store(str(path)).task("task") == task  # Migration is repeatable.


def test_new_task_unbound_and_bound_backend_is_independent_of_account_mode(tmp_path):
    store = Store(str(tmp_path / "tasks.db"))
    account = store.upsert_account({"name": "new"})
    assert account["execution_mode"] == "canvas_cli"
    assert create(store)["execution_backend"] == ""
    store.update_task("task", {"account_id": account["id"], "execution_backend": "canvas_cli",
                               "upstream_context": {"operation_id": "accepted", "canvas_id": "canvas"}})
    store.update_account(account["id"], {"execution_mode": "browser"})
    assert store.task("task")["execution_backend"] == "canvas_cli"
    assert store.task("task")["upstream_context"]["operation_id"] == "accepted"
    assert store.scheduling_rows()["inflight"][0]["execution_backend"] == "canvas_cli"


@pytest.mark.parametrize("value", [None, "", "auto", "shell", [], True])
def test_invalid_account_execution_mode_rejected(tmp_path, value):
    store = Store(str(tmp_path / "tasks.db"))
    with pytest.raises(ValueError, match="execution mode"):
        store.upsert_account({"name": "new", "execution_mode": value})


def test_media_cache_does_not_share_assets_between_backends_or_accounts(tmp_path):
    store = Store(str(tmp_path / "tasks.db"))
    store.media_cache_put(1, "images", "same-digest", {"image_uri": "web-image"})
    assert store.media_cache_get(1, "images", "same-digest", backend="canvas_cli") is None
    store.media_cache_put(1, "images", "same-digest", {"asset_id": "cli-image"}, backend="canvas_cli")
    assert store.media_cache_get(1, "images", "same-digest")["image_uri"] == "web-image"
    assert store.media_cache_get(1, "images", "same-digest", backend="canvas_cli")["asset_id"] == "cli-image"
    assert store.media_cache_get(2, "images", "same-digest", backend="canvas_cli") is None
    store.media_cache_delete(1, "images", "same-digest", backend="canvas_cli")
    assert store.media_cache_get(1, "images", "same-digest") is not None


def test_cli_recovery_context_blocks_replay_and_unknown_credit_hold_cannot_be_cleared(tmp_path):
    store = Store(str(tmp_path / "tasks.db"))
    create(store)
    store.update_task("task", {"status": "failed", "error_code": "ACCOUNT_POOL_UNAVAILABLE",
                               "upstream_context": {"operation_id": "possibly-accepted"}})
    with pytest.raises(ValueError, match="not safe to retry"):
        store.requeue_unsubmitted_task("task")
    store.update_task("task", {"account_id": 1, "execution_backend": "canvas_cli",
                               "error_code": "UPSTREAM_SUBMIT_OUTCOME_UNKNOWN"})
    create(store, "ordinary")
    store.update_task("ordinary", {"status": "failed", "error_code": "UPSTREAM_FAILED"})
    assert store.clear_terminal_tasks() == 1
    assert store.task("task") is not None
    assert store.scheduling_rows()["inflight"][0]["id"] == "task"
    assert store.active_account_count(1) == 1


@pytest.mark.parametrize("credential", ["access_token", "refresh_token", "cookie", "authorization", "device_code"])
def test_credentials_cannot_be_persisted_in_recovery_context(tmp_path, credential):
    store = Store(str(tmp_path / "tasks.db"))
    create(store)
    with pytest.raises(ValueError, match="credentials"):
        store.update_task("task", {"upstream_context": {"nested": [{credential: "synthetic-secret"}]}})
    with store._connect() as db:
        row = db.execute("SELECT upstream_context_json FROM tasks").fetchone()
    assert "synthetic-secret" not in row[0]


def test_observed_cost_groups_are_not_combined_between_backends(tmp_path):
    store = Store(str(tmp_path / "tasks.db"))
    for index, (backend, cost) in enumerate((("browser", 100), ("canvas_cli", 7))):
        create(store, str(index), execution_backend=backend)
        store.update_task(str(index), {"account_id": index + 1, "status": "succeeded", "actual_cost": cost,
                                       "completed_at": time.time()})
    rows = store.model_costs()
    assert {row["execution_backend"]: row["cost"] for row in rows} == {"browser": 100, "canvas_cli": 7}
