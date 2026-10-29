"""Check the running app's cached CLI contract without running any CLI command."""
from __future__ import annotations

import json
import os
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


def runtime_ready(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    runtime = value.get("canvas_cli_runtime")
    return bool(isinstance(runtime, dict)
        and runtime.get("available") is True
        and runtime.get("version") == "1.2.0"
        and runtime.get("distribution") == "global"
        and str(runtime.get("schema_version")) == "1"
        and runtime.get("proxy_transport") == "graftcp"
        and runtime.get("proxy_version") == "0.8.3")


def main() -> int:
    token = os.environ.get("IJM_ADMIN_TOKEN", "")
    if not token:
        return 1
    target = "http://127.0.0.1:8808/api/settings"
    request = Request(target, headers={"X-Admin-Token": token, "Accept": "application/json"})
    try:
        with build_opener(ProxyHandler({}), _NoRedirect()).open(request, timeout=4) as response:
            if response.status != 200 or response.geturl() != target:
                return 1
            raw = response.read(1048577)
            if len(raw) > 1048576:
                return 1
            return 0 if runtime_ready(json.loads(raw)) else 1
    except Exception:
        # Neither response bodies nor credentials belong in deployment logs.
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
