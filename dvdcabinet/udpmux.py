"""One UDP port for every WebRTC viewer.

Each WebRTC session (a libnice agent inside webrtcbin) listens on its own random UDP
port, which is awkward to forward through a router. The mux listens on one fixed
port instead and relays each browser's packets to the right session.

It can tell sessions apart because the first packet from any browser address is an
ICE connectivity check (a STUN binding request) whose USERNAME attribute is
"<session ufrag>:<browser ufrag>". The mux then gives that browser address its own
relay socket towards the session's libnice port; libnice simply sees the relay as
the browser, and everything after the check (DTLS, SRTP, keepalives) flows through
unchanged.
"""

from __future__ import annotations

import asyncio
import logging
import struct
import time

log = logging.getLogger(__name__)

STUN_MAGIC_COOKIE = 0x2112A442
STUN_USERNAME = 0x0006
IDLE_TIMEOUT_S = 30  # browsers send ICE consent checks every few seconds


def stun_username(data: bytes) -> str | None:
    """The USERNAME of a STUN message, or None if the packet isn't STUN."""
    if len(data) < 20 or data[0] & 0xC0:
        return None
    _type, length, cookie = struct.unpack_from("!HHI", data)
    if cookie != STUN_MAGIC_COOKIE or 20 + length > len(data):
        return None
    pos, end = 20, 20 + length
    while pos + 4 <= end:
        attr, attr_len = struct.unpack_from("!HH", data, pos)
        pos += 4
        if attr == STUN_USERNAME:
            return data[pos:pos + attr_len].decode("utf-8", "replace")
        pos += (attr_len + 3) & ~3
    return None


class _Relay(asyncio.DatagramProtocol):
    """One browser address <-> one session, via a socket connected to the session's libnice port."""

    def __init__(self, mux: UdpMux, browser: tuple, ufrag: str, first_packet: bytes):
        self.mux = mux
        self.browser = browser
        self.ufrag = ufrag
        self.transport: asyncio.DatagramTransport | None = None
        self.pending: list[bytes] | None = [first_packet]  # until the socket is open
        self.last_seen = time.monotonic()

    def connection_made(self, transport: asyncio.DatagramTransport) -> None:
        self.transport = transport
        for packet in self.pending or []:
            transport.sendto(packet)
        self.pending = None

    def to_session(self, data: bytes) -> None:
        self.last_seen = time.monotonic()
        if self.transport is None:
            self.pending.append(data)
        else:
            self.transport.sendto(data)

    def datagram_received(self, data: bytes, _addr) -> None:  # libnice -> browser
        self.last_seen = time.monotonic()
        self.mux.send_to_browser(data, self.browser)

    def error_received(self, exc: Exception) -> None:
        log.debug("relay to session %s: %s", self.ufrag, exc)

    def close(self) -> None:
        if self.transport is not None:
            self.transport.close()


class UdpMux(asyncio.DatagramProtocol):
    """Listens on one UDP port for browsers; sessions register their ICE ufrag and libnice port."""

    def __init__(self, lan_ip: str, port: int, public_ip: str):
        self.lan_ip = lan_ip
        self.port = port
        self.public_ip = public_ip
        self.loop: asyncio.AbstractEventLoop | None = None
        self.transport: asyncio.DatagramTransport | None = None
        self._sessions: dict[str, int] = {}  # session ufrag -> its libnice port on lan_ip
        self._relays: dict[tuple, _Relay] = {}  # browser address -> relay
        self._reaper: asyncio.Task | None = None

    async def start(self) -> None:
        self.loop = asyncio.get_running_loop()
        self.transport, _ = await self.loop.create_datagram_endpoint(lambda: self, local_addr=("0.0.0.0", self.port))
        self._reaper = self.loop.create_task(self._reap())

    def close(self) -> None:
        if self._reaper is not None:
            self._reaper.cancel()
        for relay in self._relays.values():
            relay.close()
        self._relays.clear()
        if self.transport is not None:
            self.transport.close()

    def candidate(self) -> str:
        """The ICE candidate browsers outside the network use: the router's public address and our port."""
        priority = (100 << 24) | (65535 << 8) | 255  # RFC 8445 server-reflexive type preference
        return (f"candidate:mux 1 UDP {priority} {self.public_ip} {self.port} "
                f"typ srflx raddr {self.lan_ip} rport {self.port}")

    # Called from GStreamer threads; the mux itself lives on the asyncio loop.
    def attach(self, ufrag: str, session_port: int) -> None:
        self.loop.call_soon_threadsafe(self._sessions.__setitem__, ufrag, session_port)

    def detach(self, ufrag: str) -> None:
        self.loop.call_soon_threadsafe(self._drop, ufrag)

    def _drop(self, ufrag: str) -> None:
        self._sessions.pop(ufrag, None)
        for browser, relay in list(self._relays.items()):
            if relay.ufrag == ufrag:
                relay.close()
                del self._relays[browser]

    def send_to_browser(self, data: bytes, browser: tuple) -> None:
        self.transport.sendto(data, browser)

    def datagram_received(self, data: bytes, browser: tuple) -> None:
        relay = self._relays.get(browser)
        if relay is not None:
            relay.to_session(data)
            return
        username = stun_username(data)
        if not username:
            return  # not an ICE check, and not from a browser we know: ignore
        ufrag = username.split(":", 1)[0]
        session_port = self._sessions.get(ufrag)
        if session_port is None:
            return
        relay = self._relays[browser] = _Relay(self, browser, ufrag, data)
        self.loop.create_task(self._open(relay, session_port))

    async def _open(self, relay: _Relay, session_port: int) -> None:
        try:
            await self.loop.create_datagram_endpoint(
                lambda: relay, local_addr=(self.lan_ip, 0), remote_addr=(self.lan_ip, session_port))
        except OSError as exc:
            log.warning("could not relay %s to its session: %s", relay.browser, exc)
            self._relays.pop(relay.browser, None)

    async def _reap(self) -> None:
        while True:
            await asyncio.sleep(10)
            now = time.monotonic()
            for browser, relay in list(self._relays.items()):
                if now - relay.last_seen > IDLE_TIMEOUT_S:
                    relay.close()
                    del self._relays[browser]
