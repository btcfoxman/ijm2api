"""Recover reference-upload uncertainty without replaying a paid submission."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from app.canvas_cli import CanvasCLIError
from app.canvas_client import CanvasClient


ACCOUNT = {"id": 6, "login_user_id": "same-account"}
UPLOAD_MESSAGE = ("The upload may already have created a remote object. "
                  "Do not automatically retry; inspect the operation or contact support.")


def upload_unknown():
    return CanvasCLIError("cli.upload_state_unknown", UPLOAD_MESSAGE,
                          details={"ok": False, "error": {
                              "code": "cli.upload_state_unknown", "class": "human_intervention",
                              "message": UPLOAD_MESSAGE, "requiredAction": "human_intervention"}},
                          exit_code=13)


class UploadRunner:
    timeout = 120

    def __init__(self, clock, responses, *, upload_succeeds=False):
        self.clock, self.responses = clock, list(responses)
        self.upload_succeeds = upload_succeeds
        self.calls = []

    def run(self, account, args, timeout=None, deadline=None):
        self.calls.append((list(args), timeout, deadline))
        if args[:2] == ["resource", "upload"]:
            if self.upload_succeeds:
                return {"ok": True, "data": {"resourceId": "original-resource", "mediaType": "image"}}
            raise upload_unknown()
        assert args[:2] == ["resource", "get"], "upload recovery must never create/run a node"
        assert args[2:] == ["original-resource", "--project-id", "original-project"]
        result = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if callable(result):
            result = result()
        if isinstance(result, Exception):
            raise result
        return {"ok": True, "data": result}


@pytest.fixture
def clock(monkeypatch):
    state = {"now": 0.0}
    monkeypatch.setattr("app.canvas_client.time", SimpleNamespace(
        monotonic=lambda: state["now"],
        sleep=lambda delay: state.update(now=state["now"] + delay)))
    return state


def upload(client, *, deadline=150):
    client._upload_resource(Path("original-file.png"), "image", "original-resource", "original-project",
                            deadline=deadline)


def ready(resource_id="original-resource"):
    return {"resourceId": resource_id, "type": "image", "status": "success"}


def test_unknown_upload_recovers_original_id_after_registration_delay(clock):
    missing = CanvasCLIError("cli.resource_unavailable", "Resource unavailable")
    runner = UploadRunner(clock, [missing] * 7 + [ready()])
    upload(CanvasClient(runner, ACCOUNT))
    assert clock["now"] >= 7
    assert sum(args[:2] == ["resource", "upload"] for args, _, _ in runner.calls) == 1
    assert len(runner.calls) == 9


def test_unknown_upload_read_reconnects_without_reupload_or_new_id(clock):
    timeout = CanvasCLIError("CLI_COMMAND_TIMEOUT", "Read timed out")
    runner = UploadRunner(clock, [timeout] * 3 + [ready()])
    upload(CanvasClient(runner, ACCOUNT))
    assert sum(args[:2] == ["resource", "upload"] for args, _, _ in runner.calls) == 1
    assert len(runner.calls) == 5


def test_unknown_upload_can_observe_processing_beyond_original_five_seconds(clock):
    def processing_then_ready():
        return ready() if clock["now"] >= 8 else {"resourceId": "original-resource", "status": "running"}
    runner = UploadRunner(clock, [processing_then_ready])
    upload(CanvasClient(runner, ACCOUNT))
    assert clock["now"] == 8
    assert sum(args[:2] == ["resource", "upload"] for args, _, _ in runner.calls) == 1


def test_unknown_upload_observation_remains_unknown_when_local_budget_expires(clock):
    runner = UploadRunner(clock, [CanvasCLIError("cli.resource_unavailable", "Resource unavailable")])
    with pytest.raises(CanvasCLIError) as found:
        upload(CanvasClient(runner, ACCOUNT), deadline=300)
    assert found.value.code == "cli.upload_state_unknown"
    assert found.value.message == UPLOAD_MESSAGE
    assert clock["now"] == 120
    assert sum(args[:2] == ["resource", "upload"] for args, _, _ in runner.calls) == 1


def test_unknown_upload_does_not_renew_original_task_deadline(clock):
    runner = UploadRunner(clock, [{"resourceId": "original-resource", "status": "running"}])
    with pytest.raises(CanvasCLIError) as found:
        upload(CanvasClient(runner, ACCOUNT), deadline=3)
    assert found.value.code == "CLI_TASK_DEADLINE_EXCEEDED"
    assert clock["now"] == 3
    assert all(deadline <= 3 for _, _, deadline in runner.calls)
    assert sum(args[:2] == ["resource", "upload"] for args, _, _ in runner.calls) == 1


@pytest.mark.parametrize("state", ["failed", "error", "canceled", "cancelled", "rejected"])
def test_terminal_upload_failure_never_reuploads(clock, state):
    runner = UploadRunner(clock, [{"resourceId": "original-resource", "status": state}])
    with pytest.raises(CanvasCLIError) as found:
        upload(CanvasClient(runner, ACCOUNT))
    assert found.value.code == "MEDIA_UPLOAD_FAILED"
    assert len(runner.calls) == 2


def test_confirmed_upload_terminal_failure_stops_without_second_read_or_reupload(clock):
    runner = UploadRunner(clock, [{"resourceId": "original-resource", "status": "failed"}, ready()],
                          upload_succeeds=True)
    with pytest.raises(CanvasCLIError) as found:
        upload(CanvasClient(runner, ACCOUNT))
    assert found.value.code == "MEDIA_UPLOAD_FAILED"
    assert len(runner.calls) == 2


@pytest.mark.parametrize("code", ["cli.authentication_required", "CLI_ACCOUNT_MISMATCH", "12009",
                                  "CLI_COMMAND_CANCELLED"])
def test_nontransport_read_error_stops_unknown_upload_recovery(clock, code):
    runner = UploadRunner(clock, [CanvasCLIError(code, "Explicit rejection")])
    with pytest.raises(CanvasCLIError) as found:
        upload(CanvasClient(runner, ACCOUNT))
    assert found.value.code == code
    assert clock["now"] == 0
    assert len(runner.calls) == 2


def test_unknown_upload_requires_positive_original_resource_id(clock):
    runner = UploadRunner(clock, [ready("another-resource")])
    with pytest.raises(CanvasCLIError) as found:
        upload(CanvasClient(runner, ACCOUNT))
    assert found.value.code == "CLI_RESOURCE_MISMATCH"
    assert len(runner.calls) == 2
