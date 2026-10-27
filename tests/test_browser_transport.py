import json
import shutil
import subprocess
import unittest
import urllib.parse
from unittest.mock import Mock, patch

from app.browser import BrowserError, BrowserRejected, CDPBrowser
from app.client import DreaminaClient


@unittest.skipUnless(shutil.which("node"), "Node.js is required for page-context execution")
class CreditRegionContextTests(unittest.TestCase):
    def expression(self, path="/commerce/v1/benefits/user_credit", **kwargs):
        browser = CDPBrowser(19301)
        with patch.object(browser, "evaluate", return_value=json.dumps(
                {"http": 200, "body": {"ret": "0", "data": {}}})) as evaluate:
            browser.post(path, {}, **kwargs)
        return evaluate.call_args.args[0]

    def execute_contexts(self, expressions, contexts):
        script = r"""
const vm = require('node:vm');
let input = '';
process.stdin.on('data', (part) => { input += part; });
process.stdin.on('end', async () => {
  const {expressions, contexts} = JSON.parse(input);
  const calls = [], values = [];
  const sandbox = {URL, location: {hostname: 'dreamina.capcut.com'}};
  sandbox.fetch = async (url, options) => {
    calls.push({url, options});
    if (sandbox.throwFetch) throw new Error('synthetic network failure');
    return {status: 200, json: async () => ({ret: '0', data: {credit: {gift_credit: 7}}})};
  };
  vm.createContext(sandbox);
  for (let i = 0; i < contexts.length; i++) {
    const item = contexts[i];
    if (item.sdk === undefined) delete sandbox.__locationInfo;
    else sandbox.__locationInfo = item.sdk;
    sandbox.location.hostname = item.hostname || 'dreamina.capcut.com';
    sandbox.throwFetch = !!item.throwFetch;
    values.push(await vm.runInContext(expressions[i % expressions.length], sandbox));
  }
  process.stdout.write(JSON.stringify({calls, values}));
});
"""
        result = subprocess.run([shutil.which("node"), "-e", script],
                                input=json.dumps({"expressions": expressions, "contexts": contexts}),
                                text=True, capture_output=True, timeout=10, check=True)
        return json.loads(result.stdout)

    def test_us_page_sdk_credit_check_uses_verified_origin_and_appid(self):
        context = {"sdk": {"code": "US", "domain": {
            "commerce_domain": "https://commerce-us-ttp2.us.capcut.com"}}}
        client = DreaminaClient({"id": 1, "cdp_port": 19301,
                                 "proxy_url": "socks5://proxy.test:20002"})
        observed = []

        def evaluate(expression, **_):
            result = self.execute_contexts([expression], [context])
            observed.extend(result["calls"])
            return result["values"][0]

        with patch.object(client.browser, "evaluate", side_effect=evaluate):
            self.assertEqual(client.account_state(max_attempts=1)["available_balance"], 7)
        self.assertEqual(len(observed), 1)
        self.assertEqual(observed[0]["url"],
                         "https://commerce-us-ttp2.us.capcut.com/commerce/v1/benefits/user_credit")
        self.assertEqual(observed[0]["options"]["headers"],
                         {"content-type": "application/json", "appid": "513641"})
        self.assertEqual(observed[0]["options"]["credentials"], "include")

    def test_sg_unknown_and_malicious_contexts_preserve_sg_request(self):
        bad_domains = ["https://commerce-us-ttp2.us.capcut.com:443",
                       "https://commerce-us-ttp2.us.capcut.com:8443",
                       "https://user@commerce-us-ttp2.us.capcut.com",
                       "https://commerce-us-ttp2.us.capcut.com.attacker.test",
                       "http://commerce-us-ttp2.us.capcut.com",
                       "https://commerce-us-ttp2.us.capcut.com/path",
                       "https://commerce-us-ttp2.us.capcut.com?token=private"]
        contexts = [{}, {"sdk": {"code": "SG", "domain": {
            "commerce_domain": "https://commerce-api-sg.capcut.com"}}}]
        contexts += [{"sdk": {"code": "US", "domain": {"commerce_domain": value}}}
                     for value in bad_domains]
        contexts += [{"hostname": "attacker.test", "sdk": {"code": "US", "domain": {
            "commerce_domain": "https://commerce-us-ttp2.us.capcut.com"}}}]
        result = self.execute_contexts([self.expression()], contexts)
        self.assertEqual(len(result["calls"]), len(contexts))
        for call in result["calls"]:
            self.assertEqual(call["url"],
                             "https://commerce-api-sg.capcut.com/commerce/v1/benefits/user_credit")
            self.assertEqual(call["options"]["headers"], {"content-type": "application/json"})

    def test_context_is_read_again_per_call_without_cross_account_cache(self):
        us = {"sdk": {"code": "US", "domain": {
            "commerce_domain": "https://commerce-us-ttp2.us.capcut.com"}}}
        sg = {"sdk": {"code": "SG", "domain": {
            "commerce_domain": "https://commerce-api-sg.capcut.com"}}}
        result = self.execute_contexts([self.expression()], [us, sg, us, {}])
        self.assertEqual(["appid" in call["options"]["headers"] for call in result["calls"]],
                         [True, False, True, False])

    def test_failed_us_fetch_is_not_replayed_in_sg(self):
        result = self.execute_contexts([self.expression()], [{"throwFetch": True, "sdk": {
            "code": "US", "domain": {"commerce_domain": "https://commerce-us-ttp2.us.capcut.com"}}}])
        self.assertEqual(len(result["calls"]), 1)
        self.assertEqual(json.loads(result["values"][0]), {"transport_error": "fetch"})

    def test_us_token_submit_and_poll_share_region_without_altering_body(self):
        us = {"sdk": {"code": "US", "domain": {
            "commerce_domain": "https://commerce-us-ttp2.us.capcut.com"}}}
        paths = ["/mweb/v1/get_upload_token", "/mweb/v1/aigc_draft/generate",
                 "/mweb/v1/get_history_by_ids"]
        for path in paths:
            with self.subTest(path=path):
                expression = self.expression(path, query={"aid": "513695", "region": "SG",
                                                         "device_platform": "web", "generate_id": "same-id"})
                call = self.execute_contexts([expression], [us])["calls"][0]
                parsed = urllib.parse.urlsplit(call["url"])
                self.assertEqual(parsed.netloc, "dreamina-api-us-ttp2.us.capcut.com")
                self.assertEqual(parsed.path, path)
                self.assertEqual(dict(urllib.parse.parse_qsl(parsed.query)), {
                    "aid": "513641", "region": "US", "device_platform": "web", "generate_id": "same-id"})
                self.assertEqual(call["options"]["headers"]["appid"], "513641")
                self.assertEqual(call["options"]["body"], "{}")
        call = self.execute_contexts([self.expression(paths[0], query={"aid": "513695"})], [us])["calls"][0]
        self.assertNotIn("region", dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(call["url"]).query)))

    def test_sg_or_unknown_mweb_context_keeps_original_query_and_headers(self):
        contexts = [{}, {"sdk": {"code": "SG", "domain": {
            "commerce_domain": "https://commerce-api-sg.capcut.com"}}},
            {"sdk": {"code": "US", "domain": {
                "commerce_domain": "https://commerce-us-ttp2.us.capcut.com:443"}}}]
        expression = self.expression("/mweb/v1/aigc_draft/generate", query={"aid": "513695", "region": "SG"})
        calls = self.execute_contexts([expression], contexts)["calls"]
        for call in calls:
            self.assertEqual(call["url"],
                             "https://mweb-api-sg.capcut.com/mweb/v1/aigc_draft/generate?aid=513695&region=SG")
            self.assertEqual(call["options"]["headers"], {"content-type": "application/json"})

    def test_mweb_context_is_not_cached_or_retried_across_regions(self):
        us = {"sdk": {"code": "US", "domain": {
            "commerce_domain": "https://commerce-us-ttp2.us.capcut.com"}}}
        expression = self.expression("/mweb/v1/aigc_draft/generate")
        result = self.execute_contexts([expression], [us, {}, {**us, "throwFetch": True}])
        self.assertEqual(len(result["calls"]), 3)
        self.assertEqual([urllib.parse.urlsplit(c["url"]).hostname for c in result["calls"]],
                         ["dreamina-api-us-ttp2.us.capcut.com", "mweb-api-sg.capcut.com",
                          "dreamina-api-us-ttp2.us.capcut.com"])
        self.assertEqual(json.loads(result["values"][-1]), {"transport_error": "fetch"})

    def test_commerce_write_and_native_paths_keep_existing_transport(self):
        us = {"sdk": {"code": "US", "domain": {
            "commerce_domain": "https://commerce-us-ttp2.us.capcut.com"}}}
        paths = ["/commerce/v1/benefits/credit_receive"]
        for path in paths:
            expression = self.expression(path)
            self.assertNotIn("__locationInfo", expression)
            call = self.execute_contexts([expression], [us])["calls"][0]
            self.assertNotIn("appid", call["options"]["headers"])
            self.assertEqual(call["url"], ("https://commerce-api-sg.capcut.com" if
                             path.startswith("/commerce/") else "https://mweb-api-sg.capcut.com") + path)
        native = self.expression(native_client=True)
        self.assertNotIn("__locationInfo", native)
        self.assertIn("req(545525).U(container,client)", native)
        self.assertIn("https://commerce-api-sg.capcut.com/commerce/v1/benefits/user_credit", native)
        native_generate = self.expression("/mweb/v1/aigc_draft/generate", native_client=True,
                                          query={"region": "SG", "aid": "513695"})
        self.assertNotIn("__locationInfo", native_generate)
        self.assertIn("https://mweb-api-sg.capcut.com/mweb/v1/aigc_draft/generate", native_generate)
        self.assertIn('"region": "SG"', native_generate)


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

    def test_image_audit_empty_ret_is_allowed_only_when_requested(self):
        browser = CDPBrowser(19301)
        reply = json.dumps({"http": 200, "body": {"ret": "", "errmsg": ""}})
        with patch.object(browser, "evaluate", return_value=reply):
            with self.assertRaises(BrowserRejected):
                browser.post("/mweb/v1/imagex/submit_audit_job", {"uri_list": ["private"]})
            self.assertEqual(browser.post(
                "/mweb/v1/imagex/submit_audit_job", {"uri_list": ["private"]},
                allow_empty_ret=True), {})

    def test_image_native_client_uses_page_interceptors_and_decodes_data(self):
        browser = CDPBrowser(19301)
        reply = json.dumps({"http": 200, "body": {"ret": "0", "data": {"run_id": "abc"}}})
        with patch.object(browser, "evaluate", return_value=reply) as evaluate:
            self.assertEqual(browser.post("/mweb/v1/aigc_draft/generate", {"submit_id": "abc"},
                                          query={"aid": "513695"}, native_client=True),
                             {"run_id": "abc"})
        expression = evaluate.call_args.args[0]
        for interceptor in ("req(545525).U(container,client)", "req(561634).vo(client)",
                            "req(547986).Wh(client)", "req(670446).P(client)",
                            "req(431316).m(client)"):
            self.assertIn(interceptor, expression)
        self.assertIn("__reactFiber$", expression)
        self.assertIn("params:{\"aid\": \"513695\"}", expression)

        with patch.object(browser, "evaluate", return_value=json.dumps(
                {"transport_error": "native-container-unavailable"})):
            with self.assertRaisesRegex(BrowserError, "service container is unavailable"):
                browser.post("/mweb/v1/aigc_draft/generate", {}, native_client=True)


if __name__ == "__main__":
    unittest.main()
