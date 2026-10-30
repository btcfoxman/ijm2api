from __future__ import annotations

import copy
import json
import threading
import time
from datetime import datetime, timezone

import pytest

from app.canvas_cli import CanvasCLI, CanvasCLIError
from app.canvas_client import CanvasClient


TOKEN = "private-confirmation-token"


@pytest.fixture
def boundary(tmp_path, monkeypatch):
    runner = CanvasCLI(tmp_path, allow_unwrapped_for_tests=True)
    account = {"id": 19}
    context = {"account_id": 19, "profile": runner.profile(account), "phase": "quoted",
               "project_id": "project-original", "node_id": "node-original",
               "submit_id": "submit-original", "quoted_credits": 20}
    calls = []
    response = {"expiresAt": "2099-01-01T00:00:00Z"}

    def execute(account, args, timeout, *, deadline=None):
        calls.append((account["id"], tuple(args[:2])))
        if args[:2] == ["node", "confirm"]:
            return {"ok": True, "data": {"creditConfirmationToken": TOKEN, "creditCeiling": 20,
                                          **response}}
        if args[:2] == ["node", "run"]:
            return {"ok": True, "data": {"items": [{"nodeId": context["node_id"],
                "submitId": context["submit_id"], "state": "accepted", "resources": []}]}}
        return {"ok": True, "data": {}}

    monkeypatch.setattr(runner, "_execute", execute)
    yield runner, CanvasClient(runner, account), context, calls, response
    runner.close()


@pytest.mark.parametrize("competing", [["node", "show"], ["node", "confirm", "--project-id", "project-original"],
    ["node", "create", "video", "--project-id=project-original"]])
def test_confirmation_dispatch_excludes_project_writers_but_queries_continue(boundary, competing):
    runner, client, context, calls, _ = boundary
    at_persist, release, same_done, other_done = (threading.Event() for _ in range(4))
    failures, threads = [], []

    def persist(value):
        if value["submission_phase"] == "run_dispatching":
            at_persist.set()
            if not release.wait(2):
                raise RuntimeError("Test boundary was not released")

    def capture(action):
        try:
            action()
        except BaseException as exc:
            failures.append(exc)

    def same_command():
        runner.run({"id": 19}, competing, deadline=time.monotonic() + 2)
        same_done.set()

    def other_command():
        runner.run({"id": 20}, ["node", "show"], deadline=time.monotonic() + 2)
        other_done.set()

    try:
        for action in (lambda: client.submit(context, 20, persist=persist),):
            thread = threading.Thread(target=capture, args=(action,))
            threads.append(thread)
            thread.start()
        assert at_persist.wait(1)
        for action in (same_command, other_command):
            thread = threading.Thread(target=capture, args=(action,))
            threads.append(thread)
            thread.start()
        assert other_done.wait(1), "A different account must not wait for this confirmation"
        if competing[:2] == ["node", "show"]:
            assert same_done.wait(1), "Same-account read-only commands must remain concurrent"
        else:
            assert not same_done.wait(0.03), "Same-project writes cannot interleave before paid dispatch"
    finally:
        release.set()
        for thread in threads:
            thread.join(3)
    assert not failures
    assert not any(thread.is_alive() for thread in threads)
    if competing[:2] == ["node", "show"]:
        assert calls.index((19, ("node", "show"))) < calls.index((19, ("node", "run")))
    else:
        assert calls.index((19, ("node", "run"))) < max(i for i, call in enumerate(calls)
            if call == (19, tuple(competing[:2])))
    assert context["phase"] == "accepted"


@pytest.mark.parametrize("expiry", [None, "", 1793246400, "not-a-date", "2026-99-99T00:00:00Z",
    "2099-01-01T00:00:00", "2020-01-01T00:00:00Z"])
def test_missing_invalid_or_expired_confirmation_never_dispatches(boundary, expiry):
    _, client, context, calls, response = boundary
    response["expiresAt"] = expiry
    durable = []
    with pytest.raises(CanvasCLIError) as found:
        client.submit(context, 20, persist=durable.append)
    assert found.value.code in {"CLI_CONFIRMATION_INVALID", "CLI_CONFIRMATION_EXPIRED"}
    assert not found.value.outcome_unknown and not found.value.details["dispatch_started"]
    assert calls == [(19, ("node", "confirm"))]
    assert TOKEN not in json.dumps(durable)


@pytest.mark.parametrize("offset", ["Z", "+08:00", "-05:30"])
def test_valid_timezone_expiry_persists_safe_timing_without_token(boundary, offset):
    _, client, context, calls, response = boundary
    response["expiresAt"] = "2099-01-01T00:00:00" + offset
    durable = []
    result = client.submit(context, 20, persist=durable.append)
    assert result["phase"] == "accepted"
    assert result["confirmation_expires_at"] == datetime.fromisoformat(
        response["expiresAt"].replace("Z", "+00:00")).timestamp()
    assert result["confirmation_received_at"] <= result["dispatch_prepared_at"] <= result["dispatch_started_at"]
    assert sum(pair == ("node", "run") for _, pair in calls) == 1
    assert TOKEN not in json.dumps(durable)
    assert "creditConfirmationToken" not in json.dumps(durable)


@pytest.mark.parametrize("clock_change", ["wall_forward", "monotonic_forward_wall_backward"])
def test_expiry_is_checked_again_after_durable_dispatch_boundary(boundary, monkeypatch, clock_change):
    _, client, context, calls, response = boundary
    wall, mono = [100.0], [10.0]
    monkeypatch.setattr("app.canvas_client.time.time", lambda: wall[0])
    monkeypatch.setattr("app.canvas_client.time.monotonic", lambda: mono[0])
    response["expiresAt"] = datetime.fromtimestamp(101, timezone.utc).isoformat()

    def persist(value):
        if value["submission_phase"] == "run_dispatching":
            if clock_change == "wall_forward":
                wall[0] = 101
            else:
                wall[0], mono[0] = 99, 11

    with pytest.raises(CanvasCLIError) as found:
        client.submit(context, 20, persist=persist, deadline=100)
    assert found.value.code == "CLI_CONFIRMATION_EXPIRED"
    assert not found.value.outcome_unknown and not found.value.details["dispatch_started"]
    assert calls == [(19, ("node", "confirm"))]
    assert "dispatch_started_at" not in context


def test_waiting_for_submission_guard_consumes_original_deadline_and_does_not_confirm(boundary):
    runner, client, context, calls, _ = boundary
    held, release = threading.Event(), threading.Event()

    def hold():
        with runner.project_guard({"id": 19}, context['project_id']):
            held.set()
            release.wait(2)

    thread = threading.Thread(target=hold)
    thread.start()
    try:
        assert held.wait(1)
        began = time.monotonic()
        with pytest.raises(CanvasCLIError) as found:
            client.submit(context, 20, deadline=time.monotonic() + 0.03)
        assert time.monotonic() - began < 0.5
        assert found.value.code == "CLI_TASK_DEADLINE_EXCEEDED"
        assert not found.value.outcome_unknown
        assert calls == []
    finally:
        release.set()
        thread.join(3)


@pytest.mark.parametrize("manual", [False, True])
def test_durable_boundary_failure_or_manual_stop_releases_guard_without_paid_run(boundary, manual):
    runner, client, context, calls, _ = boundary

    def persist(value):
        if value["submission_phase"] == "run_dispatching":
            if manual:
                raise CanvasCLIError("CLI_TASK_TERMINATED", "Operator stopped task")
            raise OSError("Database unavailable")

    with pytest.raises(CanvasCLIError) as found:
        client.submit(context, 20, persist=persist)
    assert found.value.code == "CLI_SUBMISSION_PERSIST_FAILED"
    assert not found.value.outcome_unknown
    assert calls == [(19, ("node", "confirm"))]
    with runner.account_guard({"id": 19}, deadline=time.monotonic() + 0.1):
        pass


def test_three_confirmation_failures_are_bounded_and_never_run(boundary, monkeypatch):
    runner, client, context, calls, _ = boundary
    sleeps = []

    def fail(account, args, timeout, *, deadline=None):
        calls.append((account["id"], tuple(args[:2])))
        raise CanvasCLIError("cli.invalid_service_response", "Service response was invalid")

    monkeypatch.setattr(runner, "_execute", fail)
    monkeypatch.setattr("app.canvas_client.time.sleep", sleeps.append)
    with pytest.raises(CanvasCLIError) as found:
        client.submit(context, 20, deadline=time.monotonic() + 10)
    assert not found.value.outcome_unknown
    assert calls == [(19, ("node", "confirm"))] * 3
    assert sleeps == [0.25, 0.5]


def test_unknown_paid_run_does_not_reconfirm_or_replay_and_releases_guard(boundary, monkeypatch):
    runner, client, context, calls, _ = boundary
    execute = runner._execute

    def fail_run(account, args, timeout, *, deadline=None):
        if args[:2] == ["node", "run"]:
            calls.append((account["id"], tuple(args[:2])))
            raise CanvasCLIError("cli.invalid_service_response", "Original result unknown", outcome_unknown=True)
        return execute(account, args, timeout, deadline=deadline)

    monkeypatch.setattr(runner, "_execute", fail_run)
    durable = []
    with pytest.raises(CanvasCLIError) as found:
        client.submit(context, 20, persist=lambda value: durable.append(copy.deepcopy(value)))
    assert found.value.outcome_unknown
    assert context["submit_error"]["dispatch_started"]
    assert "dispatch_started_at" in durable[-1]
    with pytest.raises(CanvasCLIError) as replay:
        client.submit(context, 20)
    assert replay.value.outcome_unknown
    assert calls == [(19, ("node", "confirm")), (19, ("node", "run"))]
    with runner.account_guard({"id": 19}, deadline=time.monotonic() + 0.1):
        pass
