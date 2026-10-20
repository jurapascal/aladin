"""
Účty na bráně — kdo se smí připojit a čím se prokáže.

Nic z toho nepotřebuje knihovnu navíc: sqlite3 i scrypt jsou ve standardní
knihovně Pythonu, takže brána zůstává tím, čím je zbytek repa — spustitelná
na holém Pythonu.

Tři věci, které stojí za vysvětlení:

* **Heslo se neukládá, ukládá se scrypt.** Kdyby někdo databázi odnesl, nemá
  z ní hesla. Parametry (N=2^15) jsou zvolené tak, aby ověření trvalo kolem
  desetiny vteřiny — dost na to, aby hádání po síti nemělo smysl, a málo na
  to, aby to při přihlášení bylo znát.

* **Token se taky ukládá jen jako otisk.** Token je heslo pro telefon: kdo ho
  má, je přihlášený. V databázi proto leží jen jeho SHA-256, stejně jako
  u hesla — z ukradené databáze se přihlásit nedá.

* **Přístup k DB je serializovaný RLockem.** Jedno sdílené sqlite připojení
  volané z víc vláken naráz (prohlížeč pálí požadavky paralelně) padá na
  „Recursive use of cursors not allowed“ a brána pak vrací 502/401. Zámek to
  převede na frontu; dotazy jsou tak krátké, že to není znát.

Účet má navíc **roli** a **jméno vaultu**:

* `role` je `admin`, nebo `user`. Admin spravuje účty z CLI; role nerozhoduje
  o izolaci session — tu drží `isolation.py` bez ohledu na roli.
* `vault` je jméno Obsidian paměti daného uživatele.

**Neúspěšná přihlášení** se zapisují sem (`login_fails`), ne do paměti brány:
zámek po hádání hesla tak přežije restart brány (noční aktualizace) a správce
ho zruší z CLI (`claude-hub-admin zamky odemknout`).
"""
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import threading
import time

from . import totp

# N=2^15, r=8 → scrypt si řekne o 128*N*r = přesně 32 MiB, což je zároveň
# výchozí strop OpenSSL. Bez `maxmem` to spadne na „memory limit exceeded“,
# tak se strop říká explicitně a s rezervou.
SCRYPT = dict(n=2 ** 15, r=8, p=1, dklen=32, maxmem=128 * 1024 * 1024)
TOKEN_BYTES = 32
ROLES = ("user", "admin")
# Čím se v daném prostoru ověřuje Claude: `central` = klíč API brány (platí se
# podle spotřeby), `own` = uživatel má v svém domově vlastní přihlášení.
AUTHS = ("central", "own")
SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id       INTEGER PRIMARY KEY,
    email    TEXT NOT NULL UNIQUE COLLATE NOCASE,
    name     TEXT NOT NULL DEFAULT '',
    role     TEXT NOT NULL DEFAULT 'user',
    vault    TEXT NOT NULL DEFAULT '',
    claude_auth TEXT NOT NULL DEFAULT 'central',
    salt     BLOB NOT NULL,
    hash     BLOB NOT NULL,
    created  REAL NOT NULL,
    disabled INTEGER NOT NULL DEFAULT 0
);
-- Passkey (WebAuthn) jako druhý krok přihlášení (gateway/webauthn.py).
CREATE TABLE IF NOT EXISTS passkeys (
    id         TEXT PRIMARY KEY,           -- credential id, base64url
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    public_key TEXT NOT NULL,              -- COSE klíč, base64url
    sign_count INTEGER NOT NULL DEFAULT 0,
    name       TEXT NOT NULL DEFAULT '',
    created    REAL NOT NULL,
    used       REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS login_fails (
    key     TEXT PRIMARY KEY,           -- ip:…|email:…, ip:…, email:…
    hits    TEXT NOT NULL DEFAULT '[]', -- časy neúspěchů, které se ještě počítají
    locked  REAL NOT NULL DEFAULT 0,    -- do kdy je zamčeno
    updated REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS tokens (
    fingerprint BLOB PRIMARY KEY,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    label       TEXT NOT NULL DEFAULT '',
    created     REAL NOT NULL,
    seen        REAL NOT NULL
);
-- Kdo smí do firemního Obsidianu: none / read / write. Bez řádku platí
-- COMPANY_DEFAULT; admin má vždycky write. Mění admini ve firemním Obsidianu.
CREATE TABLE IF NOT EXISTS company_access (
    user_id  INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    level    TEXT NOT NULL,
    by_email TEXT NOT NULL DEFAULT '',
    updated  REAL NOT NULL
);
-- Kdo smí do čího prostoru a s jakou rolí (gateway/pristup.py). Řádek zakládá
-- jen vlastník prostoru; smazáním účtu (kterékoli strany) zmizí sám.
CREATE TABLE IF NOT EXISTS space_access (
    owner_id   INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    guest_id   INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role       TEXT NOT NULL,
    created    REAL NOT NULL,
    created_by INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (owner_id, guest_id)
);
-- Přístup do prostoru pro všechny účty na bráně najednou („Všichni na
-- serveru“, gateway/pristup.py). Jeden řádek na vlastníka. Řádek
-- v space_access pro konkrétního člověka má vždycky přednost — i s nižší rolí
-- nebo s rolí `zadny` (výjimka: tenhle člověk ne).
CREATE TABLE IF NOT EXISTS space_access_all (
    owner_id   INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    role       TEXT NOT NULL,
    created    REAL NOT NULL,
    created_by INTEGER NOT NULL DEFAULT 0
);
-- Napojení z appky Claude (MCP, gateway/mcp.py). Klient se registruje sám
-- (RFC 7591), ale jen s adresou pro návrat, kterou brána zná (oauth.py).
CREATE TABLE IF NOT EXISTS oauth_clients (
    client_id     TEXT PRIMARY KEY,
    name          TEXT NOT NULL DEFAULT '',
    redirect_uris TEXT NOT NULL,            -- JSON seznam
    created       REAL NOT NULL
);
-- Jednorázový kód z přihlášení; platí minutu, svázaný s klientem, adresou
-- pro návrat a PKCE. V databázi jen otisk.
CREATE TABLE IF NOT EXISTS oauth_codes (
    fingerprint  BLOB PRIMARY KEY,
    user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    client_id    TEXT NOT NULL,
    redirect_uri TEXT NOT NULL,
    challenge    TEXT NOT NULL,
    resource     TEXT NOT NULL DEFAULT '',
    expires      REAL NOT NULL
);
-- Přístupové (hodina) a obnovovací (měsíc, při použití se vymění) tokeny.
-- `family` spojuje jedno přihlášení: když přijde už vyměněný obnovovací token,
-- někdo ho ukradl a zruší se celé přihlášení (RFC 9700, 4.14.2).
CREATE TABLE IF NOT EXISTS oauth_tokens (
    fingerprint BLOB PRIMARY KEY,
    kind        TEXT NOT NULL,              -- access / refresh
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    client_id   TEXT NOT NULL,
    family      TEXT NOT NULL,
    resource    TEXT NOT NULL DEFAULT '',
    created     REAL NOT NULL,
    expires     REAL NOT NULL,
    used        INTEGER NOT NULL DEFAULT 0, -- obnovovací už vyměněný
    seen        REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS oauth_tokens_user ON oauth_tokens(user_id);
CREATE INDEX IF NOT EXISTS oauth_tokens_family ON oauth_tokens(family);
"""
COMPANY_LEVELS = ("none", "read", "write")
# Bez záznamu: tak, jak to bylo před správou přístupů — každý čte a nahrává
# přes potvrzovací kartu. Admini to pak lidem mění ve firemním Obsidianu.
COMPANY_DEFAULT = os.environ.get("HUB_GW_COMPANY_DEFAULT", "write")
ACCESS_TTL = 60 * 60
REFRESH_TTL = 30 * 24 * 60 * 60
CODE_TTL = 60


def _hash(password, salt):
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, **SCRYPT)


def _fingerprint(token):
    return hashlib.sha256(token.encode("utf-8")).digest()


def _clean_role(role):
    role = (role or "user").strip().lower()
    return role if role in ROLES else "user"


def _clean_auth(auth):
    auth = (auth or "central").strip().lower()
    return auth if auth in AUTHS else "central"


def _times(raw):
    """Časy neúspěchů uložené jako JSON seznam čísel."""
    try:
        data = json.loads(raw or "[]")
    except ValueError:
        return []
    return [t for t in data if isinstance(t, (int, float))] if isinstance(data, list) else []


def _codes(raw):
    """Otisky záložních kódů uložené jako JSON seznam."""
    try:
        data = json.loads(raw or "[]")
    except ValueError:
        return []
    return [c for c in data if isinstance(c, str)] if isinstance(data, list) else []


class Accounts:
    """Účty a přihlašovací tokeny. Bezpečné volat z více vláken (RLock)."""

    def __init__(self, path, require_mfa=False):
        # Brána s povinným dvoufázovým ověřením: platí jen tokeny vydané po
        # druhém kroku (tokens.mfa). Správa z CLI tokeny nepoužívá.
        self.require_mfa = require_mfa
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        # check_same_thread=False: brána obsluhuje každé spojení ve vlastním
        # vlákně. Jedno sdílené připojení ale NENÍ bezpečné volat z víc vláken
        # naráz — Python sqlite3 na souběžných execute() padá („Recursive use
        # of cursors not allowed“) a prohlížeč přitom pálí assety paralelně.
        # Proto všechny veřejné metody serializuje RLock (re-entrantní, aby
        # login() mohl zavolat verify() bez zaseknutí).
        self._lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False,
                                  isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript(SCHEMA)
        self._migrate()
        # Otisky hesel a tajemství pro dvoufázové ověření — číst je nemá kdo
        # jiný než brána. sqlite zakládá soubor s 644 a WAL i SHM dědí totéž,
        # takže se to musí srovnat ručně, po journal_mode=WAL (dřív ty soubory
        # ještě nejsou). Nadřazená složka je 750, tohle je druhá vrstva.
        for suffix in ("", "-wal", "-shm"):
            try:
                os.chmod(path + suffix, 0o600)
            except OSError:
                pass

    def _migrate(self):
        """Doplní sloupce, které starší databáze ještě nemá."""
        have = {r["name"] for r in self.db.execute("PRAGMA table_info(users)")}
        if "role" not in have:
            self.db.execute("ALTER TABLE users ADD COLUMN role TEXT NOT NULL"
                            " DEFAULT 'user'")
        if "vault" not in have:
            self.db.execute("ALTER TABLE users ADD COLUMN vault TEXT NOT NULL"
                            " DEFAULT ''")
        if "claude_auth" not in have:
            self.db.execute("ALTER TABLE users ADD COLUMN claude_auth TEXT NOT"
                            " NULL DEFAULT 'central'")
        # Dvoufázové ověření (2.7.0): tajemství aplikace, poslední použité
        # okno (proti opakovanému použití kódu) a otisky záložních kódů.
        if "totp_secret" not in have:
            self.db.execute("ALTER TABLE users ADD COLUMN totp_secret TEXT NOT"
                            " NULL DEFAULT ''")
        if "totp_last" not in have:
            self.db.execute("ALTER TABLE users ADD COLUMN totp_last INTEGER NOT"
                            " NULL DEFAULT 0")
        if "recovery" not in have:
            self.db.execute("ALTER TABLE users ADD COLUMN recovery TEXT NOT"
                            " NULL DEFAULT ''")
        tokens = {r["name"] for r in self.db.execute("PRAGMA table_info(tokens)")}
        if "mfa" not in tokens:
            # Tokeny z doby před 2FA mají 0 — s povinným ověřením neplatí.
            self.db.execute("ALTER TABLE tokens ADD COLUMN mfa INTEGER NOT"
                            " NULL DEFAULT 0")

    # ── uživatelé ────────────────────────────────────────────────────────────
    def add(self, email, password, name="", role="user", vault="",
            claude_auth="central"):
        """Založí účet. Vrací id, nebo vyhodí ValueError."""
        email = (email or "").strip().lower()
        if "@" not in email:
            raise ValueError("E-mail nevypadá jako e-mail.")
        if len(password or "") < 10:
            raise ValueError("Heslo musí mít aspoň 10 znaků.")
        salt = secrets.token_bytes(16)
        with self._lock:
            try:
                cur = self.db.execute(
                    "INSERT INTO users (email, name, role, vault, claude_auth,"
                    " salt, hash, created) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (email, name or "", _clean_role(role), (vault or "").strip(),
                     _clean_auth(claude_auth), salt, _hash(password, salt),
                     time.time()))
            except sqlite3.IntegrityError:
                raise ValueError(f"{email} už účet má.") from None
            return cur.lastrowid

    def set_auth(self, email, claude_auth):
        """Přepne, čím se v prostoru ověřuje Claude: central / own."""
        with self._lock:
            cur = self.db.execute(
                "UPDATE users SET claude_auth = ? WHERE email = ?",
                (_clean_auth(claude_auth), (email or "").strip().lower()))
            if not cur.rowcount:
                raise ValueError(f"{email} tu žádný účet nemá.")

    def set_password(self, email, password):
        if len(password or "") < 10:
            raise ValueError("Heslo musí mít aspoň 10 znaků.")
        salt = secrets.token_bytes(16)
        email = (email or "").strip().lower()
        with self._lock:
            cur = self.db.execute(
                "UPDATE users SET salt = ?, hash = ? WHERE email = ?",
                (salt, _hash(password, salt), email))
            if not cur.rowcount:
                raise ValueError(f"{email} tu žádný účet nemá.")
            # Změna hesla odhlašuje: jinak by ukradený token přežil i to, kvůli
            # čemu se heslo mění. Napojení z appky Claude taky.
            self._drop_logins(email)

    def _drop_logins(self, email):
        """Odhlásí účet všude: zařízení i napojení z appky Claude (MCP)."""
        for table in ("tokens", "oauth_tokens", "oauth_codes"):
            self.db.execute(
                f"DELETE FROM {table} WHERE user_id ="
                " (SELECT id FROM users WHERE email = ?)", (email,))

    def set_role(self, email, role):
        """Přepne roli (admin/user). Poslednímu adminovi ji vzít nedovolí."""
        role = _clean_role(role)
        email = (email or "").strip().lower()
        with self._lock:
            row = self.db.execute("SELECT id, role FROM users WHERE email = ?",
                                  (email,)).fetchone()
            if not row:
                raise ValueError(f"{email} tu žádný účet nemá.")
            if (row["role"] == "admin" and role != "admin"
                    and self._admin_count() <= 1):
                raise ValueError("Tohle je poslední admin — nejdřív udělej"
                                 " adminem někoho jiného.")
            self.db.execute("UPDATE users SET role = ? WHERE email = ?",
                            (role, email))

    def set_name(self, email, name):
        """Jméno, jak ho hub ukazuje (pozdrav, členové, přístupy)."""
        name = " ".join(str(name or "").split())
        if len(name) > 80:
            raise ValueError("Jméno je moc dlouhé.")
        with self._lock:
            cur = self.db.execute("UPDATE users SET name = ? WHERE email = ?",
                                  (name, (email or "").strip().lower()))
            if not cur.rowcount:
                raise ValueError(f"{email} tu žádný účet nemá.")

    def set_vault(self, email, vault):
        with self._lock:
            cur = self.db.execute("UPDATE users SET vault = ? WHERE email = ?",
                                  ((vault or "").strip(),
                                   (email or "").strip().lower()))
            if not cur.rowcount:
                raise ValueError(f"{email} tu žádný účet nemá.")

    def set_disabled(self, email, disabled=True):
        email = (email or "").strip().lower()
        with self._lock:
            cur = self.db.execute(
                "UPDATE users SET disabled = ? WHERE email = ?",
                (1 if disabled else 0, email))
            if not cur.rowcount:
                raise ValueError(f"{email} tu žádný účet nemá.")
            if disabled:
                self._drop_logins(email)
                # Zablokovaný účet nesmí do cizích prostorů a nikdo do jeho.
                # Výjimky z „Všichni na serveru“ (role `zadny`) zůstávají —
                # po odblokování by jinak člověk, kterého vlastník vyřadil,
                # dostal přístup znovu.
                self.db.execute(
                    "DELETE FROM space_access WHERE owner_id IN (SELECT id FROM"
                    " users WHERE email = ?) OR (guest_id IN (SELECT id FROM users"
                    " WHERE email = ?) AND role != 'zadny')", (email, email))
                self.db.execute(
                    "DELETE FROM space_access_all WHERE owner_id IN (SELECT id"
                    " FROM users WHERE email = ?)", (email,))

    def remove(self, email):
        email = (email or "").strip().lower()
        with self._lock:
            # FK s ON DELETE CASCADE to udělá taky — tohle platí i pro starou
            # databázi, kde se foreign_keys nezapnuly.
            self.db.execute(
                "DELETE FROM space_access WHERE owner_id IN (SELECT id FROM users"
                " WHERE email = ?) OR guest_id IN (SELECT id FROM users WHERE"
                " email = ?)", (email, email))
            self.db.execute(
                "DELETE FROM space_access_all WHERE owner_id IN (SELECT id FROM"
                " users WHERE email = ?)", (email,))
            cur = self.db.execute("DELETE FROM users WHERE email = ?", (email,))
            if not cur.rowcount:
                raise ValueError(f"{email} tu žádný účet nemá.")

    def get(self, email):
        """Účet podle e-mailu, nebo None (bez hesla)."""
        with self._lock:
            row = self.db.execute(
                "SELECT id, email, name, role, vault, claude_auth, created,"
                " disabled FROM users WHERE email = ?",
                ((email or "").strip().lower(),)).fetchone()
        return dict(row) if row else None

    def by_id(self, user_id):
        """Účet podle id, a jen pokud není zablokovaný — vydává se na něj
        přihlášení, takže na `disabled` se tu musí koukat stejně jako u hesla."""
        if not user_id:
            return None
        with self._lock:
            row = self.db.execute(
                "SELECT * FROM users WHERE id = ? AND disabled = 0",
                (user_id,)).fetchone()
        return self._public(row) if row else None

    def list(self):
        with self._lock:
            return [dict(r) for r in self.db.execute(
                "SELECT id, email, name, role, vault, claude_auth, created,"
                " disabled, totp_secret != '' AS twofa FROM users ORDER BY email")]

    def _admin_count(self):
        return self.db.execute(
            "SELECT COUNT(*) FROM users WHERE role = 'admin' AND disabled = 0"
        ).fetchone()[0]

    def count(self):
        with self._lock:
            return self.db.execute("SELECT COUNT(*) FROM users").fetchone()[0]

    # ── přihlášení ───────────────────────────────────────────────────────────
    def verify(self, email, password):
        """Uživatel při správném heslu, jinak None. Trvá stejně tak či tak."""
        with self._lock:
            row = self.db.execute(
                "SELECT * FROM users WHERE email = ?",
                ((email or "").strip().lower(),)).fetchone()
        # I na neexistující e-mail se počítá scrypt, aby se z délky odpovědi
        # nedalo poznat, které účty existují. (Mimo zámek — scrypt je pomalý.)
        salt = row["salt"] if row else b"\0" * 16
        want = row["hash"] if row else b"\0" * SCRYPT["dklen"]
        ok = hmac.compare_digest(_hash(password or "", salt), want)
        if not row or not ok or row["disabled"]:
            return None
        return self._public(row)

    # ── neúspěšná přihlášení ─────────────────────────────────────────────────
    # `keys` = [(klíč, limit)] — pravidla skládá brána (server._fail_keys).
    def fail_wait(self, keys):
        """Kolik sekund ještě drží zámek na některém z klíčů; 0 = volno."""
        names = [key for key, _limit in keys]
        if not names:
            return 0
        now = time.time()
        with self._lock:
            rows = self.db.execute(
                "SELECT locked FROM login_fails WHERE key IN (%s)" % ",".join("?" * len(names)),
                names).fetchall()
        return max([row["locked"] - now for row in rows] + [0])

    def fail_record(self, keys, window, lock):
        """Zapíše neúspěch. Kde se tím dosáhne limitu za `window` sekund, zamkne
        na `lock` sekund. Vrací (sekund do odemčení, kolik pokusů zbývá,
        klíče zamčené právě teď)."""
        now = time.time()
        wait, left, locked_now = 0, None, []
        with self._lock:
            for key, limit in keys:
                row = self.db.execute("SELECT hits, locked FROM login_fails WHERE key = ?",
                                      (key,)).fetchone()
                hits = [t for t in _times(row["hits"] if row else "") if now - t < window]
                locked = row["locked"] if row and row["locked"] > now else 0
                hits.append(now)
                if not locked and len(hits) >= limit:
                    # Po odemčení se začíná znovu od nuly, ne od starých pokusů.
                    locked, hits = now + lock, []
                    locked_now.append(key)
                self.db.execute(
                    "INSERT OR REPLACE INTO login_fails (key, hits, locked, updated)"
                    " VALUES (?, ?, ?, ?)", (key, json.dumps(hits), locked, now))
                wait = max(wait, locked - now) if locked else wait
                remaining = 0 if locked else max(0, limit - len(hits))
                left = remaining if left is None else min(left, remaining)
            self.db.execute("DELETE FROM login_fails WHERE locked < ? AND updated < ?",
                            (now, now - window))
        return wait, (left or 0), locked_now

    def fail_clear(self, key):
        """Povedené přihlášení: pokusy na tenhle klíč se zapomenou."""
        with self._lock:
            self.db.execute("DELETE FROM login_fails WHERE key = ?", (key,))

    def fail_list(self):
        """Zámky a počítané neúspěchy pro správu: [{"key", "hits", "locked"}]."""
        now = time.time()
        with self._lock:
            rows = self.db.execute("SELECT key, hits, locked FROM login_fails"
                                   " ORDER BY locked DESC, updated DESC").fetchall()
        return [{"key": r["key"], "hits": len(_times(r["hits"])),
                 "locked": r["locked"] if r["locked"] > now else 0} for r in rows]

    def fail_unlock(self, target):
        """Zruší zámky a pokusy pro e-mail nebo adresu. Vrací, kolik záznamů smazal."""
        target = (target or "").strip().lower()
        if not target:
            raise ValueError("Zadej e-mail nebo IP adresu.")
        mark = ("email:" if "@" in target else "ip:") + target
        with self._lock:
            keys = [r["key"] for r in self.db.execute("SELECT key FROM login_fails")
                    if mark in r["key"].split("|")]
            for key in keys:
                self.db.execute("DELETE FROM login_fails WHERE key = ?", (key,))
        return len(keys)

    @staticmethod
    def _public(row):
        """Co o uživateli smí ven — nikdy salt ani hash."""
        return {"id": row["id"], "email": row["email"], "name": row["name"],
                "role": row["role"], "vault": row["vault"],
                "claude_auth": row["claude_auth"]}

    def issue_token(self, user, label="", mfa=False):
        """Vydá token pro zařízení už prokázanému uživateli.

        Oddělené od `login()` schválně: heslo není jediný způsob, jak se dá
        prokázat totožnost. Předání přihlášení z hubu na počítači i pozdější
        přihlášení přes cizího poskytovatele potřebují tenhle krok bez hesla.
        `mfa` = uživatel prošel i druhým krokem (kód z aplikace).
        """
        token = secrets.token_urlsafe(TOKEN_BYTES)
        now = time.time()
        with self._lock:
            self.db.execute(
                "INSERT INTO tokens (fingerprint, user_id, label, created, seen, mfa)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (_fingerprint(token), user["id"], label or "", now, now,
                 1 if mfa else 0))
        return token

    def login(self, email, password, label=""):
        """Ověří heslo a vydá token pro zařízení. None = nepustit dál.
        Jen heslem — s povinným dvoufázovým ověřením takový token neplatí."""
        user = self.verify(email, password)
        if not user:
            return None
        return self.issue_token(user, label)

    # ── dvoufázové ověření ───────────────────────────────────────────────────
    def twofa(self, user_id):
        """{"enabled", "recovery_left"} — pro přihlášení, nastavení i správu."""
        with self._lock:
            row = self.db.execute("SELECT totp_secret, recovery FROM users WHERE id = ?",
                                  (user_id,)).fetchone()
            keys = self.db.execute("SELECT COUNT(*) FROM passkeys WHERE user_id = ?",
                                   (user_id,)).fetchone()[0] if row else 0
        if not row:
            return {"enabled": False, "recovery_left": 0, "passkeys": 0}
        return {"enabled": bool(row["totp_secret"]),
                "recovery_left": len(_codes(row["recovery"])),
                "passkeys": keys}

    # ── passkey ──────────────────────────────────────────────────────────────
    def passkeys(self, user_id):
        """Klíče účtu — bez veřejných klíčů (ty UI nepotřebuje)."""
        with self._lock:
            return [dict(r) for r in self.db.execute(
                "SELECT id, name, created, used FROM passkeys WHERE user_id = ? ORDER BY created",
                (user_id,))]

    def passkey(self, cred_id):
        with self._lock:
            row = self.db.execute("SELECT * FROM passkeys WHERE id = ?", (cred_id,)).fetchone()
        return dict(row) if row else None

    def passkey_add(self, user_id, cred, name):
        with self._lock:
            self.db.execute(
                "INSERT INTO passkeys (id, user_id, public_key, sign_count, name, created)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (cred["id"], user_id, cred["public_key"], int(cred["sign_count"]),
                 (name or "Passkey")[:60], time.time()))

    def passkey_used(self, cred_id, count):
        with self._lock:
            self.db.execute("UPDATE passkeys SET sign_count = ?, used = ? WHERE id = ?",
                            (int(count), time.time(), cred_id))

    def passkey_delete(self, user_id, cred_id):
        with self._lock:
            return self.db.execute("DELETE FROM passkeys WHERE id = ? AND user_id = ?",
                                   (cred_id, user_id)).rowcount > 0

    def _store_recovery(self, user_id):
        codes = totp.recovery_codes()
        self.db.execute("UPDATE users SET recovery = ? WHERE id = ?",
                        (json.dumps([totp.recovery_hash(c) for c in codes]), user_id))
        return codes

    def enable_totp(self, user_id, secret):
        """Zapne ověřování s tajemstvím, které už člověk potvrdil kódem.
        Vrací záložní kódy — jediné místo, kde jsou k vidění v čitelné podobě."""
        with self._lock:
            self.db.execute("UPDATE users SET totp_secret = ?, totp_last = 0 WHERE id = ?",
                            (secret, user_id))
            return self._store_recovery(user_id)

    def new_recovery(self, user_id):
        """Nové záložní kódy; staré tím přestanou platit."""
        with self._lock:
            return self._store_recovery(user_id)

    def second_factor(self, user_id, code, allow_recovery=True):
        """Ověří kód z aplikace, nebo záložní kód. Vrací "totp", "recovery", nebo "".
        Použitý kód z aplikace ani záložní kód podruhé neprojde."""
        with self._lock:
            row = self.db.execute(
                "SELECT totp_secret, totp_last, recovery, disabled FROM users WHERE id = ?",
                (user_id,)).fetchone()
            if not row or row["disabled"] or not row["totp_secret"]:
                return ""
            counter = totp.verify(row["totp_secret"], code, row["totp_last"])
            if counter:
                self.db.execute("UPDATE users SET totp_last = ? WHERE id = ?",
                                (counter, user_id))
                return "totp"
            if allow_recovery and totp.looks_like_recovery(code):
                codes = _codes(row["recovery"])
                digest = totp.recovery_hash(code)
                if digest in codes:
                    codes.remove(digest)
                    self.db.execute("UPDATE users SET recovery = ? WHERE id = ?",
                                    (json.dumps(codes), user_id))
                    return "recovery"
        return ""

    def reset_totp(self, email):
        """Zruší ověřování (ztracený telefon i záložní kódy) a odhlásí všechna
        zařízení. Při příštím přihlášení si ho člověk nastaví znovu."""
        email = (email or "").strip().lower()
        with self._lock:
            cur = self.db.execute(
                "UPDATE users SET totp_secret = '', totp_last = 0, recovery = ''"
                " WHERE email = ?", (email,))
            if not cur.rowcount:
                raise ValueError(f"{email} tu žádný účet nemá.")
            # Ztracený telefon bývá i ztracený passkey — pryč s ověřováním celým.
            self.db.execute("DELETE FROM passkeys WHERE user_id = (SELECT id FROM users"
                            " WHERE email = ?)", (email,))
            self._drop_logins(email)

    def user_for_token(self, token):
        """Komu token patří, nebo None. Zaznamená, že se ozval."""
        if not token:
            return None
        fp = _fingerprint(token)
        with self._lock:
            row = self.db.execute(
                "SELECT u.*, t.mfa AS token_mfa FROM tokens t"
                " JOIN users u ON u.id = t.user_id WHERE t.fingerprint = ?",
                (fp,)).fetchone()
            if not row or row["disabled"]:
                return None
            # Token vydaný jen na heslo (třeba z doby před 2FA) s povinným
            # ověřením neplatí — člověk se přihlásí znovu i s kódem.
            if self.require_mfa and not row["token_mfa"]:
                return None
            self.db.execute("UPDATE tokens SET seen = ? WHERE fingerprint = ?",
                            (time.time(), fp))
            return self._public(row)

    def revoke(self, token):
        with self._lock:
            self.db.execute("DELETE FROM tokens WHERE fingerprint = ?",
                            (_fingerprint(token),))

    def devices(self, email):
        with self._lock:
            return [dict(r) for r in self.db.execute(
                "SELECT t.label, t.created, t.seen FROM tokens t JOIN users u"
                " ON u.id = t.user_id WHERE u.email = ? ORDER BY t.seen DESC",
                ((email or "").strip().lower(),))]

    # ── firemní Obsidian: kdo smí číst a zapisovat ───────────────────────────
    def company_level(self, user):
        """none / read / write pro účet. Admin má vždycky write."""
        if not user:
            return "none"
        if user.get("role") == "admin":
            return "write"
        with self._lock:
            row = self.db.execute("SELECT level FROM company_access WHERE user_id = ?",
                                  (user["id"],)).fetchone()
        level = row["level"] if row else COMPANY_DEFAULT
        return level if level in COMPANY_LEVELS else "none"

    def company_list(self):
        """Všechny aktivní účty s úrovní přístupu — pro správu ve firemním Obsidianu."""
        with self._lock:
            rows = self.db.execute(
                "SELECT u.id, u.email, u.name, u.role, c.level, c.by_email, c.updated"
                " FROM users u LEFT JOIN company_access c ON c.user_id = u.id"
                " WHERE u.disabled = 0 ORDER BY u.name COLLATE NOCASE, u.email").fetchall()
        out = []
        for r in rows:
            level = "write" if r["role"] == "admin" else (r["level"] or COMPANY_DEFAULT)
            out.append({"id": r["id"], "email": r["email"], "name": r["name"],
                        "role": r["role"], "level": level if level in COMPANY_LEVELS else "none",
                        "by": r["by_email"] or "", "updated": r["updated"] or 0})
        return out

    def set_company_level(self, admin, user_id, level):
        """Admin nastaví úroveň jinému účtu. Vrací (účet, stará úroveň)."""
        if not admin or admin.get("role") != "admin":
            raise PermissionError("Přístupy k firemnímu Obsidianu mění jen admin.")
        if level not in COMPANY_LEVELS:
            raise ValueError("Neznámá úroveň přístupu.")
        target = self.by_id(int(user_id or 0))
        if not target:
            raise ValueError("Takový účet tu není.")
        if target["role"] == "admin":
            raise ValueError("Admin má do firemního Obsidianu přístup vždycky.")
        old = self.company_level(target)
        with self._lock:
            self.db.execute(
                "INSERT OR REPLACE INTO company_access (user_id, level, by_email, updated)"
                " VALUES (?, ?, ?, ?)", (target["id"], level, admin["email"], time.time()))
        return target, old

    # ── přístup do cizího prostoru (gateway/pristup.py) ──────────────────────
    def access_set(self, owner, guest_email, role):
        """Vlastník `owner` pustí účet `guest_email` do svého prostoru s rolí
        (nebo ho rolí `zadny` vyřadí z přístupu pro všechny).
        Vrací (host, stará role nebo "")."""
        from .pristup import BLOCK, ROLES as SPACE_ROLES
        if role not in SPACE_ROLES and role != BLOCK:
            raise ValueError("Neznámá role.")
        if not owner or not owner.get("id"):
            raise PermissionError("Chybí vlastník prostoru.")
        guest = self.get(guest_email)
        if not guest or guest.get("disabled"):
            raise ValueError("Na téhle bráně takový účet není.")
        if guest["id"] == owner["id"]:
            raise ValueError("Do vlastního prostoru přístup nepotřebuješ.")
        with self._lock:
            row = self.db.execute(
                "SELECT role FROM space_access WHERE owner_id = ? AND guest_id = ?",
                (owner["id"], guest["id"])).fetchone()
            self.db.execute(
                "INSERT INTO space_access (owner_id, guest_id, role, created, created_by)"
                " VALUES (?, ?, ?, ?, ?) ON CONFLICT(owner_id, guest_id)"
                " DO UPDATE SET role = excluded.role",
                (owner["id"], guest["id"], role, time.time(), owner["id"]))
        return guest, (row["role"] if row else "")

    def access_remove(self, owner_id, guest_id):
        with self._lock:
            cur = self.db.execute(
                "DELETE FROM space_access WHERE owner_id = ? AND guest_id = ?",
                (int(owner_id), int(guest_id)))
        return bool(cur.rowcount)

    def access_row(self, owner_id, guest_id):
        """Role z řádku pro konkrétního člověka (i `zadny`), nebo None —
        bez ohledu na přístup pro všechny."""
        with self._lock:
            row = self.db.execute(
                "SELECT role FROM space_access WHERE owner_id = ? AND guest_id = ?",
                (int(owner_id), int(guest_id))).fetchone()
        return row["role"] if row else None

    def access_detail(self, owner_id, guest_id):
        """(role, odkud) hosta v prostoru vlastníka, nebo (None, None).

        `odkud` je "osobne" (řádek pro toho člověka) nebo "vsem" (přístup pro
        všechny na bráně). Řádek pro člověka má vždycky přednost — i nižší
        role a výjimka `zadny`. Obě strany musí mít aktivní účet — ptá se
        každý požadavek hosta."""
        from .pristup import BLOCK, ROLES as SPACE_ROLES
        try:
            owner_id, guest_id = int(owner_id), int(guest_id)
        except (TypeError, ValueError):
            return None, None
        if owner_id == guest_id:
            return None, None
        with self._lock:
            active = self.db.execute(
                "SELECT COUNT(*) FROM users WHERE id IN (?, ?) AND disabled = 0",
                (owner_id, guest_id)).fetchone()[0]
            if active != 2:
                return None, None
            row = self.db.execute(
                "SELECT role FROM space_access WHERE owner_id = ? AND guest_id = ?",
                (owner_id, guest_id)).fetchone()
            if row:
                if row["role"] == BLOCK or row["role"] not in SPACE_ROLES:
                    return None, None
                return row["role"], "osobne"
            row = self.db.execute("SELECT role FROM space_access_all WHERE owner_id = ?",
                                  (owner_id,)).fetchone()
        if row and row["role"] in SPACE_ROLES:
            return row["role"], "vsem"
        return None, None

    def access_role(self, owner_id, guest_id):
        """Role hosta v prostoru vlastníka (osobně, nebo přes přístup pro
        všechny), nebo None."""
        return self.access_detail(owner_id, guest_id)[0]

    def access_list(self, owner_id=None, guest_id=None, blocks=False):
        """Povolení vlastníka (komu dal přístup), hosta (kam smí), nebo všechna —
        jen řádky pro konkrétní lidi. Výjimky (`zadny`) jen s `blocks`."""
        where, args = [], []
        if owner_id is not None:
            where.append("a.owner_id = ?")
            args.append(int(owner_id))
        if guest_id is not None:
            where.append("a.guest_id = ?")
            args.append(int(guest_id))
        if not blocks:
            where.append("a.role != 'zadny'")
        sql = ("SELECT a.owner_id, a.guest_id, a.role, a.created, o.email AS owner_email,"
               " o.name AS owner_name, g.email AS guest_email, g.name AS guest_name"
               " FROM space_access a JOIN users o ON o.id = a.owner_id"
               " JOIN users g ON g.id = a.guest_id WHERE o.disabled = 0 AND g.disabled = 0")
        if where:
            sql += " AND " + " AND ".join(where)
        with self._lock:
            return [dict(r) for r in self.db.execute(sql + " ORDER BY a.created", args)]

    def access_all_set(self, owner, role):
        """Vlastník pustí do prostoru všechny aktivní účty na bráně s rolí.
        Vrací starou roli nebo ""."""
        from .pristup import ROLES as SPACE_ROLES
        if role not in SPACE_ROLES:
            raise ValueError("Neznámá role.")
        if not owner or not owner.get("id"):
            raise PermissionError("Chybí vlastník prostoru.")
        with self._lock:
            row = self.db.execute("SELECT role FROM space_access_all WHERE owner_id = ?",
                                  (owner["id"],)).fetchone()
            self.db.execute(
                "INSERT INTO space_access_all (owner_id, role, created, created_by)"
                " VALUES (?, ?, ?, ?) ON CONFLICT(owner_id) DO UPDATE SET role = excluded.role",
                (owner["id"], role, time.time(), owner["id"]))
        return row["role"] if row else ""

    def access_all_remove(self, owner_id):
        """Zruší přístup pro všechny. Vrací starou roli nebo ""."""
        with self._lock:
            row = self.db.execute("SELECT role FROM space_access_all WHERE owner_id = ?",
                                  (int(owner_id),)).fetchone()
            self.db.execute("DELETE FROM space_access_all WHERE owner_id = ?", (int(owner_id),))
        return row["role"] if row else ""

    def access_all_list(self, owner_id=None):
        """Přístupy pro všechny (jen aktivní vlastníci): [{owner_id, role, …}]."""
        sql = ("SELECT v.owner_id, v.role, v.created, o.email AS owner_email,"
               " o.name AS owner_name FROM space_access_all v JOIN users o"
               " ON o.id = v.owner_id WHERE o.disabled = 0")
        args = []
        if owner_id is not None:
            sql += " AND v.owner_id = ?"
            args.append(int(owner_id))
        with self._lock:
            return [dict(r) for r in self.db.execute(sql + " ORDER BY v.created", args)]

    def access_kam(self, guest_id):
        """Kam smí host: [{owner_id, owner_email, owner_name, role, pres}] —
        osobní povolení a přístupy pro všechny, kde host nemá vlastní řádek
        (ten má přednost, i výjimka). Host musí mít aktivní účet."""
        from .pristup import ROLES as SPACE_ROLES
        guest_id = int(guest_id)
        out = [{**r, "pres": "osobne"} for r in self.access_list(guest_id=guest_id)
               if r["role"] in SPACE_ROLES]
        with self._lock:
            if not self.db.execute("SELECT 1 FROM users WHERE id = ? AND disabled = 0",
                                   (guest_id,)).fetchone():
                return []
            rows = self.db.execute(
                "SELECT v.owner_id, v.role, v.created, o.email AS owner_email,"
                " o.name AS owner_name FROM space_access_all v JOIN users o"
                " ON o.id = v.owner_id WHERE o.disabled = 0 AND v.owner_id != ?"
                " AND NOT EXISTS (SELECT 1 FROM space_access a WHERE"
                " a.owner_id = v.owner_id AND a.guest_id = ?) ORDER BY v.created",
                (guest_id, guest_id)).fetchall()
        out += [{**dict(r), "guest_id": guest_id, "pres": "vsem"} for r in rows
                if r["role"] in SPACE_ROLES]
        return out

    # ── napojení z appky Claude (OAuth pro MCP) ──────────────────────────────
    def oauth_client_add(self, name, redirect_uris):
        client_id = "hub-" + secrets.token_urlsafe(18)
        with self._lock:
            self.db.execute(
                "INSERT INTO oauth_clients (client_id, name, redirect_uris, created)"
                " VALUES (?, ?, ?, ?)",
                (client_id, (name or "")[:80], json.dumps(list(redirect_uris)), time.time()))
        return client_id

    def oauth_client(self, client_id):
        if not client_id or len(client_id) > 80:
            return None
        with self._lock:
            row = self.db.execute("SELECT * FROM oauth_clients WHERE client_id = ?",
                                  (client_id,)).fetchone()
        if not row:
            return None
        try:
            uris = json.loads(row["redirect_uris"])
        except ValueError:
            uris = []
        return {"client_id": row["client_id"], "name": row["name"],
                "redirect_uris": [u for u in uris if isinstance(u, str)]}

    def oauth_code_new(self, user, client_id, redirect_uri, challenge, resource):
        code = secrets.token_urlsafe(32)
        now = time.time()
        with self._lock:
            self.db.execute("DELETE FROM oauth_codes WHERE expires < ?", (now,))
            self.db.execute(
                "INSERT INTO oauth_codes (fingerprint, user_id, client_id, redirect_uri,"
                " challenge, resource, expires) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (_fingerprint(code), user["id"], client_id, redirect_uri, challenge,
                 resource or "", now + CODE_TTL))
        return code

    def oauth_code_take(self, code):
        """Kód platí jednou: přečte se a hned smaže, i když by dál neprošel."""
        if not code or len(code) > 200:
            return None
        fp = _fingerprint(code)
        with self._lock:
            row = self.db.execute("SELECT * FROM oauth_codes WHERE fingerprint = ?",
                                  (fp,)).fetchone()
            self.db.execute("DELETE FROM oauth_codes WHERE fingerprint = ?", (fp,))
        if not row or row["expires"] < time.time():
            return None
        return dict(row)

    def _oauth_pair(self, user_id, client_id, family, resource):
        access = secrets.token_urlsafe(TOKEN_BYTES)
        refresh = secrets.token_urlsafe(TOKEN_BYTES)
        now = time.time()
        for token, kind, ttl in ((access, "access", ACCESS_TTL),
                                 (refresh, "refresh", REFRESH_TTL)):
            self.db.execute(
                "INSERT INTO oauth_tokens (fingerprint, kind, user_id, client_id, family,"
                " resource, created, expires) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (_fingerprint(token), kind, user_id, client_id, family, resource or "",
                 now, now + ttl))
        return {"access_token": access, "refresh_token": refresh,
                "token_type": "Bearer", "expires_in": ACCESS_TTL}

    def oauth_issue(self, user_id, client_id, resource):
        with self._lock:
            self.db.execute("DELETE FROM oauth_tokens WHERE expires < ?", (time.time(),))
            return self._oauth_pair(user_id, client_id, secrets.token_hex(16), resource)

    def oauth_refresh(self, refresh, client_id):
        """Vymění obnovovací token za nový pár. Znovu použitý (už vyměněný)
        token = krádež: zruší se celé přihlášení (rodina)."""
        if not refresh or len(refresh) > 200:
            return None
        fp = _fingerprint(refresh)
        now = time.time()
        with self._lock:
            row = self.db.execute(
                "SELECT t.*, u.disabled FROM oauth_tokens t JOIN users u ON u.id = t.user_id"
                " WHERE t.fingerprint = ? AND t.kind = 'refresh'", (fp,)).fetchone()
            if not row:
                return None
            if row["used"] or row["client_id"] != client_id:
                self.db.execute("DELETE FROM oauth_tokens WHERE family = ?", (row["family"],))
                return None
            if row["expires"] < now or row["disabled"]:
                return None
            self.db.execute("UPDATE oauth_tokens SET used = 1 WHERE fingerprint = ?", (fp,))
            # Předchozí přístupové tokeny z téhle rodiny končí s výměnou.
            self.db.execute("DELETE FROM oauth_tokens WHERE family = ? AND kind = 'access'",
                            (row["family"],))
            return self._oauth_pair(row["user_id"], client_id, row["family"], row["resource"])

    def oauth_user(self, access, resource):
        """Komu přístupový token patří (a jen pro tenhle `resource`), nebo None."""
        if not access or len(access) > 200:
            return None
        fp = _fingerprint(access)
        with self._lock:
            row = self.db.execute(
                "SELECT u.*, t.expires AS t_expires, t.resource AS t_resource,"
                " t.client_id AS t_client, t.family AS t_family FROM oauth_tokens t"
                " JOIN users u ON u.id = t.user_id"
                " WHERE t.fingerprint = ? AND t.kind = 'access'", (fp,)).fetchone()
            if not row or row["disabled"] or row["t_expires"] < time.time():
                return None
            if row["t_resource"] and resource and row["t_resource"] != resource:
                return None
            self.db.execute("UPDATE oauth_tokens SET seen = ? WHERE fingerprint = ?",
                            (time.time(), fp))
        user = self._public(row)
        user["client_id"] = row["t_client"]
        user["family"] = row["t_family"]
        return user

    def oauth_revoke_token(self, token):
        """RFC 7009: zruší celé přihlášení, ke kterému token patří."""
        if not token or len(token) > 200:
            return
        with self._lock:
            row = self.db.execute("SELECT family FROM oauth_tokens WHERE fingerprint = ?",
                                  (_fingerprint(token),)).fetchone()
            if row:
                self.db.execute("DELETE FROM oauth_tokens WHERE family = ?", (row["family"],))

    def oauth_logins(self, email=None):
        """Aktivní napojení (jedno na přihlášení) pro správu."""
        sql = ("SELECT u.email, c.name AS client, t.family, MIN(t.created) AS created,"
               " MAX(t.seen) AS seen FROM oauth_tokens t JOIN users u ON u.id = t.user_id"
               " LEFT JOIN oauth_clients c ON c.client_id = t.client_id"
               " WHERE t.expires > ? AND t.used = 0")
        args = [time.time()]
        if email:
            sql += " AND u.email = ?"
            args.append(email.strip().lower())
        sql += " GROUP BY t.family ORDER BY seen DESC"
        with self._lock:
            return [dict(r) for r in self.db.execute(sql, args)]

    def oauth_revoke_user(self, email, family=""):
        """Zruší napojení účtu — všechna, nebo jedno podle `family`. Vrací počet."""
        email = (email or "").strip().lower()
        with self._lock:
            if family:
                cur = self.db.execute(
                    "DELETE FROM oauth_tokens WHERE family = ? AND user_id ="
                    " (SELECT id FROM users WHERE email = ?)", (family, email))
            else:
                cur = self.db.execute(
                    "DELETE FROM oauth_tokens WHERE user_id ="
                    " (SELECT id FROM users WHERE email = ?)", (email,))
        return cur.rowcount
