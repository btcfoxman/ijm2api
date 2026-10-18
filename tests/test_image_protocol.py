import json
import unittest
from unittest.mock import patch

from app.image_client import (
    IMAGE_RATIOS, DreaminaImageClient, build_image_submit_body,
    image_result_urls, resolve_image_model,
)
from app.image_request import normalize_image_request
from app.errors import public_error


class ImageProtocolTests(unittest.TestCase):
    def test_all_observed_page_ratios_have_exact_codes_and_1k_dimensions(self):
        self.assertEqual(IMAGE_RATIOS, {
            "1:1": (1, 1024, 1024), "3:4": (2, 768, 1024),
            "16:9": (3, 1024, 576), "4:3": (4, 1024, 768),
            "9:16": (5, 576, 1024), "2:3": (6, 682, 1024),
            "3:2": (7, 1024, 682), "21:9": (8, 1195, 512),
        })
        for ratio, (code, width, height) in IMAGE_RATIOS.items():
            with self.subTest(ratio=ratio):
                normalized = normalize_image_request(
                    {"model": "gpt-image-2.5-flare", "prompt": "a sphere",
                     "tools": [{"type": "image_generation", "ratio": ratio}]},
                    resolve_image_model("gpt-image-2.5-flare"))
                body, _ = build_image_submit_body(normalized, [])
                draft = json.loads(body["draft_content"])
                core = draft["component_list"][0]["abilities"]["generate"]["core_param"]
                self.assertEqual(core["image_ratio"], code)
                self.assertEqual(core["large_image_info"]["width"], width)
                self.assertEqual(core["large_image_info"]["height"], height)

    def test_text_and_edit_drafts_follow_captured_page_fields(self):
        source = {"model": "gpt-image-2.5-sunburst-2k", "prompt": "a sphere",
                  "input": [{"role": "user", "content": [
                      {"type": "input_text", "text": "a sphere"},
                      {"type": "input_image", "image_url": "https://example.com/a.png"}]}],
                  "tools": [{"type": "image_generation", "ratio": "21:9"}]}
        normalized = normalize_image_request(source, resolve_image_model(source["model"]))
        self.assertEqual(normalized["resolution"], "2k")
        body, _ = build_image_submit_body(normalized, [{"image_uri": "uploaded-image"}], 42)
        component = json.loads(body["draft_content"])["component_list"][0]
        self.assertEqual(json.loads(body["draft_content"])["version"], "3.3.30")
        self.assertEqual(component["generate_type"], "blend")
        self.assertEqual(component["abilities"]["blend"]["core_param"]["large_image_info"]["width"], 2390)
        self.assertEqual(component["abilities"]["blend"]["ability_list"][0]["image_uri_list"], ["uploaded-image"])
        self.assertEqual(body["extend"]["workspace_id"], 42)
        self.assertEqual(component["abilities"]["gen_option"]["gen_count"], 1)

    def test_poll_succeeded_and_failed_history(self):
        client = DreaminaImageClient(12345)
        completed = {"task": {"status": 50}, "item_list": [
            {"image": {"large_images": [{"image_url": "https://example.com/result.png"}]}}]}
        failed = {"task": {"status": 30}, "fail_msg": "upstream rejected", "item_list": []}
        with patch.object(client.browser, "post", side_effect=[{"one": completed}, {"two": failed}]):
            self.assertEqual(client.poll("one")["result_urls"], ["https://example.com/result.png"])
            self.assertEqual(client.poll("two")["status"], "failed")
        self.assertEqual(image_result_urls(completed), ["https://example.com/result.png"])

    def test_image_submit_uses_page_request_client_and_upstream_query(self):
        source = {"model": "gpt-image-2.5-flare", "prompt": "a sphere",
                  "ratio": "3:2"}
        normalized = normalize_image_request(source, resolve_image_model(source["model"]))
        client = DreaminaImageClient(12345)
        with patch.object(client.browser, "post", side_effect=[
                {"workspace_id": 42},
                {"aigc_data": {"task": {"submit_id": "upstream-1"}}},
        ]) as post:
            self.assertEqual(client.submit(normalized, [])["run_id"], "upstream-1")
        generate = post.call_args_list[-1]
        self.assertEqual(generate.args[0], "/mweb/v1/aigc_draft/generate")
        self.assertTrue(generate.kwargs["native_client"])
        self.assertEqual(generate.kwargs["query"]["da_version"], "3.1.3")
        self.assertEqual(generate.kwargs["query"]["commerce_with_input_video"], "1")

    def test_video_reference_image_size_failure_is_normalized(self):
        failure = public_error("UPSTREAM_FAILED",
            "The image width or height is too large or too small for this model. Try a different size.")
        self.assertEqual(failure["code"], "INPUT_IMAGE_SIZE_UNSUPPORTED")
        self.assertEqual(failure["message"],
                         "参考图片尺寸不符合模型要求，请调整图片尺寸后重试~")


if __name__ == "__main__":
    unittest.main()
