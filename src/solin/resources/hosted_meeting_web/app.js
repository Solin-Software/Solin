// Solin hosted meeting — attendee app.
//
// Flow: name + meeting password + how many watch here → POST <base>/api/join → lobby
// (events WebSocket) → admitted with a short-lived Galene token → Galene through the
// hall's <base>/ws proxy. On any drop the page asks the hall for a fresh token and
// reconnects with backoff (immediately when the page becomes visible again); while the
// hall is unreachable it only says "reconnecting" — the meeting has ended only when the
// hall says so. The microphone opens only while the operator has granted `present`
// (called on, or microphones unlocked before/after the meeting); part-holders may also
// send their camera.
'use strict';

// The page is served at /<code>; every request stays under it.
const BASE = document.body.dataset.base;
const SLUG = BASE.split('/').filter(Boolean).pop();
const API = `${BASE}/api`;
const STORE = `solin-meeting:${SLUG}`;
const PROFILE = 'solin-meeting:profile';  // name and count, remembered for the next meeting

const STRINGS = {
  en: {
    yourName: 'Your name', join: 'Join', waiting: 'Waiting to be admitted…',
    waitingHint: 'The attendant will let you in shortly. Keep this page open.',
    tapForSound: 'Tap for sound', youAreLive: 'You can speak now', done: 'Done',
    raiseHand: 'Raise hand', lowerHand: 'Lower hand', micOn: 'Microphone on', micOff: 'Microphone off',
    cameraOn: 'Camera on', cameraOff: 'Camera off', options: 'Options',
    audioOnly: 'Audio only (saves data)', keepAwake: 'Keep the screen on', rejoin: 'Join again',
    reconnecting: 'Connection lost — reconnecting…', ended: 'The meeting has ended. Thank you!',
    removed: 'You were removed from the meeting.', refused: 'You cannot join this meeting.',
    noMic: 'Your microphone could not be opened. Check the browser permission.',
    micReady: 'Microphone open', connecting: 'Connecting…',
    password: 'Meeting password', howMany: 'How many people are watching on this device, including you?',
    more: 'More…', alreadyCounted: 'Already counted', needName: 'Type your name.',
    needPassword: 'Type the meeting password.', needCount: 'Choose how many people are watching.',
    wrongPassword: 'Wrong password. Check it and try again.',
    tooMany: 'Too many attempts. Wait a few minutes and try again.',
    invalidLink: 'This link is no longer valid. Ask your congregation for the current link.',
    startsIn: 'The meeting starts in {m} min', startsAt: 'The meeting starts at {t}', startsNow: 'The meeting is starting',
  },
  pt: {
    yourName: 'Seu nome', join: 'Entrar', waiting: 'Aguardando a admissão…',
    waitingHint: 'O indicador vai admitir você em breve. Mantenha esta página aberta.',
    tapForSound: 'Toque para ouvir', youAreLive: 'Você pode falar agora', done: 'Terminei',
    raiseHand: 'Levantar a mão', lowerHand: 'Abaixar a mão', micOn: 'Ligar microfone', micOff: 'Desligar microfone',
    cameraOn: 'Ligar câmera', cameraOff: 'Desligar câmera', options: 'Opções',
    audioOnly: 'Somente áudio (economiza dados)', keepAwake: 'Manter a tela ligada', rejoin: 'Entrar de novo',
    reconnecting: 'Conexão perdida — reconectando…', ended: 'A reunião terminou. Obrigado!',
    removed: 'Você foi removido da reunião.', refused: 'Você não pode entrar nesta reunião.',
    noMic: 'Não foi possível abrir o microfone. Verifique a permissão do navegador.',
    micReady: 'Microfone aberto', connecting: 'Conectando…',
    password: 'Senha da reunião', howMany: 'Quantas pessoas estão assistindo neste aparelho, incluindo você?',
    more: 'Mais…', alreadyCounted: 'Já fui contado', needName: 'Digite seu nome.',
    needPassword: 'Digite a senha da reunião.', needCount: 'Escolha quantas pessoas estão assistindo.',
    wrongPassword: 'Senha incorreta. Confira e tente de novo.',
    tooMany: 'Tentativas demais. Aguarde alguns minutos e tente de novo.',
    invalidLink: 'Este link não é mais válido. Peça à sua congregação o link atual.',
    startsIn: 'A reunião começa em {m} min', startsAt: 'A reunião começa às {t}', startsNow: 'A reunião está começando',
  },
  es: {
    yourName: 'Su nombre', join: 'Entrar', waiting: 'Esperando a ser admitido…',
    waitingHint: 'El acomodador le dejará entrar en breve. Mantenga esta página abierta.',
    tapForSound: 'Toque para oír', youAreLive: 'Puede hablar ahora', done: 'Terminé',
    raiseHand: 'Levantar la mano', lowerHand: 'Bajar la mano', micOn: 'Activar micrófono', micOff: 'Desactivar micrófono',
    cameraOn: 'Activar cámara', cameraOff: 'Desactivar cámara', options: 'Opciones',
    audioOnly: 'Solo audio (ahorra datos)', keepAwake: 'Mantener la pantalla encendida', rejoin: 'Entrar de nuevo',
    reconnecting: 'Conexión perdida — reconectando…', ended: 'La reunión ha terminado. ¡Gracias!',
    removed: 'Fue retirado de la reunión.', refused: 'No puede entrar en esta reunión.',
    noMic: 'No se pudo abrir el micrófono. Revise el permiso del navegador.',
    micReady: 'Micrófono abierto', connecting: 'Conectando…',
    password: 'Contraseña de la reunión', howMany: '¿Cuántas personas están viendo en este dispositivo, incluido usted?',
    more: 'Más…', alreadyCounted: 'Ya me contaron', needName: 'Escriba su nombre.',
    needPassword: 'Escriba la contraseña de la reunión.', needCount: 'Elija cuántas personas están viendo.',
    wrongPassword: 'Contraseña incorrecta. Revísela e inténtelo de nuevo.',
    tooMany: 'Demasiados intentos. Espere unos minutos e inténtelo de nuevo.',
    invalidLink: 'Este enlace ya no es válido. Pida a su congregación el enlace actual.',
    startsIn: 'La reunión empieza en {m} min', startsAt: 'La reunión empieza a las {t}', startsNow: 'La reunión está empezando',
  },
};
const LANG = (navigator.language || 'en').slice(0, 2);
const T = STRINGS[LANG] || STRINGS.en;
const $ = id => document.getElementById(id);
for (const el of document.querySelectorAll('[data-t]')) {
  const key = el.dataset.t;
  if (T[key]) el.textContent = T[key];
}
document.documentElement.lang = STRINGS[LANG] ? LANG : 'en';

// ── device identity ───────────────────────────────────────────────────────
function randomId(bytes) {
  const a = new Uint8Array(bytes);
  crypto.getRandomValues(a);
  return Array.from(a, b => b.toString(16).padStart(2, '0')).join('');
}
function loadDevice() {
  try {
    const saved = JSON.parse(localStorage.getItem(STORE) || 'null');
    if (saved && saved.device && saved.secret) return saved;
  } catch (e) { /* storage unavailable */ }
  return { device: randomId(16), secret: randomId(24) };
}
const me = loadDevice();
const profile = (() => {
  try { return JSON.parse(localStorage.getItem(PROFILE) || 'null') || {}; } catch (e) { return {}; }
})();
function saveDevice() {
  try {
    localStorage.setItem(STORE, JSON.stringify({ device: me.device, secret: me.secret, joined: !!me.joined }));
    localStorage.setItem(PROFILE, JSON.stringify({ name: profile.name || '', count: profile.count }));
  } catch (e) { /* ignore */ }
}

// ── views ─────────────────────────────────────────────────────────────────
const views = ['name', 'lobby', 'meeting', 'message'];
function show(view) {
  for (const v of views) $(`view-${v}`).hidden = v !== view;
}
function message(text, canRejoin = false) {
  $('message-text').textContent = text;
  $('rejoin').hidden = !canRejoin;
  show('message');
}
function connectionNote(text) {
  const el = $('connection');
  el.hidden = !text;
  el.textContent = text || '';
  el.classList.toggle('bad', !!text);
}

// ── state ─────────────────────────────────────────────────────────────────
let sc = null;              // Galene ServerConnection
let finished = false;       // ended / removed: never reconnect
let attempt = 0, retryTimer = null, wantGalene = false;
let present = false, partHolder = false, live = false, handRaised = false;
let micStream = null, upMic = null, camStream = null, upCam = null;
let wakeLock = null;

async function post(path, body) {
  const r = await fetch(`${API}/${path}`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
  });
  return { ok: r.ok, data: await r.json().catch(() => ({})) };
}

// ── hall events (lobby, admit, moderation) ────────────────────────────────
let events = null, eventsRetry = 0;
function openEvents() {
  if (finished) return;
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  events = new WebSocket(`${proto}://${location.host}${API}/events`);
  events.onopen = () => {
    eventsRetry = 0;
    events.send(JSON.stringify({ type: 'auth', device: me.device, secret: me.secret }));
  };
  events.onmessage = e => onHallEvent(JSON.parse(e.data));
  events.onclose = () => {
    events = null;
    if (!finished) setTimeout(openEvents, Math.min(8000, 500 * 2 ** eventsRetry++));
  };
}
function sendEvent(msg) {
  if (events && events.readyState === WebSocket.OPEN) events.send(JSON.stringify(msg));
}

function onHallEvent(m) {
  switch (m.type) {
    case 'admitted': startGalene(m.token); break;
    case 'renamed': profile.name = m.name; saveDevice(); restartGalene(m.token); break;
    case 'removed': finish(T.removed); break;
    case 'ended': finish(T.ended); break;
    case 'called_on': setLive(true); setHand(false); break;
    case 'done': setLive(false); break;
    case 'hand_lowered': setHand(false); break;
    case 'part_holder': partHolder = !!m.value; updatePartControls(); break;
  }
}

function finish(text) {
  finished = true;
  wantGalene = false;
  clearTimeout(retryTimer);
  stopMic(); stopCam();
  if (sc) { try { sc.close(); } catch (e) { /* ignore */ } sc = null; }
  if (events) events.close();
  releaseWakeLock();
  message(text);
}

// ── joining ───────────────────────────────────────────────────────────────
let count = null;
function selectCount(value) {
  count = value;
  for (const b of document.querySelectorAll('#name-form [data-count]')) {
    const v = b.dataset.count;
    b.classList.toggle('selected', v === 'more' ? value !== null && value > 5 : Number(v) === value);
  }
}
for (const b of document.querySelectorAll('#name-form [data-count]')) {
  b.addEventListener('click', () => {
    if (b.dataset.count === 'more') {
      $('count-more').hidden = false;
      $('count-more').focus();
      const n = Number($('count-more').value);
      selectCount(n >= 6 && n <= 20 ? n : null);
      b.classList.add('selected');
      return;
    }
    $('count-more').hidden = true;
    selectCount(Number(b.dataset.count));
  });
}
$('count-more').addEventListener('input', () => {
  const n = Math.round(Number($('count-more').value));
  selectCount(n >= 6 && n <= 20 ? n : null);
  document.querySelector('#name-form [data-count=more]').classList.add('selected');
});

function formError(text) {
  $('form-error').textContent = text || '';
  $('form-error').hidden = !text;
}

async function requestJoin(password) {
  show('lobby');
  connectionNote('');
  let res;
  try {
    res = await post('join', { device: me.device, secret: me.secret, name: profile.name, password, count });
  } catch (e) {
    connectionNote(T.reconnecting);
    setTimeout(() => requestJoin(password), 3000);
    return;
  }
  const s = res.data.status;
  if (s === 'ended') return finish(T.ended);
  if (s === 'refused') {
    if (res.data.reason === 'password') { show('name'); formError(T.wrongPassword); $('password').select(); return; }
    if (res.data.reason === 'too many attempts') { show('name'); formError(T.tooMany); return; }
    return finish(res.data.reason === 'removed' ? T.removed : T.refused);
  }
  me.joined = true;
  saveDevice();
  openEvents();
  if (s === 'admitted') startGalene(res.data.token);
}

$('name-form').addEventListener('submit', e => {
  e.preventDefault();
  const name = $('name').value.trim();
  const password = $('password').value.trim();
  if (!name) return formError(T.needName);
  if (!password) return formError(T.needPassword);
  if (count === null) return formError(T.needCount);
  formError('');
  profile.name = name;
  profile.count = count;
  saveDevice();
  requestJoin(password);
});
$('rejoin').addEventListener('click', () => location.reload());

// A device that already joined this meeting (a reload, or Solin restarted) goes
// straight back to where it was, without the form.
async function resume() {
  try {
    const res = await post('token', { device: me.device, secret: me.secret });
    const s = res.data.status;
    if (s === 'admitted') { openEvents(); startGalene(res.data.token); return true; }
    if (s === 'lobby') { show('lobby'); openEvents(); return true; }
    if (s === 'ended') { finish(T.ended); return true; }
    if (s === 'refused' && res.data.reason === 'removed') { finish(T.removed); return true; }
  } catch (e) { /* the hall is not answering: show the form */ }
  return false;
}

// ── countdown to the start (subtle; uses the hall's clock, not the phone's) ──
let startsAt = 0, clockOffset = 0;
function tickCountdown() {
  const el = $('countdown');
  if (!startsAt || finished) { el.hidden = true; return; }
  const minutes = Math.ceil((startsAt - (Date.now() + clockOffset)) / 60000);
  if (minutes < -1) { el.hidden = true; return; }
  el.hidden = false;
  if (minutes > 60) {
    const at = new Date(startsAt).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    el.textContent = T.startsAt.replace('{t}', at);
  } else {
    el.textContent = minutes > 0 ? T.startsIn.replace('{m}', minutes) : T.startsNow;
  }
}
setInterval(tickCountdown, 15000);

// ── Galene ────────────────────────────────────────────────────────────────
function galeneUrl() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  return `${proto}://${location.host}${BASE}/ws`;
}

function startGalene(token) {
  wantGalene = true;
  show('meeting');
  connectGalene(token);
}

function restartGalene(token) {
  if (sc) { const old = sc; sc = null; try { old.close(); } catch (e) { /* ignore */ } }
  connectGalene(token);
}

function connectGalene(token) {
  retryTimer = null;
  if (finished || !wantGalene) return;
  connectionNote(T.connecting);
  const conn = new ServerConnection();
  sc = conn;
  conn.onconnected = function () { this.join(SLUG, null, { type: 'token', token }); };
  conn.onjoined = function (kind, group, perms) {
    if (conn !== sc) return;
    if (kind === 'fail') { scheduleReconnect(); return; }
    if (kind === 'join') {
      attempt = 0;
      connectionNote('');
      this.userAction('setdata', this.id, { device: me.device, raisehand: handToken });
      requestMedia();
    }
    setPresent((perms || []).includes('present'));
  };
  conn.ondownstream = function (stream) {
    stream.ondowntrack = (track, transceiver, ms) => attachDown(stream, ms);
    stream.onclose = () => detachDown(stream);
  };
  conn.onusermessage = function (id, dest, username, time, privileged, kind) {
    if (kind === 'kicked' && privileged) finish(T.removed);
  };
  conn.onclose = function () {
    if (conn !== sc) return;
    sc = null;
    present = false;
    stopMic(); stopCam();
    scheduleReconnect();
  };
  try { conn.connect(galeneUrl()); } catch (e) { scheduleReconnect(); }
}

// Part-holders' cameras go up under their own label: the hall puts them in the
// program, so attendees never download them separately (Galene matches a stream's
// label first, then '').
const PARTICIPANT_LABEL = 'participant';
// 360p like the program's default quality: the hall shows it in a scene box.
const PARTICIPANT_VIDEO = { width: { ideal: 640 }, height: { ideal: 360 }, frameRate: { ideal: 30 } };
const PARTICIPANT_VIDEO_BITRATE = 500000;

function requestMedia() {
  if (!sc) return;
  sc.request({ '': $('audio-only').checked ? ['audio'] : ['audio', 'video'], [PARTICIPANT_LABEL]: [] });
}

async function scheduleReconnect(now = false) {
  if (finished || !wantGalene || retryTimer) return;
  connectionNote(T.reconnecting);
  const delay = now ? 0 : Math.min(8000, 500 * 2 ** attempt++);
  retryTimer = setTimeout(async () => {
    retryTimer = null;
    try {
      const res = await post('token', { device: me.device, secret: me.secret });
      const s = res.data.status;
      if (s === 'admitted') return connectGalene(res.data.token);
      if (s === 'ended') return finish(T.ended);
      if (s === 'refused') return finish(T.removed);
      if (s === 'lobby') { wantGalene = false; show('lobby'); return; }
    } catch (e) { /* hall unreachable: try again */ }
    scheduleReconnect();
  }, delay);
}

document.addEventListener('visibilitychange', () => {
  if (document.visibilityState !== 'visible') return;
  if (wakeLock === null && $('wake-lock').checked) requestWakeLock();
  if (wantGalene && !sc) { clearTimeout(retryTimer); retryTimer = null; scheduleReconnect(true); }
});

// ── program playback ──────────────────────────────────────────────────────
// The hall's program (published as PROGRAM_USER) is the only stream shown. Every
// other stream is a commenter or a remote part-holder: their voices play here
// directly (the hall feed is mix-minus), and part-holders' video already reaches
// attendees inside the program.
const PROGRAM_USER = 'Solin program';
const voices = new Map();

function attachDown(stream, ms) {
  if (stream.username === PROGRAM_USER) { attachProgram(ms); return; }
  let a = voices.get(stream.id);
  if (!a) {
    a = document.createElement('audio');
    a.autoplay = true;
    a.hidden = true;
    document.body.appendChild(a);
    voices.set(stream.id, a);
  }
  if (a.srcObject !== ms) a.srcObject = ms;
  play(a);
}

function detachDown(stream) {
  const a = voices.get(stream.id);
  if (!a) return;
  voices.delete(stream.id);
  a.srcObject = null;
  a.remove();
}

function play(el) {
  el.play().catch(() => {
    // Autoplay with sound needs a gesture on some browsers.
    $('unmute').hidden = false;
  });
}

function attachProgram(ms) {
  const v = $('program');
  if (v.srcObject !== ms) v.srcObject = ms;
  v.play().then(() => { $('unmute').hidden = true; }).catch(() => { $('unmute').hidden = false; });
  if ('mediaSession' in navigator) {
    navigator.mediaSession.metadata = new MediaMetadata({ title: document.title, artist: 'Solin' });
    navigator.mediaSession.playbackState = 'playing';
  }
}
$('unmute').addEventListener('click', () => {
  $('program').muted = false;
  $('program').play();
  for (const a of voices.values()) a.play().catch(() => {});
  $('unmute').hidden = true;
});
$('audio-only').addEventListener('change', requestMedia);

// ── hand, microphone, camera ──────────────────────────────────────────────
// A raised hand carries the time it was raised, so the hall can tell a new raise
// from a stale value it already lowered. Lowering always clears it in Galene too.
let handToken = null;
function setHand(raised) {
  handRaised = raised;
  handToken = raised ? Date.now() : null;
  const b = $('hand');
  b.textContent = raised ? T.lowerHand : T.raiseHand;
  b.classList.toggle('raised', raised);
  if (sc) sc.userAction('setdata', sc.id, { raisehand: handToken });
}
$('hand').addEventListener('click', () => setHand(!handRaised));

function setLive(value) {
  live = value;
  $('live-banner').hidden = !value;
  $('hand').hidden = value;
  if (value && present) startMic();      // called on: the microphone opens by itself
  else if (!value && !partHolder) stopMic();
  updatePartControls();
}
$('done').addEventListener('click', () => { sendEvent({ type: 'done' }); setLive(false); });

function setPresent(value) {
  present = value;
  if (!value) { stopMic(); stopCam(); }
  else if (live) startMic();
  updatePartControls();
}

async function startMic() {
  if (!present || !sc || upMic) return;
  try {
    micStream = micStream || await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true }, video: false,
    });
  } catch (e) {
    $('status-line').textContent = T.noMic;
    return;
  }
  if (!present || !sc) return;
  upMic = sc.newUpStream();
  upMic.label = 'camera';
  upMic.setStream(micStream);
  upMic.onclose = () => { upMic = null; };
  for (const t of micStream.getAudioTracks()) upMic.pc.addTransceiver(t, { direction: 'sendonly', streams: [micStream] });
  $('status-line').textContent = T.micReady;
  updatePartControls();
}

function stopMic() {
  if (upMic) { try { upMic.close(); } catch (e) { /* ignore */ } upMic = null; }
  if (micStream) { micStream.getTracks().forEach(t => t.stop()); micStream = null; }
  updatePartControls();
}

async function startCam() {
  if (!present || !sc || upCam || !partHolder) return;
  try {
    camStream = await navigator.mediaDevices.getUserMedia({ video: PARTICIPANT_VIDEO, audio: false });
  } catch (e) { return; }
  upCam = sc.newUpStream();
  upCam.label = PARTICIPANT_LABEL;
  upCam.setStream(camStream);
  upCam.onclose = () => { upCam = null; updatePartControls(); };
  for (const t of camStream.getVideoTracks()) {
    upCam.pc.addTransceiver(t, { direction: 'sendonly', streams: [camStream],
                                 sendEncodings: [{ maxBitrate: PARTICIPANT_VIDEO_BITRATE }] });
  }
  updatePartControls();
}

function stopCam() {
  if (upCam) { try { upCam.close(); } catch (e) { /* ignore */ } upCam = null; }
  if (camStream) { camStream.getTracks().forEach(t => t.stop()); camStream = null; }
  updatePartControls();
}

function updatePartControls() {
  // Microphone toggle: while allowed to speak without being called on (microphones
  // unlocked before/after the meeting, or a part-holder). Camera: part-holders only.
  $('part-mic').hidden = !(present && !live);
  $('part-cam').hidden = !(present && partHolder);
  $('part-mic').textContent = upMic ? T.micOff : T.micOn;
  $('part-cam').textContent = upCam ? T.cameraOff : T.cameraOn;
  // While the camera goes up, the part-holder sees it in a corner of the video.
  const selfView = $('self-view');
  const showing = upCam && camStream ? camStream : null;
  if (selfView.srcObject !== showing) selfView.srcObject = showing;
  selfView.hidden = !showing;
  if (showing) selfView.play().catch(() => {});
}
$('part-mic').addEventListener('click', () => { upMic ? stopMic() : startMic(); });
$('part-cam').addEventListener('click', () => { upCam ? stopCam() : startCam(); });

// ── keep the screen on (optional) ─────────────────────────────────────────
async function requestWakeLock() {
  if (!('wakeLock' in navigator)) return;
  try {
    wakeLock = await navigator.wakeLock.request('screen');
    wakeLock.addEventListener('release', () => { wakeLock = null; });
  } catch (e) { wakeLock = null; }
}
function releaseWakeLock() {
  if (wakeLock) { wakeLock.release(); wakeLock = null; }
}
$('wake-lock').addEventListener('change', e => (e.target.checked ? requestWakeLock() : releaseWakeLock()));

// ── start ─────────────────────────────────────────────────────────────────
(async () => {
  try {
    const info = await (await fetch(`${API}/info`)).json();
    if (info.valid === false) return message(T.invalidLink);
    if (info.title) { $('title').textContent = info.title; document.title = info.title; }
    if (info.ended) return message(T.ended);
    if (info.startsAt) {
      startsAt = Date.parse(info.startsAt);
      clockOffset = info.now ? info.now * 1000 - Date.now() : 0;
      tickCountdown();
    }
  } catch (e) { /* the join request will report problems */ }
  if (me.joined && await resume()) return;
  if (profile.name) $('name').value = profile.name;
  if (Number.isInteger(profile.count) && profile.count >= 0 && profile.count <= 20) {
    if (profile.count > 5) { $('count-more').hidden = false; $('count-more').value = profile.count; }
    selectCount(profile.count);
  }
  show('name');
  ($('name').value ? $('password') : $('name')).focus();
})();
