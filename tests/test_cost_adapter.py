"""Billing reference contracts: estimates, privacy, dimensions, and failure debits."""
from app.cost_adapter import SERVICE, build_observations, billing_evidence, input_evidence, number


def task(**changes):
    return {"id": "task-1", "generation_id": "upstream-1", "status": "failed",
            "model": "video-model", "request": {"model": "video-model", "duration": 5,
                "resolution": "720p", "_videos": [], "prompt": "private prompt"},
            "estimated_cost": 9.5, "updated_at": 123, **changes}


def test_video_dimensions_and_privacy():
    item = build_observations(task(upstream_request={"uploads": [
        {"kind": "video", "duration_ms": 3456, "url": "https://private/video"}]}))[0]
    assert item["reference_seconds"] == 3.456
    assert item["requested_reference_seconds"] == 0
    assert item["duration"] == 5
    assert item["channel"] == SERVICE
    assert "private" not in str({key: value for key, value in item.items() if not key.startswith("_")})
    assert "url" not in str(item["billing_context"])


def test_unmeasured_reference_is_unknown_not_zero():
    value = task()
    value["request"]["_videos"] = [{"value": "https://private/video"}]
    item = build_observations(value)[0]
    assert item["reference_seconds"] is None
    assert item["requested_reference_seconds"] is None


def test_invalid_numeric_values_and_real_zero():
    assert number(False) is None
    assert number("NaN") is None
    assert number("Infinity") is None
    assert number(-1) is None
    assert number("0") == 0
    assert input_evidence({"videos": [{"duration": 1.25}, {"duration": 2}]})["reference_seconds"] == 3.25


def test_default_estimated_zero_is_unknown():
    item = build_observations(task(estimated_cost=0))[0]
    assert item["charged"] is None
    assert item["quoted"] is None
    assert item["source"] == "unknown"


def test_only_video_tasks():
    assert build_observations(task(normalized={"kind": "image"})) == []


def test_failed_confirmed_balance_debit_is_retained():
    item = build_observations(task(actual_cost=2.75, _cost_balance_isolated=True))[0]
    assert item["charged"] == 2.75
    assert item["source"] == "isolated_balance"
    assert item["status"] == "failed"


def test_persisted_effective_reference_metadata():
    item = build_observations(task(normalized={"duration": 6, "resolution": "1080p",
        "videos": ["https://private/a"], "_cost_evidence": {"reference_seconds": 1.5}}))[0]
    assert item["reference_seconds"] == 1.5
    assert item["duration"] == 6


def test_legacy_balance_cost_requires_isolation_evidence():
    assert build_observations(task(actual_cost=200))[0]["charged"] is None
    assert build_observations(task(actual_cost=200, _cost_balance_isolated=False))[0]["charged"] is None
    assert build_observations(task(actual_cost=0, _cost_balance_isolated=True))[0]["charged"] == 0
