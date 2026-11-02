import json
import shutil
import subprocess
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from app.browser import BrowserError, BrowserRejected
from app.jimeng_client import (
    JimengWebClient,
    JimengReceiptMismatch,
    PreparedJimengSubmission,
    _native_expression,
    _native_response,
    _quote_expression,
    build_mini_text_submission,
    build_video_submission,
    parse_credit_state,
    parse_login_identity,
    parse_history_response,
    parse_submit_receipt,
)


class JimengIdentityAndCreditTests(unittest.TestCase):
    def test_profile_lookup_must_match_authenticated_self_configuration(self):
        config = {"sec_uid": "synthetic-original-account"}
        own = {"uid": "1234567890123456", "sec_uid": config["sec_uid"], "status": 0}
        self.assertEqual(parse_login_identity(config, own), {
            "authenticated": True, "user_id": own["uid"], "email": ""})
        foreign = {**own, "sec_uid": "synthetic-different-account"}
        with self.assertRaisesRegex(BrowserError, "identity could not be verified"):
            parse_login_identity(config, foreign)
        for invalid in ({}, {**own, "status": 1}, {**own, "uid": "0"}):
            with self.subTest(invalid=invalid):
                with self.assertRaises(BrowserError):
                    parse_login_identity(config, invalid)

    def test_invalid_or_missing_balance_is_not_admitted_as_zero(self):
        buckets = {"vip_credit": 10, "gift_credit": "2.5", "purchase_credit": 0}
        self.assertEqual(parse_credit_state({"credit": buckets})["available_balance"], 12.5)
        for invalid in ({}, {"credit": {}}, {"credit": {**buckets, "gift_credit": -1}},
                        {"credit": {**buckets, "vip_credit": float("nan")}},
                        {"credit": {**buckets, "purchase_credit": True}}):
            with self.subTest(invalid=invalid):
                with self.assertRaises(BrowserError):
                    parse_credit_state(invalid)


class JimengPreparedSubmissionTests(unittest.TestCase):
    def test_mixed_reference_metadata_keeps_each_binding_and_uses_milliseconds(self):
        request = {"model": "sd-2-0-mini", "duration": 4, "resolution": "720p", "aspect_ratio": "16:9",
                   "prompt": "@图片3 与 @图片5，参考 @图片1 @图片2 @音频1 @视频1，重复 @音频1 @视频1。",
                   "images": [f"https://synthetic.example.invalid/{i}.png" for i in range(5)],
                   "videos": ["https://synthetic.example.invalid/video.mp4"],
                   "audios": ["https://synthetic.example.invalid/audio.mp3"]}
        assets = {"images": [{"image_uri": f"synthetic-image-{i}", "width": size, "height": size}
                              for i, size in enumerate([2048, 2048, 800, 960, 1065])],
                  "videos": [{"vid": "synthetic-video", "duration": 2.02,
                              "width": 1280, "height": 720, "fps": 0}],
                  "audios": [{"vid": "synthetic-audio", "duration": 5.146}]}
        prepared = build_video_submission(request, workspace_id=12345678901234, assets=assets,
            commerce={"resource_id": "generate_video", "resource_id_type": "str", "resource_sub_type": "aigc",
                      "benefit_type": "seedance_20_mini_720p_output_5s", "amount": 6}, quoted_credits=36)
        draft = json.loads(prepared.body["draft_content"])
        inputs = draft["component_list"][0]["abilities"]["gen_video"]["text_to_video_params"]["video_gen_inputs"][0]
        unified = inputs["unified_edit_input"]
        self.assertEqual(draft["min_version"], "3.3.9")
        self.assertEqual(draft["min_features"], ["AIGC_Video_UnifiedEdit"])
        self.assertEqual(inputs["prompt"], "")
        self.assertEqual([item["material_type"] for item in unified["material_list"]],
                         ["image"] * 5 + ["video", "audio"])
        self.assertNotIn("aigc_image", unified["material_list"][0]["image_info"])
        self.assertEqual(unified["material_list"][5]["video_info"]["duration"], 2020)
        self.assertEqual(unified["material_list"][6]["audio_info"]["duration"], 5146)
        bound = [(meta["meta_type"], meta["material_ref"]["material_idx"])
                 for meta in unified["meta_list"] if meta.get("material_ref")]
        self.assertEqual(bound, [("image", 2), ("image", 4), ("image", 0), ("image", 1),
                                 ("audio", 6), ("video", 5), ("audio", 6), ("video", 5)])
        quote_bound = prepared.receipt_context(1)["quote_bound"]
        self.assertEqual(quote_bound["input_video_duration_seconds"], 2.02)
        self.assertEqual(quote_bound["material_types"], ["image"] * 5 + ["video", "audio"])

    def test_regional_model_routes_and_versions_match_all_official_serializer_cases(self):
        fixture = json.loads((Path(__file__).parent / "fixtures" /
                              "jimeng_video_draft_matrix.safe.json").read_text(encoding="utf-8"))
        aliases = {
            ("dreamina_seedance_40_mini", "720p"): "sd-2-0-mini",
            ("dreamina_seedance_40_vision", "720p"): "sd-2-0-fast",
            ("dreamina_seedance_40_pro_vision", "720p"): "sd-2-0",
            ("dreamina_seedance_40_pro_vision", "1080p"): "sd-2-0-1080p",
            ("dreamina_seedance_40_pro_vision", "4k"): "sd-2-0-4k",
            ("dreamina_seedance_45_pro", "480p"): "sd-2-5-480p",
            ("dreamina_seedance_45_pro", "720p"): "sd-2-5",
            ("dreamina_seedance_45_pro", "1080p"): "sd-2-5-1080p",
        }
        self.assertEqual(len(fixture["cases"]), 24)
        for case in fixture["cases"]:
            with self.subTest(case=case):
                request = {"model": aliases[case["model"], case["resolution"]],
                           "duration": case["duration"], "resolution": case["resolution"],
                           "aspect_ratio": "16:9", "prompt": "Synthetic serializer fixture."}
                prepared = build_video_submission(request, workspace_id=12345678901234,
                    commerce={"resource_id": "generate_video", "resource_id_type": "str",
                              "resource_sub_type": "aigc", "benefit_type": "synthetic_verified_key",
                              "amount": case["duration"]})
                draft = json.loads(prepared.body["draft_content"])
                params = draft["component_list"][0]["abilities"]["gen_video"]["text_to_video_params"]
                self.assertEqual(params["model_req_key"], case["model"])
                self.assertEqual(draft["min_version"], case["draft_min_version"])
                self.assertEqual(draft["version"], case["draft_version"])
                self.assertEqual(draft["min_features"], case["min_features"])
                self.assertEqual(params["video_gen_inputs"][0]["min_version"], case["input_min_version"])
                self.assertEqual(params["video_gen_inputs"][0]["duration_ms"], case["duration"] * 1000)

    def _client_and_quote(self):
        with patch("app.jimeng_client.CDPBrowser"):
            client = JimengWebClient({"id": 1, "cdp_port": 19301})
        quote = {"input": {"model": "dreamina_seedance_40_mini", "duration": 4,
                           "resolution": "720p", "refs": [], "inputVideoDuration": 0},
                 "commerceInfos": [{"benefitType": "seedance_20_mini_720p_output_5s",
                                    "resourceId": "generate_video", "resourceIdType": "str",
                                    "resourceSubType": "aigc", "amount": 4}],
                 "quotedCredits": 24, "chargeInputVideoDuration": True,
                 "useSeedanceFast5sFreeTrial": False}
        request = {"model": "sd-2-0-mini", "duration": 4, "resolution": "720p",
                   "aspect_ratio": "16:9", "prompt": "A synthetic red ball rolls."}
        return client, quote, request

    def test_preparation_uses_current_native_quote_instead_of_commercial_amount(self):
        client, quote, request = self._client_and_quote()
        with patch.object(client, "workspace_id", return_value=12345678901234):
            client.browser.evaluate.return_value = json.dumps(quote)
            prepared = client.prepare_submit(request, {"images": [], "videos": [], "audios": []})
            self.assertEqual(prepared.quoted_credits, 24)
            context = prepared.receipt_context(1)
            self.assertEqual(context["quoted_credits"], 24)
            self.assertEqual(context["quote_source"], "native_get_need_credits")
            self.assertEqual(context["quote_bound"]["duration_seconds"], 4)
            self.assertEqual(prepared.body["extend"]["m_video_commerce_info"]["amount"], 4)
            quote["quotedCredits"] = 0
            client.browser.evaluate.return_value = json.dumps(quote)
            self.assertEqual(client.prepare_submit(request, {}).quoted_credits, 0)
            del quote["quotedCredits"]
            client.browser.evaluate.return_value = json.dumps(quote)
            with self.assertRaisesRegex(BrowserError, "credit quote is missing"):
                client.prepare_submit(request, {})

    def test_lost_receipt_or_mutated_prepared_body_cannot_trigger_a_second_write(self):
        client, quote, request = self._client_and_quote()
        with patch.object(client, "workspace_id", return_value=12345678901234):
            client.browser.evaluate.return_value = json.dumps(quote)
            prepared = client.prepare_submit(request, {})
        with patch.object(client, "_post", side_effect=BrowserError("synthetic lost receipt")) as post:
            with self.assertRaises(BrowserError):
                client.submit_prepared(prepared)
            with self.assertRaisesRegex(BrowserError, "already dispatched"):
                client.submit_prepared(prepared)
            self.assertEqual(post.call_count, 1)
            self.assertTrue(post.call_args.kwargs["one_shot"])
        prepared.body["submit_id"] = str(uuid.uuid4())
        with patch.object(client, "_post") as post:
            with self.assertRaisesRegex(BrowserError, "prepared generation or quote is invalid"):
                client.submit_prepared(prepared)
            post.assert_not_called()

    def test_captured_mini_draft_keeps_seconds_milliseconds_and_ids_distinct(self):
        request = {"model": "sd-2-0-mini", "duration": 4, "resolution": "720p",
                   "aspect_ratio": "16:9", "prompt": "A synthetic red ball rolls."}
        commerce = {"resource_id": "generate_video", "resource_id_type": "str",
                    "resource_sub_type": "aigc", "benefit_type": "seedance_20_mini_720p_output",
                    "amount": 4}
        prepared = build_mini_text_submission(request, workspace_id=12345678901234, commerce=commerce)
        draft = json.loads(prepared.body["draft_content"])
        component = draft["component_list"][0]
        inputs = component["abilities"]["gen_video"]["text_to_video_params"]["video_gen_inputs"]
        scene = json.loads(json.loads(prepared.body["metrics_extra"])["sceneOptions"])[0]
        self.assertEqual(draft["min_version"], "3.0.5")
        self.assertEqual(draft["version"], "3.3.36")
        self.assertNotIn("gen_type", component)
        self.assertEqual(inputs[0]["duration_ms"], 4000)
        self.assertEqual(scene["videoDuration"], 4)
        self.assertEqual(prepared.body["submit_id"], prepared.submit_id)
        self.assertEqual(prepared.query["generate_id"], prepared.trace_id)
        self.assertNotEqual(prepared.trace_id, prepared.submit_id)
        self.assertEqual(prepared.body["extend"]["m_video_commerce_info"], commerce)
        self.assertEqual(prepared.body["http_common_info"], {"aid": 513695})
        with self.assertRaisesRegex(BrowserError, "verified commercial amount"):
            build_mini_text_submission(request, workspace_id=12345678901234,
                                       commerce={**commerce, "amount": 0})

    def test_durable_identity_survives_request_mutation_without_saving_content(self):
        submit_id = str(uuid.uuid4())
        body = {"submit_id": submit_id, "draft_content": "synthetic private prompt"}
        trace_id = "gen-" + str(uuid.uuid4())
        prepared = PreparedJimengSubmission(submit_id, "video", body,
                                             {"generate_id": trace_id})
        body["submit_id"] = str(uuid.uuid4())
        body["draft_content"] = "changed prompt"
        self.assertEqual(prepared.body["submit_id"], submit_id)
        context = prepared.receipt_context(7, phase="dispatching")
        self.assertEqual(context["submit_id"], submit_id)
        self.assertEqual(context["trace_id"], trace_id)
        self.assertNotEqual(context["trace_id"], submit_id)
        self.assertEqual(context["submission_phase"], "dispatching")
        self.assertNotIn("prompt", json.dumps(context))
        self.assertNotIn("body", context)
        self.assertNotIn("query", context)
        self.assertNotIn("private", repr(prepared))

    def test_query_cannot_change_the_original_trace_id(self):
        submit_id = str(uuid.uuid4())
        with self.assertRaisesRegex(ValueError, "original trace ID"):
            PreparedJimengSubmission(submit_id, "image", {"submit_id": submit_id},
                                      {"generate_id": "gen-" + str(uuid.uuid4())},
                                      trace_id="gen-" + str(uuid.uuid4()))

    def test_only_the_body_submission_receipt_can_bind_the_task(self):
        submit_id = str(uuid.uuid4())
        data = {"aigc_data": {"history_record_id": "9876543210987654321",
                             "task": {"submit_id": submit_id, "task_id": "synthetic-task"}}}
        receipt = parse_submit_receipt(data, submit_id)
        self.assertEqual(receipt["run_id"], submit_id)
        self.assertEqual(receipt["thread_id"], "9876543210987654321")
        data["aigc_data"]["task"]["submit_id"] = str(uuid.uuid4())
        with self.assertRaises(JimengReceiptMismatch):
            parse_submit_receipt(data, submit_id)


class JimengReadTransportTests(unittest.TestCase):
    def test_unsupported_modes_and_cn_total_reference_limits_reject_before_uploads(self):
        with patch("app.jimeng_client.CDPBrowser"):
            client = JimengWebClient({"id": 1, "cdp_port": 19301})
        base = {"model": "sd-2-0-mini", "duration": 4, "resolution": "720p", "aspect_ratio": "16:9",
                "prompt": "A synthetic fixture.", "images": [], "videos": [], "audios": []}
        cases = [{**base, "model": "sd-2-5-480p-preview", "resolution": "480p"},
                 {**base, "aspect_ratio": "9:21"},
                 {**base, "images": ["https://synthetic.example.invalid/image.png"] * 9,
                  "videos": ["https://synthetic.example.invalid/video.mp4"] * 3,
                  "audios": ["https://synthetic.example.invalid/audio.mp3"]}]
        with patch("app.jimeng_upload.JimengUploader") as uploader:
            for request in cases:
                with self.subTest(request=request):
                    with self.assertRaises((BrowserError, ValueError)):
                        client.upload_inputs(request)
            uploader.assert_not_called()

    def test_public_profile_response_cannot_bind_an_account_after_session_switch(self):
        account = {"id": 1, "cdp_port": 19301}
        with patch("app.jimeng_client.CDPBrowser"):
            client = JimengWebClient(account)
        config = {"sec_uid": "synthetic-original-sec-id"}
        profile = {"uid": "original-account", "sec_uid": config["sec_uid"], "status": 0}
        client.browser.evaluate.return_value = {"user_id": "different-current-account"}
        with patch.object(client, "_post", side_effect=[config, profile]):
            with self.assertRaisesRegex(JimengReceiptMismatch, "account changed"):
                client.login_identity()
        self.assertNotIn("login_user_id", client.account)
        client.browser.evaluate.return_value = {"user_id": profile["uid"]}
        with patch.object(client, "_post", side_effect=[config, profile]):
            self.assertTrue(client.login_identity()["authenticated"])
        self.assertEqual(client.account["login_user_id"], profile["uid"])
        self.assertNotIn("login_user_id", account)
    def test_missing_or_foreign_history_cannot_complete_an_unknown_submission(self):
        submit_id = str(uuid.uuid4())
        missing = parse_history_response({}, submit_id)
        self.assertFalse(missing["record_found"])
        self.assertEqual(missing["status"], "running")
        foreign = {submit_id: {"task": {"submit_id": str(uuid.uuid4()), "status": 50}}}
        with self.assertRaises(JimengReceiptMismatch):
            parse_history_response(foreign, submit_id)

    def test_cn_failure_enum_and_settled_task_debit_are_distinct_from_balances(self):
        submit_id = str(uuid.uuid4())
        record = {"task": {"submit_id": submit_id, "status": 30}, "item_list": [], "uid": "self"}
        failed = parse_history_response({submit_id: record}, submit_id, expected_user_id="self")
        self.assertEqual(failed["status"], "failed")
        record["task"]["status"] = 50
        record["item_list"] = [{"video": {"transcoded_video": {"origin": {
            "video_url": "https://synthetic.example.invalid/result.mp4"}}},
            "extra": {"credits_consume": "0"}}]
        result = parse_history_response({submit_id: record}, submit_id, expected_user_id="self")
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["task_credit_evidence"]["amount"], 0)
        self.assertEqual(result["task_credit_evidence"]["submit_id"], submit_id)
        with self.assertRaises(JimengReceiptMismatch):
            parse_history_response({submit_id: record}, submit_id, expected_user_id="different")

    def test_poll_queries_only_body_uuid_and_rejects_trace_id(self):
        submit_id = str(uuid.uuid4())
        with patch("app.jimeng_client.CDPBrowser"):
            client = JimengWebClient({"id": 1, "cdp_port": 19301})
        with patch.object(client, "_post", return_value={}) as post:
            self.assertFalse(client.poll(submit_id)["record_found"])
            self.assertEqual(post.call_args.args[1], {"submit_ids": [submit_id]})
            with self.assertRaises(ValueError):
                client.poll("gen-" + str(uuid.uuid4()))
            self.assertEqual(post.call_count, 1)

    def test_only_credit_reads_retry_and_restore_original_timeout(self):
        with patch("app.jimeng_client.CDPBrowser") as browser:
            browser.return_value.timeout = 90
            client = JimengWebClient({"id": 1, "cdp_port": 19301})
        buckets = {"vip_credit": 10, "gift_credit": 2, "purchase_credit": 0}
        with patch.object(client, "_post", side_effect=[BrowserError("synthetic disconnect"),
                           {"credit": buckets}]) as post, patch("app.jimeng_client.time.sleep"):
            self.assertEqual(client.account_state(timeout_seconds=4)["available_balance"], 12)
            self.assertEqual(post.call_count, 2)
        self.assertEqual(client.browser.timeout, 90)
        with patch.object(client, "_post", side_effect=BrowserRejected("synthetic rejected")) as post:
            with self.assertRaises(BrowserRejected):
                client.account_state()
        self.assertEqual(post.call_count, 1)

    def test_native_success_can_decode_observed_commerce_response_string(self):
        data = {"credit": {"vip_credit": 1, "gift_credit": 2, "purchase_credit": 3}}
        value = {"http": 200, "body": {"ret": "0", "response": json.dumps(data)}}
        self.assertEqual(_native_response(json.dumps(value)), data)
        for response in ({"transport_error": "native-client"}, {"http": 200, "body": {}},
                         {"http": 200, "body": {"ret": "0", "data": []}}):
            with self.subTest(response=response):
                with self.assertRaises(BrowserError):
                    _native_response(json.dumps(response))

    def test_transport_does_not_allow_a_region_change(self):
        with self.assertRaisesRegex(ValueError, "original CN account region"):
            _native_expression("/mweb/v1/get_common_config", {}, {"region": "SG"})


class JimengWorkspaceTests(unittest.TestCase):
    class AccountStore:
        def __init__(self):
            self.bound = ""

        def account(self, account_id, **kwargs):
            return {"project_id": self.bound}

        def claim_jimeng_workspace_creation(self, account_id, marker):
            if self.bound:
                return False
            self.bound = marker
            return True

        def bind_jimeng_workspace(self, account_id, marker, workspace_id):
            if self.bound != marker:
                return False
            self.bound = str(workspace_id)
            return True

    def client(self, store, selected=""):
        with patch("app.jimeng_client.CDPBrowser") as browser:
            browser.return_value.evaluate.return_value = selected
            return JimengWebClient({"id": 7, "cdp_port": 19307}, store=store)

    def test_new_workspace_is_created_once_and_verified_against_the_own_list(self):
        store = self.AccountStore()
        client = self.client(store)
        calls = []

        def post(path, body, **kwargs):
            calls.append((path, body, kwargs))
            if path.endswith("/create"):
                self.assertTrue(store.bound.startswith("jimeng-create-pending:"))
                self.assertTrue(kwargs["one_shot"])
                return {"workspace_id": 12345678901234}
            return {"workspaces": [{"workspace_id": "12345678901234"}], "has_more": False}

        with patch.object(client, "_post", side_effect=post):
            self.assertEqual(client.workspace_id(), 12345678901234)
            self.assertEqual(client.workspace_id(), 12345678901234)
        self.assertEqual(sum(path.endswith("/create") for path, _, _ in calls), 1)
        self.assertEqual(store.bound, "12345678901234")

    def test_lost_create_response_keeps_durable_marker_and_never_creates_again(self):
        store = self.AccountStore()
        client = self.client(store)
        with patch.object(client, "_post", side_effect=BrowserError("synthetic lost create receipt")) as post:
            with self.assertRaises(BrowserError):
                client.workspace_id()
            self.assertEqual(post.call_count, 1)
        original_marker = store.bound
        restarted = self.client(store)
        with patch.object(restarted, "_post") as post:
            with self.assertRaises(BrowserError):
                restarted.workspace_id()
            post.assert_not_called()
        self.assertEqual(store.bound, original_marker)

    def test_current_url_cannot_bind_another_accounts_workspace(self):
        store = self.AccountStore()
        client = self.client(store, selected="12345678901234")
        with patch.object(client, "_post", return_value={
                "workspaces": [{"workspace_id": "98765432109876"}], "has_more": False}) as post:
            with self.assertRaisesRegex(BrowserError, "does not belong"):
                client.workspace_id()
            self.assertTrue(all(call.args[0].endswith("/list") for call in post.call_args_list))
        self.assertEqual(store.bound, "")


@unittest.skipUnless(shutil.which("node"), "Node.js is required for page-context execution")
class JimengNativePageTests(unittest.TestCase):
    def evaluate(self, *, origin="https://jimeng.jianying.com", missing_module=False,
                 missing_container=False, one_shot=False,
                 path="/mweb/v1/get_common_config", expected_user_id="", profile_uid="original-account"):
        program = r"""
const vm=require('node:vm'),input=JSON.parse(require('node:fs').readFileSync(0,'utf8'));
let retryPolicy;
const calls=[],interceptors=[],ctx={location:{origin:input.origin},
crypto:{randomUUID:()=> 'synthetic-random-chunk'}};
const client={post:async(...args)=>{calls.push(args);return {ret:'0',data:{credit:{vip_credit:1}}}}};
const modules={hw:{LL:class{build(){return client}}},cBI:{U:()=>interceptors.push('cBI')},
dR:{vo:()=>interceptors.push('dR')},qN:{Wh:()=>interceptors.push('qN')},
yF:{P:(_,options)=>{interceptors.push('yF');retryPolicy=options.isRetry}},hI:{m:()=>interceptors.push('hI')}};
modules.M8X={cQ:()=>({userProfile:{uid:input.profileUid,webId:'synthetic-web-id'}})};
modules.bE={c:'synthetic-account-token'};
if(input.missingModule)modules.dR={};
const req=name=>modules[name],container={createInstance(){},invokeFunction(){}};
ctx.rspackChunkdreamina={push:entry=>entry[2](req)};
ctx.window=ctx;
ctx.document={querySelectorAll:()=>input.missingContainer?[]:[{
'__reactFiber$synthetic':{memoizedProps:{value:container},return:null}}]};
ctx.fetch=()=>{throw Error('unexpected unsigned fallback')};
(async()=>{const result=await vm.runInNewContext(input.expression,ctx);
process.stdout.write(JSON.stringify({result:JSON.parse(result),calls,interceptors,
retryAllowed:retryPolicy?retryPolicy({config:calls[0]?.[2]}):null}));})();
"""
        expression = _native_expression(path, {
            "prompt": "literal __OPTIONS__ and __ORIGIN__ and ` ${never_execute}"},
            one_shot=one_shot, expected_user_id=expected_user_id)
        result = subprocess.run([shutil.which("node"), "-e", program],
                                input=json.dumps({"expression": expression, "origin": origin,
                                                  "missingModule": missing_module,
                                                  "missingContainer": missing_container,
                                                  "profileUid": profile_uid}),
                                text=True, capture_output=True, check=True, timeout=10)
        return json.loads(result.stdout)

    def test_cold_page_uses_all_genuine_interceptors_and_keeps_user_text_literal(self):
        result = self.evaluate()
        self.assertEqual(result["interceptors"], ["cBI", "dR", "qN", "yF", "hI"])
        self.assertEqual(len(result["calls"]), 1)
        url, body, options = result["calls"][0]
        self.assertEqual(url, "https://jimeng.jianying.com/mweb/v1/get_common_config")
        self.assertEqual(body["prompt"], "literal __OPTIONS__ and __ORIGIN__ and ` ${never_execute}")
        self.assertEqual(options["params"]["region"], "CN")

    def test_paid_write_disables_official_client_automatic_retry(self):
        result = self.evaluate(one_shot=True)
        self.assertFalse(result["retryAllowed"])
        self.assertEqual(result["calls"][0][2]["networkCtx"], {"reconcileBySubmitId": True})
        self.assertEqual(len(result["calls"]), 1)

    def test_foreign_page_or_missing_native_components_cannot_send_any_request(self):
        for config in ({"origin": "https://dreamina.capcut.com"}, {"missing_module": True},
                       {"missing_container": True}):
            with self.subTest(config=config):
                result = self.evaluate(**config)
                self.assertTrue(result["result"]["transport_error"])
                self.assertEqual(result["calls"], [])

    def test_every_bound_read_or_upload_rejects_a_switched_page_identity(self):
        for path in ("/mweb/v1/get_upload_token", "/commerce/v1/benefits/user_credit",
                     "/mweb/v1/creation_agent/v2/get_agent_config", "/mweb/v1/workspace/list",
                     "/mweb/v1/workspace/create"):
            with self.subTest(path=path):
                result = self.evaluate(path=path, expected_user_id="original-account",
                                       profile_uid="different-account")
                self.assertEqual(result["calls"], [])
                self.assertEqual(result["result"]["transport_error"], "account-context-unavailable")
        for path in ("/mweb/v1/get_upload_token", "/mweb/v1/workspace/list", "/mweb/v1/workspace/create"):
            with self.subTest(path=path):
                result = self.evaluate(path=path, expected_user_id="original-account")
                self.assertNotIn("webId", result["calls"][0][2]["params"])

    def test_quote_refresh_finishes_before_pricing_and_rejects_a_mid_refresh_switch(self):
        program = r"""
const vm=require('node:vm'),input=JSON.parse(require('node:fs').readFileSync(0,'utf8'));
let sceneMaterialTypes;
const calls=[],profile={uid:'original-account',webId:'synthetic-web-id'};
const strategy={isReady:true,forceUpdateBenefitStrategy:async()=>{calls.push('refresh');
if(input.switchAccount)profile.uid='different-account';return {ok:true}}};
const feature={commercialStrategyService:strategy,getNeedCredits:()=>{
calls.push('quote');return {credits:24}}};
const models={getVideoModelConfig:()=>({commercialConfig:{}})};
const services={account:{userProfile:profile},commercial:{getInstanceSync:()=>feature},
model:{getInstanceSync:()=>models}};
const container={invokeFunction(){},createInstance:()=>({getCommerceInfos:async()=>{
calls.push('commerce');return [{benefitType:'seedance_20_mini_720p_output_5s',
resourceId:'generate_video',resourceIdType:'str',resourceSubType:'aigc',amount:4}]}})};
const modules={M8X:{cQ:(_,token)=>{if(!(token in services))throw Error('unknown service');return services[token]}},
bE:{c:'account'},yB:{X:'commercial'},nss:{Y:'model'},
Gr4:{I:x=>{calls.push('scene');sceneMaterialTypes=x.materialTypes;return x}},
gu:{Sk:{BasicVideoGenerateButton:'BasicVideoGenerateButton'}},
L6W:{Ge:()=>true},LD:{v:()=>false},FI:{E:class{}},al:{E:()=>true}};
const ctx={location:{origin:'https://jimeng.jianying.com'},__ijm_cn_require:name=>modules[name],
document:{querySelectorAll:()=>[{'__reactFiber$synthetic':{memoizedProps:{value:container},return:null}}]}};
ctx.window=ctx;ctx.fetch=()=>{throw Error('unexpected transport')};
(async()=>{const result=await vm.runInNewContext(input.expression,ctx);
process.stdout.write(JSON.stringify({result:JSON.parse(result),calls,sceneMaterialTypes}));})();
"""
        expression = _quote_expression({"model": "dreamina_seedance_40_mini", "duration": 4,
                                        "resolution": "720p", "refs": ["image", "audio", "video"],
                                        "inputVideoDuration": 2.02},
                                       "original-account")
        for switch in (False, True):
            result = subprocess.run([shutil.which("node"), "-e", program],
                input=json.dumps({"expression": expression, "switchAccount": switch}),
                text=True, capture_output=True, check=True, timeout=10)
            actual = json.loads(result.stdout)
            if switch:
                self.assertEqual(actual["result"]["error"], "benefit-refresh-unverified")
                self.assertEqual(actual["calls"], ["refresh"])
            else:
                self.assertEqual(actual["calls"], ["refresh", "scene", "commerce", "quote"])
                self.assertEqual(actual["sceneMaterialTypes"], [1, 3, 2])
                self.assertEqual(actual["result"]["quotedCredits"], 24)
