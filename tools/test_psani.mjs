/* Zkouška psaní: do políček v Aladinovi se opravdu píšou znaky a čísla —
 * v Chromiu, Firefoxu i WebKitu (Safari). Hlídá globální obsluhu kláves,
 * která by psaní pohltila (preventDefault, přesměrování do terminálu, fokus).
 *
 *     python3 claude-hub.py --no-browser --test      # v jiném okně
 *     node tools/test_psani.mjs <url> [chromium firefox webkit]
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
const [url, ...jadra] = process.argv.slice(2);
if (!url) { console.error('použití: node tools/test_psani.mjs <url> [jádra]'); process.exit(2); }
const TEXT = 'Ahoj 123 @?{}';
let spatne = 0;
for (const jadro of (jadra.length ? jadra : ['chromium', 'firefox', 'webkit'])) {
  let b;
  try { b = await pw[jadro].launch(); } catch (e) { console.log(`${jadro}: přeskočeno (${String(e.message).split('\n')[0]})`); continue; }
  const p = await (await b.newContext({ viewport: { width: 1300, height: 800 } })).newPage();
  const chyby = []; p.on('pageerror', (e) => chyby.push(e.message));
  await p.goto(url); await p.waitForTimeout(4500);
  const zkus = async (popis, sel, pred) => {
    try {
      if (pred) await pred();
      await p.click(sel); await p.fill(sel, ''); await p.keyboard.type(TEXT, { delay: 15 });
      const v = await p.inputValue(sel);
      const ok = v === TEXT;
      if (!ok) spatne++;
      console.log(`${jadro}: ${ok ? 'OK ' : 'CHYBA'} ${popis}${ok ? '' : ` — napsáno „${v}“`}`);
    } catch (e) { spatne++; console.log(`${jadro}: CHYBA ${popis} — ${String(e.message).split('\n')[0]}`); }
  };
  await zkus('hledání chatů', '#chats-search');
  await zkus('úvodní pole', '#welcome-prompt-input');
  await zkus('hledání v nastavení', '.set-search', async () => { await p.evaluate(() => document.getElementById('btn-settings').click()); await p.waitForTimeout(700); });
  // Zapomenuté tažení panelu (třída body.tahne) nesmí zablokovat políčka —
  // ve Firefoxu user-select:none vypne psaní úplně.
  await p.evaluate(() => { document.body.classList.add('tahne'); document.getElementById('btn-settings').click; });
  const sel = await p.evaluate(() => ['#chats-search', '.set-search'].map((s) => { const e = document.querySelector(s); return e ? getComputedStyle(e).userSelect : 'x'; }));
  const okSel = sel.every((v) => v !== 'none');
  if (!okSel) spatne++;
  console.log(`${jadro}: ${okSel ? 'OK ' : 'CHYBA'} políčka při zapomenutém tažení (user-select: ${sel.join(', ')})`);
  await p.keyboard.press('Escape');
  const uklid = await p.evaluate(() => !document.body.classList.contains('tahne'));
  if (!uklid) spatne++;
  console.log(`${jadro}: ${uklid ? 'OK ' : 'CHYBA'} Escape ukončí zapomenuté tažení`);
  if (chyby.length) { spatne++; console.log(`${jadro}: CHYBA výjimky na stránce: ${chyby[0]}`); }
  await b.close();
}
console.log(spatne ? `CHYBA: ${spatne}` : 'OK');
process.exit(spatne ? 1 : 0);
