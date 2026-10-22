/* Claudíci — hřiště nad polem na psaní.
 *
 * Hlavní Claudík (jantarový) je Claude v tomhle tabu: když pracuje, chodí,
 * když čeká na odpověď, zvedne otazník a poskakuje, když je hotový, stojí.
 * Pomocníci (agenti, cteni.js) z něj vyskočí, pobíhají a občas si předají
 * papír; po práci doběhnou zpátky a papír s výsledkem mu donesou. Zpráva
 * z jiného chatu (nebo do něj) přiletí / odletí jako obálka.
 *
 * Je to jen ozdoba: nic se tu nerozhoduje, kreslí se podle událostí z čtení
 * a z bubliny. Hřiště se ukáže, když se něco děje, a po chvíli klidu zmizí.
 */
'use strict';

(function (global) {

  const DOMOV = 5;             // kde hlavní Claudík stojí (% šířky)
  const OD = 16, DO = 94;      // kudy pobíhají pomocníci
  const SCHOVAT_PO = 2500;     // ms klidu, než hřiště zmizí
  const PREDAVKA = [3500, 7000];  // jak často si pomocníci něco předají

  const nahoda = (a, b) => a + Math.random() * (b - a);

  function el(cls, text) {
    const node = document.createElement('div');
    node.className = cls;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  /* `svg(typ)` kreslí postavičku (cteni.js claudik) — tady se nekreslí znovu,
     ať Claudík vypadá na kartě agenta i na hřišti stejně. */
  function hriste(root, svg) {
    const box = el('hriste');
    box.hidden = true;
    box.setAttribute('aria-hidden', 'true');
    root.appendChild(box);

    /* Kouzelná lampa vedle domovského místa hlavního Claudíka: při práci se
       potřásá a když vyskočí nový pomocník (džin), z ní vyvane kouř. */
    const lampa = el('cl-lampa');
    lampa.style.left = DOMOV + '%';
    lampa.innerHTML = '<svg viewBox="38 56 60 32" aria-hidden="true">' +
      '<path d="M58 81 H78 L76 86 H60Z" fill="#e8a02c"/>' +
      '<path d="M48 74 C48 66 58 64 66 64 C76 64 84 68 84 74 C84 80 76 82 66 82 C56 82 48 80 48 74Z" fill="#f4bd48"/>' +
      '<ellipse cx="66" cy="64" rx="7" ry="2" fill="#f4bd48"/><circle cx="66" cy="60.5" r="2.6" fill="#f4bd48"/>' +
      '<path d="M82 70 C88 70 90 65 94 61 C93 70 90 76 81 78Z" fill="#f4bd48"/>' +
      '<path d="M49 70 C41 69 40 78 47 79" fill="none" stroke="#f4bd48" stroke-width="3.2" stroke-linecap="round"/>' +
      '<path d="M53 73 C55 69 60 68 65 68" fill="none" stroke="#fff6d6" stroke-width="2" stroke-linecap="round" opacity=".7"/></svg>';
    box.appendChild(lampa);
    function kour() {
      const k = el('cl-kour', '✦');
      lampa.appendChild(k);
      setTimeout(() => k.remove(), 1500);
    }

    let zivy = true;
    let praceOn = false, cekaOn = false;
    let letu = 0;                        // papíry a obálky ve vzduchu
    let schovatTimer = null, predavkaTimer = null;
    const pomocnici = new Map();         // id → figurka

    function figurka(typ, barva, cls) {
      const f = {el: el('cl-fig ' + (cls || '')), x: DOMOV, t: null, t2: null, konci: false, rec: null, recT: null, emoEl: null, emoT: null};
      f.el.style.setProperty('--ag', barva || 'var(--accent)');
      f.bublina = el('cl-bublina');
      const telo = el('cl-postava');
      telo.innerHTML = svg(typ);
      f.el.append(f.bublina, telo);
      f.el.style.left = f.x + '%';
      box.appendChild(f.el);
      return f;
    }

    const hlavni = figurka('', 'var(--accent)', 'hlavni');

    /* ── řeč ─────────────────────────────────────────────────────────────────
       Bublina s větou nad postavičkou. Jedna na postavičku, po chvíli zmizí;
       kdo mluví moc často, nic navíc neřekne (aby to nebyl šum). */
    const vyber = (pole) => pole[Math.floor(Math.random() * pole.length)];
    const zkrat = (t, n) => {
      t = String(t || '').replace(/\s+/g, ' ').trim();
      return t.length > n ? t.slice(0, n - 1).trimEnd() + '…' : t;
    };
    function mluv(f, text, ms, pak) {
      if (!zivy || !text) return;
      const ted = Date.now();
      if (f.mluvilOd && ted - f.mluvilOd < 1400 && !pak) return;
      f.mluvilOd = ted;
      if (f.rec) f.rec.remove();
      clearTimeout(f.recT);
      f.rec = el('cl-rec', text);
      f.rec.classList.toggle('vpravo', f.x > 62);
      f.el.appendChild(f.rec);
      f.recT = setTimeout(() => { if (f.rec) { f.rec.remove(); f.rec = null; } if (pak) pak(); }, ms || 2600);
    }
    /* Bič: z hlavního k pomocníkovi — rozmáchne se a práskne. */
    function bic(cil, text) {
      if (!zivy) return;
      const od = hlavni.x, k = cil.x;
      const b = el('cl-bic');
      b.style.left = Math.min(od, k) + '%';
      b.style.width = Math.max(4, Math.abs(k - od)) + '%';
      b.classList.toggle('zpet', k < od);
      b.appendChild(el('cl-prask', 'práásk!'));
      box.appendChild(b);
      hlavni.el.classList.remove('bicuje');
      void hlavni.el.offsetWidth;
      hlavni.el.classList.add('bicuje');
      cil.el.classList.add('trhne');
      setTimeout(() => { hlavni.el.classList.remove('bicuje'); cil.el.classList.remove('trhne'); b.remove(); }, 900);
    }
    /* ── emoji nad hlavou: co Claudík právě dělá ─────────────────────────────
       Přemýšlí 💭, čte 👀, píše ✍️, hledá 🔍, běží příkaz ⚡; hotovo ✅, chyba ❌,
       čeká na odpověď ❓. Emoji se nenosí na místě — Claudík si s ním hraje
       (přehazuje ho, jako když žongluje), ať je vidět, že žije. */
    const EMO_NASTROJ = {
      Read: '👀', Grep: '🔍', Glob: '🔍', Edit: '✍️', Write: '📝', NotebookEdit: '📝',
      Bash: '⚡', WebFetch: '🌐', WebSearch: '🔎', Task: '📣', Agent: '📣', TodoWrite: '📋',
      AskUserQuestion: '❓', Monitor: '📡', ScheduleWakeup: '⏰', SendMessage: '✉️',
    };
    const emoNastroje = (jmeno) => EMO_NASTROJ[jmeno] ||
      (/^mcp__playwright__/.test(jmeno || '') ? '🧭' : /^mcp__/.test(jmeno || '') ? '🔌' : '⚙️');
    /* `trida`: mysli (houpe se) · pinka (přehazuje) · hotovo (vyskočí) · chyba (zatřese se).
       `ms` = jak dlouho; 0 = zůstane, dokud ho něco nevymění. */
    function emo(f, znak, trida, ms, pak) {
      if (!zivy) return;
      clearTimeout(f.emoT);
      if (f.emoEl) f.emoEl.remove();
      f.emoEl = null;
      f.el.classList.remove('ma-emo');
      if (!znak) return;
      f.emoEl = el('cl-emo ' + (trida || 'mysli'), znak);
      f.el.appendChild(f.emoEl);
      f.el.classList.add('ma-emo');
      if (ms) {
        f.emoT = setTimeout(() => {
          if (f.emoEl) f.emoEl.remove();
          f.emoEl = null;
          f.el.classList.remove('ma-emo');
          if (pak) pak();
        }, ms);
      }
    }
    // Co dělá hlavní, když zrovna nic jiného nehlásí: přemýšlí.
    const premysli = () => { if (praceOn && !cekaOn) emo(hlavni, '💭', 'mysli', 0); };

    const TOKY = {
      zadani: ['Jdi na to!', 'Máš práci!', 'Pracovat!', 'Šup, šup!'],
      odpoved: ['Jasně, šéfe!', 'Už běžím!', 'Rozkaz!', 'Hned to bude!'],
      kontrola: ['Tak co, jak to jde?', 'Jak jsi daleko?', 'Něco nového?'],
      stav: ['Pracuju na tom!', 'Ještě chvilku!', 'Skoro to mám.'],
      hotovo: ['Hotovo, šéfe!', 'Mám to!', 'Tady je výsledek!'],
      diky: ['Skvělá práce!', 'Díky!', 'Dobře jsi to zvládl.'],
      chyba: ['Au, nepovedlo se…', 'Něco se pokazilo.'],
      utecha: ['Nevadí, příště líp!', 'Zkusíme to jinak.'],
      predani: ['Koukni na tohle!', 'Tohle se ti bude hodit.', 'Mám pro tebe info!'],
      prijato: ['Díky, beru!', 'Super, mrknu na to.', 'Dobře, dík!'],
    };

    function jdi(f, x, pak) {
      clearTimeout(f.t);
      const ms = Math.round(350 + Math.abs(x - f.x) * 26);
      f.el.style.transitionDuration = ms + 'ms';
      if (Math.abs(x - f.x) > 0.5) f.el.classList.toggle('vlevo', x < f.x);
      f.el.classList.add('chodi');
      f.x = x;
      f.el.style.left = x + '%';
      f.t = setTimeout(() => {
        f.el.classList.remove('chodi');
        if (pak && zivy) pak();
      }, ms);
    }

    function stuj(f) {
      clearTimeout(f.t);
      clearTimeout(f.t2);
      f.el.classList.remove('chodi');
    }

    // Pomocník se toulá, dokud nemá hotovo (a nepředává zrovna papír).
    function toulej(f) {
      if (!zivy || f.konci || f.predava) return;
      jdi(f, nahoda(OD, DO), () => {
        f.t2 = setTimeout(() => toulej(f), nahoda(250, 1400));
      });
    }

    // Hlavní Claudík při práci přechází kolem svého místa.
    function prechazej() {
      if (!zivy || !praceOn || cekaOn) return;
      jdi(hlavni, nahoda(DOMOV, 30), () => {
        hlavni.t2 = setTimeout(prechazej, nahoda(400, 1600));
      });
    }

    /* Papír letí od jedné postavičky ke druhé (nebo za okraj a zpoza něj). */
    function papir(odX, kamX, znak, pak) {
      const p = el('cl-papir');
      const vnitrek = el('cl-papir-v', znak || '');
      if (!znak) vnitrek.classList.add('list');
      p.appendChild(vnitrek);
      p.style.left = odX + '%';
      box.appendChild(p);
      letu++;
      ukaz();
      const ms = Math.round(450 + Math.abs(kamX - odX) * 9);
      p.style.transitionDuration = ms + 'ms';
      vnitrek.style.animationDuration = ms + 'ms';
      requestAnimationFrame(() => requestAnimationFrame(() => { p.style.left = kamX + '%'; }));
      setTimeout(() => {
        p.remove();
        letu--;
        if (zivy && pak) pak();
        moznaSchovat();
      }, ms + 30);
    }

    function radost(f) {
      f.el.classList.remove('radost');
      void f.el.offsetWidth;            // animace znovu od začátku
      f.el.classList.add('radost');
      setTimeout(() => f.el.classList.remove('radost'), 700);
    }

    /* Dva pomocníci si občas něco předají — ten první doběhne k druhému. */
    function naplanujPredavku() {
      clearTimeout(predavkaTimer);
      predavkaTimer = setTimeout(() => {
        if (!zivy) return;
        const volni = [...pomocnici.values()].filter((f) => !f.konci && !f.predava);
        if (volni.length >= 2) {
          const a = volni[Math.floor(Math.random() * volni.length)];
          const b = volni.filter((f) => f !== a)[Math.floor(Math.random() * (volni.length - 1))];
          a.predava = true;
          stuj(a);
          jdi(a, Math.max(OD, Math.min(DO, b.x + (a.x < b.x ? -5 : 5))), () => {
            mluv(a, vyber(TOKY.predani), 1800);
            setTimeout(() => papir(a.x, b.x, '', () => {
              radost(b);
              mluv(b, vyber(TOKY.prijato), 1800);
              a.predava = false;
              toulej(a);
            }), 700);
          });
        }
        if (pomocnici.size >= 2) naplanujPredavku();
      }, nahoda(...PREDAVKA));
    }

    /* Hlavní Claudík se občas zeptá, jak to jde, a někdo z pomocníků odpoví. */
    let kontrolaTimer = null;
    function naplanujKontrolu() {
      clearTimeout(kontrolaTimer);
      kontrolaTimer = setTimeout(() => {
        if (!zivy) return;
        const bezi = [...pomocnici.values()].filter((f) => !f.konci && !f.predava && !f.cekaNaZadani);
        if (bezi.length) {
          const f = vyber(bezi);
          mluv(hlavni, vyber(TOKY.kontrola), 2000);
          setTimeout(() => mluv(f, vyber(TOKY.stav), 2000), 1400);
        }
        if (pomocnici.size) naplanujKontrolu();
      }, nahoda(9000, 15000));
    }

    function nekdoJe() {
      return praceOn || cekaOn || pomocnici.size > 0 || letu > 0;
    }

    function ukaz() {
      clearTimeout(schovatTimer);
      schovatTimer = null;
      if (box.hidden) {
        box.hidden = false;
        root.classList.add('hriste-on');
      }
    }

    function moznaSchovat() {
      if (nekdoJe() || schovatTimer) return;
      schovatTimer = setTimeout(() => {
        schovatTimer = null;
        if (nekdoJe() || !zivy) return;
        box.hidden = true;
        root.classList.remove('hriste-on');
      }, SCHOVAT_PO);
    }

    const PREVLEK = {
      low: 'Dám si to v klidu. 😴', medium: 'Jdu na to normálně.', high: 'Helmu na hlavu, makáme!',
      xhigh: 'Na tohle je potřeba superhrdina!', max: 'Teď přijde to pravé kouzlo! ✨',
    };
    let oblek = '';

    return {
      /* Úsilí (effort), se kterým Claude jede — Claudík se podle něj převlékne. */
      effort(e) {
        e = /^(low|medium|high|xhigh|max)$/.test(e || '') ? e : '';
        if (!zivy || e === oblek) return;
        const zmena = !!oblek;
        oblek = e;
        if (e) hlavni.el.dataset.effort = e; else delete hlavni.el.dataset.effort;
        if (zmena && e && !box.hidden) {
          hlavni.el.classList.remove('prevlek');
          void hlavni.el.offsetWidth;
          hlavni.el.classList.add('prevlek');
          mluv(hlavni, PREVLEK[e], 2200);
        }
      },
      /* Claude pracuje / přestal. */
      prace(on) {
        if (!zivy || praceOn === !!on) return;
        praceOn = !!on;
        lampa.classList.toggle('trese', praceOn);
        if (praceOn) {
          ukaz();
          hlavni.el.classList.remove('hotovo');
          premysli();
          prechazej();
        } else {
          stuj(hlavni);
          if (!cekaOn) emo(hlavni, '✅', 'hotovo', 2200);
          jdi(hlavni, DOMOV, () => {
            hlavni.el.classList.remove('vlevo');
            if (!cekaOn) { hlavni.el.classList.add('hotovo'); radost(hlavni); }
          });
          moznaSchovat();
        }
      },
      /* Claude spustil nástroj — emoji podle toho, co dělá, a pak zase přemýšlí. */
      nastroj(jmeno) {
        if (!zivy || !praceOn || cekaOn) return;
        ukaz();
        emo(hlavni, emoNastroje(jmeno), 'pinka', 2300, premysli);
      },
      /* Nástroj skončil chybou. */
      chyba() {
        if (!zivy) return;
        ukaz();
        hlavni.el.classList.remove('trese');
        void hlavni.el.offsetWidth;
        hlavni.el.classList.add('trese');
        setTimeout(() => hlavni.el.classList.remove('trese'), 700);
        emo(hlavni, '❌', 'chyba', 2400, premysli);
        moznaSchovat();
      },
      /* Claude čeká na odpověď — otazník nad hlavou, poskakuje. */
      ceka(on) {
        if (!zivy || cekaOn === !!on) return;
        cekaOn = !!on;
        hlavni.el.classList.toggle('ceka', cekaOn);
        if (cekaOn) {
          ukaz();
          stuj(hlavni);
          hlavni.el.classList.remove('hotovo');
          emo(hlavni, '❓', 'pinka', 0);
        } else {
          emo(hlavni, '');
          premysli();
          if (praceOn) prechazej();
          moznaSchovat();
        }
      },
      /* Nový pomocník. `tise` = byl tu už před otevřením tabu, bez výskoku. */
      pridej(id, typ, barva, tise, ukol) {
        if (!zivy || pomocnici.has(id)) return;
        ukaz();
        const f = figurka(typ, barva, 'pomocnik' + (tise ? '' : ' vznik'));
        if (!tise) kour();
        f.x = tise ? nahoda(OD, DO) : hlavni.x;
        f.el.style.left = f.x + '%';
        pomocnici.set(id, f);
        if (!tise) radost(hlavni);
        emo(f, '💭', 'mysli', 0);
        // Hlavní Claudík pomocníkovi zadá práci, práskne bičem a ten odpoví.
        f.cekaNaZadani = !tise;
        setTimeout(() => {
          if (!tise) {
            const x = Math.min(DO, Math.max(OD, hlavni.x + 14 + nahoda(0, 22)));
            jdi(f, x, () => {
              f.cekaNaZadani = false;
              mluv(hlavni, ukol ? 'Jdi: ' + zkrat(ukol, 34) : vyber(TOKY.zadani), 2800);
              setTimeout(() => {
                bic(f, 'práásk');
                setTimeout(() => { mluv(f, vyber(TOKY.odpoved), 1800); toulej(f); }, 450);
              }, 1100);
            });
          } else toulej(f);
        }, tise ? 0 : 450);
        if (pomocnici.size === 2) naplanujPredavku();
        if (!tise) naplanujKontrolu();
      },
      /* Pomocník dělá další krok — občas to řekne nahlas. */
      krok(id, text) {
        const f = pomocnici.get(id);
        if (!f || f.konci || !text) return;
        const ted = Date.now();
        if (f.krokOd && ted - f.krokOd < 7000) return;
        f.krokOd = ted;
        mluv(f, zkrat(text, 30), 2400);
        emo(f, emoNastroje(String(text).includes('Edit') ? 'Edit' : String(text).includes('grep') || String(text).includes('⌕') ? 'Grep' : 'Read'), 'pinka', 2000,
            () => { if (!f.konci) emo(f, '💭', 'mysli', 0); });
      },
      /* Pomocník doběhl: s výsledkem k hlavnímu, s chybou jen odejde. */
      hotovo(id, stav, tise) {
        const f = pomocnici.get(id);
        if (!f || f.konci) return;
        f.konci = true;
        stuj(f);
        const pryc = () => {
          f.el.classList.add('odchazi');
          setTimeout(() => {
            f.el.remove();
            pomocnici.delete(id);
            moznaSchovat();
          }, 500);
        };
        if (tise || !zivy) { f.el.remove(); pomocnici.delete(id); moznaSchovat(); return; }
        emo(f, stav === 'hotovo' ? '✅' : stav === 'chyba' ? '❌' : '⏹️', stav === 'hotovo' ? 'hotovo' : stav === 'chyba' ? 'chyba' : 'mysli', 2600);
        if (stav !== 'hotovo') {
          f.el.classList.add(stav === 'chyba' ? 'chyba' : 'stop');
          if (stav === 'chyba') {
            // Selhal: přiběhne to říct, šéf ho potěší (a malinko pohrozí bičem).
            jdi(f, Math.min(DO, hlavni.x + 7), () => {
              mluv(f, vyber(TOKY.chyba), 2200);
              setTimeout(() => { mluv(hlavni, vyber(TOKY.utecha), 2200); bic(f); }, 900);
              setTimeout(pryc, 2600);
            });
          } else setTimeout(pryc, 900);
          return;
        }
        f.el.classList.add('hotovo');
        jdi(f, Math.min(DO, hlavni.x + 7), () => {
          f.el.classList.add('vlevo');
          mluv(f, vyber(TOKY.hotovo), 1800);
          papir(f.x, hlavni.x, '', () => {
            radost(hlavni);
            mluv(hlavni, vyber(TOKY.diky), 2000);
            setTimeout(pryc, 900);
          });
        });
      },
      /* Zpráva mezi chaty: `ven` odletí za okraj, `dovnitr` přiletí. */
      dopis(smer) {
        if (!zivy) return;
        if (smer === 'ven') papir(hlavni.x, 104, '✉');
        else papir(104, hlavni.x, '✉', () => radost(hlavni));
      },
      release() {
        zivy = false;
        clearTimeout(schovatTimer);
        clearTimeout(predavkaTimer);
        clearTimeout(kontrolaTimer);
        for (const f of [hlavni, ...pomocnici.values()]) { stuj(f); clearTimeout(f.recT); clearTimeout(f.emoT); }
        box.remove();
        root.classList.remove('hriste-on');
      },
    };
  }

  global.HubClaudici = {hriste};
})(window);
