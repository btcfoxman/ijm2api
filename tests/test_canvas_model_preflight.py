"""Account model availability is distinct from unsupported generation settings."""

import copy
from types import SimpleNamespace

import pytest

from app.canvas_cli import CanvasCLIError
from app.canvas_client import CanvasClient, select_model
from test_canvas_transport import ACCOUNT, FakeRunner, model, request


def marker(error):
    return error.details.get("error", {})


@pytest.mark.parametrize("entrypoint", ["preflight_model", "prepare"])
@pytest.mark.parametrize("kind,public", [("video", "sd-2-0-fast"), ("image", "unavailable-image")])
def test_valid_catalog_missing_model_is_account_scoped_and_read_only(tmp_path, entrypoint, kind, public):
    runner = FakeRunner(tmp_path)
    client = CanvasClient(runner, ACCOUNT)
    normalized = {**request(), "kind": kind, "model": public}
    before, account_before = copy.deepcopy(normalized), copy.deepcopy(ACCOUNT)
    with pytest.raises(CanvasCLIError) as found:
        if entrypoint == "preflight_model":
            client.preflight_model(normalized)
        else:
            client.prepare(normalized, "original-task", {"images": [], "audios": [], "videos": []})
    assert found.value.code == "CLI_CAPABILITY_UNAVAILABLE"
    assert marker(found.value) == {"scope": "account", "reason": "model_not_in_catalog",
                                   "requested_model": public, "kind": kind}
    assert runner.calls == [["model", "list", "--type", kind]]
    assert not runner.persisted
    assert normalized == before and client.account == account_before and ACCOUNT == account_before


def test_preflight_and_prepare_reuse_catalog_and_preserve_selected_alias(tmp_path):
    runner = FakeRunner(tmp_path)
    original = runner.run
    advertised = model()
    advertised["model"] = "seedance_2.0_fast_vip"
    advertised["aliases"] = ["sd-2-0-fast"]

    def run(account, args, **kwargs):
        result = original(account, args, **kwargs)
        if args[:2] == ["model", "list"]:
            result["data"]["items"] = [advertised]
        return result

    runner.run = run
    client = CanvasClient(runner, ACCOUNT)
    normalized = {**request(), "model": "sd-2-0-fast"}
    assert client.preflight_model(normalized) is advertised
    assert runner.calls == [["model", "list", "--type", "video"]]
    source = tmp_path / "image.png"
    source.write_bytes(b"image")
    context = client.prepare(normalized, "same-task", {"images": [source], "videos": [], "audios": []},
                             persist=runner.persisted.append)
    assert sum(args[:2] == ["model", "list"] for args in runner.calls) == 1
    assert context["model"] == "seedance_2.0_fast_vip"
    create = next(args for args in runner.calls if args[:3] == ["node", "create", "video"])
    assert create[create.index("--model") + 1] == "seedance_2.0_fast_vip"
    assert not any(args[:2] in (["node", "confirm"], ["node", "run"]) for args in runner.calls)


def test_ambiguous_model_is_not_an_account_model_absence():
    with pytest.raises(CanvasCLIError) as found:
        select_model([model(), model()], request())
    assert found.value.code == "CLI_CAPABILITY_UNAVAILABLE"
    assert not marker(found.value)


@pytest.mark.parametrize("condition", ["mode", "resolution", "duration"])
def test_existing_model_unsupported_settings_have_no_account_absence_marker(tmp_path, condition):
    catalog = model()
    normalized = request()
    if condition == "mode":
        catalog["modes"] = [mode for mode in catalog["modes"] if mode["name"] != "t2v"]
    elif condition == "resolution":
        normalized["resolution"] = "unsupported-resolution"
    else:
        normalized["duration"] = 100
    runner = FakeRunner(tmp_path)
    client = CanvasClient(runner, ACCOUNT, capabilities={"video": [catalog]})
    assert client.preflight_model(normalized) == catalog
    with pytest.raises(CanvasCLIError) as found:
        client.prepare(normalized, "same-task", {"images": [], "audios": [], "videos": []})
    assert found.value.code == "CLI_CAPABILITY_UNAVAILABLE"
    assert not marker(found.value)
    assert not runner.calls


@pytest.mark.parametrize("items", [None, {}, [None], [{}], [{"model": 123}], [{"model": " "}]])
def test_invalid_catalog_is_not_eligible_for_account_scoped_model_skip(tmp_path, items):
    runner = FakeRunner(tmp_path)

    def run(account, args, **kwargs):
        runner.calls.append(args)
        return {"ok": True, "data": {"items": items}}

    runner.run = run
    with pytest.raises(CanvasCLIError) as found:
        CanvasClient(runner, ACCOUNT).preflight_model({**request(), "model": "sd-2-0-fast"})
    assert found.value.code == "CLI_CAPABILITIES_INVALID"
    assert not marker(found.value)
    assert runner.calls == [["model", "list", "--type", "video"]]


def test_empty_valid_catalog_can_report_account_model_absence(tmp_path):
    client = CanvasClient(FakeRunner(tmp_path), ACCOUNT, capabilities={"video": []})
    with pytest.raises(CanvasCLIError) as found:
        client.preflight_model(request())
    assert marker(found.value)["reason"] == "model_not_in_catalog"


def test_catalog_authentication_failure_is_not_account_model_absence(tmp_path):
    runner = FakeRunner(tmp_path)

    def run(account, args, **kwargs):
        runner.calls.append(args)
        raise CanvasCLIError("cli.authentication_required", "Authentication is required")

    runner.run = run
    with pytest.raises(CanvasCLIError) as found:
        CanvasClient(runner, ACCOUNT).preflight_model(request())
    assert found.value.code == "cli.authentication_required"
    assert not marker(found.value)
    assert runner.calls == [["model", "list", "--type", "video"]]


def test_cached_preflight_still_respects_original_task_deadline(tmp_path, monkeypatch):
    monkeypatch.setattr("app.canvas_client.time", SimpleNamespace(monotonic=lambda: 10))
    runner = FakeRunner(tmp_path)
    client = CanvasClient(runner, ACCOUNT, capabilities={"video": [model()]})
    with pytest.raises(CanvasCLIError) as found:
        client.preflight_model(request(), deadline=9)
    assert found.value.code == "CLI_TASK_DEADLINE_EXCEEDED"
    assert not marker(found.value)
    assert not runner.calls
