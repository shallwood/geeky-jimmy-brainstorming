const CACHE = "gj-brainstorming-shell-v56";
const SHELL = ["/", "/styles.css?v=terminal-green-27", "/app.js?v=terminal-green-38", "/manifest.webmanifest", "/Geeky%20Jimmy%20Brainstorming%20Icon.png"];
self.addEventListener("install", event => event.waitUntil(caches.open(CACHE).then(cache => cache.addAll(SHELL)).then(() => self.skipWaiting())));
self.addEventListener("activate", event => event.waitUntil(caches.keys().then(keys => Promise.all(keys.filter(key => key !== CACHE).map(key => caches.delete(key)))).then(() => self.clients.claim())));
self.addEventListener("fetch", event => {
  const url = new URL(event.request.url);
  if (event.request.method !== "GET" || url.pathname.startsWith("/api/") || (url.origin !== location.origin && url.origin !== "https://esm.sh")) return;
  if (url.pathname === "/mcp" || url.pathname.startsWith("/oauth/") || url.pathname.startsWith("/.well-known/")) return;
  event.respondWith(caches.match(event.request).then(cached => cached || fetch(event.request).then(response => {
    if (response.ok) { const copy = response.clone(); event.waitUntil(caches.open(CACHE).then(cache => cache.put(event.request, copy))); }
    return response;
  })));
});
