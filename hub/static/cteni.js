/* Čtení — konverzace místo terminálu.
 *
 * V tabu pořád běží skutečný terminál a Claude Code v něm kreslí svoje okno;
 * tahle vrstva ho překrývá a ukazuje totéž jako text: co jsem napsal, co
 * Claude odepsal a co za tím spustil. Nástroj je jeden řádek („✎ upravil
 * hub.css"), který se klepnutím rozbalí — číst se má text, ne výpisy.
 *
 * Proč překryv a ne místo terminálu: xterm potřebuje mít pořád svoji velikost,
 * jinak by Claude Code psal do jinak širokého okna, než je vidět. Terminál se
 * proto nezmenšuje, jen se přes něj položí tahle vrstva — a uhne, jakmile se
 * Claude na něco zeptá (pane.asking, viz composer.js). Jiný přepínač není.
 *
 * Bloky chodí ze serveru (hub/cteni.py) po kouscích od bajtu, na kterém se
 * minule skončilo, takže při práci přibývají jen nové.
 */
'use strict';

(function (global) {

  const POLL = 1200;          // jak často se ptát, když se něco děje
  const POLL_KLID = 4000;     // a jak, když se dlouho nic nezměnilo
  const KLID_PO = 6;          // po kolika prázdných dotazech zpomalit
  const U_DNA = 90;           // px od spodku, dokud se ještě roluje samo
  // Kolik řádků konverzace stránka drží. Tab otevřený celý den přidává
  // stovky nástrojů za hodinu a při desítkách tabů pak stránka tuhne;
  // první načtení je stejně jen konec přepisu (hub/cteni.py, MAX_BLOCKS).
  const MAX_RADKU = 1200;

  const ZNAK = {
    Read: '◉', Edit: '✎', Write: '✎', NotebookEdit: '✎',
    Bash: '⏵', Grep: '⌕', Glob: '⌕', Task: '⛭', Agent: '⛭',
    WebFetch: '⇱', WebSearch: '⌕', TodoWrite: '☑', AskUserQuestion: '?',
    peer: '✉', hub: '↻',
  };

  /* Ikonka u nástroje v konverzaci: obrázek podle druhu (soubor, tužka, terminál,
     hledání, prohlížeč…), u MCP podle služby (název je mcp__služba__nástroj).
     Čárové ikony na mřížce 24, barvu bere z textu (currentColor). */
  const IKONA = {
    soubor: '<path d="M7 3h7l4 4v14H7z"/><path d="M14 3v4h4M9.5 12h5M9.5 15.5h5"/>',
    tuzka: '<path d="M4 20l1-4L16.5 4.5a2.1 2.1 0 013 3L8 19z"/><path d="M14.5 6.5l3 3"/>',
    terminal: '<rect x="3" y="4.5" width="18" height="15" rx="2.5"/><path d="M7 10l3 2.5L7 15M12.5 15h4.5"/>',
    hledani: '<circle cx="10.5" cy="10.5" r="5.5"/><path d="M15 15l5 5"/>',
    svet: '<circle cx="12" cy="12" r="8.5"/><path d="M3.5 12h17M12 3.5c2.6 2.4 3.6 5.2 3.6 8.5s-1 6.1-3.6 8.5c-2.6-2.4-3.6-5.2-3.6-8.5s1-6.1 3.6-8.5z"/>',
    agent: '<rect x="5" y="8" width="14" height="10" rx="3"/><path d="M12 4v4M9 13h.01M15 13h.01M9.5 16h5"/>',
    seznam: '<path d="M9 6h11M9 12h11M9 18h11"/><path d="M4 6l1 1 2-2M4 12l1 1 2-2M4 18l1 1 2-2"/>',
    otazka: '<circle cx="12" cy="12" r="8.5"/><path d="M9.6 9.5a2.5 2.5 0 114 2c-.9.6-1.6 1.1-1.6 2.3M12 16.8h.01"/>',
    hodiny: '<circle cx="12" cy="12" r="8.5"/><path d="M12 7v5l3 2"/>',
    zprava: '<rect x="3.5" y="5.5" width="17" height="13" rx="2.5"/><path d="M4 7l8 6 8-6"/>',
    chat: '<path d="M5 5.5h14a2 2 0 012 2v7a2 2 0 01-2 2h-6l-4.5 3.5V16.5H5a2 2 0 01-2-2v-7a2 2 0 012-2z"/>',
    budova: '<path d="M5 20V5.5a1.5 1.5 0 011.5-1.5h7A1.5 1.5 0 0115 5.5V20M15 10h3.5A1.5 1.5 0 0120 11.5V20M3.5 20h17M8.5 8h3M8.5 12h3M8.5 16h3"/>',
    nakup: '<path d="M5.5 8h13l-1 12h-11z"/><path d="M9 8V6.5a3 3 0 016 0V8"/>',
    mikrofon: '<rect x="9" y="3.5" width="6" height="10" rx="3"/><path d="M5.5 11.5a6.5 6.5 0 0013 0M12 18v3"/>',
    kostka: '<path d="M12 3.5l8 4.5v8.5l-8 4.5-8-4.5V8z"/><path d="M4 8l8 4.5L20 8M12 12.5V21"/>',
    paleta: '<path d="M12 3.5a8.5 8.5 0 100 17c1.3 0 1.8-1 1.3-2-.5-1 .1-2 1.2-2H17a3.5 3.5 0 003.5-3.5C20.5 7 16.8 3.5 12 3.5z"/><path d="M8 11h.01M11 7.5h.01M15.5 8.5h.01"/>',
    zasuvka: '<path d="M9 3.5v5M15 3.5v5M6.5 8.5h11v3a5.5 5.5 0 01-11 0zM12 17v3.5"/>',
    server: '<rect x="4" y="4" width="16" height="6.5" rx="2"/><rect x="4" y="13.5" width="16" height="6.5" rx="2"/><path d="M8 7.2h.01M8 16.7h.01"/>',
    kalendar: '<rect x="4" y="5.5" width="16" height="14.5" rx="2.5"/><path d="M4 10h16M8.5 3.5v4M15.5 3.5v4"/>',
    kladivo: '<path d="M14 6l4 4-8.5 8.5a2 2 0 01-3-3zM13 7l-3-3 3-1 4 4-1 3"/>',
    obnov: '<path d="M19.5 12a7.5 7.5 0 01-13.2 4.9M4.5 12a7.5 7.5 0 0113.2-4.9"/><path d="M18.5 3.5v4h-4M5.5 20.5v-4h4"/>',
  };
  const NASTROJ_IKONA = {
    Read: 'soubor', Edit: 'tuzka', Write: 'tuzka', NotebookEdit: 'tuzka', Bash: 'terminal',
    Grep: 'hledani', Glob: 'hledani', WebSearch: 'hledani', ToolSearch: 'hledani',
    WebFetch: 'svet', Task: 'agent', Agent: 'agent', TodoWrite: 'seznam', TaskCreate: 'seznam',
    TaskUpdate: 'seznam', AskUserQuestion: 'otazka', ScheduleWakeup: 'hodiny', Monitor: 'hodiny',
    SendMessage: 'zprava', peer: 'zprava', hub: 'obnov', KillShell: 'kladivo', BashOutput: 'terminal',
  };
  // MCP: podle služby v názvu (mcp__<služba>__<nástroj>).
  const MCP_IKONA = [
    [/playwright|browser|prohlizec/i, 'svet'], [/beeper|whatsapp|slack|discord|telegram/i, 'chat'],
    [/clockify|calendar|kalend/i, 'hodiny'], [/google/i, 'zprava'], [/shopify|woocommerce|eshop|shop/i, 'nakup'],
    [/eleven|voice|hlas|speech/i, 'mikrofon'], [/blender|roblox|3d/i, 'kostka'], [/figma|canva|design/i, 'paleta'],
    [/server|pocitac|computer/i, 'server'], [/spolubydl|apartment|ubyt|accom|nemovit/i, 'budova'],
    [/obsidian|vault|brain|notes?/i, 'soubor'],
  ];
  // Doména služby, ze které hub stáhne favicon (/api/logo?k=d:doména).
  const MCP_DOMENA = [
    [/beeper/i, 'beeper.com'], [/blender/i, 'blender.org'], [/canva/i, 'canva.com'], [/clockify/i, 'clockify.me'],
    [/eleven/i, 'elevenlabs.io'], [/figma/i, 'figma.com'], [/google/i, 'google.com'], [/playwright/i, 'playwright.dev'],
    [/roblox/i, 'roblox.com'], [/shopify/i, 'shopify.com'], [/spolubydl/i, 'spolubydlim.cz'],
    [/claude_ai_Claude_Docs|claude/i, 'claude.ai'],
  ];
  function druhIkony(name) {
    if (NASTROJ_IKONA[name]) return NASTROJ_IKONA[name];
    if (String(name).startsWith('mcp__')) {
      const sluzba = String(name).split('__')[1] || '';
      const hit = MCP_IKONA.find(([re]) => re.test(sluzba));
      return hit ? hit[1] : 'zasuvka';
    }
    return 'kladivo';
  }
  function ikonaNastroje(name) {
    const druh = druhIkony(name);
    const s = el('span', 'cteni-ico');
    s.dataset.druh = druh;
    s.setAttribute('aria-hidden', 'true');
    s.innerHTML = '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" ' +
      'stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round">' + IKONA[druh] + '</svg>';
    // MCP nástroj: místo čárové ikonky favicon služby (když se ho podaří stáhnout).
    const sluzba = String(name).startsWith('mcp__') ? String(name).split('__')[1] || '' : '';
    const hit = sluzba && MCP_DOMENA.find(([re]) => re.test(sluzba));
    if (hit) {
      const tok = typeof TOKEN !== 'undefined' ? TOKEN : new URLSearchParams(location.search).get('t') || '';
      const img = new Image();
      img.className = 'cteni-ico-logo';
      img.alt = '';
      img.onload = () => { s.textContent = ''; s.appendChild(img); };
      img.src = '/api/logo?k=' + encodeURIComponent('d:' + hit[1]) + '&t=' + encodeURIComponent(tok);
    }
    return s;
  }

  // Každý člověk sdíleného chatu má svou barvu — stejnou v bublinách i v hlavičce.
  const KDO_PALETA = ['#a371f7', '#3fb950', '#e0843c', '#2bb3a8', '#e05d8a', '#d29922'];
  function kdoBarva(jmeno) {
    let h = 0;
    for (const ch of String(jmeno || '')) h = (h * 31 + ch.charCodeAt(0)) >>> 0;
    return KDO_PALETA[h % KDO_PALETA.length];
  }
  function inicialy(jmeno) {
    const casti = String(jmeno || '?').trim().split(/\s+/);
    return ((casti[0] || '?')[0] + (casti.length > 1 ? casti[casti.length - 1][0] : '')).toUpperCase();
  }

  function el(tag, cls, text) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  /* ── Stažení souborů z prostoru ─────────────────────────────────────────
     Co Claude zapíše (Edit/Write) nebo vyrobí příkazem, si člověk stáhne
     rovnou z chatu — na serveru se k souboru jinak nedostane. Adresu dává
     hub.js (window.HubSoubor → /api/soubor), sdílený chat bránu
     (/gw/relace/soubor). Server znovu hlídá, odkud se smí stahovat. */
  // Výstup pro člověka: dokumenty, tabulky, archivy, média — karta na konci odpovědi.
  const VYSTUP = /\.(pdf|docx?|xlsx?|pptx?|od[tsp]|csv|tsv|zip|7z|rar|tgz|tar|gz|mp3|mp4|wav|m4a|webm|mov|ics|vcf|epub|rtf|stl|3mf|gcode|glb)$/i;
  // Tohle jen mimo složky se zdrojáky (stránka webu uprostřed repa výstup není).
  const VYSTUP_MOZNA = /\.(html?|png|jpe?g|gif|webp|svg|txt|json|xml)$/i;
  const ZDROJAKY = /[\\/](src|static|public|assets|templates|components|lib|app|node_modules|vendor|dist|build|test|tests|\.[^\\/]+)[\\/]/i;
  function proCloveka(cesta) {
    return VYSTUP.test(cesta) || (VYSTUP_MOZNA.test(cesta) && !ZDROJAKY.test(cesta));
  }
  const jmenoSouboru = (cesta) => String(cesta || '').split(/[\\/]/).pop() || cesta;
  function velikost(b) {
    if (!(b >= 0)) return '';
    if (b < 1024) return b + ' B';
    if (b < 1024 * 1024) return Math.round(b / 1024) + ' kB';
    return (b / 1024 / 1024).toFixed(b < 10 * 1024 * 1024 ? 1 : 0).replace('.', ',') + ' MB';
  }
  function vychoziStahovani() {
    return {
      url: (p) => (global.HubSoubor ? global.HubSoubor.url(p) : ''),
      info: (p) => (global.HubSoubor ? global.HubSoubor.info(p) : Promise.resolve(null)),
    };
  }
  /* Nejdřív se zeptá, jestli soubor je (jinak by se stáhla chybová hláška
     jako soubor), pak stáhne přes <a download> — funguje i v telefonu. */
  async function stahni(cesta, st, notice) {
    let info = null;
    try { info = await st.info(cesta); } catch (_) { info = null; }
    if (!info || !info.ok) {
      if (notice) notice((info && info.error) || 'Soubor teď nejde stáhnout.');
      return;
    }
    const a = document.createElement('a');
    a.href = st.url(cesta);
    a.download = jmenoSouboru(cesta);
    a.rel = 'noopener';
    document.body.appendChild(a);
    a.click();
    a.remove();
  }
  function tlacitkoStahnout(cesta, st, notice, cls, text) {
    const b = el('button', 'cteni-stahnout' + (cls ? ' ' + cls : ''), text || '⤓ Stáhnout');
    b.type = 'button';
    b.title = 'Stáhnout ' + cesta;
    b.onclick = (ev) => { ev.stopPropagation(); stahni(cesta, st, notice); };
    return b;
  }

  /* Barvy poznámek a malá paleta na výběr (i na odebrání). Otevírá se pravým
     klikem — `kotva` je prvek, nebo {x, y} z události myši. */
  const DUL_BARVY = ['#e0843c', '#f85149', '#3fb950', '#4c97f0', '#a371f7', '#d29922', '#e5609c'];
  const DUL_JMENA = {'#e0843c': 'oranžová', '#f85149': 'červená', '#3fb950': 'zelená', '#4c97f0': 'modrá',
                     '#a371f7': 'fialová', '#d29922': 'žlutá', '#e5609c': 'růžová'};
  function paleta(kotva, aktualni, vyber, odeber, kopie) {
    document.querySelectorAll('.dul-paleta').forEach((n) => n.remove());
    const box = el('div', 'dul-paleta');
    // Kopírování (jen u zpráv ve Čtení): celá zpráva, a když je co, i označený kousek.
    if (kopie && kopie.zprava) {
      const z = el('button', 'dul-kopie', 'Kopírovat zprávu');
      z.onclick = (ev) => { ev.stopPropagation(); kopie.zprava(); zavri(); };
      box.appendChild(z);
      if (kopie.vyber) {
        const v = el('button', 'dul-kopie', 'Kopírovat výběr');
        v.onclick = (ev) => { ev.stopPropagation(); kopie.vyber(); zavri(); };
        box.appendChild(v);
      }
    }
    box.appendChild(el('div', 'dul-nadpis', 'Barva poznámky'));
    for (const b of DUL_BARVY) {
      const t = el('button', 'dul-barva' + (b === aktualni ? ' on' : ''));
      t.style.background = b;
      t.title = DUL_JMENA[b] || b;
      t.onclick = (ev) => { ev.stopPropagation(); vyber(b); zavri(); };
      box.appendChild(t);
    }
    if (odeber) {
      const x = el('button', 'dul-odeber', 'Odebrat z poznámek');
      x.onclick = (ev) => { ev.stopPropagation(); odeber(); zavri(); };
      box.appendChild(x);
    }
    document.body.appendChild(box);
    const r = kotva.getBoundingClientRect ? kotva.getBoundingClientRect()
      : {left: kotva.x, bottom: kotva.y};
    const w = box.offsetWidth;
    box.style.left = Math.max(8, Math.min(window.innerWidth - w - 8, r.left)) + 'px';
    box.style.top = Math.max(8, Math.min(window.innerHeight - box.offsetHeight - 8, r.bottom + 4)) + 'px';
    function zavri() { box.remove(); document.removeEventListener('mousedown', mimo, true); document.removeEventListener('keydown', esc, true); }
    function mimo(ev) { if (!box.contains(ev.target)) zavri(); }
    function esc(ev) { if (ev.key === 'Escape') zavri(); }
    setTimeout(() => { document.addEventListener('mousedown', mimo, true); document.addEventListener('keydown', esc, true); }, 0);
  }

  /* Zpráva člověka je holý text — adresy v ní se ale mají dát otevřít, stejně
     jako v odpovědi Clauda. Kreslí se po kouscích (textNode + <a>), nic se
     nevkládá jako HTML, takže se nedá nic podstrčit. Otevírá je tentýž
     handler jako odkazy v odpovědích (mount click → openLink). */
  const ADRESA = /\b(?:https?:\/\/|www\.)[^\s<>"'`]+/gi;
  function textSOdkazy(text) {
    const box = el('div', 'cteni-text');
    let kam = 0;
    for (const m of String(text).matchAll(ADRESA)) {
      let url = m[0];
      // Tečka, čárka, závorka na konci věty k adrese nepatří.
      const konec = /[.,;:!?)\]}»”]+$/.exec(url);
      if (konec) url = url.slice(0, url.length - konec[0].length);
      if (url.length < 8) continue;
      if (m.index > kam) box.appendChild(document.createTextNode(text.slice(kam, m.index)));
      const a = el('a', 'cteni-odkaz', url);
      const href = /^www\./i.test(url) ? 'https://' + url : url;
      a.href = href;
      a.dataset.href = href;
      a.title = href;
      box.appendChild(a);
      kam = m.index + url.length;
    }
    if (kam < String(text).length) box.appendChild(document.createTextNode(String(text).slice(kam)));
    return box;
  }

  /* Markdown umí už prohlížeč poznámek — Claudeův text je tentýž Markdown,
     tak se kreslí stejně (a stejně se i escapuje). Bez vault.js zbyde holý
     text, což je pořád čitelné. */
  function markdown(text) {
    if (global.HubVault && global.HubVault.render) {
      try {
        return global.HubVault.render(jakoHtml(text), {resolve: () => '', image: () => '', breaks: true}).html;
      } catch (_) { /* radši holý text než prázdno */ }
    }
    return '';
  }

  /* Odpověď, která je celá HTML (dokument, tabulka, kus stránky), by se jinak
     ukázala jako holé značky. Obalí se do bloku ```html, který ozivHtml()
     vykreslí jako náhled. */
  function jakoHtml(text) {
    const t = String(text || '').trim();
    const cely = /^<!doctype html|^<html[\s>]/i.test(t) ||
      (/^<(div|table|section|article|main|header|body|style|h[1-6]|p|ul|ol|svg)[\s>]/i.test(t) && /<\/[a-z0-9]+>$/i.test(t));
    return cely ? '~~~~~~~~html\n' + t + '\n~~~~~~~~' : text;
  }

  /* Kód v bloku ```html se ukáže jako vykreslená stránka s přepínačem na zdroj.
     iframe je bez skriptů (sandbox), takže HTML od Clauda nemůže nic spustit;
     `allow-same-origin` jen kvůli změření výšky. */
  function nahledHtml(kod) {
    const box = el('div', 'cteni-html');
    const bar = el('div', 'cteni-html-bar');
    const zdroj = el('button', 'cteni-html-btn', 'Kód');
    const nahled = el('button', 'cteni-html-btn on', 'Náhled');
    bar.append(nahled, zdroj);
    const ram = el('iframe', 'cteni-html-ram');
    ram.setAttribute('sandbox', 'allow-same-origin');
    ram.setAttribute('loading', 'lazy');
    const zaklad = '<meta charset="utf-8"><base target="_blank"><style>' +
      'body{font:14px/1.55 system-ui,sans-serif;margin:14px;color:#1b1b1b;background:#fff}' +
      'table{border-collapse:collapse}th,td{border:1px solid #ccc;padding:6px 10px;text-align:left;vertical-align:top}' +
      'img{max-width:100%}</style>';
    ram.srcdoc = zaklad + kod;
    ram.onload = () => {
      try {
        ram.style.height = '0px';     // jinak scrollHeight nikdy nezmenší pod výšku rámu
        const h = ram.contentDocument.documentElement.scrollHeight;
        ram.style.height = Math.min(h + 4, Math.round(window.innerHeight * 0.8)) + 'px';
      } catch (_) { /* zůstane výchozí výška */ }
    };
    const pre = el('pre', 'vault-code cteni-html-kod');
    pre.hidden = true;
    pre.appendChild(el('code', '', kod));
    const prepni = (jeKod) => {
      ram.hidden = jeKod;
      pre.hidden = !jeKod;
      zdroj.classList.toggle('on', jeKod);
      nahled.classList.toggle('on', !jeKod);
    };
    zdroj.onclick = () => prepni(true);
    nahled.onclick = () => prepni(false);
    box.append(bar, ram, pre);
    return box;
  }

  function ozivHtml(koren) {
    for (const pre of koren.querySelectorAll('pre.vault-code[data-lang]')) {
      if (!/^(html?|xhtml)$/.test(pre.dataset.lang)) continue;
      pre.replaceWith(nahledHtml(pre.textContent));
    }
  }

  /* Obrázek přes celé okno — klepnutí nebo Esc ho zavře. */
  function zvetsit(src) {
    const box = el('div', 'cteni-lupa');
    const img = el('img');
    img.src = src;
    img.alt = '';
    box.appendChild(img);
    const zavrit = () => { box.remove(); document.removeEventListener('keydown', naKlavesu, true); };
    function naKlavesu(ev) { if (ev.key === 'Escape') { ev.stopPropagation(); zavrit(); } }
    box.onclick = zavrit;
    document.addEventListener('keydown', naKlavesu, true);
    document.body.appendChild(box);
  }

  /* ── agenti ──────────────────────────────────────────────────────────────
     Claude pustí agenta a ten pracuje sám, často na pozadí a často několik
     najednou. V terminálu je z toho jeden řádek — tady karta: kdo to je, co
     dostal za úkol, jestli ještě běží, co zrovna dělá a co odevzdal. */

  // Každý agent dostane vlastní barvu (další z palety v pořadí, jak přicházejí),
  // ať se paralelní agenti dají od sebe poznat — i ti stejného typu.
  const AG_PALETA = ['#4c97f0', '#e0843c', '#3fb950', '#a371f7', '#e05d8a', '#2bb3a8', '#d4a72c', '#f0636b'];
  let agPoradi = 0;
  const AG_STAV = {bezi: 'Běží', hotovo: 'Hotovo', chyba: 'Selhal', stop: 'Zastaven'};
  const AG_KROKU = 5;          // kolik posledních kroků je na kartě vidět

  const casKroku = (ms) => (ms < 10000 ? (ms / 1000).toFixed(1) : Math.round(ms / 1000)) + 's';

  const dalsiBarva = () => AG_PALETA[agPoradi++ % AG_PALETA.length];

  // Jednoduchý režim × Pro pokročilé (hub.js → window.HUB_ADVANCED). Čte se
  // při každém kreslení — přepnout se dá bez reloadu.
  const pokrocile = () => !!global.HUB_ADVANCED;

  // V jednoduchém režimu jsou pomocníci Claudíci — postavička místo „agenta“.
  const CLAUDIK_PROFESE = {Explore: 'Průzkumník', Plan: 'Plánovač', 'claude-code-guide': 'Rádce'};

  function jmenoAgenta(typ) {
    if (!pokrocile()) {
      return CLAUDIK_PROFESE[typ] ? 'Claudík ' + CLAUDIK_PROFESE[typ] : 'Claudík';
    }
    if (!typ || typ === 'general-purpose') return 'Agent';
    return typ.charAt(0).toUpperCase() + typ.slice(1);
  }

  /* Claudík: malá pixelová postavička v barvě pomocníka (currentColor).
     Oči se mění podle stavu (CSS: .ag.hotovo úsměv, .ag.chyba křížky),
     Průzkumník nese lupu, Plánovač papír s tužkou, Rádce čepici. */
  /* Oblečení hlavního Claudíka podle úsilí (effort), se kterým Claude jede.
     Kreslí se do každé postavičky a CSS ukáže jen to, které sedí na
     `data-effort` u předka (claudici.js, Claudík visící z tabu):
       low     — pyžamo a noční čepice (pohodička)
       medium  — kšiltovka
       high    — stavební helma a reflexní vesta
       xhigh   — superhrdina: plášť a čelenka
       max     — kouzelník: špičatý klobouk s hvězdami a plášť */
  const PLAST = (barva) => `<rect x="1" y="5" width="14" height="6" fill="${barva}"/>` +
    `<rect x="0" y="8" width="2" height="5" fill="${barva}"/><rect x="14" y="8" width="2" height="5" fill="${barva}"/>` +
    `<rect x="-1" y="11" width="1" height="2" fill="${barva}"/><rect x="16" y="11" width="1" height="2" fill="${barva}"/>`;
  const OBLECENI_ZA =
    `<g class="cl-obl cl-obl-xhigh">${PLAST('#da3633')}</g>` +
    `<g class="cl-obl cl-obl-max">${PLAST('#6e40c9')}</g>`;
  const OBLECENI =
    '<g class="cl-obl cl-obl-low">' +
      '<rect x="3" y="3" width="10" height="1" fill="#f0f6fc"/>' +
      '<rect x="5" y="2" width="7" height="1" fill="#5b8def"/><rect x="8" y="1" width="5" height="1" fill="#5b8def"/>' +
      '<rect x="12" y="2" width="2" height="1" fill="#5b8def"/>' +
      '<rect class="cl-bambule" x="14" y="2" width="2" height="2" fill="#f0f6fc"/>' +
      '<rect x="3" y="9" width="10" height="1" fill="#5b8def" opacity=".55"/>' +
    '</g>' +
    '<g class="cl-obl cl-obl-medium">' +
      '<rect x="4" y="2" width="8" height="2" fill="#e5534b"/><rect x="7" y="1" width="2" height="1" fill="#e5534b"/>' +
      '<rect x="12" y="3" width="4" height="1" fill="#b62324"/>' +
    '</g>' +
    '<g class="cl-obl cl-obl-high">' +
      '<rect x="5" y="1" width="6" height="1" fill="#f2cc60"/><rect x="4" y="2" width="8" height="1" fill="#f2cc60"/>' +
      '<rect x="2" y="3" width="12" height="1" fill="#d4a72c"/><rect x="7" y="1" width="2" height="2" fill="#fff3b0"/>' +
      '<rect x="3" y="9" width="10" height="1" fill="#c6f432"/><rect x="5" y="8" width="1" height="3" fill="#c6f432"/>' +
      '<rect x="10" y="8" width="1" height="3" fill="#c6f432"/>' +
    '</g>' +
    '<g class="cl-obl cl-obl-xhigh">' +
      '<rect x="3" y="4" width="10" height="1" fill="#da3633"/><rect x="13" y="3" width="2" height="1" fill="#da3633"/>' +
      '<rect x="15" y="2" width="1" height="1" fill="#da3633"/>' +
      '<rect x="7" y="9" width="2" height="1" fill="#f2cc60"/>' +
    '</g>' +
    '<g class="cl-obl cl-obl-max">' +
      '<rect x="3" y="3" width="10" height="1" fill="#6e40c9"/><rect x="5" y="2" width="6" height="1" fill="#8957e5"/>' +
      '<rect x="6" y="1" width="4" height="1" fill="#8957e5"/><rect x="7" y="0" width="2" height="1" fill="#8957e5"/>' +
      '<rect x="8" y="-1" width="2" height="1" fill="#8957e5"/><rect x="10" y="-2" width="1" height="1" fill="#8957e5"/>' +
      '<rect class="cl-jiskra" x="6" y="2" width="1" height="1" fill="#f2cc60"/>' +
      '<rect class="cl-jiskra b" x="8" y="0" width="1" height="1" fill="#f2cc60"/>' +
      '<rect class="cl-jiskra" x="15" y="0" width="1" height="1" fill="#f2cc60"/>' +
      '<rect class="cl-jiskra b" x="0" y="3" width="1" height="1" fill="#f2cc60"/>' +
    '</g>';

  /* Aladin a džin: hlavní Claudík je Aladin (bílý turban s rubínem, vestička),
     pomocníci jsou džinové v barvě svého agenta (uzel vlasů, zlatá čelenka
     a náramky, bradka a ocas místo nohou). CSS ukáže turban jen bez klobouku
     podle úsilí; kdo je který, řeší předek (.cl-fig.hlavni, .tab-visi). */
  const ALADIN =
    '<g class="cl-aladin">' +
      '<rect x="3" y="8" width="3" height="3" fill="#2bb3a3"/><rect x="10" y="8" width="3" height="3" fill="#2bb3a3"/>' +
      '<rect x="3" y="3" width="10" height="2" fill="#f6eddc"/><rect x="4" y="2" width="8" height="1" fill="#f6eddc"/>' +
      '<rect x="6" y="1" width="4" height="1" fill="#f6eddc"/><rect x="3" y="4" width="10" height="1" fill="#d9d2c0"/>' +
      '<rect x="7" y="3" width="2" height="1" fill="#e5534b"/>' +
    '</g>';
  const DZIN =
    '<g class="cl-dzin">' +
      '<rect x="7" y="2" width="2" height="2" fill="currentColor"/><rect x="9" y="1" width="2" height="1" fill="currentColor"/>' +
      '<rect x="3" y="4" width="10" height="1" fill="#f2cc60"/><rect x="7" y="3" width="2" height="1" fill="#f2cc60"/>' +
      '<rect x="1" y="7" width="2" height="1" fill="#f2cc60"/><rect x="13" y="7" width="2" height="1" fill="#f2cc60"/>' +
      '<rect x="7" y="10" width="2" height="1" fill="#1b1b1b"/>' +
      '<g class="cl-ocas" fill="currentColor"><rect x="4" y="11" width="8" height="1"/><rect x="5" y="12" width="6" height="1"/>' +
      '<rect x="6" y="13" width="3" height="1"/><rect x="9" y="13" width="2" height="1" opacity=".6"/></g>' +
    '</g>';

  function claudik(typ) {
    const doplnek = {
      Explore: '<g class="cl-dopl"><rect x="13" y="3" width="3" height="1"/><rect x="12" y="4" width="1" height="3"/>' +
               '<rect x="16" y="4" width="1" height="3"/><rect x="13" y="7" width="3" height="1"/>' +
               '<rect x="12" y="8" width="1" height="1"/><rect x="11" y="9" width="1" height="1"/></g>',
      Plan: '<g class="cl-dopl"><rect x="13" y="5" width="4" height="5" fill="#fff"/>' +
            '<rect x="14" y="6" width="2" height="1" fill="#999"/><rect x="14" y="8" width="2" height="1" fill="#999"/></g>',
      'claude-code-guide': '<g class="cl-dopl"><rect x="4" y="1" width="8" height="2"/><rect x="3" y="3" width="10" height="1"/></g>',
    }[typ] || '';
    return `<svg class="claudik" viewBox="0 1 18 13" shape-rendering="crispEdges" aria-hidden="true">
      ${OBLECENI_ZA}
      <g class="cl-telo" fill="currentColor">
        <rect x="3" y="4" width="10" height="7"/>
        <rect x="1" y="6" width="2" height="2"/><rect x="13" y="6" width="2" height="2"/>
        <rect class="cl-noha a" x="4" y="11" width="1" height="2"/><rect class="cl-noha b" x="6" y="11" width="1" height="2"/>
        <rect class="cl-noha a" x="9" y="11" width="1" height="2"/><rect class="cl-noha b" x="11" y="11" width="1" height="2"/>
        ${doplnek}
      </g>
      ${OBLECENI}
      ${ALADIN}
      ${DZIN}
      <g class="cl-oci" fill="#1b1b1b">
        <rect class="cl-oko" x="5" y="6" width="1" height="2"/><rect class="cl-oko" x="10" y="6" width="1" height="2"/>
      </g>
      <g class="cl-usmev" fill="#1b1b1b">
        <rect x="4" y="7" width="1" height="1"/><rect x="5" y="6" width="1" height="1"/><rect x="6" y="7" width="1" height="1"/>
        <rect x="9" y="7" width="1" height="1"/><rect x="10" y="6" width="1" height="1"/><rect x="11" y="7" width="1" height="1"/>
      </g>
      <g class="cl-krizky" fill="#1b1b1b">
        <rect x="4" y="6" width="1" height="1"/><rect x="6" y="6" width="1" height="1"/><rect x="5" y="7" width="1" height="1"/>
        <rect x="4" y="8" width="1" height="1"/><rect x="6" y="8" width="1" height="1"/>
        <rect x="9" y="6" width="1" height="1"/><rect x="11" y="6" width="1" height="1"/><rect x="10" y="7" width="1" height="1"/>
        <rect x="9" y="8" width="1" height="1"/><rect x="11" y="8" width="1" height="1"/>
      </g>
    </svg>`;
  }

  // „claude-haiku-4-5-20251001" → „Haiku 4.5", „opus" → „Opus".
  function jmenoModelu(m) {
    const x = /(opus|sonnet|haiku|fable)(?:-(\d+)(?:-(\d{1,2})(?!\d))?)?/i.exec(m || '');
    if (!x) return '';
    return x[1].charAt(0).toUpperCase() + x[1].slice(1).toLowerCase() +
      (x[2] ? ' ' + x[2] + (x[3] ? '.' + x[3] : '') : '');
  }

  function cas(ms) {
    const s = Math.max(0, Math.round(ms / 1000));
    if (s < 60) return s + ' s';
    const m = Math.floor(s / 60);
    return m < 60 ? m + ' min ' + (s % 60) + ' s' : Math.floor(m / 60) + ' h ' + (m % 60) + ' min';
  }

  function pocet(n, jedna, dve, pet) {
    return n + ' ' + (n === 1 ? jedna : n >= 2 && n <= 4 ? dve : pet);
  }

  function tokeny(n) {
    if (!n) return '';
    const t = n >= 1000 ? (n / 1000).toFixed(n >= 100000 ? 0 : 1).replace('.', ',') + 'k' : String(n);
    return t + ' tokenů';
  }

  /* Proud bloků. Stejný pro živý tab i pro okno se starou konverzací.
     `imageUrl` převede cestu k obrázku z hub-images na adresu, ze které ho
     stránka smí načíst (/api/image). */
  /* Rychlé odpovědi. Claude často končí „Napiš „pushni““ — místo psaní
     (nebo kopírování celé věty) stačí klepnout. Bere se, co je v uvozovkách
     za slovesem (napiš, odpověz, pošli, stačí…), i další volby za „nebo“,
     a celý řádek „Rychlá odpověď: „A“ · „B““. Kód se přeskakuje. */
  const UVOZ = '[„"]([^“”"\\n]{1,60})[“”"]';
  const VOLBY_RADEK = /^\s*(?:[-*]\s*)?(?:\*\*)?(?:rychlá odpověď|rychlé odpovědi|odpověz|odpovědi)(?::\*\*|\*\*\s*:|:)(.*)$/gimu;
  const VOLBY_SLOVESO = new RegExp('(?<!\\p{L})(?:napiš|napište|odpověz|odpovězte|pošli|pošlete|' +
    'stačí napsat|stačí|řekni|řekněte)\\s*:?\\s*' + UVOZ +
    '((?:\\s*(?:,\\s*(?:nebo|anebo|či|případně)?|/|nebo|anebo|či|případně)\\s*[„"][^“”"\\n]{1,60}[“”"])*)', 'giu');
  function rychleOdpovedi(text) {
    const out = [];
    const pridej = (t) => {
      t = t.trim().replace(/[.,!?…]+$/u, '');
      if (t && !out.includes(t) && out.length < 6) out.push(t);
    };
    const uvozovky = (kus) => { for (const m of kus.matchAll(new RegExp(UVOZ, 'gu'))) pridej(m[1]); };
    const bezKodu = String(text || '').replace(/```[\s\S]*?```/g, '').replace(/`[^`\n]*`/g, '');
    for (const m of bezKodu.matchAll(VOLBY_RADEK)) uvozovky(m[1]);
    for (const m of bezKodu.matchAll(VOLBY_SLOVESO)) { pridej(m[1]); if (m[2]) uvozovky(m[2]); }
    return out;
  }

  function flow(mount, imageUrl, {historie = false, openLink = null, notice = null, zive = null,
                                  udalost = null, naPrehled = null, dulKlic = null, copy = null, sdileny = null,
                                  odpovez = null, stahovani = null, znovu = null, jeOnline = null,
                                  pozastaveno = null, pozastavit = null, serverZnovu = null, serverIhned = null,
                                  serverSeznam = null, serverUprav = null, serverSmaz = null,
                                  rozvetvit = null, odchozi = null} = {}) {
    const st = stahovani || vychoziStahovani();
    /* Odkaz v Markdownu je <a> bez href (vault.js) — sám nikam nevede,
       otevřít ho musí hub. V okně appky by holý odkaz stejně nic neudělal. */
    mount.addEventListener('click', (ev) => {
      const a = ev.target.closest('a');
      if (!a || !mount.contains(a)) return;
      ev.preventDefault();
      const url = a.dataset.href || a.getAttribute('href') || '';
      if (/^(https?:|mailto:)/i.test(url) && openLink) openLink(url);
    });

    const tools = new Map();          // id nástroje → jeho řádek
    // Tlačítka rychlé odpovědi — jen pod poslední odpovědí Clauda.
    let volby = null;
    const agenti = new Map();         // id volání Agent → karta
    let prace = null;                 // řádek „Claude pracuje…"
    let skupina = null;               // karty agentů puštěných najednou
    let posun = 0;                    // o kolik jdou hodiny stránky napřed před serverem

    /* Spodek konverzace: řádek práce, agenti na pozadí a pod tím zprávy,
       které ještě nedošly — napsané, ale Claude je zatím nevidí. Drží se
       vždycky na konci, i když nad ně přibývá. */
    const fronta = el('div', 'cteni-fronta');
    const pozadi = el('button', 'cteni-pozadi');
    pozadi.hidden = true;
    // Příkazy a hlídání, které Claude pustil na pozadí (/api/cteni-pozadi).
    // Agenti mají vlastní kartu a proužek výš, tady se neopakují.
    const ulohy = el('div', 'cteni-ulohy');
    ulohy.hidden = true;
    let ulohyData = [];
    const cekajici = [];              // ze serveru: {key, row}

    /* Přehled pro panel Průběh (hub.js renderPrehled) — jako v appce Claude:
       zadání, Claudův seznam úkolů, pomocníci, úlohy na pozadí a soubory. */
    const pr = {zadani: [], todos: null, tasks: [], soubory: [], kroky: [], pracuje: false};
    /* Soubory ke stažení v jedné odpovědi: výstupy pro člověka (proCloveka)
       dostanou na konci odpovědi výraznou kartu. `overene` = výstupy příkazů,
       o kterých server potvrdil, že soubor opravdu je. */
    let tah = {soubory: [], karta: null};
    const overene = [];
    function doTahu(cesta) {
      if (!cesta || tah.soubory.includes(cesta)) return;
      tah.soubory.push(cesta);
      kresliKartu();
    }
    function kresliKartu() {
      if (!tah.soubory.length) return;
      if (!tah.karta) {
        tah.karta = el('div', 'cteni-ke-stazeni');
        mount.appendChild(tah.karta);
      }
      const k = tah.karta;
      k.textContent = '';
      k.appendChild(el('div', 'cteni-ks-nadpis', tah.soubory.length === 1 ? 'Soubor ke stažení' : 'Soubory ke stažení'));
      for (const cesta of tah.soubory) {
        const r = el('div', 'cteni-ks-radek');
        const txt = el('span', 'cteni-ks-text');
        txt.append(el('span', 'cteni-ks-jmeno', jmenoSouboru(cesta)),
                   el('span', 'cteni-ks-cesta', cesta.replace(/[\\/][^\\/]*$/, '')));
        txt.title = cesta;
        r.append(el('span', 'cteni-ks-ico', '⤓'), txt, tlacitkoStahnout(cesta, st, notice, 'hlavni', 'Stáhnout'));
        k.appendChild(r);
      }
      naKonec();
    }
    /* Výstupy příkazu (cesty v textu příkazu, hub/cteni.py) — jen ty, které
       po doběhnutí opravdu existují. */
    function overVystupy(box, cesty) {
      for (const cesta of cesty) {
        Promise.resolve(st.info(cesta)).then((info) => {
          if (!info || !info.ok) return;
          if (!overene.includes(cesta)) overene.push(cesta);
          let radek = box.querySelector('.cteni-tool-soubor');
          if (!radek) {
            radek = el('div', 'cteni-tool-soubor');
            box.insertBefore(radek, box._detail);
          }
          radek.appendChild(tlacitkoStahnout(cesta, st, notice, 'tichy', '⤓ ' + jmenoSouboru(cesta)
                                             + (info.size ? ' · ' + velikost(info.size) : '')));
          if (box._tah === tah) doTahu(cesta);
          zmenaPrehledu();
        }).catch(() => {});
      }
    }
    let prTimer = null;
    function zmenaPrehledu() {
      if (!naPrehled || prTimer) return;
      prTimer = setTimeout(() => { prTimer = null; naPrehled(); }, 150);
    }
    function prehled() {
      const bezi = pr.pracuje;
      return {
        zadani: pr.zadani.slice(-6).map((z, i, a) => ({
          text: z.text, row: z.row, stav: i === a.length - 1 && bezi ? 'bezi' : 'hotovo'})),
        dulezite: (dulNacti(), dul.map((d) => ({k: d.k, text: d.text, barva: d.barva, row: dulRadky.get(d.k)}))),
        dulBarva, dulOdeber, dulSkoc, dulNova,
        barvy: DUL_BARVY, jmena: DUL_JMENA,
        todos: pr.todos || (pr.tasks.length ? pr.tasks : null),
        // Kroky práce od poslední zprávy — když Claude seznam úkolů nevede
        // (na tomhle účtu TodoWrite / TaskCreate nemá), jsou to jeho úkoly.
        kroky: pr.kroky.slice(-8),
        kroku: pr.kroky.length,
        agenti: [...agenti.values()].map((k) => ({
          jmeno: jmenoAgenta(k.typ), ukol: k.q('.ag-ukol').textContent,
          stav: k.stav, barva: k.barva, box: k.box})),
        ulohy: ulohyData.filter((u) => u.name !== 'Agent').map((u) => ({
          text: u.title || u.name, druh: u.name === 'Monitor' ? 'hlídá'
            : u.name === 'ScheduleWakeup' ? 'ozve se' : 'příkaz'})),
        soubory: [...new Set([...pr.soubory, ...overene])].slice(-8).reverse(),
        pracuje: bezi,
      };
    }
    /* ── Důležité body ───────────────────────────────────────────────────────
       Zprávu si člověk označí vlajkou a vybere jí barvu tečky. V panelu Průběh
       pak je v seznamu Důležité a klepnutí na ni odrolovává k té zprávě.
       Ukládá se v prohlížeči podle tabu; zprávu poznává podle začátku textu. */
    const dul = [];                   // {k, text, barva}
    const dulRadky = new Map();       // k -> řádek se zprávou
    let dulNactenoPro = null;
    const dulId = (t) => norm(t).slice(0, 80);
    const dulUlozisko = () => { const k = dulKlic ? dulKlic() : ''; return k ? 'hub-dulezite:' + k : ''; };
    function dulNacti() {
      const k = dulUlozisko();
      if (!k || dulNactenoPro === k) return;
      dulNactenoPro = k;
      dul.length = 0;
      try {
        const a = JSON.parse(localStorage.getItem(k) || '[]');
        if (Array.isArray(a)) for (const d of a) if (d && d.k && d.text) dul.push(d);
      } catch (_) { /* soukromé okno */ }
    }
    function dulUloz() {
      const k = dulUlozisko();
      if (!k) return;
      try { localStorage.setItem(k, JSON.stringify(dul)); } catch (_) { /* soukromé okno */ }
    }
    function dulVykresli(k) {
      const row = dulRadky.get(k);
      if (!row) return;
      const d = dul.find((x) => x.k === k);
      row.classList.toggle('ma-dul', !!d);
      row.style.setProperty('--dul', d ? d.barva : 'transparent');
    }
    function dulPridej(k, text, barva) {
      dulNacti();
      const d = dul.find((x) => x.k === k);
      if (d) { if (barva) d.barva = barva; }
      else dul.push({k, text: String(text).trim().split('\n')[0].slice(0, 140), barva: barva || DUL_BARVY[0]});
      dulUloz(); dulVykresli(k); zmenaPrehledu();
    }
    // Vlastní poznámka z panelu — bez zprávy, ke které by se skákalo.
    function dulNova(text, barva) {
      const t = String(text || '').trim();
      if (!t) return;
      dulNacti();
      dul.push({k: 'n:' + Date.now().toString(36) + Math.random().toString(36).slice(2, 5),
                text: t.slice(0, 140), barva: barva || DUL_BARVY[0], vlastni: true});
      dulUloz(); zmenaPrehledu();
    }
    function dulOdeber(k) {
      const i = dul.findIndex((x) => x.k === k);
      if (i >= 0) dul.splice(i, 1);
      dulUloz(); dulVykresli(k); zmenaPrehledu();
    }
    function dulBarva(k, barva) {
      const d = dul.find((x) => x.k === k);
      if (d) d.barva = barva;
      dulUloz(); dulVykresli(k); zmenaPrehledu();
    }
    /* Do schránky přes copyText z hubu (io.copy); výsledek hlásí toast. */
    function zkopiruj(text) {
      if (!copy || !text) return;
      Promise.resolve().then(() => copy(text))
        .then(() => { if (notice) notice('Zkopírováno (' + text.length + ' znaků)'); })
        .catch((err) => { if (notice) notice('Kopírování se nepovedlo: ' + ((err && err.message) || err)); });
    }
    /* Blok kódu v odpovědi → tlačítko „kopírovat“ v rohu. Blok se obalí,
       aby tlačítko stálo na místě i při vodorovném posouvání dlouhých řádků. */
    function kopirovaniKodu(koren) {
      if (!copy) return;
      for (const pre of koren.querySelectorAll('pre.vault-code')) {
        if (pre.parentNode.classList.contains('cteni-kod')) continue;
        const obal = el('div', 'cteni-kod');
        const tl = el('button', 'cteni-kod-kopie');
        tl.type = 'button';
        tl.title = 'Kopírovat';
        tl.setAttribute('aria-label', 'Kopírovat kód');
        const ikona = (id) => { tl.innerHTML = '<svg class="ico"><use href="#' + id + '"/></svg>'; };
        ikona('i-copy');
        tl.onclick = (ev) => {
          ev.stopPropagation();
          const kod = pre.querySelector('code') || pre;
          zkopiruj(kod.textContent.replace(/\n$/, ''));
          ikona('i-check');
          tl.classList.add('ok');
          setTimeout(() => { ikona('i-copy'); tl.classList.remove('ok'); }, 1500);
        };
        pre.replaceWith(obal);
        obal.append(pre, tl);
      }
    }
    /* Označený text uvnitř proudu → malá lišta „Kopírovat výběr“. Nativní
       označování prstem zůstává, lišta jen přidává tlačítko nad pole na psaní.
       Text se zachytí hned při označení — klepnutí na tlačítko by výběr mohlo
       stihnout zrušit dřív, než se přečte. */
    function pripojListuVyberu() {
      if (!copy) return;
      let lista = null, text = '';
      const skryj = () => { if (lista) lista.hidden = true; text = ''; };
      function naVyber() {
        if (!mount.isConnected) { document.removeEventListener('selectionchange', naVyber); if (lista) lista.remove(); return; }
        const sel = window.getSelection();
        const t = sel && !sel.isCollapsed ? sel.toString().trim() : '';
        if (!t || !mount.contains(sel.anchorNode) || !mount.contains(sel.focusNode)) { skryj(); return; }
        const root = mount.closest('.cteni, .onb-body');
        if (!root) return;
        if (!lista) {
          lista = el('div', 'cteni-vyber-lista');
          const tl = el('button', 'cteni-vyber-tl', 'Kopírovat výběr');
          // Bez přenosu zaostření, ať klepnutí výběr nezruší.
          tl.addEventListener('mousedown', (e) => e.preventDefault());
          tl.addEventListener('pointerdown', (e) => e.preventDefault());
          tl.onclick = () => { zkopiruj(text); skryj(); };
          lista.appendChild(tl);
        }
        if (lista.parentNode !== root) root.appendChild(lista);
        const zapis = root.querySelector('.cteni-write');
        lista.style.bottom = ((zapis ? zapis.offsetHeight : 0) + 10) + 'px';
        text = t;
        lista.hidden = false;
      }
      document.addEventListener('selectionchange', naVyber);
    }
    pripojListuVyberu();
    // Pravý klik / podržení zprávy: vybereš barvu nebo zprávu zkopíruješ.
    function dulMenu(ev, k, text) {
      ev.preventDefault();
      ev.stopPropagation();
      dulNacti();
      const d = dul.find((x) => x.k === k);
      const oznaceno = String(window.getSelection()).trim();
      paleta({x: ev.clientX, y: ev.clientY}, d && d.barva, (b) => dulPridej(k, text, b),
             d ? () => dulOdeber(k) : null,
             copy ? {zprava: () => zkopiruj(text), vyber: oznaceno ? () => zkopiruj(oznaceno) : null} : null);
    }
    /* Stejná věta napsaná dvakrát jsou dvě zprávy, každá s vlastní poznámkou:
       první má starý klíč (uložené poznámky se nerozbijí), další #2, #3… */
    const dulPocty = new Map();
    function pripojPoznamku(row, text) {
      const zaklad = dulId(text);
      if (!zaklad) return;
      const poradi = (dulPocty.get(zaklad) || 0) + 1;
      dulPocty.set(zaklad, poradi);
      const k = poradi > 1 ? zaklad + '#' + poradi : zaklad;
      dulNacti();
      row.addEventListener('contextmenu', (ev) => {
        // Odkazy a vybraný text nech prohlížeči — menu až na prázdné místo zprávy.
        if (ev.target.closest('a, img') || String(window.getSelection()).trim()) return;
        dulMenu(ev, k, text);
      });
      row.classList.add('ma-poznamku');
      dulRadky.set(k, row);
      dulVykresli(k);
    }
    function dulSkoc(row) {
      if (!row || !row.isConnected) return false;
      row.scrollIntoView({behavior: 'smooth', block: 'center'});
      row.classList.remove('dul-blik');
      void row.offsetWidth;
      row.classList.add('dul-blik');
      setTimeout(() => row.classList.remove('dul-blik'), 1800);
      return true;
    }

    let zadaniZ = null;               // zadání, které čeká na svůj řádek
    const mistni = [];                // odeslané odsud, v přepisu ještě nejsou

    function naKonec() {
      if (prace) mount.appendChild(prace);
      mount.appendChild(pozadi);
      mount.appendChild(ulohy);
      mount.appendChild(fronta);
    }

    function udelejNastroj(b) {
      const box = el('div', 'cteni-tool');
      const head = el('button', 'cteni-head');
      head.appendChild(el('span', 'cteni-caret', '▸'));
      head.appendChild(ikonaNastroje(b.name));
      head.appendChild(el('span', 'cteni-title', b.title || b.name || 'krok'));
      head.appendChild(el('span', 'cteni-bezi'));
      const meta = el('span', 'cteni-meta', b.meta || '');
      head.appendChild(meta);
      const detail = el('pre', 'cteni-detail');
      detail.hidden = true;
      detail.textContent = b.detail || '';
      head.onclick = () => {
        detail.hidden = !detail.hidden;
        box.classList.toggle('open', !detail.hidden);
      };
      box.append(head, detail);
      // Zapsaný .html soubor: vedle kódu jde ukázat i jak vypadá.
      if (b.name === 'Write' && /\.html?\b/i.test(b.title || '') && b.detail) {
        const nahled = el('button', 'cteni-html-btn cteni-html-otevrit', 'Zobrazit náhled');
        let ukazka = null;
        nahled.onclick = () => {
          if (!ukazka) {
            ukazka = nahledHtml(b.detail);
            box.appendChild(ukazka);
            nahled.textContent = 'Skrýt náhled';
          } else {
            ukazka.hidden = !ukazka.hidden;
            nahled.textContent = ukazka.hidden ? 'Zobrazit náhled' : 'Skrýt náhled';
          }
        };
        box.insertBefore(nahled, detail);
      }
      // Zapsaný / upravený soubor: nenápadné „Stáhnout" pod řádkem nástroje.
      if (b.soubor) {
        const radek = el('div', 'cteni-tool-soubor');
        radek.appendChild(tlacitkoStahnout(b.soubor, st, notice, 'tichy', '⤓ Stáhnout ' + jmenoSouboru(b.soubor)));
        box.insertBefore(radek, detail);
      }
      box.dataset.id = b.id || '';
      box._meta = meta;
      box._detail = detail;
      return box;
    }

    /* Bublina se zadáním. `stitek` = řádek pod ní: jestli ji Claude už vidí. */
    /* Ve sdíleném chatu: kdo zprávu napsal. Kolegovy zprávy chodí do chatu majitele
       jako „[Jméno] text“ (gateway/relace.py → sdilet_poslat); co prefix nemá,
       napsal majitel. Moje bubliny jsou vpravo v mé barvě, cizí vlevo a každý
       člověk má svou, vždy se jménem nad bublinou. */
    // Každý člověk v chatu dostane další barvu z palety podle pořadí, v jakém se poprvé ozval
    // (shodné pro všechny, kdo vidí stejný přepis); hash jen když lidí přibude víc než barev.
    const lide = new Map();
    function barvaLidi(kdo) {
      const k = String(kdo).toLowerCase();
      if (!lide.has(k)) lide.set(k, lide.size < KDO_PALETA.length ? KDO_PALETA[lide.size] : kdoBarva(kdo));
      return lide.get(k);
    }
    function bublina(b, cls, stitek) {
      const row = el('div', 'cteni-me' + (cls ? ' ' + cls : ''));
      let kdo = '';
      const sd = typeof sdileny === 'function' ? sdileny() : sdileny;
      if (sd && !/fronta|ceka/.test(cls || '')) {
        const m = /^\[([^\]\n]{1,60})\]\s+([\s\S]*)$/.exec(b.text || '');
        kdo = m ? m[1].trim() : (sd.majitel || '');
        if (m) b = Object.assign({}, b, {text: m[2]});
        const moje = !!sd.ja && kdo.toLowerCase() === String(sd.ja).toLowerCase();
        row.classList.add(moje ? 'cteni-moje' : 'cteni-cizi');
        // Barva podle člověka (jméno z přihlášení), i u mých bublin; jméno se píše vždy.
        row.style.setProperty('--kdo', barvaLidi(kdo));
        row.appendChild(el('div', 'cteni-kdo', kdo));
      }
      const bubble = el('div', 'cteni-bubble');
      /* Přiložený obrázek se ukáže jako obrázek, ne jako cesta k souboru —
         tu hub Claudovi napsal jen proto, aby si ho uměl otevřít. */
      const srcs = (b.images || []).map((p) => (imageUrl ? imageUrl(p) : ''))
        .filter(Boolean).concat(b.inline || []);
      if (srcs.length) {
        const imgs = el('div', 'cteni-imgs');
        for (const src of srcs) {
          const img = el('img', 'cteni-img');
          img.src = src;
          img.alt = 'obrázek';
          img.loading = 'lazy';
          img.onclick = () => zvetsit(src);
          imgs.appendChild(img);
        }
        bubble.appendChild(imgs);
      }
      if (b.text) bubble.appendChild(textSOdkazy(b.text));
      row.appendChild(bubble);
      if (stitek) row.appendChild(el('div', 'cteni-stitek', stitek));
      // Rozvětvit odsud: nový chat jen do tohohle místa (hub/vetev.py). Jen u zpráv,
      // které už jsou v přepisu (mají uuid), ne u těch, co teprve čekají na odeslání.
      if (rozvetvit && b.uuid && !sd && !/fronta|ceka/.test(cls || '')) {
        const vetev = el('button', 'cteni-vetev', '⑂ Rozvětvit odsud');
        vetev.type = 'button';
        vetev.title = 'Otevře nový chat s konverzací jen do tohohle místa (před touhle zprávou). Původní zůstane, jak je.';
        vetev.onclick = async (ev) => {
          ev.stopPropagation();
          if (vetev.disabled) return;
          vetev.disabled = true;               // dvojklik nezaloží druhou větev
          vetev.textContent = 'Větvím…';
          try { await rozvetvit(b.uuid); } finally {
            vetev.disabled = false;
            vetev.textContent = '⑂ Rozvětvit odsud';
          }
        };
        row.appendChild(vetev);
      }
      return row;
    }

    /* ── zprávy, které ještě nedošly ────────────────────────────────────────
       Co se odešle z bubliny, je vidět hned — i když Claude zrovna pracuje
       a zprávu si vezme až za chvíli. Stav pod ní říká, kde je:
         odesílá se → ve frontě (Claude ji zatím nevidí) → v konverzaci. */
    const RETRY_MS = 20000;      // nepřevzatou zprávu hub sám pošle znovu (nejvýš RETRY_MAX×)
    const RETRY_MAX = 4;
    const NEDOSLO_MS = 30000;   // bublina zkouší Enter ~25 s (composer.js, posliJednu)
    function norm(t) {
      return String(t || '').replace(/<\/?pasted_content\b[^>]*>/g, '')
        .replace(/\s+/g, ' ').trim().toLowerCase();
    }
    function sedi(m, b) {
      if (m.norm) return norm(b.text).includes(m.norm.slice(0, 48));
      return !!((b.images || []).length || (b.inline || []).length);
    }
    // Na terminál se neodkazuje nikde — lidé ho nevidí, radí se počkat.
    const STITEK = {
      start: 'Čeká, až se Claude připraví…',
      odesila: 'Odesílá se…',
      nedoslo: 'Claude zprávu zatím nepřevzal — zkusím ji poslat znovu',
      // Claude ještě dělá předchozí úkol: zpráva čeká ve frontě a vezme si ji
      // hned potom (Adam, 22. 10. 2026 — šestiminutové čekání vypadalo jako chyba).
      fronta: 'Claude ještě dokončuje práci — zprávu si vezme hned potom',
      // Zpráva je uložená na serveru (hub/fronta.py) a doručí se sama.
      server: 'Uloženo na serveru — Claude ji dostane, jakmile bude připravený',
      predano: 'Předáno Claudovi — čeká na převzetí',
      zaseklo: 'Claude se zdá zaseklý — můžeš práci přerušit a zprávu poslat hned',
      // Slash příkaz (/usage, /model…) Claude Code často do přepisu nezapíše
      // vůbec, nebo až po zavření svého okna — čekat na potvrzení nemá smysl.
      prikaz: 'Příkaz poslán Claudovi',
      // Bublina zprávu drží, dokud se spojení nevrátí — pak odejde sama.
      spojeni: 'Čeká na spojení se serverem — odejde sama',
    };
    const stitek = (stav) => STITEK[stav];
    // Jak dlouho štítek u slash příkazu zůstane, než zmizí sám.
    const PRIKAZ_MS = 8000;
    // Tak dlouho se start čeká bez poznámky; pak nejspíš visí na dotazu.
    const START_DLOUHO_MS = 45000;
    const jePrikaz = (m) => /^\/[a-z][\w:-]*(\s|$)/i.test(m.text || '');
    /* Odchozí zprávy, které drží jen tohle okno (server je nepřevzal: Claude se
       rozjíždí, spojení spadlo, příkaz z bubliny) — uložené v prohlížeči, ať je
       reload nebo restart serveru nesmaže. Po obnově se ukážou znovu jako
       „nedošla“; co mezitím Claude dostal, smaže přepis konverzace (potvrd),
       zbytek se pošle znovu. Zprávy ve frontě na serveru se ukládají tam. */
    const ODCHOZI_STAVY = new Set(['start', 'odesila', 'nedoslo', 'fronta', 'spojeni']);
    function ulozOdchozi() {
      if (!odchozi) return;
      try {
        odchozi.uloz(mistni.filter((m) => !m.server && ODCHOZI_STAVY.has(m.stav) && m.text)
          .map((m) => ({text: m.text})));
      } catch (_) { /* úložiště nedostupné */ }
    }
    function nastavMistni(m, stav) {
      if (stav === 'odesila' && jePrikaz(m)) stav = 'prikaz';
      m.stav = stav;
      m.row.className = 'cteni-me cteni-ceka mistni ' + stav;
      m.row.querySelector('.cteni-stitek').textContent = stitek(stav);
      clearTimeout(m.timer);
      if (stav === 'odesila') m.timer = setTimeout(() => nastavMistni(m, pr.pracuje ? 'fronta' : 'nedoslo'), NEDOSLO_MS);
      if (stav === 'nedoslo' && !m.server) m.timer = setTimeout(() => zkusZnovu(m), RETRY_MS);
      ukazPomoc(m);
      ukazAkce(m);
      if (stav === 'prikaz') m.timer = setTimeout(() => potvrdMistni(m), PRIKAZ_MS);
      ulozOdchozi();
      if (stav === 'start') {
        m.timer = setTimeout(() => {
          if (m.stav === 'start') {
            m.row.querySelector('.cteni-stitek').textContent =
              'Claude se pořád chystá — zkus to za chvíli znovu';
          }
        }, START_DLOUHO_MS);
      }
    }
    /* Zpráva, kterou Claude nepřevzal (nejčastěji po restartu prostoru, kdy se
       ještě rozjíždí), odejde sama znovu, jakmile je spojení. Když Claude
       pracuje, zpráva čeká ve frontě u něj a znovu se neposílá — vznikl by
       duplikát. Pozastavené odesílání se nepokouší. */
    function zkusZnovu(m, rucne) {
      if (m.server) {                            // zprávu drží server — požádat ho o nový pokus
        if (serverZnovu && m.fid) { serverZnovu(m.fid); nastavMistni(m, 'server'); }
        return;
      }
      if (m.stav !== 'nedoslo' || !mistni.includes(m)) return;
      if (pr.pracuje) { nastavMistni(m, 'fronta'); return; }
      const jeSpojeni = !jeOnline || jeOnline();
      if (!rucne && ((pozastaveno && pozastaveno()) || !jeSpojeni || (m.pokusy || 0) >= RETRY_MAX)) {
        if ((m.pokusy || 0) < RETRY_MAX && !(pozastaveno && pozastaveno())) m.timer = setTimeout(() => zkusZnovu(m), RETRY_MS);
        return;
      }
      m.pokusy = (m.pokusy || 0) + 1;
      if (znovu && znovu(m.text)) nastavMistni(m, 'odesila');
      else m.timer = setTimeout(() => zkusZnovu(m), RETRY_MS);
    }
    /* Pod zprávou, která nedošla: co se děje a dvě tlačítka — poslat hned znovu
       a pozastavit odesílání (když Claude blbne, ať se do něj nehrne další). */
    function ukazPomoc(m) {
      let box = m.row.querySelector('.cteni-pomoc');
      // Čekající zpráva má nabídku v každém stavu, ne jen když nedošla: „Odesílá se…“
      // a „čeká ve frontě“ trvají, dokud Claude pracuje, a právě tehdy je potřeba
      // zprávu poslat hned (jedna zpráva pak měla tlačítka a ta nad ní ne).
      const ceka = m.stav === 'odesila' || m.stav === 'fronta';
      if (m.stav !== 'nedoslo' && !ceka && !(m.server && (m.stav === 'server' || m.stav === 'zaseklo'))) { if (box) box.remove(); return; }
      if (!box) {
        box = el('div', 'cteni-pomoc');
        box.append(el('span', 'cteni-pomoc-text'));
        const znovuBtn = el('button', 'cteni-pomoc-btn', 'Poslat znovu');
        znovuBtn.type = 'button';
        znovuBtn.onclick = () => { m.pokusy = 0; zkusZnovu(m, true); };
        const stopBtn = el('button', 'cteni-pomoc-btn cteni-pomoc-stop', '');
        stopBtn.type = 'button';
        stopBtn.onclick = () => { if (pozastavit) pozastavit(!(pozastaveno && pozastaveno())); ukazPomoc(m); };
        const ihnedBtn = el('button', 'cteni-pomoc-btn cteni-pomoc-ihned', 'Přerušit práci a poslat hned');
        ihnedBtn.type = 'button';
        ihnedBtn.onclick = () => { if (serverIhned && m.fid) { serverIhned(m.fid); nastavMistni(m, 'server'); } };
        box.append(znovuBtn, ihnedBtn, stopBtn);
        box._znovu = znovuBtn;
        box._ihned = ihnedBtn;
        m.row.appendChild(box);
      }
      const stop = !!(pozastaveno && pozastaveno());
      // „Poslat znovu“ dává smysl jen u zprávy, která nedošla; „poslat hned“ jen když ji drží server.
      box._znovu.hidden = ceka;
      box._ihned.hidden = !(m.server && m.fid && serverIhned);
      const proc = ceka ? (pr.pracuje ? 'Claude pracuje, zpráva čeká ve frontě a odejde, jakmile bude připravený.'
                                      : 'Zpráva se odesílá Claudovi.')
        : m.stav === 'zaseklo'
        ? 'Claude už několik minut nic nezapsal — nejspíš se zasekl na nástroji. Zpráva čeká; můžeš práci přerušit a poslat ji hned.'
        : m.stav === 'server'
        ? 'Zpráva je uložená na serveru a odejde Claudovi, jakmile dokončí práci a bude připravený.'
        : (jeOnline && !jeOnline()) ? 'Spojení se serverem spadlo.'
        : pr.pracuje ? 'Claude pracuje, zpráva čeká ve frontě u něj.'
        : 'Claude teď zprávy nebere — nejspíš se po restartu ještě rozjíždí.';
      box.querySelector('.cteni-pomoc-text').textContent = proc +
        (stop ? ' Odesílání je pozastavené.'
          : ceka ? ''
          : m.server ? (m.stav === 'nedoslo' ? ' Klikni na Poslat znovu.' : '')
          : ' Zkusím to znovu' + ((m.pokusy || 0) >= RETRY_MAX ? ' — už to nezkouším, klikni na Poslat znovu.' : ' sám.'));
      box.querySelector('.cteni-pomoc-stop').textContent = stop ? 'Povolit odesílání' : 'Pozastavit odesílání';
    }
    /* Stav zprávy z fronty na serveru (WebSocket `fronta`). */
    function odServeru(msg) {
      const m = mistni.find((x) => x.fid && x.fid === msg.id);
      if (!m) return;
      if (msg.stav === 'upraveno') { zmenText(m, msg.text, !m.zamek); return; }   // zamek = vlastní uložení právě běží
      if (msg.stav === 'smazano') { potvrdMistni(m); return; }
      if (m.edit && msg.stav !== 'zaseklo') {          // zpráva mezitím odešla — úprava už nejde
        zavriEditor(m);
        hlaska('Zpráva mezitím odešla Claudovi — upravit ji už nejde.');
      }
      if (msg.stav === 'nedoslo') nastavMistni(m, 'nedoslo');
      else if (msg.stav === 'zaseklo') nastavMistni(m, 'zaseklo');
      else if (msg.stav === 'odeslano' || msg.stav === 'doruceno') nastavMistni(m, 'predano');
      else nastavMistni(m, 'server');
    }
    /* Po obnovení okna: co na serveru ještě čeká, se ukáže znovu jako bublina. */
    function obnovFrontu(zpravy) {
      for (const z of zpravy || []) {
        if (mistni.some((x) => x.fid === z.id)) continue;
        odeslano({text: z.text, fid: z.id}, 'server');
        const m = mistni[mistni.length - 1];
        if (z.stav === 'nedoslo') nastavMistni(m, 'nedoslo');
        else if (z.stav === 'odeslano') nastavMistni(m, 'predano');
      }
    }
    /* ── úprava a smazání zprávy, která ještě čeká ────────────────────────────
       Jde to jen dokud server zprávu nenapsal Claudovi (hub/fronta.py: stav
       „ceka“). Server rozhoduje — okno jen ukáže tlačítka a po odpovědi se
       srovná podle skutečného stavu (i když mezitím zpráva odešla). */
    const hlaska = (t) => { if (notice) notice(t); };
    const MAX_UPRAVA = 20000;
    function ukazAkce(m) {
      let box = m.row.querySelector('.cteni-akce');
      const lze = m.server && m.fid && serverUprav && (m.stav === 'server' || m.stav === 'zaseklo');
      if (!lze) { if (box) box.remove(); if (m.edit) zavriEditor(m); return; }
      if (box) return;
      box = el('div', 'cteni-akce');
      const uprBtn = el('button', 'cteni-pomoc-btn cteni-akce-uprav', 'Upravit');
      uprBtn.type = 'button';
      uprBtn.onclick = () => otevriEditor(m);
      const smazBtn = el('button', 'cteni-pomoc-btn cteni-akce-smaz', 'Smazat');
      smazBtn.type = 'button';
      smazBtn.onclick = () => smazZpravu(m);
      box.append(uprBtn, smazBtn);
      m.row.appendChild(box);
    }
    function zmenText(m, text, odjinud) {
      m.text = String(text || '').trim();
      m.norm = norm(m.text);
      const bubble = m.row.querySelector('.cteni-bubble');
      if (bubble) {
        for (const ch of Array.from(bubble.children)) if (!ch.classList.contains('cteni-imgs')) ch.remove();
        bubble.appendChild(textSOdkazy(m.text));
      }
      if (m.edit) {                               // jiné okno předběhlo; rozepsaný text se zahodí
        zavriEditor(m);
        if (odjinud) hlaska('Zprávu mezitím upravilo jiné okno — ukazuju její novou podobu.');
      }
    }
    function zavriEditor(m) {
      if (!m.edit) return;
      m.edit.remove();
      m.edit = null;
      const bubble = m.row.querySelector('.cteni-bubble');
      if (bubble) bubble.hidden = false;
      const box = m.row.querySelector('.cteni-akce');
      if (box) box.hidden = false;
    }
    /* Server řekl, že zpráva upravit/smazat nejde: srovnat okno se skutečností. */
    function vysledekNeslo(m, r) {
      hlaska((r && r.duvod) || 'Zprávu teď nejde změnit.');
      if (!r || r.stav === 'neni') potvrdMistni(m);                // doručená nebo smazaná — přepis ukáže pravdu
      else if (r.stav === 'odeslano' || r.stav === 'nedoslo') { zavriEditor(m); nastavMistni(m, r.stav === 'nedoslo' ? 'nedoslo' : 'predano'); }
    }
    async function otevriEditor(m) {
      if (m.edit || m.zamek || !serverSeznam) return;
      m.zamek = true;
      try {
        // Upravuje se text, který drží server (ten může mít navíc cesty k přílohám).
        const r = await serverSeznam();
        const z = r && (r.zpravy || []).find((x) => x.id === m.fid);
        if (!r) { hlaska('Nepodařilo se spojit se serverem — zkus to za chvíli.'); return; }
        if (!z) { vysledekNeslo(m, {stav: 'neni', duvod: 'Zpráva už ve frontě není — Claude ji převzal, nebo byla smazaná.'}); return; }
        if (z.stav !== 'ceka') {
          vysledekNeslo(m, {stav: z.stav, duvod: 'Zpráva už je napsaná Claudovi, upravit ji nejde.'});
          return;
        }
        const box = el('div', 'cteni-editor');
        const ta = el('textarea', 'cteni-editor-pole');
        ta.value = z.text;
        ta.maxLength = MAX_UPRAVA;
        ta.rows = Math.min(12, Math.max(3, z.text.split('\n').length + 1));
        const tl = el('div', 'cteni-pomoc');
        const ulozBtn = el('button', 'cteni-pomoc-btn', 'Uložit');
        ulozBtn.type = 'button';
        const zrusBtn = el('button', 'cteni-pomoc-btn', 'Zrušit');
        zrusBtn.type = 'button';
        zrusBtn.onclick = () => zavriEditor(m);
        ulozBtn.onclick = () => ulozUpravu(m, ta, ulozBtn);
        ta.addEventListener('keydown', (ev) => {
          if (ev.key === 'Escape') { ev.preventDefault(); zavriEditor(m); }
          else if (ev.key === 'Enter' && (ev.ctrlKey || ev.metaKey)) { ev.preventDefault(); ulozBtn.click(); }
        });
        tl.append(ulozBtn, zrusBtn);
        box.append(ta, tl);
        const bubble = m.row.querySelector('.cteni-bubble');
        if (bubble) bubble.hidden = true;
        const akce = m.row.querySelector('.cteni-akce');
        if (akce) akce.hidden = true;
        m.row.insertBefore(box, m.row.querySelector('.cteni-stitek'));
        m.edit = box;
        ta.focus();
      } catch (_) {
        hlaska('Úpravu se nepodařilo otevřít.');
      } finally {
        m.zamek = false;
      }
    }
    async function ulozUpravu(m, ta, btn) {
      if (m.zamek) return;                        // dvojklik
      const text = ta.value;
      if (!text.trim()) {                         // prázdná úprava = smazání, ale jen s potvrzením
        m.zamek = true;
        try {
          const dlg = global.HubDialog;
          const ano = dlg ? await dlg.confirm('Zpráva je prázdná. Smazat ji z fronty?', {ok: 'Smazat', title: 'Smazat zprávu'})
                          : global.confirm('Zpráva je prázdná. Smazat ji z fronty?');
          if (!ano) return;
        } finally { m.zamek = false; }
        return smazZpravu(m, true);
      }
      if (text.length > MAX_UPRAVA) { hlaska('Zpráva je moc dlouhá (nejvýš ' + MAX_UPRAVA + ' znaků).'); return; }
      m.zamek = true;
      btn.disabled = true;
      try {
        const r = await serverUprav(m.fid, text);
        if (r && r.ok) { zmenText(m, r.text, false); zavriEditor(m); }
        else if (r && r.stav === 'ceka') hlaska(r.duvod || 'Úprava se nepovedla.');   // chyba vstupu — editor zůstane
        else vysledekNeslo(m, r);
      } catch (_) {
        hlaska('Nepodařilo se spojit se serverem — zkus to za chvíli.');
      } finally {
        m.zamek = false;
        btn.disabled = false;
      }
    }
    async function smazZpravu(m, bezPtani) {
      if (m.zamek && !bezPtani) return;
      if (!serverSmaz) return;
      m.zamek = true;
      try {
        if (!bezPtani) {
          const dlg = global.HubDialog;
          const ano = dlg ? await dlg.confirm('Smazat tuto zprávu z fronty? Claude ji nedostane.', {ok: 'Smazat', title: 'Smazat zprávu'})
                          : global.confirm('Smazat tuto zprávu z fronty? Claude ji nedostane.');
          if (!ano) return;
        }
        const r = await serverSmaz(m.fid);
        if (r && r.ok) potvrdMistni(m);
        else vysledekNeslo(m, r);
      } catch (_) {
        hlaska('Nepodařilo se spojit se serverem — zkus to za chvíli.');
      } finally {
        m.zamek = false;
      }
    }
    function potvrdMistni(m) {
      clearTimeout(m.timer);
      m.row.remove();
      const i = mistni.indexOf(m);
      if (i >= 0) mistni.splice(i, 1);
      ulozOdchozi();
    }
    // Obnova po reloadu / restartu: neodeslané zprávy z minula zpátky do fronty.
    function obnovOdchozi() {
      let ulozene = [];
      try { ulozene = (odchozi && odchozi.nacti()) || []; } catch (_) { ulozene = []; }
      for (const z of ulozene) {
        const text = String((z && z.text) || '').trim();
        if (!text || mistni.some((m) => m.text === text)) continue;
        const m = {norm: norm(text), text, row: bublina({text}, '', ' '), fid: '', server: false};
        mistni.push(m);
        fronta.appendChild(m.row);
        nastavMistni(m, 'nedoslo');
      }
    }
    function odeslano(z, stav) {
      if (!z) {
        if (stav === 'spojeni') {                // spojení spadlo — neodeslané čekají
          for (const m of mistni) if (m.stav === 'odesila' || m.stav === 'nedoslo') nastavMistni(m, 'spojeni');
          return;
        }
        // zpráva ze startu právě odešla, nebo se vrátilo spojení
        for (const m of mistni) if (m.stav === 'start' || m.stav === 'spojeni') nastavMistni(m, 'odesila');
        return;
      }
      // Zpráva, co čekala na dopsání příkazů okna (composer.js submit), už bublinu
      // má — jen se jí změní stav, ať se neukáže dvakrát.
      const byla = z.fid && mistni.find((x) => x.fid === z.fid);
      if (byla) {
        if (stav === 'server') byla.server = true;
        nastavMistni(byla, stav === 'server' ? 'server' : 'odesila');
        return;
      }
      const m = {norm: norm(z.text), text: String(z.text || '').trim(), row: bublina(z, '', ' '),
                 fid: z.fid || '', server: stav === 'server'};
      mistni.push(m);
      fronta.appendChild(m.row);
      nastavMistni(m, stav === 'start' ? 'start' : stav === 'server' ? 'server' : 'odesila');
      naKonec();
    }
    /* Jeden blok z přepisu potvrdí jednu odeslanou zprávu — tu nejstarší, která
       sedí. Dvě stejné věty po sobě jsou dvě zprávy: kdyby první blok smazal
       obě, druhá by zmizela z konverzace, než ji Claude vůbec přečte. */
    function potvrd(b) {
      const m = mistni.find((x) => sedi(x, b));
      if (m) potvrdMistni(m);
    }
    function zFronty(key) {
      for (const seznam of [cekajici, ztraceneRadky]) {
        const i = seznam.findIndex((c) => c.key === key);
        if (i < 0) continue;
        seznam[i].row.remove();
        seznam.splice(i, 1);
        return;
      }
    }

    /* ── karty agentů ───────────────────────────────────────────────────── */
    function udelejAgenta(b) {
      const k = {id: b.id, typ: b.type, stav: 'bezi', bg: !!b.bg, model: b.model || '',
                 start: Date.parse(b.ts || '') || 0, ms: 0, tools: 0, tokens: 0,
                 kroky: [], prompt: b.prompt || '', vysledek: '', souhrn: '',
                 konecOd: 0, otevreno: ''};
      k.barva = dalsiBarva();
      const box = el('div', 'ag bezi');
      box.style.setProperty('--ag', k.barva);
      box.innerHTML = `
        <div class="ag-top">
          <span class="ag-avatar"></span>
          <span class="ag-jmeno">
            <span class="ag-radek"><b class="ag-typ"></b><span class="ag-model"></span><span class="ag-bg">na pozadí</span></span>
            <span class="ag-ukol"></span>
          </span>
          <span class="ag-stav"><i></i><span class="ag-stav-t"></span><span class="ag-cas"></span></span>
        </div>
        <div class="ag-prubeh" aria-hidden="true"></div>
        <ol class="ag-kroky"></ol>
        <div class="ag-nahled vault-md" hidden></div>
        <div class="ag-pata">
          <span class="ag-cisla"></span>
          <button class="ag-btn" data-v="prompt">Zadání</button>
          <button class="ag-btn" data-v="vysledek" hidden>Výsledek</button>
        </div>
        <div class="ag-telo" hidden></div>`;
      const q = (sel) => box.querySelector(sel);
      q('.ag-avatar').innerHTML = claudik(b.type);
      q('.ag-avatar').title = jmenoAgenta(b.type);
      q('.ag-typ').textContent = jmenoAgenta(b.type);
      q('.ag-ukol').textContent = b.title || 'bez popisu';
      const telo = q('.ag-telo');
      for (const btn of box.querySelectorAll('.ag-btn')) {
        btn.onclick = () => {
          const co = btn.dataset.v;
          k.otevreno = k.otevreno === co ? '' : co;
          for (const x of box.querySelectorAll('.ag-btn')) x.classList.toggle('on', x.dataset.v === k.otevreno);
          telo.hidden = !k.otevreno;
          telo.className = 'ag-telo' + (co === 'vysledek' ? ' vault-md' : '');
          if (co === 'prompt') telo.textContent = k.prompt || '(bez zadání)';
          else {
            const html = markdown(k.vysledek);
            if (html) { telo.innerHTML = html; ozivHtml(telo); }
            else telo.textContent = k.vysledek;
          }
          // Náhled výsledku je jen upoutávka — s rozbaleným celým by byl dvakrát.
          q('.ag-nahled').hidden = k.otevreno === 'vysledek' || !k.vysledek;
        };
      }
      k.box = box;
      k.q = q;
      return k;
    }

    function kresliAgenta(k) {
      const {box, q} = k;
      for (const s of Object.keys(AG_STAV)) box.classList.toggle(s, k.stav === s);
      q('.ag-stav-t').textContent = AG_STAV[k.stav];
      q('.ag-model').textContent = jmenoModelu(k.model);
      q('.ag-bg').hidden = !k.bg || k.stav !== 'bezi';
      const kroky = q('.ag-kroky');
      kroky.hidden = k.stav !== 'bezi';
      const prubeh = q('.ag-prubeh');
      prubeh.hidden = k.stav !== 'bezi' || !k.tools;
      if (k.stav === 'bezi') {
        const sig = k.kroky.map((x) => x.title + '|' + (x.ts || 0)).join('\n');
        if (sig !== kroky.dataset.sig) {
          const stare = kroky.dataset.sig ? kroky.dataset.sig.split('\n') : [];
          kroky.dataset.sig = sig;
          if (udalost && k.kroky.length) udalost({co: 'krok', id: k.id, text: k.kroky[k.kroky.length - 1].title});
          kroky.textContent = '';
          const ukazat = k.kroky.slice(-AG_KROKU);
          if (!ukazat.length) kroky.appendChild(el('li', 'ag-krok prazdny', 'Rozjíždí se…'));
          ukazat.forEach((x, i) => {
            const li = el('li', 'ag-krok');
            li.append(el('span', 'ag-krok-ico', ZNAK[x.name] || '•'), el('span', 'ag-krok-t', x.title),
                      el('span', 'ag-krok-cas'));
            if (i === ukazat.length - 1) li.classList.add('ted');
            if (!stare.some((r) => r.startsWith(x.title + '|'))) li.classList.add('nove');
            kroky.appendChild(li);
          });
        }
        // Dílky průběhu: kolik kroků agent udělal (celkem se dopředu nedá znát).
        const dilku = Math.min(k.tools, 12);
        if (prubeh.childElementCount !== dilku) {
          prubeh.textContent = '';
          for (let i = 0; i < dilku; i++) prubeh.appendChild(el('i'));
        }
        prubeh.classList.toggle('plny', k.tools > 12);
        const posl = prubeh.lastElementChild;
        for (const d of prubeh.children) d.classList.toggle('ted', d === posl);
      }
      const nahled = q('.ag-nahled');
      if (k.vysledek && nahled.dataset.src !== k.vysledek) {
        nahled.dataset.src = k.vysledek;
        const html = markdown(k.vysledek);
        if (html) { nahled.innerHTML = html; ozivHtml(nahled); }
        else nahled.textContent = k.vysledek;
      }
      nahled.hidden = !k.vysledek || k.otevreno === 'vysledek';
      q('[data-v=vysledek]').hidden = !k.vysledek;
      const cisla = [];
      if (k.tools) cisla.push(pocet(k.tools, 'krok', 'kroky', 'kroků'));
      // Tokeny jsou jen pro pokročilé.
      if (k.tokens && pokrocile()) cisla.push(tokeny(k.tokens));
      if (k.stav !== 'bezi' && k.ms) cisla.push(cas(k.ms));
      if (k.stav === 'chyba' && k.souhrn && !k.vysledek) cisla.push(k.souhrn);
      q('.ag-cisla').textContent = cisla.join(' · ');
      tikAgenta(k);
      if (k.skupina) hlavaSkupiny(k.skupina);
      zmenaPrehledu();
    }

    function tikAgenta(k) {
      // Doba kroků: u hotových do začátku dalšího, u posledního do teď.
      if (k.stav === 'bezi') {
        const radky = k.box.querySelectorAll('.ag-krok');
        const ukazat = k.kroky.slice(-AG_KROKU);
        if (radky.length === ukazat.length) {
          const ted = Date.now() - posun;
          ukazat.forEach((x, i) => {
            const dalsi = ukazat[i + 1];
            const od = x.ts || 0, do_ = dalsi ? dalsi.ts || 0 : ted;
            const c = radky[i].querySelector('.ag-krok-cas');
            if (c) c.textContent = od && do_ >= od ? casKroku(do_ - od) : '';
          });
        }
      }
      const t = k.q('.ag-cas');
      if (k.stav === 'bezi') t.textContent = k.start ? cas(Date.now() - posun - k.start) : '';
      else t.textContent = '';
    }

    function hlavaSkupiny(g) {
      const karty = g.karty;
      g.box.classList.toggle('vic', karty.length > 1);
      if (karty.length < 2) return;
      const bezi = karty.filter((k) => k.stav === 'bezi').length;
      g.pocet.textContent = (pokrocile()
        ? pocet(karty.length, 'agent', 'agenti', 'agentů')
        : pocet(karty.length, 'Claudík', 'Claudíci', 'Claudíků')) + ' najednou';
      const kus = [];
      if (bezi) kus.push(bezi + ' běží');
      if (karty.length - bezi) kus.push((karty.length - bezi) + ' hotovo');
      g.stav.textContent = kus.join(' · ');
    }

    function pridejAgenta(b) {
      if (agenti.has(b.id)) return;
      const k = udelejAgenta(b);
      // Agenti puštění jednou zprávou stojí pohromadě, jako strom.
      if (!skupina || mount.lastElementChild !== skupina.box) {
        const box = el('div', 'ag-skupina');
        const hlava = el('div', 'ag-hlava');
        const pocetEl = el('b', 'ag-hlava-n');
        const stavEl = el('span', 'ag-hlava-stav');
        hlava.append(el('span', 'ag-hlava-ico', '⛬'), pocetEl, stavEl);
        const strom = el('div', 'ag-strom');
        box.append(hlava, strom);
        skupina = {box, strom, karty: [], pocet: pocetEl, stav: stavEl};
        mount.appendChild(box);
      }
      skupina.karty.push(k);
      skupina.strom.appendChild(k.box);
      k.skupina = skupina;
      agenti.set(b.id, k);
      kresliAgenta(k);
      hlidej();
      // Na hřiště (claudici.js) — pomocník vyskočí z hlavního Claudíka.
      if (udalost) udalost({co: 'agent', id: b.id, typ: b.type, barva: k.barva, ukol: b.title || ''});
    }

    function doplnAgenta(k, info) {
      if (!info) return;
      if (info.model) k.model = info.model;
      if (typeof info.tools === 'number' && info.tools > k.tools) k.tools = info.tools;
      if (info.tokens) k.tokens = info.tokens;
      if (info.ms) k.ms = info.ms;
    }

    /* Hotovo podle hlášky o doběhlé úloze (`task`) nebo podle výsledku. */
    function dobehl(k, stav, vysledek) {
      if (udalost && k.stav === 'bezi' && stav !== 'bezi') udalost({co: 'konec', id: k.id, stav});
      k.stav = stav;
      if (vysledek) k.vysledek = vysledek;
      if (!k.ms && k.start && k.posledni) k.ms = Math.max(0, k.posledni - k.start);
    }

    /* Průběh z přepisů agentů (/api/cteni-agenti). */
    function prubeh(res) {
      if (!res) return;
      if (res.now) posun = Date.now() - res.now;
      for (const a of res.agents || []) {
        const k = agenti.get(a.id);
        if (!k) continue;
        if (a.start) k.start = a.start;
        if (a.update) k.posledni = a.update;
        if (a.tools > k.tools) k.tools = a.tools;
        if (a.tokens && (k.stav === 'bezi' || !k.tokens)) k.tokens = a.tokens;
        if (a.model) k.model = a.model;       // čím agent doopravdy jede
        if (Array.isArray(a.last)) k.kroky = a.last;
        /* Agent skončil, ale hláška o tom ještě nedorazila (nebo v přepisu
           staré konverzace chybí): po chvíli se bere jako hotový. */
        if (k.stav === 'bezi' && a.konec) {
          k.konecOd = k.konecOd || Date.now();
          if (historie || Date.now() - k.konecOd > 8000) dobehl(k, 'hotovo', '');
        } else if (!a.konec) {
          k.konecOd = 0;
        }
        kresliAgenta(k);
      }
      uklidAgenty();
      hlidej();
    }

    /* Agent, jehož hláška o konci nikdy nepřišla (Claude ho zabil, prostor se
       restartoval, přepis skončil uprostřed kroku), by běžel navěky. Na
       pozadí agent platí jen dokud ho server vidí mezi úlohami (ty, co
       naběhly před startem procesu Claude Code, tam nejsou); agent, na
       kterého Claude čeká, musí aspoň psát — jinak je hotovo, jakmile Claude
       sám přestal pracovat. */
    const AGENT_TICHO_MS = 90000;
    let ulohyNacteny = false;
    function uklidAgenty() {
      if (historie || !ulohyNacteny) return;
      const ted = Date.now() - posun;
      const zije = new Set(ulohyData.map((u) => u.id));
      let zmenil = false;
      for (const k of bezici()) {
        if (zije.has(k.id)) continue;
        if (!k.posledni || ted - k.posledni < AGENT_TICHO_MS) continue;
        if (!k.bg && pr.pracuje) continue;
        dobehl(k, 'hotovo', '');
        kresliAgenta(k);
        zmenil = true;
      }
      if (zmenil) { hlidej(); zmenaPrehledu(); }
    }

    function trvani(ms) {
      const min = Math.floor(ms / 60000);
      if (min < 1) return 'chvilku';
      if (min < 60) return min + ' min';
      return Math.floor(min / 60) + ' h ' + (min % 60) + ' min';
    }
    function kresliUlohy() {
      const seznam = historie ? [] : ulohyData.filter((u) => u.name !== 'Agent');
      ulohy.hidden = !seznam.length;
      ulohy.textContent = '';
      if (!seznam.length) return;
      const hlava = el('div', 'cteni-ulohy-hlava');
      hlava.append(el('span', 'cteni-pozadi-tecka'),
                   el('span', '', seznam.length === 1 ? 'Na pozadí běží'
                     : 'Na pozadí běží ' + seznam.length + '×'));
      ulohy.appendChild(hlava);
      for (const u of seznam) {
        const row = el('details', 'cteni-uloha');
        const sum = el('summary');
        const budik = u.name === 'ScheduleWakeup';
        const ted = Date.now() - posun;
        sum.append(el('span', 'cteni-uloha-druh',
                      budik ? 'ozve se' : u.name === 'Monitor' ? 'hlídá' : 'příkaz'),
                   el('span', 'cteni-uloha-co', u.title || u.name),
                   el('span', 'cteni-uloha-cas', budik
                     ? (u.do > ted ? 'za ' + trvani(u.do - ted + 59000) : 'teď')
                     : u.od ? trvani(ted - u.od) : ''));
        row.appendChild(sum);
        if (u.detail) row.appendChild(el('pre', 'cteni-uloha-detail', u.detail));
        ulohy.appendChild(row);
      }
    }
    function ulohyZe(res) {
      if (!res) return;
      if (res.now) posun = Date.now() - res.now;
      const otevrene = new Set([...ulohy.querySelectorAll('details[open]')].map((d) => d.dataset.id));
      ulohyData = res.tasks || [];
      ulohyNacteny = true;
      uklidAgenty();
      kresliUlohy();
      zmenaPrehledu();
      // Rozbalený detail zůstane rozbalený i po překreslení.
      [...ulohy.querySelectorAll('details')].forEach((d, i) => {
        const u = ulohyData.filter((x) => x.name !== 'Agent')[i];
        if (u) { d.dataset.id = u.id; if (otevrene.has(u.id)) d.open = true; }
      });
    }

    /* Hodiny u běžících agentů a proužek „pracují na pozadí". */
    let tikani = null;
    function bezici() {
      return [...agenti.values()].filter((k) => k.stav === 'bezi');
    }
    function hlidej() {
      const bezi = bezici();
      pozadi.hidden = !bezi.length || historie;
      if (bezi.length) {
        const jmena = bezi.map((k) => jmenoAgenta(k.typ));
        const prvni = jmena[0] + (jmena.length > 1 ? ' a ' + pocet(jmena.length - 1, 'další', 'další', 'dalších') : '');
        pozadi.textContent = '';
        pozadi.append(el('span', 'cteni-pozadi-tecka'),
                      el('span', '', prvni + (jmena.length > 1 ? ' pracují' : ' pracuje') + ' — ukázat'));
        pozadi.onclick = () => bezi[0].box.scrollIntoView({behavior: 'smooth', block: 'center'});
      }
      if (bezi.length && !tikani) {
        tikani = setInterval(() => {
          const ted = bezici();
          for (const k of ted) tikAgenta(k);
          if (!ted.length) { clearInterval(tikani); tikani = null; }
        }, 1000);
      }
    }

    /* Zprávy ve frontě, které Claude Code zahodil. Fronta žije jen v jeho
       paměti: restart tabu (nebo pád) ji smaže a v přepisu po ní zbyde jen
       zařazení. Pozná se to podle další zprávy, která z fronty nepřišla —
       frontou by před ní šel `dequeue`. Takové bubliny zůstanou vidět, ale
       řeknou pravdu: neodešly. */
    let zFrontyDalsi = false;
    // Bubliny označené jako nedoručené. Kdyby zpráva přece jen došla (úsudek
    // byl mylný, nebo ji po restartu doručil hub), bublina zmizí.
    const ztraceneRadky = [];
    function ztracene(krome) {
      for (const c of cekajici.splice(0)) {
        if (c.key === krome) { c.row.remove(); continue; }
        c.row.classList.remove('fronta');
        c.row.classList.add('ztracena');
        const st = c.row.querySelector('.cteni-stitek');
        if (st) st.textContent = 'Nedoručeno — pošli to znovu.';
        ztraceneRadky.push(c);
      }
    }
    /* Po restartu hub zprávy ze smazané fronty Claudovi doručil v připomínce
       (hub/restart.py) — bubliny se přesunou do konverzace jako přečtené. */
    function dorucenoPoRestartu(key) {
      const c = cekajici.find((x) => x.key === key) || ztraceneRadky.find((x) => x.key === key);
      if (!c) return;
      for (const seznam of [cekajici, ztraceneRadky]) {
        const i = seznam.indexOf(c);
        if (i >= 0) seznam.splice(i, 1);
      }
      c.row.className = 'cteni-me mid';
      const st = c.row.querySelector('.cteni-stitek');
      if (st) st.textContent = '✓ Doručeno po restartu';
      mount.appendChild(c.row);
    }

    function pridej(b) {
      if (b.kind === 'dequeue') {
        zFrontyDalsi = true;
        return;
      }
      if (b.kind === 'souhrn') {
        // Zhuštění konverzace — souhrn píše Claude Code, ne člověk.
        const box = udelejNastroj({name: 'hub', title: 'Claude si zhustil konverzaci (souhrn)',
                                   detail: b.text || ''});
        box.classList.add('hub-pozn');
        mount.appendChild(box);
        return;
      }
      if (b.kind === 'me') {
        if (volby) { volby.remove(); volby = null; }
        for (const key of b.doruceno || []) dorucenoPoRestartu(key);
        /* Stejná věta dvakrát za sebou: zařazení druhé do fronty se v přepisu
           často zapíše dřív než zadání první. Zadání pak vypadá jako „to, co
           jsem čekal z fronty" a druhou větu by smazalo. Když k němu ale máme
           vlastní odeslanou bublinu a ve frontě čeká totéž, je to ta první
           (přímo odeslaná) — fronta se nechá být, druhá dorazí sama. */
        const dvojice = !b.mid && !zFrontyDalsi && !!b.key &&
          cekajici.some((c) => c.key === b.key) && mistni.some((m) => sedi(m, b));
        if (!b.mid) {
          if (zFrontyDalsi) zFrontyDalsi = false;
          else if (cekajici.length && !dvojice) ztracene(b.key);
        }
        if (b.key && !dvojice) zFronty(b.key);           // z fronty rovnou do konverzace
        potvrd(b);
        // /model, /effort — co se nastavilo, řekne tichý řádek z výpisu.
        if (b.tichy) return;
        /* Připomínka od hubu (restart.py) není zpráva člověka — jeden tichý
           řádek, podrobnosti po klepnutí. */
        if (!/^\[hub\] /.test(b.text || '') && b.text) {
          pr.zadani.push({text: b.text.trim().split('\n')[0].slice(0, 140)});
          pr.kroky = [];
          tah = {soubory: [], karta: null};
          zadaniZ = pr.zadani[pr.zadani.length - 1];
          zmenaPrehledu();
        }
        if (/^\[hub\] /.test(b.text || '')) {
          const text = b.text.slice(6);
          const ulohy = (text.match(/^- (agent|hlídání|naplánované probuzení[^:\n]*|příkaz): /gm) || []).length;
          const box = udelejNastroj({name: 'hub',
            title: /restartoval/.test(text)
              ? 'Appka se restartovala' + (ulohy ? ' — Claude navazuje na ' + pocet(ulohy, 'úlohu', 'úlohy', 'úloh')
                : /přerušil tě/.test(text) ? ' — Claude navazuje na rozdělanou práci' : '')
              : text.split('\n')[0].slice(0, 120),
            detail: text});
          box.classList.add('hub-pozn');
          mount.appendChild(box);
          return;
        }
        const radek = bublina(b, b.mid ? 'mid' : '',
          b.mid ? '✓ Claude si to přečetl během práce' : '');
        mount.appendChild(radek);
        if (zadaniZ) { zadaniZ.row = radek; zadaniZ = null; }
        if (b.text) pripojPoznamku(radek, b.text);
        if (b.uuid && global.HubRewind && !(typeof sdileny === 'function' ? sdileny() : sdileny)) global.HubRewind.pripoj(radek, b);
        return;
      }
      if (b.kind === 'queued') {
        potvrd(b);
        const row = bublina(b, 'cteni-ceka fronta', 'Ve frontě — Claude to uvidí, až dokončí krok');
        cekajici.push({key: b.key, row});
        fronta.insertBefore(row, fronta.querySelector('.mistni'));
        return;
      }
      if (b.kind === 'unqueue') {
        if (b.all) {
          for (const c of cekajici) c.row.remove();
          cekajici.length = 0;
        } else {
          zFronty(b.key);
        }
        return;
      }
      if (b.kind === 'out' && b.tichy) {
        // Přepnutí modelu nebo úsilí — dva po sobě se slijí do jednoho řádku.
        const posledni = mount.lastElementChild;
        if (posledni && posledni.classList.contains('cteni-nastaveni')) {
          posledni.textContent += ' · ' + b.text;
        } else {
          mount.appendChild(el('div', 'cteni-nastaveni', b.text));
        }
        return;
      }
      if (b.kind === 'out') {
        // Výpis slash příkazu (/login, /logout…) — odpověď Claude Code, ne Clauda.
        const box = el('div', 'cteni-vypis' + (b.err ? ' chyba' : ''));
        box.append(el('span', 'cteni-vypis-ico', b.err ? '⚠' : '›'), el('span', '', b.text));
        mount.appendChild(box);
        return;
      }
      if (b.kind === 'say') {
        if (volby) { volby.remove(); volby = null; }
        const box = el('div', 'cteni-say vault-md');
        const html = markdown(b.text);
        if (html) { box.innerHTML = html; ozivHtml(box); kopirovaniKodu(box); }
        else box.textContent = b.text;
        mount.appendChild(box);
        // Karta se soubory ke stažení jde vždycky pod poslední text odpovědi.
        if (tah.karta) mount.appendChild(tah.karta);
        const moznosti = odpovez ? rychleOdpovedi(b.text) : [];
        if (moznosti.length) {
          volby = el('div', 'cteni-volby');
          for (const text of moznosti) {
            const btn = el('button', 'cteni-volba', text);
            btn.type = 'button';
            btn.title = 'Odeslat Claudovi: ' + text;
            btn.onclick = () => {
              if (!odpovez(text)) { if (notice) notice('Odpověď teď nejde odeslat — napiš ji dole.'); return; }
              if (volby) { volby.remove(); volby = null; }
            };
            volby.appendChild(btn);
          }
          box.appendChild(volby);
        }
        pripojPoznamku(box, b.text);
        // Lišta pod odpovědí jako na claude.ai: kopírovat (zobrazuje ji motiv Claude).
        if (copy && b.text) {
          const ak = el('div', 'cteni-odp-akce');
          const kop = el('button', 'cteni-odp-btn');
          kop.type = 'button';
          kop.title = 'Kopírovat odpověď';
          kop.innerHTML = '<svg class="ico"><use href="#i-copy"/></svg>';
          kop.onclick = () => { copy(b.text); if (notice) notice('Odpověď zkopírována.'); };
          ak.appendChild(kop);
          /* Palec nahoru / dolů: jen záznam v tomhle prohlížeči (Claude Code nemá kam
             hodnocení poslat) — ať je vidět, co se povedlo. Klepnutí podruhé ho zruší. */
          const klic = 'hub.palec.' + (b.uuid || norm(b.text).slice(0, 60));
          let hod = '';
          try { hod = localStorage.getItem(klic) || ''; } catch (_) { hod = ''; }
          const palec = (druh, ico, titul) => {
            const t = el('button', 'cteni-odp-btn palec' + (hod === druh ? ' on' : ''));
            t.type = 'button';
            t.title = titul;
            t.dataset.druh = druh;
            t.innerHTML = '<svg class="ico"><use href="#' + ico + '"/></svg>';
            t.onclick = () => {
              hod = hod === druh ? '' : druh;
              try { if (hod) localStorage.setItem(klic, hod); else localStorage.removeItem(klic); } catch (_) { /* nic */ }
              for (const x of ak.querySelectorAll('.palec')) x.classList.toggle('on', x.dataset.druh === hod);
            };
            return t;
          };
          ak.append(palec('up', 'i-thumb-up', 'Dobrá odpověď'), palec('down', 'i-thumb-down', 'Špatná odpověď'));
          /* Znovu: pošle Claudovi totéž zadání ještě jednou (Claude Code odpověď přegenerovat neumí). */
          const zadani = pr.zadani.length ? pr.zadani[pr.zadani.length - 1].text : '';
          if (odpovez && zadani) {
            const znovuB = el('button', 'cteni-odp-btn');
            znovuB.type = 'button';
            znovuB.title = 'Zkusit znovu — pošle Claudovi totéž zadání';
            znovuB.innerHTML = '<svg class="ico"><use href="#i-refresh"/></svg>';
            znovuB.onclick = () => { if (!odpovez(zadani) && notice) notice('Teď to nejde odeslat — napiš to dole.'); };
            ak.appendChild(znovuB);
          }
          box.appendChild(ak);
        }
        // Přečíst nahlas (hlas.js) — tlačítko v rohu bubliny. Nová odpověď
        // v tabu, na který se člověk dívá, se se zapnutým předčítáním čte sama.
        if (global.HubHlas && b.text) {
          global.HubHlas.tlacitko(box, b.text, notice);
          if (zive && zive()) global.HubHlas.speak(b.text, null, notice);
        }
        return;
      }
      if (b.kind === 'tool') {
        if (b.todos) { pr.todos = b.todos; zmenaPrehledu(); }
        if (b.name !== 'TodoWrite' && !/^Task(Create|Update|List|Get)$/.test(b.name || '')) {
          pr.kroky.push({id: b.id, text: b.title || b.name, znak: ZNAK[b.name] || '•', stav: 'bezi'});
          if (pr.kroky.length > 200) pr.kroky.shift();
          zmenaPrehledu();
        }
        if (b.ukol) {
          pr.tasks.push({id: String(pr.tasks.length + 1), text: b.ukol.text, ted: b.ukol.ted, stav: 'pending'});
          zmenaPrehledu();
        }
        if (b.ukol_zmena) {
          const t = pr.tasks.find((x) => x.id === b.ukol_zmena.id);
          if (t) {
            if (b.ukol_zmena.stav === 'deleted') pr.tasks.splice(pr.tasks.indexOf(t), 1);
            else {
              if (b.ukol_zmena.stav) t.stav = b.ukol_zmena.stav;
              if (b.ukol_zmena.text) t.text = b.ukol_zmena.text;
            }
            zmenaPrehledu();
          }
        }
        if (b.soubor) {
          const i = pr.soubory.indexOf(b.soubor);
          if (i >= 0) pr.soubory.splice(i, 1);
          pr.soubory.push(b.soubor);
          if (proCloveka(b.soubor)) doTahu(b.soubor);
          zmenaPrehledu();
        }
        // Zpráva do jiného chatu — na hřišti odletí obálka.
        if (udalost && b.name === 'SendMessage') udalost({co: 'dopis', smer: 'ven'});
        // Každý nástroj: Claudík si ho přehodí jako emoji; Playwright navíc otevře
        // okno prohlížeče (prohlizec.js to pozná podle jména).
        if (udalost && b.name) udalost({co: 'nastroj', name: b.name});
        const box = udelejNastroj(b);
        box.classList.add('ceka');
        if (b.vystupy && b.vystupy.length) { box._vystupy = b.vystupy; box._tah = tah; }
        if (b.id) tools.set(b.id, box);
        mount.appendChild(box);
        return;
      }
      if (b.kind === 'peer') {
        if (udalost) udalost({co: 'dopis', smer: 'dovnitr'});
        // Jméno odesílatele je technický název sezení — ukáže se jen pokročilým.
        const box = udelejNastroj({name: 'peer',
                                   title: b.from && pokrocile() ? 'zpráva od ' + b.from
                                     : 'zpráva z jiného rozhovoru',
                                   detail: b.text});
        box.classList.add('peer');
        mount.appendChild(box);
        return;
      }
      if (b.kind === 'agent') {
        pridejAgenta(b);
        return;
      }
      if (b.kind === 'task') {
        const k = b.id && agenti.get(b.id);
        if (k) {
          doplnAgenta(k, b);
          const stav = b.status === 'completed' ? 'hotovo'
            : b.status === 'failed' ? 'chyba'
            : /kill|stop|cancel/i.test(b.status || '') ? 'stop' : k.stav;
          k.souhrn = b.summary || '';
          dobehl(k, stav, b.result || '');
          kresliAgenta(k);
          hlidej();
          return;
        }
        // Příkaz na pozadí (Bash) — k jeho řádku se připíše, jak dopadl.
        const box = b.id && tools.get(b.id);
        if (box && !box.classList.contains('dobehl')) {
          box.classList.add('dobehl');
          box.classList.toggle('chyba', b.status === 'failed');
          box._meta.textContent = [box._meta.textContent,
            b.status === 'completed' ? 'doběhlo' : b.status === 'failed' ? 'selhalo' : b.status]
            .filter(Boolean).join(' · ');
          if (b.summary) box._detail.textContent += '\n\n' + b.summary;
        }
        return;
      }
      if (b.kind === 'res') {
        const k = b.id && agenti.get(b.id);
        if (k) {
          doplnAgenta(k, b.agent);
          if (b.agent && b.agent.async) {
            k.bg = true;                      // běží dál na pozadí
          } else if (k.stav === 'bezi') {
            dobehl(k, b.ok ? 'hotovo' : 'chyba', b.detail || '');
          }
          kresliAgenta(k);
          hlidej();
          return;
        }
        const krok = b.id && pr.kroky.find((x) => x.id === b.id);
        if (krok) { krok.stav = b.ok ? 'hotovo' : 'chyba'; zmenaPrehledu(); }
        // Výsledek patří k nástroji, u kterého se schovává. Když jeho řádek
        // není (načetl se jen konec přepisu), nekreslí se nic — samotný
        // výpis bez toho, co ho vyvolalo, neříká nic.
        const box = b.id && tools.get(b.id);
        if (!box) return;
        box.classList.remove('ceka');
        box.classList.toggle('chyba', !b.ok);
        if (b.ok && box._vystupy) { overVystupy(box, box._vystupy); box._vystupy = null; }
        if (!b.ok && udalost) udalost({co: 'chyba'});
        const kus = [];
        if (box._meta.textContent) kus.push(box._meta.textContent);
        if (!b.ok) kus.push('chyba');
        else if (b.radku) kus.push(b.radku + ' ř.');
        box._meta.textContent = kus.join(' · ');
        if (b.detail) {
          box._detail.textContent = (box._detail.textContent
            ? box._detail.textContent + '\n\n' : '') + b.detail;
        }
      }
    }

    /* Řádek „Claude pracuje": točící se hvězdička, co zrovna dělá (slovo si
       vybírá Claude Code sám), jak dlouho a kolik napsal. Stav čte bublina
       z terminálu (composer.js → io.prace); čas běží i mezi překresleními,
       ať se neposouvá po skocích. */
    let odKdy = 0, tikaniPrace = null, tikaniJisker = null;
    const DLOUHA_PRACE = 3 * 60 * 1000;
    /* Kouzelná lampa: při práci se potřásá a z hubičky vylétají písmenka
       z toho, co Claude zrovna píše (poslední jeho odstavec v konverzaci),
       nebo hvězdičky, dokud nic nenapsal. Bez pohybu, když má člověk
       vypnuté animace. */
    const LAMPA_SVG = '<svg viewBox="0 0 140 90" width="112" height="72" class="al-scena">' +
      '<defs><linearGradient id="al-zl" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#ffe08a"/>' +
      '<stop offset="1" stop-color="#e8a02c"/></linearGradient>' +
      '<linearGradient id="al-dzin" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#6cc4ff"/>' +
      '<stop offset="1" stop-color="#3d8fe8"/></linearGradient></defs>' +
      '<ellipse cx="62" cy="85" rx="52" ry="4" fill="#000" opacity=".18"/>' +
      /* kouř z hubičky do džina */
      '<path class="al-kour" d="M97 64 C104 62 94 58 104 56 C110 54 104 50 110 52" fill="none" stroke="#b6efe6" ' +
      'stroke-width="7" stroke-linecap="round" opacity=".8"/>' +
      /* džin: ocas, tělo, hlava, uzel vlasů, ruce */
      '<g class="al-dzin">' +
      '<path d="M96 56 C100 44 118 44 120 56 C121 64 108 62 106 68 C104 72 98 66 96 56Z" fill="url(#al-dzin)"/>' +
      '<ellipse cx="108" cy="44" rx="13" ry="13" fill="url(#al-dzin)"/>' +
      '<path d="M95 41 H121 V45 H95Z" fill="#ffd36e"/>' +
      '<circle cx="108" cy="25" r="10" fill="#6cc4ff"/>' +
      '<path d="M108 14 C106 8 110 4 114 6 C112 8 114 12 112 15Z" fill="#2a2a6a"/>' +
      '<circle cx="114" cy="6" r="2.4" fill="#ffd36e"/>' +
      '<path d="M98 24 C98 18 118 18 118 24Z" fill="#fff"/>' +
      '<circle cx="104" cy="26" r="1.6" fill="#162a4a" class="al-mrk"/><circle cx="112" cy="26" r="1.6" fill="#162a4a" class="al-mrk"/>' +
      '<path d="M103 30 Q108 35 113 30" fill="none" stroke="#162a4a" stroke-width="1.6" stroke-linecap="round"/>' +
      '<path d="M106 33 L108 38 L110 33Z" fill="#2a2a6a"/>' +
      '<path class="al-ruka" d="M96 44 C86 42 82 32 86 24" fill="none" stroke="#6cc4ff" stroke-width="6" stroke-linecap="round"/>' +
      '<circle cx="86" cy="23" r="3.6" fill="#6cc4ff"/>' +
      '<path d="M120 44 C128 42 130 34 126 28" fill="none" stroke="#6cc4ff" stroke-width="6" stroke-linecap="round"/>' +
      '<circle cx="126" cy="27" r="3.6" fill="#6cc4ff"/>' +
      '</g>' +
      /* Aladin: kalhoty, vesta, hlava, turban */
      '<ellipse cx="30" cy="74" rx="19" ry="9" fill="#f6eddc"/>' +
      '<path d="M18 74 C16 54 44 54 42 74Z" fill="#2bb3a3"/>' +
      '<circle cx="30" cy="40" r="10" fill="#f0b98a"/>' +
      '<path d="M19 38 C19 24 41 24 41 38 C36 33 24 33 19 38Z" fill="#fff"/>' +
      '<circle cx="30" cy="31" r="2.2" fill="#e0443c"/>' +
      '<circle cx="26.5" cy="41" r="1.4" fill="#2a1a10" class="al-mrk"/><circle cx="33.5" cy="41" r="1.4" fill="#2a1a10" class="al-mrk"/>' +
      '<path d="M26.5 45.5 Q30 48.5 33.5 45.5" fill="none" stroke="#2a1a10" stroke-width="1.4" stroke-linecap="round"/>' +
      /* lampa se potřásá, Aladin ji třese rukou */
      '<g class="al-lampa"><g transform="translate(4 0)">' +
      '<path d="M58 81 H78 L76 86 H60Z" fill="url(#al-zl)"/>' +
      '<path d="M48 74 C48 66 58 64 66 64 C76 64 84 68 84 74 C84 80 76 82 66 82 C56 82 48 80 48 74Z" fill="url(#al-zl)"/>' +
      '<ellipse cx="66" cy="64" rx="7" ry="2" fill="url(#al-zl)"/><circle cx="66" cy="60.5" r="2.6" fill="url(#al-zl)"/>' +
      '<path d="M82 70 C88 70 90 65 94 61 C93 70 90 76 81 78Z" fill="url(#al-zl)"/>' +
      '<path d="M49 70 C41 69 40 78 47 79" fill="none" stroke="url(#al-zl)" stroke-width="3.2" stroke-linecap="round"/>' +
      '<path d="M53 73 C55 69 60 68 65 68" fill="none" stroke="#fff6d6" stroke-width="2" stroke-linecap="round" opacity=".7"/>' +
      '</g></g>' +
      '<path class="al-ruka2" d="M36 56 C48 54 58 58 66 66" fill="none" stroke="#2bb3a3" stroke-width="6" stroke-linecap="round"/>' +
      '<circle class="al-ruka2" cx="67" cy="67" r="3.6" fill="#f0b98a"/>' +
      '</svg>';
    const PRACE_SLOVA = ['Plním přání', 'Čaruju', 'Chvilku, kouzlím', 'Pilně pracuju', 'Už to bude'];
    const JISKRY = ['✦', '✧', '·', '✦'];
    const klidnyPohyb = global.matchMedia && global.matchMedia('(prefers-reduced-motion: reduce)');
    function jisker() {
      const lampa = prace && prace.querySelector('.cteni-spin');
      if (!lampa || (klidnyPohyb && klidnyPohyb.matches) || document.hidden) return;
      if (lampa.querySelectorAll('.cteni-let').length >= 8) return;
      let znak = JISKRY[Math.floor(Math.random() * JISKRY.length)];
      const kusy = mount.querySelectorAll('.cteni-text');
      const posledni = kusy.length ? kusy[kusy.length - 1].textContent : '';
      if (posledni && Math.random() < 0.75) {
        const pismena = posledni.match(/[\p{L}\p{N}]/gu);
        if (pismena) znak = pismena[Math.floor(Math.random() * pismena.length)];
      }
      const let_ = el('span', 'cteni-let', znak);
      let_.style.left = (Math.random() < 0.5 ? 69 : 101) + 'px';
      let_.style.setProperty('--dx', (Math.random() * 40 - 20).toFixed(0) + 'px');
      let_.style.setProperty('--dy', (-(22 + Math.random() * 22)).toFixed(0) + 'px');
      let_.style.setProperty('--rot', (Math.random() * 60 - 30).toFixed(0) + 'deg');
      let_.addEventListener('animationend', () => let_.remove());
      lampa.appendChild(let_);
    }
    function stav(st) {
      mount.classList.toggle('pracuje', !!(st && st.on));
      if (pr.pracuje !== !!(st && st.on)) { pr.pracuje = !!(st && st.on); zmenaPrehledu(); }
      if (!st || !st.on) {
        // Claude dopracoval — zpráva z fronty teď musí dojít, tak se čeká znovu.
        for (const m of mistni) if (m.stav === 'fronta') nastavMistni(m, 'odesila');
        if (prace) { prace.remove(); prace = null; }
        clearInterval(tikaniPrace);
        tikaniPrace = null;
        clearInterval(tikaniJisker);
        tikaniJisker = null;
        odKdy = 0;
        return;
      }
      if (!prace) {
        prace = el('div', 'cteni-prace');
        const lampa = el('span', 'cteni-spin');
        lampa.setAttribute('aria-hidden', 'true');
        lampa.innerHTML = LAMPA_SVG;
        prace.append(lampa, el('span', 'cteni-slovo'),
                     el('span', 'cteni-cas'), el('span', 'cteni-tok'));
        if (!tikaniJisker) tikaniJisker = setInterval(jisker, 650);
      }
      naKonec();                            // vždycky na konci konverzace
      // Sloveso od Claude Code je anglické („Bloviating") — jen pro pokročilé.
      prace.querySelector('.cteni-slovo').textContent =
        ((pokrocile() && st.sloveso) || PRACE_SLOVA[Math.floor((Date.now() - odKdy) / 7000) % PRACE_SLOVA.length]) + '…';
      if (st.sekund !== null && st.sekund !== undefined) odKdy = Date.now() - st.sekund * 1000;
      else if (!odKdy) odKdy = Date.now();
      prace.querySelector('.cteni-tok').textContent =
        st.tokeny && pokrocile() ? '↓ ' + st.tokeny + ' tokenů' : '';
      // Po pár minutách klidná věta, že to pořád běží. Bez ní lidi po čtvrt
      // hodině mysleli, že se zaseklo, a zadali totéž do dalších tabů (Adam,
      // 21. 10. 2026 — tři taby se stejným úkolem).
      const napis = () => {
        if (!prace) return;
        const ms = Date.now() - odKdy;
        prace.querySelector('.cteni-cas').textContent = cas(ms);
        let klid = prace.querySelector('.cteni-klid');
        if (ms >= DLOUHA_PRACE && !klid) {
          klid = el('span', 'cteni-klid',
            'Pořád pracuje, nezaseklo se — větší úkol klidně trvá desítky minut. ' +
            'Znovu ho zadávat nemusíš, ozve se, až bude hotovo.');
          prace.appendChild(klid);
        } else if (ms < DLOUHA_PRACE && klid) {
          klid.remove();
        }
      };
      napis();
      if (!tikaniPrace) tikaniPrace = setInterval(napis, 1000);
    }

    /* Hláška od hubu, ne z přepisu: Claude v tabu spadl nebo skončil. */
    function upozorni(text) {
      const box = el('div', 'cteni-pozor');
      box.append(el('span', 'cteni-pozor-ico', '⚠'), el('span', '', text));
      mount.appendChild(box);
      for (const m of mistni) nastavMistni(m, 'nedoslo');
      naKonec();
    }

    obnovOdchozi();
    naKonec();
    return {
      upozorni,
      odServeru, obnovFrontu,
      add(bloky) {
        for (const b of bloky) pridej(b);
        naKonec();                          // nové bloky nad řádek práce a frontu
      },
      // Nejstarší řádky pryč, ať stránka s časem nebobtná. Jen u dna —
      // kdo čte výš, tomu text pod rukama neodjede.
      orez() {
        let navic = mount.childElementCount - MAX_RADKU;
        while (navic-- > 0 && mount.firstElementChild &&
               ![prace, pozadi, ulohy, fronta].includes(mount.firstElementChild)) {
          mount.firstElementChild.remove();
        }
      },
      prazdny() {
        return !mount.querySelector(':scope > :not(.cteni-prace):not(.cteni-pozadi):not(.cteni-ulohy):not(.cteni-fronta):not(.cteni-pozor)') &&
               !fronta.firstChild;
      },
      stav,
      odeslano,
      prubeh,
      prehled,
      // Všechny soubory z chatu ke stažení (seznam ve sdíleném chatu), nejnovější první.
      souboryVse: () => [...new Set([...pr.soubory, ...overene])].reverse(),
      stahovani: st,
      /* Vestavěný prohlížeč něco stáhl (hub/stazene.py → hub.js). */
      stazeno(info) {
        if (!info || !info.path) return;
        const box = el('div', 'cteni-ke-stazeni prohlizec');
        box.appendChild(el('div', 'cteni-ks-nadpis', 'Stažené v prohlížeči'));
        const r = el('div', 'cteni-ks-radek');
        const txt = el('span', 'cteni-ks-text');
        txt.append(el('span', 'cteni-ks-jmeno', info.name || jmenoSouboru(info.path)),
                   el('span', 'cteni-ks-cesta', info.path.replace(/[\\/][^\\/]*$/, '')
                      + (info.size ? ' · ' + velikost(info.size) : '')));
        txt.title = info.path;
        r.append(el('span', 'cteni-ks-ico', '⤓'), txt, tlacitkoStahnout(info.path, st, notice, 'hlavni', 'Stáhnout'));
        box.appendChild(r);
        mount.appendChild(box);
        naKonec();
      },
      ulohy: ulohyZe,
      maUlohy: () => ulohyData.length > 0,
      agentu: () => agenti.size,
      bezi: () => bezici().length,
      zavri() { clearInterval(tikani); clearInterval(tikaniPrace); for (const m of mistni) clearTimeout(m.timer); },
    };
  }

  /* Připojení na server: od kterého bajtu dál a co z toho přišlo. */
  function zdroj(io, dotaz) {
    let odkud = 0;
    let hotovo = false;
    let src = '';          // který přepis se čte — server podle něj pozná přestěhování
    return {
      async dalsi() {
        const url = 'cteni?' + (typeof dotaz === 'function' ? dotaz() : dotaz) +
                    '&from=' + odkud + (src ? '&src=' + encodeURIComponent(src) : '');
        const res = await io.api(url);
        if (!res || !res.ready) return {blocks: [], konec: true};
        if (res.src) src = res.src;
        const posun = res.next !== odkud;
        odkud = res.next;
        hotovo = !posun;
        return {blocks: res.blocks || [], konec: !posun};
      },
      get odkud() { return odkud; },
      get hotovo() { return hotovo; },
    };
  }

  /* ── vrstva v tabu ──────────────────────────────────────────────────────── */
  function install(tab, io) {
    const root = el('div', 'cteni');
    const scroll = el('div', 'cteni-scroll');
    const mount = el('div', 'cteni-flow');
    const prazdno = el('div', 'cteni-empty', 'Zatím nic — napiš Claudovi dole.');
    // Přepínač na terminál tu není: čtení je výchozí a terminál se ukáže sám,
    // když se Claude na něco zeptá (karta dotazu má i vlastní „Terminál").
    scroll.append(mount, prazdno);
    // Šipka dolů, když je člověk odrolovaný nahoru — jako v appce Claude.
    const dolu = el('button', 'dolu');
    dolu.title = 'Dolů na konec';
    dolu.innerHTML = '<svg class="ico"><use href="#i-up"/></svg>';
    dolu.hidden = true;
    dolu.onclick = () => scroll.scrollTo({top: scroll.scrollHeight, behavior: 'smooth'});
    root.append(scroll, dolu);
    tab.pane.appendChild(root);
    tab.pane.classList.add('cteni-on');
    // Stavový řádek (model, větev, cena, čas) — stavradek.js; bez něj se jede dál.
    let stavr = null;
    try { stavr = global.HubStav ? global.HubStav.install(root, tab, io) : null; } catch (_) { stavr = null; }
    // Claudíci nad polem na psaní (claudici.js) — bez souboru se jede dál.
    const hriste = global.HubClaudici ? global.HubClaudici.hriste(root, claudik) : null;

    /* Předčítá se jen to, co přibude naživo — ne historie při otevření tabu. */
    let nacteno = false;
    const proud = flow(mount, io.imageUrl, {
      openLink: io.openLink, notice: io.notice, copy: io.copy, odpovez: io.odpovez,
      sdileny: io.sdileny || null,
      znovu: io.znovu || null, jeOnline: io.jeOnline || null, serverZnovu: io.serverZnovu || null, serverIhned: io.serverIhned || null,
      serverSeznam: io.serverSeznam || null, serverUprav: io.serverUprav || null, serverSmaz: io.serverSmaz || null,
      pozastaveno: io.pozastaveno || null, pozastavit: io.pozastavit || null,
      odchozi: io.odchozi || null,
      rozvetvit: io.rozvetvit ? (uuid) => io.rozvetvit({id: tab.id}, uuid) : null,
      zive: () => nacteno && !!global.HubHlas && global.HubHlas.auto.get() &&
                  (!io.aktivni || io.aktivni()),
      naPrehled: () => { if (io.prehled) io.prehled(); },
      dulKlic: () => tab.id || '',
      /* Historie při otevření tabu se na hřišti nepřehrává: kdo tehdy ještě
         běžel, prostě tam stojí, a co doběhlo, se ani neukáže. */
      udalost: (e) => {
        // Prohlížeč v appce — jen živé nástroje, ne historie při otevření tabu.
        if (e.co === 'nastroj' && nacteno) {
          document.dispatchEvent(new CustomEvent('hub-tool', {detail: {name: e.name, tab: tab.id || ''}}));
          if (hriste) hriste.nastroj(e.name);
        }
        if (e.co === 'chyba' && nacteno && hriste) hriste.chyba();
        if (!hriste) return;
        if (e.co === 'agent') hriste.pridej(e.id, e.typ, e.barva, !nacteno, e.ukol);
        else if (e.co === 'krok' && nacteno) hriste.krok(e.id, e.text);
        else if (e.co === 'konec') hriste.hotovo(e.id, e.stav, !nacteno);
        else if (e.co === 'dopis' && nacteno) hriste.dopis(e.smer);
      },
    });
    const data = zdroj(io, () => 'id=' + encodeURIComponent(tab.id || ''));
    let timer = null, prazdnych = 0, zivy = true, ceka = false;

    /* Agenti si píšou vlastní přepisy — dokud některý běží, doptává se
       čtení i na ně (co zrovna dělá, kolik nástrojů, kolik tokenů). */
    let agTimer = null, agCeka = false;
    function hlidejAgenty(hned) {
      if (!zivy || agCeka || (!hned && agTimer)) return;
      if (!proud.bezi() && !hned) return;
      clearTimeout(agTimer);
      agTimer = setTimeout(async () => {
        agTimer = null;
        if (!zivy || !tab.id) return;
        agCeka = true;
        try {
          proud.prubeh(await io.api('cteni-agenti?id=' + encodeURIComponent(tab.id)));
        } catch (_) { /* příště */ }
        agCeka = false;
        if (proud.bezi()) hlidejAgenty();
      }, hned ? 50 : (!io.aktivni || io.aktivni()) ? 2000 : 6000);
    }

    /* Úlohy na pozadí: jednou za pár vteřin, i když se v přepisu nic neděje —
       příkaz může běžet dlouho potichu. Neaktivní tab se ptá zřídka. */
    let bgTimer = null;
    function hlidejUlohy() {
      clearTimeout(bgTimer);
      if (!zivy) return;
      bgTimer = setTimeout(async () => {
        if (!zivy) return;
        if (tab.id) {
          try {
            proud.ulohy(await io.api('cteni-pozadi?id=' + encodeURIComponent(tab.id)));
          } catch (_) { /* příště */ }
        }
        hlidejUlohy();
      }, (!io.aktivni || io.aktivni()) ? 4000 : 15000);
    }
    hlidejUlohy();

    // Roluje se samo, dokud je člověk u dna. Jak si odroluje nahoru číst,
    // nic mu pod rukama neuteče.
    let uDna = true;
    const hlidejDno = () => {
      const zbyva = scroll.scrollHeight - scroll.scrollTop - scroll.clientHeight;
      uDna = zbyva < U_DNA;
      dolu.hidden = zbyva < scroll.clientHeight / 2;
    };
    scroll.addEventListener('scroll', hlidejDno, {passive: true});

    async function tik() {
      if (ceka || !zivy) return;
      ceka = true;
      try {
        const {blocks, konec} = await data.dalsi();
        if (!zivy) return;
        if (blocks.length) {
          const agentu = proud.agentu();
          proud.add(blocks);
          prazdno.hidden = true;
          prazdnych = 0;
          if (stavr) stavr.zmena();
          if (uDna) { proud.orez(); scroll.scrollTop = scroll.scrollHeight; }
          else hlidejDno();          // přibylo pod rukama — šipka se ukáže
          // Nový agent: hned zjistit, co dělá, ne až za dvě vteřiny.
          if (proud.agentu() !== agentu) hlidejAgenty(true);
          else hlidejAgenty();
        } else if (konec) {
          prazdnych++;
          prazdno.hidden = !proud.prazdny();
        }
      } catch (_) {
        prazdnych++;                 // server chvilku nemohl — zkusí se dál
      } finally {
        ceka = false;
        if (data.hotovo) nacteno = true;
        if (zivy) {
          clearTimeout(timer);
          // Tab, na který není vidět, se doptává zvolna — přibýt v něm může
          // hodně, ale nikdo to zrovna nečte.
          const rychle = !document.hidden && prazdnych < KLID_PO && (!io.aktivni || io.aktivni());
          timer = setTimeout(tik, rychle ? POLL : POLL_KLID);
        }
      }
    }

    // Tab bez id ještě nemá session na serveru — počká se, až přijde.
    function start() {
      if (!zivy) return;
      if (!tab.id) { setTimeout(start, 400); return; }   // session ještě nevznikla
      tik();
    }
    start();

    return {
      hriste,
      prehled: () => proud.prehled(),
      // Vestavěný prohlížeč stáhl soubor — karta do chatu, na který se kouká.
      stazeno: (info) => { proud.stazeno(info); if (uDna) scroll.scrollTop = scroll.scrollHeight; },
      // Je v konverzaci už něco? (automatika modelu podle toho pozná navázání)
      prazdny: () => proud.prazdny(),
      release() {
        zivy = false;
        clearTimeout(timer);
        clearTimeout(agTimer);
        clearTimeout(bgTimer);
        if (stavr) { try { stavr.release(); } catch (_) { /* nic */ } }
        proud.stav(null);
        proud.zavri();
        if (hriste) hriste.release();
        root.remove();
        tab.pane.classList.remove('cteni-on');
      },
      // Fronta zpráv na serveru (hub/fronta.py): stav zprávy a obnova po reloadu.
      odServeru(msg) { if (zivy) proud.odServeru(msg); },
      obnovFrontu(zpravy) {
        if (!zivy) return;
        proud.obnovFrontu(zpravy);
        prazdno.hidden = true;
      },
      // Po přepnutí na tab se doptá hned, ať čtení není o vteřinu pozadu.
      wake() {
        if (zivy) { clearTimeout(timer); timer = setTimeout(tik, 60); }
        if (zivy && stavr) stavr.wake();
      },
      /* Zpráva odešla z bubliny (composer.js). Ve čtení je vidět hned, ještě
         než ji Claude Code zapíše — a pod ní, jestli už ji Claude vidí.
         `z` = null: zprávy čekající na start Claude Code právě odešly. */
      odeslano(z, stav) {
        if (!zivy) return;
        proud.odeslano(z, stav);
        prazdno.hidden = true;
        prazdnych = 0;
        scroll.scrollTop = scroll.scrollHeight;
        uDna = true;
        clearTimeout(timer);
        timer = setTimeout(tik, 60);
      },
      upozorni(text) {
        if (!zivy) return;
        proud.upozorni(text);
        prazdno.hidden = true;
        scroll.scrollTop = scroll.scrollHeight;
      },
      /* Claude začal nebo přestal pracovat (composer.js čte terminál). Když
         pracuje, doptává se čtení rychle, ať text přibývá, jak ho píše. */
      prace(st) {
        if (!zivy) return;
        proud.stav(st);
        if (st && st.on) {
          prazdno.hidden = true;
          prazdnych = 0;
        }
        if (uDna) scroll.scrollTop = scroll.scrollHeight;
      },
    };
  }

  /* ── okno se starou konverzací ──────────────────────────────────────────── */
  function open(io, chat) {
    const box = el('div', 'onb set-modal cteni-modal');
    box.innerHTML = `
      <div class="onb-box">
        <div class="onb-head">
          <span class="onb-mark"><svg class="ico" width="26" height="26"><use href="#i-note"/></svg></span>
          <div>
            <div class="onb-title"></div>
            <div class="onb-sub"></div>
          </div>
          <span class="spacer"></span>
          <button class="btn primary cteni-go">Pokračovat</button>
          <button class="set-x cteni-close" title="Zavřít (Esc)">×</button>
        </div>
        <div class="onb-body">
          <div class="cteni-scroll"><div class="cteni-flow"></div></div>
          <!-- Číst a nemoct odepsat je půlka věci: co se tu napíše, dostane
               Claude jako první zprávu, jakmile se konverzace otevře. -->
          <div class="cteni-write">
            <textarea class="cteni-input" rows="1" spellcheck="false"
                  autocomplete="off" autocorrect="on" autocapitalize="sentences" aria-autocomplete="none" data-form-type="other" data-1p-ignore data-lpignore="true"
                      placeholder="Napiš a Claude v konverzaci pokračuje… (Enter odešle)"></textarea>
            <button class="cteni-send" title="Odeslat (Enter)">↑</button>
          </div>
        </div>
      </div>`;
    document.body.appendChild(box);
    const q = (sel) => box.querySelector(sel);
    q('.onb-title').textContent = chat.title || 'Konverzace';
    q('.onb-sub').textContent = [chat.project, chat.when].filter(Boolean).join('  ·  ');

    let zivy = true;
    const zavrit = () => {
      zivy = false;
      if (proud) proud.zavri();
      box.remove();
      document.removeEventListener('keydown', naKlavesu, true);
    };
    function naKlavesu(ev) { if (ev.key === 'Escape') { ev.stopPropagation(); zavrit(); } }
    document.addEventListener('keydown', naKlavesu, true);
    q('.cteni-close').onclick = zavrit;
    box.addEventListener('click', (ev) => { if (ev.target === box) zavrit(); });
    q('.cteni-go').onclick = () => { zavrit(); io.resume(chat); };

    /* Napsat a pokračovat je jedno gesto: okno se zavře, konverzace se otevře
       v tabu a Claude na napsané začne dělat hned. */
    const pole = q('.cteni-input');
    // Na telefonu se dlouhá výzva zalomí do dvou řádků a spodek se ořízne.
    if (window.matchMedia('(max-width: 520px)').matches) {
      pole.placeholder = 'Napiš zprávu…';
    }
    const odeslat = () => {
      const text = pole.value.trim();
      if (!text) return pole.focus();
      zavrit();
      io.resume(chat, text);
    };
    q('.cteni-send').onclick = odeslat;
    pole.addEventListener('keydown', (ev) => {
      if (ev.key === 'Enter' && !ev.shiftKey && !ev.isComposing && !(global.HubIME && !global.HubIME.enterOdesle(ev))) {
        ev.preventDefault();
        odeslat();
      }
    });
    // Pole roste s textem, ale jen do výšky, po které je ještě vidět konverzace.
    pole.addEventListener('input', () => {
      pole.style.height = 'auto';
      pole.style.height = Math.min(pole.scrollHeight, 160) + 'px';
    });
    setTimeout(() => pole.focus(), 0);

    const scroll = q('.cteni-scroll');
    const proud = flow(q('.cteni-flow'), io.imageUrl, {historie: true, openLink: io.openLink,
                                                        notice: io.notice, copy: io.copy,
                                                        rozvetvit: io.rozvetvit ? (uuid) => io.rozvetvit({chat: chat.id}, uuid, zavrit) : null});
    const data = zdroj(io, 'chat=' + encodeURIComponent(chat.id));

    (async () => {
      // Načte se celý zbytek přepisu, ne jen první kus — stará konverzace se
      // nedočítá sama, tak ať je po otevření celá.
      for (let i = 0; i < 40 && zivy; i++) {
        let res;
        try {
          res = await data.dalsi();
        } catch (err) {
          console.warn('cteni:', err);
          io.notice('Konverzaci se teď nepodařilo načíst, zkus to za chvíli znovu.');
          return;
        }
        if (!zivy) return;
        proud.add(res.blocks);
        if (res.konec) break;
      }
      if (zivy) scroll.scrollTop = scroll.scrollHeight;
      // Karty agentů doplní, kolik toho udělali (z jejich vlastních přepisů).
      if (zivy && proud.agentu()) {
        try {
          proud.prubeh(await io.api('cteni-agenti?chat=' + encodeURIComponent(chat.id)));
        } catch (_) { /* karty zůstanou bez čísel */ }
      }
    })();
    return {close: zavrit};
  }

  /* ── sdílený chat ─────────────────────────────────────────────────────────
     Okno s chatem, který kolega sdílí: čte se živě přes bránu (io.gw →
     /gw/relace/cteni), je vidět, že je sdílený, že Claude pracuje, kdo se
     dívá, a kód, který Claude napsal (Edit/Write se rozbalí sám). Kdo smí,
     píše — zpráva dojde do chatu majitele s tvým jménem. */
  function openSdilene(io, share) {
    /* io.host = vložený do tabu (hub.js sdileneTab), jinak okno nad stránkou. */
    const host = io.host || null;
    const box = host || el('div', 'onb set-modal cteni-modal relace-modal');
    const telo = `
          <div class="cteni-scroll"><div class="cteni-flow"></div></div>
          <div class="relace-pisou" hidden></div>
          <div class="cteni-write">
            <textarea class="cteni-input" rows="1" spellcheck="false" autocomplete="off"
                      data-form-type="other" data-1p-ignore data-lpignore="true"></textarea>
            <button class="cteni-attach" title="Přiložit soubor nebo obrázek" type="button"><svg class="ico"><use href="#i-image"/></svg></button>
            <button class="cteni-mic composer-mic" title="Diktovat česky — klikni, mluv, klikni znovu" type="button" hidden><svg class="ico"><use href="#i-mic"/></svg></button>
            <button class="cteni-send" title="Odeslat (Enter)">↑</button>
            <input type="file" class="cteni-picker" multiple hidden>
          </div>
          <div class="cteni-prilohy" hidden></div>`;
    /* Jeden řádek: čí chat a co smíš · stav (jednou, s tečkou) · kdo je tu · akce. */
    const lista = `
        <div class="relace-lista">
          <span class="relace-stav"></span>
          <span class="spacer"></span>
          <div class="relace-lidi"></div>
          <div class="relace-akce"></div>
        </div>`;
    box.innerHTML = host ? `
      <div class="relace-hlava">
        <span class="relace-avatar"></span>
        <span class="relace-kdo"></span>
        <span class="relace-pravo"></span>
        ${lista}
      </div>
      <div class="relace-telo">${telo}</div>` : `
      <div class="onb-box">
        <div class="onb-head">
          <span class="onb-mark"><svg class="ico" width="26" height="26"><use href="#i-paluba"/></svg></span>
          <div>
            <div class="onb-title"></div>
            <div class="onb-sub"></div>
          </div>
          <span class="spacer"></span>
          <span class="relace-pravo"></span>
          <button class="set-x cteni-close" title="Zavřít (Esc)">×</button>
        </div>
        ${lista}
        <div class="onb-body">${telo}</div>
      </div>`;
    if (!host) document.body.appendChild(box);
    const q = (sel) => box.querySelector(sel);
    if (host) {
      const av = q('.relace-avatar');
      av.textContent = inicialy(share.majitel);
      av.style.setProperty('--kdo', kdoBarva(share.majitel));
      q('.relace-kdo').textContent = 'Sdílí ' + share.majitel;
      q('.relace-kdo').title = 'Chat běží u ' + share.majitel + ', ty ho sleduješ.';
    } else {
      q('.onb-title').textContent = share.titulek || 'Sdílený chat';
      q('.onb-sub').textContent = share.majitel + ' ti ho sdílí';
    }
    const scroll = q('.cteni-scroll');
    const pole = q('.cteni-input');
    const stav = q('.relace-stav');
    const pravo = q('.relace-pravo');
    const lidi = q('.relace-lidi');
    const akce = q('.relace-akce');
    const pisou = q('.relace-pisou');
    let zivy = true, uDna = true, odkud = 0, psat = share.role === 'pise', prazdnych = 0;

    const sdilenoInfo = {ja: share.ja || '', majitel: share.majitel};
    /* Soubory ze sdíleného chatu jdou přes bránu — ta ověří, že chat smím
       číst, a hub majitele pustí jen to, co Claude v tomhle chatu zapsal. */
    const souborGw = (p, info) => '/gw/relace/soubor?id=' + encodeURIComponent(share.id) +
      '&path=' + encodeURIComponent(p) + (info ? '&info=1' : '');
    const stahovani = {
      url: (p) => souborGw(p),
      info: async (p) => {
        const r = await fetch(souborGw(p, true), {credentials: 'same-origin'});
        return r.json().catch(() => ({ok: false}));
      },
    };
    // Obrázek z prostoru majitele (hub-images) se v mém hubu otevřít nedá — bez náhledu
    // je to lepší než rozbitý obrázek; kdo chat vede, ho vidí.
    const obrazek = (p) => (/[\\/]hub-images[\\/]/.test(p) ? '' : io.imageUrl(p));
    const proud = flow(q('.cteni-flow'), obrazek, {openLink: io.openLink, notice: io.notice, copy: io.copy,
                                                         sdileny: sdilenoInfo, stahovani});
    scroll.addEventListener('scroll', () => {
      uDna = scroll.scrollHeight - scroll.scrollTop - scroll.clientHeight < U_DNA;
    }, {passive: true});

    const zavrit = () => {
      zivy = false;
      clearInterval(prit);
      clearTimeout(timer);
      proud.stav(null);
      proud.zavri();
      if (host) host.textContent = '';
      else box.remove();
      document.removeEventListener('keydown', naKlavesu, true);
      if (io.onClose) io.onClose();
    };
    function naKlavesu(ev) { if (ev.key === 'Escape' && document.activeElement !== pole) { ev.stopPropagation(); zavrit(); } }
    if (!host) {
      document.addEventListener('keydown', naKlavesu, true);
      q('.cteni-close').onclick = zavrit;
      box.addEventListener('click', (ev) => { if (ev.target === box) zavrit(); });
    }

    let offline = false;
    function psaniPole() {
      q('.cteni-write').hidden = !psat;
      pole.disabled = offline;
      q('.cteni-send').disabled = offline;
      q('.cteni-attach').disabled = offline;
      pole.placeholder = offline
        ? `Psát půjde, až ${share.majitel} zapne prostor`
        : psat ? `Napiš do chatu (uvidí to ${share.majitel} i Claude)…` : '';
    }
    psaniPole();
    /* Přílohy a hlas: soubor se přes bránu uloží do prostoru majitele (tam ho
       Claude přečte) a k zprávě se připojí jeho cesta, tak jako u vlastního
       chatu. Diktování běží v mém hubu a do pole napíše text. */
    const prilohy = [];                       // {name, path}; path je prázdná, dokud se nahrává
    const boxPrilohy = q('.cteni-prilohy');
    const picker = q('.cteni-picker');
    const base64Soubor = (file) => new Promise((ok, fail) => {
      const r = new FileReader();
      r.onload = () => ok(String(r.result).split(',')[1] || '');
      r.onerror = () => fail(new Error('Soubor se nepodařilo přečíst.'));
      r.readAsDataURL(file);
    });
    function kresliPrilohy() {
      boxPrilohy.textContent = '';
      boxPrilohy.hidden = !prilohy.length;
      for (const a of prilohy) {
        const chip = el('span', 'cteni-priloha' + (a.path ? '' : ' nahrava'), a.name);
        const x = el('button', 'cteni-priloha-x', '×');
        x.type = 'button';
        x.title = 'Odebrat přílohu';
        x.onclick = () => { prilohy.splice(prilohy.indexOf(a), 1); kresliPrilohy(); pole.focus(); };
        chip.appendChild(x);
        boxPrilohy.appendChild(chip);
      }
    }
    async function pridejSoubory(files) {
      if (!psat || offline) return;
      for (const file of files) {
        if (file.size > 25 * 1024 * 1024) { io.notice(`„${file.name}“ je větší než 25 MB.`); continue; }
        const a = {name: file.name || 'obrazek.png', path: ''};
        prilohy.push(a);
        kresliPrilohy();
        try {
          const r = await io.gw('/priloha', {id: share.id, name: a.name, data: await base64Soubor(file)});
          if (r && r.offline) throw new Error(r.zprava || 'Majitel má prostor vypnutý.');
          a.path = r.path;
        } catch (err) {
          prilohy.splice(prilohy.indexOf(a), 1);
          io.notice(err.message);
        }
        kresliPrilohy();
      }
      pole.focus();
    }
    q('.cteni-attach').onclick = () => picker.click();
    picker.onchange = () => { if (picker.files && picker.files.length) pridejSoubory([...picker.files]); picker.value = ''; };
    pole.addEventListener('paste', (ev) => {
      const files = [...((ev.clipboardData && ev.clipboardData.files) || [])];
      if (files.length) { ev.preventDefault(); pridejSoubory(files); }
    });
    const zona = q('.relace-telo') || box;
    zona.addEventListener('dragover', (ev) => {
      if (psat && ev.dataTransfer && [...ev.dataTransfer.types].includes('Files')) ev.preventDefault();
    });
    zona.addEventListener('drop', (ev) => {
      const files = ev.dataTransfer && [...ev.dataTransfer.files];
      if (psat && files && files.length) { ev.preventDefault(); pridejSoubory(files); }
    });
    if (global.HubHlas) global.HubHlas.mic(q('.cteni-mic'), pole, {notice: io.notice});

    async function odeslat() {
      if (!psat) return;
      if (prilohy.some((a) => !a.path)) return io.notice('Počkej, příloha se ještě nahrává.');
      const puvodni = pole.value.trim();
      const cesty = prilohy.map((a) => a.path);
      const text = cesty.length ? (puvodni ? puvodni + ' ' : 'Příloha: ') + cesty.join(' ') : puvodni;
      if (!text) return;
      const zaloha = prilohy.slice();
      pole.value = '';
      pole.style.height = 'auto';
      prilohy.length = 0;
      kresliPrilohy();
      const vrat = () => { pole.value = puvodni; prilohy.push(...zaloha); kresliPrilohy(); };
      try {
        const r = await io.gw('/poslat', {id: share.id, text});
        if (r && r.offline) {
          vrat();
          io.notice(r.zprava || 'Majitel má prostor vypnutý — zpráva by nikam nedošla.');
          return;
        }
        nastavStav('Zpráva odeslána', 'ceka');
      } catch (err) {
        vrat();
        io.notice(err.message);
      }
      tik(true);
    }
    q('.cteni-send').onclick = odeslat;
    pole.addEventListener('keydown', (ev) => {
      if (ev.key === 'Enter' && !ev.shiftKey && !ev.isComposing && !(global.HubIME && !global.HubIME.enterOdesle(ev))) { ev.preventDefault(); odeslat(); }
    });
    let pisuOd = 0;
    pole.addEventListener('input', () => {
      pisuOd = Date.now();
      pole.style.height = 'auto';
      pole.style.height = Math.min(pole.scrollHeight, 160) + 'px';
    });

    /* Stav jednou a s barevnou tečkou: pracuje / čeká / zavřený chat / vypnutý prostor / chyba. */
    function nastavStav(text, druh) {
      stav.textContent = text;
      stav.className = 'relace-stav' + (druh ? ' ' + druh : '');
    }

    /* Akce se staví jednou a jen se schovávají — překreslování pod kurzorem by rušilo klik. */
    const tlZapnout = el('button', 'relace-btn hlavni');
    tlZapnout.hidden = true;
    tlZapnout.onclick = async () => {
      const puvodni = tlZapnout.textContent;
      tlZapnout.disabled = true;
      tlZapnout.textContent = 'Zapínám…';
      nastavStav('zapínám…', 'pracuje');
      try { await io.zapnout(share); tik(true); }
      catch (err) { io.notice(err.message); }
      tlZapnout.disabled = false;
      tlZapnout.textContent = puvodni;
    };
    const tlPokracovat = el('button', 'relace-btn', 'Pokračovat u sebe');
    tlPokracovat.hidden = !io.pokracovat;
    tlPokracovat.title = 'Zkopíruje chat do tvého prostoru a otevře ho jako tvůj vlastní — Claude v něm naváže, ' +
      'kde skončil, a ty v něm můžeš psát dál. Chat ' + share.majitel + ' se nijak nezmění.';
    tlPokracovat.onclick = async () => {
      tlPokracovat.disabled = true;
      tlPokracovat.textContent = 'Kopíruju…';
      try { await io.pokracovat(share); }
      catch (err) { io.notice(err.message); }
      tlPokracovat.disabled = false;
      tlPokracovat.textContent = 'Pokračovat u sebe';
    };
    /* Upravené soubory: klepnutím seznam, u každého „Stáhnout". Seznam se
       staví až při otevření — tlačítko pod kurzorem se nepřekresluje. */
    const tlSoubory = el('button', 'relace-soubory');
    tlSoubory.type = 'button';
    tlSoubory.hidden = true;
    const seznamSouboru = el('div', 'relace-soubory-seznam');
    seznamSouboru.hidden = true;
    function kresliSeznamSouboru() {
      seznamSouboru.textContent = '';
      for (const cesta of proud.souboryVse()) {
        const r = el('div', 'relace-soubor');
        const txt = el('span', 'relace-soubor-jmeno', jmenoSouboru(cesta));
        txt.title = cesta;
        r.append(txt, tlacitkoStahnout(cesta, stahovani, io.notice, 'tichy', '⤓ Stáhnout'));
        seznamSouboru.appendChild(r);
      }
    }
    tlSoubory.onclick = (ev) => {
      ev.stopPropagation();
      seznamSouboru.hidden = !seznamSouboru.hidden;
      if (!seznamSouboru.hidden) kresliSeznamSouboru();
    };
    akce.append(tlSoubory, tlPokracovat, tlZapnout);
    akce.parentNode.insertBefore(seznamSouboru, akce.nextSibling);

    // Kdo je v chatu (kromě mě) a kdo zrovna píše; kreslí se jen při změně.
    let lidiKlic = '';
    function prihomni(diva, pisi) {
      const ja = sdilenoInfo.ja;
      diva = (diva || []).filter((j) => j && j !== ja);
      pisi = (pisi || []).filter((j) => j && j !== ja);
      const klic = JSON.stringify([diva, pisi]);
      if (klic === lidiKlic) return;
      lidiKlic = klic;
      lidi.textContent = '';
      lidi.hidden = !diva.length;
      for (const jmeno of diva.slice(0, 5)) {
        const a = el('span', 'relace-avatar mini' + (pisi.includes(jmeno) ? ' pise' : ''), inicialy(jmeno));
        a.style.setProperty('--kdo', kdoBarva(jmeno));
        a.title = jmeno + (pisi.includes(jmeno) ? ' — píše' : ' — je tu');
        lidi.appendChild(a);
      }
      if (diva.length > 5) lidi.appendChild(el('span', 'relace-avatar mini vic', '+' + (diva.length - 5)));
      lidi.title = 'Teď se dívá: ' + diva.join(', ');
      pisou.hidden = !pisi.length;
      pisou.textContent = pisi.length ? (pisi.length === 1 ? pisi[0] + ' píše' : pisi.join(', ') + ' píšou') : '';
    }

    function hlavicka(res) {
      const r = res.relace;
      if (!r) return;
      psat = !!r.psat;
      offline = !!r.majitel_offline;
      psaniPole();
      pravo.textContent = psat ? 'smíš psát' : 'jen čtení';
      pravo.className = 'relace-pravo' + (psat ? ' pise' : '');
      prihomni(r.diva, r.pisou);
      const zapnout = !!io.zapnout && (res.bezi === false || offline);
      tlZapnout.hidden = !zapnout;
      if (zapnout && !tlZapnout.disabled) {
        tlZapnout.textContent = offline ? 'Zapnout prostor' : 'Zapnout chat';
        tlZapnout.title = 'Spustí ' + share.majitel + ' prostor (když spí) a otevře v něm tenhle chat — Claude naváže, ' +
          'kde skončil. Psát do něj smí jen ten, kdo na to má právo.';
      }
      const soubory = proud.souboryVse(), n = soubory.length;
      tlSoubory.hidden = !n;
      const popis = '✎ ' + n + (n === 1 ? ' soubor' : n < 5 ? ' soubory' : ' souborů');
      if (tlSoubory.textContent !== popis) tlSoubory.textContent = popis;
      tlSoubory.title = 'Upravené soubory (klepni — stáhnout):\n' + soubory.join('\n');
    }

    // Kód, který Claude napsal (Edit, Write), se rozbalí sám — kvůli tomu sdílíš.
    function rozbalKod() {
      for (const t of q('.cteni-flow').querySelectorAll('.cteni-tool:not([data-auto])')) {
        t.dataset.auto = '1';
        const ico = t.querySelector('.cteni-ico');
        if (ico && ico.dataset.druh === 'tuzka' && t.querySelector('.cteni-detail') &&
            t.querySelector('.cteni-detail').textContent) {
          t.querySelector('.cteni-head').click();
        }
      }
    }

    // Přítomnost: i když jen čtu, jednou za 3 s se hlásím a dostanu, kdo je tu a kdo píše.
    const prit = setInterval(async () => {
      if (!zivy || document.hidden || (host && !host.classList.contains('active'))) return;
      try {
        const r = await io.gw('/pritomnost', {id: share.id, pise: psat && !offline && Date.now() - pisuOd < 4000});
        if (zivy) prihomni(r.diva, r.pisou);
      } catch (_) { /* příští pokus */ }
    }, 3000);

    let timer = null, ceka = false;
    async function tik(hned) {
      if (!zivy || ceka) return;
      clearTimeout(timer);
      // Tab, na který se nekouká, nečte — jen občas zkontroluje, jestli se nemá probudit.
      if (host && !hned && !host.classList.contains('active')) { timer = setTimeout(tik, 3000); return; }
      ceka = true;
      let dalsi = prazdnych < 6 && !document.hidden ? 1200 : 4000;
      try {
        const res = await io.gw('/cteni?id=' + encodeURIComponent(share.id) + '&from=' + odkud);
        if (!zivy) return;
        if (res.offline) {
          nastavStav(share.majitel + ' je offline', 'vypnuto');
          proud.stav(null);
        } else {
          // Jméno, podle kterého se pozná, které bubliny jsou moje — dřív než se kreslí.
          if (res.relace && res.relace.ja) sdilenoInfo.ja = res.relace.ja;
          odkud = res.next !== undefined ? res.next : odkud;
          if (res.blocks && res.blocks.length) {
            proud.add(res.blocks);
            rozbalKod();
            prazdnych = 0;
            if (uDna) scroll.scrollTop = scroll.scrollHeight;
          } else prazdnych++;
          proud.stav(res.pracuje ? {on: true, sloveso: '', sekund: null, tokeny: ''} : null);
          if (res.relace && res.relace.majitel_offline) nastavStav('Prostor je vypnutý — čteš uložený chat', 'vypnuto');
          else if (res.pracuje) nastavStav('Claude pracuje…', 'pracuje');
          else if (res.bezi === false) nastavStav('Chat je zavřený — nikdo v něm teď nepracuje', 'zavreno');
          else nastavStav('Čeká na zprávu', 'ceka');
          hlavicka(res);
          if (res.pracuje) dalsi = 900;
        }
      } catch (err) {
        nastavStav(err.message, 'chyba');
        dalsi = 5000;
      } finally {
        ceka = false;
        if (zivy) timer = setTimeout(tik, dalsi);
      }
    }
    tik();
    return {close: zavrit, wake: () => tik(true)};
  }

  global.HubCteni = {install, open, openSdilene, claudik, paleta};

})(typeof window !== 'undefined' ? window : globalThis);
