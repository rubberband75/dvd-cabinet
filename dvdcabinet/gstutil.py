"""GStreamer setup and small helpers shared by the player and the thumbnailer."""

from __future__ import annotations

import ctypes.util
import logging
import os
import re
import sys
import threading

import gi

gi.require_version("Gst", "1.0")
gi.require_version("GstVideo", "1.0")
from gi.repository import GLib, GObject, Gst, GstVideo  # noqa: E402

log = logging.getLogger(__name__)

# element -> Debian/Ubuntu package that ships it
REQUIRED_ELEMENTS = {
    "rsndvdbin": "gstreamer1.0-plugins-bad",
    "dvdspu": "gstreamer1.0-plugins-bad",
    "h264parse": "gstreamer1.0-plugins-bad",
    "deinterlace": "gstreamer1.0-plugins-good",
    "mp4mux": "gstreamer1.0-plugins-good",
    "aacparse": "gstreamer1.0-plugins-good",
    "jpegenc": "gstreamer1.0-plugins-good",
    "x264enc": "gstreamer1.0-plugins-ugly",
    "avenc_aac": "gstreamer1.0-libav",
    "appsrc": "gstreamer1.0-plugins-base",
    "appsink": "gstreamer1.0-plugins-base",
    "videoconvert": "gstreamer1.0-plugins-base",
    "videoscale": "gstreamer1.0-plugins-base",
    "audioconvert": "gstreamer1.0-plugins-base",
    "audioresample": "gstreamer1.0-plugins-base",
}

# Hardware MPEG-2 decoders hand dvdspu frames it can't draw menu highlights onto
# (the result is solid green video), and DVD-resolution MPEG-2 is trivial to decode
# in software anyway, so keep them out of autoplugging.
HW_MPEG2_DECODERS = (
    "vampeg2dec",
    "vaapimpeg2dec",
    "nvmpeg2videodec",
    "v4l2slmpeg2dec",
    "msdkmpeg2dec",
    "qsvmpeg2dec",
)

_main_loop: GLib.MainLoop | None = None


def init() -> None:
    """Initialise GStreamer once and start a GLib main loop for bus watches and timers."""
    global _main_loop
    if _main_loop is not None:
        return
    Gst.init(None)
    missing = [f"{el} (from {pkg})" for el, pkg in REQUIRED_ELEMENTS.items() if not Gst.ElementFactory.find(el)]
    if missing:
        raise RuntimeError("Missing GStreamer elements: " + ", ".join(missing))
    registry = Gst.Registry.get()
    for name in HW_MPEG2_DECODERS:
        feature = registry.lookup_feature(name)
        if feature is not None:
            feature.set_rank(Gst.Rank.NONE)
    _main_loop = GLib.MainLoop()
    threading.Thread(target=_main_loop.run, name="glib-main", daemon=True).start()
    log.info("GStreamer %s ready", Gst.version_string())


class Handlers:
    """The signal handlers and pad probes of one pipeline, so they can all be dropped on close.

    A Python callback connected to a GStreamer object keeps its owner alive, and the
    object keeps the callback: a reference cycle Python's garbage collector can't see
    through. Unless the handlers are removed, closed pipelines are never freed (and
    webrtcbin keeps its threads running).
    """

    def __init__(self) -> None:
        self._signals: list[tuple[GObject.Object, int]] = []
        self._probes: list[tuple[Gst.Pad, int]] = []

    def connect(self, obj: GObject.Object, signal: str, callback, *args) -> None:
        self._signals.append((obj, obj.connect(signal, callback, *args)))

    def probe(self, pad: Gst.Pad, mask: Gst.PadProbeType, callback) -> None:
        self._probes.append((pad, pad.add_probe(mask, callback)))

    def release(self) -> None:
        for obj, handler in self._signals:
            obj.disconnect(handler)
        for pad, probe in self._probes:
            pad.remove_probe(probe)
        self._signals.clear()
        self._probes.clear()


def dvd_format(nick: str) -> Gst.Format | None:
    """rsndvdsrc registers custom "title" and "chapter" formats once it has been instantiated."""
    fmt = Gst.Format.get_by_nick(nick)
    return fmt if fmt != Gst.Format.UNDEFINED else None


def struct_uint64_array(structure: Gst.Structure, field: str) -> list[int]:
    """Read a GstValueArray of guint64 without the gst-python overrides (not always installed)."""
    m = re.search(re.escape(field) + r"=\(guint64\)<([^>]*)>", structure.to_string())
    if not m:
        return []
    return [int(v) for v in m.group(1).split(",") if v.strip()]


# Navigation events are built from their structure rather than with
# GstVideo.Navigation.event_new_*(), which only exist since GStreamer 1.22; the
# structure format is the same on every version, so this works on 1.20 too.
def _navigation(fields: str) -> Gst.Event:
    return Gst.Event.new_navigation(Gst.Structure.new_from_string(f"application/x-gst-navigation, {fields}"))


def navigation_key(key: str) -> Gst.Event:
    return _navigation(f"event=(string)key-press, key=(string){key}")


def navigation_command(command: GstVideo.NavigationCommand) -> Gst.Event:
    return _navigation(f"event=(string)command, command-code=(uint){int(command)}")


def navigation_mouse_move(x: float, y: float) -> Gst.Event:
    return _navigation(f"event=(string)mouse-move, button=(int)0, pointer_x=(double){x:f}, pointer_y=(double){y:f}")


def navigation_mouse_click(x: float, y: float) -> list[Gst.Event]:
    return [
        _navigation(f"event=(string){kind}, button=(int)1, pointer_x=(double){x:f}, pointer_y=(double){y:f}")
        for kind in ("mouse-button-press", "mouse-button-release")
    ]


def deinterlace_method(element: Gst.Element) -> str:
    """The best deinterlacer the installed GStreamer has (YADIF arrived in 1.22).

    greedyh is skipped on purpose: it garbles planar video (checked on 1.24).
    """
    pspec = element.find_property("method")
    available = {v.value_nick for v in pspec.enum_class.__enum_values__.values()}
    return next(m for m in ("yadif", "greedyl", "linear") if m in available)


def have_libdvdcss() -> bool:
    """libdvdread loads libdvdcss at runtime to decrypt copy-protected (CSS) discs."""
    return ctypes.util.find_library("dvdcss") is not None


def filter_native_output(prefixes: tuple[bytes, ...] = (b"libdvdread:", b"libdvdnav:", b"libdvdcss")) -> None:
    """Drop the libdvdread/libdvdnav chatter that their C code prints straight to stdout/stderr.

    Opening an encrypted disc prints a couple of lines per VOB file ("Get key for ..."),
    which buries the server's own log. Everything else is passed through untouched.
    """
    for fd, stream in ((1, sys.stdout), (2, sys.stderr)):
        stream.flush()
        try:
            original = os.dup(fd)
            read_fd, write_fd = os.pipe()
            os.dup2(write_fd, fd)
            os.close(write_fd)
        except OSError:
            continue

        def pump(read_fd: int = read_fd, original: int = original) -> None:
            with os.fdopen(read_fd, "rb") as pipe:
                for line in iter(pipe.readline, b""):
                    text = line.strip()
                    # libdvdread's "No css library available" box is all ** lines; we log that once
                    if text.startswith(prefixes) or (text.startswith(b"*") and text.endswith(b"*")):
                        continue
                    os.write(original, line)

        threading.Thread(target=pump, name=f"output-filter-{fd}", daemon=True).start()
