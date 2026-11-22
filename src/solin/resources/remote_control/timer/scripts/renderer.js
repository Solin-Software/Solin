import {
  announce,
  byId,
  cssIcon,
  element,
  setUiIcon,
  showToast,
  uiIcon,
} from "../../shared/scripts/dom.js";
import { currentLocale, t } from "../../shared/scripts/i18n.js";
import { ClockStageCanvas } from "./clock-face.js";
import { StageHandAlert } from "./hand-alert.js";
import {
  buildRenderModel,
  evaluateLiveSnapshot,
  formatPartLiveClock,
  formatStageDuration,
  formatWallClock,
} from "./timer-display.js";

const SECTION_ICONS = new Set([
  "treasures",
  "ministry",
  "living",
  "public_talk",
  "watchtower",
]);

export { announce, showToast };

export class Renderer {
  #stageCanvas;
  #stageHandAlert;
  #lastScheduleKey = "";

  constructor() {
    this.#stageCanvas = new ClockStageCanvas(byId("stage-canvas"));
    this.#stageHandAlert = new StageHandAlert();
  }

  invalidateStage() {
    this.#stageCanvas.invalidate();
  }

  render(state, previous = null) {
    this.#syncTheme(state);
    this.#syncViews(state);
    this.#syncModals(state, previous);
    this.#syncProfile(state);
    this.#syncConnection(state);
    this.#syncStageHud(state);
    const showHandAlert =
      state.view === "stage" &&
      state.connection === "online" &&
      !(state.loginModalOpen && state.controlAvailable);
    this.#stageHandAlert.render(showHandAlert ? state.live.handAlert : null, {
      immediate: !showHandAlert || Boolean(previous?.authenticated && !state.authenticated),
    });
    this.#syncStickyVisibility(state);
    this.#syncMeetingBar(state);
    this.#syncScheduleList(state);
    this.renderLiveFrame(state);
  }

  renderLiveFrame(state, monotonicNowMs = performance.now()) {
    if (state.view === "boot" || state.view === "setup") return;

    const evaluated = evaluateLiveSnapshot(
      state.live,
      monotonicNowMs,
      state.liveAnchorMonotonicMs,
    );
    const model = buildRenderModel(
      state.live,
      monotonicNowMs,
      state.liveAnchorMonotonicMs,
    );

    if (state.view === "stage") {
      this.#stageCanvas.render(model);
      this.#stageHandAlert.renderTime(evaluated, model);
    }

    const partPill = byId("stage-part-pill");
    if (evaluated.active && evaluated.activePartTitle) {
      partPill.hidden = false;
      partPill.textContent = evaluated.activePartTitle;
      partPill.dataset.section = evaluated.activeSection || "opening_comments";
    } else {
      partPill.hidden = true;
      partPill.textContent = "";
      delete partPill.dataset.section;
    }

    const srReadout = byId("stage-sr-readout");
    if (evaluated.active) {
      srReadout.textContent = t("stage.timerActiveLabel", {
        title: evaluated.activePartTitle || t("app.timer"),
        time: formatPartLiveClock(evaluated.displaySeconds),
      });
    } else {
      srReadout.textContent = `${t("stage.clockLabel")}: ${model.clock.primary_text}`;
    }

    if (state.view === "control") {
      this.#renderHero(state, evaluated);
      this.#renderStickyDock(state, evaluated);
      this.#renderRunningPartPills(evaluated);
    }
  }

  #syncTheme(state) {
    const scheme = state.profile.theme === "light" ? "light" : "dark";
    if (document.documentElement.dataset.theme !== scheme) {
      document.documentElement.dataset.theme = scheme;
    }
    const themeMeta = document.querySelector('meta[name="theme-color"]');
    if (themeMeta) {
      const targetColor =
        state.view === "stage"
          ? "#000000"
          : scheme === "light"
            ? "#e8edf3"
            : "#0d1117";
      if (themeMeta.getAttribute("content") !== targetColor) {
        themeMeta.setAttribute("content", targetColor);
      }
    }
  }

  #syncViews(state) {
    byId("boot-view").hidden = state.view !== "boot";
    byId("stage-view").hidden = state.view !== "stage";
    byId("control-view").hidden = state.view !== "control";
    if (state.view !== "setup") {
      byId("setup-view").hidden = true;
    }
    const skipLink = document.querySelector(".skip-link");
    if (skipLink) {
      skipLink.setAttribute(
        "href",
        state.view === "control"
          ? "#main-content"
          : state.view === "setup"
            ? "#setup-view"
            : "#stage-surface",
      );
    }
  }

  #syncModals(state, previous) {
    const loginModal = byId("login-modal");
    const shouldShowLogin = state.loginModalOpen && state.controlAvailable;
    const wasShowingLogin = Boolean(
      previous?.loginModalOpen && previous?.controlAvailable,
    );
    loginModal.hidden = !shouldShowLogin;
    if (shouldShowLogin && !wasShowingLogin) {
      window.setTimeout(() => byId("username")?.focus(), 0);
    }

    const durationModal = byId("duration-modal");
    const shouldShowDuration = Boolean(state.durationModal.open && state.authenticated);
    const wasShowingDuration = Boolean(
      previous?.durationModal?.open && previous?.authenticated,
    );
    durationModal.hidden = !shouldShowDuration;

    const modalOpen = Boolean(shouldShowLogin || shouldShowDuration);
    for (const id of ["boot-view", "stage-view", "control-view", "setup-view"]) {
      const view = byId(id);
      if (view) view.inert = modalOpen;
    }
    const skipLink = document.querySelector(".skip-link");
    if (skipLink) skipLink.inert = modalOpen;

    if (shouldShowDuration && !wasShowingDuration) {
      byId("duration-modal-title").textContent = t("part.editDuration", {
        title: state.durationModal.title,
      });
      const input = byId("duration-input");
      input.value = state.durationModal.initialText;
      byId("duration-error").hidden = true;
      window.setTimeout(() => {
        input.focus();
        input.select();
      }, 0);
    } else if (!shouldShowDuration && wasShowingDuration) {
      const closedPartId = previous?.durationModal?.partId ?? "";
      window.setTimeout(() => {
        const list = byId("schedule-list");
        const target =
          (closedPartId &&
            (list?.querySelector(
              `[data-action="edit-duration"][data-part-id="${CSS.escape(closedPartId)}"]`,
            ) ||
              list?.querySelector(`[data-part-id="${CSS.escape(closedPartId)}"]`))) ||
          byId("hero-expand-button");
        target?.focus();
      }, 0);
    }
  }

  #syncProfile(state) {
    const subtitle = state.profile.displayName || t("app.timer");
    byId("stage-profile-name").textContent = subtitle;
    byId("control-profile-name").textContent = subtitle;
  }

  #syncConnection(state) {
    const labelKey = connectionLabelKey(state.connection);
    const labelText = t(labelKey);
    for (const [chipId, labelId] of [
      ["stage-connection-chip", "stage-connection-label"],
      ["control-connection-chip", "control-connection-label"],
    ]) {
      const chip = byId(chipId);
      chip.dataset.state = state.connection;
      byId(labelId).textContent = labelText;
    }
    byId("reconnect-banner").hidden =
      state.view !== "control" ||
      (state.connection !== "reconnecting" && state.connection !== "offline");
    byId("login-offline").hidden = state.connection !== "offline";
  }

  #syncStageHud(state) {
    const stageView = byId("stage-view");
    stageView.dataset.hudVisible = state.hudVisible ? "true" : "false";

    byId("stage-signin-button").hidden =
      state.authenticated || !state.controlAvailable;
    byId("stage-control-button").hidden =
      !state.authenticated || !state.controlAvailable;

    const fsButton = byId("stage-fullscreen-button");
    const canFullscreen = Boolean(
      document.fullscreenEnabled || document.webkitFullscreenEnabled,
    );
    fsButton.hidden = !canFullscreen;
    if (canFullscreen) {
      const isFullscreen = Boolean(
        document.fullscreenElement || document.webkitFullscreenElement,
      );
      setUiIcon(
        "stage-fullscreen-icon",
        isFullscreen ? "fullscreen-exit" : "fullscreen",
      );
      fsButton.setAttribute(
        "aria-label",
        t(isFullscreen ? "stage.exitFullscreen" : "stage.enterFullscreen"),
      );
    }
  }

  #renderHero(state, evaluated) {
    const heroCard = byId("hero-card");
    const isInteractive = isControlInteractive(state);
    const caps = state.schedule.capabilities;

    heroCard.dataset.state = evaluated.active ? evaluated.state : "idle";
    heroCard.dataset.overrun = evaluated.active && evaluated.overrun ? "true" : "false";
    if (evaluated.active && evaluated.activeSection) {
      heroCard.dataset.section = evaluated.activeSection;
    } else {
      delete heroCard.dataset.section;
    }

    const eyebrow = byId("hero-eyebrow");
    const overrunBadge = byId("hero-overrun-badge");
    const title = byId("hero-title");
    const wallClockPill = byId("hero-wall-clock-pill");
    const primaryTime = byId("hero-primary-time");
    const sideTime = byId("hero-side-time");
    const ampmTime = byId("hero-ampm-time");
    const suffixTime = byId("hero-suffix-time");

    const [wallPrimary, wallSecs, wallAmpm] = formatWallClock(
      evaluated.wallClockEpoch,
      evaluated.clockConfig,
      evaluated.utcOffsetMinutes,
    );
    const wallFormatted = [
      wallSecs ? `${wallPrimary}:${wallSecs}` : wallPrimary,
      wallAmpm,
    ]
      .filter(Boolean)
      .join(" ");

    let secsText = "";
    let ampmText = "";
    if (evaluated.active) {
      eyebrow.textContent = t(
        evaluated.state === "stopped" ? "hero.stoppedEyebrow" : "hero.runningEyebrow",
      );
      overrunBadge.hidden = !evaluated.overrun;
      title.textContent = evaluated.activePartTitle || t("app.timer");
      wallClockPill.hidden = false;
      byId("hero-wall-clock").textContent = wallFormatted;

      const [stagePrimary, stageSecs] = formatStageDuration(evaluated.displaySeconds);
      primaryTime.textContent = stagePrimary;
      secsText = stageSecs || "";
    } else {
      eyebrow.textContent = t("hero.clockEyebrow");
      overrunBadge.hidden = true;
      title.textContent = t("hero.idleTitle");
      wallClockPill.hidden = true;
      primaryTime.textContent = wallPrimary;
      secsText = wallSecs || "";
      ampmText = wallAmpm || "";
    }

    ampmTime.textContent = ampmText;
    ampmTime.hidden = !ampmText;
    suffixTime.textContent = secsText;
    suffixTime.hidden = !secsText;
    sideTime.hidden = !secsText && !ampmText;

    const canNudge = Boolean(
      evaluated.active && evaluated.state === "running" && caps.controlParts,
    );
    const canStop = Boolean(
      evaluated.active && evaluated.state === "running" && caps.controlParts,
    );
    byId("hero-nudge-group").hidden = !canNudge;
    byId("hero-minus-second").disabled = !canNudge || !isInteractive;
    byId("hero-plus-second").disabled = !canNudge || !isInteractive;

    const stopBtn = byId("hero-stop-button");
    stopBtn.hidden = !canStop;
    stopBtn.disabled = !isInteractive;
    byId("hero-actions").hidden = !canNudge && !canStop;
  }

  #syncStickyVisibility(state) {
    const stickyShell = byId("sticky-timer-bar");
    const stuck = Boolean(state.view === "control" && state.heroStuck);
    if (!stuck && stickyShell.contains(document.activeElement)) {
      byId("hero-expand-button")?.focus({ preventScroll: true });
    }
    stickyShell.dataset.stuck = stuck ? "true" : "false";
    stickyShell.inert = !stuck;
    stickyShell.setAttribute("aria-hidden", stuck ? "false" : "true");
  }

  #renderStickyDock(state, evaluated) {
    const stickyShell = byId("sticky-timer-bar");
    const isInteractive = isControlInteractive(state);
    const caps = state.schedule.capabilities;

    stickyShell.dataset.state = evaluated.active ? evaluated.state : "idle";
    stickyShell.dataset.overrun =
      evaluated.active && evaluated.overrun ? "true" : "false";
    if (evaluated.active && evaluated.activeSection) {
      stickyShell.dataset.section = evaluated.activeSection;
    } else {
      delete stickyShell.dataset.section;
    }

    const primaryTime = byId("sticky-primary-time");
    const suffixTime = byId("sticky-suffix-time");
    const stickyTitle = byId("sticky-title");

    if (evaluated.active) {
      const [stagePrimary, stageSecs] = formatStageDuration(evaluated.displaySeconds);
      primaryTime.textContent = stagePrimary;
      suffixTime.textContent = stageSecs || "";
      suffixTime.hidden = !stageSecs;
      stickyTitle.textContent = evaluated.activePartTitle || t("app.timer");
    } else {
      const [wallPrimary, wallSecs, wallAmpm] = formatWallClock(
        evaluated.wallClockEpoch,
        evaluated.clockConfig,
        evaluated.utcOffsetMinutes,
      );
      const wallSuffix = [wallSecs, wallAmpm].filter(Boolean).join(" ");
      primaryTime.textContent = wallPrimary;
      suffixTime.textContent = wallSuffix;
      suffixTime.hidden = !wallSuffix;
      stickyTitle.textContent = t("hero.idleTitle");
    }

    const canControlRunning = Boolean(
      evaluated.active && evaluated.state === "running" && caps.controlParts,
    );
    byId("sticky-nudge-group").hidden = !canControlRunning;
    byId("sticky-minus-second").disabled = !canControlRunning || !isInteractive;
    byId("sticky-plus-second").disabled = !canControlRunning || !isInteractive;

    const stickyStop = byId("sticky-stop-button");
    stickyStop.hidden = !canControlRunning;
    stickyStop.disabled = !isInteractive;
  }

  #renderRunningPartPills(evaluated) {
    const liveNodes = document.querySelectorAll("[data-live-part-id]");
    for (const pill of liveNodes) {
      const partId = pill.dataset.livePartId;
      if (evaluated.active && evaluated.activePartId === partId) {
        pill.textContent = formatPartLiveClock(evaluated.displaySeconds);
        const isRunning = evaluated.state === "running";
        const overrunValue = isRunning && evaluated.overrun ? "true" : "false";
        const badge = pill.closest(".part-live-badge");
        if (badge) {
          badge.dataset.running = isRunning ? "true" : "false";
          badge.dataset.overrun = overrunValue;
        }
        const row = pill.closest(".part-row");
        if (row) {
          row.dataset.overrun = overrunValue;
        }
      }
    }
  }

  #syncMeetingBar(state) {
    if (state.view !== "control") return;
    const isInteractive = isControlInteractive(state);
    const { schedule } = state;
    const caps = schedule.capabilities;

    const midweekBtn = byId("meeting-midweek");
    const weekendBtn = byId("meeting-weekend");
    midweekBtn.setAttribute(
      "aria-pressed",
      schedule.meetingType === "midweek" ? "true" : "false",
    );
    weekendBtn.setAttribute(
      "aria-pressed",
      schedule.meetingType === "weekend" ? "true" : "false",
    );
    midweekBtn.disabled = !isInteractive || !caps.switchMeetingType;
    weekendBtn.disabled = !isInteractive || !caps.switchMeetingType;

    byId("week-label").textContent = schedule.weekLabel || schedule.weekMonday || "—";
    byId("week-not-current").hidden = Boolean(schedule.isCurrentWeek);

    const prevBtn = byId("week-prev");
    const nextBtn = byId("week-next");
    const homeBtn = byId("week-current");

    prevBtn.hidden = !caps.navigateWeeks;
    nextBtn.hidden = !caps.navigateWeeks;
    homeBtn.hidden = !caps.navigateWeeks;

    prevBtn.disabled = !isInteractive || !caps.navigateWeeks;
    nextBtn.disabled = !isInteractive || !caps.navigateWeeks;
    homeBtn.disabled =
      !isInteractive || !caps.navigateWeeks || Boolean(schedule.isCurrentWeek);
  }

  #syncScheduleList(state) {
    if (state.view !== "control") return;
    const isInteractive = isControlInteractive(state);
    const caps = state.schedule.capabilities;
    const key = [
      currentLocale(),
      state.schedule.scheduleRevision,
      state.schedule.meetingType,
      state.schedule.weekMonday,
      isInteractive ? "1" : "0",
      state.live.activePartId || "",
      state.live.state,
      JSON.stringify(caps),
      state.schedule.parts
        .map(
          (p) =>
            `${p.id}:${p.state}:${p.plannedSeconds}:${p.durationEditable ? 1 : 0}:${p.sectionCount}:${p.startedLabel}:${p.resultLabel}`,
        )
        .join("|"),
    ].join("::");
    if (key === this.#lastScheduleKey) return;
    this.#lastScheduleKey = key;

    const container = byId("schedule-list");
    const focusDescriptor = captureScheduleFocus(container);

    if (state.schedule.parts.length === 0) {
      container.replaceChildren(
        element("p", {
          className: "schedule-empty",
          text: t("schedule.empty"),
        }),
      );
      return;
    }

    const fragment = document.createDocumentFragment();
    const groups = groupPartsBySection(state.schedule.parts);
    for (const group of groups) {
      fragment.append(buildSectionCard(group, { isInteractive, caps }));
    }
    container.replaceChildren(fragment);
    restoreScheduleFocus(container, focusDescriptor);
  }
}

const PRIMARY_PART_ACTIONS = new Set(["start-part", "stop-part", "reset-part"]);

function captureScheduleFocus(container) {
  const active = document.activeElement;
  if (!active || !container.contains(active)) return null;
  const button = active.closest("button[data-action]");
  if (!button) return null;
  return {
    action: button.dataset.action || "",
    partId: button.dataset.partId || "",
    delta: button.dataset.delta || "",
    section: button.dataset.section || "",
    text: button.textContent || "",
  };
}

function restoreScheduleFocus(container, descriptor) {
  if (!descriptor) return;
  let candidate = null;
  if (PRIMARY_PART_ACTIONS.has(descriptor.action) && descriptor.partId) {
    candidate = container.querySelector(
      `.part-row[data-part-id="${CSS.escape(descriptor.partId)}"] .part-action-btn:not(:disabled)`,
    );
  } else if (descriptor.action === "adjust-part" && descriptor.partId && descriptor.delta) {
    candidate = container.querySelector(
      `button[data-action="adjust-part"][data-part-id="${CSS.escape(descriptor.partId)}"][data-delta="${CSS.escape(descriptor.delta)}"]:not(:disabled)`,
    );
  } else if (descriptor.action === "edit-duration" && descriptor.partId) {
    candidate = container.querySelector(
      `button[data-action="edit-duration"][data-part-id="${CSS.escape(descriptor.partId)}"]:not(:disabled)`,
    );
  } else if (descriptor.action === "section-count" && descriptor.section) {
    for (const btn of container.querySelectorAll(
      `button[data-action="section-count"][data-section="${CSS.escape(descriptor.section)}"]:not(:disabled)`,
    )) {
      if (btn.textContent === descriptor.text) {
        candidate = btn;
        break;
      }
    }
  }
  candidate?.focus({ preventScroll: true });
}

function isControlInteractive(state) {
  return Boolean(
    state.authenticated &&
      state.controlAvailable &&
      (state.connection === "online" || state.connection === "syncing"),
  );
}

function connectionLabelKey(status) {
  if (status === "online") return "connection.online";
  if (status === "syncing") return "connection.syncing";
  if (status === "reconnecting") return "connection.reconnecting";
  if (status === "offline") return "connection.offline";
  return "connection.connecting";
}

function groupPartsBySection(parts) {
  const groups = [];
  for (const part of parts) {
    if (!part.showSectionHeader) {
      groups.push({
        section: part.section,
        sectionKey: part.sectionKey,
        headerTitle: "",
        standalone: true,
        parts: [part],
      });
      continue;
    }
    const last = groups[groups.length - 1];
    if (last && !last.standalone && last.section === part.section) {
      last.parts.push(part);
    } else {
      groups.push({
        section: part.section,
        sectionKey: part.sectionKey,
        headerTitle: part.sectionHeaderTitle,
        standalone: false,
        parts: [part],
      });
    }
  }
  return groups;
}

function buildSectionCard(group, { isInteractive, caps }) {
  const firstPart = group.parts[0];
  const card = element("section", {
    className: `section-card${group.standalone ? " section-card--standalone" : ""}`,
    dataset: { section: group.section },
  });

  if (!group.standalone) {
    const header = element("header", { className: "section-header" });
    const heading = element("div", { className: "section-heading" });

    if (SECTION_ICONS.has(group.sectionKey)) {
      const iconBadge = element("span", {
        className: "section-icon-badge",
        attrs: { "aria-hidden": "true" },
      });
      iconBadge.append(uiIcon(`sec-${group.sectionKey}`));
      heading.append(iconBadge);
    }

    heading.append(
      element("h2", {
        className: "section-title",
        text: group.headerTitle || group.sectionKey,
      }),
    );
    header.append(heading);

    const tools = element("div", { className: "section-tools" });
    if (firstPart.sectionTotalLabel) {
      tools.append(
        element("span", {
          className: "section-total-pill",
          text: firstPart.sectionTotalLabel,
        }),
      );
    }

    if (firstPart.configurableCount && caps.setSectionCount) {
      const sectionLabel = group.headerTitle || group.sectionKey;
      const stepper = element("div", {
        className: "count-stepper",
        attrs: { role: "group", "aria-label": t("schedule.partsCount") },
      });
      const decBtn = element("button", {
        className: "stepper-button",
        text: "−",
        attrs: {
          type: "button",
          "aria-label": t("schedule.decreaseParts", { section: sectionLabel }),
        },
        dataset: {
          action: "section-count",
          section: group.section,
          count: String(Math.max(1, firstPart.sectionCount - 1)),
        },
      });
      decBtn.disabled = !isInteractive || firstPart.sectionCount <= 1;

      const countText = element("span", {
        className: "stepper-value",
        text: String(firstPart.sectionCount),
      });

      const incBtn = element("button", {
        className: "stepper-button",
        text: "+",
        attrs: {
          type: "button",
          "aria-label": t("schedule.increaseParts", { section: sectionLabel }),
        },
        dataset: {
          action: "section-count",
          section: group.section,
          count: String(Math.min(8, firstPart.sectionCount + 1)),
        },
      });
      incBtn.disabled = !isInteractive || firstPart.sectionCount >= 8;

      stepper.append(decBtn, countText, incBtn);
      tools.append(stepper);
    }

    header.append(tools);
    card.append(header);
  }

  const list = element("div", { className: "section-parts" });
  for (const part of group.parts) {
    list.append(buildPartRow(part, { isInteractive, caps }));
  }
  card.append(list);
  return card;
}

function buildPartRow(part, { isInteractive, caps }) {
  const row = element("article", {
    className: "part-row",
    dataset: {
      partId: part.id,
      partState: part.state,
      overrun: "false",
    },
  });

  const info = element("div", { className: "part-info" });
  info.append(
    element("span", {
      className: "part-number",
      text: String(part.displayNumber),
    }),
  );

  const copy = element("div", { className: "part-copy" });
  copy.append(
    element("strong", {
      className: "part-title",
      text: part.title,
    }),
  );

  const hasStarted = Boolean(part.startedLabel);
  const hasResult = Boolean(part.resultLabel);
  if (hasStarted || hasResult) {
    const submeta = element("span", { className: "part-submeta" });
    if (hasStarted) {
      submeta.append(
        element("span", {
          className: "part-meta-started",
          text: t("part.startedAt", { time: part.startedLabel }),
        }),
      );
    }
    if (hasStarted && hasResult) {
      submeta.append(
        element("span", {
          className: "part-meta-sep",
          text: " · ",
          attrs: { "aria-hidden": "true" },
        }),
      );
    }
    if (hasResult) {
      submeta.append(
        element("span", {
          className: "part-meta-recorded",
          text: t("part.recordedDuration", { duration: part.resultLabel }),
        }),
      );
    }
    copy.append(submeta);
  }
  info.append(copy);
  row.append(info);

  const controls = element("div", { className: "part-controls" });
  const isIdle = part.state === "idle";
  const isRunning = part.state === "running";
  const isStopped = part.state === "stopped";
  const canAdjustMinutes = Boolean(
    isIdle && part.durationEditable && caps.adjustPlannedMinutes,
  );
  const canEditSeconds = Boolean(
    isIdle && part.durationEditable && caps.setPlannedSeconds,
  );

  if (isRunning) {
    const liveBadge = element("div", {
      className: "part-live-badge",
      dataset: {
        running: "true",
        overrun: "false",
      },
    });
    liveBadge.append(
      element("span", {
        className: "part-live-dot",
        attrs: { "aria-hidden": "true" },
      }),
      element("span", {
        className: "part-live-time",
        text: part.plannedLabel,
        dataset: { livePartId: part.id },
      }),
    );
    controls.append(liveBadge);
  } else if (canAdjustMinutes) {
    const durationCluster = element("div", { className: "duration-cluster" });
    const minusMin = element("button", {
      className: "minute-button",
      text: t("part.minusMinute"),
      attrs: {
        type: "button",
        "aria-label": t("part.minusMinuteAria", { title: part.title }),
      },
      dataset: {
        action: "adjust-part",
        partId: part.id,
        delta: "-60",
      },
    });
    minusMin.disabled = !isInteractive || part.plannedSeconds <= 60;

    const durationNode = element(canEditSeconds ? "button" : "span", {
      className: "duration-pill",
      text: part.plannedLabel,
      attrs: canEditSeconds
        ? {
            type: "button",
            "aria-label": t("part.editDuration", { title: part.title }),
          }
        : {},
      dataset: canEditSeconds
        ? {
            action: "edit-duration",
            partId: part.id,
            title: part.title,
            plannedSeconds: String(part.plannedSeconds),
          }
        : {},
    });
    if (canEditSeconds) {
      durationNode.disabled = !isInteractive;
    }

    const plusMin = element("button", {
      className: "minute-button",
      text: t("part.plusMinute"),
      attrs: {
        type: "button",
        "aria-label": t("part.plusMinuteAria", { title: part.title }),
      },
      dataset: {
        action: "adjust-part",
        partId: part.id,
        delta: "60",
      },
    });
    plusMin.disabled = !isInteractive;

    durationCluster.append(minusMin, durationNode, plusMin);
    controls.append(durationCluster);
  } else if (canEditSeconds) {
    const durationButton = element("button", {
      className: "duration-pill duration-pill--standalone",
      text: part.plannedLabel,
      attrs: {
        type: "button",
        "aria-label": t("part.editDuration", { title: part.title }),
      },
      dataset: {
        action: "edit-duration",
        partId: part.id,
        title: part.title,
        plannedSeconds: String(part.plannedSeconds),
      },
    });
    durationButton.disabled = !isInteractive;
    controls.append(durationButton);
  } else {
    controls.append(
      element("span", {
        className: "part-duration-badge",
        text: part.plannedLabel,
      }),
    );
  }

  if (caps.controlParts) {
    const actions = element("div", { className: "part-actions" });
    if (isRunning) {
      const stopBtn = element("button", {
        className: "button button--danger button--compact part-action-btn",
        attrs: { type: "button" },
        dataset: {
          action: "stop-part",
          partId: part.id,
        },
      });
      stopBtn.disabled = !isInteractive;
      stopBtn.append(cssIcon("stop"), element("span", { text: t("part.stop") }));
      actions.append(stopBtn);
    } else if (isStopped) {
      const resetBtn = element("button", {
        className: "button button--reset button--compact part-action-btn",
        attrs: { type: "button" },
        dataset: {
          action: "reset-part",
          partId: part.id,
        },
      });
      resetBtn.disabled = !isInteractive;
      resetBtn.append(uiIcon("reset"), element("span", { text: t("part.reset") }));
      actions.append(resetBtn);
    } else {
      const startBtn = element("button", {
        className: "button button--primary button--compact part-action-btn",
        attrs: { type: "button" },
        dataset: {
          action: "start-part",
          partId: part.id,
        },
      });
      startBtn.disabled = !isInteractive;
      startBtn.append(cssIcon("play"), element("span", { text: t("part.start") }));
      actions.append(startBtn);
    }
    controls.append(actions);
  }

  row.append(controls);
  return row;
}
