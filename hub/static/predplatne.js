/* Claude na vlastním předplatném — a na víc předplatných jednoho člověka
 * (hub/predplatne.py, gateway/predplatna.py, /gw/claude).
 *
 * Aby Claude v prostoru na serveru fungoval bez klíče API, propojí ho appka na
 * počítači s předplatným toho člověka: spustí `claude setup-token`, v prohlížeči
 * stačí kliknout na Authorize a token odejde rovnou bráně.
 *
 * Předplatných může mít člověk víc (Max v práci, Pro doma). Když jednomu dojde
 * týdenní limit, přepne jedním klikem na druhé a po obnovení zpátky. U každého
 * je vidět skutečný limit (5 h a týden) — počítá ho brána, token z ní neodejde.
 * Na počítači totéž pro Claude Code v tabech: běžné přihlášení (/login) zůstává
 * výchozí, další předplatná si appka pamatuje jen na tomhle počítači.
 *
 * Tenhle modul je všechno, co z toho je vidět:
 *
 *  - `ensure(io)`      před vstupem do prostoru (server.js, go): když Claude
 *                      v prostoru nemá na čem jet, rovnou spustí propojení.
 *  - `connect(io)`     karta s průběhem — čeká na prohlížeč, nabídne odkaz
 *                      a pole na kód, kdyby se prohlížeč neotevřel.
 *  - `panel(io)`       na počítači v Nastavení → Účet: předplatná v prostoru.
 *  - `lokalBlock(io)`  na počítači v Nastavení → Účet: předplatná pro Claude
 *                      Code na tomhle počítači.
 *  - `serverBlock(io)` v prostoru v Nastavení → Účet.
 *  - `notice(io)`      v prostoru po načtení: předplatné je připojené, ale
 *                      běžící prostor ho dostane až restartem.
 *  - `hlidej(io)`      po načtení: drží seznam pro rychlé přepnutí
 *                      (`menuItem` v ⋯ a v nabídce modelu) a navrhne přepnutí,
 *                      když aktivnímu předplatnému dochází týdenní limit.
 */
'use strict';

(function (global) {

  function el(tag, cls, text) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text != null) node.textContent = text;
    return node;
  }

  // Technický detail chyby (HTTP 502, Failed to fetch…) patří do konzole,
  // člověk dostane lidskou větu.
  // Věta od serveru (česky) projde; výpadek spojení nebo syrový výpis
  // (Failed to fetch, HTTP 502, Traceback…) se nahradí lidskou větou.
  function friendly(where, e) {
    console.warn('predplatne.js: ' + where, e);
    const msg = (e && e.message) || '';
    if (!msg || e instanceof TypeError || msg.length > 200 ||
        /^HTTP \d|fetch|NetworkError|Traceback|Error:|<html/i.test(msg)) {
      return 'Server teď neodpovídá, zkus to za chvíli.';
    }
    return msg;
  }

  function date(ts) {
    const d = new Date(ts * 1000);
    return d.getDate() + '. ' + (d.getMonth() + 1) + '. ' + d.getFullYear();
  }

  const MODES = {
    predplatne: 'Claude v prostoru jede na tvém předplatném Claude.',
    api: 'Claude v prostoru jede na firemním účtu.',
    ucet: 'Claude je v prostoru přihlášený tvým účtem.',
    zadne: 'Claude v prostoru ještě není přihlášený.',
  };

  const DNY = ['po', 'út', 'st', 'čt', 'pá', 'so', 'ne'];
  // Od kolika % týdenního limitu aktivního předplatného navrhnout přepnutí —
  // a pod kolika % má jiné předplatné „volno“.
  const NAVRH_OD = 90;
  const VOLNO_POD = 75;
  const HLIDAT_MS = 10 * 60 * 1000;
  const ZAMITNUTO_KEY = 'hub-predplatne-navrh-ne';

  /* ── limity ──────────────────────────────────────────────────────────────── */
  function procenta(okno) {
    return okno && typeof okno.pct === 'number' ? Math.round(okno.pct) : null;
  }

  function kdyObnovi(iso) {
    const t = Date.parse(iso || '');
    if (!t) return '';
    const min = Math.max(0, Math.round((t - Date.now()) / 60000));
    if (min < 60) return 'obnoví se za ' + min + ' min';
    const h = Math.floor(min / 60);
    if (h < 24) return 'obnoví se za ' + h + ' h ' + (min % 60) + ' min';
    const d = new Date(t);
    return 'obnoví se ' + DNY[(d.getDay() + 6) % 7] + ' ' + d.getDate() + '. ' + (d.getMonth() + 1) +
      '. v ' + d.getHours() + ':' + String(d.getMinutes()).padStart(2, '0');
  }

  /* „týden 82 %, 5 h 10 %“ */
  function limitKratce(l) {
    if (!l || !l.ok) return '';
    const out = [];
    const w = procenta(l.week);
    const s = procenta(l.session);
    // Pevné mezery: v úzké nabídce se nesmí zalomit „5 h“ ani „82 %“.
    if (w != null) out.push('týden ' + w + ' %');
    if (s != null) out.push('5 h ' + s + ' %');
    return out.join(', ');
  }

  function stupen(p) {
    return p == null ? '' : p >= 95 ? 'nad' : p >= 80 ? 'blizko' : '';
  }

  function meter(nazev, okno) {
    const p = procenta(okno);
    const m = el('div', 'pd-meter ' + stupen(p));
    m.appendChild(el('span', 'pd-meter-k', nazev));
    const track = el('span', 'pd-meter-t');
    const fill = el('span', 'pd-meter-f');
    fill.style.width = Math.max(2, Math.min(100, p)) + '%';
    track.appendChild(fill);
    m.appendChild(track);
    m.appendChild(el('span', 'pd-meter-p', p + ' %'));
    // Na řádku jen krátce („za 2 h“, „čt 22:41“), celé po najetí myší.
    m.appendChild(el('span', 'pd-meter-kdy', kdyKratce(okno.resets)));
    const kdy = kdyObnovi(okno.resets);
    if (kdy) m.title = nazev + ' ' + p + ' % — ' + kdy;
    return m;
  }

  function kdyKratce(iso) {
    const t = Date.parse(iso || '');
    if (!t) return '';
    const min = Math.max(0, Math.round((t - Date.now()) / 60000));
    if (min < 60) return 'za ' + min + ' min';
    if (min < 24 * 60) return 'za ' + Math.round(min / 60) + ' h';
    const d = new Date(t);
    if (min < 6 * 24 * 60) return DNY[(d.getDay() + 6) % 7] + ' ' + d.getHours() + ':' + String(d.getMinutes()).padStart(2, '0');
    return d.getDate() + '. ' + (d.getMonth() + 1) + '.';
  }

  /* ── jednotný tvar seznamu ───────────────────────────────────────────────── */
  /* {kde, items: [{id, label, sub, active, expires, expiring, pevne}], limity, pocita, raw}
     kde: 'server' = předplatná v prostoru (drží je brána),
          'lokal'  = Claude Code na tomhle počítači (id '' = běžné přihlášení). */
  function zeServeru(st) {
    st = st || {};
    return {kde: 'server', items: (st.predplatna || []).map((p) => Object.assign({}, p)),
            limity: st.limity || {}, pocita: !!st.limity_pocita, raw: st};
  }

  function zPocitace(st) {
    st = st || {};
    const u = st.ucet || {};
    const items = [{id: '', label: 'Běžné přihlášení', pevne: true, active: !st.aktivni,
                    sub: u.email ? u.email + (u.plan ? ' · ' + u.plan : '') : 'Claude Code na tomhle počítači'}];
    for (const p of st.predplatna || []) items.push(Object.assign({}, p));
    return {kde: 'lokal', items, limity: st.limity || {}, pocita: !!st.limity_pocita, raw: st};
  }

  // Seznam se mění z víc míst (Nastavení, ⋯, návrh na přepnutí) — ať všechna
  // místa ukazují totéž, jde každá změna ven jako událost.
  function oznam(d) {
    global.dispatchEvent(new CustomEvent('hub-predplatne', {detail: d}));
  }

  // Odpověď na změnu (POST) limity nenese — nechat ty, co už jsou známé.
  function spoj(stare, nove) {
    if (!stare || Object.keys(nove.limity || {}).length) return nove;
    return Object.assign({}, nove, {limity: stare.limity || {}});
  }

  function chyba(d) {
    if (d && d.error) throw new Error(d.error);
    return d;
  }

  /* ── odkud seznam bere data ──────────────────────────────────────────────── */
  /* V prostoru: okno mluví s bránou samo (cookie). */
  function srcProstor(io) {
    return {
      kde: 'server',
      load: async (limity, obnovit) => zeServeru(await gwClaude(undefined,
        limity ? '?limity=1&cekat=6' + (obnovit ? '&obnovit=1' : '') : '')),
      use: async (id) => zeServeru(await gwClaude({activate: id})),
      rename: async (id, label) => zeServeru(await gwClaude({rename: id, label})),
      remove: async (id) => zeServeru(await gwClaude({remove: true, id})),
      add: (replace) => pridatVProstoru(io, {replace}),
    };
  }

  /* Na počítači: předplatná v prostoru přes appku (token zařízení). */
  function srcPcServer(io) {
    async function stav(q) {
      const st = await io.api('predplatne' + q);
      const srv = (st && st.server) || {};
      if (srv.error) throw new Error(srv.error);
      return zeServeru(srv);
    }
    const post = async (payload) => zeServeru(chyba(await io.api('predplatne', payload)));
    return {
      kde: 'server',
      load: (limity, obnovit) => stav(limity ? '?limity=1' + (obnovit ? '&obnovit=1' : '') : ''),
      use: (id) => post({action: 'server-aktivni', id}),
      rename: (id, label) => post({action: 'server-prejmenovat', id, label}),
      remove: (id) => post({action: 'server-odebrat', id}),
      add: (replace) => connect(io, {cil: 'server', replace, dalsi: true}),
    };
  }

  /* Na počítači: předplatná pro Claude Code v tabech. */
  function srcLokal(io) {
    const post = async (payload) => zPocitace(chyba(await io.api('predplatne', payload)));
    return {
      kde: 'lokal',
      load: async (limity, obnovit) => zPocitace(chyba(await io.api('predplatne?lokal=1' +
        (limity ? '&limity=1' + (obnovit ? '&obnovit=1' : '') : '')))),
      use: (id) => post({action: 'lokal-aktivni', id}),
      rename: (id, label) => post({action: 'lokal-prejmenovat', id, label}),
      remove: (id) => post({action: 'lokal-odebrat', id}),
      add: (replace) => connect(io, {cil: 'lokal', replace, dalsi: true}),
    };
  }

  /* Přepnutí — ze seznamu, z nabídky i z návrhu. Nové zprávy jdou hned na
     nové předplatné; chaty, které zrovna pracují, doběhnou na původním a
     přepnou se, až skončí (hub Hub.prenacti_chaty). */
  async function prepni(io, src, id, label) {
    const d = await src.use(id);
    // Kdo si sám vybral téměř vyčerpané předplatné, ví proč — návrh na
    // přepnutí jinam ho do obnovení limitu otravovat nemá.
    const l = (d.limity && d.limity[id]) || (W.data && W.data.kde === src.kde && W.data.limity[id]);
    if (l && l.ok && procenta(l.week) >= NAVRH_OD) {
      try { localStorage.setItem(ZAMITNUTO_KEY, src.kde + '|' + id + '|' + (l.week.resets || '')); } catch (_) { /* soukromé okno */ }
    }
    oznam(d);
    const st = d.raw || {};
    let text = 'Nové zprávy půjdou z „' + label + '“. Rozdělaná práce doběhne na původním.';
    if (src.kde === 'server' && st.needs_restart) {
      text = 'Přepnuto na „' + label + '“ — projeví se po restartu prostoru (Nastavení → Účet).';
    } else if (src.kde === 'server' && st.running === false) {
      text = 'Claude v prostoru pojede na „' + label + '“.';
    }
    io.toast(text);
    return d;
  }

  function dotaz(text, hodnota, opts) {
    if (global.HubDialog) return global.HubDialog.prompt(text, hodnota, opts);
    return Promise.resolve(global.prompt(text, hodnota));
  }

  function potvrd(text, opts) {
    if (global.HubDialog) return global.HubDialog.confirm(text, opts);
    return Promise.resolve(global.confirm(text));
  }

  /* ── seznam předplatných (Nastavení → Účet) ──────────────────────────────── */
  /* opts.onData(d) — panel kolem seznamu se podle něj překreslí (režim, tlačítka). */
  function seznam(io, src, opts) {
    opts = opts || {};
    const box = el('div', 'pd-seznam');
    let data = null;
    let timer = null;
    let pokusy = 0;

    function prijmi(d) {
      data = spoj(data, d);
      kresli();
      if (opts.onData) opts.onData(data);
    }

    async function nacti(limity, obnovit) {
      clearTimeout(timer);
      try {
        prijmi(await src.load(limity, obnovit));
      } catch (e) {
        if (!data) {
          box.textContent = '';
          box.appendChild(el('div', 'set-note', friendly('load ' + src.kde, e)));
        }
        return;
      }
      // Limity zjišťuje server na pozadí — za chvíli se zeptat znovu, nejvýš párkrát.
      if (limity && data.pocita && pokusy++ < 6) {
        timer = setTimeout(() => { if (box.isConnected) nacti(true); }, 4000);
      }
    }

    function link(text, run, cls) {
      const b = el('button', 'pd-link' + (cls ? ' ' + cls : ''), text);
      b.type = 'button';
      b.onclick = run;
      return b;
    }

    function radek(it) {
      const r = el('div', 'pd-item' + (it.active ? ' on' : ''));
      const radio = el('button', 'pd-radio');
      radio.type = 'button';
      radio.setAttribute('role', 'radio');
      radio.setAttribute('aria-checked', it.active ? 'true' : 'false');
      radio.setAttribute('aria-label', (it.active ? 'Používá se: ' : 'Použít ') + it.label);
      radio.title = it.active ? 'Na tomhle teď Claude jede' : 'Přepnout na tohle předplatné';
      radio.onclick = () => { if (!it.active) pouzij(it); };
      r.appendChild(radio);

      const main = el('div', 'pd-main');
      const name = el('div', 'pd-name');
      name.appendChild(el('span', 'pd-label', it.label));
      if (it.active) name.appendChild(el('span', 'pd-tag', 'používá se'));
      main.appendChild(name);
      const sub = [];
      if (it.sub) sub.push(it.sub);
      if (it.expires) sub.push((it.expiring ? 'brzy vyprší — platí do ' : 'platí do ') + date(it.expires));
      if (sub.length) main.appendChild(el('div', 'pd-sub' + (it.expiring ? ' warn' : ''), sub.join(' · ')));

      const l = data.limity[it.id];
      const lim = el('div', 'pd-limits');
      if (l && l.ok) {
        if (l.week) lim.appendChild(meter('týden', l.week));
        if (l.session) lim.appendChild(meter('5 h', l.session));
      } else if (l && !l.ok) {
        lim.appendChild(el('span', 'pd-sub', l.chyba || 'Limit se nepodařilo zjistit.'));
      } else if (data.pocita) {
        lim.appendChild(el('span', 'pd-sub', 'Zjišťuji limit…'));
      }
      if (lim.children.length) main.appendChild(lim);

      if (!it.pevne) {
        const acts = el('div', 'pd-acts');
        acts.appendChild(link('Přejmenovat', () => prejmenuj(it)));
        // Token platí rok — znovu připojit má smysl, když vyprší nebo přestal platit.
        if (src.add && (it.expiring || (l && l.druh === 'vyprselo'))) {
          acts.appendChild(link('Připojit znovu', () => pridej(it.id)));
        }
        acts.appendChild(link('Odebrat', () => odeber(it), 'danger'));
        main.appendChild(acts);
      }
      r.appendChild(main);

      if (!it.active) {
        const b = el('button', 'btn ghost pd-use', 'Použít');
        b.onclick = () => pouzij(it, b);
        r.appendChild(b);
      }
      return r;
    }

    function kresli() {
      box.textContent = '';
      const prazdne = !data.items.length || (data.kde === 'lokal' && data.items.length < 2);
      if (!prazdne) {
        const list = el('div', 'pd-list');
        list.setAttribute('role', 'radiogroup');
        list.setAttribute('aria-label', 'Na kterém předplatném Claude jede');
        for (const it of data.items) list.appendChild(radek(it));
        box.appendChild(list);
      }
      if (opts.pred) opts.pred(box, data);
      const row = el('div', 'pd-row pd-foot');
      if (src.add && opts.pridat !== false) {
        const b = el('button', 'btn ghost', prazdne ? '+ Přidat předplatné' : '+ Přidat další předplatné');
        b.onclick = () => pridej('');
        row.appendChild(b);
      }
      if (!prazdne) {
        const o = el('button', 'btn ghost pd-refresh', 'Obnovit limity');
        o.title = 'Zeptat se Anthropicu znovu (jinak se limity drží 5 minut)';
        o.onclick = () => { pokusy = 0; nacti(true, true); };
        row.appendChild(o);
      }
      if (row.children.length) box.appendChild(row);
    }

    async function pouzij(it, btn) {
      if (btn) btn.disabled = true;
      try {
        await prepni(io, src, it.id, it.label);
      } catch (e) {
        io.toast('Přepnout se nepovedlo: ' + friendly('use', e));
        if (btn) btn.disabled = false;
      }
    }

    async function prejmenuj(it, nove) {
      const text = nove
        ? 'Předplatné je přidané. Jak se má jmenovat? Podle názvu ho poznáš v přepínači — třeba „Max — práce“.'
        : 'Jak se má předplatné jmenovat? Podle názvu ho poznáš v přepínači.';
      const nazev = await dotaz(text, it.label, {title: 'Název předplatného', ok: 'Uložit'});
      if (nazev == null || !nazev.trim() || nazev.trim() === it.label) return;
      try {
        oznam(await src.rename(it.id, nazev.trim()));
      } catch (e) {
        io.toast('Přejmenovat se nepovedlo: ' + friendly('rename', e));
      }
    }

    async function odeber(it) {
      const dalsi = data.items.find((x) => x.id !== it.id);
      let text = 'Token se smaže; kdykoli ho můžeš přidat znovu.';
      if (it.active && data.kde === 'lokal') {
        text = 'Nové chaty na tomhle počítači pak pojedou na běžném přihlášení Claude Code.';
      } else if (it.active && dalsi) {
        text = 'Claude pak pojede na „' + dalsi.label + '“.';
      } else if (it.active) {
        text = 'Claude v prostoru pak pojede přes firemní přístup, nebo se bude muset přihlásit.';
      }
      if (!(await potvrd(text, {title: 'Odebrat „' + it.label + '“?', ok: 'Odebrat', danger: true}))) return;
      try {
        oznam(await src.remove(it.id));
        io.toast('Předplatné „' + it.label + '“ je odebrané.');
      } catch (e) {
        io.toast('Odebrat se nepovedlo: ' + friendly('remove', e));
      }
    }

    async function pridej(replace) {
      const r = await src.add(replace || '');
      if (!r || !r.done) return;
      pokusy = 0;
      await nacti(true);
      // Nové předplatné hned pojmenovat — z tokenu se e-mail ani tarif nezjistí.
      const it = !replace && r.id && data && data.items.find((x) => x.id === r.id);
      if (it) prejmenuj(it, true);
    }

    const onEv = (ev) => {
      if (!box.isConnected && data) {
        global.removeEventListener('hub-predplatne', onEv);
        return;
      }
      if (ev.detail && ev.detail.kde === src.kde) prijmi(ev.detail);
    };
    global.addEventListener('hub-predplatne', onEv);

    box.appendChild(el('div', 'set-note', 'Zjišťuji předplatná…'));
    // Napřed rychle bez limitů, pak limity (ty se ptají Anthropicu).
    (async () => {
      await nacti(false);
      if (data && data.items.length > (data.kde === 'lokal' ? 1 : 0)) nacti(true);
    })();
    box.nacti = nacti;
    return box;
  }

  /* ── na počítači: karta s propojením ─────────────────────────────────────── */
  /* opts.auto = spuštěno samo před vstupem do prostoru („Teď ne" se zapamatuje).
     opts.cil = 'server' (předplatné pro prostor) | 'lokal' (pro Claude Code na
     tomhle počítači); opts.replace = id předplatného, kterému se obnovuje
     přihlášení; opts.dalsi = přidává se další (jiný účet). Resolve {done, id}. */
  function connect(io, opts) {
    opts = opts || {};
    const cfg = (io.state && io.state.config) || {};
    const lokal = opts.cil === 'lokal';
    return new Promise((resolve) => {
      const root = el('div', 'onb pd-card');
      const box = el('div', 'onb-box');
      const head = el('div', 'onb-head');
      const mark = el('span', 'onb-mark');
      mark.innerHTML = '<svg viewBox="0 0 24 24" width="30" height="30"><use href="#i-hub"/></svg>';
      head.appendChild(mark);
      const titles = el('div');
      titles.appendChild(el('div', 'onb-title', opts.replace ? 'Připojit předplatné znovu'
        : opts.dalsi ? 'Přidat předplatné Claude' : 'Claude na tvém předplatném'));
      titles.appendChild(el('div', 'onb-sub', lokal ? 'Claude Code na tomhle počítači'
        : global.HubServer ? global.HubServer.hostOf(cfg.gw_server || '') : ''));
      head.appendChild(titles);
      box.appendChild(head);
      const body = el('div', 'onb-body');
      box.appendChild(body);
      const foot = el('div', 'onb-foot');
      const later = el('button', 'btn ghost', opts.auto ? 'Teď ne' : 'Zrušit');
      foot.appendChild(later);
      foot.appendChild(el('span', 'spacer'));
      const again = el('button', 'btn primary', 'Zkusit znovu');
      again.hidden = true;
      foot.appendChild(again);
      box.appendChild(foot);
      root.appendChild(box);
      document.body.appendChild(root);

      let timer = null;
      let finished = false;
      let shownUrl = '';

      function finish(done, id) {
        if (finished) return;
        finished = true;
        clearTimeout(timer);
        root.remove();
        resolve({done, id: id || ''});
      }

      const lead = el('p', 'onb-lead');
      if (opts.dalsi || opts.replace) {
        lead.append('V prohlížeči se otevřela stránka Claude. Přihlas se účtem, jehož předplatné ' +
          (opts.replace ? 'obnovuješ' : 'chceš přidat') + ', a klikni na ');
        lead.appendChild(el('b', '', 'Authorize (Povolit)'));
        lead.append('.');
      } else {
        lead.append('Aby Claude ve tvém prostoru na serveru fungoval, propojí se s tvým ' +
          'předplatným Claude (Pro, Max, Team). V prohlížeči se otevřela stránka Claude — ' +
          'přihlas se, když ještě nejsi, a klikni na ');
        lead.appendChild(el('b', '', 'Authorize (Povolit)'));
        lead.append('. Nic dalšího není potřeba.');
      }
      const line = el('div', 'pd-line');
      const help = el('details', 'pd-help');
      help.appendChild(el('summary', '', opts.dalsi ? 'Prohlížeč se neotevřel, nebo je v něm jiný účet?'
        : 'Prohlížeč se neotevřel?'));
      const helpBody = el('div', 'pd-help-body');
      const openBtn = el('button', 'btn ghost', 'Otevřít přihlášení');
      openBtn.disabled = true;
      openBtn.onclick = () => shownUrl && io.open(shownUrl);
      const copyBtn = el('button', 'btn ghost', 'Kopírovat odkaz');
      copyBtn.disabled = true;
      copyBtn.onclick = () => {
        if (!shownUrl) return;
        if (io.copy) io.copy(shownUrl);
        else if (navigator.clipboard) navigator.clipboard.writeText(shownUrl).catch(() => {});
        io.toast('Odkaz zkopírovaný — otevři ho v anonymním okně prohlížeče.');
      };
      helpBody.appendChild(el('div', 'set-note', opts.dalsi
        ? 'Stránka Claude použije účet, kterým jsi v prohlížeči přihlášený na claude.ai. Chceš ' +
          'přidat jiný? Zkopíruj odkaz, otevři ho v anonymním okně a přihlas se tam druhým účtem. ' +
          'Po kliknutí na Authorize (Povolit) ukáže stránka kód — vlož ho sem.'
        : 'Otevři přihlášení ručně. Po kliknutí na Authorize (Povolit) ukáže stránka kód — vlož ho sem.'));
      const row = el('div', 'pd-row');
      row.appendChild(openBtn);
      if (opts.dalsi) row.appendChild(copyBtn);
      helpBody.appendChild(row);
      const row2 = el('div', 'pd-row');
      const code = el('input', 'srv-input pd-code');
      code.placeholder = 'kód ze stránky';
      code.autocomplete = 'off';
      row2.appendChild(code);
      const send = el('button', 'btn primary', 'Odeslat');
      row2.appendChild(send);
      helpBody.appendChild(row2);
      help.appendChild(helpBody);
      const note = el('p', 'onb-note', lokal
        ? 'Token se uloží jen na tomhle počítači a jen pro tebe. Tvoje běžné přihlášení Claude Code ' +
          'zůstává — mezi předplatnými přepínáš v Nastavení → Účet nebo v chatu u výběru modelu.'
        : 'Propojení platí rok a patří jen tvému účtu na serveru. ' +
          'Na počítači se neukládá. Předplatné se mezi lidmi nesdílí — každý připojuje svoje.');
      body.append(lead, line, help, note);

      send.onclick = async () => {
        if (!code.value.trim()) return code.focus();
        send.disabled = true;
        try { await io.api('predplatne', {action: 'code', code: code.value}); } catch (_) {}
        code.value = '';
        send.disabled = false;
        say('busy', 'Ověřuji kód…');
      };
      code.onkeydown = (ev) => { if (ev.key === 'Enter') send.onclick(); };

      function say(kind, text) {
        line.className = 'pd-line ' + kind;
        line.textContent = text;
      }

      function draw(st) {
        const c = st.connect || {};
        if (c.url && c.url !== shownUrl) {
          shownUrl = c.url;
          openBtn.disabled = copyBtn.disabled = false;
        }
        again.hidden = c.state !== 'error';
        if (c.state === 'done') {
          lead.hidden = help.hidden = true;
          say('ok', lokal
            ? '✓ Hotovo. Nové chaty na tomhle počítači pojedou na tomhle předplatném.'
            : '✓ Hotovo. Claude v prostoru teď jede na ' + (opts.dalsi ? 'tomhle' : 'tvém') + ' předplatném.' +
              (c.restarted === false ? ' Projeví se po restartu prostoru.' : ''));
          if (!lokal) cfg.predplatne_skip = false;
          setTimeout(() => finish(true, c.id), 1500);
          return false;
        }
        if (c.state === 'error') {
          say('err', '! ' + (c.error || 'Nepovedlo se.'));
          return false;
        }
        say('busy', c.state === 'saving' ? (lokal ? 'Ukládám…' : 'Ukládám na server…')
          : 'Čekám na potvrzení v prohlížeči…');
        return true;
      }

      async function poll() {
        if (finished) return;
        let more = true;
        try { more = draw(await io.api('predplatne?quick=1')); } catch (_) { /* zkusit znovu */ }
        if (more) timer = setTimeout(poll, 1000);
      }

      async function run() {
        again.hidden = true;
        lead.hidden = help.hidden = false;
        say('busy', 'Spouštím přihlášení…');
        try {
          const st = await io.api('predplatne', {action: 'start', cil: opts.cil || 'server',
                                                 replace: opts.replace || ''});
          if (draw({connect: st}) !== false) poll();
        } catch (e) {
          say('err', '! ' + friendly('start', e));
          again.hidden = false;
        }
      }

      later.onclick = async () => {
        try {
          await io.api('predplatne', {action: 'cancel'});
          // Samo se to už ptát nebude; připojit jde kdykoli v Nastavení → Účet.
          if (opts.auto) {
            await io.api('predplatne', {action: 'skip'});
            cfg.predplatne_skip = true;
          }
        } catch (_) { /* jen volba */ }
        finish(false);
      };
      again.onclick = run;
      run();
    });
  }

  /* Před vstupem do prostoru: propojit, jen když tam Claude nemá na čem jet. */
  async function ensure(io) {
    const cfg = (io.state && io.state.config) || {};
    if (cfg.predplatne_skip) return;
    let st;
    try { st = await io.api('predplatne'); } catch (_) { return; }
    if (!st.claude || st.skip || !st.server || st.server.mode !== 'zadne') return;
    await connect(io, {auto: true});
  }

  /* ── na počítači: Nastavení → Účet ───────────────────────────────────────── */
  function panel(io) {
    const box = el('div', 'acc-sec pd-panel');
    box.appendChild(el('div', 'set-title', 'Claude v prostoru na serveru'));
    const body = el('div', 'set-note', 'Zjišťuji, na čem Claude v prostoru jede…');
    box.appendChild(body);

    async function load() {
      let st;
      try { st = await io.api('predplatne'); } catch (e) {
        body.textContent = friendly('load', e);
        return;
      }
      draw(st);
    }

    function draw(st) {
      body.textContent = '';
      body.className = '';
      const srv = st.server || {};
      if (srv.error) {
        body.appendChild(el('div', 'set-note', srv.kind === 'unsupported'
          ? 'Server předplatné ještě neumí — aktualizuje se sám v noci.' : srv.error));
        return;
      }
      const stav = el('div');
      body.appendChild(stav);
      function hlava(s) {
        stav.textContent = '';
        stav.appendChild(el('div', s.mode === 'zadne' ? 'set-warn' : 'set-ok',
          (s.mode === 'zadne' ? '! ' : '✓ ') + MODES[s.mode]));
        if (s.mode !== 'predplatne') {
          stav.appendChild(el('div', 'set-note',
            'Claude v prostoru jede na tvém předplatném Claude (Pro, Max, Team). ' +
            'Aplikace ho propojí sama — v prohlížeči jen klikneš na Authorize (Povolit).'));
        }
      }
      hlava(srv);
      // Brána, která umí víc předplatných, posílá seznam.
      if (Array.isArray(srv.predplatna) && srv.predplatna.length && st.claude) {
        body.appendChild(seznam(io, srcPcServer(io), {onData: (d) => hlava(d.raw)}));
        return;
      }
      if (srv.mode === 'predplatne' && srv.expires) {
        body.appendChild(el('div', 'set-note',
          (srv.expiring ? 'Brzy vyprší — připoj ho znovu. ' : '') + 'Platí do ' + date(srv.expires) + '.'));
      }
      const row = el('div', 'set-row');
      if (st.claude) {
        const b = el('button', srv.mode === 'predplatne' ? 'btn ghost' : 'btn primary',
          srv.mode === 'predplatne' ? 'Připojit znovu' : 'Použít moje předplatné');
        b.onclick = async () => {
          const r = await connect(io);
          if (r.done) load();
        };
        row.appendChild(b);
      } else {
        row.appendChild(el('span', 'set-dim',
          'Na tomhle počítači chybí Claude. Přihlásíš ho přímo v prostoru: otevři tam ' +
          'rozhovor s Claudem a zvol „Přihlásit Claude“.'));
      }
      if (srv.mode === 'predplatne') {
        const off = el('button', 'btn ghost', 'Odpojit');
        off.onclick = async () => {
          off.disabled = true;
          try {
            const r = await io.api('predplatne', {action: 'disconnect'});
            if (r.error) throw new Error(r.error);
            io.toast('Předplatné odpojené.');
          } catch (e) { io.toast('Odpojit se nepovedlo: ' + friendly('disconnect', e)); }
          load();
        };
        row.appendChild(off);
      }
      body.appendChild(row);
    }

    load();
    return box;
  }

  /* Na počítači pod účtem Claude Code: další předplatná pro taby na tomhle
     počítači. Bez nich je vidět jen krátká nabídka „+ Přidat předplatné“. */
  function lokalBlock(io) {
    const box = el('div', 'pd-lokal');
    const nadpis = el('div', 'set-title plg-podnadpis', 'Víc předplatných');
    const pozn = el('div', 'set-note');
    box.append(nadpis, pozn);
    box.appendChild(seznam(io, srcLokal(io), {onData: (d) => {
      pozn.textContent = d.items.length < 2
        ? 'Máš víc účtů Claude (třeba Max v práci a Pro doma)? Přidej další předplatné a přepínej ' +
          'mezi nimi, když jednomu dojde limit. Běžné přihlášení zůstává.'
        : 'Na vybraném předplatném jedou nové chaty s Claude Code na tomhle počítači. ' +
          'Rychle přepneš i v chatu u výběru modelu (na telefonu v nabídce ⋯).';
    }}));
    return box;
  }

  /* ── v prostoru na serveru ───────────────────────────────────────────────── */
  async function gwClaude(payload, query) {
    const r = await fetch('/gw/claude' + (query || ''), payload === undefined ? {credentials: 'same-origin'} : {
      method: 'POST', credentials: 'same-origin',
      headers: {'Content-Type': 'application/json', 'X-Hub-Account': '1'},
      body: JSON.stringify(payload),
    });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) {
      console.warn('predplatne.js: /gw/claude HTTP ' + r.status, data);
      throw new Error(data.error || 'Server teď neodpovídá, zkus to za chvíli.');
    }
    return data;
  }

  function restart(why) {
    if (typeof global.restartSpace === 'function') return global.restartSpace(why);
    return null;
  }

  /* Přidání předplatného přímo v prostoru, bez appky na počítači:
     `claude setup-token` běží v prostoru (gateway/server.py, _predplatne_pridat),
     člověk otevře odkaz, klikne na Authorize a vloží kód ze stránky. Token
     zůstane u brány. Resolve {done, id}. */
  function pridatVProstoru(io, opts) {
    opts = opts || {};
    return new Promise((resolve) => {
      const root = el('div', 'onb pd-card');
      const box = el('div', 'onb-box');
      const head = el('div', 'onb-head');
      const mark = el('span', 'onb-mark');
      mark.innerHTML = '<svg viewBox="0 0 24 24" width="30" height="30"><use href="#i-hub"/></svg>';
      head.appendChild(mark);
      const titles = el('div');
      titles.appendChild(el('div', 'onb-title', opts.replace ? 'Připojit předplatné znovu' : 'Přidat předplatné Claude'));
      titles.appendChild(el('div', 'onb-sub', 'Claude v tvém prostoru'));
      head.appendChild(titles);
      box.appendChild(head);
      const body = el('div', 'onb-body');
      box.appendChild(body);
      const foot = el('div', 'onb-foot');
      const zrusit = el('button', 'btn ghost', 'Zrušit');
      foot.appendChild(zrusit);
      foot.appendChild(el('span', 'spacer'));
      const again = el('button', 'btn primary', 'Zkusit znovu');
      again.hidden = true;
      foot.appendChild(again);
      box.appendChild(foot);
      root.appendChild(box);
      document.body.appendChild(root);

      const steps = el('ol', 'pd-steps');
      function krok(cislo, titulek, popis) {
        const li = el('li');
        li.appendChild(el('span', 'pd-num', String(cislo)));
        const d = el('div', 'pd-krok');
        d.appendChild(el('div', 'pd-krok-t', titulek));
        if (popis) d.appendChild(el('div', 'pd-krok-d', popis));
        li.appendChild(d);
        steps.appendChild(li);
        return d;
      }
      const k1 = krok(1, 'Otevři přihlášení Claude',
        'Přihlas se účtem, jehož předplatné ' + (opts.replace ? 'obnovuješ' : 'chceš přidat') + '.');
      const odkazy = el('div', 'pd-row');
      const otevrit = el('button', 'btn primary', 'Otevřít přihlášení Claude ↗');
      otevrit.disabled = true;
      const kopirovat = el('button', 'btn ghost', 'Kopírovat odkaz');
      kopirovat.disabled = true;
      odkazy.append(otevrit, kopirovat);
      k1.appendChild(odkazy);
      k1.appendChild(el('div', 'pd-tip', 'Stránka použije účet, kterým jsi v prohlížeči přihlášený na ' +
        'claude.ai. Jsi tam jiným? Zkopíruj odkaz a otevři ho v anonymním okně.'));
      krok(2, 'Klikni na Authorize (Povolit)', 'Stránka pak ukáže kód.');
      const k3 = krok(3, 'Vlož kód sem');
      const row = el('div', 'pd-row');
      const code = el('input', 'srv-input pd-code');
      code.placeholder = 'kód ze stránky Claude';
      code.autocomplete = 'off';
      const send = el('button', 'btn primary', 'Odeslat');
      row.append(code, send);
      k3.appendChild(row);
      const line = el('div', 'pd-line');
      const note = el('p', 'onb-note', 'Token platí rok, zůstane u serveru a patří jen tvému účtu. ' +
        'Předplatné se mezi lidmi nesdílí — každý připojuje svoje.');
      body.append(steps, line, note);

      let url = '';
      let timer = null;
      let finished = false;

      function say(kind, text) {
        line.className = 'pd-line ' + kind;
        line.textContent = text;
      }

      function finish(done, id) {
        if (finished) return;
        finished = true;
        clearTimeout(timer);
        root.remove();
        resolve({done, id: id || ''});
      }

      function draw(st) {
        const p = (st && st.pridani) || {};
        if (st && Array.isArray(st.predplatna)) oznam(zeServeru(st));
        if (p.url && p.url !== url) {
          url = p.url;
          otevrit.disabled = kopirovat.disabled = false;
        }
        again.hidden = p.state !== 'error';
        if (p.state === 'done') {
          steps.hidden = true;
          say('ok', '✓ Hotovo. Claude v prostoru teď jede na tomhle předplatném.');
          setTimeout(() => finish(true, p.id), 1500);
          return false;
        }
        if (p.state === 'error') {
          say('err', '! ' + (p.error || 'Nepovedlo se.'));
          return false;
        }
        if (p.state === 'idle') {
          say('err', '! Přihlášení se přerušilo — zkus to znovu.');
          again.hidden = false;
          return false;
        }
        say('busy', p.state === 'token' ? 'Ukládám…' : url ? 'Čekám na kód ze stránky Claude…' : 'Připravuji přihlášení…');
        return true;
      }

      async function poll() {
        if (finished) return;
        let more = true;
        try { more = draw(await gwClaude(undefined, '?pridani=1')); } catch (_) { /* zkusit znovu */ }
        if (more) timer = setTimeout(poll, 2000);
      }

      async function run() {
        again.hidden = true;
        steps.hidden = false;
        url = '';
        otevrit.disabled = kopirovat.disabled = true;
        say('busy', 'Připravuji přihlášení…');
        try {
          if (draw(await gwClaude({pridat: 'start', replace: opts.replace || ''})) !== false) {
            timer = setTimeout(poll, 1500);
          }
        } catch (e) {
          say('err', '! ' + friendly('pridat start', e));
          again.hidden = false;
        }
      }

      otevrit.onclick = () => url && io.open(url);
      kopirovat.onclick = () => {
        if (!url) return;
        if (io.copy) io.copy(url);
        else if (navigator.clipboard) navigator.clipboard.writeText(url).catch(() => {});
        io.toast('Odkaz zkopírovaný — otevři ho v anonymním okně prohlížeče.');
      };
      send.onclick = async () => {
        if (!code.value.trim()) return code.focus();
        send.disabled = true;
        clearTimeout(timer);
        say('busy', 'Ověřuji kód…');
        try {
          const st = await gwClaude({pridat: 'kod', kod: code.value.trim()});
          code.value = '';
          if (draw(st) !== false) timer = setTimeout(poll, 1500);
        } catch (e) {
          say('err', '! ' + friendly('pridat kod', e));
        }
        send.disabled = false;
      };
      code.onkeydown = (ev) => { if (ev.key === 'Enter') send.onclick(); };
      zrusit.onclick = async () => {
        clearTimeout(timer);
        try { await gwClaude({pridat: 'zrusit'}); } catch (_) { /* jen úklid */ }
        finish(false);
      };
      again.onclick = run;
      run();
    });
  }

  function serverBlock(io) {
    const box = el('div', 'acc-sec pd-panel');
    box.appendChild(el('div', 'set-title', 'Claude'));
    const body = el('div', 'set-note', 'Zjišťuji…');
    box.appendChild(body);
    // Z appky, která propojení umí — starší by návrat na počítač tiše přeskočila.
    const fromApp = !!(global.HubServer && global.HubServer.localBack() &&
                       global.HubServer.appAtLeast('2.14.1'));
    const oldApp = !!(global.HubServer && global.HubServer.localBack()) && !fromApp;
    const src = srcProstor(io);
    let list = null;

    function draw(st) {
      body.textContent = '';
      body.className = '';
      body.appendChild(el('div', st.mode === 'zadne' ? 'set-warn' : 'set-ok',
        (st.mode === 'zadne' ? '! ' : '✓ ') + MODES[st.mode]));
      const vic = Array.isArray(st.predplatna);
      if (st.mode === 'predplatne' && st.expires && !vic) {
        body.appendChild(el('div', 'set-note',
          (st.expiring ? 'Brzy vyprší — připoj ho znovu z aplikace na počítači. ' : '') +
          'Platí do ' + date(st.expires) + '.'));
      }
      // Kdo je přihlášený a jaké má předplatné (e-mail, tarif, organizace).
      const ucet = el('div', 'pd-ucet');
      body.appendChild(ucet);
      if (io.api && st.mode !== 'predplatne') io.api('claude-ucet').then(({account: a}) => {
        if (!a || !a.email) return;
        const radek = (k, v) => { const r = el('div', 'pd-ucet-r'); r.append(el('span', 'pd-ucet-k', k), el('span', 'pd-ucet-v', v)); ucet.appendChild(r); };
        radek('E-mail', a.email);
        radek('Předplatné', a.plan || 'neznámé');
        if (a.org) radek('Organizace', a.org);
      }).catch(() => {});
      if (oldApp && st.mode !== 'ucet') body.appendChild(global.HubServer.oldAppNote());
      if (st.mode === 'zadne' && !fromApp && !oldApp) {
        body.appendChild(el('div', 'set-note',
          'Otevři prostor v aplikaci Aladin na počítači — propojí ho s tvým předplatným ' +
          'sama. Nebo předplatné přidej tady (níž), případně v rozhovoru s Claudem zvol ' +
          'přihlášení účtem Claude.'));
      }
      const row = el('div', 'set-row');
      if (st.needs_restart) {
        const b = el('button', 'btn primary', 'Restartovat prostor');
        b.onclick = () => restart('Změna přihlášení Clauda se projeví po restartu prostoru.');
        row.appendChild(b);
      }
      if (fromApp && st.mode === 'zadne') {
        const b = el('button', 'btn primary', 'Použít moje předplatné');
        b.title = 'Okno se na chvíli vrátí do aplikace na počítači, v prohlížeči klikneš na ' +
          'Authorize (Povolit) a vrátíš se sem.';
        b.onclick = () => global.HubServer.backTo('predplatne');
        row.appendChild(b);
      }
      if (!vic) {
        // Starší brána: jedno předplatné jako dřív.
        if (fromApp && st.mode === 'predplatne') {
          const b = el('button', 'btn ghost', 'Připojit znovu');
          b.onclick = () => global.HubServer.backTo('predplatne');
          row.appendChild(b);
        }
        if (st.mode === 'predplatne') {
          const off = el('button', 'btn ghost', 'Odpojit předplatné');
          off.onclick = async () => {
            off.disabled = true;
            try { draw(await gwClaude({remove: true})); io.toast('Předplatné odpojené.'); }
            catch (e) { io.toast('Odpojit se nepovedlo: ' + friendly('gw disconnect', e)); off.disabled = false; }
          };
          row.appendChild(off);
        }
      }
      if (row.children.length) body.appendChild(row);
      // Z appky bez předplatného vede „Použít moje předplatné“ — druhé tlačítko
      // na totéž by jen mátlo.
      if (vic && !(fromApp && st.mode === 'zadne')) {
        // Seznam se kreslí jednou a překresluje sám (událost hub-predplatne);
        // hlavička nad ním se jen přepíše.
        if (!list) list = seznam(io, src, {onData: (d) => { if (d.raw && d.raw.mode) redraw(d.raw); }});
        body.appendChild(list);
        box.classList.toggle('pd-siroky', st.predplatna.length > 1);
      }
    }

    let posledni = '';
    function redraw(st) {
      // Hlavičku překreslit jen při změně — seznam se jinak zbytečně přesouvá.
      const klic = [st.mode, st.needs_restart, (st.predplatna || []).length].join('|');
      if (klic === posledni) return;
      posledni = klic;
      draw(st);
    }

    gwClaude().then(redraw, () => { body.textContent = 'Server tohle ještě neumí.'; });
    return box;
  }

  /* Po načtení prostoru: nové přihlášení čeká na restart. Jednou za načtení. */
  async function notice(io) {
    let st;
    try { st = await gwClaude(); } catch (_) { return; }
    if (st.needs_restart) {
      restart('Claude teď může jet na tvém předplatném — projeví se po restartu prostoru.');
    }
  }

  /* ── rychlé přepnutí a návrh, když dochází limit ─────────────────────────── */
  const W = {io: null, src: null, data: null, timer: 0, kdy: 0, karta: null};

  function hlidej(io) {
    if (W.io || !io || (io.state && io.state.guest)) return;
    W.io = io;
    const naServeru = !!(io.state && io.state.config && io.state.config.gateway_user);
    W.src = naServeru ? srcProstor(io) : srcLokal(io);
    global.addEventListener('hub-predplatne', (ev) => {
      if (!ev.detail || ev.detail.kde !== W.src.kde) return;
      W.data = spoj(W.data, ev.detail);
      zkontroluj();
    });
    document.addEventListener('visibilitychange', () => {
      if (!document.hidden && Date.now() - W.kdy > HLIDAT_MS) obhlidka();
    });
    // Až po prvním vykreslení — start appky má přednost.
    setTimeout(obhlidka, 4000);
  }

  async function obhlidka() {
    clearTimeout(W.timer);
    W.kdy = Date.now();
    let pocita = false;
    try {
      let d = await W.src.load(false);
      // Limity (dotaz na Anthropic) jen u toho, kdo má mezi čím přepínat.
      if (d.items.length >= 2) d = await W.src.load(true);
      W.data = spoj(W.data, d);
      pocita = d.pocita;
      zkontroluj();
    } catch (_) { /* příště */ }
    W.timer = setTimeout(() => { if (!document.hidden) obhlidka(); }, pocita ? 20000 : HLIDAT_MS);
  }

  function navrh(d) {
    const akt = d.items.find((x) => x.active);
    const la = akt && d.limity[akt.id];
    const w = la && la.ok ? procenta(la.week) : null;
    if (w == null || w < NAVRH_OD) return null;
    let best = null;
    for (const it of d.items) {
      const l = d.limity[it.id];
      if (it.active || !l || !l.ok) continue;
      const pw = procenta(l.week);
      const ps = procenta(l.session);
      if (pw == null || pw >= VOLNO_POD || (ps != null && ps >= 90)) continue;
      if (!best || pw < best.pw) best = {it, pw};
    }
    return best && {akt, w, resets: la.week.resets || '', it: best.it, pw: best.pw};
  }

  function zamitnuto() {
    try { return localStorage.getItem(ZAMITNUTO_KEY) || ''; } catch (_) { return ''; }
  }

  function zkontroluj() {
    const n = W.data && navrh(W.data);
    const klic = n ? W.data.kde + '|' + n.akt.id + '|' + n.resets : '';
    if (!n || zamitnuto() === klic) {
      if (W.karta) { W.karta.remove(); W.karta = null; }
      return;
    }
    if (W.karta && W.karta.dataset.klic === klic + '|' + n.it.id) return;
    ukazNavrh(n, klic);
  }

  function ukazNavrh(n, klic) {
    if (W.karta) W.karta.remove();
    const k = el('div', 'pd-navrh');
    k.setAttribute('role', 'status');
    k.dataset.klic = klic + '|' + n.it.id;
    const head = el('div', 'pd-navrh-head');
    head.innerHTML = '<svg class="ico"><use href="#i-bolt"/></svg>';
    head.appendChild(el('span', '', 'Dochází týdenní limit'));
    const x = el('button', 'pd-navrh-x', '×');
    x.setAttribute('aria-label', 'Zavřít');
    head.appendChild(x);
    k.appendChild(head);
    const kdy = kdyObnovi(n.resets);
    k.appendChild(el('div', 'pd-navrh-text', '„' + n.akt.label + '“ má vyčerpáno ' + n.w + ' % týdne' +
      (kdy ? ' (' + kdy + ')' : '') + '. „' + n.it.label + '“ má volno — týden ' + n.pw + ' %.'));
    const btns = el('div', 'pd-navrh-btns');
    const ano = el('button', 'btn primary', 'Přepnout na „' + n.it.label + '“');
    const ne = el('button', 'btn ghost', 'Teď ne');
    btns.append(ano, ne);
    k.appendChild(btns);
    ano.onclick = async () => {
      ano.disabled = true;
      try {
        await prepni(W.io, W.src, n.it.id, n.it.label);
        k.remove();
        if (W.karta === k) W.karta = null;
      } catch (e) {
        W.io.toast('Přepnout se nepovedlo: ' + friendly('navrh', e));
        ano.disabled = false;
      }
    };
    // „Teď ne“ platí do obnovení limitu (nebo do přepnutí jinam).
    ne.onclick = x.onclick = () => {
      try { localStorage.setItem(ZAMITNUTO_KEY, klic); } catch (_) { /* soukromé okno */ }
      k.remove();
      if (W.karta === k) W.karta = null;
    };
    document.body.appendChild(k);
    W.karta = k;
  }

  /* Položka do nabídky (⋯ v liště, výběr modelu v bublině) — jen když je mezi
     čím přepínat. `open(polozky)` otevře podnabídku na stejném místě. */
  function menuItem(open) {
    const d = W.data;
    if (!d || !W.src || d.items.length < 2) return null;
    const akt = d.items.find((x) => x.active) || d.items[0];
    return {
      icon: 'i-user',
      label: 'Předplatné: ' + akt.label + ' ▸',
      run: () => {
        open(polozky());
        if (Date.now() - W.kdy > 60000) obhlidka();
      },
    };
  }

  function polozky() {
    const d = W.data;
    const out = d.items.map((it) => {
      const lim = limitKratce(d.limity[it.id]);
      return {
        icon: 'i-user', on: it.active,
        label: it.label + (lim ? ' — ' + lim : ''),
        run: () => {
          if (it.active) return;
          prepni(W.io, W.src, it.id, it.label)
            .catch((e) => W.io.toast('Přepnout se nepovedlo: ' + friendly('menu', e)));
        },
      };
    });
    out.push({icon: 'i-gear', label: 'Spravovat předplatná…', run: () => {
      if (global.HubSettings) global.HubSettings.open(Object.assign({}, W.io, {state: W.io.state, tab: 'ucet'}));
    }});
    return out;
  }

  global.HubPredplatne = {connect, ensure, panel, lokalBlock, serverBlock, notice, hlidej, menuItem};

})(window);
