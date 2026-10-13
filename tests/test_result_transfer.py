import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import requests

from app.result_transfer import ResultTransferError, _download


class FakeResponse:
    def __init__(self, status, headers, chunks):
        self.status_code = status
        self.headers = headers
        self.chunks = chunks

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def iter_content(self, _):
        for chunk in self.chunks:
            if isinstance(chunk, Exception):
                raise chunk
            yield chunk


class FakeSession:
    responses = []
    calls = []

    def __init__(self):
        self.trust_env = True

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def get(self, url, **kwargs):
        assert self.trust_env is False
        self.calls.append(kwargs)
        return self.responses.pop(0)


class ResultDownloadTests(unittest.TestCase):
    def setUp(self):
        FakeSession.calls = []
        FakeSession.responses = []

    def test_interrupted_result_resumes_through_same_account_proxy(self):
        FakeSession.responses = [
            FakeResponse(200, {"Content-Length": "10", "ETag": '"same"'},
                         [b"abcd", requests.exceptions.ChunkedEncodingError("cut off")]),
            FakeResponse(206, {"Content-Range": "bytes 4-9/10", "ETag": '"same"'},
                         [b"efghij"]),
        ]
        with tempfile.TemporaryDirectory() as directory, \
                patch("app.result_transfer.requests.Session", FakeSession), \
                patch("app.result_transfer.time.sleep"):
            target = Path(directory) / "video.mp4"
            _download("https://video.example.com/result.mp4", "socks5://proxy:20002", target)
            self.assertEqual(target.read_bytes(), b"abcdefghij")
        self.assertEqual(FakeSession.calls[1]["headers"]["Range"], "bytes=4-")
        self.assertEqual(FakeSession.calls[1]["headers"]["If-Range"], '"same"')
        self.assertTrue(all(call["proxies"]["https"] == "socks5h://proxy:20002"
                            for call in FakeSession.calls))

    def test_range_ignored_restarts_file_instead_of_appending(self):
        FakeSession.responses = [
            FakeResponse(200, {"Content-Length": "10"},
                         [b"abcd", requests.exceptions.ChunkedEncodingError("cut off")]),
            FakeResponse(200, {"Content-Length": "6"}, [b"new123"]),
        ]
        with tempfile.TemporaryDirectory() as directory, \
                patch("app.result_transfer.requests.Session", FakeSession), \
                patch("app.result_transfer.time.sleep"):
            target = Path(directory) / "video.mp4"
            _download("https://video.example.com/result.mp4", "socks5://proxy:20002", target)
            self.assertEqual(target.read_bytes(), b"new123")
        self.assertEqual(FakeSession.calls[1]["headers"]["Range"], "bytes=4-")

    def test_wrong_resume_range_rejects_possible_corruption(self):
        FakeSession.responses = [
            FakeResponse(200, {"Content-Length": "10"},
                         [b"abcd", requests.exceptions.ChunkedEncodingError("cut off")]),
            FakeResponse(206, {"Content-Range": "bytes 0-9/10"}, [b"abcdefghij"]),
        ]
        with tempfile.TemporaryDirectory() as directory, \
                patch("app.result_transfer.requests.Session", FakeSession), \
                patch("app.result_transfer.time.sleep"):
            with self.assertRaisesRegex(ResultTransferError, "resume range is invalid"):
                _download("https://video.example.com/result.mp4", "socks5://proxy:20002",
                          Path(directory) / "video.mp4")


if __name__ == "__main__":
    unittest.main()
