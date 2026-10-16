"""Public Seedance aliases and Dreamina media-reference validation."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ModelSpec:
    id: str
    upstream_model: str
    resolution: str
    family: str
    benefit_type: str
    draft_version: str = "3.3.9"

    @property
    def generation_resolution(self) -> str:
        return "720p" if self.id == "sd-2-5-480p-x" else self.resolution

    @property
    def limits(self) -> dict[str, int]:
        return {"images": 30, "audios": 10, "videos": 10} if self.family == "2.5" else {
            "images": 9, "audios": 3, "videos": 3}

    @property
    def max_duration(self) -> int:
        return 30 if self.family == "2.5" else 15


def _model(name: str, key: str, resolution: str, family: str, benefit: str,
           version: str = "3.3.9") -> ModelSpec:
    return ModelSpec(name, key, resolution, family, benefit, version)


MODELS = {item.id: item for item in (
    _model("sd-2-0", "dreamina_seedance_40_pro", "720p", "2.0", "dreamina_video_seedance_20_pro"),
    _model("sd-2-0-1080p", "dreamina_seedance_40_pro", "1080p", "2.0", "dreamina_video_seedance_20_pro"),
    _model("sd-2-0-4k", "dreamina_seedance_40_pro", "4k", "2.0", "dreamina_video_seedance_20_pro"),
    _model("sd-2-0-fast", "dreamina_seedance_40", "720p", "2.0", "dreamina_seedance_20_fast"),
    _model("sd-2-0-mini", "dreamina_seedance_40_mini", "720p", "2.0", "seedance_20_mini_720p_output", "3.0.5"),
    _model("sd-2-5", "dreamina_seedance_45_pro", "720p", "2.5", "seedance_25_720p_output"),
    _model("sd-2-5-480p", "dreamina_seedance_45_pro", "480p", "2.5", "seedance_25_480p_output"),
    _model("sd-2-5-480p-x", "dreamina_seedance_45_pro", "480p", "2.5", "seedance_25_720p_output"),
    _model("sd-2-5-1080p", "dreamina_seedance_45_pro", "1080p", "2.5", "seedance_25_1080p_output"),
    _model("sd-2-5-480p-preview", "dreamina_seedance_45_pro_draft", "480p", "2.5",
           "seedance_25_draft_480p_output", "3.3.28"),
)}

RATIOS = {"16:9", "9:16", "1:1", "4:3", "3:4", "21:9", "9:21"}
ALIASES = re.compile(
    r"(?<![\w<])(?P<explicit>@)?(?P<kind>视频|video|v|视|图片|image|img|图|音频|audio|音)\s*(?P<index>\d+)(?!\w)",
    re.I,
)


def media_alias_kind(label: str) -> str:
    word = label.lower()
    if word in {"视频", "video", "v", "视"}:
        return "videos"
    if word in {"图片", "image", "img", "图"}:
        return "images"
    return "audios"


def _media_list(body: dict[str, Any], singular: str, plural: str) -> list[str]:
    value = body.get(plural, body.get(singular, []))
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        raise ValueError(f"{plural} must be an array")
    urls = []
    for item in value:
        if isinstance(item, dict):
            item = item.get("url") or item.get("asset_id")
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"{plural} contains an invalid item")
        urls.append(item.strip())
    return urls


def prompt_references(prompt: str, media: dict[str, list[str]], *,
                      strict: bool = False) -> list[tuple[str, int]]:
    refs = []
    for match in ALIASES.finditer(prompt):
        kind = media_alias_kind(match.group("kind"))
        index = int(match.group("index"))
        if not media[kind] and not match.group("explicit"):
            continue
        if not 1 <= index <= len(media[kind]):
            if strict:
                raise ValueError(
                    f"prompt references {match.group(0)} but only {len(media[kind])} {kind} were provided")
            continue
        refs.append((kind, index))
    return refs


def normalize_request(body: dict[str, Any], *,
                      strict_media_references: bool = False) -> dict[str, Any]:
    name = str(body.get("model") or "sd-2-0-mini").strip()
    spec = MODELS.get(name)
    if spec is None:
        raise ValueError(f"unsupported model: {name}")
    prompt = str(body.get("prompt") or "").strip()
    if not prompt:
        raise ValueError("prompt is required")
    duration = body.get("duration", body.get("seconds", 4))
    if isinstance(duration, bool) or not str(duration).isdigit():
        raise ValueError("duration must be an integer number of seconds")
    duration = int(duration)
    if not 4 <= duration <= spec.max_duration:
        raise ValueError(f"duration must be 4–{spec.max_duration} seconds for {name}")
    ratio = str(body.get("aspect_ratio") or body.get("ratio") or "16:9")
    if ratio not in RATIOS:
        raise ValueError(f"unsupported aspect ratio: {ratio}")
    resolution = str(body.get("resolution") or spec.resolution).lower()
    if resolution != spec.resolution:
        raise ValueError(f"{name} uses {spec.resolution}; choose the corresponding model alias")
    media = {
        "images": _media_list(body, "images", "image_urls"),
        "videos": _media_list(body, "videos", "video_urls"),
        "audios": _media_list(body, "audios", "audio_urls"),
    }
    for kind, limit in spec.limits.items():
        if len(media[kind]) > limit:
            raise ValueError(f"{name} supports at most {limit} {kind}")
    prompt_references(prompt, media, strict=strict_media_references)
    return {
        "model": name, "upstream_model": spec.upstream_model,
        "duration": duration, "resolution": resolution, "aspect_ratio": ratio,
        "prompt": prompt, **media,
    }


def public_models() -> list[dict[str, Any]]:
    return [{
        "id": spec.id, "object": "model", "owned_by": "ijmapi",
        "meta": {"label": spec.id},
        "capabilities": {"durations": list(range(4, spec.max_duration + 1)),
                         "resolutions": [spec.resolution], "aspect_ratios": sorted(RATIOS),
                         "media_limits": spec.limits},
    } for spec in MODELS.values()]
