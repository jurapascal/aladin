"""
Přístup do cizího prostoru — vlastník pustí jiný účet z téže brány k sobě.

Vlastník v Nastavení → Účet → Přístup zadá e-mail existujícího účtu a roli,
nebo pustí **všechny na serveru** najednou (jedna role pro všechny aktivní
účty brány, i ty, které přibudou později). Host pak přepne v přepínači
prostoru v liště (vedle Osobní / firem) na „Prostory, kam máš přístup“. Kam
host právě míří, drží cookie `gw_prostor` (id vlastníka). Cookie je jen
*přání* — o tom, jestli smí, rozhoduje při **každém** požadavku řádek
v `space_access`, případně `space_access_all` (accounts.py). Odebrání tak
platí hned a otevřená spojení (websocket terminálu) brána sama zavře.

„Všichni na serveru“ (`space_access_all`, jeden řádek na vlastníka):

* platí jen pro aktivní účty — zablokovaný účet neprojde (accounts.py
  `access_detail` kontroluje obě strany při každém požadavku),
* řádek pro konkrétního člověka má **vždycky přednost**, i s nižší rolí:
  koho vlastník pustil jen ke Správě předplatného, ten přes „všechny“
  Pomocníkem ani Plným přístupem nebude,
* výjimka: role `zadny` (BLOCK) u konkrétního člověka = tenhle ne. Výjimky
  přežijí zrušení i nové zapnutí „všech“ a zablokování účtu,
* vlastník ho může kdykoli změnit nebo zrušit (admin zrušit), spojení hostů,
  kteří přišli přes „všechny“, se zavřou hned,
* záznam jde jako host 0 (`vsem-pridat`, `vsem-role`, `vsem-odebrat`),
  vstup přes „všechny“ má `pres: vsem`.

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
# Výjimka z „Všichni na serveru“: řádek pro člověka, který přístup nemá.
# Není to role — do ROLES nepatří a host s ní nikam neprojde.
BLOCK = "zadny"
BLOCK_LABEL = "Bez přístupu (výjimka)"
# Host v záznamu u změn „Všichni na serveru“.
VSEM_HOST = 0

# Co v prostoru nesmí žádný host: vnitřní volání brány a obnova tabů po
# restartu (čte se jednou — host by ji vlastníkovi „snědl“).
DENY_ALL_GUESTS = {"prostor-snapshot", "restore", "mcp-tool", "predplatne-host", "web-host"}

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
POMOCNIK_READ_ONLY = {"app-vzhled", "pluginy", "pocitac", "agents", "auto-model-keep", "gemini"}
# Akce v těle POSTu, které mažou.
POMOCNIK_DENY_ACTIONS = {"project": {"remove"}, "firma": {"discard"}}

_log_lock = threading.Lock()


def label(role):
    if role == BLOCK:
        return BLOCK_LABEL
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

    def kick(self, owner_id=None, guest_id=None, skip=()):
        """Zavře spojení; `skip` = hosté, kterých se to netýká (mají vlastní
        řádek, takže je změna „Všichni na serveru“ nemění)."""
        with self._lock:
            keys = [k for k in self._map
                    if (owner_id is None or k[0] == owner_id)
                    and (guest_id is None or k[1] == guest_id)
                    and k[1] not in skip]
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

    def guests(self, owner_id):
        """Hosté, kteří mají v prostoru vlastníka právě otevřené spojení."""
        with self._lock:
            return {k[1] for k, s in self._map.items() if k[0] == owner_id and s}


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


def _kam(accounts, user):
    """Prostory, kam `user` smí — pro přepínač v liště i pro Nastavení.
    `pres`: "osobne" (pozvaný jménem), nebo "vsem" (Všichni na serveru)."""
    return [{**_person(r, "owner"), "role": r["role"], "label": label(r["role"]),
             "pres": r["pres"]}
            for r in accounts.access_kam(user["id"])]


def kam_only(accounts, user, here=None):
    """Lehká odpověď pro přepínač prostoru v hubu (bez záznamu a správy)."""
    return {"kam": _kam(accounts, user), "tady": here}


def overview(accounts, user, here=None):
    """Co stránka potřebuje: komu jsem dal přístup, kam smím, kde teď jsem."""
    uid = user["id"]
    rows = accounts.access_list(owner_id=uid, blocks=True)
    mine = [{**_person(r, "guest"), "role": r["role"], "label": label(r["role"]),
             "vyjimka": r["role"] == BLOCK, "created": r["created"],
             "online": LIVE.count(uid, r["guest_id"]) > 0}
            for r in rows]
    names = {r["id"]: (r["name"] or r["email"]) for r in accounts.list()}
    explicit = {r["guest_id"] for r in rows}
    vsem = None
    for r in accounts.access_all_list(owner_id=uid):
        # Kdo je teď v prostoru přes „všechny“ (bez vlastního řádku).
        tady = sorted(names.get(g, "?") for g in LIVE.guests(uid) if g not in explicit)
        vsem = {"role": r["role"], "label": label(r["role"]), "created": r["created"],
                "online": tady}
    zaznam = [{**e, "host_jmeno": ("všichni na serveru" if e.get("host") == VSEM_HOST
                                   else names.get(e.get("host"), "?")),
               "vlastnik_jmeno": names.get(e.get("vlastnik"), "?")}
              for e in read_log(uid, 100)]
    # Komu jde přístup přidat: aktivní účty na bráně (jméno a e-mail) — do výběru
    # v Nastavení → Přístup, aby se e-mail nemusel opisovat.
    lide = sorted(({"id": r["id"], "name": r["name"] or "", "email": r["email"]}
                   for r in accounts.list() if r["id"] != uid and not r["disabled"]),
                  key=lambda x: (x["name"] or x["email"]).lower())
    out = {"moje": mine, "lide": lide, "kam": _kam(accounts, user), "vsem": vsem, "zaznam": zaznam,
           "role": [{"id": r, "label": ROLE_LABELS[r]} for r in ROLES],
           "vyjimka": {"id": BLOCK, "label": BLOCK_LABEL},
           "tady": here}
    if user.get("role") == "admin":
        out["vse"] = [{"vlastnik": _person(r, "owner"), "host": _person(r, "guest"),
                       "role": r["role"], "label": label(r["role"])}
                      for r in accounts.access_list()]
        out["vse_vsem"] = [{"vlastnik": _person(r, "owner"), "role": r["role"],
                            "label": label(r["role"])}
                           for r in accounts.access_all_list()]
    return out


def _owner_for(user, form):
    """Čí přístup se ruší: vlastní, nebo (jen admin) cizí."""
    if form.get("vlastnik") not in (None, "") and _int(form.get("vlastnik")) != user["id"]:
        if user.get("role") != "admin":
            raise PermissionError("Cizí přístupy ruší jen admin.")
        return _int(form.get("vlastnik"))
    return user["id"]


def _kick_vsem(accounts, owner_id):
    """Změna „Všichni na serveru“: zavřít spojení hostů, kteří přišli přes ni
    (kdo má vlastní řádek, toho se netýká)."""
    explicit = {r["guest_id"] for r in accounts.access_list(owner_id=owner_id, blocks=True)}
    return LIVE.kick(owner_id, skip=explicit)


def act(accounts, user, form):
    """Jedna změna. Vlastník = vždycky přihlášený `user` (admin smí jen rušit)."""
    akce = str(form.get("akce") or "")
    if akce == "pridat":
        role = str(form.get("role") or "")
        email = str(form.get("email") or "")
        target = accounts.get(email)
        before = accounts.access_role(user["id"], target["id"]) if target else None
        guest, old = accounts.access_set(user, email, role)
        if role == BLOCK:
            log(user["id"], guest["id"], "vyjimka", stara=old or "")
        else:
            log(user["id"], guest["id"], "role" if old else "pridat", role=role, stara=old or "")
        # Nová pravidla = nové spojení (i když dosud chodil přes „všechny“).
        if before and before != accounts.access_role(user["id"], guest["id"]):
            LIVE.kick(user["id"], guest["id"])
        return {"ok": True}
    if akce == "role":
        role = str(form.get("role") or "")
        target = accounts.by_id(_int(form.get("host")))
        # Jen u lidí s vlastním řádkem — kdo chodí přes „všechny“, toho se
        # přidává jménem (`pridat`), ať je to v záznamu vidět.
        if not target or accounts.access_row(user["id"], target["id"]) is None:
            raise ValueError("Tenhle člověk do tvého prostoru přístup nemá.")
        _, old = accounts.access_set(user, target["email"], role)
        log(user["id"], target["id"], "vyjimka" if role == BLOCK else "role",
            role=role, stara=old)
        LIVE.kick(user["id"], target["id"])
        return {"ok": True}
    if akce == "odebrat":
        owner_id = _owner_for(user, form)
        guest_id = _int(form.get("host"))
        old = accounts.access_row(owner_id, guest_id)
        if old is None or not accounts.access_remove(owner_id, guest_id):
            raise ValueError("Takový přístup tu není.")
        log(owner_id, guest_id, "vyjimka-zrusit" if old == BLOCK else "odebrat",
            **({"admin": user["id"]} if owner_id != user["id"] else {}))
        LIVE.kick(owner_id, guest_id)
        # Bez vlastního řádku platí „Všichni na serveru“ — ať to vlastník ví.
        zbyva = accounts.access_role(owner_id, guest_id)
        return {"ok": True, "zbyva": label(zbyva) if zbyva else ""}
    if akce == "vsem":
        role = str(form.get("role") or "")
        old = accounts.access_all_set(user, role)
        log(user["id"], VSEM_HOST, "vsem-role" if old else "vsem-pridat", role=role, stara=old)
        if old and old != role:
            _kick_vsem(accounts, user["id"])
        return {"ok": True}
    if akce == "vsem-zrusit":
        owner_id = _owner_for(user, form)
        old = accounts.access_all_remove(owner_id)
        if not old:
            raise ValueError("Přístup pro všechny tu není.")
        log(owner_id, VSEM_HOST, "vsem-odebrat", stara=old,
            **({"admin": user["id"]} if owner_id != user["id"] else {}))
        _kick_vsem(accounts, owner_id)
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
