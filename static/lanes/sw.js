/* Lanes service worker: keep the app and the boards/cards you've opened usable offline.
   Pages and card details: network first, saved copy when offline. Static files: saved copy first.
   Changes made offline are queued by offline.js in the page, not here. */
const VERSION = '__VERSION__';
const STATIC = `lanes-static-${VERSION}`;
const PAGES = 'lanes-pages-v1';
const OFFLINE_URL = '/offline/';

self.addEventListener('install', e => {
  e.waitUntil(caches.open(PAGES).then(c => c.add(new Request(OFFLINE_URL, { credentials: 'same-origin' }))).catch(() => {}));
  self.skipWaiting();
});

self.addEventListener('activate', e => {
  e.waitUntil((async () => {
    for (const k of await caches.keys()) if (k.startsWith('lanes-static-') && k !== STATIC) await caches.delete(k);
    await self.clients.claim();
  })());
});

const keepable = url => /^\/(b\/\d+\/[a-z]*\/?|cards\/\d+\/|api\/boards\/\d+\/state\/|my-cards\/|planner\/|)$/.test(url.pathname)
  || url.pathname === '/';

async function networkFirst(req, fallbackOffline) {
  const cache = await caches.open(PAGES);
  try {
    const res = await fetch(req);
    if (res.ok && res.type === 'basic' && !res.redirected) cache.put(stripSearch(req), res.clone());
    return res;
  } catch {
    const hit = await cache.match(stripSearch(req));
    if (hit) return hit;
    if (fallbackOffline) return (await cache.match(OFFLINE_URL)) || new Response('Offline', { status: 503 });
    return new Response(JSON.stringify({ error: 'offline', offline: true }), { status: 503, headers: { 'Content-Type': 'application/json' } });
  }
}

// ?card=12 or ?v=31 shouldn't make a separate copy of the same page
function stripSearch(req) {
  const u = new URL(req.url);
  if (u.pathname.startsWith('/api/boards/')) { u.search = ''; return u.toString(); }
  if (u.searchParams.has('card')) u.searchParams.delete('card');
  return u.toString();
}

self.addEventListener('fetch', e => {
  const req = e.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  if (url.origin !== location.origin) return;
  if (url.pathname.startsWith('/static/')) {
    e.respondWith(caches.open(STATIC).then(async c => (await c.match(req)) || fetch(req).then(r => { if (r.ok) c.put(req, r.clone()); return r; })));
    return;
  }
  if (req.mode === 'navigate') { e.respondWith(networkFirst(req, true)); return; }
  if (keepable(url)) e.respondWith(networkFirst(req, false));
});

// The page asks us to keep its cards for offline use (a few at a time, quietly)
self.addEventListener('message', e => {
  const d = e.data || {};
  if (d.type === 'keep' && Array.isArray(d.urls)) {
    e.waitUntil((async () => {
      const cache = await caches.open(PAGES);
      for (const u of d.urls.slice(0, 80)) {
        try {
          const res = await fetch(u, { credentials: 'same-origin' });
          if (res.ok && !res.redirected) await cache.put(u, res);
        } catch { break; }
      }
    })());
  }
  if (d.type === 'forget') e.waitUntil(caches.keys().then(ks => Promise.all(ks.map(k => caches.delete(k)))));
});
