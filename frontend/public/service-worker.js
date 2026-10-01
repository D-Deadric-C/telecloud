const CACHE_VERSION = 'telecloud-shell-v7';
const SHELL_FILES = [
  '/',
  '/index.html',
  '/manifest.webmanifest',
  '/icons/telecloud.svg',
  '/icons/telecloud-192.png',
  '/icons/telecloud-512.png',
  '/icons/telecloud-maskable-512.png',
];
const API_PATHS = ['/auth', '/users', '/folders', '/files', '/shares', '/s/', '/jobs', '/health'];

async function cacheAppShell() {
  const cache = await caches.open(CACHE_VERSION);
  await cache.addAll(SHELL_FILES);

  // Vite fingerprints production assets. Discover those filenames from the
  // built HTML so CSS and JavaScript are available offline after the first load.
  const htmlResponse = await cache.match('/index.html');
  const html = await htmlResponse.text();
  const assets = [...html.matchAll(/(?:src|href)="(\/assets\/[^"?]+)"/g)]
    .map((match) => match[1]);
  if (assets.length) await cache.addAll([...new Set(assets)]);
}

self.addEventListener('install', (event) => {
  event.waitUntil(cacheAppShell());
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((key) => key !== CACHE_VERSION).map((key) => caches.delete(key))))
      .then(() => self.clients.claim()),
  );
});

self.addEventListener('fetch', (event) => {
  const { request } = event;
  if (request.method !== 'GET' || request.headers.has('range')) return;

  const url = new URL(request.url);
  if (url.origin !== self.location.origin || API_PATHS.some((path) => url.pathname.startsWith(path))) return;

  if (request.mode === 'navigate') {
    event.respondWith(
      fetch(request)
        .then((response) => {
          const copy = response.clone();
          caches.open(CACHE_VERSION).then((cache) => cache.put('/index.html', copy));
          return response;
        })
        .catch(() => caches.match('/index.html')),
    );
    return;
  }

  event.respondWith(
    caches.match(request).then((cached) => cached || fetch(request).then((response) => {
      if (response.ok) {
        const copy = response.clone();
        caches.open(CACHE_VERSION).then((cache) => cache.put(request, copy));
      }
      return response;
    })),
  );
});
