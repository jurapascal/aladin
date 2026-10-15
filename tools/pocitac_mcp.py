#!/usr/bin/env python3
"""
MCP server „pocitac" — Claude v prostoru na serveru sahá na počítač uživatele.

Běží v prostoru (v sandboxu) jako stdio MCP server Claude Code. Každý nástroj
pošle úkol bráně (`$HUB_POCITAC_URL/gw/pocitac/volani` se žetonem
`$HUB_POCITAC_TOKEN`, obojí dala prostoru brána při startu) a ta ho předá hubu
na počítači uživatele (`hub/pocitac.py`). Tady se jen skládá dotaz a z odpovědi
text pro Clauda — co smí, hlídá počítač.

Zaregistruje ho hub v prostoru sám při startu (`hub/pocitac.py`, register_mcp).
Jen standardní knihovna: v sandboxu je systémový python a nic víc.

Protokol: JSON-RPC 2.0 po řádcích na stdin/stdout. Volání nástrojů běží každé
ve vlastním vlákně — dlouhý příkaz na počítači nesmí blokovat čtení souboru,
o které si Claude řekl souběžně.
"""
import base64
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request

NAME = "pocitac"
VERSION = "1.3.0"
UKOLY_OD = "2.15.0"                   # appka na počítači, která úkoly na později umí
MAX_TRANSFER = 15 * 1024 * 1024       # stejný strop jako na počítači
MAX_TEXT = 80 * 1024                  # víc textu naráz Claudovi nepomůže

INSTRUCTIONS = """\
Nástroje mcp__pocitac__* sahají na **počítač uživatele** — ne na server, na kterém
běžíš. Běžné nástroje (Read, Write, Edit, Bash…) dál pracují tady na serveru
v prostoru uživatele.

- Když uživatel mluví o souborech, složkách nebo programech „na počítači", „u mě",
  „na ploše", „ve Stažených" a podobně, použij tyhle nástroje. Nejdřív zavolej
  `pocitace`: dozvíš se systém (cesty na Windows jsou C:\\Users\\…), domovskou
  složku, shell a jaký přístup uživatel povolil.
- Relativní cesty a `~` se na počítači berou od domovské složky uživatele.
- Soubory mezi počítačem a serverem kopíruj přes `stahnout` (počítač → server)
  a `nahrat` (server → počítač).
- Počítač patří uživateli: mazání, přepisování, instalace a jiné změny tam dělej
  jen na jeho výslovné přání. Hesla, klíče a přihlašovací údaje z počítače
  nevypisuj ani nekopíruj na server, pokud o to výslovně nepožádá.
- Přístup má tři stupně: jen čtení, „ptá se na souhlas“ a plný. Při „ptá se na souhlas“
  se zápis, spuštění příkazu, snímek obrazovky, schránka a otevírání souborů provede až
  poté, co uživatel v aplikaci (na počítači nebo tady v Nastavení → Účet → Tvůj počítač) klikne
  Povolit; čeká se na to až dvě minuty. Když to uživatel zamítne nebo nestihne, nic se
  nestalo — nepokoušej se to obejít, zeptej se ho. Souhlas „na 30 minut“ platí pro všechny
  tyhle úkoly najednou. Před větším zásahem radši řekni, co chceš udělat, ať ví, k čemu
  souhlas dává.
- Snímek obrazovky (`snimek_obrazovky`) a schránka ukazují, co má uživatel právě před sebou
  — používej je jen tehdy, když to opravdu potřebuješ, a nic z toho nepřepisuj dál,
  než je nutné.
- Když počítač není připojený, řekni uživateli, ať má na počítači otevřenou appku
  Paluba a v Nastavení → Účet zapnutý přístup pro Clauda ze serveru.
- Běžící sessions (taby s Claude Code) na počítači: `sessions` je vypíše, `session_cist`
  ukáže, co se v nich děje (a `dal` vrátí jen novinky), `session_napsat` jim pošle zprávu.
  Do cizí rozdělané práce piš jen na přání uživatele.
- Když připojený není a má se na něm něco udělat, nemusí se čekat: co jde, připrav
  tady na serveru a nech počítači úkol přes `nechat_ukol`. Až se počítač připojí,
  otevře se na něm tab s Claude Code, který úkol podle tvého zadání dodělá. Jak
  jsou úkoly daleko, ukáže `ukoly_pro_pocitac`."""

_PC = {"type": "string",
       "description": "Na kterém počítači (jméno z `pocitace`). Stačí vynechat, když je připojený jen jeden."}

TOOLS = [
    {"name": "sessions",
     "description": "Běžící sessions (taby s Claude Code) na počítači uživatele: číslo tabu, název, "
                    "složka a jestli Claude právě pracuje. Zavolej, než do nějaké session "
                    "nahlédneš nebo jí něco napíšeš.",
     "inputSchema": {"type": "object", "properties": {"pocitac": _PC}}},
    {"name": "session_cist",
     "description": "Přečte konverzaci běžící session na počítači (poslední zprávy, kroky nástrojů "
                    "a jestli Claude právě pracuje). `dal` = offset z minulého čtení, vrátí jen "
                    "novinky — tak session sleduješ.",
     "inputSchema": {"type": "object", "required": ["tab"], "properties": {
         "tab": {"type": "integer", "description": "Číslo tabu ze `sessions`."},
         "poslednich": {"type": "integer", "description": "Kolik posledních bloků (výchozí 30)."},
         "dal": {"type": "integer", "description": "Offset `next` z minulého čtení."},
         "pocitac": _PC}}},
    {"name": "session_napsat",
     "description": "Napíše zprávu do běžící session na počítači, jako by ji uživatel napsal do "
                    "tabu (zpráva dostane předponu [ze serveru]). Session ji vezme, až dokončí "
                    "rozdělaný krok. Odpověď si přečti přes `session_cist`. Při „ptá se na "
                    "souhlas“ čeká na kliknutí uživatele.",
     "inputSchema": {"type": "object", "required": ["tab", "text"], "properties": {
         "tab": {"type": "integer"},
         "text": {"type": "string"},
         "pocitac": _PC}}},
    {"name": "pocitace",
     "description": "Počítače uživatele připojené k prostoru: jméno, systém, domovská složka, "
                    "shell a povolený přístup (jen čtení / plný). Zavolej, než s počítačem "
                    "začneš pracovat.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "slozka",
     "description": "Vypíše obsah složky na počítači uživatele (ne na serveru).",
     "inputSchema": {"type": "object", "properties": {
         "cesta": {"type": "string", "description": "Složka; prázdné = domovská složka."},
         "pocitac": _PC}}},
    {"name": "precist",
     "description": "Přečte textový soubor z počítače uživatele. Vrací řádky s čísly "
                    "(jako cat -n). Dlouhý soubor čti po částech přes `od` a `pocet`.",
     "inputSchema": {"type": "object", "required": ["cesta"], "properties": {
         "cesta": {"type": "string"},
         "od": {"type": "integer", "description": "První řádek, od 1."},
         "pocet": {"type": "integer", "description": "Kolik řádků (výchozí 2000)."},
         "pocitac": _PC}}},
    {"name": "hledat",
     "description": "Hledá na počítači uživatele soubory podle jména (glob, např. *.pdf nebo "
                    "src/*.py) a volitelně podle obsahu (regulární výraz). Přeskakuje .git, "
                    "node_modules a podobné složky.",
     "inputSchema": {"type": "object", "properties": {
         "cesta": {"type": "string", "description": "Kde hledat; prázdné = domovská složka."},
         "jmeno": {"type": "string", "description": "Glob na jméno souboru, výchozí *."},
         "text": {"type": "string", "description": "Regulární výraz, který má být v obsahu."},
         "bez_velikosti": {"type": "boolean", "description": "Nerozlišovat velká a malá písmena."},
         "limit": {"type": "integer", "description": "Nejvýš kolik výsledků (výchozí 100)."},
         "pocitac": _PC}}},
    {"name": "zapsat",
     "description": "Zapíše textový soubor na počítač uživatele — přepíše celý obsah, chybějící "
                    "složky založí. Potřebuje plný přístup.",
     "inputSchema": {"type": "object", "required": ["cesta", "obsah"], "properties": {
         "cesta": {"type": "string"},
         "obsah": {"type": "string"},
         "pocitac": _PC}}},
    {"name": "upravit",
     "description": "Nahradí přesný text v souboru na počítači uživatele. `stary` musí být "
                    "v souboru právě jednou (přidej okolní řádky), nebo nastav `vsechny`. "
                    "Potřebuje plný přístup.",
     "inputSchema": {"type": "object", "required": ["cesta", "stary", "novy"], "properties": {
         "cesta": {"type": "string"},
         "stary": {"type": "string"},
         "novy": {"type": "string"},
         "vsechny": {"type": "boolean", "description": "Nahradit všechny výskyty."},
         "pocitac": _PC}}},
    {"name": "spustit",
     "description": "Spustí příkaz v shellu na počítači uživatele (bash; na Windows Git Bash, "
                    "když ho nemá, cmd) a vrátí výstup a návratový kód. Bez interaktivního "
                    "vstupu. Potřebuje plný přístup.",
     "inputSchema": {"type": "object", "required": ["prikaz"], "properties": {
         "prikaz": {"type": "string"},
         "slozka": {"type": "string", "description": "Pracovní složka; prázdné = domovská."},
         "limit_s": {"type": "integer", "description": "Časový limit v sekundách (výchozí 120, nejvýš 600)."},
         "pocitac": _PC}}},
    {"name": "stahnout",
     "description": "Zkopíruje soubor (i binární, do 15 MB) z počítače uživatele sem na server.",
     "inputSchema": {"type": "object", "required": ["cesta_na_pocitaci", "cesta_na_serveru"],
                     "properties": {
         "cesta_na_pocitaci": {"type": "string"},
         "cesta_na_serveru": {"type": "string",
                              "description": "Kam soubor uložit na serveru; relativní = od pracovní složky."},
         "pocitac": _PC}}},
    {"name": "nechat_ukol",
     "description": "Nechá úkol pro Clauda na počítači uživatele, který teď není připojený "
                    "(nebo když to má počkat). Až se počítač připojí — appka Paluba "
                    "otevřená a přístup pro Clauda ze serveru zapnutý — otevře se na něm tab "
                    "s Claude Code, dostane tvoje zadání a přílohy a úkol dodělá. Zadání piš "
                    "samostatně: Claude na počítači tuhle konverzaci nevidí, ví jen to, co "
                    "napíšeš (co udělat, kde, s čím a jak poznat, že je hotovo). Co jde "
                    "připravit tady na serveru, připrav předem a přilož.",
     "inputSchema": {"type": "object", "required": ["nazev", "zadani"], "properties": {
         "nazev": {"type": "string",
                   "description": "Krátký název, jak ho uživatel uvidí, např. „Web květinářství na plochu\"."},
         "zadani": {"type": "string",
                    "description": "Celé zadání pro Clauda na počítači, nejvýš 8000 znaků."},
         "slozka": {"type": "string",
                    "description": "Složka na počítači, ve které se tab otevře (např. ~/Desktop/projekt). "
                                   "Prázdné = domovská složka."},
         "soubory": {"type": "array", "items": {"type": "string"},
                     "description": "Soubory tady na serveru, které se k úkolu přiloží (dohromady do "
                                    "15 MB, nejvýš 20). Claude na počítači se dozví, kde je najde."},
         "pocitac": {"type": "string",
                     "description": "Pro který počítač (jméno z `pocitace`). Prázdné = první, který se připojí."}}}},
    {"name": "ukoly_pro_pocitac",
     "description": "Úkoly, které čekají na počítač uživatele nebo si je už převzal (nechat_ukol): "
                    "název, stav, kdy a na kterém počítači.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "zrusit_ukol",
     "description": "Zruší úkol, který si počítač ještě nevyzvedl.",
     "inputSchema": {"type": "object", "required": ["id"], "properties": {
         "id": {"type": "string", "description": "Id úkolu z `ukoly_pro_pocitac`."}}}},
    {"name": "snimek_obrazovky",
     "description": "Snímek celé obrazovky počítače uživatele jako obrázek. Ukáže, co má uživatel "
                    "před sebou — při přístupu „ptá se na souhlas“ potřebuje jeho souhlas.",
     "inputSchema": {"type": "object", "properties": {"pocitac": _PC}}},
    {"name": "schranka_cist",
     "description": "Přečte text, který má uživatel ve schránce (Ctrl+C) na počítači. Potřebuje souhlas.",
     "inputSchema": {"type": "object", "properties": {"pocitac": _PC}}},
    {"name": "schranka_zapsat",
     "description": "Zapíše text do schránky počítače uživatele, ať ho může vložit (Ctrl+V). Potřebuje souhlas.",
     "inputSchema": {"type": "object", "required": ["text"], "properties": {
         "text": {"type": "string"}, "pocitac": _PC}}},
    {"name": "otevrit",
     "description": "Otevře na počítači uživatele soubor, složku nebo adresu http(s) ve výchozí "
                    "aplikaci (prohlížeč, prohlížeč PDF, správce souborů…). Potřebuje souhlas.",
     "inputSchema": {"type": "object", "required": ["co"], "properties": {
         "co": {"type": "string", "description": "Cesta na počítači nebo adresa http(s)."},
         "pocitac": _PC}}},
    {"name": "oznameni",
     "description": "Ukáže uživateli krátké oznámení na jeho počítači (v okně appky a v systému) — "
                    "třeba že je dlouhý úkol hotový. Nic nemění, souhlas nechce.",
     "inputSchema": {"type": "object", "required": ["text"], "properties": {
         "text": {"type": "string", "description": "Nejvýš pár vět."},
         "nadpis": {"type": "string"}, "pocitac": _PC}}},
    {"name": "procesy",
     "description": "Seznam běžících programů na počítači uživatele (nejvíc vytěžující první). "
                    "Jen čtení, souhlas nechce.",
     "inputSchema": {"type": "object", "properties": {
         "jmeno": {"type": "string", "description": "Jen procesy, které toto jméno obsahují."},
         "limit": {"type": "integer", "description": "Nejvýš kolik řádků (výchozí 60)."},
         "pocitac": _PC}}},
    {"name": "nahrat",
     "description": "Zkopíruje soubor (i binární, do 15 MB) ze serveru na počítač uživatele. "
                    "Potřebuje plný přístup.",
     "inputSchema": {"type": "object", "required": ["cesta_na_serveru", "cesta_na_pocitaci"],
                     "properties": {
         "cesta_na_serveru": {"type": "string"},
         "cesta_na_pocitaci": {"type": "string"},
         "pocitac": _PC}}},
]


class Failed(Exception):
    """Chyba, kterou dostane Claude jako výsledek nástroje."""


# ── brána ────────────────────────────────────────────────────────────────────
# Brána je na loopbacku stroje; proxy z prostředí by dotaz poslala ven.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def gateway(op, args=None, computer="", wait=150):
    url = os.environ.get("HUB_POCITAC_URL", "").rstrip("/")
    token = os.environ.get("HUB_POCITAC_TOKEN", "")
    if not url or not token:
        raise Failed("Most na počítač tu není — prostor ho od brány nedostal. "
                     "Běží tenhle Claude opravdu v prostoru na serveru s Code Hubem?")
    if op not in ("list", "ukol-novy", "ukol-zrusit"):
        wait += 130              # při „ptá se na souhlas“ brána čeká i na rozhodnutí člověka
    body = json.dumps({"op": op, "args": args or {}, "computer": computer or ""}).encode("utf-8")
    req = urllib.request.Request(url + "/gw/pocitac/volani", data=body, method="POST",
                                 headers={"Content-Type": "application/json",
                                          "X-Hub-Pocitac": token})
    try:
        with _OPENER.open(req, timeout=wait) as res:
            data = json.loads(res.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        try:
            data = json.loads(exc.read().decode("utf-8") or "{}")
        except ValueError:
            data = {}
        raise Failed(data.get("error") or f"Brána odpověděla {exc.code}.") from None
    except (urllib.error.URLError, OSError) as exc:
        raise Failed(f"Brána neodpovídá: {getattr(exc, 'reason', exc)}") from None
    except ValueError:
        raise Failed("Brána poslala nečitelnou odpověď.") from None
    if not data.get("ok"):
        raise Failed(data.get("error") or "Nepovedlo se.")
    return data


def on_pc(op, args, computer, wait=150):
    data = gateway(op, args, computer, wait)
    return data.get("result") or {}, data.get("computer") or "počítač"


# ── nástroje ─────────────────────────────────────────────────────────────────
def _size(n):
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return ""


def _stamp(ts):
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(int(ts)))
    except (TypeError, ValueError, OverflowError):
        return ""


def _server_path(raw):
    path = os.path.expanduser(str(raw or "").strip())
    if not path:
        raise Failed("Chybí cesta na serveru.")
    return os.path.abspath(path)


def t_pocitace(a):
    data = gateway("list", wait=20)
    comps = data.get("computers") or []
    online = [c for c in comps if c.get("online") and c.get("access") in ("cteni", "ptat", "vse")]
    waiting = sum(1 for u in data.get("ukoly") or [] if u.get("state") == "ceka")
    later = f"Na počítač čeká úkolů na později: {waiting} (ukoly_pro_pocitac)." if waiting else ""
    if not online:
        return ("Žádný počítač uživatele teď není připojený. Musí mít na počítači otevřenou "
                "appku Paluba přihlášenou k tomuhle serveru a v Nastavení → Účet "
                "zapnutý přístup „Claude ze serveru na tomhle počítači\". Když to může počkat, "
                "nech počítači úkol (nechat_ukol). " + later).strip()
    lines = []
    for c in online:
        lines.append(f"- {c.get('name')}: {c.get('system') or '?'}, uživatel {c.get('user') or '?'}, "
                     f"domovská složka {c.get('home') or '?'}, shell {c.get('shell') or '?'}, "
                     f"přístup: {c.get('access_label')}")
    return "Připojené počítače:\n" + "\n".join(lines + ([later] if later else []))


def t_slozka(a):
    res, pc = on_pc("ls", {"path": a.get("cesta") or ""}, a.get("pocitac"))
    entries = res.get("entries") or []
    out = [f"{res.get('path')} na počítači {pc} — {res.get('total', len(entries))} položek"]
    for e in entries:
        if e.get("type") == "dir":
            out.append(f"  [složka]  {e.get('name')}/")
        else:
            out.append(f"  {_size(e.get('size')):>9}  {_stamp(e.get('mtime'))}  {e.get('name')}"
                       + ("  (odkaz)" if e.get("link") else ""))
    if res.get("total", 0) > len(entries):
        out.append(f"  … a dalších {res['total'] - len(entries)} (zužuj přes `hledat`)")
    return "\n".join(out)


def t_precist(a):
    args = {"path": a.get("cesta") or ""}
    for src, dst in (("od", "offset"), ("pocet", "limit")):
        if a.get(src):
            args[dst] = a[src]
    res, pc = on_pc("read", args, a.get("pocitac"))
    total = res.get("lines_total")
    head = (f"{res.get('path')} na počítači {pc} — řádky {res.get('from')}–{res.get('to')}"
            + (f" z {total}" if total else ""))
    if res.get("truncated"):
        head += f" (dál pokračuj od řádku {res.get('to', 0) + 1})"
    return head + "\n" + (res.get("text") or "(prázdný soubor)")


def t_hledat(a):
    args = {"path": a.get("cesta") or "", "name": a.get("jmeno") or "*",
            "text": a.get("text") or "", "ignore_case": a.get("bez_velikosti") is True}
    if a.get("limit"):
        args["limit"] = a["limit"]
    res, pc = on_pc("find", args, a.get("pocitac"), wait=150)
    matches = res.get("matches") or []
    if not matches:
        return f"Na počítači {pc} v {res.get('path')} nic nenalezeno."
    out = [f"Na počítači {pc} v {res.get('path')} — {len(matches)} výsledků"
           + (" (neúplné, zužuj hledání)" if res.get("truncated") else "")]
    for m in matches:
        if m.get("line"):
            out.append(f"{m.get('path')}:{m.get('line')}: {m.get('text', '')}")
        else:
            out.append(str(m.get("path")) + ("/" if m.get("type") == "dir" else ""))
    return "\n".join(out)


def t_zapsat(a):
    if not isinstance(a.get("obsah"), str):
        raise Failed("Chybí obsah.")
    res, pc = on_pc("write", {"path": a.get("cesta") or "", "content": a["obsah"]}, a.get("pocitac"))
    kind = "založen" if res.get("created") else "přepsán"
    return f"Soubor {res.get('path')} na počítači {pc} {kind} ({_size(res.get('bytes'))})."


def t_upravit(a):
    res, pc = on_pc("edit", {"path": a.get("cesta") or "", "old": a.get("stary"),
                             "new": a.get("novy"), "all": a.get("vsechny") is True},
                    a.get("pocitac"))
    return f"Upraveno {res.get('path')} na počítači {pc} — nahrazeno výskytů: {res.get('replaced')}."


def t_spustit(a):
    try:
        limit = int(a.get("limit_s") or 120)
    except (TypeError, ValueError):
        limit = 120
    limit = max(1, min(600, limit))
    res, pc = on_pc("run", {"command": a.get("prikaz") or "", "cwd": a.get("slozka") or "",
                            "timeout": limit}, a.get("pocitac"), wait=limit + 60)
    code = res.get("exit_code")
    head = (f"Na počítači {pc} ({res.get('shell')}, {res.get('cwd')}): "
            + ("PŘERUŠENO po limitu " + str(limit) + " s" if res.get("timed_out")
               else f"návratový kód {code}")
            + f", {res.get('seconds')} s")
    out = [head]
    if res.get("stdout"):
        out += ["--- výstup ---", res["stdout"].rstrip("\n")]
    if res.get("stderr"):
        out += ["--- chybový výstup ---", res["stderr"].rstrip("\n")]
    if not res.get("stdout") and not res.get("stderr"):
        out.append("(bez výstupu)")
    if res.get("note"):
        out.append(res["note"])
    return "\n".join(out)


def t_stahnout(a):
    target = _server_path(a.get("cesta_na_serveru"))
    res, pc = on_pc("download", {"path": a.get("cesta_na_pocitaci") or ""}, a.get("pocitac"),
                    wait=300)
    try:
        raw = base64.b64decode(res.get("data") or "", validate=True)
    except ValueError:
        raise Failed("Počítač poslal poškozená data.") from None
    if os.path.isdir(target):
        target = os.path.join(target, os.path.basename(str(res.get("path") or "soubor")))
    os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
    tmp = f"{target}.pocitac-{os.getpid()}-{threading.get_ident()}"
    with open(tmp, "wb") as fh:
        fh.write(raw)
    os.replace(tmp, target)
    return f"Staženo z počítače {pc}: {res.get('path')} → {target} na serveru ({_size(len(raw))})."


def t_nahrat(a):
    source = _server_path(a.get("cesta_na_serveru"))
    if not os.path.isfile(source):
        raise Failed(f"Na serveru soubor {source} není.")
    size = os.path.getsize(source)
    if size > MAX_TRANSFER:
        raise Failed(f"Soubor má {_size(size)}, najednou jde nahrát nejvýš {_size(MAX_TRANSFER)}.")
    with open(source, "rb") as fh:
        data = base64.b64encode(fh.read()).decode("ascii")
    res, pc = on_pc("upload", {"path": a.get("cesta_na_pocitaci") or "", "data": data,
                               "name": os.path.basename(source)}, a.get("pocitac"), wait=300)
    return f"Nahráno na počítač {pc}: {source} → {res.get('path')} ({_size(res.get('bytes'))})."


def _older(version, than):
    """Je verze appky starší než `than`? Neznámá verze se bere jako starší."""
    def num(v):
        return [int(p) if p.isdigit() else 0 for p in str(v or "").split(".")[:3]]
    return not version or num(version) < num(than)


def t_nechat_ukol(a):
    paths = a.get("soubory") or []
    if not isinstance(paths, list):
        raise Failed("`soubory` má být seznam cest na serveru.")
    files, total = [], 0
    for raw in paths:
        source = _server_path(raw)
        if not os.path.isfile(source):
            raise Failed(f"Na serveru soubor {source} není.")
        total += os.path.getsize(source)
        if total > MAX_TRANSFER:
            raise Failed(f"Přílohy mají dohromady přes {_size(MAX_TRANSFER)} — zabal je, nebo "
                         "přilož jen to podstatné.")
        with open(source, "rb") as fh:
            files.append({"name": os.path.basename(source),
                          "data": base64.b64encode(fh.read()).decode("ascii")})
    data = gateway("ukol-novy", {"title": a.get("nazev") or "", "text": a.get("zadani") or "",
                                 "folder": a.get("slozka") or "", "computer": a.get("pocitac") or "",
                                 "files": files}, wait=120)
    ukol = data.get("ukol") or {}
    out = [f"Úkol „{ukol.get('title')}\" (id {ukol.get('id')}) čeká na počítač"
           + (f" {ukol['computer']}" if ukol.get("computer") else "") + "."]
    try:
        comps = gateway("list", wait=20).get("computers") or []
    except Failed:
        comps = []
    target = str(ukol.get("computer") or "").casefold()
    mine = [c for c in comps if not target or target in (str(c.get("name")).casefold(),
                                                         str(c.get("id")).casefold())]
    online = [c for c in mine if c.get("online") and c.get("access") in ("cteni", "ptat", "vse")]
    if online:
        out.append(f"Počítač {online[0].get('name')} je připojený — vyzvedne si ho hned.")
    else:
        out.append("Až se počítač připojí (appka Paluba otevřená, přístup pro Clauda "
                   "ze serveru zapnutý), otevře se na něm tab s Claude Code a úkol dodělá. "
                   "S plným přístupem se spustí sám, s přístupem jen ke čtení ho uživatel "
                   "na počítači potvrdí.")
    old = [c for c in mine if _older(c.get("version"), UKOLY_OD)]
    if old:
        out.append("Pozor: appka na počítači " + ", ".join(str(c.get("name")) for c in old)
                   + f" je starší než {UKOLY_OD} a úkoly na později ještě neumí. Řekni "
                   "uživateli, ať ji aktualizuje (Nastavení → Aktualizace).")
    return "\n".join(out)


def t_ukoly(a):
    items = gateway("list", wait=20).get("ukoly") or []
    if not items:
        return "Žádné úkoly na později tu nejsou."
    lines = []
    for u in items:
        line = f"- {u.get('id')} · „{u.get('title')}\" — {u.get('state_label')}"
        if u.get("by"):
            line += f" ({u['by']}, {_stamp(u.get('changed'))})"
        line += f"; zadáno {_stamp(u.get('created'))}"
        if u.get("computer"):
            line += f"; pro počítač {u['computer']}"
        if u.get("files"):
            line += "; přílohy: " + ", ".join(u["files"])
        lines.append(line)
    return "Úkoly na později (nejnovější první):\n" + "\n".join(lines)


def t_zrusit_ukol(a):
    ukol = gateway("ukol-zrusit", {"id": a.get("id") or ""}, wait=20).get("ukol") or {}
    return f"Úkol „{ukol.get('title')}\" je zrušený, na počítač už nepřijde."


def t_snimek(a):
    res, pc = on_pc("screenshot", {}, a.get("pocitac"), wait=300)
    if not res.get("data"):
        raise Failed("Počítač snímek neposlal.")
    return [{"type": "text", "text": f"Obrazovka počítače {pc} ({_size(res.get('bytes'))})."},
            {"type": "image", "data": res["data"], "mimeType": res.get("mime") or "image/png"}]


def t_schranka_cist(a):
    res, pc = on_pc("clipboard_get", {}, a.get("pocitac"))
    text = res.get("text") or ""
    return (f"Schránka počítače {pc}" + (" (zkráceno)" if res.get("truncated") else "") + ":\n"
            + (text if text else "(prázdná)"))


def t_schranka_zapsat(a):
    if not isinstance(a.get("text"), str):
        raise Failed("Chybí text.")
    res, pc = on_pc("clipboard_set", {"text": a["text"]}, a.get("pocitac"))
    return f"Do schránky počítače {pc} zapsáno {res.get('chars')} znaků."


def t_otevrit(a):
    res, pc = on_pc("open", {"target": a.get("co") or ""}, a.get("pocitac"))
    return f"Na počítači {pc} otevřeno: {res.get('opened')}."


def t_oznameni(a):
    on_pc("notify", {"message": a.get("text") or "", "title": a.get("nadpis") or "Claude"}, a.get("pocitac"))
    return "Oznámení je na počítači zobrazené."


def t_procesy(a):
    args = {"name": a.get("jmeno") or ""}
    if a.get("limit"):
        args["limit"] = a["limit"]
    res, pc = on_pc("ps", args, a.get("pocitac"))
    return (f"Procesy na počítači {pc} — {res.get('total')} " + ("(zkráceno)" if res.get("truncated") else "")
            + "\n" + (res.get("text") or "(nic)"))


def t_sessions(a):
    res, pc = on_pc("sessions", {}, a.get("pocitac"))
    rows = res.get("sessions") or []
    if not rows:
        return f"Na počítači {pc} neběží žádná session s Claude Code."
    return f"Sessions na počítači {pc}:\n" + "\n".join(
        f"- tab {r['tab']}: {r['title']} ({r['path']})"
        + (" — PRACUJE" if r.get("working") else "") + (" — skončila" if r.get("exited") else "")
        for r in rows)


def t_session_cist(a):
    args = {"tab": a.get("tab")}
    if a.get("poslednich"):
        args["last"] = a["poslednich"]
    if a.get("dal"):
        args["from"] = a["dal"]
    res, pc = on_pc("session_read", args, a.get("pocitac"))
    state = "PRACUJE" if res.get("working") else ("skončila" if res.get("exited") else "čeká na zprávu")
    return (f"Tab {res.get('tab')} „{res.get('title')}“ na {pc} — {state}. next={res.get('next')}\n\n"
            + (res.get("text") or "(zatím nic)"))


def t_session_napsat(a):
    on_pc("session_send", {"tab": a.get("tab"), "text": a.get("text") or ""}, a.get("pocitac"))
    return "Zpráva je v session. Odpověď sleduj přes `session_cist`."


HANDLERS = {"sessions": t_sessions, "session_cist": t_session_cist,
            "session_napsat": t_session_napsat, "pocitace": t_pocitace, "slozka": t_slozka, "precist": t_precist,
            "hledat": t_hledat, "zapsat": t_zapsat, "upravit": t_upravit,
            "spustit": t_spustit, "stahnout": t_stahnout, "nahrat": t_nahrat,
            "nechat_ukol": t_nechat_ukol, "ukoly_pro_pocitac": t_ukoly,
            "zrusit_ukol": t_zrusit_ukol, "snimek_obrazovky": t_snimek,
            "schranka_cist": t_schranka_cist, "schranka_zapsat": t_schranka_zapsat,
            "otevrit": t_otevrit, "oznameni": t_oznameni, "procesy": t_procesy}


# ── MCP po stdio ─────────────────────────────────────────────────────────────
_OUT = threading.Lock()


def send(msg):
    line = json.dumps(msg, ensure_ascii=False)
    with _OUT:
        sys.stdout.write(line + "\n")
        sys.stdout.flush()


def call_tool(rid, params):
    name = params.get("name")
    args = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
    handler = HANDLERS.get(name)
    if not handler:
        return send({"jsonrpc": "2.0", "id": rid,
                     "error": {"code": -32602, "message": f"Neznámý nástroj {name}"}})
    try:
        out, error = handler(args), False
    except Failed as exc:
        out, error = str(exc), True
    except Exception as exc:                      # chyba tady, ne na počítači
        out, error = f"Nástroj {name} selhal: {exc}", True
    if isinstance(out, list):                     # hotové bloky (text + obrázek)
        content = out
    else:
        text = out
        if len(text) > MAX_TEXT:
            text = text[:MAX_TEXT] + "\n… (zkráceno — požádej o menší část)"
        content = [{"type": "text", "text": text}]
    send({"jsonrpc": "2.0", "id": rid, "result": {"content": content, "isError": error}})


def handle(msg):
    method = msg.get("method")
    rid = msg.get("id")
    if method == "initialize":
        asked = (msg.get("params") or {}).get("protocolVersion") or "2025-06-18"
        return send({"jsonrpc": "2.0", "id": rid, "result": {
            "protocolVersion": asked,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": NAME, "version": VERSION},
            "instructions": INSTRUCTIONS}})
    if method == "tools/list":
        return send({"jsonrpc": "2.0", "id": rid, "result": {"tools": TOOLS}})
    if method == "tools/call":
        threading.Thread(target=call_tool, args=(rid, msg.get("params") or {}),
                         daemon=True).start()
        return None
    if method == "ping":
        return send({"jsonrpc": "2.0", "id": rid, "result": {}})
    if rid is not None:                           # notifikace se nepotvrzují
        return send({"jsonrpc": "2.0", "id": rid,
                     "error": {"code": -32601, "message": f"Metodu {method} neznám."}})
    return None


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        if isinstance(msg, dict):
            handle(msg)


if __name__ == "__main__":
    main()
