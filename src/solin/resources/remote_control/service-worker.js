const CACHE_NAME = "solin-remote-shell-v16";
const SHELL_RESOURCES = [
  "./",
  "./index.html",
  "./manifest.webmanifest",
  "./messages.en.json",
  "./styles/tokens.css",
  "./styles/base.css",
  "./styles/layout.css",
  "./styles/components.css",
  "./scripts/api.js",
  "./scripts/app.js",
  "./scripts/format.js",
  "./scripts/i18n.js",
  "./scripts/pwa.js",
  "./scripts/renderer.js",
  "./scripts/setup.js",
  "./scripts/state.js",
  "./icons/icon-192.png",
  "./icons/icon-512.png",
  "./icons/icon-maskable-512.png",
];
const SHELL_URLS = new Set(
  SHELL_RESOURCES.map((path) => new URL(path, self.registration.scope).href),
);

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches
      .open(CACHE_NAME)
      .then((cache) =>
        cache.addAll(
          SHELL_RESOURCES.map(
            (path) =>
              new Request(new URL(path, self.registration.scope), {
                cache: "reload",
              }),
          ),
        ),
      )
      .then(() => self.skipWaiting()),
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((names) =>
        Promise.all(
          names
            .filter((name) => name !== CACHE_NAME)
            .map((name) => caches.delete(name)),
        ),
      )
      .then(() => self.clients.claim()),
  );
});

self.addEventListener("fetch", (event) => {
  const request = event.request;
  if (request.method !== "GET") return;

  const url = new URL(request.url);
  if (
    url.origin !== self.location.origin ||
    url.pathname.startsWith("/remote/api/") ||
    url.pathname === "/remote/trust-certificate.cer"
  ) return;

  if (request.mode === "navigate") {
    event.respondWith(networkFirstNavigation(request));
  } else if (SHELL_URLS.has(url.href)) {
    event.respondWith(networkFirstShellResource(request));
  }
});

async function networkFirstShellResource(request) {
  const cache = await caches.open(CACHE_NAME);
  try {
    const response = await fetch(request, { cache: "no-cache" });
    if (response.ok && response.type === "basic") {
      await cache.put(request, response.clone());
    }
    return response;
  } catch {
    return (await cache.match(request)) || Response.error();
  }
}

async function networkFirstNavigation(request) {
  try {
    return await fetch(request);
  } catch {
    return (await caches.match(new URL("./index.html", self.registration.scope).href)) || Response.error();
  }
}
