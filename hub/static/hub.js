/* Claude Code Hub — browser front-end.
 *
 * Tabs are thin: the pty and its scrollback live in the Python server, so a
 * reload re-attaches to the running Claude sessions instead of killing them.
 */
'use strict';

const TOKEN = new URLSearchParams(location.search).get('t') || '';
const $ = (id) => document.getElementById(id);

let STATE = null;          // /api/state payload
let WS = null;
let TABS = [];             // {ref,id,title,kind,path,term,fit,pane,el,exited}
let ACTIVE = null;
// Po otevření appky zůstat na Domů (restore nepřepíná do posledního chatu).
let START_HOME = true;

/* Kde člověk skončil: aktivní tab a otevřená poznámka v Obsidianu. Po dalším
   otevření appky (počítač i prostor na serveru) se na ně vrátí. Taby samotné
   drží server (a po zavření appky/uspání prostoru hub/restart.py). Ukládá se
   jen v tomhle prohlížeči — nic citlivého, cesty a názvy. */
const PLACE_KEY = 'hub.misto';
function readPlace() {
  try {
    const v = JSON.parse(localStorage.getItem(PLACE_KEY) || '{}');
    return v && typeof v === 'object' ? v : {};
  } catch (_) { return {}; }
}
function writePlace(patch) {
  try { localStorage.setItem(PLACE_KEY, JSON.stringify({...readPlace(), ...patch})); } catch (_) { /* soukromé okno */ }
}
const LAST_PLACE = readPlace();
let placeReady = false;           // do první obnovy se místo nepřepisuje
const tabKey = (t) => ({kind: t.kind || '', path: t.path || '', title: t.title || '', vault: t.vault || '',
                        chat: t.chat || t.resume || ''});
function findPlaceTab(key) {
  if (!key) return null;
  /* Nejdřív podle konverzace: název chatu se po restartu může změnit (Claude
     ho přejmenuje) a ve stejné složce bývá chatů víc — podle složky by se
     otevřel jiný. */
  const chat = key.chat || '';
  if (chat) {
    const t = TABS.find((x) => (x.chat || x.resume || '') === chat);
    if (t) return t;
  }
  const same = (t, strict) => (t.kind || '') === key.kind && (t.path || '') === key.path &&
    (t.vault || '') === (key.vault || '') && (!strict || (t.title || '') === key.title);
  return TABS.find((t) => same(t, true)) || TABS.find((t) => same(t, false)) || null;
}
let DARK = true;
let refSeq = 0;

/* Jednoduchý režim (výchozí) × Pro pokročilé (config.dev_mode). V jednoduchém
   se schovává všechno, co potřebuje technické znalosti: terminál, modely,
   / příkazy, tokeny, GitHub, výpisy chyb. Ostatní skripty (composer.js,
   cteni.js, stats.js…) se ptají na window.HUB_ADVANCED; tělo stránky má
   třídu `pokrocile`, kterou může použít CSS. */
function markAdvanced() {
  const on = !!(STATE && STATE.config && STATE.config.dev_mode);
  window.HUB_ADVANCED = on;
  document.body.classList.toggle('pokrocile', on);
}

/* ── server calls ─────────────────────────────────────────────────────────── */
async function api(path, body) {
  const url = `/api/${path}${path.includes('?') ? '&' : '?'}t=${encodeURIComponent(TOKEN)}`;
  const res = await fetch(url, body === undefined ? {} : {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    // Server chybu posílá jako {"error": "…"} — do hlášky patří věta, ne JSON.
    const body = await res.text();
    let message = body;
    try {
      const parsed = JSON.parse(body);
      message = parsed.error || parsed.detail || body;
    } catch (_) { /* není JSON */ }
    // Proxy před hubem (502/503/504, HTML stránka nginxu) = server se zrovna
    // restartuje; člověku patří věta, ne kus HTML.
    // Host v cizím prostoru, kterému vlastník přístup odebral (gateway/pristup.py):
    // brána už cookie smazala, stránka se načte znovu ve vlastním prostoru.
    if (res.status === 403 && /"pristup":\s*"odebran"/.test(body)) {
      toast('Přístup do tohohle prostoru ti byl odebrán.');
      setTimeout(() => location.reload(), 1500);
    }
    if ([502, 503, 504].includes(res.status) || /^\s*<(!doctype|html)/i.test(message)) {
      message = 'Server se právě restartuje, zkus to za chvíli.';
    }
    throw new Error(message);
  }
  return res.json();
}

// Diktování a předčítání (hlas.js) jdou přes API hubu — na počítači i na serveru.
if (window.HubHlas) {
  HubHlas.init({api, url: (name) => `/api/${name}?t=${encodeURIComponent(TOKEN)}`});
}

// Vrací, jestli zpráva odešla — bez spojení se nic neposílá a volající
// (bublina, composer.js) to musí vědět, aby zprávu nepovažoval za doručenou.
function send(msg) {
  if (!WS || WS.readyState !== WebSocket.OPEN) return false;
  try {
    WS.send(JSON.stringify(msg));
    return true;
  } catch (_) {
    return false;
  }
}

// Spojení, do kterého smí bublina poslat zprávu. Chvíli po znovupřipojení
// ještě ne: server teprve posílá výpis tabů a terminály se dohrávají, podle
// obrazovky se tedy nedá poznat, jestli zpráva stojí ve vstupním poli.
function online() {
  return !!WS && WS.readyState === WebSocket.OPEN && Date.now() - (WS.openedAt || 0) > 600;
}

function openExternal(path, kind, file) {
  api('open-path', {path, kind, file}).catch(() => toast('Nepodařilo se otevřít: ' + path));
}

/* Odkaz z terminálu. Na serveru (brána) by open-path pustil xdg-open na
 * serveru, kde se nic neukáže — odkaz se proto otevře tam, kde člověk sedí.
 * Na počítači jde do výchozího prohlížeče jako dřív. */
function openLink(url) {
  if (!/^https?:\/\//i.test(url)) return;
  if (!STATE.config.gateway_user) { openExternal(url); return; }
  // Okno appky (WebKitGTK) nové okno předá prohlížeči a vrátí null — podle
  // návratové hodnoty se tedy neúspěch poznat nedá, hláška by lhala.
  const win = window.open(url, '_blank');
  if (win) win.opener = null;
}

/* Hub běží na serveru (brána)? Pak schránka i prohlížeč serveru nejsou toho,
 * kdo se dívá — co sahá ven, musí jít přes jeho prohlížeč. */
function onServer() {
  // Volá se i z applyTheme, které běží dřív, než je stav načtený.
  return !!(STATE && STATE.config && STATE.config.gateway_user);
}

/* Do schránky toho, kdo se dívá. Na serveru přes prohlížeč: /api/clipboard by
 * psal do schránky serveru, kde žádná není. Na počítači je naopak spolehlivější
 * server, protože WebKitGTK stránku ke schránce nepustí (viz clipboard.js). */
function copyText(text) {
  if (!onServer()) return api('clipboard', {text, which: 'clipboard'});
  if (navigator.clipboard && navigator.clipboard.writeText) {
    return navigator.clipboard.writeText(text);
  }
  return Promise.reject(new Error('prohlížeč stránku ke schránce nepustil'));
}

/* Schránka terminálu (clipboard.js). Na serveru jen to, co dá prohlížeč:
 * PRIMARY stránce nedá vůbec a čtení jen na kliknutí (pravé tlačítko). Ctrl+V
 * tam clipboard.js nechytá — nativní vložení přinese text i obrázek. */
async function clipRead(which) {
  if (!onServer()) return api('clipboard?which=' + which);
  if (which !== 'clipboard' || !navigator.clipboard || !navigator.clipboard.readText) return {};
  try {
    return {text: await navigator.clipboard.readText()};
  } catch (_) {
    throw new Error('prohlížeč stránku ke schránce nepustil — vlož přes Ctrl+V');
  }
}

function clipWrite(text, which) {
  if (!onServer()) return api('clipboard', {text, which});
  return which === 'clipboard' ? copyText(text) : Promise.resolve();
}

/* ── theme ────────────────────────────────────────────────────────────────── */
const CSS_VARS = {
  AMBER: '--amber', BG: '--bg', BG_SIDEBAR: '--bg-sidebar', BG_CARD: '--bg-card',
  FG: '--fg', FG_BRIGHT: '--fg-bright', DIM: '--dim', GREEN: '--green',
  RED: '--red', CARD_HOVER: '--card-hover', BORDER: '--border', SECTION: '--section',
  CLOUD: '--cloud', FIRMA: '--firma', CHART: '--chart',
};

/* Paleta okna: výchozí z hubu (core.DARK / LIGHT), přes ni barvy zapnutého
   motivu (hub/motivy.py) — co motiv nemá, zůstane výchozí. Volá se i dřív,
   než je stav načtený, pak je prázdná a applyTheme nic nepřepíše. */
function palette() {
  const base = (STATE && STATE.palette && (DARK ? STATE.palette.dark : STATE.palette.light)) || {};
  const motiv = STATE && STATE.motiv && STATE.motiv.barvy && STATE.motiv.barvy[DARK ? 'dark' : 'light'];
  return motiv ? {...base, ...motiv} : base;
}

/* Zapnutý motiv: barvy jdou přes palette(), jeho CSS (fonty, rozložení) se
   načte jako <link id="hub-motiv"> za hub.css. Prázdné id = výchozí vzhled. */
function applyMotiv(m) {
  const motiv = m && m.id ? m : {id: ''};
  if (STATE) STATE.motiv = motiv;
  let link = document.getElementById('hub-motiv');
  const href = motiv.id && motiv.style
    ? `/api/motiv-soubor?id=${encodeURIComponent(motiv.id)}&f=${encodeURIComponent(motiv.style)}` +
      `&v=${encodeURIComponent(motiv.v || '')}&t=${encodeURIComponent(TOKEN)}`
    : '';
  if (!href) {
    if (link) link.remove();
  } else {
    if (!link) {
      link = document.createElement('link');
      link.id = 'hub-motiv';
      link.rel = 'stylesheet';
      document.head.appendChild(link);
    }
    if (link.getAttribute('href') !== href) link.setAttribute('href', href);
  }
  document.documentElement.dataset.motiv = motiv.id;
  applyTheme();
  if (typeof renderNewTabButtons === 'function' && STATE) { try { renderNewTabButtons(); } catch (_) { /* ještě není vykresleno */ } }
}

/* Barva tabu: nad firemním trezorem fialová, jinak barva prostředí — na
   počítači jantarová, v prostoru na serveru modrá. Kurzor, výběr i to, co si
   Claude Code kreslí sám, se řídí odsud, ať je celý tab v jedné barvě. */
function accentHex(tab) {
  const p = palette();
  if (tab && isFirmaVault(tab.vault)) return p.FIRMA;
  return onServer() ? p.CLOUD : p.AMBER;
}

function rgbOf(hex) {
  const h = String(hex || '').replace('#', '');
  if (h.length !== 6) return '';
  return [0, 2, 4].map((i) => parseInt(h.slice(i, i + 2), 16)).join(';');
}

function termTheme(tab) {
  const p = palette(), t = p.TERM_PALETTE, accent = accentHex(tab);
  return {
    background: p.BG, foreground: p.FG, cursor: accent, cursorAccent: p.BG,
    selectionBackground: `rgba(${rgbOf(accent).replace(/;/g, ',')},${DARK ? '.30' : '.22'})`,
    black: t[0], red: t[1], green: t[2], yellow: t[3], blue: t[4],
    magenta: t[5], cyan: t[6], white: t[7],
    brightBlack: t[8], brightRed: t[9], brightGreen: t[10], brightYellow: t[11],
    brightBlue: t[12], brightMagenta: t[13], brightCyan: t[14], brightWhite: t[15],
  };
}

function applyTheme() {
  const p = palette();
  for (const [key, cssVar] of Object.entries(CSS_VARS)) {
    if (p[key]) document.documentElement.style.setProperty(cssVar, p[key]);
  }
  // Sloupce ve statistikách jdou s prostředím jako zbytek okna. Vlastní krok
  // mají proto, že samotná barva prostředí je na graf málo kontrastní.
  document.documentElement.style.setProperty(
    '--chart', onServer() ? p.CHART_CLOUD : p.CHART);
  $('btn-theme').firstElementChild.firstElementChild
    .setAttribute('href', DARK ? '#i-moon' : '#i-sun');
  // Rozbalené <select>, posuvníky a další ovládací prvky kreslí prohlížeč sám —
  // bez tohohle zůstaly v tmavém režimu světlé.
  document.documentElement.style.colorScheme = DARK ? 'dark' : 'light';
  // Okno appky na Linuxu (WebKitGTK) kreslí rozbalený seznam jako GTK nabídku;
  // ta se řídí GTK motivem, tak se mu řekne, který je (hub/window.py).
  try {
    window.webkit.messageHandlers.hubTheme.postMessage(DARK ? 'dark' : 'light');
  } catch (_) { /* jiný prohlížeč — tam stačí color-scheme */ }
  // Každý tab má svou barvu — firemní je fialový i vedle modrého osobního.
  for (const tab of TABS) tab.term.options.theme = termTheme(tab);
}

function setTheme(dark, remember) {
  DARK = dark;
  if (remember) {
    try { localStorage.setItem('hub-theme', dark ? 'dark' : 'light'); } catch (_) { /* nic */ }
    // Téma si pamatuje i hub (hub-config): paměť prohlížeče se na počítači
    // ztratí s každým novým portem hubu a appka z plochy má vlastní.
    api('config', {theme: dark ? 'dark' : 'light'}).catch(() => {});
  }
  applyTheme();
}

/* Téma uložené v hubu má přednost před pamětí prohlížeče. */
function themeFromState() {
  const t = STATE && STATE.config && STATE.config.theme;
  if (t !== 'dark' && t !== 'light') {
    // Hub téma ještě nezná, prohlížeč ano (volba z doby před touhle verzí):
    // převzít, ať se po další aktualizaci neztratí.
    let saved = '';
    try { saved = localStorage.getItem('hub-theme') || ''; } catch (_) { /* nic */ }
    if (saved === 'dark' || saved === 'light') api('config', {theme: saved}).catch(() => {});
    return;
  }
  try { localStorage.setItem('hub-theme', t); } catch (_) { /* nic */ }
  if ((t === 'dark') !== DARK) {
    DARK = t === 'dark';
    applyTheme();
  }
}

/* ── sidebar ──────────────────────────────────────────────────────────────── */
function icon(name, cls) {
  return `<svg class="ico${cls ? ' ' + cls : ''}"><use href="#${name}"/></svg>`;
}

/* Tokeny a cena podle projektu (cesta → součty). Čte se z mezipaměti statistik,
   takže je to rychlé; po aktualizaci hubu se mezipaměť jednou přepočítá na
   pozadí (pár desítek vteřin) a seznam se pak dokreslí. */
let USAGE = {};
let usageTimer = null, usageAt = 0;
async function loadUsage() {
  clearTimeout(usageTimer);
  usageTimer = null;
  usageAt = Date.now();
  try {
    const res = await api('project-usage');
    USAGE = res.projects || {};
    renderProjects($('search').value);
    if (!$('projekty-page').hidden) renderProjekty();
    if (res.pending) usageTimer = setTimeout(loadUsage, 6000);
  } catch (_) { usageTimer = setTimeout(loadUsage, 30000); }
}

function renderProjects(filter) {
  const box = $('projects');
  const needle = (filter || '').trim().toLowerCase();
  box.textContent = '';
  const showArchived = !!STATE.config.show_archived;
  const all = STATE.projects.filter(p => showArchived || !p.archived);
  const shown = all.filter(p => !needle ||
    ((p.label || p.name).toLowerCase().includes(needle) ||
     p.name.toLowerCase().includes(needle)));
  const dirty = shown.filter(p => p.dirty).length;
  const archived = STATE.projects.filter(p => p.archived).length;
  $('projects-count').textContent =
    shown.length + (dirty ? `  ·  ${dirty} rozdělaných` : '');

  if (!shown.length) {
    box.innerHTML = '<div class="empty">(nic nenalezeno)</div>';
  }
  // Skupiny drží pohromadě, co k sobě patří; bez skupiny se nic nenadpisuje.
  let lastGroup = null;
  for (const p of shown) {
    const group = p.group || '';
    if (group !== lastGroup) {
      lastGroup = group;
      if (group) {
        const h = document.createElement('div');
        h.className = 'group-head';
        h.textContent = group;
        box.appendChild(h);
      }
    }
    const meta = [p.type, p.branch, p.dirty ? `${p.dirty} změn` : '']
      .filter(Boolean).join('  ·  ');
    // Karta je div, ne button: uvnitř má vlastní tlačítka a tlačítko
    // v tlačítku je neplatné HTML, které prohlížeče rozhodí po svém.
    const el = document.createElement('div');
    el.className = 'card' + (p.dirty ? ' dirty' : '') + (p.archived ? ' archived' : '')
                 + (p.image ? ' has-photo' : '');
    el.tabIndex = 0;
    el.innerHTML = `<span class="dot"></span>
      <span class="card-thumb"></span>
      <span class="card-col">
        <span class="card-name"></span>
        <span class="card-meta"></span>
        <span class="card-path"></span></span>
      <button class="card-usage" title="Využití tokenů — klikni pro statistiky" hidden></button>
      <button class="card-more" title="Možnosti">⋯</button>`;
    el.querySelector('.card-name').textContent = p.label || p.name;
    // Tokeny a odhad ceny u projektu (z mezipaměti statistik, /api/project-usage).
    const u = USAGE[p.path];
    if (u && (u.out || u.cost)) {
      const b = el.querySelector('.card-usage');
      b.hidden = false;
      b.innerHTML = '<span class="cu-cost"></span><span class="cu-tok"></span>';
      b.querySelector('.cu-cost').textContent = HubStats.dolary(u.cost);
      b.querySelector('.cu-tok').textContent = HubStats.cislo(u.out) + ' tok.';
      b.title = `Napsáno ${HubStats.cislo(u.out)} tokenů, ${u.answers} odpovědí v ${u.sessions} sezeních.\n` +
        `Odhad ceny podle ceníku API ${HubStats.dolary(u.cost)} — na předplatném se neplatí.\nKlikni pro statistiky projektu.`;
      b.onclick = (ev) => { ev.stopPropagation(); HubStats.openProject(hubIO(), p); };
    }
    el.querySelector('.card-meta').textContent = meta;
    el.querySelector('.card-path').textContent = shortPath(p.path);
    const thumb = el.querySelector('.card-thumb');
    if (p.image) {
      const img = document.createElement('img');
      img.src = imageUrl(p.image);
      img.alt = '';
      thumb.appendChild(img);
    } else {
      thumb.remove();
    }
    if (p.brief) {
      const flag = document.createElement('span');
      flag.className = 'card-flag';
      flag.title = 'Má popis projektu';
      flag.textContent = 'i';
      el.appendChild(flag);
    }
    el.title = [p.path, p.repo ? 'github: ' + p.repo : '',
                p.brief ? '\n' + p.brief.slice(0, 300) : '']
      .filter(Boolean).join('\n');
    const open = () => openTab({kind: 'project', path: p.path,
                                title: p.label || p.name,
                                agent: agentFor(p.path)});
    el.onclick = (ev) => { if (!ev.target.closest('.card-more')) open(); };
    el.onkeydown = (ev) => {
      if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); open(); }
    };
    el.oncontextmenu = (ev) => { ev.preventDefault(); projectMenu(ev, p); };
    el.querySelector('.card-more').onclick = (ev) => {
      ev.stopPropagation();
      const b = ev.currentTarget.getBoundingClientRect();
      projectMenu({clientX: b.left, clientY: b.bottom, preventDefault(){}}, p);
    };
    box.appendChild(el);
  }

  const foot = document.createElement('div');
  foot.className = 'projects-foot';
  const add = document.createElement('button');
  add.className = 'linkbtn';
  add.textContent = '＋ Přidat projekt';
  add.onclick = addProject;
  foot.appendChild(add);
  if (archived) {
    const t = document.createElement('button');
    t.className = 'linkbtn';
    t.textContent = showArchived ? 'skrýt archiv' : `archiv (${archived})`;
    t.onclick = async () => {
      await api('config', {show_archived: !showArchived});
      await reload();
    };
    foot.appendChild(t);
  }
  box.appendChild(foot);
}

/* Domovská složka je u každé cesty stejná a jen zabírá místo. */
function shortPath(path) {
  const home = STATE.home || '';
  const short = home && path.startsWith(home) ? '~' + path.slice(home.length) : path;
  return short.length > 42 ? '…' + short.slice(-41) : short;
}

async function addProject() {
  const picked = await pickFolder();
  if (!picked) return;
  try {
    await api('project', {action: 'add', path: picked});
    await reload();
    toast('Přidáno: ' + picked);
  } catch (err) { toast(err.message); }
}

/* Úprava projektu. Briefing je tu to hlavní: uloží se do CLAUDE.md projektu,
   takže si ho Claude Code přečte sám, jakmile ten projekt otevřeš. */
function editProject(p) {
  const box = document.createElement('div');
  box.className = 'onb';
  box.innerHTML = `
    <div class="onb-box">
      <div class="onb-head"><div>
        <div class="onb-title">Upravit projekt</div>
        <div class="onb-sub"></div>
      </div></div>
      <div class="onb-body">
        <div class="ed-grid">
          <label><span>Název v panelu</span>
            <input class="ed-label" type="text"></label>
          <label><span>Skupina</span>
            <input class="ed-group" type="text" list="ed-groups"
                   placeholder="např. Klienti, Vlastní, Archiv"></label>
        </div>
        <datalist id="ed-groups"></datalist>
        <label class="ed-full"><span>GitHub repo</span>
          <input class="ed-repo" type="text" placeholder="owner/repo"></label>
        <div class="set-title" style="margin-top:16px">Fotka projektu</div>
        <div class="ed-photo">
          <div class="ed-thumb"></div>
          <div class="ed-photo-btns">
            <button class="btn ghost ed-pick">Vybrat obrázek…</button>
            <button class="btn ghost ed-drop">Odebrat</button>
          </div>
          <input class="ed-file" type="file" accept="image/*" hidden>
        </div>
        <div class="set-title" style="margin-top:16px">Briefing</div>
        <div class="set-note">Napiš vlastními slovy, o co v projektu jde — stack,
          hosting, klient, na co si dát pozor. Uloží se do
          <code>CLAUDE.md</code> projektu, takže to Claude Code přečte sám,
          jakmile projekt otevřeš.</div>
        <textarea class="ed-brief" rows="8"
          placeholder="Např.: E-shop na vlastním PHP, Wedos hosting, deploy přes FTP…"></textarea>
      </div>
      <div class="onb-foot">
        <button class="btn ghost ed-archive"></button>
        <span class="spacer"></span>
        <button class="btn ghost ed-cancel">Zrušit</button>
        <button class="btn primary ed-save">Uložit</button>
      </div>
    </div>`;
  box.querySelector('.onb-sub').textContent = p.path;
  const q = (sel) => box.querySelector(sel);
  q('.ed-label').placeholder = p.name;
  q('.ed-label').value = p.label || '';
  q('.ed-group').value = p.group || '';
  q('.ed-repo').value = p.repo || '';
  q('.ed-brief').value = p.brief || '';

  // našeptávač skupin z toho, co už existuje
  const groups = [...new Set(STATE.projects.map(x => x.group).filter(Boolean))];
  for (const g of groups) {
    const o = document.createElement('option');
    o.value = g;
    q('#ed-groups').appendChild(o);
  }

  let image = p.image || '';
  const paintThumb = () => {
    const t = q('.ed-thumb');
    t.textContent = '';
    if (image) {
      const img = document.createElement('img');
      img.src = imageUrl(image);
      t.appendChild(img);
    } else {
      t.textContent = 'bez fotky';
    }
    q('.ed-drop').hidden = !image;
  };
  paintThumb();
  q('.ed-pick').onclick = () => q('.ed-file').click();
  q('.ed-drop').onclick = () => { image = ''; paintThumb(); };
  q('.ed-file').onchange = async (ev) => {
    const file = ev.target.files && ev.target.files[0];
    if (!file) return;
    q('.ed-pick').disabled = true;
    try {
      const data = await fileToBase64(file);
      const r = await api('upload', {name: file.name || 'projekt.png', data});
      image = r.path;
      paintThumb();
    } catch (err) {
      toast('Obrázek se nepodařilo nahrát: ' + err.message);
    } finally {
      q('.ed-pick').disabled = false;
    }
  };

  const arch = q('.ed-archive');
  arch.textContent = p.archived ? 'Vrátit z archivu' : 'Archivovat';
  const close = () => box.remove();
  q('.ed-cancel').onclick = close;
  // Jen klik, který na pozadí i začal: výběr textu v poli tažením ven z okna
  // končí na pozadí, prohlížeč to hlásí jako klik — a rozepsané úpravy by zmizely.
  let downOutside = false;
  box.addEventListener('pointerdown', (ev) => { downOutside = ev.target === box; });
  box.addEventListener('click', (ev) => { if (ev.target === box && downOutside) close(); });
  arch.onclick = async () => {
    try {
      await api('project', {action: 'save', path: p.path, archived: !p.archived});
      close();
      await reload();
      toast(p.archived ? 'Vráceno z archivu.' : 'Archivováno.');
    } catch (err) { toast(err.message); }
  };
  q('.ed-save').onclick = async () => {
    const btn = q('.ed-save');
    btn.disabled = true;
    try {
      const r = await api('project', {
        action: 'save', path: p.path,
        label: q('.ed-label').value, brief: q('.ed-brief').value,
        group: q('.ed-group').value, repo: q('.ed-repo').value.trim(),
        image,
      });
      close();
      await reload();
      toast(r.briefing && r.briefing.written
        ? 'Uloženo — briefing je v CLAUDE.md projektu.' : 'Uloženo.');
    } catch (err) {
      toast(err.message);
      btn.disabled = false;
    }
  };
  document.body.appendChild(box);
  q('.ed-label').focus();
}

function imageUrl(path) {
  return `/api/image?t=${encodeURIComponent(TOKEN)}&path=${encodeURIComponent(path)}`;
}

/* Soubor z prostoru ke stažení (čtení, Průběh, stažené v prohlížeči). Co se
   smí, hlídá server (/api/soubor): jen domov a složky projektů, žádná tajemství. */
function souborUrl(path, info) {
  return `/api/soubor?t=${encodeURIComponent(TOKEN)}&path=${encodeURIComponent(path)}${info ? '&info=1' : ''}`;
}
window.HubSoubor = {
  url: (path) => souborUrl(path),
  info: async (path) => {
    const r = await fetch(souborUrl(path, true), {credentials: 'same-origin'});
    return r.json().catch(() => ({ok: false}));
  },
  async stahni(path) {
    const info = await this.info(path).catch(() => null);
    if (!info || !info.ok) { toast((info && info.error) || 'Soubor teď nejde stáhnout.'); return; }
    const a = document.createElement('a');
    a.href = souborUrl(path);
    a.download = path.split(/[\\/]/).pop();
    document.body.appendChild(a);
    a.click();
    a.remove();
  },
};

/* Vestavěný prohlížeč něco stáhl (hub/stazene.py): soubor leží v ~/Stažené
   prostoru. Karta vpravo dole s „Stáhnout" a totéž do čtení tabu, na který
   se člověk dívá. */
let stazenoTimer = null;
function stazenoKarta(msg) {
  document.querySelectorAll('.stazeno-karta').forEach((n) => n.remove());
  const box = document.createElement('div');
  box.className = 'stazeno-karta';
  box.setAttribute('role', 'status');
  const txt = document.createElement('span');
  txt.className = 'cteni-ks-text';
  const jmeno = document.createElement('span');
  jmeno.className = 'cteni-ks-jmeno';
  jmeno.textContent = msg.name || msg.path.split('/').pop();
  const kde = document.createElement('span');
  kde.className = 'cteni-ks-cesta';
  kde.textContent = 'Stažené v prohlížeči · ' + msg.path.replace(/\/[^/]*$/, '');
  txt.title = msg.path;
  txt.append(jmeno, kde);
  const btn = document.createElement('button');
  btn.className = 'cteni-stahnout hlavni';
  btn.type = 'button';
  btn.textContent = 'Stáhnout';
  btn.onclick = () => { HubSoubor.stahni(msg.path); box.remove(); };
  const zavrit = document.createElement('button');
  zavrit.className = 'stazeno-zavrit';
  zavrit.type = 'button';
  zavrit.title = 'Zavřít';
  zavrit.textContent = '×';
  zavrit.onclick = () => box.remove();
  box.append(txt, btn, zavrit);
  document.body.appendChild(box);
  clearTimeout(stazenoTimer);
  stazenoTimer = setTimeout(() => box.remove(), 30000);
  if (ACTIVE && ACTIVE.cteni && ACTIVE.cteni.stazeno) ACTIVE.cteni.stazeno(msg);
}

/* „ukládá se sama · naposledy 14:05" — ať je vidět, že se na nic klikat nemusí. */
function autosaveLine() {
  if (!STATE.memory_autosave) return 'automatické ukládání vypnuté';
  const last = (STATE.autosave_recent || []).find((e) => e.status === 'saved');
  if (!last) return 'ukládá se sama';
  const d = new Date(last.at);
  const today = d.toDateString() === new Date().toDateString();
  const when = today ? d.toLocaleTimeString('cs-CZ', {hour: '2-digit', minute: '2-digit'})
                     : d.toLocaleDateString('cs-CZ', {day: 'numeric', month: 'numeric'});
  return `ukládá se sama · naposledy ${when}`;
}

/* Trezor Obsidian: na serveru a bez aplikace Obsidian ho hub ukáže sám
   (vault.js) — open-path by tam spustil něco na serveru, kde se nic neukáže.
   Na počítači s Obsidianem se otevírá aplikace jako dřív. */
function vaultPreview() {
  return !!window.HubVault && (onServer() || !STATE.obsidian);
}

/* Obsidian, který byl otevřený při zavření appky, se otevře znovu (jen
   náhled v hubu — aplikaci Obsidian na počítači neotvíráme sami). */
function reopenVaultPlace() {
  const v = LAST_PLACE.vault;
  if (!v || !STATE || !vaultPreview()) return;
  if (isFirmaVault(v.vault) && !firmaOf(v.vault)) return;
  openVault(v.path || '', v.vault || '', v.title || '');
}

function openVault(path, vault, title) {
  const firma = isFirmaVault(vault);
  const shared = /^sdilene:/.test(vault || '');
  const f = firma ? firmaOf(vault) : null;
  if (firma && f && f.zkratka !== prostor()) setProstor(f.zkratka);
  HubVault.open({
    api,
    path: path || '',
    vault: firma || shared ? vault : '',
    title: vault === 'firma' && firmyList().length < 2 ? 'Firemní poznámky'
      : firma ? 'Firma — ' + (f ? f.nazev : vault.slice(6))
      : shared ? 'Sdílené poznámky — ' + (title || vault.slice(8)) : 'Moje poznámky',
    fileUrl: (p) => `/api/vault-file?path=${encodeURIComponent(p)}` +
      (firma || shared ? '&vault=' + encodeURIComponent(vault) : '') +
      `&t=${encodeURIComponent(TOKEN)}`,
    uploadUrl: (p, overwrite) => `/api/vault-upload?path=${encodeURIComponent(p)}` +
      (firma || shared ? '&vault=' + encodeURIComponent(vault) : '') +
      (overwrite ? '&prepsat=1' : '') + `&t=${encodeURIComponent(TOKEN)}`,
    openLink,
    toast,
    // Firemní: jen ke čtení podle práva účtu, správa přístupů pro adminy.
    readonly: firma && (vault === 'firma' ? !!STATE.firma && STATE.firma.level === 'read'
                                          : !f || f.role === 'host'),
    admin: vault === 'firma' && !!STATE.firma && !!STATE.firma.admin,
    // Mazat ve firmě: kapitán a člen s právem mazat (gateway/firmy.py).
    mazat: firma && !!f && (f.role === 'kapitan' || !!f.mazat),
    obsidian: !firma && !shared && !onServer() && !!STATE.obsidian,
    openInObsidian: (p) => api('open-path', {kind: 'vault-note', file: p})
      .catch(() => toast('Obsidian se nepodařilo otevřít.')),
    // Změněné přístupy k firemním poznámkám se načtou restartem prostoru.
    restartSpace,
    // Přepínač trezorů nahoře v Obsidianu — poznámky nejsou v panelu vlevo.
    trezory: vaultList(),
    // Sdílené poznámky: založit nové a u otevřených členové / odejít.
    novySdileny: onServer() ? () => sharedCreate() : null,
    sdilenyMenu: onServer() ? () => sharedManage() : null,
    prepni: (v) => {
      const s = (sharedVaults || []).find((x) => 'sdilene:' + x.slug === v);
      openVault('', v, s ? s.name : '');
    },
    // Kde člověk v Obsidianu je — po dalším otevření appky se sem vrátí.
    onPlace: (p) => writePlace({vault: p === null ? null
      : {vault: firma || shared ? vault : '', path: p || '', title: title || ''}}),
  });
}

/* Které Obsidiany člověk má: osobní, firemní (když má přístup) a sdílené. */
let brMapa = {};            // které taby mají v prohlížeči karty (hlásí prohlizec.js)
let sharedVaults = [];
let sharedCizi = [];       // správce: sdílené, ve kterých není (jen spravovat, obsah nevidí)
function vaultList() {
  const out = [{id: '', name: 'Moje poznámky'}];
  const firmy = firmyList();
  for (const f of firmy) {
    out.push({id: firmaVaultId(f), name: firmy.length < 2 && f.zkratka === 'firma' ? 'Firemní' : f.nazev});
  }
  for (const v of sharedVaults) {
    if (!v.needs_restart) out.push({id: 'sdilene:' + v.slug, name: v.name, shared: true});
  }
  return out;
}

/* ── firemní Obsidian ─────────────────────────────────────────────────────────
   Společný trezor pro všechny účty na bráně. Claude do něj sám nezapíše:
   poznámku připraví (tools/firma.py), hub ji tady ukáže s náhledem a nahraje
   ji až brána po kliknutí na Nahrát. */
let firmaTimer = null;
let firmaCard = null;
const firmaLater = new Set();          // „Později": do obnovení stránky se neukáže
const firmaWarned = new Set();         // o zadrženém návrhu stačí říct jednou
let firmaBusy = false;                 // nahrávání z firemního tabu běží

/* ── prostor okna: osobní, nebo firma (gateway/firmy.py) ─────────────────────
   Každé okno je buď v osobním prostoru, nebo v jedné firmě (přepínač vedle
   „Server" v hlavičce, sessionStorage — jiné okno, jiná firma). Podle prostoru
   se řídí nový chat, seznam chatů, taby, Projekty (ve firmě Klienti), Obsidian
   i úvodní stránka. Chat firmy běží ve složce ~/firmy/<zkratka>[/<klient>/
   <podkategorie>] — podle ní hub pozná, kam chat patří, i po letech.
   Dnešní firemní trezor je firma `firma` (vault 'firma'). */
function firmyList() {
  const f = STATE && STATE.firma;
  if (!f) return [];
  if (Array.isArray(f.firmy)) return f.firmy;
  // Starší brána: jen dnešní firemní trezor.
  return f.vault ? [{zkratka: 'firma', nazev: 'Firemní', role: f.level === 'read' ? 'host' : 'clen'}] : [];
}
function hasFirma() { return firmyList().length > 0; }
function firmaVaultId(f) { return f.zkratka === 'firma' ? 'firma' : 'firma:' + f.zkratka; }
function isFirmaVault(v) { return v === 'firma' || /^firma:/.test(v || ''); }
function firmaOf(v) {
  const slug = v === 'firma' ? 'firma' : String(v || '').slice(6);
  return firmyList().find((f) => f.zkratka === slug) || null;
}
const ROLE_FIRMY = {kapitan: 'kapitán', clen: 'člen', host: 'host'};
/* Firma, která pracuje pro sebe (druh `projekty`): místo Klienti › klient ›
   podkategorie má rovnou Projekty › projekt (gateway/firmy.py DRUHY). */
function jeProjekty(f) { return !!f && f.druh === 'projekty'; }

/* Zkratka firmy okna, nebo '' (osobní prostor). */
function prostor() {
  let slug = '';
  try {
    slug = sessionStorage.getItem('hub-prostor');
    if (slug === null) slug = localStorage.getItem('hub-prostor-posledni') || '';
  } catch (e) { slug = ''; }
  return firmyList().some((f) => f.zkratka === slug) ? slug : '';
}
function prostorFirma() {
  const slug = prostor();
  return slug ? firmyList().find((f) => f.zkratka === slug) : null;
}
function setProstor(slug, keepTab) {
  try {
    sessionStorage.setItem('hub-prostor', slug || '');
    localStorage.setItem('hub-prostor-posledni', slug || '');
  } catch (e) { /* bez úložiště */ }
  applyProstor(keepTab);
}

/* Pracovní složka chatu firmy (core.FIRMY_PRACE). */
function firmaPrace(slug, klient, podkat) {
  return [STATE.home, 'firmy', slug, klient, podkat].filter(Boolean).join('/');
}
/* Do které firmy patří složka (chat, tab): zkratka, nebo '' (osobní). */
function prostorCesty(path) {
  const base = (STATE.home || '') + '/firmy/';
  const p = String(path || '');
  if (!STATE.home || !p.startsWith(base)) return '';
  const slug = p.slice(base.length).split('/')[0];
  return firmyList().some((f) => f.zkratka === slug) ? slug : '';
}
function tabProstor(t) {
  if (t && isFirmaVault(t.vault)) {
    const f = firmaOf(t.vault);
    return f ? f.zkratka : '';
  }
  return prostorCesty(t && t.path);
}

/* Firemní tab: Claude v něm pracuje nad Obsidianem firmy a zapisuje do něj
   rovnou — souhlas dal uživatel tím, že tab otevřel. Bez podkategorie je to
   chat celé firmy, s ní chat té podkategorie (Claude začne jejím CLAUDE.md). */
function openFirmaTab(slug, klient, podkat) {
  const f = (slug && firmyList().find((x) => x.zkratka === slug)) || prostorFirma() || firmyList()[0];
  if (!f) return null;
  if (f.zkratka !== prostor()) setProstor(f.zkratka, true);
  const title = [f.nazev, podkat || klient].filter(Boolean).join(' · ');
  return openTab({kind: 'project', path: firmaPrace(f.zkratka, klient, podkat), title,
                  agent: 'claude', vault: firmaVaultId(f)});
}

/* Nový chat v prostoru okna: ve firmě chat firmy, jinak osobní. */
function newChatHere(agentId) {
  if (prostorFirma()) return openFirmaTab();
  const a = agentId ? agentById(agentId) : (agentById(STATE.default_agent) || agentList(true)[0]);
  if (!a) return HubSettings.open({...hubIO(), state: STATE, tab: 'agenti'});
  return openTab({kind: 'project', path: STATE.home, title: newTabLabel(), agent: a.id});
}

/* Cizí prostory, kam mě vlastník pustil (gateway/pristup.py — jménem, nebo
   „Všichni na serveru“). Brána je vrací lehce (?jen=kam); přepínač prostoru
   je ukazuje hned pod Osobní a firmami, ať se do nich nemusí přes nastavení
   ani přes stránku brány. Načítá se nejvýš jednou za minutu, při otevření
   nabídky znovu. */
let PROSTORY = {kam: [], at: 0};
async function loadProstory(maxAge = 60000) {
  if (!onServer()) return PROSTORY;
  if (Date.now() - PROSTORY.at < maxAge) return PROSTORY;
  PROSTORY.at = Date.now();
  try {
    const r = await fetch('/gw/pristup?jen=kam', {credentials: 'same-origin'});
    const d = await r.json().catch(() => ({}));
    if (r.ok) PROSTORY = {kam: Array.isArray(d.kam) ? d.kam : [], at: Date.now()};
  } catch (_) { /* brána neodpovídá — nabídka zůstane, jak byla */ }
  renderProstorBtn();
  return PROSTORY;
}
const prostorKdo = (k) => k.name || k.email || '?';

/* Přepínač prostoru v hlavičce: osobní, firmy, cizí prostory a správa. */
function renderProstorBtn() {
  const b = $('btn-prostor');
  if (!b) return;
  const f = prostorFirma();
  const g = STATE.guest;
  const zakladat = !!(STATE.firma && STATE.firma.zakladat);
  b.hidden = !onServer() || (!hasFirma() && !zakladat && !g && !PROSTORY.kam.length);
  b.classList.toggle('firma', !!f);
  b.classList.toggle('guest', !!g);
  // V cizím prostoru: čí je a role — Osobní / firma je pak ta vlastníkova.
  b.querySelector('.prostor-name').textContent = g
    ? (g.owner_name || '?') + ' · ' + (f ? f.nazev : (g.label || g.role))
    : f ? f.nazev : 'Osobní';
  b.title = g
    ? `Jsi v prostoru: ${g.owner_name || '?'} (${g.label || g.role})` + (f ? ' · firma ' + f.nazev : '') +
      ' — klikni pro přepnutí nebo návrat do svého'
    : f ? 'Okno pracuje pro firmu ' + f.nazev + ' (' + ROLE_FIRMY[f.role] + ') — klikni pro přepnutí'
    : PROSTORY.kam.length && !hasFirma() ? 'Okno je v tvém prostoru — klikni a přepneš do prostoru, kam máš přístup'
    : 'Okno je v osobním prostoru — klikni pro přepnutí na firmu';
}
async function prostorMenu(ev) {
  const r = ev.currentTarget.getBoundingClientRect();
  await loadProstory(10000);
  const now = prostor();
  const g = STATE.guest;
  const items = [{icon: 'i-user', label: g ? 'Osobní · ' + (g.owner_name || '?') : 'Osobní prostor',
                  on: !now, run: () => setProstor('')}];
  for (const f of firmyList()) {
    items.push({icon: 'i-folder', label: f.nazev + ' · ' + ROLE_FIRMY[f.role], on: now === f.zkratka,
                color: 'var(--firma)', run: () => setProstor(f.zkratka)});
  }
  // Cizí prostory: přepnutí = cookie na bráně a nové načtení stránky.
  if (g || PROSTORY.kam.length) {
    items.push({heading: 'Prostory, kam máš přístup'});
    if (g) items.push({icon: 'i-hub', label: 'Zpět do svého prostoru', run: () => prostorJdi(0)});
    for (const k of PROSTORY.kam) {
      const tady = !!(g && g.owner === k.id);
      items.push({icon: 'i-lock', on: tady,
                  label: prostorKdo(k) + ' · ' + k.label + (k.pres === 'vsem' ? ' (všem)' : ''),
                  run: () => { if (!tady) prostorJdi(k.id); }});
    }
  }
  // Zakládat a spravovat firmy jde jen ve vlastním prostoru (host na to
  // stejně nedosáhne — brána, gateway/firmy.py, jde za přihlášeným).
  const tail = [];
  if (!g && STATE.firma && STATE.firma.zakladat) {
    tail.push({icon: 'i-plus', label: 'Založit firmu…', run: () => spravaFirem()});
  }
  if (!g && firmyList().some((f) => f.role === 'kapitan')) {
    tail.push({icon: 'i-gear', label: 'Spravovat firmy…', run: () => spravaFirem()});
  }
  if (tail.length && (g || PROSTORY.kam.length)) items.push({heading: 'Firmy'});
  items.push(...tail);
  showMenu(r.left, r.bottom, items);
}
function spravaFirem() {
  HubSettings.open({...hubIO(), state: STATE, tab: 'firmy'});
}

/* Přepnutí prostoru: překreslí všechno, co se podle něj řídí. Tab z jiného
   prostoru se schová (běží dál); `keepTab` = přepíná se kvůli tabu samotnému. */
function applyProstor(keepTab) {
  if (!STATE || !STATE.config) return;
  const f = prostorFirma();
  document.documentElement.dataset.prostor = f ? 'firma' : '';
  renderProstorBtn();
  renderNewTabButtons();
  const nav = $('nav-projekty');
  if (nav) nav.lastChild.textContent = f && !jeProjekty(f) ? ' Klienti' : ' Projekty';
  for (const t of TABS) if (t.el) t.el.hidden = tabProstor(t) !== prostor();
  paintSdileneProstor();
  if (typeof renderRelace === 'function' && RELACE) renderRelace();
  if (!keepTab && ACTIVE && tabProstor(ACTIVE) !== prostor()) activate(null);
  renderChats();
  if (!$('projekty-page').hidden) renderProjekty();
  if (!ACTIVE) { renderWelcome(); renderWelcomeOpen(); }
}

function renderFirma() {
  const on = hasFirma();
  // Tlačítka v liště („osobní" a „firemní") řeší renderNewTabButtons.
  if (!on) return;
  renderShared();
  if (!firmaTimer) {
    firmaTimer = setInterval(checkFirma, 3000);
    checkFirma();
  }
}

/* ── sdílené Obsidiany ─────────────────────────────────────────────────────────
   Trezory jen pro vybrané lidi (gateway/shared.py). Seznam je živý z brány —
   co je nové, se do prostoru sváže až po restartu, a panel ho nabídne. */
let sharedTimer = null;

async function renderShared() {
  const box = $('shared-section');
  if (!onServer()) { box.hidden = true; return; }
  if (!sharedTimer) {
    sharedTimer = setInterval(() => { if (!document.hidden) renderShared(); }, 60000);
  }
  let data;
  try {
    const r = await fetch('/gw/sdilene', {credentials: 'same-origin'});
    if (!r.ok) throw new Error('Server teď neodpovídá, zkus to za chvíli.');
    data = await r.json();
  } catch (err) {
    box.hidden = true;                 // starší brána — sekce přijde s aktualizací
    return;
  }
  box.hidden = false;
  sharedPeople = data.people || [];
  sharedVaults = data.vaults || [];
  sharedCizi = data.spravovane || [];
  const list = $('shared-list');
  list.textContent = '';
  const vaults = data.vaults || [];
  if (!vaults.length) {
    const hint = document.createElement('div');
    hint.className = 'empty shared-hint';
    hint.textContent = 'Zatím žádné. Poznámky jen pro vybrané lidi založíš tlačítkem níž.';
    list.appendChild(hint);
  }
  if (!$('shared-new')) {
    const add = document.createElement('button');
    add.id = 'shared-new';
    add.className = 'btn ghost shared-new';
    add.textContent = '+ Nové sdílené poznámky';
    add.onclick = sharedCreate;
    box.appendChild(add);
  }
  for (const v of vaults) {
    const row = document.createElement('div');
    row.className = 'shared-row';
    const more = document.createElement('button');
    more.className = 'shared-more';
    more.textContent = '⋯';
    more.title = 'Členové, odejít, smazat';
    more.onclick = (ev) => {
      ev.stopPropagation();
      const r = more.getBoundingClientRect();
      sharedMenu(v, r.left, r.bottom, true);
    };
    const item = document.createElement('button');
    item.className = 'shared-item' + (v.needs_restart ? ' pending' : '');
    item.dataset.slug = v.slug;
    item.innerHTML = '<span class="shared-name"></span><span class="shared-meta"></span>';
    item.querySelector('.shared-name').textContent = v.name;
    const who = (v.members || []).map((m) => m.name || m.email).join(', ');
    item.querySelector('.shared-meta').textContent = v.needs_restart ? 'načte se po restartu prostoru' : who;
    item.title = 'Členové: ' + who + (v.owner ? '\nZaložil: ' + v.owner : '');
    item.onclick = () => (v.needs_restart
      ? restartSpace(`Sdílený Obsidian „${v.name}“ se do prostoru načte po restartu.`)
      : openVault('', 'sdilene:' + v.slug, v.name));
    row.append(item, more);
    list.appendChild(row);
  }
}

/* Nabídka sdílených poznámek: v Obsidianu (přepínač nahoře) i jinde. */
function sharedMenu(v, x, y, withOpen) {
  const items = withOpen && v.is_member !== false
    ? [{icon: 'i-book', label: 'Otevřít', run: () => openVault('', 'sdilene:' + v.slug, v.name)}] : [];
  const spravuje = v.can_manage !== undefined ? v.can_manage : v.is_owner;
  if (spravuje) {
    items.push({icon: 'i-user', label: 'Kdo je vidí…', run: () => sharedMembers(v)});
    items.push({icon: 'i-close', label: 'Smazat…', run: () => sharedDelete(v)});
  }
  if (!v.is_owner && v.is_member !== false) {
    items.push({icon: 'i-close', label: 'Odejít…', run: () => sharedAction(v, 'odejit',
      `Odejít ze sdílených poznámek „${v.name}“? Přestaneš je vidět.`)});
  }
  showMenu(x, y, items);
}

/* Smazání sdíleného Obsidianu: soubory odložit stranou (dají se vrátit),
   nebo smazat natrvalo (uvolní místo na disku serveru). */
function sharedDelete(v) {
  return new Promise((resolve) => {
    const wrap = document.createElement('div');
    wrap.className = 'onb set-modal vault-access-modal';
    wrap.innerHTML = `
      <div class="onb-box acc-box">
        <div class="onb-head"><div><div class="onb-title"></div><div class="onb-sub">Zmizí všem, kdo je vidí.</div></div>
          <span class="spacer"></span><button class="set-x sd-close" title="Zavřít">×</button></div>
        <div class="acc-list sd-volby">
          <label class="sd-volba"><input type="radio" name="sd" value="" checked>
            <span><b>Smazat, soubory odložit stranou</b><br><small>Zůstanou na serveru, správce je umí vrátit.</small></span></label>
          <label class="sd-volba"><input type="radio" name="sd" value="1">
            <span><b>Smazat i se soubory natrvalo</b><br><small>Uvolní místo na disku. Vrátit to nejde.</small></span></label>
        </div>
        <div class="who-foot"><button class="btn ghost sd-close">Zrušit</button><button class="btn danger sd-ok">Smazat</button></div>
      </div>`;
    wrap.querySelector('.onb-title').textContent = 'Smazat sdílené poznámky „' + v.name + '“?';
    const zavri = () => { wrap.remove(); resolve(); };
    wrap.querySelectorAll('.sd-close').forEach((b) => { b.onclick = zavri; });
    wrap.onclick = (ev) => { if (ev.target === wrap) zavri(); };
    wrap.querySelector('.sd-ok').onclick = async () => {
      const natrvalo = !!wrap.querySelector('input[name=sd]:checked').value;
      if (natrvalo && !(await HubDialog.confirm(`Opravdu smazat „${v.name}“ i se všemi soubory natrvalo?`, {title: 'Smazat natrvalo', ok: 'Smazat natrvalo'}))) return;
      wrap.querySelector('.sd-ok').disabled = true;
      try {
        const out = await sharedPost({akce: 'smazat', slug: v.slug, natrvalo});
        toast(out.message || 'Smazáno.');
      } catch (err) {
        toast(err.message);
      }
      zavri();
      await renderShared();
      document.dispatchEvent(new CustomEvent('hub-shared-changed'));
    };
    document.body.appendChild(wrap);
  });
}

/* Správa sdílených poznámek z Obsidianu: všechny, které člověk vidí (a správce
   i ty ostatní), každé s tím, co s nimi smí. */
async function sharedManage() {
  await renderShared();
  const wrap = document.createElement('div');
  wrap.className = 'onb set-modal vault-access-modal';
  wrap.innerHTML = `
    <div class="onb-box acc-box">
      <div class="onb-head"><div><div class="onb-title">Sdílené poznámky</div>
        <div class="onb-sub">Poznámky jen pro vybrané lidi.</div></div>
        <span class="spacer"></span><button class="set-x sm-close" title="Zavřít">×</button></div>
      <div class="acc-list sm-list"></div>
      <div class="who-foot"><button class="btn ghost sm-new">+ Nové sdílené poznámky</button></div>
    </div>`;
  const zavri = () => { wrap.remove(); document.removeEventListener('hub-shared-changed', kresli); };
  wrap.querySelector('.sm-close').onclick = zavri;
  wrap.onclick = (ev) => { if (ev.target === wrap) zavri(); };
  wrap.querySelector('.sm-new').onclick = () => { zavri(); sharedCreate(); };
  function kresli() {
    const list = wrap.querySelector('.sm-list');
    list.textContent = '';
    const vse = [...sharedVaults, ...sharedCizi];
    if (!vse.length) list.appendChild(Object.assign(document.createElement('p'), {className: 'who-hint', textContent: 'Zatím žádné.'}));
    for (const v of vse) {
      const r = document.createElement('div');
      r.className = 'sm-radek';
      const info = document.createElement('div');
      info.className = 'sm-info';
      const jm = document.createElement('b');
      jm.textContent = v.name;
      const kdo = document.createElement('small');
      kdo.textContent = (v.members || []).map((m) => m.name || m.email).join(', ') +
        (v.is_member === false ? ' · nejsi členem' : '') + (v.owner ? '' : ' · bez zakladatele');
      info.append(jm, kdo);
      r.appendChild(info);
      const btn = (text, fn, cls) => {
        const b = document.createElement('button');
        b.className = 'btn ' + (cls || 'ghost');
        b.textContent = text;
        b.onclick = fn;
        r.appendChild(b);
      };
      if (v.is_member !== false && !v.needs_restart) btn('Otevřít', () => { zavri(); openVault('', 'sdilene:' + v.slug, v.name); });
      if (v.can_manage) btn('Lidé…', async () => { await sharedMembers(v); await renderShared(); kresli(); });
      if (!v.is_owner && v.is_member !== false) btn('Odejít', async () => { await sharedAction(v, 'odejit', `Odejít ze sdílených poznámek „${v.name}“? Přestaneš je vidět.`); kresli(); });
      if (v.can_manage) btn('Smazat…', () => sharedDelete(v), 'danger');
      list.appendChild(r);
    }
  }
  document.addEventListener('hub-shared-changed', kresli);
  kresli();
  document.body.appendChild(wrap);
}

/* Sdílené poznámky naklikáním (brána POST /gw/sdilene, gateway/shared.py).
   Kdo co smí, ověřuje brána: členy mění a maže jen zakladatel. */
let sharedPeople = [];

async function sharedPost(body) {
  const r = await fetch('/gw/sdilene', {
    method: 'POST', credentials: 'same-origin',
    headers: {'Content-Type': 'application/json', 'X-Hub-Account': '1'},
    body: JSON.stringify(body),
  });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || 'Server teď neodpovídá, zkus to za chvíli.');
  return data;
}

/* Okno s výběrem lidí (a názvem). Vrací {name, emails} nebo null. */
function peopleDialog({title, note, withName, chosen, ok, withWrite, chosenWrite, hint,
                       groups, chosenGroups, chosenGroupsWrite, people = sharedPeople}) {
  return new Promise((resolve) => {
    const me = ((STATE.config && STATE.config.gateway_user) || {}).email || '';
    const wrap = document.createElement('div');
    wrap.className = 'onb set-modal vault-access-modal';
    wrap.innerHTML = `
      <div class="onb-box acc-box">
        <div class="onb-head"><div><div class="onb-title"></div><div class="onb-sub"></div></div>
          <span class="spacer"></span><button class="set-x pd-close" title="Zavřít">×</button></div>
        <div class="acc-list">
          <label class="mcp-field pd-name"><span>Název</span><input class="set-input" placeholder="např. Marketing"></label>
          <p class="who-hint"></p>
          <div class="who-people"></div>
        </div>
        <div class="who-foot"><button class="btn ghost pd-close">Zrušit</button><button class="btn pd-ok"></button></div>
      </div>`;
    wrap.querySelector('.onb-title').textContent = title;
    wrap.querySelector('.onb-sub').textContent = note || '';
    wrap.querySelector('.pd-ok').textContent = ok;
    wrap.querySelector('.who-hint').textContent = hint || 'Kdo je uvidí (ty je vidíš vždycky):';
    wrap.querySelector('.pd-name').hidden = !withName;
    const picked = new Set(chosen || []);
    const writers = new Set(chosenWrite || []);
    const list = wrap.querySelector('.who-people');
    for (const p of people) {
      if (p.email === me) continue;
      const row = document.createElement('label');
      row.className = 'acc-row who-row';
      const cb = document.createElement('input');
      cb.type = 'checkbox';
      cb.checked = picked.has(p.email);
      let wcb = null;
      cb.onchange = () => {
        if (cb.checked) picked.add(p.email); else { picked.delete(p.email); writers.delete(p.email); }
        if (wcb) { wcb.disabled = !cb.checked; if (!cb.checked) wcb.checked = false; }
      };
      const who = document.createElement('div');
      who.className = 'acc-who';
      const strong = document.createElement('strong');
      strong.textContent = p.name || p.email;
      const mail = document.createElement('span');
      mail.className = 'acc-mail';
      mail.textContent = p.email;
      who.append(strong, mail);
      row.append(cb, who);
      if (withWrite) {
        // Číst smí každý vybraný; psát (zadávat Claudovi) jen ten, komu to výslovně dovolíš.
        const wl = document.createElement('label');
        wl.className = 'who-write';
        wl.title = 'Smí psát do chatu — jeho zprávy dostane tvůj Claude a pracuje na nich s tvými právy';
        wcb = document.createElement('input');
        wcb.type = 'checkbox';
        wcb.checked = writers.has(p.email);
        wcb.disabled = !cb.checked;
        wcb.onchange = () => (wcb.checked ? writers.add(p.email) : writers.delete(p.email));
        wl.append(wcb, document.createTextNode(' smí psát'));
        row.append(wl);
      }
      list.appendChild(row);
    }
    // Podkategorie firem (gateway/firmy.py): uvidí každý, kdo do ní vidí —
    // i ten, kdo k ní přibude později.
    const gPicked = new Set(chosenGroups || []);
    const gWriters = new Set(chosenGroupsWrite || []);
    if (groups && groups.length) {
      const h = document.createElement('p');
      h.className = 'who-hint';
      h.textContent = 'Podkategorie firem — uvidí každý, kdo do ní vidí:';
      list.appendChild(h);
    }
    for (const g of groups || []) {
      const row = document.createElement('label');
      row.className = 'acc-row who-row firma';
      const cb = document.createElement('input');
      cb.type = 'checkbox';
      cb.checked = gPicked.has(g.id);
      let wcb = null;
      cb.onchange = () => {
        if (cb.checked) gPicked.add(g.id); else { gPicked.delete(g.id); gWriters.delete(g.id); }
        if (wcb) { wcb.disabled = !cb.checked; if (!cb.checked) wcb.checked = false; }
      };
      const who = document.createElement('div');
      who.className = 'acc-who';
      const strong = document.createElement('strong');
      strong.textContent = g.nazev;
      who.append(strong);
      row.append(cb, who);
      if (withWrite) {
        const wl = document.createElement('label');
        wl.className = 'who-write';
        wl.title = 'Psát smí každý z podkategorie — jeho zprávy dostane tvůj Claude a pracuje na nich s tvými právy';
        wcb = document.createElement('input');
        wcb.type = 'checkbox';
        wcb.checked = gWriters.has(g.id);
        wcb.disabled = !cb.checked;
        wcb.onchange = () => (wcb.checked ? gWriters.add(g.id) : gWriters.delete(g.id));
        wl.append(wcb, document.createTextNode(' smí psát'));
        row.append(wl);
      }
      list.appendChild(row);
    }
    const done = (value) => { wrap.remove(); resolve(value); };
    for (const b of wrap.querySelectorAll('.pd-close')) b.onclick = () => done(null);
    wrap.addEventListener('click', (ev) => { if (ev.target === wrap) done(null); });
    wrap.querySelector('.pd-ok').onclick = () => {
      const name = wrap.querySelector('.pd-name input').value.trim();
      if (withName && !name) return toast('Napiš název.');
      if (!picked.size && !gPicked.size) {
        return toast(groups && groups.length ? 'Vyber aspoň jednoho člověka nebo podkategorii.'
                                             : 'Vyber aspoň jednoho člověka.');
      }
      done({name, emails: [...picked], psat: [...writers].filter((e) => picked.has(e)),
            skupiny: [...gPicked], skupiny_psat: [...gWriters].filter((g) => gPicked.has(g))});
    };
    document.body.appendChild(wrap);
    if (withName) wrap.querySelector('.pd-name input').focus();
  });
}

async function sharedCreate() {
  const got = await peopleDialog({title: 'Nové sdílené poznámky', withName: true, ok: 'Založit',
    note: 'Poznámky a soubory jen pro tebe a vybrané lidi.'});
  if (!got) return;
  try {
    await sharedPost({akce: 'zalozit', nazev: got.name, emaily: got.emails});
  } catch (err) {
    return toast(err.message);
  }
  await renderShared();
  restartSpace(`Sdílené poznámky „${got.name}“ jsou založené. Ve tvém prostoru se objeví po restartu.`);
}

async function sharedMembers(v) {
  const before = (v.members || []).map((m) => m.email);
  const got = await peopleDialog({title: 'Kdo vidí „' + v.name + '“', ok: 'Uložit', chosen: before,
    note: 'Přidaní je uvidí po restartu svého prostoru, odebraným zmizí.'});
  if (!got) return;
  const me = ((STATE.config && STATE.config.gateway_user) || {}).email || '';
  const pridat = got.emails.filter((e) => !before.includes(e));
  const odebrat = before.filter((e) => e !== me && !got.emails.includes(e));
  if (!pridat.length && !odebrat.length) return;
  try {
    const out = await sharedPost({akce: 'clenove', slug: v.slug, pridat, odebrat});
    toast(out.message || 'Uloženo.');
  } catch (err) {
    toast(err.message);
  }
  renderShared();
}

async function sharedAction(v, akce, question) {
  if (!(await HubDialog.confirm(question, {title: 'Potvrď', ok: 'Potvrdit'}))) return;
  try {
    const out = await sharedPost({akce, slug: v.slug});
    toast(out.message || 'Hotovo.');
  } catch (err) {
    toast(err.message);
  }
  renderShared();
}

async function restartSpace(why) {
  if (!(await HubDialog.confirm(why + '\n\nRestartovat teď? Zabere to pár vteřin a otevřené ' +
               'rozhovory se pak vrátí.', {title: 'Restartovat prostor', ok: 'Restartovat'}))) return;
  try {
    const r = await fetch('/gw/restart', {
      method: 'POST', credentials: 'same-origin',
      headers: {'Content-Type': 'application/json', 'X-Hub-Account': '1'}, body: '{}',
    });
    if (!r.ok) throw new Error('Server teď neodpovídá, zkus to za chvíli.');
  } catch (err) {
    toast('Restart se nepovedl: ' + err.message);
    return;
  }
  toast('Prostor se restartuje…');
  setTimeout(() => location.reload(), 2500);
}

async function checkFirma() {
  if (document.hidden || firmaCard || !window.HubVault) return;
  let res;
  try {
    res = await api('firma');
  } catch (err) {
    return;                            // starší hub v prostoru — po restartu to umí
  }
  const waiting = (res.pending || []).filter((p) => !firmaLater.has(p.id));
  // Z firemního tabu se nahrává rovnou. Co vypadá jako přihlašovací údaj, hub
  // tiše nenahraje — ukáže kartu a jednou to i napíše.
  const rovnou = waiting.find((p) => p.auto);
  if (rovnou) return publishFirma(rovnou);
  const citlive = waiting.find((p) => p.citlive && !firmaWarned.has(p.id));
  if (citlive) {
    firmaWarned.add(citlive.id);
    toast('Vypadá to na přihlašovací údaje — do firemního se to nahraje až po potvrzení.');
  }
  if (waiting.length && !firmaCard) showFirmaCard(waiting[0], waiting.length);
}

/* Nahrání bez karty (firemní tab). Zapisuje pořád brána a jen s cookie
   z tohohle prohlížeče — hub v prostoru do firemního trezoru nemůže. */
async function publishFirma(p) {
  if (firmaBusy) return;
  firmaBusy = true;
  let res = {};
  try {
    const r = await fetch('/gw/firma/publish', {
      method: 'POST', credentials: 'same-origin',
      headers: {'Content-Type': 'application/json', 'X-Hub-Firma': '1'},
      body: JSON.stringify({id: p.id, prepsat: !!p.prepise}),
    });
    res = await r.json().catch(() => ({error: `HTTP ${r.status}`}));
  } catch (err) {
    res = {error: err.message};
  }
  firmaBusy = false;
  if (res.ok) {
    return toast((res.message || `Nahráno do firemního Obsidianu: ${res.path}`) +
                 (p.prepise ? ' (přepsáno)' : ''));
  }
  // Když to neprojde, ať to nezmizí potichu — ukáže se karta jako jindy.
  toast('Nahrát do firemního se nepodařilo: ' + (res.error || 'neznámá chyba'));
}

function showFirmaCard(p, total) {
  const root = document.createElement('div');
  root.className = 'onb firma-modal';
  root.innerHTML = `
    <div class="onb-box">
      <div class="onb-head">
        <span class="onb-mark">${icon('i-book')}</span>
        <div>
          <div class="onb-title">Nahrát do firemního Obsidianu?</div>
          <div class="onb-sub"></div>
        </div>
      </div>
      <div class="onb-body">
        <div class="firma-target"><span>Kam</span><code></code></div>
        <div class="firma-warn" hidden>Na téhle cestě už poznámka je — nahráním se přepíše.</div>
        <div class="firma-people" hidden></div>
        <article class="vault-md firma-preview"></article>
      </div>
      <div class="onb-foot">
        <button class="btn ghost firma-later">Později</button>
        <button class="btn ghost firma-discard">Zahodit</button>
        <span class="spacer"></span>
        <button class="btn primary firma-ok"></button>
      </div>
    </div>`;
  const $$ = (sel) => root.querySelector(sel);
  // Druh návrhu: firemní Obsidian, nebo sdílený (zápis, založení, členové…).
  const kind = p.druh || 'firma';
  const writes = kind === 'firma' || kind === 'sdilene-zapis';
  const where = p.nazev ? `„${p.nazev}“` : '';
  const titles = {
    'firma': p.firma && p.firma_nazev ? `Nahrát do firmy „${p.firma_nazev}“?` : 'Nahrát do firemního Obsidianu?',
    'sdilene-zapis': `Nahrát do sdíleného Obsidianu ${where}?`,
    'sdilene-zalozit': `Založit sdílený Obsidian ${where}?`,
    'sdilene-clenove': `Změnit členy sdíleného Obsidianu ${where}?`,
    'sdilene-odejit': `Odejít ze sdíleného Obsidianu ${where}?`,
    'sdilene-smazat': `Smazat sdílený Obsidian ${where}?`,
  };
  const okLabels = {'sdilene-zalozit': 'Založit', 'sdilene-clenove': 'Změnit',
                    'sdilene-odejit': 'Odejít', 'sdilene-smazat': 'Smazat'};
  const names = (list) => (list || []).join(', ');
  const details = {
    'sdilene-zapis': p.lide && p.lide.length ? 'Uvidí: ' + names(p.lide) : '',
    'sdilene-zalozit': 'Pro: ' + names(p.lide),
    'sdilene-clenove': [p.pridat && p.pridat.length ? 'Přidat: ' + names(p.pridat) : '',
                        p.odebrat && p.odebrat.length ? 'Odebrat: ' + names(p.odebrat) : '']
      .filter(Boolean).join('\n'),
    'sdilene-odejit': 'Přestaneš ho vidět; vrátit tě může jen ten, kdo ho založil.',
    'sdilene-smazat': 'Zmizí všem členům' + (p.lide && p.lide.length ? ' (' + names(p.lide) + ')' : '') +
      '. Soubory zůstanou na serveru stranou.',
  };
  $$('.onb-title').textContent = titles[kind] || 'Potvrdit návrh?';
  $$('.onb-sub').textContent = 'Claude to připravil v tomhle prostoru' +
    (total > 1 ? ` · čeká ${total} návrhů` : '') + '. Provede se až po potvrzení.';
  $$('.firma-target').hidden = !writes;
  $$('.firma-target code').textContent = p.cil || '';
  const people = $$('.firma-people');
  people.textContent = details[kind] || '';
  people.hidden = !people.textContent;
  people.classList.toggle('danger', kind === 'sdilene-smazat' || kind === 'sdilene-odejit');
  const paint = () => {
    $$('.firma-warn').hidden = !(writes && p.prepise);
    $$('.firma-ok').textContent = writes ? (p.prepise ? 'Přepsat' : 'Nahrát') : (okLabels[kind] || 'Potvrdit');
  };
  paint();
  $$('.firma-preview').hidden = !writes;
  if (writes) {
    $$('.firma-preview').innerHTML = HubVault.render(p.text || '',
      {resolve: () => '', image: () => '', current: ''}).html;
  }
  const buttons = [...root.querySelectorAll('button')];
  const busy = (on) => buttons.forEach((b) => { b.disabled = on; });
  const close = () => {
    root.remove();
    document.removeEventListener('keydown', onKey, true);
    firmaCard = null;
    setTimeout(checkFirma, 300);       // další návrh v řadě
  };
  const later = () => { firmaLater.add(p.id); close(); };
  function onKey(ev) {
    if (ev.key === 'Escape') { ev.stopPropagation(); later(); }
  }
  $$('.firma-later').onclick = later;
  // Jen klik, který na pozadí i začal — výběr textu z náhledu tažením ven by
  // jinak kartu schoval.
  let downOutside = false;
  root.addEventListener('pointerdown', (ev) => { downOutside = ev.target === root; });
  root.addEventListener('click', (ev) => { if (ev.target === root && downOutside) later(); });
  $$('.firma-discard').onclick = async () => {
    busy(true);
    try {
      await api('firma', {action: 'discard', id: p.id});
      toast('Návrh do firemního Obsidianu zahozen.');
      close();
    } catch (err) {
      toast('Zahodit se nepodařilo: ' + err.message);
      busy(false);
    }
  };
  $$('.firma-ok').onclick = async () => {
    busy(true);
    let res = {};
    try {
      // Nahrává brána, ne hub v prostoru — a jen s cookie z tohohle prohlížeče.
      const r = await fetch('/gw/firma/publish', {
        method: 'POST', credentials: 'same-origin',
        headers: {'Content-Type': 'application/json', 'X-Hub-Firma': '1'},
        body: JSON.stringify({id: p.id, prepsat: !!p.prepise}),
      });
      res = await r.json().catch(() => ({error: `HTTP ${r.status}`}));
    } catch (err) {
      res = {error: err.message};
    }
    if (res.ok) {
      toast(res.message || `Nahráno do firemního Obsidianu: ${res.path}`);
      close();
      if (kind !== 'firma') renderShared();
    } else if (res.exists) {
      // Mezitím tam poznámku nahrál někdo jiný — ať je vidět, že se přepíše.
      p.prepise = true;
      paint();
      busy(false);
    } else {
      toast('Nahrát se nepodařilo: ' + (res.error || 'neznámá chyba'));
      busy(false);
    }
  };
  document.addEventListener('keydown', onKey, true);
  document.body.appendChild(root);
  firmaCard = root;
}

/* ── konverzace ───────────────────────────────────────────────────────────────
   Seznam minulých konverzací s Claudem jako v oficiální appce: podle dní,
   s hledáním. Klik konverzaci otevře (claude --resume) — nebo přepne na tab,
   ve kterém už běží. Data: /api/chats (hub/chats.py). */
let CHATS = [];
let chatsTimer = null;
const CHATS_PAGE = 60;
let chatsShown = CHATS_PAGE;

const foldText = (s) => String(s || '').normalize('NFD').replace(/\p{M}/gu, '').toLowerCase();

function sidebarView(view) {
  const onChats = view === 'chats';
  $('projects-view').hidden = onChats;
  $('chats-view').hidden = !onChats;
  $('projects-count').hidden = onChats;
  $('chats-count').hidden = !onChats;
  $('list-head').textContent = onChats ? 'Chaty' : 'Projekty';
  for (const b of document.querySelectorAll('.side-tab')) {
    b.classList.toggle('on', b.dataset.view === (onChats ? 'chats' : 'projects'));
  }
  try { localStorage.setItem('hub-panel', onChats ? 'chats' : 'projects'); } catch (_) {}
  clearInterval(chatsTimer);
  chatsTimer = null;
  if (onChats) {
    loadChats();
    chatsTimer = setInterval(() => { if (!document.hidden) loadChats(); }, 20000);
  }
}

async function loadChats() {
  let res;
  try {
    res = await api('chats');
  } catch (err) {
    const box = $('chats');
    box.textContent = '';
    const note = document.createElement('div');
    note.className = 'empty';
    note.textContent = 'Konverzace se nenačetly: ' + err.message;
    box.appendChild(note);
    chatsSig = '';
    return;
  }
  CHATS = res.chats || [];
  /* Seznam se načítá každých 20 s. Překreslit ho celý pokaždé znamená, že
     tlačítko pod prstem zmizí dřív, než klepnutí doběhne — na telefonu pak
     „nejde kliknout". Překresluje se jen při změně a ne pod prstem. */
  const sig = JSON.stringify(CHATS.map((c) => [c.id, c.title, c.updated, c.tab || '', c.exists, c.cwd]));
  if (sig === chatsSig) return;
  chatsSig = sig;
  renderChatsSoon();
}

/* Kdy se seznam chatů smí překreslit: ne, dokud na něm leží prst (myš), ať
   klepnutí nespadne do prázdna. Odložené překreslení doběhne po puštění. */
let chatsSig = '';
let chatsTouch = 0;               // kdy prst dosedl (0 = nikdo nesahá)
let chatsPending = false;
function renderChatsSoon() {
  // Pojistka: ztracené puštění nesmí seznam zmrazit napořád.
  if (chatsTouch && Date.now() - chatsTouch < 10000) { chatsPending = true; return; }
  chatsPending = false;
  renderChats();
}
function chatsTouchEnd() {
  if (!chatsTouch) return;
  // Click přijde až po puštění — překreslit o chvilku později.
  setTimeout(() => {
    chatsTouch = 0;
    if (chatsPending) renderChatsSoon();
  }, 400);
}

/* Celá historie: ve firmě panel ukazuje jen její chaty (a v osobním prostoru
   jen osobní). Kdo hledá starší konverzaci z druhého prostoru, rozbalí si
   všechny — volba se pamatuje. */
function chatsVse() {
  try { return localStorage.getItem('hub-chats-vse') === '1'; } catch (_) { return false; }
}
function setChatsVse(on) {
  try { localStorage.setItem('hub-chats-vse', on ? '1' : '0'); } catch (_) { /* bez úložiště */ }
  chatsShown = CHATS_PAGE;
  renderChats();
}
function prostorNazev(slug) {
  if (!slug) return 'Osobní';
  const f = firmyList().find((x) => x.zkratka === slug);
  return f ? (f.nazev || f.zkratka) : slug;
}

/* Patří tab k tomuhle chatu? Číslo konverzace se po obnově může změnit (nová kopie),
   proto se hledá podle všech tří: id tabu, aktuální konverzace i té, ze které tab vznikl. */
function tabPatriChatu(t, c) {
  return !!t && !t.exited && ((c.tab && t.id === c.tab) || (t.chat && t.chat === c.id) || (t.resume && t.resume === c.id));
}
const bezi = (c) => TABS.some((t) => tabPatriChatu(t, c));

/* Připnuté chaty: seznam id v prohlížeči (jen tohle zařízení), nahoře v panelu. */
const PRIPNUTE_KEY = 'hub.pripnute';
function pripnute() {
  try { const v = JSON.parse(localStorage.getItem(PRIPNUTE_KEY) || '[]'); return Array.isArray(v) ? v : []; } catch (_) { return []; }
}
function jePripnuty(id) { return pripnute().includes(id); }
function prepniPripnuti(id) {
  const v = pripnute();
  const i = v.indexOf(id);
  if (i >= 0) v.splice(i, 1); else v.unshift(id);
  try { localStorage.setItem(PRIPNUTE_KEY, JSON.stringify(v.slice(0, 50))); } catch (_) { /* nic */ }
  chatsSig = '';
  renderChats();
}

/* Okno se nikdy nesmí posunout do strany (fokus nebo scrollIntoView v širokém terminálu to umí): panel vlevo by zmizel. */
window.addEventListener('scroll', () => {
  const e = document.scrollingElement;
  if (e && (e.scrollLeft || e.scrollTop)) { e.scrollLeft = 0; e.scrollTop = 0; }
}, {passive: true});
for (const el of [document.documentElement, document.body]) {
  el.addEventListener('scroll', () => { if (el.scrollLeft) el.scrollLeft = 0; }, {passive: true});
}

function renderChats() {
  const box = $('chats');
  const words = foldText($('chats-search').value.trim()).split(/\s+/).filter(Boolean);
  // Jen chaty prostoru okna: ve firmě její, v osobním prostoru osobní —
  // pokud si člověk nerozbalil celou historii.
  const filtrovat = !!(STATE && STATE.config && hasFirma());
  const ted = prostor();
  const here = filtrovat ? CHATS.filter((c) => prostorCesty(c.cwd) === ted) : CHATS;
  const others = CHATS.length - here.length;
  const vse = filtrovat && others > 0 && chatsVse();
  const scope = vse ? CHATS : here;
  const list0 = words.length
    ? scope.filter((c) => {
      const hay = foldText([c.title, c.prompt, c.project].join(' '));
      return words.every((w) => hay.includes(w));
    })
    : scope;
  // Otevřený chat patří mezi dnešní a nahoru: má zelenou tečku a nemusí se hledat v „Otevřených“.
  const nyni = Date.now() / 1000;
  const uc = (c) => (bezi(c) ? Math.max(c.updated, nyni) : c.updated);
  const list = list0.slice().sort((a, b) => uc(b) - uc(a));
  $('chats-count').textContent = scope.length ? String(scope.length) : '';
  // Otevřený chat je v seznamu chatů (zelená tečka) — v panelu Otevřené zůstanou jen taby bez řádku v seznamu.
  for (const t of TABS) {
    if (!t.el) continue;
    const v = scope.some((c) => tabPatriChatu(t, c)) ? '1' : '0';
    if ((t.el.dataset.vseznamu || '') !== v) t.el.dataset.vseznamu = v;
  }
  box.textContent = '';
  const toggle = () => {
    if (!filtrovat || !others) return;
    const b = document.createElement('button');
    b.type = 'button';
    b.className = 'chats-toggle keep-drawer';
    if (vse) {
      b.textContent = 'Jen chaty tohoto prostoru';
    } else {
      b.innerHTML = '<span class="chats-toggle-main">Zobrazit celou historii</span><span class="chats-toggle-sub"></span>';
      b.querySelector('.chats-toggle-sub').textContent =
        `další ${others} ${others === 1 ? 'chat' : others < 5 ? 'chaty' : 'chatů'} z ${ted ? 'ostatních prostorů' : 'firem'}`;
    }
    b.onclick = (ev) => { ev.stopPropagation(); setChatsVse(!vse); };
    box.appendChild(b);
  };
  if (!list.length) {
    const note = document.createElement('div');
    note.className = 'empty';
    note.textContent = words.length ? 'Nic takového jsem nenašel.'
      : 'Zatím tu nejsou žádné chaty — začni tlačítkem Nový chat.';
    box.appendChild(note);
    toggle();
    return;
  }
  const now = new Date();
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime() / 1000;
  const groupOf = (t) => (t >= today ? 'Dnes'
    : t >= today - 86400 ? 'Včera'
      : t >= today - 6 * 86400 ? 'Posledních 7 dní'
        : t >= today - 29 * 86400 ? 'Posledních 30 dní'
          : new Date(t * 1000).toLocaleDateString('cs-CZ', {month: 'long', year: 'numeric'}));
  const buildItem = (c) => {
    const item = document.createElement('button');
    item.className = 'chat-item' + (c.tab || bezi(c) ? ' running' : '') + (c.exists ? '' : ' gone');
    item.dataset.id = c.id;
    item.innerHTML = '<span class="chat-title"></span><span class="chat-meta"></span>';
    const kde = vse ? prostorCesty(c.cwd) : ted;
    if (kde !== ted) {
      // Chat z jiného prostoru: štítek, ať je jasné, kam klepnutí přepne.
      const tag = document.createElement('span');
      tag.className = 'chat-prostor' + (kde ? ' firma' : '');
      tag.textContent = prostorNazev(kde);
      item.querySelector('.chat-title').appendChild(tag);
    }
    item.querySelector('.chat-title').appendChild(document.createTextNode(c.title));
    const at = new Date(c.updated * 1000);
    const when = c.updated >= today
      ? at.toLocaleTimeString('cs-CZ', {hour: '2-digit', minute: '2-digit'})
      : at.toLocaleDateString('cs-CZ', {day: 'numeric', month: 'numeric'});
    item.querySelector('.chat-meta').textContent =
      [c.project, when, c.tab ? 'otevřená' : ''].filter(Boolean).join('  ·  ');
    item.title = [c.prompt && c.prompt !== c.title ? c.prompt : '', c.cwd].filter(Boolean).join('\n\n');
    // Špendlík u řádku: připnout / odepnout (jen na tomhle zařízení).
    const pin = document.createElement('span');
    pin.className = 'chat-pin' + (jePripnuty(c.id) ? ' on' : '');
    pin.setAttribute('role', 'button');
    pin.title = jePripnuty(c.id) ? 'Odepnout' : 'Připnout nahoru';
    pin.innerHTML = '<svg class="ico"><use href="#i-star"/></svg>';
    pin.onclick = (ev) => { ev.stopPropagation(); prepniPripnuti(c.id); };
    item.appendChild(pin);
    item.onclick = () => openChat(c);
    item.oncontextmenu = (ev) => {
      ev.preventDefault();
      const items = [
        {icon: 'i-terminal', label: 'Pokračovat v tabu', run: () => openChat(c)},
        {icon: 'i-note', label: 'Jen přečíst', run: () => readChat(c)},
      ];
      const p = prenosMoznost();
      if (p) items.push({icon: 'i-up', label: p.label, run: () => prenosPoslat(c.id, c.title, true)});
      if (window.HubHledani) items.push({icon: 'i-save', label: 'Exportovat do Markdownu', run: () => HubHledani.exportovat(c)});
      items.unshift({icon: 'i-star', label: jePripnuty(c.id) ? 'Odepnout' : 'Připnout', run: () => prepniPripnuti(c.id)});
      items.push({icon: 'i-trash', label: 'Smazat', color: 'var(--red)', run: () => deleteChat(c)});
      showMenu(ev.clientX, ev.clientY, items);
    };
    return item;
  };
  // Připnuté chaty nahoře (bez hledání; při hledání se prochází všechno).
  const pripnuty = words.length ? [] : list.filter((c) => jePripnuty(c.id));
  if (pripnuty.length) {
    const head = document.createElement('div');
    head.className = 'group-head pripnute';
    head.textContent = 'Připnuté';
    box.appendChild(head);
    for (const c of pripnuty) box.appendChild(buildItem(c));
  }
  const ostatni = pripnuty.length ? list.filter((c) => !jePripnuty(c.id)) : list;
  let lastGroup = null;
  for (const c of ostatni.slice(0, chatsShown)) {
    const group = groupOf(uc(c));
    if (group !== lastGroup) {
      lastGroup = group;
      const head = document.createElement('div');
      head.className = 'group-head';
      head.textContent = group;
      box.appendChild(head);
    }
    box.appendChild(buildItem(c));
  }
  if (ostatni.length > chatsShown) {
    const zbyva = ostatni.length - chatsShown;
    const more = document.createElement('button');
    more.type = 'button';
    more.className = 'chats-more keep-drawer';
    more.textContent = zbyva > CHATS_PAGE ? `Zobrazit starší (dalších ${CHATS_PAGE} z ${zbyva})` : `Zobrazit starší (${zbyva})`;
    more.onclick = (ev) => { ev.stopPropagation(); chatsShown += CHATS_PAGE; renderChats(); };
    box.appendChild(more);
  }
  toggle();
}

/* Zavřít šuplík s panelem (telefon). Ne jen sundat třídu: mobile.js drží i
   ztmavení za šuplíkem a to by zůstalo — otevřený tab by byl šedý, dokud se
   neklepne jinam. */
function closeDrawer() {
  if (window.HubMobile) window.HubMobile.closeDrawer();
  else document.body.classList.remove('drawer-open');
}

/* Klepnutí na konverzaci ji otevře rovnou v tabu: nahoře je vidět, o čem se
   psalo (čtení z přepisu), dole se píše dál. Okno jen ke čtení bylo o krok
   navíc — kdo konverzaci otevírá, chce v ní většinou pokračovat. To okno
   zůstává v nabídce pravého tlačítka (dlouhého podržení) jako „Jen přečíst". */
function openChat(c) {
  const running = TABS.find((t) => tabPatriChatu(t, c));
  closeDrawer();
  if (running) return activate(running);
  resumeChat(c);
}

/* Smazat konverzaci z panelu Chaty. Server ji přesune do koše prostoru
   (~/.claude/hub-kos/chaty), takže omyl jde vrátit. Otevřenou v tabu nesmaže. */
async function deleteChat(c) {
  if (c.tab) return toast('Konverzace je otevřená v tabu — nejdřív ho zavři.');
  const ok = await askConfirm({
    title: 'Smazat konverzaci?',
    html: `<b>${escapeHtml(c.title)}</b> zmizí ze seznamu chatů. Přepis se přesune do koše prostoru.`,
    yes: 'Smazat', no: 'Zrušit'});
  if (!ok) return;
  try {
    await api('chat-delete', {id: c.id});
  } catch (err) {
    return toast('Nesmazáno: ' + err.message);
  }
  CHATS = CHATS.filter((x) => x.id !== c.id);
  chatsSig = '';
  renderChatsSoon();
  toast('Konverzace smazána.');
}

/* Konverzace jen ke čtení, bez spouštění Claude Code. */
function readChat(c) {
  closeDrawer();
  if (!window.HubCteni) return resumeChat(c);
  const at = new Date(c.updated * 1000);
  HubCteni.open({api, notice: toast, resume: resumeChat, imageUrl, openLink, copy: copyText, rozvetvit: rozvetvitChat},
                {...c, when: at.toLocaleString('cs-CZ')});
}

/* Rozvětvit konverzaci od zprávy: server (hub/vetev.py) vyrobí nový přepis jen
   do toho místa a hned se v něm pokračuje v novém tabu. `zdroj` = {id: tab}
   nebo {chat: id konverzace}; původní konverzace zůstává beze změny. */
let vetveniBezi = false;
async function rozvetvitChat(zdroj, uuid, zavrit) {
  if (vetveniBezi) return;               // dvojklik / rychlé opakování
  vetveniBezi = true;
  try {
    const r = await api('chat-vetev', {...zdroj, uuid});
    if (!r || !r.id) throw new Error('Server nevrátil novou konverzaci.');
    if (zavrit) zavrit();
    openTab({kind: 'project', path: r.cwd || (ACTIVE && ACTIVE.path) || '', title: (r.title || 'Větev').slice(0, 40),
             agent: 'claude', resume: r.id});
    closeDrawer();
    toast(r.znovu ? 'Větev už vznikla, otevřena znovu.' : 'Větev otevřena. Zprávu, od které jsi větvil, napiš znovu nebo uprav:\n„' +
          String(r.text || '').slice(0, 120) + '“');
    setTimeout(loadChats, 2000);
  } catch (err) {
    toast('Nerozvětveno: ' + (err && err.message ? err.message : err));
  } finally {
    vetveniBezi = false;
  }
}

/* Kdy hub sám zavřel (nebo mu skončil) tab s danou konverzací. Změna přepisu
   hned po zavření je jeho vlastní poslední zápis, ne důkaz, že konverzace běží
   jinde — jinak by se po zavření a rychlém znovuotevření nabízela „kopie“
   a seznam měl konverzaci dvakrát. */
const ZAVRENE_CHATY = new Map();
const PO_ZAVRENI_MS = 20000;         // tak dlouho po zavření Claude Code ještě dopisuje
function zapamatujZavreny(tab) {
  const id = tab && (tab.chat || tab.resume);
  if (id) ZAVRENE_CHATY.set(id, Date.now());
  if (ZAVRENE_CHATY.size > 200) ZAVRENE_CHATY.delete(ZAVRENE_CHATY.keys().next().value);
}
function psanoPoNasemZavreni(c) {
  const zavreno = ZAVRENE_CHATY.get(c.id);
  return !zavreno || c.updated * 1000 > zavreno + PO_ZAVRENI_MS;
}

/* Pokračovat v konverzaci: tohle už je nový tab s Claude Code. `prompt` je
   zpráva napsaná v okně se čtením — Claude na ní začne dělat hned po startu. */
async function resumeChat(c, prompt, preneseny) {
  if (!c.exists) {
    toast(c.cwd ? 'Složka téhle konverzace už není: ' + c.cwd
                : 'Tuhle konverzaci jsi poslal na počítač — pokračuj v ní tam.');
    return;
  }
  // Chat už je otevřený v tabu tohohle hubu: přepnout na něj, ne otevírat další
  // (druhý Claude v téže konverzaci, případně „kopie“ — v seznamu pak byl dvakrát).
  const otevreny = TABS.find((t) => tabPatriChatu(t, c));
  if (otevreny) {
    activate(otevreny);
    if (otevreny.uspany) send({t: 'wake', id: otevreny.id});
    closeDrawer();
    return;
  }
  // Změněná před chvílí a v žádném tabu hubu neběží: nejspíš je otevřená
  // jinde (terminál, jiné okno). Dva Claudy v jedné konverzaci by si
  // přepisovaly historii — otevře se proto její kopie.
  let fork = false;
  // Prave preneseny chat je cerstvy jen proto, ze jsme ho pred chvili zapsali - nikde jinde nebezi.
  if (!preneseny && Date.now() / 1000 - c.updated < 180 && psanoPoNasemZavreni(c)) {
    if (!(await HubDialog.confirm(`Konverzace „${c.title}" se změnila před chvílí — nejspíš ještě běží jinde.\n\n` +
                 'Otevřít její kopii? Původní konverzace zůstane, jak je.', {title: 'Konverzace možná běží jinde', ok: 'Otevřít kopii'}))) return;
    fork = true;
  }
  // Chat firmy (běží ve ~/firmy/<zkratka>/…) se vrátí i s firmou.
  const slug = prostorCesty(c.cwd);
  const f = slug ? firmyList().find((x) => x.zkratka === slug) : null;
  openTab({kind: 'project', path: c.cwd, title: c.title.slice(0, 40), agent: 'claude',
           resume: c.id, fork, prompt: prompt || '', vault: f ? firmaVaultId(f) : ''});
  closeDrawer();
  setTimeout(loadChats, 4000);
}

function initChats() {
  for (const b of document.querySelectorAll('.side-tab')) b.onclick = () => sidebarView(b.dataset.view);
  $('chats-search').addEventListener('input', () => { chatsShown = CHATS_PAGE; renderChats(); });
  if (window.HubHledani) HubHledani.mount({api, toast, openChat, token: TOKEN, chats: () => CHATS});
  const box = $('chats');
  box.addEventListener('pointerdown', () => { chatsTouch = Date.now(); });
  for (const ev of ['pointerup', 'pointercancel', 'pointerleave']) box.addEventListener(ev, chatsTouchEnd);
  // V panelu jsou jen chaty — projekty mají vlastní stránku (showProjekty).
  sidebarView('chats');
}

function renderMemory() {
  const mem = STATE.memory;
  $('memory-section').hidden = !mem.enabled;
  $('btn-brain').hidden = !mem.enabled;
  if (!mem.enabled) return;
  const c = mem.counts;
  $('memory-summary').innerHTML =
    `<span class="learnings">${icon('i-bulb')} ${c.learnings || 0}</span>
     <span class="errors">${icon('i-error')} ${c.errors || 0}</span>
     <span class="wins">${icon('i-star')} ${c.wins || 0}</span>
     <span class="mem-auto"></span>`;
  $('memory-summary').querySelector('.mem-auto').textContent = autosaveLine();
  const box = $('memory');
  box.textContent = '';
  if (!mem.recent.length) {
    box.innerHTML = '<div class="empty">(zatím prázdné)</div>';
    return;
  }
  const symbol = {learnings: 'i-bulb', errors: 'i-error', wins: 'i-star'};
  for (const note of mem.recent) {
    const el = document.createElement('button');
    el.className = `mem-card ${note.kind}`;
    el.innerHTML = icon(symbol[note.kind] || 'i-bulb') + '<span></span>';
    el.querySelector('span').textContent = note.title;
    el.title = note.file;
    el.onclick = () => (vaultPreview() ? openVault('memory/' + note.file)
                                       : openExternal('', 'note', note.file));
    box.appendChild(el);
  }
}

// `dev: true` = nasazení a GitHub. Ukáže se až ve vývojářském režimu; komu
// hub slouží na psaní s agentem, tomu deploy ani push nemá co nabízet.
// Uložit do paměti a poznámka k projektu tu nejsou schválně: dělá je hook
// memory-autosave.py sám, když session ztichne nebo se tab zavře.
const ACTIONS = [
  {skill: 'deploy', label: 'Deploy', icon: 'i-deploy', cmd: '/deploy\r', dev: true},
  {skill: 'push', label: 'Push na GitHub', icon: 'i-push', cmd: '/push\r', dev: true},
  {skill: 'status', label: 'Přehled projektů', icon: 'i-status', cmd: '/status\r', dev: true},
  {skill: 'screenshot', label: 'Snímek webu…', icon: 'i-image', cmd: '/screenshot '},
];

function devMode() {
  return !!(STATE.config && STATE.config.dev_mode);
}

/* Rychlé akce jsou slash příkazy — dávají smysl jen v tabu s agentem, který
   naše skilly čte. V holém shellu by /status skončil v bashi. */
function showsActions(tab) {
  if (!tab || !(tab.kind === 'project' || tab.kind.startsWith('slash:'))) return false;
  const a = agentById(tab.agent || STATE.default_agent);
  return !!(a && a.skills) && $('actions').childElementCount > 0;
}

/* Panel se skládá ze dvou nezávislých částí: rychlé akce potřebují agenta se
   skilly, stopky jen otevřenou složku. Ukáže se, když je aspoň jedna z nich.
   Stopky se načítají ze sítě, takže po dokreslení ohlásí `clockify-drawn`
   a viditelnost se dopočítá znovu — jinak by panel zůstal schovaný. */
function syncActionbar(tab) {
  const path = tab && tab.kind === 'project' ? (tab.path || '') : '';
  if (path && window.HubClockify) HubClockify.panel(hubIO(), path);
  else $('clockify').hidden = true;
  showActionbar(tab);
}

/* Jen viditelnost, nic nenačítá — tohle volá i `clockify-drawn`, takže kdyby
   sáhlo zpátky na panel(), točilo by se to pořád dokola. */
function showActionbar(tab) {
  const prehled = renderPrehled(tab);
  const acts = showsActions(tab);
  $('actions').hidden = !acts;
  $('actionbar').querySelector('.barlabel').hidden = !acts;
  const clock = !$('clockify').hidden;
  $('clockify-label').hidden = !clock;
  $('actionbar').hidden = !(acts || clock || prehled);
}

let dulFiltr = '';                       // barva, podle které se poznámky v Průběhu filtrují
let dulRazeni = 'cas';                   // cas | barva
try { dulRazeni = localStorage.getItem('hub-dul-razeni') === 'barva' ? 'barva' : 'cas'; } catch (_) { /* nic */ }

/* ── Přenos chatu mezi počítačem a serverem (hub/prenos.py) ──────────────────
   Z počítače na server a ze serveru na počítač: chat se zabalí, pošle a na
   druhé straně se dá otevřít a pokračovat v něm tam, kde skončil. */
function prenosMoznost() {
  const cfg = (STATE && STATE.config) || {};
  if (onServer()) return {smer: 'na-pocitac', label: 'Poslat na počítač', pokracovat: 'Pokračovat na počítači'};
  if (cfg.gw_server && cfg.gw_logged_in) {
    return {smer: 'na-server', label: 'Poslat na server', pokracovat: 'Pokračovat na serveru'};
  }
  return null;
}

/* Okénko vpravo nahoře s tlačítky (stejný vzhled jako „Na serveru je nová verze"). */
function prenosKarta({titulek, text, tlacitka}) {
  const card = document.createElement('div');
  card.className = 'srvupd prenos-karta';
  card.innerHTML = `
    <div class="srvupd-ico"><svg class="ico"><use href="#i-up"/></svg></div>
    <div class="srvupd-body"><div class="srvupd-title"></div><div class="srvupd-text"></div>
      <div class="srvupd-btns"></div></div>`;
  card.querySelector('.srvupd-title').textContent = titulek;
  card.querySelector('.srvupd-text').textContent = text || '';
  const zavri = () => card.remove();
  const box = card.querySelector('.srvupd-btns');
  for (const t of tlacitka) {
    const b = document.createElement('button');
    b.className = 'btn ' + (t.primary ? 'primary' : 'ghost');
    b.textContent = t.label;
    b.onclick = async () => { b.disabled = true; try { await t.run(); } finally { zavri(); } };
    box.appendChild(b);
  }
  // Víc karet najednou se neskládá přes sebe.
  card.style.top = (54 + document.querySelectorAll('.prenos-karta').length * 150) + 'px';
  document.body.appendChild(card);
  return card;
}

async function prenosApi(body) {
  const data = await api('prenos', body);
  if (data.ok === false) throw new Error(data.error || 'Přenos se nepovedl.');
  return data;
}

/* Otevře přenesený chat v tabu (po přijetí je v seznamu chatů jako každý jiný). */
async function otevriPrenesenyChat(id) {
  await loadChats();
  const c = CHATS.find((x) => x.id === id);
  if (!c) return toast('Chat se v seznamu ještě neukázal — zkus to za chvilku.');
  resumeChat(c, '', true);
}

async function prenosPoslat(chat, titulek, jenPoslat) {
  const p = prenosMoznost();
  if (!p) return toast('Přenos je jen mezi počítačem a serverem — přihlas se k serveru v Nastavení → Účet.');
  if (!chat) return toast('Tenhle chat se ještě nedá přenést — pošli v něm aspoň jednu zprávu.');
  toast(p.smer === 'na-server' ? 'Posílám chat na server…' : 'Posílám chat na počítač…');
  let res;
  try {
    res = await prenosApi({akce: p.smer, chat, titulek});
  } catch (err) {
    return toast(err.message);
  }
  if (p.smer === 'na-server') {
    const tab = TABS.find((t) => (t.chat || t.resume) === chat);
    if (tab) closeTab(tab);
    loadChats();
    prenosKarta({titulek: 'Chat je na serveru', text: `„${res.title}“ — otevři ho na serveru a pokračuj tam.`,
      tlacitka: [{label: 'Otevřít na serveru', primary: true, run: () => HubServer.go(hubIO())},
                 {label: 'Zavřít', run: async () => {}}]});
  } else {
    toast(res.message || 'Chat čeká na počítači.');
    // Chat se stěhuje: tab na serveru se zavře a chat zmizí ze seznamu.
    const tab = TABS.find((t) => (t.chat || t.resume) === chat);
    if (tab) closeTab(tab);
    loadChats();
  }
}

let prenosZnamo = new Set();
let prenosTimer = null;
async function prenosPoll() {
  let st;
  try { st = await api('prenos-stav'); } catch (_) { return; }
  for (const c of st.cekajici || []) {
    if (prenosZnamo.has(c.id)) continue;
    prenosZnamo.add(c.id);
    prenosKarta({titulek: 'Chat z počítače je tady', text: `„${c.title}“${c.from ? ' (z ' + c.from + ')' : ''} — pokračuj v něm.`,
      tlacitka: [{label: 'Otevřít', primary: true, run: async () => { await otevriPrenesenyChat(c.id); await prenosApi({akce: 'hotovo', chat: c.id}).catch(() => {}); }},
                 {label: 'Později', run: async () => { await prenosApi({akce: 'hotovo', chat: c.id}).catch(() => {}); }}]});
  }
  for (const u of st.ukoly || []) {
    if (prenosZnamo.has('u' + u.id)) continue;
    prenosZnamo.add('u' + u.id);
    prenosKarta({titulek: 'Server ti poslal chat', text: `„${u.title}“ — přijmout ho na tenhle počítač a pokračovat?`,
      tlacitka: [{label: 'Přijmout a otevřít', primary: true, run: async () => {
        try { const r = await prenosApi({akce: 'prijmout', id: u.id}); await otevriPrenesenyChat(r.chat); } catch (err) { toast(err.message); }
      }}, {label: 'Zahodit', run: async () => { await api('pocitac', {action: 'ukol-zahodit', id: u.id}).catch(() => {}); }}]});
  }
}

/* ── Sdílené chaty (gateway/relace.py) ──────────────────────────────────────
   Majitel sdílí rozběhnutý chat s kolegy; ti ho živě sledují (a když smí, píšou
   do něj). Jen na serveru — na počítači nemá kdo se dívat. */
let RELACE = {moje: [], semnou: [], lide: [], skupiny: [], ulozene: []};
let relaceTimer = null;
const relaceZnama = new Set();

async function relacePost(body) {
  const r = await fetch('/gw/relace', {
    method: 'POST', credentials: 'same-origin',
    headers: {'Content-Type': 'application/json', 'X-Hub-Account': '1'},
    body: JSON.stringify(body),
  });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || 'Server teď neodpovídá, zkus to za chvíli.');
  return data;
}

async function loadRelace() {
  if (!onServer()) return;
  try {
    const r = await fetch('/gw/relace', {credentials: 'same-origin'});
    if (!r.ok) return;                 // starší brána — přijde s aktualizací
    RELACE = await r.json();
  } catch (_) { return; }
  sharedPeople = RELACE.lide || sharedPeople;
  // Někdo mi něco nasdílel — řekne se to jednou.
  for (const s of RELACE.semnou) {
    if (!relaceZnama.has(s.id)) {
      if (relaceZnama.size || relaceHotovo) toast(`${s.majitel} s tebou sdílí chat „${s.titulek}“.`);
      relaceZnama.add(s.id);
    }
  }
  relaceHotovo = true;
  renderRelace();
  if (ACTIVE) renderPrehled(ACTIVE);
}
let relaceHotovo = false;

/* Majitel se v sdíleném chatu hlásí taky (kdo se dívá, kdo píše) — jednou za pár
   sekund, jen z aktivního tabu a ne když je okno skryté. „Píše“ = úhoz v posledních 4 s. */
let relacePritTimer = null;
function relacePritomnost() {
  if (!onServer() || document.hidden || !ACTIVE) return;
  const rel = relaceTabu(ACTIVE);
  if (!rel) return;
  const pise = Date.now() - (ACTIVE.pisuOd || 0) < 4000;
  relaceGw('/pritomnost', {id: rel.id, pise}).then((r) => {
    const a = JSON.stringify([rel.diva, rel.pisou]);
    rel.diva = r.diva || []; rel.pisou = r.pisou || [];
    if (JSON.stringify([rel.diva, rel.pisou]) !== a && ACTIVE) renderPrehled(ACTIVE);
  }).catch(() => {});
}

function relaceTabu(tab) {
  const chat = tab && (tab.chat || tab.resume);
  return chat ? RELACE.moje.find((r) => r.chat === chat) : null;
}

/* Sdílení aktuálního chatu: výběr lidí, u každého zvlášť „smí psát". */
async function sdiletChat(tab) {
  const chat = tab.chat || tab.resume;
  if (!chat) return toast('Tenhle chat se ještě nedá sdílet — pošli v něm aspoň jednu zprávu.');
  if (!sharedPeople.length) await loadRelace();
  const now = relaceTabu(tab);
  const got = await peopleDialog({
    title: now ? 'Sdílení chatu' : 'Sdílet chat', ok: now ? 'Uložit' : 'Sdílet',
    note: 'Kolegové ho uvidí živě — co píšete, nástroje i kód, který Claude napíše.',
    hint: 'S kým chat sdílíš (číst smí vybraní, psát jen ti s „smí psát“):',
    withWrite: true,
    chosen: now ? now.clenove.map((c) => c.email) : [],
    chosenWrite: now ? now.clenove.filter((c) => c.pise).map((c) => c.email) : [],
    groups: RELACE.skupiny || [],
    chosenGroups: now ? (now.skupiny || []).map((g) => g.id) : [],
    chosenGroupsWrite: now ? (now.skupiny || []).filter((g) => g.pise).map((g) => g.id) : [],
  });
  if (!got) return;
  try {
    const titulek = chatTitulek(tab);
    const res = await relacePost({akce: 'zalozit', chat, titulek,
                                  emaily: got.emails, psat: got.psat,
                                  skupiny: got.skupiny, skupiny_psat: got.skupiny_psat});
    toast(res.message || 'Chat je sdílený.');
  } catch (err) { toast(err.message); }
  loadRelace();
}

// Chat se jmenuje „Chat“ — v seznamu kolegy by nic neřekl, tak první zadání.
function chatTitulek(tab) {
  const zadani = tab.cteni && tab.cteni.prehled && (tab.cteni.prehled().zadani || [])[0];
  return (tab.title && !TITULKY_BEZ_JMENA.test(tab.title)) ? tab.title
    : (zadani && zadani.text ? zadani.text.slice(0, 60) : 'Chat');
}

/* Uložit chat do podkategorie firmy (gateway/relace.py): kopie, na kterou
   kdokoli s přístupem naváže přes „Pokračovat u sebe“ — i když tu autor
   už nebude. Jen na pokyn, nic se neukládá samo. */
async function ulozitChat(tab) {
  const chat = tab.chat || tab.resume;
  if (!chat) return toast('Tenhle chat se ještě nedá uložit — pošli v něm aspoň jednu zprávu.');
  if (!(RELACE.skupiny || []).length) await loadRelace();
  if (!(RELACE.skupiny || []).length) return toast('Nevidíš žádnou podkategorii firmy, kam by šel chat uložit.');
  const got = await peopleDialog({
    title: 'Uložit chat do podkategorie', ok: 'Uložit', people: [],
    note: 'Kopie chatu, jak je teď. Kdo do podkategorie vidí, v ní může pokračovat u sebe.',
    hint: 'Kam chat uložit:', groups: RELACE.skupiny,
  });
  if (!got) return;
  try {
    const res = await relacePost({akce: 'ulozit', chat, titulek: chatTitulek(tab), skupiny: got.skupiny});
    toast(res.message || 'Chat je uložený.');
  } catch (err) { toast(err.message); }
  loadRelace();
}

async function pokracovatUlozeny(u) {
  toast('Kopíruju chat k tobě…');
  try {
    const r = await relaceGw('/pokracovat', {ulozeny: u.id});
    if (!r || !r.chat) throw new Error('Chat se nepodařilo zkopírovat.');
    await otevriPrenesenyChat(r.chat);
    await prenosApi({akce: 'hotovo', chat: r.chat}).catch(() => {});
    toast('Chat je u tebe — pokračuješ v něm sám.');
  } catch (err) { toast(err.message); }
}

async function zrusitSdileni(rel) {
  if (!(await HubDialog.confirm('Přestat chat sdílet? Kolegové ho přestanou vidět.', {title: 'Přestat sdílet', ok: 'Přestat sdílet'}))) return;
  try { await relacePost({akce: 'zrusit', id: rel.id}); } catch (err) { toast(err.message); }
  loadRelace();
}

/* Sdílené chaty v panelu vlevo: totéž co taby nahoře, jen se z nich dá vrátit
   chat, který sis zavřel. Klepnutí otevře (nebo přepne na) tab. */
function renderRelace() {
  const box = $('relace-section');
  syncSdileneTaby();
  paintSdileni();
  if (!box) return;
  const list = $('relace-list');
  list.textContent = '';
  // Uložené a podkategorii sdílené chaty jen v prostoru své firmy.
  const ulozene = (RELACE.ulozene || []).filter((u) => !u.firma || u.firma === prostor());
  const semnou = RELACE.semnou.filter(sdilenyVProstoru);
  box.hidden = !onServer() || (!semnou.length && !ulozene.length);
  for (const u of ulozene) {
    const row = document.createElement('button');
    row.className = 'chat-item relace-row firma';
    const t = document.createElement('span');
    t.className = 'chat-title';
    t.textContent = u.titulek;
    const m = document.createElement('span');
    m.className = 'chat-meta';
    m.textContent = 'Uloženo v ' + u.skupina + ' · ' + (u.moje ? 'ty' : u.autor) + ' · klepni a pokračuj u sebe';
    row.append(t, m);
    row.title = 'Zkopíruje chat do tvého prostoru a otevře ho — Claude v něm naváže, kde skončil.';
    row.onclick = () => {
      pokracovatUlozeny(u);
      if (window.HubMobile) HubMobile.closeDrawer();
    };
    list.appendChild(row);
  }
  for (const s of semnou) {
    const row = document.createElement('button');
    row.className = 'chat-item relace-row';
    const t = document.createElement('span');
    t.className = 'chat-title';
    t.textContent = s.titulek;
    const m = document.createElement('span');
    m.className = 'chat-meta';
    m.textContent = 'Sdílí ' + s.majitel + (s.role === 'pise' ? ' · smíš psát' : ' · jen čtení') +
      (s.diva.length ? ' · dívá se ' + s.diva.join(', ') : '');
    row.append(t, m);
    row.onclick = () => {
      otevriSdilenyTab(s, true);
      if (window.HubMobile) HubMobile.closeDrawer();
    };
    list.appendChild(row);
  }
}

/* ── Sdílené chaty jako taby ────────────────────────────────────────────────
   Chat, který ti někdo sdílí, je v liště tabů jako každý jiný — se štítkem
   „Sdílené“ a jménem toho, kdo ti ho sdílí. Otevře se v tabu (ne v okně nad
   stránkou), čte se živě přes bránu a kdo smí, píše. Zavřený tab se dá vrátit
   z panelu vlevo; zrušené sdílení tab samo odstraní. */
const SDILENE_TABY = new Map();       // id relace → {id, share, el, pane, viewer}
let relaceZavrene = new Set();
try { relaceZavrene = new Set(JSON.parse(localStorage.getItem('hub-relace-zavrene') || '[]')); } catch (_) { /* nic */ }
function ulozZavrene() {
  try { localStorage.setItem('hub-relace-zavrene', JSON.stringify([...relaceZavrene].slice(-200))); } catch (_) { /* soukromé okno */ }
}

function popisSdilenyTab(st) {
  const s = st.share;
  st.el.querySelector('.tab-stitek').textContent = 'Sdílené · ' + s.majitel;
  st.el.querySelector('.tab-title').textContent = s.titulek;
  st.el.dataset.stitek = 'Sdílené · ' + s.majitel;
  st.el.title = `${s.titulek}\nSdílí ${s.majitel} — ` + (s.role === 'pise' ? 'smíš psát' : 'jen čtení');
}

/* Sdílený chat patří do prostoru okna? Sdílený podkategorii (projektu)
   firmy jen do prostoru té firmy; jmenovitě sdílený do všech. */
function sdilenyVProstoru(s) {
  const firmy = (s && s.skupiny || []).map((g) => String(g.id || '').split(':')[0]);
  return !firmy.length || firmy.includes(prostor());
}
function paintSdileneProstor() {
  for (const st of SDILENE_TABY.values()) {
    st.el.hidden = !sdilenyVProstoru(st.share);
    // Otevřený sdílený chat jiného prostoru se zavře do úvodní stránky.
    if (st.el.hidden && st.el.classList.contains('active')) {
      st.el.classList.remove('active');
      st.pane.classList.remove('active');
      if (!ACTIVE) $('welcome').hidden = false;
    }
  }
}

function otevriSdilenyTab(s, aktivovat) {
  relaceZavrene.delete(s.id);
  ulozZavrene();
  let st = SDILENE_TABY.get(s.id);
  if (!st) {
    const pane = document.createElement('div');
    pane.className = 'pane relace-tab';
    $('panes').appendChild(pane);
    const el = document.createElement('button');
    el.className = 'tab tab-sdileny';
    el.innerHTML = '<span class="tab-stitek"></span><span class="tab-title"></span>' +
                   `<span class="tab-close" title="Zavřít tab">${icon('i-close')}</span>`;
    st = {id: s.id, share: s, el, pane, viewer: null};
    el.onclick = (ev) => {
      if (ev.target.closest('.tab-close')) { zavriSdilenyTab(st, true); return; }
      aktivujSdileny(st);
    };
    $('tabbar').insertBefore(el, $('btn-new-agent'));
    SDILENE_TABY.set(s.id, st);
    // Nový tab (ne ten při prvním načtení) svítí, dokud se na něj nepodíváš.
    if (relaceHotovo) el.classList.add('hotovo');
  }
  st.share = s;
  st.el.hidden = !sdilenyVProstoru(s);
  popisSdilenyTab(st);
  if (aktivovat) aktivujSdileny(st);
  return st;
}

function aktivujSdileny(st) {
  ACTIVE = null;
  showProjekty(false);
  for (const t of TABS) { t.el.classList.remove('active'); t.pane.classList.remove('active'); }
  for (const o of SDILENE_TABY.values()) {
    o.el.classList.toggle('active', o === st);
    o.pane.classList.toggle('active', o === st);
  }
  $('welcome').hidden = true;
  st.el.classList.remove('hotovo');
  document.dispatchEvent(new CustomEvent('hub-tab', {detail: {id: 'relace:' + st.id, title: st.share.titulek, path: ''}}));
  syncActionbar(null);
  if (!st.viewer) {
    st.viewer = HubCteni.openSdilene({
      host: st.pane, gw: relaceGw, notice: toast, imageUrl, openLink, copy: copyText,
      // Majitel chat zavřel nebo mu prostor spí: znovu ho zapne a otevře.
      zapnout: async (share) => {
        const r = await relaceGw('/zapnout', {id: share.id});
        toast(r && r.otevreno ? 'Chat je znovu zapnutý u ' + share.majitel + '.' : 'Chat u ' + share.majitel + ' už běží.');
      },
      // Kopie sdíleného chatu do mého prostoru: pokračuju v něm sám, majitelův zůstává.
      pokracovat: async (share) => {
        const r = await relaceGw('/pokracovat', {id: share.id});
        if (!r || !r.chat) throw new Error('Chat se nepodařilo zkopírovat.');
        await otevriPrenesenyChat(r.chat);
        await prenosApi({akce: 'hotovo', chat: r.chat}).catch(() => {});
        toast('Chat je u tebe. Pokračuješ v něm sám, ' + share.majitel + ' ho má dál po svém.');
      },
    }, st.share);
  } else st.viewer.wake();
}

function zavriSdilenyTab(st, zapamatovat) {
  if (st.viewer) st.viewer.close();
  const bylAktivni = st.el.classList.contains('active');
  st.el.remove();
  st.pane.remove();
  SDILENE_TABY.delete(st.id);
  if (zapamatovat) { relaceZavrene.add(st.id); ulozZavrene(); }
  if (bylAktivni) {
    const dalsi = TABS[TABS.length - 1];
    if (dalsi) activate(dalsi); else activate(null);
  }
}

/* Podle seznamu z brány: nové sdílení přidá tab, zrušené ho odstraní. */
function syncSdileneTaby() {
  if (!onServer()) return;
  const ids = new Set(RELACE.semnou.map((s) => s.id));
  for (const st of [...SDILENE_TABY.values()]) if (!ids.has(st.id)) zavriSdilenyTab(st, false);
  for (const s of RELACE.semnou) {
    if (relaceZavrene.has(s.id)) continue;
    otevriSdilenyTab(s, false);
  }
}

/* U tabu, který sdílím já, je vidět s kolika lidmi — a jde na to kliknout. */
function paintSdileni() {
  for (const t of TABS) {
    const b = t.el && t.el.querySelector('.tab-sdil');
    if (!b) continue;
    const rel = onServer() ? relaceTabu(t) : null;
    b.hidden = !rel;
    if (rel) {
      b.querySelector('span').textContent = String(rel.clenove.length);
      b.title = 'Sdílíš s: ' + rel.clenove.map((c) => c.name || c.email).join(', ') + ' — klepni a uprav';
    }
  }
  syncShareBtn();
}

/* Tlačítko „Sdílet“ v liště tabů: vidět na každém chatu, který se dá sdílet. */
function syncShareBtn() {
  const b = $('btn-sdilet');
  if (!b) return;
  const ok = onServer() && ACTIVE && (ACTIVE.chat || ACTIVE.resume) && ACTIVE.kind !== 'shell';
  b.hidden = !ok;
  if (!ok) return;
  const rel = relaceTabu(ACTIVE);
  b.classList.toggle('on', !!rel);
  b.querySelector('span').textContent = rel ? 'Sdíleno' : 'Sdílet';
}

/* Okno se sdíleným chatem čte a píše přes bránu (ne přes vlastní hub). */
async function relaceGw(path, body) {
  const r = await fetch('/gw/relace' + path, body === undefined ? {credentials: 'same-origin'} : {
    method: 'POST', credentials: 'same-origin',
    headers: {'Content-Type': 'application/json', 'X-Hub-Account': '1'},
    body: JSON.stringify(body),
  });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || 'Server teď neodpovídá, zkus to za chvíli.');
  return data;
}

/* ── Průběh ─────────────────────────────────────────────────────────────────
   Panel vpravo jako v appce Claude (Cowork): na čem Claude v tomhle chatu
   dělá. Nahoře Claudův seznam úkolů, a když ho nevede, kroky práce od
   poslední zprávy; pod tím pomocníci, úlohy na pozadí a upravené soubory.
   Data sbírá čtení (cteni.js prehled), tady se jen kreslí — vpravo na
   počítači, na telefonu v okně z nabídky ⋯. */
function prehledHtml(tab, box) {
  const p = tab && tab.cteni && tab.cteni.prehled ? tab.cteni.prehled() : null;
  box.textContent = '';
  if (!p) return false;
  const mk = (tag, cls, text) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined) n.textContent = text;
    return n;
  };
  const stavIco = (stav) => mk('span', 'pr-ico ' + stav);
  const sekce = (nadpis, pocet) => {
    const s = mk('div', 'pr-sekce');
    const h = mk('div', 'pr-hlava', nadpis);
    if (pocet) h.appendChild(mk('span', 'pr-pocet', pocet));
    s.appendChild(h);
    box.appendChild(s);
    return s;
  };
  let neco = false;

  if (tab.ceka) {
    const c = mk('button', 'pr-ceka', 'Claude čeká na tvou odpověď');
    c.onclick = () => activate(tab);
    box.appendChild(c);
    neco = true;
  }
  const zadani = p.zadani[p.zadani.length - 1];
  if (zadani) {
    const s = sekce('Úkol');
    const r = mk(zadani.row ? 'button' : 'div', 'pr-radek pr-zadani');
    r.append(stavIco(tab.ceka ? 'ceka' : zadani.stav), mk('span', 'pr-text', zadani.text));
    if (zadani.row) { r.title = 'Ukázat zprávu'; r.onclick = () => p.dulSkoc(zadani.row); }
    s.appendChild(r);
    neco = true;
  }
  /* Poznámky: pravý klik na zprávu v konverzaci ji sem přidá s barvou, pravý
     klik na poznámku tady barvu změní nebo ji odebere, klik odroluje ke zprávě.
     Nahoře se dá filtrovat a řadit podle barev; vlastní poznámku napíšeš přímo. */
  if (p.dulezite && (p.dulezite.length || zadani)) {
    const s = sekce('Poznámky', p.dulezite.length ? p.dulezite.length + '' : '');
    const pridat = mk('button', 'pr-dul-plus');
    pridat.innerHTML = icon('i-plus');
    pridat.title = 'Přidat vlastní poznámku';
    s.firstChild.appendChild(pridat);
    const pole = mk('input', 'pr-dul-pole');
    pole.type = 'text';
    pole.placeholder = 'Nová poznámka — Enter uloží';
    pole.hidden = true;
    pridat.onclick = () => { pole.hidden = !pole.hidden; if (!pole.hidden) pole.focus(); };
    pole.onkeydown = (ev) => {
      ev.stopPropagation();
      if (ev.key === 'Enter' && pole.value.trim()) { p.dulNova(pole.value, dulFiltr || undefined); pole.value = ''; }
      if (ev.key === 'Escape') pole.hidden = true;
    };
    s.appendChild(pole);
    if (!p.dulezite.length) {
      s.appendChild(mk('div', 'pr-napoveda', 'Klikni pravým tlačítkem na zprávu v konverzaci, vyber barvu — a je tady.'));
    }
    const pouzite = p.barvy.filter((b) => p.dulezite.some((d) => d.barva === b));
    if (dulFiltr && !pouzite.includes(dulFiltr)) dulFiltr = '';
    if (p.dulezite.length > 1 && pouzite.length) {
      const lista = mk('div', 'pr-dul-lista');
      const vse = mk('button', 'pr-dul-chip' + (dulFiltr ? '' : ' on'), 'Vše');
      vse.onclick = () => { dulFiltr = ''; renderPrehled(tab); };
      lista.appendChild(vse);
      for (const b of pouzite) {
        const c = mk('button', 'pr-dul-chip pr-dul-barva' + (dulFiltr === b ? ' on' : ''));
        c.style.setProperty('--dul', b);
        c.title = (p.jmena[b] || b) + ' (' + p.dulezite.filter((d) => d.barva === b).length + ')';
        c.onclick = () => { dulFiltr = dulFiltr === b ? '' : b; renderPrehled(tab); };
        lista.appendChild(c);
      }
      const razeni = mk('button', 'pr-dul-razeni', dulRazeni === 'barva' ? 'podle barvy' : 'podle času');
      razeni.title = 'Přepnout řazení';
      razeni.onclick = () => {
        dulRazeni = dulRazeni === 'barva' ? 'cas' : 'barva';
        try { localStorage.setItem('hub-dul-razeni', dulRazeni); } catch (_) { /* soukromé okno */ }
        renderPrehled(tab);
      };
      lista.appendChild(razeni);
      s.appendChild(lista);
    }
    let seznam = p.dulezite.filter((d) => !dulFiltr || d.barva === dulFiltr);
    if (dulRazeni === 'barva') {
      const poradi = (d) => p.barvy.indexOf(d.barva);
      seznam = seznam.map((d, i) => [d, i]).sort((x, y) => poradi(x[0]) - poradi(y[0]) || x[1] - y[1]).map((x) => x[0]);
    }
    for (const d of seznam) {
      const r = mk('div', 'pr-radek pr-dul');
      r.style.setProperty('--dul', d.barva);
      const tecka = mk('button', 'pr-tecka');
      tecka.style.background = d.barva;
      tecka.title = 'Změnit barvu';
      const menu = (ev, kotva) => {
        ev.preventDefault();
        ev.stopPropagation();
        HubCteni.paleta(kotva, d.barva, (b) => p.dulBarva(d.k, b), () => p.dulOdeber(d.k));
      };
      tecka.onclick = (ev) => menu(ev, tecka);
      const txt = mk('button', 'pr-dul-text', d.text);
      txt.title = d.row ? 'Ukázat zprávu · pravým tlačítkem barva' : 'Pravým tlačítkem barva';
      txt.onclick = () => { if (d.row) p.dulSkoc(d.row); };
      const x = mk('button', 'pr-dul-x', '×');
      x.title = 'Odebrat poznámku';
      x.onclick = () => p.dulOdeber(d.k);
      r.oncontextmenu = (ev) => menu(ev, {getBoundingClientRect: () => ({left: ev.clientX, bottom: ev.clientY})});
      r.append(tecka, txt, x);
      s.appendChild(r);
    }
    neco = true;
  }
  /* Sdílení chatu s kolegy (jen na serveru): kdo ho vidí, kdo smí psát a kdo
     se právě dívá. */
  if (onServer() && (tab.chat || tab.resume)) {
    const rel = relaceTabu(tab);
    const s = sekce('Sdílení', rel ? String(rel.clenove.length + (rel.skupiny || []).length) : '');
    if (!rel) {
      s.appendChild(mk('div', 'pr-napoveda', 'Kolega uvidí tenhle chat živě — i kód, který Claude píše.'));
      const b = mk('button', 'btn ghost pr-sdilet', 'Sdílet chat…');
      b.onclick = () => sdiletChat(tab);
      s.appendChild(b);
      if ((RELACE.skupiny || []).length) {
        const u = mk('button', 'btn ghost pr-sdilet', 'Uložit do podkategorie…');
        u.title = 'Kopie chatu do podkategorie firmy — kdo tam vidí, v ní může pokračovat.';
        u.onclick = () => ulozitChat(tab);
        s.appendChild(u);
      }
    } else {
      // U každého člena: je tu (zelená tečka) / píše teď (pulzuje) / není.
      for (const c of rel.clenove) {
        const jm = c.name || c.email;
        const pise = (rel.pisou || []).includes(jm), tu = pise || (rel.diva || []).includes(jm);
        const r = mk('div', 'pr-radek pr-clen');
        r.append(mk('span', 'pr-tecka' + (pise ? ' pise' : tu ? ' tu' : '')),
                 mk('span', 'pr-text', jm),
                 mk('span', 'pr-druh', pise ? 'píše…' : tu ? 'je tu' : (c.pise ? 'smí psát' : 'čte')));
        r.title = pise ? jm + ' právě píše' : tu ? jm + ' se právě dívá' : jm + ' tu teď není';
        s.appendChild(r);
      }
      // Podkategorie firem: vidí každý, kdo do ní vidí.
      for (const g of rel.skupiny || []) {
        const r = mk('div', 'pr-radek pr-clen');
        r.append(mk('span', 'pr-tecka'), mk('span', 'pr-text', g.nazev),
                 mk('span', 'pr-druh', g.pise ? 'smí psát' : 'čte'));
        r.title = 'Podkategorie firmy — chat vidí každý, kdo do ní vidí';
        s.appendChild(r);
      }
      const btns = mk('div', 'pr-sdileni-btns');
      const e = mk('button', 'btn ghost pr-sdilet', 'Upravit…');
      e.onclick = () => sdiletChat(tab);
      const z = mk('button', 'btn ghost pr-sdilet', 'Přestat sdílet');
      z.onclick = () => zrusitSdileni(rel);
      btns.append(e, z);
      if ((RELACE.skupiny || []).length) {
        const u = mk('button', 'btn ghost pr-sdilet', 'Uložit do podkategorie…');
        u.onclick = () => ulozitChat(tab);
        btns.append(u);
      }
      s.appendChild(btns);
    }
    neco = true;
  }
  /* Přenos chatu na druhou stranu: počítač ↔ server (hub/prenos.py). */
  const prenos = prenosMoznost();
  if (prenos && (tab.chat || tab.resume)) {
    const s = sekce('Přenos');
    s.appendChild(mk('div', 'pr-napoveda', prenos.smer === 'na-server'
      ? 'Pošli tenhle chat na server a pokračuj v něm tam.'
      : 'Pošli tenhle chat na počítač a pokračuj v něm tam.'));
    const b = mk('button', 'btn ghost pr-sdilet', prenos.pokracovat);
    b.onclick = () => prenosPoslat(tab.chat || tab.resume, tab.title);
    s.appendChild(b);
    neco = true;
  }
  if (p.todos && p.todos.length) {
    const hotovo = p.todos.filter((t) => t.stav === 'completed').length;
    const s = sekce('Postup', hotovo + ' z ' + p.todos.length);
    for (const t of p.todos) {
      const stav = t.stav === 'completed' ? 'hotovo' : t.stav === 'in_progress' ? 'bezi' : 'ceka-na-radu';
      const r = mk('div', 'pr-radek ' + stav);
      r.append(stavIco(stav), mk('span', 'pr-text', stav === 'bezi' && t.ted ? t.ted : t.text));
      s.appendChild(r);
    }
    neco = true;
  } else if (p.kroky.length) {
    const s = sekce('Kroky', p.kroku > p.kroky.length ? p.kroku + '' : '');
    for (const k of p.kroky) {
      const stav = k.stav === 'bezi' && !p.pracuje ? 'hotovo' : k.stav;
      const r = mk('div', 'pr-radek ' + stav);
      r.append(stavIco(stav), mk('span', 'pr-text', k.text));
      r.title = k.text;
      s.appendChild(r);
    }
    neco = true;
  }
  if (p.agenti.length) {
    const bezi = p.agenti.filter((a) => a.stav === 'bezi').length;
    const s = sekce(window.HUB_ADVANCED ? 'Agenti' : 'Claudíci', bezi ? bezi + ' pracuje' : '');
    for (const a of p.agenti.slice(-8)) {
      const r = mk('button', 'pr-radek pr-agent ' + a.stav);
      r.style.setProperty('--ag', a.barva);
      r.append(stavIco(a.stav), mk('span', 'pr-text', a.ukol || a.jmeno));
      r.title = a.jmeno + (a.ukol ? ' — ' + a.ukol : '');
      // Odroluje ke kartě agenta a krátce ji zvýrazní — stejně jako klik na zprávu.
      r.onclick = () => p.dulSkoc(a.box);
      s.appendChild(r);
    }
    neco = true;
  }
  if (p.ulohy.length) {
    const s = sekce('Na pozadí');
    for (const u of p.ulohy) {
      const r = mk('div', 'pr-radek bezi');
      r.append(stavIco('bezi'), mk('span', 'pr-text', u.text), mk('span', 'pr-druh', u.druh));
      s.appendChild(r);
    }
    neco = true;
  }
  if (p.soubory.length) {
    const s = sekce('Soubory');
    for (const f of p.soubory) {
      const r = mk('div', 'pr-radek pr-soubor');
      r.innerHTML = icon('i-note');
      r.appendChild(mk('span', 'pr-text', f.split(/[\\/]/).pop()));
      r.title = f;
      // Na serveru by „otevřít složku" nic neukázalo — tam jen stáhnout.
      if (!onServer()) {
        const o = mk('button', 'pr-soubor-akce', '↗');
        o.title = 'Otevřít složku';
        o.onclick = () => openExternal(f.replace(/[\\/][^\\/]*$/, ''));
        r.appendChild(o);
      }
      const d = mk('button', 'pr-soubor-akce', '⤓');
      d.title = 'Stáhnout ' + f;
      d.onclick = () => HubSoubor.stahni(f);
      r.appendChild(d);
      s.appendChild(r);
    }
    neco = true;
  }
  return neco;
}

/* Panel se překresluje každých pár vteřin. Kdyby se pokaždé vyměnily i
   tlačítka, na telefonu by zmizela pod prstem dřív, než klepnutí skončí
   (proto dřív nešlo sdílet) — vymění se jen to, co se opravdu změnilo.
   Rozepsané pole v panelu se nepřekresluje vůbec. */
function prehledStabilne(tab, box) {
  if (box.contains(document.activeElement) && document.activeElement.tagName === 'INPUT') return !box.hidden;
  const novy = document.createElement('div');
  const neco = prehledHtml(tab, novy);
  const podpis = (neco ? '1' : '0') + novy.innerHTML + '|' + novy.textContent;
  if (box.dataset.podpis === podpis && box.firstChild) return neco;
  box.textContent = '';
  box.dataset.podpis = podpis;
  // prehledHtml navěsil obsluhu na `novy`, ne na box — děti se přesunou, obsluha jde s nimi.
  while (novy.firstChild) box.appendChild(novy.firstChild);
  return neco;
}

function renderPrehled(tab) {
  const box = $('prehled');
  if (!box) return false;
  const neco = prehledStabilne(tab, box);
  box.hidden = !neco;
  document.body.classList.toggle('ma-prehled', neco);
  return neco;
}

/* Telefon: panel vpravo není, Průběh je v okně z nabídky ⋯. */
function openPrehled(tab) {
  const wrap = document.createElement('div');
  wrap.className = 'onb set-modal prehled-modal';
  wrap.innerHTML = '<div class="onb-box"><div class="onb-head"><div><div class="onb-title">Průběh</div>' +
    '<div class="onb-sub"></div></div><span class="spacer"></span>' +
    '<button class="set-x" title="Zavřít">×</button></div><div class="prehled-telo"></div></div>';
  wrap.querySelector('.onb-sub').textContent = tab.title || '';
  const telo = wrap.querySelector('.prehled-telo');
  const kresli = () => { if (!prehledStabilne(tab, telo)) { telo.textContent = 'Zatím se tu nic neděje.'; delete telo.dataset.podpis; } };
  kresli();
  const t = setInterval(kresli, 1500);
  const zavri = () => { clearInterval(t); wrap.remove(); };
  wrap.querySelector('.set-x').onclick = zavri;
  wrap.onclick = (ev) => { if (ev.target === wrap) zavri(); };
  document.body.appendChild(wrap);
}

document.addEventListener('clockify-drawn', () => showActionbar(ACTIVE));

function renderActions() {
  const box = $('actions');
  box.textContent = '';
  for (const a of ACTIONS) {
    if (a.dev && !devMode()) continue;
    if (!STATE.skills.includes(a.skill)) continue;  // never offer "Unknown command"
    const el = document.createElement('button');
    el.className = 'barbtn';
    el.innerHTML = icon(a.icon) + '<span></span>';
    el.querySelector('span').textContent = a.label;
    el.onclick = () => runSlash(a.cmd);
    box.appendChild(el);
  }
  // Tlačítka pluginů appky (pluginy.js, api.akce).
  for (const a of (window.HubPluginy ? HubPluginy.akce() : [])) {
    const el = document.createElement('button');
    el.className = 'barbtn plg-akce';
    el.innerHTML = icon('i-plus') + '<span></span>';
    el.querySelector('span').textContent = a.label;
    el.title = a.title || ('Plugin ' + a.id);
    el.onclick = () => { try { a.run(); } catch (err) { toast('Plugin ' + a.id + ': ' + err.message); } };
    box.appendChild(el);
  }
  showActionbar(ACTIVE);
}

function renderFooter() {
  const foot = $('footer');
  foot.textContent = '';
  // Jako v appce Claude: kolečko s iniciálou a jméno — klik vede do Účtu.
  // V prostoru na serveru běží hub pod účtem `hub` — jméno je z brány.
  const gw = (STATE.config && STATE.config.gateway_user) || null;
  const name = String((gw && (gw.name || gw.email)) || STATE.user || '').trim();
  const me = document.createElement('button');
  me.className = 'footer-me';
  me.title = 'Účet a nastavení';
  const avatar = document.createElement('span');
  avatar.className = 'footer-avatar';
  avatar.textContent = (name.charAt(0) || '?').toUpperCase();
  const who = document.createElement('span');
  who.className = 'footer-name';
  who.textContent = name || 'Účet';
  me.append(avatar, who);
  me.onclick = () => HubSettings.open({...hubIO(), state: STATE, tab: 'ucet'});
  foot.appendChild(me);
  if (!spotrebaTimer) hlidejLimit();
  // Verze vede rovnou na Aktualizace: ty se hned zeptají GitHubu, co je
  // nejnovější, a mají odkaz na seznam vydání.
  const ver = document.createElement('button');
  ver.className = 'footer-ver';
  ver.textContent = 'v' + STATE.version.version;
  ver.title = 'Verze aplikace — klikni pro aktualizace';
  ver.onclick = () => openUpdateSettings(false);
  foot.appendChild(ver);
}

/* Kde se pracuje. Okno vypadá na počítači i na serveru stejně, tak to říká
   štítek v hlavičce — a klik na něj vede do Účtu, odkud se přepíná. */
/* Přepnutí prostředí jedním kliknutím. Dřív se na to muselo přes
   Nastavení → Účet, což je na věc, která se dělá několikrát denně, schovaná
   cesta. Když přepnout nejde (není adresa serveru, vypršelo přihlášení,
   prostor se neotevřel z počítače), otevře se účet — tam se to dá spravit. */
async function switchPlace() {
  const badge = $('btn-place');
  if (badge) badge.disabled = true;
  try {
    if (onServer()) {
      // Zpátky na počítač jde jen okno, které z počítače přišlo. V prohlížeči
      // se odznak nedá ani zmáčknout (renderPlace), takže se tu nic neohlašuje.
      HubServer.backTo('local');
      return;
    } else {
      const res = await HubServer.go(hubIO()).catch((e) => ({error: e.message}));
      if (res && res.ok) return;
      if (res && res.error) toast(res.error);
    }
    HubSettings.open({...hubIO(), state: STATE, tab: 'ucet'});
  } finally {
    if (badge) badge.disabled = false;
  }
}

/* Jak se tomuhle prostředí říká: „PC" na počítači, „server" v prostoru.
   Stojí to nahoře v liště, na uvítací obrazovce i v titulku okna — ať je
   z každého místa poznat, kde se to, co napíšu, odehraje. */
function placeName() {
  return onServer() ? 'na serveru' : 'na počítači';
}

/* Vlastní název a ikona appky (Nastavení → Vzhled, hub/vzhled.py): v liště,
   v šuplíku a na úvodní stránce. Bez nastavení zůstává „Aladin"
   (app.shown = vlastní název, jinak výchozí ze serveru). */
function renderBranding() {
  const app = STATE.app || {};
  const name = app.shown || 'Aladin';
  // Titulek = název okna appky (Chrome v režimu appky, záložka prohlížeče).
  document.title = titulekOkna(app.name ? app.name : name + ' ' + placeName());
  const home = $('btn-home');
  if (home) {
    home.querySelector('strong').textContent = name;
    let img = home.querySelector('.brand-img');
    const svg = home.querySelector('.brand-ico');
    if (app.icon) {
      if (!img) {
        img = document.createElement('img');
        img.className = 'brand-img';
        img.alt = '';
        home.insertBefore(img, home.firstChild);
      }
      img.src = '/app-ikona/192.png?v=' + app.icon;
      if (svg) svg.style.display = 'none';
    } else {
      if (img) img.remove();
      if (svg) svg.style.display = '';
    }
  }
  const head = document.querySelector('.drawer-head strong');
  if (head) head.textContent = name;
  const big = document.querySelector('.welcome-big');
  if (big && big.firstChild && big.firstChild.nodeType === 3) {
    big.firstChild.textContent = name + ' ';
  }
  for (const link of document.querySelectorAll('link[rel="icon"]')) {
    if (app.icon) link.href = '/app-ikona/32.png?v=' + app.icon;
  }
  // Okno appky na počítači (WebKitGTK) si název a ikonu přečte znovu hned.
  try { window.webkit.messageHandlers.hubApp.postMessage('zmena'); } catch (_) { /* prohlížeč */ }
}

/* Přístup do cizího prostoru (gateway/pristup.py). Brána drží, kde host je,
   v cookie; přepíná se POSTem na /gw/pristup a stránka se pak načte znovu. */
async function pristupGw(payload) {
  const r = await fetch('/gw/pristup', payload === undefined ? {credentials: 'same-origin'} : {
    method: 'POST', credentials: 'same-origin',
    headers: {'Content-Type': 'application/json', 'X-Hub-Account': '1'},
    body: JSON.stringify(payload),
  });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || 'Server teď neodpovídá, zkus to za chvíli.');
  return data;
}

async function prostorJdi(ownerId) {
  try {
    await pristupGw(ownerId ? {akce: 'vstoupit', vlastnik: ownerId} : {akce: 'odejit'});
    location.href = '/';
  } catch (e) { toast(e.message); }
}

/* Klik na odznak v prostoru na serveru: nabídka „Prostory, kam máš přístup“
   (a cesta zpátky na počítač, když okno z počítače přišlo). */
async function placeMenu(ev) {
  const b = ev.currentTarget.getBoundingClientRect();
  let data = {kam: []};
  try { data = await pristupGw(); } catch (e) { toast(e.message); }
  const items = [];
  if (STATE.guest) {
    items.push({icon: 'i-hub', label: 'Zpět do svého prostoru', run: () => prostorJdi(0)});
  } else if (window.HubServer && HubServer.localBack()) {
    items.push({icon: 'i-laptop', label: 'Na tomto počítači', run: () => HubServer.backTo('local')});
  }
  for (const k of data.kam || []) {
    if (STATE.guest && STATE.guest.owner === k.id) continue;
    items.push({icon: 'i-user', label: `Prostor: ${k.name || k.email} · ${k.label}`,
                run: () => prostorJdi(k.id)});
  }
  if (!items.length) {
    toast('Do žádného cizího prostoru přístup nemáš. Pozvat tě může jeho vlastník v Nastavení → Účet → Přístup.');
    return;
  }
  showMenu(b.left, b.bottom, items);
}

/* Pruh nahoře, když je host v cizím prostoru — ať je pořád jasné, čí je to. */
function renderGuestBar() {
  const g = STATE.guest;
  document.documentElement.dataset.guest = g ? g.role : '';
  let bar = $('guest-bar');
  if (!g) { if (bar) bar.remove(); return; }
  if (!bar) {
    bar = document.createElement('div');
    bar.id = 'guest-bar';
    bar.className = 'guest-bar';
    document.body.prepend(bar);
  }
  bar.textContent = '';
  const txt = document.createElement('span');
  txt.textContent = `Prostor: ${g.owner_name || '?'} · ${g.label || g.role}`;
  bar.appendChild(txt);
  const back = document.createElement('button');
  back.className = 'btn ghost';
  back.textContent = 'Zpět do svého';
  back.onclick = () => prostorJdi(0);
  bar.appendChild(back);
}

/* Odznak „spravuje …“ u tabu, který otevřel host z jiného účtu. */
function markGuest(tab, name) {
  if (!tab || !tab.el || !name) return;
  let span = tab.el.querySelector('.tab-guest');
  if (!span) {
    span = document.createElement('span');
    span.className = 'tab-guest';
    tab.el.querySelector('.tab-title').after(span);
  }
  span.textContent = 'spravuje ' + name;
  span.title = name + ' sem přišel/přišla přes přístup do tvého prostoru';
}

function renderPlace() {
  const badge = $('btn-place');
  if (!badge) return;
  const u = STATE.config.gateway_user;
  renderGuestBar();
  const app = STATE.app || {};
  const shown = app.shown || 'Aladin';
  document.title = titulekOkna(app.name ? app.name : shown + ' ' + placeName());
  const big = $('welcome-place');
  if (big) big.textContent = placeName();
  renderBranding();
  // Barva celé appky se odvozuje odsud: počítač jantarově, prostor modře.
  document.documentElement.dataset.place = u ? 'server' : 'pc';
  // Čí prostor to je, říká přepínač prostoru vedle (renderProstorBtn) a pruh
  // nahoře — odznak zůstane krátký, na telefonu by se jinak nevešel.
  badge.textContent = u ? 'Server' : 'PC';
  badge.classList.toggle('guest', !!STATE.guest);
  badge.setAttribute('aria-label', shown + ' ' + placeName());
  badge.classList.toggle('on-server', !!u);
  /* Přepnout jde jen z appky: okno v prohlížeči se na počítač vrátit nemá kam.
     Tam je odznak pouhá cedulka — po kliknutí se nestane nic a netváří se, že
     by mělo. Dřív otvíral nastavení, což s nápisem „server" nesouvisí. */
  const lzePrepnout = !u || !!(window.HubServer && HubServer.localBack());
  // Na serveru vede odznak i do cizích prostorů, kam má člověk přístup.
  badge.classList.toggle('plain', false);
  badge.onclick = u ? placeMenu : switchPlace;
  badge.title = u
    ? `Pracuješ na serveru · ${u.name || u.email}` +
      (lzePrepnout ? ' — klikni a přepneš na počítač' : '')
    : 'Pracuješ na tomhle počítači — klikni a přepneš na server';
  // Počítač, na který Claude z prostoru dosáhne (hub/pocitac.py).
  if (u) HubPocitac.chip(hubIO());
  // Připojené předplatné, které běžící prostor ještě nemá — jednou za načtení.
  if (u && !renderPlace.noticed) {
    renderPlace.noticed = true;
    HubPredplatne.notice(hubIO());
  }
}

/* Uvítání. Není to jen ozdoba — je to jediná obrazovka, kterou člověk vidí,
   než něco otevře, takže nese i to, co je dobré vědět hned: kde se naposledy
   dělalo, co zůstalo rozdělané a jestli něco chybí. */
const NS = 'http://www.w3.org/2000/svg';

function dayPart(hour) {
  if (hour < 5) return 'noc';
  if (hour < 10) return 'rano';
  if (hour < 18) return 'den';
  if (hour < 22) return 'vecer';
  return 'noc';
}

/* Scéna podle denní doby: slunce nad obzorem tím výš, čím je blíž poledni.
   Kreslí se z proměnných motivu, aby seděla ve světlém i tmavém režimu. */
function dayScene(part) {
  const svg = document.createElementNS(NS, 'svg');
  svg.setAttribute('viewBox', '0 0 280 96');
  svg.setAttribute('width', '280');
  svg.setAttribute('height', '96');
  const add = (tag, attrs) => {
    const n = document.createElementNS(NS, tag);
    for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v);
    svg.appendChild(n);
    return n;
  };

  const noc = part === 'noc';
  const y = {rano: 60, den: 34, vecer: 62, noc: 40}[part];
  const disc = noc ? 'var(--dim)' : 'var(--sun)';

  // záře kolem tělesa
  const glow = add('radialGradient', {id: 'dayglow'});
  for (const [offset, op] of [[0, '.30'], [1, '0']]) {
    const stop = document.createElementNS(NS, 'stop');
    stop.setAttribute('offset', offset);
    stop.setAttribute('stop-color', disc);
    stop.setAttribute('stop-opacity', op);
    glow.appendChild(stop);
  }
  add('circle', {cx: 140, cy: y, r: 44, fill: 'url(#dayglow)'});

  if (noc) {
    // měsíc = kruh s odkrojeným kruhem, ne "banán" z cesty
    const mask = document.createElementNS(NS, 'mask');
    mask.setAttribute('id', 'moon');
    for (const [cx, fill] of [[140, '#fff'], [150, '#000']]) {
      const c = document.createElementNS(NS, 'circle');
      c.setAttribute('cx', cx); c.setAttribute('cy', y);
      c.setAttribute('r', 15); c.setAttribute('fill', fill);
      mask.appendChild(c);
    }
    svg.appendChild(mask);
    add('circle', {cx: 140, cy: y, r: 15, fill: disc, mask: 'url(#moon)'});
    for (const [cx, cy, r] of [[92, 26, 1.6], [196, 32, 2], [72, 52, 1.3],
                               [214, 58, 1.5], [116, 18, 1.2]]) {
      add('circle', {cx, cy, r, fill: disc, opacity: '.5'});
    }
  } else {
    add('circle', {cx: 140, cy: y, r: 15, fill: disc});
    // paprsky ubývají, jak slunce klesá k obzoru
    const rays = part === 'den' ? 8 : 5;
    for (let i = 0; i < rays; i++) {
      const a = (Math.PI * (i + 0.5)) / rays;
      const x1 = 140 - Math.cos(a) * 23, y1 = y - Math.sin(a) * 23;
      const x2 = 140 - Math.cos(a) * 31, y2 = y - Math.sin(a) * 31;
      add('line', {x1, y1, x2, y2, stroke: disc, 'stroke-width': 2.5,
                   'stroke-linecap': 'round', opacity: '.65'});
    }
  }

  // obzor
  add('line', {x1: 40, y1: 78, x2: 240, y2: 78, stroke: 'var(--border)',
               'stroke-width': 2, 'stroke-linecap': 'round'});
  add('line', {x1: 96, y1: 78, x2: 184, y2: 78, stroke: disc,
               'stroke-width': 2, 'stroke-linecap': 'round', opacity: '.55'});
  return svg;
}

/* Komu se tu říká „dobrý den". V prostoru na serveru běží hub pod systémovým
   účtem `hub` — to je stroj, ne člověk, a „Dobrou noc, hub" zní jako chyba.
   Tam se vezme křestní jméno přihlášeného z brány; bez jména radši nic, než
   aby se zdravilo příjmením z e-mailu. */
function greetName() {
  const u = STATE.config && STATE.config.gateway_user;
  if (u) return String(u.name || '').trim().split(/\s+/)[0] || '';
  return (STATE.user || '').split(/[\s.]/)[0];
}

/* Pozdrav a scéna podle denní doby. Kreslí se znovu i během dne: okno bývá
   otevřené od rána do noci, a „Dobrou noc" v pět odpoledne je pozdrav, který
   se naposledy vykreslil v noci a od té doby nikdo nepřepsal. */
let greetPart = '';
function renderGreeting() {
  const part = dayPart(new Date().getHours());
  const pozdrav = {rano: 'Dobré ráno', den: 'Dobrý den',
                   vecer: 'Dobrý večer', noc: 'Dobrou noc'}[part];
  const who = greetName();
  $('welcome-greet').textContent = pozdrav + (who ? ', ' + who : '') + '.';
  if (part === greetPart) return;
  greetPart = part;
  const scene = $('welcome-scene');
  scene.textContent = '';
  scene.appendChild(dayScene(part));
}

/* Úvod jako nový chat (motiv Claude): zpráva z pole na úvodu otevře nový chat
   a pošle se mu, jakmile je bublina připravená (rozjezd Claude Code řeší composer). */
function welcomeSend() {
  const input = $('welcome-prompt-input');
  const text = String(input.value || '').trim();
  if (!text) return;
  const tab = newChatHere();
  if (!tab) return;
  input.value = '';
  let tries = 0;
  const t = setInterval(() => {
    if (++tries > 60 || !TABS.includes(tab)) return clearInterval(t);
    if (tab.composer && tab.composer.run && tab.composer.run(text + '\r')) clearInterval(t);
  }, 250);
}

function renderWelcome() {
  greetPart = '';                 // scéna se při plném překreslení kreslí vždy
  renderGreeting();

  const box = $('welcome-actions');
  box.textContent = '';
  const cfg = STATE.config.newtab || {};
  const actions = [];
  if (cfg.agent !== false && cfg.claude !== false) {
    const a = agentById(STATE.default_agent) || agentList(true)[0];
    if (a) {
      // Stejné pojmenování jako v liště tabů: kde se bude pracovat.
      // Nový chat v prostoru okna — ve firmě fialový a pro firmu.
      const f = prostorFirma();
      actions.push(['i-terminal', f ? 'Nový chat · ' + f.nazev : novyChatText(),
        f ? 'primary firma' : 'primary', () => newChatHere()]);
    }
  }
  $('nav-terminal').hidden = !(cfg.shell !== false && STATE.config.dev_mode);
  if (cfg.shell !== false && STATE.config.dev_mode) {
    actions.push(['i-terminal', 'Otevřít terminál', 'ghost',
      () => openTab({kind: 'shell', path: '', title: 'Terminál'})]);
  }
  // Nastavení patří o řádek níž: nahoře se otevírá práce, pod ní se nastavuje.
  actions.push(['break']);
  actions.push(['i-gear', 'Nastavení', 'ghost',
    () => HubSettings.open({...hubIO(), state: STATE})]);
  for (const [ico, label, kind, run] of actions) {
    if (ico === 'break') {
      const br = document.createElement('div');
      br.className = 'welcome-break';
      box.appendChild(br);
      continue;
    }
    const b = document.createElement('button');
    b.className = 'btn ' + kind;
    b.innerHTML = icon(ico) + '<span></span>';
    b.querySelector('span').textContent = label;
    b.onclick = run;
    box.appendChild(b);
  }

  renderWelcomeCols();
  renderWelcomeFirmy();
  renderWelcomeStats();
  HubStats.planNaUvodu(hubIO(), $('welcome-usage'));
  renderWelcomeUpdate();

  const facts = [];
  const mem = STATE.memory || {};
  if (mem.enabled) {
    const total = (mem.counts.learnings || 0) + (mem.counts.errors || 0) +
                  (mem.counts.wins || 0);
    facts.push(total ? total + ' poznámek v paměti' : 'paměť připravená');
    if (STATE.memory_autosave) facts.push('ukládá se sama');
  }
  facts.push('verze ' + STATE.version.version);
  // Technický řádek (paměť, verze) jen ve vývojářském režimu.
  $('welcome-facts').textContent = window.HUB_ADVANCED ? facts.join('  ·  ') : '';
}

/* Statistiky na úvodu: jen pár čísel a poslední měsíc, zbytek je pod ⧉.
   Načítá se až po vykreslení a mimo hlavní tok — první výpočet čte skoro
   gigabajt přepisů a úvodní obrazovka na něj čekat nesmí. */
let welcomeStatsLoaded = false;

async function renderWelcomeStats(force) {
  if (welcomeStatsLoaded && !force) return;
  const box = $('welcome-stats');
  let data;
  for (let i = 0; i < 30; i++) {
    try {
      data = await api('stats');
    } catch (_) { return; }
    if (!data.running) break;
    await new Promise(r => setTimeout(r, 1200));
  }
  if (!data || data.running || !data.tokens) return;
  welcomeStatsLoaded = true;

  box.textContent = '';
  const t = data.tokens;
  const gh = data.github || {};
  const row = document.createElement('div');
  row.className = 'wst-row';
  const add = (value, label) => {
    const b = document.createElement('button');
    b.className = 'wst-tile';
    b.innerHTML = '<span class="wst-value"></span><span class="wst-label"></span>';
    b.querySelector('.wst-value').textContent = value;
    b.querySelector('.wst-label').textContent = label;
    b.onclick = () => HubStats.open(hubIO());
    row.appendChild(b);
  };
  // Jednoduchý režim: jen to, čemu rozumí každý. Tokeny a GitHub pro pokročilé.
  if (window.HUB_ADVANCED) add(statNum(t.out), 'napsaných tokenů');
  add(statNum(data.prompts), 'zpráv');
  add(String(data.sessions), 'rozhovorů');
  if (!window.HUB_ADVANCED) {
    add(String((data.days || []).filter((d) => d.prompts).length), 'dní s prací');
  }
  if (window.HUB_ADVANCED && gh.ok) add(String(gh.commits_year), 'commitů za rok');
  box.appendChild(row);

  // Posledních 30 dnů — jedna série, měří velikost, proto bez legendy.
  const days = (data.days || []).slice(-30);
  if (days.length > 3) {
    const max = Math.max(1, ...days.map(d => d.prompts));
    const chart = document.createElement('div');
    chart.className = 'wst-spark';
    chart.title = 'Odeslané zprávy za posledních ' + days.length + ' dnů';
    for (const d of days) {
      const col = document.createElement('span');
      col.className = 'wst-col';
      const bar = document.createElement('span');
      bar.className = 'wst-bar';
      bar.style.height = Math.max(d.prompts ? 8 : 0, d.prompts / max * 100) + '%';
      col.title = `${d.day}: ${d.prompts} zpráv`;
      col.appendChild(bar);
      chart.appendChild(col);
    }
    box.appendChild(chart);
    const cap = document.createElement('div');
    cap.className = 'wst-cap';
    cap.textContent = 'posledních ' + days.length + ' dnů  ·  klikni pro celé statistiky';
    box.appendChild(cap);
  }
  box.hidden = false;
}

function statNum(n) {
  if (n >= 1e9) return (n / 1e9).toFixed(1).replace('.', ',') + ' mld';
  if (n >= 1e6) return (n / 1e6).toFixed(1).replace('.', ',') + ' M';
  if (n >= 1e3) return Math.round(n / 1e3) + ' tis.';
  return String(n || 0);
}

function kdy(ts) {
  if (!ts) return '';
  const dny = Math.floor((Date.now() / 1000 - ts) / 86400);
  if (dny <= 0) return 'dnes';
  if (dny === 1) return 'včera';
  if (dny < 7) return `před ${dny} dny`;
  if (dny < 60) return `před ${Math.floor(dny / 7)} týdny`;
  return `před ${Math.floor(dny / 30)} měsíci`;
}

/* Dva sloupce: kde se naposledy dělalo a co zůstalo rozdělané. Obojí je
   zkratka k tomu, co člověk stejně otevře jako první. */
/* Otevřené chaty na úvodní stránce. Appka po otevření nepřeskočí do
   posledního chatu (restore) — zůstane na Domů a tady je, kam pokračovat. */
function renderWelcomeOpen() {
  const box = $('welcome-open');
  if (!box) return;
  box.textContent = '';
  const tabs = TABS.filter(t => !t.exited && tabProstor(t) === prostor());
  box.hidden = !tabs.length;
  if (!tabs.length) return;
  box.appendChild(Object.assign(document.createElement('div'),
    {className: 'wcol-title', textContent: 'Otevřené chaty'}));
  for (const t of tabs) {
    const b = document.createElement('button');
    b.className = 'wcol-item welcome-open-item';
    b.innerHTML = '<span class="open-dot"></span><span class="wcol-name"></span>' +
                  (t.ceka ? '<span class="wcol-note ceka">čeká na tebe ›</span>'
                   : t.hotovo ? '<span class="wcol-note hotovo">hotovo ›</span>'
                   : '<span class="wcol-note">pokračovat ›</span>');
    b.classList.toggle('ceka', !!t.ceka);
    b.querySelector('.wcol-name').textContent = t.title;
    const dot = t.el && t.el.querySelector('.tab-agent');
    const color = dot && dot.style.getPropertyValue('--agent-color');
    if (isFirmaVault(t.vault)) b.querySelector('.open-dot').style.background = 'var(--firma)';
    else if (color) b.querySelector('.open-dot').style.background = color;
    b.onclick = () => activate(t);
    box.appendChild(b);
  }
}

/* Firmy na úvodní stránce: v osobním prostoru dlaždice firem (klik = okno
   přejde do firmy), ve firmě její přehled — klienti a podkategorie s chatem. */
function renderWelcomeFirmy() {
  const box = $('welcome-firmy');
  if (!box) return;
  box.textContent = '';
  const f = prostorFirma();
  const zakladat = !!(STATE.firma && STATE.firma.zakladat);
  box.hidden = !onServer() || (!hasFirma() && !zakladat);
  if (box.hidden) return;
  const title = (text) => box.appendChild(Object.assign(document.createElement('div'),
    {className: 'wcol-title', textContent: text}));
  if (!f) {
    title('Tvoje firmy');
    const row = document.createElement('div');
    row.className = 'wf-cards';
    for (const x of firmyList()) {
      const b = document.createElement('button');
      b.className = 'wf-card';
      b.innerHTML = '<span class="wf-name"></span><span class="wf-role"></span>';
      b.querySelector('.wf-name').textContent = x.nazev;
      b.querySelector('.wf-role').textContent = ROLE_FIRMY[x.role] || x.role;
      b.title = 'Přepnout okno do firmy ' + x.nazev;
      b.onclick = () => setProstor(x.zkratka);
      row.appendChild(b);
    }
    if (zakladat) {
      const add = document.createElement('button');
      add.className = 'wf-card wf-add';
      add.innerHTML = icon('i-plus') + '<span>Založit firmu</span>';
      add.onclick = spravaFirem;
      row.appendChild(add);
    }
    box.appendChild(row);
    return;
  }
  title(f.nazev + ' · ' + (ROLE_FIRMY[f.role] || f.role));
  if (!KLIENTI[f.zkratka]) loadKlienti(f.zkratka);
  const list = document.createElement('div');
  list.className = 'wf-klienti';
  const chip = (label, run, cls) => {
    const b = document.createElement('button');
    b.className = 'wf-chip' + (cls ? ' ' + cls : '');
    b.textContent = label;
    b.onclick = run;
    return b;
  };
  const projekty = jeProjekty(f);
  if (projekty) {
    // Firma pro sebe: rovnou projekty, klik = chat v projektu.
    const row = document.createElement('div');
    row.className = 'wf-klient';
    for (const k of KLIENTI[f.zkratka] || []) row.appendChild(chip(k.nazev, () => openFirmaTab(f.zkratka, k.nazev), 'wf-k'));
    if (row.childNodes.length) list.appendChild(row);
  } else {
    for (const k of KLIENTI[f.zkratka] || []) {
      const row = document.createElement('div');
      row.className = 'wf-klient';
      row.appendChild(chip(k.nazev, () => openFirmaTab(f.zkratka, k.nazev), 'wf-k'));
      for (const p of k.podkategorie) row.appendChild(chip(p, () => openFirmaTab(f.zkratka, k.nazev, p)));
      list.appendChild(row);
    }
  }
  if (!(KLIENTI[f.zkratka] || []).length) {
    list.appendChild(Object.assign(document.createElement('div'), {className: 'wf-empty',
      textContent: !KLIENTI[f.zkratka] ? 'Načítám…'
        : projekty ? 'Zatím žádný projekt, do kterého vidíš.' : 'Zatím žádný klient, do kterého vidíš.'}));
  }
  box.appendChild(list);
  const acts = document.createElement('div');
  acts.className = 'wf-acts';
  acts.append(chip(projekty ? 'Všechny projekty ›' : 'Všichni klienti ›', () => showProjekty(true), 'wf-link'),
              chip('Obsidian firmy ›', () => openVault('', firmaVaultId(f)), 'wf-link'));
  if (f.role === 'kapitan') acts.append(chip('Spravovat firmu ›', spravaFirem, 'wf-link'));
  box.appendChild(acts);
}

function renderWelcomeCols() {
  const wrap = $('welcome-cols');
  wrap.textContent = '';
  // Ve firmě jsou místo osobních projektů klienti (renderWelcomeFirmy).
  wrap.hidden = !!prostorFirma();
  if (wrap.hidden) return;
  const live = (STATE.projects || []).filter(p => !p.archived);
  const recent = [...live].sort((a, b) => b.mtime - a.mtime).slice(0, 4);
  const dirty = live.filter(p => p.dirty)
    .sort((a, b) => b.dirty - a.dirty).slice(0, 4);

  const column = (title, items, note) => {
    if (!items.length) return null;
    const col = document.createElement('div');
    col.className = 'wcol';
    col.appendChild(Object.assign(document.createElement('div'),
      {className: 'wcol-title', textContent: title}));
    for (const p of items) {
      const b = document.createElement('button');
      b.className = 'wcol-item';
      b.innerHTML = '<span class="wcol-name"></span><span class="wcol-note"></span>';
      b.querySelector('.wcol-name').textContent = p.label || p.name;
      b.querySelector('.wcol-note').textContent = note(p);
      b.title = p.path;
      b.onclick = () => openTab({kind: 'project', path: p.path,
                                 title: p.label || p.name});
      col.appendChild(b);
    }
    return col;
  };

  const a = column('Naposledy', recent, p => kdy(p.mtime));
  // Neuložené změny v gitu — řeč programátorů.
  const b = window.HUB_ADVANCED ? column('Rozdělané', dirty, p => p.dirty + ' změn') : null;
  if (a) wrap.appendChild(a);
  if (b) wrap.appendChild(b);
}

function renderDoctor() {
  const d = STATE.doctor, warn = $('welcome-warn');
  const problems = [];
  if (!d.bash) {
    problems.push(d.platform === 'windows'
      ? 'Chybí součást <b>Git for Windows</b>, bez které Claude nefunguje. ' +
        'Spusť prosím instalačku aplikace znovu — doinstaluje ji.'
      : 'Chybí součást, bez které Claude nefunguje. Spusť prosím instalačku aplikace znovu.');
  }
  // Chybí-li úplně všechno, je to problém. Chybí-li jen ten vybraný, taky —
  // ale ostatní se nabídnou, ať se dá pracovat hned.
  const ready = agentList(true);
  const def = agentById(STATE.default_agent);
  if (!ready.length) {
    const first = agentList()[0];
    problems.push('<b>Claude tu ještě není nainstalovaný.</b>' +
      (first && first.install
        ? ' <button class="btn primary" data-fix="install" data-agent="' + escapeHtml(first.id) +
          '">Nainstalovat</button>' : ' Spusť prosím instalačku aplikace znovu.'));
  } else if (def && !def.path) {
    problems.push('<b>' + escapeHtml(def.label) + '</b> tu není nainstalovaný.' +
      (def.install ? ' <button class="btn primary" data-fix="install" data-agent="' +
        escapeHtml(def.id) + '">Nainstalovat</button>' : '') +
      '<br>Můžeš zatím pracovat s: ' + ready.map((a) => escapeHtml(a.label)).join(', ') + '.');
  }
  // Složky projektů, do kterých hub nesměl — na macOS bez povolení k Ploše.
  const blocked = (d.blocked_dirs || []).map((p) => p.replace(STATE.home, '~'));
  if (blocked.length) {
    const where = blocked.map((p) => '<code>' + escapeHtml(p) + '</code>').join(', ');
    problems.push(d.platform === 'mac'
      ? 'macOS nepustil hub do složky ' + where + ' — projekty z ní v panelu chybí.<br>' +
        'Povol to: <b>Nastavení systému → Soukromí a zabezpečení → Soubory a složky</b> → ' +
        'u aplikace, ze které hub spouštíš (obvykle <b>Terminál</b>), zapni <b>Plocha</b> ' +
        '(a Dokumenty, když tam projekty máš). Pak hub zavři a spusť znovu.'
      : 'Do složky ' + where + ' hub nesmí — projekty z ní v panelu chybí. ' +
        'Zkontroluj práva ke složce.');
  }
  warn.hidden = !problems.length;
  warn.innerHTML = problems.join('<hr style="border:none;border-top:1px solid var(--border);margin:8px 0">');
  // Opravit jedním kliknutím místo příkazu k opsání.
  for (const b of warn.querySelectorAll('[data-fix=install]')) {
    const a = agentById(b.dataset.agent);
    b.onclick = () => openTab({kind: 'install:' + b.dataset.agent, path: STATE.home,
                               title: 'Instalace: ' + (a ? a.label : 'Claude')});
  }
}

/* Které „+" tlačítko se ukazuje. Kdo jede jen v agentovi, nechce vedle sebe
   pořád tlačítko na holý shell — a naopak. Klíč `claude` je tu z verzí do 1.6,
   kde se tlačítko tak jmenovalo; starý konfig se tím pádem nemusí přepisovat. */
/* Na počítači, který je připojený k serveru, je potřeba rozlišit, kde chat poběží:
   „PC" (tady) × „Server osobní" / „Server firemní" (tam). Bez serveru zůstává „Nový chat". */
function naPcSeServerem() {
  const cfg = (STATE && STATE.config) || {};
  return !onServer() && !!(cfg.gw_server && cfg.gw_logged_in);
}
/* Text tlačítka pro nový chat. */
function novyChatText() {
  // Ve firmě je „Nový chat" fialový a otevře chat firmy (prostor okna).
  return naPcSeServerem() ? 'PC' : 'Nový chat';
}
const TITULKY_BEZ_JMENA = /^(Chat|Rozhovor|Osobní chat|Server osobní|Server firemní|Firemní chat|PC)$/;

function newTabLabel() {
  // Tlačítko říká, KDE se bude pracovat, ne kdo to odpracuje — to je vidět
  // na odznaku agenta a v popisku. Na počítači „PC", v prostoru „Server";
  // kde je firemní trezor, je potřeba rozlišit i nad čím Claude pojede.
  // Kde se pracuje, říká prostor okna (přepínač v hlavičce) — taby jiného
  // prostoru v liště nejsou, takže „Server osobní" už rozlišovat netřeba.
  return naPcSeServerem() ? 'PC' : 'Chat';
}

function renderNewTabButtons() {
  const cfg = STATE.config.newtab || {};
  const btn = $('btn-new-agent');
  btn.hidden = cfg.agent === false || cfg.claude === false;
  // Holý terminál je vývojářská věc. Kdo hub používá na psaní s agentem, má
  // vedle tabů tlačítko, které nikdy nezmáčkne — a když ho zmáčkne omylem,
  // kouká do shellu a neví, kde je.
  $('btn-new-shell').hidden = cfg.shell === false || !STATE.config.dev_mode;
  // Firma okna má jediné „+ Nový chat" (fialové) — samostatné firemní tlačítko
  // nahradil přepínač prostoru v hlavičce.
  const firma = !!prostorFirma();
  $('btn-new-firma').hidden = true;
  btn.classList.toggle('firma', firma);
  // Panel vlevo: „Server osobní" a pod ním fialový „Server firemní".
  const dn = $('btn-drawer-new');
  // V motivu Claude je to prostě „Nový chat“ (název prostředí říká odznak v liště).
  if (dn) dn.querySelector('.nav-text').textContent = document.documentElement.dataset.motiv === 'claude' ? 'Nový chat' : novyChatText();
  const df = $('btn-drawer-firma');
  if (df) df.hidden = true;
  if (dn) dn.classList.toggle('firma', firma);
  // Jedno „+" na telefonu: schová se jen tehdy, když by pod ním nic nebylo.
  $('btn-new-menu').hidden = btn.hidden && $('btn-new-shell').hidden;
  const a = agentById(STATE.default_agent) || agentList(true)[0];
  const label = btn.querySelector('span');
  if (a && label) {
    label.textContent = novyChatText();
    const kde = onServer() ? (firma ? ' nad tvými osobními poznámkami' : ' na serveru')
                           : ' na tomhle počítači';
    btn.title = 'Nový chat s Claudem' + kde +
      (window.HUB_ADVANCED && agentList(true).length > 1 ? ' (pravé tlačítko = výběr agenta)' : '');
  }
}

async function reload() {
  STATE = await api('state');
  markAdvanced();
  renderProjects($('search').value);
  if (!usageTimer && Date.now() - usageAt > 120000) loadUsage();
  if (!prenosTimer) { prenosTimer = setInterval(() => { if (!document.hidden) prenosPoll(); }, 12000); setTimeout(prenosPoll, 3000); }
  if (onServer() && !relaceTimer) {
    relaceTimer = setInterval(() => { if (!document.hidden) loadRelace(); }, 5000);
    relacePritTimer = setInterval(relacePritomnost, 3000);
    loadRelace();
  }
  if (!$('projekty-page').hidden) renderProjekty();
  renderMemory();
  renderFirma();
  renderActions();
  renderFooter();
  renderPlace();
  loadProstory();                    // cizí prostory do přepínače (jen na serveru)
  renderDoctor();
  renderWelcome();
  renderNewTabButtons();
  applyProstor();
  for (const t of TABS) paintAgent(t);
  applyMotiv(STATE.motiv);           // motiv (barvy + CSS) a s ním applyTheme
  HubVerxee.apply(STATE);            // Nahlásit chybu / Podat návrh, jen s klíčem
}

/* ── agenti ───────────────────────────────────────────────────────────────── */
/* Katalog i to, co je na stroji doopravdy k dispozici, přichází ze serveru
   (/api/state → agents). Frontend si nic o agentech nedomýšlí — jen kreslí. */
function agentList(onlyReady) {
  const all = STATE.agents || [];
  return onlyReady ? all.filter((a) => a.path) : all;
}

function agentById(id) {
  return (STATE.agents || []).find((a) => a.id === id) || null;
}

/* Kterým agentem se otevře tenhle projekt: co si u něj člověk naposled vybral,
   jinak výchozí z nastavení. */
function agentFor(path) {
  const saved = (STATE.config.project_agents || {})[path];
  if (saved && agentById(saved)) return saved;
  return STATE.default_agent || 'claude';
}

/* Volba u projektu přežije zavření hubu — proto do konfigu, ne do localStorage:
   projekt otevírá i doctor a instalačka, a ty do prohlížeče nevidí. */
async function rememberAgent(path, id) {
  if (!path) return;
  const map = {...(STATE.config.project_agents || {})};
  if (map[path] === id) return;
  map[path] = id;
  STATE.config.project_agents = map;      // ať menu hned ukazuje novou volbu
  try { await api('config', {project_agents: map}); } catch (_) { /* nevadí */ }
}

/* Nabídka „čím otevřít nový tab". Nedostupní agenti v ní zůstávají — jinak by
   se z ní nedalo dostat k jejich instalaci. */
function newAgentMenu(ev) {
  const items = agentList().map((a) => ({
    icon: 'i-terminal',
    label: a.path ? a.label : a.label + ' — nainstalovat',
    on: a.path && a.id === STATE.default_agent,
    run: () => (a.path
      ? openTab({kind: 'project', path: STATE.home, title: a.label, agent: a.id})
      : openTab({kind: 'install:' + a.id, path: STATE.home,
                 title: 'instalace: ' + a.label})),
  }));
  if (window.HUB_ADVANCED) {
    items.push({icon: 'i-gear', label: 'Nastavení agentů…',
                run: () => HubSettings.open({...hubIO(), state: STATE, tab: 'agenti'})});
  }
  const b = ev.currentTarget ? ev.currentTarget.getBoundingClientRect() : null;
  showMenu(b ? b.left : ev.clientX, b ? b.bottom : ev.clientY, items);
}

/* ── tabs ─────────────────────────────────────────────────────────────────── */
function openTab({kind, path, title, agent, model, mode, resume, fork, vault, prompt, spici}) {
  const tab = createTab({kind, path, title, agent, model, mode, resume, vault});
  const dims = measure(tab);
  send({t: 'open', ref: tab.ref, kind, path, title, agent: agent || '',
        model: model || '', resume: resume || '', fork: !!fork,
        vault: vault || '', prompt: prompt || '', spici: !!spici, cols: dims.cols, rows: dims.rows});
  return tab;
}

/* Otevřít projekt konkrétním agentem a zapamatovat si tu volbu. */
function openWith(agentId, {path, title}) {
  rememberAgent(path, agentId);
  return openTab({kind: 'project', path, title, agent: agentId});
}

function createTab({kind, path, title, id, agent, model, background, bypass, mode, resume, vault}) {
  const ref = ++refSeq;
  const pane = document.createElement('div');
  pane.className = 'pane';
  $('panes').appendChild(pane);
  // Terminál bydlí ve vlastním obalu — z jeho výšky se počítá počet řádků,
  // takže je to jediné místo, kde se dá terminálu ubrat, aniž by přetekl.
  const termbox = document.createElement('div');
  termbox.className = 'termbox';
  pane.appendChild(termbox);

  const term = new Terminal({
    fontFamily: '"Cascadia Mono","JetBrains Mono","DejaVu Sans Mono",Menlo,Consolas,monospace',
    // Na telefonu je 13 px na 80 sloupců moc — Claude Code pak láme rámečky.
    fontSize: window.matchMedia('(max-width: 820px)').matches ? 11 : 13,
    /* Server přehrává po reloadu jen 256 kB (server.py SCROLLBACK), víc
       historie tedy stejně nepřežije. 100 000 řádků bylo ~150 MB na tab —
       u dvaceti tabů stránka tuhla a padala. */
    scrollback: 3000,
    cursorBlink: true,
    allowProposedApi: true,
    theme: termTheme({vault}),   // firemní tab je fialový od prvního znaku
  });
  const fit = new FitAddon.FitAddon();
  term.loadAddon(fit);
  // Links open in the real browser, not inside the app window.
  term.loadAddon(new WebLinksAddon.WebLinksAddon((_ev, uri) => openLink(uri)));
  term.open(termbox);
  // Šipka dolů, když je terminál odrolovaný nahoru. Ve čtení má vlastní (cteni.js).
  const dolu = document.createElement('button');
  dolu.className = 'dolu term-dolu';
  dolu.title = 'Dolů na konec';
  dolu.innerHTML = icon('i-up');
  dolu.hidden = true;
  pane.appendChild(dolu);
  const hlidejDno = () => {
    const b = term.buffer.active;
    dolu.hidden = b.baseY - b.viewportY < term.rows / 2;
  };
  term.onScroll(hlidejDno);
  term.onWriteParsed(hlidejDno);   // výpis přibývá, i když se kouká výš
  dolu.onclick = () => { term.scrollToBottom(); hlidejDno(); };

  const tab = {ref, id: id || null, title, kind, path, term, fit, pane, termbox,
               // Agent musí být na tabu hned: bublina se podle něj rozhoduje,
               // co umí, a instaluje se o pár řádků níž.
               agent: agent || '', model: model || '', exited: false,
               // Jde v tabu zapnout bypass (agent se spustil s tou možností)?
               // A režim, do kterého se má tab po startu přepnout sám.
               bypass: !!bypass, wantMode: mode || '',
               // Ve které uložené konverzaci tab pokračuje (seznam konverzací).
               resume: resume || '',
               // Nad kterým trezorem tab běží — firemní se kreslí fialově.
               vault: vault || ''};
  const toPty = (d) => {
    tab.lastInput = Date.now();          // ozvěna psaní není „agent pracuje"
    if (tab.id) send({t: 'in', id: tab.id, d});
  };
  term.onData(toPty);
  // Diacritics arrive from the GTK input method as composition events, which
  // xterm.js mishandles badly enough to corrupt the line — see ime.js.
  tab.releaseIME = HubIME.install(term);
  // WebKitGTK nepustí stránku ke schránce, tak se na ni sahá přes náš server.
  tab.releaseClipboard = HubClipboard.install(term, {
    read: clipRead,
    write: clipWrite,
    attach: (path) => typePaths(tab, [path]),
    notice: toast,
    native: onServer,
  });
  wireFiles(tab);
  // Odkazy z výpisu jako tlačítka — rozlámanou adresu nejde kliknout (links.js).
  // Bez links.js (stará stránka z cache, napůl nahraný server) tab jede dál.
  tab.links = window.HubLinks
    ? HubLinks.install(tab, {open: openLink, copy: copyText, notice: toast}) : null;
  // Bublina jen tam, kde běží agent. V holém shellu, při deployi ani během
  // instalace není co překrývat — a odeslaný text by skončil v bashi.
  /* Čtení: v tabu s Claude Code se na terminál kouká jen tehdy, když je na
     co — jinak je přes něj konverzace jako text (cteni.js). Ostatní agenti
     svůj přepis nepíšou, takže by nebylo z čeho číst. */
  if (kind === 'project' && window.HubCteni &&
      (agent || STATE.default_agent || 'claude') === 'claude') {
    tab.cteni = HubCteni.install(tab, {api, notice: toast, imageUrl, openLink, copy: copyText,
                                       // Neodeslané zprávy z okna (ne z fronty na serveru) — přežijí reload.
                                       odchozi: {nacti: () => nactiOdchozi(tab), uloz: (list) => ulozOdchozi(tab, list)},
                                       rozvetvit: rozvetvitChat,
                                       aktivni: () => ACTIVE === tab,
                                       // Nepřevzatá zpráva se po restartu pošle sama znovu; odesílání jde pozastavit.
                                       znovu: (text) => !!(tab.composer && tab.composer.znovu && tab.composer.znovu(text)),
                                       jeOnline: online,
                                       pozastaveno: () => !!tab.pozastaveno,
                                       pozastavit: (on) => {
                                         tab.pozastaveno = !!on;
                                         if (tab.id) api('fronta-pauza', {tab: tab.id, on: !!on}).catch(() => {});
                                       },
                                       serverZnovu: (id) => { if (tab.id) api('fronta-znovu', {tab: tab.id, id}).catch(() => {}); },
                                       serverIhned: (id) => { if (tab.id) api('fronta-ihned', {tab: tab.id, id}).catch(() => {}); },
                                       // Úprava a smazání zprávy, která ještě čeká (vrací odpověď serveru, nebo null při pádu spojení).
                                       serverSeznam: async () => { try { return tab.id ? await api('fronta?tab=' + tab.id) : null; } catch (_) { return null; } },
                                       serverUprav: (id, text) => api('fronta-uprav', {tab: tab.id, id, text}),
                                       serverSmaz: (id) => api('fronta-zrus', {tab: tab.id, id}),
                                       // Majitelův chat, který už je sdílený: bubliny podle lidí.
                                       sdileny: () => { const r = relaceTabu(tab); return r ? {ja: r.majitel, majitel: r.majitel} : null; },
                                       // Tlačítko rychlé odpovědi pod odpovědí Clauda.
                                       odpovez: (text) => !!(tab.composer && tab.composer.run(text + '\r')),
                                       // Panel Průběh vpravo (renderPrehled).
                                       prehled: () => { if (ACTIVE === tab) showActionbar(tab); }});
    // Co na serveru po restartu nebo reloadu ještě čeká, se ukáže znovu — i pozastavení.
    nactiFrontu(tab);
    // Úsilí mohla bublina zjistit dřív, než vzniklo hřiště — Claudík se převlékne hned.
    if (tab.effort && tab.cteni.hriste) tab.cteni.hriste.effort(tab.effort);
  }
  if (kind === 'project' || kind.startsWith('slash:')) {
    tab.composer = HubComposer.install(tab, {
      send,
      // Průběžné uložení rozepsané zprávy (koncept) — přežije restart i reload.
      koncept: (text, atts) => ulozKoncept(tab, text, atts),
      pozastaveno: () => !!tab.pozastaveno,
      // Zpráva se uloží na server a doručí Claudovi, až je připravený (hub/fronta.py).
      naServer: async (text, id) => {
        try { const r = await api('fronta', {tab: tab.id, id, text}); return !!(r && r.ok); } catch (_) { return false; }
      },
      // Úhoz v bublině: sdílející vidí, že tu píšu.
      pise: () => { tab.pisuOd = Date.now(); },
      online,
      menu: showMenu,
      upload: uploadFiles,
      quote: shellQuote,
      skills: () => STATE.skills || [],
      // Čím Claude v tabu doopravdy odpověděl (z přepisu konverzace na serveru).
      tabModel: (id) => api('tab-model?id=' + encodeURIComponent(id)),
      // Našeptávač `@soubor` v bublině (hub/soubory_hledani.py); kořen určí server z tabu.
      souboryHledej: (id, q) => api('soubory-hledani?id=' + encodeURIComponent(id) + '&q=' + encodeURIComponent(q)),
      tabContext: (id, m1) => api('tab-context?id=' + encodeURIComponent(id) + (m1 ? '&m1=1' : '')),
      // Druhý názor na model a effort ke zprávě (hub/automodel.py, Haiku).
      autoModel: (text) => api('auto-model', {text}),
      autoKeep: (extend) => api('auto-model-keep', {extend: !!extend}),
      /* Model ze settings.json — s ním Claude Code v tomhle tabu nastartoval.
         Přepnutí si Claude Code do settings.json uloží taky, ale my ho víme
         hned, tak si ho tu rovnou přepíšeme: další tab pak ukáže to samé. */
      // Kdo běží v tomhle tabu — bublina si podle toho vybere, co umí.
      agent: () => agentById(tab.agent || STATE.default_agent),
      // Tečka v bublině je tatáž tečka jako na tabu — ať se v prostoru
      // nekouká na modrý tab s jantarovou tečkou pod ním.
      agentColor: (a) => dotColor(tab, a),
      // Co Claude zrovna dělá — čtení (cteni.js) z toho kreslí řádek práce.
      prace: (stav) => {
        // Podle tohohle aktualizace prostoru počká, až Claude doběhne.
        const pracoval = tab.pracuje;
        tab.pracuje = !!(stav && stav.on);
        if (pracoval !== tab.pracuje) {
          document.dispatchEvent(new CustomEvent('hub-prace', {detail: {tab: tab.id, on: tab.pracuje}}));
          // Tečka na tabu klidně dýchá a bublina řekne, od kdy pracuje — ať je
          // vidět, že běží, i když se kouká jinam (bez dalšího dotazování).
          tab.pracujeOd = tab.pracuje ? Date.now() : 0;
          if (tab.el) tab.el.classList.toggle('pracuje', tab.pracuje);
          paintAgent(tab);
        }
        // Doběhl v tabu, na který se zrovna nekouká — ať je to na tabu vidět.
        if (pracoval && !tab.pracuje && tab !== ACTIVE) { tab.hotovo = true; oznacTab(tab); }
        if (window.HubClaudici && tab.cteni && tab.cteni.hriste) tab.cteni.hriste.prace(tab.pracuje);
        if (tab.cteni) tab.cteni.prace(stav);
      },
      // Odeslaná zpráva — čtení ji ukáže hned, i s tím, jestli ji Claude už vidí.
      odeslano: (zprava, stav) => { if (tab.cteni) tab.cteni.odeslano(zprava, stav); },
      // Claude se ptá (karta s volbami) — tab svítí, dokud se neodpoví.
      ceka: (on) => { tab.ceka = !!on; oznacTab(tab); },
      // Claude v tabu spadl (wrapper to napsal do terminálu).
      upozorni: (text) => { if (tab.cteni) tab.cteni.upozorni(text); else toast(text); },
      agents: (ready) => agentList(!!ready),
      /* Přepnout agenta ani model v běžícím tabu nejde: je to jiný program,
         případně jiný startovací argument. Otevře se proto nový tab nad tímtéž
         projektem — a ten starý zůstane, dokud ho člověk sám nezavře. */
      openWith: (agentId, model, opts = {}) => {
        rememberAgent(tab.path, agentId);
        const a = agentById(agentId);
        openTab({kind: 'project', path: tab.path,
                 title: (a && a.label) || agentId, agent: agentId, model,
                 mode: opts.mode});
      },
      // Potvrzení varování k bypassu — pak ho nabízí každý nový tab.
      acceptBypass: () => api('bypass', {accept: true}),
      model: (key) => {
        if (key) STATE.model = key;
        return STATE.model || '';
      },
      // Na schránku bublina sama nedosáhne — obrázek si vyžádá přes server,
      // stejnou cestou jako terminál. Na serveru brány žádná schránka není:
      // obrázek tam přijde nativním vložením (paste se souborem).
      read: (which) => (onServer() ? Promise.resolve({}) : api('clipboard?which=' + which)),
      // Náhled přílohy: soubor podá server, prohlížeč na disk nevidí.
      imageUrl,
      notice: toast,
      /* Kolik místa dole si bublina ukusuje z terminálu navíc k tomu, co
         zabírá Claudeovo vlastní vstupní pole. Terminál se o to zkrátí, takže
         poslední řádky výpisu zůstanou nad bublinou, ne pod ní. */
      reserve: (px) => {
        const now = parseFloat(tab.termbox.style.bottom) || 0;
        if (Math.abs(now - px) < 1) return;
        tab.termbox.style.bottom = px > 0 ? px + 'px' : '';
        refit(tab);
      },
    });
    // Rozepsaná zpráva z minula (restart serveru, reload, zavřené okno).
    obnovKoncept(tab);
  } else {
    // Bublina se v holém shellu neinstaluje, ale řádek kláves ano — na
    // telefonu je Tab i Ctrl+C jinak nedosažitelný. Na počítači ho CSS skryje.
    tab.keys = HubComposer.installKeys(tab, send);
  }

  const el = document.createElement('button');
  el.className = 'tab';
  el.draggable = true;
  el.innerHTML = '<span class="tab-agent" hidden></span>' +
                 '<span class="tab-title"></span>' +
                 `<span class="tab-br" hidden title="Prohlížeč tohohle chatu">${icon('i-globe')}</span>` +
                 `<span class="tab-sdil" hidden>${icon('i-user')}<span></span></span>` +
                 `<span class="tab-close" title="Zavřít tab">${icon('i-close')}</span>`;
  el.querySelector('.tab-title').textContent = title;
  el.onclick = (ev) => {
    if (ev.target.closest('input')) return;       // pole na přejmenování
    if (ev.target.closest('.tab-close')) { requestCloseTab(tab); return; }
    if (ev.target.closest('.tab-br') && window.HubProhlizec) { activate(tab); HubProhlizec.ukaz(tab.id); return; }
    if (ev.target.closest('.tab-sdil')) { activate(tab); sdiletChat(tab); return; }
    activate(tab);
  };
  el.ondblclick = (ev) => { if (!ev.target.closest('.tab-close')) startRename(tab); };
  // Telefon lištu s taby schová a přejmenovává v šuplíku (mobile.js).
  el.addEventListener('hub-rename', (ev) => renameTab(tab, ev.detail));
  wireDrag(el, tab);
  $('tabbar').insertBefore(el, $('btn-new-agent'));
  tab.el = el;
  // Tab z jiného prostoru (firma × osobní) v liště tohohle okna není; běží dál.
  if (STATE && STATE.config && tabProstor(tab) !== prostor()) el.hidden = true;
  paintAgent(tab);   // až teď — dřív tab své tlačítko ještě nemá

  TABS.push(tab);
  // Seznam chatů ukazuje otevřený chat nahoře se zelenou tečkou — překreslit hned, ne až za 20 s.
  if (typeof renderChatsSoon === 'function') { chatsSig = ''; renderChatsSoon(); }
  // Při prvním načtení (restore) se nepřepíná — appka zůstane na Domů.
  // Obnovený tab z jiného prostoru okno nepřepíná — zůstane schovaný.
  if (!background || (!ACTIVE && !START_HOME && (!STATE || !STATE.config
                                                 || tabProstor(tab) === prostor()))) activate(tab);
  else if (!ACTIVE) renderWelcomeOpen();
  return tab;
}

/* Odznak agenta na tabu: barevná tečka vždy, jméno navíc u toho, kdo není
   výchozí — jinak by u každého tabu svítilo totéž slovo. */
/* Barva tečky u tabu i v bublině pod terminálem. Tečka říká, KDE se píše:
   na počítači jantarová, v prostoru na serveru modrá, nad firemním trezorem
   fialová. Agent má svou barvu jen tam, kde je vedle ní i jeho jméno —
   u výchozího agenta by jinak barva neřekla nic, co už není vidět. */
function dotColor(tab, a) {
  if (tab && isFirmaVault(tab.vault)) return 'var(--firma)';
  return (a && a.id !== (STATE.default_agent || 'claude')) ? a.color : 'var(--accent)';
}

/* Claude Code si svou oranžovou píše napevno (truecolor 215;119;87), takže na
   ni motiv terminálu nedosáhne. Přepisuje se proto rovnou v proudu, než výpis
   dojde do terminálu: tab pak celý mluví barvou prostředí — v prostoru modře,
   nad firemním trezorem fialově. Na počítači se nepřepisuje nic, tam je
   oranžová doma. */
const CC_ORANGE = /(3|4)8;2;215;119;87/g;

function recolor(tab, data) {
  let s = (tab.cut || '') + data;
  tab.cut = '';
  clearTimeout(tab.cutTimer);
  const rgb = onServer() ? rgbOf(accentHex(tab)) : '';
  if (!rgb) return s;
  /* Sekvence může přijít rozpůlená mezi dvěma zprávami. Konec, který na
     začátek sekvence vypadá, počká na zbytek — a když nic nepřijde, vypíše se
     sám, ať se terminál nezasekne na půlce escape sekvence. */
  const half = s.match(/\x1b(?:\[[0-9;]*)?$/);
  if (half) {
    tab.cut = half[0];
    s = s.slice(0, -half[0].length);
    tab.cutTimer = setTimeout(() => {
      const rest = tab.cut;
      tab.cut = '';
      if (rest) tab.term.write(rest);
    }, 60);
  }
  return s.replace(CC_ORANGE, (_m, kind) => kind + '8;2;' + rgb);
}

function paintAgent(tab) {
  // Firemní tab je fialový celý — bublina, tlačítka i rámečky uvnitř. Stačí na
  // jeho panelu přebít barvu prostředí, ostatní styly si ji čtou samy.
  if (tab.pane) tab.pane.classList.toggle('firma', isFirmaVault(tab.vault));
  const badge = tab.el && tab.el.querySelector('.tab-agent');
  if (!badge) return;
  const a = agentById(tab.agent || (tab.kind === 'shell' ? '' : STATE.default_agent));
  if (!a || tab.kind === 'shell' || tab.kind === 'deploy') {
    badge.hidden = true;
    return;
  }
  badge.hidden = false;
  const jiny = a.id !== (STATE.default_agent || 'claude');
  badge.textContent = jiny ? a.short : '';
  badge.classList.toggle('named', !!badge.textContent);
  tab.el.classList.toggle('firma', isFirmaVault(tab.vault));
  // Samotná tečka (výchozí agent) nese barvu prostředí — na počítači
  // jantarová, v prostoru modrá, nad firemním trezorem fialová. Odznak se
  // jménem si drží barvu agenta, jinak by se agenti mezi sebou nerozeznali.
  badge.style.setProperty('--agent-color', dotColor(tab, a));
  tab.el.title = a.label + (tab.model ? ' · ' + tab.model : '') + (tab.pracujeOd
    ? '\nPracuje od ' + new Date(tab.pracujeOd).toLocaleTimeString('cs-CZ', {hour: '2-digit', minute: '2-digit'}) +
      ' — nezaseklo se, jen to trvá'
    : '');
}

function measure(tab) {
  try {
    const dims = tab.fit.proposeDimensions();
    if (dims && dims.cols > 0 && dims.rows > 0) return dims;
  } catch (_) { /* pane not laid out yet */ }
  return {cols: 100, rows: 30};
}

function activate(tab) {
  // Otevřít tab jiného prostoru = přepnout na něj celé okno.
  if (tab && TABS.includes(tab) && STATE && STATE.config && tabProstor(tab) !== prostor()) {
    setProstor(tabProstor(tab), true);
  }
  ACTIVE = tab;
  document.dispatchEvent(new CustomEvent('hub-tab', {detail: tab ? {id: tab.id, title: tab.title, path: tab.path || ''} : null}));
  showProjekty(false);
  if (tab && tab.hotovo) { tab.hotovo = false; oznacTab(tab); }
  if (placeReady) writePlace({tab: tab ? tabKey(tab) : null});
  for (const t of TABS) {
    t.el.classList.toggle('active', t === tab);
    t.pane.classList.toggle('active', t === tab);
  }
  for (const o of SDILENE_TABY.values()) { o.el.classList.remove('active'); o.pane.classList.remove('active'); }
  syncShareBtn();
  // Bez tabu (activate(null)) je vidět úvod — i když jsou taby otevřené:
  // domů se dá kdykoli (logo v liště, Domů v šuplíku) a taby běží dál.
  $('welcome').hidden = !!tab;
  if (!tab) renderWelcomeOpen();
  syncActionbar(tab);
  if (tab) {
    refit(tab);
    claimSize(tab);
    // Uspaný tab (hub/uspavani.py) se otevřením probudí — i bez fokusu okna.
    if (tab.id) send({t: 'wake', id: tab.id});
    if (tab.links) tab.links.update();      // skrytý tab odkazy nečetl
    if (tab.cteni) tab.cteni.wake();        // a čtení je o dotaz pozadu
    // Na telefonu fokus nebrat: vyjela by klávesnice a zakryla půlku
    // obrazovky, i když člověk chtěl tab jen vidět. Do pole si klepne sám.
    if (!document.body.classList.contains('is-touch')) {
      tab.term.focus();
      // Když je vidět bublina, píše se do ní — fokus patří jí.
      if (tab.composer) tab.composer.focus();
    }
  }
}

/* Tab, který na mě čeká. Claude se ptá (karta s volbami v composer.js) →
   tab svítí, dokud se neodpoví — i když je člověk v jiném tabu nebo na Domů.
   Doběhl, zatímco se kouká jinam → tab má zelenou tečku, dokud se na něj
   nepřepne. Počet čekajících je i v titulku okna. */
function oznacTab(tab) {
  if (!tab.el) return;
  tab.el.classList.toggle('ceka', !!tab.ceka);
  tab.el.classList.toggle('hotovo', !!tab.hotovo && !tab.ceka);
  if (tab.cteni && tab.cteni.hriste) tab.cteni.hriste.ceka(!!tab.ceka);
  if (tab === ACTIVE) showActionbar(tab);
  document.body.classList.toggle('neco-ceka', TABS.some(t => t.ceka));
  document.title = titulekOkna(document.title);
  if (!ACTIVE) renderWelcomeOpen();
  kresliVisici();
}

/* Claudík visící z tabu, který čeká na odpověď. Lišta s taby roluje, takže
   by ho oříznula — visí proto v <body> a jen se pod tab zarovnává. Klik na
   něj tab otevře. */
let visTimer = null;
function kresliVisici() {
  for (const t of TABS) {
    const on = !!(t.ceka && t.el && t.el.isConnected && window.HubCteni && HubCteni.claudik);
    if (!on) { if (t.visi) { t.visi.remove(); t.visi = null; } continue; }
    if (!t.visi) {
      t.visi = document.createElement('button');
      t.visi.className = 'tab-visi';
      t.visi.title = 'Claude čeká na tvou odpověď';
      t.visi.innerHTML = '<span class="tv-ruce"></span>' + HubCteni.claudik('') +
                         '<span class="tv-otaznik">?</span>';
      t.visi.onclick = () => activate(t);
      if (t.effort) t.visi.dataset.effort = t.effort;
      document.body.appendChild(t.visi);
    }
    const r = t.el.getBoundingClientRect();
    const bar = $('tabbar').getBoundingClientRect();
    // Tab odrolovaný mimo lištu (nebo lišta schovaná na telefonu) — nevisí.
    const vidno = r.width > 0 && r.right > bar.left + 20 && r.left < bar.right - 20;
    t.visi.hidden = !vidno;
    t.visi.style.left = Math.round(r.left + Math.min(r.width / 2, 60)) + 'px';
    t.visi.style.top = Math.round(r.bottom - 2) + 'px';
  }
  const nekdo = TABS.some(t => t.visi);
  if (nekdo && !visTimer) visTimer = setInterval(kresliVisici, 400);
  if (!nekdo && visTimer) { clearInterval(visTimer); visTimer = null; }
}

function titulekOkna(title) {
  const base = String(title || '').replace(/^\(\d+\) /, '');
  const n = TABS.filter(t => t.ceka).length;
  return n ? `(${n}) ${base}` : base;
}

/* ── Projekty jako stránka ──────────────────────────────────────────────────
   Jako claude.ai/projects: mřížka karet (jméno, popis, kdy naposledy),
   hledání, řazení a přidání složky. V panelu vlevo jsou jen chaty. */
let ppSort = 'recent';
try { ppSort = localStorage.getItem('hub-pp-sort') || 'recent'; } catch (_) {}

function showProjekty(on) {
  const page = $('projekty-page');
  if (!page) return;
  page.hidden = !on;
  $('nav-projekty').classList.toggle('on', !!on);
  if (!on) return;
  if (ACTIVE) activate(null);        // taby běží dál, jen se na ně nekouká
  page.hidden = false;
  $('nav-projekty').classList.add('on');
  $('welcome').hidden = true;
  renderProjekty();
  if (window.HubMobile) HubMobile.closeDrawer();
  if (!document.body.classList.contains('is-touch')) $('pp-q').focus();
}

/* ── Klienti: Projekty ve firmě ────────────────────────────────────────────
   Klienti a podkategorie, do kterých člověk vidí (core.firma_klienti čte
   trezor tak, jak je v sandboxu). U každé podkategorie chat přímo v ní. */
const KLIENTI = {};                    // zkratka → [{nazev, podkategorie}]
async function loadKlienti(slug) {
  try {
    KLIENTI[slug] = (await api('firma-klienti?firma=' + encodeURIComponent(slug))).klienti || [];
  } catch (_) { KLIENTI[slug] = KLIENTI[slug] || []; }
  if (prostor() !== slug) return;
  if (!$('projekty-page').hidden) renderProjekty();
  if (!ACTIVE) renderWelcomeFirmy();
}
function chatuV(path) {
  return CHATS.filter((c) => c.cwd === path || String(c.cwd || '').startsWith(path + '/')).length;
}
function chatyText(n) { return n === 1 ? '1 chat' : n >= 2 && n <= 4 ? n + ' chaty' : n + ' chatů'; }

/* Jeden řádek „podkategorie · počet chatů · + chat". */
function klientRadek(f, klient, podkat) {
  const row = document.createElement('div');
  row.className = 'kl-row';
  const name = document.createElement('button');
  name.className = 'kl-name';
  const projekt = jeProjekty(f);
  name.textContent = podkat || (klient ? (projekt ? 'Chat v projektu' : 'Obecně o klientovi') : 'Celá firma');
  name.title = 'Otevřít chat ' + (podkat ? 'v podkategorii ' + podkat
    : klient ? (projekt ? 'v projektu ' : 'o klientovi ') + klient : 'celé firmy');
  name.onclick = () => openFirmaTab(f.zkratka, klient, podkat);
  const n = chatuV(firmaPrace(f.zkratka, klient, podkat));
  const meta = document.createElement('span');
  meta.className = 'kl-meta';
  meta.textContent = n ? chatyText(n) : '';
  const plus = document.createElement('button');
  plus.className = 'btn ghost kl-plus';
  plus.innerHTML = icon('i-plus') + '<span>chat</span>';
  plus.onclick = name.onclick;
  row.append(name, meta, plus);
  return row;
}

function renderKlienti(f) {
  const grid = $('pp-grid');
  const projekty = jeProjekty(f);
  $('pp-title').textContent = (projekty ? 'Projekty' : 'Klienti') + ' — ' + f.nazev;
  $('pp-sort').hidden = true;
  $('pp-q').parentElement.hidden = true;
  $('pp-new').textContent = f.role === 'kapitan' ? 'Spravovat firmu' : 'Obsidian firmy';
  $('pp-new').onclick = () => (f.role === 'kapitan' ? spravaFirem() : openVault('', firmaVaultId(f)));
  grid.textContent = '';
  if (!KLIENTI[f.zkratka]) { loadKlienti(f.zkratka); }
  const firma = document.createElement('div');
  firma.className = 'pp-card kl-card kl-firma';
  firma.innerHTML = '<div class="pp-top"><span class="pp-ico"></span><span class="pp-name"></span></div>' +
                    '<div class="pp-desc"></div>';
  firma.querySelector('.pp-ico').innerHTML = icon('i-hub');
  firma.querySelector('.pp-name').textContent = f.nazev;
  firma.querySelector('.pp-desc').textContent = 'Obecné věci firmy — postupy, know-how, firemní skilly. ' +
    'Tvoje role: ' + ROLE_FIRMY[f.role] + '.';
  firma.appendChild(klientRadek(f));
  const ob = document.createElement('button');
  ob.className = 'linkbtn kl-link';
  ob.textContent = 'Otevřít Obsidian firmy ›';
  ob.onclick = () => openVault('', firmaVaultId(f));
  firma.appendChild(ob);
  grid.appendChild(firma);
  const klienti = KLIENTI[f.zkratka] || [];
  if (KLIENTI[f.zkratka] && !klienti.length) {
    grid.appendChild(Object.assign(document.createElement('div'), {className: 'pp-empty',
      textContent: projekty
        ? (f.role === 'kapitan' ? 'Zatím tu nejsou žádné projekty — přidáš je ve Spravovat firmu.'
                                : 'Zatím ti kapitán nezpřístupnil žádný projekt.')
        : (f.role === 'kapitan' ? 'Zatím tu nejsou žádní klienti — přidáš je ve Spravovat firmu.'
                                : 'Zatím ti kapitán nezpřístupnil žádného klienta.')}));
  }
  for (const k of klienti) {
    const card = document.createElement('div');
    card.className = 'pp-card kl-card';
    card.innerHTML = '<div class="pp-top"><span class="pp-ico"></span><span class="pp-name"></span></div>';
    card.querySelector('.pp-ico').innerHTML = icon('i-folder');
    card.querySelector('.pp-name').textContent = k.nazev;
    card.appendChild(klientRadek(f, k.nazev));
    for (const p of k.podkategorie) card.appendChild(klientRadek(f, k.nazev, p));
    if (!projekty && !k.podkategorie.length) {
      card.appendChild(Object.assign(document.createElement('div'), {className: 'kl-meta kl-none',
        textContent: f.role === 'kapitan' ? 'Zatím bez podkategorií — přidáš je ve Spravovat firmu.'
                                          : 'Žádná podkategorie, do které vidíš.'}));
    }
    grid.appendChild(card);
  }
}

function renderProjekty() {
  const grid = $('pp-grid');
  const fp = prostorFirma();
  if (fp) return renderKlienti(fp);
  // Osobní prostor: Projekty jako dřív (Klienti si stránku přestaví).
  $('pp-title').textContent = 'Projekty';
  $('pp-sort').hidden = false;
  $('pp-q').parentElement.hidden = false;
  $('pp-new').textContent = 'Otevřít složku…';
  $('pp-new').onclick = () => openPicker('');
  const needle = foldText($('pp-q').value.trim());
  const showArchived = !!STATE.config.show_archived;
  let list = STATE.projects.filter(p => (showArchived || !p.archived) &&
    (!needle || foldText((p.label || '') + ' ' + p.name + ' ' + (p.brief || '')).includes(needle)));
  const cena = (p) => (USAGE[p.path] || {}).cost || 0;
  list = ppSort === 'name'
    ? list.sort((a, b) => (a.label || a.name).localeCompare(b.label || b.name, 'cs'))
    : ppSort === 'cena'
      ? list.sort((a, b) => cena(b) - cena(a) || (b.mtime || 0) - (a.mtime || 0))
      : list.sort((a, b) => (b.mtime || 0) - (a.mtime || 0));
  $('pp-sort').textContent = ppSort === 'name' ? 'Podle jména' : ppSort === 'cena' ? 'Podle využití tokenů' : 'Naposledy upravené';
  grid.textContent = '';
  if (!list.length) {
    grid.appendChild(Object.assign(document.createElement('div'),
      {className: 'pp-empty', textContent: needle ? 'Nic nenalezeno.' : 'Zatím žádné projekty — přidej složku tlačítkem nahoře.'}));
    return;
  }
  for (const p of list) {
    const card = document.createElement('div');
    card.className = 'pp-card' + (p.archived ? ' archived' : '');
    card.tabIndex = 0;
    card.innerHTML = '<div class="pp-top"><span class="pp-ico"></span><span class="pp-name"></span>' +
      '<button class="pp-more" title="Možnosti">⋯</button></div>' +
      '<div class="pp-desc"></div><div class="pp-foot"><span class="pp-when"></span><span class="pp-tags"></span></div>';
    const ico = card.querySelector('.pp-ico');
    if (p.image) {
      const img = document.createElement('img');
      img.src = imageUrl(p.image);
      img.alt = '';
      ico.appendChild(img);
    } else {
      ico.innerHTML = icon('i-folder');
    }
    card.querySelector('.pp-name').textContent = p.label || p.name;
    // Popis projektu (brief), jinak nic — cesta a větev jsou pro vývojáře.
    const desc = (p.brief || '').replace(/^#.*$/gm, '').replace(/[*_`>#-]/g, ' ').replace(/\s+/g, ' ').trim();
    card.querySelector('.pp-desc').textContent = desc.slice(0, 160) || (devMode() ? shortPath(p.path) : '');
    card.querySelector('.pp-when').textContent = p.mtime ? 'Upraveno ' + kdy(p.mtime) : '';
    const tags = card.querySelector('.pp-tags');
    const u = USAGE[p.path];
    if (u && (u.out || u.cost)) {
      const chip = Object.assign(document.createElement('button'), {className: 'pp-tag pp-usage',
        textContent: HubStats.dolary(u.cost) + ' · ' + HubStats.cislo(u.out) + ' tok.'});
      chip.title = 'Využití tokenů, odhad ceny podle ceníku API — klikni pro statistiky';
      chip.onclick = (ev) => { ev.stopPropagation(); HubStats.openProject(hubIO(), p); };
      tags.appendChild(chip);
    }
    if (devMode()) {
      if (p.branch) tags.appendChild(Object.assign(document.createElement('span'), {className: 'pp-tag', textContent: p.branch}));
      if (p.dirty) tags.appendChild(Object.assign(document.createElement('span'), {className: 'pp-tag dirty', textContent: p.dirty + ' změn'}));
    }
    const open = () => openTab({kind: 'project', path: p.path, title: p.label || p.name, agent: agentFor(p.path)});
    card.onclick = (ev) => { if (!ev.target.closest('.pp-more')) open(); };
    card.onkeydown = (ev) => { if (ev.key === 'Enter') { ev.preventDefault(); open(); } };
    card.oncontextmenu = (ev) => { ev.preventDefault(); projectMenu(ev, p); };
    card.querySelector('.pp-more').onclick = (ev) => {
      ev.stopPropagation();
      const r = ev.currentTarget.getBoundingClientRect();
      projectMenu({clientX: r.left, clientY: r.bottom, preventDefault() {}}, p);
    };
    grid.appendChild(card);
  }
}

/* Úvodní stránka i s otevřenými taby — taby běží dál, klik na tab vrátí zpátky. */
function goHome() {
  activate(null);
  if (window.HubMobile) HubMobile.closeDrawer();
}
window.hubHome = goHome;
window.hubFirmaChat = () => openFirmaTab();

function refit(tab) {
  if (!tab || tab.pane.offsetWidth === 0) return;
  /* Jiná šířka přeformátuje celý výpis (reflow) a Claude Code překreslí celé
     TUI — při tažení okna to byla desetina vteřiny za každý krok. Jako VS Code
     (TerminalResizeDebouncer): výška hned, šířka až po 100 ms klidu; krátký
     výpis (nový tab) hned celý. */
  let dims = null;
  try { dims = tab.fit.proposeDimensions(); } catch (_) { return; }
  if (dims && dims.cols !== tab.term.cols && !tab.sirkaHned &&
      tab.term.buffer.normal.length >= 200) {
    clearTimeout(tab.sirkaTimer);
    tab.sirkaTimer = setTimeout(() => {
      tab.sirkaHned = true;
      refit(tab);
      tab.sirkaHned = false;
    }, 100);
    if (dims.rows !== tab.term.rows && !isNaN(dims.rows)) tab.term.resize(tab.term.cols, dims.rows);
  } else {
    try { tab.fit.fit(); } catch (_) { return; }
  }
  if (!tab.id) return;
  /* Stejný rozměr se posílat nemusí — a hlavně nemá: ConPTY na Windows
     překreslí při resize celou obrazovku i tehdy, když se nic nezměnilo.
     Přepočet se přitom spouští z několika stran (ResizeObserver, změna okna,
     rezervace místa pro bublinu), takže se sem chodí i bez změny velikosti. */
  if (tab.sentCols === tab.term.cols && tab.sentRows === tab.term.rows) return;
  tab.sentCols = tab.term.cols;
  tab.sentRows = tab.term.rows;
  send({t: 'resize', id: tab.id, cols: tab.sentCols, rows: tab.sentRows});
}

/* Tohle okno se teď používá — pty ať má jeho rozměr. Okno na pozadí si taby
 * aktivuje taky (po znovupřipojení), a to rozměr brát nesmí: přetahovalo by ho
 * tomu, kdo zrovna pracuje jinde. Psaní si rozměr bere samo na serveru. */
function claimSize(tab) {
  if (tab && tab.id && document.hasFocus()) send({t: 'focus', id: tab.id});
}

/* Potvrzení „Ano / Ne" jako slib. Vrací true, když člověk klikl na Ano.
 * Otevřený dialog nikdy nezdvojujeme — druhé volání počká na to první. */
let confirmPending = null;
function askConfirm({title, html, yes = 'Ano', no = 'Ne'}) {
  if (confirmPending) return confirmPending;
  const box = $('confirm');
  $('confirm-title').textContent = title;
  $('confirm-text').innerHTML = html;
  $('confirm-yes').textContent = yes;
  $('confirm-no').textContent = no;
  box.hidden = false;
  $('confirm-yes').focus();
  confirmPending = new Promise((resolve) => {
    const finish = (answer) => {
      box.hidden = true;
      document.removeEventListener('keydown', onKey, true);
      confirmPending = null;
      resolve(answer);
    };
    const onKey = (ev) => {
      if (ev.key === 'Escape') { ev.stopPropagation(); finish(false); }
      if (ev.key === 'Enter') { ev.stopPropagation(); finish(true); }
    };
    $('confirm-yes').onclick = () => finish(true);
    $('confirm-no').onclick = () => finish(false);
    document.addEventListener('keydown', onKey, true);
  });
  return confirmPending;
}

/* Pracuje v tabu zrovna něco? Claude Code při práci točí ukazatel a posílá
 * výpis několikrát za sekundu; v klidu mlčí. Jeden dva kusy výpisu (třeba
 * překreslení po ztrátě fokusu kliknutím na křížek) za práci nepovažujeme. */
function isBusy(tab) {
  const now = Date.now();
  return !!tab.outTimes && tab.outTimes.filter((t) => now - t < 2000).length >= 6;
}

/* Zavření tabu se neptá: do paměti se session uloží sama (memory-autosave.py)
 * a přerušená konverzace jde vrátit přes `claude --resume`. Ptá se jen, když
 * by se zavřením utnula rozdělaná práce. */
async function requestCloseTab(tab) {
  if (tab.exited || !tab.id || !isBusy(tab)) { closeTab(tab); return; }
  const ok = await askConfirm({
    title: 'Claude ještě pracuje',
    html: `V <b>${escapeHtml(tab.title)}</b> se pořád něco děje.` +
          `<span class="hint">Zavřením to přerušíš. Co je hotové, se do paměti uloží samo.</span>`,
    yes: 'Přerušit a zavřít',
    no: 'Nechat běžet',
  });
  if (ok) closeTab(tab);
}

function escapeHtml(text) {
  const d = document.createElement('div');
  d.textContent = text;
  return d.innerHTML;
}

/* `remote` = zavřelo ho jiné okno: server už o tom ví, tady se jen uklidí. */
function closeTab(tab, {remote = false} = {}) {
  if (tab.id && !remote) send({t: 'close', id: tab.id});
  zapamatujZavreny(tab);
  /* Úklid nesmí uprostřed spadnout — tab by zůstal napůl: terminál bez
     vykreslování v okně a každý pohyb myši nad ním by házel chybu. */
  const uklid = (f) => { try { f(); } catch (e) { reportToLog('zavření tabu: ' + (e && e.message || e)); } };
  if (tab.releaseIME) uklid(tab.releaseIME);
  if (tab.releaseClipboard) uklid(tab.releaseClipboard);
  if (tab.composer) uklid(() => tab.composer.release());
  if (tab.cteni) uklid(() => tab.cteni.release());
  if (tab.keys) uklid(() => tab.keys.release());
  if (tab.links) uklid(() => tab.links.release());
  /* Claude Code zapíná v terminálu režim myši. xterm pak při tažení poslouchá
     pohyb a puštění tlačítka na celém dokumentu — a když se puštění ztratí
     (pustil jsi ho mimo okno), visí to dál. Po zavření tabu by každý pohyb myši
     kdekoli na stránce hodil chybu (`dimensions`, až stovky za vteřinu).
     Puštění tlačítka mu proto pošleme, dokud terminál ještě žije. */
  uklid(() => { if (tab.term.element) tab.term.element.dispatchEvent(new MouseEvent('mouseup', {bubbles: true})); });
  uklid(() => tab.term.dispose());
  tab.el.remove();
  tab.pane.remove();
  TABS = TABS.filter(t => t !== tab);
  chatsSig = '';
  renderChatsSoon();
  if (tab.visi) { tab.visi.remove(); tab.visi = null; }
  document.body.classList.toggle('neco-ceka', TABS.some(t => t.ceka));
  document.title = titulekOkna(document.title);
  if (ACTIVE === tab) ACTIVE = null;
  // Další tab jen z prostoru okna — zavřením se okno do jiné firmy nepřepne.
  const here = STATE && STATE.config ? TABS.filter((t) => tabProstor(t) === prostor()) : TABS;
  const next = here[here.length - 1];
  if (next) activate(next);
  else { $('welcome').hidden = false; $('clockify').hidden = true; $('actionbar').hidden = true; }
  renderWelcomeOpen();
}

function startRename(tab) {
  const holder = tab.el.querySelector('.tab-title');
  const input = document.createElement('input');
  input.value = tab.title;
  input.maxLength = 60;
  holder.textContent = '';
  // Tab je tlačítko: mezera v poli by ho „stiskla" (keyup → click), tab by se
  // přepnul, pole ztratilo fokus a z názvu by zbylo jen první slovo. Tažení
  // tabu by zase bralo výběr textu myší.
  tab.el.draggable = false;
  holder.appendChild(input);
  input.focus();
  input.select();
  let done = false;
  const finish = () => {
    if (done) return;
    done = true;
    tab.el.draggable = true;
    renameTab(tab, input.value);
  };
  input.onblur = finish;
  input.onkeydown = (ev) => {
    if (ev.key === 'Enter') { ev.preventDefault(); finish(); }
    if (ev.key === 'Escape') {
      ev.preventDefault();
      done = true;
      tab.el.draggable = true;
      holder.textContent = tab.title;
    }
    ev.stopPropagation();
  };
  input.onkeyup = (ev) => {
    if (ev.key === ' ') ev.preventDefault();
    ev.stopPropagation();
  };
  input.onclick = (ev) => ev.stopPropagation();
  input.ondblclick = (ev) => ev.stopPropagation();
}

function renameTab(tab, value) {
  const name = String(value || '').replace(/\s+/g, ' ').trim().slice(0, 60) || tab.title;
  tab.title = name;
  tab.el.querySelector('.tab-title').textContent = name;
  if (!ACTIVE) renderWelcomeOpen();
  if (tab.id) send({t: 'rename', id: tab.id, title: name});
}

/* Název změněný v jiném okně. Kdo ho tu zrovna přepisuje, tomu se nepřepíše. */
function setTitle(tab, name) {
  tab.title = name;
  if (!ACTIVE) renderWelcomeOpen();
  const holder = tab.el.querySelector('.tab-title');
  if (!holder.querySelector('input')) holder.textContent = name;
}

let dragged = null;
function wireDrag(el, tab) {
  el.ondragstart = () => { dragged = tab; el.classList.add('dragging'); };
  el.ondragend = () => { dragged = null; el.classList.remove('dragging'); };
  el.ondragover = (ev) => ev.preventDefault();
  el.ondrop = (ev) => {
    ev.preventDefault();
    if (!dragged || dragged === tab) return;
    $('tabbar').insertBefore(dragged.el, el);
    const order = [...$('tabbar').querySelectorAll('.tab')];
    TABS.sort((a, b) => order.indexOf(a.el) - order.indexOf(b.el));
  };
}

/* ── pasted and dropped files ─────────────────────────────────────────────── */
/* A screenshot on the clipboard has no path, and a dropped file's real path is
 * deliberately hidden from the page — but a path is the only thing a terminal
 * can be handed. So we write our own copy through the server and type that. */
function fileToBase64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(new Error('nejde přečíst'));
    reader.onload = () => resolve(String(reader.result).split(',', 2)[1] || '');
    reader.readAsDataURL(file);
  });
}

function shellQuote(p) {
  return /[\s"'`$\\]/.test(p) ? `'${p.replace(/'/g, `'\\''`)}'` : p;
}

/* Soubory na server a zpátky jejich cesty. Odděleně od vkládání do promptu,
   protože totéž potřebuje i bublina composeru. */
async function uploadFiles(fileList) {
  const files = [...fileList].filter(f => f && f.size);
  const paths = [];
  for (const file of files) {
    try {
      const data = await fileToBase64(file);
      const res = await api('upload', {name: file.name || 'obrazek.png', data});
      if (res.path) paths.push(res.path);
    } catch (err) {
      toast(`Nepodařilo se přiložit ${file.name || 'soubor'}: ${err.message}`);
    }
  }
  return paths;
}

async function attachFiles(tab, fileList) {
  if (!tab.id) return;
  typePaths(tab, await uploadFiles(fileList));
}

/* Cesta k souboru napsaná na prompt — to je jediné, co terminál od obrázku
 * vezme. Chodí sem vložené i přetažené soubory a screenshot ze schránky. */
function typePaths(tab, paths) {
  if (!paths.length || !tab.id) return;
  // Když je vidět bublina, patří cesta do ní — do terminálu by se napsala
  // pod ni, do pole, které není vidět.
  // V bublině je po přiložení vidět náhled, ten mluví za sebe. Do terminálu
  // se píše holá cesta, a tam se hodí říct, co se vlastně stalo.
  if (tab.composer && tab.composer.insertPaths(paths)) return;
  send({t: 'in', id: tab.id, d: paths.map(shellQuote).join(' ') + ' '});
  tab.term.focus();
  toast(paths.length === 1 ? 'Přiloženo: ' + paths[0]
                           : `Přiloženo ${paths.length} souborů`);
}

function wireFiles(tab) {
  const pane = tab.pane;
  // Capture on the pane so we get there before xterm's own textarea handler:
  // it would otherwise paste the file's *name* as text. Plain text pastes are
  // left alone — those xterm does right.
  // Na počítači sem Ctrl+V nedojde: clipboard.js ho chytá na keydown a ruší mu
  // výchozí chování — obrázek řeší cestou přes server. Tenhle posluchač je tam
  // pro Shift+Insert a všechno ostatní, co paste doopravdy vyvolá. Na serveru
  // (brána) jde Ctrl+V právě sem: schránku tam má jen prohlížeč.
  pane.addEventListener('paste', (ev) => {
    const cd = ev.clipboardData;
    if (!cd || !cd.files || !cd.files.length) return;
    // Text má přednost: zkopírovaný soubor ze správce souborů nese vedle sebe
    // i svoje jméno jako text, a kdo kopíroval text, čeká text. Sáhneme po
    // souborech jen tehdy, když na schránce nic textového není — to je případ
    // screenshotu. (Vkládání textu už jednou padlo na tom, že to bylo obráceně.)
    if (cd.getData && cd.getData('text/plain')) return;
    ev.preventDefault();
    ev.stopPropagation();
    // Bublina si o obrázek umí říct serveru sama (pod WebKitGTK jinak nemá
    // jak). Když ho ale podá prohlížeč, musí o tom vědět — jinak by se
    // tentýž screenshot přiložil dvakrát, každou cestou jednou.
    if (tab.composer) tab.composer.browserPaste();
    attachFiles(tab, cd.files);
  }, true);
  pane.addEventListener('dragover', (ev) => {
    if (!ev.dataTransfer || ![...ev.dataTransfer.types].includes('Files')) return;
    ev.preventDefault();
    pane.classList.add('dropping');
  });
  pane.addEventListener('dragleave', (ev) => {
    if (ev.target === pane) pane.classList.remove('dropping');
  });
  pane.addEventListener('drop', (ev) => {
    const files = ev.dataTransfer && ev.dataTransfer.files;
    if (!files || !files.length) return;
    ev.preventDefault();
    pane.classList.remove('dropping');
    attachFiles(tab, files);
  });
}

/* Slash commands: type the text, then send Enter as its own keystroke a moment
 * later — a \r bundled with pasted text reads as a newline, not as submit. */
function runSlash(cmd) {
  if (!ACTIVE || !ACTIVE.id) return;
  if (ACTIVE.composer && ACTIVE.composer.run(cmd)) return;
  const id = ACTIVE.id;
  if (cmd.endsWith('\r')) {
    send({t: 'in', id, d: cmd.slice(0, -1)});
    setTimeout(() => send({t: 'in', id, d: '\r'}), 180);
  } else {
    send({t: 'in', id, d: cmd});
  }
  ACTIVE.term.focus();
}

/* ── context menu ─────────────────────────────────────────────────────────── */
function projectMenu(ev, p) {
  const title = p.label || p.name;
  const ready = agentList(true);
  const preferred = agentFor(p.path);
  // Nejdřív ten, kterým se projekt otevírá teď — ať je klik na kartu a první
  // položka v nabídce totéž.
  ready.sort((a, b) => (b.id === preferred) - (a.id === preferred));
  const items = ready.map((a) => ({
    icon: 'i-terminal', label: 'Otevřít v ' + a.label,
    on: a.id === preferred && ready.length > 1,
    run: () => openWith(a.id, {path: p.path, title}),
  }));
  if (!ready.length) {
    items.push({icon: 'i-terminal', label: 'Žádný agent není nainstalovaný…',
                run: () => HubSettings.open({...hubIO(), state: STATE, tab: 'agenti'})});
  }
  items.push({icon: 'i-note', label: 'Upravit…', run: () => editProject(p)});
  items.push({icon: 'i-chart', label: 'Statistiky a tokeny…',
              run: () => HubStats.openProject(hubIO(), p)});
  if (devMode()) {
    items.push({icon: 'i-deploy', label: p.deployable ? 'Deploy (FTP)' : 'Deploy',
      run: () => openTab({kind: 'deploy', path: p.path, title: 'deploy: ' + p.name})});
    if (p.repo) {
      items.push({icon: 'i-push', label: 'Otevřít na GitHubu',
        run: () => openExternal('https://github.com/' + p.repo)});
    }
  }
  if (window.HUB_ADVANCED) {
    items.push({icon: 'i-terminal', label: 'Terminál tady',
                run: () => openTab({kind: 'shell', path: p.path, title: p.name})});
  }
  items.push(
    {icon: 'i-folder', label: 'Otevřít složku', run: () => openExternal(p.path)},
    {icon: 'i-save', label: p.archived ? 'Vrátit z archivu' : 'Archivovat',
     run: async () => {
       await api('project', {action: 'save', path: p.path, archived: !p.archived});
       await reload();
     }},
    {icon: 'i-close', label: 'Odebrat ze seznamu', run: () => removeProject(p)},
  );
  showMenu(ev.clientX, ev.clientY, items);
}

/* Odebrání je jen o panelu — složka na disku zůstává. Kdyby to mazalo soubory,
   byla by to poslední věc, kterou by kdo od launcheru čekal. */
async function removeProject(p) {
  if (!(await HubDialog.confirm(`Odebrat „${p.label || p.name}" z Hubu?\n\n` +
               `Složka na disku zůstane, maže se jen z panelu:\n${p.path}`, {title: 'Odebrat projekt', ok: 'Odebrat'}))) return;
  try {
    const r = await api('project', {action: 'remove', path: p.path});
    if (r.rescanned) {
      await api('project', {action: 'save', path: p.path, archived: true});
      toast('Leží v nastavené složce, tak jsem ho aspoň archivoval.');
    } else {
      toast('Odebráno z panelu.');
    }
    await reload();
  } catch (err) { toast(err.message); }
}

/* Nabídka se otevírá pod kurzorem, takže uvolnění téhož kliknutí, kterým ji
   člověk vyvolal, dopadne rovnou na první položku a spustí ji. Proto se kreslí
   kousek vedle a chvíli po otevření kliknutí ignoruje. */
const MENU_ARM_MS = 300;
let menuArmedAt = 0;

function showMenu(x, y, items, opts) {
  const menu = $('ctxmenu');
  menu.textContent = '';
  for (const item of items) {
    // Nadpis skupiny (např. „Prostory, kam máš přístup“) — neklikací řádek.
    if (item.heading) {
      const h = document.createElement('div');
      h.className = 'ctx-head';
      h.textContent = item.heading;
      menu.appendChild(h);
      continue;
    }
    const el = document.createElement('button');
    // Vybraná položka (model, režim) se pozná fajfkou místo vlastní ikony.
    el.innerHTML = icon(item.on ? 'i-check' : item.icon) + '<span></span>';
    if (item.on) el.classList.add('on');
    el.querySelector('span').textContent = item.label;
    /* Vlastní barva položky — v prostoru je osobní modrý a firemní fialový,
       stejně jako tlačítka v liště a tečka na tabu. V nabídce to musí být
       poznat taky, jinak se do firemního trezoru klikne omylem. */
    if (item.color) {
      el.style.color = item.color;
      el.querySelector('.ico').style.color = item.color;
    }
    el.onclick = (ev) => {
      if (Date.now() - menuArmedAt < MENU_ARM_MS) {
        ev.preventDefault();
        ev.stopPropagation();
        return;                       // pořád doznívá kliknutí, které ji otevřelo
      }
      hideMenu();
      item.run();
    };
    menu.appendChild(el);
  }
  menu.hidden = false;
  menuArmedAt = Date.now();
  const box = menu.getBoundingClientRect();
  // +3 px, ať kurzor nestojí přímo na první položce
  menu.style.left = Math.min(x + 3, innerWidth - box.width - 8) + 'px';
  // Nabídka z bubliny musí růst nahoru — dole už není kam.
  const top = (opts && opts.above) ? y - box.height : y + 3;
  menu.style.top = Math.max(8, Math.min(top, innerHeight - box.height - 8)) + 'px';
}

function hideMenu() { $('ctxmenu').hidden = true; }

/* Nabídka lišty na telefonu. Čtyři ikony vedle sebe se na úzkou obrazovku
   nevejdou a na to, co která dělá, se palcem netrefí — tak je všechno pod
   jednou: kam se dívat (projekty, konverzace, Obsidiany) a čím to obsloužit
   (nastavení, vzhled, statistiky, načíst znovu). */
/* Co se dá otevřít — na telefonu pod jedním „+". Stejné volby jako tlačítka
   v liště na počítači, jen pod sebou a s místem pro palec. */
function newTabMenu(btn) {
  const cfg = STATE.config.newtab || {};
  const items = [];
  const a = agentById(STATE.default_agent) || agentList(true)[0];
  if (a && cfg.agent !== false && cfg.claude !== false) {
    const f = prostorFirma();
    items.push({icon: 'i-terminal', label: f ? 'Nový chat · ' + f.nazev : newTabLabel(),
                color: f ? 'var(--firma)' : 'var(--accent)', run: () => newChatHere()});
  }
  if (cfg.shell !== false && STATE.config.dev_mode) {
    items.push({icon: 'i-terminal', label: 'Terminál',
                run: () => openTab({kind: 'shell', path: '', title: 'Terminál'})});
  }
  // Víc agentů = ať jde vybrat i jiný než výchozí; na počítači je to pravé
  // tlačítko, kterým se na telefonu klepnout nedá.
  if (window.HUB_ADVANCED && agentList(true).length > 1) {
    items.push({icon: 'i-gear', label: 'Jiným agentem…',
                run: () => newAgentMenu({clientX: 12, clientY: 60, preventDefault() {}})});
  }
  const box = btn.getBoundingClientRect();
  showMenu(box.left, box.bottom, items);
}

function topbarMenu(btn) {
  const drawer = window.HubMobile;
  const open = (view) => {
    sidebarView(view);
    if (drawer) drawer.openDrawer();
  };
  /* Bez fajfky a bez zvýraznění: „Projekty" tu nejsou zapnutá volba, ale
     místo, kam se jde. Modrá s fajfkou vypadala, jako by na ně bylo právě
     kliknuto. */
  const items = [];
  // Průběh chatu, na který se zrovna kouká (na počítači je vpravo).
  if (ACTIVE && ACTIVE.cteni && ACTIVE.cteni.prehled) {
    items.push({icon: 'i-status', label: 'Průběh', color: 'var(--accent)', run: () => openPrehled(ACTIVE)});
  }
  // Sdílení chatu: na telefonu není lišta s tlačítkem, tak je to tady.
  if (onServer() && ACTIVE && (ACTIVE.chat || ACTIVE.resume) && ACTIVE.kind !== 'shell') {
    const rel = relaceTabu(ACTIVE);
    items.push({icon: 'i-user', label: rel ? 'Sdílení chatu (' + (rel.clenove.length + (rel.skupiny || []).length) + ')…' : 'Sdílet chat…',
                color: 'var(--accent)', run: () => sdiletChat(ACTIVE)});
  }
  items.push(
    {icon: 'i-note', label: 'Chaty', run: () => open('chats')},
    {icon: 'i-folder', label: 'Projekty', run: () => showProjekty(true)},
  );
  // Poznámky jen v Obsidianu (přepínač osobní / firemní je uvnitř).
  const mem = STATE.memory || {};
  if (mem.enabled || hasFirma()) {
    items.push({icon: 'i-book', label: 'Obsidian',
                run: () => (window.HubVault ? openVault() : openExternal('', 'brain'))});
  }
  items.push(
    {icon: 'i-gear', label: 'Nastavení',
     run: () => HubSettings.open({...hubIO(), state: STATE})},
    {icon: DARK ? 'i-sun' : 'i-moon', label: DARK ? 'Světlý vzhled' : 'Tmavý vzhled',
     run: () => setTheme(!DARK, true)},
    {icon: 'i-chart', label: 'Statistiky', run: () => HubStats.open(hubIO())},
    {icon: 'i-refresh', label: 'Načíst znovu', run: () => reload()},
  );
  // Instalace na plochu (PWA) — jen v prohlížeči, ne když už hub běží z ikony.
  const inst = window.HubInstall && HubInstall.kind();
  if (inst) {
    items.push({icon: 'i-phone', label: 'Nainstalovat na plochu', color: 'var(--accent)',
                run: () => HubInstall.run(toast)});
  }
  const box = btn.getBoundingClientRect();
  // Zprava: nabídka se otevírá pod ikonou a nesmí přetéct za okraj.
  showMenu(box.right - 230, box.bottom - 3, items);
}

/* ── folder picker ────────────────────────────────────────────────────────── */
let pickerPath = '';
let pickerResolve = null;

/* Vybrat složku a dostat ji zpátky — průvodce i nastavení potřebují cestu,
   ne otevřený tab. */
function pickFolder(start) {
  return new Promise((resolve) => {
    if (pickerResolve) pickerResolve(null);   // předchozí výběr už nikoho nezajímá
    pickerResolve = resolve;
    openPicker(start || '');
  });
}

function settlePicker(value) {
  const resolve = pickerResolve;
  pickerResolve = null;
  if (resolve) resolve(value);
  return !!resolve;
}

async function openPicker(path) {
  const data = await api('listdir?path=' + encodeURIComponent(path || ''));
  pickerPath = data.path;
  $('modal').hidden = false;
  $('modal-path').textContent = data.path;
  const roots = $('modal-roots');
  roots.textContent = '';
  for (const r of data.roots) {
    const el = document.createElement('button');
    el.textContent = r.name;
    el.onclick = () => openPicker(r.path);
    roots.appendChild(el);
  }
  const list = $('modal-list');
  list.textContent = '';
  if (data.parent) {
    const up = document.createElement('button');
    up.innerHTML = icon('i-up') + '<span>..</span>';
    up.onclick = () => openPicker(data.parent);
    list.appendChild(up);
  }
  for (const d of data.dirs) {
    const el = document.createElement('button');
    el.innerHTML = icon('i-folder') + '<span></span>';
    el.querySelector('span').textContent = d.name;
    el.onclick = () => openPicker(d.path);
    list.appendChild(el);
  }
}

function closePicker() { $('modal').hidden = true; settlePicker(null); }

/* ── websocket ────────────────────────────────────────────────────────────── */
function connect() {
  const wsProto = location.protocol === 'https:' ? 'wss://' : 'ws://';
  WS = new WebSocket(`${wsProto}${location.host}/ws?t=${encodeURIComponent(TOKEN)}`);
  WS.onopen = () => {
    WS.openedAt = Date.now();
    WS.send(JSON.stringify({t: 'hello'}));
  };
  // Binární zprávy jsou jen snímky prohlížeče v appce (prohlizec.js) — JPEG bez base64.
  WS.binaryType = 'arraybuffer';
  WS.onmessage = (ev) => {
    if (typeof ev.data !== 'string') { if (window.HubProhlizec && HubProhlizec.naSnimek) HubProhlizec.naSnimek(ev.data); return; }
    handle(JSON.parse(ev.data));
  };
  WS.addEventListener('open', () => document.dispatchEvent(new Event('hub-ws-open')));
  WS.onclose = () => setTimeout(connect, 1000);
}

// Přepnutí do tohohle okna = pracuje se tady, rozměr terminálu patří jemu.
window.addEventListener('focus', () => claimSize(ACTIVE));

/* Tab, na který se člověk dívá, se neuspává (hub/uspavani.py), i když v něm
   dlouho jen čte: viditelná stránka to serveru jednou za pár minut řekne.
   Po návratu na stránku se tab, který mezitím usnul, hned probudí. */
setInterval(() => {
  if (!document.hidden && ACTIVE && ACTIVE.id) send({t: 'zije', id: ACTIVE.id});
}, 4 * 60 * 1000);
document.addEventListener('visibilitychange', () => {
  if (!document.hidden && ACTIVE && ACTIVE.id) send({t: 'wake', id: ACTIVE.id});
});

function oznacUspany(tab, on) {
  if (!tab || !tab.el) return;
  tab.uspany = !!on;
  tab.el.classList.toggle('uspany', !!on);
}

function handle(msg) {
  // Appka se restartuje (aktualizace): nové okno se otevře samo, tohle se zavře.
  if (msg.t === 'okno-jdi' && /^http:\/\/127\.0\.0\.1:\d+\//.test(msg.url || '')) {
    // Restart po aktualizaci: stejné okno přejde rovnou na novou instanci.
    // Není to odchod — bez HUB_LEAVING by se při práci Clauda ptal beforeunload.
    window.HUB_LEAVING = true;
    setTimeout(() => { location.replace(msg.url); }, 150);
    return;
  }
  if (msg.t === 'okno-zavri') { setTimeout(() => { try { window.close(); } catch (_) { /* nic */ } }, 1200); return; }
  /* Hub si stáhl samostatnou appku a otevřel v ní okno na téže instanci
     (hub/appka.py) — tohle okno v prohlížeči už není potřeba. Okno appky
     (window.hubApp) a telefon přes Tailscale zůstávají. */
  if (msg.t === 'appka-prechod') {
    if (window.hubApp || location.hostname !== '127.0.0.1') return;
    window.HUB_LEAVING = true;
    toast('Hub se přesunul do samostatné appky — tohle okno se zavře.');
    setTimeout(() => { try { window.close(); } catch (_) { /* nic */ } }, 1500);
    return;
  }
  if (msg.t && msg.t.startsWith('br-')) { if (window.HubProhlizec) HubProhlizec.naZpravu(msg); return; }
  if (msg.t === 'out') {
    const tab = TABS.find(t => t.id === msg.id);
    if (tab) {
      tab.term.write(recolor(tab, msg.d));
      const now = Date.now();
      tab.lastOut = now;
      if (now - (tab.lastInput || 0) > 400) {
        (tab.outTimes = tab.outTimes || []).push(now);
        if (tab.outTimes.length > 12) tab.outTimes.shift();
      }
    }
  } else if (msg.t === 'opened') {
    const tab = TABS.find(t => t.ref === msg.ref);
    if (tab) {
      tab.id = msg.id;
      // Obnovený tab zná své číslo až teď — zprávy, které na serveru čekají
      // z doby před restartem, se mu ukážou až s ním (dřív se nenačetly vůbec).
      nactiFrontu(tab);
      // Místo, kam se po restartu vrátit, si pamatuje i konverzaci.
      if (tab === ACTIVE && placeReady) writePlace({tab: tabKey(tab)});
      // Rozepsaný text: chat tabu je teď známý, takže se najde i koncept,
      // který se ukládal pod číslem konverzace.
      obnovKoncept(tab);
      // Taby po restartu vznikají dřív, než server řekne jejich id — ikonka
      // prohlížeče a lišta se musí dopočítat teď, ne až při další změně.
      if (window.__brPouzij) window.__brPouzij();
      if (tab === ACTIVE) document.dispatchEvent(new CustomEvent('hub-tab', {detail: {id: tab.id, title: tab.title, path: tab.path || ''}}));
      tab.bypass = !!msg.bypass;
      tab.chat = msg.chat || tab.chat || '';
      if (msg.guest) markGuest(tab, msg.guest);
      // Server mohl model doplnit sám (Ollama bez modelu nespustíš), tak se
      // tím, co doopravdy běží, přepíše i to, s čím se tab zakládal.
      if (msg.model && msg.model !== tab.model) {
        tab.model = msg.model;
        if (tab.composer && tab.composer.setModel) tab.composer.setModel(msg.model);
        paintAgent(tab);
      }
      refit(tab);
    }
  } else if (msg.t === 'exit') {
    const tab = TABS.find(t => t.id === msg.id);
    if (tab) {
      tab.exited = true;
      zapamatujZavreny(tab);
      tab.el.classList.add('exited');
      // Ve čtení terminál není vidět — bez hlášky by jen přestalo přibývat.
      if (tab.cteni) tab.cteni.upozorni('Claude Code v tomhle tabu skončil. Konverzaci otevřeš znovu v seznamu Konverzace.');
    }
  } else if (msg.t === 'tab-uspany') {
    const tab = TABS.find(t => t.id === msg.id);
    oznacUspany(tab, msg.on);
    // Usnul tab, na který se zrovna někdo dívá (souběh) — hned vzhůru.
    if (msg.on && tab && tab === ACTIVE && !document.hidden) send({t: 'wake', id: tab.id});
  } else if (msg.t === 'pamet') {
    toast(msg.stav === 'oom'
      ? 'Prostoru došla paměť (' + msg.pouzito + ' z ' + msg.strop + ' MB), server ukončil proces. Zavři nepotřebné taby.'
      : 'Prostor je skoro na stropu paměti (' + msg.pouzito + ' z ' + msg.strop + ' MB). Zavři nepotřebné taby a testy.');
  } else if (msg.t === 'tab-opened') {
    // Tab z jiného okna téhož hubu (appka i prohlížeč naráz). Přidá se na
    // pozadí — přepnout na něj by tomu, kdo tu zrovna pracuje, sebralo tab.
    if (!TABS.some(t => t.id === msg.id)) {
      // I s trezorem — bez něj by firemní tab otevřený v telefonu byl na
      // počítači v barvě osobního, dokud se okno znovu nepřipojí.
      attachTab(createTab({kind: msg.kind, path: msg.path, title: msg.title,
                           id: msg.id, agent: msg.agent, model: msg.model,
                           bypass: msg.bypass, resume: msg.resume,
                           vault: msg.vault, background: true}));
      if (msg.guest) {
        markGuest(TABS.find(t => t.id === msg.id), msg.guest);
        if (!STATE.guest) toast(`${msg.guest} otevřel(a) ve tvém prostoru tab „${msg.title}“.`);
      }
    }
  } else if (msg.t === 'tab-closed') {
    const tab = TABS.find(t => t.id === msg.id);
    if (tab) closeTab(tab, {remote: true});
  } else if (msg.t === 'tab-renamed') {
    const tab = TABS.find(t => t.id === msg.id);
    if (tab) setTitle(tab, msg.title);
  } else if (msg.t === 'sessions') {
    if (staleVersion(msg.version)) return;
    restore(msg.list);
    for (const s of msg.list) if (s.guest) markGuest(TABS.find(t => t.id === s.id), s.guest);
    // Prázdný seznam = čerstvě nastartovaný server. Když se restartoval kvůli
    // aktualizaci, leží tu po něm uložené taby (hub/restart.py).
    if (!msg.list.length) restoreAfterRestart();
  } else if (msg.t === 'host-oznameni') {
    // Co udělal host se Správou předplatného (gateway/pristup.py).
    if (!STATE.guest && msg.text) toast(msg.text);
  } else if (msg.t === 'fronta') {
    // Stav zprávy z fronty na serveru: uložená → předaná Claudovi → doručená.
    const tab = TABS.find(t => t.id === msg.tab);
    if (tab && tab.cteni) tab.cteni.odServeru(msg);
  } else if (msg.t === 'stazeno') {
    if (msg.path) stazenoKarta(msg);
  } else if (msg.t === 'host-vstup') {
    if (!STATE.guest) toast(`${msg.name} vstoupil(a) do tvého prostoru (${msg.role}).`);
  } else if (msg.t === 'memory-saved') {
    memorySaved(msg);
  } else if (msg.t === 'prenos-prijat') {
    prenosKarta({titulek: 'Chat ze serveru je tady', text: `„${msg.title}“ — pokračuj v něm na počítači.`,
      tlacitka: [{label: 'Otevřít', primary: true, run: () => otevriPrenesenyChat(msg.chat)}, {label: 'Později', run: async () => {}}]});
  } else if (msg.t === 'prenos-ceka') {
    prenosPoll();
  } else if (msg.t === 'pocitac-schvaleni') {
    // Claude ze serveru chce na tomhle počítači udělat něco, co chce souhlas.
    HubPocitac.approvalFromHub(hubIO(), msg);
  } else if (msg.t === 'pocitac-oznameni' || msg.t === 'pocitac-pristup') {
    HubPocitac.simpleNotice(hubIO(), msg);
  } else if (msg.t === 'pocitac-ukol') {
    // Úkol, který nechal Claude ze serveru (hub/pocitac.py): dorazil, nebo běží.
    HubPocitac.ukol(hubIO(), msg);
  } else if (msg.t === 'error') {
    const tab = TABS.find(t => t.ref === msg.ref);
    if (tab) closeTab(tab);
    toast(msg.d);
  }
}

/* Po aktualizaci serveru (brána, noční aktualizace) se stránka jen znovu
   připojí a jela by dál se starým JavaScriptem, který o nových věcech neví.
   Rozepsaný text v bublině se ale nezahazuje — pak jen hláška. */
function staleVersion(version) {
  const mine = STATE && STATE.version && STATE.version.version;
  if (!version || !mine || version === mine) return false;
  const typing = [...document.querySelectorAll('.composer-input')].some(i => i.value.trim());
  if (typing) {
    toast('Je tu nová verze aplikace — až dopíšeš, načti stránku znovu (F5).');
    return false;
  }
  location.reload();
  return true;
}

let restartRestored = false;

function tabyWord(n) {
  return n === 1 ? 'tab' : (n >= 2 && n <= 4 ? 'taby' : 'tabů');
}

/* Po restartu kvůli aktualizaci: server je nový a žádné sessions nemá, ale
 * hub/restart.py před vypnutím uložil, co bylo otevřené. Otevře se to znovu —
 * u Clauda s `--resume`, takže konverzace pokračuje tam, kde skončila.
 * Soubor se čte jednou (server ho při čtení maže), tohle je jen pojistka. */
async function restoreAfterRestart() {
  // Host v cizím prostoru obnovu vlastníkovi nesní (brána ji stejně nepustí).
  if (STATE && STATE.guest) return;
  if (restartRestored) return;
  restartRestored = true;
  let tabs = [];
  let res = {};
  try {
    res = await api('restore');
    tabs = res.tabs || [];
  } catch (_) {
    return;                 // obnova je bonus, chyba tu nesmí nic shodit
  }
  if (!tabs.length) return;
  const reason = res.duvod || 'restart';
  const drafts = takeDrafts();
  /* Hned naběhne jen tab, kde člověk skončil, a taby s rozdělanou prací
     (`prompt`). Ostatní se založí uspané a Claude v nich se pustí, až je
     otevřeš — po aktualizaci tak nestartuje pět Claudů naráz a stránka se
     nezasekne překreslováním všech konverzací. */
  const key = LAST_PLACE.tab;
  const tadyKoncil = (t, strict) => !!key && (t.kind || '') === key.kind && (t.path || '') === key.path &&
    (t.vault || '') === (key.vault || '') && (!strict || (t.title || '') === key.title);
  const hlavni = (key && key.chat && tabs.find((t) => (t.resume || '') === key.chat))
    || tabs.find((t) => tadyKoncil(t, true)) || tabs.find((t) => tadyKoncil(t, false))
    || tabs[tabs.length - 1];
  for (const t of tabs) {
    // `prompt`: připomínka úloh na pozadí, které restart zastavil (restart.py).
    const prompt = t.resume ? (t.prompt || '') : '';
    const tab = openTab({kind: t.kind, path: t.path, title: t.title,
                         agent: t.agent, model: t.model, resume: t.resume, vault: t.vault || '',
                         prompt, spici: t !== hlavni && !!t.resume && !prompt});
    // Rozepsaná zpráva z doby před restartem (aktualizace prostoru).
    const at = drafts.findIndex((d) => d.path === (t.path || '') && d.title === (t.title || ''));
    if (at >= 0 && tab.composer && tab.composer.setDraft) {
      tab.composer.setDraft(drafts[at].text);
      drafts.splice(at, 1);
    }
  }
  // A na tab, kde člověk skončil (ne na poslední otevřený).
  const last = findPlaceTab(LAST_PLACE.tab);
  if (last) activate(last);
  const back = tabs.filter(t => t.resume).length;
  const head = reason === 'restart' ? 'Hub aktualizovaný' : 'Pokračuješ, kde jsi skončil';
  toast(`${head} — ${tabs.length} ${tabyWord(tabs.length)} zpátky` +
        (back ? `, z toho ${back} pokračuje v konverzaci.` : '.'));
}

/* Re-attach after a reload or a dropped connection: the server is the source of
 * truth about which sessions exist. */
function restore(list) {
  /* Tab, na kterém člověk je, zůstává. Dřív se po každém znovupřipojení
     (výpadek sítě, uspaný počítač, proxy brány zavře nečinné spojení) přepnulo
     na poslední tab a každý tab, který mezitím přibyl, se aktivoval — uprostřed
     práce to „skočilo" jinam. Na poslední se přepne jen při prvním načtení. */
  const before = ACTIVE;
  const live = new Set(list.map(s => s.id));
  for (const tab of [...TABS]) {
    if (tab.id && !live.has(tab.id)) closeTab(tab);
  }
  for (const info of list) {
    let tab = TABS.find(t => t.id === info.id);
    if (!tab) {
      tab = createTab({kind: info.kind, path: info.path, title: info.title,
                       id: info.id, agent: info.agent, model: info.model,
                       bypass: info.bypass, resume: info.resume,
                       // Trezor drží server (Session.info) — bez něj by
                       // firemní tab po obnově přestal být fialový.
                       vault: info.vault, background: true});
    }
    tab.bypass = !!info.bypass;
    tab.chat = info.chat || tab.chat || '';
    // Číslo konverzace je známé teprve teď — koncept a zprávy z fronty se
    // hledají až s ním (po reloadu nebo restartu serveru).
    obnovKoncept(tab);
    nactiFrontu(tab);
    oznacUspany(tab, info.uspany);
    if (info.vault !== undefined && (tab.vault || '') !== (info.vault || '')) {
      tab.vault = info.vault || '';
      paintAgent(tab);
      tab.term.options.theme = termTheme(tab);   // i kurzor a výběr v terminálu
    }
    attachTab(tab);
  }
  // První načtení stránky: zůstat na Domů s přehledem otevřených chatů.
  // Po znovupřipojení (výpadek sítě) zůstává tab, na kterém člověk byl.
  if (START_HOME) {
    START_HOME = false;
    // Zpátky na tab, kde člověk skončil — jinak Domů s přehledem chatů.
    const last = before && TABS.includes(before) ? before : findPlaceTab(LAST_PLACE.tab);
    activate(last);
    placeReady = true;
    reopenVaultPlace();
    return;
  }
  const keep = before && TABS.includes(before) ? before : TABS[TABS.length - 1];
  if (keep && keep !== ACTIVE) activate(keep);
}

/* Připojit tab k session na serveru. Server pošle celý výpis znovu, takže se
 * terminál nejdřív vyčistí. Rozměr se pošle i beze změny: server si ho drží
 * pro každé spojení zvlášť a nové spojení ho ještě nezná. */
function attachTab(tab) {
  tab.term.reset();          // the replay below is the full scrollback
  clearTimeout(tab.cutTimer);
  tab.cut = '';              // půlka sekvence z minulého spojení už nepatří nikam
  send({t: 'attach', id: tab.id});
  tab.sentCols = tab.sentRows = null;
  refit(tab);
}

let toastTimer = null;
/* Hook memory-autosave.py doplnil paměť. Stačí o tom vědět — nic se nepotvrzuje. */
async function memorySaved({files, projects}) {
  const notes = (files || []).filter((f) => f !== 'MEMORY.md');
  if (!notes.length) return;
  const where = (projects || []).length ? ` (${projects.join(', ')})` : '';
  toast(`Paměť doplněna${where}: ${notes.join(', ')}`);
  try {
    STATE = await api('state');
    markAdvanced();
  markAdvanced();
    renderMemory();
    renderWelcome();
  } catch (_) { /* hláška stačí, seznam se obnoví příště */ }
}

/* Týdenní limit spotřeby (gateway/spotreba.py): když je člověk u svého stropu
   nebo nad ním, hub mu to řekne pruhem nahoře. Pruh jde zavřít a stejný stav
   se ten den neukáže znovu. Jen na serveru a jen pro toho, komu správce limit
   nastavil. */
let spotrebaTimer = null;

async function hlidejLimit() {
  if (!onServer()) return;
  if (!spotrebaTimer) spotrebaTimer = setInterval(() => { if (!document.hidden) hlidejLimit(); }, 600000);
  const gw = (STATE.config && STATE.config.gateway_user) || {};
  let data;
  try {
    const r = await fetch('/gw/spotreba', {credentials: 'same-origin'});
    if (!r.ok) return;
    data = await r.json();
  } catch (_) { return; }
  const ja = (data.lide || []).find((x) => String(x.email).toLowerCase() === String(gw.email || '').toLowerCase());
  let bar = document.getElementById('spotreba-pruh');
  if (!ja || !ja.limit || !ja.stav || ja.stav === 'ok') { if (bar) bar.remove(); return; }
  const klic = 'hub-spotreba-' + ja.stav + '-' + new Date().toISOString().slice(0, 10);
  try { if (localStorage.getItem(klic)) return; } catch (_) {}
  const dolary = (c) => (Number(c) || 0).toFixed(0) + ' $';
  const text = ja.stav === 'nad'
    ? 'Tenhle týden jsi přes svůj limit spotřeby Claude (' + dolary(ja.tyden) + ' z ' + dolary(ja.limit) +
      '). Je to už ' + (ja.preslo || 1) + '. překročení — ať společný limit nevyčerpáme všichni, zkus ubrat.'
    : 'Blížíš se ke svému týdennímu limitu spotřeby Claude (' + dolary(ja.tyden) + ' z ' + dolary(ja.limit) + ').';
  if (!bar) {
    bar = document.createElement('div');
    bar.id = 'spotreba-pruh';
    document.body.appendChild(bar);
  }
  bar.className = 'spotreba-pruh ' + ja.stav;
  bar.textContent = '';
  const t = document.createElement('span');
  t.textContent = text;
  const x = document.createElement('button');
  x.textContent = '×';
  x.title = 'Zavřít (dnes se to nebude opakovat)';
  x.onclick = () => { try { localStorage.setItem(klic, '1'); } catch (_) {} bar.remove(); };
  bar.append(t, x);
}

function toast(text) {
  const el = $('toast');
  el.textContent = text;
  // Bublina se často zvětší v témže tiknutí, ve kterém hláška vzniká — vložená
  // cesta se zalomí na další řádek. ResizeObserver, který --composer-h píše, se
  // ozve až další snímek, takže by se hláška stihla posadit na starou, nižší
  // hranu, tedy přes bublinu. Změří se proto tady, těsně před zobrazením.
  const box = document.querySelector('.pane.active .composer');
  document.documentElement.style.setProperty(
    '--composer-h', (box ? box.offsetHeight : 0) + 'px');
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, 6000);
}

/* ── průvodce a nastavení ─────────────────────────────────────────────────── */
/* Přepnout na tab podle id — i na takový, který teprve dorazí po websocketu
   (tab úkolu ze serveru otevírá hub, ne tohle okno). `idle`: jen když
   člověk zrovna nemá otevřený jiný tab. */
function focusTab(id, opts = {}, tries = 15) {
  const tab = TABS.find(t => t.id === id);
  if (!tab) {
    if (tries > 0) setTimeout(() => focusTab(id, opts, tries - 1), 200);
    return;
  }
  if (opts.idle && ACTIVE && ACTIVE !== tab) return;
  activate(tab);
}

function hubIO() {
  return {
    get state() { return STATE; },
    api,
    setTheme,
    // Obě jména schválně: části UI vznikaly postupně a půlka volá toast(),
    // půlka notice(). Jedno chybějící jméno zabilo hlášku o zkopírovaném logu.
    toast,
    notice: toast,
    copy: copyText,
    // Odkaz ven — na serveru v prohlížeči toho, kdo se dívá (openLink).
    open: openLink,
    pickFolder,
    reload,
    refreshState: async () => { STATE = await api('state'); markAdvanced(); renderBranding(); applyMotiv(STATE.motiv); HubVerxee.apply(STATE); },
    // Motiv přepnutý v Nastavení → Aladin Market → Motivy, bez obnovení okna.
    setMotiv: applyMotiv,
    openWizard: () => HubOnboarding.open({...hubIO(), state: STATE}),
    /* Přihlášení jiným účtem. `/login` žije uvnitř Claude Code, ne v shellu,
       tak se otevře tab, který ho dostane rovnou jako první příkaz — stejnou
       cestou, jakou jde „Upravit poznámku" na /project. Jiný účet znamená
       i jiné konektory (třeba druhou gmailovou schránku). */
    login: () => openTab({kind: 'slash:login', path: STATE.home,
                          title: 'přihlášení'}),
    // Nastavení agentů otevírá taby: instalaci i přihlášení je lepší vidět
    // běžet, než je pustit skrytě na pozadí.
    openTab: (opts) => openTab(opts),
    focusTab,
  };
}

/* ── boot ─────────────────────────────────────────────────────────────────── */
/* Chyby ze stránky posíláme do stejného logu jako ty ze serveru — jinak by se
   problém hledal na dvou místech a jedno z nich by nikdo neotevřel. */
/* Chyba ve smyčce (pohyb myši, snímek) jich umí poslat stovky za vteřinu —
   a každá je požadavek na server, což stránku dusí víc než chyba sama.
   Stejná zpráva jde nejvýš jednou za minutu (s počtem, kolikrát mezitím
   přišla) a všech dohromady nejvýš 30 za minutu. */
const logPocty = new Map();      // zpráva → {kdy, zamlceno}
let logOkno = 0, logVOkne = 0;
function reportToLog(text) {
  try {
    text = String(text).slice(0, 500);
    const ted = Date.now();
    const z = logPocty.get(text);
    if (z && ted - z.kdy < 60000) { z.zamlceno++; return; }
    if (ted - logOkno > 60000) { logOkno = ted; logVOkne = 0; }
    if (++logVOkne > 30) return;
    if (logPocty.size > 200) logPocty.clear();
    logPocty.set(text, {kdy: ted, zamlceno: 0});
    const navic = z && z.zamlceno ? ` (za poslední minutu ještě ${z.zamlceno}×)` : '';
    fetch(`/api/log?t=${encodeURIComponent(TOKEN)}`, {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({text: text + navic, level: 'error'}),
    }).catch(() => {});
  } catch (_) { /* logování nesmí být důvod další chyby */ }
}

window.addEventListener('error', (ev) => {
  // Z minifikovaného souboru je řádek vždycky 1 — sloupec a první rámce
  // zásobníku teprve řeknou, odkud chyba je.
  const kde = ev.error && ev.error.stack ? ' | ' + String(ev.error.stack).split('\n').slice(1, 4).map((r) => r.trim()).join(' < ') : '';
  reportToLog(`${ev.message} @ ${ev.filename}:${ev.lineno}:${ev.colno}${kde}`);
});
window.addEventListener('unhandledrejection', (ev) => {
  reportToLog('neodchycené odmítnutí: ' + (ev.reason && ev.reason.message || ev.reason));
});

/* Zamrznutí stránky („Tato stránka neodpovídá") do stejného logu. Chyba se
   sama nehlásí — stránka jen stojí, a pak ji člověk zavře. Dvě cesty:
   - Long Animation Frames (Chromium 123+): snímek delší než 3 s, a v něm
     skripty, které ho držely (soubor:pozice a funkce). Přijde, až se stránka
     rozhýbe — třeba po „Vyčkat".
   - Hlídač ve workeru: stránka mu každou vteřinu kývne. Když 10 s mlčí, pošle
     záznam sám — worker běží ve vlastním vlákně, takže to jde, i když stránka
     visí natrvalo a člověk ji pak ukončí. */
function popisStranky() {
  const viditelny = ACTIVE ? (ACTIVE.kind + (ACTIVE.cteni ? '/čtení' : '')) : 'domů';
  const mb = performance.memory ? Math.round(performance.memory.usedJSHeapSize / 1048576) + ' MB' : '?';
  return `tabů ${TABS.length}, vidět ${viditelny}, paměť stránky ${mb}, prvků ${document.getElementsByTagName('*').length}`;
}

function hlidejZamrznuti() {
  let posledni = 0;
  try {
    new PerformanceObserver((list) => {
      for (const e of list.getEntries()) {
        if (e.duration < 3000 || Date.now() - posledni < 30000) continue;
        posledni = Date.now();
        const skripty = (e.scripts || []).slice().sort((a, b) => b.duration - a.duration).slice(0, 3)
          .map((s) => `${Math.round(s.duration)} ms ${s.sourceFunctionName || '?'} @ ${(s.sourceURL || '').split('/').pop()}:${s.sourceCharPosition} (${s.invoker || s.invokerType || ''})`);
        reportToLog(`zamrznutí ${(e.duration / 1000).toFixed(1)} s — ${skripty.join(' | ') || 'bez skriptu (vykreslování, paměť)'} — ${popisStranky()}`);
      }
    }).observe({type: 'long-animation-frame', buffered: false});
  } catch (_) { /* starší prohlížeč — zbude hlídač */ }

  if (typeof Worker !== 'function' || typeof Blob !== 'function') return;
  const url = new URL(`/api/log?t=${encodeURIComponent(TOKEN)}`, location.href).href;
  const kod = `
    let posledni = Date.now(), popis = '', videt = true, nahlaseno = false;
    onmessage = (ev) => {
      if (nahlaseno && videt) {
        fetch(${JSON.stringify(url)}, {method: 'POST', headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({level: 'warn', text: 'stránka se po ' + Math.round((Date.now() - posledni) / 1000) + ' s rozhýbala'})}).catch(() => {});
      }
      posledni = Date.now(); popis = ev.data.popis; videt = ev.data.videt; nahlaseno = false;
    };
    setInterval(() => {
      if (nahlaseno || !videt || Date.now() - posledni < 10000) return;
      nahlaseno = true;
      fetch(${JSON.stringify(url)}, {method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({level: 'error', text: 'stránka neodpovídá 10 s — naposledy: ' + popis})}).catch(() => {});
    }, 2000);`;
  let worker;
  try {
    worker = new Worker(URL.createObjectURL(new Blob([kod], {type: 'text/javascript'})));
  } catch (_) { return; }
  let popis = '', popisOd = 0;
  setInterval(() => {
    // Počet prvků stojí pár ms — stačí jednou za půl minuty.
    if (Date.now() - popisOd > 30000) { popis = popisStranky(); popisOd = Date.now(); }
    // Schovaná stránka má časovače přiškrcené — mlčení tam nic neznamená.
    worker.postMessage({popis, videt: !document.hidden});
  }, 1000);
  document.addEventListener('visibilitychange', () => worker.postMessage({popis, videt: !document.hidden}));
}
hlidejZamrznuti();

/* ── Menu samostatné appky (desktop/, Electron) ────────────────────────────
   Nativní menu Electronu kreslí na Linuxu a Windows Chromium a rozbalené
   položky má velké jako pro prst — zvenku se to nastavit nedá. Appka proto
   pošle šablonu svého menu (preload → window.hubApp) a hub ho nakreslí jako
   proužek nahoře, na místě nativní lišty, kterou appka schová (Alt ji ukáže,
   zkratky fungují dál). Rozbalené položky jsou stejně vysoké jako tlačítka.
   Na Macu je menu v systému, šablona je prázdná.
   Tlačítka si neberou fokus — Kopírovat a Vložit pak míří tam, kde se psalo. */
async function appMenu() {
  const api = window.hubApp;
  const nav = $('appmenu');
  if (!api || typeof api.menu !== 'function' || !nav) return;
  let menus = [];
  try { menus = await api.menu(); } catch (_) { return; }
  if (!Array.isArray(menus) || !menus.length) return;
  let open = null;                      // {btn, drop}
  const close = () => {
    if (!open) return;
    open.btn.classList.remove('on');
    open.drop.remove();
    open = null;
  };
  const show = (btn, menu) => {
    close();
    const drop = document.createElement('div');
    drop.className = 'appmenu-drop';
    for (const p of menu.polozky || []) {
      if (p.oddelovac) { drop.appendChild(document.createElement('hr')); continue; }
      const it = document.createElement('button');
      it.type = 'button';
      it.className = 'appmenu-item';
      it.disabled = p.povoleno === false;
      it.innerHTML = '<span></span><kbd></kbd>';
      it.firstChild.textContent = p.label;
      it.lastChild.textContent = p.zkratka || '';
      it.onmousedown = (ev) => ev.preventDefault();
      it.onclick = () => { close(); api.akce(p.id); };
      drop.appendChild(it);
    }
    document.body.appendChild(drop);
    const r = btn.getBoundingClientRect();
    drop.style.left = Math.max(4, Math.min(r.left, innerWidth - drop.offsetWidth - 4)) + 'px';
    drop.style.top = r.bottom + 2 + 'px';
    btn.classList.add('on');
    open = {btn, drop};
  };
  nav.textContent = '';
  for (const menu of menus) {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'appmenu-btn';
    btn.textContent = menu.label;
    btn.onmousedown = (ev) => ev.preventDefault();
    btn.onclick = () => { if (open && open.btn === btn) close(); else show(btn, menu); };
    // Jako v nativní liště: s otevřeným menu stačí najet na vedlejší.
    btn.onmouseenter = () => { if (open && open.btn !== btn) show(btn, menu); };
    nav.appendChild(btn);
  }
  document.addEventListener('mousedown', (ev) => {
    if (open && !open.drop.contains(ev.target) && !nav.contains(ev.target)) close();
  }, true);
  document.addEventListener('keydown', (ev) => { if (ev.key === 'Escape' && open) { ev.stopPropagation(); close(); } }, true);
  addEventListener('blur', close);
  addEventListener('resize', close);
  nav.hidden = false;
  api.menuVListe(true);
}

async function main() {
  appMenu();
  // Hub na serveru: appka sem přišla s `#local=`, cestou zpátky na počítač.
  // Čte se hned, ať adresa s tokenem nezůstane viset v řádku ani v historii.
  HubServer.captureLocal();
  // Návrat ze serveru se zapíše dřív, než se načte zbytek hubu: kdo okno
  // zavře hned po návratu, má příště naskočit tady, ne zase na serveru.
  const returned = await applyReturn();

  const saved = localStorage.getItem('hub-theme');
  DARK = saved ? saved === 'dark'
               : !window.matchMedia('(prefers-color-scheme: light)').matches;

  await reload();
  themeFromState();
  autoUpdateAtStart();
  if (!STATE.config.gateway_user) HubPocitac.watchPending(hubIO());

  $('search').oninput = (ev) => renderProjects(ev.target.value);
  $('btn-refresh').onclick = () => reload();
  $('btn-theme').onclick = () => setTheme(!DARK, true);
  $('btn-settings').onclick = () => HubSettings.open({...hubIO(), state: STATE});
  $('btn-stats').onclick = () => HubStats.open(hubIO());
  $('btn-menu').onclick = (ev) => topbarMenu(ev.currentTarget);
  $('btn-home').onclick = goHome;
  setInterval(renderGreeting, 5 * 60 * 1000);
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') renderGreeting();
  });
  initChats();
  $('btn-new-shell').onclick = () => openTab({kind: 'shell', path: '', title: 'Terminál'});
  // Levý klik = výchozí agent, pravý klik nebo delší podržení = výběr.
  // Nabídka se sama otevře i tehdy, když výchozí agent není nainstalovaný.
  $('btn-new-agent').onclick = (ev) => {
    const ready = agentList(true);
    const def = agentById(STATE.default_agent);
    // Ve firmě je nový chat chatem firmy (prostor okna, přepínač v hlavičce).
    if (prostorFirma()) return openFirmaTab();
    if (ready.length > 1 && (ev.altKey || !def || !def.path)) return newAgentMenu(ev);
    const a = (def && def.path) ? def : ready[0];
    if (!a) return HubSettings.open({...hubIO(), state: STATE, tab: 'agenti'});
    // Tab se jmenuje po prostředí, ne po agentovi — v liště pak jde poznat,
    // co běží na počítači a co v prostoru na serveru.
    openTab({kind: 'project', path: STATE.home, title: newTabLabel(), agent: a.id});
  };
  $('btn-new-agent').oncontextmenu = (ev) => { ev.preventDefault(); newAgentMenu(ev); };
  $('btn-sdilet').onclick = () => { if (ACTIVE) sdiletChat(ACTIVE); };
  $('btn-new-firma').onclick = () => openFirmaTab();
  $('btn-new-menu').onclick = (ev) => newTabMenu(ev.currentTarget);
  // Poznámky se otevírají v hubu (i s přepínačem firemní / sdílené); aplikace
  // Obsidian na počítači je uvnitř pod „Otevřít v Obsidianu".
  $('btn-brain').onclick = () => {
    if (!window.HubVault) return openExternal('', 'brain');
    const f = prostorFirma();
    return f ? openVault('', firmaVaultId(f)) : openVault();
  };
  $('btn-prostor').onclick = prostorMenu;
  $('nav-projekty').onclick = () => showProjekty(true);
  // Položky panelu jako v appce Claude (viditelné v motivu Claude — hub.css).
  $('nav-hledat').onclick = () => { const q = $('chats-search'); if (q) { q.scrollIntoView({block: 'nearest'}); q.focus(); } };
  $('nav-customize').onclick = () => HubSettings.open({...hubIO(), state: STATE, tab: 'rozsireni'});
  $('nav-terminal').onclick = () => openTab({kind: 'shell', path: '', title: 'Terminál'});
  $('welcome-prompt-input').addEventListener('keydown', (ev) => {
    if (ev.key === 'Enter' && !ev.shiftKey && !ev.isComposing && !(window.HubIME && !window.HubIME.enterOdesle(ev))) {
      ev.preventDefault();
      welcomeSend();
    }
  });
  $('welcome-prompt-send').onclick = () => welcomeSend();
  $('pp-q').oninput = renderProjekty;
  $('pp-sort').onclick = () => {
    ppSort = ppSort === 'recent' ? 'name' : ppSort === 'name' ? 'cena' : 'recent';
    try { localStorage.setItem('hub-pp-sort', ppSort); } catch (_) {}
    renderProjekty();
  };
  $('pp-new').onclick = () => openPicker('');
  $('modal-close').onclick = closePicker;
  $('modal-cancel').onclick = closePicker;
  $('modal-open').onclick = () => {
    const path = pickerPath;
    $('modal').hidden = true;
    if (settlePicker(path)) return;          // o cestu si někdo řekl
    const name = path.replace(/[\\/]+$/, '').split(/[\\/]/).pop() || path;
    openTab({kind: 'project', path, title: name});
  };

  window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', (ev) => {
    if (!localStorage.getItem('hub-theme')) setTheme(ev.matches, false);
  });

  // Stejné doznívající kliknutí by nabídku hned zase zavřelo, než ji stihne
  // člověk vidět.
  document.addEventListener('click', () => {
    if (Date.now() - menuArmedAt >= MENU_ARM_MS) hideMenu();
  });
  document.addEventListener('keydown', (ev) => {
    if (ev.key === 'Escape') { hideMenu(); closePicker(); }
  });

  new ResizeObserver(() => refit(ACTIVE)).observe($('panes'));
  window.addEventListener('resize', () => refit(ACTIVE));

  // Zavření celého okna. Vlastní dialog sem nedosáhne — křížek okna řídí
  // prohlížeč, takže se ptá on. A jen když by se utnula rozdělaná práce:
  // do paměti se session uloží samy, jakmile hub taby zavře.
  window.addEventListener('beforeunload', (ev) => {
    // Přechod mezi počítačem a serverem není zavírání: taby na počítači běží
    // dál a stránka se k nim po návratu připojí.
    if (window.HUB_LEAVING) return;
    if (!TABS.some(t => t.id && !t.exited && isBusy(t))) return;
    ev.preventDefault();
    ev.returnValue = '';             // vyžadují starší prohlížeče
  });

  // Pluginy appky (pluginy.js, hub/pluginy.py) — zapnuté se načtou teď.
  if (window.HubPluginy) {
    HubPluginy.start({
      api, toast, token: TOKEN, renderActions, open: openLink,
      aktivniTab: () => (ACTIVE ? {id: ACTIVE.id, title: ACTIVE.title, path: ACTIVE.path || ''} : null),
      posli: (text) => {
        if (!ACTIVE || !ACTIVE.composer || !text) return false;
        return ACTIVE.composer.run(text + '\r');
      },
    });
  }
  if (window.HubProhlizec) {
    HubProhlizec.install({send, copy: (t) => copyText(t),
                          ulozene: () => (STATE.config && STATE.config.prohlizec_okno) || null,
                          uloz: (o) => api('config', {prohlizec_okno: o}).catch(() => {}),
                          tabTitle: (sid) => { const t = TABS.find((x) => x.id === sid); return t ? t.title : ''; }});
  }
  /* Ikonka prohlížeče u tabů, které v něm mají karty (prohlizec.js). */
  document.addEventListener('hub-br-taby', (ev) => { brMapa = ev.detail || {}; brPouzij(); });
  window.__brPouzij = () => brPouzij();
  function brPouzij() {
    const mapa = brMapa;
    for (const t of TABS) {
      const ik = t.el && t.el.querySelector('.tab-br');
      if (!ik) continue;
      const m = mapa[t.id];
      ik.hidden = !m;
      ik.classList.toggle('pomoc', !!(m && m.potreba));
      ik.title = !m ? '' : m.potreba ? 'Claude v prohlížeči potřebuje tvou pomoc — ukázat'
        : 'Prohlížeč tohohle chatu (' + m.karet + (m.karet === 1 ? ' karta' : m.karet < 5 ? ' karty' : ' karet') + ')';
    }
  }
  connect();
  checkForUpdate();
  checkServerUpdate();

  await startScreen(returned);
}

/* Okno se vrátilo ze serveru: `?mode=local` (pracovat tady) nebo
 * `?mode=logout` (odhlásit appku). Parametr se z adresy hned odstraní, ať
 * reload stránky akci nezopakuje. */
async function applyReturn() {
  if (!HubServer.isAppWindow()) return '';
  const params = new URLSearchParams(location.search);
  const mode = params.get('mode');
  if (!mode) return '';
  params.delete('mode');
  history.replaceState(null, '', location.pathname + '?' + params.toString());
  if (mode === 'local') await api('account', {action: 'local'}).catch(() => {});
  else if (mode === 'logout') await api('account', {action: 'logout'}).catch(() => {});
  // pocitac, predplatne = jen volba, pak zpátky; ukoly = zůstat u úkolu ze serveru
  else if (!['pocitac', 'predplatne', 'ukoly'].includes(mode)) return '';
  return mode;
}

/* Co ukázat po startu: prostor na serveru, přihlášení, nebo průvodce.
 *
 * Když je appka v serverovém režimu, launcher okno pošle na server rovnou.
 * Sem se tedy dostane, jen když to nevyšlo (server neodpovídá, přihlášení
 * vypršelo) — nebo když se okno ze serveru vrátilo. */
async function startScreen(returned) {
  // Hub puštěný instalačkou ke stažení: nejdřív se musí nainstalovat.
  if (STATE.setup) return HubSetup.open(hubIO());
  const onLocal = (opts) => {
    // Napoprvé se hub nastavuje tady, ne v instalačce — ta běží jednou a v
    // terminálu, takže po ní nebylo kde nastavení změnit.
    if (!STATE.onboarded) HubOnboarding.open({...hubIO(), state: STATE});
    // Úkoly, které tu nechal Claude ze serveru a čekají na spuštění.
    else if (!STATE.config.gateway_user) HubPocitac.pending(hubIO(), opts);
  };
  if (STATE.config.gateway_user || !HubServer.isAppWindow()) return onLocal();

  if (returned === 'ukoly') {
    // Z prostoru kvůli úkolu ze serveru: potvrdit ho, nebo přejít na jeho tab.
    // Serverový režim zůstává — do prostoru se vrací přes Nastavení → Účet.
    return onLocal({focus: true});
  }
  if (returned === 'local') {
    toast('Pracuješ na tomhle počítači. Na server se vrátíš v Nastavení → Účet.');
    return onLocal();
  }
  if (returned === 'pocitac' || returned === 'predplatne') {
    // Z prostoru přes „Zapnout na tomhle počítači" / „Použít moje předplatné":
    // vyřídit tady a hned zpátky.
    if (returned === 'pocitac') await HubPocitac.ask(hubIO(), {back: true});
    else await HubPredplatne.connect(hubIO());
    const res = await HubServer.go(hubIO()).catch((e) => ({error: e.message}));
    if (res.ok) return;
    toast('Do prostoru se teď vrátit nepovedlo: ' + (res.error || 'neznámá chyba'));
    return onLocal();
  }
  if (returned === 'logout') {
    HubServer.gate(hubIO(), {loggedOut: true, onLocal,
                             note: 'Odhlášeno. Přihlas se znovu, nebo pracuj na počítači.'});
    return;
  }
  if (STATE.config.server_mode) {
    HubServer.gate(hubIO(), {onLocal});
    return;
  }
  onLocal();
}

/* ── Je venku nová verze? ────────────────────────────────────────────────────
 * Běží při startu na pozadí — načtení okna na to nikdy nečeká a když GitHub
 * neodpoví, mlčí se. Odpověď se drží půl dne, aby se na každé spuštění
 * neťukalo na síť; jakmile je něco venku, ukazuje se z paměti hned. */
const UPDATE_CACHE_KEY = 'hubUpdateCheck';
const UPDATE_CACHE_MS = 12 * 60 * 60 * 1000;

// Značka na GitHubu je „v1.6.0", verze v aplikaci „1.6.0" — bez tohohle by se
// po aktualizaci nikdy nerovnaly a pilulka by svítila napořád.
const bare = (v) => String(v || '').replace(/^v/, '');

function readUpdateCache() {
  try {
    const raw = JSON.parse(localStorage.getItem(UPDATE_CACHE_KEY) || 'null');
    // Po vlastní aktualizaci sedí uložené „latest" na běžící verzi — pak už
    // není co hlásit a záznam se zahodí, ať pilulka nesvítí zbytečně.
    if (raw && raw.latest && bare(raw.latest) !== bare(STATE.version.version)) return raw;
    if (raw) localStorage.removeItem(UPDATE_CACHE_KEY);
  } catch (_) { /* rozbitý nebo nedostupný localStorage nesmí zabít start */ }
  return null;
}

/* Nastavení rovnou na záložce Aktualizace — ta si novou verzi sama zjistí,
   takže stačí kliknout. `start` aktualizaci rovnou i spustí. */
function openUpdateSettings(start) {
  HubSettings.open({...hubIO(), state: STATE, tab: 'aktualizace', startUpdate: !!start});
}

// Co nabízí pruh na úvodu: {latest, run}. Plní ho kontrola na počítači
// (showUpdate) i na serveru (checkServerUpdate).
let welcomeUpdate = null;

function renderWelcomeUpdate() {
  const box = $('welcome-update');
  if (!box) return;
  box.textContent = '';
  box.hidden = !welcomeUpdate;
  if (!welcomeUpdate) return;
  const text = document.createElement('div');
  text.className = 'welcome-update-text';
  const title = document.createElement('b');
  title.textContent = `Je venku nová verze ${welcomeUpdate.latest}`;
  text.appendChild(title);
  text.appendChild(document.createTextNode(
    ` (máš ${STATE.version.version}). Aktualizuj, ať máš nejnovější, a pak začni pracovat.`));
  const btn = document.createElement('button');
  btn.className = 'btn primary';
  btn.innerHTML = icon('i-up') + '<span>Aktualizovat</span>';
  btn.onclick = welcomeUpdate.run;
  box.appendChild(text);
  box.appendChild(btn);
}

function showUpdate(latest) {
  const btn = $('btn-update');
  $('btn-update-text').textContent = `Nová verze ${latest}`;
  btn.title = `Je venku verze ${latest}, máš ${STATE.version.version}. ` +
              `Klikni a aktualizuj.`;
  btn.hidden = false;
  btn.onclick = () => openUpdateSettings(false);
  welcomeUpdate = {latest, run: () => openUpdateSettings(true)};
  renderWelcomeUpdate();
}

async function checkForUpdate() {
  // Na serveru aktualizuje server sám (gateway/update.sh) — není co nabízet.
  if (onServer()) return;
  const cached = readUpdateCache();
  if (cached) showUpdate(cached.latest);
  if (cached && Date.now() - cached.at < UPDATE_CACHE_MS) return;

  let info;
  try {
    info = await api('update-check');
  } catch (_) {
    return;                       // bez sítě se prostě nic neřekne
  }
  if (!info || !info.update_available || !info.latest) {
    try { localStorage.removeItem(UPDATE_CACHE_KEY); } catch (_) { /* nevadí */ }
    return;
  }
  try {
    localStorage.setItem(UPDATE_CACHE_KEY,
      JSON.stringify({at: Date.now(), latest: info.latest}));
  } catch (_) { /* nevadí, jen se příště zeptáme znovu */ }
  showUpdate(info.latest);
  // Toast až po síti: kdyby se ukazoval i z paměti, otravoval by při každém
  // spuštění, dokud člověk neaktualizuje.
  if (!cached || bare(cached.latest) !== bare(info.latest)) {
    toast(`Je tu nová verze aplikace ${info.latest}. ` +
          `Nainstaluješ ji tlačítkem „Nová verze" nahoře.`);
  }
}

/* ── Nová verze na serveru ───────────────────────────────────────────────────
 * Server si novou verzi připraví sám, na pozadí (gateway/update.sh): každá
 * verze má vlastní složku a běžící prostor jede dál z té své — nic se mu
 * nevymění pod rukama. Hub se každou chvíli zeptá brány (/gw/verze), jestli
 * je připravené něco novějšího, a nabídne to:
 *   Aktualizovat — uloží se taby (i rozepsané zprávy), brána prostor
 *                  restartuje už na nové verzi a taby se vrátí, Claude v nich
 *                  pokračuje v konverzaci. Když zrovna pracuje, počká se.
 *   Později      — zeptá se znovu za hodinu; pilulka v hlavičce zůstává. */
const SRV_CHECK_MS = 10 * 60 * 1000;
const SRV_LATER_MS = 60 * 60 * 1000;
const SRV_LATER_KEY = 'hub.serverUpdateLater';
const DRAFTS_KEY = 'hub.restartDrafts';
let srvNewest = '';
let srvCard = null;
let srvTimer = null;

function verNewer(a, b) {
  const x = bare(a).split('.').map((n) => parseInt(n, 10) || 0);
  const y = bare(b).split('.').map((n) => parseInt(n, 10) || 0);
  for (let i = 0; i < Math.max(x.length, y.length); i++) {
    if ((x[i] || 0) !== (y[i] || 0)) return (x[i] || 0) > (y[i] || 0);
  }
  return false;
}

async function checkServerUpdate() {
  if (!onServer()) return;
  clearTimeout(srvTimer);
  srvTimer = setTimeout(checkServerUpdate, SRV_CHECK_MS);
  if (document.hidden) return;
  let res;
  try {
    const r = await fetch('/gw/verze', {credentials: 'same-origin'});
    if (!r.ok) return;                 // starší brána to neumí
    res = await r.json();
  } catch (_) {
    return;
  }
  if (!res || !res.nejnovejsi || !verNewer(res.nejnovejsi, STATE.version.version)) return;
  srvNewest = bare(res.nejnovejsi);
  const btn = $('btn-update');
  $('btn-update-text').textContent = `Nová verze ${srvNewest}`;
  btn.title = `Na serveru je připravená verze ${srvNewest}, tvůj prostor jede na ` +
              `${STATE.version.version}. Klikni a přepni se.`;
  btn.hidden = false;
  btn.onclick = () => showServerUpdate(true);
  welcomeUpdate = {latest: srvNewest, run: () => window.hubServerUpdate()};
  renderWelcomeUpdate();
  showServerUpdate(false);
}
// Po návratu k oknu (telefon z kapsy, druhý monitor) se zeptá hned.
document.addEventListener('visibilitychange', () => {
  if (!document.hidden && onServer()) checkServerUpdate();
});

function showServerUpdate(asked) {
  if (!srvNewest || srvCard) return;
  if (!asked) {
    let later = null;
    try { later = JSON.parse(localStorage.getItem(SRV_LATER_KEY) || 'null'); } catch (_) { /* nic */ }
    if (later && later.version === srvNewest && Date.now() < later.until) return;
  }
  const card = document.createElement('div');
  card.className = 'srvupd';
  card.innerHTML = `
    <div class="srvupd-ico"><svg class="ico"><use href="#i-up"/></svg></div>
    <div class="srvupd-body">
      <div class="srvupd-title"></div>
      <div class="srvupd-text"></div>
      <div class="srvupd-btns">
        <button class="btn primary" data-act="go">Aktualizovat</button>
        <button class="btn ghost" data-act="later">Později</button>
      </div>
    </div>`;
  const q = (sel) => card.querySelector(sel);
  q('.srvupd-title').textContent = `Na serveru je nová verze ${srvNewest}`;
  q('.srvupd-text').textContent = `Tvůj prostor jede na ${STATE.version.version}. ` +
    'Po aktualizaci se taby otevřou znovu a Claude v nich pokračuje, kde skončil — ' +
    'nic se neztratí.';
  q('[data-act=later]').onclick = () => {
    try {
      localStorage.setItem(SRV_LATER_KEY,
        JSON.stringify({version: srvNewest, until: Date.now() + SRV_LATER_MS}));
    } catch (_) { /* připomene se při dalším dotazu */ }
    closeServerUpdate();
    toast('Připomenu se za hodinu. Kdykoli dřív: pilulka „Nová verze" nahoře.');
  };
  q('[data-act=go]').onclick = () => applyServerUpdate(card, false);
  document.body.appendChild(card);
  srvCard = card;
}

function closeServerUpdate() {
  if (srvCard) srvCard.remove();
  srvCard = null;
}

/* Zprávy z fronty na serveru (hub/fronta.py) — jednou na tab, jakmile má číslo. */
function nactiFrontu(tab) {
  if (!tab || !tab.id || tab.frontaNactena || !tab.cteni) return;
  tab.frontaNactena = true;
  api('fronta?tab=' + tab.id).then((r) => {
    if (!r) return;
    if (r.pauza) tab.pozastaveno = true;
    if (tab.cteni && r.zpravy && r.zpravy.length) tab.cteni.obnovFrontu(r.zpravy);
  }).catch(() => { tab.frontaNactena = false; });
}

/* Rozepsaná zpráva (koncept) se ukládá průběžně do úložiště prohlížeče —
   restart serveru, aktualizace, pád okna ani zavřený tab ji nesmažou. Klíč je
   konverzace (stejná i po obnově tabu), dokud ji tab nezná, složka a název. */
const KONCEPT_PREFIX = 'hub.koncept.';
const KONCEPT_STARI = 14 * 86400 * 1000;
function konceptKlice(tab) {
  const out = [];
  const chat = tab.chat || tab.resume;
  if (chat) out.push(KONCEPT_PREFIX + 'chat:' + chat);
  out.push(KONCEPT_PREFIX + 'tab:' + (tab.kind || '') + '|' + (tab.path || '') + '|' + (tab.title || ''));
  return out;
}
function ulozKoncept(tab, text, atts) {
  const prazdny = !String(text || '').trim() && !(atts && atts.length);
  const [hlavni, ...dalsi] = konceptKlice(tab);
  try {
    if (prazdny) {
      for (const k of [hlavni, ...dalsi]) localStorage.removeItem(k);
      return;
    }
    localStorage.setItem(hlavni, JSON.stringify({text: String(text || ''), atts: atts || [], at: Date.now()}));
    // Starší klíč (bez čísla konverzace) by po obnově vrátil zastaralý text.
    for (const k of dalsi) localStorage.removeItem(k);
  } catch (_) { /* plné nebo zakázané úložiště */ }
}
function nactiKoncept(tab) {
  for (const k of konceptKlice(tab)) {
    try {
      const v = JSON.parse(localStorage.getItem(k) || 'null');
      if (v && (v.text || (v.atts && v.atts.length)) && Date.now() - (v.at || 0) < KONCEPT_STARI) return v;
    } catch (_) { /* rozbitý záznam */ }
  }
  return null;
}
function odchoziKlic(tab) {
  return konceptKlice(tab)[0].replace(KONCEPT_PREFIX, 'hub.odchozi.');
}
function ulozOdchozi(tab, list) {
  try {
    if (list && list.length) localStorage.setItem(odchoziKlic(tab), JSON.stringify({at: Date.now(), list}));
    else localStorage.removeItem(odchoziKlic(tab));
  } catch (_) { /* nic */ }
}
function nactiOdchozi(tab) {
  try {
    const v = JSON.parse(localStorage.getItem(odchoziKlic(tab)) || 'null');
    if (v && Date.now() - (v.at || 0) < KONCEPT_STARI && Array.isArray(v.list)) return v.list;
  } catch (_) { /* nic */ }
  return [];
}
function obnovKoncept(tab) {
  if (!tab || !tab.composer || !tab.composer.setDraft) return;
  const v = nactiKoncept(tab);
  if (v) tab.composer.setDraft(v.text, v.atts);
}
// Úklid starých konceptů jednou při startu, ať úložiště neroste donekonečna.
try {
  for (let i = localStorage.length - 1; i >= 0; i--) {
    const k = localStorage.key(i);
    if (!k || !k.startsWith(KONCEPT_PREFIX)) continue;
    const v = JSON.parse(localStorage.getItem(k) || 'null');
    if (!v || Date.now() - (v.at || 0) > KONCEPT_STARI) localStorage.removeItem(k);
  }
} catch (_) { /* nic */ }

function saveDrafts() {
  const drafts = [];
  for (const t of TABS) {
    const text = t.composer && t.composer.draft ? t.composer.draft() : '';
    if (text && text.trim()) drafts.push({path: t.path || '', title: t.title || '', text});
  }
  try { localStorage.setItem(DRAFTS_KEY, JSON.stringify({at: Date.now(), drafts})); } catch (_) { /* nic */ }
}

function takeDrafts() {
  try {
    const raw = JSON.parse(localStorage.getItem(DRAFTS_KEY) || 'null');
    localStorage.removeItem(DRAFTS_KEY);
    if (raw && Date.now() - raw.at < 60 * 60 * 1000 && Array.isArray(raw.drafts)) return raw.drafts;
  } catch (_) { /* nic */ }
  return [];
}

/* Aktualizovat: když Claude v některém tabu pracuje, počká se, až doběhne —
   restart by mu práci utnul. Jde to i hned, ale jen výslovně. */
async function applyServerUpdate(card, now) {
  const q = (sel) => card.querySelector(sel);
  const busy = () => TABS.filter((t) => !t.exited && t.pracuje);
  if (!now && busy().length) {
    q('.srvupd-text').textContent = `Claude ještě pracuje (${busy().length} ${tabyWord(busy().length)}). ` +
      'Aktualizuju, jakmile doběhne — okno můžeš nechat otevřené.';
    const btns = q('.srvupd-btns');
    btns.innerHTML = '<button class="btn ghost" data-act="now">Hned</button>' +
                     '<button class="btn ghost" data-act="cancel">Zrušit</button>';
    let klid = 0;
    const hlidac = setInterval(() => {
      if (!srvCard) return clearInterval(hlidac);
      // Pár vteřin v klidu, ne jen okamžik mezi dvěma kroky.
      klid = busy().length ? 0 : klid + 1;
      if (klid >= 3) { clearInterval(hlidac); applyServerUpdate(card, true); }
    }, 1000);
    q('[data-act=now]').onclick = () => { clearInterval(hlidac); applyServerUpdate(card, true); };
    q('[data-act=cancel]').onclick = () => { clearInterval(hlidac); closeServerUpdate(); };
    return;
  }
  q('.srvupd-btns').innerHTML = '';
  q('.srvupd-text').textContent = 'Ukládám taby a rozepsané zprávy…';
  card.classList.add('busy');
  try {
    saveDrafts();
    await api('prostor-snapshot', {});
    const r = await fetch('/gw/restart', {
      method: 'POST', credentials: 'same-origin',
      headers: {'Content-Type': 'application/json', 'X-Hub-Account': '1'}, body: '{}',
    });
    if (!r.ok) throw new Error('Server teď neodpovídá, zkus to za chvíli.');
  } catch (err) {
    card.classList.remove('busy');
    q('.srvupd-text').textContent = 'Aktualizace se nepovedla: ' + err.message;
    q('.srvupd-btns').innerHTML = '<button class="btn ghost" data-act="close">Zavřít</button>';
    q('[data-act=close]').onclick = closeServerUpdate;
    return;
  }
  q('.srvupd-text').textContent = `Přepínám prostor na verzi ${srvNewest}… Taby se za chvilku vrátí.`;
  // Nový prostor naběhne s prvním požadavkem — tím je načtení stránky.
  setTimeout(() => location.reload(), 1500);
}
// Nastavení → Aktualizace (settings.js) nabízí totéž tlačítkem.
window.hubServerUpdate = () => { closeServerUpdate(); srvNewest ? showServerUpdate(true) : checkServerUpdate(); };


/* ── Aktualizace při spuštění ────────────────────────────────────────────────
 * Při každém startu hub sám zjistí, jestli je novější verze, stáhne ji a
 * nainstaluje — člověk vidí jen animaci: kontroluji → stahuji → instaluji.
 * Vypíná se v Nastavení → Aktualizace (config.auto_update).
 * Na počítači: update-check → update (na pozadí) → restart. Na serveru verzi
 * připravuje server (gateway/update.sh), prostor se jen přepne: uloží se taby
 * a brána ho restartuje už na nové verzi. Pojistka proti smyčce: pro stejnou
 * verzi se to zkusí jednou za 10 minut. */
const AUTO_TRIED_KEY = 'hub.autoUpdateTried';

function startSplash() {
  const root = document.createElement('div');
  root.className = 'startsplash';
  root.innerHTML = `
    <div class="ss-box">
      <div class="ss-ring"><svg class="ico"><use href="#i-aladin"/></svg></div>
      <div class="ss-title">Aladin</div>
      <div class="ss-text"></div>
      <div class="ss-steps"><span data-s="check">Kontrola</span><span data-s="download">Stahování</span><span data-s="install">Instalace</span></div>
    </div>`;
  document.body.appendChild(root);
  const text = root.querySelector('.ss-text');
  const steps = [...root.querySelectorAll('.ss-steps span')];
  return {
    set(stage, msg) {
      text.textContent = msg;
      const order = ['check', 'download', 'install'];
      const at = order.indexOf(stage);
      steps.forEach((el) => {
        const i = order.indexOf(el.dataset.s);
        el.classList.toggle('on', i === at);
        el.classList.toggle('done', i < at);
      });
    },
    close() {
      root.classList.add('out');
      setTimeout(() => root.remove(), 400);
    },
  };
}

function autoTriedRecently(version) {
  try {
    const t = JSON.parse(sessionStorage.getItem(AUTO_TRIED_KEY) || localStorage.getItem(AUTO_TRIED_KEY) || 'null');
    return !!(t && t.version === version && Date.now() - t.at < 10 * 60 * 1000);
  } catch (_) { return false; }
}

function markAutoTried(version) {
  try { localStorage.setItem(AUTO_TRIED_KEY, JSON.stringify({version, at: Date.now()})); } catch (_) { /* nevadí */ }
}

async function autoUpdateAtStart() {
  if (STATE.config && STATE.config.auto_update === false) return;
  const splash = startSplash();
  const started = Date.now();
  const finish = async () => {
    // Aspoň chvilku, ať animace neproblikne.
    await new Promise((r) => setTimeout(r, Math.max(0, 900 - (Date.now() - started))));
    splash.close();
  };
  splash.set('check', 'Kontroluji aktualizace…');
  try {
    if (onServer()) {
      let res = null;
      try {
        const r = await fetch('/gw/verze', {credentials: 'same-origin'});
        if (r.ok) res = await r.json();
      } catch (_) { /* bez sítě se nic neděje */ }
      const newest = res && res.nejnovejsi ? bare(res.nejnovejsi) : '';
      if (!newest || !verNewer(newest, STATE.version.version) || autoTriedRecently(newest)) return finish();
      markAutoTried(newest);
      splash.set('install', `Instaluji verzi ${newest}…`);
      saveDrafts();
      await api('prostor-snapshot', {});
      const r = await fetch('/gw/restart', {
        method: 'POST', credentials: 'same-origin',
        headers: {'Content-Type': 'application/json', 'X-Hub-Account': '1'}, body: '{}',
      });
      if (!r.ok) throw new Error('Server teď neodpovídá.');
      splash.set('install', `Přepínám na verzi ${newest}…`);
      setTimeout(() => location.reload(), 1500);
      return;
    }
    const info = await Promise.race([api('update-check'),
      new Promise((_, no) => setTimeout(() => no(new Error('timeout')), 12000))]);
    if (!info || !info.update_available || !info.latest || autoTriedRecently(bare(info.latest))) return finish();
    const latest = bare(info.latest);
    markAutoTried(latest);
    splash.set('download', `Stahuji verzi ${latest}…`);
    await api('update');
    for (;;) {
      await new Promise((r) => setTimeout(r, 800));
      const st = await api('update-status');
      if (st.running) {
        splash.set(/instal/i.test(st.step || '') ? 'install' : 'download',
          (/instal/i.test(st.step || '') ? 'Instaluji' : 'Stahuji') + ` verzi ${latest}…`);
        continue;
      }
      const r = st.result || {};
      if (!r.ok || !r.changed) {
        if (!r.ok) toast('Automatická aktualizace se nepovedla: ' + (r.detail || 'neznámá chyba'));
        return finish();
      }
      splash.set('install', `Hotovo — restartuji na verzi ${r.now || latest}…`);
      await api('restart');
      return;
    }
  } catch (err) {
    console.warn('automatická aktualizace:', err);
    return finish();
  }
}

main().catch(err => {
  console.error(err);
  toast('Aplikace se nenačetla. Zkus ji zavřít a otevřít znovu.');
});
