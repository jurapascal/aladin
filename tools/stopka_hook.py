#!/usr/bin/env python3
"""
Hook UserPromptSubmit v prostoru na serveru: stopka od správce.

Správce může člověku nastavit týdenní limit na Clauda se stopkou
(gateway/spotreba.py). Bublina v appce zprávu po překročení sama nepošle
(hub/stopka.py), ale do terminálu jde psát i mimo ni — tenhle hook se před
každou zprávou zeptá brány, jestli stopka platí, a když ano, zprávu Claudovi
nepustí a ukáže proč.

Ptá se brány přímo (loopback, žeton prostoru z prostředí — `HUB_POCITAC_URL`
a `HUB_POCITAC_TOKEN` dal prostoru hub od brány), ne souboru v domově: ten
by si člověk mohl přepsat. Když brána neodpovídá nebo tu proměnné nejsou
(počítač, starší brána), hook nic nedělá — kvůli výpadku se psaní nezastaví.
"""
import json
import os
import sys
import urllib.request


def main():
    url = os.environ.get("HUB_POCITAC_URL", "").rstrip("/")
    token = os.environ.get("HUB_POCITAC_TOKEN", "")
    try:
        sys.stdin.read()             # událost od Claude Code; obsah zprávy nás nezajímá
    except (OSError, ValueError):
        pass
    if not url or not token:
        return 0
    req = urllib.request.Request(url + "/gw/spotreba/stopka",
                                 headers={"X-Hub-Pocitac": token, "Accept": "application/json"})
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(req, timeout=4) as res:
            data = json.loads(res.read().decode("utf-8") or "{}")
    except Exception:
        return 0
    st = data.get("stopka") if isinstance(data, dict) else None
    if isinstance(st, dict) and st.get("text"):
        print(json.dumps({"decision": "block", "reason": str(st["text"])}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
