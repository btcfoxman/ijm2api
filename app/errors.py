"""Stable caller-facing messages for Dreamina failures, including old task rows."""

from __future__ import annotations


def public_error(code: str, message: str) -> dict[str, str]:
    raw = (message or "").lower()
    kind = (code or "").upper()
    if kind == "CONTENT_MODERATION_FAILED" or any(token in raw for token in (
        "'code': 12009", "'code': 12011", "content did not meet platform requirements",
        "output video may contain inappropriate content", "outputaudiocopyright",
    )):
        return {"code": "CONTENT_MODERATION_FAILED",
                "message": "检测到内容有敏感或违规情况，请修改后重试，积分已返还～"}
    if kind == "VIDEO_EDIT_CONSTRAINT" or "video editing task" in raw:
        return {"code": "VIDEO_EDIT_CONSTRAINT", "message": "素材时长不支持，请修改后再试~"}
    if kind == "UPSTREAM_SUBMIT_OUTCOME_UNKNOWN" or kind == "UPSTREAM_TIMEOUT":
        return {"code": kind, "message": "提交结果未确认，请核对上游任务，暂勿重复提交~"}
    if "media download failed" in raw or kind == "MEDIA_DOWNLOAD_FAILED":
        return {"code": "MEDIA_DOWNLOAD_FAILED", "message": "素材下载失败，请检查素材链接后重试~"}
    if "reference media exceeds" in raw:
        return {"code": "MEDIA_LIMIT_EXCEEDED", "message": "素材超限，请修改后再试~"}
    if "wrong content type" in raw:
        return {"code": "MEDIA_FORMAT_UNSUPPORTED", "message": "素材格式不支持，请修改后再试~"}
    if "web upload failed" in raw or kind == "MEDIA_UPLOAD_FAILED":
        return {"code": "MEDIA_UPLOAD_FAILED", "message": "上游维护中，请稍后再试~"}
    if kind in {"ACCOUNT_POOL_UNAVAILABLE", "BROWSERERROR", "ACCOUNT_MISSING"}:
        return {"code": kind, "message": "上游维护中，请稍后再试~"}
    return {"code": kind or "UPSTREAM_FAILED", "message": "生成失败，积分已返还，请重试~"}
