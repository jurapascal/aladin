"""
Spotřeba z počítače do prostoru na serveru.

Kdo je v appce přihlášený k serveru, tomu hub na počítači po startu a pak
každých deset minut pošle bráně souhrn svých přepisů za poslední týden: po
sezeních a dnech odhad ceny a napsané tokeny (`POST /gw/spotreba/pocitac`,
token zařízení). Brána ho přičte ke spotřebě v prostoru, když počítač jede na
stejném předplatném (gateway/spotreba.py) — správce pak v Týmu vidí celou
spotřebu, ne jen serverovou, a limity platí na obojí.

Levně: čísla bere z mezipaměti Statistik (`hub-stats.json`); přepis čte, jen
když je od té doby změněný, a jen ty z posledních osmi dní. Z obsahu
konverzací neodchází nic — jen id sezení (podle něj brána pozná chat
přenesený na server a nepočítá ho dvakrát), dny a čísla, plus id účtu Claude,
podle kterého pozná, že jde o stejné předplatné.

Odpověď brány řekne, jak je na tom člověk s limitem a jestli tu platí stopka
(hub/stopka.py). Zkušební hub (`--test`) a hub v prostoru nic neposílají.
"""
import os
import threading
import time

from . import account, core, stats

INTERVAL = 600
STARE = 8 * 86400
MAX_RELACI = 2900       # brána bere nejvýš 3000 sezení v jednom souhrnu
STAV = {"radek": None, "stopka": None, "plati_zde": False, "stejne": False, "cas": 0, "chyba": ""}
_WAKE = threading.Event()
_precteno = {}          # přepisy, které Statistiky ještě neviděly: cesta → čísla
_STARTED = False


def start():
    global _STARTED
    if core.TEST_MODE or core.on_gateway() or _STARTED:
        return
    _STARTED = True
    threading.Thread(target=_loop, daemon=True, name="spotreba-pc").start()


def wake():
    """Hned poslat (třeba po přihlášení k serveru)."""
    _WAKE.set()


def _reset(chyba=""):
    STAV.update(radek=None, stopka=None, plati_zde=False, stejne=False, cas=0, chyba=chyba)


def _loop():
    _WAKE.wait(40)              # start hubu má přednost
    umi = ""                    # brána, o které víme, že souhrny bere
    while True:
        _WAKE.clear()
        pauza = INTERVAL
        token = core.CONFIG.get("gw_token") or ""
        base = account._base()
        if not token or not base:
            _reset()
        else:
            if umi != base:
                info, err, _kind = account._call("/gw/info", base=base, timeout=10)
                if info is None:
                    pauza = 120
                elif "spotreba" not in (info.get("features") or []):
                    _reset()
                    pauza = 6 * 3600             # starší brána — zkusit po aktualizaci
                else:
                    umi = base
            if umi == base:
                try:
                    odeslat(token)
                except Exception as exc:          # vlákno nesmí umřít
                    core.log_error("spotřeba: souhrn pro server selhal", exc)
        _WAKE.wait(pauza)


def odeslat(token):
    data, err, kind = account._call("/gw/spotreba/pocitac", token=token, payload=souhrn(), timeout=30)
    if data is not None:
        STAV.update(radek=data.get("radek"), stopka=data.get("stopka"),
                    plati_zde=bool(data.get("plati_zde")), stejne=bool(data.get("stejne_predplatne")),
                    cas=time.time(), chyba="")
    elif kind == account.AUTH:
        _reset(err)
    else:
        STAV["chyba"] = err


def _ucet():
    acc = core._claude_json().get("oauthAccount") or {}
    return {"uuid": str(acc.get("accountUuid") or "")[:64]} if isinstance(acc, dict) else {}


def souhrn(now=None):
    """{"pocitac", "ucet", "relace": {id sezení: {den: [cena, tokeny]}}} za 8 dní."""
    from . import pocitac
    now = time.time() if now is None else now
    hranice_t = now - STARE
    hranice = time.strftime("%Y-%m-%d", time.gmtime(hranice_t))
    files = (stats._load_cache().get("files") or {})
    relace = {}
    videne = set()
    try:
        slozky = [e for e in os.scandir(stats.PROJECTS_ROOT) if e.is_dir()]
    except OSError:
        slozky = []
    for slozka in slozky:
        try:
            soubory = list(os.scandir(slozka.path))
        except OSError:
            continue
        for e in soubory:
            if not e.name.endswith(".jsonl"):
                continue
            try:
                st = e.stat()
            except OSError:
                continue
            if st.st_mtime < hranice_t:
                continue
            videne.add(e.path)
            c = files.get(e.path)
            if not (c and c.get("size") == st.st_size and c.get("mtime") == int(st.st_mtime)
                    and "dcost" in c):
                c = _precteno.get(e.path)
                if not (c and c["size"] == st.st_size and c["mtime"] == int(st.st_mtime)):
                    try:
                        r = stats._scan_file(e.path)
                    except OSError:
                        continue
                    c = _precteno[e.path] = {"size": st.st_size, "mtime": int(st.st_mtime),
                                             "days": r["days"], "dcost": r["dcost"]}
            per = relace.get(e.name[:-6]) or {}
            for den, usd in (c.get("dcost") or {}).items():
                if den >= hranice:
                    # Stejné id ve dvou složkách je kopie téhož chatu — větší z obou.
                    old = per.get(den) or [0.0, 0]
                    per[den] = [max(old[0], round(usd, 4)),
                                max(old[1], int((c.get("days") or {}).get(den, 0)))]
            if per:
                relace[e.name[:-6]] = per
    for gone in set(_precteno) - videne:
        _precteno.pop(gone, None)
    if len(relace) > MAX_RELACI:
        # Brána víc nevezme; drobná sezení na konci limit stejně nerozhodnou.
        nej = sorted(relace, key=lambda s: -sum(v[0] for v in relace[s].values()))[:MAX_RELACI]
        relace = {s: relace[s] for s in nej}
    return {"pocitac": {"id": pocitac.device_id(), "nazev": pocitac._machine_name()},
            "ucet": _ucet(), "relace": relace}
