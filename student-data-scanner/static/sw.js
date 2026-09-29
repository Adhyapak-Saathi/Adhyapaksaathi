const C="scanner-v5";
const ASSETS=[
  "/static/style.css?v=20260929-5",
  "/static/app.js?v=20260929-5"
];

self.addEventListener("install",event=>{
  self.skipWaiting();
  event.waitUntil(caches.open(C).then(cache=>cache.addAll(ASSETS)));
});

self.addEventListener("activate",event=>{
  event.waitUntil((async()=>{
    const keys=await caches.keys();
    await Promise.all(keys.filter(k=>k!==C).map(k=>caches.delete(k)));
    await self.clients.claim();
  })());
});

self.addEventListener("fetch",event=>{
  if(event.request.method!=="GET") return;
  if(event.request.mode==="navigate" || event.request.url.includes("/api/")){
    event.respondWith(fetch(event.request));
    return;
  }
  event.respondWith(
    fetch(event.request)
      .then(response=>{
        const copy=response.clone();
        caches.open(C).then(cache=>cache.put(event.request,copy)).catch(()=>{});
        return response;
      })
      .catch(()=>caches.match(event.request))
  );
});
