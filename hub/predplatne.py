"""
Claude v prostoru na serveru na vlastním předplatném — bez klíče API a bez
nastavování.

Kdo pracuje v prostoru na serveru, potřebuje tam mít Clauda přihlášeného. Klíč
API platí firma podle spotřeby; tohle je druhá cesta: **vlastní předplatné toho
člověka** (Pro, Max, Team, Enterprise), stejné, na kterém jede Claude Code
u něj na počítači.

* **Na počítači** (`start` a spol.): appka spustí `claude setup-token`. Ten
  otevře prohlížeč, člověk klikne *Authorize* a Claude Code vypíše token na rok
  (jen na používání Clauda, ne na správu účtu). Appka ho z výstupu vezme, pošle
  bráně (`/gw/claude`, tokenem zařízení) a nikde ho neukládá — brána ho dá jen
  prostoru tohohle účtu jako `CLAUDE_CODE_OAUTH_TOKEN`. Když se prohlížeč
  neotevře, appka ukáže odkaz a pole na kód ze stránky.
* **V prostoru na serveru** (`prepare_space`): hub při startu předvyplní
  v `~/.claude.json`, že je Claude Code nastavený a klíč API schválený — jinak
  by se i s tokenem v prostředí ptal na motiv a způsob přihlášení.

Jedno předplatné patří jednomu člověku. Brána ho proto váže na účet, který ho
připojil, a nikomu jinému ho nedá.
"""
import json
import os
import re
import secrets
import shutil
import threading
import time

from . import account, core, pty_backend

TOKEN_RE = re.compile(r"sk-ant-oat01-[A-Za-z0-9_-]{40,400}")
# Odkaz pro ruční přihlášení (přesměruje na stránku s kódem). Druhý, s návratem
# na localhost, otevírá Claude Code v prohlížeči sám.
URL_RE = re.compile(rb"https://claude\.(?:com|ai)/[^\s\x07\x1b\]]*oauth/authorize\?[^\s\x07\x1b\]]+")
_ESC = re.compile(rb"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b\[[0-9;?<>=]*[ -/]*[@-~]|\x1b[@-_]")
WAIT = 10 * 60

STATE = {"state": "idle", "url": "", "error": "", "restarted": False, "since": 0.0}
_LOCK = threading.Lock()
_RUN = {"pty": None, "id": ""}


# ── na počítači ──────────────────────────────────────────────────────────────
def claude_bin():
    return shutil.which("claude") or ""


def server_state(timeout=10):
    """Na čem Claude v prostoru jede, podle brány. {"mode", …} nebo {"error", "kind"}."""
    token = core.CONFIG.get("gw_token") or ""
    if not token or not account._base():
        return {"error": "Appka není přihlášená k serveru.", "kind": account.AUTH}
    data, err, kind = account._call("/gw/claude", token=token, timeout=timeout)
    if data is None:
        if kind == account.AUTH:
            # Starší brána /gw/claude nezná a odpoví jako na cizí token.
            info, _e, _k = account._call("/gw/info", timeout=timeout)
            if info and "predplatne" not in (info.get("features") or []):
                return {"error": "Server tohle ještě neumí.", "kind": "unsupported"}
        return {"error": err, "kind": kind}
    return data


def status(timeout=10):
    with _LOCK:
        run = dict(STATE)
    return {"claude": bool(claude_bin()), "skip": bool(core.CONFIG.get("predplatne_skip")),
            "connect": run, "server": server_state(timeout)}


def should_offer(timeout=6):
    """Má appka před vstupem do prostoru sama připojit předplatné?

    Jen když v prostoru Claude nemá na čem jet (žádné předplatné, klíč API ani
    přihlášení), na počítači je Claude Code a člověk to jednou neodmítl."""
    if core.CONFIG.get("predplatne_skip") or not claude_bin() or core.on_gateway():
        return False
    return server_state(timeout).get("mode") == "zadne"


def skip(value=True):
    core.save_config({"predplatne_skip": bool(value)})


def _set(**changes):
    with _LOCK:
        STATE.update(changes)


def start():
    """Spustí `claude setup-token` na pozadí. Vrací stav; sleduje se přes status()."""
    exe = claude_bin()
    if not exe:
        _set(state="error", url="", error="Na tomhle počítači není Claude Code (příkaz claude).")
        return dict(STATE)
    with _LOCK:
        if STATE["state"] in ("starting", "waiting", "saving"):
            return dict(STATE)
        run_id = secrets.token_hex(4)
        _RUN["id"] = run_id
        STATE.update(state="starting", url="", error="", restarted=False, since=time.time())
    argv = [exe, "setup-token"]
    if core.IS_WINDOWS and exe.lower().endswith((".cmd", ".bat")):
        argv = [os.environ.get("COMSPEC") or "cmd.exe", "/c", exe, "setup-token"]
    env = core.child_env()
    env.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
    try:
        # Široký terminál: token ani odkaz se nesmí zalomit do dvou řádků.
        pty = pty_backend.spawn(argv, cwd=core.HOME, env=env, cols=600, rows=40)
    except Exception as exc:
        _set(state="error", error=f"Claude Code nejde spustit: {exc}")
        return dict(STATE)
    _RUN["pty"] = pty
    threading.Thread(target=_watch, args=(pty, run_id), daemon=True).start()
    core.log("předplatné: spouštím claude setup-token")
    return dict(STATE)


def _current(run_id):
    return _RUN["id"] == run_id


def _watch(pty, run_id):
    buf = bytearray()
    deadline = time.time() + WAIT
    reader = threading.Thread(target=_read_into, args=(pty, buf), daemon=True)
    reader.start()
    token = ""
    while time.time() < deadline and _current(run_id):
        time.sleep(0.25)
        raw = bytes(buf)
        if not STATE["url"]:
            urls = [u.decode("ascii", "replace") for u in URL_RE.findall(raw)]
            manual = [u for u in urls if "platform.claude.com" in u or "console.anthropic.com" in u]
            if urls:
                _set(state="waiting", url=(manual or urls)[-1])
        text = _ESC.sub(b" ", raw).decode("utf-8", "replace")
        found = TOKEN_RE.findall(text)
        if found:
            time.sleep(0.5)             # dokreslit, kdyby řádek přišel po kouscích
            text = _ESC.sub(b" ", bytes(buf)).decode("utf-8", "replace")
            token = TOKEN_RE.findall(text)[-1]
            break
        if not reader.is_alive():
            break
        if "does not permit" in text:
            _set(state="error", error="Firemní nastavení Claude Code dlouhodobý token nepovoluje.")
            break
    try:
        pty.close()
    except Exception:
        pass
    if not _current(run_id):
        return
    if not token:
        if STATE["state"] != "error":
            _set(state="error", error="Přihlášení se nedokončilo." if time.time() < deadline
                 else "Přihlášení vypršelo — zkus to znovu.")
        return
    _deliver(token, run_id)


def _read_into(pty, buf):
    while True:
        data = pty.read()
        if not data:
            return
        buf.extend(data)
        if len(buf) > 2 * 1024 * 1024:
            del buf[:len(buf) - 1024 * 1024]


def _deliver(token, run_id):
    if core.on_gateway():
        # Spuštěno bránou pro Správu předplatného (host_action): token si
        # brána vyzvedne sama a uloží ho stejně, jako když ho pošle appka.
        _RUN["token"] = token
        _set(state="token")
        return
    _set(state="saving")
    gw = core.CONFIG.get("gw_token") or ""
    data, err, _kind = account._call("/gw/claude", token=gw, timeout=30,
                                     payload={"token": token, "label": _machine()})
    del token
    if not _current(run_id):
        return
    if data is None or data.get("error"):
        _set(state="error", error=(data or {}).get("error") or err or "Server token nepřijal.")
        core.log("předplatné: server token nepřijal", "warn")
        return
    _set(state="done", error="", restarted=not data.get("needs_restart"))
    core.log("předplatné: připojeno — Claude v prostoru jede na vlastním předplatném")


def _machine():
    import platform
    return "appka na " + ((platform.node() or "počítači").split(".")[0][:40])


def send_code(code):
    """Kód ze stránky po ručním přihlášení (když se prohlížeč neotevřel sám)."""
    code = str(code or "").strip()
    pty = _RUN["pty"]
    if not code or not pty or STATE["state"] not in ("starting", "waiting"):
        return dict(STATE)
    pty.write(code.encode("utf-8"))
    time.sleep(0.2)
    pty.write(b"\r")
    return dict(STATE)


def cancel():
    _RUN["id"] = ""
    pty = _RUN["pty"]
    if pty:
        try:
            pty.close()
        except Exception:
            pass
    _set(state="idle", url="", error="")
    return dict(STATE)


def disconnect():
    gw = core.CONFIG.get("gw_token") or ""
    data, err, _kind = account._call("/gw/claude", token=gw, timeout=20, payload={"remove": True})
    return data if data is not None else {"error": err}


# ── Správa předplatného hostem (gateway/pristup.py) ─────────────────────────
def _public():
    with _LOCK:
        return {k: STATE[k] for k in ("state", "url", "error")}


# Přihlášení Clauda, které brána předává prostoru (gateway/workspace.session_env).
AUTH_KLICE = ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY")


def _prihlaseni(env, prenacti=None):
    """Brána poslala nové přihlášení (připojené / odpojené předplatné): platí
    hned pro každý nový tab — taby berou prostředí z hubu (core.child_env) —
    a běžící chaty se nad toutéž konverzací spustí znovu, až nepracují."""
    env = env if isinstance(env, dict) else {}
    pred = {k: os.environ.get(k, "") for k in AUTH_KLICE}
    for k in AUTH_KLICE:
        os.environ.pop(k, None)
        v = env.get(k)
        if isinstance(v, str) and v.strip():
            os.environ[k] = v.strip()
    if core.on_gateway():
        try:
            prihlaseni_do_souboru(os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") or "")
        except Exception as exc:
            core.log_error("předplatné: zápis přihlášení do ~/.claude", exc)
    # Běžící chaty se přepnou taky (Hub.prenacti_chaty) — jen když se něco změnilo.
    if prenacti and pred != {k: os.environ.get(k, "") for k in AUTH_KLICE}:
        prenacti()
    return {"ok": True}


def host_action(action, payload, broadcast=None, prenacti=None):
    """Volá jen brána. Host vidí jen stav, odkaz a výsledek — token ani výstup
    terminálu ven nejdou (kromě `vyzvednout`, to bere brána pro sebe)."""
    if action == "stav":
        return {**_public(), "account": core.claude_account()}
    if action == "start":
        if not claude_bin():
            return {"state": "error", "url": "", "error": "V prostoru není Claude Code."}
        start()
        time.sleep(1.5)                  # ať se stihne objevit odkaz
        return _public()
    if action == "kod":
        send_code(payload.get("code"))
        return _public()
    if action == "zrusit":
        cancel()
        return _public()
    if action == "vyzvednout":
        token = _RUN.pop("token", "")
        if token:
            _set(state="done", url="")
        return {"token": token}
    if action == "prihlaseni":
        return _prihlaseni(payload.get("env"), prenacti)
    if action == "oznam":
        if broadcast:
            broadcast({"t": "host-oznameni", "text": str(payload.get("text") or "")[:300]})
        return {"ok": True}
    return {"error": "Neznámá akce."}


# ── v prostoru na serveru ────────────────────────────────────────────────────
ZALOHA_CREDENTIALS = ".credentials.json.pred-predplatnym"


def prihlaseni_do_souboru(token):
    """Token předplatného i do ~/.claude/.credentials.json.

    Claude Code token z prostředí (CLAUDE_CODE_OAUTH_TOKEN) nedává dál
    procesům, které sám pouští — hookům a příkazům. Ukládání paměti
    (memory-autosave) i appky, které si Claude v prostoru napíše a pouští
    `claude -p`, pak sáhly po starém prošlém přihlášení a padaly na „OAuth
    session expired" (Adam, 21. 10. 2026). Se souborem je přihlášený každý
    Claude v prostoru. Původní soubor (vlastní přihlášení) se jednou zazálohuje
    a po odpojení předplatného se vrátí."""
    path = os.path.join(core.CLAUDE_DIR, ".credentials.json")
    zaloha = os.path.join(core.CLAUDE_DIR, ZALOHA_CREDENTIALS)
    try:
        with open(path, encoding="utf-8") as fh:
            stary = json.load(fh)
    except (OSError, ValueError):
        stary = None
    oauth = (stary or {}).get("claudeAiOauth") if isinstance(stary, dict) else None
    nas = isinstance(oauth, dict) and oauth.get("hubPredplatne") is True
    if not token:
        if nas:                           # předplatné odpojené: náš zápis pryč
            if os.path.exists(zaloha):
                os.replace(zaloha, path)
            else:
                os.remove(path)
            core.log("předplatné: přihlášení v ~/.claude vrácené zpět")
        return
    if nas and oauth.get("accessToken") == token:
        return
    if stary is not None and not nas and not os.path.exists(zaloha):
        os.replace(path, zaloha)
    os.makedirs(core.CLAUDE_DIR, exist_ok=True)
    data = {"claudeAiOauth": {"accessToken": token, "refreshToken": None,
                              # Token z `claude setup-token` platí rok a neobnovuje se.
                              "expiresAt": int((time.time() + 365 * 86400) * 1000),
                              "scopes": ["user:inference"], "subscriptionType": None,
                              "hubPredplatne": True}}
    tmp = f"{path}.{secrets.token_hex(4)}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    os.replace(tmp, path)
    core.log("předplatné: přihlášení zapsané i do ~/.claude (pro hooky a skripty)")


def prepare_space():
    """Předvyplní ~/.claude.json, aby Claude Code s přihlášením z prostředí
    nechtěl nic nastavovat.

    Bez `hasCompletedOnboarding` se Claude Code i s tokenem předplatného nebo
    klíčem API napoprvé ptá na motiv a způsob přihlášení (ověřeno na 2.1.273).
    Klíč API navíc chce potvrdit — zapíše se, jak by to udělal on sám:
    posledních 20 znaků v customApiKeyResponses.approved.

    Běží v sandboxu prostoru, ne v bráně: soubor patří session a brána do něj
    sahat nemá (viz gateway/safefs.py). Hub startuje dřív než kterýkoli Claude
    v prostoru, takže se nikomu pod rukama nic nemění.
    """
    if not core.on_gateway():
        return
    token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") or ""
    key = os.environ.get("ANTHROPIC_API_KEY") or ""
    try:
        prihlaseni_do_souboru(token)
    except Exception as exc:
        core.log_error("předplatné: zápis přihlášení do ~/.claude", exc)
    if not token and not key:
        return
    path = os.path.join(core.HOME, ".claude.json")
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        data = {}
    except (OSError, ValueError):
        return                            # rozbitý nechat být, než smazat nastavení
    if not isinstance(data, dict):
        return
    before = json.dumps(data, sort_keys=True)
    data["hasCompletedOnboarding"] = True
    # Zbytek dřívějšího přihlášení vlastním účtem: token je pryč, údaje o účtu
    # zůstaly. Claude Code by podle nich ukazoval cizí e-mail.
    if "oauthAccount" in data and not os.path.exists(
            os.path.join(core.CLAUDE_DIR, ".credentials.json")):
        data.pop("oauthAccount", None)
    if key and not token:
        tail = key.strip()[-20:]
        resp = data.get("customApiKeyResponses")
        resp = resp if isinstance(resp, dict) else {}
        approved = [k for k in (resp.get("approved") or []) if isinstance(k, str)]
        rejected = [k for k in (resp.get("rejected") or []) if isinstance(k, str) and k != tail]
        data["customApiKeyResponses"] = {**resp, "rejected": rejected,
                                         "approved": approved if tail in approved else approved + [tail]}
    if json.dumps(data, sort_keys=True) == before:
        return
    tmp = f"{path}.{secrets.token_hex(4)}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    core.log("prostor: Claude Code připravený na přihlášení z prostředí ("
             + ("předplatné" if token else "klíč API") + ")")
