/* Zvonek s oznámeními v liště — jen v prostoru na serveru (gateway/oznameni.py).
 *
 * Šestivteřinová hláška dole na „dochází ti místo" nestačí: zmizí dřív, než ji
 * člověk přečte. Oznámení tu zůstane, dokud ho člověk nezavře, a admin v Nastavení
 * → Oznámení vidí, kdy ho přečetl, kdy na něj klikl a kdy ho zavřel.
 *
 * Tlačítko akce („Vyřešit s Claudem") otevře NOVÝ chat s Claudem v osobním
 * prostoru a pošle mu zadání z oznámení jako první zprávu — člověk nemusí nic
 * psát ani kopírovat.
 *
 *  - `apply(io)` po každém načtení /api/state (hub.js, reload): na serveru
 *                zvonek ukáže a začne se ptát brány; v cizím prostoru (host)
 *                ho schová — akce by otevřela chat u vlastníka prostoru.
 */
'use strict';

(function (global) {

  const OBNOVA = 2 * 60 * 1000;
  let io = null;
  let data = {oznameni: [], starsi: [], neprectene: 0};
  let zapnuto = false;
  let casovac = null;
  let panel = null;
  let videno = 0;                 // nejvyšší id, o kterém už hláška dole řekla
  let starsiOtevrene = false;

  function el(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }

  async function gw(payload) {
    const r = await fetch('/gw/oznameni', payload === undefined ? {credentials: 'same-origin'} : {
      method: 'POST', credentials: 'same-origin',
      headers: {'Content-Type': 'application/json', 'X-Hub-Account': '1'},
      body: JSON.stringify(payload),
    });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) {
      const e = new Error(d.error || 'Server teď neodpovídá, zkus to za chvíli.');
      e.status = r.status;
      throw e;
    }
    return d;
  }

  /* „před 5 min", „včera", „12. 10." */
  function kdy(ts) {
    const s = Math.max(0, Date.now() / 1000 - ts);
    if (s < 90) return 'právě teď';
    if (s < 3600) return 'před ' + Math.round(s / 60) + ' min';
    if (s < 86400) return 'před ' + Math.round(s / 3600) + ' h';
    const d = new Date(ts * 1000);
    const vcera = new Date(); vcera.setDate(vcera.getDate() - 1);
    if (d.toDateString() === vcera.toDateString()) return 'včera';
    if (s < 6 * 86400) return 'před ' + Math.floor(s / 86400) + ' dny';
    return d.getDate() + '. ' + (d.getMonth() + 1) + '.';
  }

  function apply(hubio) {
    io = hubio;
    const btn = document.getElementById('btn-zvonek');
    if (!btn) return;
    const st = io.state || {};
    const on = !!(st.config && st.config.gateway_user) && !st.guest;
    btn.hidden = !on;
    if (!on) { zavritPanel(); return; }
    if (zapnuto) return;
    zapnuto = true;
    btn.onclick = (ev) => { ev.stopPropagation(); if (panel) zavritPanel(); else otevritPanel(); };
    nacti(true);
    casovac = setInterval(() => { if (!document.hidden) nacti(); }, OBNOVA);
    document.addEventListener('visibilitychange', () => {
      if (document.visibilityState === 'visible') nacti();
    });
  }

  async function nacti(prvni) {
    try { data = await gw(); } catch (err) {
      // Starší brána oznámení nezná (404): zvonek se schová a po čase se
      // zkusí znovu — až se brána aktualizuje, objeví se sám.
      if (err.status === 404) {
        const btn = document.getElementById('btn-zvonek');
        if (btn) btn.hidden = true;
        zavritPanel();
        zapnuto = false;
        clearInterval(casovac);
        setTimeout(() => { if (io) apply(io); }, 10 * 60 * 1000);
      }
      return;
    }
    const btn0 = document.getElementById('btn-zvonek');
    if (btn0 && btn0.hidden && io && io.state && io.state.config && io.state.config.gateway_user) btn0.hidden = false;
    const nove = data.oznameni.filter((o) => !o.read_at && o.id > videno);
    // Nové nepřečtené řekne i hláška dole — zvonek v rohu snadno přehlédneš.
    // Při načtení stránky jen jednou souhrnně, ať se neopakuje po každém F5.
    if (nove.length && io && io.toast && (!prvni || nove.some((o) => o.severity !== 'info'))) {
      io.toast(nove.length === 1 ? 'Nové oznámení: ' + nove[0].title : `Máš ${nove.length} nová oznámení — zvonek nahoře.`);
    }
    videno = Math.max(videno, ...data.oznameni.map((o) => o.id), 0);
    odznak();
    if (panel) kresli();
  }

  function odznak() {
    const btn = document.getElementById('btn-zvonek');
    if (!btn) return;
    const n = data.neprectene || 0;
    let b = btn.querySelector('.zvonek-pocet');
    if (!b) { b = el('span', 'zvonek-pocet'); btn.appendChild(b); }
    b.textContent = n > 9 ? '9+' : String(n);
    b.hidden = !n;
    btn.classList.toggle('ma-nove', !!n);
    btn.title = n ? `Oznámení (${n} nepřečtených)` : 'Oznámení';
    btn.setAttribute('aria-label', btn.title);
  }

  function otevritPanel() {
    panel = el('div', 'ozn-panel');
    panel.setAttribute('role', 'dialog');
    panel.setAttribute('aria-label', 'Oznámení');
    document.body.appendChild(panel);
    kresli();
    setTimeout(() => {
      document.addEventListener('pointerdown', venku, true);
      document.addEventListener('keydown', esc, true);
    }, 0);
    // Otevřený zvonek = přečteno (admin to uvidí). Zavřít je dál na člověku.
    const ids = data.oznameni.filter((o) => !o.read_at).map((o) => o.id);
    if (ids.length) {
      gw({akce: 'precteno', ids}).then(() => {
        const ted = Date.now() / 1000;
        for (const o of data.oznameni) if (ids.includes(o.id)) o.read_at = ted;
        data.neprectene = 0;
        odznak();
      }).catch(() => {});
    }
  }

  function zavritPanel() {
    if (!panel) return;
    panel.remove();
    panel = null;
    document.removeEventListener('pointerdown', venku, true);
    document.removeEventListener('keydown', esc, true);
  }
  function venku(ev) {
    const btn = document.getElementById('btn-zvonek');
    if (panel && !panel.contains(ev.target) && !(btn && btn.contains(ev.target))) zavritPanel();
  }
  function esc(ev) { if (ev.key === 'Escape') { ev.stopPropagation(); zavritPanel(); } }

  function kresli() {
    if (!panel) return;
    panel.textContent = '';
    const hlava = el('div', 'ozn-hlava');
    hlava.appendChild(el('strong', null, 'Oznámení'));
    const x = el('button', 'iconbtn ozn-x', '×');
    x.title = 'Zavřít panel (Esc)';
    x.setAttribute('aria-label', 'Zavřít panel');
    x.onclick = zavritPanel;
    hlava.appendChild(x);
    panel.appendChild(hlava);
    const seznam = el('div', 'ozn-seznam');
    panel.appendChild(seznam);
    if (!data.oznameni.length) {
      seznam.appendChild(el('div', 'ozn-prazdno', 'Žádná nová oznámení. Až se na serveru něco bude týkat tebe, objeví se tady.'));
    }
    for (const o of data.oznameni) seznam.appendChild(polozka(o));
    if (data.starsi && data.starsi.length) {
      const prepni = el('button', 'ozn-starsi-btn', (starsiOtevrene ? '▾ ' : '▸ ') + 'Starší (' + data.starsi.length + ')');
      prepni.onclick = () => { starsiOtevrene = !starsiOtevrene; kresli(); };
      panel.appendChild(prepni);
      if (starsiOtevrene) {
        const st = el('div', 'ozn-seznam ozn-starsi');
        for (const o of data.starsi) st.appendChild(polozka(o, true));
        panel.appendChild(st);
      }
    }
  }

  function polozka(o, stara) {
    const box = el('div', 'ozn-polozka ' + (o.severity || 'info') + (o.read_at ? '' : ' neprectene') + (stara ? ' stara' : ''));
    const hlava = el('div', 'ozn-titulek');
    hlava.appendChild(el('span', 'ozn-tecka'));
    hlava.appendChild(el('span', null, o.title));
    box.appendChild(hlava);
    box.appendChild(el('div', 'ozn-meta', (o.sender || 'Systém') + ' · ' + kdy(o.created)));
    if (o.text) box.appendChild(el('div', 'ozn-text', o.text));
    if (stara) {
      box.appendChild(el('div', 'ozn-meta', o.replaced_by ? 'Nahrazeno novějším' : o.acted_at ? 'Řešil(a) jsi s Claudem' : 'Zavřeno'));
      return box;
    }
    const akce = el('div', 'ozn-akce');
    if (o.action && o.action.prompt) {
      const b = el('button', 'btn primary', o.action.label || 'Vyřešit s Claudem');
      b.onclick = () => vyresit(o);
      akce.appendChild(b);
      if (o.acted_at) akce.appendChild(el('span', 'ozn-stitek', 'chat s Claudem už máš otevřený'));
    }
    const z = el('button', 'btn ghost', 'Zavřít');
    z.onclick = async () => {
      try { await gw({akce: 'zavrit', ids: [o.id]}); } catch (e) { if (io) io.toast(e.message); return; }
      await nacti();
    };
    akce.appendChild(z);
    box.appendChild(akce);
    return box;
  }

  /* Nový chat s Claudem v osobním prostoru, zadání z oznámení jako první
     zpráva (openTab → `prompt`, hub ho předá Claude Code při startu). */
  function vyresit(o) {
    if (!io) return;
    const st = io.state || {};
    const agenti = st.agents || [];
    const claude = agenti.find((a) => a.id === 'claude' && a.path);
    const nazev = o.kind === 'uloziste' ? 'Úklid místa' : String(o.title || 'Oznámení').slice(0, 32);
    io.openTab({kind: 'project', path: st.home, title: nazev,
                agent: claude ? 'claude' : (st.default_agent || ''), prompt: o.action.prompt});
    gw({akce: 'vyreseno', ids: [o.id]}).then(() => nacti()).catch(() => {});
    zavritPanel();
  }

  global.HubOznameni = {apply, obnovit: () => nacti()};
})(window);
