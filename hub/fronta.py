"""
Fronta zpráv na serveru — zpráva Claudovi se nejdřív uloží, pak se doručí.

Dřív zpráva z bubliny žila jen v paměti stránky: restart prostoru, obnovení
okna nebo usnutý telefon ji smazaly a Claude se o ní nikdy nedozvěděl. Teď
okno zprávu předá serveru (`pridej`), ten ji drží v ~/.claude/hub-fronta.json
a vlákno `hlidej` ji píše do terminálu Claude Code, až je na to klid:

  * tab běží a Claude po startu dokreslil (výstup ztichl),
  * Claude nepracuje (nic nečeká na nástroj ani na odpověď v dialogu — vstup
    by se jinak trefil do dialogu).

Doručeno = zpráva je v přepisu konverzace (obyčejný `user` nebo `enqueue`
ve frontě Claude Code). Teprve pak se ze souboru smaže. Dokud to nejde
poznat, zůstává: po restartu hubu se zkusí znovu, ať se nic neztratí.
Duplikát nehrozí — před každým dalším pokusem se přepis ověří znovu a text
se napíše nejvýš jednou; potom už jen Enter a nakonec zpráva čeká na
„Poslat znovu“ od člověka.

Pozastavení (`pauza`) je u konverzace: zprávy zůstanou uložené, nic se
nedoručuje, dokud ho někdo nezruší.
"""
import json
import os
import re
import threading
import time

from . import core, cteni

SOUBOR = os.path.join(core.CLAUDE_DIR, "hub-fronta.json")
MAX_ZPRAVA = 20000
MAX_VE_FRONTE = 50
ROZJEZD_S = 10.0             # po startu Claude Code chvíli nic nebere
KLID_S = 1.5                 # výstup musí ztichnout
POTVRZENI_S = 20.0           # tak dlouho se čeká na přepis, než se zkusí Enter
ZASEKLE_S = 150.0            # tak dlouho bez změny v přepisu = Claude se zdá zaseklý
PO_ENTERU_S = 25.0           # a tak dlouho po Enteru, než je zpráva „nedošla"
STARE_S = 7 * 86400          # co nikdo týden nechtěl, se zahodí

_lock = threading.RLock()
_polozky = []                # [{id, chat, text, ts, stav, typed, enter, pokusy}]
_pauza = set()               # id konverzací
_nacteno = False
_probud = threading.Event()


# ── uložení ──────────────────────────────────────────────────────────────────
def _nacti():
    global _nacteno
    with _lock:
        if _nacteno:
            return
        _nacteno = True
        try:
            with open(SOUBOR, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return
        ted = time.time() * 1000
        _polozky[:] = [p for p in (data.get("zpravy") or [])
                       if isinstance(p, dict) and p.get("text") and p.get("chat")
                       and ted - float(p.get("ts") or 0) < STARE_S * 1000]
        _pauza.update(str(c) for c in (data.get("pauza") or []))


def _uloz():
    with _lock:
        tmp = SOUBOR + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"zpravy": _polozky, "pauza": sorted(_pauza)}, fh, ensure_ascii=False)
            os.replace(tmp, SOUBOR)
            os.chmod(SOUBOR, 0o600)
        except OSError as exc:
            core.log(f"fronta zpráv: nejde uložit ({exc})", "warn")


# ── API pro okna ─────────────────────────────────────────────────────────────
def _verejne(p):
    return {"id": p["id"], "text": p["text"], "ts": p["ts"], "stav": p["stav"]}


def seznam(chat):
    _nacti()
    with _lock:
        return [_verejne(p) for p in _polozky if p["chat"] == chat]


def pridej(chat, text, id_):
    """Uloží zprávu do fronty konverzace. Stejné `id` podruhé nic nepřidá."""
    _nacti()
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1a\x1c-\x1f\x7f]", "", str(text or "")).replace("\r", "")
    if not text.strip():
        raise ValueError("Zpráva je prázdná.")
    if len(text) > MAX_ZPRAVA:
        raise ValueError("Zpráva je moc dlouhá.")
    id_ = re.sub(r"[^A-Za-z0-9_-]", "", str(id_ or ""))[:40] or f"z{int(time.time() * 1000)}"
    with _lock:
        for p in _polozky:
            if p["id"] == id_:
                return _verejne(p)
        if sum(1 for p in _polozky if p["chat"] == chat) >= MAX_VE_FRONTE:
            raise ValueError("Ve frontě je moc zpráv — počkej, až je Claude převezme.")
        p = {"id": id_, "chat": chat, "text": text, "ts": int(time.time() * 1000),
             "stav": "ceka", "typed": 0, "enter": 0, "pokusy": 0}
        _polozky.append(p)
        _uloz()
    _probud.set()
    return _verejne(p)


def znovu(chat, id_):
    """Člověk klikl na „Poslat znovu“: zpráva se vrací do fronty od začátku."""
    _nacti()
    with _lock:
        for p in _polozky:
            if p["chat"] == chat and p["id"] == id_:
                p.update(stav="ceka", typed=0, enter=0, pokusy=0)
                _uloz()
                _probud.set()
                return True
    return False

def ihned(chat, id_):
    """„Přerušit a poslat hned“: Claude se zasekl (nástroj nevrací výsledek,
    dialog nikdo nevidí) — práce se přeruší Esc a zpráva se napíše."""
    _nacti()
    with _lock:
        for p in _polozky:
            if p["chat"] == chat and p["id"] == id_:
                p.update(stav="ceka", typed=0, enter=0, pokusy=0, ihned=True)
                _uloz()
                _probud.set()
                return True
    return False


def zrus(chat, id_):
    _nacti()
    with _lock:
        n = len(_polozky)
        _polozky[:] = [p for p in _polozky if not (p["chat"] == chat and p["id"] == id_)]
        if len(_polozky) != n:
            _uloz()
            return True
    return False


def pauza(chat, zapnout=None):
    """Zapne/vypne pozastavení; bez `zapnout` jen vrátí stav."""
    _nacti()
    with _lock:
        if zapnout is not None:
            (_pauza.add if zapnout else _pauza.discard)(chat)
            _uloz()
            _probud.set()
        return chat in _pauza


# ── doručování ───────────────────────────────────────────────────────────────
def _norm(t):
    t = re.sub(r"</?pasted_content\b[^>]*>", "", str(t or ""))
    t = re.sub(r"^\s*\[[^\]\n]{1,60}\]\s+", "", t)          # „[Jméno] " od sdílení
    return re.sub(r"\s+", " ", t).strip()


def _text_z_obsahu(obsah):
    if isinstance(obsah, str):
        return obsah
    if isinstance(obsah, list):
        return "\n".join(str(c.get("text") or "") for c in obsah
                         if isinstance(c, dict) and c.get("type") == "text")
    return ""


def _v_prepisu(cesta, p):
    """Je zpráva v přepisu (zpracovaná, nebo zařazená do fronty Claude Code)?"""
    if not cesta or not os.path.isfile(cesta):
        return False
    klic = _norm(p["text"])[:48]
    if not klic:
        return False
    try:
        with open(cesta, "rb") as fh:
            fh.seek(0, 2)
            fh.seek(max(0, fh.tell() - 600_000))
            radky = fh.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return False
    od = p["ts"] - 5000
    for radek in reversed(radky):
        if "queue-operation" not in radek[:200] and '"user"' not in radek[:300]:
            continue
        try:
            e = json.loads(radek)
        except ValueError:
            continue
        ms = cteni._ms(e.get("timestamp"))
        if ms and ms < od:
            break                                   # starší už být nemohou
        if e.get("type") == "queue-operation":
            if e.get("operation") != "enqueue":
                continue
            text = _text_z_obsahu(e.get("content"))
        elif e.get("type") == "user":
            obsah = (e.get("message") or {}).get("content")
            if isinstance(obsah, list) and any(isinstance(c, dict) and c.get("type") == "tool_result" for c in obsah):
                continue
            text = _text_z_obsahu(obsah)
        else:
            continue
        if klic in _norm(text):
            return True
    return False


def _session(chat):
    from . import server
    for s in list(server.HUB.sessions.values()):
        if getattr(s, "exited", False):
            continue
        if not (s.kind == "project" or s.kind.startswith("slash:")):
            continue
        if chat and chat in ((getattr(s, "chat_id", "") or ""), (s.resume or "")):
            return s
    return None


def chat_tabu(session):
    return (getattr(session, "chat_id", "") or getattr(session, "resume", "") or "") if session else ""


_PRIKAZ = ("<command-name>", "<local-command-stdout>", "<local-command-caveat>", "<command-message>")


def _pracuje(cesta):
    """Čeká Claude na výsledek nástroje, odpověď v dialogu, nebo zpracovává
    zprávu? Podle konce přepisu — ale bez příkazů (/model, /effort…), které
    hub posílá sám hned po startu: v čerstvém chatu jsou jediným obsahem a
    bez odpovědi Clauda by chat vypadal navždy zaneprázdněný (zpráva by
    se nikdy nedoručila)."""
    from . import mcp_tools
    try:
        age = time.time() - os.path.getmtime(cesta)
        if age < mcp_tools._PRACUJE_MTIME_S:
            return True
        if age > mcp_tools._PRACUJE_STARE_S:
            return False
        with open(cesta, "rb") as fh:
            fh.seek(0, 2)
            fh.seek(max(0, fh.tell() - 65536))
            radky = fh.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return False
    for radek in reversed(radky):
        try:
            e = json.loads(radek)
        except ValueError:
            continue
        typ = e.get("type")
        if typ not in ("user", "assistant") or e.get("isSidechain") or e.get("isMeta"):
            continue
        zprava = e.get("message") or {}
        if typ == "user":
            text = _text_z_obsahu(zprava.get("content"))
            if text.lstrip().startswith(_PRIKAZ):
                continue                            # příkaz, ne zadání pro Clauda
            if text.lstrip().startswith("[Request interrupted"):
                return False                        # práce přerušená Esc — Claude je volný
            return True
        return zprava.get("stop_reason") in (None, "tool_use")
    return False


def _stari(cesta):
    try:
        return time.time() - os.path.getmtime(cesta)
    except OSError:
        return 0.0


def _pripraven(s, cesta):
    if s.uspany:
        threading.Thread(target=s.probud, daemon=True).start()
        return False
    if time.time() - s.started < ROZJEZD_S:
        return False
    v = getattr(s, "_vystup_at", 0.0)
    if not v or time.time() - v < KLID_S:
        return False
    if getattr(s, "_fronta", None) is not None:      # právě se probouzí
        return False
    return not _pracuje(cesta)


def _napis(s, text):
    """Text a Enter zvlášť, s pauzou: slepené by je Claude Code vzal jako vložení."""
    vlozit = "\n" in text or text.startswith("?")
    data = ("\x1b[200~" + text + "\x1b[201~") if vlozit else text
    s.posledni = time.time()
    s.pty.write(data.encode("utf-8"))
    time.sleep(0.2)
    s.pty.write(b"\r")


def _oznam(s, p, stav):
    from . import server
    server.HUB.broadcast({"t": "fronta", "tab": s.id if s else 0, "chat": p["chat"],
                          "id": p["id"], "stav": stav})


def _krok():
    """Jeden průchod: ověří doručení, doručí, co jde, a zbytek nechá čekat."""
    _core = core
    with _lock:
        polozky = list(_polozky)
        pauzy = set(_pauza)
    zmena = False
    cekaji_ted = {}
    for p in polozky:
        if p["chat"] in pauzy:
            continue
        s = _session(p["chat"])
        if not s:
            continue
        cesta = _core.transcript_for(s)
        if _v_prepisu(cesta, p):
            with _lock:
                if p in _polozky:
                    _polozky.remove(p)
            _oznam(s, p, "doruceno")
            zmena = True
            continue
        if cekaji_ted.get(p["chat"]):
            continue                                # jedna zpráva za konverzaci naráz
        ted = time.time()
        if p["stav"] == "nedoslo":
            continue
        if p["typed"]:
            # Napsáno, v přepisu zatím není. Nejdřív čekat, pak jen Enter
            # (text mohl zůstat stát v poli), pak vzdát — víc by hrozilo duplikátem.
            cekej = ted - p["typed"] / 1000
            if not p["enter"] and cekej >= POTVRZENI_S and _pripraven(s, cesta):
                s.pty.write(b"\r")
                p["enter"] = int(ted * 1000)
                zmena = True
            elif p["enter"] and ted - p["enter"] / 1000 >= PO_ENTERU_S and _pripraven(s, cesta):
                p["stav"] = "nedoslo"
                _oznam(s, p, "nedoslo")
                zmena = True
            cekaji_ted[p["chat"]] = True
            continue
        pripraven = _pripraven(s, cesta)
        if not pripraven and not s.uspany and time.time() - s.started >= ROZJEZD_S \
                and getattr(s, "_fronta", None) is None:
            if p.get("ihned"):
                # Člověk chce zprávu hned: práce se přeruší (jediné Esc — dvojité
                # by otevřelo „vrátit zprávu“) a text se napíše.
                try:
                    s.pty.write(b"\x1b")
                    time.sleep(1.0)
                    pripraven = True
                except Exception:
                    pass
                p.pop("ihned", None)
            elif not p.get("zaseklo") and _stari(cesta) > ZASEKLE_S and _pracuje(cesta):
                p["zaseklo"] = True
                _oznam(s, p, "zaseklo")
                zmena = True
        if pripraven:
            try:
                _napis(s, p["text"])
            except Exception as exc:                # pty zrovna zaniklo — příště znovu
                _core.log(f"fronta zpráv: zápis do tabu {s.id} selhal ({exc})", "warn")
                continue
            p["typed"] = int(time.time() * 1000)
            p["pokusy"] += 1
            p["stav"] = "odeslano"
            _oznam(s, p, "odeslano")
            cekaji_ted[p["chat"]] = True
            zmena = True
    if zmena:
        _uloz()


def hlidej():
    """Vlákno na pozadí: každou vteřinu (nebo hned po nové zprávě) jeden krok."""
    _nacti()
    while True:
        _probud.wait(1.0)
        _probud.clear()
        try:
            with _lock:
                if not _polozky:
                    continue
            _krok()
        except Exception as exc:
            core.log_error("fronta zpráv: krok selhal", exc)
        time.sleep(0.2)


def spust():
    threading.Thread(target=hlidej, name="fronta-zprav", daemon=True).start()
