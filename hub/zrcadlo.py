"""
Počítač počítá, kopie chatů je na serveru — strana hubu (brána: gateway/zrcadlo.py).

* Na počítači přihlášeném k serveru běží Claude tady (výkon počítače, server
  odpočívá) a každý chat se průběžně kopíruje do prostoru na serveru: každých
  pár vteřin jdou bráně jen nové celé řádky přepisu. Na telefonu je tak chat
  vidět a dá se v něm pokračovat.
* Chat běží vždycky jen na jednom místě. Když si ho telefon vezme, brána to
  řekne v odpovědi na kopírování (`uvolnit`): počítač nechá Clauda dopsat
  odpověď, tab zavře, pošle poslední řádky a chat pustí. Opačně si počítač
  chat ze serveru vezme přes `prevzit` — prostor ho po dopsání odpovědi pustí
  a počítač si stáhne přepis.
* V prostoru na serveru se odsud jen ptá brány, kde který chat běží
  (`stav_prostoru`), a žádá o převzetí (`na_server`); tab, který si bere
  počítač, zavře `uvolnit_v_prostoru`.

Stav na počítači: ~/.claude/hub-zrcadlo.json — kolik řádků kterého chatu už
server má (a kde v souboru končí), a které chaty drží server.
"""
import json
import os
import re
import socket
import threading
import time
import urllib.error
import urllib.request

from . import account, core, prenos

STAV = os.path.join(core.CLAUDE_DIR, "hub-zrcadlo.json")
KAZDYCH = 10              # s mezi kopírováním
PRVNI_DNI = 3             # při prvním zapnutí se kopírují chaty z posledních dnů
DAVKA = 5 * 1024 * 1024   # bajtů řádků v jednom požadavku
SEZNAM_TTL = 30           # s — seznam chatů na serveru pro panel
SESSION_ID = prenos.SESSION_ID

_LOCK = threading.Lock()
_BEH = {"posledni": 0.0, "chyba": "", "pocet": 0, "uvolnit": set(), "pustene": [], "seznam": None,
        "seznam_cas": 0.0}
_STARTED = False
HUB = None                # server.HUB — taby (chat_tabs, close, broadcast)
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def zapnuto():
    """Kopírovat? Jen hub na počítači přihlášený k bráně, ne zkušební."""
    return (not core.on_gateway() and not core.TEST_MODE
            and bool(core.CONFIG.get("gw_token")) and bool(account._base())
            and core.CONFIG.get("zrcadlo", True) is not False)


# ── stav na disku ────────────────────────────────────────────────────────────
def _nacti():
    try:
        with open(STAV, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    data.setdefault("chaty", {})
    data.setdefault("od", time.time() - PRVNI_DNI * 86400)
    data.setdefault("server", [])          # chaty, které teď drží server
    return data


def _uloz(data):
    tmp = STAV + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    os.replace(tmp, STAV)


# ── čtení přepisů ────────────────────────────────────────────────────────────
def _ofset_radku(path, n):
    """Bajt, kde v souboru začíná řádek číslo `n` (po n celých řádcích)."""
    if n <= 0:
        return 0
    pos, videno = 0, 0
    with open(path, "rb") as fh:
        while True:
            kus = fh.read(1024 * 1024)
            if not kus:
                return pos
            i = 0
            while videno < n:
                j = kus.find(b"\n", i)
                if j < 0:
                    break
                videno += 1
                i = j + 1
            if videno >= n:
                return pos + i
            pos += len(kus)


def _nove_radky(path, ofset, limit):
    """Celé řádky od bajtu `ofset` (nejvýš ~limit bajtů) → (řádky, nový ofset).
    Useknutý konec (Claude zrovna zapisuje) počká na příště."""
    out, velikost = [], 0
    with open(path, "rb") as fh:
        fh.seek(ofset)
        for raw in fh:
            if not raw.endswith(b"\n"):
                break
            if velikost + len(raw) > limit and out:
                break
            out.append(raw[:-1].decode("utf-8", "replace"))
            velikost += len(raw)
            ofset += len(raw)
    return out, ofset


def _kandidati(data):
    """Přepisy chatů změněné od posledního kopírování: [(id, cesta, velikost)]."""
    root = os.path.join(core.CLAUDE_DIR, "projects")
    out = []
    try:
        slozky = [e for e in os.scandir(root) if e.is_dir(follow_symlinks=False)]
    except OSError:
        return out
    server = set(data["server"])
    for slozka in slozky:
        try:
            soubory = list(os.scandir(slozka.path))
        except OSError:
            continue
        for f in soubory:
            cid = f.name[:-6] if f.name.endswith(".jsonl") else ""
            if not SESSION_ID.fullmatch(cid) or cid in server or not f.is_file(follow_symlinks=False):
                continue
            st = f.stat(follow_symlinks=False)
            known = data["chaty"].get(cid)
            if not known and st.st_mtime < data["od"]:
                continue
            if known and known.get("velikost") == st.st_size:
                continue
            out.append((cid, f.path, st.st_size, st.st_mtime))
    out.sort(key=lambda x: -x[3])
    return out


# ── smyčka na počítači ──────────────────────────────────────────────────────
def start(hub):
    """Spustí kopírování (jednou za běh). Na serveru a ve zkoušce nic."""
    global _STARTED, HUB
    HUB = hub
    if core.on_gateway() or core.TEST_MODE:
        return
    with _LOCK:
        if _STARTED:
            return
        _STARTED = True
    threading.Thread(target=_smycka, daemon=True, name="zrcadlo").start()


def _smycka():
    time.sleep(8)
    while True:
        try:
            if zapnuto():
                kolo()
        except Exception as exc:                  # smyčka nesmí umřít
            with _LOCK:
                _BEH["chyba"] = str(exc)[:200]
            core.log_error("kopie chatů na server", exc)
        time.sleep(KAZDYCH)


def _tab_chatu(chat):
    """(id tabu, session) tabu, ve kterém chat běží, nebo (None, None)."""
    if not HUB:
        return None, None
    sid = HUB.chat_tabs().get(chat)
    return (sid, HUB.sessions.get(sid)) if sid is not None else (None, None)


def _pracuje(session):
    from . import mcp_tools
    try:
        cesta = core.transcript_for(session)
        return bool(cesta and mcp_tools._pracuje(cesta))
    except Exception:
        return False


def _pust_taby(data):
    """Chaty, které si bere telefon: tab se zavře, až Claude dopíše odpověď.
    Vrací chaty, které jde v tomhle kole pustit (poslední řádky jdou s nimi)."""
    with _LOCK:
        chce = set(_BEH["uvolnit"])
    pustit = []
    for chat in chce:
        sid, session = _tab_chatu(chat)
        if session is not None:
            if _pracuje(session):
                continue                          # dopisuje — příště
            titulek = session.title
            HUB.close(sid)
            HUB.broadcast({"t": "zrcadlo-odchod", "chat": chat, "title": titulek, "tab": sid})
        pustit.append(chat)
    return pustit


def kolo():
    """Jedno kopírování: nové řádky změněných chatů, uvolnění, co chce telefon."""
    data = _nacti()
    pustit = _pust_taby(data)
    davka, velikost, mapa = [], 0, {}
    for cid, path, size, _mtime in _kandidati(data) + [(c, *_cesta_a_velikost(c)) for c in pustit]:
        if cid in mapa or not path:
            continue
        k = data["chaty"].get(cid) or {"radky": 0, "ofset": 0}
        try:
            radky, ofset = _nove_radky(path, int(k.get("ofset") or 0), max(1, DAVKA - velikost))
        except OSError:
            continue
        mapa[cid] = (path, size, ofset, len(radky), k)
        davka.append({"id": cid, "od": int(k.get("radky") or 0), "radky": radky,
                      "titulek": _titulek(path), "cwd": prenos._cwd_z_prepisu(path)})
        velikost += sum(len(r) for r in radky)
        if velikost >= DAVKA or len(davka) >= 40:
            break
    from . import pocitac
    res, err, _kind = account._call("/gw/zrcadlo/sync", token=core.CONFIG.get("gw_token", ""),
                                    timeout=60, payload={
                                        "pocitac": pocitac.device_id(), "jmeno": socket.gethostname(),
                                        "chaty": davka})
    if res is None:
        with _LOCK:
            _BEH["chyba"] = err
        return
    hotove = []                               # pouštěné chaty, které server má celé
    for cid, r in (res.get("chaty") or {}).items():
        if cid not in mapa:
            continue
        path, size, ofset, poslano, k = mapa[cid]
        stav = r.get("stav")
        if stav == "ok":
            konec = ofset >= size
            data["chaty"][cid] = {"radky": int(r.get("radky") or 0), "ofset": ofset,
                                  # Velikost jen když je všechno venku — jinak příště znovu.
                                  "velikost": size if konec else -1}
            if konec and cid in pustit:
                hotove.append(cid)
        elif stav == "od":
            # Server má jiný počet řádků (první kopie, ztracená odpověď):
            # od jeho čísla se pošle znovu.
            n = int(r.get("radky") or 0)
            data["chaty"][cid] = {"radky": n, "ofset": _ofset_radku(path, n), "velikost": -1}
        elif stav == "server":
            if cid not in data["server"]:
                data["server"].append(cid)
    # Pustit až po potvrzeném zápisu posledních řádků — jinak by se konec
    # odpovědi na serveru ztratil. Co se nedopsalo, pustí se příště.
    if hotove:
        r2, err, _kind = account._call("/gw/zrcadlo/sync", token=core.CONFIG.get("gw_token", ""),
                                       timeout=60, payload={
                                           "pocitac": pocitac.device_id(), "jmeno": socket.gethostname(),
                                           "chaty": [], "uvolneno": hotove})
        if r2 is None:
            hotove = []
        for cid in hotove:
            if cid not in data["server"]:
                data["server"].append(cid)
    data["server"] = data["server"][-500:]
    _uloz(data)
    with _LOCK:
        _BEH["uvolnit"] = set(res.get("uvolnit") or []) - set(hotove)
        _BEH["posledni"] = time.time()
        _BEH["chyba"] = ""
        _BEH["pocet"] = len(data["chaty"])
        if pustit:
            _BEH["seznam_cas"] = 0.0


def _cesta_a_velikost(chat):
    path, _sub = prenos.najdi(core.CLAUDE_DIR, chat)
    try:
        st = os.stat(path) if path else None
    except OSError:
        st = None
    return (path, st.st_size if st else 0, st.st_mtime if st else 0)


def _titulek(path):
    from . import chats
    try:
        return chats.title_for(path)
    except Exception:
        return ""


# ── pro stránku na počítači ─────────────────────────────────────────────────
def prehled(obnovit=False):
    """Stav kopírování a chaty na serveru (pro panel a Nastavení → Účet)."""
    zap = zapnuto()
    data = _nacti() if zap else {"server": [], "chaty": {}}
    seznam = None
    if zap:
        with _LOCK:
            cerstvy = time.time() - _BEH["seznam_cas"] < SEZNAM_TTL and not obnovit
            seznam = _BEH["seznam"] if cerstvy else None
        if seznam is None:
            res, err, _kind = account._call("/gw/zrcadlo/seznam", token=core.CONFIG.get("gw_token", ""),
                                            timeout=20)
            seznam = (res or {}).get("chaty") if res else None
            with _LOCK:
                if seznam is not None:
                    _BEH["seznam"], _BEH["seznam_cas"] = seznam, time.time()
                else:
                    _BEH["chyba"] = err
                    seznam = _BEH["seznam"]
    with _LOCK:
        beh = {"posledni": int(_BEH["posledni"]), "chyba": _BEH["chyba"], "pocet": _BEH["pocet"]}
    return {"zapnuto": zap, "nastaveno": core.CONFIG.get("zrcadlo", True) is not False,
            **beh, "server": data["server"], "chaty": seznam or []}


def prevzit(chat):
    """Počítač si bere chat, který drží server: server ho pustí, přepis se
    stáhne a zapíše sem. → {ok, chat, cwd, title}."""
    chat = str(chat or "")
    if not SESSION_ID.fullmatch(chat):
        raise ValueError("Neplatný chat.")
    if not zapnuto():
        raise ValueError("Appka není přihlášená k serveru.")
    token = core.CONFIG.get("gw_token", "")
    from . import pocitac
    res, err, _kind = account._call("/gw/zrcadlo/prevzit", token=token, timeout=200,
                                    payload={"chat": chat, "pocitac": pocitac.device_id()})
    if res is None or not res.get("ok"):
        raise ValueError((res or {}).get("error") or err or "Server chat nepustil.")
    radky, od, celkem = [], 0, None
    while celkem is None or od < celkem:
        d, err, _kind = account._call("/gw/zrcadlo/stahnout", token=token, timeout=120,
                                      payload={"chat": chat, "od": od})
        if d is None or not d.get("ok"):
            raise ValueError((d or {}).get("error") or err or "Chat se nepodařilo stáhnout.")
        celkem = int(d.get("celkem") or 0)
        kus = d.get("radky") or []
        if not kus:
            break
        radky += kus
        od += len(kus)
    stary, _sub = prenos.najdi(core.CLAUDE_DIR, chat)
    cwd = prenos._cwd_z_prepisu(stary) if stary else ""
    if not cwd or not os.path.isdir(cwd):
        cwd = res.get("cwd_pc") if os.path.isdir(res.get("cwd_pc") or "") else core.HOME
    folder = os.path.dirname(stary) if stary else os.path.join(core.CLAUDE_DIR, "projects", prenos.slug(cwd))
    os.makedirs(folder, exist_ok=True)
    cil = os.path.join(folder, chat + ".jsonl")
    if stary:
        try:
            import shutil
            shutil.copyfile(stary, stary + ".bak")
        except OSError:
            pass
    import io
    tmp = cil + ".zrcadlo-tmp"
    with open(tmp, "wb") as fh:
        surove = b"".join(r.encode("utf-8") + b"\n" for r in radky)
        for kus in prenos._prepis_cwd(io.BytesIO(surove), cwd):
            fh.write(kus)
    os.replace(tmp, cil)
    size = os.path.getsize(cil)
    data = _nacti()
    data["server"] = [c for c in data["server"] if c != chat]
    data["chaty"][chat] = {"radky": len(radky), "ofset": size, "velikost": size}
    _uloz(data)
    with _LOCK:
        _BEH["seznam_cas"] = 0.0
    core.log(f"zrcadlo: chat {chat[:8]} převzat ze serveru ({len(radky)} řádků)")
    return {"ok": True, "chat": chat, "cwd": cwd, "title": _titulek(cil) or res.get("titulek") or "Chat"}


def nastav(zapnout):
    core.save_config({"zrcadlo": bool(zapnout)})
    return prehled()


# ── v prostoru na serveru ───────────────────────────────────────────────────
_PROSTOR = {"cas": 0.0, "chaty": {}}


def _brana(op, args):
    """Volání brány z prostoru (žeton prostoru, jen loopback brány)."""
    url = os.environ.get("HUB_POCITAC_URL", "").rstrip("/")
    token = os.environ.get("HUB_POCITAC_TOKEN", "")
    if not core.on_gateway() or not url or not token:
        return None
    body = json.dumps({"op": op, "args": args}).encode("utf-8")
    req = urllib.request.Request(url + "/gw/pocitac/volani", data=body, method="POST",
                                 headers={"Content-Type": "application/json", "X-Hub-Pocitac": token})
    try:
        with _OPENER.open(req, timeout=20) as res:
            return json.loads(res.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        if exc.code == 404:               # stará brána tuhle adresu nezná
            return {"ok": False, "error": "Tohle bude fungovat, až se server aktualizuje."}
        return None
    except (urllib.error.URLError, OSError, ValueError):
        return None


def stav_prostoru():
    """Kde běží chaty, které sem kopíruje počítač: {id: {drzi, jmeno, online, uvolnit}}."""
    if time.time() - _PROSTOR["cas"] < 5:
        return _PROSTOR["chaty"]
    out = _brana("zrcadla", {})
    if out and out.get("ok"):
        _PROSTOR["chaty"], _PROSTOR["cas"] = out.get("chaty") or {}, time.time()
    return _PROSTOR["chaty"]


def na_server(chat, vynutit=False):
    """Telefon chce chat, který drží počítač → {stav: "server"|"ceka", pocitac}."""
    out = _brana("zrcadlo-na-server", {"chat": str(chat or ""), "vynutit": bool(vynutit)})
    if not out or not out.get("ok"):
        raise ValueError((out or {}).get("error") or "Brána teď neodpovídá, zkus to za chvíli.")
    _PROSTOR["cas"] = 0.0
    return out


def uvolnit_v_prostoru(hub, chat, cekat=150):
    """Počítač si bere chat, který běží tady: tab se zavře, až Claude dopíše
    odpověď. False = pořád pracuje (počítač to zkusí znovu)."""
    chat = str(chat or "")
    if not re.fullmatch(SESSION_ID.pattern, chat):
        return False
    konec = time.time() + cekat
    while True:
        sid = hub.chat_tabs().get(chat)
        session = hub.sessions.get(sid) if sid is not None else None
        if session is None:
            return True
        if not _pracuje(session):
            hub.broadcast({"t": "zrcadlo-odchod", "chat": chat, "title": session.title, "kam": "pc", "tab": sid})
            hub.close(sid)
            return True
        if time.time() > konec:
            return False
        time.sleep(2)
