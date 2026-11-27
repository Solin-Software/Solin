const ITEM = "[data-drag-id][data-drag-type]";
const ZONE = "[data-drag-container]";
const HANDLE = "button[data-drag-handle]";
const THRESHOLD = 6;
const EDGE = 48;

function items(zone) {
  return Array.from(zone.children).filter((child) => child.matches(ITEM));
}

function info(item, zone) {
  return { id: item.dataset.dragId, type: item.dataset.dragType, sectionId: zone.dataset.dragContainer || "" };
}

function scrollParent(element) {
  for (let node = element; node; node = node.parentElement) {
    const overflow = getComputedStyle(node).overflowY;
    if (/(auto|scroll|overlay)/.test(overflow)) return node;
  }
  return document.scrollingElement;
}

function makeGhost(item, type) {
  const ghost = element("div", {
    className: "schedule-drag-ghost",
    attrs: { "aria-hidden": "true" },
  });
  ghost.append(uiIcon("grip"));
  if (type === "section") {
    const badge = item.querySelector(".section-icon-badge");
    if (badge) {
      ghost.dataset.section = item.dataset.section;
      if (item.dataset.sectionTone) ghost.dataset.sectionTone = item.dataset.sectionTone;
      ghost.append(badge.cloneNode(true));
    }
  }
  const title = item.querySelector(type === "section" ? ".section-title" : ".part-title");
  ghost.append(element("strong", { text: title?.textContent }));
  const duration = type === "part" && item.querySelector(".part-duration-badge");
  if (duration) ghost.append(duration.cloneNode(true));
  return ghost;
}

function captureDropTargets(root, source, top) {
  const bounds = (node) => {
    const rect = node.getBoundingClientRect();
    return { node, top: rect.top - top, bottom: rect.bottom - top, middle: rect.top - top + rect.height / 2 };
  };
  return items(root).filter((item) => item !== source).map((item) => {
    const target = bounds(item);
    const zone = item.querySelector(ZONE);
    if (zone) {
      target.zone = zone;
      target.parts = items(zone).filter((part) => part !== source).map(bounds);
    }
    return target;
  });
}

/** Pointer and same-container keyboard reordering for a stable schedule root. */
export class ScheduleDrag {
  constructor(root, { move, canStart = () => true } = {}) {
    if (!(root instanceof Element) || typeof move !== "function" || typeof canStart !== "function") {
      throw new TypeError("ScheduleDrag requires a root, move callback and optional canStart callback");
    }
    this.root = root;
    this.move = move;
    this.canStart = canStart;
    this.state = null;
    this.layoutAnimations = new WeakMap();
    this.onDown = this.onDown.bind(this);
    this.onMove = this.onMove.bind(this);
    this.onUp = this.onUp.bind(this);
    this.onCancel = this.onCancel.bind(this);
    this.onKey = this.onKey.bind(this);
    this.onClick = this.onClick.bind(this);
    this.onBlur = this.onBlur.bind(this);
    root.addEventListener("pointerdown", this.onDown);
    root.addEventListener("pointermove", this.onMove);
    root.addEventListener("pointerup", this.onUp);
    root.addEventListener("pointercancel", this.onCancel);
    root.addEventListener("lostpointercapture", this.onCancel);
    window.addEventListener("keydown", this.onKey);
    root.addEventListener("click", this.onClick, true);
    window.addEventListener("blur", this.onBlur);
  }

  target(target, fromCard = false) {
    if (!(target instanceof Element)) return null;
    let handle = target.closest(HANDLE);
    if (!handle && fromCard && !target.closest("button")) {
      handle = target.closest(".part-row, .section-header")?.querySelector(HANDLE);
    }
    const item = handle?.closest(ITEM);
    const zone = item?.parentElement;
    if (!handle || handle.disabled || !item || !zone?.matches(ZONE) || !this.root.contains(item)) return null;
    const data = info(item, zone);
    if (!data.id || !["part", "section"].includes(data.type) || (data.type === "section" && data.sectionId)) return null;
    return { handle, item, zone, data };
  }

  onDown(event) {
    if (this.state || !event.isPrimary || event.button !== 0) return;
    const target = this.target(event.target, event.pointerType === "mouse");
    if (!target || !this.canStart(target.data, event)) return;
    this.state = { ...target, pointerId: event.pointerId, x: event.clientX, y: event.clientY, startX: event.clientX, startY: event.clientY, active: false, frame: 0 };
    this.root.setPointerCapture(event.pointerId);
  }

  onMove(event) {
    const state = this.state;
    if (!state || event.pointerId !== state.pointerId) return;
    state.x = event.clientX;
    state.y = event.clientY;
    if (!state.active && Math.hypot(state.x - state.startX, state.y - state.startY) >= THRESHOLD) this.activate(state);
    if (state.active) {
      event.preventDefault();
      this.update(state);
      if (!state.frame) state.frame = requestAnimationFrame(() => this.tick(state));
    }
  }

  activate(state) {
    state.active = true;
    const rect = state.item.getBoundingClientRect();
    state.ghost = makeGhost(state.item, state.data.type);
    document.body.append(state.ghost);
    const { width, height } = state.ghost.getBoundingClientRect();
    state.ghostWidth = Math.min(width, rect.width);
    state.ghostHeight = height;
    state.offsetX = Math.min(state.startX - rect.left, state.ghostWidth / 2);
    state.offsetY = height / 2;
    // An explicitly held effect updates fixed geometry without inline styles or a new animation per move.
    state.position = state.ghost.animate([], { duration: 1, fill: "both" });
    state.position.pause();
    state.position.currentTime = 0;
    state.restoreFocus = document.activeElement === state.handle;
    state.item.classList.add("schedule-drag-source");
    this.root.classList.add("schedule-drag-active");
    state.scrollTarget = scrollParent(this.root);
    for (const item of this.root.querySelectorAll(ITEM)) this.layoutAnimations.get(item)?.cancel();
  }

  update(state) {
    const x = Math.max(4, Math.min(window.innerWidth - state.ghostWidth - 4, state.x - state.offsetX));
    const y = Math.max(4, Math.min(window.innerHeight - state.ghostHeight - 4, state.y - state.offsetY));
    state.position.effect.setKeyframes([{ width: `${state.ghostWidth}px`, transform: `translate(${x}px, ${y}px)`, offset: 0 }]);
    const { zone, before } = this.dropSlot(state);
    if (!zone) {
      this.clearDropIndicator(state);
      state.destination = null;
      return;
    }
    const sectionId = zone.dataset.dragContainer || "";
    const beforeId = before?.dataset.dragId || "";
    if (state.destination?.sectionId === sectionId && state.destination.beforeId === beforeId) return;
    this.clearDropIndicator(state);
    state.indicator = before ?? items(zone).filter((item) => item !== state.item).at(-1) ?? zone;
    state.indicator.dataset.dropSlot = before ? "before" : state.indicator === zone ? "inside" : "after";
    state.destination = { ...state.data, sectionId, beforeId };
  }

  clearDropIndicator(state) {
    if (!state.indicator) return;
    delete state.indicator.dataset.dropSlot;
    state.indicator = null;
  }

  dropSlot(state) {
    const rect = this.root.getBoundingClientRect();
    // Drop indicators leave the collapsed list's layout intact. Relative coordinates remain
    // valid as the scroll container moves.
    const y = state.y - rect.top;
    const hit = document.elementFromPoint(state.x, state.y);
    if (!hit || !state.scrollTarget.contains(hit) || state.x < rect.left || state.x > rect.right ||
      y < 0 || y > rect.height + 12) return {};
    if (rect.width !== state.targetWidth || rect.height !== state.targetHeight) {
      state.targets = captureDropTargets(this.root, state.item, rect.top);
      state.targetWidth = rect.width;
      state.targetHeight = rect.height;
    }
    for (const target of state.targets) {
      if (state.data.type === "part" && target.zone && y >= target.top + 12 && y < target.bottom - 6) {
        return { zone: target.zone, before: target.parts.find((part) => y < part.middle)?.node ?? null };
      }
      if (y < target.middle) return { zone: this.root, before: target.node };
    }
    return { zone: this.root, before: null };
  }

  tick(state) {
    state.frame = 0;
    if (this.state !== state || !state.active) return;
    const scroll = state.scrollTarget;
    if (scroll) {
      const isPage = scroll === document.scrollingElement;
      const rect = isPage ? { top: 0, bottom: window.innerHeight } : scroll.getBoundingClientRect();
      const delta = state.y < rect.top + EDGE ? -Math.ceil((rect.top + EDGE - state.y) / 4) : state.y > rect.bottom - EDGE ? Math.ceil((state.y - rect.bottom + EDGE) / 4) : 0;
      if (delta) {
        const old = scroll.scrollTop;
        scroll.scrollTop += Math.max(-20, Math.min(20, delta));
        if (scroll.scrollTop !== old) this.update(state);
      }
    }
    state.frame = requestAnimationFrame(() => this.tick(state));
  }

  onUp(event) {
    const state = this.state;
    if (!state || state.pointerId !== event.pointerId) return;
    if (state.active) {
      state.x = event.clientX;
      state.y = event.clientY;
      this.update(state);
      const destination = state.destination;
      const original = items(state.zone);
      const next = original[original.indexOf(state.item) + 1]?.dataset.dragId || "";
      const changed = destination && (destination.sectionId !== state.data.sectionId || destination.beforeId !== next);
      const positions = this.layoutPositions();
      if (positions.size) positions.set(state.data.id, state.ghost.getBoundingClientRect().top);
      this.suppressClick = true;
      setTimeout(() => { this.suppressClick = false; }, 0);
      this.cancel();
      if (changed) this.commit(destination, state.handle, positions);
      else this.animateLayout(positions);
    } else this.cancel();
  }

  onCancel(event) {
    if (this.state && event.pointerId === this.state.pointerId) this.cancel();
  }

  onBlur() { this.cancel(); }

  onClick(event) {
    if (this.suppressClick) {
      event.preventDefault();
      event.stopImmediatePropagation();
      this.suppressClick = false;
    }
  }

  onKey(event) {
    if (event.key === "Escape" && this.state) {
      event.preventDefault();
      this.cancel();
      return;
    }
    if (this.state || !["ArrowUp", "ArrowDown"].includes(event.key) || event.altKey || event.ctrlKey || event.metaKey || event.shiftKey) return;
    const target = this.target(event.target);
    if (!target || !this.canStart(target.data, event)) return;
    const siblings = items(target.zone);
    const index = siblings.indexOf(target.item);
    const neighbor = index + (event.key === "ArrowUp" ? -1 : 1);
    if (neighbor < 0 || neighbor >= siblings.length) return;
    event.preventDefault();
    const before = event.key === "ArrowUp" ? siblings[neighbor] : siblings[neighbor + 1];
    this.commit({ ...target.data, beforeId: before?.dataset.dragId || "" }, target.handle);
  }

  layoutPositions() {
    const bounds = new Map();
    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) return bounds;
    for (const item of this.root.querySelectorAll(ITEM)) {
      const rect = item.getBoundingClientRect();
      if (rect.bottom > 0 && rect.top < window.innerHeight) bounds.set(item.dataset.dragId, rect.top);
    }
    return bounds;
  }

  animateLayout(bounds) {
    if (!bounds.size) return;
    const entries = Array.from(this.root.querySelectorAll(ITEM));
    for (const item of entries) this.layoutAnimations.get(item)?.cancel();
    const offsets = new Map();
    for (const item of entries) {
      const previous = bounds.get(item.dataset.dragId);
      if (previous !== undefined && item.getClientRects().length) offsets.set(item, previous - item.getBoundingClientRect().top);
    }
    for (const [item, delta] of offsets) {
      const parent = item.parentElement.closest(ITEM);
      const offset = delta - (offsets.get(parent) ?? 0);
      if (Math.abs(offset) < 1) continue;
      const animation = item.animate([{ transform: `translateY(${offset}px)` }, { transform: "none" }], {
        duration: 175, easing: "cubic-bezier(.2,.7,.2,1)",
      });
      this.layoutAnimations.set(item, animation);
    }
  }

  commit(destination, handle, positions = this.layoutPositions()) {
    if (this.move(destination)) this.animateLayout(positions);
    if (handle.isConnected) handle.focus({ preventScroll: true });
    else {
      const item = Array.from(this.root.querySelectorAll(ITEM)).find((node) => node.dataset.dragId === destination.id && node.dataset.dragType === destination.type);
      item?.querySelector(HANDLE)?.focus({ preventScroll: true });
    }
  }

  cancel() {
    const state = this.state;
    if (!state) return;
    this.state = null;
    if (state.frame) cancelAnimationFrame(state.frame);
    state.position?.cancel();
    state.ghost?.remove();
    this.clearDropIndicator(state);
    state.item.classList.remove("schedule-drag-source");
    this.root.classList.remove("schedule-drag-active");
    if (this.root.hasPointerCapture(state.pointerId)) this.root.releasePointerCapture(state.pointerId);
    if (state.restoreFocus && state.handle.isConnected) state.handle.focus({ preventScroll: true });
  }

  destroy() {
    this.cancel();
    this.root.removeEventListener("pointerdown", this.onDown);
    this.root.removeEventListener("pointermove", this.onMove);
    this.root.removeEventListener("pointerup", this.onUp);
    this.root.removeEventListener("pointercancel", this.onCancel);
    this.root.removeEventListener("lostpointercapture", this.onCancel);
    window.removeEventListener("keydown", this.onKey);
    this.root.removeEventListener("click", this.onClick, true);
    window.removeEventListener("blur", this.onBlur);
  }
}

import { element, uiIcon } from "../../shared/scripts/dom.js";
