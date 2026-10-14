"""Credit accounting must not prevent a Dreamina video submission."""

import gc
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from app.browser import BrowserError, BrowserRejected
from app.catalog import normalize_request
from app.errors import public_error
from app.service import Service
from app.store import Store


class ServiceBalanceTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.addCleanup(gc.collect)
        self.store = Store(str(Path(directory.name) / "tasks.db"))
        self.service = Service(self.store)
        self.addCleanup(self.service.stop)
        account = self.store.upsert_account({
            "name": "test-account", "cdp_port": 19301,
            "proxy_url": "socks5://proxy.test:20002",
        })
        self.account_id = account["id"]
        self.store.update_account(self.account_id, {"status": "active", "balance": 100})
        request = {"model": "sd-2-5", "prompt": "A blue sky", "duration": 4}
        self.task = self.store.create_task("ijm_balance_test", request, normalize_request(request))
        self.client = Mock()
        self.client.account = self.store.account(self.account_id, private=True)
        self.client.upload_inputs.return_value = {"images": [], "videos": [], "audios": []}
        self.client.submit.return_value = {"run_id": "submit-1", "thread_id": "history-1"}
        self.client.poll.return_value = {
            "status": "succeeded", "result_urls": ["https://example.test/result.mp4"]}

    def run_task(self):
        with patch("app.service.DreaminaClient", return_value=self.client), \
             patch("app.browser_runtime.start_managed_browser", return_value=19301), \
             patch("app.browser_runtime.stop_managed_browser"):
            self.service._run(self.task["id"])
        return self.store.task(self.task["id"])

    def test_credit_read_failure_keeps_cached_balance_and_submits_once(self):
        self.client.account_state.side_effect = [
            BrowserError("Dreamina browser fetch failed"),
            {"available_balance": 80},
        ]

        task = self.run_task()

        self.assertEqual(task["status"], "succeeded")
        self.assertIsNone(task["balance_before"])
        self.assertEqual(task["balance_after"], 80)
        self.assertIsNone(task["actual_cost"])
        self.assertEqual(self.store.account(self.account_id)["balance"], 80)
        self.client.upload_inputs.assert_called_once()
        self.client.submit.assert_called_once()

    def test_credit_endpoint_rejection_does_not_mask_submit_rejection(self):
        self.client.account_state.side_effect = BrowserRejected("Dreamina HTTP 403")
        self.client.submit.side_effect = BrowserRejected("Dreamina HTTP 402")

        task = self.run_task()

        self.assertEqual(task["status"], "failed")
        self.assertEqual(task["error_code"], "UPSTREAM_REJECTED")
        self.assertEqual(task["balance_before"], None)
        self.assertEqual(self.store.account(self.account_id)["balance"], 100)
        self.client.submit.assert_called_once()

    def test_post_submit_credit_failure_does_not_change_success(self):
        self.client.account_state.side_effect = [
            {"available_balance": 100},
            BrowserError("Dreamina browser fetch failed"),
        ]

        task = self.run_task()

        self.assertEqual(task["status"], "succeeded")
        self.assertEqual(task["balance_before"], 100)
        self.assertIsNone(task["balance_after"])
        self.assertIsNone(task["actual_cost"])
        self.assertEqual(self.store.account(self.account_id)["balance"], 100)

    def test_output_audio_copyright_is_reported_as_content_moderation(self):
        self.store.update_task(self.task["id"], {"status": "submitted",
                                                 "account_id": self.account_id,
                                                 "run_id": "submit-1", "thread_id": "history-1"})
        self.client.poll.return_value = {"status": "failed", "error_code": "2052",
                                         "error": "OutputAudioCopyright"}

        self.service._poll(self.task["id"], self.client, float("inf"))

        task = self.store.task(self.task["id"])
        self.assertEqual(task["error_code"], "CONTENT_MODERATION_FAILED")
        self.assertEqual(public_error(task["error_code"], task["error_message"])["message"],
                         "检测到内容有敏感或违规情况，请修改后重试，积分已返还～")


if __name__ == "__main__":
    unittest.main()
