/* Prostor na serveru — přihlášení z appky a přechod okna na bránu.
 *
 * Appka má dvě místa, kde může hub běžet: tady na počítači, nebo na serveru
 * v prostoru, který patří jen tobě. Tenhle modul je všechno, co je spojuje:
 *
 *  - `panel()`   adresa → Ověřit → e-mail a heslo → přihlášení. Adresa se
 *                ověřuje dřív, než se píše heslo: na překlepnuté adrese by
 *                se jinak heslo poslalo cizímu webu.
 *  - `go()`      okno přejde do prostoru na serveru a appka si to zapamatuje.
 *  - `gate()`    obrazovka při startu, když se na server nedalo rovnou přejít
 *                (neodpovídá, přihlášení vypršelo).
 *  - `backTo()`  z hubu na serveru zpátky na počítač (`mode`: local, logout,
 *                pocitac = jen změnit přístup Clauda na počítač a vrátit se,
 *                predplatne = propojit Clauda s předplatným a vrátit se).
 *
 * Cesta zpátky: při přechodu na server se do adresy přidá `#local=<adresa
 * hubu na počítači>`. Fragment prohlížeč serveru neposílá, takže token hubu na
 * počítači neskončí v logu nginx; hub na serveru si ho uloží do sessionStorage
 * a „Pracovat na tomto počítači" pak vede sem.
 */
'use strict';

(function (global) {

  const LOCAL_KEY = 'hub.localBack';
  // Verze appky na počítači, která okno do prostoru poslala (`#…&app=`).
  // Prostor je vždycky nový, appka u člověka může být starší — a co nezná,
  // to po návratu na počítač tiše přeskočí a okno problikne zpátky.
  const APP_KEY = 'hub.appVersion';

  // Věta od serveru (česky) projde; výpadek spojení nebo syrový výpis
  // (Failed to fetch, HTTP 502, Traceback…) se nahradí lidskou větou.
  function friendly(where, e) {
    console.warn('server.js: ' + where, e);
    const msg = (e && e.message) || '';
    if (!msg || e instanceof TypeError || msg.length > 200 ||
        /^HTTP \d|fetch|NetworkError|Traceback|Error:|<html/i.test(msg)) {
      return 'Server teď neodpovídá, zkus to za chvíli.';
    }
    return msg;
  }

  function el(tag, cls, text) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text != null) node.textContent = text;
    return node;
  }

  function hostOf(url) {
    try { return new URL(url).host; } catch (_) { return url || ''; }
  }

  /* Okno appky vs. telefon přes Tailscale. Na server se přechází jen z okna
     na počítači — telefon má bránu otevírat napřímo. */
  function isAppWindow() {
    return ['127.0.0.1', 'localhost', '[::1]'].includes(location.hostname);
  }

  /* ── na serveru: odkud se přišlo ─────────────────────────────────────────── */
  function captureLocal() {
    const m = /(?:^#|&)local=([^&]+)/.exec(location.hash);
    if (!m) return;
    let back = '';
    try { back = decodeURIComponent(m[1]); } catch (_) { return; }
    // Jen hub na tomhle počítači. Odkaz s `#local=` na cizí web by jinak
    // z tlačítka „Pracovat na tomto počítači" udělal přesměrování kamkoli.
    try {
      const u = new URL(back);
      if (u.protocol !== 'http:' ||
          !['127.0.0.1', 'localhost'].includes(u.hostname)) return;
      sessionStorage.setItem(LOCAL_KEY, u.toString());
      const app = /&app=([0-9.]{1,20})(?:&|$)/.exec(location.hash);
      // Appka před 2.14.1 verzi neposílala — prázdná = starší.
      sessionStorage.setItem(APP_KEY, app ? app[1] : '');
    } catch (_) { /* bez sessionStorage cesta zpátky prostě nebude */ }
    history.replaceState(null, '', location.pathname + location.search);
  }

  function localBack() {
    try { return sessionStorage.getItem(LOCAL_KEY) || ''; } catch (_) { return ''; }
  }

  /* Umí appka na počítači, ze které okno přišlo, aspoň verzi `min`? */
  function appAtLeast(min) {
    let have = '';
    try { have = sessionStorage.getItem(APP_KEY) || ''; } catch (_) { return false; }
    const num = (v) => String(v).split('.').map((n) => parseInt(n, 10) || 0);
    const a = num(have), b = num(min);
    if (!have) return false;
    for (let i = 0; i < 3; i++) {
      if ((a[i] || 0) !== (b[i] || 0)) return (a[i] || 0) > (b[i] || 0);
    }
    return true;
  }

  /* Blok „nejdřív aktualizuj appku" pro nastavení v prostoru. */
  function oldAppNote() {
    const box = document.createElement('div');
    const note = document.createElement('div');
    note.className = 'set-warn';
    note.textContent = '! Aplikace Aladin na tomhle počítači je starší a tohle ještě neumí.';
    const how = document.createElement('div');
    how.className = 'set-note';
    how.textContent = 'Aktualizuj ji: tlačítkem se vrátíš na počítač, tam Nastavení → ' +
      'Aktualizace → Aktualizovat. Pak aplikaci zavři a otevři znovu.';
    const row = document.createElement('div');
    row.className = 'set-row';
    const b = document.createElement('button');
    b.className = 'btn primary';
    b.textContent = 'Na počítač aktualizovat aplikaci';
    b.onclick = () => backTo('local');
    row.appendChild(b);
    box.append(note, how, row);
    return box;
  }

  /* `mode`: 'local' = zůstat na počítači, 'logout' = odhlásit appku. */
  function backTo(mode) {
    const base = localBack();
    if (!base) return false;
    const u = new URL(base);
    if (mode) u.searchParams.set('mode', mode);
    global.HUB_LEAVING = true;
    location.href = u.toString();
    return true;
  }

  /* ── na počítači: přejít na server ───────────────────────────────────────── */
  async function go(io) {
    // Jednou, před prvním vstupem do prostoru: smí Claude ze serveru sahat
    // i na tenhle počítač? Bez odpovědi by o tom člověk nevěděl.
    const cfg = io.state && io.state.config;
    if (global.HubPocitac && cfg && !cfg.gateway_user && !cfg.pocitac_asked) {
      await global.HubPocitac.ask(io);
    }
    // Claude v prostoru bez klíče API a bez přihlášení: propojit ho s
    // předplatným rovnou tady, v prohlížeči stačí kliknout na Authorize.
    if (global.HubPredplatne && cfg && !cfg.gateway_user) {
      await global.HubPredplatne.ensure(io);
    }
    const res = await io.api('account', {action: 'connect'});
    if (!res.url) return res;                 // {error, kind}
    const here = new URL(location.href);
    here.hash = '';
    here.searchParams.delete('mode');
    // Odchod je záměr, ne zavírání okna: taby na počítači běží dál a nemá se
    // ptát „opravdu odejít?" ani ukládat postup (viz hub.js, beforeunload).
    global.HUB_LEAVING = true;
    const version = (io.state && io.state.version && io.state.version.version) || '';
    location.href = res.url + '#local=' + encodeURIComponent(here.toString()) +
      '&app=' + encodeURIComponent(version);
    return {ok: true};
  }

  /* ── adresa → ověřit → přihlásit ─────────────────────────────────────────── */
  /* opts: {server, email, onReady(status), autoProbe}
     Vrací element. Po úspěšném přihlášení zavolá onReady se stavem účtu. */
  function panel(io, opts) {
    opts = opts || {};
    const box = el('div', 'srv');

    const addrRow = el('div', 'srv-row');
    const addr = el('input', 'srv-input');
    addr.type = 'text';
    addr.placeholder = 'adresa serveru, např. hub.firma.cz';
    addr.autocomplete = 'url';
    addr.spellcheck = false;
    addr.value = opts.server ? hostOrUrl(opts.server) : '';
    const check = el('button', 'btn ghost', 'Ověřit');
    addrRow.appendChild(addr);
    addrRow.appendChild(check);
    box.appendChild(el('label', 'srv-label', 'Adresa serveru'));
    box.appendChild(el('div', 'srv-note',
      'Adresu ti pošle ten, kdo ti účet založil.'));
    box.appendChild(addrRow);

    const verdict = el('div', 'srv-status');
    verdict.hidden = true;
    box.appendChild(verdict);

    const creds = el('div', 'srv-creds');
    creds.hidden = true;
    const email = el('input', 'srv-input');
    email.type = 'email';
    email.autocomplete = 'username';
    email.placeholder = 'tvuj@email.cz';
    email.value = opts.email || '';
    const pass = el('input', 'srv-input');
    pass.type = 'password';
    pass.autocomplete = 'current-password';
    creds.appendChild(el('label', 'srv-label', 'E-mail'));
    creds.appendChild(email);
    creds.appendChild(el('label', 'srv-label', 'Heslo'));
    creds.appendChild(pass);
    const loginRow = el('div', 'srv-row srv-actions');
    const login = el('button', 'btn primary', 'Přihlásit se');
    // Passkey (otisk, obličej, PIN) jde jen v prohlížeči u adresy serveru —
    // appka ho tam otevře a počká, až se přihlásíš a potvrdíš.
    const browserLogin = el('button', 'btn ghost', 'Přihlásit v prohlížeči (passkey)');
    browserLogin.title = 'Otevře přihlášení serveru v prohlížeči — tam můžeš použít passkey. Pak se vrátíš sem.';
    loginRow.appendChild(login);
    loginRow.appendChild(browserLogin);
    creds.appendChild(loginRow);
    const loginErr = el('div', 'srv-status err');
    loginErr.hidden = true;
    creds.appendChild(loginErr);
    box.appendChild(creds);

    // Druhý krok přihlášení (kód z aplikace v mobilu).
    const second = el('div', 'srv-second');
    second.hidden = true;
    box.appendChild(second);

    let verified = '';          // adresa, která prošla ověřením

    function say(node, kind, text) {
      node.className = 'srv-status ' + kind;
      node.textContent = text;
      node.hidden = !text;
    }

    async function probe() {
      const value = addr.value.trim();
      if (!value) { say(verdict, 'err', 'Zadej adresu serveru.'); addr.focus(); return; }
      check.disabled = true;
      creds.hidden = true;
      say(verdict, 'busy', 'Ověřuji ' + value + '…');
      try {
        const res = await io.api('account', {action: 'probe', server: value});
        if (res.ok) {
          verified = res.server;
          say(verdict, 'ok', '✓ ' + res.host + ' je správná adresa, můžeš se přihlásit');
          creds.hidden = false;
          (email.value ? pass : email).focus();
        } else {
          verified = '';
          say(verdict, 'err', res.error || 'Server se nepodařilo ověřit.');
        }
      } catch (e) {
        say(verdict, 'err', friendly('probe', e));
      }
      check.disabled = false;
    }

    let cekani = 0;                // běžící čekání na přihlášení v prohlížeči
    browserLogin.onclick = async () => {
      if (!verified) return probe();
      const mine = ++cekani;
      browserLogin.disabled = true;
      say(loginErr, 'busy', 'Otevírám prohlížeč…');
      try {
        const st = await io.api('account', {action: 'browser-start', server: verified});
        if (st.error) { say(loginErr, 'err', st.error); browserLogin.disabled = false; return; }
        if (io.open) io.open(st.url); else window.open(st.url, '_blank');
        say(loginErr, 'busy', 'Přihlas se v prohlížeči (passkey, nebo heslo a kód) a potvrď. Čekám…');
        for (let i = 0; i < 300 && mine === cekani; i++) {      // asi 10 minut
          await new Promise((r) => setTimeout(r, 2000));
          if (mine !== cekani) return;
          const res = await io.api('account', {action: 'browser-poll', server: verified, n: st.n});
          if (res.error) { say(loginErr, 'err', res.error); break; }
          if (res.stav === 'hotovo') {
            say(loginErr, '', '');
            if (opts.onReady) await opts.onReady(res);
            browserLogin.disabled = false;
            return;
          }
        }
        if (mine === cekani) say(loginErr, 'err', 'Čekání vypršelo. Zkus to znovu.');
      } catch (e) {
        say(loginErr, 'err', friendly('browser', e));
      }
      browserLogin.disabled = false;
    };

    async function doLogin() {
      if (!verified) return probe();
      if (!email.value.trim() || !pass.value) {
        say(loginErr, 'err', 'Vyplň e-mail i heslo.');
        return;
      }
      login.disabled = true;
      login.textContent = 'Přihlašuji…';
      say(loginErr, '', '');
      try {
        const res = await io.api('account', {
          action: 'login', server: verified,
          email: email.value, password: pass.value,
        });
        pass.value = '';
        if (res.error) {
          say(loginErr, 'err', res.error);
        } else if (res.need) {
          showSecond(res);
        } else if (opts.onReady) {
          await opts.onReady(res);
        }
      } catch (e) {
        say(loginErr, 'err', friendly('login', e));
      }
      login.disabled = false;
      login.textContent = 'Přihlásit se';
    }

    /* Druhý krok: kód z aplikace v mobilu. Při prvním přihlášení se aplikace
       teprve nastavuje — QR kód nakreslil hub na počítači (hub/account.py). */
    function showSecond(res) {
      const setup = res.need === 'setup';
      creds.hidden = true;
      second.hidden = false;
      second.textContent = '';
      second.appendChild(el('div', 'srv-label', setup ? 'Zapni dvoufázové ověření' : 'Ověření'));
      second.appendChild(el('div', 'srv-note', setup
        ? 'Server teď chce kromě hesla i kód z telefonu. Nainstaluj si aplikaci na ' +
          'ověřovací kódy (Google Authenticator, Microsoft Authenticator…), přidej ' +
          'v ní účet a naskenuj QR kód:'
        : 'Opiš šesticiferný kód z aplikace v mobilu. Nemáš telefon? Zadej záložní kód.'));
      if (setup) {
        const pic = el('div', 'srv-qr');
        pic.innerHTML = res.qr || '';
        second.appendChild(pic);
        const key = el('div', 'srv-note');
        key.appendChild(document.createTextNode('Nejde skenovat? Ruční klíč: '));
        key.appendChild(el('code', 'srv-secret', res.secret_grouped || res.secret || ''));
        second.appendChild(key);
      }
      const code = el('input', 'srv-input');
      code.inputMode = 'numeric';
      code.autocomplete = 'one-time-code';
      code.placeholder = setup ? 'kód z aplikace' : 'kód z aplikace nebo záložní kód';
      second.appendChild(code);
      const row = el('div', 'srv-row srv-actions');
      const go = el('button', 'btn primary', setup ? 'Zapnout a přihlásit' : 'Ověřit');
      const back = el('button', 'btn ghost', 'Zpět');
      row.appendChild(go);
      row.appendChild(back);
      second.appendChild(row);
      const err = el('div', 'srv-status err');
      err.hidden = true;
      second.appendChild(err);

      back.onclick = () => {
        second.hidden = true;
        second.textContent = '';
        creds.hidden = false;
        pass.focus();
      };
      const submit = async () => {
        if (!code.value.trim()) { say(err, 'err', 'Opiš kód z aplikace.'); return; }
        go.disabled = true;
        try {
          const r = await io.api('account', {action: '2fa', server: res.server,
                                             ticket: res.ticket, code: code.value});
          if (r.error) {
            say(err, 'err', r.error);
            code.select();
            if (/Přihlas se znovu|vypršelo|zablokované/.test(r.error)) setTimeout(back.onclick, 1500);
          } else if (r.recovery && r.recovery.length) {
            showRecovery(r);
          } else if (opts.onReady) {
            await opts.onReady(r);
          }
        } catch (e) {
          say(err, 'err', friendly('2fa', e));
        }
        go.disabled = false;
      };
      go.onclick = submit;
      code.onkeydown = (ev) => { if (ev.key === 'Enter') submit(); };
      setTimeout(() => code.focus(), 0);
    }

    /* Záložní kódy po zapnutí ověřování — ukážou se jen jednou. */
    function showRecovery(r) {
      second.textContent = '';
      second.appendChild(el('div', 'srv-label', 'Záložní kódy'));
      second.appendChild(el('div', 'srv-note',
        'Když ztratíš telefon, přihlásíš se jedním z nich — každý platí jednou. ' +
        'Ulož si je (správce hesel, papír). Znovu se neukážou.'));
      const list = el('div', 'srv-codes');
      for (const c of r.recovery) list.appendChild(el('code', '', c));
      second.appendChild(list);
      const row = el('div', 'srv-row srv-actions');
      const done = el('button', 'btn primary', 'Uloženo, pokračovat');
      done.onclick = async () => {
        done.disabled = true;
        if (opts.onReady) await opts.onReady(r);
      };
      row.appendChild(done);
      second.appendChild(row);
    }

    check.onclick = probe;
    login.onclick = doLogin;
    // Změněná adresa už není ta ověřená — heslo se nesmí poslat jinam.
    addr.oninput = () => {
      if (verified && hostOrUrl(verified) !== addr.value.trim()) {
        verified = '';
        creds.hidden = true;
        say(verdict, '', '');
      }
    };
    addr.onkeydown = (ev) => { if (ev.key === 'Enter') probe(); };
    email.onkeydown = (ev) => { if (ev.key === 'Enter') pass.focus(); };
    pass.onkeydown = (ev) => { if (ev.key === 'Enter') doLogin(); };

    box.focusFirst = () => (addr.value ? check : addr).focus();
    // Známá adresa (vypršelé přihlášení, návrat k serveru) se ověří sama,
    // ať člověk rovnou píše heslo.
    if (opts.server && opts.autoProbe !== false) setTimeout(probe, 0);
    else setTimeout(() => addr.focus(), 0);
    return box;
  }

  /* https://host → host; cokoli s cestou nebo portem nechat, jak je. */
  function hostOrUrl(url) {
    try {
      const u = new URL(url);
      return u.protocol === 'https:' && u.pathname === '/' ? u.host : url;
    } catch (_) { return url || ''; }
  }

  /* ── při startu: na server se nedalo rovnou ──────────────────────────────── */
  /* opts: {onLocal(), note}. Zavře se sama, jen když člověk zvolí počítač —
     jinak z ní okno odchází na server. */
  function gate(io, opts) {
    opts = opts || {};
    const state = io.state || {};
    const cfg = state.config || {};
    const root = el('div', 'onb srv-gate');
    root.innerHTML = `
      <div class="onb-box">
        <div class="onb-head">
          <span class="onb-mark">
            <svg viewBox="0 0 24 24" width="34" height="34"><use href="#i-hub"/></svg>
          </span>
          <div>
            <div class="onb-title">Prostor na serveru</div>
            <div class="onb-sub"></div>
          </div>
        </div>
        <div class="onb-body"></div>
        <div class="onb-foot">
          <button class="btn ghost srv-local">Pracovat na tomto počítači</button>
          <span class="spacer"></span>
          <button class="btn primary srv-retry" hidden>Zkusit znovu</button>
        </div>
      </div>`;
    const sub = root.querySelector('.onb-sub');
    const body = root.querySelector('.onb-body');
    const retry = root.querySelector('.srv-retry');
    sub.textContent = cfg.gw_server ? hostOf(cfg.gw_server) : '';
    document.body.appendChild(root);

    root.querySelector('.srv-local').onclick = async () => {
      try { await io.api('account', {action: 'local'}); } catch (_) { /* jen volba */ }
      if (state.config) state.config.server_mode = false;
      root.remove();
      if (opts.onLocal) opts.onLocal();
    };

    function busy(text) {
      body.textContent = '';
      retry.hidden = true;
      body.appendChild(el('p', 'onb-lead', text));
    }

    function offline(note) {
      body.textContent = '';
      body.appendChild(el('div', 'srv-status err', '! Server teď neodpovídá'));
      body.appendChild(el('p', 'onb-lead', note ||
        'Spojení se serverem se nepodařilo navázat.'));
      body.appendChild(el('p', 'onb-note',
        'Na počítači můžeš pracovat hned. Až bude server zpátky, přepneš se ' +
        'v Nastavení → Účet.'));
      retry.hidden = false;
      retry.focus();
    }

    function login(status, note) {
      body.textContent = '';
      retry.hidden = true;
      if (note) body.appendChild(el('div', 'srv-status warn', note));
      body.appendChild(panel(io, {
        server: status.server || cfg.gw_server,
        email: (status.user && status.user.email) || cfg.gw_email,
        onReady: connect,
      }));
    }

    async function connect() {
      busy('Otevírám tvůj prostor…');
      const res = await go(io).catch((e) => ({error: friendly('go', e), kind: 'offline'}));
      if (res.ok) return;
      if (res.kind === 'auth') return login({}, res.error);
      offline(res.error);
    }

    async function run() {
      busy('Připojuji se k ' + (hostOf(cfg.gw_server) || 'serveru') + '…');
      let st;
      try {
        st = await io.api('account', {action: 'status', quick: true});
      } catch (e) {
        return offline(friendly('status', e));
      }
      if (!st.server) return login(st);
      if (st.offline) return offline(st.note);
      if (!st.logged_in) return login(st, opts.note || st.note);
      return connect();
    }

    retry.onclick = run;
    if (opts.loggedOut) login({}, opts.note); else run();
    return root;
  }

  global.HubServer = {captureLocal, localBack, backTo, go, panel, gate,
                      isAppWindow, hostOf, appAtLeast, oldAppNote, friendly};

})(window);
