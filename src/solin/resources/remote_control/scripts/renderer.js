import {
  collectionLabel,
  collectionThumbnailUrl,
  countMedia,
  findNode,
  formatDuration,
  formatMeetingWeekRange,
  groupLabel,
  mediaThumbnailUrl,
  mediaLabel,
  meetingCollectionTitle,
  meetingTypeLabel,
  meetingWeekRelation,
  thumbnailUrl,
  toneClass,
} from "./format.js";
import { meetingWeekGroups, selectedCollection, visibleCollections } from "./state.js";

const byId = (id) => document.getElementById(id);
const TIMED_MEDIA_KINDS = new Set(["audio", "video"]);

function element(tag, { className, text, attrs, dataset } = {}) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  for (const [name, value] of Object.entries(attrs ?? {})) {
    if (value !== null && value !== undefined) node.setAttribute(name, String(value));
  }
  for (const [name, value] of Object.entries(dataset ?? {})) {
    node.dataset[name] = String(value);
  }
  return node;
}

function cssIcon(name, extraClass = "") {
  return element("span", {
    className: `css-icon css-icon--${name}${extraClass ? ` ${extraClass}` : ""}`,
    attrs: { "aria-hidden": "true" },
  });
}

function uiIcon(name, extraClass = "") {
  const icon = element("svg", {
    className: `ui-icon${extraClass ? ` ${extraClass}` : ""}`,
    attrs: { "aria-hidden": "true", focusable: "false" },
  });
  icon.append(element("use", { attrs: { href: `#icon-${name}` } }));
  return icon;
}

function setUiIcon(id, name) {
  byId(id).querySelector("use")?.setAttribute("href", `#icon-${name}`);
}

function mediaPlaceholder(mediaKind) {
  const name = {
    audio: "media-audio",
    image: "media-image",
    video: "media-video",
  }[mediaKind] ?? "media-generic";
  return uiIcon(name, "media-placeholder-icon");
}

export class Renderer {
  #lastCatalog = null;
  #lastPlayerIdentity = "";
  #groupExpansion = new Map();

  constructor(store) {
    this.store = store;
    this.bootView = byId("boot-view");
    this.loginView = byId("login-view");
    this.appShell = byId("app-shell");
    this.workspace = byId("main-content");
    this.libraryContent = byId("library-content");
    this.treeContent = byId("tree-content");
    this.treeToolbar = byId("tree-toolbar");
    this.libraryPanel = byId("library-panel");
    this.treePanel = byId("tree-panel");
    this.mobileQuery = window.matchMedia("(max-width: 640px)");
    this.mobileQuery.addEventListener("change", () => {
      this.#applyMobilePanelState(this.store.state);
    });
  }

  render(state, previous) {
    this.#renderView(state, previous);
    this.#renderTheme(state, previous);
    this.#renderConnection(state, previous);

    if (state.view !== "app") return;

    const catalogChanged = state.catalog !== previous?.catalog;
    const profileChanged = state.profile !== previous?.profile;
    const meetingWeekChanged =
      state.currentMeetingWeekStart !== previous?.currentMeetingWeekStart;
    const tabChanged = state.activeTab !== previous?.activeTab;
    const selectionChanged = state.selected !== previous?.selected;
    const mobileChanged = state.mobileView !== previous?.mobileView;
    const statusChanged =
      state.catalogStatus !== previous?.catalogStatus ||
      state.catalogError !== previous?.catalogError;
    const connectionChanged = state.connection !== previous?.connection;
    const pendingChanged = state.pendingCommands !== previous?.pendingCommands;
    const currentChanged =
      state.playback?.origin?.nodeId !== previous?.playback?.origin?.nodeId;

    if (catalogChanged) this.#groupExpansion.clear();

    if (
      catalogChanged ||
      tabChanged ||
      selectionChanged ||
      mobileChanged ||
      statusChanged ||
      connectionChanged ||
      profileChanged ||
      meetingWeekChanged
    ) {
      this.#renderNavigation(state);
      this.#renderLibrary(state);
    }
    if (
      catalogChanged ||
      tabChanged ||
      selectionChanged ||
      statusChanged ||
      connectionChanged ||
      pendingChanged ||
      currentChanged ||
      profileChanged ||
      meetingWeekChanged
    ) {
      this.#renderTree(state);
    }
    if (
      state.playback !== previous?.playback ||
      state.connection !== previous?.connection ||
      pendingChanged ||
      catalogChanged
    ) {
      this.#renderPlayer(state);
    }
  }

  #renderView(state, previous) {
    if (state.view === previous?.view) return;
    this.bootView.hidden = state.view !== "boot";
    this.loginView.hidden = state.view !== "login";
    this.appShell.hidden = state.view !== "app";
    if (state.view === "login") {
      window.setTimeout(() => byId("username")?.focus(), 0);
    }
  }

  #renderTheme(state, previous) {
    if (state.profile === previous?.profile) return;
    const theme = state.profile.theme === "light" ? "light" : "dark";
    document.documentElement.dataset.theme = theme;
    document.documentElement.lang = state.profile.locale || "pt-BR";
    const themeMeta = document.querySelector('meta[name="theme-color"]');
    themeMeta?.setAttribute("content", theme === "light" ? "#e8edf3" : "#0d1117");
    byId("profile-name").textContent = state.profile.displayName || "Controle remoto";
  }

  #renderConnection(state, previous) {
    if (state.connection === previous?.connection && state.view === previous?.view) return;
    const labels = {
      connecting: "Conectando…",
      syncing: "Sincronizando…",
      online: "Conectado",
      reconnecting: "Reconectando…",
      offline: "Sem conexão",
    };
    const chip = byId("connection-chip");
    chip.dataset.state = state.connection;
    byId("connection-label").textContent = labels[state.connection] ?? labels.connecting;
    const reconnecting = state.connection === "reconnecting" || state.connection === "offline";
    byId("reconnect-banner").hidden = !reconnecting || state.view !== "app";
    byId("login-offline").hidden =
      state.view !== "login" || !["offline", "reconnecting"].includes(state.connection);
  }

  #renderNavigation(state) {
    for (const tab of ["playlists", "meetings"]) {
      const button = byId(`${tab}-tab`);
      const active = state.activeTab === tab;
      button.setAttribute("aria-selected", String(active));
      button.tabIndex = active ? 0 : -1;
    }
    const panel = byId("library-panel");
    panel.setAttribute("aria-labelledby", `${state.activeTab}-tab`);
    this.workspace.dataset.mobileView = state.mobileView;
    this.#applyMobilePanelState(state);

    const playlists = state.catalog.collections.filter(
      (collection) => collection.kind !== "meeting",
    ).length;
    const meetings = state.catalog.collections.filter(
      (collection) => collection.kind === "meeting",
    ).length;
    this.#setCount("playlists-count", playlists, "playlists");
    this.#setCount("meetings-count", meetings, "reuniões");

    const isMeetings = state.activeTab === "meetings";
    byId("library-eyebrow").textContent = isMeetings ? "REUNIÕES" : "PLAYLISTS";
    byId("library-heading").textContent = isMeetings
      ? "Escolha uma reunião"
      : "Escolha uma playlist";
  }

  #setCount(id, value, label) {
    const node = byId(id);
    node.textContent = String(value);
    node.setAttribute("aria-label", `${value} ${label}`);
  }

  #applyMobilePanelState(state) {
    const mobile = this.mobileQuery.matches;
    const detail = state.mobileView === "detail";
    this.libraryPanel.inert = mobile && detail;
    this.treePanel.inert = mobile && !detail;
    if (mobile) {
      this.libraryPanel.setAttribute("aria-hidden", String(detail));
      this.treePanel.setAttribute("aria-hidden", String(!detail));
    } else {
      this.libraryPanel.removeAttribute("aria-hidden");
      this.treePanel.removeAttribute("aria-hidden");
    }
  }

  #renderLibrary(state) {
    if (state.catalogStatus === "loading") {
      this.libraryContent.replaceChildren(this.#skeletonList());
      return;
    }
    if (state.catalogStatus === "error") {
      this.libraryContent.replaceChildren(
        this.#stateMessage({
          type: "error",
          title: "Não foi possível carregar",
          detail: state.catalogError || "Tente atualizar a biblioteca.",
          action: "retry-catalog",
          actionLabel: "Tentar novamente",
        }),
      );
      return;
    }

    const collections = visibleCollections(state);
    if (collections.length === 0) {
      const meetingTab = state.activeTab === "meetings";
      this.libraryContent.replaceChildren(
        this.#stateMessage({
          title: meetingTab ? "Nenhuma reunião disponível" : "Nenhuma playlist disponível",
          detail: meetingTab
            ? "Quando houver reuniões no Solin, elas aparecerão aqui automaticamente."
            : "Quando houver playlists no Solin, elas aparecerão aqui automaticamente.",
        }),
      );
      return;
    }

    this.libraryContent.replaceChildren(
      state.activeTab === "meetings"
        ? this.#meetingWeekList(collections, state)
        : this.#collectionList(collections, state),
    );
  }

  #collectionList(collections, state) {
    const list = element("ul", { className: "library-list" });
    for (const collection of collections) {
      list.append(this.#collectionItem(collection, state));
    }
    return list;
  }

  #meetingWeekList(collections, state) {
    const groups = meetingWeekGroups(collections, state.currentMeetingWeekStart);
    const wrapper = element("div", { className: "meeting-weeks" });
    if (groups.current) {
      wrapper.append(this.#meetingWeek(groups.current, state, { current: true }));
    }
    if (groups.future.length > 0) {
      wrapper.append(this.#meetingPeriod("Próximas semanas", groups.future, state));
    }
    if (groups.past.length > 0) {
      wrapper.append(this.#meetingPeriod("Semanas anteriores", groups.past, state));
    }
    if (groups.unclassified.length > 0) {
      wrapper.append(
        this.#meetingPeriod(
          "Outras reuniões",
          [{ weekStart: null, collections: groups.unclassified }],
          state,
        ),
      );
    }
    return wrapper;
  }

  #meetingPeriod(title, groups, state) {
    const slug = title.toLocaleLowerCase("pt-BR").replace(/[^a-z]+/g, "-");
    const headingId = `meeting-period-${slug}`;
    const section = element("section", {
      className: "meeting-period",
      attrs: { "aria-labelledby": headingId },
    });
    section.append(
      element("h3", { className: "meeting-period-title", text: title, attrs: { id: headingId } }),
    );
    for (const group of groups) {
      section.append(this.#meetingWeek(group, state, { headingTag: "h4" }));
    }
    return section;
  }

  #meetingWeek(group, state, { current = false, headingTag = "h3" } = {}) {
    const safeWeek = group.weekStart || "unclassified";
    const headingId = `meeting-week-${safeWeek}`;
    const section = element("section", {
      className: "meeting-week",
      attrs: { "aria-labelledby": headingId },
      dataset: { currentWeek: current },
    });
    const header = element("header", { className: "meeting-week-header" });
    header.append(
      element(headingTag, {
        className: "meeting-week-range",
        text: group.weekStart
          ? formatMeetingWeekRange(group.weekStart, state.profile.locale)
          : "Semana não identificada",
        attrs: { id: headingId },
      }),
    );
    if (current) {
      header.append(element("span", { className: "meeting-current-badge", text: "Esta semana" }));
    }
    section.append(header);
    if (group.collections.length === 0) {
      section.append(
        element("p", {
          className: "meeting-week-empty",
          text: "Nenhuma reunião disponível nesta semana.",
        }),
      );
      return section;
    }
    section.append(this.#collectionList(group.collections, state));
    return section;
  }

  #collectionItem(collection, state) {
    const item = element("li");
    const count = countMedia(collection.nodes);
    const button = element("button", {
      className: "library-item",
      attrs: {
        type: "button",
        "aria-current": String(state.selected[state.activeTab] === collection.id),
      },
      dataset: { action: "select-collection", collectionId: collection.id },
    });
    const icon = element("span", { className: "library-icon", attrs: { "aria-hidden": "true" } });
    const coverSource = collection.kind === "meeting" ? collectionThumbnailUrl(collection) : null;
    if (coverSource) {
      icon.classList.add("library-icon--cover");
      const image = element("img", { attrs: { src: coverSource, alt: "", loading: "lazy" } });
      image.addEventListener("error", () => {
        icon.classList.remove("library-icon--cover");
        icon.replaceChildren(uiIcon("nav-meetings"));
      }, { once: true });
      icon.append(image);
    } else {
      icon.append(collection.kind === "meeting" ? uiIcon("nav-meetings") : cssIcon("folder"));
    }
    const copy = element("span", { className: "library-copy" });
    const isMeeting = collection.kind === "meeting";
    const title = isMeeting
      ? meetingCollectionTitle(collection)
      : collection.title || "Sem título";
    const subtitle = isMeeting
      ? meetingTypeLabel(collection.meetingType)
      : collection.subtitle || collectionLabel(collection.kind);
    copy.append(
      element("span", { className: "library-title", text: title }),
      element("span", {
        className: "library-subtitle",
        text: subtitle,
      }),
    );
    button.append(
      icon,
      copy,
      element("span", {
        className: "library-meta",
        text: `${count} ${count === 1 ? "item" : "itens"}`,
      }),
    );
    item.append(button);
    return item;
  }

  #renderTree(state) {
    const collection = selectedCollection(state);
    if (!collection) {
      this.treeToolbar.hidden = true;
      this.treeContent.replaceChildren(
        this.#stateMessage({
          type: "placeholder",
          title: state.activeTab === "meetings" ? "Escolha uma reunião" : "Escolha uma playlist",
          detail: "A estrutura completa será exibida aqui, sem alterar o conteúdo do Solin.",
        }),
      );
      return;
    }

    this.treeToolbar.hidden = false;
    byId("tree-kicker").textContent = collectionLabel(collection.kind).toUpperCase();
    byId("tree-heading").textContent =
      collection.kind === "meeting"
        ? meetingCollectionTitle(collection)
        : collection.title || "Sem título";
    if (collection.kind === "meeting") {
      const relation = meetingWeekRelation(
        collection.weekStart,
        state.currentMeetingWeekStart,
      );
      const range = formatMeetingWeekRange(collection.weekStart, state.profile.locale);
      byId("tree-subtitle").textContent = [relation, range].filter(Boolean).join(" · ");
    } else {
      byId("tree-subtitle").textContent = collection.subtitle || "";
    }
    const count = countMedia(collection.nodes);
    byId("tree-item-count").textContent = `${count} ${count === 1 ? "mídia" : "mídias"}`;

    if (collection.nodes.length === 0) {
      this.treeContent.replaceChildren(
        this.#stateMessage({
          title: "Esta coleção está vazia",
          detail: "As alterações feitas no Solin aparecerão aqui automaticamente.",
        }),
      );
      return;
    }

    const list = element("ul", { className: "tree-root" });
    this.#appendNodes(list, collection.nodes, collection, state, 0);
    this.treeContent.replaceChildren(list);
  }

  #appendNodes(list, nodes, collection, state, depth) {
    for (const node of nodes) {
      const item = element("li");
      if (node.kind === "media") {
        item.append(this.#mediaRow(node, collection, state));
      } else if (node.kind === "marker") {
        item.append(this.#markerRow(node));
        if (node.children?.length) {
          const children = element("ul", { className: "tree-children" });
          this.#appendNodes(children, node.children, collection, state, depth + 1);
          item.append(children);
        }
      } else {
        item.append(this.#treeGroup(node, collection, state, depth));
      }
      list.append(item);
    }
  }

  #treeGroup(node, collection, state, depth) {
    const expansionKey = JSON.stringify([collection.id, node.id]);
    const locallyExpanded = this.#groupExpansion.get(expansionKey);
    const isOpen = locallyExpanded ?? node.collapsed !== true;
    const details = element("details", {
      className: `tree-group ${toneClass(node.color || node.id)}`,
      attrs: { open: isOpen ? "" : null },
    });
    details.addEventListener("toggle", () => {
      this.#groupExpansion.set(expansionKey, details.open);
    });
    const summary = element("summary", { className: "tree-group-summary" });
    const copy = element("span", { className: "group-title-wrap" });
    copy.append(
      element("span", { className: "group-title", text: node.title || "Sem título" }),
      element("span", { className: "group-kind", text: groupLabel(node.kind) }),
    );
    const count = countMedia(node.children);
    summary.append(
      copy,
      element("span", {
        className: "group-count",
        text: String(count),
        attrs: { "aria-label": `${count} mídias` },
      }),
    );
    const children = element("ul", { className: "tree-children" });
    this.#appendNodes(children, node.children ?? [], collection, state, depth + 1);
    details.append(summary, children);
    return details;
  }

  #markerRow(node) {
    const row = element("div", { className: "marker-row" });
    row.append(element("span", { text: node.title || "Marcador" }));
    return row;
  }

  #mediaRow(node, collection, state) {
    const current =
      state.playback.origin?.collectionId === collection.id &&
      state.playback.origin?.nodeId === node.id &&
      state.playback.state !== "idle";
    const disabled =
      !node.available ||
      state.connection !== "online" ||
      state.pendingCommands.has("play");
    const button = element("button", {
      className: "media-row",
      attrs: {
        type: "button",
        disabled: disabled ? "" : null,
        "data-current": String(current),
        "aria-label": current
          ? `${node.title}, em reprodução`
          : `Reproduzir ${node.title || "mídia"}`,
      },
      dataset: {
        action: "play-media",
        source: collection.kind,
        collectionId: collection.id,
        nodeId: node.id,
      },
    });
    const thumbnail = element("span", { className: "media-thumbnail", attrs: { "aria-hidden": "true" } });
    const source = thumbnailUrl(collection.kind, collection.id, node);
    if (source) {
      const image = element("img", { attrs: { src: source, alt: "", loading: "lazy" } });
      image.addEventListener("error", () => {
        thumbnail.replaceChildren(mediaPlaceholder(node.mediaKind));
      }, { once: true });
      thumbnail.append(image);
    } else {
      thumbnail.append(mediaPlaceholder(node.mediaKind));
    }
    const copy = element("span", { className: "media-copy" });
    copy.append(element("span", { className: "media-name", text: node.title || "Sem título" }));
    const detail = element("span", { className: "media-detail" });
    detail.append(element("span", { className: "media-kind", text: mediaLabel(node.mediaKind) }));
    if (node.durationMs > 0) {
      detail.append(document.createTextNode("•"), element("span", { text: formatDuration(node.durationMs) }));
    }
    if (!node.available) {
      detail.append(
        document.createTextNode("•"),
        element("span", {
          className: "media-unavailable",
          text: node.unavailableReason || "Indisponível",
        }),
      );
    }
    copy.append(detail);
    const action = element("span", { className: "media-action", attrs: { "aria-hidden": "true" } });
    action.append(cssIcon("play"));
    button.append(thumbnail, copy, action);
    return button;
  }

  #renderPlayer(state) {
    const playback = state.playback;
    const active = playback.state !== "idle" && Boolean(playback.playbackSessionId);
    const pending = state.pendingCommands;
    const online = state.connection === "online";
    const capabilities = playback.capabilities ?? {};
    const timed = active && TIMED_MEDIA_KINDS.has(playback.mediaKind);
    const mode = active ? (timed ? "timed" : "visual") : "idle";
    const dock = byId("player-dock");

    dock.dataset.mode = mode;
    dock.setAttribute("aria-busy", String(playback.state === "loading"));
    byId("now-playing-title").textContent = active ? "AGORA REPRODUZINDO" : "PROJEÇÃO";

    byId("media-title").textContent = active ? playback.title || "Mídia sem título" : "Nada em reprodução";
    byId("media-context").textContent = active
      ? this.#playbackContext(state)
      : "Nenhuma mídia está sendo projetada";

    const canPause = playback.state === "playing" && capabilities.pause;
    const canResume = playback.state === "paused" && capabilities.resume;
    const showQueueNavigation = active && (playback.queue?.length ?? 0) > 1;
    const showPrevious = showQueueNavigation;
    const showPlayPause = active && (canPause || canResume);
    const showNext = showQueueNavigation;
    const playPause = byId("play-pause-button");
    playPause.setAttribute("aria-label", canPause ? "Pausar" : "Continuar");
    byId("play-pause-icon").className = `css-icon css-icon--${canPause ? "pause" : "play"}`;

    this.#control(
      "previous-button",
      showPrevious,
      capabilities.previous === true && online && !pending.has("previous"),
    );
    this.#control(
      "play-pause-button",
      showPlayPause,
      online && !pending.has("pause") && !pending.has("resume"),
    );
    this.#control(
      "next-button",
      showNext,
      capabilities.next === true && online && !pending.has("next"),
    );
    byId("transport-buttons").hidden = !showPrevious && !showPlayPause && !showNext;

    const duration = Math.max(0, Number(playback.durationMs) || 0);
    const position = Math.min(duration || Number.MAX_SAFE_INTEGER, Math.max(0, Number(playback.positionMs) || 0));
    const showTimeline = timed && duration > 0;
    const timeline = byId("timeline");
    const seek = byId("seek-control");
    const seekEnabled =
      showTimeline && capabilities.seek === true && online && !pending.has("seek");
    timeline.hidden = !showTimeline;
    timeline.setAttribute("aria-disabled", String(!seekEnabled));
    seek.max = String(duration);
    if (document.activeElement !== seek) seek.value = String(position);
    seek.disabled = !seekEnabled;
    seek.setAttribute("aria-valuetext", `${formatDuration(position)} de ${formatDuration(duration)}`);
    byId("elapsed-time").textContent = formatDuration(position);
    byId("duration-time").textContent = formatDuration(duration);

    const volume = Math.min(100, Math.max(0, Number(playback.volume) || 0));
    const showVolume = timed && capabilities.setVolume === true;
    const volumeControl = byId("volume-control");
    const volumeSlider = byId("volume-slider");
    volumeControl.hidden = !showVolume;
    if (document.activeElement !== volumeSlider) volumeSlider.value = String(volume);
    volumeSlider.disabled = !showVolume || !online || pending.has("set_volume");
    volumeSlider.setAttribute("aria-valuetext", `${volume}%`);
    byId("volume-value").textContent = `${volume}%`;
    const mute = byId("mute-button");
    mute.disabled = volumeSlider.disabled;
    mute.setAttribute("aria-label", volume === 0 ? "Restaurar volume" : "Silenciar");
    const volumeIcon = volume === 0 ? "volume-mute" : volume < 50 ? "volume-low" : "volume-high";
    setUiIcon("volume-icon", volumeIcon);

    const showStop = active && capabilities.stop === true;
    this.#control("stop-button", showStop, online && !pending.has("stop"));
    const transportButtonsVisible = showPrevious || showPlayPause || showNext;
    const layoutMode = active ? (showVolume ? "timed" : "visual") : "idle";
    this.appShell.dataset.playerMode = layoutMode;
    dock.dataset.controls = showTimeline ? "timeline" : transportButtonsVisible ? "compact" : "none";
    byId("transport").hidden = !transportButtonsVisible && !showTimeline;
    byId("dock-actions").hidden = !showVolume && !showStop;

    const identity = `${playback.origin?.source}:${playback.origin?.collectionId}:${playback.origin?.nodeId}:${playback.mediaKind}:${active}`;
    if (identity !== this.#lastPlayerIdentity || state.catalog !== this.#lastCatalog) {
      this.#lastPlayerIdentity = identity;
      this.#lastCatalog = state.catalog;
      this.#renderPlayerArt(state, active);
    }
  }

  #control(id, visible, enabled) {
    const control = byId(id);
    control.hidden = !visible;
    control.disabled = !visible || !enabled;
  }

  #playbackContext(state) {
    if (state.playback.state === "error" && state.playback.error?.message) {
      return state.playback.error.message;
    }
    const origin = state.playback.origin;
    const collection = state.catalog.collections.find((item) => item.id === origin?.collectionId);
    if (collection) {
      return collection.kind === "meeting"
        ? meetingCollectionTitle(collection)
        : collection.title;
    }
    if (origin?.source === "temporary") return "Conteúdo temporário do Solin";
    return mediaLabel(state.playback.mediaKind);
  }

  #renderPlayerArt(state, active) {
    const art = byId("media-art");
    art.replaceChildren();
    if (!active) {
      art.append(mediaPlaceholder("unknown"));
      return;
    }
    const currentIndex = state.playback.currentIndex;
    const queueItem = Number.isInteger(currentIndex)
      ? state.playback.queue?.[currentIndex]
      : null;
    const origin = queueItem?.origin;
    const source = mediaThumbnailUrl(
      origin?.source,
      origin?.collectionId,
      origin?.nodeId,
      queueItem?.thumbnailId,
    );
    if (!source) {
      art.append(mediaPlaceholder(state.playback.mediaKind));
      return;
    }
    const image = element("img", { attrs: { src: source, alt: "" } });
    image.addEventListener("error", () => {
      art.replaceChildren(mediaPlaceholder(state.playback.mediaKind));
    }, { once: true });
    art.append(image);
  }

  #skeletonList() {
    const wrapper = element("div", { className: "skeleton-list", attrs: { "aria-label": "Carregando" } });
    for (let index = 0; index < 5; index += 1) {
      wrapper.append(element("div", { className: "skeleton-item", attrs: { "aria-hidden": "true" } }));
    }
    return wrapper;
  }

  #stateMessage({ type = "empty", title, detail, action, actionLabel }) {
    const className =
      type === "error" ? "error-state" : type === "placeholder" ? "tree-placeholder" : "empty-state";
    const wrapper = element("div", { className });
    const icon = element("span", { className: "state-icon", attrs: { "aria-hidden": "true" } });
    icon.append(cssIcon(type === "error" ? "refresh" : "folder"));
    wrapper.append(icon, element("strong", { text: title }), element("p", { text: detail }));
    if (action) {
      wrapper.append(
        element("button", {
          className: "button button--secondary button--compact",
          text: actionLabel,
          attrs: { type: "button" },
          dataset: { action },
        }),
      );
    }
    return wrapper;
  }
}

export function showToast(message, type = "info") {
  const region = byId("toast-region");
  const toast = element("div", {
    className: `toast toast--${type}`,
    attrs: { role: type === "error" ? "alert" : "status" },
  });
  toast.append(element("span", { className: "toast-indicator", attrs: { "aria-hidden": "true" } }));
  toast.append(element("span", { text: message }));
  region.append(toast);
  window.setTimeout(() => toast.remove(), 4_500);
}

export function announce(message) {
  const region = byId("assertive-status");
  region.textContent = "";
  window.setTimeout(() => {
    region.textContent = message;
  }, 20);
}
