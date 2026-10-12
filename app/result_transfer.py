"""Convert an upstream 720p video to 480p and store it in s3-center."""

from __future__ import annotations

import json
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


def _config() -> tuple[str, str, str]:
    base = os.getenv("IJM_S3_CENTER_API_BASE", "").strip().rstrip("/")
    token = os.getenv("IJM_S3_CENTER_TOKEN", "").strip()
    bucket = os.getenv("IJM_S3_CENTER_BUCKET", "").strip()
    parsed = urlsplit(base)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ResultTransferError("s3-center API base is not configured")
    if not token or not bucket:
        raise ResultTransferError("s3-center token or bucket is not configured")
    return base, token, bucket


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
    for attempt in range(3):
        try:
            with requests.get(url, proxies={"http": proxy, "https": proxy},
                              stream=True, timeout=(20, 120)) as response:
                if response.status_code != 200:
                    raise ResultTransferError(f"upstream result download HTTP {response.status_code}")
                size = 0
                with target.open("wb") as output:
                    for chunk in response.iter_content(1024 * 1024):
                        size += len(chunk)
                        if size > MAX_RESULT_BYTES:
                            raise ResultTransferError("upstream result exceeds 512 MiB")
                        output.write(chunk)
                if not size:
                    raise ResultTransferError("upstream result is empty")
                return
        except requests.RequestException as exc:
            if attempt == 2:
                raise ResultTransferError(f"upstream result download failed: {type(exc).__name__}") from exc
            time.sleep(2 ** attempt)


def _probe(path: Path) -> tuple[int, int, float]:
    result = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                             "-show_entries", "stream=width,height:format=duration",
                             "-of", "json", str(path)], capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise ResultTransferError("ffprobe could not read the video")
    value = json.loads(result.stdout)
    stream = (value.get("streams") or [{}])[0]
    return int(stream.get("width") or 0), int(stream.get("height") or 0), float((value.get("format") or {}).get("duration") or 0)


def _transcode(source: Path, target: Path) -> None:
    width, height, duration = _probe(source)
    if min(width, height) < 720 or duration <= 0:
        raise ResultTransferError("upstream result is not a valid 720p video")
    # Keep the shorter side at 480 px and preserve the source aspect ratio.
    scale = "scale=480:-2" if width <= height else "scale=-2:480"
    result = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(source),
                             "-map", "0:v:0", "-map", "0:a?", "-vf", scale,
                             "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p",
                             "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(target)],
                            capture_output=True, text=True, timeout=900)
    if result.returncode:
        raise ResultTransferError(f"480p transcode failed: {result.stderr[-300:]}")
    out_width, out_height, out_duration = _probe(target)
    if min(out_width, out_height) != 480 or abs(out_duration - duration) > 0.5:
        raise ResultTransferError("480p transcode validation failed")


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


def presign(bucket: str, key: str, expected_size: int | None = None) -> tuple[str, dict]:
    base, token, configured_bucket = _config()
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


def transfer(url: str, proxy_url: str, task_id: str) -> tuple[str, dict]:
    base, token, bucket = _config()
    key = f"ijm2api/{task_id}/480p.mp4"
    with tempfile.TemporaryDirectory(prefix="ijm-result-") as directory:
        source, target = Path(directory) / "source.mp4", Path(directory) / "480p.mp4"
        _download(url, proxy_url, source)
        _transcode(source, target)
        size = target.stat().st_size
        if size > MAX_SINGLE_UPLOAD_BYTES:
            raise ResultTransferError("480p result exceeds s3-center single-upload limit")
        with requests.Session() as http:
            http.trust_env = False
            created = _post(http, f"{base}/v1/uploads", token,
                            {"bucket": bucket, "key": key, "size": size, "content_type": "video/mp4"},
                            "s3-center create", {"Idempotency-Key": f"ijm2api:{task_id}:480p"})
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
        signed_url, metadata = presign(bucket, key, size)
        metadata["upstream_url"] = url
        metadata["upload_id"] = upload_id
        return signed_url, metadata
