import {
  ApiError,
  EventStream,
  RemoteApi,
  newUuid,
} from "../../shared/scripts/api.js";
import { announce, byId, setUiIcon, showToast } from "../../shared/scripts/dom.js";
import {
  applyLocalization,
  initializeLocalization,
  t,
} from "../../shared/scripts/i18n.js";
import { setupPwa } from "../../shared/scripts/pwa.js";
import { initializeSetup, isSetupRoute } from "../../shared/scripts/setup.js";
import { Renderer } from "./renderer.js";
import { ScheduleTimingEditor } from "./schedule-editor.js";
import { ScheduleOrganizer } from "./schedule-organizer.js";
import { ScheduleWorkspace } from "./schedule-workspace.js";
import {
  Store,
  applyAuthenticatedBootstrap,
  applyHeartbeatClock,
  applyPublicBootstrap,
  normalizeLiveSnapshot,
  normalizeProfile,
  normalizeScheduleSnapshot,
  nextTimerPart,
  selectedTimerPart,
  timerContextChange,
} from "./state.js";
import { ScreenWakeLock } from "./wake-lock.js";

const PUBLIC_WS_PATH = "/remote/timer/api/ws/public";
const AUTH_WS_PATH = "/remote/timer/api/ws";
const HUD_AUTOHIDE_MS = 4_000;
const LIVE_TICK_INTERVAL_MS = 250;

const SCHEDULE_BOUND_COMMANDS = new Set([
  "start_part",
  "reset_part",
  "adjust_part",
  "edit_part",
  "set_section_count",
  "set_pacing_mode",
  "create_custom_schedule",
  "select_custom_schedule",
  "select_meeting_schedule",
]);
const inflightCommandKeys = new Map();

let api = new RemoteApi("/remote/timer/api");
let initialSetupPayload = null;

await initializeLocalization({
  sourceCatalogUrl: new URL("../messages.en.json", import.meta.url),
  loadRemote: isSetupRoute()
    ? async () => {
        initialSetupPayload = await api.setup();
        return initialSetupPayload;
      }
    : undefined,
});

const store = new Store();
const renderer = new Renderer();
const wakeLock = new ScreenWakeLock();
const organizer = new ScheduleOrganizer({
  store,
  save: (fields) => sendTimerCommand(
    { ...fields, type: "replace_custom_schedule_entries" }, { inline: true, sync: true }),
});
const editor = new ScheduleTimingEditor({
  saveTiming: ({ timing, ...context }) => sendTimerCommand(
    { ...context, ...timing, type: "replace_custom_timing_settings" }, { inline: true, sync: true }),
});
const workspace = new ScheduleWorkspace({
  store,
  send: (command, options) => sendTimerCommand(command, { ...options, sync: true }),
  loadCatalog: loadCustomCatalog,
  editor,
  organizer,
});

let recoveryPromise = null;
let websocketSyncTimer = 0;
let hudHideTimer = 0;

const eventStream = new EventStream({
  wsPath: PUBLIC_WS_PATH,
  onEvent: handleRemoteEvent,
  onStatus: handleConnectionStatus,
});

let setupPage = null;
const pwa = setupPwa({
  scope: "/remote/timer/",
  onInstallAvailable: (available) => {
    byId("stage-install-button").hidden = !available;
    byId("control-install-button").hidden = !available;
    setupPage?.setInstallAvailable(available);
  },
  onInstalled: () => {
    setupPage?.markInstalled();
    showToast(t("notification.installed"), "success");
  },
  onError: () => {
    // Progressive PWA install; timer display and remote control stay functional.
  },
});

setupPage = initializeSetup({
  payload: initialSetupPayload,
  pwa,
  hideOtherViews: () => {
    store.update({ view: "setup", loginModalOpen: false });
  },
});

store.subscribe((state, previous) => {
  renderer.render(state, previous);
  const keepAwake =
    state.view === "stage" ||
    (state.view === "control" && state.live.active && state.live.state === "running");
  wakeLock.setDesired(keepAwake);
  workspace.sync(state, previous);
  organizer.sync(state, previous);
});

if (isSetupRoute()) {
  setupPage.show(initialSetupPayload);
} else {
  bindStaticEvents();
  startLiveTicker();
  armStageHudAutohide();
  void recover({ initial: true });
}

function startLiveTicker() {
  window.setInterval(() => {
    if (document.hidden) return;
    renderer.renderLiveFrame(store.state, performance.now());
  }, LIVE_TICK_INTERVAL_MS);
}

function bindStaticEvents() {
  const stageView = byId("stage-view");
  const stageHud = byId("stage-hud");

  stageView.addEventListener("click", (event) => {
    if (stageHud.contains(event.target)) {
      armStageHudAutohide();
      return;
    }
    if (store.state.hudVisible) {
      clearStageHudAutohide();
      store.update({ hudVisible: false });
    } else {
      revealStageHud();
    }
  });

  stageView.addEventListener("pointermove", (event) => {
    if (event.pointerType === "mouse") {
      revealStageHud();
    }
  });

  stageHud.addEventListener("focusin", () => {
    clearStageHudAutohide();
    if (!store.state.hudVisible) {
      store.update({ hudVisible: true });
    }
  });

  stageHud.addEventListener("focusout", (event) => {
    if (!stageHud.contains(event.relatedTarget)) {
      armStageHudAutohide();
    }
  });

  byId("stage-signin-button").addEventListener("click", () => {
    if (!store.state.controlAvailable) return;
    clearStageHudAutohide();
    byId("login-error").hidden = true;
    store.update({ loginModalOpen: true, hudVisible: true });
  });

  byId("stage-control-button").addEventListener("click", () => {
    enterControlWorkspace();
  });

  byId("stage-install-button").addEventListener("click", () => pwa.install());
  byId("control-install-button").addEventListener("click", () => pwa.install());

  byId("stage-fullscreen-button").addEventListener("click", () => {
    void toggleBrowserFullscreen();
  });

  for (const eventName of ["fullscreenchange", "webkitfullscreenchange"]) {
    document.addEventListener(eventName, () => {
      renderer.invalidateStage();
      renderer.render(store.state, store.state);
    });
  }

  byId("control-stage-button").addEventListener("click", enterStageView);
  byId("sticky-expand-button").addEventListener("click", () => enterStageView());
  byId("sticky-scroll-top").addEventListener("click", () => scrollWorkspaceToTop());

  byId("login-close").addEventListener("click", () => closeLoginModal());
  byId("login-modal").addEventListener("click", (event) => {
    if (event.target === event.currentTarget) {
      closeLoginModal();
    }
  });

  byId("login-form").addEventListener("submit", handleLogin);
  byId("toggle-password").addEventListener("click", togglePasswordVisibility);
  byId("logout-button").addEventListener("click", handleLogout);
  byId("retry-connection").addEventListener("click", () => void recover());

  const nudgeActivePart = (deltaSeconds) => {
    const partId = store.state.live.activePartId;
    if (partId) {
      void sendTimerCommand({ type: "adjust_part", partId, deltaSeconds });
    }
  };
  const stopActivePart = () => {
    void sendTimerCommand({
      type: "stop_part",
      partId: store.state.live.activePartId,
    });
  };

  byId("hero-minus-second").addEventListener("click", () => nudgeActivePart(-1));
  byId("hero-plus-second").addEventListener("click", () => nudgeActivePart(1));
  byId("hero-stop-button").addEventListener("click", stopActivePart);

  byId("sticky-minus-second").addEventListener("click", () => nudgeActivePart(-1));
  byId("sticky-plus-second").addEventListener("click", () => nudgeActivePart(1));
  byId("sticky-stop-button").addEventListener("click", stopActivePart);

  bindTimerWorkspace();
  setupStickyHeroObserver();

  byId("meeting-midweek").addEventListener("click", () => {
    if (store.state.schedule.meetingType !== "midweek") {
      void sendTimerCommand({ type: "set_meeting_type", meetingType: "midweek" });
    }
  });

  byId("meeting-weekend").addEventListener("click", () => {
    if (store.state.schedule.meetingType !== "weekend") {
      void sendTimerCommand({ type: "set_meeting_type", meetingType: "weekend" });
    }
  });

  byId("week-prev").addEventListener("click", () => {
    void sendTimerCommand({ type: "navigate_week", target: "previous" });
  });

  byId("week-next").addEventListener("click", () => {
    void sendTimerCommand({ type: "navigate_week", target: "next" });
  });

  byId("week-current").addEventListener("click", () => {
    void sendTimerCommand({ type: "navigate_week", target: "current" });
  });

  byId("schedule-list").addEventListener("click", handleScheduleClick);

  window.addEventListener("keydown", handleGlobalKeydown);
  window.addEventListener("offline", () => handleConnectionStatus("offline"));
  window.addEventListener("online", () => void recover());
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) {
      renderer.renderLiveFrame(store.state, performance.now());
      if (store.state.connection !== "online") {
        void recover();
      }
    }
  });
}

function activeModalContainer() {
  if (store.state.loginModalOpen && store.state.controlAvailable) {
    return byId("login-modal");
  }
  return document.querySelector('#stage-hand-alert-card[aria-modal="true"]');
}

function handleGlobalKeydown(event) {
  if (document.querySelector("dialog[open]")) return;
  const modal = activeModalContainer();
  if (event.key === "Tab") {
    if (modal && !modal.hidden) {
      const focusable = Array.from(
        modal.querySelectorAll(
          'button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), a[href], [tabindex]:not([tabindex="-1"])',
        ),
      ).filter((element) => !element.hidden && element.offsetParent !== null);
      if (focusable.length > 0) {
        const first = focusable[0];
        const last = focusable[focusable.length - 1];
        if (event.shiftKey && (document.activeElement === first || !modal.contains(document.activeElement))) {
          event.preventDefault();
          last.focus();
        } else if (!event.shiftKey && (document.activeElement === last || !modal.contains(document.activeElement))) {
          event.preventDefault();
          first.focus();
        }
      }
      return;
    }
  }

  if (event.key === "Escape") {
    if (store.state.loginModalOpen) {
      event.preventDefault();
      closeLoginModal();
      return;
    }
    if (store.state.view === "stage" && !store.state.hudVisible) {
      revealStageHud();
    }
    return;
  }

  if (
    store.state.view === "stage" &&
    !modal &&
    !store.state.hudVisible &&
    (event.key === " " || event.key === "Enter")
  ) {
    const tag = document.activeElement?.tagName ?? "";
    if (tag !== "BUTTON" && tag !== "A" && tag !== "INPUT") {
      event.preventDefault();
      revealStageHud();
    }
  }
}

function revealStageHud() {
  if (!store.state.hudVisible) {
    store.update({ hudVisible: true });
  }
  armStageHudAutohide();
}

function armStageHudAutohide() {
  clearStageHudAutohide();
  hudHideTimer = window.setTimeout(() => {
    const hud = byId("stage-hud");
    if (
      store.state.view === "stage" &&
      !store.state.loginModalOpen &&
      !hud.contains(document.activeElement)
    ) {
      store.update({ hudVisible: false });
    }
  }, HUD_AUTOHIDE_MS);
}

function clearStageHudAutohide() {
  window.clearTimeout(hudHideTimer);
  hudHideTimer = 0;
}

function enterStageView() {
  store.update({
    view: "stage",
    hudVisible: true,
    heroStuck: false,

  });
  renderer.invalidateStage();
  byId("stage-view").focus({ preventScroll: true });
  armStageHudAutohide();
}

function enterControlWorkspace() {
  if (!store.state.authenticated || !store.state.controlAvailable) return;
  clearStageHudAutohide();
  store.update({ view: "control", loginModalOpen: false });
  byId("control-stage-button").focus({ preventScroll: true });
  window.requestAnimationFrame(syncStickyHeroState);
}

let heroIntersecting = true;

function setupStickyHeroObserver() {
  const workspace = byId("main-content");
  const heroCard = byId("hero-card");

  if (typeof IntersectionObserver === "function") {
    const observer = new IntersectionObserver(
      (entries) => {
        for (const entry of entries) {
          if (entry.target === heroCard) {
            heroIntersecting = entry.isIntersecting;
          }
        }
        syncStickyHeroState();
      },
      {
        root: workspace,
        threshold: 0.08,
      },
    );
    observer.observe(heroCard);
  }

  let scrollTicking = false;
  workspace.addEventListener(
    "scroll",
    () => {
      if (scrollTicking) return;
      scrollTicking = true;
      window.requestAnimationFrame(() => {
        scrollTicking = false;
        syncStickyHeroState();
      });
    },
    { passive: true },
  );
}

function syncStickyHeroState() {
  const workspace = byId("main-content");
  const heroCard = byId("hero-card");
  if (store.state.view !== "control") {
    if (store.state.heroStuck) {
      store.update({ heroStuck: false });
    }
    return;
  }
  const thresholdPx = Math.max(48, heroCard.offsetHeight * 0.72);
  const scrolledPast =
    typeof IntersectionObserver === "function"
      ? !heroIntersecting && workspace.scrollTop > 40
      : workspace.scrollTop > thresholdPx;
  if (scrolledPast !== store.state.heroStuck) {
    store.update({ heroStuck: scrolledPast });
  }
}

function scrollWorkspaceToTop() {
  const workspace = byId("main-content");
  const prefersReduced = window.matchMedia?.(
    "(prefers-reduced-motion: reduce)",
  ).matches;
  workspace.scrollTo({
    top: 0,
    behavior: prefersReduced ? "auto" : "smooth",
  });
}

function closeLoginModal() {
  store.update({ loginModalOpen: false });
  armStageHudAutohide();
  window.setTimeout(() => byId("stage-signin-button")?.focus(), 0);
}

async function toggleBrowserFullscreen() {
  try {
    const activeElement =
      document.fullscreenElement || document.webkitFullscreenElement;
    if (activeElement) {
      if (document.exitFullscreen) {
        await document.exitFullscreen();
      } else if (document.webkitExitFullscreen) {
        document.webkitExitFullscreen();
      }
    } else {
      const root = document.documentElement;
      if (root.requestFullscreen) {
        await root.requestFullscreen();
      } else if (root.webkitRequestFullscreen) {
        root.webkitRequestFullscreen();
      }
    }
  } catch {
    // Fullscreen requests can be declined by browser policy; stage view still fills viewport.
  }
}

function togglePasswordVisibility() {
  const input = byId("password");
  const button = byId("toggle-password");
  const visible = input.type === "text";
  input.type = visible ? "password" : "text";
  button.setAttribute("aria-pressed", String(!visible));
  const label = visible ? t("login.showPassword") : t("login.hidePassword");
  button.setAttribute("aria-label", label);
  setUiIcon("toggle-password", visible ? "eye" : "eye-off");
}

function setLoginBusy(busy) {
  const button = byId("login-submit");
  button.disabled = busy;
  button.querySelector(".button-label").textContent = busy
    ? t("login.signingIn")
    : t("login.signIn");
  button.querySelector(".button-spinner").hidden = !busy;
}

async function handleLogin(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const username = form.elements.username.value.trim();
  const password = form.elements.password.value;
  const errorNode = byId("login-error");
  errorNode.hidden = true;

  if (!username || !password) {
    errorNode.textContent = t("login.required");
    errorNode.hidden = false;
    (!username ? form.elements.username : form.elements.password).focus();
    return;
  }

  setLoginBusy(true);
  const loginApi = api;
  try {
    const loginPayload = await loginApi.login(username, password);
    if (loginApi !== api) return;
    form.elements.password.value = "";
    applyLocalization(loginPayload.profile);
    applyAuthenticatedBootstrap(store, loginPayload, { targetView: "control" });
    connectWebSocket(AUTH_WS_PATH);
    showToast(t("login.success"), "success");
  } catch (error) {
    if (loginApi !== api) return;
    errorNode.textContent = loginErrorMessage(error);
    errorNode.hidden = false;
    announce(errorNode.textContent);
  } finally {
    setLoginBusy(false);
  }
}

function loginErrorMessage(error) {
  if (error instanceof ApiError) {
    if (error.status === 401) return t("login.invalidCredentials");
    if (error.status === 429) return t("login.rateLimited");
    if (error.status === 403) return t("api.forbidden");
    return error.message;
  }
  return t("login.failed");
}

async function handleLogout() {
  const logoutBtn = byId("logout-button");
  logoutBtn.disabled = true;
  eventStream.close();
  try {
    await api.logout();
  } catch {
    // Local downgrade still proceeds cleanly if the session already expired.
  } finally {
    logoutBtn.disabled = false;
    downgradeToPublicStage({ reason: "local_logout", notify: true });
  }
}

function downgradeToPublicStage({ reason = "", notify = false } = {}) {
  clearWebsocketSync();
  eventStream.close();
  api.clearSecrets();
  inflightCommandKeys.clear();
  // Requests started before the downgrade must not restore the old session.
  api = new RemoteApi(api.apiRoot);
  store.update((current) => ({
    view: "stage",
    authenticated: false,
    loginModalOpen: false,

    hudVisible: true,
    live: { ...current.live, handAlert: null },
    schedule: normalizeScheduleSnapshot(null),
    scheduleRevision: 0,
    selectedPartId: "",
    adjustOpen: false,
    browsingSchedules: false,
    catalog: { status: "idle", schedules: [] },
  }));
  armStageHudAutohide();
  connectWebSocket(PUBLIC_WS_PATH);

  if (notify) {
    const message = sessionEndMessage(reason);
    showToast(message, reason === "local_logout" ? "info" : "error");
    announce(message);
  }
}

function sessionEndMessage(reason) {
  if (reason === "local_logout") return t("notification.localSessionEnded");
  if (reason === "revoked_device") return t("session.disconnectedDevice");
  if (reason === "revoked_all") return t("session.disconnectedAll");
  if (reason === "signed_in_again") return t("session.signedInAgain");
  return t("login.sessionExpired");
}

function connectWebSocket(targetPath) {
  if (eventStream.wsPath !== targetPath) {
    eventStream.close();
    eventStream.setWsPath(targetPath);
  }
  eventStream.connect({ immediate: true });
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
    store.update((current) => ({
      view: current.view === "boot" ? "stage" : current.view,
      ...connectionPatch(current, "offline"),
    }));
    return false;
  }

  if (!initial) {
    store.update({ connection: "connecting" });
  }

  const recoveryApi = api;
  try {
    const publicPayload = await recoveryApi.publicBootstrap();
    if (recoveryApi !== api || !navigator.onLine) return false;
    if (store.state.authenticated && !(publicPayload.authenticated && publicPayload.controlAvailable)) {
      downgradeToPublicStage({ reason: "expired", notify: true });
      return true;
    }
    applyLocalization(publicPayload.profile);
    setupPage?.setInstallAvailable(pwa.isInstallAvailable());
    applyPublicBootstrap(store, publicPayload, { preserveView: !initial });

    if (publicPayload.authenticated && publicPayload.controlAvailable) {
      try {
        const authPayload = await recoveryApi.bootstrap();
        if (recoveryApi !== api || !navigator.onLine) return false;
        applyLocalization(authPayload.profile);
        const targetView = initial ? "stage" : store.state.view;
        applyAuthenticatedBootstrap(store, authPayload, { targetView });
        connectWebSocket(AUTH_WS_PATH);
        return true;
      } catch (authError) {
        if (recoveryApi !== api) return false;
        if (!(authError instanceof ApiError && authError.status === 401)) {
          throw authError;
        }
        downgradeToPublicStage({ reason: "expired", notify: true });
        return true;
      }
    }

    connectWebSocket(PUBLIC_WS_PATH);
    return true;
  } catch (error) {
    if (recoveryApi !== api) return false;
    store.update((current) => ({
      view: current.view === "boot" ? "stage" : current.view,
      ...connectionPatch(current, navigator.onLine ? "reconnecting" : "offline"),
    }));
    if (initial) {
      connectWebSocket(PUBLIC_WS_PATH);
    } else if (error instanceof Error && error.message) {
      announce(error.message);
    } else {
      announce(t("connection.failure"));
    }
    return false;
  }
}

function handleConnectionStatus(status) {
  if (status === "unauthorized") {
    if (store.state.authenticated) {
      downgradeToPublicStage({ reason: "expired", notify: true });
    } else {
      connectWebSocket(PUBLIC_WS_PATH);
    }
    return;
  }
  if (status === "authentication-check") {
    scheduleWebsocketSync();
    return;
  }
  store.update((current) => connectionPatch(current, status));
}

function connectionPatch(current, connection) {
  if (connection === "offline" || connection === "reconnecting") {
    return { connection, live: { ...current.live, handAlert: null } };
  }
  return { connection };
}

function scheduleWebsocketSync() {
  if (websocketSyncTimer) return;
  websocketSyncTimer = window.setTimeout(() => {
    websocketSyncTimer = 0;
    void recover();
  }, 180);
}

function clearWebsocketSync() {
  window.clearTimeout(websocketSyncTimer);
  websocketSyncTimer = 0;
}

function handleRemoteEvent(event) {
  if (!event || typeof event !== "object") return;

  if (event.type === "session.revoked") {
    downgradeToPublicStage({
      reason: String(event.payload?.reason ?? ""),
      notify: true,
    });
    return;
  }

  if (!navigator.onLine) return;

  const nextInstanceId = String(event.serverInstanceId ?? "");
  if (
    nextInstanceId &&
    store.state.serverInstanceId &&
    nextInstanceId !== store.state.serverInstanceId
  ) {
    scheduleWebsocketSync();
    return;
  }

  const nextSequence = Number(event.sequence);
  if (event.type === "public.snapshot") {
    const live = normalizeLiveSnapshot(event.payload?.live);
    const profile = normalizeProfile(event.payload?.profile);
    applyLocalization(event.payload?.profile);
    store.update((current) => ({
      connection: "online",
      serverInstanceId: nextInstanceId || current.serverInstanceId,
      eventSequence: Number.isFinite(nextSequence) ? nextSequence : current.eventSequence,
      controlAvailable: Boolean(
        event.payload?.controlAvailable ?? current.controlAvailable,
      ),
      liveRevision: live.liveRevision,
      liveAnchorMonotonicMs: performance.now(),
      profile,
      live,
    }));
    return;
  }

  if (event.type === "session.snapshot") {
    if (!store.state.authenticated) return;
    const live = normalizeLiveSnapshot(event.payload?.live);
    const schedule = normalizeScheduleSnapshot(event.payload?.schedule);
    const profile = normalizeProfile(event.payload?.profile);
    applyLocalization(event.payload?.profile);
    store.update((current) => ({
      connection: "online",
      serverInstanceId: nextInstanceId || current.serverInstanceId,
      eventSequence: Number.isFinite(nextSequence) ? nextSequence : current.eventSequence,
      controlAvailable: Boolean(
        event.payload?.controlAvailable ?? current.controlAvailable,
      ),
      liveRevision: live.liveRevision,
      scheduleRevision: schedule.scheduleRevision,
      liveAnchorMonotonicMs: performance.now(),

      ...timerContextChange(current, schedule),
      profile,
      live,
      schedule,
    }));
    return;
  }

  if (Number.isFinite(nextSequence) && store.state.eventSequence > 0) {
    if (nextSequence > store.state.eventSequence + 1) {
      scheduleWebsocketSync();
      return;
    }
    if (nextSequence < store.state.eventSequence) {
      return;
    }
  }

  if (event.type === "heartbeat") {
    if (Number.isFinite(nextSequence)) {
      store.update({ eventSequence: nextSequence, connection: "online" });
    }
    applyHeartbeatClock(store, event);
    return;
  }

  if (event.type === "timer.live") {
    const live = normalizeLiveSnapshot(event.payload);
    store.update((current) => {
      if (live.liveRevision < current.liveRevision) {
        return {
          connection: "online",
          eventSequence: Number.isFinite(nextSequence) ? nextSequence : current.eventSequence,
        };
      }
      return {
        connection: "online",
        eventSequence: Number.isFinite(nextSequence) ? nextSequence : current.eventSequence,
        liveRevision: live.liveRevision,
        liveAnchorMonotonicMs: performance.now(),
        adjustOpen: live.state === "running" && live.activePartId === current.live.activePartId && current.adjustOpen,
        selectedPartId: live.state === "running" ? live.activePartId : current.selectedPartId,
        live,
      };
    });
    return;
  }

  if (event.type === "timer.schedule") {
    if (!store.state.authenticated) return;
    const schedule = normalizeScheduleSnapshot(event.payload);
    store.update((current) => {
      if (schedule.scheduleRevision < current.scheduleRevision) {
        return {
          connection: "online",
          eventSequence: Number.isFinite(nextSequence) ? nextSequence : current.eventSequence,
        };
      }
      return {
        connection: "online",
        eventSequence: Number.isFinite(nextSequence) ? nextSequence : current.eventSequence,
        scheduleRevision: schedule.scheduleRevision,

        ...timerContextChange(current, schedule),
        schedule,
      };
    });
    return;
  }

  if (event.type === "profile.snapshot") {
    applyLocalization(event.payload);
    const profile = normalizeProfile(event.payload);
    const controlAvailable = Boolean(
      event.payload?.controlAvailable ?? store.state.controlAvailable,
    );
    if (!controlAvailable && store.state.authenticated) {
      store.update({ profile, controlAvailable: false });
      downgradeToPublicStage({ reason: "revoked_all", notify: true });
      return;
    }
    store.update({
      profile,
      controlAvailable,
      eventSequence: Number.isFinite(nextSequence)
        ? nextSequence
        : store.state.eventSequence,
    });
  }
}


function bindTimerWorkspace() {
  const selected = () => selectedTimerPart(store.state);
  const startSelected = () => {
    const part = selected();
    if (part) void sendTimerCommand({ type: "start_part", partId: part.id });
  };
  byId("hero-start-button").addEventListener("click", startSelected);
  byId("sticky-start-button").addEventListener("click", startSelected);
  byId("hero-reset-button").addEventListener("click", () => {
    const part = selected();
    if (part) void sendTimerCommand({ type: "reset_part", partId: part.id });
  });
  byId("hero-edit-button").addEventListener("click", () => {
    const part = selected();
    if (part) workspace.editPart(part);
  });
  byId("hero-adjust-button").addEventListener("click", () => {
    store.update((state) => ({ adjustOpen: !state.adjustOpen }));
  });
  byId("hero-next-button").addEventListener("click", () => {
    const next = nextTimerPart(store.state);
    if (next) void sendTimerCommand({ type: "start_part", partId: next.id });
  });
}

async function loadCustomCatalog() {
  const catalogApi = api;
  try {
    const catalog = await catalogApi.get("/custom-schedules");
    if (catalogApi !== api || !store.state.authenticated || !Array.isArray(catalog?.schedules)) {
      throw new Error(t("workspace.catalogFailed"));
    }
    return catalog.schedules;
  } catch (error) {
    if (catalogApi === api && error instanceof ApiError && error.status === 401) {
      downgradeToPublicStage({ reason: "expired", notify: true });
    }
    throw error;
  }
}

function handleScheduleClick(event) {
  const button = event.target.closest("button[data-action]");
  if (!button || button.disabled || store.state.live.state === "running") return;
  const { action, partId, section, count } = button.dataset;
  if (action === "select-part") {
    store.update({ selectedPartId: partId, adjustOpen: false });
  } else if (action === "section-count") {
    void sendTimerCommand({ type: "set_section_count", section, count: Number(count) });
  }
}

async function refreshScheduleOnly() {
  const scheduleApi = api;
  const { serverInstanceId, profile } = store.state;
  try {
    const schedulePayload = await scheduleApi.schedule();
    if (scheduleApi !== api || !store.state.authenticated || serverInstanceId !== store.state.serverInstanceId ||
        profile.id !== store.state.profile.id) return;
    const schedule = normalizeScheduleSnapshot(schedulePayload);
    store.update((current) => {
      if (schedule.scheduleRevision < current.scheduleRevision) {
        return {};
      }
      return {
        scheduleRevision: schedule.scheduleRevision,

        ...timerContextChange(current, schedule),
        schedule,
      };
    });
  } catch (error) {
    if (scheduleApi === api && error instanceof ApiError && error.status === 401) {
      downgradeToPublicStage({ reason: "expired", notify: true });
    }
  }
}

function commandDedupeKey(commandFields) {
  if (commandFields.type === "adjust_part" && Math.abs(commandFields.deltaSeconds) === 1) {
    return null;
  }
  return `${commandFields.type}:${commandFields.partId ?? commandFields.scheduleId ?? commandFields.section ?? commandFields.meetingType ?? commandFields.target ?? ""}`;
}


async function postCommandWithCsrfRecovery(commandApi, envelope) {
  try {
    return await commandApi.command(envelope);
  } catch (error) {
    if (!(error instanceof ApiError) || error.status !== 403 || commandApi !== api) throw error;
    const recovered = await recover();
    if (commandApi !== api || !store.state.authenticated) return null;
    if (!recovered) {
      throw new ApiError(t("command.sessionConfirmFailed"), {
        status: 403,
        code: "csrf_recovery_failed",
      });
    }
    return commandApi.command(envelope);
  }
}

async function sendTimerCommand(commandFields, { inline = false, sync = false } = {}) {
  const fail = (code, message, tone = "error") => {
    if (!inline && code !== "expired" && code !== "command_in_progress") showToast(message, tone);
    return { ok: false, error: { code, message } };
  };
  if (!store.state.authenticated || !store.state.controlAvailable) {
    return fail("expired", t("login.sessionExpired"));
  }
  const commandApi = api;
  const { serverInstanceId, profile } = store.state;
  const dedupeKey = commandDedupeKey(commandFields);
  if (dedupeKey && inflightCommandKeys.has(dedupeKey)) {
    return fail("command_in_progress", t("command.inProgress"));
  }
  const envelope = { commandId: newUuid(), ...commandFields };
  if (SCHEDULE_BOUND_COMMANDS.has(envelope.type)) {
    envelope.scheduleRevision ??= store.state.scheduleRevision;
  }
  if (dedupeKey) inflightCommandKeys.set(dedupeKey, envelope.commandId);
  const isCurrent = () => commandApi === api && store.state.authenticated &&
    serverInstanceId === store.state.serverInstanceId && profile.id === store.state.profile.id;
  try {
    const result = await postCommandWithCsrfRecovery(commandApi, envelope);
    if (!isCurrent() || !result) return fail("expired", t("login.sessionExpired"));
    if (result.ok) {
      if (sync && store.state.scheduleRevision < result.scheduleRevision) await refreshScheduleOnly();
      if (!isCurrent()) return fail("expired", t("login.sessionExpired"));
      if (store.state.connection !== "online") void recover();
      return result;
    }

    const code = result.error?.code ?? "failed";
    const labels = {
      schedule_stale: "command.stateChanged",
      blocked: "command.protected",
      not_found: "command.unavailable",
      unavailable: "command.unavailable",
      duration_budget: "command.durationBudget",
      invalid: "command.invalid",
      command_in_progress: "command.inProgress",
    };
    if (["schedule_stale", "not_found", "unavailable"].includes(code)) await refreshScheduleOnly();
    if (!isCurrent()) return fail("expired", t("login.sessionExpired"));
    return fail(code, t(labels[code] ?? "command.notCompleted"), code === "schedule_stale" ? "info" : "error");
  } catch (error) {
    if (!isCurrent()) return fail("expired", t("login.sessionExpired"));
    if (error instanceof ApiError && error.status === 401) {
      downgradeToPublicStage({ reason: "expired", notify: true });
      return fail("expired", t("login.sessionExpired"));
    }
    return fail(error?.code ?? "failed", error instanceof ApiError
      ? error.message || t("command.sendFailed") : t("command.sendFailed"));
  } finally {
    if (inflightCommandKeys.get(dedupeKey) === envelope.commandId) inflightCommandKeys.delete(dedupeKey);
  }
}
