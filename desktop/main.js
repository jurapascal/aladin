/* Aladin — okno jako samostatná aplikace.
 *
 * Hub je lokální webová appka. Dosud ji držel v okně Chromu (--app), takže
 * v liště svítilo „Chrome", menu bylo chromí a Nové okno otevřelo prázdný
 * Chrome. Tahle appka (Electron, stejné jádro i stejné UI) dělá to, co VS Code:
 * vlastní jméno, vlastní menu a Cmd/Ctrl+N otevře další okno hubu.
 *
 * Hub sám se nemění: spustí se nainstalovaný (~/.claude/claude-hub.py) s
 * přepínačem --vlastni-okno, který jen vypíše adresu a žije, dokud je otevřená
 * aspoň jedna stránka. Okna sdílejí tytéž taby (server to umí), každé má jiný
 * aktivní.
 *
 * Starší hub přepínač nezná: pak běží s --no-browser a ukončí ho tahle appka.
 *
 * Hub od 2.78 appku instaluje sám (hub/appka.py) a okno otevírá v ní:
 * spustí ji s --hub-url=<adresa> a appka se jen připojí, nic nespouští.
 *
 * Proměnné pro zkoušky: HUB_LAUNCHER (cesta ke claude-hub.py), HUB_PYTHON,
 * HUB_ARGS (např. --test), HUB_DESKTOP_TEST.
 */
'use strict';

const { app, BrowserWindow, Menu, shell, session, dialog, nativeTheme, ipcMain } = require('electron');
const { spawn, spawnSync } = require('child_process');
const fs = require('fs');
const http = require('http');
const os = require('os');
const path = require('path');

const TEST = !!process.env.HUB_DESKTOP_TEST;
const IS_WIN = process.platform === 'win32';
const IS_MAC = process.platform === 'darwin';
const RELEASES = 'https://github.com/jurapascal/aladin/releases/latest';
const HUB_URL = /^http:\/\/127\.0\.0\.1:\d+(\/|$)/;

const claudeDir = () => process.env.CLAUDE_CONFIG_DIR || path.join(os.homedir(), '.claude');

/* Ikona okna: z nainstalovaného hubu (~/.claude/hub/static) — mění se spolu
   s hubem, takže nová ikona nečeká na novou verzi appky. Zabalená appka
   assets/ z repa nemá (v1.1.1 tak ukazovala loďku zapečenou při sestavení);
   ve vývoji se vezme z repa, nakonec ta přibalená k appce. */
function ikonaAppky() {
  const kandidati = [path.join(claudeDir(), 'hub', 'static', 'icon-512.png'),
                     path.join(__dirname, '..', 'assets', 'claude-code.png'),
                     path.join(process.resourcesPath || '', 'icon.png')];
  return kandidati.find((p) => { try { return fs.statSync(p).isFile(); } catch (_) { return false; } }) || '';
}
const readJson = (p) => { try { return JSON.parse(fs.readFileSync(p, 'utf8')); } catch (_) { return null; } };
// Adresa hubu z příkazové řádky (hub/appka.py → --hub-url=…): hub už běží
// a okno má jít rovnou k němu.
const adresaZArgv = (argv) => {
  const a = (argv || []).find((x) => String(x).startsWith('--hub-url='));
  const u = a ? String(a).slice('--hub-url='.length) : '';
  return HUB_URL.test(u) ? u : '';
};

let hubProc = null;        // hub, který jsme spustili my
let hubSam = true;         // ukončí se hub sám (--vlastni-okno), nebo ho zavřeme my
let hubUrl = '';           // poslední známá adresa
let starting = null;       // běžící start (ať se nespustí dvakrát)
const externalOpened = []; // jen pro zkoušky
const hlasky = [];         // jen pro zkoušky: dialogy se nezobrazují, jen zapíšou

function dialogOkno(opts) {
  const { volbaVZkousce, ...o } = opts;      // volbaVZkousce = co zvolit ve zkoušce
  if (TEST) { hlasky.push(o.message); return volbaVZkousce ?? o.cancelId ?? 0; }
  const win = BrowserWindow.getFocusedWindow();
  return win ? dialog.showMessageBoxSync(win, o) : dialog.showMessageBoxSync(o);
}

/* ── hub ───────────────────────────────────────────────────────────────── */

function dotaz(url, ms = 2000) {
  return new Promise((resolve) => {
    try {
      const req = http.get(url, { timeout: ms }, (res) => { res.resume(); resolve(true); });
      req.on('timeout', () => { req.destroy(); resolve(false); });
      req.on('error', () => resolve(false));
    } catch (_) { resolve(false); }
  });
}

function pidZije(pid) {
  try { process.kill(pid, 0); return true; } catch (e) { return e.code === 'EPERM'; }
}

/* Už běžící hub (spuštěný z jiného okna, nebo z minula) — jen se k němu
 * připojíme: druhá instance nad týmiž chaty by si přepisovala konverzace. */
async function bezici() {
  const d = readJson(path.join(claudeDir(), 'hub-bezi.json'));
  if (!d || !d.url || !HUB_URL.test(d.url) || !pidZije(Number(d.pid))) return '';
  return (await dotaz(d.url)) ? d.url : '';
}

/* Starší hub (před 2.57.23) značku hub-bezi.json nepíše. Běží-li z téže
 * instalace, nesmí se spustit druhý — přepisovaly by si konverzace. Pozná se
 * z procesů (Mac, Linux); na Windows starší hub bez značky nebývá. */
function hubBezZnacky(launcher) {
  if (IS_WIN) return false;
  const r = spawnSync('ps', ['-axo', 'command='], { encoding: 'utf8' });
  return String(r.stdout || '').split('\n').some((l) =>
    l.includes(launcher) && !/--(test|doctor|setup)\b/.test(l));
}

function najdiPython() {
  const dir = claudeDir();
  const bundled = IS_WIN ? path.join(dir, 'runtime', 'python', 'python.exe')
    : path.join(dir, 'runtime', 'python', 'bin', 'python3');
  const kandidati = [
    process.env.HUB_PYTHON && { cmd: process.env.HUB_PYTHON, args: [] },
    { cmd: bundled, args: [] },
    ...(IS_WIN ? [{ cmd: 'python', args: [] }, { cmd: 'py', args: ['-3'] }]
      : [{ cmd: 'python3', args: [] }, { cmd: 'python', args: [] }]),
  ].filter(Boolean);
  for (const k of kandidati) {
    if (path.isAbsolute(k.cmd) && !fs.existsSync(k.cmd)) continue;
    const r = spawnSync(k.cmd, [...k.args, '--version'], { windowsHide: true, timeout: 8000 });
    if (r.status === 0) return k;
  }
  return null;
}

function nenainstalovano(text) {
  const volba = dialogOkno({
    type: 'info', title: 'Aladin',
    message: 'Aladin ještě není nainstalovaný.',
    detail: text, buttons: ['Stáhnout instalaci', 'Zavřít'], defaultId: 0, cancelId: 1,
  });
  if (volba === 0) shell.openExternal(RELEASES);
}

function spustHub() {
  const launcher = process.env.HUB_LAUNCHER || path.join(claudeDir(), 'claude-hub.py');
  if (!fs.existsSync(launcher)) {
    nenainstalovano(`Nenašel jsem ${launcher}. Nainstaluj hub (stránka vydání) a spusť tuhle appku znovu.`);
    return Promise.reject(new Error('hub není nainstalovaný'));
  }
  if (hubBezZnacky(launcher)) {
    // „Spustit i tak" pro případ, že proces jen zůstal viset bez okna.
    const volba = dialogOkno({
      type: 'warning', title: 'Aladin',
      message: 'Hub už běží v jiném okně.',
      detail: 'Je to starší verze, ke které se tahle appka neumí připojit. Zavři okno hubu '
        + '(v Chromu) a spusť appku znovu — dvě instance nad stejnými chaty by si přepisovaly konverzace.',
      buttons: ['Zavřít', 'Spustit i tak'], defaultId: 0, cancelId: 0,
    });
    if (volba !== 1) return Promise.reject(new Error('hub běží bez značky'));
  }
  const py = najdiPython();
  if (!py) {
    nenainstalovano('Na tomhle počítači chybí Python 3, na kterém hub běží. Instalace ho přinese s sebou.');
    return Promise.reject(new Error('chybí Python'));
  }
  // Výstup jde do souboru, ne do roury: hub tak přežije zavření téhle appky
  // (končí sám, když se mu zavřou stránky) a nikdy nenarazí na zavřenou rouru.
  // Na Windows ne odpojený: dítě tam konec rodiče přežije i tak a v odpojeném
  // procesu (bez konzole) ConPTY terminál nevytvoří (pywinpty panikaří).
  fs.mkdirSync(app.getPath('userData'), { recursive: true });
  const logFile = path.join(app.getPath('userData'), 'hub-vystup.log');
  const fd = fs.openSync(logFile, 'w');
  // --vlastni-okno umí hub od verze, která ho přinesla; starší dostane
  // --no-browser (adresu vypíše stejně, jen se neukončí sám — zavřeme ho my).
  let umi = false;
  try { umi = fs.readFileSync(launcher, 'utf8').includes('--vlastni-okno'); } catch (_) { /* ne */ }
  hubSam = umi;
  const args = [...py.args, launcher, umi ? '--vlastni-okno' : '--no-browser',
    ...(process.env.HUB_ARGS || '').split(/\s+/).filter(Boolean)];
  const proc = spawn(py.cmd, args, {
    cwd: os.homedir(), detached: !IS_WIN, windowsHide: true,
    stdio: ['ignore', fd, fd], env: process.env,
  });
  proc.unref();
  hubProc = proc;
  proc.on('exit', () => {
    if (hubProc === proc) hubProc = null;
    // Po aktualizaci hub předá okna nové instanci a sám skončí — to je v pořádku.
    if (BrowserWindow.getAllWindows().length === 0) app.quit();
  });
  return new Promise((resolve, reject) => {
    const start = Date.now();
    const timer = setInterval(() => {
      let text = '';
      try { text = fs.readFileSync(logFile, 'utf8'); } catch (_) { /* ještě nic */ }
      const m = text.match(/^http:\/\/127\.0\.0\.1:\d+\/\S*/m);
      if (m) { clearInterval(timer); resolve(m[0]); return; }
      if (hubProc !== proc) {
        clearInterval(timer);
        reject(new Error('Hub skončil dřív, než naběhl.\n\n' + text.slice(-800)));
      } else if (Date.now() - start > 90000) {
        clearInterval(timer);
        reject(new Error('Hub do 90 s nenaběhl.\n\n' + text.slice(-800)));
      }
    }, 150);
  });
}

/* Adresa běžícího hubu; když žádný neběží, spustí ho. */
function zajistiHub() {
  if (starting) return starting;
  starting = (async () => {
    // Adresa, na které okna právě jsou: po aktualizaci hub přejde na nový port
    // a okna se přepnou sama (okno-jdi) — nové okno musí jít tam, ne na starý.
    const okna = BrowserWindow.getAllWindows().map((w) => w.webContents.getURL())
      .filter((u) => HUB_URL.test(u));
    for (const u of [...okna, hubUrl].filter(Boolean)) {
      if (await dotaz(u)) return (hubUrl = u);
    }
    const url = await bezici();
    if (url) return (hubUrl = url);
    return (hubUrl = await spustHub());
  })().finally(() => { starting = null; });
  return starting;
}

/* ── okna ──────────────────────────────────────────────────────────────── */

function duveryhodne(url) {
  if (HUB_URL.test(url)) return true;
  // Prostor na serveru (brána): okno z hubu přejde na adresu serveru z nastavení.
  const cfg = readJson(path.join(claudeDir(), 'hub-config.json')) || {};
  try {
    return !!cfg.gw_server && new URL(url).origin === new URL(cfg.gw_server).origin;
  } catch (_) { return false; }
}

function ven(url) {
  if (!/^(https?|mailto):/i.test(url)) return;
  if (TEST) { externalOpened.push(url); return; }
  shell.openExternal(url);
}

function boundsFile() { return path.join(app.getPath('userData'), 'okno.json'); }

function noveOkno(url) {
  const saved = readJson(boundsFile()) || {};
  const focus = BrowserWindow.getFocusedWindow() || BrowserWindow.getAllWindows().slice(-1)[0];
  const opts = { width: saved.width || 1400, height: saved.height || 900 };
  if (focus) {                       // další okno kousek vedle (jako VS Code)
    const b = focus.getBounds();
    Object.assign(opts, { x: b.x + 28, y: b.y + 28, width: b.width, height: b.height });
  } else if (Number.isInteger(saved.x) && Number.isInteger(saved.y)) {
    Object.assign(opts, { x: saved.x, y: saved.y });
  }
  const icon = ikonaAppky();
  const win = new BrowserWindow({
    ...opts, minWidth: 720, minHeight: 480, title: 'Aladin',
    backgroundColor: nativeTheme.shouldUseDarkColors ? '#0d0f12' : '#ffffff',
    ...(icon ? { icon } : {}),
    // Menu kreslí hub v liště (window.hubApp) — nativní lišta je schovaná a Alt
    // ji ukáže. Starší hub, který menu nekreslí, ji dostane zpátky (níž).
    autoHideMenuBar: !IS_MAC,
    webPreferences: { contextIsolation: true, nodeIntegration: false, sandbox: true, spellcheck: false,
      preload: path.join(__dirname, 'preload.cjs') },
  });
  // Nová stránka (i Znovu načíst, přechod na novou instanci hubu) menu kreslí
  // znovu. Nulovat se musí při potvrzení navigace, ne až po načtení — to už
  // stránka menu dávno nahlásila a lišta by se po 4 s vrátila (dvě menu).
  win.webContents.on('did-navigate', () => { win.__menuVListe = false; });
  win.webContents.on('did-finish-load', () => {
    setTimeout(() => {
      if (!win.isDestroyed() && !win.__menuVListe && !IS_MAC) {
        win.setAutoHideMenuBar(false);
        win.setMenuBarVisibility(true);
      }
    }, 4000);
  });
  win.webContents.setWindowOpenHandler(({ url: u }) => { ven(u); return { action: 'deny' }; });
  win.webContents.on('will-navigate', (e, u) => { if (!duveryhodne(u)) { e.preventDefault(); ven(u); } });
  win.webContents.on('render-process-gone', () => { try { win.reload(); } catch (_) { /* okno už není */ } });
  // Hub se před zavřením ptá (beforeunload), když by se utnula rozdělaná práce.
  // Chrome na to ukáže dialog, Electron zavření bez ptaní zablokuje — okno by
  // nešlo zavřít a Cmd+Q by nic neudělalo. Zeptáme se tedy sami.
  win.webContents.on('will-prevent-unload', (e) => {
    // Platí pro zavření okna i pro Znovu načíst — proto obecně „odejít".
    const volba = dialogOkno({
      type: 'question', title: 'Aladin', buttons: ['Odejít', 'Zůstat'], defaultId: 1, cancelId: 1,
      message: 'Claude ještě pracuje. Opravdu odejít?',
      detail: 'Rozdělaná práce v tabech se přeruší.',
      volbaVZkousce: 0,
    });
    if (volba === 0) e.preventDefault();      // = beforeunload nebrat v úvahu, odejít
  });
  let t = null;
  const uloz = () => {
    clearTimeout(t);
    t = setTimeout(() => {
      if (win.isDestroyed() || win.isMaximized() || win.isFullScreen()) return;
      try { fs.writeFileSync(boundsFile(), JSON.stringify(win.getBounds())); } catch (_) { /* nevadí */ }
    }, 400);
  };
  win.on('resize', uloz);
  win.on('move', uloz);
  win.loadURL(url);
  return win;
}

async function otevriOkno(primo = '') {
  try {
    if (primo && (await dotaz(primo))) return noveOkno((hubUrl = primo));
    return noveOkno(await zajistiHub());
  } catch (e) {
    if (!/není nainstalovaný|chybí Python|bez značky/.test(String(e.message))) {
      if (TEST) hlasky.push(String(e.message || e));
      else dialog.showErrorBox('Aladina se nepodařilo spustit', String(e.message || e));
    }
    if (TEST) {             // zkouška se k rychle skončené appce nestihne připojit
      try { fs.writeFileSync(path.join(app.getPath('userData'), 'hlasky.json'), JSON.stringify(hlasky)); } catch (_) { /* nic */ }
    }
    if (BrowserWindow.getAllWindows().length === 0) app.quit();
    return null;
  }
}

/* Hlídač: hub okna neodpovídá (restart po aktualizaci, nová instance naběhla
 * pomaleji, než stihl starý hub okna přepnout) → když už běží nová
 * (hub-bezi.json), okno se na ni přepne samo. */
const port = (u) => (String(u).match(/^http:\/\/127\.0\.0\.1:(\d+)/) || [])[1];
function hlidej() {
  let bezi = false;
  setInterval(async () => {
    if (bezi) return;
    bezi = true;
    try {
      for (const win of BrowserWindow.getAllWindows()) {
        const u = win.webContents.getURL();
        if (!HUB_URL.test(u) || win.webContents.isLoading() || (await dotaz(u, 1500))) continue;
        const nova = await bezici();
        if (nova && port(nova) !== port(u) && !win.isDestroyed()) {
          hubUrl = nova;
          // Přechod na novou instanci není odchod (jako okno-jdi v hubu).
          await win.webContents.executeJavaScript('window.HUB_LEAVING = true').catch(() => {});
          win.loadURL(nova);
        }
      }
    } finally { bezi = false; }
  }, 2000);
}

/* ── menu ──────────────────────────────────────────────────────────────── */

function sestavMenu() {
  const edit = { label: 'Úpravy', submenu: [
    { role: 'undo', label: 'Zpět' }, { role: 'redo', label: 'Znovu' }, { type: 'separator' },
    { role: 'cut', label: 'Vyjmout' }, { role: 'copy', label: 'Kopírovat' },
    { role: 'paste', label: 'Vložit' }, { role: 'selectAll', label: 'Vybrat vše' },
  ] };
  const template = [
    ...(IS_MAC ? [{ label: app.name, submenu: [
      { role: 'about', label: 'O aplikaci Aladin' }, { type: 'separator' },
      { role: 'services' }, { type: 'separator' },
      { role: 'hide' }, { role: 'hideOthers' }, { role: 'unhide' }, { type: 'separator' },
      { role: 'quit', label: 'Ukončit Aladina' },
    ] }] : []),
    { label: 'Soubor', submenu: [
      // Na Windows a Linuxu patří Ctrl+N a Ctrl+W terminálu (historie, mazání
      // slova) — proto jako VS Code: Ctrl+Shift+N / Ctrl+Shift+W.
      { label: 'Nové okno', accelerator: IS_MAC ? 'Cmd+N' : 'Ctrl+Shift+N', click: () => { otevriOkno(); } },
      { role: 'close', label: 'Zavřít okno', accelerator: IS_MAC ? 'Cmd+W' : 'Ctrl+Shift+W' },
      ...(IS_MAC ? [] : [{ type: 'separator' }, { role: 'quit', label: 'Ukončit' }]),
    ] },
    edit,
    { label: 'Zobrazení', submenu: [
      { role: 'reload', label: 'Znovu načíst' }, { role: 'forceReload', label: 'Načíst znovu bez mezipaměti' },
      { role: 'toggleDevTools', label: 'Nástroje pro vývojáře' }, { type: 'separator' },
      { role: 'resetZoom', label: 'Skutečná velikost' }, { role: 'zoomIn', label: 'Přiblížit' },
      { role: 'zoomOut', label: 'Oddálit' }, { type: 'separator' },
      { role: 'togglefullscreen', label: 'Celá obrazovka' },
    ] },
    // Na Macu systémové menu Okno; jinde vlastní položky, ať jde nakreslit v liště hubu.
    IS_MAC ? { label: 'Okno', role: 'windowMenu' } : { label: 'Okno', submenu: [
      { role: 'minimize', label: 'Minimalizovat' },
      { label: 'Maximalizovat', click: (_i, win) => { if (win) { if (win.isMaximized()) win.unmaximize(); else win.maximize(); } } },
    ] },
    { label: 'Nápověda', submenu: [
      { label: 'Aladin na GitHubu', click: () => ven('https://github.com/jurapascal/aladin') },
    ] },
  ];
  // Každá položka dostane id — podle něj ji spouští menu nakreslené v hubu.
  let n = 0;
  const ocisluj = (items) => items.forEach((i) => { if (!i.id) i.id = `m${++n}`; if (Array.isArray(i.submenu)) ocisluj(i.submenu); });
  ocisluj(template);
  Menu.setApplicationMenu(Menu.buildFromTemplate(template));
}

/* Menu pro stránku. Nativní menu Electronu kreslí na Linuxu a Windows
 * Chromium a jeho rozbalené položky jsou velké jako pro prst — zvenku se to
 * nastavit nedá. Hub proto menu nakreslí sám v liště (hub.js, appMenu), stejně
 * malé jako ostatní tlačítka, a nativní lišta se schová (Alt ji ukáže, zkratky
 * fungují dál). Na Macu je menu nahoře v systému, tam se nic nekreslí. */
function zkratka(item) {
  let a = item.accelerator || (typeof item.getDefaultRoleAccelerator === 'function' && item.getDefaultRoleAccelerator()) || '';
  a = String(a || '').replace(/CommandOrControl|CmdOrCtrl|Control/g, 'Ctrl').replace(/\bPlus\b/, '+');
  return a;
}
function menuProStranku() {
  const m = Menu.getApplicationMenu();
  if (IS_MAC || !m) return [];
  return m.items.filter((t) => t.submenu && t.visible !== false).map((t) => ({
    label: t.label.replace(/&/g, ''),
    polozky: t.submenu.items.filter((i) => i.visible !== false).map((i) => (i.type === 'separator'
      ? { oddelovac: true }
      : { id: i.id, label: i.label, zkratka: zkratka(i), povoleno: i.enabled !== false })),
  }));
}
function mostMenu() {
  const zDuveryhodne = (e) => duveryhodne((e.senderFrame && e.senderFrame.url) || e.sender.getURL());
  ipcMain.handle('appka:menu', (e) => (zDuveryhodne(e) ? menuProStranku() : []));
  ipcMain.on('appka:akce', (e, id) => {
    if (!zDuveryhodne(e)) return;
    const win = BrowserWindow.fromWebContents(e.sender);
    const item = Menu.getApplicationMenu() && Menu.getApplicationMenu().getMenuItemById(String(id));
    if (win && item && item.enabled !== false) item.click(undefined, win, e.sender);
  });
  ipcMain.on('appka:menu-v-liste', (e, ano) => {
    const win = BrowserWindow.fromWebContents(e.sender);
    if (!win || IS_MAC) return;
    win.__menuVListe = !!ano;
    win.setAutoHideMenuBar(!!ano);
    win.setMenuBarVisibility(!ano);
  });
}

/* ── start ─────────────────────────────────────────────────────────────── */

function povoleni() {
  const dovolene = new Set(['media', 'clipboard-read', 'clipboard-sanitized-write',
    'notifications', 'fullscreen']);
  const ok = (permission, url) => dovolene.has(permission) && !!url && duveryhodne(url);
  const ses = session.defaultSession;
  ses.setPermissionRequestHandler((wc, permission, cb, d) => cb(ok(permission, d.requestingUrl)));
  ses.setPermissionCheckHandler((wc, permission, origin) => ok(permission, origin));
  // „Přesný snímek“ tlačítka Nahlásit chybu (Verxee Debug) volá getDisplayMedia.
  // Bez obsluhy ho Electron odmítne (NotSupportedError) a snímek tiše nevznikne.
  // Chrome se ptá, co sdílet — tady dostane jen obsah vlastního okna (rámec,
  // který se ptá), jen na klik a jen stránka hubu nebo prostoru; zvuk nikdy.
  ses.setDisplayMediaRequestHandler((req, cb) => {
    const frame = req.frame;
    const url = (frame && frame.url) || req.securityOrigin;
    if (!frame || !req.userGesture || !req.videoRequested || !duveryhodne(url)) return cb({});
    cb({ video: frame });
  });
}

// Na novějších Linuxech (Ubuntu 24.04+, AppArmor) neumí Chromium sandbox
// spustit — jmenné prostory má systém zakázané a pomocník chrome-sandbox
// nemá setuid (AppImage ani rozbalená složka z hubu/appka.py ho mít nemůže).
// Appka by spadla hned na startu. Okno ukazuje jen lokální hub, cizí stránky
// jdou ven do prohlížeče (will-navigate níž).
function bezSandboxu() {
  if (process.platform !== 'linux') return false;
  if (process.env.APPIMAGE) return true;
  const cti = (p) => { try { return fs.readFileSync(p, 'utf8').trim(); } catch (_) { return ''; } };
  const bezJmennychProstoru = cti('/proc/sys/kernel/apparmor_restrict_unprivileged_userns') === '1'
    || cti('/proc/sys/kernel/unprivileged_userns_clone') === '0';
  let setuid = false;
  try {
    const st = fs.statSync(path.join(path.dirname(process.execPath), 'chrome-sandbox'));
    setuid = st.uid === 0 && (st.mode & 0o4000) !== 0;
  } catch (_) { /* není */ }
  return bezJmennychProstoru && !setuid;
}
if (bezSandboxu()) app.commandLine.appendSwitch('no-sandbox');

// Složka s nastavením zůstává pod původním názvem, ať se přejmenováním
// neztratí poloha okna ani udělená oprávnění.
// Přepínač --user-data-dir (zkoušky, vlastní profil) má přednost.
if (!app.commandLine.hasSwitch('user-data-dir')) {
  app.setPath('userData', path.join(app.getPath('appData'), 'Claude Code Hub'));
}

if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  // Druhé spuštění (Nové okno z hubu, zástupce) = další okno téže appky.
  app.on('second-instance', (_e, argv) => { otevriOkno(adresaZArgv(argv)); });
  app.setAppUserModelId('com.github.jurapascal.claude-code-hub.desktop');

  app.whenReady().then(async () => {
    app.setName('Aladin');
    if (IS_MAC && app.dock) {
      const png = ikonaAppky();
      if (png) app.dock.setIcon(png);
    }
    povoleni();
    sestavMenu();
    mostMenu();
    hlidej();
    if (TEST) {
      global.hubDesktop = {
        oken: () => BrowserWindow.getAllWindows().length,
        noveOkno: () => otevriOkno().then(() => BrowserWindow.getAllWindows().length),
        url: () => hubUrl,
        ven: () => externalOpened.slice(),
        hubPid: () => (hubProc ? hubProc.pid : 0),
        hubSam: () => hubSam,
        hlasky: () => hlasky.slice(),
      };
    }
    await otevriOkno(adresaZArgv(process.argv));
  });

  app.on('activate', () => { if (BrowserWindow.getAllWindows().length === 0) otevriOkno(); });
  // Zavřené poslední okno = appka končí (jako dosud okno hubu). Hub se sám
  // ukončí, jakmile se mu 10 s nepřipojí žádná stránka.
  app.on('window-all-closed', () => app.quit());
  // Starší hub (--no-browser) by běžel dál bez okna — ten zavřeme my.
  app.on('will-quit', () => {
    if (!hubSam && hubProc) { try { process.kill(hubProc.pid); } catch (_) { /* už není */ } }
    if (TEST) {
      try { fs.writeFileSync(path.join(app.getPath('userData'), 'hlasky.json'), JSON.stringify(hlasky)); } catch (_) { /* nic */ }
    }
  });
}
