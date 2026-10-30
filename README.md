# IJM2API

IJM2API serves Dreamina's logged-in video workflow through a task API. Its
account pool, queue, browser login and management console follow PIP2API.
Generation defaults to the pinned official international Canvas CLI, with a
separate credential profile, private state directory, short-lived process and
HTTP/SOCKS proxy for each account. Accounts can explicitly choose the original
Chromium generation backend. The account
launcher `ijm.bat` uses port 20002, so the matching server proxy is
`socks5://host.docker.internal:20002`.

## Deploy

Create `/home/btcfoxman/docker/ijm2api/.env` from `.env.example` with unique
credentials. The `pre` branch runs `.github/workflows/deploy-test.yml`: a
GitHub-hosted runner runs the complete checks and builds and publishes the SHA
image. The `ijm2api-pre` self-hosted runner only pulls that image and restarts
port 8808 after tasks and logins become idle. Deployment preserves the existing
Compose layers, `.env`, data, Chrome profiles, and Cloudflared ingress at
`ijm2api.aiid.edu.kg`, with a database backup and image rollback. The public management
page redirects to `/login`; `/health` is unauthenticated.

Use **在线添加账号** with a name and account proxy, sign in through the embedded
noVNC browser, then click **完成登录并验证**. The account row's **网页登录**
action reopens the persisted profile. Login, content agreement, watermark
preferences, official CLI authorization and daily check-in use Chrome only
while needed; the last consumer closes Chrome and its desktop processes.
CLI generation and polling do not open Chrome. API credentials, CLI credentials
and Chrome profiles must stay outside Git.

### Canvas authorization and execution modes

Both existing and new accounts default to `canvas_cli`. Complete the existing
browser identity and preference checks, then use **CLI 授权**. The service opens
the official device authorization in that same account profile and proxy. If
confirmation requires interaction, **远端授权** opens the existing screenshot
and mouse control window; do not authorize through a different account.
The CLI identity must equal the verified browser identity before the account
can accept tasks. Login success alone does not establish CLI authorization.

The global **Canvas CLI 接单** switch defaults off. Enable it only after
verifying two independent account identities and proxy coverage, including
uploads, queries and result downloads. Unsupported model/parameter combinations
return a specific administrator diagnostic; explicitly choose `browser` for
accounts that should execute these through the old backend. No automatic
browser resubmission occurs after a CLI failure.

Management API: `POST/GET/DELETE /api/accounts/{id}/cli-auth` starts, reads or
cancels authorization. `POST /api/accounts/{id}/cli-auth/browser` opens the
remote authorization view. Cancelling a pending flow does not log out an
existing upstream session. `execution_mode` is writable on an idle account;
authorization state is service-managed.

Generation uses the same account priorities, smallest sufficient balance at
equal priority, concurrency limits, reservations and maximum two-account
credit-rejection policy as the browser backend. Canvas saves the requested
node, obtains the complete quote and runs within that credit ceiling. Upload
and query retries reuse original identities; a lost submission receipt only
queries its original operation and never starts a second paid generation.
Existing task records retain the browser backend; every new bound task stores
its backend and recovery identifiers independently of subsequent settings.
An explicit transient network failure during the initial read-only CLI identity
check may safely select the next account with the same bound execution mode,
before preparation or any submission/resource identifiers exist. This shares
the existing two-account admission budget and priority/credit-fit ordering,
retains the first error for audit, and never renews the original task deadline.
Failures after preparation starts or an unknown paid outcome do not use this
identity-only reselection path and never switch automatically to a browser.

Lost generation receipts retain the original account and submission UUID.
Three explicit `operation_submission_absent` responses, or expiry of the fixed
observation deadline, end the task as `failed` (`CLI_SUBMISSION_UNCONFIRMED`).
For known invalid-response or retryable service failures, three explicit
absence responses with no accepted receipt, generated resource or result
instead return `CLI_SUBMISSION_RETRYABLE`, allowing the caller's next route.
This includes read-only public projection of matching historical failures;
the original stored diagnostics, request and financial facts remain unchanged.
The provider context retains `may_have_been_accepted: true` and unconfirmed
spend; absence is not treated as proof of no charge. Accepted tasks, partial
receipts, identity conflicts and incomplete lookups retain their protections.
No manual verification is required, and the operation is never automatically
resubmitted. The worker and local
credit reservation are released; uncertain upstream spend remains recorded
as unconfirmed, without inventing a zero charge or a refund. The original
deadline survives restarts. Administrator task details retain the first
submission error and latest query error. `POST /api/tasks/{id}/reconcile`
only queries an original operation and cannot revive a closed failure.

Known service and invalid-response failures before generation use bounded
retries within the original deadline. Draft writes reuse their project, node
and update IDs; retries do not call `node run`. Exhausted preparation failures
return a normalized retry message so the caller can select its next route.


CLI result downloads preserve original media bytes and are published through
the existing s3-center configuration. Signed URLs are renewed on queries.
The optional `sd-2-5-480p-x` alias retains its explicit 720p-to-480p conversion.
Canvas resource `creditsAmount` is a pre-charge snapshot, not proof of final
net spend; isolated account balance evidence remains necessary for actual cost.
Cost history and upload caches are separated by execution backend.

The image build downloads official CLI 1.2.0 from its immutable release
directory and verifies SHA256 using `tools/install_canvas_cli.py`. The running
service never downloads or upgrades the CLI. Builds and full validation run on
GitHub Actions; the deployment host only pulls and restarts the built image.

Canvas embeds native ImageX/VOD clients that do not consistently honor HTTP
proxy environment variables. The image also pins upstream
[graftcp 0.8.3](https://github.com/hmgle/graftcp/releases/tag/v0.8.3), installed
by `tools/install_canvas_proxy.py` from its release archive with both archive
and executable SHA256 verification. Account CLI children use this process-local
tracer with the selected account proxy; it installs no host-wide proxy or
network rules. The upstream GPL license and source reference are included at
`/usr/local/share/doc/graftcp/`. CI verifies tracer startup before building.
The current 3.5 pre container supports tracing its own child under its existing
capabilities and default seccomp profile; `SYS_PTRACE`, `SYS_ADMIN` and
`privileged` are not required or added. A tracing/proxy failure keeps CLI
submission unavailable instead of silently falling back to a direct connection.

### Batch account login

The account page's **批量导入** accepts one account per line:

```text
person@example.com|password|socks5://host.docker.internal:20003
another@example.com|password
```

The proxy column is optional. Existing accounts retain their assigned proxy;
new accounts without a proxy use the least-used exit from ports 20001–20022
and 20031–20040 on `host.docker.internal`. Explicit proxies are retained,
validated, and never silently exchanged for another exit. The legacy
`name|proxy` format remains available for manual browser authorization.

Email/password imports queue ordinary browser logins with two concurrent jobs,
separate account profiles, and the selected proxy. The account remains disabled
until its login identity, credit read, watermark preference, and AI-content
agreement state are confirmed. The importer does not take over active video
tasks, change an existing account's identity, or overwrite another profile.
Duplicates are reported, and failed jobs can use **自动登录** to retry.
Passwords are private database fields and are excluded from management APIs,
batch results, logs, and exports. A successful import clears the form's password
textarea. Verification challenges and unknown dialogs remain available for
manual browser completion rather than reporting a false success.

The account preparation uses the observed website settings and verifies the
saved state. It never presses Generate to elicit the AI agreement dialog:
confirming that dialog may continue a pending paid generation. Video submission
also checks the saved preferences before sending its single generation request.

Admin API endpoints are `POST /api/accounts/batch-import` with `{ "text": "..." }`,
`POST /api/accounts/{id}/auto-login`, and `GET /api/accounts/login-results`.
Readiness fields are service-managed and cannot be set through account PATCH.

## API

All task endpoints require `Authorization: Bearer <IJM_API_KEY>` or
`X-API-Key`.

* `POST /v1/videos`: submit a video task.
* `GET /v1/videos/{task_id}`: poll status and retrieve `data[].url`.
* `GET /v1/videos/{task_id}/content`: redirect to the first result.
* `GET /v1/models`: list models and media limits.
* `POST /v1/responses`: submit an asynchronous image task.
* `GET /v1/responses/{response_id}`: poll its status and image URLs.

Image model names are `gpt-image-2.5-flare` and
`gpt-image-2.5-sunburst`, optionally suffixed with `-2k` or `-4k`.
Text-to-image and image edits share the OpenAI Responses input shape:
`input_text` plus optional `input_image` URLs. The image generation tool's
`ratio` accepts `1:1`, `16:9`, `9:16`, `4:3`, `3:4`, `3:2`, `2:3`, or `21:9`;
`resolution` accepts `1k`, `2k`, or `4k`. The service returns an image URL in
`output[].url` when generation completes.

Canvas CLI also supports `seedream-5.0-pro` (`1.5k`, `2k`, `4k`) and
`seedream-5.0-lite` (`2k`, `4k`), with matching resolution suffixes.
In management settings, `low_balance_image_rescue_threshold` defaults to 50;
0 disables this independent rule. It applies only to Pro requests on accounts
with a known positive balance below that threshold, including accounts
automatically disabled for low balance. Manual disabling is respected.
The separate global automatic-disable threshold is unchanged.

`low_balance_seedream_target_model` selects the actual model for those requests
and defaults to `seedream-5.0-pro`. The management page lists supported targets.
The original request is retained; task details show the actual model. The
target's resolution, input and output limits are checked before upload, and
its own upstream quote governs credit reservation and submission. Incompatible
requests are rejected without silently changing their parameters. Accepted
tasks keep their original model and upstream binding after settings change.

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
Uploaded reference audio shorter than 2 seconds is rejected before the
generation request is sent to Dreamina.

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
In explicit browser mode, credit reads before and after a task are best effort: a failed read does not
block video submission or hide a completed result. When either read fails,
that task's actual credit cost is left unknown instead of using a stale cached
balance to calculate it.
Canvas CLI requires a service-verified account identity; its credit fields are
optional in the official contract. Bounded follow-up reads can recover those
fields, but a network failure in that optional read preserves the identity
already verified in the same call. Authentication errors and mismatches still
fail, and no read may extend the task deadline. A cached available balance may
be used only for the shared admission budget when fresh credits are omitted;
the complete server quote and confirmation ceiling remain mandatory. Without
either balance, admission fails safely. Only fresh before/after readings can
establish actual spend: cached credits and resource precharges never become an
actual debit, even when a fresh balance is returned after generation.

## Evidence and protocol

The local CDP trace is in `research/captures/` and is excluded from Git.
`docs/protocol.md` contains its sanitized endpoint and model summary.
Only 2.0 Mini, 2.0 Standard, 2.5 Standard and 2.5 Preview were seen in
this trace. Fast, 1080p and 4K aliases use inferred protocol mappings and
require their own upstream acceptance checks if Dreamina changes model
availability.

## 视频消耗观测

任务管理列表新增“消耗额度”，区分账单实耗、可信独占余额差、上游报价、公式与未知值。失败任务的可信扣款也进入观测；只有明确退款凭据才冲减。历史 `actual_cost` 可能保存报价，不能直接视作真实支出。参考视频秒数使用实际上传素材元数据，解析不到时保留未知。

管理台“实时消耗表”打开 `/api/cost-reference/page`；本地 SQLite 观测缓存独立于任务清理保存，默认 30 秒刷新。配置独立的 `IJM_COST_SYNC_TOKEN` 后，聚合端可通过 `GET /internal/cost-observations?cursor=0&limit=500` 携带 `Authorization: Bearer <token>` 增量读取；普通 API 密钥不接受为成本同步密钥。导出只包含计费维度及费用证据，不含账号、提示词或素材 URL。

容器使用现有 Compose 的 `env_file: .env` 读取此密钥；把键加入部署目录 `.env` 后，在更新镜像时重新创建该服务容器。`Dockerfile` 已复制整个 `app`，费用模块及 HTML 随镜像发布，SQLite 缓存保存在挂载的 `./data`。镜像标签由发布命令临时设置 `IMAGE_REGISTRY`、`IMAGE_NAMESPACE`、`IMAGE_NAME`、`IMAGE_TAG`，不写进应用 `.env`。

该功能仅供手动路由调价参考，不提交付费测试任务，也不更改路由、调度或并发。未知额度不按零处理。
