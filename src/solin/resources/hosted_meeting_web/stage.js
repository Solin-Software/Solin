// Solin hosted meeting — the conductor's stage page (read-only raised hands).
// The access key travels in the URL fragment, which browsers never send to servers.
'use strict';

const SLUG = location.pathname.split('/').filter(Boolean).pop();
const KEY = location.hash.slice(1);
const STRINGS = {
  en: { live: 'Speaking', hands: 'Raised hands', empty: 'No hands raised.', lost: 'Reconnecting…', ended: 'The meeting has ended.', denied: 'This link is not valid.' },
  pt: { live: 'Falando', hands: 'Mãos levantadas', empty: 'Nenhuma mão levantada.', lost: 'Reconectando…', ended: 'A reunião terminou.', denied: 'Este link não é válido.' },
  es: { live: 'Hablando', hands: 'Manos levantadas', empty: 'Ninguna mano levantada.', lost: 'Reconectando…', ended: 'La reunión ha terminado.', denied: 'Este enlace no es válido.' },
};
const T = STRINGS[(navigator.language || 'en').slice(0, 2)] || STRINGS.en;
const $ = id => document.getElementById(id);
$('live-label').textContent = T.live;
$('hands-label').textContent = T.hands;
$('empty').textContent = T.empty;
document.title = T.hands;

let retry = 0, denied = false;
function connect() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  const ws = new WebSocket(`${proto}://${location.host}/api/${SLUG}/stage`);
  ws.onopen = () => { ws.send(JSON.stringify({ type: 'auth', key: KEY })); $('state').textContent = ''; };
  ws.onmessage = e => {
    retry = 0;
    const m = JSON.parse(e.data);
    if (m.type !== 'stage') return;
    $('live').textContent = m.live || '—';
    const list = $('hands');
    list.replaceChildren(...m.hands.map(name => { const li = document.createElement('li'); li.textContent = name; return li; }));
    $('empty').hidden = m.hands.length > 0;
    if (m.ended) $('state').textContent = T.ended;
  };
  ws.onclose = e => {
    if (e.code === 4401) { denied = true; $('state').textContent = T.denied; return; }
    $('state').textContent = T.lost;
    if (!denied) setTimeout(connect, Math.min(8000, 500 * 2 ** retry++));
  };
}
connect();
