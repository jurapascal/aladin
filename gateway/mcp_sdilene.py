"""
Sdílená napojení (MCP) — jedno nastavení, klíč drží brána, používá víc lidí.

Kdokoli si v hubu (Nastavení → Napojení → Sdílená napojení) založí napojení
na službu — Ecomail, WordPress, cokoli s MCP — a vybere, s kým ho sdílí.
Klíče, hesla a hlavičky zadá prohlížeč rovnou bráně (/gw/mcp-sdilene), do
prostorů se nikdy nedostanou: ani vlastníkovi, ani těm, s kým sdílí.

Dva druhy:

* **adresa** — vzdálený MCP server (Streamable HTTP). Brána k požadavku
  přidá uložené hlavičky (typicky `Authorization: Bearer …`).
* **účet** — účet ze služby, kterou má vlastník napojenou u sebe v prostoru
  (Nastavení → Propojené služby: Freelo, Canva, Ecomail, Clockify, Google).
  Nic se nekopíruje do registru: brána si adresu, hlavičky a přihlášení čte
  při každém spojení z vlastníkova domova, takže když se vlastník přihlásí
  znovu, platí to hned i pro ostatní. Vypršelé přihlášení OAuth brána obnoví
  sama a nové tokeny zapíše vlastníkovi zpátky (jinak by mu ten starý přestal
  fungovat). Google běží jako příkaz (workspace-mcp) v sandboxu s kopií
  tokenu jen toho jednoho účtu.
* **příkaz** — MCP server spouštěný příkazem (`npx -y balíček`), tajemství
  v proměnných prostředí. Brána ho pustí sama, v bwrap sandboxu, který vidí
  jen systém ke čtení a vlastní složku na cache (HOME) — žádný domov, žádný
  trezor, žádnou databázi. Jeden proces na člověka a jeho sezení Claude Code;
  nečinný se po IDLE ukončí.

V prostoru je za každé napojení stdio most `tools/sdilene_mcp.py <zkratka>`
(zaregistruje ho hub, `hub/connect.py` → sync_shared). Most posílá zprávy
JSON-RPC na loopback brány (`/gw/mcp-sdilene/volani`) se žetonem prostoru —
stejně jako most na počítač. Brána u **každé** zprávy znovu ověří, že člověk
je členem; odebraný tak přístup ztratí hned, ne až po restartu.

Sdílet jde i **podkategorii firmy** (`skupiny`, gateway/firmy.py): napojení
pak smí používat každý, kdo do podkategorie vidí — přístupové údaje k webu
klienta dostane Claude každého, kdo na webu dělá, a nikdo z nich je nevidí.
Kdo o přístup k podkategorii přijde, přijde i o napojení (hned, u další zprávy).

Registr: GATEWAY_DIR/mcp-sdilene/registr.json (0600, složka 0700), podle id
účtů. Záznam o změnách (bez tajemství): mcp-sdilene/zmeny.jsonl.
"""
import contextlib
import fcntl
import http.client
import json
import os
import queue
import re
import secrets
import signal
import ssl
import subprocess
import threading
import time
import unicodedata
import urllib.parse

from . import config, firmy, isolation, safefs, shared

DIR = os.environ.get("HUB_GW_MCP_SHARED_DIR", os.path.join(config.GATEWAY_DIR, "mcp-sdilene"))
REGISTRY = os.path.join(DIR, "registr.json")
LOG = os.path.join(DIR, "zmeny.jsonl")
CACHE = os.path.join(DIR, "cache")
SLUG = re.compile(r"[a-z0-9][a-z0-9-]{0,39}")
ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")
HEADER_NAME = re.compile(r"[A-Za-z0-9-]{1,64}")
# Hlavičky, které brána nastavuje sama — z uložených se nepřeberou.
OWN_HEADERS = {"host", "content-type", "content-length", "accept", "mcp-session-id",
               "connection", "transfer-encoding"}
IDLE = int(os.environ.get("HUB_GW_MCP_SHARED_IDLE", str(15 * 60)))
MAX_PROCS = int(os.environ.get("HUB_GW_MCP_SHARED_PROCS", "24"))
TIMEOUT = 180                      # jedno volání nástroje (třeba export z WordPressu)
MAX_MESSAGE = 8 * 1024 * 1024
UA = "claude-code-hub-brana/1"
_LOCK = threading.Lock()


# ── registr ──────────────────────────────────────────────────────────────────
@contextlib.contextmanager
def _locked():
    with _LOCK:
        os.makedirs(DIR, mode=0o700, exist_ok=True)
        with open(REGISTRY + ".lock", "a") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)


def _load():
    try:
        with open(REGISTRY, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    items = data.get("napojeni") if isinstance(data, dict) else None
    return items if isinstance(items, dict) else {}


def _save(items):
    tmp = REGISTRY + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump({"napojeni": items}, fh, ensure_ascii=False, indent=2, sort_keys=True)
    os.replace(tmp, REGISTRY)


def _log(user, action, slug, **extra):
    entry = {"cas": time.strftime("%Y-%m-%d %H:%M:%S"), "email": (user or {}).get("email", ""),
             "akce": action, "napojeni": slug, **extra}
    try:
        fd = os.open(LOG, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _account(uid):
    return shared.ACCOUNTS.by_id(uid) if shared.ACCOUNTS and uid else None


def _label(uid):
    u = _account(uid)
    return (u.get("name") or u["email"]) if u else ""


def _public(slug, entry, uid):
    """Co z napojení smí vidět hub — bez hodnot tajemství, jen jejich jména."""
    owner = _account(entry.get("owner"))
    out = {"slug": slug, "name": entry.get("name") or slug, "kind": entry.get("kind"),
           "owner": owner["email"] if owner else "", "owner_name": _label(entry.get("owner")),
           "is_owner": entry.get("owner") == uid, "created": entry.get("created", ""),
           "mcp_name": "sdilene-" + slug}
    if entry.get("kind") == "ucet":
        out["service"] = entry.get("service", "")
        out["account"] = entry.get("account", "")
        out["label"] = entry.get("label", "")
    if entry.get("owner") == uid:
        out["members"] = [{"email": u["email"], "name": u.get("name", "")}
                          for u in (_account(m) for m in entry.get("members", []))
                          if u and u["id"] != uid]
        out["skupiny"] = [{"id": g, "nazev": firmy.skupina_nazev(g)}
                          for g in entry.get("skupiny") or []]
        if entry.get("kind") == "adresa":
            out["url"] = entry.get("url", "")
            out["secrets"] = sorted(entry.get("headers") or {})
        elif entry.get("kind") == "prikaz":
            out["command"] = " ".join(entry.get("command") or [])
            out["secrets"] = sorted(entry.get("env") or {})
    return out


def _is_member(entry, user, data=None):
    """Smí `user` napojení používat — jmenovitě, nebo přes podkategorii firmy?"""
    if int(user["id"]) in entry.get("members", []):
        return True
    return any(firmy.ve_skupine(user, g, data) for g in entry.get("skupiny") or [])


def for_user(user):
    """Napojení, která `user` smí používat (svoje i nasdílená)."""
    uid = int(user["id"])
    data = firmy._load()
    return [_public(slug, e, uid) for slug, e in sorted(_load().items())
            if _is_member(e, user, data)]


def all_items():
    return [dict(_public(slug, e, None), members=[_label(m) for m in e.get("members", [])]
                 + [firmy.skupina_nazev(g) for g in e.get("skupiny") or []])
            for slug, e in sorted(_load().items())]


def member(user, slug):
    """Záznam napojení, když je `user` členem — jinak None."""
    entry = _load().get(str(slug or ""))
    if entry and _is_member(entry, user):
        return entry
    return None


def _skupiny(user, raw):
    """Podkategorie z formuláře — jen ty, do kterých `user` smí sdílet
    (firmy.skupiny_pro). None = ve formuláři nejsou, nic neměnit."""
    if raw is None:
        return None
    if isinstance(raw, str):
        raw = [g for g in raw.split(",") if g.strip()]
    if not isinstance(raw, list):
        raise ValueError("Seznam podkategorií je poškozený.")
    allowed = {g["id"] for g in firmy.skupiny_pro(user)}
    out = []
    for g in raw:
        g = str(g or "").strip()
        if g not in allowed:
            raise ValueError("Do téhle podkategorie sdílet nemůžeš (nevidíš ji, nebo jsi ve firmě host).")
        if g not in out:
            out.append(g)
    return out


# ── založení a správa ────────────────────────────────────────────────────────
def _resolve(emails):
    if isinstance(emails, str):
        emails = re.split(r"[,\s]+", emails)
    ids, unknown = [], []
    for email in emails or []:
        email = str(email or "").strip().lower()
        if not email:
            continue
        u = shared.ACCOUNTS.get(email) if shared.ACCOUNTS else None
        if not u or u.get("disabled"):
            unknown.append(email)
        elif u["id"] not in ids:
            ids.append(u["id"])
    return ids, unknown


def _slugify(name, taken):
    base = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    base = re.sub(r"[^a-z0-9]+", "-", base).strip("-")[:30] or "napojeni"
    slug, n = base, 2
    while slug in taken:
        slug, n = f"{base}-{n}", n + 1
    return slug


def _pairs(raw, name_re, what):
    """{jméno: hodnota} z formuláře — jména podle `name_re`, hodnoty text."""
    if not raw:
        return {}
    if not isinstance(raw, dict):
        raise ValueError(f"{what} jsou poškozené.")
    out = {}
    for key, value in raw.items():
        key = str(key or "").strip()
        if not key:
            continue
        if not name_re.fullmatch(key):
            raise ValueError(f"{what}: neplatné jméno „{key[:40]}“.")
        value = str(value if value is not None else "")
        if len(value) > 8000 or "\n" in value or "\r" in value:
            raise ValueError(f"{what}: hodnota u „{key}“ je moc dlouhá nebo víceřádková.")
        out[key] = value
    if len(out) > 30:
        raise ValueError(f"{what}: nejvýš 30 položek.")
    return out


def _spec(form):
    """Druh a nastavení napojení z formuláře. Vrací dict do registru."""
    kind = str(form.get("kind") or "")
    if kind == "adresa":
        url = str(form.get("url") or "").strip()
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme != "https" or not parsed.hostname or len(url) > 500:
            raise ValueError("Adresa MCP serveru musí začínat https://.")
        headers = _pairs(form.get("headers"), HEADER_NAME, "Hlavičky")
        if any(h.lower() in OWN_HEADERS for h in headers):
            raise ValueError("Tyhle hlavičky nastavuje brána sama: " + ", ".join(
                h for h in headers if h.lower() in OWN_HEADERS))
        return {"kind": kind, "url": url, "headers": headers}
    if kind == "prikaz":
        command = form.get("command")
        if isinstance(command, str):
            import shlex
            try:
                command = shlex.split(command)
            except ValueError:
                raise ValueError("Příkaz nejde rozložit — zkontroluj uvozovky.") from None
        if (not isinstance(command, list) or not command or len(command) > 40
                or not all(isinstance(c, str) and c and len(c) < 2000 for c in command)):
            raise ValueError("Chybí příkaz, kterým se MCP server spouští (třeba npx -y balíček).")
        return {"kind": kind, "command": command,
                "env": _pairs(form.get("env"), ENV_NAME, "Proměnné")}
    raise ValueError("Druh napojení je adresa, nebo příkaz.")


def create(user, form):
    uid = int(user["id"])
    name = " ".join(str(form.get("name") or "").split())[:60]
    if not name:
        raise ValueError("Napojení potřebuje název.")
    spec = _spec(form)
    ids, unknown = _resolve(form.get("emaily"))
    if unknown:
        raise ValueError("Tihle lidé tu účet nemají: " + ", ".join(unknown))
    with _locked():
        items = _load()
        if any((e.get("name") or "").lower() == name.lower() and e.get("owner") == uid
               for e in items.values()):
            raise ValueError(f"Napojení „{name}“ už máš.")
        slug = _slugify(name, items)
        items[slug] = {"name": name, "owner": uid, "members": [uid] + [i for i in ids if i != uid],
                       "created": time.strftime("%Y-%m-%d %H:%M:%S"), **spec}
        groups = _skupiny(user, form.get("skupiny"))
        if groups:
            items[slug]["skupiny"] = groups
        _save(items)
    _log(user, "zalozit", slug, druh=spec["kind"], clenove=[_label(i) for i in ids])
    return {"ok": True, "slug": slug}


def _own(items, user, slug):
    slug = str(slug or "")
    if not SLUG.fullmatch(slug) or slug not in items:
        raise ValueError("Takové napojení není.")
    entry = items[slug]
    if entry.get("owner") != int(user["id"]) and user.get("role") != "admin":
        raise PermissionError("Napojení spravuje jen ten, kdo ho založil.")
    return slug, entry


def update(user, form):
    """Změna sdílení, případně i nastavení (nové tajemství přepíše staré;
    prázdná hodnota u existujícího jména = nechat původní)."""
    with _locked():
        items = _load()
        slug, entry = _own(items, user, form.get("slug"))
        removed = []
        if "emaily" in form:
            ids, unknown = _resolve(form.get("emaily"))
            if unknown:
                raise ValueError("Tihle lidé tu účet nemají: " + ", ".join(unknown))
            owner = entry.get("owner")
            new = ([owner] if owner else []) + [i for i in ids if i != owner]
            removed = [m for m in entry.get("members", []) if m not in new]
            entry["members"] = new
        groups = _skupiny(user, form.get("skupiny"))
        if groups is not None:
            entry["skupiny"] = groups
        if form.get("kind"):
            spec = _spec(form)
            key = "headers" if spec["kind"] == "adresa" else "env"
            if spec["kind"] == entry.get("kind"):
                old = entry.get(key) or {}
                spec[key] = {k: (v if v else old.get(k, "")) for k, v in spec[key].items()}
            entry.update({k: v for k, v in spec.items()})
            for drop in ({"url", "headers"} if spec["kind"] == "prikaz" else {"command", "env"}):
                entry.pop(drop, None)
        name = " ".join(str(form.get("name") or "").split())[:60]
        if name:
            entry["name"] = name
        _save(items)
    _log(user, "upravit", slug, clenove=[_label(m) for m in entry.get("members", [])])
    # Odebraní přijdou o přístup hned (ověřuje se u každé zprávy); změněné
    # nastavení chce nové procesy.
    POOL.stop(slug, removed if not form.get("kind") else None)
    return {"ok": True, "slug": slug}


def delete(user, slug):
    with _locked():
        items = _load()
        slug, entry = _own(items, user, slug)
        del items[slug]
        _save(items)
    _log(user, "smazat", slug)
    POOL.stop(slug)
    return {"ok": True, "slug": slug}


# ── účty ze služeb vlastníka (druh „ucet") ──────────────────────────────────
SERVICES = {"freelo": "Freelo", "canva": "Canva", "ecomail": "Ecomail",
            "clockify": "Clockify", "google": "Google", "facebook": "Facebook a Instagram",
            "reklamy": "Meta reklamy", "vlastni": "Vlastní napojení"}
# Služby spouštěné příkazem s tokenem v proměnné (hub/connect.py, druh „token").
COMMAND_SERVICES = {"facebook": ("@oliverames/meta-mcp-server", "META_ACCESS_TOKEN"),
                    "ecomail": ("ecomail-mcp", "ECOMAIL_API_KEY")}
# Obal z tools/, přes který služba běží (hub/connect.py, SERVICES[…]["wrapper"]).
COMMAND_WRAPPERS = {"ecomail": "ecomail_mcp.py"}
ACCOUNT_NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,60}")
GOOGLE_TOOLS = ["gmail", "drive", "calendar", "docs", "sheets", "slides",
                "forms", "tasks", "contacts"]


def _home(uid):
    from . import workspace          # workspace importuje shared → až tady
    u = _account(uid)
    return workspace.home_for(u) if u else ""


def _google_file(email):
    import urllib.parse as up
    return ".google_workspace_mcp/credentials/" + up.quote(email, safe="@._-") + ".json"


def _owner_server(entry):
    """(adresa, hlavičky) účtu z vlastníkova ~/.claude.json — nebo ValueError."""
    home = _home(entry.get("owner"))
    raw = safefs.read_text(home, ".claude.json", 16 * 1024 * 1024) if home else None
    try:
        servers = (json.loads(raw or "{}").get("mcpServers") or {})
    except ValueError:
        servers = {}
    spec = servers.get(entry.get("account"))
    if not isinstance(spec, dict) or not spec.get("url"):
        raise RuntimeError("Vlastník tenhle účet u sebe už nemá — sdílení je potřeba "
                           "nastavit znovu.")
    url = str(spec["url"])
    if urllib.parse.urlparse(url).scheme != "https":
        raise RuntimeError("Sdílet jde jen napojení na https adresu.")
    headers = {str(k): str(v) for k, v in (spec.get("headers") or {}).items()
               if HEADER_NAME.fullmatch(str(k)) and str(k).lower() not in OWN_HEADERS}
    return url, headers


def _owner_command(entry):
    """Účet spouštěný příkazem (npx balíček + token) jako záznam „prikaz".
    Příkaz se nebere od vlastníka — ten by si tam mohl napsat cokoli a běželo
    by to na bráně —, jen token z jeho proměnné."""
    home = _home(entry.get("owner"))
    raw = safefs.read_text(home, ".claude.json", 16 * 1024 * 1024) if home else None
    try:
        servers = (json.loads(raw or "{}").get("mcpServers") or {})
    except ValueError:
        servers = {}
    spec = servers.get(entry.get("account"))
    package, var = COMMAND_SERVICES[entry["service"]]
    if not isinstance(spec, dict) or \
            not any(package in str(a) for a in spec.get("args") or []):
        raise RuntimeError("Vlastník tenhle účet u sebe už nemá — sdílení je potřeba "
                           "nastavit znovu.")
    token = str((spec.get("env") or {}).get(var) or "")
    if not token:
        raise RuntimeError("U účtu vlastníka chybí token.")
    # Jen balíček, nanejvýš s číslem verze — nic, co by npx vzal odjinud.
    verze = [str(a) for a in spec.get("args") or []
             if re.fullmatch(re.escape(package) + r"(@\d+\.\d+\.\d+)?", str(a))]
    command = ["npx", "-y", verze[0] if verze else package]
    wrapper = COMMAND_WRAPPERS.get(entry["service"])
    if wrapper:
        from . import workspace
        script = os.path.realpath(os.path.join(workspace.REPO_DIR, "tools", wrapper))
        if os.path.isfile(script):
            # Skript z repa hubu, ne od vlastníka; do sandboxu jen ke čtení.
            return {"kind": "prikaz", "command": ["python3", script, *command],
                    "env": {var: token}, "ro": [script]}
    return {"kind": "prikaz", "command": command, "env": {var: token}}


def _check_account(owner_uid, service, account):
    """Má vlastník tenhle účet opravdu u sebe? Jinak ValueError."""
    if service not in SERVICES:
        raise ValueError("Tuhle službu sdílet neumím.")
    home = _home(owner_uid)
    if service == "google":
        if "@" not in account or len(account) > 200 or \
                not safefs.is_file(home, _google_file(account)):
            raise ValueError("Tenhle Google účet u sebe nemáš.")
        return
    # Vlastní napojení se jmenuje podle sebe (`notion`), ne `vlastni-…`.
    if service == "vlastni":
        if not ACCOUNT_NAME.fullmatch(account) or account.startswith("sdilene-"):
            raise ValueError("Takové napojení není.")
    elif not ACCOUNT_NAME.fullmatch(account) or \
            (account != service and not account.startswith(service + "-")):
        raise ValueError("Takový účet u služby není.")
    try:
        if service in COMMAND_SERVICES:
            _owner_command({"owner": owner_uid, "account": account, "service": service})
        else:
            _owner_server({"owner": owner_uid, "account": account})
    except RuntimeError as exc:
        raise ValueError(str(exc)) from None


def share_account(user, form):
    """Nastaví, s kým vlastník sdílí svůj účet ze služby. Prázdný seznam =
    sdílení zrušit. Záznam je jeden na (vlastník, služba, účet)."""
    uid = int(user["id"])
    service = str(form.get("service") or "")
    account = str(form.get("account") or "").strip()
    label = " ".join(str(form.get("label") or "").split())[:40]
    _check_account(uid, service, account)
    ids, unknown = _resolve(form.get("emaily"))
    if unknown:
        raise ValueError("Tihle lidé tu účet nemají: " + ", ".join(unknown))
    ids = [i for i in ids if i != uid]
    groups = _skupiny(user, form.get("skupiny"))
    with _locked():
        items = _load()
        slug = next((s for s, e in items.items() if e.get("kind") == "ucet"
                     and e.get("owner") == uid and e.get("service") == service
                     and e.get("account") == account), None)
        if groups is None:
            groups = list((items.get(slug) or {}).get("skupiny") or [])
        removed = []
        if not ids and not groups:
            if slug:
                removed = items.pop(slug).get("members", [])
                _save(items)
        elif slug:
            entry = items[slug]
            removed = [m for m in entry.get("members", []) if m != uid and m not in ids]
            entry["members"] = [uid] + ids
            entry["skupiny"] = groups
            _save(items)
        else:
            owner = _label(uid).split("@")[0].split(" ")[0]
            name = (f"{label or account} ({owner})" if service == "vlastni" else
                    f"{SERVICES[service]} {label or account} ({owner})")[:60]
            slug = _slugify(f"{service} {label or account.split('@')[0]} {owner}", items)
            items[slug] = {"name": name, "owner": uid, "members": [uid] + ids,
                           "created": time.strftime("%Y-%m-%d %H:%M:%S"), "kind": "ucet",
                           "service": service, "account": account, "label": label,
                           "skupiny": groups}
            _save(items)
    if slug:
        _log(user, "sdilet-ucet", slug, clenove=[_label(i) for i in ids],
             podkategorie=[firmy.skupina_nazev(g) for g in groups])
        POOL.stop(slug, None if not (ids or groups) else removed)
    return {"ok": True, "slug": slug if ids or groups else ""}


def forget_user(uid):
    """Smazaný účet: pryč ze sdílení; jeho napojení zmizí celá (klíče byly jeho)."""
    if not os.path.exists(REGISTRY):
        return
    with _locked():
        items = _load()
        gone = [s for s, e in items.items() if e.get("owner") == uid]
        for s in gone:
            del items[s]
        for e in items.values():
            e["members"] = [m for m in e.get("members", []) if m != uid]
        _save(items)
    for s in gone:
        POOL.stop(s)


# ── předávání zpráv ──────────────────────────────────────────────────────────
def _key(mid):
    return json.dumps(mid, sort_keys=True)


def _is_request(msg):
    return isinstance(msg, dict) and "method" in msg and msg.get("id") is not None


class _Remote:
    """Vzdálený MCP server (Streamable HTTP) za jedno sezení mostu."""

    def __init__(self, entry):
        self.url = urllib.parse.urlparse(entry["url"])
        self.headers = dict(entry.get("headers") or {})
        self.session = ""
        self.used = time.time()

    def send(self, msg):
        self.used = time.time()
        body = json.dumps(msg).encode("utf-8")
        headers = self._headers()
        headers.update({"Content-Type": "application/json",
                        "Accept": "application/json, text/event-stream"})
        # Bez User-Agent některé služby (Ecomail za firewallem) odpoví 403.
        headers.setdefault("User-Agent", UA)
        if self.session:
            headers["Mcp-Session-Id"] = self.session
        path = self.url.path or "/"
        if self.url.query:
            path += "?" + self.url.query
        conn = http.client.HTTPSConnection(self.url.hostname, self.url.port or 443,
                                           timeout=TIMEOUT, context=ssl.create_default_context())
        try:
            conn.request("POST", path, body=body, headers=headers)
            resp = conn.getresponse()
            if msg.get("method") == "initialize" and resp.getheader("Mcp-Session-Id"):
                self.session = resp.getheader("Mcp-Session-Id")
            if resp.status == 202 or not _is_request(msg):
                resp.read(64 * 1024)
                return []
            if resp.status == 404 and self.session:
                self.session = ""
                raise RuntimeError("Služba sezení zapomněla — Claude se připojí znovu.")
            if resp.status in (401, 403):
                raise RuntimeError(f"Služba odmítla přihlášení ({resp.status}) — "
                                   "vlastník napojení má zkontrolovat klíč.")
            if resp.status >= 400:
                raise RuntimeError(f"Služba odpověděla {resp.status}.")
            ctype = (resp.getheader("Content-Type") or "").lower()
            if "text/event-stream" in ctype:
                return self._events(resp, msg.get("id"))
            data = json.loads(resp.read(MAX_MESSAGE).decode("utf-8") or "null")
            return data if isinstance(data, list) else [data] if data else []
        finally:
            conn.close()

    def _headers(self):
        return dict(self.headers)

    @staticmethod
    def _events(resp, want):
        """Zprávy ze streamu SSE až po odpověď na náš požadavek."""
        out, data = [], []
        while True:
            line = resp.readline(MAX_MESSAGE)
            if not line:
                break
            line = line.decode("utf-8", "replace").rstrip("\r\n")
            if line.startswith("data:"):
                data.append(line[5:].lstrip())
                continue
            if line or not data:
                continue
            try:
                msg = json.loads("\n".join(data))
            except ValueError:
                msg = None
            data = []
            if isinstance(msg, dict):
                out.append(msg)
                if "method" not in msg and _key(msg.get("id")) == _key(want):
                    break
        return out

    def close(self):
        pass


_TOKEN_LOCK = threading.Lock()


class _Account(_Remote):
    """Účet ze služby vlastníka (druh „ucet"): adresa a hlavičky z jeho
    ~/.claude.json, přihlášení OAuth z jeho ~/.claude/.credentials.json.
    Čte se znovu u každé zprávy — nové přihlášení vlastníka platí hned."""

    def __init__(self, entry):
        self.entry = entry
        url, headers = _owner_server(entry)
        super().__init__({"url": url, "headers": headers})
        self.server_url = url

    def _headers(self):
        url, headers = _owner_server(self.entry)
        if url != self.server_url:
            raise RuntimeError("Vlastník účet mezitím změnil — Claude se připojí znovu.")
        if not any(k.lower() == "authorization" for k in headers):
            # Klíč v hlavičce (Clockify) přihlášení OAuth nepotřebuje, vlastní
            # napojení bez přihlášení taky ne.
            token = _oauth_token(self.entry, url, required=not headers
                                 and self.entry.get("service") != "vlastni")
            if token:
                headers["Authorization"] = "Bearer " + token
        return headers


def _oauth_entry(creds, name, url):
    for key, item in (creds.get("mcpOAuth") or {}).items():
        if isinstance(item, dict) and item.get("serverName") == name and \
                item.get("serverUrl") == url and item.get("accessToken"):
            return key, item
    return None, None


def _oauth_token(entry, url, required=True):
    """Platný přístupový token vlastníka pro tenhle účet. Vypršelý obnoví
    a nové tokeny zapíše vlastníkovi zpátky — bez toho by mu obnovovací token,
    který služba po použití zneplatní, přestal fungovat."""
    home = _home(entry.get("owner"))
    rel = ".claude/.credentials.json"
    with _TOKEN_LOCK:
        try:
            creds = json.loads(safefs.read_text(home, rel) or "{}")
        except ValueError:
            creds = {}
        key, item = _oauth_entry(creds, entry.get("account"), url)
        if not item and not required:
            return ""
        if not item:
            raise RuntimeError("Vlastník u tohohle účtu není přihlášený — má se v "
                               "Propojených službách přihlásit znovu.")
        expires = item.get("expiresAt") or 0
        if not expires or expires / 1000 > time.time() + 90:
            return item["accessToken"]
        if not item.get("refreshToken") or not item.get("clientId"):
            raise RuntimeError("Přihlášení vlastníka vypršelo — má se v Propojených "
                               "službách přihlásit znovu.")
        fresh = _refresh(item, url)
        # Mezitím mohl zapisovat Claude Code vlastníka — načíst znovu a změnit
        # jen tenhle záznam, a jen když je pořád ten, který jsme obnovovali.
        try:
            creds = json.loads(safefs.read_text(home, rel) or "{}")
        except ValueError:
            creds = {}
        cur = (creds.get("mcpOAuth") or {}).get(key)
        if isinstance(cur, dict) and cur.get("refreshToken") == item["refreshToken"]:
            cur["accessToken"] = fresh["access_token"]
            if fresh.get("refresh_token"):
                cur["refreshToken"] = fresh["refresh_token"]
            cur["expiresAt"] = int((time.time() + int(fresh.get("expires_in") or 3600)) * 1000)
            safefs.write_text(home, rel, json.dumps(creds, indent=2), mode=0o600, heal=False)
        return fresh["access_token"]


def _http_json(url, data=None):
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise RuntimeError("Přihlašovací server služby není na https.")
    path = (parsed.path or "/") + ("?" + parsed.query if parsed.query else "")
    conn = http.client.HTTPSConnection(parsed.hostname, parsed.port or 443, timeout=20,
                                       context=ssl.create_default_context())
    try:
        if data is None:
            conn.request("GET", path, headers={"Accept": "application/json", "User-Agent": UA})
        else:
            conn.request("POST", path, body=urllib.parse.urlencode(data).encode(),
                         headers={"Content-Type": "application/x-www-form-urlencoded",
                                  "Accept": "application/json", "User-Agent": UA})
        resp = conn.getresponse()
        body = resp.read(1024 * 1024)
        try:
            out = json.loads(body.decode("utf-8") or "null")
        except ValueError:
            out = None
        return resp.status, out
    finally:
        conn.close()


def _token_endpoint(item, url):
    base = ((item.get("discoveryState") or {}).get("authorizationServerUrl") or "").rstrip("/")
    if not base:
        # Kde služba přihlašuje, prozradí metadata chráněného zdroje (RFC 9728).
        p = urllib.parse.urlparse(url)
        base = f"{p.scheme}://{p.netloc}"
        for meta in (f"{base}/.well-known/oauth-protected-resource{p.path}",
                     f"{base}/.well-known/oauth-protected-resource"):
            try:
                status, data = _http_json(meta)
            except (OSError, http.client.HTTPException):
                continue
            servers = (data or {}).get("authorization_servers") if status == 200 else None
            if servers:
                base = str(servers[0]).rstrip("/")
                break
    p = urllib.parse.urlparse(base)
    root, sub = f"{p.scheme}://{p.netloc}", p.path.strip("/")
    tries = [f"{root}/.well-known/oauth-authorization-server" + (f"/{sub}" if sub else ""),
             f"{root}/.well-known/openid-configuration" + (f"/{sub}" if sub else ""),
             f"{base}/.well-known/oauth-authorization-server",
             f"{base}/.well-known/openid-configuration"]
    for meta in dict.fromkeys(tries):
        try:
            status, data = _http_json(meta)
        except (OSError, http.client.HTTPException):
            continue
        if status == 200 and isinstance(data, dict) and data.get("token_endpoint"):
            return data["token_endpoint"]
    raise RuntimeError("Nepovedlo se obnovit přihlášení vlastníka (služba nemá token "
                       "endpoint) — má se přihlásit znovu.")


def _refresh(item, url):
    form = {"grant_type": "refresh_token", "refresh_token": item["refreshToken"],
            "client_id": item["clientId"], "resource": url}
    if item.get("clientSecret"):
        form["client_secret"] = item["clientSecret"]
    endpoint = _token_endpoint(item, url)
    try:
        status, data = _http_json(endpoint, form)
        if status == 400 and isinstance(data, dict) and "resource" in str(data):
            form.pop("resource")
            status, data = _http_json(endpoint, form)
    except (OSError, http.client.HTTPException) as exc:
        raise RuntimeError(f"Přihlašovací server služby není k dosažení: {exc}") from None
    if status != 200 or not isinstance(data, dict) or not data.get("access_token"):
        raise RuntimeError("Přihlášení vlastníka vypršelo a nejde obnovit — má se v "
                           "Propojených službách přihlásit znovu.")
    return data


def _google_entry(entry):
    """Google účet vlastníka jako příkaz: workspace-mcp s kopií tokenu jen toho
    jednoho účtu ve vlastní složce napojení (ne ve vlastníkově domově)."""
    from . import workspace
    cid, secret = workspace.google_client()
    if not cid or not secret:
        raise RuntimeError("Google na tomhle serveru není nastavený.")
    email = entry["account"]
    data = safefs.read_bytes(_home(entry.get("owner")), _google_file(email), 256 * 1024)
    if not data:
        raise RuntimeError("Vlastník tenhle Google účet u sebe už nemá.")
    return {"kind": "prikaz",
            "command": ["uvx", "workspace-mcp", "--tool-tier", "extended",
                        "--tools", *GOOGLE_TOOLS],
            "env": {"GOOGLE_OAUTH_CLIENT_ID": cid, "GOOGLE_OAUTH_CLIENT_SECRET": secret,
                    "USER_GOOGLE_EMAIL": email,
                    "WORKSPACE_MCP_CREDENTIALS_DIR": "{home}/.google_workspace_mcp/credentials"},
            "files": {_google_file(email): data}}


class _Process:
    """MCP server spuštěný příkazem, v sandboxu, za jedno sezení mostu."""

    def __init__(self, slug, entry):
        home = os.path.join(CACHE, slug)
        os.makedirs(home, mode=0o700, exist_ok=True)
        argv = list(entry["command"])
        if config.ISOLATION == "bwrap":
            from . import workspace
            tools = os.path.realpath(os.path.join(workspace.REPO_DIR, "tools")) + os.sep
            # Jen obaly z tools/ hubu (COMMAND_WRAPPERS) — nic jiného z disku brány.
            ro = tuple(p for p in entry.get("ro") or ()
                       if os.path.realpath(p).startswith(tools))
            argv = isolation._bwrap(argv, home, extra_ro=ro)
            # Proces patří bráně: když brána skončí, skončí i on (a s ním celý
            # jmenný prostor PID) — žádná scope, kterou by musel někdo uklízet.
            argv.insert(argv.index("--"), "--die-with-parent")
        for rel, data in (entry.get("files") or {}).items():
            safefs.write_bytes(home, rel, data, mode=0o600)
        env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": home, "LANG": "C.UTF-8",
               **{k: str(v).replace("{home}", home) for k, v in (entry.get("env") or {}).items()}}
        self.proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, env=env, cwd=home,
                                     start_new_session=True)
        self.used = time.time()
        self.waiting = {}              # id požadavku → Queue
        self.extra = []                # zprávy, na které nikdo nečeká (notifikace, dotazy serveru)
        self.lock = threading.Lock()
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        for raw in self.proc.stdout:
            try:
                msg = json.loads(raw.decode("utf-8"))
            except ValueError:
                continue                 # server píše na stdout i něco jiného
            if not isinstance(msg, dict):
                continue
            with self.lock:
                q = self.waiting.pop(_key(msg.get("id")), None) if "method" not in msg else None
                if q is None:
                    self.extra.append(msg)
                    del self.extra[:-50]
            if q:
                q.put(msg)
        with self.lock:
            for q in self.waiting.values():
                q.put(None)
            self.waiting.clear()

    def alive(self):
        return self.proc.poll() is None

    def send(self, msg):
        self.used = time.time()
        q = None
        if _is_request(msg):
            q = queue.Queue(1)
            with self.lock:
                self.waiting[_key(msg["id"])] = q
        try:
            self.proc.stdin.write(json.dumps(msg).encode("utf-8") + b"\n")
            self.proc.stdin.flush()
        except OSError:
            raise RuntimeError("MCP server napojení neběží (spadl při startu?).") from None
        if q is None:
            return self._take_extra()
        try:
            reply = q.get(timeout=TIMEOUT)
        except queue.Empty:
            raise RuntimeError("MCP server napojení neodpověděl včas.") from None
        if reply is None:
            raise RuntimeError("MCP server napojení skončil — vlastník má zkontrolovat "
                               "příkaz a proměnné.")
        return self._take_extra() + [reply]

    def _take_extra(self):
        with self.lock:
            out, self.extra = self.extra, []
        return out

    def close(self):
        try:
            os.killpg(self.proc.pid, signal.SIGKILL)
        except OSError:
            pass


class Pool:
    """Běžící spojení: (zkratka, id účtu, sezení mostu) → _Remote / _Process."""

    def __init__(self):
        self.items = {}
        self.lock = threading.Lock()
        threading.Thread(target=self._reaper, daemon=True).start()

    def get(self, slug, uid, relace, entry):
        key = (slug, uid, relace)
        with self.lock:
            item = self.items.get(key)
            if item and (not isinstance(item, _Process) or item.alive()):
                return item
            if item:
                item.close()
            if entry["kind"] == "ucet" and entry.get("service") == "google":
                entry = _google_entry(entry)
            elif entry["kind"] == "ucet" and entry.get("service") in COMMAND_SERVICES:
                entry = _owner_command(entry)
            if entry["kind"] == "prikaz":
                procs = [k for k, v in self.items.items() if isinstance(v, _Process)]
                if len(procs) >= MAX_PROCS:
                    oldest = min(procs, key=lambda k: self.items[k].used)
                    self.items.pop(oldest).close()
                item = _Process(slug, entry)
            elif entry["kind"] == "ucet":
                item = _Account(entry)
            else:
                item = _Remote(entry)
            self.items[key] = item
            return item

    def stop(self, slug, uids=None):
        """Ukončí spojení napojení — všech, nebo jen účtů `uids`."""
        with self.lock:
            keys = [k for k in self.items if k[0] == slug and (uids is None or k[1] in uids)]
            gone = [self.items.pop(k) for k in keys]
        for item in gone:
            item.close()

    def stop_one(self, slug, uid, relace):
        with self.lock:
            item = self.items.pop((slug, uid, relace), None)
        if item:
            item.close()

    def _reaper(self):
        while True:
            time.sleep(60)
            now = time.time()
            with self.lock:
                keys = [k for k, v in self.items.items() if now - v.used > IDLE]
                gone = [self.items.pop(k) for k in keys]
            for item in gone:
                item.close()


POOL = Pool()


def call(user, slug, relace, msg):
    """Jedna zpráva z mostu v prostoru → zprávy pro Claude Code."""
    relace = str(relace or "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,64}", relace):
        raise ValueError("Neplatné sezení mostu.")
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
        raise ValueError("Zpráva není JSON-RPC.")
    entry = member(user, slug)
    if not entry:
        POOL.stop(str(slug or ""), [int(user["id"])])
        raise PermissionError("Tohle napojení s tebou už nikdo nesdílí.")
    item = POOL.get(slug, int(user["id"]), relace, entry)
    try:
        return item.send(msg)
    except (OSError, http.client.HTTPException, ValueError) as exc:
        raise RuntimeError(f"Služba není k dosažení: {exc}") from None


def test(user, slug):
    """Zkusí napojení (initialize + tools/list) za `user`. Vrací počet nástrojů."""
    relace = "test-" + secrets.token_hex(8)
    try:
        call(user, slug, relace, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "claude-code-hub", "version": "1"}}})
        call(user, slug, relace, {"jsonrpc": "2.0", "method": "notifications/initialized"})
        out = call(user, slug, relace, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    finally:
        POOL.stop_one(slug, int(user["id"]), relace)
    reply = next((m for m in out if m.get("id") == 2), None) or {}
    if reply.get("error"):
        raise RuntimeError(str(reply["error"].get("message") or reply["error"])[:300])
    return len((reply.get("result") or {}).get("tools") or [])
