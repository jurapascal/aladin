"""
Napojení ze serveru i v Claudovi na počítači.

Kdo pracuje na počítači (Claude počítá tam, chaty se kopírují na server,
gateway/zrcadlo.py), má mít po ruce i napojení, která má v prostoru na
serveru. Klíče a přihlášení ale zůstávají na bráně: počítač dostane za každé
napojení jen stdio most (`tools/server_napojeni_mcp.py <jméno>`, registruje
ho hub/mcp_pc.py), který zprávy JSON-RPC posílá sem s tokenem zařízení, a
brána je vyřídí stejně jako pro prostor:

* **Napojení z prostoru** (`~/.claude.json` v domově, user scope): vzdálené
  na https adrese — brána k nim přidá uložené hlavičky nebo přihlášení OAuth
  z `~/.claude/.credentials.json` a vypršelé obnoví (jako sdílený účet,
  gateway/mcp_sdilene.py `_Account`); Ecomail / Meta spouštěné příkazem —
  brána pustí ověřený balíček v sandboxu s tokenem z proměnné (`_owner_command`).
  Libovolný příkaz z konfigu se na bráně nespouští.
* **Sdílená napojení**, kterých je člověk členem — rovnou přes mcp_sdilene.call.
* **Aladin** — MCP brány samotné (gateway/mcp.py): prostor, konverzace,
  firemní a sdílený Obsidian, soubory a příkazy v prostoru.

Mosty prostoru samotného (`pocitac`, `server`, `sdilene-…`), prohlížeč
a Gemini dávají smysl jen tam, kde běží — ty se nenabízejí.
"""
import json
import re

from . import mcp, mcp_sdilene, safefs, workspace

JMENO = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,60}")
RELACE = re.compile(r"[A-Za-z0-9_-]{8,40}")
ALADIN = "aladin"
# Jen tam, kde běží: most na počítač, sessions, prohlížeč, Gemini v prostoru.
VYNECHAT = {"pocitac", "server", "playwright", "gemini"}


def _servery(user):
    home = workspace.home_for(user)
    raw = safefs.read_text(home, ".claude.json", 16 * 1024 * 1024) if home else None
    try:
        servers = (json.loads(raw or "{}").get("mcpServers") or {})
    except ValueError:
        servers = {}
    return servers if isinstance(servers, dict) else {}


def _moje(user):
    """Napojení z prostoru, která jde obsloužit z brány: {jméno: služba}."""
    out = {}
    for jmeno, spec in _servery(user).items():
        if not isinstance(spec, dict) or not JMENO.fullmatch(str(jmeno)) or jmeno in VYNECHAT \
                or jmeno.startswith(("sdilene-", "server-")) or jmeno == ALADIN:
            continue
        url = str(spec.get("url") or "")
        if url.startswith("https://"):
            out[jmeno] = "vlastni"
            continue
        args = [str(a) for a in spec.get("args") or []]
        for sluzba, (balik, _var) in mcp_sdilene.COMMAND_SERVICES.items():
            if any(balik in a for a in args):
                out[jmeno] = sluzba
                break
    return out


def seznam(user):
    """Co si počítač může napojit: [{jmeno, zdroj, nazev}]."""
    out = [{"jmeno": ALADIN, "zdroj": "brana", "nazev": "Aladin (prostor, konverzace a Obsidian na serveru)"}]
    for jmeno in sorted(_moje(user)):
        out.append({"jmeno": jmeno, "zdroj": "prostor", "nazev": jmeno})
    for item in mcp_sdilene.for_user(user):
        slug = str(item.get("slug") or "")
        # Svůj účet ze služby má člověk mezi napojeními z prostoru napřímo.
        if JMENO.fullmatch(slug) and not (item.get("kind") == "ucet" and item.get("is_owner")):
            out.append({"jmeno": "sdilene-" + slug, "zdroj": "sdilene",
                        "nazev": str(item.get("name") or slug)})
    return out


def volani(h, user, jmeno, relace, msg):
    """Jedna zpráva JSON-RPC z mostu na počítači → zprávy pro Claude Code."""
    jmeno, relace = str(jmeno or ""), str(relace or "")
    if not RELACE.fullmatch(relace):
        raise ValueError("Neplatné sezení mostu.")
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
        raise ValueError("Zpráva není JSON-RPC.")
    if jmeno == ALADIN:
        odpoved = mcp._handle(h, {**user, "client_id": "pocitac"}, msg)
        return [odpoved] if odpoved else []
    if jmeno.startswith("sdilene-"):
        return mcp_sdilene.call(user, jmeno[len("sdilene-"):], "pc-" + relace, msg)
    sluzba = _moje(user).get(jmeno)
    if not sluzba:
        raise PermissionError("Tohle napojení v prostoru na serveru nemáš.")
    entry = {"kind": "ucet", "owner": int(user["id"]), "account": jmeno, "service": sluzba}
    item = mcp_sdilene.POOL.get("pc-" + jmeno, int(user["id"]), relace, entry)
    try:
        return item.send(msg)
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"Služba není k dosažení: {exc}") from None
