from __future__ import annotations

import copy
import hashlib
import json
import os
import stat
import time
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.canvas_cli import CanvasCLIError
from app.canvas_client import CanvasClient


RESOURCE = "11111111-1111-4111-8111-111111111111"
ACCOUNT = {"id": 19, "login_user_id": "original-user"}
VIDEO = b"\x00\x00\x00\x20ftypisom" + b"complete-original-video"


def read_failure(**changes):
    error = {"code": "cli.download_read_failed", "class": "retryable_failure",
             "retryable": True, "requiredAction": "retry", **changes}
    return CanvasCLIError("cli.download_read_failed", "Original download read failed",
                          details={"error": error}, exit_code=21)


class Runner:
    """Real Client with only original-operation reads and local output I/O."""
    def __init__(self, root):
        self.root = root
        self.calls = []
        self.errors = []
        self.state = "succeeded"
        self.profile_name = "original-profile"
        self.payload = VIDEO
        self.kind = "video"
        self.output_ids = [RESOURCE]

    def account_directory(self, account):
        assert account["id"] == ACCOUNT["id"]
        return self.root / "account-19"

    def account_guard(self, account, **kwargs):
        assert account == ACCOUNT
        return nullcontext()

    def profile(self, account):
        return self.profile_name

    def run(self, account, args, **kwargs):
        assert account == ACCOUNT
        self.calls.append((list(args), dict(kwargs)))
        pair = args[:2]
        assert pair in (["operation", "status"], ["resource", "get"], ["resource", "download"]), "No generation permitted"
        assert args[args.index("--project-id") + 1] == "original-project"
        if pair == ["operation", "status"]:
            assert args[2] == "original-submit"
            data = {"operationRef": args[2], "state": self.state,
                    "resources": [{"resourceId": value, "state": self.state} for value in self.output_ids]}
        elif pair == ["resource", "get"]:
            assert args[2] in self.output_ids
            data = {"resourceId": args[2], "type": self.kind, "creditsAmount": 76}
        else:
            assert args[2] in self.output_ids
            path = Path(args[args.index("--output") + 1])
            path.write_bytes(self.payload)
            if self.errors:
                raise self.errors.pop(0)
            data = {"resourceId": args[2], "path": str(path), "size": len(self.payload),
                    "sha256": hashlib.sha256(self.payload).hexdigest()}
        return {"ok": True, "data": data}

    @property
    def downloads(self):
        return [args for args, _ in self.calls if args[:2] == ["resource", "download"]]


def make_client(tmp_path):
    runner = Runner(tmp_path)
    context = {"task_id": "ijm_original", "account_id": 19, "profile": "original-profile",
               "project_id": "original-project", "node_id": "original-node", "submit_id": "original-submit",
               "expected_user_id": "original-user", "kind": "video", "resource_ids": [RESOURCE],
               "phase": "accepted", "submission_phase": "accepted", "output_count": 1}
    return runner, CanvasClient(runner, ACCOUNT), context


def durable_download(client, context):
    saved = []
    result = client.poll(context, persist_download=lambda receipt: saved.append(receipt))
    assert result["status"] == "succeeded" and len(saved) == 1
    return result["result_files"][0], saved[0]


def test_missing_reason_native_read_failure_retries_only_original_download(tmp_path, monkeypatch):
    monkeypatch.setattr("app.canvas_client.time.sleep", lambda _: None)
    runner, client, context = make_client(tmp_path)
    runner.errors = [read_failure(), read_failure()]
    phases = []
    saved = []
    result = client.poll(context, deadline=time.monotonic() + 30,
                         persist_download=lambda value: saved.append(value), on_phase=phases.append)
    assert result["status"] == "succeeded"
    assert phases == ["querying", "downloading"]
    assert len(runner.downloads) == 3
    assert len({args[-1] for args in runner.downloads}) == 3
    assert all(args[2] == RESOURCE for args in runner.downloads)
    assert len({call[1]["deadline"] for call in runner.calls}) == 1
    assert not list(runner.root.rglob("*.part"))
    assert context["download_receipts"][RESOURCE] == saved[0]


@pytest.mark.parametrize("changes", [
    {"retryable": "true"}, {"retryable": 1}, {"retryable": False},
    {"requiredAction": "contact_support"}, {"class": "auth_required"},
    {"reason": "human_intervention"}, {"serviceCode": 403}, {"code": "other.error"},
])
def test_native_read_retry_requires_exact_typed_contract(tmp_path, changes):
    runner, client, context = make_client(tmp_path)
    runner.errors = [read_failure(**changes)]
    with pytest.raises(CanvasCLIError) as found:
        client.poll(context)
    assert found.value.code == "cli.download_read_failed"
    assert len(runner.downloads) == 1
    assert "download_receipts" not in context


def test_native_read_retry_has_three_attempt_cap(tmp_path, monkeypatch):
    monkeypatch.setattr("app.canvas_client.time.sleep", lambda _: None)
    runner, client, context = make_client(tmp_path)
    runner.errors = [read_failure() for _ in range(4)]
    with pytest.raises(CanvasCLIError):
        client.poll(context)
    assert len(runner.downloads) == 3 and len(runner.errors) == 1
    assert not list(runner.root.rglob("*.part"))


def test_retry_keeps_absolute_deadline_and_does_not_start_late_attempt(tmp_path, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr("app.canvas_client.time.monotonic", lambda: clock[0])
    monkeypatch.setattr("app.canvas_client.time.sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    runner, client, context = make_client(tmp_path)
    runner.errors = [read_failure(), read_failure()]
    with pytest.raises(CanvasCLIError) as found:
        client.poll(context, deadline=100.5)
    assert found.value.code == "CLI_TASK_DEADLINE_EXCEEDED"
    assert len(runner.downloads) == 2
    assert all(kwargs["deadline"] == 100.5 for _, kwargs in runner.calls)
    assert clock[0] == 100.5


@pytest.mark.parametrize("kind", ["video", "image"])
def test_durable_receipt_survives_restart_and_avoids_second_download(tmp_path, kind):
    runner, client, context = make_client(tmp_path)
    runner.kind = context['kind'] = kind
    if kind == 'image':
        runner.payload = b'\xff\xd8\xfforiginal-image'
    file, receipt = durable_download(client, context)
    # A new process receives only JSON-roundtripped durable context, no client state.
    restored = json.loads(json.dumps(context))
    restarted = Runner(tmp_path)
    restarted.kind = kind
    second = CanvasClient(restarted, ACCOUNT)
    saved, phases = [], []
    result = second.poll(restored, persist_download=saved.append, on_phase=phases.append)
    assert result["status"] == "succeeded"
    assert result["result_files"][0] == file
    assert restarted.downloads == [] and saved == [receipt]
    assert phases == ["querying", "downloading"]
    assert [args[:2] for args, _ in restarted.calls] == [["operation", "status"], ["resource", "get"]]
    assert Path(file["path"]).read_bytes() == runner.payload


def test_two_outputs_keep_original_accepted_order_across_reversed_response_and_cache_resume(tmp_path):
    runner, client, context = make_client(tmp_path)
    second_id = '22222222-2222-4222-8222-222222222222'
    original_order = [RESOURCE, second_id]
    context.update(resource_ids=original_order, output_count=2)
    runner.output_ids = list(reversed(original_order))
    saved = []
    result = client.poll(context, persist_download=saved.append)
    assert [item['resource_id'] for item in result['result_files']] == original_order
    assert [item['resource_id'] for item in saved] == original_order
    assert [args[2] for args in runner.downloads] == original_order
    restored = json.loads(json.dumps(context))
    restarted = Runner(tmp_path)
    restarted.output_ids = original_order
    resumed = CanvasClient(restarted, ACCOUNT).poll(restored, persist_download=lambda _: True)
    assert resumed['result_files'] == result['result_files']
    assert restarted.downloads == []
    assert [item['resource_id'] for item in resumed['result_files']] == original_order


@pytest.mark.parametrize("field,value", [
    ("task_id", "another-task"), ("account_id", 20), ("profile", "another-profile"),
    ("project_id", "another-project"), ("node_id", "another-node"), ("submit_id", "another-submit"),
    ("expected_user_id", "another-user"), ("resource_id", "22222222-2222-4222-8222-222222222222"),
    ("kind", "image"), ("filename", "../elsewhere.mp4"), ("version", True),
    ("mime", "image/jpeg"), ("size_bytes", True), ("sha256", "0" * 64),
])
def test_changed_receipt_binding_or_integrity_requires_fresh_download(tmp_path, field, value):
    runner, client, context = make_client(tmp_path)
    durable_download(client, context)
    context["download_receipts"][RESOURCE][field] = value
    client.poll(context, persist_download=lambda _: True)
    assert len(runner.downloads) == 2
    actual = context["download_receipts"][RESOURCE][field]
    assert type(actual) is not type(value) or actual != value


@pytest.mark.parametrize("damage", ["same_size_corrupt", "truncated", "missing", "missing_receipt", "wrong_mime"])
def test_unverified_or_corrupt_cached_file_is_never_published(tmp_path, damage):
    runner, client, context = make_client(tmp_path)
    file, _ = durable_download(client, context)
    path = Path(file["path"])
    if damage == "same_size_corrupt":
        path.write_bytes(VIDEO[:-1] + b"X")
    elif damage == "truncated":
        path.write_bytes(VIDEO[:15])
    elif damage == "missing":
        path.unlink()
    elif damage == "missing_receipt":
        context.pop("download_receipts")
    else:
        path.write_bytes(b"X" * len(VIDEO))
        context["download_receipts"][RESOURCE]["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    client.poll(context, persist_download=lambda _: True)
    assert len(runner.downloads) == 2 and path.read_bytes() == VIDEO


@pytest.mark.parametrize("cached", [False, True])
@pytest.mark.parametrize("decision", ["stopped", "error"])
def test_receipt_rejection_or_persistence_error_returns_no_deliverable(tmp_path, cached, decision):
    runner, client, context = make_client(tmp_path)
    if cached:
        durable_download(client, context)
    before = copy.deepcopy(context)
    def persist(_):
        if decision == "error":
            raise OSError("private storage failure")
        return False
    with pytest.raises(CanvasCLIError) as found:
        client.poll(context, persist_download=persist)
    assert found.value.code == ("CLI_STOPPING" if decision == "stopped" else "CLI_DOWNLOAD_RECEIPT_PERSIST_FAILED")
    assert context == before
    assert len(runner.downloads) == 1


def test_receipt_callback_runs_after_file_fsync_and_before_client_claim(tmp_path, monkeypatch):
    runner, client, context = make_client(tmp_path)
    events = []
    original = os.fsync
    def synced(fd):
        events.append("fsync")
        original(fd)
    monkeypatch.setattr("app.canvas_client.os.fsync", synced)
    def persist(receipt):
        assert "fsync" in events and "download_receipts" not in context
        path = runner.account_directory(ACCOUNT) / "results" / context["task_id"] / receipt["filename"]
        assert path.read_bytes() == VIDEO
        events.append("persist")
    client.poll(context, persist_download=persist)
    assert events[-1] == "persist"


def test_legacy_direct_download_without_complete_binding_never_caches(tmp_path):
    runner, client, context = make_client(tmp_path)
    context.pop("expected_user_id")
    first = client.download_result(context, RESOURCE, "video")
    second = client.download_result(context, RESOURCE, "video")
    assert first == second and len(runner.downloads) == 2
    assert "download_receipt" not in first and "download_receipts" not in context


@pytest.mark.parametrize("outputs", [None, [], ["another-resource"], [RESOURCE, RESOURCE], [False]])
def test_cache_requires_original_received_output_membership(tmp_path, outputs):
    runner, client, context = make_client(tmp_path)
    durable_download(client, context)
    context['resource_ids'] = outputs
    # A direct legacy read is still possible but cannot reuse the old receipt.
    client.download_result(context, RESOURCE, 'video')
    assert len(runner.downloads) == 2
    with pytest.raises(CanvasCLIError) as found:
        client.download_result(context, RESOURCE, 'video', persist_download=lambda _: True)
    assert found.value.code == 'CLI_RESULT_INVALID' and len(runner.downloads) == 2


@pytest.mark.parametrize("change", ["retired_profile", "different_account_uid"])
def test_current_identity_mismatch_never_reads_or_downloads_cached_result(tmp_path, change):
    runner, client, context = make_client(tmp_path)
    durable_download(client, context)
    if change == "retired_profile":
        runner.profile_name = "replacement-profile"
    else:
        client.account["login_user_id"] = "replacement-user"
    with pytest.raises(CanvasCLIError) as found:
        client.download_result(context, RESOURCE, "video")
    assert found.value.code == "CLI_ACCOUNT_MISMATCH"
    assert len(runner.downloads) == 1


def test_phase_can_stop_before_any_native_read(tmp_path):
    runner, client, context = make_client(tmp_path)
    with pytest.raises(CanvasCLIError) as found:
        client.poll(context, on_phase=lambda _: False)
    assert found.value.code == "CLI_STOPPING" and runner.calls == []


def test_nonterminal_operation_reports_generation_without_downloading(tmp_path):
    runner, client, context = make_client(tmp_path)
    runner.state = "running"
    phases = []
    assert client.poll(context, on_phase=phases.append)["status"] == "running"
    assert phases == ["querying", "generating"] and runner.downloads == []


def test_windows_junction_result_directory_is_rejected_without_native_io(tmp_path, monkeypatch):
    runner, client, context = make_client(tmp_path)
    results = runner.account_directory(ACCOUNT) / "results"
    original = Path.lstat
    def lstat(path, *args, **kwargs):
        if path == results:
            return SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=0x400)
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "lstat", lstat)
    with pytest.raises(CanvasCLIError) as found:
        client.download_result(context, RESOURCE, "video")
    assert found.value.code == "CLI_RESULT_INVALID" and runner.calls == []


@pytest.mark.skipif(os.name == "nt", reason="Windows symlink creation requires an OS privilege")
@pytest.mark.parametrize("location", ["final", "results", "task"])
def test_symlink_in_original_result_path_is_rejected_without_download(tmp_path, location):
    runner, client, context = make_client(tmp_path)
    file, _ = durable_download(client, context)
    path = Path(file["path"])
    target = path if location == "final" else path.parent if location == "task" else path.parent.parent
    destination = tmp_path / "outside"
    target.rename(destination)
    target.symlink_to(destination, target_is_directory=location != "final")
    with pytest.raises(CanvasCLIError) as found:
        client.poll(context)
    assert found.value.code == "CLI_RESULT_INVALID"
    assert len(runner.downloads) == 1
