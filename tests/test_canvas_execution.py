"""CLI execution preserves paid-operation identity and account credit safety."""
from copy import deepcopy
import time
from unittest.mock import Mock, patch
import uuid

import pytest

from app.canvas_cli import CanvasCLIError
from app.catalog import normalize_request
from app.service import Service
from app.store import Store


@pytest.fixture
def context(tmp_path, monkeypatch):
    store = Store(str(tmp_path / "tasks.db"))
    store.update_settings({"canvas_cli_enabled": True, "task_timeout_seconds": 5})
    service = Service(store)
    monkeypatch.setattr(service._pool, "submit", Mock())
    monkeypatch.setattr(service.browser_runtime, "acquire", Mock(side_effect=AssertionError("CLI opened Chrome")))
    monkeypatch.setattr(service.browser_runtime, "release", Mock())
    monkeypatch.setattr(service._stop, "wait", Mock(return_value=False))
    stored = Mock(side_effect=lambda path, task_id, settings, **kwargs:
                  (f"https://results.example.test/{task_id}/{path.name}", {"key": path.name, "size_bytes": path.stat().st_size}))
    monkeypatch.setattr("app.canvas_service.store_original", stored)
    yield store, service, stored
    service.stop()


def owner(store, name="owner", *, balance=100, priority=0, mode="canvas_cli", authorized=True):
    value = store.upsert_account({"name": name, "proxy_url": "socks5://proxy.test:20001",
                                  "execution_mode": mode, "priority": priority, "max_concurrency": 3})
    store.update_account(value["id"], {"enabled": True, "status": "active", "balance": balance,
        "login_user_id": f"user-{value['id']}", "cli_user_id": f"user-{value['id']}",
        "watermark_ready": True, "agreement_ready": True,
        "cli_auth_status": "ready" if authorized else "pending"})
    return store.account(value["id"], private=True)


def task(store, identifier="task", *, backend="", **request):
    body = {"model": "sd-2-0-mini", "prompt": "Synthetic landscape", "duration": 4, **request}
    return store.create_task(identifier, body, normalize_request(body), execution_backend=backend)


def fake_client(store, service, account, *, quote=10, actual=None):
    client = Mock(account=account)
    client.identity.side_effect = lambda: {"user_id": account["login_user_id"],
        "available_balance": account["balance"] - (actual if actual is not None else quote) if client.submit.call_count else account["balance"]}

    def prepare(normalized, task_id, assets, *, persist):
        context = {"backend": "canvas_cli", "account_id": account["id"], "task_id": task_id,
            "profile": f"synthetic-{account['id']}", "project_id": f"project-{account['id']}",
            "node_id": f"node-{task_id}-{account['id']}", "submit_id": f"submit-{task_id}-{account['id']}",
            "kind": "video", "phase": "quoted", "quoted_credits": quote}
        persist(deepcopy(context))
        return context

    client.prepare.side_effect = prepare

    def submit(context, ceiling):
        durable = store.task(context["task_id"])
        assert durable["execution_backend"] == "canvas_cli"
        assert durable["upstream_context"]["submit_id"] == context["submit_id"]
        assert durable["upstream_context"]["submission_phase"] == "submitting"
        assert durable["estimated_cost"] == quote
        assert durable["actual_cost"] is None
        assert ceiling >= quote
        return {**context, "phase": "accepted", "resource_ids": ["accepted-resource"]}

    client.submit.side_effect = submit
    path = service.canvas_cli.account_directory(account) / "results" / "original.mp4"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"synthetic-original-video-bytes")
    client.poll.return_value = {"status": "succeeded", "progress": 100, "result_urls": [],
        "result_paths": [str(path)],
        "result_files": [{"path": str(path), "mime": "video/mp4", "resource_id": str(uuid.uuid4())}],
        "credit_facts": [{"source": "precharge", "amount": 777, "field": "resource.creditsAmount", "settled": False}]}
    return client


def test_cli_video_runs_without_browser_and_stores_original_output_before_return(context):
    store, service, stored = context
    account = owner(store)
    row = task(store)
    client = fake_client(store, service, account)
    with patch.object(service, "canvas_client", return_value=client):
        service._run(row["id"])
    result = store.task(row["id"])
    assert result["status"] == "succeeded", result["error_message"]
    assert result["execution_backend"] == "canvas_cli"
    assert result["result_urls"][0].startswith("https://results.example.test/")
    assert result["result_transfer"]["original_bytes"] is True
    assert result["estimated_cost"] == result["actual_cost"] == 10
    assert result["upstream_context"]["submission_phase"] == "submitted"
    client.submit.assert_called_once()
    service.browser_runtime.acquire.assert_not_called()
    stored.assert_called_once()
    assert not service._credit_reservations
    assert service._worker_slots._value == 50


def test_quote_and_original_context_are_committed_before_paid_submission(context):
    store, service, _ = context
    account = owner(store)
    task(store)
    client = fake_client(store, service, account, quote=23)
    with patch.object(service, "canvas_client", return_value=client):
        service._run("task")
    client.submit.assert_called_once()
    durable = store.task("task")
    assert durable["attempted_account_ids"] == [account["id"]]
    assert durable["normalized"]["_cost_evidence"]["canvas_quote"]["amount"] == 23
    assert durable["actual_cost"] != 777


def test_expired_preparation_never_starts_a_paid_generation(context):
    store, service, _ = context
    account = owner(store)
    task(store)
    client = fake_client(store, service, account)
    with patch.object(service, 'canvas_client', return_value=client):
        service._execute_canvas('task', account, time.monotonic() - 1)
    client.identity.assert_not_called()
    client.prepare.assert_not_called()
    client.submit.assert_not_called()
    assert store.task('task')['error_code'] == 'CLI_PREPARATION_TIMEOUT'


def test_deadline_reached_while_uploading_and_quoting_never_runs_paid_generation(context):
    store, service, _ = context
    account = owner(store)
    task(store)
    store.update_task('task', {'account_id': account['id'], 'status': 'preparing'})
    client = fake_client(store, service, account)
    original_prepare = client.prepare.side_effect
    clock = Mock()
    clock.monotonic.return_value = 100
    def prepare(*args, **kwargs):
        value = original_prepare(*args, **kwargs)
        clock.monotonic.return_value = 102
        return value
    client.prepare.side_effect = prepare
    with patch.object(service, 'canvas_client', return_value=client), patch('app.canvas_service.time', clock):
        service._execute_canvas('task', account, 101)
    client.prepare.assert_called_once()
    client.submit.assert_not_called()
    result = store.task('task')
    assert result['error_code'] == 'CLI_PREPARATION_TIMEOUT'
    assert result['attempted_account_ids'] == []
    assert not service._credit_reservations


def test_insufficient_quote_never_invokes_paid_run(context):
    store, service, _ = context
    account = owner(store, balance=5)
    task(store)
    client = fake_client(store, service, account, quote=10)
    with patch.object(service, "canvas_client", return_value=client):
        service._run("task")
    result = store.task("task")
    assert result["status"] == "failed"
    assert result["error_code"] in {"INSUFFICIENT_CREDITS", "ACCOUNT_POOL_UNAVAILABLE"}
    assert result["upstream_context"] == {}
    assert result["attempted_account_ids"] == []
    client.submit.assert_not_called()
    client.poll.assert_not_called()


def test_same_priority_reselects_smallest_funded_account_before_paid_run(context):
    store, service, _ = context
    big = owner(store, "big", balance=100)
    small = owner(store, "small", balance=15)
    tiny = owner(store, "tiny", balance=5)
    task(store)
    clients = {a["id"]: fake_client(store, service, a, quote=10) for a in (big, small, tiny)}
    with patch.object(service, "canvas_client", side_effect=lambda a: clients[a["id"]]):
        service._run("task")
    result = store.task("task")
    assert result["status"] == "succeeded", result["error_message"]
    assert result["account_id"] == small["id"]
    assert result["attempted_account_ids"] == [small["id"]]
    clients[big["id"]].submit.assert_not_called()
    clients[tiny["id"]].submit.assert_not_called()
    clients[small["id"]].submit.assert_called_once()


def test_explicit_priority_precedes_same_priority_credit_fit(context):
    store, service, _ = context
    high = owner(store, "high", balance=100, priority=5)
    low = owner(store, "low", balance=15, priority=4)
    task(store)
    clients = {a["id"]: fake_client(store, service, a, quote=10) for a in (high, low)}
    with patch.object(service, "canvas_client", side_effect=lambda a: clients[a["id"]]):
        service._run("task")
    assert store.task("task")["account_id"] == high["id"]
    clients[high["id"]].submit.assert_called_once()
    clients[low["id"]].submit.assert_not_called()


def test_lost_acceptance_receipt_queries_original_ids_and_never_repeats_submit(context):
    store, service, _ = context
    account = owner(store)
    task(store)
    client = fake_client(store, service, account)
    client.submit.side_effect = CanvasCLIError("CLI_TIMEOUT", "synthetic receipt lost", outcome_unknown=True)
    with patch.object(service, "canvas_client", return_value=client):
        service._run("task")
    result = store.task("task")
    assert result["status"] == "succeeded", result["error_message"]
    client.submit.assert_called_once()
    submitted = client.submit.call_args.args[0]
    polled = client.poll.call_args.args[0]
    assert polled["submit_id"] == submitted["submit_id"]
    assert polled["node_id"] == submitted["node_id"]
    assert polled["project_id"] == submitted["project_id"]


def test_read_only_poll_failure_retries_query_not_generation(context):
    store, service, _ = context
    account = owner(store)
    task(store)
    client = fake_client(store, service, account)
    successful = client.poll.return_value
    client.poll.side_effect = [CanvasCLIError("CLI_NETWORK_ERROR", "synthetic query interruption"), successful]
    with patch.object(service, "canvas_client", return_value=client):
        service._run("task")
    assert store.task("task")["status"] == "succeeded"
    assert client.poll.call_count == 2
    client.submit.assert_called_once()
    assert client.poll.call_args_list[0].args[0]["submit_id"] == client.poll.call_args_list[1].args[0]["submit_id"]


def test_verified_cli_credit_rejection_uses_shared_next_account_policy(context):
    store, service, _ = context
    first = owner(store, "primary", balance=100, priority=5)
    second = owner(store, "fallback", balance=100, priority=4)
    task(store)
    clients = {a["id"]: fake_client(store, service, a, quote=10) for a in (first, second)}
    clients[first["id"]].submit.side_effect = CanvasCLIError(
        "INSUFFICIENT_CREDITS", "not enough credits left", outcome_unknown=False)
    with patch.object(service, "canvas_client", side_effect=lambda a: clients[a["id"]]):
        service._run("task")
    result = store.task("task")
    assert result["status"] == "succeeded", result["error_message"]
    assert result["account_id"] == second["id"]
    assert result["attempted_account_ids"] == [first["id"], second["id"]]
    assert result["upstream_context"]["account_id"] == second["id"]
    assert result["error_code"] == ""
    clients[first["id"]].submit.assert_called_once()
    clients[first["id"]].poll.assert_not_called()
    clients[second["id"]].submit.assert_called_once()


def test_cli_credit_fallback_keeps_existing_two_attempt_limit(context):
    store, service, _ = context
    accounts = [owner(store, f"owner-{i}", balance=100, priority=5-i) for i in range(3)]
    task(store)
    clients = {a["id"]: fake_client(store, service, a, quote=10) for a in accounts}
    for client in clients.values():
        client.submit.side_effect = CanvasCLIError("INSUFFICIENT_CREDITS", "not enough credits left", outcome_unknown=False)
    with patch.object(service, "canvas_client", side_effect=lambda a: clients[a["id"]]):
        service._run("task")
    result = store.task("task")
    assert result["status"] == "failed"
    assert result["error_code"] == "INSUFFICIENT_CREDITS"
    assert result["attempted_account_ids"] == [a["id"] for a in accounts[:2]]
    clients[accounts[2]["id"]].submit.assert_not_called()


def test_unknown_cli_acceptance_retains_account_and_never_uses_credit_fallback(context):
    store, service, _ = context
    first = owner(store, "primary", priority=5)
    second = owner(store, "fallback", priority=4)
    task(store)
    clients = {a["id"]: fake_client(store, service, a) for a in (first, second)}
    clients[first["id"]].submit.side_effect = CanvasCLIError("CLI_TIMEOUT", "receipt lost", outcome_unknown=True)
    with patch.object(service, "canvas_client", side_effect=lambda a: clients[a["id"]]), \
         patch.object(service, "_poll_canvas") as poll:
        service._run("task")
    result = store.task("task")
    assert result["account_id"] == first["id"]
    assert result["error_code"] == "UPSTREAM_SUBMIT_OUTCOME_UNKNOWN"
    assert result["attempted_account_ids"] == [first["id"]]
    assert store.active_account_count(first["id"]) == 1
    poll.assert_called_once()
    clients[first["id"]].submit.assert_called_once()
    clients[second["id"]].submit.assert_not_called()


def test_resource_precharge_is_not_recorded_as_actual_when_balance_overlaps(context):
    store, service, _ = context
    account = owner(store)
    task(store)
    client = fake_client(store, service, account)
    with patch.object(service, "canvas_client", return_value=client), patch.object(store, "has_account_overlap", return_value=True):
        service._run("task")
    result = store.task("task")
    assert result["status"] == "succeeded"
    assert result["actual_cost"] is None
    assert result["estimated_cost"] == 10


@pytest.mark.parametrize("enabled,authorized", [(False, True), (True, False)])
def test_cli_disabled_or_unauthorized_fails_without_generation_or_browser(context, enabled, authorized):
    store, service, _ = context
    owner(store, authorized=authorized)
    store.update_settings({"canvas_cli_enabled": enabled})
    task(store)
    with patch.object(service, "canvas_client", Mock()) as client:
        service._run("task")
    assert store.task("task")["status"] == "failed"
    assert store.task("task")["error_code"] == "ACCOUNT_POOL_UNAVAILABLE"
    client.assert_not_called()
    service.browser_runtime.acquire.assert_not_called()


def test_old_bound_browser_task_does_not_select_cli_account(context):
    store, service, _ = context
    old = owner(store, "web", balance=100, mode="browser")
    owner(store, "cli", balance=1000, priority=20)
    task(store, backend="browser")
    reserved = service._reserve_account(10, task_id="task")
    assert reserved["id"] == old["id"]
    service._release_account(old["id"], "task")


def test_canvas_restart_uses_retained_original_operation_without_preparing_or_submitting(context):
    store, service, _ = context
    account = owner(store)
    task(store, backend="canvas_cli")
    original = {"task_id": "task", "account_id": account["id"], "project_id": "retained-project",
                "node_id": "retained-node", "submit_id": "retained-submit", "submission_phase": "submitted",
                "expected_user_id": account["login_user_id"]}
    store.update_task("task", {"status": "running", "account_id": account["id"],
                               "upstream_context": original, "estimated_cost": 10})
    client = fake_client(store, service, account)
    with patch.object(service, "canvas_client", return_value=client):
        service._resume_canvas_poll("task")
    assert store.task("task")["status"] == "succeeded"
    client.prepare.assert_not_called()
    client.submit.assert_not_called()
    assert client.poll.call_args.args[0] == original
    service.browser_runtime.acquire.assert_not_called()
