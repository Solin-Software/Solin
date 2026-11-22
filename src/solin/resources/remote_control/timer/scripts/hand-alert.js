import { byId } from "../../shared/scripts/dom.js";
import { currentLocale } from "../../shared/scripts/i18n.js";
import { formatWallClock } from "./timer-display.js";

const EMPTY_NAMES = Object.freeze([]);
const MIN_NAMES_FOR_READOUT = 3;
const PRESENCE_DURATION_MS = 150;

function updateText(element, value) {
  if (element.textContent !== value) element.textContent = value;
}

export class StageHandAlert {
  #stage = byId("stage-view");
  #hud = byId("stage-hud");
  #readout = byId("stage-sr-readout");
  #layer = byId("stage-hand-alert");
  #scrim = byId("stage-hand-alert-scrim");
  #manual = byId("stage-hand-alert-manual");
  #card = byId("stage-hand-alert-card");
  #header = byId("stage-hand-alert-header");
  #title = byId("stage-hand-alert-title");
  #count = byId("stage-hand-alert-count");
  #time = byId("stage-hand-alert-time");
  #primary = byId("stage-hand-alert-primary");
  #seconds = byId("stage-hand-alert-seconds");
  #suffix = byId("stage-hand-alert-suffix");
  #list = byId("stage-hand-alert-list");
  #footer = byId("stage-hand-alert-footer");
  #remaining = byId("stage-hand-alert-remaining");
  #expand = byId("stage-hand-alert-expand");
  #close = byId("stage-hand-alert-close");

  #portrait = window.matchMedia("(orientation: portrait)");
  #reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");
  #alert = null;
  #names = EMPTY_NAMES;
  #shownRows = 0;
  #locale = "";
  #numberFormat;
  #overflowFormat;
  #expanded = false;
  #movement = null;
  #presenceAnimations = new WeakMap();

  constructor() {
    this.#expand.addEventListener("click", () => this.#setExpanded(true));
    this.#close.addEventListener("click", () => this.#setExpanded(false));
    this.#scrim.addEventListener("click", () => this.#setExpanded(false));
    for (const eventName of ["click", "pointermove"]) {
      this.#layer.addEventListener(eventName, (event) => event.stopPropagation());
    }
    this.#card.addEventListener("keydown", (event) => {
      if (this.#expanded && event.key === "Escape") {
        event.preventDefault();
        event.stopPropagation();
        this.#setExpanded(false);
      }
    });
    this.#reducedMotion.addEventListener("change", (event) => {
      if (event.matches) {
        this.#movement?.cancel();
        this.render(this.#alert, { immediate: true });
      }
    });

    const observer = new ResizeObserver(() => this.#fitPreview());
    observer.observe(this.#stage);
    observer.observe(this.#layer);
    observer.observe(this.#header);
  }

  render(alert, { immediate = false } = {}) {
    const locale = currentLocale();
    if (
      alert === this.#alert && locale === this.#locale &&
      (!immediate || (this.#card.hidden && this.#manual.hasAttribute("hidden")))
    ) return;
    if (locale !== this.#locale) {
      this.#locale = locale;
      this.#numberFormat = new Intl.NumberFormat(locale);
      this.#overflowFormat = new Intl.NumberFormat(locale, { signDisplay: "always" });
    }
    this.#alert = alert;
    const automatic = alert?.kind === "automatic";
    const names = automatic ? alert.names : EMPTY_NAMES;
    const named = names.length > 0;
    const hadFocus = this.#card.contains(document.activeElement);

    this.#setPresence(this.#manual, alert?.kind === "manual", immediate);
    if (!automatic) {
      // Release focus and controls while the card finishes at its current position.
      this.#syncExpanded(false, { keepPosition: true });
      this.#scrim.inert = true;
      this.#setPresence(this.#card, false, immediate, () => this.#clearCard());
      if (hadFocus && !this.#stage.hidden && !this.#stage.inert) {
        this.#stage.focus({ preventScroll: true });
      }
      return;
    }

    if (this.#card.inert) this.#layer.dataset.expanded = "false";
    if (!named) {
      this.#movement?.cancel();
      this.#syncExpanded(false);
      this.#footer.hidden = true;
      this.#remaining.textContent = "";
    }
    this.#scrim.hidden = !named;
    this.#scrim.inert = false;
    this.#card.dataset.mode = named ? "names" : "count";
    this.#title.classList.toggle("visually-hidden", !named);
    this.#count.textContent = this.#numberFormat.format(alert.count);
    if (!named || this.#expanded) this.#time.hidden = names.length < MIN_NAMES_FOR_READOUT;
    this.#list.hidden = !named;
    this.#syncNames(names);
    this.#setPresence(this.#card, true, immediate);

    if (this.#expanded) {
      this.#showRows(names.length);
    } else if (named) {
      this.#fitPreview();
    } else if (hadFocus && !this.#stage.hidden && !this.#stage.inert) {
      this.#stage.focus({ preventScroll: true });
    }
  }

  #setPresence(element, shown, immediate, onHidden) {
    const previous = this.#presenceAnimations.get(element);
    if (immediate || this.#reducedMotion.matches) {
      previous?.cancel();
      this.#presenceAnimations.delete(element);
      element.toggleAttribute("inert", !shown);
      element.toggleAttribute("hidden", !shown);
      if (!shown) onHidden?.();
      return;
    }
    const hidden = element.hasAttribute("hidden");
    if (hidden ? !shown : shown === !element.hasAttribute("inert")) return;

    const from = hidden ? 0 : Number(getComputedStyle(element).opacity);
    previous?.cancel();
    element.toggleAttribute("inert", !shown);
    element.removeAttribute("hidden");
    const to = shown ? Number(getComputedStyle(element).opacity) : 0;
    const animation = element.animate(
      [{ opacity: from }, { opacity: to }],
      { duration: PRESENCE_DURATION_MS, easing: "ease-out", fill: "both" },
    );
    this.#presenceAnimations.set(element, animation);
    animation.onfinish = () => {
      if (this.#presenceAnimations.get(element) !== animation) return;
      this.#presenceAnimations.delete(element);
      element.toggleAttribute("hidden", !shown);
      animation.cancel();
      if (!shown) onHidden?.();
    };
  }

  #clearCard() {
    this.#movement?.cancel();
    this.#layer.dataset.expanded = "false";
    this.#scrim.hidden = true;
    this.#list.hidden = true;
    this.#footer.hidden = true;
    this.#time.hidden = true;
    this.#count.textContent = "";
    this.#remaining.textContent = "";
    this.#syncNames(EMPTY_NAMES);
  }

  renderTime(evaluated, model) {
    if (this.#time.hidden || this.#card.inert) return;
    const face = model.display_mode === "clock" ? model.clock : model.timer;
    const [primary, seconds, suffix] = face.primary_text
      ? [face.primary_text, face.seconds_text, face.secondary_text]
      : formatWallClock(
          evaluated.wallClockEpoch,
          evaluated.clockConfig,
          evaluated.utcOffsetMinutes,
        );
    this.#time.classList.toggle("is-overrun", Boolean(face.overrun));
    this.#seconds.hidden = !seconds;
    this.#suffix.hidden = !suffix;
    updateText(this.#primary, primary);
    updateText(this.#seconds, seconds);
    updateText(this.#suffix, suffix);
  }

  #syncNames(names) {
    if (
      names.length === this.#names.length &&
      names.every((name, index) => name === this.#names[index])
    ) {
      return;
    }
    this.#names = names;
    const rows = names.map((name) => {
      const row = document.createElement("li");
      row.textContent = name;
      row.dir = "auto";
      row.hidden = true;
      return row;
    });
    this.#list.replaceChildren(...rows);
    this.#shownRows = 0;
  }

  #showRows(count) {
    const children = this.#list.children;
    for (
      let index = Math.min(count, this.#shownRows);
      index < Math.max(count, this.#shownRows);
      index += 1
    ) {
      children[index].hidden = index >= count;
    }
    this.#shownRows = count;
  }

  #fitPreview() {
    if (this.#expanded || this.#list.hidden || this.#card.inert) return;
    const total = this.#names.length;
    const portrait = this.#portrait.matches;
    const limit = Math.min(total, portrait ? 8 : 4);
    const returnFocus = document.activeElement === this.#expand;
    this.#showRows(limit);
    this.#footer.hidden = total === limit;
    const rowHeight = this.#list.firstElementChild.getBoundingClientRect().height;
    const rowsThatFit = () => Math.min(
      limit,
      Math.floor((this.#list.getBoundingClientRect().height + 0.25) / rowHeight),
    );
    let visible = rowHeight > 0 ? rowsThatFit() : 0;
    if (this.#footer.hidden && visible < total && rowHeight > 0) {
      this.#footer.hidden = false;
      visible = rowsThatFit();
    }
    this.#showRows(visible);
    if (!this.#footer.hidden) {
      this.#remaining.textContent = this.#overflowFormat.format(total - visible);
    }
    this.#time.hidden = portrait ? total < MIN_NAMES_FOR_READOUT : !this.#previewReachesReadout();
    if (returnFocus && document.activeElement !== this.#expand) {
      (this.#footer.hidden ? this.#stage : this.#expand).focus({ preventScroll: true });
    }
  }

  #previewReachesReadout() {
    const cutoff = Number(getComputedStyle(this.#layer).getPropertyValue("--hand-preview-stop"));
    const stage = this.#stage.getBoundingClientRect();
    // Measure the layout position even while the card animates back from the dialog.
    const bottom = this.#layer.getBoundingClientRect().top + this.#card.offsetTop +
      this.#card.offsetHeight;
    return bottom - stage.top >= stage.height * cutoff;
  }

  #syncExpanded(expanded, { keepPosition = false } = {}) {
    if (this.#expanded === expanded) return;
    this.#expanded = expanded;
    if (!keepPosition) this.#layer.dataset.expanded = String(expanded);
    this.#close.hidden = !expanded;
    this.#hud.inert = expanded;
    this.#readout.inert = expanded;
    this.#stage.tabIndex = expanded ? -1 : 0;
    if (expanded) {
      this.#card.setAttribute("role", "dialog");
      this.#card.setAttribute("aria-modal", "true");
      this.#list.tabIndex = 0;
      this.#footer.hidden = true;
    } else {
      this.#card.removeAttribute("role");
      this.#card.removeAttribute("aria-modal");
      this.#list.removeAttribute("tabindex");
    }
  }

  #setExpanded(expanded) {
    if (this.#expanded === expanded) return;
    const animate = !this.#reducedMotion.matches;
    const before = animate ? this.#card.getBoundingClientRect() : null;
    this.#movement?.cancel();
    this.#syncExpanded(expanded);
    if (expanded) {
      this.#time.hidden = this.#names.length < MIN_NAMES_FOR_READOUT;
      this.#showRows(this.#names.length);
    } else {
      this.#fitPreview();
    }
    this.#list.scrollTop = 0;

    if (before) {
      const after = this.#card.getBoundingClientRect();
      const offset = "translate(" + (before.left - after.left) + "px, " +
        (before.top - after.top) + "px)";
      const scale = "scale(" + before.width / after.width + ", " +
        before.height / after.height + ")";
      this.#movement = this.#card.animate(
        [
          { transformOrigin: "top left", transform: offset + " " + scale },
          { transformOrigin: "top left", transform: "none" },
        ],
        { duration: 320, easing: "cubic-bezier(.22, .76, .27, 1)" },
      );
    }
    const target = expanded ? this.#close : this.#footer.hidden ? this.#stage : this.#expand;
    target.focus({ preventScroll: true });
  }
}
