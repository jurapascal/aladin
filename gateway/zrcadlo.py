"""
Chaty z počítače v prostoru na serveru — „počítač počítá, kopie je na serveru".

Kdo má appku na počítači přihlášenou k serveru, nechá Clauda běžet na
počítači (šetří server) a každý jeho chat se sem průběžně kopíruje: počítač
posílá jen nové řádky přepisu (hub/zrcadlo.py). Brána je zapíše rovnou do
domova člověka (`~/.claude/projects/<domov>/<id>.jsonl`), takže chat je
v prostoru vidět a jde v něm pokračovat — a prostor se kvůli tomu nebudí.

Chat běží vždycky jen na jednom místě (`drzi`: "pc" nebo "server"):

* Telefon (prostor) otevře chat, který drží počítač → brána počítači při
  příští synchronizaci řekne „uvolni" (`uvolnit`). Počítač nechá dopsat
  rozdělanou odpověď, tab zavře, pošle poslední řádky a ohlásí `uvolneno`;
  pak chat drží server. Počítač, který se dlouho neozval, se nečeká.
* Počítač otevře chat, který drží server → brána řekne běžícímu prostoru, ať
  chat po dopsání odpovědi zavře (`zrcadlo-uvolnit` v hub/server.py), a pak
  počítači přepis vydá (`stahnout`). Odteď ho drží počítač a zase kopíruje.

Řádky se počítají po řádcích, ne po bajtech: `cwd` v přepisu se při zápisu
přepisuje na složku strany, která chat má (počítač ≠ server), takže bajty se
liší, ale řádků je stejně.

Registr drží brána mimo domovy (GATEWAY_DIR/zrcadla/<uid>.json) — člověk si
v prostoru nemůže přepsat, kde chat „běží".
"""
import contextlib
import fcntl
import json
import os
import re
import threading
import time

from hub import chats as hub_chats
from hub import prenos

from . import config, workspace

REG_DIR = os.path.join(config.GATEWAY_DIR, "zrcadla")
SESSION_ID = prenos.SESSION_ID
PC_ONLINE_S = 75          # počítač se ozývá každých ~10 s; tohle ticho = vypnutý
UVOLNENI_MAX_S = 150      # déle se na počítač nečeká (odpověď se neutne, jen se nečeká)
MAX_RADKU = 4000          # řádků v jedné dávce
MAX_DAVKA = 6 * 1024 * 1024
SEZNAM_DNI = 60           # seznam chatů na serveru pro počítač: jak staré

_LOCK = threading.Lock()  # registr (spolu se zámkem souboru)
_MEZI = threading.Lock()  # mezipaměti níž — zvlášť, volají se i se zamčeným registrem
_INFO = {}                # cesta → (velikost, mtime, info) pro seznam
_RADKY = {}               # cesta → (velikost, počet řádků)


# ── registr ──────────────────────────────────────────────────────────────────
def _reg_path(user):
    return os.path.join(REG_DIR, "%d.json" % int(user["id"]))


@contextlib.contextmanager
def _zamek(user):
    os.makedirs(REG_DIR, mode=0o700, exist_ok=True)
    with _LOCK:
        with open(_reg_path(user) + ".lock", "a") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)


def _nacti(user):
    try:
        with open(_reg_path(user), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    data.setdefault("chaty", {})
    data.setdefault("pocitace", {})
    return data


def _uloz(user, data):
    tmp = _reg_path(user) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False)
    os.replace(tmp, _reg_path(user))


def _online(data, pocitac):
    p = (data.get("pocitace") or {}).get(pocitac) or {}
    return time.time() - float(p.get("videno") or 0) < PC_ONLINE_S


# ── soubory v domově ─────────────────────────────────────────────────────────
def _claude_dir(user):
    return os.path.join(workspace.home_for(user), ".claude")


def _cesta(user, chat):
    """Kde chat v domově je, nebo kam ho zapsat (složka domova)."""
    path, _sub = prenos.najdi(_claude_dir(user), chat)
    if path:
        return path
    home = workspace.home_for(user)
    return os.path.join(_claude_dir(user), "projects", prenos.slug(home), chat + ".jsonl")


def _pocet_radku(path):
    """Kolik celých řádků přepis má. Drží se podle velikosti — přepis roste
    jen připisováním, takže se nepočítá pořád od začátku."""
    try:
        size = os.path.getsize(path)
    except OSError:
        return 0
    with _MEZI:
        cached = _RADKY.get(path)
    if cached and cached[0] == size:
        return cached[1]
    start, n = 0, 0
    if cached and cached[0] < size:
        start, n = cached
    with open(path, "rb") as fh:
        fh.seek(start)
        while True:
            kus = fh.read(1024 * 1024)
            if not kus:
                break
            n += kus.count(b"\n")
    with _MEZI:
        _RADKY[path] = (size, n)
    return n


def _radky_od(path, od, limit=MAX_DAVKA):
    """Řádky přepisu od indexu `od` (bez konce řádku), nejvýš ~`limit` bajtů.
    → (řádky, celkem). Useknutý poslední řádek (Claude zrovna píše) se nevydá."""
    out, velikost, i = [], 0, 0
    with open(path, "rb") as fh:
        for raw in fh:
            if i >= od:
                if not raw.endswith(b"\n"):
                    break
                if velikost + len(raw) > limit and out:
                    break
                out.append(raw[:-1].decode("utf-8", "replace"))
                velikost += len(raw)
            i += 1
    return out, _pocet_radku(path)


def _pripis(path, radky, cwd):
    """Připíše řádky na konec přepisu a `cwd` v nich přepíše na `cwd`."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    data = b"".join(r.encode("utf-8") + b"\n" for r in radky)
    import io
    with open(path, "ab") as fh:
        for kus in prenos._prepis_cwd(io.BytesIO(data), cwd):
            fh.write(kus)


def _platne_radky(radky):
    """Jen celé řádky JSON (objekty) — nic jiného do přepisu nepatří."""
    if not isinstance(radky, list) or len(radky) > MAX_RADKU:
        raise ValueError("Dávka řádků je moc velká.")
    out = []
    for r in radky:
        if not isinstance(r, str) or "\n" in r:
            raise ValueError("Neplatný řádek přepisu.")
        try:
            if not isinstance(json.loads(r), dict):
                raise ValueError
        except ValueError:
            raise ValueError("Neplatný řádek přepisu.") from None
        out.append(r)
    return out


# ── počítač: kopie chatů a příkazy zpátky ────────────────────────────────────
def sync(user, pocitac, jmeno, chaty, uvolneno=()):
    """Dávka z počítače. `chaty` = [{id, od, radky, titulek, cwd}], `uvolneno`
    = chaty, které počítač pustil (telefon si o ně řekl). Vrací
    {"chaty": {id: {"stav": "ok"|"od"|"server", "radky": n}}, "uvolnit": [id…]}."""
    pocitac = str(pocitac or "")[:80] or "pocitac"
    home = workspace.home_for(user)
    vysledek = {}
    with _zamek(user):
        data = _nacti(user)
        data["pocitace"][pocitac] = {"jmeno": str(jmeno or "")[:80] or "Počítač", "videno": time.time()}
        for c in chaty or ():
            cid = str((c or {}).get("id") or "")
            if not SESSION_ID.fullmatch(cid):
                continue
            e = data["chaty"].get(cid) or {}
            path = _cesta(user, cid)
            mam = _pocet_radku(path) if os.path.isfile(path) else 0
            if e.get("drzi") == "server" or (e.get("drzi") == "pc" and e.get("pocitac") not in (None, "", pocitac)
                                             and _online(data, e.get("pocitac"))):
                # Chat drží někdo jiný — počítač ho nesmí přepsat.
                vysledek[cid] = {"stav": "server", "radky": mam}
                continue
            try:
                od = int(c.get("od") or 0)
            except (TypeError, ValueError):
                od = -1
            if od != mam:
                vysledek[cid] = {"stav": "od", "radky": mam}
                continue
            try:
                radky = _platne_radky(c.get("radky") or [])
            except ValueError as exc:
                vysledek[cid] = {"stav": "chyba", "radky": mam, "error": str(exc)}
                continue
            if radky:
                _pripis(path, radky, home)
                mam = _pocet_radku(path)
            e.update(drzi="pc", pocitac=pocitac, radky=mam, kdy=time.time(),
                     titulek=str(c.get("titulek") or e.get("titulek") or "")[:120],
                     cwd_pc=str(c.get("cwd") or e.get("cwd_pc") or "")[:400])
            e.setdefault("uvolnit", 0)
            data["chaty"][cid] = e
            vysledek[cid] = {"stav": "ok", "radky": mam}
        # Až po zápisu: s uvolněním chodí i poslední řádky dopsané odpovědi.
        for chat in uvolneno or ():
            e = data["chaty"].get(str(chat))
            if e and e.get("drzi") == "pc" and e.get("pocitac") == pocitac:
                e.update(drzi="server", uvolnit=0, prevzato=time.time())
        uvolnit = [cid for cid, e in data["chaty"].items()
                   if e.get("uvolnit") and e.get("drzi") == "pc" and e.get("pocitac") == pocitac]
        _uloz(user, data)
    return {"ok": True, "chaty": vysledek, "uvolnit": uvolnit}


def seznam(user):
    """Chaty v prostoru pro seznam na počítači (bez přepisů): id, název,
    náhled, kdy, kde běží. Prostor se kvůli tomu nebudí — čte se z domova."""
    root = os.path.join(_claude_dir(user), "projects")
    hranice = time.time() - SEZNAM_DNI * 86400
    with _zamek(user):
        data = _nacti(user)
    out = []
    try:
        slozky = [e for e in os.scandir(root) if e.is_dir(follow_symlinks=False)]
    except OSError:
        slozky = []
    for slozka in slozky:
        try:
            soubory = list(os.scandir(slozka.path))
        except OSError:
            continue
        for f in soubory:
            if not f.name.endswith(".jsonl") or not SESSION_ID.fullmatch(f.name[:-6]) \
                    or not f.is_file(follow_symlinks=False):
                continue
            st = f.stat(follow_symlinks=False)
            if st.st_mtime < hranice:
                continue
            with _MEZI:
                cached = _INFO.get(f.path)
            if cached and cached[0] == st.st_size and cached[1] == st.st_mtime:
                info = cached[2]
            else:
                info = hub_chats._info(f.path, st.st_size)
                with _MEZI:
                    _INFO[f.path] = (st.st_size, st.st_mtime, info)
            if not info:
                continue
            e = data["chaty"].get(info["id"]) or {}
            out.append({"id": info["id"], "title": info["title"], "prompt": info.get("prompt", ""),
                        "posledni": info.get("posledni", ""), "posledni_kdo": info.get("posledni_kdo", ""),
                        "parents": info.get("parents", []), "updated": int(st.st_mtime),
                        "drzi": e.get("drzi") or "server", "pocitac": e.get("pocitac", ""),
                        "cwd_pc": e.get("cwd_pc", "")})
    # Obnovená konverzace nahradí tu, ze které vznikla (stejně jako hub/chats.py).
    kdy = {c["id"]: c["updated"] for c in out}
    kryte = {p for c in out for p in c["parents"] if p in kdy and kdy[p] <= c["updated"]}
    # `parents` zůstávají: počítač podle nich schová svou starší kopii.
    out = [c for c in out if c["id"] not in kryte]
    out.sort(key=lambda c: -c["updated"])
    return out[:400]


def stav(user):
    """Pro prostor (badge v seznamu chatů): {id: {drzi, pocitac, jmeno, online, uvolnit}}."""
    with _zamek(user):
        data = _nacti(user)
    out = {}
    for cid, e in data["chaty"].items():
        p = e.get("pocitac", "")
        out[cid] = {"drzi": e.get("drzi", ""), "pocitac": p,
                    "jmeno": ((data["pocitace"].get(p) or {}).get("jmeno") or "Počítač"),
                    "online": _online(data, p), "uvolnit": bool(e.get("uvolnit"))}
    return out


# ── převzetí ─────────────────────────────────────────────────────────────────
def chci_na_server(user, chat, vynutit=False):
    """Telefon (prostor) chce chat, který drží počítač. → {"stav": "server"}
    hned (počítač je vypnutý, nebo chat nikdo nedrží), nebo {"stav": "ceka"}
    — počítač ho pustí, až Claude dopíše odpověď."""
    chat = str(chat or "")
    with _zamek(user):
        data = _nacti(user)
        e = data["chaty"].get(chat)
        if not e or e.get("drzi") != "pc":
            return {"ok": True, "stav": "server"}
        ted = time.time()
        if vynutit or not _online(data, e.get("pocitac")) or \
                (e.get("uvolnit") and ted - float(e["uvolnit"]) > UVOLNENI_MAX_S):
            # Vypnutý počítač, nebo ho nepouští moc dlouho (nebo to člověk chce
            # hned): bere si ho server.
            # Co počítač mezitím připíše, brána odmítne (drží server).
            e.update(drzi="server", uvolnit=0, prevzato=ted)
            _uloz(user, data)
            return {"ok": True, "stav": "server"}
        if not e.get("uvolnit"):
            e["uvolnit"] = ted
            _uloz(user, data)
        jmeno = (data["pocitace"].get(e.get("pocitac")) or {}).get("jmeno") or "Počítač"
        return {"ok": True, "stav": "ceka", "pocitac": jmeno}


def chci_na_pocitac(user, chat, pocitac, uvolni_v_prostoru):
    """Počítač chce chat, který drží server. `uvolni_v_prostoru(chat)` zavře
    tab v běžícím prostoru po dopsání odpovědi (False = nepodařilo se).
    Pak chat drží počítač a stáhne si ho (`stahnout`)."""
    chat = str(chat or "")
    if not SESSION_ID.fullmatch(chat):
        raise ValueError("Neplatný chat.")
    path, _sub = prenos.najdi(_claude_dir(user), chat)
    if not path:
        raise ValueError("Tenhle chat na serveru není.")
    if not uvolni_v_prostoru(chat):
        raise ValueError("Na serveru v tomhle chatu Claude pořád pracuje — zkus to za chvíli.")
    with _zamek(user):
        data = _nacti(user)
        e = data["chaty"].get(chat) or {}
        e.update(drzi="pc", pocitac=str(pocitac or "")[:80], uvolnit=0, kdy=time.time(),
                 radky=_pocet_radku(path))
        data["chaty"][chat] = e
        _uloz(user, data)
        return {"ok": True, "radky": e["radky"], "cwd_pc": e.get("cwd_pc", ""),
                "titulek": e.get("titulek", "")}


def stahnout(user, chat, od):
    """Řádky přepisu od `od` pro počítač (po dávkách)."""
    chat = str(chat or "")
    path, _sub = prenos.najdi(_claude_dir(user), chat) if SESSION_ID.fullmatch(chat) else ("", "")
    if not path:
        raise ValueError("Tenhle chat na serveru není.")
    try:
        od = max(0, int(od or 0))
    except (TypeError, ValueError):
        od = 0
    radky, celkem = _radky_od(path, od)
    return {"ok": True, "radky": radky, "od": od, "celkem": celkem}


def je_id(chat):
    return bool(re.fullmatch(SESSION_ID.pattern, str(chat or "")))
