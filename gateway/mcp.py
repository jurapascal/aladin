"""
Napojení z appky Claude — MCP server brány s přihlášením přes OAuth.

Každý si v appce Claude (claude.ai, desktop, mobil, Claude Code) přidá
vlastní konektor s adresou `https://<brána>/mcp`. Appka ho při prvním
použití pošle sem na přihlášení: e-mail, heslo, kód z aplikace a souhlas.
Pak Claude v appce vidí a dělá jen to, co ten člověk ve svém prostoru.

Jak je to zabezpečené:

* **Přihlášení je stejně přísné jako do hubu**: heslo + kód z aplikace
  (povinné 2FA), stejné zámky proti hádání (`login_fails`), a pokaždé znovu —
  přihlášení z prohlížeče (cookie) se nepřebírá, souhlas nejde vylákat
  jedním kliknutím z cizí stránky. Stránky nejdou vložit do rámu.
* **OAuth 2.1 bez tajemství klienta, jen s PKCE (S256)**. Klient se
  registruje sám (RFC 7591), ale adresa pro návrat musí být appka Claude
  (claude.ai / claude.com) nebo smyčka na vlastním počítači (Claude Code) —
  kód tedy nikdy neodejde na cizí web. Kód platí minutu a jednou.
* **Tokeny**: přístupový hodinu, obnovovací měsíc a při každém použití se
  vymění; znovu použitý obnovovací token zruší celé přihlášení (krádež).
  Token je svázaný s adresou `/mcp` této brány (RFC 8707). V databázi jen
  otisky. Změna hesla, blokace účtu nebo reset 2FA napojení zruší.
* **Jen vlastní prostor**: každý nástroj běží v instanci hubu toho účtu,
  v jeho sandboxu (bwrap) — cizí domov do něj vůbec není přivázaný. Firemní
  Obsidian podle práva účtu (none / read / write) kontroluje brána při každém
  volání; zapisuje do něj i do sdílených Obsidianů brána sama.
* **Záznam**: každé volání nástroje jde do `mcp.jsonl` (kdo, čím, co —
  bez obsahu souborů a poznámek). Napojení vypíše a zruší
  `claude-hub-admin mcp`.
"""
import base64
import hashlib
import hmac
import html
import http.client
import json
import os
import re
import secrets
import threading
import time
import urllib.parse

from hub import __version__

from . import config, firmy, shared, workspace

PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")
MAX_BODY = 4 * 1024 * 1024
LOG = os.path.join(config.GATEWAY_DIR, "mcp.jsonl")

# Kam smí přihlášení vrátit kód. Appka Claude (web, desktop i mobil jdou přes
# claude.ai) a Claude Code na vlastním počítači (smyčka, RFC 8252). Další
# adresy jen výslovně: HUB_GW_MCP_REDIRECTS="https://…,https://…".
REDIRECTS = {"https://claude.ai/api/mcp/auth_callback",
             "https://claude.com/api/mcp/auth_callback"}
REDIRECTS |= {u.strip() for u in os.environ.get("HUB_GW_MCP_REDIRECTS", "").split(",")
              if u.strip().startswith("https://")}
LOOPBACK = re.compile(r"http://(localhost|127\.0\.0\.1|\[::1\]):\d{1,5}(/[^\s#]*)?")
# Odkud smí přijít požadavek s hlavičkou Origin (prohlížečový klient).
ORIGINS = {"https://claude.ai", "https://claude.com"}

# Rozpracované přihlášení (od /oauth/authorize po souhlas) — jen v paměti.
REQUEST_TTL = 10 * 60
_requests = {}
_req_lock = threading.Lock()
# Registrace klientů: kolik z jedné adresy za hodinu.
REGISTER_LIMIT = 30
_registers = {}


def _log(user, client, tool, ok, **extra):
    entry = {"cas": time.strftime("%Y-%m-%d %H:%M:%S"),
             "email": (user or {}).get("email", ""), "klient": client or "",
             "nastroj": tool, "ok": bool(ok)}
    entry.update({k: v for k, v in extra.items() if v not in (None, "")})
    try:
        fd = os.open(LOG, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


def redirect_ok(uri):
    return isinstance(uri, str) and len(uri) < 500 and (
        uri in REDIRECTS or bool(LOOPBACK.fullmatch(uri)))


# ── adresy ───────────────────────────────────────────────────────────────────
def base_url(h):
    """Veřejná adresa brány. Pevně z HUB_GW_PUBLIC_URL, jinak z hlaviček od
    nginx (brána poslouchá jen na loopbacku, podvrhnout je nejde)."""
    fixed = os.environ.get("HUB_GW_PUBLIC_URL", "").rstrip("/")
    if fixed:
        return fixed
    scheme = "https" if h._https() else "http"
    return f"{scheme}://{h._public_host()}"


def resource_url(h):
    return base_url(h) + "/mcp"


def _no_frame():
    return {"Content-Security-Policy": "frame-ancestors 'none'",
            "X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer"}


def _read_body(h):
    try:
        length = int(h.headers.get("Content-Length") or 0)
    except ValueError:
        length = 0
    if length > MAX_BODY:
        raise ValueError("too-large")
    return h.rfile.read(length) if length > 0 else b""


def _form(h):
    raw = _read_body(h)
    ctype = h.headers.get("Content-Type", "")
    if ctype.startswith("application/json"):
        try:
            data = json.loads(raw or b"{}")
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}
    parsed = urllib.parse.parse_qs(raw.decode("utf-8", "replace"), keep_blank_values=True)
    return {k: v[0] for k, v in parsed.items()}


# ── metadata (RFC 9728, RFC 8414) ────────────────────────────────────────────
def protected_resource(h):
    base = base_url(h)
    h._json({"resource": base + "/mcp", "authorization_servers": [base],
             "scopes_supported": ["prostor"], "bearer_methods_supported": ["header"],
             "resource_name": "Aladin — tvůj prostor"})


def auth_server(h):
    base = base_url(h)
    h._json({"issuer": base,
             "authorization_endpoint": base + "/oauth/authorize",
             "token_endpoint": base + "/oauth/token",
             "registration_endpoint": base + "/oauth/register",
             "revocation_endpoint": base + "/oauth/revoke",
             "response_types_supported": ["code"],
             "grant_types_supported": ["authorization_code", "refresh_token"],
             "code_challenge_methods_supported": ["S256"],
             "token_endpoint_auth_methods_supported": ["none"],
             "revocation_endpoint_auth_methods_supported": ["none"],
             "scopes_supported": ["prostor"]})


# ── registrace klienta (RFC 7591) ────────────────────────────────────────────
def register(h, method):
    try:
        raw = _read_body(h)
    except ValueError:
        h.close_connection = True
        return h._json({"error": "invalid_request"}, 413)
    if method != "POST":
        return h._json({"error": "invalid_request"}, 405)
    ip = h._client_ip()
    now = time.time()
    with _req_lock:
        hits = [t for t in _registers.get(ip, []) if now - t < 3600]
        if len(hits) >= REGISTER_LIMIT:
            return h._json({"error": "invalid_request",
                            "error_description": "Too many registrations."}, 429)
        _registers[ip] = hits + [now]
    try:
        data = json.loads(raw or b"{}")
    except ValueError:
        data = None
    if not isinstance(data, dict):
        return h._json({"error": "invalid_client_metadata"}, 400)
    uris = data.get("redirect_uris")
    if (not isinstance(uris, list) or not uris or len(uris) > 5
            or not all(redirect_ok(u) for u in uris)):
        return h._json({"error": "invalid_redirect_uri",
                        "error_description": "Only the Claude app callback or a "
                                             "loopback address is allowed."}, 400)
    method_auth = data.get("token_endpoint_auth_method") or "none"
    if method_auth != "none":
        return h._json({"error": "invalid_client_metadata",
                        "error_description": "Public clients only (PKCE)."}, 400)
    name = str(data.get("client_name") or "Claude")[:80]
    client_id = h.accounts.oauth_client_add(name, uris)
    return h._send(201, json.dumps({
        "client_id": client_id, "client_name": name, "redirect_uris": uris,
        "token_endpoint_auth_method": "none",
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"], "client_id_issued_at": int(now)}),
        "application/json; charset=utf-8")


# ── přihlášení a souhlas ─────────────────────────────────────────────────────
def _page(title, body):
    from .server import _page as page
    return page(title, body)


def _error_page(h, text, code=400):
    return h._send(code, _page("Napojení — Aladin", f"""
<div class=card><h1>Napojení se nepovedlo</h1>
<p class=sub>{html.escape(text)}</p></div>"""), "text/html; charset=utf-8", _no_frame())


def _req_new(data):
    rid = secrets.token_urlsafe(24)
    now = time.time()
    with _req_lock:
        for old, r in list(_requests.items()):
            if r["exp"] < now:
                _requests.pop(old, None)
        data.update({"exp": now + REQUEST_TTL, "uid": None, "stage": "heslo", "tries": 0})
        _requests[rid] = data
    return rid


def _req_get(rid):
    with _req_lock:
        r = _requests.get(rid or "")
        if r and r["exp"] < time.time():
            _requests.pop(rid, None)
            return None
        return r


def _req_drop(rid):
    with _req_lock:
        _requests.pop(rid or "", None)


def _host(uri):
    return urllib.parse.urlsplit(uri).netloc


def _form_page(h, rid, req, error=""):
    err = f'<div class=err>{html.escape(error)}</div>' if error else ""
    who = html.escape(req["client_name"] or "Claude")
    where = html.escape(_host(req["redirect_uri"]))
    if req["stage"] == "heslo":
        body = f"""
<form class=card method=post action="/oauth/authorize">
<h1>Napojit {who}</h1>
<p class=sub>Přihlas se ke svému účtu na serveru. Napojení povede na
<b>{where}</b>.</p>
<input type=hidden name=req value="{rid}">
<input type=hidden name=krok value=heslo>
<label>E-mail</label>
<input name=email type=email autocomplete=username autofocus required>
<label>Heslo</label>
<input name=password type=password autocomplete=current-password required>
<button type=submit>Pokračovat</button>
{err}
</form>"""
    elif req["stage"] == "kod":
        body = f"""
<form class=card method=post action="/oauth/authorize">
<h1>Kód z aplikace</h1>
<p class=sub>Zadej šesticiferný kód z aplikace v mobilu (nebo záložní kód).</p>
<input type=hidden name=req value="{rid}">
<input type=hidden name=krok value=kod>
<label>Kód</label>
<input name=code inputmode=numeric autocomplete=one-time-code autofocus required>
<button type=submit>Ověřit</button>
{err}
</form>"""
    else:
        user = h.accounts.by_id(req["uid"]) or {}
        level = h.accounts.company_level(user)
        firma = {"none": "", "read": "<li>číst firemní poznámky</li>",
                 "write": "<li>číst firemní poznámky a zapisovat do nich</li>"}[level]
        body = f"""
<form class=card method=post action="/oauth/authorize">
<h1>Povolit {who}?</h1>
<p class=sub>Přihlášen jako <b>{html.escape(user.get("email", ""))}</b>.
Napojení povede na <b>{where}</b>.</p>
<p>Claude v aplikaci pak bude moct <b>jen ve tvém prostoru</b>:</p>
<ul class=steps>
<li>vidět tvoje projekty a rozhovory</li>
<li>číst a zapisovat tvoje poznámky a sdílené poznámky, kde jsi členem</li>
{firma}
<li>číst a zapisovat soubory v tvém prostoru a <b>spouštět v něm programy</b></li>
</ul>
<p class=hint>Napojení zrušíš změnou hesla, nebo ti ho zruší správce serveru.</p>
<input type=hidden name=req value="{rid}">
<input type=hidden name=krok value=souhlas>
<button type=submit name=rozhodnuti value=povolit>Povolit</button>
<button type=submit name=rozhodnuti value=zamitnout
 style="background:none;border:1px solid #30363d;color:#c9d1d9">Zamítnout</button>
{err}
</form>"""
    return h._send(200, _page("Napojení — Aladin", body), "text/html; charset=utf-8",
                   _no_frame())


def _back(h, req, **params):
    """Návrat do appky: kód (nebo chyba) na ověřenou adresu pro návrat."""
    params["state"] = req.get("state") or None
    params["iss"] = base_url(h)
    query = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    uri = req["redirect_uri"]
    return h._redirect(uri + ("&" if "?" in uri else "?") + query)


def authorize(h, method):
    if method == "GET":
        q = {k: v[0] for k, v in urllib.parse.parse_qs(
            urllib.parse.urlparse(h.path).query).items()}
        client = h.accounts.oauth_client(q.get("client_id", ""))
        if not client:
            return _error_page(h, "Neznámá aplikace — přidej napojení v aplikaci Claude znovu.")
        redirect = q.get("redirect_uri") or (client["redirect_uris"][0]
                                             if len(client["redirect_uris"]) == 1 else "")
        # Dokud adresa pro návrat není ověřená, nikam se nepřesměrovává.
        if redirect not in client["redirect_uris"] or not redirect_ok(redirect):
            return _error_page(h, "Něco nesedí s aplikací, která se chce napojit. Přidej napojení v aplikaci Claude znovu.")
        req = {"client_id": client["client_id"], "client_name": client["name"],
               "redirect_uri": redirect, "state": q.get("state", "")[:500]}
        if q.get("response_type") != "code":
            return _back(h, req, error="unsupported_response_type")
        challenge = q.get("code_challenge", "")
        if q.get("code_challenge_method") != "S256" or not re.fullmatch(r"[A-Za-z0-9_-]{43}", challenge):
            return _back(h, req, error="invalid_request",
                         error_description="PKCE S256 is required.")
        resource = q.get("resource", "")
        if resource and resource.rstrip("/") != resource_url(h):
            return _back(h, req, error="invalid_target")
        req.update({"challenge": challenge, "resource": resource_url(h)})
        rid = _req_new(req)
        return _form_page(h, rid, _req_get(rid))

    if method != "POST":
        return _error_page(h, "Tahle stránka se takhle otevřít nedá. Spusť napojení v aplikaci Claude znovu.", 405)
    try:
        form = _form(h)
    except ValueError:
        h.close_connection = True
        return _error_page(h, "Tahle stránka se takhle otevřít nedá. Spusť napojení v aplikaci Claude znovu.", 413)
    if not h._same_origin():
        return _error_page(h, "Formulář se musí odeslat z téhle stránky. Spusť napojení v aplikaci Claude znovu.", 403)
    rid = form.get("req", "")
    req = _req_get(rid)
    if not req:
        return _error_page(h, "Přihlášení vypršelo — spusť napojení v aplikaci Claude znovu.")
    step = form.get("krok")
    if step != req["stage"]:
        return _form_page(h, rid, req)

    if step == "heslo":
        email = (form.get("email") or "").strip()
        keys = h._fail_keys(email)
        wait = h.accounts.fail_wait(keys)
        if wait:
            from .server import too_many
            return _form_page(h, rid, req, too_many(wait))
        user = h.accounts.verify(email, form.get("password") or "")
        if not user:
            note, _left = h._failed(keys)
            return _form_page(h, rid, req, "Špatný e-mail nebo heslo. " + (note or ""))
        req["uid"] = user["id"]
        state = h.accounts.twofa(user["id"])
        if state["enabled"]:
            req["stage"] = "kod"
        elif config.REQUIRE_2FA:
            _req_drop(rid)
            return _error_page(h, "Nejdřív se přihlas do Aladina v prohlížeči a nastav si "
                                  "ověření kódem z aplikace. Pak napojení zopakuj.")
        else:
            h.accounts.fail_clear(keys[0][0])
            req["stage"] = "souhlas"
        return _form_page(h, rid, req)

    if step == "kod":
        user = h.accounts.by_id(req["uid"])
        if not user:
            _req_drop(rid)
            return _error_page(h, "Účet není aktivní.")
        keys = h._fail_keys(user["email"])
        wait = h.accounts.fail_wait(keys)
        if wait:
            _req_drop(rid)
            from .server import too_many
            return _error_page(h, too_many(wait), 429)
        if not h.accounts.second_factor(user["id"], str(form.get("code") or "")):
            note, _left = h._failed(keys)
            req["tries"] += 1
            if req["tries"] >= config.LOGIN_TRIES:
                _req_drop(rid)
                return _error_page(h, "Kód nesedí. Spusť napojení v aplikaci Claude znovu. " + (note or ""))
            return _form_page(h, rid, req, "Kód nesedí. " + (note or ""))
        h.accounts.fail_clear(keys[0][0])
        req["stage"] = "souhlas"
        return _form_page(h, rid, req)

    # souhlas
    _req_drop(rid)
    user = h.accounts.by_id(req["uid"])
    if not user:
        return _error_page(h, "Účet není aktivní.")
    if form.get("rozhodnuti") != "povolit":
        return _back(h, req, error="access_denied")
    code = h.accounts.oauth_code_new(user, req["client_id"], req["redirect_uri"],
                                     req["challenge"], req["resource"])
    _log(user, req["client_name"], "napojeni", True, kam=_host(req["redirect_uri"]))
    return _back(h, req, code=code)


# ── tokeny ───────────────────────────────────────────────────────────────────
def _token_error(h, error, text="", code=400):
    body = {"error": error}
    if text:
        body["error_description"] = text
    return h._send(code, json.dumps(body), "application/json; charset=utf-8",
                   {"Pragma": "no-cache"})


def token(h, method):
    try:
        form = _form(h)
    except ValueError:
        h.close_connection = True
        return _token_error(h, "invalid_request", code=413)
    if method != "POST":
        return _token_error(h, "invalid_request", code=405)
    grant = form.get("grant_type")
    client_id = form.get("client_id", "")
    if not h.accounts.oauth_client(client_id):
        return _token_error(h, "invalid_client", code=401)
    resource = form.get("resource", "")
    if resource and resource.rstrip("/") != resource_url(h):
        return _token_error(h, "invalid_target")

    if grant == "authorization_code":
        found = h.accounts.oauth_code_take(form.get("code", ""))
        verifier = form.get("code_verifier", "")
        if (not found or found["client_id"] != client_id
                or found["redirect_uri"] != form.get("redirect_uri", found["redirect_uri"])
                or not re.fullmatch(r"[A-Za-z0-9._~-]{43,128}", verifier)):
            return _token_error(h, "invalid_grant")
        digest = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode()
        if not hmac.compare_digest(digest, found["challenge"]):
            return _token_error(h, "invalid_grant")
        if not h.accounts.by_id(found["user_id"]):
            return _token_error(h, "invalid_grant")
        out = h.accounts.oauth_issue(found["user_id"], client_id, found["resource"])
    elif grant == "refresh_token":
        out = h.accounts.oauth_refresh(form.get("refresh_token", ""), client_id)
        if not out:
            return _token_error(h, "invalid_grant")
    else:
        return _token_error(h, "unsupported_grant_type")
    out["scope"] = "prostor"
    return h._send(200, json.dumps(out), "application/json; charset=utf-8",
                   {"Pragma": "no-cache"})


def revoke(h, method):
    try:
        form = _form(h)
    except ValueError:
        h.close_connection = True
        return _token_error(h, "invalid_request", code=413)
    if method != "POST":
        return _token_error(h, "invalid_request", code=405)
    h.accounts.oauth_revoke_token(form.get("token", ""))
    return h._send(200, b"")               # RFC 7009: vždycky 200


# ── nástroje ─────────────────────────────────────────────────────────────────
def _s(desc, **props):
    required = [k for k, v in props.items() if v.pop("_req", False)]
    return {"type": "object", "properties": props, "required": required,
            "additionalProperties": False}


OBSIDIAN = {"type": "string",
            "description": "Který Obsidian: 'osobni' (výchozí), 'firma' (firemní), "
                           "'firma:<zkratka>' (další firma) nebo 'sdilene:<zkratka>' "
                           "(viz nástroj prostor)."}
TOOLS = [
    {"name": "prostor", "title": "Můj prostor",
     "description": "Kdo jsem, jaké mám právo k firemnímu Obsidianu, do kterých "
                    "sdílených Obsidianů patřím a kde je můj domov na serveru. "
                    "Zavolej jako první.",
     "inputSchema": _s(""), "annotations": {"readOnlyHint": True}},
    {"name": "projekty", "title": "Projekty",
     "description": "Projekty v mém prostoru (jméno, cesta, git větev, necommitnuté změny).",
     "inputSchema": _s(""), "annotations": {"readOnlyHint": True}},
    {"name": "konverzace", "title": "Konverzace",
     "description": "Konverzace s Claude Code v mém prostoru, nejnovější první.",
     "inputSchema": _s("", hledat={"type": "string", "description": "Hledaný text"},
                       limit={"type": "integer", "minimum": 1, "maximum": 200}),
     "annotations": {"readOnlyHint": True}},
    {"name": "konverzace_cti", "title": "Přečíst konverzaci",
     "description": "Text konverzace (zadání, odpovědi, použité nástroje) — od konce.",
     "inputSchema": _s("", id={"type": "string", "description": "id z nástroje konverzace",
                               "_req": True}),
     "annotations": {"readOnlyHint": True}},
    {"name": "obsidian_seznam", "title": "Poznámky v Obsidianu",
     "description": "Seznam poznámek v Obsidianu.",
     "inputSchema": _s("", obsidian=dict(OBSIDIAN)), "annotations": {"readOnlyHint": True}},
    {"name": "obsidian_hledat", "title": "Hledat v Obsidianu",
     "description": "Fulltextové hledání v poznámkách Obsidianu.",
     "inputSchema": _s("", dotaz={"type": "string", "_req": True}, obsidian=dict(OBSIDIAN)),
     "annotations": {"readOnlyHint": True}},
    {"name": "obsidian_cti", "title": "Přečíst poznámku",
     "description": "Obsah poznámky v Obsidianu.",
     "inputSchema": _s("", cesta={"type": "string", "description": "Cesta k .md v trezoru",
                                  "_req": True}, obsidian=dict(OBSIDIAN)),
     "annotations": {"readOnlyHint": True}},
    {"name": "obsidian_zapis", "title": "Zapsat poznámku",
     "description": "Uloží poznámku (Markdown) do Obsidianu. Firemní jen s právem zápisu "
                    "a uvidí ji celý tým; sdílený jen jako člen. Existující poznámku "
                    "přepíše jen s prepsat=true.",
     "inputSchema": _s("", cesta={"type": "string", "_req": True},
                       text={"type": "string", "_req": True}, obsidian=dict(OBSIDIAN),
                       prepsat={"type": "boolean"}),
     "annotations": {"readOnlyHint": False, "destructiveHint": True}},
    {"name": "slozka", "title": "Obsah složky",
     "description": "Soubory a složky v mém prostoru (cesta relativně k domovu, nebo "
                    "absolutní v něm).",
     "inputSchema": _s("", cesta={"type": "string"}), "annotations": {"readOnlyHint": True}},
    {"name": "soubor_cti", "title": "Přečíst soubor",
     "description": "Textový soubor z mého prostoru (do 512 kB).",
     "inputSchema": _s("", cesta={"type": "string", "_req": True}),
     "annotations": {"readOnlyHint": True}},
    {"name": "soubor_zapis", "title": "Zapsat soubor",
     "description": "Zapíše textový soubor v mém prostoru. Existující přepíše jen "
                    "s prepsat=true.",
     "inputSchema": _s("", cesta={"type": "string", "_req": True},
                       text={"type": "string", "_req": True}, prepsat={"type": "boolean"}),
     "annotations": {"readOnlyHint": False, "destructiveHint": True}},
    {"name": "prikaz", "title": "Spustit příkaz",
     "description": "Spustí příkaz v bashi v mém prostoru na serveru (izolovaně, jen "
                    "můj domov). Vrací výstup a návratový kód.",
     "inputSchema": _s("", prikaz={"type": "string", "_req": True},
                       slozka={"type": "string", "description": "Pracovní složka v domově"},
                       limit_sekund={"type": "integer", "minimum": 1, "maximum": 600}),
     "annotations": {"readOnlyHint": False, "destructiveHint": True,
                     "openWorldHint": True}},
]
TOOL_NAMES = {t["name"] for t in TOOLS}

INSTRUCTIONS = ("Nástroje pracují v prostoru přihlášeného člověka na serveru Claude Code "
                "Hubu: jeho projekty, konverzace s Claude Code, jeho Obsidian, firemní "
                "Obsidian podle jeho práva a sdílené Obsidiany, kde je členem. Začni "
                "nástrojem `prostor`. Do firemního Obsidianu zapisuj jen na výslovné "
                "přání — uvidí to celý tým; hesla ani klíče tam nikdy nepatří.")


class ToolError(Exception):
    pass


def _hub_call(h, user, tool, args, timeout=90):
    """Zavolá nástroj v instanci hubu uživatele (v jeho sandboxu)."""
    try:
        hub = h.hubs.get(user)
    except RuntimeError as exc:
        raise ToolError(f"Prostor teď nejde spustit: {exc}") from None
    body = json.dumps({"tool": tool, "args": args}).encode("utf-8")
    conn = http.client.HTTPConnection("127.0.0.1", hub.port, timeout=timeout)
    try:
        conn.request("POST", "/api/mcp-tool", body=body, headers={
            "Content-Type": "application/json", "X-Hub-Token": hub.token,
            "Host": f"127.0.0.1:{hub.port}"})
        resp = conn.getresponse()
        raw = resp.read(8 * 1024 * 1024)
    except (OSError, http.client.HTTPException) as exc:
        raise ToolError(f"Prostor neodpověděl: {exc}") from None
    finally:
        conn.close()
    try:
        data = json.loads(raw.decode("utf-8"))
    except ValueError:
        raise ToolError(f"Prostor odpověděl nesrozumitelně ({resp.status}).") from None
    if resp.status != 200 or not isinstance(data, dict):
        raise ToolError((data or {}).get("error") or f"Prostor vrátil {resp.status}.")
    if not data.get("ok"):
        raise ToolError(data.get("error") or "Nepovedlo se.")
    return data.get("result")


def _which(args):
    which = str(args.get("obsidian") or "osobni").strip()
    if which in ("", "osobní"):
        which = "osobni"
    return which


def _check_vault(h, user, which, write=False):
    """Oprávnění k Obsidianu — vždycky v bráně, ne jen v prostoru."""
    if which == "osobni":
        return
    if which == "firma":
        level = h.accounts.company_level(user)
        if level == "none":
            raise ToolError("K firemnímu Obsidianu nemáš přístup — požádej admina.")
        if write and level != "write":
            raise ToolError("Firemní Obsidian máš jen ke čtení — požádej admina o zápis.")
        return
    if which.startswith("firma:"):
        role = firmy.role(user, which[6:]) if firmy.exists(which[6:]) else ""
        if not role:
            raise ToolError("Do téhle firmy nepatříš.")
        if write and role == "host":
            raise ToolError("Ve firmě jsi host — jen čteš. O zápis požádej kapitána.")
        return
    if which.startswith("sdilene:"):
        if which[8:] not in {v["slug"] for v in shared.vaults_for(user)}:
            raise ToolError("Do tohohle sdíleného Obsidianu nepatříš.")
        return
    raise ToolError("Neznámý Obsidian — použij osobni, firma, firma:<zkratka> "
                    "nebo sdilene:<zkratka>.")


def run_tool(h, user, name, args):
    if name not in TOOL_NAMES:
        raise ToolError(f"Neznámý nástroj: {name}")
    if not isinstance(args, dict):
        args = {}
    if name == "prostor":
        return {"email": user["email"], "jmeno": user["name"], "role": user["role"],
                "firemni_obsidian": {"none": "nemá přístup", "read": "jen čtení",
                                     "write": "čtení i zápis"}[h.accounts.company_level(user)],
                "sdilene_obsidiany": [{"zkratka": "sdilene:" + v["slug"], "nazev": v["name"]}
                                      for v in shared.vaults_for(user)],
                "firmy": [{"zkratka": "firma:" + f["zkratka"], "nazev": f["nazev"],
                           "role": firmy.ROLE_LABELS[f["role"]]}
                          for f in firmy.firmy_for(user) if f["zkratka"] != firmy.LEGACY],
                "domov": workspace.home_for(user)}
    if name in ("obsidian_seznam", "obsidian_hledat", "obsidian_cti"):
        which = _which(args)
        _check_vault(h, user, which)
        return _hub_call(h, user, name, {**args, "obsidian": which})
    if name == "obsidian_zapis":
        which = _which(args)
        _check_vault(h, user, which, write=True)
        text = args.get("text")
        if not isinstance(text, str):
            raise ToolError("Chybí text poznámky.")
        try:
            if which == "firma" or which.startswith("firma:"):
                result = workspace.write_company(user, args.get("cesta"), text,
                                                 bool(args.get("prepsat")), via="mcp",
                                                 firma=which[6:] or firmy.LEGACY)
            elif which.startswith("sdilene:"):
                result = shared.write_note(user, which[8:], args.get("cesta"), text,
                                           bool(args.get("prepsat")))
            else:
                return _hub_call(h, user, "obsidian_zapis_osobni", args)
        except (ValueError, PermissionError) as exc:
            raise ToolError(str(exc)) from None
        if result.get("exists"):
            result["zprava"] = "Poznámka už existuje — pošli znovu s prepsat: true."
        return result
    if name == "prikaz":
        try:
            limit = max(1, min(int(args.get("limit_sekund") or 120), 600))
        except (TypeError, ValueError):
            limit = 120
        return _hub_call(h, user, name, args, timeout=limit + 30)
    return _hub_call(h, user, name, args)


def _log_args(name, args):
    """Co z volání zapsat do záznamu: cesty a příkaz, ne obsah."""
    if not isinstance(args, dict):
        return {}
    out = {}
    for key in ("cesta", "obsidian", "id", "slozka"):
        if args.get(key):
            out[key] = str(args[key])[:200]
    if name == "prikaz" and args.get("prikaz"):
        out["prikaz"] = str(args["prikaz"])[:300]
    return out


# ── MCP přes HTTP (Streamable HTTP, jen JSON odpovědi) ───────────────────────
def _unauthorized(h, error="invalid_token"):
    meta = base_url(h) + "/.well-known/oauth-protected-resource"
    return h._send(401, json.dumps({"error": error}), "application/json; charset=utf-8",
                   {"WWW-Authenticate": f'Bearer error="{error}", '
                                        f'resource_metadata="{meta}"'})


def _rpc_result(mid, result):
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def _rpc_error(mid, code, message):
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


def _handle(h, user, msg):
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
        return _rpc_error(None, -32600, "Invalid Request")
    mid = msg.get("id")
    method = msg.get("method")
    params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
    if mid is None:                               # notifikace — žádná odpověď
        return None
    if method == "initialize":
        want = params.get("protocolVersion")
        return _rpc_result(mid, {
            "protocolVersion": want if want in PROTOCOLS else PROTOCOLS[0],
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "claude-code-hub", "title": "Aladin",
                           "version": __version__},
            "instructions": INSTRUCTIONS})
    if method == "ping":
        return _rpc_result(mid, {})
    if method == "tools/list":
        return _rpc_result(mid, {"tools": TOOLS})
    if method == "tools/call":
        name = str(params.get("name") or "")
        args = params.get("arguments") or {}
        try:
            result = run_tool(h, user, name, args)
            _log(user, user.get("client_id"), name, True, **_log_args(name, args))
            return _rpc_result(mid, {"content": [{"type": "text", "text": json.dumps(
                result, ensure_ascii=False, indent=1)}], "isError": False})
        except ToolError as exc:
            _log(user, user.get("client_id"), name, False, chyba=str(exc)[:200],
                 **_log_args(name, args))
            return _rpc_result(mid, {"content": [{"type": "text", "text": str(exc)}],
                                     "isError": True})
    return _rpc_error(mid, -32601, "Method not found")


def endpoint(h, method):
    # Tělo se čte vždycky jako první: odmítnutý požadavek by jinak nechal
    # nedočtené bajty na spojení a ty by začínaly další požadavek.
    try:
        raw = _read_body(h)
    except ValueError:
        h.close_connection = True
        return h._send(413, b"Too large")
    origin = h.headers.get("Origin", "")
    if origin and origin not in ORIGINS and origin != base_url(h):
        return h._send(403, b"Forbidden origin")
    head = h.headers.get("Authorization", "")
    access = head[7:].strip() if head[:7].lower() == "bearer " else ""
    user = h.accounts.oauth_user(access, resource_url(h)) if access else None
    if not user:
        return _unauthorized(h)
    if method != "POST":
        # Bez vlastního proudu událostí (SSE): server nic sám neposílá.
        return h._send(405, b"", extra={"Allow": "POST"})
    try:
        msg = json.loads(raw or b"null")
    except ValueError:
        return h._json(_rpc_error(None, -32700, "Parse error"), 400)
    if isinstance(msg, list):
        out = [r for r in (_handle(h, user, m) for m in msg[:20]) if r is not None]
        return h._json(out) if out else h._send(202, b"")
    reply = _handle(h, user, msg)
    if reply is None:
        return h._send(202, b"")
    return h._json(reply)


def route(h, method, path):
    """Vrátí True, když cestu obsloužil tenhle modul."""
    if path in ("/.well-known/oauth-protected-resource",
                "/.well-known/oauth-protected-resource/mcp"):
        protected_resource(h)
    elif path in ("/.well-known/oauth-authorization-server",
                  "/.well-known/oauth-authorization-server/mcp"):
        auth_server(h)
    elif path == "/oauth/register":
        register(h, method)
    elif path == "/oauth/authorize":
        authorize(h, method)
    elif path == "/oauth/token":
        token(h, method)
    elif path == "/oauth/revoke":
        revoke(h, method)
    elif path in ("/mcp", "/mcp/"):
        endpoint(h, method)
    else:
        return False
    return True
