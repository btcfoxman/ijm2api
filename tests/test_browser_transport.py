import json
import unittest
from unittest.mock import Mock, patch

from app.browser import BrowserError, BrowserRejected, CDPBrowser
from app.client import DreaminaClient


class BrowserTransportTests(unittest.TestCase):
    def test_credit_read_retries_transient_browser_failure(self):
        client = DreaminaClient({"id": 1, "cdp_port": 19301,
                                 "proxy_url": "socks5://proxy.test:20002"})

        class Browser:
            calls = 0

            def post(self, path, body):
                self.calls += 1
                self.assert_path = path
                if self.calls == 1:
                    raise BrowserError("Dreamina browser fetch failed")
                return {"credit": {"vip_credit": 5, "gift_credit": 2,
                                   "purchase_credit": 3}}

        client.browser = Browser()
        with patch("app.client.time.sleep"):
            state = client.account_state()
        self.assertEqual(state["available_balance"], 10)
        self.assertEqual(client.browser.calls, 2)
        self.assertEqual(client.browser.assert_path, "/commerce/v1/benefits/user_credit")

    def test_credit_read_does_not_retry_upstream_rejection(self):
        client = DreaminaClient({"id": 1, "cdp_port": 19301,
                                 "proxy_url": "socks5://proxy.test:20002"})

        class Browser:
            calls = 0

            def post(self, *_):
                self.calls += 1
                raise BrowserRejected("Dreamina HTTP 403")

        client.browser = Browser()
        with self.assertRaises(BrowserRejected):
            client.account_state()
        self.assertEqual(client.browser.calls, 1)

    def test_task_credit_read_has_short_timeout_and_restores_browser(self):
        client = DreaminaClient({"id": 1, "cdp_port": 19301,
                                 "proxy_url": "socks5://proxy.test:20002"})

        class Browser:
            timeout = 90
            calls = 0

            def post(self, *_):
                self.calls += 1
                self.assert_timeout = self.timeout
                raise BrowserError("Dreamina browser fetch failed")

        client.browser = Browser()
        with patch("app.client.time.sleep"), self.assertRaises(BrowserError):
            client.account_state(max_attempts=2, timeout_seconds=6)
        self.assertEqual(client.browser.calls, 2)
        self.assertEqual(client.browser.assert_timeout, 6)
        self.assertEqual(client.browser.timeout, 90)

    def test_poll_prefers_raw_failure_over_none_localization_placeholder(self):
        client = DreaminaClient({"id": 1, "cdp_port": 19301,
                                 "proxy_url": "socks5://proxy.test:20002"})
        client.browser = Mock()
        client.browser.post.return_value = {"submit-1": {
            "status": 30, "task": {"status": 30, "resp_ret": {"ret": "0"}},
            "fail_code": "2052", "fail_msg": "OutputAudioCopyright",
            "fail_starling_message": "[none]",
            "fail_starling_key": "ErrMessage_APP_OutputAudioRisk",
        }}

        result = client.poll("submit-1", "history-1")

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error_code"], "2052")
        self.assertEqual(result["error"], "OutputAudioCopyright")

    def test_fetch_and_non_json_failures_have_safe_diagnostics(self):
        browser = CDPBrowser(19301)
        with patch.object(browser, "evaluate", return_value=json.dumps(
                {"transport_error": "fetch"})) as evaluate:
            with self.assertRaisesRegex(BrowserError, "browser fetch failed") as caught:
                browser.post("/mweb/v1/aigc_draft/generate", {"prompt": "private text"})
            self.assertEqual(evaluate.call_count, 1)
            self.assertNotIn("private text", str(caught.exception))
        with patch.object(browser, "evaluate", return_value=json.dumps(
                {"transport_error": "json", "http": 503})):
            with self.assertRaisesRegex(BrowserRejected, "HTTP 503 returned non-JSON"):
                browser.post("/commerce/v1/benefits/user_credit", {})


if __name__ == "__main__":
    unittest.main()
