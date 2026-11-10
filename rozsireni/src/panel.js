/* Aladin panel — chat next to the page.
 *
 * Logs in like the desktop app (device token from the gateway, /gw/zarizeni,
 * works with 2FA and passkeys) and talks to the user's space through
 * /gw/prostor/volani:
 *   stranka_poslat  message (+ the page) into a chat; without a chat a new one
 *   stranka_cteni   what's new in the chat (same blocks as the app's reader)
 *   stranka_chaty   recent chats, to continue in another one
 * The chat is an ordinary chat in the space — Obsidian, connections and skills
 * included — and it shows up in Aladin on the computer and the phone as well.
 *
 * No innerHTML anywhere (AMO linter): everything is built with createElement. */
'use strict';

const B = globalThis.browser ?? globalThis.chrome;

const SERVERY = [
  {base: 'https://test.alba-rosa.cz', nazev: 'Brněnská Buržoazie', popis: 'test.alba-rosa.cz'},
  {base: 'https://aladin.tiskarnalancov.cz', nazev: 'Tiskárna Lančov', popis: 'aladin.tiskarnalancov.cz'},
];

// Pages whose content never leaves the browser (banks, payment gateways).
const CITLIVE = /(^|\.)(airbank|csob|kb|moneta|fio|rb|raiffeisen|unicredit|mbank|creditas|equa|revolut|wise|paypal|gopay|stripe|comgate|thepay|gpwebpay|adyen|paysafe|trustpay|twisto|skippay)\.|ibanking|internetbanking|bankovnictvi|3dsecure|3ds\./i;

const OKNO = new URLSearchParams(location.search).get('okno') === '1';
const OKNO_W = Number(new URLSearchParams(location.search).get('w') || 0);
const VERZE = B.runtime.getManifest().version;

const $ = (id) => document.getElementById(id);
function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
}

// ── state ───────────────────────────────────────────────────────────────────
const S = {
  ucty: {},          // base -> {token, email, jmeno}
  aktivni: '',       // base
  chaty: {},         // base -> current chat id
  autoStranka: true,
  nahlizet: true,    // Aladin may look into the browser himself (gateway/prohlizece.py)
  prohlizecId: '',
  // runtime
  od: 0, bloky: [], fronta: [], pracuje: false, timer: 0, posledniOdeslani: 0,
  screenshot: false, bezStranky: false, cilTab: null, odesila: false, prihlasovani: null,
};

async function nacti() {
  const d = await B.storage.local.get(['ucty', 'aktivni', 'chaty', 'autoStranka', 'nahlizet', 'prohlizecId']);
  S.ucty = d.ucty || {};
  S.aktivni = d.aktivni && S.ucty[d.aktivni] ? d.aktivni : (Object.keys(S.ucty)[0] || '');
  S.chaty = d.chaty || {};
  S.autoStranka = d.autoStranka !== false;
  S.nahlizet = d.nahlizet !== false;
  S.prohlizecId = d.prohlizecId || '';
  if (!/^[A-Za-z0-9_-]{8,64}$/.test(S.prohlizecId)) {
    const b = new Uint8Array(12);
    crypto.getRandomValues(b);
    S.prohlizecId = 'p' + Array.from(b, (x) => x.toString(16).padStart(2, '0')).join('');
    await B.storage.local.set({prohlizecId: S.prohlizecId});
  }
}
function uloz() {
  return B.storage.local.set({ucty: S.ucty, aktivni: S.aktivni, chaty: S.chaty, autoStranka: S.autoStranka,
                              nahlizet: S.nahlizet});
}
const ucet = () => S.ucty[S.aktivni] || null;
const chatId = () => S.chaty[S.aktivni] || '';

// ── gateway ─────────────────────────────────────────────────────────────────
class Odhlaseno extends Error {}

async function gw(base, path, {method = 'GET', token = '', body} = {}) {
  let r;
  try {
    r = await fetch(base + path, {
      method, credentials: 'omit', cache: 'no-store',
      headers: Object.assign({}, token ? {Authorization: 'Bearer ' + token} : {},
                             body ? {'Content-Type': 'application/json'} : {}),
      body: body ? JSON.stringify(body) : undefined,
    });
  } catch (_) {
    throw new Error('Server neodpovídá — jsi připojený k internetu?');
  }
  let data = {};
  try { data = await r.json(); } catch (_) { data = {}; }
  if (r.status === 401) throw new Odhlaseno(data.error || 'Přihlášení vypršelo.');
  if (!r.ok && data.error) throw new Error(data.error);
  if (!r.ok) throw new Error('Server vrátil chybu ' + r.status + '.');
  return data;
}

async function nastroj(tool, args) {
  const u = ucet();
  if (!u) throw new Odhlaseno('Nepřihlášeno.');
  const data = await gw(S.aktivni, '/gw/prostor/volani', {method: 'POST', token: u.token, body: {tool, args}});
  if (!data.ok) throw new Error(data.error || 'Nepovedlo se.');
  return data.result;
}

// ── login (device token, same as the desktop app) ────────────────────────────
function nazevProhlizece() {
  const ua = navigator.userAgent;
  if (/OPR\//.test(ua)) return 'Opera';
  if (/Edg\//.test(ua)) return 'Edge';
  if (/Firefox\//.test(ua)) return 'Firefox';
  if (/Safari\//.test(ua) && !/Chrome\//.test(ua)) return 'Safari';
  return 'Chrome';
}

async function prihlasit(base) {
  base = base.trim().replace(/\/+$/, '');
  if (!/^https:\/\/[^/]+$/.test(base) && !/^http:\/\/(localhost|127\.0\.0\.1)(:\d+)?$/.test(base)) {
    return stavPrihlaseni('Adresa serveru musí začínat https:// (bez cesty za doménou).');
  }
  // Firefox: host permissions are opt-in in MV3 — ask now (we are in a click).
  if (B.permissions && B.permissions.request) {
    try { await B.permissions.request({origins: ['<all_urls>']}); } catch (_) { /* Chrome has them */ }
  }
  stavPrihlaseni('Ověřuji server…');
  try {
    const info = await gw(base, '/gw/info');
    if (info.app !== 'claude-code-hub') throw new Error('Na téhle adrese Aladin není.');
    const z = await gw(base, '/gw/zarizeni', {method: 'POST', body: {label: 'Rozšíření ' + nazevProhlizece()}});
    if (!z.n || !z.url) throw new Error('Server přihlášení nespustil.');
    const karta = await B.tabs.create({url: base + z.url});
    stavPrihlaseni('Dokonči přihlášení na stránce, která se otevřela, a klikni na „Povolit“.');
    const konec = Date.now() + 10 * 60 * 1000;
    const id = Symbol('prihlaseni');
    S.prihlasovani = id;
    while (Date.now() < konec && S.prihlasovani === id) {
      await new Promise((r) => setTimeout(r, 2000));
      let s;
      try {
        s = await gw(base, '/gw/zarizeni?n=' + encodeURIComponent(z.n));
      } catch (err) {
        if (/vypršelo/.test(err.message)) throw err;
        continue;
      }
      if (s.stav === 'hotovo' && s.token) {
        const u = s.user || {};
        S.ucty[base] = {token: s.token, email: u.email || '', jmeno: u.name || u.jmeno || ''};
        S.aktivni = base;
        await uloz();
        stavPrihlaseni('');
        // Back to the page the person was on: the login tab is no longer needed.
        if (karta && karta.id) { try { await B.tabs.remove(karta.id); } catch (_) { /* closed already */ } }
        return zobraz();
      }
    }
    if (S.prihlasovani === id) stavPrihlaseni('Přihlášení vypršelo, zkus to znovu.');
  } catch (err) {
    stavPrihlaseni(err.message);
  }
}
function stavPrihlaseni(t) { $('prihlaseni-stav').textContent = t; }

// ── which tab is "the page" ──────────────────────────────────────────────────
const webova = (t) => t && /^https?:/i.test(t.url || '');
// Pages the browser never lets an extension read: its own settings, about:,
// other extensions, the add-on stores, the built-in PDF viewer.
const OBCHODY = /^https:\/\/(addons\.mozilla\.org|chromewebstore\.google\.com|chrome\.google\.com\/webstore|microsoftedge\.microsoft\.com\/addons|addons\.opera\.com)/i;
const nelzeCist = (t) => !webova(t) || OBCHODY.test(t.url || '');
const nasePanel = (t) => t && (t.url || '').startsWith(B.runtime.getURL(''));

async function aktivniV(query) {
  try {
    const tabs = await B.tabs.query(query);
    return tabs.find((t) => !nasePanel(t)) || null;
  } catch (_) { return null; }
}

async function cilovyTab() {
  // The tab the user is looking at — a web page if there is one, otherwise
  // whatever is active (a browser page we can at least name).
  let t = null;
  if (OKNO && OKNO_W) t = await aktivniV({active: true, windowId: OKNO_W});
  else {
    t = await aktivniV({active: true, currentWindow: true});
    if (!webova(t)) {
      const jiny = await aktivniV({active: true, lastFocusedWindow: true});
      if (webova(jiny) || !t) t = jiny;
    }
  }
  if (!webova(t) && B.windows && B.windows.getLastFocused) {
    try {
      const w = await B.windows.getLastFocused({windowTypes: ['normal']});
      const jiny = await aktivniV({active: true, windowId: w.id});
      if (webova(jiny) || !t) t = jiny;
    } catch (_) { /* keep what we have */ }
  }
  return t;
}

function citlive(url) {
  try { return CITLIVE.test(new URL(url).hostname); } catch (_) { return false; }
}

// Firefox (MV3) may install the extension without access to websites — the
// user then has to allow it once. Chrome grants it at install time.
async function maPristup() {
  if (!B.permissions || !B.permissions.contains) return true;
  try { return await B.permissions.contains({origins: ['<all_urls>']}); } catch (_) { return true; }
}

async function povolitPristup() {
  try {
    const ok = await B.permissions.request({origins: ['<all_urls>']});
    if (!ok) chyba('Bez povolení Aladin stránky nevidí. Jde to povolit i v Doplňcích → Aladin → Oprávnění.');
  } catch (err) { chyba('Povolení nešlo vyžádat: ' + err.message); }
  obnovChip();
}

async function obnovChip() {
  const t = await cilovyTab();
  S.cilTab = t;
  const chip = $('stranka-chip');
  const povolit = $('stranka-povolit');
  povolit.hidden = true;
  if (!t || !S.autoStranka) { chip.hidden = true; return; }
  chip.hidden = false;
  const zakazane = webova(t) && citlive(t.url);
  const nelze = nelzeCist(t);
  const pristup = nelze || zakazane || await maPristup();
  let popis = t.title || t.url || 'Stránka';
  if (zakazane) popis = 'Nepřikládá se (banka/platba): ' + popis;
  else if (nelze) popis = 'Stránka prohlížeče, obsah nejde číst: ' + popis;
  else if (!pristup) popis = 'Aladin zatím nesmí číst stránky: ' + popis;
  chip.classList.toggle('vyp', S.bezStranky || zakazane);
  chip.classList.toggle('pozor', !zakazane && (nelze || !pristup));
  $('stranka-nazev').textContent = popis;
  chip.title = zakazane ? 'Obsah bankovních a platebních stránek se Aladinovi neposílá.'
    : nelze ? 'Stránky prohlížeče (about:, nastavení, obchod s doplňky) rozšíření číst nesmí. Aladin dostane jen adresu a název.\n' + (t.url || '')
    : !pristup ? 'Klikni na „Povolit“, ať Aladin vidí text stránky.'
    : 'Tahle stránka se přiloží ke zprávě\n' + t.url;
  povolit.hidden = pristup;
  const ikona = $('stranka-ikona');
  if (t.favIconUrl && /^https?:|^data:/.test(t.favIconUrl)) { ikona.src = t.favIconUrl; ikona.hidden = false; } else ikona.hidden = true;
}

// Runs inside the page.
function vytahniStranku() {
  const sel = String(window.getSelection ? window.getSelection() : '');
  const body = document.body ? document.body.innerText : '';
  return {url: location.href, title: document.title, vyber: sel.slice(0, 4000), text: (body || '').slice(0, 400000)};
}

async function stranka(t) {
  const zaklad = {url: t.url || '', title: t.title || ''};
  if (webova(t) && citlive(t.url)) return null;
  if (nelzeCist(t)) return {...zaklad, nelze: 'stránka prohlížeče — rozšíření ji číst nesmí'};
  try {
    const res = await B.scripting.executeScript({target: {tabId: t.id}, func: vytahniStranku});
    const v = res && res[0] && res[0].result;
    if (v && v.url) return v;
    return {...zaklad, nelze: 'stránka nic nevrátila'};
  } catch (err) {
    // No permission (Firefox before „Povolit“), PDF viewer, a page still loading…
    const bezPristupu = !(await maPristup());
    return {...zaklad, nelze: bezPristupu ? 'rozšíření zatím nemá povolený přístup ke stránkám'
      : 'obsah nešel přečíst (' + String(err && err.message || err).slice(0, 120) + ')'};
  }
}

async function snimek(t) {
  const url = await B.tabs.captureVisibleTab(t.windowId, {format: 'jpeg', quality: 72});
  return url || '';
}

// ── sending ─────────────────────────────────────────────────────────────────
async function odeslat() {
  const pole = $('text');
  const text = pole.value.trim();
  if (!text || S.odesila) return;
  S.odesila = true;
  $('poslat').disabled = true;
  chyba('');
  try {
    const args = {text, id: 'r' + Date.now().toString(36) + Math.random().toString(36).slice(2, 6)};
    const chat = chatId();
    if (chat) args.chat = chat;
    const t = await cilovyTab();
    if (t && S.autoStranka && !S.bezStranky) {
      const s = await stranka(t);
      if (s) args.stranka = s;
    }
    if (t && S.screenshot && !citlive(t.url || '')) {
      try { args.obrazek = await snimek(t); } catch (err) { throw new Error('Screenshot nešel udělat: ' + err.message); }
    }
    if (!args.stranka && t) args.title = t.title || '';
    const r = await nastroj('stranka_poslat', args);
    if (r.chat && r.chat !== chat) {
      S.chaty[S.aktivni] = r.chat;
      S.od = 0; S.bloky = [];
      await uloz();
    }
    if (r.title) $('chat-nazev').textContent = r.title;
    pole.value = '';
    S.screenshot = false; S.bezStranky = false;
    $('screenshot').classList.remove('on');
    S.posledniOdeslani = Date.now();
    vykresli();
    planuj(300);
  } catch (err) {
    if (err instanceof Odhlaseno) return odhlasenoZnovu(err);
    chyba(err.message);
  } finally {
    S.odesila = false;
    $('poslat').disabled = false;
    obnovChip();
  }
}

function chyba(t) { const c = $('chyba'); c.textContent = t; c.hidden = !t; }

// ── reading ─────────────────────────────────────────────────────────────────
async function cist() {
  const chat = chatId();
  if (!chat || !ucet()) return;
  try {
    const d = await nastroj('stranka_cteni', {chat, od: S.od});
    if (chat !== chatId()) return;
    const nove = (d.blocks || []).filter((b) => ['me', 'say', 'tool'].includes(b.kind) && !b.tichy);
    if (S.od === 0) S.bloky = nove; else S.bloky.push(...nove);
    if (d.next) S.od = d.next;
    S.fronta = d.fronta || [];
    S.pracuje = !!d.pracuje;
    if (d.title) $('chat-nazev').textContent = d.title;
    chyba('');
    vykresli();
  } catch (err) {
    if (err instanceof Odhlaseno) return odhlasenoZnovu(err);
    chyba(err.message);
  }
}

function planuj(ms) {
  clearTimeout(S.timer);
  S.timer = setTimeout(async () => {
    if (document.visibilityState === 'visible' && chatId()) await cist();
    const zive = S.pracuje || S.fronta.length || Date.now() - S.posledniOdeslani < 30000;
    planuj(zive ? 1500 : 6000);
  }, ms);
}

// ── rendering ───────────────────────────────────────────────────────────────
const ZNACKA = '\n[Stránka v prohlížeči';

function zprava(text) {
  // Our own message: what the person wrote; the page context only as a note.
  const i = text.indexOf(ZNACKA);
  const vlastni = (i >= 0 ? text.slice(0, i) : text).trim();
  const m = i >= 0 ? /Název: (.*)/.exec(text.slice(i)) || /Adresa: (.*)/.exec(text.slice(i)) : null;
  return {vlastni, stranka: m ? m[1] : '', screenshot: /Screenshot/.test(text.slice(Math.max(0, i)))};
}

function bublinaMe(text, ceka) {
  const z = zprava(text);
  const d = el('div', 'z-me' + (ceka ? ' ceka' : ''), z.vlastni || '(stránka)');
  if (z.stranka || z.screenshot) {
    d.appendChild(el('span', 'pril', '📄 ' + (z.stranka || 'stránka') + (z.screenshot ? ' · 📷 screenshot' : '')));
  }
  if (ceka) d.appendChild(el('span', 'pril', 'Odesláno, Aladin si to za chvíli vezme…'));
  return d;
}

function inline(parent, text) {
  // **bold**, `code`, [text](url) and bare links.
  const re = /(\*\*([^*]+)\*\*|`([^`]+)`|\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)|(https?:\/\/[^\s)<>]+))/g;
  let last = 0, m;
  while ((m = re.exec(text))) {
    if (m.index > last) parent.appendChild(document.createTextNode(text.slice(last, m.index)));
    if (m[2]) parent.appendChild(el('strong', '', m[2]));
    else if (m[3]) parent.appendChild(el('code', '', m[3]));
    else {
      const a = el('a', '', m[4] || m[6]);
      a.href = m[5] || m[6]; a.target = '_blank'; a.rel = 'noopener';
      parent.appendChild(a);
    }
    last = m.index + m[0].length;
  }
  if (last < text.length) parent.appendChild(document.createTextNode(text.slice(last)));
}

function markdown(text) {
  const box = el('div', 'z-say');
  const radky = String(text || '').split('\n');
  let i = 0;
  while (i < radky.length) {
    const r = radky[i];
    if (/^```/.test(r)) {
      const kod = [];
      i++;
      while (i < radky.length && !/^```/.test(radky[i])) kod.push(radky[i++]);
      i++;
      const pre = el('pre');
      pre.appendChild(el('code', '', kod.join('\n')));
      box.appendChild(pre);
      continue;
    }
    if (/^\s*[-*•] /.test(r)) {
      const ul = el('ul');
      while (i < radky.length && /^\s*[-*•] /.test(radky[i])) {
        const li = el('li');
        inline(li, radky[i].replace(/^\s*[-*•] /, ''));
        ul.appendChild(li);
        i++;
      }
      box.appendChild(ul);
      continue;
    }
    if (/^#{1,6} /.test(r)) {
      const h = el('h3');
      inline(h, r.replace(/^#+ /, ''));
      box.appendChild(h);
      i++;
      continue;
    }
    if (!r.trim()) { i++; continue; }
    const p = el('p');
    const odstavec = [];
    while (i < radky.length && radky[i].trim() && !/^```|^\s*[-*•] |^#{1,6} /.test(radky[i])) odstavec.push(radky[i++]);
    odstavec.forEach((t, j) => { if (j) p.appendChild(el('br')); inline(p, t); });
    box.appendChild(p);
  }
  return box;
}

function vykresli() {
  const box = $('zpravy');
  const dole = box.scrollHeight - box.scrollTop - box.clientHeight < 60;
  const uzly = [];
  if (!chatId() && !S.fronta.length) {
    const u = el('div', 'uvod');
    u.appendChild(el('h2', '', 'S čím ti mám pomoct?'));
    u.appendChild(el('p', '', 'Vidím stránku, na které jsi, a mám tvůj Obsidian i napojení. Napiš, co potřebuješ.'));
    uzly.push(u);
  }
  const videne = new Set();
  for (const b of S.bloky) {
    if (b.kind === 'me') { uzly.push(bublinaMe(b.text || '', false)); videne.add((b.text || '').trim()); }
    else if (b.kind === 'say') uzly.push(markdown(b.text));
    else if (b.kind === 'tool') {
      const d = el('div', 'z-tool');
      d.appendChild(el('span', '', '•'));
      d.appendChild(el('span', 'n', [b.title || b.name, b.meta].filter(Boolean).join(' · ')));
      uzly.push(d);
    }
  }
  for (const f of S.fronta) if (!videne.has((f.text || '').trim())) uzly.push(bublinaMe(f.text || '', true));
  box.replaceChildren(...uzly);
  $('prace').hidden = !(S.pracuje || S.fronta.length);
  $('prace-text').textContent = S.fronta.length && !S.pracuje ? 'Aladin se chystá…' : 'Aladin pracuje…';
  if (dole) box.scrollTop = box.scrollHeight;
}

// ── chat list ───────────────────────────────────────────────────────────────
async function seznamChatu() {
  const panel = $('seznam-chatu');
  if (!panel.hidden) { panel.hidden = true; return; }
  panel.replaceChildren(el('div', 'prazdny', 'Načítám…'));
  panel.hidden = false;
  try {
    const r = await nastroj('stranka_chaty', {limit: 40});
    const uzly = [];
    const novy = el('button');
    novy.appendChild(el('span', 't', '＋ Nový chat'));
    novy.onclick = () => { panel.hidden = true; novyChat(); };
    uzly.push(novy);
    for (const c of r.chaty || []) {
      const b = el('button', c.id === chatId() ? 'on' : '');
      b.appendChild(el('span', 't', c.title || 'Chat'));
      if (c.posledni) b.appendChild(el('span', 'p', c.posledni));
      b.onclick = () => { panel.hidden = true; prepniChat(c.id, c.title); };
      uzly.push(b);
    }
    panel.replaceChildren(...uzly);
  } catch (err) {
    if (err instanceof Odhlaseno) { panel.hidden = true; return odhlasenoZnovu(err); }
    panel.replaceChildren(el('div', 'prazdny', err.message));
  }
}

async function prepniChat(id, title) {
  S.chaty[S.aktivni] = id;
  S.od = 0; S.bloky = []; S.fronta = []; S.pracuje = false;
  $('chat-nazev').textContent = title || 'Chat';
  await uloz();
  vykresli();
  await cist();
  planuj(1500);
}

async function novyChat() {
  delete S.chaty[S.aktivni];
  S.od = 0; S.bloky = []; S.fronta = []; S.pracuje = false;
  $('chat-nazev').textContent = 'Nový chat';
  await uloz();
  vykresli();
  $('text').focus();
}

async function otevritVAladinovi() {
  const u = ucet();
  if (!u) return;
  try {
    const r = await gw(S.aktivni, '/gw/handoff', {method: 'POST', token: u.token});
    await B.tabs.create({url: r.url || S.aktivni});
  } catch (err) {
    if (err instanceof Odhlaseno) return odhlasenoZnovu(err);
    await B.tabs.create({url: S.aktivni});
  }
}

// ── accounts ────────────────────────────────────────────────────────────────
function odhlasenoZnovu(err) {
  if (S.aktivni) delete S.ucty[S.aktivni];
  S.aktivni = Object.keys(S.ucty)[0] || '';
  uloz();
  zobraz();
  stavPrihlaseni((err && err.message ? err.message + ' ' : '') + 'Přihlas se znovu.');
}

function vykresliUcty() {
  const box = $('ucty-seznam');
  const uzly = [];
  for (const [base, u] of Object.entries(S.ucty)) {
    const r = el('div', 'ucet-radek' + (base === S.aktivni ? ' aktivni' : ''));
    const info = el('div', 'info');
    const sv = SERVERY.find((s) => s.base === base);
    info.appendChild(el('b', '', sv ? sv.nazev : new URL(base).host));
    info.appendChild(el('small', '', (u.email || '') + ' · ' + new URL(base).host));
    r.appendChild(info);
    if (base !== S.aktivni) {
      const pouzit = el('button', 'mini', 'Používat');
      pouzit.onclick = async () => { S.aktivni = base; S.od = 0; S.bloky = []; await uloz(); vykresliUcty(); };
      r.appendChild(pouzit);
    }
    const odhlasit = el('button', 'mini', 'Odhlásit');
    odhlasit.onclick = async () => {
      delete S.ucty[base]; delete S.chaty[base];
      if (S.aktivni === base) S.aktivni = Object.keys(S.ucty)[0] || '';
      await uloz();
      if (!S.aktivni) return zobraz();
      vykresliUcty();
    };
    r.appendChild(odhlasit);
    uzly.push(r);
  }
  box.replaceChildren(...uzly);
  $('auto-stranka').checked = S.autoStranka;
  $('nahlizet').checked = S.nahlizet;
}

function vykresliServery() {
  const uzly = SERVERY.map((s) => {
    const b = el('button', 'server');
    b.type = 'button';
    b.appendChild(el('b', '', s.nazev));
    b.appendChild(el('small', '', s.popis + (S.ucty[s.base] ? ' · přihlášeno' : '')));
    b.onclick = () => prihlasit(s.base);
    return b;
  });
  $('servery').replaceChildren(...uzly);
}

// ── screens ─────────────────────────────────────────────────────────────────
let obrazovka = '';
function zobraz(kam) {
  if (!kam) kam = ucet() ? 'chat' : 'prihlaseni';
  obrazovka = kam;
  $('prihlaseni').hidden = kam !== 'prihlaseni';
  $('ucty').hidden = kam !== 'ucty';
  $('chat').hidden = kam !== 'chat';
  $('psani').hidden = kam !== 'chat';
  for (const id of ['chat-volba', 'novy', 'otevrit']) $(id).hidden = kam !== 'chat';
  $('seznam-chatu').hidden = true;
  if (kam === 'prihlaseni') vykresliServery();
  if (kam === 'ucty') vykresliUcty();
  if (kam === 'chat') {
    const u = ucet();
    const sv = SERVERY.find((s) => s.base === S.aktivni);
    $('ucet').textContent = (sv ? sv.nazev : new URL(S.aktivni).host) + (u && u.email ? ' · ' + u.email : '');
    if (!chatId()) $('chat-nazev').textContent = 'Nový chat';
    S.od = 0; S.bloky = []; S.fronta = [];
    vykresli();
    obnovChip();
    mostOznam();
    if (chatId()) { cist(); planuj(1500); }
    prevzitCekajici();
  }
}

// Right-click menu "Zeptat se Aladina…" (background.js) left something for us.
async function prevzitCekajici() {
  const d = await B.storage.local.get('ceka');
  const c = d.ceka;
  if (!c || Date.now() - (c.cas || 0) > 60000) return;
  await B.storage.local.remove('ceka');
  const pole = $('text');
  if (c.vyber) pole.value = '„' + c.vyber.trim().slice(0, 2000) + '“\n\n' + pole.value;
  pole.focus();
  pole.setSelectionRange(pole.value.length, pole.value.length);
}

// ── wiring ──────────────────────────────────────────────────────────────────
function propoj() {
  $('poslat').onclick = odeslat;
  $('text').addEventListener('keydown', (ev) => {
    if (ev.key === 'Enter' && !ev.shiftKey && !ev.isComposing) { ev.preventDefault(); odeslat(); }
  });
  $('text').addEventListener('input', () => {
    const t = $('text');
    t.style.height = 'auto';
    t.style.height = Math.min(t.scrollHeight, innerHeight * 0.4) + 'px';
  });
  $('screenshot').onclick = () => { S.screenshot = !S.screenshot; $('screenshot').classList.toggle('on', S.screenshot); };
  $('stranka-vypnout').onclick = () => { S.bezStranky = !S.bezStranky; obnovChip(); };
  $('stranka-povolit').onclick = povolitPristup;
  $('chat-volba').onclick = seznamChatu;
  $('novy').onclick = novyChat;
  $('otevrit').onclick = otevritVAladinovi;
  $('nastaveni').onclick = () => zobraz(obrazovka === 'ucty' || !ucet() ? undefined : 'ucty');
  $('ucty-zpet').onclick = () => zobraz();
  $('pridat-ucet').onclick = () => zobraz('prihlaseni');
  $('auto-stranka').onchange = async (ev) => { S.autoStranka = ev.target.checked; await uloz(); };
  $('nahlizet').onchange = async (ev) => { S.nahlizet = ev.target.checked; await uloz(); mostOznam(); };
  addEventListener('pagehide', mostSbohem);
  $('jiny-form').onsubmit = (ev) => { ev.preventDefault(); prihlasit($('jiny-url').value); };
  document.addEventListener('click', (ev) => {
    const panel = $('seznam-chatu');
    if (!panel.hidden && !panel.contains(ev.target) && !$('chat-volba').contains(ev.target)) panel.hidden = true;
  });
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible' && obrazovka === 'chat') { obnovChip(); planuj(100); }
  });
  if (B.tabs.onActivated) B.tabs.onActivated.addListener(() => { S.bezStranky = false; obnovChip(); });
  if (B.tabs.onUpdated) B.tabs.onUpdated.addListener((_id, zmena) => { if (zmena.title || zmena.url || zmena.status === 'complete') obnovChip(); });
  if (B.windows && B.windows.onFocusChanged) B.windows.onFocusChanged.addListener(() => obnovChip());
  B.storage.onChanged.addListener((zmeny) => { if (zmeny.ceka && zmeny.ceka.newValue && obrazovka === 'chat') prevzitCekajici(); });
}

// ── Aladin looks into the browser himself ───────────────────────────────────
// While this panel is open, Claude in the space can ask for the open tabs, the
// text or HTML of a page and a screenshot (tools/pocitac_mcp.py prohlizec_* →
// gateway/prohlizece.py). The panel long-polls the gateway for such requests and
// answers them. Read-only; banks and payment pages are refused.
const MOST = {bezi: false, base: '', posledni: ''};
const spi = (ms) => new Promise((r) => setTimeout(r, ms));

function mostOznam(text) {
  const e = $('most');
  if (!e) return;
  if (text) { MOST.posledni = text; }
  e.hidden = !(S.nahlizet && ucet());
  $('most-text').textContent = MOST.posledni || 'Aladin vidí tvůj prohlížeč, dokud je panel otevřený';
}

async function mostSmycka() {
  if (MOST.bezi) return;
  MOST.bezi = true;
  let pauza = 0;
  for (;;) {
    if (pauza) { await spi(pauza); pauza = 0; }
    const u = ucet();
    if (!u || !S.nahlizet) { await spi(2000); continue; }
    const base = S.aktivni;
    MOST.base = base;
    let r;
    try {
      r = await gw(base, '/gw/prohlizec/poll', {method: 'POST', token: u.token, body: {
        prohlizec: {id: S.prohlizecId, name: nazevProhlizece(), browser: nazevProhlizece(), version: VERZE}, wait: 25}});
    } catch (err) {
      // Old server without the bridge (404), offline, logged out — try again later.
      pauza = err instanceof Odhlaseno ? 30000 : /404|nic|Neznámý/.test(err.message) ? 120000 : 5000;
      continue;
    }
    for (const t of r.tasks || []) vyridUkol(base, u.token, t);
  }
}

function mostSbohem() {
  const u = S.ucty[MOST.base];
  if (!u || !MOST.base) return;
  try {
    fetch(MOST.base + '/gw/prohlizec/poll', {method: 'POST', keepalive: true, credentials: 'omit',
      headers: {'Content-Type': 'application/json', Authorization: 'Bearer ' + u.token},
      body: JSON.stringify({bye: true, prohlizec: {id: S.prohlizecId}})});
  } catch (_) { /* closing anyway */ }
}

const POPIS_UKOLU = {karty: 'Aladin se podíval na tvoje karty', stranka: 'Aladin si přečetl stránku',
  snimek: 'Aladin si udělal screenshot'};

async function vyridUkol(base, token, ukol) {
  let vysledek;
  try {
    vysledek = {ok: true, result: await provedUkol(ukol.op, ukol.args || {})};
    const cas = new Date().toLocaleTimeString('cs-CZ', {hour: '2-digit', minute: '2-digit'});
    mostOznam((POPIS_UKOLU[ukol.op] || 'Aladin se díval do prohlížeče') + ' · ' + cas);
  } catch (err) {
    vysledek = {ok: false, error: String(err && err.message || err)};
  }
  try {
    await gw(base, '/gw/prohlizec/vysledek', {method: 'POST', token,
      body: Object.assign({prohlizec: S.prohlizecId, id: ukol.id}, vysledek)});
  } catch (_) { /* the space gets a timeout */ }
}

async function kartaPodleArgs(args) {
  if (args.karta !== undefined && args.karta !== null && args.karta !== '') {
    try { return await B.tabs.get(Number(args.karta)); } catch (_) { throw new Error('Karta ' + args.karta + ' už není otevřená.'); }
  }
  const t = await cilovyTab();
  if (!t) throw new Error('V prohlížeči teď není otevřená žádná karta.');
  return t;
}

function hlidejKartu(t) {
  if (webova(t) && citlive(t.url)) throw new Error('Tohle je bankovní nebo platební stránka — tu Aladin z prohlížeče nečte.');
  if (nelzeCist(t)) throw new Error('Tohle je stránka samotného prohlížeče (' + (t.url || '') + ') — tu žádné rozšíření číst nesmí.');
}

// Runs inside the page.
function obsahStranky(co, selektor, cele) {
  const koren = selektor ? document.querySelector(selektor) : document.documentElement;
  if (!koren) return {chyba: 'Na stránce není nic pro selektor ' + selektor};
  const zaklad = {url: location.href, title: document.title};
  if (co === 'html') {
    const kopie = koren.cloneNode(true);
    if (!cele) {
      for (const e of kopie.querySelectorAll('script, style, noscript, template')) e.remove();
      for (const e of kopie.querySelectorAll('svg')) { while (e.firstChild) e.firstChild.remove(); }
      for (const e of kopie.querySelectorAll('[style]')) e.removeAttribute('style');
    }
    for (const e of kopie.querySelectorAll('input[type=password]')) e.removeAttribute('value');
    return Object.assign(zaklad, {obsah: kopie.outerHTML.slice(0, 2000000)});
  }
  if (co === 'odkazy') {
    const radky = [];
    for (const a of koren.querySelectorAll('a[href]')) {
      const text = (a.innerText || a.getAttribute('aria-label') || a.title || '').replace(/\s+/g, ' ').trim().slice(0, 150);
      radky.push((text || '(bez textu)') + ' → ' + a.href);
      if (radky.length >= 2000) break;
    }
    return Object.assign(zaklad, {obsah: radky.join('\n')});
  }
  if (co === 'formulare') {
    const radky = [];
    for (const e of koren.querySelectorAll('input, select, textarea, button')) {
      const typ = (e.type || e.tagName).toLowerCase();
      if (typ === 'hidden') continue;
      const label = (e.labels && e.labels[0] && e.labels[0].innerText) || e.getAttribute('aria-label') || e.placeholder || e.name || e.id || '';
      let hodnota = typ === 'password' ? '(heslo — neposílá se)' : e.tagName === 'SELECT'
        ? (e.selectedOptions[0] ? e.selectedOptions[0].text : '') : (typ === 'checkbox' || typ === 'radio') ? (e.checked ? 'zaškrtnuto' : 'nezaškrtnuto')
        : e.tagName === 'BUTTON' ? (e.innerText || '') : (e.value || '');
      hodnota = String(hodnota).replace(/\s+/g, ' ').trim().slice(0, 300);
      radky.push(`[${typ}] ${String(label).replace(/\s+/g, ' ').trim().slice(0, 100)}${e.name ? ' (name=' + e.name + ')' : ''}: ${hodnota}`);
      if (radky.length >= 1000) break;
    }
    return Object.assign(zaklad, {obsah: radky.join('\n') || '(na stránce nejsou žádná pole)'});
  }
  return Object.assign(zaklad, {obsah: (koren.innerText || '').slice(0, 2000000)});
}

async function provedUkol(op, args) {
  if (!S.nahlizet) throw new Error('Uživatel v rozšíření vypnul, aby se Aladin díval do prohlížeče.');
  if (op === 'karty') {
    const tabs = await B.tabs.query({});
    const aktivni = await cilovyTab();
    return {karty: tabs.filter((t) => !nasePanel(t)).slice(0, 300).map((t) => ({
      id: t.id, title: (t.title || '').slice(0, 200),
      url: webova(t) && citlive(t.url) ? new URL(t.url).origin + '/… (banka/platba)' : (t.url || '').slice(0, 500),
      aktivni: !!(aktivni && aktivni.id === t.id), okno: t.windowId,
      lze_cist: !nelzeCist(t) && !(webova(t) && citlive(t.url))}))};
  }
  if (op === 'stranka') {
    const t = await kartaPodleArgs(args);
    hlidejKartu(t);
    if (!(await maPristup())) throw new Error('Rozšíření zatím nemá povolený přístup ke stránkám — uživatel musí v panelu Aladina kliknout na „Povolit“.');
    const co = ['text', 'html', 'odkazy', 'formulare'].includes(args.co) ? args.co : 'text';
    let res;
    try {
      res = await B.scripting.executeScript({target: {tabId: t.id}, func: obsahStranky,
        args: [co, String(args.selektor || ''), !!args.cele]});
    } catch (err) {
      throw new Error('Stránka nejde přečíst: ' + (err && err.message || err));
    }
    const v = res && res[0] && res[0].result;
    if (!v) throw new Error('Stránka nic nevrátila (možná se ještě načítá).');
    if (v.chyba) throw new Error(v.chyba);
    return v;
  }
  if (op === 'snimek') {
    const t = await cilovyTab();
    if (!t) throw new Error('V prohlížeči teď není otevřená žádná karta.');
    hlidejKartu(t);
    const data = await B.tabs.captureVisibleTab(t.windowId, {format: 'jpeg', quality: 70});
    return {data, mime: 'image/jpeg', url: t.url, title: t.title};
  }
  throw new Error('Tohle rozšíření zatím neumí: ' + op);
}

(async () => {
  document.documentElement.dataset.verze = VERZE;
  await nacti();
  propoj();
  zobraz();
  mostOznam();
  mostSmycka();
})();
