"""The disc library: finds DVD images on disk and keeps a menu-screenshot thumbnail for each."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import queue
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from .discinfo import DiscInfo, read_disc_info
from .gstutil import Gst, GstVideo, navigation_command, struct_uint64_array

log = logging.getLogger(__name__)

COVER_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp")
THUMB_CAPS = "video/x-raw,format=RGB,pixel-aspect-ratio=1/1,height=480"


@dataclass
class Disc:
    id: str
    path: str  # an .iso file or a folder containing VIDEO_TS
    title: str
    size: int
    mtime: float
    info: DiscInfo
    cover: str | None = None  # user-supplied artwork next to the image
    meta: dict = field(default_factory=dict)  # filled in by the thumbnailer

    @property
    def cache_key(self) -> str:
        return f"{self.id}-{int(self.mtime)}-{self.size}"


def pretty_title(name: str) -> str:
    name = re.sub(r"[_]+", " ", name).strip()
    if name.isupper():  # volume-label style: BUZZ_LIGHTYEAR -> Buzz Lightyear
        name = name.title()
    return name


def _dir_size(path: str) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def _find_cover(stem: Path, folder: Path | None = None) -> str | None:
    """Artwork named like the disc (Movie.iso -> Movie.jpg), or folder.jpg/cover.jpg in a rip folder."""
    candidates = [stem.parent / (stem.name + ext) for ext in COVER_EXTENSIONS]
    if folder is not None:
        candidates += [folder / f"{stem}{ext}" for stem in ("cover", "folder", "poster") for ext in COVER_EXTENSIONS]
    for c in candidates:
        if c.is_file():
            return str(c)
    return None


class Library:
    def __init__(self, root: Path, cache_dir: Path):
        self.root = root.resolve()
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._discs: dict[str, Disc] = {}
        self._info_cache: dict[tuple, DiscInfo] = {}
        self._lock = threading.Lock()
        self._queue: queue.Queue[Disc] = queue.Queue()
        self._queued: set[str] = set()
        self._failed: set[str] = set()
        threading.Thread(target=self._thumbnail_worker, name="thumbnailer", daemon=True).start()

    # ---- scanning ---------------------------------------------------------------

    def scan(self) -> list[Disc]:
        found: dict[str, Disc] = {}
        for dirpath, dirnames, filenames in os.walk(self.root, followlinks=True):
            dirnames.sort()
            if any(d.upper() == "VIDEO_TS" for d in dirnames):
                self._add(found, Path(dirpath), is_dir=True)
                dirnames[:] = [d for d in dirnames if d.upper() != "VIDEO_TS"]
            for name in sorted(filenames):
                if name.lower().endswith(".iso"):
                    self._add(found, Path(dirpath) / name, is_dir=False)
        with self._lock:
            self._discs = found
        for disc in found.values():
            self._ensure_thumbnail(disc)
        return sorted(found.values(), key=lambda d: d.title.lower())

    def _add(self, found: dict[str, Disc], path: Path, is_dir: bool) -> None:
        try:
            st = path.stat()
        except OSError:
            return
        if path.resolve() == self.root and is_dir:
            rel = path.name
        else:
            rel = str(path.relative_to(self.root)) if path.is_relative_to(self.root) else str(path)
        disc_id = hashlib.sha1(rel.encode()).hexdigest()[:12]
        size = _dir_size(str(path)) if is_dir else st.st_size
        key = (str(path), st.st_mtime, size)
        info = self._info_cache.get(key)
        if info is None:
            info = self._info_cache[key] = read_disc_info(str(path))
        title = pretty_title(path.name if is_dir else path.stem)
        cover = _find_cover(path, path) if is_dir else _find_cover(path.with_suffix(""))
        disc = Disc(disc_id, str(path), title, size, st.st_mtime, info, cover)
        disc.meta = self._load_meta(disc)
        found[disc_id] = disc

    def get(self, disc_id: str) -> Disc | None:
        with self._lock:
            disc = self._discs.get(disc_id)
        if disc is None:  # maybe added since the last scan
            self.scan()
            with self._lock:
                disc = self._discs.get(disc_id)
        return disc

    # ---- thumbnails -------------------------------------------------------------

    def _thumb_file(self, disc: Disc) -> Path:
        return self.cache_dir / f"{disc.cache_key}.jpg"

    def _meta_file(self, disc: Disc) -> Path:
        return self.cache_dir / f"{disc.cache_key}.json"

    def _load_meta(self, disc: Disc) -> dict:
        try:
            return json.loads(self._meta_file(disc).read_text())
        except (OSError, ValueError):
            return {}

    def thumbnail_path(self, disc: Disc) -> str | None:
        if disc.cover:
            return disc.cover
        thumb = self._thumb_file(disc)
        return str(thumb) if thumb.is_file() else None

    def thumbnail_state(self, disc: Disc) -> str:
        if self.thumbnail_path(disc):
            return "ready"
        return "failed" if disc.cache_key in self._failed else "pending"

    def _ensure_thumbnail(self, disc: Disc) -> None:
        # Always run once per disc version: it also collects title/runtime info for the card.
        if self._meta_file(disc).is_file() and (disc.cover or self._thumb_file(disc).is_file()):
            return
        with self._lock:
            if disc.cache_key in self._queued or disc.cache_key in self._failed:
                return
            self._queued.add(disc.cache_key)
        self._queue.put(disc)

    def _thumbnail_worker(self) -> None:
        while True:
            disc = self._queue.get()
            started = time.monotonic()
            try:
                meta = capture_menu_thumbnail(disc.path, self._thumb_file(disc))
                self._meta_file(disc).write_text(json.dumps(meta))
                disc.meta = meta
                log.info("thumbnail for %r ready (%.1fs)", disc.title, time.monotonic() - started)
            except Exception as exc:  # a broken image must not stop the worker
                log.warning("could not make a thumbnail for %r: %s", disc.title, exc)
                self._failed.add(disc.cache_key)
            finally:
                with self._lock:
                    self._queued.discard(disc.cache_key)

    # ---- JSON -------------------------------------------------------------------

    def to_json(self, disc: Disc) -> dict:
        state = self.thumbnail_state(disc)
        return {
            "id": disc.id,
            "title": disc.title,
            "size": disc.size,
            "format": disc.info.to_json(),
            "titles": disc.meta.get("titles"),
            "runtime": disc.meta.get("main_duration"),
            "thumbnail": f"/api/discs/{disc.id}/thumbnail?v={disc.cache_key}" if state == "ready" else None,
            "thumbnail_state": state,
        }


def capture_menu_thumbnail(device: str, out_path: Path, max_wait: float = 30.0) -> dict:
    """Play the disc for a few seconds and snapshot its first menu screen.

    Waits for the first menu (the first button highlight), lets motion menus animate
    in for a moment, then grabs a frame. Discs that start with warnings or trailers
    get a MENU press after a while; if nothing shows up we keep whatever is on screen.
    """
    from .player import DvdPipeline  # avoid an import cycle at module load

    latest: list[Gst.Sample | None] = [None]
    menu_at: list[float | None] = [None]
    durations: list[int] = []
    errors: list[str] = []

    def on_event(name: str, _s: Gst.Structure) -> None:
        if name == "dvd-spu-highlight" and menu_at[0] is None:
            menu_at[0] = time.monotonic()

    def on_message(msg: Gst.Message) -> None:
        if msg.type == Gst.MessageType.ERROR:
            errors.append(msg.parse_error()[0].message)
        elif msg.type == Gst.MessageType.ELEMENT:
            s = msg.get_structure()
            if s is not None and s.get_string("event") == "dvd-title-info":
                durations[:] = struct_uint64_array(s, "title-durations")

    pipe = DvdPipeline(device, THUMB_CAPS, on_video=lambda s: latest.__setitem__(0, s),
                       on_dvd_event=on_event, on_message=on_message)
    pipe.set_state(Gst.State.PLAYING)
    started = time.monotonic()
    pressed_menu = False
    try:
        while not errors:
            time.sleep(0.2)
            now = time.monotonic()
            if menu_at[0] is not None and now - menu_at[0] > 2.5:
                break
            if not pressed_menu and menu_at[0] is None and now - started > 12:
                pipe.send_upstream(navigation_command(GstVideo.NavigationCommand.MENU3))  # root menu
                pressed_menu = True
            if now - started > max_wait:
                break
        sample = latest[0]
    finally:
        pipe.close()
    if errors and sample is None:
        raise RuntimeError(errors[0])
    if sample is None:
        raise RuntimeError("no picture")
    jpeg = GstVideo.video_convert_sample(sample, Gst.Caps.from_string("image/jpeg"), 5 * Gst.SECOND)
    buf = jpeg.get_buffer()
    out_path.write_bytes(buf.extract_dup(0, buf.get_size()))

    real = [d for d in durations[1:] if d not in (0, Gst.CLOCK_TIME_NONE)]
    return {
        "titles": len(durations) if durations else None,  # last title is missing from the list
        "main_duration": round(max(real) / Gst.SECOND) if real else None,
        "menu_found": menu_at[0] is not None,
        "generated": int(time.time()),
    }
