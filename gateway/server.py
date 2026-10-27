"""
Brána: přihlášení a reverzní proxy na hub každého uživatele.

Jak to drží pohromadě:

* Uživatel otevře adresu brány. Bez platné cookie dostane **přihlašovací
  stránku**; po přihlášení mu brána nastaví cookie s tokenem (v databázi jen
  jako otisk) a od té chvíle ho pozná.
* Pro přihlášeného uživatele brána zajistí, že běží **jeho** instance hubu —
  `claude-hub.py --no-browser` spuštěná v izolaci (`bwrap`) s jeho domovem —
  a všechen provoz (HTTP i WebSocket) do ní **proxuje**. Token té instance
  drží brána a vkládá ho hlavičkou; do prohlížeče se nikdy nedostane.

Žádná správcovská stránka: účty se zakládají a mění z příkazové řádky
(`python3 -m gateway.admin …`) na serveru, ne přes web.

TLS terminuje nginx před bránou, takže brána poslouchá na loopbacku. Bez
izolace (`none`) se pustí jen tam — `isolation.check()` to hlídá.
"""
import hashlib
import html
import http.client
import json
import os
import secrets
import socket
import subprocess
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from hub import __version__, qr

from . import webauthn
from . import mcp_pc, zrcadlo
from . import (config, firmy, isolation, mcp, mcp_sdilene, pocitac, poznamky, pristup, relace, safefs, shared,
               slozky, totp, workspace)
from .accounts import Accounts

HTML = "text/html; charset=utf-8"

# Hlavičky, které se u proxy nepřeposílají — patří jednomu skoku spojení, ne
# tomu za ním.
HOP_BY_HOP = {"connection", "keep-alive", "proxy-authenticate",
              "proxy-authorization", "te", "trailers", "transfer-encoding",
              "upgrade", "host", "content-length"}
HUB_URL_RE = "http://127.0.0.1:"

# Předání přihlášení z hubu na počítači: ten se k bráně přihlásí přes API,
# drží token — ale prohlížeč potřebuje cookie na doméně brány. Posílat token
# v adrese by ho zapsalo do historie, proto se za něj vymění jednorázový kód
# s minutovou platností, který se při prvním použití zahodí.
#
# Cookie dostane **tentýž token**, který drží appka — ne nový. Appka se předává
# při každém spuštění, takže nový token by každým startem přibyl v databázi.
# A hlavně: odhlášení v okně pak zneplatní i přihlášení appky, takže se po
# „Odhlásit se" příště opravdu zeptá, místo aby tiše naskočila zpátky.
HANDOFF_TTL = 60
_handoffs = {}                       # kód -> (token zařízení, do kdy)
# Přihlášení appky na počítači přes prohlížeč (passkey): appka vyrobí nonce,
# otevře v prohlížeči /zarizeni?n=…, člověk se přihlásí (i passkeyem) a potvrdí,
# a appka si token vyzvedne (GET /gw/zarizeni?n=…) — jednou.
_zarizeni = {}                       # nonce -> {"label", "exp", "token", "user"}
ZAR_RE = __import__("re").compile(r"^[A-Za-z0-9_-]{20,64}$")
_handoff_lock = threading.Lock()


def _handoff_new(token):
    code = secrets.token_urlsafe(24)
    now = time.time()
    with _handoff_lock:
        for old, (_tok, exp) in list(_handoffs.items()):
            if exp < now:
                _handoffs.pop(old, None)
        _handoffs[code] = (token, now + HANDOFF_TTL)
    return code


# Přihlášení ve dvou krocích. Po správném hesle brána ještě nevydá token, jen
# lístek na druhý krok — kód z aplikace, nebo první nastavení aplikace — s pěti
# minutami platnosti a stejným počtem pokusů jako heslo. Lístek žije jen
# v paměti brány.
LOGIN_TTL = 5 * 60
LOGIN_TRIES = config.LOGIN_TRIES
_tickets = {}                        # lístek -> {"uid", "exp", "tries", "secret", "label"}
_ticket_lock = threading.Lock()


# Hádání hesel a kódů: po config.LOGIN_TRIES neúspěších se přihlášení na
# config.LOGIN_LOCK zamkne (Accounts.fail_*, klíče skládá _fail_keys).
# scrypt sám zdrží jen o desetinu vteřiny — bez zámku by se dalo hádat dál.
def _duration(seconds):
    """Doba ve 4. pádě: „hodinu", „3 minuty", „40 minut"."""
    minutes = max(1, -(-int(seconds) // 60))
    if 55 <= minutes <= 60:
        return "hodinu"
    return ("1 minutu" if minutes == 1 else f"{minutes} minuty" if minutes < 5
            else f"{minutes} minut")


def too_many(seconds):
    return ("Moc neúspěšných pokusů — přihlášení je zablokované. "
            f"Zkus to znovu za {_duration(seconds)}.")


def locked_now(seconds):
    return f"Přihlášení je teď na {_duration(seconds)} zablokované."


def tries_left(left):
    if left == 1:
        return f"Zbývá 1 pokus, pak se přihlášení na {_duration(config.LOGIN_LOCK)} zablokuje."
    return f"Zbývají {left} pokusy." if 1 < left < 5 else f"Zbývá {left} pokusů."


_pk_reg = {}       # user_id → (výzva pro přidání passkeye, platí do)
_pk_login = {}     # id výzvy → (výzva pro přihlášení passkeyem, platí do)


def _ticket_new(uid, label="", secret=""):
    ticket = secrets.token_urlsafe(24)
    now = time.time()
    with _ticket_lock:
        for old, t in list(_tickets.items()):
            if t["exp"] < now:
                _tickets.pop(old, None)
        _tickets[ticket] = {"uid": uid, "exp": now + LOGIN_TTL, "tries": 0,
                            "secret": secret, "label": label}
    return ticket


def _ticket_get(ticket):
    with _ticket_lock:
        found = _tickets.get(ticket or "")
        if found and found["exp"] < time.time():
            _tickets.pop(ticket, None)
            return None
        return found


def _ticket_drop(ticket):
    with _ticket_lock:
        _tickets.pop(ticket or "", None)


def _ticket_miss(ticket):
    """Špatný kód. Vrací, kolik pokusů zbývá; po posledním lístek propadne."""
    with _ticket_lock:
        found = _tickets.get(ticket or "")
        if not found:
            return 0
        found["tries"] += 1
        left = LOGIN_TRIES - found["tries"]
        if left <= 0:
            _tickets.pop(ticket, None)
        return max(0, left)


def _handoff_take(code):
    """Vrátí token zařízení a kód zahodí. Druhé použití už nic nedostane."""
    if not code:
        return ""
    with _handoff_lock:
        found = _handoffs.pop(code, None)
    if not found:
        return ""
    token, expiry = found
    return token if expiry >= time.time() else ""


def _errlog(where, exc):
    """Dočasná diagnostika: zapíše výjimku i s tracebackem do err.log."""
    try:
        import traceback
        with open(os.path.join(config.GATEWAY_DIR, "err.log"), "a",
                  encoding="utf-8") as fh:
            fh.write("%s  %s  %r\n%s\n" % (
                time.strftime("%H:%M:%S"), where, exc,
                traceback.format_exc()))
    except Exception:
        pass


# ── instance hubu jednoho uživatele ──────────────────────────────────────────
class HubProc:
    """Jedna běžící instance `claude-hub.py --no-browser` pro jednoho uživatele.

    Na serveru běží v systemd scope pojmenované podle účtu (`unit`). Podle ní
    se pozná, jestli ještě žije, a zastavuje se celá — proces, který brána
    spustila, to říct neumí: `systemd-run` se v kontextu služby od sandboxu
    odpojí a sandbox má vlastní init, který SIGTERM ignoruje. Podrobně
    v isolation.py u stop_scope.
    """

    # Jak často se smí živost číst znovu. Ptá se každý proxovaný požadavek a
    # stránka jich při načtení pošle desítky.
    CHECK_EVERY = 2.0

    def __init__(self, user, mode):
        self.user = user
        self.uid = user["id"]
        self.mode = mode
        self.proc = None
        self.port = None
        self.token = None
        self.home = None
        self.unit = ""
        self.cgroup = ""
        self.started = 0
        self.last_active = time.time()
        # Kolik prohlížečů je k prostoru právě připojených (websocket). Prostor
        # s připojením se kvůli místu neuspává — shodil by rozdělanou práci.
        self.conns = 0
        # Sdílené Obsidiany svázané při startu — nové se ukážou až po restartu.
        self.shared = []
        # Poznámky firemního trezoru, které prostor při startu nedostal
        # (gateway/poznamky.py) — podle toho hub pozná, že chce restart.
        self.hidden = {}
        # Žeton, kterým se prostor u brány prokazuje, když Claude sahá na
        # počítač uživatele (gateway/pocitac.py). Nový s každým startem.
        self.pocitac_token = ""
        # S jakým přihlášením Clauda prostor nastartoval (workspace.auth_mark) —
        # připojené předplatné se do běžícího prostoru dostane až restartem.
        self.auth_mark = ""
        # S jakým webem od správce prostor jede (workspace.web_mark) — změnu za
        # běhu mu pošle HubManager (preved_web), bez restartu.
        self.web_mark = ""
        # Verze hubu, na které prostor jede (workspace.code_dir při startu).
        self.version = ""
        self._checked = 0.0
        self._alive = False
        self._lock = threading.Lock()

    def start(self):
        # Scope, která zbyla z minula (spadlá brána, instance, o které se
        # nevědělo), i pod starým jménem u<id>: systemd-run by na stejném jménu
        # skončil chybou a domov se pod běžícím prostorem přejmenovat nesmí.
        names = (workspace.unit_name(self.user), workspace.legacy_unit_name(self.user))
        running = [n for n in names
                   if not isolation.stop_scope(n) and isolation.scope_active(n)]
        if not running:
            # Domov podle e-mailu; starý u<id> se přejmenuje tady, kdy prostor
            # prokazatelně neběží.
            workspace.migrate_home(self.user)
        self.shared = [v["slug"] for v in shared.vaults_for(self.user)]
        argv, home, unit, verze, self.hidden = workspace.session_spec(
            self.user, isolation, self.mode)
        self.home = home
        self.unit = unit
        self.version = verze
        env = dict(os.environ, HOME=home)
        # Klíč API brány jen tomu, kdo jede na `central` — `own` ho nesmí
        # zdědit ani z prostředí samotné brány.
        env.pop("ANTHROPIC_API_KEY", None)
        env.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
        env.pop("GOOGLE_OAUTH_CLIENT_ID", None)
        env.pop("GOOGLE_OAUTH_CLIENT_SECRET", None)
        env.update(workspace.session_env(self.user))
        self.auth_mark = workspace.auth_mark(self.user)
        # Docker prostředí brány do kontejneru nepouští (HUB_WEB tam nedojde) —
        # web pak doručí HubManager za běhu, jako po změně.
        self.web_mark = "" if self.mode == "docker" else workspace.web_mark(self.user)
        # Most na počítač uživatele: MCP server v prostoru (tools/pocitac_mcp.py)
        # volá bránu přímo na loopbacku, ne přes nginx — a tenhle žeton říká,
        # čí prostor volá. Jiné prostory ho nevidí (každý má svoje prostředí).
        self.pocitac_token = secrets.token_urlsafe(32)
        env["HUB_POCITAC_URL"] = _loopback_url()
        env["HUB_POCITAC_TOKEN"] = self.pocitac_token
        # XDG_RUNTIME_DIR musí session dostat, jinak si systemd --user scope
        # nemá kam sáhnout (limity by tiše vypadly).
        env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
        self.proc = subprocess.Popen(
            argv, cwd=home, env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            bufsize=1, start_new_session=True)
        # První řádek výpisu je URL s portem a tokenem (viz claude-hub.py
        # --no-browser). Čekáme na něj s rozumným stropem.
        url = self._read_url(timeout=40)
        if not url:
            self.stop()
            raise RuntimeError("Instance hubu nenaběhla (nevypsala adresu).")
        parsed = urllib.parse.urlparse(url)
        self.port = parsed.port
        self.token = urllib.parse.parse_qs(parsed.query).get("t", [""])[0]
        if unit:
            self.cgroup = isolation.scope_cgroup(unit)
        self.started = time.time()
        self.last_active = time.time()
        self._checked = 0.0
        # Zbytek výpisu jen odsáváme, ať se roura nezaplní a proces nezasekne.
        threading.Thread(target=self._drain, daemon=True).start()

    def _read_url(self, timeout):
        deadline = time.time() + timeout
        while time.time() < deadline:
            line = self.proc.stdout.readline()
            if not line:
                return ""  # proces skončil dřív, než něco vypsal
            line = line.strip()
            if line.startswith(HUB_URL_RE):
                return line
        return ""

    def _drain(self):
        try:
            for _ in self.proc.stdout:
                pass
        except Exception:
            pass

    def alive(self):
        if self.proc is None:
            return False
        if not self.unit:
            # Bez scope (docker, stroj bez systemd) je proces to jediné, co máme.
            return self.proc.poll() is None
        now = time.time()
        if now - self._checked >= self.CHECK_EVERY:
            self._alive = (isolation.cgroup_alive(self.cgroup) if self.cgroup
                           else isolation.scope_active(self.unit))
            self._checked = now
        return self._alive

    def touch(self):
        self.last_active = time.time()

    def snapshot(self, reason="uspani"):
        """Než se prostor zastaví: hub v něm uloží otevřené taby (i id
        konverzací), ať po dalším otevření appka pokračuje, kde člověk skončil.
        Chyba nevadí — zastavit se musí i zaseklý prostor."""
        if not (self.port and self.token and self.proc):
            return
        try:
            conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
            body = json.dumps({"duvod": reason}).encode()
            conn.request("POST", "/api/prostor-snapshot", body=body,
                         headers={"X-Hub-Token": self.token, "Content-Type": "application/json"})
            conn.getresponse().read()
            conn.close()
        except Exception:
            pass

    def stop(self, reason="uspani"):
        """Zastaví prostor i se vším, co v něm běží (hub, shelly, Claude Code).
        Předtím si hub v něm uloží taby (`reason` = uspani / restart)."""
        self.snapshot(reason)
        p, self.proc = self.proc, None
        self._alive = False
        if self.unit:
            isolation.stop_scope(self.unit)
        if p:
            try:
                os.killpg(os.getpgid(p.pid), 15)
            except Exception:
                try:
                    p.terminate()
                except Exception:
                    pass


def hub_post(hub, name, payload, timeout=20):
    """Brána volá hub prostoru napřímo (ne z prohlížeče) — jeho tokenem."""
    conn = http.client.HTTPConnection("127.0.0.1", hub.port, timeout=timeout)
    try:
        conn.request("POST", "/api/" + name, body=json.dumps(payload).encode("utf-8"),
                     headers={"X-Hub-Token": hub.token, "Content-Type": "application/json"})
        resp = conn.getresponse()
        data = json.loads(resp.read() or b"{}")
        return data if resp.status == 200 else {"error": data.get("error") or "Prostor odmítl."}
    except (OSError, ValueError, http.client.HTTPException) as exc:
        _errlog("hub_call " + name, exc)
        return {"error": "Prostor teď neodpovídá, zkus to za chvíli."}
    finally:
        conn.close()


class HubManager:
    """Které instance hubu běží a kdo je čí. Uspává nečinné, drží strop počtu."""

    def __init__(self, mode, max_sessions, idle):
        self.mode = mode
        self.max_sessions = max_sessions
        self.idle = idle
        self.procs = {}          # uid -> HubProc
        self._lock = threading.Lock()
        threading.Thread(target=self._reaper, daemon=True).start()

    def get(self, user):
        """Vrátí běžící instanci uživatele; když neběží, spustí ji.

        Vyhodí RuntimeError, když je server plný a nejde nic uvolnit.
        """
        uid = user["id"]
        with self._lock:
            proc = self.procs.get(uid)
            if proc and proc.alive():
                proc.touch()
                return proc
            if proc:
                # Spadlá nebo zastavená zvenku (`gateway.admin stop`). Zbytky
                # uklidit, ať nová instance nevedle staré neběží dvě.
                self.procs.pop(uid, None)
                proc.stop()
            self._make_room()
            proc = HubProc(user, self.mode)
            proc.start()
            self.procs[uid] = proc
            return proc

    # Jak dlouho musí být prostor bez práce, než ho smí uspat někdo jiný.
    EVICT_IDLE = 5 * 60

    def _make_room(self):
        """Uvolní místo, když je dosažen strop: uspí nejdéle nečinný prostor,
        ke kterému není nikdo připojený. Když takový není, radši odmítne
        nového, než aby shodil někomu rozdělanou práci.

        Dřív se bral nejdéle nečinný bez ohledu na připojení. Když bylo lidí
        s otevřeným hubem víc než míst, prostory se v kolečku shazovaly:
        uspaný se za vteřinu znovu připojil a uspal dalšího (2.5.4 po
        restartu brány: 24 startů jednoho prostoru za 15 minut).
        """
        live = [p for p in self.procs.values() if p.alive()]
        if len(live) < self.max_sessions:
            return
        now = time.time()
        spare = [p for p in live
                 if not p.conns and now - p.last_active >= self.EVICT_IDLE]
        if not spare:
            raise RuntimeError(
                f"Server je právě plný (obsazeno {len(live)} z {self.max_sessions} "
                "míst). Zkus to za pár minut znovu.")
        idlest = min(spare, key=lambda p: p.last_active)
        self.procs.pop(idlest.uid, None)
        idlest.stop()

    def _reaper(self):
        # Kontroluje se častěji, než je doba uspání — jinak by se s krátkým
        # IDLE_SLEEP (test) čekalo celou minutu navíc.
        every = max(1.0, min(60.0, self.idle / 2))
        while True:
            time.sleep(every)
            now = time.time()
            to_stop = []
            with self._lock:
                for uid, proc in list(self.procs.items()):
                    if not proc.alive() or now - proc.last_active > self.idle:
                        self.procs.pop(uid, None)
                        to_stop.append(proc)
            # Zastavení může trvat pár vteřin (hub dostane čas skončit) —
            # mimo zámek, ať mezitím nečekají požadavky ostatních.
            for proc in to_stop:
                proc.stop()
            # Web změněný přes claude-hub-admin (jiný proces) do běžících
            # prostorů — nejpozději do minuty, bez restartu.
            with self._lock:
                live = list(self.procs.values())
            for proc in live:
                try:
                    if proc.alive() and proc.web_mark != workspace.web_mark(proc.user):
                        self.preved_web(proc.user)
                except Exception as exc:
                    _errlog("web do prostoru", exc)

    def preved_web(self, user):
        """Přidělený / změněný / odebraný web pošle do běžícího prostoru (hub
        ho zapíše do ~/.claude/hub-web.json). Neběžící ho dostane při startu
        (workspace.session_env). Otisk se zapíše i po neúspěchu — starý hub,
        který akci nezná, by se jinak zkoušel každou minutu znovu."""
        proc = self.procs.get(user["id"])
        if not proc or not proc.alive():
            return False
        mark = workspace.web_mark(user)
        out = hub_post(proc, "web-host", {"web": json.loads(workspace.web_env(user) or "null")},
                       timeout=10)
        proc.web_mark = mark
        if not out.get("ok"):
            _errlog("web do prostoru", RuntimeError(str(out.get("error") or "")[:200]))
        return bool(out.get("ok"))

    def user_for_pocitac(self, token):
        """Čí běžící prostor se tímhle žetonem prokazuje, nebo None."""
        if not token:
            return None
        with self._lock:
            procs = list(self.procs.values())
        for proc in procs:
            if proc.pocitac_token and secrets.compare_digest(proc.pocitac_token, token) \
                    and proc.alive():
                return proc.user
        return None

    def restart(self, user):
        """Zastaví prostor uživatele; další požadavek ho spustí znovu — i s nově
        svázanými sdílenými Obsidiany. Na přání z hubu."""
        with self._lock:
            proc = self.procs.pop(user["id"], None)
        if proc:
            # Restart na přání z hubu: taby zpátky hned, jako po aktualizaci.
            proc.stop(reason="restart")
        return bool(proc)

    def stop_idle(self, uid):
        """Zastaví prostor, ke kterému není připojený žádný prohlížeč. Po odebrání
        ze sdíleného Obsidianu, ať přístup nevydrží do dalšího restartu."""
        with self._lock:
            proc = self.procs.get(uid)
            if not proc or proc.conns:
                return False
            self.procs.pop(uid, None)
        proc.stop()
        return True

    def stop_all(self):
        with self._lock:
            procs = list(self.procs.values())
            self.procs.clear()
        for proc in procs:
            proc.stop()


# ── stránky, které patří bráně (ne hubu) ─────────────────────────────────────
def _page(title, body):
    return ("""<!doctype html><html lang=cs><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>""" + html.escape(title) + """</title>
<link rel=manifest href="/manifest.webmanifest">
<link rel=icon href="/favicon.ico" sizes=any>
<link rel=apple-touch-icon href="/icon-192.png">
<meta name=theme-color content="#0d1117">
<meta name=mobile-web-app-capable content=yes>
<meta name=apple-mobile-web-app-capable content=yes>
<meta name=apple-mobile-web-app-status-bar-style content=black-translucent>
<meta name=apple-mobile-web-app-title content="Aladin">
<style>
:root{color-scheme:dark}
*{box-sizing:border-box}
body{font:15px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;margin:0;
min-height:100vh;display:grid;place-items:center;background:#0d1117;color:#e6edf3;padding:24px}
.card{width:100%;max-width:24rem;background:#1b2129;border:1px solid #30363d;
border-radius:12px;padding:28px}
h1{font-size:1.25rem;margin:0 0 4px}
p.sub{margin:0 0 20px;color:#8b949e;font-size:.9rem}
label{display:block;margin:14px 0 4px;font-size:.85rem;color:#c9d1d9}
input{width:100%;padding:10px 12px;border-radius:8px;border:1px solid #30363d;
background:#0d1117;color:#e6edf3;font-size:1rem}
input:focus{outline:none;border-color:#4c97f0}
button{margin-top:20px;width:100%;padding:11px;border:0;border-radius:8px;
background:#4c97f0;color:#0d1117;font-weight:600;font-size:1rem;cursor:pointer}
button:hover{background:#6aa9f4}
.err{margin-top:14px;color:#ff7b72;font-size:.9rem;min-height:1.2em}
a{color:#4c97f0}
.card.wide{max-width:30rem}
.steps{padding-left:1.2rem;margin:10px 0;font-size:.92rem;line-height:1.55;color:#c9d1d9}
.qr{background:#fff;border-radius:10px;padding:10px;display:flex;justify-content:center;margin:12px 0}
.qr svg{display:block;width:220px;height:220px}
.hint{font-size:.85rem;color:#8b949e;margin:10px 0 0;line-height:1.5}
code{font-family:ui-monospace,monospace;color:#4c97f0;overflow-wrap:anywhere}
.key{display:block;margin-top:4px;font-size:1rem;letter-spacing:.02em}
.codes{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin:16px 0}
.codes code{background:#0d1117;border:1px solid #30363d;border-radius:6px;padding:7px;text-align:center;font-size:1rem;color:#e6edf3}
a.button{display:block;text-align:center;margin-top:20px;padding:11px;border-radius:8px;
background:#4c97f0;color:#0d1117;font-weight:600;text-decoration:none}
</style>
""" + body + "</html>").encode("utf-8")


def predplatne_page(owner):
    """Správa předplatného pro hosta — místo hubu vlastníka (gateway/pristup.py).
    Vlastní stránka v barvách appky na serveru (paleta DARK z hub/core.py,
    modrý akcent prostoru `CLOUD`), ne obecná
    karta brány: host má vidět, že je v Palubě, v čím prostoru a co se stalo."""
    who = html.escape(owner.get("name") or owner.get("email") or "")
    return ("""<!doctype html><html lang=cs><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>Správa předplatného — Aladin</title>
<link rel=icon href="/favicon.ico" sizes=any>
<meta name=theme-color content="#161b22">
<style>
:root{color-scheme:dark;--bg:#0d1117;--bar:#161b22;--card:#1b2129;--hover:#242b35;--border:#30363d;
--fg:#d0d0d0;--bright:#e6edf3;--dim:#8b949e;--accent:#4c97f0;--green:#3fb950;--red:#f85149}
*{box-sizing:border-box}
html,body{margin:0;min-height:100%;background:var(--bg);color:var(--fg);
font:14px/1.5 system-ui,"Segoe UI",Cantarell,Ubuntu,sans-serif}
.top{display:flex;align-items:center;gap:10px;height:46px;padding:0 14px;background:var(--bar);
border-bottom:1px solid var(--border)}
.brand{display:flex;align-items:center;gap:8px;color:var(--bright);font-weight:700}
.brand svg{width:20px;height:20px}
.badge{padding:2px 9px;border-radius:999px;border:1px solid color-mix(in srgb,var(--accent) 55%,transparent);
color:var(--accent);font-size:12px;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;min-width:0}
.sp{flex:1}
.btn{display:inline-flex;align-items:center;justify-content:center;gap:7px;padding:9px 16px;border-radius:8px;
border:1px solid var(--border);background:var(--card);color:var(--bright);font:inherit;font-weight:600;
cursor:pointer;text-decoration:none;white-space:nowrap}
.btn:hover{background:var(--hover)}
.btn.primary{background:var(--accent);border-color:var(--accent);color:var(--bg)}
.btn.primary:hover{filter:brightness(1.08)}
.btn.small{padding:6px 12px;font-size:13px}
.btn.danger{background:none;border-color:transparent;color:var(--dim);font-weight:500}
.btn.danger:hover{color:var(--red);background:none}
.btn:disabled{opacity:.5;cursor:default}
main{max-width:560px;margin:0 auto;padding:36px 16px 48px}
h1{margin:0 0 4px;font-size:22px;color:var(--bright)}
.sub{margin:0 0 22px;color:var(--dim)}
.card{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:18px;margin-bottom:14px}
.row{display:flex;align-items:center;gap:12px;flex-wrap:wrap}
.lbl{color:var(--dim);font-size:12px;text-transform:uppercase;letter-spacing:.04em;margin-bottom:8px}
.pill{display:inline-flex;align-items:center;gap:7px;padding:4px 11px;border-radius:999px;font-weight:600;font-size:13px}
.pill i{width:8px;height:8px;border-radius:50%;background:currentColor}
.pill.ok{color:var(--green);background:color-mix(in srgb,var(--green) 14%,transparent)}
.pill.firma{color:var(--accent);background:color-mix(in srgb,var(--accent) 14%,transparent)}
.pill.nic{color:var(--red);background:color-mix(in srgb,var(--red) 14%,transparent)}
.ucet{color:var(--fg)}
.note{color:var(--dim);font-size:13px;margin-top:10px}
.steps{list-style:none;margin:0;padding:0;display:flex;flex-direction:column;gap:14px}
.steps li{display:flex;gap:12px}
.num{flex:none;width:26px;height:26px;border-radius:50%;display:grid;place-items:center;font-weight:700;font-size:13px;
border:1px solid var(--border);color:var(--dim)}
.steps li.on .num{border-color:var(--accent);color:var(--accent)}
.steps li.hotovo .num{border-color:var(--green);background:var(--green);color:#0d1117}
.steps .t{color:var(--bright);font-weight:600}
.steps .d{color:var(--dim);font-size:13px;margin:2px 0 8px}
input{width:100%;padding:9px 12px;border-radius:8px;border:1px solid var(--border);background:var(--bg);
color:var(--bright);font:inherit;font-size:15px}
input:focus{outline:none;border-color:var(--accent)}
.kod{display:flex;gap:8px}
.kod input{flex:1;min-width:0}
.banner{display:flex;gap:12px;align-items:flex-start;padding:14px 16px;border-radius:12px;margin-bottom:14px;font-weight:500}
.banner svg{flex:none;width:22px;height:22px;margin-top:1px}
.banner.ok{background:color-mix(in srgb,var(--green) 13%,var(--card));border:1px solid color-mix(in srgb,var(--green) 45%,transparent);color:var(--bright)}
.banner.ok svg{color:var(--green)}
.banner.err{background:color-mix(in srgb,var(--red) 12%,var(--card));border:1px solid color-mix(in srgb,var(--red) 45%,transparent);color:var(--bright)}
.banner.err svg{color:var(--red)}
.banner small{display:block;color:var(--dim);font-weight:400;margin-top:2px}
.foot{display:flex;justify-content:flex-end;margin-top:6px}
[hidden]{display:none!important}
@media (max-width:480px){.badge{max-width:46vw}.btn.zpet span{display:none}}
</style>
<header class=top>
  <span class=brand><svg viewBox="0 0 24 24" fill="#4c97f0" fill-rule="evenodd"><path d="M12 2.6a2.7 2.7 0 1 1 0 5.4 2.7 2.7 0 0 1 0-5.4zM8.7 11.2c0-2.3 6.6-2.3 6.6 0l-1.6 2.1h-3.4zM8.2 9.6 6 7.2l1-.9 2.3 2.5zM15.8 9.6 18 7.2l-1-.9-2.3 2.5zM10.3 13.6h3.4v1.6c2 .8 3.6 2.4 3.6 4.7 0 1.4-1 2.3-2.4 2.3H9.1c-1.4 0-2.4-.9-2.4-2.3 0-2.3 1.6-3.9 3.6-4.7z"/></svg>Aladin</span>
  <span class=badge>Prostor: """ + who + """ · Správa předplatného</span>
  <span class=sp></span>
  <button id=zpet class="btn small zpet">← <span>Zpět do svého</span></button>
</header>
<main>
  <h1>Správa předplatného</h1>
  <p class=sub>Nastavuješ, na čem jede Claude v prostoru <b>""" + who + """</b>. Nic jiného tady nevidíš ani neměníš.</p>

  <div id=ok class="banner ok" hidden><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><path d="M7.5 12.5l3 3 6-6.5"/></svg>
    <div id=okText>Předplatné je připojené.<small>Nové i běžící chaty v prostoru už jedou na něm.</small></div></div>
  <div id=err class="banner err" hidden><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><circle cx="12" cy="12" r="10"/><path d="M12 7v6M12 16.5v.5"/></svg>
    <div id=errText></div></div>

  <section class=card>
    <div class=lbl>Claude v prostoru</div>
    <div class=row><span id=pill class=pill><i></i><span id=rezim>Zjišťuji…</span></span><span id=ucet class=ucet></span></div>
    <div id=pozn class=note hidden></div>
  </section>

  <section id=pripojit class=card>
    <div class=lbl>Připojit vlastní předplatné Claude</div>
    <ol class=steps>
      <li id=k1 class=on><span class=num>1</span><div><div class=t>Začni přihlášení</div>
        <div class=d>Připraví odkaz na přihlášení k účtu Claude.</div>
        <button id=prihlasit class="btn primary">Připojit předplatné</button></div></li>
      <li id=k2><span class=num>2</span><div><div class=t>Přihlas se a potvrď</div>
        <div class=d>Otevře se stránka Claude — přihlas se účtem s předplatným a potvrď přístup.</div>
        <a id=odkaz class="btn" target=_blank rel=noopener hidden>Otevřít přihlášení Claude ↗</a></div></li>
      <li id=k3><span class=num>3</span><div><div class=t>Vlož kód</div>
        <div class=d>Po potvrzení ti Claude ukáže kód — zkopíruj ho sem.</div>
        <div id=kodbox class=kod hidden><input id=kod autocomplete=off placeholder="Kód ze stránky Claude"><button id=poslat class="btn primary">Odeslat</button></div>
        <button id=zrusit class="btn danger" hidden>Zrušit přihlášení</button></div></li>
    </ol>
  </section>

  <div class=foot><button id=odpojit class="btn danger" hidden>Odpojit předplatné (zpět na firemní přístup)</button></div>
</main>
<script>
const $ = (i) => document.getElementById(i);
const REZIM = {predplatne: ['ok', 'Vlastní předplatné'], api: ['firma', 'Firemní přístup'],
  ucet: ['ok', 'Přihlášený účet Claude'], zadne: ['nic', 'Claude není přihlášený']};
let timer = null, zamek = 0, prave = false;
async function gw(path, data) {
  const r = await fetch(path, data === undefined ? {credentials: 'same-origin'} : {
    method: 'POST', credentials: 'same-origin',
    headers: {'Content-Type': 'application/json', 'X-Hub-Account': '1'}, body: JSON.stringify(data)});
  const d = await r.json().catch(() => ({}));
  if (d.pristup === 'odebran') location.href = '/';
  if (!r.ok) throw new Error(d.error || 'Server neodpovídá.');
  return d;
}
function chyba(text) { $('errText').textContent = text || ''; $('err').hidden = !text; }
function kresli(d) {
  const u = d.ucet || {}, p = d.prihlaseni || {}, mode = (d.rezim || {}).mode;
  const [cls, txt] = REZIM[mode] || ['nic', mode || '?'];
  $('pill').className = 'pill ' + cls; $('rezim').textContent = txt;
  $('ucet').textContent = u.email ? u.email + (u.plan ? ' · ' + u.plan : '') : '';
  $('pozn').hidden = !d.needs_restart;
  $('pozn').textContent = 'Nové přihlášení se do prostoru ještě nepředalo — zkus obnovit stránku za chvíli.';
  const bezi = ['starting', 'waiting', 'token'].includes(p.state);
  const pripojeno = mode === 'predplatne';
  // Úspěch jen tehdy, když předplatné opravdu platí — ne podle starého stavu přihlášení.
  if (pripojeno && (prave || p.state === 'done')) $('ok').hidden = false;
  if (!pripojeno) $('ok').hidden = true;
  chyba(p.state === 'error' ? p.error : '');
  $('pripojit').hidden = pripojeno && !bezi;
  $('odpojit').hidden = !pripojeno || bezi;
  $('prihlasit').hidden = bezi;
  $('k1').className = bezi ? 'hotovo' : 'on';
  $('k2').className = bezi && p.url ? 'on' : '';
  $('k3').className = bezi && p.url ? 'on' : '';
  $('odkaz').hidden = !(bezi && p.url); if (p.url) $('odkaz').href = p.url;
  $('kodbox').hidden = !(bezi && p.url); $('zrusit').hidden = !bezi;
  // Krátký zámek po překreslení: druhé kliknutí dvojkliku nesmí trefit tlačítko,
  // které se právě objevilo pod myší (tak se předplatné omylem odpojilo).
  zamek = Date.now() + 700;
  clearTimeout(timer);
  if (bezi) timer = setTimeout(nacti, 2000);
}
async function nacti() { try { kresli(await gw('/gw/pristup/predplatne')); } catch (e) { chyba(e.message); } }
async function akce(a, extra, btn) {
  if (Date.now() < zamek) return;
  const tl = [...document.querySelectorAll('button')]; tl.forEach((b) => b.disabled = true);
  chyba('');
  try {
    const d = await gw('/gw/pristup/predplatne', {akce: a, ...(extra || {})});
    prave = a === 'kod' || (prave && a !== 'odpojit');
    kresli(d);
    if (a === 'kod' && (d.prihlaseni || {}).state !== 'error') setTimeout(nacti, 1500);
  } catch (e) { chyba(e.message); }
  finally { tl.forEach((b) => b.disabled = false); }
}
$('prihlasit').onclick = () => akce('prihlasit');
$('poslat').onclick = () => { const k = $('kod').value.trim(); if (!k) return; akce('kod', {kod: k}); $('kod').value = ''; };
$('kod').onkeydown = (e) => { if (e.key === 'Enter') $('poslat').click(); };
$('zrusit').onclick = () => akce('zrusit');
$('odpojit').onclick = () => { if (confirm('Odpojit vlastní předplatné? Claude v prostoru pojede zase přes firemní přístup.')) { prave = false; akce('odpojit'); } };
$('zpet').onclick = async () => { await gw('/gw/pristup', {akce: 'odejit'}).catch(() => {}); location.href = '/'; };
nacti();
</script></html>""").encode("utf-8")


def error_page(message):
    """Chyba, kterou uvidí člověk v prohlížeči — věta a tlačítko zkusit znovu."""
    return _page("Chvilku strpení — Aladin", f"""
<div class=card>
<h1>Chvilku strpení</h1>
<p class=sub>{html.escape(message)}</p>
<a class=button href="">Zkusit znovu</a>
</div>""")


# Lidské věty místo technických hlášek (detail jde do err.log přes _errlog).
MSG_STARTING = "Tvůj prostor se právě spouští — obnov stránku za chvíli."
MSG_FULL = "Server je teď plný, zkus to za pár minut."
MSG_BROKEN = "Něco se pokazilo. Zkus to prosím za chvíli znovu."
MSG_SAVE = "Uložit se nepodařilo. Zkus to prosím za chvíli znovu."


PASSKEY_LOGIN_JS = """<script>
(() => {
  if (!window.PublicKeyCredential) return;
  const form = document.querySelector('form.card');
  const b64d = (s) => Uint8Array.from(atob(s.replace(/-/g, '+').replace(/_/g, '/') + '==='.slice((s.length + 3) % 4)), (c) => c.charCodeAt(0));
  const b64e = (b) => btoa(String.fromCharCode(...new Uint8Array(b))).replace(/\\+/g, '-').replace(/\\//g, '_').replace(/=+$/, '');
  const sep = document.createElement('p'); sep.className = 'hint'; sep.style.textAlign = 'center'; sep.textContent = 'nebo';
  const btn = document.createElement('button'); btn.type = 'button'; btn.id = 'pklogin';
  btn.style.cssText = 'background:transparent;color:#4c97f0;border:1px solid #4c97f0;margin-top:6px';
  btn.textContent = 'Přihlásit se passkeyem';
  const err = document.createElement('div'); err.className = 'err'; err.id = 'pklogerr';
  form.append(sep, btn, err);
  const post = (body) => fetch('/login/passkey', {method: 'POST', credentials: 'same-origin',
    headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)}).then((r) => r.json());
  btn.onclick = async () => {
    err.textContent = ''; btn.disabled = true;
    try {
      const o = await post({step: 'login-options'});
      if (o.error) throw new Error(o.error);
      const c = await navigator.credentials.get({publicKey: {challenge: b64d(o.challenge), rpId: o.rpId,
        userVerification: 'required', timeout: 60000}});
      const v = await post({step: 'login-verify', cid: o.cid, id: c.id, clientData: b64e(c.response.clientDataJSON),
        authData: b64e(c.response.authenticatorData), signature: b64e(c.response.signature)});
      if (v.error) throw new Error(v.error);
      location.href = v.redirect || '/';
    } catch (e) {
      err.textContent = e && e.name === 'NotAllowedError' ? 'Přihlášení passkeyem bylo zrušené, nebo tu žádný passkey není.'
                                                          : (e && e.message) || 'Přihlášení nevyšlo.';
      btn.disabled = false;
    }
  };
})();
</script>"""


# Návod u přihlášení — lidi nevěděli, jaké údaje zadat a kde najdou kód.
LOGIN_HELP = """<details class=help>
<summary>Jak se přihlásit?</summary>
<ol>
<li><b>E-mail</b> je ten, na který ti správce založil účet (stejný jako v pozvánce).</li>
<li><b>Heslo</b> sis zvolil při prvním přihlášení. Zapomenuté heslo obnoví správce
serveru — napiš mu.</li>
<li><b>Kód</b> (šest číslic) ukazuje aplikace na kódy v mobilu, kam sis při prvním
přihlášení naskenoval QR kód: Google Authenticator, Microsoft Authenticator, nebo
na iPhonu aplikace <b>Hesla</b> (sekce Kódy). Účet se tam jmenuje <i>Code Hub</i>
nebo podle tvého e-mailu.</li>
<li>Nemáš telefon? Použij jeden ze záložních kódů, které se ukázaly po zapnutí ověřování.</li>
</ol>
<p>Přihlášení vydrží 90 dní od posledního použití — aktualizace serveru tě neodhlásí.</p>
</details>
<style>
.help{margin-top:14px;font-size:13px;color:inherit;opacity:.85;text-align:left}
.help summary{cursor:pointer;font-weight:600}
.help ol{padding-left:18px;margin:8px 0}.help li{margin:4px 0}.help p{margin:6px 0 0}
</style>"""


def login_page(error=""):
    err = f'<div class=err>{html.escape(error)}</div>' if error else ''
    return _page("Přihlášení — Aladin", f"""
<form class=card method=post action="/login">
<h1>Aladin</h1>
<p class=sub>Přihlaš se ke svému účtu na serveru.</p>
<label>E-mail</label>
<input name=email type=email autocomplete=username autofocus required>
<label>Heslo</label>
<input name=password type=password autocomplete=current-password required>
<button type=submit>Přihlásit se</button>
{err}
{LOGIN_HELP}
</form>
{PASSKEY_LOGIN_JS}""")


PASSKEY_JS = """<script>
(() => {
  const b64d = (s) => Uint8Array.from(atob(s.replace(/-/g, '+').replace(/_/g, '/') + '==='.slice((s.length + 3) % 4)), (c) => c.charCodeAt(0));
  const b64e = (b) => btoa(String.fromCharCode(...new Uint8Array(b))).replace(/\\+/g, '-').replace(/\\//g, '_').replace(/=+$/, '');
  const btn = document.getElementById('pk');
  const err = document.getElementById('pkerr');
  if (!btn) return;
  if (!window.PublicKeyCredential) { btn.hidden = true; return; }
  const ticket = btn.dataset.ticket;
  const post = (body) => fetch('/login/passkey', {method: 'POST', credentials: 'same-origin',
    headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)}).then((r) => r.json());
  async function overit() {
    err.textContent = '';
    btn.disabled = true;
    try {
      const o = await post({ticket, step: 'options'});
      if (o.error) throw new Error(o.error);
      const c = await navigator.credentials.get({publicKey: {challenge: b64d(o.challenge), rpId: o.rpId,
        allowCredentials: o.allow.map((id) => ({type: 'public-key', id: b64d(id)})),
        userVerification: 'preferred', timeout: 60000}});
      const v = await post({ticket, step: 'verify', id: c.id, clientData: b64e(c.response.clientDataJSON),
        authData: b64e(c.response.authenticatorData), signature: b64e(c.response.signature)});
      if (v.error) throw new Error(v.error);
      location.href = v.redirect || '/';
    } catch (e) {
      err.textContent = e && e.name === 'NotAllowedError' ? 'Ověření bylo zrušené — zkus to znovu, nebo opiš kód.'
                                                          : (e && e.message) || 'Ověření nevyšlo.';
      btn.disabled = false;
    }
  }
  btn.onclick = (ev) => { ev.preventDefault(); overit(); };
})();
</script>"""


def code_page(ticket, error="", passkey=False):
    """Druhý krok přihlášení: passkey (když ho účet má), kód z aplikace,
    nebo záložní kód."""
    err = f'<div class=err>{html.escape(error)}</div>' if error else ''
    pk = (f'<button type=button id=pk data-ticket="{html.escape(ticket)}">Ověřit passkey</button>'
          '<div class=err id=pkerr></div>'
          '<p class=hint>Otisk prstu, obličej, PIN nebo bezpečnostní klíč. Nebo opiš kód níž.</p>'
          ) if passkey else ''
    return _page("Ověření — Aladin", f"""
<form class=card method=post action="/login/2fa">
<h1>Ověření</h1>
{pk}
<p class=sub>Opiš šesticiferný kód z aplikace v mobilu
(Google Authenticator, Microsoft Authenticator, na iPhonu aplikace Hesla → Kódy).</p>
<input type=hidden name=ticket value="{html.escape(ticket)}">
<label>Kód</label>
<input name=code inputmode=numeric autocomplete=one-time-code autofocus required
 maxlength=16 placeholder="123 456">
<button type=submit>Ověřit</button>
<p class=hint>Nemáš u sebe telefon? Zadej jeden ze záložních kódů.</p>
{err}
{LOGIN_HELP}
</form>{PASSKEY_JS if passkey else ""}""")


def setup_page(ticket, secret, email, error=""):
    """Při prvním přihlášení: zapnutí dvoufázového ověření."""
    err = f'<div class=err>{html.escape(error)}</div>' if error else ''
    svg = qr.svg(totp.uri(secret, email), quiet=2, scale=5)
    return _page("Dvoufázové ověření — Aladin", f"""
<form class="card wide" method=post action="/login/2fa">
<h1>Zapni dvoufázové ověření</h1>
<p class=sub>Přihlášení na server teď chce kromě hesla i kód z telefonu.</p>
<ol class=steps>
<li>Nainstaluj si do mobilu aplikaci na ověřovací kódy — třeba
<b>Google Authenticator</b> nebo <b>Microsoft Authenticator</b>.</li>
<li>V aplikaci přidej účet a naskenuj tenhle QR kód:</li>
</ol>
<div class=qr>{svg}</div>
<p class=hint>Nejde skenovat? Zadej v aplikaci ručně klíč
<code class=key>{html.escape(totp.grouped(secret))}</code></p>
<ol class=steps start=3><li>Opiš šesticiferný kód, který aplikace ukáže:</li></ol>
<input type=hidden name=ticket value="{html.escape(ticket)}">
<input name=code inputmode=numeric autocomplete=one-time-code autofocus required
 maxlength=8 placeholder="123 456">
<button type=submit>Zapnout a přihlásit</button>
{err}
</form>""")


def recovery_page(codes):
    """Záložní kódy hned po zapnutí ověřování — ukážou se jen tady."""
    items = "".join(f"<code>{html.escape(c)}</code>" for c in codes)
    return _page("Záložní kódy — Aladin", f"""
<div class="card wide">
<h1>Záložní kódy</h1>
<p class=sub>Když ztratíš telefon, přihlásíš se jedním z nich — každý platí
jednou. Ulož si je (správce hesel, vytisknout). Znovu se neukážou.</p>
<div class=codes>{items}</div>
<a class=button href="/">Uloženo, pokračovat</a>
</div>""")


# Veřejné soubory PWA — nic tajného, stejné pro všechny. Berou se ze zdroje
# hubu, ze kterého jede brána.
PWA_FILES = {
    "/manifest.webmanifest": ("application/manifest+json", "no-cache"),
    "/sw.js": ("text/javascript; charset=utf-8", "no-cache"),
    "/favicon.ico": ("image/x-icon", "public, max-age=86400"),
    "/icon-32.png": ("image/png", "public, max-age=86400"),
    "/icon-192.png": ("image/png", "public, max-age=86400"),
    "/icon-256.png": ("image/png", "public, max-age=86400"),
    "/icon-512.png": ("image/png", "public, max-age=86400"),
    "/icon-512-maskable.png": ("image/png", "public, max-age=86400"),
}
PWA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "hub", "static")


# ── HTTP handler ──────────────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    server_version = "ClaudeHubGateway"
    protocol_version = "HTTP/1.1"

    def log_message(self, *_a):
        pass

    # ---- pomůcky ----
    @property
    def accounts(self):
        return self.server.accounts

    @property
    def hubs(self):
        return self.server.hubs

    def _cookies(self):
        jar = {}
        for part in self.headers.get("Cookie", "").split(";"):
            name, _, value = part.strip().partition("=")
            if name:
                jar[name] = urllib.parse.unquote(value)
        return jar

    def _user(self):
        token = self._cookies().get(config.SESSION_COOKIE, "")
        return self.accounts.user_for_token(token) if token else None

    def _https(self):
        return (self.headers.get("X-Forwarded-Proto", "") == "https"
                or self.server.assume_https)

    def _public_host(self):
        return (self.headers.get("X-Forwarded-Host")
                or self.headers.get("Host", "") or "")

    def _same_origin(self):
        """Pro POST akce: Origin/Referer musí být tahle stránka (proti CSRF)."""
        host = self._public_host()
        src = self.headers.get("Origin") or self.headers.get("Referer") or ""
        if not src:
            return False
        return urllib.parse.urlsplit(src).netloc == host

    def _send(self, code, body=b"", ctype="text/plain; charset=utf-8", extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if body and self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False),
                   "application/json; charset=utf-8")

    def _fail(self, code, message):
        """Chyba pro člověka: navigaci prohlížeče stylovaná stránka, volání
        z hubu JSON, ostatním (WebSocket, curl) prostý text. Stavový kód
        zůstává, jak ho volající poslal."""
        accept = self.headers.get("Accept") or ""
        if self.command == "GET" and "text/html" in accept:
            return self._send(code, error_page(message), HTML)
        if self._wants_json():
            return self._json({"error": message}, code)
        return self._send(code, message)

    def _redirect(self, where, extra=None):
        self._send(303, b"", extra={"Location": where, **(extra or {})})

    def _set_cookie(self, token):
        flags = "; Secure" if self._https() else ""
        return (f"{config.SESSION_COOKIE}={urllib.parse.quote(token)}; Path=/; "
                f"HttpOnly; SameSite=Lax; Max-Age={config.SESSION_MAX_AGE}{flags}")

    def _clear_cookie(self):
        return f"{config.SESSION_COOKIE}=; Path=/; HttpOnly; Max-Age=0"

    # ---- routování ----
    def do_GET(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")

    def do_PUT(self):
        self._route("PUT")

    def do_DELETE(self):
        self._route("DELETE")

    def do_HEAD(self):
        self._route("GET")

    def _route(self, method):
        try:
            parsed = urllib.parse.urlparse(self.path)
            route = parsed.path

            # Soubory pro instalaci na plochu (PWA). Prohlížeč si manifest
            # i ikony stahuje bez přihlašovací cookie — kdyby chtěly přihlášení,
            # Android by instalaci nenabídl a iPhone by neměl ikonu.
            # Manifest a ikony s vlastním názvem a ikonou účtu (?u=kód,
            # /app-ikona/<kód>/…) — bez přihlášení, podle tajného kódu.
            if route == "/manifest.webmanifest" and "u=" in parsed.query:
                return self._pwa_user_manifest(parsed.query)
            if route.startswith("/app-ikona/") and route.count("/") == 3:
                return self._pwa_user_icon(route)
            if route in PWA_FILES:
                return self._pwa_file(route)
            # Napojení z appky Claude (MCP + OAuth): vlastní přihlášení
            # a tokeny, cookie brány se tu nepoužívá (gateway/mcp.py).
            if mcp.route(self, method, route):
                return
            if route == "/login":
                return self._login(method)
            if route == "/logout":
                return self._logout()
            if route == "/login/2fa":
                return self._login_second(method)
            if route == "/login/passkey":
                return self._login_passkey(method)
            if route == "/zarizeni":
                return self._zarizeni_stranka(method)
            if route == "/gw/zarizeni":
                return self._zarizeni_appka(method)
            # Rozhraní pro hub běžící na počítači: ověřuje se tokenem
            # v hlavičce, ne cookie, a nikdy se neproxuje do instance.
            if route == "/gw/info":
                return self._gw_info()
            if route == "/gw/me":
                return self._gw_me()
            if route == "/gw/handoff":
                return self._gw_handoff(method)
            # Most na počítač: počítač se ptá tokenem zařízení, prostor volá
            # svým žetonem. Ani jedno nejde přes cookie a nic z toho se neproxuje.
            if route == "/gw/pocitac/poll":
                return self._pocitac_poll(method)
            if route == "/gw/pocitac/vysledek":
                return self._pocitac_reply(method)
            if route == "/gw/pocitac/schvaleni":
                return self._pocitac_schvaleni(method)
            if route == "/gw/pocitac/volani":
                return self._pocitac_call(method)
            if route == "/gw/prostor/volani":
                return self._prostor_volani(method)
            if route == "/gw/pocitac/ukol":
                return self._pocitac_ukol(method)
            # Sdílená napojení: most v prostoru (tools/sdilene_mcp.py) posílá
            # zprávy žetonem prostoru, jen na loopback.
            if route == "/gw/mcp-sdilene/volani":
                return self._mcp_shared_call(method)
            # Přihlášení Clauda v prostoru: appka tokenem zařízení, prostor
            # (prohlížeč) cookie — obojí řeší _gw_claude.
            if route == "/gw/claude":
                return self._gw_claude(method)
            # Přepis řeči pro hub na počítači (hub/hlas.py): notebook by přesný
            # model počítal dlouho, server ho má za pár vteřin. Token zařízení.
            if route == "/gw/hlas/prepis":
                return self._gw_hlas(method)
            # Chat z počítače na server (hub/prenos.py): token zařízení, balíček po kusech.
            if route == "/gw/prenos/nahrat":
                return self._gw_prenos(method)
            # Napojení ze serveru v Claudovi na počítači (gateway/mcp_pc.py): token zařízení.
            if route in ("/gw/mcp-pc", "/gw/mcp-pc/volani"):
                return self._gw_mcp_pc(route, method)
            # Počítač počítá, kopie chatů je tady (gateway/zrcadlo.py): token zařízení.
            if route in ("/gw/zrcadlo/sync", "/gw/zrcadlo/seznam", "/gw/zrcadlo/prevzit",
                         "/gw/zrcadlo/stahnout"):
                return self._gw_zrcadlo(route, method)
            # Spotřeba z počítače (hub/spotreba_pc.py, token zařízení) a stopka
            # pro hub a hook v prostoru (žeton prostoru, jen loopback).
            if route == "/gw/spotreba/pocitac":
                return self._spotreba_pocitac(method)
            if route == "/gw/spotreba/stopka":
                return self._spotreba_stopka()

            user = self._user()

            if not user:
                # Navigaci pošli na přihlášení, API/WS ať dostane jasné 401.
                if method == "GET" and "text/html" in self.headers.get("Accept", ""):
                    return self._redirect("/login")
                return self._fail(401, "Nejsi přihlášený. Přihlas se prosím znovu.")

            if route == "/gw/firma/publish":
                return self._firma_publish(method, user)
            if route == "/gw/firma/pristupy":
                return self._firma_access(method, user)
            if route == "/gw/firma/poznamky":
                return self._firma_notes(method, user)
            if route == "/gw/firma/soubor":
                return self._firma_file(method, user, parsed.query)
            if route == "/gw/firmy":
                return self._gw_firmy(method, user)
            if route == "/gw/firma/smazat":
                return self._firma_delete(method, user)
            # Zabezpečení účtu z Nastavení → Účet (hub v prostoru o heslech nic neví).
            if route == "/gw/account":
                return self._json({"user": user, "twofa": self.accounts.twofa(user["id"]),
                                   "required": config.REQUIRE_2FA})
            if route == "/gw/password":
                return self._gw_password(method, user)
            if route == "/gw/2fa/recovery":
                return self._gw_recovery(method, user)
            if route == "/gw/passkey":
                return self._gw_passkey(method, user)
            if route == "/gw/sdilene":
                if method == "POST":
                    return self._gw_shared_change(user)
                return self._gw_shared(user)
            if route == "/gw/relace/soubor":
                return self._relace_soubor(method, user)
            if route in ("/gw/relace", "/gw/relace/cteni", "/gw/relace/poslat", "/gw/relace/priloha", "/gw/relace/pokracovat",
                        "/gw/relace/zapnout", "/gw/relace/pritomnost"):
                return self._gw_relace(route, method, user)
            if route == "/gw/tym":
                return self._gw_team(method, user)
            if route == "/gw/pristup":
                return self._gw_pristup(method, user)
            if route == "/gw/pristup/predplatne":
                return self._gw_pristup_predplatne(method, user)
            if route == "/gw/spotreba":
                return self._gw_spotreba(method, user)
            # Místo na disku po lidech a zvonek s oznámeními (uloziste.py, oznameni.py).
            if route == "/gw/uloziste":
                return self._gw_uloziste(method, user)
            if route in ("/gw/oznameni", "/gw/oznameni/admin"):
                return self._gw_oznameni(route, method, user)
            if route == "/gw/mcp-sdilene":
                return self._mcp_shared(method, user)
            if route == "/gw/restart":
                return self._gw_restart(method, user)
            if route == "/gw/verze":
                return self._gw_verze(user)
            if route in ("/gw/pocitac/rozhodnout", "/gw/pocitac/pristup"):
                return self._pocitac_clovek(route, method, user)
            if route == "/gw/pocitac":
                # Které počítače jsou k prostoru připojené — štítek v hlavičce —
                # a kde jsou úkoly, které jim Claude nechal na později.
                return self._json({"computers": self.server.pocitac.list(user["id"]),
                                   "ukoly": self.server.pocitac.ukoly(user["id"])})

            # Nástroje pro appku Claude volá v instanci jen brána sama — přes
            # proxy z prohlížeče se k nim nikdo nedostane.
            if route.startswith(("/api/mcp-tool", "/api/predplatne-host", "/api/web-host")):
                return self._send(404, b"404")
            # Host v cizím prostoru (gateway/pristup.py): povolení se ověřuje
            # při každém požadavku, role rozhoduje, kam smí.
            if self._cookies().get(pristup.COOKIE):
                return self._proxy_guest(method, user, route, parsed.query)
            # Přihlášený → všechno ostatní jde do jeho instance hubu.
            return self._proxy(method, user)
        except BrokenPipeError:
            pass
        except Exception as exc:
            _errlog("route " + self.path, exc)
            try:
                self._fail(502, MSG_BROKEN)
            except Exception:
                pass

    _pwa_ids = {"at": 0, "map": {}}

    def _pwa_user(self, pid):
        """Účet podle kódu z adresy (workspace.pwa_id); seznam se přepočítá
        nejvýš jednou za minutu."""
        import re as _re
        if not _re.fullmatch(r"[0-9a-f]{24}", pid or ""):
            return None
        cache = self._pwa_ids
        if time.time() - cache["at"] > 60 or pid not in cache["map"]:
            cache["map"] = {workspace.pwa_id(u): u["id"] for u in self.accounts.list()
                            if not u.get("disabled")}
            cache["at"] = time.time()
        uid = cache["map"].get(pid)
        return self.accounts.by_id(uid) if uid else None

    def _pwa_user_manifest(self, query):
        # Název a ikona jsou pro všechny stejné (hub/vzhled.py) — dřív uložené
        # vlastní se už nečtou. Adresy s kódem účtu zůstávají kvůli appkám,
        # které už jsou na ploše nainstalované.
        return self._pwa_file("/manifest.webmanifest")

    def _pwa_user_icon(self, route):
        import re as _re
        m = _re.fullmatch(r"/app-ikona/([0-9a-f]{24})/(512|192|32)\.png", route)
        if not m:
            return self._send(404, b"404")
        return self._pwa_file(f"/icon-{m.group(2)}.png")

    def _pwa_file(self, route):
        ctype, cache = PWA_FILES[route]
        try:
            with open(os.path.join(PWA_DIR, route.lstrip("/")), "rb") as fh:
                body = fh.read()
        except OSError:
            return self._send(404, b"404")
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    # ---- firemní Obsidian ----
    def _firma_publish(self, method, user):
        """Nahrání návrhu do firemního Obsidianu — jen tlačítkem v hubu.

        Rozhoduje člověk v prohlížeči, ne Claude v prostoru: požadavek musí
        přijít z téhle stránky, s přihlašovací cookie (do prostoru se
        nepřeposílá, viz _forward_headers) a s hlavičkou, kterou cizí web
        bez povolení poslat nemůže.
        """
        if method != "POST":
            return self._json({"error": "Jen POST."}, 405)
        if not self._same_origin() or self.headers.get("X-Hub-Firma") != "1":
            return self._json({"error": "Nahrát jde jen tlačítkem v aplikaci."}, 403)
        form = self._read_form()
        try:
            result = workspace.apply_proposal(user, form.get("id", ""),
                                              form.get("prepsat") is True)
        except ValueError as exc:
            return self._json({"error": str(exc)}, 400)
        except OSError as exc:
            _errlog("firma publish", exc)
            return self._json({"error": MSG_SAVE}, 500)
        # Odebraní ze sdíleného Obsidianu: kdo zrovna nepracuje, tomu se prostor
        # zastaví hned (přístup zmizí); ostatním při dalším startu.
        if self.hubs:
            for uid in result.get("revoked") or []:
                self.hubs.stop_idle(uid)
        result = {k: v for k, v in result.items() if k not in ("affected", "revoked")}
        return self._json(result)

    def _firma_access(self, method, user):
        """Kdo firemní Obsidian vidí a kdo do něj zapisuje — spravují admini
        ve firemním Obsidianu v hubu. Seznam i změna jen pro admina; změna jen
        ze stránky hubu (stejný původ + hlavička, kterou cizí web nepošle)."""
        if user.get("role") != "admin":
            return self._json({"error": "Přístupy k firemnímu Obsidianu spravují admini."}, 403)
        if method == "GET":
            return self._json({"people": self.accounts.company_list()})
        if method != "POST":
            return self._json({"error": "Jen GET nebo POST."}, 405)
        form = self._read_form()
        if not self._account_post_ok():
            return self._json({"error": "Přístupy jde měnit jen v aplikaci."}, 403)
        try:
            target, old = self.accounts.set_company_level(
                user, form.get("id"), str(form.get("level") or ""))
        except PermissionError as exc:
            return self._json({"error": str(exc)}, 403)
        except (ValueError, TypeError) as exc:
            return self._json({"error": str(exc)}, 400)
        level = self.accounts.company_level(target)
        note = ""
        if level == "none" and old != "none" and self.hubs:
            # Trezor je v běžícím prostoru přivázaný — kdo zrovna nepracuje,
            # tomu se prostor zastaví hned, ostatním při dalším startu.
            self.hubs.stop_idle(target["id"])
            note = "trezor mu zmizí, až se jeho prostor znovu spustí"
        elif old == "none" and level != "none":
            note = "uvidí ho po restartu svého prostoru"
        try:
            with open(os.path.join(config.COMPANY_DIR, "pristupy.jsonl"), "a",
                      encoding="utf-8") as fh:
                fh.write(json.dumps({"cas": time.strftime("%Y-%m-%d %H:%M:%S"),
                                     "admin": user["email"], "komu": target["email"],
                                     "z": old, "na": level}, ensure_ascii=False) + "\n")
        except OSError:
            pass
        return self._json({"ok": True, "level": level, "note": note})

    def _firma_file(self, method, user, query):
        """Nahrání souboru do firemního nebo sdíleného Obsidianu (gateway/soubory.py).

        Tělo je soubor sám, cíl v adrese: `vault` (firma | sdilene:<zkratka>),
        `cesta` (relativně k trezoru), `prepsat=1`. Jen člověk ze stránky hubu —
        cookie brány do prostoru nechodí, hlavička X-Hub-Firma z cizího webu
        neprojde. Velikost se hlídá před čtením těla."""
        from . import soubory
        if method != "POST":
            return self._json({"error": "Jen POST."}, 405)
        if not self._same_origin() or self.headers.get("X-Hub-Firma") != "1":
            return self._json({"error": "Nahrát soubor jde jen v hubu."}, 403)
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if length <= 0:
            return self._json({"error": "Soubor je prázdný."}, 400)
        if length > soubory.UPLOAD_MAX:
            self.close_connection = True
            return self._json({"error": f"Soubor je větší než "
                                        f"{soubory.UPLOAD_MAX // (1024 * 1024)} MB."}, 413)
        q = urllib.parse.parse_qs(query)
        vault = (q.get("vault") or [""])[0]
        rel = (q.get("cesta") or [""])[0]
        overwrite = (q.get("prepsat") or [""])[0] == "1"
        raw = self.rfile.read(length)
        if len(raw) != length:
            return self._json({"error": "Soubor nedorazil celý."}, 400)
        try:
            if vault == "firma" or vault.startswith("firma:"):
                firma = vault[len("firma:"):] if vault.startswith("firma:") else firmy.LEGACY
                if not firmy.exists(firma) or not firmy.role(user, firma):
                    return self._json({"error": "K firemnímu Obsidianu nemáš přístup."}, 403)
                result = workspace.upload_company(user, rel, raw, overwrite, firma=firma)
            elif vault.startswith("sdilene:"):
                result = shared.upload(user, vault[len("sdilene:"):], rel, raw, overwrite)
            else:
                return self._json({"error": "Neznámý Obsidian."}, 400)
        except ValueError as exc:
            return self._json({"error": str(exc)}, 400)
        except OSError as exc:
            _errlog("nahrání souboru", exc)
            return self._json({"error": f"Uložit se nepodařilo: {exc}"}, 500)
        if not result.get("ok"):
            return self._json({**result, "error": "Soubor s tímhle jménem už tam je."}, 409)
        result.pop("sha256", None)
        return self._json(result)

    def _firma_notes(self, method, user):
        """Kdo vidí kterou poznámku firemního Obsidianu (gateway/poznamky.py).

        GET vrátí každému, jestli je správce poznámek a jestli se od startu
        jeho prostoru přístupy změnily (`zmena` → hub nabídne restart).
        Správci navíc dostanou omezené poznámky a lidi. POST {cesta, emaily}
        jen správce, jen ze stránky hubu; prázdné `emaily` = vidí všichni.
        Firma (gateway/firmy.py) v `?firma=` nebo poli `firma`, bez ní dnešní."""
        form = self._read_form() if method == "POST" else {}
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        firma = str(form.get("firma") or (query.get("firma") or [firmy.LEGACY])[0])
        if not firmy.exists(firma) or not firmy.role(user, firma):
            return self._json({"error": "K firemnímu Obsidianu nemáš přístup."}, 403)
        manager = poznamky.is_manager(user, firma)
        if method == "GET":
            proc = self.hubs.procs.get(user["id"]) if self.hubs else None
            # Cizí Lidé/ a Sdílené/ se nepočítají: nový kolega nebo sdílený
            # trezor jiných lidí restart nepotřebuje (Sdílené hlídá svůj panel).
            firm = lambda paths: sorted(p for p in paths if not slozky.special(p))
            changed = bool(proc and proc.alive()
                           and firm(proc.hidden.get(firma, []))
                           != firm(poznamky.hidden_for(user, firma=firma)))
            out = {"spravce": manager, "zmena": changed}
            if manager:
                out.update(poznamky=poznamky.listing(firma), slozky=poznamky.folders(firma),
                           lide=poznamky.people(firma))
            return self._json(out)
        if method != "POST":
            return self._json({"error": "Jen GET nebo POST."}, 405)
        if not self._account_post_ok():
            return self._json({"error": "Přístupy k poznámkám jde měnit jen v hubu."}, 403)
        emails = form.get("emaily") or []
        if not isinstance(emails, (list, str)):
            return self._json({"error": "Seznam lidí je poškozený."}, 400)
        try:
            only = form.get("jen")
            result = poznamky.set_note(user, form.get("cesta"), emails,
                                       None if only is None else bool(only), firma=firma)
        except PermissionError as exc:
            return self._json({"error": str(exc)}, 403)
        except ValueError as exc:
            return self._json({"error": str(exc)}, 400)
        stopped = 0
        if self.hubs:
            # Kdo poznámku přestal vidět a zrovna nepracuje, tomu se prostor
            # zastaví hned; ostatním zmizí (nebo přibude) při dalším startu.
            for uid in result["revoked"]:
                stopped += bool(self.hubs.stop_idle(uid))
        waiting = len(result["revoked"]) - stopped + len(result["granted"])
        note = f"{waiting} lidem se to projeví po restartu jejich prostoru" if waiting else ""
        return self._json({"ok": True, "path": result["path"], "jen": result["jen"],
                           "people": result["people"], "note": note})

    def _firma_delete(self, method, user):
        """Smazání poznámky z trezoru firmy — jen tlačítkem v hubu (stejný původ,
        cookie brány a X-Hub-Firma, jako Nahrát). Claude v prostoru mazat nemůže."""
        if method != "POST":
            return self._json({"error": "Jen POST."}, 405)
        if not self._same_origin() or self.headers.get("X-Hub-Firma") != "1":
            return self._json({"error": "Smazat jde jen tlačítkem v aplikaci."}, 403)
        form = self._read_form()
        try:
            result = workspace.delete_company(user, form.get("cesta"), str(form.get("firma") or firmy.LEGACY))
        except ValueError as exc:
            return self._json({"error": str(exc)}, 400)
        except OSError as exc:
            _errlog("smazání ve firmě", exc)
            return self._json({"error": MSG_SAVE}, 500)
        return self._json(result)

    # ---- firmy (gateway/firmy.py) ----
    def _gw_firmy(self, method, user):
        """Nastavení → Firmy: firmy člověka, a kapitánům lidé, klienti,
        podkategorie a kdo kam vidí. Změny jen ze stránky hubu; kdo co smí,
        rozhoduje firmy.py. Kdo přístup ztratil a zrovna nepracuje, tomu se
        prostor zastaví hned, ostatním se změna projeví po restartu."""
        if method == "GET":
            mine = firmy.firmy_for(user)
            proc = self.hubs.procs.get(user["id"]) if self.hubs else None
            changed = False
            if proc and proc.alive():
                now = {f["zkratka"]: sorted(p for p in poznamky.hidden_for(user, firma=f["zkratka"])
                                            if not slozky.special(p)) for f in mine}
                was = {k: sorted(p for p in v if not slozky.special(p))
                       for k, v in proc.hidden.items()}
                changed = now != was
            captain = [f["zkratka"] for f in mine if f["role"] == "kapitan"]
            out = {"firmy": mine, "zakladat": firmy.muze_zakladat(user), "zmena": changed,
                   "prehled": {slug: firmy.prehled(user, slug) for slug in captain}}
            if captain:
                out["ucty"] = [{"id": r["id"], "email": r["email"], "name": r["name"] or ""}
                               for r in self.accounts.list() if not r["disabled"]]
            return self._json(out)
        if method != "POST":
            return self._json({"error": "Jen GET nebo POST."}, 405)
        form = self._read_form()
        if not self._account_post_ok():
            return self._json({"error": "Firmy jde měnit jen v hubu."}, 403)
        action = str(form.get("akce") or "")
        slug = str(form.get("firma") or "")
        try:
            if action == "zalozit":
                result = firmy.zalozit(user, form.get("nazev"),
                                       druh_firmy=str(form.get("druh") or "klienti"))
            elif action == "smazat-firmu":
                result = firmy.smazat_firmu(user, slug)
            elif action == "druh":
                result = firmy.nastavit_druh(user, slug, str(form.get("druh") or ""))
            elif action == "nazev":
                result = firmy.prejmenovat_firmu(user, slug, form.get("nazev"))
            elif action == "klienti":
                result = firmy.nastavit_klienty(user, slug, bool(form.get("zapnout")))
            elif action == "clovek":
                result = firmy.nastavit_cloveka(user, slug, form.get("id"),
                                                str(form.get("role") or ""), bool(form.get("mazat")))
            elif action == "mazani":
                result = firmy.nastavit_mazani(user, slug, form.get("id"), bool(form.get("ano")))
            elif action == "pozvat":
                from . import tym
                result = tym.pozvat_do_firmy(self.accounts, user, slug, form)
            elif action == "slozka":
                result = firmy.zalozit_slozku(user, slug, form.get("klient"),
                                              form.get("podkategorie") or "")
            elif action == "pristup":
                result = firmy.nastavit_pristup(user, slug, form.get("cesta"), form.get("id"),
                                                bool(form.get("ano")))
            elif action == "prejmenovat":
                result = firmy.prejmenovat_slozku(user, slug, form.get("cesta"), form.get("nazev"))
            elif action == "smazat":
                result = firmy.smazat_slozku(user, slug, form.get("cesta"))
            else:
                return self._json({"error": "Neznámá akce."}, 400)
        except PermissionError as exc:
            return self._json({"error": str(exc)}, 403)
        except (ValueError, TypeError) as exc:
            return self._json({"error": str(exc)}, 400)
        except OSError as exc:
            _errlog("firmy " + action, exc)
            return self._json({"error": MSG_SAVE}, 500)
        stopped = 0
        if self.hubs:
            for uid in result.get("revoked") or []:
                if uid != user["id"]:
                    stopped += bool(self.hubs.stop_idle(uid))
        result = {k: v for k, v in result.items() if k not in ("affected", "revoked")}
        return self._json(result)

    # ---- přihlášení ----
    def _wants_json(self):
        return ("application/json" in (self.headers.get("Content-Type") or "")
                or "application/json" in (self.headers.get("Accept") or ""))

    def _login(self, method):
        if method != "POST":
            # Předané přihlášení z hubu na počítači: kód se vymění za cookie.
            # Musí to být obyčejná navigace GETem, jinak cookie se SameSite=Lax
            # prohlížeč neuloží.
            code = urllib.parse.parse_qs(
                urllib.parse.urlparse(self.path).query).get("handoff", [""])[0]
            if code:
                token = _handoff_take(code)
                # Token se ověřuje až teď, ne při vydání kódu: mezi tím mohl
                # být účet zablokovaný nebo zařízení odhlášené.
                if not token or not self.accounts.user_for_token(token):
                    return self._send(200, login_page(
                        "Odkaz na přihlášení už platnost ztratil. "
                        "Zkus to z appky znovu."), "text/html; charset=utf-8")
                return self._redirect(
                    "/", extra={"Set-Cookie": self._set_cookie(token)})
            return self._send(200, login_page(),
                              "text/html; charset=utf-8")
        form = self._read_form()
        email = (form.get("email") or "").strip()
        password = form.get("password") or ""
        label = self.headers.get("User-Agent", "")[:60]
        keys = self._fail_keys(email)
        wait = self.accounts.fail_wait(keys)
        if wait:
            return self._login_error(too_many(wait), 429)
        user = self.accounts.verify(email, password)
        if not user:
            locked, left = self._failed(keys)
            if locked:
                return self._login_error("Špatný e-mail nebo heslo. " + locked, 429)
            return self._login_error("Špatný e-mail nebo heslo. " + tries_left(left))
        state = self.accounts.twofa(user["id"])
        if not state["enabled"] and not config.REQUIRE_2FA:
            return self._login_done(self.accounts.issue_token(user, label), user)
        if state["enabled"]:
            ticket = _ticket_new(user["id"], label)
            if self._wants_json():
                return self._json({"need": "totp", "ticket": ticket})
            return self._send(200, code_page(ticket, passkey=state["passkeys"] > 0), HTML)
        # Ověřování ještě nemá: tajemství vznikne teď a do účtu se zapíše, až
        # ho člověk potvrdí prvním kódem z aplikace.
        secret = totp.new_secret()
        ticket = _ticket_new(user["id"], label, secret)
        if self._wants_json():
            return self._json({"need": "setup", "ticket": ticket, "secret": secret,
                               "uri": totp.uri(secret, user["email"])})
        return self._send(200, setup_page(ticket, secret, user["email"]), HTML)

    def _client_ip(self):
        # nginx před bránou posílá skutečnou adresu v X-Real-IP; brána sama
        # poslouchá jen na loopbacku, takže ji odjinud nikdo nepodvrhne.
        return self.headers.get("X-Real-IP") or self.client_address[0]

    def _fail_keys(self, email=""):
        """[(klíč, limit)]: přísně účet z téhle adresy, volněji adresa a účet zvlášť.
        Přísný klíč je vždycky první — ten se po přihlášení maže."""
        ip = "ip:" + self._client_ip()
        email = (email or "").strip().lower()
        if not email:
            return [(ip, config.LOGIN_WIDE)]
        return [(f"{ip}|email:{email}", config.LOGIN_TRIES), (ip, config.LOGIN_WIDE),
                ("email:" + email, config.LOGIN_WIDE)]

    def _failed(self, keys):
        """Zapíše neúspěch. Vrací (sekund do odemčení, kolik pokusů zbývá) a hlášku
        o zámku: „právě zamčeno", nebo „pořád zamčeno" (zámek z jiného klíče)."""
        wait, left, locked = self.accounts.fail_record(keys, config.LOGIN_LOCK,
                                                       config.LOGIN_LOCK)
        # Pro fail2ban (filtr claude-hub-brana): zámek v databázi drží hádání
        # hesel u jednoho účtu, tohle nechá bránu blokovat celou adresu na
        # firewallu, ať se na server nebuší dál.
        print(f"neúspěšné přihlášení z {self._client_ip()}", flush=True)
        if locked:
            print(f"přihlášení zablokováno na {config.LOGIN_LOCK // 60} min: "
                  + ", ".join(locked), flush=True)
        note = (locked_now(wait) if locked else too_many(wait)) if wait else ""
        return note, left

    def _login_error(self, message, code=401):
        if self._wants_json():
            return self._json({"error": message}, code)
        return self._send(200, login_page(message), HTML)

    def _login_done(self, token, user, recovery=None):
        # Povedlo se: překlepy před tím se tomuhle účtu z téhle adresy odpouští.
        self.accounts.fail_clear(self._fail_keys(user["email"])[0][0])
        if self._wants_json():
            # Hub na počítači si token uloží sám; cookie by mu byla k ničemu.
            out = {"token": token, "user": user}
            if recovery:
                out["recovery"] = recovery
            return self._json(out)
        cookie = {"Set-Cookie": self._set_cookie(token)}
        if recovery:
            return self._send(200, recovery_page(recovery), HTML, cookie)
        return self._redirect(self._cil_po_prihlaseni(), extra=cookie)

    def _login_second(self, method):
        """Druhý krok: kód z aplikace (nebo záložní), při prvním přihlášení
        zapnutí ověřování potvrzené prvním kódem."""
        if method != "POST":
            return self._redirect("/login")
        form = self._read_form()
        ticket_id = form.get("ticket") or ""
        code = str(form.get("code") or "")
        ticket = _ticket_get(ticket_id)
        user = self.accounts.by_id(ticket["uid"]) if ticket else None
        if not user:
            _ticket_drop(ticket_id)
            return self._login_error("Přihlášení vypršelo — zadej znovu e-mail a heslo.")
        keys = self._fail_keys(user["email"])
        wait = self.accounts.fail_wait(keys)
        if wait:
            _ticket_drop(ticket_id)
            return self._login_error(too_many(wait), 429)
        recovery = None
        if ticket["secret"]:
            ok = bool(totp.verify(ticket["secret"], code))
            if ok:
                recovery = self.accounts.enable_totp(user["id"], ticket["secret"])
                # Kód, kterým se nastavení potvrdilo, ať už podruhé neprojde.
                self.accounts.second_factor(user["id"], code, allow_recovery=False)
        else:
            ok = bool(self.accounts.second_factor(user["id"], code))
        if not ok:
            locked, left = self._failed(keys)
            left = min(left, _ticket_miss(ticket_id))
            if locked:
                _ticket_drop(ticket_id)
                return self._login_error("Kód nesedí. " + locked, 429)
            message = "Kód nesedí." + (" " + tries_left(left) if left
                                       else " Přihlas se znovu.")
            if self._wants_json():
                return self._json({"error": message, "left": left}, 401)
            if not left:
                return self._send(200, login_page(message), HTML)
            page = (setup_page(ticket_id, ticket["secret"], user["email"], message)
                    if ticket["secret"] else
                    code_page(ticket_id, message, passkey=self.accounts.twofa(user["id"])["passkeys"] > 0))
            return self._send(200, page, HTML)
        _ticket_drop(ticket_id)
        token = self.accounts.issue_token(user, ticket["label"], mfa=True)
        return self._login_done(token, user, recovery)

    # ---- passkey (WebAuthn) ----
    def _rp(self):
        """(rp_id, origin) podle adresy, na které stránku člověk má."""
        host = self._public_host()
        proto = "https" if self._https() else "http"
        rp_id = host[1:host.index("]")] if host.startswith("[") else host.rsplit(":", 1)[0]
        return rp_id, f"{proto}://{host}"

    def _login_passkey(self, method):
        """Druhý krok přihlášení passkeyem: výzva (`options`), pak ověření
        podpisu (`verify`). Lístek z prvního kroku (heslo) je podmínka."""
        if method != "POST" or not self._same_origin():
            return self._json({"error": "Jen ze stránky přihlášení."}, 403)
        form = self._read_form()
        if form.get("step") in ("login-options", "login-verify"):
            return self._passkey_login(form)
        ticket_id = str(form.get("ticket") or "")
        ticket = _ticket_get(ticket_id)
        user = self.accounts.by_id(ticket["uid"]) if ticket else None
        if not user:
            return self._json({"error": "Přihlášení vypršelo — zadej znovu e-mail a heslo."}, 401)
        keys = self._fail_keys(user["email"])
        wait = self.accounts.fail_wait(keys)
        if wait:
            _ticket_drop(ticket_id)
            return self._json({"error": too_many(wait)}, 429)
        rp_id, origin = self._rp()
        if form.get("step") == "options":
            allow = [k["id"] for k in self.accounts.passkeys(user["id"])]
            if not allow:
                return self._json({"error": "Účet nemá žádný passkey."}, 400)
            ticket["pk_challenge"] = os.urandom(32)
            return self._json({"challenge": webauthn.b64e(ticket["pk_challenge"]), "rpId": rp_id,
                               "allow": allow})
        challenge = ticket.pop("pk_challenge", None)
        cred = self.accounts.passkey(str(form.get("id") or ""))
        try:
            if not challenge or not cred or cred["user_id"] != user["id"]:
                raise webauthn.Chyba("neznámý klíč nebo chybí výzva")
            count = webauthn.prihlaseni(
                webauthn.b64d(form.get("clientData")), webauthn.b64d(form.get("authData")),
                webauthn.b64d(form.get("signature")), challenge, origin, rp_id,
                cred["public_key"], cred["sign_count"])
        except (webauthn.Chyba, ValueError, TypeError, KeyError) as exc:
            _errlog("passkey přihlášení", exc)
            locked, left = self._failed(keys)
            left = min(left, _ticket_miss(ticket_id))
            return self._json({"error": "Passkey nesedí. " + (locked or tries_left(left))}, 401)
        _ticket_drop(ticket_id)
        self.accounts.passkey_used(cred["id"], count)
        self.accounts.fail_clear(keys[0][0])
        token = self.accounts.issue_token(user, ticket["label"], mfa=True)
        return self._send(200, json.dumps({"ok": True, "redirect": "/"}),
                          "application/json; charset=utf-8", {"Set-Cookie": self._set_cookie(token)})

    def _passkey_login(self, form):
        """Přihlášení jen passkeyem, bez hesla: klíč s ověřením člověka na
        zařízení (otisk, obličej, PIN) je zároveň vlastnictví i znalost, takže
        platí za celé přihlášení včetně druhého kroku. Passkey se hledá podle
        id, které autentizátor pošle (objevitelný klíč) — nikdo neříká, jaký
        e-mail zkouší."""
        rp_id, origin = self._rp()
        keys = self._fail_keys("")
        wait = self.accounts.fail_wait(keys)
        if wait:
            return self._json({"error": too_many(wait)}, 429)
        now = time.time()
        if form.get("step") == "login-options":
            with _ticket_lock:
                for k, (_, exp) in list(_pk_login.items()):
                    if exp < now:
                        _pk_login.pop(k, None)
                if len(_pk_login) > 2000:
                    return self._json({"error": "Server je teď zahlcený, zkus to za chvíli."}, 429)
                cid, challenge = secrets.token_urlsafe(18), os.urandom(32)
                _pk_login[cid] = (challenge, now + 180)
            return self._json({"cid": cid, "challenge": webauthn.b64e(challenge), "rpId": rp_id})
        with _ticket_lock:
            challenge, exp = _pk_login.pop(str(form.get("cid") or ""), (None, 0))
        cred = self.accounts.passkey(str(form.get("id") or ""))
        user = self.accounts.by_id(cred["user_id"]) if cred else None
        try:
            if not challenge or exp < now or not cred or not user or user.get("disabled"):
                raise webauthn.Chyba("neznámý klíč, vypršelá výzva nebo zakázaný účet")
            count = webauthn.prihlaseni(
                webauthn.b64d(form.get("clientData")), webauthn.b64d(form.get("authData")),
                webauthn.b64d(form.get("signature")), challenge, origin, rp_id,
                cred["public_key"], cred["sign_count"], uv=True)
        except (webauthn.Chyba, ValueError, TypeError, KeyError) as exc:
            _errlog("passkey přihlášení bez hesla", exc)
            locked, left = self._failed(keys)
            return self._json({"error": "Passkey nesedí. " + (locked or tries_left(left))}, 401)
        self.accounts.passkey_used(cred["id"], count)
        self.accounts.fail_clear(keys[0][0])
        token = self.accounts.issue_token(user, self.headers.get("User-Agent", "")[:60], mfa=True)
        return self._send(200, json.dumps({"ok": True, "redirect": self._cil_po_prihlaseni()}),
                          "application/json; charset=utf-8", {"Set-Cookie": self._set_cookie(token)})

    # ---- přihlášení appky na počítači přes prohlížeč ----
    def _cil_po_prihlaseni(self):
        """Kam po přihlášení: běžně na hlavní stránku, ale když ho vyvolala appka
        na počítači (/zarizeni), zpátky na potvrzení pro ni."""
        nonce = self._cookies().get("hub_zar", "")
        with _ticket_lock:
            ok = bool(ZAR_RE.match(nonce)) and nonce in _zarizeni and _zarizeni[nonce]["exp"] > time.time()
        return "/zarizeni?n=" + nonce if ok else "/"

    def _zarizeni_appka(self, method):
        """Appka: POST {label} založí nonce, GET ?n=… se ptá, jestli už je potvrzeno."""
        now = time.time()
        if method == "POST":
            form = self._read_form()
            with _ticket_lock:
                for k, v in list(_zarizeni.items()):
                    if v["exp"] < now:
                        _zarizeni.pop(k, None)
                if len(_zarizeni) > 500:
                    return self._json({"error": "Server je teď zahlcený, zkus to za chvíli."}, 429)
                nonce = secrets.token_urlsafe(24)
                _zarizeni[nonce] = {"label": str(form.get("label") or "počítač")[:60], "exp": now + 600,
                                    "token": None, "user": None}
            return self._json({"n": nonce, "url": "/zarizeni?n=" + nonce})
        nonce = (urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get("n") or [""])[0]
        with _ticket_lock:
            z = _zarizeni.get(nonce) if ZAR_RE.match(nonce) else None
            if not z or z["exp"] < now:
                _zarizeni.pop(nonce, None)
                return self._json({"error": "Přihlášení vypršelo, začni znovu."}, 410)
            if not z["token"]:
                return self._json({"stav": "ceka"})
            _zarizeni.pop(nonce, None)                  # token se vydá jednou
        return self._json({"stav": "hotovo", "token": z["token"], "user": z["user"]})

    def _zarizeni_stranka(self, method):
        """Prohlížeč: přihlášení (heslem i passkeyem) a potvrzení pro appku."""
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        nonce = (query.get("n") or [""])[0]
        if method == "POST":
            nonce = str(self._read_form().get("n") or "")
        with _ticket_lock:
            z = _zarizeni.get(nonce) if ZAR_RE.match(nonce) else None
            platne = bool(z) and z["exp"] > time.time() and not z["token"]
        if not platne:
            return self._send(200, error_page("Tohle přihlášení už vypršelo. Zkus to znovu z appky."), HTML)
        user = self._user()
        if not user:
            # Přihlásit se; po přihlášení brána vrátí sem (cookie hub_zar).
            flags = "; Secure" if self._https() else ""
            return self._redirect("/login", extra={"Set-Cookie": f"hub_zar={nonce}; Path=/; HttpOnly; "
                                                                 f"SameSite=Lax; Max-Age=600{flags}"})
        if method != "POST":
            return self._send(200, _page("Přihlášení appky — Aladin", f"""
<form class=card method=post action="/zarizeni">
<h1>Povolit přihlášení?</h1>
<p class=sub>Appka <b>Aladin</b> na zařízení <b>{html.escape(z["label"])}</b>
chce být přihlášená jako <b>{html.escape(user["email"])}</b>.</p>
<input type=hidden name=n value="{html.escape(nonce)}">
<button type=submit>Povolit</button>
<p class=hint>Nepoznáváš to zařízení? Zavři stránku, nic se nestane.</p>
</form>"""), HTML)
        if not self._same_origin():
            return self._send(403, error_page("Tohle jde jen z téhle stránky."), HTML)
        token = self.accounts.issue_token(user, "Appka · " + z["label"], mfa=True)
        with _ticket_lock:
            z["token"], z["user"] = token, user
        return self._send(200, _page("Hotovo — Aladin", """
<div class=card>
<h1>Hotovo</h1>
<p class=sub>Appka je přihlášená. Tuhle stránku můžeš zavřít a vrátit se do appky.</p>
</div>"""), HTML, {"Set-Cookie": "hub_zar=; Path=/; HttpOnly; Max-Age=0"})

    def _gw_passkey(self, method, user):
        """Nastavení → Účet: seznam passkeyů, přidání (výzva + ověření) a odebrání.
        Přidat jde jen s kódem z aplikace — passkey je jeho pohodlnější náhrada,
        hub na počítači a záložní kódy dál jedou přes kód."""
        if method == "GET":
            return self._json({"passkeys": self.accounts.passkeys(user["id"])})
        if method != "POST" or not self._account_post_ok():
            return self._json({"error": "Passkey jde spravovat jen v nastavení aplikace."}, 403)
        form = self._read_form()
        action = form.get("action")
        rp_id, origin = self._rp()
        if action == "options":
            if not self.accounts.twofa(user["id"])["enabled"]:
                return self._json({"error": "Nejdřív si zapni ověření kódem z aplikace."}, 400)
            challenge = os.urandom(32)
            with _ticket_lock:
                _pk_reg[user["id"]] = (challenge, time.time() + 300)
            return self._json({
                "challenge": webauthn.b64e(challenge),
                "rp": {"id": rp_id, "name": "Aladin"},
                "user": {"id": webauthn.b64e(hashlib.sha256(f"claude-hub:{user['id']}".encode()).digest()),
                         "name": user["email"], "displayName": user.get("name") or user["email"]},
                "algs": list(webauthn.ALGS),
                "exclude": [k["id"] for k in self.accounts.passkeys(user["id"])]})
        if action == "register":
            with _ticket_lock:
                challenge, exp = _pk_reg.pop(user["id"], (None, 0))
            if not challenge or exp < time.time():
                return self._json({"error": "Výzva vypršela, zkus to znovu."}, 400)
            try:
                cred = webauthn.registrace(webauthn.b64d(form.get("clientData")),
                                           webauthn.b64d(form.get("attestation")), challenge, origin, rp_id)
            except (webauthn.Chyba, ValueError, TypeError) as exc:
                _errlog("passkey registrace", exc)
                return self._json({"error": "Passkey se nepodařilo přidat."}, 400)
            if self.accounts.passkey(cred["id"]):
                return self._json({"error": "Tenhle passkey už je přidaný."}, 400)
            self.accounts.passkey_add(user["id"], cred, str(form.get("name") or "")[:60])
            return self._json({"ok": True, "passkeys": self.accounts.passkeys(user["id"])})
        if action == "delete":
            ok = self.accounts.passkey_delete(user["id"], str(form.get("id") or ""))
            return self._json({"ok": ok, "passkeys": self.accounts.passkeys(user["id"])})
        return self._json({"error": "Neznámá akce."}, 400)

    def _account_post_ok(self):
        """Změny účtu jen z vlastní stránky a s hlavičkou, kterou cizí web nepošle."""
        return self._same_origin() and self.headers.get("X-Hub-Account") == "1"

    def _gw_password(self, method, user):
        """Změna hesla z Nastavení → Účet. Chce současné heslo, odhlásí ostatní
        zařízení a tomuhle prohlížeči vydá nové přihlášení."""
        if method != "POST":
            return self._json({"error": "Jen POST."}, 405)
        if not self._account_post_ok():
            return self._json({"error": "Heslo jde změnit jen v nastavení aplikace."}, 403)
        keys = self._fail_keys(user["email"])
        wait = self.accounts.fail_wait(keys)
        if wait:
            return self._json({"error": too_many(wait)}, 429)
        form = self._read_form()
        current = str(form.get("current") or "")
        new = str(form.get("new") or "")
        if not self.accounts.verify(user["email"], current):
            locked, left = self._failed(keys)
            if locked:
                return self._json({"error": "Současné heslo nesedí. " + locked}, 429)
            return self._json({"error": "Současné heslo nesedí. " + tries_left(left)}, 400)
        if new == current:
            return self._json({"error": "Nové heslo je stejné jako to současné."}, 400)
        try:
            self.accounts.set_password(user["email"], new)      # odhlásí všechna zařízení
        except ValueError as exc:
            return self._json({"error": str(exc)}, 400)
        token = self.accounts.issue_token(user, self.headers.get("User-Agent", "")[:60],
                                          mfa=True)
        return self._send(200, json.dumps({"ok": True}), "application/json; charset=utf-8",
                          {"Set-Cookie": self._set_cookie(token)})

    def _gw_recovery(self, method, user):
        """Nové záložní kódy — jen s platným kódem z aplikace, ne se záložním."""
        if method != "POST":
            return self._json({"error": "Jen POST."}, 405)
        if not self._account_post_ok():
            return self._json({"error": "Kódy jde vygenerovat jen v nastavení aplikace."}, 403)
        keys = self._fail_keys(user["email"])
        wait = self.accounts.fail_wait(keys)
        if wait:
            return self._json({"error": too_many(wait)}, 429)
        form = self._read_form()
        if not self.accounts.second_factor(user["id"], str(form.get("code") or ""),
                                           allow_recovery=False):
            locked, left = self._failed(keys)
            if locked:
                return self._json({"error": "Kód z aplikace nesedí. " + locked}, 429)
            return self._json({"error": "Kód z aplikace nesedí. " + tries_left(left)}, 400)
        return self._json({"recovery": self.accounts.new_recovery(user["id"])})

    def _gw_shared(self, user):
        """Sdílené Obsidiany uživatele živě z registru. `needs_restart` = je
        členem, ale běžící prostor ho ještě nemá svázaný."""
        proc = self.hubs.procs.get(user["id"]) if self.hubs else None
        bound = set(proc.shared) if proc and proc.alive() else None
        vaults = shared.vaults_for(user)
        for v in vaults:
            v.pop("path", None)
            v["needs_restart"] = bound is not None and v["slug"] not in bound
        gone = sorted(bound - {v["slug"] for v in vaults}) if bound else []
        cizi = shared.spravovane(user)
        for v in cizi:
            v.pop("path", None)
        return self._json({"vaults": vaults, "people": shared.people(), "gone": gone,
                           "spravovane": cizi})

    def _gw_relace(self, route, method, user):
        """Sdílené relace: chat jednoho člověka živě u dalších (gateway/relace.py).

        Seznam a správa sdílení jdou z hubu (`X-Hub-Account`), čtení a psaní
        z okna se sdíleným chatem. Brána ověří, kdo smí, a teprve potom sáhne
        do prostoru majitele; prohlížeč kolegy se k cizímu hubu nedostane."""
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        if route == "/gw/relace":
            if method != "POST":
                data = relace.seznam(user)
                data["lide"] = [p for p in shared.people() if p["email"] != user["email"]]
                # Podkategorie firem, kam chat jde sdílet nebo uložit (gateway/firmy.py).
                data["skupiny"] = firmy.skupiny_pro(user)
                return self._json(data)
            if not self._account_post_ok():
                return self._json({"error": "Tohle jde jen v hubu."}, 403)
            form = self._read_form()
            akce = str(form.get("akce") or "")
            try:
                if akce == "zalozit":
                    result = relace.zalozit(user, form.get("chat"), form.get("titulek"),
                                            form.get("emaily") or [], form.get("psat") or [],
                                            form.get("skupiny") or [], form.get("skupiny_psat") or [])
                elif akce == "ulozit":
                    result = relace.ulozit(user, form.get("chat"), form.get("titulek"),
                                           form.get("skupiny") or [])
                elif akce == "smazat-ulozeny":
                    result = relace.smazat_ulozeny(user, str(form.get("id") or ""))
                elif akce == "zrusit":
                    result = relace.zrusit(user, str(form.get("id") or ""))
                elif akce == "odejit":
                    result = relace.odejit(user, str(form.get("id") or ""))
                elif akce == "oznameno":
                    ids = form.get("ids")
                    result = relace.oznameno(user, [str(i) for i in ids][:200] if isinstance(ids, list) else [])
                else:
                    return self._json({"error": "Neznámá akce."}, 400)
            except ValueError as exc:
                return self._json({"error": str(exc)}, 400)
            except OSError as exc:
                _errlog("sdílené relace", exc)
                return self._json({"error": "Nepodařilo se to uložit, zkus to prosím znovu."}, 500)
            return self._json(result)

        if route == "/gw/relace/pokracovat":
            return self._relace_pokracovat(method, user)
        if route == "/gw/relace/zapnout":
            return self._relace_zapnout(method, user)
        if route == "/gw/relace/pritomnost":
            if method != "POST" or not self._account_post_ok():
                return self._json({"error": "Jen z hubu."}, 403)
            form = self._read_form()
            try:
                result = relace.pritomnost(user, str(form.get("id") or ""),
                                           str(form.get("pise") or "").lower() in ("1", "true"))
            except ValueError as exc:
                return self._json({"error": str(exc)}, 403)
            # Odpověď rovnou nese, kdo je v chatu — okno se obnoví bez čekání na další dotaz.
            entry, _r = relace.ziskat(user, str(form.get("id") or ""))
            result["diva"] = relace.verejne(user, str(form.get("id") or ""), entry)["diva"]
            result["pisou"] = relace.verejne(user, str(form.get("id") or ""), entry)["pisou"]
            return self._json(result)

        # Čtení a psaní — vždycky přes majitelův hub, a jen když mu prostor běží
        # (kvůli dívání se neprobouzí spící prostor).
        if route == "/gw/relace/priloha":
            if method != "POST" or not self._account_post_ok():
                return self._json({"error": "Přílohu jde poslat jen z hubu."}, 403)
            if int(self.headers.get("Content-Length") or 0) > 40 * 1024 * 1024:
                return self._json({"error": "Soubor je moc velký."}, 413)
            form = self._read_form()
            rid = str(form.get("id") or "")
            try:
                entry, _role = relace.ziskat(user, rid, psat=True)
            except ValueError as exc:
                return self._json({"error": str(exc)}, 403)
            if not relace.smi_poslat(user):
                return self._json({"error": "Posíláš moc rychle, chvilku počkej."}, 429)
            tool, args = "sdilet_priloha", {"chat": entry["chat"], "name": str(form.get("name") or ""),
                                            "data": str(form.get("data") or "")}
        elif route == "/gw/relace/poslat":
            if method != "POST" or not self._account_post_ok():
                return self._json({"error": "Zpráva jde odeslat jen z hubu."}, 403)
            form = self._read_form()
            rid = str(form.get("id") or "")
            try:
                entry, _role = relace.ziskat(user, rid, psat=True)
            except ValueError as exc:
                return self._json({"error": str(exc)}, 403)
            if not relace.smi_poslat(user):
                return self._json({"error": "Píšeš moc rychle, chvilku počkej."}, 429)
            tool, args = "sdilet_poslat", {"chat": entry["chat"], "text": str(form.get("text") or ""),
                                           "kdo": "" if _role == "majitel" else relace.jmeno(user)}
        else:
            if method != "GET":
                return self._json({"error": "Jen GET."}, 405)
            rid = str((query.get("id") or [""])[0])
            try:
                entry, _role = relace.ziskat(user, rid)
            except ValueError as exc:
                return self._json({"error": str(exc)}, 403)
            relace.dival_se(user, rid)
            tool, args = "sdilet_cteni", {"chat": entry["chat"],
                                          "from": (query.get("from") or ["0"])[0]}
        owner = shared.ACCOUNTS.by_id(entry.get("owner")) if shared.ACCOUNTS else None
        proc = self.hubs.procs.get(entry.get("owner")) if self.hubs else None
        if not owner or not proc or not proc.alive():
            # Majitelův prostor neběží — číst jde i tak, přímo z uloženého přepisu
            # (jen čtení, bez „Claude pracuje“). Psát se do chatu bez prostoru nedá.
            if tool == "sdilet_cteni":
                path = relace.prepis_offline(owner, entry.get("chat"))
                if path:
                    from hub import cteni as _cteni
                    try:
                        start = max(0, int(args.get("from") or 0))
                    except (TypeError, ValueError):
                        start = 0
                    result = _cteni.read(path, start, tail=not start)
                    result["pracuje"] = False
                    result["bezi"] = False
                    relace.pridej_stav(result, relace.verejne(user, rid, entry), _role, user,
                                       majitel_offline=True)
                    return self._json(result)
            return self._json({"offline": True, "zprava": "Majitel má prostor vypnutý — zpráva by nikam nedošla. "
                                                          "Čtení funguje, psát jde, až ho zapne."})
        try:
            result = mcp._hub_call(self, owner, tool, args, timeout=20)
        except mcp.ToolError as exc:
            return self._json({"error": str(exc)}, 502)
        if tool == "sdilet_cteni":
            relace.pridej_stav(result, relace.verejne(user, rid, entry), _role, user)
        return self._json(result)

    def _relace_soubor(self, method, user):
        """Soubor ze sdíleného chatu ke stažení (cteni.js → „Stáhnout“).

        Smí každý, kdo chat smí číst (relace.ziskat), a jen soubor, který Claude
        v tom chatu zapsal — to hlídá hub majitele podle přepisu (`chat`,
        hub/server.py _soubor_ke_stazeni), ani ze skrytých složek. Odpověď jde
        proudem rovnou z majitelova hubu, prohlížeč kolegy k němu přístup nemá."""
        if method not in ("GET", "HEAD"):
            return self._json({"error": "Jen GET."}, 405)
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        rid = str((query.get("id") or [""])[0])
        try:
            entry, _role = relace.ziskat(user, rid)
        except ValueError as exc:
            return self._json({"error": str(exc)}, 403)
        proc = self.hubs.procs.get(entry.get("owner")) if self.hubs else None
        if not proc or not proc.alive():
            return self._json({"error": "Majitel má prostor vypnutý — stáhnout to půjde, až ho zapne."}, 409)
        dotaz = urllib.parse.urlencode({"path": str((query.get("path") or [""])[0])[:2000],
                                        "chat": str(entry.get("chat") or ""),
                                        "info": "1" if (query.get("info") or [""])[0] == "1" else ""})
        conn = http.client.HTTPConnection("127.0.0.1", proc.port, timeout=60)
        try:
            conn.request(method, "/api/soubor?" + dotaz,
                         headers={"X-Hub-Token": proc.token, "Host": f"127.0.0.1:{proc.port}"})
            resp = conn.getresponse()
            self.send_response(resp.status)
            for key in ("Content-Type", "Content-Length", "Content-Disposition", "Content-Security-Policy",
                        "X-Content-Type-Options", "Cache-Control"):
                if resp.getheader(key):
                    self.send_header(key, resp.getheader(key))
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("X-Accel-Buffering", "no")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            if method == "HEAD":
                return None
            if resp.status == 200 and "info=1" not in dotaz:
                relace._log(user, "stazeno", rid, soubor=str((query.get("path") or [""])[0])[-120:])
            while True:
                kus = resp.read1(65536)
                if not kus:
                    break
                self.wfile.write(kus)
        except (OSError, http.client.HTTPException) as exc:
            _errlog("relace soubor", exc)
        finally:
            conn.close()
        return None

    def _relace_zapnout(self, method, user):
        """Zapnout sdílený chat znovu: spustí majiteli prostor, když spal, a otevře
        v něm chat, který zavřel. Smí každý, kdo chat sdílený vidí — spuštění
        nikomu nic nepřidá, zprávy píše dál jen kdo má právo psát."""
        if method != "POST" or not self._account_post_ok():
            return self._json({"error": "Tohle jde jen z hubu."}, 403)
        form = self._read_form()
        rid = str(form.get("id") or "")
        try:
            entry, _role = relace.ziskat(user, rid)
        except ValueError as exc:
            return self._json({"error": str(exc)}, 403)
        owner = shared.ACCOUNTS.by_id(entry.get("owner")) if shared.ACCOUNTS else None
        if not owner or not self.hubs:
            return self._json({"error": "Majitel už tu účet nemá."}, 404)
        if not relace.smi_zapnout(user, rid):
            return self._json({"error": "Zapínáš moc rychle, chvilku počkej."}, 429)
        try:
            result = mcp._hub_call(self, owner, "sdilet_otevrit",
                                   {"chat": entry["chat"], "title": entry.get("titulek") or ""},
                                   timeout=90)
        except mcp.ToolError as exc:
            return self._json({"error": str(exc)}, 502)
        relace._log(user, "zapnuto", rid)
        return self._json({"ok": True, **(result if isinstance(result, dict) else {})})

    def _relace_pokracovat(self, method, user):
        """„Pokračovat u sebe“: kopie sdíleného chatu do prostoru toho, kdo ho
        sleduje. Chat se zabalí z majitelova domova a předá hubu v prostoru
        žadatele (`prenos_prijmout`, jako chat z počítače) — Claude v něm naváže,
        kde skončil, a majitelův chat se nijak nemění. Stačí právo číst."""
        import base64
        from hub import prenos
        from . import workspace
        if method != "POST" or not self._account_post_ok():
            return self._json({"error": "Tohle jde jen z hubu."}, 403)
        form = self._read_form()
        if form.get("ulozeny"):
            # Chat uložený do podkategorie firmy: zip leží ve složce firmy.
            try:
                info, path = relace.ulozeny(user, form.get("ulozeny"))
                with open(path, "rb") as fh:
                    data = fh.read()
                meta = {"id": info["chat"]}
            except (ValueError, OSError) as exc:
                return self._json({"error": str(exc) if isinstance(exc, ValueError)
                                   else "Uložený chat se nepodařilo přečíst."}, 403)
            owner = shared.ACCOUNTS.by_id(info.get("owner")) if shared.ACCOUNTS else None
            entry = {"titulek": info.get("titulek") or "Chat"}
            owner = owner or {"name": "kolega"}
        else:
            try:
                entry, _role = relace.ziskat(user, str(form.get("id") or ""))
            except ValueError as exc:
                return self._json({"error": str(exc)}, 403)
            owner = shared.ACCOUNTS.by_id(entry.get("owner")) if shared.ACCOUNTS else None
            if not owner or not relace.prepis_offline(owner, entry.get("chat")):
                return self._json({"error": "Chat už u majitele není, nemám co zkopírovat."}, 404)
            try:
                data, meta = prenos.zabal(os.path.join(workspace.home_for(owner), ".claude"),
                                          entry["chat"], entry.get("titulek") or "Chat",
                                          odkud=relace.jmeno(owner))
            except ValueError as exc:
                return self._json({"error": str(exc)}, 400)
        parts = prenos.kusy(data)
        try:
            for i, chunk in enumerate(parts, 1):
                result = mcp._hub_call(self, user, "prenos_prijmout", {
                    "id": meta["id"], "part": i, "parts": len(parts),
                    "zip": base64.b64encode(chunk).decode("ascii"),
                    "from": relace.jmeno(owner)[:80],
                    "title": ("Kopie: " + (entry.get("titulek") or "Chat"))[:120]}, timeout=180)
        except mcp.ToolError as exc:
            return self._json({"error": str(exc)}, 502)
        relace._log(user, "pokracovat", str(form.get("id") or form.get("ulozeny") or ""))
        return self._json({"ok": True, "chat": meta["id"], "hotovo": bool((result or {}).get("hotovo"))})

    def _gw_shared_change(self, user):
        """Sdílené Obsidiany naklikáním (panel v hubu): založit, členové,
        odejít, smazat. Oprávnění ověřuje shared.py podle registru."""
        form = self._read_form()
        if not self._account_post_ok():
            return self._json({"error": "Tohle jde jen v hubu."}, 403)
        action = str(form.get("akce") or "")
        try:
            if action == "zalozit":
                result = shared.create(user, form.get("nazev"), form.get("emaily") or [])
            elif action == "clenove":
                result = shared.change_members(user, form.get("slug"), form.get("pridat") or [],
                                               form.get("odebrat") or [])
            elif action == "odejit":
                result = shared.leave(user, form.get("slug"))
            elif action == "smazat":
                result = shared.delete(user, form.get("slug"), natrvalo=bool(form.get("natrvalo")))
            else:
                return self._json({"error": "Neznámá akce."}, 400)
        except ValueError as exc:
            return self._json({"error": str(exc)}, 400)
        except OSError as exc:
            _errlog("sdílené obsidiany", exc)
            return self._json({"error": "Nepodařilo se to uložit, zkus to prosím znovu."}, 500)
        try:
            slozky.sync()                    # Sdílené/<název> ve firemním trezoru
        except (OSError, ValueError):
            pass
        if self.hubs:
            for uid in result.get("revoked") or []:
                self.hubs.stop_idle(uid)
        return self._json({"ok": True, "message": result.get("message", "")})

    def _gw_team(self, method, user):
        """Nastavení → Tým: lidé a jejich účty. Jen admin, změny jen z hubu."""
        from . import tym
        if user.get("role") != "admin":
            return self._json({"error": "Tým spravují admini."}, 403)
        if method == "GET":
            return self._json({"lide": tym.people(self.accounts, self.hubs), "ja": user["id"]})
        if method != "POST":
            return self._json({"error": "Jen GET nebo POST."}, 405)
        form = self._read_form()
        if not self._account_post_ok():
            return self._json({"error": "Tým jde měnit jen v hubu."}, 403)
        try:
            return self._json(tym.act(self.accounts, user, form, self.hubs))
        except ValueError as exc:
            return self._json({"error": str(exc)}, 400)

    # ---- přístup do cizího prostoru (gateway/pristup.py) ----
    def _space_cookie(self, owner_id=None):
        flags = "; Secure" if self._https() else ""
        if owner_id is None:
            return f"{pristup.COOKIE}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0{flags}"
        return (f"{pristup.COOKIE}={int(owner_id)}; Path=/; HttpOnly; SameSite=Lax; "
                f"Max-Age={config.SESSION_MAX_AGE}{flags}")

    def _space_here(self, user):
        """(vlastník, role) prostoru, kde host právě je, nebo None."""
        raw = self._cookies().get(pristup.COOKIE, "")
        if not raw.isdigit():
            return None
        role = self.accounts.access_role(int(raw), user["id"])
        owner = self.accounts.by_id(int(raw)) if role else None
        return (owner, role) if owner else None

    def _gw_pristup(self, method, user):
        """Nastavení → Účet → Přístup a přepínání prostorů v liště.

        Vlastník je vždycky přihlášený člověk — host si tudy přístup do cizího
        prostoru přidat nemůže, jen do svého. Změny jen ze stránky (CSRF)."""
        if method == "GET":
            here = self._space_here(user)
            tady = None
            if here:
                owner, role = here
                tady = {"id": owner["id"], "name": owner.get("name") or "",
                        "email": owner["email"], "role": role, "label": pristup.label(role)}
            # Přepínač prostoru v liště hubu chce jen „kam smím“ — bez záznamu.
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            if (query.get("jen") or [""])[0] == "kam":
                return self._json(pristup.kam_only(self.accounts, user, tady))
            return self._json(pristup.overview(self.accounts, user, tady))
        if method != "POST":
            return self._json({"error": "Jen GET nebo POST."}, 405)
        form = self._read_form()
        if not self._account_post_ok():
            return self._json({"error": "Přístupy jdou měnit jen v aplikaci."}, 403)
        akce = str(form.get("akce") or "")
        if akce == "vstoupit":
            try:
                owner_id = int(form.get("vlastnik"))
            except (TypeError, ValueError):
                return self._json({"error": "Neznámý prostor."}, 400)
            role, pres = self.accounts.access_detail(owner_id, user["id"])
            owner = self.accounts.by_id(owner_id) if role else None
            if not owner:
                return self._json({"error": "Do tohohle prostoru přístup nemáš."}, 403)
            pristup.log(owner_id, user["id"], "vstup", role=role, pres=pres)
            self._owner_notice(owner, f"{user.get('name') or user['email']} vstoupil(a) do tvého "
                               f"prostoru ({pristup.label(role)}).")
            return self._send(200, json.dumps({"ok": True}), "application/json; charset=utf-8",
                              extra={"Set-Cookie": self._space_cookie(owner_id)})
        if akce == "odejit":
            raw = self._cookies().get(pristup.COOKIE, "")
            if raw.isdigit():
                pristup.log(int(raw), user["id"], "odchod")
                pristup.LIVE.kick(int(raw), user["id"])
            return self._send(200, json.dumps({"ok": True}), "application/json; charset=utf-8",
                              extra={"Set-Cookie": self._space_cookie()})
        try:
            return self._json(pristup.act(self.accounts, user, form))
        except PermissionError as exc:
            return self._json({"error": str(exc)}, 403)
        except ValueError as exc:
            return self._json({"error": str(exc)}, 400)

    def _proxy_guest(self, method, user, route, query):
        """Požadavek hosta do cizího prostoru — jen s platným povolením a jen
        tam, kam role pustí. Bez povolení se cookie smaže a host je doma."""
        here = self._space_here(user)
        if not here:
            raw = self._cookies().get(pristup.COOKIE, "")
            if raw.isdigit():
                pristup.LIVE.kick(int(raw), user["id"])
            clear = {"Set-Cookie": self._space_cookie()}
            if method == "GET" and "text/html" in self.headers.get("Accept", "") \
                    and "websocket" not in self.headers.get("Upgrade", "").lower():
                return self._redirect("/", extra=clear)
            body = json.dumps({"error": "Přístup do tohohle prostoru už nemáš.",
                               "pristup": "odebran"}, ensure_ascii=False)
            return self._send(403, body, "application/json; charset=utf-8", extra=clear)
        owner, role = here
        if role == "predplatne" and method == "GET" and route == "/" \
                and "websocket" not in self.headers.get("Upgrade", "").lower():
            # Správa předplatného nedostane hub vlastníka, jen stránku brány.
            return self._send(200, predplatne_page(owner), HTML)
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else None
        why = pristup.http_denied(role, method, route, query, body or b"")
        if why:
            pristup.log(owner["id"], user["id"], "zamitnuto", cesta=route[:80], metoda=method)
            return self._json({"error": why, "pristup": "role"}, 403)
        try:
            hub = self.hubs.get(owner)
        except RuntimeError as exc:
            _errlog("proxy start (host)", exc)
            return self._fail(503, MSG_FULL if "plný" in str(exc) else MSG_STARTING)
        hub.touch()
        extra = {pristup.GUEST_HEADER: pristup.guest_header(user, owner, role)}
        if "websocket" in self.headers.get("Upgrade", "").lower():
            return self._proxy_ws(hub, extra=extra, guest=(owner, user, role))
        obnov = method == "GET" and urllib.parse.urlsplit(self.path).path in ("/", "/index.html")
        return self._proxy_http(method, hub, body=body, extra=extra, guest=True, obnov_cookie=obnov)

    def _hub_call(self, hub, name, payload, timeout=20):
        """Brána volá hub prostoru napřímo (ne z prohlížeče) — jeho tokenem."""
        return hub_post(hub, name, payload, timeout)

    def _preved_prihlaseni(self, user):
        """Nové přihlášení Clauda (připojené / odpojené předplatné) pošle do
        běžícího prostoru — nové chaty na něm jedou hned, bez restartu.
        Neběžící prostor ho vezme při startu sám (workspace.session_env)."""
        proc = self.hubs.procs.get(user["id"]) if self.hubs else None
        if not proc or not proc.alive():
            return
        env = workspace.session_env(user)
        out = self._hub_call(proc, "predplatne-host", {"action": "prihlaseni", "env": {
            k: env.get(k, "") for k in ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY")}}, timeout=10)
        if out.get("ok"):
            proc.auth_mark = workspace.auth_mark(user)

    def _owner_notice(self, owner, text):
        """Toast vlastníkovi — jen když jeho prostor běží (nebudí ho kvůli tomu)."""
        proc = self.hubs.procs.get(owner["id"]) if self.hubs else None
        if proc and proc.alive():
            self._hub_call(proc, "predplatne-host", {"action": "oznam", "text": text}, timeout=5)

    def _gw_pristup_predplatne(self, method, user):
        """Správa předplatného hostem: jediné, co role `predplatne` v cizím
        prostoru smí. Přihlášení (`claude setup-token`) běží skrytě v hubu
        vlastníka; host vidí jen stav, odkaz a výsledek. Token si brána
        vyzvedne z hubu a uloží stejně jako předplatné připojené z appky."""
        here = self._space_here(user)
        if not here or here[1] not in ("predplatne", "plny"):
            return self._json({"error": "Do tohohle prostoru přístup nemáš.",
                               "pristup": "odebran"}, 403)
        owner, role = here
        jmeno = user.get("name") or user["email"]
        if method == "POST":
            form = self._read_form()
            if not self._account_post_ok():
                return self._json({"error": "Jde to jen ze stránky."}, 403)
            akce = str(form.get("akce") or "")
            if akce not in pristup.PREDPLATNE_AKCE:
                pristup.log(owner["id"], user["id"], "zamitnuto", akce_hosta=akce[:30])
                return self._json({"error": "Tohle Správa předplatného nesmí."}, 403)
            if akce == "odpojit":
                # Stejné jako „Odpojit“ u vlastníka: zpátky na firemní přístup.
                workspace.remove_predplatne(owner)
                pristup.log(owner["id"], user["id"], "predplatne-odpojit")
                self._preved_prihlaseni(owner)
                self._owner_notice(owner, f"{jmeno} odpojil(a) vlastní předplatné — nové chaty "
                                          "jedou zase přes firmu.")
            else:
                try:
                    hub = self.hubs.get(owner)
                except RuntimeError as exc:
                    return self._json({"error": str(exc)}, 503)
                action = {"prihlasit": "start", "kod": "kod", "zrusit": "zrusit"}[akce]
                out = self._hub_call(hub, "predplatne-host",
                                     {"action": action, "code": str(form.get("kod") or "")[:500]})
                pristup.log(owner["id"], user["id"], "predplatne-" + akce)
                if out.get("error") and not out.get("state"):
                    return self._json({"error": out["error"]}, 502)
        return self._json(self._predplatne_stav(owner, user, jmeno))

    def _predplatne_stav(self, owner, user, jmeno):
        def rezim():
            # Host vidí jen režim, ne názvy a seznam předplatných vlastníka.
            st = workspace.claude_state(owner)
            for k in ("label", "predplatna", "aktivni"):
                st.pop(k, None)
            return st
        out = {"vlastnik": owner.get("name") or owner["email"], "rezim": rezim(),
               "prihlaseni": {"state": "idle", "url": "", "error": ""}, "ucet": {}}
        proc = self.hubs.procs.get(owner["id"]) if self.hubs else None
        if proc and proc.alive():
            st = self._hub_call(proc, "predplatne-host", {"action": "stav"}, timeout=10)
            out["ucet"] = st.get("account") or {}
            out["prihlaseni"] = {k: st.get(k, "") for k in ("state", "url", "error")}
            if st.get("state") == "token":
                out["prihlaseni"], sid = self._vyzvedni_predplatne(
                    owner, proc, f"Správa předplatného: {jmeno}"[:80])
                if sid:
                    pristup.log(owner["id"], user["id"], "predplatne-pripojeno")
                    self._owner_notice(owner, f"{jmeno} připojil(a) tvé vlastní předplatné Claude — "
                                              "nové chaty už jedou na něm.")
                out["rezim"] = rezim()
            out["needs_restart"] = proc.auth_mark != workspace.auth_mark(owner)
        return out

    def _gw_spotreba(self, method, user):
        """Statistiky → Tým: spotřeba po lidech. Admin vidí všechny a nastavuje
        týdenní limity (USD / procenta / tokeny, stopka) a výjimku do konce
        týdne, ostatní jen sebe (a jestli jsou u svého limitu)."""
        from . import spotreba
        admin = user.get("role") == "admin"
        if method == "GET":
            data = spotreba.prehled(self.accounts.list(), None if admin else user["email"],
                                    wait=2.0)
            data["admin"] = admin
            return self._json(data)
        if method != "POST":
            return self._json({"error": "Jen GET nebo POST."}, 405)
        if not admin:
            return self._json({"error": "Limity nastavují admini."}, 403)
        form = self._read_form()
        if not self._account_post_ok():
            return self._json({"error": "Limity jde měnit jen v hubu."}, 403)
        try:
            row = next((r for r in self.accounts.list() if r["id"] == int(form.get("id"))), None)
            if not row:
                raise ValueError("Tenhle člověk tu účet nemá.")
            akce = str(form.get("akce") or "")
            if akce == "povolit":
                do = spotreba.povolit(row["email"], user["email"])
                return self._json({"ok": True, "vyjimka": {"do": do}})
            if akce == "zrusit-vyjimku":
                spotreba.zrusit_vyjimku(row["email"])
                return self._json({"ok": True, "vyjimka": None})
            if "limit" in form and not any(k in form for k in ("usd", "pct", "tokeny", "stopka")):
                # Starší stránka (jen pole „limit $“) — ostatní druhy zůstanou.
                usd = spotreba.set_limit(row["email"], form.get("limit"))
                return self._json({"ok": True, "limit": usd})
            zmeny = {k: form[k] for k in ("usd", "pct", "tokeny", "stopka") if k in form}
            lim = spotreba.set_limity(row["email"], **zmeny)
        except (TypeError, ValueError) as exc:
            return self._json({"error": str(exc)}, 400)
        return self._json({"ok": True, "limit": lim["usd"], "limity": lim})

    def _spotreba_pocitac(self, method):
        """Souhrn spotřeby z hubu na počítači (hub/spotreba_pc.py). Odpověď
        řekne, jak na tom člověk je, a jestli stopka platí i na tomhle počítači
        (jen když jede na stejném předplatném jako prostor)."""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length > 2 * 1024 * 1024:
            self.close_connection = True
            return self._json({"error": "Souhrn je moc velký."}, 413)
        if method != "POST":
            return self._json({"error": "Jen POST."}, 405)
        form = self._read_form()
        user = self._bearer()
        if not user:
            return self._json({"error": "Neplatný token."}, 401)
        from . import spotreba
        try:
            spotreba.uloz_pocitac(user, form if isinstance(form, dict) else {})
        except ValueError as exc:
            return self._json({"error": str(exc)}, 400)
        out = spotreba.stopka_pro(self.accounts.list(), user)
        stejne = spotreba.pocitac_stejny(user, str(form["pocitac"]["id"]))
        return self._json({"ok": True, **out, "plati_zde": bool(out.get("stopka") and stejne),
                           "stejne_predplatne": stejne})

    def _spotreba_stopka(self):
        """Platí pro tenhle prostor stopka? Ptá se hub v prostoru (hub/stopka.py)
        a hook před každou zprávou Claudovi (tools/stopka_hook.py). Jen na
        loopbacku se žetonem prostoru — stejně jako most na počítač."""
        if self.headers.get("X-Real-IP") or self.headers.get("X-Forwarded-For"):
            return self._send(404, b"404")
        user = self.hubs.user_for_pocitac(self.headers.get("X-Hub-Pocitac", "")) \
            if self.hubs else None
        if not user:
            return self._json({"error": "Neplatný žeton prostoru."}, 401)
        if self.command == "POST":
            self._read_form()
        from . import spotreba
        return self._json(spotreba.stopka_pro(self.accounts.list(), user))

    def _gw_uloziste(self, method, user):
        """Nastavení → Úložiště: kolik kdo zabírá a čím, limity, úklid. Každý
        vidí sebe a přepočítá si svůj domov; admin celý server, limity a úklid.
        Těžká práce běží v samostatném procesu (uloziste.spustit)."""
        from . import uloziste
        admin = user.get("role") == "admin"
        if method == "GET":
            return self._json(uloziste.prehled(user, self.accounts.list(), admin))
        if method != "POST":
            return self._json({"error": "Jen GET nebo POST."}, 405)
        form = self._read_form()
        if not self._account_post_ok():
            return self._json({"error": "Úložiště jde měnit jen v hubu."}, 403)
        akce = str(form.get("akce") or "")
        try:
            if akce == "prepocitat":
                vse = admin and bool(form.get("vse"))
                if not uloziste.spustit("sken", None if vse else user["id"]):
                    return self._json({"error": "Rozbor už běží — počkej, až doběhne."}, 409)
                return self._json({"ok": True, "bezi": uloziste.bezi()})
            if not admin:
                return self._json({"error": "Tohle nastavují admini."}, 403)
            if akce == "limit":
                row = next((r for r in self.accounts.list() if r["id"] == int(form.get("id"))), None)
                if not row:
                    raise ValueError("Tenhle člověk tu účet nemá.")
                return self._json({"ok": True, "kvota": uloziste.set_limit(row["email"], form.get("gb"))})
            if akce in ("uklid", "rozbor"):
                if not uloziste.spustit(akce):
                    return self._json({"error": "Rozbor nebo úklid už běží — počkej, až doběhne."}, 409)
                return self._json({"ok": True, "bezi": uloziste.bezi()})
        except (TypeError, ValueError) as exc:
            return self._json({"error": str(exc)}, 400)
        return self._json({"error": "Neznámá akce."}, 400)

    def _gw_oznameni(self, route, method, user):
        """Zvonek v liště (hub/static/oznameni.js): oznámení přihlášeného
        člověka a jeho přečteno / vyřešeno / zavřeno. `/admin`: kdo co dostal
        a co s tím udělal, a ruční oznámení vybraným lidem."""
        from . import oznameni, uloziste
        if route == "/gw/oznameni/admin":
            if user.get("role") != "admin":
                return self._json({"error": "Záznam oznámení vidí admini."}, 403)
            if method == "GET":
                ted = uloziste.pouzito_vsech()
                zaznam = oznameni.vse(400)
                for o in zaznam:
                    o["stav"] = oznameni.stav(o)
                    o["pouzito_ted"] = ted.get(o["uid"])
                lide = [{"id": r["id"], "email": r["email"], "name": r["name"] or ""}
                        for r in self.accounts.list() if not r["disabled"]]
                return self._json({"oznameni": zaznam, "lide": lide})
            if method != "POST":
                return self._json({"error": "Jen GET nebo POST."}, 405)
            form = self._read_form()
            if not self._account_post_ok():
                return self._json({"error": "Oznámení jde posílat jen z hubu."}, 403)
            try:
                return self._json(oznameni.poslat(
                    self.accounts, user, form.get("komu"), form.get("titulek"), form.get("text"),
                    form.get("prompt"), form.get("tlacitko"), str(form.get("zavaznost") or "info")))
            except ValueError as exc:
                return self._json({"error": str(exc)}, 400)
        if method == "GET":
            return self._json(oznameni.pro(user["id"]))
        if method != "POST":
            return self._json({"error": "Jen GET nebo POST."}, 405)
        form = self._read_form()
        if not self._account_post_ok():
            return self._json({"error": "Oznámení jde měnit jen v hubu."}, 403)
        try:
            n = oznameni.oznacit(user["id"], form.get("ids"), str(form.get("akce") or ""))
        except ValueError as exc:
            return self._json({"error": str(exc)}, 400)
        return self._json({"ok": True, "zmeneno": n})

    def _gw_restart(self, method, user):
        """Restart vlastního prostoru z hubu (nové sdílené Obsidiany, přepnutí
        na novou verzi)."""
        if method != "POST":
            return self._json({"error": "Jen POST."}, 405)
        # Tělo se musí přečíst, i když v něm nic není: na spojení, které
        # prohlížeč drží otevřené, by jinak zbylé „{}" začínalo další požadavek
        # („{}GET …" → 501) — a tím je hned načtení stránky po restartu.
        self._read_form()
        if not self._account_post_ok():
            return self._json({"error": "Restart jde jen z aplikace."}, 403)
        if self.hubs:
            self.hubs.restart(user)
        return self._json({"ok": True})

    def _gw_verze(self, user):
        """Na jaké verzi prostor jede a na jakou by se přepnul restartem.

        Nová verze se na serveru připravuje na pozadí (gateway/update.sh) a do
        běžícího prostoru se nedostane sama — hub se podle tohohle zeptá, jestli
        ji člověk chce hned, nebo později."""
        proc = self.hubs.procs.get(user["id"]) if self.hubs else None
        running = proc.version if proc and proc.alive() else ""
        return self._json({"prostor": running, "nejnovejsi": workspace.code_dir()[1]})

    # ---- rozhraní pro hub na počítači ----
    def _bearer_token(self):
        head = self.headers.get("Authorization", "")
        return head[7:].strip() if head[:7].lower() == "bearer " else ""

    def _bearer(self):
        token = self._bearer_token()
        return self.accounts.user_for_token(token) if token else None

    def _gw_info(self):
        """Bez přihlášení: appka tím ověří, že na adrese je opravdu brána.

        Bez toho by šlo zjistit jen „něco odpovídá" — a na špatně napsané
        adrese by pak člověk psal heslo do cizího webu.
        """
        return self._json({"app": "claude-code-hub", "kind": "gateway",
                           "version": __version__,
                           # Co brána umí navíc — appka podle toho pozná, jestli
                           # se má ptát třeba na most na počítač.
                           "features": ["pocitac", "predplatne", "ukoly", "schvalovani", "povoleni", "zrcadlo", "spotreba", "mcp-pc"],
                           "host": self._public_host()})

    def _gw_me(self):
        """Komu patří token — hub se tím ptá, jestli je pořád přihlášený."""
        user = self._bearer()
        if not user:
            return self._json({"error": "Neplatný token."}, 401)
        return self._json({"user": user})

    def _gw_handoff(self, method):
        """Vymění platný token za jednorázový kód do adresy prohlížeče."""
        if method != "POST":
            return self._json({"error": "Jen POST."}, 405)
        user = self._bearer()
        if not user:
            return self._json({"error": "Neplatný token."}, 401)
        code = _handoff_new(self._bearer_token())
        scheme = "https" if self._https() else "http"
        host = self._public_host()
        return self._json({"code": code, "ttl": HANDOFF_TTL,
                           "url": f"{scheme}://{host}/login?handoff={code}"})

    # ---- Claude na vlastním předplatném ----
    _hlas_slots = threading.BoundedSemaphore(2)   # přepisy naráz (paměť, procesor)

    def _gw_prenos(self, method):
        """Kus balíčku s chatem z počítače. Brána ho jen předá hubu v prostoru
        uživatele (nástroj `prenos_prijmout`) — ten si chat zapíše sám, jako
        svůj, takže ho Claude Code může dál doplňovat. Poslední kus chat
        rozbalí a hub si ho ukáže k otevření."""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length > 14 * 1024 * 1024:
            self.close_connection = True
            return self._json({"error": "Kus balíčku je moc velký."}, 413)
        if method != "POST":
            return self._json({"error": "Jen POST."}, 405)
        user = self._bearer()
        form = self._read_form()
        if not user:
            return self._json({"error": "Nepřihlášeno."}, 401)
        try:
            result = mcp._hub_call(self, user, "prenos_prijmout", {
                "id": str(form.get("id") or ""), "part": form.get("part"),
                "parts": form.get("parts"), "zip": str(form.get("zip") or ""),
                "from": str(form.get("from") or "")[:80],
                "title": str(form.get("title") or "")[:120]}, timeout=180)
        except mcp.ToolError as exc:
            return self._json({"ok": False, "error": str(exc)}, 400)
        return self._json({"ok": True, **(result if isinstance(result, dict) else {})})

    def _gw_mcp_pc(self, route, method):
        """Most z počítače na napojení v prostoru (tools/server_napojeni_mcp.py).
        Jen token zařízení; brána u každé zprávy znovu ověří, že napojení
        člověku patří (nebo ho s ním někdo sdílí). Klíče z brány neodejdou."""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length > 8 * 1024 * 1024:
            self.close_connection = True
            return self._json({"error": "Zpráva je moc velká."}, 413)
        form = self._read_form() if method == "POST" else {}
        user = self._bearer()
        if not user:
            return self._json({"error": "Nepřihlášeno."}, 401)
        if route == "/gw/mcp-pc":
            return self._json({"ok": True, "napojeni": mcp_pc.seznam(user)})
        if method != "POST":
            return self._json({"error": "Jen POST."}, 405)
        try:
            out = mcp_pc.volani(self, user, form.get("jmeno"), form.get("relace"), form.get("zprava"))
        except PermissionError as exc:
            return self._json({"ok": False, "error": str(exc), "gone": True})
        except (ValueError, RuntimeError) as exc:
            return self._json({"ok": False, "error": str(exc)})
        return self._json({"ok": True, "zpravy": out})

    def _gw_zrcadlo(self, route, method):
        """Kopie chatů z počítače (hub/zrcadlo.py ↔ gateway/zrcadlo.py).

        Jen token zařízení — appka na počítači. Řádky se zapisují rovnou do
        domova člověka, prostor se kvůli tomu nespouští; jen když si počítač
        bere chat, který na serveru zrovna běží, řekne se prostoru, ať ho po
        dopsání odpovědi pustí."""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length > 14 * 1024 * 1024:
            self.close_connection = True
            return self._json({"error": "Dávka je moc velká."}, 413)
        form = self._read_form() if method == "POST" else {}
        user = self._bearer()
        if not user:
            return self._json({"error": "Nepřihlášeno."}, 401)
        try:
            if route == "/gw/zrcadlo/seznam":
                return self._json({"ok": True, "chaty": zrcadlo.seznam(user)})
            if method != "POST":
                return self._json({"error": "Jen POST."}, 405)
            if route == "/gw/zrcadlo/sync":
                chaty = form.get("chaty") if isinstance(form.get("chaty"), list) else []
                uvolneno = form.get("uvolneno") if isinstance(form.get("uvolneno"), list) else []
                return self._json(zrcadlo.sync(user, form.get("pocitac"), form.get("jmeno"),
                                               chaty[:50], [str(c) for c in uvolneno][:50]))
            if route == "/gw/zrcadlo/stahnout":
                return self._json(zrcadlo.stahnout(user, form.get("chat"), form.get("od")))

            def uvolni(chat):
                proc = self.hubs.procs.get(user["id"]) if self.hubs else None
                if not proc or not proc.alive():
                    return True
                out = hub_post(proc, "zrcadlo-uvolnit", {"chat": chat}, timeout=170)
                return bool(out.get("ok"))
            return self._json(zrcadlo.chci_na_pocitac(user, form.get("chat"), form.get("pocitac"), uvolni))
        except ValueError as exc:
            return self._json({"ok": False, "error": str(exc)}, 400)
        except OSError as exc:
            _errlog("zrcadlo", exc)
            return self._json({"ok": False, "error": "Chat se na serveru nepodařilo zapsat."}, 500)

    def _gw_hlas(self, method):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length > 20 * 1024 * 1024:
            self.close_connection = True
            return self._json({"error": "Nahrávka je moc dlouhá."}, 413)
        form = self._read_form()
        if method != "POST":
            return self._json({"error": "Jen POST."}, 405)
        user = self._bearer()
        if not user:
            return self._json({"error": "Nepřihlášeno."}, 401)
        from hub import hlas
        if not hlas.root():
            return self._json({"error": "Na serveru není hlas nainstalovaný."}, 503)
        if not self._hlas_slots.acquire(timeout=60):
            return self._json({"error": "Server teď přepisuje za ostatní — zkus to za chvíli."}, 503)
        try:
            import base64
            wav = base64.b64decode(str(form.get("wav") or ""), validate=False)
            text = hlas.prepis_zde(wav, str(form.get("model") or ""))
        except ValueError as exc:
            return self._json({"error": str(exc)}, 400)
        except (RuntimeError, subprocess.TimeoutExpired) as exc:
            _errlog("hlas prepis", exc)
            return self._json({"error": "Přepsat nahrávku se nepovedlo, zkus to prosím znovu."}, 500)
        finally:
            self._hlas_slots.release()
        return self._json({"text": text})

    def _gw_claude(self, method):
        """Na čem Claude v prostoru jede; připojení a odpojení předplatného.

        Appka na počítači se prokazuje tokenem zařízení (token předplatného
        vyrobí `claude setup-token` u člověka na počítači a pošle ho sem).
        Z prohlížeče jen s cookie, ze stránky brány a s hlavičkou — Claude
        v prostoru cookie nemá, takže si předplatné nepřepíše.

        Předplatných může mít člověk víc (gateway/predplatna.py): POST umí
        přidat, obnovit, přejmenovat, odebrat a přepnout; `?limity=1` přidá
        skutečný limit každého z nich. Z prohlížeče v prostoru navíc
        `pridat` — přihlášení přímo v prostoru, bez appky na počítači.
        """
        from . import predplatna
        bearer = self._bearer_token()
        user = self._bearer() if bearer else self._user()
        if not user:
            return self._json({"error": "Nepřihlášeno."}, 401)
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        pridano = ""
        pridani = None
        if method == "POST":
            if not bearer and not self._account_post_ok():
                return self._json({"error": "Jde to jen z nastavení aplikace."}, 403)
            form = self._read_form()
            if form.get("pridat") and not bearer:
                err = self._predplatne_pridat(user, form)
                if err:
                    return err
                pridani = True
            else:
                try:
                    zmena, pridano = predplatna.uprav(user, form, bool(bearer),
                                                      self.headers.get("User-Agent", ""))
                except ValueError as exc:
                    return self._json({"error": str(exc)}, 400)
                # Běžící prostor dostane nové přihlášení hned — nové chaty na
                # něm jedou bez restartu, rozdělaná práce doběhne na původním
                # (hub/predplatne.py, _prihlaseni).
                if zmena:
                    self._preved_prihlaseni(user)
        if pridani or (not bearer and query.get("pridani")):
            pridani = self._predplatne_pridani(user)
        state = workspace.claude_state(user)
        proc = self.hubs.procs.get(user["id"]) if self.hubs else None
        state["running"] = bool(proc and proc.alive())
        state["needs_restart"] = bool(state["running"] and
                                      proc.auth_mark != workspace.auth_mark(user))
        if pridano:
            state["pridano"] = pridano
        if pridani is not None:
            state["pridani"] = pridani
        if query.get("limity") and state["predplatna"]:
            try:
                cekat = min(8.0, max(0.0, float((query.get("cekat") or ["0"])[0])))
            except ValueError:
                cekat = 0.0
            state.update(predplatna.limity(user, cekat=cekat,
                                           obnovit=(query.get("obnovit") or [""])[0] == "1"))
        return self._json(state)

    def _predplatne_pridat(self, user, form):
        """Přidání předplatného přímo v prostoru: `claude setup-token` běží
        v hubu prostoru (stejně jako u Správy předplatného hostem), člověk
        otevře odkaz, klikne na Authorize a vloží kód. Token si brána
        vyzvedne v _predplatne_pridani. Vrací chybovou odpověď, nebo None."""
        from . import predplatna
        akce = str(form.get("pridat") or "")
        if akce not in ("start", "kod", "zrusit"):
            return self._json({"error": "Neznámá akce."}, 400)
        if akce == "start":
            try:
                hub = self.hubs.get(user)
            except RuntimeError as exc:
                return self._json({"error": str(exc)}, 503)
            predplatna.cekej(user, form.get("replace"), form.get("label"))
        else:
            hub = self.hubs.procs.get(user["id"]) if self.hubs else None
            if not hub or not hub.alive():
                return self._json({"error": "Prostor neběží — začni přihlášení znovu."}, 409)
        out = self._hub_call(hub, "predplatne-host",
                             {"action": {"start": "start", "kod": "kod", "zrusit": "zrusit"}[akce],
                              "code": str(form.get("kod") or "")[:500]})
        if akce == "zrusit":
            predplatna.zrus(user)
        if out.get("error") and not out.get("state"):
            return self._json({"error": out["error"]}, 502)
        return None

    def _predplatne_pridani(self, user):
        """Jak je na tom přidání v prostoru ({state, url, error}); hotový token
        si rovnou vyzvedne a uloží."""
        proc = self.hubs.procs.get(user["id"]) if self.hubs else None
        if not proc or not proc.alive():
            return {"state": "idle", "url": "", "error": ""}
        st = self._hub_call(proc, "predplatne-host", {"action": "stav"}, timeout=10)
        out = {k: st.get(k, "") for k in ("state", "url", "error")}
        if st.get("state") == "token":
            out, sid = self._vyzvedni_predplatne(user, proc, "v prostoru")
            if sid:
                out["id"] = sid
        return out

    def _vyzvedni_predplatne(self, owner, proc, source):
        """Token z `claude setup-token` v hubu prostoru → k předplatným účtu.
        Vrací ({state, url, error}, id přidaného nebo "")."""
        from . import predplatna
        token = self._hub_call(proc, "predplatne-host", {"action": "vyzvednout"}).get("token")
        meta = predplatna.vyzvedni_meta(owner)
        try:
            sid = workspace.save_predplatne(owner, token, source, label=meta["label"],
                                            replace=meta["replace"])
        except ValueError as exc:
            return {"state": "error", "url": "", "error": str(exc)}, ""
        finally:
            del token
        self._preved_prihlaseni(owner)
        return {"state": "done", "url": "", "error": ""}, sid

    # ---- most na počítač uživatele (gateway/pocitac.py) ----
    def _pocitac_poll(self, method):
        """Počítač čeká na úkoly. Drží se až POLL_WAIT sekund."""
        if method != "POST":
            return self._json({"error": "Jen POST."}, 405)
        user = self._bearer()
        if not user:
            return self._json({"error": "Neplatný token."}, 401)
        form = self._read_form()
        info = form.get("computer") if isinstance(form.get("computer"), dict) else {}
        broker = self.server.pocitac
        if form.get("bye") is True:
            return self._json({"ok": broker.bye(user["id"], info.get("id"))})
        try:
            wait = float(form.get("wait", pocitac.POLL_WAIT))
        except (TypeError, ValueError):
            wait = pocitac.POLL_WAIT
        try:
            tasks = broker.poll(user["id"], info, wait)
        except ValueError as exc:
            return self._json({"error": str(exc)}, 400)
        try:
            return self._json({"tasks": tasks,
                               "prikazy": broker.take_directives(user["id"], info.get("id")),
                               "ukoly": broker.ukoly_offer(user["id"], info.get("id"))})
        except OSError:
            # Počítač mezitím spojení zavřel — úkoly nesmí propadnout.
            broker.requeue(user["id"], info.get("id"), tasks)
            raise

    # Co smí Claude z počítače v prostoru téhož člověka: vidět sessions, číst je a psát
    # do nich. Nic dalšího — soubory a příkazy v prostoru jdou přes jeho vlastní cestu.
    _PROSTOR_NASTROJE = frozenset({"session_seznam", "sdilet_cteni", "sdilet_poslat"})

    def _prostor_volani(self, method):
        """Claude na počítači (tools/server_mcp.py přes hub) sahá do sessions v prostoru
        svého majitele. Token zařízení = jeho účet, jiný prostor tudy nedosáhne."""
        if method != "POST":
            return self._json({"error": "Jen POST."}, 405)
        user = self._bearer()
        if not user:
            return self._json({"error": "Neplatný token."}, 401)
        form = self._read_form()
        tool = str(form.get("tool") or "")
        if tool not in self._PROSTOR_NASTROJE:
            return self._json({"ok": False, "error": "Tohle z počítače nejde."}, 400)
        args = form.get("args") if isinstance(form.get("args"), dict) else {}
        if tool == "sdilet_poslat":
            args = {**args, "kdo": "z počítače"}
        try:
            result = mcp._hub_call(self, user, tool, args, timeout=30)
        except mcp.ToolError as exc:
            return self._json({"ok": False, "error": str(exc)})
        return self._json({"ok": True, "result": result})

    def _pocitac_schvaleni(self, method):
        """Počítač hlásí, na co čeká od člověka (žádost o souhlas) — ať se to
        ukáže v prostoru hned, ne až s příštím dotazem."""
        if method != "POST":
            return self._json({"error": "Jen POST."}, 405)
        user = self._bearer()
        if not user:
            return self._json({"error": "Neplatný token."}, 401)
        form = self._read_form()
        ok = self.server.pocitac.update_pending(user["id"], form.get("computer"),
                                                form.get("pending"), form.get("grants"))
        return self._json({"ok": ok})

    def _pocitac_clovek(self, route, method, user):
        """Rozhodnutí člověka z prostoru: souhlas s úkolem počítače, změna přístupu.

        Jen s přihlašovací cookie a jen ze stránky hubu (_account_post_ok). Claude
        v prostoru má jen žeton prostoru, který tyhle adresy nezná — nemůže si
        tedy souhlas dát sám."""
        if method != "POST":
            return self._json({"error": "Jen POST."}, 405)
        form = self._read_form()
        if not self._account_post_ok():
            return self._json({"error": "Tohle jde jen ze stránky hubu."}, 403)
        broker = self.server.pocitac
        if route.endswith("/rozhodnout"):
            out = broker.decide(user["id"], str(form.get("computer") or ""),
                                form.get("id"), str(form.get("volba") or ""),
                                form.get("odpovedi"))
        else:
            out = broker.set_access(user["id"], str(form.get("computer") or ""),
                                    str(form.get("level") or ""))
        return self._json(out, 200 if out.get("ok") else 400)

    def _pocitac_reply(self, method):
        if method != "POST":
            return self._json({"error": "Jen POST."}, 405)
        user = self._bearer()
        if not user:
            return self._json({"error": "Neplatný token."}, 401)
        form = self._read_form()
        result = {"ok": form.get("ok") is True, "result": form.get("result"),
                  "error": str(form.get("error") or "")[:2000]}
        ok = self.server.pocitac.reply(user["id"], form.get("computer"), form.get("id"), result)
        return self._json({"ok": ok})

    def _pocitac_ukol(self, method):
        """Úkol na později: počítač si ho stáhne (take) a hlásí, kde je (state)."""
        if method != "POST":
            return self._json({"error": "Jen POST."}, 405)
        user = self._bearer()
        if not user:
            return self._json({"error": "Neplatný token."}, 401)
        form = self._read_form()
        broker = self.server.pocitac
        if form.get("action") == "take":
            return self._json(broker.ukol_take(user["id"], form.get("computer"), form.get("id"),
                                               bez_dat=bool(form.get("bez_dat"))))
        if form.get("action") == "soubor":
            return self._json(broker.ukol_soubor(user["id"], form.get("computer"), form.get("id"),
                                                 str(form.get("name") or "")))
        return self._json(broker.ukol_mark(user["id"], form.get("computer"), form.get("id"),
                                           str(form.get("state") or "")))

    def _pocitac_call(self, method):
        """Úkol od Clauda z prostoru pro počítač jeho uživatele.

        Volá se jen z prostoru přímo na loopback brány. Přes nginx ne: ten
        vždycky přidá X-Real-IP, takže žeton, kdyby unikl, zvenku nic neotevře.
        """
        if self.headers.get("X-Real-IP") or self.headers.get("X-Forwarded-For"):
            return self._send(404, b"404")
        if method != "POST":
            return self._json({"error": "Jen POST."}, 405)
        user = self.hubs.user_for_pocitac(self.headers.get("X-Hub-Pocitac", "")) \
            if self.hubs else None
        if not user:
            return self._json({"ok": False, "error": "Neplatný žeton prostoru."}, 401)
        form = self._read_form()
        op = str(form.get("op") or "")
        args = form.get("args") if isinstance(form.get("args"), dict) else {}
        broker = self.server.pocitac
        if op == "list":
            return self._json({"ok": True, "computers": broker.list(user["id"]),
                               "ukoly": broker.ukoly(user["id"])})
        # Úkoly na později: počítač teď připojený být nemusí.
        if op == "ukol-novy":
            return self._json(broker.ukol_new(user["id"], args))
        if op == "ukol-kus":
            return self._json(broker.ukol_kus(user["id"], args))
        if op == "ukol-zrusit":
            return self._json(broker.ukol_cancel(user["id"], args.get("id")))
        # Chaty z počítače (gateway/zrcadlo.py): kde který běží, a převzetí na server.
        if op == "zrcadla":
            return self._json({"ok": True, "chaty": zrcadlo.stav(user)})
        if op == "zrcadlo-na-server":
            return self._json(zrcadlo.chci_na_server(user, args.get("chat")))
        return self._json(broker.call(user["id"], op, args, str(form.get("computer") or "")))

    def _mcp_shared(self, method, user):
        """Sdílená napojení z Nastavení → Napojení (gateway/mcp_sdilene.py).

        Klíče a hesla sem posílá prohlížeč rovnou — hub v prostoru je nikdy
        nevidí. Změny jen ze stránky hubu (stejný původ + X-Hub-Account)."""
        if method == "GET":
            return self._json({"napojeni": mcp_sdilene.for_user(user),
                               "people": shared.people(),
                               # Podkategorie firem, kam smí sdílet (gateway/firmy.py).
                               "skupiny": firmy.skupiny_pro(user)})
        if method != "POST":
            return self._json({"error": "Jen GET nebo POST."}, 405)
        form = self._read_form()
        if not self._account_post_ok():
            return self._json({"error": "Sdílená napojení jde měnit jen v aplikaci."}, 403)
        action = str(form.get("akce") or "")
        try:
            if action == "zalozit":
                result = mcp_sdilene.create(user, form)
            elif action == "upravit":
                result = mcp_sdilene.update(user, form)
            elif action == "smazat":
                result = mcp_sdilene.delete(user, form.get("slug"))
            elif action == "sdilet-ucet":
                result = mcp_sdilene.share_account(user, form)
            elif action == "test":
                if not mcp_sdilene.member(user, form.get("slug")):
                    raise ValueError("Takové napojení nemáš.")
                result = {"ok": True, "tools": mcp_sdilene.test(user, form.get("slug"))}
            else:
                return self._json({"error": "Neznámá akce."}, 400)
        except PermissionError as exc:
            return self._json({"error": str(exc)}, 403)
        except (ValueError, RuntimeError) as exc:
            return self._json({"error": str(exc)}, 400)
        return self._json(result)

    def _mcp_shared_call(self, method):
        """Zpráva z mostu v prostoru pro sdílené napojení. Jen loopback, jen
        se žetonem běžícího prostoru; členství se ověřuje u každé zprávy."""
        if self.headers.get("X-Real-IP") or self.headers.get("X-Forwarded-For"):
            return self._send(404, b"404")
        if method != "POST":
            return self._json({"error": "Jen POST."}, 405)
        user = self.hubs.user_for_pocitac(self.headers.get("X-Hub-Pocitac", "")) \
            if self.hubs else None
        if not user:
            return self._json({"ok": False, "error": "Neplatný žeton prostoru."}, 401)
        form = self._read_form()
        if form.get("op") == "seznam":
            return self._json({"ok": True, "napojeni": [
                {"slug": n["slug"], "name": n["name"], "owner": n["owner_name"] or n["owner"],
                 "mcp_name": n["mcp_name"]} for n in mcp_sdilene.for_user(user)
                # Svůj účet ze služby má vlastník v prostoru napřímo.
                if not (n.get("kind") == "ucet" and n.get("is_owner"))]})
        try:
            out = mcp_sdilene.call(user, form.get("slug"), form.get("relace"), form.get("zprava"))
        except PermissionError as exc:
            return self._json({"ok": False, "error": str(exc), "gone": True})
        except (ValueError, RuntimeError) as exc:
            return self._json({"ok": False, "error": str(exc)})
        return self._json({"ok": True, "zpravy": out})

    def _logout(self):
        token = self._cookies().get(config.SESSION_COOKIE, "")
        if token:
            self.accounts.revoke(token)
        return self._redirect("/login", extra={"Set-Cookie": self._clear_cookie()})

    def _read_form(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        ctype = self.headers.get("Content-Type", "")
        if ctype.startswith("application/json"):
            try:
                return json.loads(raw or b"{}")
            except Exception:
                return {}
        parsed = urllib.parse.parse_qs(raw.decode("utf-8", "replace"))
        return {k: v[0] for k, v in parsed.items()}


    # ---- proxy do instance hubu ----
    def _proxy(self, method, user):
        try:
            hub = self.hubs.get(user)
        except RuntimeError as exc:
            _errlog("proxy start", exc)
            full = "plný" in str(exc)
            return self._fail(503, MSG_FULL if full else MSG_STARTING)
        hub.touch()
        if "websocket" in self.headers.get("Upgrade", "").lower():
            return self._proxy_ws(hub)
        # Načtení stránky prodlouží přihlášení o celou dobu znovu (posuvná
        # platnost). Dřív platila cookie pevně 30 dní od přihlášení a pak
        # člověka bez varování odhlásila, i když bránu používal každý den.
        obnov = method == "GET" and urllib.parse.urlsplit(self.path).path in ("/", "/index.html")
        return self._proxy_http(method, hub, obnov_cookie=obnov)

    def _forward_headers(self, hub, extra=None):
        out = {}
        for key in self.headers.keys():
            if key.lower() in HOP_BY_HOP or key.lower() == "x-hub-token":
                continue
            out[key] = self.headers.get(key)
        # Identitu hosta (gateway/pristup.py) vkládá jen brána — co přijde
        # zvenku, zahodit, ať se nikdo nevydává za hosta ani za vlastníka.
        pristup.strip_guest_headers(out)
        # Přihlašovací cookie brány do prostoru nepatří: běží v něm Claude
        # a s ní by si mohl sám potvrdit třeba nahrání do firemního Obsidianu.
        # Hub se ověřuje tokenem X-Hub-Token, cookie brány nepotřebuje.
        for key in [k for k in out if k.lower() == "cookie"]:
            kept = [c.strip() for c in out[key].split(";")
                    if c.strip() and c.split("=", 1)[0].strip() != config.SESSION_COOKIE]
            kept = [c for c in kept if c.split("=", 1)[0].strip() != pristup.COOKIE]
            if kept:
                out[key] = "; ".join(kept)
            else:
                del out[key]
        # Hub pozná původ podle shody Host == Origin; drž veřejný host a řekni
        # mu, že spojení bylo přes https (kvůli Secure cookie uvnitř).
        out["Host"] = self._public_host() or f"127.0.0.1:{hub.port}"
        out["X-Forwarded-Proto"] = "https" if self._https() else "http"
        out["X-Forwarded-Host"] = self._public_host()
        # Token instance hubu vkládá brána — do prohlížeče se nikdy nedostane.
        out["X-Hub-Token"] = hub.token
        out.update(extra or {})
        return out

    def _proxy_http(self, method, hub, body=None, extra=None, guest=False, obnov_cookie=False):
        if body is None:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else None
        headers = self._forward_headers(hub, extra)
        idempotent = method in ("GET", "HEAD", "OPTIONS")
        resp = data = last = None
        proud = None
        # Instance hubu pod náporem paralelních požadavků občas spojení zavře
        # bez odpovědi (RemoteDisconnected). Reverzní proxy to nesmí propustit
        # jako 502 — zkusí to znovu na čerstvém spojení. `RemoteDisconnected`
        # znamená, že hub požadavek nezpracoval, takže opakovat je bezpečné
        # i u neidempotentních metod; jiné chyby opakujeme jen u GET/HEAD.
        for attempt in range(4):
            conn = http.client.HTTPConnection("127.0.0.1", hub.port, timeout=60)
            try:
                conn.request(method, self.path, body=body, headers=headers)
                resp = conn.getresponse()
                # Zvuk předčítání (hub/eleven.py) jde proudem — přehrává se od
                # prvních bajtů, bufferovat ho celý by zdrželo start o vteřiny.
                if (resp.getheader("Content-Type") or "").startswith("audio/") \
                        and resp.getheader("Content-Length") is None:
                    proud, data = conn, b""
                    break
                # Soubor ke stažení (hub /api/soubor, až 200 MB) taky proudem —
                # celý v paměti brány by ji při pár stahováních najednou dusil.
                if (resp.getheader("Content-Disposition") or "").startswith("attachment"):
                    proud, data = conn, b""
                    break
                data = resp.read()
                conn.close()
                break
            except (http.client.RemoteDisconnected, ConnectionError,
                    OSError) as exc:
                last = exc
                try:
                    conn.close()
                except Exception:
                    pass
                retryable = idempotent or isinstance(
                    exc, http.client.RemoteDisconnected)
                if not retryable or attempt == 3:
                    break
                time.sleep(0.05 * (attempt + 1))
            except Exception as exc:
                last = exc
                try:
                    conn.close()
                except Exception:
                    pass
                break
        if data is None:
            _errlog("proxy_http " + self.path, last)
            return self._fail(502, MSG_STARTING)
        self.send_response(resp.status)
        for key, value in resp.getheaders():
            low = key.lower()
            if low in HOP_BY_HOP or low == "content-length":
                continue
            # Cookie hubu (jeho token) nikdy ven do prohlížeče.
            if low == "set-cookie":
                continue
            self.send_header(key, value)
        if obnov_cookie:
            token = self._cookies().get(config.SESSION_COOKIE, "")
            if token:
                self.send_header("Set-Cookie", self._set_cookie(token))
        if guest and urllib.parse.urlsplit(self.path).path.startswith("/api/"):
            # Soubory z cizího prostoru (poznámky, obrázky) běží na stejném
            # původu jako brána — host s nimi nesmí spustit cizí skript.
            self.send_header("Content-Security-Policy",
                             "default-src 'none'; img-src 'self' data:; "
                             "style-src 'unsafe-inline'; sandbox")
        if proud is not None:
            delka = resp.getheader("Content-Length")
            if delka and delka.isdigit():
                self.send_header("Content-Length", delka)     # průběh stahování
            self.send_header("X-Accel-Buffering", "no")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            try:
                while True:
                    kus = resp.read1(8192)
                    if not kus:
                        break
                    self.wfile.write(kus)
                    self.wfile.flush()
            except (OSError, ValueError):
                pass
            finally:
                proud.close()
            return None
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if data and self.command != "HEAD":
            self.wfile.write(data)

    def _proxy_ws(self, hub, extra=None, guest=None):
        """Průhledný tunel WebSocketu mezi prohlížečem a instancí hubu.

        Handshake se přepošle beze změny (klíč klienta → stejný accept od
        hubu), pak už jen přeléváme bajty tam a zpět, dokud jedna strana
        nezavře. Rámce se neparsují — brána do obsahu nevidí, jen ho přepravuje.
        """
        try:
            up = socket.create_connection(("127.0.0.1", hub.port), timeout=15)
        except OSError as exc:
            _errlog("proxy_ws", exc)
            return self._fail(502, MSG_STARTING)
        # Sestav upgrade požadavek pro hub z hlaviček klienta + token.
        head = [f"GET {self.path} HTTP/1.1"]
        for key, value in self._forward_headers(
                hub, {"Connection": "Upgrade", "Upgrade": "websocket", **(extra or {})}).items():
            head.append(f"{key}: {value}")
        up.sendall(("\r\n".join(head) + "\r\n\r\n").encode("latin-1"))

        # Přečti odpověď hubu (hlavičky) a přepošli ji klientovi tak, jak je.
        resp = _read_until(up, b"\r\n\r\n")
        if not resp:
            up.close()
            return
        self.wfile.write(resp)
        self.wfile.flush()
        self.close_connection = True
        # Limit 15 s z create_connection platí jen pro navázání spojení.
        # V tunelu by recv() po 15 s ticha od hubu spadl na timeout a WebSocket
        # se každých ~17 s přerušil — klik v tu chvíli propadl.
        up.settimeout(None)

        client = self.connection
        # Psaní do terminálu jde jen tudy, žádným HTTP požadavkem. Bez tohohle
        # by se prostor uspal po IDLE_SLEEP i člověku, který celou dobu píše.
        # Připojení se počítá po dobu tunelu (_pump_both čeká na konec):
        # prostor s otevřeným prohlížečem se kvůli místu neuspává.
        hub.conns += 1
        try:
            if guest:
                self._pump_guest(client, up, hub, guest)
            else:
                _pump_both(client, up, on_client=hub.touch)
        finally:
            hub.conns -= 1

    def _pump_guest(self, client, up, hub, guest):
        """Tunel hosta: zprávy do hubu přes filtr role, povolení se ověřuje
        průběžně a odebrání spojení zavře (pristup.LIVE)."""
        owner, user, role = guest
        key = (owner["id"], user["id"])
        accounts = self.accounts

        def opened(msg):
            pristup.log(owner["id"], user["id"], "tab", druh=str(msg.get("kind") or "project")[:40],
                        nazev=str(msg.get("title") or "")[:60])

        def denied(msg, why):
            kind = msg.get("t") if isinstance(msg, dict) else "?"
            pristup.log(owner["id"], user["id"], "zamitnuto", zprava=str(kind)[:20])

        flt = pristup.WsFilter(role, lambda: accounts.access_role(*key),
                               on_open=opened, on_denied=denied)
        pristup.LIVE.add(key, client)
        try:
            pristup.pump_guest(client, up, flt, on_client=hub.touch)
        finally:
            pristup.LIVE.drop(key, client)

    # BaseHTTPRequestHandler používá tyhle pro neznámé metody; ať projdou.
    def do_PATCH(self):
        self._route("PATCH")


def _read_until(sock, marker):
    buf = b""
    while marker not in buf:
        try:
            chunk = sock.recv(4096)
        except OSError:
            return b""
        if not chunk:
            return b""
        buf += chunk
        if len(buf) > 65536:
            break
    return buf


def _pump_both(a, b, on_client=None):
    """Přelévej bajty oběma směry, dokud jedna strana nezavře.

    `on_client` se zavolá s každým kusem dat od prohlížeče (`a`) — tak brána
    pozná, že je prostor používaný. Výstup terminálu (`b` → `a`) se nepočítá:
    běžící příkaz, který něco vypisuje, ještě neznamená, že tam někdo je.
    """
    done = threading.Event()

    def pump(src, dst):
        try:
            while not done.is_set():
                data = src.recv(65536)
                if not data:
                    break
                if on_client and src is a:
                    on_client()
                dst.sendall(data)
        except OSError:
            pass
        finally:
            done.set()
            for s in (a, b):
                try:
                    s.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

    t1 = threading.Thread(target=pump, args=(a, b), daemon=True)
    t2 = threading.Thread(target=pump, args=(b, a), daemon=True)
    t1.start()
    t2.start()
    done.wait()
    t1.join(timeout=1)
    t2.join(timeout=1)
    try:
        b.close()
    except OSError:
        pass


# ── server ────────────────────────────────────────────────────────────────────
class Gateway(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, accounts, hubs, assume_https=False):
        super().__init__(address, Handler)
        self.accounts = accounts
        self.hubs = hubs
        self.assume_https = assume_https
        self.pocitac = pocitac.Broker(config.POCITAC_UKOLY_DIR)


def _loopback_url():
    """Kudy prostor na tomhle stroji dosáhne na bránu — mimo nginx."""
    host = config.HOST if config.HOST not in ("", "0.0.0.0", "::") else "127.0.0.1"
    if ":" in host:
        host = f"[{host}]"
    return f"http://{host}:{config.PORT}"


def stop_orphans():
    """Zastaví prostory, o kterých brána neví. Vrací jejich počet."""
    names = [name for name, _desc in isolation.running_scopes()]
    for name in names:
        isolation.stop_scope(name)
    if names:
        print(f"zastaveno osiřelých prostorů: {len(names)}", flush=True)
    return len(names)


def serve():
    problem = isolation.check(config.ISOLATION, config.HOST)
    if problem:
        raise SystemExit("Izolace: " + problem)
    # Úklid osiřelých instancí z minulého běhu: bez `--die-with-parent` po
    # restartu brány zůstanou běžet huby, které už nikdo nespravuje — a každý
    # s Claude Code drží stovky MB.
    stop_orphans()
    accounts = Accounts(config.DB_PATH, require_mfa=config.REQUIRE_2FA)
    shared.ACCOUNTS = accounts
    hubs = HubManager(config.ISOLATION, config.MAX_SESSIONS, config.IDLE_SLEEP)
    # assume_https zapneme, když je za bránou nginx s TLS (řekne to env).
    assume_https = os.environ.get("HUB_GW_ASSUME_HTTPS", "") == "1"
    gw = Gateway((config.HOST, config.PORT), accounts, hubs, assume_https)
    from . import spotreba, uloziste
    spotreba.sledovani(accounts.list)
    # Noční úklid, rozbor místa a oznámení (běží v samostatném procesu).
    uloziste.hlidac()
    where = f"{config.HOST}:{config.PORT}"
    print(f"brána poslouchá na {where}, izolace {config.ISOLATION}, "
          f"účtů {accounts.count()}", flush=True)
    try:
        gw.serve_forever(poll_interval=0.3)
    except KeyboardInterrupt:
        pass
    finally:
        hubs.stop_all()
        gw.shutdown()


if __name__ == "__main__":
    serve()
