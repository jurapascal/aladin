/* Zkouška appky na tomhle systému:
 *   1. naběhne hub, otevře se okno; Nové okno = druhé okno téhož hubu, sdílené
 *      taby; odkazy jdou ven; zavření oken ukončí i hub,
 *   2. hub už běží → appka se jen připojí; 2b. hub appku pustí s --hub-url,
 *   3. starší hub bez --vlastni-okno → appka ho spustí s --no-browser a sama zavře,
 *   4. aktualizace hubu za běhu → okna přejdou na novou instanci (i když
 *      nová naběhne pomalu — pak je přepne hlídač appky),
 *   5. starší hub bez značky už běží → appka nespustí druhý (ne na Windows).
 *
 *     cd desktop && npm install && npm test
 *
 * Používá hub z repa a prázdnou dočasnou konfiguraci (CLAUDE_CONFIG_DIR), takže
 * nesahá na nainstalovaný hub ani na ~/.claude. Linux bez displeje:
 * xvfb-run -a npm test. Zabalená appka: HUB_DESKTOP_EXE=<spustitelný soubor> npm test
 */
import { _electron as electron } from 'playwright';
import { execFileSync, spawn } from 'child_process';
import fs from 'fs';
import os from 'os';
import path from 'path';
import { fileURLToPath } from 'url';

const here = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(here, '..', '..');
const python = process.env.HUB_PYTHON || (process.platform === 'win32' ? 'python' : 'python3');
const pauza = (ms) => new Promise((r) => setTimeout(r, ms));
const zije = (pid) => { try { process.kill(pid, 0); return true; } catch (e) { return e.code === 'EPERM'; } };
const tmp = (j) => fs.mkdtempSync(path.join(os.tmpdir(), j));
// Dočasná konfigurace hubu: bez automatické aktualizace (stránka by jinak
// stahovala a instalovala novou verzi do dočasné složky).
const tmpClaude = () => {
  const d = tmp('hub-desktop-');
  fs.writeFileSync(path.join(d, 'hub-config.json'), JSON.stringify({ auto_update: false }));
  return d;
};
const vysledky = [];
const ok = (jmeno, podminka, detail = '') => {
  vysledky.push(!!podminka);
  console.log(`  ${podminka ? 'OK   ' : 'CHYBA'} ${jmeno}${detail ? '  (' + detail + ')' : ''}`);
};
const nacteno = (p) => p.waitForFunction(() => typeof openTab === 'function' && typeof STATE !== 'undefined'
  && !!STATE && typeof online === 'function' && online(), null, { timeout: 90000 });
const port = (u) => (u.match(/:(\d+)\//) || [])[1];

async function spust(claude, extraEnv = {}, extraArgs = []) {
  const ud = tmp('hub-desktop-ud-');
  const app = await electron.launch({
    ...(process.env.HUB_DESKTOP_EXE ? { executablePath: process.env.HUB_DESKTOP_EXE } : {}),
    args: [...(process.env.HUB_DESKTOP_EXE ? [] : [path.join(here, '..', 'main.js')]), `--user-data-dir=${ud}`,
      ...(process.env.CI ? ['--no-sandbox'] : []), ...extraArgs],
    env: { ...process.env, CLAUDE_CONFIG_DIR: claude, HUB_LAUNCHER: path.join(root, 'claude-hub.py'),
      HUB_PYTHON: python, HUB_DESKTOP_TEST: '1', ...extraEnv },
  });
  app.__ud = ud;
  return app;
}
// Při selhání: co přesně běží pod danou konfigurací (příkazy, rodiče).
const vypisProcesy = (claude) => {
  const pids = hubyVe(claude);
  if (!pids.length || process.platform === 'win32') { console.log('    procesy hubu: žádné'); return; }
  try {
    const out = String(execFileSync('ps', ['-o', 'pid=,ppid=,etime=,command=', '-p', pids.join(',')], { encoding: 'utf8' }));
    console.log('    procesy hubu:\n' + out.trim().split('\n').map((l) => '      ' + l.slice(0, 200)).join('\n'));
    const deti = String(execFileSync('ps', ['-axo', 'pid=,ppid=,etime=,command='], { encoding: 'utf8' })).split('\n')
      .filter((l) => pids.includes(parseInt(l.trim().split(/\s+/)[1], 10)));
    if (deti.length) console.log('    jejich podprocesy:\n' + deti.map((l) => '      ' + l.trim().slice(0, 200)).join('\n'));
    // Kde visí Python hubu (py-spy, jen v CI): zásobníky vláken.
    if (process.env.HUB_PYSPY) {
      for (const l of out.trim().split('\n').filter((x) => x.includes('claude-hub.py'))) {
        const pid = l.trim().split(/\s+/)[0];
        try { console.log(String(execFileSync('sudo', ['-n', 'py-spy', 'dump', '--pid', pid], { encoding: 'utf8', timeout: 60000 })).slice(0, 5000)); }
        catch (x) { console.log('    py-spy selhal:', String(x.message).slice(0, 200)); }
      }
    }
  } catch (e) { console.log('    ps selhal', String(e.message).slice(0, 80)); }
};
// Procesy hubu s danou dočasnou konfigurací (podle prostředí procesu) — po
// scénáři se uklidí, ať selhaný scénář neovlivní další. Na Windows ps není.
function hubyVe(claude) {
  if (process.platform === 'win32') return [];
  const args = process.platform === 'darwin' ? ['-axEww', '-o', 'pid=,command='] : ['axeww', '-o', 'pid=,command='];
  const out = String(execFileSync('ps', args, { encoding: 'utf8', maxBuffer: 64 << 20 }));
  return out.split('\n').filter((l) => l.includes('claude-hub.py') && l.includes(`CLAUDE_CONFIG_DIR=${claude}`))
    .map((l) => parseInt(l, 10)).filter(Boolean);
}
async function konec(app, claude) {
  if (app) { await app.close().catch(() => {}); if (app.__ud) fs.rmSync(app.__ud, { recursive: true, force: true }); }
  for (const pid of hubyVe(claude)) { try { process.kill(pid); } catch (_) { /* pryč */ } }
  for (let i = 0; i < 5; i++) {                    // Windows soubory chvíli drží
    try { fs.rmSync(claude, { recursive: true, force: true }); break; } catch (_) { await pauza(1000); }
  }
}
async function cekejKonec(pid, ms = 40000) {
  const t0 = Date.now();
  while (zije(pid) && Date.now() - t0 < ms) await pauza(500);
  return Math.round((Date.now() - t0) / 1000);
}
const procesyHubu = (launcher) => String(execFileSync('ps', ['-axo', 'pid=,command='], { encoding: 'utf8' }))
  .split('\n').filter((l) => l.includes(launcher)).map((l) => parseInt(l, 10));
// Při selhání: konec logu hubu z dočasné složky (důvod bývá tam).
const vypisLog = (claude) => {
  try {
    const t = fs.readFileSync(path.join(claude, 'hub.log'), 'utf8').trim().split('\n')
      .filter((l) => !/úloha (stats|chats)/.test(l)).slice(-25);
    console.log('    hub.log:\n' + t.map((l) => '      ' + l).join('\n'));
  } catch (_) { console.log('    (hub.log není)'); }
};
// „Claude pracuje": gesto (beforeunload bez něj Chromium nebere) a stejný
// beforeunload jako hub — při přechodu na novou instanci se ptát nesmí.
const prace = async (okno) => {
  await okno.mouse.click(4, 4);
  await okno.evaluate(() => addEventListener('beforeunload', (e) => {
    if (window.HUB_LEAVING) return;
    e.preventDefault(); e.returnValue = '';
  }));
};
// Menu v proužku nahoře (hub.js, appMenu) — na Macu je menu v systému.
const noveOknoZMenu = async (app, okno) => {
  if (process.platform === 'darwin') return klikniMenu(app, 'Nové okno');
  await okno.click('#appmenu .appmenu-btn >> text=Soubor');
  await okno.click('.appmenu-drop .appmenu-item >> text=Nové okno');
};
const klikniMenu = (app, label) => app.evaluate(({ Menu }, label) => {
  const find = (items) => { for (const i of items) { if (i.label === label) return i; if (i.submenu) { const x = find(i.submenu.items); if (x) return x; } } };
  find(Menu.getApplicationMenu().items).click();
}, label);

/* ── 1. základ ─────────────────────────────────────────────────────────── */
{
  const claude = tmpClaude();
  let app = null, hubPid = 0;
  try {
    console.log('\n── 1. okno, Nové okno, odkazy, konec ──');
    app = await spust(claude, { HUB_ARGS: '--test' });
    const okno1 = await app.firstWindow({ timeout: 90000 });
    const chybyStranky = [];
    okno1.on('pageerror', (e) => chybyStranky.push('pageerror: ' + String(e).slice(0, 200)));
    okno1.on('console', (m) => { if (m.type() === 'error' || m.type() === 'warning') chybyStranky.push(m.type() + ': ' + m.text().slice(0, 200)); });
    globalThis.__okno1 = okno1; globalThis.__chybyStranky = chybyStranky;
    await nacteno(okno1);
    const jmeno = await app.evaluate(({ app: a }) => a.getName());
    ok('název aplikace je Aladin', jmeno === 'Aladin', jmeno);
    ok('okno ukazuje hub (adresa 127.0.0.1)', /^http:\/\/127\.0\.0\.1:\d+\//.test(okno1.url()), okno1.url().replace(/\?.*/, ''));
    hubPid = await app.evaluate(() => global.hubDesktop.hubPid());
    ok('hub běží jako samostatný proces s --vlastni-okno', hubPid > 0 && zije(hubPid)
      && (await app.evaluate(() => global.hubDesktop.hubSam())), String(hubPid));

    const menu = await app.evaluate(({ Menu }) => {
      const out = [];
      const proj = (items) => items.forEach((i) => { if (i.label) out.push(`${i.label}|${i.accelerator || ''}`); if (i.submenu) proj(i.submenu.items); });
      proj(Menu.getApplicationMenu().items);
      return out;
    });
    const mac = process.platform === 'darwin';
    ok(`menu má Nové okno na ${mac ? 'Cmd+N' : 'Ctrl+Shift+N'}`, menu.includes(`Nové okno|${mac ? 'Cmd+N' : 'Ctrl+Shift+N'}`));
    ok(`Zavřít okno na ${mac ? 'Cmd+W' : 'Ctrl+Shift+W (Ctrl+W patří terminálu)'}`, menu.includes(`Zavřít okno|${mac ? 'Cmd+W' : 'Ctrl+Shift+W'}`));
    ok('menu má Úpravy (kopírovat/vložit)', menu.some((m) => m.startsWith('Kopírovat')) && menu.some((m) => m.startsWith('Vložit')));
    ok('v menu není nic z Chromu (History, Bookmarks)', !menu.some((m) => /^(History|Bookmarks|Profiles)/.test(m)));
    if (!mac) {
      // Nativní menu má na Linuxu a Windows rozbalené položky velké jako pro
      // prst — hub ho kreslí sám jako proužek nahoře a nativní lišta se schová.
      await okno1.waitForFunction(() => !document.getElementById('appmenu').hidden, null, { timeout: 15000 });
      await pauza(4500);                       // pojistka pro starší hub čeká 4 s
      ok('menu je proužek nahoře v hubu, nativní lišta schovaná',
        !(await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].isMenuBarVisible()))
        && (await okno1.evaluate(() => document.getElementById('appmenu').getBoundingClientRect().top)) === 0);
      await okno1.click('#appmenu .appmenu-btn >> text=Soubor');
      const vysky = await okno1.evaluate(() => ({
        btn: document.querySelector('#appmenu .appmenu-btn').getBoundingClientRect().height,
        pol: [...document.querySelectorAll('.appmenu-drop .appmenu-item')].map((b) => b.getBoundingClientRect().height),
      }));
      ok('rozbalené položky stejně vysoké jako tlačítko Soubor', vysky.pol.length > 0
        && vysky.pol.every((h) => Math.abs(h - vysky.btn) < 0.5), `${vysky.btn} px / ${vysky.pol.join(', ')} px`);
      await okno1.keyboard.press('Escape');
    }

    // Odkazy ještě bez tabu: běžící tab hub drží beforeunload a přechod na cizí
    // stránku by se ptal (to zkouší scénář 1b), Playwright by se o dialog přetahoval.
    const pred = okno1.url();
    await okno1.evaluate(() => { window.open('https://example.com/zkouska', '_blank'); });
    await pauza(600);
    ok('odkaz (window.open) jde ven do prohlížeče', (await app.evaluate(() => global.hubDesktop.ven())).includes('https://example.com/zkouska'));
    await okno1.evaluate(() => { location.href = 'https://example.org/'; });
    for (let i = 0; i < 25 && !(await app.evaluate(() => global.hubDesktop.ven())).includes('https://example.org/'); i++) await pauza(200);
    ok('okno hubu nepřejde na cizí stránku, adresa jde ven', okno1.url() === pred
      && (await app.evaluate(() => global.hubDesktop.ven())).includes('https://example.org/'));
    await okno1.evaluate(() => openTab({ kind: 'shell', path: '', title: 'sdileny' }));
    await okno1.waitForFunction(() => TABS.length === 1 && !!TABS[0].id, null, { timeout: 60000 });
    await klikniMenu(app, 'Nové okno');
    for (let i = 0; i < 40 && app.windows().length < 2; i++) await pauza(250);
    ok('Nové okno otevře druhé okno hubu', app.windows().length === 2, `oken: ${app.windows().length}`);
    const okno2 = app.windows().find((w) => w !== okno1);
    await nacteno(okno2);
    ok('druhé okno ukazuje tentýž hub', port(okno2.url()) === port(okno1.url()));
    await okno2.waitForFunction(() => TABS.length === 1, null, { timeout: 30000 });
    ok('druhé okno vidí tab z prvního (sdílené taby)', true);
    ok('druhé okno nespustilo druhý hub', (await app.evaluate(() => global.hubDesktop.hubPid())) === hubPid);


    // Tab nejdřív zavřít: běžící tab hub drží přes beforeunload a ten tu obslouží
    // appka vlastním dialogem (zkouší ho scénář 1b) — Playwright by se o něj přetahoval.
    await okno1.evaluate(() => { for (const t of [...TABS]) closeTab(t); });
    await pauza(500);
    await Promise.all(app.windows().map((w) => w.close().catch(() => {})));
    const s = await cekejKonec(hubPid);
    ok('po zavření oken skončí i hub', !zije(hubPid), `${s} s`);
  } catch (e) {
    ok('scénář 1 doběhl bez výjimky', false, String(e.message || e).split('\n')[0]);
    vypisLog(claude);
    try {
      console.log('    stránka:', JSON.stringify(await globalThis.__okno1.evaluate(() => ({
        tabs: TABS.map((t) => ({ ref: t.ref, id: t.id, kind: t.kind, exited: t.exited })),
        ws: WS && WS.readyState, online: online(),
        toast: (document.getElementById('toast') || {}).innerText || '',
      }))));
      console.log('    chyby stránky:', JSON.stringify((globalThis.__chybyStranky || []).slice(-12)));
      // stderr hubu (výjimky ve vláknech serveru) — appka ho píše do hub-vystup.log.
      try {
        const v = fs.readFileSync(path.join(app.__ud, 'hub-vystup.log'), 'utf8').trim().split('\n').slice(-40);
        console.log('    hub-vystup.log:\n' + v.map((l) => '      ' + l.slice(0, 220)).join('\n'));
      } catch (x) { console.log('    hub-vystup.log nečitelný:', String(x.message).slice(0, 100)); }
      // Zná server tab? Vlastní websocket a „hello" → seznam sessions.
      console.log('    server zná:', await globalThis.__okno1.evaluate(() => new Promise((hotovo) => {
        const ws = new WebSocket(`ws://${location.host}/ws?t=${encodeURIComponent(TOKEN)}`);
        ws.onopen = () => ws.send(JSON.stringify({ t: 'hello' }));
        ws.onmessage = (m) => { try { const d = JSON.parse(m.data); if (d.t === 'sessions') { hotovo(JSON.stringify(d.list.map((x) => ({ id: x.id, kind: x.kind })))); ws.close(); } } catch (_) { /* jiná zpráva */ } };
        setTimeout(() => hotovo('(bez odpovědi)'), 5000);
      })));
    } catch (x) { console.log('    stránka nečitelná:', String(x.message).slice(0, 120)); }
    // Kde hub visí: zásobníky všech vláken (py-spy, jen když je k dispozici).
    if (hubPid && process.env.HUB_PYSPY) {
      try {
        const d = String(execFileSync('py-spy', ['dump', '--pid', String(hubPid)], { encoding: 'utf8', timeout: 60000 }));
        // Jen vlákna a jejich nejvnitřnější rámec — celý výpis by se nevešel.
        const radky = d.split('\n'), out = [];
        radky.forEach((l, i) => { if (/^Thread /.test(l)) out.push(l + '  ←  ' + (radky[i + 1] || '').trim()); });
        console.log('    vlákna hubu:\n' + out.map((l) => '      ' + l.slice(0, 180)).join('\n'));
      }
      catch (x) { console.log('    py-spy selhal:', String(x.message).slice(0, 200)); }
    }
  } finally {
    if (hubPid && zije(hubPid)) { try { process.kill(hubPid); } catch (_) { /* pryč */ } }
    await konec(app, claude);
  }
}

/* ── 1b. zavření, když Claude pracuje (bez Playwrightu) ─────────────────── */
if (!process.env.HUB_DESKTOP_EXE) {
  const claude = tmpClaude();
  const ud = tmp('hub-desktop-ud-');
  const vysledek = path.join(ud, 'zavreni.json');
  try {
    console.log('\n── 1b. zavření okna, když Claude pracuje (beforeunload) ──');
    const { default: electronBin } = await import('electron');
    const p = spawn(electronBin, [path.join(here, 'zavreni.cjs'), `--user-data-dir=${ud}`, ...(process.env.CI ? ['--no-sandbox'] : [])], {
      env: { ...process.env, CLAUDE_CONFIG_DIR: claude, HUB_LAUNCHER: path.join(root, 'claude-hub.py'), HUB_PYTHON: python,
        HUB_ARGS: '--test', HUB_DESKTOP_TEST: '1', HUB_ZAVRENI_VYSLEDEK: vysledek },
      stdio: 'ignore', windowsHide: true });
    for (let i = 0; i < 360 && p.exitCode === null; i++) await pauza(250);
    let r = {};
    try { r = JSON.parse(fs.readFileSync(vysledek, 'utf8')); } catch (_) { /* nic */ }
    ok('okno, kde Claude pracuje, se po potvrzení zavře (nezasekne se)', r.zavreno === true, JSON.stringify(r));
    if (p.exitCode === null) p.kill();
  } catch (e) {
    ok('scénář 1b doběhl bez výjimky', false, String(e.message || e).split('\n')[0]);
    vypisLog(claude);
  } finally {
    await konec(null, claude);
    fs.rmSync(ud, { recursive: true, force: true });
  }
}

/* ── 2. hub už běží ────────────────────────────────────────────────────── */
{
  const claude = tmpClaude();
  const log = path.join(claude, 'vystup.log');
  const hub = spawn(python, [path.join(root, 'claude-hub.py'), '--vlastni-okno'],
    { env: { ...process.env, CLAUDE_CONFIG_DIR: claude }, stdio: ['ignore', fs.openSync(log, 'w'), 'ignore'], windowsHide: true });
  let app = null;
  try {
    console.log('\n── 2. připojení k už běžícímu hubu ──');
    let url = '';
    for (let i = 0; i < 300 && !url; i++) {
      await pauza(200);
      const m = fs.readFileSync(log, 'utf8').match(/^http:\/\/127\.0\.0\.1:\d+\/\S*/m);
      if (m) url = m[0];
    }
    for (let i = 0; i < 50 && !fs.existsSync(path.join(claude, 'hub-bezi.json')); i++) await pauza(200);
    ok('hub spuštěný zvlášť běží a zapsal hub-bezi.json', !!url && fs.existsSync(path.join(claude, 'hub-bezi.json')));
    app = await spust(claude);
    const w = await app.firstWindow({ timeout: 90000 });
    await nacteno(w);
    ok('appka se připojila ke stejné adrese a nespustila vlastní hub',
      port(w.url()) === port(url) && (await app.evaluate(() => global.hubDesktop.hubPid())) === 0);
    await w.close().catch(() => {});
    const s = await cekejKonec(hub.pid);
    ok('hub se sám ukončí po zavření stránky', !zije(hub.pid), `${s} s`);
  } catch (e) {
    ok('scénář 2 doběhl bez výjimky', false, String(e.message || e).split('\n')[0]);
    vypisLog(claude);
  } finally {
    try { process.kill(hub.pid); } catch (_) { /* pryč */ }
    await konec(app, claude);
  }
}

/* ── 2b. okno od hubu (--hub-url, hub/appka.py) ─────────────────────────── */
{
  const claude = tmpClaude();
  const log = path.join(claude, 'vystup.log');
  const hub = spawn(python, [path.join(root, 'claude-hub.py'), '--no-browser', '--test'],
    { env: { ...process.env, CLAUDE_CONFIG_DIR: claude }, stdio: ['ignore', fs.openSync(log, 'w'), 'ignore'], windowsHide: true });
  let app = null;
  try {
    console.log('\n── 2b. okno od hubu (--hub-url) ──');
    let url = '';
    for (let i = 0; i < 300 && !url; i++) {
      await pauza(200);
      const m = fs.readFileSync(log, 'utf8').match(/^http:\/\/127\.0\.0\.1:\d+\/\S*/m);
      if (m) url = m[0];
    }
    // Zkušební hub značku hub-bezi.json nepíše — appka musí jít jen podle adresy.
    app = await spust(claude, {}, [`--hub-url=${url}`]);
    const w = await app.firstWindow({ timeout: 90000 });
    await nacteno(w);
    ok('appka s --hub-url jde rovnou na hub, který ji spustil',
      port(w.url()) === port(url) && (await app.evaluate(() => global.hubDesktop.hubPid())) === 0);
    await w.close().catch(() => {});
  } catch (e) {
    ok('scénář 2b doběhl bez výjimky', false, String(e.message || e).split('\n')[0]);
    vypisLog(claude);
  } finally {
    try { process.kill(hub.pid); } catch (_) { /* pryč */ }
    await konec(app, claude);
  }
}

/* ── 3. starší hub bez --vlastni-okno ──────────────────────────────────── */
{
  const claude = tmpClaude();
  const stary = tmp('hub-stary-');
  let app = null, hubPid = 0;
  try {
    console.log('\n── 3. starší hub (nezná --vlastni-okno) ──');
    // Spouštěč bez přepínače (vyškrtnutý), nad stejným balíkem hub/.
    const puvodni = fs.readFileSync(path.join(root, 'claude-hub.py'), 'utf8').replaceAll('--vlastni-okno', '--neznamy-prepinac');
    fs.writeFileSync(path.join(stary, 'claude-hub.py'), puvodni);
    fs.cpSync(path.join(root, 'hub'), path.join(stary, 'hub'), { recursive: true });
    app = await spust(claude, { HUB_ARGS: '--test', HUB_LAUNCHER: path.join(stary, 'claude-hub.py') });
    const w = await app.firstWindow({ timeout: 90000 });
    await nacteno(w);
    hubPid = await app.evaluate(() => global.hubDesktop.hubPid());
    ok('okno naběhne i se starším hubem (--no-browser)', hubPid > 0 && !(await app.evaluate(() => global.hubDesktop.hubSam())));
    await app.close();
    app = null;
    const s = await cekejKonec(hubPid, 15000);
    ok('po ukončení appky je starší hub zavřený', !zije(hubPid), `${s} s`);
  } catch (e) {
    ok('scénář 3 doběhl bez výjimky', false, String(e.message || e).split('\n')[0]);
    vypisLog(claude);
  } finally {
    if (hubPid && zije(hubPid)) { try { process.kill(hubPid); } catch (_) { /* pryč */ } }
    await konec(app, claude);
    fs.rmSync(stary, { recursive: true, force: true });
  }
}

/* ── 4. aktualizace hubu za běhu ───────────────────────────────────────── */
{
  const claude = tmpClaude();
  let app = null, novyPid = 0, staryPid = 0;
  try {
    console.log('\n── 4. aktualizace hubu za běhu (restart na novou instanci) ──');
    app = await spust(claude);
    const okno1 = await app.firstWindow({ timeout: 90000 });
    await nacteno(okno1);
    staryPid = await app.evaluate(() => global.hubDesktop.hubPid());
    const pred = port(okno1.url());
    await prace(okno1);
    await okno1.evaluate(() => api('restart').catch(() => {}));      // jako po aktualizaci
    for (let i = 0; i < 120 && port(okno1.url()) === pred; i++) await pauza(250);
    ok('okno se samo přepnulo na novou instanci', port(okno1.url()) !== pred, `${pred} → ${port(okno1.url())}`);
    if (port(okno1.url()) === pred) { vypisLog(claude); vypisProcesy(claude); }
    await nacteno(okno1);
    const s = await cekejKonec(staryPid, 20000);
    ok('stará instance skončila, appka běží dál', !zije(staryPid) && app.windows().length === 1, `${s} s`);
    ok('přechod na novou instanci se při práci neptal „Opravdu odejít?"',
      !(await app.evaluate(() => global.hubDesktop.hlasky())).some((h) => /pracuje/.test(h)));
    if (process.platform !== 'darwin') {
      await okno1.waitForFunction(() => !document.getElementById('appmenu').hidden, null, { timeout: 15000 });
    }
    await noveOknoZMenu(app, okno1);
    for (let i = 0; i < 40 && app.windows().length < 2; i++) await pauza(250);
    const okno2 = app.windows().find((w) => w !== okno1);
    if (okno2) await nacteno(okno2);
    ok('Nové okno jde na novou instanci, ne na starou', !!okno2 && port(okno2.url()) === port(okno1.url()));
    ok('a nespustilo další hub', (await app.evaluate(() => global.hubDesktop.hubPid())) === 0);
    const bezi = JSON.parse(fs.readFileSync(path.join(claude, 'hub-bezi.json'), 'utf8'));
    novyPid = Number(bezi.pid);
    await Promise.all(app.windows().map((w) => w.close().catch(() => {})));
    const s2 = await cekejKonec(novyPid);
    ok('po zavření oken skončí i nová instance', !zije(novyPid), `${s2} s`);
  } catch (e) {
    ok('scénář 4 doběhl bez výjimky', false, String(e.message || e).split('\n')[0]);
    vypisLog(claude);
  } finally {
    for (const pid of [novyPid, staryPid]) if (pid && zije(pid)) { try { process.kill(pid); } catch (_) { /* pryč */ } }
    await konec(app, claude);
  }
}

/* ── 4b. aktualizace, nová instance naběhne pomalu ─────────────────────── */
{
  const claude = tmpClaude();
  let app = null, staryPid = 0, novyPid = 0;
  try {
    console.log('\n── 4b. aktualizace, nová instance naběhne pomalu (> 6 s) ──');
    // Restart pouští ~/.claude/claude-hub.py (tady dočasná složka): ten 9 s čeká.
    fs.writeFileSync(path.join(claude, 'claude-hub.py'), [
      'import os, runpy, sys, time',
      'time.sleep(9)',
      `cil = ${JSON.stringify(path.join(root, 'claude-hub.py'))}`,
      'sys.argv[0] = cil',
      'runpy.run_path(cil, run_name="__main__")', ''].join('\n'));
    app = await spust(claude);
    const okno1 = await app.firstWindow({ timeout: 90000 });
    await nacteno(okno1);
    staryPid = await app.evaluate(() => global.hubDesktop.hubPid());
    const pred = port(okno1.url());
    await prace(okno1);
    await okno1.evaluate(() => api('restart').catch(() => {}));
    for (let i = 0; i < 160 && port(okno1.url()) === pred; i++) await pauza(250);
    ok('okno přepnul hlídač appky na pomalou novou instanci', port(okno1.url()) !== pred && app.windows().length === 1,
      `${pred} → ${port(okno1.url())}`);
    if (port(okno1.url()) === pred) { vypisLog(claude); vypisProcesy(claude); }
    await nacteno(okno1);
    ok('stará instance skončila', !zije(staryPid));
    ok('přepnutí hlídačem se při práci neptalo „Opravdu odejít?"',
      !(await app.evaluate(() => global.hubDesktop.hlasky())).some((h) => /pracuje/.test(h)));
    novyPid = Number(JSON.parse(fs.readFileSync(path.join(claude, 'hub-bezi.json'), 'utf8')).pid);
    await okno1.close().catch(() => {});
    const s2 = await cekejKonec(novyPid);
    ok('po zavření okna skončí i nová instance', !zije(novyPid), `${s2} s`);
  } catch (e) {
    ok('scénář 4b doběhl bez výjimky', false, String(e.message || e).split('\n')[0]);
    vypisLog(claude);
  } finally {
    for (const pid of [novyPid, staryPid]) if (pid && zije(pid)) { try { process.kill(pid); } catch (_) { /* pryč */ } }
    await konec(app, claude);
  }
}

/* ── 5. starší hub bez značky už běží → nespouštět druhý ─────────────── */
if (process.platform !== 'win32') {
  const claude = tmpClaude();
  const stary = tmp('hub-stary-');
  let app = null, hub = null;
  try {
    console.log('\n── 5. starší hub bez značky hub-bezi.json už běží ──');
    fs.writeFileSync(path.join(stary, 'claude-hub.py'),
      fs.readFileSync(path.join(root, 'claude-hub.py'), 'utf8').replaceAll('--vlastni-okno', '--neznamy-prepinac'));
    fs.cpSync(path.join(root, 'hub'), path.join(stary, 'hub'), { recursive: true });
    const log = path.join(claude, 'vystup.log');
    hub = spawn(python, [path.join(stary, 'claude-hub.py'), '--no-browser'],
      { env: { ...process.env, CLAUDE_CONFIG_DIR: claude }, stdio: ['ignore', fs.openSync(log, 'w'), 'ignore'] });
    let url = '';
    for (let i = 0; i < 300 && !url; i++) { await pauza(200); url = (fs.readFileSync(log, 'utf8').match(/^http:\/\/127\S+/m) || [''])[0]; }
    await pauza(800);
    fs.rmSync(path.join(claude, 'hub-bezi.json'), { force: true });   // starší verze ho nepíše
    // Appka skončí hned (než se k ní zkouška připojí) a hlášky zapíše do souboru.
    const ud = tmp('hub-desktop-ud-');
    const pred = new Set(procesyHubu(path.join(stary, 'claude-hub.py')));
    try {
      app = await electron.launch({
        ...(process.env.HUB_DESKTOP_EXE ? { executablePath: process.env.HUB_DESKTOP_EXE } : {}),
        args: [...(process.env.HUB_DESKTOP_EXE ? [] : [path.join(here, '..', 'main.js')]), `--user-data-dir=${ud}`],
        env: { ...process.env, CLAUDE_CONFIG_DIR: claude, HUB_LAUNCHER: path.join(stary, 'claude-hub.py'),
          HUB_PYTHON: python, HUB_DESKTOP_TEST: '1' },
      });
      for (let i = 0; i < 60 && app.process().exitCode === null; i++) await pauza(250);
    } catch (_) { /* appka skončila dřív, než se zkouška připojila — čeká se to */ }
    let hl = [];
    for (let i = 0; i < 40 && !hl.length; i++) {
      try { hl = JSON.parse(fs.readFileSync(path.join(ud, 'hlasky.json'), 'utf8')); } catch (_) { await pauza(250); }
    }
    ok('appka skončila s upozorněním „Hub už běží v jiném okně"', hl.some((h) => /už běží v jiném okně/.test(h)), JSON.stringify(hl));
    const nove = procesyHubu(path.join(stary, 'claude-hub.py')).filter((p) => !pred.has(p));
    ok('nespustila druhý hub, běžící zůstal nedotčený', nove.length === 0 && zije(hub.pid), `nových: ${nove.length}`);
    fs.rmSync(ud, { recursive: true, force: true });
  } catch (e) {
    ok('scénář 5 doběhl bez výjimky', false, String(e.message || e).split('\n')[0]);
    vypisLog(claude);
  } finally {
    try { process.kill(hub.pid); } catch (_) { /* pryč */ }
    if (app) await app.close().catch(() => {});
    await konec(null, claude);
    fs.rmSync(stary, { recursive: true, force: true });
  }
}

const spatne = vysledky.filter((v) => !v).length;
console.log(`\n${vysledky.length - spatne}/${vysledky.length} v pořádku`);
process.exit(spatne ? 1 : 0);
