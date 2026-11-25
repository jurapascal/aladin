/* Lupa — obrázek přes celé okno s přibližováním (HubLupa.otevri).

   „Chci lepší práci s obrázky, aby byl zoom“ (Jura, 25. 11. 2026). Kolečko
   myši nebo sevření prstů přibližuje k místu pod kurzorem, tažením se
   posouvá, dvojklik přepne celý obrázek ↔ přiblížení. Víc obrázků z jedné
   konverzace jde listovat šipkami nebo přejetím prstu. Klávesy: + − 0 (celý)
   1 (skutečná velikost) R (otočit) ← → Esc.

   HubLupa.otevri(polozky, index)
     polozky: src (řetězec) nebo [{src, nazev, cesta}] — cesta = soubor
     v prostoru, pak jde i „Stáhnout“ (HubSoubor.stahni). */
(function (global) {
  'use strict';

  const MAX = 16;
  const KROK = 1.25;

  function el(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }
  function tlacitko(text, title, cls) {
    const b = el('button', 'lupa-btn' + (cls ? ' ' + cls : ''), text);
    b.type = 'button';
    b.title = title;
    return b;
  }

  let otevrena = null;          // jen jedna lupa naráz

  function otevri(polozky, index) {
    if (otevrena) otevrena.zavrit();
    const seznam = (Array.isArray(polozky) ? polozky : [polozky])
      .map((p) => (typeof p === 'string' ? {src: p} : p))
      .filter((p) => p && p.src);
    if (!seznam.length) return;
    let i = Math.max(0, Math.min(seznam.length - 1, index | 0));

    const box = el('div', 'lupa');
    box.tabIndex = -1;
    const bar = el('div', 'lupa-bar');
    const nazev = el('span', 'lupa-nazev');
    const pocet = el('span', 'lupa-pocet');
    const mensi = tlacitko('−', 'Oddálit (−)');
    const procenta = tlacitko('100 %', 'Celý obrázek (0) · skutečná velikost (1)', 'lupa-proc');
    const vetsi = tlacitko('+', 'Přiblížit (+)');
    const otoc = tlacitko('⟳', 'Otočit (R)');
    const stahnout = tlacitko('⤓', 'Stáhnout');
    const original = el('a', 'lupa-btn', '↗');
    original.title = 'Otevřít originál v nové kartě';
    original.target = '_blank';
    original.rel = 'noopener';
    const zavritBtn = tlacitko('✕', 'Zavřít (Esc)', 'lupa-zavrit');
    bar.append(nazev, pocet, el('span', 'lupa-mezera'), mensi, procenta, vetsi, otoc, stahnout, original, zavritBtn);

    const plocha = el('div', 'lupa-plocha');
    const img = el('img', 'lupa-img');
    img.alt = '';
    img.draggable = false;
    plocha.appendChild(img);
    const predchozi = tlacitko('‹', 'Předchozí (←)', 'lupa-sipka lupa-vlevo');
    const dalsi = tlacitko('›', 'Další (→)', 'lupa-sipka lupa-vpravo');
    const napoveda = el('div', 'lupa-napoveda',
      'Kolečkem nebo prsty přiblížíš, tažením posuneš, dvojklikem přepneš celý obrázek.');
    box.append(bar, plocha, predchozi, dalsi, napoveda);

    // Stav: měřítko vůči skutečné velikosti obrázku, posun středu obrázku od
    // středu plochy (px) a otočení po 90°.
    let s = 1, x = 0, y = 0, rot = 0, fit = 1, nw = 0, nh = 0;

    const rozmer = () => {
      const r = plocha.getBoundingClientRect();
      return {w: r.width, h: r.height, l: r.left, t: r.top};
    };
    const otoceny = () => rot % 180 !== 0;
    function spocitejFit() {
      const {w, h} = rozmer();
      const iw = otoceny() ? nh : nw, ih = otoceny() ? nw : nh;
      if (!iw || !ih) { fit = 1; return; }
      fit = Math.min((w - 24) / iw, (h - 24) / ih);
      // Malé obrázky (ikony) se roztáhnou nanejvýš 2×, ať nejsou rozmazané.
      if (fit > 1) fit = Math.min(fit, /\.svg(\?|$)/i.test(img.src) ? 8 : 2);
    }
    function omez() {
      const {w, h} = rozmer();
      const iw = (otoceny() ? nh : nw) * s, ih = (otoceny() ? nw : nh) * s;
      const mx = Math.max(0, (iw - w) / 2 + 40), my = Math.max(0, (ih - h) / 2 + 40);
      x = Math.max(-mx, Math.min(mx, x));
      y = Math.max(-my, Math.min(my, y));
      if (iw <= w) x = 0;
      if (ih <= h) y = 0;
    }
    function kresli(plynule) {
      omez();
      img.classList.toggle('plynule', !!plynule);
      img.style.width = nw + 'px';
      img.style.height = nh + 'px';
      img.style.transform = `translate(-50%, -50%) translate(${x}px, ${y}px) rotate(${rot}deg) scale(${s})`;
      procenta.textContent = Math.round(s * 100) + ' %';
      box.classList.toggle('priblizeno', s > fit * 1.01);
    }
    function cely(plynule) { spocitejFit(); s = fit; x = 0; y = 0; kresli(plynule); }
    // Přiblížení k bodu (px v okně): bod pod kurzorem zůstane na místě.
    function zoom(nove, bx, by, plynule) {
      const min = Math.min(fit, 1) * 0.5;
      nove = Math.max(min, Math.min(Math.max(MAX, fit * 4), nove));
      const {w, h, l, t} = rozmer();
      const px = (bx == null ? l + w / 2 : bx) - (l + w / 2);
      const py = (by == null ? t + h / 2 : by) - (t + h / 2);
      x = px - (px - x) * (nove / s);
      y = py - (py - y) * (nove / s);
      s = nove;
      kresli(plynule);
    }

    function ukaz(n) {
      i = (n + seznam.length) % seznam.length;
      const p = seznam[i];
      nw = 0; nh = 0; rot = 0;
      box.classList.add('nacita');
      img.onload = () => {
        nw = img.naturalWidth || 800;
        nh = img.naturalHeight || 600;
        box.classList.remove('nacita');
        cely(false);
      };
      img.onerror = () => { box.classList.remove('nacita'); nazev.textContent = (p.nazev || '') + ' — nejde načíst'; };
      img.src = p.src;
      const jmeno = p.nazev || (p.cesta ? String(p.cesta).split(/[\\/]/).pop() : '');
      nazev.textContent = jmeno;
      nazev.title = p.cesta || jmeno;
      pocet.textContent = seznam.length > 1 ? (i + 1) + ' / ' + seznam.length : '';
      predchozi.hidden = dalsi.hidden = seznam.length < 2;
      original.href = p.src;
      stahnout.hidden = !(p.cesta && global.HubSoubor && global.HubSoubor.stahni);
    }

    // ── myš, prsty ──
    plocha.addEventListener('wheel', (ev) => {
      ev.preventDefault();
      // Sevření na touchpadu chodí jako ctrl+kolečko s malým krokem.
      const k = Math.exp(-ev.deltaY * (ev.ctrlKey ? 0.01 : 0.0018));
      zoom(s * k, ev.clientX, ev.clientY, false);
    }, {passive: false});

    const prsty = new Map();
    let tah = null;             // {x, y, sx, sy, posun} — jedno táhnutí
    let stipnuti = null;        // {d, s, mx, my}
    let posledniKlep = 0;
    plocha.addEventListener('pointerdown', (ev) => {
      if (ev.button !== 0 && ev.pointerType === 'mouse') return;
      plocha.setPointerCapture(ev.pointerId);
      prsty.set(ev.pointerId, {x: ev.clientX, y: ev.clientY});
      if (prsty.size === 1) {
        tah = {x: ev.clientX, y: ev.clientY, sx: x, sy: y, posun: 0, naObrazku: ev.target === img};
      } else if (prsty.size === 2) {
        const [a, b] = [...prsty.values()];
        stipnuti = {d: Math.hypot(a.x - b.x, a.y - b.y) || 1, s, mx: (a.x + b.x) / 2, my: (a.y + b.y) / 2, x, y};
        tah = null;
      }
    });
    plocha.addEventListener('pointermove', (ev) => {
      if (!prsty.has(ev.pointerId)) return;
      prsty.set(ev.pointerId, {x: ev.clientX, y: ev.clientY});
      if (stipnuti && prsty.size >= 2) {
        const [a, b] = [...prsty.values()];
        const d = Math.hypot(a.x - b.x, a.y - b.y) || 1;
        const mx = (a.x + b.x) / 2, my = (a.y + b.y) / 2;
        x = stipnuti.x + (mx - stipnuti.mx);
        y = stipnuti.y + (my - stipnuti.my);
        s = stipnuti.s;
        zoom(stipnuti.s * d / stipnuti.d, mx, my, false);
        return;
      }
      if (!tah) return;
      const dx = ev.clientX - tah.x, dy = ev.clientY - tah.y;
      tah.posun = Math.max(tah.posun, Math.hypot(dx, dy));
      if (s > fit * 1.01) {
        x = tah.sx + dx;
        y = tah.sy + dy;
        kresli(false);
        box.classList.add('tahne');
      }
    });
    const pust = (ev) => {
      if (!prsty.has(ev.pointerId)) return;
      prsty.delete(ev.pointerId);
      box.classList.remove('tahne');
      if (prsty.size < 2) stipnuti = null;
      if (!tah || prsty.size) { if (!prsty.size) tah = null; return; }
      const t = tah;
      tah = null;
      const dx = ev.clientX - t.x;
      // Přejetí prstem doleva/doprava na celém obrázku = další/předchozí.
      if (s <= fit * 1.01 && seznam.length > 1 && Math.abs(dx) > 60 && t.posun > 60) {
        ukaz(i + (dx < 0 ? 1 : -1));
        return;
      }
      if (t.posun > 6) return;
      const ted = Date.now();
      if (ted - posledniKlep < 300) {                    // dvojklik / dvojí klepnutí
        posledniKlep = 0;
        if (s > fit * 1.01) cely(true);
        else zoom(Math.max(fit * 2.5, 1), ev.clientX, ev.clientY, true);
        return;
      }
      posledniKlep = ted;
      // Klepnutí vedle obrázku zavře (jen když se zrovna nepřibližuje).
      if (!t.naObrazku && s <= fit * 1.01) {
        setTimeout(() => { if (posledniKlep === ted) zavrit(); }, 300);
      }
    };
    plocha.addEventListener('pointerup', pust);
    plocha.addEventListener('pointercancel', pust);
    plocha.addEventListener('dblclick', (ev) => ev.preventDefault());

    // ── tlačítka a klávesy ──
    mensi.onclick = () => zoom(s / KROK, null, null, true);
    vetsi.onclick = () => zoom(s * KROK, null, null, true);
    procenta.onclick = () => (Math.abs(s - fit) < 0.01 ? zoom(1, null, null, true) : cely(true));
    otoc.onclick = () => { rot = (rot + 90) % 360; cely(true); };
    stahnout.onclick = () => global.HubSoubor.stahni(seznam[i].cesta);
    predchozi.onclick = () => ukaz(i - 1);
    dalsi.onclick = () => ukaz(i + 1);
    zavritBtn.onclick = () => zavrit();
    function klavesa(ev) {
      if (ev.target && /^(INPUT|TEXTAREA)$/.test(ev.target.tagName)) return;
      const k = ev.key;
      let hotovo = true;
      if (k === 'Escape') zavrit();
      else if (k === '+' || k === '=') zoom(s * KROK, null, null, true);
      else if (k === '-' || k === '_') zoom(s / KROK, null, null, true);
      else if (k === '0') cely(true);
      else if (k === '1') zoom(1, null, null, true);
      else if (k === 'r' || k === 'R') otoc.onclick();
      else if (k === 'ArrowLeft' && seznam.length > 1) ukaz(i - 1);
      else if (k === 'ArrowRight' && seznam.length > 1) ukaz(i + 1);
      else hotovo = false;
      if (hotovo) { ev.preventDefault(); ev.stopPropagation(); }
    }
    const naZmenu = () => { if (nw) { const bylCely = Math.abs(s - fit) < 0.01; spocitejFit(); if (bylCely) s = fit; kresli(false); } };

    function zavrit() {
      document.removeEventListener('keydown', klavesa, true);
      removeEventListener('resize', naZmenu);
      box.remove();
      if (otevrena && otevrena.box === box) otevrena = null;
    }
    document.addEventListener('keydown', klavesa, true);
    addEventListener('resize', naZmenu);
    document.body.appendChild(box);
    box.focus({preventScroll: true});
    otevrena = {box, zavrit};
    ukaz(i);
    // Nápověda jen chvíli a jen poprvé za načtení stránky.
    if (global.__lupaNapoveda) napoveda.remove();
    else { global.__lupaNapoveda = true; setTimeout(() => napoveda.classList.add('pryc'), 3500); }
  }

  global.HubLupa = {otevri, zavrit: () => otevrena && otevrena.zavrit()};
})(window);
