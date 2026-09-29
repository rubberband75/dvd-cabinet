"""HTTP + WebSocket front end: the library API, thumbnails, and one DvdSession per player socket.

WebSocket protocol (/api/discs/<id>/play):
  server -> client  {"type": "hello", "disc", "transports": ["webrtc", "mse"]}
  client -> server  {"type": "start", "transport": "webrtc"|"mse", "video_codecs": [...]}
  then, for WebRTC: offer / answer / ice messages both ways (the media itself goes over UDP);
        for MSE:    {"type": "stream", "codecs"} and binary messages: 1 type byte + payload
                    (0x01 = MP4 init segment, 0x02 = media segment)
  server -> client  JSON events ({"type": "status"|"highlight"|"hover"|"titles"|...})
  client -> server  JSON commands ({"type": "key", "key": "up"}, {"type": "seek", ...}, ...)
"""

from __future__ import annotations

import asyncio
import gc
import json
import logging
import weakref
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from aiohttp import WSCloseCode, WSMsgType, web

from . import webrtc
from .fmp4 import Fmp4Output
from .library import Library
from .player import DvdSession
from .udpmux import UdpMux

log = logging.getLogger(__name__)

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
MAX_QUEUED_SEGMENTS = 900  # ~12 s of media; beyond this the client isn't keeping up


@dataclass
class WebRtcConfig:
    codec: str = "auto"  # auto | h264 | vp8
    mux: UdpMux | None = None  # the one UDP port visitors from the internet connect to


LIBRARY = web.AppKey("library", Library)
CRF = web.AppKey("crf", int)
WEBRTC = web.AppKey("webrtc", object)  # WebRtcConfig, or None when WebRTC is unavailable
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

    # Behind a reverse proxy the peer is the proxy; it passes the viewer's address along.
    viewer = request.headers.get("X-Real-IP") or request.remote
    ws = web.WebSocketResponse(heartbeat=20, max_msg_size=1 << 16)
    await ws.prepare(request)
    request.app[SOCKETS].add(ws)
    loop = asyncio.get_running_loop()
    outbox: asyncio.Queue[tuple[str, object]] = asyncio.Queue()
    dropping = False
    session: DvdSession | None = None
    rtc: WebRtcConfig | None = request.app[WEBRTC]
    crf = request.app[CRF]

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

    def open_session(start: dict) -> DvdSession:
        if start.get("transport") == "webrtc" and rtc is not None:
            codec = webrtc.pick_codec(start.get("video_codecs"), rtc.codec)
            log.info("playing %r for %s over WebRTC (%s)", disc.title, viewer, codec)
            return DvdSession(disc.path, disc.info, lambda geo, on_error: webrtc.WebRtcOutput(
                geo, lambda m: post("json", m), on_error, codec, crf, rtc.mux),
                lambda ev: post("json", ev))
        log.info("playing %r for %s over MSE", disc.title, viewer)
        return DvdSession(disc.path, disc.info, lambda geo, on_error: Fmp4Output(
            geo, on_init, on_segment, on_error, crf), lambda ev: post("json", ev))

    # All GStreamer control happens on one thread per session, in order.
    control = ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"dvd-{disc.id}")

    def run(fn, *args) -> None:
        try:
            fn(*args)
        except Exception:
            log.exception("command failed")

    transports = (["webrtc"] if rtc is not None else []) + ["mse"]
    await ws.send_json({"type": "hello", "disc": library.to_json(disc), "transports": transports})
    sender = asyncio.create_task(_send_loop(ws, outbox))
    try:
        async for msg in ws:
            if msg.type == WSMsgType.ERROR:
                break
            if msg.type != WSMsgType.TEXT:
                continue
            try:
                command = json.loads(msg.data)
            except ValueError:
                continue
            if not isinstance(command, dict):
                continue
            if session is None:
                if command.get("type") == "start":
                    session = open_session(command)
                    await loop.run_in_executor(control, session.start)
                continue
            loop.run_in_executor(control, run, session.handle, command)
    except Exception as exc:
        log.exception("session for %r failed", disc.title)
        if not ws.closed:
            await ws.send_json({"type": "error", "message": str(exc)})
    finally:
        sender.cancel()
        if session is not None:
            await loop.run_in_executor(control, session.close)
            log.info("stopped %r for %s", disc.title, viewer)
        control.shutdown(wait=False)
        session = None
        # Free the session's pipelines now rather than whenever Python next collects
        # cycles; an idle server might not for a long time, keeping webrtcbin's threads alive.
        await loop.run_in_executor(None, gc.collect)
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


async def _start_mux(app: web.Application) -> None:
    rtc = app[WEBRTC]
    if rtc is not None and rtc.mux is not None:
        await rtc.mux.start()


async def _stop_mux(app: web.Application) -> None:
    rtc = app[WEBRTC]
    if rtc is not None and rtc.mux is not None:
        rtc.mux.close()


async def _close_sockets(app: web.Application) -> None:
    # Players never finish on their own; close them so shutdown doesn't wait on them.
    for ws in list(app[SOCKETS]):
        await ws.close(code=WSCloseCode.GOING_AWAY, message=b"server shutting down")


def create_app(library: Library, crf: int = 20, rtc: WebRtcConfig | None = None) -> web.Application:
    app = web.Application()
    app[LIBRARY] = library
    app[CRF] = crf
    app[WEBRTC] = rtc
    app[SOCKETS] = weakref.WeakSet()
    app.on_startup.append(_start_mux)
    app.on_shutdown.append(_close_sockets)
    app.on_cleanup.append(_stop_mux)
    app.router.add_get("/", index)
    app.router.add_get("/api/discs", list_discs)
    app.router.add_get("/api/discs/{disc_id}/thumbnail", thumbnail)
    app.router.add_get("/api/discs/{disc_id}/play", play)
    app.router.add_static("/static/", WEB_DIR)
    return app
