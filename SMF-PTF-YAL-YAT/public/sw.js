const CACHE_NAME = 'baha-enerji-v30';
const APP_SHELL = [
  '/', '/styles.css?v=22', '/app.js?v=30', '/config.js', '/manifest.webmanifest',
  '/assets/baha-logo.png', '/assets/icon-192.png',
  '/assets/icon-512.png', '/assets/apple-touch-icon.png'
];

self.addEventListener('install', event => {
  event.waitUntil(caches.open(CACHE_NAME).then(cache => cache.addAll(APP_SHELL)));
  self.skipWaiting();
});

self.addEventListener('activate', event => {
  event.waitUntil(caches.keys().then(keys => Promise.all(
    keys.filter(key => key !== CACHE_NAME).map(key => caches.delete(key))
  )));
  self.clients.claim();
});

self.addEventListener('fetch', event => {
  const request = event.request;
  const url = new URL(request.url);
  if (request.method !== 'GET' || url.pathname.startsWith('/api/')) return;

  if (request.mode === 'navigate') {
    event.respondWith(fetch(request)
      .then(response => {
        const copy = response.clone();
        caches.open(CACHE_NAME).then(cache => cache.put('/', copy));
        return response;
      })
      .catch(() => caches.match('/')));
    return;
  }

  event.respondWith(fetch(request).then(response => {
    if (response.ok || response.type === 'opaque') {
      const copy = response.clone();
      caches.open(CACHE_NAME).then(cache => cache.put(request, copy));
    }
    return response;
  }).catch(() => caches.match(request)));
});
