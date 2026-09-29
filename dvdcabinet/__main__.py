"""python -m dvdcabinet --library ./DVDs"""

from __future__ import annotations

import argparse
import logging
import socket
from pathlib import Path

from . import gstutil


def _lan_address() -> str | None:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))  # no packets are sent; just picks the outgoing interface
            return s.getsockname()[0]
    except OSError:
        return None


def main() -> None:
    root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(prog="dvdcabinet", description="Stream DVD images, menus and all, to a browser.")
    parser.add_argument("--library", type=Path, default=root / "DVDs", help="folder with .iso files / VIDEO_TS folders")
    parser.add_argument("--cache", type=Path, default=root / ".cache", help="where thumbnails are kept")
    parser.add_argument("--host", default="0.0.0.0", help="address to listen on (default: all interfaces)")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--crf", type=int, default=20, help="x264 quality, lower is better (default 20)")
    parser.add_argument("--dvd-logs", action="store_true", help="show libdvdread/libdvdnav console output")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    if not args.dvd_logs:
        gstutil.filter_native_output()
    gstutil.init()

    from aiohttp import web

    from .library import Library
    from .server import create_app

    if not args.library.is_dir():
        parser.error(f"library folder {args.library} does not exist")
    library = Library(args.library, args.cache)
    discs = library.scan()
    logging.info("library %s: %d disc(s)", args.library.resolve(), len(discs))

    urls = [f"http://localhost:{args.port}/"]
    lan = _lan_address()
    if args.host in ("0.0.0.0", "::") and lan:
        urls.append(f"http://{lan}:{args.port}/")
    print("\n  DVD Cabinet is running:  " + "   ".join(urls) + "\n", flush=True)
    web.run_app(create_app(library, args.crf), host=args.host, port=args.port, print=None, shutdown_timeout=5,
                access_log=logging.getLogger("aiohttp.access") if args.verbose else None)


if __name__ == "__main__":
    main()
