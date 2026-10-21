"""
Web uživatele (FTP), kam Claude v prostoru zveřejňuje, co postaví.

Claude v prostoru postavil Adamovi appku a neměl ji kam dát (21. 10. 2026).
Správce brány teď účtu přidělí FTP účet webu (`claude-hub-admin web …`,
gateway/workspace.py save_web) a prostor ho dostane do
`~/.claude/hub-web.json` (0600) — při startu v proměnné HUB_WEB, za běhu
přes /api/web-host (volá jen brána, jako predplatne-host). Ke každému chatu
Clauda pak jde krátký pokyn (prompt): máš web na <url>, tady jsou údaje,
nahrávej curlem, ověř 200 a pošli odkaz.

Vedle JSONu se zapíše i `~/.claude/hub-web.netrc` — curl si z něj vezme
jméno a heslo sám, takže heslo není v příkazu, v `ps` ani v přepisu chatu.

Na počítači (mimo bránu) nic nezapisuje ani nemaže; když tam soubor někdo
připraví ručně, pokyn se do chatů dostane taky.
"""
import json
import os
import re
import secrets

from . import core

SOUBOR = os.path.join(core.CLAUDE_DIR, "hub-web.json")
NETRC = os.path.join(core.CLAUDE_DIR, "hub-web.netrc")
ENV = "HUB_WEB"
ZNACKA = "brana"                 # „spravuje“: soubor zapsal hub podle brány

_HOST = re.compile(r"[A-Za-z0-9.-]{1,253}")
_RIDICI = re.compile(r"[\x00-\x1f\x7f]")


def _cisty(data):
    """Zkontrolovaný záznam webu, nebo None. Stejná pravidla jako brána
    (workspace.save_web) — sem ale data jdou i ze souboru, který mohl kdokoli
    v prostoru přepsat, tak se nic nebere naslepo."""
    if not isinstance(data, dict):
        return None
    host = str(data.get("host") or "").strip()
    user = str(data.get("user") or "")
    password = str(data.get("password") or "")
    url = str(data.get("url") or "").strip().rstrip("/")
    remote = "/" + str(data.get("remote_dir") or "/").strip().strip("/")
    try:
        port = 21 if data.get("port") in (None, "") else int(data["port"])
    except (TypeError, ValueError):
        return None
    if not _HOST.fullmatch(host) or not user or not password or not 0 < port < 65536:
        return None
    if not re.match(r"https?://[^\s/]+", url) or _RIDICI.search(user + password + url + remote):
        return None
    if ".." in remote.split("/"):
        return None
    return {"host": host, "port": port, "protocol": "ftps", "user": user,
            "password": password, "remote_dir": remote.rstrip("/") or "/", "url": url}


def nacti():
    """Web z ~/.claude/hub-web.json, nebo None."""
    try:
        with open(SOUBOR, encoding="utf-8") as fh:
            return _cisty(json.load(fh))
    except (OSError, ValueError):
        return None


def _zapis_soubor(path, text):
    tmp = f"{path}.{secrets.token_hex(4)}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, path)


def _netrc_slovo(text):
    """Hodnota do .netrc. S mezerou, uvozovkou nebo zpětným lomítkem v
    uvozovkách (curl 7.84+), jinak tak, jak je."""
    if not re.search(r'[\s"\\]', text):
        return text
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def zapis(data):
    web = _cisty(data)
    if not web:
        raise ValueError("Neplatné údaje webu.")
    os.makedirs(core.CLAUDE_DIR, exist_ok=True)
    _zapis_soubor(SOUBOR, json.dumps(dict(web, spravuje=ZNACKA), ensure_ascii=False,
                                     indent=2) + "\n")
    _zapis_soubor(NETRC, f"machine {web['host']} login {_netrc_slovo(web['user'])} "
                         f"password {_netrc_slovo(web['password'])}\n")
    core.log(f"web: přidělený web {web['url']} zapsaný do ~/.claude/hub-web.json")


def smaz():
    """Odebraný web pryč — jen soubor, který zapsal hub (ruční nechá být)."""
    try:
        with open(SOUBOR, encoding="utf-8") as fh:
            nas = (json.load(fh) or {}).get("spravuje") == ZNACKA
    except (OSError, ValueError, AttributeError):
        return
    if not nas:
        return
    for path in (SOUBOR, NETRC):
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
    core.log("web: přidělený web odebraný z ~/.claude")


def pri_startu():
    """Prostor startuje: brána poslala web v HUB_WEB (prázdná = žádný). Bez
    proměnné vůbec (hub se restartoval sám) se nic nemění. Proměnná se hned
    zahodí, ať ji nedědí taby, Claude ani jeho příkazy."""
    if not core.on_gateway():
        return
    raw = os.environ.pop(ENV, None)
    if raw is None:
        return
    if raw.strip():
        try:
            zapis(json.loads(raw))
        except (ValueError, TypeError) as exc:
            core.log_error("web: údaje od brány", exc)
    else:
        smaz()


def host_action(payload):
    """Volá jen brána: správce web přidělil, změnil nebo odebral."""
    web = (payload or {}).get("web")
    try:
        if web:
            zapis(web)
        else:
            smaz()
    except ValueError as exc:
        return {"error": str(exc)}
    except OSError as exc:
        core.log_error("web: zápis do ~/.claude", exc)
        return {"error": "Zápis do prostoru se nepovedl."}
    return {"ok": True}


def prompt():
    """Pokyn pro chat Clauda, když uživatel má web; jinak ''. Heslo nikdy."""
    web = nacti()
    if not web:
        return ""
    hostport = web["host"] + ("" if web["port"] == 21 else f":{web['port']}")
    base = web["remote_dir"].rstrip("/")
    if os.path.isfile(NETRC):
        netrc, kde = "--netrc-file ~/.claude/hub-web.netrc ", ""
    else:
        netrc, kde = '--user "$U:$P" ', " (U a P = user a password z hub-web.json)"
    return (
        "## Web uživatele\n"
        f"Uživatel má vlastní web {web['url']} — soubor nahraný do `{base or '/'}/cesta` je na "
        f"{web['url']}/cesta. Přístup (FTP přes TLS) je v `~/.claude/hub-web.json`; heslo nikdy "
        "nevypisuj ani neposílej.\n"
        "Nahrávej curlem (server chce TLS session reuse, Python ftplib padá na 522):\n"
        f"`curl --ssl-reqd --ftp-create-dirs {netrc}-T soubor \"ftp://{hostport}{base}/cesta/soubor\"`{kde}\n"
        "Pak ověř, že adresa vrací HTTP 200 (`curl -sI`), a pošli odkaz. Když postavíš appku, "
        "stránku nebo soubor pro lidi, nabídni, že ho tam zveřejníš.")
