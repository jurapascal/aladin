/* Bublina místo vstupního řádku.
 *
 * Terminál zůstává terminálem — jen jeho spodek, kde Claude Code kreslí svoje
 * vstupní pole, překryje HTML bublina: textové pole, přepínač modelu, slash
 * příkazy, příloha a režimy. Píše se do bubliny, odesláním se text pošle do
 * PTY, takže Claude Code dostane přesně to, co by dostal z klávesnice.
 *
 * Proč překryv a ne obyčejný blok pod terminálem: kdyby terminál zůstal celý,
 * bylo by vstupní pole vidět dvakrát — jednou Claudeovo, jednou naše.
 *
 * Bublina se sama uklidí, kdykoli dole v terminálu není klidný prompt:
 * u výběru modelu, dotazu na oprávnění nebo při listování historií by pod ní
 * zmizelo právě to, na co se má člověk podívat. Zbyde po ní úzký proužek,
 * kterým se dá vrátit zpátky.
 */
'use strict';

(function (global) {

  // Kolik spodních řádků terminálu se čte při rozhodování, jestli je dole
  // klidný prompt. Vstupní pole i s nápovědou pod ním má do šesti řádků.
  const PROBE_ROWS = 8;
  // Jak dlouho musí prompt vydržet, než do něj odejde zpráva z fronty (tab
  // právě vznikl). Claude Code ho kreslí dřív, než doběhne start.
  const READY_MS = 1500;

  /* Claude Code při práci kreslí nad vstupním polem řádek jako
       ✽ Bloviating… (8s · ↓ 437 tokens · thinking with xhigh effort)
     a do nápovědy pod ním přidá „esc to interrupt". Naměřeno na 2.1.278.
     Hlavní je řádek s hvězdičkou: nápověda se na úzkém terminálu (telefon)
     ořízne na „· es…" a „esc to interrupt" v ní vůbec není. Slovo je vždy
     jedno anglické s velkým písmenem — podle toho se řádek neplete s textem
     odpovědi, kde může odrážka taky začínat hvězdičkou. */
  const WORKING = /esc to interrupt/i;
  const SPINNER = /^\s*[·✢*✶✻✽∗]\s+([A-Z][A-Za-z-]+)…(?:\s*\((.*))?\s*$/;
  const WORK_ROWS = 14;       // spinner je nad vstupním polem, i víceřádkovým
  // Wrapper to napíše, když Clauda něco zabije — nejčastěji jádro, kterému
  // došla paměť (agent-wrapper.sh). Ve čtení je terminál schovaný, tak se
  // z toho udělá hláška.
  const KILLED = /Claude Code byl násilně ukončen/;

  /* Jak vypadá spodek Claude Code, když jen čeká na zadání (naměřeno, ne
     odhadnuto):

         ────────────────────────────────
         ❯
         ────────────────────────────────
           ⏵⏵ bypass permissions on (shift+tab to cycle)

     Šipka ❯ sama o sobě nestačí — stejnou kreslí i vybraná položka v dialogu
     („❯ No, exit"). Rozhoduje se proto podle dvojice: řádek se šipkou a pod
     ním nápověda k režimu nebo zkratkám. */
  const PROMPT = /^\s*[│|]?\s*[❯>›]\s*$|^\s*[│|]?\s*[❯>›]\s+\S/;
  const HINT = /(shift\+tab to cycle|for shortcuts|bypass permissions on|accept edits on|plan mode on|auto-accept edits|manual mode on)/i;

  // Dotazy, které Claude Code kreslí místo vstupního pole. Bublina jim musí
  // uhnout, jinak se odpovídá naslepo.
  const DIALOG = /(Esc to cancel|Do you want|Would you like|Proceed\?|\(y\/n\)|\[y\/N\]|to confirm|^\s*❯?\s*\d+\.\s|↑\/↓)/i;

  /* Číslovaná volba v dialogu: „❯ 1. Yes" — i s okrajem rámečku po stranách.
     Podle ní se dialog přenese do tlačítek, aby se dalo odpovídat myší a ne
     hledáním v terminálu. */
  const OPTION = /^[\s│|]*(❯|>)?\s*(\d+)[.)]\s+(\S.*?)[\s│|]*$/;

  // Výběr ze seznamu (historie promptů, volba modelu): ovládá se šipkami.
  const PICKER = /(↑\/↓|to navigate)/i;
  const YESNO = /\(y\/n\)|\[y\/N\]/i;

  /* Panel, ze kterého se jen odchází — „Rewind / Nothing to rewind to yet. /
     Esc to cancel". Volbu nenabízí, takže by po něm zbyl holý terminál a ven
     by se člověk musel trefit klávesou. Dostane kartu s jediným tlačítkem.
     „esc to interrupt" (Claude zrovna pracuje) sem schválně nepatří. */
  const CANCEL = /(^|\s)(esc|escape) to (cancel|close|exit|go back)\b/i;
  const CANCEL_LINE = /^(esc|escape) to (cancel|close|exit|go back)\.?$/i;
  // Delší panel je spíš výpis než hláška — tomu ať zůstane terminál.
  const NOTE_MAX = 8;

  /* Přihlášení (/login, první start bez účtu): Claude Code ukáže odkaz a pod
     ním čeká na kód z přihlašovací stránky. Není to prompt ani dialog s
     volbami — jen řádek na vložení, a bez bubliny by kód nebylo kam dát. */
  const LOGIN_CODE = /Paste (the )?code here/i;
  // Hláška, po které Claude Code čeká jen na Enter: po přihlášení („continue")
  // i po neplatném kódu („retry" — jinak by se z chyby nedalo myší ven).
  const CONTINUE = /Press Enter to (continue|retry)/i;
  /* Obrazovky, na kterých musí být vidět terminál, i když je tab ve čtení
     (cteni.js): přihlašovací odkaz, čekání na kód, „Press Enter" po přihlášení
     a konec Claude Code (po /logout zbyde shell). Nic z toho se do přepisu
     konverzace nezapíše, takže pod čtením by zůstala prázdná stránka. */
  // Claude Code skončil a v tabu zbyl shell (hub/core.py cmd_agent).
  const ENDED = /\[ session ukon/;
  const TERM_NEEDED = /oauth\/authorize|Opening browser to sign in|Browser didn't open\?|Paste (the )?code here|Press Enter to (continue|retry)|\[ session ukon/i;

  /* Dialog, který volby nečísluje — stojí prostě pod sebou a vybranou označuje
     jedině šipka („Yes, I trust this folder" hned po startu). Že jde o nabídku
     a ne o výpis, řekne až nápověda s Enterem: samotná šipka na začátku řádku
     patří i Claudeovu vstupnímu poli. */
  const CONFIRM = /Enter to confirm/i;
  const MARKED = /^[\s│|]*[❯>]\s+\S/;
  const CHOICE = /^[\s│|]*(❯|>)?\s*(\S.*?)[\s│|]*$/;
  // Delší seznam patří šipkám: dvacet tlačítek přes celou šířku už nikdo nečte.
  const PLAIN_MAX = 8;

  // Přípony, u kterých má smysl kreslit náhled přílohy.
  const IMG_EXT = /\.(png|jpe?g|gif|webp|bmp|avif|svg)$/i;

  // Jak dlouho po doptání na schránku se další žádost bere jako tentýž stisk.
  // Ctrl+V dorazí dvakrát (keydown i paste) během jednotek milisekund.
  const PASTE_GAP = 400;   // ms

  // Trvalá volba v kartě je po objevení chvíli zamčená a odpověď se neposílá
  // dvakrát (ms): mezera mezi dvěma kliknutími a nejdelší zámek jednoho dotazu.
  const PERM_ARM_MS = 450;
  const SENT_GAP = 700;
  const ANSWER_LOCK_MS = 4000;

  // Kolik řádků nahoru se dialog čte. Nabídka oprávnění má i s rámečkem
  // a textem příkazu k dvaceti řádkům.
  const DIALOG_ROWS = 24;

  // Jména, kterým rozumí `/model <jméno>` — přepne rovnou, bez procházení
  // výběru v terminálu. Bez „výchozího": vybraný model má být vidět jménem,
  // a „výchozí" je jenom jiné jméno pro jeden z nich.
  // Verze v názvech jsou jen výchozí popisek: alias bere vždycky nejnovější
  // model rodiny a jakmile ho Claude Code ohlásí (hlavička, odpověď), nabídka
  // ukáže jeho skutečné číslo (modelMenu → labelFor).
  const MODELS = [
    ['Opus 5.5', 'opus'],
    ['Sonnet 5.5', 'sonnet'],
    ['Haiku 4.5', 'haiku'],
    ['Fable 5.1', 'fable'],
  ];

  /* Režimy, jak je Claude Code hlásí pod vstupním polem. Přepíná se jedině
     Shift+Tab, které cykluje dokola — proto se sem tiskne i pořadí: kliknutí
     na režim znamená „mačkej Shift+Tab, dokud dole nesvítí tenhle".

     Bypass je v cyklu, jen když session nastartovala s možností bypassu
     (hub Claude pouští s --allow-dangerously-skip-permissions, viz agents.py
     bypass_arg) nebo rovnou v něm. Naměřené pořadí v Claude Code 2.1.272:
     normální → auto-accept → plán → bypass → auto → normální. */
  /* Čtvrtý sloupec je popisek pro jednoduchý režim (bez HUB_ADVANCED):
     tam se neříká, jak se režim jmenuje v Claude Code, ale co znamená. */
  const MODES = [
    ['normal', 'Normální', /manual mode on/i, 'Ptát se před změnami'],
    ['accept', 'Auto-accept', /(auto-)?accept edits on/i, 'Upravovat samo'],
    ['plan', 'Plán', /plan mode on/i, 'Nejdřív plán'],
    ['auto', 'Auto', /auto mode on/i, 'Dělat samo'],
    ['bypass', 'Bypass', /bypass permissions on/i, 'Bez ptaní'],
  ];

  // Jednoduchý režim (výchozí) × Pro pokročilé — hub.js nastavuje
  // window.HUB_ADVANCED. Čte se pokaždé znovu, přepnout se dá bez reloadu.
  const advanced = () => !!global.HUB_ADVANCED;

  // Kolikrát se zkusí Shift+Tab, než to vzdáme. Cyklus má nejvýš pět kroků,
  // šestý je pojistka proti tomu, že klávesu nikdo nečte.
  const MODE_TRIES = 6;
  const MODE_STEP = 220;   // ms — než Claude Code překreslí nápovědu

  function modeOf(lines) {
    const text = (lines || []).join('\n');
    for (const [key, , re] of MODES) if (re && re.test(text)) return key;
    return 'normal';
  }

  function modeLabel(key) {
    const found = MODES.find(m => m[0] === key) || MODES[0];
    return advanced() ? found[1] : found[3];
  }

  /* Číslované volby z dialogu, odshora dolů. Bere se jen souvislá řada od
     jedničky — čísla ve výpisu (seznam kroků, řádky souboru) se tak do
     tlačítek nedostanou. */
  function scanOptions(lines) {
    const out = [];
    for (let i = 0; i < lines.length; i++) {
      const m = OPTION.exec(lines[i]);
      if (!m) continue;
      const num = Number(m[2]);
      if (num === 1) out.length = 0;                    // začíná nová nabídka
      else if (!out.length || num !== Number(out[out.length - 1].key) + 1) continue;
      out.push({
        key: m[2],
        label: m[3].replace(/\s+/g, ' ').trim(),
        sel: !!m[1],
        row: i,
      });
    }
    return out;
  }

  /* Volby dialogu, který nečísluje. Bere se blok řádků slepený kolem toho se
     šipkou; hranicí je prázdný řádek, takže text nad nabídkou ani nápověda pod
     ní se do tlačítek nedostanou. */
  function scanPlain(lines) {
    if (!lines.some(l => CONFIRM.test(l))) return [];
    let at = -1;
    for (let i = lines.length - 1; i >= 0; i--) {
      if (MARKED.test(lines[i]) && !CONFIRM.test(lines[i])) { at = i; break; }
    }
    if (at < 0) return [];
    let from = at, to = at;
    while (from > 0 && lines[from - 1].trim()) from--;
    while (to < lines.length - 1 && lines[to + 1].trim()) to++;
    const out = [];
    for (let i = from; i <= to; i++) {
      const m = CHOICE.exec(lines[i]);
      // Cokoli, co nevypadá jako volba, znamená, že to nabídka není.
      if (!m || CONFIRM.test(lines[i])) return [];
      out.push({label: m[2].replace(/\s+/g, ' ').trim().slice(0, 60),
                sel: !!m[1], at: i - from, row: i});
    }
    return out.length > 1 && out.length <= PLAIN_MAX && out.some(o => o.sel)
      ? out : [];
  }

  /* ── dotaz přenesený do karty ─────────────────────────────────────────────
     Claude Code kreslí dotaz do terminálu: rámeček z čar, šipka u vybrané
     volby, nápověda pod tím. Přečíst se to dá, odpovědět myší ne — a hlavně
     to vypadá jako výpis, ne jako otázka. Rozebere se proto na text, volby
     a nápovědu, ze kterých se poskládá karta. */

  /* Čáry rámečku a vodorovná pravítka. V terminálu drží dotaz pohromadě,
     v kartě by z nich bylo jen rozsypané písmo navíc. Bere se celý blok
     Unicode s kreslicími znaky (2500–257F) i s poloviční výplní (2580–259F) —
     Claude Code kreslí oddělovač panelu z `▔`, a ten vyjmenovaný seznam čar
     minul: stal se z něj nadpis karty a odsazení pod ním pak vyšlo jako
     ukázka kódu. */
  const RULE = /^[\s|\u2500-\u259F]+$/;
  // Jen vnější rámeček: ten vnořený (příkaz, diff) patří do textu dotazu.
  const BOX_TOP = /^\s*[╭┌╔]/;

  /* Řádek zbavený svislého okraje rámečku — i vnořeného, protože příkaz nebo
     diff uvnitř dotazu mívá vlastní. Odsazení uvnitř zůstává: podle něj se
     v kartě pozná věta od ukázky. */
  function unbox(line) {
    let s = line.replace(/[\s│┃|]+$/, '');
    for (let i = 0; i < 2; i++) s = s.replace(/^\s*[│┃|] ?/, '');
    return s;
  }

  // Kolik řádků nad volbami se ještě počítá za text dotazu.
  const BODY_MAX = 30;

  /* Text dotazu: všechno nad volbami. Dotaz v rámečku se čte po jeho horní
     hranu; ten bez rámečku (důvěra ke složce hned po startu) nahoru tak
     dlouho, dokud text drží pohromadě — dva prázdné řádky za sebou znamenají,
     že výš už je obyčejný výpis, ne otázka. */
  function dialogBody(lines, upto) {
    let from = 0, blanks = 0;
    for (let i = upto - 1; i >= 0; i--) {
      if (!lines[i].trim()) {
        if (++blanks >= 2) { from = i + 1; break; }
        continue;
      }
      blanks = 0;
      if (BOX_TOP.test(lines[i])) { from = i + 1; break; }
      if (upto - i >= BODY_MAX) { from = i; break; }
    }
    const out = [];
    for (let i = from; i < upto; i++) {
      const s = unbox(lines[i]);
      // Čára rámečku se nekreslí, ale odděluje — bez ní by se nadpis slepil
      // s cestou k souboru pod ním do jednoho odstavce.
      if (!s.trim() || RULE.test(s)) { if (out.length) out.push(''); continue; }
      out.push(s);
    }
    while (out.length && !out[out.length - 1].trim()) out.pop();
    /* Odsazení se měří proti nejlevějšímu řádku, ne proti nule. Rámeček bývá
       odsazený celý (Claude Code do něj sype „│  text") a bez tohohle kroku
       by věty vypadaly jako ukázka kódu — naměřeno na vlastním dotazu, kde
       tak skončil úplně celý text. */
    let base = Infinity;
    for (const line of out) {
      if (line.trim()) base = Math.min(base, line.length - line.trimStart().length);
    }
    return base > 0 && base < Infinity ? out.map(l => l.slice(base)) : out;
  }

  /* Souvislé kusy textu. Odsazený řádek je ukázka (příkaz, cesta, diff) a
     patří do neproporcionálního bloku, zbytek je věta.

     Věty se přitom slepují dvakrát. Řádky uvnitř odstavce do jedné věty:
     terminál je zalomil na svoji šířku a v kartě se má text lámat sám, jinak
     zůstane po zalomení díra uprostřed řádku. A odstavce do jednoho bloku:
     každý zvlášť by z dotazu udělal sloupec oddělených kousků místo textu,
     který se dá přečíst v jednom tahu. */
  function bodyBlocks(body) {
    const out = [];
    let para = '';

    function push(code, line) {
      const last = out[out.length - 1];
      if (last && last.code === code) last.lines.push(line);
      else out.push({code, lines: [line]});
    }

    function endPara() {
      if (!para) return;
      push(false, para);
      para = '';
    }

    for (const line of body) {
      if (!line.trim()) { endPara(); continue; }
      if (/^\s/.test(line)) { endPara(); push(true, line); continue; }
      para = para ? para + ' ' + line.trim() : line.trim();
    }
    endPara();
    return out;
  }

  /* Nápověda pod volbami. Claude Code tam píše „Enter to confirm · Esc to
     cancel" — v kartě, která má vlastní tlačítko Zrušit, by to bylo totéž
     dvakrát a ještě anglicky. Píše se proto, co v kartě opravdu platí. */
  const HINT_NUM = 'Odpovědět jde i číslem · Enter potvrdí vybranou volbu';
  const HINT_PLAIN = 'Enter potvrdí vybranou volbu';
  const HINT_PERM = 'Odpovědět jde i číslem · Čárkovaná volba platí i příště, ne jen teď';

  /* Dotaz s víc otázkami nebo se zaškrtávátky (AskUserQuestion). Naměřeno
     v Claude Code 2.1.283:

         ←  ☒ Barvy  ☐ Jidlo  ✔ Submit  →
         Ktere barvy mas rad?
         ❯ 1. [✔] Cervena
                  Vibrantni a energicka barva
           2. [ ] Modra
           …
           4. [ ] Type something
              Next
         ──────────────
           5. Chat about this
         Enter to select · Tab/Arrow keys to navigate · Esc to cancel

     Číslo volbu PŘEPNE (kurzor ❯ se nehne), Enter a mezerník přepnou tu pod
     kurzorem, Tab (i →) jde na další otázku a nakonec na „Submit". Šipka ❯
     tu tedy neznamená „tohle je vybrané" — vybrané je, co má [✔]. A číslo
     „Chat about this" dotaz rovnou zahodí. */
  const TABS = /^[\s│|]*←\s.*→[\s│|]*$/;
  const TAB_ITEM = /([☐☒✔✓■□])\s+(.+?)(?=\s{2,}[☐☒✔✓■□]|\s*→|$)/g;
  const CHECKBOX = /^\[([^\]]?)\]\s*/;
  const NEXT_LINE = /^(Next|Submit)$/;
  const HINT_MULTI = 'Zaškrtni všechno, co platí · pak Další';
  const HINT_TABS = 'Klikni na odpověď — pak přijde další otázka';
  // Popisky, které Claude Code do dotazu dává sám, anglicky.
  const OWN_LABELS = [
    [/^Type something\.?$/i, 'Napsat vlastní odpověď…'],
    [/^Chat about this$/i, 'Radši to probrat v chatu (dotaz zavře)'],
    [/^Submit answers$/i, 'Odeslat odpovědi'],
    [/^Cancel$/i, 'Zrušit'],
  ];
  function ownLabel(label) {
    for (const [re, cz] of OWN_LABELS) if (re.test(label)) return cz;
    return label;
  }

  /* Trvalé povolení v dialogu oprávnění. Claude Code vedle jednorázového
     „Yes" nabízí „Yes, and don't ask again for <příkaz> commands in <složka>"
     (do nastavení) nebo „Yes, allow all edits during this session (shift+tab)"
     (do konce relace). V kartě mají vypadat jinak než jednorázové Ano, aby se
     na ně nešlo trefit omylem, a česky říct, co se tím povolí. Vrací null,
     když volba trvalá není. Nic nezapisuje — trvalost řeší Claude Code, karta
     jen klikne volbu. */
  const PERM_ALWAYS = /don['’]t ask again|always allow|during this session|for this session|rest of (this|the) session/i;
  const PERM_SESSION = /during this session|for this session|rest of (this|the) session/i;
  const PERM_FOLDER = /^(.*?)\s+in\s+((?:\/|~|\.{1,2}[\/\\]|[A-Za-z]:[\\\/]).*)$/;
  function permInfo(full) {
    const raw = String(full || '').replace(/\s+/g, ' ').trim();
    if (!PERM_ALWAYS.test(raw)) return null;
    // Klávesová nápověda na konci („(shift+tab)") k popisu nepatří.
    const text = raw.replace(/\s*\((?:shift\+tab|[^()]*\btab)\)\s*$/i, '');
    const session = PERM_SESSION.test(text);
    let what = '', folder = '';
    const m = /don['’]t ask again for:?\s*(.+)$/i.exec(text);
    if (m) {
      what = m[1];
      const f = PERM_FOLDER.exec(what);
      if (f) { what = f[1]; folder = f[2]; }
      what = what.replace(/\s+commands?$/i, '').trim();
    }
    const where = folder ? ' ve složce ' + folder : '';
    let detail;
    if (/allow all edits/i.test(text)) {
      detail = 'Povolí všechny úpravy souborů do konce této relace' + where + '.';
    } else if (what) {
      detail = 'Povolí „' + what + '"' + where +
        (session ? ' do konce této relace.' : ' napořád — zapíše se do nastavení Claude Code.');
    } else {
      detail = (session ? 'Povolí to do konce této relace.' : 'Povolí to napořád.') +
        ' Přesné znění volby je níž.';
    }
    return {
      label: session ? 'Ano, do konce relace' : 'Ano a příště se neptat',
      badge: session ? 'Do konce relace' : 'Napořád',
      detail, raw,
    };
  }

  /* Záložky otázek z řádku „←  ☒ Barvy  ☐ Jidlo  ✔ Submit  →". Submit je
     vždycky s fajfkou, tak se vynechává — patří mu tlačítko Další. */
  function scanTabs(line) {
    const out = [];
    const inner = line.replace(/^[\s│|]*←\s*/, '').replace(/\s*→[\s│|]*$/, '');
    for (const m of inner.matchAll(TAB_ITEM)) {
      const label = m[2].trim();
      if (/^submit$/i.test(label)) continue;
      out.push({label, done: m[1] !== '☐' && m[1] !== '□'});
    }
    return out;
  }

  // Trvalé povolení u každé z voleb (null = jednorázová), i když se znění zalomilo.
  function permsOf(lines, opts) {
    return opts.map((o, i) => {
      const end = i + 1 < opts.length ? opts[i + 1].row : lines.length;
      return permInfo(o.label + ' ' + optionDesc(lines, o.row, end));
    });
  }

  /* Popis pod volbou (odsazené řádky mezi ní a další volbou). Bez něj by
     v kartě zbyla holá slova a nebylo by poznat, čím se volby liší. */
  function optionDesc(lines, from, to) {
    const out = [];
    for (let i = from + 1; i < to && i < lines.length; i++) {
      const raw = unbox(lines[i]);
      const s = raw.replace(/^\s*[❯>]?\s*/, '').trim();
      // Popis je vždycky odsazený — nápověda pod volbami („Enter to select")
      // začíná u kraje a do popisu nepatří.
      if (!s || !/^\s{3}/.test(raw) || RULE.test(s) || NEXT_LINE.test(s)) break;
      out.push(s);
    }
    return out.join(' ').slice(0, 200);
  }

  // Vestavěné příkazy Claude Code, které se nedají vyčíst ze složky skillů.
  // Ostatní agenti si svoje nesou v katalogu (hub/agents.py).
  const BUILTIN = ['/clear', '/compact', '/context', '/model', '/status',
                   '/resume', '/cost', '/help'];

  /* Co bublina o agentovi ví. Vše, co je Claude-specifické — dialogy, režimy
     přes Shift+Tab, měření vstupního pole — platí jen pro profil `full`.
     U ostatních se nic nedomýšlí: bublina je vidět, text a Enter dojdou do
     TUI, a terminál se o její výšku zkrátí, aby se nic nepřekrylo.

     Profily se doladí, až bude na čem měřit; hádat cizí TUI dopředu by
     znamenalo odpovídat naslepo na dialogy, kterým nerozumíme. */
  function profileOf(agent) {
    const full = !agent || agent.composer === 'full';
    return {
      full,
      id: (agent && agent.id) || 'claude',
      label: (agent && agent.label) || 'Claude',
      models: (agent && agent.models) || (full ? MODELS : []),
      slash: (agent && agent.slash) || (full ? BUILTIN : []),
      skills: agent ? !!agent.skills : true,
      modelCmd: (agent && agent.model_cmd) || '',
      bypass: !!(agent && agent.bypass),
    };
  }

  function el(tag, cls, text) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text != null) node.textContent = text;
    return node;
  }

  function icon(id) {
    return `<svg class="ico"><use href="#${id}"/></svg>`;
  }

  /* Text z posledních řádků toho, co je vidět. Ne z konce bufferu: když člověk
     odroluje nahoru, zajímá nás, co má před očima on, ne kde stojí Claude.

     `from` je číslo prvního vráceného řádku v terminálu — podle něj se pozná,
     kde Claudeovo vstupní pole začíná. */
  /* Spodek výpisu — počítáno od posledního neprázdného řádku, ne od fyzického
     spodku okna.

     Dokud je konverzace krátká, vstupní pole agenta na spodek terminálu
     nedosáhne a pod ním zůstává prázdno. Kdyby se četlo prostě posledních N
     řádků, byly by prázdné: klidný prompt by se nenašel, bublina by se
     neotevřela a zbyl by po ní jen proužek. Přesně tohle řeší o kus níž
     dialogLines pro dialogy — pro prompt to chybělo.

     `from` je index prvního vráceného řádku v okně; ownRows() z něj počítá,
     odkud dolů patří obrazovka vstupnímu poli, takže musí zůstat sedět. */
  function visibleBottom(term, count) {
    const buf = term.buffer.active;
    const text = (i) => {
      const row = buf.getLine(buf.viewportY + i);
      return row ? row.translateToString(true) : '';
    };
    let last = term.rows - 1;
    while (last >= 0 && !text(last).trim()) last--;
    if (last < 0) {
      const empty = [];
      empty.from = 0;
      return empty;
    }
    const from = Math.max(0, last - count + 1);
    const lines = [];
    for (let i = from; i <= last; i++) lines.push(text(i));
    lines.from = from;
    return lines;
  }

  /* Spodek výpisu pro čtení dialogu — prázdné řádky pod ním se přeskakují.
     Dotaz na důvěru ke složce přijde hned po startu, kdy je obrazovka ještě
     prázdná: nabídka stojí nahoře a pod ní zbývá zbytek okna. Kdyby se četlo
     prostě N posledních řádků terminálu, nespadla by do nich. */
  function dialogLines(term, count) {
    const buf = term.buffer.active;
    const text = (i) => {
      const row = buf.getLine(buf.viewportY + i);
      return row ? row.translateToString(true) : '';
    };
    let last = term.rows - 1;
    while (last >= 0 && !text(last).trim()) last--;
    if (last < 0) return [];
    const lines = [];
    for (let i = Math.max(0, last - count + 1); i <= last; i++) lines.push(text(i));
    return lines;
  }

  /* Kam až nahoru sahá bublina. Hlášky (toast) se podle toho posadí nad ni —
     dřív ležely přes ni a schovaly zrovna to pole, do kterého se píše.
     Měří se bublina aktivního tabu; ostatní panely jsou display:none, takže
     mají nulovou výšku a na pořadí volání nezáleží. */
  function syncHeight() {
    const on = document.querySelector('.pane.active .composer');
    document.documentElement.style.setProperty(
      '--composer-h', (on ? on.offsetHeight : 0) + 'px');
  }


  /* Řádek kláves. Na měkké klávesnici není Esc, Tab ani šipky kde vzít, a bez
     nich se agent ani shell neovládají: Shift+Tab přepíná režim oprávnění,
     šipkami se vybírá v seznamech, Ctrl+C ukončí běžící příkaz, Tab doplňuje
     cesty. Na počítači je řádek skrytý přes CSS (`.is-touch`). */
  const KEYS = [
    ['Esc', '\x1b', 'Zavřít dialog nebo přerušit, co Claude dělá'],
    ['Tab', '\t', 'Doplnit'],
    ['\u21e7Tab', '\x1b[Z', 'Přepnout režim oprávnění'],
    ['^C', '\x03', 'Ukončit běžící příkaz'],
    ['\u2191', '\x1b[A', 'Nahoru'],
    ['\u2193', '\x1b[B', 'Dolů'],
    ['\u2190', '\x1b[D', 'Doleva'],
    ['\u2192', '\x1b[C', 'Doprava'],
    ['\u23ce', '\r', 'Potvrdit'],
  ];

  function buildKeys(toPty) {
    const row = el('div', 'composer-keys');
    for (const [label, code, title] of KEYS) {
      const b = el('button', 'composer-key', label);
      b.type = 'button';
      b.title = title;
      // pointerdown, ne click: klepnutí by nejdřív sebralo fokus poli a měkká
      // klávesnice by sjela dolů, což u šipek při výběru vypadá jako porucha.
      b.addEventListener('pointerdown', (ev) => {
        ev.preventDefault();
        toPty(code);
      });
      row.appendChild(b);
    }
    return row;
  }

  /* Taby bez bubliny (holý shell, deploy) řádek potřebují taky — spíš víc,
     protože v nich se doplňuje tabulátorem a přerušuje Ctrl+C. Dostanou ho
     samotný ve stejném obalu, takže sedí na stejném místě jako u bubliny;
     třída na panelu o jeho výšku zkrátí terminál, aby řádek nepřekryl prompt. */
  function installKeys(tab, send) {
    const wrap = el('div', 'composer keys-only');
    wrap.appendChild(buildKeys((data) => {
      if (tab.id) send({t: 'in', id: tab.id, d: data});
    }));
    tab.pane.appendChild(wrap);
    tab.pane.classList.add('keys');
    return {release() { wrap.remove(); tab.pane.classList.remove('keys'); }};
  }

  function install(tab, io) {
    const term = tab.term;
    const AG = profileOf(io.agent ? io.agent() : null);
    const root = el('div', 'composer');
    root.innerHTML = `
      <div class="composer-answer" hidden>
        <span class="composer-answer-q"></span>
        <div class="composer-answer-opts"></div>
      </div>
      <button class="composer-peek" title="Psát v bublině">
        ${icon('i-up')}<span>Psát v bublině</span>
      </button>
      <div class="composer-box">
        <div class="composer-atts" hidden></div>
        <textarea class="composer-input" rows="1" spellcheck="false"
                  autocomplete="off" autocorrect="on" autocapitalize="sentences" aria-autocomplete="none" data-form-type="other" data-1p-ignore data-lpignore="true"
                  placeholder="Napiš, s čím ti má Claude pomoct… (Enter odešle, Shift+Enter nový řádek)"></textarea>
        <div class="composer-bar">
          <!-- Nástroje jedou v jedné řadě a na úzké obrazovce se posouvají
               prstem. Odeslat a Esc zůstávají mimo posuv, pořád na očích. -->
          <div class="composer-tools">
          <button class="composer-chip" data-act="agent"
                  title="Čím tenhle tab jede. Přepnout se dá jen novým tabem — agent běží jako vlastní program.">
            <span class="composer-dot"></span><span class="val"></span> ▾</button>
          <button class="composer-chip" data-act="model"
                  title="Přepne model.">Model: <span class="val"></span> ▾</button>
          <button class="composer-chip" data-act="slash">/ příkazy</button>
          <button class="composer-chip" data-act="history"
                  title="Dřívější zadání. Co je napsané v poli, tím se seznam rovnou filtruje.">${icon('i-refresh')} Historie</button>
          <button class="composer-chip" data-act="file">${icon('i-image')} Příloha</button>
          <button class="composer-chip" data-act="mode"
                  title="Režim oprávnění (Shift+Tab) — normální / auto-accept / plán / auto / bypass">Režim: <span class="val"></span> ▾</button>
          </div>
          <button class="composer-chip ghost" data-act="esc" title="Přeruší, co Claude právě dělá (Esc)">Zastavit</button>
          <button class="composer-mic" title="Diktovat česky — klikni, mluv, klikni znovu" hidden>${icon('i-mic')}</button>
          <button class="composer-send" title="Odeslat (Enter)">${icon('i-up')}</button>
        </div>
      </div>
      <input type="file" multiple hidden>`;
    tab.pane.appendChild(root);

    /* Karta s dotazem leží přes terminál. Když se Claude Code ptá, nemá být
       na co koukat do výpisu: otázka i volby patří do okna, ne mezi čáry
       rámečku. Terminál si jde kdykoli vyvolat zpátky proužkem. */
    const askRoot = el('div', 'ask');
    askRoot.hidden = true;
    askRoot.innerHTML = `
      <div class="ask-card">
        <div class="ask-tabs" hidden></div>
        <div class="ask-title"></div>
        <div class="ask-body"></div>
        <div class="ask-opts"></div>
        <div class="ask-foot">
          <span class="ask-hint"></span>
          <span class="spacer"></span>
          <button class="ask-nav ghost" data-act="prev" hidden title="Předchozí otázka">← Zpět</button>
          <button class="ask-nav" data-act="next" hidden title="Další otázka (Tab)">Další →</button>
          <button class="ask-ghost" data-act="term" title="Schová kartu a ukáže dotaz tak, jak ho kreslí Claude Code">${icon('i-terminal')} Terminál</button>
          <button class="ask-ghost" data-act="esc" title="Zavře dotaz bez odpovědi (Esc)">Zrušit</button>
        </div>
      </div>
      <button class="ask-back" title="Zpátky ke kartě s dotazem">${icon('i-bulb')} Zpět k otázce</button>`;
    tab.pane.insertBefore(askRoot, root);

    const input = root.querySelector('.composer-input');
    // Diktování česky (hlas.js) — tlačítko se ukáže, jen když je hlas nainstalovaný.
    if (global.HubHlas) global.HubHlas.mic(root.querySelector('.composer-mic'), input, {notice: io.notice});
    const attBox = root.querySelector('.composer-atts');
    const picker = root.querySelector('input[type=file]');
    const modelBtn = root.querySelector('[data-act=model]');
    const modelChip = modelBtn.querySelector('.val');
    const modeBtn = root.querySelector('[data-act=mode]');
    const modeChip = modeBtn.querySelector('.val');
    const agentBtn = root.querySelector('[data-act=agent]');
    const slashBtn = root.querySelector('[data-act=slash]');
    const askTermBtn = askRoot.querySelector('[data-act=term]');
    // Historie zadání. Ukládá se po projektech, ať přežije zavření okna —
    // jinak by pro ni člověk musel do Claudeova vlastního hledání v terminálu.
    // Po agentech zvlášť: co se psalo Claudeovi, nemusí dávat smysl v aiderovi.
    const HIST_KEY = 'hub.history:' + AG.id + ':' + (tab.path || 'home');
    const HIST_MAX = 200;
    const history = loadHistory();

    function loadHistory() {
      try {
        const raw = JSON.parse(localStorage.getItem(HIST_KEY) || '[]');
        return Array.isArray(raw) ? raw.filter(t => typeof t === 'string') : [];
      } catch (err) {
        return [];
      }
    }

    function saveHistory() {
      try {
        localStorage.setItem(HIST_KEY, JSON.stringify(history.slice(-HIST_MAX)));
      } catch (err) { /* plná nebo zakázaná úložiště nejsou důvod spadnout */ }
    }

    let histAt = -1;         // -1 = píše se nový text, ne historie
    let draft = '';
    let hiddenByUser = false;
    let shown = false;
    /* Čtení (cteni.js): terminál je pod ním schovaný, takže bublina tam nic
       nezakrývá a je jediné místo, kam psát — ukazuje se pořád. Na to, jestli
       Claude Code opravdu čeká na zadání, se pak nedá koukat přes `shown`;
       drží se to zvlášť. `everIdle` = Claude Code už jednou naběhl, zpráva
       napsaná dřív počká v `cekaZprava`. */
    let idleNow = false;
    let everIdle = false;
    let cekaZprava = '';
    let odchazi = false;     // zpráva z fronty se právě odesílá
    let idleSince = 0;       // odkdy Claude Code bez přestávky čeká na zadání
    let cekaTimer = null;
    let codePrompt = false;  // Claude Code čeká na kód z přihlášení (LOGIN_CODE)
    /* Terminál se drží, dokud se Claude Code nevrátí ke klidnému promptu.
       Na telefonu jinak tab skákal mezi terminálem, bublinou a prázdným
       čtením: po otevření klávesnice se terminál zmenší, přihlašovací odkaz
       odroluje z obrazovky a TERM_NEEDED ho přestane vidět. */
    let termHold = false;
    // Karta s dotazem uhne až po chvíli bez dotazu — mezi dvěma dialogy
    // (motiv → způsob přihlášení) je na okamžik prázdno a čtení by problesklo.
    const ASK_HOLD_MS = 700;
    let askingNow = false;
    let cekaHlaseno = false;   // co naposledy šlo do io.ceka (svítící tab)
    let askOffTimer = null;
    let normalPlaceholder = '';
    /* Bublina se ukáže, když je dole klidný prompt — jenže tím, že se ukáže,
       terminál zkrátí; agent překreslí TUI a prompt může zmizet. Pak by se
       schovala, terminál povyroste, prompt je zpátky, a takhle dokola. Po
       vlastním přepnutí se proto chvíli nepřepíná zpátky; co si vyžádá člověk
       (proužek, Esc) jde okamžitě. */
    const DWELL_MS = 450;
    let flippedAt = 0;
    let lastInsert = 0;      // kdy naposledy do pole přistála cesta k souboru
    // Klíč pro `/model <jméno>`. Ze settings.json (co si Claude Code uložil),
    // a když tam nic není, aspoň to, co se naposledy vybralo tady.
    // Model tabu: co se s ním spustilo, jinak poslední volba u tohohle agenta.
    // U Claudea navíc to, co má zapsané v settings.json.
    const MODEL_KEY = 'hub.model:' + AG.id;
    let model = tab.model ||
      (AG.full && io.model ? io.model() : '') ||
      localStorage.getItem(MODEL_KEY) || '';
    let mode = 'normal';     // co dole hlásí Claude Code
    let seenBypass = false;  // bypass je v cyklu jen u takhle spuštěné session
    let switching = false;   // běží přepínání režimu, nemačkat další
    // Model, kterým Claude v tabu naposledy doopravdy odpověděl (z přepisu na
    // serveru). Má přednost před volbou — „výchozí" neřekne, co běží.
    let actual = '';
    let actualAt = '';       // čas té odpovědi; po přepnutí se starší nebere
    let modelAsked = 0;
    let modelTimer = null;
    // Model z hlavičky Claude Code („Opus 5 (1M context) · API Usage Billing").
    // Je tam od startu, takže chip nemusí čekat na první odpověď.
    let banner = '';
    let picked = false;      // model se v tomhle tabu přepínal z nabídky

    /* ── automatická volba modelu a effortu ─────────────────────────────────
       Ke každé zprávě se odhadne, kolik je to práce, a podle toho se před ní
       pošle `/model` a `/effort` — na „díky" nemusí jet Opus na plný výkon,
       na návrh systému zas nestačí Sonnet na nízký. Rozhoduje:

       1. Haiku (hub/automodel.py) — přesnější, ale ~5 s. Ptá se proto už při
          psaní, když se člověk na chvíli zastaví; při odeslání je hotovo.
       2. Pravidla tady — okamžitě, když Haiku ještě neodpověděl.

       Stupně jdou po sobě; dolů se jde až o dva stupně, ať se u krátkého
       „a ještě tohle" uprostřed velké práce model nepřehazuje (každé
       přepnutí zahodí cache konverzace). Nahoru hned. Ruční volba modelu
       nebo effortu automatiku vypne, v nabídce se dá zase zapnout. */
    const STUPNE = [['haiku', 'low'], ['sonnet', 'low'], ['sonnet', 'medium'],
                    ['opus', 'high'], ['opus', 'xhigh']];
    const EFFORTS = ['low', 'medium', 'high', 'xhigh', 'max'];
    const AUTO_KEY = 'hub.auto:' + AG.id;
    let auto = AG.full && localStorage.getItem(AUTO_KEY) !== '0';
    let effort = '';         // effort, který jsme naposledy poslali
    let stupen = -1;         // stupeň, na kterém tab jede (-1 = nevíme)
    let pracuje = false;     // Claude zrovna pracuje (sledujPraci)
    let spinner = false;     // na obrazovce se točí (i hooky, i po /model)
    let pracovalAt = 0;      // kdy naposledy bylo vidět, že pracuje
    const KLID_PO_PRACI_MS = 2500;   // mezi kroky práce spinner na chvilku zmizí
    const nazory = new Map();   // text zprávy → odpověď Haiku
    let nazorTimer = null;
    const autoPrikazy = new Set();   // co z fronty poslala automatika
    let predAuto = null;             // stav před posledním přepnutím (kdyby se zahodilo)

    function stupenPravidly(text) {
      const t = text.toLowerCase();
      const slov = t.split(/\s+/).filter(Boolean).length;
      const odrazek = (text.match(/^\s*(?:[-*•]|\d+[.)])\s/gm) || []).length;
      if (/\b(architektur|navrhni systém|refaktor|přepiš celý|audit|bezpečnost|migrac|celou apl|celý projekt|od nuly|nový projekt)/.test(t) ||
          slov > 180 || odrazek >= 5) return 4;
      if (/\b(debug|nefunguje|nejde|padá|spadl|chyba|error|traceback|proč|implementuj|postav|vytvoř|naprogramuj|přidej funkci|optimaliz|napoj|integrac|produkc|ostr)/.test(t) ||
          slov > 60 || odrazek >= 2) return 3;
      if (/\b(přidej|uprav|změň|oprav|udělej|napiš|předělej|css|styl|text|e-?mail|stránk)/.test(t) || slov > 20) return 2;
      if (/^(díky|dík|děkuju|ok|jo|ano|ne|super|dobře|hotovo|čau|ahoj)\b[\s!.]*$/.test(t)) return 0;
      return 1;
    }

    function stupenZ(nazor) {
      if (!nazor || !nazor.model) return -1;
      const i = STUPNE.findIndex(([m, e]) => m === nazor.model && e === nazor.effort);
      if (i >= 0) return i;
      if (nazor.model === 'haiku') return 0;
      if (nazor.model === 'sonnet') return nazor.effort === 'low' ? 1 : 2;
      return nazor.effort === 'xhigh' || nazor.effort === 'max' ? 4 : 3;
    }

    // Při psaní: po chvíli klidu se zeptat Haiku, ať je názor hotový dřív než Enter.
    function nazorPriPsani() {
      clearTimeout(nazorTimer);
      if (!auto || !io.autoModel) return;
      const text = input.value.trim();
      if (text.length < 25 || text.startsWith('/') || nazory.has(text)) return;
      nazorTimer = setTimeout(() => {
        if (input.value.trim() !== text || nazory.has(text)) return;
        nazory.set(text, null);                    // ptá se — neptat znovu
        io.autoModel(text).then((r) => {
          if (r && r.model) nazory.set(text, r); else nazory.delete(text);
        }).catch(() => nazory.delete(text));
        if (nazory.size > 50) nazory.delete(nazory.keys().next().value);
      }, 1200);
    }

    /* Smí automatika přepnout model? Jen když člověk píše novou zprávu do
       klidu: Claude nepracuje (ani mezi dvěma kroky, kdy spinner na chvilku
       zmizí), na obrazovce není dialog ani otázka (schválení plánu,
       oprávnění, AskUserQuestion) a nic dalšího nečeká ve frontě. Claude Code
       totiž `/model` poslaný během práce zařadí do fronty a provede hned po
       dalším kroku — zbytek rozdělané práce by pak jel na jiném modelu
       (naměřeno na serveru: přepnutí mezi dvěma nástroji bez zprávy člověka). */
    function klidProAuto(sFrontou) {
      if (pracuje || codePrompt || tab.ceka || tab.exited) return false;
      if (spinner && !hookBezi) return false;
      if (Date.now() - pracovalAt < KLID_PO_PRACI_MS) return false;
      if (dialogDole()) return false;
      if (!sFrontou && fronta.length) return false;
      return true;
    }

    /* Před odesláním: přepnout model a effort, když je potřeba — jen pro
       zprávu, kterou napsal člověk, a jen když Claude nepracuje. */
    function autoPredOdeslanim(text) {
      if (!auto || !AG.full || !text.trim() || text.trim().startsWith('/')) return;
      if (!klidProAuto()) return;
      // Tab se ještě rozjíždí: zpráva počká na prompt (submit → cekaZprava),
      // `/model` poslaný teď by se ztratil ve startu. Přepne se u další zprávy.
      if (tab.cteni && (!tab.id || !everIdle)) return;
      const slov = text.trim().split(/\s+/).length;
      // Krátká odpověď („ano", „pokračuj", „to první") navazuje na rozdělanou
      // práci — model se kvůli ní nemění, jinak by zbytek dojel na Haiku.
      if (slov <= 8 && (stupen >= 0 || tab.resume || (tab.cteni && tab.cteni.prazdny && !tab.cteni.prazdny()))) return;
      let cil = stupenZ(nazory.get(text.trim()));
      if (cil < 0) cil = stupenPravidly(text);
      if (stupen >= 0 && cil < stupen && stupen - cil < 2) cil = stupen;
      const [m, e] = STUPNE[cil];
      // Po vlastním přepnutí víme, na čem tab jede; jinak podle toho, co ukazuje
      // chip (hlavička ze startu může být stará, proto až jako druhá možnost).
      const ted = stupen >= 0 ? STUPNE[stupen][0]
        : (family(shownModel()) || model || '').toLowerCase();
      const jinyModel = ted !== m;
      const poslat = jinyModel || stupen < 0 || e !== effort;
      // Výchozí pro nové chaty (settings.json) se tím měnit nemá — hub ho vrátí.
      if (poslat && io.autoKeep) io.autoKeep().catch(() => {});
      if (poslat) predAuto = {model, actual, effort, stupen};
      if (jinyModel) {
        autoPrikazy.add('/model ' + m);
        doruc('/model ' + m);
        model = m;           // jen tenhle tab — io.model by to dal i novým
        actual = '';
      }
      // `/model` si nastaví svůj výchozí effort — proto po něm vždycky znovu.
      if (jinyModel || e !== effort) {
        autoPrikazy.add('/effort ' + e);
        doruc('/effort ' + e);
      }
      effort = e;
      stupen = cil;
      syncModel();
    }
    const bornAt = Date.now();

    /* ── odesílání ────────────────────────────────────────────────────────── */

    // Vrací, jestli bajty odešly na server. Bez spojení (telefon ztratil síť,
    // prostor se restartuje) neodejde nic — a zpráva z bubliny pak počká.
    function toPty(data) {
      return !!tab.id && io.send({t: 'in', id: tab.id, d: data}) !== false;
    }
    const online = () => !io.online || io.online();

    const keyRow = buildKeys(toPty);
    root.insertBefore(keyRow, root.querySelector('.composer-box'));

    /* Enter se posílá zvlášť a s odstupem: slepený s textem ho Claude Code
       přečte jako nový řádek, ne jako odeslání. (Stejný důvod jako u tlačítek
       rychlých akcí.) */
    /* Čtení ukáže odeslanou zprávu hned — dřív, než ji Claude Code zapíše
       do přepisu, a když zrovna pracuje, i s tím, že čeká ve frontě. Kód
       z přihlášení se neukazuje: je to jednorázové heslo. */
    function ohlas(body, opts, stav) {
      if (!io.odeslano || codePrompt) return;
      io.odeslano({text: opts.text !== undefined ? opts.text : body, fid: opts.fid || '',
                   images: (opts.atts || []).filter((p) => IMG_EXT.test(p))}, stav);
    }

    /* ── doručení zprávy do Claude Code ────────────────────────────────────
       Zpráva z bubliny nesmí cestou zmizet. Proto jde přes frontu, která:

       - čeká na spojení se serverem (telefon bez signálu, restart prostoru) —
         bez něj se nic nepošle a nic se nepovažuje za odeslané;
       - posílá zprávy po jedné, text a Enter zvlášť (slepený Enter bere
         Claude Code jako nový řádek);
       - ověří, že text ze vstupního pole zmizel. Když tam pořád stojí, Enter
         se ztratil (Claude Code si ho hned po startu nebo při překreslení
         spolkne) a pošle se jen Enter znovu — text by v poli byl dvakrát.
       Když spojení spadne mezi textem a Enterem, po návratu se pozná podle pole:
       text v něm stojí → dopošle se Enter, není tam → napíše se celý znovu. */
    const fronta = [];
    let pumpuje = false;
    let pryc = false;                // bublina zavřená s tabem
    const konec = () => tab.exited || pryc;
    const spi = (ms) => new Promise((res) => setTimeout(res, ms));
    const PO_PRIKAZU_MS = 8000;
    async function dokud(podminka) {
      while (!podminka()) await spi(250);
    }
    // Čekání na spojení vidí i čtení — jinak by zpráva po 15 s zčervenala.
    async function naSpojeni() {
      if (konec() || online()) return;
      if (io.odeslano) io.odeslano(null, 'spojeni');
      await dokud(() => konec() || online());
      if (io.odeslano && !konec()) io.odeslano(null, 'odesila');
    }

    // Delší vložený text Claude Code v poli nahradí štítkem „[Pasted text #1 …]".
    function vPoli(zacatek, vlozit) {
      const dole = visibleBottom(term, PROBE_ROWS);
      return dole.some((l) => PROMPT.test(l) &&
        (l.includes(zacatek) || (vlozit && /\[Pasted text/i.test(l))));
    }
    const dialogDole = () => visibleBottom(term, DIALOG_ROWS).some((l) => DIALOG.test(l) || CONFIRM.test(l));

    function doruc(body) {
      fronta.push(body);
      pumpuj();
    }

    async function pumpuj() {
      if (pumpuje) return;
      pumpuje = true;
      try {
        while (fronta.length && !konec()) {
          await naSpojeni();
          await dokud(() => konec() || (tab.id && online()));
          if (konec()) break;
          const poslano = fronta[0];
          /* Automatický /model nebo /effort čekal ve frontě a Claude se mezitím
             rozběhl (nebo vyskočil dialog): přepnutí se zahodí, zpráva jde dál
             na tom modelu, na kterém Claude právě jede. */
          if (autoPrikazy.has(poslano) && !klidProAuto(true)) {
            for (let i = fronta.length - 1; i >= 0; i--) {
              if (autoPrikazy.has(fronta[i])) { autoPrikazy.delete(fronta[i]); fronta.splice(i, 1); }
            }
            if (predAuto) ({model, actual, effort, stupen} = predAuto);
            predAuto = null;
            syncModel();
            continue;
          }
          await posliJednu(poslano);
          fronta.shift();
          // Přepnutí modelu se občas ptá (kontext, účtování) — potvrdí se samo.
          if (/^\/(model|effort)\b/i.test(poslano)) potvrdPrepnuti();
          // Automatický /model nebo /effort doběhl — vrácení výchozího odložit.
          if (autoPrikazy.delete(poslano) && io.autoKeep) io.autoKeep(true).catch(() => {});
          /* Po slash příkazu (/model, /effort) Claude Code chvíli zpracovává
             a co se mezitím napíše, zůstane stát v poli nebo se ztratí. Další
             kus fronty proto počká, až je zase klid (nejdéle pár vteřin). */
          if (fronta.length && /^\/[a-z]/i.test(poslano)) {
            const od = Date.now();
            let klidOd = 0;
            await spi(150);
            await dokud(() => {
              if (konec() || Date.now() - od > PO_PRIKAZU_MS) return true;
              const klid = idleNow && !spinner && Date.now() - (tab.lastOut || 0) > 300;
              if (!klid) { klidOd = 0; return false; }
              klidOd = klidOd || Date.now();
              return Date.now() - klidOd >= 350;      // klid musí chvíli vydržet
            });
          }
        }
      } finally {
        pumpuje = false;
        fronta.length = konec() ? 0 : fronta.length;
      }
    }

    /* Po /model a /effort Claude Code někdy zobrazí potvrzení („Switch to …",
       1M kontext, účtování). Hub ho odklepne sám: číslovaná nabídka se
       potvrdí první kladnou volbou, nečíslovaná Enterem. Dívá se jen pár
       vteřin po příkazu a jen na dotaz, který se modelu týká — cizí dotaz
       (oprávnění nástroje) zůstává na člověku. */
    const PREPNUTI = /(model|effort|context window|1M context|extra usage|switch to)/i;
    const ZAPORNA = /^(no\b|cancel|keep|stay|don'?t|never)/i;
    async function potvrdPrepnuti() {
      if (!AG.full) return;
      let potvrzeno = 0;
      for (let i = 0; i < 16 && !konec() && potvrzeno < 2; i++) {
        await spi(350);
        const dole = visibleBottom(term, DIALOG_ROWS);
        if (!dole.some((l) => CONFIRM.test(l) || /^\s*[│|]?\s*❯?\s*1[.)]\s/.test(l))) {
          if (potvrzeno) return;                 // dotaz zmizel → hotovo
          continue;
        }
        const text = dole.join('\n');
        if (!PREPNUTI.test(text) || /Do you want to (proceed|make this edit|create|run)|Bash command|Allow /i.test(text)) return;
        const moznosti = scanOptions(dole);
        if (moznosti.length) {
          const vybrana = moznosti.find((o) => o.sel) || moznosti[0];
          const cil = ZAPORNA.test(vybrana.label) ? (moznosti.find((o) => !ZAPORNA.test(o.label)) || vybrana) : vybrana;
          toPty(cil.key);
        } else if (CONFIRM.test(text)) {
          toPty('\r');
        } else {
          return;
        }
        potvrzeno++;
        await spi(900);
      }
    }

    async function posliJednu(body) {
      // Víceřádkový text musí dorazit jako vložení, jinak by se každý řádek
      // odeslal zvlášť. Jednořádkový jde rovnou — bez uvozovacích sekvencí.
      // Taky text, který začíná otazníkem: v prázdném poli Claude Code
      // otazník neznamená znak, ale nápovědu se zkratkami — samotné „?"
      // by se tak nikdy neodeslalo. Vložení ho předá doslova.
      const vlozit = body.includes('\n') || body.startsWith('?');
      const data = vlozit ? '\x1b[200~' + body + '\x1b[201~' : body;
      const zacatek = body.split('\n')[0].trim().slice(0, 24);
      for (let pokus = 0; pokus < 20 && !konec(); pokus++) {
        if (!toPty(data)) {                       // text neodešel vůbec
          await naSpojeni();
          continue;
        }
        await spi(180);
        let enter = toPty('\r');
        if (!AG.full || !zacatek) return;           // cizí TUI neumíme přečíst
        /* /model a /effort Claude Code zpracuje hned — pole se vyprázdní za
           zlomek vteřiny. Stačí se ptát často, ne čekat 1,2 s jako u zprávy. */
        if (/^\/(model|effort)\b/i.test(body)) {
          const do_ = Date.now() + 4000;
          while (Date.now() < do_ && !konec()) {
            await spi(120);
            if (!vPoli(zacatek, vlozit) && enter) return;
          }
        }
        // Kontroly s rostoucím odstupem: jedna mohla trefit chvíli, kdy Claude
        // Code zrovna překresloval a prompt na obrazovce nebyl.
        for (const za of [1200, 1800, 2500, 4000, 6000, 9000]) {
          await spi(za);
          if (konec()) return;
          if (!online()) {
            await naSpojeni();
            await spi(800);                        // dohrání obrazovky po návratu
          }
          if (!vPoli(zacatek, vlozit)) {
            if (enter) return;                     // pole je prázdné → odešlo
            break;                                 // Enter ani text nedošly → znovu celé
          }
          if (dialogDole()) continue;              // nad polem visí dotaz — počkat
          enter = toPty('\r') || enter;
        }
        if (vPoli(zacatek, vlozit)) return;        // stojí v poli, Enter nebere — nech být
        if (enter) return;
      }
    }

    /* Znovu doručit zprávu, kterou Claude nepřevzal (volá čtení po restartu
       prostoru). Text, co už stojí v poli Claude Code, se jen odešle Enterem;
       co už čeká ve frontě, se nezdvojuje. */
    function znovu(text) {
      const body = String(text || '').replace(/\r/g, '');
      if (!body.trim() || konec() || !tab.id || !online()) return false;
      if (fronta.includes(body)) return true;
      const zacatek = body.split('\n')[0].trim().slice(0, 24);
      const vlozit = body.includes('\n') || body.startsWith('?');
      if (AG.full && zacatek && vPoli(zacatek, vlozit)) { toPty('\r'); return true; }
      doruc(body);
      return true;
    }

    function submit(text, opts = {}) {
      const body = text.replace(/\r/g, '');
      if (!body.trim()) return;
      if (io.pozastaveno && io.pozastaveno()) {
        if (io.notice) io.notice('Odesílání zpráv je pozastavené — povol ho pod zprávou, která nedošla.');
        return;
      }
      const ef = /^\/effort\s+(low|medium|high|xhigh|max)\b/i.exec(body.trim());
      if (ef) { effort = ef[1].toLowerCase(); syncModel(); }
      /* Tab právě vznikl a Claude Code se ještě rozjíždí. Co by se teď poslalo
         do terminálu, by se ztratilo v jeho startu — zpráva proto počká a
         odejde sama, jakmile se objeví prompt. */
      /* Zpráva Claudovi (ne příkaz, ne odpověď na dotaz) jde přes server: tam se
         uloží a doručí, až je Claude připravený — přežije restart prostoru
         i zavřené okno. Příkazy a odpovědi na dotazy jdou rovnou do terminálu. */
      const pres_server = !!(io.naServer && tab.id && tab.cteni && !opts.zeStartu && !codePrompt &&
                             !tab.ceka && !askingNow && !/^\s*[\/!]/.test(body) && !dialogDole());
      if (!pres_server && tab.cteni && (!tab.id || !everIdle)) {
        cekaZprava = cekaZprava ? cekaZprava + '\n' + body : body;
        ohlas(body, opts, 'start');
        if (io.notice && !io.odeslano) io.notice('Claude se ještě chystá… Zpráva odejde, jakmile bude připravený.');
        return;
      }
      if (pres_server) {
        const fid = 'z' + Date.now().toString(36) + Math.random().toString(36).slice(2, 6);
        io.naServer(body, fid).then((ok) => {
          if (ok) { ohlas(body, Object.assign({}, opts, {fid}), 'server'); return; }
          ohlas(body, opts, 'odesila');          // server zprávu nevzal — stará cesta
          doruc(body);
        });
      } else {
        if (opts.zeStartu) {
          if (io.odeslano) io.odeslano(null, 'odesila');
        } else {
          ohlas(body, opts, 'odesila');
        }
        doruc(body);
      }
      // Kód z přihlášení do historie nepatří — platí jednou a je to heslo.
      if (!codePrompt) {
        // Dvakrát po sobě to samé je v seznamu jen k horšímu.
        if (history[history.length - 1] !== body) history.push(body);
        if (history.length > HIST_MAX) history.splice(0, history.length - HIST_MAX);
        saveHistory();
      }
      histAt = -1;
      draft = '';
    }

    function send() {
      const files = atts.map(io.quote).join(' ');
      const text = files ? input.value.trim() : input.value;
      /* Cesty k přílohám až za text. Zpráva, která začíná lomítkem, je pro
         Claude Code příkaz („/home/…png“) — příkazy si během práce nebere,
         takže by čekala ve frontě až do konce celé úlohy (a restart prostoru
         by ji mezitím smazal). Bez textu ji uvede slovo, ať lomítkem nezačíná. */
      const body = files ? (text ? text + ' ' + files : 'Příloha: ' + files) : text;
      if (!body.trim()) return;
      autoPredOdeslanim(input.value);
      document.dispatchEvent(new CustomEvent('hub-odeslano', {detail: {tab: tab.id, text: input.value.trim()}}));
      submit(body, {text: input.value.trim(), atts: atts.slice()});
      input.value = '';
      atts.length = 0;
      renderAtts();
      autogrow();
      // Na telefonu po odeslání schovat klávesnici — jinak zakrývá půlku
      // obrazovky s odpovědí, na kterou se teď čeká. Na počítači zůstává
      // kurzor v poli, ať jde hned psát dál.
      if (document.body.classList.contains('is-touch')) input.blur();
      else input.focus();
    }

    function insert(text, {focus = true} = {}) {
      const at = input.selectionStart ?? input.value.length;
      const before = input.value.slice(0, at);
      const after = input.value.slice(input.selectionEnd ?? at);
      const glue = before && !/\s$/.test(before) ? ' ' : '';
      input.value = before + glue + text + after;
      const pos = (before + glue + text).length;
      input.setSelectionRange(pos, pos);
      autogrow();
      if (focus) input.focus();
    }

    // Odesílací tlačítko se rozsvítí, až když je co odeslat — text nebo příloha.
    function ready() {
      root.classList.toggle('ready', !!input.value.trim() || atts.length > 0);
    }

    function autogrow() {
      input.style.height = 'auto';
      // Strop je pět řádků: dál už by bublina ukrajovala z výpisu příliš.
      input.style.height = Math.min(input.scrollHeight, 5 * 20 + 12) + 'px';
      ready();
      // Vyšší bublina = o řádek kratší terminál, ať pod ní nic nezmizí.
      if (shown) { dirty = true; fitOver(); }
    }

    /* ── nabídky ──────────────────────────────────────────────────────────── */

    /* Nabídky se otevírají nad bublinu — pod ní je konec okna. Kotví se na
       horní hranu celé bubliny, ne na tlačítko, ať nepřekrývají text. */
    function anchor(ev) {
      const chip = ev.currentTarget.getBoundingClientRect();
      const bubble = root.querySelector('.composer-box').getBoundingClientRect();
      return [chip.left, bubble.top - 6];
    }

    /* Jméno modelu pro člověka. V settings.json nemusí být zrovna to krátké
       jméno, co bere `/model` — bývá tam i celé id (claude-opus-5), tak se
       hledá i podle kusu jména. */
    function modelName(key) {
      if (!key) return 'výchozí';
      const exact = AG.models.find(m => m[1] === key);
      if (exact) return exact[0];
      const near = AG.models.find(m => key.toLowerCase().includes(m[1]));
      return near ? near[0] : key;
    }

    /* Rodina modelu z popisku („Opus 5.5" → opus). Novější model téže rodiny
       je pořád ten, co se vybírá aliasem — v nabídce má být vidět jeho číslo,
       ne to, které bylo v hubu napsané při vydání. */
    const family = (text) => (String(text || '').match(/\b(opus|sonnet|haiku|fable)\b/i) || [])[1];
    function sameFamily(text, key) {
      const f = family(text);
      return f ? f.toLowerCase() === key : text === modelName(key);
    }
    function labelFor(label, key) {
      for (const seen of [actual, banner]) {
        if (seen && sameFamily(seen, key) && /\d/.test(seen)) return seen;
      }
      return label;
    }

    /* Co ukázat: čím Claude naposledy odpověděl; jinak volba z nabídky; jinak
       hlavička, se kterou nastartoval; a teprve pak settings.json. */
    function shownModel() {
      return actual || (picked ? modelName(model) : (banner || modelName(model)));
    }

    function syncModel() {
      // Claudík se převlékne podle úsilí (claudici.js) — i ten visící z tabu.
      if (tab.effort !== effort) {
        tab.effort = effort;
        if (tab.cteni && tab.cteni.hriste) tab.cteni.hriste.effort(effort);
        if (tab.visi) tab.visi.dataset.effort = effort || '';
      }
      modelChip.textContent = AG.models.length
        ? (auto ? 'Auto · ' + shownModel() + (effort ? ' · ' + effort : '') : shownModel() + (effort ? ' · ' + effort : ''))
        : (AG.modelCmd || '').replace('{model}', '').trim();
    }

    // Hlavička je v prvních řádcích — hledá se jen chvíli po startu tabu.
    const BANNER_RE = /\b(Opus|Sonnet|Haiku|Fable) (\d+(?:\.\d+)?)\b/;
    function readBanner() {
      if (banner || !AG.full || !AG.models.length || Date.now() - bornAt > 60000) return;
      const buf = term.buffer.active;
      for (let i = 0, n = Math.min(buf.length, 60); i < n; i++) {
        const row = buf.getLine(i);
        const line = row ? row.translateToString(true) : '';
        const m = line.match(BANNER_RE);
        if (m) {
          banner = m[1] + ' ' + m[2];
          // „Sonnet 5.5 with low effort" — s čím tab nastartoval. Bez toho by
          // automatika první zprávu vždycky začínala přepnutím /effort.
          const ef = line.match(/with (low|medium|high|xhigh|max) effort/i);
          if (ef && !effort) effort = ef[1].toLowerCase();
          syncModel();
          return;
        }
      }
    }

    /* Co doopravdy odpovídá, ví jen přepis konverzace. Ptá se na něj, když se
       bublina ukáže (Claude dopsal), nejvýš jednou za pět sekund. Odpověď
       starší než ta známá se nebere — po přepnutí modelu by chip vrátila. */
    function refreshModel() {
      if (!AG.full || !AG.models.length || !io.tabModel || !tab.id || modelTimer) return;
      const wait = Math.max(800, modelAsked + 5000 - Date.now());
      modelTimer = setTimeout(() => {
        modelTimer = null;
        modelAsked = Date.now();
        io.tabModel(tab.id).then((r) => {
          if (!r || !r.label || !r.at || r.at <= actualAt) return;
          actualAt = r.at;
          if (r.label !== actual) { actual = r.label; syncModel(); }
        }).catch(() => { /* bez odpovědi zůstane, co je */ });
      }, wait);
    }

    /* Přepnutí modelu má u každého agenta jinou cenu:

       - Claude Code umí `/model <jméno>` za běhu, takže se přepne rovnou.
       - Ollama, opencode a aider berou model jako argument při startu —
         přepnout se dá jedině novým tabem, a tak to menu i řekne.
       - Kdo si nabídku drží sám (Codex, Gemini), dostane svůj vlastní příkaz;
         vypisovat tady jména jejich modelů by znamenalo nabízet i ta, která
         u poskytovatele mezitím zmizela. */
    function modelMenu(ev) {
      const [x, y] = anchor(ev);
      if (!AG.models.length) {
        if (AG.modelCmd) submit(AG.modelCmd.replace('{model}', '').trim());
        return;
      }
      const now = shownModel();
      const vyber = (seznam) => seznam.map(([label, key]) => ({
        icon: 'i-star',
        label: AG.full ? (key.startsWith('claude-') ? label : labelFor(label, key)) : label + '  (nový tab)',
        on: !auto && (key.startsWith('claude-') ? now === label : sameFamily(now, key)),
        run: () => {
          auto = false;
          try { localStorage.setItem(AUTO_KEY, '0'); } catch (err) { /* soukromé okno */ }
          stupen = -1;
          if (AG.full) {
            submit('/model ' + key);
            model = key;
            picked = true;
            actual = '';           // do další odpovědi platí volba
            if (io.model) io.model(key);
            syncModel();
          } else if (io.openWith) {
            io.openWith(AG.id, key);
          }
          try { localStorage.setItem(MODEL_KEY, key); } catch (err) { /* soukromé okno */ }
        },
      }));
      const hlavni = AG.models.filter((m) => !m[2]);
      const starsi = AG.models.filter((m) => m[2] === 'old');
      const items = [];
      if (AG.full) {
        items.push({icon: 'i-bolt', label: 'Automaticky podle úkolu', on: auto, run: () => {
          auto = true;
          try { localStorage.setItem(AUTO_KEY, '1'); } catch (err) { /* soukromé okno */ }
          syncModel();
        }});
      }
      items.push(...vyber(hlavni));
      if (starsi.length) {
        items.push({icon: 'i-more', label: 'Starší modely…',
                    run: () => setTimeout(() => io.menu(x, y, vyber(starsi), {above: true}), 0)});
      }
      if (AG.full) {
        items.push({icon: 'i-gear', label: 'Effort' + (effort ? ' (' + effort + ')' : '') + '…',
                    run: () => setTimeout(() => io.menu(x, y, EFFORTS.map((e) => ({
                      icon: 'i-dot', label: e, on: !auto && e === effort,
                      run: () => {
                        auto = false;
                        try { localStorage.setItem(AUTO_KEY, '0'); } catch (err) { /* soukromé okno */ }
                        stupen = -1;
                        submit('/effort ' + e);
                        effort = e;
                        syncModel();
                      },
                    })), {above: true}), 0)});
      }
      io.menu(x, y, items, {above: true});
    }

    /* Agenta v běžícím tabu přepnout nejde — je to jiný program, ne přepínač.
       Nabídka proto otevírá nový tab a říká to nahlas. */
    function agentMenu(ev) {
      const [x, y] = anchor(ev);
      const list = (io.agents ? io.agents(true) : []);
      const items = list.map(a => ({
        icon: 'i-terminal',
        label: a.id === AG.id ? a.label : a.label + '  (nový tab)',
        on: a.id === AG.id,
        run: () => { if (a.id !== AG.id && io.openWith) io.openWith(a.id, ''); },
      }));
      if (items.length) io.menu(x, y, items, {above: true});
    }

    /* ── režim oprávnění ──────────────────────────────────────────────────── */

    /* Přepnout se dá jedině Shift+Tab, a to cykluje: na vybraný režim se tedy
       mačká tak dlouho, dokud ho Claude Code dole nenahlásí. Slepě poslat
       jedno Shift+Tab nestačilo — nápověda s režimem leží pod bublinou, takže
       po tlačítku nebylo nic vidět a působilo mrtvě. */
    function syncMode(lines) {
      const now = modeOf(lines);
      if (now === 'bypass') seenBypass = true;
      if (now === mode) return now;
      mode = now;
      modeChip.textContent = modeLabel(mode);
      modeBtn.classList.toggle('normal', mode === 'normal');
      return now;
    }

    function readMode() {
      return syncMode(visibleBottom(term, PROBE_ROWS));
    }

    async function setMode(target) {
      if (switching) return;
      switching = true;
      try {
        for (let i = 0; i < MODE_TRIES; i++) {
          if (readMode() === target) return;
          toPty('\x1b[Z');
          await new Promise(done => setTimeout(done, MODE_STEP));
        }
        if (readMode() !== target) {
          io.notice(target === 'bypass'
            ? 'Tenhle rozhovor režim bez ptaní neumí (začal ještě před aktualizací) — otevři nový rozhovor.'
            : advanced()
              ? 'Režim se přepnout nepodařilo — zkus Shift+Tab v terminálu.'
              : 'Režim se přepnout nepodařilo — zkus to za chvíli znovu.');
        }
      } finally {
        switching = false;
        input.focus();
      }
    }

    function modeMenu(ev) {
      const [x, y] = anchor(ev);
      readMode();
      syncLevel();
      const pro = advanced();
      // Bypass je jen pro pokročilé — v jednoduchém režimu se nenabízí vůbec.
      const items = MODES
        .filter(([key]) => key !== 'bypass' || (pro && (AG.bypass || seenBypass)))
        .map(([key, label, , simple]) => ({
          icon: key === 'plan' ? 'i-note' : 'i-dot',
          label: pro ? label : simple,
          on: key === mode,
          run: () => (key === 'bypass' ? chooseBypass() : setMode(key)),
        }));
      io.menu(x, y, items, {above: true});
    }

    /* Bypass jde v běžícím Claude Code zapnout, jen když se tab spustil
       s možností bypassu. Tu hub dává až po potvrzeném varování — bez něj se
       Claude Code při každém startu anglicky ptá a Enter ho ukončí. Po
       potvrzení se proto otevře nový tab, rovnou v bypassu. */
    async function chooseBypass() {
      if (tab.bypass || seenBypass) return setMode('bypass');
      const ok = await HubDialog.confirm(
        'Režim bez ptaní: Claude pak sám spouští programy a mění soubory — ' +
        'i takové, které můžou něco smazat nebo rozbít.\n\n' +
        'Zapne se v novém rozhovoru, tenhle zůstane, jak je. Pokračovat?', {title: 'Režim bez ptaní', ok: 'Zapnout', danger: true});
      if (!ok) return;
      try {
        if (io.acceptBypass) await io.acceptBypass();
      } catch (err) {
        console.warn('bypass:', err);
        io.notice('Režim bez ptaní se teď zapnout nepodařilo, zkus to za chvíli znovu.');
        return;
      }
      if (io.openWith) io.openWith(AG.id, model, {mode: 'bypass'});
    }

    function slashMenu(ev) {
      const [x, y] = anchor(ev);
      // Naše skilly z ~/.claude/skills čte jen Claude Code; jinému agentovi by
      // se `/save` poslalo jako holý text a nic by se nestalo.
      const own = AG.skills ? (io.skills() || []).map(s => '/' + s).sort() : [];
      // Vlastní skill může mít stejné jméno jako vestavěný příkaz (/status),
      // a dvakrát v nabídce by jen mátl.
      const items = [...new Set([...own, ...AG.slash])].map(cmd => ({
        icon: cmd === '/clear' || cmd === '/compact' ? 'i-refresh' : 'i-terminal',
        label: cmd,
        run: () => insert(cmd + ' '),
      }));
      io.menu(x, y, items, {above: true});
    }

    /* Dřívější zadání v nabídce. Claude Code má svoje hledání (Ctrl+R), jenže
       to je celoobrazovkový terminálový výběr — tady stačí kliknout. Místo
       filtračního políčka slouží to, co je zrovna napsané v poli. */
    function historyMenu(ev) {
      const [x, y] = anchor(ev);
      const needle = input.value.trim().toLowerCase();
      const seen = new Set();
      const items = [];
      for (let i = history.length - 1; i >= 0 && items.length < 40; i--) {
        const text = history[i];
        if (seen.has(text)) continue;
        seen.add(text);
        if (needle && !text.toLowerCase().includes(needle)) continue;
        items.push({
          icon: text.startsWith('/') ? 'i-terminal' : 'i-note',
          // Víceřádkové zadání by nabídku roztáhlo; v poli se pak ukáže celé.
          label: text.replace(/\s+/g, ' ').slice(0, 80),
          run: () => {
            input.value = text;
            autogrow();
            input.focus();
            input.setSelectionRange(text.length, text.length);
          },
        });
      }
      if (!items.length) {
        items.push({icon: 'i-bulb',
                    label: needle ? 'Nic takového tu není' : 'Zatím prázdno',
                    run: () => input.focus()});
      }
      io.menu(x, y, items, {above: true});
    }

    /* ── jednoduchý režim × pro pokročilé ─────────────────────────────────
       V jednoduchém režimu (výchozí) zůstává jen to, čemu rozumí každý:
       psaní, historie, příloha, režim srozumitelnými slovy a Zastavit.
       Agent, model, / příkazy, řádek terminálových kláves a tlačítko
       Terminál na kartě s dotazem jsou jen pro pokročilé. */
    let levelNow = null;
    function syncLevel() {
      const pro = advanced();
      if (pro === levelNow) return;
      levelNow = pro;
      // Pro pokročilé zůstává jako dřív — seznam agentů se může načíst
      // až po otevření tabu, takže se podle jeho délky neschovává.
      agentBtn.hidden = !pro;
      // Model, který se nedá vybrat ani poslat vlastním příkazem, nemá chip.
      modelBtn.hidden = !pro || (!AG.models.length && !AG.modelCmd);
      slashBtn.hidden = !pro;
      keyRow.hidden = !pro;
      askTermBtn.hidden = !pro;
      modeChip.textContent = modeLabel(mode);
      modeBtn.title = pro
        ? 'Režim oprávnění (Shift+Tab) — normální / auto-accept / plán / auto / bypass'
        : 'Jak moc se má Claude ptát, než něco udělá';
      // Jiná sada tlačítek = jiná výška bubliny, terminál se přeměří.
      dirty = true;
      syncHeight();
    }

    /* ── odpovídání na dialogy myší ───────────────────────────────────────── */

    /* Když se Claude Code na něco ptá, bublina uhne (jinak by dotaz překryla)
       a zbyl by jen terminál. Otázka se proto přenese do tlačítek: volby
       z rámečku, ano/ne i šipky u výběru ze seznamu. */
    const answer = root.querySelector('.composer-answer');
    const answerQ = root.querySelector('.composer-answer-q');
    const answerOpts = root.querySelector('.composer-answer-opts');
    const askCard = askRoot.querySelector('.ask-card');
    const askTitle = askRoot.querySelector('.ask-title');
    const askBody = askRoot.querySelector('.ask-body');
    const askOpts = askRoot.querySelector('.ask-opts');
    const askHint = askRoot.querySelector('.ask-hint');
    const askTabs = askRoot.querySelector('.ask-tabs');
    const askPrev = askRoot.querySelector('[data-act=prev]');
    const askNext = askRoot.querySelector('[data-act=next]');
    const askEsc = askRoot.querySelector('[data-act=esc]');
    let answerSig = '';
    let questionSig = '';      // která otázka je v kartě — bez stavu zaškrtnutí

    function press(data) {
      toPty(data);
      // Psaní (filtr v hledání, vlastní odpověď) má po kliknutí pokračovat
      // v terminálu, ne na tlačítku.
      term.focus();
    }

    /* Klávesa bez fokusu do terminálu. Zaškrtávání se kliká několikrát za
       sebou a fokus by na telefonu pokaždé vytáhl klávesnici: okno se
       zmenší, Claude Code překreslí, karta poskočí — a další klik se trefí
       vedle. Přesně tohle bylo to „lagování". */
    function tap(data) {
      toPty(data);
    }

    /* Nabídka bez čísel se ovládá jedině šipkami, takže kliknutí znamená
       „dojeď na tu volbu a potvrď". Klávesy jdou po jedné s odstupem: slepené
       v jedné dávce si je Claude Code přebere jako jediný stisk. */
    function pickPlain(steps) {
      const key = steps < 0 ? '\x1b[A' : '\x1b[B';
      let wait = 0;
      for (let i = Math.abs(steps); i > 0; i--, wait += 50) {
        setTimeout(() => toPty(key), wait);
      }
      setTimeout(() => toPty('\r'), wait + 120);
      term.focus();
    }

    /* Odpověď na dotaz se pošle nejvýš jednou. Dvojklik by druhou číslici
       poslal už do dalšího dotazu (nebo do vstupního pole), a tam může
       znamenat něco jiného. Tlačítka po kliknutí zešednou; odemknou se, až
       se karta změní nebo zmizí, a pro jistotu po ANSWER_LOCK_MS. */
    let sentAt = 0, sentSig = null;
    function answerOnce(key) {
      const now = Date.now();
      if (now - sentAt < SENT_GAP || (sentSig === answerSig && now - sentAt < ANSWER_LOCK_MS)) return;
      sentAt = now;
      sentSig = answerSig;
      const btns = [...askOpts.querySelectorAll('button')];
      btns.forEach((b) => { b.disabled = true; });
      setTimeout(() => btns.forEach((b) => { b.disabled = false; }), ANSWER_LOCK_MS);
      press(key);
    }

    function renderAnswer() {
      // Rozebírat cizí dialogy podle regexů naměřených na Claudeovi by
      // znamenalo odpovídat naslepo — u ostatních se karta nekreslí.
      if (!AG.full) return;
      const buf = term.buffer.active;
      // Odrolováno nahoru: dole je historie, ne živý dotaz.
      // Klidný prompt = žádný dotaz. Ve čtení je bublina vidět pořád, takže
      // se to pozná podle `idleNow`, ne podle toho, jestli je bublina ukázaná.
      const live = !idleNow && !tab.exited && buf.viewportY >= buf.baseY - 1;
      const lines = live ? dialogLines(term, Math.min(term.rows, DIALOG_ROWS)) : [];
      const text = lines.join('\n');
      /* Číslovaný seznam se ve výpisu objeví i jen tak (kroky, poznámky).
         Že jde o dotaz, prozradí až šipka ❯ u jedné z voleb — tu Claude Code
         kreslí jedině u toho, co se dá vybrat. */
      const found = lines.length ? scanOptions(lines) : [];
      const opts = found.some(o => o.sel) ? found : [];
      // Záložky otázek nad volbami (AskUserQuestion) — poslední nad první volbou.
      let tabsRow = -1;
      if (opts.length) {
        for (let i = opts[0].row - 1; i >= 0; i--) {
          if (TABS.test(lines[i])) { tabsRow = i; break; }
        }
      }
      const tabs = tabsRow >= 0 ? scanTabs(lines[tabsRow]) : [];
      const multi = opts.some(o => CHECKBOX.test(o.label));
      /* Na úzkém telefonu se řádek se záložkami zalomí a nenajde. Dotaz pak
         začíná pod vodorovnou čárou, kterou ho Claude Code odděluje od výpisu. */
      let top = tabsRow + 1;
      if (tabsRow < 0 && multi) {
        for (let i = opts[0].row - 1; i >= 0; i--) {
          if (RULE.test(lines[i]) && lines[i].trim()) { top = i + 1; break; }
        }
      }
      // Otázka (ne závěrečné „Submit answers") — na ní má smysl Další.
      const asking = (tabsRow >= 0 || multi) &&
                     !opts.some(o => /^Submit answers$/i.test(o.label));
      const plain = opts.length || !lines.length ? [] : scanPlain(lines);
      const picker = !opts.length && !plain.length && PICKER.test(text);
      const yesno = !opts.length && !plain.length && !picker && YESNO.test(text);

      /* Volby jako řádky karty. Číslované se odpovídají číslem, nečíslované
         dojezdem šipek — pro člověka je to v obou případech jedno kliknutí. */
      const rows = [];
      if (opts.length && (multi || asking || tabsRow >= 0)) {
        opts.forEach((o, i) => {
          const box = CHECKBOX.exec(o.label);
          const label = box ? o.label.slice(box[0].length) : o.label;
          const typing = /^Type something/i.test(label);
          const end = i + 1 < opts.length ? opts[i + 1].row : lines.length;
          rows.push({
            key: o.key, label: ownLabel(label), row: o.row,
            desc: typing ? '' : optionDesc(lines, o.row, end),
            // U zaškrtávátek „vybrané" = [✔], ne kurzor; jinde kurzor.
            sel: multi ? !!(box && box[1].trim()) : o.sel,
            check: !!box,
            // Vlastní odpověď se píše v terminálu, tam fokus patří.
            run: () => (typing ? press(o.key) : tap(o.key)),
          });
        });
      } else if (opts.length) {
        /* Trvalé povolení se pozná z celého znění volby — na úzkém okně se
           zalomí a složka je až na dalším řádku. Jednorázové Ano/Ne dostanou
           česky jen tam, kde je vedle nich trvalá volba, aby se daly srovnat. */
        const perms = permsOf(lines, opts);
        const anyPerm = perms.some(Boolean);
        opts.forEach((o, i) => {
          const p = perms[i];
          const row = {key: o.key, label: o.label, sel: o.sel, row: o.row,
                       run: () => answerOnce(o.key)};
          if (p) Object.assign(row, {perm: p, label: p.label, desc: p.detail, raw: p.raw, sel: false});
          else if (anyPerm && /^Yes$/i.test(o.label)) row.label = 'Ano, jen tentokrát';
          else if (anyPerm && /^No$/i.test(o.label)) row.label = 'Ne';
          rows.push(row);
        });
      } else if (plain.length) {
        const now = plain.findIndex(o => o.sel);
        for (const o of plain) {
          rows.push({label: o.label, sel: o.sel, row: o.row,
                     run: () => pickPlain(o.at - now)});
        }
      } else if (yesno) {
        rows.push({label: 'Ano', run: () => press('y')},
                  {label: 'Ne', run: () => press('n')});
      }
      // Hláška, ze které se jen odchází. Volbu za ni neděláme — jen cestu ven.
      const note = !rows.length && !picker && lines.length && CANCEL.test(text);

      // Text nad volbami a nápověda pod nimi. Ano/ne se nekreslí jako seznam,
      // takže tam žádné „nad" a „pod" není — bere se celý dotaz.
      const numbered = rows.length && rows[0].row != null;
      /* Nad záložkami je obyčejný výpis konverzace (rámeček tu není), takže
         text dotazu začíná až pod nimi — jinak by se do karty dostalo až
         třicet řádků toho, co Claude psal předtím. */
      const body = rows.length || note
        ? dialogBody(lines.slice(top), (numbered ? rows[0].row : lines.length) - top) : [];
      const hint = !rows.length ? ''
                 : multi ? HINT_MULTI : asking ? HINT_TABS
                 : opts.length ? (rows.some(r => r.perm) ? HINT_PERM : HINT_NUM)
                 : plain.length ? HINT_PLAIN : '';
      let blocks = bodyBlocks(body);
      /* „Esc to cancel" v textu je nápověda ke klávese, ne věta dotazu —
         v kartě ji zastupuje tlačítko. */
      for (const b of blocks) {
        if (!b.code) b.lines = b.lines.filter(l => !CANCEL_LINE.test(l.trim()));
      }
      blocks = blocks.filter(b => b.lines.length);
      if (note) {
        // Delší panel je spíš výpis než hláška a karta s jediným tlačítkem nad
        // prázdnem taky nemá co říct — v obou případech ať zůstane terminál.
        const len = blocks.reduce((n, b) => n + b.lines.length, 0);
        if (!len || len > NOTE_MAX) blocks = [];
        else rows.push({label: 'Zavřít (Esc)', run: () => press('\x1b')});
      }
      // První krátká věta je nadpis („Bash command", „Accessing workspace:").
      let title = '';
      const head = blocks[0];
      if (head && !head.code && head.lines[0].length <= 70) {
        title = head.lines[0];
        head.lines = head.lines.slice(1);
        if (!head.lines.length) blocks = blocks.slice(1);
      }

      /* Výběr ze seznamu (historie, volba modelu) do karty nepatří: seznam je
         v terminálu a karta by ho překryla. Tomu zbývá lišta se šipkami. */
      const buttons = [];
      if (picker) {
        buttons.push({label: '↑', title: 'O položku výš', run: () => press('\x1b[A')},
                     {label: '↓', title: 'O položku níž', run: () => press('\x1b[B')},
                     {label: 'Vybrat (Enter)', run: () => press('\r')},
                     {label: 'Zrušit (Esc)', ghost: true, run: () => press('\x1b')});
      } else if (!rows.length && lines.length && CONTINUE.test(text) && !ENDED.test(text)) {
        // Čeká se jen na Enter (po přihlášení a podobně) — tlačítko do lišty,
        // ať se nemusí hledat terminál pod ní.
        buttons.push({label: /retry/i.test(text) ? 'Zkusit znovu (Enter)' : 'Pokračovat (Enter)',
                      run: () => press('\r')});
      }

      // Překreslovat se má jen při změně: jinak by tlačítko zmizelo pod prstem
      // uprostřed kliknutí, protože terminál překresluje i sám od sebe.
      const sig = [
        rows.map(r => (r.sel ? '*' : '') + (r.key || '') + r.label + (r.raw || '')).join('|'),
        tabs.map(t => (t.done ? '+' : '') + t.label).join('|'),
        asking ? 'nav' : '',
        title,
        blocks.map(b => b.lines.join('\n')).join('\n\n'),
        hint,
        buttons.map(b => b.label).join('|'),
      ].join('\u0000');
      askingNow = !!rows.length;
      // Claude čeká na odpověď — tab svítí, i když je člověk jinde (hub.js).
      if (askingNow !== cekaHlaseno) {
        cekaHlaseno = askingNow;
        if (io.ceka) io.ceka(askingNow);
      }
      if (sig === answerSig) return;
      answerSig = sig;
      sentSig = null;      // karta se změnila (i na prázdno) — další odpověď je nová

      // Nový dotaz = nová otázka, na kterou se má člověk podívat. Odsunutí
      // karty platilo pro ten předchozí.
      askRoot.classList.remove('peek');
      askRoot.hidden = !rows.length;
      if (rows.length) {
        clearTimeout(askOffTimer);
        askOffTimer = null;
        tab.pane.classList.add('asking');
      } else if (tab.pane.classList.contains('asking') && !askOffTimer) {
        askOffTimer = setTimeout(() => {
          askOffTimer = null;
          if (askingNow) return;
          tab.pane.classList.remove('asking');
          schedule();
        }, ASK_HOLD_MS);
      }
      if (rows.length) {
        askTabs.textContent = '';
        for (const t of tabs) {
          askTabs.appendChild(el('span', 'ask-tab' + (t.done ? ' done' : ''),
                                 (t.done ? '✓ ' : '') + t.label));
        }
        askTabs.hidden = tabs.length < 2;
        askTitle.textContent = title;
        askTitle.hidden = !title;
        askBody.textContent = '';
        for (const b of blocks) {
          const node = el(b.code ? 'pre' : 'p', b.code ? 'ask-code' : 'ask-p',
                          b.lines.join('\n'));
          askBody.appendChild(node);
        }
        askBody.hidden = !blocks.length;
        askOpts.textContent = '';
        for (const r of rows) {
          const btn = el('button', 'ask-opt' + (r.sel ? ' sel' : '') + (r.check ? ' check' : '') +
                                   (r.perm ? ' perm' : ''));
          btn.type = 'button';
          if (r.check) btn.appendChild(el('span', 'ask-box', r.sel ? '✓' : ''));
          else if (r.key) btn.appendChild(el('span', 'ask-num', r.key));
          const text = el('span', 'ask-opt-label', r.label);
          if (r.perm) text.appendChild(el('span', 'ask-badge', r.perm.badge));
          if (r.desc) text.appendChild(el('small', 'ask-desc', r.desc));
          // Přesné znění od Claude Code — kdyby překlad něco zamlčel.
          if (r.perm) text.appendChild(el('small', 'ask-raw', r.raw));
          btn.appendChild(text);
          // Na kterou volbu ukazuje ❯ v terminálu — tam padne Enter.
          if (r.sel && !r.check) btn.insertAdjacentHTML('beforeend', icon('i-check'));
          btn.onclick = r.run;
          // Trvalá volba se hned po objevení karty neklikne: dotaz mohl
          // naskočit pod prstem, který mířil na jiné tlačítko.
          if (r.perm) {
            btn.disabled = true;
            setTimeout(() => { btn.disabled = false; }, PERM_ARM_MS);
          }
          askOpts.appendChild(btn);
        }
        askHint.textContent = hint;
        // U hlášky je „Zavřít" jediné tlačítko nahoře — „Zrušit" v patičce
        // dělá to samé a jen mate, čím z toho se vlastně odchází.
        askEsc.hidden = note;
        // Zpět i ze závěrečného „Odeslat odpovědi" — kdo si to rozmyslí.
        askPrev.hidden = tabs.length < 2;
        askNext.hidden = !asking;
        askNext.textContent = tabs.length && tabs.every(t => t.done) || tabs.length < 2
          ? 'Hotovo →' : 'Další →';
        /* Nahoru jen u nové otázky. Zaškrtnutí kartu překreslí taky, a kdyby
           pokaždé vyjela nahoru, další volba by utekla zpod prstu. */
        const q = title + '\u0000' + rows.map(r => r.key + r.label).join('|');
        if (q !== questionSig) askCard.scrollTop = 0;
        questionSig = q;
      } else {
        questionSig = '';
      }

      answer.hidden = !buttons.length;
      answerOpts.textContent = '';
      if (buttons.length) {
        answerQ.textContent = picker ? 'Výběr:' : '';
        for (const b of buttons) {
          const btn = el('button', 'composer-answer-btn' +
                                   (b.sel ? ' sel' : '') + (b.ghost ? ' ghost' : ''),
                         b.label);
          if (b.title) btn.title = b.title;
          btn.onclick = b.run;
          answerOpts.appendChild(btn);
        }
      }
      // I zmizení lišty musí terminálu vrátit řádky, které si na ni vzala.
      holdForAnswer();
      syncHeight();
    }

    /* Karta se dá odsunout — třeba když se dotaz nepovedlo přečíst tak, jak
       ho Claude Code nakreslil. Zbyde po ní tlačítko, kterým se vrátí. */
    function askShow(on) {
      askRoot.classList.toggle('peek', !on);
      if (on) askCard.scrollTop = 0;
      term.focus();
    }
    askRoot.querySelector('[data-act=term]').onclick = () => askShow(false);
    askRoot.querySelector('.ask-back').onclick = () => askShow(true);
    askEsc.onclick = () => press('\x1b');
    // Tab i ← jdou mezi otázkami bez ohledu na to, kde stojí kurzor ❯.
    askNext.onclick = () => tap('\t');
    askPrev.onclick = () => tap('\x1b[D');

    /* Lišta s odpovědí leží přes spodek terminálu — tedy přes poslední řádky
       dialogu, který popisuje. Terminál se o ni proto na tu chvíli zkrátí,
       ať zůstane vidět celá otázka. */
    let answerHold = -1;
    function holdForAnswer() {
      if (shown || answer.hidden) {
        if (answerHold >= 0) {
          answerHold = -1;
          dirty = true;
          io.reserve(reserved > 0 ? reserved : 0);
        }
        return;
      }
      const px = Math.round(root.offsetHeight);
      if (Math.abs(answerHold - px) < 1) return;
      answerHold = px;
      dirty = true;
      io.reserve(px);
    }

    async function attach(files) {
      const paths = await io.upload(files);
      if (paths.length) insertPaths(paths);
    }

    /* Cesta k souboru je jediné, co si Claude Code z obrázku vezme — jenže
       v poli je z ní jen dlouhý řetěz, ze kterého nikdo nepozná, co vlastně
       přiložil. Cesty se proto drží stranou, v bublině je vidět náhled a
       k textu se připojí až při odeslání.

       Sem chodí i to, co člověk pustí nebo vloží kdekoli v tabu — hub.js se
       ptá bubliny dřív, než by cestu napsal do terminálu. */
    const atts = [];

    function insertPaths(paths) {
      if (!shown || !paths.length) return false;
      for (const path of paths) {
        if (!atts.includes(path)) atts.push(path);
      }
      renderAtts();
      lastInsert = Date.now();
      input.focus();
      return true;
    }

    function dropAtt(path) {
      const at = atts.indexOf(path);
      if (at >= 0) atts.splice(at, 1);
      renderAtts();
      input.focus();
    }

    function renderAtts() {
      attBox.textContent = '';
      attBox.hidden = !atts.length;
      for (const path of atts) {
        const name = path.split(/[\\/]/).pop();
        const chip = el('div', 'composer-att');
        // Celá cesta patří pod myš, ne do bubliny — tam mluví sám náhled.
        chip.title = path;
        const x = el('button', 'composer-att-x');
        x.title = 'Odebrat přílohu';
        x.innerHTML = icon('i-close');
        x.onclick = () => dropAtt(path);
        // Jméno pasteovaného screenshotu je jen časové razítko, to nikomu nic
        // neřekne. U obrázku proto stojí za sebe náhled, jméno až u ostatních.
        const named = () => {
          chip.classList.remove('img');
          chip.classList.add('bare');
          chip.insertBefore(el('span', 'composer-att-name', name), x);
        };
        if (IMG_EXT.test(name) && io.imageUrl) {
          chip.classList.add('img');
          const img = el('img');
          img.src = io.imageUrl(path);
          img.alt = name;
          // Náhled se nemusí povést (soubor mimo složku hubu) — pak zbyde jméno.
          img.onerror = () => { img.remove(); named(); };
          chip.appendChild(img);
          chip.appendChild(x);
        } else {
          chip.appendChild(x);
          named();
        }
        attBox.appendChild(chip);
      }
      ready();
      // Pruh s náhledy je o kus vyšší bublina — terminál se o něj musí zkrátit.
      if (shown) { dirty = true; fitOver(); }
      syncHeight();
    }

    /* Ctrl+V s obrázkem v bublině. Vkládání do terminálu si řeší clipboard.js
       přes server, jenže když je bublina vidět, fokus drží její textové pole —
       a tam Ctrl+V obslouží prohlížeč. Text zvládne, ale obrázek pod WebKitGTK
       do `clipboardData` nedá vůbec nic, takže vkládání screenshotu do bubliny
       vyznělo naprázdno. Doptáme se tedy serveru: ten na systémovou schránku
       dosáhne (xclip / wl-paste), odloží si kopii na disk a vrátí cestu.

       Text schválně neřešíme — ten prohlížeč vloží sám a podruhé ho nechceme.

       Jedno Ctrl+V přitom projde dvěma cestami: nejdřív jako `keydown`, pak
       jako `paste`, kterému nebereme výchozí chování kvůli textu. Bez pojistky
       se server zeptá dvakrát — a protože si obrázek pokaždé odloží pod novým
       jménem, přilepily se k promptu dvě kopie téhož screenshotu. */
    let pasteRun = null;     // běžící doptání serveru
    let pasteAt = 0;         // kdy skončilo to poslední
    let byBrowser = 0;       // kdy si obrázek vzal prohlížeč sám

    function pasteImage() {
      // Druhá cesta téhož stisku mlčí. Okno je krátké, takže dva screenshoty
      // po sobě si člověk pořád vloží oba.
      if (pasteRun) return pasteRun;
      if (Date.now() - pasteAt < PASTE_GAP) return Promise.resolve();
      pasteRun = (async () => {
        const at = Date.now();
        // Kdyby prohlížeč obrázek přece jen podal, dorazí za okamžik jako paste
        // se souborem a cestu vloží ta cesta. Pauza jí dá přednost, ať tentýž
        // screenshot neskončí v poli dvakrát.
        await new Promise(done => setTimeout(done, 140));
        if (lastInsert >= at || byBrowser >= at) return;
        let res;
        try {
          res = await io.read('clipboard');
        } catch (err) {
          return;
        }
        // Nahrávání souboru trvá dýl než tohle doptání, takže se ptáme znovu:
        // mezitím mohla cesta dorazit tou druhou cestou.
        if (lastInsert >= at || byBrowser >= at) return;
        // Bez hlášky s cestou: co se přiložilo, je vidět na náhledu v bublině.
        if (res && res.image) insertPaths([res.image]);
      })();
      return pasteRun.finally(() => { pasteRun = null; pasteAt = Date.now(); });
    }

    /* ── kolik spodku terminálu si bublina bere ───────────────────────────── */

    /* Bublina leží přes spodek terminálu, aby překryla vstupní pole, které si
       Claude Code kreslí sám. Jenže její výška se s tím polem nepotká: je-li
       bublina vyšší, spolkne navíc i poslední řádky výpisu — a to bývá zrovna
       to, co má člověk číst (zařazená zpráva, poslední odpověď). Naměří se
       proto, kolik řádků Claudeovo pole zabírá, a terminál se o ten rozdíl
       zkrátí. Bublina pak končí přesně na horní hraně Claudeova pole. */

    // Výška jednoho řádku. Terminál kreslí DOM renderer, takže řádek je prvek;
    // kdyby nebyl, spočítá se z plátna.
    function cellHeight() {
      const rows = tab.pane.querySelector('.xterm-rows');
      const first = rows && rows.firstElementChild;
      if (first) {
        const h = first.getBoundingClientRect().height;
        if (h > 4) return h;
      }
      const screen = tab.pane.querySelector('.xterm-screen');
      if (screen && term.rows) {
        const h = screen.getBoundingClientRect().height / term.rows;
        if (h > 4) return h;
      }
      return 0;
    }

    /* Kolik spodních řádků patří Claudeovu vstupnímu poli. Šipka ❯ je jeho
       prostředek, nad ní je horní hrana rámečku, pod ní dolní hrana a nápověda
       k režimu. Zařazené zprávy leží nad rámečkem, ty se počítat nesmí — právě
       o ně tady jde. */
    function ownRows(lines) {
      for (let k = lines.length - 1; k >= 0; k--) {
        if (!PROMPT.test(lines[k])) continue;
        const row = (lines.from || 0) + k;
        return term.rows - Math.max(0, row - 1);
      }
      return 0;
    }

    // Výška bubliny samotné, bez dorovnání na celé řádky.
    function naturalHeight() {
      const keep = root.style.minHeight;
      root.style.minHeight = '0px';
      const h = root.offsetHeight;
      root.style.minHeight = keep;
      return h;
    }

    let reserved = -1;       // kolik pixelů dole si bublina drží
    /* Rezervace se počítá z výšky bubliny, jenže bublina se řídí tím, kolik
       řádků terminálu zbylo — a to rezervace mění. Když si dvě hodnoty vymění
       místo, obraz skáče: terminál se přepočítá, agent překreslí celé TUI,
       z něj vyjde původní rezervace a jede se dokola. Pojistky níž porovnávají
       jen s poslední hodnotou, takže cyklus A→B→A→B nezastaví — proto se
       pamatuje, co bylo nasazeno za poslední chvilku, a návrat na to se
       přeskočí. Po CYCLE_MS klidu se paměť zapomene, ať pozdější poctivá
       změna projde. */
    const CYCLE_MS = 1500;
    let seen = [];           // {v, t} rezervace nasazené za poslední chvilku
    let seenSpace = -1;      // pro kterou výšku panelu paměť platí
    let lastLines = null;    // poslední naměřený spodek terminálu
    let lastOwn = 0;
    let dirty = true;        // je co přeměřit (jinak se sahá jen na regexy)

    function fitOver() {
      // Ve čtení drží rezervaci na nule apply() (terminál má celou plochu).
      // Přeměření z psaní (autogrow) by si ji bralo zpátky a každé písmeno
      // pak terminál zkrátilo a vrátilo — dvakrát celé TUI znovu.
      if (tab.cteni && !tab.pane.classList.contains('terminal')) return;
      // U cizího agenta se nepočítá, kolik řádků patří jeho vstupnímu poli —
      // to je měřené na Claudeovi. own = 0 znamená „nepřekrývat nic",
      // takže se terminál zkrátí přesně o výšku bubliny.
      // Kód z přihlášení: výzva se nepřekrývá (řádek „Paste code here" má
      // zůstat vidět), terminál se zkrátí o celou bublinu jako u cizího agenta.
      const own = AG.full && !codePrompt ? (lastLines ? ownRows(lastLines) : 0) : 0;
      if (AG.full && !own && !codePrompt) return;
      // Přeměřovat při každém překreslení by znamenalo vynutit si přepočet
      // rozvržení stránky uprostřed výpisu. Sáhne se na to, jen když se něco
      // změnilo — jinak stačí porovnat čísla řádků.
      if (!dirty && own === lastOwn) return;
      const cell = cellHeight();
      const box = tab.termbox;
      if (!cell || !box) return;
      dirty = false;
      lastOwn = own;
      // Místo od horní hrany terminálu po spodek panelu — o tohle se terminál
      // s bublinou dělí.
      const space = tab.pane.getBoundingClientRect().bottom -
                    box.getBoundingClientRect().top;
      const natural = naturalHeight();
      /* Terminál dostane tolik celých řádků, aby na bublinu zbylo aspoň
         tolik, kolik potřebuje. Bublina pak sahá přesně na horní hranu
         Claudeova vstupního pole — ani o řádek výš, kde už je výpis.

         Počítá se přes počet řádků schválně: terminál kreslí jen celé řádky
         a zbytek pod nimi nechává prázdný, takže z pixelů by vyšla mezera. */
      let rows = Math.floor((space + own * cell - natural) / cell);
      rows = Math.max(1, Math.min(rows, Math.floor(space / cell)));
      root.style.minHeight =
        Math.min(space, space - (rows - own) * cell) + 'px';
      const keep = Math.round(space - rows * cell);
      // Místo se drží i ve složeném stavu: uvolnit ho při každém dialogu by
      // znamenalo terminál pořád zvětšovat a zmenšovat, a překreslování v něm
      // je vidět víc než prázdný proužek dole.
      if (Math.abs(reserved - keep) < 1) return;
      // Jiná výška panelu = jiný výpočet, paměť z té minulé neplatí.
      const now = Date.now();
      if (Math.abs(space - seenSpace) >= 1) { seenSpace = space; seen = []; }
      while (seen.length && now - seen[0].t > CYCLE_MS) seen.shift();
      // Sem už jsme před chvílí sáhli — další přepnutí by byl jenom kmit.
      if (seen.some((s) => Math.abs(s.v - keep) < 1)) return;
      seen.push({v: keep, t: now});
      reserved = keep;
      io.reserve(keep);
    }

    /* ── viditelnost ──────────────────────────────────────────────────────── */

    /* Bublina na kód z přihlášení: jen pole a odeslání s popiskem, co do něj
       patří. Model, režim ani příloha k přihlašování nepatří (hub.css). */
    function setCodePrompt(on) {
      codePrompt = on;
      root.classList.toggle('code', on);
      if (on) {
        normalPlaceholder = input.placeholder;
        input.placeholder = 'Vlož kód z přihlašovací stránky a odešli (Enter)';
      } else if (normalPlaceholder) {
        input.placeholder = normalPlaceholder;
      }
      dirty = true;
    }

    /* Je na obrazovce něco, co se musí vyřídit v terminálu (TERM_NEEDED)?
       Čte se celý viditelný terminál — odkaz stojí nad polem na kód.
       Zalomené řádky se slepují: na úzkém telefonu se odkaz i „Paste code
       here" lámou přes víc řádků a „oauth/authorize" rozťaté vejpůl by se
       po řádcích nenašlo nikdy. */
    function needsTerminal() {
      if (!AG.full || !tab.cteni) return false;
      const buf = term.buffer.active;
      let line = '';
      for (let i = 0; i <= term.rows; i++) {
        const row = i < term.rows ? buf.getLine(buf.viewportY + i) : null;
        if (row && row.isWrapped) {
          line += row.translateToString(true);
          continue;
        }
        if (line && TERM_NEEDED.test(line)) return true;
        line = row ? row.translateToString(true) : '';
      }
      return false;
    }

    function looksIdle() {
      if (tab.exited) return false;
      const buf = term.buffer.active;
      // Odrolováno nahoru: tam bublina jen zakrývá historii.
      if (buf.viewportY < buf.baseY - 1) return false;
      if (!AG.full) {
        // Cizí TUI neumíme číst, tak se do něj nehádáme: bublina je vidět,
        // psát jde pořád, a terminál se o ni zkrátí, takže nic nezakryje.
        lastLines = visibleBottom(term, PROBE_ROWS);
        return true;
      }
      const lines = visibleBottom(term, PROBE_ROWS);
      const code = lines.some(l => LOGIN_CODE.test(l));
      if (code !== codePrompt) setCodePrompt(code);
      if (code) {
        lastLines = lines;
        return true;
      }
      if (lines.some(l => DIALOG.test(l))) return false;
      if (!lines.some(l => PROMPT.test(l)) || !lines.some(l => HINT.test(l))) {
        return false;
      }
      lastLines = lines;
      // Režim se dá poznat jen z nápovědy pod vstupním polem — a tu bublina
      // vzápětí překryje. Tady je naposledy vidět, tak se z ní opíše na chip.
      syncMode(lines);
      return true;
    }

    /* Záloha startu: prompt se pozná podle šipky a nápovědy pod ní. Když
       nápověda chybí (úzký telefon ji ořízne, jiná verze Claude Code ji píše
       jinak, na jejím místě je upozornění), zpráva by čekala věčně. Stačí
       proto i samotná šipka vstupního pole — když na obrazovce není žádný
       dotaz a terminál už pár vteřin nic nekreslí. */
    const START_TICHO_MS = 2500;
    const START_MIN_MS = 4000;
    function startZaloha() {
      if (!AG.full || tab.exited || !tab.id || Date.now() - bornAt < START_MIN_MS) return false;
      if (Date.now() - (tab.lastOut || 0) < START_TICHO_MS) return false;
      const buf = term.buffer.active;
      if (buf.viewportY < buf.baseY - 1) return false;
      if (dialogDole()) return false;
      return visibleBottom(term, PROBE_ROWS).some((l) => PROMPT.test(l));
    }
    // Když terminál mlčí, nic ho nepřekresluje a apply by se nezavolal —
    // čekající zpráva se proto kontroluje i sama, jednou za vteřinu.
    const cekaHlidac = setInterval(() => { if (cekaZprava && !odchazi) apply(); }, 1000);

    function apply(force) {
      const idle = looksIdle();
      if (idle && !idleNow) idleSince = Date.now();
      idleNow = idle;
      if (idle) everIdle = true;
      /* Zpráva napsaná, než Claude Code naběhl, odchází až ve chvíli, kdy
         prompt chvíli vydržel. Claude Code ho totiž kreslí dřív, než doběhne
         start (ještě se ptá terminálu, co umí) — a Enter poslaný do té doby
         se ztratí, zatímco text zůstane stát v jeho poli. Naměřeno. */
      if (cekaZprava && !odchazi) {
        const vydrzel = idle ? Date.now() - idleSince : 0;
        if ((idle && vydrzel >= READY_MS) || startZaloha()) {
          everIdle = true;           // i ze zálohy — jinak by submit zprávu vrátil do čekání
          odchazi = true;
          const zprava = cekaZprava;
          cekaZprava = '';
          submit(zprava, {zeStartu: true});
          odchazi = false;
        } else if (idle) {
          clearTimeout(cekaTimer);
          cekaTimer = setTimeout(() => apply(), READY_MS - vydrzel + 30);
        }
      }
      if (needsTerminal()) termHold = true;
      else if (termHold && idle && !codePrompt) termHold = false;
      const naTerminal = termHold && AG.full && !!tab.cteni;
      tab.pane.classList.toggle('terminal', naTerminal);
      const cteni = !!tab.cteni && !naTerminal && !tab.pane.classList.contains('asking');
      const want = !hiddenByUser && (idle || cteni);
      if (want !== shown && (force || Date.now() - flippedAt >= DWELL_MS)) {
        flippedAt = Date.now();
        const hadTerm = tab.pane.contains(document.activeElement) &&
                        document.activeElement !== input;
        shown = want;
        root.classList.toggle('on', shown);
        if (!shown) root.style.minHeight = '';
        dirty = true;
        syncHeight();
        // Fokus musí jít za tím, do čeho se píše. Jinak by psaní s diakritikou
        // (skládané klávesy) skončilo v poli schovaném pod bublinou.
        if (shown && hadTerm) input.focus();
        if (!shown && document.activeElement === input) term.focus();
      } else if (want !== shown) {
        schedule();          // rozhodne se, až se překreslování ustálí
      }
      // Měřit jde až s nasazenou třídou: složená bublina je jenom proužek
      // a vyšla by z ní čtvrtinová výška.
      if (AG.full) readBanner();
      if (shown && tab.cteni && !naTerminal) {
        /* Ve čtení je terminál schovaný, takže není co přesně překrývat.
           Přeměřování podle Claudeova vstupního pole (fitOver) tu bublinu
           při každém překreslení výpisu natahovalo a smršťovalo a zkracovalo
           terminál — Claude Code pak překreslil a kolo se točilo znovu. Pole
           na psaní tím skákalo pod rukama. Tady má bublina svou výšku
           a terminál celou plochu, jednou provždy. */
        if (root.style.minHeight) root.style.minHeight = '';
        if (reserved !== 0) { reserved = 0; io.reserve(0); }
        refreshModel();
      } else if (shown) {
        fitOver();
        refreshModel();
        // Tab otevřený rovnou do režimu (Bypass po potvrzení): přepne se,
        // jakmile Claude Code čeká na zadání a nápověda s režimem je čitelná.
        if (tab.wantMode && !switching) {
          const want = tab.wantMode;
          tab.wantMode = '';
          setMode(want);
        }
      }
      renderAnswer();
      sledujPraci();
    }

    /* Co Claude zrovna dělá — pro čtení, kde terminál není vidět a jinak by
       se nepoznalo, jestli pracuje, nebo čeká. Hlásí se jen změna. */
    let pracSig = '';
    let hookBezi = false;
    let zabitVidet = false;
    function sledujPraci() {
      if (io.upozorni && AG.full) {
        const zabit = visibleBottom(term, WORK_ROWS).some((l) => KILLED.test(l));
        if (zabit && !zabitVidet) {
          // Technická příčina (došla paměť, strop na celý prostor) je
          // v agent-wrapper.sh — člověku stačí vědět, co udělat.
          io.upozorni('Claude se zasekl. Rozhovor je uložený — zavři rozhovory, ' +
                      'které nepotřebuješ, a zkus to znovu.');
        }
        zabitVidet = zabit;
      }
      if (!io.prace || !AG.full) return;
      let stav = {on: false};
      if (!tab.exited) {
        const dole = visibleBottom(term, WORK_ROWS);
        const spin = dole.map((l) => SPINNER.exec(l)).filter(Boolean).pop();
        if (spin || dole.some((l) => WORKING.test(l))) {
          const zavorka = (spin && spin[2]) || '';
          const sek = /(\d+)\s*s\b/.exec(zavorka);
          const tok = /↓\s*([\d.,]+\s*k?)\s*tokens/i.exec(zavorka);
          /* Stop hooky („Auto-saving… 0/3 · 7s") — jen podle textu: samotné
             „… 2/5 ·" ukazuje Claude Code i u rozdělaných úkolů uprostřed práce. */
          const radek = (spin && spin[0]) || '';
          stav = {on: true, sloveso: spin ? spin[1] : '',
                  hook: /…\s*\d+\/\d+\s*·/.test(zavorka) && /(auto-?sav|hook|hlídám|ukládám)/i.test(radek + ' ' + zavorka),
                  sekund: sek ? Number(sek[1]) : null,
                  tokeny: tok ? tok[1].replace(/\s/g, '') : ''};
        }
      }
      /* Po odpovědi ještě běží hooky (Stop: „Auto-saving… 0/3 · 7s") — spinner
         je stejný, ale Claude už nepracuje. Automatika pak smí přepnout: slash
         příkazy se zařadí do fronty před zprávu a proběhnou, až hooky doběhnou. */
      pracuje = !!stav.on && !stav.hook;
      spinner = !!stav.on;
      hookBezi = !!stav.hook;
      if (pracuje) pracovalAt = Date.now();
      const sig = JSON.stringify(stav);
      if (sig === pracSig) return;
      pracSig = sig;
      io.prace(stav);
    }

    let pending = null;
    function schedule() {
      if (pending) return;
      pending = setTimeout(() => { pending = null; apply(); }, 90);
    }

    const offRender = term.onRender(schedule);
    const offScroll = term.onScroll(schedule);
    /* Tab na pozadí se nekreslí (onRender mlčí), a terminál pod čtením taky ne
       (je odsunutý mimo obrazovku, hub.css) — dotaz v nich ale poznat musíme,
       jinak se karta s otázkou neukáže a Claude čeká na povolení navždy.
       Výpis se proto čte i bez kreslení; schedule() dvojí podnět sloučí. */
    const offParsed = term.onWriteParsed(schedule);
    // Jiná velikost okna = jiná výška řádku i jiný počet řádků, přeměřit.
    const offResize = term.onResize(() => { dirty = true; schedule(); });
    // Roste s textem a mizí s přepnutím tabu — obojí musí hlášky poznat.
    const sizes = new ResizeObserver(syncHeight);
    sizes.observe(root);

    /* ── klávesy ──────────────────────────────────────────────────────────── */

    input.addEventListener('keydown', (ev) => {
      // Na dotykové klávesnici Enter = nový řádek, odesílá tlačítko (ime.js).
      if (ev.key === 'Enter' && !ev.shiftKey && !(global.HubIME && !global.HubIME.enterOdesle(ev))) {
        ev.preventDefault();
        send();
        return;
      }
      if (ev.key === 'Escape') {
        // Esc patří Claudeovi (přeruší práci), text v bublině zůstává.
        ev.preventDefault();
        toPty('\x1b');
        return;
      }
      if (ev.key === 'Tab' && ev.shiftKey) {
        ev.preventDefault();
        toPty('\x1b[Z');
        // Nápověda s režimem je pod bublinou, tak ať se změna projeví na chipu.
        setTimeout(readMode, MODE_STEP);
        return;
      }
      // Bez preventDefault: text si vloží prohlížeč sám, my jen doplníme to,
      // co nám z obrázku nedá.
      if ((ev.ctrlKey || ev.metaKey) && !ev.altKey &&
          (ev.key || '').toLowerCase() === 'v') {
        pasteImage();
        return;
      }
      // Historie je vlastní: Claudeovu bychom listovali v poli, které není
      // vidět, a člověk by netušil, co vlastně odesílá.
      if ((ev.key === 'ArrowUp' || ev.key === 'ArrowDown') && history.length) {
        const oneLine = !input.value.includes('\n');
        if (!oneLine) return;
        ev.preventDefault();
        if (ev.key === 'ArrowUp') {
          if (histAt === -1) { draft = input.value; histAt = history.length; }
          histAt = Math.max(0, histAt - 1);
          input.value = history[histAt];
        } else {
          histAt = histAt === -1 ? -1 : histAt + 1;
          input.value = histAt >= history.length ? (histAt = -1, draft)
                                                 : history[histAt];
        }
        autogrow();
        input.setSelectionRange(input.value.length, input.value.length);
      }
    });

    input.addEventListener('input', autogrow);
    input.addEventListener('input', nazorPriPsani);
    input.addEventListener('input', () => { if (io.pise) io.pise(); });
    input.addEventListener('paste', (ev) => {
      const cd = ev.clipboardData;
      const files = cd && cd.files;
      // Text má přednost: zkopírovaný soubor ze správce nese vedle sebe i svoje
      // jméno jako text, a kdo kopíroval text, čeká text.
      const text = cd && cd.getData && cd.getData('text/plain');
      if (files && files.length && !text) {
        ev.preventDefault();
        attach(files);
        return;
      }
      // Ani soubor, ani text — přesně tak vypadá screenshot pod WebKitGTK.
      // Ctrl+V si hlídá keydown, tohle je pro Shift+Insert a nabídku.
      if (!text) pasteImage();
    });

    /* Když je bublina vidět, psaní do terminálu by končilo v poli schovaném pod
       ní. Písmeno tedy přesměrujeme do bubliny; ovládací klávesy si terminál
       nechává, ať Ctrl+C, výběr textu a dialogy fungují dál. */
    term.attachCustomKeyEventHandler((ev) => {
      if (!shown || ev.type !== 'keydown') return true;
      if (ev.ctrlKey || ev.altKey || ev.metaKey) return true;
      if (ev.key.length !== 1) return true;
      // preventDefault je tu nutný: po přesunu fokusu by prohlížeč to samé
      // písmeno do pole zapsal ještě jednou (naměřeno — psalo se „aa").
      ev.preventDefault();
      input.focus();
      insert(ev.key, {focus: true});
      return false;
    });

    root.querySelector('.composer-send').onclick = send;
    root.querySelector('.composer-peek').onclick = () => {
      hiddenByUser = false;
      apply(true);
      if (shown) input.focus();
    };
    modelBtn.onclick = modelMenu;
    root.querySelector('[data-act=slash]').onclick = slashMenu;
    root.querySelector('[data-act=history]').onclick = historyMenu;
    root.querySelector('[data-act=file]').onclick = () => picker.click();
    modeBtn.onclick = (ev) => { syncLevel(); modeMenu(ev); };
    root.querySelector('[data-act=esc]').onclick = () => { toPty('\x1b'); input.focus(); };
    picker.onchange = () => {
      if (picker.files && picker.files.length) attach(picker.files);
      picker.value = '';
    };

    agentBtn.onclick = agentMenu;
    syncModel();
    modeBtn.classList.add('normal');
    // Režim oprávnění cykluje Shift+Tab a hlásí se pod vstupním polem —
    // obojí je Claude Code. Jinde by to tlačítko jen mačkalo tabulátor.
    modeBtn.hidden = !AG.full;
    agentBtn.querySelector('.val').textContent = AG.label;
    syncLevel();
    // Přepnutí „Pro pokročilé" mění třídu na <body> — bublina se podle ní
    // přestaví hned, ne až při dalším překreslení terminálu.
    const levelWatch = new MutationObserver(() => syncLevel());
    levelWatch.observe(document.body, {attributes: true, attributeFilter: ['class']});
    if (io.agent) {
      const a = io.agent();
      // Barvu dodává hostitel: tečka v bublině má říkat totéž co tečka na
      // tabu nad ní (prostředí, případně firemní trezor), ne co agent.
      if (a) {
        agentBtn.querySelector('.composer-dot').style.background =
          io.agentColor ? io.agentColor(a) : a.color;
      }
    }
    // Na úzké obrazovce se dlouhá výzva zalomila do dvou řádků a spodní byl
    // uříznutý — pole vypadalo rozbitě. Tam stačí krátká.
    // Dotyková klávesnice ukáže místo „Odeslat“ šipku nového řádku (ime.js).
    if (global.HubIME && global.HubIME.dotykove()) input.setAttribute('enterkeyhint', 'enter');
    input.placeholder = window.matchMedia('(max-width: 520px)').matches
      ? 'Napiš zprávu…'
      : 'Napiš, s čím ti má ' + AG.label + ' pomoct… (Enter odešle, Shift+Enter nový řádek)';
    schedule();

    return {
      insertPaths,
      /* Prohlížeč si obrázek ze schránky vzal sám (dostal ho v `clipboardData`
         jako soubor) a nahrává ho. Doptávat se ještě serveru by znamenalo
         přiložit tentýž screenshot dvakrát — jednou jako nahraný soubor,
         podruhé jako kopii odloženou serverem. */
      browserPaste: () => { byBrowser = Date.now(); },
      /* Model, se kterým se tab doopravdy spustil. Ollama si ho doplňuje sama
         (bez modelu není co pustit), a to se pozná až z odpovědi serveru —
         tou dobou už bublina dávno stojí. */
      setModel: (m) => {
        if (!m || m === model) return;
        model = m;
        syncModel();
      },
      /* Rychlé akce z pravého panelu. Příkaz bez koncového \r se má jen
         napsat — třeba /screenshot čeká, až doplníš adresu. */
      run: (cmd) => {
        if (!shown) return false;
        if (cmd.endsWith('\r')) submit(cmd.slice(0, -1));
        else insert(cmd);
        return true;
      },
      focus: () => { if (shown) input.focus(); },
      // Rozepsaná zpráva — přežije restart prostoru při aktualizaci (hub.js).
      draft: () => input.value,
      znovu,
      setDraft: (text) => { if (!input.value) { input.value = text; autogrow(); } },
      visible: () => shown,
      hide: () => { hiddenByUser = true; apply(true); },
      release: () => {
        pryc = true;
        fronta.length = 0;
        clearInterval(cekaHlidac);
        levelWatch.disconnect();
        offRender.dispose();
        offScroll.dispose();
        offParsed.dispose();
        offResize.dispose();
        sizes.disconnect();
        if (pending) clearTimeout(pending);
        clearTimeout(askOffTimer);
        root.remove();
        askRoot.remove();
        tab.pane.classList.remove('asking');
        if (reserved > 0) io.reserve(0);
        syncHeight();
      },
    };
  }

  global.HubComposer = {install, installKeys, _test: {scanOptions, permInfo, permsOf, dialogBody, bodyBlocks}};

})(window);
