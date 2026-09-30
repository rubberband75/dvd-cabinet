"""WebRTC transport: the paced stream goes straight to the browser as a real-time media stream.

Browsers play WebRTC frames as soon as they arrive instead of buffering like a video
file, so menu navigation feels much snappier than with the MSE fallback. Signaling
(the SDP offer/answer and ICE candidates) travels over the player's WebSocket; the
media itself flows over UDP directly between server and browser.

No STUN/TURN servers are configured. On a home network the server's own addresses
are all the browser needs. For visitors from the internet, one UDP port is forwarded
to this machine and every session is reached through it (see udpmux.py).
"""

from __future__ import annotations

import ctypes
import ctypes.util
import logging
import re
from typing import Callable

import gi

from .gstutil import Gst, GstVideo
from .player import OutputPipeline, StreamGeometry
from .udpmux import UdpMux

log = logging.getLogger(__name__)

try:
    gi.require_version("GstSdp", "1.0")
    gi.require_version("GstWebRTC", "1.0")
    from gi.repository import GstSdp, GstWebRTC
except (ImportError, ValueError):
    GstSdp = GstWebRTC = None

# element -> Debian/Ubuntu package that ships it
REQUIRED_ELEMENTS = {
    "webrtcbin": "gstreamer1.0-plugins-bad",
    "nicesrc": "gstreamer1.0-nice",
    "nicesink": "gstreamer1.0-nice",
    "rtph264pay": "gstreamer1.0-plugins-good",
    "rtpvp8pay": "gstreamer1.0-plugins-good",
    "vp8enc": "gstreamer1.0-plugins-good",
    "rtpopuspay": "gstreamer1.0-plugins-good",
    "opusenc": "gstreamer1.0-plugins-base",
}
CODECS = ("H264", "VP8")
VIDEO_PT = 96
AUDIO_PT = 111
# Ask browsers to report lost pictures (PLI/FIR) so the encoder sends a fresh keyframe.
RTCP_FEEDBACK = "rtcp-fb-nack=(boolean)true,rtcp-fb-nack-pli=(boolean)true,rtcp-fb-ccm-fir=(boolean)true"


def unavailable_reason() -> str | None:
    """None if WebRTC streaming can work here, otherwise what's missing."""
    if GstWebRTC is None:
        return "the GstWebRTC Python bindings are missing (install gir1.2-gst-plugins-bad-1.0)"
    missing = [f"{el} (from {pkg})" for el, pkg in REQUIRED_ELEMENTS.items() if not Gst.ElementFactory.find(el)]
    return "missing GStreamer elements: " + ", ".join(missing) if missing else None


def pick_codec(browser_codecs: list[str] | None, preference: str = "auto") -> str:
    """H.264 is cheaper to encode well; VP8 is the one every WebRTC browser must support."""
    if preference.upper() in CODECS:
        return preference.upper()
    offered = {str(c).upper() for c in browser_codecs or []}
    for codec in CODECS:
        if not offered or codec in offered:
            return codec
    return "VP8"


def _rtp_video_caps(codec: str) -> str:
    return f"application/x-rtp,media=video,encoding-name={codec},payload={VIDEO_PT},clock-rate=90000,{RTCP_FEEDBACK}"


def _video_encoder(codec: str, geometry: StreamGeometry, crf: int) -> str:
    gop = 2 * geometry.fps_n // geometry.fps_d
    if codec == "H264":
        # constrained baseline: the profile every browser's WebRTC stack can decode
        return f"""
            x264enc name=video tune=zerolatency speed-preset=veryfast pass=qual quantizer={crf}
                bitrate=8000 key-int-max={gop} !
              video/x-h264,profile=constrained-baseline ! h264parse !
              rtph264pay config-interval=-1 aggregate-mode=zero-latency mtu=1200 !
              capsfilter name=vrtp caps="{_rtp_video_caps('H264')}" ! webrtc.
        """
    return f"""
        vp8enc name=video deadline=1 cpu-used=4 threads=4 lag-in-frames=0 end-usage=cbr
            target-bitrate=3500000 keyframe-max-dist={gop} error-resilient=partitions !
          rtpvp8pay mtu=1200 picture-id-mode=15-bit !
          capsfilter name=vrtp caps="{_rtp_video_caps('VP8')}" ! webrtc.
    """


def _disable_upnp(webrtcbin: Gst.Element) -> None:
    """Switch off UPnP on webrtcbin's libnice agent.

    Without this, libnice asks the router (UPnP) to forward ports from the internet.
    It goes through GObject's C API because reading webrtcbin's "ice-agent" from Python
    makes PyGObject take over the agent's floating reference, and webrtcbin then uses a
    freed agent once the Python wrapper is collected.
    """
    gobject = ctypes.CDLL(ctypes.util.find_library("gobject-2.0") or "libgobject-2.0.so.0")
    capsule_pointer = ctypes.pythonapi.PyCapsule_GetPointer
    capsule_pointer.restype = ctypes.c_void_p
    capsule_pointer.argtypes = [ctypes.py_object, ctypes.c_char_p]
    ice = ctypes.c_void_p()
    gobject.g_object_get(ctypes.c_void_p(capsule_pointer(webrtcbin.__gpointer__, None)), b"ice-agent", ctypes.byref(ice), None)
    if not ice:
        return
    try:
        agent = ctypes.c_void_p()
        gobject.g_object_get(ice, b"agent", ctypes.byref(agent), None)
        if agent:
            gobject.g_object_set(agent, b"upnp", ctypes.c_int(0), None)
            gobject.g_object_unref(agent)
    finally:
        gobject.g_object_unref(ice)


class WebRtcOutput(OutputPipeline):
    """Encode for WebRTC and hand the result to webrtcbin; we always make the offer.

    on_signal(dict) sends {"type": "offer", "sdp"} and {"type": "ice", ...} to the browser.
    """

    def __init__(
        self,
        geometry: StreamGeometry,
        on_signal: Callable[[dict], None],
        on_error: Callable[[str], None],
        codec: str = "H264",
        crf: int = 20,
        mux: UdpMux | None = None,  # makes the session reachable from the internet
    ):
        super().__init__(
            geometry,
            f"""
            webrtcbin name=webrtc bundle-policy=max-bundle
            {_video_encoder(codec, geometry, crf)}
            audioconvert name=audio ! audioresample ! opusenc bitrate=128000 frame-size=20 !
              rtpopuspay pt={AUDIO_PT} !
              capsfilter name=artp caps="application/x-rtp,media=audio,encoding-name=OPUS,payload={AUDIO_PT},clock-rate=48000" !
              webrtc.
            """,
            on_error,
        )
        self.codec = codec
        self._on_signal = on_signal
        self._mux = mux
        self._ufrag: str | None = None  # this session's ICE username fragment
        self._muxed = False
        self.webrtc = self.pipeline.get_by_name("webrtc")
        for name, is_video in (("vrtp", True), ("artp", False)):
            pad = self.pipeline.get_by_name(name).get_static_pad("src").get_peer()
            transceiver = pad.get_property("transceiver")
            transceiver.set_property("direction", GstWebRTC.WebRTCRTPTransceiverDirection.SENDONLY)
            if is_video:
                transceiver.set_property("do-nack", True)  # retransmit packets lost on Wi-Fi
            if pad.find_property("msid") is not None:
                pad.set_property("msid", "dvd")  # one stream, so browsers lip-sync the two tracks
        _disable_upnp(self.webrtc)
        self._handlers.connect(self.webrtc, "on-negotiation-needed", self._on_negotiation_needed)
        self._handlers.connect(self.webrtc, "on-ice-candidate", self._on_ice_candidate)
        self._handlers.connect(self.webrtc, "notify::connection-state", self._on_connection_state)

    # ---- offer / answer ----------------------------------------------------------

    def _on_negotiation_needed(self, webrtc: Gst.Element) -> None:
        promise = Gst.Promise.new_with_change_func(self._on_offer_created, None)
        webrtc.emit("create-offer", None, promise)

    def _on_offer_created(self, promise: Gst.Promise, _data) -> None:
        reply = promise.get_reply()
        offer = reply.get_value("offer") if reply is not None else None
        if offer is None:
            log.error("webrtcbin could not create an offer: %s", reply.to_string() if reply else "no reply")
            self._on_error("could not set up WebRTC")
            return
        sdp = offer.sdp.as_text()
        ufrag = re.search(r"^a=ice-ufrag:(\S+)", sdp, re.MULTILINE)
        self._ufrag = ufrag.group(1) if ufrag else None
        # Send the offer before applying it: ICE candidates start flowing as soon as it's set.
        self._on_signal({"type": "offer", "sdp": sdp})
        self.webrtc.emit("set-local-description", offer, None)

    def _on_ice_candidate(self, _webrtc: Gst.Element, mline: int, candidate: str) -> None:
        self._on_signal({"type": "ice", "sdpMLineIndex": mline, "candidate": candidate})
        if self._mux is None or self._muxed or not self._ufrag:
            return
        # candidate:<foundation> <component> <transport> <priority> <address> <port> typ <type> ...
        parts = candidate.split()
        if len(parts) >= 8 and parts[2].upper() == "UDP" and parts[4] == self._mux.lan_ip and parts[7] == "host":
            # Our LAN socket is known: route this session's traffic from the shared port to it,
            # then tell the browser about the shared port.
            self._muxed = True
            self._mux.attach(self._ufrag, int(parts[5]))
            for extra in self._mux.candidates():
                self._on_signal({"type": "ice", "sdpMLineIndex": mline, "candidate": extra})

    def close(self) -> None:
        if self._muxed:
            self._mux.detach(self._ufrag)
        super().close()

    def signal(self, msg: dict) -> None:
        if msg.get("type") == "answer":
            res, sdp = GstSdp.SDPMessage.new_from_text(str(msg.get("sdp", "")))
            if res != GstSdp.SDPResult.OK:
                log.warning("browser sent an unreadable SDP answer")
                return
            answer = GstWebRTC.WebRTCSessionDescription.new(GstWebRTC.WebRTCSDPType.ANSWER, sdp)
            self.webrtc.emit("set-remote-description", answer, None)
        elif msg.get("type") == "ice" and msg.get("candidate"):
            self.webrtc.emit("add-ice-candidate", int(msg.get("sdpMLineIndex") or 0), str(msg["candidate"]))

    def _on_connection_state(self, webrtc: Gst.Element, _pspec) -> None:
        state = webrtc.get_property("connection-state")
        log.info("WebRTC connection %s", state.value_nick)
        if state == GstWebRTC.WebRTCPeerConnectionState.CONNECTED:
            # Frames encoded before the browser connected were dropped; start it on a keyframe.
            event = GstVideo.video_event_new_upstream_force_key_unit(Gst.CLOCK_TIME_NONE, True, 0)
            self.pipeline.get_by_name("video").get_static_pad("src").send_event(event)
