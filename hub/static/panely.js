/* Šířka panelů vlevo (chaty) a vpravo (Průběh, rychlé akce) — jen na počítači.
 *
 * Za okraj panelu jde tahat (dvojklik vrátí výchozí šířku) a tlačítkem v liště
 * jde panel schovat a zase ukázat. Šířky drží proměnné, ze kterých kreslí
 * zbytek stránky: --sidebar-w (na <html>, motiv Claude podle ní kreslí i lištu)
 * a --bar-w (na <body>, kde ji Průběh rozšiřuje přes body.ma-prehled). Pamatuje
 * si to každé zařízení zvlášť (localStorage). Na telefonu je panel šuplík
 * a nic z toho se nezapíná.
 */
'use strict';

(function () {
  const NARROW = '(max-width: 820px)';
  const $ = (id) => document.getElementById(id);
  const MEZE = {levy: [200, 560], pravy: [180, 640]};
  const KLIC = {levy: 'hub-panel-levy-w', pravy: 'hub-panel-pravy-w'};
  const SKRYTY = {levy: 'hub-panel-levy-skryty', pravy: 'hub-panel-pravy-skryty'};
  const TRIDA = {levy: 'bez-leveho', pravy: 'bez-praveho'};

  const cti = (k) => { try { return localStorage.getItem(k); } catch (_) { return null; } };
  const pis = (k, v) => {
    try { if (v == null) localStorage.removeItem(k); else localStorage.setItem(k, String(v)); } catch (_) { /* soukromé okno */ }
  };

  function cil(strana) {
    return strana === 'levy' ? document.documentElement : document.body;
  }
  function promenna(strana) {
    return strana === 'levy' ? '--sidebar-w' : '--bar-w';
  }
  function sirka(strana, px) {
    const [min, max] = MEZE[strana];
    const w = Math.round(Math.max(min, Math.min(max, px, window.innerWidth * 0.45)));
    cil(strana).style.setProperty(promenna(strana), w + 'px');
    return w;
  }
  function vychozi(strana) {
    cil(strana).style.removeProperty(promenna(strana));
    pis(KLIC[strana], null);
    poZmene();
  }

  // Terminál v tabu se po změně šířky přepočítá sám (ResizeObserver na
  // #panes); lišta a čtení dostanou resize, ať se přeskládají hned.
  let resizeTimer = null;
  function poZmene() {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => window.dispatchEvent(new Event('resize')), 60);
  }

  function skryt(strana, ano) {
    document.body.classList.toggle(TRIDA[strana], ano);
    pis(SKRYTY[strana], ano ? '1' : null);
    syncTlacitka();
    poZmene();
  }

  /* Tahání za okraj: pointer capture drží tah, i když myš vyjede nad terminál. */
  function tahlo(strana) {
    const el = document.createElement('div');
    el.className = 'tahlo tahlo-' + strana;
    el.setAttribute('role', 'separator');
    el.setAttribute('aria-orientation', 'vertical');
    el.title = 'Táhni pro změnu šířky · dvojklik vrátí výchozí';
    el.addEventListener('pointerdown', (ev) => {
      if (ev.button !== 0) return;
      ev.preventDefault();
      el.setPointerCapture(ev.pointerId);
      document.body.classList.add('tahne');
      el.classList.add('aktivni');
      const layout = document.querySelector('.layout').getBoundingClientRect();
      let w = null;
      const pohyb = (e) => {
        w = sirka(strana, strana === 'levy' ? e.clientX - layout.left : layout.right - e.clientX);
      };
      const konec = () => {
        el.removeEventListener('pointermove', pohyb);
        el.removeEventListener('pointerup', konec);
        el.removeEventListener('pointercancel', konec);
        document.body.classList.remove('tahne');
        el.classList.remove('aktivni');
        if (w != null) pis(KLIC[strana], w);
        poZmene();
      };
      el.addEventListener('pointermove', pohyb);
      el.addEventListener('pointerup', konec);
      el.addEventListener('pointercancel', konec);
    });
    el.addEventListener('dblclick', () => vychozi(strana));
    document.querySelector('.layout').appendChild(el);
  }

  /* Tlačítko pravého panelu jen tehdy, když panel má co ukázat (hub.js,
     showActionbar nastavuje #actionbar[hidden] podle obsahu). */
  function syncTlacitka() {
    const bar = $('actionbar');
    const prave = $('btn-panel-pravy');
    if (prave && bar) {
      prave.hidden = bar.hidden;
      const skryty = document.body.classList.contains(TRIDA.pravy);
      prave.title = skryty ? 'Ukázat panel vpravo' : 'Schovat panel vpravo';
      prave.classList.toggle('on', !skryty);
    }
    const leve = $('btn-panel-levy');
    if (leve) {
      const skryty = document.body.classList.contains(TRIDA.levy);
      leve.title = skryty ? 'Ukázat panel s chaty' : 'Schovat panel s chaty';
    }
  }

  function start() {
    if (!document.querySelector('.layout')) return;
    for (const strana of ['levy', 'pravy']) {
      const w = parseInt(cti(KLIC[strana]) || '', 10);
      if (w) sirka(strana, w);
      if (cti(SKRYTY[strana]) === '1') document.body.classList.add(TRIDA[strana]);
      tahlo(strana);
    }
    const leve = $('btn-panel-levy');
    if (leve) leve.onclick = () => skryt('levy', !document.body.classList.contains(TRIDA.levy));
    const prave = $('btn-panel-pravy');
    if (prave) prave.onclick = () => skryt('pravy', !document.body.classList.contains(TRIDA.pravy));
    const bar = $('actionbar');
    if (bar) new MutationObserver(syncTlacitka).observe(bar, {attributes: true, attributeFilter: ['hidden']});
    syncTlacitka();
    // Užší okno: uložená šířka nesmí panelem přerůst polovinu okna.
    window.addEventListener('resize', () => {
      if (window.matchMedia(NARROW).matches) return;
      for (const strana of ['levy', 'pravy']) {
        const w = parseInt(cti(KLIC[strana]) || '', 10);
        if (w) sirka(strana, w);
      }
    });
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start);
  else start();
  window.HubPanely = {skryt, vychozi};
})();
