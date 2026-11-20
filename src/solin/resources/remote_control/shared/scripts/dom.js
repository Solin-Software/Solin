export const byId = (id) => document.getElementById(id);

export function element(tag, { className, text, attrs, dataset } = {}) {
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

export function svgElement(tag, { className, attrs } = {}) {
  const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
  if (className) node.setAttribute("class", className);
  for (const [name, value] of Object.entries(attrs ?? {})) {
    if (value !== null && value !== undefined) node.setAttribute(name, String(value));
  }
  return node;
}

export function cssIcon(name, extraClass = "") {
  return element("span", {
    className: `css-icon css-icon--${name}${extraClass ? ` ${extraClass}` : ""}`,
    attrs: { "aria-hidden": "true" },
  });
}

export function uiIcon(name, extraClass = "") {
  const icon = svgElement("svg", {
    className: `ui-icon${extraClass ? ` ${extraClass}` : ""}`,
    attrs: { "aria-hidden": "true", focusable: "false" },
  });
  icon.append(svgElement("use", { attrs: { href: `#icon-${name}` } }));
  return icon;
}

export function setUiIcon(id, name) {
  byId(id)?.querySelector("use")?.setAttribute("href", `#icon-${name}`);
}

export function showToast(message, type = "info") {
  const region = byId("toast-region");
  if (!region) return;
  const toast = element("div", {
    className: `toast toast--${type}`,
    attrs: { role: type === "error" ? "alert" : "status" },
  });
  toast.append(
    element("span", { className: "toast-indicator", attrs: { "aria-hidden": "true" } }),
    element("span", { text: message }),
  );
  region.append(toast);
  while (region.children.length > 3) {
    region.firstElementChild?.remove();
  }
  window.setTimeout(() => toast.remove(), 4200);
}

export function announce(message) {
  const region = byId("assertive-status");
  if (!region) return;
  region.textContent = "";
  window.setTimeout(() => {
    region.textContent = message;
  }, 30);
}
