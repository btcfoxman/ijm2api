"""Original-result transport retries cannot replay or rebind generation."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from app.canvas_cli import CanvasCLIError
from app.canvas_client import CanvasClient
from test_canvas_download_resume import RESOURCE, VIDEO, make_client


def transport_failure(outer_code="cli.transport_failed", **changes):
    error = {"code": outer_code, "class": "retryable_failure", "retryable": True,
             "requiredAction": "retry", **changes}
    return CanvasCLIError(outer_code, "Synthetic original-result read failure",
                          details={"error": error}, exit_code=21)


@pytest.mark.parametrize("code", ["cli.transport_failed", "cli.download_read_failed"])
def test_native_transport_without_reason_retries_only_bound_original_download(tmp_path, monkeypatch, code):
    monkeypatch.setattr("app.canvas_client.time.sleep", lambda _: None)
    runner, client, context = make_client(tmp_path)
    runner.errors = [transport_failure(code), transport_failure(code)]
    deadline = time.monotonic() + 30
    saved = []
    result = client.poll(context, deadline=deadline, persist_download=saved.append)
    assert result["status"] == "succeeded" and len(saved) == 1
    assert len(runner.downloads) == 3
    assert all(args[2] == RESOURCE for args in runner.downloads)
    assert len({args[-1] for args in runner.downloads}) == 3
    assert all(kwargs["deadline"] == deadline for _, kwargs in runner.calls)
    assert [args[:2] for args, _ in runner.calls] == [
        ["operation", "status"], ["resource", "get"], *([["resource", "download"]] * 3)]
    assert Path(result["result_paths"][0]).read_bytes() == VIDEO
    assert not list(runner.root.rglob("*.part"))


@pytest.mark.parametrize("code", ["cli.transport_failed", "cli.download_read_failed"])
@pytest.mark.parametrize("changes", [
    {"class": "authentication_failure"}, {"class": "invalid_input"},
    {"class": "conflict"}, {"requiredAction": "contact_support"},
    {"requiredAction": "login"}, {"retryable": False}, {"retryable": "true"},
    {"retryable": 1}, {"serviceCode": 403}, {"code": "cli.authentication_required"},
])
def test_network_reason_cannot_override_native_hard_failure(tmp_path, code, changes):
    runner, client, context = make_client(tmp_path)
    runner.errors = [transport_failure(code, reason="network_error", **changes)]
    with pytest.raises(CanvasCLIError) as found:
        client.poll(context)
    assert found.value.code == code
    assert len(runner.downloads) == 1
    assert "download_receipts" not in context
    assert not list(runner.root.rglob("*.part"))


@pytest.mark.parametrize("changes", [
    {"class": "authentication_failure"}, {"class": "invalid_input"},
    {"code": "cli.permission_denied"}, {"requiredAction": "fix_input"},
    {"retryable": False}, {"retryable": "true"}, {"serviceCode": 409},
])
def test_legacy_timeout_cannot_override_explicit_hard_failure(changes):
    assert not CanvasClient._download_retryable(transport_failure(
        "CLI_COMMAND_TIMEOUT", reason="network_error", **changes))


def test_transport_retry_requires_its_explicit_nested_code():
    assert not CanvasClient._download_retryable(transport_failure(code=None))


def test_original_download_transport_retry_has_one_three_attempt_budget(tmp_path, monkeypatch):
    monkeypatch.setattr("app.canvas_client.time.sleep", lambda _: None)
    runner, client, context = make_client(tmp_path)
    runner.errors = [transport_failure() for _ in range(4)]
    with pytest.raises(CanvasCLIError):
        client.poll(context)
    assert len(runner.downloads) == 3 and len(runner.errors) == 1
    assert len(runner.calls) == 5  # Two reads plus three downloads, never 3 x 3.
    assert "download_receipts" not in context


def test_transport_retry_stops_at_original_absolute_deadline(tmp_path, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr("app.canvas_client.time.monotonic", lambda: clock[0])
    monkeypatch.setattr("app.canvas_client.time.sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    runner, client, context = make_client(tmp_path)
    runner.errors = [transport_failure(), transport_failure()]
    with pytest.raises(CanvasCLIError) as found:
        client.poll(context, deadline=100.5)
    assert found.value.code == "CLI_TASK_DEADLINE_EXCEEDED"
    assert len(runner.downloads) == 2 and clock[0] == 100.5
    assert "download_receipts" not in context


def fake_clock(monkeypatch, initial=100.0):
    clock = [initial]
    monkeypatch.setattr("app.canvas_client.time.monotonic", lambda: clock[0])
    monkeypatch.setattr("app.canvas_client.time.sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    return clock


def test_verified_output_claims_delivery_once_and_long_download_outlives_query_budget(tmp_path, monkeypatch):
    clock = fake_clock(monkeypatch)
    runner, client, context = make_client(tmp_path)
    original = runner.run
    events, saved = [], []

    def run(account, args, **kwargs):
        if args[:2] in (["operation", "status"], ["resource", "get"]):
            assert kwargs["deadline"] == 220.0
            clock[0] += 5
        else:
            assert kwargs["deadline"] == 1010.0
            assert kwargs["timeout"] == 900.0  # No hidden 180s command cutoff.
            clock[0] += 240
        return original(account, args, **kwargs)

    def persist(receipt):
        events.append("binding")
        assert receipt["resource_ids"] == [RESOURCE]
        return True

    def delivery_deadline():
        events.append("delivery_claim")
        assert context["output_binding"]["resource_ids"] == [RESOURCE]
        return clock[0] + 900

    runner.run = run
    result = client.poll(context, deadline=220.0, persist_output_binding=persist,
                         get_delivery_deadline=delivery_deadline, persist_download=saved.append)
    assert result["status"] == "succeeded" and clock[0] == 350.0
    assert events == ["binding", "delivery_claim"] and len(saved) == 1
    assert context["download_receipts"][RESOURCE] == saved[0]
    assert len(runner.downloads) == 1


def test_all_original_outputs_and_read_retries_share_one_delivery_deadline(tmp_path, monkeypatch):
    clock = fake_clock(monkeypatch)
    runner, client, context = make_client(tmp_path)
    second = "22222222-2222-4222-8222-222222222222"
    runner.output_ids = context["resource_ids"] = [RESOURCE, second]
    context["output_count"] = 2
    runner.errors = [transport_failure()]
    original = runner.run
    claims = []

    def run(account, args, **kwargs):
        if args[:2] == ["resource", "download"]:
            assert kwargs["deadline"] == 1000.0
            clock[0] += 10
        return original(account, args, **kwargs)

    def delivery_deadline():
        claims.append(True)
        return 1000.0

    runner.run = run
    result = client.poll(context, deadline=220.0, persist_output_binding=lambda _: True,
                         get_delivery_deadline=delivery_deadline, persist_download=lambda _: True)
    assert result["status"] == "succeeded" and claims == [True]
    assert [args[2] for args in runner.downloads] == [RESOURCE, RESOURCE, second]
    budgets = [kwargs["timeout"] for args, kwargs in runner.calls if args[:2] == ["resource", "download"]]
    assert budgets == [900.0, 889.75, 879.75]
    assert set(context["download_receipts"]) == {RESOURCE, second}


@pytest.mark.parametrize("case", ["running", "wrong_operation", "wrong_resource", "binding_refused", "query_timeout"])
def test_delivery_not_claimed_before_successful_complete_binding(tmp_path, monkeypatch, case):
    clock = fake_clock(monkeypatch)
    runner, client, context = make_client(tmp_path)
    original = runner.run
    claims = []

    def run(account, args, **kwargs):
        reply = original(account, args, **kwargs)
        if args[:2] == ["operation", "status"]:
            if case == "running":
                reply["data"]["state"] = "running"
            elif case == "wrong_operation":
                reply["data"]["operationRef"] = "another-submit"
            elif case == "query_timeout":
                clock[0] = 221.0
        elif case == "wrong_resource":
            reply["data"]["resourceId"] = "another-resource"
        return reply

    runner.run = run
    kwargs = {"deadline": 220.0, "persist_output_binding": lambda _: case != "binding_refused",
              "get_delivery_deadline": lambda: claims.append(True) or 1000.0}
    if case == "running":
        assert client.poll(context, **kwargs)["status"] == "running"
    else:
        with pytest.raises(CanvasCLIError):
            client.poll(context, **kwargs)
    assert claims == [] and runner.downloads == []


@pytest.mark.parametrize("deadline", [None, True, "900", float("nan"), float("inf"), 99.0])
def test_invalid_or_spent_delivery_claim_cannot_start_download(tmp_path, monkeypatch, deadline):
    fake_clock(monkeypatch)
    runner, client, context = make_client(tmp_path)
    with pytest.raises(CanvasCLIError) as found:
        client.poll(context, deadline=220.0, persist_output_binding=lambda _: True,
                    get_delivery_deadline=lambda: deadline)
    assert found.value.code in {"CLI_RESULT_INVALID", "CLI_TASK_DEADLINE_EXCEEDED"}
    assert runner.downloads == [] and "download_receipts" not in context


def test_download_default_budget_is_bounded_900_seconds_not_runner_or_180(tmp_path, monkeypatch):
    fake_clock(monkeypatch)
    runner, client, context = make_client(tmp_path)
    client.download_result(context, RESOURCE, "video", persist_download=lambda _: True)
    _, kwargs = runner.calls[0]
    assert kwargs["deadline"] == 1000.0 and kwargs["timeout"] == 900.0


def test_delivery_timeout_does_not_renew_or_start_another_download(tmp_path, monkeypatch):
    clock = fake_clock(monkeypatch)
    runner, client, context = make_client(tmp_path)
    original = runner.run
    claims = []

    def run(account, args, **kwargs):
        if args[:2] == ["resource", "download"]:
            original(account, args, **kwargs)  # Simulate bytes written before cutoff.
            clock[0] = kwargs["deadline"]
            raise CanvasCLIError("CLI_COMMAND_TIMEOUT", "Synthetic download cutoff")
        return original(account, args, **kwargs)

    runner.run = run
    with pytest.raises(CanvasCLIError) as found:
        client.poll(context, deadline=220.0, persist_output_binding=lambda _: True,
                    get_delivery_deadline=lambda: claims.append(True) or 1000.0)
    assert found.value.code == "CLI_TASK_DEADLINE_EXCEEDED"
    assert claims == [True] and len(runner.downloads) == 1
    assert not list(runner.root.rglob("*.part")) and "download_receipts" not in context
