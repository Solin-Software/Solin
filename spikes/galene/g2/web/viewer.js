// G2 viewer: plays the Galene program and reports decode/playback stats to /log.
'use strict';

const params = new URLSearchParams(location.search);
const GROUP = params.get('group') || 'g2';
const LABEL = params.get('label') || 'device';
const WS = params.get('ws') || 'ws://127.0.0.1:8443/ws';
const statusEl = document.getElementById('status');
const video = document.getElementById('video');
let pc = null, prev = null, reports = 0, started = 0;

function post(obj) {
  fetch('log', { method: 'POST', body: JSON.stringify(obj) }).catch(() => {});
}

async function sample() {
  if (!pc) return;
  const stats = await pc.getStats();
  const by = new Map([...stats.values()].map(s => [s.id, s]));
  let v = null, a = null;
  for (const s of stats.values()) {
    if (s.type === 'inbound-rtp' && s.kind === 'video') v = s;
    if (s.type === 'inbound-rtp' && s.kind === 'audio') a = s;
  }
  const now = performance.now();
  const r = {
    label: LABEL, ua: navigator.userAgent, t: Math.round((now - started) / 1000),
    visibility: document.visibilityState, paused: video.paused, muted: video.muted,
  };
  if (v) {
    const dt = prev ? (now - prev.now) / 1000 : 0;
    Object.assign(r, {
      vCodec: by.get(v.codecId)?.mimeType, vFmtp: by.get(v.codecId)?.sdpFmtpLine,
      decoder: v.decoderImplementation, powerEfficient: v.powerEfficientDecoder,
      size: `${v.frameWidth}x${v.frameHeight}`,
      fps: prev && dt ? +((v.framesDecoded - prev.v.framesDecoded) / dt).toFixed(1) : v.framesPerSecond,
      framesDecoded: v.framesDecoded, framesDropped: v.framesDropped, freezes: v.freezeCount,
      freezeSec: v.totalFreezesDuration, vLost: v.packetsLost,
      vJitterBufMs: v.jitterBufferEmittedCount ? Math.round(1000 * v.jitterBufferDelay / v.jitterBufferEmittedCount) : null,
    });
  }
  if (a) {
    Object.assign(r, {
      aCodec: by.get(a.codecId)?.mimeType,
      audioEnergy: prev?.a ? +(a.totalAudioEnergy - prev.a.totalAudioEnergy).toFixed(4) : 0,
      concealedPct: a.totalSamplesReceived ? +(100 * a.concealedSamples / a.totalSamplesReceived).toFixed(2) : null,
      aLost: a.packetsLost,
    });
  }
  // Selected ICE pair: which path the media actually uses.
  let pair = null;
  for (const st of stats.values()) {
    if (st.type === 'transport' && st.selectedCandidatePairId) pair = by.get(st.selectedCandidatePairId);
  }
  if (!pair) for (const st of stats.values()) if (st.type === 'candidate-pair' && st.nominated && st.state === 'succeeded') pair = st;
  if (pair) {
    const desc = c => c && `${c.candidateType}${c.relayProtocol ? '/' + c.relayProtocol : ''} ${c.protocol} ${c.address ?? c.ip}:${c.port}`;
    r.iceLocal = desc(by.get(pair.localCandidateId));
    r.iceRemote = desc(by.get(pair.remoteCandidateId));
    r.rttMs = pair.currentRoundTripTime != null ? Math.round(pair.currentRoundTripTime * 1000) : null;
  }
  prev = { now, v, a };
  reports++;
  post(r);
  const videoOk = r.fps >= 20, audioOk = r.audioEnergy > 0;
  statusEl.innerHTML =
    `<span class="${videoOk ? 'ok' : 'bad'}">video ${videoOk ? 'OK' : 'NOT PLAYING'}</span>  ` +
    `<span class="${audioOk ? 'ok' : 'bad'}">audio ${audioOk ? 'OK' : 'silent?'}</span>\n` +
    `${r.vCodec} ${r.size} ${r.fps} fps, decoder ${r.decoder}${r.powerEfficient ? ' (hardware)' : ''}\n` +
    `freezes ${r.freezes}, dropped ${r.framesDropped}, lost ${r.vLost}/${r.aLost}, audio concealed ${r.concealedPct}%\n` +
    `path: ${r.iceLocal ?? '?'} -> ${r.iceRemote ?? '?'}, rtt ${r.rttMs ?? '?'} ms\n` +
    `reports sent: ${reports}, reconnects: ${reconnects}`;
}

// Connection state for automatic reconnection.
let sc = null, closedAt = null, attempt = 0, reconnects = 0, retryTimer = null;

function connect() {
  retryTimer = null;
  sc = new ServerConnection();
  if (params.has('relay')) {
    // Force TURN: only relay candidates, as when no direct path exists.
    const base = sc.getRTCConfiguration.bind(sc);
    const tcpOnly = params.get('relay') === 'tcp';   // emulate a UDP-blocked network
    sc.getRTCConfiguration = () => {
      const conf = { ...base(), iceTransportPolicy: 'relay' };
      if (tcpOnly) {
        conf.iceServers = (conf.iceServers || []).map(s => ({
          ...s, urls: [].concat(s.urls).filter(u => u.includes('transport=tcp')),
        })).filter(s => s.urls.length);
      }
      return conf;
    };
  }
  const me = sc;
  sc.onconnected = function () { this.join(GROUP, `${LABEL}-${Date.now() % 100000}`, 'x'); };
  sc.onjoined = function (kind, group, perms, status, data, error, message) {
    if (kind === 'join') this.request({ '': ['audio', 'video'] });
    if (kind === 'fail') { statusEl.textContent = `join failed: ${message}`; post({ label: LABEL, error: message }); }
  };
  sc.ondownstream = function (stream) {
    pc = stream.pc;
    prev = null;   // new connection: counters restart
    stream.ondowntrack = (track, transceiver, ms) => {
      if (video.srcObject !== ms) { video.srcObject = ms; video.play().catch(err => post({ label: LABEL, playError: String(err) })); }
      if (closedAt !== null) {
        post({ label: LABEL, event: 'reconnected', downtime_ms: Math.round(performance.now() - closedAt), attempt, visibility: document.visibilityState });
        closedAt = null; attempt = 0; reconnects++;
      }
    };
  };
  sc.onclose = function (code, reason) {
    if (me !== sc) return;   // a stale connection
    pc = null;
    if (closedAt === null) closedAt = performance.now();
    post({ label: LABEL, event: 'closed', code, reason, visibility: document.visibilityState, t: Math.round((performance.now() - started) / 1000) });
    scheduleReconnect();
  };
  try {
    sc.connect(WS);
  } catch (err) {
    post({ label: LABEL, error: String(err) });
    scheduleReconnect();
  }
}

function scheduleReconnect(now = false) {
  if (retryTimer) return;
  const delay = now ? 0 : Math.min(8000, 500 * 2 ** attempt);
  attempt++;
  statusEl.textContent = `connection lost; reconnecting in ${delay / 1000} s (attempt ${attempt})...`;
  retryTimer = setTimeout(connect, delay);
}

async function start() {
  document.getElementById('start')?.remove();
  started = performance.now();
  statusEl.textContent = 'connecting...';
  // Tell the OS this page is playing media, which may keep it running in the background.
  if ('mediaSession' in navigator) {
    navigator.mediaSession.metadata = new MediaMetadata({ title: 'Meeting', artist: 'Solin' });
    navigator.mediaSession.playbackState = 'playing';
  }
  document.addEventListener('visibilitychange', () => {
    post({ label: LABEL, event: 'visibility', state: document.visibilityState, connected: !!pc, t: Math.round((performance.now() - started) / 1000) });
    if (document.visibilityState === 'visible' && closedAt !== null) {
      clearTimeout(retryTimer); retryTimer = null;
      scheduleReconnect(true);
    }
  });
  setInterval(sample, 5000);
  connect();
}

document.getElementById('start').onclick = start;
if (params.has('auto')) {
  video.muted = true;   // autoplay without a gesture; audio stats still count
  start();
}
