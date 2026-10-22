/* Pluginy — Nastavení → Pluginy a běh pluginů appky (hub/pluginy.py).
 *
 * Dvě záložky:
 *  - Claude Code: katalog z marketplace (instalace jedním klikem),
 *    nainstalované (zapnout / vypnout / aktualizovat / odinstalovat)
 *    a správa marketplace. Dělá to `claude plugin …` na serveru hubu.
 *  - Pro appku: rozšíření samotného hubu. Plugin je složka s plugin.json
 *    a JS/CSS; po zapnutí se načte do okna a dostane API:
 *
 *      HubPluginy.registruj('moje-id', (api) => {
 *        api.akce({label: 'Pozdrav', run: () => api.toast('Ahoj!')});
 *        api.on('nastroj', (e) => console.log('Claude spustil', e.name));
 *        api.on('odeslano', (e) => …);   // zpráva z pole na psaní
 *        api.on('tab', (e) => …);        // přepnutý tab
 *        api.on('prace', (e) => …);      // Claude začal / přestal pracovat
 *        api.posli('text');              // pošle zprávu do aktivního chatu
 *        api.aktivniTab();               // {id, title, path} nebo null
 *        api.uloziste.set('k', 1); api.uloziste.get('k');
 *        api.hub('state');               // volání API hubu (plná práva!)
 *        api.nastaveni((box) => { … });  // vlastní nastavení v Pluginy
 *      });
 *
 *    Plugin běží se stejnými právy jako appka — proto je po přidání vypnutý
 *    a zapnout ho jde jen vědomě.
 */
'use strict';

(function (global) {
  let io = null;                    // {api, toast, posli, aktivniTab, renderActions}
  const registrace = new Map();     // id → {api, nastaveni}
  const posluchaci = new Map();     // událost → [{id, fn}]
  const akceSeznam = [];            // tlačítka v Rychlých akcích
  const nactene = new Set();
  let zakladni = null;              // seznam pluginů appky z poslední odpovědi

  function el(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }

  /* ── běh pluginů appky ──────────────────────────────────────────────── */
  function apiPro(id) {
    const klic = (k) => 'hub-plugin:' + id + ':' + k;
    return {
      id,
      toast: (t) => io.toast(String(t)),
      akce: ({label, run, title}) => {
        akceSeznam.push({id, label: String(label || id), title: title || '', run});
        if (io.renderActions) io.renderActions();
      },
      on: (udalost, fn) => {
        if (!posluchaci.has(udalost)) posluchaci.set(udalost, []);
        posluchaci.get(udalost).push({id, fn});
      },
      posli: (text) => io.posli(String(text || '')),
      aktivniTab: () => io.aktivniTab(),
      uloziste: {
        get: (k) => { try { return JSON.parse(localStorage.getItem(klic(k))); } catch (_) { return null; } },
        set: (k, v) => { try { localStorage.setItem(klic(k), JSON.stringify(v)); } catch (_) { /* plné */ } },
      },
      hub: (path, body) => io.api(path, body),
      nastaveni: (fn) => { const r = registrace.get(id); if (r) r.nastaveni = fn; },
    };
  }

  function registruj(id, init) {
    id = String(id || '');
    if (!id || registrace.has(id)) return;
    const reg = {api: apiPro(id), nastaveni: null};
    registrace.set(id, reg);
    try { init(reg.api); }
    catch (err) {
      console.error('plugin ' + id, err);
      io.toast('Plugin „' + id + '“ spadl při startu: ' + (err && err.message || err));
    }
  }

  function vysli(udalost, data) {
    for (const {id, fn} of posluchaci.get(udalost) || []) {
      try { fn(data); } catch (err) { console.error('plugin ' + id + ' (' + udalost + ')', err); }
    }
  }

  const url = (id, f) => '/api/plugin-soubor?id=' + encodeURIComponent(id) + '&f=' + encodeURIComponent(f) +
                         '&t=' + encodeURIComponent(io.token || '');

  function nactiPlugin(p) {
    if (!p.enabled || nactene.has(p.id)) return;
    nactene.add(p.id);
    if (p.style) {
      const l = el('link');
      l.rel = 'stylesheet';
      l.href = url(p.id, p.style);
      l.dataset.plugin = p.id;
      document.head.appendChild(l);
    }
    if (p.main) {
      const s = el('script');
      s.src = url(p.id, p.main);
      s.dataset.plugin = p.id;
      s.onerror = () => io.toast('Plugin „' + p.name + '“ se nenačetl.');
      document.body.appendChild(s);
    }
  }

  async function start(opts) {
    io = opts;
    // Události hubu → pluginy.
    document.addEventListener('hub-tool', (ev) => vysli('nastroj', ev.detail || {}));
    document.addEventListener('hub-odeslano', (ev) => vysli('odeslano', ev.detail || {}));
    document.addEventListener('hub-tab', (ev) => vysli('tab', ev.detail || {}));
    document.addEventListener('hub-prace', (ev) => vysli('prace', ev.detail || {}));
    try {
      zakladni = await io.api('pluginy?jen=appka');
      if (zakladni.allowed) for (const p of zakladni.plugins || []) nactiPlugin(p);
    } catch (_) { /* starší hub — pluginy nejsou */ }
  }

  /* ── Nastavení → Pluginy ────────────────────────────────────────────── */
  function sekce() {
    const box = el('div', 'set-sec plg');
    box.appendChild(el('div', 'set-title', 'Pluginy'));
    const tabs = el('div', 'plg-tabs');
    const telo = el('div', 'plg-telo');
    let kde = localStorage.getItem('hub-plg-tab') || 'cc';
    const prepni = (k) => {
      kde = k;
      try { localStorage.setItem('hub-plg-tab', k); } catch (_) { /* nic */ }
      for (const b of tabs.children) b.classList.toggle('on', b.dataset.k === k);
      telo.textContent = '';
      telo.appendChild(k === 'cc' ? claudeCode() : appka());
    };
    for (const [k, t] of [['cc', 'Claude Code'], ['appka', 'Pro appku']]) {
      const b = el('button', 'plg-tab', t);
      b.dataset.k = k;
      b.onclick = () => prepni(k);
      tabs.appendChild(b);
    }
    box.append(tabs, telo);
    prepni(kde);
    return box;
  }

  function radek(nazev, popis, meta, ico, logo) {
    const r = el('div', 'plg-radek');
    r.appendChild(ikonka(nazev, ico, logo));
    const info = el('div', 'plg-info');
    info.appendChild(el('b', '', nazev));
    if (meta) info.appendChild(el('span', 'plg-meta', meta));
    if (popis) { const d = el('div', 'plg-popis', popis); d.title = popis; info.appendChild(d); }
    r.appendChild(info);
    const tlacitka = el('div', 'plg-btns');
    r.appendChild(tlacitka);
    return {r, tlacitka};
  }

  function tlacitko(text, fn, cls) {
    const b = el('button', 'btn ' + (cls || 'ghost'), text);
    b.onclick = async () => {
      b.disabled = true;
      try { await fn(); } finally { b.disabled = false; }
    };
    return b;
  }

  /* Ikonka: barevný čtvereček s písmenem (barva z názvu) a kategorie podle slov. */
  const KATEGORIE = [
    ['design', 'Design', 'i-image', /design|figma|ui\b|ux|css|frontend|canva|logo|color|palette/i],
    ['kod', 'Kód a review', 'i-terminal', /code|review|debug|test|refactor|lint|typescript|python|git\b|github|pull request|tdd/i],
    ['data', 'Data a API', 'i-chart', /database|sql|data|api\b|postgres|supabase|analytics|openapi|query/i],
    ['web', 'Web a prohlížeč', 'i-globe', /browser|playwright|web|seo|scrap|search|crawl|http/i],
    ['produktivita', 'Práce a komunikace', 'i-note', /slack|notion|linear|jira|asana|email|calendar|docs|project|task|workflow|agent/i],
    ['bezpecnost', 'Bezpečnost', 'i-lock', /security|vulnerab|owasp|secret|auth|audit/i],
  ];
  const kategorie = (p) => (KATEGORIE.find((k) => k[3].test(p.name + ' ' + p.description)) || ['ostatni', 'Ostatní', 'i-star'])[0];
  const katInfo = (id) => KATEGORIE.find((k) => k[0] === id) || ['ostatni', 'Ostatní', 'i-star'];
  /* Ikonka řádku: barevný čtvereček (barva z názvu) s ikonkou appky
     (`i-…` ze sady v index.html), nebo s prvním písmenem názvu. */
  function ikonka(nazev, ico, logo) {
    let h = 0;
    for (const c of nazev) h = (h * 31 + c.charCodeAt(0)) % 360;
    const i = el('span', 'plg-ikona');
    if (ico && ico.startsWith('i-')) i.innerHTML = '<svg class="ico"><use href="#' + ico + '"/></svg>';
    else i.textContent = (nazev.replace(/^[^A-Za-zÀ-ž0-9]+/, '')[0] || '?').toUpperCase();
    i.style.background = 'hsl(' + h + ' 45% 32%)';
    // Skutečné logo vydavatele / služby (hub ho stáhne a uloží); bez něj zůstává ikonka.
    if (logo && io) {
      const img = el('img', 'plg-logo');
      img.alt = '';
      img.onload = () => { i.classList.add('s-logem'); i.textContent = ''; i.appendChild(img); };
      img.src = '/api/logo?k=' + encodeURIComponent(logo) + '&t=' + encodeURIComponent(io.token || '');
    }
    return i;
  }
  /* Klíč loga z adresy serveru — doménu značky určuje hub (hub/pluginy.py logo). */
  function logoZUrl(url) {
    let host = '';
    try { host = new URL(/^https?:/.test(url) ? url : 'https://' + url).hostname.toLowerCase(); } catch (_) { return ''; }
    return /^[a-z0-9.-]{3,100}$/.test(host) && host.includes('.') ? 'h:' + host : '';
  }
  const svg = (ico) => '<svg class="ico"><use href="#' + ico + '"/></svg>';

  const kolik = (n) => n >= 1e6 ? (n / 1e6).toFixed(1).replace('.0', '') + ' mil.' : n >= 1000 ? Math.round(n / 1000) + ' tis.' : String(n);

  function claudeCode(mode) {
    const wrap = el('div');
    const objevit = mode === 'objevit';
    wrap.appendChild(el('div', 'set-note', objevit
      ? 'Katalog pluginů — instalace jedním klikem, projeví se v nově otevřených chatech.'
      : 'Pluginy přidávají Claudovi dovednosti, příkazy, pomocníky a napojení.'));
    const stav = el('div', 'set-note', 'Načítám katalog…');
    wrap.appendChild(stav);
    const obsah = el('div');
    wrap.appendChild(obsah);
    let data = null;

    async function akce(body, potvrzeni) {
      try {
        const out = await io.api('pluginy', body);
        if (out.potvrdit) {
          const ok = (await HubDialog.confirm(out.message + '\n\nPříkaz:\n' + out.potvrdit + '\n\nSpustit a nainstalovat?', {title: 'Instalace spustí příkaz', ok: 'Spustit a nainstalovat'}));
          if (ok) return akce({...body, sha: out.sha});
          return;
        }
        io.toast(out.message || 'Hotovo.');
        await nacti(false);
      } catch (err) {
        io.toast(err.message || 'Nepovedlo se.');
      }
    }

    async function nacti(obnovit) {
      try {
        const res = await io.api('pluginy' + (obnovit ? '?refresh=1' : ''));
        data = res.cc;
        stav.textContent = data.error ? 'Katalog se nenačetl: ' + data.error : '';
        stav.hidden = !data.error;
        kresli();
      } catch (err) {
        stav.textContent = 'Katalog se nenačetl: ' + err.message;
      }
    }

    function kresli() {
      obsah.textContent = '';
      // Moje: nainstalované
      if (!objevit && !data.installed.length) obsah.appendChild(el('div', 'set-note', 'Zatím žádné pluginy. Vyber si v záložce Objevit.'));
      for (const p of (objevit ? [] : data.installed)) {
        const [jmeno, trh] = p.id.split('@');
        const vKatalogu = data.available.find((a) => a.id === p.id);
        const {r, tlacitka} = radek(jmeno, vKatalogu ? vKatalogu.description : '', (trh || '') + (p.scope === 'project' ? ' · jen v projektu ' + (p.projectPath || '').split(/[\\/]/).pop() : '') +
                                    (p.enabled ? ' · zapnutý' : ' · vypnutý'),
                                    katInfo(vKatalogu ? kategorie(vKatalogu) : 'ostatni')[2], vKatalogu && vKatalogu.logo);
        if (p.scope === 'user') {
          tlacitka.appendChild(tlacitko(p.enabled ? 'Vypnout' : 'Zapnout', () => akce({akce: p.enabled ? 'disable' : 'enable', id: p.id})));
          tlacitka.appendChild(tlacitko('Aktualizovat', () => akce({akce: 'update', id: p.id})));
          tlacitka.appendChild(tlacitko('Odinstalovat', async () => (await HubDialog.confirm('Odinstalovat „' + jmeno + '“?', {title: 'Odinstalovat plugin', ok: 'Odinstalovat'})) && akce({akce: 'uninstall', id: p.id}), 'danger'));
        } else {
          tlacitka.appendChild(tlacitko('Nainstalovat všude', () => akce({akce: 'install', id: p.id})));
        }
        obsah.appendChild(r);
      }
      if (!objevit) {
        // Katalogy (marketplace) patří k mým pluginům — odkud se berou.
        obsah.appendChild(el('div', 'set-title plg-podnadpis', 'Katalogy (marketplace)'));
        for (const m of data.marketplaces) obsah.appendChild(radek(m.name, '', m.repo || m.url || m.source || '', 'i-folder').r);
        const pridat = el('div', 'set-row');
        const zdroj = el('input', 'set-input');
        zdroj.placeholder = 'GitHub owner/repo nebo adresa katalogu';
        pridat.append(zdroj, tlacitko('Přidat katalog', () => zdroj.value.trim() && akce({akce: 'marketplace-add', zdroj: zdroj.value.trim()})));
        pridat.appendChild(tlacitko('Aktualizovat katalogy', () => akce({akce: 'marketplace-update'})));
        obsah.appendChild(pridat);
        return;
      }
      const hledat = el('input', 'set-input plg-hledat');
      hledat.placeholder = 'Hledat plugin — třeba „figma", „github", „design"…';
      const radka = el('div', 'plg-filtry');
      let kat = '', razeni = 'popularita';
      const chipy = el('div', 'plg-chipy');
      const mkChip = (id, text, ico) => {
        const c = el('button', 'plg-chip' + (kat === id ? ' on' : ''));
        c.innerHTML = (ico ? svg(ico) : '') + '<span></span>';
        c.querySelector('span').textContent = text;
        c.onclick = () => { kat = id; for (const x of chipy.children) x.classList.toggle('on', x.dataset.k === id); vypis(); };
        c.dataset.k = id;
        chipy.appendChild(c);
      };
      mkChip('', 'Vše');
      for (const k of KATEGORIE) mkChip(k[0], k[1], k[2]);
      mkChip('ostatni', 'Ostatní', 'i-star');
      const sel = el('select', 'set-input plg-razeni');
      for (const [v, t] of [['popularita', 'Nejoblíbenější'], ['nazev', 'Podle abecedy']]) sel.appendChild(Object.assign(el('option', '', t), {value: v}));
      sel.onchange = () => { razeni = sel.value; vypis(); };
      radka.append(chipy, sel);
      obsah.append(hledat, radka);
      const seznam = el('div', 'plg-seznam');
      obsah.appendChild(seznam);
      let token = 0;
      const vypis = () => {
        const q = hledat.value.trim().toLowerCase();
        const mine = ++token;
        let hit = data.available.filter((p) => (!q || (p.name + ' ' + p.description + ' ' + p.marketplace).toLowerCase().includes(q)) &&
                                               (!kat || kategorie(p) === kat));
        if (razeni === 'nazev') hit = hit.slice().sort((x, y) => x.name.localeCompare(y.name));
        seznam.textContent = '';
        if (!hit.length) { seznam.appendChild(el('div', 'set-note', 'Nic takového v katalogu není.')); return; }
        const celkem = Math.min(hit.length, 120);
        let i = 0;
        const dalsi = () => {            // po jednom, postupně — okno se nezasekne a řádky naskakují
          if (mine !== token) return;
          const dalsiHit = hit[i++];
          const {r, tlacitka} = radek(dalsiHit.name, dalsiHit.description, dalsiHit.marketplace + ' · ' + kolik(dalsiHit.installs) + ' instalací',
                                      katInfo(kategorie(dalsiHit))[2], dalsiHit.logo);
          r.classList.add('plg-vstup');
          if (data.installed.some((x) => x.id === dalsiHit.id && x.scope === 'user')) {
            tlacitka.appendChild(el('span', 'set-ok', '✓ Nainstalováno'));
          } else {
            tlacitka.appendChild(tlacitko('Nainstalovat', () => akce({akce: 'install', id: dalsiHit.id}), 'primary'));
          }
          seznam.appendChild(r);
          if (i < celkem) setTimeout(dalsi, 25);
          else if (hit.length > celkem) seznam.appendChild(el('div', 'set-note', 'A dalších ' + (hit.length - celkem) + ' — upřesni hledání nebo vyber kategorii.'));
        };
        dalsi();
      };
      let t = null;
      hledat.oninput = () => { clearTimeout(t); t = setTimeout(vypis, 150); };
      vypis();
    }
    nacti(false);
    return wrap;
  }

  /* Skilly: co Claude umí. Moje příkazy (slash), ze zapnutých pluginů a
     postupy v Obsidian Brainu, které si Claude načte, když je potřebuje. */
  const ZDROJE = [['moje', 'Moje příkazy', 'i-bolt'], ['plugin', 'Z pluginů', 'i-plus'], ['obsidian', 'Z Obsidianu', 'i-book']];
  const ZDROJ_JMENO = {moje: 'moje příkazy', plugin: 'plugin', obsidian: 'Obsidian'};
  const stari = (ts) => {
    if (!ts) return '';
    const d = Math.floor((Date.now() / 1000 - ts) / 86400);
    return d < 1 ? 'dnes' : d === 1 ? 'včera' : d < 30 ? 'před ' + d + ' dny' : new Date(ts * 1000).toLocaleDateString('cs-CZ');
  };
  const velikost = (n) => n < 1024 ? n + ' B' : Math.round(n / 1024) + ' kB';

  function skilly() {
    const wrap = el('div');
    const seznamBox = el('div');
    const detailBox = el('div');
    detailBox.hidden = true;
    wrap.append(seznamBox, detailBox);

    /* Detail skillu jako v claude.ai: popis, obsah (soubory), vyzkoušet v chatu. */
    async function detail(x) {
      seznamBox.hidden = true;
      detailBox.hidden = false;
      detailBox.textContent = '';
      const zpet = el('button', 'plg-zpet');
      zpet.innerHTML = svg('i-chevron') + '<span>Skilly</span>';
      zpet.onclick = () => { detailBox.hidden = true; seznamBox.hidden = false; };
      detailBox.appendChild(zpet);
      const hlava = el('div', 'plg-detail-hlava');
      hlava.appendChild(ikonka(x.name, (ZDROJE.find((z) => z[0] === x.source) || [])[2]));
      const jm = el('div', 'plg-info');
      jm.appendChild(el('b', 'plg-detail-nazev', x.source === 'moje' ? '/' + x.name : x.name));
      const meta = el('span', 'plg-meta plg-detail-meta', 'z ' + ZDROJ_JMENO[x.source] + (x.plugin ? ' ' + x.plugin : '') + (x.category ? ' · ' + x.category : ''));
      jm.appendChild(meta);
      hlava.appendChild(jm);
      const vyzkouset = tlacitko('Vyzkoušet v chatu', async () => {
        const text = x.source === 'moje' ? '/' + x.name : 'Použij skill „' + x.name + '“.';
        if (!io.posli(text)) io.toast('Nejdřív otevři chat, ve kterém to má Claude zkusit.');
        else io.toast('Poslal jsem to Claudovi do aktivního chatu.');
      }, 'primary');
      hlava.appendChild(vyzkouset);
      detailBox.appendChild(hlava);
      const tabs = el('div', 'plg-detail-tabs');
      const telo = el('div', 'plg-detail-telo');
      detailBox.append(tabs, telo);
      telo.appendChild(el('div', 'set-note', 'Načítám…'));
      let d;
      try {
        d = await io.api('pluginy?jen=skill&zdroj=' + encodeURIComponent(x.source) + '&name=' + encodeURIComponent(x.name) +
                         '&kde=' + encodeURIComponent(x.plugin || x.category || ''));
      } catch (err) { telo.textContent = 'Detail se nenačetl: ' + err.message; return; }
      if (d.error) { telo.textContent = d.error; return; }
      if (d.updated) meta.textContent += ' · upraveno ' + stari(d.updated);
      const prehled = () => {
        telo.textContent = '';
        const sloupce = el('div', 'plg-detail-sloupce');
        const levy = el('div');
        levy.appendChild(el('div', 'plg-detail-stitek', 'Popis'));
        levy.appendChild(el('div', 'plg-detail-popis', d.description || 'Skill nemá popis.'));
        const pravy = el('div', 'plg-detail-bok');
        pravy.appendChild(el('div', 'plg-detail-stitek', 'Zdroj'));
        pravy.appendChild(el('div', '', ZDROJ_JMENO[x.source][0].toUpperCase() + ZDROJ_JMENO[x.source].slice(1) + (x.plugin ? ' · ' + x.plugin : '') + (x.category ? ' · ' + x.category : '')));
        pravy.appendChild(el('div', 'plg-detail-stitek', 'Jak se používá'));
        pravy.appendChild(el('div', 'set-note', x.source === 'moje' ? 'Napiš /' + x.name + ' do chatu.' : 'Claude si ho vezme sám, když se hodí — nebo ho zmiň v chatu.'));
        sloupce.append(levy, pravy);
        telo.appendChild(sloupce);
      };
      const obsah = () => {
        telo.textContent = '';
        for (const f of d.files) {
          const r = el('div', 'plg-soubor');
          r.append(el('span', '', f.path), el('span', 'plg-meta', velikost(f.size)));
          telo.appendChild(r);
        }
        telo.appendChild(el('div', 'plg-detail-stitek', 'SKILL.md'));
        const pre = el('pre', 'plg-detail-kod');
        pre.textContent = d.text;
        telo.appendChild(pre);
      };
      for (const [id, text, fn] of [['p', 'Přehled', prehled], ['o', 'Obsah · ' + d.files.length, obsah]]) {
        const b = el('button', 'plg-detail-tab', text);
        b.onclick = () => { for (const t of tabs.children) t.classList.toggle('on', t === b); fn(); };
        tabs.appendChild(b);
      }
      tabs.firstChild.click();
    }

    seznamBox.appendChild(el('div', 'set-note',
      'Skilly jsou návody, podle kterých Claude dělá konkrétní práci. Moje příkazy spustíš lomítkem ' +
      '(třeba /push), ostatní si Claude vezme sám, když se hodí. Klikni na skill pro detail.'));
    const hledat = el('input', 'set-input plg-hledat');
    hledat.placeholder = 'Hledat skill — třeba „newsletter", „seo", „deploy"…';
    const filtry = el('div', 'plg-filtry');
    const chipy = el('div', 'plg-chipy');
    filtry.appendChild(chipy);
    const seznam = el('div', 'plg-seznam');
    seznamBox.append(hledat, filtry, seznam);

    const vse = [];                       // co už se načetlo
    const zobrazene = new Set();          // skilly, které už mají řádek
    let zdroj = '';
    let fronta = [];                      // řádky čekající na zobrazení (po jednom)
    let bezi = false, tik = 0;
    const pocty = {};
    const chip = (id, text, ico) => {
      const c = el('button', 'plg-chip' + (zdroj === id ? ' on' : ''));
      c.innerHTML = (ico ? svg(ico) : '') + '<span></span>';
      c.dataset.k = id;
      c.onclick = () => { zdroj = id; for (const x of chipy.children) x.classList.toggle('on', x.dataset.k === id); prekresli(); };
      chipy.appendChild(c);
      return c;
    };
    const chipy_ = {'': chip('', 'Vše')};
    for (const [id, , ico] of ZDROJE) { chipy_[id] = chip(id, '', ico); chipy_[id].hidden = true; }
    const popisky = () => {
      chipy_[''].querySelector('span').textContent = 'Vše · ' + vse.length;
      for (const [id, text] of ZDROJE) {
        const n = vse.filter((x) => x.source === id).length;
        chipy_[id].querySelector('span').textContent = text + ' · ' + n;
        chipy_[id].hidden = !n;
      }
    };
    popisky();

    const shoda = (x) => {
      const q = hledat.value.trim().toLowerCase();
      return (!zdroj || x.source === zdroj) &&
        (!q || (x.name + ' ' + x.description + ' ' + (x.plugin || '') + ' ' + (x.category || '')).toLowerCase().includes(q));
    };
    let skupina = null;
    const rada = (x) => {
      const nadpis = x.source === 'obsidian' ? 'Obsidian · ' + (x.category || '')
        : x.source === 'plugin' ? 'Plugin ' + (x.plugin || '') : 'Moje příkazy';
      if (nadpis !== skupina) {
        skupina = nadpis;
        seznam.appendChild(el('div', 'set-title plg-podnadpis plg-vstup', nadpis));
      }
      const {r} = radek(x.source === 'moje' ? '/' + x.name : x.name, x.description, '',
                        (ZDROJE.find((z) => z[0] === x.source) || [])[2]);
      r.classList.add('plg-vstup', 'plg-klik');
      r.onclick = () => detail(x);
      seznam.appendChild(r);
    };
    // Řádky naskakují po jednom, hned jak je skill načtený (i když zbytek ještě nedorazil).
    const dalsi = (mine) => {
      if (mine !== tik) return;
      const x = fronta.shift();
      if (!x) { bezi = false; return; }
      if (shoda(x) && seznam.querySelectorAll('.plg-radek').length < 300) rada(x);
      setTimeout(() => dalsi(mine), 12);
    };
    const spust = () => { if (!bezi) { bezi = true; dalsi(tik); } };
    const razene = () => vse.slice().sort((a, b) =>
      ZDROJE.findIndex((z) => z[0] === a.source) - ZDROJE.findIndex((z) => z[0] === b.source) ||
      (a.category || a.plugin || '').localeCompare(b.category || b.plugin || '', 'cs') || a.name.localeCompare(b.name, 'cs'));
    function prekresli() {
      tik++;
      bezi = false;
      skupina = null;
      seznam.textContent = '';
      fronta = razene().filter(shoda);
      if (!fronta.length && stav.isConnected) { /* čeká se na načtení */ }
      spust();
    }
    const stav = el('div', 'set-note', 'Načítám skilly…');
    seznamBox.insertBefore(stav, seznam);
    let t = null;
    hledat.oninput = () => { clearTimeout(t); t = setTimeout(prekresli, 150); };

    /* Zdroje se načítají jeden po druhém a každý se ukáže, jakmile dorazí:
       nejdřív moje příkazy, pak pluginy, nakonec Obsidian (571 postupů). */
    (async () => {
      for (const [id, text] of ZDROJE) {
        try {
          const d = await io.api('pluginy?jen=skilly&zdroj=' + id);
          const nove = d.skills || [];
          vse.push(...nove);
          popisky();
          stav.textContent = 'Načítám… ' + vse.length + ' skillů (zbývá ' + (ZDROJE.length - ZDROJE.findIndex((z) => z[0] === id) - 1) + ' zdroje)';
          // Nové se přidají na konec fronty, jen pokud sedí do filtru a nerozbijí řazení
          // (skupiny po zdrojích jdou za sebou, takže nové zdroje patří na konec).
          fronta.push(...nove.sort((a, b) => (a.category || a.plugin || '').localeCompare(b.category || b.plugin || '', 'cs') ||
                                              a.name.localeCompare(b.name, 'cs')).filter(shoda));
          spust();
        } catch (err) { stav.textContent = text + ' se nenačetly: ' + err.message; }
      }
      stav.hidden = true;
      if (!vse.length) { stav.hidden = false; stav.textContent = 'Žádné skilly se nenašly.'; }
    })();
    return wrap;
  }

  /* Katalog napojení: oficiální registr MCP serverů (přes 7 000). Bez hledání
     doporučená oficiální napojení firem, s hledáním výsledky z registru.
     Přidání = vlastní napojení (adresa serveru); přihlásí se v jeho kartě. */
  function katalogNapojeni(poPridani) {
    const wrap = el('div', 'plg-kat-napojeni');
    const claudeAi = el('div', 'plg-radek');
    claudeAi.appendChild(ikonka('claude.ai', 'i-globe'));
    const info = el('div', 'plg-info');
    info.appendChild(el('b', '', 'Konektory z claude.ai'));
    info.appendChild(el('div', 'plg-popis', 'Gmail, Kalendář, Drive, Notion, Slack a další z oficiálního adresáře claude.ai. ' +
      'Co tam zapneš, objeví se tu samo — pod účtem z Nastavení → Účet.'));
    claudeAi.appendChild(info);
    const btns = el('div', 'plg-btns');
    const otevrit = el('button', 'btn ghost', 'Otevřít adresář');
    otevrit.onclick = () => io.open('https://claude.ai/settings/connectors');
    btns.appendChild(otevrit);
    claudeAi.appendChild(btns);
    wrap.appendChild(claudeAi);

    const hledat = el('input', 'set-input plg-hledat');
    hledat.placeholder = 'Hledat v katalogu MCP serverů — třeba „slack", „jira", „shopify"…';
    const stav = el('div', 'set-note', 'Načítám doporučená napojení…');
    const seznam = el('div', 'plg-seznam');
    wrap.append(hledat, stav, seznam);
    let tik = 0;
    async function nacti() {
      const q = hledat.value.trim();
      const mine = ++tik;
      stav.hidden = false;
      stav.textContent = q ? 'Hledám v katalogu…' : 'Načítám doporučená napojení…';
      let d;
      try { d = await io.api('pluginy?jen=napojeni' + (q ? '&q=' + encodeURIComponent(q) : '')); }
      catch (err) { if (mine === tik) stav.textContent = 'Katalog se nenačetl: ' + err.message; return; }
      if (mine !== tik) return;
      seznam.textContent = '';
      stav.textContent = d.error || (q ? '' : 'Oficiální napojení firem:');
      stav.hidden = !stav.textContent;
      if (!d.items.length && !d.error) { stav.hidden = false; stav.textContent = 'Nic takového v katalogu není.'; }
      d.items.forEach((x, i) => setTimeout(() => {
        if (mine !== tik) return;
        const {r, tlacitka} = radek(x.title, x.description, (x.official ? 'oficiální · ' : '') + x.url.replace(/^https?:\/\//, ''), null, x.logo);
        r.classList.add('plg-vstup');
        tlacitka.appendChild(tlacitko('Přidat', async () => {
          try {
            const out = await io.api('connect', {action: 'add', service: 'vlastni', label: x.title, account: x.url});
            if (out.ok === false) throw new Error(out.detail || 'Nepovedlo se.');
            io.toast(x.title + ' přidané — přihlas se u něj v kartě Vlastní napojení.');
            if (poPridani) poPridani();
          } catch (err) { io.toast(err.message); }
        }, 'primary'));
        seznam.appendChild(r);
      }, i * 35));
    }
    let t = null;
    hledat.oninput = () => { clearTimeout(t); t = setTimeout(nacti, 350); };
    nacti();
    return wrap;
  }

  let vypnutoNekdy = false;       // vypnutý plugin zmizí až po obnovení okna

  function appka() {
    const wrap = el('div');
    const varovani = el('div', 'set-note plg-varovani');
    varovani.innerHTML = svg('i-error') + '<span>Plugin appky běží uvnitř aplikace se stejnými právy jako ty — může ' +
      'číst chaty, posílat zprávy Claudovi a spouštět příkazy. Zapni jen plugin, kterému věříš.</span>';
    wrap.appendChild(varovani);
    const obsah = el('div');
    wrap.appendChild(obsah);

    async function akce(body) {
      try {
        const out = await io.api('pluginy', body);
        if (out.message) io.toast(out.message);
        zakladni = {...zakladni, plugins: out.appka.plugins, allowed: out.appka.allowed, dir: out.appka.dir};
        if (body.akce === 'appka-zapni') {
          const p = out.appka.plugins.find((x) => x.id === body.id);
          if (p) nactiPlugin(p);
        }
        kresli();
        return out;
      } catch (err) {
        io.toast(err.message || 'Nepovedlo se.');
        return null;
      }
    }

    function kresli() {
      obsah.textContent = '';
      const seznam = (zakladni && zakladni.plugins) || [];
      if (zakladni && !zakladni.allowed) obsah.appendChild(el('div', 'set-warn', 'Pluginy appky jsou tady vypnuté správcem.'));
      if (!seznam.length) obsah.appendChild(el('div', 'set-note', 'Zatím žádný plugin appky.'));
      for (const p of seznam) {
        const {r, tlacitka} = radek(p.name, p.error || p.description,
          [p.version && 'v' + p.version, p.author, p.enabled ? 'zapnutý' : 'vypnutý'].filter(Boolean).join(' · '));
        if (!p.error) {
          tlacitka.appendChild(tlacitko(p.enabled ? 'Vypnout' : 'Zapnout', async () => {
            if (!p.enabled && !(await HubDialog.confirm('Zapnout plugin „' + p.name + '“?\n\nPoběží v aplikaci se stejnými právy jako ty ' +
                                      '(chaty, zprávy Claudovi, příkazy). Zapni ho, jen když mu věříš.', {title: 'Zapnout plugin appky', ok: 'Zapnout'}))) return;
            if (p.enabled) vypnutoNekdy = true;
            await akce({akce: p.enabled ? 'appka-vypni' : 'appka-zapni', id: p.id});
          }));
          const reg = registrace.get(p.id);
          if (p.enabled && reg && reg.nastaveni) {
            tlacitka.appendChild(tlacitko('Nastavení', () => {
              const panel = el('div', 'plg-nastaveni');
              r.after(panel);
              try { reg.nastaveni(panel); } catch (err) { panel.textContent = 'Nastavení pluginu spadlo: ' + err.message; }
            }));
          }
        }
        tlacitka.appendChild(tlacitko('Odebrat', async () => (await HubDialog.confirm('Odebrat plugin „' + p.name + '“ i s jeho soubory?', {title: 'Odebrat plugin', ok: 'Odebrat'})) && akce({akce: 'appka-odeber', id: p.id}), 'danger'));
        obsah.appendChild(r);
      }
      if (vypnutoNekdy || seznam.some((p) => !p.enabled && nactene.has(p.id))) {
        const row = el('div', 'set-row');
        row.appendChild(el('span', 'set-note', 'Vypnutý plugin zmizí po obnovení okna.'));
        row.appendChild(tlacitko('Obnovit okno', () => location.reload()));
        obsah.appendChild(row);
      }
      obsah.appendChild(el('div', 'set-title plg-podnadpis', 'Přidat plugin'));
      const row = el('div', 'set-row');
      const adresa = el('input', 'set-input');
      adresa.placeholder = 'GitHub owner/repo nebo https://… adresa repozitáře';
      row.append(adresa, tlacitko('Přidat z gitu', () => adresa.value.trim() && akce({akce: 'appka-pridej', url: adresa.value.trim()})));
      obsah.appendChild(row);
      const row2 = el('div', 'set-row');
      row2.appendChild(tlacitko('Vytvořit ukázkový plugin', () => akce({akce: 'appka-ukazka'})));
      obsah.appendChild(row2);
      if (zakladni && zakladni.dir) {
        obsah.appendChild(el('div', 'set-note', 'Vlastní plugin: složka v ' + zakladni.dir +
          ' s plugin.json (name, version, description, main, style). Ukázkový plugin je dobrá šablona.'));
      }
    }
    if (zakladni) kresli();
    else io.api('pluginy?jen=appka').then((d) => { zakladni = d; kresli(); }, () => kresli());
    return wrap;
  }

  /* ── Aladin Market: motivy, katalog Aladina, přibalené skilly ───────────
     Tyhle části volá Nastavení (settings.js) a podává jim `opts` z okna:
     {setMotiv, open, vychozi, poInstalaci} — io pluginů appky je nemá. */

  /* Náhled motivu: pruhy jeho barev (`nahled` v motiv.json, pár hex barev). */
  function nahledMotivu(barvy) {
    const n = el('div', 'mtv-nahled');
    const hex = (barvy || []).filter((b) => /^#[0-9a-f]{6}$/i.test(String(b))).slice(0, 6);
    if (!hex.length) n.classList.add('prazdny');
    for (const b of hex) {
      const s = el('span');
      s.style.background = b;
      n.appendChild(s);
    }
    return n;
  }

  function kartaMotivu(m, zapnuty) {
    const k = el('div', 'mtv-karta' + (zapnuty ? ' on' : '') + (m.error ? ' chyba' : ''));
    k.appendChild(nahledMotivu(m.nahled));
    const text = el('div', 'mtv-text');
    const hlava = el('div', 'mtv-nazev');
    hlava.appendChild(el('b', '', m.name || m.id || 'Motiv'));
    if (zapnuty) hlava.appendChild(el('span', 'mtv-stitek', 'Zapnutý'));
    text.appendChild(hlava);
    const meta = [m.author, m.version && 'v' + m.version].filter(Boolean).join(' · ');
    if (meta) text.appendChild(el('div', 'mtv-meta', meta));
    if (m.error) text.appendChild(el('div', 'mtv-popis set-warn', m.error));
    else if (m.description) {
      const d = el('div', 'mtv-popis', m.description);
      d.title = m.description;
      text.appendChild(d);
    }
    k.appendChild(text);
    const btns = el('div', 'mtv-btns');
    k.appendChild(btns);
    return {k, btns};
  }

  /* Zapne motiv (id '' = výchozí vzhled) a hned ho ukáže v okně. */
  async function zapniMotiv(id, opts) {
    const r = await io.api('pluginy', {akce: 'motiv-nastav', id});
    if (r.ok === false) throw new Error(r.message || r.error || 'Motiv se nezapnul.');
    if (opts && opts.setMotiv) opts.setMotiv(r.motiv || {id: ''});
    else location.reload();            // starší okno bez setMotiv — aspoň obnovit
    return r;
  }

  /* Moje motivy: mřížka karet, klik = zapnout. */
  function motivy(opts) {
    opts = opts || {};
    const wrap = el('div');
    wrap.appendChild(el('div', 'set-note',
      'Motiv mění barvy okna i terminálu, písmo a rozložení. Přepne se hned, bez obnovení okna.'));
    const stav = el('div', 'set-note', 'Načítám motivy…');
    const mrizka = el('div', 'mtv-mrizka');
    const dole = el('div');
    wrap.append(stav, mrizka, dole);
    let data = null, pracuje = false;

    async function nastav(id) {
      if (pracuje || !data || data.aktivni === id) return;
      pracuje = true;
      mrizka.classList.add('pracuje');
      try {
        await zapniMotiv(id, opts);
        data.aktivni = id;
        for (const m of data.motivy || []) m.aktivni = m.id === id;
        kresli();
      } catch (err) {
        io.toast(err.message || 'Nepovedlo se.');
      } finally {
        pracuje = false;
        mrizka.classList.remove('pracuje');
      }
    }

    async function akce(body) {
      try {
        const out = await io.api('pluginy', body);
        if (out.ok === false) throw new Error(out.message || out.error || 'Nepovedlo se.');
        if (out.message) io.toast(out.message);
        const driv = data && data.aktivni;
        if (out.motivy) data = out.motivy;
        // Odebraný motiv byl zapnutý → okno se vrátí k výchozímu vzhledu.
        if (driv && data && !data.aktivni && opts.setMotiv) opts.setMotiv({id: ''});
        kresli();
        return out;
      } catch (err) {
        io.toast(err.message || 'Nepovedlo se.');
        return null;
      }
    }

    function karta(m, zapnuty) {
      const {k, btns} = kartaMotivu(m, zapnuty);
      if (!m.error) {
        k.classList.add('plg-klik');
        k.tabIndex = 0;
        k.setAttribute('role', 'button');
        k.title = zapnuty ? 'Tenhle motiv je zapnutý' : 'Zapnout motiv';
        k.onclick = () => nastav(m.id);
        k.onkeydown = (ev) => { if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); nastav(m.id); } };
      }
      if (m.vlastni) {
        const del = tlacitko('Odebrat', async () => {
          if (await HubDialog.confirm('Odebrat motiv „' + (m.name || m.id) + '“ i s jeho soubory?',
                                      {title: 'Odebrat motiv', ok: 'Odebrat'})) {
            await akce({akce: 'motiv-odeber', id: m.id});
          }
        }, 'danger');
        del.addEventListener('click', (ev) => ev.stopPropagation());
        btns.appendChild(del);
      }
      return k;
    }

    function kresli() {
      mrizka.textContent = '';
      dole.textContent = '';
      const aktivni = (data && data.aktivni) || '';
      mrizka.appendChild(karta({id: '', name: 'Výchozí (Aladin)', author: 'Aladin',
                                description: 'Původní vzhled Aladina — jantarová na počítači, modrá na serveru.',
                                nahled: opts.vychozi || []}, !aktivni));
      for (const m of (data && data.motivy) || []) mrizka.appendChild(karta(m, !!aktivni && m.id === aktivni));

      dole.appendChild(el('div', 'set-title plg-podnadpis', 'Přidat motiv'));
      const row = el('div', 'set-row');
      const adresa = el('input', 'set-input');
      adresa.placeholder = 'GitHub owner/repo nebo https://… adresa repozitáře';
      row.append(adresa, tlacitko('Přidat z gitu', () => adresa.value.trim() && akce({akce: 'motiv-pridej', url: adresa.value.trim()})));
      dole.appendChild(row);
      const pro = el('div', 'set-note mtv-autori');
      pro.textContent = 'Pro autory: motiv je složka' + (data && data.dir ? ' v ' + data.dir : '') +
        ' s motiv.json — name, version, description, author, nahled (pár hex barev na kartu) a barvy ' +
        '{dark, light} s klíči palety: AMBER, BG, BG_SIDEBAR, BG_CARD, FG, FG_BRIGHT, DIM, GREEN, RED, ' +
        'CARD_HOVER, BORDER, SECTION, CLOUD, FIRMA, CHART a TERM_PALETTE (16 barev terminálu). ' +
        'Co v motivu chybí, zůstane výchozí. Fonty a rozložení patří do CSS: "style": "motiv.css".';
      dole.appendChild(pro);
    }

    io.api('pluginy?jen=motivy').then((d) => {
      data = d || {};
      stav.hidden = true;
      kresli();
    }, (err) => {
      stav.className = 'set-warn';
      stav.textContent = 'Motivy se nenačetly: ' + err.message;
      data = {motivy: [], aktivni: ''};
      kresli();
    });
    return wrap;
  }

  /* Objevit v Aladin Marketu: katalog pluginů a motivů Aladina (hub/market.py). */
  function market(druh, opts) {
    opts = opts || {};
    const jeMotiv = druh === 'motivy';
    const wrap = el('div');
    wrap.appendChild(el('div', 'set-note', jeMotiv
      ? 'Motivy z katalogu Aladina — barvy, písmo a rozložení okna. Po instalaci ho jedním klikem zapneš.'
      : 'Pluginy z katalogu Aladina. Po instalaci je plugin vypnutý — zapneš ho v záložce Moje, až budeš vědět, co dělá.'));
    const stav = el('div', 'set-note', 'Načítám katalog…');
    const obsah = el('div', jeMotiv ? 'mtv-mrizka' : 'plg-seznam');
    const pata = el('div', 'set-row mkt-pata');
    wrap.append(stav, obsah, pata);
    let data = null;
    const zapnute = new Set();          // motivy zapnuté odsud (katalog stav nezná)

    async function nacti(obnovit) {
      stav.hidden = false;
      stav.className = 'set-note';
      stav.textContent = obnovit ? 'Stahuju katalog znovu…' : 'Načítám katalog…';
      obsah.textContent = '';
      pata.textContent = '';
      try { data = await io.api('pluginy?jen=market' + (obnovit ? '&refresh=1' : '')); }
      catch (err) { data = {ok: false, error: err.message}; }
      kresli();
    }

    async function instaluj(x) {
      try {
        const out = await io.api('pluginy', {akce: 'market-instaluj', druh, id: x.id});
        if (out.ok === false) throw new Error(out.message || out.error || 'Nepovedlo se.');
        x.installed = true;
        if (out.appka) zakladni = {...zakladni, plugins: out.appka.plugins, allowed: out.appka.allowed, dir: out.appka.dir};
        io.toast(out.message || (jeMotiv ? 'Motiv „' + (x.name || x.id) + '“ je nainstalovaný.'
                                         : 'Plugin „' + (x.name || x.id) + '“ je nainstalovaný — zapneš ho v záložce Moje.'));
        kresli();
      } catch (err) {
        io.toast(err.message || 'Nepovedlo se.');
      }
    }

    function kresli() {
      obsah.textContent = '';
      pata.textContent = '';
      if (!data || data.ok === false) {
        stav.hidden = false;
        stav.className = 'set-warn';
        stav.textContent = 'Katalog se nenačetl: ' + ((data && data.error) || 'neznámá chyba');
        pata.appendChild(tlacitko('Zkusit znovu', () => nacti(true), 'primary'));
        return;
      }
      const polozky = (jeMotiv ? data.motivy : data.pluginy) || [];
      stav.className = 'set-note';
      stav.hidden = !!polozky.length;
      stav.textContent = jeMotiv ? 'V katalogu zatím žádné motivy nejsou.' : 'V katalogu zatím žádné pluginy nejsou.';
      polozky.forEach((x, i) => {
        let radka, btns;
        if (jeMotiv) {
          ({k: radka, btns} = kartaMotivu(x, zapnute.has(x.id)));
        } else {
          ({r: radka, tlacitka: btns} = radek(x.name || x.id, x.description,
            [x.version && 'v' + x.version, x.author].filter(Boolean).join(' · '), 'i-plus'));
        }
        radka.classList.add('plg-vstup');
        radka.style.animationDelay = Math.min(i, 20) * 30 + 'ms';
        if (!x.installed) {
          btns.appendChild(tlacitko('Nainstalovat', () => instaluj(x), 'primary'));
        } else {
          btns.appendChild(el('span', 'set-ok', '✓ Nainstalováno'));
          if (jeMotiv && !zapnute.has(x.id)) {
            btns.appendChild(tlacitko('Zapnout', async () => {
              try {
                await zapniMotiv(x.id, opts);
                zapnute.clear();
                zapnute.add(x.id);
                io.toast('Motiv „' + (x.name || x.id) + '“ je zapnutý.');
                kresli();
              } catch (err) { io.toast(err.message || 'Nepovedlo se.'); }
            }, 'primary'));
          } else if (!jeMotiv && opts.poInstalaci) {
            btns.appendChild(tlacitko('Zapnout v Moje', () => opts.poInstalaci()));
          }
        }
        obsah.appendChild(radka);
      });
      if (data.repo) {
        pata.appendChild(el('span', 'set-note', 'Katalog: ' + data.repo));
        if (opts.open) pata.appendChild(tlacitko('Otevřít na GitHubu', () => opts.open('https://github.com/' + data.repo)));
      }
      pata.appendChild(tlacitko('Obnovit katalog', () => nacti(true)));
    }

    nacti(false);
    return wrap;
  }

  /* Skilly, které Aladin přibaluje (hub/skills): po instalaci jsou v Claude
     Code jako /příkaz. Některé potřebují Obsidian vault. */
  function palubaSkilly() {
    const wrap = el('div');
    wrap.appendChild(el('div', 'set-note',
      'Skilly, které Aladin přibaluje (/deploy, /audit, /screenshot…). Po instalaci je máš v Claude Code ' +
      'jako /příkaz — v chatu napíšeš třeba /deploy.'));
    const stav = el('div', 'set-note', 'Načítám skilly…');
    const seznam = el('div', 'plg-seznam');
    wrap.append(stav, seznam);
    let skills = [];

    async function akce(body) {
      try {
        const out = await io.api('pluginy', body);
        if (out.ok === false) throw new Error(out.message || out.error || 'Nepovedlo se.');
        if (out.message) io.toast(out.message);
        if (out.skills) skills = out.skills;
        kresli();
      } catch (err) {
        io.toast(err.message || 'Nepovedlo se.');
      }
    }

    function kresli() {
      seznam.textContent = '';
      stav.hidden = !!skills.length;
      if (!skills.length) stav.textContent = 'Aladin žádné skilly nepřibaluje.';
      skills.forEach((x, i) => {
        const bezVaultu = x.vault && !x.available;
        const meta = [x.installed ? 'nainstalovaný' : '', bezVaultu ? 'potřebuje Obsidian vault' : x.vault ? 'pracuje s Obsidian vaultem' : '']
          .filter(Boolean).join(' · ');
        const {r, tlacitka} = radek('/' + x.name, x.description, meta, 'i-bolt');
        r.classList.add('plg-vstup');
        r.style.animationDelay = Math.min(i, 20) * 30 + 'ms';
        if (bezVaultu && !x.installed) {
          r.classList.add('plg-zasedle');
          r.title = 'Tenhle skill pracuje s Obsidian vaultem — nejdřív si ho nastav (Nastavení → Paměť).';
        } else if (x.installed) {
          tlacitka.appendChild(tlacitko('Odebrat', async () => {
            if (await HubDialog.confirm('Odebrat skill /' + x.name + '? Příkaz pak v Claude Code nebude.',
                                        {title: 'Odebrat skill', ok: 'Odebrat'})) {
              await akce({akce: 'skill-odeber', name: x.name});
            }
          }, 'danger'));
        } else {
          tlacitka.appendChild(tlacitko('Nainstalovat', () => akce({akce: 'skill-instaluj', name: x.name}), 'primary'));
        }
        seznam.appendChild(r);
      });
    }

    io.api('pluginy?jen=paluba-skilly').then((d) => {
      skills = (d && d.skills) || [];
      kresli();
    }, (err) => {
      stav.className = 'set-warn';
      stav.textContent = 'Skilly se nenačetly: ' + err.message;
    });
    return wrap;
  }

  global.HubPluginy = {
    start, registruj, sekce, ikonka, logoZUrl,
    claudeCode, skilly, appka, katalogNapojeni,
    motivy, market, palubaSkilly,
    akce: () => akceSeznam.slice(),
  };
})(window);
