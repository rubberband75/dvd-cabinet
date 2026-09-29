"""Re-frame mp4mux's fragmented-MP4 byte stream into Media Source Extensions units.

mp4mux emits headers and fragments in arbitrary small pieces (an mdat header in one
buffer, its payload in the next, ...). Browsers want the init segment (ftyp+moov)
first, then complete media segments (moof+mdat), so we split on top-level boxes.
"""

from __future__ import annotations

from typing import Callable


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
