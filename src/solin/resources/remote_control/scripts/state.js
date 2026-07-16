const EMPTY_PLAYBACK = Object.freeze({
  playbackRevision: 0,
  playbackSessionId: null,
  state: "idle",
  title: "",
  origin: null,
  mediaKind: "unknown",
  queue: [],
  currentIndex: -1,
  positionMs: 0,
  durationMs: 0,
  volume: 100,
  capabilities: {},
  error: null,
});

export function createInitialState() {
  return {
    view: "boot",
    connection: "connecting",
    activeTab: "meetings",
    mobileView: "list",
    selected: { playlists: null, meetings: null },
    serverInstanceId: null,
    eventSequence: null,
    currentMeetingWeekStart: null,
    profile: {
      displayName: "Controle remoto",
      locale: "pt-BR",
      theme: "dark",
    },
    catalog: {
      catalogRevision: 0,
      collections: [],
    },
    catalogStatus: "loading",
    catalogError: null,
    playback: EMPTY_PLAYBACK,
    pendingCommands: new Set(),
  };
}

export class Store {
  #listeners = new Set();
  #state;

  constructor(initialState = createInitialState()) {
    this.#state = initialState;
  }

  get state() {
    return this.#state;
  }

  subscribe(listener) {
    this.#listeners.add(listener);
    listener(this.#state, null);
    return () => this.#listeners.delete(listener);
  }

  update(patch) {
    const previous = this.#state;
    const nextPatch = typeof patch === "function" ? patch(previous) : patch;
    this.#state = { ...previous, ...nextPatch };
    for (const listener of this.#listeners) {
      listener(this.#state, previous);
    }
  }

  reset() {
    const previous = this.#state;
    this.#state = createInitialState();
    for (const listener of this.#listeners) {
      listener(this.#state, previous);
    }
  }
}

export function applyBootstrap(store, bootstrap) {
  const profile = bootstrap.profile ?? {};
  const catalog = bootstrap.catalog ?? {
    catalogRevision: bootstrap.catalogRevision ?? 0,
    collections: [],
  };
  const playback = bootstrap.playback ?? EMPTY_PLAYBACK;

  store.update((state) => ({
    view: "app",
    connection: "online",
    serverInstanceId: bootstrap.serverInstanceId,
    eventSequence: Number.isInteger(bootstrap.eventSequence)
      ? bootstrap.eventSequence
      : state.eventSequence,
    currentMeetingWeekStart:
      bootstrap.currentMeetingWeekStart || state.currentMeetingWeekStart,
    profile: {
      displayName: profile.name || "Controle remoto",
      locale: profile.locale || "pt-BR",
      theme: profile.theme === "light" ? "light" : "dark",
    },
    catalog,
    catalogStatus: "ready",
    catalogError: null,
    playback,
    selected: reconcileSelection(state.selected, catalog.collections),
  }));
}

function reconcileSelection(selected, collections) {
  const ids = new Set(collections.map((collection) => collection.id));
  return {
    playlists: ids.has(selected.playlists) ? selected.playlists : null,
    meetings: ids.has(selected.meetings) ? selected.meetings : null,
  };
}

export function collectionTab(kind) {
  return kind === "meeting" ? "meetings" : "playlists";
}

export function visibleCollections(state, tab = state.activeTab) {
  return state.catalog.collections.filter(
    (collection) => collectionTab(collection.kind) === tab,
  );
}

export function selectedCollection(state) {
  const id = state.selected[state.activeTab];
  return state.catalog.collections.find((collection) => collection.id === id) ?? null;
}

const MEETING_TYPE_ORDER = Object.freeze({
  midweek: 0,
  weekend: 1,
  memorial: 2,
  other: 3,
});

export function meetingWeekGroups(collections, currentWeekStart) {
  const byWeek = new Map();
  const unclassified = [];
  for (const collection of collections) {
    if (!/^\d{4}-\d{2}-\d{2}$/.test(collection.weekStart || "")) {
      unclassified.push(collection);
      continue;
    }
    if (!byWeek.has(collection.weekStart)) byWeek.set(collection.weekStart, []);
    byWeek.get(collection.weekStart).push(collection);
  }

  const groups = [...byWeek].map(([weekStart, entries]) => ({
    weekStart,
    collections: entries.sort(
      (left, right) =>
        (MEETING_TYPE_ORDER[left.meetingType] ?? MEETING_TYPE_ORDER.other) -
          (MEETING_TYPE_ORDER[right.meetingType] ?? MEETING_TYPE_ORDER.other) ||
        String(left.title || "").localeCompare(String(right.title || "")),
    ),
  }));
  const current = groups.find((group) => group.weekStart === currentWeekStart) ??
    (/^\d{4}-\d{2}-\d{2}$/.test(currentWeekStart || "")
      ? { weekStart: currentWeekStart, collections: [] }
      : null);
  const future = groups
    .filter((group) => group.weekStart > currentWeekStart)
    .sort((left, right) => left.weekStart.localeCompare(right.weekStart));
  const past = groups
    .filter((group) => group.weekStart < currentWeekStart)
    .sort((left, right) => right.weekStart.localeCompare(left.weekStart));

  return { current, future, past, unclassified };
}
