"""Print field paths, never values, from one IJM image draft."""

from __future__ import annotations

import json
import sys

sys.path.insert(0, "/app")
from app.browser import CDPBrowser  # noqa: E402


def paths(value: object, prefix: str = "", depth: int = 0) -> list[str]:
    if depth > 9:
        return []
    if isinstance(value, dict):
        result = []
        for key, child in value.items():
            name = f"{prefix}.{key}" if prefix else key
            result.append(name)
            result.extend(paths(child, name, depth + 1))
        return result
    if isinstance(value, list) and value:
        return paths(value[0], f"{prefix}[]", depth + 1)
    return []


def main(port: int, submit_id: str) -> None:
    data = CDPBrowser(port).post(
        "/mweb/v1/get_history_by_ids", {"submit_ids": [submit_id]},
        query={"aid": "513695", "device_platform": "web", "region": "SG"},
    )
    record = data.get(submit_id) or {}
    draft = json.loads(record.get("draft_content") or "{}")
    for path in paths(draft):
        print(path)


if __name__ == "__main__":
    main(int(sys.argv[1]), sys.argv[2])
