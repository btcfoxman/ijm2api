"""Store original Canvas files and explicitly requested 480p results in s3-center."""

from __future__ import annotations

import json
import hashlib
import mimetypes
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
    def __init__(self, *args, stage: str | None = None, code: str | None = None,
                 http_status: int | None = None, retryable: bool | None = None):
        super().__init__(*args)
        self.stage = stage
        self.code = code
        self.http_status = http_status
        self.retryable = retryable


def _transfer_error(stage: str, code: str, *, http_status: int | None = None,
                    retryable: bool = False) -> ResultTransferError:
    # Only fixed local enums and the HTTP integer cross the task audit boundary.
    return ResultTransferError(f'{stage}: {code}', stage=stage, code=code,
                               http_status=http_status, retryable=retryable)


def _http_error(label: str, status: int, stage: str | None) -> ResultTransferError:
    code = {401: 'S3_AUTH_FAILED', 403: 'S3_AUTH_FAILED', 404: 'S3_NOT_FOUND',
            409: 'S3_SESSION_CONFLICT', 410: 'S3_SESSION_EXPIRED',
            507: 'S3_INSUFFICIENT_CAPACITY'}.get(status, 'S3_HTTP_ERROR')
    return ResultTransferError(f'{label} HTTP {status}', stage=stage,
        code=code if stage else None, http_status=status if stage else None,
        retryable=(status in {408, 425, 429} or status >= 500) if stage else None)


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


def _json_response(response: requests.Response, label: str, *, stage: str | None = None) -> dict:
    if not 200 <= response.status_code < 300:
        raise _http_error(label, response.status_code, stage)
    try:
        value = response.json()
    except ValueError:
        raise ResultTransferError(f"{label} returned invalid JSON", stage=stage,
            code='S3_RESPONSE_INVALID' if stage else None,
            http_status=response.status_code if stage else None, retryable=False if stage else None) from None
    if not isinstance(value, dict):
        raise ResultTransferError(f"{label} returned invalid JSON", stage=stage,
            code='S3_RESPONSE_INVALID' if stage else None,
            http_status=response.status_code if stage else None, retryable=False if stage else None)
    return value


def _post(http: requests.Session, url: str, token: str, body: dict | None, label: str,
          headers: dict | None = None, *, stage: str | None = None) -> dict:
    for attempt in range(3):
        try:
            options = {'allow_redirects': False} if stage else {}
            response = http.post(url, headers={"Authorization": f"Bearer {token}", **(headers or {})},
                                 json=body, timeout=60, **options)
            if response.status_code in {408, 425, 429, 500, 502, 503, 504} and attempt < 2:
                time.sleep(2 ** attempt)
                continue
            return _json_response(response, label, stage=stage)
        except requests.RequestException as exc:
            if attempt == 2:
                raise ResultTransferError(f"{label} transport failure: {type(exc).__name__}",
                    stage=stage, code='S3_TRANSPORT_ERROR' if stage else None,
                    retryable=True if stage else None) from None
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
                      "s3-center presign", stage='s3_presign')
    request = grant.get("request") or {}
    if not isinstance(request, dict):
        raise _transfer_error('s3_presign', 'S3_RESPONSE_INVALID')
    url = str(request.get("url") or "")
    if (grant.get("bucket") != bucket or grant.get("key") != key or request.get("method") != "GET"
            or not url.startswith("https://") or (expected_size is not None and
            (type(grant.get('size_bytes')) is not int or grant['size_bytes'] != expected_size))):
        raise _transfer_error('s3_presign', 'S3_RESPONSE_INVALID')
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


_CHECKPOINT_FIELDS = frozenset({'version', 'task_id', 'index', 'bucket', 'key', 'size_bytes',
    'sha256', 'content_type', 'idempotency_key', 'upload_id', 'state'})
_CHECKPOINT_STATES = frozenset({'creating', 'url_issued', 'uploaded', 'committed'})
_RETRY_HTTP = frozenset({408, 425, 429, 500, 502, 503, 504})


def _get_upload(http, base, token, upload_id):
    for attempt in range(3):
        try:
            response = http.get(f'{base}/v1/uploads/{quote(upload_id, safe="")}',
                headers={'Authorization': f'Bearer {token}'}, timeout=60, allow_redirects=False)
            if response.status_code in _RETRY_HTTP and attempt < 2:
                time.sleep(2 ** attempt)
                continue
            return _json_response(response, 's3-center resume', stage='s3_resume')
        except requests.RequestException:
            if attempt == 2:
                raise _transfer_error('s3_resume', 'S3_TRANSPORT_ERROR', retryable=True) from None
            time.sleep(2 ** attempt)
    raise AssertionError('unreachable')


def store_original(path: Path, task_id: str, storage: dict | None = None, *,
                   index: int = 0, content_type: str = 'video/mp4',
                   checkpoint: dict | None = None, persist_checkpoint=None,
                   on_phase=None) -> tuple[str, dict]:
    """Publish original bytes, resuming a bound session when its checkpoint exists.

    The persistence callback must acknowledge each boundary before its next HTTP
    effect. Callers without callbacks retain the original stateless API.
    """
    try:
        base, token, bucket, prefix = _config(storage)
    except (ResultTransferError, ValueError, TypeError):
        raise _transfer_error('s3_checkpoint', 'S3_CONFIG_INVALID') from None
    if (not isinstance(task_id, str) or re.fullmatch(r'[A-Za-z0-9_-]{1,120}', task_id) is None
            or type(index) is not int or not 0 <= index < 10000
            or not isinstance(content_type, str)
            or re.fullmatch(r'[A-Za-z0-9.+-]+/[A-Za-z0-9.+-]+', content_type) is None):
        raise _transfer_error('s3_checkpoint', 'S3_FILE_BINDING_INVALID')
    path = Path(path)
    try:
        initial = path.stat()
        size = initial.st_size
        if not path.is_file() or not 0 < size <= MAX_SINGLE_UPLOAD_BYTES:
            raise _transfer_error('s3_checkpoint', 'S3_FILE_INVALID')
        hasher = hashlib.sha256()
        with path.open('rb') as contents:
            for chunk in iter(lambda: contents.read(1024 * 1024), b''):
                hasher.update(chunk)
    except OSError:
        raise _transfer_error('s3_checkpoint', 'S3_FILE_INVALID') from None
    suffix = (mimetypes.guess_extension(content_type) or path.suffix).lower()
    if suffix not in {'.mp4', '.mov', '.webm', '.png', '.jpg', '.jpeg', '.webp', '.gif', '.avif'}:
        raise _transfer_error('s3_checkpoint', 'S3_FILE_TYPE_UNSUPPORTED')
    key, digest = f'{prefix}/{task_id}/original-{index}{suffix}', hasher.hexdigest()
    base_idem = 'ijm2api:' + hashlib.sha256(f'{task_id}:{bucket}:{key}:{digest}'.encode()).hexdigest()
    binding = {'version': 1, 'task_id': task_id, 'index': index, 'bucket': bucket, 'key': key,
               'size_bytes': size, 'sha256': digest, 'content_type': content_type}
    current = {**binding, 'idempotency_key': base_idem, 'upload_id': '', 'state': 'creating'}
    if checkpoint is not None:
        if (not isinstance(checkpoint, dict) or set(checkpoint) != _CHECKPOINT_FIELDS
                or any(type(checkpoint.get(k)) is not type(v) or checkpoint[k] != v for k, v in binding.items())
                or not isinstance(checkpoint.get('state'), str) or checkpoint['state'] not in _CHECKPOINT_STATES
                or not isinstance(checkpoint.get('idempotency_key'), str)
                or re.fullmatch(re.escape(base_idem) + r'(?::resume:[1-9][0-9]{0,8})?', checkpoint['idempotency_key']) is None
                or (checkpoint['state'] == 'creating' and checkpoint.get('upload_id') != '')
                or (checkpoint['state'] != 'creating' and (not isinstance(checkpoint.get('upload_id'), str)
                    or re.fullmatch(r'du_[a-f0-9]{32}', checkpoint['upload_id']) is None))):
            raise _transfer_error('s3_checkpoint', 'S3_CHECKPOINT_MISMATCH')
        current = dict(checkpoint)

    def phase(name):
        if on_phase is not None:
            try:
                acknowledged = on_phase(name)
            except Exception:
                raise _transfer_error(name, 'S3_TRANSFER_INTERRUPTED') from None
            if acknowledged is False:
                raise _transfer_error(name, 'S3_TRANSFER_INTERRUPTED')

    def save(**changes):
        nonlocal current
        value = {**current, **changes}
        phase('s3_checkpoint')
        if persist_checkpoint is not None:
            try:
                acknowledged = persist_checkpoint(dict(value))
            except Exception:
                raise _transfer_error('s3_checkpoint', 'S3_CHECKPOINT_PERSIST_FAILED', retryable=True) from None
            if acknowledged is False:
                raise _transfer_error('s3_checkpoint', 'S3_CHECKPOINT_PERSIST_FAILED')
        current = value

    def validate_session(value, stage):
        if (not isinstance(value, dict) or value.get('id') != current['upload_id']
                or value.get('logical_key') != f'{bucket}/{key}'
                or type(value.get('declared_bytes')) is not int or value['declared_bytes'] != size):
            raise _transfer_error(stage, 'S3_SESSION_BINDING_MISMATCH')
        if not isinstance(value.get('status'), str):
            raise _transfer_error(stage, 'S3_RESPONSE_INVALID')
        return value.get('status')

    with requests.Session() as http:
        http.trust_env = False
        need_upload = True
        if current['upload_id']:
            phase('s3_resume')
            session = _get_upload(http, base, token, current['upload_id'])
            state = validate_session(session, 's3_resume')
            if state == 'committed':
                save(state='committed')
                need_upload = False
            elif current['state'] == 'committed':
                raise _transfer_error('s3_resume', 'S3_SESSION_STATE_CONFLICT')
            elif state in {'expired', 'aborted'}:
                ordinal = int(current['idempotency_key'].split(':resume:')[-1]) + 1 if ':resume:' in current['idempotency_key'] else 1
                if ordinal > 999999999:
                    raise _transfer_error('s3_checkpoint', 'S3_CHECKPOINT_INVALID')
                save(idempotency_key=f'{base_idem}:resume:{ordinal}', upload_id='', state='creating')
            elif state in {'reserved', 'url_issued'}:
                need_upload = current['state'] != 'uploaded'
            else:
                raise _transfer_error('s3_resume', 'S3_SESSION_STATE_INVALID')
        if need_upload:
            if checkpoint is None:
                save()  # Persist the first deterministic intent before create.
            phase('s3_create')
            created = _post(http, f'{base}/v1/uploads', token,
                {'bucket': bucket, 'key': key, 'size': size, 'content_type': content_type},
                's3-center create', {'Idempotency-Key': current['idempotency_key']}, stage='s3_create')
            upload_id = created.get('id')
            if (not isinstance(upload_id, str) or re.fullmatch(r'du_[a-f0-9]{32}', upload_id) is None
                    or type(created.get('declared_bytes')) is not int or created['declared_bytes'] != size
                    or 'logical_key' in created and created['logical_key'] != f'{bucket}/{key}'
                    or current['upload_id'] and current['upload_id'] != upload_id):
                raise _transfer_error('s3_create', 'S3_RESPONSE_INVALID')
            if created.get('status') == 'committed':
                if created.get('logical_key') != f'{bucket}/{key}':
                    raise _transfer_error('s3_create', 'S3_SESSION_BINDING_MISMATCH')
                save(upload_id=upload_id, state='committed')
            else:
                upload = created.get('upload')
                if (created.get('status') != 'url_issued' or not isinstance(upload, dict)
                        or upload.get('method') != 'PUT' or not isinstance(upload.get('url'), str)):
                    raise _transfer_error('s3_create', 'S3_RESPONSE_INVALID')
                signed = urlsplit(upload['url'])
                if signed.scheme != 'https' or not signed.hostname or signed.username or signed.password or signed.fragment:
                    raise _transfer_error('s3_create', 'S3_RESPONSE_INVALID')
                try:
                    headers = _upload_headers(upload.get('headers'))
                except ResultTransferError:
                    raise _transfer_error('s3_create', 'S3_RESPONSE_INVALID') from None
                save(upload_id=upload_id, state='url_issued')
                phase('s3_put')
                for attempt in range(3):
                    try:
                        with path.open('rb') as contents:
                            actual = os.fstat(contents.fileno())
                            if (actual.st_dev, actual.st_ino, actual.st_size, actual.st_mtime_ns) != (
                                    initial.st_dev, initial.st_ino, size, initial.st_mtime_ns):
                                raise _transfer_error('s3_put', 'S3_FILE_CHANGED')
                            response = http.put(upload['url'], headers=headers, data=contents,
                                                timeout=(30, 300), allow_redirects=False)
                        if 200 <= response.status_code < 300:
                            break
                        if response.status_code not in _RETRY_HTTP or attempt == 2:
                            raise _http_error('s3-center upload', response.status_code, 's3_put')
                    except requests.RequestException:
                        if attempt == 2:
                            raise _transfer_error('s3_put', 'S3_TRANSPORT_ERROR', retryable=True) from None
                    except OSError:
                        raise _transfer_error('s3_put', 'S3_FILE_INVALID') from None
                    time.sleep(2 ** attempt)
                save(state='uploaded')
        if current['state'] != 'committed':
            phase('s3_complete')
            committed = _post(http, f'{base}/v1/uploads/{quote(current["upload_id"], safe="")}/complete',
                              token, None, 's3-center complete', stage='s3_complete')
            if validate_session(committed, 's3_complete') != 'committed':
                raise _transfer_error('s3_complete', 'S3_RESPONSE_INVALID')
            save(state='committed')
    phase('s3_presign')
    try:
        url, metadata = presign(bucket, key, size, storage)
    except ResultTransferError as exc:
        if exc.stage:
            raise
        raise _transfer_error('s3_presign', 'S3_RESPONSE_INVALID') from None
    metadata.update(upload_id=current['upload_id'], sha256=digest, original_bytes=True,
                    content_type=content_type)
    return url, metadata
