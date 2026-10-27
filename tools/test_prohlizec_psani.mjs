/* Zkouška psaní ve vestavěném prohlížeči Aladina (hub/prohlizec.py + prohlizec.js):
 * klávesy z okna → WebSocket → CDP → stránka v Chromu. Píše se opravdu, přes
 * plátno a skrytou textareu, do inputu, textarey, contenteditable a formuláře.
 *
 *     python3 -m http.server 8999 --bind 127.0.0.1                 # ve složce se stránkou t.html
 *     HUB_BROWSER_PORT=9777 CLAUDE_CONFIG_DIR=/tmp/x HUB_PROHLIZEC=1 \
 *       python3 claude-hub.py --no-browser --test                   # vlastní port a profil!
 *     node tools/test_prohlizec_psani.mjs <url hubu> http://127.0.0.1:8999/t.html
 *
 * Vlastní port je nutný: sdílený Chromium (třeba z Playwright MCP) by stisky
 * posílal zpátky do stránky, ze které přišly, a násobil je.
 */
import { createRequire } from 'node:module';
import fs from 'node:fs';
import path from 'node:path';

function najdi() {
  if (process.env.PLAYWRIGHT_MODULE) return process.env.PLAYWRIGHT_MODULE;
  const root = path.join(process.env.HOME, '.npm/_npx');
  for (const d of fs.existsSync(root) ? fs.readdirSync(root) : []) {
    const m = path.join(root, d, 'node_modules/playwright');
    if (fs.existsSync(m)) return m;
  }
  throw new Error('Playwright nenalezen (PLAYWRIGHT_MODULE=…)');
}
const pw = createRequire(import.meta.url)(najdi());
const [hub, stranka] = process.argv.slice(2);
if (!hub || !stranka) { console.error('použití: node tools/test_prohlizec_psani.mjs <url hubu> <url testovací stránky>'); process.exit(2); }
const b = await pw.chromium.launch();
const p = await (await b.newContext({ viewport: { width: 1500, height: 900 } })).newPage();
await p.goto(hub); await p.waitForTimeout(4500);
await p.evaluate(() => HubProhlizec.otevriVzdy());
await p.waitForSelector('.br-okno', { state: 'attached', timeout: 15000 }).catch(() => {});
await p.waitForTimeout(2500);
console.log('okno:', await p.evaluate(() => !!document.querySelector('.br-okno')), p.url().slice(0, 40));
await p.evaluate((u) => send({ t: 'br', a: 'go', url: u }), stranka);
await p.waitForTimeout(3500);
const cil = async () => { // obsah stránky ve vzdáleném Chromu přes jeho ladicí port
  const r = await fetch(`http://127.0.0.1:${process.env.HUB_BROWSER_PORT}/json`).then((x) => x.json());
  return r.find((t) => t.type === 'page' && t.url.includes(new URL(stranka).host + new URL(stranka).pathname));
};
let spatne = 0;
const c = await cil();
if (!c) { console.log('CHYBA: stránka se ve vzdáleném Chromu nenačetla'); process.exit(1); }
const pwc = await pw.chromium.connectOverCDP(`http://127.0.0.1:${process.env.HUB_BROWSER_PORT}`);
const strana = pwc.contexts().flatMap((x) => x.pages()).find((x) => x.url().includes('t.html'));
const hodnoty = () => strana.evaluate(() => JSON.stringify([i.value, t.value, c.textContent, f.value]));
const klik = async (sel) => { const r = await strana.evaluate((s) => { const e = document.querySelector(s).getBoundingClientRect(); return [e.x + 10, e.y + 10]; }, sel);
  const pl = await p.evaluate(() => { const c = document.querySelector('.br-okno canvas'); const r = c.getBoundingClientRect(); return [r.x, r.y, r.width, r.height, c.width, c.height]; });
  await p.mouse.click(pl[0] + r[0] * pl[2] / pl[4], pl[1] + r[1] * pl[3] / pl[5]); await p.waitForTimeout(250); };
const over = (ok, txt) => { if (!ok) spatne++; console.log(`${ok ? 'OK ' : 'CHYBA'} ${txt}`); };
for (const [sel, nazev, idx] of [['#i', 'input', 0], ['#t', 'textarea', 1], ['#c', 'contenteditable', 2]]) {
  await klik(sel);
  await p.keyboard.type('Ahoj 123 ?:', { delay: 15 });
  await p.keyboard.insertText(' ěščřžýáíé');
  await p.waitForTimeout(500);
  const v = JSON.parse(await hodnoty())[idx];
  over(v === 'Ahoj 123 ?: ěščřžýáíé', `${nazev}: napsáno „${v}“`);
}
await klik('#f'); await p.keyboard.type('ok'); await p.keyboard.press('Enter'); await p.waitForTimeout(400);
over((await strana.evaluate(() => window.sub || 0)) === 1, 'Enter odešle formulář');
await klik('#i'); await p.keyboard.press('Control+a'); await p.keyboard.press('Backspace'); await p.waitForTimeout(300);
over(JSON.parse(await hodnoty())[0] === '', 'Ctrl+A a Backspace smaže pole');
await b.close();
console.log(spatne ? `CHYBA: ${spatne}` : 'OK');
process.exit(spatne ? 1 : 0);
