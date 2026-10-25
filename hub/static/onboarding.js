/* První spuštění: co se dřív odpovídalo v instalačce, se teď nastavuje tady.
 *
 * Instalačka běží jednou, v terminálu, a kdo ji překlikal, neměl potom kde to
 * změnit. Průvodce v aplikaci to bere jinak: ptá se až na hotové appce, ukazuje
 * výsledek rovnou (motiv se přepne pod rukama) a dá se kdykoli spustit znovu.
 *
 * Zapisuje se přes /api/config a /api/vault, takže server si cesty přepočítá
 * hned — restart aplikace kvůli tomu potřeba není.
 */
'use strict';

(function (global) {

  let io = null;        // {api, setTheme, isDark, toast, pickFolder, reload}
  let state = null;     // payload z /api/state
  let step = 0;
  let chosen = {dirs: [], vault: '', backup: 'later', cloudPath: '', repo: 'claude-brain'};
  let root = null;

/* Kroky se liší podle režimu, který si člověk vybere hned na uvítanou.
 *
 * Základní režim nemá v nastavení Projekty, Paměť ani zálohu — ptát se na ně
 * v průvodci by znamenalo nastavit něco, k čemu se pak nedá vrátit. Zbydou
 * tedy tři kroky a hub naskočí na výchozích složkách; kdo je bude chtít jinak,
 * zapne si vývojářský režim a projde průvodce znovu. */
  const STEPS_ZAKLAD = ['vitej', 'vzhled', 'hotovo'];
  const STEPS_VYVOJ = ['vitej', 'vzhled', 'projekty', 'pamet', 'zaloha', 'hotovo'];
  /* Na serveru je všechno nastavené v prostoru, tady se jen přihlásí. Vzhled
     a zbytek průvodce patří hubu na serveru, ne tomuhle počítači. */
  const STEPS_SERVER = ['vitej', 'server'];

  function steps() {
    if (chosen.place === 'server') return STEPS_SERVER;
    return chosen.dev ? STEPS_VYVOJ : STEPS_ZAKLAD;
  }

  /* ── kostra ─────────────────────────────────────────────────────────────── */
  function el(tag, cls, text) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text != null) node.textContent = text;
    return node;
  }

  function mark(size) {
    // Značka aplikace — stejné tvary jako ikona, jen v currentColor.
    // Ikona Aladina (černý džin na bílém), ne jednobarevná značka.
    const img = document.createElement('img');
    img.className = 'onb-ikona';
    img.src = '/icon-192.png';
    img.alt = '';
    img.width = size;
    img.height = size;
    img.draggable = false;
    return img;
  }

  function open(opts) {
    io = opts;
    state = opts.state;
    step = 0;
    chosen.dirs = (state.config.project_dirs || []).slice();
    if (!chosen.dirs.length) chosen.dirs = (state.suggest_dirs || []).slice();
    chosen.vault = state.config.brain_dir || '';
    chosen.dev = !!(state.config && state.config.dev_mode);
    // Na hubu, který běží na serveru, se volba místa nenabízí — už tam jsi.
    chosen.onServer = !!(state.config && state.config.gateway_user) ||
      !HubServer.isAppWindow();
    chosen.place = (!chosen.onServer && opts.place) || 'pc';
    chosen.connected = false;
    chosen.checked = false;
    chosen.backup = state.vault_git.is_repo ? 'git' : 'later';
    build();
    render();
  }

  function build() {
    root = el('div', 'onb');
    root.innerHTML = `
      <div class="onb-box">
        <div class="onb-head">
          <span class="onb-mark"></span>
          <div>
            <div class="onb-title"></div>
            <div class="onb-sub"></div>
          </div>
          <div class="onb-dots"></div>
        </div>
        <div class="onb-body"></div>
        <div class="onb-foot">
          <button class="btn ghost onb-skip">Přeskočit</button>
          <span class="spacer"></span>
          <button class="btn ghost onb-back">Zpět</button>
          <button class="btn primary onb-next">Dál</button>
        </div>
      </div>`;
    root.querySelector('.onb-mark').appendChild(mark(34));
    root.querySelector('.onb-skip').onclick = () => finish(true);
    root.querySelector('.onb-back').onclick = () => { if (step > 0) { step--; render(); } };
    document.body.appendChild(root);
  }

  function render() {
    const box = root.querySelector('.onb-body');
    box.textContent = '';
    const dots = root.querySelector('.onb-dots');
    dots.textContent = '';
    steps().forEach((_, i) => {
      const d = el('i', 'onb-dot' + (i === step ? ' on' : (i < step ? ' done' : '')));
      dots.appendChild(d);
    });
    root.querySelector('.onb-back').style.visibility = step === 0 ? 'hidden' : '';
    root.querySelector('.onb-skip').style.visibility =
      step === steps().length - 1 ? 'hidden' : '';
    const next = root.querySelector('.onb-next');
    next.disabled = false;
    next.onclick = goNext;             // krok `server` si ho přebije svým
    next.textContent = step === steps().length - 1 ? 'Začít' : 'Dál';
    ({vitej, vzhled, projekty, pamet, zaloha, hotovo, server})[steps()[step]](box);
  }

  function head(title, sub) {
    root.querySelector('.onb-title').textContent = title;
    root.querySelector('.onb-sub').textContent = sub || '';
  }

  /* ── kroky ──────────────────────────────────────────────────────────────── */
  function vitej(box) {
    head('Aladin', 'Chvilka nastavení a pak už jen práce.');

    // Kde se bude pracovat — rozhoduje o celém zbytku, tak je to první otázka.
    if (!chosen.onServer) {
      box.appendChild(el('div', 'onb-lead', 'Kde chceš pracovat?'));
      const misto = el('div', 'onb-tiles');
      for (const [id, label, note] of [
        ['pc', 'Na tomto počítači',
         'Aplikace běží tady a pracuje s tvými soubory a poznámkami na tomhle počítači.'],
        ['server', 'Na serveru',
         'Přihlásíš se ke svému prostoru. Soubory, poznámky i napojené služby máš ' +
         'tam a dostaneš se k nim odkudkoli.'],
      ]) {
        const t = el('button', 'onb-tile' + (chosen.place === id ? ' on' : ''));
        t.appendChild(el('span', 'onb-tile-t', label));
        t.appendChild(el('span', 'onb-tile-s', note));
        t.onclick = () => { chosen.place = id; render(); };
        misto.appendChild(t);
      }
      box.appendChild(misto);
    }

    if (chosen.place === 'server') {
      box.appendChild(el('div', 'onb-note',
        'Na dalším kroku zadáš adresu serveru a přihlásíš se. Adresu, e-mail ' +
        'a heslo ti dá ten, kdo server spravuje.'));
      return;
    }
    if (!chosen.onServer) box.appendChild(el('div', 'onb-gap'));

    // Věta se musí trefit do počtu kroků, které pak přijdou — jinak průvodce
    // slíbí nastavení projektů a paměti a hned skončí.
    const uvod = el('p', 'onb-lead');
    uvod.textContent = chosen.dev
        ? 'Každý projekt má vlastní okno s Claudem. Teď si nastavíme vzhled, kde máš projekty a kde jsou poznámky.'
        : 'Vítej! Stačí vybrat, jak má aplikace vypadat, a můžeš začít psát s Claudem.';
    box.appendChild(uvod);

    // Vývojářský režim se v průvodci nevybírá — zapíná se jen v Nastavení →
    // Ostatní. Průvodce zůstává jednoduchý pro každého.

    /* Kontrola součástí. V běžném režimu se ukazuje jen to, co chybí —
       seznam samých fajfek člověku nic neřekne. Obsidian je volitelný,
       takže jeho absence se nezvýrazňuje jako chyba. */
    const checks = [
      [!!state.doctor.bash, 'Součást pro spouštění příkazů',
       'Bez ní se rozhovor s Claudem neotevře.'],
      [!!state.doctor.claude, 'Claude', 'Bez něj se nedá začít.'],
      [!!state.doctor.clipboard, 'Kopírování a vkládání',
       'Kopírovat půjde jen ručně.'],
      [!!state.obsidian || !chosen.dev, 'Obsidian (volitelné)',
       'Poznámky půjdou otevřít i bez něj.'],
    ];
    const missing = checks.filter(c => !c[0]);
    const list = el('ul', 'onb-check');
    if (!chosen.dev && !missing.length) {
      list.appendChild(el('li', 'ok', 'Všechno potřebné je připravené.'));
    }
    for (const [ok, text, why] of (chosen.dev ? checks : missing)) {
      list.appendChild(el('li', ok ? 'ok' : 'miss', ok ? text : text + ' chybí. ' + why));
    }
    box.appendChild(list);
    if (missing.length) {
      box.appendChild(el('div', 'onb-note',
        'Když něco chybí, pomůže ten, kdo ti aplikaci instaloval.'));
    }
  }

  /* Přihlášení na server. „Dál" se změní na „Otevřít můj prostor" a pustí,
     až když přihlášení prošlo — dřív není kam jít. */
  function server(box) {
    head('Přihlášení na server', 'Tvůj prostor, tvoje paměť, odkudkoli.');
    const next = root.querySelector('.onb-next');
    const skip = root.querySelector('.onb-skip');
    skip.style.visibility = 'hidden';
    next.textContent = 'Otevřít můj prostor';
    next.disabled = !chosen.connected;

    const err = el('div', 'srv-status err');
    err.hidden = true;

    function ready(status) {
      chosen.connected = true;
      box.textContent = '';
      const u = status.user || {};
      box.appendChild(el('div', 'srv-status ok',
        '✓ Přihlášeno jako ' + (u.name || u.email || '') +
        ' na ' + (status.host || HubServer.hostOf(status.server))));
      box.appendChild(el('p', 'onb-lead',
        'Aplikace si to pamatuje: příště se otevře rovnou ve tvém prostoru. ' +
        'Zpátky na počítač se přepneš v Nastavení → Účet.'));
      const other = el('button', 'btn ghost', 'Přihlásit se jiným účtem');
      other.onclick = () => { chosen.connected = false; render(); };
      box.appendChild(other);
      box.appendChild(err);
      next.disabled = false;
      next.focus();
    }

    // Klik na „Dál" v posledním kroku by průvodce zavřel; tady má okno odejít.
    next.onclick = async () => {
      if (!chosen.connected) return;
      next.disabled = true;
      next.textContent = 'Otevírám…';
      const res = await HubServer.go(io).catch((e) => ({error: HubServer.friendly('onboarding go', e)}));
      if (res.ok) return;
      err.textContent = res.error || 'Prostor se nepodařilo otevřít.';
      err.hidden = false;
      next.disabled = false;
      next.textContent = 'Otevřít můj prostor';
      if (res.kind === 'auth') { chosen.connected = false; render(); }
    };

    const cfg = state.config || {};
    if (cfg.gw_logged_in && !chosen.connected && !chosen.checked) {
      // Na serveru už přihlášený (třeba se vrátil ze serveru) — neptat se znovu.
      chosen.checked = true;
      box.appendChild(el('p', 'onb-lead', 'Zjišťuji přihlášení…'));
      io.api('account', {action: 'status', quick: true}).then((st) => {
        if (steps()[step] !== 'server') return;
        if (st.logged_in && !st.offline) ready(st); else render();
      }, () => render());
      return;
    }
    if (chosen.connected) {
      io.api('account', {action: 'status', quick: true}).then(ready, () => {});
      return;
    }
    box.appendChild(HubServer.panel(io, {
      server: cfg.gw_server,
      email: cfg.gw_email,
      onReady: ready,
    }));
  }

  function vzhled(box) {
    head('Vzhled', 'Přepne se hned, ať je vidět, do čeho jdeš.');
    const wrap = el('div', 'onb-tiles');
    const opts = [
      ['dark', 'Tmavý', true],
      ['light', 'Světlý', false],
      ['system', 'Podle systému', null],
    ];
    const current = localStorage.getItem('hub-theme') || 'system';
    for (const [key, label, dark] of opts) {
      const t = el('button', 'onb-tile' + (current === key ? ' on' : ''));
      t.appendChild(el('span', 'onb-swatch ' + key));
      t.appendChild(el('span', null, label));
      t.onclick = () => {
        if (key === 'system') {
          localStorage.removeItem('hub-theme');
          io.setTheme(!matchMedia('(prefers-color-scheme: light)').matches, false);
        } else {
          io.setTheme(dark, true);
        }
        render();
      };
      wrap.appendChild(t);
    }
    box.appendChild(wrap);
  }

  function projekty(box) {
    head('Projekty', 'Složky, ve kterých se hledají projekty do panelu.');
    const list = el('div', 'onb-list');
    const all = [...new Set([...chosen.dirs, ...(state.suggest_dirs || [])])];
    if (!all.length) {
      list.appendChild(el('div', 'empty', '(zatím žádná — přidej složku níž)'));
    }
    for (const dir of all) {
      const row = el('label', 'onb-row');
      const cb = el('input');
      cb.type = 'checkbox';
      cb.checked = chosen.dirs.includes(dir);
      cb.onchange = () => {
        chosen.dirs = cb.checked ? [...new Set([...chosen.dirs, dir])]
                                 : chosen.dirs.filter(d => d !== dir);
      };
      row.appendChild(cb);
      row.appendChild(el('span', null, dir));
      list.appendChild(row);
    }
    box.appendChild(list);
    const add = el('button', 'actionbtn', '＋ Přidat složku…');
    add.onclick = async () => {
      const picked = await io.pickFolder();
      if (picked) { chosen.dirs = [...new Set([...chosen.dirs, picked])]; render(); }
    };
    box.appendChild(add);
  }

  function pamet(box) {
    head('Paměť', 'Poznámky, které si Claude nese mezi rozhovory.');
    box.appendChild(el('p', 'onb-lead',
      'Paměť je obyčejná složka s poznámkami, dá se otevřít v Obsidianu. ' +
      'Claude si do ní sám ukládá, na čem se pracovalo — klikat se na nic nemusí.'));

    // Napojit existující vault je nejčastější případ: kdo Obsidian používá,
    // už poznámky někde má a nechce začínat znovu.
    const vaults = state.vaults || [];
    if (vaults.length) {
      box.appendChild(el('div', 'set-title', 'Našel jsem tyhle složky s poznámkami (Obsidian)'));
      const list = el('div', 'onb-list');
      for (const v of vaults) {
        const row = el('label', 'onb-row');
        const rb = el('input');
        rb.type = 'radio';
        rb.name = 'vault';
        rb.checked = chosen.vault === v.path;
        rb.onchange = () => { chosen.vault = v.path; renderPath(); };
        row.appendChild(rb);
        const col = el('span', 'onb-col');
        col.appendChild(el('span', null, v.name));
        col.appendChild(el('small', null, v.path + ' · ' +
          (v.has_memory ? v.notes + ' poznámek' : 'zatím bez paměti — založí se')));
        row.appendChild(col);
        list.appendChild(row);
      }
      box.appendChild(list);
    }

    const path = el('div', 'onb-path');
    path.id = 'onb-vault-path';
    path.textContent = chosen.vault || '(nenastaveno)';
    box.appendChild(path);

    const row = el('div', 'onb-btns');
    const pick = el('button', 'actionbtn', 'Vybrat jinou složku…');
    pick.onclick = async () => {
      const picked = await io.pickFolder();
      if (picked) { chosen.vault = picked; render(); }
    };
    const clone = el('button', 'actionbtn', 'Stáhnout z GitHubu…');
    clone.onclick = async () => {
      const repo = await HubDialog.prompt('Adresa úložiště na GitHubu (např. jmeno/pamet nebo celý odkaz):', '', {title: 'Stáhnout z GitHubu', ok: 'Stáhnout'});
      if (!repo) return;
      try {
        const r = await io.api('vault', {action: 'clone', repo: repo.trim()});
        chosen.vault = r.path;
        io.toast('Paměť stažena do ' + r.path);
        await io.refreshState();
        state = io.state;
        render();
      } catch (err) { io.toast(HubServer.friendly('clone', err)); }
    };
    const make = el('button', 'actionbtn', 'Založit novou');
    make.onclick = () => {
      chosen.vault = (state.home || '~') + '/Obsidian/Claude-Brain';
      render();
    };
    row.appendChild(pick);
    row.appendChild(clone);
    row.appendChild(make);
    box.appendChild(row);
  }

  function renderPath() {
    const node = document.getElementById('onb-vault-path');
    if (node) node.textContent = chosen.vault || '(nenastaveno)';
  }

  function zaloha(box) {
    head('Záloha paměti', 'Aby poznámky nežily jen na jednom disku.');
    const cloud = state.cloud || [];
    const opts = [];
    // Tenhle krok se ukazuje jen ve vývojářském režimu, takže GitHub tu může
    // být bez další podmínky — kdo si režim vybral, o `gh` stojí.
    opts.push({
      key: 'git', label: 'Do soukromého úložiště na GitHubu',
      note: state.doctor.git && state.vault_git.is_repo
        ? 'Poznámky už na GitHubu jsou — jen se zapne automatické posílání.'
        : 'Založí soukromé úložiště a po každém rozhovoru tam pošle změny.',
      disabled: !state.doctor.git,
    });
    for (const c of cloud) {
      opts.push({key: 'cloud:' + c.path, label: 'Přesunout do ' + c.name,
                 note: c.path});
    }
    opts.push({
      key: 'folder', label: 'Přesunout do vlastní složky…',
      note: cloud.length ? 'Když máš cloud jinde.'
                         : 'Žádnou cloudovou aplikaci (OneDrive, Dropbox…) jsem tu nenašel — ' +
                           'když si ho doinstaluješ, ukaž sem na jeho složku.',
    });
    opts.push({key: 'later', label: 'Zatím ne', note: 'Dá se zapnout kdykoli později.'});

    const list = el('div', 'onb-list');
    for (const o of opts) {
      const row = el('label', 'onb-row' + (o.disabled ? ' off' : ''));
      const rb = el('input');
      rb.type = 'radio';
      rb.name = 'zaloha';
      rb.disabled = !!o.disabled;
      rb.checked = chosen.backup === o.key ||
                   (o.key.startsWith('cloud:') && chosen.backup === 'cloud' &&
                    chosen.cloudPath === o.key.slice(6));
      rb.onchange = () => {
        if (o.key.startsWith('cloud:')) {
          chosen.backup = 'cloud';
          chosen.cloudPath = o.key.slice(6);
        } else {
          chosen.backup = o.key;
        }
      };
      row.appendChild(rb);
      const col = el('span', 'onb-col');
      col.appendChild(el('span', null, o.label));
      col.appendChild(el('small', null, o.note));
      row.appendChild(col);
      list.appendChild(row);
    }
    box.appendChild(list);
  }

  function hotovo(box) {
    head('Hotovo', 'Můžeme začít.');
    const list = el('ul', 'onb-check');
    list.appendChild(el('li', 'ok',
      'režim: ' + (chosen.dev ? 'pro programátory' : 'běžné používání')));
    if (chosen.dev) {
      list.appendChild(el('li', 'ok', 'projekty: ' +
        (chosen.dirs.length ? chosen.dirs.join(', ') : '(žádné)')));
      list.appendChild(el('li', chosen.vault ? 'ok' : 'miss',
        'paměť: ' + (chosen.vault || 'vypnutá')));
      const zal = {git: 'soukromé úložiště na GitHubu', cloud: chosen.cloudPath,
                   folder: 'vlastní složka', later: 'zatím ne'}[chosen.backup];
      list.appendChild(el('li', chosen.backup === 'later' ? 'miss' : 'ok',
        'záloha paměti: ' + zal));
    } else {
      // Nic z toho se v základním režimu neptalo — ať je vidět, co platí.
      list.appendChild(el('li', 'ok', 'projekty a paměť: výchozí nastavení'));
    }
    box.appendChild(list);
    box.appendChild(el('p', 'onb-lead',
      'Změnit se to dá kdykoli v Nastavení (⚙ nahoře).'));
  }

  /* ── posun ──────────────────────────────────────────────────────────────── */
  async function goNext() {
    const btn = root.querySelector('.onb-next');
    btn.disabled = true;
    const at = step;
    try {
      if (steps()[step] === 'projekty') {
        await io.api('config', {project_dirs: chosen.dirs});
      } else if (steps()[step] === 'pamet' && chosen.vault) {
        // Existující složku jen napojíme; create by jinak přepsal rozcestník.
        const known = (state.vaults || []).some(v => v.path === chosen.vault);
        await io.api('vault',
          {action: known ? 'use' : 'create', path: chosen.vault});
      } else if (steps()[step] === 'zaloha') {
        // Nepovedenou zálohu nepřeskakujeme — ať je vidět, co se stalo.
        if (!await applyBackup()) return;
      } else if (steps()[step] === 'hotovo') {
        await finish(false);
        return;
      }
      step++;
      render();
    } catch (err) {
      io.toast(HubServer.friendly('onboarding', err));
    } finally {
      // Po posunu tlačítko nastavil už render() podle nového kroku — krok
      // `server` ho drží vypnuté, dokud se nepřihlásí.
      if (step === at && root) btn.disabled = false;
    }
  }

  /* Záloha běží na serveru na pozadí a stav se odečítá — dřív to byl jeden
     dlouhý požadavek a průvodce na něm zůstal viset. */
  function backupStatus(text, kind) {
    let node = root.querySelector('.onb-job');
    if (!node) {
      node = el('div', 'set-status onb-job');
      root.querySelector('.onb-body').appendChild(node);
    }
    node.className = 'set-status onb-job ' + (kind || 'busy');
    node.textContent = text;
  }

  async function waitForVault() {
    for (;;) {
      await new Promise(r => setTimeout(r, 900));
      let st;
      try {
        st = await io.api('job?name=vault');
      } catch (err) {
        console.warn('onboarding: job', err);
        backupStatus('Spojení se přerušilo. Zkus to prosím znovu.', 'warn');
        return false;
      }
      if (st.running) {
        backupStatus('Zálohuju… ' + (st.step || ''), 'busy');
        continue;
      }
      const r = st.result || {};
      if (!r.ok) {
        backupStatus(r.detail || 'Zálohu se nepovedlo nastavit.', 'warn');
        return false;
      }
      if (r.path) chosen.vault = r.path;
      backupStatus('✓ ' + (r.path ? 'Paměť přesunuta do ' + r.path
                                  : 'Paměť je v soukromém úložišti na GitHubu.'), 'ok');
      await io.refreshState();
      state = io.state;
      return true;
    }
  }

  async function applyBackup() {
    if (chosen.backup === 'later') return true;
    let payload = null;
    if (chosen.backup === 'git') {
      payload = {action: 'git', repo: chosen.repo};
    } else if (chosen.backup === 'cloud' && chosen.cloudPath) {
      payload = {action: 'move', path: chosen.cloudPath};
    } else if (chosen.backup === 'folder') {
      const picked = await io.pickFolder();
      if (!picked) return true;          // rozmyslel si to, jdeme dál
      payload = {action: 'move', path: picked};
    }
    if (!payload) return true;
    backupStatus('Zálohuju…', 'busy');
    try {
      await io.api('vault', payload);
    } catch (err) {
      console.warn('onboarding: backup', err);
      backupStatus('Zálohu se nepodařilo spustit. Zkus to prosím znovu.', 'warn');
      return false;
    }
    return waitForVault();
  }

  async function finish(skipped) {
    try {
      await io.api('config', {onboarded: true, dev_mode: !!chosen.dev});
    } catch (_) { /* nastavení se nepovedlo uložit — průvodce přesto zavíráme */ }
    close();
    if (!skipped) io.toast('Nastaveno. Ať to jde od ruky.');
    io.reload();
  }

  function close() {
    if (root) root.remove();
    root = null;
  }

  global.HubOnboarding = {open, close};

})(window);
