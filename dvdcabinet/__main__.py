"""python -m dvdcabinet --library ./DVDs

Settings come from, in order of precedence: command-line flags, environment
variables, a .env file next to run.sh, and the defaults below.
"""

from __future__ import annotations

import argparse
import time
import logging
import os
import socket
from pathlib import Path

from . import gstutil

ROOT = Path(__file__).resolve().parent.parent


def _lan_address() -> str | None:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))  # no packets are sent; just picks the outgoing interface
            return s.getsockname()[0]
    except OSError:
        return None


def load_env_file(path: Path) -> dict[str, str]:
    """KEY=value lines; # comments, optional quotes and a leading `export` are allowed."""
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, sep, value = line.partition("=")
        if not sep or line.startswith("#"):
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        else:
            value = value.split(" #", 1)[0].strip()
        values[key.strip()] = value
    return values


def _udp_port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.bind(("0.0.0.0", port))
            return True
        except OSError:
            return False


def _seed_library(db, folder: Path) -> None:
    """On first start, add the configured library folder so there's something to watch."""
    if db.get_setting("library_seeded"):
        return
    db.set_setting("library_seeded", "1")
    if folder.is_dir():
        db.execute("INSERT OR IGNORE INTO library_folders (path, added_at) VALUES (?, ?)",
                   (str(folder.resolve()), int(time.time())))
        logging.info("added library folder %s", folder.resolve())


def main() -> None:
    settings = {**load_env_file(ROOT / ".env"), **os.environ}

    def setting(name: str, default=None):
        value = settings.get(f"DVD_{name}", "")
        return value if value != "" else default

    def path_setting(name: str, default: Path) -> Path:
        value = setting(name)
        return default if value is None else (ROOT / value).resolve()  # relative to the project folder

    parser = argparse.ArgumentParser(
        prog="dvdcabinet", description="Stream DVD images, menus and all, to a browser.",
        epilog="Every option can also be set in .env or the environment as DVD_<NAME>, e.g. DVD_PORT=8080.")
    parser.add_argument("--data-dir", type=Path, default=path_setting("DATA_DIR", ROOT / "data"),
                        help="where the server keeps its database and thumbnails (DVD_DATA_DIR)")
    parser.add_argument("--library", type=Path, default=path_setting("LIBRARY", ROOT / "DVDs"),
                        help="library folder to start with on first run; after that, manage folders "
                             "on the admin page (DVD_LIBRARY)")
    parser.add_argument("--host", default=setting("HOST", "0.0.0.0"),
                        help="address to listen on, default all interfaces (DVD_HOST)")
    parser.add_argument("--port", type=int, default=int(setting("PORT", 8080)), help="web port (DVD_PORT)")
    parser.add_argument("--crf", type=int, default=int(setting("CRF", 20)),
                        help="x264 quality, lower is better, default 20 (DVD_CRF)")
    parser.add_argument("--webrtc-codec", choices=("auto", "h264", "vp8"), default=setting("WEBRTC_CODEC", "auto"),
                        help="video codec for WebRTC, default H.264 if the browser supports it (DVD_WEBRTC_CODEC)")
    parser.add_argument("--rtc-port", type=int, default=setting("RTC_PORT"),
                        help="the one UDP port WebRTC viewers from outside your network use; forward it "
                             "to this machine (DVD_RTC_PORT)")
    parser.add_argument("--public-ip", metavar="IP", default=setting("PUBLIC_IP"),
                        help="your router's public address, used with --rtc-port (DVD_PUBLIC_IP)")
    parser.add_argument("--no-webrtc", action="store_true", help="always stream over the WebSocket (MSE)")
    parser.add_argument("--dvd-logs", action="store_true", help="show libdvdread/libdvdnav console output")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()
    args.rtc_port = int(args.rtc_port) if args.rtc_port is not None else None
    if bool(args.public_ip) != bool(args.rtc_port):
        parser.error("--rtc-port and --public-ip go together: the port is forwarded to this machine, "
                     "the IP is where browsers outside send to")

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    if not args.dvd_logs:
        gstutil.filter_native_output()
    gstutil.init()
    if not gstutil.have_libdvdcss():
        logging.warning("libdvdcss isn't installed, so copy-protected discs (most commercial DVDs) won't play. "
                        "On Debian/Ubuntu: sudo apt install libdvd-pkg && sudo dpkg-reconfigure libdvd-pkg")

    from aiohttp import web

    from . import webrtc
    from .accounts import Accounts
    from .db import Database
    from .library import Library
    from .server import WebRtcConfig, create_app
    from .udpmux import UdpMux

    lan = _lan_address()
    rtc = None
    if not args.no_webrtc:
        reason = webrtc.unavailable_reason()
        if reason:
            logging.warning("WebRTC disabled, streaming over the WebSocket instead: %s", reason)
        else:
            mux = None
            if args.rtc_port:
                if lan is None:
                    parser.error("--rtc-port: could not work out this machine's LAN address")
                if not _udp_port_free(args.rtc_port):
                    parser.error(f"UDP port {args.rtc_port} is already in use")
                mux = UdpMux(lan, args.rtc_port, args.public_ip)
                logging.info("WebRTC from outside: browsers connect to %s:%d/udp (forward it to %s:%d)",
                             args.public_ip, args.rtc_port, lan, args.rtc_port)
            rtc = WebRtcConfig(args.webrtc_codec, mux)
    db = Database(args.data_dir / "dvdcabinet.db")
    accounts = Accounts(db)
    accounts.prune_sessions()
    _seed_library(db, args.library)
    library = Library(args.data_dir / "cache")
    library.set_folders([r["path"] for r in db.all("SELECT path FROM library_folders ORDER BY path")])
    library.scan_in_background()

    urls = [f"http://localhost:{args.port}/"]
    if args.host in ("0.0.0.0", "::") and lan:
        urls.append(f"http://{lan}:{args.port}/")
    print("\n  DVD Cabinet is running:  " + "   ".join(urls) + "\n", flush=True)
    if accounts.needs_setup():
        print("  Open it to claim this server and create the admin account. From outside your\n"
              f"  local network you'll also need this setup code:  {accounts.setup_code}\n", flush=True)
    web.run_app(create_app(db, accounts, library, args.crf, rtc), host=args.host, port=args.port, print=None,
                shutdown_timeout=5, access_log=logging.getLogger("aiohttp.access") if args.verbose else None)


if __name__ == "__main__":
    main()
