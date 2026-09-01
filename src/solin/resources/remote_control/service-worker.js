const SHELL_REVISION = "__SOLIN_REMOTE_SHELL_REVISION__";
const CACHE_PREFIX = "solin-remote-shell-";
const CACHE_NAME = `${CACHE_PREFIX}${SHELL_REVISION}`;
const SHELL_RESOURCES = __SOLIN_REMOTE_SHELL_RESOURCES__;
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
            .filter((name) => name.startsWith(CACHE_PREFIX) && name !== CACHE_NAME)
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
    event.respondWith(cacheFirstShellResource(request));
  }
});

async function cacheFirstShellResource(request) {
  const cache = await caches.open(CACHE_NAME);
  const cached = await cache.match(request);
  if (cached) return cached;

  const response = await fetch(request, { cache: "reload" });
  if (response.ok && response.type === "basic") {
    await cache.put(request, response.clone());
  }
  return response;
}

async function networkFirstNavigation(request) {
  const cache = await caches.open(CACHE_NAME);
  try {
    return await fetch(request);
  } catch {
    return (await cache.match(new URL("./index.html", self.registration.scope).href))
      || Response.error();
  }
}
