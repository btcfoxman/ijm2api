"""Dreamina ImageX and VOD uploads using account-scoped proxy transport."""

from __future__ import annotations

import hashlib
import hmac
import io
import json
import secrets
import subprocess
import tempfile
import time
import urllib.parse
import zlib
from pathlib import Path
from typing import Any

import requests
from PIL import Image

from .browser import CDPBrowser


class UploadError(Exception):
    pass


def _session(proxy_url: str) -> requests.Session:
    proxy = proxy_url.replace("socks5://", "socks5h://", 1)
    session = requests.Session()
    session.trust_env = False
    session.proxies = {"http": proxy, "https": proxy}
    return session


def _sign(method: str, url: str, *, access_key: str, secret_key: str,
          session_token: str, region: str, service: str,
          payload: bytes = b"") -> dict[str, str]:
    """Construct a standard AWS4 HMAC SHA256 signature for Volcengine APIs."""
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    day = stamp[:8]
    digest = hashlib.sha256(payload).hexdigest() if method == "POST" else hashlib.sha256(b"").hexdigest()
    signed = {"x-amz-date": stamp, "x-amz-security-token": session_token}
    if method == "POST":
        signed["x-amz-content-sha256"] = digest
    parts = urllib.parse.urlsplit(url)
    query = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    canonical_query = urllib.parse.urlencode(sorted(query), quote_via=urllib.parse.quote)
    canonical_headers = "".join(f"{key}:{value.strip()}\n" for key, value in sorted(signed.items()))
    signed_names = ";".join(sorted(signed))
    canonical = "\n".join((method, parts.path or "/", canonical_query,
                            canonical_headers, signed_names, digest))
    scope = f"{day}/{region}/{service}/aws4_request"
    string_to_sign = "\n".join(("AWS4-HMAC-SHA256", stamp, scope,
                                 hashlib.sha256(canonical.encode()).hexdigest()))

    def derive(key: bytes, item: str) -> bytes:
        return hmac.new(key, item.encode(), hashlib.sha256).digest()

    key = derive(derive(derive(derive(("AWS4" + secret_key).encode(), day), region), service),
                 "aws4_request")
    signature = hmac.new(key, string_to_sign.encode(), hashlib.sha256).hexdigest()
    signed["Authorization"] = (
        f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, "
        f"SignedHeaders={signed_names}, Signature={signature}")
    return signed


def _request(session: requests.Session, method: str, url: str,
             *, headers: dict[str, str] | None = None, data: bytes | None = None,
             timeout: int = 90) -> dict[str, Any]:
    for attempt in range(4):
        try:
            response = session.request(method, url, headers=headers, data=data,
                                       timeout=(15, min(timeout, 60)))
            response.raise_for_status()
            result = response.json()
            break
        except (requests.Timeout, requests.ConnectionError) as exc:
            if attempt == 3:
                raise UploadError(
                    f"Dreamina upload transport failed after 4 proxy attempts: {type(exc).__name__}"
                ) from exc
            time.sleep(min(2 ** attempt, 4))
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else 0
            if status >= 500 and attempt < 3:
                time.sleep(min(2 ** attempt, 4))
                continue
            raise UploadError(f"Dreamina upload HTTP {status}") from exc
        except ValueError as exc:
            raise UploadError("Dreamina upload returned invalid JSON") from exc
    if not isinstance(result, dict):
        raise UploadError("Dreamina upload returned an invalid response")
    error = (result.get("ResponseMetadata") or {}).get("Error")
    if error:
        raise UploadError(f"Dreamina upload rejected: {error.get('Code') or 'unknown'}")
    return result


def _api_call(session: requests.Session, method: str, url: str,
              token: dict[str, str], service: str,
              payload: dict[str, Any] | None = None, *,
              signing_region: str = "ap-southeast-1") -> dict[str, Any]:
    body = json.dumps(payload, separators=(",", ":")).encode() if payload is not None else b""
    headers = _sign(method, url, access_key=token["access_key_id"],
                    secret_key=token["secret_access_key"],
                    session_token=token["session_token"], region=signing_region,
                    service=service, payload=body)
    headers.update({"accept": "*/*", "origin": "https://dreamina.capcut.com",
                    "referer": "https://dreamina.capcut.com/ai-tool/generate"})
    if payload is not None:
        headers["content-type"] = "application/json"
    return _request(session, method, url, headers=headers, data=body if payload is not None else None)


def _upload_blob(session: requests.Session, url: str, auth: str, contents: bytes,
                 *, image: bool) -> None:
    headers = {"Authorization": auth, "Content-CRC32": f"{zlib.crc32(contents) & 0xffffffff:08x}",
               "Content-Type": "application/octet-stream",
               "Origin": "https://dreamina.capcut.com",
               "Referer": "https://dreamina.capcut.com/ai-tool/generate"}
    if image:
        headers["Content-Disposition"] = 'attachment; filename="upload"'
    response = _request(session, "POST", url, headers=headers, data=contents,
                        timeout=max(90, min(600, 60 + len(contents) // 50000)))
    if response.get("code") not in (None, 2000):
        raise UploadError(f"Dreamina file upload rejected: {response.get('code')}")


def _probe_media(contents: bytes, suffix: str) -> dict[str, float]:
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as handle:
        handle.write(contents)
        path = Path(handle.name)
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries",
             "format=duration:stream=width,height,r_frame_rate", "-of", "json", str(path)],
            capture_output=True, text=True, timeout=30, check=True)
        data = json.loads(result.stdout)
        streams = data.get("streams") or []
        video = next((x for x in streams if x.get("width")), {})
        rate = str(video.get("r_frame_rate") or "0/1").split("/")
        fps = float(rate[0]) / max(1, float(rate[1])) if len(rate) == 2 else 0
        return {"duration": float((data.get("format") or {}).get("duration") or 0),
                "width": float(video.get("width") or 0),
                "height": float(video.get("height") or 0), "fps": fps}
    except (OSError, subprocess.SubprocessError, ValueError, ZeroDivisionError):
        return {"duration": 0, "width": 0, "height": 0, "fps": 0}
    finally:
        path.unlink(missing_ok=True)


def upload_media_bytes(contents: bytes, kind: str, *, browser: CDPBrowser,
                       proxy_url: str, filename: str = "upload") -> dict[str, Any]:
    if kind not in {"images", "videos", "audios"}:
        raise ValueError("unsupported media kind")
    session = _session(proxy_url)
    image = kind == "images"
    token = browser.post("/mweb/v1/get_upload_token", {"scene": 2 if image else 1},
                         query={"aid": "513695", "device_platform": "web"})
    for key in ("access_key_id", "secret_access_key", "session_token", "space_name"):
        if not token.get(key):
            raise UploadError("Dreamina upload token is incomplete")
    if image:
        service_id = str(token["space_name"])
        region = token.get("region")
        if region == "us":
            url = "https://imagex16-normal-us-ttp2.capcutapi.us/"
            signing_region = "us-east-1"
        elif region == "useast8":
            # Verified against the official SDK's region fallback and a successful Apply.
            if ("upload_domain" in token
                    and token["upload_domain"] != "imagex16-normal-us-ttp2.capcutapi.us"):
                raise UploadError("Dreamina image upload domain is unsupported")
            url = "https://imagex16-normal-us-ttp2.capcutapi.us/"
            signing_region = "ap-singapore-1"
        elif region in (None, "", "sg"):
            url = "https://imagex-normal-sg.capcutapi.com/"
            signing_region = "ap-southeast-1"
        else:
            raise UploadError("Dreamina image upload region is unsupported")
        apply = url + "?" + urllib.parse.urlencode({
            "Action": "ApplyImageUpload", "Version": "2018-08-01",
            "ServiceId": service_id, "FileSize": len(contents),
            "s": secrets.token_hex(6), "device_platform": "web"})
        result = _api_call(session, "GET", apply, token, "imagex",
                           signing_region=signing_region)
        address = (result.get("Result") or {}).get("UploadAddress") or {}
        store = (address.get("StoreInfos") or [None])[0] or {}
        host = (address.get("UploadHosts") or [None])[0]
        if not host or not store.get("StoreUri") or not store.get("Auth"):
            raise UploadError("Dreamina image upload address is missing")
        _upload_blob(session, f"https://{host}/upload/v1/{store['StoreUri']}",
                     store["Auth"], contents, image=True)
        commit = url + "?" + urllib.parse.urlencode({
            "Action": "CommitImageUpload", "Version": "2018-08-01",
            "ServiceId": service_id})
        result = _api_call(session, "POST", commit, token, "imagex",
                           {"SessionKey": address["SessionKey"], "SuccessActionStatus": "200"},
                           signing_region=signing_region)
        item = ((result.get("Result") or {}).get("Results") or [None])[0] or {}
        plugin = ((result.get("Result") or {}).get("PluginResult") or [None])[0] or {}
        uri = plugin.get("ImageUri") or item.get("Uri")
        if not uri or item.get("UriStatus") not in (2000, "2000"):
            raise UploadError("Dreamina image commit did not return a valid URI")
        try:
            with Image.open(io.BytesIO(contents)) as picture:
                width, height = picture.size
        except (OSError, ValueError) as exc:
            raise UploadError("invalid reference image") from exc
        return {"image_uri": str(uri), "width": width, "height": height,
                "filename": filename}
    space = str(token["space_name"])
    origin = "https://vod-normal-sg.capcutapi.com/"
    api_kwargs: dict[str, str] = {}
    region = token.get("region")
    if region in ("useast8", "us"):
        if ("upload_domain" in token
                and token["upload_domain"] != "vod16-normal-us-ttp2.capcutapi.us"):
            raise UploadError("Dreamina VOD upload domain is unsupported")
        origin = "https://vod16-normal-us-ttp2.capcutapi.us/"
        api_kwargs["signing_region"] = "ap-singapore-1" if region == "useast8" else "us-east-1"
    apply = origin + "?" + urllib.parse.urlencode({
        "Action": "ApplyUploadInner", "Version": "2020-11-19",
        "SpaceName": space, "FileType": "video", "IsInner": "1",
        "FileSize": len(contents), "s": secrets.token_hex(6),
        "device_platform": "web"})
    result = _api_call(session, "GET", apply, token, "vod", **api_kwargs)
    node = (((result.get("Result") or {}).get("InnerUploadAddress") or {})
            .get("UploadNodes") or [None])[0] or {}
    store = (node.get("StoreInfos") or [None])[0] or {}
    if not node.get("UploadHost") or not store.get("StoreUri") or not store.get("Auth"):
        raise UploadError("Dreamina VOD upload address is missing")
    _upload_blob(session, f"https://{node['UploadHost']}/upload/v1/{store['StoreUri']}",
                 store["Auth"], contents, image=False)
    commit = origin + "?" + urllib.parse.urlencode({
        "Action": "CommitUploadInner", "Version": "2020-11-19", "SpaceName": space})
    result = _api_call(session, "POST", commit, token, "vod",
                       {"SessionKey": node["SessionKey"], "Functions": []}, **api_kwargs)
    item = ((result.get("Result") or {}).get("Results") or [None])[0] or {}
    vid = item.get("Vid") or node.get("Vid")
    if not vid:
        raise UploadError("Dreamina VOD commit did not return a video ID")
    probe = _probe_media(contents, Path(filename).suffix or (".mp4" if kind == "videos" else ".mp3"))
    upstream = item.get("VideoMeta") or {}
    duration = probe["duration"] or float(upstream.get("Duration") or 0)
    return {"vid": str(vid), "duration": duration,
            "width": int(probe["width"] or upstream.get("Width") or 0),
            "height": int(probe["height"] or upstream.get("Height") or 0),
            "fps": round(probe["fps"] or float(upstream.get("Fps") or 0), 3),
            "filename": filename}
