/* Upozornění z chatů — co Aladin udělal, když se člověk dívá jinam.
 *
 * Tři události (hlásí je hub.js z tabu):
 *   hotovo  – Claude dokončil práci (tah skončil, nic se neptá)
 *   otazka  – Claude se ptá nebo chce povolení (karta s volbami v bublině)
 *   chyba   – Claude v tabu spadl nebo se zasekl
 *
 * Dvě cesty, obě se dají vypnout v Nastavení → Oznámení:
 *   v aplikaci – hláška dole a záznam ve zvonku nahoře (oznameni.js)
 *   systémová  – oznámení systému (Windows, macOS, Linux, Android) přes
 *                Notification API; na telefonu přes service worker, protože
 *                `new Notification()` tam nejde. Funguje, dokud appka běží
 *                (i schovaná / na pozadí); zavřená appka nic nehlásí.
 *
 * Nastavení je na zařízení (localStorage): na telefonu chce člověk něco
 * jiného než na počítači a povolení systému platí stejně jen pro tohle zařízení.
 */
'use strict';

(function (global) {

  const KLIC = 'hub-upozorneni';
  const KLIC_SEZNAM = 'hub-upozorneni-seznam';
  const MAX_SEZNAM = 40;
  const VYCHOZI = {
    aplikace: true,      // hláška + zvonek
    system: false,       // systémové oznámení (chce povolení prohlížeče)
    zvuk: false,         // krátké pípnutí
    hotovo: true,
    otazka: true,
    chyba: true,
    jenPryc: true,       // jen když se na ten chat zrovna nedívám
  };
  const DRUHY = {
    hotovo: {titul: 'Aladin je hotový', ikona: '✓'},
    otazka: {titul: 'Aladin se ptá', ikona: '?'},
    chyba: {titul: 'Aladin narazil na problém', ikona: '!'},
  };

  let io = null;                 // {toast, focusTab, tabExists, aktivni}
  let seznam = nactiSeznam();
  const posledni = new Map();    // tab+druh → čas (proti opakování)

  function nastaveni() {
    let ulozene = {};
    try { ulozene = JSON.parse(localStorage.getItem(KLIC) || '{}') || {}; } catch (_) { ulozene = {}; }
    return {...VYCHOZI, ...ulozene};
  }
  function uloz(zmena) {
    const n = {...nastaveni(), ...zmena};
    try { localStorage.setItem(KLIC, JSON.stringify(n)); } catch (_) { /* soukromé okno */ }
    return n;
  }

  function nactiSeznam() {
    try {
      const s = JSON.parse(localStorage.getItem(KLIC_SEZNAM) || '[]');
      return Array.isArray(s) ? s.slice(0, MAX_SEZNAM) : [];
    } catch (_) { return []; }
  }
  function ulozSeznam() {
    try { localStorage.setItem(KLIC_SEZNAM, JSON.stringify(seznam.slice(0, MAX_SEZNAM))); } catch (_) { /* nic */ }
    if (global.HubOznameni && global.HubOznameni.mistniZmena) global.HubOznameni.mistniZmena();
  }

  /* ── systémová oznámení ─────────────────────────────────────────────────── */
  const umiSystem = () => 'Notification' in global && global.isSecureContext !== false;
  const povoleni = () => (umiSystem() ? Notification.permission : 'unsupported');

  async function pozadatOPovoleni() {
    if (!umiSystem()) return 'unsupported';
    if (Notification.permission === 'granted' || Notification.permission === 'denied') return Notification.permission;
    try { return await Notification.requestPermission(); } catch (_) { return Notification.permission; }
  }

  async function systemove(z) {
    if (povoleni() !== 'granted') return;
    const telo = z.text || '';
    const data = {tab: z.tab || '', id: z.id};
    const volby = {body: telo, tag: 'aladin-' + (z.tab || '') + '-' + z.druh, renotify: true,
                   icon: '/icon-192.png', badge: '/icon-192.png', data, silent: !nastaveni().zvuk};
    // Napřed obyčejné oznámení (počítač, i appka v Electronu, kde service
    // worker oznámení neumí). Android ho odmítne („Illegal constructor") —
    // tam jde jen přes service worker.
    try {
      const n = new Notification(z.titul, volby);
      n.onclick = () => { n.close(); otevri(z.tab); };
      return;
    } catch (_) { /* zkusí se service worker */ }
    try {
      const reg = navigator.serviceWorker && await Promise.race([
        navigator.serviceWorker.getRegistration(),
        new Promise((ok) => setTimeout(() => ok(null), 800)),
      ]);
      if (reg && reg.showNotification) await reg.showNotification(z.titul, volby);
    } catch (_) { /* systém oznámení odmítl */ }
  }

  /* Klik na oznámení: okno dopředu a chat otevřít. */
  function otevri(tabId) {
    try { global.focus(); } catch (_) { /* nic */ }
    if (global.hubApp && global.hubApp.ukaz) { try { global.hubApp.ukaz(); } catch (_) { /* starší appka */ } }
    if (tabId && io && io.focusTab) io.focusTab(tabId);
  }
  // Klik na oznámení ze service workeru (sw.js → postMessage).
  if (navigator.serviceWorker) {
    navigator.serviceWorker.addEventListener('message', (ev) => {
      const d = ev.data || {};
      if (d.t === 'upozorneni-klik') otevri(d.tab);
    });
  }
  // Appka otevřená z oznámení (sw.js → ?upozorneni=<tab>), když okno neběželo.
  try {
    const u = new URL(location.href);
    const t = u.searchParams.get('upozorneni');
    if (t) {
      u.searchParams.delete('upozorneni');
      history.replaceState(null, '', u.pathname + u.search + u.hash);
      setTimeout(() => otevri(t), 1500);
    }
  } catch (_) { /* nic */ }

  /* ── zvuk ──────────────────────────────────────────────────────────────── */
  let ac = null;
  function pipni(druh) {
    try {
      ac = ac || new (global.AudioContext || global.webkitAudioContext)();
      const t = ac.currentTime;
      const tony = druh === 'otazka' ? [660, 880] : druh === 'chyba' ? [440, 330] : [784, 1046];
      tony.forEach((f, i) => {
        const o = ac.createOscillator();
        const g = ac.createGain();
        o.frequency.value = f;
        o.type = 'sine';
        g.gain.setValueAtTime(0.0001, t + i * 0.14);
        g.gain.exponentialRampToValueAtTime(0.12, t + i * 0.14 + 0.02);
        g.gain.exponentialRampToValueAtTime(0.0001, t + i * 0.14 + 0.18);
        o.connect(g).connect(ac.destination);
        o.start(t + i * 0.14);
        o.stop(t + i * 0.14 + 0.2);
      });
    } catch (_) { /* bez zvuku */ }
  }

  /* ── událost z tabu ─────────────────────────────────────────────────────── */
  /* `druh`: hotovo | otazka | chyba; `tab`: {id, title}; `text`: podrobnost. */
  function udalost(druh, tab, text) {
    const n = nastaveni();
    if (!DRUHY[druh] || !n[druh]) return;
    if (!n.aplikace && !n.system) return;
    const tabId = tab && tab.id ? String(tab.id) : '';
    // Stejná věc ze stejného chatu jen jednou za chvíli (karta s dotazem se
    // překresluje, Claude někdy skončí a hned zase začne).
    const klic = tabId + '|' + druh;
    const ted = Date.now();
    if (ted - (posledni.get(klic) || 0) < 8000) return;
    posledni.set(klic, ted);

    const divaSe = !document.hidden && document.hasFocus() && io && io.aktivni && io.aktivni() === tabId;
    if (n.jenPryc && divaSe) return;

    const chat = (tab && tab.title) || 'Chat';
    const z = {
      id: ted.toString(36) + Math.random().toString(36).slice(2, 6),
      druh, tab: tabId, chat,
      titul: DRUHY[druh].titul + ' · ' + chat,
      text: String(text || (druh === 'hotovo' ? 'Úkol je dokončený.' : druh === 'otazka' ? 'Čeká na tvou odpověď.' : '')).slice(0, 240),
      cas: Math.floor(ted / 1000),
      precteno: false,
    };
    if (n.aplikace) {
      seznam.unshift(z);
      seznam = seznam.slice(0, MAX_SEZNAM);
      ulozSeznam();
      if (io && io.toast && !divaSe) io.toast(DRUHY[druh].ikona + ' ' + z.titul + (z.text ? ' — ' + z.text : ''));
    }
    // Systémové jen když okno není vepředu — jinak stačí hláška v aplikaci.
    if (n.system && (document.hidden || !document.hasFocus() || !n.aplikace)) systemove(z);
    if (n.zvuk) pipni(druh);
  }

  /* Pro zvonek (oznameni.js). */
  function mistni() { return seznam; }
  function precteno() {
    let zmena = false;
    for (const z of seznam) if (!z.precteno) { z.precteno = true; zmena = true; }
    if (zmena) ulozSeznam();
  }
  function smaz(id) {
    seznam = id ? seznam.filter((z) => z.id !== id) : [];
    ulozSeznam();
  }
  function neprectene() { return seznam.filter((z) => !z.precteno).length; }

  function init(hubio) { io = hubio; }

  global.HubUpozorneni = {init, udalost, nastaveni, uloz, povoleni, pozadatOPovoleni, umiSystem,
                          mistni, precteno, smaz, neprectene, otevri, DRUHY,
                          zkouska: () => systemove({titul: 'Aladin · zkouška', text: 'Takhle bude vypadat oznámení.', druh: 'hotovo', tab: '', id: 'zkouska'})};
})(window);
