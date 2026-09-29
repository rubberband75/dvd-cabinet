'use strict';

/* DVD Cabinet web client.
 *
 * Library: a grid of discs from /api/discs (thumbnails are menu screenshots taken by the server).
 * Player:  one WebSocket per viewing. The server runs a virtual DVD player and streams it to us
 *          over WebRTC (falling back to fragmented MP4 over the socket, played with Media Source
 *          Extensions); remote-control presses, mouse moves and clicks go back up the socket as JSON.
 */

const $ = (id) => document.getElementById(id);

const ICONS = {
  back: '<path d="M15 18l-6-6 6-6"/>',
  play: '<path d="M7 4.5v15l12.5-7.5z" fill="currentColor" stroke="none"/>',
  pause: '<path d="M8 5v14M16 5v14"/>',
  playpause: '<path d="M4 5.5v13l8.5-6.5z" fill="currentColor" stroke="none"/><path d="M16 6v12M20 6v12"/>',
  prev: '<path d="M6 5v14"/><path d="M19 5.5v13L9.5 12z" fill="currentColor" stroke="none"/>',
  next: '<path d="M18 5v14"/><path d="M5 5.5v13l9.5-6.5z" fill="currentColor" stroke="none"/>',
  help: '<circle cx="12" cy="12" r="9.5"/><path d="M9.4 9.3a2.7 2.7 0 0 1 5.2 1c0 1.8-2.6 2.4-2.6 4"/><path d="M12 17.4v.1"/>',
  list: '<path d="M9 6h12M9 12h12M9 18h12"/><path d="M4 6h.01M4 12h.01M4 18h.01" stroke-width="3"/>',
  remote: '<rect x="7" y="2.5" width="10" height="19" rx="3"/><circle cx="12" cy="8.5" r="2"/><path d="M10 14h.01M14 14h.01M10 17.5h.01M14 17.5h.01" stroke-width="2.6"/>',
  expand: '<path d="M8 3H5a2 2 0 0 0-2 2v3M21 8V5a2 2 0 0 0-2-2h-3M3 16v3a2 2 0 0 0 2 2h3M16 21h3a2 2 0 0 0 2-2v-3"/>',
  shrink: '<path d="M8 3v3a2 2 0 0 1-2 2H3M21 8h-3a2 2 0 0 1-2-2V3M3 16h3a2 2 0 0 1 2 2v3M16 21v-3a2 2 0 0 1 2-2h3"/>',
  up: '<path d="M6 15l6-6 6 6"/>',
  down: '<path d="M6 9l6 6 6-6"/>',
  left: '<path d="M15 6l-6 6 6 6"/>',
  right: '<path d="M9 6l6 6-6 6"/>',
};
const icon = (name) => `<svg class="i" viewBox="0 0 24 24" aria-hidden="true">${ICONS[name] || ''}</svg>`;
const setIcon = (el, name) => { el.innerHTML = icon(name); };

function discArt(spinning) {
  return `<svg viewBox="0 0 48 48" aria-hidden="true" class="${spinning ? 'spin' : ''}">
    <circle cx="24" cy="24" r="22" fill="#222a3b"/>
    <circle cx="24" cy="24" r="16.5" fill="none" stroke="#fff" stroke-opacity=".07"/>
    <circle cx="24" cy="24" r="12" fill="none" stroke="#fff" stroke-opacity=".05"/>
    <path d="M24 2a22 22 0 0 1 20.6 14.3" fill="none" stroke="#f5b53d" stroke-opacity=".55" stroke-width="2" stroke-linecap="round"/>
    <circle cx="24" cy="24" r="7" fill="#0b0d12"/></svg>`;
}

const store = {
  get(key, fallback) { try { const v = localStorage.getItem(`dvdcabinet.${key}`); return v === null ? fallback : JSON.parse(v); } catch { return fallback; } },
  set(key, value) { try { localStorage.setItem(`dvdcabinet.${key}`, JSON.stringify(value)); } catch { /* private mode */ } },
};

function fmtTime(sec) {
  if (sec == null || !isFinite(sec)) return '–:––';
  sec = Math.max(0, Math.floor(sec));
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
  const ss = String(s).padStart(2, '0');
  return h ? `${h}:${String(m).padStart(2, '0')}:${ss}` : `${m}:${ss}`;
}
const fmtRuntime = (sec) => { const m = Math.round(sec / 60); return m >= 60 ? `${Math.floor(m / 60)} h ${m % 60} min` : `${m} min`; };
const fmtSize = (b) => (b >= 1e9 ? `${(b / 1e9).toFixed(1)} GB` : `${Math.round(b / 1e6)} MB`);

/* ---- Library ------------------------------------------------------------ */

const Library = {
  discs: new Map(),
  timer: null,

  show() {
    $('library').classList.remove('hidden');
    document.title = 'DVD Cabinet';
    this.refresh();
  },

  hide() {
    $('library').classList.add('hidden');
    clearTimeout(this.timer);
    this.timer = null;
  },

  async refresh() {
    clearTimeout(this.timer);
    let discs;
    try {
      const res = await fetch('/api/discs');
      discs = (await res.json()).discs;
    } catch {
      this.timer = setTimeout(() => this.refresh(), 5000);
      return;
    }
    this.discs = new Map(discs.map((d) => [d.id, d]));
    if ($('library').classList.contains('hidden')) return;
    this.render(discs);
    if (discs.some((d) => d.thumbnail_state === 'pending')) this.timer = setTimeout(() => this.refresh(), 2500);
  },

  render(discs) {
    const grid = $('grid');
    $('lib-count').textContent = discs.length ? `${discs.length} disc${discs.length === 1 ? '' : 's'}` : '';
    $('lib-empty').classList.toggle('hidden', discs.length > 0);
    const existing = new Map([...grid.children].map((el) => [el.dataset.id, el]));
    const cards = discs.map((d) => {
      const sig = JSON.stringify([d.title, d.thumbnail, d.thumbnail_state, d.runtime]);
      const old = existing.get(d.id);
      return old && old.dataset.sig === sig ? old : this.card(d, sig);
    });
    grid.replaceChildren(...cards);
  },

  card(d, sig) {
    const a = document.createElement('a');
    a.className = 'disc';
    a.href = `#/play/${d.id}`;
    a.dataset.id = d.id;
    a.dataset.sig = sig;
    a.setAttribute('aria-label', `Play ${d.title}`);

    const art = document.createElement('div');
    art.className = 'disc-art';
    if (d.thumbnail) {
      const img = new Image();
      img.src = d.thumbnail;
      img.alt = '';
      img.decoding = 'async';
      art.append(img);
    } else {
      const pending = d.thumbnail_state === 'pending';
      art.innerHTML = `<div class="placeholder">${discArt(pending)}<span>${pending ? 'Reading disc…' : ''}</span></div>`;
    }
    art.insertAdjacentHTML('beforeend', `<div class="disc-play"><span>${icon('play')}</span></div>`);

    const info = document.createElement('div');
    info.className = 'disc-info';
    const title = document.createElement('div');
    title.className = 'disc-title';
    title.textContent = d.title;
    const meta = document.createElement('div');
    meta.className = 'disc-meta';
    const bits = [];
    if (d.runtime) bits.push(fmtRuntime(d.runtime));
    bits.push(`${d.format.aspect} ${d.format.standard}`, fmtSize(d.size));
    meta.textContent = bits.join(' · ');
    info.append(title, meta);
    a.append(art, info);
    return a;
  },
};

/* ---- Streaming ---------------------------------------------------------- */

// One WebSocket per viewing. It carries control messages both ways, plus either the
// WebRTC signaling (the media then flows peer-to-peer over UDP) or, as a fallback,
// the media itself as fragmented MP4 for Media Source Extensions.
class Connection {
  constructor(video, discId, transport, onEvent) {
    this.video = video;
    this.onEvent = onEvent;
    this.wanted = transport; // 'auto' | 'webrtc' | 'mse'
    this.transport = null;
    this.media = null;
    this.closed = false;
    this.blocked = false;
    this.playPending = false;
    this.started = false;
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    this.ws = new WebSocket(`${proto}://${location.host}/api/discs/${discId}/play`);
    this.ws.binaryType = 'arraybuffer';
    this.ws.onmessage = (e) => this.message(e);
    this.ws.onclose = () => { if (!this.closed) this.onEvent({ type: 'disconnected' }); };
    this.onVideoError = () => {
      const err = this.video.error;
      if (err) this.onEvent({ type: 'error', message: `The browser couldn't decode the video (${err.message || `code ${err.code}`}).` });
    };
    this.video.addEventListener('error', this.onVideoError);
  }

  message(e) {
    if (this.closed) return; // messages already queued when we were closed
    if (typeof e.data !== 'string') {
      this.media?.binary(e.data);
      return;
    }
    const msg = JSON.parse(e.data);
    if (msg.type === 'hello') this.begin(msg);
    if (!this.media?.handle(msg)) this.onEvent(msg);
  }

  begin(hello) {
    const rtc = this.wanted !== 'mse' && 'RTCPeerConnection' in window && (hello.transports || []).includes('webrtc');
    this.transport = rtc ? 'webrtc' : 'mse';
    this.media = rtc ? new RtcMedia(this) : new MseMedia(this);
    this.send({ type: 'start', transport: this.transport, ...this.media.startParams() });
  }

  send(msg) {
    if (this.ws.readyState === WebSocket.OPEN) this.ws.send(JSON.stringify(msg));
  }

  close() {
    this.closed = true;
    this.video.removeEventListener('error', this.onVideoError);
    try { this.ws.close(); } catch { /* already closed */ }
    this.video.pause();
    this.media?.close();
    this.video.srcObject = null;
    this.video.removeAttribute('src');
    this.video.load();
  }

  play() {
    if (this.playPending || this.blocked) return;
    this.playPending = true;
    this.video.play().then(() => {
      if (!this.started) { this.started = true; this.onEvent({ type: 'playing' }); }
    }).catch((err) => {
      if (err.name === 'NotAllowedError') { this.blocked = true; this.onEvent({ type: 'autoplay-blocked' }); }
    }).finally(() => { this.playPending = false; });
  }

  unblock() {
    this.blocked = false;
    this.play();
  }
}

// WebRTC: the server makes the offer; we answer and trade ICE candidates over the socket.
class RtcMedia {
  constructor(conn) {
    this.conn = conn;
    this.video = conn.video;
    this.stream = new MediaStream();
    this.pendingIce = [];
    this.failed = false;
    this.pc = new RTCPeerConnection({ bundlePolicy: 'max-bundle' });
    this.pc.ontrack = (e) => {
      this.stream.addTrack(e.track);
      // Play frames as soon as they arrive; this is an interactive stream, not a movie file.
      try { e.receiver.jitterBufferTarget = 0; } catch { /* not supported */ }
      if (this.video.srcObject !== this.stream) this.video.srcObject = this.stream;
      conn.play();
    };
    this.pc.onicecandidate = (e) => {
      if (e.candidate) conn.send({ type: 'ice', candidate: e.candidate.candidate, sdpMLineIndex: e.candidate.sdpMLineIndex });
    };
    this.pc.onconnectionstatechange = () => { if (this.pc.connectionState === 'failed') this.fail(); };
    // Blocked UDP never "fails" quickly on its own, so give up after a while and fall back.
    this.timer = setTimeout(() => { if (this.pc.connectionState !== 'connected') this.fail(); }, 8000);
  }

  startParams() {
    const caps = window.RTCRtpReceiver?.getCapabilities?.('video');
    const codecs = caps ? [...new Set(caps.codecs.map((c) => c.mimeType.split('/')[1].toUpperCase()))] : [];
    return { video_codecs: codecs };
  }

  handle(msg) {
    if (msg.type === 'offer') {
      this.answer(msg.sdp);
      return true;
    }
    if (msg.type === 'ice') {
      const candidate = { candidate: msg.candidate, sdpMLineIndex: msg.sdpMLineIndex };
      if (this.pendingIce) this.pendingIce.push(candidate); // remote description not set yet
      else this.pc.addIceCandidate(candidate).catch(() => {});
      return true;
    }
    return false;
  }

  async answer(sdp) {
    try {
      await this.pc.setRemoteDescription({ type: 'offer', sdp });
      for (const c of this.pendingIce) this.pc.addIceCandidate(c).catch(() => {});
      this.pendingIce = null;
      const answer = await this.pc.createAnswer();
      await this.pc.setLocalDescription({ type: 'answer', sdp: stereoOpus(answer.sdp) });
      this.conn.send({ type: 'answer', sdp: this.pc.localDescription.sdp });
    } catch (err) {
      console.warn('WebRTC negotiation failed:', err);
      this.fail();
    }
  }

  fail() {
    if (this.failed || this.conn.closed) return;
    this.failed = true;
    this.conn.onEvent({ type: 'transport-failed' });
  }

  binary() {}

  close() {
    clearTimeout(this.timer);
    this.pc.close();
  }
}

// Browsers decode WebRTC Opus as mono unless the answer asks for stereo.
function stereoOpus(sdp) {
  const m = sdp.match(/a=rtpmap:(\d+) opus\/48000\/2/i);
  if (!m) return sdp;
  const fmtp = new RegExp(`a=fmtp:${m[1]} [^\\r\\n]*`);
  if (!fmtp.test(sdp)) return sdp.replace(m[0], `${m[0]}\r\na=fmtp:${m[1]} stereo=1;sprop-stereo=1`);
  return sdp.replace(fmtp, (line) => (/[ ;]stereo=/.test(line) ? line : `${line};stereo=1;sprop-stereo=1`));
}

// Fallback: fragmented MP4 over the WebSocket into Media Source Extensions.
class MseMedia {
  constructor(conn) {
    this.conn = conn;
    this.video = conn.video;
    this.queue = [];
    this.ms = null;
    this.sb = null;
    this.ticker = setInterval(() => this.tick(), 250);
  }

  startParams() { return {}; }

  handle(msg) {
    if (msg.type !== 'stream') return false;
    this.setup(msg);
    return true;
  }

  binary(data) {
    // 1 type byte (0x01 init segment, 0x02 media segment), then the MP4 bytes
    this.queue.push(new Uint8Array(data, 1));
    this.pump();
  }

  setup(msg) {
    const MS = window.MediaSource || window.ManagedMediaSource;
    const mime = `video/mp4; codecs="${msg.codecs}"`;
    if (!MS || !MS.isTypeSupported(mime)) {
      this.conn.onEvent({ type: 'error', message: `This browser can't play ${mime} through Media Source Extensions.` });
      return;
    }
    this.ms = new MS();
    if (!window.MediaSource) this.video.disableRemotePlayback = true; // required by ManagedMediaSource
    this.objectUrl = URL.createObjectURL(this.ms);
    this.video.src = this.objectUrl;
    this.ms.addEventListener('sourceopen', () => {
      if (this.conn.closed) return;
      try {
        this.sb = this.ms.addSourceBuffer(mime);
      } catch (err) {
        this.conn.onEvent({ type: 'error', message: `This browser can't play the stream (${err.name}).` });
        return;
      }
      this.sb.mode = 'segments';
      this.sb.addEventListener('updateend', () => this.pump());
      this.pump();
    }, { once: true });
  }

  pump() {
    const sb = this.sb;
    if (!sb || sb.updating || !this.queue.length || this.ms.readyState !== 'open') return;
    let data = this.queue[0];
    if (this.queue.length > 1) {
      data = new Uint8Array(this.queue.reduce((n, q) => n + q.byteLength, 0));
      let off = 0;
      for (const q of this.queue) { data.set(q, off); off += q.byteLength; }
    }
    this.queue = [];
    try {
      sb.appendBuffer(data);
    } catch (err) {
      this.conn.onEvent({ type: 'error', message: `The browser rejected the video stream (${err.name}).` });
    }
  }

  // Keep playback pinned near the live edge: the stream is interactive, so latency matters.
  tick() {
    const v = this.video;
    const sb = this.sb;
    if (!sb || !v.buffered.length) return;
    const last = v.buffered.length - 1;
    const start = v.buffered.start(last);
    const end = v.buffered.end(last);
    const lag = end - v.currentTime;
    // Browsers keep a minimum amount buffered (Firefox ~0.2 s), so aim just above that.
    if (v.currentTime < start || lag > 1.0) {
      v.currentTime = Math.max(start, end - 0.25);
    } else if (lag > 0.4) {
      v.playbackRate = 1.1; // drift back towards the edge without a visible jump
    } else if (lag < 0.25) {
      v.playbackRate = 1;
    }
    if (v.paused) this.conn.play();
    if (!sb.updating && v.currentTime - v.buffered.start(0) > 30) {
      try { sb.remove(0, v.currentTime - 10); } catch { /* busy; next tick */ }
    }
  }

  close() {
    clearInterval(this.ticker);
    if (this.objectUrl) URL.revokeObjectURL(this.objectUrl);
  }
}

/* ---- Player ------------------------------------------------------------- */

const Player = {
  client: null,
  discId: null,
  // ?transport=mse forces the WebSocket fallback; otherwise WebRTC when possible.
  transport: new URLSearchParams(location.search).get('transport') || 'auto',
  status: {},
  statusAt: 0,
  titles: [],
  dragging: false,
  idleTimer: null,
  toastTimer: null,
  pointerTimer: null,
  pendingPointer: null,
  lastPointerAt: 0,

  init() {
    document.querySelectorAll('[data-icon]').forEach((el) => {
      el.insertAdjacentHTML('afterbegin', icon(el.dataset.icon));
    });
    const video = $('video');
    $('btn-back').onclick = () => { location.hash = '#/'; };
    $('btn-msg-back').onclick = () => { location.hash = '#/'; };
    $('btn-restart').onclick = () => this.start();
    $('btn-start').onclick = () => { $('ov-start').classList.add('hidden'); this.client?.unblock(); };
    $('btn-help').onclick = () => this.toggleHelp();
    $('btn-help-close').onclick = () => this.toggleHelp(false);
    $('btn-fs').onclick = () => this.toggleFullscreen();
    $('btn-remote').onclick = () => this.toggleRemote();
    $('btn-titles').onclick = () => this.toggleTitles();

    document.querySelectorAll('#player [data-key]').forEach((b) => { b.onclick = () => this.key(b.dataset.key); });
    document.querySelectorAll('#player [data-cmd]').forEach((b) => { b.onclick = () => this.command(b.dataset.cmd); });
    document.querySelectorAll('#player [data-action]').forEach((b) => {
      b.onclick = () => ({ prev: () => this.chapter(-1), next: () => this.chapter(1), pause: () => this.togglePause() })[b.dataset.action]();
    });
    // Keep keyboard focus off buttons so arrows/Enter/Space always drive the DVD.
    $('player').addEventListener('click', (e) => { if (e.target.closest('button')) e.target.closest('button').blur(); });

    const range = $('seek-range');
    range.addEventListener('input', () => { this.dragging = true; $('t-pos').textContent = fmtTime(+range.value); });
    range.addEventListener('change', () => {
      this.dragging = false;
      this.send({ type: 'seek', position: +range.value });
      this.status.position = +range.value;
      this.statusAt = performance.now();
      range.blur();
    });

    video.addEventListener('click', (e) => this.onVideoClick(e));
    // Hide the spinner once a picture is actually showing (WebRTC "plays" before its first frame).
    const shown = () => { if (!video.paused && video.videoWidth) $('ov-loading').classList.add('hidden'); };
    video.addEventListener('playing', shown);
    video.addEventListener('resize', shown);
    $('stage').addEventListener('pointermove', (e) => {
      this.wake();
      if (e.target === video && e.pointerType === 'mouse' && this.status.menu) {
        const p = this.streamPoint(e);
        if (p) this.queuePointer(p);
      }
    });
    $('player').addEventListener('pointermove', () => this.wake());
    window.addEventListener('keydown', (e) => this.onKey(e), true);
    document.addEventListener('fullscreenchange', () => {
      const fs = document.fullscreenElement === $('player');
      $('player').classList.toggle('fs', fs);
      setIcon($('btn-fs'), fs ? 'shrink' : 'expand');
      this.wake();
    });
    setInterval(() => this.renderTime(), 250);
    this.setRemote(store.get('remote', matchMedia('(pointer: coarse)').matches));
  },

  get active() { return !$('player').classList.contains('hidden'); },

  show(discId) {
    $('player').classList.remove('hidden');
    this.discId = discId;
    const disc = Library.discs.get(discId);
    this.setTitle(disc ? disc.title : '');
    this.start();
  },

  hide() {
    if (!this.active) return;
    this.stop();
    if (document.fullscreenElement) document.exitFullscreen().catch(() => {});
    $('player').classList.add('hidden');
  },

  start() {
    this.stop();
    this.status = {};
    this.titles = [];
    for (const id of ['ov-start', 'ov-message', 'titles-pop', 'help', 'paused-badge']) $(id).classList.add('hidden');
    $('ov-loading').classList.remove('hidden');
    $('stage').classList.remove('over-button');
    this.renderStatus({ menu: true });
    $('np-status').textContent = 'Loading…';
    this.client = new Connection($('video'), this.discId, this.transport, (ev) => this.onEvent(ev));
  },

  stop() {
    if (this.client) { this.client.close(); this.client = null; }
  },

  send(msg) { this.client?.send(msg); },

  setTitle(title) {
    $('np-title').textContent = title;
    document.title = title ? `${title} · DVD Cabinet` : 'DVD Cabinet';
  },

  onEvent(ev) {
    switch (ev.type) {
      case 'hello': this.setTitle(ev.disc.title); break;
      case 'status': this.renderStatus(ev); break;
      case 'hover': $('stage').classList.toggle('over-button', ev.active); break;
      case 'highlight': if (!ev.rect) $('stage').classList.remove('over-button'); break;
      case 'titles': this.titles = ev.titles; this.renderTitles(); break;
      case 'transport-failed':
        // WebRTC couldn't connect (UDP blocked?): replay over the WebSocket for the rest of this visit.
        console.warn('WebRTC could not connect; falling back to streaming over the WebSocket');
        this.transport = 'mse';
        this.start();
        break;
      case 'autoplay-blocked': $('ov-loading').classList.add('hidden'); $('ov-start').classList.remove('hidden'); break;
      case 'ended': this.showMessage('The disc has finished', 'Thanks for watching.'); break;
      case 'error': this.showMessage('Playback problem', ev.message); break;
      case 'disconnected': this.showMessage('Connection lost', 'The connection to the server was interrupted.'); break;
      default: break;
    }
  },

  showMessage(title, text) {
    this.stop();
    $('ov-loading').classList.add('hidden');
    $('msg-title').textContent = title;
    $('msg-text').textContent = text;
    $('ov-message').classList.remove('hidden');
  },

  renderStatus(s) {
    const prev = this.status;
    this.status = s;
    this.statusAt = performance.now();
    const inMenu = !!s.menu || !s.duration;
    $('seek').classList.toggle('hidden', inMenu);
    $('menu-hint').classList.toggle('hidden', !inMenu);
    let label = 'Disc menu';
    if (!s.menu) label = s.title ? `Title ${s.title}${s.chapters ? ` · Chapter ${s.chapter} of ${s.chapters}` : ''}` : 'Playing';
    $('np-status').textContent = s.paused ? `${label} · Paused` : label;
    $('paused-badge').classList.toggle('hidden', !s.paused);
    setIcon($('btn-play'), s.paused ? 'play' : 'pause');
    if (s.duration) {
      $('seek-range').max = Math.floor(s.duration);
      $('t-dur').textContent = fmtTime(s.duration);
    }
    if (!s.menu && prev.title === s.title && prev.chapter != null && s.chapter !== prev.chapter) this.toast(`Chapter ${s.chapter}`);
    if (s.paused) $('player').classList.remove('idle'); // keep the bars up while paused
    else if (prev.paused) this.wake(); // resumed: let them hide again after a moment
    this.renderTime();
    if (prev.title !== s.title || prev.menu !== s.menu) this.renderTitles();
  },

  position() {
    const s = this.status;
    if (s.position == null) return 0;
    const elapsed = s.paused ? 0 : (performance.now() - this.statusAt) / 1000;
    return Math.min(s.duration || Infinity, s.position + elapsed);
  },

  renderTime() {
    if (!this.active || this.dragging || !this.status.duration) return;
    const pos = this.position();
    $('t-pos').textContent = fmtTime(pos);
    $('seek-range').value = pos;
  },

  renderTitles() {
    const list = $('titles-list');
    const longest = Math.max(0, ...this.titles.map((t) => t.duration || 0));
    list.replaceChildren(...this.titles.map((t) => {
      const li = document.createElement('li');
      const b = document.createElement('button');
      b.className = t.title === this.status.title && !this.status.menu ? 'current' : '';
      const name = document.createElement('span');
      name.textContent = `Title ${t.title}`;
      if (t.duration >= 600 && t.duration >= 0.75 * longest) name.className = 'feature';
      const dur = document.createElement('span');
      dur.className = 'dur';
      dur.textContent = t.duration ? fmtTime(t.duration) : '';
      b.append(name, dur);
      b.onclick = () => { this.send({ type: 'title', title: t.title }); this.toggleTitles(false); };
      li.append(b);
      return li;
    }));
  },

  // -- remote control ----------------------------------------------------------

  key(k) {
    if ((k === 'left' || k === 'right') && !this.status.menu && this.status.duration) this.toast(k === 'left' ? '−10 s' : '+10 s');
    this.send({ type: 'key', key: k });
  },

  command(c) { this.send({ type: 'command', command: c }); },

  chapter(delta) { this.send({ type: 'chapter', delta }); },

  togglePause() {
    this.send({ type: 'toggle_pause' });
    this.status.paused = !this.status.paused; // optimistic; the next status confirms
    this.renderStatus({ ...this.status });
  },

  onKey(e) {
    if (!this.active || e.ctrlKey || e.metaKey || e.altKey) return;
    if (!$('help').classList.contains('hidden')) {
      if (['Escape', 'Enter', '?', ' '].includes(e.key)) { e.preventDefault(); this.toggleHelp(false); }
      return;
    }
    const actions = {
      ArrowUp: () => this.key('up'),
      ArrowDown: () => this.key('down'),
      ArrowLeft: () => this.key('left'),
      ArrowRight: () => this.key('right'),
      Enter: () => this.key('enter'),
      ' ': () => this.togglePause(),
      k: () => this.togglePause(),
      m: () => this.command('menu'),
      t: () => this.command('title_menu'),
      ',': () => this.chapter(-1),
      '<': () => this.chapter(-1),
      PageUp: () => this.chapter(-1),
      '.': () => this.chapter(1),
      '>': () => this.chapter(1),
      PageDown: () => this.chapter(1),
      f: () => this.toggleFullscreen(),
      r: () => this.toggleRemote(),
      '?': () => this.toggleHelp(),
      Escape: () => { this.toggleTitles(false); },
    };
    const action = actions[e.key.length === 1 ? e.key.toLowerCase() : e.key] || actions[e.key];
    if (!action) return;
    if (e.key === 'Escape' && document.fullscreenElement) return; // let the browser leave fullscreen
    e.preventDefault();
    e.stopPropagation();
    action();
    this.wake();
  },

  // -- mouse: point at menu buttons like on a computer DVD player --------------

  streamPoint(e) {
    const v = $('video');
    const r = v.getBoundingClientRect();
    if (!v.videoWidth || !v.videoHeight) return null;
    const scale = Math.min(r.width / v.videoWidth, r.height / v.videoHeight);
    const w = v.videoWidth * scale;
    const h = v.videoHeight * scale;
    // object-fit: contain, placed by object-position (centred, or top-aligned on portrait phones)
    const [ax, ay] = getComputedStyle(v).objectPosition.split(' ').map((p) => parseFloat(p) / 100);
    const x = (e.clientX - (r.left + (r.width - w) * (isNaN(ax) ? 0.5 : ax))) / w;
    const y = (e.clientY - (r.top + (r.height - h) * (isNaN(ay) ? 0.5 : ay))) / h;
    return x >= 0 && x <= 1 && y >= 0 && y <= 1 ? { x, y } : null;
  },

  queuePointer(p) {
    this.pendingPointer = p;
    if (this.pointerTimer) return;
    const wait = Math.max(0, 40 - (performance.now() - this.lastPointerAt));
    this.pointerTimer = setTimeout(() => {
      this.pointerTimer = null;
      const q = this.pendingPointer;
      this.pendingPointer = null;
      if (!q) return;
      this.lastPointerAt = performance.now();
      this.send({ type: 'pointer', x: q.x, y: q.y });
    }, wait);
  },

  onVideoClick(e) {
    if (this.status.menu) {
      const p = this.streamPoint(e);
      if (p) this.send({ type: 'pointer', x: p.x, y: p.y, click: true });
    } else {
      this.togglePause();
    }
  },

  // -- chrome ------------------------------------------------------------------

  toggleFullscreen() {
    if (document.fullscreenElement) document.exitFullscreen().catch(() => {});
    else $('player').requestFullscreen?.().catch(() => {});
  },

  setRemote(on) {
    $('remote').classList.toggle('hidden', !on);
    $('btn-remote').classList.toggle('on', on);
    if (on) $('titles-pop').classList.add('hidden');
  },

  toggleRemote() {
    const on = $('remote').classList.contains('hidden');
    this.setRemote(on);
    store.set('remote', on);
  },

  toggleTitles(force) {
    const pop = $('titles-pop');
    const show = force ?? pop.classList.contains('hidden');
    pop.classList.toggle('hidden', !show);
    if (show) $('remote').classList.add('hidden');
    else this.setRemote(store.get('remote', matchMedia('(pointer: coarse)').matches));
  },

  toggleHelp(force) {
    const help = $('help');
    help.classList.toggle('hidden', !(force ?? help.classList.contains('hidden')));
  },

  toast(text) {
    const t = $('toast');
    t.textContent = text;
    t.classList.add('show');
    clearTimeout(this.toastTimer);
    this.toastTimer = setTimeout(() => t.classList.remove('show'), 1200);
  },

  // In fullscreen the bars float over the picture; tuck them away when idle.
  wake() {
    const p = $('player');
    p.classList.remove('idle');
    clearTimeout(this.idleTimer);
    this.idleTimer = setTimeout(() => {
      const busy = !$('titles-pop').classList.contains('hidden') || !$('help').classList.contains('hidden');
      if (!this.status.paused && !busy) p.classList.add('idle');
    }, 2500);
  },
};

/* ---- Router ------------------------------------------------------------- */

function route() {
  const m = location.hash.match(/^#\/play\/([0-9a-f]+)$/);
  if (m) {
    Library.hide();
    Player.show(m[1]);
  } else {
    Player.hide();
    Library.show();
  }
}

Player.init();
window.addEventListener('hashchange', route);
route();
