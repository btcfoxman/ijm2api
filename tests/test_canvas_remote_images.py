"""The documented image URL import retains proxy and original-ID safety."""
from copy import deepcopy

import pytest

from app.canvas_cli import CanvasCLIError
from app.canvas_client import CanvasClient, public_image_source_url
from app.canvas_service import CanvasServiceMixin
from test_canvas_transport import ACCOUNT, FakeRunner, request


URL = "https://media.example/reference.png?signature=a%2Fb%2Bz&expires=123"


def test_public_image_url_is_not_reencoded():
    assert public_image_source_url(URL) == URL


@pytest.mark.parametrize("value", [None, "", "file:///tmp/a.png", "data:image/png;base64,AA==",
    "http://127.0.0.1/a.png", "http://[::1]/a.png", "http://192.168.3.5/a.png",
    "http://169.254.169.254/a.png", "http://localhost/a.png", "http://server.internal/a.png",
    "http://2130706433/a.png", "http://127.1/a.png", "http://0x7f000001/a.png",
    "https://user:secret@media.example/a.png", "https://media.example:0/a.png",
    "https://media.example/a.png\n--model=other"])
def test_remote_image_import_refuses_nonpublic_or_ambiguous_locator(value):
    with pytest.raises(CanvasCLIError):
        public_image_source_url(value)


def test_public_images_skip_python_download_without_dropping_or_deduplicating(tmp_path, monkeypatch):
    monkeypatch.setattr("app.canvas_service._download", lambda *a, **k: pytest.fail("Public image downloaded locally"))
    body = {"images": [URL, URL], "videos": [], "audios": []}
    original = deepcopy(body)
    result = CanvasServiceMixin()._canvas_local_assets(body, ACCOUNT, tmp_path, deadline=10)
    assert result == {"images": [{"source_url": URL}, {"source_url": URL}], "videos": [], "audios": []}
    assert body == original and not list(tmp_path.iterdir())


def test_data_image_video_and_audio_keep_original_file_and_probe_path(tmp_path, monkeypatch):
    seen = []
    data_url = "data:image/png;base64,AA=="
    def download(source, kind, proxy, **kwargs):
        seen.append((source, kind, proxy, kwargs))
        suffix = {"images": "png", "videos": "mp4", "audios": "wav"}[kind]
        return b"original-fixture", "fixture/mime", "reference." + suffix
    probed = []
    def probe(body, suffix):
        probed.append((body, suffix))
        return {"duration": 4.0}
    monkeypatch.setattr("app.canvas_service._download", download)
    monkeypatch.setattr("app.canvas_service._probe_media", probe)
    body = {"images": [data_url], "videos": ["https://media.example/a.mp4"],
            "audios": ["https://media.example/a.wav"]}
    result = CanvasServiceMixin()._canvas_local_assets(body, ACCOUNT, tmp_path, deadline=123)
    assert [x[1] for x in seen] == ["images", "videos", "audios"]
    assert all(x[2] == ACCOUNT["proxy_url"] and x[3]["deadline"] == 123 for x in seen)
    assert [x[1] for x in probed] == [".mp4", ".wav"]
    for values in result.values():
        from pathlib import Path
        assert Path(values[0]["path"]).read_bytes() == b"original-fixture"
    assert result["videos"][0]["duration"] == result["audios"][0]["duration"] == 4


@pytest.mark.parametrize("unknown", [False, True])
def test_prepare_imports_original_image_url_once_with_original_resource_id(tmp_path, unknown):
    class Runner(FakeRunner):
        def run(self, account, args, **kwargs):
            result = super().run(account, args, **kwargs)
            if unknown and args[:2] == ["resource", "upload"]:
                raise CanvasCLIError("cli.upload_state_unknown", "registration receipt unknown", outcome_unknown=True)
            return result
    runner = Runner(tmp_path)
    client = CanvasClient(runner, ACCOUNT)
    prepared = client.prepare(request(), "ijm_remote_fixture", {
        "images": [{"source_url": URL}], "videos": [], "audios": []}, persist=runner.persisted.append)
    uploads = [args for args in runner.calls if args[:2] == ["resource", "upload"]]
    assert len(uploads) == 1
    assert "--file" not in uploads[0]
    assert uploads[0][uploads[0].index("--source-url") + 1] == URL
    resource_id = prepared["resources"]["images"][0]
    assert uploads[0][uploads[0].index("--resource-id") + 1] == resource_id
    assert prepared["uploaded_resources"] == [resource_id] and prepared["phase"] == "quoted"
    assert all(args[2] == resource_id for args in runner.calls if args[:2] == ["resource", "get"])
    assert not any(args[:2] in (["node", "confirm"], ["node", "run"]) for args in runner.calls)


@pytest.mark.parametrize("media_type", ["video", "audio"])
def test_source_url_is_never_used_for_nonimage_media(tmp_path, media_type):
    runner = FakeRunner(tmp_path)
    with pytest.raises(CanvasCLIError):
        CanvasClient(runner, ACCOUNT)._upload_resource(None, media_type, "original-resource", "original-project", source_url=URL)
    assert not runner.calls
