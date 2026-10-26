"""Gemini (Google AI Pro) přes Antigravity CLI — stav a napojení pro Clauda.

Gemini tu je dvakrát:

* jako **agent v tabu** (katalog v agents.py, id `antigravity`) — člověk si
  s ním povídá sám,
* jako **pomocník Clauda** — MCP server tools/gemini_mcp.py (nástroje
  mcp__gemini__*: obrázek, přepis nahrávky, popis videa, obecné zadání)
  a příkaz `gemini-pomoc` v terminálu. Claude tak posílá obrázky, zvuk, video
  a hromadnou práci tam, kde je jiný týdenní limit než ten jeho.

Tenhle modul drží to druhé v souladu se strojem: když je `agy` nainstalovaný
a aspoň jednou spuštěný, tedy přihlášený (a člověk to nevypnul), zaregistruje MCP server přes
`claude mcp add` a dá `gemini-pomoc` do ~/.local/bin; když `agy` zmizí,
uklidí obojí. Jak se `agy` volá, ví jen tools/gemini_pomoc.py — tady se
načte jako modul, ať se cesty a pravidla nepíšou dvakrát.
"""
import importlib.util
import os
import subprocess
import sys
import threading

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKRIPT_MCP = os.path.join(ROOT, "tools", "gemini_mcp.py")
SKRIPT_POMOC = os.path.join(ROOT, "tools", "gemini_pomoc.py")
MCP_NAME = "gemini"
PRIKAZ = os.path.join(os.path.expanduser("~"), ".local", "bin", "gemini-pomoc")

_GP = None
_LOCK = threading.Lock()


def gp():
    """tools/gemini_pomoc.py jako modul (jednou za běh)."""
    global _GP
    if _GP is None:
        spec = importlib.util.spec_from_file_location("aladin_gemini_pomoc", SKRIPT_POMOC)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _GP = mod
    return _GP


def prihlaseni():
    """True / False / None (nedá se poznat) — viz gemini_pomoc.prihlaseni."""
    try:
        return gp().prihlaseni()
    except Exception:
        return None


def modely(cerstve=False):
    """[[popisek, id]] pro chip modelu v tabu s Gemini."""
    try:
        mod = gp()
        # Spouštět `agy models` jen u jistě přihlášeného: bez přihlášení by
        # se mohl pokusit otevřít prohlížeč s přihlášením, a to z kontroly
        # agentů v Nastavení nikdo nečeká.
        if cerstve and mod.najdi_agy() and mod.prihlaseni() is True:
            ids = mod.modely(cerstve=True)
        else:
            ids = mod.modely()
    except Exception:
        return []
    # Flash první (výchozí, největší limit), v rodině nejdřív „-medium" — s tím jede agy sám.
    ids = sorted(ids, key=lambda m: ("flash" not in m, "medium" not in m, m))
    return [[mod.popis_modelu(m), m] for m in ids]


def _zapnuto():
    from . import core
    return core.CONFIG.get("gemini_pomocnik", True) is not False


def _nase_registrace(entry):
    args = entry.get("args") if isinstance(entry, dict) else None
    return bool(args) and any(os.path.basename(str(a)) == "gemini_mcp.py" for a in args)


def _registrace():
    from . import core
    return (core._claude_json().get("mcpServers") or {}).get(MCP_NAME) or {}


def _prikaz_je_nas():
    return os.path.islink(PRIKAZ) and \
        os.path.basename(os.readlink(PRIKAZ)) == "gemini_pomoc.py"


def stav():
    """Co ukáže karta v Nastavení → AI agenti."""
    mod = gp()
    from . import core
    path = mod.najdi_agy()
    entry = _registrace()
    data = mod.nacti_stav()
    posledni = data.get("posledni") or {}
    return {
        "installed": bool(path),
        "path": path,
        "logged": mod.prihlaseni() if path else False,
        "enabled": _zapnuto(),
        "registered": _nase_registrace(entry),
        "cizi": bool(entry) and not _nase_registrace(entry),
        "prikaz": PRIKAZ if _prikaz_je_nas() else "",
        "tyden": mod.tyden(),
        "chyba": "" if posledni.get("ok") else str(posledni.get("chyba") or ""),
        "install": mod.INSTALL,
        "test": core.TEST_MODE,
    }


def sync():
    """Srovná registraci MCP a příkaz gemini-pomoc se stavem stroje. Nikdy nevyhazuje.

    Zkušební hub (--test) nic nemění: sdílí domov s opravdovým a `claude mcp
    add` by sáhl do jeho ~/.claude.json."""
    from . import core
    if core.TEST_MODE:
        return
    with _LOCK:
        try:
            _sync()
        except Exception as exc:          # noqa: BLE001 — jen úklid, nic nesmí shodit
            core.log_error("gemini: srovnání napojení", exc)


def _sync():
    from . import agents, core
    mod = gp()
    agy = mod.najdi_agy()
    # Stačí, že se agy už někdy spustil (má svou složku): přihlášení v klíčence
    # se zvenku poznat nedá, a když vyprší, nástroj to Claudovi řekne česky
    # i s cestou v Nastavení — lepší než pomocník, který tiše zmizí a po
    # novém přihlášení v terminálu by se sám nevrátil.
    chci = bool(agy) and os.path.isdir(mod.AGY_DIR) and _zapnuto() \
        and os.path.isfile(SKRIPT_MCP)
    entry = _registrace()
    nase = _nase_registrace(entry)
    if entry and not nase:
        # Server jménem „gemini" si člověk přidal sám — ten se nepřepisuje.
        chci_mcp = False
    else:
        chci_mcp = chci
    claude = agents.which(agents.resolve("claude") or {"bin": "claude"})
    python = sys.executable or "python3"
    if claude and chci_mcp and not (entry.get("command") == python
                                    and entry.get("args") == [SKRIPT_MCP]):
        if nase:
            core.run([claude, "mcp", "remove", MCP_NAME, "-s", "user"], cwd=core.HOME, timeout=60)
        try:
            r = subprocess.run([claude, "mcp", "add", MCP_NAME, "-s", "user", "--", python, SKRIPT_MCP],
                               capture_output=True, text=True, cwd=core.HOME, timeout=60,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            ok, detail = r.returncode == 0, (r.stderr or r.stdout or "").strip()[:200]
        except Exception as exc:
            ok, detail = False, str(exc)
        core.log("gemini: Claude dostal pomocníka Gemini (mcp__gemini__*)" if ok
                 else f"gemini: MCP server se nepodařilo napojit — {detail}",
                 "info" if ok else "warn")
    elif claude and nase and not chci:
        core.run([claude, "mcp", "remove", MCP_NAME, "-s", "user"], cwd=core.HOME, timeout=60)
        core.log("gemini: pomocník Gemini odpojen (agy chybí, nepřihlášený nebo vypnutý)")
    _sync_prikaz(chci)


def _sync_prikaz(chci):
    """`gemini-pomoc` do ~/.local/bin — odkaz na tools/gemini_pomoc.py.

    Na Windows ne: odkaz tam chce práva správce a Claude má MCP. Cizí soubor
    stejného jména se nepřepisuje, mažeme jen vlastní odkaz."""
    if os.name == "nt":
        return
    from . import core
    if chci and os.path.isfile(SKRIPT_POMOC):
        if _prikaz_je_nas() and os.path.realpath(PRIKAZ) == os.path.realpath(SKRIPT_POMOC):
            return
        if os.path.lexists(PRIKAZ) and not _prikaz_je_nas():
            return
        try:
            os.makedirs(os.path.dirname(PRIKAZ), exist_ok=True)
            if os.path.lexists(PRIKAZ):
                os.remove(PRIKAZ)
            if not os.access(SKRIPT_POMOC, os.X_OK):
                try:
                    os.chmod(SKRIPT_POMOC, os.stat(SKRIPT_POMOC).st_mode | 0o111)
                except OSError:
                    pass          # na serveru je kód hubu jen ke čtení (a spustitelný z gitu)
            os.symlink(SKRIPT_POMOC, PRIKAZ)
            core.log(f"gemini: příkaz gemini-pomoc → {SKRIPT_POMOC}")
        except OSError as exc:
            core.log(f"gemini: příkaz gemini-pomoc nejde založit — {exc}", "warn")
    elif _prikaz_je_nas():
        try:
            os.remove(PRIKAZ)
        except OSError:
            pass


def nastav(zapnout):
    """Vypínač „Claude smí volat Gemini" z Nastavení."""
    from . import core
    core.save_config({"gemini_pomocnik": bool(zapnout)})
    sync()
    return stav()


def spust_na_pozadi(zpozdeni=6.0):
    """Při startu hubu: po chvíli, ať `claude mcp add` nezdržuje první okno."""
    def jed():
        import time
        time.sleep(zpozdeni)
        sync()
    threading.Thread(target=jed, name="gemini-sync", daemon=True).start()
