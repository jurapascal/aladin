"""
Místo chatu na počítači: Počítač, nebo prostor na serveru (osobní, firma, cizí).

Každý chat má své místo (Jura, 6. 11. 2026): vybírá se u nového chatu a jakmile
v něm padne první zpráva, je pevné. Starší chat má místo podle toho, odkud je —
z tohohle počítače = Počítač, převzatý ze serveru = jeho prostor na serveru.

* Tab: `~/.claude/hub-misto/<tab>.json` — čte ho most ke serveru
  (tools/server_napojeni_mcp.py) u každé zprávy podle `HUB_TAB`, takže Claude
  v tabu má napojení a Obsidian ze „svého“ místa, ať je výchozí jakékoli.
* Chat: `~/.claude/hub-chat-misto.json` {id konverzace: místo} — po znovuotevření
  chatu dostane tab zase jeho místo.
* Výchozí místo pro nové chaty je dál v hub-config (`server_napojeni`,
  `gw_server`, `gw_space`, `gw_firma`) — mění ho přepínač na úvodní obrazovce.

Místo: {"kde": "pc" | "server", "server": adresa, "prostor": id (0 = vlastní),
        "firma": zkratka, "nazev": jméno prostoru / firmy}
"""
import json
import os
import re
import threading
import time

from . import core

DIR = os.path.join(core.CLAUDE_DIR, "hub-misto")
CHATY = os.path.join(core.CLAUDE_DIR, "hub-chat-misto.json")
MAX_CHATU = 5000
_LOCK = threading.Lock()


def soubor_tabu(tag):
    """Soubor místa tabu. Tag je `instance:id` — dvojtečka do jména souboru na Windows nesmí."""
    return os.path.join(DIR, re.sub(r"[^A-Za-z0-9_.-]", "-", str(tag or "")) + ".json")


def _cisty(m):
    """Místo jen se známými klíči (cokoli jiného = Počítač)."""
    m = m if isinstance(m, dict) else {}
    if m.get("kde") != "server" or not str(m.get("server") or "").startswith("https://"):
        return {"kde": "pc"}
    try:
        prostor = int(m.get("prostor") or 0)
    except (TypeError, ValueError):
        prostor = 0
    return {"kde": "server", "server": str(m["server"]).rstrip("/"), "prostor": prostor,
            "firma": "" if prostor else str(m.get("firma") or "")[:40],
            "nazev": str(m.get("nazev") or "")[:80]}


def vychozi():
    """Místo pro nový chat podle přepínače na úvodní obrazovce."""
    cfg = core.CONFIG
    if cfg.get("server_napojeni", True) is False or not cfg.get("gw_token") or not cfg.get("gw_server"):
        return {"kde": "pc"}
    prostor = int(cfg.get("gw_space") or 0)
    firma = "" if prostor else str(cfg.get("gw_firma") or "")
    return _cisty({"kde": "server", "server": cfg.get("gw_server"), "prostor": prostor, "firma": firma,
                   "nazev": cfg.get("gw_space_name") if prostor else cfg.get("gw_firma_name") or ""})


def _nacti_chaty():
    try:
        with open(CHATY, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def pro_chat(chat):
    """Uložené místo konverzace, nebo None (starší chat bez záznamu)."""
    if not chat:
        return None
    m = _nacti_chaty().get(str(chat))
    return _cisty(m) if isinstance(m, dict) else None


def vsechny_chaty():
    """{id konverzace: místo} — pro seznam chatů (barva podle místa)."""
    return {k: _cisty(v) for k, v in _nacti_chaty().items() if isinstance(v, dict)}


def uloz_chat(chat, m):
    if not core.SESSION_ID.fullmatch(str(chat or "")):
        return
    with _LOCK:
        data = _nacti_chaty()
        data[str(chat)] = {**_cisty(m), "ts": int(time.time())}
        if len(data) > MAX_CHATU:
            for k, _v in sorted(data.items(), key=lambda kv: kv[1].get("ts", 0))[:len(data) - MAX_CHATU]:
                data.pop(k, None)
        tmp = CHATY + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(data, fh)
            os.replace(tmp, CHATY)
        except OSError as exc:
            core.log(f"místo chatu se nepodařilo uložit: {exc}", "warn")


def zapis_tab(tag, m):
    """Místo tabu pro most ke serveru. Vrací zapsané místo."""
    m = _cisty(m)
    try:
        os.makedirs(DIR, mode=0o700, exist_ok=True)
        cil = soubor_tabu(tag)
        tmp = cil + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(m, fh)
        os.replace(tmp, cil)
    except OSError as exc:
        core.log(f"místo tabu se nepodařilo zapsat: {exc}", "warn")
    return m


def smaz_tab(tag):
    try:
        os.remove(soubor_tabu(tag))
    except OSError:
        pass


def uklid(ziva_tagy):
    """Soubory tabů, které už neběží (po pádu, starší instance)."""
    try:
        jmena = os.listdir(DIR)
    except OSError:
        return
    nech = {os.path.basename(soubor_tabu(t)) for t in ziva_tagy}
    for jmeno in jmena:
        if jmeno.endswith(".json") and jmeno not in nech:
            try:
                if time.time() - os.path.getmtime(os.path.join(DIR, jmeno)) > 3600:
                    os.remove(os.path.join(DIR, jmeno))
            except OSError:
                pass


def pro_otevreni(resume, chce=None):
    """Místo nového tabu: pokračování konverzace = její uložené místo (starší chat
    podle toho, odkud je), nový chat = co vybral člověk, jinak výchozí."""
    if resume:
        m = pro_chat(resume)
        if m:
            return m
        try:
            from . import zrcadlo
            server = zrcadlo.byl_na_serveru(resume)
        except Exception:
            server = None
        return _cisty(server) if server else {"kde": "pc"}
    if isinstance(chce, dict) and chce:
        return _cisty(chce)
    return vychozi()
