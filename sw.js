/* 阿星梦想空间 Service Worker：让网页可安装、可离线打开 */
const C = "dream-v1";
self.addEventListener("install", e => {
  e.waitUntil(caches.open(C).then(c => c.addAll(["./index.html"])));
  self.skipWaiting();
});
self.addEventListener("activate", e => e.waitUntil(self.clients.claim()));
self.addEventListener("fetch", e => {
  e.respondWith(
    caches.match(e.request).then(r => r || fetch(e.request).then(res => {
      const cp = res.clone();
      caches.open(C).then(c => c.put(e.request, cp));
      return res;
    }).catch(() => caches.match("./index.html")))
  );
});
