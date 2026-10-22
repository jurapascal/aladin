/* Vrácení souborů k zadání (hub/rewind.py).
 *
 * U každé zprávy člověka v konverzaci je malé tlačítko „Vrátit soubory do
 * tohoto místa“. Klepnutí otevře PŘEHLED (které soubory se od zprávy měnily,
 * kolik řádků přibylo a ubylo, rozdíly) a nic nemění. Soubory se vrátí až po
 * potvrzení. Smazání souborů, které před zprávou neexistovaly, se potvrzuje
 * zvlášť. Konverzace se nemění, jen soubory; před přepsáním se současný stav
 * uloží do zálohy a jde ho vzít zpět („Vrátit vrácení“).
 *
 *   HubRewind.pripoj(radekZpravy, blok)   blok = {uuid, chat, text}
 */
'use strict';

(function (global) {
  function el(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }

  function kratce(p) {
    return String(p || '').replace(/^\/home\/[^/]+\//, '~/').replace(/^\/Users\/[^/]+\//, '~/');
  }

  function pocet(n, jedna, dve, pet) {
    return n + ' ' + (n === 1 ? jedna : n >= 2 && n <= 4 ? dve : pet);
  }

  /* Výpadek spojení: u zápisu nevíme, jestli server stihl soubory vrátit. */
  async function volej(cesta, telo, zapis) {
    if (typeof global.api !== 'function') throw new Error('Spojení s hubem není k dispozici.');
    try {
      return await global.api(cesta, telo);
    } catch (err) {
      if (!/failed to fetch|networkerror|load failed|aborted/i.test(String(err && err.message))) throw err;
      throw new Error(zapis
        ? 'Spojení s hubem se přerušilo — nevím, jestli se vrácení provedlo. Otevři přehled znovu a zkontroluj to.'
        : 'Spojení s hubem se přerušilo, nic se nezměnilo.');
    }
  }

  async function potvrd(text, o) {
    if (global.HubDialog && global.HubDialog.confirm) return global.HubDialog.confirm(text, o);
    return global.confirm(text);
  }

  function diffEl(text) {
    const pre = el('pre', 'rw-diff');
    for (const radek of String(text || '').split('\n')) {
      pre.appendChild(el('span', radek.startsWith('+') ? 'rw-plus' : radek.startsWith('-') ? 'rw-minus'
        : radek.startsWith('@@') ? 'rw-hunk' : 'rw-ctx', radek + '\n'));
    }
    return pre;
  }

  function otevri(blok, poZavreni) {
    const wrap = el('div', 'rw-wrap');
    wrap.setAttribute('role', 'dialog');
    wrap.setAttribute('aria-modal', 'true');
    const box = el('div', 'rw-box');
    wrap.appendChild(box);
    box.appendChild(el('div', 'rw-title', 'Vrátit soubory do tohoto místa'));
    if (blok.text) box.appendChild(el('div', 'rw-citat', String(blok.text).slice(0, 160)));
    const telo = el('div', 'rw-telo');
    const pata = el('div', 'rw-btns');
    box.append(telo, pata);
    document.body.appendChild(wrap);

    let zavrene = false;
    let zamceno = false;                       // běží požadavek nebo potvrzení — okno nejde zavřít omylem
    function zavri() {
      if (zavrene || zamceno) return;
      zavrene = true;
      window.removeEventListener('keydown', naKlavesu, true);
      wrap.remove();
      if (poZavreni) poZavreni();
    }
    /* Na `window` v zachytávací fázi, tedy dřív než Esc zachytí okno konverzace
       (cteni.js), které by se jinak zavřelo spolu s tímhle. Esc nad potvrzovacím
       oknem zruší jen to. */
    function naKlavesu(ev) {
      if (ev.key !== 'Escape') return;
      ev.stopImmediatePropagation();
      ev.preventDefault();
      const dialog = document.querySelector('.hd-wrap .hd-btns .btn');
      if (dialog) dialog.click();
      else zavri();
    }
    window.addEventListener('keydown', naKlavesu, true);
    /* Klik na pozadí zavírá, jen když i začal na pozadí — tažení výběru textu ven okno nezavře. */
    let dolu = false;
    wrap.addEventListener('mousedown', (ev) => { dolu = ev.target === wrap; });
    wrap.addEventListener('click', (ev) => { if (ev.target === wrap && dolu) zavri(); });

    function tlacitka(...b) { pata.replaceChildren(...b); }
    function tl(text, cls, fn) {
      const b = el('button', 'btn ' + (cls || 'ghost'), text);
      b.type = 'button';
      b.onclick = fn;
      return b;
    }
    function chyba(text, znovu) {
      telo.replaceChildren(el('div', 'rw-chyba', text));
      const b = [tl('Zavřít', 'ghost', zavri)];
      if (znovu) b.unshift(tl('Zkusit znovu', 'primary', nacti));
      tlacitka(...b);
    }

    async function nacti() {
      zamceno = true;
      telo.replaceChildren(el('div', 'rw-info', 'Zjišťuju, které soubory se od téhle zprávy měnily…'));
      tlacitka(tl('Zavřít', 'ghost', zavri));
      let r;
      try {
        r = await volej('rewind-prehled', {chat: blok.chat, uuid: blok.uuid});
      } catch (err) {
        zamceno = false;
        return chyba(err.message || String(err), true);
      }
      zamceno = false;
      if (!zavrene) ukaz(r);
    }

    function sekce(nadpis, seznam, druh) {
      if (!seznam.length) return;
      telo.appendChild(el('div', 'rw-nadpis', nadpis));
      const ul = el('div', 'rw-seznam');
      for (const s of seznam) ul.appendChild(radek(s, druh));
      telo.appendChild(ul);
    }

    function ukaz(r) {
      telo.replaceChildren();
      const soubory = r.soubory || [];
      const mazat = soubory.filter((s) => s.akce === 'smazat');
      const dela = soubory.filter((s) => s.akce === 'obnovit');
      const nejde = soubory.filter((s) => s.akce === 'preskocit');
      telo.appendChild(el('div', 'rw-info',
        'Konverzace zůstane beze změny, vrací se jen soubory. Vrať, až Claude dopracuje — ' +
        'změní-li se soubory mezi přehledem a vrácením, nic se nepřepíše. ' +
        'Nevrací se, co se změnilo příkazy v terminálu (ne úpravou souboru).'));
      if (!dela.length && !mazat.length) {
        telo.appendChild(el('div', 'rw-prazdno', nejde.length
          ? 'Nic, co by šlo vrátit.'
          : 'Od téhle zprávy se nezměnil žádný soubor' + (r.bez_zmeny ? ' (' + r.bez_zmeny + ' je už ve stavu z té doby)' : '') + '.'));
      }
      sekce('Vrátí se do dřívějšího stavu (' + dela.length + ')', dela, 'obnovit');
      sekce('Před touhle zprávou neexistovaly (' + mazat.length + ')', mazat, 'smazat');
      sekce('Přeskočí se (' + nejde.length + ')', nejde, 'preskocit');
      if (r.celkem > soubory.length) {
        telo.appendChild(el('div', 'rw-info', 'Zobrazeno prvních ' + soubory.length + ' z ' + r.celkem + ' souborů.'));
      }
      let smazat = null;
      if (mazat.length) {
        const lab = el('label', 'rw-smazat');
        smazat = el('input');
        smazat.type = 'checkbox';
        lab.append(smazat, el('span', null,
          ' Smazat i ' + pocet(mazat.length, 'soubor', 'soubory', 'souborů') + ', které před touhle zprávou neexistovaly'));
        telo.appendChild(lab);
      }
      const hlavni = tl('', 'primary', () => provest(r, hlavni, smazat));
      const obnov = () => {
        const n = dela.length + (smazat && smazat.checked ? mazat.length : 0);
        hlavni.textContent = 'Vrátit ' + pocet(n, 'soubor', 'soubory', 'souborů');
        hlavni.disabled = n === 0;
      };
      if (smazat) smazat.onchange = obnov;
      obnov();
      tlacitka(tl('Zrušit', 'ghost', zavri), hlavni);
    }

    function radek(s, druh) {
      const r = el('div', 'rw-soubor rw-d-' + druh);
      const hlava = el('div', 'rw-hlava');
      const cesta = el('span', 'rw-cesta', kratce(s.path));
      cesta.title = s.path;
      hlava.appendChild(cesta);
      if (s.plus != null) {
        hlava.appendChild(el('span', 'rw-plus', '+' + s.plus));
        hlava.appendChild(el('span', 'rw-minus', '−' + s.minus));
      }
      r.appendChild(hlava);
      if (s.duvod) r.appendChild(el('div', 'rw-duvod', s.duvod));
      if (s.zdroj === 'prepis') r.appendChild(el('div', 'rw-duvod', 'úprava z posledního kola konverzace'));
      if (s.bez_diffu) r.appendChild(el('div', 'rw-duvod', s.bez_diffu));
      if (s.diff) {
        const d = diffEl(s.diff);
        d.hidden = true;
        const t = el('button', 'rw-toggle', 'Ukázat rozdíly');
        t.type = 'button';
        t.onclick = () => { d.hidden = !d.hidden; t.textContent = d.hidden ? 'Ukázat rozdíly' : 'Skrýt rozdíly'; };
        hlava.appendChild(t);
        r.appendChild(d);
      }
      return r;
    }

    async function provest(r, hlavni, smazat) {
      if (zamceno) return;                    // dvojklik
      const maz = !!(smazat && smazat.checked);
      const n = (r.obnovit || 0) + (maz ? r.smazat : 0);
      const text = 'Přepíše se ' + pocet(r.obnovit || 0, 'soubor', 'soubory', 'souborů') +
        (maz ? ' a SMAŽE ' + pocet(r.smazat, 'soubor', 'soubory', 'souborů') : '') +
        '. Současný stav se předtím uloží do zálohy, takže jde vzít zpět.';
      zamceno = true;
      let ok = false;
      try {
        ok = await potvrd(text, {title: 'Vrátit ' + pocet(n, 'soubor', 'soubory', 'souborů') + '?',
                                 ok: maz ? 'Vrátit a smazat' : 'Vrátit', danger: maz});
      } finally { zamceno = false; }
      if (!ok || zavrene) return;
      zamceno = true;
      hlavni.disabled = true;
      hlavni.textContent = 'Vracím…';
      let out;
      try {
        out = await volej('rewind-proved', {chat: blok.chat, uuid: blok.uuid, token: r.token, smazat: maz}, true);
      } catch (err) {
        zamceno = false;
        return chyba(err.message || String(err), true);
      }
      zamceno = false;
      if (!zavrene) hotovo(out);
    }

    function preskoceno(seznam) {
      if (!seznam || !seznam.length) return;
      telo.appendChild(el('div', 'rw-nadpis', 'Přeskočeno (' + seznam.length + ')'));
      const ul = el('div', 'rw-seznam');
      for (const p of seznam) {
        const r = el('div', 'rw-soubor rw-d-preskocit');
        const c = el('div', 'rw-cesta', kratce(p.path));
        c.title = p.path;
        r.appendChild(c);
        if (p.duvod) r.appendChild(el('div', 'rw-duvod', p.duvod));
        ul.appendChild(r);
      }
      telo.appendChild(ul);
    }

    function hotovo(out) {
      telo.replaceChildren();
      telo.appendChild(el('div', 'rw-ok', out.zaloha
        ? 'Hotovo: ' + pocet(out.vraceno || 0, 'soubor vrácen', 'soubory vráceny', 'souborů vráceno') +
          (out.smazano ? ', ' + pocet(out.smazano, 'smazán', 'smazány', 'smazáno') : '') + '.'
        : 'Nebylo co vracet.'));
      preskoceno(out.preskoceno);
      const b = [tl('Zavřít', 'primary', zavri)];
      if (out.zaloha) {
        telo.appendChild(el('div', 'rw-info', 'Původní stav souborů je uložený v záloze ' + out.zaloha + '.'));
        b.unshift(tl('Vrátit vrácení', 'ghost', () => zpet(out.zaloha)));
      }
      tlacitka(...b);
    }

    async function zpet(zaloha) {
      if (zamceno) return;
      zamceno = true;
      let ok = false;
      try {
        ok = await potvrd('Soubory se vrátí do stavu, v jakém byly před tímhle vrácením. ' +
          'Co jsi od té doby změnil, se nepřepíše.', {title: 'Vrátit vrácení?', ok: 'Vrátit vrácení'});
      } finally { zamceno = false; }
      if (!ok || zavrene) return;
      zamceno = true;
      let out;
      try {
        out = await volej('rewind-zpet', {zaloha}, true);
      } catch (err) {
        zamceno = false;
        return chyba(err.message || String(err), false);
      }
      zamceno = false;
      if (zavrene) return;
      telo.replaceChildren(el('div', 'rw-ok', 'Hotovo: ' +
        pocet(out.obnoveno || 0, 'soubor vrácen', 'soubory vráceny', 'souborů vráceno') + ' do stavu před vrácením.'));
      preskoceno(out.preskoceno);
      tlacitka(tl('Zavřít', 'primary', zavri));
    }

    nacti();
  }

  function pripoj(radek, blok) {
    if (!radek || !blok || !blok.uuid || !blok.chat) return;
    const b = el('button', 'rw-tlacitko', '↶ Vrátit soubory sem');
    b.type = 'button';
    b.title = 'Vrátit soubory do tohoto místa';
    let otevrene = false;
    b.onclick = (ev) => {
      ev.stopPropagation();
      if (otevrene) return;                   // rychlé opakování
      otevrene = true;
      otevri(blok, () => { otevrene = false; });
    };
    radek.appendChild(b);
  }

  global.HubRewind = {pripoj};
})(typeof window !== 'undefined' ? window : globalThis);
