const TAU = Math.PI * 2;
const FONT_STACK = 'Inter, ui-sans-serif, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif';

export class ClockStageCanvas {
  #canvas;
  #ctx;
  #lastPaintKey = "";
  #lastModel = null;
  #resizeObserver = null;

  constructor(canvas) {
    this.#canvas = canvas;
    this.#ctx = canvas.getContext("2d");
    if (typeof ResizeObserver === "function") {
      this.#resizeObserver = new ResizeObserver(() => {
        this.#lastPaintKey = "";
        if (this.#lastModel) this.render(this.#lastModel);
      });
      this.#resizeObserver.observe(canvas);
    }
  }

  invalidate() {
    this.#lastPaintKey = "";
  }

  render(model) {
    this.#lastModel = model;
    const width = Math.max(1, Math.floor(this.#canvas.clientWidth));
    const height = Math.max(1, Math.floor(this.#canvas.clientHeight));
    const dpr = Math.min(3, Math.max(1, window.devicePixelRatio || 1));
    const paintKey = buildPaintKey(model, width, height, dpr);
    if (paintKey === this.#lastPaintKey) return;
    this.#lastPaintKey = paintKey;

    const targetW = Math.round(width * dpr);
    const targetH = Math.round(height * dpr);
    if (this.#canvas.width !== targetW || this.#canvas.height !== targetH) {
      this.#canvas.width = targetW;
      this.#canvas.height = targetH;
    }

    const ctx = this.#ctx;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, width, height);

    const pad = Math.max(14, Math.min(width, height) * 0.045);
    const innerX = pad;
    const innerY = pad;
    const innerW = Math.max(1, width - pad * 2);
    const innerH = Math.max(1, height - pad * 2);
    const displayPct = Math.max(0.1, Math.min(1.0, (Number(model.text_scale_pct) || 100) / 100));

    if (model.display_mode === "clock_timer") {
      const sideBySide = innerW >= innerH * 1.05;
      const gap = Math.max(18, Math.min(innerW, innerH) * 0.045);
      if (sideBySide) {
        const paneW = Math.max(1, (innerW - gap) / 2);
        drawFaceInBox(ctx, model.clock, innerX, innerY, paneW, innerH, displayPct);
        drawDivider(
          ctx,
          innerX + paneW + gap / 2,
          innerY + innerH * 0.14,
          innerX + paneW + gap / 2,
          innerY + innerH * 0.86,
        );
        drawFaceInBox(
          ctx,
          model.timer,
          innerX + paneW + gap,
          innerY,
          paneW,
          innerH,
          displayPct,
        );
      } else {
        const paneH = Math.max(1, (innerH - gap) / 2);
        drawFaceInBox(ctx, model.clock, innerX, innerY, innerW, paneH, displayPct);
        drawDivider(
          ctx,
          innerX + innerW * 0.14,
          innerY + paneH + gap / 2,
          innerX + innerW * 0.86,
          innerY + paneH + gap / 2,
        );
        drawFaceInBox(
          ctx,
          model.timer,
          innerX,
          innerY + paneH + gap,
          innerW,
          paneH,
          displayPct,
        );
      }
      return;
    }

    const singleFace = model.display_mode === "timer" ? model.timer : model.clock;
    drawFaceInBox(ctx, singleFace, innerX, innerY, innerW, innerH, displayPct);
  }
}

function buildPaintKey(model, width, height, dpr) {
  const faceKey = (face) => {
    if (!face) return "";
    const sector = face.duration_sector || {};
    return [
      face.mode,
      face.analog_style,
      face.primary_text,
      face.seconds_text,
      face.secondary_text,
      face.overrun,
      Math.round((face.hour_angle || 0) * 10),
      Math.round((face.minute_angle || 0) * 10),
      Math.round((face.second_angle || 0) * 10),
      Boolean(sector.visible),
      Boolean(sector.show_remaining),
      Boolean(sector.show_overrun),
      Math.round((sector.end_angle || 0) * 10),
      Math.round((sector.current_angle || 0) * 10),
    ].join("|");
  };
  return [
    width,
    height,
    dpr,
    model.display_mode,
    model.text_scale_pct,
    faceKey(model.clock),
    faceKey(model.timer),
  ].join("::");
}

function drawDivider(ctx, x1, y1, x2, y2) {
  ctx.save();
  ctx.strokeStyle = "rgba(255, 255, 255, 0.16)";
  ctx.lineWidth = 1.5;
  ctx.beginPath();
  ctx.moveTo(x1, y1);
  ctx.lineTo(x2, y2);
  ctx.stroke();
  ctx.restore();
}

function drawFaceInBox(ctx, face, x, y, width, height, displayPct) {
  if (!face) return;
  if (face.mode === "analog") {
    const diameter = Math.max(24, Math.min(width, height) * displayPct);
    drawAnalogDial(
      ctx,
      face,
      x + width / 2,
      y + height / 2,
      diameter,
    );
    return;
  }
  if (face.mode === "analog_digital") {
    const ratio = 1.24;
    const diameter = Math.max(24, Math.min(width, height / ratio) * displayPct);
    const totalH = diameter * ratio;
    const topY = y + (height - totalH) / 2;
    drawAnalogDial(ctx, face, x + width / 2, topY + diameter / 2, diameter);
    const digitalTop = topY + diameter * 1.02;
    const digitalH = Math.max(18, totalH - diameter * 1.02);
    drawDigitalReadout(ctx, face, x, digitalTop, width, digitalH, 1.0);
    return;
  }
  drawDigitalReadout(ctx, face, x, y, width, height, displayPct);
}

function drawDigitalReadout(ctx, face, x, y, width, height, displayPct) {
  const primary = String(face.primary_text || "");
  if (!primary) return;
  const seconds = String(face.seconds_text || "");
  const secondary = String(face.secondary_text || "");
  const hasSide = Boolean(seconds || secondary);

  ctx.save();
  const basePx = 100;
  const secondsRatio = 0.54;
  const secondaryRatio = seconds ? 0.22 : 0.28;
  const gapRatio = hasSide ? 0.055 : 0;

  ctx.font = `700 ${basePx}px ${FONT_STACK}`;
  const primaryMeasure = ctx.measureText(primary).width;

  let sideMeasure = 0;
  if (seconds) {
    ctx.font = `650 ${Math.round(basePx * secondsRatio)}px ${FONT_STACK}`;
    sideMeasure = Math.max(sideMeasure, ctx.measureText(seconds).width);
  }
  if (secondary) {
    ctx.font = `650 ${Math.round(basePx * secondaryRatio)}px ${FONT_STACK}`;
    sideMeasure = Math.max(sideMeasure, ctx.measureText(secondary).width);
  }

  const unitWidth = Math.max(1, primaryMeasure + basePx * gapRatio + sideMeasure);
  const unitHeight = basePx * 1.08;
  const maxPx = Math.min((width * basePx) / unitWidth, (height * basePx) / unitHeight);
  const primaryPx = Math.max(14, Math.floor(maxPx * displayPct));
  const secondsPx = Math.max(10, Math.round(primaryPx * secondsRatio));
  const secondaryPx = Math.max(9, Math.round(primaryPx * secondaryRatio));
  const gapPx = hasSide ? Math.round(primaryPx * gapRatio) : 0;

  ctx.font = `700 ${primaryPx}px ${FONT_STACK}`;
  const actualPrimaryW = ctx.measureText(primary).width;
  let actualSecondsW = 0;
  let actualSecondaryW = 0;
  if (seconds) {
    ctx.font = `650 ${secondsPx}px ${FONT_STACK}`;
    actualSecondsW = ctx.measureText(seconds).width;
  }
  if (secondary) {
    ctx.font = `650 ${secondaryPx}px ${FONT_STACK}`;
    actualSecondaryW = ctx.measureText(secondary).width;
  }
  const actualSideW = Math.max(actualSecondsW, actualSecondaryW);

  const totalW = actualPrimaryW + gapPx + actualSideW;
  const startX = x + (width - totalW) / 2;
  const sideRightX = startX + totalW;
  const baselineY = y + height / 2 + primaryPx * 0.34;
  const color = face.overrun ? "#ff5555" : "#ffffff";

  ctx.fillStyle = color;
  ctx.textAlign = "left";
  ctx.textBaseline = "alphabetic";
  ctx.font = `700 ${primaryPx}px ${FONT_STACK}`;
  ctx.fillText(primary, startX, baselineY);

  if (seconds) {
    ctx.fillStyle = face.overrun
      ? "rgba(255, 85, 85, 0.72)"
      : "rgba(255, 255, 255, 0.58)";
    ctx.textAlign = "right";
    ctx.font = `650 ${secondsPx}px ${FONT_STACK}`;
    ctx.fillText(seconds, sideRightX, baselineY);
  }

  if (secondary) {
    ctx.fillStyle = "#aeb8c2";
    ctx.textAlign = "right";
    ctx.font = `650 ${secondaryPx}px ${FONT_STACK}`;
    const secondaryBaselineY = seconds
      ? baselineY - secondsPx * 0.86
      : baselineY - primaryPx * 0.04;
    ctx.fillText(secondary, sideRightX, secondaryBaselineY);
  }
  ctx.restore();
}

function drawAnalogDial(ctx, face, cx, cy, size) {
  if (size <= 4) return;
  if (face.analog_style === "classic") {
    drawClassicDial(ctx, face, cx, cy, size);
  } else {
    drawSignatureDial(ctx, face, cx, cy, size);
  }
}

function drawSector(ctx, sector, cx, cy, size) {
  if (!sector || sector.visible !== true) return;
  const outerRadius = size * 0.372;
  const innerRadius = size * 0.1;

  const paintArc = (startDeg, endDeg, fillStyle, strokeStyle) => {
    let sweep = (endDeg - startDeg) % 360;
    if (sweep < 0) sweep += 360;
    if (sweep <= 0.05) return;
    const startRad = ((startDeg - 90) * Math.PI) / 180;
    const endRad = startRad + (sweep * Math.PI) / 180;

    ctx.save();
    ctx.beginPath();
    ctx.moveTo(
      cx + Math.cos(startRad) * outerRadius,
      cy + Math.sin(startRad) * outerRadius,
    );
    ctx.arc(cx, cy, outerRadius, startRad, endRad, false);
    ctx.lineTo(
      cx + Math.cos(endRad) * innerRadius,
      cy + Math.sin(endRad) * innerRadius,
    );
    ctx.arc(cx, cy, innerRadius, endRad, startRad, true);
    ctx.closePath();
    ctx.fillStyle = fillStyle;
    ctx.fill();
    ctx.strokeStyle = strokeStyle;
    ctx.lineWidth = Math.max(1, size * 0.0018);
    ctx.stroke();
    ctx.restore();
  };

  if (sector.show_remaining) {
    paintArc(
      Number(sector.current_angle) || 0,
      Number(sector.end_angle) || 0,
      "rgba(45, 192, 105, 0.22)",
      "rgba(23, 142, 79, 0.30)",
    );
  }
  if (sector.show_overrun) {
    paintArc(
      Number(sector.end_angle) || 0,
      Number(sector.current_angle) || 0,
      "rgba(255, 72, 72, 0.34)",
      "rgba(217, 36, 53, 0.42)",
    );
  }
}

function drawClassicDial(ctx, face, cx, cy, size) {
  const r = size / 2 - 4;
  if (r <= 0) return;

  ctx.save();
  ctx.lineWidth = Math.max(2, r * 0.02);
  ctx.strokeStyle = "#2a2f36";
  ctx.beginPath();
  ctx.arc(cx, cy, r, 0, TAU);
  ctx.stroke();

  drawSector(ctx, face.duration_sector, cx, cy, size);

  ctx.strokeStyle = "#5a636d";
  for (let i = 0; i < 12; i += 1) {
    const a = (i / 12) * TAU;
    const inner = r * (i % 3 === 0 ? 0.82 : 0.88);
    ctx.lineWidth = i % 3 === 0 ? Math.max(3, r * 0.03) : Math.max(1, r * 0.015);
    ctx.beginPath();
    ctx.moveTo(cx + inner * Math.sin(a), cy - inner * Math.cos(a));
    ctx.lineTo(cx + r * 0.96 * Math.sin(a), cy - r * 0.96 * Math.cos(a));
    ctx.stroke();
  }

  const hand = (angleDeg, length, widthPx, color) => {
    const a = (angleDeg * Math.PI) / 180;
    ctx.strokeStyle = color;
    ctx.lineWidth = widthPx;
    ctx.lineCap = "round";
    ctx.beginPath();
    ctx.moveTo(cx, cy);
    ctx.lineTo(cx + length * Math.sin(a), cy - length * Math.cos(a));
    ctx.stroke();
  };

  hand(face.hour_angle || 0, r * 0.5, Math.max(4, r * 0.045), "#ffffff");
  hand(face.minute_angle || 0, r * 0.75, Math.max(3, r * 0.03), "#ffffff");
  hand(face.second_angle || 0, r * 0.85, Math.max(1, r * 0.012), "#ff5555");

  ctx.fillStyle = "#ffffff";
  ctx.beginPath();
  ctx.arc(cx, cy, Math.max(3, r * 0.03), 0, TAU);
  ctx.fill();
  ctx.restore();
}

function drawSignatureDial(ctx, face, cx, cy, size) {
  const r = size * 0.485;
  if (r <= 0) return;

  const circle = (radius, fillStyle, strokeStyle, lineWidth) => {
    ctx.beginPath();
    ctx.arc(cx, cy, radius, 0, TAU);
    if (fillStyle) {
      ctx.fillStyle = fillStyle;
      ctx.fill();
    }
    if (strokeStyle && lineWidth > 0) {
      ctx.strokeStyle = strokeStyle;
      ctx.lineWidth = lineWidth;
      ctx.stroke();
    }
  };

  const point = (angleDeg, radius) => {
    const a = (angleDeg * Math.PI) / 180;
    return {
      x: cx + radius * Math.sin(a),
      y: cy - radius * Math.cos(a),
    };
  };

  ctx.save();
  const rimGradient = ctx.createLinearGradient(cx - r, cy - r, cx + r, cy + r);
  rimGradient.addColorStop(0.0, "#eef3f7");
  rimGradient.addColorStop(0.24, "#ffffff");
  rimGradient.addColorStop(0.55, "#bdc8d2");
  rimGradient.addColorStop(0.78, "#f9fbfd");
  rimGradient.addColorStop(1.0, "#8d9aa6");
  circle(r, rimGradient, "#0f1720", Math.max(1, r * 0.008));
  circle(r * 0.955, "#d9e1e8", "#f9fbfd", Math.max(1, r * 0.01));
  circle(r * 0.922, "#f9fbfd", "#aeb8c2", Math.max(1, r * 0.006));

  const faceGradient = ctx.createRadialGradient(
    cx - r * 0.18,
    cy - r * 0.22,
    r * 0.08,
    cx,
    cy,
    r * 0.9,
  );
  faceGradient.addColorStop(0.0, "#ffffff");
  faceGradient.addColorStop(0.62, "#fbfcfd");
  faceGradient.addColorStop(1.0, "#edf2f6");
  circle(r * 0.895, faceGradient, "#d7dee5", Math.max(1, r * 0.005));

  drawSector(ctx, face.duration_sector, cx, cy, size);

  for (let i = 0; i < 60; i += 1) {
    const isHour = i % 5 === 0;
    const isQuarter = i % 15 === 0;
    const outer = point(i * 6, r * 0.838);
    const inner = point(i * 6, r * (isQuarter ? 0.74 : isHour ? 0.77 : 0.805));
    ctx.beginPath();
    ctx.moveTo(inner.x, inner.y);
    ctx.lineTo(outer.x, outer.y);
    ctx.lineCap = "round";
    ctx.lineWidth = isQuarter
      ? Math.max(2.5, r * 0.017)
      : isHour
        ? Math.max(1.8, r * 0.012)
        : Math.max(1, r * 0.0048);
    ctx.strokeStyle = isHour ? "#17202a" : "#8f9aa5";
    ctx.stroke();
  }

  ctx.textAlign = "center";
  ctx.textBaseline = "middle";
  ctx.fillStyle = "#17202a";
  ctx.font = `700 ${Math.round(Math.max(13, r * 0.105))}px ${FONT_STACK}`;
  for (let n = 1; n <= 12; n += 1) {
    const labelPoint = point(n * 30, r * 0.635);
    ctx.fillText(String(n), labelPoint.x, labelPoint.y);
  }

  const taperedHand = (angleDeg, length, tail, halfWidth, fillStyle, strokeStyle) => {
    const a = (angleDeg * Math.PI) / 180;
    const dx = Math.sin(a);
    const dy = -Math.cos(a);
    const px = Math.cos(a);
    const py = Math.sin(a);
    const shoulder = Math.max(halfWidth * 1.5, length * 0.12);
    const tipX = cx + dx * length;
    const tipY = cy + dy * length;
    const neckX = cx + dx * shoulder;
    const neckY = cy + dy * shoulder;
    const tailX = cx - dx * tail;
    const tailY = cy - dy * tail;

    ctx.save();
    ctx.shadowColor = "rgba(15, 23, 32, 0.28)";
    ctx.shadowBlur = Math.max(2, r * 0.015);
    ctx.shadowOffsetY = Math.max(1, r * 0.008);
    ctx.beginPath();
    ctx.moveTo(tipX, tipY);
    ctx.lineTo(neckX + px * halfWidth, neckY + py * halfWidth);
    ctx.lineTo(tailX + px * halfWidth * 0.58, tailY + py * halfWidth * 0.58);
    ctx.lineTo(tailX - px * halfWidth * 0.58, tailY - py * halfWidth * 0.58);
    ctx.lineTo(neckX - px * halfWidth, neckY - py * halfWidth);
    ctx.closePath();
    ctx.fillStyle = fillStyle;
    ctx.fill();
    ctx.strokeStyle = strokeStyle;
    ctx.lineWidth = Math.max(1, halfWidth * 0.22);
    ctx.stroke();
    ctx.restore();
  };

  taperedHand(
    face.hour_angle || 0,
    r * 0.445,
    r * 0.08,
    Math.max(4, r * 0.037),
    "#202a35",
    "#0e1720",
  );
  taperedHand(
    face.minute_angle || 0,
    r * 0.65,
    r * 0.092,
    Math.max(3, r * 0.026),
    "#1a2430",
    "#0b121a",
  );

  const secA = ((face.second_angle || 0) * Math.PI) / 180;
  const secDx = Math.sin(secA);
  const secDy = -Math.cos(secA);
  ctx.save();
  ctx.strokeStyle = "#d42435";
  ctx.lineWidth = Math.max(1.8, r * 0.008);
  ctx.lineCap = "round";
  ctx.beginPath();
  ctx.moveTo(cx - secDx * r * 0.18, cy - secDy * r * 0.18);
  ctx.lineTo(cx + secDx * r * 0.735, cy + secDy * r * 0.735);
  ctx.stroke();
  ctx.fillStyle = "#d42435";
  ctx.beginPath();
  ctx.arc(
    cx - secDx * r * 0.205,
    cy - secDy * r * 0.205,
    Math.max(2.5, r * 0.017),
    0,
    TAU,
  );
  ctx.fill();
  ctx.restore();

  const capGradient = ctx.createRadialGradient(
    cx - r * 0.018,
    cy - r * 0.024,
    r * 0.008,
    cx,
    cy,
    r * 0.056,
  );
  capGradient.addColorStop(0.0, "#ffffff");
  capGradient.addColorStop(0.34, "#dce4eb");
  capGradient.addColorStop(1.0, "#52606d");
  circle(r * 0.06, capGradient, "#17202a", Math.max(1, r * 0.006));
  circle(r * 0.025, "#d42435", "#ffffff", Math.max(1, r * 0.004));
  ctx.restore();
}
