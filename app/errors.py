"""Stable caller-facing messages for Dreamina failures, including old task rows."""

from __future__ import annotations

import re


CONTENT_MODERATION_MESSAGE = "检测到内容可能违规，请修改提示词或参考素材后重试~"


def upstream_review_error(code: str, message: str) -> str | None:
    """Classify observed terminal review failures without inferring refunds."""
    by_code = {"2059": "UPSTREAM_REVIEW_FAILED", "1064": "UPSTREAM_REVIEW_SERVICE_ERROR",
               "2041": "UPSTREAM_IMAGE_REVIEW_FAILED"}
    by_name = {"SafeCenterReviewFail": "UPSTREAM_REVIEW_FAILED",
               "ReviewRpc": "UPSTREAM_REVIEW_SERVICE_ERROR",
               "OutputImageRisk": "UPSTREAM_IMAGE_REVIEW_FAILED",
               "This image may contain inappropriate content": "UPSTREAM_IMAGE_REVIEW_FAILED"}
    return by_code.get(str(code or "").strip()) or by_name.get((message or "").strip())


def public_error(code: str, message: str) -> dict[str, str]:
    raw = (message or "").lower()
    kind = (code or "").upper()
    if (kind == 'OPERATOR_CONTENT_RESTRICTED' and message ==
            '检测到内容可能涉及版权或受限内容，请修改后重试，额度已返还~~'):
        return {'code': kind, 'message': message}
    # This exact legacy signature was emitted only by the account identity
    # check before the generation POST. Keep stored records unchanged.
    if (kind == "UPSTREAM_SUBMIT_OUTCOME_UNKNOWN" and message ==
            "Dreamina identity read could not complete; automatic resubmission is disabled"):
        kind = "ACCOUNT_VERIFICATION_FAILED"
    # Canvas's generic terminal failures use the caller's "possibly unsafe"
    # vocabulary, including legacy rows and codes beyond the observed 20115
    # third-party rejection. This is presentation, not a confirmed policy
    # verdict or refund. Submission/transport uncertainty keeps its category.
    canvas_terminal_failure = (
        kind not in {"UPSTREAM_SUBMIT_OUTCOME_UNKNOWN", "UPSTREAM_TIMEOUT"}
        and not kind.startswith(("CLI_", "CLI."))
        and re.fullmatch(r"Canvas generation failed(?: \([^\r\n()]{1,80}\))?",
                         (message or "").strip(), re.IGNORECASE)
    )
    if kind in {"20115", "CONTENT_MODERATION_FAILED"} or canvas_terminal_failure:
        return {"code": "CONTENT_MODERATION_FAILED", "message": CONTENT_MODERATION_MESSAGE}
    if (kind in {"INPUT_AUDIO_DURATION_UNSUPPORTED", "INPUTAUDIODURATIONERROR"} or
            "audio clip is too long or too short" in raw):
        return {"code": "INPUT_AUDIO_DURATION_UNSUPPORTED",
                "message": "素材时长不支持，请修改后再试~"}
    if (kind == "INPUT_IMAGE_CONTENT_VIOLATION" or
            ("image you uploaded" in raw and "community guidelines" in raw)):
        return {"code": "INPUT_IMAGE_CONTENT_VIOLATION",
                "message": "检测到图片有敏感或违规内容，积分已返还，请重试~"}
    if (kind == "OUTPUT_VIDEO_COPYRIGHT" or
            "video may contain third-party content" in raw):
        return {"code": "OUTPUT_VIDEO_COPYRIGHT",
                "message": "生成的视频内容违规，请修改描述后重试，积分已返还~"}
    if (kind == "INPUT_IMAGE_SIZE_UNSUPPORTED" or
            "image width or height is too large or too small" in raw):
        return {"code": "INPUT_IMAGE_SIZE_UNSUPPORTED",
                "message": "参考图片尺寸不符合模型要求，请调整图片尺寸后重试~"}
    if any(token in raw for token in (
        "'code': 12009", "'code': 12011", "content did not meet platform requirements",
        "output video may contain inappropriate content", "outputaudiocopyright",
    )):
        return {"code": "CONTENT_MODERATION_FAILED",
                "message": CONTENT_MODERATION_MESSAGE}
    if kind in {'12009', '12011', '2052'} or any(token in raw for token in (
        'this generation may violate our terms of service', 'generation has been blocked')):
        return {'code': 'CONTENT_MODERATION_FAILED',
                'message': CONTENT_MODERATION_MESSAGE}
    if kind == 'CLI_CAPABILITY_UNAVAILABLE' and 'does not support duration=' in raw:
        return {'code': 'INPUT_AUDIO_DURATION_UNSUPPORTED', 'message': '素材时长不支持，请修改后再试~'}
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
    if kind in {"ACCOUNT_POOL_UNAVAILABLE", "BROWSERERROR", "ACCOUNT_MISSING", "ACCOUNT_VERIFICATION_FAILED"}:
        return {"code": kind, "message": "上游维护中，请稍后再试~"}
    if kind.startswith(('CLI_', 'CLI.')) or kind in {'RESULT_TRANSFER_FAILED', 'MEDIA_UPLOAD_PENDING'}:
        return {'code': kind, 'message': '上游维护中，请稍后再试~'}
    review_kind = kind if kind in {"UPSTREAM_REVIEW_FAILED", "UPSTREAM_REVIEW_SERVICE_ERROR",
                                  "UPSTREAM_IMAGE_REVIEW_FAILED"} else None
    if not review_kind and kind in {"UPSTREAM_FAILED", "2059", "1064", "2041", ""}:
        review_kind = upstream_review_error(code, message)
    if review_kind == "UPSTREAM_REVIEW_FAILED":
        return {"code": review_kind, "message": "上游审核未通过，请检查提示词和参考素材后重试~"}
    if review_kind == "UPSTREAM_REVIEW_SERVICE_ERROR":
        return {"code": review_kind, "message": "上游审核服务异常，请稍后重试~"}
    if review_kind == "UPSTREAM_IMAGE_REVIEW_FAILED":
        return {"code": review_kind, "message": "上游图片内容审核未通过，请调整提示词或参考素材后重试~"}
    return {"code": kind or "UPSTREAM_FAILED", "message": "生成失败，积分已返还，请重试~"}
