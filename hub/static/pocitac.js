/* Claude ze serveru na tomhle počítači (hub/pocitac.py, gateway/pocitac.py).
 *
 * Claude v prostoru na serveru může sahat i na počítač, ze kterého se k prostoru
 * přihlásilo — když to člověk na tom počítači povolí. Tenhle modul je všechno,
 * co z toho je vidět:
 *
 *  - `panel(io)`       na počítači v Nastavení → Účet: volba přístupu (vypnuto /
 *                      jen čtení / plný), stav spojení a co Claude naposledy dělal.
 *  - `ask(io, opts)`   karta s touž volbou. Ukáže se jednou, před prvním
 *                      přechodem do prostoru (server.js, go), a když se okno
 *                      ze serveru vrátí s `?mode=pocitac`.
 *  - `serverBlock(io)` v prostoru v Nastavení → Účet: které počítače jsou
 *                      připojené a cesta, jak to zapnout.
 *  - `chip(io)`        štítek v hlavičce prostoru, dokud je nějaký počítač
 *                      připojený. Hlídá i úkoly na později: jakmile si je
 *                      počítač převezme, ukáže to kartou (`ukolyNotice`).
 *  - `ukol(io, msg)`   na počítači: hláška od hubu o úkolu ze serveru (převzatý
 *                      čeká na potvrzení → karta, spuštěný → hláška).
 *  - `pending(io)`     na počítači po načtení: čekající úkoly ze serveru.
 *  - karty souhlasu (`showApproval`): v prostoru i žádosti z tabu s úkolem ze
 *                      serveru — Povolit / Zamítnout, u otázky výběr odpovědi.
 *
 * Volba přístupu se ukládá jen na počítači (POST /api/pocitac). Prostor na
 * serveru ji změnit neumí — tlačítko tam jen vrátí okno sem.
 */
'use strict';

(function (global) {

  const LEVELS = [
    ['', 'Vypnuto', 'Claude ze serveru na tenhle počítač nesahá.'],
    ['cteni', 'Jen čtení',
     'Prochází složky, čte a hledá soubory a kopíruje je na server. Nic nezmění ani nespustí.'],
    ['ptat', 'Ptát se na souhlas (doporučeno)',
     'Čte sám. Zápis, spuštění programu, snímek obrazovky, schránku a otevírání souborů provede ' +
     'až po tvém kliknutí na Povolit — v aplikaci na počítači nebo tady ze serveru.'],
    ['vse', 'Plný přístup',
     'Navíc zapisuje a upravuje soubory a spouští programy bez ptaní — jako Claude spuštěný tady.'],
  ];

  const OPS = {info: 'zjistil údaje', ls: 'prošel složku', read: 'četl', find: 'hledal v',
               download: 'stáhl na server', write: 'zapsal', edit: 'upravil',
               upload: 'nahrál', run: 'spustil', ps: 'zjistil procesy', notify: 'oznámil',
               screenshot: 'udělal snímek obrazovky', clipboard_get: 'přečetl schránku',
               clipboard_set: 'zapsal do schránky', open: 'otevřel'};
  const OP_ASK = {run: 'spustit příkaz', write: 'zapsat soubor', edit: 'upravit soubor',
                  upload: 'nahrát soubor na počítač', open: 'otevřít na počítači',
                  screenshot: 'udělat snímek obrazovky', clipboard_get: 'přečíst schránku',
                  clipboard_set: 'zapsat do schránky'};

  function el(tag, cls, text) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text != null) node.textContent = text;
    return node;
  }

  function icon(name) {
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('class', 'ico');
    const use = document.createElementNS('http://www.w3.org/2000/svg', 'use');
    use.setAttribute('href', '#' + name);
    svg.appendChild(use);
    return svg;
  }

  // Technický detail chyby jde do konzole, člověk dostane lidskou větu.
  // Věta od serveru (česky) projde; výpadek spojení nebo syrový výpis
  // (Failed to fetch, HTTP 502, Traceback…) se nahradí lidskou větou.
  function friendly(where, e) {
    console.warn('pocitac.js: ' + where, e);
    const msg = (e && e.message) || '';
    if (!msg || e instanceof TypeError || msg.length > 200 ||
        /^HTTP \d|fetch|NetworkError|Traceback|Error:|<html/i.test(msg)) {
      return 'Server teď neodpovídá, zkus to za chvíli.';
    }
    return msg;
  }

  function ago(seconds) {
    const s = Math.max(0, Math.round(seconds));
    if (s < 45) return 'právě teď';
    if (s < 3600) return 'před ' + Math.round(s / 60) + ' min';
    if (s < 86400) return 'před ' + Math.round(s / 3600) + ' h';
    return 'před ' + Math.round(s / 86400) + ' dny';
  }

  function levelLabel(level) {
    return (LEVELS.find(([id]) => id === level) || LEVELS[0])[1];
  }

  /* Tři dlaždice. `onPick(level)` dostane volbu; vrací element s .set(level). */
  function tiles(current, onPick) {
    const wrap = el('div', 'onb-tiles pc-tiles');
    const buttons = [];
    for (const [id, title, text] of LEVELS) {
      const b = el('button', 'onb-tile');
      b.type = 'button';
      b.dataset.level = id;
      b.appendChild(el('span', 'onb-tile-t', title));
      b.appendChild(el('span', 'onb-tile-s', text));
      b.onclick = () => onPick(id);
      buttons.push(b);
      wrap.appendChild(b);
    }
    wrap.set = (level) => {
      for (const b of buttons) b.classList.toggle('on', b.dataset.level === level);
    };
    wrap.set(current);
    return wrap;
  }

  function stamp(ts) {
    if (!ts) return '';
    const d = new Date(ts * 1000);
    return d.getDate() + '. ' + (d.getMonth() + 1) + '. ' +
      d.getHours() + ':' + String(d.getMinutes()).padStart(2, '0');
  }

  const TRUST_NOTE =
    'Platí jen pro tvůj prostor a jen dokud je tady aplikace otevřená. Každý přístup se ' +
    'zapíše do přehledu a posledních pár uvidíš v Nastavení → Účet. Na počítač tak ' +
    'dosáhne i ten, kdo spravuje server — zapínej jen u serveru, kterému věříš.';


  /* ── souhlas s úkolem počítače (úroveň „ptát se“) ─────────────────────────
     Karta vpravo dole, stejná na počítači i v prostoru na serveru. `decide(id,
     volba)` pošle rozhodnutí tam, kam patří: v appce na počítači do hubu, v
     prostoru přes bránu (cookie — Claude v prostoru ji nemá, nemůže si
     souhlas dát sám). Karta zmizí sama, když žádost mezitím vyřídil kdo jiný
     nebo vypršela (`stillPending`). */
  const askCards = new Map();

  function askHost() {
    let host = document.querySelector('.pc-asks');
    if (!host) {
      host = el('div', 'pc-asks');
      document.body.appendChild(host);
    }
    return host;
  }

  /* Žádost z tabu s úkolem ze serveru (hub/pocitac.py, povoleni_*): Claude
     Code na počítači chce souhlas s nástrojem, ptá se, nebo chce schválit plán.
     Čeká, dokud neodpoví člověk tady nebo v terminálu na počítači — platí
     první odpověď, druhá karta pak zmizí sama (pruneApprovals). */
  const TOOL_ASK = {Bash: 'spustit příkaz', Write: 'zapsat soubor', Edit: 'upravit soubor',
                    MultiEdit: 'upravit soubor', NotebookEdit: 'upravit notebook',
                    WebFetch: 'otevřít web', WebSearch: 'hledat na webu'};

  function sessionCard(io, req, decide, where) {
    const card = el('div', 'pc-ask-card pc-ask-session');
    const head = el('div', 'pc-ask-head');
    head.appendChild(icon('i-laptop'));
    const title = req.op === 'otazka' ? 'Claude se ptá'
      : req.op === 'plan' ? 'Claude chce schválit plán'
      : 'Claude chce ' + (TOOL_ASK[req.tool] || ('použít nástroj ' + (req.tool || '')));
    head.appendChild(el('b', null, title));
    if (where) head.appendChild(el('span', 'set-dim', ' · ' + where));
    card.appendChild(head);
    card.appendChild(el('div', 'pc-ask-sub',
      'Úkol „' + (req.ukol || 'ze serveru') + '“' + (req.tab ? ' · tab ' + req.tab : '')));
    const what = req.op === 'otazka' ? '' : (req.preview || req.summary || '');
    if (what) card.appendChild(el('pre', 'pc-ask-what' + (req.op === 'plan' ? ' plan' : ''), what));

    const btns = el('div', 'pc-ask-btns');
    const lock = (on) => card.querySelectorAll('button, input').forEach((x) => { x.disabled = on; });
    const send = async (volba, odpovedi) => {
      lock(true);
      try {
        await decide(req.id, volba, odpovedi);
        io.toast(volba === 'zamitnout' ? 'Zamítnuto — Claude na počítači to dostane.'
                                        : 'Odesláno — Claude na počítači pokračuje.');
        closeApproval(req.id);
      } catch (e) {
        io.toast('Nepovedlo se: ' + friendly('rozhodnout', e));
        lock(false);
      }
    };

    if (req.op === 'otazka' && (req.otazky || []).length) {
      const picks = new Map();          // otázka -> Set vybraných voleb
      const other = new Map();          // otázka -> vlastní odpověď
      for (const q of req.otazky) {
        const box = el('div', 'pc-ask-q');
        box.appendChild(el('div', 'pc-ask-qt', q.question));
        if (q.multi) box.appendChild(el('div', 'set-dim', 'Můžeš vybrat víc možností.'));
        const chosen = new Set();
        picks.set(q.question, chosen);
        const opts = el('div', 'pc-ask-opts');
        for (const o of q.options || []) {
          const b = el('button', 'pc-ask-opt');
          b.type = 'button';
          b.appendChild(el('span', 'pc-ask-ol', o.label));
          if (o.description) b.appendChild(el('span', 'pc-ask-od', o.description));
          b.onclick = () => {
            if (!q.multi) {
              chosen.clear();
              opts.querySelectorAll('.pc-ask-opt').forEach((x) => x.classList.remove('on'));
              other.get(q.question).value = '';
            }
            if (chosen.has(o.label)) chosen.delete(o.label); else chosen.add(o.label);
            b.classList.toggle('on', chosen.has(o.label));
          };
          opts.appendChild(b);
        }
        box.appendChild(opts);
        const own = el('input', 'pc-ask-own');
        own.type = 'text';
        own.maxLength = 500;
        own.placeholder = 'Jiná odpověď…';
        own.oninput = () => {
          if (own.value.trim() && !q.multi) {
            chosen.clear();
            opts.querySelectorAll('.pc-ask-opt').forEach((x) => x.classList.remove('on'));
          }
        };
        other.set(q.question, own);
        box.appendChild(own);
        card.appendChild(box);
      }
      const ok = el('button', 'btn primary', 'Odeslat odpověď');
      ok.onclick = () => {
        const odpovedi = {};
        for (const q of req.otazky) {
          const own = other.get(q.question).value.trim();
          const list = [...picks.get(q.question)];
          if (own) list.push(own);
          if (!list.length) {
            io.toast('Vyber odpověď na: ' + q.question);
            return;
          }
          odpovedi[q.question] = q.multi ? list : list[list.length - 1];
        }
        send('odpoved', odpovedi);
      };
      const no = el('button', 'btn ghost', 'Neodpovídat');
      no.onclick = () => send('zamitnout');
      btns.append(ok, no);
    } else {
      const ok = el('button', 'btn primary', req.op === 'plan' ? 'Schválit plán' : 'Povolit');
      ok.onclick = () => send('povolit');
      const no = el('button', 'btn ghost', 'Zamítnout');
      no.onclick = () => send('zamitnout');
      btns.append(ok, no);
    }
    card.appendChild(el('div', 'pc-ask-note',
      'Čeká, dokud neodpovíš — tady, nebo v terminálu na počítači. Platí první odpověď.'));
    card.appendChild(btns);
    return card;
  }

  function showApproval(io, req, decide, where) {
    if (askCards.has(req.id)) return;
    if (req.kind === 'session') {
      const card = sessionCard(io, req, decide, where);
      askHost().appendChild(card);
      askCards.set(req.id, card);
      return;
    }
    const card = el('div', 'pc-ask-card');
    const head = el('div', 'pc-ask-head');
    head.appendChild(icon('i-laptop'));
    head.appendChild(el('b', null, 'Claude chce ' + (OP_ASK[req.op] || req.op)));
    if (where) head.appendChild(el('span', 'set-dim', ' · ' + where));
    card.appendChild(head);
    const what = req.preview || req.summary || '';
    if (what) card.appendChild(el('pre', 'pc-ask-what', what));
    const wait = Math.max(0, 120 - (Date.now() / 1000 - (req.at || Date.now() / 1000)));
    card.appendChild(el('div', 'pc-ask-note',
      'Nepovolíš-li do ' + Math.round(wait) + ' s, Claude to bude brát jako ne. Povolení na 30 minut ' +
      'platí pro všechny takové úkoly najednou.'));
    const btns = el('div', 'pc-ask-btns');
    const mk = (cls, text, volba) => {
      const b = el('button', 'btn ' + cls, text);
      b.onclick = async () => {
        btns.querySelectorAll('button').forEach((x) => { x.disabled = true; });
        try {
          await decide(req.id, volba);
        } catch (e) {
          io.toast('Nepovedlo se: ' + friendly('rozhodnout', e));
        }
        closeApproval(req.id);
      };
      return b;
    };
    btns.append(mk('primary', 'Povolit jednou', 'jednou'), mk('ghost', 'Povolit na 30 minut', '30min'),
                mk('ghost', 'Zamítnout', 'zamitnout'));
    card.appendChild(btns);
    askHost().appendChild(card);
    askCards.set(req.id, card);
  }

  function closeApproval(id) {
    const c = askCards.get(id);
    if (c) c.remove();
    askCards.delete(id);
    const host = document.querySelector('.pc-asks');
    if (host && !host.children.length) host.remove();
  }

  // Na počítači: žádost z tabu má dialog přímo v tabu, karta by byla navíc.
  function localPending(list) {
    return (list || []).filter((p) => p.kind !== 'session');
  }

  // Karty žádostí, které už nikdo nečeká, zmizí.
  function pruneApprovals(list) {
    const live = new Set((list || []).map((p) => p.id));
    for (const id of [...askCards.keys()]) if (!live.has(id)) closeApproval(id);
  }

  /* Na počítači: hub pošle zprávu `pocitac-schvaleni`; stav se pak hlídá, dokud
     karta visí (rozhodnutí z prostoru ji zavře). */
  function approvalFromHub(io, msg) {
    const decide = async (id, volba) => {
      const r = await io.api('pocitac', {action: 'rozhodnout', id, volba});
      if (r && r.error) throw new Error(r.error);
    };
    showApproval(io, {id: msg.id, op: msg.op, summary: msg.summary, at: Date.now() / 1000}, decide, 'z prostoru na serveru');
    (async function watch() {
      while (askCards.has(msg.id)) {
        await new Promise((r) => setTimeout(r, 2000));
        let st = null;
        try { st = await io.api('pocitac'); } catch (_) { continue; }
        const mine = (st.pending || []).find((p) => p.id === msg.id);
        if (mine && askCards.get(msg.id)) {
          const pre = askCards.get(msg.id).querySelector('.pc-ask-what');
          if (!pre && mine.preview) {
            const node = el('pre', 'pc-ask-what', mine.preview);
            askCards.get(msg.id).querySelector('.pc-ask-head').after(node);
          }
        }
        if (!mine) closeApproval(msg.id);
      }
    })();
  }

  /* Na počítači: žádost, která přišla dřív, než se okno otevřelo (nebo když zpráva
     o ní nedoletěla), se najde dotazem na stav. Hlídá se jen při stupni „ptát se“. */
  let watching = false;
  function watchPending(io) {
    if (watching) return;
    watching = true;
    const decide = async (id, volba) => {
      const r = await io.api('pocitac', {action: 'rozhodnout', id, volba});
      if (r && r.error) throw new Error(r.error);
    };
    (async function tick() {
      let wait = 6000;
      try {
        if (!document.hidden) {
          const st = await io.api('pocitac');
          const mine = localPending(st.pending);
          if (st.access === 'ptat' || mine.length) {
            for (const p of mine) showApproval(io, p, decide, 'z prostoru na serveru');
            pruneApprovals(mine);
            if (mine.length) wait = 2000;
          } else {
            wait = 15000;
          }
        }
      } catch (_) { wait = 15000; }
      setTimeout(tick, wait);
    })();
  }

  function simpleNotice(io, msg) {
    if (msg.t === 'pocitac-oznameni') io.toast((msg.title ? msg.title + ': ' : '') + msg.message);
    else if (msg.t === 'pocitac-pristup') {
      io.toast('Přístup pro Clauda ze serveru byl změněn z prostoru na: ' + levelLabel(msg.level || '').toLowerCase());
    }
  }

  /* ── na počítači: Nastavení → Účet ───────────────────────────────────────── */
  function panel(io) {
    const box = el('div', 'acc-sec pc-panel');
    box.appendChild(el('div', 'set-title', 'Claude ze serveru na tomhle počítači'));
    box.appendChild(el('div', 'set-note',
      'Claude ve tvém prostoru na serveru může sahat i sem — na soubory a programy ' +
      'na tomhle počítači. ' + TRUST_NOTE));
    const cfg = (io.state && io.state.config) || {};
    const choice = tiles(cfg.pocitac_access || '', pick);
    box.appendChild(choice);
    const line = el('div', 'pc-state');
    box.appendChild(line);
    const recent = el('div', 'pc-recent');
    box.appendChild(recent);
    const tasks = el('div', 'pc-recent');
    box.appendChild(tasks);

    let timer = null;
    let busy = false;

    async function pick(level) {
      if (busy) return;
      busy = true;
      choice.set(level);
      try {
        const st = await io.api('pocitac', {access: level});
        if (st.error) throw new Error(st.error);
        cfg.pocitac_access = st.access;
        cfg.pocitac_asked = true;
        draw(st);
        io.toast(level ? 'Claude ze serveru teď na tenhle počítač dosáhne (' +
                         levelLabel(level).toLowerCase() + ').'
                       : 'Claude ze serveru na tenhle počítač už nesahá.');
      } catch (e) {
        choice.set(cfg.pocitac_access || '');
        io.toast('Nepovedlo se: ' + friendly('pick', e));
      }
      busy = false;
      schedule(800);
    }

    function draw(st) {
      choice.set(st.access || '');
      line.textContent = '';
      line.className = 'pc-state';
      const server = st.server || 'server';
      if (!st.access) {
        line.hidden = true;
      } else {
        line.hidden = false;
        if (st.state === 'online') {
          line.appendChild(el('span', 'set-ok', '✓ Připojeno k ' + server));
          line.appendChild(document.createTextNode(
            ' — Claude ve tvém prostoru na tenhle počítač (' + st.name + ') dosáhne.'));
        } else if (st.state === 'connecting') {
          line.appendChild(el('span', 'set-dim', 'Připojuji se k ' + server + '…'));
          if (st.note) line.appendChild(el('div', 'set-dim', st.note));
        } else {
          line.appendChild(el('span', 'set-warn', '! ' + (st.note || 'Počítač není připojený.')));
        }
      }
      drawTasks(st.ukoly || []);
      // Souhlas: platné povolení na 30 minut a čekající žádosti.
      const g = (st.grants || [])[0];
      if (g && g.until * 1000 > Date.now()) {
        const row = el('div', 'pc-grant');
        row.appendChild(el('span', null, 'Povoleno bez ptaní do ' +
          new Date(g.until * 1000).toLocaleTimeString('cs-CZ', {hour: '2-digit', minute: '2-digit'}) + '.'));
        const off = el('button', 'btn ghost', 'Zrušit povolení');
        off.onclick = async () => { await io.api('pocitac', {action: 'zrusit-povoleni'}).catch(() => {}); load(); };
        row.appendChild(off);
        line.appendChild(row);
      }
      for (const p of localPending(st.pending)) {
        showApproval(io, p, async (id, volba) => {
          const r = await io.api('pocitac', {action: 'rozhodnout', id, volba});
          if (r && r.error) throw new Error(r.error);
        }, 'z prostoru na serveru');
      }
      pruneApprovals(localPending(st.pending));
      recent.textContent = '';
      const rows = st.recent || [];
      if (!rows.length) return;
      recent.appendChild(el('div', 'set-dim pc-recent-t', 'Naposledy Claude ze serveru:'));
      const now = Date.now() / 1000;
      for (const r of rows) {
        const row = el('div', 'pc-row' + (r.ok ? '' : ' bad'));
        row.appendChild(el('span', 'pc-mark', r.ok ? '✓' : '✗'));
        row.appendChild(el('span', 'pc-op', OPS[r.op] || r.op));
        row.appendChild(el('code', 'pc-what', r.what));
        row.appendChild(el('span', 'pc-when', ago(now - r.at)));
        if (!r.ok && r.error) row.title = r.error;
        recent.appendChild(row);
      }
    }

    /* Úkoly, které Claude ze serveru nechal na později (hub/pocitac.py). */
    function drawTasks(list) {
      tasks.textContent = '';
      if (!list.length) return;
      tasks.appendChild(el('div', 'set-dim pc-recent-t', 'Úkoly ze serveru:'));
      for (const u of list.slice(0, 8)) {
        const row = el('div', 'pc-row pc-task');
        row.appendChild(el('span', 'pc-op', UKOL_LABEL[u.state] || u.state));
        row.appendChild(el('span', 'pc-what', u.title));
        row.appendChild(el('span', 'pc-when', stamp(u.received)));
        if (u.state === 'ceka') {
          const b = el('button', 'btn ghost pc-task-btn', 'Zobrazit');
          b.onclick = () => card(io, u);
          row.appendChild(b);
        }
        tasks.appendChild(row);
      }
    }

    async function load() {
      timer = null;
      if (!box.isConnected) return;          // nastavení se zavřelo
      try { draw(await io.api('pocitac')); } catch (_) { /* příště */ }
      schedule(4000);
    }

    function schedule(ms) {
      clearTimeout(timer);
      timer = setTimeout(load, ms);
    }

    schedule(0);
    return box;
  }

  /* ── na počítači: karta s volbou ─────────────────────────────────────────── */
  /* opts.back = okno přišlo ze serveru, hlavní tlačítko ho tam vrátí.
     Resolve s volbou (i nezměněnou). */
  function ask(io, opts) {
    opts = opts || {};
    const cfg = (io.state && io.state.config) || {};
    // Kdo se ještě nerozhodl, má předvybraný plný přístup — kvůli němu tahle
    // funkce vznikla. Potvrdit ho ale musí sám.
    let level = cfg.pocitac_asked ? (cfg.pocitac_access || '') : 'ptat';
    return new Promise((resolve) => {
      const root = el('div', 'onb pc-ask');
      const boxEl = el('div', 'onb-box');
      const head = el('div', 'onb-head');
      const mark = el('span', 'onb-mark');
      mark.appendChild(icon('i-laptop'));
      head.appendChild(mark);
      const titles = el('div');
      titles.appendChild(el('div', 'onb-title', 'Claude ze serveru na tomhle počítači'));
      titles.appendChild(el('div', 'onb-sub',
        global.HubServer ? global.HubServer.hostOf(cfg.gw_server || '') : ''));
      head.appendChild(titles);
      boxEl.appendChild(head);

      const body = el('div', 'onb-body');
      body.appendChild(el('p', 'onb-lead',
        'Ve svém prostoru na serveru máš vlastního Clauda. Může dosáhnout i sem — na ' +
        'soubory a programy na tomhle počítači. Kolik smí?'));
      const choice = tiles(level, (id) => { level = id; choice.set(id); });
      body.appendChild(choice);
      body.appendChild(el('p', 'onb-note pc-trust', TRUST_NOTE +
        ' Změníš to kdykoli v Nastavení → Účet.'));
      const err = el('div', 'srv-status err');
      err.hidden = true;
      body.appendChild(err);
      boxEl.appendChild(body);

      const foot = el('div', 'onb-foot');
      foot.appendChild(el('span', 'spacer'));
      const ok = el('button', 'btn primary', opts.back ? 'Uložit a zpět do prostoru' : 'Pokračovat');
      foot.appendChild(ok);
      boxEl.appendChild(foot);
      root.appendChild(boxEl);
      document.body.appendChild(root);
      ok.focus();

      ok.onclick = async () => {
        ok.disabled = true;
        try {
          const st = await io.api('pocitac', {access: level});
          if (st.error) throw new Error(st.error);
          cfg.pocitac_access = st.access;
          cfg.pocitac_asked = true;
          root.remove();
          resolve(st.access);
        } catch (e) {
          err.textContent = 'Uložit se nepovedlo: ' + friendly('ask', e);
          err.hidden = false;
          ok.disabled = false;
        }
      };
    });
  }

  /* ── na počítači: úkoly ze serveru ────────────────────────────────────────── */
  const UKOL_LABEL = {ceka: 'čeká', spusteno: 'spuštěný', zahozeno: 'zahozený'};
  const shown = new Set();            // úkoly, které mají kartu nebo ji měly

  /* Karta s úkolem, který Claude ze serveru nechal na později. Spustí ho
     člověk tady — s přístupem jen ke čtení se sám nespustí. */
  function card(io, u) {
    const open = document.querySelector('.pc-ukol');
    if (open) open.remove();
    shown.add(u.id);
    const root = el('div', 'onb pc-ukol');
    const box = el('div', 'onb-box');
    const head = el('div', 'onb-head');
    const mark = el('span', 'onb-mark');
    mark.appendChild(icon('i-laptop'));
    head.appendChild(mark);
    const titles = el('div');
    titles.appendChild(el('div', 'onb-title', 'Úkol od Clauda ze serveru'));
    titles.appendChild(el('div', 'onb-sub',
      [u.server, u.created && 'zadáno ' + stamp(u.created)].filter(Boolean).join(' · ')));
    head.appendChild(titles);
    box.appendChild(head);

    const body = el('div', 'onb-body');
    body.appendChild(el('div', 'pc-ukol-name', u.title));
    body.appendChild(el('div', 'pc-ukol-text', u.text));
    if (u.folder) body.appendChild(el('div', 'pc-ukol-meta', 'Složka: ' + u.folder));
    if ((u.files || []).length) {
      body.appendChild(el('div', 'pc-ukol-meta', 'Přílohy: ' + u.files.join(', ')));
    }
    body.appendChild(el('p', 'onb-note',
      'Spustí se v novém rozhovoru s Claudem tady na počítači. Claude si zadání přečte ' +
      'a než začne, shrne, co udělá.'));
    const err = el('div', 'srv-status err');
    err.hidden = true;
    body.appendChild(err);
    box.appendChild(body);

    const foot = el('div', 'onb-foot');
    const drop = el('button', 'btn ghost', 'Zahodit');
    const later = el('button', 'btn ghost', 'Později');
    const run = el('button', 'btn primary', 'Spustit');
    foot.append(drop, later, el('span', 'spacer'), run);
    box.appendChild(foot);
    root.appendChild(box);
    document.body.appendChild(root);
    run.focus();

    async function act(action, done) {
      run.disabled = drop.disabled = true;
      try {
        const r = await io.api('pocitac', {action, id: u.id});
        if (r.error) throw new Error(r.error);
        root.remove();
        done(r);
        pending(io);                         // další čekající, když nějaký je
      } catch (e) {
        err.textContent = 'Nepovedlo se: ' + friendly('ukol', e);
        err.hidden = false;
        run.disabled = drop.disabled = false;
      }
    }
    run.onclick = () => act('ukol-spustit', (r) => {
      if (r.tab && io.focusTab) io.focusTab(r.tab.id);
    });
    drop.onclick = () => act('ukol-zahodit', () => io.toast('Úkol zahozen.'));
    later.onclick = () => {
      root.remove();
      io.toast('Úkol počká — najdeš ho v Nastavení → Účet.');
    };
  }

  /* Po načtení okna: čekající úkoly. `opts.focus` = okno se vrátilo ze
     serveru kvůli úkolu — když už běží, přepne se na jeho tab. */
  async function pending(io, opts) {
    opts = opts || {};
    let st;
    try { st = await io.api('pocitac'); } catch (_) { return; }
    const list = st.ukoly || [];
    const waiting = list.filter((u) => u.state === 'ceka' && !shown.has(u.id));
    if (waiting.length) return card(io, waiting[waiting.length - 1]);   // nejstarší
    if (opts.focus && io.focusTab) {
      const running = list.find((u) => u.state === 'spusteno' && u.tab);
      if (running) io.focusTab(running.tab);
    }
  }

  /* Hláška od hubu (websocket): úkol ze serveru dorazil nebo se spustil. */
  async function ukol(io, msg) {
    if (msg.state === 'spusteno') {
      io.toast('Úkol ze serveru „' + msg.title + '“ běží v novém rozhovoru.');
      if (msg.auto && io.focusTab) io.focusTab(msg.tab, {idle: true});
      return;
    }
    if (msg.state === 'ceka' && !shown.has(msg.id) && !document.querySelector('.pc-ukol')) {
      pending(io);
    }
  }

  /* ── v prostoru na serveru ───────────────────────────────────────────────── */
  async function gwState() {
    const r = await fetch('/gw/pocitac', {credentials: 'same-origin'});
    if (!r.ok) {
      console.warn('pocitac.js: /gw/pocitac HTTP ' + r.status);
      throw new Error('Server teď neodpovídá, zkus to za chvíli.');
    }
    return r.json();
  }

  async function gwPost(path, payload) {
    const r = await fetch(path, {
      method: 'POST', credentials: 'same-origin',
      headers: {'Content-Type': 'application/json', 'X-Hub-Account': '1'},
      body: JSON.stringify(payload),
    });
    const d = await r.json().catch(() => ({}));
    if (!r.ok || d.ok === false) throw new Error(d.error || 'Server teď neodpovídá, zkus to za chvíli.');
    return d;
  }

  // Souhlas s úkolem počítače z prostoru: rozhoduje člověk přes bránu, ne prostor.
  function serverApprovals(io, list) {
    for (const c of list) {
      for (const p of c.pending || []) {
        showApproval(io, p, (id, volba, odpovedi) => gwPost('/gw/pocitac/rozhodnout',
          {computer: c.id, id, volba, odpovedi}), 'počítač ' + c.name);
      }
    }
    pruneApprovals(list.flatMap((c) => c.pending || []));
  }

  async function computers() {
    const data = await gwState();
    return (data.computers || []).filter((c) => c.online && c.access);
  }

  /* Karta v prostoru: počítač si úkol převzal. Jednou na úkol a stav — co
     už člověk viděl, drží localStorage (bez něj se ukáže jen v tomhle načtení). */
  const SEEN_KEY = 'hub.ukolySeen';
  const seenHere = new Set();

  function seen(key) {
    if (seenHere.has(key)) return true;
    try { return (JSON.parse(localStorage.getItem(SEEN_KEY) || '[]')).includes(key); }
    catch (_) { return false; }
  }

  function markSeen(key) {
    seenHere.add(key);
    try {
      const list = JSON.parse(localStorage.getItem(SEEN_KEY) || '[]').filter((k) => k !== key);
      list.push(key);
      localStorage.setItem(SEEN_KEY, JSON.stringify(list.slice(-50)));
    } catch (_) { /* jen pohodlí */ }
  }

  function ukolyNotice(io, list) {
    if (document.querySelector('.pc-ukol')) return;
    const now = Date.now() / 1000;
    const u = (list || []).find((x) => x.kind !== 'chat' && (x.state === 'prevzato' || x.state === 'spusteno') &&
                                       now - x.changed < 30 * 60 && !seen(x.id + ':' + x.state));
    if (!u) return;
    markSeen(u.id + ':' + u.state);
    const started = u.state === 'spusteno';
    const root = el('div', 'onb pc-ukol');
    const box = el('div', 'onb-box');
    const head = el('div', 'onb-head');
    const mark = el('span', 'onb-mark');
    mark.appendChild(icon('i-laptop'));
    head.appendChild(mark);
    const titles = el('div');
    titles.appendChild(el('div', 'onb-title',
      started ? 'Úkol běží na počítači' : 'Úkol čeká na počítači'));
    titles.appendChild(el('div', 'onb-sub', u.by || ''));
    head.appendChild(titles);
    box.appendChild(head);
    const body = el('div', 'onb-body');
    body.appendChild(el('div', 'pc-ukol-name', u.title));
    body.appendChild(el('p', 'onb-lead', started
      ? 'Počítač ' + (u.by || '') + ' si úkol převzal a Claude na něm pracuje v novém rozhovoru. ' +
        'Když bude potřebovat souhlas, ukáže se to i tady jako karta.'
      : 'Počítač ' + (u.by || '') + ' si úkol převzal. Má zapnutý přístup jen ke čtení, ' +
        'takže se nespustí sám — potvrď ho v aplikaci na počítači.'));
    box.appendChild(body);
    const foot = el('div', 'onb-foot');
    const close = el('button', 'btn ghost', 'Zavřít');
    close.onclick = () => root.remove();
    foot.append(close, el('span', 'spacer'));
    const hs = global.HubServer;
    if (hs && hs.localBack() && hs.appAtLeast('2.15.0')) {
      const go = el('button', 'btn primary', started ? 'Přejít na počítač' : 'Potvrdit na počítači');
      go.title = 'Okno přejde do aplikace na počítači. Do prostoru se vrátíš v Nastavení → Účet.';
      go.onclick = () => hs.backTo('ukoly');
      foot.appendChild(go);
    }
    box.appendChild(foot);
    root.appendChild(box);
    document.body.appendChild(root);
  }

  function lastText(c) {
    if (!c.last) return '';
    return (OPS[c.last.op] || c.last.op) + ' „' + c.last.summary + '" ' + ago(c.last.ago);
  }

  function serverBlock(io) {
    const box = el('div', 'acc-sec pc-panel');
    box.appendChild(el('div', 'set-title', 'Tvůj počítač'));
    const body = el('div');
    body.appendChild(el('div', 'set-note', 'Zjišťuji, jestli je připojený…'));
    box.appendChild(body);

    gwState().then((data) => {
      const list = (data.computers || []).filter((c) => c.online && c.access);
      body.textContent = '';
      if (list.length) {
        body.appendChild(el('div', 'set-note',
          'Claude tady v prostoru sahá i na tyhle počítače — ' +
          'řekni mu třeba „podívej se na počítači do Stažených".'));
        for (const c of list) {
          const row = el('div', 'pc-comp');
          row.appendChild(el('span', 'set-ok', '● ' + c.name));
          row.appendChild(el('span', 'set-dim',
            [c.system, c.access_label, lastText(c) && 'naposledy ' + lastText(c)]
              .filter(Boolean).join(' · ')));
          body.appendChild(row);
          // Přístup se dá změnit i odsud: rozhoduješ ty (přihlášený v prohlížeči), ne Claude.
          const pick = tiles(c.access, async (level) => {
            pick.set(level);
            try {
              await gwPost('/gw/pocitac/pristup', {computer: c.id, level});
              io.toast('Změna se na počítači ' + c.name + ' projeví za pár vteřin: ' +
                       levelLabel(level).toLowerCase() + '.');
            } catch (e) {
              pick.set(c.access);
              io.toast('Nepovedlo se: ' + friendly('pristup', e));
            }
          });
          body.appendChild(pick);
          const g = (c.grants || [])[0];
          if (g && g.until * 1000 > Date.now()) {
            body.appendChild(el('div', 'set-dim', 'Povoleno bez ptaní do ' +
              new Date(g.until * 1000).toLocaleTimeString('cs-CZ', {hour: '2-digit', minute: '2-digit'}) + '.'));
          }
        }
      } else {
        body.appendChild(el('div', 'set-note',
          'Claude v prostoru teď na žádný tvůj počítač nedosáhne. Zapíná se v aplikaci ' +
          'Aladin na počítači (Nastavení → Účet → Claude ze serveru na tomhle ' +
          'počítači) a funguje, dokud je aplikace otevřená. Co má počkat, než se počítač ' +
          'připojí, řekni Claudovi — nechá mu úkol a Claude na počítači ho pak dodělá.'));
      }
      const tasks = data.ukoly || [];
      if (tasks.length) {
        body.appendChild(el('div', 'set-dim pc-recent-t', 'Úkoly na později:'));
        for (const u of tasks.slice(0, 8)) {
          const row = el('div', 'pc-row pc-task');
          row.appendChild(el('span', 'pc-op', u.state_label + (u.by ? ' (' + u.by + ')' : '')));
          row.appendChild(el('span', 'pc-what', u.title));
          row.appendChild(el('span', 'pc-when', stamp(u.changed || u.created)));
          body.appendChild(row);
        }
      }
      if (global.HubServer && global.HubServer.localBack() &&
          !global.HubServer.appAtLeast('2.14.1')) {
        body.appendChild(global.HubServer.oldAppNote());
      } else if (global.HubServer && global.HubServer.localBack()) {
        const row = el('div', 'set-row');
        const b = el('button', 'btn ghost',
          list.length ? 'Změnit na tomhle počítači' : 'Zapnout na tomhle počítači');
        b.title = 'Okno se na chvíli vrátí do aplikace na počítači, tam zvolíš přístup ' +
          'a pak se vrátíš sem.';
        b.onclick = () => global.HubServer.backTo('pocitac');
        row.appendChild(b);
        body.appendChild(row);
      }
    }, () => {
      body.textContent = '';
      body.appendChild(el('div', 'set-note', 'Server propojení s počítačem zatím neumí.'));
    });
    return box;
  }

  /* Štítek v hlavičce: viditelný jen s připojeným počítačem. */
  function chip(io) {
    const host = document.querySelector('.topbar-title');
    if (!host || host.querySelector('.topbar-pc')) return;
    const b = el('span', 'topbar-pc plain');
    b.hidden = true;
    b.appendChild(icon('i-laptop'));
    const label = el('span');
    b.appendChild(label);
    /* Cedulka, ne tlačítko: říká, na který počítač Claude z prostoru
       dosáhne. Klikalo se tu do nastavení, což s tím jménem nesouvisí —
       podrobnosti jsou v popisku, který se ukáže po najetí. */
    host.appendChild(b);

    let timer = null;
    async function tick() {
      clearTimeout(timer);
      let wait = 15000;
      try {
        const data = await gwState();
        const list = (data.computers || []).filter((c) => c.online && c.access);
        ukolyNotice(io, data.ukoly);
        serverApprovals(io, list);
        if (list.some((c) => (c.pending || []).length)) wait = 2000;
        b.hidden = !list.length;
        if (list.length) {
          label.textContent = list.length === 1 ? list[0].name : list.length + ' počítače';
          b.title = list.map((c) => 'Claude v prostoru dosáhne na počítač ' + c.name +
            ' (' + c.access_label + ').' + (c.last ? ' Naposledy ' + lastText(c) + '.' : ''))
            .join('\n');
          // Zvýraznit, když Claude na počítači právě něco dělá; mezitím se
          // ptát častěji, ať štítek nezaostává.
          const newest = Math.min(...list.map((c) => (c.last ? c.last.ago : 1e9)));
          b.classList.toggle('busy', newest < 10);
          if (wait > 5000) wait = newest < 60 ? 5000 : 15000;
        }
      } catch (_) {
        b.hidden = true;
        wait = 5 * 60 * 1000;                 // starší brána most nezná
      }
      timer = setTimeout(tick, wait);
    }
    document.addEventListener('visibilitychange', () => {
      if (document.visibilityState === 'visible') tick();
    });
    tick();
  }

  global.HubPocitac = {LEVELS, panel, ask, serverBlock, chip, levelLabel, ukol, pending,
                       approvalFromHub, simpleNotice, watchPending};

})(window);
