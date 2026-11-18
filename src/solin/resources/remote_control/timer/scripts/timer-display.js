const SECONDS_PER_ANALOG_LAP = 3600.0;
const MIN_PLANNED_SECONDS = 60;
const MAX_PLANNED_SECONDS = 24 * 60 * 60 - 1;

function pad2(value) {
  return String(Math.trunc(value)).padStart(2, "0");
}

function localTimeParts(epochSeconds, utcOffsetMinutes) {
  const wholeEpoch = Math.floor(Number(epochSeconds) || 0);
  const offsetSeconds = Math.trunc(Number(utcOffsetMinutes) || 0) * 60;
  const date = new Date((wholeEpoch + offsetSeconds) * 1000);
  return {
    hours24: date.getUTCHours(),
    minutes: date.getUTCMinutes(),
    seconds: date.getUTCSeconds(),
  };
}

export function formatWallClock(epochSeconds, config, utcOffsetMinutes = 0) {
  const { hours24, minutes, seconds } = localTimeParts(epochSeconds, utcOffsetMinutes);
  if (config.hourFormat24h) {
    const primary = `${pad2(hours24)}:${pad2(minutes)}`;
    const secs = config.showSeconds ? pad2(seconds) : "";
    return [primary, secs, ""];
  }
  const hour12 = hours24 % 12 === 0 ? 12 : hours24 % 12;
  const primary = `${hour12}:${pad2(minutes)}`;
  const secs = config.showSeconds ? pad2(seconds) : "";
  const ampm = config.showAmpm ? (hours24 < 12 ? "AM" : "PM") : "";
  return [primary, secs, ampm];
}

export function formatStageDuration(seconds) {
  const numeric = Number(seconds) || 0;
  const sign = numeric < 0 ? "-" : "";
  const total = Math.round(Math.abs(numeric));
  const hours = Math.floor(total / 3600);
  const rem = total % 3600;
  const minutes = Math.floor(rem / 60);
  const secs = rem % 60;
  if (hours > 0) {
    return [`${sign}${hours}:${pad2(minutes)}`, pad2(secs)];
  }
  return [`${sign}${pad2(minutes)}:${pad2(secs)}`, ""];
}

export function formatPartLiveClock(seconds) {
  const numeric = Number(seconds) || 0;
  const sign = numeric < 0 ? "-" : "";
  const total = Math.round(Math.abs(numeric));
  const hours = Math.floor(total / 3600);
  const rem = total % 3600;
  const minutes = Math.floor(rem / 60);
  const secs = rem % 60;
  if (hours > 0) {
    return `${sign}${hours}:${pad2(minutes)}:${pad2(secs)}`;
  }
  return `${sign}${pad2(minutes)}:${pad2(secs)}`;
}

export function parseDurationInput(raw) {
  const trimmed = String(raw || "").trim();
  if (!trimmed) return null;
  if (/^\d+$/.test(trimmed)) {
    const minutes = Number.parseInt(trimmed, 10);
    if (!Number.isFinite(minutes) || minutes <= 0) return null;
    const total = minutes * 60;
    return Math.min(MAX_PLANNED_SECONDS, Math.max(MIN_PLANNED_SECONDS, total));
  }
  const parts = trimmed.split(":");
  if (parts.length !== 2 && parts.length !== 3) return null;
  if (!parts.every((segment) => /^\d+$/.test(segment))) return null;
  const nums = parts.map((segment) => Number.parseInt(segment, 10));
  let totalSeconds = 0;
  if (nums.length === 2) {
    const [minutes, seconds] = nums;
    if (seconds >= 60) return null;
    totalSeconds = minutes * 60 + seconds;
  } else {
    const [hours, minutes, seconds] = nums;
    if (minutes >= 60 || seconds >= 60) return null;
    totalSeconds = hours * 3600 + minutes * 60 + seconds;
  }
  if (totalSeconds < MIN_PLANNED_SECONDS || totalSeconds > MAX_PLANNED_SECONDS) {
    return null;
  }
  return totalSeconds;
}

function minuteAngleForEpoch(epochSeconds, utcOffsetMinutes) {
  const { minutes, seconds } = localTimeParts(epochSeconds, utcOffsetMinutes);
  return minutes * 6.0 + seconds * 0.1;
}

function blankFace(config) {
  return {
    mode: config.mode,
    analog_style: config.analogStyle,
    primary_text: "",
    seconds_text: "",
    secondary_text: "",
    overrun: false,
    duration_sector: { visible: false },
    hour_angle: 0.0,
    minute_angle: 0.0,
    second_angle: 0.0,
  };
}

function buildClockFace(epochSeconds, config, utcOffsetMinutes) {
  const face = blankFace(config);
  if (config.mode === "analog" || config.mode === "analog_digital") {
    const { hours24, minutes, seconds } = localTimeParts(epochSeconds, utcOffsetMinutes);
    const minute = minutes + seconds / 60.0;
    const hour = (hours24 % 12) + minute / 60.0;
    face.second_angle = seconds * 6.0;
    face.minute_angle = minute * 6.0;
    face.hour_angle = hour * 30.0;
    if (config.mode === "analog_digital") {
      const [text, secs, ampm] = formatWallClock(epochSeconds, config, utcOffsetMinutes);
      face.primary_text = text;
      face.seconds_text = secs;
      face.secondary_text = ampm;
    }
    return face;
  }
  const [text, secs, ampm] = formatWallClock(epochSeconds, config, utcOffsetMinutes);
  face.primary_text = text;
  face.seconds_text = secs;
  face.secondary_text = ampm;
  return face;
}

function buildTimerFace(evaluated) {
  const [primary, seconds] = formatStageDuration(evaluated.displaySeconds);
  return {
    mode: "digital",
    analog_style: "",
    primary_text: primary,
    seconds_text: seconds,
    secondary_text: "",
    overrun: Boolean(evaluated.overrun && evaluated.active),
    duration_sector: { visible: false },
    hour_angle: 0.0,
    minute_angle: 0.0,
    second_angle: 0.0,
  };
}

function buildDurationSector(evaluated, utcOffsetMinutes) {
  if (
    !evaluated.active ||
    evaluated.state === "stopped" ||
    evaluated.plannedSeconds <= 0
  ) {
    return { visible: false };
  }
  const remaining = evaluated.remainingSeconds;
  const currentEpoch = evaluated.wallClockEpoch;
  const overrunSeconds = Math.max(0.0, -remaining);
  const endEpoch = currentEpoch + remaining;
  const showRemaining = remaining > 0.0 && remaining < SECONDS_PER_ANALOG_LAP;
  const showOverrun = evaluated.overrun && overrunSeconds < SECONDS_PER_ANALOG_LAP;
  return {
    visible: Boolean(showRemaining || showOverrun),
    show_remaining: Boolean(showRemaining),
    show_overrun: Boolean(showOverrun),
    end_angle: minuteAngleForEpoch(endEpoch, utcOffsetMinutes),
    current_angle: minuteAngleForEpoch(currentEpoch, utcOffsetMinutes),
  };
}

function selectActiveDisplay(config) {
  if (config.partTimerDisplay === "clock") return "clock";
  if (config.partTimerDisplay === "clock_timer") return "clock_timer";
  return "timer";
}

export function normalizeClockConfig(raw) {
  const cfg = raw && typeof raw === "object" ? raw : {};
  const mode = ["digital", "analog", "analog_digital"].includes(cfg.mode)
    ? cfg.mode
    : "digital";
  const analogStyle = ["signature", "classic"].includes(cfg.analogStyle ?? cfg.analog_style)
    ? (cfg.analogStyle ?? cfg.analog_style)
    : "signature";
  const hourFormat24h = Boolean(cfg.hourFormat24h ?? cfg.hour_format_24h ?? true);
  const showAmpm = Boolean(cfg.showAmpm ?? cfg.show_ampm ?? false);
  const showSeconds = Boolean(cfg.showSeconds ?? cfg.show_seconds ?? true);
  const direction = (cfg.direction === "up" ? "up" : "down");
  const rawDisplay = cfg.partTimerDisplay ?? cfg.part_timer_display ?? "timer";
  const partTimerDisplay = ["timer", "clock", "clock_timer"].includes(rawDisplay)
    ? rawDisplay
    : "timer";
  const rawScale = Number(cfg.textScalePct ?? cfg.text_scale_pct ?? 100);
  const textScalePct = Math.max(10, Math.min(100, Math.trunc(Number.isFinite(rawScale) ? rawScale : 100)));
  const rawFreeze = Number(cfg.freezeSeconds ?? cfg.freeze_seconds ?? 3);
  const freezeSeconds = Math.max(0, Math.min(30, Math.trunc(Number.isFinite(rawFreeze) ? rawFreeze : 3)));
  return {
    mode,
    analogStyle,
    hourFormat24h,
    showAmpm,
    showSeconds,
    direction,
    partTimerDisplay,
    textScalePct,
    freezeSeconds,
  };
}

export function evaluateLiveSnapshot(live, monotonicNowMs = 0, anchorMonotonicMs = 0) {
  const deltaMs = Math.max(0, (Number(monotonicNowMs) || 0) - (Number(anchorMonotonicMs) || 0));
  const serverEpochMs = Number(live?.serverEpochMs) || 0;
  const wallClockEpoch = (serverEpochMs + deltaMs) / 1000.0;
  const utcOffsetMinutes = Math.trunc(Number(live?.utcOffsetMinutes) || 0);
  const active = Boolean(live?.active);
  const state = ["idle", "running", "stopped"].includes(live?.state) ? live.state : "idle";
  const config = normalizeClockConfig(live?.clockConfig);
  const direction = live?.direction === "up" ? "up" : config.direction;
  const plannedSeconds = Math.max(0, Math.trunc(Number(live?.plannedSeconds) || 0));
  const baseElapsed = Math.max(0, Number(live?.elapsedSeconds) || 0);
  const elapsedSeconds =
    active && state === "running"
      ? baseElapsed + deltaMs / 1000.0
      : active
        ? baseElapsed
        : 0.0;
  const remainingSeconds = active ? plannedSeconds - elapsedSeconds : 0.0;
  const overrun = Boolean(active && remainingSeconds < 0);
  let displaySeconds = 0.0;
  if (active) {
    if (state === "stopped") {
      displaySeconds = elapsedSeconds;
    } else {
      displaySeconds = direction === "up" ? elapsedSeconds : remainingSeconds;
    }
  }
  return {
    active,
    state,
    direction,
    wallClockEpoch,
    utcOffsetMinutes,
    activePartId: live?.activePartId ?? null,
    activePartTitle: String(live?.activePartTitle ?? ""),
    activeSection: live?.activeSection ?? null,
    plannedSeconds,
    elapsedSeconds,
    remainingSeconds,
    displaySeconds,
    overrun,
    clockConfig: config,
  };
}

export function buildRenderModel(live, monotonicNowMs = 0, anchorMonotonicMs = 0) {
  const evaluated = evaluateLiveSnapshot(live, monotonicNowMs, anchorMonotonicMs);
  const config = evaluated.clockConfig;
  const clockFace = buildClockFace(
    evaluated.wallClockEpoch,
    config,
    evaluated.utcOffsetMinutes,
  );
  const timerFace = evaluated.active ? buildTimerFace(evaluated) : blankFace(config);
  let displayMode = "clock";
  let selectedFace = clockFace;
  if (evaluated.active) {
    displayMode = selectActiveDisplay(config);
    if (displayMode === "timer") {
      selectedFace = timerFace;
    } else if (config.mode === "analog" || config.mode === "analog_digital") {
      clockFace.duration_sector = buildDurationSector(
        evaluated,
        evaluated.utcOffsetMinutes,
      );
    }
  }

  return {
    active: evaluated.active,
    display_mode: displayMode,
    clock: clockFace,
    timer: timerFace,
    mode: selectedFace.mode,
    analog_style: selectedFace.analog_style,
    primary_text: selectedFace.primary_text,
    seconds_text: selectedFace.seconds_text,
    secondary_text: selectedFace.secondary_text,
    overrun: displayMode === "timer" ? selectedFace.overrun : timerFace.overrun,
    show_seconds: config.showSeconds,
    text_scale_pct: config.textScalePct,
    hour_angle: selectedFace.hour_angle,
    minute_angle: selectedFace.minute_angle,
    second_angle: selectedFace.second_angle,
  };
}
