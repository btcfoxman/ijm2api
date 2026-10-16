# IJM2API

IJM2API serves Dreamina's logged-in video workflow through a task API. Its
account pool, queue, browser login and management console follow PIP2API.
Dreamina requests use each account's native Chromium session; ImageX and VOD
uploads use the same account's configured SOCKS/HTTP proxy. The account
launcher `ijm.bat` uses port 20002, so the matching server proxy is
`socks5://host.docker.internal:20002`.

## Deploy

Create `/home/btcfoxman/docker/ijm2api/.env` from `.env.example` with unique
credentials. The `pre` branch runs `.github/workflows/deploy-test.yml` on the
`ijm2api-pre` self-hosted runner, builds the Docker image, starts port 8808,
and installs `ijm2api.aiid.edu.kg` into cloudflared. The public management
page redirects to `/login`; `/health` is unauthenticated.

Use **在线添加账号** with a name and account proxy, sign in through the embedded
noVNC browser, then click **完成登录并验证**. The account row's **网页登录**
action reconnects to that same Chrome. The verified browser stays running;
video submission, polling, credit checks and daily check-in share its CDP
session. A service restart restores it from the persisted profile. Existing
sessions can also be copied into the account's managed profile by an
administrator. API credentials and Chrome profiles must stay outside Git.

## API

All task endpoints require `Authorization: Bearer <IJM_API_KEY>` or
`X-API-Key`.

* `POST /v1/videos`: submit a video task.
* `GET /v1/videos/{task_id}`: poll status and retrieve `data[].url`.
* `GET /v1/videos/{task_id}/content`: redirect to the first result.
* `GET /v1/models`: list models and media limits.

Example:

```json
{
  "model": "sd-2-5",
  "prompt": "Use 图片1, 音频1 and 视频1 as references.",
  "duration": 4,
  "aspect_ratio": "16:9",
  "image_urls": ["https://example.com/reference.png"],
  "audio_urls": ["https://example.com/sound.wav"],
  "video_urls": ["https://example.com/reference.mp4"]
}
```

Model aliases: `sd-2-0`, `sd-2-0-1080p`, `sd-2-0-4k`,
`sd-2-0-fast`, `sd-2-0-mini`, `sd-2-5`, `sd-2-5-480p`,
`sd-2-5-1080p`, and `sd-2-5-480p-preview`. Unsuffixed aliases
default to 720p. Seedance 2.0 accepts up to 9 images, 3 audio clips and
3 videos; Seedance 2.5 accepts up to 30, 10 and 10 respectively.

`sd-2-5-480p-x` is an opt-in result-processing model. Enable it and configure
the **S3 result storage** fields in the management console. The container
environment variables `IJM_S3_CENTER_API_BASE`, `IJM_S3_CENTER_TOKEN`,
`IJM_S3_CENTER_BUCKET`, and `IJM_S3_CENTER_KEY_PREFIX` provide initial values.
The default bucket is `assets` and the object prefix is `aicontent/ai-sdijm`;
the API base and token can match Lingya2API's s3-center direct settings.
The console never returns the stored Bearer token; leave the password field
empty to retain it.
IJM2API submits Seedance 2.5 at 720p using 720p credits, downloads the
finished video through the selected account's proxy, then crops and scales it
to the requested 480p aspect specification (for example, 1:1 is 640×640).
It preserves audio, uploads the new MP4 to s3-center, and returns the
presigned temporary URL. Querying the
IJM2API task again refreshes that URL when it is close to expiry. If transfer
fails, the task reports `RESULT_TRANSFER_FAILED` and keeps the upstream URL
in its private task record for diagnosis; it never returns the 720p source
as a 480p result.

The processed output dimensions follow the requested aspect ratio:

| Aspect ratio | Output pixels |
| --- | --- |
| 16:9 | 864×496 |
| 4:3 | 752×560 |
| 1:1 | 640×640 |
| 3:4 | 560×752 |
| 9:16 | 496×864 |
| 21:9 | 992×432 |
| 9:21 | 432×992 |

References such as `视频1`, `video2`, `v3`, `视4`, `图片1`,
`图1`, `img2`, `Image3`, `Audio1` and `音频2` become Dreamina
`meta_list` media references by one-based index.
If a prompt names a reference beyond the supplied media count, IJM2API
keeps that label as ordinary prompt text by default. Enable **Strict prompt
media references** in runtime settings to reject such requests before upload.

The console tracks exact observed credit deltas by model, duration,
resolution and media count. Account concurrency defaults to 1 and can be
raised to 50; global worker and queue limits are configurable. A full queue
returns HTTP 429 before submitting upstream. An interrupted upstream submit
is marked outcome unknown and is not automatically repeated.
Credit reads before and after a task are best effort: a failed read does not
block video submission or hide a completed result. When either read fails,
that task's actual credit cost is left unknown instead of using a stale cached
balance to calculate it.

## Evidence and protocol

The local CDP trace is in `research/captures/` and is excluded from Git.
`docs/protocol.md` contains its sanitized endpoint and model summary.
Only 2.0 Mini, 2.0 Standard, 2.5 Standard and 2.5 Preview were seen in
this trace. Fast, 1080p and 4K aliases use inferred protocol mappings and
require their own upstream acceptance checks if Dreamina changes model
availability.
