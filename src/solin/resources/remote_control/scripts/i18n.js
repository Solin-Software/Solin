const SOURCE_CATALOG_URL = new URL("../messages.en.json", import.meta.url);

let sourceMessages = Object.freeze({});
let activeMessages = Object.freeze({});
let activeLocale = "en";

export async function initializeLocalization(loadRemote) {
  sourceMessages = Object.freeze(await loadSourceCatalog());
  applyLocalization({ locale: "en", messages: {} });

  try {
    const remote = await loadRemote();
    applyLocalization(remote);
  } catch {
    // The service-worker-cached English shell keeps the offline login usable.
  }
}

export function applyLocalization(localization) {
  const locale = normalizeLocale(localization?.locale);
  const messages = isMessageCatalog(localization?.messages)
    ? localization.messages
    : {};
  activeLocale = locale;
  activeMessages = Object.freeze({ ...sourceMessages, ...messages });
  document.documentElement.lang = activeLocale;
  localizeDocument();
}

export function t(key, parameters = {}) {
  const template = activeMessages[key] ?? sourceMessages[key] ?? key;
  return String(template).replace(/\{([A-Za-z][A-Za-z0-9_]*)\}/g, (match, name) =>
    Object.hasOwn(parameters, name) ? String(parameters[name]) : match,
  );
}

export function tp(baseKey, count, parameters = {}) {
  let category = "other";
  try {
    category = new Intl.PluralRules(activeLocale).select(Number(count));
  } catch {
    category = Number(count) === 1 ? "one" : "other";
  }
  const candidate = `${baseKey}.${category}`;
  const key = Object.hasOwn(activeMessages, candidate) ? candidate : `${baseKey}.other`;
  return t(key, { ...parameters, count });
}

export function currentLocale() {
  return activeLocale;
}

function localizeDocument() {
  document.title = t("app.title");
  for (const node of document.querySelectorAll("[data-i18n]")) {
    node.textContent = t(node.dataset.i18n);
  }
  for (const node of document.querySelectorAll("[data-i18n-aria-label]")) {
    node.setAttribute("aria-label", t(node.dataset.i18nAriaLabel));
  }
}

async function loadSourceCatalog() {
  try {
    const response = await fetch(SOURCE_CATALOG_URL, { cache: "force-cache" });
    if (!response.ok) return {};
    const payload = await response.json();
    return isMessageCatalog(payload) ? payload : {};
  } catch {
    return {};
  }
}

function isMessageCatalog(value) {
  return Boolean(
    value &&
      typeof value === "object" &&
      !Array.isArray(value) &&
      Object.entries(value).every(
        ([key, message]) => key.length > 0 && typeof message === "string",
      ),
  );
}

function normalizeLocale(locale) {
  const requested = String(locale || "en").replaceAll("_", "-");
  try {
    return Intl.DateTimeFormat.supportedLocalesOf([requested])[0] || "en";
  } catch {
    return "en";
  }
}
