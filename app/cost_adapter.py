"""Read-only, privacy-safe video billing evidence for cost reference exports.

Do not use legacy actual_cost as proof: several services store estimates there.
"""
from __future__ import annotations

import math
import time
from typing import Any

SERVICE = "ijm2api"


def number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        value = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return value if math.isfinite(value) and value >= 0 else None


def mapping(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def first_number(*values: Any) -> float | None:
    return next((result for value in values if (result := number(value)) is not None), None)


def seconds(items: Any) -> float | None:
    if not isinstance(items, list):
        return None
    values = []
    for item in items:
        row = mapping(item)
        meta = mapping(row.get("metadata"))
        value = first_number(row.get("duration"), row.get("duration_seconds"), meta.get("duration"))
        if value is None:
            milliseconds = first_number(row.get("duration_ms"), meta.get("duration_ms"))
            value = milliseconds / 1000 if milliseconds is not None else None
        if value is None or value <= 0:
            return None
        values.append(value)
    return round(sum(values), 6)


def input_evidence(assets: dict) -> dict:
    """Persist only measured input dimensions, never uploaded identifiers or URLs."""
    return {"reference_seconds": seconds(assets.get("videos", [])),
            "image_count": len(assets.get("images") or []),
            "video_count": len(assets.get("videos") or []),
            "audio_count": len(assets.get("audios") or [])}


def settle_failed_balance(store: Any, task: dict, client: Any) -> None:
    """Record terminal failure debits only when the account interval is isolated."""
    try:
        state = client.account_state()
        before = number(task.get("balance_before"))
        after = number(state.get("available_balance"))
        isolated = not store.has_account_overlap(int(task["account_id"]), task["id"],
                                                 task["created_at"], time.time())
        if before is None or after is None or not isolated or after > before:
            return
        cost = before - after
        estimate = number(task.get("estimated_cost"))
        # Large drops may be a subscription expiration rather than task billing.
        if estimate and cost > max(estimate * 3, estimate + 100):
            return
        store.update_task(task["id"], {"balance_after": after, "actual_cost": cost,
                                           "cost_evidence": {"balance_isolated": True}})
    except Exception:
        # Cost observation never changes a generation outcome.
        return


def _videos(request: dict) -> list:
    for key in ("_videos", "videos", "video_urls"):
        value = request.get(key)
        if isinstance(value, list):
            return value
    return []


def build_observations(task: dict[str, Any]) -> list[dict[str, Any]]:
    request = mapping(task.get("normalized") or task.get("request"))
    caller = mapping(task.get("caller_request") or task.get("request"))
    if request.get("kind") == "image" or task.get("kind") == "image":
        return []
    upstream = mapping(task.get("upstream_response"))
    raw = mapping(task.get("raw_status"))
    audit = mapping(task.get("upstream_request"))
    evidence = mapping(request.get("_cost_evidence"))
    evidence = {**mapping(upstream.get("cost_evidence")), **evidence}
    protocol = mapping(upstream.get("protocol"))
    submit = mapping(mapping(audit.get("submit")).get("body"))
    uploads = audit.get("uploads")
    videos = _videos(request)
    reference = evidence.get("reference_seconds") if "reference_seconds" in evidence else None
    if reference is None and isinstance(uploads, list):
        reference = seconds([item for item in uploads if mapping(item).get("kind") == "video"])
    if reference is None and not videos and not uploads:
        reference = 0
    requested_reference = first_number(caller.get("reference_seconds"), caller.get("reference_video_duration"))
    if requested_reference is None:
        requested_reference = seconds(_videos(caller))
    context = {}
    # Only scalar fields with known billing meaning may cross the service boundary.
    for key in ("billing_mode", "workflow", "recipe", "_recipe", "aspect_ratio", "generate_audio",
                "generateAudio", "audio", "advanced_mode", "n"):
        value = request.get(key)
        if isinstance(value, (bool, int, float)) or (isinstance(value, str) and len(value) <= 80
                                                    and "://" not in value):
            context[key.lstrip("_")] = value
    for label, aliases in (("image_count", ("_images", "images", "image_urls")),
                           ("video_count", ("_videos", "videos", "video_urls")),
                           ("audio_count", ("_audio", "audios", "audio_urls"))):
        context[label] = evidence.get(label, next((len(request[key]) for key in aliases
                                                  if isinstance(request.get(key), list)), 0))
    if isinstance(uploads, list):
        for label, kind in (("image_count", "image"), ("video_count", "video"), ("audio_count", "audio")):
            context[label] = sum(mapping(item).get("kind") == kind for item in uploads)
    attempts = upstream.get("attempts") if isinstance(upstream.get("attempts"), list) else []
    charged = None
    refunded = None
    quoted = number(task.get("estimated_cost"))
    quoted = quoted if quoted is not None and quoted > 0 else None
    source = "formula" if quoted is not None else "unknown"
    if SERVICE in {"ak2api", "mag2api"}:
        quotes = [number(mapping(item).get("fee")) for item in attempts if "calculate_fee" in mapping(item)]
        if quotes:
            quoted, source = quotes[-1], "quote"
    if SERVICE == "tap2api":
        quoted = first_number(mapping(audit.get("estimate")).get("fee"), quoted)
        source = "quote" if mapping(audit.get("estimate")).get("fee") is not None else source
    if SERVICE == "dra2api":
        quoted = first_number(protocol.get("quoted_credits"), quoted)
        source = "quote" if protocol.get("quoted_credits") is not None else source
        billing = mapping(raw.get("billing"))
        charged = first_number(billing.get("credits"), raw.get("actual_cost"))
        if billing.get("refunded_at") or billing.get("status") == "refunded":
            refunded = first_number(billing.get("refunded_credits"), charged)
        else:
            refunded = number(billing.get("refunded_credits"))
    elif SERVICE == "pol2api":
        charged = number(upstream.get("budget_debited"))
        receipt = mapping(upstream.get("refund"))
        if receipt.get("source") == "refundCreditDecimal":
            refunded = number(receipt.get("credits"))
        detail = mapping(upstream.get("detail") or raw)
        refunded = first_number(detail.get("refundCreditDecimal"), refunded)
        if charged is None:
            charged = number(mapping(detail.get("generateRecord")).get("creditDecimal"))
        if upstream.get("quote"):
            source = "quote"
    elif SERVICE == "mag2api":
        generated = [mapping(mapping(item).get("generate")) for item in attempts if "generate" in mapping(item)]
        if generated:
            charged = number(generated[-1].get("actualCost"))
        ledger = mapping(mapping(mapping(raw.get("raw")).get("metadata")).get("creditLedgerTotals"))
        charged = first_number(ledger.get("credits"), charged, evidence.get("charged"))
        refunded = first_number(ledger.get("refundedCredits"), evidence.get("refunded"))
    elif SERVICE in {"pip2api", "ijm2api", "jdc2api"}:
        isolated = task.get("_cost_balance_isolated", evidence.get("balance_isolated"))
        # Legacy actual_cost may predate overlap protection. Unknown is safer.
        charged = number(task.get("actual_cost")) if isolated is True else None
        if charged is not None:
            source = "isolated_balance"
    elif SERVICE == "nim2api":
        isolated = task.get("_cost_balance_isolated", evidence.get("balance_isolated"))
        charged = first_number(evidence.get("charged"), task.get("actual_cost") if isolated is True else None)
        source = "actual" if evidence.get("charged") is not None else "isolated_balance" if charged is not None else source
        settled = number(task.get("actual_cost"))
        if charged is not None and settled is not None and evidence.get("charged") is not None:
            # A known isolated terminal balance can confirm a partial/full refund.
            if isolated is True and settled <= charged:
                refunded = charged - settled
    if charged is not None and source != "isolated_balance":
        source = "actual"
    if charged is None and SERVICE == "hg2api":
        # HeyGen apiCreditCost is synthesized by the client, not a debit receipt.
        source = "formula" if quoted is not None else "unknown"
    duration = first_number(evidence.get("duration"), submit.get("duration"),
                            submit.get("duration_sec"), submit.get("videoDuration"), request.get("duration"))
    model = caller.get("model") or task.get("model") or request.get("model") or ""
    execution = task.get("channel") or SERVICE
    attempt = task.get("generation_id") or task.get("run_id") or protocol.get("job_ref")
    row = {"task_id": str(task.get("id") or task.get("task_id") or ""),
           "attempt_id": str(attempt or (str(task.get("id") or "") + ":pending")),
           "upstream_task_id": str(task.get("generation_id") or task.get("run_id") or ""),
           "channel": SERVICE, "execution_source": str(execution), "model": str(model),
           "upstream_model": str(request.get("upstream_model") or submit.get("model") or model),
           "duration": duration, "requested_duration": first_number(caller.get("duration"), duration),
           "reference_seconds": number(reference), "requested_reference_seconds": requested_reference,
           "billable_reference_seconds": first_number(evidence.get("billable_reference_seconds"), submit.get("billable_reference_seconds")),
           "resolution": str(submit.get("resolution") or request.get("resolution") or "unknown"),
           "billing_context": context, "unit": "credits", "charged": charged,
           "refunded": min(refunded, charged) if refunded is not None and charged is not None else refunded,
           "quoted": quoted, "source": source, "status": str(task.get("status") or "unknown"),
           "observed_at": task.get("updated_at") or task.get("created_at") or 0}
    if row["reference_seconds"] is None:
        # Private collection hint consumed before normalization, never exported.
        candidates = ([item for item in uploads if mapping(item).get("kind") == "video"]
                      if isinstance(uploads, list) else videos)
        urls = []
        for video in candidates:
            value = video if isinstance(video, str) else (mapping(video).get("url") or mapping(video).get("value"))
            if isinstance(value, str) and value.startswith(("https://", "http://")):
                urls.append(value)
        if urls:
            row["_reference_urls"] = urls
    if SERVICE == "mag2api":
        records = billing_evidence(attempts, upstream.get("cost_evidence"))["attempts"]
        result = []
        for receipt in records:
            item = {**row, "attempt_id": receipt["id"], "upstream_task_id": receipt["id"], "charged": receipt.get("charged"),
                    "refunded": receipt.get("refunded"), "source": "actual"}
            if receipt.get("id") != str(attempt):
                item["status"] = "failed"
            result.append(item)
        if result:
            if str(attempt) not in {item["attempt_id"] for item in result}:
                result.append(row)
            return result
    return [row]


def billing_evidence(attempts: list, previous: Any = None) -> dict:
    """Preserve numeric Magnific ledger receipts beyond the bounded poll log."""
    records = {str(item["id"]): dict(item) for item in mapping(previous).get("attempts", [])
               if isinstance(item, dict) and item.get("id")}
    for attempt in attempts:
        generated = mapping(mapping(attempt).get("generate"))
        identity = generated.get("generationId")
        if identity and number(generated.get("actualCost")) is not None:
            records.setdefault(str(identity), {"id": str(identity)}).update(charged=number(generated["actualCost"]))
        poll = mapping(mapping(attempt).get("poll"))
        identity = poll.get("generationId")
        ledger = mapping(mapping(mapping(poll.get("raw")).get("metadata")).get("creditLedgerTotals"))
        if identity and ledger:
            record = records.setdefault(str(identity), {"id": str(identity)})
            for field, key in (("charged", "credits"), ("refunded", "refundedCredits")):
                if number(ledger.get(key)) is not None:
                    record[field] = number(ledger[key])
            record["status"] = poll.get("status")
    return {"attempts": list(records.values())}
