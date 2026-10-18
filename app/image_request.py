"""Normalize image requests without changing the caller's prompt or media order."""

from __future__ import annotations

import re
from typing import Any


RATIOS = {"1:1", "16:9", "9:16", "4:3", "3:4", "3:2", "2:3", "21:9"}
RESOLUTIONS = {"1k", "2k", "4k"}


def _images(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, list):
        result: list[str] = []
        for item in value:
            result.extend(_images(item))
        return result
    if isinstance(value, dict):
        kind = str(value.get("type") or "").lower()
        if kind in {"input_image", "image_url", "image"} or "image_url" in value:
            return _images(value.get("image_url") or value.get("url"))
        if "url" in value and len(value) <= 3:
            return _images(value["url"])
    return []


def _content(value: Any, texts: list[str], images: list[str]) -> None:
    if isinstance(value, str):
        texts.append(value)
    elif isinstance(value, list):
        for item in value:
            _content(item, texts, images)
    elif isinstance(value, dict):
        kind = str(value.get("type") or "").lower()
        if kind in {"input_image", "image_url", "image"} or "image_url" in value:
            images.extend(_images(value))
        elif kind in {"input_text", "text"}:
            text = value.get("text")
            if isinstance(text, str):
                texts.append(text)
        elif "content" in value:
            _content(value["content"], texts, images)


def normalize_image_request(payload: dict[str, Any], upstream_model: str) -> dict[str, Any]:
    texts: list[str] = []
    images: list[str] = []
    _content(payload.get("input"), texts, images)
    _content(payload.get("messages"), texts, images)
    images.extend(_images(payload.get("image_urls") or payload.get("images") or payload.get("image")))
    prompt = str(payload.get("prompt") or "\n".join(texts)).strip()
    if not prompt:
        raise ValueError("image prompt is required")
    tool = next((item for item in (payload.get("tools") or [])
                 if isinstance(item, dict) and item.get("type") == "image_generation"), {})
    size = str(payload.get("size") or tool.get("size") or "").strip().lower()
    ratio = str(payload.get("ratio") or tool.get("ratio") or "").strip()
    resolution = str(payload.get("resolution") or tool.get("resolution") or "").strip().lower()
    if not ratio and size in RATIOS:
        ratio = size
    if not resolution and size in RESOLUTIONS:
        resolution = size
    match = re.fullmatch(r"(\d{1,2}:\d{1,2})\((\d[kK])\)", size)
    if match:
        ratio = ratio or match.group(1)
        resolution = resolution or match.group(2).lower()
    ratio = ratio or "1:1"
    model_tier = re.search(r"-(1k|2k|4k)$", str(payload.get("model") or "").lower())
    resolution = resolution or (model_tier.group(1) if model_tier else "1k")
    if ratio not in RATIOS:
        raise ValueError(f"unsupported image ratio: {ratio}")
    if resolution not in RESOLUTIONS:
        raise ValueError(f"unsupported image resolution: {resolution}")
    count = int(payload.get("n") or payload.get("count") or tool.get("n") or 1)
    if count < 1 or count > 4:
        raise ValueError("image count must be between 1 and 4")
    images = list(dict.fromkeys(source.strip() for source in images if source.strip()))
    if len(images) > 10:
        raise ValueError("at most 10 reference images are supported")
    for source in images:
        if not source.startswith(("https://", "http://", "data:image/", "dreamina-image:")):
            raise ValueError("image input must be an HTTP URL, image data URL or Dreamina image URI")
    return {"kind": "image", "model": str(payload.get("model") or "").strip(),
            "upstream_model": upstream_model, "prompt": prompt,
            "images": images, "videos": [], "audios": [], "duration": 0,
            "aspect_ratio": ratio, "resolution": resolution, "count": count}
