"""Pure, account-specific admission rules; never apply to submitted task polling."""

from __future__ import annotations

from typing import Any

from .catalog import MODELS
from .image_client import IMAGE_MODELS, image_model_resolutions


REGIONS = ("cn", "us", "other")
DEFAULT_REGION_GENERATION_POLICY = {
    "cn": {"enabled": True, "models": {}},
    "us": {"enabled": False, "models": {}},
    "other": {"enabled": False, "models": {}},
}


def normalize_region(value: Any, *, default: str = "other") -> str:
    region = default if value is None else value
    if not isinstance(region, str) or region not in REGIONS:
        raise ValueError("account region must be cn, us or other")
    return region


def account_requires_proxy(account: dict[str, Any]) -> bool:
    """Only the explicit Jimeng web transport can use a direct connection."""
    return account.get("execution_mode") != "jimeng_web"


def _image_aliases() -> set[str]:
    return {alias for name, upstream in IMAGE_MODELS.items()
            for alias in (name, *(name + "-" + tier for tier in image_model_resolutions(upstream)))}


def validate_region_generation_policy(value: Any) -> dict[str, dict[str, Any]]:
    """Validate the whole replacement before settings are written."""
    if not isinstance(value, dict) or set(value) != set(REGIONS):
        raise ValueError("region_generation_policy must contain exactly cn, us and other")
    image_aliases = _image_aliases()
    result = {}
    for region in REGIONS:
        entry = value[region]
        if (not isinstance(entry, dict) or set(entry) != {"enabled", "models"}
                or type(entry["enabled"]) is not bool or not isinstance(entry["models"], dict)):
            raise ValueError(f"invalid generation policy for {region}; expected enabled and models")
        models = {}
        for model, rule in entry["models"].items():
            if not isinstance(model, str) or model not in MODELS and model not in image_aliases:
                raise ValueError(f"unknown public model alias in {region} policy: {model}")
            if not isinstance(rule, dict):
                raise ValueError(f"invalid generation rule for {region}/{model}")
            if model in image_aliases:
                if rule:
                    raise ValueError(f"image generation rule for {region}/{model} must be empty")
                models[model] = {}
                continue
            if (set(rule) != {"min_duration", "max_duration"}
                    or type(rule["min_duration"]) is not int or type(rule["max_duration"]) is not int):
                raise ValueError(f"{region}/{model} requires integer min_duration and max_duration")
            minimum, maximum = rule["min_duration"], rule["max_duration"]
            lower, upper = 4, MODELS[model].max_duration
            if not lower <= minimum <= maximum <= upper:
                raise ValueError(f"invalid duration range for {region}/{model}; expected {lower}–{upper} seconds")
            models[model] = {"min_duration": minimum, "max_duration": maximum}
        result[region] = {"enabled": entry["enabled"], "models": models}
    return result


def region_denial_detail(account: dict[str, Any], normalized: dict[str, Any],
                         settings: dict[str, Any]) -> str | None:
    """Return a safe rejection explanation, using only normalized request fields."""
    try:
        region = normalize_region(account.get("region"))
        policy = validate_region_generation_policy(settings.get(
            "region_generation_policy", DEFAULT_REGION_GENERATION_POLICY))
    except ValueError:
        return "account region generation policy is invalid; generation was not submitted"
    entry = policy[region]
    if not entry["enabled"]:
        return None
    # Image normalization accepts case-insensitive aliases. Video aliases are
    # already strict; matching their normalized names is safe for both paths.
    model = str(normalized.get("model") or "").strip().lower()
    if model not in entry["models"]:
        return f"region {region} does not allow model {model}; generation was not submitted"
    if normalized.get("kind") == "image":
        return None
    rule = entry["models"][model]
    duration = normalized.get("duration")
    if (type(duration) is not int or not rule
            or not rule["min_duration"] <= duration <= rule["max_duration"]):
        return (f"region {region} does not allow {model} duration {duration}; "
                "generation was not submitted")
    return None


def region_allows_request(account: dict[str, Any], normalized: dict[str, Any],
                          settings: dict[str, Any]) -> bool:
    return region_denial_detail(account, normalized, settings) is None
