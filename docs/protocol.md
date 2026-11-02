# Dreamina CDP protocol record

Captured on 2026-10-12 from the logged-in `ijm.bat` Chromium session.
The local JSONL capture stores endpoint names, parameter keys and body
shapes without cookies, tokens, prompt text or media contents.

| Step | Endpoint | Observed behavior |
| --- | --- | --- |
| Credit balance | `POST commerce-api-sg.capcut.com/commerce/v1/benefits/user_credit` | Returns VIP, gift and purchase credit buckets. |
| Upload credential | `POST mweb-api-sg.capcut.com/mweb/v1/get_upload_token` | `scene=2` for ImageX and `scene=1` for VOD. |
| Image upload | `ApplyImageUpload` → TOS `/upload/v1/…` → `CommitImageUpload` | Signed ImageX API with temporary credential; committed URI is the image reference. |
| Video/audio upload | `ApplyUploadInner` → TOS `/upload/v1/…` → `CommitUploadInner` | Signed VOD API; committed VID, duration and dimensions form media references. |
| Generate | `POST mweb-api-sg.capcut.com/mweb/v1/aigc_draft/generate` | Draft contains `video_gen_inputs[0].unified_edit_input.material_list` and `meta_list`. |
| Poll | `POST mweb-api-sg.capcut.com/mweb/v1/get_history_by_ids` | Query by `submit_ids`; successful `item_list[].video.transcoded_video.origin.video_url`. |
| Detail | `POST mweb-api-sg.capcut.com/mweb/v1/get_local_item_list` | Result detail and video download data. |

Observed upstream keys: 2.0 Mini `dreamina_seedance_40_mini`;
2.0 Standard `dreamina_seedance_40_pro`;
2.5 Standard `dreamina_seedance_45_pro`;
2.5 Preview `dreamina_seedance_45_pro_draft`.
The separate 2.0 Fast key `dreamina_seedance_40` is inferred from
the international Dreamina protocol, not from this session's completed
task history.

The observed 2.5 Standard request used 4 images, 2 audio clips and one
2.02-second video. Its `material_list` order was image, video, audio,
and `meta_list` used `material_ref.material_idx` to bind references.
The service extends this structure to the documented 30/10/10 limits.
Daily claim transport was verified on 2026-11-02 against the original signed-in
Dreamina page: ordinary `XMLHttpRequest` to
`commerce/v1/benefits/credit_receive`, JSON `time_zone`, credentials enabled.
The website's installed SDK supplies its normal request context. Bare `fetch`
was rejected with `34070104`; legacy webpack module IDs were unavailable.
No signatures or credential values are copied or constructed by the service.
Before claiming, a bounded 20-second read-only gate waits for the current
document's naturally completed benefit metadata request on the selected
verified commerce origin. It checks only signature query-key presence,
then rechecks identity and account policy. Timeout sends no claim request;
the final XHR evaluation checks readiness again without replaying a request.
The receipt must have HTTP success, `ret=0`, a finite nonnegative
`receive_quota`, and matching account identity. Request and response must stay
on the selected verified SG/US commerce origin and exact endpoint path.

Daily maintenance remains opt-in. Its independent queue selects at most two
naturally idle accounts. Queued maintenance does not block new generation;
only the actual claim holds the account, and existing tasks are never interrupted.
Transport failures use a three-attempt short retry burst, then a persisted
one-hour cooldown before another bounded burst on the same day. Explicit
`34070104`, unclassified rejections and verification failures use the long
cooldown immediately. Explicit `34010100` and HTTP 5xx responses use the
bounded short burst. Private state and logs retain numeric error codes,
without raw provider messages or request context.
Confirmed same-day receipts permit only bounded quota refresh, never another
claim. Receipt day and request timezone both use `IJM_TIME_ZONE`.

After confirmation, fresh website quota buckets update the credit aggregate.
A later CLI aggregate is independent of its optional breakdown: omitted
details retain a marked historical snapshot and cannot invent gift credits or
erase the confirmed receipt. Gift consumption or expiry is not proof of a
missing daily claim. Jimeng CN daily claiming remains unsupported.
