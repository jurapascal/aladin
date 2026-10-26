/* Statistiky používání — z toho, co si Claude Code sám ukládá na disk.
 *
 * Všechny grafy tu měří jedno a totéž: velikost. Žádná série se nerozlišuje
 * barvou, takže tu není co plést a nepotřebují legendu — nadpis říká, co je
 * na svislé ose, a hodnotu ukáže popisek po najetí. Barva je jantarová
 * aplikace ve dvou krocích (`--chart`), jeden pro světlý a jeden pro tmavý
 * podklad: stejný odstín na obojím by na jednom z nich neměl dost kontrastu.
 */
'use strict';

(function (global) {

  let io = null;
  let root = null;
  let data = null;

  const DNY = ['po', 'út', 'st', 'čt', 'pá', 'so', 'ne'];

  /* Jednoduchý režim (výchozí) × Pro pokročilé (hub.js → window.HUB_ADVANCED).
     V jednoduchém se ukazuje jen to, čemu rozumí každý — zprávy, rozhovory,
     dny s prací. Tokeny, cache, GitHub a cesty na disku jsou pro pokročilé. */
  const pokrocile = () => !!global.HUB_ADVANCED;

  // Tvar slova podle počtu: 1 rozhovor, 2–4 rozhovory, 5+ rozhovorů.
  function tvar(n, jedna, dve, pet) {
    return n === 1 ? jedna : n >= 2 && n <= 4 ? dve : pet;
  }

  function el(tag, cls, text) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text != null) node.textContent = text;
    return node;
  }

  /* Velká čísla se nekreslí jako graf — jedno číslo je samo o sobě sdělení. */
  function tile(value, label, note) {
    const box = el('div', 'st-tile');
    box.appendChild(el('div', 'st-value', value));
    box.appendChild(el('div', 'st-label', label));
    if (note) box.appendChild(el('div', 'st-note', note));
    return box;
  }

  function cislo(n) {
    if (n >= 1e9) return (n / 1e9).toFixed(1).replace('.', ',') + ' mld';
    if (n >= 1e6) return (n / 1e6).toFixed(1).replace('.', ',') + ' M';
    if (n >= 1e3) return Math.round(n / 1e3) + ' tis.';
    return String(n);
  }

  /* „Pěkná" stupnice: nejbližší vyšší krok 1/2/5 × 10^n, ať jsou čáry na
     kulatých číslech (0, 50, 100, 150…) a ne na 37,4. */
  function stupnice(max, cilKroku) {
    const hrub = max / Math.max(1, cilKroku);
    const mag = Math.pow(10, Math.floor(Math.log10(hrub)));
    const f = hrub / mag;
    const krok = (f <= 1 ? 1 : f <= 2 ? 2 : f <= 5 ? 5 : 10) * mag;
    const top = Math.ceil(max / krok) * krok;
    const ticks = [];
    for (let v = 0; v <= top + krok / 2; v += krok) ticks.push(v);
    return {top, ticks};
  }

  /* Sloupcový graf: osa Y s kulatými hodnotami a vodorovnou mřížkou, popisky
     dní po osě X a vlastní bublina po najetí (hodnota + den). */
  function bars(items, opts) {
    opts = opts || {};
    const max = Math.max(1, ...items.map(i => i.value));
    const {top, ticks} = stupnice(max, 4);
    const wrap = el('div', 'st-chart');
    const yaxis = el('div', 'st-yaxis');
    for (const v of ticks) {
      const t = el('span', 'st-ytick', cislo(v));
      t.style.bottom = (v / top * 100) + '%';
      yaxis.appendChild(t);
    }
    const plot = el('div', 'st-plot');
    for (const v of ticks) {
      const g = el('div', 'st-grid' + (v === 0 ? ' zero' : ''));
      g.style.bottom = (v / top * 100) + '%';
      plot.appendChild(g);
    }
    // Popisky osy X: kde je zadaný `tick`, ten; u dnů jinak každý n-tý.
    const krok = Math.ceil(items.length / 8);
    const maDatum = (i) => /^\d{4}-\d{2}-\d{2}$/.test(i.full || '');
    const tip = el('div', 'st-tip');
    tip.hidden = true;
    items.forEach((item, idx) => {
      const col = el('div', 'st-col');
      const bar = el('div', 'st-bar');
      bar.style.height = Math.max(item.value ? 2 : 0, item.value / top * 100) + '%';
      if (opts.dim && opts.dim(item)) bar.classList.add('dim');
      col.appendChild(bar);
      let tick = item.tick;
      if (maDatum(item) && items.length > 14) {
        tick = (idx % krok === 0 && items.length - 1 - idx >= krok / 2) || idx === items.length - 1
          ? item.full.slice(8) + '. ' + Number(item.full.slice(5, 7)) + '.' : '';
      }
      if (tick) col.appendChild(el('span', 'st-tick', tick));
      const text = `${item.full || item.label}: ${cislo(item.value)}${opts.unit || ''}`;
      col.onmouseenter = () => {
        tip.textContent = text;
        tip.hidden = false;
        const pw = plot.clientWidth, cx = col.offsetLeft + col.offsetWidth / 2;
        tip.style.left = Math.min(pw - tip.offsetWidth / 2, Math.max(tip.offsetWidth / 2, cx)) + 'px';
        tip.style.bottom = (bar.offsetHeight + 18) + 'px';
      };
      col.onmouseleave = () => { tip.hidden = true; };
      plot.appendChild(col);
    });
    plot.appendChild(tip);
    wrap.append(yaxis, plot);
    return wrap;
  }

  function section(title, note) {
    const box = el('div', 'st-sec');
    box.appendChild(el('div', 'set-title', title));
    if (note) box.appendChild(el('div', 'set-note', note));
    return box;
  }

  async function open(opts) {
    io = opts;
    root = el('div', 'onb');
    root.innerHTML = `
      <div class="onb-box st-box">
        <div class="onb-head">
          <span class="onb-mark"></span>
          <div>
            <div class="onb-title">Statistiky</div>
            <div class="onb-sub">Z toho, co si Claude Code ukládá na disk</div>
          </div>
        </div>
        <div class="onb-body"></div>
        <div class="onb-foot">
          <button class="btn ghost st-refresh">Přepočítat</button>
          <span class="st-export-obal">
            <button class="btn ghost st-export" aria-haspopup="true">Exportovat</button>
            <span class="st-export-menu" hidden></span>
          </span>
          <span class="spacer"></span>
          <button class="btn primary st-close">Zavřít</button>
        </div>
      </div>`;
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('viewBox', '0 0 24 24');
    svg.setAttribute('width', '34');
    svg.setAttribute('height', '34');
    const use = document.createElementNS('http://www.w3.org/2000/svg', 'use');
    use.setAttribute('href', '#i-hub');
    svg.appendChild(use);
    root.querySelector('.onb-mark').appendChild(svg);
    root.querySelector('.st-close').onclick = close;
    root.querySelector('.st-refresh').onclick = () => load(true);
    root.querySelector('.st-export').onclick = (ev) => { ev.stopPropagation(); exportMenu(); };
    // Zavřít jen klikem, který na pozadí i začal: výběr textu tažením ven
    // z okna končí puštěním tlačítka na pozadí a prohlížeč to hlásí jako klik.
    let downOutside = false;
    root.addEventListener('pointerdown', (ev) => { downOutside = ev.target === root; });
    root.addEventListener('click', (ev) => { if (ev.target === root && downOutside) close(); });
    document.body.appendChild(root);
    load(false);
  }

  async function load(refresh) {
    const body = root.querySelector('.onb-body');
    body.textContent = '';
    const status = el('div', 'set-status busy', 'Počítám…');
    body.appendChild(status);
    for (;;) {
      let res;
      try {
        res = await io.api('stats' + (refresh ? '?refresh=1' : ''));
      } catch (err) {
        console.warn('stats:', err);
        status.className = 'set-status warn';
        status.textContent = pokrocile() ? 'Nepovedlo se: ' + err.message
          : 'Statistiky se teď nepodařilo spočítat, zkus to za chvíli znovu.';
        return;
      }
      refresh = false;
      if (res.running) {
        status.textContent = res.step || 'Počítám…';
        await new Promise(r => setTimeout(r, 1000));
        continue;
      }
      data = res;
      render();
      return;
    }
  }

  function render() {
    const body = root.querySelector('.onb-body');
    body.textContent = '';

    const planSec = section('Limit předplatného', 'Totéž, co ukazuje /usage v Claude Code.');
    const planBox = el('div', 'pl-telo');
    planSec.appendChild(planBox);
    body.insertBefore(planSec, body.firstChild);
    plan(io, planBox, false);
    const t = data.tokens || {};
    const pro = pokrocile();
    root.querySelector('.onb-sub').textContent = pro
      ? 'Z toho, co si Claude Code ukládá na disk'
      : 'Jak často s Claudem pracuješ';

    // ── velká čísla ──────────────────────────────────────────────────────
    const tiles = el('div', 'st-tiles');
    if (pro) {
      tiles.appendChild(tile(cislo(t.out || 0), 'napsaných tokenů',
        `z toho ${cislo(t.think || 0)} přemýšlení`));
      tiles.appendChild(tile(cislo(data.prompts || 0), 'odeslaných zpráv',
        `${cislo(t.answers || 0)} odpovědí`));
      tiles.appendChild(tile(String(data.sessions || 0), 'sezení',
        `${data.active_days || 0} dnů s prací`));
      tiles.appendChild(tile(cislo(t.cache_r || 0), 'přečteno z cache',
        `zapsáno ${cislo(t.cache_w || 0)}`));
      if (data.cost) tiles.appendChild(tile(dolary(data.cost), 'odhad ceny', 'podle ceníku API, na předplatném se neplatí'));
    } else {
      const zprav = data.prompts || 0;
      const odpovedi = t.answers || 0;
      const rozhovoru = data.sessions || 0;
      const dni = data.active_days || 0;
      tiles.appendChild(tile(cislo(zprav),
        tvar(zprav, 'odeslaná zpráva', 'odeslané zprávy', 'odeslaných zpráv'),
        odpovedi ? `${cislo(odpovedi)} ${tvar(odpovedi, 'odpověď', 'odpovědi', 'odpovědí')} od Clauda` : ''));
      tiles.appendChild(tile(String(rozhovoru),
        tvar(rozhovoru, 'rozhovor', 'rozhovory', 'rozhovorů')));
      tiles.appendChild(tile(String(dni), tvar(dni, 'den s prací', 'dny s prací', 'dní s prací')));
    }
    body.appendChild(tiles);

    // ── denní doba ───────────────────────────────────────────────────────
    const hours = (data.hours || []).map((v, h) => ({
      value: v, label: h + ':00', full: `${h}:00–${h}:59`,
      tick: h % 6 === 0 ? String(h) : '',
    }));
    const peak = hours.reduce((a, b) => (b.value > a.value ? b : a), hours[0] || {});
    const s1 = section('Kdy píšeš', peak && peak.value
      ? `Nejvíc mezi ${peak.full} — ${peak.value} ${tvar(peak.value, 'zpráva', 'zprávy', 'zpráv')}.` : '');
    s1.appendChild(bars(hours, {unit: ' zpráv'}));
    body.appendChild(s1);

    // ── dny v týdnu ──────────────────────────────────────────────────────
    const wd = (data.weekdays || []).map((v, i) => ({
      value: v, label: DNY[i], full: DNY[i], tick: DNY[i],
    }));
    const s2 = section('Dny v týdnu');
    s2.appendChild(bars(wd, {unit: ' zpráv', dim: (i) => i.label === 'so' || i.label === 'ne'}));
    body.appendChild(s2);

    // ── poslední dny ─────────────────────────────────────────────────────
    const days = (data.days || []).slice(-60);
    if (days.length) {
      const items = days.map((d, i) => ({
        value: d.prompts, label: d.day, full: d.day,
        tick: i === 0 || i === days.length - 1 ? d.day.slice(5) : '',
      }));
      const s3 = section('Posledních ' + days.length + ' dnů',
        'Počet odeslaných zpráv za den.');
      s3.appendChild(bars(items, {unit: ' zpráv'}));
      body.appendChild(s3);

      // Dnes a včera vedle sebe, pod nimi tokeny po dnech.
      const iso = (d) => new Date(d.getTime() - d.getTimezoneOffset() * 6e4).toISOString().slice(0, 10);
      const dnes = new Date(), vcera = new Date(Date.now() - 864e5);
      const najdi = (k) => days.find((d) => d.day === k) || {prompts: 0, out: 0};
      const a = najdi(iso(dnes)), b = najdi(iso(vcera));
      const sd = section('Denní přehled', 'Dnes a včera, pak napsané tokeny za každý den.');
      const tiles = el('div', 'st-tiles');
      tiles.appendChild(tile(String(a.prompts), 'zpráv dnes', cislo(a.out) + ' tokenů'));
      tiles.appendChild(tile(String(b.prompts), 'zpráv včera', cislo(b.out) + ' tokenů'));
      sd.appendChild(tiles);
      sd.appendChild(bars(days.map((d) => ({value: d.out || 0, label: d.day, full: d.day})),
        {unit: ' tokenů'}));
      body.appendChild(sd);
    }

    // ── projekty ─────────────────────────────────────────────────────────
    // Pokročilí vidí tokeny, ostatní počet zpráv — to je, co člověk napsal.
    const kolik = (p) => (pro ? p.out : p.prompts) || 0;
    const projects = (data.projects || []).slice();
    if (!pro) projects.sort((a, b) => kolik(b) - kolik(a));
    if (projects.length) {
      const s4 = section(pro ? 'Kde to padá' : 'Na čem pracuješ',
        pro ? 'Napsané tokeny podle projektu.' : 'Počet zpráv podle projektu.');
      const list = el('div', 'st-rows');
      const max = Math.max(1, ...projects.map(kolik));
      for (const p of projects) {
        const row = el('div', 'st-row');
        row.appendChild(el('span', 'st-row-name', p.name));
        const track = el('span', 'st-track');
        const fill = el('span', 'st-fill');
        fill.style.width = Math.max(1, kolik(p) / max * 100) + '%';
        track.appendChild(fill);
        row.appendChild(track);
        row.appendChild(el('span', 'st-row-val', cislo(kolik(p))));
        row.title = pro
          ? `${p.path || p.name}\n${cislo(p.out)} tokenů · ${p.prompts} zpráv · ${dolary(p.cost)}`
          : `${p.name}\n${p.prompts || 0} ${tvar(p.prompts || 0, 'zpráva', 'zprávy', 'zpráv')}`;
        list.appendChild(row);
      }
      s4.appendChild(list);
      body.appendChild(s4);
    }

    // Zbytek (GitHub, odkud se počítá) je jen pro pokročilé.
    if (!pro) return;

    // ── GitHub ───────────────────────────────────────────────────────────
    const gh = data.github || {};
    const s5 = section('GitHub', gh.ok ? '@' + gh.login : '');
    if (!gh.ok) {
      const warn = el('div', 'set-status warn', gh.detail || 'Nedostupné.');
      s5.appendChild(warn);
    } else {
      const g = el('div', 'st-tiles');
      g.appendChild(tile(String(gh.commits_year), 'commitů za rok',
        `ve ${gh.repos_touched} repozitářích`));
      g.appendChild(tile(String(gh.contributions), 'příspěvků celkem',
        `${gh.private_repos} privátních repozitářů`));
      s5.appendChild(g);
      const ghDays = (gh.days || []).slice(-60).map((d, i, arr) => ({
        value: d.count, label: d.day, full: d.day,
        tick: i === 0 || i === arr.length - 1 ? d.day.slice(5) : '',
      }));
      if (ghDays.length) s5.appendChild(bars(ghDays, {unit: ' příspěvků'}));
    }
    body.appendChild(s5);

    const tymBox = el('div', 'st-tym');
    body.appendChild(tymBox);
    kresliTym(tymBox);

    const foot = el('div', 'set-note',
      'Počítáno z ~/.claude — tokeny z přepisů sezení, zprávy z historie. ' +
      (data.rescanned ? `Nově přečteno ${data.rescanned} souborů.` : 'Z mezipaměti.'));
    body.appendChild(foot);
  }

  /* ── tým: spotřeba po lidech a týdenní limity ────────────────────────────
     Jen na serveru (brána). Správce vidí všechny (server i počítače na
     stejném předplatném) a každému nastaví týdenní limit — v USD, procentech
     předplatného, tokenech nebo kombinaci — a jestli má po překročení stopku.
     Ostatní vidí jen sebe, a jen když nějaký limit mají. */
  async function gwSpotreba(payload) {
    const r = await fetch('/gw/spotreba', payload === undefined ? {credentials: 'same-origin'} : {
      method: 'POST', credentials: 'same-origin',
      headers: {'Content-Type': 'application/json', 'X-Hub-Account': '1'},
      body: JSON.stringify(payload),
    });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(d.error || 'Server teď neodpovídá, zkus to za chvíli.');
    return d;
  }


  /* ── limit předplatného: totéž co /usage v Claude Code ───────────────────
     Procenta skutečného limitu (5 hodin a týden) od Anthropic, ne odhad z
     přepisů. Každý vidí svůj; na serveru je to limit jeho vlastního
     předplatného (v prostoru jede jen on), správce navíc vidí všechny v Týmu. */
  function zbyva(iso) {
    const t = Date.parse(iso || '');
    if (!t) return '';
    const min = Math.max(0, Math.round((t - Date.now()) / 60000));
    if (min < 60) return 'obnoví se za ' + min + ' min';
    const h = Math.floor(min / 60);
    if (h < 24) return 'obnoví se za ' + h + ' h ' + (min % 60) + ' min';
    const d = new Date(t);
    return 'obnoví se ' + DNY[(d.getDay() + 6) % 7] + ' ' + d.getDate() + '. ' + (d.getMonth() + 1) + '. v ' +
      d.getHours() + ':' + String(d.getMinutes()).padStart(2, '0');
  }

  function planRadek(nazev, okno, tenky) {
    const row = el('div', 'pl-row' + (tenky ? ' tenky' : ''));
    const stav = okno.pct >= 95 ? 'nad' : okno.pct >= 80 ? 'blizko' : '';
    if (stav) row.classList.add(stav);
    const hlava = el('div', 'pl-hlava');
    hlava.appendChild(el('span', 'pl-nazev', nazev));
    hlava.appendChild(el('span', 'pl-pct', Math.round(okno.pct) + ' %'));
    row.appendChild(hlava);
    const track = el('span', 'st-track');
    const fill = el('span', 'st-fill');
    fill.style.width = Math.max(1, Math.min(100, okno.pct)) + '%';
    track.appendChild(fill);
    row.appendChild(track);
    const kdy = zbyva(okno.resets);
    if (kdy) row.appendChild(el('div', 'pl-kdy', kdy));
    return row;
  }

  function kresliPlanData(box, d) {
    box.textContent = '';
    if (!d || !d.ok) {
      box.appendChild(el('div', 'set-note', (d && d.chyba) || 'Limit se teď nepodařilo zjistit.'));
      return;
    }
    if (d.session) box.appendChild(planRadek('Aktuální sezení (5 hodin)', d.session));
    if (d.week) box.appendChild(planRadek('Týden — všechny modely', d.week));
    if (d.week_sonnet) box.appendChild(planRadek('Týden — Sonnet', d.week_sonnet, true));
    if (d.week_opus) box.appendChild(planRadek('Týden — Opus', d.week_opus, true));
    if (d.extra && d.extra.limit) {
      const k = Math.pow(10, d.extra.dec || 2);
      const cena = (v) => (Number(v || 0) / k).toLocaleString('cs-CZ', {maximumFractionDigits: 2}) + ' ' + (d.extra.mena || '');
      box.appendChild(planRadek('Navíc (měsíc) — ' + cena(d.extra.used) + ' z ' + cena(d.extra.limit),
        {pct: Number(d.extra.pct || 0), resets: null}, true));
    }
  }

  async function plan(opts, box, refresh) {
    box.textContent = '';
    box.appendChild(el('div', 'set-note', 'Zjišťuju limit…'));
    let d;
    try {
      d = await opts.api('plan-usage' + (refresh ? '?refresh=1' : ''));
    } catch (_) { d = {ok: false, chyba: 'Limit se teď nepodařilo zjistit.'}; }
    kresliPlanData(box, d);
    return d;
  }

  /* Úvodní obrazovka: panel, který vidí každý. Bez přihlášení Clauda se nekreslí nic. */
  async function planNaUvodu(opts, box) {
    const d = await opts.api('plan-usage').catch(() => null);
    if (!d || (!d.ok && d.druh === 'bez-prihlaseni')) { box.hidden = true; return; }
    box.textContent = '';
    box.appendChild(el('div', 'set-title', 'Limit předplatného'));
    const telo = el('div', 'pl-telo');
    box.appendChild(telo);
    kresliPlanData(telo, d);
    box.hidden = false;
  }

  const STAV_TEXT = {nad: 'nad limitem', blizko: 'blízko limitu'};
  const tokeny = (n) => {
    n = Number(n) || 0;
    if (n >= 1e6) return (Math.round(n / 1e5) / 10).toLocaleString('cs-CZ') + ' mil.';
    return Math.round(n).toLocaleString('cs-CZ');
  };
  const kdyText = (ts) => {
    if (!ts) return '';
    const d = new Date(ts * 1000);
    return DNY[(d.getDay() + 6) % 7] + ' ' + d.getDate() + '. ' + (d.getMonth() + 1) + '. ' +
      d.getHours() + ':' + String(d.getMinutes()).padStart(2, '0');
  };
  // Odkud jsou procenta u člověka (gateway/spotreba.py, `procenta.zdroj`).
  function pctZdroj(p) {
    if (!p) return '';
    if (p.zdroj === 'tym') return 'Klíč API procenta předplatného nemá — počítá se podíl na spotřebě celého týmu.';
    if (p.zdroj === 'sdileny') {
      return 'Přihlášení sdílí ' + p.lidi + ' lidé (účet je na ' + Math.round(p.ucet_pct) +
        ' % týdne) — každému připadá díl podle jeho podílu na ceně.';
    }
    if (p.pct == null) return 'Anthropic teď procenta neřekl — limit v procentech se do té doby nehlídá.';
    return 'Skutečná procenta týdenního limitu předplatného od Anthropic (včetně počítače na stejném předplatném).';
  }
  // Limit člověka jako text (řádek Týmu, export): „150 $ · 50 % · 2 mil. tokenů · stopka“.
  function limitText(r) {
    const l = r.limity || {usd: r.limit || 0};
    const casti = [];
    if (l.usd) casti.push(Number.isInteger(Number(l.usd)) ? Number(l.usd).toLocaleString('cs-CZ') + ' $' : dolary(l.usd));
    if (l.pct) casti.push(l.pct + ' %');
    if (l.tokeny) casti.push(tokeny(l.tokeny) + ' tok.');
    if (casti.length && l.stopka) casti.push('stopka');
    return casti.join(' · ');
  }
  // Z čeho je týdenní spotřeba: server, počítač na stejném předplatném, jinde.
  function rozpad(r) {
    const pc = r.pocitac || {};
    const radky = ['Na serveru ' + dolary((r.server || {}).tyden != null ? r.server.tyden : r.tyden)];
    const stroje = (pc.zarizeni || []).filter((z) => z.stejne).map((z) => z.nazev);
    if (pc.tyden || stroje.length) radky.push('na počítači ' + dolary(pc.tyden) + (stroje.length ? ' (' + stroje.join(', ') + ')' : ''));
    if (pc.jinde_tyden) {
      radky.push('na počítači s jiným předplatným ' + dolary(pc.jinde_tyden) + ' — do limitu se nepočítá');
    }
    return radky.join(' · ');
  }

  async function kresliTym(box) {
    let d;
    try {
      d = await gwSpotreba();
      // Brána krátce počítá na pozadí — chvíli počkat, než čísla přijdou.
      for (let i = 0; d.pocita && i < 40 && root; i++) {
        await new Promise((r) => setTimeout(r, 1500));
        d = await gwSpotreba();
      }
    } catch (_) { return; }               // mimo server /gw/spotreba není
    if (!root || !d || !d.lide) return;
    tymData = d;
    box.textContent = '';
    const admin = !!d.admin;
    const lide = d.admin ? d.lide : d.lide.filter((r) => limitText(r));
    if (!lide.length) return;
    const s = section(admin ? 'Tým — spotřeba za 7 dní' : 'Tvůj týdenní limit',
      admin ? 'Odhad podle ceníku API (server i počítače na stejném předplatném); na předplatném se neplatí. ' +
              'Limit upozorní, se stopkou zastaví nové zprávy Claudovi. ' +
              (d.stari != null && d.stari > 90 ? 'Stav před ' + Math.round(d.stari / 60) + ' min.' : '')
            : 'Správce ti nastavil týdenní limit, ať společný limit nevyčerpá jeden člověk.');
    const max = Math.max(0.01, ...lide.map((r) => Math.max(r.tyden, r.limit || 0)));
    const list = el('div', 'st-tym-rows');
    const prekresli = () => kresliTym(box);
    for (const r of lide) {
      const row = el('div', 'st-tym-row' + (r.stav ? ' ' + r.stav : '') + (r.stopka ? ' stopka' : ''));
      const name = el('span', 'st-row-name', r.name || r.email);
      name.title = r.email;
      row.appendChild(name);
      const track = el('span', 'st-track');
      const fill = el('span', 'st-fill');
      fill.style.width = Math.max(1, r.tyden / max * 100) + '%';
      track.appendChild(fill);
      // Díl z počítače jako světlejší konec téhož pruhu.
      const pcDil = (r.pocitac || {}).tyden || 0;
      if (pcDil > 0 && r.tyden > 0) {
        const pc = el('span', 'st-fill-pc');
        pc.style.width = Math.max(0.5, pcDil / max * 100) + '%';
        pc.style.left = Math.max(0, (r.tyden - pcDil) / max * 100) + '%';
        track.appendChild(pc);
      }
      if (r.limit) {
        const znacka = el('span', 'st-limit-znacka');
        znacka.style.left = Math.min(100, r.limit / max * 100) + '%';
        track.appendChild(znacka);
      }
      row.appendChild(track);
      const val = el('span', 'st-row-val', dolary(r.tyden));
      row.appendChild(val);
      row.title = rozpad(r) + '\nDnes ' + dolary(r.dnes) + ' · ' + cislo(r.tokenu) + ' tokenů výstupu · ' +
        Math.round(r.podil * 100) + ' % týmu';
      const p = r.procenta || {};
      const pct = el('span', 'st-tym-pct', p.pct != null ? Math.round(p.pct) + ' %' + (p.zdroj === 'tym' ? '*' : '') : '');
      pct.title = p.pct != null ? 'Týden: ' + pctZdroj(p) : '';
      row.appendChild(pct);
      let stavText = STAV_TEXT[r.stav] || '';
      if (r.stopka) stavText = 'stopka';
      else if (r.vyjimka) stavText = 'výjimka';
      const stav = el('span', 'st-tym-stav', stavText);
      if (r.stopka) stav.title = 'Nové zprávy Claudovi neodejdou' + (r.stopka.do ? ' — uvolní se ' + kdyText(r.stopka.do) : '') + '.';
      else if (r.vyjimka) stav.title = 'Povoleno do konce týdne (' + kdyText(r.vyjimka.do) + ')';
      row.appendChild(stav);
      // Kolikrát už přes strop přešel (jen když nějaký má).
      const kolikrat = el('span', 'st-tym-preslo', limitText(r) ? 'přes limit ' + (r.preslo || 0) + '×' : '');
      if (r.preslo_kdy) kolikrat.title = 'Naposledy ' + r.preslo_kdy;
      row.appendChild(kolikrat);
      list.appendChild(row);
      if (admin) {
        const btn = el('button', 'st-tym-limit' + (limitText(r) ? '' : ' prazdny'), limitText(r) || 'nastavit limit');
        btn.type = 'button';
        btn.title = 'Týdenní limit: USD, procenta předplatného, tokeny a stopka';
        row.appendChild(btn);
        // Stopka: řádek pod člověkem — do kdy platí a jedním klikem pustit dál.
        if (r.stopka) {
          const sub = el('div', 'st-tym-sub', 'Stopka — nové zprávy Claudovi neodejdou' +
            (r.stopka.do ? ', sama se uvolní ' + kdyText(r.stopka.do) : '') + '.');
          const pust = el('button', 'st-tym-povolit', 'Povolit do konce týdne');
          pust.type = 'button';
          pust.title = 'Stopka se do neděle nezapne; limit dál ukazuje, kolik čerpá.';
          pust.onclick = async () => {
            pust.disabled = true;
            try { await gwSpotreba({id: r.id, akce: 'povolit'}); prekresli(); } catch (err) { pust.disabled = false; pust.title = err.message; }
          };
          sub.appendChild(pust);
          list.appendChild(sub);
        }
        const pop = limitPanel(r, prekresli);
        pop.hidden = true;
        list.appendChild(pop);
        btn.onclick = () => {
          const otevreny = !pop.hidden;
          for (const o of list.querySelectorAll('.st-tym-pop')) o.hidden = true;
          pop.hidden = otevreny;
          if (!pop.hidden) { const f = pop.querySelector('input'); if (f) f.focus(); }
        };
      } else {
        row.appendChild(el('span', 'st-tym-limit-txt', 'limit ' + limitText(r)));
        if (r.stopka) list.appendChild(el('div', 'set-note st-tym-stopka-txt', r.stopka.text));
      }
    }
    s.appendChild(list);
    box.appendChild(s);

    // Správce: kdo je jak blízko skutečnému limitu předplatného.
    const sPlan = lide.filter((r) => r.plan && r.plan.ok);
    if (admin && sPlan.length) {
      const p = section('Tým — limit předplatného', 'Skutečná procenta limitu od Anthropic u těch, kdo mají v prostoru předplatné ' +
        '(připojené z appky, nebo přihlášené přímo v prostoru). Zahrnují i počítač na stejném předplatném.');
      const rows = el('div', 'st-tym-rows');
      for (const r of sPlan) {
        const row = el('div', 'st-tym-row');
        row.appendChild(el('span', 'st-row-name', r.name || r.email));
        for (const [nazev, okno] of [['5 h', r.plan.session], ['týden', r.plan.week]]) {
          if (!okno) continue;
          const track = el('span', 'st-track');
          const fill = el('span', 'st-fill');
          fill.style.width = Math.max(1, Math.min(100, okno.pct)) + '%';
          if (okno.pct >= 95) fill.style.background = 'var(--red)';
          else if (okno.pct >= 80) fill.style.background = 'var(--accent)';
          track.appendChild(fill);
          row.appendChild(track);
          const v = el('span', 'st-row-val', nazev + ' ' + Math.round(okno.pct) + ' %');
          v.title = zbyva(okno.resets) + (r.plan.stare ? ' (poslední známý stav)' : '');
          row.appendChild(v);
        }
        rows.appendChild(row);
      }
      p.appendChild(rows);
      box.appendChild(p);
    }
  }

  /* Rozbalovací panel s limity jednoho člověka (jen správce): tři pole,
     stopka a výjimka. Prázdné pole = ten druh limitu nemá. */
  function limitPanel(r, hotovo) {
    const l = r.limity || {usd: r.limit || 0, pct: 0, tokeny: 0, stopka: false};
    const pop = el('div', 'st-tym-pop');
    const pole = (popis, klic, krok, napoveda) => {
      const lab = el('label', 'st-tym-pole');
      lab.appendChild(el('span', '', popis));
      const inp = el('input', 'set-input');
      inp.type = 'number'; inp.min = '0'; inp.step = krok; inp.inputMode = 'decimal';
      inp.value = l[klic] || '';
      inp.placeholder = 'bez limitu';
      inp.name = klic;
      lab.title = napoveda;
      lab.appendChild(inp);
      pop.appendChild(lab);
      return inp;
    };
    const usd = pole('USD za týden', 'usd', '10', 'Odhad podle ceníku API ze serveru i z počítače na stejném předplatném.');
    const pct = pole('% předplatného', 'pct', '5', pctZdroj(r.procenta) || 'Kolik procent týdenního limitu předplatného smí vyčerpat.');
    const tok = pole('tokenů za týden', 'tokeny', '100000', 'Napsané tokeny (výstup Clauda) za posledních 7 dní.');
    const stop = el('label', 'st-tym-stopka');
    const chk = el('input');
    chk.type = 'checkbox';
    chk.checked = !!l.stopka;
    stop.append(chk, el('span', '', 'Stopka — po překročení nepustit nové zprávy Claudovi (rozdělaná práce doběhne)'));
    pop.appendChild(stop);
    if (r.procenta && r.procenta.zdroj === 'tym') {
      pop.appendChild(el('div', 'set-note', 'Jede na klíči API, takže procenta předplatného nemá — limit v procentech ' +
        'se u něj počítá z podílu na spotřebě celého týmu (teď ' + Math.round(r.procenta.pct || 0) + ' %).'));
    }
    const chyba = el('div', 'set-status warn');
    chyba.hidden = true;
    const tlac = el('div', 'st-tym-tlacitka');
    const ulozit = el('button', 'btn primary', 'Uložit');
    ulozit.type = 'button';
    ulozit.onclick = async () => {
      ulozit.disabled = true;
      try {
        await gwSpotreba({id: r.id, usd: usd.value, pct: pct.value, tokeny: tok.value, stopka: chk.checked});
        hotovo();
      } catch (err) {
        chyba.textContent = err.message;
        chyba.hidden = false;
        ulozit.disabled = false;
      }
    };
    tlac.appendChild(ulozit);
    if (r.vyjimka) {
      const zrus = el('button', 'btn ghost', 'Zrušit výjimku');
      zrus.type = 'button';
      zrus.title = 'Povoleno do ' + kdyText(r.vyjimka.do) + (r.vyjimka.kdo ? ' (' + r.vyjimka.kdo + ')' : '');
      zrus.onclick = async () => {
        try { await gwSpotreba({id: r.id, akce: 'zrusit-vyjimku'}); hotovo(); } catch (err) { chyba.textContent = err.message; chyba.hidden = false; }
      };
      tlac.appendChild(zrus);
    } else if (r.stav === 'nad' || r.stopka) {
      const pust = el('button', 'btn ghost', 'Povolit do konce týdne');
      pust.type = 'button';
      pust.onclick = async () => {
        try { await gwSpotreba({id: r.id, akce: 'povolit'}); hotovo(); } catch (err) { chyba.textContent = err.message; chyba.hidden = false; }
      };
      tlac.appendChild(pust);
    }
    pop.append(tlac, chyba);
    pop.addEventListener('keydown', (ev) => { if (ev.key === 'Enter' && ev.target.tagName === 'INPUT') ulozit.click(); });
    const info = el('div', 'set-note st-tym-rozpad', rozpad(r) + (r.stopka && r.stopka.do ? ' · stopka se sama uvolní ' + kdyText(r.stopka.do) : ''));
    pop.appendChild(info);
    return pop;
  }

  /* ── export ────────────────────────────────────────────────────────────
     CSV pro Excel (středník, čárka v desetinných číslech, BOM, ať Excel
     pozná UTF-8 a diakritiku) a celé statistiky jako JSON. Tým jen správce
     na serveru — z téhož, co ukazuje sekce Tým. */
  let tymData = null;
  function csv(radky) {
    const bunka = (v) => {
      if (typeof v === 'number') return String(Math.round(v * 10000) / 10000).replace('.', ',');
      const t = String(v == null ? '' : v);
      return /[;"\n\r]/.test(t) ? '"' + t.replace(/"/g, '""') + '"' : t;
    };
    return '\ufeff' + radky.map((r) => r.map(bunka).join(';')).join('\r\n') + '\r\n';
  }
  function stahniSoubor(jmeno, obsah, typ) {
    const url = URL.createObjectURL(new Blob([obsah], {type: typ}));
    const a = document.createElement('a');
    a.href = url;
    a.download = jmeno;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 5000);
  }
  function exportMenu() {
    const menu = root.querySelector('.st-export-menu');
    if (!menu.hidden) { menu.hidden = true; return; }
    if (!data) { io.toast('Statistiky se ještě počítají.'); return; }
    const den = new Date().toISOString().slice(0, 10);
    const volby = [
      ['Po dnech (CSV)', () => stahniSoubor(`aladin-statistiky-dny-${den}.csv`, csv([
        ['Datum', 'Zprávy', 'Napsané tokeny'],
        ...(data.days || []).map((d) => [d.day, d.prompts, d.out]),
      ]), 'text/csv;charset=utf-8')],
      ['Po projektech (CSV)', () => stahniSoubor(`aladin-statistiky-projekty-${den}.csv`, csv([
        ['Projekt', 'Cesta', 'Zprávy', 'Odpovědi', 'Tokeny vstup', 'Tokeny výstup',
         'Cache zápis', 'Cache čtení', 'Odhad ceny USD'],
        ...(data.projects || []).map((p) => [p.name, p.path, p.prompts, p.answers, p.in, p.out,
                                            p.cache_w, p.cache_r, p.cost]),
      ]), 'text/csv;charset=utf-8')],
      ['Modely (CSV)', () => stahniSoubor(`aladin-statistiky-modely-${den}.csv`, csv([
        ['Model', 'Odhad ceny USD'],
        ...Object.entries(data.cost_by_model || {}).map(([m, c]) => [m, c]),
      ]), 'text/csv;charset=utf-8')],
    ];
    if (tymData && tymData.admin && (tymData.lide || []).length) {
      volby.push(['Tým za 7 dní (CSV)', () => stahniSoubor(`aladin-tym-${den}.csv`, csv([
        ['Jméno', 'E-mail', 'Týden USD', 'Z toho na počítači USD', 'Dnes USD', 'Tokeny výstupu', 'Podíl týmu %',
         'Týden % předplatného', 'Limit', 'Stav', 'Přes limit (krát)'],
        ...tymData.lide.map((r) => [r.name || '', r.email, r.tyden, (r.pocitac || {}).tyden || 0, r.dnes, r.tokenu,
                                    Math.round((r.podil || 0) * 1000) / 10,
                                    r.procenta && r.procenta.pct != null ? r.procenta.pct : '', limitText(r),
                                    r.stopka ? 'stopka' : r.vyjimka ? 'výjimka do konce týdne' : STAV_TEXT[r.stav] || '',
                                    r.preslo || 0]),
      ]), 'text/csv;charset=utf-8')]);
    }
    volby.push(['Všechno (JSON)', () => stahniSoubor(`aladin-statistiky-${den}.json`,
      JSON.stringify({vygenerovano: new Date().toISOString(), statistiky: data,
                      tym: tymData && tymData.admin ? tymData.lide : undefined}, null, 2),
      'application/json')]);
    menu.textContent = '';
    for (const [nazev, run] of volby) {
      const b = el('button', 'st-export-volba', nazev);
      b.type = 'button';
      b.onclick = () => { menu.hidden = true; run(); };
      menu.appendChild(b);
    }
    menu.hidden = false;
    const zavri = (ev) => {
      if (menu.contains(ev.target)) return;
      menu.hidden = true;
      document.removeEventListener('click', zavri, true);
    };
    document.addEventListener('click', zavri, true);
  }

  function close() {
    if (root) root.remove();
    root = null;
  }

  /* ── statistiky jednoho projektu ─────────────────────────────────────────
     Tokeny, odhad ceny, modely, dny a nejdražší sezení. Cena je podle ceníku
     API (hub/pricing.py) — na předplatném se po tokenech neplatí, je to míra,
     co projekt „váží". */
  const dolary = (c) => {
    c = Number(c) || 0;
    if (c >= 100) return Math.round(c).toLocaleString('cs-CZ') + ' $';
    if (c >= 1) return c.toFixed(1).replace('.', ',') + ' $';
    return c.toFixed(2).replace('.', ',') + ' $';
  };
  const datum = (iso) => (iso ? new Date(iso).toLocaleDateString('cs-CZ') : '–');
  const kratce = (iso) => (iso ? new Date(iso).toLocaleDateString('cs-CZ', {day: 'numeric', month: 'numeric'}) : '–');
  const jmenoModelu = (m) => String(m || '?').replace(/^claude-/, '').replace(/-(\d{8})$/, '')
    .replace(/-(\d+)-(\d+)$/, ' $1.$2').replace(/-(\d+)$/, ' $1').replace(/^\w/, (c) => c.toUpperCase());

  async function openProject(opts, project) {
    const wrap = el('div', 'onb st-projekt');
    wrap.innerHTML = `
      <div class="onb-box st-box">
        <div class="onb-head">
          <div><div class="onb-title"></div><div class="onb-sub">Využití tokenů a odhad ceny</div></div>
          <span class="spacer"></span>
          <button class="set-x" title="Zavřít">×</button>
        </div>
        <div class="onb-body"></div>
      </div>`;
    wrap.querySelector('.onb-title').textContent = project.label || project.name;
    const zavri = () => wrap.remove();
    wrap.querySelector('.set-x').onclick = zavri;
    let downOutside = false;
    wrap.addEventListener('pointerdown', (ev) => { downOutside = ev.target === wrap; });
    wrap.addEventListener('click', (ev) => { if (ev.target === wrap && downOutside) zavri(); });
    document.body.appendChild(wrap);
    const body = wrap.querySelector('.onb-body');
    body.appendChild(el('div', 'set-status busy', 'Počítám…'));
    let d;
    try {
      d = await opts.api('project-stats?path=' + encodeURIComponent(project.path));
    } catch (err) {
      body.textContent = '';
      body.appendChild(el('div', 'set-status warn', 'Nepovedlo se: ' + err.message));
      return;
    }
    body.textContent = '';
    const t = d.tokens || {};
    if (!t.answers) {
      body.appendChild(el('div', 'set-note', 'U tohohle projektu zatím nejsou žádná sezení s Claudem.'));
      return;
    }
    const tiles = el('div', 'st-tiles');
    tiles.appendChild(tile(dolary(d.cost), 'odhad ceny', 'podle ceníku API'));
    tiles.appendChild(tile(cislo(t.out || 0), 'napsaných tokenů', `z toho ${cislo(t.think || 0)} přemýšlení`));
    tiles.appendChild(tile(cislo(d.prompts || 0), tvar(d.prompts || 0, 'zpráva', 'zprávy', 'zpráv'), `${cislo(t.answers)} odpovědí`));
    tiles.appendChild(tile(String(d.sessions_count || 0), tvar(d.sessions_count || 0, 'sezení', 'sezení', 'sezení'),
      `${d.active_days || 0} dnů s prací`));
    body.appendChild(tiles);

    const s1 = section('Tokeny', 'Z čeho se cena skládá.');
    const rows = [
      ['Vstup (nový)', t.in, 'tokeny, které model četl poprvé'],
      ['Výstup', t.out, 'co Claude napsal a promyslel'],
      ['Zápis do cache', t.cache_w, 'kontext uložený pro další zprávy'],
      ['Čtení z cache', t.cache_r, 'kontext znovu použitý (levné)'],
    ];
    const max = Math.max(1, ...rows.map((r) => r[1] || 0));
    const list = el('div', 'st-rows');
    for (const [name, val, note] of rows) {
      const row = el('div', 'st-row');
      row.appendChild(el('span', 'st-row-name', name));
      const track = el('span', 'st-track');
      const fill = el('span', 'st-fill');
      fill.style.width = Math.max(1, (val || 0) / max * 100) + '%';
      track.appendChild(fill);
      row.appendChild(track);
      row.appendChild(el('span', 'st-row-val', cislo(val || 0)));
      row.title = note;
      list.appendChild(row);
    }
    s1.appendChild(list);
    body.appendChild(s1);

    if ((d.models || []).length) {
      const s2 = section('Modely', 'Odhad ceny podle toho, čím Claude odpovídal.');
      const l2 = el('div', 'st-rows');
      const mc = Math.max(0.01, ...d.models.map((m) => m.cost));
      for (const m of d.models) {
        const row = el('div', 'st-row');
        row.appendChild(el('span', 'st-row-name', jmenoModelu(m.model)));
        const track = el('span', 'st-track');
        const fill = el('span', 'st-fill');
        fill.style.width = Math.max(1, m.cost / mc * 100) + '%';
        track.appendChild(fill);
        row.appendChild(track);
        row.appendChild(el('span', 'st-row-val', dolary(m.cost)));
        row.title = `${m.model}\n${cislo(m.out)} tokenů výstupu · ${m.answers} odpovědí`;
        l2.appendChild(row);
      }
      s2.appendChild(l2);
      body.appendChild(s2);
    }

    const dny = (d.days || []);
    if (dny.length) {
      const s3 = section('Cena po dnech', `Posledních ${dny.length} dnů s prací.`);
      s3.appendChild(bars(dny.map((x, i, a) => ({
        value: x.cost, label: x.day, full: x.day, tick: i === 0 || i === a.length - 1 ? x.day.slice(5) : '',
      })), {unit: ' $'}));
      body.appendChild(s3);
    }

    if ((d.sessions || []).length) {
      const s4 = section('Nejdražší sezení', 'Jedno sezení = jeden rozhovor s Claudem.');
      const l4 = el('div', 'st-rows');
      for (const s of d.sessions.slice(0, 6)) {
        const row = el('div', 'st-row');
        const nm = el('span', 'st-row-name', kratce(s.first) + (s.last && s.last.slice(0, 10) !== (s.first || '').slice(0, 10) ? ' – ' + kratce(s.last) : ''));
        nm.style.minWidth = '110px';
        row.appendChild(nm);
        row.appendChild(el('span', 'st-track'));
        row.appendChild(el('span', 'st-row-val', dolary(s.cost)));
        row.title = `${cislo(s.out)} tokenů · ${s.answers} odpovědí\n${s.id}`;
        l4.appendChild(row);
      }
      s4.appendChild(l4);
      body.appendChild(s4);
    }
    body.appendChild(el('div', 'set-note',
      `${datum(d.first)} – ${datum(d.last)}. ${d.note || ''}`));
  }

  global.HubStats = {open, close, openProject, dolary, cislo, plan, planNaUvodu};

})(window);
