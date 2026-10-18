"""Dreamina image draft submission and history polling through account Chrome."""

from __future__ import annotations

import json
import random
import time
import uuid
from typing import Any

from .browser import CDPBrowser


IMAGE_MODELS = {
    "gpt-image-2.5-flare": "dreamina_gpt_image_2_5_flare",
    "gpt-image-2.5-sunburst": "dreamina_gpt_image_2_5_sunburst",
}
IMAGE_RATIOS = {
    "1:1": (1, 1024, 1024),
    "3:4": (2, 768, 1024),
    "16:9": (3, 1024, 576),
    "4:3": (4, 1024, 768),
    "9:16": (5, 576, 1024),
    "2:3": (6, 682, 1024),
    "3:2": (7, 1024, 682),
    "21:9": (8, 1195, 512),
}
IMAGE_VERSION = "3.0.2"
IMAGE_DRAFT_VERSION = "3.3.30"


def resolve_image_model(model: str) -> str:
    name = model.strip().lower()
    for tier in ("-1k", "-2k", "-4k"):
        if name.endswith(tier):
            name = name[:-len(tier)]
            break
    if name not in IMAGE_MODELS:
        raise ValueError(f"unsupported IJM image model: {model}")
    return IMAGE_MODELS[name]


def _uuid() -> str:
    return str(uuid.uuid4())


def _core(normalized: dict[str, Any]) -> dict[str, Any]:
    ratio_code, base_width, base_height = IMAGE_RATIOS[normalized["aspect_ratio"]]
    scale = {"1k": 1, "2k": 2, "4k": 4}[normalized["resolution"]]
    result: dict[str, Any] = {
        "type": "", "id": _uuid(), "model": normalized["upstream_model"],
        "prompt": normalized["prompt"], "sample_strength": 0.5,
        "image_ratio": ratio_code,
        "large_image_info": {"type": "", "id": _uuid(),
                             "height": base_height * scale, "width": base_width * scale,
                             "resolution_type": normalized["resolution"]},
        "intelligent_ratio": False, "generate_type": 0,
    }
    if not normalized["images"]:
        result["negative_prompt"] = ""
        result["seed"] = random.randint(1, 2**32 - 1)
    return result


def build_image_submit_body(normalized: dict[str, Any], assets: list[dict[str, Any]],
                            workspace_id: int = 0) -> tuple[dict[str, Any], str]:
    """Build the observed 3.0.2 image draft; edit resources keep input order."""
    submit_id = _uuid()
    component_id = _uuid()
    is_edit = bool(assets)
    branch_name = "blend" if is_edit else "generate"
    branch: dict[str, Any] = {"type": "", "id": _uuid()}
    if is_edit:
        branch["min_features"] = []
    branch["core_param"] = _core(normalized)
    if is_edit:
        uris = [str(item["image_uri"]) for item in assets]
        branch["ability_list"] = [{
            "type": "", "id": _uuid(), "name": "byte_edit",
            "image_uri_list": uris,
            "image_list": [{"type": "image", "id": _uuid(), "source_from": "upload",
                            "platform_type": 1, "name": "", "image_uri": uri,
                            "width": 0, "height": 0, "format": "", "title": "", "uri": uri}
                           for uri in uris],
            "strength": 0.5,
        }]
        branch["prompt_placeholder_info_list"] = [
            {"type": "", "id": _uuid(), "ability_index": 0}]
        branch["postedit_param"] = {"type": "", "id": _uuid(), "generate_type": 0}
    component = {
        "type": "image_base_component", "id": component_id,
        "min_version": IMAGE_VERSION, "aigc_mode": "workbench",
        "metadata": {"type": "", "id": _uuid(), "created_platform": 3,
                     "created_platform_version": "",
                     "created_time_in_ms": str(round(time.time() * 1000)),
                     "created_did": ""},
        "generate_type": branch_name,
        "abilities": {"type": "", "id": _uuid(), branch_name: branch,
                      "gen_option": {"type": "", "id": _uuid(),
                                     "gen_count": normalized["count"],
                                     "generate_all": False}},
    }
    draft = {"type": "draft", "id": _uuid(), "min_version": IMAGE_VERSION,
             "min_features": [], "is_from_tsn": True, "version": IMAGE_DRAFT_VERSION,
             "main_component_id": component_id, "component_list": [component]}
    metrics = {"promptSource": "manual", "generateCount": normalized["count"],
               "enterFrom": "click", "position": "page_bottom_box",
               "isBoxSelect": False, "isCutout": False,
               "hasRejectedAudit": 0, "generateId": submit_id, "isRegenerate": False}
    return {
        "extend": {"root_model": normalized["upstream_model"],
                   "workspace_id": workspace_id},
        "submit_id": submit_id,
        "metrics_extra": json.dumps(metrics, ensure_ascii=False, separators=(",", ":")),
        "draft_content": json.dumps(draft, ensure_ascii=False, separators=(",", ":")),
        "http_common_info": {"aid": 513695},
    }, submit_id


def image_result_urls(record: dict[str, Any]) -> list[str]:
    urls: list[str] = []
    for item in record.get("item_list") or []:
        for image in (item.get("image") or {}).get("large_images") or []:
            url = image.get("image_url")
            if isinstance(url, str) and url.startswith("https://") and url not in urls:
                urls.append(url)
    return urls


class DreaminaImageClient:
    def __init__(self, port: int):
        self.browser = CDPBrowser(port)

    def create_workspace(self) -> int:
        data = self.browser.post("/mweb/v1/workspace/create", {"name": "Image generation"},
                                 query={"aid": "513695", "device_platform": "web", "region": "SG"})
        return int(data.get("workspace_id") or 0)

    def submit(self, normalized: dict[str, Any], assets: list[dict[str, Any]]) -> dict[str, str]:
        if assets:
            uris = [str(item["image_uri"]) for item in assets]
            self.browser.post("/mweb/v1/imagex/submit_audit_job", {"uri_list": uris},
                              query={"aid": "513695", "device_platform": "web", "region": "SG"},
                              allow_empty_ret=True)
            self.browser.post("/mweb/v1/execute_generate_audit", {
                "scene": 1, "model_key": normalized["upstream_model"],
                "material_list": [{"uri": uri, "material_type": 1} for uri in uris],
            }, query={"aid": "513695", "device_platform": "web", "region": "SG",
                      "da_version": IMAGE_VERSION})
        workspace_id = self.create_workspace()
        body, submit_id = build_image_submit_body(normalized, assets, workspace_id)
        data = self.browser.post("/mweb/v1/aigc_draft/generate", body,
                                 query={"aid": "513695", "device_platform": "web",
                                        "region": "SG", "os": "windows",
                                        "web_component_open_flag": "1",
                                        "da_version": "3.1.3",
                                        "web_version": "7.5.0",
                                        "aigc_features": "app_lip_sync",
                                        "babi_param": "{}",
                                        "generate_id": submit_id,
                                        "commerce_with_input_video": "1"},
                                 native_client=True)
        record = data.get("aigc_data") or {}
        task = record.get("task") or {}
        if not task.get("submit_id"):
            raise RuntimeError("Dreamina image submit returned no submit ID")
        return {"run_id": str(task["submit_id"]),
                "thread_id": str(record.get("history_record_id") or task.get("history_id") or "")}

    def poll(self, submit_id: str) -> dict[str, Any]:
        data = self.browser.post("/mweb/v1/get_history_by_ids", {"submit_ids": [submit_id]},
                                 query={"aid": "513695", "device_platform": "web", "region": "SG"})
        record = data.get(submit_id) or {}
        if not record:
            return {"status": "running", "result_urls": [], "error": ""}
        status = int((record.get("task") or {}).get("status") or record.get("status") or 0)
        urls = image_result_urls(record)
        if status == 50 and urls:
            return {"status": "succeeded", "result_urls": urls, "error": ""}
        if status in {30, 40, 60, 70, 80, 90}:
            message = next((str(record.get(key) or "").strip()
                            for key in ("fail_starling_message", "fail_msg", "fail_starling_key")
                            if str(record.get(key) or "").strip().lower()
                            not in {"", "[none]", "none", "null", "undefined"}), "")
            return {"status": "failed", "result_urls": [],
                    "error": message or "Dreamina image generation failed"}
        return {"status": "running", "result_urls": [], "error": ""}
