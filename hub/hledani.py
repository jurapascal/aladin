"""
Hledání napříč konverzacemi a export konverzace do Markdownu.

Hledání: projde přepisy (~/.claude/projects/*/<id>.jsonl) po řádcích, nikdy
ne celé do paměti, a hledá v textu zpráv člověka a Clauda. Výsledky nástrojů
(obří výpisy) jen na výslovné přání. Bez ohledu na velikost písmen i diakritiku
(„prilis" najde „příliš"). Běží ve vlastním vlákně s časovým limitem a limitem
výsledků; nový dotaz starý zruší, takže naráz běží nejvýš jedno hledání.
Stránka se ptá na stav (`stav`) a průběžné výsledky ukazuje hned.

Poškozený řádek, obří řádek, binární smetí, soubor, který mezitím zmizel nebo
se zkrátil — vše se přeskočí, hledání kvůli tomu nespadne.

Export: celá konverzace jako Markdown (zprávy, nástroje stručně na řádek).
Nic se nemaskuje, přepisuje se přesně — o tom, co s textem udělá, rozhoduje
člověk. Velikost výstupu i doba jsou omezené.
"""
import datetime
import json
import os
import re
import secrets
import stat
import threading
import time
import unicodedata

from . import chats
from . import core

MIN_DOTAZ = 2                    # nejmíň znaků dotazu
MAX_DOTAZ = 200
MAX_SLOV = 6
LIMIT_CASU = 25.0                # s — pak se hledání ukončí s částečnými výsledky
LIMIT_CHATU = 50                 # kolik konverzací se vrátí
LIMIT_NALEZU = 3                 # úryvků na konverzaci
MAX_RADEK = 1024 * 1024          # delší řádek (obrázky, výpisy) se přeskočí
MAX_TEXT = 60_000                # víc z jedné zprávy se nehledá
OKOLI = 70                       # znaků kolem shody v úryvku
EXPORT_MAX = 20 * 1024 * 1024    # největší .md
EXPORT_CAS = 30.0
EXPORT_RADEK = 4 * 1024 * 1024

_LOCK = threading.Lock()
_JOB = [None]                    # poslední (běžící nebo hotové) hledání
_EXPORTY = threading.Semaphore(2)

_ZNACKY = re.compile("[\u0300-\u036f]")
_DOPLNEK = {"ł": "l", "Ł": "l", "đ": "d", "Đ": "d", "ø": "o", "Ø": "o", "ı": "i"}
_DOPLNEK_RE = re.compile("[łŁđĐøØı]")


def fold(text):
    """Malá písmena bez diakritiky — „Příliš" -> „prilis". Rychlá cesta pro
    ASCII i regulární výrazy místo znaku po znaku (hledá se v desítkách MB)."""
    s = str(text)
    if s.isascii():
        return s.casefold()
    s = _ZNACKY.sub("", unicodedata.normalize("NFKD", s))
    return _DOPLNEK_RE.sub(lambda m: _DOPLNEK[m.group()], s).casefold()


def _slova(dotaz):
    """Dotaz -> seznam složených slov, nebo ValueError s větou pro člověka."""
    if not isinstance(dotaz, str):
        raise ValueError("Napiš dotaz textem.")
    text = " ".join(dotaz.replace("\0", " ").split())
    if len(text) < MIN_DOTAZ:
        raise ValueError(f"Napiš aspoň {MIN_DOTAZ} znaky.")
    if len(text) > MAX_DOTAZ:
        raise ValueError("Dotaz je moc dlouhý.")
    slova = []
    for w in fold(text).split():
        if w not in slova:
            slova.append(w)
    slova = slova[:MAX_SLOV]
    if not slova or max(len(w) for w in slova) < MIN_DOTAZ:
        raise ValueError(f"Napiš aspoň {MIN_DOTAZ} znaky.")
    return slova


def _cteni_radku(fh, limit):
    """Další řádek jako bytes; příliš dlouhý se dočte do konce a vrátí None
    (přeskočit). Na konci souboru vrací ''."""
    line = fh.readline(limit + 1)
    if not line:
        return ""
    if len(line) > limit and not line.endswith(b"\n"):
        while True:
            kus = fh.readline(1024 * 1024)
            if not kus or kus.endswith(b"\n"):
                break
        return None
    return line


def _texty(entry, nastroje=False):
    """(role, text) zpráv v jednom záznamu přepisu: 'me' / 'claude' / 'nastroj'."""
    kind = entry.get("type")
    if kind not in ("user", "assistant") or entry.get("isSidechain"):
        return
    if kind == "user" and entry.get("isMeta"):
        return
    content = (entry.get("message") or {}).get("content")
    if isinstance(content, str):
        content = [{"type": "text", "text": content}]
    if not isinstance(content, list):
        return
    for part in content:
        if not isinstance(part, dict):
            continue
        typ = part.get("type")
        if typ == "text" and isinstance(part.get("text"), str):
            text = part["text"]
            if kind == "user":
                text = chats._NOISE.sub(" ", text)
                if text.lstrip().startswith(("Caveat:", "[Request interrupted")):
                    continue
            yield ("me" if kind == "user" else "claude"), text
        elif nastroje and typ == "tool_use":
            yield "nastroj", json.dumps(part.get("input"), ensure_ascii=False)[:MAX_TEXT]
        elif nastroje and typ == "tool_result":
            res = part.get("content")
            if isinstance(res, list):
                res = " ".join(p.get("text", "") for p in res
                               if isinstance(p, dict) and isinstance(p.get("text"), str))
            if isinstance(res, str):
                yield "nastroj", res


def _cas(entry, zalozni):
    ts = entry.get("timestamp")
    if isinstance(ts, str):
        try:
            return int(datetime.datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp())
        except (ValueError, OverflowError, OSError):
            pass
    return zalozni


def _uryvek(text, slova):
    """Úryvek kolem první shody + místa zvýraznění, nebo None, když zpráva
    nemá všechna slova. Pozice se mapují zpět na původní text (diakritika
    může změnit délku)."""
    text = text[:MAX_TEXT]
    if not all(w in fold(text) for w in slova):
        return None                          # rychlá cesta: většina zpráv končí tady
    mapa, slozene = [], []
    for i, ch in enumerate(text):
        f = fold(ch)
        slozene.append(f)
        mapa.extend([i] * len(f))
    cele = "".join(slozene)
    nalezy = []
    for w in slova:
        pos = cele.find(w)
        if pos < 0:
            return None
        nalezy.append((mapa[pos], mapa[min(pos + len(w) - 1, len(mapa) - 1)] + 1, w))
    prvni = min(n[0] for n in nalezy)
    od = max(0, prvni - OKOLI)
    do = min(len(text), prvni + OKOLI * 2)
    out = text[od:do]
    # Víc bílých znaků za jeden; místa zvýraznění se přepočítají.
    zv, nove, posledni_mezera = [], [], False
    for i, ch in enumerate(out):
        mezera = ch.isspace() or ord(ch) < 32
        if mezera and posledni_mezera:
            zv.append(len(nove))
            continue
        zv.append(len(nove))
        nove.append(" " if mezera else ch)
        posledni_mezera = mezera
    zv.append(len(nove))
    ranges = []
    for a, b, _w in nalezy:
        a, b = a - od, b - od
        if b <= 0 or a >= len(out):
            continue
        a, b = max(a, 0), min(b, len(out))
        if zv[b] > zv[a]:
            ranges.append([zv[a], zv[b] - zv[a]])
    ranges.sort()
    return {"text": "".join(nove), "ranges": ranges,
            "pred": od > 0, "po": do < len(text)}


def _projdi_soubor(path, slova, nastroje, zrusit, konec, st):
    """Nálezy v jednom přepisu: seznam {role, when, uryvek, ranges…}."""
    nalezy, pocet = [], 0
    try:
        fh = open(path, "rb")
    except OSError:
        return nalezy, 0
    with fh:
        while not zrusit.is_set() and time.monotonic() < konec:
            try:
                line = _cteni_radku(fh, MAX_RADEK)
            except (OSError, ValueError, MemoryError):
                break
            if line == "":
                break
            if line is None or not line.lstrip().startswith(b"{"):
                continue
            # Řádky, které zprávu nenesou (přílohy, snímky souborů), ani neparsovat.
            if b'"user"' not in line and b'"assistant"' not in line:
                continue
            # Výsledky nástrojů jsou nejtěžší část přepisu — bez přání se ani neparsují.
            if not nastroje and b'"tool_result"' in line:
                continue
            try:
                entry = json.loads(line)
            except Exception:                # poškozený / useknutý řádek, hluboké vnoření
                continue
            if not isinstance(entry, dict):
                continue
            try:
                for role, text in _texty(entry, nastroje):
                    u = _uryvek(text, slova)
                    if not u:
                        continue
                    pocet += 1
                    if len(nalezy) < LIMIT_NALEZU:
                        nalezy.append(dict(u, role=role, when=_cas(entry, int(st.st_mtime))))
            except Exception:                # nečekaný tvar zprávy
                continue
    return nalezy, pocet


def _seznam_prepisu():
    """[(cesta, stat)] od nejnovějšího; jen běžné soubory s id konverzace."""
    root = os.path.join(core.CLAUDE_DIR, "projects")
    out = []
    try:
        slozky = os.listdir(root)
    except OSError:
        return out
    for folder in slozky:
        base = os.path.join(root, folder)
        try:
            jmena = os.listdir(base)
        except OSError:
            continue
        for name in jmena:
            if not name.endswith(".jsonl") or not core.SESSION_ID.fullmatch(name[:-6]):
                continue
            path = os.path.join(base, name)
            try:
                st = os.stat(path)
            except OSError:
                continue
            if stat.S_ISREG(st.st_mode):
                out.append((path, st))
    out.sort(key=lambda x: x[1].st_mtime, reverse=True)
    return out


def _bez(job):
    """Co se o hledání smí říct stránce."""
    return {k: job[k] for k in ("job", "hotovo", "chyba", "castecne", "prohledano", "celkem",
                                "vysledky", "nalezeno")}


def _beh(job, slova, nastroje):
    try:
        soubory = _seznam_prepisu()
        job["celkem"] = len(soubory)
        konec = time.monotonic() + LIMIT_CASU
        for path, st in soubory:
            if job["zrusit"].is_set():
                return                       # nahradilo ho nové hledání
            if time.monotonic() >= konec:
                job["castecne"] = True
                break
            nalezy, pocet = _projdi_soubor(path, slova, nastroje, job["zrusit"], konec, st)
            job["prohledano"] += 1
            if nalezy:
                try:
                    title = chats.title_for(path)
                except Exception:
                    title = ""
                cwd = ""
                with chats._LOCK:
                    info = (chats._CACHE.get(path) or (0, 0, None))[2] or {}
                cwd = info.get("cwd", "")
                job["vysledky"].append({
                    "id": os.path.basename(path)[:-6], "title": title or "(bez názvu)",
                    "project": info.get("project", ""), "cwd": cwd,
                    "exists": bool(cwd) and os.path.isdir(cwd),
                    "updated": int(st.st_mtime), "pocet": pocet, "nalezy": nalezy})
                job["nalezeno"] += pocet
                if len(job["vysledky"]) >= LIMIT_CHATU:
                    job["castecne"] = True
                    break
        if job["zrusit"].is_set():
            return
    except Exception as exc:                 # hledání nesmí nikdy shodit hub
        core.log_error("hledání v konverzacích", exc)
        job["chyba"] = "Hledání se nepovedlo."
    job["hotovo"] = True


def spust(dotaz, nastroje=False):
    """Zahájí hledání (staré zruší). Vrací {"job": id}; ValueError = špatný dotaz."""
    slova = _slova(dotaz)
    with _LOCK:
        stary = _JOB[0]
        if stary:
            stary["zrusit"].set()
        job = {"job": secrets.token_hex(6), "zrusit": threading.Event(), "hotovo": False,
               "chyba": "", "castecne": False, "prohledano": 0, "celkem": 0,
               "vysledky": [], "nalezeno": 0}
        _JOB[0] = job
    threading.Thread(target=_beh, args=(job, slova, bool(nastroje)), daemon=True).start()
    return {"job": job["job"]}


def stav(job_id):
    """Průběžný stav hledání, nebo None, když už je nahrazené jiným."""
    with _LOCK:
        job = _JOB[0]
        if not job or job["job"] != str(job_id or ""):
            return None
        snimek = _bez(job)
    snimek["vysledky"] = list(snimek["vysledky"])
    return snimek


def zrus():
    with _LOCK:
        if _JOB[0]:
            _JOB[0]["zrusit"].set()


# ── Export ──────────────────────────────────────────────────────────────────
def nazev_souboru(title, chat_id):
    """Bezpečné jméno: bez lomítek, teček na začátku a řídicích znaků."""
    t = re.sub(r"[^\w\- ]+", " ", str(title or ""), flags=re.U)
    t = "-".join(t.split())[:60].strip("-_")
    return f"{t or 'konverzace'}-{str(chat_id)[:8]}.md"


def _kratce(nazev, vstup):
    """Volání nástroje na jeden řádek."""
    if isinstance(vstup, dict):
        for k in ("file_path", "path", "command", "pattern", "url", "query", "description", "prompt"):
            if isinstance(vstup.get(k), str) and vstup[k]:
                return f"{nazev}: {' '.join(vstup[k].split())[:160]}"
    return str(nazev)


def export_md(chat_id):
    """(jméno souboru, bytes) s konverzací jako Markdown. ValueError s větou
    pro člověka, když to nejde."""
    from . import cteni
    path = cteni.path_for(chat_id)
    if not path:
        raise ValueError("Konverzace už neexistuje.")
    try:
        title = chats.title_for(path)
    except Exception:
        title = ""
    kus, velikost = [f"# {title or 'Konverzace'}\n\n"], 0
    stavy = {"zkraceno": ""}
    konec = time.monotonic() + EXPORT_CAS

    def pridej(s):
        nonlocal velikost
        b = len(s.encode("utf-8", "replace"))
        if velikost + b > EXPORT_MAX:
            stavy["zkraceno"] = "velikost"
            return False
        kus.append(s)
        velikost += b
        return True

    posledni = None
    try:
        fh = open(path, "rb")
    except OSError:
        raise ValueError("Konverzaci nejde přečíst.")
    with fh:
        while True:
            if time.monotonic() >= konec:
                stavy["zkraceno"] = "čas"
                break
            try:
                line = _cteni_radku(fh, EXPORT_RADEK)
            except (OSError, ValueError, MemoryError):
                break
            if line == "":
                break
            if line is None or not line.lstrip().startswith(b"{"):
                continue
            if (b'"user"' not in line and b'"assistant"' not in line) or b'"tool_result"' in line:
                continue
            try:
                entry = json.loads(line)
            except Exception:
                continue
            if not isinstance(entry, dict) or entry.get("type") not in ("user", "assistant") \
                    or entry.get("isSidechain") or entry.get("isMeta"):
                continue
            kdo = "Ty" if entry["type"] == "user" else "Claude"
            cas = ""
            ts = _cas(entry, 0)
            if ts:
                cas = " — " + datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
            content = (entry.get("message") or {}).get("content")
            if isinstance(content, str):
                content = [{"type": "text", "text": content}]
            if not isinstance(content, list):
                continue
            radky = []
            for part in content:
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "text" and isinstance(part.get("text"), str):
                    text = part["text"]
                    if entry["type"] == "user":
                        text = chats._NOISE.sub("", text)
                        if text.lstrip().startswith(("Caveat:", "[Request interrupted")):
                            continue
                    if text.strip():
                        radky.append(text.strip() + "\n\n")
                elif part.get("type") == "tool_use":
                    radky.append("> nástroj: `" + _kratce(part.get("name", "?"), part.get("input"))
                                 .replace("`", "'") + "`\n\n")
            if not radky:
                continue
            hlavicka = ""
            if kdo != posledni or any(not r.startswith("> nástroj") for r in radky):
                hlavicka = f"## {kdo}{cas}\n\n"
            posledni = kdo
            if not pridej(hlavicka + "".join(radky)):
                break
    if stavy["zkraceno"]:
        kus.append("\n---\n*Export je zkrácený "
                   + ("(dosáhl největší velikosti)" if stavy["zkraceno"] == "velikost"
                      else "(vypršel čas)") + ".*\n")
    return nazev_souboru(title, chat_id), "".join(kus).encode("utf-8", "replace")


def export_povolen():
    """Souběžně se exportují nejvýš dva přepisy."""
    return _EXPORTY.acquire(blocking=False)


def export_hotov():
    _EXPORTY.release()
