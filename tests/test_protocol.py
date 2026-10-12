import json
import gc
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import requests

from app.catalog import normalize_request
from app.client import build_submit_body
from app.upload import _request
from app.result_transfer import ResultTransferError
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
        with self.assertRaisesRegex(ValueError, "only 1 images"):
            normalize_request(payload)

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
            request = {"model": "sd-2-5-480p-x", "prompt": "A blue sky", "duration": 4}
            task = store.create_task("ijm_test", request, normalize_request(request))
            store.update_task(task["id"], {"status": "running", "account_id": 1})
            with patch("app.service.transfer", side_effect=ResultTransferError("upload failed")):
                service._poll(task["id"], Client(), time.monotonic() + 10)
            failed = store.task(task["id"])
            self.assertEqual(failed["error_code"], "RESULT_TRANSFER_FAILED")
            self.assertEqual(failed["upstream_result_urls"], ["https://upstream.test/720p.mp4"])
            self.assertEqual(service.public_task(failed)["data"], [])
            service.stop()
            gc.collect()


if __name__ == "__main__":
    unittest.main()
