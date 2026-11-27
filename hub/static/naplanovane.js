/* Naplánované úlohy (hub/naplanovane.py): seznam, přidání a úprava.
 *
 * Sekce v Nastavení a položka „Naplánované“ v panelu vlevo. Úloha je zadání,
 * které Claude dostane sám v nastavený čas — v novém tabu ve zvolené složce.
 * Rozhoduje a spouští hub; tady se jen kreslí a posílá. */
(function (global) {
  'use strict';

  const DNY = ['po', 'út', 'st', 'čt', 'pá', 'so', 'ne'];
  const TYPY = [
    ['denne', 'Každý den'],
    ['pracovni', 'Pracovní dny (po–pá)'],
    ['tydne', 'Vybrané dny v týdnu'],
    ['hodinove', 'Každou hodinu'],
    ['mesicne', 'Jednou za měsíc'],
  ];

  function el(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }

  function kdyText(ts) {
    if (!ts) return '';
    const d = new Date(ts * 1000);
    const dnes = new Date();
    const zitra = new Date(Date.now() + 86400000);
    const cas = d.toLocaleTimeString('cs-CZ', {hour: 'numeric', minute: '2-digit'});
    if (d.toDateString() === dnes.toDateString()) return 'dnes ' + cas;
    if (d.toDateString() === zitra.toDateString()) return 'zítra ' + cas;
    return d.toLocaleDateString('cs-CZ', {weekday: 'short', day: 'numeric', month: 'numeric'}) + ' ' + cas;
  }

  /* Sekce do Nastavení. `io` = hubIO() (api, toast, projekty ve state). */
  function sekce(io) {
    const box = el('div', 'set-sec napl');
    box.appendChild(el('div', 'set-title', 'Naplánované úlohy'));
    box.appendChild(el('div', 'set-note',
      'Claude dostane zadání sám v nastavený čas — třeba „každý den v 7:00 projdi nové objednávky“. ' +
      'Otevře se mu na to nový chat ve vybrané složce, výsledek si přečteš jako každý jiný chat. ' +
      'Běží, jen když běží Aladin; co propadne během vypnutí, doběhne po startu (nejvýš 12 h zpětně).'));
    const seznam = el('div', 'napl-seznam');
    const pridat = el('button', 'btn primary napl-pridat', '+ Nová úloha');
    pridat.type = 'button';
    box.append(pridat, seznam);
    let data = {ulohy: [], domov: ''};

    const chyba = (e) => (io.toast ? io.toast(e.message || String(e)) : alert(e.message || e));

    async function nacti() {
      try {
        data = await io.api('naplanovane');
      } catch (e) {
        seznam.textContent = '';
        seznam.appendChild(el('div', 'set-note', 'Úlohy se nepodařilo načíst: ' + (e.message || e)));
        return;
      }
      kresli();
    }

    function kresli() {
      seznam.textContent = '';
      if (!data.ulohy.length) {
        seznam.appendChild(el('div', 'set-note napl-prazdno', 'Zatím žádná naplánovaná úloha.'));
        return;
      }
      for (const u of data.ulohy) {
        const row = el('div', 'napl-uloha' + (u.zapnuto === false ? ' vypnuto' : ''));
        const hlava = el('div', 'napl-hlava');
        const sw = el('input');
        sw.type = 'checkbox';
        sw.checked = u.zapnuto !== false;
        sw.title = sw.checked ? 'Zapnuto — vypnout' : 'Vypnuto — zapnout';
        sw.onchange = async () => {
          try { await io.api('naplanovane', {akce: 'prepni', id: u.id, zapnuto: sw.checked}); } catch (e) { chyba(e); }
          nacti();
        };
        const nazev = el('div', 'napl-nazev', u.nazev);
        hlava.append(sw, nazev);
        row.appendChild(hlava);
        const info = [u.popis];
        if (u.zapnuto !== false && u.dalsi) info.push('další ' + kdyText(u.dalsi));
        if (u.posledni) info.push('naposledy ' + kdyText(u.posledni));
        row.appendChild(el('div', 'napl-info', info.join(' · ')));
        row.appendChild(el('div', 'napl-slozka', (u.slozka || '').replace(data.domov, '~') + (u.model ? ' · ' + u.model : '')));
        row.appendChild(el('div', 'napl-zadani', u.zadani.length > 220 ? u.zadani.slice(0, 220) + '…' : u.zadani));
        const tl = el('div', 'napl-tlacitka');
        const hned = el('button', 'btn ghost', 'Spustit teď');
        hned.type = 'button';
        hned.onclick = async () => {
          hned.disabled = true;
          try {
            await io.api('naplanovane', {akce: 'spust', id: u.id});
            if (io.toast) io.toast('Úloha běží v novém chatu „🕒 ' + u.nazev + '“.');
          } catch (e) { chyba(e); }
          hned.disabled = false;
          nacti();
        };
        const uprav = el('button', 'btn ghost', 'Upravit');
        uprav.type = 'button';
        uprav.onclick = () => formular(u);
        const smaz = el('button', 'btn ghost napl-smaz', 'Smazat');
        smaz.type = 'button';
        smaz.onclick = async () => {
          if (!confirm('Smazat naplánovanou úlohu „' + u.nazev + '“?')) return;
          try { await io.api('naplanovane', {akce: 'smaz', id: u.id}); } catch (e) { chyba(e); }
          nacti();
        };
        tl.append(hned, uprav, smaz);
        row.appendChild(tl);
        seznam.appendChild(row);
      }
    }

    function formular(u) {
      const stary = box.querySelector('.napl-form');
      if (stary) stary.remove();
      u = u || {kdy: {typ: 'denne', cas: '08:00'}, slozka: '', zadani: '', nazev: '', model: '', zapnuto: true};
      const f = el('form', 'napl-form');
      f.setAttribute('autocomplete', 'off');
      const pole = (popis, input, pozn) => {
        const l = el('label', 'napl-pole');
        l.appendChild(el('span', 'napl-popis', popis));
        l.appendChild(input);
        if (pozn) l.appendChild(el('small', 'set-dim', pozn));
        f.appendChild(l);
        return input;
      };
      const nazev = pole('Název', el('input', 'set-input'));
      nazev.value = u.nazev || '';
      nazev.placeholder = 'Ranní kontrola objednávek';
      const zadani = pole('Co má Claude udělat', el('textarea', 'set-input napl-text'),
        'Tohle dostane Claude jako první zprávu. Piš jako běžné zadání v chatu.');
      zadani.value = u.zadani || '';
      zadani.rows = 5;
      zadani.required = true;

      const slozka = pole('Složka (projekt)', el('select', 'set-input'));
      const projekty = ((io.state && io.state.projects) || []).filter((p) => p && p.path);
      const volby = [[data.domov || '', 'Domovská složka']].concat(projekty.map((p) => [p.path, p.name || p.path]));
      if (u.slozka && !volby.some(([p]) => p === u.slozka)) volby.push([u.slozka, u.slozka]);
      for (const [p, jm] of volby) {
        const o = el('option', null, jm);
        o.value = p;
        slozka.appendChild(o);
      }
      slozka.value = u.slozka || data.domov || '';

      const typ = pole('Jak často', el('select', 'set-input'));
      for (const [k, t] of TYPY) {
        const o = el('option', null, t);
        o.value = k;
        typ.appendChild(o);
      }
      typ.value = (u.kdy && u.kdy.typ) || 'denne';
      const cas = pole('Čas', el('input', 'set-input napl-cas'));
      cas.type = 'time';
      cas.value = (u.kdy && u.kdy.cas) || '08:00';
      const dnyBox = el('div', 'napl-dny');
      const dnyCb = DNY.map((d, i) => {
        const l = el('label', 'napl-den');
        const c = el('input');
        c.type = 'checkbox';
        c.checked = ((u.kdy && u.kdy.dny) || []).includes(i);
        l.append(c, el('span', null, d));
        dnyBox.appendChild(l);
        return c;
      });
      pole('Dny', dnyBox);
      const den = pole('Den v měsíci', el('input', 'set-input napl-cas'));
      den.type = 'number';
      den.min = 1;
      den.max = 28;
      den.value = (u.kdy && u.kdy.den) || 1;
      const model = pole('Model (nepovinné)', el('input', 'set-input'), 'Prázdné = výchozí model. Např. sonnet, opus, haiku.');
      model.value = u.model || '';

      const ukaz = () => {
        const t = typ.value;
        dnyBox.closest('.napl-pole').hidden = !(t === 'tydne' || t === 'hodinove');
        den.closest('.napl-pole').hidden = t !== 'mesicne';
        cas.closest('.napl-pole').querySelector('.napl-popis').textContent =
          t === 'hodinove' ? 'V kolik minut (hodina se nebere)' : 'Čas';
      };
      typ.onchange = ukaz;
      ukaz();

      const tl = el('div', 'napl-tlacitka');
      const ok = el('button', 'btn primary', u.id ? 'Uložit' : 'Naplánovat');
      ok.type = 'submit';
      const zrus = el('button', 'btn ghost', 'Zrušit');
      zrus.type = 'button';
      zrus.onclick = () => f.remove();
      tl.append(ok, zrus);
      f.appendChild(tl);
      f.onsubmit = async (ev) => {
        ev.preventDefault();
        ok.disabled = true;
        const kdy = {typ: typ.value, cas: cas.value,
                     dny: dnyCb.map((c, i) => (c.checked ? i : -1)).filter((i) => i >= 0),
                     den: Number(den.value) || 1};
        try {
          await io.api('naplanovane', {akce: 'uloz', uloha: {
            id: u.id || '', nazev: nazev.value, zadani: zadani.value, slozka: slozka.value,
            model: model.value.trim(), kdy, zapnuto: u.zapnuto !== false}});
          f.remove();
          nacti();
        } catch (e) {
          chyba(e);
        }
        ok.disabled = false;
      };
      box.insertBefore(f, seznam);
      zadani.focus();
    }

    pridat.onclick = () => formular(null);
    seznam.appendChild(el('div', 'set-dim', 'Načítám…'));
    nacti();
    return box;
  }

  global.HubNaplanovane = {sekce};
})(window);
