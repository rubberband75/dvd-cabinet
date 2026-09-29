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
another device on your network).

Put `.iso` images, or ripped folders that contain `VIDEO_TS/`, in `DVDs/`,
or point at another folder with `./run.sh --library /path/to/discs`. New discs
appear the next time you open or reload the library page, with no server
restart. The first time a disc is seen, the
server plays it for a few seconds in the background and uses a screenshot of its
menu as the thumbnail. To use your own artwork instead, put an image next to the
disc with the same name (`Movie.iso` → `Movie.jpg`), or a `cover.jpg` inside a
rip folder.

### Requirements

Linux with GStreamer 1.22+ and PyGObject. On Ubuntu / Linux Mint / Debian:

```bash
sudo apt install python3-venv python3-gi gir1.2-gstreamer-1.0 gir1.2-gst-plugins-base-1.0 \
  gstreamer1.0-plugins-base gstreamer1.0-plugins-good gstreamer1.0-plugins-bad \
  gstreamer1.0-plugins-ugly gstreamer1.0-libav
```

Images of copy-protected (CSS) discs also need `libdvdcss2`; on Ubuntu/Mint that
comes from `sudo apt install libdvd-pkg && sudo dpkg-reconfigure libdvd-pkg`.
Everything above is already present on this machine.

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
player per viewer and streams its output to the browser like a live broadcast.
Your button presses travel back up the same connection.

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
             x264 + AAC ─► fragmented MP4 ─► WebSocket ─► browser (Media Source Extensions)
                                                ▲
             keys / mouse / remote (JSON) ──────┘
```

* `dvdcabinet/player.py`: one playback session (the DVD pipeline, the pacer, the encoder).
* `dvdcabinet/server.py`: HTTP API, thumbnails, and one session per WebSocket.
* `dvdcabinet/library.py`: finds discs and makes the menu thumbnails.
* `dvdcabinet/discinfo.py`: reads NTSC/PAL and 4:3 vs 16:9 from the disc's IFO files, so
  each stream is shaped to fit its disc (a 4:3 disc gets a 4:3 stream with no side bars).
* `web/`: the library grid and the player (plain HTML/CSS/JS, no build step).

## Options

```
./run.sh --library DIR   folder to scan (default: ./DVDs)
         --port 8080     --host 0.0.0.0 (all interfaces; use 127.0.0.1 for this machine only)
         --crf 20        video quality, lower is better (18–23 is sensible)
         --cache DIR     where thumbnails live (default: ./.cache)
         --dvd-logs      show libdvdread/libdvdnav console output (hidden by default)
         -v              verbose logging
```

## Good to know

* Each browser tab is its own DVD player, and reloading the page "reinserts" the
  disc. Two people can watch different (or the same) discs at once; each viewer
  costs about a quarter of one CPU core.
* Button presses take about 0.4 s to show up. That is mostly the browser's own
  playback buffer, and not far off a real DVD player's menu lag.
* Audio is mixed down to stereo. Audio and subtitle languages are switched with
  the disc's own setup menus, as on a TV.
* The server has no login and no HTTPS, which is fine on a home network. Don't
  expose it to the internet as-is.
* Tested with Firefox. Chrome, Edge and Safari support the same streaming
  features; iPhones need iOS 17.1 or newer.
* Some discs have quirks built into their menus. On *Buzz Lightyear*, the first
  arrow press on the main menu briefly re-enters the menu. That comes from the
  disc's own button data, not from this app.
