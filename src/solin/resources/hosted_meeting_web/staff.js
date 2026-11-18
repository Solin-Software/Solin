// Solin hosted meeting — sign-in shared by the stage page and the moderator page.
//
// The first time, the page asks for a name and the meeting password, and waits until
// the operator approves this device. Approved devices are remembered:
//   * moderators keep an ECDSA P-256 key pair in IndexedDB, created non-extractable,
//     and sign a fresh challenge on every visit (a copied link, cookie or password is
//     useless on another device). Without a secure context (plain HTTP on the hall
//     network) WebCrypto is unavailable, so they fall back to a random secret;
//   * the stage page keeps a random secret (it is read-only).
'use strict';

const StaffSession = (() => {
  const BASE = document.body.dataset.base;
  const DB = 'solin-meeting-devices';

  function randomId(bytes) {
    const a = new Uint8Array(bytes);
    crypto.getRandomValues(a);
    return Array.from(a, b => b.toString(16).padStart(2, '0')).join('');
  }
  const b64 = buf => btoa(String.fromCharCode(...new Uint8Array(buf)));

  // ── key pair in IndexedDB (moderators) ──────────────────────────────────
  function idb(mode, fn) {
    return new Promise((resolve, reject) => {
      const open = indexedDB.open(DB, 1);
      open.onupgradeneeded = () => open.result.createObjectStore('keys');
      open.onerror = () => reject(open.error);
      open.onsuccess = () => {
        const tx = open.result.transaction('keys', mode);
        const req = fn(tx.objectStore('keys'));
        tx.oncomplete = () => resolve(req && req.result);
        tx.onerror = () => reject(tx.error);
      };
    });
  }
  async function keyPair(role) {
    let pair = await idb('readonly', store => store.get(role)).catch(() => null);
    if (!pair) {
      pair = await crypto.subtle.generateKey({ name: 'ECDSA', namedCurve: 'P-256' }, false, ['sign', 'verify']);
      await idb('readwrite', store => store.put(pair, role));
    }
    return pair;
  }
  async function forgetKey(role) {
    await idb('readwrite', store => store.delete(role)).catch(() => {});
  }

  // ── the session ─────────────────────────────────────────────────────────
  // ui: { login(reason), pending(), ready(), view(msg), message(type, msg), offline() }
  function start(role, ui) {
    const storeKey = `solin-meeting-device:${role}`;
    const useKey = role === 'moderator' && !!(window.crypto && crypto.subtle && window.indexedDB);
    let saved = {};
    try { saved = JSON.parse(localStorage.getItem(storeKey) || 'null') || {}; } catch (e) { saved = {}; }
    if (!saved.device) saved.device = randomId(16);
    if (!useKey && !saved.secret) saved.secret = randomId(24);
    const save = () => { try { localStorage.setItem(storeKey, JSON.stringify(saved)); } catch (e) { /* ignore */ } };
    save();

    let ws = null, retry = 0, closedForGood = false;
    const send = msg => { if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(msg)); };

    async function sign(challenge) {
      const pair = await keyPair(role);
      const data = new TextEncoder().encode(`solin-meeting-device:${challenge}`);
      const sig = await crypto.subtle.sign({ name: 'ECDSA', hash: 'SHA-256' }, pair.privateKey, data);
      send({ type: 'proof', signature: b64(sig) });
    }

    function connect() {
      const proto = location.protocol === 'https:' ? 'wss' : 'ws';
      ws = new WebSocket(`${proto}://${location.host}${BASE}/api/staff`);
      ws.onopen = () => {
        retry = 0;
        ws.send(JSON.stringify({ type: 'hello', role, device: saved.device, secret: useKey ? '' : saved.secret }));
      };
      ws.onmessage = async e => {
        const m = JSON.parse(e.data);
        switch (m.type) {
          case 'login': ui.login(m.reason || ''); break;
          case 'pending': ui.pending(); break;
          case 'challenge': await sign(m.challenge); break;
          case 'ready': ui.ready(); break;
          case 'view': ui.view(m); break;
          case 'refused': case 'removed': case 'ended':
            closedForGood = true;
            if (m.type !== 'ended' && useKey) await forgetKey(role);
            ui.message(m.type, m);
            break;
          default: ui.message(m.type, m);
        }
      };
      ws.onclose = () => {
        if (closedForGood) return;
        ui.offline();
        setTimeout(connect, Math.min(8000, 500 * 2 ** retry++));
      };
    }

    async function login(name, password) {
      let keyKind = 'secret', key = saved.secret;
      if (useKey) {
        const pair = await keyPair(role);
        keyKind = 'ecdsa';
        key = b64(await crypto.subtle.exportKey('spki', pair.publicKey));
      }
      send({ type: 'login', name, password, keyKind, key });
    }

    connect();
    return { login, command: (name, ...args) => send({ type: 'command', name, args }) };
  }

  return { start };
})();
