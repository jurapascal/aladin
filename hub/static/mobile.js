/* Co hub potřebuje navíc, když ho někdo otevře v telefonu.
 *
 * Rozvržení řeší hub.css: na úzké obrazovce se z postranního panelu stane
 * šuplík. Tady je jen to, co musí umět kód — otevírání šuplíku, dlouhý stisk
 * místo pravého tlačítka (iOS žádné contextmenu neposílá), přepočet terminálu
 * při vyjetí měkké klávesnice a registrace service workeru, bez kterého
 * Android nenabídne instalaci na plochu.
 *
 * Se stránkou se domlouvá přes DOM a `resize`, ne přes vnitřek hub.js —
 * v prohlížeči na počítači se tenhle soubor jen tiše nezapne.
 */
'use strict';

(function () {

  const NARROW = '(max-width: 820px)';
  const LONG_PRESS_MS = 500;
  const DOUBLE_TAP_MS = 300;   // dvě ťuknutí rychleji = dvojklik
  const MOVE_TOLERANCE = 10;   // px, nad to je to scroll a ne podržení

  const $ = (id) => document.getElementById(id);

  /* ── třídy na <body> ──────────────────────────────────────────────────── */
  // Dotyk a úzká obrazovka jsou dvě různé věci: tablet je dotykový a široký,
  // okno hubu zmenšené na půlku obrazovky je úzké a myší.
  function applyClasses() {
    const narrow = window.matchMedia(NARROW).matches;
    document.body.classList.toggle('is-narrow', narrow);
    if (!narrow) closeDrawer();
    moveActionbar(narrow);
  }

  const touch = matchMedia('(pointer: coarse)').matches;
  document.body.classList.toggle('is-touch', touch);
  // Standalone = spuštěno z ikony na ploše, ne z prohlížeče.
  if (matchMedia('(display-mode: standalone)').matches ||
      window.navigator.standalone) {
    document.body.classList.add('is-app');
  }

  /* ── šuplík s projekty ────────────────────────────────────────────────── */
  const scrim = $('drawer-scrim');
  const toggle = $('btn-drawer');

  function openDrawer() {
    document.body.classList.add('drawer-open');
    if (scrim) scrim.hidden = false;
  }

  function closeDrawer() {
    document.body.classList.remove('drawer-open');
    if (scrim) scrim.hidden = true;
  }

  if (toggle) {
    toggle.onclick = () => {
      if (document.body.classList.contains('drawer-open')) closeDrawer();
      else openDrawer();
    };
  }
  if (scrim) scrim.onclick = closeDrawer;
  // Šuplík otevírá i nabídka v liště (hub.js) — odsud na ni sahá.
  window.HubMobile = {openDrawer, closeDrawer};
  // Křížek v hlavičce šuplíku: klika, která ho otevřela, je pod ním.
  const closeBtn = $('btn-drawer-close');
  if (closeBtn) closeBtn.onclick = closeDrawer;

  // Klik na projekt otevře tab — šuplík už jen překáží.
  for (const id of ['projects', 'chats', 'memory', 'actions']) {
    const box = $(id);
    // Kromě tlačítek, která jen rozbalují seznam (Zobrazit starší, celá historie).
    if (box) box.addEventListener('click', (ev) => {
      if (ev.target.closest && ev.target.closest('.keep-drawer')) return;
      setTimeout(closeDrawer, 60);
    });
  }
  document.addEventListener('keydown', (ev) => {
    if (ev.key === 'Escape') closeDrawer();
  });

  /* Rychlé akce mají na telefonu vlastní sloupec vedle terminálu kde vzít —
     tak se stěhují do šuplíku pod projekty. Přesouvá se celý prvek, takže
     tlačítka v něm si dál plní hub.js a o ničem neví. */
  const layout = document.querySelector('.layout');
  const sidebar = document.querySelector('.sidebar');
  const actionbar = $('actionbar');
  const spacer = sidebar ? sidebar.querySelector('.spacer') : null;

  function moveActionbar(narrow) {
    if (!actionbar || !layout || !sidebar) return;
    if (narrow && actionbar.parentElement !== sidebar) {
      sidebar.insertBefore(actionbar, spacer);
    } else if (!narrow && actionbar.parentElement !== layout) {
      layout.appendChild(actionbar);
    }
  }

  /* ── jako appka Claude: taby v šuplíku, jméno tabu v liště ────────────── */
  /* Lišta s taby se na telefon nevejde (a v appce Claude žádná není): otevřené
     taby jsou v šuplíku nad projekty a jméno toho, na který se kouká, stojí
     uprostřed horní lišty. Seznam je zrcadlo #tabbar — klepnutí posílá dál
     na původní tlačítko tabu, takže o přepínání i zavírání dál rozhoduje
     hub.js a o tomhle nic neví. */
  const tabbar = $('tabbar');
  const openBox = $('open-tabs');
  const openSection = $('open-section');
  const tabTitle = $('topbar-tab');

  let renaming = false;   // pole na přejmenování by překreslení smazalo

  function syncTabs() {
    if (!tabbar || !openBox || renaming) return;
    // Taby jiného prostoru (firma × osobní, hub.js applyProstor) jsou schované.
    const tabs = [...tabbar.querySelectorAll('.tab')].filter((t) => !t.hidden);
    const active = tabs.find((t) => t.classList.contains('active'));
    const name = (t) => (t.querySelector('.tab-title') || t).textContent.trim();
    if (tabTitle) tabTitle.textContent = active ? name(active) : '';
    document.body.classList.toggle('has-tab', !!active);
    openSection.hidden = !tabs.length;
    openBox.textContent = '';
    for (const t of tabs) {
      const row = document.createElement('div');
      row.className = 'open-tab' + (t === active ? ' on' : '') +
                      (t.classList.contains('exited') ? ' exited' : '') +
                      (t.classList.contains('ceka') ? ' ceka' : '') +
                      (t.classList.contains('hotovo') ? ' hotovo' : '');
      const dot = t.querySelector('.tab-agent');
      const d = document.createElement('span');
      d.className = 'open-dot';
      if (dot && dot.style.getPropertyValue('--agent-color')) {
        d.style.background = dot.style.getPropertyValue('--agent-color');
      }
      if (t.classList.contains('firma')) d.style.background = 'var(--firma)';
      const go = document.createElement('button');
      go.className = 'open-name';
      go.textContent = name(t);
      // Sdílený chat: štítek „Sdílené · Jiří“ nad názvem.
      if (t.dataset.stitek) {
        row.classList.add('sdileny');
        const st = document.createElement('span');
        st.className = 'open-stitek';
        st.textContent = t.dataset.stitek;
        go.prepend(st);
      }
      // Jedno ťuknutí přepne tab, dvě rychle po sobě ho přejmenují jako
      // dvojklik na počítači. Přepnutí proto chvíli počká, jestli nepřijde druhé.
      let wait = 0;
      go.onclick = () => {
        if (wait) { clearTimeout(wait); wait = 0; renameIn(go, t, row); return; }
        wait = setTimeout(() => { wait = 0; t.click(); closeDrawer(); }, DOUBLE_TAP_MS);
      };
      const x = document.createElement('button');
      x.className = 'open-x';
      x.title = 'Zavřít chat';
      x.innerHTML = '<svg class="ico"><use href="#i-close"/></svg>';
      x.onclick = () => { const c = t.querySelector('.tab-close'); if (c) c.click(); };
      // Podržení (níž) pošle contextmenu — u tabu v šuplíku taky přejmenuje.
      row.oncontextmenu = (ev) => { ev.preventDefault(); renameIn(go, t, row); };
      row.append(d, go, x);
      openBox.appendChild(row);
    }
  }
  /* Lišta s taby, kde se přejmenovává dvojklikem, je na telefonu schovaná.
     Pole se proto otevře na místě jména (v šuplíku nebo nahoře v liště)
     a nový název dostane hub.js událostí `hub-rename` na tlačítku tabu. */
  function renameIn(el, t, row) {
    if (renaming || !t) return;
    renaming = true;
    const input = document.createElement('input');
    input.className = 'open-input';
    input.value = (t.querySelector('.tab-title') || t).textContent.trim();
    input.maxLength = 60;
    input.enterKeyHint = 'done';
    if (row) row.classList.add('editing');
    // Pole v tlačítku ne: mezera by ho „stiskla" (viz startRename v hub.js).
    const inButton = el.tagName === 'BUTTON';
    const old = el.textContent;
    if (inButton) el.replaceWith(input);
    else { el.textContent = ''; el.appendChild(input); }
    input.focus();
    input.select();
    let done = false;
    const finish = (save) => {
      if (done) return;
      done = true;
      renaming = false;
      if (row) row.classList.remove('editing');
      if (inButton) input.replaceWith(el);
      else el.textContent = old;
      if (save) t.dispatchEvent(new CustomEvent('hub-rename', {detail: input.value}));
      syncTabs();
    };
    input.onblur = () => finish(true);
    input.onkeydown = (ev) => {
      if (ev.key === 'Enter') { ev.preventDefault(); finish(true); }
      if (ev.key === 'Escape') { ev.preventDefault(); finish(false); }
      ev.stopPropagation();
    };
    // Ťuknutí do pole nesmí projít na tlačítko pod ním (přepnutí tabu).
    input.onclick = (ev) => ev.stopPropagation();
  }

  // Jméno tabu nahoře uprostřed: dvojí ťuknutí = přejmenovat.
  if (tabTitle) {
    let last = 0;
    tabTitle.onclick = () => {
      if (renaming) return;
      const now = Date.now();
      if (now - last < DOUBLE_TAP_MS) {
        last = 0;
        renameIn(tabTitle, tabbar && tabbar.querySelector('.tab.active'));
      } else last = now;
    };
  }

  if (tabbar) {
    let pending = 0;
    new MutationObserver(() => {
      cancelAnimationFrame(pending);
      pending = requestAnimationFrame(syncTabs);
    }).observe(tabbar, {childList: true, subtree: true, characterData: true,
                        attributes: true, attributeFilter: ['class', 'style', 'hidden']});
    syncTabs();
  }

  /* Nový tab: stejná nabídka jako „+" v liště tabů (hub.js), jen zakotvená
     u tlačítka, na které se kleplo. */
  function newTab(anchor) {
    const plus = $('btn-new-menu');
    if (plus && plus.onclick) plus.onclick({currentTarget: anchor});
  }
  const newTop = $('btn-new-top');
  if (newTop) newTop.onclick = () => newTab(newTop);
  const drawerNew = $('btn-drawer-new');
  // Na počítači je „Nový chat" v panelu totéž co „+ Nový chat" v liště tabů.
  if (drawerNew) drawerNew.onclick = () => {
    closeDrawer();
    if (matchMedia(NARROW).matches) { if (newTop) newTab(newTop); } else $('btn-new-agent').click();
  };
  const drawerFirma = $('btn-drawer-firma');
  if (drawerFirma) drawerFirma.onclick = () => { closeDrawer(); if (window.hubFirmaChat) window.hubFirmaChat(); };
  const drawerHome = $('btn-drawer-home');
  if (drawerHome) drawerHome.onclick = () => { if (window.hubHome) window.hubHome(); else closeDrawer(); };
  const drawerX = $('btn-drawer-x');
  if (drawerX) drawerX.onclick = closeDrawer;

  matchMedia(NARROW).addEventListener('change', applyClasses);
  applyClasses();

  /* ── dlouhý stisk místo pravého tlačítka ──────────────────────────────── */
  /* Chrome na Androidu pošle při podržení `contextmenu` sám, Safari na iOS ne
     — tam by se k přejmenování, archivaci ani deploy projektu nikdo nedostal.
     Podržení proto vyrobí tutéž událost ručně; prohlížeč, který ji pošle sám,
     se pozná podle toho, že přijde dřív, a druhá se už nekoná. */
  if (touch) {
    let timer = null, start = null, fired = false;

    const cancel = () => {
      clearTimeout(timer);
      timer = null;
      start = null;
    };

    document.addEventListener('touchstart', (ev) => {
      if (ev.touches.length !== 1) return cancel();
      const t = ev.touches[0];
      const target = t.target.closest('.card, .tab, .open-tab, .barbtn, .chat-item');
      if (!target) return;
      start = {x: t.clientX, y: t.clientY};
      fired = false;
      timer = setTimeout(() => {
        fired = true;
        target.dispatchEvent(new MouseEvent('contextmenu', {
          bubbles: true, cancelable: true,
          clientX: start.x, clientY: start.y,
        }));
        if (navigator.vibrate) navigator.vibrate(12);
      }, LONG_PRESS_MS);
    }, {passive: true});

    document.addEventListener('touchmove', (ev) => {
      if (!start || !timer) return;
      const t = ev.touches[0];
      if (Math.abs(t.clientX - start.x) > MOVE_TOLERANCE ||
          Math.abs(t.clientY - start.y) > MOVE_TOLERANCE) cancel();
    }, {passive: true});

    document.addEventListener('touchend', (ev) => {
      // Po vyvolané nabídce nesmí projít i klepnutí, jinak se rovnou otevře tab.
      if (fired) { ev.preventDefault(); fired = false; }
      cancel();
    });
    document.addEventListener('touchcancel', cancel, {passive: true});
  }

  /* ── měkká klávesnice ─────────────────────────────────────────────────── */
  /* Když vyjede klávesnice, okno se na Androidu zmenší (interactive-widget
     v <meta viewport>), kdežto na iOS se jen posune vizuální výřez a `resize`
     nepřijde. Terminál by pak počítal se starou výškou. hub.js na `resize`
     přepočítává sám, takže stačí ho poslat. */
  // Jen na dotykovém zařízení. Na počítači měkká klávesnice není, zato tam
  // visualViewport umí přijít i jindy — a každé takové „přepočítej" jde přes
  // refit až do pty, kde ConPTY na Windows překreslí celou obrazovku.
  if (touch && window.visualViewport) {
    /* Výška appky = to, co je opravdu vidět (--app-h, hub.css). Firefox na
       Androidu i Safari na iPhonu při klávesnici stránku nezmenší, jen ji
       posunou, aby bylo vidět pole — a po zavření klávesnice posun nechají:
       pod polem na psaní pak zůstal prázdný pruh. Takhle se appka vejde
       nad klávesnici, a jakmile zajede, vrátí se celá na místo. */
    const root = document.documentElement;
    const vv = window.visualViewport;
    /* Z ikony na ploše (is-app) iOS do visualViewport nezapočítá stavový
       řádek nahoře (black-translucent), takže appka vyšla o jeho výšku nižší
       a pod polem na psaní zůstal pruh. Tam se výška podle výřezu bere jen
       při vysunuté klávesnici; jinak platí celá obrazovka (hub.css, 100vh).
       Klávesnice = výřez o kus nižší než nejvyšší, jaký byl v téhle šířce. */
    const isApp = document.body.classList.contains('is-app');
    const tallest = {};
    let pending = 0, last = 0;
    const sync = () => {
      const h = Math.round(vv.height);
      const w = window.innerWidth;
      tallest[w] = Math.max(tallest[w] || 0, h);
      const kbd = h < tallest[w] - 120;
      if (isApp && !kbd) {
        if (last) { root.style.removeProperty('--app-h'); last = 0; }
      } else if (h !== last) {
        root.style.setProperty('--app-h', h + 'px');
        last = h;
      }
      // Stránka se sama nikdy neposouvá (body má overflow: hidden) — každý
      // posun je od prohlížeče kvůli klávesnici a má zmizet.
      if (window.scrollY || document.scrollingElement.scrollTop) {
        window.scrollTo(0, 0);
        document.scrollingElement.scrollTop = 0;
      }
    };
    const nudge = () => {
      sync();
      clearTimeout(pending);
      pending = setTimeout(() => { sync(); window.dispatchEvent(new Event('resize')); }, 120);
    };
    vv.addEventListener('resize', nudge);
    vv.addEventListener('scroll', nudge);
    // Po zavření klávesnice (fokus pryč z pole) ještě jednou, až dojede animace.
    document.addEventListener('focusout', () => setTimeout(nudge, 350));
    sync();
  }

  /* ── service worker ───────────────────────────────────────────────────── */
  /* ── instalace na plochu ─────────────────────────────────────────────── */
  /* Android (Chrome, Edge, Samsung) pošle `beforeinstallprompt` — ten se
     schová a vyvolá z nabídky v liště. Safari na iPhonu instalaci skriptem
     nedovolí: tam nabídka ukáže, kde to v Safari najít. Když hub už běží
     z ikony na ploše, nenabízí se nic. */
  let installEv = null;
  window.addEventListener('beforeinstallprompt', (ev) => {
    ev.preventDefault();
    installEv = ev;
  });
  window.addEventListener('appinstalled', () => { installEv = null; });
  const standalone = () => matchMedia('(display-mode: standalone)').matches ||
                           window.navigator.standalone === true;
  const ios = /iPad|iPhone|iPod/.test(navigator.userAgent) ||
              (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
  window.HubInstall = {
    // Co nabídnout: 'prompt' (Android), 'ios' (návod), nebo '' (nic).
    kind() {
      if (standalone()) return '';
      if (installEv) return 'prompt';
      return ios ? 'ios' : '';
    },
    async run(notice) {
      if (installEv) {
        const ev = installEv;
        installEv = null;
        ev.prompt();
        try { await ev.userChoice; } catch (_) { /* zavřeno */ }
        return;
      }
      if (ios && notice) {
        notice('V Safari klepni na Sdílet (čtvereček se šipkou) → Přidat na plochu.');
      }
    },
  };

  // Registruje se jen v bezpečném kontextu; přes http na tailnet adrese ho
  // prohlížeč stejně odmítne a hub funguje i bez něj, jen se nedá nainstalovat.
  if ('serviceWorker' in navigator && window.isSecureContext) {
    window.addEventListener('load', () => {
      navigator.serviceWorker.register('/sw.js').catch(() => {
        /* bez něj se jen nenabídne instalace na plochu */
      });
    });
  }

})();
