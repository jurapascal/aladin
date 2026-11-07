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
PRVNI_DNI = 60            # kopírují se chaty z posledních dnů (jako seznam na bráně, SEZNAM_DNI)
DAVKA = 5 * 1024 * 1024   # bajtů řádků v jednom požadavku
SEZNAM_TTL = 30           # s — seznam chatů na serveru pro panel
SESSION_ID = prenos.SESSION_ID

_LOCK = threading.Lock()
_BEH = {"posledni": 0.0, "chyba": "", "pocet": 0, "uvolnit": set(), "pustene": [], "seznam": None,
        "seznam_cas": 0.0, "doruceno": [], "videno": set()}
_STARTED = False
HUB = None                # server.HUB — taby (chat_tabs, close, broadcast)
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def pripojeno():
    """Počítač přihlášený k bráně (ne zkušební hub): chaty ze serveru se ukazují
    v seznamu a dají se převzít — i když se chaty z počítače na server nekopírují."""
    return (not core.on_gateway() and not core.TEST_MODE
            and bool(core.CONFIG.get("gw_token")) and bool(account._base()))


def na_serveru():
    """V přepínači místa je vybraný prostor na serveru (ne „Počítač“)."""
    return core.CONFIG.get("server_napojeni", True) is not False


def zapnuto():
    """Kopírovat chaty z počítače na server? Přihlášený počítač a nevypnutá kopie.
    Které chaty, rozhoduje jejich místo (hub/misto.py, _kandidati): chat
    s místem „Počítač“ zůstává jen tady (Jura, 6. 11. 2026)."""
    return pripojeno() and core.CONFIG.get("zrcadlo", True) is not False


def byl_na_serveru(chat):
    """Starší chat bez uloženého místa, který se už kopíroval na server (nebo ho
    server drží): jeho místo je osobní prostor na serveru. Jinak None."""
    if not pripojeno():
        return None
    data = _nacti()
    if chat in data["chaty"] or chat in data["server"]:
        return {"kde": "server", "server": account._base(), "prostor": 0}
    return None


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
    # Do 2.130 se kopírovaly jen chaty z posledních 3 dnů od zapnutí — na serveru pak
    # chyběla starší historie. Jednou se hranice posune na PRVNI_DNI zpátky.
    if not data.get("od_v2"):
        data["od"] = min(float(data["od"]), time.time() - PRVNI_DNI * 86400)
        data["od_v2"] = 1
    data.setdefault("server", [])          # chaty, které teď drží server
    data.setdefault("smazat", [])          # smazané chaty, které server ještě nemá odepsané
    data.setdefault("sdilene", [])         # chaty sdílené z PC — kopírují se i s vypnutou kopií
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


def _kandidati(data, jen=None):
    """Přepisy chatů změněné od posledního kopírování: [(id, cesta, velikost)].
    `jen` = jen tyhle chaty (sdílené); s vypnutou kopií se berou jen sdílené."""
    root = os.path.join(core.CLAUDE_DIR, "projects")
    out = []
    try:
        slozky = [e for e in os.scandir(root) if e.is_dir(follow_symlinks=False)]
    except OSError:
        return out
    server = set(data["server"])
    sdilene = set(data.get("sdilene") or [])
    if jen is None and not zapnuto():
        jen = sdilene
    from . import misto
    mista = misto.vsechny_chaty()
    base = account._base()
    for slozka in slozky:
        try:
            soubory = list(os.scandir(slozka.path))
        except OSError:
            continue
        for f in soubory:
            cid = f.name[:-6] if f.name.endswith(".jsonl") else ""
            if not SESSION_ID.fullmatch(cid) or cid in server or not f.is_file(follow_symlinks=False):
                continue
            if jen is not None and cid not in jen:
                continue
            st = f.stat(follow_symlinks=False)
            known = data["chaty"].get(cid)
            if cid in sdilene:
                # Sdílený chat: na server vždycky (ostatní ho jinak neuvidí), celý.
                if not (known and known.get("velikost") == st.st_size):
                    out.append((cid, f.path, st.st_size, st.st_mtime))
                continue
            # Místo chatu: na server jen chaty s místem na tomhle serveru; starší chat
            # bez místa jen když se kopíroval už dřív.
            m = mista.get(cid)
            if m is not None and (m.get("kde") != "server" or account.normalize(m.get("server") or "") != base):
                continue
            if m is None and not known:
                continue
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
            if zapnuto() or (pripojeno() and _nacti().get("sdilene")):
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


def smazano(chat):
    """Chat smazaný na počítači — kopie na serveru má zmizet taky. Zapíše se do
    fronty (přežije vypnutý počítač nebo server) a odešle se v dalším kole."""
    chat = str(chat or "")
    if not SESSION_ID.fullmatch(chat) or not zapnuto():
        return
    with _LOCK:
        data = _nacti()
        if chat not in data["smazat"]:
            data["smazat"].append(chat)
        data["smazat"] = data["smazat"][-200:]
        data["chaty"].pop(chat, None)
        _uloz(data)
    threading.Thread(target=_posli_smazane, daemon=True, name="zrcadlo-smazat").start()


def _posli_smazane():
    """Odešle frontu smazaných chatů bráně; co se nepovede, zůstane na příště."""
    with _LOCK:
        data = _nacti()
        fronta = list(data["smazat"])
    hotovo = []
    for cid in fronta:
        res, _err, _kind = account._call("/gw/zrcadlo/smazat", token=core.CONFIG.get("gw_token", ""),
                                         timeout=40, payload={"chat": cid})
        if res is not None and res.get("ok"):
            hotovo.append(cid)
    if hotovo:
        with _LOCK:
            data = _nacti()
            data["smazat"] = [c for c in data["smazat"] if c not in hotovo]
            _uloz(data)
        with _LOCK:
            _BEH["seznam_cas"] = 0.0


SIROTCI_KAZDYCH = 60      # s — jak často se porovnává server s tím, co je na počítači
SIROTCI_NAJEDNOU = 25     # nejvíc smazaných kopií za jedno porovnání
_SIROTCI = {"cas": 0.0}


def uklid_sirotku():
    """Kopie na serveru, které už na počítači nemají přepis (smazal jsem je tady,
    ještě než to hub uměl hlásit, nebo ručně ze složky): zmizí ze serveru taky.
    Bere se jen to, co sem kopíroval právě tenhle počítač (`drzi` = pc a stejné
    id počítače) a co tu doopravdy nikde není — přesun do koše na serveru jde
    vrátit. Když složka s přepisy na počítači není vidět, nedělá se nic."""
    if time.time() - _SIROTCI["cas"] < SIROTCI_KAZDYCH:
        return
    _SIROTCI["cas"] = time.time()
    root = os.path.join(core.CLAUDE_DIR, "projects")
    if not os.path.isdir(root):
        return
    from . import pocitac
    res, _err, _kind = account._call("/gw/zrcadlo/seznam", token=core.CONFIG.get("gw_token", ""), timeout=20)
    seznam = (res or {}).get("chaty") if res else None
    if not seznam:
        return
    ja = pocitac.device_id()
    data = _nacti()
    fronta = set(data["smazat"])
    nove = []
    for c in seznam:
        cid = str(c.get("id") or "")
        if not SESSION_ID.fullmatch(cid) or c.get("drzi") != "pc" or c.get("pocitac") != ja or cid in fronta:
            continue
        if prenos.najdi(core.CLAUDE_DIR, cid)[0] or _tab_chatu(cid)[0] is not None:
            continue
        nove.append(cid)
        if len(nove) >= SIROTCI_NAJEDNOU:
            break
    if not nove:
        return
    with _LOCK:
        data = _nacti()
        data["smazat"] = (data["smazat"] + [c for c in nove if c not in data["smazat"]])[-200:]
        for cid in nove:
            data["chaty"].pop(cid, None)
        _uloz(data)


_KOLO = threading.Lock()


def sdilet(chat):
    """Chat sdílený z appky na PC: kopíruje se na server i s vypnutou kopií chatů
    a hned teď (na pozadí, jen on) — okno sdílení na to nečeká."""
    if not SESSION_ID.fullmatch(str(chat or "")) or not pripojeno():
        return
    with _LOCK:
        data = _nacti()
        if chat not in data["sdilene"]:
            data["sdilene"] = (data["sdilene"] + [chat])[-300:]
            _uloz(data)

    def hned():
        try:
            kolo(jen={chat})
        except Exception as exc:
            core.log_error("kopie sdíleného chatu", exc)
    threading.Thread(target=hned, daemon=True, name="zrcadlo-sdileny").start()


def kolo(jen=None):
    """Jedno kopírování: nové řádky změněných chatů, uvolnění, co chce telefon.
    `jen` = zkopírovat jen tyhle chaty (sdílení), bez úklidu."""
    with _KOLO:
        return _kolo(jen)


def _kolo(jen=None):
    if jen is None:
        uklid_sirotku()
        if _nacti().get("smazat"):
            _posli_smazane()
    data = _nacti()
    pustit = _pust_taby(data) if jen is None else []
    davka, velikost, mapa = [], 0, {}
    for cid, path, size, _mtime in _kandidati(data, jen) + [(c, *_cesta_a_velikost(c)) for c in pustit]:
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
    with _LOCK:
        doruceno = list(_BEH["doruceno"])
    res, err, _kind = account._call("/gw/zrcadlo/sync", token=core.CONFIG.get("gw_token", ""),
                                    timeout=60, payload={
                                        "pocitac": pocitac.device_id(), "jmeno": socket.gethostname(),
                                        "chaty": davka, "doruceno": doruceno})
    if res is None:
        with _LOCK:
            _BEH["chyba"] = err
        return
    with _LOCK:
        _BEH["doruceno"] = [d for d in _BEH["doruceno"] if d not in doruceno]
    for z in res.get("zpravy") or []:
        if isinstance(z, dict):
            _prijmi_zpravu(z)
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
    data["sdilene"] = _nacti().get("sdilene") or []     # mezitím mohl přibýt sdílený chat
    _uloz(data)
    with _LOCK:
        _BEH["uvolnit"] = set(res.get("uvolnit") or []) - set(hotove)
        _BEH["posledni"] = time.time()
        _BEH["chyba"] = ""
        _BEH["pocet"] = len(data["chaty"])
        if pustit:
            _BEH["seznam_cas"] = 0.0


# ── Zprávy od ostatních do chatu sdíleného z počítače ─────────────────────────
def _prijmi_zpravu(z):
    """Zpráva kolegy / hosta z odkazu (gateway/zrcadlo.py zprava_pro_pocitac) do
    chatu, který běží tady. Zavřený chat se otevře. Doručení se potvrdí příště."""
    zid, chat = str(z.get("id") or ""), str(z.get("chat") or "")
    if not zid or not SESSION_ID.fullmatch(chat):
        return
    with _LOCK:
        if zid in _BEH["videno"]:
            return
        _BEH["videno"].add(zid)
        if len(_BEH["videno"]) > 500:
            _BEH["videno"] = {zid}
    threading.Thread(target=_dorucit, args=(zid, chat, str(z.get("text") or ""), str(z.get("kdo") or "")),
                     daemon=True, name="sdileni-zprava").start()


def _dorucit(zid, chat, text, kdo):
    import re as _re
    text = _re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text).strip()
    if not text or not HUB:
        return
    _sid, session = _tab_chatu(chat)
    if session is None:
        path, _sub = prenos.najdi(core.CLAUDE_DIR, chat)
        if not path:
            return
        cwd = prenos._cwd_z_prepisu(path)
        if not cwd or not os.path.isdir(cwd):
            cwd = core.HOME
        try:
            HUB.open("project", cwd, _titulek(path) or "Sdílený chat", 120, 32, agent="claude", resume=chat)
        except Exception as exc:
            core.log_error("sdílený chat se nepodařilo otevřít", exc)
            return
        for _ in range(40):
            time.sleep(0.5)
            _sid, session = _tab_chatu(chat)
            if session is not None:
                break
        time.sleep(4)                         # ať Claude Code naběhne, než dostane text
    if session is None or getattr(session, "exited", False):
        return
    kdo = _re.sub(r"[\r\n\[\]]", " ", kdo).strip()[:60]
    body = (f"[{kdo}] " if kdo else "") + text
    data = ("\x1b[200~" + body + "\x1b[201~") if "\n" in body else body
    session.write(data.encode("utf-8"))
    threading.Timer(0.25, lambda: None if session.exited else session.write(b"\r")).start()
    with _LOCK:
        _BEH["doruceno"].append(zid)
    core.log(f"sdílení: zpráva od {kdo or '?'} doručena do chatu {chat[:8]}")


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
    """Stav kopírování a chaty na serveru (pro panel a Nastavení → Účet).
    Seznam chatů na serveru jde i s vypnutou kopií: co člověk dělal na serveru,
    má vidět i v appce na počítači (Adam Uher, 6. 11. 2026)."""
    zap = zapnuto()
    pripojen = pripojeno()
    data = _nacti() if pripojen else {"server": [], "chaty": {}}
    seznam = None
    if pripojen:
        base = account._base()
        with _LOCK:
            # Seznam patří serveru, ze kterého přišel — po přepnutí serveru hned nový.
            cerstvy = (time.time() - _BEH["seznam_cas"] < SEZNAM_TTL and not obnovit
                       and _BEH.get("seznam_server") == base)
            seznam = _BEH["seznam"] if cerstvy else None
        if seznam is None:
            res, err, _kind = account._call("/gw/zrcadlo/seznam", token=core.CONFIG.get("gw_token", ""),
                                            timeout=20)
            seznam = (res or {}).get("chaty") if res else None
            with _LOCK:
                if seznam is not None:
                    _BEH["seznam"], _BEH["seznam_cas"], _BEH["seznam_server"] = seznam, time.time(), base
                else:
                    _BEH["chyba"] = err
                    seznam = _BEH["seznam"] if _BEH.get("seznam_server") == base else []
    with _LOCK:
        beh = {"posledni": int(_BEH["posledni"]), "chyba": _BEH["chyba"], "pocet": _BEH["pocet"]}
    return {"zapnuto": zap, "pripojeno": pripojen, "na_serveru": na_serveru(),
            "nastaveno": core.CONFIG.get("zrcadlo", True) is not False,
            **beh, "server": data["server"], "chaty": seznam or []}


def prevzit(chat):
    """Počítač si bere chat, který drží server: server ho pustí, přepis se
    stáhne a zapíše sem. → {ok, chat, cwd, title}."""
    chat = str(chat or "")
    if not SESSION_ID.fullmatch(chat):
        raise ValueError("Neplatný chat.")
    if not pripojeno():
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
    # Místo chatu: zůstává na serveru (osobní, nebo firma podle cesty na serveru).
    try:
        from . import misto
        with _LOCK:
            hit = next((c for c in (_BEH["seznam"] or []) if c.get("id") == chat), {})
        misto.uloz_chat(chat, {"kde": "server", "server": account._base(), "prostor": 0,
                               "firma": hit.get("firma") or "", "nazev": hit.get("firma_nazev") or ""})
    except Exception as exc:
        core.log_error("místo převzatého chatu", exc)
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
