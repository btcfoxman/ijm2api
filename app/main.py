"""IJM2API public video API and AK2API-style management console."""

from __future__ import annotations

from .cost_adapter import build_observations

import os
import secrets
import hashlib
import hmac
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Body, Cookie, Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from .browser import BrowserError, CDPBrowser, browser_alive, clear_managed_profile, start_managed_browser, stop_managed_browser
from .browser_login import BrowserLoginManager, LoginError
from .browser_login_api import register_browser_login
from .account_import import AccountImporter
from .auto_login import AutoLoginManager
from .catalog import public_models
from .image_client import IMAGE_MODELS
from .service import Service
from .store import ACCOUNT_SECRET_FIELDS, ACCOUNT_SETUP_FIELDS, Store


API_KEY = os.getenv("IJM_API_KEY", "")
ADMIN_TOKEN = os.getenv("IJM_ADMIN_TOKEN", "")
SYNC_TOKEN = os.getenv("IJM_SYNC_TOKEN", "")
if not API_KEY or not ADMIN_TOKEN:
    raise RuntimeError("IJM_API_KEY and IJM_ADMIN_TOKEN must be configured")
ADMIN_COOKIE = hmac.new(ADMIN_TOKEN.encode(), b"ijm2api-admin-session", hashlib.sha256).hexdigest()

DATA_DIR = Path(os.getenv("IJM_DATA_DIR", "/app/data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
store = Store(str(DATA_DIR / "ijm2api.db"))
service = Service(store)
browser_login = BrowserLoginManager(store, service)
auto_login = AutoLoginManager(store, service, manual_active=lambda: browser_login.active)
service.auto_login_busy = auto_login.busy
STATIC = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(_: FastAPI):
    auto_login.recover_interrupted()
    service.start()
    browser_login.start_maintenance()
    try:
        yield
    finally:
        auto_login.close()
        service.stop()
        await browser_login.close()


app = FastAPI(title="IJM2API", version="0.1.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


def _bearer(authorization: str | None, x_api_key: str | None) -> str:
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return str(x_api_key or "").strip()


def api_auth(authorization: str | None = Header(default=None),
             x_api_key: str | None = Header(default=None)) -> None:
    if not secrets.compare_digest(_bearer(authorization, x_api_key), API_KEY):
        raise HTTPException(status_code=401, detail="invalid API key")


def _admin_authorized(ijm_admin: str | None, x_admin_token: str | None) -> bool:
    header_ok = bool(x_admin_token) and secrets.compare_digest(x_admin_token, ADMIN_TOKEN)
    cookie_ok = bool(ijm_admin) and secrets.compare_digest(ijm_admin, ADMIN_COOKIE)
    return header_ok or cookie_ok


def admin_auth(ijm_admin: str | None = Cookie(default=None),
               x_admin_token: str | None = Header(default=None)) -> None:
    if not _admin_authorized(ijm_admin, x_admin_token):
        raise HTTPException(status_code=401, detail="admin login required")


register_browser_login(app, browser_login, store, admin_auth, ADMIN_COOKIE)


def sync_auth(authorization: str | None = Header(default=None),
              x_api_key: str | None = Header(default=None)) -> None:
    value = _bearer(authorization, x_api_key)
    if not any(secrets.compare_digest(value, item) for item in (SYNC_TOKEN, API_KEY) if item):
        raise HTTPException(status_code=401, detail="invalid sync token")


def _account(account_id: int, *, private: bool = False) -> dict[str, Any]:
    value = store.account(account_id, private=private)
    if not value:
        raise HTTPException(status_code=404, detail="account not found")
    return value


async def _close_online_account(account_id: int) -> None:
    try:
        await browser_login.close_account(account_id)
    except LoginError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc


def _task(task_id: str) -> dict[str, Any]:
    value = store.task(task_id)
    if not value:
        raise HTTPException(status_code=404, detail="task not found")
    return value


@app.get("/health")
def health() -> dict[str, Any]:
    return {"status": "ok", "service": "ijm2api", "models": len(public_models())}


@app.get("/login")
def login_page(error: str | None = None) -> HTMLResponse:
    notice = "<p role='alert'>Invalid admin token. Please try again.</p>" if error == "invalid" else ""
    return HTMLResponse("""<!doctype html><meta charset='utf-8'><title>IJM2API sign in</title>
    <style>body{font:16px system-ui;background:#f4f5f8;display:grid;place-items:center;min-height:95vh}
    form{background:white;padding:32px;border-radius:16px;box-shadow:0 12px 40px #0002;min-width:300px}
    input,button{box-sizing:border-box;width:100%;padding:12px;margin-top:14px;border-radius:8px}
    button{background:#4450d4;color:white;border:0;cursor:pointer}</style>
    <form action='/login' method='post'><h1>IJM2API</h1>""" + notice + """
    <label>Admin token<input name='token' type='password' autofocus required></label>
    <button type='submit'>Sign in</button></form>""")


@app.post("/login")
async def login(request: Request) -> RedirectResponse:
    form = await request.form()
    if not secrets.compare_digest(str(form.get("token") or ""), ADMIN_TOKEN):
        return RedirectResponse("/login?error=invalid", status_code=303)
    response = RedirectResponse("/", status_code=303)
    response.set_cookie("ijm_admin", ADMIN_COOKIE, httponly=True, samesite="strict",
                        secure=request.url.scheme == "https", max_age=30 * 86400)
    return response


@app.post("/logout")
def logout() -> RedirectResponse:
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie("ijm_admin")
    return response


@app.get("/")
def home(ijm_admin: str | None = Cookie(default=None),
         x_admin_token: str | None = Header(default=None)):
    if not _admin_authorized(ijm_admin, x_admin_token):
        return RedirectResponse("/login", status_code=303)
    return FileResponse(STATIC / "index.html")


@app.get("/api/settings", dependencies=[Depends(admin_auth)])
def settings() -> dict[str, Any]:
    return service.public_settings()


@app.patch("/api/settings", dependencies=[Depends(admin_auth)])
def update_settings(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    try:
        return service.update_settings(body)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/api/accounts", dependencies=[Depends(admin_auth)])
def accounts() -> list[dict[str, Any]]:
    return store.accounts()


@app.post("/api/accounts", dependencies=[Depends(admin_auth)])
def add_account(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    if (ACCOUNT_SECRET_FIELDS | ACCOUNT_SETUP_FIELDS) & body.keys():
        raise HTTPException(status_code=422, detail='use batch import for login credentials; setup state is service-managed')
    try:
        with service._lock:
            existing = next((item for item in store.accounts(private=True)
                if item['name'] == str(body.get('name') or '').strip()), None)
            if existing:
                _assert_account_mutable(existing, body)
            result = store.upsert_account(body)
            if existing and 'enabled' in body:
                result = store.update_account(existing['id'], {'login_enable_after': False if body['enabled'] is False else None})
            return result
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/api/accounts/batch-import", dependencies=[Depends(admin_auth)])
def batch_import(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    with service._lock:
        result = AccountImporter(store, login_busy=auto_login.busy).import_text(str(body.get('text') or ''))
        for row in result['results']:
            if not row.get('login_needed'):
                continue
            try:
                account = auto_login.start(int(row['account_id']))
                row.update(status=account['status'], login_stage=account.get('login_stage', ''),
                           message='已加入自动登录队列')
                result['login_started_count'] += 1
            except (ValueError, BrowserError):
                row['message'] = '账号已保存，自动登录尚未启动，请检查占用状态后重试'
                result['errors'].append(f"第 {row['line']} 行：账号已保存，自动登录尚未启动")
        return result


@app.post('/api/accounts/{account_id}/auto-login', dependencies=[Depends(admin_auth)], status_code=202)
def start_auto_login(account_id: int) -> dict[str, Any]:
    try:
        return auto_login.start(account_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail='account not found') from exc
    except (ValueError, BrowserError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get('/api/accounts/login-results', dependencies=[Depends(admin_auth)])
def account_login_results() -> dict[str, Any]:
    return {'jobs': [{'account_id': account['id'], 'status': account['status'],
        'phase': account.get('login_stage', ''), 'message': account.get('last_error', ''),
        'watermark_ready': account.get('watermark_ready', False),
        'agreement_ready': account.get('agreement_ready', False)}
        for account in store.accounts() if account.get('login_stage')]}


def _assert_account_mutable(account: dict[str, Any], body: dict[str, Any]) -> None:
    """Call under service._lock to protect the browser identity during work."""
    account_id = int(account['id'])
    if (auto_login.busy(account_id) or account['status'] == 'logging_in'
            or browser_login.active and browser_login.active['account_id'] == account_id):
        raise HTTPException(status_code=409, detail='account is in browser login')
    identity = {'cdp_port', 'proxy_url', 'project_id', 'managed_profile'}
    if any(key in body and body[key] != account.get(key) for key in identity):
        if service._active.get(account_id, 0) or store.active_account_count(account_id):
            raise HTTPException(status_code=409, detail='account has active tasks')


@app.patch("/api/accounts/{account_id}", dependencies=[Depends(admin_auth)])
async def patch_account(account_id: int, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    try:
        if auto_login.busy(account_id):
            raise HTTPException(status_code=409, detail='account is in automatic login')
        if (ACCOUNT_SECRET_FIELDS | ACCOUNT_SETUP_FIELDS) & body.keys():
            raise HTTPException(status_code=422, detail='use batch import for login credentials; setup state is service-managed')
        if 'enabled' in body:
            body = {**body, 'login_enable_after': False if body['enabled'] is False else None}
        account = _account(account_id, private=True)
        if "proxy_url" in body and str(body["proxy_url"]).strip() != str(account["proxy_url"]).strip():
            if browser_login.active and browser_login.active["account_id"] == account_id:
                raise HTTPException(status_code=409, detail="finish or cancel online login first")
            port = int(account["cdp_port"] or 0)
            if port and port != 19300 + account_id:
                if not account.get("managed_profile"):
                    raise HTTPException(status_code=409, detail="relaunch the external account browser with the new proxy first")
            await _close_online_account(account_id)
            stop_managed_browser(account_id)
            body = {**body, "cdp_port": 0, "status": "pending", "last_error": ""}
        with service._lock:
            _assert_account_mutable(_account(account_id, private=True), body)
            return store.update_account(account_id, body)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="account not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.delete("/api/accounts/{account_id}", dependencies=[Depends(admin_auth)])
async def delete_account(account_id: int) -> dict[str, Any]:
    if auto_login.busy(account_id):
        raise HTTPException(status_code=409, detail='account is in automatic login')
    if browser_login.active and browser_login.active["account_id"] == account_id:
        raise HTTPException(status_code=409, detail="finish or cancel online login first")
    if any(task["account_id"] == account_id and task["status"] not in {"succeeded", "failed", "expired"}
           for task in store.tasks(10000)):
        raise HTTPException(status_code=409, detail="account has active tasks")
    account = _account(account_id, private=True)
    await _close_online_account(account_id)
    clear_managed_profile(account, Path(str(service.settings()["chrome_user_data_root"])))
    deleted = store.delete_account(account_id)
    return {"deleted": deleted}


@app.post("/api/accounts/{account_id}/profile/reset", dependencies=[Depends(admin_auth)])
async def reset_profile(account_id: int, body: dict[str, Any] = Body(default={})) -> dict[str, Any]:
    if auto_login.busy(account_id):
        raise HTTPException(status_code=409, detail='account is in automatic login')
    if browser_login.active and browser_login.active["account_id"] == account_id:
        raise HTTPException(status_code=409, detail="finish or cancel online login first")
    account = _account(account_id, private=True)
    if any(task["account_id"] == account_id and task["status"] not in {"succeeded", "failed", "expired"}
           for task in store.tasks(10000)):
        raise HTTPException(status_code=409, detail="account has active tasks")
    await _close_online_account(account_id)
    clear_managed_profile(account, Path(str(service.settings()["chrome_user_data_root"])))
    store.clear_media_cache(account_id)
    values: dict[str, Any] = {"cdp_port": 0, "project_id": "", "status": "pending",
                              "last_error": "", "login_stage": "", "watermark_ready": False,
                              "agreement_ready": False, "setup_checked_at": None, "login_enable_after": None,
                              "login_user_id": ""}
    if body.get("proxy_url") is not None:
        values["proxy_url"] = str(body["proxy_url"])
    result = store.update_account(account_id, values)
    if body.get("start_login"):
        service.check_account(account_id, start_browser=True)
        result = _account(account_id)
    return result


@app.post("/api/accounts/{account_id}/check", dependencies=[Depends(admin_auth)])
@app.post("/api/accounts/{account_id}/balance", dependencies=[Depends(admin_auth)])
def check_account(account_id: int) -> dict[str, Any]:
    _account(account_id)
    try:
        return service.check_account(account_id)
    except BrowserError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/accounts/{account_id}/cdp/reconnect", dependencies=[Depends(admin_auth)])
def reconnect_account(account_id: int) -> dict[str, Any]:
    _account(account_id)
    try:
        return service.check_account(account_id, start_browser=True)
    except BrowserError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/accounts/{account_id}/checkin", dependencies=[Depends(admin_auth)])
def checkin_account(account_id: int) -> dict[str, Any]:
    _account(account_id)
    try:
        return service.checkin_account(account_id)
    except BrowserError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


def _browser(account_id: int, *, start: bool = False) -> tuple[dict[str, Any], CDPBrowser]:
    try:
        account = service.open_browser_view(account_id) if start else service.browser_view(account_id)
    except (BrowserError, KeyError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    port = int(account["cdp_port"] or 0)
    if not port:
        raise HTTPException(status_code=409, detail="account browser is not configured")
    return account, CDPBrowser(port)


def _frame(browser: CDPBrowser) -> dict[str, Any]:
    try:
        image = browser.screenshot()
        viewport = browser.evaluate("({width:innerWidth,height:innerHeight})")
        width, height = int(viewport["width"]), int(viewport["height"])
        return {"image": image, "width": width, "height": height,
                "challenge_required": False, "has_login_form": True}
    except BrowserError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/accounts/{account_id}/browser/open", dependencies=[Depends(admin_auth)])
def browser_open(account_id: int) -> dict[str, Any]:
    return _frame(_browser(account_id, start=True)[1])


@app.get("/api/accounts/{account_id}/browser", dependencies=[Depends(admin_auth)])
def browser_frame(account_id: int) -> dict[str, Any]:
    return _frame(_browser(account_id)[1])


@app.post("/api/accounts/{account_id}/browser/action", dependencies=[Depends(admin_auth)])
def browser_action(account_id: int, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    browser = _browser(account_id)[1]
    action = str(body.get("action") or "")
    try:
        if action == "click":
            frame = _frame(browser)
            browser.click(float(body.get("x") or 0) * frame["width"],
                          float(body.get("y") or 0) * frame["height"])
        elif action == "scroll":
            browser.scroll(float(body.get("delta_y") or 0))
        elif action == "type":
            value = str(body.get("text") or "")
            browser.command("Input.insertText", {"text": value})
        elif action == "key":
            key = str(body.get("key") or "")
            if key not in {"Enter", "Tab", "Backspace", "Escape"}:
                raise ValueError("unsupported browser key")
            browser.command("Input.dispatchKeyEvent", {"type": "keyDown", "key": key})
            browser.command("Input.dispatchKeyEvent", {"type": "keyUp", "key": key})
        else:
            raise ValueError("unsupported browser action")
        return {"action_result": {"phase": "done"}}
    except (BrowserError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/api/accounts/{account_id}/browser/complete", dependencies=[Depends(admin_auth)])
def browser_complete(account_id: int) -> dict[str, Any]:
    result = service.check_account(account_id)
    if result["status"] != "active":
        raise HTTPException(status_code=409, detail=result["last_error"] or "Dreamina login is incomplete")
    return result


@app.post("/api/accounts/{account_id}/browser/close", dependencies=[Depends(admin_auth)])
def browser_close(account_id: int) -> dict[str, Any]:
    _account(account_id)
    service.close_browser_view(account_id)
    return {"ok": True}


@app.get("/api/models", dependencies=[Depends(admin_auth)])
def admin_models() -> list[dict[str, Any]]:
    return [model for model in public_models() if model["id"] != "sd-2-5-480p-x" or
            service.settings()["enable_sd_2_5_480p_x"]]


@app.get("/v1/models", dependencies=[Depends(api_auth)])
def models() -> dict[str, Any]:
    image_models = [{"id": model + suffix, "object": "model", "owned_by": "ijmapi"}
                    for model in IMAGE_MODELS for suffix in ("", "-2k", "-4k")]
    return {"object": "list", "data": admin_models() + image_models}


@app.get("/api/model-costs", dependencies=[Depends(admin_auth)])
def model_costs() -> list[dict[str, Any]]:
    return store.model_costs()


@app.get("/api/tasks", dependencies=[Depends(admin_auth)])
def admin_tasks(limit: int = 50) -> list[dict[str, Any]]:
    tasks = store.tasks(max(1, min(limit, 10000)))
    for task in tasks:
        if task["status"] in {"failed", "expired"}:
            task["public_error"] = service.public_task(task)["error"]
    return [_with_cost_observations(task) for task in tasks]


@app.post("/api/tasks", dependencies=[Depends(admin_auth)])
def admin_create_task(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    return _create(body)


@app.get("/api/tasks/{task_id}", dependencies=[Depends(admin_auth)])
def admin_task(task_id: str) -> dict[str, Any]:
    task = _task(task_id)
    if task["status"] in {"failed", "expired"}:
        task["public_error"] = service.public_task(task)["error"]
    return _with_cost_observations(task)


@app.delete("/api/tasks", dependencies=[Depends(admin_auth)])
def clear_tasks() -> dict[str, Any]:
    return {"deleted": store.clear_terminal_tasks()}


@app.get("/api/integration-docs", dependencies=[Depends(admin_auth)])
def integration_docs() -> PlainTextResponse:
    models = ", ".join(item["id"] for item in admin_models())
    return PlainTextResponse(f"IJM2API / IJMAPI\n\nPOST /v1/videos\nGET /v1/videos/{{task_id}}\n"
                             "POST /v1/responses (image)\nGET /v1/responses/{response_id}\n"
                             f"GET /v1/models\n\nBearer: IJM_API_KEY\n\nModels: {models}\n"
                             "Request fields: model, prompt, duration, aspect_ratio, "
                             "image_urls, video_urls, audio_urls.\n")


def _create(body: dict[str, Any]) -> dict[str, Any]:
    try:
        result = service.create(body)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc),
                            headers={"X-IJMAPI-Submit-Outcome": "not-accepted"}) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=429, detail=str(exc),
                            headers={"X-IJMAPI-Submit-Outcome": "not-accepted"}) from exc
    if body.get("background") is False:
        deadline = time.monotonic() + min(900, int(service.settings()["task_timeout_seconds"]))
        while time.monotonic() < deadline:
            task = _task(result["id"])
            if task["status"] in {"succeeded", "failed", "expired"}:
                return service.public_task(task)
            time.sleep(2)
    return result


@app.post("/v1/responses", dependencies=[Depends(api_auth)])
def create_image_response(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    try:
        return service.create_image(body)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc),
                            headers={"X-IJMAPI-Submit-Outcome": "not-accepted"}) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=429, detail=str(exc),
                            headers={"X-IJMAPI-Submit-Outcome": "not-accepted"}) from exc


@app.get("/v1/responses/{response_id}", dependencies=[Depends(api_auth)])
def get_image_response(response_id: str) -> dict[str, Any]:
    task = _task(response_id)
    if task["normalized"].get("kind") != "image":
        raise HTTPException(status_code=404, detail="image response not found")
    return service.image_response(task)


@app.post("/v1/videos", dependencies=[Depends(api_auth)])
@app.post("/v1/videos/generations", dependencies=[Depends(api_auth)])
@app.post("/api/videos/generate", dependencies=[Depends(api_auth)])
def create_video(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    return _create(body)


@app.get("/v1/videos/{task_id}", dependencies=[Depends(api_auth)])
@app.get("/api/videos/{task_id}", dependencies=[Depends(api_auth)])
def get_video(task_id: str) -> dict[str, Any]:
    return service.public_task(_task(task_id))


@app.get("/v1/videos/{task_id}/content", dependencies=[Depends(api_auth)])
def get_video_content(task_id: str) -> RedirectResponse:
    task = _task(task_id)
    if task["status"] != "succeeded" or not task["result_urls"]:
        raise HTTPException(status_code=409, detail="video is not ready")
    return RedirectResponse(service.public_task(task)["data"][0]["url"], status_code=307)


@app.post("/api/v3/contents/generations/tasks", dependencies=[Depends(api_auth)])
def create_generic(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    return _create(body)


@app.get("/api/v3/contents/generations/tasks/{task_id}", dependencies=[Depends(api_auth)])
def get_generic(task_id: str) -> dict[str, Any]:
    return service.public_task(_task(task_id))


def _with_cost_observations(task: dict[str, Any]) -> dict[str, Any]:
    full_task = task
    return {**task, "cost_observations": [{key: value for key, value in row.items() if not key.startswith("_")}
                                        for row in build_observations(full_task or task)]}


from .cost_reference import install_cost_reference

cost_reference = install_cost_reference(
    app, service_name="ijm2api", database_path=store.path,
    admin_dependency=admin_auth, adapter=build_observations,
    token_env="IJM_COST_SYNC_TOKEN",
)
