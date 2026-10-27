"""Observed upstream review failures must not imply policy guilt or refunds."""

import gc
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from app.catalog import normalize_request
from app.errors import public_error, upstream_review_error
from app.service import Service
from app.store import Store


class ReviewErrorTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.addCleanup(gc.collect)
        self.store = Store(str(Path(directory.name) / "tasks.db"))
        self.service = Service(self.store)
        self.addCleanup(self.service.stop)
        self.serial = 0

    def task(self):
        self.serial += 1
        request = {"model": "sd-2-5", "prompt": "A blue sky", "duration": 4}
        task = self.store.create_task(f"ijm_review_{self.serial}", request,
                                      normalize_request(request))
        return self.store.update_task(task["id"], {"status": "submitted",
            "run_id": "existing-submit", "thread_id": "existing-history"})

    def poll_failure(self, upstream_code, reason):
        task = self.task()
        client = Mock()
        client.poll.return_value = {"status": "failed", "error_code": upstream_code,
                                    "error": reason}
        with patch("app.service.settle_failed_balance") as settle, \
             patch("app.service.time.sleep"):
            self.service._poll(task["id"], client, float("inf"))
        settle.assert_called_once()
        client.submit.assert_not_called()
        return self.store.task(task["id"])

    def test_poll_classifies_code_only_and_name_only_and_keeps_raw_reason(self):
        for upstream_code, reason, expected in (
            ("2059", "", "UPSTREAM_REVIEW_FAILED"),
            ("1064", "", "UPSTREAM_REVIEW_SERVICE_ERROR"),
            ("", "SafeCenterReviewFail", "UPSTREAM_REVIEW_FAILED"),
            ("", "ReviewRpc", "UPSTREAM_REVIEW_SERVICE_ERROR"),
            ("2041", "", "UPSTREAM_IMAGE_REVIEW_FAILED"),
            ("", "OutputImageRisk", "UPSTREAM_IMAGE_REVIEW_FAILED"),
            ("", "This image may contain inappropriate content", "UPSTREAM_IMAGE_REVIEW_FAILED"),
        ):
            with self.subTest(upstream_code=upstream_code, reason=reason):
                task = self.poll_failure(upstream_code, reason)
                self.assertEqual(task["status"], "failed")
                self.assertEqual(task["error_code"], expected)
                self.assertEqual(task["error_message"], reason or "Dreamina video generation failed")
                self.assertEqual(self.service.public_task(task)["error"]["code"], expected)

    def test_legacy_rows_are_normalized_without_modifying_audit_fields(self):
        for raw, code, message in (
            ("SafeCenterReviewFail", "UPSTREAM_REVIEW_FAILED",
             "上游审核未通过，请检查提示词和参考素材后重试~"),
            ("ReviewRpc", "UPSTREAM_REVIEW_SERVICE_ERROR", "上游审核服务异常，请稍后重试~"),
            ("OutputImageRisk", "UPSTREAM_IMAGE_REVIEW_FAILED",
             "上游图片内容审核未通过，请调整提示词或参考素材后重试~"),
            ("This image may contain inappropriate content", "UPSTREAM_IMAGE_REVIEW_FAILED",
             "上游图片内容审核未通过，请调整提示词或参考素材后重试~"),
        ):
            with self.subTest(raw=raw):
                task = self.task()
                self.store.update_task(task["id"], {"status": "failed",
                    "error_code": "UPSTREAM_FAILED", "error_message": raw})
                before = self.store.task(task["id"])
                self.assertEqual(self.service.public_task(before)["error"],
                                 {"code": code, "message": message})
                self.assertEqual(self.store.task(task["id"]), before)

    def test_public_code_only_and_new_enums_never_claim_refunds(self):
        for upstream_code, expected in (
            ("2059", "UPSTREAM_REVIEW_FAILED"),
            ("1064", "UPSTREAM_REVIEW_SERVICE_ERROR"),
            ("UPSTREAM_REVIEW_FAILED", "UPSTREAM_REVIEW_FAILED"),
            ("UPSTREAM_REVIEW_SERVICE_ERROR", "UPSTREAM_REVIEW_SERVICE_ERROR"),
            ("2041", "UPSTREAM_IMAGE_REVIEW_FAILED"),
            ("UPSTREAM_IMAGE_REVIEW_FAILED", "UPSTREAM_IMAGE_REVIEW_FAILED"),
        ):
            with self.subTest(upstream_code=upstream_code):
                error = public_error(upstream_code, "")
                self.assertEqual(error["code"], expected)
                self.assertNotIn("返还", error["message"])
                self.assertNotIn("违规", error["message"])

    def test_specific_localized_failures_keep_priority_over_review_codes(self):
        for upstream_code, raw, expected in (
            ("2059", "The audio clip is too long or too short for this model.",
             "INPUT_AUDIO_DURATION_UNSUPPORTED"),
            ("1064", "The image you uploaded violates our Community Guidelines.",
             "INPUT_IMAGE_CONTENT_VIOLATION"),
            ("2059", "The video may contain third-party content", "OUTPUT_VIDEO_COPYRIGHT"),
            ("2041", "The image you uploaded violates our Community Guidelines.",
             "INPUT_IMAGE_CONTENT_VIOLATION"),
        ):
            with self.subTest(expected=expected):
                task = self.poll_failure(upstream_code, raw)
                self.assertEqual(task["error_code"], expected)
                self.assertEqual(public_error(upstream_code, raw)["code"], expected)

    def test_existing_moderation_and_temporary_codes_keep_priority(self):
        for upstream_code, raw, expected in (
            ("2052", "OutputAudioCopyright", "CONTENT_MODERATION_FAILED"),
            ("12009", "SafeCenterReviewFail", "CONTENT_MODERATION_FAILED"),
            ("12011", "ReviewRpc", "CONTENT_MODERATION_FAILED"),
            ("99999", "ReviewRpc", "UPSTREAM_TEMPORARY"),
            ("2052", "OutputImageRisk", "CONTENT_MODERATION_FAILED"),
            ("2059", "OutputImageRisk", "UPSTREAM_REVIEW_FAILED"),
            ("1064", "OutputImageRisk", "UPSTREAM_REVIEW_SERVICE_ERROR"),
        ):
            with self.subTest(upstream_code=upstream_code):
                self.assertEqual(self.poll_failure(upstream_code, raw)["error_code"], expected)

    def test_review_names_are_exact_and_submit_uncertainty_is_preserved(self):
        for raw in ("ReviewRpcTimeout", "not SafeCenterReviewFail", "reviewrpc",
                    "OutputImageRiskTimeout", "This image may contain inappropriate content in a longer message"):
            with self.subTest(raw=raw):
                self.assertIsNone(upstream_review_error("", raw))
                self.assertEqual(public_error("UPSTREAM_FAILED", raw)["code"], "UPSTREAM_FAILED")
        self.assertEqual(public_error("UPSTREAM_SUBMIT_OUTCOME_UNKNOWN", "ReviewRpc")["code"],
                         "UPSTREAM_SUBMIT_OUTCOME_UNKNOWN")


if __name__ == "__main__":
    unittest.main()
