export function setupPwa({ onInstallAvailable, onInstalled, onError }) {
  let installPrompt = null;

  window.addEventListener("beforeinstallprompt", (event) => {
    event.preventDefault();
    installPrompt = event;
    onInstallAvailable(true);
  });

  window.addEventListener("appinstalled", () => {
    installPrompt = null;
    onInstallAvailable(false);
    onInstalled();
  });

  const install = async () => {
    if (!installPrompt) return false;
    const prompt = installPrompt;
    installPrompt = null;
    onInstallAvailable(false);
    await prompt.prompt();
    const result = await prompt.userChoice;
    return result.outcome === "accepted";
  };

  if ("serviceWorker" in navigator && isSecureContext) {
    const replacingController = Boolean(navigator.serviceWorker.controller);
    let reloadingForUpdate = false;
    navigator.serviceWorker.addEventListener("controllerchange", () => {
      if (!replacingController || reloadingForUpdate) return;
      reloadingForUpdate = true;
      window.location.reload();
    });
    window.addEventListener("load", () => {
      navigator.serviceWorker
        .register("./service-worker.js", {
          scope: "/remote/",
          updateViaCache: "none",
        })
        .catch(onError);
    }, { once: true });
  }

  return { install };
}
