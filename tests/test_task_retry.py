"""Operator recovery is restricted to recent tasks which never submitted."""
from concurrent.futures import ThreadPoolExecutor
import importlib
import json
import time
from unittest.mock import Mock

from fastapi.testclient import TestClient
import pytest

from app.catalog import normalize_request
from app.service import Service
from app.store import Store


@pytest.fixture
def context(tmp_path, monkeypatch):
    store = Store(str(tmp_path / "tasks.db"))
    service = Service(store)
    monkeypatch.setattr(service._pool, "submit", Mock())
    yield store, service
    service.stop()


def failed_task(store, task_id="synthetic-task"):
    request = {"model": "sd-2-0-mini", "duration": 4, "resolution": "720p",
               "prompt": "A synthetic landscape", "image_urls": ["https://media.example.test/image.png"]}
    store.create_task(task_id, request, normalize_request(request))
    return store.update_task(task_id, {"status": "failed", "progress": 100,
        "error_code": "ACCOUNT_POOL_UNAVAILABLE", "error_message": "no account before submission",
        "estimated_cost": 42, "completed_at": time.time()})


def test_retry_keeps_original_id_request_normalized_media_and_creation(context):
    store, service = context
    old = failed_task(store)
    with store._connect() as db:
        raw = dict(db.execute("SELECT * FROM tasks WHERE id=?", (old["id"],)).fetchone())
    result = service.retry_unsubmitted_task(old["id"])
    assert result["id"] == old["id"] and result["status"] == "queued"
    assert "error" not in result
    service._pool.submit.assert_called_once_with(service._run, old["id"])
    with store._connect() as db:
        changed = dict(db.execute("SELECT * FROM tasks WHERE id=?", (old["id"],)).fetchone())
    for field in ("id", "model", "request_json", "normalized_json", "media_inputs_json", "created_at", "estimated_cost"):
        assert changed[field] == raw[field]
    assert changed["account_id"] is None and changed["completed_at"] is None
    assert changed["error_code"] == changed["error_message"] == ""
    assert changed["run_id"] == changed["thread_id"] == ""
    assert changed["progress"] == 0


@pytest.mark.parametrize("changes", [
    {"status": "queued"}, {"status": "preparing"}, {"status": "running"},
    {"status": "succeeded"}, {"status": "expired"}, {"status": "submission_unknown"},
    {"error_code": "UPSTREAM_SUBMIT_OUTCOME_UNKNOWN"},
    {"error_code": "INSUFFICIENT_CREDITS"}, {"error_code": "UPSTREAM_REJECTED"},
    {"run_id": "accepted-run"}, {"thread_id": "accepted-thread"},
    {"attempted_account_ids": [7]}, {"actual_cost": 1},
    {"result_urls": ["https://media.example.test/result.mp4"]},
    {"upstream_result_urls": ["https://media.example.test/result.mp4"]},
])
def test_non_retryable_tasks_remain_unchanged_and_never_dispatch(context, changes):
    store, service = context
    old = failed_task(store)
    before = store.update_task(old["id"], changes)
    with pytest.raises(ValueError, match="not safe to retry"):
        service.retry_unsubmitted_task(old["id"])
    assert store.task(old["id"]) == before
    service._pool.submit.assert_not_called()


@pytest.mark.parametrize("age", [3600, 3601, -1])
def test_only_tasks_created_within_last_hour_can_retry(context, monkeypatch, age):
    store, service = context
    old = failed_task(store)
    now = 200_000.0
    with store._connect() as db:
        db.execute("UPDATE tasks SET created_at=? WHERE id=?", (now - age, old["id"]))
    monkeypatch.setattr("app.store.time.time", lambda: now)
    before = store.task(old["id"])
    with pytest.raises(ValueError, match="not safe to retry"):
        service.retry_unsubmitted_task(old["id"])
    assert store.task(old["id"]) == before
    service._pool.submit.assert_not_called()


@pytest.mark.parametrize("attempts", ["not-json", "null", "{}", "[7]"])
def test_corrupt_or_nonempty_attempt_evidence_fails_closed(context, attempts):
    store, service = context
    old = failed_task(store)
    with store._connect() as db:
        db.execute("UPDATE tasks SET attempted_account_ids_json=? WHERE id=?", (attempts, old["id"]))
    with pytest.raises(ValueError, match="not safe to retry"):
        service.retry_unsubmitted_task(old["id"])
    service._pool.submit.assert_not_called()
    with store._connect() as db:
        row = db.execute("SELECT status,attempted_account_ids_json FROM tasks WHERE id=?", (old["id"],)).fetchone()
    assert row["status"] == "failed" and row["attempted_account_ids_json"] == attempts


def test_concurrent_retry_compare_and_set_dispatches_once(context):
    store, service = context
    old = failed_task(store)

    def retry():
        try:
            return service.retry_unsubmitted_task(old["id"])["status"]
        except ValueError:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(lambda _: retry(), range(2)))
    assert sorted(results) == ["conflict", "queued"]
    service._pool.submit.assert_called_once_with(service._run, old["id"])


def test_dispatch_failure_stays_proven_unsubmitted_and_can_be_retried(context):
    store, service = context
    old = failed_task(store)
    service._pool.submit.side_effect = RuntimeError("executor is closed")
    with pytest.raises(ValueError, match="dispatch unavailable"):
        service.retry_unsubmitted_task(old["id"])
    current = store.task(old["id"])
    assert current["status"] == "failed" and current["error_code"] == "ACCOUNT_POOL_UNAVAILABLE"
    assert current["run_id"] == current["thread_id"] == ""
    assert current["attempted_account_ids"] == [] and current["request"] == old["request"]
    service._pool.submit.side_effect = None
    assert service.retry_unsubmitted_task(old["id"])["status"] == "queued"


def test_stopped_scheduler_does_not_requeue(context):
    store, service = context
    old = failed_task(store)
    service._stop.set()
    with pytest.raises(ValueError, match="scheduling is stopped"):
        service.retry_unsubmitted_task(old["id"])
    assert store.task(old["id"]) == old
    service._pool.submit.assert_not_called()


def test_retry_respects_existing_queue_capacity(context):
    store, service = context
    old = failed_task(store)
    store.update_settings({"task_workers": 1, "task_queue_capacity": 1})
    for index in range(2):
        store.create_task(f"queued-{index}", old["request"], old["normalized"])
    with pytest.raises(ValueError, match="queue is full"):
        service.retry_unsubmitted_task(old["id"])
    assert store.task(old["id"]) == old
    service._pool.submit.assert_not_called()


def test_separate_store_transactions_requeue_only_once(context):
    store, _ = context
    old = failed_task(store)
    other = Store(str(store.path))

    def retry(current):
        try:
            return current.requeue_unsubmitted_task(old["id"])["status"]
        except ValueError:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(retry, (store, other)))
    assert sorted(results) == ["conflict", "queued"]


def test_retry_endpoint_requires_admin_and_rejects_duplicates(context, tmp_path, monkeypatch):
    store, service = context
    monkeypatch.setenv("IJM_API_KEY", "synthetic-api")
    monkeypatch.setenv("IJM_ADMIN_TOKEN", "synthetic-admin")
    monkeypatch.setenv("IJM_DATA_DIR", str(tmp_path / "api"))
    main = importlib.import_module("app.main")
    monkeypatch.setattr(main, "store", store)
    monkeypatch.setattr(main, "service", service)
    monkeypatch.setattr(main, "ADMIN_TOKEN", "synthetic-admin")
    api = TestClient(main.app)  # No lifespan, browser, or generation operation.
    old = failed_task(store)
    endpoint = f"/api/tasks/{old['id']}/retry"
    assert api.post(endpoint).status_code == 401
    assert api.post(endpoint, headers={"X-API-Key": "synthetic-api"}).status_code == 401
    headers = {"X-Admin-Token": "synthetic-admin"}
    assert api.post("/api/tasks/missing/retry", headers=headers).status_code == 404
    result = api.post(endpoint, headers=headers)
    assert result.status_code == 202 and result.json()["id"] == old["id"]
    assert result.json()["status"] == "queued"
    assert api.post(endpoint, headers=headers).status_code == 409
    unknown = failed_task(store, "unknown-task")
    store.update_task(unknown["id"], {"error_code": "UPSTREAM_SUBMIT_OUTCOME_UNKNOWN"})
    assert api.post(f"/api/tasks/{unknown['id']}/retry", headers=headers).status_code == 409
    service._pool.submit.assert_called_once_with(service._run, old["id"])


def test_history_projection_retains_original_upstream_run_id(context):
    store, _ = context
    old = failed_task(store)
    store.update_task(old["id"], {"status": "succeeded", "actual_cost": 42, "run_id": "synthetic-run"})
    history = store.scheduling_rows()["history"]
    assert len(history) == 1 and history[0]["run_id"] == "synthetic-run"
    assert json.loads(history[0]["request_json"]) == old["request"]
