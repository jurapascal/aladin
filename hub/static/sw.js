/* Service worker — schválně skoro prázdný.
 *
 * Je tu proto, že bez něj Android nenabídne instalaci na plochu. Cachovat ale
 * nemá co: hub je okno do terminálu na počítači, takže offline stejně k ničemu
 * není, a nakešované hub.js by po aktualizaci hubu tiše obsluhovalo nový server
 * starým kódem. Navíc dělá jen dvě věci: slušnou hlášku, když se na server
 * nedá dosáhnout — místo prohlížečového „nelze načíst stránku" — a příjem
 * souborů nasdílených z jiných appek v telefonu (viz níž).
 */
'use strict';

const OFFLINE = `<!doctype html><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>Aladin</title>
<style>body{font:16px/1.6 system-ui,sans-serif;margin:0;min-height:100vh;
display:grid;place-items:center;background:#0d1117;color:#e8e3d3;padding:24px}
div{max-width:24rem;text-align:center}b{color:#e0843c}small{color:#8b949e}
button{margin-top:18px;padding:10px 18px;border-radius:9px;border:1px solid #3a3f47;
background:#1b2129;color:#e8e3d3;font:inherit;cursor:pointer}
code{font-size:12px;background:#1b2129;padding:6px 8px;border-radius:6px;display:block;margin-top:6px;word-break:break-all;user-select:all}</style>
<div><p><b id=n>Hub není k zastižení.</b></p>
<p id=t>Zkontroluj připojení k internetu. Když se připojuješ k počítači, musí na
něm běžet Aladin a telefon musí být v Tailscale.</p>
<p><small id=s>Zkouším to znovu samo…</small></p>
<button onclick="zkus(true)">Zkusit znovu</button></div>
<script>
/* Na počítači (okno appky na 127.0.0.1): Aladin se po aktualizaci restartuje nebo
   neběží — stránka to zkouší sama a jakmile odpoví, načte se. Dřív tu bylo jen
   tlačítko, které načetlo tutéž mrtvou adresu a „nic nedělalo“. */
var pc = ['127.0.0.1', 'localhost', '[::1]'].indexOf(location.hostname) >= 0;
if (pc) {
  document.getElementById('n').textContent = 'Aladin na tomhle počítači teď neodpovídá.';
  document.getElementById('t').innerHTML = 'Nejspíš se restartuje po aktualizaci — chvilku počkej, okno se připojí samo. ' +
    'Když se nic neděje: zavři okno, u hodin klikni pravým na ikonu Aladina → Ukončit appku a otevři Aladina znovu ze zástupce. ' +
    'A když ani to nepomůže, přeinstaluj ho (PowerShell):' +
    '<code>irm https://raw.githubusercontent.com/jurapascal/aladin/main/get.ps1 | iex</code>';
}
var n = 0;
function zkus(rucne) {
  var s = document.getElementById('s');
  if (rucne) s.textContent = 'Zkouším…';
  fetch(location.href, {cache: 'no-store'}).then(function (r) {
    if (r.status !== 503) location.reload();
    else throw 0;
  }).catch(function () {
    n++;
    s.textContent = 'Pořád neodpovídá — zkouším znovu každé 3 s (' + n + '×).';
  });
}
setInterval(zkus, 3000);
</script>`;

/* Sdílení do Aladina (share_target v manifest.webmanifest). Android pošle
 * soubory a text POSTem na /sdilet. Na server je posílat nejde: hub chce
 * token (u telefonu v cookie, kterou cizí POST nemusí dostat) a člověk ještě
 * neřekl, do kterého chatu to patří. Uloží se proto tady v telefonu do
 * IndexedDB a appka se otevře s ?sdileno=<id> — výběr chatu a nahrání na hub
 * pak obstará stránka (sdileni.js) s běžným přihlášením. Adresa akce ani
 * přesměrování nenesou nic tajného. */
const SDILENI = '/sdilet';
const SDILENI_DB = 'aladin-sdileni';     // stejné jméno v sdileni.js
const SDILENI_STARI = 24 * 3600 * 1000;  // nevybrané sdílení se po dni zahodí

function sdileniDb() {
  return new Promise((ok, chyba) => {
    const r = indexedDB.open(SDILENI_DB, 1);
    r.onupgradeneeded = () => r.result.createObjectStore('sdileni', {keyPath: 'id'});
    r.onsuccess = () => ok(r.result);
    r.onerror = () => chyba(r.error);
  });
}

function sdileniId() {
  if (self.crypto && crypto.randomUUID) return crypto.randomUUID();
  return Date.now().toString(36) + Math.random().toString(36).slice(2);
}

async function prijmiSdileni(req) {
  let id = 'chyba';
  try {
    const form = await req.formData();
    const pole = (k) => String(form.get(k) || '').slice(0, 100000);
    const zaznam = {
      id: sdileniId(), at: Date.now(),
      nazev: pole('nazev'), text: pole('text'), odkaz: pole('odkaz'),
      soubory: form.getAll('soubory').filter((f) => f && typeof f === 'object' && f.size > 0),
    };
    const db = await sdileniDb();
    await new Promise((ok, chyba) => {
      const tx = db.transaction('sdileni', 'readwrite');
      const st = tx.objectStore('sdileni');
      // Úklid: sdílení, ke kterému se nikdo nevrátil (appka zavřená uprostřed výběru).
      st.openCursor().onsuccess = (ev) => {
        const kurzor = ev.target.result;
        if (!kurzor) return;
        if (Date.now() - (kurzor.value.at || 0) > SDILENI_STARI) kurzor.delete();
        kurzor.continue();
      };
      st.put(zaznam);
      tx.oncomplete = () => ok();
      tx.onerror = () => chyba(tx.error);
      tx.onabort = () => chyba(tx.error);
    });
    db.close();
    id = zaznam.id;
  } catch (_) { /* id zůstane „chyba" — stránka řekne, že se to nepovedlo */ }
  return Response.redirect(new URL('/?sdileno=' + encodeURIComponent(id), self.location.origin).href, 303);
}

self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', (ev) => ev.waitUntil(self.clients.claim()));

self.addEventListener('fetch', (ev) => {
  const req = ev.request;
  const url = new URL(req.url);
  if (req.method === 'POST' && url.origin === self.location.origin && url.pathname === SDILENI) {
    ev.respondWith(prijmiSdileni(req));
    return;
  }
  if (req.mode !== 'navigate') return;   // zbytek jde rovnou na síť
  ev.respondWith(fetch(req).catch(() => new Response(
    OFFLINE, {status: 503, headers: {'Content-Type': 'text/html; charset=utf-8'}})));
});
