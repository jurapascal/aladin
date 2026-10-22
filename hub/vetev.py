"""
Větvení konverzace — nová konverzace z přepisu jen do zvoleného místa.

Claude Code umí `--resume <id> --fork-session`, jenže vždy od konce. Rozvětvit
od zprávy uprostřed se proto dělá nad přepisem (JSONL v ~/.claude/projects):

    1. Najde se zpráva člověka podle `uuid` a od ní se řetězem `parentUuid`
       jde zpátky ke kořeni. Do větve patří jen ten řetěz (zpráva samotná ne —
       člověk ji napíše znovu nebo upraví), ne vedlejší větve a dohrávky.
    2. K volání nástroje v řetězu se dotáhne jeho výsledek (a naopak): u souběžných
       nástrojů je výsledek odrůdka vedle řetězu. Osiřelé `tool_use` Claude Code
       při --resume odmítne, proto se raději nevyrobí nic a řekne se proč.
    3. Záznamy se přepíšou na nové `sessionId` a uloží vedle původního souboru
       pod novým UUID. Atomicky (tmp → ověření → rename), práva 0600.

Původní přepis se jen čte, nikdy se nemění ani nemaže. Čte se streamováním
(přepisy mají desítky MB), v paměti zůstává jen rejstřík řádků.

Co se NEkopíruje: `queue-operation` (fronta by se doručila znovu),
`file-history-*` (zálohy souborů patří původní konverzaci), `api-request*`
(ladicí data, nesou celé prompty), `cost-state`, `atis-latch` a podkonverzace
agentů (soubory v <id>/subagents/). Z `mode`/`permission-mode` zůstane poslední
stav před místem větvení.
"""
import errno
import json
import os
import re
import threading
import time
import uuid as uuidlib

from . import chats
from . import core

# Nad tuhle velikost se přepis nevětví — dvouprůchodové čtení by trvalo minuty.
MAX_PREPIS = 2 * 1024 * 1024 * 1024
# Dvojklik: stejná větev ze stejného místa v téhle době se nezakládá znovu,
# vrátí se ta, která právě vznikla.
DVOJKLIK_S = 15
# Tyhle záznamy se ani nerozebírají (a nekopírují) — jsou velké a k obnovení
# konverzace nejsou potřeba. Jméno typu stojí v přepisu vždy na začátku řádku.
_PRESKOC = re.compile(rb'^\{"type":"(api-request|file-history-|queue-operation|cost-state|atis-latch|last-prompt)')

_ZAMEK = threading.Lock()
_POSLEDNI = {}            # (původní id, uuid) → (čas, výsledek)


class VetevChyba(Exception):
    """Chyba, kterou lze říct člověku česky tak, jak je."""


def _chyba_zapisu(exc, slozka):
    """OSError při zápisu → srozumitelná hláška."""
    kod = getattr(exc, "errno", None)
    if kod in (errno.ENOSPC, getattr(errno, "EDQUOT", -1)):
        return VetevChyba("Na disku není místo — větev se nevytvořila, původní konverzace je beze změny.")
    if kod in (errno.EACCES, errno.EPERM, errno.EROFS):
        return VetevChyba(f"Do složky konverzací nemůžu zapisovat ({slozka}) — zkontroluj práva.")
    return VetevChyba(f"Větev se nepodařilo uložit: {exc}")


def _nazev_vetve(nazev):
    """Název větve: „Větev: …“, i při větvení větve jen jednou."""
    nazev = str(nazev or "").strip()
    return nazev[:120] if nazev.startswith("Větev: ") else ("Větev: " + (nazev or "konverzace"))[:120]


def _je_clovek(entry):
    """Zadání napsané člověkem — ne výsledek nástroje, souhrn ani meta zpráva."""
    if entry.get("isSidechain") or entry.get("isMeta") or entry.get("isCompactSummary"):
        return False
    druh = entry.get("type")
    if druh == "attachment":
        p = entry.get("attachment")
        puvod = p.get("origin") if isinstance(p, dict) and isinstance(p.get("origin"), dict) else {}
        return (isinstance(p, dict) and p.get("type") == "queued_command"
                and p.get("commandMode") != "task-notification"
                and puvod.get("kind") in (None, "human")
                and isinstance(p.get("prompt"), (str, list)))
    if druh != "user":
        return False
    obsah = (entry.get("message") or {}).get("content")
    if isinstance(obsah, str):
        return bool(obsah.strip())
    if isinstance(obsah, list):
        casti = [c for c in obsah if isinstance(c, dict)]
        return bool(casti) and not any(c.get("type") == "tool_result" for c in casti)
    return False


def _text(entry):
    """Text zprávy člověka (na náhled a do hlášky)."""
    obsah = (entry.get("message") or {}).get("content") if entry.get("type") == "user" \
        else (entry.get("attachment") or {}).get("prompt")
    if isinstance(obsah, list):
        obsah = "\n".join(c.get("text", "") for c in obsah
                          if isinstance(c, dict) and c.get("type") == "text")
    return obsah if isinstance(obsah, str) else ""


def _nastroje(entry):
    """(id volání nástroje, id výsledků) z jednoho záznamu."""
    obsah = (entry.get("message") or {}).get("content")
    pouziti, vysledky = [], []
    if isinstance(obsah, list):
        for c in obsah:
            if not isinstance(c, dict):
                continue
            if c.get("type") == "tool_use" and c.get("id"):
                pouziti.append(str(c["id"]))
            elif c.get("type") == "tool_result" and c.get("tool_use_id"):
                vysledky.append(str(c["tool_use_id"]))
    return pouziti, vysledky


def _radky(fh, konec):
    """(offset, bajty) kompletních řádků do bajtu `konec`. Nedopsaný poslední
    řádek (bez \\n) se vynechá — Claude Code ho právě píše."""
    pos = 0
    while pos < konec:
        radek = fh.readline()
        if not radek:
            return
        if not radek.endswith(b"\n"):
            return
        if pos + len(radek) > konec:
            return
        yield pos, radek
        pos += len(radek)


def _slozka_ok(path):
    """Přepis musí ležet přímo ve složce projektu pod ~/.claude/projects."""
    koren = os.path.realpath(os.path.join(core.CLAUDE_DIR, "projects"))
    skutecna = os.path.realpath(path)
    return (os.path.dirname(os.path.dirname(skutecna)) == koren
            and re.fullmatch(core.SESSION_ID.pattern + r"\.jsonl", os.path.basename(skutecna)) is not None)


def _rejstrik(path, konec, cil):
    """První průchod: rejstřík řádků s uuid, nástroje a poslední mode/title před `cil`."""
    r = {"rodic": {}, "radky": [], "pouziti": {}, "vysledky": {}, "vysledek_uuid": {},
         "cil": None, "stav": {}}
    with open(path, "rb") as fh:
        for off, radek in _radky(fh, konec):
            if _PRESKOC.match(radek) or not radek.startswith(b"{"):
                continue
            try:
                e = json.loads(radek)
            except ValueError:
                continue                       # poškozený řádek se přeskočí
            if not isinstance(e, dict):
                continue
            u = e.get("uuid")
            if not isinstance(u, str):
                if r["cil"] is None and e.get("type") in ("mode", "permission-mode", "ai-title"):
                    r["stav"][e["type"]] = (off, len(radek))
                continue
            r["rodic"][u] = e.get("parentUuid") if isinstance(e.get("parentUuid"), str) else None
            r["radky"].append((off, len(radek), u, e.get("type")))
            pouziti, vysledky = _nastroje(e)
            if pouziti:
                r["pouziti"][u] = pouziti
            if vysledky:
                r["vysledky"][u] = vysledky
                for i in vysledky:
                    r["vysledek_uuid"].setdefault(i, []).append(u)
            if u == cil and r["cil"] is None:
                if not _je_clovek(e):
                    raise VetevChyba("Rozvětvit jde jen od zprávy, kterou jsi napsal ty.")
                r["cil"] = {"off": off, "text": _text(e), "cwd": e.get("cwd") or ""}
    return r


def _vyber(r, cil):
    """Množina uuid do větve: řetěz předků cíle + dotažené výsledky nástrojů."""
    if r["cil"] is None:
        raise VetevChyba("Tuhle zprávu jsem v přepisu nenašel — možná ji Claude Code mezitím přepsal. "
                         "Zkus konverzaci otevřít znovu.")
    vyber, u = [], r["rodic"].get(cil)
    videno = set()
    while u and u in r["rodic"] and u not in videno:
        videno.add(u)
        vyber.append(u)
        u = r["rodic"][u]
    if not vyber:
        raise VetevChyba("Před touhle zprávou ještě nic není — rozvětvovat není z čeho.")
    vyber = set(vyber)
    pouziti = {i for u in vyber for i in r["pouziti"].get(u, ())}
    vysledky = {i for u in vyber for i in r["vysledky"].get(u, ())}
    for i in pouziti - vysledky:
        # Výsledek souběžného nástroje visí vedle řetězu — dotáhne se, ale jen
        # pokud patří ke zvolenému místu (jeho rodič je ve větvi).
        for u in r["vysledek_uuid"].get(i, ()):
            if r["rodic"].get(u) in vyber:
                vyber.add(u)
                vysledky.add(i)
                break
    if pouziti - vysledky or vysledky - pouziti:
        raise VetevChyba("V tomhle místě Claude zrovna pracoval s nástrojem a výsledek chybí — "
                         "rozvětvit jde od zprávy, před kterou byla práce dokončená.")
    return vyber, pouziti


def _prepis_radek(radek, nove_id, vyber):
    """Řádek s novým sessionId; rodič mimo větev (utržený řetěz) → null."""
    e = json.loads(radek)
    for k in ("sessionId", "session_id"):
        if k in e:
            e[k] = nove_id
    if e.get("parentUuid") is not None and e["parentUuid"] not in vyber:
        e["parentUuid"] = None
    return (json.dumps(e, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


def _over(path, nove_id):
    """Ověření hotového souboru: platný JSONL, souvislý řetěz parentUuid,
    nové sessionId všude, žádný osiřelý nástroj. Vrací počet zpráv."""
    uuids, rodice, pouziti, vysledky, zprav = set(), [], set(), set(), 0
    with open(path, "rb") as fh:
        for radek in fh:
            if not radek.endswith(b"\n"):
                raise VetevChyba("Větev se zapsala nedokončená.")
            e = json.loads(radek)
            if not isinstance(e, dict):
                raise VetevChyba("Větev obsahuje neplatný řádek.")
            for k in ("sessionId", "session_id"):
                if k in e and e[k] != nove_id:
                    raise VetevChyba("Ve větvi zůstalo staré id konverzace.")
            u = e.get("uuid")
            if isinstance(u, str):
                uuids.add(u)
                if isinstance(e.get("parentUuid"), str):
                    rodice.append(e["parentUuid"])
                p, v = _nastroje(e)
                pouziti.update(p)
                vysledky.update(v)
                if e.get("type") in ("user", "assistant"):
                    zprav += 1
    if not zprav:
        raise VetevChyba("Větev by byla prázdná.")
    if any(p not in uuids for p in rodice):
        raise VetevChyba("Ve větvi se přerušil řetěz zpráv.")
    if pouziti != vysledky:
        raise VetevChyba("Ve větvi zůstal nástroj bez výsledku.")
    return zprav


def _zapis(cesta, nove_id, r, vyber, cil, nazev):
    slozka = os.path.dirname(cesta)
    konecne = os.path.join(slozka, nove_id + ".jsonl")
    tmp = konecne + ".tmp"
    if os.path.exists(konecne):
        raise VetevChyba("Větev s tímhle id už existuje, zkus to znovu.")
    st = os.stat(cesta)
    kept = sorted(off for off, _, u, _t in r["radky"] if u in vyber)
    delky = {off: ln for off, ln, _u, _t in r["radky"]}
    tip = r["rodic"].get(cil)
    posledni_zadani = ""
    fd = None
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
        with os.fdopen(fd, "wb") as out, open(cesta, "rb") as zdroj:
            fd = None
            if os.fstat(zdroj.fileno()).st_ino != st.st_ino:
                raise VetevChyba("Konverzace se mezitím změnila, zkus to znovu.")
            for typ in ("mode", "permission-mode"):
                if typ in r["stav"]:
                    zdroj.seek(r["stav"][typ][0])
                    out.write(_prepis_radek(zdroj.read(r["stav"][typ][1]), nove_id, vyber))
            for off in kept:
                zdroj.seek(off)
                radek = zdroj.read(delky[off])
                out.write(_prepis_radek(radek, nove_id, vyber))
            hlavicka = {"type": "ai-title", "aiTitle": _nazev_vetve(nazev), "sessionId": nove_id}
            out.write((json.dumps(hlavicka, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8"))
            if tip:
                # Konec větve pro Claude Code: list konverzace končí tímhle záznamem.
                out.write((json.dumps({"type": "last-prompt", "lastPrompt": posledni_zadani,
                                       "leafUuid": tip, "sessionId": nove_id},
                                      ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8"))
            out.flush()
            os.fsync(out.fileno())
        pocet = _over(tmp, nove_id)
        # Původní soubor mezitím nesmí být nahrazený ani zkrácený (Claude Code
        # přepis při zhuštění přepisuje) — kopie by pak nemusela sedět.
        if os.stat(cesta).st_ino != st.st_ino or os.stat(cesta).st_size < st.st_size:
            raise VetevChyba("Konverzace se mezitím změnila, zkus to znovu.")
        os.chmod(tmp, 0o600)
        os.rename(tmp, konecne)
        return konecne, pocet
    except VetevChyba:
        raise
    except OSError as exc:
        raise _chyba_zapisu(exc, slozka)
    except ValueError as exc:
        raise VetevChyba(f"Přepis se nepodařilo zpracovat: {exc}")
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            os.remove(tmp)                     # po úspěšném rename tu nic není
        except OSError:
            pass


def rozvetvit(path, zprava):
    """Nová konverzace z přepisu `path` do místa před zprávou `zprava` (uuid).

    Vrací {"id", "cwd", "title", "text", "pocet"} (`text` = zpráva, od které se
    větví, ať ji může člověk napsat znovu). Chyby jako VetevChyba."""
    if not path or not os.path.isfile(path) or not _slozka_ok(path):
        raise VetevChyba("Přepis konverzace jsem nenašel.")
    if not core.SESSION_ID.fullmatch(str(zprava or "")):
        raise VetevChyba("Neplatná zpráva.")
    puvodni = os.path.basename(path)[:-6]
    with _ZAMEK:
        # Dvojklik: tu samou větev nezakládáme dvakrát.
        klic = (puvodni, zprava)
        minule = _POSLEDNI.get(klic)
        if minule and time.time() - minule[0] < DVOJKLIK_S \
                and os.path.isfile(os.path.join(os.path.dirname(path), minule[1]["id"] + ".jsonl")):
            return dict(minule[1], znovu=True)
        try:
            st = os.stat(path)
            if st.st_size > MAX_PREPIS:
                raise VetevChyba("Konverzace je příliš dlouhá na rozvětvení.")
            r = _rejstrik(path, st.st_size, zprava)
            vyber, _ = _vyber(r, zprava)
            nove = str(uuidlib.uuid4())
            nazev = chats.title_for(path) or ""
            _zapis(path, nove, r, vyber, zprava, nazev)
        except OSError as exc:
            raise _chyba_zapisu(exc, os.path.dirname(path))
        cwd = r["cil"]["cwd"]
        vysledek = {"id": nove, "cwd": cwd, "title": _nazev_vetve(nazev),
                    "text": r["cil"]["text"], "pocet": len(vyber)}
        _POSLEDNI[klic] = (time.time(), vysledek)
        for k in [k for k, v in _POSLEDNI.items() if time.time() - v[0] > 300]:
            _POSLEDNI.pop(k, None)
        return vysledek
