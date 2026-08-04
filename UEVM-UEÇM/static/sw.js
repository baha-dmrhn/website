const CACHE_NAME = "baha-enerji-shell-v19";
const NAVIGATION_TIMEOUT_MS = 700;
const LOADING_PATH = "/panel-hazirlaniyor";
const READY_HEADER = "X-Baha-Panel";
const STATIC_ASSETS = [
  "/login",
  "/login.css",
  "/login.js",
  "/suite-loading.js",
  "/panel-hazirlaniyor",
  "/panel-loading.js?v=4",
  "/manifest.webmanifest",
  "/suite-assets/baha-logo.png",
  "/suite-assets/icon-192.png",
  "/suite-assets/icon-512.png",
  "/suite-assets/apple-touch-icon.png",
];

function loadingFallback() {
  return caches.match(LOADING_PATH);
}

function shouldUseLoadingFallback(url) {
  return !url.searchParams.has("baha_ready");
}

function isBahaPanelResponse(response) {
  return response.ok && response.headers.get(READY_HEADER) === "ready";
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
  if (url.pathname === "/health") return;

  if (event.request.mode === "navigate") {
    if (url.pathname === LOADING_PATH) {
      event.respondWith(
        loadingFallback().then((cached) => cached || fetch(event.request)),
      );
      return;
    }
    const useLoadingFallback = shouldUseLoadingFallback(url);
    const network = fetch(event.request)
      .then((response) => {
        if (useLoadingFallback && !isBahaPanelResponse(response)) {
          return loadingFallback().then((fallback) => fallback || response);
        }
        return response;
      })
      .catch((error) => {
        if (useLoadingFallback) return loadingFallback();
        throw error;
      });
    if (!useLoadingFallback) {
      event.respondWith(network);
      return;
    }
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
