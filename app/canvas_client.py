"""Official Canvas 1.2.0 commands mapped to IJM's durable task lifecycle."""

from __future__ import annotations

import copy
import hashlib
import math
import re
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from .canvas_cli import CanvasCLI, CanvasCLIError, account_profile
from .catalog import ALIASES, media_alias_kind
from .errors import public_error, upstream_review_error


_NAMESPACE = uuid.UUID("b867f326-c52e-491e-b971-3937d0c731c1")
_MODEL_NAMES = {
    "sd-2-0": ("seedance_2.0_vip", "seedance_2.0"),
    "sd-2-0-fast": ("seedance_2.0_fast_vip", "seedance_2.0_fast"),
    "sd-2-0-mini": ("seedance_2.0_mini",),
    "sd-2-5": ("seedance_2.5",),
    "sd-2-5-480p-preview": ("seedance_2.5_draft", "seedance_2.5_preview"),
}
_TRANSIENT = {"network_timeout", "dns_resolution_failed", "network_error",
              "service_unavailable", "rate_limited", "transport_error"}


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) and result >= 0 else None


def _key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value).lower())


def _stable(*parts: Any) -> str:
    return str(uuid.uuid5(_NAMESPACE, ":".join(map(str, parts))))


def _file_mime(path: Path, media_type: str) -> str:
    with path.open("rb") as stream:
        header = stream.read(32)
    mime = ""
    if header.startswith(b"\xff\xd8\xff"):
        mime = "image/jpeg"
    elif header.startswith(b"\x89PNG\r\n\x1a\n"):
        mime = "image/png"
    elif header[:6] in {b"GIF87a", b"GIF89a"}:
        mime = "image/gif"
    elif header.startswith(b"RIFF") and header[8:12] == b"WEBP":
        mime = "image/webp"
    elif len(header) >= 12 and header[4:8] == b"ftyp":
        mime = "image/avif" if header[8:12] in {b"avif", b"avis"} else "video/mp4"
    if not mime or not mime.startswith(media_type + "/"):
        raise CanvasCLIError("CLI_RESULT_INVALID", f"Canvas download is not the requested {media_type} media")
    return mime


def _node_id(task_id: str) -> str:
    value = int.from_bytes(hashlib.sha256(task_id.encode()).digest()[:7], "big") >> 6
    alphabet = "0123456789abcdefghjkmnpqrstvwxyz"
    result = ""
    for _ in range(10):
        result = alphabet[value & 31] + result
        value >>= 5
    return "node_" + result


def _data(envelope: dict[str, Any]) -> dict[str, Any]:
    value = envelope.get("data")
    if not isinstance(value, dict):
        raise CanvasCLIError("CLI_INVALID_RESPONSE", "Canvas returned no structured data")
    return value


def _unsupported(message: str) -> CanvasCLIError:
    return CanvasCLIError("CLI_CAPABILITY_UNAVAILABLE", message + "; select browser submission mode manually")


def _failure_code(code: Any, message: str) -> str:
    code = str(code or "")
    specific = public_error(code, message)["code"]
    if specific in {"INPUT_AUDIO_DURATION_UNSUPPORTED", "INPUT_IMAGE_CONTENT_VIOLATION",
                    "OUTPUT_VIDEO_COPYRIGHT", "INPUT_IMAGE_SIZE_UNSUPPORTED", "VIDEO_EDIT_CONSTRAINT"}:
        return specific
    if code in {"12009", "12011", "2052"}:
        return "CONTENT_MODERATION_FAILED"
    if code == "99999":
        return "UPSTREAM_TEMPORARY"
    return upstream_review_error(code, message) or (specific if specific != code else code) or "UPSTREAM_FAILED"


def _model_candidates(normalized: dict[str, Any]) -> list[str]:
    public = str(normalized.get("model") or "")
    base = re.sub(r"-(?:480p|720p|1080p|1k|2k|4k)(?:-x)?$", "", public)
    preferred = list(_MODEL_NAMES.get(public, _MODEL_NAMES.get(base, ())))
    # Existing image aliases must be found as themselves or their actual Web
    # model ID. They must never be silently mapped to a different image model.
    return [public, base, str(normalized.get("upstream_model") or ""), *preferred]


def select_model(items: list[dict[str, Any]], normalized: dict[str, Any]) -> dict[str, Any]:
    candidates = [_key(x) for x in _model_candidates(normalized) if x]
    for candidate in candidates:
        matches = [item for item in items if candidate in {
            _key(item.get("model")), _key(item.get("displayName")),
            *(_key(alias) for alias in item.get("aliases", []) if isinstance(alias, str))}]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise _unsupported("Canvas model discovery returned an ambiguous model mapping")
    raise _unsupported(f"Canvas does not expose the requested model {normalized.get('model', '')}")


def select_mode(model: dict[str, Any], normalized: dict[str, Any], counts: dict[str, int]) -> dict[str, Any]:
    image = normalized.get("kind") == "image"
    has_refs = any(counts.values())
    desired = ("i2i" if has_refs else "t2i") if image else ("m2v" if has_refs else "t2v")
    requested = str(normalized.get("mode") or "")
    if not image and requested in {"i2v", "i2v_first_last", "first_last_frame"}:
        if counts["images"] in {1, 2} and not counts["videos"] and not counts["audios"]:
            desired = "first_last_frame"
    modes = [mode for mode in model.get("modes", [])
             if isinstance(mode, dict) and mode.get("name") == desired]
    if len(modes) != 1:
        raise _unsupported(f"Canvas model does not expose {desired} mode")
    mode = modes[0]
    references = mode.get("references")
    if isinstance(references, dict):
        total = sum(counts.values())
        if total < int(references.get("min", 0)) or total > int(references.get("max", total)):
            raise _unsupported("Canvas model reference count is outside its live limits")
        types = references.get("types") or []
        for media_type, plural in (("image", "images"), ("video", "videos"), ("audio", "audios")):
            rule = next((x for x in types if x.get("mediaType") == media_type), None)
            if rule is None and counts[plural] and types:
                raise _unsupported(f"Canvas model does not accept {media_type} references")
            if rule is not None and not int(rule.get("min", 0)) <= counts[plural] <= int(rule.get("max", counts[plural])):
                raise _unsupported(f"Canvas model {media_type} reference count is outside its live limits")
        required = references.get("requireAnyOfTypes") or []
        if required and not any(counts.get(str(x) + "s", 0) for x in required):
            raise _unsupported("Canvas model requires another reference media type")
        exclusive = references.get("mutuallyExclusiveTypes") or []
        if sum(bool(counts.get(str(x) + "s", 0)) for x in exclusive) > 1:
            raise _unsupported("Canvas model does not allow this reference media combination")
    return mode


def generation_flags(mode: dict[str, Any], normalized: dict[str, Any]) -> list[str]:
    requested = {"ratio": normalized.get("aspect_ratio"), "resolution": normalized.get("resolution"),
                 "count": normalized.get("count", 1)}
    if normalized.get("kind") != "image":
        requested["duration"] = normalized.get("duration")
    rules = {str(x.get("flag", "")).lstrip("-"): x for x in mode.get("flags", []) if isinstance(x, dict)}
    result: list[str] = []
    for name, value in requested.items():
        if value is None or value == "":
            continue
        rule = rules.get(name)
        if rule is None:
            # Count 1 is the documented default and can be omitted if the
            # live mode does not advertise it. Other caller settings cannot.
            if name == "count" and value == 1:
                continue
            raise _unsupported(f"Canvas model does not expose --{name}")
        allowed = rule.get("values")
        if isinstance(allowed, list):
            match = next((x for x in allowed if str(x).lower() == str(value).lower()), None)
            if match is None:
                raise _unsupported(f"Canvas model does not support {name}={value}")
            value = match
        number = _number(value)
        if any(x in rule for x in ("min", "max", "step")):
            if number is None or number < float(rule.get("min", 0)) or number > float(rule.get("max", number)):
                raise _unsupported(f"Canvas model does not support {name}={value}")
            step = float(rule.get("step") or 0)
            if step and abs((number - float(rule.get("min", 0))) / step - round((number - float(rule.get("min", 0))) / step)) > 1e-6:
                raise _unsupported(f"Canvas model does not support {name}={value}")
        result.extend(["--" + name, str(value)])
    return result


def prompt_with_resources(prompt: str, resources: dict[str, list[str]]) -> str:
    # Caller text is not allowed to inject another account's Canvas locator.
    prompt = prompt.replace("{{", "\\{{")
    def replace(match: re.Match[str]) -> str:
        kind = media_alias_kind(match.group("kind"))
        index = int(match.group("index")) - 1
        ids = resources.get(kind, [])
        return "{{res:" + ids[index] + "}}" if 0 <= index < len(ids) else match.group(0)
    return ALIASES.sub(replace, prompt)


class CanvasClient:
    def __init__(self, runner: CanvasCLI, account: dict[str, Any], *,
                 capabilities: dict[str, list[dict[str, Any]]] | None = None) -> None:
        self.runner = runner
        self.account = dict(account)
        self._capabilities = capabilities or {}

    def _call(self, args: list[str], *, timeout: float | None = None,
              read_only: bool = False) -> dict[str, Any]:
        for attempt in range(3):
            try:
                return _data(self.runner.run(self.account, args, timeout=timeout))
            except CanvasCLIError as exc:
                error = exc.details.get("error", {})
                transient = (exc.code in {"CLI_COMMAND_TIMEOUT", "CLI_INVALID_RESPONSE"}
                             or bool(error.get("retryable")) and error.get("reason") in _TRANSIENT)
                if not read_only or attempt == 2 or not transient:
                    raise
                time.sleep(0.25 * 2 ** attempt)
        raise AssertionError("unreachable")

    def identity(self) -> dict[str, Any]:
        data = self._call(["auth", "account"], read_only=True)
        user_id = str(data.get("userId") or "")
        if not user_id:
            raise CanvasCLIError("CLI_IDENTITY_INVALID", "Canvas did not return a service-authenticated user ID")
        expected = str(self.account.get("login_user_id") or "")
        if expected and user_id != expected:
            raise CanvasCLIError("CLI_ACCOUNT_MISMATCH", "Canvas authorization belongs to another account")
        credits = data.get("credits") if isinstance(data.get("credits"), dict) else {}
        balance = _number(credits.get("available", data.get("availableCredits")))
        return {"user_id": user_id, "id": user_id, "name": str(data.get("displayName") or ""),
                "available_balance": balance, "credits": balance,
                "buckets": {"details": credits.get("details", [])},
                "membership": {k: data[k] for k in ("isVip", "vipLevel", "vipLevelStartsAt", "vipLevelExpiresAt") if k in data},
                "raw": data}

    def auth_status(self) -> dict[str, Any]:
        return self._call(["auth", "status"])

    def auth_start(self, force: bool = False) -> dict[str, Any]:
        return self._call(["auth", "login", *(["--force"] if force else [])])

    def auth_wait(self, device_code: str, timeout: float = 10) -> dict[str, Any]:
        budget = max(1, min(600, int(timeout)))
        return self._call(["auth", "wait", "--device-code", device_code, "--timeout", f"{budget}s"], timeout=budget + 15)

    def auth_refresh(self) -> dict[str, Any]:
        return self._call(["auth", "refresh"])

    def capabilities(self, kind: str = "video") -> list[dict[str, Any]]:
        if kind not in {"image", "video"}:
            raise ValueError("Canvas capability type must be image or video")
        if kind not in self._capabilities:
            data = self._call(["model", "list", "--type", kind], read_only=True)
            items = data.get("items")
            if not isinstance(items, list):
                raise CanvasCLIError("CLI_CAPABILITIES_INVALID", "Canvas returned an invalid model catalog")
            self._capabilities[kind] = [x for x in items if isinstance(x, dict) and x.get("model")]
        return self._capabilities[kind]

    def _ensure_project(self, context: dict[str, Any]) -> None:
        wanted = context["project_id"]
        args = ["canvas", "ls", "--limit", "100"]
        for _ in range(20):
            data = self._call(args, read_only=True)
            if any(x.get("projectId") == wanted for x in data.get("items", []) if isinstance(x, dict)):
                return
            if not data.get("hasMore") or not data.get("nextCursor"):
                break
            args = ["canvas", "ls", "--limit", "100", "--cursor", str(data["nextCursor"])]
        try:
            self._call(["canvas", "create", f"IJM account {self.account['id']}", "--project-id", wanted])
        except CanvasCLIError:
            # Creation is idempotent by the already persisted project UUID.
            # Re-read once after a lost response; never allocate another ID.
            data = self._call(["canvas", "ls", "--limit", "100"], read_only=True)
            if not any(x.get("projectId") == wanted for x in data.get("items", []) if isinstance(x, dict)):
                raise

    def _resource_ready(self, resource_id: str, project_id: str, *, timeout: float = 120) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while True:
            data = self._call(["resource", "get", resource_id, "--project-id", project_id], read_only=True)
            state = str(data.get("status") or "").lower()
            if state in {"success", "succeeded", "completed"}:
                return data
            if state in {"failed", "error", "canceled", "cancelled", "rejected"}:
                raise CanvasCLIError("MEDIA_UPLOAD_FAILED", f"Canvas reference upload failed ({data.get('errorCode', state)})")
            if time.monotonic() >= deadline:
                raise CanvasCLIError("MEDIA_UPLOAD_PENDING", "Canvas reference upload is not ready; retained original resource ID")
            time.sleep(1)

    def _upload_resource(self, path: Path, media_type: str, resource_id: str, project_id: str) -> None:
        """Retries keep the same documented registration idempotency key."""
        for attempt in range(3):
            try:
                self._call(["resource", "upload", "--file", str(path.resolve()),
                            "--type", media_type, "--project-id", project_id,
                            "--resource-id", resource_id], timeout=180)
                self._resource_ready(resource_id, project_id)
                return
            except CanvasCLIError as exc:
                # A lost response can follow successful storage/registration.
                # Observe the persisted ID before another same-ID upload.
                try:
                    self._resource_ready(resource_id, project_id, timeout=5)
                    return
                except CanvasCLIError as check:
                    if check.code == "MEDIA_UPLOAD_FAILED":
                        raise check
                error = exc.details.get("error", {})
                retryable = (exc.code in {"CLI_COMMAND_TIMEOUT", "CLI_INVALID_RESPONSE", "MEDIA_UPLOAD_PENDING"}
                             or bool(error.get("retryable")))
                if attempt == 2 or not retryable:
                    raise
                time.sleep(0.5 * 2 ** attempt)

    def prepare(self, normalized: dict[str, Any], task_id: str,
                assets: dict[str, list[Any]], *,
                persist: Callable[[dict[str, Any]], Any] | None = None) -> dict[str, Any]:
        kind = "image" if normalized.get("kind") == "image" else "video"
        counts = {x: len(assets.get(x, [])) for x in ("images", "videos", "audios")}
        model = select_model(self.capabilities(kind), normalized)
        mode = select_mode(model, normalized, counts)
        flags = generation_flags(mode, normalized)
        old = normalized.get("_execution")
        if isinstance(old, dict) and old.get("backend") == "canvas_cli":
            context = copy.deepcopy(old)
            if context.get("account_id") != int(self.account["id"]):
                raise CanvasCLIError("CLI_ACCOUNT_MISMATCH", "An existing Canvas task cannot move to another account")
        else:
            context = {"backend": "canvas_cli", "account_id": int(self.account["id"]),
                       "profile": self.runner.profile(self.account) if hasattr(self.runner, "profile") else account_profile(self.account),
                       "task_id": task_id, "kind": kind,
                       "output_count": int(normalized.get("count") or 1),
                       "project_id": _stable("account", self.account["id"], self.account.get("login_user_id", "")),
                       "node_id": _node_id(f"{self.account['id']}:{task_id}"),
                       "update_id": _stable(self.account["id"], task_id, "update"),
                       "submit_id": _stable(self.account["id"], task_id, "submit"), "model": str(model["model"]),
                       "mode": str(mode["name"]), "resources": {
                           group: [_stable(self.account["id"], task_id, group, index) for index in range(counts[group])]
                           for group in counts}, "uploaded_resources": [], "resource_ids": [], "phase": "prepared"}
        if context.get("submission_phase") in {"submitting", "accepted", "unknown"}:
            raise CanvasCLIError("UPSTREAM_SUBMIT_OUTCOME_UNKNOWN", "Canvas submission must be resumed by its original submit ID", outcome_unknown=True)
        def save(phase: str) -> None:
            context["phase"] = phase
            if persist:
                persist(copy.deepcopy(context))
        save("preparing")
        self._ensure_project(context)
        for group, media_type in (("images", "image"), ("videos", "video"), ("audios", "audio")):
            for index, asset in enumerate(assets.get(group, [])):
                resource_id = context["resources"][group][index]
                if resource_id not in context["uploaded_resources"]:
                    path = asset.get("path") if isinstance(asset, dict) else asset
                    if not isinstance(path, (str, Path)) or not Path(path).is_file():
                        raise CanvasCLIError("MEDIA_INPUT_MISSING", "Canvas reference file is missing")
                    self._upload_resource(Path(path), media_type, resource_id, context["project_id"])
                    context["uploaded_resources"].append(resource_id)
                    save("uploading")
        args = ["node", "create", kind, "--project-id", context["project_id"],
                "--node-id", context["node_id"], "--update-id", context["update_id"],
                "--title", "IJM " + task_id, "--mode", context["mode"],
                "--model", context["model"], "--prompt",
                prompt_with_resources(str(normalized["prompt"]), context["resources"]), *flags]
        for group in ("images", "videos", "audios"):
            for resource_id in context["resources"][group]:
                args.extend(["--ref", "res:" + resource_id])
        save("saving")
        self._call(args)
        save("quoting")
        quote = self._call(["node", "quote", "--project-id", context["project_id"],
                            "--node-id", context["node_id"]], read_only=True)
        items = quote.get("items")
        if (not isinstance(items, list) or len(items) != 1 or items[0].get("nodeId") != context["node_id"]
                or items[0].get("error") or items[0].get("generationDiff") or not quote.get("confirmable", False)):
            raise CanvasCLIError("CLI_QUOTE_UNAVAILABLE", "Canvas could not quote the complete requested task")
        amount = _number(quote.get("totalMaxCredits"))
        if amount is None or amount != int(amount):
            raise CanvasCLIError("CLI_QUOTE_UNAVAILABLE", "Canvas did not return an authoritative integer credit quote")
        context["quoted_credits"] = context["quote_credit"] = int(amount)
        context["quote"] = {"total_max_credits": int(amount), "draft_version": quote.get("draftVersion"),
                            "confirmation_required": bool(quote.get("confirmationRequired")),
                            "confirmable": bool(quote.get("confirmable"))}
        save("quoted")
        return context

    def submit(self, context: dict[str, Any], ceiling: int) -> dict[str, Any]:
        self._check_context(context)
        if context.get("phase") in {"accepted", "unknown"}:
            raise CanvasCLIError("UPSTREAM_SUBMIT_OUTCOME_UNKNOWN", "An existing Canvas submission must only be queried", outcome_unknown=True)
        if isinstance(ceiling, bool) or not isinstance(ceiling, int) or ceiling < int(context["quoted_credits"]):
            raise CanvasCLIError("CLI_CREDIT_CEILING_EXCEEDED", "Canvas quote exceeds the authorized credit ceiling")
        confirmed = self._call(["node", "confirm", "--project-id", context["project_id"],
                                "--node-id", context["node_id"], "--credit-ceiling", str(ceiling)])
        token = confirmed.get("creditConfirmationToken")
        if not isinstance(token, str) or not token:
            raise CanvasCLIError("CLI_CONFIRMATION_INVALID", "Canvas did not issue a credit confirmation token")
        result = self._call(["node", "run", "--project-id", context["project_id"],
                             "--node-id", context["node_id"], "--submit-id", context["submit_id"],
                             "--credit-token", token])
        items = result.get("items")
        if not isinstance(items, list) or len(items) != 1:
            raise CanvasCLIError("UPSTREAM_SUBMIT_OUTCOME_UNKNOWN", "Canvas acceptance result is incomplete", outcome_unknown=True)
        item = items[0]
        if item.get("nodeId") != context["node_id"] or item.get("submitId") != context["submit_id"]:
            raise CanvasCLIError("UPSTREAM_SUBMIT_OUTCOME_UNKNOWN", "Canvas acceptance identities do not match", outcome_unknown=True)
        state = str(item.get("state") or "").lower()
        if item.get("error") or state in {"failed", "rejected", "canceled", "cancelled"}:
            error = item.get("error") or {}
            raise CanvasCLIError("UPSTREAM_REJECTED", str(error.get("message") or "Canvas rejected the generation"), details={"error": error})
        if state not in {"accepted", "running", "in_progress", "processing", "completed", "succeeded", "success"}:
            raise CanvasCLIError("UPSTREAM_SUBMIT_OUTCOME_UNKNOWN", "Canvas returned an uncertain submission state", outcome_unknown=True)
        context = copy.deepcopy(context)
        context["resource_ids"] = [str(x["resourceId"]) for x in item.get("resources", []) if x.get("resourceId")]
        context["phase"] = "accepted"
        return context

    def download_result(self, context: dict[str, Any], resource_id: str, media_type: str) -> dict[str, Any]:
        if (not re.fullmatch(r"[A-Za-z0-9_-]{1,120}", str(context["task_id"]))
                or not re.fullmatch(r"[a-f0-9-]{36}", resource_id)):
            raise CanvasCLIError("CLI_RESULT_INVALID", "Canvas result identifiers are invalid")
        base = self.runner.account_directory(self.account) / "results" / context["task_id"]
        base.mkdir(parents=True, exist_ok=True, mode=0o700)
        suffix = ".mp4" if media_type == "video" else ".png"
        path = base / (resource_id + suffix)
        if path.is_file() and path.stat().st_size:
            return {"path": str(path), "mime": _file_mime(path, media_type),
                    "resource_id": resource_id}
        data = self._call(["resource", "download", resource_id, "--project-id", context["project_id"],
                           "--output", str(path)], timeout=180, read_only=True)
        returned = Path(str(data.get("path") or "")).resolve()
        if returned != path.resolve() or not path.is_file() or path.stat().st_size <= 0:
            raise CanvasCLIError("CLI_RESULT_DOWNLOAD_FAILED", "Canvas result download did not create the expected original media file")
        expected_hash = data.get("sha256")
        if expected_hash and hashlib.sha256(path.read_bytes()).hexdigest() != expected_hash:
            raise CanvasCLIError("CLI_RESULT_DOWNLOAD_FAILED", "Canvas result download checksum did not match")
        return {"path": str(path), "mime": _file_mime(path, media_type),
                "resource_id": resource_id}

    def _failure_details(self, context: dict[str, Any], data: dict[str, Any],
                         resources: list[dict[str, Any]], failed: list[dict[str, Any]]) -> dict[str, Any]:
        error = data.get("submissionError") or (failed[0] if failed else {})
        code = error.get("serviceCode") or error.get("errorCode") or error.get("code") or ""
        message = str(error.get("message") or error.get("reason") or "")
        facts = []
        ids = list(dict.fromkeys([str(x.get("resourceId") or "") for x in resources]
                                 + list(context.get("resource_ids") or [])))
        for resource_id in ids:
            if not resource_id:
                continue
            try:
                resource = self._call(["resource", "get", resource_id,
                    "--project-id", context["project_id"]], timeout=10, read_only=True)
                if resource.get("resourceId") != resource_id:
                    continue
                if not code and resource.get("errorCode"):
                    code = resource["errorCode"]
                amount = _number(resource.get("creditsAmount"))
                if amount is not None:
                    facts.append({"source": "precharge", "amount": amount,
                        "field": "resource.creditsAmount", "resource_id": resource_id,
                        "submit_id": context["submit_id"], "settled": False})
            except CanvasCLIError:
                pass  # A failed diagnostic read cannot erase a known terminal failure.
        if not code:
            try:
                view = self._call(["node", "show", "--project-id", context["project_id"],
                    "--node-id", context["node_id"]], timeout=10, read_only=True)
                for item in view.get("nodes", []):
                    node = item.get("node") or {}
                    if node.get("nodeId") != context["node_id"]:
                        continue
                    for resource in node.get("resources", []):
                        if resource.get("submitId") == context["submit_id"] and resource.get("errorCode"):
                            code = resource["errorCode"]
                            break
                    if not code and item.get("errorCode"):
                        code = item["errorCode"]
            except CanvasCLIError:
                pass
        return {"status": "failed", "progress": 100, "error_code": _failure_code(code, message),
                "error_message": message or f"Canvas generation failed ({code or data.get('state') or 'unknown'})",
                "upstream_error_code": str(code or ""), "credit_facts": facts}

    def poll(self, context: dict[str, Any]) -> dict[str, Any]:
        self._check_context(context)
        data = self._call(["operation", "status", context["submit_id"],
                           "--project-id", context["project_id"]], read_only=True)
        if data.get("operationRef") and data["operationRef"] != context["submit_id"]:
            raise CanvasCLIError("UPSTREAM_SUBMIT_OUTCOME_UNKNOWN", "Canvas returned another submission identity", outcome_unknown=True)
        state = str(data.get("state") or "").lower()
        submission = data.get("submission") if isinstance(data.get("submission"), dict) else {}
        result: dict[str, Any] = {"status": "running", "progress": 45, "result_urls": [],
                                  "result_paths": [], "result_files": [], "credit_facts": [],
                                  "error_code": "", "error_message": ""}
        if state in {"absent", "unknown"} or submission.get("state") in {"absent", "unknown"}:
            result.update(status="unknown", error_code="UPSTREAM_SUBMIT_OUTCOME_UNKNOWN",
                          error_message="Canvas submission could not be resolved; automatic resubmission is disabled")
            return result
        resources = data.get("resources") if isinstance(data.get("resources"), list) else []
        failed = [x for x in resources if str(x.get("state") or "").lower() in {"failed", "rejected", "error", "canceled", "cancelled"}]
        if state in {"failed", "partial_failed", "rejected", "error", "canceled", "cancelled"} or failed or data.get("submissionError"):
            result.update(self._failure_details(context, data, resources, failed))
            return result
        terminal = state in {"completed", "success", "succeeded"}
        successful = [x for x in resources if str(x.get("state") or "").lower() in {"success", "succeeded", "completed"}]
        if not terminal or not resources or len(successful) != len(resources):
            return result
        if len(resources) != int(context.get("output_count") or 1):
            result.update(status="failed", progress=100, error_code="CLI_RESULT_INCOMPLETE",
                          error_message="Canvas completed with an unexpected number of generated outputs")
            return result
        for resource in resources:
            resource_id = str(resource.get("resourceId") or "")
            if not resource_id:
                raise CanvasCLIError("CLI_RESULT_INVALID", "Canvas completed a task without its output resource ID")
            facts = self._call(["resource", "get", resource_id, "--project-id", context["project_id"]], read_only=True)
            if facts.get("resourceId") != resource_id or facts.get("type") != context["kind"]:
                raise CanvasCLIError("CLI_RESULT_INVALID", "Canvas returned another output resource or media type")
            amount = _number(facts.get("creditsAmount"))
            if amount is not None:
                result["credit_facts"].append({"source": "precharge", "amount": amount,
                    "field": "resource.creditsAmount", "resource_id": resource_id,
                    "submit_id": context["submit_id"], "settled": False})
            downloaded = self.download_result(context, resource_id, str(facts.get("type") or context["kind"]))
            result["result_files"].append(downloaded)
            result["result_paths"].append(downloaded["path"])
        result.update(status="succeeded", progress=100)
        return result

    def _check_context(self, context: dict[str, Any]) -> None:
        if context.get("account_id") != int(self.account["id"]):
            raise CanvasCLIError("CLI_ACCOUNT_MISMATCH", "Canvas command account does not match its durable task")
        if hasattr(self.runner, "profile") and context.get("profile") != self.runner.profile(self.account):
            raise CanvasCLIError("CLI_ACCOUNT_MISMATCH", "Canvas task belongs to a retired credential profile")
