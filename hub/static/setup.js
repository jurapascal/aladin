/* Instalace v okně (hub/setup.py) — první spuštění instalačky ke stažení.
 *
 * Hub se pustil ze staženého zdroje s `--setup`: tabů tu ještě není kde
 * běžet, tak okno ukáže jen tohle. Instalace naběhne sama, průběh je vidět
 * jako seznam kroků (řádky instalačky s ✓ ▸ ⚠) a celý výpis je pod
 * „Podrobnosti". Po doběhnutí se appka restartuje do nainstalované verze
 * a tam pokračuje průvodce (onboarding.js).
 */
'use strict';

(function (global) {

  const POLL = 700;
  // Řádek instalačky: „  ✓ Obsidian", „  ▸ stahuju prohlížeč…", „  ⚠ …".
  const STEP = /^\s*([✓▸⚠])\s+(.+)$/;

  function el(tag, cls, text) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text != null) node.textContent = text;
    return node;
  }

  function open(io) {
    const root = el('div', 'onb setup');
    root.innerHTML = `
      <div class="onb-box">
        <div class="onb-head">
          <span class="onb-mark"></span>
          <div>
            <div class="onb-title">Instalace Aladina</div>
            <div class="onb-sub">Chvilku to potrvá — všechno se nastaví samo.</div>
          </div>
        </div>
        <div class="onb-body">
          <div class="setup-state"></div>
          <ul class="setup-steps"></ul>
          <details class="setup-log"><summary>Podrobnosti</summary><pre></pre></details>
        </div>
        <div class="onb-foot">
          <span class="spacer"></span>
          <button class="btn ghost setup-retry" hidden>Zkusit znovu</button>
          <button class="btn primary setup-go" disabled>Instaluju…</button>
        </div>
      </div>`;
    const mark = document.querySelector('#i-hub')
      ? '<svg viewBox="0 0 24 24" width="34" height="34"><use href="#i-hub"/></svg>' : '';
    root.querySelector('.onb-mark').innerHTML = mark;
    document.body.appendChild(root);

    const stateBox = root.querySelector('.setup-state');
    const steps = root.querySelector('.setup-steps');
    const log = root.querySelector('.setup-log pre');
    const details = root.querySelector('.setup-log');
    const go = root.querySelector('.setup-go');
    const retry = root.querySelector('.setup-retry');
    let timer = null;
    let shown = '';

    function say(text, kind) {
      stateBox.className = 'setup-state ' + (kind || '');
      stateBox.textContent = text;
    }

    function draw(st) {
      const lines = st.lines || [];
      // Překreslit i po doběhnutí — poslední krok už se nemá točit.
      const key = lines.length + (st.running ? 'r' : 'd');
      if (key !== shown) {
        shown = key;
        const atBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 40;
        log.textContent = lines.join('\n');
        if (atBottom) log.scrollTop = log.scrollHeight;
        steps.textContent = '';
        const items = lines.map((l) => STEP.exec(l)).filter(Boolean);
        items.forEach((m, i) => {
          const last = i === items.length - 1;
          const kind = m[1] === '✓' ? 'ok' : m[1] === '⚠' ? 'warn'
            : (last && st.running ? 'busy' : 'info');
          steps.appendChild(el('li', kind, m[2]));
        });
        steps.scrollTop = steps.scrollHeight;
      }
      if (st.running) {
        say('Instaluju… okno nezavírej.', 'busy');
        go.disabled = true;
        go.textContent = 'Instaluju…';
        retry.hidden = true;
      } else if (st.done && st.ok) {
        say('✓ Hotovo. Aladin je nainstalovaný.', 'ok');
        go.disabled = false;
        go.textContent = 'Otevřít Aladina';
        retry.hidden = true;
        go.focus();
      } else if (st.done) {
        say(st.error || 'Instalace se nepovedla.', 'err');
        details.open = true;
        go.disabled = !st.installed;
        go.textContent = st.installed ? 'Otevřít i tak' : 'Instaluju…';
        retry.hidden = false;
      }
    }

    async function poll() {
      clearTimeout(timer);
      let st = null;
      try { st = await io.api('setup'); } catch (_) { /* hub chvilku neodpověděl */ }
      if (st) draw(st);
      if (!st || st.running || !st.done) timer = setTimeout(poll, POLL);
    }

    async function start() {
      shown = '';
      try { draw(await io.api('setup', {action: 'start'})); } catch (err) {
        console.warn('setup.js: start', err);
        say('Instalaci se nepodařilo spustit. Zkus to znovu.', 'err');
        retry.hidden = false;
        return;
      }
      poll();
    }

    go.onclick = async () => {
      go.disabled = true;
      go.textContent = 'Otevírám…';
      try {
        await io.api('setup', {action: 'launch'});
        say('Otevírám nainstalovanou aplikaci… Tohle okno se může zavřít.', 'ok');
        // Nové okno otevře nová instance; tohle už nemá komu patřit.
        setTimeout(() => { try { window.close(); } catch (_) { /* nic */ } }, 2500);
      } catch (err) {
        console.warn('setup.js: launch', err);
        say('Aplikaci se nepodařilo spustit. Otevři ji z nabídky aplikací ' +
            '(Aladin, případně ještě pod starým názvem Claude Code Hub).', 'err');
        go.disabled = false;
        go.textContent = 'Otevřít Aladina';
      }
    };
    retry.onclick = start;

    // Když okno naběhne podruhé (reload), instalace už může běžet nebo být hotová.
    io.api('setup').then((st) => {
      if (st.running || st.done) { draw(st); poll(); } else start();
    }, start);
  }

  global.HubSetup = {open};
})(window);
