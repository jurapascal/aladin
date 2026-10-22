/* Claude v prostoru na vlastním předplatném (hub/predplatne.py, /gw/claude).
 *
 * Aby Claude v prostoru na serveru fungoval bez klíče API, propojí ho appka na
 * počítači s předplatným toho člověka: spustí `claude setup-token`, v prohlížeči
 * stačí kliknout na Authorize a token odejde rovnou bráně. Tenhle modul je
 * všechno, co z toho je vidět:
 *
 *  - `ensure(io)`      před vstupem do prostoru (server.js, go): když Claude
 *                      v prostoru nemá na čem jet, rovnou spustí propojení.
 *  - `connect(io)`     karta s průběhem — čeká na prohlížeč, nabídne odkaz
 *                      a pole na kód, kdyby se prohlížeč neotevřel.
 *  - `panel(io)`       na počítači v Nastavení → Účet.
 *  - `serverBlock(io)` v prostoru v Nastavení → Účet.
 *  - `notice(io)`      v prostoru po načtení: předplatné je připojené, ale
 *                      běžící prostor ho dostane až restartem.
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

  /* ── na počítači: karta s propojením ─────────────────────────────────────── */
  /* opts.auto = spuštěno samo před vstupem do prostoru („Teď ne" se zapamatuje).
     Resolve {done}. */
  function connect(io, opts) {
    opts = opts || {};
    const cfg = (io.state && io.state.config) || {};
    return new Promise((resolve) => {
      const root = el('div', 'onb pd-card');
      const box = el('div', 'onb-box');
      const head = el('div', 'onb-head');
      const mark = el('span', 'onb-mark');
      mark.innerHTML = '<svg viewBox="0 0 24 24" width="30" height="30"><use href="#i-hub"/></svg>';
      head.appendChild(mark);
      const titles = el('div');
      titles.appendChild(el('div', 'onb-title', 'Claude na tvém předplatném'));
      titles.appendChild(el('div', 'onb-sub',
        global.HubServer ? global.HubServer.hostOf(cfg.gw_server || '') : ''));
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

      function finish(done) {
        if (finished) return;
        finished = true;
        clearTimeout(timer);
        root.remove();
        resolve({done});
      }

      const lead = el('p', 'onb-lead');
      lead.append('Aby Claude ve tvém prostoru na serveru fungoval, propojí se s tvým ' +
        'předplatným Claude (Pro, Max, Team). V prohlížeči se otevřela stránka Claude — ' +
        'přihlas se, když ještě nejsi, a klikni na ');
      lead.appendChild(el('b', '', 'Authorize (Povolit)'));
      lead.append('. Nic dalšího není potřeba.');
      const line = el('div', 'pd-line');
      const help = el('details', 'pd-help');
      help.appendChild(el('summary', '', 'Prohlížeč se neotevřel?'));
      const helpBody = el('div', 'pd-help-body');
      const openBtn = el('button', 'btn ghost', 'Otevřít přihlášení');
      openBtn.disabled = true;
      openBtn.onclick = () => shownUrl && io.open(shownUrl);
      helpBody.appendChild(el('div', 'set-note',
        'Otevři přihlášení ručně. Po kliknutí na Authorize (Povolit) ukáže stránka kód — vlož ho sem.'));
      const row = el('div', 'set-row');
      row.appendChild(openBtn);
      const code = el('input', 'srv-input pd-code');
      code.placeholder = 'kód ze stránky';
      code.autocomplete = 'off';
      row.appendChild(code);
      const send = el('button', 'btn primary', 'Odeslat');
      row.appendChild(send);
      helpBody.appendChild(row);
      help.appendChild(helpBody);
      const note = el('p', 'onb-note', 'Propojení platí rok a patří jen tvému účtu na serveru. ' +
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
        if (c.url && c.url !== shownUrl) { shownUrl = c.url; openBtn.disabled = false; }
        again.hidden = c.state !== 'error';
        if (c.state === 'done') {
          lead.hidden = help.hidden = true;
          say('ok', '✓ Hotovo. Claude v prostoru teď jede na tvém předplatném.' +
            (c.restarted === false ? ' Projeví se po restartu prostoru.' : ''));
          cfg.predplatne_skip = false;
          setTimeout(() => finish(true), 1500);
          return false;
        }
        if (c.state === 'error') {
          say('err', '! ' + (c.error || 'Nepovedlo se.'));
          return false;
        }
        say('busy', c.state === 'saving' ? 'Ukládám na server…' : 'Čekám na potvrzení v prohlížeči…');
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
          const st = await io.api('predplatne', {action: 'start'});
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
      body.appendChild(el('div', srv.mode === 'zadne' ? 'set-warn' : 'set-ok',
        (srv.mode === 'zadne' ? '! ' : '✓ ') + MODES[srv.mode]));
      if (srv.mode === 'predplatne' && srv.expires) {
        body.appendChild(el('div', 'set-note',
          (srv.expiring ? 'Brzy vyprší — připoj ho znovu. ' : '') + 'Platí do ' + date(srv.expires) + '.'));
      } else {
        body.appendChild(el('div', 'set-note',
          'Claude v prostoru jede na tvém předplatném Claude (Pro, Max, Team). ' +
          'Aplikace ho propojí sama — v prohlížeči jen klikneš na Authorize (Povolit).'));
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

  /* ── v prostoru na serveru ───────────────────────────────────────────────── */
  async function gwClaude(payload) {
    const r = await fetch('/gw/claude', payload === undefined ? {credentials: 'same-origin'} : {
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

  function serverBlock(io) {
    const box = el('div', 'acc-sec pd-panel');
    box.appendChild(el('div', 'set-title', 'Claude'));
    const body = el('div', 'set-note', 'Zjišťuji…');
    box.appendChild(body);
    // Z appky, která propojení umí — starší by návrat na počítač tiše přeskočila.
    const fromApp = !!(global.HubServer && global.HubServer.localBack() &&
                       global.HubServer.appAtLeast('2.14.1'));
    const oldApp = !!(global.HubServer && global.HubServer.localBack()) && !fromApp;

    function draw(st) {
      body.textContent = '';
      body.className = '';
      body.appendChild(el('div', st.mode === 'zadne' ? 'set-warn' : 'set-ok',
        (st.mode === 'zadne' ? '! ' : '✓ ') + MODES[st.mode]));
      if (st.mode === 'predplatne' && st.expires) {
        body.appendChild(el('div', 'set-note',
          (st.expiring ? 'Brzy vyprší — připoj ho znovu z aplikace na počítači. ' : '') +
          'Platí do ' + date(st.expires) + '.'));
      }
      // Kdo je přihlášený a jaké má předplatné (e-mail, tarif, organizace).
      const ucet = el('div', 'pd-ucet');
      body.appendChild(ucet);
      if (io.api) io.api('claude-ucet').then(({account: a}) => {
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
          'sama. Nebo v rozhovoru s Claudem zvol přihlášení účtem Claude.'));
      }
      const row = el('div', 'set-row');
      if (st.needs_restart) {
        const b = el('button', 'btn primary', 'Restartovat prostor');
        b.onclick = () => restart('Změna přihlášení Clauda se projeví po restartu prostoru.');
        row.appendChild(b);
      }
      if (fromApp && st.mode !== 'ucet') {
        const b = el('button', st.mode === 'zadne' ? 'btn primary' : 'btn ghost',
          st.mode === 'predplatne' ? 'Připojit znovu' : 'Použít moje předplatné');
        b.title = 'Okno se na chvíli vrátí do aplikace na počítači, v prohlížeči klikneš na ' +
          'Authorize (Povolit) a vrátíš se sem.';
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
      if (row.children.length) body.appendChild(row);
    }

    gwClaude().then(draw, () => { body.textContent = 'Server tohle ještě neumí.'; });
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

  global.HubPredplatne = {connect, ensure, panel, serverBlock, notice};

})(window);
