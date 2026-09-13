/* Minimal service worker: cache the app shell for offline use.

   CACHE_VERSION must be bumped whenever index.html or app.js changes,
   otherwise installed PWAs keep serving the old shell forever
   (caches.match returns the first cached copy, and there was no update
   path — that bit us once already). */
const CACHE_VERSION = 'v7';
const CACHE = `bambu-dl-${CACHE_VERSION}`;
const SHELL = ['/', '/static/app.js', '/manifest.webmanifest'];

self.addEventListener('install', (e) => {
  e.waitUntil(
    // Bypass any HTTP cache so the new version installs fresh bytes.
    caches.open(CACHE).then((c) => c.addAll(SHELL.map((u) => new Request(u, { cache: 'reload' }))))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (e) => {
  const url = new URL(e.request.url);
  // Never cache API calls, downloads, or thumbnails.
  if (url.pathname.startsWith('/api/') || url.pathname.startsWith('/thumb')) return;
  e.respondWith(
    // Network-first for the shell: users always get the current UI,
    // falling back to the cache only when offline.
    fetch(e.request)
      .then((resp) => {
        if (resp.ok && SHELL.includes(url.pathname)) {
          const copy = resp.clone();
          caches.open(CACHE).then((c) => c.put(e.request, copy));
        }
        return resp;
      })
      .catch(() => caches.match(e.request))
  );
});