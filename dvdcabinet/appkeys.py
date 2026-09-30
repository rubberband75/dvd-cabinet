"""Typed keys for the objects shared through the aiohttp application."""

from __future__ import annotations

import weakref

from aiohttp import web

from .accounts import Accounts
from .db import Database
from .library import Library

DB = web.AppKey("db", Database)
ACCOUNTS = web.AppKey("accounts", Accounts)
LIBRARY = web.AppKey("library", Library)
CRF = web.AppKey("crf", int)
WEBRTC = web.AppKey("webrtc", object)  # server.WebRtcConfig, or None when WebRTC is unavailable
SOCKETS = web.AppKey("sockets", weakref.WeakSet)  # open player WebSockets (each has ws["user_id"])
