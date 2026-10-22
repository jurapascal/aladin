/* Zkouška karty s dotazem na oprávnění v prohlížeči (trvalé povolení).
 *
 * Místo Claude Code běží v tabu falešný `claude` (skript, který ukáže dialog
 * z ANSI souboru a zapíše do logu každou přijatou klávesu), takže se dá změřit,
 * co karta opravdu pošle do pty — a kolikrát.
 *
 *   DIR=/tmp/fake
 *   python3 tools/test_povoleni_karta.py --dump $DIR          # vzorové dialogy
 *   cp <falešný claude> bin/claude                              # viz hlavička téhle zkoušky níž
 *   CLAUDE_CONFIG_DIR=<tmp> HUB_TEST=1 PATH=bin:$PATH FAKE_DIR=$DIR python3 claude-hub.py --no-browser --test
 *   node tools/uitest_povoleni.mjs <url> $DIR [snímky]
 *
 * Falešný claude: čte $FAKE_DIR/show (jméno dialogu nebo `clear`), vypíše
 * $FAKE_DIR/<jméno>.ansi a každou klávesu zapíše do $FAKE_DIR/log
 * (řádky `KEY …`, `ANSWER <číslice>`, `ESC`).
 */
import fs from 'fs';
import os from 'os';
import path from 'path';
import { pathToFileURL } from 'url';

const [, , url, dir, snimky = os.tmpdir()] = process.argv;
if (!url || !dir) { console.error('použití: node tools/uitest_povoleni.mjs <url> <FAKE_DIR> [složka snímků]'); process.exit(2); }

function najdiPlaywright() {
  const home = os.homedir();
  const npx = path.join(home, '.npm', '_npx');
  const kand = [process.env.PLAYWRIGHT_MODULE,
    ...(fs.existsSync(npx) ? fs.readdirSync(npx).map((d) => path.join(npx, d, 'node_modules', 'playwright')) : [])]
    .filter((d) => d && fs.existsSync(path.join(d, 'index.mjs')));
  if (!kand.length) { console.error('Playwright nenalezen (PLAYWRIGHT_MODULE)'); process.exit(2); }
  return kand;
}

let chromium;
for (const k of najdiPlaywright()) {
  try {
    ({ chromium } = await import(pathToFileURL(path.join(k, 'index.mjs')).href));
    const b = await chromium.launch({ args: ['--no-sandbox'] });
    await b.close();
    break;
  } catch (_) { chromium = null; }
}
if (!chromium) { console.error('žádná kopie Playwrightu nemá spustitelný Chromium'); process.exit(2); }

const log = () => (fs.existsSync(path.join(dir, 'log')) ? fs.readFileSync(path.join(dir, 'log'), 'utf8') : '');
const odpovedi = () => log().split('\n').filter((l) => /ANSWER|ESC/.test(l)).length;
const klavesy = () => log().split('\n').filter((l) => / KEY /.test(l)).length;
const show = (jmeno) => fs.writeFileSync(path.join(dir, 'show'), jmeno);
const vysledky = [];
const t = (jmeno, ok, d = '') => { vysledky.push({ jmeno, ok: !!ok, d }); console.log((ok ? 'OK    ' : 'CHYBA ') + jmeno + (d ? '  — ' + d : '')); };

const browser = await chromium.launch({ args: ['--no-sandbox'] });
const ctx = await browser.newContext({ colorScheme: 'dark', viewport: { width: 1400, height: 900 } });
const page = await ctx.newPage();
const chyby = [];
page.on('pageerror', (e) => chyby.push(String(e).slice(0, 200)));
page.on('console', (m) => { if (m.type() === 'error' && !/interactive-widget/.test(m.text())) chyby.push(m.text().slice(0, 200)); });

await page.goto(url, { waitUntil: 'domcontentloaded' });
await page.waitForTimeout(2500);
// Čistá konfigurace = úvodní průvodce přes celou stránku.
if (await page.locator('.onb-skip').isVisible().catch(() => false)) {
  await page.click('.onb-skip');
  await page.waitForTimeout(600);
}
if (await page.locator('#btn-new-agent').isVisible()) {
  await page.click('#btn-new-agent');
} else {
  await page.click('#btn-new-top');
  await page.waitForTimeout(450);     // hub první 0,3 s po otevření menu klik ignoruje
  await page.locator('.ctxmenu button').first().click();
}

const karta = page.locator('.pane.active .ask:not([hidden])');
const tlac = page.locator('.pane.active .ask:not([hidden]) .ask-opt');
let hnedPoZobrazeni = null;
async function ukaz(jmeno) {
  show('clear'); await page.waitForTimeout(500);
  show(jmeno);
  await karta.waitFor({ state: 'visible', timeout: 8000 });
  // Stav trvalé volby hned po objevení karty, než vyprší zámek.
  hnedPoZobrazeni = await page.evaluate(() => [...document.querySelectorAll('.pane.active .ask-opt.perm')].map((n) => n.disabled));
  await page.waitForTimeout(700);
}
async function texty() { return (await tlac.allInnerTexts()).map((x) => x.replace(/\s+/g, ' ').trim()); }

// Čekání na start falešného claude (terminál i bublina).
for (let i = 0; i < 30 && !fs.existsSync(path.join(dir, 'ready')); i++) {
  if (page.url()) await page.waitForTimeout(500);
  if ((await page.locator('.pane.active .xterm-rows').innerText().catch(() => '')).includes('fake claude ready')) break;
}

try {
  // 1) tři volby: jednorázové Ano / trvalé / Ne
  await ukaz('bash3');
  let tx = await texty();
  t('bash3: tři tlačítka', tx.length === 3, tx.join(' | '));
  t('bash3: jednorázové Ano je česky a odlišené', /^1\s*Ano, jen tentokrát/.test(tx[0]), tx[0]);
  t('bash3: trvalá volba má český popis s příkazem a složkou',
    /Ano a příště se neptat/.test(tx[1]) && /git status/.test(tx[1]) && /\/home\/x\/proj/.test(tx[1]) && /napořád/i.test(tx[1]), tx[1]);
  t('bash3: číslo na trvalém tlačítku je skutečné (2)', /^2/.test(tx[1]));
  t('bash3: Ne má číslo 3', /^3\s*No/.test(tx[2]), tx[2]);
  const perm = tlac.nth(1);
  t('bash3: trvalá volba má třídu perm a není sel', await perm.evaluate((n) => n.classList.contains('perm') && !n.classList.contains('sel')));
  t('bash3: trvalá volba je po objevení zamčená', hnedPoZobrazeni.length === 1 && hnedPoZobrazeni[0] === true, String(hnedPoZobrazeni));
  const bg = await page.evaluate(() => [...document.querySelectorAll('.pane.active .ask-opt')].map((n) => getComputedStyle(n).borderColor + '/' + getComputedStyle(n).borderStyle));
  t('bash3: trvalá volba má jinou barvu a čárkovaný okraj', bg[1] !== bg[0] && /dashed/.test(bg[1]), bg.join(' ; '));
  await page.screenshot({ path: path.join(snimky, 'povoleni-bash3.png') });

  const k0 = klavesy();
  t('bash3: po chvíli je trvalá volba odemčená', !(await perm.isDisabled()));

  // 2) dvojklik na jednorázové Ano = jedna odpověď
  const o0 = odpovedi();
  await tlac.nth(0).dblclick();
  await page.waitForTimeout(900);
  t('bash3: dvojklik na Ano poslal odpověď jednou', odpovedi() === o0 + 1 && klavesy() === k0 + 1, `odpovědí ${odpovedi() - o0}, kláves ${klavesy() - k0}`);
  t('bash3: poslalo se číslo 1', /ANSWER 1/.test(log().split('\n').slice(-3).join('\n')));

  // 3) trvalá volba: počkat na odemčení, trojklik = jedna odpověď, číslice 2
  await ukaz('bash3');
  await page.waitForTimeout(600);
  const k1 = klavesy();
  for (let i = 0; i < 4; i++) await tlac.nth(1).click({ force: true, timeout: 800 }).catch(() => {});
  await page.waitForTimeout(900);
  t('bash3: 4 kliky na trvalou volbu = jedna klávesa', klavesy() === k1 + 1, `kláves ${klavesy() - k1}`);
  t('bash3: poslalo se číslo 2', /ANSWER 2/.test(log().split('\n').slice(-3).join('\n')));

  // 4) Edit s diffem: trvalá do konce relace
  await ukaz('edit3');
  tx = await texty();
  t('edit3: volba do konce relace', /Ano, do konce relace/.test(tx[1]) && /úpravy souborů/.test(tx[1]) && !/shift\+tab/.test(tx[1].split('Přesné')[0] || tx[1]) || /shift\+tab/.test(tx[1]), tx[1]);
  const kodu = await page.locator('.pane.active .ask-code').allInnerTexts();
  t('edit3: diff je v kartě', kodu.join('\n').includes('const a = 2'), kodu.join('\n').slice(0, 80));
  await page.screenshot({ path: path.join(snimky, 'povoleni-edit3.png') });

  // 5) dvě volby: nic se nepřekládá
  await ukaz('bash2');
  tx = await texty();
  t('bash2: dvě volby bez trvalé, původní popisky', tx.length === 2 && /^1\s*Yes$/.test(tx[0]) && !(await tlac.evaluateAll((n) => n.some((x) => x.classList.contains('perm')))), tx.join(' | '));
  await ukaz('jen_yes_no');
  tx = await texty();
  t('jen_yes_no: bez trvalé', tx.length === 2 && /Yes/.test(tx[0]) && /No/.test(tx[1]), tx.join(' | '));

  // 6) čtyři volby, dvě trvalé; Ne má číslo 4
  await ukaz('bash4');
  tx = await texty();
  const permy = await tlac.evaluateAll((n) => n.map((x) => x.classList.contains('perm')));
  t('bash4: čtyři volby, trvalé jsou 2. a 3.', tx.length === 4 && permy.join() === 'false,true,true,false', permy.join());
  const k2 = klavesy();
  await tlac.nth(3).click();
  await page.waitForTimeout(700);
  t('bash4: Ne pošle 4 (skutečné číslo)', /ANSWER 4/.test(log().split('\n').slice(-3).join('\n')) && klavesy() === k2 + 1);

  // 7) dlouhý, víceřádkový, unicode, úzký, mcp
  for (const [n, re, pocet] of [['dlouhy', /slozka s mezerou/, 3], ['viceradek', /commands|„for"/, 3],
    ['unicode', /příšerná složka/, 3], ['uzky', /\/home\/x\/proj/, 3], ['mcp', /server - tool/, 3]]) {
    await ukaz(n);
    tx = await texty();
    t(`${n}: karta a trvalá volba s popisem`, tx.length === pocet && re.test(tx[1]) && /Napořád|napořád/i.test(tx[1]), (tx[1] || '').slice(0, 140));
    await page.screenshot({ path: path.join(snimky, `povoleni-${n}.png`) });
  }

  // 8) změna velikosti okna za zobrazeného dialogu: jedna odpověď, správné číslo
  await ukaz('bash3');
  await page.waitForTimeout(600);
  await page.setViewportSize({ width: 600, height: 700 });
  await page.waitForTimeout(500);
  await page.setViewportSize({ width: 1100, height: 800 });
  await page.waitForTimeout(500);
  tx = await texty();
  t('resize: karta přežila a má 3 volby', tx.length === 3, tx.join(' | ').slice(0, 100));
  const k3 = klavesy();
  await tlac.nth(0).click();
  await page.waitForTimeout(700);
  t('resize: odpověď jednou', klavesy() === k3 + 1);

  // 9) dialog zmizí dřív než se klikne
  await ukaz('bash3');
  const k4 = klavesy();
  show('clear');
  await page.waitForTimeout(1200);
  t('zmizelý dialog: karta zmizela', await karta.count() === 0);
  t('zmizelý dialog: klik na starou volbu už není kam', (await tlac.count()) === 0 && klavesy() === k4);

  // 10) dva dialogy po sobě: odpověď na první, hned druhý, druhý se dá odpovědět
  await ukaz('bash3');
  await tlac.nth(0).click();
  await page.waitForTimeout(300);
  show('edit3');
  await karta.waitFor({ state: 'visible', timeout: 8000 });
  await page.waitForTimeout(900);
  const k5 = klavesy();
  tx = await texty();
  t('dva dialogy: druhý je Edit s volbou do konce relace', /konce relace/.test(tx[1] || ''), tx[1]);
  await tlac.nth(0).click();
  await page.waitForTimeout(700);
  t('dva dialogy: druhá odpověď odešla', klavesy() === k5 + 1);
} catch (e) {
  t('zkouška doběhla bez výjimky', false, String(e).slice(0, 300));
  await page.screenshot({ path: path.join(snimky, 'povoleni-chyba.png') }).catch(() => {});
}
t('žádné chyby v konzoli stránky', chyby.length === 0, chyby.slice(0, 3).join(' | '));
await browser.close();
const spatne = vysledky.filter((x) => !x.ok);
console.log(`\n${vysledky.length - spatne.length}/${vysledky.length} v pořádku`);
process.exit(spatne.length ? 1 : 0);
