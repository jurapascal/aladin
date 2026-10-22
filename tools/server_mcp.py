#!/usr/bin/env python3
"""
MCP server „server" — Claude na počítači sahá do sessions v prostoru na serveru.

Protějšek `pocitac_mcp.py`: tam Claude ze serveru čte počítač, tady Claude
z počítače vidí běžící sessions (taby s Claude Code) ve svém prostoru, čte, co
se v nich děje, a píše do nich. Přihlášení k serveru bere z `hub-config.json`
(token zařízení), jde jen do prostoru téhož člověka. Jen standardní knihovna.

Protokol: JSON-RPC 2.0 po řádcích na stdin/stdout.
"""
import json
import os
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

NAME = "server"
VERSION = "1.0.0"
CONFIG = os.path.join(os.path.expanduser("~"), ".claude", "hub-config.json")
MAX_TEXT = 80 * 1024

INSTRUCTIONS = """\
Nástroje mcp__server__* sahají do **prostoru uživatele na serveru** (Claude Code Hub),
ne na tenhle počítač. Slouží ke sledování a řízení sessions, které tam běží.

- `sessions` vypíše běžící sessions a jestli Claude právě pracuje.
- `session_cist` ukáže jejich konverzaci; `dal` (offset `next`) vrátí jen novinky.
- `session_napsat` pošle zprávu do session jako člověk (předpona [z počítače]).
  Piš do rozdělané práce jen na přání uživatele; odpověď si pak přečti přes `session_cist`."""

TOOLS = [
    {"name": "sessions",
     "description": "Běžící sessions (taby s Claude Code) v prostoru uživatele na serveru: číslo "
                    "konverzace, název, složka a jestli Claude právě pracuje.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "session_cist",
     "description": "Přečte konverzaci session na serveru (poslední zprávy a kroky nástrojů). "
                    "`dal` = offset `next` z minulého čtení, vrátí jen novinky.",
     "inputSchema": {"type": "object", "required": ["chat"], "properties": {
         "chat": {"type": "string", "description": "Číslo konverzace ze `sessions`."},
         "poslednich": {"type": "integer", "description": "Kolik posledních bloků (výchozí 30)."},
         "dal": {"type": "integer", "description": "Offset `next` z minulého čtení."}}}},
    {"name": "session_napsat",
     "description": "Napíše zprávu do běžící session na serveru, jako by ji napsal člověk. "
                    "Session ji vezme po dokončení rozdělaného kroku.",
     "inputSchema": {"type": "object", "required": ["chat", "text"], "properties": {
         "chat": {"type": "string"},
         "text": {"type": "string"}}}},
]


class Failed(Exception):
    pass


def _config():
    try:
        with open(CONFIG, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        data = {}
    base = str(data.get("gw_server") or "").rstrip("/")
    token = str(data.get("gw_token") or "")
    if not base or not token:
        raise Failed("Appka Aladin na tomhle počítači není přihlášená k serveru "
                     "(Nastavení → Účet).")
    return base, token


def call(tool, args):
    import ssl                      # líně: ~3 MB, tab bez volání serveru ho nepotřebuje
    base, token = _config()
    body = json.dumps({"tool": tool, "args": args}).encode("utf-8")
    req = urllib.request.Request(base + "/gw/prostor/volani", data=body, method="POST", headers={
        "Content-Type": "application/json", "Authorization": "Bearer " + token,
        "User-Agent": "claude-code-hub-server-mcp"})
    try:
        with urllib.request.urlopen(req, timeout=60, context=ssl.create_default_context()) as res:
            data = json.loads(res.read().decode("utf-8", "replace") or "{}")
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            raise Failed("Přihlášení k serveru vypršelo — přihlas se v appce znovu.") from None
        raise Failed(f"Server odpověděl {exc.code} — možná je starší verze, aktualizuj ho.") from None
    except (OSError, ValueError) as exc:
        raise Failed(f"Server teď nejde zavolat: {exc}") from None
    if not data.get("ok"):
        raise Failed(data.get("error") or "Nepovedlo se.")
    return data.get("result") or {}


def t_sessions(_a):
    rows = call("session_seznam", {}).get("sessions") or []
    if not rows:
        return "V prostoru na serveru neběží žádná session s Claude Code."
    return "Sessions na serveru:\n" + "\n".join(
        f"- {r.get('chat') or '(bez id)'}: {r.get('title')} ({r.get('path')})"
        + (" — PRACUJE" if r.get("working") else "") for r in rows)


def t_cist(a):
    args = {"chat": a.get("chat")}
    if a.get("dal"):
        args["from"] = a["dal"]
    res = call("sdilet_cteni", args)
    try:
        last = max(1, min(int(a.get("poslednich") or 30), 200))
    except (TypeError, ValueError):
        last = 30
    lines = []
    for b in (res.get("blocks") or [])[-last:]:
        kind = b.get("kind")
        if kind == "me":
            lines.append("## Uživatel\n" + (b.get("text") or ""))
        elif kind == "say":
            lines.append("## Claude\n" + (b.get("text") or ""))
        elif kind == "tool":
            lines.append(f"- nástroj {b.get('name', '')}: {(b.get('title') or '')[:200]}")
    state = "PRACUJE" if res.get("pracuje") else ("čeká na zprávu" if res.get("bezi") else "je zavřená")
    return f"Session {a.get('chat')} — {state}. next={res.get('next')}\n\n" + ("\n\n".join(lines) or "(zatím nic)")


def t_napsat(a):
    call("sdilet_poslat", {"chat": a.get("chat"), "text": a.get("text") or ""})
    return "Zpráva je v session. Odpověď sleduj přes `session_cist`."


HANDLERS = {"sessions": t_sessions, "session_cist": t_cist, "session_napsat": t_napsat}
_OUT = threading.Lock()


def send(msg):
    with _OUT:
        sys.stdout.write(json.dumps(msg, ensure_ascii=False) + "\n")
        sys.stdout.flush()


def call_tool(rid, params):
    handler = HANDLERS.get(params.get("name"))
    args = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
    if not handler:
        return send({"jsonrpc": "2.0", "id": rid,
                     "error": {"code": -32602, "message": f"Neznámý nástroj {params.get('name')}"}})
    try:
        out, error = handler(args), False
    except Failed as exc:
        out, error = str(exc), True
    except Exception as exc:
        out, error = f"Nástroj selhal: {exc}", True
    if len(out) > MAX_TEXT:
        out = out[:MAX_TEXT] + "\n… (zkráceno)"
    send({"jsonrpc": "2.0", "id": rid,
          "result": {"content": [{"type": "text", "text": out}], "isError": error}})


def handle(msg):
    method, rid = msg.get("method"), msg.get("id")
    if method == "initialize":
        asked = (msg.get("params") or {}).get("protocolVersion") or "2025-06-18"
        return send({"jsonrpc": "2.0", "id": rid, "result": {
            "protocolVersion": asked, "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": NAME, "version": VERSION}, "instructions": INSTRUCTIONS}})
    if method == "tools/list":
        return send({"jsonrpc": "2.0", "id": rid, "result": {"tools": TOOLS}})
    if method == "tools/call":
        threading.Thread(target=call_tool, args=(rid, msg.get("params") or {}), daemon=True).start()
        return None
    if method == "ping":
        return send({"jsonrpc": "2.0", "id": rid, "result": {}})
    if rid is not None:
        return send({"jsonrpc": "2.0", "id": rid,
                     "error": {"code": -32601, "message": f"Metodu {method} neznám."}})
    return None


def main():
    for line in sys.stdin:
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        if isinstance(msg, dict):
            handle(msg)


if __name__ == "__main__":
    main()
