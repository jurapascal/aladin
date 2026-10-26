/* Nastavení → Úložiště a Nastavení → Oznámení (jen na serveru).
 *
 * Úložiště: koláč „co zabírá můj prostor" (gateway/uloziste.py), limit, co jde
 * uvolnit a tlačítko, které na to pustí Clauda. Admin navíc vidí celý disk
 * (systém, zálohy, každý člověk, volno), lidi s jejich limity a po kliknutí
 * koláč každého z nich; spouští přepočet, sloučení mezipamětí a oznámení.
 *
 * Oznámení (admin): poslat oznámení vybraným lidem a záznam, kdo co dostal
 * a co s tím udělal (gateway/oznameni.py).
 *
 * Grafy jsou ruční SVG, bez knihoven. Barvy kategorií mají pevné pořadí
 * (barva patří kategorii, ne jejímu pořadí podle velikosti) a vlastní odstíny
 * pro tmavý režim; legenda s čísly je vždycky vedle, barva sama nic neříká.
 */
'use strict';

(function (global) {

  const MB = 1024 * 1024;
  const GB = 1024 * MB;
  // Kategorické barvy (světlý / tmavý režim), pořadí ověřené na barvoslepost.
  const BARVY = {
    light: ['#2a78d6', '#eb6834', '#1baf7a', '#eda100', '#e87ba4', '#008300', '#6250d6', '#e34948'],
    dark:  ['#3987e5', '#d95926', '#199e70', '#c98500', '#d55181', '#008300', '#9085e9', '#e66767'],
  };
  const SEDA = {light: ['#8f8d87', '#6c6a65'], dark: ['#8a8984', '#a9a8a2']};
  const KAT_SLOT = {chaty: 0, mezipameti: 1, projekty: 2, obsidian: 3, stazene: 4, programy: 5, ostatni: 6};
  const STAV = {nad: 'Nad limitem', blizko: 'Blízko limitu', ok: 'V pořádku', '': 'Bez limitu'};

  const tmavy = () => document.documentElement.style.colorScheme === 'dark';
  const barvy = () => BARVY[tmavy() ? 'dark' : 'light'];

  function el(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }

  function velikost(b) {
    b = Number(b || 0);
    if (b >= GB) return (b / GB).toLocaleString('cs-CZ', {maximumFractionDigits: 1, minimumFractionDigits: 1}) + ' GB';
    if (b >= MB) return Math.round(b / MB).toLocaleString('cs-CZ') + ' MB';
    return b ? Math.max(1, Math.round(b / 1024)) + ' kB' : '0 MB';
  }

  /* „1 soubor", „3 soubory", „5 souborů" */
  function pocet(n, jeden, dva, pet) {
    n = Number(n || 0);
    return n.toLocaleString('cs-CZ') + ' ' + (n === 1 ? jeden : n >= 2 && n <= 4 ? dva : pet);
  }

  function kdy(ts) {
    if (!ts) return 'zatím nikdy';
    const s = Math.max(0, Date.now() / 1000 - ts);
    if (s < 90) return 'právě teď';
    if (s < 3600) return 'před ' + Math.round(s / 60) + ' min';
    if (s < 86400) return 'před ' + Math.round(s / 3600) + ' h';
    const d = new Date(ts * 1000);
    return d.getDate() + '. ' + (d.getMonth() + 1) + '. ' + d.getHours() + ':' + String(d.getMinutes()).padStart(2, '0');
  }

  async function gw(url, payload) {
    const r = await fetch(url, payload === undefined ? {credentials: 'same-origin'} : {
      method: 'POST', credentials: 'same-origin',
      headers: {'Content-Type': 'application/json', 'X-Hub-Account': '1'},
      body: JSON.stringify(payload),
    });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(d.error || 'Server teď neodpovídá, zkus to za chvíli.');
    return d;
  }

  /* ── koláč ──────────────────────────────────────────────────────────────
     `casti` = [{nazev, bajtu, barva}], `celkem` = 100 % (u disku i volné
     místo, které zůstane jako prázdná stopa). Mezi dílky 2px mezera v barvě
     podkladu; najetí na dílek nebo řádek legendy zvýrazní oba. */
  function kolac(casti, celkem, stred, popisStredu) {
    const wrap = el('div', 'ul-kolac');
    const NS = 'http://www.w3.org/2000/svg';
    const svg = document.createElementNS(NS, 'svg');
    svg.setAttribute('viewBox', '0 0 120 120');
    svg.setAttribute('role', 'img');
    svg.setAttribute('aria-label', casti.map((c) => c.nazev + ' ' + velikost(c.bajtu)).join(', '));
    const R = 46, OBVOD = 2 * Math.PI * R;
    const stopa = document.createElementNS(NS, 'circle');
    stopa.setAttribute('cx', 60); stopa.setAttribute('cy', 60); stopa.setAttribute('r', R);
    stopa.setAttribute('class', 'ul-stopa');
    svg.appendChild(stopa);
    const legenda = el('div', 'ul-legenda');
    const vidim = casti.filter((c) => c.bajtu > 0);
    const mezera = vidim.length > 1 ? 1.6 : 0;
    let od = 0;
    const radky = [];
    vidim.forEach((c, i) => {
      const delka = OBVOD * c.bajtu / (celkem || 1);
      const dil = document.createElementNS(NS, 'circle');
      dil.setAttribute('cx', 60); dil.setAttribute('cy', 60); dil.setAttribute('r', R);
      dil.setAttribute('class', 'ul-dil');
      dil.setAttribute('stroke', c.barva);
      dil.setAttribute('stroke-dasharray', Math.max(0.6, delka - mezera) + ' ' + OBVOD);
      dil.setAttribute('stroke-dashoffset', String(-od));
      dil.setAttribute('transform', 'rotate(-90 60 60)');
      const t = document.createElementNS(NS, 'title');
      const pct = celkem ? Math.round(c.bajtu / celkem * 100) : 0;
      t.textContent = `${c.nazev}: ${velikost(c.bajtu)} (${pct} %)`;
      dil.appendChild(t);
      svg.appendChild(dil);
      od += delka;
      const r = el('div', 'ul-leg-radek');
      const znacka = el('span', 'ul-znacka');
      znacka.style.background = c.barva;
      r.appendChild(znacka);
      r.appendChild(el('span', 'ul-leg-nazev', c.nazev));
      r.appendChild(el('span', 'ul-leg-hodnota', velikost(c.bajtu)));
      r.appendChild(el('span', 'ul-leg-pct', pct < 1 ? '<1 %' : pct + ' %'));
      if (c.klik) { r.classList.add('klik'); r.onclick = c.klik; dil.style.cursor = 'pointer'; dil.onclick = c.klik; }
      legenda.appendChild(r);
      radky.push([dil, r]);
      const on = (zap) => {
        wrap.classList.toggle('hover', zap);
        dil.classList.toggle('on', zap);
        r.classList.toggle('on', zap);
      };
      dil.onmouseenter = r.onmouseenter = () => on(true);
      dil.onmouseleave = r.onmouseleave = () => on(false);
    });
    const text = document.createElementNS(NS, 'text');
    text.setAttribute('x', 60); text.setAttribute('y', popisStredu ? 58 : 64);
    text.setAttribute('class', 'ul-stred');
    text.textContent = stred;
    svg.appendChild(text);
    if (popisStredu) {
      const t2 = document.createElementNS(NS, 'text');
      t2.setAttribute('x', 60); t2.setAttribute('y', 73);
      t2.setAttribute('class', 'ul-stred-pod');
      t2.textContent = popisStredu;
      svg.appendChild(t2);
    }
    const graf = el('div', 'ul-graf');
    graf.appendChild(svg);
    wrap.append(graf, legenda);
    return wrap;
  }

  function kolacCloveka(c) {
    const b = barvy();
    const casti = (c.kategorie || []).map((k) => ({nazev: k.nazev, bajtu: k.bajtu, barva: b[KAT_SLOT[k.id] ?? 7]}));
    const celkem = casti.reduce((s, k) => s + k.bajtu, 0);
    return kolac(casti, celkem, velikost(c.pouzito), c.kvota ? 'z ' + velikost(c.kvota) : 'bez limitu');
  }

  /* Ukazatel limitu: plná délka = limit, barva podle stavu (stav je i slovem). */
  function limit(c) {
    const box = el('div', 'ul-limit ' + (c.stav || ''));
    const hlava = el('div', 'ul-limit-hlava');
    hlava.appendChild(el('span', null, c.kvota ? `${velikost(c.pouzito)} z ${velikost(c.kvota)}` : velikost(c.pouzito)));
    hlava.appendChild(el('span', 'ul-stav', (STAV[c.stav] || '') + (c.procent != null ? ' · ' + c.procent + ' %' : '')));
    box.appendChild(hlava);
    if (c.kvota) {
      const track = el('span', 'st-track');
      const fill = el('span', 'st-fill');
      fill.style.width = Math.max(1, Math.min(100, c.procent || 0)) + '%';
      track.appendChild(fill);
      box.appendChild(track);
    }
    return box;
  }

  function slozky(list, max) {
    const box = el('div', 'ul-slozky');
    const top = (list || []).slice(0, max || 8);
    const nej = Math.max(1, ...top.map((s) => s.bajtu));
    const b = barvy();
    for (const s of top) {
      const r = el('div', 'ul-slozka');
      const jm = el('span', 'ul-slozka-jm', '~/' + s.cesta);
      jm.title = '~/' + s.cesta + (s.dni != null ? ' · naposledy změněno před ' + s.dni + ' dny' : '');
      r.appendChild(jm);
      const track = el('span', 'st-track');
      const fill = el('span', 'st-fill');
      fill.style.width = Math.max(1, s.bajtu / nej * 100) + '%';
      fill.style.background = b[KAT_SLOT[s.kat] ?? 7];
      track.appendChild(fill);
      r.appendChild(track);
      r.appendChild(el('span', 'ul-slozka-v', velikost(s.bajtu)));
      box.appendChild(r);
    }
    return box;
  }

  function claudeTab(io, prompt) {
    const st = io.state || {};
    const claude = (st.agents || []).find((a) => a.id === 'claude' && a.path);
    io.openTab({kind: 'project', path: st.home, title: 'Úklid místa',
                agent: claude ? 'claude' : (st.default_agent || ''), prompt});
    if (io.zavrit) io.zavrit();
  }

  /* ── Nastavení → Úložiště ─────────────────────────────────────────────── */
  function uloziste(io) {
    const box = el('div', 'set-sec ul-sec');
    box.appendChild(el('div', 'set-title', 'Úložiště'));
    box.appendChild(el('div', 'set-note',
      'Kolik místa na serveru zabírá tvůj prostor a čím. Počítá se každou noc; „Přepočítat" to spočítá hned. ' +
      'Nic se tu samo nemaže — co jde uvolnit, projdeš s Claudem a on se před smazáním zeptá.'));
    const body = el('div');
    box.appendChild(body);
    let cekam = null;

    async function nacti() {
      let d;
      try { d = await gw('/gw/uloziste'); } catch (e) {
        body.textContent = '';
        body.appendChild(el('div', 'set-note', e.message));
        return;
      }
      if (!box.isConnected && body.childNodes.length) return;
      kresli(d);
      // Rozbor běží → za chvíli znovu.
      clearTimeout(cekam);
      if (d.bezi) cekam = setTimeout(() => { if (box.isConnected) nacti(); }, 4000);
    }

    async function spust(payload, hlaska) {
      try {
        await gw('/gw/uloziste', payload);
        if (hlaska) io.toast(hlaska);
      } catch (e) { io.toast(e.message); }
      nacti();
    }

    function kresli(d) {
      body.textContent = '';
      const ja = d.ja || {};
      const bezi = d.bezi;
      const radek = el('div', 'ul-radek-akci');
      radek.appendChild(el('span', 'set-dim', bezi ? 'Počítám… (může to trvat i minutu)' :
        'Spočítáno ' + kdy(ja.cas || d.cas)));
      const pre = el('button', 'btn ghost', 'Přepočítat');
      pre.disabled = !!bezi;
      pre.onclick = () => spust({akce: 'prepocitat'});
      radek.appendChild(pre);
      body.appendChild(radek);

      if (!ja.zmereno) {
        body.appendChild(el('div', 'set-note', bezi ? 'Tvůj prostor se právě počítá.' :
          'Tvůj prostor se ještě nepočítal. Klikni na Přepočítat.'));
      } else {
        const karta = el('div', 'ul-karta');
        karta.appendChild(kolacCloveka(ja));
        karta.appendChild(limit(ja));
        if (ja.usetreno) {
          karta.appendChild(el('div', 'set-note ul-pozn',
            'Sloučením stejných souborů v mezipaměti s kolegy ušetřeno ' + velikost(ja.usetreno) + ' (do limitu se ti počítá jen tvůj díl).'));
        }
        body.appendChild(karta);
        kresliNalezy(body, ja);
        if ((ja.slozky || []).length) {
          const s = el('div', 'ul-pod');
          s.appendChild(el('div', 'ul-nadpis', 'Největší složky'));
          s.appendChild(slozky(ja.slozky, 8));
          body.appendChild(s);
        }
      }
      if (d.admin) kresliServer(body, d, spust);
    }

    function kresliNalezy(cil, ja) {
      const nalezy = ja.nalezy || [];
      const s = el('div', 'ul-pod');
      s.appendChild(el('div', 'ul-nadpis', 'Co jde uvolnit'));
      if (!nalezy.length) {
        s.appendChild(el('div', 'set-note', 'Nic velkého, co by šlo uvolnit, jsme nenašli.'));
      } else {
        s.appendChild(el('div', 'set-note', ja.lze_uvolnit
          ? 'Bez ztráty čehokoli důležitého jde uvolnit asi ' + velikost(ja.lze_uvolnit) +
            ' — mezipaměti, věci, které se dají znovu nainstalovat, a stejné kopie.'
          : 'Nic, co by šlo smazat bez rozmýšlení. Níž je, co stojí za to projít.'));
        const list = el('div', 'ul-nalezy');
        for (const n of nalezy) {
          const r = el('div', 'ul-nalez' + (n.uvolnit ? ' bezpecne' : ''));
          r.appendChild(el('span', 'ul-nalez-stitek', n.uvolnit ? 'jde uvolnit' : 'ke zvážení'));
          r.appendChild(el('span', 'ul-nalez-text', n.popis));
          list.appendChild(r);
        }
        s.appendChild(list);
      }
      // Bez nálezů a v limitu by Claude neměl co dělat.
      if (ja.prompt && (nalezy.length || ja.stav === 'blizko' || ja.stav === 'nad')) {
        const b = el('button', 'btn primary', 'Uklidit s Claudem');
        b.title = 'Otevře nový chat s Claudem, který to projde a před smazáním se zeptá';
        b.onclick = () => claudeTab(io, ja.prompt);
        const a = el('div', 'set-row ul-akce');
        a.appendChild(b);
        s.appendChild(a);
      }
      cil.appendChild(s);
    }

    function kresliServer(cil, d, spust) {
      const sv = d.server || {};
      const s = el('div', 'ul-pod ul-server');
      s.appendChild(el('div', 'ul-nadpis', 'Celý server'));
      const b = barvy();
      const seda = SEDA[tmavy() ? 'dark' : 'light'];
      const lide = (d.lide || []).filter((r) => r.pouzito);
      const TOP = 5;
      const casti = lide.slice(0, TOP).map((r, i) => ({nazev: r.jmeno || r.email, bajtu: r.pouzito, barva: b[i],
                                                         klik: () => otevriCloveka(r.id)}));
      const zbytek = lide.slice(TOP).reduce((x, r) => x + r.pouzito, 0);
      if (zbytek) casti.push({nazev: 'Další lidé (' + (lide.length - TOP) + ')', bajtu: zbytek, barva: b[5]});
      const spolecne = (sv.spolecne || 0) + (sv.odlozene || 0);
      if (spolecne) casti.push({nazev: 'Společné Obsidiany a smazané účty', bajtu: spolecne, barva: b[6]});
      if (sv.zalohy) casti.push({nazev: 'Zálohy', bajtu: sv.zalohy, barva: seda[1]});
      casti.push({nazev: sv.zalohy ? 'Systém a programy' : 'Systém, programy a zálohy', bajtu: sv.system || 0, barva: seda[0]});
      if (sv.celkem) {
        const k = kolac(casti, sv.celkem, velikost(sv.pouzito), 'z ' + velikost(sv.celkem));
        const volno = el('div', 'ul-leg-radek ul-volno');
        volno.appendChild(el('span', 'ul-znacka ul-znacka-volno'));
        volno.appendChild(el('span', 'ul-leg-nazev', 'Volné místo'));
        volno.appendChild(el('span', 'ul-leg-hodnota', velikost(sv.volno)));
        volno.appendChild(el('span', 'ul-leg-pct', Math.round(sv.volno / sv.celkem * 100) + ' %'));
        k.querySelector('.ul-legenda').appendChild(volno);
        s.appendChild(k);
      } else {
        s.appendChild(el('div', 'set-note', 'Celý server se ještě nepočítal.'));
      }

      // Úklid a oznámení
      const info = [];
      if (d.uklid && d.uklid.cas) {
        info.push(d.uklid.oddelene_ucty ? 'Slučování mezi lidmi je vypnuté (každý prostor má vlastní účet systému).'
          : `Poslední sloučení mezipamětí ${kdy(d.uklid.cas)}: ${pocet(d.uklid.slouceno, 'soubor', 'soubory', 'souborů')}, ` +
            `ušetřeno ${velikost(d.uklid.usetreno)}.`);
      }
      if (d.oznameni_beh && d.oznameni_beh.cas) {
        info.push(`Oznámení o místě naposledy ${kdy(d.oznameni_beh.cas)}: ` +
                  `${pocet(d.oznameni_beh.poslano, 'člověk', 'lidé', 'lidí')}.`);
      }
      info.push('Každou noc se mezipaměti sloučí, místo přepočítá a komu dochází, dostane oznámení.');
      s.appendChild(el('div', 'set-note', info.join(' ')));
      const akce = el('div', 'set-row ul-akce');
      const tl = (text, title, payload, hlaska) => {
        const btn = el('button', 'btn ghost', text);
        btn.title = title;
        btn.disabled = !!d.bezi;
        btn.onclick = () => spust(payload, hlaska);
        akce.appendChild(btn);
      };
      tl('Přepočítat všechny', 'Projde domovy všech lidí znovu (běží na pozadí, chvíli trvá)',
         {akce: 'prepocitat', vse: true}, 'Počítám všechny prostory…');
      tl('Sloučit mezipaměti', 'Stejné soubory v mezipaměti (uv, Playwright, npm…) na disku jen jednou — dokumenty se nemění',
         {akce: 'uklid'}, 'Slučuju mezipaměti — výsledek tu bude za chvíli.');
      tl('Poslat oznámení teď', 'Komu dochází místo nebo má hodně co uvolnit, dostane oznámení (nejvýš jednou za pár dní)',
         {akce: 'rozbor'}, 'Posílám oznámení — kdo co dostal, uvidíš v sekci Oznámení.');
      s.appendChild(akce);

      // Lidé
      s.appendChild(el('div', 'ul-nadpis ul-mezera', 'Lidé'));
      s.appendChild(el('div', 'set-note', `Limit nic nevypíná — od ${Math.round((d.blizko || 0.8) * 100)} % dostane člověk oznámení. ` +
        `Výchozí limit ${velikost(d.kvota_vychozi)}; prázdné pole = výchozí, 0 = bez limitu. Klikni na člověka pro jeho koláč.`));
      const list = el('div', 'ul-lide');
      const detaily = {};
      for (const r of d.lide || []) {
        const row = el('div', 'ul-clovek ' + (r.stav || ''));
        const hlava = el('div', 'ul-clovek-hlava');
        const jm = el('button', 'ul-clovek-jm', r.jmeno || r.email);
        jm.title = r.email;
        hlava.appendChild(jm);
        const track = el('span', 'st-track');
        const fill = el('span', 'st-fill');
        fill.style.width = (r.kvota ? Math.max(1, Math.min(100, r.procent || 0)) : 0) + '%';
        track.appendChild(fill);
        hlava.appendChild(track);
        hlava.appendChild(el('span', 'ul-clovek-v', r.zmereno ? (velikost(r.pouzito) + (r.kvota ? ' / ' + velikost(r.kvota) : '')) : 'nezměřeno'));
        hlava.appendChild(el('span', 'ul-stav', r.zmereno ? STAV[r.stav] || '' : ''));
        const inp = el('input', 'set-input ul-limit-inp');
        inp.type = 'text';
        inp.inputMode = 'decimal';
        inp.placeholder = 'GB';
        inp.title = 'Limit v GB — prázdné = výchozí, 0 = bez limitu';
        inp.value = r.kvota === d.kvota_vychozi ? '' : String(Math.round(r.kvota / GB * 10) / 10).replace('.', ',');
        inp.onchange = async () => {
          try {
            await gw('/gw/uloziste', {akce: 'limit', id: r.id, gb: inp.value.trim()});
            io.toast('Limit pro ' + (r.jmeno || r.email) + ' uložený.');
            nacti();
          } catch (e) { io.toast(e.message); inp.classList.add('chyba'); }
        };
        hlava.appendChild(inp);
        row.appendChild(hlava);
        const pod = [];
        if (r.lze_uvolnit) pod.push('jde uvolnit ' + velikost(r.lze_uvolnit));
        if (r.usetreno) pod.push('sloučením ušetřeno ' + velikost(r.usetreno));
        if (r.oznameni) pod.push('oznámení ' + kdy(r.oznameni.created) + ' — ' + ({neprecteno: 'nepřečtené', precteno: 'přečtené',
          vyreseno: 'řeší s Claudem', zavreno: 'zavřené', nahrazeno: 'nahrazené'}[r.oznameni.stav] || r.oznameni.stav));
        if (pod.length) row.appendChild(el('div', 'ul-clovek-pod', pod.join(' · ')));
        const det = el('div', 'ul-clovek-detail');
        det.hidden = true;
        row.appendChild(det);
        detaily[r.id] = () => {
          det.hidden = !det.hidden;
          row.classList.toggle('otevreny', !det.hidden);
          if (!det.hidden && !det.childNodes.length) {
            if (!r.zmereno) det.appendChild(el('div', 'set-note', 'Prostor se ještě nepočítal.'));
            else {
              det.appendChild(kolacCloveka(r));
              if ((r.slozky || []).length) det.appendChild(slozky(r.slozky, 6));
              if (r.nalezu) det.appendChild(el('div', 'set-note', `Nálezů ${r.nalezu} — podrobně: claude-hub-admin uloziste ${r.email}`));
            }
          }
          if (!det.hidden) row.scrollIntoView({block: 'nearest', behavior: 'smooth'});
        };
        jm.onclick = detaily[r.id];
        list.appendChild(row);
      }
      function otevriCloveka(id) { if (detaily[id]) detaily[id](); }
      s.appendChild(list);
      cil.appendChild(s);
    }

    body.appendChild(el('div', 'set-note', 'Načítám…'));
    nacti();
    return box;
  }

  /* ── Nastavení → Oznámení (admin) ─────────────────────────────────────── */
  const STAVY = {neprecteno: 'nepřečteno', precteno: 'přečteno', vyreseno: 'řeší s Claudem',
                 zavreno: 'zavřeno', nahrazeno: 'nahrazeno novějším'};

  function oznameni(io) {
    const box = el('div', 'set-sec ozn-sec');
    box.appendChild(el('div', 'set-title', 'Oznámení'));
    box.appendChild(el('div', 'set-note',
      'Lidem se oznámení ukáže pod zvonkem v liště. Tlačítko v oznámení otevře nový chat s Claudem a pošle mu zadání — ' +
      'člověk nemusí nic psát. Níž je vidět, kdo co dostal a co s tím udělal.'));
    const body = el('div');
    box.appendChild(body);
    let filtr = 'vse';
    let data = null;

    async function nacti() {
      try { data = await gw('/gw/oznameni/admin'); } catch (e) {
        body.textContent = '';
        body.appendChild(el('div', 'set-note', e.message));
        return;
      }
      kresli();
    }

    function formular() {
      const f = el('div', 'ozn-form');
      f.appendChild(el('div', 'ul-nadpis', 'Poslat oznámení'));
      const komu = el('div', 'ozn-komu');
      const vsichni = el('label', 'ozn-zaskrt');
      const vsichniCb = el('input');
      vsichniCb.type = 'checkbox';
      vsichni.append(vsichniCb, el('span', null, 'Všichni'));
      komu.appendChild(vsichni);
      const cbs = [];
      for (const p of data.lide || []) {
        const l = el('label', 'ozn-zaskrt');
        const cb = el('input');
        cb.type = 'checkbox';
        cb.value = p.id;
        l.append(cb, el('span', null, p.name || p.email));
        l.title = p.email;
        cbs.push(cb);
        komu.appendChild(l);
      }
      vsichniCb.onchange = () => { for (const cb of cbs) { cb.checked = vsichniCb.checked; cb.disabled = vsichniCb.checked; } };
      f.appendChild(komu);
      const pole = (ph, tag) => {
        const i = el(tag || 'input', 'set-input');
        i.placeholder = ph;
        return i;
      };
      const titulek = pole('Nadpis (třeba „V sobotu ráno bude server chvíli stát")');
      titulek.maxLength = 120;
      const text = pole('Text pro lidi', 'textarea');
      text.rows = 3;
      const prompt = pole('Zadání pro Clauda (nepovinné) — s ním dostane oznámení tlačítko, které otevře chat a zadání pošle', 'textarea');
      prompt.rows = 3;
      const tlacitko = pole('Popisek tlačítka (výchozí „Vyřešit s Claudem")');
      tlacitko.maxLength = 40;
      const zav = el('select', 'set-input');
      for (const [v, t] of [['info', 'Informace'], ['varovani', 'Upozornění'], ['vazne', 'Důležité']]) {
        const o = el('option', null, t); o.value = v; zav.appendChild(o);
      }
      f.append(titulek, text, prompt, tlacitko, zav);
      const poslat = el('button', 'btn primary', 'Poslat');
      poslat.onclick = async () => {
        const ids = cbs.filter((cb) => cb.checked).map((cb) => Number(cb.value));
        if (!vsichniCb.checked && !ids.length) return io.toast('Vyber, komu oznámení poslat.');
        if (!titulek.value.trim()) return io.toast('Oznámení potřebuje nadpis.');
        poslat.disabled = true;
        try {
          const r = await gw('/gw/oznameni/admin', {komu: vsichniCb.checked ? 'vsichni' : ids, titulek: titulek.value,
            text: text.value, prompt: prompt.value, tlacitko: tlacitko.value, zavaznost: zav.value});
          io.toast('Posláno ' + r.poslano + (r.poslano === 1 ? ' člověku.' : ' lidem.'));
          nacti();
        } catch (e) { io.toast(e.message); poslat.disabled = false; }
      };
      const a = el('div', 'set-row ul-akce');
      a.appendChild(poslat);
      f.appendChild(a);
      return f;
    }

    function kresli() {
      body.textContent = '';
      body.appendChild(formular());
      const z = el('div', 'ozn-zaznam');
      const hlava = el('div', 'ozn-zaznam-hlava');
      hlava.appendChild(el('div', 'ul-nadpis', 'Kdo co dostal'));
      const seg = el('div', 'set-seg');
      for (const [v, t] of [['vse', 'Vše'], ['uloziste', 'Místo na disku'], ['zprava', 'Od adminů']]) {
        const b = el('button', 'set-seg-btn' + (filtr === v ? ' on' : ''), t);
        b.onclick = () => { filtr = v; kresli(); };
        seg.appendChild(b);
      }
      hlava.appendChild(seg);
      z.appendChild(hlava);
      const jmena = {};
      for (const p of data.lide || []) jmena[p.id] = p.name || p.email;
      const list = (data.oznameni || []).filter((o) => filtr === 'vse' || o.kind === filtr);
      if (!list.length) z.appendChild(el('div', 'set-note', 'Zatím žádná oznámení.'));
      for (const o of list.slice(0, 200)) {
        const r = el('div', 'ozn-z-radek ' + o.stav);
        const h = el('button', 'ozn-z-hlava');
        h.appendChild(el('span', 'ozn-z-kdy', kdy(o.created)));
        h.appendChild(el('span', 'ozn-z-komu', jmena[o.uid] || o.email));
        h.appendChild(el('span', 'ozn-z-titulek', o.title));
        h.appendChild(el('span', 'ozn-z-stav', STAVY[o.stav] || o.stav));
        r.appendChild(h);
        const det = el('div', 'ozn-z-detail');
        det.hidden = true;
        const meta = ['Od: ' + (o.sender || 'Systém')];
        if (o.read_at) meta.push('přečteno ' + kdy(o.read_at));
        if (o.acted_at) meta.push('chat s Claudem ' + kdy(o.acted_at));
        if (o.dismissed_at) meta.push((o.replaced_by ? 'nahrazeno ' : 'zavřeno ') + kdy(o.dismissed_at));
        if (o.kind === 'uloziste' && o.meta && o.meta.pouzito != null) {
          meta.push('tehdy zabíral(a) ' + velikost(o.meta.pouzito) +
                    (o.pouzito_ted != null ? ', teď ' + velikost(o.pouzito_ted) : ''));
          meta.push(o.meta.claude ? 'text napsal Claudík' : 'text ze šablony');
        }
        det.appendChild(el('div', 'set-dim', meta.join(' · ')));
        if (o.text) det.appendChild(el('div', 'ozn-z-text', o.text));
        if (o.action && o.action.prompt) {
          det.appendChild(el('div', 'set-dim', 'Zadání pro Clauda (tlačítko „' + (o.action.label || 'Vyřešit s Claudem') + '"):'));
          det.appendChild(el('pre', 'ozn-z-prompt', o.action.prompt));
        }
        r.appendChild(det);
        h.onclick = () => { det.hidden = !det.hidden; r.classList.toggle('otevreny', !det.hidden); };
        z.appendChild(r);
      }
      body.appendChild(z);
    }

    body.appendChild(el('div', 'set-note', 'Načítám…'));
    nacti();
    return box;
  }

  global.HubUloziste = {uloziste, oznameni, velikost};
})(window);
