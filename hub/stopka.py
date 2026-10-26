"""
Stopka od správce: týdenní limit na Clauda (gateway/spotreba.py) je vyčerpaný
a správce u něj zapnul, že se pak dál nepíše.

Rozhoduje brána, ne hub — soubor se stavem v domově by si člověk v prostoru
přepsal. Hub se jí ptá:

* **v prostoru na serveru** přímo na loopbacku žetonem prostoru
  (`HUB_POCITAC_URL` / `HUB_POCITAC_TOKEN`, jako most na počítač) a odpověď
  drží minutu;
* **na počítači** stav přinese souhrn spotřeby (hub/spotreba_pc.py). Platí
  tu jen, když počítač jede na stejném předplatném jako prostor — limit je
  na společné předplatné, ne na cizí.

Co se zastaví: zpráva z bubliny (`/api/fronta` ji nevezme a okno řekne proč)
a v prostoru i to, co člověk napíše Claudovi rovnou do terminálu — hook
UserPromptSubmit (tools/stopka_hook.py), který hub při startu zapojí do
~/.claude/settings.json. Rozdělaná práce doběhne a příkazy (`/model`)
i odpovědi na otázky Clauda jdou dál. Na počítači hook zapojený není: je to
stroj toho člověka a jeho nastavení Claude Code hub jen tak nemění.
"""
import json
import os
import sys
import threading
import time
import urllib.request

from . import core

TTL = 60            # jak dlouho platí odpověď brány (dotaz chodí s každou zprávou)
HOOK = "stopka_hook.py"

_lock = threading.Lock()
_cache = {"t": 0.0, "data": None}
_opener = None


def _z_brany():
    """{"radek", "stopka"} od brány, nebo None, když se nedá zeptat."""
    global _opener
    url = os.environ.get("HUB_POCITAC_URL", "").rstrip("/")
    token = os.environ.get("HUB_POCITAC_TOKEN", "")
    if not url or not token:
        return None
    req = urllib.request.Request(url + "/gw/spotreba/stopka",
                                 headers={"X-Hub-Pocitac": token, "Accept": "application/json"})
    if _opener is None:
        # Brána je na loopbacku — proxy z prostředí by dotaz poslala ven.
        _opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with _opener.open(req, timeout=5) as res:
            data = json.loads(res.read().decode("utf-8") or "{}")
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def stav(cerstve=False):
    """Jak je na tom člověk s limitem: {"radek", "stopka", "kde", …} nebo {}
    (žádná brána, žádný limit). `stopka` je vyplněná, jen když tu platí."""
    if core.TEST_MODE:
        return {}
    if core.on_gateway():
        with _lock:
            if not cerstve and _cache["data"] is not None and time.time() - _cache["t"] < TTL:
                return _cache["data"]
        data = _z_brany()
        with _lock:
            if data is None:
                # Brána teď neodpovídá: platí, co řekla naposledy (prázdno = nic).
                data = _cache["data"] if _cache["data"] is not None else {}
            else:
                data = {"radek": data.get("radek"), "stopka": data.get("stopka"), "kde": "server"}
            _cache.update(t=time.time(), data=data)
        return data
    from . import spotreba_pc
    st = spotreba_pc.STAV
    if not st.get("cas"):
        return {}
    return {"radek": st.get("radek"), "kde": "pocitac",
            "stopka": st.get("stopka") if st.get("plati_zde") else None,
            # Na serveru stopka je, ale tenhle počítač jede na jiném předplatném.
            "stopka_na_serveru": bool(st.get("stopka") and not st.get("plati_zde")),
            "stejne_predplatne": bool(st.get("stejne"))}


def zablokovano():
    """Stopka, která tu platí ({"text", "do", …}), nebo None."""
    st = stav().get("stopka")
    return st if isinstance(st, dict) and st.get("text") else None


def pri_startu():
    """V prostoru na serveru zapojí hook, který stopku hlídá i v terminálu."""
    if not core.on_gateway() or not os.environ.get("HUB_POCITAC_URL"):
        return
    try:
        _zapoj_hook()
    except Exception as exc:
        core.log_error("stopka: hook se nepodařilo zapojit", exc)


def _zapoj_hook():
    """UserPromptSubmit do ~/.claude/settings.json — vedle hooků, které tam
    člověk má (nic nenahrazuje). Soubor, který nejde přečíst, se nechá být.
    Volá se při každém startu prostoru, takže smazaný hook se vrátí."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    script = os.path.join(root, "tools", HOOK)
    if not os.path.isfile(script):
        return
    python = sys.executable or "python3"
    command = f'"{python}" "{script}"'
    path = os.path.join(core.CLAUDE_DIR, "settings.json")
    try:
        with open(path, encoding="utf-8-sig") as fh:
            text = fh.read().strip()
        data = json.loads(text) if text else {}
    except FileNotFoundError:
        data = {}
    except (OSError, ValueError):
        return
    if not isinstance(data, dict):
        return
    hooks = data.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        return
    skupiny = hooks.setdefault("UserPromptSubmit", [])
    if not isinstance(skupiny, list):
        return
    for skupina in skupiny:
        for hook in (skupina.get("hooks") if isinstance(skupina, dict) else None) or []:
            if isinstance(hook, dict) and HOOK in str(hook.get("command", "")):
                if hook.get("command") == command:
                    return
                hook["command"] = command          # jiná cesta (nová verze, jiný Python)
                break
        else:
            continue
        break
    else:
        skupiny.append({"hooks": [{"type": "command", "command": command, "timeout": 10}]})
    os.makedirs(core.CLAUDE_DIR, exist_ok=True)
    tmp = path + ".hub-tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    os.replace(tmp, path)
    core.log("stopka: hook pro limit spotřeby zapojen")
