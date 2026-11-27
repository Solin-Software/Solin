import { newUuid } from "../../shared/scripts/api.js";
import { byId } from "../../shared/scripts/dom.js";
import { t } from "../../shared/scripts/i18n.js";
import { ScheduleDrag } from "./schedule-drag.js";

function editableEntries(entries) {
  return entries.map(({ type, id, title, parts, icon, color, plannedSeconds }) =>
    type === "section" ? { type, id, title, icon, color, parts: editableEntries(parts) } :
      { type, id, title, plannedSeconds });
}

function locate(entries, id) {
  for (const entry of entries) {
    if (entry.id === id) return { entry, entries, sectionId: "" };
    if (entry.type === "section") {
      const part = entry.parts.find((item) => item.id === id);
      if (part) return { entry: part, entries: entry.parts, sectionId: entry.id };
    }
  }
  return null;
}

export class ScheduleOrganizer {
  #store;
  #save;
  #drag;

  constructor({ store, save }) {
    this.#store = store;
    this.#save = save;
    this.#drag = new ScheduleDrag(byId("schedule-list"), {
      canStart: () => this.canEdit,
      move: (target) => this.move(target),
    });
    byId("organizer-cancel").addEventListener("click", () => this.close());
    byId("organizer-save").addEventListener("click", () => void this.commit());
  }

  get draft() {
    return this.#store.state.organizer;
  }

  get canEdit() {
    return Boolean(this.draft && !this.draft.saving && !this.draft.stale &&
      this.#store.state.connection === "online" && this.#store.state.live.state !== "running");
  }

  open() {
    const state = this.#store.state;
    const schedule = state.schedule.customSchedule;
    if (!schedule || !state.authenticated || !state.controlAvailable ||
      !state.schedule.capabilities.manageCustomSchedules || state.connection !== "online" ||
      state.live.state === "running" || this.draft) return;
    this.#store.update({ organizer: {
      scheduleId: schedule.id, scheduleRevision: state.scheduleRevision, customRevision: schedule.revision,
      entries: editableEntries(schedule.entries), dirty: false, saving: false, stale: false, error: "",
    } });
    byId("organizer-add-part").focus({ preventScroll: true });
  }

  close({ force = false } = {}) {
    const draft = this.draft;
    if (!draft || (!force && (draft.saving ||
      (draft.dirty && !window.confirm(t("scheduleEditor.unsavedConfirm")))))) return;
    this.#drag.cancel();
    this.#store.update({ organizer: null });
    if (!force) byId("custom-organize").focus({ preventScroll: true });
  }

  sync(state, previous) {
    if (!state.organizer) return;
    if (!state.authenticated || !state.controlAvailable || state.view !== "control" ||
      state.serverInstanceId !== previous.serverInstanceId || state.profile.id !== previous.profile.id ||
      !state.schedule.capabilities.manageCustomSchedules ||
      state.schedule.customSchedule?.id !== state.organizer.scheduleId) {
      this.close({ force: true });
    } else if (state.connection !== "online" || state.live.state === "running") {
      this.#drag.cancel();
      if (state.live.state === "running" && !state.organizer.stale && !state.organizer.saving) {
        this.#store.update({ organizer: { ...state.organizer, stale: true, error: t("scheduleEditor.draftStale") } });
      }
    }
  }

  item(id) {
    return this.draft && locate(this.draft.entries, id);
  }

  move({ id, sectionId, beforeId }) {
    return this.#change((entries) => {
      const source = locate(entries, id);
      const destination = sectionId
        ? entries.find((item) => item.type === "section" && item.id === sectionId)?.parts : entries;
      if (!source || !destination || (sectionId && source.entry.type === "section") || beforeId === id) return false;
      const index = source.entries.indexOf(source.entry);
      const anchor = beforeId ? destination.find((item) => item.id === beforeId) : null;
      if (beforeId && !anchor) return false;
      if (destination === source.entries && (destination[index + 1] === anchor ||
        (!anchor && index === destination.length - 1))) return false;
      source.entries.splice(index, 1);
      destination.splice(anchor ? destination.indexOf(anchor) : destination.length, 0, source.entry);
      return true;
    });
  }

  apply(type, id, values, sectionId) {
    return this.#change((entries) => {
      const old = id ? locate(entries, id) : null;
      if (id && old?.entry.type !== type) return false;
      const destination = sectionId
        ? entries.find((entry) => entry.type === "section" && entry.id === sectionId)?.parts : entries;
      if (!destination || (sectionId && type === "section")) return false;
      const updated = { ...(old?.entry ?? { type, id: newUuid() }), ...values };
      if (type === "section") updated.parts ??= [];
      if (old && old.entries === destination) {
        destination.splice(destination.indexOf(old.entry), 1, updated);
      } else {
        if (old) old.entries.splice(old.entries.indexOf(old.entry), 1);
        destination.push(updated);
      }
      return true;
    });
  }

  remove(id) {
    return this.#change((entries) => {
      const source = locate(entries, id);
      if (!source || source.entry.parts?.length) return false;
      source.entries.splice(source.entries.indexOf(source.entry), 1);
      return true;
    });
  }

  #change(operation) {
    if (!this.canEdit) return false;
    const draft = this.draft;
    const entries = editableEntries(draft.entries);
    if (!operation(entries)) return false;
    this.#store.update({ organizer: { ...draft, entries, dirty: true, error: "" } });
    return true;
  }

  async commit() {
    if (!this.canEdit || !this.draft.dirty) return;
    const { scheduleId, scheduleRevision, customRevision, entries } = this.draft;
    const saving = { ...this.draft, saving: true, error: "" };
    this.#drag.cancel();
    this.#store.update({ organizer: saving });
    const result = await this.#save({ scheduleId, scheduleRevision, customRevision, entries });
    if (this.draft !== saving) return;
    if (result.ok) {
      this.close({ force: true });
      byId("custom-organize").focus({ preventScroll: true });
      return;
    }
    const stale = result.error.code === "schedule_stale";
    const error = stale ? t("scheduleEditor.draftStale") : result.error.message;
    this.#store.update({ organizer: { ...saving, saving: false, stale, error } });
    (stale ? byId("organizer-cancel") : byId("organizer-save")).focus({ preventScroll: true });
  }
}
