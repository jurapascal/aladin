"""
Naplánované úlohy: Claude dostane zadání sám, v nastavený čas a opakovaně
(„každý den v 7:00 projdi objednávky“, „v pondělí shrň minulý týden“).

Úloha = název, zadání (první zpráva Claudovi), složka projektu, model a kdy.
V daný čas hub otevře nový tab s Claudem v té složce a zadání mu pošle —
stejně jako úkol ze serveru (server.open_ukol_tab). Výsledek je tedy obyčejný
chat: jde ho číst, pokračovat v něm a najde se v seznamu chatů.

Kdy (pole `kdy.typ`):
  hodinove  — každou hodinu v minutu `cas` („:15“), volitelně jen v `dny`
  denne     — každý den v `cas`
  pracovni  — po–pá v `cas`
  tydne     — ve vybrané `dny` (0 = pondělí … 6 = neděle) v `cas`
  mesicne   — každý měsíc `den` (1–28) v `cas`

Hub běží, jen když běží appka (na počítači) nebo prostor (na serveru). Úloha,
která mezitím propadla, se po startu doběhne jednou — když nepropadla víc než
DOHNAT_S. Starší se přeskočí a čeká se na další termín. Když předchozí běh
téže úlohy ještě pracuje, nový se nespustí (dvě stejné práce by se praly).

Uloženo v ~/.claude/hub-naplanovane.json. Žádná výjimka z hlídače ven nejde.
"""
import datetime
import json
import os
import re
import threading
import time
import uuid

from . import core

TYPY = ("hodinove", "denne", "pracovni", "tydne", "mesicne")
DOHNAT_S = 12 * 3600          # propadlý běh se po startu doběhne, je-li čerstvější
KAZDYCH_S = 30                # jak často hlídač kouká na hodiny
MAX_ULOH = 100
MAX_ZADANI = 20_000
_CAS = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")

_LOCK = threading.RLock()


def soubor():
    return os.path.join(core.CLAUDE_DIR, "hub-naplanovane.json")


def _nacti():
    try:
        with open(soubor(), encoding="utf-8") as fh:
            data = json.load(fh)
        ulohy = data.get("ulohy") if isinstance(data, dict) else None
        return [u for u in (ulohy or []) if isinstance(u, dict) and u.get("id")]
    except (OSError, ValueError):
        return []


def _uloz(ulohy):
    cil = soubor()
    os.makedirs(os.path.dirname(cil), exist_ok=True)
    tmp = cil + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"ulohy": ulohy}, fh, ensure_ascii=False, indent=1)
    os.chmod(tmp, 0o600)
    os.replace(tmp, cil)


# ── kdy ─────────────────────────────────────────────────────────────────────

def _hm(cas):
    m = _CAS.match(str(cas or "").strip())
    return (int(m.group(1)), int(m.group(2))) if m else (8, 0)


def _cisty_kdy(kdy):
    """Zkontrolovaný a doplněný rozpis — cokoli divného se opraví na rozumné."""
    kdy = kdy if isinstance(kdy, dict) else {}
    typ = kdy.get("typ") if kdy.get("typ") in TYPY else "denne"
    h, m = _hm(kdy.get("cas"))
    dny = sorted({int(d) for d in (kdy.get("dny") or []) if str(d).lstrip("-").isdigit() and 0 <= int(d) <= 6})
    try:
        den = min(28, max(1, int(kdy.get("den") or 1)))
    except (TypeError, ValueError):
        den = 1
    out = {"typ": typ, "cas": f"{h:02d}:{m:02d}"}
    if typ == "tydne":
        out["dny"] = dny or [0]
    elif typ == "hodinove" and dny:
        out["dny"] = dny
    if typ == "mesicne":
        out["den"] = den
    return out


def dalsi_beh(kdy, po):
    """První termín úlohy ostře po `po` (datetime, místní čas), nebo None."""
    kdy = _cisty_kdy(kdy)
    h, m = _hm(kdy["cas"])
    typ = kdy["typ"]
    zacatek = po.replace(second=0, microsecond=0)
    if typ == "hodinove":
        t = zacatek.replace(minute=m)
        if t <= po:
            t += datetime.timedelta(hours=1)
        dny = set(kdy.get("dny") or range(7))
        for _ in range(24 * 8):
            if t.weekday() in dny:
                return t
            t += datetime.timedelta(hours=1)
        return None
    if typ == "mesicne":
        t = zacatek.replace(day=kdy["den"], hour=h, minute=m)
        if t <= po:
            rok, mesic = (t.year + 1, 1) if t.month == 12 else (t.year, t.month + 1)
            t = t.replace(year=rok, month=mesic)
        return t
    dny = {"denne": set(range(7)), "pracovni": {0, 1, 2, 3, 4}}.get(typ) or set(kdy.get("dny") or [0])
    t = zacatek.replace(hour=h, minute=m)
    if t <= po:
        t += datetime.timedelta(days=1)
    for _ in range(8):
        if t.weekday() in dny:
            return t
        t += datetime.timedelta(days=1)
    return None


def popis(kdy):
    """Lidsky: „každý den v 7:00“, „po, st v 9:30“…"""
    kdy = _cisty_kdy(kdy)
    jmena = ["po", "út", "st", "čt", "pá", "so", "ne"]
    h, m = _hm(kdy["cas"])
    cas = f"{h}:{m:02d}"
    typ = kdy["typ"]
    if typ == "hodinove":
        dny = kdy.get("dny")
        return f"každou hodinu v :{m:02d}" + (" (" + ", ".join(jmena[d] for d in dny) + ")" if dny else "")
    if typ == "denne":
        return f"každý den v {cas}"
    if typ == "pracovni":
        return f"v pracovní dny v {cas}"
    if typ == "mesicne":
        return f"každý měsíc {kdy['den']}. v {cas}"
    return ", ".join(jmena[d] for d in kdy.get("dny") or [0]) + f" v {cas}"


# ── úlohy ───────────────────────────────────────────────────────────────────

def _verejna(u, ted=None):
    ted = ted or datetime.datetime.now()
    dalsi = dalsi_beh(u.get("kdy"), ted) if u.get("zapnuto", True) else None
    return {**u, "popis": popis(u.get("kdy")),
            "dalsi": int(dalsi.timestamp()) if dalsi else 0}


def seznam():
    with _LOCK:
        return [_verejna(u) for u in _nacti()]


class Chyba(ValueError):
    pass


def uloz(data):
    """Nová úloha (bez `id`) nebo úprava existující. Vrací uloženou úlohu."""
    if not isinstance(data, dict):
        raise Chyba("Chybí úloha.")
    nazev = " ".join(str(data.get("nazev") or "").split())[:80]
    zadani = str(data.get("zadani") or "").strip()[:MAX_ZADANI]
    if not zadani:
        raise Chyba("Napiš, co má Claude udělat.")
    slozka = os.path.expanduser(str(data.get("slozka") or "").strip()) or core.HOME
    if not os.path.isdir(slozka):
        raise Chyba(f"Složka {slozka} neexistuje.")
    model = str(data.get("model") or "").strip()[:60]
    if model and not re.fullmatch(r"[A-Za-z0-9._\[\]-]+", model):
        raise Chyba("Neplatný model.")
    with _LOCK:
        ulohy = _nacti()
        uid = str(data.get("id") or "")
        stara = next((u for u in ulohy if u["id"] == uid), None) if uid else None
        if not stara and len(ulohy) >= MAX_ULOH:
            raise Chyba(f"Víc než {MAX_ULOH} naplánovaných úloh mít nejde.")
        u = stara or {"id": uuid.uuid4().hex[:12], "vytvoreno": int(time.time()), "posledni": 0}
        kdy = _cisty_kdy(data.get("kdy"))
        u.update({"nazev": nazev or zadani.split("\n")[0][:60], "zadani": zadani,
                  "slozka": slozka, "model": model, "kdy": kdy,
                  "zapnuto": bool(data.get("zapnuto", True))})
        if stara and stara.get("kdy") != kdy:
            # Nový rozpis platí od teď — starý propadlý termín se nedohání.
            u["od"] = int(time.time())
        if not stara:
            u["od"] = int(time.time())
            ulohy.append(u)
        _uloz(ulohy)
        return _verejna(u)


def smaz(uid):
    with _LOCK:
        ulohy = _nacti()
        zbyle = [u for u in ulohy if u["id"] != uid]
        if len(zbyle) == len(ulohy):
            raise Chyba("Taková úloha není.")
        _uloz(zbyle)
    return {"ok": True}


def prepni(uid, zapnuto):
    with _LOCK:
        ulohy = _nacti()
        u = next((x for x in ulohy if x["id"] == uid), None)
        if not u:
            raise Chyba("Taková úloha není.")
        u["zapnuto"] = bool(zapnuto)
        if zapnuto:
            u["od"] = int(time.time())       # po zapnutí se nedohání, co propadlo vypnuté
        _uloz(ulohy)
        return _verejna(u)


# ── spouštění ───────────────────────────────────────────────────────────────

def _bezi_jeste(hub, u):
    """Pracuje ještě tab z minulého běhu téhle úlohy?"""
    sid = u.get("posledni_tab")
    s = hub.sessions.get(sid) if sid else None
    # Po restartu hubu se čísla tabů počítají znovu — číslo musí sedět i s názvem.
    if not s or s.exited or s.title != "🕒 " + (u.get("nazev") or "Naplánovaná úloha"):
        return False
    try:
        from . import uspavani
        return bool(uspavani.pracuje(s))
    except Exception:
        return False


def spust(uid, hub=None, rucne=True):
    """Pustí úlohu hned. Vrací info o otevřeném tabu."""
    from . import server
    hub = hub or server.HUB
    with _LOCK:
        ulohy = _nacti()
        u = next((x for x in ulohy if x["id"] == uid), None)
        if not u:
            raise Chyba("Taková úloha není.")
        if _bezi_jeste(hub, u):
            raise Chyba("Minulý běh téhle úlohy ještě pracuje.")
        slozka = u.get("slozka") or core.HOME
        if not os.path.isdir(slozka):
            slozka = core.HOME
        titulek = "🕒 " + (u.get("nazev") or "Naplánovaná úloha")
        session = hub.open("project", slozka, titulek, 120, 32, agent="claude",
                           model=u.get("model") or "", prompt=u["zadani"])
        session.vlastni_nazev = True          # název úlohy, ne vymyšlený z obsahu
        hub.broadcast({"t": "tab-opened", **session.info()})
        u["posledni"] = int(time.time())
        u["posledni_tab"] = session.id
        u["behu"] = int(u.get("behu") or 0) + 1
        _uloz(ulohy)
    core.log(f"naplánovaná úloha „{u.get('nazev')}“ spuštěna" + (" ručně" if rucne else "")
             + f" (tab {session.id}, {slozka})")
    return {"ok": True, "tab": session.id}


def _splatne(ted):
    """Úlohy, na které právě došlo (id), podle posledního běhu a rozpisu."""
    out = []
    for u in _nacti():
        if not u.get("zapnuto", True):
            continue
        od = max(int(u.get("posledni") or 0), int(u.get("od") or u.get("vytvoreno") or 0))
        if not od:
            continue
        termin = dalsi_beh(u.get("kdy"), datetime.datetime.fromtimestamp(od))
        if not termin or termin > ted:
            continue
        if (ted - termin).total_seconds() > DOHNAT_S:
            # Appka byla dlouho zavřená: tenhle termín propadl, čeká se na další.
            with _LOCK:
                vse = _nacti()
                for x in vse:
                    if x["id"] == u["id"]:
                        x["od"] = int(ted.timestamp())
                _uloz(vse)
            core.log(f"naplánovaná úloha „{u.get('nazev')}“: termín {termin:%d. %m. %H:%M} "
                     "propadl (appka neběžela), čekám na další")
            continue
        out.append(u["id"])
    return out


def hlidej(hub):
    """Vlákno hubu: jednou za KAZDYCH_S spustí, na co došlo."""
    time.sleep(20)                 # ať start hubu doběhne (obnova tabů)
    while True:
        try:
            for uid in _splatne(datetime.datetime.now()):
                try:
                    spust(uid, hub, rucne=False)
                except Chyba as exc:
                    core.log(f"naplánovaná úloha {uid}: {exc}", "warn")
                    with _LOCK:      # odložit do dalšího termínu, ať to nezkouší každých 30 s
                        vse = _nacti()
                        for x in vse:
                            if x["id"] == uid:
                                x["od"] = int(time.time())
                        _uloz(vse)
        except Exception as exc:
            core.log_error("naplánované úlohy", exc)
        time.sleep(KAZDYCH_S)


def start(hub):
    if core.TEST_MODE:
        return
    threading.Thread(target=hlidej, args=(hub,), name="naplanovane", daemon=True).start()
