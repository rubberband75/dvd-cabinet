"""HTTP + WebSocket front end: the library API, thumbnails, and one DvdSession per player socket.

WebSocket protocol (/api/discs/<id>/play):
  server -> client, text:    JSON events ({"type": "stream"|"status"|"highlight"|"hover"|"titles"|...})
  server -> client, binary:  1 type byte + payload: 0x01 = MP4 init segment, 0x02 = media segment
  client -> server, text:    JSON commands ({"type": "key", "key": "up"}, {"type": "seek", ...}, ...)
"""

from __future__ import annotations

import asyncio
import json
import logging
import weakref
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from aiohttp import WSCloseCode, WSMsgType, web

from .library import Library
from .player import DvdSession

log = logging.getLogger(__name__)

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
MAX_QUEUED_SEGMENTS = 900  # ~12 s of media; beyond this the client isn't keeping up

LIBRARY = web.AppKey("library", Library)
CRF = web.AppKey("crf", int)
SOCKETS = web.AppKey("sockets", weakref.WeakSet)


async def index(_request: web.Request) -> web.Response:
    # Stamp asset URLs with their modification time so browsers never run a stale app.js.
    version = str(int(max(f.stat().st_mtime for f in WEB_DIR.iterdir())))
    html = (WEB_DIR / "index.html").read_text().replace("__VERSION__", version)
    return web.Response(text=html, content_type="text/html", headers={"Cache-Control": "no-cache"})


async def list_discs(request: web.Request) -> web.Response:
    library = request.app[LIBRARY]
    discs = await asyncio.get_running_loop().run_in_executor(None, library.scan)
    return web.json_response({"discs": [library.to_json(d) for d in discs]})


async def thumbnail(request: web.Request) -> web.StreamResponse:
    library = request.app[LIBRARY]
    disc = library.get(request.match_info["disc_id"])
    path = disc and library.thumbnail_path(disc)
    if not path:
        raise web.HTTPNotFound()
    return web.FileResponse(path, headers={"Cache-Control": "public, max-age=86400"})


async def play(request: web.Request) -> web.StreamResponse:
    library = request.app[LIBRARY]
    disc = library.get(request.match_info["disc_id"])
    if disc is None:
        raise web.HTTPNotFound()

    ws = web.WebSocketResponse(heartbeat=20, max_msg_size=1 << 16)
    await ws.prepare(request)
    request.app[SOCKETS].add(ws)
    loop = asyncio.get_running_loop()
    outbox: asyncio.Queue[tuple[str, object]] = asyncio.Queue()
    dropping = False

    def post(kind: str, payload: object) -> None:  # called from GStreamer threads
        loop.call_soon_threadsafe(outbox.put_nowait, (kind, payload))

    def on_init(init: bytes, codecs: str) -> None:
        geo = session.geometry
        post("json", {"type": "stream", "codecs": codecs, "width": geo.width, "height": geo.height})
        post("bin", b"\x01" + init)

    def on_segment(segment: bytes) -> None:
        nonlocal dropping
        if outbox.qsize() > MAX_QUEUED_SEGMENTS:
            if not dropping:
                log.warning("client for %r is not keeping up; dropping video", disc.title)
            dropping = True
            return
        dropping = False
        post("bin", b"\x02" + segment)

    session = DvdSession(disc.path, disc.info, on_init, on_segment, lambda ev: post("json", ev), crf=request.app[CRF])
    # All GStreamer control happens on one thread per session, in order.
    control = ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"dvd-{disc.id}")

    def run(fn, *args) -> None:
        try:
            fn(*args)
        except Exception:
            log.exception("command failed")

    log.info("playing %r for %s", disc.title, request.remote)
    await ws.send_json({"type": "hello", "disc": library.to_json(disc)})
    sender = asyncio.create_task(_send_loop(ws, outbox))
    try:
        await loop.run_in_executor(control, session.start)
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                try:
                    command = json.loads(msg.data)
                except ValueError:
                    continue
                if isinstance(command, dict):
                    loop.run_in_executor(control, run, session.handle, command)
            elif msg.type == WSMsgType.ERROR:
                break
    except Exception as exc:
        log.exception("session for %r failed", disc.title)
        if not ws.closed:
            await ws.send_json({"type": "error", "message": str(exc)})
    finally:
        sender.cancel()
        await loop.run_in_executor(control, session.close)
        control.shutdown(wait=False)
        log.info("stopped %r for %s", disc.title, request.remote)
    return ws


async def _send_loop(ws: web.WebSocketResponse, outbox: asyncio.Queue) -> None:
    try:
        while True:
            kind, payload = await outbox.get()
            if ws.closed:
                return
            if kind == "json":
                await ws.send_str(json.dumps(payload))
            else:
                await ws.send_bytes(payload)
    except (ConnectionResetError, asyncio.CancelledError):
        pass


async def _close_sockets(app: web.Application) -> None:
    # Players never finish on their own; close them so shutdown doesn't wait on them.
    for ws in list(app[SOCKETS]):
        await ws.close(code=WSCloseCode.GOING_AWAY, message=b"server shutting down")


def create_app(library: Library, crf: int = 20) -> web.Application:
    app = web.Application()
    app[LIBRARY] = library
    app[CRF] = crf
    app[SOCKETS] = weakref.WeakSet()
    app.on_shutdown.append(_close_sockets)
    app.router.add_get("/", index)
    app.router.add_get("/api/discs", list_discs)
    app.router.add_get("/api/discs/{disc_id}/thumbnail", thumbnail)
    app.router.add_get("/api/discs/{disc_id}/play", play)
    app.router.add_static("/static/", WEB_DIR)
    return app
