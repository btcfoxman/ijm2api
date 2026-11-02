"""Account-scoped CN reference uploads, isolated from Dreamina credentials."""

from __future__ import annotations

import hashlib
import io
import ipaddress
import json
import math
import mimetypes
import re
import secrets
import socket
import subprocess
import tempfile
import threading
import time
import zlib
from fractions import Fraction
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote, urlencode, urljoin, urlsplit

import requests
from PIL import Image

from .media import (MAX_MEDIA_BYTES, MediaError, _deadline_chunks, _download,
                    _media_remaining)
from .upload import UploadError, _sign


_UPLOAD_LOCKS: dict[int, threading.RLock] = {}
_UPLOAD_LOCKS_GUARD = threading.Lock()
_KINDS = ("images", "videos", "audios")
CN_SIGNING_REGION = "cn-north-1"
# Native get_upload_token metadata captured from the authenticated CN page on
# 2026-11-02. The user's MP3 upload also verified scene 1 and a VOD video ID.
_TOKEN_ROUTES = {
    "images": {"scene": 2, "host": "imagex.bytedanceapi.com", "space": "tb4s082cfz"},
    "videos": {"scene": 1, "host": "vod.bytedanceapi.com", "space": "dreamina"},
    "audios": {"scene": 1, "host": "vod.bytedanceapi.com", "space": "dreamina"},
}
# Browser and Python Apply responses rotate among tos-lf-x.snssdk.com,
# tos-d-x-lf.bytedancevod.com and tos-d-lq.bytedancevod.com. Only this bounded
# TOS label family under the two observed registered domains is accepted.
# There may be one to four ASCII alphanumeric segments, each 1–12 characters.
_STORAGE_HOST_PATTERN = re.compile(
    r"tos-[a-z0-9]{1,12}(?:-[a-z0-9]{1,12}){0,3}\.(?:snssdk\.com|bytedancevod\.com)", re.ASCII)
_STORE_PREFIXES = {"images": "tos-cn-i-tb4s082cfz/", "videos": "tos-cn-v-148450/", "audios": "tos-cn-v-148450/"}
SUPPORTED_UPLOAD_KINDS = frozenset(_KINDS)


def supports_upload_kind(kind: str) -> bool:
    return kind in SUPPORTED_UPLOAD_KINDS


def _validate_token(token: Any, kind: str) -> dict[str, str]:
    route = _TOKEN_ROUTES.get(kind)
    if route is None:
        raise UploadError("Jimeng reference upload scene is unavailable")
    if (not isinstance(token, dict) or token.get("region") != "cn"
            or token.get("upload_domain") != route["host"] or token.get("space_name") != route["space"]):
        raise UploadError("Jimeng upload token domain, region or space is unsupported")
    for field in ("access_key_id", "secret_access_key", "session_token"):
        value = token.get(field)
        if (not isinstance(value, str) or not value or len(value) > 16384
                or any(ord(character) < 32 or ord(character) == 127 for character in value)):
            raise UploadError("Jimeng upload token is incomplete")
    return {field: token[field] for field in (
        "access_key_id", "secret_access_key", "session_token", "region", "upload_domain", "space_name")}


def _upload_token(post: Callable[..., dict[str, Any]], kind: str) -> dict[str, str]:
    route = _TOKEN_ROUTES.get(kind)
    if route is None:
        raise UploadError("Jimeng reference upload scene is unavailable")
    return _validate_token(post("/mweb/v1/get_upload_token", {"scene": route["scene"]}), kind)


def _session(proxy_url: str) -> requests.Session:
    session = requests.Session()
    session.trust_env = False
    if proxy_url:
        parsed = urlsplit(proxy_url)
        try:
            valid = (parsed.scheme in {"http", "https", "socks5", "socks5h"}
                     and parsed.hostname and parsed.port and not parsed.query and not parsed.fragment)
        except ValueError:
            valid = False
        if not valid:
            session.close()
            raise UploadError("Jimeng account proxy is invalid")
        proxy = proxy_url.replace("socks5://", "socks5h://", 1)
        session.proxies = {"http": proxy, "https": proxy}
    return session


def _public_source_url(source: str) -> tuple[str, str]:
    """Resolve each direct media hop once, rejecting every private address."""
    try:
        parsed = urlsplit(source)
        host, port = parsed.hostname, parsed.port
        if (parsed.scheme not in {"http", "https"} or not host or parsed.username or parsed.password
                or parsed.fragment or port not in {None, 80, 443}
                or host == "localhost" or host.endswith((".localhost", ".local", ".internal"))):
            raise ValueError
        addresses = socket.getaddrinfo(host, port or (443 if parsed.scheme == "https" else 80),
                                      type=socket.SOCK_STREAM)
        if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global
                                or ipaddress.ip_address(item[4][0]).is_multicast for item in addresses):
            raise ValueError
        return host, addresses[0][4][0]
    except (ValueError, OSError):
        raise MediaError("Jimeng direct media URL must resolve to a public address", category="invalid_source") from None


class _PinnedSourceAdapter(requests.adapters.HTTPAdapter):
    """Connect to the checked public IP while verifying the original TLS host."""
    def __init__(self, host: str, address: str, *, allow_proxy: bool = False):
        super().__init__()
        self.host, self.address = host, address
        self.allow_proxy = allow_proxy

    def get_connection_with_tls_context(self, request, verify, proxies=None, cert=None):
        if urlsplit(request.url).hostname != self.host or proxies and not self.allow_proxy:
            raise MediaError("Jimeng direct media route changed", category="invalid_source")
        builder = getattr(self, "build_connection_pool_key_attributes", None)
        if callable(builder):
            host_params, pool_kwargs = builder(request, verify, cert)
        else:  # Requests 2.32.0/2.32.1 use the older adapter connection API.
            parsed = urlsplit(request.url)
            host_params = {"scheme": parsed.scheme, "host": self.host, "port": parsed.port}
            pool_kwargs = {}
        host_params["host"] = self.address
        if host_params["scheme"] == "https":
            pool_kwargs.update(server_hostname=self.host, assert_hostname=self.host)
        request.headers["Host"] = urlsplit(request.url).netloc
        proxy = requests.utils.select_proxy(request.url, proxies or {})
        manager = self.proxy_manager_for(proxy) if proxy else self.poolmanager
        return manager.connection_from_host(**host_params, pool_kwargs=pool_kwargs)

    def get_connection(self, url, proxies=None):
        request = requests.Request("GET", url).prepare()
        return self.get_connection_with_tls_context(request, True, proxies=proxies)

    def _get_connection(self, request, verify, proxies=None, cert=None):
        return self.get_connection_with_tls_context(request, verify, proxies=proxies, cert=cert)

    def add_headers(self, request, **kwargs):
        if urlsplit(request.url).hostname != self.host:
            raise MediaError("Jimeng direct media route changed", category="invalid_source")
        request.headers["Host"] = urlsplit(request.url).netloc


def _download_source(source: str, kind: str, proxy_url: str, *,
                     deadline: float) -> tuple[bytes, str, str]:
    if source.startswith("data:") or proxy_url:
        return _download(source, kind, proxy_url, allow_direct_fallback=False, deadline=deadline)
    current_source = source
    for redirects in range(4):
        host, address = _public_source_url(current_source)
        _media_remaining(deadline)
        with _session("") as session:
            session.mount(urlsplit(current_source).scheme + "://", _PinnedSourceAdapter(host, address))
            try:
                remaining = _media_remaining(deadline)
                with session.get(current_source, timeout=(min(10, remaining), min(45, remaining)),
                                 stream=True, allow_redirects=False,
                                 headers={"User-Agent": "Mozilla/5.0", "Accept-Encoding": "identity"}) as response:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        location = response.headers.get("Location")
                        if redirects == 3 or not isinstance(location, str) or not location.strip():
                            raise MediaError("Jimeng direct media redirect limit or location is invalid", category="redirect")
                        current_source = urljoin(current_source, location)
                        continue  # The next hop gets its own DNS check and pinned connection.
                    if 300 <= response.status_code < 400:
                        raise MediaError("Jimeng direct media redirect is unsupported", category="redirect")
                    response.raise_for_status()
                    length = response.headers.get("Content-Length", "")
                    if length.isdigit() and int(length) > MAX_MEDIA_BYTES:
                        raise MediaError("reference media exceeds 200 MB", category="too_large")
                    chunks, total = [], 0
                    for chunk in _deadline_chunks(response, deadline):
                        total += len(chunk)
                        if total > MAX_MEDIA_BYTES:
                            raise MediaError("reference media exceeds 200 MB", category="too_large")
                        chunks.append(chunk)
                    contents = b"".join(chunks)
                    mime = response.headers.get("Content-Type", "application/octet-stream").split(";", 1)[0].lower()
                    break
            except requests.RequestException as exc:
                raise MediaError("Jimeng direct media download failed", category="transport") from exc
    if not contents:
        raise MediaError("reference media is empty", category="empty")
    filename = Path(urlsplit(current_source).path).name or Path(urlsplit(source).path).name or kind + ".bin"
    expected = {"images": "image/", "videos": "video/", "audios": "audio/"}[kind]
    if not mime.startswith(expected):
        mime = mimetypes.guess_type(filename)[0] or ""
        if not mime.startswith(expected):
            raise MediaError("Jimeng reference has the wrong content type", category="content_type")
    _media_remaining(deadline)
    return contents, mime, filename


def _request_json(session: requests.Session, method: str, url: str, *, headers: dict[str, str],
                  data: bytes | None = None, deadline: float) -> dict[str, Any]:
    remaining = _media_remaining(deadline)
    try:
        response = session.request(method, url, headers=headers, data=data, allow_redirects=False,
                                   timeout=(min(15, remaining), min(60, remaining)))
        try:
            if 300 <= response.status_code < 400:
                raise UploadError("Jimeng upload redirects are not allowed")
            response.raise_for_status()
            result = response.json()
        finally:
            response.close()
    except requests.RequestException:
        raise UploadError("Jimeng upload transport failed") from None
    except ValueError:
        raise UploadError("Jimeng upload returned invalid JSON") from None
    _media_remaining(deadline)
    if not isinstance(result, dict) or (result.get("ResponseMetadata") or {}).get("Error"):
        raise UploadError("Jimeng upload API rejected the request")
    return result


def _api_call(session: requests.Session, method: str, query: dict[str, Any], token: dict[str, str],
              *, image: bool, payload: dict[str, Any] | None = None, deadline: float) -> dict[str, Any]:
    # The token was checked against this fixed CN route before signing. No
    # credential can be sent to a URL chosen by a caller or upload response.
    host = "imagex.bytedanceapi.com" if image else "vod.bytedanceapi.com"
    service = "imagex" if image else "vod"
    url = "https://" + host + "/?" + urlencode(query)
    body = json.dumps(payload, separators=(",", ":")).encode() if payload is not None else b""
    headers = _sign(method, url, access_key=token["access_key_id"], secret_key=token["secret_access_key"],
                    session_token=token["session_token"], region=CN_SIGNING_REGION, service=service, payload=body)
    headers.update({"accept": "*/*", "origin": "https://jimeng.jianying.com",
                    "referer": "https://jimeng.jianying.com/ai-tool/generate"})
    if payload is not None:
        headers["content-type"] = "application/json"
    result = _request_json(session, method, url, headers=headers,
                           data=body if payload is not None else None, deadline=deadline)
    meta = result.get("ResponseMetadata") or {}
    if (meta.get("Region") != CN_SIGNING_REGION or meta.get("Service") != service
            or meta.get("Action") != query["Action"]):
        raise UploadError("Jimeng upload response changed its CN API binding")
    return result


def _field(value: Any, label: str) -> str:
    if (not isinstance(value, str) or not value or len(value) > 16384
            or any(ord(character) < 32 or ord(character) == 127 for character in value)):
        raise UploadError("Jimeng upload address has an invalid " + label)
    return value


def _validate_blob_uri(uri: Any, kind: str) -> str:
    if kind not in _STORE_PREFIXES:
        raise UploadError("Jimeng reference upload contract is unavailable")
    if (not isinstance(uri, str) or not re.fullmatch(r"[A-Za-z0-9._/-]{1,4096}", uri)
            or uri.startswith("/") or any(part in {"", ".", ".."} for part in uri.split("/"))):
        raise UploadError("Jimeng upload blob URI is invalid")
    if not uri.startswith(_STORE_PREFIXES[kind]):
        raise UploadError("Jimeng upload blob URI changed its CN storage namespace")
    return uri


def _validate_blob_host(host: Any) -> str:
    if not isinstance(host, str) or not _STORAGE_HOST_PATTERN.fullmatch(host):
        raise UploadError("Jimeng upload blob host is unsupported")
    return host


def _blob_url(host: Any, uri: Any, kind: str) -> str:
    uri = _validate_blob_uri(uri, kind)
    host = _validate_blob_host(host)
    return "https://" + host + "/upload/v1/" + uri


def _select_blob_route(hosts: Any, uri: Any, kind: str) -> tuple[str, str, str]:
    """Select one validated host from the same native Apply upload group."""
    uri = _validate_blob_uri(uri, kind)
    if not isinstance(hosts, list) or not hosts:
        raise UploadError("Jimeng upload blob host is unsupported")
    known = False
    for host in hosts:
        try:
            url = _blob_url(host, uri, kind)
        except UploadError:
            continue  # Unknown names are never resolved or contacted.
        known = True
        try:
            verified_host, address = _public_source_url(url)
        except MediaError:
            continue
        if verified_host == host:
            return url, verified_host, address
    if known:
        raise UploadError("Jimeng upload storage host did not resolve to a public address")
    raise UploadError("Jimeng upload blob host is unsupported")


def _upload_blob(session: requests.Session, url: str, auth: str, contents: bytes, *,
                 user_id: str, filename: str, deadline: float) -> None:
    headers = {"Authorization": auth, "Content-CRC32": f"{zlib.crc32(contents) & 0xffffffff:08x}",
               "Content-Type": "application/octet-stream",
               "Content-Disposition": 'attachment; filename="' + quote(filename, safe=";/?:@&=+$,#-_.!~*'()") + '"',
               "X-Storage-U": quote(user_id, safe="-_.!~*'()"),
               "Origin": "https://jimeng.jianying.com",
               "Referer": "https://jimeng.jianying.com/ai-tool/generate"}
    result = _request_json(session, "POST", url, headers=headers, data=contents, deadline=deadline)
    if result.get("code") not in (2000, "2000"):
        raise UploadError("Jimeng file upload was rejected")


def _probe_reference(contents: bytes, filename: str) -> dict[str, float]:
    suffix = Path(filename).suffix
    if not re.fullmatch(r"\.[A-Za-z0-9]{1,8}", suffix):
        suffix = ".bin"
    with tempfile.TemporaryDirectory(prefix="ijm-cn-probe-") as folder:
        path = Path(folder) / ("reference" + suffix)
        path.write_bytes(contents)
        try:
            process = subprocess.run(["ffprobe", "-v", "error", "-protocol_whitelist", "file,pipe",
                "-show_entries", "format=duration:stream=width,height,r_frame_rate,codec_type",
                "-of", "json", str(path)], capture_output=True, text=True, timeout=30, check=True)
            result = json.loads(process.stdout)
            duration = float((result.get("format") or {}).get("duration") or 0)
            video = next((item for item in result.get("streams", []) if item.get("codec_type") == "video"), {})
            fps = float(Fraction(video.get("r_frame_rate") or "0/1"))
            metadata = {"duration": duration, "width": int(video.get("width") or 0),
                        "height": int(video.get("height") or 0), "fps": fps}
            if duration <= 0 or any(not math.isfinite(value) or value < 0 for value in metadata.values()):
                raise ValueError
            return metadata
        except (OSError, subprocess.SubprocessError, TypeError, ValueError, ZeroDivisionError):
            raise UploadError("Jimeng reference media metadata could not be verified") from None


def upload_media_bytes(contents: bytes, kind: str, *, account: dict[str, Any],
                       post: Callable[..., dict[str, Any]], filename: str = "upload",
                       deadline: float | None = None) -> dict[str, Any]:
    if account.get("execution_mode") != "jimeng_web":
        raise UploadError("Jimeng uploads require the account's Jimeng web transport")
    if kind not in _KINDS:
        raise UploadError("Jimeng reference upload contract is unavailable")
    if not isinstance(contents, bytes) or not contents or len(contents) > MAX_MEDIA_BYTES:
        raise UploadError("Jimeng reference media must contain at most 200 MB")
    deadline = deadline if deadline is not None else time.monotonic() + 300
    user_id = _field(account.get("login_user_id"), "account identity")
    image = kind == "images"
    if image:
        try:
            with Image.open(io.BytesIO(contents)) as picture:
                width, height = picture.size
        except (OSError, ValueError, Image.DecompressionBombError):
            raise UploadError("Jimeng reference image is invalid") from None
    else:
        metadata = _probe_reference(contents, filename)
    token = _upload_token(post, kind)
    with _session(str(account.get("proxy_url") or "")) as session:
        apply = {"Action": "ApplyImageUpload" if image else "ApplyUploadInner",
                 "Version": "2018-08-01" if image else "2020-11-19",
                 "ServiceId" if image else "SpaceName": token["space_name"],
                 "FileSize": len(contents), "s": secrets.token_hex(6)}
        if not image:
            apply.update(FileType="video", IsInner="1")
        result = _api_call(session, "GET", apply, token, image=image, deadline=deadline).get("Result") or {}
        if image:
            node = result.get("UploadAddress") or {}
            hosts = node.get("UploadHosts")
        else:
            node = ((result.get("InnerUploadAddress") or {}).get("UploadNodes") or [None])[0] or {}
            hosts = [node.get("UploadHost")]
        store = (node.get("StoreInfos") or [None])[0] or {}
        uri, auth = _field(store.get("StoreUri"), "store URI"), _field(store.get("Auth"), "authorization")
        session_key = _field(node.get("SessionKey"), "session key")
        if node.get("UploadHeader") or store.get("UploadHeader") or store.get("StorageHeader"):
            raise UploadError("Jimeng upload address requires unsupported storage headers")
        url, host, address = _select_blob_route(hosts, uri, kind)
        # Keep TLS certificate/SNI checks on the native host, but connect only
        # to the public IP verified above, including through a configured proxy.
        session.mount("https://" + host + "/", _PinnedSourceAdapter(host, address, allow_proxy=True))
        _upload_blob(session, url, auth, contents, user_id=user_id, filename=filename, deadline=deadline)
        commit = {"Action": "CommitImageUpload" if image else "CommitUploadInner",
                  "Version": apply["Version"], "ServiceId" if image else "SpaceName": token["space_name"]}
        body = {"SessionKey": session_key}
        if not image:
            body["Functions"] = []
        result = _api_call(session, "POST", commit, token, image=image, payload=body, deadline=deadline).get("Result") or {}
    item = (result.get("Results") or [None])[0] or {}
    if image:
        plugin = (result.get("PluginResult") or [None])[0] or {}
        image_uri = plugin.get("ImageUri") or item.get("Uri")
        if (item.get("UriStatus") not in (2000, "2000") or item.get("Uri") != uri or image_uri != uri
                or plugin.get("SourceUri", uri) != uri):
            raise UploadError("Jimeng image commit did not confirm a valid URI")
        for key, fallback in (("ImageWidth", width), ("ImageHeight", height)):
            value = plugin.get(key, fallback)
            if type(value) is not int or value <= 0:
                raise UploadError("Jimeng image commit returned invalid dimensions")
            if key == "ImageWidth":
                width = value
            else:
                height = value
        return {"image_uri": image_uri, "width": width, "height": height, "filename": filename}
    vid = _field(item.get("Vid"), "video ID")
    if vid != node.get("Vid"):
        raise UploadError("Jimeng VOD commit changed the originally allocated video ID")
    upstream = item.get("VideoMeta") or {}
    values = dict(metadata)
    for target, source in (("duration", "Duration"), ("width", "Width"), ("height", "Height"), ("fps", "Fps")):
        raw = upstream.get(source)
        if raw is None:
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError):
            raise UploadError("Jimeng VOD commit returned invalid media metadata") from None
        if (isinstance(raw, bool) or not math.isfinite(value) or value < 0 or target == "duration" and value == 0
                or target in {"width", "height"} and not value.is_integer()):
            raise UploadError("Jimeng VOD commit returned invalid media metadata")
        values[target] = value
    return {"vid": vid, "duration": values["duration"],
            "width": int(values["width"]) if kind == "videos" else 0,
            "height": int(values["height"]) if kind == "videos" else 0,
            "fps": round(values["fps"], 3) if kind == "videos" else 0, "filename": filename}


class JimengUploader:
    def __init__(self, account: dict[str, Any], *, post: Callable[..., dict[str, Any]], store: Any = None):
        if account.get("execution_mode") != "jimeng_web":
            raise UploadError("Jimeng uploads require the account's Jimeng web transport")
        account_id = account.get("id")
        if isinstance(account_id, bool) or not isinstance(account_id, int) or account_id < 1:
            raise UploadError("Jimeng uploads require a valid account")
        self.account = dict(account)
        self.post = post
        self.store = store

    def upload_inputs(self, normalized: dict[str, Any], *,
                      deadline: float | None = None) -> dict[str, list[dict[str, Any]]]:
        deadline = deadline if deadline is not None else time.monotonic() + 300
        sources = {kind: normalized.get(kind) or [] for kind in _KINDS}
        # Validate the complete request before uploading the first reference.
        if any(not isinstance(items, list) or any(not isinstance(source, str)
                or not source.startswith(("data:", "http://", "https://")) for source in items)
               for items in sources.values()):
            raise MediaError("Jimeng references require HTTP, HTTPS or data URLs", category="invalid_source")
        account_id = self.account["id"]
        with _UPLOAD_LOCKS_GUARD:
            lock = _UPLOAD_LOCKS.setdefault(account_id, threading.RLock())
        result: dict[str, list[dict[str, Any]]] = {kind: [] for kind in _KINDS}
        for kind in _KINDS:
            for source in sources[kind]:
                contents, _, filename = _download_source(source, kind,
                    str(self.account.get("proxy_url") or ""), deadline=deadline)
                identity = str(self.account.get("login_user_id") or "").encode()
                digest = hashlib.sha256(identity + b"\0" + contents).hexdigest()
                with lock:
                    cached = (self.store.media_cache_get(account_id, kind, digest, backend="jimeng_web")
                              if self.store else None)
                    if cached:
                        result[kind].append(cached)
                        continue
                    uploaded = upload_media_bytes(contents, kind, account=self.account, post=self.post,
                                                  filename=filename, deadline=deadline)
                    if self.store:
                        self.store.media_cache_put(account_id, kind, digest, uploaded, backend="jimeng_web")
                    result[kind].append(uploaded)
        return result
