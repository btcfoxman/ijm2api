import json
import shutil
import subprocess
import unittest
import urllib.parse
from unittest.mock import Mock, patch

from app.browser import BrowserError, BrowserRejected, CDPBrowser, _RESOURCE_TIMING_BUFFER_SCRIPT
from app.client import DreaminaClient


class TimingSocket:
    """A CDP peer with unsolicited events and document-scoped init scripts."""

    def __init__(self, failure=None):
        self.failure = failure
        self.sent = []
        self.replies = []
        self.closed = False
        self.scripts = {}
        self.timeouts = []

    def settimeout(self, value):
        self.timeouts.append(value)

    def send(self, text):
        request = json.loads(text)
        self.sent.append(request)
        method = request['method']
        if self.failure == (method, 'socket'):
            raise OSError('synthetic private transport detail')
        if self.failure == (method, 'cdp'):
            self.replies.append({'id': request['id'], 'error': {'message': 'synthetic private CDP detail'}})
            return
        if method == 'Page.addScriptToEvaluateOnNewDocument':
            self.scripts['synthetic-script'] = request['params']['source']
            result = {'identifier': 'synthetic-script'}
        elif method == 'Runtime.evaluate':
            result = {'result': {'value': True}}
        elif method == 'Page.removeScriptToEvaluateOnNewDocument':
            self.scripts.pop(request['params']['identifier'], None)
            result = {}
        else:
            result = {}
        self.replies.extend([{'method': 'Page.frameNavigated', 'params': {}}, {'id': request['id'], 'result': result}])

    def recv(self):
        return json.dumps(self.replies.pop(0))

    def new_document_scripts(self):
        return list(self.scripts.values()) if not self.closed else []

    def close(self):
        self.closed = True


class ResourceTimingCaptureTests(unittest.TestCase):
    def test_cold_document_waits_for_false_and_transient_runtime_context_before_login(self):
        class ColdSocket(TimingSocket):
            def send(self, text):
                request = json.loads(text)
                if request['method'] == 'Runtime.evaluate':
                    self.sent.append(request)
                    self.evaluations = getattr(self, 'evaluations', 0) + 1
                    if self.evaluations == 1:
                        response = {'result': {'result': {'value': False}}}
                    elif self.evaluations == 2:
                        response = {'error': {'code': -32000, 'message': 'Cannot find default execution context'}}
                    else:
                        response = {'result': {'result': {'value': True}}}
                    self.replies.append({'id': request['id'], **response})
                    return
                super().send(text)

        socket, clock = ColdSocket(), [0.0]
        with patch('app.browser._page', return_value={'webSocketDebuggerUrl': 'ws://127.0.0.1/synthetic'}), \
             patch('app.browser.websocket.create_connection', return_value=socket), \
             patch('app.browser.time.monotonic', side_effect=lambda: clock[0]), \
             patch('app.browser.time.sleep', side_effect=lambda seconds: clock.__setitem__(0, clock[0] + seconds)):
            with CDPBrowser(19301, timeout=15).resource_timing_capture():
                self.assertEqual(socket.evaluations, 3)
                self.assertEqual(socket.new_document_scripts(), [_RESOURCE_TIMING_BUFFER_SCRIPT])
                self.assertFalse(socket.closed)
                self.assertEqual(socket.timeouts[-1], 15)
        self.assertAlmostEqual(clock[0], .4)
        self.assertTrue(socket.closed)
        self.assertEqual(socket.scripts, {})

    def test_unready_document_wait_is_bounded_without_entering_login(self):
        class UnreadySocket(TimingSocket):
            def send(self, text):
                request = json.loads(text)
                if request['method'] == 'Runtime.evaluate':
                    self.sent.append(request)
                    self.replies.append({'id': request['id'], 'result': {'result': {'value': False}}})
                    return
                super().send(text)

        socket, clock = UnreadySocket(), [0.0]
        with patch('app.browser._page', return_value={'webSocketDebuggerUrl': 'ws://127.0.0.1/synthetic'}), \
             patch('app.browser.websocket.create_connection', return_value=socket), \
             patch('app.browser.time.monotonic', side_effect=lambda: clock[0]), \
             patch('app.browser.time.sleep', side_effect=lambda seconds: clock.__setitem__(0, clock[0] + seconds)):
            with self.assertRaises(BrowserError):
                with CDPBrowser(19301).resource_timing_capture():
                    self.fail('login cannot begin before normal telemetry is ready')
        self.assertEqual(clock[0], 30)
        self.assertTrue(socket.closed)
        self.assertEqual(socket.scripts, {})

    def test_current_and_future_documents_share_one_live_session_until_cleanup(self):
        socket = TimingSocket()
        with patch('app.browser._page', return_value={'webSocketDebuggerUrl': 'ws://127.0.0.1/synthetic'}), \
             patch('app.browser.websocket.create_connection', return_value=socket) as connect:
            with CDPBrowser(19301, timeout=15).resource_timing_capture():
                self.assertFalse(socket.closed)
                self.assertEqual(socket.new_document_scripts(), [_RESOURCE_TIMING_BUFFER_SCRIPT])
                self.assertEqual(socket.sent[-1]['method'], 'Runtime.evaluate')
                self.assertEqual(socket.sent[-1]['params']['expression'], _RESOURCE_TIMING_BUFFER_SCRIPT)
                connect.assert_called_once_with('ws://127.0.0.1/synthetic', timeout=15, suppress_origin=True)
            self.assertTrue(socket.closed)
            self.assertEqual(socket.scripts, {})
        self.assertEqual([item['method'] for item in socket.sent], ['Page.enable', 'Page.addScriptToEvaluateOnNewDocument',
                                                                  'Runtime.evaluate', 'Page.removeScriptToEvaluateOnNewDocument'])

    def test_body_error_is_preserved_and_registration_is_removed(self):
        socket = TimingSocket()
        original = RuntimeError('synthetic login interruption')
        with patch('app.browser._page', return_value={'webSocketDebuggerUrl': 'ws://127.0.0.1/synthetic'}), \
             patch('app.browser.websocket.create_connection', return_value=socket):
            with self.assertRaises(RuntimeError) as caught:
                with CDPBrowser(19301).resource_timing_capture():
                    raise original
        self.assertIs(caught.exception, original)
        self.assertTrue(socket.closed)
        self.assertEqual(socket.scripts, {})

    def test_setup_failure_never_enters_login_and_cleans_registered_script(self):
        for method in ('Page.addScriptToEvaluateOnNewDocument', 'Runtime.evaluate'):
            for failure in ('socket', 'cdp'):
                with self.subTest(method=method, failure=failure):
                    socket = TimingSocket((method, failure))
                    with patch('app.browser._page', return_value={'webSocketDebuggerUrl': 'ws://127.0.0.1/synthetic'}), \
                         patch('app.browser.websocket.create_connection', return_value=socket):
                        with self.assertRaises(BrowserError) as caught:
                            with CDPBrowser(19301).resource_timing_capture():
                                self.fail('login must not start when timing retention is unavailable')
                    self.assertNotIn('private', str(caught.exception))
                    self.assertTrue(socket.closed)
                    self.assertEqual(socket.scripts, {})

    @unittest.skipUnless(shutil.which('node'), 'Node.js is required for timing buffer semantics')
    def test_full_buffer_late_sdk_and_new_document_retention_without_fabricated_identity(self):
        from app.account_automation import _IDENTITY

        program = r'''
const vm=require('vm'),payload=JSON.parse(require('fs').readFileSync(0,'utf8'));
const sdk='https://dreamina.capcut.com/passport/web/account/info/?sdk_version=observed-synthetic&aid=synthetic';
async function scenario(initial,lateAssets,includeSdk,origin='https://dreamina.capcut.com'){
 let capacity=250,entries=[],calls=[];
 const add=name=>{if(entries.length<capacity)entries.push({name});};
 for(let i=0;i<initial;i++)add('https://dreamina.capcut.com/assets/initial-'+i+'.js');
 const context={URL,AbortController,setTimeout:()=>0,clearTimeout:()=>{},location:{origin},
  performance:{getEntriesByType:()=>entries,setResourceTimingBufferSize:n=>{capacity=n}},
  fetch:async(url)=>{calls.push(url);return {ok:true,json:async()=>({data:{user_id_str:'synthetic-user',email:'operator@example.invalid'}})}}};
 const before=await vm.runInNewContext(payload.identity,context);
 const retained=vm.runInNewContext(payload.source,context);
 for(let i=0;i<lateAssets;i++)add('https://dreamina.capcut.com/assets/late-'+i+'.js');
 if(includeSdk)add(sdk);
 const after=await vm.runInNewContext(payload.identity,context);
 return {capacity,count:entries.length,calls,before,after,retained};
}
(async()=>{const results=[await scenario(250,0,true),await scenario(0,300,true),
 await scenario(250,300,false),await scenario(250,0,true,'https://unrelated.example.invalid')];
 process.stdout.write(JSON.stringify(results));})();
'''
        result = subprocess.run(['node', '-e', program],
                                input=json.dumps({'source': _RESOURCE_TIMING_BUFFER_SCRIPT, 'identity': _IDENTITY}),
                                text=True, capture_output=True, timeout=10, check=True)
        full, new_document, absent, foreign = json.loads(result.stdout)
        for item, expected_count in [(full, 251), (new_document, 301)]:
            self.assertEqual(item['capacity'], 5000)
            self.assertEqual(item['count'], expected_count)
            self.assertTrue(item['retained'])
            self.assertTrue(item['before']['transport_error'])
            self.assertTrue(item['after']['authenticated'])
            self.assertEqual(len(item['calls']), 1)
            self.assertIn('sdk_version=observed-synthetic&aid=synthetic', item['calls'][0])
        self.assertTrue(absent['after']['transport_error'])
        self.assertEqual(absent['calls'], [])
        self.assertFalse(foreign['retained'])
        self.assertEqual(foreign['capacity'], 250)
        self.assertEqual(foreign['calls'], [])


@unittest.skipUnless(shutil.which("node"), "Node.js is required for page-context execution")
class CreditRegionContextTests(unittest.TestCase):
    def expression(self, path="/commerce/v1/benefits/user_credit", *, body=None, **kwargs):
        browser = CDPBrowser(19301)
        with patch.object(browser, "evaluate", return_value=json.dumps(
                {"http": 200, "body": {"ret": "0", "data": {}}})) as evaluate:
            browser.post(path, {} if body is None else body, **kwargs)
        return evaluate.call_args.args[0]

    def execute_contexts(self, expressions, contexts):
        script = r"""
const vm = require('node:vm');
let input = '';
process.stdin.on('data', (part) => { input += part; });
process.stdin.on('end', async () => {
  const {expressions, contexts} = JSON.parse(input);
  const calls = [], values = [], reads = [];
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
    let domainReads = 0, originReads = 0;
    if (item.originSequence) {
      const readOrigin = () => item.originSequence[Math.min(originReads++, item.originSequence.length - 1)];
      const domain = item.originAccess === 'proxy'
        ? new Proxy({}, {get: (_, key) => key === 'commerce_domain' ? readOrigin() : undefined})
        : Object.defineProperty({}, 'commerce_domain', {get: readOrigin});
      Object.defineProperty(sandbox.__locationInfo, 'domain', {get: () => {domainReads++; return domain;}});
    }
    sandbox.location.hostname = item.hostname || 'dreamina.capcut.com';
    sandbox.throwFetch = !!item.throwFetch;
    values.push(await vm.runInContext(expressions[i % expressions.length], sandbox));
    reads.push({domain: domainReads, origin: originReads});
  }
  process.stdout.write(JSON.stringify({calls, values, reads}));
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

    def test_both_evidenced_us_commerce_origins_preserve_query_body_and_existing_headers(self):
        origins = ["https://commerce-us-ttp2.us.capcut.com", "https://commerce.us.capcut.com"]
        contexts = [{"sdk": {"code": "US", "domain": {"commerce_domain": origin}}} for origin in origins]
        for path in ["/commerce/v1/benefits/user_credit", "/commerce/v1/benefits/credit_receive"]:
            with self.subTest(path=path):
                expression = self.expression(path, query={"aid": "caller-aid", "source": "unchanged"})
                result = self.execute_contexts([expression], contexts)
                self.assertEqual(len(result['calls']), 2)
                for call, origin in zip(result['calls'], origins):
                    self.assertEqual(call['url'], origin + path + '?aid=caller-aid&source=unchanged')
                    self.assertEqual(call['options']['headers'], {'content-type': 'application/json', 'appid': '513641'})
                    self.assertEqual(call['options']['body'], '{}')
                    self.assertEqual(call['options']['credentials'], 'include')

    def test_new_us_origin_requires_exact_origin_us_code_and_official_page(self):
        domains = ['https://commerce.us.capcut.com:443', 'https://commerce.us.capcut.com:8443',
                   'https://user:private@commerce.us.capcut.com', 'https://commerce.us.capcut.com/',
                   'https://commerce.us.capcut.com/path', 'https://commerce.us.capcut.com?token=private',
                   'https://commerce.us.capcut.com#fragment', 'https://commerce.us.capcut.com.attacker.test',
                   'http://commerce.us.capcut.com', None, {}, ['https://commerce.us.capcut.com']]
        contexts = [{"sdk": {"code": "US", "domain": {"commerce_domain": value}}} for value in domains]
        contexts += [{"sdk": {"code": "JP", "domain": {"commerce_domain": 'https://commerce.us.capcut.com'}}},
                     {"sdk": {"code": "SG", "domain": {"commerce_domain": 'https://commerce.us.capcut.com'}}},
                     {"hostname": "attacker.test", "sdk": {"code": "US", "domain": {
                         "commerce_domain": 'https://commerce.us.capcut.com'}}}]
        for path in ["/commerce/v1/benefits/user_credit", "/commerce/v1/benefits/credit_receive", "/mweb/v1/get_settings"]:
            result = self.execute_contexts([self.expression(path)], contexts)
            self.assertEqual(len(result['calls']), len(contexts))
            for call in result['calls']:
                origin = 'https://mweb-api-sg.capcut.com' if path.startswith('/mweb/') else 'https://commerce-api-sg.capcut.com'
                self.assertEqual(call['url'], origin + path)
                self.assertEqual(call['options']['headers'], {'content-type': 'application/json'})

    def test_us_origin_getter_and_proxy_are_read_once_and_cannot_change_dispatch_origin(self):
        old, new, attacker = ('https://commerce-us-ttp2.us.capcut.com',
                              'https://commerce.us.capcut.com', 'https://arbitrary.test')
        contexts = [{'sdk': {'code': 'US'}, 'originAccess': access, 'originSequence': sequence}
                    for access, sequence in [('getter', [new, attacker]), ('proxy', [old, attacker]),
                                             ('getter', [attacker, new]), ('proxy', [attacker, old])]]
        for path in ['/commerce/v1/benefits/user_credit', '/commerce/v1/benefits/credit_receive']:
            with self.subTest(path=path):
                result = self.execute_contexts([self.expression(path)], contexts)
                self.assertEqual([call['url'] for call in result['calls']],
                                 [new + path, old + path, 'https://commerce-api-sg.capcut.com' + path,
                                  'https://commerce-api-sg.capcut.com' + path])
                self.assertEqual(result['reads'], [{'domain': 1, 'origin': 1}] * len(contexts))
                self.assertTrue(all('arbitrary.test' not in call['url'] for call in result['calls']))

    def test_new_us_commerce_context_is_not_cached_or_retried_in_another_region(self):
        contexts = [{"sdk": {"code": "US", "domain": {"commerce_domain": origin}}}
                    for origin in ['https://commerce.us.capcut.com', 'https://commerce-us-ttp2.us.capcut.com']]
        contexts += [{}, {"throwFetch": True, **contexts[0]}]
        result = self.execute_contexts([self.expression()], contexts)
        self.assertEqual([call['url'] for call in result['calls']], [
            'https://commerce.us.capcut.com/commerce/v1/benefits/user_credit',
            'https://commerce-us-ttp2.us.capcut.com/commerce/v1/benefits/user_credit',
            'https://commerce-api-sg.capcut.com/commerce/v1/benefits/user_credit',
            'https://commerce.us.capcut.com/commerce/v1/benefits/user_credit'])
        self.assertEqual(json.loads(result['values'][-1]), {'transport_error': 'fetch'})

    def test_new_commerce_domain_uses_original_verified_us_mweb_origin(self):
        context = {'sdk': {'code': 'US', 'domain': {'commerce_domain': 'https://commerce.us.capcut.com',
                                                  'web_domain': 'https://web-edit.us.capcut.com'}}}
        expression = self.expression('/mweb/v1/get_settings', query={'aid': '513695', 'region': 'SG'})
        call = self.execute_contexts([expression], [context])['calls'][0]
        self.assertEqual(call['url'], 'https://dreamina-api-us-ttp2.us.capcut.com/mweb/v1/get_settings?aid=513641&region=US')
        self.assertEqual(call['options']['headers'], {'content-type': 'application/json', 'appid': '513641'})

    def test_both_evidenced_us_contexts_keep_original_mweb_route_body_and_query_rules(self):
        body = {'synthetic_marker': ['unchanged', 1], 'nested': {'enabled': True}}
        contexts = [{'sdk': {'code': 'US', 'domain': {'commerce_domain': origin}}}
                    for origin in ['https://commerce-us-ttp2.us.capcut.com', 'https://commerce.us.capcut.com']]
        for path in ['/mweb/v1/get_settings', '/mweb/v1/get_upload_token', '/mweb/v1/get_history_by_ids']:
            with self.subTest(path=path):
                expression = self.expression(path, body=body, query={'aid': '513695', 'region': 'SG', 'keep': 'caller'})
                result = self.execute_contexts([expression], contexts)
                self.assertEqual(len(result['calls']), 2)
                for call in result['calls']:
                    url = urllib.parse.urlsplit(call['url'])
                    self.assertEqual(url.hostname, 'dreamina-api-us-ttp2.us.capcut.com')
                    self.assertEqual(url.path, path)
                    self.assertEqual(dict(urllib.parse.parse_qsl(url.query)), {'aid': '513641', 'region': 'US', 'keep': 'caller'})
                    self.assertEqual(call['options']['headers'], {'content-type': 'application/json', 'appid': '513641'})
                    self.assertEqual(json.loads(call['options']['body']), body)
                    self.assertEqual(call['options']['credentials'], 'include')
        call = self.execute_contexts([self.expression('/mweb/v1/get_settings', query={'keep': 'caller'})], [contexts[1]])['calls'][0]
        self.assertEqual(dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(call['url']).query)), {'keep': 'caller', 'aid': '513641'})

    def test_mweb_getter_and_proxy_use_one_verified_origin_snapshot(self):
        old, new, attacker = ('https://commerce-us-ttp2.us.capcut.com', 'https://commerce.us.capcut.com', 'https://arbitrary.test')
        contexts = [{'sdk': {'code': 'US'}, 'originAccess': access, 'originSequence': sequence}
                    for access, sequence in [('getter', [new, attacker]), ('proxy', [old, attacker]),
                                             ('getter', [attacker, new]), ('proxy', [attacker, old])]]
        expression = self.expression('/mweb/v1/get_settings', query={'aid': 'caller', 'region': 'SG', 'keep': 'unchanged'})
        result = self.execute_contexts([expression], contexts)
        self.assertEqual(result['reads'], [{'domain': 1, 'origin': 1}] * 4)
        for i, call in enumerate(result['calls']):
            url = urllib.parse.urlsplit(call['url'])
            self.assertEqual(url.hostname, 'dreamina-api-us-ttp2.us.capcut.com' if i < 2 else 'mweb-api-sg.capcut.com')
            self.assertEqual(dict(urllib.parse.parse_qsl(url.query)), {
                'aid': '513641' if i < 2 else 'caller', 'region': 'US' if i < 2 else 'SG', 'keep': 'unchanged'})
            self.assertNotIn('arbitrary.test', call['url'])

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
        new_us = {'sdk': {'code': 'US', 'domain': {'commerce_domain': 'https://commerce.us.capcut.com'}}}
        result = self.execute_contexts([expression], [us, new_us, {}, {**new_us, "throwFetch": True}])
        self.assertEqual(len(result["calls"]), 4)
        self.assertEqual([urllib.parse.urlsplit(c["url"]).hostname for c in result["calls"]],
                         ["dreamina-api-us-ttp2.us.capcut.com", "dreamina-api-us-ttp2.us.capcut.com", "mweb-api-sg.capcut.com",
                          "dreamina-api-us-ttp2.us.capcut.com"])
        self.assertEqual(json.loads(result["values"][-1]), {"transport_error": "fetch"})

    def test_daily_claim_uses_verified_region_once_without_altering_body(self):
        us = {"sdk": {"code": "US", "domain": {
            "commerce_domain": "https://commerce-us-ttp2.us.capcut.com"}}}
        jp = {"sdk": {"code": "JP", "domain": {
            "commerce_domain": "https://commerce-api-sg.capcut.com"}}}
        client = DreaminaClient({"id": 1, "cdp_port": 19301,
                                 "proxy_url": "socks5://proxy.test:20002"})
        with patch.object(client.browser, "evaluate", return_value=json.dumps(
                {"http": 200, "body": {"ret": "0", "data": {"receive_quota": 5}}})) as evaluate:
            self.assertEqual(client.daily_checkin(), {"receive_quota": 5})
        expression = evaluate.call_args.args[0]
        result = self.execute_contexts([expression], [us, jp, {**us, "throwFetch": True}])
        self.assertEqual(len(result['calls']), 3)
        self.assertEqual([call['url'] for call in result['calls']], [
            "https://commerce-us-ttp2.us.capcut.com/commerce/v1/benefits/credit_receive",
            "https://commerce-api-sg.capcut.com/commerce/v1/benefits/credit_receive",
            "https://commerce-us-ttp2.us.capcut.com/commerce/v1/benefits/credit_receive"])
        for call in result['calls']:
            self.assertEqual(json.loads(call['options']['body']), {"time_zone": "Asia/Taipei"})
            self.assertEqual(call['options']['credentials'], 'include')
        self.assertEqual(result['calls'][0]['options']['headers']['appid'], '513641')
        self.assertNotIn('appid', result['calls'][1]['options']['headers'])
        self.assertEqual(json.loads(result['values'][-1]), {'transport_error': 'fetch'})

    def test_unrelated_commerce_write_and_native_paths_keep_existing_transport(self):
        us = {"sdk": {"code": "US", "domain": {
            "commerce_domain": "https://commerce-us-ttp2.us.capcut.com"}}}
        paths = ["/commerce/v1/benefits/team_credit_allocate"]
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
