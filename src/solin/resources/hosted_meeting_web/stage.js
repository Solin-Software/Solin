// Solin hosted meeting — the stage page: who is speaking, raised hands in order and the
// participants, in large text for the platform. Read-only; the device is approved once
// by the operator (see staff.js).
'use strict';

const STRINGS = {
  en: { yourName: 'Your name', password: 'Meeting password', continue: 'Continue',
        pending: 'Waiting for the operator to approve this device…', live: 'Speaking', hands: 'Raised hands',
        empty: 'No hands raised.', participants: 'Participants', lost: 'Reconnecting…',
        ended: 'The meeting has ended.', refused: 'The operator did not approve this device.',
        removed: 'This device is no longer approved.', wrongPassword: 'Wrong password. Check it and try again.',
        tooMany: 'Too many attempts. Wait a few minutes and try again.', needBoth: 'Type your name and the password.',
        stage: 'Stage', moderator: 'Moderator' },
  pt: { yourName: 'Seu nome', password: 'Senha da reunião', continue: 'Continuar',
        pending: 'Aguardando o operador aprovar este aparelho…', live: 'Falando', hands: 'Mãos levantadas',
        empty: 'Nenhuma mão levantada.', participants: 'Participantes', lost: 'Reconectando…',
        ended: 'A reunião terminou.', refused: 'O operador não aprovou este aparelho.',
        removed: 'Este aparelho não está mais aprovado.', wrongPassword: 'Senha incorreta. Confira e tente de novo.',
        tooMany: 'Tentativas demais. Aguarde alguns minutos e tente de novo.', needBoth: 'Digite seu nome e a senha.',
        stage: 'Tribuna', moderator: 'Moderador' },
  es: { yourName: 'Su nombre', password: 'Contraseña de la reunión', continue: 'Continuar',
        pending: 'Esperando que el operador apruebe este dispositivo…', live: 'Hablando', hands: 'Manos levantadas',
        empty: 'Ninguna mano levantada.', participants: 'Participantes', lost: 'Reconectando…',
        ended: 'La reunión ha terminado.', refused: 'El operador no aprobó este dispositivo.',
        removed: 'Este dispositivo ya no está aprobado.', wrongPassword: 'Contraseña incorrecta. Revísela e inténtelo de nuevo.',
        tooMany: 'Demasiados intentos. Espere unos minutos e inténtelo de nuevo.', needBoth: 'Escriba su nombre y la contraseña.',
        stage: 'Plataforma', moderator: 'Moderador' },
};
const LANG = (navigator.language || 'en').slice(0, 2);
const T = STRINGS[LANG] || STRINGS.en;
const $ = id => document.getElementById(id);
for (const el of document.querySelectorAll('[data-t]')) if (T[el.dataset.t]) el.textContent = T[el.dataset.t];
document.documentElement.lang = STRINGS[LANG] ? LANG : 'en';
document.title = T.hands;

function show(view) {
  for (const v of ['login', 'pending', 'board']) $(`view-${v}`).hidden = v !== view;
}
function badge(role) {
  const span = document.createElement('span');
  span.className = `badge ${role}`;
  span.textContent = T[role] || role;
  return span;
}
function item(tag, name, role) {
  const li = document.createElement(tag);
  li.textContent = name;
  if (role) { li.append(' '); li.append(badge(role)); }
  return li;
}

const session = StaffSession.start('stage', {
  login(reason) {
    show('login');
    $('form-error').hidden = !reason;
    $('form-error').textContent = reason === 'password' ? T.wrongPassword : reason ? T.tooMany : '';
  },
  pending() { $('state').textContent = ''; show('pending'); },
  ready() { $('state').textContent = ''; show('board'); },
  view(m) {
    show('board');
    $('state').textContent = m.ended ? T.ended : '';
    $('live').textContent = m.live || '—';
    $('hands').replaceChildren(...m.hands.map(name => item('li', name)));
    $('empty').hidden = m.hands.length > 0;
    $('participants').replaceChildren(
      ...(m.staff || []).map(s => item('li', s.name, s.role)),
      ...(m.participants || []).map(name => item('li', name)));
  },
  message(type) { show(null); $('state').textContent = T[type] || ''; },
  offline() { $('state').textContent = T.lost; },
});

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
