import { normalizeClockConfig } from "./timer-display.js";

const DEFAULT_CAPABILITIES = Object.freeze({
  controlParts: true,
  adjustPlannedMinutes: true,
  setPlannedSeconds: true,
  setSectionCount: true,
  switchMeetingType: true,
  navigateWeeks: true,
});

function nowMonotonicMs() {
  return typeof performance !== "undefined" && typeof performance.now === "function"
    ? performance.now()
    : Date.now();
}

export function createInitialState() {
  const nowEpochMs = Date.now();
  return {
    view: "boot",
    loginModalOpen: false,
    durationModal: {
      open: false,
      partId: "",
      title: "",
      initialText: "",
    },
    hudVisible: true,
    heroStuck: false,
    authenticated: false,
    controlAvailable: true,
    connection: "connecting",
    serverInstanceId: "",
    eventSequence: 0,
    liveRevision: 0,
    scheduleRevision: 0,
    liveAnchorMonotonicMs: nowMonotonicMs(),
    profile: {
      id: "",
      name: "",
      displayName: "",
      theme: "dark",
    },
    live: normalizeLiveSnapshot({
      liveRevision: 0,
      serverEpochMs: nowEpochMs,
      utcOffsetMinutes: -new Date().getTimezoneOffset(),
      active: false,
      state: "idle",
      direction: "down",
      clockConfig: {},
    }),
    schedule: normalizeScheduleSnapshot(null),
  };
}

export class Store {
  #state = createInitialState();
  #subscribers = new Set();

  get state() {
    return this.#state;
  }

  subscribe(listener) {
    this.#subscribers.add(listener);
    return () => this.#subscribers.delete(listener);
  }

  update(patchOrUpdater) {
    const previous = this.#state;
    const patch =
      typeof patchOrUpdater === "function" ? patchOrUpdater(previous) : patchOrUpdater;
    if (!patch || typeof patch !== "object") return;
    this.#state = { ...previous, ...patch };
    for (const listener of this.#subscribers) {
      listener(this.#state, previous);
    }
  }
}

export function normalizeCapabilities(raw) {
  const src = raw && typeof raw === "object" ? raw : {};
  return {
    controlParts: Boolean(src.controlParts ?? DEFAULT_CAPABILITIES.controlParts),
    adjustPlannedMinutes: Boolean(
      src.adjustPlannedMinutes ?? DEFAULT_CAPABILITIES.adjustPlannedMinutes,
    ),
    setPlannedSeconds: Boolean(
      src.setPlannedSeconds ?? DEFAULT_CAPABILITIES.setPlannedSeconds,
    ),
    setSectionCount: Boolean(src.setSectionCount ?? DEFAULT_CAPABILITIES.setSectionCount),
    switchMeetingType: Boolean(
      src.switchMeetingType ?? DEFAULT_CAPABILITIES.switchMeetingType,
    ),
    navigateWeeks: Boolean(src.navigateWeeks ?? DEFAULT_CAPABILITIES.navigateWeeks),
  };
}

export function normalizeLiveSnapshot(raw) {
  const src = raw && typeof raw === "object" ? raw : {};
  const clockConfig = normalizeClockConfig(src.clockConfig);
  const active = Boolean(src.active);
  const state = active && ["idle", "running", "stopped"].includes(src.state)
    ? src.state
    : "idle";
  return {
    liveRevision: Math.max(0, Math.trunc(Number(src.liveRevision) || 0)),
    serverEpochMs: Math.max(0, Math.trunc(Number(src.serverEpochMs) || Date.now())),
    utcOffsetMinutes: Math.trunc(
      Number.isFinite(Number(src.utcOffsetMinutes))
        ? Number(src.utcOffsetMinutes)
        : -new Date().getTimezoneOffset(),
    ),
    active,
    state,
    direction: src.direction === "up" ? "up" : clockConfig.direction,
    activePartId: active && typeof src.activePartId === "string" ? src.activePartId : null,
    activePartTitle: active ? String(src.activePartTitle ?? "") : "",
    activeSection:
      active && typeof src.activeSection === "string" ? src.activeSection : null,
    plannedSeconds: Math.max(0, Math.trunc(Number(src.plannedSeconds) || 0)),
    elapsedSeconds: Math.max(0, Number(src.elapsedSeconds) || 0),
    clockConfig,
    handAlert: normalizeHandAlert(src.handAlert),
  };
}

function normalizeHandAlert(raw) {
  if (raw?.kind === "manual") return { kind: "manual" };
  if (raw?.kind !== "automatic" || !Number.isSafeInteger(raw.count) || raw.count < 1) {
    return null;
  }
  const names = Array.isArray(raw.names)
    ? raw.names.filter((name) => typeof name === "string" && name.trim()).slice(0, raw.count)
    : [];
  return { kind: "automatic", count: raw.count, names };
}

export function normalizeScheduleSnapshot(raw) {
  const src = raw && typeof raw === "object" ? raw : {};
  const parts = Array.isArray(src.parts)
    ? src.parts.filter((item) => item && typeof item === "object").map(normalizePartItem)
    : [];
  return {
    scheduleRevision: Math.max(0, Math.trunc(Number(src.scheduleRevision) || 0)),
    weekMonday: String(src.weekMonday ?? ""),
    weekLabel: String(src.weekLabel ?? ""),
    isCurrentWeek: src.isCurrentWeek !== false,
    meetingType: src.meetingType === "weekend" ? "weekend" : "midweek",
    parts,
    capabilities: normalizeCapabilities(src.capabilities),
  };
}

function normalizePartItem(raw) {
  const state = ["idle", "running", "stopped"].includes(raw.state) ? raw.state : "idle";
  const durationEditable =
    raw.durationEditable === undefined
      ? state === "idle"
      : Boolean(raw.durationEditable && state === "idle");
  return {
    id: String(raw.id ?? ""),
    displayNumber: Math.max(1, Math.trunc(Number(raw.displayNumber) || 1)),
    section: String(raw.section ?? "opening_comments"),
    sectionKey: String(raw.sectionKey ?? raw.section ?? "opening_comments"),
    sectionHeaderTitle: String(raw.sectionHeaderTitle ?? ""),
    title: String(raw.title ?? ""),
    plannedSeconds: Math.max(0, Math.trunc(Number(raw.plannedSeconds) || 0)),
    plannedLabel: String(raw.plannedLabel ?? "0:00"),
    state,
    firstOfSection: Boolean(raw.firstOfSection),
    showSectionHeader: Boolean(raw.showSectionHeader),
    configurableCount: Boolean(raw.configurableCount),
    sectionCount: Math.max(0, Math.trunc(Number(raw.sectionCount) || 0)),
    durationEditable,
    sectionTotalLabel: String(raw.sectionTotalLabel ?? ""),
    startedLabel: String(raw.startedLabel ?? ""),
    resultLabel: String(raw.resultLabel ?? ""),
  };
}

export function normalizeProfile(raw) {
  const src = raw && typeof raw === "object" ? raw : {};
  return {
    id: String(src.id ?? ""),
    name: String(src.name ?? ""),
    displayName: String(src.displayName ?? src.name ?? ""),
    theme: src.theme === "light" ? "light" : "dark",
  };
}

export function applyPublicBootstrap(store, payload, { preserveView = false } = {}) {
  const live = normalizeLiveSnapshot(payload?.live);
  store.update((current) => ({
    view: preserveView && current.view !== "boot" ? current.view : "stage",
    authenticated: Boolean(payload?.authenticated),
    controlAvailable: Boolean(payload?.controlAvailable),
    serverInstanceId: String(payload?.serverInstanceId ?? current.serverInstanceId),
    eventSequence: Math.max(0, Math.trunc(Number(payload?.eventSequence) || 0)),
    liveRevision: live.liveRevision,
    liveAnchorMonotonicMs: nowMonotonicMs(),
    profile: normalizeProfile(payload?.profile),
    live,
  }));
}

export function applyAuthenticatedBootstrap(
  store,
  payload,
  { targetView = null } = {},
) {
  const live = normalizeLiveSnapshot(payload?.live);
  const schedule = normalizeScheduleSnapshot(payload?.schedule);
  store.update((current) => ({
    view: targetView ?? (current.view === "boot" ? "stage" : current.view),
    loginModalOpen: false,
    authenticated: true,
    controlAvailable: Boolean(payload?.controlAvailable ?? true),
    serverInstanceId: String(payload?.serverInstanceId ?? current.serverInstanceId),
    eventSequence: Math.max(0, Math.trunc(Number(payload?.eventSequence) || 0)),
    liveRevision: live.liveRevision,
    scheduleRevision: schedule.scheduleRevision,
    liveAnchorMonotonicMs: nowMonotonicMs(),
    profile: normalizeProfile(payload?.profile),
    live,
    schedule,
  }));
}

export function applyHeartbeatClock(store, event) {
  const nextEpochMs = Number(event?.serverEpochMs);
  if (!Number.isFinite(nextEpochMs) || nextEpochMs <= 0) return;
  const anchorNow = nowMonotonicMs();
  store.update((current) => {
    const deltaSeconds = Math.max(
      0,
      (anchorNow - current.liveAnchorMonotonicMs) / 1000.0,
    );
    const rebasedElapsed =
      current.live.active && current.live.state === "running"
        ? current.live.elapsedSeconds + deltaSeconds
        : current.live.elapsedSeconds;
    return {
      liveAnchorMonotonicMs: anchorNow,
      live: {
        ...current.live,
        serverEpochMs: Math.trunc(nextEpochMs),
        utcOffsetMinutes: Number.isFinite(Number(event?.utcOffsetMinutes))
          ? Math.trunc(Number(event.utcOffsetMinutes))
          : current.live.utcOffsetMinutes,
        elapsedSeconds: rebasedElapsed,
      },
    };
  });
}
