import io
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

from PIL import Image

from app.upload import UploadError, _api_call, upload_media_bytes


class UploadRegionTests(unittest.TestCase):
    def setUp(self):
        self.token = {
            "access_key_id": "test-access", "secret_access_key": "test-secret",
            "session_token": "test-session", "space_name": "test-space",
        }
        output = io.BytesIO()
        Image.new("RGB", (3, 2)).save(output, format="PNG")
        self.contents = output.getvalue()

    def test_image_apply_and_commit_use_token_region_and_returned_blob_host(self):
        for region, origin, signing_region in (
            ("us", "https://imagex16-normal-us-ttp2.capcutapi.us/", "us-east-1"),
            ("useast8", "https://imagex16-normal-us-ttp2.capcutapi.us/", "ap-singapore-1"),
            ("sg", "https://imagex-normal-sg.capcutapi.com/", "ap-southeast-1"),
            (None, "https://imagex-normal-sg.capcutapi.com/", "ap-southeast-1"),
            ("", "https://imagex-normal-sg.capcutapi.com/", "ap-southeast-1"),
        ):
            with self.subTest(region=region):
                token = dict(self.token)
                if region is not None:
                    token["region"] = region
                if region == "useast8":
                    token["upload_domain"] = "imagex16-normal-us-ttp2.capcutapi.us"
                browser = Mock()
                browser.post.return_value = token
                replies = [
                    {"Result": {"UploadAddress": {
                        "UploadHosts": ["blob.example.test"],
                        "StoreInfos": [{"StoreUri": "test/image", "Auth": "test-auth"}],
                        "SessionKey": "test-key",
                    }}},
                    {"Result": {"Results": [{"Uri": "uploaded-image", "UriStatus": 2000}]}},
                ]
                with patch("app.upload._session") as session, \
                        patch("app.upload._api_call", side_effect=replies) as api, \
                        patch("app.upload._upload_blob") as blob:
                    result = upload_media_bytes(self.contents, "images", browser=browser,
                                                proxy_url="socks5://proxy.test:1080",
                                                filename="reference.png")
                self.assertEqual(result, {"image_uri": "uploaded-image", "width": 3,
                                          "height": 2, "filename": "reference.png"})
                browser.post.assert_called_once_with("/mweb/v1/get_upload_token", {"scene": 2},
                                                     query={"aid": "513695", "device_platform": "web"})
                self.assertEqual([call.args[1] for call in api.call_args_list], ["GET", "POST"])
                for call in api.call_args_list:
                    self.assertEqual(urlsplit(call.args[2]).netloc, urlsplit(origin).netloc)
                    self.assertEqual(call.kwargs, {"signing_region": signing_region})
                    self.assertEqual(call.args[3:5], (token, "imagex"))
                blob.assert_called_once_with(session.return_value,
                                             "https://blob.example.test/upload/v1/test/image",
                                             "test-auth", self.contents, image=True)

    def test_unknown_explicit_image_region_stops_before_apply(self):
        for region in ("eu", "US", "https://untrusted.test", {}, 1):
            with self.subTest(region=region):
                browser = Mock()
                browser.post.return_value = dict(self.token, region=region)
                with patch("app.upload._session"), patch("app.upload._api_call") as api, \
                        patch("app.upload._upload_blob") as blob:
                    with self.assertRaisesRegex(UploadError, "^Dreamina image upload region is unsupported$"):
                        upload_media_bytes(self.contents, "images", browser=browser,
                                           proxy_url="socks5://proxy.test:1080")
                api.assert_not_called()
                blob.assert_not_called()

    def test_useast8_rejects_mismatched_upload_domain_before_apply(self):
        for domain in (None, "untrusted.test", "imagex-normal-sg.capcutapi.com", "",
                       "imagex16-normal-us-ttp2.capcutapi.us.untrusted.test",
                       "https://imagex16-normal-us-ttp2.capcutapi.us/", {}, 1):
            with self.subTest(domain=domain):
                browser = Mock()
                browser.post.return_value = dict(self.token, region="useast8", upload_domain=domain)
                with patch("app.upload._session"), patch("app.upload._api_call") as api, \
                        patch("app.upload._upload_blob") as blob:
                    with self.assertRaisesRegex(UploadError, "^Dreamina image upload domain is unsupported$"):
                        upload_media_bytes(self.contents, "images", browser=browser,
                                           proxy_url="socks5://proxy.test:1080")
                api.assert_not_called()
                blob.assert_not_called()

    def test_useast8_without_upload_domain_uses_only_fixed_verified_origin(self):
        browser = Mock()
        browser.post.return_value = dict(self.token, region="useast8")
        with patch("app.upload._session"), \
                patch("app.upload._api_call", side_effect=UploadError("stop after apply")) as api:
            with self.assertRaisesRegex(UploadError, "^stop after apply$"):
                upload_media_bytes(self.contents, "images", browser=browser,
                                   proxy_url="socks5://proxy.test:1080")
        self.assertEqual(urlsplit(api.call_args.args[2]).netloc,
                         "imagex16-normal-us-ttp2.capcutapi.us")
        self.assertEqual(api.call_args.kwargs, {"signing_region": "ap-singapore-1"})

    def test_api_call_preserves_default_signing_and_accepts_explicit_us(self):
        for kwargs, region in (({}, "ap-southeast-1"),
                               ({"signing_region": "us-east-1"}, "us-east-1"),
                               ({"signing_region": "ap-singapore-1"}, "ap-singapore-1")):
            with self.subTest(region=region), patch("app.upload._request", return_value={}) as request:
                _api_call(Mock(), "GET", "https://upload.example.test/?Action=ApplyImageUpload",
                          self.token, "imagex", **kwargs)
            authorization = request.call_args.kwargs["headers"]["Authorization"]
            self.assertIn(f"/{region}/imagex/aws4_request", authorization)

    def test_vod_apply_blob_commit_use_token_region_and_returned_blob_host(self):
        for kind, filename in (("videos", "reference.mp4"), ("audios", "reference.mp3")):
            for region, domain, host, api_kwargs in (
                ("useast8", "vod16-normal-us-ttp2.capcutapi.us", "vod16-normal-us-ttp2.capcutapi.us", {"signing_region": "ap-singapore-1"}),
                ("useast8", None, "vod16-normal-us-ttp2.capcutapi.us", {"signing_region": "ap-singapore-1"}),
                ("us", "vod16-normal-us-ttp2.capcutapi.us", "vod16-normal-us-ttp2.capcutapi.us", {"signing_region": "us-east-1"}),
                ("us", None, "vod16-normal-us-ttp2.capcutapi.us", {"signing_region": "us-east-1"}),
                ("sg", None, "vod-normal-sg.capcutapi.com", {}),
                (None, None, "vod-normal-sg.capcutapi.com", {}),
                ("", None, "vod-normal-sg.capcutapi.com", {}),
                ("sg1", None, "vod-normal-sg.capcutapi.com", {}),
                ("unknown", "untrusted.test", "vod-normal-sg.capcutapi.com", {}),
                ("US", None, "vod-normal-sg.capcutapi.com", {}),
            ):
                with self.subTest(kind=kind, region=region, domain=domain):
                    self._assert_vod_flow(kind, filename, region, domain, host, api_kwargs)

    def _assert_vod_flow(self, kind, filename, region, domain, host, api_kwargs):
        browser = Mock()
        token = dict(self.token)
        if region is not None:
            token["region"] = region
        if domain is not None:
            token["upload_domain"] = domain
        browser.post.return_value = token
        replies = [
            {"Result": {"InnerUploadAddress": {"UploadNodes": [{
                "UploadHost": "vod-blob.example.test", "SessionKey": "vod-session",
                "StoreInfos": [{"StoreUri": "test/video", "Auth": "vod-auth"}],
            }]}}},
            {"Result": {"Results": [{"Vid": "uploaded-video"}]}},
        ]
        with patch("app.upload._session") as session, \
                patch("app.upload._api_call", side_effect=replies) as api, \
                patch("app.upload._upload_blob") as blob, \
                patch("app.upload._probe_media", return_value={
                    "duration": 4, "width": 16, "height": 9, "fps": 24}):
            result = upload_media_bytes(b"media", kind, browser=browser,
                                        proxy_url="socks5://proxy.test:1080", filename=filename)
        self.assertEqual(result, {"vid": "uploaded-video", "duration": 4,
                                  "width": 16, "height": 9, "fps": 24, "filename": filename})
        browser.post.assert_called_once_with("/mweb/v1/get_upload_token", {"scene": 1},
                                             query={"aid": "513695", "device_platform": "web"})
        self.assertEqual([call.args[1] for call in api.call_args_list], ["GET", "POST"])
        self.assertEqual([parse_qs(urlsplit(call.args[2]).query)["Action"] for call in api.call_args_list],
                         [["ApplyUploadInner"], ["CommitUploadInner"]])
        for call in api.call_args_list:
            self.assertEqual(urlsplit(call.args[2]).netloc, host)
            self.assertEqual(call.args[3:5], (token, "vod"))
            self.assertEqual(call.kwargs, api_kwargs)
        self.assertEqual(api.call_args_list[1].args[5], {"SessionKey": "vod-session", "Functions": []})
        blob.assert_called_once_with(session.return_value,
                                     "https://vod-blob.example.test/upload/v1/test/video",
                                     "vod-auth", b"media", image=False)

    def test_vod_us_rejects_mismatched_domain_before_apply_or_blob(self):
        for kind in ("videos", "audios"):
            for region in ("useast8", "us"):
                for domain in (None, "", "vod-normal-sg.capcutapi.com", "untrusted.test",
                               "vod16-normal-us-ttp2.capcutapi.us.untrusted.test",
                               "https://vod16-normal-us-ttp2.capcutapi.us/", {}, 1):
                    with self.subTest(kind=kind, region=region, domain=domain):
                        browser = Mock()
                        browser.post.return_value = dict(self.token, region=region, upload_domain=domain)
                        with patch("app.upload._session"), patch("app.upload._api_call") as api, \
                                patch("app.upload._upload_blob") as blob:
                            with self.assertRaisesRegex(UploadError, "^Dreamina VOD upload domain is unsupported$"):
                                upload_media_bytes(b"media", kind, browser=browser,
                                                   proxy_url="socks5://proxy.test:1080")
                        api.assert_not_called()
                        blob.assert_not_called()


if __name__ == "__main__":
    unittest.main()
