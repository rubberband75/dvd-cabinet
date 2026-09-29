"""Fallback transport: fragmented MP4 over the WebSocket, played with Media Source Extensions.

Used when WebRTC can't connect (UDP blocked, no WebRTC support). It works anywhere
a WebSocket does, at the cost of the browser buffering a few hundred ms more.

mp4mux emits headers and fragments in arbitrary small pieces (an mdat header in one
buffer, its payload in the next, ...). Browsers want the init segment (ftyp+moov)
first, then complete media segments (moof+mdat), so we split on top-level boxes.
"""

from __future__ import annotations

from typing import Callable

from .gstutil import Gst
from .player import OutputPipeline, StreamGeometry


class Fmp4Output(OutputPipeline):
    """H.264 + AAC in fragmented MP4, one fragment per frame; on_init/on_segment get the bytes."""

    def __init__(
        self,
        geometry: StreamGeometry,
        on_init: Callable[[bytes, str], None],
        on_segment: Callable[[bytes], None],
        on_error: Callable[[str], None],
        crf: int = 20,
    ):
        super().__init__(
            geometry,
            f"""
            x264enc name=video tune=zerolatency speed-preset=veryfast pass=qual quantizer={crf}
                key-int-max={2 * geometry.fps_n // geometry.fps_d} !
              video/x-h264,profile=high ! h264parse ! queue ! mux.
            audioconvert name=audio ! avenc_aac bitrate=192000 ! aacparse ! queue ! mux.
            mp4mux name=mux fragment-duration=1 streamable=true !
              appsink name=sink sync=false emit-signals=true
            """,
            on_error,
        )
        self._on_init = on_init
        self._splitter = Mp4Splitter(self._got_init, on_segment)
        self._handlers.connect(self.pipeline.get_by_name("sink"), "new-sample", self._on_sample)

    def _on_sample(self, sink: Gst.Element) -> Gst.FlowReturn:
        sample = sink.emit("pull-sample")
        if sample is not None:
            buf = sample.get_buffer()
            self._splitter.feed(buf.extract_dup(0, buf.get_size()))
        return Gst.FlowReturn.OK

    def _got_init(self, init: bytes) -> None:
        self._on_init(init, codecs_from_init(init))


class Mp4Splitter:
    def __init__(self, on_init: Callable[[bytes], None], on_segment: Callable[[bytes], None]):
        self._on_init = on_init
        self._on_segment = on_segment
        self._buf = bytearray()
        self._init = bytearray()
        self._moof: bytes | None = None
        self.init_segment: bytes | None = None

    def feed(self, data: bytes) -> None:
        self._buf += data
        while len(self._buf) >= 8:
            size = int.from_bytes(self._buf[0:4], "big")
            header = 8
            if size == 1:  # 64-bit "largesize"
                if len(self._buf) < 16:
                    return
                size = int.from_bytes(self._buf[8:16], "big")
                header = 16
            if size < header:
                raise ValueError(f"corrupt MP4 box (size {size})")
            if len(self._buf) < size:
                return
            box_type = bytes(self._buf[4:8])
            box = bytes(self._buf[:size])
            del self._buf[:size]
            self._handle(box_type, box)

    def _handle(self, box_type: bytes, box: bytes) -> None:
        if self.init_segment is None:
            self._init += box
            if box_type == b"moov":
                self.init_segment = bytes(self._init)
                self._on_init(self.init_segment)
            return
        if box_type == b"moof":
            self._moof = box
        elif box_type == b"mdat" and self._moof is not None:
            self._on_segment(self._moof + box)
            self._moof = None
        # anything else (free, mfra, ...) carries nothing a live player needs


def codecs_from_init(init: bytes) -> str:
    """Build the RFC 6381 codecs string (e.g. 'avc1.64001e,mp4a.40.2') from an init segment."""
    codecs = []
    i = init.find(b"avcC")
    if i >= 0:
        profile, compat, level = init[i + 5], init[i + 6], init[i + 7]
        codecs.append(f"avc1.{profile:02x}{compat:02x}{level:02x}")
    if init.find(b"mp4a") >= 0:
        codecs.append("mp4a.40.2")
    return ",".join(codecs)
