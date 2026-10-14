"""Authenticated same-origin noVNC gateway for interactive account login."""

from __future__ import annotations

import asyncio
import secrets
import time
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import Body, Depends, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse

from .browser_login import LoginError, browser_proxy


PREFIX = "/account-login"
COOKIE = "ijm_login_viewer"
NOVNC_ROOT = Path("/usr/share/novnc")
STATIC = Path(__file__).parent / "static"
HEADERS = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
           "X-Content-Type-Options": "nosniff", "X-Frame-Options": "SAMEORIGIN",
           "Content-Security-Policy": "frame-ancestors 'self'"}


def same_origin(request, *, required: bool = False) -> bool:
    origin = request.headers.get("origin")
    if not origin:
        return not required and request.headers.get("sec-fetch-site") != "cross-site"
    source = urlsplit(origin)
    scheme = request.headers.get("x-forwarded-proto", "").split(",")[0].strip() or request.url.scheme
    scheme = {"ws": "http", "wss": "https"}.get(scheme, scheme)
    return source.scheme == scheme and source.netloc == request.headers.get("host")


def asset_path(asset: str) -> Path:
    if "\\" in asset or any(part.startswith(".") for part in Path(asset).parts):
        raise HTTPException(404)
    root = NOVNC_ROOT.resolve()
    candidate = (root / asset).resolve()
    if (not candidate.is_relative_to(root) or not candidate.is_file()
            or Path(asset).parts[0] not in {"core", "vendor"}):
        raise HTTPException(404)
    return candidate


def register_browser_login(app, manager, store, admin_auth, admin_token: str) -> None:
    def auth(request: Request, _=Depends(admin_auth)) -> None:
        if not same_origin(request):
            raise HTTPException(403, "cross-site account login is not allowed")

    def response(value: dict) -> JSONResponse:
        return JSONResponse(value, headers=HEADERS)

    def viewer(request) -> dict:
        if not same_origin(request) or not secrets.compare_digest(request.cookies.get("ijm_admin") or "", admin_token):
            raise HTTPException(403)
        lease = manager.viewer(request.cookies.get(COOKIE))
        if not lease:
            raise HTTPException(401, "login viewer has expired")
        return lease

    async def execute(operation):
        try:
            return await operation
        except LoginError as exc:
            raise HTTPException(exc.status, str(exc)) from exc

    @app.get("/api/admin/account-login", dependencies=[Depends(auth)])
    def status() -> JSONResponse:
        return response(manager.status())

    @app.post("/api/admin/accounts/pending", dependencies=[Depends(auth)])
    def create_pending(body: dict = Body(...)) -> JSONResponse:
        try:
            browser_proxy(str(body.get("proxy_url") or ""))
            account = store.create_pending_account(str(body.get("name") or ""),
                str(body.get("proxy_url") or ""), int(body.get("max_concurrency") or 1))
        except LoginError as exc:
            raise HTTPException(exc.status, str(exc)) from exc
        except (ValueError, TypeError) as exc:
            raise HTTPException(422, str(exc)) from exc
        return response(account)

    @app.post("/api/admin/accounts/{account_id}/login/start", dependencies=[Depends(auth)])
    async def start(account_id: int) -> JSONResponse:
        return response(await execute(manager.start(account_id)))

    @app.post("/api/admin/account-login/{session_id}/viewer", dependencies=[Depends(auth)])
    def issue_viewer(session_id: str, request: Request) -> JSONResponse:
        try:
            lease = manager._owned(session_id)
        except LoginError as exc:
            raise HTTPException(exc.status, str(exc)) from exc
        if lease["phase"] != "login":
            raise HTTPException(409, "login browser is not ready")
        result = response({"url": PREFIX + "/viewer"})
        secure = (request.headers.get("x-forwarded-proto") or request.url.scheme) == "https"
        result.set_cookie(COOKIE, lease["ticket"], httponly=True, secure=secure,
                          samesite="strict", path=PREFIX,
                          max_age=max(1, int(lease["deadline"] - time.monotonic())))
        return result

    @app.post("/api/admin/account-login/{session_id}/finish", dependencies=[Depends(auth)])
    async def finish(session_id: str) -> JSONResponse:
        return response(await execute(manager.finish(session_id)))

    @app.post("/api/admin/account-login/{session_id}/cancel", dependencies=[Depends(auth)])
    async def cancel(session_id: str) -> JSONResponse:
        await execute(manager.cancel(session_id))
        return response({"ok": True})

    @app.get(PREFIX + "/viewer")
    def viewer_page(request: Request) -> FileResponse:
        viewer(request)
        return FileResponse(STATIC / "account-viewer.html", headers=HEADERS)

    @app.get(PREFIX + "/credentials")
    def credentials(request: Request) -> JSONResponse:
        lease = viewer(request)
        return response({"password": lease["desktop"].password})

    @app.get(PREFIX + "/novnc/{asset:path}")
    def assets(asset: str, request: Request) -> FileResponse:
        viewer(request)
        return FileResponse(asset_path(asset), headers=HEADERS)

    connections: set[WebSocket] = set()

    @app.websocket(PREFIX + "/websockify")
    async def gateway(websocket: WebSocket) -> None:
        ticket = websocket.cookies.get(COOKIE)
        lease = manager.viewer(ticket)
        authenticated = secrets.compare_digest(websocket.cookies.get("ijm_admin") or "", admin_token)
        if not same_origin(websocket, required=True) or not authenticated or not lease or len(connections) >= 2:
            await websocket.close(code=1008)
            return
        connections.add(websocket)
        writer, tasks = None, []
        try:
            protocols = websocket.scope.get("subprotocols", [])
            await websocket.accept(subprotocol="binary" if "binary" in protocols else None)
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection("127.0.0.1", lease["desktop"].vnc_port), 3)

            async def incoming():
                while manager.viewer(ticket) is lease:
                    message = await websocket.receive_bytes()
                    if len(message) > 1024 * 1024 or manager.viewer(ticket) is not lease:
                        return
                    writer.write(message)
                    await writer.drain()

            async def outgoing():
                while manager.viewer(ticket) is lease:
                    data = await reader.read(65536)
                    if not data or manager.viewer(ticket) is not lease:
                        return
                    await websocket.send_bytes(data)

            async def expiry():
                while manager.viewer(ticket) is lease:
                    await asyncio.sleep(.5)

            tasks = [asyncio.create_task(fn()) for fn in (incoming, outgoing, expiry)]
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        except (WebSocketDisconnect, OSError, asyncio.TimeoutError, RuntimeError, KeyError):
            pass
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if writer:
                writer.close()
                try:
                    await asyncio.wait_for(writer.wait_closed(), 2)
                except (OSError, asyncio.TimeoutError):
                    pass
            connections.discard(websocket)
            try:
                await websocket.close()
            except RuntimeError:
                pass
