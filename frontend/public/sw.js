/**
 * Service worker.
 *
 * Written by hand rather than generated, because the caching policy here is a
 * clinical-safety decision rather than a performance one:
 *
 *  - The app shell is cached so RTC opens with no signal at all.
 *  - GET API responses are network-first with a cache fallback, so a user offline
 *    sees the last known data rather than an error — but never sees stale data in
 *    preference to fresh data.
 *  - Mutating requests are NEVER cached or replayed here. Queuing and replay belong
 *    to the application's outbox, which knows about client UUIDs and conflict
 *    detection; a service worker replaying a POST blindly could duplicate a
 *    clinical record.
 */

const VERSION = 'rtc-v1'
const SHELL_CACHE = `${VERSION}-shell`
const DATA_CACHE = `${VERSION}-data`

const SHELL_ASSETS = ['/', '/index.html', '/manifest.webmanifest', '/icon.svg']

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches
      .open(SHELL_CACHE)
      .then((cache) => cache.addAll(SHELL_ASSETS))
      .then(() => self.skipWaiting())
      .catch(() => self.skipWaiting()),
  )
})

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) =>
        Promise.all(
          keys
            .filter((key) => !key.startsWith(VERSION))
            .map((key) => caches.delete(key)),
        ),
      )
      .then(() => self.clients.claim()),
  )
})

function isApiRequest(url) {
  return url.pathname.startsWith('/api/')
}

function isStaticAsset(url) {
  return (
    url.pathname.startsWith('/assets/') ||
    /\.(?:js|css|woff2?|png|svg|jpg|jpeg|webp|ico)$/.test(url.pathname)
  )
}

self.addEventListener('fetch', (event) => {
  const { request } = event
  const url = new URL(request.url)

  // Only same-origin traffic is our concern.
  if (url.origin !== self.location.origin) return

  // Never intervene in writes. The outbox owns offline write semantics.
  if (request.method !== 'GET') return

  // Authentication must always hit the network — a cached session response would
  // be both wrong and a security problem.
  if (url.pathname.includes('/auth/')) return

  // ---- API: network first, fall back to the last good response --------
  if (isApiRequest(url)) {
    event.respondWith(
      fetch(request)
        .then((response) => {
          if (response.ok) {
            const copy = response.clone()
            caches.open(DATA_CACHE).then((cache) => cache.put(request, copy))
          }
          return response
        })
        .catch(async () => {
          const cached = await caches.match(request)
          if (cached) {
            // Mark it so the client can show "showing offline data".
            const headers = new Headers(cached.headers)
            headers.set('X-RTC-From-Cache', 'true')
            return new Response(cached.body, {
              status: cached.status,
              statusText: cached.statusText,
              headers,
            })
          }
          return new Response(
            JSON.stringify({
              detail: 'You are offline and this data is not available on this device.',
              code: 'offline_no_cache',
            }),
            { status: 503, headers: { 'Content-Type': 'application/json' } },
          )
        }),
    )
    return
  }

  // ---- Static assets: cache first (they are content-hashed) -----------
  if (isStaticAsset(url)) {
    event.respondWith(
      caches.match(request).then(
        (cached) =>
          cached ??
          fetch(request).then((response) => {
            if (response.ok) {
              const copy = response.clone()
              caches.open(SHELL_CACHE).then((cache) => cache.put(request, copy))
            }
            return response
          }),
      ),
    )
    return
  }

  // ---- Navigation: serve the shell so client routing works offline ----
  if (request.mode === 'navigate') {
    event.respondWith(
      fetch(request).catch(() => caches.match('/index.html').then((cached) => cached ?? Response.error())),
    )
  }
})

// The app asks the worker to step aside after an update so a reload picks up the
// new build rather than serving yesterday's bundle indefinitely.
self.addEventListener('message', (event) => {
  if (event.data === 'SKIP_WAITING') self.skipWaiting()
})
