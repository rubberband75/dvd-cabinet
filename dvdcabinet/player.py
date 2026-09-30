"""One DVD playback session, streamed to one browser.

DVD pipeline (runs on "DVD time": stops dead on still menus, flushes on every jump)

    rsndvdbin ── video ──────► dvdspu ─► deinterlace ─► scale ─► appsink ──┐
    (libdvdnav)  subpicture ─►  (draws highlights)                         │
                 audio ──────► convert to 48 kHz stereo ─────► appsink ──┤
                                                                           ▼
                          MediaPacer: re-sends the latest frame and fills
                          silence, producing a steady, never-ending stream
                                                                           │
Output pipeline (live)                                                     ▼
    appsrc ─► video encoder ─┐
                              ├─► WebRTC (webrtc.py), or fragmented MP4 over the WebSocket (fmp4.py)
    appsrc ─► audio encoder ─┘

Keeping the two apart means nothing the disc does (stills, flushes, resolution and
aspect changes, 5.1 vs stereo) can ever reach the encoder or the browser's player.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable

from .discinfo import DiscInfo
from .gstutil import (
    GLib,
    Gst,
    GstVideo,
    Handlers,
    deinterlace_method,
    dvd_format,
    navigation_command,
    navigation_key,
    navigation_mouse_click,
    navigation_mouse_move,
    struct_uint64_array,
)

log = logging.getLogger(__name__)

AUDIO_RATE = 48000
AUDIO_CHANNELS = 2
AUDIO_BPF = 2 * AUDIO_CHANNELS  # S16LE interleaved
AUDIO_CAPS = f"audio/x-raw,format=S16LE,rate={AUDIO_RATE},channels={AUDIO_CHANNELS},layout=interleaved"
AUDIO_SLICE = AUDIO_RATE // 100  # the pacer sends audio in 10 ms pieces

NAV_KEYS = {"up": "Up", "down": "Down", "left": "Left", "right": "Right", "enter": "Return"}
# GObject introspection only exposes MENU1..7; navigation.h aliases them as the DVD_* commands.
NAV_COMMANDS = {
    "menu": GstVideo.NavigationCommand.MENU1,  # DVD_MENU: a remote's MENU (root menu, or resume from one)
    "title_menu": GstVideo.NavigationCommand.MENU2,  # DVD_TITLE_MENU
    "root_menu": GstVideo.NavigationCommand.MENU3,  # DVD_ROOT_MENU
    "subtitle_menu": GstVideo.NavigationCommand.MENU4,  # DVD_SUBPICTURE_MENU
    "audio_menu": GstVideo.NavigationCommand.MENU5,  # DVD_AUDIO_MENU
    "angle_menu": GstVideo.NavigationCommand.MENU6,  # DVD_ANGLE_MENU
    "chapter_menu": GstVideo.NavigationCommand.MENU7,  # DVD_CHAPTER_MENU
}
SEEK_STEP_S = 10


def make(factory: str, name: str | None = None, **props) -> Gst.Element:
    el = Gst.ElementFactory.make(factory, name)
    if el is None:
        raise RuntimeError(f"could not create GStreamer element {factory!r}")
    for key, value in props.items():
        key = key.replace("_", "-")
        if isinstance(value, str) and not isinstance(el.get_property(key), str):
            Gst.util_set_object_arg(el, key, value)  # enums/flags given by nick
        else:
            el.set_property(key, value)
    return el


@dataclass(frozen=True)
class StreamGeometry:
    """Fixed size/rate of the stream sent to the browser (square pixels)."""

    width: int
    height: int
    fps_n: int
    fps_d: int
    dvd_width: int = 720  # button/highlight coordinates live in this space
    dvd_height: int = 480

    @classmethod
    def for_disc(cls, info: DiscInfo) -> StreamGeometry:
        pal = info.standard == "PAL"
        height = 576 if pal else 480
        width = round(height * (16 / 9 if info.widescreen else 4 / 3) / 2) * 2
        fps_n, fps_d = (25, 1) if pal else (30000, 1001)
        return cls(width, height, fps_n, fps_d, 720, height)

    @property
    def video_caps(self) -> str:
        return f"video/x-raw,format=I420,width={self.width},height={self.height},pixel-aspect-ratio=1/1"

    def frame_time(self, n: int) -> int:
        return n * Gst.SECOND * self.fps_d // self.fps_n


class DvdPipeline:
    """rsndvdbin plus everything needed to get plain frames (highlights drawn in) and PCM out."""

    def __init__(
        self,
        device: str,
        video_caps: str,
        on_video: Callable[[Gst.Sample], None],
        on_audio: Callable[[Gst.Sample], None] | None = None,
        on_dvd_event: Callable[[str, Gst.Structure], None] | None = None,
        on_message: Callable[[Gst.Message], None] | None = None,
    ):
        self._on_video = on_video
        self._on_audio = on_audio
        self._on_dvd_event = on_dvd_event
        self._on_message = on_message
        self.dar = 4 / 3  # display aspect of the current DVD picture

        self._handlers = Handlers()
        self.pipeline = Gst.Pipeline.new("dvd")
        self.dvd = make("rsndvdbin", device=device)
        self.spu = make("dvdspu")  # renders subtitles and menu button highlights
        deinterlace = make("deinterlace", fields="top")
        Gst.util_set_object_arg(deinterlace, "method", deinterlace_method(deinterlace))
        video_chain = [
            self.spu,
            make("queue", max_size_buffers=3, max_size_bytes=0, max_size_time=0),
            deinterlace,
            make("videoconvert"),
            make("videoscale", add_borders=True),
            make("capsfilter", caps=Gst.Caps.from_string(video_caps)),
            make("appsink", "vsink", sync=True, max_buffers=1, drop=True, emit_signals=True),
        ]
        if on_audio is not None:
            audio_chain = [
                make("queue"),
                make("audioconvert"),
                make("audioresample"),
                make("capsfilter", caps=Gst.Caps.from_string(AUDIO_CAPS)),
                make("appsink", "asink", sync=True, max_buffers=0, emit_signals=True),
            ]
        else:
            audio_chain = [make("queue"), make("fakesink", sync=True)]
        self._audio_head = audio_chain[0]
        for chain in (video_chain, audio_chain):
            for el in chain:
                self.pipeline.add(el)
            for a, b in zip(chain, chain[1:]):
                if not a.link(b):
                    raise RuntimeError(f"could not link {a.get_name()} -> {b.get_name()}")
        self.pipeline.add(self.dvd)

        self.vsink = video_chain[-1]
        self._handlers.connect(self.vsink, "new-sample", self._pull, self._on_video)
        if on_audio is not None:
            self._handlers.connect(audio_chain[-1], "new-sample", self._pull, self._on_audio)
        self._handlers.connect(self.dvd, "pad-added", self._on_pad_added)

        self._bus = self.pipeline.get_bus()
        self._bus.add_watch(GLib.PRIORITY_DEFAULT, self._on_bus)

    @staticmethod
    def _pull(sink: Gst.Element, callback) -> Gst.FlowReturn:
        sample = sink.emit("pull-sample")
        if sample is not None:
            callback(sample)
        return Gst.FlowReturn.OK

    def _on_pad_added(self, _dvd: Gst.Element, pad: Gst.Pad) -> None:
        name = pad.get_name()
        queue = make("queue")
        self.pipeline.add(queue)
        queue.sync_state_with_parent()
        pad.link(queue.get_static_pad("sink"))
        if name.startswith("video"):
            queue.link_pads("src", self.spu, "video")
            self._handlers.probe(pad, Gst.PadProbeType.EVENT_DOWNSTREAM, self._on_video_pad_event)
        elif name.startswith("subpicture"):
            queue.link_pads("src", self.spu, "subpicture")
        elif name.startswith("audio"):
            queue.link(self._audio_head)
        else:
            log.warning("ignoring unexpected rsndvdbin pad %s", name)

    def _on_video_pad_event(self, _pad: Gst.Pad, info: Gst.PadProbeInfo) -> Gst.PadProbeReturn:
        event = info.get_event()
        if event.type == Gst.EventType.CAPS:
            s = event.parse_caps().get_structure(0)
            ok_w, w = s.get_int("width")
            ok_h, h = s.get_int("height")
            ok_par, par_n, par_d = s.get_fraction("pixel-aspect-ratio")
            if ok_w and ok_h:
                if not ok_par or par_d == 0:
                    par_n = par_d = 1
                self.dar = (w * par_n) / (h * par_d)
        elif self._on_dvd_event is not None:
            s = event.get_structure()
            if s is not None:
                name = s.get_name()
                if name == "application/x-gst-dvd":
                    self._on_dvd_event(s.get_string("event") or "", s)
                elif name == "GstEventStillFrame":
                    self._on_dvd_event("still-frame", s)
        return Gst.PadProbeReturn.OK

    def _on_bus(self, _bus: Gst.Bus, message: Gst.Message) -> bool:
        if self._on_message is not None:
            try:
                self._on_message(message)
            except Exception:  # never let a handler kill the bus watch
                log.exception("error handling bus message")
        return True

    def send_upstream(self, event: Gst.Event) -> bool:
        """Deliver a navigation/seek event to rsndvdsrc (it travels up through dvdspu)."""
        return self.spu.get_static_pad("src").send_event(event)

    def query(self, fmt: Gst.Format | None, duration: bool = False) -> int | None:
        if fmt is None:
            return None
        ok, value = (self.pipeline.query_duration if duration else self.pipeline.query_position)(fmt)
        return value if ok else None

    def set_state(self, state: Gst.State) -> Gst.StateChangeReturn:
        return self.pipeline.set_state(state)

    def close(self) -> None:
        self.pipeline.set_state(Gst.State.NULL)
        self._bus.remove_watch()
        self._handlers.release()


class MediaPacer:
    """Turns the DVD pipeline's stop-and-go output into a steady live stream.

    The DVD side only produces a frame when there is something new to show: a still
    menu delivers a couple of frames and then nothing for as long as it sits there,
    and every jump flushes. The encoder and mp4mux need uninterrupted, monotonic
    timestamps, so this thread ticks at the output frame rate, re-sending the latest
    frame, and sends audio in small slices, filling gaps with silence. Audio and video
    are both delayed by `delay_ms` so bursty audio arrival never underruns.
    """

    def __init__(self, pipeline: Gst.Pipeline, vsrc: Gst.Element, asrc: Gst.Element, geometry: StreamGeometry, delay_ms: int = 60):
        self.pipeline = pipeline
        self.vsrc = vsrc
        self.asrc = asrc
        self.geometry = geometry
        self.delay_ns = delay_ms * 1_000_000
        self.audio_target = AUDIO_RATE * delay_ms // 1000  # samples to keep queued
        self._frames: deque[tuple[int, Gst.Buffer]] = deque()
        self._frame = _black_frame(geometry)
        self._audio = bytearray()
        self._audio_buffering = True
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="pacer", daemon=True)

    def add_video(self, buf: Gst.Buffer) -> None:
        with self._lock:
            self._frames.append((time.monotonic_ns(), buf))
            while len(self._frames) > 30:
                self._frames.popleft()

    def add_audio(self, data: bytes) -> None:
        with self._lock:
            self._audio += data
            limit = AUDIO_RATE * AUDIO_BPF * 2  # never hold more than 2 s
            if len(self._audio) > limit:
                del self._audio[: len(self._audio) - limit]

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=2)

    def _take_audio(self, n: int) -> bytes:
        """n samples from the jitter buffer, padded with silence on underrun."""
        avail = len(self._audio) // AUDIO_BPF
        if self._audio_buffering:
            if avail < self.audio_target:
                return bytes(n * AUDIO_BPF)
            self._audio_buffering = False
        excess = avail - self.audio_target - AUDIO_RATE // 10
        if excess > 0:  # drifted more than 100 ms behind: catch up
            del self._audio[: excess * AUDIO_BPF]
            avail -= excess
        if avail >= n:
            out = bytes(self._audio[: n * AUDIO_BPF])
            del self._audio[: n * AUDIO_BPF]
            return out
        out = bytes(self._audio) + bytes((n - avail) * AUDIO_BPF)
        self._audio.clear()
        self._audio_buffering = True  # audio stopped (still frame, pause...): refill before resuming
        return out

    def _run(self) -> None:
        clock = self.pipeline.get_clock() or Gst.SystemClock.obtain()
        base = self.pipeline.get_base_time()
        start = clock.get_time() - base  # current running time of the output pipeline
        geo = self.geometry
        frame = 0
        samples = 0
        # Video goes out once per frame; audio in 10 ms slices on its own cadence, so it
        # leaves as a smooth stream rather than in frame-sized bursts (WebRTC receivers
        # size their audio jitter buffer to the burstiness they see).
        while not self._stop.is_set():
            vpts = start + geo.frame_time(frame)
            apts = start + samples * Gst.SECOND // AUDIO_RATE
            due = min(vpts, apts)
            now = clock.get_time() - base
            if due > now:
                if self._stop.wait((due - now) / 1e9):
                    break
                continue
            if now - due > Gst.SECOND:  # stalled (suspend, debugger...): skip ahead
                frame = (now - start) * geo.fps_n // (Gst.SECOND * geo.fps_d)
                samples = (now - start) * AUDIO_RATE // Gst.SECOND
                continue

            if vpts <= apts:
                cutoff = time.monotonic_ns() - self.delay_ns
                with self._lock:
                    while self._frames and self._frames[0][0] <= cutoff:
                        self._frame = self._frames.popleft()[1]
                    video = self._frame
                buf = video.copy()  # shallow: shares the frame's memory
                buf.unset_flags(Gst.BufferFlags.DISCONT | Gst.BufferFlags.GAP)
                buf.pts = buf.dts = vpts
                buf.duration = geo.frame_time(frame + 1) - geo.frame_time(frame)
                if self.vsrc.emit("push-buffer", buf) != Gst.FlowReturn.OK:
                    break
                frame += 1
            else:
                with self._lock:
                    audio = self._take_audio(AUDIO_SLICE)
                buf = Gst.Buffer.new_wrapped(audio)
                buf.pts = buf.dts = apts
                buf.duration = AUDIO_SLICE * Gst.SECOND // AUDIO_RATE
                if self.asrc.emit("push-buffer", buf) != Gst.FlowReturn.OK:
                    break
                samples += AUDIO_SLICE


def _black_frame(geometry: StreamGeometry) -> Gst.Buffer:
    """A correctly laid-out black I420 frame, shown until the disc produces a picture."""
    pipe = Gst.parse_launch(
        f"videotestsrc pattern=black num-buffers=1 ! {geometry.video_caps},framerate={geometry.fps_n}/{geometry.fps_d} "
        "! appsink name=sink sync=false"
    )
    pipe.set_state(Gst.State.PLAYING)
    sample = pipe.get_by_name("sink").emit("try-pull-sample", 5 * Gst.SECOND)
    pipe.set_state(Gst.State.NULL)
    if sample is None:
        raise RuntimeError("could not generate a black frame")
    return sample.get_buffer()


class OutputPipeline:
    """The encoder side of a session: appsrcs named "vsrc" and "asrc", fed by the MediaPacer.

    Subclasses supply the rest of the pipeline (encoders and how the result reaches
    the browser) and handle any signaling messages the browser sends for it.
    """

    def __init__(self, geometry: StreamGeometry, rest: str, on_error: Callable[[str], None]):
        geo = geometry
        self.pipeline = Gst.parse_launch(
            f"""
            appsrc name=vsrc is-live=true format=time do-timestamp=false max-bytes=0
                caps="{geo.video_caps},framerate={geo.fps_n}/{geo.fps_d}" !
              queue max-size-buffers=8 max-size-bytes=0 max-size-time=0 ! video.
            appsrc name=asrc is-live=true format=time do-timestamp=false max-bytes=0 caps="{AUDIO_CAPS}" !
              queue ! audio.
            {rest}
            """
        )
        self.vsrc = self.pipeline.get_by_name("vsrc")
        self.asrc = self.pipeline.get_by_name("asrc")
        self._on_error = on_error
        self._handlers = Handlers()  # subclasses connect their callbacks through this
        self._bus = self.pipeline.get_bus()
        self._bus.add_watch(GLib.PRIORITY_DEFAULT, self._on_bus)

    def _on_bus(self, _bus: Gst.Bus, msg: Gst.Message) -> bool:
        if msg.type == Gst.MessageType.ERROR:
            err, debug = msg.parse_error()
            log.error("encoder pipeline error: %s (%s)", err.message, debug)
            self._on_error(err.message)
        return True

    def start(self) -> None:
        if self.pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            raise RuntimeError("could not start the encoder pipeline")

    def close(self) -> None:
        self.pipeline.set_state(Gst.State.NULL)
        self._bus.remove_watch()
        self._handlers.release()

    def signal(self, msg: dict) -> None:
        """Transport signaling from the browser (WebRTC answer / ICE candidates)."""


class DvdSession:
    """A virtual DVD player: plays one disc image and streams it to one browser.

    make_output(geometry, on_error) builds the OutputPipeline that decides how the
    stream travels (WebRTC or fragmented MP4 over the WebSocket). on_event receives
    JSON-able state updates for the UI; like all callbacks here it fires on
    GStreamer threads.
    """

    def __init__(
        self,
        device: str,
        info: DiscInfo,
        make_output: Callable[[StreamGeometry, Callable[[str], None]], OutputPipeline],
        on_event: Callable[[dict], None],
    ):
        self.geometry = StreamGeometry.for_disc(info)
        self._on_event = on_event
        self._lock = threading.Lock()
        self._closed = False
        self._status: dict = {}
        self.paused = False
        self.has_buttons = False
        geo = self.geometry

        self.dvd = DvdPipeline(
            device,
            geo.video_caps,
            on_video=lambda s: self.pacer.add_video(s.get_buffer()),
            on_audio=self._on_dvd_audio,
            on_dvd_event=self._on_dvd_event,
            on_message=self._on_dvd_message,
        )

        self.output = make_output(geo, lambda message: self._emit({"type": "error", "message": f"Streaming failed: {message}"}))
        self.pacer = MediaPacer(self.output.pipeline, self.output.vsrc, self.output.asrc, geo)

    # ---- lifecycle ------------------------------------------------------------

    def start(self) -> None:
        self.output.start()
        self.pacer.start()
        if self.dvd.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            raise RuntimeError("could not open the disc")
        GLib.timeout_add(500, self._poll_status)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True  # the status timer sees this and removes itself
        self.pacer.stop()
        self.dvd.close()
        self.output.close()

    # ---- commands from the browser ---------------------------------------------

    def handle(self, msg: dict) -> None:
        kind = msg.get("type")
        if kind == "key":
            self.key(str(msg.get("key", "")))
        elif kind == "pointer":
            self.pointer(float(msg["x"]), float(msg["y"]), bool(msg.get("click")))
        elif kind == "command":
            self.command(str(msg.get("command", "")))
        elif kind == "pause":
            self.set_paused(True)
        elif kind == "play":
            self.set_paused(False)
        elif kind == "toggle_pause":
            self.set_paused(not self.paused)
        elif kind == "seek":
            self.seek(float(msg["position"]))
        elif kind == "seek_by":
            self.seek_by(float(msg["offset"]))
        elif kind == "chapter":
            self.chapter(int(msg.get("delta", 1)))
        elif kind == "title":
            self.play_title(int(msg["title"]))
        elif kind in ("answer", "ice"):
            self.output.signal(msg)
        else:
            log.debug("unknown command %r", msg)

    def key(self, key: str) -> None:
        if key in ("left", "right") and not self.has_buttons:
            # No menu buttons on screen (i.e. watching a title): left/right seek instead.
            self.seek_by(SEEK_STEP_S if key == "right" else -SEEK_STEP_S)
            return
        name = NAV_KEYS.get(key)
        if name:
            self._navigate([navigation_key(name)])

    def command(self, name: str) -> None:
        cmd = NAV_COMMANDS.get(name)
        if cmd is not None:
            self._navigate([navigation_command(cmd)])

    def pointer(self, x: float, y: float, click: bool) -> None:
        """x, y are 0..1 across the streamed picture; map them onto DVD button coordinates."""
        geo = self.geometry
        dar = self.dvd.dar
        cw, ch = (geo.width, geo.width / dar) if dar > geo.width / geo.height else (geo.height * dar, geo.height)
        px = (x * geo.width - (geo.width - cw) / 2) / cw
        py = (y * geo.height - (geo.height - ch) / 2) / ch
        if not (0 <= px <= 1 and 0 <= py <= 1):
            return
        dx, dy = px * geo.dvd_width, py * geo.dvd_height
        events = navigation_mouse_click(dx, dy) if click else [navigation_mouse_move(dx, dy)]
        self._navigate(events, resume=click)

    def set_paused(self, paused: bool) -> None:
        if paused == self.paused:
            return
        self.paused = paused
        self.dvd.set_state(Gst.State.PAUSED if paused else Gst.State.PLAYING)
        self._poll_status()

    def seek(self, seconds: float) -> None:
        """Seek within the current title (menus aren't seekable)."""
        dur = self.dvd.query(Gst.Format.TIME, duration=True)
        if self.dvd.query(dvd_format("title")) in (None, 0) or not dur or dur <= 0:
            return
        target = min(max(0, int(seconds * Gst.SECOND)), dur - Gst.SECOND)
        self.set_paused(False)
        event = Gst.Event.new_seek(
            1.0, Gst.Format.TIME, Gst.SeekFlags.FLUSH, Gst.SeekType.SET, max(0, target), Gst.SeekType.NONE, -1
        )
        if not self.dvd.send_upstream(event):
            log.info("seek to %.1fs refused", seconds)

    def seek_by(self, offset: float) -> None:
        pos = self.dvd.query(Gst.Format.TIME)
        if pos is not None:
            self.seek(pos / Gst.SECOND + offset)

    def chapter(self, delta: int) -> None:
        self._navigate([navigation_key("period" if delta > 0 else "comma")])

    def play_title(self, title: int) -> None:
        fmt = dvd_format("title")
        if fmt is None:
            return
        self.set_paused(False)
        event = Gst.Event.new_seek(1.0, fmt, Gst.SeekFlags.FLUSH, Gst.SeekType.SET, title, Gst.SeekType.NONE, -1)
        self.dvd.send_upstream(event)

    def _navigate(self, events: list[Gst.Event], resume: bool = True) -> None:
        if resume:
            self.set_paused(False)  # like a real player: pressing a button un-pauses
        for event in events:
            self.dvd.send_upstream(event)

    # ---- DVD side --------------------------------------------------------------

    def _on_dvd_audio(self, sample: Gst.Sample) -> None:
        buf = sample.get_buffer()
        self.pacer.add_audio(buf.extract_dup(0, buf.get_size()))

    def _on_dvd_event(self, name: str, s: Gst.Structure) -> None:
        if name == "dvd-spu-highlight":
            self.has_buttons = True
            rect = [s.get_int(k)[1] for k in ("sx", "sy", "ex", "ey")]
            self._emit({"type": "highlight", "button": s.get_int("button")[1], "rect": self._to_stream_rect(rect)})
        elif name == "dvd-spu-reset-highlight":
            self.has_buttons = False
            self._emit({"type": "highlight", "button": 0, "rect": None})
        elif name == "still-frame":
            self._emit({"type": "still", "still": bool(s.get_boolean("still-state")[1])})

    def _to_stream_rect(self, rect: list[int]) -> list[float]:
        """DVD button coordinates -> 0..1 fractions of the streamed picture."""
        geo = self.geometry
        dar = self.dvd.dar
        cw, ch = (geo.width, geo.width / dar) if dar > geo.width / geo.height else (geo.height * dar, geo.height)
        ox, oy = (geo.width - cw) / 2, (geo.height - ch) / 2
        sx, sy, ex, ey = rect
        return [
            round((ox + sx / geo.dvd_width * cw) / geo.width, 4),
            round((oy + sy / geo.dvd_height * ch) / geo.height, 4),
            round((ox + ex / geo.dvd_width * cw) / geo.width, 4),
            round((oy + ey / geo.dvd_height * ch) / geo.height, 4),
        ]

    def _on_dvd_message(self, msg: Gst.Message) -> None:
        t = msg.type
        if t == Gst.MessageType.ELEMENT:
            s = msg.get_structure()
            if s is None:
                return
            if s.get_name() == "application/x-gst-dvd" and s.get_string("event") == "dvd-title-info":
                self._emit_titles(struct_uint64_array(s, "title-durations"))
            elif GstVideo.Navigation.message_get_type(msg) == GstVideo.NavigationMessageType.MOUSE_OVER:
                ok, active = GstVideo.Navigation.message_parse_mouse_over(msg)
                if ok:
                    self._emit({"type": "hover", "active": bool(active)})
        elif t == Gst.MessageType.EOS:
            self._emit({"type": "ended"})
        elif t == Gst.MessageType.ERROR:
            err, debug = msg.parse_error()
            log.error("DVD pipeline error: %s (%s)", err.message, debug)
            self._emit({"type": "error", "message": err.message})
        elif t == Gst.MessageType.WARNING:
            err, _ = msg.parse_warning()
            log.warning("DVD pipeline: %s", err.message)

    def _emit_titles(self, durations: list[int]) -> None:
        # Index = title number; rsndvdsrc leaves index 0 unset and omits the last title.
        count = self.dvd.query(dvd_format("title"), duration=True) or len(durations)
        titles = []
        for n in range(1, count + 1):
            d = durations[n] if n < len(durations) else None
            titles.append({"title": n, "duration": None if d in (None, Gst.CLOCK_TIME_NONE) else round(d / Gst.SECOND, 1)})
        self._emit({"type": "titles", "titles": titles})

    def _poll_status(self) -> bool:
        if self._closed:
            return False
        pos = self.dvd.query(Gst.Format.TIME)
        dur = self.dvd.query(Gst.Format.TIME, duration=True)
        title = self.dvd.query(dvd_format("title"))
        chapter = self.dvd.query(dvd_format("chapter"))
        status = {
            "type": "status",
            "paused": self.paused,
            "menu": title == 0 or self.has_buttons,
            "title": title,
            "chapter": chapter,
            "chapters": self.dvd.query(dvd_format("chapter"), duration=True),
            "position": round(pos / Gst.SECOND, 1) if pos is not None and pos >= 0 else None,
            "duration": round(dur / Gst.SECOND, 1) if dur and dur > 0 else None,
        }
        if status != self._status:
            self._status = status
            self._emit(dict(status))
        return True

    def _emit(self, event: dict) -> None:
        if not self._closed:
            self._on_event(event)
