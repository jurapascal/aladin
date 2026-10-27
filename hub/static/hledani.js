/* Hledání v textech konverzací + export do Markdownu (server: hub/hledani.py).
   Okno hledání (otevri): hledá se na serveru na pozadí,
   stránka se jen průběžně ptá na stav. Nový dotaz starý na serveru zruší.
   Úryvky i zvýraznění se kreslí přes textNode / <mark>, nikdy jako HTML. */
(function (global) {
  'use strict';
  let io = null;             // {api, toast, openChat, chats, token}
  let seq = 0;               // číslo dotazu — odpověď staršího se zahodí
  let casovac = 0, dotaz = 0;

  function el(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined) n.textContent = text;
    return n;
  }
  const kdy = (t) => {
    const d = new Date(t * 1000);
    return d.toLocaleDateString('cs-CZ', {day: 'numeric', month: 'numeric', year: '2-digit'}) + ' '
      + d.toLocaleTimeString('cs-CZ', {hour: '2-digit', minute: '2-digit'});
  };

  /* Stažení .md: nejdřív fetch (chyba serveru se ukáže větou, ne souborem
     s JSONem), pak <a download> na blob — funguje i v telefonu. */
  const exportuje = new Set();
  async function exportovat(chat) {
    if (!chat || !chat.id || exportuje.has(chat.id)) return;
    exportuje.add(chat.id);
    io.toast('Připravuju export…');
    try {
      const url = `/api/chat-export?id=${encodeURIComponent(chat.id)}&t=${encodeURIComponent(io.token)}`;
      const res = await fetch(url, {credentials: 'same-origin'});
      if (!res.ok) {
        let msg = 'Export se nepovedl.';
        try { msg = (await res.json()).error || msg; } catch (_) { /* není JSON */ }
        return io.toast(msg);
      }
      const blob = await res.blob();
      let jmeno = 'konverzace.md';
      const m = /filename\*=UTF-8''([^;]+)/i.exec(res.headers.get('Content-Disposition') || '');
      if (m) { try { jmeno = decodeURIComponent(m[1]); } catch (_) { /* nechat výchozí */ } }
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = jmeno;
      a.rel = 'noopener';
      document.body.appendChild(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(a.href), 30000);
      io.toast('Export uložen: ' + jmeno);
    } catch (err) {
      io.toast('Export se nepovedl: spojení s hubem selhalo.');
    } finally {
      exportuje.delete(chat.id);
    }
  }

  function uryvek(n) {
    const p = el('div', 'hl-uryvek');
    p.appendChild(el('span', 'hl-role', n.role === 'me' ? 'Ty' : n.role === 'claude' ? 'Claude' : 'Nástroj'));
    let kam = 0;
    if (n.pred) p.appendChild(document.createTextNode('… '));
    for (const [od, delka] of n.ranges || []) {
      if (od < kam) continue;
      p.appendChild(document.createTextNode(n.text.slice(kam, od)));
      p.appendChild(el('mark', 'hl-mark', n.text.slice(od, od + delka)));
      kam = od + delka;
    }
    p.appendChild(document.createTextNode(n.text.slice(kam) + (n.po ? ' …' : '')));
    return p;
  }

  function radek(r) {
    const znamy = (io.chats() || []).find((c) => c.id === r.id)
      || {id: r.id, title: r.title, project: r.project, cwd: r.cwd, exists: r.exists, updated: r.updated, prompt: ''};
    // `tab` (běží v tabu) se mění — bere se z čerstvého výsledku hledání.
    const chat = Object.assign({}, znamy, {tab: r.tab || znamy.tab});
    const box = el('div', 'hl-vysledek');
    const hlava = el('button', 'hl-hlava');
    hlava.type = 'button';
    hlava.append(el('span', 'hl-titulek', r.title),
      el('span', 'hl-meta', [r.project, kdy(r.updated), r.pocet + '×', r.tab ? 'otevřená' : ''].filter(Boolean).join('  ·  ')));
    hlava.onclick = () => { if (zavriPopup) zavriPopup(); io.openChat(chat); };
    box.appendChild(hlava);
    for (const n of r.nalezy || []) {
      const u = uryvek(n);
      u.onclick = hlava.onclick;
      box.appendChild(u);
    }
    const ex = el('button', 'hl-export', 'Exportovat (.md)');
    ex.type = 'button';
    ex.title = 'Stáhne celou konverzaci jako Markdown';
    ex.onclick = (ev) => { ev.stopPropagation(); exportovat(chat); };
    box.appendChild(ex);
    return box;
  }

  const fold = (t) => String(t || '').normalize('NFD').replace(/\p{M}/gu, '').toLowerCase();
  let zavriPopup = null;

  function sekce(nadpis) {
    const h = el('div', 'hl-sekce', nadpis);
    h.hidden = true;
    return h;
  }
  function polozka(titulek, meta, onclick) {
    const b = el('button', 'hl-polozka');
    b.type = 'button';
    b.append(el('span', 'hl-titulek', titulek), el('span', 'hl-meta', meta));
    b.onclick = onclick;
    return b;
  }

  /* Okno hledání: chaty a projekty podle názvu hned, texty konverzací na
     serveru na pozadí. Otevírá ho „Hledat" v panelu i lupa na telefonu. */
  function otevri() {
    if (!io || zavriPopup) return;
    const modal = el('div', 'modal hl-modal');
    const box = el('div', 'modal-box hl-popup');
    const pole = el('input', 'hl-input');
    pole.type = 'search';
    pole.placeholder = 'Hledat chaty, projekty a konverzace…';
    pole.autocomplete = 'off';
    pole.maxLength = 200;
    pole.setAttribute('enterkeyhint', 'search');
    const telo = el('div', 'hl-telo');
    const sChaty = sekce('Chaty'), lChaty = el('div', 'hl-seznam');
    const sProj = sekce('Projekty'), lProj = el('div', 'hl-seznam');
    const sText = sekce('V textu konverzací'), stav = el('div', 'hl-stav'), vysl = el('div', 'hl-vysledky');
    telo.append(sChaty, lChaty, sProj, lProj, sText, stav, vysl);
    box.append(pole, telo);
    modal.appendChild(box);
    document.body.appendChild(modal);

    function zrus() { seq++; clearTimeout(casovac); clearTimeout(dotaz); }
    function zavri() {
      zrus();
      io.api('chat-hledej-zrus', {}).catch(() => {});
      document.removeEventListener('keydown', naKlavesu, true);
      modal.remove();
      zavriPopup = null;
    }
    zavriPopup = zavri;
    function naKlavesu(ev) { if (ev.key === 'Escape') { ev.stopPropagation(); zavri(); } }
    document.addEventListener('keydown', naKlavesu, true);
    modal.addEventListener('mousedown', (ev) => { if (ev.target === modal) zavri(); });

    function podleNazvu() {
      const slova = fold(pole.value.trim()).split(/\s+/).filter(Boolean);
      lChaty.textContent = '';
      lProj.textContent = '';
      let chaty = [], projekty = [];
      if (slova.length) {
        const shoda = (txt) => slova.every((w) => fold(txt).includes(w));
        chaty = (io.chats() || []).filter((c) => shoda([c.title, c.prompt, c.project, c.posledni].join(' ')))
          .sort((a, b) => b.updated - a.updated).slice(0, 8);
        projekty = (io.projects() || []).filter((p) => !p.archived && shoda([p.label, p.name, p.group].join(' '))).slice(0, 6);
      }
      for (const c of chaty) {
        lChaty.appendChild(polozka(c.title || '(bez názvu)', [c.project, c.updated ? kdy(c.updated) : ''].filter(Boolean).join('  ·  '),
          () => { zavri(); io.openChat(c); }));
      }
      for (const p of projekty) {
        lProj.appendChild(polozka(p.label || p.name, [p.type, p.branch].filter(Boolean).join('  ·  '),
          () => { zavri(); io.openProject(p); }));
      }
      sChaty.hidden = !chaty.length;
      sProj.hidden = !projekty.length;
    }

    async function hledej() {
      zrus();
      const moje = seq;
      const q = pole.value.trim();
      vysl.textContent = '';
      stav.textContent = '';
      sText.hidden = q.length < 2;
      if (q.length < 2) return;
      stav.textContent = 'Hledám…';
      // Nejdřív texty konverzací (rychle), potom samo i výpisy nástrojů (pomalejší).
      const faze = async (nastroje) => {
        let job;
        try {
          job = (await io.api('chat-hledej', {q, nastroje})).job;
        } catch (err) {
          if (moje === seq) stav.textContent = 'Hledání nejde: ' + err.message;
          return;
        }
        const popis = nastroje ? ' ve výpisech nástrojů' : '';
        const ptej = async () => {
          if (moje !== seq) return;
          let s;
          try {
            s = await io.api('chat-hledej-stav', {job});
          } catch (err) {
            if (moje === seq) stav.textContent = 'Hledání nejde: ' + err.message;
            return;
          }
          if (moje !== seq || s.nahrazeno) return;
          // Druhá fáze nahradí první, až má co ukázat (je to nadmnožina).
          if (!nastroje || s.hotovo || (s.vysledky || []).length) {
            vysl.textContent = '';
            for (const r of s.vysledky || []) vysl.appendChild(radek(r));
          }
          const kolik = (s.vysledky || []).length;
          if (s.chyba) { stav.textContent = s.chyba; return; }
          if (!s.hotovo) {
            stav.textContent = `Hledám${popis}… (${s.prohledano}/${s.celkem || '?'} konverzací, nalezeno ${s.nalezeno})`;
            casovac = setTimeout(ptej, 350);
            return;
          }
          if (!nastroje) {
            if (kolik) stav.textContent = `Nalezeno ${s.nalezeno}× v ${kolik} konverzacích. Hledám i ve výpisech nástrojů…`;
            else stav.textContent = 'Hledám i ve výpisech nástrojů…';
            faze(true);
            return;
          }
          stav.textContent = kolik
            ? `Nalezeno ${s.nalezeno}× v ${kolik} konverzacích.` + (s.castecne ? ' Výsledků je moc nebo hledání trvalo dlouho — zpřesni dotaz.' : '')
            : (s.castecne ? 'Nic jsem nenašel, ale hledání nestihlo projít vše.' : 'Nic takového jsem nenašel.');
        };
        ptej();
      };
      faze(false);
    }
    pole.addEventListener('input', () => {
      zrus();
      podleNazvu();
      sText.hidden = pole.value.trim().length < 2;
      if (pole.value.trim().length < 2) { vysl.textContent = ''; stav.textContent = ''; return; }
      dotaz = setTimeout(hledej, 450);
    });
    pole.addEventListener('keydown', (ev) => { if (ev.key === 'Enter') { zrus(); hledej(); } });
    pole.focus();
  }

  function mount(opts) { io = opts; }

  global.HubHledani = {mount, otevri, exportovat: (c) => io && exportovat(c)};
})(window);
