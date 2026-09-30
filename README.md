# DVD Cabinet

A personal media server that plays DVD disc images **with their real menus**:
motion menus, button highlights, special features, chapter menus, the lot. You
browse your discs in a web page, click one, and drive it with the keyboard, the
mouse, or an on-screen remote, just like a DVD player.

## Quick start

```bash
./run.sh                  # first run creates .venv and installs aiohttp
```

Then open <http://localhost:8080/> (or `http://<this-machine's-ip>:8080/` from
another device on your network). The first visit asks you to **claim the server**
by creating the admin account; see [Accounts](#accounts).

Your discs are `.iso` images, or ripped folders that contain `VIDEO_TS/`. On first
run the `DVDs/` folder next to `run.sh` (or `DVD_LIBRARY`) is added to the library;
add more folders, anywhere on the server, under **Server settings → Library**, and
press **Scan for new discs** when you add images. The first time a disc is seen, the
server plays it for a few seconds in the background and uses a screenshot of its
menu as the thumbnail. To use your own artwork instead, put an image next to the
disc with the same name (`Movie.iso` → `Movie.jpg`), or a `cover.jpg` inside a
rip folder.

## Accounts

Everyone signs in; nothing but the sign-in page is reachable without an account.

* **Claiming the server:** while no accounts exist, the web page offers to create
  the admin account. From the local network that's all it takes. From anywhere
  else (for example through your reverse proxy) it also asks for a setup code,
  printed in the server's log at startup, so a stranger who finds the site first
  can't claim it.
* **Server settings** (admins, from the menu under your name): add people, set
  their passwords, make them admins or delete them (whatever they're watching
  stops), and manage the library folders.
* Anyone can change their own password from the same menu; that signs them out
  on their other devices.
* Ten wrong passwords in 15 minutes lock that account and address out for a while.

Everything the server keeps (a SQLite database with the accounts, sign-in
sessions and library folders, plus the thumbnail cache) lives in one folder,
`DVD_DATA_DIR` (default `data/`). Back that folder up and you've backed up the
server. Your disc images are only ever read.

### Requirements

Linux with GStreamer 1.20+ (e.g. Ubuntu 22.04 or newer, Debian 12) and PyGObject.
On Ubuntu / Linux Mint / Debian:

```bash
sudo apt install python3-venv python3-gi gir1.2-gstreamer-1.0 gir1.2-gst-plugins-base-1.0 \
  gstreamer1.0-plugins-base gstreamer1.0-plugins-good gstreamer1.0-plugins-bad \
  gstreamer1.0-plugins-ugly gstreamer1.0-libav \
  gstreamer1.0-nice gir1.2-gst-plugins-bad-1.0     # these two enable WebRTC
```

Without the last two packages everything still works, just streamed over the
WebSocket with more lag (the server logs which one is missing).

Images of copy-protected (CSS) discs, which is most commercial DVDs, also need
`libdvdcss2`; on Ubuntu/Mint/Debian that comes from
`sudo apt install libdvd-pkg && sudo dpkg-reconfigure libdvd-pkg` (on Debian,
enable the `contrib` component first). The server warns at startup if it's missing.

If the server has a firewall (ufw, or the Proxmox firewall for a container),
allow the web port (`DVD_PORT`, 8080/tcp) and, with `DVD_RTC_PORT` set, that UDP
port. That one UDP port is enough for WebRTC from home as well as from outside.

## Controls

| Key | Action |
| --- | --- |
| Arrow keys | Move around menus (while watching a title, ← / → skip 10 s) |
| Enter | Select the highlighted button |
| Space / K | Pause / resume |
| M | Disc menu. Press it again to resume where you were (the disc's own resume) |
| T | Title menu |
| , / . (or PgUp / PgDn) | Previous / next chapter |
| F | Fullscreen |
| R | On-screen remote |
| ? | Shortcut help |

You can also point at menu buttons with the mouse (the pointer changes over a
button) and click them, or tap them on a phone. The ☰ button lists every title
on the disc so you can jump straight to one.

## How it works

Browsers can't play DVDs, and DVD menus aren't just video: they are small
programs run by a "virtual machine" on the disc. So the server runs a real DVD
player per viewer and streams its output to the browser like a video call
(WebRTC). Your button presses travel back over a WebSocket.

```
DVD image ─► rsndvdbin (libdvdnav: runs the disc's menus and navigation)
               ├─ video + subpicture ─► dvdspu (draws button highlights) ─► deinterlace ─► scale
               └─ audio ─► 48 kHz stereo
                        │
                        ▼
             MediaPacer: re-sends the last frame during still menus and fills silence,
             so the stream never stops or jumps, whatever the disc does
                        │
                        ▼
             H.264 (or VP8) + Opus ─► WebRTC ─► browser               (UDP, direct)
   fallback: H.264 + AAC ─► fragmented MP4 over the WebSocket ─► browser (Media Source Extensions)

browser ─► WebSocket ─► server: keys / mouse / remote, plus the WebRTC offer/answer and ICE
```

* `dvdcabinet/player.py`: one playback session (the DVD pipeline and the pacer).
* `dvdcabinet/webrtc.py`: the WebRTC output; `dvdcabinet/fmp4.py`: the WebSocket fallback.
* `dvdcabinet/server.py`: HTTP API, thumbnails, and one session per WebSocket.
* `dvdcabinet/library.py`: finds discs and makes the menu thumbnails.
* `dvdcabinet/accounts.py`, `db.py`: users, passwords and sessions in SQLite;
  `web_auth.py`: sign-in and the guards on every route; `admin.py`: Server settings.
* `dvdcabinet/discinfo.py`: reads NTSC/PAL and 4:3 vs 16:9 from the disc's IFO files, so
  each stream is shaped to fit its disc (a 4:3 disc gets a 4:3 stream with no side bars).
* `web/`: the library grid and the player (plain HTML/CSS/JS, no build step).

## Settings

Settings live in `.env` (copy `.env.example` to start; `run.sh` reads it). Environment
variables of the same name, and command-line flags, override it:

| `.env` | Flag | Default | |
| --- | --- | --- | --- |
| `DVD_DATA_DIR` | `--data-dir` | `data` | database and thumbnails |
| `DVD_LIBRARY` | `--library` | `DVDs` | library folder added on the very first run |
| `DVD_HOST` | `--host` | `0.0.0.0` | all interfaces; `127.0.0.1` for this machine only |
| `DVD_PORT` | `--port` | `8080` | web port |
| `DVD_CRF` | `--crf` | `20` | video quality, lower is better (18–23 is sensible) |
| `DVD_WEBRTC_CODEC` | `--webrtc-codec` | `auto` | `auto` (H.264 if the browser has it), `h264`, `vp8` |
| `DVD_RTC_PORT` | `--rtc-port` | | UDP port for WebRTC from outside (see below) |
| `DVD_PUBLIC_IP` | `--public-ip` | | your router's public address (see below) |

Also `--no-webrtc` (always stream over the WebSocket), `--dvd-logs` (show
libdvdread/libdvdnav output) and `-v` (verbose logging). Adding `?transport=mse`
to the page address makes one browser use the WebSocket fallback.

## Watching from outside your network

1. Put the server behind a reverse proxy with HTTPS. It needs WebSocket support,
   i.e. the `Upgrade` and `Connection` headers passed through.
2. For WebRTC, forward one UDP port on your router to this machine, with the same
   number on both sides, and set it with your public IP in `.env`:
   ```
   DVD_RTC_PORT=50000
   DVD_PUBLIC_IP=203.0.113.5
   ```

Every viewer from outside shares that one port: the server tells their browser
to send to `DVD_PUBLIC_IP:DVD_RTC_PORT` and passes each browser's packets to the
right session (`dvdcabinet/udpmux.py`). Browsers at home still connect directly.
Without the port forward, remote viewers get the WebSocket fallback, which works
through any proxy. The log shows each viewer's real address if the proxy sends
`X-Real-IP`.

Sign-in cookies are marked Secure when the proxy says the visit was HTTPS
(`X-Forwarded-Proto`), so serve the site over HTTPS.

## Good to know

* Each browser tab is its own DVD player, and reloading the page "reinserts" the
  disc. Two people can watch different (or the same) discs at once; each viewer
  costs about a quarter of one CPU core.
* Button presses show up in about 0.1 s over WebRTC (about 0.4 s on the WebSocket
  fallback, where the browser buffers more).
* WebRTC needs UDP between the browser and this machine. If it's blocked (a
  firewall, some VPNs, no port forward), the player notices within a few seconds
  and switches to the WebSocket fallback on its own.
* Audio is mixed down to stereo. Audio and subtitle languages are switched with
  the disc's own setup menus, as on a TV.
* The server doesn't do HTTPS itself. On the internet, put it behind a proxy
  that does (see above).
* Tested with Firefox. Chrome, Edge and Safari support WebRTC with H.264 as well;
  on the fallback path iPhones need iOS 17.1 or newer.
* Some discs have quirks built into their menus. On *Buzz Lightyear*, the first
  arrow press on the main menu briefly re-enters the menu. That comes from the
  disc's own button data, not from this app.
