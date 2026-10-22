// Stage page: the raised-hands queue, read-only, for the conductor's own device.
// Remote commenters are never shown on the hall's monitors; this page is the only place
// the conductor sees who is waiting to comment. It needs no audio and no LiveKit client.
(function () {
  "use strict";
  const T = window.SolinText;
  T.apply();
  const secret = location.pathname.split("/").pop();
  const list = document.getElementById("hands");
  const empty = document.getElementById("empty");
  const watching = document.getElementById("watching");
  let last = "";

  function render(state) {
    const key = JSON.stringify(state);
    if (key === last) return;
    last = key;
    list.replaceChildren(...(state.hands || []).map((hand) => {
      const item = document.createElement("li");
      item.classList.toggle("speaking", !!hand.speaking);
      const position = document.createElement("span");
      position.className = "position";
      position.textContent = String(hand.position);
      const name = document.createElement("span");
      name.textContent = hand.name;
      item.append(position, name);
      if (hand.speaking) {
        const badge = document.createElement("span");
        badge.className = "badge";
        badge.textContent = T.t("stageSpeaking");
        item.append(badge);
      }
      return item;
    }));
    empty.textContent = !state.open ? T.t("stageClosed") : state.hands.length ? "" : T.t("stageNone");
    watching.textContent = state.open ? T.t("stageWatching", { n: state.attendance }) : "";
  }

  async function poll() {
    try {
      const reply = await fetch("/api/stage/" + encodeURIComponent(secret) + "/hands", { cache: "no-store" });
      render(reply.ok ? await reply.json() : { open: false, hands: [] });
    } catch {
      render({ open: false, hands: [] });
    }
    setTimeout(poll, 1000);
  }
  poll();
})();
