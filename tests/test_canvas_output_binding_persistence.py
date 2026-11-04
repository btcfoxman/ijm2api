"""Original-operation output binding must commit before any download effect."""
from contextlib import nullcontext
from copy import deepcopy
import hashlib
from pathlib import Path
import socket
import subprocess
import time
from unittest.mock import Mock

import pytest

from app.canvas_cli import CanvasCLIError
from app.canvas_client import CanvasClient
from app.service import Service
from app.store import Store, accepted_canvas_result
from test_canvas_execution import context, owner, task


RID = "11111111-1111-4111-8111-111111111111"
OTHER = "22222222-2222-4222-8222-222222222222"
VIDEO = b"\x00\x00\x00\x20ftypisom" + b"original-output-fixture"


@pytest.fixture(autouse=True)
def no_live_commands_or_network(monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("Output binding tests forbid real network/processes")
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)


def original_context(account, *, ids=(), count=1, task_id="ijm_binding"):
    return {"backend": "canvas_cli", "account_id": account["id"], "task_id": task_id,
            "profile": "original-profile", "project_id": "original-project", "node_id": "original-node",
            "submit_id": "original-submit", "expected_user_id": account["login_user_id"], "kind": "video",
            "resource_ids": list(ids), "output_count": count, "phase": "accepted", "submission_phase": "accepted",
            "observation_deadline": time.time() + 300,
            "submission_evidence": {"version": 1, "accepted": True, "output_resources": bool(ids),
                                    "receipt": False, "identity_conflict": False, "ambiguous_partial": False},
            "preparation_timing": {"commands": {"node run": {"count": 1, "failed": 0}}}}


def binding_receipt(bound, ids=(RID,)):
    return {"version": 1, "source": "operation_status", "operation_ref": bound["submit_id"],
            **{key: bound[key] for key in ("account_id", "task_id", "profile", "project_id", "node_id",
                                          "submit_id", "expected_user_id", "kind")}, "resource_ids": list(ids)}


class ReadRunner:
    timeout = 120

    def __init__(self, directory, account, bound):
        self.directory, self.account, self.bound = directory, account, bound
        self.calls = []
        self.ids = [RID]
        self.state = "succeeded"
        self.submission_state = None
        self.facts = {}
        self.before_download = None
        self.download_failure = False

    def account_directory(self, account):
        return self.directory

    def profile(self, account):
        return self.bound["profile"]

    def account_guard(self, account, **_kwargs):
        return nullcontext()

    def close(self):
        pass

    def run(self, account, args, **_kwargs):
        pair = tuple(args[:2])
        assert pair in {("auth", "account"), ("operation", "status"), ("resource", "get"), ("resource", "download")}, "Never prepare/confirm/run an accepted task"
        self.calls.append(list(args))
        if pair == ("auth", "account"):
            data = {"userId": account["login_user_id"], "credits": {"available": 90}}
        else:
            assert args[args.index("--project-id") + 1] == self.bound["project_id"]
            if pair == ("operation", "status"):
                assert args[2] == self.bound["submit_id"]
                data = {"operationRef": self.bound["submit_id"], "state": self.state,
                        "resources": [{"resourceId": rid, "state": self.state} for rid in self.ids]}
                if self.submission_state is not None:
                    data["submission"] = {"state": self.submission_state}
            elif pair == ("resource", "get"):
                # Real resource.get need not return project/node/submit fields.
                data = {"resourceId": args[2], "type": "video", "creditsAmount": 20,
                        **self.facts.get(args[2], {})}
            else:
                if self.before_download:
                    self.before_download(args[2])
                if self.download_failure:
                    raise CanvasCLIError("CLI_RESULT_DOWNLOAD_FAILED", "synthetic original file read failed")
                path = Path(args[args.index("--output") + 1]); path.write_bytes(VIDEO)
                data = {"resourceId": args[2], "path": str(path), "size": len(VIDEO), "sha256": hashlib.sha256(VIDEO).hexdigest()}
        return {"ok": True, "data": data}

    def count(self, pair):
        return sum(tuple(args[:2]) == pair for args in self.calls)


@pytest.fixture
def client_case(tmp_path):
    account = {"id": 5, "login_user_id": "original-user", "cli_user_id": "original-user"}
    bound = original_context(account)
    runner = ReadRunner(tmp_path, account, bound)
    return CanvasClient(runner, account), runner, bound


@pytest.fixture
def durable(context, tmp_path):
    store, service, _ = context
    account = owner(store)
    created = task(store, "ijm_binding", backend="canvas_cli")
    bound = original_context(account, task_id=created["id"])
    row = store.update_task(created["id"], {"account_id": account["id"], "status": "running", "progress": 45,
        "estimated_cost": 20, "balance_before": 100, "actual_cost": None, "upstream_context": bound})
    runner = ReadRunner(tmp_path, account, bound)
    service.canvas_cli = runner
    return store, service, account, runner, row


def test_complete_output_list_is_verified_and_saved_in_original_order_before_first_download(client_case):
    client, runner, bound = client_case
    bound.update(resource_ids=[OTHER, RID], output_count=2)
    runner.ids = [RID, OTHER]
    saved = []
    def persist(receipt):
        assert runner.count(("resource", "get")) == 2 and runner.count(("resource", "download")) == 0
        assert receipt == binding_receipt(bound, [OTHER, RID])
        saved.append(deepcopy(receipt)); return True
    runner.before_download = lambda rid: saved and saved[0]["resource_ids"] == [OTHER, RID] or pytest.fail("Download preceded complete durable binding")
    result = client.poll(bound, persist_output_binding=persist, persist_download=lambda _value: True)
    assert result["status"] == "succeeded" and result["resource_ids"] == [OTHER, RID]
    assert [item["resource_id"] for item in result["result_files"]] == [OTHER, RID]
    assert len(saved) == 1 and runner.count(("resource", "download")) == 2


@pytest.mark.parametrize("value", [False, None, 0, 1, "true"])
def test_non_true_binding_acknowledgement_stops_all_downloads(client_case, value):
    client, runner, bound = client_case
    with pytest.raises(CanvasCLIError) as error:
        client.poll(bound, persist_output_binding=lambda _receipt: value)
    assert error.value.code == "CLI_STOPPING" and runner.count(("resource", "download")) == 0
    assert bound["resource_ids"] == []


def test_binding_callback_failure_is_not_a_download_or_an_accepted_new_submission(client_case):
    client, runner, bound = client_case
    def rejected(_receipt):
        raise OSError("synthetic disk commit failed")
    with pytest.raises(CanvasCLIError) as error:
        client.poll(bound, persist_output_binding=rejected)
    assert error.value.code == "CLI_OUTPUT_BINDING_PERSIST_FAILED"
    assert bound["resource_ids"] == [] and runner.count(("resource", "download")) == 0


@pytest.mark.parametrize("change", [{"resourceId": OTHER}, {"type": "image"},
    {"projectId": "another-project"}, {"nodeId": "another-node"}, {"submitId": "another-submit"}])
def test_last_output_validation_failure_cannot_download_the_first_output(client_case, change):
    client, runner, bound = client_case
    bound["output_count"] = 2; runner.ids = [RID, OTHER]
    # The first output is valid; the later output is the contradictory one.
    runner.facts[OTHER] = change if "resourceId" not in change else {"resourceId": RID}
    callback = Mock(return_value=True)
    with pytest.raises(CanvasCLIError):
        client.poll(bound, persist_output_binding=callback)
    callback.assert_not_called()
    assert runner.count(("resource", "download")) == 0 and bound["resource_ids"] == []


@pytest.mark.parametrize("outputs", [[RID, RID], ["not-a-resource-uuid"]])
def test_duplicate_or_non_uuid_resources_are_not_persisted(client_case, outputs):
    client, runner, bound = client_case
    runner.ids = outputs; bound["output_count"] = len(outputs)
    callback = Mock(return_value=True)
    with pytest.raises(CanvasCLIError):
        client.poll(bound, persist_output_binding=callback)
    callback.assert_not_called()
    assert runner.count(("resource", "download")) == 0


def test_incomplete_output_count_never_saves_a_partial_binding(client_case):
    client, runner, bound = client_case
    bound["output_count"] = 2
    callback = Mock(return_value=True)
    result = client.poll(bound, persist_output_binding=callback)
    assert result["status"] == "failed" and result["error_code"] == "CLI_RESULT_INCOMPLETE"
    callback.assert_not_called(); assert runner.count(("resource", "download")) == 0


@pytest.mark.parametrize("field,value", [("account_id", 99), ("task_id", "another-task"),
    ("profile", "retired-profile"), ("project_id", "another-project"), ("node_id", "another-node"),
    ("submit_id", "another-submit"), ("operation_ref", "another-submit"), ("expected_user_id", "another-user"),
    ("kind", "image"), ("resource_ids", [RID, OTHER]), ("resource_ids", ["not-a-uuid"]), ("resource_ids", [RID, RID])])
def test_service_rejects_a_binding_that_changes_accepted_task_identity(durable, field, value):
    store, service, _account, _runner, before = durable
    receipt = binding_receipt(before["upstream_context"]); receipt[field] = value
    assert service._canvas_persist_output_binding(before["id"], receipt) is False
    assert store.task(before["id"]) == before


def test_existing_resource_order_is_an_immutable_part_of_the_binding(durable):
    store, service, _account, _runner, before = durable
    ordered = {**before["upstream_context"], "resource_ids": [RID, OTHER], "output_count": 2}
    before = store.update_task(before["id"], {"upstream_context": ordered})
    assert service._canvas_persist_output_binding(before["id"], binding_receipt(ordered, [OTHER, RID])) is False
    assert store.task(before["id"]) == before


@pytest.mark.parametrize("change", [{"durable": False}, {"durable": None}, {"version": True},
    {"source": "unverified_dict"}, {"operation_ref": "another-submit"},
    {"expected_user_id": "another-user"}, {"resource_ids": [OTHER]}])
def test_unverified_or_contradictory_binding_cannot_override_submit_error_audit(durable, change):
    _store, _service, _account, _runner, before = durable
    row = deepcopy(before)
    bound = row["upstream_context"]
    bound.update(resource_ids=[RID], submit_error={"code": "cli.invalid_service_response"})
    bound["submission_evidence"].update(output_resources=True, receipt=True)
    bound["output_binding"] = {**binding_receipt(bound), "durable": True, **change}
    assert accepted_canvas_result(row) is False


@pytest.mark.parametrize("race", ["terminate", "task_account", "login_user_id", "cli_user_id"])
def test_concurrent_terminal_or_identity_change_wins_before_download(durable, monkeypatch, race):
    store, service, account, runner, row = durable
    original_bind = store.bind_canvas_outputs
    changed = False
    def bind(task_id, binding, **kwargs):
        nonlocal changed
        if not changed:
            changed = True
            other = Store(str(store.path))
            if race == "terminate":
                other.update_task(task_id, {"status": "cancelled", "error_code": "MANUALLY_TERMINATED",
                    "upstream_context": {**other.task(task_id)["upstream_context"], "manual_termination": {"source": "synthetic-admin"}}})
            elif race == "task_account":
                other.update_task(task_id, {"account_id": account["id"] + 1})
            else:
                other.update_account(account["id"], {race: "another-user"})
        return original_bind(task_id, binding, **kwargs)
    monkeypatch.setattr(store, "bind_canvas_outputs", bind)
    client = CanvasClient(runner, account)
    with pytest.raises(CanvasCLIError) as error:
        client.poll(deepcopy(row["upstream_context"]), persist_output_binding=lambda receipt: service._canvas_persist_output_binding(row["id"], receipt))
    assert changed and error.value.code == "CLI_STOPPING"
    assert runner.count(("resource", "download")) == 0


@pytest.mark.parametrize("identity", ["login_user_id", "cli_user_id"])
def test_already_changed_account_identity_cannot_become_the_expected_callback_identity(durable, identity):
    store, service, account, runner, before = durable
    store.update_account(account["id"], {identity: "another-user"})
    with pytest.raises(CanvasCLIError) as error:
        CanvasClient(runner, account).poll(deepcopy(before["upstream_context"]),
            persist_output_binding=lambda receipt: service._canvas_persist_output_binding(before["id"], receipt))
    assert error.value.code == "CLI_STOPPING" and runner.count(("resource", "download")) == 0
    assert store.task(before["id"]) == before


@pytest.mark.parametrize("submit_error_audit", [False, True])
def test_newly_observed_rid_survives_download_failure_and_service_restart_without_run(durable, monkeypatch, submit_error_audit):
    store, service, account, runner, before = durable
    if submit_error_audit:
        audited = {**before["upstream_context"], "phase": "quoted", "submission_phase": "submitting",
            "submission_evidence": {"version": 1, "accepted": False, "output_resources": False,
                "receipt": False, "identity_conflict": False, "ambiguous_partial": False}, "submit_error": {
            "code": "cli.invalid_service_response", "phase": "run_dispatching",
            "message": "Original run returned an invalid response"}}
        before = store.update_task(before["id"], {"upstream_context": audited,
            "error_code": "UPSTREAM_SUBMIT_OUTCOME_UNKNOWN", "error_message": "Original admission receipt unavailable"})
        runner.bound = audited
        assert accepted_canvas_result(before) is False
    observed = []
    def already_durable(rid):
        current = Store(str(store.path)).task(before["id"])
        assert current["upstream_context"]["resource_ids"] == [rid]
        assert current["upstream_context"]["output_binding"] == {
            **binding_receipt(before["upstream_context"]), "durable": True}
        assert accepted_canvas_result(current) is True
        observed.append(rid)
    runner.before_download = already_durable; runner.download_failure = True
    service._poll_canvas(before["id"], CanvasClient(runner, account), time.monotonic() + 10, one_shot=True)
    failed_read = store.task(before["id"])
    assert observed == [RID] and failed_read["status"] == "running"
    assert failed_read["upstream_context"]["resource_ids"] == [RID]
    assert failed_read["error_code"] == "" and accepted_canvas_result(failed_read) is True
    if submit_error_audit:
        assert failed_read["upstream_context"]["submit_error"] == before["upstream_context"]["submit_error"]
    assert failed_read["request"] == before["request"] and failed_read["actual_cost"] is None
    fresh = Service(Store(str(store.path)))
    replacement = ReadRunner(runner.directory, account, failed_read["upstream_context"])
    fresh.canvas_cli = replacement; monkeypatch.setattr(fresh._stop, "wait", Mock(return_value=False))
    try:
        fresh._poll_canvas(before["id"], CanvasClient(replacement, account), time.monotonic() + 10, one_shot=True)
        done = fresh.store.task(before["id"])
        assert done["status"] == "succeeded" and len(done["result_urls"]) == 1
        assert done["request"] == before["request"]
        assert done["upstream_context"]["submit_id"] == before["upstream_context"]["submit_id"]
        assert done["upstream_context"]["preparation_timing"]["commands"]["node run"]["count"] == 1
        if submit_error_audit:
            assert done["upstream_context"]["submit_error"] == before["upstream_context"]["submit_error"]
        assert all(args[:2] != ["node", "run"] for args in runner.calls + replacement.calls)
    finally:
        fresh.stop()


@pytest.mark.parametrize("summary", [None, "unknown", "absent"])
def test_real_in_progress_without_resources_never_consumes_no_resource_recovery_budget(durable, monkeypatch, summary):
    store, service, account, runner, before = durable
    pending = {**before["upstream_context"], "phase": "quoted", "submission_phase": "run_dispatching",
        "submission_evidence": {"version": 1, "accepted": False, "output_resources": False,
            "receipt": False, "identity_conflict": False, "ambiguous_partial": False},
        "submit_error": {"code": "cli.invalid_service_response", "phase": "run_dispatching"}}
    before = store.update_task(before["id"], {"upstream_context": pending,
        "error_code": "UPSTREAM_SUBMIT_OUTCOME_UNKNOWN"})
    runner.state = "in_progress"; runner.ids = []; runner.submission_state = summary
    client = CanvasClient(runner, account)
    clock = [time.time()]
    monkeypatch.setattr("app.canvas_service.time.time", lambda: clock[0])
    for _ in range(5):
        clock[0] += 31
        service._poll_canvas(before["id"], client, time.monotonic() + 10, one_shot=True)
    current = store.task(before["id"])
    assert current["status"] == "running" and current["error_code"] == ""
    assert current["upstream_context"]["resource_ids"] == []
    assert current["upstream_context"]["phase"] == "quoted"
    assert current["upstream_context"]["submission_phase"] == "run_dispatching"
    assert current["upstream_context"]["submission_evidence"]["accepted"] is False
    assert current["upstream_context"]["submit_error"] == pending["submit_error"]
    assert runner.count(("operation", "status")) == 5 and runner.count(("resource", "download")) == 0
    assert all(args[:2] != ["node", "run"] for args in runner.calls)


def test_positive_in_progress_ends_an_anomaly_episode_without_a_stale_sixty_second_kill(durable, monkeypatch):
    store, service, account, runner, before = durable
    runner.state = "in_progress"; runner.ids = []
    client = CanvasClient(runner, account)
    clock = [time.time()]
    monkeypatch.setattr("app.canvas_service.time.time", lambda: clock[0])
    monkeypatch.setattr(client, "_sleep", lambda *_args, **_kwargs: None)
    original_run = runner.run
    failing = [True]
    waits = []
    def read(account, args, **kwargs):
        if args[:2] == ["operation", "status"] and failing[0]:
            raise CanvasCLIError("CLI_NETWORK_ERROR", "synthetic interrupted original operation read")
        return original_run(account, args, **kwargs)
    monkeypatch.setattr(runner, "run", read)
    def wait(_delay):
        current = store.task(before["id"])
        episode = current["upstream_context"]["missing_resource_recovery"]
        waits.append(deepcopy(episode))
        if episode['active']:
            assert episode["active"] is True and episode["attempts"] == 1
            # Include the fixed next-read slot; the extra wait spends no read.
            failing[0] = False; clock[0] += max(1, _delay)
        elif sum(not item['active'] for item in waits) == 1:
            assert episode["active"] is False
            clock[0] += 155
        else:
            assert episode["active"] is False
            service._stop.set()
        return False
    monkeypatch.setattr(service._stop, "wait", wait)
    try:
        service._poll_canvas(before["id"], client, time.monotonic() + 300)
        current = store.task(before["id"])
        assert len(waits) == 4 and current["status"] == "running" and current["error_code"] == ""
        assert waits[0]["started_at"] == waits[-1]["started_at"]
        assert runner.count(("operation", "status")) == 2 and runner.count(("resource", "download")) == 0
        assert all(args[:2] != ["node", "run"] for args in runner.calls)
    finally:
        service._stop.clear()
