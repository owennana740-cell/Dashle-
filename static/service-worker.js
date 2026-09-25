const CACHE = 'dashle-static-v2';
const ASSETS = [
  '/static/manifest.json',
  '/static/logo.png',
  '/static/icons/dashle-icon-1024.png',
  '/static/icons/dashle-icon-512.png',
  '/static/icons/dashle-icon-192.png',
  '/static/icons/dashle-icon-48.png',
  '/static/icons/dashle-logo-header.png',
  '/static/icon-attach.png'
];

self.addEventListener('install', (event) => {
  event.waitUntil(caches.open(CACHE).then((cache) => cache.addAll(ASSETS)));
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) => Promise.all(
      keys.filter((key) => key !== CACHE).map((key) => caches.delete(key))
    ))
  );
  self.clients.claim();
});

self.addEventListener('fetch', (event) => {
  if (event.request.method !== 'GET') return;
  event.respondWith(caches.match(event.request).then((cached) => cached || fetch(event.request)));
});
