"""Convert an upstream 720p video to 480p and store it in s3-center."""

from __future__ import annotations

import json
import hashlib
import os
import re
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlsplit

import requests


class ResultTransferError(Exception):
    pass


MAX_RESULT_BYTES = 512 * 1024 * 1024
MAX_SINGLE_UPLOAD_BYTES = 256 * 1024 * 1024
RESULT_DOWNLOAD_ATTEMPTS = 6
_CONTENT_RANGE = re.compile(r"bytes (\d+)-(\d+)/(\d+)")
SD_2_5_480P_X_DIMENSIONS = {
    "16:9": (864, 496),
    "4:3": (752, 560),
    "1:1": (640, 640),
    "3:4": (560, 752),
    "9:16": (496, 864),
    "21:9": (992, 432),
    "9:21": (432, 992),
}


def _config(storage: dict | None = None) -> tuple[str, str, str, str]:
    storage = storage or {}
    if str(storage.get("result_s3_backend") or "s3_center_direct") != "s3_center_direct":
        raise ResultTransferError("sd-2-5-480p-x requires s3-center direct storage")
    base = str(storage.get("result_s3_center_api_base") or os.getenv("IJM_S3_CENTER_API_BASE", "")).strip().rstrip("/")
    token = str(storage.get("result_s3_center_token") or os.getenv("IJM_S3_CENTER_TOKEN", "")).strip()
    bucket = str(storage.get("result_s3_bucket") or os.getenv("IJM_S3_CENTER_BUCKET", "assets")).strip()
    prefix = str(storage.get("result_s3_key_prefix") or os.getenv("IJM_S3_CENTER_KEY_PREFIX", "aicontent/ai-sdijm")).strip("/")
    parsed = urlsplit(base)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ResultTransferError("s3-center API base is not configured")
    if not token or not bucket:
        raise ResultTransferError("s3-center token or bucket is not configured")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9/_-]{0,99}", prefix) or "//" in prefix or any(
        part in {".", ".."} for part in prefix.split("/")
    ):
        raise ResultTransferError("s3-center object key prefix is invalid")
    return base, token, bucket, prefix


def _json_response(response: requests.Response, label: str) -> dict:
    if not 200 <= response.status_code < 300:
        raise ResultTransferError(f"{label} HTTP {response.status_code}")
    try:
        value = response.json()
    except ValueError as exc:
        raise ResultTransferError(f"{label} returned invalid JSON") from exc
    if not isinstance(value, dict):
        raise ResultTransferError(f"{label} returned invalid JSON")
    return value


def _post(http: requests.Session, url: str, token: str, body: dict | None, label: str,
          headers: dict | None = None) -> dict:
    for attempt in range(3):
        try:
            response = http.post(url, headers={"Authorization": f"Bearer {token}", **(headers or {})},
                                 json=body, timeout=60)
            if response.status_code in {408, 425, 429, 500, 502, 503, 504} and attempt < 2:
                time.sleep(2 ** attempt)
                continue
            return _json_response(response, label)
        except requests.RequestException as exc:
            if attempt == 2:
                raise ResultTransferError(f"{label} transport failure: {type(exc).__name__}") from exc
            time.sleep(2 ** attempt)
    raise ResultTransferError(f"{label} failed")


def _download(url: str, proxy_url: str, target: Path) -> None:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ResultTransferError("upstream result URL is invalid")
    if not proxy_url:
        raise ResultTransferError("account proxy is required for upstream result download")
    proxy = proxy_url.replace("socks5://", "socks5h://", 1)
    expected_size: int | None = None
    validator = ""
    source_etag = ""
    source_modified = ""
    target.unlink(missing_ok=True)
    for attempt in range(RESULT_DOWNLOAD_ATTEMPTS):
        offset = target.stat().st_size if target.exists() else 0
        # Some proxies drop the final HTTP frame after delivering every byte.
        if expected_size is not None and offset == expected_size and offset > 0:
            return
        headers = {"Accept-Encoding": "identity"}
        if offset:
            headers["Range"] = f"bytes={offset}-"
            if validator:
                headers["If-Range"] = validator
        try:
            with requests.Session() as http:
                http.trust_env = False
                with http.get(url, proxies={"http": proxy, "https": proxy},
                              headers=headers, stream=True, timeout=(20, 120)) as response:
                    if response.status_code in {408, 425, 429, 500, 502, 503, 504}:
                        raise requests.HTTPError(f"HTTP {response.status_code}")
                    if response.status_code not in {200, 206}:
                        raise ResultTransferError(f"upstream result download HTTP {response.status_code}")
                    if response.status_code == 206:
                        match = _CONTENT_RANGE.fullmatch(response.headers.get("Content-Range", ""))
                        if (not offset or not match or int(match.group(1)) != offset
                                or int(match.group(2)) < offset
                                or int(match.group(3)) <= int(match.group(2))):
                            raise ResultTransferError("upstream result resume range is invalid")
                        total = int(match.group(3))
                        if expected_size is not None and total != expected_size:
                            raise ResultTransferError("upstream result changed during download")
                        expected_size = total
                    else:
                        # The server ignored Range (or If-Range detected a new
                        # object). Start again instead of appending a duplicate.
                        offset = 0
                        length = response.headers.get("Content-Length", "")
                        expected_size = int(length) if length.isdecimal() else None
                    if expected_size is not None and expected_size > MAX_RESULT_BYTES:
                        raise ResultTransferError("upstream result exceeds 512 MiB")
                    etag = response.headers.get("ETag", "")
                    modified = response.headers.get("Last-Modified", "")
                    if offset and ((source_etag and etag and etag != source_etag)
                                   or (source_modified and modified and modified != source_modified)):
                        raise ResultTransferError("upstream result changed during download")
                    source_etag, source_modified = etag, modified
                    validator = etag if etag and not etag.startswith("W/") else modified
                    size = offset
                    with target.open("ab" if offset else "wb") as output:
                        for chunk in response.iter_content(1024 * 1024):
                            size += len(chunk)
                            if size > MAX_RESULT_BYTES or (expected_size is not None and size > expected_size):
                                raise ResultTransferError("upstream result exceeds declared size")
                            output.write(chunk)
                    if expected_size is not None and size < expected_size:
                        raise requests.exceptions.ChunkedEncodingError("upstream result ended before declared size")
                    if not size:
                        raise ResultTransferError("upstream result is empty")
                    return
        except requests.RequestException as exc:
            if attempt == RESULT_DOWNLOAD_ATTEMPTS - 1:
                raise ResultTransferError(f"upstream result download failed: {type(exc).__name__}") from exc
            time.sleep(min(2 ** attempt, 8))


def _probe(path: Path) -> tuple[int, int, float]:
    result = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                             "-show_entries", "stream=width,height:format=duration",
                             "-of", "json", str(path)], capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise ResultTransferError("ffprobe could not read the video")
    value = json.loads(result.stdout)
    stream = (value.get("streams") or [{}])[0]
    return int(stream.get("width") or 0), int(stream.get("height") or 0), float((value.get("format") or {}).get("duration") or 0)


def _transcode(source: Path, target: Path, aspect_ratio: str) -> tuple[int, int]:
    dimensions = SD_2_5_480P_X_DIMENSIONS.get(aspect_ratio)
    if dimensions is None:
        raise ResultTransferError(f"unsupported 480p result aspect ratio: {aspect_ratio}")
    target_width, target_height = dimensions
    width, height, duration = _probe(source)
    if width < target_width or height < target_height or duration <= 0:
        raise ResultTransferError("upstream result is not a valid 720p video")
    # Cover the requested output frame and crop from its center. The 480p
    # product tier has aspect-specific pixel dimensions, including 640x640.
    scale = f"max({target_width}/(iw*sar),{target_height}/ih)"
    video_filter = (
        f"scale=w='ceil(iw*sar*{scale}/2)*2':"
        f"h='ceil(ih*{scale}/2)*2':flags=lanczos,"
        f"crop={target_width}:{target_height}:(iw-ow)/2:(ih-oh)/2,setsar=1"
    )
    result = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(source),
                             "-map", "0:v:0", "-map", "0:a?", "-vf", video_filter,
                             "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p",
                             "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(target)],
                            capture_output=True, text=True, timeout=900)
    if result.returncode:
        raise ResultTransferError(f"480p transcode failed: {result.stderr[-300:]}")
    out_width, out_height, out_duration = _probe(target)
    if (out_width, out_height) != dimensions or abs(out_duration - duration) > 0.5:
        raise ResultTransferError("480p transcode validation failed")
    return dimensions


def _upload_headers(value: object) -> dict[str, str]:
    """Normalize s3-center presigned headers (its values can be string lists)."""
    if not isinstance(value, dict):
        raise ResultTransferError("s3-center upload headers are invalid")
    headers: dict[str, str] = {}
    for raw_name, raw_value in value.items():
        name = str(raw_name or "").strip()
        if not name or any(character in name for character in "\r\n:"):
            raise ResultTransferError("s3-center upload header name is invalid")
        if isinstance(raw_value, str):
            header_value = raw_value
        elif isinstance(raw_value, (list, tuple)) and all(isinstance(item, str) for item in raw_value):
            header_value = ",".join(raw_value)
        else:
            raise ResultTransferError("s3-center upload header value is invalid")
        if not header_value or header_value[:1].isspace() or any(character in header_value for character in "\r\n"):
            raise ResultTransferError("s3-center upload header value is invalid")
        headers[name] = header_value
    return headers


def presign(bucket: str, key: str, expected_size: int | None = None,
            storage: dict | None = None) -> tuple[str, dict]:
    base, token, configured_bucket, _ = _config(storage)
    if bucket != configured_bucket:
        raise ResultTransferError("s3-center bucket mismatch")
    with requests.Session() as http:
        http.trust_env = False
        grant = _post(http, f"{base}/v1/objects/presign", token,
                      {"bucket": bucket, "key": key, "method": "GET", "expires_seconds": 0},
                      "s3-center presign")
    request = grant.get("request") or {}
    url = str(request.get("url") or "")
    if (grant.get("bucket") != bucket or grant.get("key") != key or request.get("method") != "GET"
            or not url.startswith("https://") or (expected_size is not None and
            int(grant.get("size_bytes") or -1) != expected_size)):
        raise ResultTransferError("s3-center presign response does not match object")
    return url, {"bucket": bucket, "key": key, "size_bytes": int(grant.get("size_bytes") or 0),
                 "expires_at": str(request.get("expires_at") or "")}


def expires_soon(metadata: dict) -> bool:
    value = str(metadata.get("expires_at") or "")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() < time.time() + 300
    except ValueError:
        return True


def transfer(url: str, proxy_url: str, task_id: str,
             storage: dict | None = None, *, aspect_ratio: str = "16:9") -> tuple[str, dict]:
    base, token, bucket, prefix = _config(storage)
    key = f"{prefix}/{task_id}/480p.mp4"
    with tempfile.TemporaryDirectory(prefix="ijm-result-") as directory:
        source, target = Path(directory) / "source.mp4", Path(directory) / "480p.mp4"
        _download(url, proxy_url, source)
        width, height = _transcode(source, target, aspect_ratio)
        size = target.stat().st_size
        if size > MAX_SINGLE_UPLOAD_BYTES:
            raise ResultTransferError("480p result exceeds s3-center single-upload limit")
        idempotency_key = f"ijm2api:{task_id}:{hashlib.sha256(f'{bucket}/{key}'.encode()).hexdigest()[:24]}"
        with requests.Session() as http:
            http.trust_env = False
            created = _post(http, f"{base}/v1/uploads", token,
                            {"bucket": bucket, "key": key, "size": size, "content_type": "video/mp4"},
                            "s3-center create", {"Idempotency-Key": idempotency_key})
            upload_id = str(created.get("id") or "")
            upload = created.get("upload") or {}
            if (not re.fullmatch(r"du_[a-f0-9]{32}", upload_id)
                    or created.get("status") != "url_issued"
                    or int(created.get("declared_bytes") or -1) != size
                    or upload.get("method") != "PUT"
                    or not str(upload.get("url") or "").startswith("https://")):
                raise ResultTransferError("s3-center create response is invalid")
            headers = _upload_headers(upload.get("headers"))
            for attempt in range(3):
                try:
                    with target.open("rb") as contents:
                        response = http.put(upload["url"], headers=headers, data=contents,
                                            timeout=(30, 300), allow_redirects=False)
                    if 200 <= response.status_code < 300:
                        break
                    if response.status_code not in {408, 425, 429, 500, 502, 503, 504} or attempt == 2:
                        raise ResultTransferError(f"s3-center upload HTTP {response.status_code}")
                except requests.RequestException as exc:
                    if attempt == 2:
                        raise ResultTransferError(f"s3-center upload failed: {type(exc).__name__}") from exc
                time.sleep(2 ** attempt)
            committed = _post(http, f"{base}/v1/uploads/{quote(upload_id, safe='')}/complete",
                              token, None, "s3-center complete")
            if (committed.get("id") != upload_id or committed.get("status") != "committed"
                    or committed.get("logical_key") != f"{bucket}/{key}"
                    or int(committed.get("declared_bytes") or -1) != size):
                raise ResultTransferError("s3-center committed object does not match upload")
        signed_url, metadata = presign(bucket, key, size, storage)
        metadata["upstream_url"] = url
        metadata["upload_id"] = upload_id
        metadata["width"] = width
        metadata["height"] = height
        metadata["aspect_ratio"] = aspect_ratio
        metadata["fit"] = "center_crop"
        return signed_url, metadata
