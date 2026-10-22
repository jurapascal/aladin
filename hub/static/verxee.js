/* Tlačítko Nahlásit chybu / Podat návrh (Verxee Debug, https://alba-rosa.cz/debug/).
 *
 * Červená půlka chyba, modrá návrh; hlášení jdou do portálu, odkud je Claude
 * čte příkazem `chyby`. Skript se načte jen s klíčem `vxd_…` v hub-config
 * (Nastavení → Ostatní, na serveru ho nastavuje správce brány) — bez klíče
 * se z alba-rosa.cz nic nestahuje. Klíč je veřejný; chrání ho seznam
 * povolených domén v portálu (Klíče a domény).
 *
 *  - `apply(state)` po každém načtení /api/state (hub.js, reload): vloží
 *                   skript, změní klíč nebo jméno, s prázdným klíčem
 *                   tlačítko schová.
 */
'use strict';

(function (global) {

  const SKRIPT = 'https://alba-rosa.cz/debug/debug.js';
  let nacteno = false;

  // Kdo hlásí: host v cizím prostoru, člověk v prostoru na serveru, nebo na
  // počítači účet claude.ai (Nastavení → Účet), e-mail účtu na serveru
  // a jméno uživatele systému. Tlačítko se ukazuje, jen dokud je jméno neprázdné.
  let ucet = null;              // účet claude.ai na počítači; načte se jednou
  function kdo(state) {
    const cfg = state.config || {};
    const g = state.guest || cfg.gateway_user;
    if (g) return {jmeno: g.name || g.email || 'Aladin', email: g.email || ''};
    const a = ucet || {};
    return {jmeno: a.name || state.user || cfg.gw_email || a.email || 'Aladin',
            email: cfg.gw_email || a.email || ''};
  }

  // Na počítači hub jinak zná jen jméno uživatele systému (a e-mail účtu na
  // serveru, když je s ním spojený). debug.js jméno i e-mail čte až při
  // odeslání, stačí je doplnit, až účet přijde.
  let zapnuto = false;
  function nactiUcet(state) {
    const cfg = state.config || {};
    if (ucet || state.guest || cfg.gateway_user || typeof global.api !== 'function') return;
    ucet = {};
    global.api('claude-ucet').then((r) => {
      ucet = (r && r.account) || {};
      if (zapnuto) Object.assign(global.VerxeeDebug, kdo(state));
    }, () => {});
  }

  // debug.js kreslí plovoucí tlačítko do rohu a to se v hubu kryje s ostatními
  // plovoucími prvky (Prohlížeč, panely). Schováme ho (shadow root je otevřený)
  // a formulář otevírá ikonka v horní liště; Alt+Shift+B / N fungují dál.
  const STYL = '.fab{display:none!important}';

  function uprav() {
    const host = document.getElementById('verxee-chyby');
    const root = host && host.shadowRoot;
    if (!root || root.querySelector('style[data-hub]')) return !!root;
    const st = document.createElement('style');
    st.dataset.hub = '1';
    st.textContent = STYL;
    root.appendChild(st);
    return true;
  }

  // Dvojité tlačítko v horní liště jako plovoucí tlačítko na webech: červená
  // půlka Chyba (= Alt+Shift+B), modrá Návrh (= Alt+Shift+N).
  function lista(ukazat) {
    const b = document.getElementById('btn-verxee');
    if (!b) return;
    b.hidden = !ukazat;
    if (b.dataset.v) return;
    b.dataset.v = '1';
    for (const btn of b.querySelectorAll('button[data-klavesa]')) {
      const k = btn.dataset.klavesa;
      btn.addEventListener('click', () => document.dispatchEvent(new KeyboardEvent('keydown',
        {key: k, code: 'Key' + k, altKey: true, shiftKey: true, bubbles: true})));
    }
  }

  function apply(state) {
    if (!state) return;
    const klic = String((state.config || {}).verxee_debug_klic || '').trim();
    const d = global.VerxeeDebug = global.VerxeeDebug || {};
    const host = document.getElementById('verxee-chyby');
    zapnuto = !!klic;
    if (!klic) {
      // debug.js se ptá na jméno při každém kreslení; host schováme hned.
      d.jmeno = '';
      lista(false);
      if (host) host.style.display = 'none';
      return;
    }
    Object.assign(d, {klic, web: 'claude-code-hub'}, kdo(state));
    nactiUcet(state);
    lista(true);
    if (host) host.style.display = '';
    if (nacteno) return;
    nacteno = true;
    const s = document.createElement('script');
    s.src = SKRIPT;
    s.async = true;
    s.onload = () => {
      // Tlačítko vzniká při spuštění skriptu, někdy až po DOMContentLoaded.
      let n = 0;
      const t = setInterval(() => { if (uprav() || ++n > 40) clearInterval(t); }, 250);
    };
    s.onerror = () => { nacteno = false; console.warn('Verxee Debug: skript se nenačetl'); };
    document.head.appendChild(s);
  }

  global.HubVerxee = {apply};
})(window);
