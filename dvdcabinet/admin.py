"""Admin API: user accounts, library folders, library scans, and a server folder browser."""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path

from aiohttp import WSCloseCode, web

from .accounts import AccountError
from .appkeys import ACCOUNTS, DB, LIBRARY, SOCKETS
from .web_auth import run_blocking, json_error, read_json, require_admin

log = logging.getLogger(__name__)


def library_folders(app: web.Application) -> list[str]:
    return [r["path"] for r in app[DB].all("SELECT path FROM library_folders ORDER BY path")]


def apply_library_folders(app: web.Application, scan: bool = True) -> None:
    library = app[LIBRARY]
    library.set_folders(library_folders(app))
    if scan:
        library.scan_in_background()


# ---- users -------------------------------------------------------------------------


@require_admin
async def list_users(request: web.Request) -> web.Response:
    return web.json_response({"users": [u.to_json() for u in request.app[ACCOUNTS].list_users()]})


@require_admin
async def create_user(request: web.Request) -> web.Response:
    body = await read_json(request)
    try:
        user = await run_blocking(request.app[ACCOUNTS].create_user, str(body.get("username", "")),
                               str(body.get("password", "")), bool(body.get("is_admin")))
    except AccountError as exc:
        raise json_error(web.HTTPBadRequest, str(exc)) from None
    log.info("%s added user %r", request["user"].username, user.username)
    return web.json_response({"user": user.to_json()})


def _target_user(request: web.Request):
    try:
        user = request.app[ACCOUNTS].get_user(int(request.match_info["user_id"]))
    except ValueError:
        user = None
    if user is None:
        raise json_error(web.HTTPNotFound, "No such user.")
    return user


@require_admin
async def update_user(request: web.Request) -> web.Response:
    accounts = request.app[ACCOUNTS]
    user = _target_user(request)
    body = await read_json(request)
    try:
        if "is_admin" in body:
            accounts.set_admin(user.id, bool(body["is_admin"]))
        if body.get("password"):
            await run_blocking(accounts.set_password, user.id, str(body["password"]))
            await _close_players(request.app, user.id)
    except AccountError as exc:
        raise json_error(web.HTTPBadRequest, str(exc)) from None
    return web.json_response({"user": accounts.get_user(user.id).to_json()})


@require_admin
async def delete_user(request: web.Request) -> web.Response:
    user = _target_user(request)
    try:
        request.app[ACCOUNTS].delete_user(user.id)
    except AccountError as exc:
        raise json_error(web.HTTPBadRequest, str(exc)) from None
    await _close_players(request.app, user.id)
    log.info("%s deleted user %r", request["user"].username, user.username)
    return web.json_response({"ok": True})


async def _close_players(app: web.Application, user_id: int) -> None:
    """Stop anything that user is watching (their sessions are gone)."""
    for ws in list(app[SOCKETS]):
        if ws.get("user_id") == user_id:
            await ws.close(code=WSCloseCode.POLICY_VIOLATION, message=b"signed out")


# ---- library -----------------------------------------------------------------------


def _library_state(app: web.Application) -> dict:
    library = app[LIBRARY]
    counts = library.folder_counts()
    rows = app[DB].all("SELECT id, path, added_at FROM library_folders ORDER BY path")
    discs = library.discs()
    return {
        "folders": [{"id": r["id"], "path": r["path"], "exists": os.path.isdir(r["path"]),
                     "discs": counts.get(r["path"], 0)} for r in rows],
        "scanning": library.scanning,
        "last_scan": library.last_scan,
        "discs": len(discs),
        "thumbnails_pending": sum(1 for d in discs if library.thumbnail_state(d) == "pending"),
    }


@require_admin
async def library_state(request: web.Request) -> web.Response:
    return web.json_response(_library_state(request.app))


@require_admin
async def add_folder(request: web.Request) -> web.Response:
    body = await read_json(request)
    raw = str(body.get("path", "")).strip()
    if not raw or not os.path.isabs(raw):
        raise json_error(web.HTTPBadRequest, "Enter a full path, like /mnt/Media/DVDs.")
    path = os.path.normpath(raw)
    if not os.path.isdir(path):
        raise json_error(web.HTTPBadRequest, f"{path} isn't a folder this server can see.")
    if not os.access(path, os.R_OK | os.X_OK):
        raise json_error(web.HTTPBadRequest, f"The server doesn't have permission to read {path}.")
    if request.app[DB].one("SELECT 1 FROM library_folders WHERE path = ?", (path,)):
        raise json_error(web.HTTPBadRequest, f"{path} is already in the library.")
    request.app[DB].execute("INSERT INTO library_folders (path, added_at) VALUES (?, ?)", (path, int(time.time())))
    log.info("%s added library folder %s", request["user"].username, path)
    apply_library_folders(request.app)
    return web.json_response(_library_state(request.app))


@require_admin
async def remove_folder(request: web.Request) -> web.Response:
    request.app[DB].execute("DELETE FROM library_folders WHERE id = ?", (request.match_info["folder_id"],))
    apply_library_folders(request.app)
    return web.json_response(_library_state(request.app))


@require_admin
async def scan(request: web.Request) -> web.Response:
    request.app[LIBRARY].scan_in_background()
    return web.json_response(_library_state(request.app))


@require_admin
async def browse(request: web.Request) -> web.Response:
    """Subfolders of a server path, for picking library folders."""
    path = os.path.normpath(request.query.get("path") or "/")
    if not os.path.isabs(path) or not os.path.isdir(path):
        raise json_error(web.HTTPBadRequest, f"{path} isn't a folder this server can see.")
    try:
        entries = sorted(os.scandir(path), key=lambda e: e.name.lower())
    except OSError as exc:
        raise json_error(web.HTTPBadRequest, f"Can't open {path}: {exc.strerror}.") from None
    dirs, isos = [], 0
    for entry in entries:
        if entry.name.startswith("."):
            continue
        try:
            if entry.is_dir():
                dirs.append({"name": entry.name, "path": entry.path})
            elif entry.name.lower().endswith(".iso"):
                isos += 1
        except OSError:
            continue
    parent = str(Path(path).parent) if path != "/" else None
    return web.json_response({"path": path, "parent": parent, "dirs": dirs[:1000], "isos": isos})


def add_routes(app: web.Application) -> None:
    app.router.add_get("/api/admin/users", list_users)
    app.router.add_post("/api/admin/users", create_user)
    app.router.add_patch("/api/admin/users/{user_id}", update_user)
    app.router.add_delete("/api/admin/users/{user_id}", delete_user)
    app.router.add_get("/api/admin/library", library_state)
    app.router.add_post("/api/admin/library/folders", add_folder)
    app.router.add_delete("/api/admin/library/folders/{folder_id}", remove_folder)
    app.router.add_post("/api/admin/library/scan", scan)
    app.router.add_get("/api/admin/browse", browse)
