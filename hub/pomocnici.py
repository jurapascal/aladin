"""Spolupráce modelů — Claude si volá Codex, opencode, aider a Ollamu na pomoc.

MCP server tools/pomocnici_mcp.py (nástroje mcp__pomocnici__seznam a zeptat).
Tenhle modul drží registraci v Claude Code v souladu s tím, koho člověk povolil
v Nastavení → AI agenti → Spolupráce modelů (hub-config `pomocnici`): aspoň
jeden povolený = server je napojený, nikdo = odpojený. Hlavní model si z
nabídky vybírá sám. Gemini má vlastní kartu (hub/gemini.py).
"""
import os
import subprocess
import sys
import threading

from . import core

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKRIPT = os.path.join(ROOT, "tools", "pomocnici_mcp.py")
MCP_NAME = "pomocnici"
# id → (jméno, binárka)
AGENTI = {"codex": ("Codex", "codex"), "opencode": ("opencode", "opencode"),
          "aider": ("aider", "aider"), "ollama": ("Ollama", "ollama")}
_LOCK = threading.Lock()


def povoleni():
    seznam = core.CONFIG.get("pomocnici")
    return [a for a in (seznam if isinstance(seznam, list) else []) if a in AGENTI]


def _instalovan(binarka):
    from . import agents
    return bool(agents.which({"bin": binarka}))


def _registrace():
    return (core._claude_json().get("mcpServers") or {}).get(MCP_NAME) or {}


def stav():
    entry = _registrace()
    zapnuty = povoleni()
    return {
        "agenti": [{"id": k, "nazev": v[0], "installed": _instalovan(v[1]), "enabled": k in zapnuty}
                   for k, v in AGENTI.items()],
        "registered": bool(entry),
        "test": core.TEST_MODE,
    }


def sync():
    """Srovná registraci s povolenými pomocníky. Nikdy nevyhazuje."""
    if core.TEST_MODE or core.on_gateway():
        return
    with _LOCK:
        try:
            _sync()
        except Exception as exc:          # noqa: BLE001 — jen úklid
            core.log_error("pomocníci: srovnání napojení", exc)


def _sync():
    from . import agents
    chci = bool([a for a in povoleni() if _instalovan(AGENTI[a][1])]) and os.path.isfile(SKRIPT)
    entry = _registrace()
    claude = agents.which(agents.resolve("claude") or {"bin": "claude"})
    if not claude:
        return
    python = sys.executable or "python3"
    ma = entry.get("command") == python and entry.get("args") == [SKRIPT]
    if chci and not ma:
        if entry:
            core.run([claude, "mcp", "remove", MCP_NAME, "-s", "user"], cwd=core.HOME, timeout=60)
        r = subprocess.run([claude, "mcp", "add", MCP_NAME, "-s", "user", "--", python, SKRIPT],
                           capture_output=True, text=True, cwd=core.HOME, timeout=60)
        core.log("pomocníci: Claude dostal pomocníky (mcp__pomocnici__*)" if r.returncode == 0
                 else "pomocníci: napojení se nepovedlo — " + (r.stderr or r.stdout or "").strip()[:200],
                 "info" if r.returncode == 0 else "warn")
    elif not chci and entry and any(os.path.basename(str(x)) == "pomocnici_mcp.py"
                                    for x in (entry.get("args") or [])):
        core.run([claude, "mcp", "remove", MCP_NAME, "-s", "user"], cwd=core.HOME, timeout=60)
        core.log("pomocníci: odpojeni (nikdo nepovolen)")


def nastav(agenti):
    zapnuti = [a for a in (agenti or []) if a in AGENTI]
    core.save_config({"pomocnici": zapnuti})
    threading.Thread(target=sync, daemon=True, name="pomocnici-sync").start()
    return stav()


def spust_na_pozadi(zpozdeni=9.0):
    def jed():
        import time
        time.sleep(zpozdeni)
        sync()
    threading.Thread(target=jed, name="pomocnici-start", daemon=True).start()
