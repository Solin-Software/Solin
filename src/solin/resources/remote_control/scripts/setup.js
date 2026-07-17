import { t } from "./i18n.js";

export function isSetupRoute() {
  return new URLSearchParams(window.location.search).get("setup") === "1";
}

export function initializeSetup({ payload, pwa }) {
  const view = document.getElementById("setup-view");
  const unavailable = document.getElementById("setup-unavailable");
  const steps = document.getElementById("setup-steps");
  const certificateLink = document.getElementById("setup-certificate-link");
  const verificationCode = document.getElementById("setup-verification-code");
  const fingerprint = document.getElementById("setup-fingerprint");
  const installStep = document.getElementById("setup-install-step");
  const installButton = document.getElementById("setup-install-button");
  const installManuals = document.querySelectorAll("[data-install-manual]");
  const platform = detectPlatform();

  const tls = isTlsSetup(payload?.tls) ? payload.tls : null;
  unavailable.hidden = Boolean(tls);
  if (tls) {
    steps.removeAttribute("aria-disabled");
    certificateLink.removeAttribute("aria-disabled");
    certificateLink.href = tls.authorityCertificateUrl;
    verificationCode.textContent = tls.verificationCode;
    fingerprint.textContent = tls.authoritySha256;
  } else {
    steps.setAttribute("aria-disabled", "true");
    certificateLink.setAttribute("aria-disabled", "true");
    certificateLink.removeAttribute("href");
  }

  showPlatformHelp(platform);

  const markInstalled = () => {
    installStep.hidden = true;
  };

  const setInstallAvailable = (available) => {
    if (pwa.isStandalone()) {
      markInstalled();
      return;
    }
    installButton.hidden = !available;
    for (const note of installManuals) {
      note.hidden = available || note.dataset.platformHelp !== platform;
    }
  };

  installButton.addEventListener("click", async () => {
    installButton.disabled = true;
    try {
      const accepted = await pwa.install();
      if (!accepted) setInstallAvailable(false);
    } finally {
      installButton.disabled = false;
    }
  });

  setInstallAvailable(pwa.isInstallAvailable());

  return {
    show() {
      document.getElementById("boot-view").hidden = true;
      document.getElementById("login-view").hidden = true;
      document.getElementById("app-shell").hidden = true;
      view.hidden = false;
      document.title = `${t("setup.title")} · Solin`;
    },
    setInstallAvailable,
    markInstalled,
  };
}

function isTlsSetup(value) {
  return Boolean(
    value
      && typeof value === "object"
      && typeof value.authoritySha256 === "string"
      && value.authoritySha256.length > 0
      && typeof value.verificationCode === "string"
      && value.verificationCode.length > 0
      && typeof value.authorityCertificateUrl === "string"
      && value.authorityCertificateUrl.startsWith("/remote/"),
  );
}

function detectPlatform() {
  const userAgent = navigator.userAgent || "";
  if (/Android/i.test(userAgent)) return "android";
  if (
    /iPhone|iPad|iPod/i.test(userAgent)
    || (/Macintosh/i.test(userAgent) && navigator.maxTouchPoints > 1)
  ) return "apple";
  return "generic";
}

function showPlatformHelp(platform) {
  for (const node of document.querySelectorAll("[data-platform-help]")) {
    node.hidden = node.dataset.platformHelp !== platform;
  }
}
