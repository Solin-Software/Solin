import { byId, element, setUiIcon, uiIcon } from "../../shared/scripts/dom.js";
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


import { nextTimerPart, selectedTimerPart } from "./state.js";

import { applySectionColor, sectionMark } from "./section-mark.js";

export class Renderer {
  #stageCanvas;
  #stageHandAlert;
  #lastScheduleKey = null;

  constructor() {
    this.#stageCanvas = new ClockStageCanvas(byId("stage-canvas"));
    this.#stageHandAlert = new StageHandAlert();
  }

  invalidateStage() {
    this.#stageCanvas.invalidate();
  }

  render(state, previous = null) {
    const focused = document.activeElement?.id;
    if (previous?.view === "control" && state.view !== "control") syncPacingLine(null);
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
    this.#syncCatalog(state);
    this.#syncScheduleList(state);
    this.renderLiveFrame(state);
    if (state.view === "control" && previous?.live.state !== state.live.state) {
      this.#restoreTimerActionFocus(focused, state.live.state);
    }
  }

  #restoreTimerActionFocus(focused, state) {
    const running = state === "running";
    const from = running ? "start" : "stop";
    const to = running ? "stop" : "start";
    for (const group of ["hero", "sticky"]) {
      if (focused === `${group}-${from}-button`) {
        byId(`${group}-${to}-button`).focus({ preventScroll: true });
      }
    }
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
      this.#renderPacing(state, evaluated);
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

    for (const id of ["boot-view", "stage-view", "control-view", "setup-view"]) {
      const view = byId(id);
      if (view) view.inert = shouldShowLogin;
    }
    const skipLink = document.querySelector(".skip-link");
    if (skipLink) skipLink.inert = shouldShowLogin;

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
    const part = selectedTimerPart(state);
    const running = evaluated.active && evaluated.state === "running";
    const showingLive = !state.browsingSchedules &&
      (running || (evaluated.active && part?.id === evaluated.activePartId));
    const stopped = !running && (part?.state === "stopped" || (showingLive && evaluated.state === "stopped"));
    const overrun = showingLive && evaluated.overrun;

    heroCard.dataset.state = running ? "running" : stopped ? "stopped" : "idle";
    heroCard.dataset.overrun = String(overrun);
    if (part || (showingLive && evaluated.activeSection)) {
      heroCard.dataset.section = part?.section || evaluated.activeSection;
    } else {
      delete heroCard.dataset.section;
    }

    const [wallPrimary, wallSecs, wallAmpm] = formatWallClock(
      evaluated.wallClockEpoch, evaluated.clockConfig, evaluated.utcOffsetMinutes,
    );
    const wallFormatted = [wallSecs ? `${wallPrimary}:${wallSecs}` : wallPrimary, wallAmpm]
      .filter(Boolean).join(" ");
    const targetSeconds = showingLive ? evaluated.plannedSeconds :
      part?.effectiveSeconds ?? part?.plannedSeconds;
    const [primary, suffix] = showingLive
      ? formatStageDuration(evaluated.displaySeconds)
      : stopped && part?.resultLabel
        ? [part.resultLabel, ""]
        : part
          ? formatStageDuration(targetSeconds)
          : [wallPrimary, wallSecs];
    const period = part || showingLive ? "" : wallAmpm;

    const eyebrow = running ? "hero.runningEyebrow" : stopped ? "hero.stoppedEyebrow" :
      part ? "workspace.selectedPart" : "hero.clockEyebrow";
    setText(byId("hero-eyebrow"), t(eyebrow));
    byId("hero-overrun-badge").hidden = !overrun;
    setText(byId("hero-title"), showingLive ? evaluated.activePartTitle || part?.title || t("app.timer") :
      part?.title ?? t(state.browsingSchedules ? "workspace.chooseSchedule" :
        state.schedule.customSchedule ? "schedule.empty" : "hero.idleTitle"));
    byId("hero-wall-clock-pill").hidden = !part && !showingLive;
    setText(byId("hero-wall-clock"), wallFormatted);
    setText(byId("hero-primary-time"), primary);
    setText(byId("hero-ampm-time"), period || "");
    byId("hero-ampm-time").hidden = !period;
    setText(byId("hero-suffix-time"), suffix || "");
    byId("hero-suffix-time").hidden = !suffix;
    byId("hero-side-time").hidden = !suffix && !period;

    byId("hero-target").hidden = !targetSeconds;
    const progress = byId("hero-progress");
    progress.hidden = !running;
    if (running) progress.value = Math.min(1, evaluated.elapsedSeconds / Math.max(1, targetSeconds));
    if (targetSeconds) {
      const plannedSeconds = part?.plannedSeconds ?? targetSeconds;
      setText(byId("hero-target-label"), targetSeconds === plannedSeconds
        ? t("workspace.plannedDuration", { duration: formatPartLiveClock(plannedSeconds) })
        : t("workspace.adjustedDuration", {
            target: formatPartLiveClock(targetSeconds),
            planned: formatPartLiveClock(plannedSeconds),
          }));
    }

    const canOperate = caps.controlParts;
    const canStart = !running && Boolean(part) && canOperate;
    const canStop = running && canOperate;
    const canReset = stopped && Boolean(part) && canOperate;
    const canEdit = !running && Boolean(part) &&
      ((caps.controlParts && caps.manageCustomSchedules) || (part.durationEditable && caps.setPlannedSeconds));
    setButton("hero-start-button", canStart, isInteractive && canStart);
    setText(byId("hero-start-label"), t(stopped ? "workspace.restart" : "part.start"));
    setButton("hero-reset-button", canReset, isInteractive && canReset);
    setButton("hero-edit-button", canEdit, isInteractive && canEdit);
    setButton("hero-stop-button", canStop, isInteractive && canStop);
    setButton("hero-adjust-button", canStop, isInteractive && canStop);
    byId("hero-adjust-button").setAttribute("aria-expanded", String(canStop && state.adjustOpen));
    byId("hero-nudge-group").hidden = !canStop || !state.adjustOpen;
    for (const sign of ["minus", "plus"]) {
      setButton(`hero-${sign}-second`, canStop, isInteractive && canStop);
    }
    byId("hero-actions").hidden = !canStart && !canStop && !canReset;

    const next = nextTimerPart(state);
    const showNext = Boolean(next && (running || next.id !== part?.id));
    byId("hero-next").hidden = !showNext;
    setButton("hero-next-button", showNext && canOperate, isInteractive && canOperate);
    setText(byId("hero-next-title"), showNext ? next.title : "");
    setText(byId("hero-next-duration"), showNext ?
      formatPartLiveClock(next.effectiveSeconds ?? next.plannedSeconds) : "");
  }

  #renderPacing(state, evaluated) {
    const schedule = state.schedule;
    const copy = state.authenticated && state.controlAvailable && state.connection === "online" &&
      !state.browsingSchedules
      ? pacingCopy(schedule, evaluated.wallClockEpoch, evaluated.clockConfig,
          evaluated.utcOffsetMinutes)
      : null;
    const { customSchedule, capabilities, pacingAvailability, pacingMode } = schedule;
    const canManage = state.authenticated && capabilities.managePacing;
    syncPacingLine(copy);
    byId("hero-pacing-panel").hidden = !copy && !canManage;
    byId("pacing-setting").hidden = !canManage;
    const select = byId("pacing-mode");
    select.disabled = !canManage || !isControlInteractive(state);
    if (select.value !== pacingMode) select.value = pacingMode;
    const canSetTime = Boolean(copy && canManage && customSchedule && capabilities.manageCustomSchedules &&
      pacingAvailability === "missing_window");
    setButton("pacing-set-time", canSetTime, canSetTime &&
      isControlInteractive(state) && evaluated.state !== "running");
  }

  #syncStickyVisibility(state) {
    const stickyShell = byId("sticky-timer-bar");
    const stuck = Boolean(state.view === "control" && state.heroStuck && !state.organizer);
    if (!stuck && stickyShell.contains(document.activeElement)) {
      byId("control-stage-button")?.focus({ preventScroll: true });
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

    const part = selectedTimerPart(state);
    const running = evaluated.active && evaluated.state === "running";
    if (evaluated.active && (running || evaluated.activePartId === part?.id)) {
      const [stagePrimary, stageSecs] = formatStageDuration(evaluated.displaySeconds);
      primaryTime.textContent = stagePrimary;
      suffixTime.textContent = stageSecs || "";
      suffixTime.hidden = !stageSecs;
      stickyTitle.textContent = evaluated.activePartTitle || t("app.timer");
    } else if (part) {
      const [primary, suffix] = part.state === "stopped" && part.resultLabel
        ? [part.resultLabel, ""]
        : formatStageDuration(part.effectiveSeconds ?? part.plannedSeconds);
      setText(primaryTime, primary);
      setText(suffixTime, suffix || "");
      suffixTime.hidden = !suffix;
      setText(stickyTitle, part.title);
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
    const canStart = !running && Boolean(part) && caps.controlParts;
    setButton("sticky-start-button", canStart, canStart && isInteractive);
    setText(byId("sticky-start-label"), t(part?.state === "stopped" ? "workspace.restart" : "part.start"));
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
    const isInteractive = isControlInteractive(state) && state.live.state !== "running";
    const { schedule } = state;
    const caps = schedule.capabilities;
    const custom = Boolean(schedule.customSchedule || state.browsingSchedules);
    byId("meeting-bar").hidden = custom;
    if (custom) return;

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


  #syncCatalog(state) {
    const selector = byId("custom-selector");
    if (state.view !== "control") {
      if (!state.authenticated && selector.options.length) selector.replaceChildren();
      return;
    }
    const { schedule, catalog, browsingSchedules, organizer } = state;
    const visible = schedule.capabilities.manageCustomSchedules;
    const current = schedule.customSchedule;
    const custom = visible && Boolean(current || browsingSchedules);
    const canChange = isControlInteractive(state) && state.live.state !== "running" &&
      !organizer && !state.scheduleSwitch;
    const canEdit = state.connection === "online" && state.live.state !== "running" &&
      !organizer?.saving && !organizer?.stale;
    byId("control-stage-button").disabled = Boolean(organizer);
    byId("control-container").dataset.organizing = String(Boolean(organizer));
    byId("organizer-toolbar").hidden = !organizer;
    byId("organizer-add-part").disabled = !canEdit;
    byId("organizer-add-section").disabled = !canEdit;
    byId("organizer-save").disabled = !canEdit || !organizer?.dirty;
    byId("organizer-cancel").disabled = Boolean(organizer?.saving);
    byId("organizer-error").hidden = !organizer?.error;
    setText(byId("organizer-error"), organizer?.error ?? "");
    byId("schedule-tabs").hidden = !visible;
    byId("tab-meetings").disabled = !canChange;
    byId("tab-schedules").disabled = !canChange;
    byId("tab-meetings").setAttribute("aria-pressed", String(!custom));
    byId("tab-schedules").setAttribute("aria-pressed", String(custom));
    byId("custom-bar").hidden = !custom || (!current && !catalog.schedules.length);
    setButton("custom-organize", Boolean(current) && !organizer, canChange);
    setButton("custom-actions", Boolean(current), canChange);

    selector.disabled = !canChange || (!current && (catalog.status !== "ready" || !catalog.schedules.length));
    if (custom) {
      const schedules = catalog.schedules.map((item) => item.id === current?.id ? current : item);
      if (current && !schedules.some((item) => item.id === current.id)) schedules.unshift(current);
      const options = schedules.map((item) => [item.id, item.title]);
      if (!current) options.unshift(["", t("workspace.chooseSchedule")]);
      if (options.length !== selector.options.length || options.some(([id, title], index) =>
        selector.options[index]?.value !== id || selector.options[index]?.textContent !== title)) {
        selector.replaceChildren(...options.map(([value, text]) => {
          const option = element("option", { text, attrs: { value } });
          option.disabled = !value;
          return option;
        }));
      }
      if (selector.value !== (current?.id ?? "")) selector.value = current?.id ?? "";
    } else if (selector.options.length) {
      selector.replaceChildren();
    }

    const message = !custom ? "" :
      catalog.status === "error" ? "workspace.catalogFailed" :
      current ? "" :
      catalog.status !== "ready" ? "workspace.loadingSchedules" :
      !catalog.schedules.length ? "workspace.noSchedules" : "";
    byId("catalog-message").hidden = !message && !(custom && !current);
    setText(byId("catalog-status"), message ? t(message) : "");
    setButton("catalog-retry", custom && catalog.status === "error", canChange);
    setButton("catalog-new", custom && !current, canChange && catalog.status === "ready");
  }

  #syncScheduleList(state) {
    if (state.view !== "control") {
      if (this.#lastScheduleKey) byId("schedule-list").replaceChildren();
      this.#lastScheduleKey = null;
      return;
    }
    const isInteractive = isControlInteractive(state);
    const { schedule, browsingSchedules, organizer } = state;
    const caps = schedule.capabilities;
    const part = selectedTimerPart(state);
    const editing = Boolean(organizer);
    const canEdit = editing && state.connection === "online" && state.live.state !== "running" &&
      !organizer.saving && !organizer.stale;
    const key = [currentLocale(), editing ? organizer : schedule, isInteractive, canEdit,
      part?.id, state.live.state, browsingSchedules];
    if (this.#lastScheduleKey?.every((value, index) => value === key[index])) return;
    this.#lastScheduleKey = key;

    const container = byId("schedule-list");
    if (editing) container.dataset.dragContainer = "";
    else delete container.dataset.dragContainer;
    const focus = document.activeElement;
    const hadFocus = container.contains(focus);
    const { action, partId, section, count } = hadFocus ? focus.dataset : {};
    const custom = schedule.customSchedule;
    const waitingForSelection = browsingSchedules && !custom;
    const groups = editing ? groupCustomParts(draftParts(schedule.parts, organizer.entries), organizer.entries) :
      waitingForSelection ? [] : custom
      ? groupCustomParts(schedule.parts, custom.entries)
      : groupPartsBySection(schedule.parts);
    if (!groups.length && !waitingForSelection) {
      container.replaceChildren(element("p", { className: "schedule-empty", text: t("schedule.empty") }));
      if (hadFocus) byId("control-stage-button").focus({ preventScroll: true });
      return;
    }

    const fragment = document.createDocumentFragment();
    for (const group of groups) {
      fragment.append(buildSectionCard(group, {
        isInteractive,
        caps,
        editing,
        canEdit,
        selectedId: part?.id,
        canSelect: state.live.state !== "running",
      }));
    }
    container.replaceChildren(fragment);
    if (hadFocus) {
      const newFocus = [...container.querySelectorAll("button:enabled")].find((button) =>
        action === button.dataset.action && partId === button.dataset.partId &&
        section === button.dataset.section && (count === undefined || count === button.dataset.count));
      const current = part?.id && container.querySelector(
        `button[data-action="select-part"][data-part-id="${CSS.escape(part.id)}"]`);
      (newFocus || current || byId("control-stage-button")).focus({ preventScroll: true });
    }
  }
}

function draftParts(parts, entries) {
  const persisted = new Map(parts.map((part) => [part.id, part]));
  return entries.flatMap((entry) =>
    entry.type === "section" ? entry.parts.map((part) => [part, entry.id]) : [[entry, ""]]
  ).map(([entry, section], index) => ({
    state: "idle", ...persisted.get(entry.id), id: entry.id, title: entry.title, section,
    effectiveSeconds: null,
    displayNumber: index + 1, plannedSeconds: entry.plannedSeconds,
    plannedLabel: formatPartLiveClock(entry.plannedSeconds),
  }));
}

function isControlInteractive(state) {
  return Boolean(
    state.authenticated &&
      state.controlAvailable &&
      (state.connection === "online" || state.connection === "syncing"),
  );
}


function pacingCopy(schedule, now, clockConfig, utcOffsetMinutes) {
  if (schedule.pacingMode === "off") return null;
  const wallTime = (epoch, offset = utcOffsetMinutes) => {
    const [clock, , period] = formatWallClock(epoch, clockConfig, offset);
    return period ? `${clock} ${period}` : clock;
  };
  const configuredTime = (time) => {
    const [hours, minutes] = time.split(":").map(Number);
    return wallTime(hours * 3600 + minutes * 60, 0);
  };
  const config = schedule.customSchedule?.pacingConfig;
  const configuredWindow = config?.start_time && config?.end_time
    ? [configuredTime(config.start_time), configuredTime(config.end_time)] : null;
  if (schedule.pacingAvailability !== "ready" || !schedule.pacing) {
    const labels = {
      missing_window: schedule.customSchedule && schedule.capabilities.managePacing && schedule.capabilities.manageCustomSchedules
        ? "pacing.missingWindow" : "pacing.configureInSolin",
      awaiting_reset: "pacing.awaitingReset",
      no_projection: "pacing.noProjection",
    };
    const key = labels[schedule.pacingAvailability];
    return key ? { tone: "neutral", label: t(key), window: configuredWindow } : null;
  }
  const pacing = schedule.pacing;
  const finish = pacing.complete ? pacing.projection_anchor_epoch :
    Math.max(now, pacing.projection_anchor_epoch) + pacing.projection_tail_seconds;
  const difference = finish - pacing.end_epoch;
  const tone = Math.abs(difference) <= pacing.tolerance_seconds ? "onTime" :
    difference > 0 ? "behind" : "ahead";
  return {
    tone,
    label: t(`pacing.${tone}`, { duration: formatPartLiveClock(Math.abs(difference)) }),
    detail: t("pacing.projectedFinish", { finish: wallTime(finish) }),
    window: configuredWindow ?? (Number.isFinite(pacing.start_epoch)
      ? [wallTime(pacing.start_epoch), wallTime(pacing.end_epoch)] : null),
  };
}

function syncPacingLine(copy) {
  const line = byId("hero-pacing");
  line.hidden = !copy;
  line.dataset.tone = copy?.tone ?? "";
  setText(byId("pacing-status"), copy?.label ?? "");
  setText(byId("pacing-forecast"), copy?.detail ?? "");
  const windowInfo = byId("pacing-window");
  const [start, end] = copy?.window ?? [];
  windowInfo.hidden = !start;
  const windowLabel = start ? `${t("scheduleEditor.start")} ${start} · ${t("scheduleEditor.end")} ${end}` : "";
  if (windowInfo.title !== windowLabel) {
    windowInfo.title = windowLabel;
    windowInfo.setAttribute("aria-label", windowLabel);
  }
  setText(byId("pacing-window-time"), start ? `${start}–${end}` : "");
}

function setText(node, value) {
  if (node.textContent !== value) node.textContent = value;
}

function setButton(id, visible, enabled) {
  const button = byId(id);
  button.hidden = !visible;
  button.disabled = !enabled;
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
        icon: part.sectionIconKey || part.sectionKey,
        headerTitle: part.sectionHeaderTitle,
        standalone: false,
        parts: [part],
      });
    }
  }
  return groups;
}

function groupCustomParts(parts, entries) {
  const byPartId = new Map(parts.map((part) => [part.id, part]));
  const groups = [];
  for (const entry of entries) {
    if (entry.type === "section") {
      groups.push({
        section: entry.id,
        icon: entry.icon,
        color: entry.color,
        headerTitle: entry.title,
        standalone: false,
        parts: entry.parts.map((part) => byPartId.get(part.id)).filter(Boolean),
      });
    } else {
      const part = byPartId.get(entry.id);
      if (part) groups.push({ section: part.section, standalone: true, parts: [part] });
    }
  }
  return groups;
}

function buildSectionCard(group, { isInteractive, caps, selectedId, canSelect, editing, canEdit }) {
  const firstPart = group.parts[0];
  const card = element("section", {
    className: `section-card${group.standalone ? " section-card--standalone" : ""}`,
    dataset: { section: group.section },
  });
  applySectionColor(card, group.color);
  if (editing) {
    card.dataset.dragId = group.standalone ? firstPart.id : group.section;
    card.dataset.dragType = group.standalone ? "part" : "section";
  }

  if (!group.standalone) {
    const header = element("header", { className: "section-header" });
    const heading = element("div", { className: "section-heading" });
    if (editing) heading.append(entryDragHandle(group.headerTitle, canEdit));

    const mark = sectionMark(group.icon, group.color);
    if (mark) {
      const iconBadge = element("span", {
        className: "section-icon-badge",
        attrs: { "aria-hidden": "true" },
      });
      iconBadge.append(mark);
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
    if (editing) {
      tools.append(entryEditButton(group.section, "workspace.editSection", canEdit));
    } else if (firstPart?.sectionTotalLabel) {
      tools.append(
        element("span", {
          className: "section-total-pill",
          text: firstPart.sectionTotalLabel,
        }),
      );
    }

    if (firstPart?.configurableCount && caps.setSectionCount) {
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
      decBtn.disabled = !isInteractive || !canSelect || firstPart.sectionCount <= 1;

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
      incBtn.disabled = !isInteractive || !canSelect || firstPart.sectionCount >= 8;

      stepper.append(decBtn, countText, incBtn);
      tools.append(stepper);
    }

    header.append(tools);
    card.append(header);
  }

  const list = element("div", { className: "section-parts" });
  if (editing && !group.standalone) list.dataset.dragContainer = group.section;
  if (!group.parts.length) {
    const empty = element("div", { className: "section-empty" });
    empty.append(element("span", { text: t("schedule.emptySection") }));
    if (editing) {
      const add = element("button", {
        className: "button button--secondary button--compact",
        attrs: { type: "button" },
        dataset: { action: "organize-add-part", section: group.section },
        text: t("scheduleEditor.addPart"),
      });
      add.disabled = !canEdit;
      empty.append(add);
    }
    list.append(empty);
  }
  for (const part of group.parts) {
    const row = buildPartRow(part, { isInteractive, selectedId, canSelect, editing, canEdit });
    if (editing && !group.standalone) {
      row.dataset.dragId = part.id;
      row.dataset.dragType = "part";
    }
    list.append(row);
  }
  card.append(list);
  return card;
}

function entryDragHandle(title, enabled) {
  const label = t("scheduleEditor.moveTo");
  const handle = element("button", {
    className: "icon-button entry-grip",
    attrs: { type: "button", title: label, "aria-label": `${label} · ${title}`, "aria-keyshortcuts": "ArrowUp ArrowDown" },
    dataset: { dragHandle: "" },
  });
  handle.disabled = !enabled;
  handle.append(uiIcon("grip"));
  return handle;
}

function entryEditButton(id, key, enabled) {
  const label = t(key);
  const button = element("button", {
    className: "icon-button",
    attrs: { type: "button", title: label, "aria-label": label },
    dataset: { action: "organize-edit", partId: id },
  });
  button.disabled = !enabled;
  button.append(uiIcon("edit"));
  return button;
}

function buildPartRow(part, { isInteractive, selectedId, canSelect, editing, canEdit }) {
  const row = element(editing ? "div" : "button", {
    className: "part-row",
    attrs: editing ? {} : { type: "button", "aria-pressed": String(part.id === selectedId) },
    dataset: {
      action: "select-part",
      partId: part.id,
      partState: part.state,
      overrun: "false",
    },
  });
  if (editing) row.append(entryDragHandle(part.title, canEdit));
  else row.disabled = !isInteractive || !canSelect;

  const info = element("span", { className: "part-info" });
  info.append(element("span", {
    className: "part-number",
    text: part.state === "stopped" ? "✓" : String(part.displayNumber),
    attrs: { "aria-hidden": "true" },
  }));
  const copy = element("span", { className: "part-copy" });
  copy.append(element("strong", { className: "part-title", text: part.title }));
  if (part.startedLabel) {
    copy.append(element("span", {
      className: "part-submeta",
      text: t("part.startedAt", { time: part.startedLabel }),
    }));
  }
  info.append(copy);
  row.append(info);

  const times = element("span", { className: "part-times" });
  if (part.state === "running") {
    const badge = element("span", {
      className: "part-live-badge",
      dataset: { running: "true", overrun: "false" },
    });
    badge.append(element("span", { className: "part-live-dot", attrs: { "aria-hidden": "true" } }),
      element("span", { className: "part-live-time", text: part.plannedLabel, dataset: { livePartId: part.id } }));
    times.append(badge);
  } else {
    times.append(element("span", {
      className: "part-duration-badge",
      text: part.plannedLabel,
    }));
    if (part.resultLabel) {
      times.append(element("span", {
        className: "part-secondary-time",
        text: t("part.recordedDuration", { duration: part.resultLabel }),
      }));
    }
  }
  row.append(times);
  if (editing) row.append(entryEditButton(part.id, "workspace.editPart", canEdit));
  return row;
}
