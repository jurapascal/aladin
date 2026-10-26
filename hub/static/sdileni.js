/* Sdílení do Aladina z jiných appek v telefonu (Web Share Target).
 *
 * Nainstalovaný Aladin se na Androidu objeví v nabídce Sdílet (share_target
 * v manifest.webmanifest). Prohlížeč pošle soubory a text na /sdilet, ještě
 * v telefonu je chytí service worker (sw.js), uloží do IndexedDB a otevře
 * appku s ?sdileno=<id>. Tady si člověk vybere, do kterého chatu to patří:
 * soubory se nahrají na hub stejně jako příloha v bublině (hub.js uploadFiles)
 * a spolu s textem čekají v bublině vybraného chatu. Nic se neodesílá samo —
 * člověk k tomu většinou chce dopsat, co s tím má Claude udělat.
 *
 * iPhone (Safari) sdílení do webových appek neumí, tam tahle cesta nevede.
 * Obal pro Android (mobile/android) může sdílení předat přes HubSdileni.otevri.
 */
(function (global) {
  'use strict';

  const DB = 'aladin-sdileni';       // stejné jméno jako v sw.js
  const MAX = 25 * 1024 * 1024;      // core.MAX_UPLOAD — větší soubor server odmítne
  const STRANKA = 30;                // tolik chatů bez hledání, další na tlačítko
  let io = null;
  let otevreno = null;               // právě ukázaný výběr (jen jeden naráz)

  function el(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }
  const ikona = (name) => `<svg class="ico"><use href="#${name}"/></svg>`;

  /* ── úložiště v telefonu (zapisuje sw.js) ─────────────────────────────── */
  function db() {
    return new Promise((ok, chyba) => {
      const r = indexedDB.open(DB, 1);
      r.onupgradeneeded = () => r.result.createObjectStore('sdileni', {keyPath: 'id'});
      r.onsuccess = () => ok(r.result);
      r.onerror = () => chyba(r.error);
    });
  }
  async function zDb(mode, fn) {
    const d = await db();
    try {
      return await new Promise((ok, chyba) => {
        const tx = d.transaction('sdileni', mode);
        const req = fn(tx.objectStore('sdileni'));
        tx.oncomplete = () => ok(req && req.result);
        tx.onerror = () => chyba(tx.error);
        tx.onabort = () => chyba(tx.error);
      });
    } finally {
      d.close();
    }
  }
  const nacti = (id) => zDb('readonly', (st) => st.get(id)).catch(() => null);
  const smaz = (id) => (id ? zDb('readwrite', (st) => st.delete(id)).catch(() => {}) : Promise.resolve());

  /* Parametr pryč z adresy, ať reload výběr neotevře znovu. */
  function uklidAdresu() {
    const p = new URLSearchParams(location.search);
    if (!p.has('sdileno')) return;
    p.delete('sdileno');
    const q = p.toString();
    history.replaceState(history.state, '', location.pathname + (q ? '?' + q : '') + location.hash);
  }

  /* ── co přišlo ────────────────────────────────────────────────────────── */
  // Některé appky posílají soubor bez přípony (jen „1000012345" a typ).
  // Server se podle přípony rozhoduje, kam soubor dát, a Claude podle ní
  // pozná, co čte — doplní se tedy z typu.
  const PRIPONY = {
    'image/jpeg': 'jpg', 'image/png': 'png', 'image/webp': 'webp', 'image/gif': 'gif',
    'image/heic': 'heic', 'image/heif': 'heif', 'application/pdf': 'pdf', 'text/plain': 'txt',
    'text/markdown': 'md', 'text/csv': 'csv', 'text/html': 'html', 'application/json': 'json',
    'audio/mpeg': 'mp3', 'audio/mp4': 'm4a', 'audio/ogg': 'ogg', 'audio/wav': 'wav',
    'video/mp4': 'mp4', 'video/quicktime': 'mov', 'video/webm': 'webm', 'application/zip': 'zip',
    'application/msword': 'doc', 'application/vnd.ms-excel': 'xls', 'application/vnd.ms-powerpoint': 'ppt',
    'application/vnd.openxmlformats-officedocument.wordprocessingml.document': 'docx',
    'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet': 'xlsx',
    'application/vnd.openxmlformats-officedocument.presentationml.presentation': 'pptx',
    'application/vnd.oasis.opendocument.text': 'odt', 'application/vnd.oasis.opendocument.spreadsheet': 'ods',
  };
  function sJmenem(f) {
    const jmeno = String(f.name || '').trim().split(/[\\/]/).pop();
    if (/\.[A-Za-z0-9]{1,12}$/.test(jmeno)) return jmeno === f.name ? f : new File([f], jmeno, {type: f.type});
    const typ = String(f.type || '').toLowerCase().split(';')[0].trim();
    const ext = PRIPONY[typ] || (/^[a-z]+\/([a-z0-9]{1,10})$/.exec(typ) || [])[1] || 'bin';
    const zaklad = jmeno || (typ.startsWith('image/') ? 'obrazek' : 'soubor');
    return new File([f], zaklad + '.' + ext, {type: f.type});
  }

  /* Soubor od obalu appky ({name, type, data v base64}) → File. */
  function zBase64(s) {
    if (s instanceof Blob) return s;
    const bin = atob(String(s.data || ''));
    const bytes = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
    return new File([bytes], String(s.name || ''), {type: String(s.type || '')});
  }

  /* Text do bubliny: appky posílají název, text a odkaz různě (Chrome dá
     název stránky a odkaz v textu, jiné odkaz zvlášť) — bez opakování. */
  function textSdileni(z) {
    const text = String(z.text || '').trim();
    const odkaz = String(z.odkaz || '').trim();
    const nazev = String(z.nazev || '').trim();
    const jmena = (z.soubory || []).map((f) => f.name);
    const casti = [];
    if (nazev && nazev !== odkaz && !text.includes(nazev) && !jmena.includes(nazev)) casti.push(nazev);
    if (text) casti.push(text);
    if (odkaz && !text.includes(odkaz)) casti.push(odkaz);
    return casti.join('\n');
  }

  function velikost(n) {
    if (n < 1024) return n + ' B';
    if (n < 1024 * 1024) return Math.round(n / 1024) + ' kB';
    return (n / 1024 / 1024).toFixed(1).replace('.', ',') + ' MB';
  }
  const pocet = (n, a, b, c) => n + ' ' + (n === 1 ? a : n < 5 ? b : c);

  /* ── výběr chatu ──────────────────────────────────────────────────────── */
  function ukaz(z) {
    if (otevreno) otevreno.zavri();
    z.soubory = (z.soubory || []).map(sJmenem);
    const text = textSdileni(z);
    const urls = [];

    const wrap = el('div', 'sd-wrap');
    wrap.setAttribute('role', 'dialog');
    wrap.setAttribute('aria-modal', 'true');
    wrap.setAttribute('aria-label', 'Kam to poslat?');
    const box = el('div', 'sd-box');
    const hlava = el('div', 'sd-hlava');
    hlava.appendChild(el('div', 'sd-nadpis', 'Kam to poslat?'));
    const x = el('button', 'sd-x');
    x.type = 'button';
    x.title = 'Zrušit sdílení';
    x.innerHTML = ikona('i-close');
    hlava.appendChild(x);
    box.appendChild(hlava);

    // Co se posílá: soubory (obrázek s náhledem) a začátek textu.
    const co = el('div', 'sd-co');
    for (const f of z.soubory) {
      const chip = el('div', 'sd-chip' + (f.size > MAX ? ' velky' : ''));
      if (/^image\/(png|jpe?g|gif|webp|bmp|avif)$/i.test(f.type || '') && f.size <= MAX) {
        const img = el('img', 'sd-nahled');
        const u = URL.createObjectURL(f);
        urls.push(u);
        img.src = u;
        img.alt = '';
        chip.appendChild(img);
      } else {
        chip.insertAdjacentHTML('beforeend', ikona('i-soubor'));
      }
      const popis = el('span', 'sd-chip-text');
      popis.appendChild(el('span', 'sd-chip-jmeno', f.name));
      popis.appendChild(el('span', 'sd-chip-vel',
        f.size > MAX ? `${velikost(f.size)} · víc než ${MAX / 1024 / 1024} MB, nepřiloží se` : velikost(f.size)));
      chip.appendChild(popis);
      co.appendChild(chip);
    }
    if (text) co.appendChild(el('div', 'sd-text', text.length > 220 ? text.slice(0, 220) + '…' : text));
    box.appendChild(co);

    const hledat = el('input', 'sd-hledat');
    hledat.type = 'search';
    hledat.placeholder = 'Hledat chat…';
    hledat.setAttribute('aria-label', 'Hledat chat');
    hledat.spellcheck = false;
    box.appendChild(hledat);

    const seznam = el('div', 'chats sd-seznam');
    box.appendChild(seznam);
    const stav = el('div', 'sd-stav');
    stav.hidden = true;
    box.appendChild(stav);
    wrap.appendChild(box);

    let chats = (io.chats() || []).slice();
    let ukazano = STRANKA;
    let prace = false;

    const radek = (opts, onclick) => {
      const r = io.chatRadek(Object.assign({bezHvezdy: true}, opts));
      r.classList.add('sd-radek');
      r.onclick = () => { if (!prace) onclick(); };
      return r;
    };
    const sekce = (nazev) => seznam.appendChild(el('div', 'sd-sekce', nazev));

    function vykresli() {
      const slova = io.fold(hledat.value.trim()).split(/\s+/).filter(Boolean);
      const sedi = (...co) => {
        const hay = io.fold(co.join(' '));
        return slova.every((w) => hay.includes(w));
      };
      seznam.textContent = '';
      if (!slova.length) {
        const novy = radek({druh: 'osobni', titulek: 'Nový chat', nahled: 'Začne nový rozhovor s Claudem'},
                           () => posli({novy: true}, 'Nový chat'));
        novy.classList.add('sd-novy');
        novy.querySelector('.chat-av use').setAttribute('href', '#i-plus');
        seznam.appendChild(novy);
      }
      // Otevřené taby (i ty, které v seznamu chatů ještě nejsou).
      const taby = io.tabs();
      const tabChat = (t) => chats.find((c) => io.patri(t, c));
      const otevrene = taby.filter((t) => {
        const c = tabChat(t);
        return sedi(t.title, t.path, c ? c.title : '', c ? c.prompt : '');
      });
      if (otevrene.length) {
        sekce('Otevřené');
        for (const t of otevrene) {
          const c = tabChat(t);
          const titulek = (c && c.title) || t.title || 'Chat';
          seznam.appendChild(radek({
            druh: c ? io.druhChatu(c) : (t.vault ? 'firma' : 'osobni'), titulek,
            cas: c ? c.updated : 0, nahled: c ? io.chatNahled(c) : 'Otevřený chat',
            stav: t.ceka ? 'Čeká na tebe' : t.pracuje ? 'Claude píše…' : '',
          }, () => posli({tab: t}, titulek)));
        }
      }
      // Nedávné chaty bez těch, které už jsou otevřené výš.
      const nedavne = chats
        .filter((c) => c.exists && !taby.some((t) => io.patri(t, c)))
        .filter((c) => !slova.length || sedi(c.title, c.prompt, c.project, c.posledni))
        .sort((a, b) => b.updated - a.updated);
      if (nedavne.length) {
        sekce(slova.length ? 'Nalezené chaty' : 'Nedávné chaty');
        const limit = slova.length ? 100 : ukazano;
        for (const c of nedavne.slice(0, limit)) {
          seznam.appendChild(radek({druh: io.druhChatu(c), titulek: c.title, cas: c.updated,
                                    nahled: io.chatNahled(c)}, () => posli({chat: c}, c.title)));
        }
        if (nedavne.length > limit && !slova.length) {
          const vic = el('button', 'chats-more sd-vic', `Zobrazit starší (${nedavne.length - limit})`);
          vic.type = 'button';
          vic.onclick = () => { ukazano += STRANKA; vykresli(); };
          seznam.appendChild(vic);
        }
      }
      if (slova.length && !otevrene.length && !nedavne.length) {
        seznam.appendChild(el('div', 'empty', 'Nic takového jsem nenašel.'));
      }
    }

    function hlaska(text, chyba) {
      stav.hidden = !text;
      stav.textContent = text || '';
      stav.classList.toggle('chyba', !!chyba);
    }
    function pracuje(text) {
      prace = true;
      box.classList.add('pracuje');
      hledat.disabled = true;
      hlaska(text);
    }
    function znovu(text) {
      prace = false;
      box.classList.remove('pracuje');
      hledat.disabled = false;
      hlaska(text, true);
      vykresli();
    }

    function zavri() {
      if (otevreno !== ovladani) return;
      otevreno = null;
      document.removeEventListener('keydown', klavesy, true);
      wrap.remove();
      for (const u of urls) URL.revokeObjectURL(u);
    }
    async function zrus() {
      if (prace) return;
      zavri();
      await smaz(z.id);
      uklidAdresu();
    }
    const klavesy = (ev) => {
      if (ev.key === 'Escape') { ev.preventDefault(); ev.stopPropagation(); zrus(); }
    };

    /* Vybráno: nahrát soubory, otevřít chat, vložit do bubliny. Nahrané
       cesty se pamatují — když se chat neotevře, další volba už nenahrává. */
    async function posli(cil, nazev) {
      if (prace) return;
      pracuje('Připojuju se k hubu…');
      // Hub musí být připojený a znát otevřené taby — jinak by se chat,
      // který už běží, otevřel podruhé (dva Claudové v jedné konverzaci).
      if (!(await pockej(io.pripraveno, 15000))) return znovu('Hub se zatím nepřipojil. Zkus to za chvíli znovu.');
      if (!z.cesty) {
        const male = z.soubory.filter((f) => f.size <= MAX);
        const cesty = [];
        for (let i = 0; i < male.length; i++) {
          pracuje(male.length > 1 ? `Nahrávám ${i + 1}. soubor z ${male.length}…` : 'Nahrávám soubor…');
          cesty.push(...await io.upload([male[i]]));
        }
        if (!cesty.length && !text) {
          return znovu(male.length ? 'Soubory se nepodařilo nahrát. Zkus to znovu.'
                                   : `Není co vložit — soubory jsou větší než ${MAX / 1024 / 1024} MB.`);
        }
        z.cesty = cesty;
      }
      pracuje('Otevírám chat…');
      let tab = null;
      try {
        tab = cil.novy ? io.novy() : cil.tab ? io.tab(cil.tab) : await io.chat(cil.chat);
      } catch (_) { tab = null; }
      if (!tab || !tab.composer || !tab.composer.pridej) return znovu('Chat se neotevřel — vyber jiný.');
      tab.composer.pridej(text, z.cesty);
      zavri();
      await smaz(z.id);
      uklidAdresu();
      const n = z.cesty.length;
      const coJe = n ? pocet(n, 'soubor', 'soubory', 'souborů') + (text ? ' a text' : '') : 'Text';
      io.toast(`${coJe} v chatu „${nazev}“ — dopiš, co s tím má Claude udělat, a odešli.`);
    }

    hledat.addEventListener('input', () => { ukazano = STRANKA; vykresli(); });
    x.onclick = zrus;
    document.addEventListener('keydown', klavesy, true);
    const ovladani = {zavri};
    otevreno = ovladani;
    document.body.appendChild(wrap);
    vykresli();
    // Čerstvý seznam chatů — stránka se právě načetla, panel ho nemusí mít.
    io.api('chats').then((res) => {
      if (otevreno !== ovladani || !res || !Array.isArray(res.chats)) return;
      chats = res.chats;
      vykresli();
    }).catch(() => { /* zůstane, co je */ });
    // Otevřené taby hlásí server po připojení — seznam se jednou dokreslí.
    pockej(io.pripraveno, 15000).then(() => { if (otevreno === ovladani && !prace) vykresli(); });
  }

  async function pockej(podminka, ms) {
    const konec = Date.now() + ms;
    while (!podminka()) {
      if (Date.now() > konec) return false;
      await new Promise((r) => setTimeout(r, 150));
    }
    return true;
  }

  async function start(spojeni) {
    io = spojeni;
    const id = new URLSearchParams(location.search).get('sdileno');
    if (!id) return;
    const z = id === 'chyba' ? null : await nacti(id);
    if (!z || (!(z.soubory || []).length && !textSdileni(z))) {
      uklidAdresu();
      if (z) await smaz(z.id);
      io.toast(id === 'chyba' || !z
        ? 'Nasdílené soubory se do appky nedostaly. Otevři Aladina, chvíli počkej a zkus sdílení znovu.'
        : 'Sdílení bylo prázdné — nic nepřišlo.');
      return;
    }
    ukaz(z);
  }

  global.HubSdileni = {
    start,
    /* Pro obal appky (mobile/android): sdílení předané rovnou, bez service
       workeru. `soubory` = File/Blob, nebo {name, type, data v base64}. */
    otevri(z) {
      if (!io) return false;
      const soubory = ((z && z.soubory) || []).map(zBase64);
      ukaz({id: '', nazev: (z && z.nazev) || '', text: (z && z.text) || '', odkaz: (z && z.odkaz) || '', soubory});
      return true;
    },
  };
})(window);
