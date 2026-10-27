/* Prohlížeč v appce — plovoucí okno s prohlížečem, který používá Claude.
 *
 * Chová se jako skutečný prohlížeč: karty se zavíráním a ikonami, adresní
 * řádek se zámkem, zpět / vpřed / načíst znovu / zastavit, klávesové zkratky
 * (Ctrl+L, T, W, R, Alt+šipky, Ctrl+Tab, Ctrl+C), dialogy stránky (alert,
 * confirm, prompt) a stránka, která vidí přesně tu velikost, kterou vidíš ty.
 * Claude v něm pracuje přes Playwright a ty mu můžeš do toho sáhnout: kliknout,
 * přihlásit se, vyřešit captchu.
 *
 * Rozložení jako Chrome: nahoře pruh karet (za jeho prázdné místo se okno
 * přesouvá, vpravo poměr stran, minimalizace, maximalizace, zavření), pod ním
 * adresní řádek a pod ním stránka — nic se přes ni nepřekrývá. Plocha stránky
 * má zvolený poměr stran (16:9, 4:3, 9:16 = stránka jako na telefonu, nebo
 * volně) a prohlížeč na serveru dostane přesně tuhle velikost.
 *
 * Okno jde přesunout, změnit mu velikost za pravý dolní roh,
 * minimalizovat do lišty „Prohlížeč“ a zavřít. Když Claude prohlížeč použije,
 * okno zůstane jen jako lišta; samo se otevře, až je potřeba člověk —
 * přihlášení, kód z SMS/e-mailu, captcha — a po vyřízení se zase složí.
 *
 * Každý chat má své karty (hub/prohlizec_proxy.py), profil je společný.
 * Tab chatu, který má v prohlížeči karty, dostane ikonku; klik na ni
 * ukáže prohlížeč s kartami toho chatu. V záhlaví okna se mezi chaty přepíná.
 *
 * Obraz a vstup jdou přes websocket hubu (hub/prohlizec.py): server posílá
 * snímky stránky binárně (JPEG), tady se dekódují mimo hlavní vlákno
 * (createImageBitmap), kreslí na plátno a každý vykreslený snímek se potvrdí
 * — server pak pošle další. Nic se nehromadí: snímek, který nestihl přijít
 * na řadu, se zahodí, a schovaný panel nepotvrzuje, takže obraz stojí.
 * Zpátky jde myš, kolečko (sloučené po snímcích obrazovky) a klávesy.
 */
'use strict';

(function (global) {
  const KEY = 'hub-prohlizec';
  const KEY_POMER = 'hub-prohlizec-pomer';   // poměr stran — na každém zařízení svůj
  const MIN_W = 260, MIN_H = 200;
  const POMERY = {'16:9': 16 / 9, '4:3': 4 / 3, '9:16': 9 / 16};   // + 'volne'
  const UZKE = '(max-width: 820px)';
  const POP_PAUZA = 25000;          // po jak dlouhé pauze mezi nástroji vyskočí znovu

  let send = () => {};
  let copyText = null;
  let root = null, canvas = null, ctx = null, urlInput = null, tabsBox = null, stavEl = null;
  let kb = null, scena = null, zamek = null, dialogEl = null, pomerEl = null, tlMax = null;
  let tlZpet = null, tlVpred = null, tlZnovu = null;
  let pill = null;
  let frame = {w: 1280, h: 800};
  let stav = 'zavreno';             // zavreno | okno | mini
  let geo = null;
  let otevrene = false;              // poslali jsme serveru {a:'open'}
  let posledniNastroj = 0;
  let info = {pages: [], active: '', url: '', back: false, fwd: false, loading: false};
  let sizeTimer = null, odeslanaVelikost = '';
  let pomer = '16:9';
  let odeslaneVidi = '';              // co jsme serveru naposledy řekli o viditelnosti
  const taby = new Map();            // id karty → její prvek
  let tabTitle = () => '';           // sid tabu hubu → jeho název (hub.js)
  let vyber = null;                  // sid chatu, jehož karty okno ukazuje (null = všechny)
  let chatyEl = null, pomocEl = null;
  let samo = false;                  // okno otevřela potřeba člověka (po vyřízení se zase složí)
  const odbyto = new Set();          // „id:důvod“ — člověk okno při té potřebě sám zavřel
  let posledniTaby = '';
  let mapaTabu = {};                 // sid tabu → {karet, potreba} (co hlásí server)
  let zavrelJsi = false;             // okno jsi zavřel sám — lišta se sama nevrací, dokud Claude prohlížeč znovu nepoužije
  let aktivniTab = null;             // tab, na kterém zrovna stojíš
  const posledniKarta = {};          // sid chatu → karta, na které jsi v něm naposledy byl
  /* Id tabu hubu je číslo, vlastník karty ze serveru text ("4") — porovnává
     se přes `===`, takže všechno jako text, jinak se karty chatu nikdy
     nenajdou (okno pak neukáže jeho karty a přepnutí chatu nic neudělá). */
  const sidOf = (x) => (x === null || x === undefined || x === '' ? null : String(x));
  const DUVODY = {
    heslo: 'Claude potřebuje, aby ses tu přihlásil.',
    kod: 'Claude potřebuje ověřovací kód — opiš ho sem.',
    captcha: 'Tady je potřeba vyřešit ověření „nejsem robot“.',
  };

  const $ = (s, r) => (r || root).querySelector(s);
  function el(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined) n.textContent = text;
    return n;
  }
  const ico = (id) => `<svg class="ico"><use href="#${id}"/></svg>`;

  let io_ = {};
  function nactiGeo() {
    // Napřed to, co drží hub (přežije restart i aktualizaci — port, a tím i
    // localStorage, je pokaždé jiný), pak localStorage.
    let g = null;
    try { g = (io_.ulozene && io_.ulozene()) || null; } catch (_) { g = null; }
    if (!g || !g.w) {
      try { g = JSON.parse(localStorage.getItem(KEY) || 'null'); } catch (_) { g = null; }
    }
    const vw = window.innerWidth, vh = window.innerHeight;
    pomer = nactiPomer();
    const w = Math.min(g && g.w || vychoziSirka(pomer), vw - 16);
    const out = {w: Math.max(MIN_W, w), h: Math.max(MIN_H, Math.min(g && g.h || Math.round(vh * 0.72), vh - 16)),
                 stav: g && g.stav, cela: !!(g && g.cela)};
    prizpusob(out);
    out.x = Math.max(0, Math.min(g ? g.x : vw - out.w - 24, vw - 120));
    out.y = Math.max(0, Math.min(g ? g.y : vh - out.h - 24, vh - 60));
    return out;
  }

  /* ── poměr stran plochy stránky ────────────────────────────────────────── */
  function nactiPomer() {
    let p = null;
    try { p = localStorage.getItem(KEY_POMER); } catch (_) { p = null; }
    if (p && (POMERY[p] || p === 'volne')) return p;
    return window.matchMedia(UZKE).matches ? '9:16' : '16:9';
  }
  function vychoziSirka(p) {
    const vw = window.innerWidth;
    // 9:16 = telefon; na šířku tak, ať stránka dostane aspoň 1024 px (bez zmenšení).
    if (p === '9:16') return Math.min(400, vw - 16);
    return Math.max(MIN_W, Math.min(Math.max(1060, Math.round(vw * 0.5)), 1200, vw - 48));
  }
  /* Výška záhlaví (karty + adresní řádek + rámeček) — vše kromě stránky. */
  function ramecek() {
    if (root && !root.hidden) {
      const h = root.querySelector('.br-hlava'), l = root.querySelector('.br-lista');
      const v = (h ? h.offsetHeight : 0) + (l ? l.offsetHeight : 0);
      if (v > 0) return v + 2;
    }
    return 84;
  }
  /* Rozměr okna podle poměru stran: plocha stránky má přesně zvolený poměr;
     když se nevejde na výšku, zúží se. `podleVysky` = táhne se za výšku. */
  function prizpusob(g, podleVysky) {
    const r = POMERY[pomer];
    const vw = window.innerWidth, vh = window.innerHeight;
    if (!r) { g.w = Math.max(MIN_W, Math.min(g.w, vw - 8)); g.h = Math.max(MIN_H, Math.min(g.h, vh - 8)); return g; }
    const ram = ramecek();
    if (podleVysky) g.w = Math.round((g.h - ram) * r) + 2;
    g.w = Math.max(MIN_W, Math.min(g.w, vw - 8));
    g.h = Math.round((g.w - 2) / r) + ram;
    if (g.h > vh - 8) {
      g.h = vh - 8;
      g.w = Math.max(MIN_W, Math.round((g.h - ram) * r) + 2);
    }
    return g;
  }
  function zmenPomer(p) {
    if (!(POMERY[p] || p === 'volne')) return;
    const pred = pomer;
    pomer = p;
    try { localStorage.setItem(KEY_POMER, p); } catch (_) { /* soukromé okno */ }
    if (pomerEl && pomerEl.value !== p) pomerEl.value = p;
    if (!geo) return;
    // Otočení (na výšku ↔ na šířku) = rozumná výchozí šířka; pravý dolní roh zůstane.
    const vys = (x) => x === '9:16';
    const pravy = geo.x + geo.w, dolni = geo.y + geo.h;
    if (vys(p) !== vys(pred) && p !== 'volne') geo.w = vychoziSirka(p);
    prizpusob(geo);
    geo.x = Math.max(0, Math.min(pravy - geo.w, window.innerWidth - geo.w));
    geo.y = Math.max(0, Math.min(dolni - geo.h, window.innerHeight - geo.h));
    if (root) root.classList.remove('br-cela');
    usad();
    ulozGeo();
    odeslanaVelikost = '';
    posliVelikost();
  }
  let ulozTimer = null;
  function ulozGeo() {
    const data = {...geo, stav, cela: !!(root && root.classList.contains('br-cela'))};
    try { localStorage.setItem(KEY, JSON.stringify(data)); } catch (_) { /* soukromé okno */ }
    // Hubu se to řekne se zpožděním (tažení okna volá ulozGeo pořád dokola).
    clearTimeout(ulozTimer);
    // Telefon (úzká obrazovka) má okno dané CSS — jeho rozměr by počítači
    // přepsal tvoje okno (hub drží jedno nastavení pro všechna zařízení).
    if (window.matchMedia(UZKE).matches) return;
    ulozTimer = setTimeout(() => { if (io_.uloz) io_.uloz(data); }, 600);
  }

  /* Maximalizované okno vyplní plochu appky pod horní lištou (vedle panelu
     chatů) — horní lišta a chaty zůstanou vidět, nic se nepřekrývá. */
  function plochaMax() {
    const m = document.querySelector('.main');
    const r = m && m.getBoundingClientRect();
    if (!r || r.width < 300 || r.height < 200 || window.matchMedia(UZKE).matches) {
      return {x: 0, y: 0, w: window.innerWidth, h: window.innerHeight};
    }
    return {x: Math.round(r.left), y: Math.round(r.top), w: Math.round(r.width), h: Math.round(r.height)};
  }
  function usad() {
    if (!root) return;
    const g = root.classList.contains('br-cela') ? plochaMax() : geo;
    root.style.left = g.x + 'px';
    root.style.top = g.y + 'px';
    root.style.width = g.w + 'px';
    root.style.height = g.h + 'px';
    if (tlMax) tlMax.title = root.classList.contains('br-cela') ? 'Obnovit velikost' : 'Maximalizovat';
  }
  function maximalizuj(on) {
    root.classList.toggle('br-cela', on === undefined ? !root.classList.contains('br-cela') : !!on);
    usad();
    ulozGeo();
  }

  function sestav() {
    root = el('div', 'br-okno');
    root.innerHTML =
      '<div class="br-hlava">' +
        '<div class="br-taby"></div>' +
        '<span class="br-chaty"></span>' +
        '<select class="br-pomer" title="Poměr stran stránky">' +
          '<option value="16:9">16:9</option><option value="4:3">4:3</option>' +
          '<option value="9:16">9:16 mobil</option><option value="volne">Volně</option></select>' +
        '<button class="br-btn br-mini" title="Minimalizovat">' + ico('i-minus') + '</button>' +
        '<button class="br-btn br-max" title="Maximalizovat"><svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="5" y="5" width="14" height="14" rx="2"/></svg></button>' +
        '<button class="br-btn br-zavri" title="Zavřít okno">' + ico('i-close') + '</button>' +
      '</div>' +
      '<div class="br-lista">' +
        '<button class="br-btn br-zpet br-otoc" title="Zpět (Alt+←)">' + ico('i-chevron') + '</button>' +
        '<button class="br-btn br-vpred" title="Vpřed (Alt+→)">' + ico('i-chevron') + '</button>' +
        '<button class="br-btn br-znovu" title="Načíst znovu (Ctrl+R)">' + ico('i-refresh') + '</button>' +
        '<div class="br-urlbox"><span class="br-zamek"></span>' +
          '<input class="br-url" type="text" spellcheck="false" autocomplete="off" placeholder="Adresa nebo hledání"></div>' +
      '</div>' +
      // Pruh „Claude potřebuje pomoc“ je mezi lištou a stránkou (jako lišta
      // s oznámením v Chromu) — stránku posune, nepřekryje.
      '<div class="br-pomoc" hidden><span></span><button class="br-pomoc-ok">Hotovo</button></div>' +
      '<div class="br-scena"><canvas class="br-platno"></canvas>' +
        '<textarea class="br-kb" autocapitalize="off" autocomplete="off" autocorrect="off" spellcheck="false" tabindex="0"></textarea>' +
        '<div class="br-stav"></div><div class="br-dialog" hidden></div></div>' +
      '<div class="br-roh" title="Změnit velikost"></div>';
    document.body.appendChild(root);
    canvas = $('.br-platno');
    ctx = canvas.getContext('2d');
    urlInput = $('.br-url');
    tabsBox = $('.br-taby');
    stavEl = $('.br-stav');
    stavEl.textContent = 'Připojuji prohlížeč…';
    scena = $('.br-scena');
    zamek = $('.br-zamek');
    pomerEl = $('.br-pomer');
    pomerEl.value = pomer;
    pomerEl.addEventListener('change', () => { zmenPomer(pomerEl.value); try { kb.focus({preventScroll: true}); } catch (_) { /* nic */ } });
    pomerEl.addEventListener('keydown', (ev) => ev.stopPropagation());
    tlMax = $('.br-max');
    tlMax.onclick = () => maximalizuj();
    dialogEl = $('.br-dialog');
    chatyEl = $('.br-chaty');
    pomocEl = $('.br-pomoc');
    $('.br-pomoc-ok').onclick = () => { odbydPotreby(); pomocEl.hidden = true; nastav('mini'); };
    kb = $('.br-kb');
    tlZpet = $('.br-zpet'); tlVpred = $('.br-vpred'); tlZnovu = $('.br-znovu');
    $('.br-mini').onclick = () => { odbydPotreby(); nastav('mini'); };
    $('.br-zavri').onclick = () => { odbydPotreby(); zavrelJsi = true; nastav('zavreno'); };
    tlZpet.onclick = () => send({t: 'br', a: 'back'});
    tlVpred.onclick = () => send({t: 'br', a: 'forward'});
    tlZnovu.onclick = () => send({t: 'br', a: info.loading ? 'stop' : 'reload'});
    urlInput.addEventListener('focus', () => { urlInput.value = info.url === 'about:blank' ? '' : (info.url || ''); urlInput.select(); });
    urlInput.addEventListener('blur', ukazUrl);
    urlInput.addEventListener('keydown', (ev) => {
      ev.stopPropagation();
      if (ev.key === 'Enter') { send({t: 'br', a: 'go', url: urlInput.value}); kb.focus(); }
      else if (ev.key === 'Escape') { kb.focus(); }
      else zkratka(ev);
    });
    tahni($('.br-hlava'));
    velikost($('.br-roh'));
    platno();
    // Změna plochy okna = stránka dostane novou velikost (jako když tažíš za okraj v Chromu).
    new ResizeObserver(() => { rozloz(); posliVelikost(); }).observe(scena);
    // Schovaný panel (jiná karta prohlížeče, zamknutá obrazovka) obraz nechce.
    document.addEventListener('visibilitychange', () => { posliVidi(); if (!document.hidden) pumpuj(); });
    root.addEventListener('pointerdown', (ev) => {
      root.classList.add('br-nahore');
      // Zkratky (Ctrl+T, Ctrl+Tab…) mají fungovat i po kliknutí na kartu nebo lištu.
      // Výběr (poměr stran) ne: přesun fokusu by jeho nabídku hned zavřel.
      if (!ev.target.closest('input, textarea, select')) setTimeout(() => { try { kb.focus({preventScroll: true}); } catch (_) { /* nic */ } }, 0);
    });
    // Zkratky fungují kdekoli v okně (i na liště), ne jen v poli pro psaní.
    root.addEventListener('keydown', (ev) => { if (ev.target !== kb && ev.target !== urlInput) zkratka(ev); });
  }

  /* ── plátno: velikost, přesun ─────────────────────────────────────────────
     Obraz se na plátno nezmenšuje přes CSS (`object-fit` na canvasu některé
     jádra neumí), velikost i střed se počítají tady. Stránka má velikost
     plochy okna, takže měřítko je většinou přesně 1:1. */
  function rozloz() {
    if (!canvas || !scena) return;
    const sw = scena.clientWidth, sh = scena.clientHeight;
    if (!sw || !sh) return;
    const k = Math.min(sw / frame.w, sh / frame.h);
    const w = Math.round(frame.w * k), h = Math.round(frame.h * k);
    canvas.style.width = w + 'px';
    canvas.style.height = h + 'px';
    canvas.style.left = Math.round((sw - w) / 2) + 'px';
    canvas.style.top = Math.round((sh - h) / 2) + 'px';
  }

  function posliVelikost() {
    clearTimeout(sizeTimer);
    sizeTimer = setTimeout(() => {
      if (!otevrene || !scena) return;
      const w = scena.clientWidth, h = scena.clientHeight;
      // Hustota pixelů displeje: stránka se pak kreslí ostře i na HiDPI.
      const dpr = Math.round((window.devicePixelRatio || 1) * 100) / 100;
      // 9:16 = stránka jako na telefonu (mobilní viewport přesně o velikosti plochy).
      const mobil = pomer === '9:16' && (!root.classList.contains('br-cela') || window.matchMedia(UZKE).matches);
      const sig = w + 'x' + h + '@' + dpr + (mobil ? 'm' : '');
      if (!w || !h || sig === odeslanaVelikost) return;
      odeslanaVelikost = sig;
      send({t: 'br', a: 'size', w, h, dpr, mobil});
    }, 250);
  }

  function tahni(hlava) {
    const neni = (t) => t.closest('.br-btn, .br-tab, .br-tab-plus, .br-chat, .br-pomer, button, select');
    hlava.addEventListener('pointerdown', (ev) => {
      if (neni(ev.target) || root.classList.contains('br-cela')) return;
      ev.preventDefault();
      const x0 = ev.clientX - geo.x, y0 = ev.clientY - geo.y;
      hlava.setPointerCapture(ev.pointerId);
      const move = (e) => {
        geo.x = Math.max(-geo.w + 100, Math.min(e.clientX - x0, window.innerWidth - 100));
        geo.y = Math.max(0, Math.min(e.clientY - y0, window.innerHeight - 40));
        usad();
      };
      const up = () => {
        hlava.removeEventListener('pointermove', move);
        hlava.removeEventListener('pointerup', up);
        ulozGeo();
      };
      hlava.addEventListener('pointermove', move);
      hlava.addEventListener('pointerup', up);
    });
    hlava.addEventListener('dblclick', (ev) => {
      if (neni(ev.target)) return;
      maximalizuj();
    });
  }

  function velikost(roh) {
    roh.addEventListener('pointerdown', (ev) => {
      ev.preventDefault();
      const x0 = ev.clientX, y0 = ev.clientY, w0 = geo.w, h0 = geo.h;
      roh.setPointerCapture(ev.pointerId);
      const move = (e) => {
        geo.w = Math.max(MIN_W, Math.min(w0 + e.clientX - x0, window.innerWidth - geo.x));
        geo.h = Math.max(MIN_H, Math.min(h0 + e.clientY - y0, window.innerHeight - geo.y));
        if (POMERY[pomer]) {
          // Pevný poměr: rozhoduje směr, kterým se táhne víc.
          const zVysky = Math.round((geo.h - ramecek()) * POMERY[pomer]) + 2;
          prizpusob(geo, zVysky > geo.w);
          if (geo.x + geo.w > window.innerWidth || geo.y + geo.h > window.innerHeight) {
            geo.w = Math.min(geo.w, window.innerWidth - geo.x);
            prizpusob(geo);
          }
        }
        usad();
      };
      const up = () => {
        roh.removeEventListener('pointermove', move);
        roh.removeEventListener('pointerup', up);
        ulozGeo();
      };
      roh.addEventListener('pointermove', move);
      roh.addEventListener('pointerup', up);
    });
  }

  /* ── zkratky jako v prohlížeči ───────────────────────────────────────────── */
  function dalsiKarta(posun) {
    const ids = viditelne(info.pages).map((p) => p.id);
    if (ids.length < 2) return;
    const i = ids.indexOf(info.active);
    send({t: 'br', a: 'tab', id: ids[(i + posun + ids.length) % ids.length]});
  }

  /* Ctrl+F: hledání na stránce (CDP žádné vlastní nemá) — dotaz a `window.find`. */
  async function hledejNaStrance() {
    const dotaz = await window.HubDialog.prompt('Hledat na stránce:', hledano || '', {title: 'Hledat'});
    if (dotaz == null || !dotaz) return;
    hledano = dotaz;
    send({t: 'br', a: 'najit', text: dotaz});
  }
  let hledano = '';

  /* Vrací true, když zkratku vzalo okno (a nemá jít do stránky). */
  function zkratka(ev) {
    const ctrl = ev.ctrlKey || ev.metaKey;
    const k = ev.key.toLowerCase();
    let hotovo = true;
    if (ctrl && k === 'l') { urlInput.focus(); }
    else if (ctrl && k === 't') { send({t: 'br', a: 'newtab'}); }
    else if (ctrl && k === 'w') { if (info.active) send({t: 'br', a: 'closetab', id: info.active}); }
    else if ((ctrl && k === 'r') || ev.key === 'F5') { send({t: 'br', a: 'reload'}); }
    else if (ev.altKey && ev.key === 'ArrowLeft') { send({t: 'br', a: 'back'}); }
    else if (ev.altKey && ev.key === 'ArrowRight') { send({t: 'br', a: 'forward'}); }
    else if (ctrl && ev.key === 'Tab') { dalsiKarta(ev.shiftKey ? -1 : 1); }
    else if (ctrl && ev.key === 'PageDown') { dalsiKarta(1); }
    else if (ctrl && ev.key === 'PageUp') { dalsiKarta(-1); }
    else if (ctrl && k === 'c' && document.activeElement !== urlInput) { send({t: 'br', a: 'copy'}); }
    else if (ctrl && k === 'f' && document.activeElement !== urlInput) { hledejNaStrance(); }
    else if (ev.key === 'Escape' && info.loading && document.activeElement !== urlInput) { send({t: 'br', a: 'stop'}); }
    else hotovo = false;
    if (hotovo) { ev.preventDefault(); ev.stopPropagation(); }
    return hotovo;
  }

  /* ── plátno: myš, kolečko, klávesy ───────────────────────────────────────── */
  function platno() {
    // Plátno má přesně velikost obrazu, takže stačí poměr jeho rozměrů.
    const bod = (ev) => {
      const r = canvas.getBoundingClientRect();
      return {x: (ev.clientX - r.left) * frame.w / (r.width || 1), y: (ev.clientY - r.top) * frame.h / (r.height || 1)};
    };
    const mod = (ev) => (ev.altKey ? 1 : 0) | (ev.ctrlKey ? 2 : 0) | (ev.metaKey ? 4 : 0) | (ev.shiftKey ? 8 : 0);
    let posl = 0, pohybTimer = null, pohyb = null;
    const posliPohyb = () => {
      pohybTimer = null;
      if (!pohyb) return;
      posl = Date.now();
      send(pohyb);
      pohyb = null;
    };
    canvas.addEventListener('pointerdown', (ev) => {
      kb.focus();
      canvas.setPointerCapture(ev.pointerId);
      send({t: 'br', a: 'mouse', type: 'down', ...bod(ev), button: ev.button, clicks: ev.detail || 1, mod: mod(ev)});
    });
    canvas.addEventListener('pointerup', (ev) => {
      send({t: 'br', a: 'mouse', type: 'up', ...bod(ev), button: ev.button, clicks: ev.detail || 1, mod: mod(ev)});
    });
    // Pohyb nejvýš ~30× za vteřinu, ale poslední poloha vždycky dojde
    // (jinak by po zastavení myši zůstal hover na špatném místě).
    canvas.addEventListener('pointermove', (ev) => {
      pohyb = {t: 'br', a: 'mouse', type: 'move', ...bod(ev), button: 0, clicks: 0, mod: mod(ev)};
      if (pohybTimer) return;
      const zbyva = 33 - (Date.now() - posl);
      if (zbyva <= 0) posliPohyb(); else pohybTimer = setTimeout(posliPohyb, zbyva);
    });
    // Kolečko: události za jeden snímek obrazovky se sečtou do jedné zprávy
    // (touchpad jich posílá desítky za vteřinu). Řádky/stránky → pixely.
    let kolecko = null;
    canvas.addEventListener('wheel', (ev) => {
      ev.preventDefault();
      const k = ev.deltaMode === 1 ? 40 : ev.deltaMode === 2 ? (scena.clientHeight || 600) : 1;
      if (!kolecko) {
        kolecko = {...bod(ev), dx: 0, dy: 0};
        requestAnimationFrame(() => {
          const w = kolecko;
          kolecko = null;
          if (w && (w.dx || w.dy)) send({t: 'br', a: 'wheel', x: w.x, y: w.y, dx: w.dx, dy: w.dy});
        });
      }
      kolecko.dx += ev.deltaX * k;
      kolecko.dy += ev.deltaY * k;
    }, {passive: false});
    canvas.addEventListener('contextmenu', (ev) => ev.preventDefault());

    /* Psaní bere skrytá textarea pod plátnem: na plátno se psát nedá a diakritiku
       (ě, š, č…) z metody zadávání by zahodilo. Obyčejná písmena ASCII jdou jako
       klávesy (stránky je vidí jako skutečné stisky), cokoli dalšího — diakritika,
       složené znaky, vložení — jde jako text. Enter, šipky, Backspace a zkratky
       prohlížeče jdou vždycky jako klávesy / příkazy. */
    const foc = () => { try { kb.focus({preventScroll: true}); } catch (_) { kb.focus(); } };
    canvas.addEventListener('pointerdown', foc);
    scena.addEventListener('pointerup', () => { if (document.activeElement !== urlInput) foc(); });
    const ascii = (ev) => ev.key.length === 1 && ev.key.charCodeAt(0) < 128;
    /* AltGr (české @ { } [ ] \\ |): na Windows ho prohlížeč hlásí jako Ctrl+Alt,
       jinde jako stav „AltGraph". Znak z něj jde jako vložený text — jako stisk
       s Ctrl+Alt by ho stránka zahodila. */
    const altgr = (ev) => ev.key.length === 1 &&
      (ev.getModifierState('AltGraph') || (ev.ctrlKey && ev.altKey));
    /* Dotyková klávesnice (Android) posílá `Unidentified` a keyCode 229: stisky
       tam nejsou, text přijde přes `input` / složení. */
    const dotykova = (ev) => ev.key === 'Unidentified' || ev.keyCode === 229;
    kb.addEventListener('keydown', (ev) => {
      ev.stopPropagation();
      if (zkratka(ev)) return;
      if (ev.isComposing || ev.key === 'Process' || ev.key === 'Dead' || dotykova(ev)) return;
      if (altgr(ev)) { ev.preventDefault(); send({t: 'br', a: 'key', type: 'down', key: ev.key, altgr: true, mod: 0}); return; }
      const zk = ev.ctrlKey || ev.metaKey || ev.altKey;
      if (ev.key.length === 1 && !ascii(ev) && !zk) return;            // diakritika → textarea → input
      if ((ev.ctrlKey || ev.metaKey) && ev.key.toLowerCase() === 'v') return;   // vložení → paste
      ev.preventDefault();
      send({t: 'br', a: 'key', type: 'down', key: ev.key, code: ev.code, vk: ev.keyCode, mod: mod(ev)});
    });
    kb.addEventListener('keyup', (ev) => {
      ev.stopPropagation();
      if (ev.isComposing || ev.key === 'Process' || ev.key === 'Dead' || dotykova(ev) || altgr(ev)) return;
      if (ev.key.length === 1 && !ascii(ev) && !(ev.ctrlKey || ev.metaKey || ev.altKey)) return;
      if ((ev.ctrlKey || ev.metaKey) && ['v', 'c', 'l', 't', 'w', 'r'].includes(ev.key.toLowerCase())) return;
      if (ev.key === 'F5' || (ev.altKey && (ev.key === 'ArrowLeft' || ev.key === 'ArrowRight'))) return;
      ev.preventDefault();
      send({t: 'br', a: 'key', type: 'up', key: ev.key, code: ev.code, vk: ev.keyCode, mod: mod(ev)});
    });
    const vypis = () => {
      const text = kb.value;
      kb.value = '';
      if (text) send({t: 'br', a: 'text', text});
    };
    kb.addEventListener('input', (ev) => { if (!ev.isComposing) vypis(); });
    kb.addEventListener('compositionend', () => setTimeout(vypis, 0));
    kb.addEventListener('paste', (ev) => {
      ev.preventDefault();
      const text = ev.clipboardData && ev.clipboardData.getData('text');
      if (text) send({t: 'br', a: 'text', text});
    });
  }

  /* ── stavy okna ──────────────────────────────────────────────────────────── */
  function otevriSpojeni(pasivne) {
    if (otevrene) return;
    otevrene = true;
    odeslanaVelikost = '';
    odeslaneVidi = '';
    send({t: 'br', a: 'open', pasivne: !!pasivne, bin: true});
    send({t: 'br', a: 'sleduj', tab: vyber});
    posliVidi();
    posliVelikost();
  }

  /* Obraz jen tehdy, když je okno opravdu vidět: rozbalené, ne schované
     (chat bez karet) a stránka appky není na pozadí. Schované = `pauza`:
     stránka si nechá velikost okna, po návratu se nic nepřeskládá;
     minimalizované = stránka se vrátí do svého okna. */
  function posliVidi() {
    if (!otevrene) return;
    const on = stav === 'okno' && !!root && !root.hidden && !document.hidden;
    const pauza = !on && stav === 'okno';
    const sig = on ? '1' : pauza ? 'p' : '0';
    if (sig === odeslaneVidi) return;
    odeslaneVidi = sig;
    send({t: 'br', a: 'vidi', on, pauza});
  }
  function zavriSpojeni() {
    if (!otevrene) return;
    otevrene = false;
    send({t: 'br', a: 'close'});
  }

  function pilulka() {
    if (pill) return pill;
    pill = el('button', 'br-pilulka');
    pill.innerHTML = ico('i-globe') + '<span>Prohlížeč</span>';
    pill.title = 'Znovu otevřít prohlížeč';
    pill.onclick = () => nastav('okno');
    // Zmenšený prohlížeč bydlí v horní liště (dřív plaval vpravo dole a kryl
    // se s pole zprávy a dalšími plovoucími prvky).
    const lista = document.getElementById('btn-refresh');
    (lista && lista.parentNode ? lista.parentNode : document.body)
      .insertBefore(pill, lista || null);
    return pill;
  }

  /* Okno i lišta patří chatu, na kterém stojíš: vidíš je jen tam, kde má Claude
     v prohlížeči karty. Jiný chat = jiný prohlížeč, nebo žádný. Výjimka: okno,
     které se samo otevřelo, protože je potřeba člověk (přihlášení, captcha). */
  function usadViditelnost() {
    const ma = (aktivniTab && !!mapaTabu[aktivniTab]) || (samo && stav === 'okno');
    if (root) root.hidden = stav !== 'okno' || !ma;
    if (pill) pill.hidden = stav !== 'mini' || !ma;
    document.body.classList.toggle('br-otevren', stav === 'okno' && ma);
    posliVidi();
  }

  /* `zavreno` = pryč a server neposílá nic; `mini` = jen lišta (obraz se dál
     stahuje, ať je při rozbalení hned vidět, co se děje); `okno` = plovoucí. */
  function nastav(novy, potichu) {
    if (!root) { geo = nactiGeo(); sestav(); usad(); }
    stav = novy;
    pilulka();
    if (novy !== 'okno') samo = false;
    usadViditelnost();
    if (novy === 'zavreno') zavriSpojeni(); else otevriSpojeni();
    // Minimalizované okno obraz nestahuje — jen ví, co je v kartách.
    posliVidi();
    if (pomocEl && novy !== 'okno') pomocEl.hidden = true;
    if (novy === 'okno') {
      if (geo && !root.hidden) prizpusob(geo);
      usad();
      rozloz();
      posliVelikost();
      if (!potichu) root.classList.remove('br-zavolano');
    }
    ulozGeo();
  }

  /* ── obraz: příjem, dekódování, kreslení, potvrzení ──────────────────────
     Čeká nejvýš jeden snímek; přijde-li novější, starší se zahodí (a potvrdí,
     ať server ví, že je vyřízený). Dekóduje se mimo hlavní vlákno a kreslí
     se jen celý obrázek — žádné <img> s base64, žádné přeskládání stránky.
     Plátno má tolik pixelů, kolik snímek (Chrome ho posílá už ve velikosti
     plochy), a CSS ho kreslí na velikost plochy — 1:1, nic se nepřevzorkuje. */
  let cekaSnimek = null, dekoduji = false;
  const potvrd = () => send({t: 'br', a: 'ack'});

  function prijmi(hdr, zdroj) {
    if (cekaSnimek) potvrd();             // nestihl se vykreslit — zahozený
    cekaSnimek = {hdr, zdroj};
    pumpuj();
  }

  function dekoduj(zdroj) {
    if (zdroj instanceof Blob && global.createImageBitmap) return createImageBitmap(zdroj);
    return new Promise((ok, chyba) => {
      const url = zdroj instanceof Blob ? URL.createObjectURL(zdroj) : zdroj;
      const img = new Image();
      const uklid = () => { if (zdroj instanceof Blob) URL.revokeObjectURL(url); };
      img.onload = () => { uklid(); img.close = () => {}; ok(img); };
      img.onerror = () => { uklid(); chyba(new Error('snímek')); };
      img.src = url;
    });
  }

  async function pumpuj() {
    // Schovaná stránka nedekóduje ani nepotvrzuje — server pak nic neposílá.
    if (dekoduji || !cekaSnimek || document.hidden) return;
    const f = cekaSnimek;
    cekaSnimek = null;
    dekoduji = true;
    let bmp = null;
    try { bmp = await dekoduj(f.zdroj); } catch (_) { bmp = null; }
    dekoduji = false;
    if (bmp && canvas) {
      const w = bmp.width || bmp.naturalWidth, h = bmp.height || bmp.naturalHeight;
      const zmena = f.hdr.w && f.hdr.h && (f.hdr.w !== frame.w || f.hdr.h !== frame.h);
      if (f.hdr.w && f.hdr.h) frame = {w: f.hdr.w, h: f.hdr.h};
      if (canvas.width !== w || canvas.height !== h) {
        canvas.width = w;
        canvas.height = h;
        ctx.imageSmoothingQuality = 'high';
        rozloz();
      } else if (zmena) rozloz();
      ctx.drawImage(bmp, 0, 0, w, h);
      if (!stavEl.hidden && !stavEl.textContent.startsWith('Prázdná')) stavEl.hidden = true;
    }
    if (bmp && bmp.close) bmp.close();
    potvrd();
    pumpuj();
  }

  /* Binární zpráva ze serveru: "BR", délka hlavičky (2 B), JSON hlavička, JPEG. */
  function naSnimek(buf) {
    if (!root || !(buf instanceof ArrayBuffer) || buf.byteLength < 4) return;
    const dv = new DataView(buf);
    if (dv.getUint8(0) !== 66 || dv.getUint8(1) !== 82) return;       // "BR"
    const n = dv.getUint16(2);
    let hdr = {};
    try { hdr = JSON.parse(new TextDecoder().decode(new Uint8Array(buf, 4, n))); } catch (_) { hdr = {}; }
    prijmi(hdr, new Blob([new Uint8Array(buf, 4 + n)], {type: 'image/jpeg'}));
  }

  function hostitel(url) {
    try { return new URL(url).host.replace(/^www\./, ''); } catch (_) { return ''; }
  }

  function ukazUrl() {
    if (!urlInput || document.activeElement === urlInput) return;
    const u = info.url || '';
    urlInput.value = !u || u === 'about:blank' ? '' : u.replace(/^https?:\/\/(www\.)?/, '').replace(/\/$/, '');
    urlInput.title = u;
    const https = /^https:/i.test(u);
    zamek.className = 'br-zamek' + (https ? ' ok' : u && !/^about:/i.test(u) ? ' nejiste' : '');
    zamek.innerHTML = https ? ico('i-lock') : (u && !/^about:/i.test(u) ? '<span class="br-nezab">Nezabezpečeno</span>' : '');
  }

  /* Karty se jen aktualizují (nepřestavují), ať neblikají a klik nezmizí pod
     myší. Pořadí drží server podle vzniku karty — kliknutá karta zůstane na
     svém místě. */
  /* Karty, které okno ukazuje: jen vybraného chatu (a ty bez majitele, když
     je vybráno „Vše“). Když chat žádné nemá, ukážou se všechny. */
  function viditelne(pages) {
    pages = pages || [];
    if (!vyber) return pages;
    return pages.filter((p) => p.tab === vyber);
  }

  function kresliKarty(msg) {
    const pages = viditelne(msg.pages);
    const ids = new Set(pages.map((p) => p.id));
    for (const [id, node] of taby) if (!ids.has(id)) { node.remove(); taby.delete(id); }
    let plus = tabsBox.querySelector('.br-tab-plus');
    if (!plus) {
      plus = el('button', 'br-tab-plus', '+');
      plus.title = 'Nová karta (Ctrl+T)';
      plus.onclick = () => send({t: 'br', a: 'newtab'});
      tabsBox.appendChild(plus);
    }
    let pred = null;
    for (const p of pages) {
      let node = taby.get(p.id);
      if (!node) {
        node = el('div', 'br-tab');
        node.innerHTML = '<span class="br-fav"></span><span class="br-tab-t"></span>' +
          '<button class="br-tab-x" title="Zavřít kartu (Ctrl+W)">' + ico('i-close') + '</button>';
        node.addEventListener('pointerdown', (ev) => {
          if (ev.button === 1) { ev.preventDefault(); send({t: 'br', a: 'closetab', id: p.id}); }
        });
        node.addEventListener('click', (ev) => {
          if (ev.target.closest('.br-tab-x')) { ev.stopPropagation(); send({t: 'br', a: 'closetab', id: p.id}); return; }
          send({t: 'br', a: 'tab', id: p.id});
        });
        taby.set(p.id, node);
        tabsBox.insertBefore(node, plus);
      }
      // Pořadí podle serveru; prvky se přesouvají jen když nesedí.
      if (pred ? node.previousElementSibling !== pred : node !== tabsBox.firstElementChild) {
        tabsBox.insertBefore(node, pred ? pred.nextSibling : tabsBox.firstChild);
      }
      pred = node;
      const aktivni = p.id === msg.active;
      node.classList.toggle('on', aktivni);
      node.classList.toggle('br-tab-pomoc', !!p.potreba);
      node.classList.toggle('nacita', aktivni && !!msg.loading);
      const t = p.title && p.title !== p.url ? p.title : (hostitel(p.url) || (p.url === 'about:blank' ? 'Nová karta' : p.url) || 'Nová karta');
      node.querySelector('.br-tab-t').textContent = t;
      node.title = (p.title || t) + (p.url ? '\n' + p.url : '');
      const fav = node.querySelector('.br-fav');
      if (p.icon && fav.dataset.src !== p.icon) {
        fav.dataset.src = p.icon;
        fav.textContent = '';
        const img = el('img');
        img.src = p.icon;
        img.alt = '';
        img.onerror = () => { fav.textContent = ''; };
        fav.appendChild(img);
      } else if (!p.icon && fav.dataset.src) {
        fav.dataset.src = '';
        fav.textContent = '';
      }
    }
  }

  function dialog(msg) {
    if (msg.zavrit) { dialogEl.hidden = true; dialogEl.textContent = ''; return; }
    dialogEl.textContent = '';
    dialogEl.hidden = false;
    const card = el('div', 'br-dialog-karta');
    card.appendChild(el('div', 'br-dialog-text', msg.zprava || ''));
    let pole = null;
    if (msg.typ === 'prompt') {
      pole = el('input', 'br-dialog-pole');
      pole.type = 'text';
      pole.value = msg.vychozi || '';
      card.appendChild(pole);
    }
    const btns = el('div', 'br-dialog-btns');
    const ok = el('button', 'btn primary', 'OK');
    ok.onclick = () => send({t: 'br', a: 'dialog', accept: true, text: pole ? pole.value : ''});
    btns.appendChild(ok);
    if (msg.typ !== 'alert') {
      const no = el('button', 'btn ghost', 'Zrušit');
      no.onclick = () => send({t: 'br', a: 'dialog', accept: false});
      btns.appendChild(no);
    }
    card.appendChild(btns);
    dialogEl.appendChild(card);
    (pole || ok).focus();
    if (pole) pole.onkeydown = (ev) => { ev.stopPropagation(); if (ev.key === 'Enter') ok.click(); if (ev.key === 'Escape') btns.lastChild.click(); };
  }

  function naZpravu(msg) {
    if (!root) {
      // Pasivní sledování: okno ještě neexistuje, ale chat má karty → postaví se lišta.
      if (msg.t !== 'br-info') return;
      ohlasTaby(msg);
      if (zavrelJsi || !aktivniTab || !mapaTabu[aktivniTab]) return;
      nastav('mini', true);
    }
    if (msg.t === 'br-frame') {
      // Starý tvar (base64 v JSONu) — jen kdyby server ještě neuměl binární.
      prijmi({w: msg.w, h: msg.h}, 'data:image/jpeg;base64,' + msg.d);
    } else if (msg.t === 'br-info') {
      info = msg;
      const akt = (msg.pages || []).find((p) => p.id === msg.active);
      if (akt && akt.tab && akt.tab !== '?') posledniKarta[akt.tab] = akt.id;
      ukazUrl();
      kresliKarty(msg);
      tlZpet.disabled = !msg.back;
      tlVpred.disabled = !msg.fwd;
      tlZnovu.innerHTML = msg.loading ? ico('i-close') : ico('i-refresh');
      tlZnovu.title = msg.loading ? 'Zastavit načítání (Esc)' : 'Načíst znovu (Ctrl+R)';
      // Prázdná karta je v Chromiu černá — vypadá to jako porucha, tak se řekne, co to je.
      const prazdna = !msg.url || msg.url === 'about:blank';
      if (prazdna) { stavEl.textContent = 'Prázdná karta — napiš nahoře adresu, nebo počkej, až tam Claude něco otevře.'; stavEl.hidden = false; }
      else if (stavEl.textContent.startsWith('Prázdná')) stavEl.hidden = true;
      kresliChaty(msg);
      ohlasTaby(msg);
      hlidejPotreby(msg);
      // Chat, na kterém stojíš, má karty (třeba z doby před restartem appky,
      // kdy se žádný nástroj naživo nespustil) — ukáže se lišta.
      if (stav === 'zavreno' && !zavrelJsi && aktivniTab && mapaTabu[aktivniTab]) nastav('mini', true);
    } else if (msg.t === 'br-nic') {
      // Prohlížeč neběží (pasivní dotaz). Když mezitím okno otevřel člověk,
      // požádá se o něj hned naostro — jinak by zůstalo „Připojuji…“ navždy.
      otevrene = false;
      if (stav !== 'zavreno') otevriSpojeni();
    } else if (msg.t === 'br-dialog') {
      dialog(msg);
    } else if (msg.t === 'br-copy') {
      if (msg.text && copyText) copyText(msg.text);
    } else if (msg.t === 'br-stav') {
      if (!msg.ok) {
        stavEl.hidden = false;
        stavEl.textContent = msg.zprava || 'Prohlížeč není k dispozici.';
      }
    }
  }

  /* ── chaty v záhlaví ─────────────────────────────────────────────────────── */
  function chatyKaret(pages) {
    const out = [];
    for (const p of pages || []) if (p.tab && p.tab !== '?' && !out.includes(p.tab)) out.push(p.tab);
    return out;
  }

  /* Jedno tlačítko „Vše ▾" / název chatu s nabídkou místo řady pilulek —
     pilulky se v úzkém okně tlačily přes poměr stran a přes sebe. */
  let chatyVolby = [];
  function kresliChaty(msg) {
    if (!chatyEl) return;
    const chaty = chatyKaret(msg.pages);
    chatyEl.textContent = '';
    chatyVolby = [];
    if (chaty.length < 2 && !(chaty.length === 1 && (msg.pages || []).some((p) => p.tab !== chaty[0]))) return;
    const pomoc = (sid) => !!sid && (msg.pages || []).some((p) => p.tab === sid && p.potreba);
    chatyVolby = [{sid: null, text: 'Vše', pomoc: false}]
      .concat(chaty.map((sid) => ({sid, text: tabTitle(sid) || 'Chat', pomoc: pomoc(sid)})));
    const ted = chatyVolby.find((v) => v.sid === (vyber || null)) || chatyVolby[0];
    const b = el('button', 'br-chat on' + (chatyVolby.some((v) => v.pomoc) ? ' br-chat-pomoc' : ''));
    b.appendChild(el('span', 'br-chat-text', ted.text));
    b.insertAdjacentHTML('beforeend', ico('i-chevron').replace('class="ico"', 'class="ico br-chat-caret"'));
    b.title = 'Karty kterého chatu ukázat';
    b.onclick = (ev) => {
      ev.stopPropagation();
      const r = b.getBoundingClientRect();
      const polozky = chatyVolby.map((v) => ({
        icon: 'i-terminal', label: v.text + (v.pomoc ? ' — potřebuje pomoc' : ''),
        on: v.sid === (vyber || null), run: () => vyberChat(v.sid)}));
      if (typeof global.showMenu === 'function') global.showMenu(r.left, r.bottom, polozky);
      else vyberChat(null);
    };
    chatyEl.appendChild(b);
  }

  /* Karta, na kterou se má okno u chatu přepnout: ta, na které jsi v něm
     naposledy byl (ne vždycky ta nejnovější), jinak jeho poslední. '' = nic
     se přepínat nemusí (aktivní karta už je jeho), nebo chat žádné nemá. */
  function kartaChatu(sid) {
    const moje = (info.pages || []).filter((p) => !sid || p.tab === sid);
    if (!moje.length || moje.some((p) => p.id === info.active)) return '';
    const minule = sid && posledniKarta[sid];
    return (moje.find((p) => p.id === minule) || moje[moje.length - 1]).id;
  }

  /* Okno přepne na karty daného chatu (null = všechny) a na jeho poslední kartu. */
  function vyberChat(sid, karta) {
    vyber = sidOf(sid);
    send({t: 'br', a: 'sleduj', tab: vyber});
    const cil = karta || kartaChatu(vyber);
    if (cil) send({t: 'br', a: 'tab', id: cil});
    kresliKarty(info);
    kresliChaty(info);
  }

  /* Hubu se řekne, které taby mají v prohlížeči karty (ikonka u tabu). */
  function ohlasTaby(msg) {
    const mapa = {};
    for (const p of msg.pages || []) {
      if (!p.tab || p.tab === '?') continue;
      mapa[p.tab] = mapa[p.tab] || {karet: 0, potreba: ''};
      mapa[p.tab].karet++;
      if (p.potreba) mapa[p.tab].potreba = p.potreba;
    }
    mapaTabu = mapa;
    usadViditelnost();
    const sig = JSON.stringify(mapa);
    if (sig === posledniTaby) return;
    posledniTaby = sig;
    document.dispatchEvent(new CustomEvent('hub-br-taby', {detail: mapa}));
  }

  /* ── je potřeba člověk ─────────────────────────────────────────────────────
     Server hlídá karty (hub/prohlizec.py): pole na heslo nebo ověřovací kód,
     které na stránce vydrží, nebo captcha. Pak se okno samo otevře na té
     kartě; když potřeba zmizí (přihlásil ses), složí se zpátky do lišty.
     Když ho při té potřebě zavřeš sám, znovu kvůli ní nevyskočí. */
  function odbydPotreby() {
    for (const p of info.pages || []) if (p.potreba) odbyto.add(p.id + ':' + p.potreba);
  }

  function hlidejPotreby(msg) {
    const pages = msg.pages || [];
    for (const k of [...odbyto]) {
      const [id, duvod] = k.split(':');
      if (!pages.some((p) => p.id === id && p.potreba === duvod)) odbyto.delete(k);
    }
    const ceka = pages.filter((p) => p.potreba && !odbyto.has(p.id + ':' + p.potreba));
    const ted = ceka.find((p) => p.id === msg.active) ||
                ceka.find((p) => vyber && p.tab === vyber) || ceka[0];
    if (ted) {
      if (stav !== 'okno' || ted.id !== msg.active) {
        if (stav !== 'okno') { nastav('okno', true); samo = true; }
        if (ted.tab && ted.tab !== '?' && ted.tab !== vyber) vyberChat(ted.tab, ted.id);
        else if (ted.id !== msg.active) send({t: 'br', a: 'tab', id: ted.id});
      }
      if (ted.id === msg.active) {
        pomocEl.querySelector('span').textContent = DUVODY[ted.potreba] || 'Claude tu potřebuje tvou pomoc.';
        pomocEl.hidden = false;
      }
      return;
    }
    if (pomocEl) pomocEl.hidden = true;
    if (samo && stav === 'okno') nastav('mini');      // vyřízeno — zpátky do lišty
  }

  /* ── Claude prohlížeč používá ───────────────────────────────────────────── */
  /* Zavolá ho čtení, když Claude spustí nástroj Playwright. Okno se kvůli tomu
     neotevírá — jen se ukáže lišta „Prohlížeč“ (zamrká), ať je vidět, že
     Claude v prohlížeči pracuje. Otevře se samo, až je potřeba člověk. */
  function potreba(sid) {
    zavrelJsi = false;
    const ted = Date.now();
    const dlouho = ted - posledniNastroj > POP_PAUZA;
    posledniNastroj = ted;
    if (stav === 'zavreno') nastav('mini', true);
    // Zamrká jen u chatu, ve kterém Claude prohlížeč používá — jinde by to rušilo.
    if (stav === 'mini' && dlouho && pill && (!sid || sidOf(sid) === aktivniTab)) {
      pill.classList.remove('br-mrk');
      void pill.offsetWidth;
      pill.classList.add('br-mrk');
    }
  }

  function install(io) {
    send = io.send;
    io_ = io;
    copyText = io.copy || null;
    if (io.tabTitle) tabTitle = io.tabTitle;
    // Obnovení po reloadu: zůstává tak, jak jsi ho nechal (okno / lišta).
    const g = nactiGeo();
    if (g.stav === 'okno' || g.stav === 'mini') {
      geo = g;
      nastav(g.stav, true);
      if (g.cela && root) { root.classList.add('br-cela'); usad(); }
    }
    // Sledování bez otevírání: appka zjišťuje, jestli mají chaty v prohlížeči karty
    // (i po restartu), a ukáže lištu tam, kde mají. Prohlížeč se kvůli tomu nespouští.
    const pozoruj = () => { if (stav === 'zavreno' && !zavrelJsi) otevriSpojeni(true); };
    setTimeout(pozoruj, 1500);
    setInterval(pozoruj, 15000);
    document.addEventListener('hub-tab', (ev) => {
      aktivniTab = sidOf(ev.detail && ev.detail.id);
      pozoruj();
      vyber = aktivniTab;
      samo = false;
      if (otevrene) {
        send({t: 'br', a: 'sleduj', tab: vyber});
        const cil = kartaChatu(vyber);
        if (cil) send({t: 'br', a: 'tab', id: cil});
        if (root) { kresliKarty(info); kresliChaty(info); }
      }
      usadViditelnost();
    });
    document.addEventListener('hub-tool', (ev) => {
      if (ev.detail && /^mcp__playwright__/.test(ev.detail.name || '')) potreba(ev.detail.tab || null);
    });
    window.addEventListener('resize', () => {
      if (!geo || !root) return;
      geo.w = Math.min(geo.w, window.innerWidth - 8);
      geo.h = Math.min(geo.h, window.innerHeight - 8);
      prizpusob(geo);
      geo.x = Math.max(0, Math.min(geo.x, window.innerWidth - 100));
      geo.y = Math.max(0, Math.min(geo.y, window.innerHeight - 40));
      usad();
      posliVelikost();       // přesun na jiný monitor / zoom mění hustotu pixelů
    });
    // Maximalizované okno drží plochu appky i při zasunutí panelu chatů.
    const plocha = document.querySelector('.main');
    if (plocha && global.ResizeObserver) {
      new ResizeObserver(() => { if (root && root.classList.contains('br-cela')) usad(); }).observe(plocha);
    }
    // Nové spojení hubu (reconnect) — okno si o obraz řekne znovu.
    document.addEventListener('hub-ws-open', () => { otevrene = false; if (stav !== 'zavreno') otevriSpojeni(); });
  }

  global.HubProhlizec = {
    install, naZpravu, naSnimek, potreba,
    otevri: () => nastav('okno'),
    /* Ikonka u tabu: prohlížeč s kartami toho chatu. */
    ukaz: (sid) => { aktivniTab = sidOf(sid); samo = false; nastav('okno'); vyberChat(sid); usadViditelnost(); },
    get stav() { return stav; },
  };
})(window);
