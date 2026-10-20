"""
Přístup do cizího prostoru — vlastník pustí jiný účet z téže brány k sobě.

Vlastník v Nastavení → Účet → Přístup zadá e-mail existujícího účtu a roli;
host pak v liště přepne odznak prostoru na „Prostory, kam máš přístup“. Kam
host právě míří, drží cookie `gw_prostor` (id vlastníka). Cookie je jen
*přání* — o tom, jestli smí, rozhoduje při **každém** požadavku řádek
v `space_access` (accounts.py). Odebrání tak platí hned a otevřená spojení
(websocket terminálu) brána sama zavře.

Proč cookie, ne prefix v cestě: stránka hubu volá absolutní cesty (`/api/…`,
`/ws`) a brána by je musela přepisovat v HTML, JS i odpovědích. Cookie je
HttpOnly, nastavuje ji jen brána (POST se stejným původem a X-Hub-Account,
jako ostatní změny účtu) a do prostoru se nikdy nepřeposílá.

Role (vynucuje brána, ne stránka):

* `predplatne` — Správa předplatného: do hubu vlastníka **vůbec** — žádná
  stránka hubu, terminál ani Claude. Host dostane stránku brány „Správa
  předplatného“ a úzké API `/gw/pristup/predplatne` (stav, přihlášení přes
  `claude setup-token` spuštěné skrytě v hubu vlastníka, kód, zrušení,
  odpojení). Token i výstup terminálu zůstávají mezi hubem a bránou.
* `pomocnik` — Pomocník: práce v prostoru, ale bez mazání a bez nastavení,
  napojení, účtu a aktualizací — blacklist.
* `plny` — Plný přístup: jako vlastník v hubu. Správa přístupů, heslo, 2FA,
  passkeys a smazání účtu jsou na bráně (/gw/…) a ty jdou vždycky za tím,
  kdo je přihlášený — host na vlastníkovy nikdy nedosáhne.

Hostující hub se dozví, kdo je host, z hlavičky `X-Hub-Guest`, kterou vkládá
jen brána (z příchozích požadavků ji vždycky zahodí). Podle ní u tabu ukáže
„spravuje …“ a sám ještě jednou pohlídá, na které taby host smí.

Záznam (kdo, kdy, co) jde do `GATEWAY_DIR/pristup.jsonl`.
"""
import base64
import json
import os
import socket
import struct
import threading
import time

from . import config

COOKIE = "gw_prostor"
GUEST_HEADER = "X-Hub-Guest"
LOG = os.path.join(config.GATEWAY_DIR, "pristup.jsonl")

ROLES = ("predplatne", "pomocnik", "plny")
ROLE_LABELS = {"predplatne": "Správa předplatného", "pomocnik": "Pomocník",
               "plny": "Plný přístup"}

# Co v prostoru nesmí žádný host: vnitřní volání brány a obnova tabů po
# restartu (čte se jednou — host by ji vlastníkovi „snědl“).
DENY_ALL_GUESTS = {"prostor-snapshot", "restore", "mcp-tool", "predplatne-host"}

# Správa předplatného: akce na /gw/pristup/predplatne (POST). Nic jiného.
PREDPLATNE_AKCE = {"prihlasit", "kod", "zrusit", "odpojit"}

# Pomocník: tohle nikdy (mění nastavení, účet, napojení, maže, nebo sahá na
# tajemství), ani GETem — řada akcí hubu běží i bez těla požadavku.
POMOCNIK_DENY = {
    "config", "account", "connect", "mcp", "predplatne", "remote", "restart",
    "update", "quit", "bypass", "setup", "okno-otevrit", "log-clear",
    "vault-delete", "vault", "hlas-install", "hlas-eleven", "jev", "clockify",
    "claude-ucet", "chat-delete",
}
# … a tohle jen ke čtení (POST mění nastavení).
POMOCNIK_READ_ONLY = {"app-vzhled", "pluginy", "pocitac", "agents", "auto-model-keep"}
# Akce v těle POSTu, které mažou.
POMOCNIK_DENY_ACTIONS = {"project": {"remove"}, "firma": {"discard"}}

_log_lock = threading.Lock()


def label(role):
    return ROLE_LABELS.get(role, role)


def log(owner_id, guest_id, akce, **extra):
    entry = {"cas": time.strftime("%Y-%m-%d %H:%M:%S"), "ts": time.time(),
             "vlastnik": int(owner_id or 0), "host": int(guest_id or 0), "akce": akce, **extra}
    with _log_lock:
        try:
            os.makedirs(os.path.dirname(LOG), exist_ok=True)
            with open(LOG, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError:
            pass


def read_log(owner_id=None, limit=200):
    """Posledních `limit` záznamů (pro vlastníka jen jeho prostor), nejnovější první."""
    out = []
    try:
        with open(LOG, encoding="utf-8") as fh:
            lines = fh.readlines()[-5000:]
    except OSError:
        return []
    for line in reversed(lines):
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if owner_id is not None and e.get("vlastnik") != owner_id:
            continue
        out.append(e)
        if len(out) >= limit:
            break
    return out


# ── HTTP ─────────────────────────────────────────────────────────────────────
def http_denied(role, method, path, query="", body=b""):
    """Proč host s rolí nesmí na tuhle cestu hubu, nebo None, když smí.

    `path` je cesta bez dotazu, `query` surový dotaz, `body` tělo POSTu."""
    if role not in ROLES:
        return "Neznámá role."
    if role == "predplatne":
        # Do hubu nic: stránku „Správa předplatného“ dává brána sama.
        return "Správa předplatného smí jen spravovat předplatné."
    if path == "/ws" or path.startswith("/ws/"):
        return None                       # zprávy hlídá filtr websocketu
    if not path.startswith("/api/"):
        return None                       # stránka a statika hubu
    name = path[5:].strip("/")
    if name in DENY_ALL_GUESTS:
        return "Tohle v cizím prostoru nejde."
    if role == "plny":
        return None
    # pomocnik
    if name in POMOCNIK_DENY:
        return "Pomocník nemění nastavení, napojení ani účet."
    if method not in ("GET", "HEAD"):
        if name in POMOCNIK_READ_ONLY:
            return "Pomocník nemění nastavení."
        bad = POMOCNIK_DENY_ACTIONS.get(name)
        if bad:
            try:
                action = (json.loads(body or b"{}") or {}).get("action")
            except (ValueError, AttributeError):
                return "Nečitelný požadavek."
            if action in bad:
                return "Pomocník nemaže."
    return None


def guest_header(guest, owner, role):
    """Hodnota X-Hub-Guest — base64 kvůli diakritice ve jménu."""
    raw = json.dumps({"id": guest["id"], "name": guest.get("name") or "",
                      "email": guest.get("email") or "", "role": role,
                      "label": label(role), "owner": owner["id"],
                      "owner_name": owner.get("name") or owner.get("email") or ""},
                     ensure_ascii=False)
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")


def strip_guest_headers(headers):
    """Z hlaviček pro hub vyhodí cokoli, co se tváří jako identita hosta
    (bez ohledu na velikost písmen). Vkládá ji jen brána."""
    for key in [k for k in headers if k.lower() == GUEST_HEADER.lower()
                or k.lower().startswith("x-hub-guest")]:
        del headers[key]
    return headers


# ── websocket ────────────────────────────────────────────────────────────────
MAX_WS_FRAME = 2 * 1024 * 1024


def ws_denied(role, message):
    """Proč host nesmí poslat tuhle zprávu (už rozbalený JSON), nebo None."""
    if role == "predplatne":
        return "Správa předplatného terminál nedostane."
    return None


class _Live:
    """Otevřená spojení hostů: (vlastník, host) → sockety. Odebrání přístupu je
    zavře hned, nečeká se na další zprávu."""

    def __init__(self):
        self._lock = threading.Lock()
        self._map = {}

    def add(self, key, sock):
        with self._lock:
            self._map.setdefault(key, set()).add(sock)

    def drop(self, key, sock):
        with self._lock:
            s = self._map.get(key)
            if s:
                s.discard(sock)
                if not s:
                    self._map.pop(key, None)

    def kick(self, owner_id=None, guest_id=None):
        with self._lock:
            keys = [k for k in self._map
                    if (owner_id is None or k[0] == owner_id)
                    and (guest_id is None or k[1] == guest_id)]
            socks = [s for k in keys for s in self._map.pop(k, ())]
        for s in socks:
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        return len(socks)

    def count(self, owner_id, guest_id):
        with self._lock:
            return len(self._map.get((owner_id, guest_id), ()))


LIVE = _Live()


def _parse_frame(buf):
    """(délka rámce, fin, opcode, rozbalená data) nebo None, když ještě není celý.
    Vyhodí ValueError u rámce, který hostovi nepropustíme (nemaskovaný, velký)."""
    if len(buf) < 2:
        return None
    b1, b2 = buf[0], buf[1]
    fin, opcode = bool(b1 & 0x80), b1 & 0x0F
    if not b2 & 0x80:
        raise ValueError("nemaskovaný rámec od prohlížeče")
    length, pos = b2 & 0x7F, 2
    if length == 126:
        if len(buf) < 4:
            return None
        length, pos = struct.unpack(">H", buf[2:4])[0], 4
    elif length == 127:
        if len(buf) < 10:
            return None
        length, pos = struct.unpack(">Q", buf[2:10])[0], 10
    if length > MAX_WS_FRAME:
        raise ValueError("příliš velký rámec")
    end = pos + 4 + length
    if len(buf) < end:
        return None
    mask = buf[pos:pos + 4]
    data = bytes(b ^ mask[i % 4] for i, b in enumerate(buf[pos + 4:end]))
    return end, fin, opcode, data


class WsFilter:
    """Hlídá zprávy hosta do hubu: rámec po rámci, přeposílá původní bajty.

    `check()` vrací aktuální roli z DB (None = přístup odebrán); volá se
    nejvýš jednou za RECHECK vteřin. `on_open(msg)` zapíše otevření tabu."""
    RECHECK = 3.0

    def __init__(self, role, check, on_open=None, on_denied=None):
        self.role = role
        self.check = check
        self.on_open = on_open
        self.on_denied = on_denied
        self.buf = b""
        self.checked = time.time()

    def feed(self, data):
        """Vrací bajty k přeposlání; ValueError = spojení zavřít."""
        now = time.time()
        if now - self.checked >= self.RECHECK:
            self.checked = now
            if self.check() != self.role:
                raise ValueError("přístup odebrán nebo změněn")
        self.buf += data
        out = b""
        while True:
            got = _parse_frame(self.buf)
            if got is None:
                if len(self.buf) > MAX_WS_FRAME + 16:
                    raise ValueError("příliš velký rámec")
                return out
            end, fin, opcode, payload = got
            raw, self.buf = self.buf[:end], self.buf[end:]
            if opcode >= 0x8:                       # close, ping, pong
                out += raw
                continue
            if not fin or opcode == 0x0:
                # Rozdělené zprávy stránka hubu neposílá — a rozdělenou nejde
                # posoudit, tak ji host radši nedostane.
                if self.role == "predplatne":
                    raise ValueError("rozdělená zpráva")
                out += raw
                continue
            if opcode == 0x1:
                try:
                    msg = json.loads(payload.decode("utf-8"))
                except (ValueError, UnicodeDecodeError):
                    msg = None
                why = ws_denied(self.role, msg)
                if why:
                    if self.on_denied:
                        self.on_denied(msg, why)
                    continue                         # zahodit, hub ji nedostane
                if isinstance(msg, dict) and msg.get("t") == "open" and self.on_open:
                    self.on_open(msg)
                out += raw
            elif self.role == "predplatne":
                continue                             # binární zprávy hub nečeká
            else:
                out += raw


def pump_guest(client, up, flt, on_client=None):
    """Jako server._pump_both, jen prohlížeč → hub jde přes filtr."""
    done = threading.Event()

    def close_all():
        done.set()
        for s in (client, up):
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def from_client():
        try:
            while not done.is_set():
                data = client.recv(65536)
                if not data:
                    break
                if on_client:
                    on_client()
                out = flt.feed(data)
                if out:
                    up.sendall(out)
        except (OSError, ValueError):
            pass
        finally:
            close_all()

    def from_hub():
        try:
            while not done.is_set():
                data = up.recv(65536)
                if not data:
                    break
                client.sendall(data)
        except OSError:
            pass
        finally:
            close_all()

    t1 = threading.Thread(target=from_client, daemon=True)
    t2 = threading.Thread(target=from_hub, daemon=True)
    t1.start()
    t2.start()
    done.wait()
    t1.join(timeout=1)
    t2.join(timeout=1)
    try:
        up.close()
    except OSError:
        pass


# ── správa z Nastavení → Účet → Přístup ──────────────────────────────────────
def _person(row, prefix):
    return {"id": row[prefix + "_id"], "email": row[prefix + "_email"],
            "name": row[prefix + "_name"] or ""}


def overview(accounts, user, here=None):
    """Co stránka potřebuje: komu jsem dal přístup, kam smím, kde teď jsem."""
    mine = [{**_person(r, "guest"), "role": r["role"], "label": label(r["role"]),
             "created": r["created"], "online": LIVE.count(user["id"], r["guest_id"]) > 0}
            for r in accounts.access_list(owner_id=user["id"])]
    kam = [{**_person(r, "owner"), "role": r["role"], "label": label(r["role"])}
           for r in accounts.access_list(guest_id=user["id"])]
    names = {r["id"]: (r["name"] or r["email"]) for r in accounts.list()}
    zaznam = [{**e, "host_jmeno": names.get(e.get("host"), "?"),
               "vlastnik_jmeno": names.get(e.get("vlastnik"), "?")}
              for e in read_log(user["id"], 100)]
    out = {"moje": mine, "kam": kam, "zaznam": zaznam,
           "role": [{"id": r, "label": ROLE_LABELS[r]} for r in ROLES],
           "tady": here}
    if user.get("role") == "admin":
        out["vse"] = [{"vlastnik": _person(r, "owner"), "host": _person(r, "guest"),
                       "role": r["role"], "label": label(r["role"])}
                      for r in accounts.access_list()]
    return out


def act(accounts, user, form):
    """Jedna změna. Vlastník = vždycky přihlášený `user` (admin smí jen rušit)."""
    akce = str(form.get("akce") or "")
    if akce == "pridat":
        role = str(form.get("role") or "")
        guest, old = accounts.access_set(user, str(form.get("email") or ""), role)
        log(user["id"], guest["id"], "role" if old else "pridat", role=role, stara=old or "")
        if old and old != role:
            LIVE.kick(user["id"], guest["id"])       # nová pravidla = nové spojení
        return {"ok": True}
    if akce == "role":
        role = str(form.get("role") or "")
        target = accounts.by_id(_int(form.get("host")))
        if not target or not accounts.access_role(user["id"], target["id"]):
            raise ValueError("Tenhle člověk do tvého prostoru přístup nemá.")
        _, old = accounts.access_set(user, target["email"], role)
        log(user["id"], target["id"], "role", role=role, stara=old)
        LIVE.kick(user["id"], target["id"])
        return {"ok": True}
    if akce == "odebrat":
        owner_id = user["id"]
        if form.get("vlastnik") not in (None, "") and _int(form.get("vlastnik")) != user["id"]:
            if user.get("role") != "admin":
                raise PermissionError("Cizí přístupy ruší jen admin.")
            owner_id = _int(form.get("vlastnik"))
        guest_id = _int(form.get("host"))
        if not accounts.access_remove(owner_id, guest_id):
            raise ValueError("Takový přístup tu není.")
        log(owner_id, guest_id, "odebrat",
            **({"admin": user["id"]} if owner_id != user["id"] else {}))
        LIVE.kick(owner_id, guest_id)
        return {"ok": True}
    raise ValueError("Neznámá akce.")


def forget_user(uid):
    """Smazaný nebo zablokovaný účet: odpojit jeho spojení (řádky v DB maže accounts)."""
    LIVE.kick(owner_id=uid)
    LIVE.kick(guest_id=uid)


def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ValueError("Neznámý člověk.") from None
