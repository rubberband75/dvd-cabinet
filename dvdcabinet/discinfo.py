"""Read basic DVD-Video attributes straight from a disc's IFO files.

Works on ISO images (through the ISO 9660 "bridge" filesystem that every DVD-Video
disc carries alongside UDF) and on plain VIDEO_TS folders. We only need a few
facts before playback starts: the volume label, NTSC vs PAL, and whether any
menu or title is 16:9 -- that decides the geometry of the stream we send.
"""

from __future__ import annotations

import os
import struct
from dataclasses import dataclass

SECTOR = 2048


@dataclass
class DiscInfo:
    volume_id: str | None = None
    standard: str = "NTSC"  # "NTSC" or "PAL"
    widescreen: bool = False  # True if any menu or title is 16:9
    title_sets: int = 0

    def to_json(self) -> dict:
        return {
            "volume_id": self.volume_id,
            "standard": self.standard,
            "aspect": "16:9" if self.widescreen else "4:3",
            "title_sets": self.title_sets,
        }


class _Iso9660:
    """Just enough ISO 9660 to find and read files by path."""

    def __init__(self, f):
        self.f = f
        pvd = self._read(16, SECTOR)
        if pvd[1:6] != b"CD001":
            raise ValueError("no ISO 9660 filesystem")
        self.volume_id = pvd[40:72].decode("ascii", "replace").strip() or None
        self.root = self._parse_record(pvd[156:190])

    def _read(self, lba: int, size: int) -> bytes:
        self.f.seek(lba * SECTOR)
        return self.f.read(size)

    @staticmethod
    def _parse_record(rec: bytes) -> tuple[int, int, bool, str]:
        # Multi-byte fields are stored both-endian; use the little-endian half.
        lba, size = struct.unpack_from("<I", rec, 2)[0], struct.unpack_from("<I", rec, 10)[0]
        is_dir = bool(rec[25] & 0x02)
        name = rec[33:33 + rec[32]].decode("ascii", "replace")
        return lba, size, is_dir, name

    def _list(self, directory: tuple[int, int, bool, str]):
        lba, size, _, _ = directory
        data = self._read(lba, size)
        pos = 0
        while pos < len(data):
            length = data[pos]
            if length == 0:  # records never straddle sectors; skip the padding
                pos = (pos // SECTOR + 1) * SECTOR
                continue
            yield self._parse_record(data[pos:pos + length])
            pos += length

    def find(self, *parts: str):
        node = self.root
        for part in parts:
            for rec in self._list(node):
                if rec[3].split(";")[0].upper() == part.upper():
                    node = rec
                    break
            else:
                return None
        return node

    def read_file(self, *parts: str, size: int = SECTOR) -> bytes | None:
        rec = self.find(*parts)
        if rec is None or rec[2]:
            return None
        return self._read(rec[0], min(size, rec[1]))


def _video_attr(data: bytes, offset: int) -> tuple[str, bool]:
    b = data[offset]
    standard = "PAL" if (b >> 4) & 0x3 == 1 else "NTSC"
    widescreen = (b >> 2) & 0x3 == 3
    return standard, widescreen


def read_disc_info(path: str) -> DiscInfo:
    """Best effort: returns defaults for anything that can't be read."""
    info = DiscInfo()
    try:
        if os.path.isdir(path):
            vts_dir = os.path.join(path, "VIDEO_TS")

            def read(name: str) -> bytes | None:
                p = os.path.join(vts_dir, name)
                if not os.path.exists(p):
                    return None
                with open(p, "rb") as fh:
                    return fh.read(SECTOR)

            _fill(info, read)
            info.volume_id = os.path.basename(os.path.normpath(path))
        else:
            with open(path, "rb") as fh:
                iso = _Iso9660(fh)
                info.volume_id = iso.volume_id
                _fill(info, lambda name: iso.read_file("VIDEO_TS", name))
    except (OSError, ValueError, IndexError, struct.error):
        pass
    return info


def _fill(info: DiscInfo, read) -> None:
    vmg = read("VIDEO_TS.IFO")
    if not vmg or not vmg.startswith(b"DVDVIDEO-VMG"):
        return
    info.title_sets = struct.unpack_from(">H", vmg, 0x3E)[0]
    info.standard, info.widescreen = _video_attr(vmg, 0x100)
    for n in range(1, info.title_sets + 1):
        vts = read(f"VTS_{n:02d}_0.IFO")
        if not vts or not vts.startswith(b"DVDVIDEO-VTS"):
            continue
        for offset in (0x100, 0x200):  # title-set menus, then the titles themselves
            _, wide = _video_attr(vts, offset)
            info.widescreen |= wide
