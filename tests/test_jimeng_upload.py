"""CN references never inherit ambient proxies or reuse another site's assets."""

import socket
import io
import json
from unittest.mock import MagicMock, Mock
from urllib.parse import urlsplit

import pytest
import requests
from PIL import Image

from app import jimeng_upload
from app.jimeng_upload import JimengUploader
from app.media import MediaError
from app.store import Store
from app.upload import UploadError


ACCOUNT = {"id": 7, "execution_mode": "jimeng_web", "proxy_url": "", "login_user_id": "synthetic-cn-user"}


@pytest.fixture(autouse=True)
def no_live_dns(monkeypatch):
    monkeypatch.setattr(jimeng_upload.socket, "getaddrinfo", Mock(return_value=[
        (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("8.8.8.8", 443))]))


def upload_token(kind="images"):
    return {"access_key_id": "synthetic-access", "secret_access_key": "synthetic-secret",
            "session_token": "synthetic-session", "region": "cn",
            "upload_domain": "imagex.bytedanceapi.com" if kind == "images" else "vod.bytedanceapi.com",
            "space_name": "tb4s082cfz" if kind == "images" else "dreamina"}


@pytest.mark.parametrize("kind,scene", [("images", 2), ("videos", 1), ("audios", 1)])
def test_only_observed_cn_token_metadata_is_accepted(kind, scene):
    post = Mock(return_value=upload_token(kind))
    assert jimeng_upload._upload_token(post, kind) == upload_token(kind)
    post.assert_called_once_with("/mweb/v1/get_upload_token", {"scene": scene})
    assert jimeng_upload.CN_SIGNING_REGION == "cn-north-1"


@pytest.mark.parametrize("field,value", [("region", None), ("region", "us"), ("region", "cn-north-1"),
    ("upload_domain", "127.0.0.1"), ("upload_domain", "imagex.bytedanceapi.com.evil.test"),
    ("upload_domain", "https://imagex.bytedanceapi.com/"), ("upload_domain", "imagex.bytedanceapi.com:443"),
    ("upload_domain", "evil.test@imagex.bytedanceapi.com"), ("space_name", "dreamina"),
    ("access_key_id", ""), ("session_token", "synthetic\r\nheader")])
def test_token_cannot_choose_an_untrusted_host_region_space_or_header(field, value):
    with pytest.raises(UploadError):
        jimeng_upload._validate_token({**upload_token(), field: value}, "images")


def test_unrecognized_upload_kind_never_selects_a_token_scene():
    post = Mock()
    with pytest.raises(UploadError, match="scene is unavailable"):
        jimeng_upload._upload_token(post, "unknown")
    post.assert_not_called()


def api_result(action, result, service="vod"):
    return {"ResponseMetadata": {"Region": "cn-north-1", "Service": service, "Action": action}, "Result": result}


def vod_flow():
    node = {"Vid": "synthetic-vid", "UploadHost": "tos-d-x-hl.snssdk.com", "SessionKey": "synthetic-session-key",
            "StoreInfos": [{"StoreUri": "tos-cn-v-148450/synthetic-object", "Auth": "synthetic-auth", "UploadHeader": {}, "StorageHeader": {}}]}
    replies = [api_result("ApplyUploadInner", {"InnerUploadAddress": {"UploadNodes": [node]}}),
        api_result("CommitUploadInner", {"Results": [{"Vid": "synthetic-vid", "VideoMeta": {
            "Duration": 5.146083, "Width": 1280, "Height": 720, "Fps": 0}}]})]
    return node, replies


@pytest.mark.parametrize("kind", ["videos", "audios"])
def test_vod_apply_blob_commit_use_verified_cn_routes_and_provider_duration(monkeypatch, kind):
    node, replies = vod_flow()
    post = Mock(return_value=upload_token(kind))
    session, api, blob = MagicMock(), Mock(side_effect=replies), Mock()
    session.__enter__.return_value = session
    monkeypatch.setattr(jimeng_upload, "_session", Mock(return_value=session))
    monkeypatch.setattr(jimeng_upload, "_api_call", api)
    monkeypatch.setattr(jimeng_upload, "_upload_blob", blob)
    monkeypatch.setattr(jimeng_upload, "_probe_reference", Mock(return_value={"duration": 5.329, "width": 640, "height": 360, "fps": 24}))
    result = jimeng_upload.upload_media_bytes(b"synthetic-media", kind, account=ACCOUNT, post=post, filename="reference.mp3")
    assert result["vid"] == node["Vid"]
    assert result["duration"] == 5.146083, "provider duration avoids MP3 container padding"
    assert (result["width"], result["height"], result["fps"]) == ((1280, 720, 0) if kind == "videos" else (0, 0, 0))
    post.assert_called_once_with("/mweb/v1/get_upload_token", {"scene": 1})
    apply = api.call_args_list[0].args[2]
    assert {k:v for k,v in apply.items() if k != "s"} == {"Action": "ApplyUploadInner", "Version": "2020-11-19",
        "SpaceName": "dreamina", "FileType": "video", "IsInner": "1", "FileSize": len(b"synthetic-media")}
    assert api.call_args_list[1].kwargs["payload"] == {"SessionKey": node["SessionKey"], "Functions": []}
    assert blob.call_args.args[1] == "https://tos-d-x-hl.snssdk.com/upload/v1/" + node["StoreInfos"][0]["StoreUri"]
    assert blob.call_args.kwargs["user_id"] == ACCOUNT["login_user_id"]


def test_image_apply_and_commit_follow_observed_official_sdk_body(monkeypatch):
    output = io.BytesIO()
    Image.new("RGB", (3, 2)).save(output, format="PNG")
    contents = output.getvalue()
    uri = "tos-cn-i-tb4s082cfz/synthetic.png"
    node = {"UploadHosts": ["tos-lf-x.snssdk.com"], "SessionKey": "synthetic-session-key",
            "StoreInfos": [{"StoreUri": uri, "Auth": "synthetic-auth"}]}
    replies = [api_result("ApplyImageUpload", {"UploadAddress": node}, "imagex"),
        api_result("CommitImageUpload", {"Results": [{"Uri": uri, "UriStatus": 2000}],
            "PluginResult": [{"ImageUri": uri, "SourceUri": uri, "ImageWidth": 3, "ImageHeight": 2}]}, "imagex")]
    session, api, blob = MagicMock(), Mock(side_effect=replies), Mock()
    session.__enter__.return_value = session
    monkeypatch.setattr(jimeng_upload, "_session", Mock(return_value=session))
    monkeypatch.setattr(jimeng_upload, "_api_call", api)
    monkeypatch.setattr(jimeng_upload, "_upload_blob", blob)
    post = Mock(return_value=upload_token())
    result = jimeng_upload.upload_media_bytes(contents, "images", account=ACCOUNT, post=post, filename="reference.png")
    assert result == {"image_uri": uri, "width": 3, "height": 2, "filename": "reference.png"}
    assert api.call_args_list[0].args[2]["ServiceId"] == "tb4s082cfz"
    assert "NeedFallback" not in api.call_args_list[0].args[2]
    assert api.call_args_list[1].kwargs["payload"] == {"SessionKey": "synthetic-session-key"}
    assert blob.call_args.args[1].startswith("https://tos-lf-x.snssdk.com/upload/v1/")


@pytest.mark.parametrize("host", ["127.0.0.1", "tos-d-x-hl.snssdk.com.evil.test", "https://tos-d-x-hl.snssdk.com/",
    "tos-d-x-hl.snssdk.com:443", "evil@tos-d-x-hl.snssdk.com"])
def test_vod_blob_destination_cannot_be_chosen_by_the_apply_response(monkeypatch, host):
    node, replies = vod_flow()
    node["UploadHost"] = host
    session, api, blob = MagicMock(), Mock(side_effect=replies), Mock()
    session.__enter__.return_value = session
    monkeypatch.setattr(jimeng_upload, "_session", Mock(return_value=session))
    monkeypatch.setattr(jimeng_upload, "_api_call", api)
    monkeypatch.setattr(jimeng_upload, "_upload_blob", blob)
    monkeypatch.setattr(jimeng_upload, "_probe_reference", Mock(return_value={"duration": 5, "width": 0, "height": 0, "fps": 0}))
    with pytest.raises(UploadError, match="host is unsupported"):
        jimeng_upload.upload_media_bytes(b"synthetic", "audios", account=ACCOUNT, post=Mock(return_value=upload_token("audios")))
    blob.assert_not_called()
    assert api.call_count == 1, "untrusted address cannot reach blob or commit"


def test_signed_cn_api_headers_do_not_reuse_dreamina_origin_or_region(monkeypatch):
    request = Mock(return_value=api_result("CommitUploadInner", {}))
    monkeypatch.setattr(jimeng_upload, "_request_json", request)
    jimeng_upload._api_call(Mock(), "POST", {"Action": "CommitUploadInner", "SpaceName": "dreamina"},
        upload_token("videos"), image=False, payload={"SessionKey": "synthetic-key", "Functions": []}, deadline=10**20)
    args, kwargs = request.call_args
    assert args[2].startswith("https://vod.bytedanceapi.com/?")
    assert "/cn-north-1/vod/aws4_request" in kwargs["headers"]["Authorization"]
    assert kwargs["headers"]["origin"] == "https://jimeng.jianying.com"
    assert json.loads(kwargs["data"]) == {"SessionKey": "synthetic-key", "Functions": []}


def test_blob_headers_match_the_official_cn_binary_upload(monkeypatch):
    request = Mock(return_value={"code": 2000})
    monkeypatch.setattr(jimeng_upload, "_request_json", request)
    jimeng_upload._upload_blob(Mock(), "https://tos-lf-x.snssdk.com/upload/v1/test", "synthetic-auth", b"abc",
        user_id="user id", filename='reference".png', deadline=10**20)
    headers = request.call_args.kwargs["headers"]
    assert headers["X-Storage-U"] == "user%20id"
    assert headers["Content-CRC32"] == "352441c2"
    assert headers["Content-Disposition"] == 'attachment; filename="reference%22.png"'


@pytest.mark.parametrize("uri", ["../private", "/absolute", "test?token=x", "test#fragment", "test%2fprivate", "test//double"])
def test_blob_paths_cannot_inject_a_host_query_or_traversal(uri):
    with pytest.raises(UploadError):
        jimeng_upload._blob_url("tos-d-x-hl.snssdk.com", uri, "videos")


def test_all_three_observed_upload_kinds_are_available_for_preflight():
    assert jimeng_upload.SUPPORTED_UPLOAD_KINDS == {"images", "videos", "audios"}
    assert not jimeng_upload.supports_upload_kind("files")


def test_signed_upload_requests_never_follow_redirects_or_repeat_a_write():
    session, response = Mock(), Mock()
    session.request.return_value = response
    response.status_code = 302
    with pytest.raises(UploadError, match="redirects are not allowed"):
        jimeng_upload._request_json(session, "POST", "https://vod.bytedanceapi.com/", headers={"Authorization": "synthetic"}, deadline=10**20)
    assert session.request.call_count == 1
    assert session.request.call_args.kwargs["allow_redirects"] is False
    response.json.assert_not_called()
    response.close.assert_called_once()


def test_cn_api_response_region_cannot_silently_switch(monkeypatch):
    response = api_result("ApplyUploadInner", {})
    response["ResponseMetadata"]["Region"] = "us-east-1"
    monkeypatch.setattr(jimeng_upload, "_request_json", Mock(return_value=response))
    with pytest.raises(UploadError, match="CN API binding"):
        jimeng_upload._api_call(Mock(), "GET", {"Action": "ApplyUploadInner"},
            upload_token("videos"), image=False, deadline=10**20)


def test_vod_commit_must_confirm_the_original_allocated_asset(monkeypatch):
    _, replies = vod_flow()
    replies[1]["Result"]["Results"][0]["Vid"] = "some-other-asset"
    session = MagicMock()
    session.__enter__.return_value = session
    monkeypatch.setattr(jimeng_upload, "_session", Mock(return_value=session))
    monkeypatch.setattr(jimeng_upload, "_api_call", Mock(side_effect=replies))
    monkeypatch.setattr(jimeng_upload, "_upload_blob", Mock())
    monkeypatch.setattr(jimeng_upload, "_probe_reference", Mock(return_value={"duration": 5, "width": 0, "height": 0, "fps": 0}))
    with pytest.raises(UploadError, match="originally allocated"):
        jimeng_upload.upload_media_bytes(b"synthetic", "audios", account=ACCOUNT, post=Mock(return_value=upload_token("audios")))


def test_local_probe_cannot_open_network_urls_hidden_in_a_playlist(monkeypatch):
    process = Mock(return_value=Mock(stdout=json.dumps({"format": {"duration": "5"}, "streams": []})))
    monkeypatch.setattr(jimeng_upload.subprocess, "run", process)
    assert jimeng_upload._probe_reference(b"synthetic", "reference.mp3")["duration"] == 5
    command = process.call_args.args[0]
    assert command[command.index("-protocol_whitelist") + 1] == "file,pipe"


def test_dynamic_native_tos_family_is_allowed_without_trusting_arbitrary_subdomains():
    uri = "tos-cn-i-tb4s082cfz/synthetic.png"
    for host in ("tos-lf-x.snssdk.com", "tos-d-x-hl.snssdk.com", "tos-d-x-lf.bytedancevod.com",
                 "tos-d-lq.bytedancevod.com", "tos-d-x-hl.bytedancevod.com"):
        assert jimeng_upload._blob_url(host, uri, "images").startswith("https://" + host + "/")
    for host in ("evil.bytedancevod.com", "arbitrary.snssdk.com", "evil.tos-d-lq.bytedancevod.com",
                 "tos-d-x-lf.bytedancevod.com.evil.test", "tos-d-x-lf.bytedancevod.com:443",
                 "https://tos-d-x-lf.bytedancevod.com", "tos-d-lq.bytedancevod.com.",
                 "tos--d.snssdk.com", "tos-d-.snssdk.com", "tos-d-\u00e4.snssdk.com",
                 "tos-aaaaaaaaaaaaa.snssdk.com", "tos-a-b-c-d-e.snssdk.com"):
        with pytest.raises(UploadError):
            jimeng_upload._blob_url(host, uri, "images")


def test_same_apply_group_can_select_a_verified_fallback_without_contacting_unknown_hosts(monkeypatch):
    resolve = Mock(return_value=("tos-d-x-lf.bytedancevod.com", "8.8.8.8"))
    monkeypatch.setattr(jimeng_upload, "_public_source_url", resolve)
    url, host, address = jimeng_upload._select_blob_route(
        ["untrusted.test", "tos-d-x-lf.bytedancevod.com"], "tos-cn-i-tb4s082cfz/synthetic.png", "images")
    assert (host, address) == ("tos-d-x-lf.bytedancevod.com", "8.8.8.8")
    assert url.startswith("https://tos-d-x-lf.bytedancevod.com/")
    resolve.assert_called_once_with(url)


def test_known_storage_host_with_private_dns_cannot_receive_upload_authorization(monkeypatch):
    monkeypatch.setattr(jimeng_upload.socket, "getaddrinfo", Mock(return_value=[
        (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("127.0.0.1", 443))]))
    with pytest.raises(UploadError, match="public address"):
        jimeng_upload._select_blob_route(["tos-d-x-lf.bytedancevod.com"], "tos-cn-i-tb4s082cfz/synthetic.png", "images")


@pytest.mark.parametrize("uri", ["tos-us-i-tb4s082cfz/object", "tos-cn-i-other-service/object", "tos-cn-v-148450/object"])
def test_known_image_host_cannot_change_the_native_cn_service_namespace(uri):
    with pytest.raises(UploadError, match="CN storage namespace"):
        jimeng_upload._blob_url("tos-d-x-lf.bytedancevod.com", uri, "images")


def test_uri_error_is_preserved_before_any_fallback_host_validation_or_dns(monkeypatch):
    resolve = Mock()
    monkeypatch.setattr(jimeng_upload, "_public_source_url", resolve)
    with pytest.raises(UploadError, match="CN storage namespace"):
        jimeng_upload._select_blob_route(["unknown.test", "tos-d-lq.bytedancevod.com"],
            "tos-us-i-tb4s082cfz/object", "images")
    resolve.assert_not_called()
    with pytest.raises(UploadError, match="blob URI is invalid"):
        jimeng_upload._select_blob_route(["unknown.test"], "../private", "images")
    resolve.assert_not_called()


def test_pinned_storage_connection_uses_account_proxy_without_resolving_the_target_again(monkeypatch):
    host = "tos-d-x-lf.bytedancevod.com"
    adapter = jimeng_upload._PinnedSourceAdapter(host, "8.8.8.8", allow_proxy=True)
    manager = Mock()
    factory = Mock(return_value=manager)
    monkeypatch.setattr(adapter, "proxy_manager_for", factory)
    request = requests.Request("POST", "https://" + host + "/upload/v1/test").prepare()
    proxies = {"https": "socks5h://proxy.test:1080"}
    adapter.get_connection_with_tls_context(request, True, proxies=proxies)
    factory.assert_called_once_with("socks5h://proxy.test:1080")
    args = manager.connection_from_host.call_args.kwargs
    assert args["host"] == "8.8.8.8"
    assert args["pool_kwargs"]["server_hostname"] == host
    assert args["pool_kwargs"]["assert_hostname"] == host


def test_upload_wrapper_preserves_source_order_and_uses_cn_account_cache(tmp_path, monkeypatch):
    store = Store(str(tmp_path / "test.db"))
    account = store.upsert_account({"name": "CN", "execution_mode": "jimeng_web"})
    store.update_account(account["id"], {"login_user_id": "synthetic-cn-user"})
    account = store.account(account["id"], private=True)
    download = Mock(side_effect=lambda source, kind, proxy, **kwargs: (source.encode(), "image/png", source))
    uploaded = Mock(side_effect=lambda contents, kind, **kwargs: {"image_uri": contents.decode(), "width": 2, "height": 1})
    monkeypatch.setattr(jimeng_upload, "_download_source", download)
    monkeypatch.setattr(jimeng_upload, "upload_media_bytes", uploaded)
    body = {"images": ["https://media.test/first.png", "https://media.test/second.png", "https://media.test/first.png"],
            "videos": [], "audios": []}
    uploader = JimengUploader(account, post=Mock(), store=store)
    result = uploader.upload_inputs(body)
    assert [item["image_uri"] for item in result["images"]] == body["images"]
    assert uploaded.call_count == 2
    assert result["videos"] == result["audios"] == []
    with store._connect() as database:
        assert {row[0] for row in database.execute("SELECT kind FROM media_cache")} == {"jimeng_web:images"}
    uploader.upload_inputs(body)
    assert uploaded.call_count == 2
    # Reauthenticating the same numeric account must not reuse the prior user.
    JimengUploader({**account, "login_user_id": "different-user"}, post=Mock(), store=store).upload_inputs(body)
    assert uploaded.call_count == 4


def test_cross_site_asset_in_any_kind_stops_before_the_first_download(monkeypatch):
    download = Mock()
    monkeypatch.setattr(jimeng_upload, "_download_source", download)
    uploader = JimengUploader(ACCOUNT, post=Mock())
    for invalid in ("dreamina-image:old-asset", "dreamina-vid:old-vid", "file:///private", "//private/file"):
        with pytest.raises(MediaError):
            uploader.upload_inputs({"images": ["https://media.test/first.png"], "audios": [invalid]})
    download.assert_not_called()


@pytest.mark.parametrize("mode", ["canvas_cli", "browser", None])
def test_account_backend_must_explicitly_be_jimeng(mode):
    with pytest.raises(UploadError):
        JimengUploader({**ACCOUNT, "execution_mode": mode}, post=Mock())


def test_empty_proxy_is_explicit_direct_and_socks_dns_stays_on_account_route(monkeypatch):
    session = Mock()
    session.proxies = {}
    monkeypatch.setattr(jimeng_upload.requests, "Session", Mock(return_value=session))
    assert jimeng_upload._session("") is session
    assert session.trust_env is False
    assert session.proxies == {}
    jimeng_upload._session("socks5://proxy.test:1080")
    assert session.proxies == {"http": "socks5h://proxy.test:1080", "https": "socks5h://proxy.test:1080"}


@pytest.mark.parametrize("url", ["http://127.0.0.1/a", "http://169.254.169.254/a",
    "https://localhost/a", "https://service.internal/a", "https://user:pass@public.test/a",
    "https://public.test:9443/a"])
def test_direct_sources_reject_private_hosts_credentials_and_unapproved_ports(monkeypatch, url):
    monkeypatch.setattr(jimeng_upload.socket, "getaddrinfo", Mock(return_value=[
        (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("127.0.0.1", 443))]))
    session = Mock()
    monkeypatch.setattr(jimeng_upload, "_session", session)
    with pytest.raises(MediaError):
        jimeng_upload._download_source(url, "images", "", deadline=10**20)
    session.assert_not_called()


def test_proxy_media_download_has_no_direct_fallback(monkeypatch):
    download = Mock(return_value=(b"image", "image/png", "image.png"))
    monkeypatch.setattr(jimeng_upload, "_download", download)
    assert jimeng_upload._download_source("https://media.test/image.png", "images",
        "socks5://proxy.test:1080", deadline=123) == download.return_value
    download.assert_called_once_with("https://media.test/image.png", "images", "socks5://proxy.test:1080",
                                    allow_direct_fallback=False, deadline=123)


def test_direct_media_connection_pins_the_validated_ip_and_preserves_tls_hostname(monkeypatch):
    adapter = jimeng_upload._PinnedSourceAdapter("public.test", "8.8.8.8")
    pool = Mock()
    monkeypatch.setattr(adapter, "poolmanager", pool)
    request = requests.Request("GET", "https://public.test/image.png").prepare()
    adapter.get_connection_with_tls_context(request, verify=True)
    args = pool.connection_from_host.call_args.kwargs
    assert args["host"] == "8.8.8.8"
    assert args["pool_kwargs"]["server_hostname"] == "public.test"
    assert args["pool_kwargs"]["assert_hostname"] == "public.test"
    assert request.headers["Host"] == "public.test"
    request.url = "https://different.test/image.png"
    with pytest.raises(MediaError):
        adapter.get_connection_with_tls_context(request, verify=True)


def test_older_requests_connection_api_cannot_bypass_the_pinned_ip(monkeypatch):
    adapter = jimeng_upload._PinnedSourceAdapter("public.test", "8.8.8.8")
    pool = Mock()
    monkeypatch.setattr(adapter, "poolmanager", pool)
    monkeypatch.setattr(adapter, "build_connection_pool_key_attributes", None)
    adapter.get_connection("https://public.test/image.png")
    args = pool.connection_from_host.call_args.kwargs
    assert args["host"] == "8.8.8.8"
    assert args["pool_kwargs"]["server_hostname"] == "public.test"
    assert args["pool_kwargs"]["assert_hostname"] == "public.test"
    request = requests.Request("GET", "https://public.test/image.png").prepare()
    adapter.add_headers(request)
    assert request.headers["Host"] == "public.test"
    adapter._get_connection(request, True)
    assert pool.connection_from_host.call_args.kwargs["host"] == "8.8.8.8"


def redirect_sessions(monkeypatch, locations):
    sessions, responses = [], []
    for location in [*locations, None]:
        session, response = MagicMock(), MagicMock()
        session.__enter__.return_value = session
        session.get.return_value.__enter__.return_value = response
        response.status_code = 302 if location is not None else 200
        response.headers = {"Location": location} if location is not None else {"Content-Type": "image/png"}
        sessions.append(session)
        responses.append(response)
    factory = Mock(side_effect=sessions)
    monkeypatch.setattr(jimeng_upload, "_session", factory)
    monkeypatch.setattr(jimeng_upload, "_deadline_chunks", Mock(return_value=[b"synthetic-image"]))
    return sessions, factory


def test_direct_media_accepts_three_controlled_redirects_with_each_dns_route_pinned(monkeypatch):
    sessions, factory = redirect_sessions(monkeypatch, ["/second", "https://cdn.test/third", "/image.png"])
    resolve = Mock(side_effect=lambda url: (urlsplit(url).hostname, "8.8.8.8"))
    monkeypatch.setattr(jimeng_upload, "_public_source_url", resolve)
    data, mime, filename = jimeng_upload._download_source("https://public.test/first", "images", "", deadline=10**20)
    assert (data, mime, filename) == (b"synthetic-image", "image/png", "image.png")
    assert [call.args[0] for call in resolve.call_args_list] == ["https://public.test/first",
        "https://public.test/second", "https://cdn.test/third", "https://cdn.test/image.png"]
    assert factory.call_count == 4
    for session, url in zip(sessions, [call.args[0] for call in resolve.call_args_list]):
        assert session.get.call_args.kwargs["allow_redirects"] is False
        assert isinstance(session.mount.call_args.args[1], jimeng_upload._PinnedSourceAdapter)
        assert session.mount.call_args.args[1].host == urlsplit(url).hostname


def test_redirect_to_private_host_stops_before_connecting_that_hop(monkeypatch):
    sessions, factory = redirect_sessions(monkeypatch, ["http://169.254.169.254/credentials"])
    monkeypatch.setattr(jimeng_upload.socket, "getaddrinfo", Mock(side_effect=lambda host, *args, **kwargs:
        [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("169.254.169.254" if host == "169.254.169.254" else "8.8.8.8", 443))]))
    with pytest.raises(MediaError):
        jimeng_upload._download_source("https://public.test/first", "images", "", deadline=10**20)
    assert factory.call_count == 1
    sessions[1].get.assert_not_called()


def test_fourth_redirect_is_rejected_with_a_fixed_network_bound(monkeypatch):
    _, factory = redirect_sessions(monkeypatch, ["/second", "/third", "/fourth", "/fifth"])
    monkeypatch.setattr(jimeng_upload, "_public_source_url", Mock(return_value=("public.test", "8.8.8.8")))
    with pytest.raises(MediaError, match="redirect limit"):
        jimeng_upload._download_source("https://public.test/first", "images", "", deadline=10**20)
    assert factory.call_count == 4
