"""Download caller media and upload it through an authenticated Dreamina browser."""

from __future__ import annotations

import base64
import hashlib
import ipaddress
import mimetypes
import struct
import tempfile
import threading
import time
import uuid
import zlib
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import requests
from urllib3.exceptions import HTTPError as Urllib3HTTPError
from urllib3.response import _get_decoder
from urllib3.util import Timeout

from .browser import BrowserError, BrowserRejected, CDPBrowser


MAX_MEDIA_BYTES = 200 * 1024 * 1024
MIME_EXTENSIONS = {"audio/mpeg": ".mp3", "audio/wav": ".wav", "image/jpeg": ".jpg",
                   "image/png": ".png", "video/mp4": ".mp4", "video/quicktime": ".mov"}
_MEDIA_ROUTE_LOCK = threading.RLock()
_DIRECT_MEDIA_UNTIL: dict[str, float] = {}
# This caller-owned public media host consistently fails TLS through the
# account proxy on pre, while its direct route succeeds. Dreamina hosts never
# enter this list and always use the account proxy.
_DIRECT_FIRST_MEDIA_HOSTS = frozenset({"media.juxingdonghua.com"})
_MEDIA_ERROR_CATEGORIES = frozenset({"unknown", "invalid_source", "proxy_required", "deadline",
    "transport", "connection", "proxy", "tls", "timeout", "http", "redirect", "decode",
    "unsupported_encoding", "unbounded_stream", "too_large", "empty", "content_type"})
_MEDIA_EXCEPTION_TYPES = frozenset({"MediaError", "Exception", "ConnectionError", "ProxyError",
    "SSLError", "Timeout", "ConnectTimeout", "ReadTimeout", "ChunkedEncodingError", "HTTPError",
    "RequestException", "ProtocolError", "ReadTimeoutError", "ConnectTimeoutError", "MaxRetryError",
    "NewConnectionError", "NameResolutionError", "SSLCertVerificationError", "SSLEOFError",
    "IncompleteRead", "DecodeError", "ContentDecodingError", "ValueError", "Error"})


def _safe_media_fields(fields: dict) -> dict:
    """Diagnostic values are enums/numbers, never exception text or URLs."""
    category = fields.get("category")
    result = {"category": category if isinstance(category, str) and category in _MEDIA_ERROR_CATEGORIES else "unknown"}
    for key, lower, upper in (("http_status", 100, 599), ("attempt_count", 0, 4), ("media_index", 1, 10000)):
        value = fields.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and lower <= value <= upper:
            result[key] = value
    if "attempt_count" in result:
        result["retry_count"] = max(0, result["attempt_count"] - 1)
    for key, allowed in (("route", {"account_proxy", "direct_media"}),
                         ("media_kind", {"images", "videos", "audios"})):
        if isinstance(fields.get(key), str) and fields[key] in allowed:
            result[key] = fields[key]
    return result


class MediaError(Exception):
    def __init__(self, message: str, *, category: str = "unknown", **diagnostic) -> None:
        super().__init__(message)
        self.diagnostic = _safe_media_fields({**diagnostic, "category": category})


def media_error_diagnostic(error: MediaError) -> dict:
    """Retain only a bounded exception-type chain; do not inspect its args."""
    fields = getattr(error, "diagnostic", {})
    result = _safe_media_fields(fields if isinstance(fields, dict) else {})
    types, seen = [], set()
    current = error
    while isinstance(current, BaseException) and len(types) < 4 and id(current) not in seen:
        seen.add(id(current))
        name = type(current).__name__
        types.append(name if name in _MEDIA_EXCEPTION_TYPES else "Exception")
        current = current.__cause__ or current.__context__
    result["exception_types"] = types
    return result


def _media_transport_category(error: BaseException) -> str:
    for error_type, category in ((requests.exceptions.ProxyError, "proxy"),
            (requests.exceptions.SSLError, "tls"), (requests.exceptions.Timeout, "timeout"),
            (requests.exceptions.ConnectionError, "connection")):
        if isinstance(error, error_type):
            return category
    return "transport"


def _unique_image_copy(data: bytes, mime: str) -> bytes | None:
    """Make a visually identical upload copy when a prior asset lacks a cache ID."""
    marker = b"ijm2api-upload\x00" + uuid.uuid4().hex.encode("ascii")
    if mime == "image/png" and data.startswith(b"\x89PNG\r\n\x1a\n"):
        position = 8
        while position + 12 <= len(data):
            length = struct.unpack_from(">I", data, position)[0]
            end = position + 12 + length
            if end > len(data):
                return None
            if data[position + 4:position + 8] == b"IEND":
                if position + 12 != len(data) or length:
                    return None
                chunk = (struct.pack(">I", len(marker)) + b"tEXt" + marker
                         + struct.pack(">I", zlib.crc32(b"tEXt" + marker) & 0xffffffff))
                return data[:position] + chunk + data[position:]
            position = end
    if mime == "image/jpeg" and data.startswith(b"\xff\xd8"):
        comment = b"ijm2api-upload:" + uuid.uuid4().hex.encode("ascii")
        return data[:2] + b"\xff\xfe" + struct.pack(">H", len(comment) + 2) + comment + data[2:]
    return None


def _unique_mp4_copy(data: bytes, mime: str) -> bytes | None:
    """Change the file identity without changing MP4 video or audio samples."""
    if mime not in {"video/mp4", "video/quicktime"}:
        return None
    position = 0
    found_ftyp = False
    found_mdat = False
    while position + 8 <= len(data):
        size, kind = struct.unpack_from(">I4s", data, position)
        header_size = 8
        if size == 1:
            if position + 16 > len(data):
                return None
            size = struct.unpack_from(">Q", data, position + 8)[0]
            header_size = 16
        if size < header_size or position + size > len(data):
            return None
        found_ftyp |= kind == b"ftyp"
        found_mdat |= kind == b"mdat"
        position += size
    if position != len(data) or not found_ftyp or not found_mdat:
        return None
    marker = b"ijm2api-upload-" + uuid.uuid4().hex.encode("ascii")
    return data + struct.pack(">I4s", 8 + len(marker), b"free") + marker


def _allow_direct_media_fallback(host: str | None) -> bool:
    """Keep browser/upstream and local URLs on the account proxy."""
    if not host or host == "dreamina.capcut.com" or host.endswith((".capcut.com", ".capcutapi.com", ".bytedanceapi.com", ".bytetos.com", ".vodupload.com")):
        return False
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
        return False
    try:
        return ipaddress.ip_address(host).is_global
    except ValueError:
        return "." in host


def _media_remaining(deadline: float | None, *, diagnostic: dict | None = None) -> float | None:
    if deadline is None:
        return None
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise MediaError("reference media download deadline exceeded", category="deadline", **(diagnostic or {}))
    return remaining


def _deadline_chunks(response: requests.Response, deadline: float):
    """Read one available socket buffer, rather than waiting to fill a MiB.

    A stream that continuously drips bytes must not renew the task budget.
    Limit each actual socket read to the remaining time, and check it again
    after the read. The normal browser download path remains unchanged.
    """
    raw = response.raw
    connection = getattr(raw, '_connection', None)
    stream_socket = getattr(connection, 'sock', None)
    if stream_socket is None:
        file_pointer = getattr(getattr(raw, '_fp', None), 'fp', None)
        stream_socket = getattr(getattr(file_pointer, 'raw', None), '_sock', None)
    encoding = response.headers.get('Content-Encoding', '').lower().strip()
    if encoding not in {'', 'identity', 'gzip', 'x-gzip', 'deflate'}:
        raise MediaError('reference server ignored identity encoding; bounded download requires gzip, deflate or identity',
                         category="unsupported_encoding")
    # Reuse urllib3's established content decoding: it handles concatenated
    # gzip members and both zlib-wrapped and raw DEFLATE just like requests.
    # Its private factory is covered by real-response compatibility tests and
    # the explicitly bounded urllib3 2.x dependency. Keep decoding outside
    # read1(), whose decode loop could otherwise perform multiple socket reads
    # before the task's remaining budget can be checked again.
    decoder = _get_decoder(encoding) if encoding in {'gzip', 'x-gzip', 'deflate'} else None
    if not callable(getattr(raw, 'read1', None)) or stream_socket is None and not raw.closed:
        raise MediaError('reference response does not support a bounded streaming read', category="unbounded_stream")
    while True:
        remaining = _media_remaining(deadline)
        # read1() closes the underlying socket immediately after consuming a
        # Content-Length response. Do not touch that saved socket on the next
        # iteration, even though the final data chunk was nonempty.
        if raw.closed:
            break
        if stream_socket is not None:
            stream_socket.settimeout(min(45, remaining))
        chunk = raw.read1(64 * 1024, decode_content=False)
        _media_remaining(deadline)
        if not chunk:
            break
        if decoder:
            try:
                chunk = decoder.decompress(chunk)
            except zlib.error as exc:
                raise MediaError('reference media content decoding failed', category="decode") from exc
        _media_remaining(deadline)
        yield chunk
    if decoder:
        try:
            chunk = decoder.flush()
        except zlib.error as exc:
            raise MediaError('reference media content decoding failed', category="decode") from exc
        _media_remaining(deadline)
        yield chunk


def _download(source: str, kind: str, proxy_url: str = "", *,
              allow_direct_fallback: bool = True, deadline: float | None = None) -> tuple[bytes, str, str]:
    diagnostic = {"media_kind": kind, "attempt_count": 0}
    _media_remaining(deadline, diagnostic=diagnostic)
    if source.startswith("data:"):
        header, separator, payload = source.partition(",")
        if not separator or ";base64" not in header:
            raise MediaError("media data URL must be base64 encoded", category="invalid_source", **diagnostic)
        mime = header[5:].split(";", 1)[0].lower()
        try:
            data = base64.b64decode(payload, validate=True)
        except ValueError as exc:
            raise MediaError("media data URL contains invalid base64", category="invalid_source", **diagnostic) from exc
        filename = kind + MIME_EXTENSIONS.get(mime, mimetypes.guess_extension(mime) or ".bin")
    elif source.startswith(("https://", "http://")):
        parsed = urlsplit(source)
        if not parsed.hostname or parsed.username or parsed.password:
            raise MediaError("media URL is invalid", category="invalid_source", **diagnostic)
        if not proxy_url:
            raise MediaError("account proxy is required for media download", category="proxy_required", **diagnostic)
        # Requests' socks5h form resolves the destination through the same
        # account proxy that Chromium uses for Dreamina traffic.
        proxy = proxy_url.replace("socks5://", "socks5h://", 1)
        account_route = {"http": proxy, "https": proxy}
        routes = [("account proxy", account_route), ("account proxy", account_route)]
        # Caller-owned public media can use a direct route if the account proxy
        # repeatedly fails; Dreamina and local hosts never use that route.
        direct_allowed = allow_direct_fallback and _allow_direct_media_fallback(parsed.hostname)
        direct_first = False
        if direct_allowed:
            with _MEDIA_ROUTE_LOCK:
                direct_first = (parsed.hostname in _DIRECT_FIRST_MEDIA_HOSTS or
                                _DIRECT_MEDIA_UNTIL.get(parsed.hostname, 0) > time.monotonic())
            direct_route = ("direct media fallback", {})
            # A short direct-route interruption must not leave a caller-owned
            # media host at the mercy of a consistently timing-out proxy.
            routes = ([direct_route] + routes + [direct_route]) if direct_first else (routes + [direct_route])
        for index, (route_name, route_proxy) in enumerate(routes):
            remaining = _media_remaining(deadline, diagnostic=diagnostic)
            session = requests.Session()
            session.trust_env = False
            diagnostic.update(attempt_count=index + 1, route="account_proxy" if route_proxy else "direct_media")
            diagnostic.pop("http_status", None)
            try:
                timeout = ((10, 45) if route_proxy else (10, 60)) if remaining is None else Timeout(
                    total=remaining, connect=min(10, remaining), read=min(45, remaining))
                headers = {"User-Agent": "Mozilla/5.0"}
                if deadline is not None:
                    headers['Accept-Encoding'] = 'identity'
                with session.get(source, timeout=timeout, stream=True,
                                 proxies=route_proxy,
                                 allow_redirects=bool(route_proxy),
                                 headers=headers) as response:
                    diagnostic["http_status"] = response.status_code
                    _media_remaining(deadline, diagnostic=diagnostic)
                    if not route_proxy and 300 <= response.status_code < 400:
                        raise MediaError("direct media fallback redirected; account proxy is required",
                                         category="redirect", **diagnostic)
                    response.raise_for_status()
                    chunks = []
                    total = 0
                    chunks_source = (response.iter_content(1024 * 1024) if deadline is None
                                     else _deadline_chunks(response, deadline))
                    for chunk in chunks_source:
                        total += len(chunk)
                        if total > MAX_MEDIA_BYTES:
                            raise MediaError("reference media exceeds 200 MB", category="too_large", **diagnostic)
                        chunks.append(chunk)
                    data = b"".join(chunks)
                    mime = response.headers.get("Content-Type", "application/octet-stream").split(";", 1)[0].lower()
                    filename = Path(parsed.path).name or kind + MIME_EXTENSIONS.get(mime, ".bin")
                    if direct_allowed:
                        with _MEDIA_ROUTE_LOCK:
                            if route_proxy:
                                _DIRECT_MEDIA_UNTIL.pop(parsed.hostname, None)
                            else:
                                _DIRECT_MEDIA_UNTIL[parsed.hostname] = time.monotonic() + 120
                    break
            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout, Urllib3HTTPError,
                    requests.exceptions.ChunkedEncodingError) as exc:
                if direct_allowed and not route_proxy:
                    with _MEDIA_ROUTE_LOCK:
                        _DIRECT_MEDIA_UNTIL.pop(parsed.hostname, None)
                if index == len(routes) - 1:
                    raise MediaError(f"media download failed via {route_name}: {type(exc).__name__}",
                                     category=_media_transport_category(exc), **diagnostic) from exc
                delay = 0.5 * (index + 1)
                time.sleep(delay if deadline is None else min(delay, _media_remaining(deadline, diagnostic=diagnostic)))
            except requests.exceptions.HTTPError as exc:
                status = exc.response.status_code if exc.response is not None else 0
                if direct_allowed and not route_proxy and index < len(routes) - 1:
                    with _MEDIA_ROUTE_LOCK:
                        _DIRECT_MEDIA_UNTIL.pop(parsed.hostname, None)
                    continue
                if status not in {429, 500, 502, 503, 504} or index == len(routes) - 1:
                    raise MediaError(f"media download failed via {route_name}: HTTP {status}",
                                     category="http", **{**diagnostic, "http_status": status}) from exc
                delay = 0.5 * (index + 1)
                time.sleep(delay if deadline is None else min(delay, _media_remaining(deadline, diagnostic=diagnostic)))
            except requests.RequestException as exc:
                if direct_allowed and not route_proxy and index < len(routes) - 1:
                    with _MEDIA_ROUTE_LOCK:
                        _DIRECT_MEDIA_UNTIL.pop(parsed.hostname, None)
                    continue
                raise MediaError(f"media download failed via {route_name}: {type(exc).__name__}",
                                 category=_media_transport_category(exc), **diagnostic) from exc
            except MediaError as exc:
                exc.diagnostic = _safe_media_fields({**diagnostic, **exc.diagnostic})
                if direct_first and index < len(routes) - 1 and "direct media fallback redirected" in str(exc):
                    with _MEDIA_ROUTE_LOCK:
                        _DIRECT_MEDIA_UNTIL.pop(parsed.hostname, None)
                    continue
                raise
            finally:
                session.close()
    else:
        raise MediaError("media must be an HTTPS URL, HTTP URL, data URL, or Dreamina asset ID",
                         category="invalid_source", **diagnostic)
    if not data:
        raise MediaError("reference media is empty", category="empty", **diagnostic)
    if len(data) > MAX_MEDIA_BYTES:
        raise MediaError("reference media exceeds 200 MB", category="too_large", **diagnostic)
    if not mime.startswith({"images": "image/", "videos": "video/", "audios": "audio/"}[kind]):
        guessed = mimetypes.guess_type(filename)[0] or ""
        if not guessed.startswith({"images": "image/", "videos": "video/", "audios": "audio/"}[kind]):
            raise MediaError(f"{kind} reference has the wrong content type", category="content_type", **diagnostic)
        mime = guessed
    _media_remaining(deadline, diagnostic=diagnostic)
    return data, mime, filename
