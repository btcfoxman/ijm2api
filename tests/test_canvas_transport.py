from __future__ import annotations

import json
import hashlib
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
            data = {"resourceId": args[2], "path": str(output), "size": output.stat().st_size,
                    "sha256": hashlib.sha256(output.read_bytes()).hexdigest()}
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
    runner = CanvasCLI(tmp_path, allow_unwrapped_for_tests=True)
    first = runner.environment(ACCOUNT)
    other = runner.environment({**ACCOUNT, "id": 6})
    assert first["HOME"] != other["HOME"]
    assert first["DREAMINA_CANVAS_STATE_DIR"] != other["DREAMINA_CANVAS_STATE_DIR"]
    assert "HTTPS_PROXY" not in first and "HTTP_PROXY" not in first
    assert "NO_PROXY" not in first and "http_proxy" not in first
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
        CanvasCLI(tmp_path, allow_unwrapped_for_tests=True).run(ACCOUNT, ["node", "run"])
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
        CanvasCLI(tmp_path, allow_unwrapped_for_tests=True).run(ACCOUNT, ["node", "run"])
    assert found.value.outcome_unknown is unknown


def test_runner_output_limit(monkeypatch, tmp_path):
    patch_process(monkeypatch, "import sys;sys.stdout.write('x'*1000000)")
    with pytest.raises(CanvasCLIError) as found:
        CanvasCLI(tmp_path, max_output_bytes=1024, allow_unwrapped_for_tests=True).run(ACCOUNT, ["node", "run"])
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
        CanvasCLI(tmp_path, allow_unwrapped_for_tests=True).run(ACCOUNT, ["node", "run"])
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
    runner = CanvasCLI(tmp_path, allow_unwrapped_for_tests=True)
    monkeypatch.setattr(runner, "run", lambda *a, **k: {"data": {
        "version": "1.2.0", "distribution": "cn", "edition": "public"}})
    with pytest.raises(CanvasCLIError) as found:
        runner.verify_runtime()
    assert found.value.code == "CLI_RUNTIME_INCOMPATIBLE"


def test_real_image_catalog_null_aliases_keeps_exact_existing_model():
    flare = {"model": "dreamina_gpt_image_2_5_flare", "aliases": None, "displayName": "GPT Image 2.5 Flare"}
    sunburst = {"model": "dreamina_gpt_image_2_5_sunburst", "aliases": None, "displayName": "GPT Image 2.5 Sunburst"}
    for public, canonical in (("gpt-image-2.5-flare", flare["model"]), ("gpt-image-2.5-sunburst-4k", sunburst["model"])):
        selected = select_model([flare, sunburst], {"kind": "image", "model": public, "upstream_model": canonical})
        assert selected["model"] == canonical


def real_frame_catalog():
    result = model()
    result["modes"] = [{"name": "first_last_frame", "flags": [], "references": {
        "min": n, "max": n, "types": [{"mediaType": "image", "min": n, "max": n}]}}
        for n in (2, 1)] + [mode for mode in result["modes"] if mode["name"] != "first_last_frame"]
    return result


@pytest.mark.parametrize("image_count", [1, 2])
def test_real_duplicate_frame_modes_resolve_by_reference_constraints(image_count):
    selected = select_mode(real_frame_catalog(), {**request(), "mode": "first_last_frame"},
                           {"images": image_count, "videos": 0, "audios": 0})
    assert selected["references"]["min"] == selected["references"]["max"] == image_count


@pytest.mark.parametrize("counts", [
    {"images": 0, "videos": 0, "audios": 0}, {"images": 3, "videos": 0, "audios": 0},
    {"images": 1, "videos": 1, "audios": 0}, {"images": 1, "videos": 0, "audios": 1},
])
def test_explicit_frame_mode_does_not_fall_back_to_m2v(counts):
    with pytest.raises(CanvasCLIError) as found:
        select_mode(real_frame_catalog(), {**request(), "mode": "i2v"}, counts)
    assert found.value.code == "CLI_CAPABILITY_UNAVAILABLE"


def test_ambiguous_frame_constraints_are_not_resolved_by_first_entry():
    catalog = real_frame_catalog()
    catalog["modes"].append(dict(catalog["modes"][0]))
    with pytest.raises(CanvasCLIError):
        select_mode(catalog, {**request(), "mode": "first_last_frame"}, {"images": 2, "videos": 0, "audios": 0})


class IdentityRunner:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def run(self, account, args, timeout=None):
        self.calls.append(args)
        assert args == ["auth", "account"]
        return {"ok": True, "data": self.responses[min(len(self.calls) - 1, len(self.responses) - 1)]}


def test_real_identity_partial_credits_read_is_retried_without_login(monkeypatch):
    monkeypatch.setattr("app.canvas_client.time.sleep", lambda _: None)
    # Observed live: a successful identity response omitted all optional credit
    # fields, while the next independent read returned the aggregate normally.
    runner = IdentityRunner([{"userId": 42, "isVip": True, "vipLevel": "basic"},
        {"userId": 42, "availableCredits": "47385", "credits": {"available": "47385",
            "details": [{"available": "47385", "expiresAt": "2026-11-27T11:25:25Z"}]}}])
    identity = CanvasClient(runner, ACCOUNT).identity()
    assert identity["available_balance"] == 47385 and len(runner.calls) == 2


def test_missing_credits_stays_unknown_after_bounded_read_retries(monkeypatch):
    monkeypatch.setattr("app.canvas_client.time.sleep", lambda _: None)
    runner = IdentityRunner([{"userId": 42, "isVip": True, "vipLevel": "basic"}])
    assert CanvasClient(runner, ACCOUNT).identity()["available_balance"] is None
    assert len(runner.calls) == 3


@pytest.mark.parametrize("fields,expected", [
    ({"availableCredits": "8", "credits": {"available": None}}, 8),
    ({"availableCredits": "0", "credits": {"available": "0"}}, 0),
    ({"availableCredits": "240", "credits": {"available": "225"}}, None),
    ({"credits": {"details": [{"available": "8"}]}}, None),
])
def test_identity_only_uses_consistent_explicit_aggregate_balance(monkeypatch, fields, expected):
    monkeypatch.setattr("app.canvas_client.time.sleep", lambda _: None)
    identity = CanvasClient(IdentityRunner([{"userId": 42, **fields}]), ACCOUNT).identity()
    assert identity["available_balance"] == expected


def test_protected_command_uses_private_account_proxy_config_and_cleans_it(tmp_path, monkeypatch):
    monkeypatch.setattr("app.canvas_cli.sys.platform", "linux")
    actual = subprocess.Popen
    captured = {}
    def protected_popen(command, **kwargs):
        captured["command"] = command
        captured["env"] = kwargs["env"]
        config = Path(command[2])
        captured["config"] = config
        captured["text"] = config.read_text()
        if os.name != "nt":
            assert config.stat().st_mode & 0o777 == 0o600
            assert config.parent.stat().st_mode & 0o777 == 0o700
        return actual([sys.executable, "-c", "print('{\"ok\":true,\"data\":{}}')"], **kwargs)
    monkeypatch.setattr("app.canvas_cli.subprocess.Popen", protected_popen)
    runner = CanvasCLI(tmp_path, proxy_binary="/pinned/graftcp")
    runner.run(ACCOUNT, ["auth", "status"])
    assert captured["command"][:2] == ["/pinned/graftcp", "--config"]
    assert captured["command"][3:5] == ["--", "dreamina-canvas"]
    assert "secret" not in " ".join(captured["command"])
    assert not any("proxy" in key.lower() for key in captured["env"])
    assert "select_proxy_mode = only_socks5" in captured["text"]
    assert "socks5 = 127.0.0.1:20005" in captured["text"]
    assert "socks5_password = secret" in captured["text"]
    assert "dns_proxy = true" in captured["text"] and "udp_proxy = true" in captured["text"]
    assert "ignore_local = false" in captured["text"]
    assert not captured["config"].exists()


def test_missing_proxy_wrapper_never_runs_bare_cli(tmp_path, monkeypatch):
    monkeypatch.setattr("app.canvas_cli.sys.platform", "linux")
    commands = []
    def unavailable(command, **kwargs):
        commands.append(command)
        raise FileNotFoundError("not installed")
    monkeypatch.setattr("app.canvas_cli.subprocess.Popen", unavailable)
    runner = CanvasCLI(tmp_path, proxy_binary="/missing/graftcp")
    with pytest.raises(CanvasCLIError) as found:
        runner.run(ACCOUNT, ["auth", "status"])
    assert found.value.code == "CLI_PROXY_WRAPPER_UNAVAILABLE"
    assert len(commands) == 1 and commands[0][0] == "/missing/graftcp"
    assert not list(runner.account_directory(ACCOUNT).glob("proxy/*.conf"))


def test_proxy_configs_are_per_account_and_http_has_no_direct_fallback(tmp_path):
    runner = CanvasCLI(tmp_path)
    first = runner._proxy_config(ACCOUNT)
    other = runner._proxy_config({**ACCOUNT, "id": 6, "proxy_url": "http://127.0.0.1:20006"})
    try:
        assert first.parent != other.parent
        assert "only_socks5" in first.read_text() and "only_http_proxy" in other.read_text()
        assert "http_proxy = 127.0.0.1:20006" in other.read_text()
        assert "secret" not in other.read_text()
        assert " = direct" not in other.read_text() and " = auto" not in other.read_text()
    finally:
        first.unlink()
        other.unlink()


@pytest.mark.parametrize("url", [
    "socks5://user:secret%0Aselect_proxy_mode%3Ddirect@127.0.0.1:20005",
    "socks5://user:%20secret@127.0.0.1:20005",
    "http://user:secret@127.0.0.1:20005",
    "https://127.0.0.1:20005",
])
def test_unsupported_proxy_input_cannot_change_routing_or_drop_auth(tmp_path, url):
    runner = CanvasCLI(tmp_path)
    with pytest.raises(CanvasCLIError):
        runner._proxy_config({**ACCOUNT, "proxy_url": url})
    assert not list(runner.account_directory(ACCOUNT).glob("proxy/*.conf"))


def test_invalid_service_response_retries_only_read_calls(tmp_path, monkeypatch):
    monkeypatch.setattr("app.canvas_client.time.sleep", lambda _: None)
    runner = FakeRunner(tmp_path)
    original = runner.run
    failures = 2
    def interrupted(account, args, timeout=None):
        nonlocal failures
        if args[:2] == ["model", "list"] and failures:
            runner.calls.append(args)
            failures -= 1
            raise CanvasCLIError("cli.invalid_service_response", "The service returned an invalid response")
        return original(account, args, timeout=timeout)
    runner.run = interrupted
    assert CanvasClient(runner, ACCOUNT).capabilities()[0]["model"] == "seedance_2.0_mini"
    assert len(runner.calls) == 3
    assert all(args[:2] == ["model", "list"] for args in runner.calls)


def test_invalid_upload_response_looks_up_then_retries_original_id(tmp_path, monkeypatch):
    monkeypatch.setattr("app.canvas_client.time.sleep", lambda _: None)
    runner = FakeRunner(tmp_path)
    original = runner.run
    first = True
    lookup_missing = False
    def interrupted(account, args, timeout=None):
        nonlocal first, lookup_missing
        if args[:2] == ["resource", "upload"] and first:
            first, lookup_missing = False, True
            runner.calls.append(args)
            raise CanvasCLIError("cli.invalid_service_response", "The service returned an invalid response")
        if args[:2] == ["resource", "get"] and lookup_missing:
            lookup_missing = False
            runner.calls.append(args)
            raise CanvasCLIError("cli.resource_unavailable", "Resource not registered")
        return original(account, args, timeout=timeout)
    runner.run = interrupted
    source = tmp_path / "input.png"
    source.write_bytes(b"image")
    context = CanvasClient(runner, ACCOUNT).prepare(request(), "ijm_retry_upload",
        {"images": [str(source)], "videos": [], "audios": []}, persist=runner.persisted.append)
    uploads = [(index, args) for index, args in enumerate(runner.calls) if args[:2] == ["resource", "upload"]]
    assert len(uploads) == 2 and uploads[0][1] == uploads[1][1]
    resource_id = uploads[0][1][-1]
    assert runner.calls[uploads[0][0] + 1][2] == resource_id
    assert context["uploaded_resources"] == [resource_id]
    assert not any(args[:2] == ["node", "run"] for args in runner.calls)


def test_invalid_confirmation_response_retries_tokens_then_runs_once(tmp_path, monkeypatch):
    monkeypatch.setattr("app.canvas_client.time.sleep", lambda _: None)
    runner, client, context = prepare(tmp_path)
    original = runner.run
    failures = 2
    def interrupted(account, args, timeout=None):
        nonlocal failures
        if args[:2] == ["node", "confirm"] and failures:
            runner.calls.append(args)
            failures -= 1
            raise CanvasCLIError("cli.invalid_service_response", "The service returned an invalid response")
        return original(account, args, timeout=timeout)
    runner.run = interrupted
    accepted = client.submit(context, 20)
    assert accepted["phase"] == "accepted"
    confirms = [args for args in runner.calls if args[:2] == ["node", "confirm"]]
    assert len(confirms) == 3 and confirms[0] == confirms[1] == confirms[2]
    assert sum(args[:2] == ["node", "run"] for args in runner.calls) == 1


def test_invalid_paid_run_response_is_not_replayed(tmp_path):
    runner, client, context = prepare(tmp_path)
    runner.run_failure = CanvasCLIError("cli.invalid_service_response", "The service returned an invalid response",
                                       outcome_unknown=True)
    with pytest.raises(CanvasCLIError) as found:
        client.submit(context, 20)
    assert found.value.outcome_unknown
    assert sum(args[:2] == ["node", "confirm"] for args in runner.calls) == 1
    assert sum(args[:2] == ["node", "run"] for args in runner.calls) == 1


def test_old_magic_valid_half_video_is_not_reused(tmp_path):
    runner, client, context = prepare(tmp_path)
    resource_id = "11111111-1111-4111-8111-111111111111"
    base = tmp_path / "results" / context["task_id"]
    base.mkdir(parents=True)
    final = base / (resource_id + ".mp4")
    final.write_bytes(b"\x00\x00\x00\x20ftypisom" + b"truncated-old-video")
    result = client.download_result(context, resource_id, "video")
    assert Path(result["path"]).read_bytes().endswith(b"original-bytes")
    assert sum(args[:2] == ["resource", "download"] for args in runner.calls) == 1
    assert not list(base.glob("*.part"))


def test_timed_out_partial_download_is_removed_before_same_resource_retry(tmp_path, monkeypatch):
    monkeypatch.setattr("app.canvas_client.time.sleep", lambda _: None)
    runner, client, context = prepare(tmp_path)
    original = runner.run
    attempted = []
    def interrupted(account, args, timeout=None):
        if args[:2] == ["resource", "download"]:
            output = Path(args[-1])
            assert not output.exists()
            attempted.append((args[2], output))
            if len(attempted) == 1:
                output.write_bytes(b"\x00\x00\x00\x20ftypisom" + b"half-download")
                raise CanvasCLIError("CLI_COMMAND_TIMEOUT", "download timed out")
            assert not attempted[0][1].exists()
        return original(account, args, timeout=timeout)
    runner.run = interrupted
    result = client.download_result(context, "11111111-1111-4111-8111-111111111111", "video")
    assert len(attempted) == 2 and attempted[0][0] == attempted[1][0]
    assert attempted[0][1] != attempted[1][1]
    assert Path(result["path"]).read_bytes().endswith(b"original-bytes")
    assert not any(path.exists() for _, path in attempted)


@pytest.mark.parametrize("invalid", ["missing_checksum", "wrong_checksum", "wrong_size"])
def test_download_cannot_publish_without_matching_receipt(tmp_path, invalid):
    runner, client, context = prepare(tmp_path)
    original = runner.run
    def corrupt(account, args, timeout=None):
        value = original(account, args, timeout=timeout)
        if args[:2] == ["resource", "download"]:
            if invalid == "missing_checksum":
                value["data"].pop("sha256")
            elif invalid == "wrong_checksum":
                value["data"]["sha256"] = "0" * 64
            else:
                value["data"]["size"] += 1
        return value
    runner.run = corrupt
    resource_id = "11111111-1111-4111-8111-111111111111"
    with pytest.raises(CanvasCLIError):
        client.download_result(context, resource_id, "video")
    base = tmp_path / "results" / context["task_id"]
    assert not (base / (resource_id + ".mp4")).exists()
    assert not list(base.glob("*.part"))
