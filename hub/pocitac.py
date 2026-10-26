"""
Claude ze serveru na tomhle počítači.

V prostoru na serveru má člověk svého Clauda, ale projekty, dokumenty a programy
má často pořád tady. Tenhle modul je most mezi nimi a má dvě půlky:

* **Na počítači** (`start()` → `_loop`): hub se tokenem zařízení ptá brány,
  jestli pro něj Claude z prostoru nemá úkol — přečíst soubor, spustit příkaz —,
  provede ho a pošle výsledek (brána: `gateway/pocitac.py`). Spojení staví
  počítač: dlouhé dotazy po HTTPS tou cestou, kudy jde přihlášení. Žádný port
  se neotevírá.
* **V prostoru na serveru** (`register_mcp`): hub zaregistruje Claude Code
  MCP server `tools/pocitac_mcp.py`, přes který Claude úkoly posílá.

Pravidla, na kterých to stojí:

* **Vypnuto, dokud se nezapne.** Přístup se volí tady, v appce na počítači
  (Nastavení → Účet): vypnuto, jen čtení, plný přístup. Ověřuje se u každého
  úkolu znovu, podle toho, co platí teď. Ze serveru se změnit nedá — POST na
  /api musí přijít ze stránky hubu (server._origin_ok), ne z prostoru.
* **Běží, jen když běží appka.** Zavřené okno = počítač není připojený.
* **Přihlášení appky a Claude Code Claude ze serveru nepřečte** (`_guarded`):
  s tokenem zařízení by si v prostoru sám potvrdil, co má potvrdit člověk.
  Příkazům (plný přístup) se to zakázat nedá — proto je to zvlášť volba.
* Každý úkol jde do logu hubu a posledních pár je vidět v nastavení.

**Úkoly na později.** Když tenhle počítač připojený nebyl, mohl mu Claude
z prostoru nechat zadání a soubory (MCP nástroj `nechat_ukol`). Brána je
nabídne v odpovědi na dotaz; hub si je stáhne do `~/.claude/hub-ukoly/<id>`
a úkol dodělá Claude Code v novém tabu tady. S plným přístupem a s „ptát se“
se tab otevře sám, s přístupem jen ke čtení čeká úkol na potvrzení v okně
appky — prostor na serveru ho spustit neumí, stejně jako neumí změnit přístup.

**Souhlas ze serveru.** Když Claude v tabu s úkolem potřebuje souhlas
(oprávnění, otázka, plán), hook `tools/povoleni_hook.py` žádost ohlásí hubu
a ten ji pošle bráně — člověk odpoví v prostoru na serveru nebo v terminálu
tady, platí první odpověď (`povoleni_*`, `tab_launch`).
"""
import base64
import collections
import fnmatch
import getpass
import json
import os
import platform
import re
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time

from . import __version__, account, core

LEVELS = ("", "cteni", "ptat", "vse")
READ_OPS = frozenset({"info", "ls", "read", "find", "download", "sessions", "session_read"})
# Nic nemění a nic citlivého neukazuje: seznam procesů, oznámení v okně appky.
SAFE_OPS = frozenset({"ps", "notify"})
# Mění počítač nebo vidí obrazovku a schránku — při „ptát se“ jen po souhlasu člověka.
ASK_OPS = frozenset({"write", "edit", "upload", "run", "session_send", "screenshot", "clipboard_get",
                     "clipboard_set", "open"})
ALL_OPS = READ_OPS | SAFE_OPS | ASK_OPS
ALLOWED = {"cteni": READ_OPS | SAFE_OPS, "ptat": ALL_OPS, "vse": ALL_OPS}
MCP_NAME = "pocitac"
APPROVE_WAIT = 120          # jak dlouho se čeká na souhlas; brána s tím počítá
GRANT_SECONDS = 30 * 60     # „povolit na 30 minut“
DECISIONS = ("jednou", "30min", "zamitnout")

POLL_TIMEOUT = 45           # brána drží dotaz 25 s; tohle je strop na síť
MAX_TRANSFER = 15 * 1024 * 1024
READ_LINES = 2000
READ_LINE_CHARS = 2000
READ_TEXT = 64 * 1024
LS_MAX = 500
FIND_MAX = 300
FIND_SCAN = 200_000
FIND_FILE = 5 * 1024 * 1024
RUN_DEFAULT = 120
RUN_MAX = 600
# Výstup příkazu: začátek a konec, prostředek se vynechá (jako Bash v Claude Code).
OUT_LIMITS = {"stdout": (8 * 1024, 24 * 1024), "stderr": (4 * 1024, 12 * 1024)}
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".cache",
             ".npm", ".gradle", ".Trash", "$RECYCLE.BIN", "System Volume Information"}

UKOLY_DIR = os.path.join(core.CLAUDE_DIR, "hub-ukoly")
UKOL_ID = re.compile(r"[a-f0-9]{16}")
UKOL_KEEP = 30 * 86400              # vyřízené úkoly se po měsíci uklidí
# Stav úkolu tady → stav, který se hlásí bráně.
UKOL_REPORT = {"ceka": "prevzato", "spusteno": "spusteno", "zahozeno": "zahozeno"}
_WIN_RESERVED = re.compile(r"(?i)(con|prn|aux|nul|com[0-9]|lpt[0-9])(\..*)?")

# Háčky, které nastaví server.py (tenhle modul o tabech ani oknech neví):
OPEN_TAB = None      # (title, path, prompt, ukol=) -> info otevřeného tabu
NOTIFY = None        # (zpráva) -> pošle ji všem otevřeným oknům
HOOK_URL = ""        # http://127.0.0.1:<port> hubu — tam se hlásí povoleni_hook.py

# Souhlas ze serveru v tabu s úkolem (povoleni_*): žádost, na kterou se hook
# tak dlouho neptal, už nikdo nečeká. Timeout hooku = jak dlouho může karta
# na serveru viset (Claude Code ho pak zruší bez rozhodnutí).
SESSION_SEEN = 90
HOOK_TIMEOUT = 24 * 3600
TAB_TOKENS = {}                     # žeton tabu -> {"tab", "ukol", "title"}
_ANSWERED = collections.OrderedDict()   # (id žádosti, žeton) -> odpověď pro hook

STATE = {"state": "off", "note": "", "since": 0.0}
RECENT = collections.deque(maxlen=12)
PENDING = {}                # id žádosti -> {"op", "summary", "preview", "at", "event", "volba"}
GRANT = {"until": 0.0}      # do kdy platí „povolit na 30 minut“
_WAKE = threading.Event()
_SLOTS = threading.BoundedSemaphore(6)
_LOCK = threading.Lock()
_STARTED = False
_TAKING = set()                     # úkoly na později, které se právě stahují


class TaskError(Exception):
    """Chyba úkolu — věta, kterou dostane Claude v prostoru."""


# ── nastavení ────────────────────────────────────────────────────────────────
def access_level():
    level = core.CONFIG.get("pocitac_access") or ""
    return level if level in LEVELS else ""


def device_id():
    """Stálé id tohohle počítače u brány (ne tajemství, jen rozlišení strojů)."""
    cid = core.CONFIG.get("pocitac_id") or ""
    if not re.fullmatch(r"[a-f0-9]{16}", cid):
        cid = secrets.token_hex(8)
        core.save_config({"pocitac_id": cid})
    return cid


def set_access(level):
    """Uloží volbu z nastavení. Vypnutí se bráně ohlásí hned, ne až po dotazu."""
    level = str(level or "")
    if level not in LEVELS:
        raise ValueError("Neznámá úroveň přístupu.")
    before = access_level()
    core.save_config({"pocitac_access": level, "pocitac_asked": True})
    if level != before:
        core.log(f"počítač: přístup pro Clauda ze serveru → {level or 'vypnuto'}")
        if not level:
            threading.Thread(target=_bye, daemon=True).start()
    wake()
    return status()


def wake():
    _WAKE.set()


def status():
    with _LOCK:
        recent = list(RECENT)[::-1]
    return {"access": access_level(), "asked": bool(core.CONFIG.get("pocitac_asked")),
            "state": STATE["state"], "note": STATE["note"],
            "server": account._host(account._base()) if account._base() else "",
            "name": _machine_name(), "recent": recent[:6], "ukoly": ukoly(),
            "pending": pending_public(), "grants": grants_public()}


def _set(state, note=""):
    if STATE["state"] != state or STATE["note"] != note:
        if state == "online" and STATE["state"] != "online":
            core.log(f"počítač: připojeno k {account._host(account._base())}")
        STATE.update(state=state, note=note, since=time.time())


def _machine_name():
    return (platform.node() or "počítač").split(".")[0][:60]


def _system_label():
    if core.IS_WINDOWS:
        return f"Windows {platform.release()}".strip()
    if core.IS_MAC:
        return f"macOS {platform.mac_ver()[0]}".strip()
    try:
        with open("/etc/os-release", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("PRETTY_NAME="):
                    return "Linux (" + line.split("=", 1)[1].strip().strip('"') + ")"
    except OSError:
        pass
    return "Linux"


def _shell_argv(command):
    """Čím příkaz spustit: bash (na Windows Git Bash), jinak cmd / sh."""
    if core.BASH:
        return [core.BASH, "-lc", command], "bash"
    if core.IS_WINDOWS:
        return [os.environ.get("COMSPEC") or "cmd.exe", "/d", "/s", "/c", command], "cmd"
    return ["/bin/sh", "-lc", command], "sh"


def describe(access):
    return {"id": device_id(), "name": _machine_name(), "system": _system_label(),
            "home": core.HOME, "user": _user_name(), "shell": _shell_argv("")[1],
            "access": access, "version": __version__,
            "pending": pending_public(), "grants": grants_public()}


def _user_name():
    try:
        return getpass.getuser()
    except Exception:
        return ""


# ── spojení s bránou ─────────────────────────────────────────────────────────
def start():
    """Spustí most, jednou za běh hubu. V prostoru na serveru jen napojí MCP."""
    global _STARTED
    if core.TEST_MODE:
        return                  # zkušební hub není tenhle počítač
    with _LOCK:
        if _STARTED:
            return
        _STARTED = True
    target = register_mcp if core.on_gateway() else _loop
    threading.Thread(target=target, daemon=True, name="pocitac").start()
    if not core.on_gateway():
        threading.Thread(target=register_server_mcp, daemon=True, name="pocitac-mcp").start()


def stop():
    """Appka končí — ať prostor hned ví, že počítač odešel."""
    if STATE["state"] == "online":
        _bye(timeout=3)


def _bye(timeout=8):
    token = core.CONFIG.get("gw_token") or ""
    if token and account._base():
        account._call("/gw/pocitac/poll", token=token, timeout=timeout,
                      payload={"bye": True, "computer": {"id": device_id()}})


def _supported():
    """Umí brána most? Starší na neznámou adresu odpoví 401 jako na špatný
    token — podle toho by se appka mylně odhlásila."""
    data, err, kind = account._call("/gw/info", timeout=10)
    if data is None:
        return None, err
    return "pocitac" in (data.get("features") or []), ""


def _loop():
    backoff = 0
    checked = ""                      # server, u kterého už víme, že most umí
    while True:
        # Probuzení (změna v nastavení) platí pro stav přečtený až po něm.
        _WAKE.clear()
        access = access_level()
        token = core.CONFIG.get("gw_token") or ""
        base = account._base()
        if not access:
            _set("off")
        elif not base or not token:
            _set("waiting", "Appka není přihlášená k serveru.")
        if not access or not base or not token:
            _WAKE.wait(60)
            backoff = 0
            continue

        if checked != base:
            ok, err = _supported()
            if ok is None:
                backoff = min(60, max(3, backoff * 2))
                _set("connecting", err)
                _WAKE.wait(backoff)
                continue
            if not ok:
                _set("unsupported", "Server tohle ještě neumí — potřebuje novější verzi "
                                    "Aladina (aktualizuje se sama v noci).")
                _WAKE.wait(15 * 60)
                continue
            checked = base

        data, err, kind = account._call("/gw/pocitac/poll", token=token, timeout=POLL_TIMEOUT,
                                        payload={"computer": describe(access)})
        if data is not None and isinstance(data.get("tasks"), list):
            _set("online")
            backoff = 0
            for task in data["tasks"]:
                if isinstance(task, dict):
                    threading.Thread(target=_handle, args=(task, token), daemon=True).start()
            for cmd in data.get("prikazy") or []:
                if isinstance(cmd, dict):
                    _directive(cmd)
            for offer in data.get("ukoly") or []:
                if isinstance(offer, dict):
                    _take_later(str(offer.get("id") or ""), token)
            if time.time() - _SYNC["at"] > 300:
                _SYNC["at"] = time.time()
                threading.Thread(target=_sync_reports, args=(token,), daemon=True).start()
            continue
        if kind == account.AUTH:
            _set("error", "Přihlášení k serveru vypršelo — přihlas se znovu (Nastavení → Účet).")
            _WAKE.wait(300)
            continue
        checked = ""                   # po výpadku se ověří znovu (třeba jiná verze)
        backoff = min(60, max(3, backoff * 2))
        _set("connecting", err or "Server neodpovídá.")
        _WAKE.wait(backoff)


def _handle(task, token):
    rid = str(task.get("id") or "")
    op = str(task.get("op") or "")
    args = task.get("args") if isinstance(task.get("args"), dict) else {}
    started = time.time()
    with _SLOTS:
        try:
            reply = {"ok": True, "result": execute(op, args, rid)}
        except TaskError as exc:
            reply = {"ok": False, "error": str(exc)}
        except Exception as exc:
            core.log_error(f"počítač: úkol {op} selhal", exc)
            reply = {"ok": False, "error": f"Na počítači se to nepovedlo: {exc}"}
    _remember(op, args, reply, started)
    payload = {"computer": device_id(), "id": rid, **reply}
    for attempt in range(3):
        data, _err, kind = account._call("/gw/pocitac/vysledek", token=token,
                                         payload=payload, timeout=120)
        if data is not None or kind == account.AUTH:
            return
        time.sleep(2 * (attempt + 1))


def _remember(op, args, reply, started):
    what = str(args.get("command") if op == "run" else
               args.get("target") if op == "open" else
               args.get("message") if op == "notify" else
               args.get("path") or ("" if op in ("screenshot", "ps", "clipboard_get", "clipboard_set") else "~"))
    if reply["ok"] and op == "run" and reply["result"].get("timed_out"):
        reply = {"ok": False, "error": "přerušeno po časovém limitu"}   # jen pro výpis
    entry = {"op": op, "what": what[:160], "at": started, "ok": reply["ok"],
             "error": "" if reply["ok"] else str(reply.get("error"))[:200],
             "seconds": round(time.time() - started, 1)}
    with _LOCK:
        RECENT.append(entry)
    core.log(f"počítač: {op} {what[:160]!r} — "
             + ("ok" if reply["ok"] else "chyba: " + entry["error"]),
             "info" if reply["ok"] else "warn")


# ── úkoly ────────────────────────────────────────────────────────────────────
def execute(op, args, rid=""):
    level = access_level()
    if op not in ALLOWED.get(level, ()):
        if not level:
            raise TaskError("Na tomhle počítači je přístup pro Clauda ze serveru vypnutý.")
        raise TaskError("Na tomhle počítači je povolené jen čtení — zapisovat, spouštět příkazy, "
                        "dívat se na obrazovku ani na schránku nejde. Uživatel to může změnit "
                        "v appce na počítači (Nastavení → Účet) nebo v prostoru na serveru "
                        "(Nastavení → Účet → Tvůj počítač); doporučená je volba „ptát se na souhlas“.")
    if level == "ptat" and op in ASK_OPS:
        _need_approval(op, args, rid)
    if op not in OPS:
        raise TaskError(f"Úkol {op!r} tahle verze appky neumí — aktualizuj Code Hub na počítači.")
    return OPS[op](args)


# ── souhlas člověka (úroveň „ptát se“) ───────────────────────────────────────
def _preview(op, args):
    if op == "run":
        cwd = str(args.get("cwd") or "").strip()
        return (str(args.get("command") or "")[:500] + (f"\n(ve složce {cwd})" if cwd else ""))
    if op == "write":
        return f"{args.get('path') or ''}\n({len(str(args.get('content') or ''))} znaků)"
    if op == "edit":
        return f"{args.get('path') or ''}\n− {str(args.get('old') or '')[:150]}\n+ {str(args.get('new') or '')[:150]}"
    if op == "upload":
        return f"{args.get('path') or ''} ({args.get('name') or 'soubor'})"
    if op == "open":
        return str(args.get("target") or "")
    if op == "session_send":
        return f"tab {args.get('tab')}: {str(args.get('text') or '')[:400]}"
    if op == "clipboard_set":
        return str(args.get("text") or "")[:300]
    return ""


_OP_TEXT = {"session_send": "napsat do běžící session", "run": "spustit příkaz", "write": "zapsat soubor", "edit": "upravit soubor",
            "upload": "nahrát soubor na počítač", "open": "otevřít",
            "screenshot": "udělat snímek obrazovky", "clipboard_get": "přečíst schránku",
            "clipboard_set": "zapsat do schránky"}


def pending_public():
    now = time.time()
    with _LOCK:
        # Žádost z tabu, na kterou se hook dlouho neptal (skončil, tab zavřený),
        # už nikdo nečeká — dialog v terminálu případně dál visí jen tady.
        for rid in [r for r, p in PENDING.items()
                    if p.get("kind") == "session" and now - p["seen"] > SESSION_SEEN]:
            PENDING.pop(rid)["event"].set()
        out = []
        for rid, p in PENDING.items():
            item = {"id": rid, "op": p["op"], "summary": p["summary"], "preview": p["preview"],
                    "at": int(p["at"])}
            if p.get("kind") == "session":
                item.update(kind="session", tool=p["tool"], tab=p["tab"], ukol=p["ukol"],
                            otazky=p["otazky"])
            out.append(item)
        return out


def grants_public():
    return [{"until": int(GRANT["until"])}] if GRANT["until"] > time.time() else []


def _push_pending():
    """Žádost se hned ohlásí bráně, ať ji člověk uvidí i v prostoru na serveru."""
    token = core.CONFIG.get("gw_token") or ""
    if not token or not account._base():
        return
    account._call("/gw/pocitac/schvaleni", token=token, timeout=10,
                  payload={"computer": device_id(), "pending": pending_public(),
                           "grants": grants_public()})


def _need_approval(op, args, rid):
    """Počká na souhlas člověka; jinak vyhodí TaskError s větou pro Clauda."""
    if GRANT["until"] > time.time():
        return
    key = rid or secrets.token_hex(8)
    summary = (str(args.get("command") or args.get("path") or args.get("target") or "")
               if op != "screenshot" else "snímek obrazovky")[:160] or _OP_TEXT.get(op, op)
    entry = {"op": op, "summary": summary, "preview": _preview(op, args)[:600],
             "at": time.time(), "event": threading.Event(), "volba": ""}
    with _LOCK:
        PENDING[key] = entry
    core.log(f"počítač: čeká na souhlas — {op} {summary!r}")
    if NOTIFY:
        try:
            NOTIFY({"t": "pocitac-schvaleni", "id": key, "op": op, "summary": summary})
        except Exception:
            pass
    threading.Thread(target=_push_pending, daemon=True).start()
    try:
        entry["event"].wait(APPROVE_WAIT)
    finally:
        with _LOCK:
            PENDING.pop(key, None)
        threading.Thread(target=_push_pending, daemon=True).start()
    if entry["volba"] in ("jednou", "30min"):
        return
    if entry["volba"] == "zamitnout":
        raise TaskError("Uživatel to nepovolil. Zeptej se ho, co by mu vyhovovalo, nebo to "
                        "udělej jinak.")
    raise TaskError(f"Uživatel to do {APPROVE_WAIT} s nepotvrdil (možná u počítače nesedí). "
                    "Řekni mu, že čekáš na jeho souhlas — ukáže se jako karta v aplikaci na počítači a "
                    "v prostoru v Nastavení → Účet → Tvůj počítač — a pak to zkus znovu.")


def decide(rid, choice, via="počítač", odpovedi=None):
    """Rozhodnutí o žádosti: z appky na počítači, nebo z prostoru přes bránu."""
    with _LOCK:
        entry = PENDING.get(str(rid or ""))
        if entry and entry.get("kind") == "session":
            # Žádost z tabu platí jednou: kdo ji vyřídí první, tomu patří.
            answer = _session_answer(entry, choice, odpovedi)
            PENDING.pop(str(rid))
    if entry and entry.get("kind") == "session":
        entry["reply"] = answer
        with _LOCK:
            # Hook může být zrovna mezi dvěma dotazy — odpověď si vyzvedne.
            _ANSWERED[(str(rid), entry["token"])] = answer
            while len(_ANSWERED) > 50:
                _ANSWERED.popitem(last=False)
        core.log(f"počítač: tab {entry['tab_id']} — {entry['op']} {entry['summary']!r}: "
                 f"{answer['behavior']} ({via})")
        entry["event"].set()
        threading.Thread(target=_push_pending, daemon=True).start()
        return status()
    if not entry:
        raise ValueError("Tuhle žádost už nikdo nečeká (vypršela nebo je vyřízená).")
    if choice not in DECISIONS:
        raise ValueError("Neznámá volba.")
    entry["volba"] = choice
    if choice == "30min":
        GRANT["until"] = time.time() + GRANT_SECONDS
    core.log(f"počítač: souhlas ({via}) — {entry['op']} {entry['summary']!r}: {choice}")
    entry["event"].set()
    return status()


def revoke_grant():
    GRANT["until"] = 0.0
    threading.Thread(target=_push_pending, daemon=True).start()
    return status()


def _directive(task):
    """Pokyn z brány: rozhodnutí o žádosti, nebo změna přístupu z prostoru."""
    kind = str(task.get("typ") or "")
    try:
        if kind == "rozhodnuti":
            decide(task.get("id"), str(task.get("volba") or ""), via="prostor",
                   odpovedi=task.get("odpovedi"))
        elif kind == "pristup":
            level = str(task.get("level") or "")
            before = access_level()
            set_access(level)
            if NOTIFY and level != before:
                NOTIFY({"t": "pocitac-pristup", "level": level})
    except ValueError as exc:
        core.log(f"počítač: pokyn {kind} zamítnut: {exc}", "warn")


def _path(raw):
    text = str(raw or "").strip()
    if "\0" in text:
        raise TaskError("Neplatná cesta.")
    if not text:
        return core.HOME
    if core.IS_WINDOWS:
        # Claude píše cesty i po bashovsku: /c/Users/… → C:\Users\…
        m = re.match(r"^/([A-Za-z])(/|$)", text)
        if m:
            text = m.group(1).upper() + ":\\" + text[3:]
    path = os.path.expanduser(text)
    if not os.path.isabs(path):
        path = os.path.join(core.HOME, path)
    return os.path.abspath(path)


# Soubory s přihlášením appky k serveru a Claude Code. S tokenem zařízení by si
# Claude v prostoru mohl sám potvrdit, co má potvrzovat člověk (nahrání do
# firemního Obsidianu), a s přihlášením Claude Code jet na cizí účet. Totéž
# další předplatná pro Claude Code na počítači (hub/predplatne.py).
def _guarded():
    dirs = {core.CLAUDE_DIR, os.path.join(core.HOME, ".claude")}
    return [os.path.join(d, name) for d in dirs
            for name in ("hub-config.json", "hub-config.json.tmp", ".credentials.json",
                         "hub-predplatna.json")]


def _guard(path):
    real = os.path.normcase(os.path.realpath(path))
    for secret in _guarded():
        if real == os.path.normcase(os.path.realpath(secret)):
            raise TaskError("Tenhle soubor drží přihlášení appky nebo Claude Code — Claude "
                            "ze serveru ho číst ani měnit nesmí.")


def _atomic_write(path, data):
    target = os.path.realpath(path)
    parent = os.path.dirname(target)
    os.makedirs(parent, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=parent, prefix="." + os.path.basename(target) + ".")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        if os.path.exists(target):
            try:
                shutil.copymode(target, tmp)
            except OSError:
                pass
        try:
            os.replace(tmp, target)
        except PermissionError:
            # Windows: soubor drží otevřený jiný program — zapsat aspoň přímo.
            with open(target, "wb") as fh:
                fh.write(data)
            os.remove(tmp)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def op_info(_args):
    return describe(access_level())


def op_ls(args):
    path = _path(args.get("path"))
    if not os.path.isdir(path):
        raise TaskError(f"Složka {path} na počítači není." +
                        (" Je to soubor — použij precist." if os.path.isfile(path) else ""))
    entries = []
    try:
        with os.scandir(path) as it:
            for e in it:
                try:
                    is_dir = e.is_dir()
                    st = e.stat() if not is_dir else None
                except OSError:
                    is_dir, st = False, None
                entries.append({"name": e.name, "type": "dir" if is_dir else "file",
                                "link": e.is_symlink(),
                                "size": st.st_size if st else None,
                                "mtime": int(st.st_mtime) if st else None})
    except PermissionError:
        raise TaskError(f"Do složky {path} nemá uživatel přístup.") from None
    entries.sort(key=lambda e: (e["type"] != "dir", e["name"].casefold()))
    return {"path": path, "entries": entries[:LS_MAX], "total": len(entries)}


def op_read(args):
    path = _path(args.get("path"))
    _guard(path)
    if os.path.isdir(path):
        raise TaskError(f"{path} je složka — použij slozka.")
    if not os.path.isfile(path):
        raise TaskError(f"Soubor {path} na počítači není.")
    try:
        start = max(1, int(args.get("offset") or 1))
        limit = max(1, min(10 * READ_LINES, int(args.get("limit") or READ_LINES)))
    except (TypeError, ValueError):
        raise TaskError("od a pocet musí být čísla.") from None
    lines, size, last, total, truncated = [], 0, start - 1, 0, False
    try:
        with open(path, "rb") as fh:
            if b"\0" in fh.read(8192):
                raise TaskError(f"{path} je binární soubor — zkopíruj ho na server přes stahnout.")
            fh.seek(0)
            for n, raw in enumerate(fh, 1):
                total = n
                if n < start or truncated:
                    if truncated and fh.tell() > 64 * 1024 * 1024:
                        total = None          # dál se nepočítá, soubor je obrovský
                        break
                    continue
                if n >= start + limit:
                    truncated = True
                    continue
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                if len(line) > READ_LINE_CHARS:
                    line = line[:READ_LINE_CHARS] + " …"
                row = f"{n:>6}\t{line}"
                size += len(row) + 1
                if size > READ_TEXT:
                    truncated = True
                    continue
                lines.append(row)
                last = n
    except PermissionError:
        raise TaskError(f"Soubor {path} nemá uživatel právo číst.") from None
    if total and start > total:
        raise TaskError(f"Soubor {path} má jen {total} řádků.")
    return {"path": path, "text": "\n".join(lines), "from": start, "to": last,
            "lines_total": total, "truncated": truncated}


def op_find(args):
    root = _path(args.get("path"))
    if not os.path.isdir(root):
        raise TaskError(f"Složka {root} na počítači není.")
    pattern = str(args.get("name") or "*").strip() or "*"
    text = str(args.get("text") or "")
    try:
        rx = re.compile(text, re.IGNORECASE if args.get("ignore_case") else 0) if text else None
    except re.error as exc:
        raise TaskError(f"Neplatný regulární výraz: {exc}") from None
    try:
        limit = max(1, min(FIND_MAX, int(args.get("limit") or 100)))
    except (TypeError, ValueError):
        limit = 100
    by_path = "/" in pattern or "\\" in pattern
    guarded = {os.path.normcase(os.path.realpath(p)) for p in _guarded()}
    matches, scanned, truncated = [], 0, False
    deadline = time.time() + 60

    def hit(rel, name):
        if by_path:
            return fnmatch.fnmatch(rel.replace("\\", "/"), pattern.replace("\\", "/"))
        return fnmatch.fnmatch(name, pattern)

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        if not rx:
            for d in dirnames:
                full = os.path.join(dirpath, d)
                if hit(os.path.relpath(full, root), d):
                    matches.append({"path": full, "type": "dir"})
        for name in sorted(filenames):
            scanned += 1
            if scanned > FIND_SCAN or time.time() > deadline or len(matches) >= limit:
                truncated = True
                break
            full = os.path.join(dirpath, name)
            if not hit(os.path.relpath(full, root), name):
                continue
            if not rx:
                matches.append({"path": full, "type": "file"})
                continue
            if os.path.normcase(os.path.realpath(full)) in guarded:
                continue
            try:
                if os.path.getsize(full) > FIND_FILE:
                    continue
                with open(full, "rb") as fh:
                    raw = fh.read()
            except OSError:
                continue
            if b"\0" in raw[:8192]:
                continue
            found = 0
            for n, line in enumerate(raw.decode("utf-8", "replace").splitlines(), 1):
                if rx.search(line):
                    matches.append({"path": full, "line": n, "text": line.strip()[:200]})
                    found += 1
                    if found >= 20 or len(matches) >= limit:
                        break
        if truncated:
            break
    return {"path": root, "matches": matches[:limit],
            "truncated": truncated or len(matches) > limit}


def op_download(args):
    path = _path(args.get("path"))
    _guard(path)
    if not os.path.isfile(path):
        raise TaskError(f"Soubor {path} na počítači není.")
    size = os.path.getsize(path)
    if size > MAX_TRANSFER:
        raise TaskError(f"Soubor má {size // (1024 * 1024)} MB, najednou jde nejvýš "
                        f"{MAX_TRANSFER // (1024 * 1024)} MB — rozděl nebo zabal ho.")
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except PermissionError:
        raise TaskError(f"Soubor {path} nemá uživatel právo číst.") from None
    return {"path": path, "size": len(data), "data": base64.b64encode(data).decode("ascii")}


def op_write(args):
    path = _path(args.get("path"))
    _guard(path)
    content = args.get("content")
    if not isinstance(content, str):
        raise TaskError("Chybí obsah souboru.")
    data = content.encode("utf-8")
    if len(data) > MAX_TRANSFER:
        raise TaskError("Obsah je moc velký.")
    if os.path.isdir(path):
        raise TaskError(f"{path} je složka.")
    existed = os.path.exists(path)
    _atomic_write(path, data)
    return {"path": path, "bytes": len(data), "created": not existed}


def op_edit(args):
    path = _path(args.get("path"))
    _guard(path)
    old, new = args.get("old"), args.get("new")
    if not isinstance(old, str) or not old or not isinstance(new, str):
        raise TaskError("Chybí text, který se má nahradit, nebo čím.")
    if old == new:
        raise TaskError("Starý a nový text jsou stejné.")
    if not os.path.isfile(path):
        raise TaskError(f"Soubor {path} na počítači není.")
    if os.path.getsize(path) > MAX_TRANSFER:
        raise TaskError("Soubor je na úpravu moc velký.")
    with open(path, "rb") as fh:
        text = fh.read().decode("utf-8", "surrogateescape")
    count = text.count(old)
    if not count and "\r\n" in text and "\n" in old:
        # Soubor z Windows: Claude posílá konce řádků jen \n.
        old, new = old.replace("\r\n", "\n").replace("\n", "\r\n"), \
            new.replace("\r\n", "\n").replace("\n", "\r\n")
        count = text.count(old)
    if not count:
        raise TaskError("Text se v souboru nenašel — přečti si ho znovu, mohl se změnit.")
    replace_all = args.get("all") is True
    if count > 1 and not replace_all:
        raise TaskError(f"Text je v souboru {count}× — přidej okolní řádky, ať je jednoznačný, "
                        "nebo nahraď všechny výskyty.")
    text = text.replace(old, new) if replace_all else text.replace(old, new, 1)
    _atomic_write(path, text.encode("utf-8", "surrogateescape"))
    return {"path": path, "replaced": count if replace_all else 1}


def op_upload(args):
    path = _path(args.get("path"))
    _guard(path)
    try:
        data = base64.b64decode(str(args.get("data") or ""), validate=True)
    except ValueError:
        raise TaskError("Poškozená data.") from None
    if len(data) > MAX_TRANSFER:
        raise TaskError("Soubor je moc velký.")
    if os.path.isdir(path):
        path = os.path.join(path, os.path.basename(str(args.get("name") or "")) or "soubor")
        _guard(path)
    existed = os.path.exists(path)
    _atomic_write(path, data)
    return {"path": path, "bytes": len(data), "created": not existed}


class _Capture:
    """Čte výstup příkazu na pozadí; drží začátek a konec, prostředek zahodí."""

    def __init__(self, stream, head, tail):
        self.stream, self.head_max, self.tail_max = stream, head, tail
        self.head, self.tail, self.total = bytearray(), bytearray(), 0
        self.thread = threading.Thread(target=self._read, daemon=True)
        self.thread.start()

    def _read(self):
        try:
            while True:
                chunk = self.stream.read1(65536) if hasattr(self.stream, "read1") \
                    else self.stream.read(65536)
                if not chunk:
                    break
                self.total += len(chunk)
                room = self.head_max - len(self.head)
                if room > 0:
                    self.head += chunk[:room]
                    chunk = chunk[room:]
                if chunk:
                    self.tail += chunk
                    if len(self.tail) > self.tail_max:
                        del self.tail[:len(self.tail) - self.tail_max]
        except (OSError, ValueError):
            pass

    def text(self):
        skipped = self.total - len(self.head) - len(self.tail)
        head = bytes(self.head).decode("utf-8", "replace")
        tail = bytes(self.tail).decode("utf-8", "replace")
        if skipped > 0:
            return f"{head}\n… (vynecháno {skipped} bajtů) …\n{tail}"
        return head + tail


def _kill_tree(proc):
    if core.IS_WINDOWS:
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                       capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(proc.pid, sig)
        except OSError:
            return
        try:
            proc.wait(3)
            return
        except subprocess.TimeoutExpired:
            continue


def op_run(args):
    command = str(args.get("command") or "").strip()
    if not command:
        raise TaskError("Chybí příkaz.")
    cwd = _path(args.get("cwd"))
    if not os.path.isdir(cwd):
        raise TaskError(f"Složka {cwd} na počítači není.")
    try:
        timeout = max(1, min(RUN_MAX, int(args.get("timeout") or RUN_DEFAULT)))
    except (TypeError, ValueError):
        timeout = RUN_DEFAULT
    argv, shell = _shell_argv(command)
    env = core.child_env()
    # Nikdo u terminálu nesedí: žádné barvy, stránkování ani dotazy na heslo.
    env.update(TERM="dumb", NO_COLOR="1", PAGER="cat", GIT_PAGER="cat",
               GIT_TERMINAL_PROMPT="0", HUB_POCITAC="1")
    env.pop("COLORTERM", None)
    kwargs = {}
    if core.IS_WINDOWS:
        kwargs["creationflags"] = (getattr(subprocess, "CREATE_NO_WINDOW", 0)
                                   | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    else:
        kwargs["start_new_session"] = True
    started = time.time()
    try:
        proc = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kwargs)
    except OSError as exc:
        raise TaskError(f"Příkaz nejde spustit: {exc}") from None
    out = _Capture(proc.stdout, *OUT_LIMITS["stdout"])
    err = _Capture(proc.stderr, *OUT_LIMITS["stderr"])
    timed_out = False
    try:
        proc.wait(timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_tree(proc)
    note = ""
    for cap in (out, err):
        cap.thread.join(5)
        if cap.thread.is_alive():
            # Proces na pozadí (server, `… &`) drží výstup otevřený dál.
            note = "(Příkaz nechal běžet proces na pozadí; jeho další výstup se už nečte.)"
    return {"exit_code": proc.returncode, "timed_out": timed_out, "shell": shell, "cwd": cwd,
            "seconds": round(time.time() - started, 1), "stdout": out.text(),
            "stderr": err.text(), "note": note}


# ── další nástroje: procesy, oznámení, obrazovka, schránka, otevření ──────────
def _cmd(argv, timeout=15, data=None):
    """Spustí pomocný příkaz systému. (kód, stdout bajty, stderr text)."""
    kwargs = {}
    if core.IS_WINDOWS:
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        r = subprocess.run(argv, input=data, capture_output=True, timeout=timeout, **kwargs)
    except FileNotFoundError:
        return 127, b"", "nenalezeno"
    except subprocess.TimeoutExpired:
        return 124, b"", "vypršel časový limit"
    return r.returncode, r.stdout, r.stderr.decode("utf-8", "replace").strip()


def op_ps(args):
    flt = str(args.get("name") or "").strip().lower()
    if core.IS_WINDOWS:
        code, out, err = _cmd(["tasklist", "/FO", "CSV", "/NH"])
        rows = [ln for ln in out.decode("cp852", "replace").splitlines() if ln.strip()]
    else:
        argv = (["ps", "-Ao", "pid,etime,pcpu,pmem,comm", "-r"] if core.IS_MAC
                else ["ps", "-eo", "pid,etime,pcpu,pmem,comm", "--sort=-pcpu"])
        code, out, err = _cmd(argv)
        rows = out.decode("utf-8", "replace").splitlines()
    if code != 0 and not rows:
        raise TaskError("Seznam procesů se nepodařilo získat" + (f": {err}" if err else "."))
    head, rows = (rows[:1], rows[1:]) if not core.IS_WINDOWS else ([], rows)
    if flt:
        rows = [r for r in rows if flt in r.lower()]
    try:
        limit = max(1, min(200, int(args.get("limit") or 60)))
    except (TypeError, ValueError):
        limit = 60
    return {"text": "\n".join(head + rows[:limit]), "total": len(rows), "truncated": len(rows) > limit}


def op_notify(args):
    msg = " ".join(str(args.get("message") or "").split())[:300]
    title = " ".join(str(args.get("title") or "Claude").split())[:80]
    if not msg:
        raise TaskError("Chybí text oznámení.")
    _notify({"t": "pocitac-oznameni", "title": title, "message": msg})      # v okně appky
    if core.IS_MAC:
        esc = lambda t: t.replace("\\", "\\\\").replace('"', '\\"')
        _cmd(["osascript", "-e", f'display notification "{esc(msg)}" with title "{esc(title)}"'])
    elif core.IS_WINDOWS:
        pass                                  # okno appky stačí; toast bez doplňků nejde
    else:
        _cmd(["notify-send", title, msg])
    return {"shown": True}


def _screen_png():
    """PNG obrazovky do dočasného souboru → bajty. Zkouší nástroje podle systému."""
    path = os.path.join(tempfile.gettempdir(), f"hub-snimek-{secrets.token_hex(4)}.png")
    try:
        if core.IS_MAC:
            tries = [["screencapture", "-x", path]]
        elif core.IS_WINDOWS:
            ps = ("Add-Type -AssemblyName System.Windows.Forms,System.Drawing;"
                  "$b=[System.Windows.Forms.SystemInformation]::VirtualScreen;"
                  "$bmp=New-Object System.Drawing.Bitmap $b.Width,$b.Height;"
                  "$g=[System.Drawing.Graphics]::FromImage($bmp);"
                  "$g.CopyFromScreen($b.Location,[System.Drawing.Point]::Empty,$b.Size);"
                  f"$bmp.Save('{path}',[System.Drawing.Imaging.ImageFormat]::Png)")
            tries = [["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps]]
        else:
            tries = [["grim", path], ["gnome-screenshot", "-f", path], ["spectacle", "-b", "-n", "-o", path],
                     ["maim", path], ["scrot", "-o", path], ["import", "-window", "root", path]]
            if "gnome" in (os.environ.get("XDG_CURRENT_DESKTOP") or "").lower():
                tries.insert(0, tries.pop(1))      # GNOME nemá wlr-screencopy, grim by tam jen selhal
        last = "žádný nástroj na snímek obrazovky nenalezen"
        for argv in tries:
            code, _out, err = _cmd(argv, timeout=20)
            if code == 0 and os.path.isfile(path) and os.path.getsize(path) > 100:
                with open(path, "rb") as fh:
                    return fh.read()
            if code != 127:
                last = err or last
        raise TaskError("Snímek obrazovky na tomhle počítači nejde udělat: " + last +
                        (" (na Linuxu je potřeba grim, gnome-screenshot, spectacle, maim, scrot "
                         "nebo imagemagick)" if not (core.IS_MAC or core.IS_WINDOWS) else ""))
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def op_screenshot(_args):
    png = _screen_png()
    if len(png) > MAX_TRANSFER:
        raise TaskError("Snímek je moc velký (přes 15 MB).")
    return {"mime": "image/png", "bytes": len(png), "data": base64.b64encode(png).decode("ascii")}


def op_clipboard_get(_args):
    if core.IS_MAC:
        argvs = [["pbpaste"]]
    elif core.IS_WINDOWS:
        argvs = [["powershell", "-NoProfile", "-Command", "Get-Clipboard -Raw"]]
    else:
        argvs = [["wl-paste", "--no-newline"], ["xclip", "-selection", "clipboard", "-o"],
                 ["xsel", "-bo"]]
    for argv in argvs:
        code, out, err = _cmd(argv)
        if code == 0:
            text = out.decode("utf-8", "replace")
            return {"text": text[:READ_TEXT], "truncated": len(text) > READ_TEXT}
    raise TaskError("Schránku se nepodařilo přečíst" + (" (na Linuxu je potřeba wl-paste, xclip "
                    "nebo xsel)." if not (core.IS_MAC or core.IS_WINDOWS) else "."))


def op_clipboard_set(args):
    text = str(args.get("text") if args.get("text") is not None else "")
    if len(text) > READ_TEXT:
        raise TaskError("Text do schránky je moc dlouhý.")
    data = text.encode("utf-8")
    if core.IS_MAC:
        argvs = [["pbcopy"]]
    elif core.IS_WINDOWS:
        argvs = [["clip"]]
        data = text.encode("utf-16-le")
    else:
        argvs = [["wl-copy"], ["xclip", "-selection", "clipboard"], ["xsel", "-bi"]]
    for argv in argvs:
        code, _out, _err = _cmd(argv, data=data)
        if code == 0:
            return {"chars": len(text)}
    raise TaskError("Do schránky se nepodařilo zapsat.")


def op_open(args):
    target = str(args.get("target") or "").strip()
    if not target:
        raise TaskError("Chybí, co otevřít.")
    if re.match(r"^https?://", target, re.I):
        what = target
    else:
        if re.match(r"^[a-z][a-z0-9+.-]*:", target, re.I) and not re.match(r"^[a-z]:[\\/]", target, re.I):
            raise TaskError("Otevřít jde jen adresa http(s) nebo soubor či složka na počítači.")
        what = _path(target)
        _guard(what)
        if not os.path.exists(what):
            raise TaskError(f"{what} na počítači není.")
    if core.IS_WINDOWS:
        try:
            os.startfile(what)               # noqa: S606 — soubor vybral člověk souhlasem
        except OSError as exc:
            raise TaskError(f"Nejde otevřít: {exc}") from None
    else:
        code, _o, err = _cmd(["open" if core.IS_MAC else "xdg-open", what], timeout=10)
        if code not in (0,):
            raise TaskError("Nejde otevřít" + (f": {err}" if err else "."))
    return {"opened": what}


def _tab_session(args):
    from . import server
    try:
        sid = int(args.get("tab"))
    except (TypeError, ValueError):
        raise TaskError("Chybí číslo tabu (z nástroje `sessions`).") from None
    session = server.HUB.sessions.get(sid)
    if not session:
        raise TaskError(f"Tab {sid} na počítači není otevřený.")
    return session


def op_sessions(_args):
    """Otevřené taby s Claude Code: číslo, název, složka, jestli právě pracuje."""
    from . import mcp_tools, server
    out = []
    for s in list(server.HUB.sessions.values()):
        if not (s.kind == "project" or s.kind.startswith("slash:")):
            continue
        path = ""
        try:
            path = core.transcript_for(s)
        except Exception:
            pass
        out.append({"tab": s.id, "title": s.title, "path": s.path, "agent": s.agent or "claude",
                    "exited": s.exited, "chat": getattr(s, "chat_id", "") or s.resume or "",
                    "working": bool(path and not s.exited and mcp_tools._pracuje(path)),
                    "started": int(s.started)})
    return {"sessions": out}


def op_session_read(args):
    """Poslední zprávy konverzace v tabu (od konce), nebo dál od offsetu `from`."""
    from . import cteni, mcp_tools
    session = _tab_session(args)
    path = core.transcript_for(session)
    if not path or not os.path.isfile(path):
        raise TaskError("Tab zatím nemá přepis konverzace (Claude ještě nic nezapsal).")
    try:
        start = max(0, int(args.get("from") or 0))
    except (TypeError, ValueError):
        start = 0
    try:
        last = max(1, min(int(args.get("last") or 30), 200))
    except (TypeError, ValueError):
        last = 30
    data = cteni.read(path, start, tail=not start)
    lines = []
    for b in (data.get("blocks") or [])[-last:]:
        kind = b.get("kind")
        if kind == "me":
            lines.append("## Uživatel\n" + (b.get("text") or ""))
        elif kind == "say":
            lines.append("## Claude\n" + (b.get("text") or ""))
        elif kind == "tool":
            lines.append(f"- nástroj {b.get('name', '')}: {(b.get('title') or '')[:200]}")
    text = "\n\n".join(lines)
    if len(text) > 100 * 1024:
        text = "… (začátek vynechán)\n\n" + text[-100 * 1024:]
    return {"tab": session.id, "title": session.title, "text": text, "next": data.get("next", 0),
            "working": mcp_tools._pracuje(path) and not session.exited, "exited": session.exited}


def op_session_send(args):
    """Napíše zprávu do běžícího tabu jako člověk (Enter až po chvíli, jako bublina)."""
    session = _tab_session(args)
    if session.exited:
        raise TaskError("Claude v tabu už neběží.")
    if not (session.kind == "project" or session.kind.startswith("slash:")):
        raise TaskError("Psát jde jen do tabů s Claude Code.")
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", str(args.get("text") or "")).strip()
    if not text:
        raise TaskError("Zpráva je prázdná.")
    if len(text) > 20000:
        raise TaskError("Zpráva je moc dlouhá.")
    body = "[ze serveru] " + text
    data = ("\x1b[200~" + body + "\x1b[201~") if "\n" in body else body
    session.write(data.encode("utf-8"))      # uspaný tab se tím probudí
    threading.Timer(0.25, lambda: None if session.exited else session.write(b"\r")).start()
    return {"sent": True, "tab": session.id}


OPS = {"sessions": op_sessions, "session_read": op_session_read, "session_send": op_session_send,
       "info": op_info, "ls": op_ls, "read": op_read, "find": op_find,
       "download": op_download, "write": op_write, "edit": op_edit,
       "upload": op_upload, "run": op_run, "ps": op_ps, "notify": op_notify,
       "screenshot": op_screenshot, "clipboard_get": op_clipboard_get,
       "clipboard_set": op_clipboard_set, "open": op_open}


# ── úkoly na později ─────────────────────────────────────────────────────────
_SYNC = {"at": 0.0}


def _take_later(tid, token):
    if not UKOL_ID.fullmatch(tid):
        return
    with _LOCK:
        if tid in _TAKING:
            return
        _TAKING.add(tid)
    threading.Thread(target=_fetch_ukol, args=(tid, token), daemon=True).start()


def _fetch_ukol(tid, token):
    """Stáhne úkol, uloží ho, potvrdí bráně a spustí (plný přístup) nebo ohlásí."""
    try:
        meta = _read_ukol(tid)
        if meta is None:
            # Přílohy po jedné (`bez_dat`): chat ze serveru má i stovky MB.
            # Starší brána `bez_dat` nezná a pošle je rovnou v odpovědi.
            data, err, _kind = account._call(
                "/gw/pocitac/ukol", token=token, timeout=180,
                payload={"action": "take", "computer": device_id(), "id": tid, "bez_dat": True})
            if data is None or not data.get("ok"):
                if not (data or {}).get("gone"):
                    core.log(f"počítač: úkol ze serveru se nepodařilo stáhnout — "
                             f"{(data or {}).get('error') or err}", "warn")
                return

            def stahni(name):
                res, err2, _k = account._call(
                    "/gw/pocitac/ukol", token=token, timeout=300,
                    payload={"action": "soubor", "computer": device_id(), "id": tid, "name": name})
                if res is None or not res.get("ok"):
                    raise ValueError(f"příloha {name} se nestáhla — {(res or {}).get('error') or err2}")
                return base64.b64decode(str(res.get("data") or ""), validate=True)
            try:
                meta = _store_ukol(data.get("ukol") or {}, stahni)
            except (OSError, ValueError) as exc:
                core.log_error("počítač: úkol ze serveru se nepodařilo uložit", exc)
                return
            core.log(f"počítač: převzat úkol ze serveru „{meta['title']}\"")
            if meta.get("kind") == "chat":
                _chat_ze_serveru(meta, token)
                return
            # Plný přístup i „ptát se“ spustí úkol sám: při „ptát se“ běží tab
            # v režimu, kdy se Claude Code na zápis a příkazy ptá, a žádosti
            # jdou i na server (tab_launch). Jen čtení čeká na člověka tady.
            if meta["state"] == "ceka" and access_level() in ("vse", "ptat") and OPEN_TAB:
                try:
                    start_ukol(tid, auto=True)
                    return
                except ValueError as exc:
                    core.log(f"počítač: úkol „{meta['title']}\" se nespustil sám — {exc}", "warn")
            _notify({"t": "pocitac-ukol", "id": tid, "title": meta["title"],
                     "state": meta["state"]})
        _report(meta, token)
    finally:
        with _LOCK:
            _TAKING.discard(tid)


def _file_name(raw):
    """Jméno přílohy bez cesty a znaků, které Windows v názvu nesnese."""
    name = str(raw or "").replace("\\", "/").rsplit("/", 1)[-1]
    name = re.sub(r'[\x00-\x1f<>:"|?*]', "_", name).strip()[:120].rstrip(". ")
    if name in ("", ".", ".."):
        return ""
    return "_" + name if _WIN_RESERVED.fullmatch(name) else name


def _ukol_dir(tid):
    return os.path.join(UKOLY_DIR, tid)


def _read_ukol(tid):
    try:
        with open(os.path.join(_ukol_dir(tid), "ukol.json"), encoding="utf-8") as fh:
            meta = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(meta, dict) or meta.get("id") != tid or meta.get("state") not in UKOL_REPORT:
        return None
    return meta


def _write_ukol(meta):
    _atomic_write(os.path.join(_ukol_dir(meta["id"]), "ukol.json"),
                  json.dumps(meta, ensure_ascii=False, indent=1).encode("utf-8"))


def _store_ukol(ukol, stahni=None):
    tid = str(ukol.get("id") or "")
    if not UKOL_ID.fullmatch(tid):
        raise ValueError("úkol bez platného id")
    title = " ".join(str(ukol.get("title") or "").split())[:120] or "Úkol ze serveru"
    names = []
    for item in ukol.get("files") or []:
        name = _file_name(item.get("name")) if isinstance(item, dict) else ""
        if not name or name.casefold() in (n.casefold() for n in names):
            continue
        if "data" in item or stahni is None:
            data = base64.b64decode(str(item.get("data") or ""), validate=True)
        else:
            data = stahni(item.get("name"))
        _atomic_write(os.path.join(_ukol_dir(tid), "soubory", name), data)
        names.append(name)
    meta = {"id": tid, "title": title, "text": str(ukol.get("text") or ""),
            "kind": "chat" if ukol.get("kind") == "chat" else "",
            "folder": str(ukol.get("folder") or "")[:500], "files": names,
            "created": int(ukol.get("created") or 0), "received": int(time.time()),
            "server": account._host(account._base()) if account._base() else "",
            "state": "ceka", "changed": int(time.time()), "reported": ""}
    _write_ukol(meta)
    return meta


def _report(meta, token=""):
    """Řekne bráně, kde úkol je. Když se to nepovede, zkusí to smyčka později."""
    want = UKOL_REPORT.get(meta.get("state"))
    token = token or core.CONFIG.get("gw_token") or ""
    if not want or meta.get("reported") == want or not token or not account._base():
        return
    data, _err, _kind = account._call(
        "/gw/pocitac/ukol", token=token, timeout=20,
        payload={"action": "state", "computer": device_id(), "id": meta["id"], "state": want})
    if data is not None and (data.get("ok") or data.get("gone")):
        with _LOCK:
            fresh = _read_ukol(meta["id"])
            if fresh and fresh["state"] == meta["state"]:
                fresh["reported"] = want
                _write_ukol(fresh)


def _sync_reports(token):
    for meta in ukoly():
        if UKOL_REPORT[meta["state"]] != meta.get("reported"):
            _report(meta, token)


def _notify(message):
    if NOTIFY:
        try:
            NOTIFY(message)
        except Exception:
            pass


def ukoly():
    """Úkoly ze serveru na tomhle počítači, nejnovější první. Staré uklidí."""
    try:
        names = os.listdir(UKOLY_DIR)
    except OSError:
        return []
    out, now = [], time.time()
    for name in names:
        meta = _read_ukol(name) if UKOL_ID.fullmatch(name) else None
        if meta is None:
            continue
        if meta["state"] != "ceka" and now - meta.get("changed", 0) > UKOL_KEEP:
            shutil.rmtree(_ukol_dir(name), ignore_errors=True)
            continue
        out.append(meta)
    return sorted(out, key=lambda m: -m.get("received", 0))


def _set_state(tid, state, expect="ceka"):
    with _LOCK:
        meta = _read_ukol(str(tid or "")) if UKOL_ID.fullmatch(str(tid or "")) else None
        if meta is None:
            raise ValueError("Takový úkol tu není.")
        if meta["state"] != expect:
            raise ValueError("Úkol už je spuštěný nebo zahozený.")
        meta.update(state=state, changed=int(time.time()))
        _write_ukol(meta)
    return meta


def _chat_ze_serveru(meta, token=""):
    """Chat, který člověk poslal ze serveru (hub/prenos.py). S plným přístupem
    se přijme rovnou, jinak čeká na jeho souhlas v appce — zapisuje se do
    složky projektů na tomhle počítači."""
    if access_level() == "vse":
        try:
            prijmi_chat(meta["id"])
            return
        except ValueError as exc:
            core.log(f"počítač: chat ze serveru se nepřijal — {exc}", "warn")
    _notify({"t": "prenos-ceka", "id": meta["id"], "title": meta["title"]})
    _report(meta, token)


def prijmi_chat(tid):
    """Rozbalí chat ze serveru do složky projektů (v domovské složce počítače)."""
    from . import prenos
    meta = _read_ukol(str(tid or "")) if UKOL_ID.fullmatch(str(tid or "")) else None
    if meta is None or meta.get("kind") != "chat":
        raise ValueError("Takový chat tu nečeká.")
    folder = os.path.join(_ukol_dir(meta["id"]), "soubory")
    balik = os.path.join(_ukol_dir(meta["id"]), "chat.zip")
    try:
        names = sorted(os.listdir(folder))
        # Kusy se skládají na disku, ne v paměti — balíček má i stovky MB.
        prenos.sloz([os.path.join(folder, name) for name in names], balik)
    except OSError:
        raise ValueError("Balíček chatu se nenašel.") from None
    try:
        chat = prenos.rozbal_na_disk(balik, core.CLAUDE_DIR, core.HOME)
    finally:
        try:
            os.remove(balik)
        except OSError:
            pass
    _set_state(meta["id"], "spusteno")
    shutil.rmtree(folder, ignore_errors=True)
    core.log(f"počítač: chat „{chat['title']}\" ze serveru je tu")
    _notify({"t": "prenos-prijat", "chat": chat["id"], "title": chat["title"],
             "from": meta.get("server", "")})
    threading.Thread(target=_report, args=(_read_ukol(meta["id"]) or meta,), daemon=True).start()
    return {"ok": True, "chat": chat["id"], "title": chat["title"]}


def start_ukol(tid, auto=False):
    """Otevře tab s Claude Code, který úkol dodělá."""
    if not OPEN_TAB:
        raise ValueError("Úkol jde spustit jen v appce na počítači.")
    pred = _read_ukol(str(tid or "")) if UKOL_ID.fullmatch(str(tid or "")) else None
    if pred and pred.get("kind") == "chat":
        raise ValueError("Tohle je chat ze serveru — přijmi ho tlačítkem „Přijmout a otevřít“.")
    meta = _set_state(tid, "spusteno")
    note = ""
    path = _path(meta["folder"]) if meta["folder"] else core.HOME
    if not os.path.isdir(path):
        note = (f"Složka {meta['folder']} na tomhle počítači není, tab je proto otevřený "
                f"v domovské složce {core.HOME}.")
        path = core.HOME
    try:
        tab = OPEN_TAB(meta["title"][:40], path, ukol_prompt(meta, note), ukol=meta["id"])
    except Exception as exc:
        _set_state(tid, "ceka", expect="spusteno")
        raise ValueError(f"Tab se nepodařilo otevřít: {exc}") from None
    with _LOCK:
        fresh = _read_ukol(meta["id"])
        if fresh:
            fresh["tab"] = tab.get("id")         # okno se na něj po návratu přepne
            _write_ukol(fresh)
    core.log(f"počítač: úkol ze serveru „{meta['title']}\" spuštěn v tabu"
             + (" (sám, plný přístup)" if auto else ""))
    _notify({"t": "pocitac-ukol", "id": meta["id"], "title": meta["title"],
             "state": "spusteno", "tab": tab.get("id"), "auto": auto})
    threading.Thread(target=_report, args=(meta,), daemon=True).start()
    return {"ok": True, "tab": tab}


def dismiss_ukol(tid):
    meta = _set_state(tid, "zahozeno")
    shutil.rmtree(os.path.join(_ukol_dir(meta["id"]), "soubory"), ignore_errors=True)
    core.log(f"počítač: úkol ze serveru „{meta['title']}\" zahozen")
    threading.Thread(target=_report, args=(meta,), daemon=True).start()
    return {"ok": True}


def ukol_prompt(meta, note=""):
    """Úvodní zpráva pro Clauda v tabu — tuhle konverzaci ze serveru nezná."""
    when = time.strftime("%d. %m. %Y %H:%M", time.localtime(meta.get("created") or time.time()))
    lines = [f"Claude v prostoru na serveru {meta.get('server') or ''} ti nechal úkol pro "
             f"tenhle počítač (zadáno {when}). Konverzaci ze serveru nevidíš — řiď se "
             "zadáním níž.", "", f"# {meta['title']}", "", meta["text"].strip()]
    if meta["files"]:
        lines += ["", "Přílohy ze serveru jsou ve složce "
                  + os.path.join(_ukol_dir(meta["id"]), "soubory") + ":"]
        lines += [f"- {name}" for name in meta["files"]]
    if note:
        lines += ["", note]
    lines += ["", "Než začneš, v pár větách shrň, co uděláš. Když zadání nesedí na to, co "
              "tady najdeš, nebo by šlo o mazání či přepsání něčeho, co zadání výslovně "
              "nezmiňuje, zeptej se uživatele."]
    return "\n".join(lines)


# ── souhlas ze serveru v tabu s úkolem ze serveru ────────────────────────────
# Tab, který hub otevřel kvůli úkolu ze serveru, dostane hook PermissionRequest
# (tools/povoleni_hook.py). Když se Claude v něm chystá zeptat — oprávnění
# k nástroji, otázka, schválení plánu —, hook to ohlásí sem (`/povoleni`) a hub
# žádost pošle bráně jako ostatní žádosti o souhlas: člověk ji v prostoru na
# serveru uvidí jako kartu. Dialog v terminálu přitom visí taky a platí první
# odpověď. Odpověď v terminálu se pozná z přepisu konverzace (tool_result
# k témuž volání) a karta na serveru zmizí. Ručně otevřené taby nic z toho
# nemají — žeton i hook dostane jen tab s úkolem.
def ukol_for_tab(ukol="", resume=""):
    """Úkol ze serveru, ke kterému tab patří: rovnou (start_ukol), nebo podle
    konverzace, ve které se pokračuje (restart hubu, seznam konverzací)."""
    ukol, resume = str(ukol or ""), str(resume or "")
    if ukol:
        meta = _read_ukol(ukol) if UKOL_ID.fullmatch(ukol) else None
        return meta if meta and meta.get("kind") != "chat" else None
    if resume:
        for meta in ukoly():
            if meta.get("chat") == resume and meta["state"] == "spusteno":
                return meta
    return None


def _hook_settings():
    """Soubor s hookem pro `claude --settings` (jen pro taby s úkolem)."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    script = os.path.join(root, "tools", "povoleni_hook.py")
    if not os.path.isfile(script):
        return ""
    python = sys.executable or "python3"
    if core.IS_WINDOWS:
        python, script = python.replace("\\", "/"), script.replace("\\", "/")
    # Timeout hooku = jak dlouho může karta na serveru čekat. Po něm Claude
    # Code hook zruší bez rozhodnutí a dialog čeká jen na počítači.
    body = json.dumps({"hooks": {"PermissionRequest": [{"hooks": [
        {"type": "command", "command": f'"{python}" "{script}"', "timeout": HOOK_TIMEOUT}]}]}},
        ensure_ascii=False, indent=1)
    path = os.path.join(core.CLAUDE_DIR, "hub-povoleni-ze-serveru.json")
    try:
        with open(path, encoding="utf-8") as fh:
            same = fh.read() == body
    except OSError:
        same = False
    if not same:
        _atomic_write(path, body.encode("utf-8"))
    return path


def tab_launch(meta):
    """Co tab s úkolem ze serveru dostane navíc. Plný přístup = jako každý tab
    tady (režim oprávnění podle nastavení Claude Code, bypass v nabídce). Jinak
    tab startuje v režimu `default` a bez bypassu: zápis i příkazy se ptají a
    žádost jde i na server — jedno Shift+Tab by souhlas jinak obešlo."""
    full = access_level() == "vse"
    args, env = [], {}
    settings = _hook_settings() if HOOK_URL else ""
    token = secrets.token_urlsafe(24)
    # Tvar `--x=hodnota`: agent-wrapper.sh po pádu (SIGKILL) pokračuje
    # s přepínači bez hodnot — samostatná hodnota by se mu ztratila.
    if settings:
        args.append("--settings=" + settings)
        env = {"HUB_POVOLENI_URL": HOOK_URL, "HUB_POVOLENI_TOKEN": token}
    if not full:
        args.append("--permission-mode=default")
    return {"args": args, "env": env, "bypass": full, "token": token, "ukol": meta["id"]}


def bind_tab(launch, session, meta):
    """Žeton z `tab_launch` patří tomuhle tabu. Úkol si pamatuje konverzaci,
    ať tab po restartu hubu zůstane tabem s úkolem."""
    session.ukol = meta["id"]
    with _LOCK:
        TAB_TOKENS[launch["token"]] = {"tab": session.id, "ukol": meta["id"],
                                       "title": meta["title"]}
        chat = getattr(session, "chat_id", "") or ""
        fresh = _read_ukol(meta["id"])
        if chat and fresh and fresh.get("chat") != chat:
            fresh["chat"] = chat
            _write_ukol(fresh)


def _tab_for_token(token):
    from . import server
    token = str(token or "")
    with _LOCK:
        hit = next((b for t, b in TAB_TOKENS.items()
                    if token and secrets.compare_digest(t, token)), None)
    session = server.HUB.sessions.get(hit["tab"]) if hit else None
    if not session or session.exited:
        return None, None
    return hit, session


def _cut(text, limit):
    text = str(text or "")
    return text if len(text) <= limit else text[:limit] + "…"


def _describe_request(tool, inp):
    """(op, krátký popis, náhled, otázky) pro kartu na serveru."""
    if tool == "AskUserQuestion":
        otazky = []
        for q in (inp.get("questions") or [])[:4]:
            if not isinstance(q, dict):
                continue
            opts = [{"label": _cut(o.get("label"), 120), "description": _cut(o.get("description"), 300)}
                    for o in (q.get("options") or [])[:6] if isinstance(o, dict) and o.get("label")]
            otazky.append({"question": _cut(q.get("question"), 500), "header": _cut(q.get("header"), 40),
                           "multi": q.get("multiSelect") is True, "options": opts})
        first = otazky[0]["question"] if otazky else "otázka"
        return "otazka", _cut(first, 160), "", otazky
    if tool == "ExitPlanMode":
        return "plan", "schválit plán", _cut(inp.get("plan"), 3000), []
    if tool == "Bash":
        cmd = str(inp.get("command") or "")
        note = str(inp.get("description") or "")
        return "povoleni", _cut(cmd, 160), _cut(cmd, 1500) + (f"\n({_cut(note, 200)})" if note else ""), []
    path = str(inp.get("file_path") or inp.get("notebook_path") or inp.get("path") or "")
    if tool == "Write":
        return "povoleni", _cut(path, 160), f"{path}\n({len(str(inp.get('content') or ''))} znaků)", []
    if tool == "Edit":
        return "povoleni", _cut(path, 160), (f"{path}\n− {_cut(inp.get('old_string'), 300)}"
                                             f"\n+ {_cut(inp.get('new_string'), 300)}"), []
    if path:
        return "povoleni", _cut(path, 160), path, []
    url = str(inp.get("url") or inp.get("query") or "")
    if url:
        return "povoleni", _cut(url, 160), url, []
    raw = json.dumps(inp, ensure_ascii=False) if inp else ""
    return "povoleni", _cut(tool, 160), _cut(raw, 600), []


def _transcript_tail(path, limit=512 * 1024):
    """Konec přepisu jako JSON záznamy a velikost souboru."""
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - limit))
            data = fh.read()
    except OSError:
        return [], 0
    out = []
    for line in data.splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict):
            out.append(entry)
    return out, size


def _content(entry):
    msg = entry.get("message") if isinstance(entry.get("message"), dict) else {}
    content = msg.get("content")
    return [c for c in content if isinstance(c, dict)] if isinstance(content, list) else []


def _find_tool_use(path, tool, inp):
    """Id volání nástroje, na které se Claude Code ptá — poslední tool_use
    se stejným jménem a vstupem, ke kterému ještě není tool_result."""
    entries, size = _transcript_tail(path)
    done = {c.get("tool_use_id") for e in entries for c in _content(e) if c.get("type") == "tool_result"}
    found = ""
    for e in entries:
        if e.get("type") != "assistant":
            continue
        for c in _content(e):
            if c.get("type") == "tool_use" and c.get("name") == tool and c.get("input") == inp \
                    and c.get("id") not in done:
                found = str(c.get("id") or "")
    return found, size


def _resolved_elsewhere(entry, session):
    """Vyřídil to někdo v terminálu (nebo tab skončil)? Pozná se podle
    tool_result k volání, na které se Claude Code ptal. Volání se do přepisu
    může zapsat až chvíli po hooku — hledá se proto i v nových řádcích."""
    if session is None or session.exited:
        return True
    path = entry["transcript"]
    if not path:
        return False
    try:
        with open(path, "rb") as fh:
            fh.seek(entry["offset"])
            data = fh.read(4 * 1024 * 1024)
    except OSError:
        return False
    end = data.rfind(b"\n")
    if end >= 0:
        entry["offset"] += end + 1
    for line in (data[:end].splitlines() if end >= 0 else []):
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if not isinstance(e, dict):
            continue
        for c in _content(e):
            if not entry["use_id"] and c.get("type") == "tool_use" and \
                    c.get("name") == entry["tool_name"] and c.get("input") == entry["input"]:
                entry["use_id"] = str(c.get("id") or "")
            elif entry["use_id"] and c.get("type") == "tool_result" and \
                    c.get("tool_use_id") == entry["use_id"]:
                return True
        # Volání se v přepisu nenašlo ani po půl minutě (jiný formát přepisu):
        # Claude, který pokračuje dál, už na dialog nečeká.
        if not entry["use_id"] and e.get("type") == "assistant" and time.time() - entry["at"] > 30:
            return True
    return False


def povoleni_new(token, event):
    """Hook z tabu ohlásil dialog. Vrací id žádosti, nebo None (cizí žeton)."""
    bind, session = _tab_for_token(token)
    if not bind:
        return None
    event = event if isinstance(event, dict) else {}
    tool = str(event.get("tool_name") or "")[:80]
    inp = event.get("tool_input") if isinstance(event.get("tool_input"), dict) else {}
    op, summary, preview, otazky = _describe_request(tool, inp)
    path = ""
    try:
        path = core.transcript_for(session) or ""
    except Exception:
        pass
    hinted = str(event.get("transcript_path") or "")
    if (not path or not os.path.isfile(path)) and hinted.endswith(".jsonl") and \
            os.path.realpath(hinted).startswith(os.path.realpath(core.CLAUDE_DIR) + os.sep):
        path = hinted                   # přepis, který udal sám Claude Code v tabu
    use_id, offset = _find_tool_use(path, tool, inp) if path else ("", 0)
    rid = secrets.token_hex(8)
    now = time.time()
    entry = {"kind": "session", "op": op, "tool": tool, "summary": summary or tool,
             "preview": preview, "otazky": otazky, "tab": _cut(session.title, 80),
             "tab_id": session.id, "ukol": bind["title"], "token": token, "at": now, "seen": now,
             "event": threading.Event(), "reply": None, "transcript": path,
             "use_id": use_id, "offset": offset, "tool_name": tool, "input": inp}
    with _LOCK:
        PENDING[rid] = entry
    core.log(f"počítač: tab {session.id} čeká na souhlas — {tool} {summary!r}")
    threading.Thread(target=_push_pending, daemon=True).start()
    return rid


def povoleni_wait(token, rid, wait=25):
    """Hook čeká na odpověď. None = žádost nezná (cizí žeton, vyřízená, vypršelá)."""
    from . import server
    with _LOCK:
        entry = PENDING.get(str(rid or ""))
    if not entry or entry.get("kind") != "session" or \
            not secrets.compare_digest(entry["token"], str(token or "")):
        # Vyřízená mezitím: odpověď si hook ještě vyzvedne.
        with _LOCK:
            return _ANSWERED.pop((str(rid or ""), str(token or "")), None)
    entry["seen"] = time.time()
    deadline = time.time() + max(1.0, min(float(wait), 25.0))
    while not entry["event"].wait(1.0):
        session = server.HUB.sessions.get(entry["tab_id"])
        if _resolved_elsewhere(entry, session):
            with _LOCK:
                gone = PENDING.pop(str(rid), None)
            if gone:
                core.log(f"počítač: tab {entry['tab_id']} — {entry['op']} vyřízeno na počítači")
                threading.Thread(target=_push_pending, daemon=True).start()
            return {"hotovo": True}
        entry["seen"] = time.time()
        if time.time() >= deadline:
            return {"cekej": True}
    with _LOCK:
        _ANSWERED.pop((str(rid), entry["token"]), None)
    return entry["reply"] or {"hotovo": True}


def _session_answer(entry, choice, odpovedi):
    """Volba z karty → odpověď pro hook. ValueError, když nesedí na žádost."""
    if entry["op"] == "otazka" and choice in ("odpoved", "jednou", "povolit") and odpovedi is not None:
        if not isinstance(odpovedi, dict):
            raise ValueError("Odpovědi nejsou čitelné.")
        answers = {}
        for q in entry["otazky"]:
            raw = odpovedi.get(q["question"])
            if isinstance(raw, list):
                parts = [str(x).strip()[:500] for x in raw if str(x).strip()][:6]
                if not parts or not q["multi"]:
                    raise ValueError("Odpověď nesedí na otázku.")
                answers[q["question"]] = ", ".join(parts)
            elif isinstance(raw, str) and raw.strip():
                answers[q["question"]] = raw.strip()[:500]
            else:
                raise ValueError("Chybí odpověď na otázku: " + q["question"][:80])
        return {"behavior": "allow", "answers": answers}
    if choice == "zamitnout":
        msg = {"otazka": "Uživatel na otázku v prostoru na serveru neodpověděl a zamítl ji. "
                         "Zeptej se ho jinak, nebo pokračuj bez toho.",
               "plan": "Uživatel plán v prostoru na serveru neschválil. Zeptej se ho, co změnit."}
        return {"behavior": "deny", "message": msg.get(entry["op"],
                "Uživatel to v prostoru na serveru zamítl. Zeptej se ho, co by mu vyhovovalo, "
                "nebo to udělej jinak.")}
    if entry["op"] == "otazka":
        raise ValueError("Na otázku je potřeba vybrat odpověď.")
    if choice in ("povolit", "jednou", "30min"):
        return {"behavior": "allow"}
    raise ValueError("Neznámá volba.")


# ── v prostoru na serveru ────────────────────────────────────────────────────
def register_server_mcp():
    """Na počítači napojí MCP `server` (tools/server_mcp.py): Claude Code tu vidí
    sessions v prostoru na serveru. Jen když je appka k serveru přihlášená."""
    if not (core.CONFIG.get("gw_token") and account._base()):
        return
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    script = os.path.join(root, "tools", "server_mcp.py")
    claude = shutil.which("claude")
    if not os.path.isfile(script) or not claude:
        return
    python = sys.executable or "python3"
    have = (core._claude_json().get("mcpServers") or {}).get("server") or {}
    if have.get("command") == python and have.get("args") == [script]:
        return
    if have:
        core.run([claude, "mcp", "remove", "server", "-s", "user"], cwd=core.HOME, timeout=60)
    try:
        r = subprocess.run([claude, "mcp", "add", "server", "-s", "user", "--", python, script],
                           capture_output=True, text=True, cwd=core.HOME, timeout=60)
        ok, detail = r.returncode == 0, (r.stderr or r.stdout or "").strip()[:200]
    except Exception as exc:
        ok, detail = False, str(exc)
    core.log("server: MCP most pro Clauda na počítači napojen" if ok
             else f"server: MCP most se nepodařilo napojit — {detail}", "info" if ok else "warn")


def register_mcp():
    """Napojí MCP server tools/pocitac_mcp.py pro Claude Code v prostoru.

    Jen tam, kde brána prostoru dala most (HUB_POCITAC_URL). Registruje se
    přes `claude mcp add` — ~/.claude.json patří Claude Code a do jeho souboru
    se nepíše ručně. Hub startuje dřív než kterýkoli Claude v prostoru, takže
    se nikomu pod rukama nic nemění.
    """
    if not os.environ.get("HUB_POCITAC_URL"):
        return
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    script = os.path.join(root, "tools", "pocitac_mcp.py")
    claude = shutil.which("claude")
    if not os.path.isfile(script) or not claude:
        return
    python = sys.executable or "python3"
    have = (core._claude_json().get("mcpServers") or {}).get(MCP_NAME) or {}
    if have.get("command") == python and have.get("args") == [script]:
        return
    if have:
        core.run([claude, "mcp", "remove", MCP_NAME, "-s", "user"], cwd=core.HOME, timeout=60)
    try:
        r = subprocess.run([claude, "mcp", "add", MCP_NAME, "-s", "user", "--", python, script],
                           capture_output=True, text=True, cwd=core.HOME, timeout=60)
        ok = r.returncode == 0
        detail = (r.stderr or r.stdout or "").strip()[:200]
    except Exception as exc:
        ok, detail = False, str(exc)
    core.log("počítač: MCP most pro Clauda napojen" if ok
             else f"počítač: MCP most se nepodařilo napojit — {detail}", "info" if ok else "warn")
