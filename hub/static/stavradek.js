/* Stavový řádek tabu — model, větev, odhad ceny a čas (hub/stavradek.py).
 *
 * Tenký proužek nad konverzací. Je to jeden pevný řádek se čtyřmi prvky, které
 * se jen přepisují (textContent) — nikdy se nestaví znovu, takže nemůže zrušit
 * klepnutí ani poskočit. Ptá se serveru jen při změně: když ve čtení přibyl
 * obsah, při přepnutí na tab a jednou při startu; nejvýš jednou za MIN_MEZERA.
 * Čas běhu se dopočítává v prohlížeči (bez dotazu), jednou za půl minuty.
 *
 * Zapnutí je v Nastavení → Vzhled (jen na tomhle zařízení, výchozí zapnuto).
 */
'use strict';

(function (global) {

  const KLIC = 'hub-stavradek';
  const MIN_MEZERA = 5000;        // ms — nejčastější dotaz na server
  const CAS_KAZDYCH = 30000;      // ms — jak často se přepíše čas běhu
  const DOPOCET_KAZDYCH = 1500;   // ms — když server ještě dopočítává velký přepis
  const DOPOCET_MAX = 40;

  function zapnuto() {
    try { return localStorage.getItem(KLIC) !== '0'; } catch (_) { return true; }
  }
  function nastav(on) {
    try { localStorage.setItem(KLIC, on ? '1' : '0'); } catch (_) { /* soukromé okno */ }
    document.dispatchEvent(new CustomEvent('hub-stavradek'));
  }

  const num = new Intl.NumberFormat('cs-CZ', {maximumFractionDigits: 1});
  function tokeny(n) {
    if (!(n > 0)) return '';
    if (n >= 1e6) return num.format(n / 1e6) + ' mil.';
    if (n >= 1e3) return Math.round(n / 1e3) + ' tis.';
    return String(Math.round(n));
  }
  function cena(c) {
    if (typeof c !== 'number' || !(c > 0)) return '';
    if (c < 0.01) return '< $0,01';
    return '≈ $' + (c < 10 ? c.toFixed(2) : Math.round(c)).toString().replace('.', ',');
  }
  function cas(ms) {
    if (!(ms > 0)) return '';
    const min = Math.floor(ms / 60000);
    if (min < 1) return '< 1 min';
    if (min < 60) return min + ' min';
    const h = Math.floor(min / 60), z = min % 60;
    return h + ' h' + (z ? ' ' + z + ' min' : '');
  }

  function install(root, tab, io) {
    const rad = document.createElement('div');
    rad.className = 'stavradek';
    rad.hidden = true;
    // Čtyři pevné prvky; schovávají se atributem hidden, nevznikají a nezanikají.
    const prvek = (trida) => {
      const e = document.createElement('span');
      e.className = 'sr-' + trida;
      e.hidden = true;
      rad.appendChild(e);
      return e;
    };
    const eModel = prvek('model'), eVetev = prvek('vetev'), eSpotreba = prvek('spotreba'), eCas = prvek('cas');
    root.insertBefore(rad, root.firstChild);

    let zivy = true, cekaDotaz = false, posledni = 0, timer = null, caser = null;
    let data = null, dopocet = 0, nutne = true;

    const nastavText = (e, text, title) => {
      const skryt = !text;
      if (e.hidden !== skryt) e.hidden = skryt;
      if (e.textContent !== text) e.textContent = text;
      const t = skryt ? '' : (title || '');
      if (e.title !== t) e.title = t;
    };

    function vykresli() {
      if (!data || !data.ok || !zapnuto()) {
        if (!rad.hidden) rad.hidden = true;
        return;
      }
      nastavText(eModel, data.label || data.model || '', data.model || '');
      let vetev = '', vTitle = '';
      if (data.git) {
        if (data.branch) vetev = (data.detached ? '@' : '⎇ ') + data.branch;
        else vetev = '⎇ ?';
        if (typeof data.dirty === 'number' && data.dirty > 0) vetev += ' ● ' + data.dirty;
        vTitle = (data.detached ? 'Odpojená HEAD, commit ' : 'Větev ') + (data.branch || '?') +
          (typeof data.dirty === 'number'
            ? (data.dirty ? ' · ' + data.dirty + ' nezacommitovaných souborů' : ' · vše zacommitováno')
            : ' · změny se nepodařilo zjistit');
      }
      nastavText(eVetev, vetev, vTitle);
      const tok = tokeny(data.tokens), c = cena(data.cost);
      let sp = c || (tok ? tok + ' tok.' : '');
      if (c && tok) sp = c + ' · ' + tok;
      const u = data.usage || [0, 0, 0, 0];
      const sTitle = 'Tokeny: vstup ' + u[0] + ', výstup ' + u[1] + ', zápis do cache ' + u[2] +
        ', čtení z cache ' + u[3] + (c ? '. Cena je odhad podle ceníku API — na předplatném se nic neplatí.'
          : '. Cenu nelze spočítat (model není v ceníku).') + (data.partial ? ' Přepis se ještě dopočítává.' : '');
      nastavText(eSpotreba, (data.partial ? '… ' : '') + sp, sTitle);
      tikCas();
      nahoru();
      const videt = !!(eModel.textContent || eVetev.textContent || eSpotreba.textContent || eCas.textContent);
      if (rad.hidden === videt) rad.hidden = !videt;
    }

    /* Totéž vedle názvu chatu v horní liště (motiv Claude ho ukazuje místo řádku nad konverzací). */
    function nahoru() {
      const h = document.getElementById('topbar-stav');
      if (!h || (io.aktivni && !io.aktivni())) return;
      const text = [eModel, eVetev, eSpotreba, eCas].map((e) => (e.hidden ? '' : e.textContent)).filter(Boolean).join('  ·  ');
      if (h.textContent !== text) h.textContent = text;
    }

    function tikCas() {
      if (!data || !data.started) { nastavText(eCas, ''); return; }
      // Hodiny serveru vs. stránky: posun se odečte, ať čas nelže o rozdíl mezi nimi.
      const ted = Date.now() + (data.now ? data.now - data.ref : 0);
      nastavText(eCas, cas(ted - data.started), 'Jak dlouho tab běží');
    }

    async function dotaz() {
      if (!zivy || cekaDotaz || !tab.id || !zapnuto()) return;
      cekaDotaz = true;
      posledni = Date.now();
      try {
        const odp = await io.api('stav-radek?id=' + encodeURIComponent(tab.id));
        if (!zivy) return;
        if (odp && odp.ok) {
          odp.ref = Date.now();
          data = odp;
          nutne = false;
          dopocet = odp.partial ? dopocet + 1 : 0;
          vykresli();
          if (odp.partial && dopocet < DOPOCET_MAX) naplanuj(DOPOCET_KAZDYCH);
        }
      } catch (_) { /* server chvilku nemohl — příště */ }
      cekaDotaz = false;
    }

    function naplanuj(ms) {
      if (!zivy) return;
      clearTimeout(timer);
      timer = setTimeout(() => { timer = null; dotaz(); }, Math.max(ms, 0));
    }

    // Změna ve čtení nebo přepnutí na tab: dotaz, ale nejdřív za MIN_MEZERA od minulého.
    function zmena() {
      if (!zivy || !zapnuto()) return;
      if (io.aktivni && !io.aktivni()) { nutne = true; return; }    // neviditelný tab se nedotazuje
      if (timer) return;
      naplanuj(Math.max(MIN_MEZERA - (Date.now() - posledni), 50));
    }

    caser = setInterval(() => {
      if (!zivy || !data || document.hidden) return;
      if (io.aktivni && !io.aktivni()) return;
      tikCas();
    }, CAS_KAZDYCH);

    const naNastaveni = () => { if (zapnuto()) { zmena(); } vykresli(); };
    document.addEventListener('hub-stavradek', naNastaveni);

    // První dotaz — tab bez id ještě nemá session, tak ho zkusí hned, jak dostane.
    (function start() {
      if (!zivy) return;
      if (!tab.id) { setTimeout(start, 500); return; }
      dotaz();
    })();

    return {
      zmena,
      // Přepnutí na tab: dohnat, co se mezitím mohlo změnit (a srovnat čas).
      wake() { if (!zivy) return; if (data) { tikCas(); vykresli(); } if (nutne || Date.now() - posledni > MIN_MEZERA) zmena(); },
      release() {
        zivy = false;
        clearTimeout(timer);
        clearInterval(caser);
        document.removeEventListener('hub-stavradek', naNastaveni);
        rad.remove();
        const h = document.getElementById('topbar-stav');
        if (h) h.textContent = '';
      },
    };
  }

  global.HubStav = {install, zapnuto, nastav, format: {tokeny, cena, cas}};
})(window);
