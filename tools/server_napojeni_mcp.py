#!/usr/bin/env python3
"""
Most na napojení ze serveru — `python3 tools/server_napojeni_mcp.py <jméno>`.

Běží na počítači jako stdio MCP server Claude Code. Sám nic neumí: každou
zprávu JSON-RPC pošle bráně (`<server>/gw/mcp-pc/volani`, token zařízení
z `~/.claude/hub-config.json`) a co brána vrátí, vypíše Claudovi. Skutečné
napojení — i s klíči a přihlášením — drží brána (gateway/mcp_pc.py), na
počítač se z nich nic nedostane.

Zaregistruje ho hub na počítači sám (hub/mcp_pc.py) pod jménem
`server-<jméno>`. Jen standardní knihovna; posílání zpráv je stejné jako
u mostu na sdílená napojení v prostoru (tools/sdilene_mcp.py).
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sdilene_mcp  # noqa: E402  (vedle tohohle souboru)

CONFIG = os.path.join(os.path.expanduser("~"), ".claude", "hub-config.json")
UA = "claude-code-hub-mcp-pc"


def _config():
    try:
        with open(CONFIG, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        data = {}
    return str(data.get("gw_server") or "").rstrip("/"), str(data.get("gw_token") or "")


def gateway(body):
    """Tatáž zpráva jako v prostoru, jen na bránu přes https s tokenem zařízení."""
    import ssl
    base, token = _config()
    if not base or not token:
        return {"ok": False, "error": "Appka Aladin na tomhle počítači není přihlášená k serveru "
                                      "(Nastavení → Účet)."}
    data = json.dumps({"jmeno": body["slug"], "relace": body["relace"],
                       "zprava": body["zprava"]}).encode("utf-8")
    req = sdilene_mcp.urllib.request.Request(base + "/gw/mcp-pc/volani", data=data, method="POST", headers={
        "Content-Type": "application/json", "Authorization": "Bearer " + token, "User-Agent": UA})
    try:
        with sdilene_mcp.urllib.request.urlopen(req, timeout=sdilene_mcp.TIMEOUT,
                                                context=ssl.create_default_context()) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except sdilene_mcp.urllib.error.HTTPError as exc:
        if exc.code == 401:
            return {"ok": False, "error": "Přihlášení k serveru vypršelo — přihlas se v appce znovu."}
        if exc.code == 404:
            return {"ok": False, "error": "Server ještě nemá novou verzi — napojení ze serveru "
                                          "pojedou po jeho aktualizaci."}
        return {"ok": False, "error": f"Server odpověděl {exc.code}."}
    except (OSError, ValueError) as exc:
        return {"ok": False, "error": f"Server není k dosažení: {exc}"}


if __name__ == "__main__":
    sdilene_mcp.gateway = gateway
    sdilene_mcp.main(sys.argv)
