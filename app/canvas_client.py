"""Official Canvas 1.2.1 commands mapped to IJM's durable task lifecycle."""

from __future__ import annotations

import copy
import hashlib
import ipaddress
import json
import math
import os
import re
import stat
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Event
from contextlib import contextmanager, nullcontext
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from .canvas_cli import CanvasCLI, CanvasCLIError, _has_accepted, account_profile, admission_verdict, sanitize
from .catalog import ALIASES, media_alias_kind
from .errors import public_error, upstream_review_error
from .canvas_upload_limiter import UPLOAD_LIMITER, runner_namespace
from .canvas_input_recovery import (TICKET_KEY, digest as input_digest, file_identity,
                                    remaining_deadline, safe_error, validated_tickets)
from .canvas_operation_recovery import recover_absent_operation
from .canvas_resource_recovery import (SOURCE as ACCEPTED_RESOURCE_SOURCE,
    accepted_output_context, observe_bound_resources, resource_observation_retryable)
from .canvas_validation import (MATERIAL_REQUIREMENTS_ERROR, MATERIAL_REQUIREMENTS_MESSAGE,
    material_validation_from_quote)


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
_TRANSPORT_RETRY_READS = frozenset({('auth', 'account'), ('model', 'list'), ('canvas', 'ls'),
    ('node', 'quote'), ('node', 'show'), ('resource', 'get'), ('operation', 'status')})
_PREPARATION_RETRY_READS = frozenset({('auth', 'account'), ('model', 'list'),
    ('canvas', 'ls'), ('node', 'quote'), ('resource', 'get')})
_SUBMISSION_EVIDENCE_FLAGS = ('accepted', 'output_resources', 'receipt', 'identity_conflict', 'ambiguous_partial')


def submission_evidence_blocks_fallback(value: dict[str, Any]) -> bool:
    """A legacy partial-response bit is not affirmative acceptance evidence."""
    evidence = value.get('submission_evidence')
    if evidence is None:
        return False
    if (not isinstance(evidence, dict) or type(evidence.get('version')) is not int
            or evidence['version'] != 1 or set(evidence) != {'version', *_SUBMISSION_EVIDENCE_FLAGS}
            or any(type(evidence.get(key)) is not bool for key in _SUBMISSION_EVIDENCE_FLAGS)):
        return True  # An incompatible evidence format cannot grant failover.
    return any(evidence[key] for key in _SUBMISSION_EVIDENCE_FLAGS if key != 'ambiguous_partial')


def _upload_dispatch_evidence(value):
    if not isinstance(value, dict):
        return False
    error = value.get('submit_error')
    evidence = value.get('submission_evidence')
    return (value.get('submission_phase') in {'submitting', 'confirming', 'run_dispatching', 'submitted', 'accepted', 'unknown'}
        or value.get('phase') in {'accepted', 'unknown', 'submitted', 'running', 'succeeded'}
        or any(value.get(key) for key in ('run_id', 'thread_id', 'resource_ids', 'accepted',
            'submission_receipt_evidence', 'dispatch_started'))
        or isinstance(error, dict) and error.get('outcome_unknown') is True
        or evidence is not None)


def _remember_submission_evidence(context: dict[str, Any], envelope: dict[str, Any]) -> None:
    """Keep only monotonic, safe receipt facts before the raw envelope is redacted.

    A partial/unknown response still means the paid outcome is unknown. It is
    distinct from an accepted state, output identity or contradictory binding.
    Neither category authorizes another node run on this task.
    """
    evidence = {'version': 1, **{key: False for key in _SUBMISSION_EVIDENCE_FLAGS}}
    evidence['ambiguous_partial'] = bool(envelope.get('partialData')) or _has_accepted(envelope)
    error = envelope.get('error')
    if isinstance(error, dict) and any(error.get(key) for key in ('resources', 'submission', 'items', 'accepted')):
        evidence['ambiguous_partial'] = True
    bindings = {'projectId': context.get('project_id'), 'nodeId': context.get('node_id'),
                'submitId': context.get('submit_id'), 'operationRef': context.get('submit_id')}
    pending = [envelope]
    while pending:
        value = pending.pop()
        if isinstance(value, list):
            pending.extend(value)
        elif isinstance(value, dict):
            for key, content in value.items():
                if key in bindings and content not in (None, '') and content != bindings[key]:
                    evidence['identity_conflict'] = True
                if (key in {'state', 'submissionState', 'status'} and isinstance(content, str)
                        and content.strip().upper().removeprefix('RUN_') in {
                            'ACCEPTED', 'SUBMITTED', 'RUNNING', 'PROCESSING', 'SUCCEEDED', 'COMPLETED', 'SUCCESS'}):
                    evidence['accepted'] = True
                if key == 'accepted' and content is True:
                    evidence['accepted'] = True
                if key in {'resources', 'resourceIds', 'resource_ids'} and isinstance(content, list) and content:
                    evidence['output_resources'] = True
                if key == 'resourceId' and content:
                    evidence['output_resources'] = True
                if key in {'receipt', 'run_receipt', 'runReceipt', 'generationId', 'generation_id'} and content:
                    evidence['receipt'] = True
                # Journal spec/references describe the input, not output resources.
                if key not in {'spec', 'references', 'request', 'arguments', 'params', 'validation'} and isinstance(content, (dict, list)):
                    pending.append(content)
    previous = context.get('submission_evidence')
    if previous is not None:
        if (not isinstance(previous, dict) or type(previous.get('version')) is not int
                or previous['version'] != 1 or set(previous) != set(evidence)
                or any(type(previous.get(key)) is not bool for key in _SUBMISSION_EVIDENCE_FLAGS)):
            evidence['receipt'] = True
        else:
            for key in _SUBMISSION_EVIDENCE_FLAGS:
                evidence[key] = evidence[key] or previous[key]
    context['submission_evidence'] = evidence
    if any(evidence[key] for key in _SUBMISSION_EVIDENCE_FLAGS):
        # Preserve the old diagnostic marker for stored/debug compatibility.
        context['submission_receipt_evidence'] = True


def preparation_service_unavailable(exc: CanvasCLIError) -> bool:
    """The observed typed service.1 is retryable, never a paid-run verdict."""
    error = exc.details.get('error')
    return (exc.code == 'service.1' and not exc.outcome_unknown
        and isinstance(error, dict) and error.get('code') in (None, 'service.1')
        and type(error.get('serviceCode')) in (int, str) and error['serviceCode'] in (1, '1')
        and error.get('class') == 'retryable_failure' and error.get('retryable') is True
        and error.get('requiredAction') == 'retry'
        and str(error.get('reason') or '') in {'', 'service_unavailable'}
        and not any(error.get(key) for key in ('resources', 'submission', 'items', 'accepted')))


def preparation_transport_failure(exc: CanvasCLIError) -> bool:
    """Typed transport failure before paid dispatch, never permission to replay run."""
    details = exc.details
    error = details.get('error') if isinstance(details, dict) else None
    if (exc.outcome_unknown or exc.code != 'cli.transport_failed'
            or not isinstance(error, dict) or error.get('code') != 'cli.transport_failed'
            or error.get('class') != 'retryable_failure' or error.get('retryable') is not True
            or error.get('requiredAction') != 'retry' or error.get('serviceCode') not in (None, '')
            or str(error.get('reason') or '') not in {'', *_TRANSIENT}):
        return False
    # A structured error may carry a partial operation/receipt. A local
    # transport label cannot override that evidence or a dispatch boundary.
    local_storages = [value['localStorage'] for value in (details, error) if value.get('localStorage')]
    if any(not isinstance(value, dict) or value.get('submissionPhase') != 'before_submit'
           for value in local_storages):
        return False
    for value in (details, error, *local_storages):
        if (value.get('dispatch_started') is True or value.get('outcome_unknown') is True
                or value.get('phase') in {'run_dispatching', 'submitted', 'accepted', 'unknown'}
                or any(value.get(key) for key in ('partialData', 'data', 'accepted', 'resources',
                    'items', 'submission', 'receipt', 'run_receipt', 'run_data', 'operation',
                    'operationRef', 'operation_ref', 'operation_id', 'operationId', 'resource_ids',
                    'resourceIds', 'run_id', 'runId', 'thread_id', 'threadId',
                    'submission_evidence', 'submission_receipt_evidence'))):
            return False
    return True


def preparation_retryable_failure(exc: CanvasCLIError) -> bool:
    """Known service/transport/decoding failures before a generation command."""
    if preparation_service_unavailable(exc) or preparation_transport_failure(exc):
        return True
    error = exc.details.get('error') or {}
    return (not exc.outcome_unknown
        and exc.code in {'cli.invalid_service_response', 'CLI_INVALID_RESPONSE'}
        and isinstance(error, dict) and error.get('code') in (None, exc.code)
        and error.get('class') in (None, '', 'internal_failure')
        and error.get('requiredAction') in (None, '', 'retry', 'contact_support')
        and error.get('serviceCode') in (None, '')
        and not any(error.get(key) for key in ('resources', 'submission', 'items', 'accepted'))
        and not exc.details.get('partialData'))


def _source_url_fallback_allowed(exc: CanvasCLIError) -> bool:
    """A local file is an alternate input transport, not a policy/auth retry."""
    if exc.outcome_unknown:
        return False
    error = exc.details.get('error') or {}
    if not isinstance(error, dict):
        return False
    if error.get('class') in {'permanent_failure', 'business_failure', 'human_intervention'}:
        return False
    facts = ' '.join(str(value).lower() for value in
                     (exc.code, error.get('code'), error.get('class'), error.get('reason')))
    if (any(word in facts for word in ('auth', 'credential', 'identity', 'permission',
            'policy', 'moderation', 'copyright', 'content_rejected', 'invalid_input',
            'invalid_argument', 'validation', 'insufficient', 'credit', 'balance'))
            or error.get('requiredAction') not in (None, '', 'none', 'retry', 'contact_support')):
        return False
    if preparation_retryable_failure(exc):
        return True
    if 'retryable' in error and error['retryable'] is not True:
        return False
    return (exc.code == 'CLI_COMMAND_TIMEOUT' and error.get('serviceCode') in (None, '')
        and not exc.details.get('partialData')
        and not any(error.get(key) for key in ('resources', 'submission', 'items', 'accepted')))


def _read_only_transport_retry(args: list[str], exc: CanvasCLIError) -> bool:
    """An exact read may reconnect after a typed transport failure."""
    if exc.code != 'cli.transport_failed' or tuple(args[:2]) not in _TRANSPORT_RETRY_READS:
        return False
    error = exc.details.get('error') or {}
    if not isinstance(error, dict) or error.get('retryable') is False:
        return False
    if error.get('serviceCode') not in (None, ''):
        return False
    if str(error.get('requiredAction') or '').lower() not in {'', 'retry'}:
        return False
    if str(error.get('code') or '') not in {'', 'cli.transport_failed'}:
        return False
    if str(error.get('class') or '').lower() not in {
            '', 'transport_error', 'transport_failure', 'network_error', 'network_failure', 'internal_failure', 'retryable_failure'}:
        return False
    reason = str(error.get('reason') or '').lower()
    return not reason or reason in _TRANSIENT


def identity_network_error(exc: CanvasCLIError) -> bool:
    """Explicit read-only transport errors, excluding auth and business errors."""
    code = str(exc.code).upper()
    if exc.outcome_unknown or any(word in code for word in (
            'AUTH', 'IDENTITY', 'CREDENTIAL', 'MISMATCH', 'EXPIRED', 'UNKNOWN')):
        return False
    error = exc.details.get('error') or {}
    if not isinstance(error, dict) or code.isdigit() or str(error.get('serviceCode') or '').isdigit():
        return False
    if code == 'CLI.TRANSPORT_FAILED':
        return preparation_transport_failure(exc)
    return code == 'CLI_COMMAND_TIMEOUT' or (error.get('retryable') is True
        and str(error.get('reason') or '').lower() in {
            'network_timeout', 'dns_resolution_failed', 'network_error', 'transport_error'})


def _resource_read_intervention(exc: CanvasCLIError) -> bool:
    error = exc.details.get('error') or {}
    if not isinstance(error, dict):
        return False
    action = str(error.get('requiredAction') or '').lower()
    classification = ' '.join([str(exc.code).lower(),
        *(str(error.get(key) or '').lower() for key in ('class', 'reason'))])
    return (action not in {'', 'none', 'retry', 'fix_input'}
        or any(word in classification for word in ('human_intervention', 'auth', 'identity', 'mismatch', 'permission')))


def _planned_resource_not_found(exc: CanvasCLIError, resource_id: str) -> bool:
    """Observe registration absence only for a previously planned upload ID."""
    error = exc.details.get('error') or {}
    return (exc.code == 'service.6' and not exc.outcome_unknown and isinstance(error, dict)
        and error.get('code') in (None, 'service.6')
        and error.get('serviceCode') in (6, '6') and error.get('class') == 'invalid_input'
        and str(error.get('message') or exc.message).strip() == 'The requested resource was not found.'
        and str(error.get('reason') or '').lower() in {'', 'not_found', 'resource_not_found'}
        and all(error.get(key) in (None, '', resource_id) for key in ('resourceId', 'resource_id'))
        and not _resource_read_intervention(exc))


def safe_cli_diagnostic(exc: CanvasCLIError, *, secrets: tuple[str, ...] = ()) -> dict[str, Any]:
    """Keep failure facts without persisting an envelope or its credentials."""
    source = exc.details.get("error", exc.details)
    source = source if isinstance(source, dict) else {}
    result = {}
    for key in ("code", "class", "reason", "serviceCode", "traceId", "message", "requiredAction", "retryable"):
        value = source.get(key)
        if isinstance(value, (str, int, float, bool)):
            result[key] = sanitize(value)
    result.setdefault("code", exc.code)
    result.setdefault("message", exc.message)
    result["exit_code"] = exc.exit_code
    result["outcome_unknown"] = exc.outcome_unknown
    for key, value in result.items():
        if isinstance(value, str):
            for secret in secrets:
                if secret:
                    value = value.replace(secret, "[redacted]")
            result[key] = sanitize(value)
    return result


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) and result >= 0 else None


def _available_balance(data: dict[str, Any]) -> float | None:
    """Only the CLI's explicit aggregate available-credit facts are balances.

    auth account can succeed with userId/membership alone when its optional
    credit dependency is unavailable. Never substitute cached credit, sum an
    incomplete expiry list, or turn those omitted facts into zero.
    """
    credits = data.get("credits") if isinstance(data.get("credits"), dict) else {}
    values = [_number(credits.get("available")), _number(data.get("availableCredits"))]
    known = [value for value in values if value is not None]
    return known[0] if known and all(value == known[0] for value in known) else None


def _key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value).lower())


def _stable(*parts: Any) -> str:
    return str(uuid.uuid5(_NAMESPACE, ":".join(map(str, parts))))


def public_image_source_url(value: Any) -> str:
    """Keep remote-image imports on public HTTP(S) locators, without fetching."""
    try:
        if (not isinstance(value, str) or not value or len(value) > 16384
                or '\\' in value or any(ord(char) <= 32 or ord(char) == 127 for char in value)):
            raise ValueError
        parsed = urlsplit(value)
        host = (parsed.hostname or '').rstrip('.').lower()
        if (parsed.scheme not in {'http', 'https'} or not host or parsed.username is not None
                or parsed.password is not None or '%' in host
                or parsed.port is not None and not 1 <= parsed.port <= 65535
                or host in {'localhost', 'metadata', 'instance-data'}
                or host.endswith(('.localhost', '.local', '.internal', '.lan', '.home'))):
            raise ValueError
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            if '.' not in host or re.fullmatch(r'(?:0x[0-9a-f]+|[0-9]+)(?:\.(?:0x[0-9a-f]+|[0-9]+)){0,3}', host):
                raise ValueError
        else:
            mapped = getattr(address, 'ipv4_mapped', None)
            if not address.is_global or mapped is not None and not mapped.is_global:
                raise ValueError
    except (TypeError, ValueError):
        raise CanvasCLIError('MEDIA_DOWNLOAD_FAILED', 'Canvas reference image requires a public HTTP(S) URL') from None
    return value


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


def _unsupported(message: str, *, details: dict[str, Any] | None = None) -> CanvasCLIError:
    return CanvasCLIError("CLI_CAPABILITY_UNAVAILABLE", message + "; select browser submission mode manually",
                          details=details)


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
    if not isinstance(items, list) or any(not isinstance(item, dict)
            or not isinstance(item.get("model"), str) or not item["model"].strip() for item in items):
        raise CanvasCLIError("CLI_CAPABILITIES_INVALID", "Canvas returned an invalid model catalog")
    candidates = [_key(x) for x in _model_candidates(normalized) if x]
    for candidate in candidates:
        matches = [item for item in items if candidate in {
            _key(item.get("model")), _key(item.get("displayName")),
            *(_key(alias) for alias in (item.get("aliases") or []) if isinstance(alias, str))}]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise _unsupported("Canvas model discovery returned an ambiguous model mapping")
    requested = str(normalized.get("model") or "")
    raise _unsupported(f"Canvas does not expose the requested model {requested}", details={"error": {
        "scope": "account", "reason": "model_not_in_catalog", "requested_model": requested,
        "kind": "image" if normalized.get("kind") == "image" else "video"}})


def select_mode(model: dict[str, Any], normalized: dict[str, Any], counts: dict[str, int]) -> dict[str, Any]:
    image = normalized.get("kind") == "image"
    has_refs = any(counts.values())
    desired = ("i2i" if has_refs else "t2i") if image else ("m2v" if has_refs else "t2v")
    requested = str(normalized.get("mode") or "")
    if not image and requested in {"i2v", "i2v_first_last", "first_last_frame"}:
        desired = "first_last_frame"
    elif not image and requested in {"t2v", "m2v"}:
        desired = requested
    modes = [mode for mode in (model.get("modes") or [])
             if isinstance(mode, dict) and mode.get("name") == desired]
    if not modes:
        raise _unsupported(f"Canvas model does not expose {desired} mode")
    # The real catalog has two first_last_frame entries: one for a single
    # first frame, one for first+last. Their public name is intentionally the
    # same; resolve by declared media constraints, never by array order.
    valid = []
    errors = []
    for mode in modes:
        error = _reference_error(mode, counts)
        if error:
            errors.append(error)
        else:
            valid.append(mode)
    if len(valid) != 1:
        message = errors[-1] if not valid and errors else f"Canvas model exposes an ambiguous {desired} mode"
        raise _unsupported(message)
    return valid[0]


def _reference_error(mode: dict[str, Any], counts: dict[str, int]) -> str | None:
    references = mode.get("references")
    if isinstance(references, dict):
        total = sum(counts.values())
        if total < int(references.get("min", 0)) or total > int(references.get("max", total)):
            return "Canvas model reference count is outside its live limits"
        types = references.get("types") or []
        for media_type, plural in (("image", "images"), ("video", "videos"), ("audio", "audios")):
            rule = next((x for x in types if x.get("mediaType") == media_type), None)
            if rule is None and counts[plural]:
                return f"Canvas model does not accept {media_type} references"
            if rule is not None and not int(rule.get("min", 0)) <= counts[plural] <= int(rule.get("max", counts[plural])):
                return f"Canvas model {media_type} reference count is outside its live limits"
        required = references.get("requireAnyOfTypes") or []
        if required and not any(counts.get(str(x) + "s", 0) for x in required):
            return "Canvas model requires another reference media type"
        exclusive = references.get("mutuallyExclusiveTypes") or []
        if sum(bool(counts.get(str(x) + "s", 0)) for x in exclusive) > 1:
            return "Canvas model does not allow this reference media combination"
    elif any(counts.values()):
        return "Canvas model does not declare support for these references"
    return None


def generation_flags(mode: dict[str, Any], normalized: dict[str, Any]) -> list[str]:
    requested = {"ratio": normalized.get("aspect_ratio"), "resolution": normalized.get("resolution"),
                 "count": normalized.get("count", 1)}
    if normalized.get("kind") != "image":
        requested["duration"] = normalized.get("duration")
    rules = {str(x.get("flag", "")).lstrip("-"): x for x in (mode.get("flags") or []) if isinstance(x, dict)}
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
                 capabilities: dict[str, list[dict[str, Any]]] | None = None,
                 upload_concurrency: int = 1, account_upload_concurrency: int = 4,
                 total_upload_concurrency: int = 8) -> None:
        for value, maximum in ((upload_concurrency, 8), (account_upload_concurrency, 16),
                               (total_upload_concurrency, 32)):
            if type(value) is not int or not 1 <= value <= maximum:
                raise ValueError('Canvas upload concurrency is out of range')
        self.runner = runner
        self.account = dict(account)
        self._capabilities = capabilities or {}
        self.preparation_timing = None
        self.upload_concurrency = upload_concurrency
        self.account_upload_concurrency = account_upload_concurrency
        self.total_upload_concurrency = total_upload_concurrency

    def _parallel_uploads(self):
        return self.upload_concurrency > 1 and (not isinstance(self.runner, CanvasCLI)
            or self.runner._parallel_verified)

    def _account_guard(self, deadline):
        return (self.runner.account_guard(self.account, deadline=deadline)
                if isinstance(self.runner, CanvasCLI) else nullcontext())

    def _phase(self, name, **metadata):
        return self.preparation_timing.phase(name, **metadata) if self.preparation_timing is not None else nullcontext()

    @staticmethod
    def _remaining(deadline: float | None, timeout: float | None = None) -> float | None:
        if deadline is None:
            return timeout
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise CanvasCLIError("CLI_TASK_DEADLINE_EXCEEDED", "Canvas task observation deadline was reached")
        return min(remaining, timeout) if timeout is not None else remaining

    @classmethod
    def _sleep(cls, delay: float, deadline: float | None) -> None:
        time.sleep(cls._remaining(deadline, delay))
        cls._remaining(deadline)

    def _call(self, args: list[str], *, timeout: float | None = None,
              read_only: bool = False, deadline: float | None = None) -> dict[str, Any]:
        for attempt in range(3):
            try:
                budget = self._remaining(deadline, timeout if timeout is not None
                                         else getattr(self.runner, 'timeout', 120) if deadline is not None else None)
                kwargs = {'timeout': budget}
                if deadline is not None:
                    kwargs['deadline'] = deadline
                invoke = lambda: _data(self.runner.run(self.account, args, **kwargs))
                result = (self.preparation_timing.command(self.runner, self.account, args, kwargs, attempt + 1, invoke)
                          if self.preparation_timing is not None else invoke())
                # A paid receipt must remain accepted even if processing it
                # reaches the clock boundary. submit() owns that safety edge.
                if args[:2] not in (["node", "confirm"], ["node", "run"]):
                    self._remaining(deadline)
                return result
            except CanvasCLIError as exc:
                error = exc.details.get("error", {})
                transient = (exc.code in {"CLI_COMMAND_TIMEOUT", "CLI_INVALID_RESPONSE", "cli.invalid_service_response"}
                             or bool(error.get("retryable")) and error.get("reason") in _TRANSIENT
                             or read_only and _read_only_transport_retry(args, exc)
                             or (tuple(args[:2]) in _PREPARATION_RETRY_READS
                                 and preparation_service_unavailable(exc)))
                if exc.code == 'cli.transport_failed':
                    # Explicit auth/business/action vetoes in the transport
                    # contract must not be bypassed by a generic network reason.
                    transient = read_only and _read_only_transport_retry(args, exc)
                if not read_only or attempt == 2 or not transient:
                    raise
                self._sleep(0.25 * 2 ** attempt, deadline)
        raise AssertionError("unreachable")

    def _preparation_write(self, args: list[str], *, deadline: float | None = None) -> dict[str, Any]:
        """At most three same-ID draft writes; never a generation command."""
        pair = tuple(args[:2])
        required = ('--project-id', '--node-id', '--update-id') if pair == ('node', 'create') else ('--project-id',)
        if (pair not in {('canvas', 'create'), ('node', 'create')}
                or any(arg.split('=', 1)[0] in {'--run', '--submit-id', '--credit-token'} for arg in args)
                or any(args.count(flag) != 1 or args.index(flag) + 1 >= len(args) or not args[args.index(flag) + 1]
                       for flag in required)):
            raise CanvasCLIError('CLI_PREPARATION_WRITE_INVALID', 'Canvas preparation retry requires its original draft IDs')
        for attempt in range(3):
            try:
                return self._call(args, deadline=deadline)
            except CanvasCLIError as exc:
                if attempt == 2 or not preparation_retryable_failure(exc):
                    raise
                self._sleep(0.25 * 2 ** attempt, deadline)
        raise AssertionError('unreachable')

    def identity(self, *, deadline: float | None = None) -> dict[str, Any]:
        validated = None
        for attempt in range(3):
            try:
                data = self._call(["auth", "account"], timeout=10, read_only=(attempt == 0), deadline=deadline)
            except CanvasCLIError as exc:
                if validated is None or not identity_network_error(exc):
                    raise
                # Credit fields are optional. A failed follow-up cannot erase
                # the identity confirmed by this invocation's service reply.
                self._remaining(deadline)
                data = validated
                balance = None
                break
            user_id = str(data.get("userId") or "")
            if not user_id:
                raise CanvasCLIError("CLI_IDENTITY_INVALID", "Canvas did not return a service-authenticated user ID")
            expected = str(self.account.get("login_user_id") or "")
            if expected and user_id != expected:
                raise CanvasCLIError("CLI_ACCOUNT_MISMATCH", "Canvas authorization belongs to another account")
            if validated is not None and str(validated.get('userId')) != user_id:
                raise CanvasCLIError("CLI_ACCOUNT_MISMATCH", "Canvas account changed during its credit read")
            validated = data
            balance = _available_balance(data)
            if balance is not None or attempt == 2:
                break
            self._sleep(0.25 * 2 ** attempt, deadline)
        credits = data.get("credits") if isinstance(data.get("credits"), dict) else {}
        return {"user_id": user_id, "id": user_id, "name": str(data.get("displayName") or ""),
                "available_balance": balance, "credits": balance,
                "buckets": ({"details": credits['details']} if isinstance(credits.get('details'), list) else {}),
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

    def capabilities(self, kind: str = "video", *, deadline: float | None = None) -> list[dict[str, Any]]:
        if kind not in {"image", "video"}:
            raise ValueError("Canvas capability type must be image or video")
        if kind not in self._capabilities:
            data = self._call(["model", "list", "--type", kind], read_only=True, deadline=deadline)
            items = data.get("items")
            if not isinstance(items, list) or any(not isinstance(item, dict)
                    or not isinstance(item.get("model"), str) or not item["model"].strip() for item in items):
                raise CanvasCLIError("CLI_CAPABILITIES_INVALID", "Canvas returned an invalid model catalog")
            self._capabilities[kind] = items
        return self._capabilities[kind]

    def preflight_model(self, normalized: dict[str, Any], *,
                        deadline: float | None = None) -> dict[str, Any]:
        """Resolve this account's advertised model before material or canvas writes."""
        self._remaining(deadline)
        kind = "image" if normalized.get("kind") == "image" else "video"
        selected = select_model(self.capabilities(kind, deadline=deadline), normalized)
        if kind == 'image' and (normalized.get('_image_model_route') or {}).get('mapped'):
            counts = {group: len(normalized.get(group) or []) for group in ('images', 'videos', 'audios')}
            mode = select_mode(selected, normalized, counts)
            generation_flags(mode, normalized)
            prompt = str(normalized.get('prompt') or '')
            rule = next((flag for flag in mode.get('flags') or []
                         if flag.get('flag') == '--prompt'), {})
            if ('maxLength' in rule and len(prompt) > rule['maxLength']
                    or 'minLength' in rule and len(prompt) < rule['minLength']):
                raise _unsupported('Configured image model does not support the requested prompt length')
        self._remaining(deadline)
        return selected

    def _ensure_project(self, context: dict[str, Any], *, deadline: float | None = None) -> None:
        wanted = context["project_id"]
        args = ["canvas", "ls", "--limit", "100"]
        for _ in range(20):
            data = self._call(args, read_only=True, deadline=deadline)
            if any(x.get("projectId") == wanted for x in data.get("items", []) if isinstance(x, dict)):
                return
            if not data.get("hasMore") or not data.get("nextCursor"):
                break
            args = ["canvas", "ls", "--limit", "100", "--cursor", str(data["nextCursor"])]
        try:
            self._preparation_write(["canvas", "create", f"IJM account {self.account['id']}", "--project-id", wanted], deadline=deadline)
        except CanvasCLIError:
            # Creation is idempotent by the already persisted project UUID.
            # Re-read once after a lost response; never allocate another ID.
            data = self._call(["canvas", "ls", "--limit", "100"], read_only=True, deadline=deadline)
            if not any(x.get("projectId") == wanted for x in data.get("items", []) if isinstance(x, dict)):
                raise

    @staticmethod
    def _reference_state(data: dict, resource_id: str, media_type: str | None) -> str:
        if data.get('resourceId') != resource_id:
            raise CanvasCLIError('CLI_RESOURCE_MISMATCH', 'Canvas returned another reference resource')
        if media_type and any(value not in (None, '', media_type)
                              for value in (data.get('type'), data.get('mediaType'))):
            raise CanvasCLIError('CLI_RESOURCE_MISMATCH', 'Canvas returned another reference media type')
        state = str(data.get('status') or '').lower()
        if state in {'failed', 'error', 'canceled', 'cancelled', 'rejected'}:
            raise CanvasCLIError('MEDIA_UPLOAD_FAILED', f"Canvas reference upload failed ({data.get('errorCode', state)})")
        return state

    def _resource_ready(self, resource_id: str, project_id: str, *, timeout: float = 120,
                        deadline: float | None = None, media_type: str | None = None) -> dict[str, Any]:
        ready_deadline = time.monotonic() + timeout
        observed_missing = False
        if deadline is not None:
            ready_deadline = min(ready_deadline, deadline)
        try:
            while True:
                try:
                    data = self._call(["resource", "get", resource_id, "--project-id", project_id],
                                      read_only=True, deadline=ready_deadline)
                except CanvasCLIError as exc:
                    if not _planned_resource_not_found(exc, resource_id):
                        raise
                    observed_missing = True
                    self._sleep(1, ready_deadline)
                    continue
                state = self._reference_state(data, resource_id, media_type)
                if state in {"success", "succeeded", "completed"}:
                    return data
                self._sleep(1, ready_deadline)
        except CanvasCLIError as exc:
            if exc.code == 'CLI_TASK_DEADLINE_EXCEEDED' and (observed_missing or deadline is None or time.monotonic() < deadline):
                raise CanvasCLIError("MEDIA_UPLOAD_PENDING", "Canvas reference upload is not ready; retained original resource ID") from exc
            raise

    def _reference_fallback_file(self, fallback_file: Callable[[], Path], deadline: float | None) -> Path:
        self._remaining(deadline)
        closed = getattr(self.runner, '_closed', None)
        if isinstance(closed, Event) and closed.is_set():
            raise CanvasCLIError('CLI_STOPPING', 'Canvas reference preparation stopped')
        # Preserve the service's structured MediaError and bounded download policy.
        path = fallback_file()
        self._remaining(deadline)
        if isinstance(closed, Event) and closed.is_set():
            raise CanvasCLIError('CLI_STOPPING', 'Canvas reference preparation stopped')
        if not isinstance(path, Path) or not path.is_file():
            raise CanvasCLIError('MEDIA_INPUT_MISSING', 'Canvas image fallback file is unavailable')
        return path

    def _resume_unacknowledged_upload(self, resource_id: str, project_id: str, media_type: str, *,
                                     fallback_file: Callable[[], Path], deadline: float | None,
                                     on_registered: Callable[[str], Any]) -> None:
        """Recheck a locally unacknowledged URL write; never send its URL again."""
        if media_type != 'image' or not callable(fallback_file):
            raise CanvasCLIError('CLI_UPLOAD_CHECKPOINT_INVALID', 'Canvas unacknowledged upload is not a bound URL image')
        self._remaining(deadline)
        probe_deadline = time.monotonic() + 15
        if deadline is not None:
            probe_deadline = min(probe_deadline, deadline)
        try:
            existing = self._call(['resource', 'get', resource_id, '--project-id', project_id],
                timeout=15, read_only=True, deadline=probe_deadline)
        except CanvasCLIError as check:
            if _planned_resource_not_found(check, resource_id):
                path = self._reference_fallback_file(fallback_file, deadline)
                self._upload_resource(path, media_type, resource_id, project_id,
                    deadline=deadline, on_registered=on_registered)
                return
            retryable_read = (not check.outcome_unknown and (
                preparation_retryable_failure(check) or identity_network_error(check)
                or check.code in {'CLI_COMMAND_TIMEOUT', 'CLI_INVALID_RESPONSE', 'CLI_TASK_DEADLINE_EXCEEDED'}))
            if not retryable_read or _resource_read_intervention(check):
                raise
            self._remaining(deadline)
            raise CanvasCLIError('MEDIA_UPLOAD_PENDING',
                'Canvas original reference registration is temporarily unavailable; retained original resource ID') from check
        if existing.get('projectId') not in (None, '', project_id):
            raise CanvasCLIError('CLI_RESOURCE_MISMATCH', 'Canvas returned another reference project')
        state = self._reference_state(existing, resource_id, media_type)
        if state in {'success', 'succeeded', 'completed'}:
            return
        # A positive registered/pending resource is now observe-only, even if a
        # later resource read fails or temporarily reports registration absence.
        on_registered('acknowledged')
        self._resource_ready(resource_id, project_id, deadline=deadline, media_type=media_type)

    def _upload_resource(self, path: Path | None, media_type: str, resource_id: str, project_id: str, *,
                         deadline: float | None = None, source_url: str | None = None,
                          on_registered: Callable[[str], Any] | None = None,
                           fallback_file: Callable[[], Path] | None = None,
                           file_recovery=None, file_source_sha256: str | None = None) -> None:
        """Retries keep the same documented registration idempotency key."""
        if source_url is not None:
            if media_type != 'image' or path is not None:
                raise CanvasCLIError('MEDIA_INPUT_MISSING', 'Canvas source-url imports require an image and no local file')
            source = ['--source-url', public_image_source_url(source_url)]
            if fallback_file is not None and not callable(fallback_file):
                raise CanvasCLIError('MEDIA_INPUT_MISSING', 'Canvas image fallback provider is invalid')
        elif isinstance(path, Path):
            source = ['--file', str(path.resolve())]
        else:
            raise CanvasCLIError('MEDIA_INPUT_MISSING', 'Canvas reference file is missing')
        original_file = None
        if source_url is None and callable(file_recovery):
            original_file = file_identity(path, lambda: self._remaining(deadline))
            if original_file[-1] != file_source_sha256:
                raise CanvasCLIError('CLI_UPLOAD_CHECKPOINT_INVALID', 'Canvas input bytes changed before registration')
        for attempt in range(3):
            upload_acknowledged = False
            try:
                self._call(["resource", "upload", *source,
                            "--type", media_type, "--project-id", project_id,
                            "--resource-id", resource_id], timeout=180, deadline=deadline)
                upload_acknowledged = True
                if on_registered is not None:
                    on_registered('acknowledged')
                self._resource_ready(resource_id, project_id, deadline=deadline, media_type=media_type)
                return
            except CanvasCLIError as exc:
                if upload_acknowledged:
                    raise  # An acknowledged upload only needs observation; never write it again.
                if exc.code == "MEDIA_UPLOAD_FAILED":
                    raise
                if exc.code == "cli.upload_state_unknown" or exc.outcome_unknown:
                    observation_deadline = min(time.monotonic() + 120, deadline) if deadline is not None else time.monotonic() + 120
                    try:
                        if (exc.code == 'cli.upload_state_unknown' and not exc.outcome_unknown
                                and original_file is not None):
                            # Input UUIDs have a documented registration-idempotency
                            # contract. This one FILE recovery is separate from paid
                            # node.run and retains the original SDK diagnostic.
                            observation_deadline, eligible = file_recovery('observing', exc, observation_deadline)
                            if eligible and attempt < 2:
                                self._recover_unknown_file(path, media_type, resource_id, project_id, exc,
                                    original_file, file_recovery, observation_deadline, on_registered)
                        elif on_registered is not None:
                            on_registered('unknown')
                        self._observe_unknown_upload(resource_id, project_id, deadline=deadline, media_type=media_type,
                            observation_deadline=observation_deadline)
                    except CanvasCLIError as observed:
                        if (observed.code == 'MEDIA_UPLOAD_PENDING'
                                or observed.code == 'CLI_TASK_DEADLINE_EXCEEDED'
                                   and (deadline is None or time.monotonic() < deadline)):
                            raise exc from observed
                        raise
                    return
                if source[0] == '--source-url' and fallback_file is not None:
                    if not _source_url_fallback_allowed(exc):
                        raise
                    # Only exact registration absence permits changing the
                    # transport of this same planned ID. A failed/pending read
                    # is not evidence that a new write is safe.
                    probe_deadline = time.monotonic() + 5
                    if deadline is not None:
                        probe_deadline = min(probe_deadline, deadline)
                    try:
                        existing = self._call(['resource', 'get', resource_id, '--project-id', project_id],
                            timeout=5, read_only=True, deadline=probe_deadline)
                    except CanvasCLIError as check:
                        if not _planned_resource_not_found(check, resource_id):
                            retryable_read = (not check.outcome_unknown and (
                                preparation_retryable_failure(check) or identity_network_error(check)
                                or check.code in {'CLI_COMMAND_TIMEOUT', 'CLI_INVALID_RESPONSE', 'CLI_TASK_DEADLINE_EXCEEDED'}))
                            if not retryable_read or _resource_read_intervention(check):
                                raise
                            if on_registered is not None:
                                # A locally unacknowledged URL is a different
                                # state from SDK unknown. Same-ID FILE recovery
                                # needs its own durable ticket and binding proof.
                                on_registered('unacknowledged')
                            raise exc from check
                        if attempt == 2:
                            raise exc from check  # The three-write cap is shared by both transports.
                        fallback_path = self._reference_fallback_file(fallback_file, deadline)
                        source = ['--file', str(fallback_path.resolve())]
                        self._sleep(0.5 * 2 ** attempt, deadline)
                        continue
                    state = self._reference_state(existing, resource_id, media_type)
                    if state in {'success', 'succeeded', 'completed'}:
                        return
                    if on_registered is not None:
                        on_registered('acknowledged')
                    self._resource_ready(resource_id, project_id, deadline=deadline, media_type=media_type)
                    return
                # A lost response can follow successful storage/registration.
                # Observe the persisted ID before another same-ID upload.
                try:
                    self._resource_ready(resource_id, project_id, timeout=5, deadline=deadline, media_type=media_type)
                    return
                except CanvasCLIError as check:
                    if check.code in {"MEDIA_UPLOAD_FAILED", "CLI_RESOURCE_MISMATCH"} or _resource_read_intervention(check):
                        raise check
                error = exc.details.get("error", {})
                retryable = (exc.code in {"CLI_COMMAND_TIMEOUT", "CLI_INVALID_RESPONSE", "MEDIA_UPLOAD_PENDING", "cli.invalid_service_response"}
                             or bool(error.get("retryable")))
                if attempt == 2 or not retryable:
                    raise
                self._sleep(0.5 * 2 ** attempt, deadline)

    def _recover_unknown_file(self, path, media_type, resource_id, project_id, original_error,
                              original_file, ticket, observation_deadline, on_registered):
        """Fresh exact read + durable once claim; no generic unknown replay."""
        details = original_error.details if isinstance(original_error.details, dict) else {}
        facts = details.get('error') if isinstance(details.get('error'), dict) else {}
        classification = ' '.join(str(facts.get(key) or '').lower() for key in ('class', 'reason'))
        if (facts.get('class') in {'permanent_failure', 'business_failure', 'invalid_input'}
                or facts.get('requiredAction') in {'login', 'fix_input'}
                or any(word in classification for word in ('auth', 'identity', 'mismatch', 'permission',
                    'policy', 'moderation', 'copyright', 'invalid_input', 'insufficient', 'unsupported'))
                or any(value.get(key) for value in (details, facts) for key in
                       ('partialData', 'accepted', 'resources', 'receipt', 'run_receipt', 'submission'))):
            raise original_error
        try:
            identity = self.identity(deadline=observation_deadline)
        except CanvasCLIError as check:
            if identity_network_error(check):
                return  # An unavailable fresh identity permits reads, never a write.
            raise
        expected = str(self.account.get('login_user_id') or self.account.get('cli_user_id') or '')
        if (not expected or str(identity.get('user_id') or '') != expected
                or any(str(self.account.get(key) or '') not in {'', expected}
                       for key in ('login_user_id', 'cli_user_id'))):
            raise CanvasCLIError('CLI_IDENTITY_MISMATCH', 'Canvas input recovery identity changed')
        try:
            current = self._call(['resource', 'get', resource_id, '--project-id', project_id],
                timeout=10, read_only=True, deadline=observation_deadline)
        except CanvasCLIError as check:
            if not _planned_resource_not_found(check, resource_id):
                if (not check.outcome_unknown and not _resource_read_intervention(check)
                        and (preparation_retryable_failure(check) or identity_network_error(check)
                             or check.code in {'CLI_INVALID_RESPONSE', 'cli.resource_unavailable'})):
                    return  # A failed absence probe stays on the original observation path.
                raise
            if any(value.get(key) for value in (check.details, check.details.get('error') or {})
                   if isinstance(value, dict) for key in ('partialData', 'accepted', 'resources', 'receipt', 'submission')):
                raise  # Contradictory partial presence is not registration absence.
        else:
            if current.get('projectId') not in (None, '', project_id):
                raise CanvasCLIError('CLI_RESOURCE_MISMATCH', 'Canvas input recovery returned another project')
            self._reference_state(current, resource_id, media_type)
            if on_registered is not None:
                on_registered('acknowledged')
            return  # A ready/processing registration is observed, never rewritten.
        if file_identity(path, lambda: self._remaining(observation_deadline)) != original_file:
            raise CanvasCLIError('CLI_UPLOAD_CHECKPOINT_INVALID', 'Canvas recovery input path or bytes changed')
        if not ticket('claim', original_error, observation_deadline)[1]:
            return
        # Saving the claim may take time or invoke a callback. Recheck the file
        # after that durable boundary; a failed check burns the ticket safely.
        if file_identity(path, lambda: self._remaining(observation_deadline)) != original_file:
            raise CanvasCLIError('CLI_UPLOAD_CHECKPOINT_INVALID', 'Canvas recovery input changed after its claim')
        if not ticket('verify', original_error, observation_deadline)[1]:
            return
        try:
            self._call(['resource', 'upload', '--file', str(path.resolve()), '--type', media_type,
                '--project-id', project_id, '--resource-id', resource_id], timeout=180, deadline=observation_deadline)
        except CanvasCLIError as retried:
            if (retried.code != 'cli.upload_state_unknown'
                    and not preparation_retryable_failure(retried)
                    and retried.code not in {'CLI_COMMAND_TIMEOUT', 'CLI_INVALID_RESPONSE'}):
                raise
            return  # A second ambiguous write only observes the original UUID.
        if on_registered is not None:
            on_registered('acknowledged')

    def _observe_unknown_upload(self, resource_id: str, project_id: str, *,
                                deadline: float | None = None, media_type: str | None = None,
                                observation_deadline: float | None = None) -> None:
        if observation_deadline is None:
            observation_deadline = time.monotonic() + 120
        if deadline is not None:
            observation_deadline = min(observation_deadline, deadline)
        try:
            while True:
                try:
                    data = self._resource_ready(resource_id, project_id, deadline=observation_deadline,
                                                media_type=media_type)
                    if data.get("resourceId") != resource_id:
                        raise CanvasCLIError("CLI_RESOURCE_MISMATCH", "Canvas returned another reference resource")
                    return
                except CanvasCLIError as exc:
                    if _resource_read_intervention(exc):
                        raise
                    retryable_read = (not exc.outcome_unknown and (
                        exc.code in {"cli.resource_unavailable", "CLI_COMMAND_TIMEOUT", "CLI_INVALID_RESPONSE",
                                     "cli.invalid_service_response"}
                        or identity_network_error(exc)))
                    if not retryable_read:
                        raise
                    self._sleep(1, observation_deadline)
        except CanvasCLIError as exc:
            if exc.code == "CLI_TASK_DEADLINE_EXCEEDED" and (deadline is None or time.monotonic() < deadline):
                raise CanvasCLIError("MEDIA_UPLOAD_PENDING", "Canvas reference upload remains unconfirmed") from exc
            raise

    @contextmanager
    def _upload_binding_guard(self, context, epoch, deadline):
        with self._account_guard(deadline):
            self._check_context(context, deadline=deadline)
            getter = getattr(self.runner, 'credential_epoch', None)
            if epoch is not None and callable(getter) and getter(self.account) != epoch:
                raise CanvasCLIError('CLI_ACCOUNT_MISMATCH', 'Canvas credentials changed during reference preparation')
            self._remaining(deadline)
            yield

    def _upload_plan(self, normalized, task_id, assets, context, checkpoint, deadline):
        """Bind verified checkpoints to inputs, never to temporary file paths."""
        def digest(value):
            return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True,
                separators=(',', ':'), allow_nan=False).encode()).hexdigest()

        jobs = []
        for group, media_type in (('images', 'image'), ('videos', 'video'), ('audios', 'audio')):
            for index, asset in enumerate(assets.get(group, [])):
                self._remaining(deadline)
                source_url = asset.get('source_url') if isinstance(asset, dict) else None
                fallback_file = asset.get('fallback_file') if isinstance(asset, dict) else None
                path = asset.get('path') if isinstance(asset, dict) else asset
                if source_url is not None:
                    if media_type != 'image' or path is not None:
                        raise CanvasCLIError('MEDIA_INPUT_MISSING', 'Canvas remote references require an image URL only')
                    source_url = public_image_source_url(source_url)
                    if fallback_file is not None and not callable(fallback_file):
                        raise CanvasCLIError('MEDIA_INPUT_MISSING', 'Canvas image fallback provider is invalid')
                    source_kind, source_hash = 'url', hashlib.sha256(source_url.encode()).hexdigest()
                    path = None
                else:
                    if not isinstance(path, (str, Path)) or not Path(path).is_file():
                        raise CanvasCLIError('MEDIA_INPUT_MISSING', 'Canvas reference file is missing')
                    path = Path(path)
                    hasher = hashlib.sha256()
                    with path.open('rb') as stream:
                        for block in iter(lambda: stream.read(1024 * 1024), b''):
                            self._remaining(deadline)
                            hasher.update(block)
                    source_kind, source_hash = 'file', hasher.hexdigest()
                jobs.append({'group': group, 'index': index, 'media_type': media_type,
                    'source_kind': source_kind, 'source_sha256': source_hash,
                    'path': path, 'source_url': source_url, 'fallback_file': fallback_file})
        binding = {'task_id': task_id, 'account_id': int(self.account['id']),
            'profile_sha256': digest(context['profile']),
            'user_id_sha256': digest(str(self.account.get('login_user_id') or self.account.get('cli_user_id') or '')),
            'proxy_sha256': digest(str(self.account.get('proxy_url') or '')),
            'project_id': context['project_id'],
            'request_sha256': digest({key: value for key, value in normalized.items() if not key.startswith('_')}),
            'media_sha256': digest([{key: job[key] for key in ('group', 'index', 'source_kind', 'source_sha256')}
                                   for job in jobs])}
        binding_hash = digest(binding)
        if isinstance(checkpoint, dict):
            if _upload_dispatch_evidence(checkpoint):
                raise CanvasCLIError('UPSTREAM_SUBMIT_OUTCOME_UNKNOWN',
                    'Canvas checkpoint has prior dispatch evidence; query its original operation', outcome_unknown=True)
            previous_binding = checkpoint.get('binding')
            if isinstance(previous_binding, dict) and any(key in previous_binding and previous_binding[key] != binding[key]
                    for key in ('task_id', 'account_id', 'profile_sha256', 'user_id_sha256', 'proxy_sha256', 'project_id')):
                raise CanvasCLIError('CLI_ACCOUNT_MISMATCH', 'Canvas upload checkpoint identity binding changed')
        refs = []
        for job in jobs:
            job['resource_id'] = _stable(self.account['id'], task_id, job['group'], job['index'], binding_hash)
            refs.append({key: job[key] for key in ('group', 'index', 'resource_id', 'source_kind', 'source_sha256')})
        expected = {ref['resource_id']: {**ref, 'verified': True} for ref in refs}
        verified = set()
        pending_refs = {}
        has_pending = isinstance(checkpoint, dict) and bool(checkpoint.get('pending_refs'))
        if (isinstance(checkpoint, dict) and 'pending_refs' in checkpoint
                and not isinstance(checkpoint['pending_refs'], list)):
            raise CanvasCLIError('CLI_UPLOAD_CHECKPOINT_INVALID', 'Canvas pending upload checkpoint is invalid')
        candidate_valid = (isinstance(checkpoint, dict) and type(checkpoint.get('version')) is int
            and checkpoint['version'] == 1 and checkpoint.get('backend') == 'canvas_cli'
            and checkpoint.get('binding') == binding and isinstance(checkpoint.get('refs'), list)
            and len(checkpoint['refs']) <= len(refs))
        if isinstance(checkpoint, dict) and checkpoint.get(TICKET_KEY) and not candidate_valid:
            raise CanvasCLIError('CLI_UPLOAD_CHECKPOINT_INVALID', 'Canvas input recovery tickets require their original binding')
        if has_pending and not candidate_valid:
            raise CanvasCLIError('CLI_UPLOAD_CHECKPOINT_INVALID',
                'Canvas pending uploads must retain their original input binding')
        if candidate_valid:
            valid = True
            for ref in checkpoint['refs']:
                resource_id = ref.get('resource_id') if isinstance(ref, dict) else None
                if (not isinstance(resource_id, str) or resource_id not in expected
                        or type(ref.get('index')) is not int or ref.get('verified') is not True
                        or ref != expected[resource_id] or resource_id in verified):
                    valid = False
                    break
                verified.add(resource_id)
            if not valid:
                if has_pending:
                    raise CanvasCLIError('CLI_UPLOAD_CHECKPOINT_INVALID', 'Canvas pending upload checkpoint is invalid')
                verified.clear()
            for ref in checkpoint.get('pending_refs', []):
                resource_id = ref.get('resource_id') if isinstance(ref, dict) else None
                disposition = ref.get('registration') if isinstance(ref, dict) else None
                base = ({key: value for key, value in expected[resource_id].items() if key != 'verified'}
                        if isinstance(resource_id, str) and resource_id in expected else None)
                if (base is None or type(ref.get('index')) is not int or ref.get('observe_only') is not True
                        or not isinstance(disposition, str) or disposition not in {'acknowledged', 'unknown', 'unacknowledged'}
                        or ref != {**base, 'observe_only': True, 'registration': disposition}
                        or resource_id in pending_refs or resource_id in verified):
                    raise CanvasCLIError('CLI_UPLOAD_CHECKPOINT_INVALID', 'Canvas pending upload checkpoint is invalid')
                if disposition == 'unacknowledged':
                    job = next(job for job in jobs if job['resource_id'] == resource_id)
                    if (job['source_kind'] != 'url' or job['media_type'] != 'image'
                            or not callable(job['fallback_file'])):
                        raise CanvasCLIError('CLI_UPLOAD_CHECKPOINT_INVALID',
                            'Canvas unacknowledged upload requires its original URL image fallback')
                pending_refs[resource_id] = disposition
        tickets = validated_tickets(checkpoint, jobs, binding) if isinstance(checkpoint, dict) else []
        if any(item['resource_id'] not in verified and item['resource_id'] not in pending_refs for item in tickets):
            raise CanvasCLIError('CLI_UPLOAD_CHECKPOINT_INVALID', 'Canvas input recovery lost its original registration state')
        context['resources'] = {group: [job['resource_id'] for job in jobs if job['group'] == group]
                                for group in ('images', 'videos', 'audios')}
        context['uploaded_resources'] = [ref['resource_id'] for ref in refs if ref['resource_id'] in verified]
        context['upload_checkpoint'] = {'version': 1, 'backend': 'canvas_cli', 'binding': binding,
            'refs': [expected[ref['resource_id']] for ref in refs if ref['resource_id'] in verified],
            'pending_refs': [{**ref, 'observe_only': True, 'registration': pending_refs[ref['resource_id']]}
                for ref in refs if ref['resource_id'] in pending_refs]}
        if tickets:
            context['upload_checkpoint'][TICKET_KEY] = tickets
        return jobs, refs, expected, verified, pending_refs

    def _upload_references(self, jobs, refs, expected, verified, pending_refs, context, save, state_lock,
                           epoch, deadline, *, durable_persist=False):
        stopped = Event()
        failure_lock = threading.Lock()
        first_failure = []
        fatal_failures = []
        namespace = runner_namespace(self.runner)

        def cancelled():
            closed = getattr(self.runner, '_closed', None)
            return stopped.is_set() or isinstance(closed, threading.Event) and closed.is_set()

        def upload(job):
            admitted = False

            def registered(disposition, *, flush=True):
                with state_lock:
                    pending_refs[job['resource_id']] = disposition
                    context['upload_checkpoint']['pending_refs'] = [
                        {**ref, 'observe_only': True, 'registration': pending_refs[ref['resource_id']]}
                        for ref in refs if ref['resource_id'] in pending_refs]
                    # Commit the observe-only restriction before any readiness
                    # call can fail or another sibling can select a retry.
                    if flush:
                        save('uploading')

            original_binding = copy.deepcopy(context['upload_checkpoint']['binding'])
            started_ticket = None

            def file_recovery(event, error, observation_deadline):
                nonlocal started_ticket
                with state_lock:
                    checkpoint = context['upload_checkpoint']
                    if (_upload_dispatch_evidence(context) or checkpoint.get('binding') != original_binding
                            or int(self.account['id']) != original_binding['account_id']
                            or context.get('task_id') != original_binding['task_id']
                            or input_digest(str(self.account.get('login_user_id') or self.account.get('cli_user_id') or '')) != original_binding['user_id_sha256']
                            or any(input_digest(str(self.account[key])) != original_binding['user_id_sha256']
                                   for key in ('login_user_id', 'cli_user_id') if self.account.get(key))
                            or input_digest(str(self.account.get('proxy_url') or '')) != original_binding['proxy_sha256']
                            or context.get('project_id') != original_binding['project_id']):
                        raise CanvasCLIError('CLI_ACCOUNT_MISMATCH', 'Canvas input recovery binding changed')
                    self._check_context(context, deadline=observation_deadline)
                    getter = getattr(self.runner, 'credential_epoch', None)
                    if epoch is not None and callable(getter) and getter(self.account) != epoch:
                        raise CanvasCLIError('CLI_ACCOUNT_MISMATCH', 'Canvas input recovery credentials changed')
                    self._remaining(observation_deadline)
                    entries = validated_tickets(checkpoint, jobs, original_binding)
                    previous = next((item for item in entries if item['resource_id'] == job['resource_id']), None)
                    if event == 'observing':
                        if previous is not None:
                            return remaining_deadline(previous, clock=time.monotonic, wall_time=time.time,
                                                      deadline=observation_deadline), False
                        now = time.time()
                        started_ticket = {'version': 1, 'resource_id': job['resource_id'],
                            'media_type': job['media_type'], 'source_sha256': job['source_sha256'],
                            'binding_sha256': input_digest(original_binding), 'attempt': 0, 'state': 'observing',
                            'observation_started_at': now,
                            'observation_deadline_at': now + max(0, observation_deadline - time.monotonic()),
                            'original_error': safe_error(error)}
                        registered('unknown', flush=False)
                        checkpoint[TICKET_KEY] = [*entries, copy.deepcopy(started_ticket)]
                    elif (event == 'claim' and started_ticket is not None and previous == started_ticket
                            and pending_refs.get(job['resource_id']) == 'unknown'
                            and job['resource_id'] not in verified):
                        claimed = {**started_ticket, 'attempt': 1, 'state': 'claimed'}
                        checkpoint[TICKET_KEY] = [claimed if item['resource_id'] == job['resource_id'] else item for item in entries]
                    elif (event == 'verify' and started_ticket is not None
                            and previous == {**started_ticket, 'attempt': 1, 'state': 'claimed'}
                            and pending_refs.get(job['resource_id']) == 'unknown' and job['resource_id'] not in verified):
                        return observation_deadline, True
                    else:
                        return observation_deadline, False
                    # The state-zero deadline and state-one claim are each
                    # durable before a query/write can advance this recovery.
                    save('uploading')
                    return observation_deadline, True

            try:
                on_wait = None
                if self.preparation_timing is not None:
                    on_wait = lambda seconds, admitted: self.preparation_timing.record_upload_admission(
                        seconds, admitted, index=job['index'] + 1, media_type=job['media_type'])
                with UPLOAD_LIMITER.slot(namespace, int(self.account['id']),
                        account_limit=self.account_upload_concurrency, total_limit=self.total_upload_concurrency,
                        deadline=deadline, stopped=cancelled, clock=time.monotonic, on_wait=on_wait):
                    admitted = True
                    with self._upload_binding_guard(context, epoch, deadline):
                        with self._phase('reference_upload', index=job['index'] + 1, media_type=job['media_type']):
                            if job['resource_id'] in verified:
                                self._resource_ready(job['resource_id'], context['project_id'],
                                    deadline=deadline, media_type=job['media_type'])
                            elif job['resource_id'] in pending_refs:
                                if pending_refs[job['resource_id']] == 'unacknowledged':
                                    self._resume_unacknowledged_upload(job['resource_id'], context['project_id'],
                                        job['media_type'], deadline=deadline, fallback_file=job['fallback_file'],
                                        on_registered=registered)
                                elif pending_refs[job['resource_id']] == 'unknown':
                                    previous = next((item for item in context['upload_checkpoint'].get(TICKET_KEY, [])
                                                     if item['resource_id'] == job['resource_id']), None)
                                    observed_until = (remaining_deadline(previous, clock=time.monotonic,
                                        wall_time=time.time, deadline=deadline) if previous else None)
                                    self._observe_unknown_upload(job['resource_id'], context['project_id'],
                                        deadline=deadline, media_type=job['media_type'], observation_deadline=observed_until)
                                else:
                                    self._resource_ready(job['resource_id'], context['project_id'],
                                        deadline=deadline, media_type=job['media_type'])
                            else:
                                self._upload_resource(job['path'], job['media_type'], job['resource_id'],
                                    context['project_id'], deadline=deadline, source_url=job['source_url'],
                                      on_registered=registered, fallback_file=job['fallback_file'],
                                      file_recovery=file_recovery if durable_persist and epoch is not None and job['source_kind'] == 'file' else None,
                                      file_source_sha256=job['source_sha256'])
                        self._remaining(deadline)
                        # Successful siblings remain useful after another worker fails.
                        with state_lock:
                            verified.add(job['resource_id'])
                            pending_refs.pop(job['resource_id'], None)
                            context['uploaded_resources'] = [ref['resource_id'] for ref in refs
                                if ref['resource_id'] in verified]
                            context['upload_checkpoint']['refs'] = [expected[ref['resource_id']] for ref in refs
                                if ref['resource_id'] in verified]
                            context['upload_checkpoint']['pending_refs'] = [
                                {**ref, 'observe_only': True, 'registration': pending_refs[ref['resource_id']]}
                                for ref in refs if ref['resource_id'] in pending_refs]
                            save('uploading')
            except BaseException as exc:
                # Publish the real failure before waking cancelled siblings.
                # A waiting sibling's termination can otherwise finish its
                # Future first and mask a retryable upstream error.
                with failure_lock:
                    if not first_failure:
                        first_failure.append(exc)
                    cancellation = (not admitted and stopped.is_set()
                        and isinstance(exc, CanvasCLIError) and exc.code == 'CLI_TASK_TERMINATED')
                    transient = (isinstance(exc, CanvasCLIError) and not exc.outcome_unknown
                        and not _resource_read_intervention(exc)
                        and (preparation_retryable_failure(exc) or exc.code in {
                            'CLI_COMMAND_TIMEOUT', 'CLI_INVALID_RESPONSE', 'cli.invalid_service_response',
                            'cli.transport_failed', 'MEDIA_UPLOAD_PENDING'}))
                    observing = (isinstance(exc, CanvasCLIError) and exc.code == 'cli.upload_state_unknown'
                        and job['resource_id'] in pending_refs)
                    if not cancellation and not transient and not observing:
                        fatal_failures.append(exc)
                    original = first_failure[0]
                code = str(getattr(original, 'code', type(original).__name__))
                safe_code = code if re.fullmatch(r'[A-Za-z0-9_.-]{1,80}', code) else 'Error'
                try:
                    with state_lock:
                        context['upload_checkpoint']['first_failure'] = {'code': safe_code}
                        save('uploading')
                except Exception:
                    # A failed checkpoint cannot authorize a retry. Retain the
                    # persistence failure as a fatal cause instead of replaying.
                    with failure_lock:
                        fatal_failures.append(CanvasCLIError('CLI_UPLOAD_CHECKPOINT_FAILED',
                            'Canvas upload checkpoint could not be persisted'))
                stopped.set()
                with failure_lock:
                    fatal = fatal_failures[0] if fatal_failures else None
                if fatal is not None and fatal is not exc:
                    if isinstance(fatal, CanvasCLIError):
                        fatal.details = {**fatal.details, 'parallel_first_failure': {'code': safe_code}}
                    raise fatal from exc
                raise

        images = [job for job in jobs if job['group'] == 'images']
        if self._parallel_uploads() and len(images) > 1:
            with ThreadPoolExecutor(max_workers=min(self.upload_concurrency, len(images)),
                                    thread_name_prefix='canvas-upload') as executor:
                futures = [executor.submit(upload, job) for job in images]
                error = None
                try:
                    for future in as_completed(futures):
                        try:
                            future.result()
                        except BaseException as exc:
                            if error is None:
                                error = exc
                                stopped.set()
                                for pending in futures:
                                    pending.cancel()
                    if error is not None:
                        with failure_lock:
                            cause = fatal_failures[0] if fatal_failures else first_failure[0] if first_failure else error
                            original = first_failure[0] if first_failure else error
                        if cause is not original and isinstance(cause, CanvasCLIError):
                            original_code = str(getattr(original, 'code', type(original).__name__))
                            cause.details = {**cause.details, 'parallel_first_failure': {
                                'code': original_code if re.fullmatch(r'[A-Za-z0-9_.-]{1,80}', original_code) else 'Error'}}
                        raise cause
                except BaseException:
                    stopped.set()
                    raise
                finally:
                    for pending in futures:
                        pending.cancel()
                    # Executor shutdown joins every running worker before returning.
        else:
            for job in images:
                upload(job)
        for job in jobs:
            if job['group'] != 'images':
                upload(job)

    def prepare(self, normalized: dict[str, Any], task_id: str,
                assets: dict[str, list[Any]], *,
                persist: Callable[[dict[str, Any]], Any] | None = None,
                deadline: float | None = None, checkpoint: dict | None = None) -> dict[str, Any]:
        with self._account_guard(deadline):
            getter = getattr(self.runner, 'credential_epoch', None)
            epoch = getter(self.account) if callable(getter) else None
            segmented = isinstance(self.runner, CanvasCLI) and self.runner._parallel_verified
            if not segmented and not self._parallel_uploads():
                return self._prepare(normalized, task_id, assets, persist=persist, deadline=deadline,
                                     checkpoint=checkpoint, credential_epoch=epoch)
        # No parent reader remains while child readers wait behind an auth writer.
        return self._prepare(normalized, task_id, assets, persist=persist, deadline=deadline,
                             checkpoint=checkpoint, credential_epoch=epoch)

    def _project_guard(self, context, deadline):
        return (self.runner.project_guard(self.account, context['project_id'], deadline=deadline)
                if isinstance(self.runner, CanvasCLI) else nullcontext())

    def _prepare(self, normalized: dict[str, Any], task_id: str,
                 assets: dict[str, list[Any]], *,
                 persist: Callable[[dict[str, Any]], Any] | None = None,
                 deadline: float | None = None, checkpoint: dict | None = None,
                 credential_epoch=None) -> dict[str, Any]:
        kind = "image" if normalized.get("kind") == "image" else "video"
        counts = {x: len(assets.get(x, [])) for x in ("images", "videos", "audios")}
        model = self.preflight_model(normalized, deadline=deadline)
        mode = select_mode(model, normalized, counts)
        flags = generation_flags(mode, normalized)
        old = normalized.get("_execution")
        if isinstance(old, dict) and old.get("backend") == "canvas_cli":
            context = copy.deepcopy(old)
            if context.get("account_id") != int(self.account["id"]):
                raise CanvasCLIError("CLI_ACCOUNT_MISMATCH", "An existing Canvas task cannot move to another account")
            self._check_context(context, deadline=deadline)
            if context.get('task_id') != task_id:
                raise CanvasCLIError('CLI_ACCOUNT_MISMATCH', 'Canvas checkpoint belongs to another task')
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
        if _upload_dispatch_evidence(context):
            raise CanvasCLIError("UPSTREAM_SUBMIT_OUTCOME_UNKNOWN", "Canvas submission must be resumed by its original submit ID", outcome_unknown=True)
        jobs, refs, expected, verified, pending_refs = self._upload_plan(normalized, task_id, assets, context,
            checkpoint if checkpoint is not None else context.get('upload_checkpoint'), deadline)
        state_lock = threading.RLock()
        def save(phase: str) -> None:
            with state_lock:
                context["phase"] = phase
                if persist:
                    if persist(copy.deepcopy(context)) is False:
                        raise CanvasCLIError('CLI_TASK_TERMINATED', 'Canvas preparation persistence was not acknowledged')
        save("preparing")
        with self._upload_binding_guard(context, credential_epoch, deadline):
            with self._phase('project'):
                self._ensure_project(context, deadline=deadline)
        self._upload_references(jobs, refs, expected, verified, pending_refs, context, save, state_lock,
                                credential_epoch, deadline, durable_persist=callable(persist))
        args = ["node", "create", kind, "--project-id", context["project_id"],
                "--node-id", context["node_id"], "--update-id", context["update_id"],
                "--title", "IJM " + task_id, "--mode", context["mode"],
                "--model", context["model"], "--prompt",
                prompt_with_resources(str(normalized["prompt"]), context["resources"]), *flags]
        for group in ("images", "videos", "audios"):
            for resource_id in context["resources"][group]:
                args.extend(["--ref", "res:" + resource_id])
        with self._upload_binding_guard(context, credential_epoch, deadline), self._project_guard(context, deadline):
            save("saving")
            with self._phase('draft_save'):
                self._preparation_write(args, deadline=deadline)
            save("quoting")
            with self._phase('quote'):
                quote = self._call(["node", "quote", "--project-id", context["project_id"],
                                    "--node-id", context["node_id"]], read_only=True, deadline=deadline)
        material_validation = material_validation_from_quote(quote, context['node_id'])
        if material_validation is not None:
            context['material_validation'] = material_validation
            save('quoting')
            raise CanvasCLIError(MATERIAL_REQUIREMENTS_ERROR, MATERIAL_REQUIREMENTS_MESSAGE,
                details={'material_validation': material_validation})
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

    def submit(self, context: dict[str, Any], ceiling: int, *,
               persist: Callable[[dict[str, Any]], Any] | None = None,
               deadline: float | None = None) -> dict[str, Any]:
        if context.get("account_id") != int(self.account["id"]):
            raise CanvasCLIError("CLI_ACCOUNT_MISMATCH", "Canvas command account does not match its durable task")
        if (context.get("phase") in {"accepted", "unknown"}
                or context.get("submission_phase") in {"run_dispatching", "submitted", "accepted", "unknown"}):
            raise CanvasCLIError("UPSTREAM_SUBMIT_OUTCOME_UNKNOWN", "An existing Canvas submission must only be queried", outcome_unknown=True)
        if isinstance(ceiling, bool) or not isinstance(ceiling, int) or ceiling < int(context["quoted_credits"]):
            raise CanvasCLIError("CLI_CREDIT_CEILING_EXCEEDED", "Canvas quote exceeds the authorized credit ceiling")
        dispatched = False
        token = ""

        def remaining() -> float | None:
            if deadline is None:
                return None
            budget = deadline - time.monotonic()
            if budget <= 0:
                raise CanvasCLIError("CLI_SUBMISSION_DEADLINE_EXCEEDED",
                                     "Canvas submission deadline expired before generation was dispatched")
            return budget

        def save(stage: str) -> None:
            context["submission_phase"] = stage
            if persist:
                try:
                    if persist(copy.deepcopy(context)) is False:
                        raise RuntimeError("Persistence was not acknowledged")
                except Exception as cause:
                    raise CanvasCLIError("CLI_SUBMISSION_PERSIST_FAILED",
                                         "Canvas submission boundary could not be persisted",
                                         outcome_unknown=dispatched) from cause

        try:
            # Keep this task's credential/profile identity stable from confirm
            # through its single run. Other tasks keep independent bindings.
            with self.runner.account_guard(self.account, deadline=deadline), self._project_guard(context, deadline):
                remaining()
                self._check_context(context, deadline=deadline)
                save("confirming")
                # Confirmation only mints a spending-ceiling token. Retrying it
                # never starts generation; node run below is attempted once only.
                for attempt in range(3):
                    try:
                        confirmed = self._call(["node", "confirm", "--project-id", context["project_id"],
                                                "--node-id", context["node_id"], "--credit-ceiling", str(ceiling)],
                                               timeout=remaining(), deadline=deadline)
                        break
                    except CanvasCLIError as exc:
                        if not preparation_retryable_failure(exc) or attempt == 2:
                            raise
                        # Keep confirmation's established pre-dispatch deadline error.
                        time.sleep(min(0.25 * 2 ** attempt, remaining()) if deadline is not None
                                   else 0.25 * 2 ** attempt)
                        remaining()
                token = confirmed.get("creditConfirmationToken")
                if not isinstance(token, str) or not token:
                    raise CanvasCLIError("CLI_CONFIRMATION_INVALID", "Canvas did not issue a credit confirmation token")
                received_at, received_mono = time.time(), time.monotonic()
                raw_expiry = confirmed.get("expiresAt")
                try:
                    if not isinstance(raw_expiry, str):
                        raise ValueError("Missing confirmation expiry")
                    expires = datetime.fromisoformat(raw_expiry.replace("Z", "+00:00"))
                    if expires.utcoffset() is None:
                        raise ValueError("Confirmation expiry has no timezone")
                    expires_at = expires.timestamp()
                    if not math.isfinite(expires_at):
                        raise ValueError("Invalid confirmation expiry")
                except (ValueError, OverflowError, OSError):
                    raise CanvasCLIError("CLI_CONFIRMATION_INVALID", "Canvas credit confirmation expiry is invalid") from None
                context["confirmation_received_at"] = received_at
                context["confirmation_expires_at"] = expires_at
                token_deadline = received_mono + max(0, expires_at - received_at)

                def valid_confirmation() -> None:
                    remaining()
                    if time.time() >= expires_at or time.monotonic() >= token_deadline:
                        raise CanvasCLIError("CLI_CONFIRMATION_EXPIRED", "Canvas credit confirmation expired before generation was dispatched")

                valid_confirmation()
                context["dispatch_prepared_at"] = time.time()
                save("run_dispatching")
                valid_confirmation()  # Persistence must not outlive the token.
                budget = remaining()
                context["dispatch_started_at"] = time.time()
                dispatched = True
                result = self._call(["node", "run", "--project-id", context["project_id"],
                                     "--node-id", context["node_id"], "--submit-id", context["submit_id"],
                                     "--credit-token", token], timeout=budget, deadline=deadline)
                _remember_submission_evidence(context, {'data': result})
                items = result.get("items")
                if not isinstance(items, list) or len(items) != 1 or not isinstance(items[0], dict):
                    raise CanvasCLIError("UPSTREAM_SUBMIT_OUTCOME_UNKNOWN", "Canvas acceptance result is incomplete", outcome_unknown=True)
                item = items[0]
                if item.get("nodeId") != context["node_id"] or item.get("submitId") != context["submit_id"]:
                    raise CanvasCLIError("UPSTREAM_SUBMIT_OUTCOME_UNKNOWN", "Canvas acceptance identities do not match", outcome_unknown=True)
                verdict = admission_verdict(item.get("state"))
                if item.get("error") or verdict == "rejected":
                    error = item.get("error") if isinstance(item.get("error"), dict) else {}
                    raise CanvasCLIError("UPSTREAM_REJECTED", str(error.get("message") or "Canvas rejected the generation"), details={"error": error},
                                         outcome_unknown=bool(item.get("resources")) or verdict != "rejected")
                if verdict != "accepted":
                    raise CanvasCLIError("UPSTREAM_SUBMIT_OUTCOME_UNKNOWN", "Canvas returned an uncertain submission state", outcome_unknown=True)
                context["resource_ids"] = [str(x["resourceId"]) for x in item.get("resources", []) if isinstance(x, dict) and x.get("resourceId")]
                context["phase"] = "accepted"
                context.pop("submit_error", None)
                save("accepted")
                return copy.deepcopy(context)
        except Exception as caught:
            exc = caught if isinstance(caught, CanvasCLIError) else CanvasCLIError(
                "CLI_SUBMISSION_INTERRUPTED", "Canvas submission was interrupted", outcome_unknown=dispatched)
            # A pre-run failure cannot be a paid outcome, even if a callback
            # already changed the caller's in-memory phase before failing.
            if not dispatched:
                exc.outcome_unknown = False
            _remember_submission_evidence(context, exc.details)
            diagnostic = safe_cli_diagnostic(exc, secrets=(token,) if isinstance(token, str) else ())
            diagnostic.update({"dispatch_started": dispatched, "phase": context.get("submission_phase", "confirming")})
            context["submit_error"] = diagnostic
            exc.message = str(diagnostic["message"])
            exc.args = (exc.message,)
            exc.details = {"error": diagnostic, "dispatch_started": dispatched, "phase": diagnostic["phase"]}
            if persist:
                try:
                    persist(copy.deepcopy(context))
                except Exception:
                    pass  # Preserve the original failure and dispatch fact.
            if exc is caught:
                raise
            raise exc from caught

    @staticmethod
    def _download_retryable(exc: CanvasCLIError) -> bool:
        error = exc.details.get("error")
        error = error if isinstance(error, dict) else {}
        # These native errors describe reading the bound original resource, not
        # admission or generation. Their explicit contract outranks a generic
        # network reason; auth/input/action failures must never fall through.
        if exc.code in {"cli.download_read_failed", "cli.transport_failed"}:
            expected_codes = (None, exc.code) if exc.code == "cli.download_read_failed" else (exc.code,)
            return (error.get("code") in expected_codes
                and error.get("class") == "retryable_failure"
                and error.get("retryable") is True and error.get("requiredAction") == "retry"
                and error.get("serviceCode") in (None, "")
                and error.get("reason") in (None, "", *_TRANSIENT))
        classification = " ".join(str(value or "").lower() for value in
            (exc.code, error.get("code"), error.get("class"), error.get("reason")))
        if (error.get("retryable") is not None and error.get("retryable") is not True
                or error.get("serviceCode") not in (None, "")
                or error.get("requiredAction") not in (None, "", "retry")
                or any(word in classification for word in
                    ("auth", "credential", "identity", "permission", "invalid_input",
                     "invalid_argument", "conflict", "mismatch", "human_intervention"))):
            return False
        return (exc.code in {"CLI_COMMAND_TIMEOUT", "CLI_INVALID_RESPONSE", "cli.invalid_service_response"}
                or error.get("retryable") is True and error.get("reason") in _TRANSIENT)

    @staticmethod
    def _download_safe_path(path: Path) -> None:
        """Reject symlinks/junctions before reading or writing a result path."""
        for part in reversed((path, *path.parents)):
            try:
                info = part.lstat()
            except FileNotFoundError:
                continue
            if (stat.S_ISLNK(info.st_mode)
                    or getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)):
                raise CanvasCLIError("CLI_RESULT_INVALID", "Canvas result path cannot contain a symbolic link")

    def _download_binding(self, context: dict[str, Any], resource_id: str, media_type: str) -> dict[str, Any] | None:
        fields = ("task_id", "profile", "project_id", "node_id", "submit_id", "expected_user_id")
        outputs = context.get("resource_ids")
        if (any(not isinstance(context.get(key), str) or not context[key].strip() for key in fields)
                or type(context.get("account_id")) is not int or media_type not in {"video", "image"}
                or not isinstance(outputs, list) or not outputs
                or any(not isinstance(value, str) or not value.strip() for value in outputs)
                or len(set(outputs)) != len(outputs) or resource_id not in outputs):
            return None  # Old direct callers can download, but cannot reuse a file.
        if (context["account_id"] != int(self.account["id"]) or context.get("kind") != media_type
                or context["expected_user_id"] != str(self.account.get("login_user_id") or self.account.get("cli_user_id") or "")):
            raise CanvasCLIError("CLI_ACCOUNT_MISMATCH", "Canvas download does not match its original account identity")
        return {"version": 1, **{key: context[key] for key in ("account_id", *fields)},
                "resource_id": resource_id, "kind": media_type}

    def _verify_download_file(self, path: Path, size: Any, expected_hash: Any, media_type: str, *,
                              deadline: float | None = None, durable: bool = False) -> str:
        self._remaining(deadline)
        self._download_safe_path(path)
        if (type(size) is not int or size <= 0 or not isinstance(expected_hash, str)
                or re.fullmatch(r"[0-9a-fA-F]{64}", expected_hash) is None):
            raise CanvasCLIError("CLI_RESULT_DOWNLOAD_FAILED", "Canvas original-media receipt is invalid")
        # Windows FlushFileBuffers requires a writable handle even after reading.
        flags = (os.O_RDWR if durable else os.O_RDONLY) | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        with os.fdopen(os.open(path, flags), "rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_size != size:
                raise CanvasCLIError("CLI_RESULT_DOWNLOAD_FAILED", "Canvas result size did not match its receipt")
            digest = hashlib.sha256()
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                self._remaining(deadline)
                digest.update(block)
            mime = _file_mime(path, media_type)
            after, current = os.fstat(stream.fileno()), path.lstat()
            signature = lambda item: (item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns)
            self._download_safe_path(path)
            if (digest.hexdigest() != expected_hash.lower() or signature(before) != signature(after)
                    or signature(after) != signature(current)):
                raise CanvasCLIError("CLI_RESULT_DOWNLOAD_FAILED", "Canvas result checksum or file identity changed")
            if durable:
                os.fsync(stream.fileno())
        self._remaining(deadline)
        return mime

    def _persist_download(self, context: dict[str, Any], receipt: dict[str, Any],
                          persist_download: Callable[[dict[str, Any]], Any] | None) -> None:
        if persist_download is None:
            return
        try:
            accepted = persist_download(copy.deepcopy(receipt))
        except Exception as exc:
            raise CanvasCLIError("CLI_DOWNLOAD_RECEIPT_PERSIST_FAILED", "Canvas download receipt could not be saved") from exc
        if accepted is False:
            raise CanvasCLIError("CLI_STOPPING", "Canvas result observation was stopped before delivery")
        receipts = context.get("download_receipts")
        receipts = copy.deepcopy(receipts) if isinstance(receipts, dict) else {}
        receipts[receipt["resource_id"]] = copy.deepcopy(receipt)
        context["download_receipts"] = receipts

    def download_result(self, context: dict[str, Any], resource_id: str, media_type: str, *,
                        deadline: float | None = None,
                        persist_download: Callable[[dict[str, Any]], Any] | None = None) -> dict[str, Any]:
        if (not re.fullmatch(r"[A-Za-z0-9_-]{1,120}", str(context["task_id"]))
                or not re.fullmatch(r"[a-f0-9-]{36}", resource_id)):
            raise CanvasCLIError("CLI_RESULT_INVALID", "Canvas result identifiers are invalid")
        # A direct original-result read is bounded even without a service
        # delivery claim. Service callers pass the one durable attempt deadline.
        if deadline is None:
            deadline = time.monotonic() + 900
        self._remaining(deadline)
        base = self.runner.account_directory(self.account).absolute() / "results" / context["task_id"]
        self._download_safe_path(base)
        base.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._download_safe_path(base)
        suffix = ".mp4" if media_type == "video" else ".png"
        path = base / (resource_id + suffix)
        self._download_safe_path(path)
        binding = self._download_binding(context, resource_id, media_type)
        if persist_download is not None and binding is None:
            raise CanvasCLIError("CLI_RESULT_INVALID", "Canvas download is missing its durable task binding")
        if binding is not None:
            self._check_context(context, deadline=deadline)
        receipts = context.get("download_receipts")
        receipt = receipts.get(resource_id) if isinstance(receipts, dict) else None
        if (binding is not None and isinstance(receipt, dict)
                and type(receipt.get("version")) is int
                and all(type(receipt.get(key)) is type(value) and receipt.get(key) == value for key, value in binding.items())
                and receipt.get("filename") == path.name):
            try:
                mime = self._verify_download_file(path, receipt.get("size_bytes"), receipt.get("sha256"), media_type, deadline=deadline)
                if mime != receipt.get("mime"):
                    raise CanvasCLIError("CLI_RESULT_INVALID", "Canvas cached result media type changed")
            except (OSError, CanvasCLIError) as exc:
                if isinstance(exc, CanvasCLIError) and exc.code == "CLI_TASK_DEADLINE_EXCEEDED":
                    raise
            else:
                self._persist_download(context, receipt, persist_download)
                self._remaining(deadline)
                return {"path": str(path), "mime": mime, "resource_id": resource_id,
                        "download_receipt": copy.deepcopy(receipt)}
        # Files without a matching durable receipt are always fetched again.
        for attempt in range(3):
            partial = base / (resource_id + "." + uuid.uuid4().hex + ".part")
            try:
                data = self._call(["resource", "download", resource_id,
                    "--project-id", context["project_id"], "--output", str(partial)],
                    timeout=self._remaining(deadline), read_only=False, deadline=deadline)
                returned = Path(str(data.get("path") or "")).absolute()
                size, expected_hash = data.get("size"), data.get("sha256")
                if (data.get("resourceId") != resource_id or returned != partial or not partial.is_file()
                        or isinstance(size, bool) or not isinstance(size, int) or size <= 0
                        or partial.stat().st_size != size
                        or not isinstance(expected_hash, str)
                        or re.fullmatch(r"[0-9a-fA-F]{64}", expected_hash) is None):
                    raise CanvasCLIError("CLI_RESULT_DOWNLOAD_FAILED", "Canvas download did not return a complete original-media receipt")
                mime = self._verify_download_file(partial, size, expected_hash, media_type, deadline=deadline, durable=True)
                self._download_safe_path(path)
                partial.replace(path)
                if os.name != "nt":
                    directory = os.open(base, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
                    try:
                        os.fsync(directory)
                    finally:
                        os.close(directory)
                result = {"path": str(path), "mime": mime, "resource_id": resource_id}
                if binding is not None:
                    receipt = {**binding, "filename": path.name, "size_bytes": size,
                               "sha256": expected_hash.lower(), "mime": mime}
                    self._remaining(deadline)
                    self._persist_download(context, receipt, persist_download)
                    self._remaining(deadline)
                    result["download_receipt"] = copy.deepcopy(receipt)
                return result
            except CanvasCLIError as exc:
                if attempt == 2 or not self._download_retryable(exc):
                    raise
                self._sleep(0.25 * 2 ** attempt, deadline)
            finally:
                partial.unlink(missing_ok=True)
        raise AssertionError("unreachable")

    def _failure_details(self, context: dict[str, Any], data: dict[str, Any],
                         resources: list[dict[str, Any]], failed: list[dict[str, Any]], *,
                         deadline: float | None = None) -> dict[str, Any]:
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
                    "--project-id", context["project_id"]], timeout=10, read_only=True, deadline=deadline)
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
                    "--node-id", context["node_id"]], timeout=10, read_only=True, deadline=deadline)
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

    def poll(self, context: dict[str, Any], *, deadline: float | None = None,
             persist_download: Callable[[dict[str, Any]], Any] | None = None,
              persist_output_binding: Callable[[dict[str, Any]], Any] | None = None,
             get_delivery_deadline: Callable[[], float] | None = None,
             on_phase: Callable[[str], Any] | None = None) -> dict[str, Any]:
        with self.runner.account_guard(self.account, deadline=deadline):
            self._check_context(context, deadline=deadline)
            return self._poll(context, deadline=deadline, persist_download=persist_download,
                              persist_output_binding=persist_output_binding,
                              get_delivery_deadline=get_delivery_deadline, on_phase=on_phase)

    def _poll(self, context: dict[str, Any], *, deadline: float | None = None,
              persist_download: Callable[[dict[str, Any]], Any] | None = None,
               persist_output_binding: Callable[[dict[str, Any]], Any] | None = None,
              get_delivery_deadline: Callable[[], float] | None = None,
              on_phase: Callable[[str], Any] | None = None) -> dict[str, Any]:
        def phase(name: str, *, phase_deadline: float | None = None) -> None:
            self._remaining(deadline if phase_deadline is None else phase_deadline)
            if on_phase is not None and on_phase(name) is False:
                raise CanvasCLIError("CLI_STOPPING", "Canvas result observation was stopped before delivery")
        phase("querying")
        output_count = context.get('output_count')
        query_seconds = (max(30, 15 * output_count)
                         if context.get('resource_ids') and type(output_count) is int and 1 <= output_count <= 4 else 15)
        query_deadline = min(deadline, time.monotonic() + query_seconds) if deadline is not None else time.monotonic() + query_seconds
        recovered_node = False
        recovered_resources = False
        data = None
        if accepted_output_context(context):
            try:
                data = observe_bound_resources(self, context, deadline=query_deadline)
            except CanvasCLIError as original:
                if not resource_observation_retryable(original):
                    raise
                self._remaining(query_deadline)
            recovered_resources = data is not None
        if data is None:
            try:
                data = self._call(["operation", "status", context["submit_id"],
                                   "--project-id", context["project_id"]], read_only=True, deadline=query_deadline)
            except CanvasCLIError as original:
                data = observe_bound_resources(self, context, error=original, deadline=query_deadline)
                recovered_resources = data is not None
                if data is None:
                    data = recover_absent_operation(self, context, original, deadline=query_deadline)
                    if data is None:
                        raise
                    recovered_node = True
        reference = data.get("operationRef")
        if not isinstance(reference, str) or not reference.strip():
            raise CanvasCLIError("CLI_INVALID_RESPONSE", "Canvas operation response omitted its submission identity", outcome_unknown=True)
        if reference != context["submit_id"]:
            raise CanvasCLIError("CLI_RESOURCE_MISMATCH", "Canvas returned another submission identity", outcome_unknown=True)
        for key, expected_key in (("projectId", "project_id"), ("nodeId", "node_id"), ("submitId", "submit_id")):
            if data.get(key) not in (None, "", context.get(expected_key)):
                raise CanvasCLIError("CLI_RESOURCE_MISMATCH", "Canvas operation changed its original binding", outcome_unknown=True)
        raw_state = data.get("state")
        state = raw_state.strip().lower() if isinstance(raw_state, str) else ""
        if state not in {"pending", "queued", "accepted", "running", "in_progress", "processing",
                         "completed", "success", "succeeded", "partial_failed", "failed", "rejected",
                         "error", "canceled", "cancelled", "absent", "unknown"}:
            raise CanvasCLIError("CLI_INVALID_RESPONSE", "Canvas operation response has no valid state", outcome_unknown=True)
        resources = data.get("resources")
        if not isinstance(resources, list):
            raise CanvasCLIError("CLI_INVALID_RESPONSE", "Canvas operation response omitted its resource list", outcome_unknown=True)
        resource_states = {"pending", "queued", "running", "in_progress", "processing", "completed", "success", "succeeded",
                           "failed", "rejected", "error", "canceled", "cancelled"}
        for resource in resources:
            if (not isinstance(resource, dict)
                    or not isinstance(resource.get("resourceId"), str) or not resource["resourceId"].strip()
                    or not isinstance(resource.get("state"), str) or resource["state"].strip().lower() not in resource_states):
                raise CanvasCLIError("CLI_INVALID_RESPONSE", "Canvas operation response contains an invalid resource", outcome_unknown=True)
        resource_ids = [resource["resourceId"] for resource in resources]
        if len(set(resource_ids)) != len(resource_ids):
            raise CanvasCLIError("CLI_INVALID_RESPONSE", "Canvas operation response contains duplicate resources", outcome_unknown=True)
        expected = context.get("resource_ids") or []
        if (not isinstance(expected, list) or any(not isinstance(value, str) or not value.strip() for value in expected)
                or len(set(expected)) != len(expected)
                or expected and not set(resource_ids).issubset(expected)):
            raise CanvasCLIError("CLI_RESOURCE_MISMATCH", "Canvas operation resources do not match the original receipt", outcome_unknown=True)
        result: dict[str, Any] = {"status": "running", "progress": 45, "result_urls": [],
                                  "result_paths": [], "result_files": [], "credit_facts": [],
                                   "error_code": "", "error_message": "", "operation_state": state,
                                   "output_resources_missing": not resources,
                                   "resource_recovery_required": not resources and state in {"completed", "success", "succeeded"}}
        if state in {"absent", "unknown"}:
            result.update(status="unknown", error_code="UPSTREAM_SUBMIT_OUTCOME_UNKNOWN",
                          error_message="Canvas submission could not be resolved; automatic resubmission is disabled")
            return result
        failed = [x for x in resources if str(x.get("state") or "").lower() in {"failed", "rejected", "error", "canceled", "cancelled"}]
        if (state in {"failed", "partial_failed", "rejected", "error", "canceled", "cancelled"}
                or failed or data.get("submissionError")):
            result.update(self._failure_details(context, data, resources, failed, deadline=deadline))
            return result
        successful = [x for x in resources if str(x.get("state") or "").lower() in {"success", "succeeded", "completed"}]
        terminal = (state in {"completed", "success", "succeeded"}
                    or bool(resources) and len(successful) == len(resources))
        if terminal and (not resources or len(successful) != len(resources)):
            result['resource_recovery_required'] = True
        if not terminal or not resources or len(successful) != len(resources):
            # operation.submission summarizes the lifecycle (e.g. completed),
            # unlike node.run's ACCEPTED/REJECTED admission receipt. Concrete
            # original-operation resources/terminal states outrank that summary.
            if not terminal:
                phase("generating")
            return result
        if len(resources) != int(context.get("output_count") or 1):
            result.update(status="failed", progress=100, error_code="CLI_RESULT_INCOMPLETE",
                          error_message="Canvas completed with an unexpected number of generated outputs")
            return result
        if expected:
            positions = {resource_id: index for index, resource_id in enumerate(expected)}
            resources = sorted(resources, key=lambda resource: positions[resource["resourceId"]])
        checked_resources = []
        for resource in resources:
            resource_id = str(resource.get("resourceId") or "")
            if re.fullmatch(r"[a-f0-9-]{36}", resource_id) is None:
                raise CanvasCLIError("CLI_RESULT_INVALID", "Canvas completed a task without its output resource ID")
            observed = data.get('_observed_resource_facts') if recovered_resources else None
            facts = observed.get(resource_id) if isinstance(observed, dict) else None
            if not isinstance(facts, dict):
                facts = self._call(["resource", "get", resource_id, "--project-id", context["project_id"]],
                                   read_only=True, deadline=query_deadline)
            if facts.get("resourceId") != resource_id or facts.get("type") != context["kind"]:
                raise CanvasCLIError("CLI_RESOURCE_MISMATCH", "Canvas returned another output resource or media type")
            for key, expected_key in (("projectId", "project_id"), ("nodeId", "node_id"), ("submitId", "submit_id")):
                if facts.get(key) not in (None, "", context.get(expected_key)):
                    raise CanvasCLIError("CLI_RESOURCE_MISMATCH", "Canvas output changed its original binding", outcome_unknown=True)
            checked_resources.append((resource_id, facts))
            amount = _number(facts.get("creditsAmount"))
            if amount is not None:
                result["credit_facts"].append({"source": "precharge", "amount": amount,
                    "field": "resource.creditsAmount", "resource_id": resource_id,
                    "submit_id": context["submit_id"], "settled": False})
        # Publish the complete original-operation binding before the first file
        # operation. A later download failure must not lose recovered output IDs.
        outputs = [resource_id for resource_id, _ in checked_resources]
        source = (ACCEPTED_RESOURCE_SOURCE if recovered_resources else
                  "node_show_original_submission" if recovered_node else "operation_status")
        receipt = {"version": 1, "source": source, "operation_ref": reference,
                   **{key: context.get(key) for key in ("account_id", "task_id", "profile", "project_id", "node_id",
                                                       "submit_id", "expected_user_id", "kind")},
                   "resource_ids": outputs}
        if recovered_node:
            receipt['node_recovery'] = copy.deepcopy(data['node_recovery'])
        if recovered_resources:
            receipt['resource_recovery'] = copy.deepcopy(data['resource_recovery'])
        if persist_output_binding is not None:
            candidate = {**context, "resource_ids": outputs}
            if self._download_binding(candidate, outputs[0], context["kind"]) is None:
                raise CanvasCLIError("CLI_OUTPUT_BINDING_PERSIST_FAILED", "Canvas output has no complete original task binding", outcome_unknown=True)
            try:
                saved = persist_output_binding(copy.deepcopy(receipt))
            except CanvasCLIError:
                raise
            except Exception as exc:
                raise CanvasCLIError("CLI_OUTPUT_BINDING_PERSIST_FAILED", "Canvas output binding could not be durably saved", outcome_unknown=True) from exc
            if saved is not True:
                raise CanvasCLIError("CLI_STOPPING", "Canvas output binding changed before delivery")
        elif persist_download is not None and not expected:
            raise CanvasCLIError("CLI_OUTPUT_BINDING_PERSIST_FAILED", "Canvas output binding must be saved before durable download", outcome_unknown=True)
        context["resource_ids"] = outputs
        if persist_output_binding is not None:
            context["output_binding"] = copy.deepcopy(receipt)
        result["resource_ids"] = list(outputs)
        # Query/authentication keeps its short budget. Only a complete verified
        # original output may claim a separate, durably bounded delivery attempt.
        # Claim once for all files and retries, never once per command or output.
        delivery_deadline = deadline if deadline is not None else time.monotonic() + 900
        if get_delivery_deadline is not None:
            delivery_deadline = get_delivery_deadline()
            if (isinstance(delivery_deadline, bool) or not isinstance(delivery_deadline, (int, float))
                    or not math.isfinite(delivery_deadline)):
                raise CanvasCLIError("CLI_RESULT_INVALID", "Canvas original-result delivery deadline is invalid")
            self._remaining(delivery_deadline)
        for resource_id, facts in checked_resources:
            phase("downloading", phase_deadline=delivery_deadline)
            download_kwargs = {"deadline": delivery_deadline}
            if persist_download is not None:
                download_kwargs["persist_download"] = persist_download
            downloaded = self.download_result(context, resource_id, str(facts.get("type") or context["kind"]), **download_kwargs)
            result["result_files"].append(downloaded)
            result["result_paths"].append(downloaded["path"])
        result.update(status="succeeded", progress=100)
        return result

    def _check_context(self, context: dict[str, Any], *, deadline: float | None = None) -> None:
        if context.get("account_id") != int(self.account["id"]):
            raise CanvasCLIError("CLI_ACCOUNT_MISMATCH", "Canvas command account does not match its durable task")
        if hasattr(self.runner, "profile"):
            profile = (self.runner.profile(self.account, deadline=deadline)
                       if isinstance(self.runner, CanvasCLI) else self.runner.profile(self.account))
            if context.get("profile") != profile:
                raise CanvasCLIError("CLI_ACCOUNT_MISMATCH", "Canvas task belongs to a retired credential profile")
