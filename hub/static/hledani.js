/* Hledání v textech konverzací + export do Markdownu (server: hub/hledani.py).
   Samostatný blok pod políčkem „Hledat chat…": hledá se na serveru na pozadí,
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
    hlava.onclick = () => io.openChat(chat);
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

  function mount(opts) {
    io = opts;
    const wrap = document.querySelector('#chats-view .searchwrap');
    const seznam = document.getElementById('chats');
    if (!wrap || !seznam || document.getElementById('hledani-blok')) return;
    const blok = el('div', 'hl-blok');
    blok.id = 'hledani-blok';
    const toggle = el('button', 'hl-toggle', 'Hledat v textu konverzací');
    toggle.type = 'button';
    const panel = el('div', 'hl-panel');
    panel.hidden = true;
    const pole = el('input', 'hl-input');
    pole.type = 'search';
    pole.placeholder = 'Hledat ve zprávách (min. 2 znaky)…';
    pole.autocomplete = 'off';
    pole.maxLength = 200;
    const lab = el('label', 'hl-nastroje');
    const cb = el('input');
    cb.type = 'checkbox';
    lab.append(cb, document.createTextNode(' i ve výpisech nástrojů (pomalejší)'));
    const stav = el('div', 'hl-stav');
    const vysl = el('div', 'hl-vysledky');
    panel.append(pole, lab, stav, vysl);
    blok.append(toggle, panel);
    wrap.after(blok);

    function zrus() {
      seq++;
      clearTimeout(casovac);
      clearTimeout(dotaz);
    }
    function zavri() {
      zrus();
      panel.hidden = true;
      seznam.hidden = false;
      toggle.textContent = 'Hledat v textu konverzací';
      io.api('chat-hledej-zrus', {}).catch(() => {});
    }
    async function hledej() {
      zrus();
      const moje = seq;
      const q = pole.value.trim();
      vysl.textContent = '';
      if (q.length < 2) {
        stav.textContent = q ? 'Napiš aspoň 2 znaky.' : '';
        return;
      }
      stav.textContent = 'Hledám…';
      let job;
      try {
        job = (await io.api('chat-hledej', {q, nastroje: cb.checked})).job;
      } catch (err) {
        if (moje === seq) stav.textContent = 'Hledání nejde: ' + err.message;
        return;
      }
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
        vysl.textContent = '';
        for (const r of s.vysledky || []) vysl.appendChild(radek(r));
        const kolik = (s.vysledky || []).length;
        if (s.chyba) { stav.textContent = s.chyba; return; }
        if (!s.hotovo) {
          stav.textContent = `Hledám… (${s.prohledano}/${s.celkem || '?'} konverzací, nalezeno ${s.nalezeno})`;
          casovac = setTimeout(ptej, 350);
          return;
        }
        stav.textContent = kolik
          ? `Nalezeno ${s.nalezeno}× v ${kolik} konverzacích.` + (s.castecne ? ' Výsledků je moc nebo hledání trvalo dlouho — zpřesni dotaz.' : '')
          : (s.castecne ? 'Nic jsem nenašel, ale hledání nestihlo projít vše.' : 'Nic takového jsem nenašel.');
      };
      ptej();
    }
    toggle.onclick = () => {
      if (!panel.hidden) return zavri();
      panel.hidden = false;
      seznam.hidden = true;
      toggle.textContent = 'Zpět na seznam chatů';
      pole.value = document.getElementById('chats-search').value.trim();
      pole.focus();
      if (pole.value.length >= 2) hledej();
    };
    pole.addEventListener('input', () => { zrus(); dotaz = setTimeout(hledej, 450); });
    pole.addEventListener('keydown', (ev) => {
      if (ev.key === 'Enter') { zrus(); hledej(); }
      if (ev.key === 'Escape') zavri();
    });
    cb.addEventListener('change', hledej);
  }

  global.HubHledani = {mount, exportovat: (c) => io && exportovat(c)};
})(window);
