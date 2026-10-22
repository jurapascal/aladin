/* Service worker — schválně skoro prázdný.
 *
 * Je tu proto, že bez něj Android nenabídne instalaci na plochu. Cachovat ale
 * nemá co: hub je okno do terminálu na počítači, takže offline stejně k ničemu
 * není, a nakešované hub.js by po aktualizaci hubu tiše obsluhovalo nový server
 * starým kódem. Jediné, co dělá navíc, je slušná hláška, když se na server
 * nedá dosáhnout — místo prohlížečového „nelze načíst stránku".
 */
'use strict';

const OFFLINE = `<!doctype html><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>Aladin</title>
<style>body{font:16px/1.6 system-ui,sans-serif;margin:0;min-height:100vh;
display:grid;place-items:center;background:#0d1117;color:#e8e3d3;padding:24px}
div{max-width:22rem;text-align:center}b{color:#e0843c}
button{margin-top:18px;padding:10px 18px;border-radius:9px;border:1px solid #3a3f47;
background:#1b2129;color:#e8e3d3;font:inherit}</style>
<div><p><b>Hub není k zastižení.</b></p>
<p>Zkontroluj připojení k internetu. Když se připojuješ k počítači, musí na
něm běžet Aladin a telefon musí být v Tailscale.</p>
<button onclick="location.reload()">Zkusit znovu</button></div>`;

self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', (ev) => ev.waitUntil(self.clients.claim()));

self.addEventListener('fetch', (ev) => {
  if (ev.request.mode !== 'navigate') return;   // zbytek jde rovnou na síť
  ev.respondWith(fetch(ev.request).catch(() => new Response(
    OFFLINE, {status: 503, headers: {'Content-Type': 'text/html; charset=utf-8'}})));
});
