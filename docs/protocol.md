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
No explicit daily claim was captured; account check-in uses the
`commerce/v1/benefits/credit_receive` endpoint and is disabled by
default until an account opts in.
