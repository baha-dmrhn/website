const CACHE_NAME = "baha-enerji-shell-v15";
const NAVIGATION_TIMEOUT_MS = 1000;
const STATIC_ASSETS = [
  "/login",
  "/login.css",
  "/login.js",
  "/suite-loading.js",
  "/panel-hazirlaniyor",
  "/panel-loading.js",
  "/manifest.webmanifest",
  "/suite-assets/baha-logo.png",
  "/suite-assets/icon-192.png",
  "/suite-assets/icon-512.png",
  "/suite-assets/apple-touch-icon.png",
];

function loadingFallback() {
  return caches.match("/panel-hazirlaniyor");
}

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches
      .open(CACHE_NAME)
      .then((cache) => cache.addAll(STATIC_ASSETS))
      .then(() => self.skipWaiting()),
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) =>
        Promise.all(
          keys
            .filter((key) => key !== CACHE_NAME)
            .map((key) => caches.delete(key)),
        ),
      )
      .then(() => self.clients.claim()),
  );
});

self.addEventListener("fetch", (event) => {
  if (event.request.method !== "GET") return;
  const url = new URL(event.request.url);
  if (url.origin !== self.location.origin || url.pathname.startsWith("/api/")) return;

  if (event.request.mode === "navigate") {
    const network = fetch(event.request)
      .then((response) => {
        if (response.status >= 500) {
          return loadingFallback().then((fallback) => fallback || response);
        }
        return response;
      })
      .catch(() => loadingFallback());
    const fallbackAfterDelay = new Promise((resolve) => {
      setTimeout(() => loadingFallback().then(resolve), NAVIGATION_TIMEOUT_MS);
    });
    event.respondWith(Promise.race([network, fallbackAfterDelay]).then((response) => response || network));
    return;
  }

  event.respondWith(
    fetch(event.request)
      .then((response) => {
        if (response.ok && STATIC_ASSETS.includes(url.pathname)) {
          const copy = response.clone();
          caches.open(CACHE_NAME).then((cache) => cache.put(event.request, copy));
        }
        return response;
      })
      .catch(() => caches.match(event.request)),
  );
});
