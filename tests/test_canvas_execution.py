"""CLI execution preserves paid-operation identity and account credit safety."""
from copy import deepcopy
import json
import time
from types import SimpleNamespace
from unittest.mock import Mock, patch
import uuid

import pytest

from app.canvas_cli import CanvasCLIError
from app.catalog import normalize_request
from app.service import Service
from app.store import Store
from app.result_transfer import ResultTransferError


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


def fake_client(store, service, account, *, quote=10, actual=None, kind="video"):
    client = Mock(account=account)
    client.preflight_model.return_value = {'model': 'synthetic-supported-model'}
    client.identity.side_effect = lambda **kwargs: {"user_id": account["login_user_id"],
        "available_balance": account["balance"] - (actual if actual is not None else quote) if client.submit.call_count else account["balance"]}

    def prepare(normalized, task_id, assets, *, persist, deadline=None):
        context = {"backend": "canvas_cli", "account_id": account["id"], "task_id": task_id,
            "profile": f"synthetic-{account['id']}", "project_id": f"project-{account['id']}",
            "node_id": f"node-{task_id}-{account['id']}", "submit_id": f"submit-{task_id}-{account['id']}",
            "kind": kind, "phase": "quoted", "quoted_credits": quote}
        persist(deepcopy(context))
        return context

    client.prepare.side_effect = prepare

    def submit(context, ceiling, *, persist, deadline):
        durable = store.task(context["task_id"])
        assert durable["execution_backend"] == "canvas_cli"
        assert durable["upstream_context"]["submit_id"] == context["submit_id"]
        assert durable["upstream_context"]["submission_phase"] == "confirming"
        assert durable["estimated_cost"] == quote
        assert durable["actual_cost"] is None
        assert ceiling >= quote
        assert deadline > time.monotonic()
        persist({'submission_phase': 'run_dispatching'})
        return {**context, "phase": "accepted", "resource_ids": ["accepted-resource"]}

    client.submit.side_effect = submit
    filename = "original.png" if kind == "image" else "original.mp4"
    mime = "image/png" if kind == "image" else "video/mp4"
    path = service.canvas_cli.account_directory(account) / "results" / filename
    path.parent.mkdir(parents=True)
    path.write_bytes(b"synthetic-original-video-bytes")
    client.poll.return_value = {"status": "succeeded", "progress": 100, "result_urls": [],
        "result_paths": [str(path)],
        "result_files": [{"path": str(path), "mime": mime, "resource_id": str(uuid.uuid4())}],
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
    assert result["upstream_context"]["submission_phase"] == "accepted"
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
    clock.time.return_value = time.time()
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
    assert set(result['upstream_context']) == {
        'preparation_admission_at', 'preparation_attempts', 'preparation_attempt_count'}
    assert result['upstream_context']['preparation_attempts'][0]['reason'] == 'insufficient_credits'
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


def test_cli_image_reselection_retains_quote_and_uses_smallest_funded_account(context):
    store, service, _ = context
    big = owner(store, "big", balance=100)
    small = owner(store, "small", balance=15)
    tiny = owner(store, "tiny", balance=5)
    response = service.create_image({"model": "gpt-image-2.5-flare", "prompt": "Synthetic landscape"})
    clients = {a["id"]: fake_client(store, service, a, quote=10, kind="image") for a in (big, small, tiny)}
    with patch.object(service, "canvas_client", side_effect=lambda a: clients[a["id"]]):
        service._run_image(response["id"])
    result = store.task(response["id"])
    assert result["status"] == "succeeded", result["error_message"]
    assert result["account_id"] == small["id"]
    assert result["estimated_cost"] == 10
    assert result["attempted_account_ids"] == [small["id"]]
    clients[big["id"]].prepare.assert_called_once()
    clients[big["id"]].submit.assert_not_called()
    clients[tiny["id"]].prepare.assert_not_called()
    clients[tiny["id"]].submit.assert_not_called()
    clients[small["id"]].submit.assert_called_once()
    service.browser_runtime.acquire.assert_not_called()


def test_cli_image_unfunded_quote_selects_funded_account_without_paid_retry(context):
    store, service, _ = context
    first = owner(store, "primary", balance=5, priority=5)
    second = owner(store, "fallback", balance=15, priority=4)
    response = service.create_image({"model": "gpt-image-2.5-flare", "prompt": "Synthetic landscape"})
    clients = {a["id"]: fake_client(store, service, a, quote=10, kind="image") for a in (first, second)}
    with patch.object(service, "canvas_client", side_effect=lambda a: clients[a["id"]]):
        service._run_image(response["id"])
    result = store.task(response["id"])
    assert result["status"] == "succeeded", result["error_message"]
    assert result["account_id"] == second["id"]
    assert result["attempted_account_ids"] == [second["id"]]
    clients[first["id"]].submit.assert_not_called()
    clients[second["id"]].submit.assert_called_once()
    assert not service._credit_reservations


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
    # This tests retry identity, not real SQLite/host scheduling latency;
    # deadline behavior is covered separately with the controlled clock.
    store.update_settings({'task_timeout_seconds': 30})
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
    assert {key: client.poll.call_args.args[0][key] for key in original} == original
    assert client.poll.call_args.args[0]['observation_deadline'] == pytest.approx(
        store.task('task')['created_at'] + service.settings()['task_timeout_seconds'])
    service.browser_runtime.acquire.assert_not_called()


@pytest.mark.parametrize('shape', ['exception', 'status'])
def test_three_explicit_absences_fail_without_resubmission_and_keep_financial_audit(context, shape):
    store, service, _ = context
    account = owner(store)
    task(store)
    client = fake_client(store, service, account)
    client.submit.side_effect = CanvasCLIError('cli.invalid_service_response', 'original receipt lost; token=private',
        details={'error': {'class': 'transport_error', 'traceId': 'original-trace', 'accessToken': 'private'}},
        outcome_unknown=True)
    client.poll.side_effect = ([CanvasCLIError('cli.operation_submission_absent', f'lookup {index} absent') for index in range(3)]
        if shape == 'exception' else [{'status': 'unknown', 'error_code': 'cli.operation_submission_absent',
                                     'error_message': f'lookup {index} absent'} for index in range(3)])
    with patch.object(service, 'canvas_client', return_value=client):
        service._run('task')
    result = store.task('task')
    context = result['upstream_context']
    assert result['status'] == 'failed' and result['error_code'] == 'CLI_SUBMISSION_UNCONFIRMED'
    assert context['reconciliation_paused'] is False and context['reconciliation_closed'] is True
    assert context['reconciliation_close_reason'] == 'operation_submission_absent'
    assert context['financial_outcome'] == 'unconfirmed' and result['completed_at'] is not None
    assert context['consecutive_submission_absent'] == 3
    assert context['submit_error']['code'] == 'cli.invalid_service_response'
    assert context['submit_error']['traceId'] == 'original-trace'
    assert 'original receipt lost' in context['submit_error']['message']
    assert 'requires verification' not in result['error_message']
    assert 'private' not in str(context)
    assert context['last_query_error']['code'] == 'cli.operation_submission_absent'
    assert result['account_id'] == account['id'] and result['estimated_cost'] == 10
    assert result['actual_cost'] is None
    assert store.active_account_count(account['id']) == 0
    assert store.scheduling_rows()['inflight'] == []
    assert service._active[account['id']] == 0 and not service._credit_reservations
    client.submit.assert_called_once()
    assert client.poll.call_count == 3


def test_network_errors_do_not_count_as_explicit_absence_or_cause_premature_parking(context):
    store, service, _ = context
    account = owner(store)
    task(store)
    client = fake_client(store, service, account)
    success = client.poll.return_value
    client.poll.side_effect = [CanvasCLIError('CLI_NETWORK_ERROR', 'temporary query error') for _ in range(3)] + [success]
    with patch.object(service, 'canvas_client', return_value=client):
        service._run('task')
    result = store.task('task')
    assert result['status'] == 'succeeded'
    assert result['upstream_context']['reconciliation_paused'] is False
    assert result['upstream_context']['consecutive_submission_absent'] == 0
    assert 'last_query_error' not in result['upstream_context']
    assert client.poll.call_count == 4
    client.submit.assert_called_once()


def test_known_running_clears_obsolete_query_error_but_keeps_first_submit_error(context):
    store, service, _ = context
    account = owner(store)
    task(store)
    client = fake_client(store, service, account)
    success = client.poll.return_value
    client.submit.side_effect = CanvasCLIError('CLI_TIMEOUT', 'original receipt unavailable', outcome_unknown=True)
    client.poll.side_effect = [CanvasCLIError('CLI_NETWORK_ERROR', 'query broke'),
                               {'status': 'running', 'progress': 52}, success]
    inspected = []
    def observe(_seconds):
        current = store.task('task')
        if current['status'] == 'running':
            assert current['error_code'] == current['error_message'] == ''
            assert 'last_query_error' not in current['upstream_context']
            assert current['upstream_context']['submit_error']['code'] == 'CLI_TIMEOUT'
            inspected.append(True)
        return False
    with patch.object(service, 'canvas_client', return_value=client), patch.object(service._stop, 'wait', side_effect=observe):
        service._run('task')
    assert inspected == [True]
    assert store.task('task')['status'] == 'succeeded'


def test_absence_streak_is_reset_by_network_error_and_positive_running_match(context):
    store, service, _ = context
    account = owner(store)
    task(store)
    client = fake_client(store, service, account)
    success = client.poll.return_value
    absent = lambda: CanvasCLIError('cli.operation_submission_absent', 'not resolved')
    client.poll.side_effect = [absent(), CanvasCLIError('CLI_NETWORK_ERROR', 'query broke'), absent(),
        {'status': 'running', 'progress': 50}, absent(), absent(), success]
    with patch.object(service, 'canvas_client', return_value=client):
        service._run('task')
    assert store.task('task')['status'] == 'succeeded'
    assert client.poll.call_count == 7
    client.submit.assert_called_once()


def retained_operation(store, account, *, paused=True, deadline=None):
    task(store, backend='canvas_cli')
    context = {'project_id': 'retained-project', 'node_id': 'retained-node', 'submit_id': 'retained-submit',
        'submission_phase': 'submitting', 'phase': 'quoted', 'expected_user_id': account['login_user_id'],
        'reconciliation_paused': paused, 'submit_error': {'code': 'original-error', 'message': 'original diagnostic'}}
    if deadline is not None:
        context['observation_deadline'] = deadline
    store.update_task('task', {'status': 'submitted', 'account_id': account['id'], 'upstream_context': context,
        'estimated_cost': 10, 'balance_before': 100, 'error_code': 'UPSTREAM_SUBMIT_OUTCOME_UNKNOWN'})
    return store.task('task')


def test_legacy_restart_uses_created_deadline_and_never_grants_a_new_window(context):
    store, service, _ = context
    account = owner(store)
    retained_operation(store, account, paused=False)
    created = time.time() - 1200
    with store._connect() as db:
        db.execute('UPDATE tasks SET created_at=? WHERE id=?', (created, 'task'))
    client = fake_client(store, service, account)
    with patch.object(service, 'canvas_client', return_value=client):
        service._resume_canvas_poll('task')
    result = store.task('task')
    assert result['status'] == 'failed' and result['error_code'] == 'CLI_SUBMISSION_UNCONFIRMED'
    assert result['upstream_context']['reconciliation_closed'] is True
    assert result['upstream_context']['reconciliation_paused'] is False
    assert result['upstream_context']['observation_deadline'] == pytest.approx(created + 5)
    first = result['upstream_context']['observation_deadline']
    store.update_settings({'task_timeout_seconds': 3000})
    with patch.object(service, 'canvas_client', return_value=client):
        service._resume_canvas_poll('task')
    assert store.task('task')['upstream_context']['observation_deadline'] == first
    client.poll.assert_not_called()
    client.submit.assert_not_called()


def test_legacy_one_shot_unmatched_operation_fails_without_extending_deadline_or_resubmission(context):
    store, service, _ = context
    account = owner(store)
    deadline = time.time() - 1200
    retained_operation(store, account, deadline=deadline)
    client = fake_client(store, service, account)
    client.poll.side_effect = CanvasCLIError('cli.operation_submission_absent', 'still absent')
    with patch.object(service, 'canvas_client', return_value=client):
        result = service.reconcile_canvas_task('task')
    assert result['status'] == 'failed' and result['error_code'] == 'CLI_SUBMISSION_UNCONFIRMED'
    assert result['upstream_context']['reconciliation_closed'] is True
    assert result['upstream_context']['observation_deadline'] == deadline
    assert result['upstream_context']['submit_id'] == 'retained-submit'
    assert result['upstream_context']['submit_error']['message'] == 'original diagnostic'
    assert store.active_account_count(account['id']) == 0
    assert service._worker_slots._value == 50 and service._active[account['id']] == 0
    client.poll.assert_called_once()
    client.prepare.assert_not_called()
    client.submit.assert_not_called()


def test_manual_reconciliation_can_publish_positive_original_result_without_generation(context):
    store, service, stored = context
    account = owner(store)
    deadline = time.time() - 1200
    retained_operation(store, account, deadline=deadline)
    client = fake_client(store, service, account)
    with patch.object(service, 'canvas_client', return_value=client):
        result = service.reconcile_canvas_task('task')
    assert result['status'] == 'succeeded'
    assert result['upstream_context']['observation_deadline'] == deadline
    assert result['result_urls'][0].startswith('https://results.example.test/')
    assert result['error_code'] == '' and store.active_account_count(account['id']) == 0
    client.poll.assert_called_once()
    client.submit.assert_not_called()
    stored.assert_called_once()


def test_manual_reconciliation_rejects_busy_account_before_any_query(context):
    store, service, _ = context
    account = owner(store)
    retained_operation(store, account)
    service._active[account['id']] = 1
    before = service._worker_slots._value
    with patch.object(service, 'canvas_client') as client, pytest.raises(RuntimeError, match='busy'):
        service.reconcile_canvas_task('task')
    client.assert_not_called()
    assert service._worker_slots._value == before


def test_positive_confirmation_failure_releases_credit_and_keeps_typed_original_error(context):
    store, service, _ = context
    account = owner(store)
    task(store)
    client = fake_client(store, service, account)
    client.submit.side_effect = CanvasCLIError('CLI_CONFIRMATION_INVALID', 'no confirmation token',
        details={'dispatch_started': False, 'error': {'class': 'invalid_response'}}, outcome_unknown=False)
    with patch.object(service, 'canvas_client', return_value=client):
        service._run('task')
    result = store.task('task')
    assert result['status'] == 'failed' and result['error_code'] == 'CLI_CONFIRMATION_INVALID'
    assert result['upstream_context']['submit_error']['dispatch_started'] is False
    assert store.active_account_count(account['id']) == 0
    assert store.scheduling_rows()['inflight'] == []
    client.poll.assert_not_called()


def test_query_diagnostics_only_keep_whitelisted_scalars(context):
    _, service, _ = context
    error = CanvasCLIError('original.code', 'safe failure', exit_code=21, details={'error': {
        'class': 'transport_error', 'reason': 'network_error', 'serviceCode': '55', 'traceId': 'trace',
        'authorization': 'private', 'deviceCode': 'private', 'untrusted': 'drop', 'debug': {'token': 'private'}}})
    saved = service._canvas_diagnostic(error, 'query')
    assert saved['traceId'] == 'trace' and saved['exit_code'] == 21
    assert saved['phase'] == 'query'
    assert 'private' not in str(saved) and 'untrusted' not in saved and 'debug' not in saved


def test_explicit_recovery_ceiling_is_checked_before_confirm_or_paid_run(context):
    store, service, _ = context
    account = owner(store)
    row = task(store)
    normalized = {**row['normalized'], '_canvas_credit_ceiling': 9, '_canvas_attempt_id': 'new-attempt'}
    with store._connect() as db:
        db.execute('UPDATE tasks SET normalized_json=? WHERE id=?', (json.dumps(normalized), 'task'))
    client = fake_client(store, service, account, quote=10)
    with patch.object(service, 'canvas_client', return_value=client):
        service._run('task')
    assert store.task('task')['error_code'] == 'CLI_CREDIT_CEILING_EXCEEDED'
    assert store.task('task')['status'] == 'failed'
    client.submit.assert_not_called()
    client.poll.assert_not_called()
    assert store.scheduling_rows()['inflight'] == []


def test_new_explicit_attempt_preserves_recovery_history_without_reusing_old_active_ids(context):
    store, service, _ = context
    account = owner(store)
    task(store)
    archive = [{'submit_id': 'old-submit', 'account_id': account['id'], 'outcome': 'unresolved'}]
    started = time.time()
    resolution = {'type': 'duplicate_charge_risk_acknowledged', 'risk_acknowledged': True,
                  'attempt_started_at': started}
    store.update_task('task', {'upstream_context': {'previous_submissions': archive,
        'recovery_resolution': resolution, 'submit_id': 'must-not-reuse', 'reconciliation_paused': True,
        'observation_deadline': 1, 'last_query_error': {'code': 'old-query'}}})
    client = fake_client(store, service, account)
    with patch.object(service, 'canvas_client', return_value=client):
        service._run('task')
    result = store.task('task')
    assert result['status'] == 'succeeded', result['error_message']
    saved = result['upstream_context']
    assert saved['previous_submissions'] == archive and saved['recovery_resolution'] == resolution
    assert saved['submit_id'] != 'must-not-reuse'
    assert saved['observation_deadline'] == pytest.approx(started + 5)
    assert 'last_query_error' not in saved
    client.submit.assert_called_once()


def test_acknowledged_new_attempt_already_out_of_time_never_prepares_or_runs(context):
    store, service, _ = context
    account = owner(store)
    task(store)
    store.update_task('task', {'upstream_context': {'recovery_resolution': {
        'type': 'duplicate_charge_risk_acknowledged', 'risk_acknowledged': True,
        'attempt_started_at': time.time() - 1200}}})
    client = fake_client(store, service, account)
    with patch.object(service, 'canvas_client', return_value=client):
        service._run('task')
    assert store.task('task')['error_code'] == 'CLI_PREPARATION_TIMEOUT'
    client.identity.assert_not_called()
    client.prepare.assert_not_called()
    client.submit.assert_not_called()


@pytest.mark.parametrize('state', ['running', 'unknown'])
def test_bounded_observation_timeout_fails_without_resubmission_and_retains_unknown_finance(context, monkeypatch, state):
    store, service, _ = context
    account = owner(store)
    wall = {'now': time.time()}
    deadline = wall['now'] + 5
    retained_operation(store, account, paused=False, deadline=deadline)
    original = store.task('task')['upstream_context']
    client = Mock(account=account)
    client.poll.return_value = {'status': state, 'result_urls': [], 'result_paths': [], 'progress': 55}
    # The task ends after the bounded window; its financial uncertainty stays
    # explicit instead of claiming no charge or starting another generation.
    monkeypatch.setattr('app.canvas_service.time', SimpleNamespace(
        time=lambda: wall['now'], monotonic=lambda: 1))
    service._stop.wait.side_effect = lambda _: wall.update(now=deadline + 1)
    service._poll_canvas('task', client, 100)
    result = store.task('task')
    saved = result['upstream_context']
    assert result['status'] == 'failed' and result['error_code'] == 'CLI_SUBMISSION_UNCONFIRMED'
    assert result['completed_at'] is not None and result['actual_cost'] is None
    assert saved['reconciliation_closed'] is True and saved['reconciliation_paused'] is False
    assert saved['reconciliation_close_reason'] == 'observation_deadline_reached'
    assert saved['financial_outcome'] == 'unconfirmed'
    assert saved['observation_deadline'] == deadline
    assert saved['submit_id'] == original['submit_id']
    assert store.active_account_count(account['id']) == 0
    assert store.scheduling_rows()['inflight'] == []
    client.poll.assert_called_once()
    client.submit.assert_not_called()
    client.prepare.assert_not_called()


def test_manual_completed_without_final_output_remains_pending_and_keeps_hold(context):
    store, service, _ = context
    account = owner(store)
    retained_operation(store, account, deadline=time.time() - 1200)
    client = Mock(account=account)
    client.poll.return_value = {'status': 'succeeded', 'result_urls': [], 'result_paths': []}
    with patch.object(service, 'canvas_client', return_value=client):
        result = service.reconcile_canvas_task('task')
    assert result['status'] == 'submitted' and result['completed_at'] is None
    assert result['error_code'] == 'UPSTREAM_SUBMIT_OUTCOME_UNKNOWN'
    assert result['upstream_context']['reconciliation_pause_reason'] == 'result_pending'
    assert result['upstream_context']['submission_phase'] == 'accepted'
    assert 'generation completed' in result['error_message']
    assert store.active_account_count(account['id']) == 1
    client.poll.assert_called_once()
    client.submit.assert_not_called()


def test_confirmed_completion_without_result_parks_without_repeating_generation(context):
    store, service, _ = context
    account = owner(store)
    retained_operation(store, account, paused=False, deadline=time.time() + 60)
    client = Mock(account=account)
    client.poll.return_value = {'status': 'succeeded', 'result_urls': [], 'result_paths': []}
    service._poll_canvas('task', client, time.monotonic() + 60)
    result = store.task('task')
    assert result['status'] == 'submitted' and result['completed_at'] is None
    assert result['upstream_context']['reconciliation_pause_reason'] == 'result_pending'
    assert result['upstream_context']['submission_phase'] == 'accepted'
    assert store.active_account_count(account['id']) == 1
    client.poll.assert_called_once()
    client.submit.assert_not_called()


def test_completed_generation_with_repeated_storage_errors_parks_original_result(context):
    store, service, stored = context
    account = owner(store)
    retained_operation(store, account, paused=False, deadline=time.time() + 120)
    client = fake_client(store, service, account)
    stored.side_effect = ResultTransferError('synthetic S3 unavailable')
    service._poll_canvas('task', client, time.monotonic() + 120)
    result = store.task('task')
    assert result['status'] == 'submitted' and result['error_code'] == 'UPSTREAM_SUBMIT_OUTCOME_UNKNOWN'
    assert result['completed_at'] is None and result['upstream_context']['reconciliation_paused']
    assert result['upstream_context']['reconciliation_pause_reason'] == 'result_transfer_failed'
    assert result['upstream_context']['submission_phase'] == 'accepted'
    assert result['upstream_context']['resource_ids']
    assert 'generation completed' in result['error_message']
    assert 'unavailable' in result['error_message']
    assert store.active_account_count(account['id']) == 1
    assert stored.call_count == client.poll.call_count == 8
    client.submit.assert_not_called()


def test_manual_result_storage_failure_keeps_confirmed_operation_and_credit_hold(context):
    store, service, stored = context
    account = owner(store)
    retained_operation(store, account, deadline=time.time() - 1200)
    client = fake_client(store, service, account)
    stored.side_effect = ResultTransferError('synthetic S3 unavailable')
    with patch.object(service, 'canvas_client', return_value=client):
        result = service.reconcile_canvas_task('task')
    assert result['status'] == 'submitted' and result['completed_at'] is None
    assert result['upstream_context']['reconciliation_pause_reason'] == 'result_transfer_failed'
    assert result['upstream_context']['submission_phase'] == 'accepted'
    assert store.active_account_count(account['id']) == 1
    client.poll.assert_called_once()
    stored.assert_called_once()
    client.submit.assert_not_called()


@pytest.mark.parametrize('transition', ['funding', 'reselect'])
def test_pre_run_account_change_keeps_acknowledged_history_and_attempt_deadline(context, transition):
    store, service, _ = context
    first = owner(store, 'first', balance=5 if transition == 'funding' else 100,
                  priority=5 if transition == 'funding' else 0)
    second = owner(store, 'second', balance=15, priority=4 if transition == 'funding' else 0)
    task(store)
    archive = [{'submit_id': 'old-submit', 'outcome': 'unresolved'}]
    resolution = {'type': 'duplicate_charge_risk_acknowledged', 'risk_acknowledged': True,
                  'attempt_started_at': time.time()}
    store.update_task('task', {'upstream_context': {'previous_submissions': archive,
                                                 'recovery_resolution': resolution}})
    clients = {a['id']: fake_client(store, service, a, quote=10) for a in (first, second)}
    original_prepare = clients[second['id']].prepare.side_effect
    def verify_history(*args, **kwargs):
        preserved = store.task('task')['upstream_context']
        assert preserved['previous_submissions'] == archive
        assert preserved['recovery_resolution'] == resolution
        assert 'submit_id' not in preserved, 'a new account must not inherit the preceding active binding'
        return original_prepare(*args, **kwargs)
    clients[second['id']].prepare.side_effect = verify_history
    with patch.object(service, 'canvas_client', side_effect=lambda a: clients[a['id']]):
        service._run('task')
    result = store.task('task')
    assert result['status'] == 'succeeded', result['error_message']
    assert result['account_id'] == second['id']
    assert result['upstream_context']['previous_submissions'] == archive
    assert result['upstream_context']['recovery_resolution'] == resolution
    assert result['upstream_context']['observation_deadline'] == pytest.approx(resolution['attempt_started_at'] + 5)
    clients[first['id']].submit.assert_not_called()
    clients[second['id']].submit.assert_called_once()


def manually_stop(store, identifier='task'):
    current = store.task(identifier)
    return store.update_task(identifier, {'status': 'failed', 'progress': 100,
        'error_code': 'OPERATOR_CONTENT_RESTRICTED', 'error_message': 'operator stopped this task',
        'completed_at': time.time(), 'upstream_context': {**current['upstream_context'],
            'manual_termination': {'reason': 'content restricted', 'at': time.time()}}})


@pytest.mark.parametrize('state', ['running', 'succeeded', 'failed', 'unknown', 'query_error'])
def test_manual_stop_during_query_is_never_overwritten_by_late_provider_response(context, state):
    store, service, stored = context
    account = owner(store)
    retained_operation(store, account, paused=False, deadline=time.time() + 60)
    client = fake_client(store, service, account)
    output = client.poll.return_value
    stopped = []
    def late_response(_context, *, deadline=None):
        stopped.append(manually_stop(store))
        if state == 'query_error':
            raise CanvasCLIError('CLI_NETWORK_ERROR', 'late query failed')
        if state == 'succeeded':
            return output
        return {'status': state, 'progress': 50, 'error_code': 'UPSTREAM_FAILED', 'error_message': 'late error'}
    client.poll.side_effect = late_response
    service._poll_canvas('task', client, time.monotonic() + 60)
    assert store.task('task') == stopped[0]
    stored.assert_not_called()
    client.submit.assert_not_called()
    assert store.active_account_count(account['id']) == 0


def test_manual_stop_during_sleep_survives_observation_deadline_exit(context, monkeypatch):
    store, service, _ = context
    account = owner(store)
    wall = {'now': time.time()}
    deadline = wall['now'] + 5
    retained_operation(store, account, paused=False, deadline=deadline)
    client = Mock(account=account)
    client.poll.return_value = {'status': 'running', 'progress': 50}
    stopped = []
    monkeypatch.setattr('app.canvas_service.time', SimpleNamespace(time=lambda: wall['now'], monotonic=lambda: 1))
    def stop_while_sleeping(_seconds):
        stopped.append(manually_stop(store))
        wall['now'] = deadline + 1
    service._stop.wait.side_effect = stop_while_sleeping
    service._poll_canvas('task', client, 100)
    assert store.task('task') == stopped[0]
    client.poll.assert_called_once()
    client.submit.assert_not_called()


def test_manual_stop_between_close_read_and_atomic_update_keeps_entire_terminal_row(context, monkeypatch):
    store, service, _ = context
    account = owner(store)
    retained_operation(store, account, paused=False, deadline=time.time() - 1)
    actual_update = store.update_task
    stopped = []
    def race(identifier, changes, **kwargs):
        if kwargs.get('preserve_terminal') and 'upstream_context' in changes and not stopped:
            stopped.append('in-progress')
            stopped[0] = manually_stop(store, identifier)
        return actual_update(identifier, changes, **kwargs)
    monkeypatch.setattr(store, 'update_task', race)
    result = service._close_unconfirmed_canvas_task('task', 'observation_deadline_reached')
    assert result == store.task('task') == stopped[0]
    assert result['error_code'] == 'OPERATOR_CONTENT_RESTRICTED'
    assert 'reconciliation_pause_reason' not in result['upstream_context']


def test_manual_stop_before_paid_boundary_persist_aborts_dispatch_with_typed_error(context):
    store, service, _ = context
    account = owner(store)
    task(store)
    client = fake_client(store, service, account)
    paid_run = Mock()
    stopped = []
    errors = []
    def confirm_then_stop(current, ceiling, *, persist, deadline):
        stopped.append(manually_stop(store))
        try:
            persist({**current, 'submission_phase': 'run_dispatching'})
        except CanvasCLIError as exc:
            errors.append(exc.code)
            raise
        paid_run()
    client.submit.side_effect = confirm_then_stop
    with patch.object(service, 'canvas_client', return_value=client):
        service._run('task')
    assert store.task('task') == stopped[0]
    assert errors == ['CLI_TASK_TERMINATED']
    paid_run.assert_not_called()
    client.poll.assert_not_called()
    assert not service._credit_reservations


@pytest.mark.parametrize('status', ['failed', 'expired'])
def test_unknown_terminal_record_can_still_query_its_original_operation(context, status):
    store, service, _ = context
    account = owner(store)
    retained_operation(store, account, paused=False, deadline=time.time() + 60)
    store.update_task('task', {'status': status, 'completed_at': time.time(),
                               'error_code': 'UPSTREAM_SUBMIT_OUTCOME_UNKNOWN'})
    client = Mock(account=account)
    client.poll.return_value = {'status': 'running', 'progress': 51}
    with patch.object(service, 'canvas_client', return_value=client):
        result = service.reconcile_canvas_task('task')
    assert result['status'] == 'running' and result['error_code'] == ''
    assert result['completed_at'] is None
    assert result['upstream_context']['submit_id'] == 'retained-submit'
    client.poll.assert_called_once()
    client.prepare.assert_not_called()
    client.submit.assert_not_called()


def test_old_paused_unconfirmed_operation_resumes_bounded_reads_and_ends_without_paid_retry(context):
    store, service, _ = context
    account = owner(store)
    retained_operation(store, account, paused=True, deadline=time.time() + 60)
    store.update_task('task', {'upstream_context': {**store.task('task')['upstream_context'],
        'reconciliation_pause_reason': 'operation_submission_absent'}})
    client = fake_client(store, service, account)
    client.poll.side_effect = [CanvasCLIError('cli.operation_submission_absent', 'not found') for _ in range(3)]
    with patch.object(service, 'canvas_client', return_value=client):
        service._resume_canvas_poll('task')
    result = store.task('task')
    assert result['status'] == 'failed' and result['error_code'] == 'CLI_SUBMISSION_UNCONFIRMED'
    assert result['upstream_context']['reconciliation_closed'] is True
    assert result['upstream_context']['financial_outcome'] == 'unconfirmed'
    assert result['upstream_context']['submit_id'] == 'retained-submit'
    assert client.poll.call_count == 3
    client.prepare.assert_not_called()
    client.submit.assert_not_called()
    assert store.active_account_count(account['id']) == 0
    assert service._active[account['id']] == 0


def test_automatically_closed_unconfirmed_task_cannot_be_revived_by_late_success_or_reconcile(context):
    store, service, _ = context
    account = owner(store)
    retained_operation(store, account, paused=False, deadline=time.time() + 60)
    closed = service._close_unconfirmed_canvas_task('task', 'operation_submission_absent')
    client = fake_client(store, service, account)
    service._poll_canvas('task', client, time.monotonic() + 60, one_shot=True)
    with patch.object(service, 'canvas_client', return_value=client):
        service._resume_canvas_poll('task')
        with pytest.raises(ValueError, match='no longer requires'):
            service.reconcile_canvas_task('task')
    assert service._close_unconfirmed_canvas_task('task', 'observation_deadline_reached') == closed
    assert store.task('task') == closed
    client.poll.assert_not_called()
    client.prepare.assert_not_called()
    client.submit.assert_not_called()


def test_explicit_confirmed_upstream_failure_still_completes_as_failure(context):
    store, service, _ = context
    account = owner(store)
    retained_operation(store, account, deadline=time.time() - 1200)
    client = fake_client(store, service, account)
    client.poll.return_value = {'status': 'failed', 'error_code': 'CONTENT_MODERATION_FAILED',
                                'error_message': 'explicit terminal provider rejection'}
    with patch.object(service, 'canvas_client', return_value=client):
        result = service.reconcile_canvas_task('task')
    assert result['status'] == 'failed' and result['completed_at'] is not None
    assert result['error_code'] == 'CONTENT_MODERATION_FAILED'
    assert not result['upstream_context']['reconciliation_paused']
    assert store.active_account_count(account['id']) == 0
    client.poll.assert_called_once()
    client.submit.assert_not_called()


def test_full_reference_preparation_stops_at_shared_deadline_and_releases_worker(context, tmp_path):
    """Fifteen individually legal uploads cannot each renew a 30-minute task."""
    from app.canvas_client import CanvasClient
    from test_canvas_transport import FakeRunner

    store, service, _ = context
    store.update_settings({'task_timeout_seconds': 1800})
    account = owner(store)
    store.update_account(account['id'], {'login_user_id': '42', 'cli_user_id': '42'})
    account = store.account(account['id'], private=True)
    task(store)
    clock = {'now': 100.0}
    fake_time = SimpleNamespace(monotonic=lambda: clock['now'],
        time=lambda: 1700000000 + clock['now'],
        sleep=lambda delay: clock.update(now=clock['now'] + delay))
    input_file = tmp_path / 'synthetic-input.bin'
    input_file.write_bytes(b'fixture only; never sent to a network')
    assets = {group: [{'path': str(input_file)} for _ in range(count)]
              for group, count in [('images', 9), ('videos', 3), ('audios', 3)]}

    class TimedRunner(FakeRunner):
        def __init__(self):
            super().__init__(tmp_path)
            self.upload_budgets = []
            self.persisted.append({'fixture': True})

        def run(self, account, args, timeout=None, deadline=None):
            if args[:2] == ['resource', 'upload']:
                self.upload_budgets.append(timeout)
                assert service._active[account['id']] == 1
                clock['now'] += min(179, timeout)
                if timeout < 179:
                    raise CanvasCLIError('CLI_COMMAND_TIMEOUT', 'synthetic upload reached its bounded timeout')
            return super().run(account, args, timeout=timeout)

    runner = TimedRunner()
    client = CanvasClient(runner, account)
    free_slots = service._worker_slots._value
    with patch.object(service, 'canvas_client', return_value=client), \
            patch.object(service, '_canvas_local_assets', return_value=assets), \
            patch('app.canvas_client.time', fake_time), patch('app.canvas_service.time', fake_time), \
            patch('app.service.time', fake_time):
        service._run('task')
    result = store.task('task')
    assert clock['now'] == 1900
    assert runner.upload_budgets == [180] * 10 + [10]
    assert not any(args[:2] in (['node', 'confirm'], ['node', 'run'], ['node', 'quote']) for args in runner.calls)
    assert result['status'] == 'failed' and result['error_code'] == 'CLI_PREPARATION_TIMEOUT'
    assert result['actual_cost'] is None and not result['attempted_account_ids']
    assert sum(service._active.values()) == 0 and not service._credit_reservations
    assert service._worker_slots._value == free_slots


def test_poll_budget_uses_original_wall_deadline_and_still_accepts_running(context):
    store, service, _ = context
    account = owner(store)
    retained_operation(store, account, paused=False, deadline=time.time() + 5)
    client = fake_client(store, service, account)
    client.poll.return_value = {'status': 'running', 'progress': 60}
    # Stop after the positive response to inspect the passed budget without
    # adding a synthetic second query or extending the original window.
    service._stop.wait.side_effect = lambda delay: service._stop.set()
    before = time.monotonic()
    service._poll_canvas('task', client, before + 1800)
    passed = client.poll.call_args.kwargs['deadline']
    assert before < passed <= before + 5.1
    assert store.task('task')['status'] == 'running'
    assert not store.task('task')['error_code']


def preflight_task(store, service, kind):
    store.update_settings({'task_timeout_seconds': 30})
    if kind == 'image':
        return service.create_image({'model': 'gpt-image-2.5-flare', 'prompt': 'Synthetic landscape'})['id']
    task(store)
    return 'task'


@pytest.mark.parametrize('kind', ['video', 'image'])
@pytest.mark.parametrize('code,details', [
    ('CLI_COMMAND_TIMEOUT', {}),
    ('cli.api_failed', {'error': {'retryable': True, 'reason': 'network_error'}}),
])
def test_identity_network_failure_reselects_once_before_any_paid_dispatch(context, kind, code, details):
    store, service, _ = context
    accounts = [owner(store, 'primary', priority=5), owner(store, 'healthy', priority=4)]
    identifier = preflight_task(store, service, kind)
    clients = {a['id']: fake_client(store, service, a, kind=kind) for a in accounts}
    clients[accounts[0]['id']].identity.side_effect = CanvasCLIError(code,
        'synthetic identity interruption token=private-value', details=details)
    with patch.object(service, 'canvas_client', side_effect=lambda a: clients[a['id']]):
        service._run(identifier)
    result = store.task(identifier)
    assert result['status'] == 'succeeded', result['error_message']
    assert result['account_id'] == accounts[1]['id']
    assert result['attempted_account_ids'] == [a['id'] for a in accounts]
    failure = result['upstream_context']['preflight_failures'][0]
    assert failure['account_id'] == accounts[0]['id'] and failure['code'] == code
    assert failure['phase'] == 'identity' and failure['dispatch_started'] is False
    assert 'private-value' not in json.dumps(failure)
    clients[accounts[0]['id']].prepare.assert_not_called()
    clients[accounts[0]['id']].submit.assert_not_called()
    clients[accounts[1]['id']].submit.assert_called_once()
    assert sum(service._active.values()) == 0 and not service._credit_reservations


@pytest.mark.parametrize('kind', ['video', 'image'])
def test_two_identity_failures_keep_last_real_error_and_never_use_third_account(context, kind):
    store, service, _ = context
    accounts = [owner(store, f'owner-{i}', priority=5-i) for i in range(3)]
    identifier = preflight_task(store, service, kind)
    clients = {a['id']: fake_client(store, service, a, kind=kind) for a in accounts}
    clients[accounts[0]['id']].identity.side_effect = CanvasCLIError('CLI_COMMAND_TIMEOUT', 'first identity timeout')
    clients[accounts[1]['id']].identity.side_effect = CanvasCLIError('cli.api_failed', 'last identity connection failure',
        details={'error': {'retryable': True, 'reason': 'network_error'}})
    with patch.object(service, 'canvas_client', side_effect=lambda a: clients[a['id']]):
        service._run(identifier)
    result = store.task(identifier)
    assert result['status'] == 'failed' and result['error_code'] == 'cli.api_failed'
    assert result['error_message'] == 'last identity connection failure'
    assert result['attempted_account_ids'] == [a['id'] for a in accounts[:2]]
    assert [x['message'] for x in result['upstream_context']['preflight_failures']] == [
        'first identity timeout', 'last identity connection failure']
    clients[accounts[2]['id']].identity.assert_not_called()
    assert all(not client.prepare.called and not client.submit.called for client in clients.values())
    assert result['actual_cost'] is None and not service._credit_reservations


@pytest.mark.parametrize('kind', ['video', 'image'])
def test_identity_failure_and_credit_rejection_share_the_existing_two_account_limit(context, kind):
    store, service, _ = context
    accounts = [owner(store, f'owner-{i}', priority=5-i) for i in range(3)]
    identifier = preflight_task(store, service, kind)
    clients = {a['id']: fake_client(store, service, a, kind=kind) for a in accounts}
    clients[accounts[0]['id']].identity.side_effect = CanvasCLIError('CLI_COMMAND_TIMEOUT', 'identity timeout')
    clients[accounts[1]['id']].submit.side_effect = CanvasCLIError('INSUFFICIENT_CREDITS',
        'not enough credits left', outcome_unknown=False)
    with patch.object(service, 'canvas_client', side_effect=lambda a: clients[a['id']]):
        service._run(identifier)
    result = store.task(identifier)
    assert result['status'] == 'failed' and result['error_code'] == 'INSUFFICIENT_CREDITS'
    assert result['attempted_account_ids'] == [a['id'] for a in accounts[:2]]
    assert result['upstream_context']['preflight_failures'][0]['account_id'] == accounts[0]['id']
    clients[accounts[0]['id']].submit.assert_not_called()
    clients[accounts[1]['id']].submit.assert_called_once()
    clients[accounts[2]['id']].identity.assert_not_called()


@pytest.mark.parametrize('kind', ['video', 'image'])
def test_identity_failover_retains_priority_and_smallest_sufficient_account_selection(context, kind):
    store, service, _ = context
    accounts = [owner(store, 'first', balance=100, priority=9),
                owner(store, 'bigger', balance=30, priority=5),
                owner(store, 'fitting', balance=20, priority=5),
                owner(store, 'lower-priority', balance=15, priority=4)]
    identifier = preflight_task(store, service, kind)
    clients = {a['id']: fake_client(store, service, a, kind=kind, quote=10) for a in accounts}
    clients[accounts[0]['id']].identity.side_effect = CanvasCLIError('CLI_COMMAND_TIMEOUT', 'identity timeout')
    with patch.object(service, 'canvas_client', side_effect=lambda a: clients[a['id']]), \
            patch.object(service, '_cost_estimate', return_value=(10, {'source': 'synthetic-history'})):
        service._run(identifier)
    result = store.task(identifier)
    assert result['status'] == 'succeeded' and result['account_id'] == accounts[2]['id']
    assert result['attempted_account_ids'] == [accounts[0]['id'], accounts[2]['id']]
    assert result['upstream_context']['preflight_failures'][0]['account_id'] == accounts[0]['id']
    assert sum(client.submit.call_count for client in clients.values()) == 1
    clients[accounts[0]['id']].identity.assert_called_once()
    clients[accounts[3]['id']].identity.assert_not_called()


@pytest.mark.parametrize('stage', ['auth', 'mismatch', 'missing_balance', 'prepare', 'unknown', 'provider'])
def test_identity_failover_never_retries_non_network_or_later_stage_failures(context, stage):
    store, service, _ = context
    first, backup = owner(store, 'first', priority=5), owner(store, 'backup', priority=4)
    identifier = preflight_task(store, service, 'video')
    clients = {a['id']: fake_client(store, service, a) for a in [first, backup]}
    client = clients[first['id']]
    if stage == 'auth':
        client.identity.side_effect = CanvasCLIError('CLI_AUTH_EXPIRED', 'authorization expired')
    elif stage == 'mismatch':
        client.identity.side_effect = None
        client.identity.return_value = {'user_id': 'other-user', 'available_balance': 100}
    elif stage == 'missing_balance':
        client.identity.side_effect = None
        client.identity.return_value = {'user_id': first['login_user_id'], 'available_balance': None}
    elif stage == 'prepare':
        client.prepare.side_effect = CanvasCLIError('CLI_COMMAND_TIMEOUT', 'upload timeout')
    elif stage == 'unknown':
        client.submit.side_effect = CanvasCLIError('CLI_COMMAND_TIMEOUT', 'lost paid receipt', outcome_unknown=True)
    else:
        client.poll.return_value = {'status': 'failed', 'error_code': '20114', 'error_message': 'provider failed'}
    with patch.object(service, 'canvas_client', side_effect=lambda a: clients[a['id']]):
        service._run(identifier)
    result = store.task(identifier)
    assert result['account_id'] == first['id']
    assert not result['upstream_context'].get('preflight_failures')
    clients[backup['id']].identity.assert_not_called()
    clients[backup['id']].submit.assert_not_called()


@pytest.mark.parametrize('binding', ['submit_id', 'resource_ids', 'resources', 'submission_phase', 'run_id', 'thread_id'])
def test_identity_timeout_does_not_reselect_any_existing_active_operation(context, binding):
    store, service, _ = context
    account = owner(store)
    task(store)
    values = {'account_id': account['id'], 'status': 'preparing', 'upstream_context': {}}
    if binding in {'run_id', 'thread_id'}:
        values[binding] = 'original-accepted-id'
    else:
        values['upstream_context'][binding] = 'original-accepted-id'
    store.update_task('task', values)
    client = fake_client(store, service, account)
    client.identity.side_effect = CanvasCLIError('CLI_COMMAND_TIMEOUT', 'read timeout')
    with patch.object(service, 'canvas_client', return_value=client):
        assert service._execute_canvas('task', account, time.monotonic() + 30) is True
    result = store.task('task')
    assert (result[binding] if binding in {'run_id', 'thread_id'} else result['upstream_context'][binding]) == 'original-accepted-id'
    assert not result['upstream_context'].get('preflight_failures')
    assert not result['attempted_account_ids']


@pytest.mark.parametrize('kind', ['video', 'image'])
def test_manual_stop_during_identity_timeout_blocks_safe_account_reselection(context, kind):
    store, service, _ = context
    accounts = [owner(store, 'first', priority=5), owner(store, 'backup', priority=4)]
    identifier = preflight_task(store, service, kind)
    clients = {a['id']: fake_client(store, service, a, kind=kind) for a in accounts}
    def stopped(**kwargs):
        store.update_task(identifier, {'status': 'failed', 'error_code': 'OPERATOR_CONTENT_RESTRICTED',
            'error_message': 'operator stopped task', 'upstream_context': {'manual_termination': {'reason': 'operator'}}})
        raise CanvasCLIError('CLI_COMMAND_TIMEOUT', 'late identity timeout')
    clients[accounts[0]['id']].identity.side_effect = stopped
    with patch.object(service, 'canvas_client', side_effect=lambda a: clients[a['id']]):
        service._run(identifier)
    result = store.task(identifier)
    assert result['error_code'] == 'OPERATOR_CONTENT_RESTRICTED' and result['error_message'] == 'operator stopped task'
    assert not result['upstream_context'].get('preflight_failures')
    clients[accounts[1]['id']].identity.assert_not_called()


@pytest.mark.parametrize('kind', ['video', 'image'])
def test_identity_failure_without_healthy_backup_keeps_real_error(context, kind):
    store, service, _ = context
    account = owner(store)
    identifier = preflight_task(store, service, kind)
    client = fake_client(store, service, account, kind=kind)
    client.identity.side_effect = CanvasCLIError('CLI_COMMAND_TIMEOUT', 'original identity timeout')
    with patch.object(service, 'canvas_client', return_value=client):
        service._run(identifier)
    result = store.task(identifier)
    assert result['status'] == 'failed' and result['error_code'] == 'CLI_COMMAND_TIMEOUT'
    assert result['error_message'] == 'original identity timeout'
    assert result['upstream_context']['preflight_failures'][0]['account_id'] == account['id']
    client.prepare.assert_not_called()
    client.submit.assert_not_called()


@pytest.mark.parametrize('kind', ['video', 'image'])
def test_identity_reselection_keeps_original_deadline_in_memory_and_durable_context(context, kind):
    store, service, _ = context
    accounts = [owner(store, 'first', priority=5), owner(store, 'backup', priority=4)]
    identifier = preflight_task(store, service, kind)
    store.update_settings({'task_timeout_seconds': 5})
    clients = {a['id']: fake_client(store, service, a, kind=kind) for a in accounts}
    initial_mono, initial_wall = time.monotonic(), time.time()
    clock = {'elapsed': 0.0}
    fake_time = SimpleNamespace(monotonic=lambda: initial_mono + clock['elapsed'],
        time=lambda: initial_wall + clock['elapsed'], sleep=lambda delay: clock.update(elapsed=clock['elapsed'] + delay))
    deadlines = []
    def first_identity(*, deadline):
        deadlines.append(deadline)
        clock['elapsed'] += 4
        raise CanvasCLIError('CLI_COMMAND_TIMEOUT', 'first identity timed out')
    clients[accounts[0]['id']].identity.side_effect = first_identity
    second = clients[accounts[1]['id']]
    original_identity = second.identity.side_effect
    def second_identity(*, deadline):
        deadlines.append(deadline)
        return original_identity()
    second.identity.side_effect = second_identity
    def paid(context, ceiling, *, persist, deadline):
        deadlines.append(deadline)
        assert deadline - fake_time.monotonic() == pytest.approx(1)
        persist({'submission_phase': 'run_dispatching'})
        return {**context, 'phase': 'accepted', 'resource_ids': ['original-resource']}
    second.submit.side_effect = paid
    with patch.object(service, 'canvas_client', side_effect=lambda a: clients[a['id']]), \
            patch('app.service.time', fake_time), patch('app.canvas_service.time', fake_time):
        service._run(identifier)
    result = store.task(identifier)
    assert result['status'] == 'succeeded', result['error_message']
    assert deadlines and all(value == pytest.approx(initial_mono + 5) for value in deadlines)
    assert result['upstream_context']['preflight_failures'][0]['task_deadline'] == pytest.approx(initial_wall + 5)
    assert result['upstream_context']['observation_deadline'] == pytest.approx(initial_wall + 5)


def test_restart_does_not_renew_budget_after_safe_identity_reselection(context):
    store, service, _ = context
    account = owner(store)
    task(store)
    prior = {'account_id': 99, 'code': 'CLI_COMMAND_TIMEOUT', 'message': 'first timeout',
             'task_deadline': time.time() - 1, 'dispatch_started': False, 'phase': 'identity'}
    store.update_task('task', {'account_id': account['id'], 'status': 'preparing',
        'upstream_context': {'preflight_failures': [prior]}})
    client = fake_client(store, service, account)
    with patch.object(service, 'canvas_client', return_value=client):
        service._execute_canvas('task', account, time.monotonic() + 1800)
    assert store.task('task')['error_code'] == 'CLI_PREPARATION_TIMEOUT'
    assert store.task('task')['upstream_context']['preflight_failures'] == [prior]
    client.identity.assert_not_called()
    client.prepare.assert_not_called()
    client.submit.assert_not_called()


@pytest.mark.parametrize('kind', ['video', 'image'])
def test_queued_identity_retry_without_eligible_accounts_retains_last_error_on_restart(context, kind):
    store, service, _ = context
    first = owner(store)
    identifier = preflight_task(store, service, kind)
    prior = {'account_id': first['id'], 'code': 'CLI_COMMAND_TIMEOUT', 'phase': 'identity',
             'message': 'prior identity timeout', 'task_deadline': time.time() + 30}
    store.update_task(identifier, {'attempted_account_ids': [first['id']], 'error_code': 'CLI_COMMAND_TIMEOUT',
        'error_message': 'prior identity timeout', 'upstream_context': {'preflight_failures': [prior]}})
    with patch.object(service, 'canvas_client') as client:
        service._run(identifier)
    result = store.task(identifier)
    assert result['status'] == 'failed' and result['error_code'] == 'CLI_COMMAND_TIMEOUT'
    assert result['error_message'] == 'prior identity timeout'
    client.assert_not_called()


@pytest.mark.parametrize('kind', ['video', 'image'])
def test_optional_credit_timeout_can_generate_with_cached_budget_without_fabricating_actual_cost(context, kind, monkeypatch):
    from app.canvas_client import CanvasClient
    store, service, _ = context
    account = owner(store, balance=100)
    identifier = preflight_task(store, service, kind)
    client = fake_client(store, service, account, kind=kind, quote=100)
    class IdentityRead:
        def __init__(self):
            self.calls = 0
        def run(self, selected, args, timeout=None, deadline=None):
            assert selected['id'] == account['id'] and args == ['auth', 'account']
            self.calls += 1
            if self.calls == 2:
                raise CanvasCLIError('CLI_COMMAND_TIMEOUT', 'optional credit refresh timeout')
            data = {'userId': account['login_user_id'], 'isVip': True, 'vipLevel': 'basic'}
            if self.calls > 2:
                data['availableCredits'] = '26'
            return {'ok': True, 'data': data}
    runner = IdentityRead()
    client.identity.side_effect = CanvasClient(runner, account).identity
    client.poll.return_value['credit_facts'][0]['amount'] = 10
    original_prepare = client.prepare.side_effect
    def prepare(*args, **kwargs):
        assert store.task(identifier)['balance_before'] is None
        assert store.account(account['id'])['balance'] == 100
        return original_prepare(*args, **kwargs)
    client.prepare.side_effect = prepare
    monkeypatch.setattr('app.canvas_client.time.sleep', lambda _: None)
    with patch.object(service, 'canvas_client', return_value=client):
        service._run(identifier)
    result = store.task(identifier)
    assert result['status'] == 'succeeded', result['error_message']
    assert result['balance_before'] is None and result['balance_after'] == 26
    assert result['actual_cost'] is None and result['estimated_cost'] == 100
    evidence = result['normalized']['_cost_evidence']
    assert evidence['canvas_resource_snapshots'][0]['amount'] == 10
    assert evidence['canvas_resource_snapshots'][0]['source'] == 'precharge'
    assert evidence['canvas_resource_snapshots'][0]['settled'] is False
    assert not evidence.get('balance_isolated')
    assert store.account(account['id'])['balance'] == 26
    assert not result['upstream_context'].get('preflight_failures')
    client.submit.assert_called_once()
    assert client.submit.call_args.args[1] == 100
    assert runner.calls == 3 and not service._credit_reservations


@pytest.mark.parametrize('kind', ['video', 'image'])
def test_missing_fresh_and_cached_balance_fails_before_preparation(context, kind):
    store, service, _ = context
    account = owner(store, balance=None)
    identifier = preflight_task(store, service, kind)
    store.update_task(identifier, {'account_id': account['id'], 'status': 'preparing'})
    client = fake_client(store, service, account, kind=kind)
    client.identity.side_effect = None
    client.identity.return_value = {'user_id': account['login_user_id'], 'available_balance': None}
    with patch.object(service, 'canvas_client', return_value=client):
        service._execute_canvas(identifier, account, time.monotonic() + 30)
    result = store.task(identifier)
    assert result['status'] == 'failed' and result['error_code'] == 'ACCOUNT_VERIFICATION_FAILED'
    assert result['balance_before'] is None and result['actual_cost'] is None
    client.prepare.assert_not_called()
    client.submit.assert_not_called()


@pytest.mark.parametrize('kind', ['video', 'image'])
def test_cached_budget_still_uses_server_quote_and_two_account_credit_rejection_limit(context, kind):
    store, service, _ = context
    accounts = [owner(store, f'owner-{i}', balance=100, priority=5-i) for i in range(3)]
    identifier = preflight_task(store, service, kind)
    clients = {a['id']: fake_client(store, service, a, kind=kind, quote=10) for a in accounts}
    for account in accounts:
        client = clients[account['id']]
        client.identity.side_effect = None
        client.identity.return_value = {'user_id': account['login_user_id'], 'available_balance': None}
        client.submit.side_effect = CanvasCLIError('INSUFFICIENT_CREDITS', 'not enough credits left', outcome_unknown=False)
    with patch.object(service, 'canvas_client', side_effect=lambda a: clients[a['id']]):
        service._run(identifier)
    result = store.task(identifier)
    assert result['status'] == 'failed' and result['error_code'] == 'INSUFFICIENT_CREDITS'
    assert result['attempted_account_ids'] == [a['id'] for a in accounts[:2]]
    assert result['balance_before'] is None and result['actual_cost'] is None
    assert all(clients[a['id']].submit.call_args.args[1] == 10 for a in accounts[:2])
    clients[accounts[2]['id']].identity.assert_not_called()


@pytest.mark.parametrize('kind', ['video', 'image'])
def test_cached_balance_limits_budget_and_quote_reselection_without_becoming_fresh(context, kind):
    store, service, _ = context
    accounts = [owner(store, 'too-small', balance=5, priority=5),
                owner(store, 'funded', balance=15, priority=4)]
    identifier = preflight_task(store, service, kind)
    clients = {a['id']: fake_client(store, service, a, kind=kind, quote=10) for a in accounts}
    for account in accounts:
        clients[account['id']].identity.side_effect = None
        clients[account['id']].identity.return_value = {'user_id': account['login_user_id'], 'available_balance': None}
    with patch.object(service, 'canvas_client', side_effect=lambda a: clients[a['id']]):
        service._run(identifier)
    result = store.task(identifier)
    assert result['status'] == 'succeeded' and result['account_id'] == accounts[1]['id']
    assert result['balance_before'] is None and result['actual_cost'] is None
    clients[accounts[0]['id']].submit.assert_not_called()
    clients[accounts[1]['id']].submit.assert_called_once()


@pytest.mark.parametrize('kind', ['video', 'image'])
def test_cached_balance_preserves_same_priority_smallest_funded_quote_reselection(context, kind):
    store, service, _ = context
    accounts = [owner(store, 'big', balance=100, priority=5),
                owner(store, 'fit', balance=20, priority=5),
                owner(store, 'too-small', balance=5, priority=5),
                owner(store, 'lower', balance=15, priority=4)]
    identifier = preflight_task(store, service, kind)
    clients = {a['id']: fake_client(store, service, a, kind=kind, quote=10) for a in accounts}
    for account in accounts:
        clients[account['id']].identity.side_effect = None
        clients[account['id']].identity.return_value = {'user_id': account['login_user_id'], 'available_balance': None}
    with patch.object(service, 'canvas_client', side_effect=lambda a: clients[a['id']]):
        service._run(identifier)
    result = store.task(identifier)
    assert result['status'] == 'succeeded' and result['account_id'] == accounts[1]['id']
    assert result['balance_before'] is None and result['actual_cost'] is None
    assert result['attempted_account_ids'] == [accounts[1]['id']]
    assert sum(client.submit.call_count for client in clients.values()) == 1
    clients[accounts[2]['id']].identity.assert_not_called()
    clients[accounts[3]['id']].identity.assert_not_called()
