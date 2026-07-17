import { ApiError, EventStream, RemoteApi, newCommandId } from "./api.js";
import { formatDuration } from "./format.js";
import { applyLocalization, initializeLocalization, t } from "./i18n.js";
import { setupPwa } from "./pwa.js";
import { Renderer, announce, showToast } from "./renderer.js";
import { initializeSetup, isSetupRoute } from "./setup.js";
import { Store, applyBootstrap } from "./state.js";

const api = new RemoteApi();
let publicSetup = null;
await initializeLocalization(async () => {
  publicSetup = await api.setup();
  return publicSetup;
});
const store = new Store();
const renderer = new Renderer(store);
let lastNonZeroVolume = 100;
let recoveryPromise = null;
let websocketBaseline = null;
let websocketBaselineTimer = 0;

store.subscribe((state, previous) => renderer.render(state, previous));

const eventStream = new EventStream({
  onEvent: handleRemoteEvent,
  onStatus: handleConnectionStatus,
});

let setupPage = null;
const pwa = setupPwa({
  onInstallAvailable: (available) => {
    document.getElementById("install-button").hidden = !available;
    setupPage?.setInstallAvailable(available);
  },
  onInstalled: () => {
    setupPage?.markInstalled();
    showToast(t("notification.installed"), "success");
  },
  onError: () => {
    // Installation support is progressive; remote control remains fully available.
  },
});
setupPage = initializeSetup({ payload: publicSetup, pwa });

if (isSetupRoute()) {
  setupPage.show();
} else {
  bindStaticEvents();
  recover({ initial: true });
}

function bindStaticEvents() {
  document.getElementById("login-form").addEventListener("submit", handleLogin);
  document.getElementById("toggle-password").addEventListener("click", togglePassword);
  document.getElementById("logout-button").addEventListener("click", handleLogout);
  document.getElementById("install-button").addEventListener("click", () => pwa.install());
  document.getElementById("retry-connection").addEventListener("click", () => recover());
  document.getElementById("refresh-library").addEventListener("click", () => recover());
  document.getElementById("mobile-back").addEventListener("click", () => {
    store.update({ mobileView: "list" });
    window.setTimeout(focusSelectedCollection, 0);
  });

  document.querySelector(".tab-list").addEventListener("click", (event) => {
    const tab = event.target.closest("[data-tab]")?.dataset.tab;
    if (tab) selectTab(tab);
  });
  document.querySelector(".tab-list").addEventListener("keydown", handleTabKeydown);
  document.getElementById("main-content").addEventListener("click", handleWorkspaceAction);

  document.getElementById("play-pause-button").addEventListener("click", () => {
    sendTransport(store.state.playback.state === "playing" ? "pause" : "resume");
  });
  document.getElementById("previous-button").addEventListener("click", () => sendTransport("previous"));
  document.getElementById("next-button").addEventListener("click", () => sendTransport("next"));
  document.getElementById("stop-button").addEventListener("click", () => sendCommand({ type: "stop" }));

  const seek = document.getElementById("seek-control");
  seek.addEventListener("input", () => {
    document.getElementById("elapsed-time").textContent = formatDuration(Number(seek.value));
  });
  seek.addEventListener("change", () => {
    sendTransport("seek", { positionMs: Math.round(Number(seek.value)) });
  });

  const volume = document.getElementById("volume-slider");
  volume.addEventListener("input", () => {
    const value = Math.round(Number(volume.value));
    document.getElementById("volume-value").textContent = `${value}%`;
  });
  volume.addEventListener("change", () => setVolume(Number(volume.value)));
  document.getElementById("mute-button").addEventListener("click", () => {
    const current = Math.round(Number(store.state.playback.volume) || 0);
    if (current > 0) {
      lastNonZeroVolume = current;
      setVolume(0);
    } else {
      setVolume(lastNonZeroVolume);
    }
  });

  window.addEventListener("offline", () => store.update({ connection: "offline" }));
  window.addEventListener("online", () => recover());
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden && store.state.view === "app" && store.state.connection !== "online") {
      recover();
    }
  });
}

async function handleLogin(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const username = form.elements.username.value.trim();
  const password = form.elements.password.value;
  const errorNode = document.getElementById("login-error");
  errorNode.hidden = true;

  if (!username || !password) {
    errorNode.textContent = t("login.required");
    errorNode.hidden = false;
    (!username ? form.elements.username : form.elements.password).focus();
    return;
  }

  setLoginBusy(true);
  try {
    const bootstrap = await api.login(username, password);
    form.elements.password.value = "";
    await activateLoginSession(bootstrap);
  } catch (error) {
    const message = loginErrorMessage(error);
    errorNode.textContent = message;
    errorNode.hidden = false;
    form.elements.password.select();
  } finally {
    setLoginBusy(false);
  }
}

async function activateLoginSession(bootstrap) {
  const provisionalCatalog = {
    catalogRevision: bootstrap.catalogRevision ?? 0,
    collections: [],
  };
  applyLocalization(bootstrap.profile);
  applyBootstrap(store, { ...bootstrap, catalog: provisionalCatalog });
  store.update({ catalogStatus: "loading" });
  eventStream.connect({ immediate: true });
  announce(t("login.success"));

  try {
    const catalog = await api.catalog();
    if (catalog.catalogRevision >= store.state.catalog.catalogRevision) {
      store.update({ catalog, catalogStatus: "ready", catalogError: null });
    }
  } catch (error) {
    if (isUnauthorized(error)) {
      showLogin({ expired: true });
      return false;
    }
    if (store.state.catalogStatus !== "ready") {
      store.update({
        catalogStatus: "error",
        catalogError: error instanceof Error ? error.message : t("connection.failure"),
      });
      showToast(t("connection.libraryRecovery"), "info");
    }
  }
  return true;
}

function setLoginBusy(busy) {
  const submit = document.getElementById("login-submit");
  submit.disabled = busy;
  submit.querySelector(".button-label").textContent = t(
    busy ? "login.signingIn" : "login.signIn",
  );
  submit.querySelector(".button-spinner").hidden = !busy;
}

function loginErrorMessage(error) {
  if (!(error instanceof ApiError)) return t("login.failed");
  if (error.status === 401 || error.code === "invalid_credentials") {
    return t("login.invalidCredentials");
  }
  if (error.status === 429 || error.code === "rate_limited") {
    return t("login.rateLimited");
  }
  if (new Set(["network", "timeout", "invalid_response"]).has(error.code)) {
    return error.message;
  }
  return t("login.failed");
}

function togglePassword() {
  const input = document.getElementById("password");
  const button = document.getElementById("toggle-password");
  const show = input.type === "password";
  input.type = show ? "text" : "password";
  button.setAttribute("aria-pressed", String(show));
  button.setAttribute("aria-label", t(show ? "login.hidePassword" : "login.showPassword"));
  button
    .querySelector("use")
    ?.setAttribute("href", show ? "#icon-eye-off" : "#icon-eye");
  input.focus();
}

async function handleLogout() {
  document.getElementById("logout-button").disabled = true;
  eventStream.close();
  try {
    await api.logout();
  } catch (error) {
    if (!(error instanceof ApiError && error.status === 401)) {
      showToast(t("notification.localSessionEnded"), "info");
    }
  } finally {
    showLogin({ expired: false });
    document.getElementById("logout-button").disabled = false;
  }
}

async function recover({ initial = false } = {}) {
  if (recoveryPromise) return recoveryPromise;
  recoveryPromise = doRecover({ initial }).finally(() => {
    recoveryPromise = null;
  });
  return recoveryPromise;
}

async function doRecover({ initial }) {
  if (!navigator.onLine) {
    store.update({
      view: initial ? "login" : store.state.view,
      connection: "offline",
    });
    return false;
  }

  if (!initial) {
    store.update({ connection: "connecting", catalogStatus: "loading" });
  }
  try {
    const bootstrap = await api.bootstrap();
    const catalog = await api.catalog();
    applyLocalization(bootstrap.profile);
    applyBootstrap(store, { ...bootstrap, catalog });
    eventStream.connect({ immediate: true });
    return true;
  } catch (error) {
    if (isUnauthorized(error)) {
      showLogin({ expired: !initial });
      return false;
    }
    if (initial) {
      store.update({ view: "login", connection: "offline" });
      return false;
    }
    store.update({
      connection: navigator.onLine ? "reconnecting" : "offline",
      catalogStatus: store.state.catalog.collections.length ? "ready" : "error",
      catalogError: error instanceof Error ? error.message : t("connection.failure"),
    });
    return false;
  }
}

function showLogin({ expired, reason = "" }) {
  clearWebsocketBaseline();
  eventStream.close();
  api.clearSecrets();
  store.reset();
  store.update({ view: "login", connection: navigator.onLine ? "connecting" : "offline" });
  if (expired) {
    const message = sessionEndMessage(reason);
    const error = document.getElementById("login-error");
    error.textContent = message;
    error.hidden = false;
    announce(message);
  }
}

function sessionEndMessage(reason) {
  if (reason === "revoked_device") return t("session.disconnectedDevice");
  if (reason === "revoked_all") return t("session.disconnectedAll");
  if (reason === "signed_in_again") return t("session.signedInAgain");
  return t("login.sessionExpired");
}

function focusSelectedCollection() {
  const selectedId = store.state.selected[store.state.activeTab];
  const buttons = document.querySelectorAll('[data-action="select-collection"]');
  for (const button of buttons) {
    if (button.dataset.collectionId === selectedId) {
      button.focus();
      return;
    }
  }
  document.getElementById("library-panel").focus();
}

function selectTab(tab) {
  if (!new Set(["playlists", "meetings"]).has(tab)) return;
  store.update({ activeTab: tab, mobileView: "list" });
}

function handleTabKeydown(event) {
  if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
  event.preventDefault();
  const tabs = ["meetings", "playlists"];
  const current = tabs.indexOf(store.state.activeTab);
  let target = current;
  if (event.key === "ArrowRight") target = (current + 1) % tabs.length;
  if (event.key === "ArrowLeft") target = (current - 1 + tabs.length) % tabs.length;
  if (event.key === "Home") target = 0;
  if (event.key === "End") target = tabs.length - 1;
  selectTab(tabs[target]);
  document.getElementById(`${tabs[target]}-tab`).focus();
}

function handleWorkspaceAction(event) {
  const control = event.target.closest("[data-action]");
  if (!control) return;
  const { action } = control.dataset;
  if (action === "select-collection") {
    const tab = store.state.activeTab;
    store.update((state) => ({
      selected: { ...state.selected, [tab]: control.dataset.collectionId },
      mobileView: "detail",
    }));
    window.setTimeout(() => document.getElementById("tree-heading")?.focus(), 0);
  } else if (action === "play-media") {
    playMedia(control.dataset);
  } else if (action === "retry-catalog") {
    recover();
  }
}

function playMedia({ source, collectionId, nodeId }) {
  sendCommand({
    type: "play",
    catalogRevision: store.state.catalog.catalogRevision,
    origin: { source, collectionId, nodeId },
    startPaused: false,
  });
}

function sendTransport(type, fields = {}) {
  const playbackSessionId = store.state.playback.playbackSessionId;
  if (!playbackSessionId) return;
  sendCommand({ type, playbackSessionId, ...fields });
}

function setVolume(value) {
  const volume = Math.min(100, Math.max(0, Math.round(Number(value) || 0)));
  if (volume > 0) lastNonZeroVolume = volume;
  sendTransport("set_volume", { volume });
}

async function sendCommand(fields) {
  const { type } = fields;
  if (store.state.connection !== "online" || store.state.pendingCommands.has(type)) return;
  const command = { commandId: newCommandId(), ...fields };
  setCommandPending(type, true);
  try {
    const result = await postCommandWithAuthenticationRecovery(command);
    if (result === null) return;
    if (!result.ok) {
      const remoteError = result.error ?? {};
      throw new ApiError(remoteError.message || t("command.notCompleted"), {
        code: remoteError.code || "failed",
        retryable: Boolean(remoteError.retryable),
      });
    }
  } catch (error) {
    if (isUnauthorized(error)) {
      showLogin({ expired: true });
      return;
    }
    const code = error instanceof ApiError ? error.code : "failed";
    if (code === "catalog_stale" || code === "playback_stale") {
      showToast(t("command.stateChanged"), "info");
      await recover();
    } else if (code === "blocked") {
      showToast(t("command.protected"), "error");
    } else if (code === "unavailable" || code === "not_found") {
      showToast(t("command.unavailable"), "error");
      await recover();
    } else {
      showToast(t("command.sendFailed"), "error");
    }
  } finally {
    setCommandPending(type, false);
  }
}

async function postCommandWithAuthenticationRecovery(command) {
  try {
    return await api.command(command);
  } catch (error) {
    if (!(error instanceof ApiError) || error.status !== 403) throw error;
    const recovered = await recover();
    if (store.state.view !== "app") return null;
    if (!recovered) {
      throw new ApiError(t("command.sessionConfirmFailed"), {
        code: "network",
        retryable: true,
      });
    }
    return api.command(command);
  }
}

function setCommandPending(type, pending) {
  store.update((state) => {
    const commands = new Set(state.pendingCommands);
    if (pending) commands.add(type);
    else commands.delete(type);
    return { pendingCommands: commands };
  });
}

function handleRemoteEvent(message) {
  if (!message || typeof message !== "object" || typeof message.type !== "string") {
    return;
  }
  if (websocketBaseline) {
    applyWebsocketBaselineEvent(message);
    return;
  }
  const state = store.state;
  if (message.serverInstanceId && state.serverInstanceId && message.serverInstanceId !== state.serverInstanceId) {
    recover();
    return;
  }
  if (Number.isInteger(message.sequence) && Number.isInteger(state.eventSequence)) {
    if (message.sequence > state.eventSequence + 1) {
      recover();
      return;
    }
    if (message.sequence < state.eventSequence) {
      return;
    }
  }
  if (Number.isInteger(message.sequence) && message.sequence !== state.eventSequence) {
    store.update({ eventSequence: message.sequence });
  }

  if (message.type === "catalog.snapshot") {
    const catalog = message.payload;
    if (catalog?.catalogRevision >= state.catalog.catalogRevision) {
      store.update({ catalog, catalogStatus: "ready", catalogError: null });
    }
  } else if (message.type === "playback.snapshot") {
    const playback = message.payload;
    if (playback?.playbackRevision >= state.playback.playbackRevision) {
      store.update({ playback });
    }
  } else if (message.type === "profile.snapshot") {
    const profile = message.payload ?? {};
    applyLocalization(profile);
    store.update((current) => ({
      profile: {
        displayName: profile.name || current.profile.displayName,
        locale: profile.locale || current.profile.locale,
        theme: profile.theme === "light" ? "light" : "dark",
      },
    }));
  } else if (
    message.type === "heartbeat" &&
    message.currentMeetingWeekStart &&
    message.currentMeetingWeekStart !== state.currentMeetingWeekStart
  ) {
    store.update({ currentMeetingWeekStart: message.currentMeetingWeekStart });
  } else if (message.type === "session.revoked") {
    showLogin({ expired: true, reason: message.payload?.reason });
  }
}

function applyWebsocketBaselineEvent(message) {
  if (message.type === "session.revoked") {
    showLogin({ expired: true, reason: message.payload?.reason });
    return;
  }
  if (!new Set(["catalog.snapshot", "playback.snapshot", "profile.snapshot"]).has(message.type)) return;
  const baseline = websocketBaseline;
  if (!baseline) return;
  if (
    message.serverInstanceId &&
    store.state.serverInstanceId &&
    message.serverInstanceId !== store.state.serverInstanceId
  ) {
    clearWebsocketBaseline();
    recover();
    return;
  }
  baseline.serverInstanceId = message.serverInstanceId || baseline.serverInstanceId;
  if (Number.isInteger(message.sequence)) {
    baseline.sequence = Math.max(baseline.sequence ?? message.sequence, message.sequence);
  }
  if (message.type === "catalog.snapshot") baseline.catalog = message.payload;
  else if (message.type === "playback.snapshot") baseline.playback = message.payload;
  else baseline.profile = message.payload;

  if (!baseline.catalog || !baseline.playback || !baseline.profile) return;
  window.clearTimeout(websocketBaselineTimer);
  websocketBaseline = null;
  applyLocalization(baseline.profile);
  store.update({
    profile: {
      displayName: baseline.profile.name || store.state.profile.displayName,
      locale: baseline.profile.locale || store.state.profile.locale,
      theme: baseline.profile.theme === "light" ? "light" : "dark",
    },
    catalog: baseline.catalog,
    playback: baseline.playback,
    eventSequence: baseline.sequence,
    connection: "online",
    catalogStatus: "ready",
    catalogError: null,
  });
}

function beginWebsocketBaseline() {
  clearWebsocketBaseline();
  websocketBaseline = {
    serverInstanceId: null,
    sequence: null,
    catalog: null,
    playback: null,
    profile: null,
  };
  websocketBaselineTimer = window.setTimeout(() => {
    clearWebsocketBaseline();
    recover();
  }, 10_000);
}

function clearWebsocketBaseline() {
  window.clearTimeout(websocketBaselineTimer);
  websocketBaselineTimer = 0;
  websocketBaseline = null;
}

function handleConnectionStatus(status) {
  if (status === "unauthorized") {
    showLogin({ expired: true });
    return;
  }
  if (status === "authentication-check") {
    clearWebsocketBaseline();
    recover();
    return;
  }
  if (status === "syncing") {
    beginWebsocketBaseline();
  } else if (status === "reconnecting" || status === "offline") {
    clearWebsocketBaseline();
  }
  if (store.state.view === "app") {
    store.update({ connection: status });
  }
}

function isUnauthorized(error) {
  return error instanceof ApiError && (error.status === 401 || error.code === "unauthorized");
}
