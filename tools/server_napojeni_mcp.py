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


def _misto_tabu():
    """Místo chatu, ve kterém most běží (hub/misto.py: ~/.claude/hub-misto/<HUB_TAB>.json),
    nebo None — most pustil Claude mimo tab hubu."""
    import re
    tag = os.environ.get("HUB_TAB", "")
    if not tag:
        return None
    cesta = os.path.join(os.path.dirname(CONFIG), "hub-misto",
                         re.sub(r"[^A-Za-z0-9_.-]", "-", tag) + ".json")
    try:
        with open(cesta, encoding="utf-8") as fh:
            m = json.load(fh)
        return m if isinstance(m, dict) else None
    except (OSError, ValueError):
        return None


def _config():
    """(server, token, prostor, firma, napojení ze serveru zapnutá) — čte se u každé
    zprávy. Platí místo chatu (každý chat má svoje, Jura 6. 11. 2026); bez něj
    výchozí místo z hub-config."""
    try:
        with open(CONFIG, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        data = {}
    base = str(data.get("gw_server") or "").rstrip("/")
    token = str(data.get("gw_token") or "")
    m = _misto_tabu()
    if m is not None:
        if m.get("kde") != "server":
            return base, token, 0, "", False
        server = str(m.get("server") or "").rstrip("/")
        if server and server != base:
            # Chat na jiném uloženém serveru než aktivním: jeho token.
            hit = next((e for e in data.get("gw_servers") or []
                        if isinstance(e, dict) and str(e.get("server") or "").rstrip("/") == server), None)
            base, token = server, str((hit or {}).get("token") or "")
        try:
            prostor = int(m.get("prostor") or 0)
        except (TypeError, ValueError):
            prostor = 0
        return base, token, prostor, "" if prostor else str(m.get("firma") or ""), True
    return (base, token, int(data.get("gw_space") or 0), str(data.get("gw_firma") or ""),
            data.get("server_napojeni", True) is not False)


POCITAC = ("V Aladinu je teď vybraný Počítač — napojení a Obsidian ze serveru jsou vypnuté. "
           "Použij to, co je na tomhle počítači.")


def _odpoved(msg, result):
    return {"ok": True, "zpravy": [{"jsonrpc": "2.0", "id": msg.get("id"), "result": result}]}


def _posli(body, base, token, space, firma):
    """Tatáž zpráva jako v prostoru, jen na bránu přes https s tokenem zařízení."""
    import ssl
    data = json.dumps({"jmeno": body["slug"], "relace": body["relace"],
                       "zprava": body["zprava"], "prostor": space,
                       "firma": "" if space else firma}).encode("utf-8")
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


def gateway(body):
    """Zpráva od Clauda → brána. S vybraným „Počítač“ most nástroje nenabízí a volání
    odmítne; po přepnutí místa řekne Claudovi, ať si nástroje načte znovu
    (`notifications/tools/list_changed`, viz _hlidej_misto) — jako /mcp reconnect,
    jen na pozadí a bez nového načtení chatu (Jura, 6. 11. 2026)."""
    msg = body["zprava"] if isinstance(body.get("zprava"), dict) else {}
    method = msg.get("method")
    base, token, space, firma, zapnuto = _config()
    if not base or not token:
        if method == "tools/list":
            return _odpoved(msg, {"tools": []})
        return {"ok": False, "error": "Appka Aladin na tomhle počítači není přihlášená k serveru "
                                      "(Nastavení → Účet)."}
    if not zapnuto and method == "tools/list":
        return _odpoved(msg, {"tools": []})
    if not zapnuto and method == "tools/call":
        return {"ok": False, "error": POCITAC}
    res = _posli(body, base, token, space, firma)
    if method == "initialize":
        if res.get("ok"):
            # Claude Code si po list_changed nástroje načte znovu — musí vědět, že to most umí.
            for z in res.get("zpravy") or []:
                vysledek = z.get("result") if isinstance(z, dict) else None
                if isinstance(vysledek, dict):
                    caps = vysledek.setdefault("capabilities", {})
                    if isinstance(caps, dict):
                        nastroje = caps.get("tools") if isinstance(caps.get("tools"), dict) else {}
                        caps["tools"] = {**nastroje, "listChanged": True}
        elif not zapnuto:
            # Server teď nejde a místo je Počítač: most se přesto rozběhne (bez nástrojů),
            # ať po přepnutí na server naskočí i v tomhle chatu.
            params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
            return _odpoved(msg, {"protocolVersion": params.get("protocolVersion") or "2025-06-18",
                                  "capabilities": {"tools": {"listChanged": True}},
                                  "serverInfo": {"name": "aladin-" + str(body.get("slug") or ""),
                                                 "version": "1"}})
    return res


def _hlidej_misto():
    """Přepnutí místa (Počítač ↔ prostor na serveru, jiný prostor, firma): Claudovi
    se pošle `notifications/tools/list_changed` a on si nástroje vyžádá znovu."""
    import threading
    import time

    def klic():
        base, _token, space, firma, zapnuto = _config()
        return (base, space, firma, zapnuto)

    def smycka():
        posledni = klic()
        while True:
            time.sleep(2)
            try:
                ted = klic()
            except Exception:
                continue
            if ted != posledni:
                posledni = ted
                sdilene_mcp.emit({"jsonrpc": "2.0", "method": "notifications/tools/list_changed"})
    threading.Thread(target=smycka, daemon=True, name="misto").start()


if __name__ == "__main__":
    sdilene_mcp.gateway = gateway
    _hlidej_misto()
    sdilene_mcp.main(sys.argv)
