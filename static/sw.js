// Service worker do Painel da Câmara: permite instalar o app no computador e no celular.
// Não guarda páginas em cache (os dados exigem login e mudam sempre); só mostra um aviso sem internet.
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (e) => e.waitUntil(self.clients.claim()));
self.addEventListener("fetch", (e) => {
  if (e.request.mode !== "navigate") return;
  e.respondWith(fetch(e.request).catch(() => new Response(
    "<!doctype html><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>" +
    "<title>Sem conexão</title><body style='font-family:system-ui;padding:2rem;color:#0b5d3b'>" +
    "<h2>Sem conexão</h2><p>O Painel da Câmara precisa de internet. Verifique a conexão e tente de novo.</p>",
    {headers: {"Content-Type": "text/html; charset=utf-8"}})));
});
