export function setupPwa({ onInstallAvailable, onInstalled, onError }) {
  let installPrompt = null;
  let installAvailable = false;

  const publishInstallAvailability = (available) => {
    installAvailable = available;
    onInstallAvailable(available);
  };

  window.addEventListener("beforeinstallprompt", (event) => {
    event.preventDefault();
    installPrompt = event;
    publishInstallAvailability(true);
  });

  window.addEventListener("appinstalled", () => {
    installPrompt = null;
    publishInstallAvailability(false);
    onInstalled();
  });

  const install = async () => {
    if (!installPrompt) return false;
    const prompt = installPrompt;
    installPrompt = null;
    publishInstallAvailability(false);
    await prompt.prompt();
    const result = await prompt.userChoice;
    return result.outcome === "accepted";
  };

  let serviceWorkerReady = Promise.resolve(null);
  if ("serviceWorker" in navigator && isSecureContext) {
    const replacingController = Boolean(navigator.serviceWorker.controller);
    let reloadingForUpdate = false;
    navigator.serviceWorker.addEventListener("controllerchange", () => {
      if (!replacingController || reloadingForUpdate) return;
      reloadingForUpdate = true;
      window.location.reload();
    });
    serviceWorkerReady = navigator.serviceWorker
      .register("./service-worker.js", {
        scope: "/remote/",
        updateViaCache: "none",
      })
      .then(async (registration) => {
        try {
          await registration.update();
        } catch (error) {
          onError(error);
        }
        return navigator.serviceWorker.ready;
      })
      .catch((error) => {
        onError(error);
        return null;
      });
  }

  return {
    install,
    isInstallAvailable: () => installAvailable,
    isStandalone: () => isStandalone(),
    serviceWorkerReady,
  };
}

function isStandalone() {
  return Boolean(
    window.matchMedia?.("(display-mode: standalone)").matches
      || window.navigator.standalone === true,
  );
}
