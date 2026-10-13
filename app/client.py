"""Dreamina Web video submission, media binding and result polling."""

from __future__ import annotations

import json
import random
import threading
import time
import uuid
from typing import Any

from .browser import BrowserError, BrowserRejected, CDPBrowser, account_proxy_url
from .catalog import ALIASES, MODELS, media_alias_kind
from .media import _download
from .store import Store
from .upload import UploadError, upload_media_bytes


class DreaminaError(Exception):
    pass


_UPLOAD_LOCKS_GUARD = threading.RLock()
_UPLOAD_LOCKS: dict[int, threading.RLock] = {}


def _uuid() -> str:
    return str(uuid.uuid4())


def _metadata(kind: str, index: int) -> dict[str, Any]:
    return {"type": "", "id": _uuid(), "meta_type": kind, "text": "",
            "material_ref": {"type": "", "id": _uuid(), "material_idx": index}}


def build_materials(assets: dict[str, list[dict[str, Any]]]) -> tuple[list[dict[str, Any]], dict[str, list[int]]]:
    """Preserve the UI's material_list order: images, videos, then audio."""
    materials: list[dict[str, Any]] = []
    index_map: dict[str, list[int]] = {"images": [], "videos": [], "audios": []}
    for kind, material_type in (("images", "image"), ("videos", "video"), ("audios", "audio")):
        for item in assets[kind]:
            index_map[kind].append(len(materials))
            base = {"type": "", "id": _uuid(), "material_type": material_type}
            if kind == "images":
                uri = str(item["image_uri"])
                base["image_info"] = {
                    "type": "image", "id": _uuid(), "source_from": "upload",
                    "platform_type": 1, "name": "", "image_uri": uri,
                    "aigc_image": {"type": "", "id": _uuid()},
                    "width": int(item["width"]), "height": int(item["height"]),
                    "format": "", "title": "", "uri": uri}
            elif kind == "videos":
                base["video_info"] = {
                    "type": "video", "id": _uuid(), "source_from": "upload",
                    "name": "", "vid": str(item["vid"]),
                    "fps": float(item.get("fps") or 0),
                    "width": int(item.get("width") or 0),
                    "height": int(item.get("height") or 0),
                    "duration": round(float(item.get("duration") or 0) * 1000),
                    "cover_image_url": ""}
            else:
                base["audio_info"] = {
                    "type": "audio", "id": _uuid(), "source_from": "upload",
                    "vid": str(item["vid"]),
                    "duration": round(float(item.get("duration") or 0) * 1000),
                    "name": ""}
            materials.append(base)
    return materials, index_map


def build_meta_list(prompt: str, index_map: dict[str, list[int]]) -> list[dict[str, Any]]:
    """Turn caller labels into Dreamina's ordered text and material_ref nodes."""
    result: list[dict[str, Any]] = []
    cursor = 0
    for match in ALIASES.finditer(prompt):
        kind = media_alias_kind(match.group("kind"))
        position = int(match.group("index")) - 1
        if not index_map[kind] and not match.group("explicit"):
            continue
        if not 0 <= position < len(index_map[kind]):
            raise ValueError(f"unbound media reference: {match.group(0)}")
        if match.start() > cursor:
            result.append({"type": "", "id": _uuid(), "meta_type": "text",
                           "text": prompt[cursor:match.start()]})
        result.append(_metadata({"images": "image", "videos": "video",
                                 "audios": "audio"}[kind], index_map[kind][position]))
        cursor = match.end()
    if cursor < len(prompt) or not result:
        result.append({"type": "", "id": _uuid(), "meta_type": "text",
                       "text": prompt[cursor:]})
    return result


def build_submit_body(normalized: dict[str, Any],
                      assets: dict[str, list[dict[str, Any]]]) -> tuple[dict[str, Any], str]:
    spec = MODELS[normalized["model"]]
    materials, index_map = build_materials(assets)
    submit_id = _uuid()
    component_id = _uuid()
    duration = int(normalized["duration"])
    video_seconds = sum(float(item.get("duration") or 0) for item in assets["videos"])
    scene = {
        "type": "video", "scene": "BasicVideoGenerateButton",
        "resolution": spec.generation_resolution, "modelReqKey": spec.upstream_model,
        "videoDuration": duration, "batchNumber": 1,
        "inputVideoDuration": round(video_seconds, 3),
        "chargeInputVideoDuration": True, "isLongVideo": duration > 15,
        "hasInputVideo": bool(assets["videos"]),
        "useSeedanceFast5sFreeTrial": False,
        "reportParams": {
            "enterSource": "generate", "vipSource": "generate",
            "extraVipFunctionKey": f"{spec.upstream_model}-{spec.generation_resolution}",
            "useVipFunctionDetailsReporterHoc": True,
            "cashierBusinessExtra": {}},
        "materialTypes": [1 if item["material_type"] == "image"
                          else 2 if item["material_type"] == "video" else 3
                          for item in materials],
    }
    metrics: dict[str, Any] = {
        "isDefaultSeed": 1, "originSubmitId": submit_id,
        "isRegenerate": False, "enterFrom": "click",
        "position": "page_bottom_box", "promptType": "original_prompt",
        "functionMode": "omni_reference", "generatorFeature": "omniReference",
        "sceneOptions": json.dumps([scene], ensure_ascii=False, separators=(",", ":")),
        "batchNumber": 1, "submitGroupId": _uuid(), "hasRejectedAudit": 0,
    }
    if spec.upstream_model.endswith("_draft"):
        metrics["videoStage"] = "draft"
    input_item: dict[str, Any] = {
        "type": "", "id": _uuid(), "min_version": spec.draft_version,
        "prompt": "" if materials else normalized["prompt"],
        "video_mode": 2, "fps": 24,
        "duration_ms": duration * 1000, "resolution": spec.generation_resolution,
        "idip_meta_list": [],
    }
    if spec.upstream_model.endswith("_draft"):
        input_item["is_draft_mode"] = True
    if materials:
        input_item["unified_edit_input"] = {
            "type": "", "id": _uuid(), "material_list": materials,
            "meta_list": build_meta_list(normalized["prompt"], index_map)}
    draft = {
        "type": "draft", "id": _uuid(), "min_version": spec.draft_version,
        "min_features": (["AIGC_Video_UnifiedEdit", "AIGC_Video_Seedance25ResultAction"]
                         if materials and spec.family == "2.5" else
                         ["AIGC_Video_UnifiedEdit"] if materials else []),
        "is_from_tsn": True, "version": spec.draft_version,
        "main_component_id": component_id,
        "component_list": [{
            "type": "video_base_component", "id": component_id,
            "min_version": "1.0.0", "aigc_mode": "workbench",
            "gen_type": 10,
            "metadata": {"type": "", "id": _uuid(), "created_platform": 3,
                         "created_platform_version": "",
                         "created_time_in_ms": str(round(time.time() * 1000)),
                         "created_did": ""},
            "generate_type": "gen_video",
            "abilities": {"type": "", "id": _uuid(), "gen_video": {
                "type": "", "id": _uuid(),
                "text_to_video_params": {
                    "type": "", "id": _uuid(), "video_gen_inputs": [input_item],
                    "video_aspect_ratio": normalized["aspect_ratio"],
                    "seed": random.randint(1, 2**32 - 1),
                    "model_req_key": spec.upstream_model, "priority": 0},
                "video_task_extra": json.dumps(metrics, ensure_ascii=False,
                                               separators=(",", ":"))}},
            "process_type": 1,
        }],
    }
    commerce = {
        "amount": 0, "benefit_type": spec.benefit_type,
        "resource_id": "generate_video", "resource_id_type": "str",
        "resource_sub_type": "aigc"}
    body = {
        "extend": {"root_model": spec.upstream_model,
                   "m_video_commerce_info": commerce,
                   "workspace_id": 0,
                   "m_video_commerce_info_list": [commerce]},
        "submit_id": submit_id,
        "metrics_extra": json.dumps(metrics, ensure_ascii=False, separators=(",", ":")),
        "draft_content": json.dumps(draft, ensure_ascii=False, separators=(",", ":")),
    }
    return body, submit_id


def extract_result_urls(record: dict[str, Any]) -> list[str]:
    urls = []
    for item in record.get("item_list") or []:
        if not isinstance(item, dict):
            continue
        video = item.get("video") or {}
        origin = (video.get("transcoded_video") or {}).get("origin") or {}
        url = origin.get("video_url")
        if isinstance(url, str) and url.startswith("https://") and url not in urls:
            urls.append(url)
    return urls


class DreaminaClient:
    def __init__(self, account: dict[str, Any], *, store: Store | None = None):
        account_proxy_url(account)
        self.account = account
        self.store = store
        self.browser = CDPBrowser(int(account["cdp_port"]))

    def account_state(self) -> dict[str, Any]:
        # A credit read cannot create a video task. Retry only this safe call
        # when a tab navigation or a short proxy interruption breaks fetch.
        for attempt in range(3):
            try:
                data = self.browser.post("/commerce/v1/benefits/user_credit", {})
                break
            except BrowserRejected:
                raise
            except BrowserError:
                if attempt == 2:
                    raise
                time.sleep(0.5 * (2 ** attempt))
        credit = data.get("credit") or {}
        balance = sum(float(credit.get(key) or 0) for key in
                      ("vip_credit", "gift_credit", "purchase_credit"))
        return {"available_balance": balance, "buckets": credit}

    def daily_checkin(self) -> dict[str, Any]:
        return self.browser.post("/commerce/v1/benefits/credit_receive",
                                 {"time_zone": "Asia/Taipei"})

    def upload_inputs(self, normalized: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
        account_id = int(self.account["id"])
        with _UPLOAD_LOCKS_GUARD:
            lock = _UPLOAD_LOCKS.setdefault(account_id, threading.RLock())
        result: dict[str, list[dict[str, Any]]] = {}
        for kind in ("images", "videos", "audios"):
            result[kind] = []
            for source in normalized[kind]:
                # Existing Dreamina media can be reused by supplying a typed
                # asset URI/VID; regular URLs and data URLs are uploaded first.
                if source.startswith("dreamina-image:") and kind == "images":
                    result[kind].append({"image_uri": source.split(":", 1)[1],
                                         "width": 0, "height": 0})
                    continue
                if source.startswith("dreamina-vid:") and kind != "images":
                    result[kind].append({"vid": source.split(":", 1)[1],
                                         "duration": 0, "width": 0, "height": 0, "fps": 0})
                    continue
                contents, _, filename = _download(source, kind,
                                                  str(self.account["proxy_url"]))
                import hashlib
                digest = hashlib.sha256(contents).hexdigest()
                with lock:
                    cached = (self.store.media_cache_get(account_id, kind, digest)
                              if self.store else None)
                    if cached:
                        result[kind].append(cached)
                        continue
                    uploaded = upload_media_bytes(contents, kind, browser=self.browser,
                                                  proxy_url=str(self.account["proxy_url"]),
                                                  filename=filename)
                    if self.store:
                        self.store.media_cache_put(account_id, kind, digest, uploaded)
                    result[kind].append(uploaded)
        return result

    def submit(self, normalized: dict[str, Any],
               assets: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
        body, submit_id = build_submit_body(normalized, assets)
        query = {"aid": "513695", "device_platform": "web", "region": "SG",
                 "os": "windows", "web_component_open_flag": "1",
                 "aigc_features": "app_lip_sync",
                 "da_version": MODELS[normalized["model"]].draft_version,
                 "generate_id": submit_id,
                 "commerce_with_input_video": "1" if assets["videos"] else "0"}
        data = self.browser.post("/mweb/v1/aigc_draft/generate", body, query=query)
        record = data.get("aigc_data") or {}
        task = record.get("task") or {}
        if not task.get("task_id") or not task.get("submit_id"):
            raise DreaminaError("Dreamina submit response has no task ID")
        return {"run_id": str(task["submit_id"]),
                "thread_id": str(record.get("history_record_id") or task.get("history_id") or "")}

    def poll(self, submit_id: str, history_id: str) -> dict[str, Any]:
        data = self.browser.post(
            "/mweb/v1/get_history_by_ids", {"submit_ids": [submit_id]},
            query={"aid": "513695", "device_platform": "web", "region": "SG"})
        record = data.get(submit_id) or {}
        if not record:
            return {"status": "running", "result_urls": [], "error": "", "error_code": ""}
        task = record.get("task") or {}
        status = int(task.get("status") or record.get("status") or 0)
        urls = extract_result_urls(record)
        response_return = task.get("resp_ret") or {}
        if isinstance(response_return, dict):
            response_return = response_return.get("ret")
        error_code = str(record.get("fail_code") or response_return or "")
        error = str(record.get("fail_starling_message") or
                    record.get("fail_msg") or record.get("fail_starling_key") or "")
        if urls:
            state = "succeeded"
        elif status in {40, 60, 70, 80, 90} or error_code not in {"", "0"}:
            state = "failed"
        else:
            state = "running"
        return {"status": state, "state": status, "result_urls": urls,
                "error_code": error_code, "error": error}
