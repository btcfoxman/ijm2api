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


def test_jimeng_quote_zero_is_evidence_not_a_charge_and_uses_the_real_provider_job():
    value = task(execution_backend='jimeng_web', generation_id=None, account_id=7, run_id='original-submit',
                 upstream_context={'submit_id': 'original-submit', 'upstream_task_id': '49769566645772'},
                 normalized={'_cost_evidence': {'jimeng_quote': {'source': 'native_get_need_credits', 'amount': 0,
                     'account_id': 7, 'submit_id': 'original-submit'}}})
    result = build_observations(value)[0]
    assert result['quoted'] == 0 and result['source'] == 'quote'
    assert result['charged'] is None and result['refunded'] is None
    assert result['upstream_task_id'] == '49769566645772'
    assert result['attempt_id'] == 'original-submit'
    value['run_id'] = None
    prepared = build_observations(value)[0]
    assert prepared['source'] == 'quote' and prepared['quoted'] == 0 and prepared['charged'] is None
    value['run_id'] = 'original-submit'
    value['normalized']['_cost_evidence']['jimeng_quote']['submit_id'] = 'another-submit'
    assert build_observations(value)[0]['source'] == 'formula'
    value['upstream_context']['submit_id'] = 'another-submit'
    assert build_observations(value)[0]['source'] == 'formula'


def test_jimeng_without_a_provider_job_does_not_export_its_local_uuid_as_one():
    value = task(execution_backend='jimeng_web', generation_id=None, run_id='allocated-submit',
                 upstream_context={'submit_id': 'allocated-submit'})
    result = build_observations(value)[0]
    assert result['upstream_task_id'] == ''
    assert result['attempt_id'] == 'allocated-submit'


def test_jimeng_model_comes_from_its_bound_native_quote_instead_of_international_alias():
    value = task(execution_backend='jimeng_web', account_id=7, run_id='original-submit',
                 upstream_context={'submit_id': 'original-submit'}, normalized={
                     'upstream_model': 'dreamina_seedance_40', '_cost_evidence': {'jimeng_quote': {
                         'source': 'native_get_need_credits', 'amount': 24, 'account_id': 7,
                         'submit_id': 'original-submit', 'bound': {'model_req_key': 'dreamina_seedance_40_vision'}}}})
    assert build_observations(value)[0]['upstream_model'] == 'dreamina_seedance_40_vision'
    value['run_id'] = 'different-submit'
    assert build_observations(value)[0]['upstream_model'] == 'dreamina_seedance_40'
