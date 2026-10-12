"""Record a credential-free CDP protocol trace from the dedicated IJM Chrome."""

from __future__ import annotations

import base64
import datetime as dt
import json
import re
import sys
import threading
import time
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

import requests
import websocket


PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 9339
ROOT = Path(__file__).resolve().parent / "captures"
ROOT.mkdir(parents=True, exist_ok=True)
OUTPUT = ROOT / f"ijm-cdp-{dt.datetime.now(dt.timezone.utc):%Y%m%dT%H%M%SZ}.jsonl"
WRITE_LOCK = threading.Lock()
SAFE_VALUES = {
    "model", "model_name", "model_id", "duration", "duration_sec", "seconds",
    "resolution", "ratio", "aspect_ratio", "quality", "generate_type",
    "task_type", "state", "status", "ret", "code", "error_code",
    "credits", "credit", "cost", "quota", "count", "quantity", "width",
    "height", "fps", "watermark", "watermark_switch", "agent_name",
    "mime_type", "media_type", "asset_type", "mode", "is_vip", "is_member",
}
UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f-]{24,}$", re.I)


def record(**values: object) -> None:
    values["at"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds")
    with WRITE_LOCK:
        with OUTPUT.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(values, ensure_ascii=False, separators=(",", ":")) + "\n")


def endpoint(url: str) -> dict[str, object]:
    parts = urlsplit(url)
    segments = []
    for part in parts.path.split("/"):
        if len(part) > 48 or UUID.fullmatch(part) or "@" in part:
            segments.append(":id")
        else:
            segments.append(part)
    return {"host": parts.hostname or "", "path": "/".join(segments),
            "query_keys": sorted({key for key, _ in parse_qsl(parts.query)})}


def shape(value: object, key: str = "", depth: int = 0) -> object:
    if depth >= 8:
        return "<depth-limit>"
    if isinstance(value, dict):
        return {str(k)[:100]: shape(v, str(k).lower(), depth + 1)
                for k, v in list(value.items())[:100]}
    if isinstance(value, list):
        return {"type": "array", "length": len(value),
                "sample": [shape(item, "", depth + 1) for item in value[:2]]}
    if isinstance(value, str):
        if len(value) < 131072 and value[:1] in "[{":
            try:
                return {"encoded_json": shape(json.loads(value), key, depth + 1)}
            except (ValueError, TypeError):
                pass
        if key in SAFE_VALUES and len(value) <= 80:
            return value
        return f"<string:{len(value)}>"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value if key in SAFE_VALUES else "<number>"
    if isinstance(value, bool):
        return value
    return None


def payload_shape(raw: str) -> object:
    try:
        return shape(json.loads(raw))
    except (ValueError, TypeError):
        return {"format": "non-json", "length": len(raw)}


def header_names(headers: object) -> list[str]:
    return sorted(str(key).lower() for key in headers) if isinstance(headers, dict) else []


def interesting(method: str, kind: str, url: str) -> bool:
    path = urlsplit(url).path.lower()
    return (kind in {"XHR", "Fetch", "Document", "Preflight", "WebSocket"}
            or method not in {"GET", "HEAD"} or path.endswith((".mp4", ".mov")))


class PageCapture(threading.Thread):
    def __init__(self, target: dict[str, object]) -> None:
        super().__init__(daemon=True, name=f"ijm-cdp-{str(target['id'])[:8]}")
        self.target = target
        self.requests: dict[str, dict[str, object]] = {}
        self.pending: dict[int, tuple[str, str]] = {}
        self.next_id = 0
        self.alive = True

    def send(self, ws: websocket.WebSocket, method: str,
             params: dict[str, object] | None = None, request_id: str = "") -> None:
        self.next_id += 1
        if request_id:
            self.pending[self.next_id] = (method, request_id)
        ws.send(json.dumps({"id": self.next_id, "method": method,
                            "params": params or {}}, separators=(",", ":")))

    def run(self) -> None:
        target_id = str(self.target["id"])
        try:
            ws = websocket.create_connection(str(self.target["webSocketDebuggerUrl"]),
                                             timeout=3, suppress_origin=True)
            ws.settimeout(2)
            self.send(ws, "Network.enable")
            self.send(ws, "Page.enable")
            record(event="attached", target=target_id[:12], page=endpoint(str(self.target.get("url") or "")))
            while True:
                try:
                    message = json.loads(ws.recv())
                except websocket.WebSocketTimeoutException:
                    continue
                if "id" in message:
                    pending = self.pending.pop(message["id"], None)
                    if pending:
                        self.command_response(pending, message)
                    continue
                method = message.get("method")
                data = message.get("params") or {}
                rid = str(data.get("requestId") or "")
                if method == "Network.requestWillBeSent":
                    req = data.get("request") or {}
                    url = str(req.get("url") or "")
                    verb = str(req.get("method") or "GET")
                    kind = str(data.get("type") or "")
                    if not interesting(verb, kind, url):
                        continue
                    self.requests[rid] = {"endpoint": endpoint(url), "method": verb,
                                          "type": kind, "mime": ""}
                    record(event="request", target=target_id[:12], request_id=rid,
                           method=verb, resource_type=kind, **endpoint(url),
                           header_names=header_names(req.get("headers")),
                           body=payload_shape(req["postData"]) if isinstance(req.get("postData"), str) else None,
                           has_post_data=bool(req.get("hasPostData")))
                    if verb not in {"GET", "HEAD"} and "postData" not in req:
                        self.send(ws, "Network.getRequestPostData", {"requestId": rid}, rid)
                elif method == "Network.responseReceived" and rid in self.requests:
                    response = data.get("response") or {}
                    info = self.requests[rid]
                    info["mime"] = str(response.get("mimeType") or "")
                    record(event="response", target=target_id[:12], request_id=rid,
                           status=response.get("status"), mime=info["mime"],
                           protocol=response.get("protocol"),
                           header_names=header_names(response.get("headers")),
                           **info["endpoint"])
                elif method == "Network.loadingFinished" and rid in self.requests:
                    info = self.requests[rid]
                    if "json" in str(info.get("mime") or "").lower():
                        self.send(ws, "Network.getResponseBody", {"requestId": rid}, rid)
                    else:
                        self.requests.pop(rid, None)
                elif method == "Network.loadingFailed" and rid in self.requests:
                    info = self.requests.pop(rid)
                    record(event="loading_failed", target=target_id[:12], request_id=rid,
                           error=str(data.get("errorText") or "")[:100], **info["endpoint"])
        except (OSError, ValueError, websocket.WebSocketException) as exc:
            record(event="detached", target=target_id[:12], error=type(exc).__name__)
        finally:
            self.alive = False
            try:
                ws.close()
            except (NameError, OSError):
                pass

    def command_response(self, pending: tuple[str, str], message: dict[str, object]) -> None:
        command, rid = pending
        info = self.requests.get(rid)
        if not info or message.get("error"):
            return
        result = message.get("result") or {}
        if command == "Network.getRequestPostData":
            raw = result.get("postData")
            if isinstance(raw, str):
                record(event="request_body", request_id=rid, body=payload_shape(raw),
                       **info["endpoint"])
        elif command == "Network.getResponseBody":
            self.requests.pop(rid, None)
            raw = result.get("body")
            if not isinstance(raw, str):
                return
            if result.get("base64Encoded"):
                try:
                    raw = base64.b64decode(raw).decode("utf-8", "replace")
                except (ValueError, UnicodeError):
                    return
            record(event="response_body", request_id=rid, body=payload_shape(raw),
                   **info["endpoint"])


def main() -> None:
    record(event="listener_started", port=PORT)
    print(f"IJM_LISTEN_READY port={PORT} log={OUTPUT}", flush=True)
    pages: dict[str, PageCapture] = {}
    while True:
        try:
            targets = requests.get(f"http://127.0.0.1:{PORT}/json/list", timeout=4).json()
            for target in targets:
                if target.get("type") != "page" or not target.get("webSocketDebuggerUrl"):
                    continue
                target_id = str(target["id"])
                if target_id not in pages or not pages[target_id].alive:
                    pages[target_id] = PageCapture(target)
                    pages[target_id].start()
        except (requests.RequestException, ValueError) as exc:
            record(event="discovery_error", error=type(exc).__name__)
        time.sleep(2)


if __name__ == "__main__":
    main()
