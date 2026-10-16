import json
import gc
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import requests

from app.catalog import normalize_request
from app.client import build_submit_body
from app.upload import _request
from app.result_transfer import (
    ResultTransferError, SD_2_5_480P_X_DIMENSIONS, _config, _probe,
    _transcode, _upload_headers,
)
from app.service import Service
from app.store import Store


class ProtocolTests(unittest.TestCase):
    def test_25_acceptance_shape_and_reference_order(self):
        body = {
            "model": "sd-2-5", "prompt": "图片10，音频3，视频1。最后只生成一条视频。",
            "duration": 4, "aspect_ratio": "16:9",
            "image_urls": [f"https://example.com/{i}.png" for i in range(10)],
            "audio_urls": [f"https://example.com/{i}.wav" for i in range(3)],
            "video_urls": ["https://example.com/2s.mp4"],
        }
        normalized = normalize_request(body)
        assets = {
            "images": [{"image_uri": f"image-{i}", "width": 512, "height": 512}
                       for i in range(10)],
            "videos": [{"vid": "video-1", "duration": 2, "width": 512,
                        "height": 288, "fps": 24}],
            "audios": [{"vid": f"audio-{i}", "duration": 2} for i in range(3)],
        }
        submitted, submit_id = build_submit_body(normalized, assets)
        draft = json.loads(submitted["draft_content"])
        params = draft["component_list"][0]["abilities"]["gen_video"]["text_to_video_params"]
        media = params["video_gen_inputs"][0]["unified_edit_input"]
        refs = [(item["meta_type"], item["material_ref"]["material_idx"])
                for item in media["meta_list"] if "material_ref" in item]
        self.assertEqual(len(media["material_list"]), 14)
        self.assertEqual(refs, [("image", 9), ("audio", 13), ("video", 10)])
        self.assertEqual(params["model_req_key"], "dreamina_seedance_45_pro")
        self.assertEqual(params["video_gen_inputs"][0]["duration_ms"], 4000)
        self.assertEqual(submit_id, json.loads(submitted["metrics_extra"])["originSubmitId"])

    def test_20_limits_and_missing_reference(self):
        payload = {"model": "sd-2-0", "prompt": "图片10",
                   "image_urls": ["https://example.com/a.png"] * 10}
        with self.assertRaisesRegex(ValueError, "at most 9"):
            normalize_request(payload)
        payload["image_urls"] = ["https://example.com/a.png"]
        self.assertEqual(normalize_request(payload)["prompt"], payload["prompt"])
        with self.assertRaisesRegex(ValueError, "only 1 images"):
            normalize_request(payload, strict_media_references=True)

    def test_unmatched_image_labels_remain_text_and_valid_labels_bind(self):
        payload = {"model": "sd-2-5-480p", "duration": 4,
                   "prompt": "参考 @图片5、@图片2，再生成一段视频",
                   "image_urls": [f"https://example.com/{index}.png" for index in range(1, 5)]}
        normalized = normalize_request(payload)
        assets = {"images": [{"image_uri": f"image-{index}", "width": 512, "height": 512}
                             for index in range(1, 5)], "videos": [], "audios": []}
        submitted, _ = build_submit_body(normalized, assets)
        draft = json.loads(submitted["draft_content"])
        nodes = draft["component_list"][0]["abilities"]["gen_video"]["text_to_video_params"][
            "video_gen_inputs"][0]["unified_edit_input"]["meta_list"]
        self.assertIn("@图片5", "".join(node.get("text", "") for node in nodes))
        self.assertEqual([(node["meta_type"], node["material_ref"]["material_idx"])
                          for node in nodes if "material_ref" in node], [("image", 1)])
        with self.assertRaisesRegex(ValueError, "only 4 images"):
            normalize_request(payload, strict_media_references=True)

    def test_strict_reference_setting_controls_task_creation(self):
        with tempfile.TemporaryDirectory() as directory:
            service = Service(Store(str(Path(directory) / "settings.db")))
            request = {"model": "sd-2-5-480p", "duration": 4,
                       "prompt": "参考 @图片5",
                       "image_urls": [f"https://example.com/{index}.png" for index in range(4)]}
            self.assertFalse(service.settings()["strict_prompt_media_references"])
            with patch.object(service._pool, "submit"):
                self.assertEqual(service.create(request)["status"], "queued")
            service.update_settings({"strict_prompt_media_references": True})
            with self.assertRaisesRegex(ValueError, "only 4 images"):
                service.create(request)
            service.stop()
            del service
            gc.collect()

    def test_inferred_resolution_alias_is_fixed(self):
        with self.assertRaisesRegex(ValueError, "uses 480p"):
            normalize_request({"model": "sd-2-5-480p", "prompt": "A blue sky",
                               "resolution": "720p"})

    def test_480p_x_generates_upstream_at_720p(self):
        normalized = normalize_request({"model": "sd-2-5-480p-x", "prompt": "A blue sky",
                                        "duration": 4, "resolution": "480p"})
        submitted, _ = build_submit_body(normalized, {"images": [], "videos": [], "audios": []})
        draft = json.loads(submitted["draft_content"])
        item = draft["component_list"][0]["abilities"]["gen_video"]["text_to_video_params"]["video_gen_inputs"][0]
        scene = json.loads(json.loads(submitted["metrics_extra"])["sceneOptions"])[0]
        self.assertEqual(item["resolution"], "720p")
        self.assertEqual(scene["resolution"], "720p")
        self.assertEqual(submitted["extend"]["m_video_commerce_info"]["benefit_type"],
                         "seedance_25_720p_output")

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg is required")
    def test_480p_x_transcodes_to_aspect_specific_dimensions(self):
        self.assertEqual(SD_2_5_480P_X_DIMENSIONS["1:1"], (640, 640))
        self.assertEqual(SD_2_5_480P_X_DIMENSIONS["9:21"], (432, 992))
        with tempfile.TemporaryDirectory() as directory:
            for ratio, source_size, expected in (
                ("1:1", "720x720", (640, 640)),
                ("21:9", "1344x576", (992, 432)),
            ):
                with self.subTest(ratio=ratio):
                    source = Path(directory) / f"source-{ratio.replace(':', '-')}.mp4"
                    target = Path(directory) / f"target-{ratio.replace(':', '-')}.mp4"
                    subprocess.run([
                        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                        "-f", "lavfi", "-i", f"color=c=blue:s={source_size}:r=12:d=0.5",
                        "-f", "lavfi", "-i", "sine=frequency=440:duration=0.5",
                        "-shortest", "-c:v", "libx264", "-preset", "ultrafast",
                        "-pix_fmt", "yuv420p", "-c:a", "aac", str(source),
                    ], check=True, capture_output=True, text=True, timeout=30)
                    self.assertEqual(_transcode(source, target, ratio), expected)
                    self.assertEqual(_probe(target)[:2], expected)
                    streams = subprocess.run([
                        "ffprobe", "-v", "error", "-show_entries", "stream=codec_type",
                        "-of", "json", str(target),
                    ], check=True, capture_output=True, text=True, timeout=30)
                    self.assertEqual(
                        {stream["codec_type"] for stream in json.loads(streams.stdout)["streams"]},
                        {"video", "audio"},
                    )

    def test_s3_center_presigned_header_lists_are_accepted(self):
        headers = _upload_headers({"Content-Type": ["video/mp4"],
                                   "X-Amz-Meta-Upload-Id": ["du_123"]})
        self.assertEqual(headers, {"Content-Type": "video/mp4",
                                   "X-Amz-Meta-Upload-Id": "du_123"})
        with self.assertRaisesRegex(ResultTransferError, "value is invalid"):
            _upload_headers({"Content-Type": [None]})

    def test_s3_settings_are_editable_without_exposing_token(self):
        with tempfile.TemporaryDirectory() as directory:
            service = Service(Store(str(Path(directory) / "settings.db")))
            public = service.update_settings({
                "enable_sd_2_5_480p_x": True,
                "result_s3_center_api_base": "https://s3-api.example.com",
                "result_s3_center_token": "secret-token",
                "result_s3_bucket": "assets",
                "result_s3_key_prefix": "aicontent/ai-sdijm",
            })
            self.assertEqual(public["result_s3_center_token"], "")
            self.assertTrue(public["result_s3_center_token_configured"])
            self.assertEqual(_config(service.settings()),
                             ("https://s3-api.example.com", "secret-token", "assets", "aicontent/ai-sdijm"))
            service.update_settings({"result_s3_center_token": ""})
            self.assertEqual(service.settings()["result_s3_center_token"], "secret-token")
            with self.assertRaisesRegex(ValueError, "prefix is invalid"):
                service.update_settings({"result_s3_key_prefix": "../invalid"})
            service.stop()
            del service
            gc.collect()

    def test_upload_retries_proxy_read_timeout(self):
        class ProxySession:
            def __init__(self):
                self.calls = 0

            def request(self, *args, **kwargs):
                self.calls += 1
                if self.calls == 1:
                    raise requests.ReadTimeout("transient proxy timeout")
                return self

            def raise_for_status(self):
                return None

            def json(self):
                return {"Result": {"ok": True}}

        session = ProxySession()
        with patch("app.upload.time.sleep"):
            result = _request(session, "GET", "https://example.com/upload")
        self.assertEqual(session.calls, 2)
        self.assertTrue(result["Result"]["ok"])

    def test_480p_x_never_returns_upstream_720p_on_transfer_failure(self):
        class Client:
            account = {"proxy_url": "socks5://proxy.test:20002"}

            def poll(self, *_):
                return {"status": "succeeded", "result_urls": ["https://upstream.test/720p.mp4"]}

        with tempfile.TemporaryDirectory() as directory:
            store = Store(str(Path(directory) / "test.db"))
            service = Service(store)
            request = {"model": "sd-2-5-480p-x", "prompt": "A blue sky", "duration": 4,
                       "aspect_ratio": "1:1"}
            task = store.create_task("ijm_test", request, normalize_request(request))
            store.update_task(task["id"], {"status": "running", "account_id": 1})
            with patch("app.service.transfer", side_effect=ResultTransferError("upload failed")) as transfer_mock:
                service._poll(task["id"], Client(), time.monotonic() + 10)
            self.assertEqual(transfer_mock.call_args.kwargs["aspect_ratio"], "1:1")
            failed = store.task(task["id"])
            self.assertEqual(failed["error_code"], "RESULT_TRANSFER_FAILED")
            self.assertEqual(failed["upstream_result_urls"], ["https://upstream.test/720p.mp4"])
            self.assertEqual(service.public_task(failed)["data"], [])
            service.stop()
            gc.collect()


if __name__ == "__main__":
    unittest.main()
