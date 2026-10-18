"""Read only non-sensitive protocol fields from an existing image history entry.

This diagnostic deliberately never writes or prints prompts, cookies, media URIs,
user identifiers, or raw upstream responses.
"""

from __future__ import annotations

import json
import sys

sys.path.insert(0, "/app")
from app.browser import CDPBrowser  # noqa: E402


def fields(value: object, names: tuple[str, ...]) -> dict[str, object]:
    if not isinstance(value, dict):
        return {}
    return {key: value[key] for key in names if key in value and
            isinstance(value[key], (str, int, float, bool, type(None)))}


def describe(port: int, submit_id: str) -> dict[str, object]:
    data = CDPBrowser(port).post(
        "/mweb/v1/get_history_by_ids", {"submit_ids": [submit_id]},
        query={"aid": "513695", "device_platform": "web", "region": "SG"},
    )
    record = data.get(submit_id) or {}
    draft = json.loads(record.get("draft_content") or "{}")
    component = (draft.get("component_list") or [{}])[0]
    abilities = component.get("abilities") or {}
    branch_name = str(component.get("generate_type") or "")
    branch = abilities.get(branch_name) or {}
    core = branch.get("core_param") or {}
    option = abilities.get("gen_option") or {}
    image = ((branch.get("ability_list") or [{}])[0].get("image_list") or [{}])[0]
    ability = (branch.get("ability_list") or [{}])[0]
    placeholder = (branch.get("prompt_placeholder_info_list") or [{}])[0]
    output = {
        "model": (record.get("model_info") or {}).get("model_req_key"),
        "status": (record.get("task") or {}).get("status"),
        "result_image_count": len(record.get("item_list") or []),
        "workspace_id_exceeds_js_safe_integer": int(record.get("workspace_id") or 0) > 2**53 - 1,
        "resource_count": len(record.get("resources") or []),
        "draft": fields(draft, ("type", "version", "min_version", "is_from_tsn")),
        "component": fields(component, ("type", "min_version", "aigc_mode", "gen_type", "generate_type")),
        "metadata": fields(component.get("metadata"), ("type", "created_platform", "created_platform_version")),
        "abilities": fields(abilities, ("type",)),
        "branch": fields(branch, ("type",)),
        "core": fields(core, ("type", "model", "sample_strength", "image_ratio", "intelligent_ratio", "generate_type")),
        "large_image_info": fields(core.get("large_image_info"), ("type", "width", "height", "resolution_type")),
        "gen_option": fields(option, ("type", "gen_count", "generate_all")),
        "ability": fields(ability, ("type", "name", "strength")),
        "ability_image": fields(image, ("type", "source_from", "platform_type", "format", "width", "height")),
        "placeholder": fields(placeholder, ("type", "ability_index")),
        "postedit": fields(branch.get("postedit_param"), ("type", "generate_type")),
    }
    return output


if __name__ == "__main__":
    port = int(sys.argv[1])
    for item in sys.argv[2:]:
        print(json.dumps(describe(port, item), ensure_ascii=False, separators=(",", ":")))
