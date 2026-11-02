"""Account-scoped Jimeng web submissions and original-submission recovery.

The Jimeng website owns authentication and request signing.  A prepared
submission is allocated before the external write so the service can durably
record its original ID even when the browser response is lost.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import random
import re
import threading
import time
import uuid
import urllib.parse
from dataclasses import dataclass, field
from typing import Any

from .browser import BrowserError, BrowserRejected, CDPBrowser


JIMENG_ORIGIN = "https://jimeng.jianying.com"
COMMON_QUERY = {"aid": "513695", "device_platform": "web", "region": "CN"}
_WORKSPACE_LOCK_GUARD = threading.Lock()
_WORKSPACE_LOCKS: dict[int, threading.RLock] = {}


@dataclass(frozen=True)
class _VideoSpec:
    upstream_model: str
    resolution: str
    max_duration: int = 15
    max_prompt: int = 4000
    seedance25: bool = False


# Exact keys and option capabilities from the authenticated CN model config.
# Commerce price rows can advertise resolutions absent from these options.
_VIDEO_MODELS = {
    "sd-2-0-mini": _VideoSpec("dreamina_seedance_40_mini", "720p"),
    "sd-2-0-fast": _VideoSpec("dreamina_seedance_40_vision", "720p"),
    "sd-2-0": _VideoSpec("dreamina_seedance_40_pro_vision", "720p"),
    "sd-2-0-1080p": _VideoSpec("dreamina_seedance_40_pro_vision", "1080p"),
    "sd-2-0-4k": _VideoSpec("dreamina_seedance_40_pro_vision", "4k"),
    "sd-2-5": _VideoSpec("dreamina_seedance_45_pro", "720p", 30, 15000, True),
    "sd-2-5-480p": _VideoSpec("dreamina_seedance_45_pro", "480p", 30, 15000, True),
    "sd-2-5-1080p": _VideoSpec("dreamina_seedance_45_pro", "1080p", 30, 15000, True),
}

# Verified against the site's repository client and four successful read calls
# on 2026-11-02.  Signing remains inside these genuine website interceptors.
_NATIVE_BOOTSTRAP = r"""
  const pending=(stage,reason)=>JSON.stringify({transport_error:'native-not-ready',
   stage,reason,did_dispatch:false});
  stage='runtime';
  if(location.origin!==__ORIGIN__)return JSON.stringify({transport_error:'wrong-site'});
  const chunks=window.rspackChunkdreamina;
  if(!chunks||chunks.push===Array.prototype.push)return pending(stage,'runtime-pending');
  let req=window.__ijm_cn_require;
  if(!req){chunks.push([['ijm-native-'+crypto.randomUUID()],{},r=>{req=r}]);
   if(req)window.__ijm_cn_require=req;}
  if(typeof req!=='function'||req.j!=='wl4')return pending(stage,'runtime-pending');
  stage='modules';
  const moduleIds=['hw','cBI','dR','qN','yF','hI','M8X','bE'];
  if(!req.m||moduleIds.some(id=>typeof req.m[id]!=='function'))
   return pending(stage,'factories-pending');
  stage='container';
  let container;
  const seen=new Set();
  for(const element of document.querySelectorAll('*')){
   const key=Object.keys(element).find(k=>k.startsWith('__reactFiber$'));
   let fiber=key&&element[key];
   while(fiber&&!seen.has(fiber)){
    seen.add(fiber);const value=fiber.memoizedProps?.value;
    if(value&&typeof value.createInstance==='function'&&typeof value.invokeFunction==='function'){
     container=value;break;}
    fiber=fiber.return;
   }
   if(container)break;
  }
  if(!container)return pending(stage,'container-pending');
  stage='profile';
  const getService=req('M8X')?.cQ,accountToken=req('bE')?.c;
  if(typeof getService!=='function'||!accountToken)return pending(stage,'profile-pending');
  const profile=getService(container,accountToken)?.userProfile;
  if(profile?.uid&&__EXPECTED_UID__&&String(profile.uid)!==__EXPECTED_UID__)
   return JSON.stringify({transport_error:'account-mismatch',stage,did_dispatch:false});
  if(!profile?.uid||(__ADD_WEB_ID__&&!profile?.webId))return pending(stage,'profile-pending');
  // Never require SDK modules until their factories and the signed-in page exist.
  // Requiring an absent Rspack module can cache its empty exports permanently.
  stage='modules';
  const hw=req('hw'),cBI=req('cBI'),dR=req('dR'),qN=req('qN'),yF=req('yF'),hI=req('hI');
  if(typeof hw?.LL!=='function'||typeof cBI?.U!=='function'||typeof dR?.vo!=='function'||
    typeof qN?.Wh!=='function'||typeof yF?.P!=='function'||typeof hI?.m!=='function')
   return pending(stage,'exports-pending');
"""

_NATIVE_READY = r"""(()=>{
 let stage='runtime';
 try{
  __BOOTSTRAP__
  return JSON.stringify({ready:true});
 }catch(_){return JSON.stringify({transport_error:'native-not-ready',stage,
  reason:'bootstrap-pending',did_dispatch:false});}
})()""".replace("__BOOTSTRAP__", _NATIVE_BOOTSTRAP)

_NATIVE_POST = r"""(async()=>{
 let stage='runtime',didDispatch=false;
 try{
  __BOOTSTRAP__
  const options=__OPTIONS__;
  if(__ADD_WEB_ID__)options.params.webId=String(profile.webId);
  stage='client-build';
  const client=new(hw.LL)().build({headers:{'Content-Type':'application/json'},withCredentials:true});
  stage='interceptors';
  cBI.U(container,client);dR.vo(client);qN.Wh(client);
  const oneShot=options.networkCtx?.reconcileBySubmitId===true;
  yF.P(client,{isRetry:e=>!oneShot&&e.config?.networkCtx?.reconcileBySubmitId!==true});hI.m(client);
  stage='request';didDispatch=true;
  const response=await client.post(__URL__,__BODY__,options);
  return JSON.stringify({http:200,body:response});
 }catch(e){
  if(e?.response)return JSON.stringify({http:e.response.status,body:e.response.data});
  if(e?.ret!==undefined)return JSON.stringify({http:200,body:e});
  if(!didDispatch&&['runtime','modules','container','profile'].includes(stage))
   return JSON.stringify({transport_error:'native-not-ready',stage,
    reason:'bootstrap-pending',did_dispatch:false});
  return JSON.stringify({transport_error:'native-client',stage,did_dispatch:didDispatch});
 }
})()""".replace("__BOOTSTRAP__", _NATIVE_BOOTSTRAP)

_NATIVE_QUOTE = r"""(async()=>{
 if(location.origin!=='https://jimeng.jianying.com')return JSON.stringify({error:'wrong-site'});
 try{
  let req=window.__ijm_cn_require;
  if(!req){const chunks=window.rspackChunkdreamina;
   if(!chunks)return JSON.stringify({error:'require-unavailable'});
   chunks.push([['ijm-quote-'+crypto.randomUUID()],{},r=>{req=r}]);
   if(req)window.__ijm_cn_require=req;}
  if(typeof req!=='function')return JSON.stringify({error:'require-unavailable'});
  let container;const seen=new Set();
  for(const element of document.querySelectorAll('*')){
   const key=Object.keys(element).find(k=>k.startsWith('__reactFiber$'));let fiber=key&&element[key];
   while(fiber&&!seen.has(fiber)){seen.add(fiber);const value=fiber.memoizedProps?.value;
    if(value&&typeof value.createInstance==='function'&&typeof value.invokeFunction==='function'){
     container=value;break;}fiber=fiber.return;}
   if(container)break;}
  if(!container)return JSON.stringify({error:'container-unavailable'});
  const input=__INPUT__,get=req('M8X').cQ;
  const profile=get(container,req('bE').c)?.userProfile;
  if(!profile?.uid||!profile?.webId||(__EXPECTED_UID__&&String(profile.uid)!==__EXPECTED_UID__))
   return JSON.stringify({error:'account-context-unavailable'});
  const profileUserId=String(profile.uid),profileWebId=String(profile.webId);
  const feature=get(container,req('yB').X).getInstanceSync();
  const models=get(container,req('nss').Y).getInstanceSync();
  if(!feature||!models)
   return JSON.stringify({error:'account-benefit-strategy-not-ready'});
  const strategy=feature.commercialStrategyService;
  if(typeof strategy?.forceUpdateBenefitStrategy!=='function')
   return JSON.stringify({error:'benefit-refresh-unavailable'});
  const refreshed=await strategy.forceUpdateBenefitStrategy();
  const currentProfile=get(container,req('bE').c)?.userProfile;
  if(refreshed?.ok!==true||feature.commercialStrategyService?.isReady!==true||
    !currentProfile?.uid||!currentProfile?.webId||String(currentProfile.uid)!==profileUserId||
    String(currentProfile.webId)!==profileWebId||
    (__EXPECTED_UID__&&String(currentProfile.uid)!==__EXPECTED_UID__))
   return JSON.stringify({error:'benefit-refresh-unverified'});
  const model=models.getVideoModelConfig(input.model);
  if(!model)return JSON.stringify({error:'model-not-in-current-config'});
  const sceneOptions=req('Gr4').I({scene:req('gu').Sk.BasicVideoGenerateButton,
   modelReqKey:input.model,modelConfig:model,resolution:input.resolution,
   videoDuration:input.duration,batchNumber:1,isRegenerate:false,
   hasInputVideo:input.refs.includes('video'),isLongVideo:false,
   chargeInputVideoDuration:req('L6W').Ge(model.commercialConfig),
   inputVideoDuration:input.inputVideoDuration,
   materialTypes:input.refs.map(kind=>({image:1,video:2,audio:3}[kind])),
   useSeedanceFast5sFreeTrial:req('LD').v({modelReqKey:input.model,
    resolution:input.resolution,videoDuration:input.duration})});
  const validateQuote=req('al')?.E;
  if(typeof validateQuote!=='function'||validateQuote(feature,sceneOptions)!==true)
   return JSON.stringify({error:'commercial-quote-unavailable'});
  const manager=container.createInstance(req('FI').E,{sceneOptions});
  const commerceInfos=await manager.getCommerceInfos();
  const quote=feature.getNeedCredits(sceneOptions);
  return JSON.stringify({input,commerceInfos,quotedCredits:quote?.credits,
   chargeInputVideoDuration:sceneOptions.chargeInputVideoDuration,
   useSeedanceFast5sFreeTrial:sceneOptions.useSeedanceFast5sFreeTrial});
 }catch(_){return JSON.stringify({error:'commerce-helper-unavailable'});}
})()"""

_CURRENT_IDENTITY = r"""(()=>{
 if(location.origin!=='https://jimeng.jianying.com')return null;
 try{
  const req=window.__ijm_cn_require;
  if(typeof req!=='function')return null;
  let container;const seen=new Set();
  for(const element of document.querySelectorAll('*')){
   const key=Object.keys(element).find(k=>k.startsWith('__reactFiber$'));let fiber=key&&element[key];
   while(fiber&&!seen.has(fiber)){seen.add(fiber);const value=fiber.memoizedProps?.value;
    if(value&&typeof value.createInstance==='function'&&typeof value.invokeFunction==='function'){
     container=value;break;}fiber=fiber.return;}
   if(container)break;}
  if(!container)return null;
  const profile=req('M8X').cQ(container,req('bE').c)?.userProfile;
  return profile?.uid?{user_id:String(profile.uid)}:null;
 }catch(_){return null;}
})()"""


def _quote_expression(input_value: dict[str, Any], expected_user_id: str) -> str:
    values = {"__INPUT__": input_value, "__EXPECTED_UID__": expected_user_id}
    return re.sub(r"__(?:INPUT|EXPECTED_UID)__", lambda match: json.dumps(
        values[match.group()], ensure_ascii=True, separators=(",", ":")), _NATIVE_QUOTE)


def _ready_expression(expected_user_id: str, *, require_web_id: bool = False) -> str:
    values = {"__ORIGIN__": JIMENG_ORIGIN, "__EXPECTED_UID__": expected_user_id,
              "__ADD_WEB_ID__": require_web_id}
    return re.sub(r"__(?:ORIGIN|EXPECTED_UID|ADD_WEB_ID)__", lambda match: json.dumps(
        values[match.group()], ensure_ascii=True, separators=(",", ":")), _NATIVE_READY)


def _request_digest(body: dict[str, Any], query: dict[str, Any]) -> str:
    encoded = json.dumps({"body": body, "query": query}, sort_keys=True,
                         ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def _native_expression(path: str, body: dict[str, Any], query: dict[str, str] | None = None, *,
                       one_shot: bool = False, expected_user_id: str = "") -> str:
    if not isinstance(path, str) or not path.startswith("/") or path.startswith("//"):
        raise ValueError("Jimeng API path must be absolute")
    params = {**COMMON_QUERY, **(query or {})}
    if (params.get("aid") != COMMON_QUERY["aid"]
            or params.get("device_platform") != "web" or params.get("region") not in {"CN", "cn"}):
        raise ValueError("Jimeng request must retain its original CN account region")
    options: dict[str, Any] = {"params": params}
    if one_shot or path == "/mweb/v1/aigc_draft/generate":
        options["networkCtx"] = {"reconcileBySubmitId": True}
    replacements = {"__ORIGIN__": JIMENG_ORIGIN, "__URL__": JIMENG_ORIGIN + path,
                    "__BODY__": body, "__OPTIONS__": options,
                    "__ACCOUNT_CONTEXT__": bool(expected_user_id) or path in {"/mweb/v1/aigc_draft/generate", "/mweb/v1/get_history_by_ids",
                                                     "/mweb/v1/workspace/list", "/mweb/v1/workspace/create",
                                                     "/mweb/v1/get_upload_token", "/commerce/v1/benefits/user_credit"},
                    "__ADD_WEB_ID__": path in {"/mweb/v1/aigc_draft/generate", "/mweb/v1/get_history_by_ids"},
                    "__EXPECTED_UID__": expected_user_id}
    return re.sub(r"__(?:ORIGIN|URL|BODY|OPTIONS|ACCOUNT_CONTEXT|ADD_WEB_ID|EXPECTED_UID)__", lambda match: json.dumps(
        replacements[match.group()], ensure_ascii=True, separators=(",", ":")), _NATIVE_POST)


def _native_response(value: Any) -> dict[str, Any]:
    try:
        response = json.loads(value)
    except (TypeError, ValueError) as exc:
        raise BrowserError("Jimeng returned an unreadable response") from exc
    if not isinstance(response, dict):
        raise BrowserError("Jimeng returned an invalid response envelope")
    if response.get("transport_error"):
        if response["transport_error"] == "wrong-site":
            raise BrowserError("Jimeng request requires the original account's Jimeng page")
        if response["transport_error"] == "account-mismatch":
            raise JimengReceiptMismatch("Jimeng signed-in account does not match this account")
        if (response["transport_error"] == "native-not-ready"
                and response.get("did_dispatch") is False
                and isinstance(response.get("stage"), str)
                and response["stage"] in {"runtime", "modules", "container", "profile"}
                and isinstance(response.get("reason"), str)
                and response["reason"] in {"runtime-pending", "factories-pending", "container-pending",
                                           "profile-pending", "exports-pending", "bootstrap-pending"}):
            raise JimengPageNotReady(response.get("stage"), response.get("reason"))
        stage = response.get("stage")
        safe_stage = stage if isinstance(stage, str) and stage in {
            "runtime", "modules", "container", "profile", "client-build", "interceptors", "request"} else "unknown"
        raise BrowserError(f"Jimeng authenticated page client failed at {safe_stage}")
    status = response.get("http")
    if isinstance(status, bool) or not isinstance(status, int):
        raise BrowserError("Jimeng returned an invalid HTTP status")
    if status >= 500 or status == 408:
        raise BrowserError(f"Jimeng HTTP {status}; original submission outcome must be queried")
    if status >= 400:
        raise BrowserRejected(f"Jimeng HTTP {status}")
    if not 200 <= status < 300:
        raise BrowserError("Jimeng returned an unexpected HTTP status")
    envelope = response.get("body")
    if not isinstance(envelope, dict) or "ret" not in envelope:
        raise BrowserError("Jimeng returned an invalid response envelope")
    if str(envelope["ret"]) != "0":
        raise BrowserRejected(f"Jimeng ret={envelope['ret']}: {str(envelope.get('errmsg') or 'upstream error')[:300]}")
    data = envelope.get("data")
    if data is None and isinstance(envelope.get("response"), str):
        try:
            data = json.loads(envelope["response"])
        except ValueError as exc:
            raise BrowserError("Jimeng returned an unreadable response payload") from exc
    if not isinstance(data, dict):
        raise BrowserError("Jimeng returned an invalid response payload")
    return data


class JimengProtocolUnavailable(BrowserError):
    """The authenticated website contract has not been established."""


class JimengReceiptMismatch(BrowserError):
    """A response does not belong to the originally dispatched submission."""


class JimengPageNotReady(BrowserError):
    """The pure bootstrap gate proved that no business POST was invoked."""

    did_dispatch = False

    def __init__(self, stage: Any = "runtime", reason: Any = "bootstrap-pending"):
        self.stage = stage if isinstance(stage, str) and stage in {
            "runtime", "modules", "container", "profile"} else "runtime"
        self.reason = reason if isinstance(reason, str) and reason in {
            "runtime-pending", "factories-pending", "container-pending",
            "profile-pending", "exports-pending", "bootstrap-pending"} else "bootstrap-pending"
        super().__init__(f"Jimeng page is not ready ({self.stage}/{self.reason}); request was not sent")


def parse_credit_state(data: dict[str, Any]) -> dict[str, Any]:
    """Parse the observed CN commerce credit buckets, rejecting missing data."""
    credit = data.get("credit") if isinstance(data, dict) else None
    if not isinstance(credit, dict):
        raise BrowserError("Jimeng credit response has no credit buckets")
    buckets: dict[str, float] = {}
    for key in ("vip_credit", "gift_credit", "purchase_credit"):
        value = credit.get(key)
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise BrowserError("Jimeng credit response has an invalid credit bucket") from exc
        if isinstance(value, bool) or not math.isfinite(number) or number < 0:
            raise BrowserError("Jimeng credit response has an invalid credit bucket")
        buckets[key] = number
    balance = sum(buckets.values())
    if not math.isfinite(balance):
        raise BrowserError("Jimeng credit response has an invalid total balance")
    return {"available_balance": balance, "buckets": buckets}


def parse_login_identity(config: dict[str, Any], info: dict[str, Any]) -> dict[str, Any]:
    """Bind a profile response to the signed configuration's own sec_uid."""
    own_sec_uid = config.get("sec_uid") if isinstance(config, dict) else None
    user_id = info.get("uid") if isinstance(info, dict) else None
    if (not isinstance(own_sec_uid, str) or not own_sec_uid.strip()
            or not isinstance(user_id, str) or not user_id.strip() or user_id == "0"
            or info.get("sec_uid") != own_sec_uid
            or isinstance(info.get("status"), bool)
            or info.get("status") not in (0, "0")):
        raise BrowserError("Jimeng authenticated identity could not be verified")
    return {"authenticated": True, "user_id": user_id, "email": ""}


def _record_identifier(value: Any) -> str:
    if isinstance(value, int) and not isinstance(value, bool) and 0 < value <= 2 ** 53 - 1:
        return str(value)
    if (isinstance(value, str) and value and len(value) <= 200
            and all(character.isalnum() or character in "._:-" for character in value)):
        return value
    return ""


def parse_submit_receipt(data: dict[str, Any], submit_id: str) -> dict[str, Any]:
    """Accept only a receipt that confirms the original body submission ID."""
    _submit_id(submit_id)
    fail_code = data.get("fail_code") if isinstance(data, dict) else None
    if fail_code not in (None, "", 0, "0"):
        raise BrowserRejected(f"Jimeng generation rejected: {fail_code}")
    record = data.get("aigc_data") if isinstance(data, dict) else None
    task = record.get("task") if isinstance(record, dict) else None
    if (not isinstance(task, dict) or task.get("submit_id") != submit_id
            or not _record_identifier(task.get("task_id"))
            or (record.get("submit_id") and record["submit_id"] != submit_id)):
        raise JimengReceiptMismatch("Jimeng response did not confirm the original submission ID")
    return {"run_id": submit_id,
            "thread_id": _record_identifier(record.get("history_record_id") or task.get("history_id")),
            "upstream_task_id": _record_identifier(task["task_id"])}


def parse_history_response(data: dict[str, Any], submit_id: str, *,
                           history_id: str = "", expected_user_id: str = "") -> dict[str, Any]:
    """Read only the original submission; an absent record remains unresolved."""
    _submit_id(submit_id)
    if not isinstance(data, dict):
        raise BrowserError("Jimeng history response is invalid")
    record = data.get(submit_id)
    if record is None:
        return {"status": "running", "result_urls": [], "error": "", "error_code": "",
                "record_found": False}
    if not isinstance(record, dict):
        raise BrowserError("Jimeng history record is invalid")
    task = record.get("task")
    if (not isinstance(task, dict) or task.get("submit_id") != submit_id
            or (record.get("submit_id") and record["submit_id"] != submit_id)
            or (expected_user_id and str(record.get("uid") or "") != expected_user_id)):
        raise JimengReceiptMismatch("Jimeng history does not belong to the original submission")
    record_history_id = _record_identifier(record.get("history_record_id") or task.get("history_id"))
    if history_id and record_history_id and history_id != record_history_id:
        raise JimengReceiptMismatch("Jimeng history ID changed for the original submission")
    raw_status = task.get("status", record.get("status", 0))
    try:
        state = int(raw_status)
    except (ValueError, TypeError) as exc:
        raise BrowserError("Jimeng history returned an invalid status") from exc
    if isinstance(raw_status, bool) or (isinstance(raw_status, float) and raw_status != state):
        raise BrowserError("Jimeng history returned an invalid status")
    urls: list[str] = []
    for item in record.get("item_list") or []:
        if not isinstance(item, dict):
            continue
        video = item.get("video")
        if not isinstance(video, dict):
            continue
        transcoded = video.get("transcoded_video")
        origin = transcoded.get("origin") if isinstance(transcoded, dict) else None
        if not isinstance(origin, dict):
            continue
        url = origin.get("video_url")
        if isinstance(url, str) and url.startswith("https://") and url not in urls:
            urls.append(url)
    response_ret = task.get("resp_ret") or {}
    response_ret = response_ret.get("ret") if isinstance(response_ret, dict) else response_ret
    error_code = str(record.get("fail_code") or response_ret or "")
    error = next((str(record[key]).strip() for key in ("fail_starling_message", "fail_msg", "fail_starling_key")
                  if record.get(key) is not None and str(record[key]).strip().lower()
                  not in {"", "[none]", "none", "null", "undefined"}), "")
    # These values are the CN website's L4.A enum, captured on 2026-11-02.
    status = ("failed" if state in {10, 30, 40, 100} or error_code not in {"", "0"}
              else "succeeded" if state == 50 and urls else "running")
    result = {"status": status, "state": state, "result_urls": urls,
              "error": error, "error_code": error_code, "record_found": True,
              "upstream_task_id": _record_identifier(task.get("task_id")),
              "history_id": record_history_id}
    if status in {"succeeded", "failed"}:
        from .client import history_task_credits
        evidence = history_task_credits(record, submit_id, status)
        if evidence is not None:
            result["task_credit_evidence"] = evidence
    return result


def _submit_id(value: str | None = None) -> str:
    if value is None:
        return str(uuid.uuid4())
    if not isinstance(value, str):
        raise ValueError("Jimeng submission ID must be a UUID")
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError) as exc:
        raise ValueError("Jimeng submission ID must be a UUID") from exc
    if str(parsed) != value:
        raise ValueError("Jimeng submission ID must be a canonical UUID")
    return value


def preflight_model(normalized: dict[str, Any]) -> None:
    """Reject unsupported requests before account admission or external writes."""
    _video_request(normalized)
    requested = {kind for kind in ("images", "videos", "audios") if normalized.get(kind)}
    if requested:
        from .jimeng_upload import SUPPORTED_UPLOAD_KINDS
        if not requested.issubset(SUPPORTED_UPLOAD_KINDS):
            raise JimengProtocolUnavailable("Jimeng upload contract is unavailable for these references")


def _video_request(normalized: dict[str, Any]) -> _VideoSpec:
    spec = _VIDEO_MODELS.get(normalized.get("model"))
    if normalized.get("kind") == "image" or spec is None:
        raise JimengProtocolUnavailable("Jimeng generation contract is unavailable for this model")
    counts = {}
    for kind, maximum in (("images", 30 if spec.seedance25 else 9),
                          ("videos", 10 if spec.seedance25 else 3),
                          ("audios", 10 if spec.seedance25 else 3)):
        sources = normalized.get(kind) or []
        if not isinstance(sources, list) or any(not isinstance(source, str) for source in sources):
            raise ValueError("Jimeng references must be arrays of media sources")
        counts[kind] = len(sources)
        if counts[kind] > maximum:
            raise ValueError(f"Jimeng {normalized['model']} supports at most {maximum} {kind}")
    if sum(counts.values()) > (50 if spec.seedance25 else 12):
        raise ValueError("Jimeng total media reference count exceeds this model's limit")
    if not spec.seedance25 and counts["audios"] and not counts["images"] and not counts["videos"]:
        raise ValueError("Jimeng Seedance 2.0 audio references require an image or video reference")
    duration = normalized.get("duration")
    if isinstance(duration, bool) or not isinstance(duration, int) or not 4 <= duration <= spec.max_duration:
        raise ValueError(f"Jimeng {normalized['model']} duration must be 4–{spec.max_duration} seconds")
    if normalized.get("resolution") != spec.resolution:
        raise ValueError(f"Jimeng {normalized['model']} supports {spec.resolution}")
    if normalized.get("aspect_ratio") not in {"21:9", "16:9", "4:3", "1:1", "3:4", "9:16"}:
        raise ValueError("unsupported Jimeng video aspect ratio")
    prompt = normalized.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > spec.max_prompt:
        raise ValueError(f"Jimeng {normalized['model']} requires a prompt of at most {spec.max_prompt} characters")
    return spec


def _video_materials(normalized: dict[str, Any], spec: _VideoSpec,
                      assets: dict[str, list[dict[str, Any]]] | None) -> tuple[list[dict[str, Any]], dict[str, list[int]], float]:
    assets = assets or {}
    materials: list[dict[str, Any]] = []
    index_map: dict[str, list[int]] = {"images": [], "videos": [], "audios": []}
    video_seconds = 0.0
    for kind, singular in (("images", "image"), ("videos", "video"), ("audios", "audio")):
        values = assets.get(kind) or []
        if not isinstance(values, list) or len(values) != len(normalized.get(kind) or []):
            raise ValueError("Jimeng uploaded references do not match the requested media")
        total = 0.0
        for item in values:
            if not isinstance(item, dict):
                raise ValueError("Jimeng uploaded reference metadata is invalid")
            index_map[kind].append(len(materials))
            material = {"type": "", "id": str(uuid.uuid4()), "material_type": singular}
            info: dict[str, Any] = {"type": singular, "id": str(uuid.uuid4()), "source_from": "upload"}
            if kind == "images":
                uri = item.get("image_uri")
                width, height = item.get("width"), item.get("height")
                if (not isinstance(uri, str) or not uri or type(width) is not int or width <= 0
                        or type(height) is not int or height <= 0):
                    raise ValueError("Jimeng uploaded image metadata is incomplete")
                info.update(platform_type=1, name="", image_uri=uri, width=width, height=height,
                            format="", title="", uri=uri)
            else:
                vid, seconds = item.get("vid"), item.get("duration")
                if (not isinstance(vid, str) or not vid or isinstance(seconds, bool)
                        or not isinstance(seconds, (int, float)) or not math.isfinite(seconds)):
                    raise ValueError("Jimeng uploaded media metadata is incomplete")
                minimum = 1.8 if spec.seedance25 else 2.0
                maximum = 30.2 if spec.seedance25 else 15.4 if kind == "videos" else 15.0
                if not minimum <= seconds <= maximum:
                    raise ValueError(f"Jimeng {singular} references must be {minimum:g}–{maximum:g} seconds")
                total += seconds
                info.update(vid=vid, duration=round(seconds * 1000), name="")
                if kind == "videos":
                    width, height, fps = item.get("width"), item.get("height"), item.get("fps", 0)
                    if (type(width) is not int or type(height) is not int or width <= 0 or height <= 0
                            or isinstance(fps, bool) or not isinstance(fps, (int, float))
                            or not math.isfinite(fps) or fps < 0):
                        raise ValueError("Jimeng uploaded video dimensions or frame rate are incomplete")
                    info.update(fps=fps, width=width, height=height, cover_image_url="")
            material[singular + "_info"] = info
            materials.append(material)
        if kind != "images":
            maximum_total = 30.2 if spec.seedance25 else 15.4 if kind == "videos" else 15.0
            if total > maximum_total + 1e-9:
                raise ValueError(f"Jimeng total {singular} reference duration exceeds {maximum_total:g} seconds")
        if kind == "videos":
            video_seconds = round(total, 3)
    return materials, index_map, video_seconds


def build_video_submission(normalized: dict[str, Any], *, workspace_id: int,
                               commerce: dict[str, Any], submit_id: str | None = None,
                               trace_id: str | None = None, quoted_credits: float | None = None,
                               charge_input_video: bool = True,
                               use_fast_trial: bool = False,
                               owner_account_id: int = 0,
                               assets: dict[str, list[dict[str, Any]]] | None = None) -> PreparedJimengSubmission:
    """Build the CN schema with its documented serializer feature rules."""
    spec = _video_request(normalized)
    materials, index_map, video_seconds = _video_materials(normalized, spec, assets)
    submit_id = _submit_id(submit_id)
    if isinstance(workspace_id, bool) or not isinstance(workspace_id, int) or not 0 < workspace_id <= 2 ** 53 - 1:
        raise ValueError("Jimeng generation requires the original account's selected workspace")
    if (not isinstance(commerce, dict) or commerce.get("resource_id") != "generate_video"
            or commerce.get("resource_id_type") != "str" or commerce.get("resource_sub_type") != "aigc"
            or not isinstance(commerce.get("benefit_type"), str)
            or not commerce["benefit_type"] or len(commerce["benefit_type"]) > 200):
        raise JimengProtocolUnavailable("Jimeng generation requires a verified commercial key")
    amount = commerce.get("amount")
    if isinstance(amount, bool) or not isinstance(amount, (int, float)) or not math.isfinite(amount) or amount <= 0:
        raise JimengProtocolUnavailable("Jimeng generation requires a verified commercial amount")
    duration = normalized["duration"]
    model, resolution = spec.upstream_model, spec.resolution
    # Fo.DAVideoGenInput.calculateMinVersion / jM.FeaturesMinVersion.
    min_version = "3.3.9" if materials else "3.2.4" if resolution == "1080p" else "3.0.5"
    new_id = lambda: str(uuid.uuid4())
    scene = {"type": "video", "scene": "BasicVideoGenerateButton", "resolution": resolution,
             "modelReqKey": model, "videoDuration": duration, "batchNumber": 1,
             "inputVideoDuration": video_seconds, "chargeInputVideoDuration": charge_input_video, "isLongVideo": False,
             "hasInputVideo": bool(index_map["videos"]), "useSeedanceFast5sFreeTrial": use_fast_trial,
             "reportParams": {"enterSource": "generate", "vipSource": "generate",
                              "extraVipFunctionKey": model + "-" + resolution,
                              "useVipFunctionDetailsReporterHoc": True, "cashierBusinessExtra": {}},
             "materialTypes": [{"image": 1, "video": 2, "audio": 3}[item["material_type"]] for item in materials]}
    encode = lambda value: json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    metrics = {"isDefaultSeed": 1, "originSubmitId": submit_id, "isRegenerate": False,
               "enterFrom": "click", "position": "page_bottom_box", "promptType": "original_prompt",
               "functionMode": "omni_reference", "generatorFeature": "omniReference",
               "sceneOptions": encode([scene]), "batchNumber": 1,
               "submitGroupId": new_id(), "hasRejectedAudit": 0}
    component_id = new_id()
    params = {"type": "", "id": new_id(),
              "video_gen_inputs": [{"type": "", "id": new_id(), "min_version": min_version,
                                    "prompt": "" if materials else normalized["prompt"], "video_mode": 2, "fps": 24,
                                    "duration_ms": duration * 1000, "resolution": resolution,
                                    "idip_meta_list": []}],
              "video_aspect_ratio": normalized["aspect_ratio"],
              "seed": random.randint(1, 2 ** 32 - 1), "model_req_key": model, "priority": 0}
    if materials:
        from .client import build_meta_list
        params["video_gen_inputs"][0]["unified_edit_input"] = {
            "type": "", "id": new_id(), "material_list": materials,
            "meta_list": build_meta_list(normalized["prompt"], index_map)}
    draft = {"type": "draft", "id": new_id(), "min_version": min_version,
             "min_features": (["AIGC_Video_UnifiedEdit"] if materials else [])
                             + (["AIGC_Video_Seedance25ResultAction"] if spec.seedance25 else []),
             "is_from_tsn": True, "version": "3.3.36", "main_component_id": component_id,
             "component_list": [{"type": "video_base_component", "id": component_id,
                                 "min_version": "1.0.0", "aigc_mode": "workbench",
                                 "metadata": {"type": "", "id": new_id(), "created_platform": 3,
                                              "created_platform_version": "",
                                              "created_time_in_ms": str(round(time.time() * 1000)),
                                              "created_did": ""},
                                 "generate_type": "gen_video",
                                 "abilities": {"type": "", "id": new_id(), "gen_video": {
                                     "type": "", "id": new_id(), "text_to_video_params": params,
                                     "video_task_extra": encode(metrics)}}, "process_type": 1}]}
    body = {"extend": {"root_model": model, "workspace_id": workspace_id,
                       "m_video_commerce_info": copy.deepcopy(commerce),
                       "m_video_commerce_info_list": [copy.deepcopy(commerce)]},
            "submit_id": submit_id, "metrics_extra": encode(metrics), "draft_content": encode(draft),
            "http_common_info": {"aid": 513695}}
    query = {"region": "cn", "da_version": "3.3.36", "web_component_open_flag": "1",
             "commerce_with_input_video": "1", "web_version": "7.5.0", "aigc_features": "app_lip_sync"}
    roi = {"feature_entrance": "create", "feature_entrance_detail": "create-text_to_video",
           "feature_key": "text_to_video", "scenario": "image_video_generation", "edit_type": "tool",
           "tool_id": "tool_video", "sub_tool_id": "tool_video", "tab_name": "tool", "enter_from": "tool",
           "template_id": "", "scene_lv1": "tool", "scene_lv2": "tool_video",
           "extra_param": {"model_id": model, "generate_type": "t2v"}}
    query["babi_param"] = urllib.parse.quote(encode(roi), safe="~!*'()")
    return PreparedJimengSubmission(submit_id, "video", body, query, trace_id=trace_id or "",
                                    quoted_credits=quoted_credits, owner_account_id=owner_account_id)


def build_mini_text_submission(normalized: dict[str, Any], **kwargs: Any) -> PreparedJimengSubmission:
    if normalized.get("model") != "sd-2-0-mini":
        raise ValueError("the mini fixture builder requires sd-2-0-mini")
    return build_video_submission(normalized, **kwargs)


@dataclass(frozen=True)
class PreparedJimengSubmission:
    """One immutable identity and a detached request body for one paid write."""

    submit_id: str
    kind: str
    body: dict[str, Any] = field(repr=False)
    query: dict[str, str] = field(default_factory=dict, repr=False)
    trace_id: str = ""
    quoted_credits: float | None = None
    owner_account_id: int = 0
    request_digest: str = field(init=False, repr=False)

    def __post_init__(self) -> None:
        _submit_id(self.submit_id)
        if self.kind not in {"video", "image"}:
            raise ValueError("Jimeng submission kind must be video or image")
        if (isinstance(self.owner_account_id, bool) or not isinstance(self.owner_account_id, int)
                or self.owner_account_id < 0):
            raise ValueError("Jimeng submission account binding is invalid")
        if not isinstance(self.body, dict) or self.body.get("submit_id") != self.submit_id:
            raise ValueError("Jimeng request body must use its original submission ID")
        if not isinstance(self.query, dict):
            raise ValueError("Jimeng request query must be an object")
        query = dict(self.query)
        trace_id = self.trace_id or query.get("generate_id") or "gen-" + str(uuid.uuid4())
        if not isinstance(trace_id, str) or not trace_id.startswith("gen-"):
            raise ValueError("Jimeng trace ID must use the observed gen-UUID format")
        _submit_id(trace_id[4:])
        if "generate_id" in query and query["generate_id"] != trace_id:
            raise ValueError("Jimeng query must retain its original trace ID")
        query["generate_id"] = trace_id
        object.__setattr__(self, "body", copy.deepcopy(self.body))
        object.__setattr__(self, "query", query)
        object.__setattr__(self, "trace_id", trace_id)
        if self.quoted_credits is not None:
            if (isinstance(self.quoted_credits, bool) or not isinstance(self.quoted_credits, (int, float))
                    or not math.isfinite(self.quoted_credits) or self.quoted_credits < 0):
                raise ValueError("Jimeng quote must be a finite nonnegative credit amount")
        object.__setattr__(self, "request_digest", _request_digest(self.body, query))

    def receipt_context(self, account_id: int, *, phase: str = "prepared") -> dict[str, Any]:
        """Persist only ownership and identity, never prompts or credentials."""
        if isinstance(account_id, bool) or not isinstance(account_id, int) or account_id < 1:
            raise ValueError("Jimeng submission requires a valid account ID")
        if self.owner_account_id and self.owner_account_id != account_id:
            raise ValueError("Jimeng submission must retain its original account")
        if phase not in {"prepared", "dispatching", "submitted", "accepted"}:
            raise ValueError("invalid Jimeng submission phase")
        context = {"backend": "jimeng_web", "site": "jimeng", "account_id": account_id,
                   "submit_id": self.submit_id, "trace_id": self.trace_id,
                   "kind": self.kind, "submission_phase": phase, "request_digest": self.request_digest}
        workspace_id = (self.body.get("extend") or {}).get("workspace_id")
        if workspace_id:
            context["workspace_id"] = workspace_id
        if self.quoted_credits is not None:
            context["quoted_credits"] = self.quoted_credits
            context["quote_source"] = "native_get_need_credits"
            scene = json.loads(json.loads(self.body["metrics_extra"])["sceneOptions"])[0]
            context["quote_bound"] = {"model_req_key": scene["modelReqKey"],
                                      "resolution": scene["resolution"],
                                      "duration_seconds": scene["videoDuration"], "batch_number": 1,
                                      "input_video_duration_seconds": scene["inputVideoDuration"],
                                      "material_types": [{1: "image", 2: "video", 3: "audio"}[kind]
                                                         for kind in scene["materialTypes"]]}
        return context


class JimengWebClient:
    """Transport through the original account's authenticated Jimeng page."""

    def __init__(self, account: dict[str, Any], *, store: Any = None):
        self.account = dict(account)
        self.store = store
        self.browser = CDPBrowser(int(account["cdp_port"]), site="jimeng")
        self._submission_lock = threading.Lock()
        self._dispatched: set[str] = set()

    @staticmethod
    def preflight_model(normalized: dict[str, Any]) -> None:
        preflight_model(normalized)

    def wait_until_ready(self, *, timeout_seconds: float = 30,
                         poll_interval_seconds: float = 0.25,
                         require_web_id: bool = False) -> None:
        """Wait only on the pure page bootstrap; never replay a business POST."""
        if (isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float))
                or not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 30):
            raise ValueError("Jimeng readiness timeout must be between 0 and 30 seconds")
        if (isinstance(poll_interval_seconds, bool) or not isinstance(poll_interval_seconds, (int, float))
                or not math.isfinite(poll_interval_seconds) or not 0 < poll_interval_seconds <= 1):
            raise ValueError("Jimeng readiness poll interval must be between 0 and 1 seconds")
        original_timeout = self.browser.timeout
        deadline = time.monotonic() + min(timeout_seconds, original_timeout)
        expression = _ready_expression(str(self.account.get("login_user_id") or ""),
                                       require_web_id=require_web_id)
        last_pending = JimengPageNotReady("runtime", "runtime-pending")
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise last_pending
                self.browser.timeout = min(original_timeout, remaining)
                raw = self.browser.evaluate(expression)
                try:
                    response = json.loads(raw)
                except (TypeError, ValueError) as exc:
                    raise BrowserError("Jimeng returned an unreadable readiness result") from exc
                if isinstance(response, dict) and response.get("ready") is True:
                    return
                try:
                    _native_response(raw)
                except JimengPageNotReady as exc:
                    last_pending = exc
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise
                    time.sleep(min(poll_interval_seconds, remaining))
                    continue
                raise BrowserError("Jimeng returned an invalid readiness result")
        finally:
            self.browser.timeout = original_timeout

    def login_identity(self) -> dict[str, Any]:
        config = self._post("/mweb/v1/get_common_config", {})
        sec_uid = config.get("sec_uid")
        if not isinstance(sec_uid, str) or not sec_uid.strip():
            raise BrowserError("Jimeng authenticated identity could not be verified")
        info = self._post("/mweb/v1/get_user_info", {"sec_uid": sec_uid})
        identity = parse_login_identity(config, info)
        current = self.browser.evaluate(_CURRENT_IDENTITY)
        if not isinstance(current, dict) or current.get("user_id") != identity["user_id"]:
            raise JimengReceiptMismatch("Jimeng account changed while its identity was being verified")
        self.account["login_user_id"] = identity["user_id"]
        return identity

    def _post(self, path: str, body: dict[str, Any], *,
              query: dict[str, str] | None = None, one_shot: bool = False) -> dict[str, Any]:
        # A paid write must reach its single bootstrap gate immediately, without
        # sleeping past the service's final cancellation/dispatch boundary.
        if not one_shot and path != "/mweb/v1/aigc_draft/generate":
            self.wait_until_ready(require_web_id=path == "/mweb/v1/get_history_by_ids")
        expression = _native_expression(path, body, query, one_shot=one_shot,
                                         expected_user_id=str(self.account.get("login_user_id") or ""))
        return _native_response(self.browser.evaluate(expression, await_promise=True))

    def model_config(self) -> dict[str, Any]:
        return self._post("/mweb/v1/creation_agent/v2/get_agent_config", {})

    def workspace_id(self) -> int:
        """Bind only an explicitly selected workspace in the signed own list."""
        self.wait_until_ready()
        account_id = int(self.account["id"])
        with _WORKSPACE_LOCK_GUARD:
            lock = _WORKSPACE_LOCKS.setdefault(account_id, threading.RLock())
        with lock:
            saved = self.store.account(account_id, private=True) if self.store else None
            bound = str((saved or self.account).get("project_id") or "").strip()
            selected = self.browser.evaluate(
                "(()=>{if(location.origin!=='https://jimeng.jianying.com')return '';"
                "return new URL(location.href).searchParams.get('workspace')||''})()")
            candidate = bound or (selected if isinstance(selected, str) else "")
            if not candidate:
                if (not self.store or not callable(getattr(self.store, "claim_jimeng_workspace_creation", None))
                        or not callable(getattr(self.store, "bind_jimeng_workspace", None))):
                    raise JimengProtocolUnavailable("Jimeng workspace creation requires durable account storage")
                marker = "jimeng-create-pending:" + str(uuid.uuid4())
                if not self.store.claim_jimeng_workspace_creation(account_id, marker):
                    raise BrowserError("Jimeng workspace initialization already has an original operation; verify that workspace")
                # This ordinary workspace write is also never retried.  A lost
                # response retains its durable marker instead of making twins.
                created = self._post("/mweb/v1/workspace/create", {"name": "IJM API", "scene": "generate"},
                                     query={"da_version": "3.3.36", "web_version": "7.5.0"}, one_shot=True)
                candidate = _record_identifier(created.get("workspace_id"))
                if (not candidate.isascii() or not candidate.isdigit()
                        or not 0 < int(candidate) <= 2 ** 53 - 1):
                    raise BrowserError("Jimeng workspace creation returned no usable workspace ID; verify its original operation")
                if not self.store.bind_jimeng_workspace(account_id, marker, candidate):
                    raise BrowserError("Jimeng account workspace binding changed during initialization")
                bound = candidate
            if (not candidate.isdigit() or not 0 < int(candidate) <= 2 ** 53 - 1):
                raise JimengProtocolUnavailable("select an owned Jimeng workspace before generation")
            offset = 0
            for _ in range(10):
                data = self._post("/mweb/v1/workspace/list", {"offset": offset, "limit": 50},
                                  query={"da_version": "3.3.36", "web_version": "7.5.0"})
                workspaces = data.get("workspaces")
                if not isinstance(workspaces, list):
                    raise BrowserError("Jimeng workspace ownership response is invalid")
                if any(isinstance(workspace, dict) and str(workspace.get("workspace_id")) == candidate
                       for workspace in workspaces):
                    if self.store and not bound:
                        marker = "jimeng-create-pending:" + str(uuid.uuid4())
                        if (not self.store.claim_jimeng_workspace_creation(account_id, marker)
                                or not self.store.bind_jimeng_workspace(account_id, marker, candidate)):
                            current = self.store.account(account_id, private=True) or {}
                            if str(current.get("project_id") or "") != candidate:
                                raise BrowserError("Jimeng account workspace binding changed during initialization")
                    self.account["project_id"] = candidate
                    return int(candidate)
                if not data.get("has_more") or not workspaces:
                    break
                offset += len(workspaces)
            raise BrowserError("the selected Jimeng workspace does not belong to the authenticated account")

    def account_state(self, *, max_attempts: int = 3,
                      timeout_seconds: float | None = None) -> dict[str, Any]:
        if isinstance(max_attempts, bool) or not isinstance(max_attempts, int) or not 1 <= max_attempts <= 3:
            raise ValueError("Jimeng credit read attempts must be between 1 and 3")
        original_timeout = self.browser.timeout
        if timeout_seconds is not None:
            if isinstance(timeout_seconds, bool) or not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
                raise ValueError("Jimeng credit read timeout must be positive")
            self.browser.timeout = timeout_seconds
        try:
            for attempt in range(max_attempts):
                try:
                    return parse_credit_state(self._post("/commerce/v1/benefits/user_credit", {}))
                except (BrowserRejected, JimengPageNotReady, JimengReceiptMismatch):
                    raise
                except BrowserError:
                    if attempt + 1 == max_attempts:
                        raise
                    time.sleep(0.5 * 2 ** attempt)
        finally:
            self.browser.timeout = original_timeout

    def upload_inputs(self, normalized: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
        preflight_model(normalized)
        if any(normalized.get(kind) for kind in ("images", "videos", "audios")):
            self.wait_until_ready()
            from .jimeng_upload import JimengUploader
            return JimengUploader(self.account, post=self._post, store=self.store).upload_inputs(normalized)
        return {"images": [], "videos": [], "audios": []}

    def prepare_submit(self, normalized: dict[str, Any],
                       assets: dict[str, list[dict[str, Any]]], *,
                       submit_id: str | None = None) -> PreparedJimengSubmission:
        preflight_model(normalized)
        spec = _video_request(normalized)
        if not isinstance(assets, dict):
            raise ValueError("Jimeng uploaded references must be an object")
        materials, _, video_seconds = _video_materials(normalized, spec, assets)
        self.wait_until_ready(require_web_id=True)
        workspace_id = self.workspace_id()
        input_value = {"model": spec.upstream_model, "duration": normalized["duration"],
                       "resolution": spec.resolution,
                       "refs": [item["material_type"] for item in materials],
                       "inputVideoDuration": video_seconds}
        raw = self.browser.evaluate(_quote_expression(input_value,
            str(self.account.get("login_user_id") or "")), await_promise=True)
        try:
            quote = json.loads(raw)
        except (TypeError, ValueError) as exc:
            raise JimengProtocolUnavailable("Jimeng current commercial quote is unavailable") from exc
        if (not isinstance(quote, dict) or quote.get("error") or quote.get("input") != input_value
                or not isinstance(quote.get("commerceInfos"), list) or len(quote["commerceInfos"]) != 1
                or not isinstance(quote.get("chargeInputVideoDuration"), bool)
                or not isinstance(quote.get("useSeedanceFast5sFreeTrial"), bool)):
            raise JimengProtocolUnavailable("Jimeng current commercial quote could not be verified")
        credits = quote.get("quotedCredits")
        if (isinstance(credits, bool) or not isinstance(credits, (int, float))
                or not math.isfinite(credits) or credits < 0):
            raise JimengProtocolUnavailable("Jimeng current credit quote is missing or invalid")
        info = quote["commerceInfos"][0]
        if not isinstance(info, dict):
            raise JimengProtocolUnavailable("Jimeng current commercial key is invalid")
        fields = {"benefitType": "benefit_type", "resourceId": "resource_id",
                  "resourceIdType": "resource_id_type", "resourceSubType": "resource_sub_type",
                  "amount": "amount"}
        commerce = {target: info.get(source) for source, target in fields.items()}
        return build_video_submission(normalized, workspace_id=workspace_id, commerce=commerce,
            submit_id=submit_id, quoted_credits=credits,
            charge_input_video=quote["chargeInputVideoDuration"],
            use_fast_trial=quote["useSeedanceFast5sFreeTrial"], owner_account_id=int(self.account["id"]),
            assets=assets)

    def submit_prepared(self, prepared: PreparedJimengSubmission) -> dict[str, Any]:
        if (not isinstance(prepared, PreparedJimengSubmission) or prepared.kind != "video"
                or prepared.quoted_credits is None
                or not prepared.owner_account_id or prepared.owner_account_id != int(self.account["id"])
                or (prepared.body.get("extend") or {}).get("root_model") not in {
                    spec.upstream_model for spec in _VIDEO_MODELS.values()}
                or _request_digest(prepared.body, prepared.query) != prepared.request_digest):
            raise JimengProtocolUnavailable("Jimeng prepared generation or quote is invalid")
        with self._submission_lock:
            if prepared.submit_id in self._dispatched:
                raise BrowserError("Jimeng original submission was already dispatched; query its original ID")
            self._dispatched.add(prepared.submit_id)
        data = self._post("/mweb/v1/aigc_draft/generate", prepared.body,
                          query=prepared.query, one_shot=True)
        return parse_submit_receipt(data, prepared.submit_id)

    def poll(self, submit_id: str, history_id: str = "") -> dict[str, Any]:
        _submit_id(submit_id)
        data = self._post("/mweb/v1/get_history_by_ids", {"submit_ids": [submit_id]},
                          query={"region": "cn", "da_version": "3.3.36",
                                 "web_version": "7.5.0", "aigc_features": "app_lip_sync"})
        return parse_history_response(data, submit_id, history_id=history_id,
                                      expected_user_id=str(self.account.get("login_user_id") or ""))
