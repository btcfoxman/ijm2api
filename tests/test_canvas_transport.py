from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from app.canvas_cli import CanvasCLI, CanvasCLIError, sanitize
from app.canvas_client import CanvasClient, _file_mime, generation_flags, prompt_with_resources, select_model, select_mode


ACCOUNT = {"id": 5, "login_user_id": "42", "proxy_url": "socks5://user:secret@127.0.0.1:20005"}


def model(kind="video"):
    return {"model": "seedance_2.0_mini" if kind == "video" else "gpt_image_2_5_flare",
            "aliases": ["sd-2-0-mini"] if kind == "video" else ["gpt-image-2.5-flare"],
            "modes": [{"name": mode, "flags": [
                {"flag": "--ratio", "values": ["16:9", "9:16", "1:1"]},
                {"flag": "--resolution", "values": ["480p", "720p"] if kind == "video" else ["1K", "2K"]},
                {"flag": "--duration", "min": 4, "max": 15, "step": 1},
                {"flag": "--count", "min": 1, "max": 4}],
                "references": {"min": 0, "max": 15, "types": [
                    {"mediaType": "image", "min": 0, "max": 9},
                    {"mediaType": "audio", "min": 0, "max": 3},
                    {"mediaType": "video", "min": 0, "max": 3}]}}
                       for mode in (["t2v", "m2v", "first_last_frame"] if kind == "video" else ["t2i", "i2i"])]}


def request():
    return {"model": "sd-2-0-mini", "prompt": "图片1 Image1 video1 Audio1 图片26", "duration": 4,
            "resolution": "480p", "aspect_ratio": "9:16"}


class FakeRunner:
    def __init__(self, tmp_path):
        self.path = tmp_path
        self.calls = []
        self.persisted = []
        self.upload_timeout = False
        self.run_failure = None
        self.state = "completed"
        self.media_type = "video"
        self.resource_error_code = 0
        self.diagnostic_failure = False

    def account_directory(self, account):
        return self.path

    def profile(self, account):
        return "test-account-5"

    def run(self, account, args, timeout=None):
        self.calls.append(args)
        action = args[:2]
        if action == ["auth", "account"]:
            data = {"userId": 42, "displayName": "test", "credits": {"available": "123.5", "details": []}}
        elif action == ["model", "list"]:
            data = {"items": [model(args[-1])]}
        elif action == ["canvas", "ls"]:
            data = {"items": []}
        elif action == ["canvas", "create"]:
            assert self.persisted, "IDs must be stored before the first remote write"
            data = {"project": {"projectId": args[-1]}}
        elif action == ["resource", "upload"]:
            if self.upload_timeout:
                raise CanvasCLIError("CLI_COMMAND_TIMEOUT", "upload timed out")
            data = {"resourceId": args[-1]}
        elif action == ["resource", "get"]:
            if self.diagnostic_failure:
                raise CanvasCLIError("CLI_COMMAND_TIMEOUT", "diagnostic unavailable")
            data = {"resourceId": args[2], "status": "success", "type": self.media_type, "creditsAmount": 20,
                    "errorCode": self.resource_error_code}
        elif action == ["node", "create"]:
            assert "--run" not in args
            data = {"node": {"nodeId": args[args.index("--node-id") + 1]}}
        elif action == ["node", "quote"]:
            data = {"items": [{"nodeId": args[-1], "maxCredits": 20}], "totalMaxCredits": 20,
                    "draftVersion": 1, "confirmable": True, "confirmationRequired": True}
        elif action == ["node", "show"]:
            if self.diagnostic_failure:
                raise CanvasCLIError("CLI_COMMAND_TIMEOUT", "diagnostic unavailable")
            data = {"nodes": []}
        elif action == ["node", "confirm"]:
            data = {"creditConfirmationToken": "never-save-this-token", "creditCeiling": 20}
        elif action == ["node", "run"]:
            if self.run_failure:
                raise self.run_failure
            data = {"items": [{"nodeId": args[args.index("--node-id") + 1],
                               "submitId": args[args.index("--submit-id") + 1], "state": "accepted",
                               "resources": [{"resourceId": "11111111-1111-4111-8111-111111111111", "state": "running"}]}]}
        elif action == ["operation", "status"]:
            data = {"operationRef": args[2], "state": self.state, "resources": [{
                "resourceId": "11111111-1111-4111-8111-111111111111", "state": self.state}]}
        elif action == ["resource", "download"]:
            output = Path(args[-1])
            output.write_bytes(b"\x00\x00\x00\x20ftypisom" + b"original-bytes" if self.media_type == "video"
                               else b"\xff\xd8\xff" + b"original-jpeg")
            data = {"path": str(output), "size": output.stat().st_size}
        else:
            raise AssertionError(args)
        return {"ok": True, "data": data}


def prepare(tmp_path, *, upload_timeout=False):
    runner = FakeRunner(tmp_path)
    runner.upload_timeout = upload_timeout
    file = tmp_path / "input.png"
    file.write_bytes(b"image")
    client = CanvasClient(runner, ACCOUNT)
    context = client.prepare(request(), "ijm_test", {"images": [{"path": str(file)}], "videos": [], "audios": []},
                             persist=lambda value: runner.persisted.append(value))
    return runner, client, context


def test_isolated_environments_and_invalidation(tmp_path, monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://wrong:secret@ambient:9999")
    monkeypatch.setenv("ALL_PROXY", "http://ambient:9999")
    monkeypatch.setenv("NO_PROXY", "*")
    monkeypatch.setenv("OPENAI_API_KEY", "do-not-inherit")
    runner = CanvasCLI(tmp_path)
    first = runner.environment(ACCOUNT)
    other = runner.environment({**ACCOUNT, "id": 6})
    assert first["HOME"] != other["HOME"]
    assert first["DREAMINA_CANVAS_STATE_DIR"] != other["DREAMINA_CANVAS_STATE_DIR"]
    assert first["HTTPS_PROXY"] == "socks5h://user:secret@127.0.0.1:20005"
    assert first["NO_PROXY"] != "*"
    assert "ALL_PROXY" not in first and "OPENAI_API_KEY" not in first
    profile = runner.profile(ACCOUNT)
    (runner.account_directory(ACCOUNT) / "old-business.json").write_text("keep me")
    runner.invalidate(5)
    assert runner.profile(ACCOUNT) != profile
    assert list((tmp_path / "canvas-cli").glob("retired-account-5-*/old-business.json"))


def patch_process(monkeypatch, program):
    actual = subprocess.Popen
    def popen(command, **kwargs):
        return actual([sys.executable, "-c", program], **kwargs)
    monkeypatch.setattr("app.canvas_cli.subprocess.Popen", popen)


def test_runner_reads_error_json_from_stderr_and_redacts(monkeypatch, tmp_path):
    patch_process(monkeypatch, "import sys,json;sys.stderr.write(json.dumps({'ok':False,'error':{'code':'bad','message':'https://user:pass@example.test/path?token=secret','accessToken':'secret'}}));sys.exit(21)")
    with pytest.raises(CanvasCLIError) as found:
        CanvasCLI(tmp_path).run(ACCOUNT, ["node", "run"])
    assert found.value.outcome_unknown
    assert "secret" not in str(found.value) and "pass@" not in str(found.value)
    assert found.value.details["error"]["accessToken"] == "[redacted]"


@pytest.mark.parametrize("error,exit_code,unknown", [
    ({"code": "cli.api_failed", "class": "service_error", "retryable": True}, 21, True),
    ({"code": "cli.authentication_required", "class": "auth_required"}, 11, False),
    ({"code": "cli.invalid_input", "class": "invalid_input"}, 2, False),
    ({"code": "cli.operation_incomplete", "class": "operation_incomplete", "requiredAction": "resume"}, 20, True),
    ({"code": "cli.service_rejected", "reason": "insufficient_credits", "message": "Not enough credits left"}, 22, False),
    ({"code": "cli.service_error", "reason": "insufficient_credits", "requiredAction": "resume"}, 20, True),
])
def test_run_outcome_is_conservative(monkeypatch, tmp_path, error, exit_code, unknown):
    value = json.dumps({"ok": False, "error": error})
    patch_process(monkeypatch, f"import sys;sys.stderr.write({value!r});sys.exit({exit_code})")
    with pytest.raises(CanvasCLIError) as found:
        CanvasCLI(tmp_path).run(ACCOUNT, ["node", "run"])
    assert found.value.outcome_unknown is unknown


def test_runner_output_limit(monkeypatch, tmp_path):
    patch_process(monkeypatch, "import sys;sys.stdout.write('x'*1000000)")
    with pytest.raises(CanvasCLIError) as found:
        CanvasCLI(tmp_path, max_output_bytes=1024).run(ACCOUNT, ["node", "run"])
    assert found.value.code == "CLI_OUTPUT_LIMIT" and found.value.outcome_unknown


def test_prepare_upload_timeout_checks_same_resource_before_continue(tmp_path):
    runner, client, context = prepare(tmp_path, upload_timeout=True)
    assert context["quoted_credits"] == 20
    upload = next(x for x in runner.calls if x[:2] == ["resource", "upload"])
    checks = [x for x in runner.calls if x[:2] == ["resource", "get"]]
    assert checks and all(x[2] == upload[-1] for x in checks)
    assert not any(x[:2] in [["node", "confirm"], ["node", "run"]] for x in runner.calls)
    assert all("never-save-this-token" not in json.dumps(x) for x in runner.persisted)


def test_submit_query_download_preserves_original_and_precharge(tmp_path):
    runner, client, context = prepare(tmp_path)
    accepted = client.submit(context, 20)
    result = client.poll(accepted)
    assert result["status"] == "succeeded"
    assert Path(result["result_paths"][0]).read_bytes().endswith(b"original-bytes")
    assert result["result_files"][0]["mime"] == "video/mp4"
    assert result["credit_facts"][0]["source"] == "precharge"
    assert result["credit_facts"][0]["settled"] is False
    assert "actual_cost_credits" not in result
    assert sum(x[:2] == ["node", "run"] for x in runner.calls) == 1
    with pytest.raises(CanvasCLIError) as found:
        client.submit(accepted, 20)
    assert found.value.outcome_unknown


def test_insufficient_ceiling_never_confirms_or_runs(tmp_path):
    runner, client, context = prepare(tmp_path)
    with pytest.raises(CanvasCLIError):
        client.submit(context, 19)
    assert not any(x[:2] == ["node", "confirm"] for x in runner.calls)


def test_unknown_query_never_replays_submission(tmp_path):
    runner, client, context = prepare(tmp_path)
    runner.state = "absent"
    assert client.poll(context)["status"] == "unknown"
    assert not any(x[:2] == ["node", "run"] for x in runner.calls)


def test_image_canonical_mapping_is_exact_and_live():
    normalized = {"kind": "image", "model": "gpt-image-2.5-flare", "upstream_model": "dreamina_gpt_image_2_5_flare",
                  "resolution": "2k", "aspect_ratio": "1:1", "count": 1}
    selected = select_model([model("image")], normalized)
    mode = select_mode(selected, normalized, {"images": 1, "videos": 0, "audios": 0})
    assert mode["name"] == "i2i"
    assert "2K" in generation_flags(mode, normalized)
    with pytest.raises(CanvasCLIError) as found:
        select_model([model()], normalized)
    assert found.value.code == "CLI_CAPABILITY_UNAVAILABLE"
    assert select_model([model("image")], {**normalized, "model": "gpt-image-2.5-flare-2k"})["model"] == "gpt_image_2_5_flare"


def test_model_limits_prevent_upload_and_prompt_indices_remain_text():
    with pytest.raises(CanvasCLIError):
        select_mode(model(), request(), {"images": 10, "videos": 0, "audios": 0})
    text = prompt_with_resources("图片1 Image1 video1 Audio1 图片26", {
        "images": ["one"], "videos": ["two"], "audios": ["three"]})
    assert text == "{{res:one}} {{res:one}} {{res:two}} {{res:three}} 图片26"


def test_video_download_cannot_accept_a_thumbnail(tmp_path):
    path = tmp_path / "result.mp4"
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"a-cover-image")
    with pytest.raises(CanvasCLIError) as found:
        _file_mime(path, "video")
    assert found.value.code == "CLI_RESULT_INVALID"


def test_completed_image_batch_requires_all_requested_outputs(tmp_path):
    runner, client, context = prepare(tmp_path)
    context["output_count"] = 4
    state = client.poll(context)
    assert state["status"] == "failed" and state["error_code"] == "CLI_RESULT_INCOMPLETE"
    assert not any(args[:2] == ["resource", "download"] for args in runner.calls)


def test_partial_failed_operation_is_terminal_even_without_failed_resource(tmp_path):
    runner, client, context = prepare(tmp_path)
    runner.state = "partial_failed"
    assert client.poll(context)["status"] == "failed"


def test_partial_acceptance_keeps_unknown_even_with_rejection_reason(monkeypatch, tmp_path):
    value = json.dumps({"ok": False, "error": {"code": "cli.invalid_input", "class": "invalid_input"},
                        "partialData": {"items": [{"state": "accepted", "resources": [{"resourceId": "one"}]}]}})
    patch_process(monkeypatch, f"import sys;sys.stderr.write({value!r});sys.exit(2)")
    with pytest.raises(CanvasCLIError) as found:
        CanvasCLI(tmp_path).run(ACCOUNT, ["node", "run"])
    assert found.value.outcome_unknown


@pytest.mark.parametrize("upstream,normalized", [
    (12009, "CONTENT_MODERATION_FAILED"), (12011, "CONTENT_MODERATION_FAILED"),
    (2052, "CONTENT_MODERATION_FAILED"), (2059, "UPSTREAM_REVIEW_FAILED"),
    (1064, "UPSTREAM_REVIEW_SERVICE_ERROR"), (2041, "UPSTREAM_IMAGE_REVIEW_FAILED"),
])
def test_terminal_resource_error_preserves_policy_category(tmp_path, upstream, normalized):
    runner, client, context = prepare(tmp_path)
    runner.state = "failed"
    runner.resource_error_code = upstream
    result = client.poll(context)
    assert result["error_code"] == normalized and str(upstream) in result["error_message"]
    assert result["credit_facts"][0]["settled"] is False


def test_failed_diagnostic_does_not_hide_terminal_generation_failure(tmp_path, monkeypatch):
    runner, client, context = prepare(tmp_path)
    runner.state = "failed"
    runner.diagnostic_failure = True
    monkeypatch.setattr("app.canvas_client.time.sleep", lambda _: None)
    assert client.poll(context)["status"] == "failed"


def test_runtime_probe_rejects_wrong_distribution(tmp_path, monkeypatch):
    runner = CanvasCLI(tmp_path)
    monkeypatch.setattr(runner, "run", lambda *a, **k: {"data": {
        "version": "1.2.0", "distribution": "cn", "edition": "public"}})
    with pytest.raises(CanvasCLIError) as found:
        runner.verify_runtime()
    assert found.value.code == "CLI_RUNTIME_INCOMPATIBLE"
