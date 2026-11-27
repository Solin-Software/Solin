import { t } from "../../shared/scripts/i18n.js";
import { uiIcon } from "../../shared/scripts/dom.js";
import { formatPartLiveClock } from "./timer-display.js";

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function optionalSeconds(value) {
  const trimmed = String(value).trim();
  if (!trimmed) return null;
  if (!/^\d+$/.test(trimmed)) return NaN;
  const number = Number(trimmed);
  return Number.isSafeInteger(number) ? number : NaN;
}

function allParts(entries) {
  return entries.flatMap((entry) => entry.type === "section" ? entry.parts : [entry]);
}

function validTime(value) {
  return /^(?:[01]\d|2[0-3]):[0-5]\d$/.test(value);
}

function hasWindow({ start, end }) {
  return validTime(start) && validTime(end) && start !== end;
}

export class ScheduleTimingEditor {
  constructor({ saveTiming }) {
    this.saveTiming = saveTiming;
    this.generation = 0;
    this.dialog = element("dialog", "workspace-dialog schedule-editor");
    this.dialog.setAttribute("aria-labelledby", "schedule-editor-title");
    this.dialog.addEventListener("cancel", (event) => {
      event.preventDefault();
      this.requestClose();
    });
    document.body.append(this.dialog);
  }

  text(key) {
    return t(`scheduleEditor.${key}`);
  }

  open(schedule, scheduleRevision) {
    if (!schedule || typeof schedule.id !== "string" || !Number.isInteger(schedule.revision) || !Number.isInteger(scheduleRevision) || !Array.isArray(schedule.entries)) {
      throw new TypeError("A custom schedule and its captured revisions are required");
    }
    if (this.dialog.open && !this.requestClose()) return false;
    this.generation += 1;
    const pacing = schedule.pacingConfig || {};
    const parts = allParts(schedule.entries);
    const weights = Object.fromEntries(parts
      .filter((part) => Number.isInteger(part.adjustmentWeight))
      .map((part) => [part.id, part.adjustmentWeight]));
    this.draft = {
      scheduleId: schedule.id, customRevision: schedule.revision, scheduleRevision,
      parts,
      start: pacing.start_time || "", end: pacing.end_time || "",
      hadWindow: Boolean(pacing.start_time && pacing.end_time),
      untracked: pacing.untracked_seconds == null ? "" : String(pacing.untracked_seconds),
      closing: pacing.transitions?.closing_seconds == null ? "" : String(pacing.transitions.closing_seconds),
      before: { ...(pacing.transitions?.before_parts || {}) },
      weights: { ...weights }, originalWeights: weights,
      dirtyWeights: new Set(), dirty: false, saving: false,
    };
    this.render();
    this.dialog.showModal();
    return true;
  }

  invalidate() {
    this.generation += 1;
    this.draft = null;
    if (this.dialog.open) this.dialog.close();
    this.dialog.replaceChildren();
  }

  requestClose() {
    if (!this.draft) return true;
    if (this.draft.saving) return false;
    if (this.draft.dirty && !window.confirm(this.text("unsavedConfirm"))) return false;
    this.invalidate();
    return true;
  }

  button(label, action, { style = "secondary", icon } = {}) {
    const button = element("button", icon ? "icon-button" : `button button--${style}`, icon ? undefined : label);
    button.type = "button";
    if (icon) {
      button.append(uiIcon(icon));
      button.setAttribute("aria-label", label);
      button.title = label;
    }
    button.addEventListener("click", action);
    return button;
  }

  field(labelText, value, onInput, options = {}) {
    const label = element("label", "schedule-editor__field");
    label.append(element("span", "schedule-editor__label", labelText));
    const input = element("input", "schedule-editor__input");
    input.type = options.type || "text";
    input.value = value ?? "";
    if (options.inputMode) input.inputMode = options.inputMode;
    if (options.placeholder) input.placeholder = options.placeholder;
    if (options.disabled) input.disabled = true;
    input.addEventListener("input", () => { onInput(input.value); this.draft.dirty = true; });
    label.append(input);
    return { label, input };
  }

  render() {
    const draft = this.draft;
    if (!draft) return;
    const header = element("header", "modal-card-header");
    const title = element("h2", "", t("workspace.timing"));
    title.id = "schedule-editor-title";
    header.append(title, this.button(this.text("close"), () => this.requestClose(), { icon: "close" }));
    const content = element("div", "workspace-dialog-content");
    const feedback = element("p", "workspace-dialog-error form-error");
    feedback.setAttribute("role", "alert");
    feedback.hidden = !draft.stale;
    if (draft.stale) feedback.textContent = this.text("draftStale");
    const footer = element("footer", "workspace-dialog-actions");
    const clear = this.button(this.text("clearWindow"), () => this.clearWindow(), { style: "ghost" });
    clear.classList.add("schedule-editor__clear");
    const cancel = this.button(this.text("cancel"), () => this.requestClose());
    const save = element("button", "button button--primary", this.text("save"));
    save.type = "submit";
    save.disabled = Boolean(draft.stale);
    footer.append(clear, cancel, save);
    this.renderTiming(content, clear);
    const form = element("form");
    form.noValidate = true;
    form.addEventListener("submit", (event) => {
      event.preventDefault();
      void this.save();
    });
    form.append(header, content, feedback, footer);
    this.dialog.replaceChildren(form);
  }

  clearWindow() {
    const draft = this.draft;
    Object.assign(draft, { start: "", end: "", untracked: "", closing: "", before: {}, dirty: true });
    this.render();
    this.dialog.querySelector('input[type="time"]').focus();
  }

  renderTiming(content, clear) {
    const draft = this.draft;
    const estimates = [];
    const syncWindow = () => {
      const available = hasWindow(draft);
      for (const input of estimates) input.disabled = !available;
      clear.hidden = !draft.start && !draft.end;
    };
    const grid = element("div", "schedule-editor__grid");
    for (const endpoint of ["start", "end"]) {
      const time = this.field(this.text(endpoint), draft[endpoint], (value) => {
        draft[endpoint] = value;
        syncWindow();
      }, { type: "time" });
      grid.append(time.label);
    }
    content.append(grid);

    const parts = element("fieldset", "schedule-editor__parts");
    parts.append(element("legend", "", this.text("adjustableParts")));
    const help = element("p", "schedule-editor__hint");
    const updateHelp = () => {
      const any = Object.values(draft.weights).some((weight) => Number(weight) > 0);
      help.textContent = this.text(any ? "selectedWeightsHelp" : "defaultWeightsHelp");
    };
    parts.append(help);
    if (draft.parts.length) content.append(parts);

    const details = element("details", "schedule-editor__advanced");
    details.append(element("summary", "", this.text("advanced")), element("p", "schedule-editor__hint", this.text("weightsHelp")));
    const summary = element("div", "schedule-editor__grid");
    for (const estimate of ["untracked", "closing"]) {
      const field = this.field(this.text(estimate), draft[estimate], (value) => {
        draft[estimate] = value;
      }, { inputMode: "numeric", placeholder: this.text("auto") });
      estimates.push(field.input);
      summary.append(field.label);
    }
    details.append(summary);
    for (const part of draft.parts) {
      const eligible = part.plannedSeconds >= 60;
      const checkbox = element("input");
      checkbox.type = "checkbox";
      const row = element("label", "schedule-editor__part-choice");
      row.append(checkbox, element("span", "", part.title), element("small", "",
        eligible ? formatPartLiveClock(part.plannedSeconds) : this.text("shortPart")));
      parts.append(row);

      const card = element("div", "schedule-editor__timing-part");
      card.setAttribute("role", "group");
      card.setAttribute("aria-label", part.title);
      card.append(element("strong", "", part.title));
      const fields = element("div", "schedule-editor__grid");
      const weight = this.field(this.text("weight"), draft.weights[part.id] ?? "0", (value) => {
        setWeight(value);
      }, { inputMode: "numeric", disabled: !eligible });
      const setWeight = (value) => {
        draft.weights[part.id] = value;
        draft.dirtyWeights.add(part.id);
        draft.dirty = true;
        syncChoice();
        updateHelp();
      };
      const syncChoice = () => {
        checkbox.checked = Number(draft.weights[part.id]) > 0;
        checkbox.disabled = !eligible && !checkbox.checked;
      };
      syncChoice();
      checkbox.addEventListener("change", () => {
        setWeight(checkbox.checked ? 1 : 0);
        weight.input.value = draft.weights[part.id];
      });
      const before = this.field(this.text("before"), draft.before[part.id] ?? "", (value) => {
        draft.before[part.id] = value;
      }, { inputMode: "numeric", placeholder: this.text("auto") });
      estimates.push(before.input);
      fields.append(weight.label, before.label);
      card.append(fields);
      details.append(card);
    }
    content.append(details);
    updateHelp();
    syncWindow();
  }

  timingPayload() {
    const draft = this.draft;
    if ((draft.start || draft.end) && !hasWindow(draft)) throw new Error(this.text("invalidWindow"));
    const clearingWindow = draft.hadWindow && !draft.start && !draft.end;
    const untracked = clearingWindow ? null : optionalSeconds(draft.untracked);
    const closing = clearingWindow ? null : optionalSeconds(draft.closing);
    const before = clearingWindow ? {} : Object.fromEntries(draft.parts.map(
      (part) => [part.id, optionalSeconds(draft.before[part.id] ?? "")],
    ));
    if ([untracked, closing, ...Object.values(before)].some(Number.isNaN)) throw new Error(this.text("invalidEstimate"));
    if (!draft.start && (untracked !== null || closing !== null || Object.values(before).some((value) => value !== null))) throw new Error(this.text("windowRequired"));
    const weights = {};
    for (const part of draft.parts) {
      if (!draft.dirtyWeights.has(part.id)) continue;
      const raw = String(draft.weights[part.id]).trim() || "0";
      const value = Number(raw);
      if (!/^\d+$/.test(raw) || !Number.isInteger(value) || value > 100 ||
          (value > 0 && value !== draft.originalWeights[part.id] &&
            part.plannedSeconds < 60)) {
        throw new Error(this.text("invalidWeight"));
      }
      weights[part.id] = value;
    }
    return { startTime: draft.start || null, endTime: draft.end || null, untrackedSeconds: untracked, weights, beforeParts: before, closingSeconds: closing };
  }

  async save() {
    const draft = this.draft;
    if (!draft || draft.saving || draft.stale) return;
    const feedback = this.dialog.querySelector(".workspace-dialog-error");
    feedback.hidden = true;
    let payload;
    try {
      payload = { scheduleId: draft.scheduleId, customRevision: draft.customRevision, scheduleRevision: draft.scheduleRevision };
      payload.timing = this.timingPayload();
    } catch (error) {
      feedback.textContent = error.message;
      feedback.hidden = false;
      return;
    }
    const generation = this.generation;
    draft.saving = true;
    const controls = this.dialog.querySelectorAll("button:enabled, input:enabled, select:enabled");
    controls.forEach((node) => { node.disabled = true; });
    let result;
    let message;
    try {
      result = await this.saveTiming(payload);
      message = result?.error?.message;
    } catch (error) {
      message = error?.message;
    }
    if (this.generation !== generation) return;
    if (result?.ok) {
      this.invalidate();
      return;
    }
    draft.saving = false;
    controls.forEach((node) => { node.disabled = false; });
    draft.stale = result?.error?.code === "schedule_stale";
    if (draft.stale) {
      this.dialog.querySelector('button[type="submit"]').disabled = true;
      message = this.text("draftStale");
    }
    feedback.textContent = message || this.text("saveFailed");
    feedback.hidden = false;
  }
}
