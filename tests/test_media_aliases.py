"""Natural Chinese labels must bind the actual uploaded material indices."""
import json
import unittest

from app.catalog import normalize_request, prompt_references
from app.client import build_materials, build_meta_list, build_submit_body


class MediaAliasTests(unittest.TestCase):
    def normalized_and_nodes(self, prompt, *, images=2, videos=0, audios=0, strict=True):
        request = {"model": "sd-2-5", "duration": 4, "prompt": prompt,
                   "image_urls": [f"https://example.test/{i}.png" for i in range(images)],
                   "video_urls": [f"https://example.test/{i}.mp4" for i in range(videos)],
                   "audio_urls": [f"https://example.test/{i}.mp3" for i in range(audios)]}
        normalized = normalize_request(request, strict_media_references=strict)
        assets = {"images": [{"image_uri": f"image-{i + 1}", "width": 512, "height": 512}
                             for i in range(images)],
                  "videos": [{"vid": f"video-{i + 1}", "duration": 2, "fps": 24}
                             for i in range(videos)],
                  "audios": [{"vid": f"audio-{i + 1}", "duration": 2.5}
                             for i in range(audios)]}
        _, index_map = build_materials(assets)
        return normalized, build_meta_list(normalized["prompt"], index_map), assets

    @staticmethod
    def refs(nodes):
        return [(node["meta_type"], node["material_ref"]["material_idx"])
                for node in nodes if "material_ref" in node]

    @staticmethod
    def text(nodes):
        return "".join(node.get("text", "") for node in nodes)

    def test_observed_natural_chinese_labels_bind_in_both_modes(self):
        for strict in (False, True):
            for prompt, refs, text in (
                ("图片1和图片2", [("image", 0), ("image", 1)], "和"),
                ("参考图片1，参考图片2", [("image", 0), ("image", 1)], "参考，参考"),
                ("用图1中的角色", [("image", 0)], "用中的角色"),
            ):
                with self.subTest(prompt=prompt, strict=strict):
                    normalized, nodes, _ = self.normalized_and_nodes(prompt, strict=strict)
                    self.assertEqual(normalized["prompt"], prompt)
                    self.assertEqual(self.refs(nodes), refs)
                    self.assertEqual(self.text(nodes), text)

    def test_explicit_chinese_neighbors_keep_explicit_missing_media_semantics(self):
        prompt = "角色@图片1和@图2跳舞"
        _, nodes, _ = self.normalized_and_nodes(prompt)
        self.assertEqual(self.refs(nodes), [("image", 0), ("image", 1)])
        for prompt, kind in (("用@图1中的角色", "images"),
                             ("按@视频1中的动作", "videos"),
                             ("跟@音频1中的节奏", "audios")):
            with self.subTest(kind=kind):
                with self.assertRaisesRegex(ValueError, f"only 0 {kind}"):
                    self.normalized_and_nodes(prompt, images=0, strict=True)
                _, nodes, _ = self.normalized_and_nodes(prompt, images=0, strict=False)
                self.assertEqual(self.refs(nodes), [])
                self.assertEqual(self.text(nodes), prompt)
        # Natural language without supplied media remains allowed in strict mode.
        _, nodes, _ = self.normalized_and_nodes("图1中的人看视频1", images=0)
        self.assertEqual(self.refs(nodes), [])

    def test_ascii_words_underscores_tags_and_unobserved_kind_remain_text(self):
        for token in ("myimage1", "myIMAGE1", "image1suffix", "_image1", "image1_",
                      "prefix图片1", "图片1suffix", "图1_中文", "myv1", "v1tail",
                      "<image1>", "<img1>", "<图1>", "<video1>", "<@image1>",
                      "my@image1", "@image1suffix", "图像1"):
            with self.subTest(token=token):
                _, nodes, _ = self.normalized_and_nodes(token)
                self.assertEqual(self.refs(nodes), [])
                self.assertEqual(self.text(nodes), token)
        _, nodes, _ = self.normalized_and_nodes("<image1>和图片2")
        self.assertEqual(self.refs(nodes), [("image", 1)])
        self.assertEqual(self.text(nodes), "<image1>和")

    def test_multidigit_complete_indices_never_backtrack_to_valid_prefix(self):
        for label in ("图11中文", "图１２中文", "图0中文"):
            with self.subTest(label=label):
                with self.assertRaisesRegex(ValueError, "only 10 images"):
                    self.normalized_and_nodes(label, images=10)
                _, nodes, _ = self.normalized_and_nodes(label, images=10, strict=False)
                self.assertEqual(self.refs(nodes), [])
                self.assertEqual(self.text(nodes), label)
        _, nodes, _ = self.normalized_and_nodes("用图片10中的人物和图01中的背景", images=10)
        self.assertEqual(self.refs(nodes), [("image", 9), ("image", 0)])
        _, nodes, _ = self.normalized_and_nodes("用图１２中的人物", images=12)
        self.assertEqual(self.refs(nodes), [("image", 11)])
        for label in ("image1２tail", "图１２suffix", "image12suffix"):
            with self.subTest(label=label):
                _, nodes, _ = self.normalized_and_nodes(label, images=12)
                self.assertEqual(self.refs(nodes), [])
                self.assertEqual(self.text(nodes), label)

    def test_multimedia_case_whitespace_emoji_and_repeated_refs_keep_order(self):
        prompt = "😀图2和@图片2，按ViDeO\t1中的动作及@AuDiO 2的节奏，再用IMG\n1✨"
        normalized, nodes, _ = self.normalized_and_nodes(prompt, images=2, videos=1, audios=2)
        self.assertEqual(self.refs(nodes), [("image", 1), ("image", 1), ("video", 2),
                                           ("audio", 4), ("image", 0)])
        self.assertEqual(prompt_references(normalized["prompt"], normalized, strict=True),
                         [("images", 2), ("images", 2), ("videos", 1), ("audios", 2), ("images", 1)])
        self.assertEqual(self.text(nodes), "😀和，按中的动作及的节奏，再用✨")

    def test_out_of_range_unmatched_text_and_valid_bindings_survive_together(self):
        prompt = "用图片10中的角色和图2中的背景，再按视频2里的动作"
        with self.assertRaisesRegex(ValueError, "only 2 images"):
            self.normalized_and_nodes(prompt, images=2, videos=1)
        _, nodes, _ = self.normalized_and_nodes(prompt, images=2, videos=1, strict=False)
        self.assertEqual(self.refs(nodes), [("image", 1)])
        self.assertIn("图片10", self.text(nodes))
        self.assertIn("视频2", self.text(nodes))

    def test_actual_submit_draft_points_to_the_intended_uploaded_materials(self):
        prompt = "用图2中的角色，按视频1和音频2的节奏，图2再次出现"
        normalized, _, assets = self.normalized_and_nodes(prompt, images=2, videos=1, audios=2)
        body, _ = build_submit_body(normalized, assets)
        draft = json.loads(body["draft_content"])
        unified = draft["component_list"][0]["abilities"]["gen_video"]["text_to_video_params"][
            "video_gen_inputs"][0]["unified_edit_input"]
        nodes = unified["meta_list"]
        self.assertEqual(self.refs(nodes), [("image", 1), ("video", 2), ("audio", 4), ("image", 1)])
        materials = unified["material_list"]
        self.assertEqual(materials[1]["image_info"]["image_uri"], "image-2")
        self.assertEqual(materials[2]["video_info"]["vid"], "video-1")
        self.assertEqual(materials[4]["audio_info"]["vid"], "audio-2")


if __name__ == "__main__":
    unittest.main()
