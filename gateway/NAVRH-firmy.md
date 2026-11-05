# Návrh: víc firem na jedné bráně

**Stav: hotové (2.82.0), odzkoušené na testovací bráně (Ubuntu 26.04 arm64, bwrap).**
Struktura schválená (Petr + Jirka, 14. 10. 2026). Jak se to používá: README brány,
„Víc firem“.

Dnes má brána jeden firemní Obsidian pro všechny (`/home/hub/firma/Firemní
Brain`). Agentura ale pracuje pro víc firem a pro každou z nich pro víc klientů,
a lidé zvenku (externí kodér, grafik) mají vidět jen to, na čem dělají. Návrh
dělá z jedné firmy víc firem — každou s vlastním trezorem, lidmi, rolemi, klienty
a přístupy — a nic nemění tomu, kdo další firmu nezaloží.

## Pojmy

Firma = vlastní trezor, skilly a lidé s rolí. Je jednoho ze dvou **druhů**
(volí se při založení, změnit jde, dokud je firma prázdná):

```
agentura — pracuje pro klienty           firma pro sebe — klienty nemá
Krekrrr                                   Good Food
└─ Klienti/<klient>   (Pytlík)            └─ Projekty/<projekt>   (Web, Pokladna)
   └─ <podkategorie>  (Web, Finance)
```

Nejhlubší úroveň (podkategorie, resp. projekt) je zároveň „skupina“ — příjemce
sdílených napojení a chatů a místo, kde se otevírá chat.

| role | co smí |
|---|---|
| **generál** | všechno ve firmě: lidi, role, klienty, podkategorie, přístupy, mazat |
| **člen** | číst a zapisovat tam, kam vidí; mazat jen s příznakem `mazat` |
| **host** | jen číst tam, kam vidí |

**Kdo co vidí:**

- Generál vidí celou firmu.
- Člen a host vidí **obecné věci firmy** — všechno mimo `Klienti/`.
- U firmy s projekty je **projekt** skrytý a zpřístupňuje se jednou úrovní.
- **Klient** je ve výchozím stavu skrytý. Kdo dostane přístup ke klientovi, vidí
  soubory přímo v jeho složce (kdo to je, kontakty, brief), ale ne podkategorie.
- **Podkategorie** je ve výchozím stavu skrytá a vyžaduje i přístup ke klientovi.
  Pepa s přístupem `Pytlík` + `Pytlík/Web` vidí obecné věci firmy, obecné
  věci Pytlíku a celý Web — Marketing ani Finance Pytlíku ne.
- Skrytí je natvrdo, stejným mechanismem jako dnes omezená poznámka
  (`poznamky.mask_args`): co člověk nevidí, v jeho sandboxu vůbec není.

Jeden účet může být ve víc firmách s různou rolí. Firmy se nemíchají: každý
zápis jde přes bránu s konkrétní firmou a každý tab Clauda patří jedné firmě.

## Kde to leží

```
/home/hub/firmy/                       HUB_GW_FIRMY_DIR
├─ .firmy.json                         firmy, lidé (id účtu → role, mazat), přístupy, kdo smí zakládat
├─ zmeny.jsonl                         kdo co ve firmách změnil (bez obsahu)
└─ <zkratka>/
   ├─ Brain/                           trezor firmy (CLAUDE.md, skills/, Klienti/, …)
   ├─ chaty/                           uložené chaty podkategorií (mimo trezor)
   ├─ smazano/                         odložení klienti a podkategorie
   ├─ nahrano.jsonl                    kdo co nahrál
   └─ poznamky-pristupy.json           omezení jednotlivých poznámek (jako dnes)
```

Registr leží **vedle trezorů, ne v nich** — prostor má trezory jen ke čtení
a do registru nedosáhne vůbec, takže si přístup nepřidá.

`.firmy.json`:

```json
{"zakladatele": [2],
 "firmy": {"krekrrr": {
   "nazev": "Krekrrr", "zalozil": 1, "zalozeno": "2026-10-15 10:00:00",
   "lide": {"1": {"role": "kapitan"}, "7": {"role": "clen", "mazat": false}},
   "pristupy": {"Klienti/Květinářství Pytlík": [7],
                "Klienti/Květinářství Pytlík/Web": [7]}}}}
```

Přístupy se drží podle cesty a id účtů. Přejmenování klienta nebo podkategorie
dělá brána (`firmy.prejmenovat`) a přepíše i cesty v `pristupy`.

## Dnešní firma

Dnešní firemní trezor se stane firmou se zkratkou `firma` — **nic se
nestěhuje**, adresář zůstává `HUB_GW_COMPANY_DIR`. Lidé a role se pro ni
odvozují z dnešní správy přístupů: admin → generál, `write` → člen (s `mazat`),
`read` → host, `none` → není členem. Výchozí `HUB_GW_COMPANY_DEFAULT` platí dál,
takže na serveru, kde nikdo další firmu nezaloží, je všechno jako dřív:
stejné cesty, stejné pokyny pro Clauda, stejné tlačítko Přístupy.

Hub ve starší verzi dostane v `hub-config.json` dál `company_vault`
(dnešní firma) — kombinace nová brána + starší hub funguje jako dnes.

## Sandbox

`workspace.session_spec` přiváže **trezory všech firem, kde je člověk členem**,
každý jen ke čtení a s vlastními `mask_args` (skryté klienty, podkategorie
a poznámky). Kdo ve firmě není, ten její trezor v sandboxu nemá. Přepnutí
firmy v okně tak nepotřebuje restart prostoru; změna členství nebo přístupů
ano — jako dnes u sdílených Obsidianů (hub nabídne restart, nečinný prostor
odebranému člověku brána zastaví hned).

## Hub: prostor okna

- `hub-config.json` dostane `firmy`: `[{zkratka, nazev, cesta, role, mazat}]`.
- Trezor v API hubu: `firma:<zkratka>`. Staré `firma` = dnešní firma.
- **Okno je v osobním prostoru, nebo v jedné firmě** — přepínač vedle „Server“
  v hlavičce (sessionStorage okna; v jednom okně Krekrrr, v druhém Good Food).
  Zakládání a správa firem je v něm taky.
- Podle prostoru se řídí všechno: jedno **„Nový chat“** (ve firmě fialové),
  **chaty** v panelu, **taby** v liště (jiný prostor schovaný, běží dál),
  **Projekty → Klienti** ve firmě (klienti a podkategorie, jak je prostor
  vidí, u každé „+ chat“), **Obsidian** a **úvodní stránka** (v osobním
  prostoru dlaždice firem, ve firmě její klienti a podkategorie).
- **Chat firmy běží ve složce `~/firmy/<zkratka>[/<klient>/<podkategorie>]`**
  — pracovní soubory se nemíchají s osobními ani s jinou firmou a podle složky
  hub pozná, kam chat patří (i otevřený znovu ze seznamu). V podkategorii
  dostane Claude pokyn: dokumentace je v `Klienti/<klient>/<podkategorie>/`,
  začni jejím `CLAUDE.md`.
- **Tab patří firmě** (`tab.vault = 'firma:<zkratka>'`, `HUB_VAULT=firma:<zkratka>`)
  a jmenuje se `Firma · podkategorie`. Taby různých firem můžou být vedle sebe.
- `tools/firma.py navrh` zapisuje do firmy z `HUB_VAULT`, v osobním tabu
  `--firma <zkratka>`.
- Pokyny v `~/.claude/CLAUDE.md` prostoru vyjmenují firmy člověka, jeho roli
  a cesty; pokyn firemního tabu jmenuje jen tu jednu firmu.

## Pravidla pro Clauda ve složkách

Většinu dokumentace bude psát Claude. Každá firma, klient i podkategorie může
mít vlastní `CLAUDE.md` — co do složky patří, co ne (citlivé údaje, finance),
jak pojmenovávat poznámky. Pokyn firemního tabu říká: **před zápisem do složky
si přečti `CLAUDE.md` v ní a ve všech složkách nad ní.** Generál je píše jako
každou jinou poznámku. Nová podkategorie dostane výchozí `CLAUDE.md`.

## Správa v appce: Nastavení → Firmy

- **Založit firmu:** admin a účty, kterým to admin povolí (Nastavení → Tým
  „smí zakládat firmy"). Kdo firmu založí, je její generál.
- **Generál:** lidi (přidat existující účet, nebo pozvat nový — heslo vymyslí
  brána jako v Týmu), role a `mazat`, klienti a podkategorie (založit,
  přejmenovat, smazat = odložit stranou), přístupy.
- **Přehled „kdo kam vidí":** tabulka lidé × klienti a podkategorie se
  zaškrtávátky. Na telefonu seznam po lidech.
- Brána: `GET/POST /gw/firmy` (akce `zalozit`, `lide`, `klient`, `podkategorie`,
  `pristup`, `prejmenovat`, `smazat`). POST jen ze stránky hubu
  (`_account_post_ok`), oprávnění rozhoduje jen `gateway/firmy.py`.
- CLI: `claude-hub-admin firmy` (seznam), `firmy <zkratka>` (lidé a přístupy),
  `firmy zalozit`, `firmy kapitan`, `firmy zakladatele`.

## Sdílení do podkategorie

**Napojení (MCP)** — přístupové údaje k webu klienta (WordPress, Directus,
Ecomail přes klíč…) se nasdílí podkategorii, ne jmenovitě. Jedno napojení může
patřit víc podkategoriím (Ecomail Marketingu i Webu). Klíče drží brána jako
dnes (`mcp_sdilene.py`) a do prostoru se nedostanou — **Pepův Claude s nimi
pracuje, Pepa je nezná.** Člen podkategorie napojení používá, dokud k ní má
přístup; odebráním přístupu ho ztratí hned (ověřuje se u každé zprávy).

> Pozor: tohle platí jen pro napojení. Cokoli dané do prostředí nebo souboru
> v prostoru si člověk přečte. Přihlašovací údaje proto patří jen do napojení.

**Chaty** — dvě možnosti, vybírá ten, kdo sdílí:

- **živě** (jako dnes sdílený chat): vidí ho všichni s přístupem k podkategorii,
  dokud majitel sdílení nezruší; psát smí, komu to majitel povolí,
- **uložit do podkategorie**: kopie přepisu do `chaty/<podkategorie>/`, na kterou
  kdokoli s přístupem naváže přes „Pokračovat u sebe" — i když autor
  z firmy odejde. Ukládá se jen na pokyn, nic automaticky.

## Co se mění

| soubor | co |
|---|---|
| `gateway/firmy.py` (nový) | registr firem, role, přístupy, kdo co vidí, skrývání pro sandbox |
| `gateway/config.py` | `FIRMY_DIR` |
| `gateway/poznamky.py`, `slozky.py`, `soubory.py` | cesty a registr podle firmy, ne natvrdo |
| `gateway/workspace.py` | trezory firem v sandboxu, `hub-config` `firmy`, pokyny, zápis do firmy |
| `gateway/server.py` | `/gw/firmy`, `/gw/firma/*` s parametrem firmy |
| `gateway/mcp_sdilene.py`, `relace.py` | sdílení podkategorii |
| `gateway/mcp.py` | nástroje Obsidianu pro appku Claude s firmami |
| `gateway/admin.py` | `claude-hub-admin firmy …` |
| `hub/core.py`, `hub/server.py` | `firma:<zkratka>`, taby firem |
| `hub/static/*.js` | přepínač, Nastavení → Firmy, přehled přístupů, sdílení do podkategorie |
| `tools/firma.py` | `--firma`, `HUB_VAULT=firma:<zkratka>` |

## Testování

Mac bwrap neumí, takže se ověřuje na Linuxu: **testovací brána vedle ostré**
na tomtéž serveru, pod **vlastním systémovým účtem** (`hubtest`), vlastní
složkou a portem, bez nginx a bez Funnelu — jen přes Tailscale. Vlastní účet
je nutný: brána při startu zastaví všechny prostory, o kterých neví
(`stop_orphans`), a pod týmž účtem `hub` by shodila ostré prostory.

Ověřeno 15. 10. 2026 na testovací bráně (Ubuntu 26.04 arm64, stránky 16K,
bwrap) — výpis přímo v jmenném prostoru sandboxu (`nsenter -m`), ne jen v hubu:

| | výsledek |
|---|---|
| člen s přístupem `Pytlík` + `Pytlík/Web` vidí obecné věci, brief klienta a Web | ✅ |
| Finance, Marketing a jiný klient v sandboxu nejsou (ani `ls`, ani `cat`) | ✅ `No such file` |
| trezor firmy jen ke čtení, registr firem v sandboxu není | ✅ `Read-only file system` |
| klient a podkategorie založené za běhu se členovi neukážou | ✅ |
| zápis do Webu (návrh → Nahrát) projde a v prostoru je vidět hned | ✅ |
| zápis do Finance odmítnut | ✅ |
| nový přístup → „přístupy se změnily“ → po restartu vidí i Marketing | ✅ |
| zavřený klient → nečinný prostor zastaven hned | ✅ |
| host nezapisuje, člen bez „smí mazat“ nemaže, generál vidí vše | ✅ |
| napojení a chat sdílené podkategorii: má jen ten, kdo do ní vidí (tajemství nikdo) | ✅ |
| chat uložený do podkategorie → „Pokračovat u sebe“ ho zapíše do prostoru člena | ✅ |
| dnešní firma beze změny (role z přístupů, trezor v prostoru jako dřív) | ✅ |

Logika je navíc pokrytá zkouškou nanečisto (52 kontrol, bez sandboxu) — i to, že
odebráním přístupu napojení zmizí u další zprávy a že `Klienti/` v dnešní firmě
se neskrývá, dokud to generál nezapne.
