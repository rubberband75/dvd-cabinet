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

function ago(ts) {
  if (!ts) return 'never';
  const s = Math.max(0, Date.now() / 1000 - ts);
  if (s < 60) return 'just now';
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  const d = Math.round(s / 86400);
  return d === 1 ? 'yesterday' : `${d} days ago`;
}

function el(tag, props = {}, ...children) {
  const node = Object.assign(document.createElement(tag), props);
  node.append(...children.filter((c) => c != null));
  return node;
}

class ApiError extends Error {
  constructor(status, message) { super(message); this.status = status; }
}

// JSON in, JSON out. Being signed out (401) sends you to the sign-in screen.
async function api(method, url, body) {
  const res = await fetch(url, {
    method,
    headers: body === undefined ? {} : { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  let data = {};
  try { data = await res.json(); } catch { /* empty body */ }
  if (res.status === 401 && !url.startsWith('/api/auth/')) {
    Auth.user = null;
    route();
  }
  if (!res.ok) throw new ApiError(res.status, data.error || `Request failed (${res.status})`);
  return data;
}

// The single <dialog>, filled in per use. onOk may throw to show an error and stay open.
function openDialog({ title, body, ok = 'OK', danger = false, onOk }) {
  const dlg = $('dlg');
  $('dlg-title').textContent = title;
  $('dlg-body').replaceChildren(...[].concat(body || []));
  $('dlg-error').textContent = '';
  const okBtn = $('dlg-ok');
  okBtn.textContent = ok;
  okBtn.classList.toggle('danger', danger);
  okBtn.disabled = false;
  return new Promise((resolve) => {
    const form = $('dlg-form');
    const finish = (value) => {
      form.onsubmit = null;
      dlg.onclose = null;
      if (dlg.open) dlg.close();
      resolve(value);
    };
    form.onsubmit = async (e) => {
      e.preventDefault();
      if (e.submitter && e.submitter.value === 'cancel') return finish(null);
      okBtn.disabled = true;
      try {
        finish(onOk ? await onOk() : true);
      } catch (err) {
        $('dlg-error').textContent = err.message;
        okBtn.disabled = false;
      }
    };
    dlg.onclose = () => finish(null);
    dlg.showModal();
    dlg.querySelector('input')?.focus();
  });
}

/* ---- Sign in / claim ------------------------------------------------------ */

const Auth = {
  user: null,
  checked: false,
  setupRequired: false,
  codeRequired: false,

  async check() {
    const state = await (await fetch('/api/auth/state')).json();
    this.user = state.user;
    this.setupRequired = state.setup_required;
    this.codeRequired = state.setup_code_required;
    this.checked = true;
  },

  init() {
    $('auth-form').addEventListener('submit', (e) => { e.preventDefault(); this.submit(); });
    $('btn-user').onclick = (e) => { e.stopPropagation(); this.toggleMenu(); };
    document.addEventListener('click', () => this.toggleMenu(false));
    $('menu-logout').onclick = () => this.logout();
    $('menu-password').onclick = () => this.changePassword();
  },

  show() {
    const setup = this.setupRequired;
    $('auth').classList.remove('hidden');
    document.title = setup ? 'Set up DVD Cabinet' : 'Sign in · DVD Cabinet';
    $('auth-title').textContent = setup ? 'Claim this server' : 'Sign in';
    $('auth-text').textContent = setup
      ? 'Welcome to DVD Cabinet. Create the admin account; you can add more people afterwards.'
      : 'Sign in to watch your discs.';
    $('auth-pass').autocomplete = setup ? 'new-password' : 'current-password';
    $('auth-confirm-row').classList.toggle('hidden', !setup);
    $('auth-code-row').classList.toggle('hidden', !(setup && this.codeRequired));
    $('auth-submit').textContent = setup ? 'Create admin account' : 'Sign in';
    $('auth-error').textContent = '';
    $('auth-user').focus();
  },

  hide() { $('auth').classList.add('hidden'); },

  async submit() {
    const setup = this.setupRequired;
    const username = $('auth-user').value.trim();
    const password = $('auth-pass').value;
    const error = $('auth-error');
    if (!username || !password) { error.textContent = 'Enter a username and password.'; return; }
    if (setup && password !== $('auth-confirm').value) { error.textContent = "The passwords don't match."; return; }
    $('auth-submit').disabled = true;
    try {
      const body = { username, password };
      if (setup) body.setup_code = $('auth-code').value;
      const res = await api('POST', setup ? '/api/auth/setup' : '/api/auth/login', body);
      this.user = res.user;
      this.setupRequired = false;
      $('auth-form').reset();
      if (setup) location.hash = '#/admin';
      route();
    } catch (err) {
      error.textContent = err.message;
      if (err.status === 409) { await this.check(); this.show(); } // someone else claimed it first
    } finally {
      $('auth-submit').disabled = false;
    }
  },

  renderMenu() {
    const u = this.user;
    if (!u) return;
    $('user-name').textContent = u.username;
    $('user-avatar').textContent = u.username.slice(0, 1);
    $('menu-admin').classList.toggle('hidden', !u.is_admin);
  },

  toggleMenu(force) {
    const pop = $('user-pop');
    const show = force ?? pop.classList.contains('hidden');
    pop.classList.toggle('hidden', !show);
    $('btn-user').setAttribute('aria-expanded', String(show));
  },

  async logout() {
    await api('POST', '/api/auth/logout').catch(() => {});
    this.user = null;
    location.hash = '#/';
    route();
  },

  changePassword() {
    const current = el('input', { type: 'password', autocomplete: 'current-password', required: true });
    const next = el('input', { type: 'password', autocomplete: 'new-password', required: true });
    const again = el('input', { type: 'password', autocomplete: 'new-password', required: true });
    return openDialog({
      title: 'Change your password',
      body: [el('label', {}, 'Current password', current), el('label', {}, 'New password', next),
        el('label', {}, 'New password again', again)],
      ok: 'Change password',
      onOk: async () => {
        if (next.value !== again.value) throw new Error("The new passwords don't match.");
        await api('POST', '/api/auth/password', { current: current.value, new: next.value });
        toast('Password changed. Other devices have been signed out.');
      },
    });
  },
};

function toast(text) {
  const t = el('div', { className: 'toast show page-toast', textContent: text });
  document.body.append(t);
  setTimeout(() => t.remove(), 3000);
}

/* ---- Library ------------------------------------------------------------ */

const Library = {
  discs: new Map(),
  timer: null,

  show() {
    $('library').classList.remove('hidden');
    document.title = 'DVD Cabinet';
    Auth.renderMenu();
    this.refresh();
  },

  hide() {
    $('library').classList.add('hidden');
    clearTimeout(this.timer);
    this.timer = null;
  },

  async refresh() {
    clearTimeout(this.timer);
    let discs, scanning;
    try {
      ({ discs, scanning } = await api('GET', '/api/discs'));
    } catch (err) {
      if (err.status !== 401) this.timer = setTimeout(() => this.refresh(), 5000);
      return;
    }
    this.discs = new Map(discs.map((d) => [d.id, d]));
    if ($('library').classList.contains('hidden')) return;
    this.render(discs);
    if (scanning || discs.some((d) => d.thumbnail_state === 'pending')) this.timer = setTimeout(() => this.refresh(), 2500);
  },

  render(discs) {
    const grid = $('grid');
    $('lib-count').textContent = discs.length ? `${discs.length} disc${discs.length === 1 ? '' : 's'}` : '';
    $('lib-empty').classList.toggle('hidden', discs.length > 0);
    $('lib-empty-admin').classList.toggle('hidden', !Auth.user?.is_admin);
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
      case 'disconnected':
        this.stop();
        Auth.check().then(() => {
          if (!Auth.user) route(); // signed out (or the account was removed): back to sign-in
          else this.showMessage('Connection lost', 'The connection to the server was interrupted.');
        }, () => this.showMessage('Connection lost', 'The server is not reachable.'));
        break;
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

/* ---- Server settings (admins) ------------------------------------------- */

const FOLDER_ICON = '<svg class="i folder-icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg>';

const Admin = {
  timer: null,

  init() {
    $('folder-form').addEventListener('submit', (e) => { e.preventDefault(); this.addFolder($('folder-path').value); });
    $('btn-browse').onclick = () => this.browse();
    $('btn-scan').onclick = async () => this.renderLibrary(await api('POST', '/api/admin/library/scan'));
    $('user-form').addEventListener('submit', (e) => { e.preventDefault(); this.addUser(); });
  },

  show(tab) {
    $('admin').classList.remove('hidden');
    document.title = 'Server settings · DVD Cabinet';
    $('tab-library').classList.toggle('current', tab === 'library');
    $('tab-users').classList.toggle('current', tab === 'users');
    $('panel-library').classList.toggle('hidden', tab !== 'library');
    $('panel-users').classList.toggle('hidden', tab !== 'users');
    if (tab === 'users') this.loadUsers(); else this.loadLibrary();
  },

  hide() {
    $('admin').classList.add('hidden');
    clearTimeout(this.timer);
  },

  // -- library ------------------------------------------------------------------

  async loadLibrary() {
    clearTimeout(this.timer);
    try {
      this.renderLibrary(await api('GET', '/api/admin/library'));
    } catch (err) {
      $('folder-error').textContent = err.message;
    }
  },

  renderLibrary(st) {
    clearTimeout(this.timer);
    $('folder-list').replaceChildren(...st.folders.map((f) => {
      const remove = el('button', { className: 'pill-btn', textContent: 'Remove' });
      remove.onclick = () => this.removeFolder(f);
      const sub = f.exists ? `${f.discs} disc${f.discs === 1 ? '' : 's'}` : "Missing: the server can't see this folder right now";
      return el('li', { className: 'row' },
        el('div', { className: 'row-main' }, el('div', { className: 'row-title', textContent: f.path }),
          el('div', { className: `row-sub${f.exists ? '' : ' bad'}`, textContent: sub })),
        remove);
    }));
    let status;
    if (st.scanning) status = 'Scanning…';
    else if (!st.folders.length) status = 'Add a folder above, then its discs show up in the library.';
    else if (st.last_scan) status = `Last scan ${ago(st.last_scan.finished)}: ${st.discs} disc${st.discs === 1 ? '' : 's'} in the library.`;
    else status = 'Not scanned yet.';
    if (st.thumbnails_pending) status += ` Making menu thumbnails for ${st.thumbnails_pending} disc${st.thumbnails_pending === 1 ? '' : 's'}…`;
    $('scan-status').textContent = status;
    $('btn-scan').disabled = st.scanning || !st.folders.length;
    $('btn-scan').textContent = st.scanning ? 'Scanning…' : 'Scan for new discs';
    if ((st.scanning || st.thumbnails_pending) && !$('admin').classList.contains('hidden')) {
      this.timer = setTimeout(() => this.loadLibrary(), 1500);
    }
  },

  async addFolder(path) {
    $('folder-error').textContent = '';
    try {
      this.renderLibrary(await api('POST', '/api/admin/library/folders', { path: path.trim() }));
      $('folder-path').value = '';
    } catch (err) {
      $('folder-error').textContent = err.message;
    }
  },

  async removeFolder(folder) {
    const ok = await openDialog({
      title: 'Remove this folder?',
      body: el('p', {}, 'Its discs leave the library. Nothing is deleted from ', el('code', { textContent: folder.path }), '.'),
      ok: 'Remove',
      danger: true,
    });
    if (ok) this.renderLibrary(await api('DELETE', `/api/admin/library/folders/${folder.id}`));
  },

  // A small file-manager view of the server's folders.
  browse() {
    const pathLine = el('div', { className: 'browser-path' });
    const list = el('ul', { className: 'browser-list' });
    const note = el('p', { className: 'muted' });
    let current = null;
    const load = async (path) => {
      $('dlg-error').textContent = '';
      let res;
      try {
        res = await api('GET', `/api/admin/browse?path=${encodeURIComponent(path)}`);
      } catch (err) {
        $('dlg-error').textContent = err.message;
        return;
      }
      current = res.path;
      pathLine.textContent = res.path;
      const items = res.dirs.map((d) => {
        const b = el('button', { type: 'button' });
        b.innerHTML = FOLDER_ICON;
        b.append(el('span', { textContent: d.name }));
        b.onclick = () => load(d.path);
        return el('li', {}, b);
      });
      if (res.parent) {
        const up = el('button', { type: 'button' });
        up.innerHTML = `${icon('up')}`;
        up.append(el('span', { textContent: 'Up a level' }));
        up.onclick = () => load(res.parent);
        items.unshift(el('li', {}, up));
      }
      if (!res.dirs.length) items.push(el('li', { className: 'empty', textContent: 'No folders in here.' }));
      list.replaceChildren(...items);
      note.textContent = res.isos ? `${res.isos} disc image${res.isos === 1 ? '' : 's'} directly in this folder.` : '';
    };
    const start = $('folder-path').value.trim() || '/mnt';
    load(start.startsWith('/') ? start : '/').then(() => { if (current === null) load('/'); });
    return openDialog({
      title: 'Choose a folder',
      body: [pathLine, list, note],
      ok: 'Use this folder',
      onOk: async () => {
        if (!current) throw new Error('Pick a folder first.');
        this.renderLibrary(await api('POST', '/api/admin/library/folders', { path: current }));
      },
    });
  },

  // -- users --------------------------------------------------------------------

  async loadUsers() {
    try {
      const { users } = await api('GET', '/api/admin/users');
      this.renderUsers(users);
    } catch (err) {
      $('user-error').textContent = err.message;
    }
  },

  renderUsers(users) {
    const me = Auth.user;
    $('user-list').replaceChildren(...users.map((u) => {
      const title = el('div', { className: 'row-title', textContent: u.username });
      if (u.is_admin) title.append(el('span', { className: 'badge', textContent: 'Admin' }));
      if (u.id === me.id) title.append(el('span', { className: 'badge plain', textContent: 'You' }));
      const actions = [];
      const reset = el('button', { className: 'pill-btn', textContent: 'Set password' });
      reset.onclick = () => this.resetPassword(u);
      actions.push(reset);
      if (u.id !== me.id) {
        const role = el('button', { className: 'pill-btn', textContent: u.is_admin ? 'Remove admin' : 'Make admin' });
        role.onclick = () => this.update(u, { is_admin: !u.is_admin });
        const del = el('button', { className: 'pill-btn danger', textContent: 'Delete' });
        del.onclick = () => this.deleteUser(u);
        actions.push(role, del);
      }
      return el('li', { className: 'row' },
        el('div', { className: 'row-main' }, title,
          el('div', { className: 'row-sub', textContent: u.last_login_at ? `Last signed in ${ago(u.last_login_at)}` : 'Never signed in' })),
        ...actions);
    }));
  },

  async addUser() {
    $('user-error').textContent = '';
    try {
      await api('POST', '/api/admin/users', {
        username: $('new-user').value, password: $('new-pass').value, is_admin: $('new-admin').checked,
      });
      $('user-form').reset();
      this.loadUsers();
    } catch (err) {
      $('user-error').textContent = err.message;
    }
  },

  async update(user, change) {
    try {
      await api('PATCH', `/api/admin/users/${user.id}`, change);
    } catch (err) {
      toast(err.message);
    }
    this.loadUsers();
  },

  resetPassword(user) {
    const pass = el('input', { type: 'password', autocomplete: 'new-password', required: true });
    return openDialog({
      title: `New password for ${user.username}`,
      body: [el('label', {}, 'Password', pass),
        el('p', { className: 'muted', textContent: "They'll be signed out everywhere and use this from now on." })],
      ok: 'Set password',
      onOk: () => api('PATCH', `/api/admin/users/${user.id}`, { password: pass.value }),
    });
  },

  async deleteUser(user) {
    const ok = await openDialog({
      title: `Delete ${user.username}?`,
      body: el('p', { textContent: "They're signed out right away and can't sign in again." }),
      ok: 'Delete',
      danger: true,
      onOk: () => api('DELETE', `/api/admin/users/${user.id}`),
    });
    if (ok) this.loadUsers();
  },
};

/* ---- Router ------------------------------------------------------------- */

const VIEWS = { auth: Auth, library: Library, player: Player, admin: Admin };

function showOnly(name, ...args) {
  for (const [key, view] of Object.entries(VIEWS)) if (key !== name) view.hide();
  VIEWS[name].show(...args);
}

async function route() {
  if (!Auth.checked || !Auth.user) {
    try {
      await Auth.check();
    } catch {
      setTimeout(route, 3000); // server not reachable yet
      return;
    }
  }
  if (Auth.setupRequired || !Auth.user) return showOnly('auth');
  const hash = location.hash;
  const play = hash.match(/^#\/play\/([0-9a-f]+)$/);
  if (play) return showOnly('player', play[1]);
  if (hash.startsWith('#/admin')) {
    if (!Auth.user.is_admin) { location.hash = '#/'; return; }
    return showOnly('admin', hash === '#/admin/users' ? 'users' : 'library');
  }
  showOnly('library');
}

Player.init();
Auth.init();
Admin.init();
window.addEventListener('hashchange', route);
route();
