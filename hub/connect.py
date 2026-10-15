"""
Služby na pár kliknutí: Freelo, Canva, Ecomail a Google — i s více účty.

Katalog v core.MCP_CATALOG je technický („zaregistruj server s těmahle údaji").
Tohle je vrstva pro člověka: karta služby, pod ní účty a u každého Přihlásit
a Odebrat (Nastavení → Napojení). Každý účet je samostatné napojení:

* **Freelo, Canva** — oficiální vzdálené MCP servery s OAuth, které si
  klienta zaregistrují samy (DCR — ověřeno na jejich metadatech). Účet je
  `claude mcp add` pod jménem `<služba>-<popisek>`. Claude Code ukládá
  přihlášení pod jménem serveru (`canva-firma|<otisk>`), takže dva účty téže
  služby se nepřepíšou.

  Přihlašuje `claude mcp login <jméno> --no-browser`: vypíše adresu a čeká,
  až se vloží adresa, kam prohlížeč po přihlášení přesměroval. Bez terminálu to
  odmítne („stdin isn't a terminal"), proto běží v pty. Na počítači
  přesměrování na localhost doběhne samo; na serveru (brána) localhost
  v prohlížeči není server, tak se adresa vloží ručně.

* **Google** — jeden server workspace-mcp (open source: Gmail, Disk, Kalendář,
  Dokumenty, Tabulky, Prezentace, Formuláře, Úkoly, Kontakty) pro všechny účty;
  nástroje berou `user_google_email`. Přihlášení obstará hub sám (OAuth s PKCE)
  a token uloží tam, kde ho workspace-mcp hledá
  (~/.google_workspace_mcp/credentials/<e-mail>.json). Server pak vlastní
  přihlašovací tok ani port 8000 nepotřebuje — na bráně by se o něj prostory
  jinak přetahovaly. Klienta OAuth založí správce jednou: na bráně přijde
  v prostředí (GOOGLE_OAUTH_CLIENT_ID/SECRET), na počítači se uloží do
  hub-config.json.
"""
import base64
import datetime
import hashlib
import html
import http.server
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

from . import core, pty_backend, qr

SERVICES = {
    "freelo": {
        "label": "Freelo",
        "note": "Projekty, úkoly, komentáře a výkazy práce.",
        "kind": "mcp",
        "url": "https://mcp.freelo.io/mcp",
    },
    "canva": {
        "label": "Canva",
        "note": "Návrhy, šablony a export.",
        "kind": "mcp",
        "url": "https://mcp.canva.com/mcp",
        "warn": "Canva napojení pouští jen v placeném tarifu (Pro, Teams…). "
                "V týmovém účtu ho musí správce Canvy povolit: Canva AI Connector.",
    },
    # Vzdálený server Ecomailu ({účet}.ecomailapp.cz/mcp) pouští OAuth jen
    # claude.ai a ChatGPT — registraci klienta s návratem na localhost odmítne
    # 403 (ověřeno 6. 10. 2026). Pro Claude Code má Ecomail oficiální lokální
    # server s API klíčem (github.com/Ecomailcz/ecomail-public-mcp).
    "ecomail": {
        "label": "Ecomail",
        "note": "Kontakty, seznamy a kampaně.",
        "kind": "token",
        "command": ["npx", "-y", "ecomail-mcp@1.0.0"],
        "package": "ecomail-mcp",
        "env": "ECOMAIL_API_KEY",
        "field": {"name": "account", "label": "API klíč z Ecomailu", "secret": True,
                  "help": "Ecomail → Správa účtu → Pro vývojáře → Zkopírovat API klíč"},
        "warn": "API klíč dává přístup k celému účtu Ecomailu, včetně odesílání kampaní.",
        # Server se spustí i se špatným klíčem a spadne až první nástroj —
        # proto klíč ověří přímo API Ecomailu ještě před uložením.
        "check": {"url": "https://api2.ecomailapp.cz/account", "header": "key"},
    },
    # Clockify se nepřihlašuje přes OAuth, bere API klíč v hlavičce — proto
    # vlastní druh „apikey": po vložení klíče je hotovo, žádné přihlášení.
    "clockify": {
        "label": "Clockify",
        "note": "Výkazy času, projekty a stopky.",
        "kind": "apikey",
        "url": "https://api.clockify.me/mcp-server/mcp",
        "header": "x-api-key",
        "field": {"name": "account", "label": "API klíč z Clockify", "secret": True,
                  "help": "Clockify → foto profilu → Preferences → Advanced → "
                          "Manage API keys → Generate New"},
    },
    # Facebook stránky i Instagram (firemní/tvůrce) jedním tokenem Mety —
    # open-source @oliverames/meta-mcp-server (MIT). Meta vlastní server na
    # příspěvky nemá. Verze je pevná: cizí kód se nemá měnit pod rukama.
    "facebook": {
        "label": "Facebook a Instagram",
        "note": "Příspěvky, komentáře a statistiky stránek i Instagramu.",
        "kind": "token",
        "command": ["npx", "-y", "@oliverames/meta-mcp-server@2.0.2"],
        "package": "@oliverames/meta-mcp-server",
        "env": "META_ACCESS_TOKEN",
        "field": {"name": "account", "label": "Přístupový token Mety (EAA…)", "secret": True,
                  "help": "developers.facebook.com → Graph API Explorer → Generate Access "
                          "Token (stránky a Instagram) → v Access Token Debuggeru "
                          "Extend Access Token (platí 60 dní)."},
        "warn": "Instagram musí být firemní nebo tvůrce a propojený s facebookovou stránkou.",
        "setup": [
            {"title": "Založ aplikaci", "button": "Otevřít",
             "url": "https://developers.facebook.com/apps",
             "text": "Create App → typ Business. Pro vlastní stránku nic neschvaluje Meta."},
            {"title": "Vygeneruj token", "button": "Graph API Explorer",
             "url": "https://developers.facebook.com/tools/explorer",
             "text": "Vpravo nahoře vyber svou aplikaci, zaškrtni oprávnění pages_* , "
                     "instagram_* , read_insights a business_management a dej Generate "
                     "Access Token. Vyber stránku i Instagram."},
            {"title": "Prodluž ho na 60 dní", "button": "Token Debugger",
             "url": "https://developers.facebook.com/tools/debug/accesstoken/",
             "text": "Vlož token, dej Debug → Extend Access Token a nový token vlož sem dolů."},
        ],
    },
    # Oficiální server Mety — jen reklamy, přihlášení Facebookem.
    "reklamy": {
        "label": "Meta reklamy",
        "note": "Reklamy na Facebooku a Instagramu: výkon, kampaně, katalogy.",
        "kind": "mcp",
        "url": "https://mcp.facebook.com/ads",
    },
    # GitHub = dvě věci naráz, obě pod jedním přihlášením:
    #  - oficiální vzdálený MCP server (github/github-mcp-server) se VŠEMI sadami
    #    nástrojů (/x/all — 95 nástrojů místo výchozích 46),
    #  - přihlášené `gh` + git, protože MCP server ani se všemi sadami neumí
    #    spolupracovníky, tajné údaje a proměnné Actions, nastavení repa,
    #    ochranu větví, webhooky, Pages ani mazání (ověřeno tools/list 11. 10. 2026).
    # Přihlášení: kód na github.com/login/device přes `gh auth login --web` —
    # žádné přesměrování, takže jde i na serveru. OAuth přímo do MCP serveru
    # GitHub pouští jen předem registrovaným klientům, proto token v hlavičce.
    "github": {
        "label": "GitHub",
        "note": "Repozitáře, kód, issues, pull requesty, Actions, tajné údaje, "
                "spolupracovníci a nastavení repozitářů.",
        "kind": "github",
        "url": "https://api.githubcopilot.com/mcp/x/all",
        "header": "Authorization",
        "prefix": "Bearer ",
        "field": {"name": "account", "label": "Osobní přístupový token (ghp_… nebo github_pat_…)",
                  "secret": True,
                  "help": "Jen když nechceš přihlášení přes GitHub výše."},
        "warn": "Claude smí na GitHubu všechno, co ty — včetně mazání repozitářů a "
                "změn v organizacích. Přihlášení jde kdykoli zrušit křížkem tady, "
                "nebo na GitHubu v Settings → Applications.",
        "check": {"url": "https://api.github.com/user", "header": "Authorization", "prefix": "Bearer "},
        "setup": [
            {"title": "Vytvoř token", "button": "Otevřít GitHub",
             "url": "https://github.com/settings/tokens/new?description=Claude%20Hub&scopes="
                    "repo,workflow,admin:org,admin:repo_hook,admin:org_hook,delete_repo,"
                    "gist,user,project,notifications,write:packages,delete:packages",
             "text": "Oprávnění jsou předvyplněná na všechno. Nastav platnost, dole "
                     "Generate token a token zkopíruj."},
            {"title": "Vlož ho sem dolů", "button": "", "url": "",
             "text": "Token GitHub ukáže jen jednou — když ho ztratíš, vytvoř nový. "
                     "Fine-grained token taky jde, ale Claude pak umí jen to, co mu v něm povolíš."},
        ],
    },
    # WhatsApp: Claude píše uživateli a ptá se ho (a čeká na odpověď) z vlastního
    # čísla — propojené zařízení přes QR jako WhatsApp Web, knihovna Baileys.
    # Jedno spojení na uživatele drží tools/whatsapp_mcp.mjs (démon), všechny
    # konverzace se k němu připojují. Neoficiální klient → proto číslo pro Clauda,
    # ne hlavní číslo uživatele.
    "whatsapp": {
        "label": "WhatsApp",
        "note": "Claude ti napíše nebo se zeptá a počká na tvou odpověď.",
        "kind": "whatsapp",
        "field": {"name": "account", "label": "Tvoje číslo — kam má Claude psát",
                  "help": "Třeba +420 777 123 456. Odpovědi bere Claude jen z tohohle čísla."},
        "warn": "Claude se propojí s WhatsAppem na samostatném čísle (předplacená SIM, nebo "
                "WhatsApp Business na druhé SIM v telefonu). Je to neoficiální klient, takže "
                "nepropojuj svoje hlavní číslo — kdyby ho Meta zablokovala, přišel bys o něj.",
    },
    "google": {
        "label": "Google",
        "note": "Gmail, Disk, Kalendář, Dokumenty, Tabulky, Prezentace, "
                "Formuláře, Úkoly a Kontakty.",
        "kind": "google",
    },
    # Cokoli dalšího jako v oficiální appce Claude („Add custom connector"):
    # název a adresa vzdáleného MCP serveru, v pokročilých OAuth Client ID
    # a Secret pro servery, které si klienta neumí zaregistrovat samy (DCR).
    # Napojení se jmenuje podle názvu (`notion`), ne `vlastni-…` — tak ho
    # pojmenuje i Claude v nástrojích (mcp__notion__…).
    "vlastni": {
        "label": "Vlastní napojení",
        "note": "Jakýkoli vzdálený MCP server — stačí jeho adresa.",
        "kind": "custom",
        "field": {"name": "account", "label": "Adresa vzdáleného MCP serveru",
                  "help": "Třeba https://mcp.example.com/mcp — najdeš ji v dokumentaci služby."},
    },
}

GOOGLE_MCP_NAME = "google"
# Chat a Apps Script schválně ne: u soukromých účtů Google jejich oprávnění
# odmítne a přihlášení by padlo celé.
GOOGLE_TOOLS = ["gmail", "drive", "calendar", "docs", "sheets", "slides",
                "forms", "tasks", "contacts"]
# Odkud smí workspace-mcp číst soubory k nahrání (Disk, přílohy Gmailu).
# Bez toho jen z ~/.workspace-mcp/attachments a nahrání čehokoli, co Claude
# vyrobí, končí na „Local file could not be accessed". Domov a /tmp (tam má
# Claude Code scratchpad); Claude na oboje dosáhne i tak, nic se neotevírá.
# „~" rozbalí až workspace-mcp — na serveru tak platí domov prostoru.
GOOGLE_FILE_DIRS = os.pathsep.join(["~", tempfile.gettempdir()])
_G = "https://www.googleapis.com/auth/"
GOOGLE_SCOPES = ["openid", _G + "userinfo.email", _G + "userinfo.profile"] + [
    _G + s for s in (
        "gmail.readonly", "gmail.send", "gmail.compose", "gmail.modify",
        "gmail.labels", "gmail.settings.basic",
        "drive", "drive.readonly", "drive.file",
        "calendar", "calendar.readonly", "calendar.events",
        "documents", "documents.readonly",
        "spreadsheets", "spreadsheets.readonly",
        "presentations", "presentations.readonly",
        "forms.body", "forms.body.readonly", "forms.responses.readonly",
        "tasks", "tasks.readonly",
        "contacts", "contacts.readonly")]
GOOGLE_AUTH = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO = "https://openidconnect.googleapis.com/v1/userinfo"

# Návod na klienta OAuth — jen pro hub na počítači; na serveru ho má správce.
GOOGLE_SETUP = list(core.MCP_CATALOG.get("google-workspace", {}).get("setup") or []) + [
    {"title": "Zveřejni aplikaci",
     "text": "Na přihlašovací obrazovce dej Publikovat aplikaci (V produkci). "
             "V testovacím režimu Google přihlášení po 7 dnech zruší.",
     "button": "Otevřít obrazovku",
     "url": "https://console.cloud.google.com/apis/credentials/consent"},
]

LOGIN_TTL = 15 * 60
# Přesměrování po přihlášení u vlastního klienta OAuth (redirect URI ve službě).
CUSTOM_CALLBACK_PORT = 33418
CUSTOM_REDIRECT = f"http://localhost:{CUSTOM_CALLBACK_PORT}/callback"


# ── pomocné ──────────────────────────────────────────────────────────────────
def _claude():
    return shutil.which("claude") or ""


def _run(argv, timeout=60, env=None):
    try:
        return subprocess.run(argv, capture_output=True, text=True, cwd=core.HOME,
                              timeout=timeout, creationflags=core._NO_WINDOW,
                              env=env, stdin=subprocess.DEVNULL)
    except Exception as exc:
        return subprocess.CompletedProcess(argv, 1, "", str(exc))


def _slug(text, limit=30):
    """„Firma s.r.o." → „firma-s-r-o". Z popisku se skládá jméno serveru."""
    ascii_text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", ascii_text.lower()).strip("-")[:limit].strip("-")


def _user_servers():
    return core._claude_json().get("mcpServers") or {}


_recheck_waiting = threading.Event()


_unknown_at = 0.0


def _recheck():
    """Znovu zjistit stav napojení po změně. Když kontrola zrovna běží, začala
    před změnou a nový server nezná — pustí se proto ještě jednou, až doběhne."""
    if core.start_job("mcp", core.mcp_list) or _recheck_waiting.is_set():
        return
    _recheck_waiting.set()

    def later():
        try:
            while core.job_state("mcp").get("running"):
                time.sleep(0.5)
            core.start_job("mcp", core.mcp_list)
        finally:
            _recheck_waiting.clear()
    threading.Thread(target=later, daemon=True).start()


def _states():
    done = core.job_state("mcp").get("result") or {}
    return {s["name"]: (s["state"], s["status"]) for s in done.get("servers") or []}


def _clean(text):
    """Výstup terminálu bez řídicích sekvencí (barvy, odkazy OSC 8)."""
    text = re.sub(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)", "", text)
    text = re.sub(r"\x1b\[[0-9;?]*[ -/]*[@-~]", "", text)
    return text.replace("\r", "")


def _find_url(raw):
    """Adresa k přihlášení z výstupu `claude mcp login`. V terminálu ji Claude
    Code posílá jako odkaz OSC 8 — z něj je celá, i kdyby se text zalomil."""
    found = re.findall(r"\x1b\]8;;(https://[^\x07\x1b]+)", raw)
    found += re.findall(r"https://\S+", _clean(raw))
    for url in found:
        if "authorize" in url or "response_type=" in url:
            return url
    return found[0] if found else ""


def _last_line(text, skip=r"^(https?://|or paste|waiting for|visit this url)"):
    for line in reversed([l.strip() for l in text.split("\n")]):
        if line and not re.search(skip, line, re.I):
            return line[:300]
    return ""


def _exit_code(pty):
    proc = getattr(pty, "proc", None)
    try:
        if hasattr(proc, "wait"):
            return proc.wait(timeout=5)
        return getattr(proc, "exitstatus", None)
    except Exception:
        return None


# ── katalog a účty ───────────────────────────────────────────────────────────
def google_client():
    """(client_id, client_secret, odkud) klienta OAuth pro Google."""
    cid = os.environ.get("GOOGLE_OAUTH_CLIENT_ID", "").strip()
    secret = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET", "").strip()
    if cid and secret:
        return cid, secret, "server" if core.on_gateway() else "env"
    saved = core.CONFIG.get("google_oauth") or {}
    if isinstance(saved, dict) and saved.get("client_id") and saved.get("client_secret"):
        return saved["client_id"], saved["client_secret"], "hub"
    return "", "", ""


def _google_dir():
    base = (os.environ.get("WORKSPACE_MCP_CREDENTIALS_DIR")
            or os.environ.get("GOOGLE_MCP_CREDENTIALS_DIR"))
    if base:
        return os.path.expanduser(base)
    return os.path.join(core.HOME, ".google_workspace_mcp", "credentials")


def _google_file(email):
    # Stejně jako workspace-mcp: e-mail zakódovaný, @ . _ - zůstávají.
    return os.path.join(_google_dir(), urllib.parse.quote(email, safe="@._-") + ".json")


def _google_accounts():
    try:
        names = sorted(os.listdir(_google_dir()))
    except OSError:
        return []
    out = []
    for fn in names:
        email = urllib.parse.unquote(fn[:-5]) if fn.endswith(".json") else ""
        if "@" not in email:
            continue
        try:
            with open(os.path.join(_google_dir(), fn), encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            data = {}
        ok = isinstance(data, dict) and bool(data.get("refresh_token"))
        out.append({"name": email, "label": email, "detail": "",
                    "state": "ok" if ok else "auth",
                    "status": "připojeno" if ok else "přihlásit znovu"})
    return out


def _mcp_accounts(service_id, spec, states):
    host = urllib.parse.urlparse(spec["url"].replace("{account}", "x")).hostname or ""
    per_account = "{account}" in spec["url"]
    suffix = host.split(".", 1)[1] if per_account else host
    out = []
    for name, entry in sorted(_user_servers().items()):
        if not isinstance(entry, dict):
            continue
        if name != service_id and not name.startswith(service_id + "-"):
            continue
        entry_host = urllib.parse.urlparse(entry.get("url") or "").hostname or ""
        if entry_host != suffix and not entry_host.endswith("." + suffix):
            continue
        state, status = states.get(name, ("unknown", "zjišťuju…"))
        out.append({"name": name,
                    "label": name[len(service_id) + 1:] if name != service_id else "hlavní",
                    "state": state, "status": status,
                    "detail": entry_host if per_account else ""})
    return out


def _cmd_accounts(service_id, spec, states):
    """Účty služby spouštěné příkazem — podle jména a balíčku v příkazu."""
    out = []
    for name, entry in sorted(_user_servers().items()):
        if not isinstance(entry, dict):
            continue
        if name != service_id and not name.startswith(service_id + "-"):
            continue
        if not any(spec["package"] in str(a) for a in entry.get("args") or []):
            continue
        state, status = states.get(name, ("unknown", "zjišťuju…"))
        out.append({"name": name,
                    "label": name[len(service_id) + 1:] if name != service_id else "hlavní",
                    "state": state, "status": status, "detail": ""})
    return out


def _custom_accounts(states, claimed):
    """Vlastní napojení: vzdálené servery (adresa, ne příkaz), které nepatří
    žádné službě z katalogu ani sdílení. Ukážou se i ty přidané ručně přes
    `claude mcp add` — v oficiální appce jsou taky všechna pohromadě."""
    out = []
    for name, entry in sorted(_user_servers().items()):
        if not isinstance(entry, dict) or not entry.get("url"):
            continue
        if name in claimed or name.startswith(SHARED_PREFIX):
            continue
        state, status = states.get(name, ("unknown", "zjišťuju…"))
        out.append({"name": name, "label": name, "state": state, "status": status,
                    "detail": urllib.parse.urlparse(entry["url"]).hostname or ""})
    return out


def _catalog_names():
    """Jména serverů, které patří službám z katalogu (Freelo-firma, Clockify…)."""
    names = set()
    for sid, spec in SERVICES.items():
        if spec["kind"] in ("mcp", "apikey", "github"):
            names.update(a["name"] for a in _mcp_accounts(sid, spec, {}))
        elif spec["kind"] == "token":
            names.update(a["name"] for a in _cmd_accounts(sid, spec, {}))
    return names


def _custom_url(text):
    """Adresa vlastního serveru, nebo ValueError s větou pro člověka."""
    url = (text or "").strip()
    if url and "://" not in url:
        url = "https://" + url
    parts = urllib.parse.urlparse(url)
    local = parts.hostname in ("localhost", "127.0.0.1", "::1")
    if parts.scheme not in ("https", "http") or not parts.hostname:
        raise ValueError("Tohle nevypadá jako adresa serveru — čekám https://…")
    if parts.scheme == "http" and not local:
        raise ValueError("Adresa musí začínat https:// (http jde jen na tomhle počítači, localhost).")
    return url


def _server_state(name):
    """(stav, popis) jednoho serveru z `claude mcp get` — ptá se jen jeho."""
    r = _run([_claude(), "mcp", "get", name], 45)
    m = re.search(r"^\s*Status:\s*(.+)$", r.stdout or "", re.M)
    if not m:
        return "fail", _last_line(_clean(r.stderr or r.stdout or "")) or "server neodpověděl"
    return core._mcp_status(m.group(1))


def add_custom(label, url, client_id="", client_secret=""):
    """Vlastní napojení: registrace a podle odpovědi serveru buď hotovo
    (server přihlášení nechce), nebo přihlášení OAuth jako u Freela."""
    claude = _claude()
    if not claude:
        return {"ok": False, "detail": "Claude Code (claude) tu není nainstalovaný."}
    name = _slug(label)
    if not name:
        return {"ok": False, "detail": "Pojmenuj napojení, ať se dá poznat."}
    if name in _user_servers() or name in SERVICES or name.startswith(SHARED_PREFIX) \
            or name in ("playwright", GOOGLE_MCP_NAME):
        return {"ok": False, "detail": f"Napojení „{name}“ už je — zvol jiný název."}
    try:
        url = _custom_url(url)
    except ValueError as exc:
        return {"ok": False, "detail": str(exc)}
    client_id, client_secret = (client_id or "").strip(), (client_secret or "").strip()
    if client_secret and not client_id:
        return {"ok": False, "detail": "K OAuth Client Secret patří i Client ID."}
    if re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", client_id):
        return {"ok": False, "detail": "Do OAuth Client ID se dostal e-mail (nejspíš z "
                "automatického vyplnění). Pole nech prázdné — server si klienta zaregistruje sám."}
    path = urllib.parse.urlparse(url).path.rstrip("/")
    argv = [claude, "mcp", "add", "--transport", "sse" if path.endswith("/sse") else "http",
            "-s", "user", name, url]
    env = None
    if client_id:
        # Vlastní klient má u služby předem zapsanou adresu pro přesměrování;
        # bez pevného portu by Claude Code poslal pokaždé jiný a nesedělo by to.
        argv += ["--client-id", client_id, "--callback-port", str(CUSTOM_CALLBACK_PORT)]
    if client_secret:
        # Secret jen v proměnné prostředí, ne v příkazové řádce (ps by ho ukázal).
        argv.append("--client-secret")
        env = dict(os.environ, MCP_CLIENT_SECRET=client_secret)
    r = _run(argv, env=env)
    if r.returncode != 0:
        return {"ok": False, "detail": (r.stderr or r.stdout or "nepovedlo se").strip()[:300]}
    core.log(f"služby: přidáno vlastní napojení {name}")
    state, status = _server_state(name)
    if state == "ok":
        write_claude_md()
        _recheck()
        return {"ok": True, "name": name,
                "login": {"done": True, "message": f"Napojení {name} funguje."}}
    started = login_start(name) if state == "auth" else {"ok": False}
    if not started.get("ok"):
        # Nefunkční napojení v seznamu by jen mátlo — registrace pryč.
        _run([claude, "mcp", "logout", name], 30)
        _run([claude, "mcp", "remove", name, "-s", "user"], 30)
        detail = started.get("detail") or ""
        if state != "auth":
            hint = (f"Server na téhle adrese neodpovídá jako MCP server ({status}). "
                    "Zkontroluj adresu.")
        elif re.search(r"registration|client", detail, re.I) and not client_id:
            hint = ("Server si nenechá automaticky zaregistrovat klienta — v Pokročilém "
                    "nastavení zadej OAuth Client ID (a Secret) od té služby.")
        else:
            hint = f"Přihlášení se nerozběhlo: {detail or 'server ho odmítl'}"
        return {"ok": False, "detail": hint}
    write_claude_md()
    return {**started, "name": name}


def services(refresh=False):
    """Karty služeb s účty a jejich stavem. Stav je z `claude mcp list`, který
    se ptá každého serveru (~10 s) — počítá se na pozadí v úloze „mcp"."""
    job = core.job_state("mcp")
    if (refresh or not job.get("result")) and not job.get("running"):
        _recheck()
        job = core.job_state("mcp")
    states = _states()
    out = []
    claimed = set()                  # servery, které už patří službě z katalogu
    for sid, spec in SERVICES.items():
        item = {"id": sid, "label": spec["label"], "note": spec["note"],
                "redirect": CUSTOM_REDIRECT if spec["kind"] == "custom" else "",
                "kind": spec["kind"], "warn": spec.get("warn", ""),
                "field": spec.get("field")}
        if spec["kind"] == "google":
            cid, _, source = google_client()
            item["accounts"] = _google_accounts()
            item["client"] = source
            if not cid:
                if core.on_gateway():
                    item["missing"] = ("Google na tomhle serveru ještě nenastavil "
                                       "správce (claude-hub-admin google set).")
                else:
                    item["missing"] = ("Nejdřív je potřeba jednou založit klienta "
                                       "OAuth v Google Cloudu.")
                    item["setup"] = GOOGLE_SETUP
            elif not shutil.which("uvx"):
                item["missing"] = ("Chybí uv (uvx), na kterém napojení na Google "
                                   "běží: https://docs.astral.sh/uv/")
        elif spec["kind"] == "whatsapp":
            item["accounts"] = _wa_accounts(states)
            item["owner"] = "+" + _wa_config().get("owner", "") if _wa_config().get("owner") else ""
            if not shutil.which("node") or not shutil.which("npm"):
                item["missing"] = "Chybí Node.js (node a npm), na kterém WhatsApp běží: https://nodejs.org"
        elif spec["kind"] == "custom":
            item["accounts"] = _custom_accounts(states, claimed)
        elif spec["kind"] == "token":
            item["accounts"] = _cmd_accounts(sid, spec, states)
            item["setup"] = spec.get("setup")
            if not shutil.which("npx"):
                item["missing"] = "Chybí Node.js (npx), na kterém napojení běží: https://nodejs.org"
        else:
            item["accounts"] = _mcp_accounts(sid, spec, states)
            if spec.get("setup"):
                item["setup"] = spec["setup"]
            if spec["kind"] == "github" and not _gh():
                item["missing"] = ("Chybí GitHub CLI (gh), přes které Claude dělá to, co "
                                   "MCP server GitHubu neumí: https://cli.github.com")
        if not _claude():
            item["missing"] = "Claude Code (claude) tu není nainstalovaný."
        if spec["kind"] == "google":
            if item.get("accounts"):
                claimed.add(GOOGLE_MCP_NAME)  # jeden server pro všechny Google účty
        else:
            claimed.update(a["name"] for a in item.get("accounts") or [])
        out.append(item)
    # Účet, o jehož stavu se zatím neví (právě přidaný, nebo kontrola doběhla
    # před jeho přidáním), by zůstal navždy na „zjišťuju…" — kontrola se pustí
    # znovu (nejvýš jednou za minutu).
    global _unknown_at
    if not job.get("running") and not _recheck_waiting.is_set() and time.time() - _unknown_at > 60 and any(
            a.get("state") == "unknown" for it in out for a in it.get("accounts") or []):
        _unknown_at = time.time()
        _recheck()
        job = core.job_state("mcp")
    # `servers` = MCP servery, které už má některá karta služby — v seznamu
    # ostatních napojení se neopakují.
    return {"services": out, "checking": bool(job.get("running")) or _recheck_waiting.is_set(),
            "on_server": core.on_gateway(), "servers": sorted(claimed)}


def _key_rejected(spec, key):
    """Věta pro člověka, když služba klíč odmítla; jinak "" (i když se
    ověřit nepovedlo — výpadek sítě nemá napojení blokovat)."""
    check = spec.get("check")
    if not check:
        return ""
    req = urllib.request.Request(check["url"], headers={
        check["header"]: check.get("prefix", "") + key, "User-Agent": "claude-code-hub"})
    try:
        urllib.request.urlopen(req, timeout=15).close()
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            return f"{spec['label']} ten klíč nepřijal — zkopíruj ho znovu celý."
    except (urllib.error.URLError, OSError):
        pass
    return ""


def add_account(service, label="", account="", client_id="", client_secret=""):
    """Přidá účet a rovnou spustí přihlášení. Vrací {ok, name, login|detail}."""
    spec = SERVICES.get(service)
    if not spec:
        return {"ok": False, "detail": "Tuhle službu neznám."}
    if spec["kind"] == "google":
        return google_login_start()
    if spec["kind"] == "custom":
        return add_custom(label, account, client_id, client_secret)
    if spec["kind"] == "whatsapp":
        if not _claude():
            return {"ok": False, "detail": "Claude Code (claude) tu není nainstalovaný."}
        return whatsapp_add(account)
    if spec["kind"] == "github":
        if not _claude():
            return {"ok": False, "detail": "Claude Code (claude) tu není nainstalovaný."}
        return github_add_token(account) if (account or "").strip() else github_login_start()
    claude = _claude()
    if not claude:
        return {"ok": False, "detail": "Claude Code (claude) tu není nainstalovaný."}
    slug = _slug(label) or "ucet"
    name = f"{service}-{slug}"
    url = spec.get("url", "")
    if spec["kind"] == "token":
        key = (account or "").strip()
        if not key:
            return {"ok": False, "detail": f"Chybí {spec['field']['label']}."}
        bad = _key_rejected(spec, key)
        if bad:
            return {"ok": False, "detail": bad}
        if name in _user_servers():
            return {"ok": False, "detail": f"Účet „{slug}“ u služby {spec['label']} už je "
                                           "— zvol jiný popisek."}
        r = _run([claude, "mcp", "add", "-s", "user", name, "-e", f"{spec['env']}={key}",
                  "--", *spec["command"]])
        if r.returncode != 0:
            return {"ok": False, "detail": (r.stderr or r.stdout or "nepovedlo se").strip()[:300]}
        core.log(f"služby: přidán účet {name}")
        write_claude_md()
        _recheck()
        return {"ok": True, "name": name,
                "login": {"done": True, "message": f"{spec['label']} napojené."}}
    if spec["kind"] == "apikey":
        key = (account or "").strip()
        if not key:
            return {"ok": False, "detail": f"Chybí {spec['field']['label']}."}
        if spec.get("prefix") and key.lower().startswith(spec["prefix"].lower()):
            key = key[len(spec["prefix"]):].strip()      # vložil i „Bearer “
        bad = _key_rejected(spec, key)
        if bad:
            return {"ok": False, "detail": bad}
        if name in _user_servers():
            return {"ok": False, "detail": f"Účet „{slug}“ u služby {spec['label']} už je "
                                           "— zvol jiný popisek."}
        r = _run([claude, "mcp", "add", "--transport", "http", "-s", "user", name, url,
                  "--header", f"{spec['header']}: {spec.get('prefix', '')}{key}"])
        if r.returncode != 0:
            return {"ok": False, "detail": (r.stderr or r.stdout or "nepovedlo se").strip()[:300]}
        core.log(f"služby: přidán účet {name}")
        write_claude_md()
        _recheck()
        return {"ok": True, "name": name,
                "login": {"done": True, "message": f"{spec['label']} napojené."}}
    if name in _user_servers():
        return {"ok": False, "detail": f"Účet „{slug}“ u služby {spec['label']} už je "
                                       "— zvol jiný popisek."}
    r = _run([claude, "mcp", "add", "--transport", "http", "-s", "user", name, url])
    if r.returncode != 0:
        return {"ok": False, "detail": (r.stderr or r.stdout or "nepovedlo se").strip()[:300]}
    core.log(f"služby: přidán účet {name}")
    started = login_start(name)
    if not started.get("ok"):
        # Přihlášení se ani nerozběhlo (server třeba registraci klienta
        # odmítl). Nefunkční účet v seznamu by jen mátl, tak se registrace
        # vrátí zpátky.
        _run([claude, "mcp", "remove", name, "-s", "user"], 30)
        return {"ok": False, "detail": f"Přihlášení k {spec['label']} se nerozběhlo."}
    write_claude_md()
    return {**started, "name": name}


def remove_account(service, name):
    spec = SERVICES.get(service)
    if not spec:
        return {"ok": False, "detail": "Tuhle službu neznám."}
    claude = _claude()
    if spec["kind"] == "google":
        path = _google_file(name)
        if not os.path.isfile(path):
            return {"ok": False, "detail": "Tenhle Google účet tu není."}
        os.remove(path)
        if not _google_accounts() and GOOGLE_MCP_NAME in _user_servers() and claude:
            _run([claude, "mcp", "remove", GOOGLE_MCP_NAME, "-s", "user"], 30)
        elif GOOGLE_MCP_NAME in _user_servers() and claude:
            _ensure_google_server()        # odebraný mohl být ten výchozí
        write_claude_md()
        core.log("služby: odebrán Google účet")
        _recheck()
        return {"ok": True, "detail": f"Google účet {name} odebrán."}
    if spec["kind"] == "whatsapp":
        return whatsapp_remove()
    if spec["kind"] == "github" and name.startswith("github-") and name in _user_servers():
        return github_remove(name)
    if spec["kind"] == "custom":
        if name not in {a["name"] for a in _custom_accounts({}, _catalog_names())}:
            return {"ok": False, "detail": "Takové vlastní napojení tu není."}
    elif (name != service and not name.startswith(service + "-")) or \
            name not in _user_servers():
        return {"ok": False, "detail": "Tenhle účet tu není."}
    # Odhlásit dřív, než zmizí registrace — jinak by token zůstal v úložišti.
    _run([claude, "mcp", "logout", name], 30)
    r = _run([claude, "mcp", "remove", name, "-s", "user"], 30)
    if r.returncode != 0:
        return {"ok": False, "detail": (r.stderr or r.stdout or "nepovedlo se").strip()[:300]}
    write_claude_md()
    core.log(f"služby: odebrán účet {name}")
    _recheck()
    return {"ok": True, "detail": f"Napojení {name} odebráno." if spec["kind"] == "custom"
            else f"Účet {name} odebrán."}


def _google_default():
    """Účet, pod kterým Google nástroje jedou, když Claude e-mail nezadá.

    Bez něj je `user_google_email` povinný a Claude ho hádá — podle účtu
    Claude, nebo podle e-mailu někoho z týmu, na kterého narazil. Pro ten
    přihlášení není, nástroj vrátí „No valid credentials" a Claude pak
    tvrdí, že Google napojený není. S USER_GOOGLE_EMAIL je parametr
    nepovinný a bez něj se použije tenhle účet."""
    accounts = _google_accounts()
    ok = [a["name"] for a in accounts if a["state"] == "ok"]
    return (ok or [a["name"] for a in accounts] or [""])[0]


def _google_command():
    """Command + leading args of workspace-mcp. `uvx workspace-mcp` resolves PyPI on every start and on a slow
    network overruns Claude Code's 30 s MCP connect limit (the tools then vanish for the whole session), so the
    package is installed once as a uv tool and started from disk. Falls back to uvx when the install fails."""
    uv = shutil.which("uv")
    if uv:
        try:
            subprocess.run([uv, "tool", "install", "--quiet", "workspace-mcp"], capture_output=True, timeout=180)
        except Exception:
            pass
    exe = shutil.which("workspace-mcp") or os.path.expanduser("~/.local/bin/workspace-mcp")
    if os.path.isfile(exe) and os.access(exe, os.X_OK):
        return exe, []
    return "uvx", ["workspace-mcp"]


def _ensure_mcp_timeout():
    """Give MCP servers 2 minutes to connect (default 30 s) so one slow start (npx/uvx cold cache) does not
    leave a session without that server's tools."""
    path = os.path.expanduser("~/.claude/settings.json")
    try:
        with open(path, encoding="utf-8") as f:
            cfg = json.load(f)
    except FileNotFoundError:
        cfg = {}
    except Exception:
        return
    env = cfg.setdefault("env", {})
    if str(env.get("MCP_TIMEOUT", "")) not in ("", "30000") and int(str(env.get("MCP_TIMEOUT")) or 0) >= 120000:
        return
    env["MCP_TIMEOUT"] = "120000"
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _ensure_google_server():
    """Server workspace-mcp v Claude Code — jeden pro všechny Google účty."""
    claude = _claude()
    cid, secret, _ = google_client()
    _ensure_mcp_timeout()
    cmd, lead = _google_command()
    args = [*lead, "--tool-tier", "extended", "--tools", *GOOGLE_TOOLS]
    default = _google_default()
    entry = _user_servers().get(GOOGLE_MCP_NAME)
    env = (entry.get("env") or {}) if isinstance(entry, dict) else {}
    if isinstance(entry, dict) and entry.get("command") == cmd and entry.get("args") == args and \
            env.get("GOOGLE_OAUTH_CLIENT_ID") == cid and \
            env.get("USER_GOOGLE_EMAIL", "") == default and \
            env.get("ALLOWED_FILE_DIRS") == GOOGLE_FILE_DIRS:
        return {"ok": True}
    if entry:
        _run([claude, "mcp", "remove", GOOGLE_MCP_NAME, "-s", "user"], 30)
    extra = ["-e", f"USER_GOOGLE_EMAIL={default}"] if default else []
    r = _run([claude, "mcp", "add", GOOGLE_MCP_NAME, "-s", "user",
              "-e", f"GOOGLE_OAUTH_CLIENT_ID={cid}",
              "-e", f"GOOGLE_OAUTH_CLIENT_SECRET={secret}",
              "-e", f"ALLOWED_FILE_DIRS={GOOGLE_FILE_DIRS}", *extra,
              "--", cmd, *args])
    if r.returncode != 0:
        return {"ok": False, "detail": (r.stderr or r.stdout or "nepovedlo se").strip()[:300]}
    return {"ok": True}


def save_google_client(client_id, client_secret):
    """Klient OAuth pro hub na počítači. Na serveru ho drží správce brány."""
    if core.on_gateway():
        return {"ok": False, "detail": "Na serveru klienta nastavuje správce "
                                       "(claude-hub-admin google set)."}
    cid = (client_id or "").strip()
    secret = (client_secret or "").strip()
    if not cid.endswith(".apps.googleusercontent.com"):
        return {"ok": False, "detail": "Client ID končí na .apps.googleusercontent.com "
                                       "— zkontroluj, co jsi vložil."}
    if not secret:
        return {"ok": False, "detail": "Chybí client secret."}
    core.save_config({"google_oauth": {"client_id": cid, "client_secret": secret}})
    return {"ok": True, "detail": "Klient Google je uložený — teď už jen přidej účet."}


# ── přihlašování ─────────────────────────────────────────────────────────────
_LOGINS = {}
_LOCK = threading.Lock()


class _McpLogin:
    """`claude mcp login --no-browser` v pty: adresa ven, přesměrování dovnitř."""
    kind = "mcp"

    def __init__(self, name):
        self.id = secrets.token_urlsafe(12)
        self.name = name
        self.started = time.time()
        self.raw = ""
        self.since = ""          # výstup od posledního vložení adresy
        self.url = ""
        self.done = False
        self.ok = False
        self.message = ""
        env = core.child_env()
        env["BROWSER"] = "true"   # prohlížeč na stroji hubu neotvírat nikdy
        # Široký terminál: dlouhá adresa se nesmí zalomit do dvou řádků.
        self.pty = pty_backend.spawn([_claude(), "mcp", "login", name, "--no-browser"],
                                     cwd=core.HOME, env=env, cols=4000, rows=40)
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self):
        while True:
            chunk = self.pty.read()
            if not chunk:
                break
            text = chunk.decode("utf-8", "replace")
            with _LOCK:
                self.raw = (self.raw + text)[-20000:]
                self.since = (self.since + text)[-8000:]
                if not self.url:
                    self.url = _find_url(self.raw)
        code = _exit_code(self.pty)
        text = _clean(self.raw)
        with _LOCK:
            self.done = True
            self.ok = code == 0 and not re.search(r"couldn.t complete|failed", text[-800:], re.I)
            self.message = (f"Účet {self.name} je přihlášený." if self.ok
                            else _last_line(text) or "Přihlášení se nepovedlo.")
        _recheck()

    def paste(self, url, wait=20):
        with _LOCK:
            self.since = ""
            self.message = ""
        self.pty.write((url.strip() + "\r").encode("utf-8"))
        end = time.time() + wait
        while time.time() < end:
            time.sleep(0.25)
            with _LOCK:
                if self.done:
                    return
                since = _clean(self.since)
            if re.search(r"doesn.t look like|invalid|mismatch|expired|try again", since, re.I):
                with _LOCK:
                    self.message = ("Tahle adresa k přihlášení nepatří — vlož celou "
                                    "adresu ze stránky, kam tě přihlášení poslalo.")
                return
        with _LOCK:
            self.message = "Zatím nic — zkontroluj, že je adresa celá, a zkus to znovu."

    def cancel(self):
        try:
            self.pty.close()
        except Exception:
            pass


class _GoogleLogin:
    """OAuth s PKCE, který obstará hub. Na počítači přesměrování přijme malý
    posluchač na 127.0.0.1; na serveru se adresa vloží (complete)."""
    kind = "google"

    def __init__(self):
        self.client_id, self.client_secret, _ = google_client()
        self.id = secrets.token_urlsafe(12)
        self.started = time.time()
        self.done = False
        self.ok = False
        self.message = ""
        self.lock = threading.Lock()
        self.state = secrets.token_urlsafe(24)
        self.verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(
            hashlib.sha256(self.verifier.encode()).digest()).rstrip(b"=").decode()
        login = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                query = urllib.parse.urlparse(self.path).query
                if "state=" not in query:
                    self.send_response(404)
                    self.end_headers()
                    return
                login.complete("http://127.0.0.1/?" + query)
                text = ("✓ Hotovo — Google účet je napojený. Okno můžeš zavřít."
                        if login.ok else "Přihlášení se nepovedlo: " + login.message)
                body = ("<!doctype html><meta charset=utf-8><title>Google</title>"
                        "<body style='font:16px sans-serif;padding:40px'>"
                        + html.escape(text)).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        self.redirect = f"http://127.0.0.1:{self.server.server_address[1]}/"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = GOOGLE_AUTH + "?" + urllib.parse.urlencode({
            "client_id": self.client_id, "redirect_uri": self.redirect,
            "response_type": "code", "scope": " ".join(GOOGLE_SCOPES),
            "code_challenge": challenge, "code_challenge_method": "S256",
            "state": self.state, "access_type": "offline", "prompt": "consent"})

    def complete(self, url):
        with self.lock:
            if self.done:
                return
            q = urllib.parse.parse_qs(urllib.parse.urlparse((url or "").strip()).query)
            if q.get("error"):
                self.message = "Google přihlášení odmítl: " + q["error"][0]
                return
            if (q.get("state") or [""])[0] != self.state or not q.get("code"):
                self.message = ("Tahle adresa k tomuhle přihlášení nepatří — vlož "
                                "celou adresu ze stránky, kam tě Google poslal.")
                return
            try:
                token = _post_form(GOOGLE_TOKEN, {
                    "code": q["code"][0], "client_id": self.client_id,
                    "client_secret": self.client_secret, "redirect_uri": self.redirect,
                    "grant_type": "authorization_code", "code_verifier": self.verifier})
                info = _get_json(GOOGLE_USERINFO, token.get("access_token", ""))
            except Exception as exc:
                self.message = f"Google přihlášení nedokončil: {exc}"
                self.done = True
                self._stop()
                return
            email = (info.get("email") or "").strip()
            if not email:
                self.message = "Google neřekl, o jaký účet jde (chybí e-mail)."
                self.done = True
                self._stop()
                return
            _write_google_token(email, token, self.client_id, self.client_secret)
            ensured = _ensure_google_server()
            write_claude_md()
            self.done = True
            self.ok = ensured["ok"]
            self.message = (f"Google účet {email} je napojený." if self.ok
                            else ensured.get("detail") or "Napojení se nepovedlo.")
            if self.ok and not token.get("refresh_token"):
                self.message += " Google ale nevydal trvalé přihlášení — po hodině se přihlas znovu."
        core.log("služby: napojen Google účet")
        _recheck()
        self._stop()

    def _stop(self):
        # Z obsluhy požadavku by shutdown() čekal sám na sebe.
        threading.Thread(target=self.server.shutdown, daemon=True).start()

    def cancel(self):
        self._stop()


def _post_form(url, data):
    req = urllib.request.Request(url, data=urllib.parse.urlencode(data).encode(),
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    return _open_json(req)


def _get_json(url, access_token):
    return _open_json(urllib.request.Request(
        url, headers={"Authorization": f"Bearer {access_token}"}))


def _open_json(req):
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.load(r)
    except urllib.error.HTTPError as exc:
        try:
            err = json.load(exc)
            detail = err.get("error_description") or err.get("error") or ""
        except Exception:
            detail = ""
        raise RuntimeError(detail or f"HTTP {exc.code}") from None


def _write_google_token(email, token, client_id, client_secret):
    """Token ve tvaru, který čte workspace-mcp (credential_store.py)."""
    folder = _google_dir()
    os.makedirs(folder, exist_ok=True)
    now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    data = {
        "token": token.get("access_token"),
        "refresh_token": token.get("refresh_token"),
        "token_uri": GOOGLE_TOKEN,
        "client_id": client_id,
        "client_secret": client_secret,
        "scopes": (token.get("scope") or "").split() or GOOGLE_SCOPES,
        "expiry": (now + datetime.timedelta(
            seconds=int(token.get("expires_in") or 3600))).isoformat(),
    }
    path = _google_file(email)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    os.replace(tmp, path)


# ── GitHub: MCP server + gh pod jedním přihlášením ──────────────────────────
GITHUB_DEVICE = "https://github.com/login/device"
# Všechno, co klasický token umí, kromě enterprise a kódových prostorů.
GITHUB_SCOPES = ("repo,workflow,admin:org,admin:repo_hook,admin:org_hook,delete_repo,"
                 "gist,user,project,notifications,write:packages,delete:packages,"
                 "admin:public_key,admin:gpg_key,admin:ssh_signing_key")


def _gh():
    return shutil.which("gh") or ""


def _github_user(token):
    """Přihlašovací jméno na GitHubu k tokenu, nebo ""."""
    req = urllib.request.Request("https://api.github.com/user", headers={
        "Authorization": "Bearer " + token, "User-Agent": "claude-code-hub",
        "Accept": "application/vnd.github+json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return str(json.loads(resp.read().decode("utf-8")).get("login") or "")
    except (OSError, ValueError):
        return ""


def _gh_accounts():
    """Účty, pod kterými je `gh` přihlášené (podle `gh auth status`)."""
    if not _gh():
        return []
    r = _run([_gh(), "auth", "status", "-h", "github.com"], 30)
    return re.findall(r"account (\S+)", (r.stdout or "") + (r.stderr or ""))


def _gh_login_token(token):
    """`gh auth login --with-token` + git přes gh. Vrací "" nebo větu o chybě."""
    try:
        r = subprocess.run([_gh(), "auth", "login", "-h", "github.com", "-p", "https",
                            "--with-token"], input=token + "\n", capture_output=True,
                           text=True, cwd=core.HOME, timeout=60,
                           creationflags=core._NO_WINDOW)
    except Exception as exc:
        return str(exc)
    if r.returncode != 0:
        err = (r.stderr or r.stdout or "").strip()
        if "read:org" in err:
            return ("gh ten token nevzalo — klasickému tokenu chybí oprávnění read:org "
                    "(nebo admin:org). Vytvoř ho tlačítkem výše, tam je všechno předvyplněné.")
        return err[:300] or "gh se nepřihlásilo."
    return ""


def _github_register(token=""):
    """Po přihlášení `gh`: git přes gh a MCP server `github-<login>` se stejným
    tokenem. Vrací (ok, zpráva, jméno serveru)."""
    gh = _gh()
    if not token:
        r = _run([gh, "auth", "token", "-h", "github.com"], 30)
        token = (r.stdout or "").strip()
    login = _github_user(token) if token else ""
    if not login:
        return False, "GitHub přihlášení nepotvrdil — zkus to znovu.", ""
    _run([gh, "auth", "setup-git", "-h", "github.com"], 30)
    name = "github-" + (_slug(login, 40) or "ucet")
    claude = _claude()
    if name in _user_servers():          # znovu přihlášený účet → nový token
        _run([claude, "mcp", "remove", name, "-s", "user"], 30)
    spec = SERVICES["github"]
    r = _run([claude, "mcp", "add", "--transport", "http", "-s", "user", name, spec["url"],
              "--header", f"{spec['header']}: {spec['prefix']}{token}"])
    if r.returncode != 0:
        return False, (r.stderr or r.stdout or "nepovedlo se").strip()[:300], ""
    core.log(f"služby: GitHub {login} napojený (MCP + gh)")
    write_claude_md()
    _recheck()
    return True, f"GitHub {login} napojený — MCP nástroje i gh a git.", name


class _GithubLogin:
    """`gh auth login --web` v pty: jednorázový kód ven, GitHub potvrdí sám
    (gh se ho mezitím ptá), na konci se zaregistruje MCP server."""
    kind = "github"

    def __init__(self):
        self.id = secrets.token_urlsafe(12)
        self.started = time.time()
        self.raw = ""
        self.code = ""
        self.done = False
        self.ok = False
        self.message = ""
        self._entered = False
        self._answered = 0
        env = core.child_env()
        env["BROWSER"] = env["GH_BROWSER"] = "true"   # prohlížeč tady neotvírat
        env.pop("GH_TOKEN", None)
        env.pop("GITHUB_TOKEN", None)
        self.pty = pty_backend.spawn([_gh(), "auth", "login", "-h", "github.com", "-p", "https",
                                      "--web", "-s", GITHUB_SCOPES],
                                     cwd=core.HOME, env=env, cols=400, rows=40)
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self):
        while True:
            chunk = self.pty.read()
            if not chunk:
                break
            with _LOCK:
                self.raw = (self.raw + chunk.decode("utf-8", "replace"))[-20000:]
                text = _clean(self.raw)
                if not self.code:
                    m = re.search(r"one-time code:\s*([A-Z0-9]{4}-[A-Z0-9]{4})", text)
                    self.code = m.group(1) if m else ""
            # „Press Enter to open github.com…" a případné „(Y/n)" — výchozí volba.
            asked = len(re.findall(r"\(Y/n\)", text))
            if ("Press Enter" in text and not self._entered) or asked > self._answered:
                self._entered = "Press Enter" in text
                self._answered = asked
                try:
                    self.pty.write(b"\r")
                except OSError:
                    pass
        code = _exit_code(self.pty)
        ok, message = False, _last_line(_clean(self.raw)) or "Přihlášení se nepovedlo."
        if code == 0:
            ok, message, _ = _github_register()
        with _LOCK:
            self.done, self.ok, self.message = True, ok, message

    def paste(self, url, wait=0):
        with _LOCK:
            self.message = "Tady se nic nevkládá — stačí na GitHubu zadat kód a potvrdit."

    def cancel(self):
        try:
            self.pty.close()
        except Exception:
            pass


def github_login_start():
    if not _gh():
        return {"ok": False, "detail": "Chybí GitHub CLI (gh): https://cli.github.com"}
    _cleanup()
    try:
        login = _GithubLogin()
    except (pty_backend.PtyUnavailable, OSError) as exc:
        return {"ok": False, "detail": f"Přihlášení nejde spustit: {exc}"}
    with _LOCK:
        _LOGINS[login.id] = login
    end = time.time() + 25
    while time.time() < end:
        with _LOCK:
            if login.code or login.done:
                break
        time.sleep(0.2)
    with _LOCK:
        if login.code and not login.done:
            return {"ok": True, "login": {"id": login.id, "url": GITHUB_DEVICE,
                                          "code": login.code}}
        detail = login.message or _last_line(_clean(login.raw)) or "gh kód nevypsalo."
    login.cancel()
    return {"ok": False, "detail": detail}


def github_add_token(token):
    token = (token or "").strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    if not token:
        return {"ok": False, "detail": "Chybí token."}
    if not _gh():
        return {"ok": False, "detail": "Chybí GitHub CLI (gh): https://cli.github.com"}
    bad = _key_rejected(SERVICES["github"], token)
    if bad:
        return {"ok": False, "detail": bad}
    bad = _gh_login_token(token)
    if bad:
        return {"ok": False, "detail": bad}
    ok, message, name = _github_register(token)
    if not ok:
        return {"ok": False, "detail": message}
    return {"ok": True, "name": name, "login": {"done": True, "message": message}}


def github_remove(name):
    """MCP server pryč a `gh` se z toho účtu odhlásí — „odebrat" má znamenat,
    že Claude na GitHub nesmí vůbec, ne jen přes MCP."""
    claude, spec = _claude(), SERVICES["github"]
    auth = str(((_user_servers().get(name) or {}).get("headers") or {}).get(spec["header"]) or "")
    login = _github_user(auth[len(spec["prefix"]):]) if auth.startswith(spec["prefix"]) else ""
    login = (login or name[len("github-"):]).lower()
    r = _run([claude, "mcp", "remove", name, "-s", "user"], 30)
    if r.returncode != 0:
        return {"ok": False, "detail": (r.stderr or r.stdout or "nepovedlo se").strip()[:300]}
    for acc in _gh_accounts():
        if acc.lower() == login:
            _run([_gh(), "auth", "logout", "-h", "github.com", "-u", acc], 30)
    write_claude_md()
    core.log(f"služby: odebrán GitHub {name}")
    _recheck()
    return {"ok": True, "detail": f"GitHub {login} odebrán (i z gh)."}


def github_upgrade():
    """Účty z 2.67 (jen MCP, výchozí sady nástrojů, gh nepřihlášené) převede
    na plné napojení — se stejným tokenem, bez nového přihlašování."""
    claude, spec = _claude(), SERVICES["github"]
    for name, entry in list(_user_servers().items()):
        if not name.startswith("github-") or not isinstance(entry, dict):
            continue
        url = entry.get("url") or ""
        if "api.githubcopilot.com" not in url:
            continue
        auth = str((entry.get("headers") or {}).get(spec["header"]) or "")
        token = auth[len(spec["prefix"]):] if auth.startswith(spec["prefix"]) else ""
        if url.rstrip("/") == spec["url"] or not token:
            continue
        if _gh() and not _gh_accounts():
            bad = _gh_login_token(token)
            if not bad:
                _run([_gh(), "auth", "setup-git", "-h", "github.com"], 30)
        _run([claude, "mcp", "remove", name, "-s", "user"], 30)
        r = _run([claude, "mcp", "add", "--transport", "http", "-s", "user", name,
                  spec["url"], "--header", f"{spec['header']}: {spec['prefix']}{token}"])
        core.log(f"služby: GitHub {name} převeden na všechny nástroje"
                 + ("" if r.returncode == 0 else f" — selhalo: {(r.stderr or '')[:200]}"),
                 "info" if r.returncode == 0 else "warn")


# ── WhatsApp: démon s jedním spojením + MCP pro každou konverzaci ────────────
WA_MCP_NAME = "whatsapp"
WA_BAILEYS = "baileys@7.0.0-rc14"     # pevná verze: cizí kód se nemá měnit pod rukama
WA_DIR = os.path.join(core.CLAUDE_DIR, "whatsapp")


def _wa_script():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(root, "tools", "whatsapp_mcp.mjs")


def _wa_call(name, body=None, timeout=10):
    """Volání démona (127.0.0.1 + tajný žeton z daemon.json); None, když neběží."""
    try:
        with open(os.path.join(WA_DIR, "daemon.json"), encoding="utf-8") as fh:
            info = json.load(fh)
        req = urllib.request.Request(f"http://127.0.0.1:{int(info['port'])}/{name}",
                                     data=json.dumps(body or {}).encode("utf-8"), method="POST",
                                     headers={"Content-Type": "application/json",
                                              "X-Token": str(info["token"])})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _wa_linked():
    return os.path.isfile(os.path.join(WA_DIR, "auth", "creds.json"))


def _wa_config():
    try:
        with open(os.path.join(WA_DIR, "config.json"), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def _wa_save_config(data):
    os.makedirs(WA_DIR, mode=0o700, exist_ok=True)
    tmp = os.path.join(WA_DIR, "config.json.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    os.replace(tmp, os.path.join(WA_DIR, "config.json"))


def _wa_number(text):
    """„+420 777 123 456" → „420777123456"; devět číslic = české číslo."""
    num = re.sub(r"\D", "", text or "")
    if num.startswith("00"):
        num = num[2:]
    if len(num) == 9:
        num = "420" + num
    return num if 10 <= len(num) <= 15 else ""


def _wa_installed():
    pkg = os.path.join(WA_DIR, "node_modules", "baileys", "package.json")
    try:
        with open(pkg, encoding="utf-8") as fh:
            return json.load(fh).get("version") == WA_BAILEYS.split("@", 1)[1]
    except (OSError, ValueError):
        return False


def _wa_install():
    """Knihovna do ~/.claude/whatsapp/node_modules (na serveru je kód hubu jen ke čtení)."""
    npm = shutil.which("npm")
    if not npm:
        return "Chybí Node.js (npm): https://nodejs.org"
    os.makedirs(WA_DIR, mode=0o700, exist_ok=True)
    pkg = os.path.join(WA_DIR, "package.json")
    if not os.path.isfile(pkg):
        with open(pkg, "w", encoding="utf-8") as fh:
            fh.write('{"private": true}\n')
    r = _run([npm, "install", "--prefix", WA_DIR, "--no-audit", "--no-fund", "--omit=dev",
              "--loglevel=error", WA_BAILEYS], timeout=600)
    if r.returncode != 0 or not _wa_installed():
        return "Knihovna WhatsAppu se nenainstalovala: " + \
            (_last_line(r.stderr or r.stdout or "") or "npm selhal")
    return ""


def _wa_spawn(pair=False):
    node = shutil.which("node")
    if not node:
        return False
    argv = [node, _wa_script(), "daemon"] + (["--pair"] if pair else [])
    kwargs = {"cwd": WA_DIR, "stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
              "stderr": subprocess.DEVNULL}
    if core.IS_WINDOWS:
        kwargs["creationflags"] = subprocess.DETACHED_PROCESS | core._NO_WINDOW
    else:
        kwargs["start_new_session"] = True
    try:
        subprocess.Popen(argv, **kwargs)
        return True
    except OSError:
        return False


def _wa_stop():
    if _wa_call("stop", timeout=5) is None:
        return
    for _ in range(30):
        time.sleep(0.2)
        if _wa_call("status", timeout=2) is None:
            return


def _wa_register():
    """MCP server `whatsapp` (stdio) — každá konverzace si pustí tenký most."""
    claude = _claude()
    if WA_MCP_NAME in _user_servers():
        _run([claude, "mcp", "remove", WA_MCP_NAME, "-s", "user"], 30)
    r = _run([claude, "mcp", "add", "-s", "user", WA_MCP_NAME, "--",
              shutil.which("node") or "node", _wa_script(), "mcp"])
    return "" if r.returncode == 0 else (r.stderr or r.stdout or "nepovedlo se").strip()[:300]


WA_WELCOME = ("Ahoj, tady Claude 👋 Odsud ti budu psát, když budu něco potřebovat "
              "nebo bude hotovo. Na otázky odpovídej sem.")


class _WhatsappLogin:
    """Instalace knihovny → démon v režimu párování → QR do nastavení →
    po naskenování registrace MCP a uvítací zpráva (ověří celou cestu)."""
    kind = "whatsapp"

    def __init__(self):
        self.id = secrets.token_urlsafe(12)
        self.started = time.time()
        self.done = False
        self.ok = False
        self.message = "Připravuju…"
        self.qr = ""
        self._qr_text = ""
        self._stop = False
        threading.Thread(target=self._run, daemon=True).start()

    def extra(self):
        return {"qr": self.qr, "phase": self.message if not self.done else ""}

    def _finish(self, ok, message):
        with _LOCK:
            self.done, self.ok, self.message = True, ok, message

    def _run(self):
        try:
            if not _wa_installed():
                self.message = "Instaluju knihovnu WhatsAppu (poprvé to trvá kolem minuty)…"
                bad = _wa_install()
                if bad:
                    return self._finish(False, bad)
            self.message = "Spouštím spojení…"
            _wa_stop()
            shutil.rmtree(os.path.join(WA_DIR, "auth"), ignore_errors=True)
            if not _wa_spawn(pair=True):
                return self._finish(False, "Spojení s WhatsAppem nejde spustit (chybí node?).")
            end = time.time() + LOGIN_TTL
            while time.time() < end and not self._stop:
                time.sleep(1)
                st = _wa_call("status", timeout=3)
                if not st:
                    continue
                state = st.get("state")
                if state == "qr" and st.get("qr") and st["qr"] != self._qr_text:
                    self._qr_text = st["qr"]
                    with _LOCK:
                        self.qr = qr.svg(st["qr"], quiet=2, scale=5)
                        self.message = "Naskenuj QR telefonem s Claudovým číslem."
                elif state == "open":
                    with _LOCK:
                        self.qr = ""
                        self.message = "Propojeno, ověřuju zprávou…"
                    bad = _wa_register()
                    if bad:
                        return self._finish(False, bad)
                    sent = _wa_call("send", {"text": WA_WELCOME}, timeout=60) or {}
                    write_claude_md()
                    _recheck()
                    me = st.get("me") or ""
                    core.log(f"služby: WhatsApp propojený jako {me}")
                    if not sent.get("ok"):
                        return self._finish(True, f"WhatsApp {me} propojený, ale uvítací zprávu "
                                            f"se poslat nepovedlo: {sent.get('error') or 'bez odpovědi'}")
                    return self._finish(True, f"WhatsApp {me} propojený — poslal jsem ti "
                                        f"zkušební zprávu na {sent.get('sent_to')}.")
                elif state == "logged_out":
                    return self._finish(False, st.get("detail") or "WhatsApp propojení odmítl.")
            if not self._stop:
                _wa_stop()
                self._finish(False, "QR nikdo nenaskenoval — začni znovu.")
        except Exception as exc:          # vlákno nesmí umřít potichu
            self._finish(False, f"Propojení se nepovedlo: {exc}")

    def paste(self, url, wait=0):
        pass

    def cancel(self):
        self._stop = True
        if not self.done:
            _wa_stop()


def whatsapp_add(number):
    owner = _wa_number(number)
    if not owner:
        return {"ok": False, "detail": "Tohle nevypadá jako telefonní číslo — napiš ho i s předvolbou, "
                                       "třeba +420 777 123 456."}
    if not shutil.which("node") or not shutil.which("npm"):
        return {"ok": False, "detail": "Chybí Node.js (node a npm): https://nodejs.org"}
    _wa_save_config({**_wa_config(), "owner": owner})
    # Už propojený a běží → jen jiné číslo příjemce, nic se znovu neskenuje.
    st = _wa_call("status", timeout=3) or {}
    if _wa_linked() and st.get("state") == "open" and WA_MCP_NAME in _user_servers():
        write_claude_md()
        return {"ok": True, "name": WA_MCP_NAME,
                "login": {"done": True, "message": f"Claude teď píše na +{owner}."}}
    _cleanup()
    login = _WhatsappLogin()
    with _LOCK:
        _LOGINS[login.id] = login
    return {"ok": True, "name": WA_MCP_NAME,
            "login": {"id": login.id, "qr": "", "phase": login.message}}


def whatsapp_remove():
    """Odpojí zařízení na straně WhatsAppu (zmizí z Propojených zařízení),
    zastaví démona a zruší napojení v Claude Code. Knihovna zůstane."""
    if _wa_call("logout", timeout=20) is None:
        shutil.rmtree(os.path.join(WA_DIR, "auth"), ignore_errors=True)
    for name in ("inbox.json", "inbox.json.read"):
        try:
            os.remove(os.path.join(WA_DIR, name))
        except OSError:
            pass
    if WA_MCP_NAME in _user_servers():
        _run([_claude(), "mcp", "remove", WA_MCP_NAME, "-s", "user"], 30)
    write_claude_md()
    core.log("služby: WhatsApp odpojený")
    _recheck()
    return {"ok": True, "detail": "WhatsApp odpojený — zmizí i z Propojených zařízení v telefonu."}


def _wa_accounts(states):
    if WA_MCP_NAME not in _user_servers():
        return []
    cfg = _wa_config()
    owner = "+" + cfg["owner"] if cfg.get("owner") else ""
    st = _wa_call("status", timeout=2)
    if st and st.get("state") == "open":
        state, status = "ok", "připojeno" + (f" · píše na {owner}" if owner else "")
    elif st and st.get("state") in ("logged_out", "not_linked"):
        state, status = "fail", "odpojeno — propoj znovu"
    elif not _wa_linked():
        state, status = "fail", "nepropojeno — propoj znovu"
    elif st:
        state, status = "unknown", "připojuju se…"
    else:
        state, status = "ok", "spí — probudí se, až ho Claude použije" + \
            (f" · píše na {owner}" if owner else "")
    label = (st or {}).get("me") or "WhatsApp"
    return [{"name": WA_MCP_NAME, "label": label, "state": state, "status": status, "detail": ""}]


def _cleanup():
    now = time.time()
    with _LOCK:
        old = [k for k, v in _LOGINS.items() if now - v.started > LOGIN_TTL]
        stale = [_LOGINS.pop(k) for k in old]
    for login in stale:
        login.cancel()


def login_start(name):
    """Přihlášení existujícího účtu (Freelo, Canva, Meta reklamy)."""
    _cleanup()
    if name not in _user_servers():
        return {"ok": False, "detail": "Tenhle účet tu není."}
    try:
        login = _McpLogin(name)
    except (pty_backend.PtyUnavailable, OSError) as exc:
        return {"ok": False, "detail": f"Přihlášení nejde spustit: {exc}"}
    with _LOCK:
        _LOGINS[login.id] = login
    end = time.time() + 25
    while time.time() < end:
        with _LOCK:
            if login.url or login.done:
                break
        time.sleep(0.2)
    with _LOCK:
        if login.url and not login.done:
            return {"ok": True, "login": {"id": login.id, "url": login.url}}
        if login.done and login.ok:
            return {"ok": True, "login": {"id": login.id, "url": "", "done": True,
                                          "message": login.message}}
        detail = login.message or _last_line(_clean(login.raw)) or \
            "Claude Code adresu k přihlášení nevypsal."
    login.cancel()
    return {"ok": False, "detail": detail}


def google_login_start():
    cid, _, _ = google_client()
    if not cid:
        return {"ok": False, "detail": "Google zatím nemá klienta OAuth."}
    if not shutil.which("uvx"):
        return {"ok": False, "detail": "Chybí uv (uvx): https://docs.astral.sh/uv/"}
    if not _claude():
        return {"ok": False, "detail": "Claude Code (claude) tu není nainstalovaný."}
    _cleanup()
    login = _GoogleLogin()
    with _LOCK:
        _LOGINS[login.id] = login
    return {"ok": True, "login": {"id": login.id, "url": login.url}}


def login_status(login_id):
    login = _LOGINS.get(login_id)
    if not login:
        return {"done": True, "ok": False, "message": "Přihlášení vypršelo — začni znovu."}
    out = {"done": login.done, "ok": login.ok, "message": login.message}
    if hasattr(login, "extra"):
        out.update(login.extra())
    return out


def login_finish(login_id, url):
    login = _LOGINS.get(login_id)
    if not login:
        return login_status(login_id)
    if not (url or "").strip():
        return {"done": False, "ok": False, "message": "Vlož adresu z adresního řádku."}
    if login.kind == "google":
        login.complete(url)
    else:
        login.paste(url)
    return login_status(login_id)


def login_cancel(login_id):
    with _LOCK:
        login = _LOGINS.pop(login_id, None)
    if login:
        login.cancel()
    return {"ok": True}


# ── sdílená napojení (jen v prostoru na serveru) ─────────────────────────────
SHARED_PREFIX = "sdilene-"
SHARED_SYNC_EVERY = 120
_shared_lock = threading.Lock()


def _gateway_call(body, timeout=15):
    """Loopback brány se žetonem prostoru — stejná cesta jako most na počítač."""
    url = os.environ.get("HUB_POCITAC_URL", "").rstrip("/")
    token = os.environ.get("HUB_POCITAC_TOKEN", "")
    if not url or not token:
        return None
    req = urllib.request.Request(url + "/gw/mcp-sdilene/volani",
                                 data=json.dumps(body).encode("utf-8"), method="POST",
                                 headers={"Content-Type": "application/json",
                                          "X-Hub-Pocitac": token})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (OSError, ValueError):
        return None


def sync_shared(force=True):
    """Srovná `sdilene-*` v Claude Code s tím, co s uživatelem kdo sdílí.

    Každé sdílené napojení je v Claude Code stdio most tools/sdilene_mcp.py —
    klíče drží brána (gateway/mcp_sdilene.py). Nová napojení uvidí Claude Code
    od další konverzace; odebraná brána odmítne hned, tady se jen uklidí.
    Volá se při startu hubu a když se v nastavení se sdílením něco změní.
    Vrací počet napojení, nebo None mimo prostor na serveru."""
    claude = _claude()
    data = _gateway_call({"op": "seznam"})
    if not claude or not data or not data.get("ok"):
        return None
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    script = os.path.join(root, "tools", "sdilene_mcp.py")
    python = sys.executable or "python3"
    with _shared_lock:
        have = {n: s for n, s in _user_servers().items() if n.startswith(SHARED_PREFIX)}
        want = {n["mcp_name"]: n["slug"] for n in data.get("napojeni") or []
                if isinstance(n, dict) and re.fullmatch(r"sdilene-[a-z0-9-]{1,40}",
                                                        str(n.get("mcp_name") or ""))}
        changed = False
        for name, spec in have.items():
            if name in want and spec.get("command") == python \
                    and spec.get("args") == [script, want[name]]:
                continue
            _run([claude, "mcp", "remove", name, "-s", "user"])
            changed = True
        for name, slug in want.items():
            spec = have.get(name) or {}
            if spec.get("command") == python and spec.get("args") == [script, slug]:
                continue
            changed = True
            r = _run([claude, "mcp", "add", name, "-s", "user", "--", python, script, slug])
            if r.returncode != 0:
                core.log(f"sdílené napojení {name}: {(r.stderr or r.stdout or '').strip()[:200]}",
                         "warn")
    if changed or force:
        write_claude_md()
    return len(want)


def start_shared_sync():
    threading.Thread(target=_sync_on_start, daemon=True, name="napojeni").start()


def _sync_on_start():
    # Google napojený starší verzí hubu nemá výchozí účet ani ALLOWED_FILE_DIRS.
    try:
        if GOOGLE_MCP_NAME in _user_servers() and _google_accounts() and \
                _claude() and google_client()[0]:
            _ensure_google_server()
    except Exception as exc:
        core.log(f"napojení Google při startu: {exc}", "warn")
    try:
        # Odpovědi a zprávy chodí, i když zrovna žádná konverzace WhatsApp nepoužívá.
        if WA_MCP_NAME in _user_servers() and _wa_linked() and _wa_call("status", timeout=3) is None:
            _wa_spawn()
    except Exception as exc:
        core.log(f"napojení WhatsApp při startu: {exc}", "warn")
    try:
        github_upgrade()
    except Exception as exc:
        core.log(f"napojení GitHub při startu: {exc}", "warn")
    # sync_shared přehled v CLAUDE.md zapíše samo — když doběhne.
    if not core.on_gateway() or sync_shared() is None:
        write_claude_md()
        return
    # Co mi kdo nově nasdílí (nebo vezme), se v Claude Code objeví samo —
    # bez restartu prostoru a bez otevírání nastavení.
    while True:
        time.sleep(SHARED_SYNC_EVERY)
        try:
            sync_shared(force=False)
        except Exception as exc:
            core.log(f"sdílená napojení: {exc}", "warn")


# ── přehled napojení pro Clauda ──────────────────────────────────────────────
NAPOJENI_MARK = ("<!-- claude-hub:napojeni -->", "<!-- /claude-hub:napojeni -->")


def _napojeni_block():
    servers = sorted(n for n, e in _user_servers().items() if isinstance(e, dict))
    if not servers:
        return ""
    google = [a["name"] for a in _google_accounts()] if GOOGLE_MCP_NAME in servers else []
    default = _google_default() if google else ""
    lines = [NAPOJENI_MARK[0], "## Napojení (MCP)", "",
             "V hubu (Nastavení → Napojení) má tenhle uživatel napojené: "
             + ", ".join(f"`{n}`" for n in servers) + ".",
             "Jejich nástroje se jmenují `mcp__<napojení>__…` a často jsou",
             "odložené — v seznamu nástrojů je jen jméno bez popisu. **Než řekneš,",
             "že něco napojené není, najdi to přes ToolSearch** (třeba `+google`).",
             "Za nenapojené to považuj, až když se nic nenajde; pak pošli uživatele",
             "do Nastavení → Napojení."]
    if google:
        others = [g for g in google if g != default]
        lines += ["",
                  f"**Google:** napojený účet je **{default}**"
                  + (f" (další: {', '.join(others)})" if others else "") + ".",
                  "U nástrojů `mcp__google__*` parametr `user_google_email` vynech — doplní",
                  f"se {default}. Jiný e-mail zadej, jen když uživatel chce jiný",
                  "z napojených účtů. Nikdy ho nehádej podle účtu Claude ani podle",
                  "e-mailů jiných lidí — pro ně přihlášení není a nástroj selže."]
    if WA_MCP_NAME in servers:
        owner = _wa_config().get("owner")
        lines += ["",
                  "**WhatsApp:** `mcp__whatsapp__ask` pošle uživateli otázku na WhatsApp"
                  + (f" (+{owner})" if owner else "") + " a **počká na odpověď**,",
                  "`send_message` pošle zprávu nebo soubor, `read_messages` přečte, co",
                  "uživatel napsal mimo otázky. Použij, když o to uživatel požádá („napiš",
                  "mi“, „zeptej se na WhatsAppu“), nebo když pracuješ sám a potřebuješ",
                  "rozhodnutí a uživatel v terminálu neodpovídá. Piš krátce a srozumitelně",
                  "bez kontextu obrazovky; nespamuj — jedna zpráva na jednu událost."]
    github = [n for n in servers if n.startswith("github-")]
    if github:
        lines += ["",
                  "**GitHub:** napojený jako " + ", ".join(f"`{n}`" for n in github) + ".",
                  "Pod stejným účtem je přihlášené i `gh` a git (push a clone přes https).",
                  "MCP nástroje GitHubu neumí spolupracovníky, tajné údaje a proměnné",
                  "Actions, nastavení repa, ochranu větví, webhooky, Pages, deploy klíče",
                  "ani mazání repa — na to použij rovnou `gh` (`gh secret set`,",
                  "`gh variable set`, `gh repo edit`, `gh repo delete`) nebo `gh api`,",
                  "třeba `gh api -X PUT repos/VLASTNIK/REPO/collaborators/LOGIN -f permission=push`.",
                  "Uživatele **nikdy neposílej na `gh auth login`** — když `gh auth status`",
                  "hlásí chybu, ať se znovu přihlásí v Nastavení → Napojení → GitHub.",
                  "Před nevratnými kroky (mazání repa, změny v organizaci) se zeptej."]
    lines.append(NAPOJENI_MARK[1])
    return "\n".join(lines) + "\n"


def write_claude_md():
    """Přehled napojení do ~/.claude/CLAUDE.md, mezi značkami (jako firemní
    blok brány). Jen jména a e-maily účtů, žádné klíče. Zbytek souboru patří
    uživateli a zůstane; bez napojení se blok smaže."""
    try:
        path = os.path.join(core.CLAUDE_DIR, "CLAUDE.md")
        try:
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
        except FileNotFoundError:
            text = ""
        block = _napojeni_block()
        start, end = text.find(NAPOJENI_MARK[0]), text.find(NAPOJENI_MARK[1])
        if start >= 0 and end > start:
            new = text[:start] + block.rstrip("\n") + text[end + len(NAPOJENI_MARK[1]):]
            if not block:
                new = new.rstrip("\n") + "\n" if new.strip() else ""
        elif block:
            new = (text.rstrip("\n") + "\n\n" if text.strip() else "") + block
        else:
            return
        if new == text:
            return
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(new)
        os.replace(tmp, path)
    except (OSError, UnicodeDecodeError) as exc:
        core.log(f"přehled napojení do CLAUDE.md: {exc}", "warn")
