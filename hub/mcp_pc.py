"""
Napojení ze serveru i v Claudovi na počítači (brána: gateway/mcp_pc.py).

Claude počítá na počítači, ale napojení (Todoist, GitHub, Ecomail…, sdílená
napojení a MCP Aladin s firemním Obsidianem) má člověk nastavená v prostoru
na serveru. Hub na počítači přihlášený k serveru se brány zeptá, co tam je,
a za každé zaregistruje v Claude Code most `server-<jméno>`
(tools/server_napojeni_mcp.py). Klíče a přihlášení drží brána — na počítač se
nic nekopíruje.

* Co člověk má i na počítači pod stejným jménem, se nezdvojuje — místní vyhrává.
* Napojení, které na serveru zmizelo, hub z Claude Code zase odebere (jen ta,
  která sám přidal — seznam v hub-config `server_napojeni_moje`).
* Bez spojení se serverem se nic neodebírá; zkusí se to příště.
* Vypnout: Nastavení → Účet → Napojení ze serveru (`server_napojeni`: false).
"""
import hashlib
import os
import shutil
import subprocess
import sys
import threading
import time

from . import account, core

PREFIX = "server-"
KAZDYCH = 30 * 60
_STAV = {"kdy": 0, "chyba": "", "napojeni": []}
_LOCK = threading.Lock()
_STARTED = False


def zapnuto():
    return (not core.on_gateway() and not core.TEST_MODE
            and bool(core.CONFIG.get("gw_token")) and bool(account._base())
            and core.CONFIG.get("server_napojeni", True) is not False)


def jmeno_mostu(jmeno):
    """Jméno v Claude Code. Názvy nástrojů mají strop 64 znaků
    (mcp__<jméno>__<nástroj>), dlouhé jméno se proto zkrátí s otiskem."""
    name = PREFIX + jmeno
    if len(name) <= 32:
        return name
    return PREFIX + jmeno[:20] + "-" + hashlib.sha1(jmeno.encode("utf-8")).hexdigest()[:4]


def start():
    global _STARTED
    if core.on_gateway() or core.TEST_MODE:
        return
    with _LOCK:
        if _STARTED:
            return
        _STARTED = True

    def smycka():
        time.sleep(20)
        while True:
            try:
                sync()
            except Exception as exc:
                core.log_error("napojení ze serveru", exc)
            time.sleep(KAZDYCH)
    threading.Thread(target=smycka, daemon=True, name="mcp-pc").start()


def _claude(*args):
    claude = shutil.which("claude")
    if not claude:
        return False, "Claude Code tu není nainstalovaný."
    try:
        r = subprocess.run([claude, "mcp", *args], capture_output=True, text=True,
                           cwd=core.HOME, timeout=60)
        return r.returncode == 0, (r.stderr or r.stdout or "").strip()[:200]
    except Exception as exc:
        return False, str(exc)


def sync():
    """Srovná mosty v Claude Code s tím, co je na serveru. → prehled()."""
    moje = set(core.CONFIG.get("server_napojeni_moje") or [])
    local = core._claude_json().get("mcpServers") or {}
    if not zapnuto():
        # Vypnuto nebo odhlášeno: co jsme přidali, pryč.
        if moje and not core.TEST_MODE and not core.on_gateway():
            for name in moje:
                if name in local:
                    _claude("remove", name, "-s", "user")
            core.save_config({"server_napojeni_moje": []})
        with _LOCK:
            _STAV.update(kdy=time.time(), chyba="", napojeni=[])
        return prehled()
    res, err, _kind = account._call("/gw/mcp-pc", token=core.CONFIG.get("gw_token", ""), timeout=30)
    if res is None or not res.get("ok"):
        with _LOCK:
            _STAV.update(kdy=time.time(), chyba=(res or {}).get("error") or err or "Server neodpověděl.")
        return prehled()
    script = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "tools", "server_napojeni_mcp.py")
    python = sys.executable or "python3"
    chci, seznam, chyby = {}, [], []
    for n in res.get("napojeni") or []:
        jmeno = str(n.get("jmeno") or "")
        if not jmeno:
            continue
        name = jmeno_mostu(jmeno)
        if jmeno in local and jmeno not in moje:
            seznam.append({**n, "most": "", "stav": "máš ho i tady — Claude použije místní"})
            continue
        chci[name] = jmeno
        have = local.get(name) or {}
        if have.get("command") == python and have.get("args") == [script, jmeno]:
            seznam.append({**n, "most": name, "stav": "napojeno"})
            continue
        if have:
            _claude("remove", name, "-s", "user")
        ok, detail = _claude("add", name, "-s", "user", "--", python, script, jmeno)
        seznam.append({**n, "most": name, "stav": "napojeno" if ok else "nepovedlo se: " + detail})
        if not ok:
            chyby.append(f"{jmeno}: {detail}")
    for name in moje - set(chci):
        if name in local:
            _claude("remove", name, "-s", "user")
    if set(chci) != moje:
        core.save_config({"server_napojeni_moje": sorted(chci)})
        core.log(f"napojení ze serveru: {len(chci)} na počítači ({', '.join(sorted(chci.values())) or 'žádné'})")
    with _LOCK:
        _STAV.update(kdy=time.time(), chyba="; ".join(chyby)[:300], napojeni=seznam)
    return prehled()


def prehled():
    with _LOCK:
        stav = dict(_STAV)
    return {"zapnuto": zapnuto(), "nastaveno": core.CONFIG.get("server_napojeni", True) is not False,
            "kdy": int(stav["kdy"]), "chyba": stav["chyba"], "napojeni": stav["napojeni"]}


def nastav(zapnout):
    core.save_config({"server_napojeni": bool(zapnout)})
    return sync()
