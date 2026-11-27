import { byId, element } from "../../shared/scripts/dom.js";
import { t } from "../../shared/scripts/i18n.js";
import { formatPartLiveClock, parseDurationInput } from "./timer-display.js";
import { sectionAppearanceFields } from "./section-appearance.js";
import { isSectionColor } from "./section-mark.js";

export class ScheduleWorkspace {
  #store;
  #send;
  #loadCatalog;
  #editor;
  #organizer;
  #dialog = byId("workspace-dialog");
  #submit = byId("workspace-submit");
  #confirmation = null;
  #request = 0;
  #lastCustomScheduleId = "";

  constructor({ store, send, loadCatalog, editor, organizer }) {
    this.#store = store;
    this.#send = send;
    this.#loadCatalog = loadCatalog;
    this.#editor = editor;
    this.#organizer = organizer;

    byId("tab-meetings").addEventListener("click", () => void this.#selectMeetings());
    byId("tab-schedules").addEventListener("click", () => void this.#selectSchedules());
    byId("custom-selector").addEventListener("change", (event) => {
      const id = event.target.value;
      event.target.value = this.#store.state.schedule.customSchedule?.id ?? "";
      void this.#selectSchedule(id);
    });
    byId("custom-organize").addEventListener("click", () => organizer.open());
    byId("organizer-add-part").addEventListener("click", () => this.#editEntry("part"));
    byId("organizer-add-section").addEventListener("click", () => this.#editEntry("section"));
    byId("schedule-list").addEventListener("click", (event) => {
      const add = event.target.closest('button[data-action="organize-add-part"]');
      if (add) return this.#editEntry("part", { sectionId: add.dataset.section });
      const button = event.target.closest('button[data-action="organize-edit"]');
      const item = button && this.#organizer.item(button.dataset.partId);
      if (item) this.#editEntry(item.entry.type, item);
    });
    byId("pacing-set-time").addEventListener("click", () => this.#openTiming());
    byId("custom-actions").addEventListener("click", () => this.#openActions());
    byId("catalog-new").addEventListener("click", () => this.#editName("create"));
    byId("catalog-retry").addEventListener("click", () => void this.#selectSchedules());
    byId("pacing-mode").addEventListener("change", (event) => {
      const mode = event.target.value;
      event.target.value = this.#store.state.schedule.pacingMode;
      void this.#send({ type: "set_pacing_mode", mode });
    });
    byId("workspace-dialog-close").addEventListener("click", () => this.#dialog.close());
    byId("workspace-cancel").addEventListener("click", () => this.#dialog.close());
    byId("workspace-form").addEventListener("submit", (event) => {
      event.preventDefault();
      void this.#confirm();
    });
    this.#dialog.addEventListener("close", () => {
      if (this.#dialog.open) return;
      this.#confirmation = null;
      byId("workspace-dialog-content").replaceChildren();
      this.#setError("");
    });
  }

  sync(state, previous) {
    const scopeChanged = !state.authenticated || !state.controlAvailable ||
      state.profile.id !== previous.profile.id ||
      state.serverInstanceId !== previous.serverInstanceId;
    const lostCatalog = !state.schedule.capabilities.manageCustomSchedules;
    const resetWorkspace = scopeChanged || (lostCatalog && previous.schedule.capabilities.manageCustomSchedules);
    if (resetWorkspace) this.#lastCustomScheduleId = "";
    const custom = state.schedule.customSchedule;
    if (custom && state.schedule !== previous.schedule && state.authenticated &&
      state.controlAvailable && !lostCatalog) {
      this.#lastCustomScheduleId = custom.id;
    }
    if (resetWorkspace) {
      this.#request++;
      this.#dialog.close();
      this.#editor.invalidate();
      if (state.catalog.status !== "idle" || state.browsingSchedules || state.scheduleSwitch) {
        this.#store.update({ browsingSchedules: false, scheduleSwitch: null, catalog: { status: "idle", schedules: [] } });
        return;
      }
    } else if (state.view !== "control") {
      this.#dialog.close();
      this.#editor.invalidate();
    } else if (state.live.state === "running" && previous.live.state !== "running") {
      this.#dialog.close();
      this.#editor.invalidate();
    }

    const previousCustom = previous.schedule.customSchedule;
    if (state.view === "control" && custom && state.authenticated &&
      (previous.view !== "control" || custom.id !== previousCustom?.id)) {
      void this.refreshCatalog();
    }
  }

  async refreshCatalog() {
    const { authenticated, controlAvailable, schedule, catalog } = this.#store.state;
    if (!authenticated || !controlAvailable || !schedule.capabilities.manageCustomSchedules) return [];
    const request = ++this.#request;
    this.#store.update({ catalog: { ...catalog, status: "loading" } });
    try {
      const schedules = await this.#loadCatalog();
      if (request !== this.#request) return [];
      this.#store.update({ catalog: { status: "ready", schedules } });
      return schedules;
    } catch {
      if (request === this.#request) {
        this.#store.update({ catalog: { ...catalog, status: "error" } });
      }
      return [];
    }
  }

  editPart(part) {
    const state = this.#store.state;
    if (state.live.state === "running" || state.organizer) return;
    const caps = state.schedule.capabilities;
    const canRename = caps.manageCustomSchedules && caps.controlParts;
    const canSetDuration = part.durationEditable && caps.setPlannedSeconds;
    if (!canRename && !canSetDuration) return;
    const minimum = state.schedule.customSchedule ? 1 : 60;
    this.#present(t("workspace.editPart"), {
      action: "workspace.save",
      fields: (container) => {
        const name = this.#input(container, "workspace.name", part.title, {
          maxlength: "200", disabled: canRename ? null : "",
        });
        const duration = this.#input(container, "part.durationInputLabel",
          formatPartLiveClock(part.plannedSeconds), { maxlength: "12", disabled: canSetDuration ? null : "" });
        let restore = false;
        if (canRename && part.titleOverridden && part.canonicalTitle) {
          name.addEventListener("input", () => { restore = false; });
          this.#action(container, "workspace.restoreTitle", () => {
            restore = true;
            name.value = part.canonicalTitle;
            name.focus();
          });
        }
        return { focus: canRename ? name : duration, submit: () => {
          if (canRename && !name.value.trim()) return this.#invalid("workspace.enterName", name);
          const seconds = canSetDuration ? this.#readDuration(duration, minimum) : part.plannedSeconds;
          if (seconds === null) return null;
          const fields = {};
          if (restore) fields.title = null;
          else if (canRename && name.value.trim() !== part.title) fields.title = name.value.trim();
          if (seconds !== part.plannedSeconds) fields.plannedSeconds = seconds;
          if (!Object.keys(fields).length) return { ok: true };
          return this.#send({
            type: "edit_part", scheduleRevision: state.scheduleRevision, partId: part.id, ...fields,
          }, { inline: true });
        } };
      },
    });
  }

  #editEntry(type, item = null) {
    if (!this.#organizer.canEdit) return;
    const { entry, sectionId = "" } = item ?? {};
    const isPart = type === "part";
    const titleKey = entry ? (isPart ? "workspace.editPart" : "workspace.editSection") :
      (isPart ? "scheduleEditor.addPart" : "scheduleEditor.addSection");
    const snapshotPart = isPart && this.#store.state.schedule.parts.find((part) => part.id === entry?.id);
    const restoreFocus = () => {
      if (!this.#organizer.draft) return;
      const edit = entry && byId("schedule-list").querySelector(
        `button[data-action="organize-edit"][data-part-id="${CSS.escape(entry.id)}"]`,
      );
      (edit || byId(isPart ? "organizer-add-part" : "organizer-add-section")).focus({ preventScroll: true });
    };
    this.#present(t(titleKey), {
      action: "workspace.save",
      onSuccess: restoreFocus,
      fields: (container) => {
        const name = this.#input(container, "workspace.name", entry?.title ?? "", { maxlength: "200" });
        const options = this.#organizer.draft.entries.filter((item) => item.type === "section");
        let duration, destination, appearance;
        if (isPart) {
          duration = this.#input(container, "part.durationInputLabel",
            formatPartLiveClock(entry?.plannedSeconds ?? 300), {
              maxlength: "12", disabled: snapshotPart && !snapshotPart.durationEditable ? "" : null,
            });
          if (options.length) destination = this.#select(container, "scheduleEditor.moveTo",
            [["", t("scheduleEditor.root")], ...options.map(({ id, title }) => [id, title])], sectionId);
        } else {
          appearance = sectionAppearanceFields(container, name, entry);
        }
        if (entry && !entry.parts?.length) {
          const remove = isPart ? "scheduleEditor.removePart" : "scheduleEditor.removeSection";
          this.#action(container, remove, () => this.#present(t(remove), {
            action: remove, text: entry.title, danger: true, onSuccess: restoreFocus, fields: () => ({
              submit: () => this.#draftResult(this.#organizer.remove(entry.id)),
            }),
          }), true);
        }
        return { focus: name, submit: () => {
          const title = name.value.trim();
          if (!title) return this.#invalid("workspace.enterName", name);
          const fields = { title };
          if (isPart) {
            fields.plannedSeconds = duration.disabled ? entry.plannedSeconds : this.#readDuration(duration, 1);
            if (fields.plannedSeconds === null) return null;
          } else {
            const color = appearance.readColor();
            if (color && !isSectionColor(color)) return this.#invalid("scheduleEditor.invalidColor", appearance.hex);
            fields.icon = appearance.readIcon();
            fields.color = color || null;
          }
          return this.#draftResult(this.#organizer.apply(type, entry?.id, fields, destination?.value ?? ""));
        } };
      },
    });
  }

  #draftResult(ok) {
    return ok ? { ok } : { ok: false, error: { code: "blocked", message: t("command.protected") } };
  }

  #input(container, key, value, attrs) {
    const input = element("input", { attrs: { type: "text", autocomplete: "off", ...attrs } });
    input.value = value;
    const label = element("label", { className: "field", text: t(key) });
    label.append(input);
    container.append(label);
    return input;
  }

  #select(container, key, choices, value) {
    const select = element("select");
    select.append(...choices.map(([id, text]) => element("option", { text, attrs: { value: id } })));
    select.value = value;
    const label = element("label", { className: "field", text: t(key) });
    label.append(select);
    container.append(label);
    return select;
  }

  #invalid(key, input) {
    this.#setError(t(key));
    input.focus();
    return null;
  }

  #readDuration(input, minimum) {
    const duration = parseDurationInput(input.value, { minimum });
    return duration ?? this.#invalid(minimum === 1 ? "scheduleEditor.invalidDuration" : "part.invalidDuration", input);
  }

  async #selectMeetings() {
    const state = this.#store.state;
    if (state.live.state === "running" || state.scheduleSwitch) return;
    if (state.schedule.customSchedule) {
      const result = await this.#switchSchedule({ type: "select_meeting_schedule" });
      if (!result.ok) return;
    }
    this.#store.update({ browsingSchedules: false });
  }

  async #selectSchedules() {
    const state = this.#store.state;
    if (state.live.state === "running" || state.scheduleSwitch ||
      (state.browsingSchedules && state.catalog.status === "loading")) return;
    if (!state.schedule.customSchedule) this.#store.update({ browsingSchedules: true });
    const catalog = await this.refreshCatalog();
    const current = this.#store.state;
    if (current.view !== "control" || !current.browsingSchedules || current.schedule.customSchedule) return;
    const target = catalog.find((schedule) => schedule.id === this.#lastCustomScheduleId) ?? catalog[0];
    if (target) await this.#selectSchedule(target.id, catalog);
  }

  async #selectSchedule(id, catalog = this.#store.state.catalog.schedules) {
    const state = this.#store.state;
    const target = catalog.find((schedule) => schedule.id === id);
    if (!target || state.live.state === "running" || state.scheduleSwitch) return false;
    const result = await this.#switchSchedule({
      type: "select_custom_schedule",
      scheduleId: target.id,
      customRevision: target.revision,
    });
    if (result.ok) this.#store.update({ browsingSchedules: false });
    else if (result.error.code === "schedule_stale") void this.refreshCatalog();
    return result.ok;
  }

  async #switchSchedule(command) {
    this.#store.update({ scheduleSwitch: command });
    try {
      return await this.#send(command);
    } finally {
      if (this.#store.state.scheduleSwitch === command) this.#store.update({ scheduleSwitch: null });
    }
  }

  #openTiming() {
    const state = this.#store.state;
    const schedule = state.schedule.customSchedule;
    if (!schedule || !state.schedule.capabilities.manageCustomSchedules ||
      state.live.state === "running") return;
    if (!state.schedule.capabilities.managePacing || state.organizer) return;
    this.#editor.open(schedule, state.scheduleRevision);
  }

  #openActions() {
    const state = this.#store.state;
    const schedule = state.schedule.customSchedule;
    if (!schedule || state.live.state === "running") return;
    const context = { schedule, scheduleRevision: state.scheduleRevision };
    const contents = this.#present(t("workspace.actions"));
    this.#action(contents, "workspace.newSchedule", () => this.#editName("create"));
    this.#action(contents, "workspace.rename", () => this.#editName("rename", context));
    this.#action(contents, "workspace.duplicate", () => this.#editName("duplicate", context));
    if (state.schedule.capabilities.managePacing) {
      this.#action(contents, "workspace.timing", () => {
        if (this.#store.state.schedule.customSchedule?.id !== schedule.id) return;
        this.#dialog.close();
        this.#openTiming();
      });
    } else {
      contents.append(element("p", { className: "workspace-note", text: t("pacing.configureInSolin") }));
    }
    if (state.schedule.parts.some((part) => part.state === "stopped")) {
      this.#action(contents, "workspace.resetResults", () => this.#confirmReset(context));
    }
    this.#action(contents, "workspace.delete", () => this.#confirmDelete(context), true);
  }

  #bound({ schedule, scheduleRevision }) {
    return { scheduleId: schedule.id, customRevision: schedule.revision, scheduleRevision };
  }

  #editName(operation, context = null) {
    const scheduleRevision = context?.scheduleRevision ?? this.#store.state.scheduleRevision;
    const cmds = {
      create: (title, firstPart) => ({
        type: "create_custom_schedule", scheduleRevision, title, ...(firstPart && { firstPart }),
      }),
      rename: (title) => ({
        type: "edit_custom_schedule", ...this.#bound(context),
        action: "rename_schedule", values: { title },
      }),
      duplicate: (title) => ({ type: "duplicate_custom_schedule", ...this.#bound(context), title }),
    };
    const action = { create: "workspace.newSchedule", rename: "workspace.rename", duplicate: "workspace.duplicate" }[operation];
    const name = operation === "rename" ? context.schedule.title :
      operation === "duplicate" ? t("workspace.copyTitle", { title: context.schedule.title }).slice(0, 200) : "";
    this.#present(t(action), {
      action: operation === "rename" ? "workspace.save" : action,
      name,
      allowFirstPart: operation === "create",
      command: cmds[operation],
      onSuccess: async (result) => {
        const catalog = await this.refreshCatalog();
        if (result.createdScheduleId) await this.#selectSchedule(result.createdScheduleId, catalog);
      },
    });
  }

  #confirmDelete(context) {
    this.#present(t("workspace.delete"), {
      action: "workspace.delete",
      danger: true,
      text: t("workspace.deleteConfirm", { title: context.schedule.title }),
      command: () => ({ type: "delete_custom_schedule", ...this.#bound(context) }),
      onSuccess: async () => {
        if (!this.#store.state.schedule.customSchedule) this.#store.update({ browsingSchedules: true });
        await this.refreshCatalog();
      },
    });
  }

  #confirmReset({ schedule, scheduleRevision }) {
    this.#present(t("workspace.resetResults"), {
      action: "workspace.resetResults",
      danger: true,
      text: t("workspace.resetConfirm", { title: schedule.title }),
      command: () => ({ type: "reset_all", scheduleRevision }),
    });
  }

  #action(container, key, callback, danger = false) {
    const button = element("button", {
      className: danger ? "button button--danger" : "button button--secondary",
      text: t(key),
      attrs: { type: "button" },
    });
    button.addEventListener("click", callback);
    container.append(button);
  }

  #present(title, confirmation = null) {
    const container = byId("workspace-dialog-content");
    const titleNode = byId("workspace-dialog-title");
    titleNode.textContent = title;
    this.#setError("");
    container.replaceChildren();
    this.#confirmation = confirmation;
    byId("workspace-dialog-actions").hidden = !confirmation;
    this.#submit.disabled = false;
    this.#submit.textContent = confirmation ? t(confirmation.action) : "";
    this.#submit.className = `button ${confirmation?.danger ? "button--danger" : "button--primary"}`;
    let focusTarget = byId("workspace-dialog-close");
    if (confirmation?.text) container.append(element("p", { text: confirmation.text }));
    if (confirmation?.name !== undefined) {
      const field = element("label", { className: "field", text: t("workspace.name") });
      focusTarget = element("input", { attrs: { type: "text", name: "title", maxlength: "200", autocomplete: "off" } });
      focusTarget.value = confirmation.name;
      field.append(focusTarget);
      container.append(field);
    }
    if (confirmation?.allowFirstPart) {
      const label = element("label", { className: "workspace-checkbox" });
      const addPart = element("input", { attrs: { type: "checkbox", name: "addPart" } });
      label.append(addPart, t("scheduleEditor.addPart"));
      const fields = element("div", { className: "workspace-first-part" });
      fields.hidden = true;
      for (const [name, key, value, maxlength] of [
        ["partTitle", "scheduleEditor.title", "", "200"],
        ["partDuration", "scheduleEditor.duration", "05:00", "12"],
      ]) {
        const field = element("label", { className: "field", text: t(key) });
        const input = element("input", { attrs: { type: "text", name, maxlength, autocomplete: "off" } });
        input.value = value;
        field.append(input);
        fields.append(field);
      }
      addPart.addEventListener("change", () => {
        fields.hidden = !addPart.checked;
      });
      container.append(label, fields);
    }
    if (confirmation?.fields) {
      const form = confirmation.fields(container);
      confirmation.submit = form.submit;
      focusTarget = form.focus ?? focusTarget;
    }
    if (!this.#dialog.open) this.#dialog.showModal();
    focusTarget.focus();
    if (focusTarget instanceof HTMLInputElement) focusTarget.select();
    return container;
  }

  #setError(message) {
    const node = byId("workspace-dialog-error");
    node.textContent = message;
    node.hidden = !message;
  }

  async #confirm() {
    const confirmation = this.#confirmation;
    if (!confirmation || this.#submit.disabled) return;
    this.#setError("");
    const pending = confirmation.submit ? confirmation.submit() : this.#submitCommand(confirmation);
    if (!pending) return;
    this.#submit.disabled = true;
    const result = await pending;
    if (confirmation !== this.#confirmation || !this.#dialog.open) return;
    this.#submit.disabled = false;
    if (!result.ok) {
      const stale = result.error.code === "schedule_stale";
      this.#submit.disabled = stale;
      this.#setError(stale ? t("scheduleEditor.draftStale") : result.error.message);
      return;
    }
    this.#dialog.close();
    await confirmation.onSuccess?.(result);
  }

  #submitCommand(confirmation) {
    const input = this.#dialog.querySelector('input[name="title"]');
    const title = input?.value.trim();
    if (input && !title) {
      this.#setError(t("workspace.enterName"));
      input.focus();
      return null;
    }
    let firstPart;
    if (this.#dialog.querySelector('input[name="addPart"]')?.checked) {
      const partTitle = this.#dialog.querySelector('input[name="partTitle"]');
      const partDuration = this.#dialog.querySelector('input[name="partDuration"]');
      const plannedSeconds = parseDurationInput(partDuration.value, { minimum: 1 });
      if (!partTitle.value.trim() || plannedSeconds === null) {
        const missingTitle = !partTitle.value.trim();
        this.#setError(t(missingTitle ? "scheduleEditor.invalidTitle" : "scheduleEditor.invalidDuration"));
        (missingTitle ? partTitle : partDuration).focus();
        return null;
      }
      firstPart = { title: partTitle.value.trim(), plannedSeconds };
    }
    return this.#send(confirmation.command(title, firstPart), { inline: true });
  }
}
