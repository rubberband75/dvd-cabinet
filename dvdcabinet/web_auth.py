"""Sign-in for the web API: session cookies, the first-run "claim this server" step, and guards.

Every request carries request["user"] (None when signed out). Handlers are wrapped
with require_user / require_admin. Anything that changes state, and every player
WebSocket, must come from our own origin: SameSite cookies plus an Origin check
keep other websites from acting with a signed-in visitor's cookie.
"""

from __future__ import annotations

import asyncio
import functools
import ipaddress
import json
import logging

from aiohttp import web

from .accounts import AccountError, User
from .appkeys import ACCOUNTS

log = logging.getLogger(__name__)

COOKIE = "dvdcabinet_session"
COOKIE_MAX_AGE = 30 * 86400


def _is_private(ip: str | None) -> bool:
    try:
        addr = ipaddress.ip_address(ip or "")
    except ValueError:
        return False
    return addr.is_private or addr.is_loopback or addr.is_link_local


def client_ip(request: web.Request) -> str:
    """The viewer's address. A proxy on the local network (nginx) passes it in X-Real-IP;
    those headers are only believed when the connection itself comes from such a proxy."""
    peer = request.remote or ""
    if _is_private(peer):
        forwarded = request.headers.get("X-Real-IP") or request.headers.get("X-Forwarded-For", "").split(",")[0]
        if forwarded.strip():
            return forwarded.strip()
    return peer


def is_local(request: web.Request) -> bool:
    return _is_private(client_ip(request))


def _is_https(request: web.Request) -> bool:
    if request.secure:
        return True
    return _is_private(request.remote) and request.headers.get("X-Forwarded-Proto", "").lower() == "https"


def json_error(status: type[web.HTTPException], message: str) -> web.HTTPException:
    return status(text=json.dumps({"error": message}), content_type="application/json")


@web.middleware
async def auth_middleware(request: web.Request, handler):
    request["user"] = None
    token = request.cookies.get(COOKIE)
    if token:
        request["user"] = request.app[ACCOUNTS].session_user(token)
    unsafe = request.method not in ("GET", "HEAD", "OPTIONS")
    if unsafe or request.headers.get("Upgrade", "").lower() == "websocket":
        origin = request.headers.get("Origin")
        # Browsers always send Origin on these requests; other clients can't carry a victim's cookie.
        if origin and origin.split("://", 1)[-1] != request.host:
            raise json_error(web.HTTPForbidden, "Cross-site request refused.")
    return await handler(request)


def require_user(handler):
    @functools.wraps(handler)
    async def wrapper(request: web.Request):
        if request["user"] is None:
            raise json_error(web.HTTPUnauthorized, "Please sign in.")
        return await handler(request)
    return wrapper


def require_admin(handler):
    @functools.wraps(handler)
    async def wrapper(request: web.Request):
        user: User | None = request["user"]
        if user is None:
            raise json_error(web.HTTPUnauthorized, "Please sign in.")
        if not user.is_admin:
            raise json_error(web.HTTPForbidden, "Only admins can do that.")
        return await handler(request)
    return wrapper


async def read_json(request: web.Request) -> dict:
    try:
        body = await request.json()
    except ValueError:
        raise json_error(web.HTTPBadRequest, "Expected a JSON body.") from None
    if not isinstance(body, dict):
        raise json_error(web.HTTPBadRequest, "Expected a JSON object.")
    return body


def _signed_in(request: web.Request, user: User) -> web.Response:
    token = request.app[ACCOUNTS].create_session(user.id)
    resp = web.json_response({"user": user.to_json()})
    resp.set_cookie(COOKIE, token, max_age=COOKIE_MAX_AGE, httponly=True, samesite="Lax",
                    secure=_is_https(request), path="/")
    return resp


async def run_blocking(fn, *args):
    """Password hashing takes ~50 ms; keep it off the event loop."""
    return await asyncio.get_running_loop().run_in_executor(None, fn, *args)


# ---- routes ------------------------------------------------------------------------


async def auth_state(request: web.Request) -> web.Response:
    accounts = request.app[ACCOUNTS]
    setup = accounts.needs_setup()
    user = request["user"]
    return web.json_response({
        "setup_required": setup,
        "setup_code_required": setup and not is_local(request),
        "user": user.to_json() if user else None,
    })


async def setup(request: web.Request) -> web.Response:
    accounts = request.app[ACCOUNTS]
    body = await read_json(request)
    ip = client_ip(request)
    if not accounts.needs_setup():
        raise json_error(web.HTTPConflict, "This server has already been claimed.")
    if not is_local(request):
        if accounts.throttle.blocked(f"ip:{ip}"):
            raise json_error(web.HTTPTooManyRequests, "Too many attempts. Try again in a few minutes.")
        code = str(body.get("setup_code", "")).strip().upper().replace(" ", "")
        if code != accounts.setup_code:
            accounts.throttle.failed(f"ip:{ip}")
            raise json_error(web.HTTPForbidden, "That setup code isn't right. It's shown in the server's log.")
    try:
        user = await run_blocking(accounts.claim, str(body.get("username", "")), str(body.get("password", "")))
    except AccountError as exc:
        raise json_error(web.HTTPBadRequest, str(exc)) from None
    log.info("server claimed by %r from %s", user.username, ip)
    return _signed_in(request, user)


async def login(request: web.Request) -> web.Response:
    accounts = request.app[ACCOUNTS]
    body = await read_json(request)
    username = str(body.get("username", "")).strip()
    keys = (f"ip:{client_ip(request)}", f"user:{username.lower()}")
    if accounts.throttle.blocked(*keys):
        raise json_error(web.HTTPTooManyRequests, "Too many failed sign-ins. Try again in a few minutes.")
    user = await run_blocking(accounts.authenticate, username, str(body.get("password", "")))
    if user is None:
        accounts.throttle.failed(*keys)
        log.info("failed sign-in for %r from %s", username, client_ip(request))
        raise json_error(web.HTTPUnauthorized, "Wrong username or password.")
    accounts.throttle.succeeded(*keys)
    return _signed_in(request, user)


async def logout(request: web.Request) -> web.Response:
    token = request.cookies.get(COOKIE)
    if token:
        request.app[ACCOUNTS].end_session(token)
    resp = web.json_response({"ok": True})
    resp.del_cookie(COOKIE, path="/")
    return resp


@require_user
async def change_password(request: web.Request) -> web.Response:
    accounts = request.app[ACCOUNTS]
    body = await read_json(request)
    user: User = request["user"]
    keys = (f"user:{user.username.lower()}",)
    if accounts.throttle.blocked(*keys):
        raise json_error(web.HTTPTooManyRequests, "Too many attempts. Try again in a few minutes.")
    if await run_blocking(accounts.authenticate, user.username, str(body.get("current", ""))) is None:
        accounts.throttle.failed(*keys)
        raise json_error(web.HTTPForbidden, "Your current password isn't right.")
    try:
        await run_blocking(accounts.set_password, user.id, str(body.get("new", "")))
    except AccountError as exc:
        raise json_error(web.HTTPBadRequest, str(exc)) from None
    return _signed_in(request, user)  # changing it signed out every other session


def add_routes(app: web.Application) -> None:
    app.router.add_get("/api/auth/state", auth_state)
    app.router.add_post("/api/auth/setup", setup)
    app.router.add_post("/api/auth/login", login)
    app.router.add_post("/api/auth/logout", logout)
    app.router.add_post("/api/auth/password", change_password)
