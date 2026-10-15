/* Nastavení — to, co průvodce nastaví napoprvé, se tu dá změnit kdykoli.
 *
 * Sekce se přepínají tlačítky vlevo a v těle je vždycky jen jedna. Jako jeden
 * dlouhý svitek se v tom ztrácelo: kvůli přepínači tabů se scrollovalo přes
 * paměť, napojení i logy. Vybraná sekce se pamatuje, takže po zavření a
 * otevření je člověk tam, kde skončil.
 *
 * Je tu i aktualizace aplikace. Schválně daleko od ⟳ v hlavičce: to jen znovu
 * přečte projekty a paměť, kdežto tohle stáhne novou verzi hubu a přeinstaluje
 * ji. Dvě různé věci, dvě různá místa, dvě různá jména.
 */
'use strict';

(function (global) {

  let io = null;
  let state = null;
  let root = null;
  let active = localStorage.getItem('hub-set-tab') || 'vzhled';
  // Propojené služby a Pluginy jsou od 2.57.2 jedna sekce.
  if (active === 'napojeni' || active === 'pluginy') active = 'rozsireni';

  // Pořadí je i pořadím v panelu vlevo: napřed to, co se mění nejčastěji,
  // servis (aktualizace, logy) až na konci.
  const SECTIONS = [
    ['vzhled',     'Vzhled',    'i-sun',      () => vzhled()],
    // `dev` = jen ve vývojářském režimu. Kdo hub používá na psaní s agentem,
    // neřeší složky s projekty, zálohu paměti ani tlačítka nových tabů.
    ['projekty',   'Projekty',  'i-folder',   () => projekty(), 'dev'],
    ['taby',       'Taby',      'i-terminal', () => taby(), 'dev'],
    // Bez vývojářského režimu jen modely přes API (Jev) — CLI agenti ne.
    ['agenti',     'AI agenti', 'i-hub',      () => agenti()],
    ['pamet',      'Paměť',     'i-book',     () => pamet(), 'dev'],
    ['ucet',       'Účet',      'i-user',     () => ucet()],
    // Firmy, lidé a přístupy ke klientům (gateway/firmy.py) — jen na bráně.
    ['firmy',      'Firmy',     'i-folder',   () => firmy(), 'server'],
    ['hlas',       'Hlas',      'i-mic',      () => hlas()],
    // Všechno, čím se Claude rozšiřuje — služby, pluginy, skilly, MCP, pluginy appky.
    ['rozsireni',  'Napojení a pluginy', 'i-hub', () => rozsireni()],
    ['aktualizace','Aktualizace', 'i-up',     () => aktualizace()],
    ['logy',       'Logy',      'i-status',   () => logy(), 'dev'],
    ['ostatni',    'Ostatní',   'i-gear',     () => ostatni()],
  ];

  function el(tag, cls, text) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text != null) node.textContent = text;
    return node;
  }

  function section(title, note) {
    const box = el('div', 'set-sec');
    box.appendChild(el('div', 'set-title', title));
    if (note) box.appendChild(el('div', 'set-note', note));
    return box;
  }

  async function open(opts) {
    io = opts;
    state = opts.state;
    if (opts.tab === 'napojeni' || opts.tab === 'pluginy') opts.tab = 'rozsireni';
    if (opts.tab && SECTIONS.some(([id]) => id === opts.tab)) active = opts.tab;
    root = el('div', 'onb set-modal');
    root.innerHTML = `
      <div class="onb-box">
        <div class="onb-head">
          <span class="onb-mark"></span>
          <div>
            <div class="onb-title">Nastavení</div>
            <div class="onb-sub"></div>
          </div>
        </div>
        <div class="onb-body set-body">
          <nav class="set-nav"></nav>
          <div class="set-panel"></div>
        </div>
        <div class="onb-foot">
          <button class="btn ghost set-wizard">Spustit průvodce znovu</button>
          <span class="spacer"></span>
          <button class="btn primary set-close">Zavřít</button>
        </div>
      </div>`;
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('viewBox', '0 0 24 24');
    svg.setAttribute('width', '34');
    svg.setAttribute('height', '34');
    const use = document.createElementNS('http://www.w3.org/2000/svg', 'use');
    use.setAttribute('href', '#i-hub');
    svg.appendChild(use);
    root.querySelector('.onb-mark').appendChild(svg);
    root.querySelector('.onb-sub').textContent = 'Verze ' + state.version.version;
    root.querySelector('.set-close').onclick = close;
    root.querySelector('.set-wizard').onclick = () => {
      close();
      io.openWizard();
    };
    // Zavřít jen klikem, který na pozadí i začal: výběr textu tažením ven
    // z okna končí puštěním tlačítka na pozadí a prohlížeč to hlásí jako klik.
    let downOutside = false;
    root.addEventListener('pointerdown', (ev) => { downOutside = ev.target === root; });
    root.addEventListener('click', (ev) => { if (ev.target === root && downOutside) close(); });
    document.body.appendChild(root);
    render();
  }

  /* Sekce, které se teď mají ukazovat.
   *
   * Filtruje se až při kreslení, ne jednou při načtení: režim se přepíná
   * v nastavení samotném a panel se musí překreslit hned, ne po restartu. */
  function visibleSections() {
    const dev = advanced();
    const u = (state && state.config && state.config.gateway_user) || null;
    const admin = !!(u && u.role === 'admin');
    return SECTIONS.filter(([, , , , flag]) => (flag !== 'dev' || dev) && (flag !== 'admin' || admin)
                                              && (flag !== 'server' || !!u));
  }

  // Pro pokročilé (config.dev_mode) — jinak jednoduchý režim bez techniky.
  const advanced = () => !!(state && state.config && state.config.dev_mode);

  function render() {
    const nav = root.querySelector('.set-nav');
    const panel = root.querySelector('.set-panel');
    const shown = visibleSections();
    // Vypnutím režimu může zmizet sekce, ve které uživatel zrovna stojí.
    if (!shown.some(([id]) => id === active)) active = shown[0][0];

    nav.textContent = '';
    for (const [id, label, ico] of shown) {
      const b = el('button', 'set-tab' + (id === active ? ' on' : ''));
      b.innerHTML = '<svg class="ico"><use href="#' + ico + '"/></svg><span></span>';
      b.querySelector('span').textContent = label;
      b.onclick = () => {
        active = id;
        localStorage.setItem('hub-set-tab', id);
        render();
        panel.scrollTop = 0;
      };
      nav.appendChild(b);
    }

    panel.textContent = '';
    const build = (shown.find(([id]) => id === active) || shown[0])[3];
    panel.appendChild(build());
  }

  /* Firmy (gateway/firmy.py): kde jsem a s jakou rolí, založení firmy a pro
     kapitány lidé, klienti, podkategorie a kdo kam vidí. Rozhoduje brána —
     tady se jen kreslí a posílá; změny se v prostorech projeví po restartu. */
  function firmy() {
    const box = section('Firmy',
      'Každá firma má vlastní Obsidian, skilly a lidi. Obecné věci firmy vidí každý její člen; ' +
      'klienty a jejich podkategorie (Web, Marketing…) jen ti, komu je kapitán zpřístupní.');
    box.classList.add('firmy-sec');
    const body = el('div');
    box.appendChild(body);
    const ROLE = {kapitan: 'kapitán', clen: 'člen', host: 'host'};
    const gw = async (payload) => {
      const r = await fetch('/gw/firmy', payload === undefined ? {credentials: 'same-origin'} : {
        method: 'POST', credentials: 'same-origin',
        headers: {'Content-Type': 'application/json', 'X-Hub-Account': '1'},
        body: JSON.stringify(payload),
      });
      const data = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(data.error || 'Server teď neodpovídá, zkus to za chvíli.');
      return data;
    };
    const run = async (payload, done) => {
      try {
        const out = await gw(payload);
        if (done) io.toast(out.message || done);
        await kresli();
        return out;
      } catch (e) { io.toast(e.message); return null; }
    };
    const input = (placeholder, type) => {
      const i = el('input', 'set-input');
      i.placeholder = placeholder;
      if (type) i.type = type;
      return i;
    };
    const select = (options, value) => {
      const sel = el('select', 'set-input');
      for (const [v, label] of options) {
        const o = el('option', null, label);
        o.value = v;
        o.selected = v === value;
        sel.appendChild(o);
      }
      return sel;
    };
    const roleOptions = [['kapitan', 'kapitán'], ['clen', 'člen'], ['host', 'host (jen čte)']];
    const DRUHY = [['klienti', 'pracuje pro klienty (Klienti › klient › podkategorie)'],
                   ['projekty', 'pracuje pro sebe (rovnou projekty)']];

    async function kresli() {
      let d;
      try { d = await gw(); } catch (e) { body.textContent = e.message; return; }
      body.textContent = '';
      if (d.zmena) {
        const row = el('div', 'set-row firmy-zmena');
        row.appendChild(el('span', 'set-warn', 'Přístupy se změnily — v prostoru se projeví po restartu.'));
        const b = el('button', 'btn primary', 'Restartovat prostor');
        b.onclick = () => (typeof global.restartSpace === 'function'
          ? global.restartSpace('Načítám změněné firmy a přístupy…') : io.reload());
        row.appendChild(b);
        body.appendChild(row);
      }
      // Moje firmy.
      if (!d.firmy.length) body.appendChild(el('div', 'set-note', 'Zatím nejsi v žádné firmě.'));
      for (const f of d.firmy) {
        const row = el('div', 'set-row');
        row.appendChild(el('span', null, f.nazev));
        row.appendChild(el('small', null, ROLE[f.role] + (f.role === 'clen' && f.mazat ? ' · smí mazat' : '')));
        body.appendChild(row);
      }
      if (d.zakladat) {
        // Druh firmy (gateway/firmy.py DRUHY): agentura má klienty a pod nimi
        // podkategorie, firma pro sebe rovnou projekty.
        const row = el('div', 'set-row');
        const name = input('Název nové firmy');
        const druh = select(DRUHY, 'klienti');
        druh.classList.add('firmy-druh');
        const b = el('button', 'btn primary', 'Založit firmu');
        b.onclick = () => name.value.trim()
          ? run({akce: 'zalozit', nazev: name.value.trim(), druh: druh.value}, 'Firma je založená.')
          : io.toast('Vyplň název firmy.');
        row.append(name, druh, b);
        body.appendChild(row);
      }
      for (const [slug, p] of Object.entries(d.prehled || {})) {
        body.appendChild(kapitan(slug, p, d.ucty || []));
      }
    }

    // Správa jedné firmy pro jejího kapitána.
    function kapitan(slug, p, ucty) {
      const sec = el('div', 'firmy-firma');
      sec.appendChild(el('div', 'set-title', p.nazev + ' — správa'));
      // Název firmy.
      const nr = el('div', 'set-row');
      const nazev = input('Název firmy');
      nazev.value = p.nazev;
      const nb = el('button', 'btn ghost', 'Přejmenovat');
      nb.onclick = () => run({akce: 'nazev', firma: slug, nazev: nazev.value.trim()}, 'Přejmenováno.');
      nr.append(nazev, nb);
      sec.appendChild(nr);
      // Druh firmy jde změnit, jen dokud v ní nejsou klienti / projekty (brána).
      const dr = el('div', 'set-row');
      const druh = select(DRUHY, p.druh || 'klienti');
      druh.classList.add('firmy-druh');
      druh.disabled = Object.keys(p.klienti || {}).length > 0;
      druh.title = druh.disabled ? 'Druh jde změnit, jen dokud je firma bez klientů a projektů.' : '';
      druh.onchange = () => run({akce: 'druh', firma: slug, druh: druh.value}, 'Druh firmy změněn.');
      dr.append(el('span', null, 'Firma'), druh);
      sec.appendChild(dr);
      if (slug === 'firma') {
        const kr = el('label', 'set-row');
        const cb = el('input');
        cb.type = 'checkbox';
        cb.checked = !!p.klienti_zapnute;
        cb.onchange = () => run({akce: 'klienti', firma: slug, zapnout: cb.checked}, 'Uloženo.');
        kr.append(cb, el('span', null, 'Klienti a podkategorie s přístupy (složka Klienti/ je pak vidět jen s přístupem)'));
        sec.appendChild(kr);
      }

      // Lidé a role.
      sec.appendChild(el('div', 'set-title firmy-sub', 'Lidé'));
      const members = new Set(p.lide.map((m) => m.id));
      for (const m of p.lide) {
        const row = el('div', 'set-row');
        row.appendChild(el('span', null, (m.name || m.email) + (m.name ? ' · ' + m.email : '')));
        const sel = select([...roleOptions, ['', 'odebrat z firmy']], m.role);
        sel.onchange = () => run({akce: 'clovek', firma: slug, id: m.id, role: sel.value,
                                  mazat: m.mazat}, sel.value ? 'Role změněna.' : 'Odebráno z firmy.');
        row.appendChild(sel);
        if (m.role === 'clen') {
          const lab = el('label', 'firmy-mazat');
          const cb = el('input');
          cb.type = 'checkbox';
          cb.checked = !!m.mazat;
          cb.onchange = () => run({akce: 'mazani', firma: slug, id: m.id, ano: cb.checked}, 'Uloženo.');
          lab.append(cb, el('span', null, 'smí mazat'));
          row.appendChild(lab);
        }
        sec.appendChild(row);
      }
      const others = ucty.filter((u) => !members.has(u.id));
      if (others.length) {
        const row = el('div', 'set-row');
        const who = select(others.map((u) => [String(u.id), (u.name || u.email) + (u.name ? ' · ' + u.email : '')]));
        const role = select(roleOptions, 'clen');
        const b = el('button', 'btn primary', 'Přidat do firmy');
        b.onclick = () => run({akce: 'clovek', firma: slug, id: Number(who.value), role: role.value},
                              'Přidáno do firmy.');
        row.append(who, role, b);
        sec.appendChild(row);
      }
      // Pozvat někoho, kdo na serveru účet ještě nemá.
      const pr = el('div', 'set-row');
      const email = input('e-mail nového člověka', 'email');
      const jmeno = input('jméno a příjmení');
      const prole = select(roleOptions, 'clen');
      const pb = el('button', 'btn ghost', 'Pozvat');
      const heslo = el('div', 'set-note');
      pb.onclick = async () => {
        if (!email.value.trim()) return io.toast('Vyplň e-mail.');
        const out = await run({akce: 'pozvat', firma: slug, email: email.value.trim(),
                               jmeno: jmeno.value.trim(), role: prole.value});
        if (out && out.heslo) {
          heslo.textContent = `Účet ${out.email} je založený. Heslo (ukáže se jen teď, předej ho): ${out.heslo}` +
            ' — při prvním přihlášení si nastaví ověření v telefonu.';
          sec.appendChild(heslo);
        }
      };
      pr.append(email, jmeno, prole, pb);
      sec.appendChild(pr);

      // Klienti, podkategorie a kdo kam vidí.
      if (p.klienti_zapnute) sec.appendChild(klienti(slug, p));
      // Smazat firmu (dnešní firemní Obsidian ne): odloží se na serveru stranou.
      if (slug !== 'firma') {
        const dr = el('div', 'set-row firmy-smazat');
        const del = el('button', 'btn ghost', 'Smazat firmu');
        del.title = 'Firma zmizí všem jejím lidem; na serveru se odloží stranou a správce ji umí vrátit.';
        del.onclick = () => { if (confirmTwice(del)) run({akce: 'smazat-firmu', firma: slug}, 'Firma je smazaná.'); };
        dr.appendChild(del);
        sec.appendChild(dr);
      }
      return sec;
    }

    function klienti(slug, p) {
      if (p.druh === 'projekty') return projekty(slug, p);
      const wrap = el('div');
      wrap.appendChild(el('div', 'set-title firmy-sub', 'Klienti a kdo kam vidí'));
      wrap.appendChild(el('div', 'set-note',
        'Zaškrtni, kdo vidí klienta (jeho obecné věci) a kdo jednotlivé podkategorie. ' +
        'Podkategorie zpřístupní i klienta. Kapitáni vidí všechno vždycky.'));
      const lidi = p.lide.filter((m) => m.role !== 'kapitan');
      const pristup = (rel, id) => (p.pristupy[rel] || []).includes(id);
      for (const [klient, podkategorie] of Object.entries(p.klienti)) {
        const kRel = 'Klienti/' + klient;
        const k = el('div', 'firmy-klient');
        const head = el('div', 'set-row');
        head.appendChild(el('strong', null, klient));
        const nova = input('nová podkategorie (Web, Marketing…)');
        const nb = el('button', 'btn ghost', 'Přidat');
        nb.onclick = () => nova.value.trim()
          ? run({akce: 'slozka', firma: slug, klient, podkategorie: nova.value.trim()}, 'Podkategorie je založená.')
          : io.toast('Vyplň název podkategorie.');
        const ren = el('button', 'btn ghost', 'Přejmenovat');
        // Bez dialogů prohlížeče: nový název se píše do pole vedle.
        ren.title = 'Nový název klienta napiš do pole vedle';
        ren.onclick = () => (nova.value.trim()
          ? run({akce: 'prejmenovat', firma: slug, cesta: kRel, nazev: nova.value.trim()}, 'Přejmenováno.')
          : io.toast('Nový název napiš do pole vedle a klikni znovu.'));
        const del = el('button', 'btn ghost', 'Odložit');
        del.title = 'Složka klienta se odloží mimo Obsidian firmy (jde vrátit ze serveru).';
        del.onclick = () => { if (confirmTwice(del)) run({akce: 'smazat', firma: slug, cesta: kRel}, 'Klient je odložený.'); };
        head.append(nova, nb, ren, del);
        k.appendChild(head);
        if (lidi.length) {
          const table = el('table', 'firmy-tabulka');
          const tr = el('tr');
          tr.appendChild(el('th', null, ''));
          tr.appendChild(el('th', null, 'klient'));
          for (const pk of podkategorie) tr.appendChild(el('th', null, pk));
          table.appendChild(tr);
          for (const m of lidi) {
            const row = el('tr');
            row.appendChild(el('td', null, m.name || m.email));
            for (const rel of [kRel, ...podkategorie.map((pk) => kRel + '/' + pk)]) {
              const td = el('td');
              const cb = el('input');
              cb.type = 'checkbox';
              cb.checked = pristup(rel, m.id);
              cb.onchange = () => run({akce: 'pristup', firma: slug, cesta: rel, id: m.id, ano: cb.checked});
              td.appendChild(cb);
              row.appendChild(td);
            }
            table.appendChild(row);
          }
          k.appendChild(table);
        }
        wrap.appendChild(k);
      }
      const add = el('div', 'set-row');
      const name = input('nový klient');
      const b = el('button', 'btn primary', 'Přidat klienta');
      b.onclick = () => name.value.trim()
        ? run({akce: 'slozka', firma: slug, klient: name.value.trim()}, 'Klient je založený.')
        : io.toast('Vyplň název klienta.');
      add.append(name, b);
      wrap.appendChild(add);
      return wrap;
    }

    /* Firma pro sebe: projekty v jedné tabulce (lidé × projekty), u každého
       projektu přejmenovat / odložit. Kapitáni vidí všechno vždycky. */
    function projekty(slug, p) {
      const wrap = el('div');
      wrap.appendChild(el('div', 'set-title firmy-sub', 'Projekty a kdo kam vidí'));
      wrap.appendChild(el('div', 'set-note',
        'Zaškrtni, kdo vidí který projekt. Kapitáni vidí všechno vždycky.'));
      const lidi = p.lide.filter((m) => m.role !== 'kapitan');
      const names = Object.keys(p.klienti || {});
      for (const name of names) {
        const rel = 'Projekty/' + name;
        const row = el('div', 'set-row');
        row.appendChild(el('strong', null, name));
        const nove = input('nový název');
        const ren = el('button', 'btn ghost', 'Přejmenovat');
        ren.onclick = () => (nove.value.trim()
          ? run({akce: 'prejmenovat', firma: slug, cesta: rel, nazev: nove.value.trim()}, 'Přejmenováno.')
          : io.toast('Nový název napiš do pole vedle a klikni znovu.'));
        const del = el('button', 'btn ghost', 'Odložit');
        del.title = 'Složka projektu se odloží mimo Obsidian firmy (jde vrátit ze serveru).';
        del.onclick = () => { if (confirmTwice(del)) run({akce: 'smazat', firma: slug, cesta: rel}, 'Projekt je odložený.'); };
        row.append(nove, ren, del);
        wrap.appendChild(row);
      }
      if (names.length && lidi.length) {
        const table = el('table', 'firmy-tabulka');
        const tr = el('tr');
        tr.appendChild(el('th', null, ''));
        for (const name of names) tr.appendChild(el('th', null, name));
        table.appendChild(tr);
        for (const m of lidi) {
          const row = el('tr');
          row.appendChild(el('td', null, m.name || m.email));
          for (const name of names) {
            const rel = 'Projekty/' + name;
            const td = el('td');
            const cb = el('input');
            cb.type = 'checkbox';
            cb.checked = (p.pristupy[rel] || []).includes(m.id);
            cb.onchange = () => run({akce: 'pristup', firma: slug, cesta: rel, id: m.id, ano: cb.checked});
            td.appendChild(cb);
            row.appendChild(td);
          }
          table.appendChild(row);
        }
        wrap.appendChild(table);
      }
      const add = el('div', 'set-row');
      const name = input('nový projekt (Web, Sociální sítě…)');
      const b = el('button', 'btn primary', 'Přidat projekt');
      b.onclick = () => name.value.trim()
        ? run({akce: 'slozka', firma: slug, klient: name.value.trim()}, 'Projekt je založený.')
        : io.toast('Vyplň název projektu.');
      add.append(name, b);
      wrap.appendChild(add);
      return wrap;
    }

    // Mazání bez dialogu prohlížeče: první klik se zeptá, druhý provede.
    function confirmTwice(btn) {
      if (btn.dataset.sure) return true;
      btn.dataset.sure = '1';
      const was = btn.textContent;
      btn.textContent = 'Opravdu?';
      setTimeout(() => { delete btn.dataset.sure; btn.textContent = was; }, 4000);
      return false;
    }

    kresli();
    return box;
  }

  function vzhled() {
    const box = section('Vzhled');
    const wrap = el('div', 'onb-tiles');
    const current = localStorage.getItem('hub-theme') || 'system';
    for (const [key, label, dark] of [['dark', 'Tmavý', true],
                                      ['light', 'Světlý', false],
                                      ['system', 'Podle systému', null]]) {
      const t = el('button', 'onb-tile' + (current === key ? ' on' : ''));
      t.appendChild(el('span', 'onb-swatch ' + key));
      t.appendChild(el('span', null, label));
      t.onclick = () => {
        if (key === 'system') {
          localStorage.removeItem('hub-theme');
          io.api('config', {theme: ''}).catch(() => {});
          io.setTheme(!matchMedia('(prefers-color-scheme: light)').matches, false);
        } else {
          io.setTheme(dark, true);
        }
        render();
      };
      wrap.appendChild(t);
    }
    box.appendChild(wrap);
    return box;
  }

  /* Jednoduchý režim je výchozí: aplikace se chová jako chat s Claudem a nic
     technického neukazuje. Vývojářský režim se zapíná jen tady (ne v průvodci)
     a ukáže všechno do detailu. */
  function ostatni() {
    const box = section('Ostatní');
    box.appendChild(el('div', 'set-title', 'Vývojářský režim'));
    box.appendChild(el('div', 'set-note',
      'Aplikace je nastavená jednoduše — na psaní s Claudem, poznámky a soubory. ' +
      'Vývojářský režim ukáže všechno do detailu. Běžně ho nepotřebuješ.'));
    const row = el('label', 'onb-row');
    const cb = el('input');
    cb.type = 'checkbox';
    cb.checked = advanced();
    cb.onchange = () => save({dev_mode: cb.checked});
    row.appendChild(cb);
    row.appendChild(el('span', null, 'Zapnout vývojářský režim'));
    box.appendChild(row);
    box.appendChild(el('div', 'set-note',
      'Přibude: projekty a jejich složky, terminál, výběr modelu a příkazy v poli ' +
      'na psaní, nahrávání webů a GitHub, spotřeba tokenů, jiní AI pomocníci, ' +
      'technické podrobnosti napojení, cesty na disku a záznam chyb. Vypnutím se ' +
      'zase schová — nic se nesmaže.'));
    box.appendChild(el('div', 'set-title', 'Běh na pozadí'));
    const volba = (klic, text, popis) => {
      const r = el('label', 'onb-row');
      const c = el('input');
      c.type = 'checkbox';
      c.checked = !!(state.config && state.config[klic]);
      c.onchange = () => { if (state.config) state.config[klic] = c.checked; save({[klic]: c.checked}); };
      r.appendChild(c);
      const col = el('span', 'onb-col');
      col.appendChild(el('span', null, text));
      col.appendChild(el('small', null, popis));
      r.appendChild(col);
      box.appendChild(r);
    };
    volba('na_pozadi', 'Nechat appku běžet na pozadí',
      'Zavřením okna appka neskončí: chaty s Claudem běží dál a když ji spustíš znovu, ' +
      'otevře se okno k té běžící.');
    volba('tray', 'Ikonka v liště dole',
      'Je vidět, že appka běží, i se zavřeným oknem. Klik na ni okno otevře, ' +
      'v nabídce je i Ukončit.');
    volba('bez_spanku', 'Nenechat počítač usnout, dokud appka běží',
      'I se zavřeným víkem, kde to systém dovolí. Spánek vyvolaný ručně (nabídka, tlačítko) ' +
      'žádná aplikace zakázat nemůže.');
    const konec = el('button', 'btn ghost', 'Ukončit appku úplně');
    konec.title = 'Zavře i to, co běží na pozadí — chaty s Claudem se zastaví.';
    konec.onclick = async () => {
      if (!(await HubDialog.confirm('Ukončit appku úplně? Chaty s Claudem, které běží na pozadí, se zastaví.',
                                    {title: 'Ukončit appku', ok: 'Ukončit'}))) return;
      try { await io.api('quit', {}); } catch (_) { /* appka už neodpoví */ }
      io.toast('Appka se ukončuje…');
    };
    box.appendChild(konec);
    box.appendChild(verxee());
    return box;
  }

  /* Tlačítko Nahlásit chybu / Podat návrh (hub/static/verxee.js). Klíč je
     veřejný (stejně skončí ve stránce), na serveru ho nastavuje správce. */
  function verxee() {
    const wrap = el('div');
    wrap.style.marginTop = '18px';
    wrap.appendChild(el('div', 'set-title', 'Nahlásit chybu / Podat návrh'));
    const klic = (state.config && state.config.verxee_debug_klic) || '';
    if (state.config && state.config.gateway_user) {
      wrap.appendChild(el('div', 'set-note', klic
        ? 'Tlačítko vpravo dole posílá hlášení správci Paluby (Verxee Debug).'
        : 'Tlačítko na hlášení chyb a návrhů na tomhle serveru zapíná správce.'));
      return wrap;
    }
    wrap.appendChild(el('div', 'set-note',
      'Tlačítko vpravo dole — červená půlka chyba, modrá návrh. Hlášení jdou do ' +
      'portálu Verxee Debug a Claude je přečte příkazem „chyby". Klíč vxd_… najdeš ' +
      'na alba-rosa.cz/debug v Klíče a domény; mezi doménami klíče musí být 127.0.0.1 ' +
      'a localhost. Bez klíče se tlačítko neukáže.'));
    const row = el('label', 'mcp-field');
    row.appendChild(el('span', null, 'Klíč'));
    const i = el('input');
    i.type = 'text';
    i.placeholder = 'vxd_…';
    i.autocomplete = 'off';
    i.spellcheck = false;
    i.value = klic;
    row.appendChild(i);
    wrap.appendChild(row);
    const ulozit = el('button', 'btn ghost', 'Uložit klíč');
    ulozit.onclick = async () => {
      const v = i.value.trim();
      if (v && !/^vxd_[a-f0-9]{24}$/.test(v)) {
        io.toast('Klíč vypadá jako vxd_ a 24 znaků 0–9 a a–f.');
        return;
      }
      await save({verxee_debug_klic: v});
      io.toast(v ? 'Tlačítko na hlášení je zapnuté.' : 'Tlačítko na hlášení je vypnuté.');
    };
    const portal = el('button', 'btn ghost', 'Otevřít portál');
    portal.onclick = () => io.open('https://alba-rosa.cz/debug/');
    const radek = el('div', 'onb-row');
    radek.append(ulozit, portal);
    wrap.appendChild(radek);
    return wrap;
  }

  function projekty() {
    const box = section('Složky s projekty',
      'Odsud se plní panel vlevo.');
    const dirs = (state.config.project_dirs || []).slice();
    const list = el('div', 'onb-list');
    if (!dirs.length) list.appendChild(el('div', 'empty', '(žádné)'));
    for (const dir of dirs) {
      const row = el('div', 'onb-row');
      row.appendChild(el('span', null, dir));
      const del = el('button', 'set-x', '×');
      del.title = 'Odebrat';
      del.onclick = async () => {
        const next = dirs.filter(d => d !== dir);
        await save({project_dirs: next});
      };
      row.appendChild(el('span', 'spacer'));
      row.appendChild(del);
      list.appendChild(row);
    }
    box.appendChild(list);
    const add = el('button', 'actionbtn', '＋ Přidat složku…');
    add.onclick = async () => {
      const picked = await io.pickFolder();
      if (picked) await save({project_dirs: [...new Set([...dirs, picked])]});
    };
    box.appendChild(add);
    return box;
  }

  function taby() {
    const box = section('Tlačítka nových tabů',
      'Co má být vedle tabů. Terminál se ukazuje jen ve vývojářském režimu — ' +
      'kdo jede jen v agentovi, nemá vedle tabů tlačítko, které nikdy ' +
      'nezmáčkne.');
    const cfg = state.config.newtab || {};
    const def = (state.agents || []).find((a) => a.id === state.default_agent);
    const list = el('div', 'onb-list');
    for (const [key, label] of [['agent', 'Otevřít ' + (def ? def.label : 'agenta')],
                                ['shell', 'Otevřít terminál']]) {
      const row = el('label', 'onb-row');
      const cb = el('input');
      cb.type = 'checkbox';
      // `claude` je jméno klíče z verzí do 1.6 — starý konfig se tím pádem
      // nemusí přepisovat a zaškrtnutí se ukládá do obou, ať se nerozejdou.
      cb.checked = key === 'agent'
        ? (cfg.agent !== false && cfg.claude !== false) : cfg[key] !== false;
      cb.onchange = () => save({newtab: key === 'agent'
        ? {...cfg, agent: cb.checked, claude: cb.checked}
        : {...cfg, [key]: cb.checked}});
      row.appendChild(cb);
      row.appendChild(el('span', null, label));
      list.appendChild(row);
    }
    box.appendChild(list);
    return box;
  }

  function pamet() {
    const box = section('Paměť',
      'Claude si pamatuje, na čem jste spolu pracovali — i v dalším rozhovoru. ' +
      'Najdeš to v Mých poznámkách.');
    if (advanced()) {
      box.appendChild(Object.assign(el('div', 'onb-path'),
        {textContent: state.config.brain_dir || '(vypnutá)'}));
    }
    if (state.config.brain_dir) box.appendChild(autosave());

    // Záloha paměti do privátního repa je práce s GitHubem: chce `gh`, umí si
    // říct o instalaci a o přihlášení. V základním režimu se o ní mlčí — i to
    // varování „bez zálohy" je pro někoho, kdo git nepoužívá, jen otrava.
    const git = state.vault_git || {};
    const line = el('div', 'set-row');
    if (!state.config.dev_mode) {
      // nic: ani stav, ani nabídka
    } else if (git.is_repo && git.remote) {
      line.appendChild(el('span', 'set-ok', '✓ zálohuje se do gitu'));
      line.appendChild(el('small', null, git.remote));
    } else {
      line.appendChild(el('span', 'set-warn', '! bez zálohy'));
      const b = el('button', 'btn ghost', 'Zálohovat do privátního repa');
      b.onclick = async () => {
        b.disabled = true;
        b.textContent = 'Zakládám…';
        try {
          await io.api('vault', {action: 'git', repo: 'claude-brain'});
        } catch (err) {
          io.toast('Nepodařilo se spustit: ' + err.message);
          b.disabled = false;
          return;
        }
        // Běží to na pozadí, tak se ptáme na stav — jinak by tlačítko viselo.
        for (;;) {
          await new Promise(r => setTimeout(r, 900));
          const st = await io.api('job?name=vault').catch(() => null);
          if (!st) break;
          if (st.running) { b.textContent = st.step || 'Zakládám…'; continue; }
          const r = st.result || {};
          io.toast(r.ok ? 'Paměť je v privátním repu.'
                        : ('Nepovedlo se: ' + (r.detail || '')));
          break;
        }
        await io.refreshState();
        state = io.state;
        render();
      };
      line.appendChild(b);
    }
    box.appendChild(line);

    // Jiná složka s pamětí a přesun — jen vývojářský režim.
    if (!advanced()) return box;
    const vaults = (state.vaults || []).filter(v => v.path !== state.config.brain_dir);
    if (vaults.length) {
      box.appendChild(el('div', 'set-note', 'Použít jinou složku s poznámkami:'));
      const list = el('div', 'onb-list');
      for (const v of vaults) {
        const row = el('div', 'onb-row');
        const col = el('span', 'onb-col');
        col.appendChild(el('span', null, v.name));
        col.appendChild(el('small', null, v.path + ' · ' +
          (v.has_memory ? v.notes + ' poznámek' : 'zatím bez paměti')));
        row.appendChild(col);
        row.appendChild(el('span', 'spacer'));
        const b = el('button', 'btn ghost', 'Napojit');
        b.onclick = async () => {
          try {
            await io.api('vault', {action: 'use', path: v.path});
            io.toast('Paměť napojena: ' + v.path);
            await io.refreshState();
            state = io.state;
            render();
          } catch (err) { io.toast(err.message); }
        };
        row.appendChild(b);
        list.appendChild(row);
      }
      box.appendChild(list);
    }

    const move = el('button', 'actionbtn', 'Přesunout paměť do jiné složky…');
    move.onclick = async () => {
      const picked = await io.pickFolder();
      if (!picked) return;
      try {
        await io.api('vault', {action: 'move', path: picked});
        move.disabled = true;
        for (;;) {
          await new Promise(r => setTimeout(r, 900));
          const st = await io.api('job?name=vault').catch(() => null);
          if (!st) break;
          if (st.running) { move.textContent = st.step || 'Přesouvám…'; continue; }
          const r = st.result || {};
          io.toast(r.ok ? ('Paměť přesunuta do ' + r.path)
                        : ('Nepovedlo se: ' + (r.detail || '')));
          break;
        }
        move.disabled = false;
        await io.refreshState();
        state = io.state;
        render();
      } catch (err) {
        io.toast(err.message);
      }
    };
    box.appendChild(move);
    return box;
  }

  /* Automatické ukládání: když session ztichne (20 min) nebo se tab zavře,
     Claude na pozadí sám doplní poznámku k projektu a případný poznatek.
     Stojí to trochu z předplatného, tak to jde vypnout — a je vidět, co zapsal. */
  function autosave() {
    const wrap = el('div');
    const row = el('label', 'onb-row');
    const cb = el('input');
    cb.type = 'checkbox';
    cb.checked = state.memory_autosave !== false;
    cb.onchange = () => save({memory_autosave: cb.checked});
    row.appendChild(cb);
    // div, ne span: `.onb-body label > span` by z popisu udělal tučný nadpis.
    const col = el('div', 'onb-col');
    col.appendChild(el('span', null, 'Ukládat do paměti samo'));
    col.appendChild(el('small', null,
      'Když rozhovor skončí (zavřeš ho, nebo 20 minut nikdo nepíše), Claude si ' +
      'sám zapíše, co stojí za zapamatování. Nemusíš nic ukládat.'));
    row.appendChild(col);
    wrap.appendChild(row);

    const recent = state.autosave_recent || [];
    if (recent.length) {
      wrap.appendChild(el('div', 'set-note', 'Naposledy uloženo:'));
      const list = el('div', 'onb-list');
      for (const e of recent) {
        const line = el('div', 'onb-row');
        const c = el('span', 'onb-col');
        const when = new Date(e.at).toLocaleString('cs-CZ',
          {day: 'numeric', month: 'numeric', hour: '2-digit', minute: '2-digit'});
        const what = e.status === 'saved'
          ? (e.files || []).filter((f) => f !== 'MEMORY.md').join(', ')
          : 'nepovedlo se: ' + (e.detail || 'neznámá chyba');
        c.appendChild(el('span', e.status === 'saved' ? null : 'set-warn', what));
        c.appendChild(el('small', null,
          [when, (e.projects || []).join(', ')].filter(Boolean).join(' · ')));
        line.appendChild(c);
        list.appendChild(line);
      }
      wrap.appendChild(list);
    }
    return wrap;
  }

  /* Napojení (MCP) — na co Claude Code na tomhle stroji dosáhne.
   *
   * Kontrola nečte jen registrace: každý server se opravdu osloví, protože
   * „zaregistrovaný" a „odpovídá" jsou dvě různé věci — konektor s vypršeným
   * přihlášením vypadá v souborech úplně stejně jako ten funkční. Trvá to
   * kolem deseti sekund, tak to počítá server na pozadí a sekce se dokreslí.
   */
  let mcpLast = null;      // poslední doběhlý výsledek, ať sekce mezi otevřeními nebliká

  const MCP_STATES = {
    ok:      ['set-ok', '●', 'připojeno'],
    auth:    ['set-warn', '●', 'chce přihlásit'],
    fail:    ['set-bad', '●', 'nepřipojeno'],
    local:   ['set-dim', '○', 'jen v projektu'],
    unknown: ['set-dim', '○', 'neznámý stav'],
  };

  /* ── AI agenti ─────────────────────────────────────────────────────────────
     Kdo je na stroji, kým se otevírají projekty, a jak doinstalovat zbytek.
     Verze se zjišťují spuštěním každého CLI, takže to jede na pozadí stejně
     jako MCP a mezitím je vidět, že se pracuje. */
  let agentsLast = null;

  function agenti() {
    if (!advanced()) {
      const only = section('AI agenti',
        'Modely, které si Claude umí zavolat na pomoc. Vyplníš údaje a je napojeno.');
      only.appendChild(jev());
      return only;
    }
    const box = section('AI agenti',
      'Každý tab se otevírá jedním z nich. Volba u projektu se pamatuje, ' +
      'takže e-shop může jezdit v Claudeovi a experiment v něčem jiném.');
    const summary = el('div', 'set-row');
    const list = el('div', 'onb-list');
    const ollama = el('div', 'set-note');
    const btns = el('div', 'onb-btns');
    const check = el('button', 'btn ghost', 'Zkontrolovat znovu');
    btns.appendChild(check);
    box.append(summary, list, ollama, btns);

    function busy(text) {
      summary.textContent = '';
      summary.appendChild(el('span', 'set-dim', text));
      check.disabled = true;
    }

    function openIn(kind, title) {
      close();
      io.openTab({kind, path: state.home, title});
    }

    function row(a, def) {
      const r = el('div', 'onb-row');
      const dot = el('span', a.path ? 'set-ok' : 'set-dim', a.path ? '●' : '○');
      dot.style.color = a.path ? a.color : '';
      const col = el('span', 'onb-col');
      const head = el('span', null, a.label + (a.version ? '  ' + a.version : ''));
      if (a.id === def) head.appendChild(el('b', 'set-dim', '  · výchozí'));
      col.appendChild(head);
      col.appendChild(el('small', null, a.path
        ? (a.note || a.path)
        : 'není nainstalovaný'));
      r.append(dot, col, el('span', 'spacer'));

      if (!a.path && a.install) {
        const inst = el('button', 'btn ghost', 'Nainstalovat');
        inst.title = a.install + '\n\nPustí se v tabu, ať je vidět, co se děje.';
        inst.onclick = () => openIn('install:' + a.id, 'instalace: ' + a.label);
        r.appendChild(inst);
      }
      if (a.path && (a.auth || {}).cmd) {
        const auth = el('button', 'btn ghost', 'Přihlásit');
        auth.title = (a.auth.note || a.auth.cmd);
        auth.onclick = () => openIn('auth:' + a.id, 'přihlášení: ' + a.label);
        r.appendChild(auth);
      }
      if (a.path && a.id !== def) {
        const use = el('button', 'btn ghost', 'Jako výchozí');
        use.onclick = async () => {
          await save({default_agent: a.id});
          agentsLast = null;
          load(true);
        };
        r.appendChild(use);
      }
      return r;
    }

    function draw(data) {
      check.disabled = false;
      agentsLast = data;
      const all = data.agents || [];
      const ready = all.filter((a) => a.path);
      summary.textContent = '';
      summary.appendChild(el('span', null,
        ready.length + ' z ' + all.length + ' k dispozici'));
      list.textContent = '';
      for (const a of all) list.appendChild(row(a, data.default));
      // Ollama je zvláštní: nainstalovaná nestačí, musí i běžet — bez toho
      // z ní opencode ani aider nedostanou jediný model.
      const o = data.ollama || {};
      if (!o.installed) {
        ollama.textContent = 'Lokální modely: Ollama není nainstalovaná. ' +
          'S ní umí opencode i aider jet u tebe na počítači, bez placení za tokeny.';
      } else if (!o.running) {
        ollama.textContent = 'Lokální modely: Ollama je nainstalovaná, ale neběží ' +
          '(spusť ollama serve). Dokud neběží, opencode ani aider z ní model nedostanou.';
      } else if (!(o.models || []).length) {
        ollama.textContent = 'Lokální modely: Ollama běží, ale žádný model není ' +
          'stažený (třeba ollama pull qwen2.5-coder).';
      } else {
        ollama.textContent = 'Lokální modely z Ollamy: ' + o.models.join(', ') +
          ' — nabídnou se v bublině u opencode, aidera i v tabu Ollamy.';
      }
    }

    async function load(refresh) {
      busy(refresh ? 'zjišťuji, co je na stroji…' : 'načítám…');
      try {
        let data = await io.api('agents' + (refresh ? '?refresh=1' : ''));
        // Detekce běží na pozadí (spouští se každé CLI s --version), tak se
        // na ni počká stejně jako u MCP — po sekundách, ne blokujícím čekáním.
        for (let i = 0; data.running && i < 40; i++) {
          await new Promise((r) => setTimeout(r, 500));
          data = await io.api('agents');
        }
        if (data.running) { busy('pořád zjišťuji…'); return; }
        draw(data);
      } catch (err) {
        busy('nepodařilo se zjistit: ' + err);
      }
    }

    check.onclick = () => load(true);
    if (agentsLast) draw(agentsLast); else load(false);
    box.appendChild(jev());
    return box;
  }

  /* Jev (TypeSafe) — model, ne agent: neotevírá se v tabu, volá ho Claude
     na hromadná rozhodnutí (skill jev). Postup je u něj, ať ho každý najde
     tam, kde se to vyplňuje; text kroků posílá server (hub/jev.py). */
  function jev() {
    const wrap = el('div', 'svc-card');
    const head = el('div', 'onb-row');
    const dot = el('span', 'set-dim', '○');
    const col = el('span', 'onb-col');
    col.appendChild(el('span', null, 'Jev · TypeSafe AI'));
    const sub = el('small', null, 'načítám…');
    col.appendChild(sub);
    head.append(dot, col, el('span', 'spacer'));
    const toggle = el('button', 'btn ghost', 'Nastavit');
    const off = el('button', 'btn ghost', 'Odpojit');
    off.hidden = true;
    head.append(toggle, off);
    const form = el('div', 'svc-form');
    form.hidden = true;
    wrap.append(head, form);

    function input(label, placeholder, help) {
      const row = el('label', 'mcp-field');
      row.appendChild(el('span', null, label));
      const i = el('input');
      i.type = 'text';
      i.placeholder = placeholder;
      i.autocomplete = 'off';
      i.spellcheck = false;
      row.appendChild(i);
      if (help) row.appendChild(el('small', null, help));
      form.appendChild(row);
      return i;
    }

    function draw(s) {
      dot.className = s.configured ? 'set-ok' : 'set-dim';
      dot.textContent = s.configured ? '●' : '○';
      sub.textContent = s.configured
        ? 'Napojeno přes Cloudflare (účet ' + s.account_id.slice(0, 6) + '…, token ' +
          s.token_hint + '). Claude ho volá na hromadné třídění — stačí mu to říct.'
        : 'Levný model na hromadná rozhodnutí (ano/ne, kategorie, skóre). ' +
          'Claude si ho zavolá třeba na třídění stovek e-mailů.';
      toggle.textContent = s.configured ? 'Změnit údaje' : 'Nastavit';
      off.hidden = !s.configured;

      form.textContent = '';
      const steps = el('ol', 'mcp-steps');
      for (const step of s.setup || []) {
        const li = el('li', 'mcp-step');
        const h = el('div', 'mcp-step-head');
        h.appendChild(el('strong', null, step.title));
        if (step.url) {
          h.appendChild(el('span', 'spacer'));
          const go = el('button', 'btn ghost', step.button || 'Otevřít');
          go.onclick = () => io.open(step.url);
          h.appendChild(go);
        }
        li.appendChild(h);
        li.appendChild(el('div', 'set-note', step.text));
        steps.appendChild(li);
      }
      form.appendChild(steps);
      const acc = input('Account ID', '32 znaků, např. 0123456789abcdef…');
      acc.value = s.account_id || '';
      const tok = input('API token', s.configured ? 'nech prázdné = token zůstane' : 'token z Cloudflare',
        'Uloží se jen k tobě do ~/.claude/jev.json, do prohlížeče se už nevrací.');
      tok.type = 'password';
      const status = el('div', 'set-note');
      const go = el('button', 'btn primary', 'Ověřit a napojit');
      go.onclick = async () => {
        go.disabled = true;
        status.className = 'set-note';
        status.textContent = 'Ověřuju — posílám Jevovi zkušební otázku…';
        try {
          const r = await io.api('jev', {action: 'save', account_id: acc.value,
                                         api_token: tok.value});
          io.toast(r.message || 'Napojeno.');
          form.hidden = true;
          draw(r);
        } catch (err) {
          status.className = 'set-warn';
          status.textContent = err.message;
          go.disabled = false;
        }
      };
      tok.onkeydown = (ev) => { if (ev.key === 'Enter') go.click(); };
      form.append(go, status);
    }

    toggle.onclick = () => {
      form.hidden = !form.hidden;
      if (!form.hidden) (form.querySelector('input') || {focus() {}}).focus();
    };
    off.onclick = async () => {
      if (!(await HubDialog.confirm('Odpojit Jev? Údaje se z tohohle počítače smažou.', {title: 'Odpojit Jev', ok: 'Odpojit'}))) return;
      try { draw(await io.api('jev', {action: 'remove'})); }
      catch (err) { io.toast(err.message); }
    };
    io.api('jev').then(draw).catch((err) => { sub.textContent = 'nepodařilo se zjistit: ' + err.message; });
    return wrap;
  }

  /* Účet na serveru.
   *
   * Hub běží na dvou místech: na počítači u projektů, na serveru v prostoru,
   * který je jen tvůj. Tahle sekce je to, co je spojuje — a vypadá jinak podle
   * toho, kde se zrovna kreslí:
   *
   *  - na počítači: přihlásit se (adresa → ověřit → heslo), přejít do prostoru
   *    na serveru, odhlásit;
   *  - na serveru: kdo jsi, zpátky na počítač, odhlásit.
   *
   * Přechod mezi nimi je přechod celého okna, ne nová záložka: appka si
   * pamatuje, kde jsi byl naposledy, a příště se otevře tam.
   */
  /* Účet claude.ai, pod kterým je Claude Code přihlášený. Na něm visí
     konektory z claude.ai (Gmail, Drive…) — jiný účet = jiné konektory. */
  function claudeUcet() {
    const wrap = el('div', 'acc-claude');
    wrap.appendChild(el('div', 'set-title plg-podnadpis', 'Předplatné Claude'));
    const radek = el('div', 'mcp-acct');
    radek.appendChild(el('span', 'set-dim', 'Zjišťuji účet…'));
    wrap.appendChild(radek);
    io.api('claude-ucet').then(({account: a}) => {
      radek.textContent = '';
      const col = el('span', 'onb-col');
      if (a && a.email) {
        col.appendChild(el('span', null, a.email));
        col.appendChild(el('small', null, 'Předplatné: ' + (a.plan || 'neznámé') +
          (a.org ? ' · ' + a.org : '') + (a.name ? ' · ' + a.name : '')));
        col.appendChild(el('small', null, 'Konektory z claude.ai patří k tomuhle účtu.'));
      } else {
        col.appendChild(el('span', 'set-warn', 'Claude Code není přihlášený'));
        col.appendChild(el('small', null, 'Bez přihlášení Claude nepracuje a konektory z claude.ai nejsou vidět.'));
      }
      radek.appendChild(col);
      radek.appendChild(el('span', 'spacer'));
      const swap = el('button', 'btn ghost', a && a.email ? 'Přepnout účet' : 'Přihlásit');
      swap.title = 'Přihlásí Claude k jinému účtu. Jiný účet = jiné konektory (třeba druhá gmailová schránka).';
      swap.onclick = () => { close(); io.login(); };
      radek.appendChild(swap);
    }, () => { radek.textContent = ''; radek.appendChild(el('span', 'set-warn', 'Účet se nepodařilo zjistit.')); });
    return wrap;
  }

  function ucet() {
    // Na serverovou instanci píše brána údaje o uživateli do konfigurace,
    // takže se pozná podle nich — ne podle adresy, ta může být za proxy jaká chce.
    const naServeru = !!(state.config && state.config.gateway_user);

    const box = section('Účet',
      naServeru
        ? ''
        : 'Na serveru máš vlastní prostor, který běží pořád a dostaneš se na ' +
          'něj odkudkoli — z téhle appky i z telefonu.');

    const body = el('div');
    box.appendChild(body);
    // Na serveru je účet a předplatné uvnitř bloku Claude (predplatne.js); na počítači samostatně.
    if (!naServeru) box.appendChild(claudeUcet());

    function busy(text) {
      body.textContent = '';
      body.appendChild(el('div', 'set-note', text));
    }

    function who(u, extra) {
      body.appendChild(el('div', 'acc-who', u.name || u.email || ''));
      body.appendChild(el('div', 'set-note',
        [u.name ? u.email : '',
         u.role === 'admin' ? 'správce' : 'uživatel',
         u.vault ? 'paměť „' + u.vault + '"' : '',
         extra || ''].filter(Boolean).join(' · ')));
    }

    // ---- hub na serveru ----
    // Karta s ikonkou v nadpisu — Claude, počítač, zabezpečení vedle sebe
    // přehledně, ne jako jeden dlouhý text pod sebou.
    function card(block, ico) {
      block.classList.add('acc-card');
      const title = block.querySelector('.set-title');
      if (title) {
        const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
        svg.setAttribute('class', 'ico');
        const use = document.createElementNS('http://www.w3.org/2000/svg', 'use');
        use.setAttribute('href', '#' + ico);
        svg.appendChild(use);
        title.prepend(svg);
      }
      return block;
    }

    function drawServer() {
      const u = state.config.gateway_user || {};
      const zpet = HubServer.localBack();
      body.textContent = '';
      if (state.guest) {
        // Host v cizím prostoru: účet a zabezpečení tady patří vlastníkovi,
        // svoje má host ve vlastním prostoru (gateway/pristup.py).
        body.appendChild(el('div', 'set-note',
          `Jsi v prostoru ${state.guest.owner_name || '?'} jako „${state.guest.label}“. ` +
          'Svůj účet, heslo a přístupy spravuješ ve vlastním prostoru.'));
        const back = el('button', 'btn primary', 'Zpět do svého');
        back.onclick = async () => {
          back.disabled = true;
          await fetch('/gw/pristup', {method: 'POST', credentials: 'same-origin',
            headers: {'Content-Type': 'application/json', 'X-Hub-Account': '1'},
            body: JSON.stringify({akce: 'odejit'})}).catch(() => {});
          location.href = '/';
        };
        body.appendChild(back);
        return;
      }

      // Kdo jsem: iniciály, jméno, e-mail a role — a odhlášení hned vedle.
      const prof = el('div', 'acc-profile');
      const name = u.name || u.email || '';
      const initials = name.split(/[\s.@]+/).filter(Boolean).slice(0, 2)
        .map((w) => w[0].toUpperCase()).join('');
      prof.appendChild(el('span', 'acc-avatar', initials || '?'));
      const col = el('span', 'acc-id');
      col.appendChild(el('strong', null, name));
      col.appendChild(el('small', null, [u.name ? u.email : '',
        u.role === 'admin' ? 'správce' : ''].filter(Boolean).join(' · ')));
      prof.appendChild(col);
      const acts = el('span', 'acc-acts');
      if (zpet) {
        const local = el('button', 'btn primary', 'Na tomto počítači');
        local.title = 'Okno se vrátí na hub v tvém počítači. Prostor na ' +
          'serveru běží dál a přihlášení zůstává.';
        local.onclick = () => HubServer.backTo('local');
        acts.appendChild(local);
      }
      const out = el('button', 'btn ghost', 'Odhlásit se');
      out.title = zpet
        ? 'Odhlásí appku z tohoto serveru. Příště se zeptá znovu.'
        : 'Odhlásí tenhle prohlížeč. Ostatní zařízení zůstanou přihlášená.';
      out.onclick = async () => {
        out.disabled = true;
        if (!zpet) { location.href = '/logout'; return; }
        // Cookie okna i token appky jsou tentýž token — brána ho zneplatní
        // jednou. Appka na počítači si ho pak ještě zahodí sama.
        try { await fetch('/logout', {credentials: 'same-origin'}); } catch (_) {}
        HubServer.backTo('logout');
      };
      acts.appendChild(out);
      prof.appendChild(acts);
      body.appendChild(prof);

      const cards = el('div', 'acc-cards');
      cards.appendChild(card(HubPredplatne.serverBlock(io), 'i-hub'));
      cards.appendChild(card(HubPocitac.serverBlock(io), 'i-laptop'));
      cards.appendChild(card(zabezpeceni(), 'i-lock'));
      cards.appendChild(card(pristup(), 'i-user'));
      body.appendChild(cards);
    }

    /* Přístup do mého prostoru (gateway/pristup.py): komu a s jakou rolí,
       změna role, odebrání a záznam, kdo kdy přišel a co otevřel. Vlastník je
       vždycky ten, kdo je přihlášený — brána nic jiného nepustí. */
    function pristup() {
      const wrap = el('div', 'acc-sec');
      wrap.appendChild(el('div', 'set-title', 'Přístup'));
      wrap.appendChild(el('div', 'set-note',
        'Pusť do svého prostoru někoho z týmu — třeba aby ti nastavil předplatné. ' +
        'Přístup můžeš kdykoli odebrat, platí to hned.'));
      const list = el('div');
      const log = el('div');
      const gw = async (payload) => {
        const r = await fetch('/gw/pristup', payload === undefined ? {credentials: 'same-origin'} : {
          method: 'POST', credentials: 'same-origin',
          headers: {'Content-Type': 'application/json', 'X-Hub-Account': '1'},
          body: JSON.stringify(payload),
        });
        const data = await r.json().catch(() => ({}));
        if (!r.ok) throw new Error(data.error || 'Server teď neodpovídá, zkus to za chvíli.');
        return data;
      };
      const AKCE = {pridat: 'dostal(a) přístup', role: 'změna role', odebrat: 'přístup odebrán',
                    vstup: 'vstoupil(a)', odchod: 'odešel/odešla', tab: 'otevřel(a) tab',
                    zamitnuto: 'zamítnuto'};
      let roles = [];
      const roleSelect = (value) => {
        const sel = el('select', 'set-input');
        for (const r of roles) {
          const o = el('option', null, r.label);
          o.value = r.id;
          if (r.id === value) o.selected = true;
          sel.appendChild(o);
        }
        return sel;
      };
      const run = async (payload, okText) => {
        try { await gw(payload); if (okText) io.toast(okText); } catch (e) { io.toast(e.message); }
        draw();
      };
      async function draw() {
        let d;
        try { d = await gw(); } catch (e) { list.textContent = e.message; return; }
        roles = d.role || [];
        list.textContent = '';
        if (!d.moje.length) list.appendChild(el('div', 'set-note', 'Zatím sem nikdo jiný nesmí.'));
        for (const g of d.moje) {
          const row = el('div', 'set-row');
          row.appendChild(el('span', null, (g.name || g.email) + (g.name ? ' · ' + g.email : '') +
                                           (g.online ? ' · právě tady' : '')));
          const sel = roleSelect(g.role);
          sel.onchange = () => run({akce: 'role', host: g.id, role: sel.value}, 'Role změněna.');
          row.appendChild(sel);
          const del = el('button', 'btn ghost', 'Odebrat');
          del.onclick = () => run({akce: 'odebrat', host: g.id}, 'Přístup odebrán.');
          row.appendChild(del);
          list.appendChild(row);
        }
        const add = el('div', 'set-row');
        const email = el('input', 'set-input');
        email.type = 'email';
        email.placeholder = 'e-mail účtu na téhle bráně';
        const sel = roleSelect('predplatne');
        const btn = el('button', 'btn primary', 'Přidat');
        btn.onclick = () => {
          if (!email.value.trim()) return io.toast('Vyplň e-mail.');
          run({akce: 'pridat', email: email.value.trim(), role: sel.value}, 'Přístup přidán.');
        };
        add.append(email, sel, btn);
        list.appendChild(add);
        if (d.vse && d.vse.length) {
          // Admin: všechny přístupy na bráně, jen k zobrazení a zrušení.
          list.appendChild(el('div', 'set-title', 'Všechny přístupy na bráně'));
          for (const a of d.vse) {
            const row = el('div', 'set-row');
            row.appendChild(el('span', null, `${a.host.name || a.host.email} → ${a.vlastnik.name || a.vlastnik.email} · ${a.label}`));
            const del = el('button', 'btn ghost', 'Zrušit');
            del.onclick = () => run({akce: 'odebrat', vlastnik: a.vlastnik.id, host: a.host.id}, 'Přístup zrušen.');
            row.appendChild(del);
            list.appendChild(row);
          }
        }
        log.textContent = '';
        if (d.zaznam && d.zaznam.length) {
          log.appendChild(el('div', 'set-title', 'Záznam'));
          for (const e of d.zaznam.slice(0, 30)) {
            const what = [AKCE[e.akce] || e.akce, e.role ? '(' + e.role + ')' : '',
                          e.nazev ? '„' + e.nazev + '“' : '', e.cesta || e.zprava || ''].filter(Boolean).join(' ');
            log.appendChild(el('div', 'set-note', `${e.cas} · ${e.host_jmeno} · ${what}`));
          }
        }
      }
      wrap.append(list, log);
      draw();
      return wrap;
    }

    /* Zabezpečení na serveru: dvoufázové ověření a heslo. Mluví přímo s bránou
       (/gw/…), ne s hubem v prostoru — ten o heslech nic neví. */
    function zabezpeceni() {
      const wrap = el('div', 'acc-sec');
      wrap.appendChild(el('div', 'set-title', 'Zabezpečení'));
      const gw = async (path, payload) => {
        const r = await fetch(path, payload === undefined ? {credentials: 'same-origin'} : {
          method: 'POST', credentials: 'same-origin',
          headers: {'Content-Type': 'application/json', 'X-Hub-Account': '1'},
          body: JSON.stringify(payload),
        });
        const data = await r.json().catch(() => ({}));
        if (!r.ok) throw new Error(data.error || 'Server teď neodpovídá, zkus to za chvíli.');
        return data;
      };

      const tfa = el('div', 'set-note', 'Dvoufázové ověření: zjišťuji…');
      wrap.appendChild(tfa);
      // Tlačítka vedle sebe; formuláře se rozbalí až po kliknutí.
      const actions = el('div', 'set-row');
      const passBtn = el('button', 'btn ghost', 'Změnit heslo');
      actions.appendChild(passBtn);
      wrap.appendChild(actions);
      const codesBox = el('div');
      wrap.appendChild(codesBox);
      gw('/gw/account').then((d) => {
        const t = d.twofa || {};
        tfa.textContent = '';
        tfa.appendChild(el('span', t.enabled ? 'set-ok' : 'set-warn',
          t.enabled ? '✓ Dvoufázové ověření zapnuté' : '! Dvoufázové ověření není zapnuté'));
        if (!t.enabled) return;
        tfa.appendChild(document.createTextNode(' · záložních kódů zbývá ' + t.recovery_left));
        const again = el('button', 'btn ghost', 'Nové záložní kódy');
        again.onclick = newCodes;
        actions.prepend(again);
        passkeys();
      }, (e) => { tfa.textContent = 'Stav ověření se nenačetl: ' + e.message; });

      /* Passkey jako druhý krok přihlášení (gateway/webauthn.py): místo
         opisování kódu otisk prstu, obličej, PIN nebo bezpečnostní klíč.
         Kód z aplikace zůstává jako záloha a pro appku na počítači. */
      const pkBox = el('div', 'acc-pk');
      wrap.insertBefore(pkBox, actions.nextSibling);
      const b64d = (t) => Uint8Array.from(atob(t.replace(/-/g, '+').replace(/_/g, '/') + '==='.slice((t.length + 3) % 4)), (c) => c.charCodeAt(0));
      const b64e = (b) => btoa(String.fromCharCode(...new Uint8Array(b))).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
      const kdy = (ts) => ts ? new Date(ts * 1000).toLocaleDateString('cs-CZ') : 'zatím ne';
      function kresliPk(list) {
        pkBox.textContent = '';
        pkBox.appendChild(el('div', 'set-note', list.length
          ? 'Passkey — přihlášení otiskem prstu, obličejem nebo bezpečnostním klíčem místo kódu:'
          : 'Passkey: přihlašuj se otiskem prstu, obličejem nebo bezpečnostním klíčem místo opisování kódu.'));
        for (const k of list) {
          const r = el('div', 'set-row acc-pk-radek');
          r.appendChild(el('span', 'acc-pk-jmeno', '🔑 ' + (k.name || 'Passkey')));
          r.appendChild(el('span', 'set-note', 'přidán ' + kdy(k.created) + ' · naposledy ' + kdy(k.used)));
          const del = el('button', 'btn ghost', 'Odebrat');
          del.onclick = async () => {
            if (!(await HubDialog.confirm('Odebrat passkey „' + (k.name || 'Passkey') + '“? Přihlásíš se pak kódem z aplikace.', {title: 'Odebrat passkey', ok: 'Odebrat'}))) return;
            try { kresliPk((await gw('/gw/passkey', {action: 'delete', id: k.id})).passkeys); }
            catch (e) { io.toast(e.message); }
          };
          r.appendChild(del);
          pkBox.appendChild(r);
        }
        const add = el('button', 'btn ghost', '+ Přidat passkey');
        add.onclick = pridatPk;
        if (!window.PublicKeyCredential) {
          add.disabled = true;
          add.title = 'Tenhle prohlížeč passkey neumí.';
        }
        pkBox.appendChild(add);
      }
      async function passkeys() {
        try { kresliPk((await gw('/gw/passkey')).passkeys || []); }
        catch (_) { /* starší brána bez passkeyů — nic neukazovat */ }
      }
      async function pridatPk() {
        const vychozi = /iPhone|iPad/.test(navigator.userAgent) ? 'iPhone' : /Android/.test(navigator.userAgent) ? 'Android'
          : /Mac/.test(navigator.platform) ? 'Mac' : /Win/.test(navigator.platform) ? 'Windows' : 'Počítač';
        const name = await HubDialog.prompt('Jak se má passkey jmenovat? (třeba „Můj telefon“)', vychozi, {title: 'Název passkeye', ok: 'Přidat'});
        if (name === null) return;
        try {
          const o = await gw('/gw/passkey', {action: 'options'});
          const c = await navigator.credentials.create({publicKey: {
            challenge: b64d(o.challenge), rp: o.rp,
            user: {id: b64d(o.user.id), name: o.user.name, displayName: o.user.displayName},
            pubKeyCredParams: o.algs.map((alg) => ({type: 'public-key', alg})),
            excludeCredentials: o.exclude.map((id) => ({type: 'public-key', id: b64d(id)})),
            authenticatorSelection: {residentKey: 'required', userVerification: 'required'},
            attestation: 'none', timeout: 120000}});
          const d = await gw('/gw/passkey', {action: 'register', name: name.trim(),
            clientData: b64e(c.response.clientDataJSON), attestation: b64e(c.response.attestationObject)});
          kresliPk(d.passkeys);
          io.toast('Passkey přidán — příště se přihlásíš bez opisování kódu.');
        } catch (e) {
          io.toast(e && e.name === 'NotAllowedError' ? 'Přidání passkeye bylo zrušené.'
            : e && e.name === 'InvalidStateError' ? 'Tenhle passkey už je přidaný.' : (e.message || 'Passkey se nepřidal.'));
        }
      }

      function newCodes() {
        codesBox.textContent = '';
        const row = el('div', 'set-row');
        const code = el('input', 'srv-input acc-code');
        code.placeholder = 'kód z aplikace';
        code.inputMode = 'numeric';
        code.autocomplete = 'one-time-code';
        const go = el('button', 'btn primary', 'Vygenerovat');
        row.appendChild(code);
        row.appendChild(go);
        codesBox.appendChild(row);
        codesBox.appendChild(el('div', 'set-note', 'Staré záložní kódy tím přestanou platit.'));
        const run = async () => {
          go.disabled = true;
          try {
            const d = await gw('/gw/2fa/recovery', {code: code.value});
            codesBox.textContent = '';
            codesBox.appendChild(el('div', 'set-note',
              'Nové záložní kódy — každý platí jednou. Ulož si je, znovu se neukážou.'));
            const list = el('div', 'srv-codes');
            for (const c of d.recovery) list.appendChild(el('code', '', c));
            codesBox.appendChild(list);
          } catch (e) {
            io.toast(e.message);
            go.disabled = false;
          }
        };
        go.onclick = run;
        code.onkeydown = (ev) => { if (ev.key === 'Enter') run(); };
        code.focus();
      }

      const form = el('div', 'acc-pass');
      form.hidden = true;
      passBtn.onclick = () => {
        form.hidden = !form.hidden;
        if (!form.hidden) form.querySelector('input').focus();
      };
      const field = (placeholder, auto) => {
        const input = el('input', 'srv-input');
        input.type = 'password';
        input.placeholder = placeholder;
        input.autocomplete = auto;
        form.appendChild(input);
        return input;
      };
      const cur = field('současné heslo', 'current-password');
      const nw = field('nové heslo (aspoň 10 znaků)', 'new-password');
      const nw2 = field('nové heslo znovu', 'new-password');
      const row = el('div', 'set-row');
      const save = el('button', 'btn primary', 'Změnit heslo');
      row.appendChild(save);
      form.appendChild(row);
      form.appendChild(el('div', 'set-note',
        'Po změně se odhlásí ostatní zařízení i appka na počítači; tenhle ' +
        'prohlížeč zůstane přihlášený.'));
      wrap.appendChild(form);
      save.onclick = async () => {
        if (!cur.value) return io.toast('Vyplň současné heslo.');
        if (nw.value.length < 10) return io.toast('Nové heslo musí mít aspoň 10 znaků.');
        if (nw.value !== nw2.value) return io.toast('Nová hesla se neshodují.');
        save.disabled = true;
        try {
          await gw('/gw/password', {current: cur.value, new: nw.value});
          cur.value = nw.value = nw2.value = '';
          form.hidden = true;
          io.toast('Heslo změněno. Ostatní zařízení jsou odhlášená.');
        } catch (e) {
          io.toast(e.message);
        }
        save.disabled = false;
      };
      return wrap;
    }

    // ---- hub na počítači, nepřihlášený ----
    function drawLogin(data) {
      body.textContent = '';
      if (data.note) body.appendChild(el('div', 'set-warn', '! ' + data.note));
      body.appendChild(HubServer.panel(io, {
        server: data.server || state.config.gw_server,
        email: (data.user && data.user.email) || state.config.gw_email,
        autoProbe: false,
        onReady: (res) => {
          state.config.gw_server = res.server;
          state.config.gw_logged_in = true;
          io.toast('Přihlášeno.');
          draw(res);
        },
      }));
    }

    // ---- hub na počítači, přihlášený ----
    function drawIn(data) {
      const u = data.user || {};
      body.textContent = '';

      body.appendChild(el('div', data.offline ? 'set-warn' : 'set-ok',
        data.offline ? '! Server teď neodpovídá' : '✓ Přihlášeno'));
      who(u, data.host);
      if (data.offline && data.note) {
        body.appendChild(el('div', 'set-note', data.note +
          ' Zůstáváš přihlášený, jen se teď k serveru nedá.'));
      }

      const err = el('div', 'set-warn');
      err.hidden = true;

      const row = el('div', 'set-row');
      const open = el('button', 'btn primary', 'Otevřít můj prostor');
      open.title = 'Okno přejde na server. Taby na počítači běží dál a ' +
        'appka se příště otevře rovnou v prostoru.';
      open.disabled = !!data.offline;
      open.onclick = async () => {
        open.disabled = true;
        open.textContent = 'otevírám…';
        const res = await HubServer.go(io).catch((e) => ({error: e.message}));
        if (res.ok) return;
        err.textContent = '! ' + (res.error || 'Nepovedlo se.');
        err.hidden = false;
        open.disabled = false;
        open.textContent = 'Otevřít můj prostor';
        if (res.kind === 'auth') load();
      };
      row.appendChild(open);

      row.appendChild(el('span', 'spacer'));
      const out = el('button', 'btn ghost', 'Odhlásit se');
      out.title = 'Zahodí přihlášení tohohle počítače. Ostatní zařízení ' +
        'zůstanou přihlášená.';
      out.onclick = async () => {
        out.disabled = true;
        try {
          const res = await io.api('account', {action: 'logout'});
          state.config.gw_logged_in = false;
          state.config.server_mode = false;
          draw(res);
          io.toast('Odhlášeno.');
        } catch (e) {
          io.toast('Nepovedlo se: ' + e.message);
          out.disabled = false;
        }
      };
      row.appendChild(out);
      body.appendChild(row);
      body.appendChild(err);

      body.appendChild(el('div', 'set-note',
        'Appka si pamatuje, kde jsi pracoval naposledy. Ze serveru se sem ' +
        'vrátíš v jeho nastavení: Účet → Pracovat na tomto počítači. Heslo ' +
        'a záložní kódy změníš tamtéž, v Nastavení → Účet v prostoru na serveru.'));
      body.appendChild(HubPredplatne.panel(io));
      body.appendChild(HubPocitac.panel(io));
    }

    function draw(data) {
      if (data && data.logged_in) return drawIn(data);
      drawLogin(data || {});
    }

    function load() {
      busy('zjišťuji stav…');
      io.api('account', {action: 'status'}).then(draw,
        (err) => drawLogin({note: err.message}));
    }

    if (naServeru) drawServer(); else load();
    return box;
  }

  // Brána: sdílená napojení a sdílení účtů ze služeb (gateway/mcp_sdilene.py).
  async function gwShared(payload) {
    const r = await fetch('/gw/mcp-sdilene', payload === undefined ? {credentials: 'same-origin'} : {
      method: 'POST', credentials: 'same-origin',
      headers: {'Content-Type': 'application/json', 'X-Hub-Account': '1'},
      body: JSON.stringify(payload),
    });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.error || 'Server teď neodpovídá, zkus to za chvíli.');
    return data;
  }

  /* Výběr lidí zaškrtávátky — kdo účet nebo napojení uvidí. */
  function peopleChecks(people, chosen) {
    const box = el('div', 'svc-people');
    const me = ((state.config && state.config.gateway_user) || {}).email || '';
    const others = people.filter((p) => p.email !== me);
    if (!others.length) box.appendChild(el('span', 'set-dim', 'Na serveru zatím nikdo další není.'));
    for (const p of others) {
      const row = el('label', 'svc-person');
      const cb = el('input');
      cb.type = 'checkbox';
      cb.value = p.email;
      cb.checked = chosen.has(p.email);
      row.append(cb, el('span', null, p.name || p.email));
      box.appendChild(row);
    }
    box.value = () => [...box.querySelectorAll('input:checked')].map((c) => c.value);
    return box;
  }

  /* Podkategorie firem jako příjemce sdílení: [{id, nazev}] → zaškrtávátka. */
  function groupChecks(groups, chosen) {
    const box = el('div', 'svc-people');
    for (const g of groups) {
      const row = el('label', 'svc-person');
      const cb = el('input');
      cb.type = 'checkbox';
      cb.value = g.id;
      cb.checked = chosen.has(g.id);
      row.append(cb, el('span', null, g.nazev));
      box.appendChild(row);
    }
    box.value = () => [...box.querySelectorAll('input:checked')].map((c) => c.value);
    return box;
  }

  /* Ikonka služby, ať ji člověk pozná na první pohled (hub/static/sluzby/,
     oficiální ikony z webů služeb — nic se nenačítá zvenku). */
  const SVC_ICONS = ['freelo', 'canva', 'ecomail', 'clockify', 'google', 'wordpress',
                     'facebook', 'reklamy', 'github'];
  const SVC_ICON_FILE = {facebook: 'facebook-instagram', reklamy: 'meta'};
  function svcIcon(id) {
    const key = String(id || '').toLowerCase();
    if (!SVC_ICONS.includes(key)) return null;
    const img = el('img', 'svc-ico');
    img.src = '/sluzby/' + (SVC_ICON_FILE[key] || key) + '.png';
    img.alt = '';
    return img;
  }

  /* Služby — karta na službu, pod ní účty (hub/connect.py). Pro člověka, který
     chce „napojit Freelo", ne registrovat MCP server: popisek účtu, Přihlásit,
     a na serveru vložit adresu, kam přihlášení přesměrovalo. Víc účtů u jedné
     služby je normální (firma, osobní). `filtr` vybere, které karty se
     ukážou (WhatsApp patří do Paluba Marketu, ostatní do Claude Code). */
  function sluzby(onData, filtr) {
    const wrap = el('div', 'svc-list');
    wrap.appendChild(el('div', 'set-dim', 'Načítám služby…'));
    let data = null;
    // Na serveru: kdo co s kým sdílí (brána). Účet jde nasdílet dalším lidem
    // a nasdílené od ostatních se ukážou v kartě služby.
    const onServer = !!(state.config && state.config.gateway_user);
    let shared = null;
    const sharedItems = (svcId) => ((shared && shared.napojeni) || [])
      .filter((n) => n.kind === 'ucet' && n.service === svcId);
    let timer = null;
    const wait = (ms) => new Promise((res) => setTimeout(res, ms));

    async function load(refresh) {
      clearTimeout(timer);
      try {
        [data, shared] = await Promise.all([
          io.api('connect' + (refresh ? '?refresh=1' : '')),
          onServer ? gwShared().catch(() => null) : null,
        ]);
      } catch (err) {
        wrap.textContent = '';
        wrap.appendChild(el('div', 'set-warn', 'Služby se nenačetly: ' + err.message));
        return;
      }
      if (!wrap.isConnected && wrap.parentNode === null && data) { /* ještě nevložené */ }
      // Rozdělané přihlášení se nepřekresluje — člověk by přišel o vložený text.
      // …ani rozdělaný výběr lidí u sdílení.
      if (!wrap.querySelector('.svc-login, .svc-share')) draw();
      if (onData) onData(data);
      if (data.checking) timer = setTimeout(() => load(false), 2500);
    }

    function draw() {
      wrap.textContent = '';
      for (const svc of (data.services || []).filter(filtr || (() => true))) wrap.appendChild(card(svc));
    }

    function field(parent, label, placeholder, help) {
      const row = el('label', 'mcp-field');
      row.appendChild(el('span', null, label));
      const input = el('input');
      input.type = 'text';
      input.placeholder = placeholder || label;
      input.autocomplete = 'off';
      row.appendChild(input);
      if (help) row.appendChild(el('small', null, help));
      parent.appendChild(row);
      return input;
    }

    function card(svc) {
      const box = el('div', 'svc-card');
      box.dataset.service = svc.id;
      const head = el('div', 'svc-head');
      const ico = svcIcon(svc.id);
      if (ico) head.appendChild(ico);
      head.appendChild(el('strong', null, svc.label));
      head.appendChild(el('span', 'svc-note', svc.note));
      box.appendChild(head);

      const list = el('div', 'svc-accounts');
      const fromOthers = sharedItems(svc.id).filter((n) => !n.is_owner);
      if (!(svc.accounts || []).length && !fromOthers.length) {
        list.appendChild(el('div', 'svc-empty',
          svc.kind === 'custom' ? 'Zatím žádné vlastní napojení.' : 'Zatím žádný účet.'));
      }
      for (const acc of svc.accounts || []) list.appendChild(accountRow(svc, acc));
      for (const n of fromOthers) list.appendChild(sharedRow(n));
      box.appendChild(list);
      if (svc.warn) box.appendChild(el('div', 'mcp-warn', svc.warn));

      if (svc.missing) {
        box.appendChild(el('div', 'set-warn svc-missing', svc.missing));
        if (svc.setup) {
          const toggle = el('button', 'btn ghost svc-add', 'Nastavit Google');
          const form = googleClient(svc);
          form.hidden = true;
          toggle.onclick = () => { form.hidden = !form.hidden; };
          box.appendChild(toggle);
          box.appendChild(form);
        }
        return box;
      }
      const addText = svc.kind === 'custom' ? '+ Přidat napojení' : '+ Přidat účet';
      const add = el('button', 'btn ghost svc-add', addText);
      const form = el('div', 'svc-form');
      form.hidden = true;
      add.onclick = () => {
        form.hidden = !form.hidden;
        add.textContent = form.hidden ? addText : 'Zavřít';
        if (!form.hidden) {
          addForm(svc, form);
          const first = form.querySelector('input');
          if (first) first.focus();
        }
      };
      box.appendChild(add);
      box.appendChild(form);
      return box;
    }

    function accountRow(svc, acc) {
      const [cls, dot] = MCP_STATES[acc.state] || MCP_STATES.unknown;
      const wrapRow = el('div', 'svc-acc-wrap');
      const row = el('div', 'svc-acc');
      // Logo u každého účtu: vlastní napojení má logo svého webu, ostatní logo služby.
      const sIco = svcIcon(svc.id);
      if (svc.kind === 'custom' && window.HubPluginy) {
        row.appendChild(HubPluginy.ikonka(acc.label, 'i-hub', HubPluginy.logoZUrl(acc.detail || '')));
      } else if (sIco) {
        const box = el('span', 'plg-ikona s-logem');
        sIco.className = 'plg-logo';
        box.appendChild(sIco);
        row.appendChild(box);
      }
      row.appendChild(el('span', 'mcp-dot ' + cls, dot));
      const col = el('span', 'onb-col');
      col.appendChild(el('span', null, acc.label));
      col.appendChild(el('small', null, [acc.status, acc.detail].filter(Boolean).join(' · ')));
      const mine = sharedItems(svc.id).find((n) => n.is_owner && n.account === acc.name);
      const members = (mine && mine.members) || [];
      const skupiny = (mine && mine.skupiny) || [];
      if (members.length || skupiny.length) {
        col.appendChild(el('small', 'svc-sharing',
          'vidí i: ' + [...members.map((m) => m.name || m.email), ...skupiny.map((g) => g.nazev)].join(', ')));
      }
      row.appendChild(col);
      row.appendChild(el('span', 'spacer'));
      const slot = el('div', 'svc-slot');
      if (onServer && shared) {
        const share = el('button', 'btn ghost', members.length || skupiny.length ? 'Sdíleno' : 'Sdílet');
        share.title = 'Vyber, kdo další může tenhle účet používat';
        share.onclick = () => {
          if (slot.querySelector('.svc-share')) { slot.textContent = ''; return; }
          slot.textContent = '';
          slot.appendChild(shareForm(svc, acc, members, slot, skupiny));
        };
        row.appendChild(share);
      }
      // API klíč se znovu nepřihlašuje — špatný klíč = odebrat a přidat nový.
      if (acc.state !== 'ok' && acc.state !== 'unknown' && svc.kind !== 'apikey' && svc.kind !== 'token') {
        const again = el('button', 'btn ghost', 'Přihlásit');
        again.onclick = async () => {
          again.disabled = true;
          try {
            const r = svc.kind === 'whatsapp'
              ? await io.api('connect', {action: 'add', service: svc.id, account: svc.owner || ''})
              : (svc.kind === 'google' || svc.kind === 'github')
              ? await io.api('connect', {action: 'add', service: svc.id})
              : await io.api('connect', {action: 'login', name: acc.name});
            startLogin(svc, r, slot);
          } catch (err) {
            io.toast('Nepovedlo se: ' + err.message);
            again.disabled = false;
          }
        };
        row.appendChild(again);
      }
      const del = el('button', 'set-x', '×');
      del.title = 'Odebrat účet';
      del.onclick = async () => {
        const question = svc.kind === 'custom' ? `Odebrat napojení ${acc.label}?`
          : svc.kind === 'whatsapp' ? `Odpojit WhatsApp ${acc.label}? Claude ti přestane psát a zařízení zmizí z Propojených zařízení v telefonu.`
          : svc.kind === 'github' ? `Odebrat GitHub ${acc.label}? Odhlásí se i gh, takže tady přestane fungovat i git push na GitHub.`
          : `Odebrat účet ${acc.label} (${svc.label})?`;
        if (!(await HubDialog.confirm(question, {title: 'Odebrat napojení', ok: 'Odebrat'}))) return;
        try {
          const r = await io.api('connect', {action: 'remove', service: svc.id, name: acc.name});
          if (mine) {
            await gwShared({akce: 'sdilet-ucet', service: svc.id, account: acc.name, emaily: []})
              .catch(() => {});
          }
          io.toast(r.detail || 'Odebráno.');
          load(true);
        } catch (err) { io.toast('Nepovedlo se: ' + err.message); }
      };
      row.appendChild(del);
      wrapRow.appendChild(row);
      wrapRow.appendChild(slot);
      return wrapRow;
    }

    /* Kdo další účet uvidí. Klíče ani přihlášení se nikam nekopírují — brána
       je čte z tvého prostoru, ostatní dostanou jen most (sdilene-…). */
    function shareForm(svc, acc, members, slot, skupiny = []) {
      const f = el('div', 'svc-form svc-share');
      f.appendChild(el('div', 'set-note', 'Kdo další může účet ' + acc.label +
        ' používat? Budou pracovat pod tvým přihlášením' +
        (svc.id === 'google' ? ' — uvidí tvůj Gmail, Disk i Kalendář.' : '.') +
        ' Přihlašovací údaje neuvidí nikdo z nich.'));
      const picker = peopleChecks((shared && shared.people) || [],
                                  new Set(members.map((m) => m.email)));
      // Podkategorie firem (gateway/firmy.py): účet dostane každý, kdo do
      // podkategorie vidí — i ten, kdo k ní přibude později.
      const moznosti = (shared && shared.skupiny) || [];
      const groups = groupChecks(moznosti, new Set(skupiny.map((g) => g.id)));
      const save = el('button', 'btn primary', 'Uložit');
      const cancel = el('button', 'btn ghost', 'Zrušit');
      const btns = el('div', 'svc-acc');
      btns.append(save, cancel);
      f.append(picker);
      if (moznosti.length) {
        f.append(el('div', 'set-note', 'Nebo podkategoriím firem — účet pak dostane každý, kdo do nich vidí:'), groups);
      }
      f.append(btns);
      cancel.onclick = () => { slot.textContent = ''; };
      save.onclick = async () => {
        save.disabled = true;
        const emaily = picker.value();
        const vybrane = groups.value();
        try {
          await gwShared({akce: 'sdilet-ucet', service: svc.id, account: acc.name,
                          label: acc.label, emaily, skupiny: vybrane});
          io.toast(emaily.length || vybrane.length ? 'Účet ' + acc.label + ' teď vidí i vybraní lidé.'
                                                   : 'Účet ' + acc.label + ' už s nikým nesdílíš.');
          load(false);
        } catch (err) {
          io.toast('Nepovedlo se: ' + err.message);
          save.disabled = false;
        }
      };
      return f;
    }

    // Účet, který mi nasdílel někdo jiný — jen ke čtení, spravuje ho vlastník.
    function sharedRow(n) {
      const row = el('div', 'svc-acc-wrap');
      const r = el('div', 'svc-acc');
      r.appendChild(el('span', 'mcp-dot set-ok', '●'));
      const col = el('span', 'onb-col');
      col.appendChild(el('span', null, n.label || n.account));
      col.appendChild(el('small', null, 'nasdílel ' + (n.owner_name || n.owner) +
        ' · v Claude jako ' + n.mcp_name));
      r.appendChild(col);
      row.appendChild(r);
      return row;
    }

    /* GitHub: hlavní cesta je přihlášení kódem (gh auth login --web), token
       z GitHubu jen jako záloha schovaná pod odkazem. */
    function githubForm(svc, form) {
      form.textContent = '';
      form.appendChild(el('div', 'set-note',
        'Přihlásíš se na GitHubu a Claude pak umí všechno: repozitáře, issues, ' +
        'pull requesty, Actions, tajné údaje, spolupracovníky, nastavení i push.'));
      const slot = el('div', 'svc-slot');
      const go = el('button', 'btn primary', 'Přihlásit přes GitHub');
      go.onclick = async () => {
        go.disabled = true;
        go.textContent = 'Připravuju přihlášení…';
        try {
          const r = await io.api('connect', {action: 'add', service: svc.id});
          go.hidden = true;
          adv.hidden = true;
          startLogin(svc, r, slot);
        } catch (err) {
          io.toast('Nepovedlo se: ' + err.message);
          go.disabled = false;
          go.textContent = 'Přihlásit přes GitHub';
        }
      };
      form.appendChild(go);
      const adv = el('details', 'svc-adv');
      adv.appendChild(el('summary', null, 'Raději vložím vlastní token'));
      const steps = el('ol', 'mcp-steps');
      for (const step of svc.setup || []) {
        const li = el('li', 'mcp-step');
        const head = el('div', 'mcp-step-head');
        head.appendChild(el('strong', null, step.title));
        if (step.url) {
          head.appendChild(el('span', 'spacer'));
          const open = el('button', 'btn ghost', step.button || 'Otevřít');
          open.onclick = () => io.open(step.url);
          head.appendChild(open);
        }
        li.appendChild(head);
        li.appendChild(el('div', 'set-note', step.text));
        steps.appendChild(li);
      }
      adv.appendChild(steps);
      const token = field(adv, svc.field.label, '', svc.field.help);
      token.type = 'password';
      token.autocomplete = 'new-password';
      const useToken = el('button', 'btn primary', 'Napojit tokenem');
      useToken.onclick = async () => {
        const value = token.value.trim();
        if (!value) { io.toast('Vlož token.'); token.focus(); return; }
        useToken.disabled = true;
        useToken.textContent = 'Ověřuju token…';
        try {
          const r = await io.api('connect', {action: 'add', service: svc.id, account: value});
          token.value = '';
          startLogin(svc, r, slot);
        } catch (err) {
          io.toast('Nepovedlo se: ' + err.message);
        }
        useToken.disabled = false;
        useToken.textContent = 'Napojit tokenem';
      };
      token.onkeydown = (ev) => { if (ev.key === 'Enter') useToken.click(); };
      adv.appendChild(useToken);
      form.appendChild(adv);
      form.appendChild(slot);
    }

    /* Kód na github.com/login/device — nic se nevkládá zpátky, gh se GitHubu
       samo ptá, jestli už je potvrzeno. */
    function deviceLogin(svc, login, slot) {
      slot.textContent = '';
      const p = el('div', 'svc-login');
      p.appendChild(el('div', 'svc-step', '1. Zkopíruj tenhle kód:'));
      const codeRow = el('div', 'svc-paste');
      const code = el('input', 'svc-code');
      code.type = 'text';
      code.readOnly = true;
      code.value = login.code;
      code.onclick = () => code.select();
      const copy = el('button', 'btn ghost', 'Kopírovat');
      const copyCode = async () => {
        try { await navigator.clipboard.writeText(login.code); io.toast('Kód zkopírovaný.'); }
        catch (_) { code.select(); }
      };
      copy.onclick = copyCode;
      codeRow.append(code, copy);
      p.appendChild(codeRow);
      p.appendChild(el('div', 'svc-step',
        '2. Otevři GitHub, vlož kód a dej Authorize. Hotovo se tady ukáže samo.'));
      const openBtn = el('button', 'btn primary', 'Zkopírovat kód a otevřít GitHub');
      openBtn.onclick = () => { copyCode(); io.open(login.url); };
      p.appendChild(openBtn);
      const status = el('div', 'set-note svc-status', 'Čekám na potvrzení na GitHubu…');
      p.appendChild(status);
      const cancel = el('button', 'linkbtn', 'Zrušit');
      p.appendChild(cancel);
      slot.appendChild(p);
      let stopped = false;
      cancel.onclick = () => {
        stopped = true;
        io.api('connect', {action: 'cancel', id: login.id}).catch(() => {});
        slot.textContent = '';
        load(false);
      };
      (async () => {
        while (!stopped) {
          await wait(2000);
          if (stopped || !p.isConnected) break;
          let st;
          try {
            st = await io.api('connect', {action: 'status', id: login.id});
          } catch (_) { continue; }
          if (stopped || !st.done) continue;
          stopped = true;
          if (st.ok) {
            io.toast(st.message || 'GitHub napojený.');
            slot.textContent = '';
            load(true);
          } else {
            status.className = 'set-warn svc-status';
            status.textContent = st.message || 'Přihlášení se nepovedlo.';
          }
        }
      })();
    }

    /* WhatsApp: hub nainstaluje knihovnu, pustí spojení a ukazuje QR, dokud ho
       telefon s Claudovým číslem nenaskenuje (QR se každých ~20 s mění). */
    function whatsappLogin(svc, login, slot) {
      slot.textContent = '';
      const p = el('div', 'svc-login');
      p.appendChild(el('div', 'svc-step',
        'Na telefonu s Claudovým číslem: WhatsApp → ⋮ / Nastavení → Propojená zařízení → ' +
        'Propojit zařízení, a naskenuj kód:'));
      const qrBox = el('div', 'svc-qr');
      p.appendChild(qrBox);
      const status = el('div', 'set-note svc-status', login.phase || 'Připravuju…');
      p.appendChild(status);
      const cancel = el('button', 'linkbtn', 'Zrušit');
      p.appendChild(cancel);
      slot.appendChild(p);
      let stopped = false;
      let shown = '';
      const show = (svg) => {
        if (svg === shown) return;
        shown = svg;
        qrBox.innerHTML = svg || '';          // SVG z hub/qr.py, žádný cizí vstup
        qrBox.hidden = !svg;
      };
      show(login.qr || '');
      cancel.onclick = () => {
        stopped = true;
        io.api('connect', {action: 'cancel', id: login.id}).catch(() => {});
        slot.textContent = '';
        load(false);
      };
      (async () => {
        while (!stopped) {
          await wait(1500);
          if (stopped || !p.isConnected) break;
          let st;
          try {
            st = await io.api('connect', {action: 'status', id: login.id});
          } catch (_) { continue; }
          if (stopped) break;
          if (!st.done) {
            show(st.qr || '');
            if (st.phase) status.textContent = st.phase;
            continue;
          }
          stopped = true;
          if (st.ok) {
            io.toast(st.message || 'WhatsApp propojený.');
            slot.textContent = '';
            load(true);
          } else {
            show('');
            status.className = 'set-warn svc-status';
            status.textContent = st.message || 'Propojení se nepovedlo.';
          }
        }
      })();
    }

    function addForm(svc, form) {
      if (svc.kind === 'github') { githubForm(svc, form); return; }
      form.textContent = '';
      const inputs = {};
      if (svc.kind === 'custom') {
        inputs.label = field(form, 'Název', 'třeba Notion',
          'Podle něj napojení poznáš ty i Claude (jeho nástroje budou mcp__název__…).');
      } else if (svc.kind === 'whatsapp') {
        form.appendChild(el('div', 'set-note',
          'Na telefonu s Claudovým číslem pak naskenuješ QR (WhatsApp → Propojená zařízení). ' +
          'Claude ti potom píše jako normální kontakt a odpovědi bere jen z tvého čísla.'));
      } else if (svc.kind !== 'google') {
        inputs.label = field(form, 'Popisek účtu', 'třeba firma nebo osobní',
          'Podle něj účty poznáš ty i Claude.');
      }
      // Návod krok za krokem (token Mety) — tlačítko otevře přesně tu stránku.
      if ((svc.kind === 'token' || svc.kind === 'apikey') && (svc.setup || []).length) {
        const steps = el('ol', 'mcp-steps');
        for (const step of svc.setup) {
          const li = el('li', 'mcp-step');
          const head = el('div', 'mcp-step-head');
          head.appendChild(el('strong', null, step.title));
          if (step.url) {
            head.appendChild(el('span', 'spacer'));
            const go = el('button', 'btn ghost', step.button || 'Otevřít');
            go.onclick = () => io.open(step.url);
            head.appendChild(go);
          }
          li.appendChild(head);
          li.appendChild(el('div', 'set-note', step.text));
          steps.appendChild(li);
        }
        form.appendChild(steps);
      }
      if (svc.field) {
        inputs.account = field(form, svc.field.label,
          svc.kind === 'custom' ? 'https://…' : '', svc.field.help);
        if (svc.field.secret) inputs.account.type = 'password';
      }
      // Nepovinné jako v oficiální appce: jen pro servery, které si klienta
      // OAuth neumí zaregistrovat samy.
      const optional = {};
      if (svc.kind === 'custom') {
        const adv = el('details', 'svc-adv');
        adv.appendChild(el('summary', null, 'Pokročilá nastavení'));
        optional.client_id = field(adv, 'OAuth Client ID (nepovinné)', '');
        optional.client_secret = field(adv, 'OAuth Client Secret (nepovinné)', '');
        optional.client_secret.type = 'password';
        // Správce hesel jinak vyplní heslo do Secretu a e-mail do Client ID před ním.
        optional.client_secret.autocomplete = 'new-password';
        optional.client_id.autocomplete = 'off';
        optional.client_id.setAttribute('data-lpignore', 'true');
        optional.client_secret.setAttribute('data-lpignore', 'true');
        adv.appendChild(el('small', 'set-note',
          'Vyplň, jen když služba vydává vlastní přihlašovací klienty. ' +
          'Jako adresu pro přesměrování (redirect URI) jim zadej ' + (svc.redirect || '') + '.'));
        form.appendChild(adv);
      }
      const goText = svc.kind === 'google' ? 'Přihlásit Google účet'
        : svc.kind === 'custom' ? 'Přidat'
        : svc.kind === 'whatsapp' ? 'Pokračovat k QR kódu'
        : (svc.kind === 'apikey' || svc.kind === 'token') ? 'Napojit' : 'Přihlásit';
      const go = el('button', 'btn primary', goText);
      form.appendChild(go);
      const slot = el('div', 'svc-slot');
      form.appendChild(slot);
      for (const input of [...Object.values(inputs), ...Object.values(optional)]) {
        input.onkeydown = (ev) => { if (ev.key === 'Enter') go.click(); };
      }
      go.onclick = async () => {
        const payload = {action: 'add', service: svc.id};
        for (const [key, input] of Object.entries(inputs)) {
          payload[key] = input.value.trim();
          if (!payload[key]) {
            io.toast(key === 'label' ? (svc.kind === 'custom' ? 'Pojmenuj napojení.'
                                                               : 'Pojmenuj účet, ať se dají poznat.')
                                     : 'Vyplň ' + svc.field.label + '.');
            input.focus();
            return;
          }
        }
        for (const [key, input] of Object.entries(optional)) {
          if (input.value.trim()) payload[key] = input.value.trim();
        }
        go.disabled = true;
        go.textContent = svc.kind === 'custom' ? 'Připojuju se k serveru…' : 'Připravuju přihlášení…';
        try {
          const r = await io.api('connect', payload);
          go.hidden = true;
          for (const input of [...Object.values(inputs), ...Object.values(optional)]) input.disabled = true;
          startLogin(svc, r, slot);
        } catch (err) {
          io.toast('Nepovedlo se: ' + err.message);
          go.disabled = false;
          go.textContent = goText;
        }
      };
    }

    /* Přihlášení: tlačítko na stránku služby a pole na adresu, kam to potom
       přesměrovalo. Na počítači to většinou doběhne samo (hlídá se stav). */
    function startLogin(svc, r, slot) {
      const login = r.login || {};
      if (login.done) {
        io.toast(login.message || 'Přihlášeno.');
        load(true);
        return;
      }
      if (login.code) { deviceLogin(svc, login, slot); return; }
      if (svc.kind === 'whatsapp') { whatsappLogin(svc, login, slot); return; }
      slot.textContent = '';
      const p = el('div', 'svc-login');
      p.appendChild(el('div', 'svc-step', '1. Otevři přihlášení a potvrď přístup.'));
      const who = svc.kind === 'custom' && r.name ? r.name : svc.label;
      const openBtn = el('button', 'btn primary', 'Otevřít přihlášení — ' + who);
      openBtn.onclick = () => io.open(login.url);
      p.appendChild(openBtn);
      p.appendChild(el('div', 'svc-step', data && data.on_server
        ? '2. Stránka, kam tě to pak pošle, se nenačte — to je v pořádku. Zkopíruj celou adresu z adresního řádku a vlož ji sem:'
        : '2. Hotovo se ukáže samo. Kdyby ne, vlož sem adresu z adresního řádku stránky, kam tě to poslalo:'));
      const row = el('div', 'svc-paste');
      const input = el('input');
      input.type = 'text';
      input.placeholder = 'http://localhost…';
      input.autocomplete = 'off';
      const done = el('button', 'btn primary', 'Dokončit');
      row.appendChild(input);
      row.appendChild(done);
      p.appendChild(row);
      const status = el('div', 'set-note svc-status');
      p.appendChild(status);
      const cancel = el('button', 'linkbtn', 'Zrušit');
      p.appendChild(cancel);
      slot.appendChild(p);

      let stopped = false;
      const finish = (res) => {
        stopped = true;
        io.toast(res.message || 'Napojeno.');
        slot.textContent = '';
        load(true);
      };
      done.onclick = async () => {
        const value = input.value.trim();
        if (!value) { input.focus(); return; }
        done.disabled = true;
        status.className = 'set-note svc-status';
        status.textContent = 'Ověřuju…';
        try {
          const res = await io.api('connect', {action: 'finish', id: login.id, url: value});
          if (res.done && res.ok) { finish(res); return; }
          status.className = 'set-warn svc-status';
          status.textContent = res.message || 'Přihlášení se nepovedlo.';
          if (res.done) stopped = true;
          else done.disabled = false;
        } catch (err) {
          status.className = 'set-warn svc-status';
          status.textContent = err.message;
          done.disabled = false;
        }
      };
      input.onkeydown = (ev) => { if (ev.key === 'Enter') done.click(); };
      cancel.onclick = () => {
        stopped = true;
        io.api('connect', {action: 'cancel', id: login.id}).catch(() => {});
        slot.textContent = '';
        load(false);
      };
      (async () => {
        while (!stopped) {
          await wait(1500);
          if (stopped || !p.isConnected) break;
          let st;
          try {
            st = await io.api('connect', {action: 'status', id: login.id});
          } catch (_) { continue; }
          if (stopped) break;
          if (st.done && st.ok) { finish(st); break; }
          if (st.done) {
            stopped = true;
            status.className = 'set-warn svc-status';
            status.textContent = st.message || 'Přihlášení se nepovedlo.';
            done.disabled = true;
          }
        }
      })();
    }

    // Klient OAuth pro Google na počítači (na serveru ho drží správce brány).
    function googleClient(svc) {
      const f = el('div', 'svc-form');
      const steps = el('ol', 'mcp-steps');
      for (const step of svc.setup || []) {
        const li = el('li', 'mcp-step');
        const head = el('div', 'mcp-step-head');
        head.appendChild(el('strong', null, step.title));
        if (step.url) {
          head.appendChild(el('span', 'spacer'));
          const go = el('button', 'btn ghost', step.button || 'Otevřít');
          go.onclick = () => io.open(step.url);
          head.appendChild(go);
        }
        li.appendChild(head);
        li.appendChild(el('div', 'set-note', step.text));
        steps.appendChild(li);
      }
      f.appendChild(steps);
      const id = field(f, 'Client ID', '….apps.googleusercontent.com');
      const secret = field(f, 'Client secret');
      secret.type = 'password';
      const save = el('button', 'btn primary', 'Uložit klienta');
      save.onclick = async () => {
        save.disabled = true;
        try {
          const r = await io.api('connect', {action: 'google-client',
                                             client_id: id.value, client_secret: secret.value});
          io.toast(r.detail || 'Uloženo.');
          load(false);
        } catch (err) {
          io.toast('Nepovedlo se: ' + err.message);
          save.disabled = false;
        }
      };
      f.appendChild(save);
      return f;
    }

    load(false);
    return wrap;
  }

  /* Napojení a pluginy: dva obchody vedle sebe.
       Claude Code Marketplace — co rozšiřuje Clauda: pluginy (katalog),
         MCP (služby a napojení) a skilly (moje příkazy, z pluginů, z Obsidianu).
       Paluba Market — co rozšiřuje samotnou appku: pluginy appky, motivy,
         napojení, která Paluba přináší (WhatsApp, prohlížeč…), a přibalené skilly.
     Záložka s Moje | Objevit má u sebe přepínač; volba se pamatuje. */
  const TRHY = {
    cc: {
      nazev: 'Claude Code Marketplace', pod: 'Pluginy, MCP a skilly pro Claude Code', ico: 'i-terminal',
      tabs: [['pluginy', 'Pluginy', 'i-plus', true], ['mcp', 'MCP', 'i-hub', true], ['skilly', 'Skilly', 'i-book', false]],
    },
    paluba: {
      nazev: 'Paluba Market', pod: 'Pluginy, motivy, MCP a skilly pro Palubu', ico: 'i-paluba',
      tabs: [['pluginy', 'Pluginy', 'i-plus', true], ['motivy', 'Motivy', 'i-sun', true],
             ['mcp', 'MCP', 'i-hub', false], ['skilly', 'Skilly', 'i-bolt', false]],
    },
  };
  const ROZ_STARE = {skilly: ['cc', 'skilly'], konektory: ['cc', 'mcp'], pluginy: ['cc', 'pluginy'], appka: ['paluba', 'pluginy']};

  function rozsireni() {
    const box = section('Napojení a pluginy',
      'Claude Code Marketplace rozšiřuje Clauda — pluginy, napojení na služby a skilly. ' +
      'Paluba Market rozšiřuje samotnou appku — pluginy, motivy vzhledu a to, co Paluba přináší sama.');
    const uloz = (k, v) => { try { localStorage.setItem(k, v); } catch (_) { /* soukromé okno */ } };
    const cti = (k) => { try { return localStorage.getItem(k) || ''; } catch (_) { return ''; } };
    // Záložky z doby před dvěma obchody (hub-roz-tab) se převezmou jednou.
    const stara = ROZ_STARE[cti('hub-roz-tab')];
    if (stara && !cti('hub-roz-trh')) {
      uloz('hub-roz-trh', stara[0]);
      uloz('hub-roz-tab-' + stara[0], stara[1]);
      try { localStorage.removeItem('hub-roz-tab'); } catch (_) { /* nic */ }
    }
    let trh = TRHY[cti('hub-roz-trh')] ? cti('hub-roz-trh') : 'cc';
    const kdeV = (t) => {
      const k = cti('hub-roz-tab-' + t);
      return TRHY[t].tabs.some(([id]) => id === k) ? k : TRHY[t].tabs[0][0];
    };
    let kde = kdeV(trh);
    let cim = cti('hub-roz-cim') === 'objevit' ? 'objevit' : 'moje';
    const P = window.HubPluginy;

    const volba = el('div', 'trh-volba');
    const tabs = el('div', 'plg-tabs roz-tabs');
    const prepinac = el('div', 'plg-tabs roz-moje');       // Moje | Objevit
    const telo = el('div', 'roz-telo');
    const ikona = (ico) => (document.getElementById(ico) ? ico : 'i-hub');

    for (const [id, t] of Object.entries(TRHY)) {
      const b = el('button', 'trh-karta');
      b.type = 'button';
      b.dataset.k = id;
      b.innerHTML = '<span class="trh-ico"><svg class="ico"><use href="#' + ikona(t.ico) + '"/></svg></span>' +
                    '<span class="trh-text"><b></b><small></small></span>';
      b.querySelector('b').textContent = t.nazev;
      b.querySelector('small').textContent = t.pod;
      b.onclick = () => {
        if (trh === id) return;
        trh = id;
        kde = kdeV(id);
        uloz('hub-roz-trh', id);
        zalozky();
        kresli();
      };
      volba.appendChild(b);
    }

    const opts = () => ({
      setMotiv: io.setMotiv, open: io.open,
      vychozi: (() => {
        const d = (state.palette && state.palette.dark) || {};
        return [d.BG, d.BG_CARD, d.AMBER, d.CLOUD, d.FG_BRIGHT].filter(Boolean);
      })(),
      // Po instalaci pluginu z katalogu Paluby: zpátky do Moje, kde se zapíná.
      poInstalaci: () => { const t = prepinac.querySelector('.plg-tab[data-k="moje"]'); if (t) t.click(); },
    });

    function obsah(objevit) {
      if (trh === 'cc' && kde === 'mcp') return napojeni(objevit);
      if (trh === 'paluba' && kde === 'mcp') return palubaMcp();
      if (!P) return el('div', 'set-note', 'Načítám…');
      if (trh === 'cc') return kde === 'pluginy' ? P.claudeCode(objevit ? 'objevit' : 'moje') : P.skilly();
      if (!P.motivy) return el('div', 'set-note', 'Tahle část potřebuje obnovit okno (Ctrl+R).');
      if (kde === 'pluginy') return objevit ? P.market('pluginy', opts()) : P.appka();
      if (kde === 'motivy') return objevit ? P.market('motivy', opts()) : P.motivy(opts());
      return P.palubaSkilly();
    }

    function zalozky() {
      tabs.textContent = '';
      for (const [id, text, ico] of TRHY[trh].tabs) {
        const b = el('button', 'plg-tab');
        b.innerHTML = '<svg class="ico"><use href="#' + ico + '"/></svg><span></span>';
        b.querySelector('span').textContent = text;
        b.dataset.k = id;
        b.onclick = () => { kde = id; uloz('hub-roz-tab-' + trh, id); kresli(); };
        tabs.appendChild(b);
      }
    }

    function kresli() {
      for (const b of volba.children) {
        b.classList.toggle('on', b.dataset.k === trh);
        b.setAttribute('aria-pressed', b.dataset.k === trh ? 'true' : 'false');
      }
      for (const b of tabs.children) b.classList.toggle('on', b.dataset.k === kde);
      const maObjevit = !!(TRHY[trh].tabs.find(([id]) => id === kde) || [])[3];
      prepinac.hidden = !maObjevit;
      for (const b of prepinac.children) b.classList.toggle('on', b.dataset.k === cim);
      telo.textContent = '';
      telo.appendChild(obsah(maObjevit && cim === 'objevit'));
    }

    for (const [id, text] of [['moje', 'Moje'], ['objevit', 'Objevit']]) {
      const b = el('button', 'plg-tab', text);
      b.dataset.k = id;
      b.onclick = () => { cim = id; uloz('hub-roz-cim', id); kresli(); };
      prepinac.appendChild(b);
    }
    const hlava = el('div', 'roz-hlava');
    hlava.append(tabs, prepinac);
    box.append(volba, hlava, telo);
    zalozky();
    kresli();
    return box;
  }

  /* Paluba Market → MCP: napojení, která přináší Paluba sama. WhatsApp
     (karta služby z hub/connect.py) a „Součást appky" — prohlížeč, spojení
     s počítačem a sdílená napojení od kolegů. Ty spravuje hub, odebrat nejdou. */
  function palubaMcp() {
    const box = el('div');
    box.appendChild(el('div', 'set-note',
      'Napojení, která přináší Paluba: WhatsApp, přes který ti Claude píše a ptá se tě, ' +
      'a nástroje appky — prohlížeč, spojení s počítačem a napojení sdílená od kolegů.'));
    box.appendChild(el('div', 'set-title plg-podnadpis', 'WhatsApp'));
    box.appendChild(sluzby(null, (svc) => svc.id === 'whatsapp'));
    box.appendChild(el('div', 'set-title plg-podnadpis', 'Součást appky'));
    const summary = el('div', 'set-row');
    const list = el('div', 'onb-list mcp-appka');
    box.append(summary, list);
    const naServeru = !!(state.config && state.config.gateway_user);
    const PEVNE = [
      {nazev: 'Prohlížeč pro Clauda', ico: 'i-globe', popis: 'vestavěný prohlížeč, který vidíš v appce',
       chybi: 'Paluba ho napojí sama, jakmile je v appce zapnutý vestavěný prohlížeč.'},
      {nazev: 'Spojení s počítačem', ico: 'i-laptop', popis: 'Claude ze serveru sahá na tvůj počítač',
       chybi: naServeru ? 'Napojí se samo, když je tvůj prostor na serveru spojený s počítačem.'
                        : 'Funguje v prostoru na serveru — Claude odtamtud sahá na tenhle počítač. Tady ho nepotřebuješ.'},
    ];

    function radek(info, x) {
      const [cls, dot] = x ? (MCP_STATES[x.state] || MCP_STATES.unknown) : MCP_STATES.unknown;
      const row = el('div', 'onb-row mcp-row' + (x ? '' : ' plg-zasedle'));
      row.appendChild(window.HubPluginy ? HubPluginy.ikonka(info.nazev, info.ico) : el('span'));
      row.appendChild(Object.assign(el('span', 'mcp-dot ' + cls), {textContent: x ? dot : '–'}));
      const col = el('span', 'onb-col');
      col.appendChild(el('span', null, info.nazev));
      col.appendChild(el('small', null, x
        ? info.popis + (x.state !== 'ok' && x.status ? ' · ' + x.status : '')
        : 'není zapnuté · ' + info.chybi));
      row.appendChild(col);
      return row;
    }

    function draw(data) {
      list.textContent = '';
      summary.textContent = '';
      if (data.ok === false && data.detail) summary.appendChild(el('span', 'set-warn', data.detail));
      const appky = (data.servers || []).map((x) => [soucastAppky(x), x]).filter(([info]) => info);
      for (const pevne of PEVNE) {
        if (!appky.some(([info]) => info.nazev === pevne.nazev)) list.appendChild(radek(pevne, null));
      }
      for (const [info, x] of appky) list.appendChild(radek(info, x));
    }

    async function load() {
      summary.textContent = '';
      summary.appendChild(el('span', 'set-dim', 'Načítám…'));
      try {
        let data = await io.api('mcp');
        // Hub se zrovna ptá serverů — počkat, ale ne donekonečna a ne po odchodu ze záložky.
        for (let i = 0; data.running && i < 100; i++) {
          summary.textContent = '';
          summary.appendChild(el('span', 'set-dim', 'Zjišťuji, která napojení odpovídají…'));
          await new Promise((r) => setTimeout(r, 1200));
          if (!box.isConnected) return;
          data = await io.api('mcp');
        }
        if (!data.running) mcpLast = data;
        draw(data);
      } catch (err) {
        summary.textContent = '';
        summary.appendChild(el('span', 'set-warn', 'Nepovedlo se: ' + err.message));
      }
    }

    if (mcpLast) draw(mcpLast);
    else for (const pevne of PEVNE) list.appendChild(radek(pevne, null));
    load();
    return box;
  }

  /* Napojení, která jsou součástí appky (hub je zakládá a spravuje sám). */
  function soucastAppky(s) {
    const t = s.target || '';
    if (s.name === 'playwright' && /playwright_bridge/.test(t)) {
      return {nazev: 'Prohlížeč pro Clauda', ico: 'i-globe', popis: 'vestavěný prohlížeč, který vidíš v appce'};
    }
    if (s.name === 'pocitac' || /pocitac_mcp/.test(t)) {
      return {nazev: 'Spojení s počítačem', ico: 'i-laptop', popis: 'Claude ze serveru sahá na tvůj počítač'};
    }
    if (/^sdilene-/.test(s.name) || /sdilene_mcp/.test(t)) {
      return {nazev: 'Sdílené: ' + s.name.replace(/^sdilene-/, ''), ico: 'i-user', popis: 'napojení, které s tebou sdílí kolega'};
    }
    return null;
  }

  function napojeni(objevit) {
    const box = el('div');
    // Objevit: katalog (konektory claude.ai + registr MCP serverů).
    if (objevit) {
      box.appendChild(el('div', 'set-note',
        'Najdi službu a přidej ji jedním klikem. Přihlásíš se pak v záložce Moje.'));
      if (window.HubPluginy) box.appendChild(HubPluginy.katalogNapojeni(() => {
        try { localStorage.setItem('hub-roz-cim', 'moje'); } catch (_) { /* nic */ }
        const t = document.querySelector('.roz-moje .plg-tab[data-k="moje"]');
        if (t) t.click();
      }));
      return box;
    }
    box.appendChild(el('div', 'set-note',
      'Služby a nástroje, se kterými Claude umí pracovat — e-mail, kalendář, úkoly, design… ' +
      'Další najdeš v záložce Objevit.'));
    box.appendChild(el('div', 'set-title plg-podnadpis', 'Služby'));
    let sluzbyServery = null;            // MCP servery, které už mají karty služeb
    // WhatsApp patří do Paluba Marketu → MCP (palubaMcp).
    box.appendChild(sluzby((d) => {
      const nove = (d.servers || []).join('|');
      if (sluzbyServery && [...sluzbyServery].join('|') === nove) return;   // bez blikání při kontrole
      sluzbyServery = new Set(d.servers || []);
      if (mcpLast) draw(mcpLast);
    }, (svc) => svc.id !== 'whatsapp'));
    box.appendChild(el('div', 'set-title plg-podnadpis', 'Ostatní napojení'));
    const summary = el('div', 'set-row');
    const list = el('div', 'onb-list');
    const btns = el('div', 'onb-btns');
    const check = el('button', 'actionbtn', 'Zkontrolovat znovu');
    btns.appendChild(check);
    const tech = el('div');
    tech.append(summary, list, btns);
    box.appendChild(tech);

    function busy(text) {
      summary.textContent = '';
      summary.appendChild(el('span', 'set-dim', text));
      check.disabled = true;
    }

    function draw(data) {
      check.disabled = false;
      mcpLast = data;
      const servers = data.servers || [];
      const c = data.counts || {};

      if (!data.ok && data.detail) {
        summary.textContent = '';
        summary.appendChild(el('span', 'set-warn', data.detail));
      }
      list.textContent = '';
      if (!servers.length) list.appendChild(el('div', 'empty', '(žádné napojení)'));
      // Funkční nahoře, pak co chce přihlásit, pak zbytek; uvnitř podle abecedy.
      const PORADI = {ok: 0, auth: 1, local: 2};
      const mimoSluzby = sluzbyServery ? servers.filter((x) => !sluzbyServery.has(x.name)) : servers;
      // Součást appky a WhatsApp ukazuje Paluba Market → MCP (palubaMcp).
      const ostatni = mimoSluzby.filter((x) => !soucastAppky(x) && x.name !== 'whatsapp');
      // Počty jen za to, co je tady vidět (služby mají stav ve svých kartách).
      summary.textContent = '';
      if (data.ok !== false && ostatni.length) {
        const ok = ostatni.filter((x) => x.state === 'ok').length;
        summary.appendChild(el('span', 'set-ok', ok + ' z ' + ostatni.length + ' připojeno'));
      }
      if (!ostatni.length && servers.length) list.appendChild(el('div', 'set-note', 'Žádná další napojení — všechna jsou mezi službami výš.'));
      const serazene = ostatni.slice().sort((a, b) =>
        ((PORADI[a.state] ?? 3) - (PORADI[b.state] ?? 3)) ||
        a.name.replace(/^claude\.ai /, '').localeCompare(b.name.replace(/^claude\.ai /, ''), 'cs'));
      const tik = ++drawTik;
      serazene.forEach((s, i) => setTimeout(() => {
        if (tik !== drawTik) return;       // mezitím přišel novější výsledek
        const [cls, dot, fallback] = MCP_STATES[s.state] || MCP_STATES.unknown;
        const row = el('div', 'onb-row mcp-row plg-vstup');
        const jmeno = s.name.replace(/^claude\.ai /, '');
        row.appendChild(window.HubPluginy ? HubPluginy.ikonka(jmeno, s.name.startsWith('claude.ai ') ? 'i-globe' : 'i-hub', HubPluginy.logoZUrl(s.target || '')) : el('span'));
        row.appendChild(Object.assign(el('span', 'mcp-dot ' + cls), {textContent: dot}));
        const col = el('span', 'onb-col');
        col.appendChild(el('span', null, jmeno + (s.name.startsWith('claude.ai ') ? '  · z účtu claude.ai' : '')));
        const where = [s.status || fallback];
        if (s.where) where.push(s.where);
        col.appendChild(el('small', null, where.join(' · ')));
        if (s.target) col.appendChild(el('small', 'mcp-target', s.target));
        row.appendChild(col);
        row.appendChild(el('span', 'spacer'));
        if (s.removable) {
          const del = el('button', 'set-x', '×');
          del.title = 'Odebrat napojení';
          del.onclick = async (ev) => {
            ev.stopPropagation();
            if (!(await HubDialog.confirm('Odebrat napojení ' + s.name + '?', {title: 'Odebrat napojení', ok: 'Odebrat'}))) return;
            try {
              await io.api('mcp', {action: 'remove', name: s.name});
              io.toast(s.name + ' odebrán.');
              load(true);
            } catch (err) { io.toast('Nepovedlo se: ' + err.message); }
          };
          row.appendChild(del);
        }
        list.appendChild(row);
      }, i * 45));
    }

    let drawTik = 0;
    async function load(refresh) {
      busy(refresh ? 'Ptám se serverů…' : 'Načítám…');
      let data;
      try {
        data = await io.api('mcp' + (refresh ? '?refresh=1' : ''));
        } catch (err) {
        summary.textContent = '';
        summary.appendChild(el('span', 'set-warn', 'Nepovedlo se: ' + err.message));
        check.disabled = false;
        return;
      }
      while (data.running) {
        busy('Zjišťuji, která napojení odpovídají… (každé se ptá zvlášť, chvíli to trvá)');
          await new Promise(r => setTimeout(r, 1200));
        try {
          data = await io.api('mcp');
        } catch (err) {
          summary.textContent = '';
          summary.appendChild(el('span', 'set-warn', 'Nepovedlo se: ' + err.message));
          check.disabled = false;
          return;
        }
      }
      draw(data);
    }

    check.onclick = () => load(true);
    if (mcpLast) draw(mcpLast);          // ať je hned vidět minulý výsledek
    load(false);                         // a na pozadí se dotáhne aktuální
    return box;
  }

  /* Hub na serveru (brána): zdroj patří serveru a novou verzi si server
     připraví sám (gateway/update.sh). Instalovat do domova prostoru nemá
     smysl — nabízí se jen přepnutí prostoru na připravenou verzi. */
  function aktualizaceServer() {
    const box = section('Aktualizace aplikace',
      'Novou verzi si server připraví sám, na pozadí — tvému prostoru se nic ' +
      'nevymění pod rukama. Když je hotová, aplikace nabídne Aktualizovat: ' +
      'přepne se a otevřené rozhovory se vrátí.');
    const row = el('div', 'set-row');
    row.appendChild(el('span', 'set-ver', 'Tvoje verze: ' + state.version.version));
    box.appendChild(row);
    // Připravenou novější verzi jde zapnout i odsud (hub.js, hubServerUpdate).
    fetch('/gw/verze', {credentials: 'same-origin'})
      .then((r) => (r.ok ? r.json() : null))
      .then((v) => {
        const nova = v && v.nejnovejsi;
        if (!nova || nova === state.version.version || !window.hubServerUpdate) return;
        const btns = el('div', 'onb-btns');
        const go = el('button', 'actionbtn', 'Přepnout na verzi ' + nova);
        go.onclick = () => window.hubServerUpdate();
        btns.appendChild(go);
        row.after(btns);
      }).catch(() => { /* starší brána */ });
    const status = el('div', 'set-status');
    status.textContent = 'Zjišťuju, jak dopadla poslední aktualizace serveru…';
    box.appendChild(status);
    io.api('update-check').then((v) => {
      const s = (v && v.server) || {};
      if (!s.checked_at) {
        status.textContent = 'Aktualizace serveru zatím neproběhla.';
        return;
      }
      const when = new Date(s.checked_at * 1000).toLocaleString('cs-CZ');
      status.className = 'set-status ' + (s.ok === false ? 'warn' : 'ok');
      status.textContent = `Naposledy ${when}: ${s.detail || ''}`;
    }).catch(() => { status.textContent = ''; });
    return box;
  }

  /* Diktování a předčítání česky (hub/hlas.py, hlas.js). Na počítači se
     instaluje odsud, na serveru ho připravuje správce pro všechny. */
  function hlas() {
    const box = section('Hlas', 'Mluv místo psaní a nech si odpovědi přečíst nahlas.');
    const status = el('div', 'set-status', 'Zjišťuju…');
    box.appendChild(status);
    const btns = el('div', 'onb-btns');
    box.appendChild(btns);
    const ready = el('div', 'hlas-ready');

    // Dlaždice s ikonkou: nadpis a krátký popisek pod ním.
    function tile(ico, title, sub, on) {
      const t = el('button', 'onb-tile hlas-tile' + (on ? ' on' : ''));
      t.innerHTML = '<svg class="hlas-ico"><use href="#' + ico + '"/></svg>';
      const col = el('span');
      col.append(el('span', 'onb-tile-t', title), el('span', 'onb-tile-s', sub));
      t.appendChild(col);
      return t;
    }
    function group(ico, title) {
      const h = el('div', 'hlas-head');
      h.innerHTML = '<svg class="ico"><use href="#' + ico + '"/></svg>';
      h.appendChild(el('span', null, title));
      ready.appendChild(h);
      const tiles = el('div', 'onb-tiles');
      ready.appendChild(tiles);
      return tiles;
    }

    function drawReady(s) {
      ready.textContent = '';
      // Přesnost, nebo rychlost přepisu (hub/hlas.py, MODELY).
      const prepis = group('i-mic', 'Diktování');
      const moznosti = [
        ['turbo', 'i-star', 'Přesně', 'méně chyb · asi 10 s'],
        ['small', 'i-bolt', 'Rychle', 'asi 3 s · víc chyb'],
      ];
      for (const [id, ico, nazev, popis] of moznosti) {
        const m = (s.models || {})[id] || {};
        const t = tile(ico, nazev, m.installed ? popis : 'tady není', s.model === id);
        t.disabled = !m.installed;
        t.onclick = async () => {
          await save({hlas_model: id});
          s.model = id;
          drawReady(s);
          if (window.HubHlas) HubHlas.refresh();
        };
        prepis.appendChild(t);
      }

    }

    /* Předčítání: místní Jirka, nebo hlas z ElevenLabs (hub/eleven.py).
       Kreslí se vždycky — ElevenLabs jde i bez nainstalovaného hlasu. */
    const cteniWrap = el('div', 'hlas-ready');
    const UKAZKA = 'Ahoj, tady je hub. Tímhle hlasem ti budu číst odpovědi.';
    const ELEVEN_SVG = '<svg class="hlas-ico" viewBox="0 0 24 24"><rect x="7" y="4" width="3.2" height="16" rx=".6"/>' +
                       '<rect x="13.8" y="4" width="3.2" height="16" rx=".6"/></svg>';
    let elevenForm = false;           // rozbalený formulář na klíč

    function playBtn(opts) {
      const play = el('span', 'hlas-play');
      play.title = 'Přehrát ukázku';
      play.setAttribute('role', 'button');
      // Během přípravy se točí kolečko, během čtení je tam stop.
      play.innerHTML = '<svg class="hlas-i-play"><use href="#i-play"/></svg>' +
                       '<svg class="hlas-i-stop"><use href="#i-stop"/></svg>' +
                       '<span class="hlas-spin"></span>';
      play.onclick = (ev) => {
        ev.stopPropagation();
        if (window.HubHlas) HubHlas.speak(UKAZKA, play, io.toast, opts);
      };
      return play;
    }

    async function elevenVyber(body, s) {
      try {
        const r = await io.api('hlas-eleven', Object.assign({action: 'vyber'}, body));
        if (r.ok === false) throw new Error(r.error || 'Nepovedlo se uložit.');
        s.eleven = r;
        s.cteni = s.ready || r.active;
      } catch (err) { io.toast(err.message); }
      drawCteni(s);
      if (window.HubHlas) HubHlas.refresh();
    }

    function drawCteni(s) {
      cteniWrap.textContent = '';
      const ev = s.eleven || {};
      const h = el('div', 'hlas-head');
      h.innerHTML = '<svg class="ico"><use href="#i-speak"/></svg>';
      h.appendChild(el('span', null, 'Předčítání'));
      cteniWrap.appendChild(h);
      const tiles = el('div', 'onb-tiles');
      cteniWrap.appendChild(tiles);

      // Jirka: místní hlas, zdarma, nic neodchází ven.
      const jirka = tile('i-speak', 'Jirka', s.ready ? 'v počítači · zdarma' : 'tady není',
                         s.ready && !ev.active);
      jirka.classList.add('hlas-voice');
      jirka.disabled = !s.ready;
      jirka.onclick = () => { if (ev.active) elevenVyber({active: false}, s); };
      if (s.ready) jirka.appendChild(playBtn({engine: 'piper'}));
      tiles.appendChild(jirka);

      // ElevenLabs: hlas z účtu, za kredity.
      const el11 = tile('i-speak', 'ElevenLabs',
                        ev.connected ? (ev.voice_name || 'vyber hlas') : 'napojit vlastním klíčem',
                        !!ev.active);
      el11.classList.add('hlas-voice');
      el11.querySelector('.hlas-ico').outerHTML = ELEVEN_SVG;
      el11.onclick = () => {
        if (!ev.connected) { elevenForm = !elevenForm; drawCteni(s); return; }
        if (!ev.active) elevenVyber({active: true}, s);
      };
      if (ev.connected && ev.voice_id) el11.appendChild(playBtn({voice_id: ev.voice_id, model_id: ev.model_id}));
      tiles.appendChild(el11);

      if (!ev.connected && elevenForm) cteniWrap.appendChild(elevenPripojit(s));
      if (ev.connected) cteniWrap.appendChild(elevenPanel(s));

      const row = el('label', 'onb-row hlas-auto');
      const cb = el('input');
      cb.type = 'checkbox';
      cb.checked = !!(window.HubHlas && HubHlas.auto.get());
      cb.disabled = !s.cteni;
      cb.onchange = () => { if (window.HubHlas) HubHlas.auto.set(cb.checked); };
      row.append(cb, el('span', null, 'Číst nové odpovědi samy'));
      row.title = 'Jen v tabu, na který se díváš, a jen na tomhle zařízení.';
      cteniWrap.appendChild(row);
    }

    // Formulář na API klíč (jako u Jevu): klíč se ověří, uloží jen k tobě
    // a do prohlížeče se už nevrací.
    function elevenPripojit(s) {
      const form = el('div', 'eleven-form');
      const steps = el('div', 'set-note');
      steps.textContent = 'V ElevenLabs otevři Developers → API Keys → Create Key ' +
        '(stačí práva Text to Speech a Voices: Read) a klíč vlož sem.';
      const open = el('button', 'btn ghost', 'Otevřít API klíče');
      open.onclick = () => io.open('https://elevenlabs.io/app/developers/api-keys');
      const row = el('label', 'mcp-field');
      row.appendChild(el('span', null, 'API klíč'));
      const key = el('input');
      key.type = 'password';
      key.placeholder = 'sk_…';
      key.autocomplete = 'off';
      key.spellcheck = false;
      row.appendChild(key);
      row.appendChild(el('small', null, 'Uloží se jen k tobě do ~/.claude/elevenlabs.json. ' +
        'Text odpovědí se pak posílá na ElevenLabs a čerpá kredity účtu.'));
      const status = el('div', 'set-note');
      const btns = el('div', 'onb-btns');
      const go = el('button', 'btn primary', 'Ověřit a napojit');
      async function napoj(body) {
        go.disabled = true;
        status.className = 'set-note';
        status.textContent = 'Ověřuju klíč a načítám hlasy…';
        try {
          const r = await io.api('hlas-eleven', Object.assign({action: 'save'}, body));
          if (r.ok === false) throw new Error(r.error || 'Napojení se nepovedlo.');
          s.eleven = r;
          s.cteni = true;
          elevenForm = false;
          elevenSeznam = r;
          io.toast('ElevenLabs napojený — odpovědi čte ' + (r.voice_name || 'vybraný hlas') + '.');
          drawCteni(s);
          if (window.HubHlas) HubHlas.refresh();
        } catch (err) {
          status.className = 'set-warn';
          status.textContent = err.message;
          go.disabled = false;
        }
      }
      go.onclick = () => napoj({api_key: key.value});
      key.onkeydown = (e) => { if (e.key === 'Enter') go.click(); };
      btns.append(go, open);
      form.append(steps, row, btns, status);
      setTimeout(() => key.focus(), 0);
      return form;
    }

    // Napojeno: výběr hlasu a modelu. Hlasy a modely se tahají z účtu.
    let elevenSeznam = null;
    function elevenPanel(s) {
      const ev = s.eleven || {};
      const panel = el('div', 'eleven-panel');
      const hlava = el('div', 'eleven-row');
      hlava.appendChild(el('span', 'eleven-lbl', 'Model'));
      const model = el('select', 'set-input eleven-model');
      hlava.appendChild(model);
      const seznam = el('div', 'eleven-voices');
      seznam.appendChild(el('div', 'set-note', 'Načítám hlasy z účtu…'));
      const pata = el('div', 'eleven-foot');
      pata.appendChild(el('span', 'set-note', 'Klíč ' + (ev.key_hint || '') +
        ' · další hlasy přidáš v knihovně ElevenLabs (Add to My Voices).'));
      const lib = el('button', 'btn ghost', 'Knihovna hlasů');
      lib.onclick = () => io.open('https://elevenlabs.io/app/voice-library?language=cs');
      const reload = el('button', 'btn ghost', 'Načíst znovu');
      reload.onclick = () => { elevenSeznam = null; nactiSeznam(true); };
      const off = el('button', 'btn ghost', 'Odpojit');
      off.onclick = async () => {
        if (!(await HubDialog.confirm('Odpojit ElevenLabs? Klíč se z tohohle počítače smaže ' +
            'a odpovědi bude číst zase místní hlas.', {title: 'Odpojit ElevenLabs', ok: 'Odpojit'}))) return;
        try {
          const r = await io.api('hlas-eleven', {action: 'remove'});
          s.eleven = r;
          s.cteni = s.ready;
          elevenSeznam = null;
        } catch (err) { io.toast(err.message); }
        drawCteni(s);
        if (window.HubHlas) HubHlas.refresh();
      };
      pata.append(lib, reload, off);
      panel.append(hlava, seznam, pata);

      function kresli(n) {
        model.textContent = '';
        for (const m of n.models || []) {
          // Flash začne mluvit nejdřív (~0,4 s), Multilingual v2 až za ~1,5 s.
          const o = el('option', null, m.name + (/flash/.test(m.id) ? ' · okamžitý start' : '') +
                                       (m.half ? ' · poloviční cena' : ''));
          o.value = m.id;
          o.selected = m.id === ev.model_id;
          model.appendChild(o);
        }
        model.onchange = () => elevenVyber({model_id: model.value}, s);
        seznam.textContent = '';
        for (const v of n.voices || []) {
          const r = el('div', 'eleven-voice' + (v.id === ev.voice_id ? ' on' : '') + (v.ok ? '' : ' off'));
          r.setAttribute('role', 'button');
          r.tabIndex = 0;
          const txt = el('span', 'eleven-vt');
          const nm = el('strong', null, v.name);
          txt.appendChild(nm);
          if (v.cs) txt.appendChild(el('span', 'eleven-tag', 'česky'));
          if (v.own) txt.appendChild(el('span', 'eleven-tag', 'tvůj'));
          const bits = [v.desc, v.gender === 'male' ? 'muž' : v.gender === 'female' ? 'žena' : '',
                        v.accent].filter(Boolean);
          if (!v.ok) bits.unshift('ještě se dotrénovává');
          if (bits.length) txt.appendChild(el('small', null, bits.join(' · ')));
          r.appendChild(txt);
          seznam.appendChild(r);
          if (!v.ok) { r.title = 'ElevenLabs tenhle hlas ještě dotrénovává — zatím nejde použít.'; continue; }
          r.appendChild(playBtn({voice_id: v.id, model_id: model.value || ev.model_id}));
          const vyber = () => { if (v.id !== ev.voice_id) elevenVyber({voice_id: v.id, active: true}, s); };
          r.onclick = vyber;
          r.onkeydown = (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); vyber(); } };
        }
        if (!(n.voices || []).length) seznam.appendChild(el('div', 'set-note', 'Na účtu nejsou žádné hlasy.'));
      }
      async function nactiSeznam(fresh) {
        if (elevenSeznam && elevenSeznam.voices) return kresli(elevenSeznam);
        try {
          const n = await io.api('hlas-eleven' + (fresh ? '?fresh=1' : ''));
          if (n.ok === false) throw new Error(n.error);
          elevenSeznam = n;
          kresli(n);
        } catch (err) {
          seznam.textContent = '';
          seznam.appendChild(el('div', 'set-warn', err.message));
        }
      }
      nactiSeznam(false);
      return panel;
    }

    let poll = null;
    async function nacti() {
      let s;
      try { s = await io.api('hlas'); } catch (_) { s = {ready: false}; }
      btns.textContent = '';
      drawCteni(s);
      if (s.ready) {
        status.hidden = true;
        drawReady(s);
        if (!ready.parentNode) box.insertBefore(ready, cteniWrap);
        if (window.HubHlas) HubHlas.refresh();
        return;
      }
      status.hidden = false;
      if (ready.parentNode) ready.remove();
      const job = s.job || {};
      if (job.running || s.installing) {
        status.className = 'set-status';
        status.textContent = 'Instaluju: ' + (job.step || 'začínám…');
        clearTimeout(poll);
        poll = setTimeout(nacti, 2000);
        return;
      }
      if (job.done && job.result && job.result.ok === false) {
        status.className = 'set-status warn';
        status.textContent = 'Instalace se nepovedla: ' + (job.result.detail || '');
      } else if (!s.can_install) {
        status.className = 'set-status warn';
        status.textContent = 'Na serveru hlas ještě není — přibude s další ' +
          'aktualizací serveru.';
        return;
      } else {
        status.className = 'set-status';
        status.textContent = 'Hlas tu zatím není. Instalace stáhne asi 1 GB ' +
          '(knihovny a modely) a zabere pár minut.';
      }
      const go = el('button', 'actionbtn', 'Nainstalovat hlas');
      go.onclick = async () => {
        go.disabled = true;
        try { await io.api('hlas-install', {}); } catch (err) { io.toast(err.message); }
        nacti();
      };
      btns.appendChild(go);
    }
    box.appendChild(cteniWrap);
    nacti();
    return box;
  }

  function aktualizace() {
    if (state.config && state.config.gateway_user) return aktualizaceServer();
    const box = section('Aktualizace aplikace',
      'Stáhne novou verzi aplikace a nainstaluje ji. Otevřené rozhovory se po ' +
      'restartu vrátí.');
    const info = el('span', 'set-ver', 'Nainstalováno: ' + state.version.version);
    const infoRow = el('div', 'set-row');
    infoRow.appendChild(info);
    box.appendChild(infoRow);

    const autoRow = el('label', 'onb-row');
    const autoCb = el('input');
    autoCb.type = 'checkbox';
    autoCb.checked = !(state.config && state.config.auto_update === false);
    autoCb.onchange = () => { if (state.config) state.config.auto_update = autoCb.checked; save({auto_update: autoCb.checked}); };
    autoRow.appendChild(autoCb);
    const autoCol = el('span', 'onb-col');
    autoCol.appendChild(el('span', null, 'Aktualizovat automaticky při spuštění'));
    autoCol.appendChild(el('small', null, 'Při každém startu se podívá, jestli je novější verze, stáhne ji a nainstaluje. ' +
      'Otevřené rozhovory se po restartu vrátí.'));
    autoRow.appendChild(autoCol);
    box.appendChild(autoRow);

    const status = el('div', 'set-status');
    box.appendChild(status);

    const btns = el('div', 'onb-btns');
    const check = el('button', 'actionbtn', 'Zjistit, jestli je novější');
    const doIt = el('button', 'actionbtn', 'Aktualizovat');
    doIt.hidden = true;

    check.onclick = async () => {
      check.disabled = true;
      status.className = 'set-status busy';
      status.textContent = 'Kontroluju…';
      try {
        const v = await io.api('update-check');
        if (!v.latest) {
          status.className = 'set-status warn';
          status.textContent = v.why || 'Nepodařilo se zjistit.';
        } else if (v.update_available) {
          status.className = 'set-status warn';
          status.textContent = `Je k dispozici nová verze ${v.latest} (máš ${v.version}).`;
          doIt.hidden = false;
        } else {
          status.className = 'set-status ok';
          status.textContent = `Máš nejnovější verzi (${v.version}).`;
        }
      } catch (err) {
        status.className = 'set-status warn';
        status.textContent = 'Kontrola selhala: ' + err.message;
      } finally {
        check.disabled = false;
      }
    };

    // Aktualizace běží na serveru na pozadí a stav se odečítá — dřív to byl
    // jeden dlouhý požadavek a stránka na něm zůstala viset s „Stahuju…".
    async function watchUpdate() {
      for (;;) {
        await new Promise(r => setTimeout(r, 1200));
        let st;
        try {
          st = await io.api('update-status');
        } catch (err) {
          status.className = 'set-status warn';
          status.textContent = 'Ztratil jsem spojení se serverem: ' + err.message;
          return;
        }
        if (st.running) {
          status.textContent = 'Aktualizuju… ' + (st.step || '');
          continue;
        }
        const r = st.result || {};
        if (!r.ok) {
          status.className = 'set-status warn';
          status.textContent = r.detail || 'Aktualizace se nepovedla.';
        } else if (r.changed) {
          status.className = 'set-status ok';
          doIt.hidden = true;
          info.textContent = 'Nainstalováno: ' + r.now;
          /* Nová verze se projeví až po startu, tak si hub sáhne na restart
             sám. Otevřené taby se před tím uloží a nové okno je zase otevře —
             u Clauda se pokračuje v téže konverzaci (`--resume`). */
          status.textContent = `✓ Nainstalována verze ${r.now}. Restartuju…`;
          try {
            const res = await io.api('restart');
            status.textContent = res && res.tabs
              ? `✓ Verze ${r.now}. Restartuju a vracím ${res.tabs} ` +
                (res.tabs === 1 ? 'rozhovor…' : res.tabs < 5 ? 'rozhovory…' : 'rozhovorů…')
              : `✓ Verze ${r.now}. Restartuju…`;
          } catch (err) {
            status.className = 'set-status warn';
            status.textContent = `✓ Nainstalována verze ${r.now}, ale restart se ` +
              'nepovedl: ' + err.message + ' Zavři a znovu otevři aplikaci.';
          }
        } else {
          status.className = 'set-status ok';
          status.textContent = '✓ ' + (r.detail || 'Nic nového.');
          doIt.hidden = true;
        }
        doIt.disabled = false;
        check.disabled = false;
        await io.refreshState();
        state = io.state;
        return;
      }
    }

    doIt.onclick = async () => {
      doIt.disabled = true;
      check.disabled = true;
      status.className = 'set-status busy';
      status.textContent = 'Aktualizuju…';
      try {
        await io.api('update');
      } catch (err) {
        status.className = 'set-status warn';
        status.textContent = 'Nepodařilo se spustit: ' + err.message;
        doIt.disabled = false;
        check.disabled = false;
        return;
      }
      watchUpdate();
    };

    // Když se stránka načte během běžící aktualizace, navážeme na ni. Jinak
    // se hned zeptáme na novou verzi — kdo sem přišel přes „Nová verze",
    // má ji vidět a jen kliknout. Tlačítko Aktualizovat na úvodu ji rovnou
    // i spustí (startUpdate).
    const start = !!io.startUpdate;
    io.startUpdate = false;
    io.api('update-status').then(async (st) => {
      if (st.running) {
        status.className = 'set-status busy';
        status.textContent = 'Aktualizuju…';
        doIt.disabled = true;
        check.disabled = true;
        watchUpdate();
        return;
      }
      await check.onclick();
      if (start && !doIt.hidden) doIt.onclick();
    }).catch(() => {});

    // Co v které verzi přibylo — vydání na GitHubu.
    const gh = el('button', 'actionbtn', 'Co je nového');
    gh.hidden = !advanced();
    gh.onclick = () => io.open('https://github.com/' +
      ((state.version && state.version.repo) || 'jurapascal/claude-code-hub') + '/releases');

    btns.appendChild(check);
    btns.appendChild(doIt);
    btns.appendChild(gh);
    box.appendChild(btns);
    return box;
  }

  function logy() {
    const box = section('Logy',
      'Co se v aplikaci dělo — starty, otevřené taby, běhy na pozadí a chyby. ' +
      'Leží to v ~/.claude/hub.log a nikam se to samo neposílá.');
    const view = el('pre', 'log-view', 'Načítám…');
    box.appendChild(view);

    const btns = el('div', 'onb-btns');
    const refresh = el('button', 'actionbtn', 'Načíst znovu');
    const copy = el('button', 'actionbtn', 'Zkopírovat hlášení');
    const save = el('button', 'actionbtn', 'Uložit hlášení do souboru');
    const clear = el('button', 'actionbtn', 'Vymazat log');

    const load = async () => {
      view.textContent = 'Načítám…';
      try {
        const r = await io.api('log?lines=400');
        const lines = r.lines || [];
        view.textContent = '';
        if (!lines.length) { view.textContent = '(log je prázdný)'; return; }
        for (const line of lines) {
          const row = el('span', 'log-line');
          if (/\bERROR\b/.test(line)) row.classList.add('err');
          else if (/\bWARN\b/.test(line)) row.classList.add('warn');
          row.textContent = line;
          view.appendChild(row);
        }
        view.scrollTop = view.scrollHeight;   // konec je to zajímavé
      } catch (err) {
        view.textContent = 'Nepovedlo se: ' + err.message;
      }
    };

    refresh.onclick = load;
    copy.onclick = async () => {
      try {
        const r = await io.api('report');
        // Přes io.copy: na serveru by /api/clipboard psal do schránky serveru.
        await io.copy(r.text);
        io.toast('Hlášení je ve schránce — stačí vložit.');
      } catch (err) { io.toast('Nepovedlo se: ' + err.message); }
    };
    save.onclick = async () => {
      try {
        const r = await io.api('report');
        // Uloží se stejnou cestou jako přiložené obrázky, takže se pak dá
        // rovnou přetáhnout nebo poslat.
        const data = btoa(unescape(encodeURIComponent(r.text)));
        const out = await io.api('upload', {name: 'hlaseni-hub.txt', data});
        io.toast('Uloženo: ' + out.path);
      } catch (err) { io.toast('Nepovedlo se: ' + err.message); }
    };
    clear.onclick = async () => {
      if (!(await HubDialog.confirm('Vymazat log?', {title: 'Vymazat log', ok: 'Vymazat'}))) return;
      try { await io.api('log-clear', {}); await load(); }
      catch (err) { io.toast(err.message); }
    };

    for (const b of [refresh, copy, save, clear]) btns.appendChild(b);
    box.appendChild(btns);
    load();
    return box;
  }

  async function save(updates) {
    try {
      await io.api('config', updates);
      // Hub si obnoví svůj stav (kvůli liště tlačítek pod nastavením)…
      await io.refreshState();
      // …ale `io.state` je zmrazená kopie: nastavení se otevírá přes
      // `{...hubIO(), state: STATE}` a spread getter `state` vyhodnotí na
      // hodnotu. Čerstvá data si tedy musíme vzít sami, jinak se panel
      // překreslí ze starých — a přepnutí, které mění, co je vidět, vypadá
      // jako by nefungovalo.
      state = await io.api('state');
      render();
    } catch (err) {
      io.toast(err.message);
    }
  }

  function close() {
    if (root) root.remove();
    root = null;
    io.reload();
  }

  global.HubSettings = {open, close};

})(window);
