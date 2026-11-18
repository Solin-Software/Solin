// Solin hosted meeting — the remote moderator's page. The same power as the Solin
// operator over people: the lobby, calling on and releasing commenters, the
// microphones, raised hands and removing someone. Not the program, the links or the
// end of the meeting. The device is approved once by the operator (see staff.js).
'use strict';

const STRINGS = {
  en: { title: 'Moderator', yourName: 'Your name', password: 'Meeting password', continue: 'Continue',
        approvalHint: 'The meeting operator approves this device the first time.',
        pending: 'Waiting for the operator to approve this device…', lobby: 'Lobby', hands: 'Raised hands',
        attendees: 'Attendees', admitAll: 'Admit all', admit: 'Admit', refuse: 'Refuse', callOn: 'Call on',
        done: 'Done', lower: 'Lower', remove: 'Remove', lockMics: 'Lock microphones', unlockMics: 'Unlock microphones',
        totals: 'Online: {p} people on {d} devices', official: 'Attendance: {p} people on {d} devices',
        alreadyCounted: 'already counted', ok: 'OK', lost: 'Reconnecting…', ended: 'The meeting has ended.',
        refused: 'The operator did not approve this device.', removed: 'This device is no longer approved.',
        wrongPassword: 'Wrong password. Check it and try again.',
        tooMany: 'Too many attempts. Wait a few minutes and try again.', needBoth: 'Type your name and the password.',
        stage: 'Stage', moderator: 'Moderator', confirmRemove: 'Remove {n} from the meeting?' },
  pt: { title: 'Moderador', yourName: 'Seu nome', password: 'Senha da reunião', continue: 'Continuar',
        approvalHint: 'O operador da reunião aprova este aparelho na primeira vez.',
        pending: 'Aguardando o operador aprovar este aparelho…', lobby: 'Sala de espera', hands: 'Mãos levantadas',
        attendees: 'Assistentes', admitAll: 'Admitir todos', admit: 'Admitir', refuse: 'Recusar', callOn: 'Dar a palavra',
        done: 'Encerrar', lower: 'Abaixar', remove: 'Remover', lockMics: 'Bloquear microfones', unlockMics: 'Liberar microfones',
        totals: 'Online: {p} pessoas em {d} aparelhos', official: 'Assistência: {p} pessoas em {d} aparelhos',
        alreadyCounted: 'já contado', ok: 'OK', lost: 'Reconectando…', ended: 'A reunião terminou.',
        refused: 'O operador não aprovou este aparelho.', removed: 'Este aparelho não está mais aprovado.',
        wrongPassword: 'Senha incorreta. Confira e tente de novo.',
        tooMany: 'Tentativas demais. Aguarde alguns minutos e tente de novo.', needBoth: 'Digite seu nome e a senha.',
        stage: 'Tribuna', moderator: 'Moderador', confirmRemove: 'Remover {n} da reunião?' },
  es: { title: 'Moderador', yourName: 'Su nombre', password: 'Contraseña de la reunión', continue: 'Continuar',
        approvalHint: 'El operador de la reunión aprueba este dispositivo la primera vez.',
        pending: 'Esperando que el operador apruebe este dispositivo…', lobby: 'Sala de espera', hands: 'Manos levantadas',
        attendees: 'Asistentes', admitAll: 'Admitir a todos', admit: 'Admitir', refuse: 'Rechazar', callOn: 'Dar la palabra',
        done: 'Terminar', lower: 'Bajar', remove: 'Retirar', lockMics: 'Bloquear micrófonos', unlockMics: 'Desbloquear micrófonos',
        totals: 'En línea: {p} personas en {d} dispositivos', official: 'Asistencia: {p} personas en {d} dispositivos',
        alreadyCounted: 'ya contado', ok: 'OK', lost: 'Reconectando…', ended: 'La reunión ha terminado.',
        refused: 'El operador no aprobó este dispositivo.', removed: 'Este dispositivo ya no está aprobado.',
        wrongPassword: 'Contraseña incorrecta. Revísela e inténtelo de nuevo.',
        tooMany: 'Demasiados intentos. Espere unos minutos e inténtelo de nuevo.', needBoth: 'Escriba su nombre y la contraseña.',
        stage: 'Plataforma', moderator: 'Moderador', confirmRemove: '¿Retirar a {n} de la reunión?' },
};
const LANG = (navigator.language || 'en').slice(0, 2);
const T = STRINGS[LANG] || STRINGS.en;
const $ = id => document.getElementById(id);
for (const el of document.querySelectorAll('[data-t]')) if (T[el.dataset.t]) el.textContent = T[el.dataset.t];
document.documentElement.lang = STRINGS[LANG] ? LANG : 'en';
document.title = T.title;

function show(view) {
  for (const v of ['login', 'pending', 'console']) $(`view-${v}`).hidden = v !== view;
}
const fill = (text, values) => text.replace(/\{(\w)\}/g, (_, k) => values[k]);

function button(label, onClick, cls = '') {
  const b = document.createElement('button');
  b.textContent = label;
  if (cls) b.className = cls;
  b.addEventListener('click', onClick);
  return b;
}
function row(name, extra, buttons, role) {
  const li = document.createElement('li');
  const label = document.createElement('span');
  label.className = 'name';
  label.textContent = name;
  if (extra) { const small = document.createElement('span'); small.className = 'muted'; small.textContent = ` · ${extra}`; label.append(small); }
  if (role) { const b = document.createElement('span'); b.className = `badge ${role}`; b.textContent = T[role] || role; label.append(' ', b); }
  li.append(label, ...buttons);
  return li;
}
const countLabel = a => (a.count === 0 ? T.alreadyCounted : String(a.count));

let seenCount = '';
const session = StaffSession.start('moderator', {
  login(reason) {
    show('login');
    $('form-error').hidden = !reason;
    $('form-error').textContent = reason === 'password' ? T.wrongPassword : reason ? T.tooMany : '';
  },
  pending() { $('state').textContent = ''; show('pending'); },
  ready() { $('state').textContent = ''; show('console'); },
  view(m) {
    show('console');
    $('state').textContent = m.ended ? T.ended : '';
    $('totals').textContent = fill(T.totals, { p: m.people || 0, d: m.devices || 0 });
    $('mics').textContent = m.micsLocked === false ? T.lockMics : T.unlockMics;
    $('mics').onclick = () => session.command('lock_mics', m.micsLocked === false);
    $('admit-all').hidden = m.lobby.length < 2;
    $('lobby-count').textContent = `(${m.lobby.length})`;
    $('lobby').replaceChildren(...m.lobby.map(a => row(a.name, countLabel(a), [
      button(T.admit, () => session.command('admit', a.device), 'primary'),
      button(T.refuse, () => session.command('remove', a.device)),
    ])));
    $('hands-count').textContent = `(${m.hands.length})`;
    $('hands').replaceChildren(...m.hands.map(a => row(a.name, '', [
      button(T.callOn, () => session.command('call_on', a.device), 'primary'),
      button(T.lower, () => session.command('lower_hand', a.device)),
    ])));
    $('attendees-count').textContent = `(${m.attendees.length})`;
    $('staff').replaceChildren(...(m.staff || []).map(s => row(s.name, '', [], s.role)));
    $('attendees').replaceChildren(...m.attendees.map(a => {
      const li = row(a.name + (a.hand ? ' ✋' : ''), countLabel(a), [
        a.live ? button(T.done, () => session.command('done', a.device), 'danger')
               : button(T.callOn, () => session.command('call_on', a.device), 'primary'),
        button(T.remove, () => { if (confirm(fill(T.confirmRemove, { n: a.name }))) session.command('remove', a.device); }),
      ]);
      li.classList.toggle('offline', !a.connected);
      li.classList.toggle('live', !!a.live);
      return li;
    }));
    if (m.officialCount) showOfficial(m.officialCount);
  },
  message(type, m) {
    if (type === 'official_count') { showOfficial(m); return; }
    show(null);
    $('state').textContent = T[type] || '';
  },
  offline() { $('state').textContent = T.lost; },
});

function showOfficial(c) {
  const key = `${c.at}`;
  if (key === seenCount) return;
  $('official-text').textContent = fill(T.official, { p: c.people, d: c.devices });
  $('official').hidden = false;
  $('official-dismiss').onclick = () => { seenCount = key; $('official').hidden = true; };
}
$('admit-all').addEventListener('click', () => session.command('admit_all'));

try {
  const profile = JSON.parse(localStorage.getItem('solin-meeting:profile') || 'null');
  if (profile && profile.name) $('name').value = profile.name;
} catch (e) { /* ignore */ }
$('login-form').addEventListener('submit', e => {
  e.preventDefault();
  const name = $('name').value.trim(), password = $('password').value.trim();
  if (!name || !password) { $('form-error').hidden = false; $('form-error').textContent = T.needBoth; return; }
  session.login(name, password);
});
