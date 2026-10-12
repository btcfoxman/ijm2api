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

Add an account with its proxy in the console, open **Web login**, sign in
and click **Save login session**. Existing sessions can also be copied into
the account's managed browser profile by an administrator. API credentials
and Chrome profiles must stay outside Git.

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
References such as `视频1`, `video2`, `v3`, `视4`, `图片1`,
`图1`, `img2`, `Image3`, `Audio1` and `音频2` become Dreamina
`meta_list` media references by one-based index.

The console tracks exact observed credit deltas by model, duration,
resolution and media count. Account concurrency defaults to 1 and can be
raised to 50; global worker and queue limits are configurable. A full queue
returns HTTP 429 before submitting upstream. An interrupted upstream submit
is marked outcome unknown and is not automatically repeated.

## Evidence and protocol

The local CDP trace is in `research/captures/` and is excluded from Git.
`docs/protocol.md` contains its sanitized endpoint and model summary.
Only 2.0 Mini, 2.0 Standard, 2.5 Standard and 2.5 Preview were seen in
this trace. Fast, 1080p and 4K aliases use inferred protocol mappings and
require their own upstream acceptance checks if Dreamina changes model
availability.
