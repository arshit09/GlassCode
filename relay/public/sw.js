// Minimal service worker: lets phones install GlassesClaude as an app.
// It never caches requests or answers; everything goes straight to the network.
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (event) => event.waitUntil(self.clients.claim()));
self.addEventListener("fetch", () => {});
