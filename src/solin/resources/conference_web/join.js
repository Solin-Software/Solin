// Attendee page for a Solin-hosted meeting.
//
// Behaviour proven in the hosted-meetings spike (S7, S8, S9):
// * the device keeps one identity (localStorage), so a rejoin replaces the old connection
//   and the hall re-admits it without the attendant;
// * attendance is asked once per meeting and re-sent, with the hand, on every connect
//   (the server forgets attributes when a participant reconnects);
// * the page rejoins by itself after a drop, and when it comes back to the screen with
//   no media for 3 s (phones freeze locked pages) it drops the stale session first;
// * the UI follows permission changes, not track events (clients are not always told);
// * the end-to-end encryption key comes from the link's #fragment, never from a server.
(function () {
  "use strict";
  const LK = window.LivekitClient;
  const T = window.SolinText;
  const $ = (id) => document.getElementById(id);
  T.apply();

  const store = {
    get(key) { try { return localStorage.getItem(key); } catch { return null; } },
    set(key, value) { try { localStorage.setItem(key, value); } catch { /* private mode */ } },
  };

  function newDeviceId() {
    const bytes = new Uint8Array(12);
    crypto.getRandomValues(bytes);
    return Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
  }
  const deviceId = store.get("solin.device") || (() => {
    const id = newDeviceId();
    store.set("solin.device", id);
    return id;
  })();
  const e2eeKey = new URLSearchParams(location.hash.slice(1)).get("k") || "";

  let meeting = null;
  let room = null;
  let count = 1;
  let handUp = false;
  let wantConnected = false;
  let removed = false;
  let videoTrack = null;
  let micOn = false;
  let micRefused = false; // the browser refused it; do not keep asking until the call ends
  let cameraOn = false;
  let cameraRefused = false;
  const CAMERA_SOURCE = 1; // livekit TrackSource.CAMERA
  let wakeLock = null;

  // ── Screens ──────────────────────────────────────────────────────────────
  function show(card) {
    for (const id of ["closedCard", "joinCard", "lobbyCard", "meetingCard"]) $(id).classList.toggle("hidden", id !== card);
  }
  function banner(text, error) {
    $("banner").textContent = text || "";
    $("banner").classList.toggle("hidden", !text);
    $("banner").classList.toggle("error", !!error);
  }

  // ── Meeting discovery ────────────────────────────────────────────────────
  async function loadMeeting() {
    try {
      const reply = await fetch("/api/meeting", { cache: "no-store" });
      meeting = await reply.json();
    } catch {
      meeting = { open: false };
    }
    if (meeting.hallName) $("title").textContent = meeting.hallName;
    if (!meeting.open) {
      show("closedCard");
      setTimeout(loadMeeting, 5000);
      return;
    }
    if (meeting.e2ee && !e2eeKey) {
      $("closedText").textContent = T.t("badLink");
      show("closedCard");
      return;
    }
    const saved = store.get("solin.attendance." + meeting.meetingId);
    count = parseInt(saved || "1", 10) || 1;
    $("count").textContent = count;
    $("name").value = store.get("solin.name") || "";
    $("join").textContent = T.t(saved ? "rejoin" : "join");
    show("joinCard");
  }

  $("minus").addEventListener("click", () => { count = Math.max(1, count - 1); $("count").textContent = count; });
  $("plus").addEventListener("click", () => { count = Math.min(20, count + 1); $("count").textContent = count; });

  // ── Connection ───────────────────────────────────────────────────────────
  async function connect() {
    const name = $("name").value.trim();
    const reply = await fetch("/api/join", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ deviceId, name }),
    });
    if (reply.status === 403) { removed = true; throw new Error("removed"); }
    if (!reply.ok) throw new Error("join " + reply.status);
    const { token } = await reply.json();

    const options = { adaptiveStream: true, dynacast: true };
    let keyProvider = null;
    if (e2eeKey) {
      keyProvider = new LK.ExternalE2EEKeyProvider();
      await keyProvider.setKey(e2eeKey);
      options.e2ee = { keyProvider, worker: new Worker("/vendor/livekit-client.e2ee.worker.mjs", { type: "module" }) };
    }
    const next = new LK.Room(options);
    if (keyProvider) await next.setE2EEEnabled(true);
    room = next;
    // Events from a room replaced by a rejoin are ignored.
    next.on(LK.RoomEvent.TrackSubscribed, (track) => {
      if (next !== room) return;
      attach(track);
      render();
    });
    next.on(LK.RoomEvent.ParticipantPermissionsChanged, (_previous, participant) => {
      if (next === room && (!participant || participant.isLocal)) render();
    });
    next.on(LK.RoomEvent.RoomMetadataChanged, () => { if (next === room) render(); });
    next.on(LK.RoomEvent.ParticipantAttributesChanged, (_changed, participant) => {
      // The operator lowers the hand by clearing it on the server.
      if (next === room && participant?.isLocal) showHand(!!participant.attributes.hand);
    });
    next.on(LK.RoomEvent.Reconnecting, () => { if (next === room) banner(T.t("reconnecting")); });
    next.on(LK.RoomEvent.Reconnected, () => { if (next === room) banner(""); });
    next.on(LK.RoomEvent.Disconnected, (reason) => {
      if (next !== room) return;
      if (reason === LK.DisconnectReason.PARTICIPANT_REMOVED) {
        removed = true;
        wantConnected = false;
        stopped(T.t("removed"));
        return;
      }
      if (wantConnected) rejoin();
    });
    const origin = location.origin.replace(/^http/, "ws");
    await next.connect(origin, token);
    await next.localParticipant.setAttributes({
      attendance: String(count), hand: handUp ? String(Date.now()) : "",
    }).catch(() => {});
    micOn = false;
    cameraOn = false;
    $("self").classList.add("hidden");
    banner("");
    render();
  }

  async function rejoin() {
    banner(T.t("reconnecting"));
    for (let attempt = 1; wantConnected && !removed; attempt++) {
      await new Promise((resolve) => setTimeout(resolve, Math.min(1000 * attempt, 8000)));
      if (!wantConnected) return;
      try {
        await connect();
        keepAlive();
        return;
      } catch (error) {
        if (removed) { stopped(T.t("removed")); return; }
      }
    }
  }

  function stopped(message) {
    show("closedCard");
    $("closedText").textContent = message;
    banner("");
    releaseWakeLock();
  }

  $("join").addEventListener("click", async () => {
    store.set("solin.name", $("name").value.trim());
    store.set("solin.attendance." + meeting.meetingId, String(count));
    $("join").disabled = true;
    $("join").textContent = T.t("joining");
    wantConnected = true;
    try {
      await connect();
      keepAlive();
    } catch (error) {
      wantConnected = false;
      $("join").disabled = false;
      $("join").textContent = T.t("join");
      if (removed) stopped(T.t("removed"));
      else banner(T.t("joinFailed"), true);
    }
  });

  $("leave").addEventListener("click", async () => {
    wantConnected = false;
    const current = room;
    room = null;
    await current?.disconnect().catch(() => {});
    stopped(T.t("left"));
  });

  // ── Media ────────────────────────────────────────────────────────────────
  function attach(track) {
    if (track.kind === "video") {
      videoTrack = track;
      track.attach($("video"));
      return;
    }
    const element = track.attach();
    element.playsInline = true;
    document.body.appendChild(element);
  }

  // ── State from permissions and the room's lock ───────────────────────────
  function roomLocked() {
    try { return !!JSON.parse(room?.metadata || "{}").locked; } catch { return false; }
  }

  function render() {
    if (!room) return;
    const permissions = room.localParticipant.permissions;
    const admitted = !!(permissions && permissions.canSubscribe);
    show(admitted ? "meetingCard" : "lobbyCard");
    if (admitted) room.startAudio().catch(() => {});
    const mayTalk = admitted && !!(permissions && permissions.canPublish);
    const calledOn = mayTalk && roomLocked();
    if (!calledOn && micRefused) {
      micRefused = false;
      banner("");
    }
    if (calledOn && !micOn && !micRefused) setMicrophone(true);
    if (!mayTalk && micOn) { micOn = false; room.localParticipant.setMicrophoneEnabled(false).catch(() => {}); }
    // Before the meeting (unlocked) attendees switch their own microphone; once locked
    // only the operator gives it, and it switches on by itself.
    $("mic").classList.toggle("hidden", !mayTalk || calledOn);
    $("micGap").classList.toggle("hidden", !mayTalk || calledOn);
    $("mic").textContent = T.t(micOn ? "mute" : "unmute");
    $("mic").classList.toggle("live", micOn);
    // The camera follows the same rule: allowed while locked means the operator is
    // showing this person in the hall (a remote part-holder), so it switches on.
    const sources = (permissions && permissions.canPublishSources) || [];
    const mayCamera = mayTalk && sources.some((s) => s === CAMERA_SOURCE || s === "CAMERA");
    const shown = mayCamera && roomLocked();
    if (!shown && cameraRefused) { cameraRefused = false; banner(""); }
    if (shown && !cameraOn && !cameraRefused) setCamera(true);
    if (!mayCamera && cameraOn) setCamera(false);
    $("camera").classList.toggle("hidden", !mayCamera || shown);
    $("cameraGap").classList.toggle("hidden", !mayCamera || shown);
    $("camera").textContent = T.t(cameraOn ? "cameraOff" : "cameraOn");
    $("camera").classList.toggle("live", cameraOn);
    $("meetingCard").classList.toggle("speaking", calledOn);
    $("status").textContent = shown ? T.t("shownInHall") : calledOn ? T.t("speakNow") : "";
  }

  async function setCamera(enabled) {
    cameraOn = enabled;
    try {
      const publication = await room.localParticipant.setCameraEnabled(enabled);
      const track = publication?.track;
      if (enabled && track) track.attach($("self"));
      $("self").classList.toggle("hidden", !enabled);
    } catch {
      cameraOn = false;
      if (enabled) {
        cameraRefused = true;
        banner(T.t("cameraBlocked"), true);
      }
      $("self").classList.add("hidden");
    }
    render();
  }

  $("camera").addEventListener("click", () => setCamera(!cameraOn));

  async function setMicrophone(enabled) {
    micOn = enabled;
    try {
      await room.localParticipant.setMicrophoneEnabled(enabled);
    } catch {
      micOn = false;
      micRefused = true;
      banner(T.t("micBlocked"), true);
    }
    render();
  }

  $("mic").addEventListener("click", () => setMicrophone(!micOn));

  function showHand(up) {
    handUp = up;
    $("hand").textContent = T.t(up ? "lowerHand" : "raiseHand");
    $("hand").classList.toggle("up", up);
  }

  $("hand").addEventListener("click", async () => {
    showHand(!handUp);
    if (handUp) {
      // Ask for the microphone now, on a tap, so it can switch on by itself when called on.
      navigator.mediaDevices?.getUserMedia({ audio: true })
        .then((stream) => stream.getTracks().forEach((track) => track.stop()))
        .catch(() => banner(T.t("micBlocked"), true));
    }
    await room?.localParticipant.setAttributes({ hand: handUp ? String(Date.now()) : "" }).catch(() => {});
  });

  // ── Staying alive on phones ──────────────────────────────────────────────
  async function keepAlive() {
    if ("mediaSession" in navigator) {
      navigator.mediaSession.metadata = new MediaMetadata({ title: $("title").textContent });
      navigator.mediaSession.playbackState = "playing";
    }
    try {
      if ("wakeLock" in navigator && document.visibilityState === "visible" && !wakeLock) {
        wakeLock = await navigator.wakeLock.request("screen");
        wakeLock.addEventListener("release", () => { wakeLock = null; });
      }
    } catch { /* not allowed now */ }
  }
  function releaseWakeLock() {
    wakeLock?.release().catch(() => {});
    wakeLock = null;
  }

  async function framesDecoded() {
    try {
      const report = await videoTrack?.getRTCStatsReport();
      let frames = null;
      report?.forEach((stat) => { if (stat.type === "inbound-rtp" && stat.kind === "video") frames = stat.framesDecoded; });
      return frames;
    } catch { return null; }
  }

  document.addEventListener("visibilitychange", async () => {
    if (document.visibilityState !== "visible" || !wantConnected || !room) return;
    keepAlive();
    const admitted = room.localParticipant.permissions?.canSubscribe;
    if (!admitted || !videoTrack) return;
    const before = await framesDecoded();
    setTimeout(async () => {
      const after = await framesDecoded();
      if (wantConnected && (after === null || after === before)) {
        // A frozen session: drop it; the Disconnected handler rejoins.
        await room?.disconnect().catch(() => {});
      }
    }, 3000);
  });

  // Clients are not always told about permission changes (spike S9): check again
  // regularly so the screen never stays on the lobby or misses a granted microphone.
  setInterval(() => { if (wantConnected && room) render(); }, 2000);

  loadMeeting();
})();
