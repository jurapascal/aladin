#!/usr/bin/env python3
"""
Most na sdílené napojení (MCP) — `python3 tools/sdilene_mcp.py <zkratka>`.

Běží v prostoru na serveru jako stdio MCP server Claude Code. Sám nic neumí:
každou zprávu JSON-RPC přepošle bráně (`$HUB_POCITAC_URL/gw/mcp-sdilene/volani`
se žetonem `$HUB_POCITAC_TOKEN`) a co brána vrátí, vypíše Claudovi. Skutečný
MCP server — i s klíči a hesly — drží brána (gateway/mcp_sdilene.py), sem se
nic z toho nedostane.

Zaregistruje ho hub v prostoru sám (hub/connect.py, sync_shared) pod jménem
`sdilene-<zkratka>`. Jen standardní knihovna.

Každá zpráva jde ve vlastním vlákně — dlouhé volání nástroje nesmí blokovat
další, o které si Claude řekl souběžně.
"""
import json
import os
import secrets
import sys
import threading


class _LenyUrllib:
    """urllib.request vytáhne ssl, http a email (~8 MB RAM). Tab, který most na
    počítač nikdy nezavolá, to platit nemá — importuje se až při prvním dotazu."""

    @property
    def request(self):
        import urllib.request as mod
        return mod

    @property
    def error(self):
        import urllib.error as mod
        return mod


urllib = _LenyUrllib()

TIMEOUT = 200                          # o kus víc než brána (TIMEOUT v mcp_sdilene)
_out = threading.Lock()


def emit(msg):
    with _out:
        sys.stdout.write(json.dumps(msg, ensure_ascii=False) + "\n")
        sys.stdout.flush()


def gateway(body):
    url = os.environ.get("HUB_POCITAC_URL", "").rstrip("/")
    token = os.environ.get("HUB_POCITAC_TOKEN", "")
    if not url or not token:
        return {"ok": False, "error": "Sdílené napojení funguje jen v prostoru na serveru."}
    req = urllib.request.Request(url + "/gw/mcp-sdilene/volani",
                                 data=json.dumps(body).encode("utf-8"), method="POST",
                                 headers={"Content-Type": "application/json",
                                          "X-Hub-Pocitac": token})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return {"ok": False, "error": f"Brána odpověděla {exc.code}."}
    except (OSError, ValueError) as exc:
        return {"ok": False, "error": f"Brána není k dosažení: {exc}"}


def handle(slug, relace, msg):
    data = gateway({"slug": slug, "relace": relace, "zprava": msg})
    if data.get("ok"):
        for reply in data.get("zpravy") or []:
            emit(reply)
        return
    if isinstance(msg, dict) and "method" in msg and msg.get("id") is not None:
        emit({"jsonrpc": "2.0", "id": msg["id"],
              "error": {"code": -32000, "message": data.get("error") or "Nepovedlo se."}})


def main(argv):
    if len(argv) != 2:
        sys.exit("použití: sdilene_mcp.py <zkratka>")
    slug = argv[1]
    # Jedno sezení mostu = jeden běh Claude Code; brána podle něj drží spojení.
    relace = secrets.token_urlsafe(12)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            emit({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}})
            continue
        threading.Thread(target=handle, args=(slug, relace, msg), daemon=True).start()


if __name__ == "__main__":
    main(sys.argv)
