// Minimal service worker: enables PWA install + offline app shell.
// Usage data is always fetched fresh from the network (never cached stale).
const SHELL = "usage-shell-v8";
const SHELL_FILES = [
  "/",
  "/static/style.css",
  "/static/app.js",
  "/static/icon.svg",
  "/static/icon-192.png",
  "/static/icon-512.png",
  "/manifest.webmanifest",
];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(SHELL).then((c) => c.addAll(SHELL_FILES)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== SHELL).map((k) => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  // Never cache API calls — always go to network.
  if (url.pathname.startsWith("/api/")) return;
  // Cache-first for the static app shell, falling back to network.
  e.respondWith(caches.match(e.request).then((hit) => hit || fetch(e.request)));
});
