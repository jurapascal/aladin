"""
Čtení — konverzace s Claude Code jako text, ne jako terminál.

V tabu běží skutečný terminál a v něm Claude Code kreslí svoje okno. Přečíst
se to dá, ale nedá se v tom nic rozkliknout: jsou to řádky znaků, ne bloky.
Claude Code si přitom o každé konverzaci píše přepis (JSONL) a v něm je
všechno rozlišené — co řekl člověk, co odepsal Claude a co spustil za nástroj.
Tenhle modul z přepisu skládá bloky, ze kterých stránka postaví čtení:

    me       zadání člověka (`mid` = Claude si ho přečetl uprostřed práce)
    queued   zpráva poslaná během práce — stojí ve frontě, Claude ji ještě nevidí
    unqueue  zpráva z fronty odešla (nebo ji člověk vzal zpátky do pole)
    say      co Claude napsal (markdown)
    tool     volání nástroje, složené na jeden řádek (rozbalí se klepnutím)
    res      výsledek nástroje — patří k `tool` / `agent` se stejným `id`
    agent    Claude pustil agenta (Agent/Task) — kreslí se jako karta
    task     úloha na pozadí doběhla (agent i příkaz) — patří k `id`
    peer     zpráva od jiné session Claude Code

Zprávy poslané během práce Claude Code do přepisu nezapisuje jako zadání:
nejdřív je zařadí do fronty (`queue-operation` enqueue), a když si je vezme
uprostřed práce, zapíše je jako přílohu (`attachment` queued_command). Bez
toho by ve čtení druhá zpráva nebyla vůbec. Podle `key` (otisk textu) stránka
pozná, že zpráva z fronty je tatáž, která pak přišla jako zadání.

Čte se od bajtu, na kterém stránka minule skončila (`start`), takže během
práce přibývají jen nové bloky a přepis o stovkách MB se nikdy nečte celý.
Nedopsaný poslední řádek se nechává na příště — zapisuje ho Claude Code
zrovna teď.
"""
import datetime
import hashlib
import json
import os
import re
import threading
import time

from . import chats
from . import core
from . import rewind

# Kolik se toho pošle najednou. Přepis narůstá po kilobajtech, ale první
# načtení dlouhé konverzace by jinak poslalo desítky MB do prohlížeče.
MAX_BYTES = 8 * 1024 * 1024
# Od kolika se při prvním otevření čte jen konec. Výsledky nástrojů nesou
# i obrázky v base64, takže přepis naroste do stovek MB, aniž by v něm bylo
# víc konverzace — a člověk stejně kouká na to, co bylo naposledy.
TAIL_BYTES = 8 * 1024 * 1024
# Strop na jedno načtení, ať se do stránky nevysype půlden práce najednou.
MAX_BLOCKS = 400
# Strop na jeden rozbalený blok. Výpis `npm install` má klidně 200 kB a v okně
# ho stejně nikdo nečte — co se nevejde, končí třemi tečkami.
MAX_DETAIL = 4000
MAX_TEXT = 200_000
# Co agent odevzdal, je zpráva k přečtení (markdown), ne výpis — vejde se víc.
MAX_RESULT = 60_000
# Nástroje, kterými Claude pouští agenta. „Task" je staré jméno téhož.
AGENT_TOOLS = ("Agent", "Task")


def path_for(chat_id):
    """Přepis konverzace podle jejího id, nebo ''."""
    if not je_id(chat_id):
        return ""
    root = os.path.join(core.CLAUDE_DIR, "projects")
    try:
        folders = os.listdir(root)
    except OSError:
        return ""
    for folder in folders:
        path = os.path.join(root, folder, chat_id + ".jsonl")
        if os.path.isfile(path):
            return path
    return ""


def je_id(chat_id):
    """Id konverzace z prohlížeče se dosazuje do cesty — pustí se jen tvar,
    který Claude Code sám používá (UUID), nic s lomítkem ani tečkami."""
    return bool(core.SESSION_ID.fullmatch(str(chat_id or "")))


_PRIKAZ = re.compile(r"<command-name>(.*?)</command-name>", re.S)
_ARGY = re.compile(r"<command-args>(.*?)</command-args>", re.S)
# Delší vložený text Claude Code obalí značkami <pasted_content id="…">.
# Do bubliny patří jen to, co člověk vložil.
_VLOZENO = re.compile(r"</?pasted_content\b[^>]*>")
_OD_SESSION = re.compile(r"\s*<cross-session-message\b([^>]*)>(.*?)(?:</cross-session-message>|$)", re.S)
_OZNAMENI = re.compile(r"<task-notification>(.*?)</task-notification>", re.S)
# What a background agent hands back when it finishes. Claude Code queues it
# like a typed message (`queue-operation` enqueue → dequeue → meta `user`), but
# it is not from the person — its result reaches the agent card through the
# <task-notification> that follows.
_OD_AGENTA = re.compile(r"\s*(?:Another Claude session sent a message:\s*)?<agent-message\b")
# Poznámka, kterou Claude Code předřadí výsledku agenta pro Clauda („control
# tags below are neutralized…"). Člověku nic neříká, do karty nepatří.
_POZNAMKA_HARNESS = re.compile(r"^\s*\[harness:[^\n]*\]\s*")
# Výpis slash příkazu (/login, /logout, /model…). Claude Code ho zapíše jako
# zprávu od člověka, jen obalenou značkou — bez ní by ve čtení nebyl vidět.
_VYPIS = re.compile(r"<local-command-(stdout|stderr)>(.*?)</local-command-\1>", re.S)
_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")


# Přepnutí modelu a effortu (i automatické z bubliny) — do čtení jako tichý
# řádek, ne jako zpráva a výpis v rámečku.
_TICHE_PRIKAZY = ("/model", "/effort")
_NASTAVENO = re.compile(r"^Set (model|effort level) to\s+`?([^`(\n]+?)`?(?:\s+and saved|\s*\(|:|$)", re.I)


def _vypisy(text):
    """Bloky `out` z výpisů slash příkazů — co Claude Code odpověděl mimo
    konverzaci („Login successful", chyba přihlášení…)."""
    out = []
    for m in _VYPIS.finditer(text if isinstance(text, str) else ""):
        obsah = _ANSI.sub("", m.group(2)).strip()
        if not obsah:
            continue
        nastaveno = _NASTAVENO.match(obsah)
        if nastaveno and m.group(1) == "stdout":
            co = "Model" if nastaveno.group(1).lower() == "model" else "Úsilí"
            out.append({"kind": "out", "text": f"{co}: {nastaveno.group(2).strip()}",
                        "tichy": True})
            continue
        out.append({"kind": "out", "text": _zkrat(obsah, MAX_RESULT),
                    "err": m.group(1) == "stderr"})
    return out


def _text_cloveka(obsah):
    """Co člověk napsal, i s odřádkováním. chats.prompt_text je na jednořádkový
    náhled v seznamu (slepí bílé znaky a usekne na 200 znaků) — do čtení patří
    celá zpráva tak, jak ji psal."""
    if isinstance(obsah, list):
        obsah = "\n".join(cast.get("text", "") for cast in obsah
                          if isinstance(cast, dict) and cast.get("type") == "text")
    if not isinstance(obsah, str):
        return ""
    prikaz = _PRIKAZ.search(obsah)
    if prikaz:
        argy = _ARGY.search(obsah)
        return (prikaz.group(1) + " " + (argy.group(1) if argy else "")).strip()
    if _OD_SESSION.match(obsah) or _OD_AGENTA.match(obsah):
        return ""                     # zpráva od jiné session / agenta, ne od člověka
    text = _VLOZENO.sub("", chats._NOISE.sub("", obsah)).strip()
    if text.startswith(("Caveat:", "[Request interrupted")):
        return ""
    return text


def _klic(text, obrazky=()):
    """Otisk zprávy. Tatáž zpráva projde přepisem víckrát — ve frontě, při
    vyjmutí z fronty a nakonec jako zadání — a stránka podle otisku pozná,
    kterou bublinu z fronty tím sundat."""
    norm = " ".join(str(text or "").split()) + "|" + "|".join(obrazky)
    return hashlib.sha1(norm.encode("utf-8", "replace")).hexdigest()[:12]


def _zprava_cloveka(obsah):
    """Blok `me` ze zadání (text i seznam kusů s obrázky), nebo None."""
    text, cesty = _obrazky(_text_cloveka(obsah))
    vlozene = _vlozene(obsah)
    if not (text or cesty or vlozene):
        return None
    blok = {"kind": "me", "text": _zkrat(text, MAX_TEXT), "images": cesty,
            "key": _klic(text, cesty)}
    if not cesty and text.strip().split(" ", 1)[0] in _TICHE_PRIKAZY:
        blok["tichy"] = True             # potvrdí se, ale bublina se nekreslí
    if vlozene:
        blok["inline"] = vlozene
    syrovy = _text_cloveka(obsah)
    if syrovy.startswith("[hub] ") and "\n---\n" in syrovy:
        # Připomínka po restartu (hub/restart.py) nese zprávy, které restart
        # smazal z fronty, mezi řádky `---`. Stránka podle otisků pozná,
        # které bubliny „ve frontě“ tím došly.
        radky = syrovy.split("\n")
        meze = [i for i, r in enumerate(radky) if r == "---"]
        kusy = ["\n".join(radky[a + 1:b]).strip() for a, b in zip(meze, meze[1:])]
        blok["doruceno"] = [_klic(*_obrazky(k)) for k in kusy if k]
    return blok


def _znacka(text, jmeno):
    m = re.search(r"<%s>(.*?)</%s>" % (jmeno, jmeno), text, re.S)
    return m.group(1).strip() if m else ""


def _cislo(text):
    try:
        return int(str(text).strip())
    except (TypeError, ValueError):
        return None


def _oznameni(text):
    """Bloky `task` z hlášek o doběhlé úloze na pozadí. Agent v nich odevzdává
    výsledek — a podle `tool-use-id` se najde karta, která na něj čeká."""
    out = []
    for m in _OZNAMENI.finditer(text if isinstance(text, str) else ""):
        telo = m.group(1)
        # Výsledek je markdown a klidně v něm může být cokoli — bere se
        # od první otevírací značky po poslední zavírací.
        zac, kon = telo.find("<result>"), telo.rfind("</result>")
        vysledek = telo[zac + 8:kon].strip() if 0 <= zac < kon else ""
        vysledek = _POZNAMKA_HARNESS.sub("", vysledek)
        uziti = _znacka(telo, "usage")
        blok = {"kind": "task", "id": _znacka(telo, "tool-use-id"),
                "task": _znacka(telo, "task-id"),
                "status": _znacka(telo, "status"),
                "summary": _znacka(telo, "summary"),
                "result": _zkrat(vysledek, MAX_RESULT),
                "tokens": _cislo(_znacka(uziti, "subagent_tokens") or
                                 _znacka(uziti, "total_tokens")),
                "tools": _cislo(_znacka(uziti, "tool_uses")),
                "ms": _cislo(_znacka(uziti, "duration_ms"))}
        if blok["id"] or blok["task"]:
            out.append(blok)
    return out


def _zprava_od_session(text, jmeno=""):
    """Blok `peer` — jiná session Claude Code poslala zprávu (SendMessage)."""
    m = _OD_SESSION.match(text) if isinstance(text, str) else None
    if not m:
        return None
    if not jmeno:
        od = re.search(r'from-name="([^"]*)"', m.group(1))
        jmeno = od.group(1) if od else ""
    return {"kind": "peer", "from": jmeno, "text": _zkrat(m.group(2).strip(), MAX_DETAIL)}


# Obrázek, který člověk přiložil v bublině: hub ho uloží do ~/.claude/hub-images
# a Claudovi napíše jeho cestu. Ve čtení má být vidět obrázek, ne cesta.
_OBRAZEK = re.compile(r"""['"]?(%s/[^\s'"]+?\.(?:png|jpe?g|gif|webp|bmp))['"]?"""
                      % re.escape(core.IMAGE_DIR), re.I)
# Obrázek vložený přímo do Claude Code (Ctrl+V v terminálu) je v přepisu jako
# base64. Do stránky se pošle jen rozumně velký — obří by čtení zdržel.
MAX_INLINE = 2_500_000


def _obrazky(text):
    """(text bez cest k obrázkům, [cesty]) — jen obrázky, které hub opravdu
    uložil a stránka si je umí vyžádat (/api/image čte jen z hub-images)."""
    cesty = []
    for m in _OBRAZEK.finditer(text):
        if os.path.isfile(m.group(1)) and m.group(1) not in cesty:
            cesty.append(m.group(1))
    if not cesty:
        return text, []
    zbytek = _OBRAZEK.sub(" ", text)
    return re.sub(r"[ \t]{2,}", " ", zbytek).strip(), cesty


def _vlozene(obsah):
    """Obrázky vložené přímo v Claude Code jako data: URI."""
    out = []
    if not isinstance(obsah, list):
        return out
    for cast in obsah:
        if not isinstance(cast, dict) or cast.get("type") != "image":
            continue
        zdroj = cast.get("source") or {}
        data = zdroj.get("data") or ""
        if zdroj.get("type") == "base64" and data and len(data) <= MAX_INLINE:
            out.append("data:%s;base64,%s" % (zdroj.get("media_type") or "image/png", data))
    return out


def _zkrat(text, limit=MAX_DETAIL):
    text = str(text or "")
    return text if len(text) <= limit else text[:limit].rstrip() + "\n…"


def _jmeno(cesta):
    return os.path.basename(str(cesta or "").rstrip("/")) or str(cesta or "")


def _radku(text):
    return str(text or "").count("\n") + 1 if text else 0


def _do_prehledu(name, vstup):
    """Co z volání nástroje patří do panelu Průběh (cteni.js): Claudův seznam
    úkolů (TodoWrite, TaskCreate/TaskUpdate) a upravené soubory."""
    v = vstup if isinstance(vstup, dict) else {}
    if name == "TodoWrite" and isinstance(v.get("todos"), list):
        return {"todos": [{"text": str(t.get("content") or "")[:200],
                           "ted": str(t.get("activeForm") or "")[:200],
                           "stav": str(t.get("status") or "pending")}
                          for t in v["todos"][:50] if isinstance(t, dict)]}
    if name == "TaskCreate":
        return {"ukol": {"text": str(v.get("subject") or v.get("description") or "")[:200],
                         "ted": str(v.get("activeForm") or "")[:200]}}
    if name == "TaskUpdate":
        return {"ukol_zmena": {"id": str(v.get("taskId") or ""),
                               "stav": str(v.get("status") or ""),
                               "text": str(v.get("subject") or "")[:200]}}
    if name in ("Edit", "Write", "NotebookEdit") and (v.get("file_path") or v.get("notebook_path")):
        return {"soubor": str(v.get("file_path") or v.get("notebook_path"))[:500]}
    if name == "Bash":
        vystupy = vystupy_prikazu(v.get("command"))
        return {"vystupy": vystupy} if vystupy else {}
    return {}


# Soubory, které vznikají pro člověka (dokumenty, tabulky, archivy, média) —
# u příkazu se hledají v textu, protože Bash nikde neříká, co zapsal. Jen celé
# cesty (/… nebo ~/…); stránka si pak ověří, že soubor opravdu je.
VYSTUP_PRIPONY = ("pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "odt", "ods", "odp", "csv",
                  "tsv", "zip", "7z", "rar", "tgz", "tar", "gz", "mp3", "mp4", "wav", "m4a", "webm",
                  "mov", "ics", "vcf", "epub", "rtf", "stl", "3mf", "gcode", "glb")
_VYSTUP_RE = re.compile(r"(?<![\w.~/-])((?:~|/)[^\s'\"`<>|;&(){}*?$]*\.(?:" + "|".join(VYSTUP_PRIPONY)
                        + r"))(?![\w.])", re.IGNORECASE)


def vystupy_prikazu(prikaz):
    """Celé cesty k výstupním souborům zmíněné v příkazu (nejvýš 10)."""
    out = []
    for m in _VYSTUP_RE.finditer(str(prikaz or "")[:20000]):
        cesta = m.group(1)[:500]
        if cesta not in out and len(cesta) > 2:
            out.append(cesta)
        if len(out) >= 10:
            break
    return out


def soubory_chatu(path):
    """Co Claude v chatu zapsal nebo upravil (Edit/Write) a výstupy příkazů —
    víc se ze sdíleného chatu stáhnout nedá (gateway/server.py /gw/relace/soubor)."""
    out = set()
    try:
        fh = open(path, "rb")
    except OSError:
        return out
    with fh:
        for line in fh:
            if b'"tool_use"' not in line or (b"file_path" not in line and b"notebook_path" not in line
                                              and b'"Bash"' not in line):
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            obsah = ((entry.get("message") or {}).get("content")
                     if isinstance(entry, dict) and isinstance(entry.get("message"), dict) else None)
            if not isinstance(obsah, list):
                continue
            for cast in obsah:
                if isinstance(cast, dict) and cast.get("type") == "tool_use":
                    p = _do_prehledu(cast.get("name"), cast.get("input"))
                    if p.get("soubor"):
                        out.add(p["soubor"])
                    out.update(p.get("vystupy") or ())
    return out


def _popis(name, vstup):
    """(nadpis, doplněk, rozbalený text) pro jedno volání nástroje.

    Nadpis je věta v minulém čase — „upravil hub.css" — protože blok se čte
    zpětně jako záznam toho, co se stalo, ne jako tlačítko.
    """
    v = vstup if isinstance(vstup, dict) else {}
    if name == "Read":
        return "přečetl " + _jmeno(v.get("file_path")), "", str(v.get("file_path") or "")
    if name in ("Edit", "NotebookEdit"):
        stare, nove = str(v.get("old_string") or ""), str(v.get("new_string") or "")
        meta = f"+{_radku(nove)} −{_radku(stare)}" if (stare or nove) else ""
        detail = ""
        if stare:
            detail += "\n".join("− " + r for r in stare.split("\n")) + "\n"
        if nove:
            detail += "\n".join("+ " + r for r in nove.split("\n"))
        return "upravil " + _jmeno(v.get("file_path")), meta, detail
    if name == "Write":
        obsah = str(v.get("content") or "")
        return ("zapsal " + _jmeno(v.get("file_path")),
                f"{_radku(obsah)} ř.", obsah)
    if name == "Bash":
        prikaz = str(v.get("command") or "")
        return (str(v.get("description") or prikaz.split("\n")[0]) or "spustil příkaz",
                "", prikaz)
    if name in ("Grep", "Glob"):
        return ("hledal " + str(v.get("pattern") or ""), "",
                json.dumps(v, ensure_ascii=False, indent=2))
    if name in AGENT_TOOLS:
        return ("pustil agenta " + str(v.get("subagent_type") or ""),
                str(v.get("description") or ""), str(v.get("prompt") or ""))
    if name in ("WebFetch", "WebSearch"):
        return ("načetl " + str(v.get("url") or v.get("query") or ""), "",
                json.dumps(v, ensure_ascii=False, indent=2))
    if name == "TodoWrite":
        ukoly = v.get("todos") if isinstance(v.get("todos"), list) else []
        return "zapsal úkoly", f"{len(ukoly)}", json.dumps(ukoly, ensure_ascii=False, indent=2)
    if name == "AskUserQuestion":
        return "zeptal se", "", json.dumps(v.get("questions"), ensure_ascii=False, indent=2)
    if str(name).startswith("mcp__"):
        cast = str(name).split("__")
        server = cast[1] if len(cast) > 2 else ""
        nastroj = cast[-1]
        return f"{server}: {nastroj}", "", json.dumps(v, ensure_ascii=False, indent=2)
    return str(name or "nástroj"), "", json.dumps(v, ensure_ascii=False, indent=2)


def _text_vysledku(obsah):
    """Výsledek nástroje jako text. Obrázek se nepřenáší — v čtení by z něj
    byl kilometr base64 a beztak se nevykreslí."""
    if isinstance(obsah, str):
        return obsah
    if isinstance(obsah, list):
        kusy = []
        for cast in obsah:
            if not isinstance(cast, dict):
                continue
            if cast.get("type") == "text":
                kusy.append(str(cast.get("text") or ""))
            elif cast.get("type") == "image":
                kusy.append("(obrázek)")
        return "\n".join(kusy)
    return ""


def _fronta(entry):
    """Bloky z `queue-operation`: co člověk poslal, zatímco Claude pracoval.

    enqueue  zpráva se zařadila do fronty — Claude ji zatím nevidí
    remove   zpráva z fronty odešla: Claude si ji vzal uprostřed práce
             (`absorbed_mid_turn`, vzápětí přijde jako příloha) a podobně
    popAll   člověk si frontu vzal zpátky do pole (Esc / šipka nahoru)
    dequeue  Claude dopracoval a zprávu si bere jako další zadání — přijde
             hned za tím jako obyčejné `user` se stejným otiskem

    Frontou chodí i hlášky o doběhlých úlohách na pozadí, zprávy od jiných
    session a to, co odevzdal agent (`<agent-message>`). Od člověka nejsou, bublinu ve frontě nedostanou — hláška
    o agentovi se ale rovnou propíše do jeho karty.
    """
    op = entry.get("operation")
    obsah = entry.get("content")
    if op == "enqueue":
        if isinstance(obsah, str) and "<task-notification>" in obsah:
            return _oznameni(obsah)
        zprava = _zprava_cloveka(obsah)
        if not zprava:
            return []
        zprava["kind"] = "queued"
        return [zprava]
    if op == "remove":
        zprava = _zprava_cloveka(obsah)
        if not zprava:
            return []
        return [{"kind": "unqueue", "key": zprava["key"],
                 "why": str(entry.get("reason") or "")}]
    if op == "popAll":
        return [{"kind": "unqueue", "all": True}]
    if op == "dequeue":
        # Hned za tím přijde zpráva z fronty jako obyčejné `user` — čtení
        # podle toho pozná, že nepřišla „odjinud“ (viz cteni.js, ztracene).
        return [{"kind": "dequeue"}]
    return []


def _priloha(entry):
    """Zpráva, kterou si Claude vzal z fronty uprostřed práce. V přepisu je
    jako příloha (`queued_command`) na místě, kde ji uviděl — a tam patří
    i ve čtení."""
    priloha = entry.get("attachment") or {}
    if priloha.get("type") != "queued_command":
        return []
    prompt = priloha.get("prompt")
    puvod = priloha.get("origin") if isinstance(priloha.get("origin"), dict) else {}
    if priloha.get("commandMode") == "task-notification" or (
            isinstance(prompt, str) and "<task-notification>" in prompt):
        return _oznameni(prompt)
    if puvod.get("kind") == "peer" or (isinstance(prompt, str) and _OD_SESSION.match(prompt)):
        blok = _zprava_od_session(prompt, str(puvod.get("name") or ""))
        return [blok] if blok else []
    if puvod.get("kind") not in (None, "human"):
        return []
    zprava = _zprava_cloveka(prompt)
    if not zprava:
        return []
    zprava["mid"] = True
    return [zprava]


def _agent_z_vysledku(entry, text):
    """Co o agentovi Claude Code zapsal k výsledku nástroje Agent."""
    info = entry.get("toolUseResult")
    if not isinstance(info, dict) or not info.get("agentId"):
        return None
    return {"agentId": str(info.get("agentId")),
            "async": bool(info.get("isAsync")),
            "status": str(info.get("status") or ""),
            "tokens": info.get("totalTokens") if isinstance(info.get("totalTokens"), int) else None,
            "tools": info.get("totalToolUseCount") if isinstance(info.get("totalToolUseCount"), int) else None,
            "ms": info.get("totalDurationMs") if isinstance(info.get("totalDurationMs"), int) else None,
            "model": str(info.get("resolvedModel") or "")}


def _bloky_zpravy(entry):
    """Bloky z jednoho řádku přepisu. Meta zprávy a podkonverzace se přeskakují
    — do čtení patří jen to, co je mezi člověkem a Claudem."""
    if entry.get("isSidechain"):
        return []
    druh = entry.get("type")
    if druh == "queue-operation":
        return _fronta(entry)
    if druh == "attachment":
        return _priloha(entry)
    if entry.get("isMeta"):
        return []
    zprava = entry.get("message") or {}
    obsah = zprava.get("content")
    out = []

    if druh == "user" and entry.get("isCompactSummary"):
        # Souhrn po zhuštění konverzace píše Claude Code jako zprávu „od
        # člověka“. Člověk ho nepsal — a čtení by podle něj usoudilo, že
        # fronta zmizela, a čekající zprávu označilo za nedoručenou.
        return [{"kind": "souhrn", "text": _zkrat(_text_cloveka(obsah) or "", MAX_TEXT)}]

    if druh == "user":
        if isinstance(obsah, str):
            if "<task-notification>" in obsah:
                out.extend(_oznameni(obsah))
            if "<local-command-" in obsah:
                out.extend(_vypisy(obsah))
            od = _zprava_od_session(obsah)
            if od:
                return out + [od]
            blok = _zprava_cloveka(obsah)
            if blok:
                out.append(blok)
            return out
        if isinstance(obsah, list):
            # Zpráva s obrázkem vloženým přímo v Claude Code: text i obrázky
            # patří do jedné bubliny, ne každý kus zvlášť.
            vysledky = [c for c in obsah if isinstance(c, dict) and c.get("type") == "tool_result"]
            if not vysledky:
                for cast in obsah:
                    if isinstance(cast, dict) and cast.get("type") == "text":
                        out.extend(_oznameni(cast.get("text")))
                blok = _zprava_cloveka(obsah)
                if blok:
                    out.append(blok)
                return out
            for cast in obsah:
                if not isinstance(cast, dict):
                    continue
                if cast.get("type") == "tool_result":
                    text = _text_vysledku(cast.get("content"))
                    blok = {"kind": "res",
                            "id": str(cast.get("tool_use_id") or ""),
                            "ok": not cast.get("is_error"),
                            "detail": _zkrat(text),
                            "radku": _radku(text)}
                    # K výsledku agenta Claude Code zapisuje, jestli běží na
                    # pozadí, a když doběhl, kolik toho udělal. Jeden výsledek
                    # na řádek, jinak by se nevědělo, ke kterému to patří.
                    agent = _agent_z_vysledku(entry, text) if len(vysledky) == 1 else None
                    if agent:
                        blok["agent"] = agent
                        blok["detail"] = "" if agent["async"] else _zkrat(
                            _POZNAMKA_HARNESS.sub("", text), MAX_RESULT)
                    out.append(blok)
                elif cast.get("type") == "text":
                    blok = _zprava_cloveka(cast.get("text"))
                    if blok:
                        out.append(blok)
        return out

    if druh == "assistant":
        if isinstance(obsah, str):
            if obsah.strip():
                out.append({"kind": "say", "text": _zkrat(obsah, MAX_TEXT)})
            return out
        if isinstance(obsah, list):
            for cast in obsah:
                if not isinstance(cast, dict):
                    continue
                if cast.get("type") == "text" and str(cast.get("text") or "").strip():
                    out.append({"kind": "say", "text": _zkrat(cast["text"], MAX_TEXT)})
                elif cast.get("type") == "tool_use" and cast.get("name") in AGENT_TOOLS:
                    v = cast.get("input") if isinstance(cast.get("input"), dict) else {}
                    out.append({"kind": "agent", "id": str(cast.get("id") or ""),
                                "type": str(v.get("subagent_type") or "general-purpose"),
                                "title": str(v.get("description") or ""),
                                "prompt": _zkrat(v.get("prompt"), MAX_RESULT),
                                "model": str(v.get("model") or ""),
                                "bg": bool(v.get("run_in_background")),
                                "ts": str(entry.get("timestamp") or "")})
                elif cast.get("type") == "tool_use":
                    nadpis, meta, detail = _popis(cast.get("name"), cast.get("input"))
                    blok = {"kind": "tool", "id": str(cast.get("id") or ""),
                            "name": str(cast.get("name") or ""),
                            "title": nadpis, "meta": meta,
                            "detail": _zkrat(detail)}
                    blok.update(_do_prehledu(cast.get("name"), cast.get("input")))
                    out.append(blok)
                # thinking se nečte: ve čtení má být to, co Claude napsal,
                # ne to, co si přitom myslel.
        return out
    return []


def navaz(stary, odkud, novy):
    """Kde v novém přepisu pokračovat, když Claude Code konverzaci přestěhoval
    (core._pokracovani): najde poslední zprávu, kterou čtení ze starého přepisu
    už mělo (před bajtem `odkud`), a vrátí pozici hned za ní v novém. Když ji
    nenajde, vrátí 0 — čtení pak začne od konce nového jako při otevření."""
    try:
        with open(stary, "rb") as fh:
            fh.seek(max(0, int(odkud) - 2 * 1024 * 1024))
            kus = fh.read(max(0, int(odkud) - fh.tell()))
    except (OSError, ValueError):
        return 0
    uuid = None
    for radek in reversed(kus.splitlines()):
        m = re.search(rb'"uuid":"([0-9a-f-]{36})"', radek)
        if m:
            uuid = m.group(0)
            break
    if not uuid:
        return 0
    try:
        with open(novy, "rb") as fh:
            data = fh.read()
    except OSError:
        return 0
    i = data.find(uuid)
    if i < 0:
        return 0
    konec = data.find(b"\n", i)
    return konec + 1 if konec >= 0 else len(data)


def read(path, start=0, tail=False):
    """Nové bloky od bajtu `start`. Vrací i offset, na kterém se skončilo.

    `tail` = první otevření: začne se u konce přepisu, ne u prvního řádku.
    """
    if not path or not os.path.isfile(path):
        return {"blocks": [], "next": 0, "ready": False}
    try:
        velikost = os.path.getsize(path)
        start = max(0, min(int(start or 0), velikost))
        preskoceno = False
        if tail and start == 0 and velikost > TAIL_BYTES:
            start = velikost - TAIL_BYTES
            preskoceno = True
        with open(path, "rb") as fh:
            fh.seek(start)
            syrove = fh.read(MAX_BYTES)
    except (OSError, ValueError):
        return {"blocks": [], "next": start, "ready": False}

    if preskoceno:
        # Doprostřed řádku se skočit nedá — první nedočtený se zahodí.
        rez = syrove.find(b"\n")
        if rez < 0:
            return {"blocks": [], "next": velikost, "ready": True}
        start += rez + 1
        syrove = syrove[rez + 1:]

    # Poslední řádek může být rozepsaný — ten se nechá na příště.
    konec = syrove.rfind(b"\n")
    if konec < 0:
        return {"blocks": [], "next": start, "ready": True}
    hotovo, syrove = syrove[:konec + 1], None

    bloky = []
    for radek in hotovo.split(b"\n"):
        radek = radek.strip()
        if not radek.startswith(b"{"):
            continue
        try:
            entry = json.loads(radek)
        except ValueError:
            continue
        if isinstance(entry, dict):
            bloky.extend(rewind.oznac(entry, _bloky_zpravy(entry), os.path.basename(path)[:-6]))
    return {"blocks": bloky[-MAX_BLOCKS:], "next": start + konec + 1, "ready": True}


# ── agenti ───────────────────────────────────────────────────────────────────
# Agent si píše vlastní přepis vedle konverzace:
#     <id konverzace>/subagents/agent-<agentId>.jsonl  (+ .meta.json)
# V meta je `toolUseId` — id volání nástroje Agent, podle kterého stránka najde
# kartu. Z přepisu se čte, kolik toho agent udělal a co dělá teď. Soubory
# rostou po celou dobu práce (klidně na MB), proto se čtou od místa, kde se
# minule skončilo, a stav se drží tady.
MAX_AG_READ = 4 * 1024 * 1024
AG_KROKU = 5                  # kolik posledních kroků agenta se posílá
_AG_STAV = {}
_AG_ZAMEK = threading.Lock()


def _ms(stamp):
    """ISO čas z přepisu → epoch v ms (0 = nevíme)."""
    try:
        return int(datetime.datetime.fromisoformat(
            str(stamp).replace("Z", "+00:00")).timestamp() * 1000)
    except (TypeError, ValueError):
        return 0


def _novy_stav():
    return {"pos": 0, "tools": 0, "last": [], "text": "", "start": 0,
            "update": 0, "tokens": 0, "model": "", "konec": False}


def _zapocti(stav, entry):
    cas = _ms(entry.get("timestamp"))
    if cas:
        stav["start"] = stav["start"] or cas
        stav["update"] = cas
    if entry.get("type") != "assistant":
        if entry.get("type") == "user":
            stav["konec"] = False          # agent pokračuje (výsledek nástroje)
        return
    zprava = entry.get("message") or {}
    stav["model"] = str(zprava.get("model") or stav["model"])
    uziti = zprava.get("usage") if isinstance(zprava.get("usage"), dict) else {}
    # Stejné číslo jako Claude Code u běžícího agenta: kolik tokenů má
    # v kontextu po poslední odpovědi.
    tokeny = sum(v for k, v in uziti.items() if isinstance(v, int) and k in (
        "input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens",
        "output_tokens"))
    if tokeny:
        stav["tokens"] = tokeny
    obsah = zprava.get("content")
    nastroj = False
    for cast in obsah if isinstance(obsah, list) else []:
        if not isinstance(cast, dict):
            continue
        if cast.get("type") == "tool_use":
            nastroj = True
            stav["tools"] += 1
            nadpis, meta, _ = _popis(cast.get("name"), cast.get("input"))
            stav["last"].append({"name": str(cast.get("name") or ""),
                                 "title": _zkrat(nadpis, 160), "meta": meta})
            del stav["last"][:-AG_KROKU]
        elif cast.get("type") == "text" and str(cast.get("text") or "").strip():
            stav["text"] = _zkrat(" ".join(str(cast["text"]).split()), 240)
    stav["konec"] = not nastroj and zprava.get("stop_reason") == "end_turn"


def _prubeh(cesta):
    """Stav jednoho agenta z jeho přepisu — dočte jen to, co přibylo."""
    try:
        velikost = os.path.getsize(cesta)
    except OSError:
        return None
    with _AG_ZAMEK:
        stav = _AG_STAV.get(cesta)
        if stav is None or velikost < stav["pos"]:
            stav = _novy_stav()
            if len(_AG_STAV) > 300:
                _AG_STAV.clear()
            _AG_STAV[cesta] = stav
        if velikost > stav["pos"]:
            try:
                with open(cesta, "rb") as fh:
                    fh.seek(stav["pos"])
                    syrove = fh.read(MAX_AG_READ)
            except OSError:
                syrove = b""
            konec = syrove.rfind(b"\n")
            if konec >= 0:
                for radek in syrove[:konec + 1].split(b"\n"):
                    radek = radek.strip()
                    if not radek.startswith(b"{"):
                        continue
                    try:
                        entry = json.loads(radek)
                    except ValueError:
                        continue
                    if isinstance(entry, dict):
                        _zapocti(stav, entry)
                stav["pos"] += konec + 1
        return {k: (list(v) if isinstance(v, list) else v)
                for k, v in stav.items() if k != "pos"}


def agenti(path):
    """Průběh všech agentů, které konverzace pustila. `now` je čas serveru —
    podle něj si stránka srovná hodiny, ať běžící čas nelže o rozdíl mezi
    počítačem a serverem."""
    ted = int(time.time() * 1000)
    if not path or not path.endswith(".jsonl"):
        return {"agents": [], "now": ted}
    slozka = os.path.join(path[:-len(".jsonl")], "subagents")
    try:
        jmena = os.listdir(slozka)
    except OSError:
        return {"agents": [], "now": ted}
    out = []
    for jmeno in sorted(jmena):
        if not (jmeno.startswith("agent-") and jmeno.endswith(".jsonl")):
            continue
        agent_id = jmeno[len("agent-"):-len(".jsonl")]
        try:
            with open(os.path.join(slozka, "agent-%s.meta.json" % agent_id),
                      encoding="utf-8") as fh:
                meta = json.load(fh)
        except (OSError, ValueError):
            meta = {}
        if not isinstance(meta, dict):
            meta = {}
        stav = _prubeh(os.path.join(slozka, jmeno))
        if stav is None:
            continue
        stav.update({"agentId": agent_id, "id": str(meta.get("toolUseId") or ""),
                     "type": str(meta.get("agentType") or ""),
                     "title": str(meta.get("description") or "")})
        out.append(stav)
    return {"agents": out, "now": ted}


# ── úlohy na pozadí ─────────────────────────────────────────────────────────
# Co Claude pustil na pozadí a ještě to neskončilo: příkaz s run_in_background,
# Monitor, agent na pozadí. Konec ohlásí <task-notification> se stejným
# tool-use-id, případně TaskStop. Úlohy jsou děti procesu Claude Code — co se
# spustilo dřív, než tenhle proces naběhl (`od`), už neběží a nepočítá se.
_BG_ID = re.compile(r"(?:resumedAgentId\"?:\s*\"|with ID|agentId|task_id)[:=]?\s*([A-Za-z0-9_-]+)")
# „Resuming agent" = SendMessage znovu rozběhl doběhlého agenta: běží zase na
# pozadí a konec ohlásí task-notification s jeho id jako task-id.
_BG_ZNAK = ("running in background", "Async agent launched", "Monitor started",
            "is now running", "monitoring", "Resuming agent")
_BG_STAV = {}
_BG_ZAMEK = threading.Lock()


def _bg_text(obsah):
    if isinstance(obsah, str):
        return obsah
    if isinstance(obsah, list):
        return " ".join(str(b.get("text") or "") for b in obsah if isinstance(b, dict))
    return ""


def _bg_konec(stav, text):
    for m in _OZNAMENI.finditer(text or ""):
        telo = m.group(1)
        tid, task = _znacka(telo, "tool-use-id"), _znacka(telo, "task-id")
        for key in [k for k, u in stav["bezi"].items() if k == tid or (task and u["task"] == task)]:
            stav["bezi"].pop(key, None)


def _bg_radek(stav, entry):
    typ = entry.get("type")
    if typ == "queue-operation":
        _bg_konec(stav, entry.get("content") or "")
        return
    if typ == "attachment":
        _bg_konec(stav, (entry.get("attachment") or {}).get("prompt") or "")
        return
    if entry.get("isSidechain"):
        return
    obsah = (entry.get("message") or {}).get("content")
    if isinstance(obsah, str):
        _bg_konec(stav, obsah)
        return
    if not isinstance(obsah, list):
        return
    for b in obsah:
        if not isinstance(b, dict):
            continue
        if b.get("type") == "text":
            _bg_konec(stav, b.get("text") or "")
        elif b.get("type") == "tool_use":
            name, vstup = b.get("name") or "", b.get("input") or {}
            if name == "TaskStop":
                cil = str(vstup.get("task_id") or vstup.get("shell_id") or "")
                for key in [k for k, u in stav["bezi"].items() if cil and u["task"] == cil]:
                    stav["bezi"].pop(key, None)
                continue
            if name == "ScheduleWakeup":
                # Naplánované probuzení: platí to poslední (další ho nahradí).
                if vstup.get("stop"):
                    stav["budik"] = None
                else:
                    od = _ms(entry.get("timestamp"))
                    try:
                        za = max(60, min(3600, int(float(vstup.get("delaySeconds") or 0))))
                    except (TypeError, ValueError):
                        za = 60
                    stav["budik"] = {"id": b.get("id"), "name": name, "task": "",
                                     "title": str(vstup.get("reason") or "naplánované pokračování"),
                                     "detail": _zkrat(str(vstup.get("prompt") or ""), 600),
                                     "od": od, "do": od + za * 1000}
                continue
            if name == "Monitor" or name == "Agent" or name == "SendMessage" or \
                    (isinstance(vstup, dict) and vstup.get("run_in_background")):
                nadpis, _meta, detail = _popis(name, vstup)
                if name == "SendMessage":
                    nadpis = "agent pokračuje: " + str(vstup.get("summary") or vstup.get("to") or "")
                    detail = str(vstup.get("message") or "")
                stav["cekaji"][b.get("id")] = {
                    "id": b.get("id"), "name": name, "title": nadpis,
                    "detail": _zkrat(detail, 600), "od": _ms(entry.get("timestamp")),
                    "task": ""}
                del_old = list(stav["cekaji"])[:-50]
                for k in del_old:
                    stav["cekaji"].pop(k, None)
        elif b.get("type") == "tool_result":
            u = stav["cekaji"].pop(b.get("tool_use_id"), None)
            text = _bg_text(b.get("content"))
            if u and not b.get("is_error") and any(z in text for z in _BG_ZNAK):
                m = _BG_ID.search(text)
                u["task"] = m.group(1) if m else ""
                stav["bezi"][u["id"]] = u
            _bg_konec(stav, text)


def pozadi(path, od_ms=0):
    """Úlohy na pozadí, které podle přepisu ještě běží (nejstarší první)."""
    if not path or not os.path.isfile(path):
        return []
    with _BG_ZAMEK:
        stav = _BG_STAV.get(path)
        try:
            size = os.path.getsize(path)
        except OSError:
            return []
        if not stav or size < stav["pos"]:
            stav = _BG_STAV[path] = {"pos": 0, "cekaji": {}, "bezi": {}, "budik": None}
        if size > stav["pos"]:
            try:
                with open(path, "rb") as fh:
                    fh.seek(stav["pos"])
                    data = fh.read(64 * 1024 * 1024)
            except OSError:
                return []
            konec = data.rfind(b"\n") + 1      # rozepsaný řádek až příště
            for line in data[:konec].splitlines():
                if b"tool_use" not in line and b"task-notification" not in line:
                    continue
                try:
                    _bg_radek(stav, json.loads(line))
                except ValueError:
                    continue
            stav["pos"] += konec
        if len(_BG_STAV) > 64:
            for k in list(_BG_STAV)[:-64]:
                _BG_STAV.pop(k, None)
        out = [dict(u) for u in stav["bezi"].values()
               if not od_ms or u["od"] >= od_ms - 5000]
        budik = stav.get("budik")
        # Probuzení se po čase dávno odbylo (zpráva přišla, Claude jede dál).
        if budik and (not od_ms or budik["od"] >= od_ms - 5000) and \
                time.time() * 1000 < budik["do"] + 90 * 1000:
            out.append(dict(budik))
        return sorted(out, key=lambda u: u["od"])
