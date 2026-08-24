/* DelayBahn service worker.
 *
 * Strategies:
 *   - App shell (HTML/CSS/JS/icons): precached on install, then cache-first with a
 *     background revalidate, so the UI opens instantly and works offline.
 *   - /api/* (connection + delay data): network-first. Responses are stamped with the
 *     time they were fetched; when the network is unavailable the cached copy is served
 *     ONLY if it is younger than API_MAX_AGE. Delay data is never served stale past that.
 *   - /stats/* (analytics) and non-GET requests (feedback POST): never intercepted.
 *
 * Bump SHELL_VERSION whenever the precached asset URLs below change (e.g. after a
 * ?v= cache-buster bump in index.html) so old shells are dropped and re-primed.
 */
const SHELL_VERSION = "v38";
const SHELL_CACHE = `delaybahn-shell-${SHELL_VERSION}`;
const API_CACHE = `delaybahn-api-${SHELL_VERSION}`;

// Delay/connection data older than this is treated as unusable when offline.
const API_MAX_AGE = 10 * 60 * 1000; // 10 minutes

// Keep the versioned URLs in step with static/index.html.
const PRECACHE = [
  "/",
  "/en/",
  "/style.css?v=83",
  "/app.js?v=103",
  "/manifest.json",
  "/favicon.png",
  "/logo.png?v=3",
  "/icons/icon-192.png",
  "/icons/icon-512.png",
];

const STATIC_ASSET = /\.(?:css|js|mjs|png|jpe?g|webp|gif|svg|ico|woff2?|json)$/i;

self.addEventListener("install", (event) => {
  event.waitUntil(
    (async () => {
      const cache = await caches.open(SHELL_CACHE);
      // Cache entries individually so one bad/renamed URL can't fail the whole install.
      await Promise.allSettled(PRECACHE.map((url) => cache.add(url)));
      await self.skipWaiting();
    })()
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    (async () => {
      const keep = new Set([SHELL_CACHE, API_CACHE]);
      const names = await caches.keys();
      await Promise.all(names.map((n) => (keep.has(n) ? null : caches.delete(n))));
      await self.clients.claim();
    })()
  );
});

self.addEventListener("fetch", (event) => {
  const req = event.request;
  const url = new URL(req.url);

  // Let the browser handle: non-GET (feedback POST, analytics POST), cross-origin,
  // analytics assets, and the service worker script itself.
  if (req.method !== "GET" || url.origin !== self.location.origin) return;
  if (url.pathname.startsWith("/stats/") || url.pathname === "/sw.js") return;

  if (url.pathname.startsWith("/api/")) {
    event.respondWith(networkFirstApi(req));
  } else if (req.mode === "navigate") {
    event.respondWith(networkFirstNavigation(req));
  } else if (STATIC_ASSET.test(url.pathname)) {
    event.respondWith(staleWhileRevalidate(req));
  }
  // anything else (e.g. /health) falls through to the network untouched
});

// Stamp a response with the moment it was fetched so freshness can be checked later.
async function withTimestamp(res) {
  const body = await res.blob();
  const headers = new Headers(res.headers);
  headers.set("sw-cached-at", Date.now().toString());
  return new Response(body, { status: res.status, statusText: res.statusText, headers });
}

function isFresh(res) {
  const t = Number(res.headers.get("sw-cached-at") || 0);
  return t > 0 && Date.now() - t < API_MAX_AGE;
}

async function networkFirstApi(req) {
  const cache = await caches.open(API_CACHE);
  try {
    const res = await fetch(req);
    if (res && res.ok) {
      cache.put(req, await withTimestamp(res.clone()));
    }
    return res;
  } catch (err) {
    const cached = await cache.match(req);
    if (cached && isFresh(cached)) return cached;
    // No network and nothing recent enough: fail explicitly rather than show stale data.
    return new Response(
      JSON.stringify({
        error: "offline",
        message: "Keine Verbindung und keine aktuellen Daten (unter 10 Minuten) im Cache.",
      }),
      { status: 503, headers: { "Content-Type": "application/json; charset=utf-8" } }
    );
  }
}

async function networkFirstNavigation(req) {
  const cache = await caches.open(SHELL_CACHE);
  try {
    const res = await fetch(req);
    // Don't cache redirects (e.g. /entschaedigung/ -> /entschaedigung): a redirected
    // response replayed for a navigation throws in some browsers.
    if (res && res.ok && !res.redirected) cache.put(req, res.clone());
    return res;
  } catch (err) {
    // fall back to the shell of the language the request was for
    const home = new URL(req.url).pathname.startsWith("/en") ? "/en/" : "/";
    return (await cache.match(req)) || (await cache.match(home)) || offlinePage(home);
  }
}

async function staleWhileRevalidate(req) {
  const cache = await caches.open(SHELL_CACHE);
  const cached = await cache.match(req);
  const network = fetch(req)
    .then((res) => {
      if (res && res.ok) cache.put(req, res.clone());
      return res;
    })
    .catch(() => null);
  return cached || (await network) || new Response("", { status: 504 });
}

function offlinePage(home = "/") {
  const lang = home === "/en/" ? "en" : "de";
  const lead = lang === "en"
    ? "This page isn't available right now. Please try again later."
    : "Diese Seite ist gerade nicht verfügbar. Bitte später erneut versuchen.";
  return new Response(
    `<!doctype html><html lang=${lang}><meta charset=utf-8><title>Offline</title>` +
      "<body style=\"font-family:system-ui;padding:2rem;text-align:center;color:#282d37\">" +
      `<h1>Offline</h1><p>${lead}</p>`,
    { status: 503, headers: { "Content-Type": "text/html; charset=utf-8" } }
  );
}
