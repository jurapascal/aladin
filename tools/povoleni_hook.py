#!/usr/bin/env python3
"""
Hook PermissionRequest pro taby, které hub otevřel kvůli úkolu ze serveru
(hub/pocitac.py, „Claude v prostoru na serveru ti nechal úkol…").

Claude Code ho spustí, když se chystá zeptat člověka — oprávnění k nástroji,
otázka (AskUserQuestion), schválení plánu (ExitPlanMode). Dialog v terminálu
se přitom ukáže taky a platí, kdo odpoví dřív: člověk u počítače, nebo tenhle
hook s odpovědí, kterou člověk dal v prostoru na serveru.

Hook žádost ohlásí hubu na loopbacku (`/povoleni`, žeton tabu z prostředí —
hub ho dává jen tabům s úkolem ze serveru) a čeká, co hub vrátí:

* rozhodnutí ze serveru → vypíše ho Claude Code (allow / deny, u otázky
  odpovědi v `updatedInput.answers`);
* vyřízeno jinde (odpověď v terminálu, tab skončil) → skončí bez výstupu;
* hub neběží nebo spadlo spojení → skončí bez výstupu. Dialog na počítači
  pak čeká dál, nic se samo nezamítá.
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request

WAIT = 25           # jak dlouho hub drží jeden dotaz
DOWN_LIMIT = 90     # jak dlouho se zkouší hub, který neodpovídá


def call(url, token, payload=None, timeout=WAIT + 15):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method="POST" if data else "GET",
                                 headers={"X-Hub-Povoleni": token,
                                          "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read() or b"{}")


def decision(event, reply):
    behavior = reply.get("behavior")
    if behavior == "allow":
        # Vstup nástroje vždycky zpátky: nástroje, které čekají na člověka
        # (ExitPlanMode, AskUserQuestion), bez updatedInput dialog nezavřou —
        # naměřeno v Claude Code 2.1.300. U otázky v něm jsou i odpovědi.
        tool_input = event.get("tool_input") if isinstance(event.get("tool_input"), dict) else {}
        updated = dict(tool_input)
        if isinstance(reply.get("answers"), dict):
            updated["answers"] = reply["answers"]
        return {"behavior": "allow", "updatedInput": updated}
    if behavior == "deny":
        return {"behavior": "deny", "message": str(reply.get("message") or
                                                   "Uživatel to v prostoru na serveru zamítl.")}
    return None


def main():
    base = os.environ.get("HUB_POVOLENI_URL", "")
    token = os.environ.get("HUB_POVOLENI_TOKEN", "")
    if not base or not token:
        return
    try:
        event = json.load(sys.stdin)
    except ValueError:
        return
    if not isinstance(event, dict):
        return
    try:
        rid = str(call(base + "/povoleni", token, event, timeout=15).get("id") or "")
    except (OSError, ValueError, urllib.error.URLError):
        return
    if not rid:
        return
    down_since = 0.0
    while True:
        try:
            reply = call(f"{base}/povoleni?id={rid}&wait={WAIT}", token)
            down_since = 0.0
        except urllib.error.HTTPError:
            return                              # hub žádost nezná (restart, vyřízená)
        except (OSError, ValueError, urllib.error.URLError):
            down_since = down_since or time.time()
            if time.time() - down_since > DOWN_LIMIT:
                return
            time.sleep(3)
            continue
        if reply.get("cekej"):
            continue
        out = decision(event, reply)
        if out:
            print(json.dumps({"hookSpecificOutput": {"hookEventName": "PermissionRequest",
                                                     "decision": out}}, ensure_ascii=False))
        return


if __name__ == "__main__":
    main()
