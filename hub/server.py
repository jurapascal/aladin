"""
Local HTTP + WebSocket server behind the hub UI.

It binds to 127.0.0.1 on a random port and every request must carry the token
printed into the page URL at startup — this process can spawn shells, so it is
never reachable by anything that didn't get the token from us.

Terminal sessions live in the server, not in the browser connection: reloading
the page re-attaches to the running Claude sessions and replays their recent
output instead of killing them.

Several pages can watch the same sessions at once — on the gateway the app and
a browser open the same user's hub. Output goes to every page, tabs opened,
closed or renamed in one show up in the others, and the pty takes the size of
the page that was used last (typing or focus), so pages don't fight over it.
"""
import base64
import codecs
import gzip
import hashlib
import json
import mimetypes
import os
import posixpath
import re
import secrets
import socketserver
import string
import subprocess
import struct
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import (account, automodel, chats, hledani, clockify, connect, core, cteni, jev, motivy, pocitac, predplatne,
               fronta, pozadi, prenos, prenos_hub, prohlizec, pty_backend, qr, remote, restart, setup, stats, stazene,
               stavradek, uspavani, vzhled, rewind, soubory_hledani, kontext, vetev)

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
MAX_FRAME = 4 * 1024 * 1024          # a client frame is keystrokes; 4 MB is plenty


def _scrollback():
    """Kolik bajtů výstupu si tab pamatuje pro nové okno / reload (256 KB–1 MB).
    `scrollback_kb` v hub-config.json nebo HUB_SCROLLBACK_KB."""
    raw = os.environ.get("HUB_SCROLLBACK_KB") or core.CONFIG.get("scrollback_kb") or 256
    try:
        kb = int(float(raw))
    except (TypeError, ValueError):
        kb = 256
    return max(256, min(1024, kb)) * 1024


SCROLLBACK = _scrollback()           # replayed to the page after a reload
SLEEP_COALESCE = 0.016               # výstup tabu se oknům posílá po ~16 ms dávkách
# Kolik neodeslaných bajtů smí u jednoho okna ležet v socketu, než se mu živý
# výstup přestane posílat (pomalý klient nesmí zdržovat čtečku ptyje).
OUT_BACKLOG = 512 * 1024
COOKIE_NAME = "hub_phone"            # so a paired phone opens without the QR
COOKIE_MAX_AGE = 60 * 60 * 24 * 365

# A phone whose token was rotated still has the icon on its home screen; a bare
# 403 there looks like the hub is broken.
PAIR_AGAIN = """<!doctype html><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>Aladin</title>
<style>body{font:16px/1.6 system-ui,sans-serif;margin:0;min-height:100vh;
display:grid;place-items:center;background:#16150f;color:#e8e3d3;padding:24px}
div{max-width:22rem;text-align:center}b{color:#e0a458}</style>
<div><p><b>Telefon už není spárovaný.</b></p>
<p>Otevři na počítači Aladina → Nastavení → Telefon a načti QR kód znovu.</p></div>
"""


# ── WebSocket plumbing (RFC 6455, only what we need) ─────────────────────────
class WSConn:
    """One websocket connection. Safe to send from several threads."""

    def __init__(self, sock):
        self.sock = sock
        self._lock = threading.Lock()
        self.closed = False
        # Pomalé okno: živý výstup se mu přeskočil a čeká na překreslení.
        self.lost = False

    def backlog(self):
        """Kolik bajtů leží v odesílací frontě socketu (Linux: SIOCOUTQ); jinde 0."""
        try:
            import fcntl
            import termios
            buf = fcntl.ioctl(self.sock.fileno(), termios.TIOCOUTQ, b"\0\0\0\0")
            return struct.unpack("i", buf)[0]
        except Exception:
            return 0

    def send(self, payload, opcode=0x1):
        header = bytearray([0x80 | opcode])
        n = len(payload)
        if n < 126:
            header.append(n)
        elif n < 65536:
            header.append(126)
            header += struct.pack(">H", n)
        else:
            header.append(127)
            header += struct.pack(">Q", n)
        with self._lock:
            if self.closed:
                return
            try:
                self.sock.sendall(bytes(header) + payload)
            except OSError:
                self.closed = True

    def send_json(self, obj):
        self.send(json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def close(self):
        self.closed = True
        try:
            self.sock.shutdown(2)
        except OSError:
            pass


def read_frame(rfile):
    """Return (fin, opcode, payload) or None at end of stream."""
    head = rfile.read(2)
    if len(head) < 2:
        return None
    b1, b2 = head[0], head[1]
    fin, opcode = b1 & 0x80, b1 & 0x0F
    masked, length = b2 & 0x80, b2 & 0x7F
    if length == 126:
        length = struct.unpack(">H", rfile.read(2))[0]
    elif length == 127:
        length = struct.unpack(">Q", rfile.read(8))[0]
    if length > MAX_FRAME:
        return None
    mask = rfile.read(4) if masked else b""
    payload = rfile.read(length) if length else b""
    if len(payload) < length:
        return None
    if masked:
        payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    return bool(fin), opcode, payload


# ── Host v cizím prostoru (gateway/pristup.py) ──────────────────────────────
def guest_from(headers):
    """Kdo je host, jak ho brána ohlásila v X-Hub-Guest — jen v prostoru na
    serveru. Brána tu hlavičku z příchozích požadavků vždycky zahodí a vloží
    vlastní, takže jí tady jde věřit; na počítači se nebere vůbec."""
    raw = headers.get("X-Hub-Guest", "") if headers else ""
    if not raw or not core.on_gateway():
        return None
    try:
        data = json.loads(base64.urlsafe_b64decode(raw.encode("ascii")).decode("utf-8"))
        data["id"] = int(data["id"])
    except (ValueError, KeyError, TypeError):
        return None
    if data.get("role") not in ("predplatne", "pomocnik", "plny"):
        return None
    return data


def guest_owns(guest, session):
    """Je tab hostův? (Správa předplatného smí jen na své.)"""
    g = getattr(session, "guest", None) if session else None
    return bool(g and guest and g.get("id") == guest.get("id"))


# ── Terminal sessions ────────────────────────────────────────────────────────
class _SpiciPty:
    """Místo pty uspaného tabu: nic nedělá. Starý pty je zavřený a jeho číslo
    souboru už může patřit něčemu jinému — druhé zavření by to zavřelo."""
    proc = None

    def read(self):
        return b""

    def write(self, data):
        pass

    def resize(self, cols, rows):
        pass

    def alive(self):
        return False

    def close(self):
        pass


class Session:
    """A pty running one bash session, plus the scrollback we replay on reload."""

    def __init__(self, sid, title, kind, path, argv, cwd, cols, rows,
                 agent="", model="", env=None, vault="", spici=""):
        self.id = sid
        self.title = title
        self.kind = kind
        self.path = path
        # Čím tab jede, se drží tady: po reloadu stránky se sessions obnovují
        # ze serveru, takže bez toho by tab po F5 zapomněl, kdo v něm běží.
        self.agent = agent
        self.model = model
        # Nad kterým trezorem tab běží. Drží se to tu ze stejného důvodu jako
        # agent: po reloadu (nebo když proxy zavře nečinné spojení) se taby
        # obnovují ze serveru a firemní tab by jinak zapomněl, že je firemní —
        # v liště by přestal svítit fialově, což je jediné, podle čeho se
        # pozná, že se píše do trezoru celého týmu.
        self.vault = vault
        self.started = time.time()
        # Spustil se agent s možností bypassu? Bez ní ho v tabu zapnout nejde.
        self.bypass = (env or {}).get("HUB_AGENT_BYPASS") == "1"
        # V které uložené konverzaci tab pokračuje (seznam konverzací).
        self.resume = (env or {}).get("HUB_AGENT_RESUME", "")
        # Konverzace, do které tab píše. Nové id volí hub sám (--session-id),
        # u pokračování je to ta, ve které se pokračuje — čtení (cteni.py) pak
        # ví, který přepis číst, aniž by ho hledalo podle času souborů.
        self.chat_id = (env or {}).get("HUB_SESSION_ID", "") or self.resume
        # Kdo tab otevřel, když to byl host z jiného účtu (guest_from) — u tabu
        # se ukáže „spravuje …“.
        self.guest = None
        if spici:
            # Obnova po restartu: tab, na který se zrovna nikdo nedívá, se
            # rovnou založí uspaný (`spici` = konverzace). Claude se pustí, až
            # ho někdo otevře — po aktualizaci tak nenabíhají všechny taby
            # najednou (procesor, paměť a překreslování celé konverzace v každém).
            self.pty = _SpiciPty()
        else:
            child = core.child_env()
            child.update(env or {})
            self.pty = pty_backend.spawn(argv, cwd=cwd, env=child,
                                         cols=cols, rows=rows)
        self.buffer = bytearray()
        # Výstup čekající na odeslání oknům (slučuje se po ~16 ms).
        self._ceka = []
        self._casovac = None
        self._odeslano = 0.0
        # Tab může vidět víc oken naráz — appka i prohlížeč na tentýž prostor.
        # Výpis jde všem; pty ale má jen jeden rozměr a ten drží okno, které se
        # naposledy používalo (`driver`). Rozměry ostatních se pamatují, aby
        # rozměr šlo předat hned, jak se začne pracovat tam.
        self.conns = set()
        self.sizes = {}
        self.driver = None
        self.size = (cols, rows)
        self.exited = False
        self._znovu = False
        # Uspaný tab (hub/uspavani.py): proces neběží, konverzace čeká na
        # probuzení. `posledni` = kdy naposled přišel vstup nebo výstup.
        self.uspany = False
        self.uspany_chat = ""
        # Název tabu se sám bere z konverzace (HUB.pojmenuj_taby), dokud ho
        # člověk nepřejmenuje ručně.
        self.vlastni_nazev = False
        self.posledni = time.time()
        self._vystup_at = 0.0
        self._vstup_at = 0.0
        # Vstup, který přišel do uspaného (nebo právě se probouzejícího) tabu;
        # doručí se, až Claude naběhne. None = píše se rovnou.
        self._fronta = None
        self._buzeni = threading.Lock()
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._lock = threading.Lock()
        if spici:
            self.uspany = True
            self.uspany_chat = spici
            self._fronta = []
            self._znovu = True          # _pump hned skončí, tab ale nezavře
            self.buffer += ("\x1b[2m[ tab čeká — Claude se vrátí do konverzace, "
                            "jakmile ho otevřeš ]\x1b[0m\r\n").encode("utf-8")
        self.thread = threading.Thread(target=self._pump, daemon=True)
        self.thread.start()

    def _pump(self):
        while True:
            data = self.pty.read()
            if not data:
                break
            # Appending and sending under one lock is what keeps a replay from
            # interleaving with live output when a page attaches mid-stream.
            with self._lock:
                self.posledni = self._vystup_at = time.time()
                self.buffer += data
                if len(self.buffer) > SCROLLBACK:
                    del self.buffer[:len(self.buffer) - SCROLLBACK]
                text = self._decoder.decode(data)
                if text:
                    self._ceka.append(text)
                    # Dávka: první kus jde hned, další do 16 ms se slepí. Čtečka
                    # ptyje nikdy nečeká — odeslání obstará časovač.
                    if self._casovac is None:
                        if time.time() - self._odeslano >= SLEEP_COALESCE:
                            self._odeslat()
                        else:
                            self._casovac = threading.Timer(SLEEP_COALESCE, self._casovac_tik)
                            self._casovac.daemon = True
                            self._casovac.start()
        # Pod zámkem spolu s attach(): každé okno dostane „exit" právě jednou.
        with self._lock:
            self._odeslat()                  # nic nezůstane viset v dávce
            if self._znovu:
                return                       # relaunch(): tab jede dál s novým procesem
            self.exited = True
            conns = list(self.conns)
        for conn in conns:
            conn.send_json({"t": "exit", "id": self.id})

    def _casovac_tik(self):
        with self._lock:
            self._casovac = None
            self._odeslat()

    def _odeslat(self):
        """Pod zámkem: pošle slepenou dávku výstupu všem oknům. Okno, kterému
        leží v socketu moc neodeslaného (pomalé spojení), živý výstup přeskočí
        a až se vzpamatuje, dostane tab znovu vykreslený z paměti — čtečka
        ptyje se kvůli němu nezdrží ani nenafukuje paměť."""
        if not self._ceka:
            return
        text = "".join(self._ceka)
        self._ceka = []
        self._odeslano = time.time()
        msg = None
        for conn in list(self.conns):
            if conn.closed:
                continue
            fronta = conn.backlog()
            if conn.lost:
                if fronta > OUT_BACKLOG // 4:
                    continue
                conn.lost = False
                conn.send_json({"t": "out", "id": self.id,
                                "d": "\x1b[2J\x1b[3J\x1b[H"
                                     + bytes(self.buffer).decode("utf-8", "replace")})
                continue
            if fronta > OUT_BACKLOG:
                conn.lost = True
                continue
            if msg is None:
                msg = {"t": "out", "id": self.id, "d": text}
            conn.send_json(msg)

    def attach(self, conn):
        """Add a page to the viewers and replay what it missed.

        The replay goes only to that page — the others already show it."""
        with self._lock:
            self._odeslat()                  # čekající dávka už je v paměti, novému oknu by se zdvojila
            self.conns.add(conn)
            backlog = bytes(self.buffer)
            if backlog:
                # Decoded separately: the live decoder carries partial-character
                # state for the stream and replay must not disturb it.
                conn.send_json({"t": "out", "id": self.id,
                                "d": backlog.decode("utf-8", "replace")})
            exited = self.exited
        if exited:
            conn.send_json({"t": "exit", "id": self.id})

    def detach(self, conn):
        """The page went away. If it held the pty size, another page takes it."""
        with self._lock:
            self.conns.discard(conn)
            self.sizes.pop(conn, None)
            if self.driver is conn:
                # Převezme ho okno, které svůj rozměr hlásilo naposledy.
                self.driver = next(reversed(self.sizes), None)
                if self.driver is not None:
                    self._apply(self.sizes[self.driver])

    def resize(self, conn, cols, rows):
        """Remember the page's terminal size; the pty follows only the driver."""
        with self._lock:
            self.sizes.pop(conn, None)       # na konec: „hlásil naposledy"
            self.sizes[conn] = (cols, rows)
            if self.driver is None:
                self.driver = conn
            if self.driver is conn:
                self._apply((cols, rows))

    def drive(self, conn):
        """The page is in use now (typing, focus) — the pty takes its size.

        Bez toho si okna rozměr přetahují: každé ho posílá při každém přepočtu
        a Claude Code se pak v tom druhém vykreslí na cizí šířku."""
        if self.driver is conn:              # při psaní skoro vždycky
            return
        with self._lock:
            size = self.sizes.get(conn)
            if size is None:
                return
            self.driver = conn
            self._apply(size)

    def _apply(self, size):
        # Pod zámkem. Stejný rozměr znovu neposílat: ConPTY na Windows při
        # každém resize překreslí celou obrazovku.
        if size != self.size:
            self.size = size
            self.pty.resize(*size)

    def relaunch(self, argv, cwd, env):
        """Pustí v tomtéž tabu nový proces (stejné id, stejná okna). Pro Clauda
        s novým přihlášením: `--resume` téže konverzace, takže člověk vidí jen
        krátké překreslení a pokračuje, kde byl."""
        with self._lock:
            self._znovu = True
        try:
            self.pty.close()
        except Exception:
            pass
        self.thread.join(timeout=5)
        child = core.child_env()
        child.update(env or {})
        self.pty = pty_backend.spawn(argv, cwd=cwd, env=child, cols=self.size[0], rows=self.size[1])
        with self._lock:
            self._znovu = False
            self.exited = False
            self.uspany = False
            self.started = self.posledni = time.time()
            self._vystup_at = 0.0
            self.resume = (env or {}).get("HUB_AGENT_RESUME", "") or self.resume
            self.chat_id = (env or {}).get("HUB_SESSION_ID", "") or self.resume
            self.buffer = bytearray()
            self._ceka = []
            self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
            for conn in self.conns:
                conn.send_json({"t": "out", "id": self.id, "d": "\x1b[2J\x1b[3J\x1b[H"})
        self.thread = threading.Thread(target=self._pump, daemon=True)
        self.thread.start()

    def write(self, data):
        """Vstup do tabu. Uspaný tab se probudí a vstup dostane, až Claude naběhne."""
        with self._lock:
            self.posledni = self._vstup_at = time.time()   # _vstup_at: kdy psalo okno (fronta.py)
            if self._fronta is not None:
                self._fronta.append(data)
                spi = self.uspany
            else:
                spi = None
        if spi is None:
            self.pty.write(data)
        elif spi:
            self.probud()

    def uspi(self, chat):
        """Ukončí Clauda v tabu, tab zůstává (hub/uspavani.py). Obraz i okna
        zůstanou; `probud()` ho pustí znovu nad konverzací `chat`."""
        with self._buzeni:
            with self._lock:
                if self.uspany or self.exited:
                    return False
                self._znovu = True
                self._fronta = []
            try:
                self.pty.close()
            except Exception:
                pass
            self.thread.join(timeout=5)
            self.pty = _SpiciPty()
            note = ("\r\n\x1b[2m[ tab je uspaný, ať nedrží paměť — Claude se vrátí "
                    "do téže konverzace, jakmile tab otevřeš nebo napíšeš ]\x1b[0m\r\n")
            with self._lock:
                self._znovu = False
                self.uspany = True
                self.uspany_chat = chat or ""
                self.buffer += note.encode("utf-8")
                for conn in self.conns:
                    conn.send_json({"t": "out", "id": self.id, "d": note})
                cekaji = bool(self._fronta)
        if cekaji:
            # Někdo napsal zrovna během uspávání — hned zase vzhůru.
            self.probud()
        return True

    def probud(self):
        """Pustí uspaný tab znovu (`claude --resume`) a doručí, co mezitím přišlo."""
        with self._buzeni:
            if not self.uspany:
                return
            try:
                HUB.probud(self)
            except Exception as exc:
                core.log_error(f"tab {self.id}: probuzení selhalo", exc)
                with self._lock:
                    self.exited = True
                    self.uspany = False
                    self._fronta = None
                    conns = list(self.conns)
                for conn in conns:
                    conn.send_json({"t": "exit", "id": self.id})
                return
        HUB.broadcast({"t": "tab-uspany", "id": self.id, "on": False})
        threading.Thread(target=self._dorucit_frontu, daemon=True).start()

    def _dorucit_frontu(self, ticho=1.5, nejdele=25.0):
        """Počká, až Claude po startu dokreslí (výstup a pak chvíle ticha),
        a pošle mu vstup z fronty. Kousky s pauzou — text a Enter slepené
        dohromady by Claude Code vzal jako vložený text (viz composer.js)."""
        konec = time.time() + nejdele
        while time.time() < konec:
            v = self._vystup_at
            if v and time.time() - v >= ticho:
                break
            time.sleep(0.2)
        while True:
            with self._lock:
                if not self._fronta:
                    self._fronta = None
                    return
                kus = self._fronta.pop(0)
            if self.exited:
                return
            try:
                self.pty.write(kus)
            except Exception:
                pass
            time.sleep(0.25)

    def close(self):
        with self._lock:
            self.conns.clear()
            self.sizes.clear()
            self.driver = None
            self._fronta = None
        try:
            self.pty.close()
        except Exception:
            pass

    def info(self):
        return {"id": self.id, "title": self.title, "kind": self.kind,
                "path": self.path, "exited": self.exited,
                "agent": self.agent, "model": self.model, "bypass": self.bypass,
                "resume": self.resume, "vault": self.vault,
                "chat": getattr(self, "chat_id", "") or self.resume or "",
                "uspany": self.uspany,
                "guest": (self.guest or {}).get("name") or (self.guest or {}).get("email") or ""}


class Hub:
    """Server-side state: the open sessions and who is currently watching them."""

    def __init__(self):
        self.sessions = {}
        self.clients = 0
        self.conns = set()               # otevřená okna — komu poslat hlášku
        self.last_empty_at = None
        self._next_id = 1
        self._lock = threading.Lock()

    def new_id(self):
        with self._lock:
            sid = self._next_id
            self._next_id += 1
            return sid

    def pojmenuj_taby(self):
        """Tab s Claudem dostane název konverzace (Claude Code ho sám vymyslí
        z obsahu — `ai-title`), ať se v tabech dá vyznat i bez přejmenovávání."""
        for s in list(self.sessions.values()):
            if s.exited or s.vlastni_nazev:
                continue
            if not (s.kind == "project" or s.kind.startswith("slash:")):
                continue
            if (s.agent or core.default_agent()) != "claude":
                continue
            try:
                cesta = core.transcript_for(s)
                nazev = " ".join(chats.title_for(cesta).split())[:60] if cesta else ""
            except Exception:
                continue
            if nazev and nazev != s.title:
                s.title = nazev
                self.broadcast({"t": "tab-renamed", "id": s.id, "title": nazev})

    def hlidej_nazvy(self, every=15):
        while True:
            time.sleep(every)
            try:
                self.pojmenuj_taby()
            except Exception as exc:
                core.log_error("názvy tabů", exc)

    def chat_tabs(self):
        """Id konverzace -> id tabu, ve kterém právě běží. Seznam konverzací
        podle toho na tab přepne, místo aby pustil druhého Clauda do téže."""
        running = {}
        tabs = [s for s in list(self.sessions.values())
                if not s.exited and (s.kind == "project" or s.kind.startswith("slash:"))
                and (s.agent or core.default_agent()) == "claude"]
        # Napřed jistota: id, které tabu dal hub (--session-id), pokračování
        # (--resume) a přestěhované kopie (core._pokracovani).
        for s in tabs:
            for chat in [s.resume, getattr(s, "chat_id", "")] + list(getattr(s, "rodina", None) or []):
                if chat:
                    running.setdefault(chat, s.id)
        # Odhad podle nejnovějšího přepisu ve složce jen u tabů, které vlastní
        # id nemají. Dřív se hádalo u všech a tab přebral cizí chat ze stejné
        # složky (dva nové chaty v ~): klepnutí na chat pak otevřelo jiný.
        for s in tabs:
            if s.resume or getattr(s, "chat_id", ""):
                continue
            transcript = core._tab_transcript(s.id, s.path or core.HOME, s.started)
            if transcript:
                running.setdefault(os.path.basename(transcript)[:-6], s.id)
        return running

    def open(self, kind, path, title, cols, rows, agent="", model="", resume="", fork=False,
             vault="", prompt="", ukol="", spici=False):
        # Tab s úkolem ze serveru (hub/pocitac.py): otevřený kvůli úkolu, nebo
        # pokračování jeho konverzace. Dostane hook, přes který žádosti
        # o souhlas jdou i na server; ručně otevřený tab nic z toho nemá.
        launch = meta = None
        if kind == "project" and (agent or core.default_agent()) == "claude" \
                and not core.on_gateway():
            meta = pocitac.ukol_for_tab(ukol, resume)
            launch = pocitac.tab_launch(meta) if meta else None
        script, cwd, env = self._command_for(kind, path, agent, model, resume, fork, vault, prompt,
                                             launch)
        # Agent si model mohl doplnit sám (Ollama bez modelu nespustíš) —
        # ať se to dozví i tab, jinak by chip v bublině hlásil „výchozí".
        model = env.get("HUB_AGENT_MODEL") or model
        sid = self.new_id()
        # Podle téhle značky Stop hook pozná, ve kterém tabu session běží,
        # a zavření tabu pak uloží do paměti právě ji.
        env = {**env, "HUB_TAB": core.tab_tag(sid)}
        # Uspaný start jen tam, kde je co probudit: Claude nad uloženou
        # konverzací, bez úvodní zprávy (ta by čekala, až ho někdo otevře).
        spici = (resume if spici and resume and not prompt and not fork and not launch
                 and (kind == "project" or kind.startswith("slash:"))
                 and (agent or core.default_agent()) == "claude" else "")
        session = Session(sid, title, kind, path, core.bash_argv(script), cwd,
                          cols, rows, agent=agent, model=model, env=env, vault=vault,
                          spici=spici)
        self.sessions[sid] = session
        if launch:
            pocitac.bind_tab(launch, session, meta)
        return session

    @staticmethod
    def _command_for(kind, path, agent="", model="", resume="", fork=False, vault="", prompt="",
                     launch=None):
        """(příkaz, pracovní složka, prostředí navíc) pro daný druh tabu."""
        if kind == "shell":
            return core.cmd_shell(), (path or core.HOME), {}
        if kind == "deploy":
            return core.cmd_deploy(path), path, {}
        if kind.startswith("install:"):
            return core.cmd_install(kind.split(":", 1)[1]), (path or core.HOME), {}
        if kind.startswith("auth:"):
            return core.cmd_auth(kind.split(":", 1)[1]), (path or core.HOME), {}
        if kind.startswith("slash:"):
            # Slash příkazy jsou naše skilly z ~/.claude/skills — rozumí jim jen
            # agent, který je čte. Jinému by se předal jako holý text promptu.
            spec = core.agent_spec(agent)
            if not spec.get("skills"):
                agent = ""
            script, env = core.cmd_agent(path, agent,
                                         slash="/" + kind.split(":", 1)[1])
            return script, path, env
        # Claude Code se u nové složky ptá, jestli jí věří — tab nad ní ale
        # otevírá člověk sám, tak se to rovnou potvrdí (core.trust_folder).
        if (agent or core.default_agent()) == "claude" and path:
            try:
                core.trust_folder(path)
            except Exception as exc:
                core.log_error("důvěra ke složce", exc)
        script, env = core.cmd_agent(path, agent, model=model, resume=resume, fork=fork,
                                     vault=vault, prompt=prompt, launch=launch)
        return script, path, env

    def probud(self, s):
        """Uspaný tab (Session.uspi) znovu s Claudem: nad toutéž konverzací,
        nebo čistý, když v něm ještě nic nebylo. Vždy jako běžný tab projektu —
        slash příkaz z otevření se podruhé nespouští."""
        chat = s.uspany_chat
        script, cwd, env = self._command_for("project", s.path, s.agent, s.model,
                                             resume=chat, vault=s.vault)
        env = {**env, "HUB_TAB": core.tab_tag(s.id)}
        s.relaunch(core.bash_argv(script), cwd, env)
        core.log(f"tab {s.id}: probuzený" + (f" (pokračuje v {chat[:8]})" if chat else ""))

    def prenacti_chaty(self, cekat=6 * 3600):
        """Nové přihlášení Clauda (hub/predplatne.py) i do už běžících chatů:
        každý tab s Claudem se spustí znovu nad toutéž konverzací. Kdo zrovna
        pracuje (uprostřed tahu, úloha na pozadí), dojede na starém a přepne
        se, až skončí — rozdělanou práci to nepřeruší."""
        from . import cteni

        def zaneprazdneny(s):
            cesta = core.transcript_for(s)
            od_ms = int(s.started * 1000)
            return bool(cesta and (restart._uprostred_prace(cesta, od_ms) or cteni.pozadi(cesta, od_ms)))

        def jed():
            # Uspané taby ne: probudí se rovnou s novým přihlášením.
            cekaji = [s for s in list(self.sessions.values())
                      if not s.exited and not s.uspany
                      and (s.kind == "project" or s.kind.startswith("slash:"))
                      and (s.agent or core.default_agent()) == "claude"]
            konec = time.time() + cekat
            while cekaji and time.time() < konec:
                zbyva = []
                for s in cekaji:
                    if s.exited or s.uspany or self.sessions.get(s.id) is not s:
                        continue
                    try:
                        if zaneprazdneny(s):
                            zbyva.append(s)
                            continue
                        chat = restart._resume_for(self, s)
                        if not chat:
                            continue
                        meta = pocitac.ukol_for_tab("", chat)
                        launch = pocitac.tab_launch(meta) if meta else None
                        script, cwd, env = self._command_for("project", s.path, s.agent, s.model,
                                                             resume=chat, vault=s.vault,
                                                             launch=launch)
                        env = {**env, "HUB_TAB": core.tab_tag(s.id)}
                        s.relaunch(core.bash_argv(script), cwd, env)
                        if launch:
                            pocitac.bind_tab(launch, s, meta)
                        core.log(f"tab {s.id}: Claude s novým přihlášením (pokračuje v {chat[:8]})")
                    except Exception as exc:
                        core.log_error("přenačtení chatu s novým přihlášením", exc)
                cekaji = zbyva
                if cekaji:
                    time.sleep(10)
        threading.Thread(target=jed, daemon=True).start()

    def close(self, sid, autosave=True):
        session = self.sessions.pop(sid, None)
        if session:
            session.close()
            if autosave:
                core.autosave_tabs_closed([sid])

    def shutdown(self, keep=True):
        """Zavře všechny taby. `keep` = nejdřív je uložit, ať se po dalším
        otevření appky vrátí (restart.take) — kromě restartu po aktualizaci,
        ten si je uložil sám."""
        if keep and not core.on_gateway():
            # V prostoru na serveru ukládá brána přes /api/prostor-snapshot
            # (uspani), dřív než prostor zastaví — tady by Claude už nežil.
            try:
                restart.snapshot(self, "zavreni")
            except Exception as exc:
                core.log_error("uložení tabů při zavření", exc)
        sids = list(self.sessions)
        for sid in sids:
            self.close(sid, autosave=False)
        core.autosave_tabs_closed(sids)      # jeden proces na všechny taby

    def broadcast(self, message, skip=None):
        for conn in list(self.conns):
            if conn is skip:
                continue
            try:
                conn.send_json(message)
            except Exception:
                pass


HUB = Hub()


# ── HTTP ─────────────────────────────────────────────────────────────────────
# Statické soubory se posílají zkomprimované a s ETagem. Dřív šlo při každém
# otevření 1,5 MB bez komprese a bez cache — na počítači to nevadí, ale telefon
# přes Tailscale nebo bránu na tom čekal. `no-cache` = prohlížeč si soubor
# nechá, ale před použitím se zeptá; nezměněný soubor je pak jen 304 bez těla
# a po aktualizaci hubu se nový stáhne hned (ETag je z mtime a velikosti).
_GZIP_TYPES = ("text/", "application/javascript", "application/json",
               "image/svg+xml", "application/manifest+json")
_GZIP_MIN = 1024
_GZIP_CACHE = {}          # cesta → (mtime_ns, velikost, zkomprimované tělo)
_GZIP_LOCK = threading.Lock()


def _gzipped(full, st, body):
    key = (st.st_mtime_ns, st.st_size)
    with _GZIP_LOCK:
        hit = _GZIP_CACHE.get(full)
    if hit and hit[:2] == key:
        return hit[2]
    packed = gzip.compress(body, compresslevel=6, mtime=0)
    with _GZIP_LOCK:
        _GZIP_CACHE[full] = key + (packed,)
    return packed


def _verze_souboru(st):
    return f"{st.st_mtime_ns:x}{st.st_size:x}"


_ODKAZ = re.compile(rb'((?:src|href)=")/([A-Za-z0-9_./-]+\.(?:js|css))"')


def _verzuj(page):
    """Skripty a styly v index.html dostanou `?v=<verze souboru>`. Změněný
    soubor = nová adresa, takže je prohlížeč smí držet natrvalo bez ptaní."""
    def nahrad(m):
        rel = posixpath.normpath("/" + m.group(2).decode()).lstrip("/")
        try:
            st = os.stat(os.path.join(STATIC_DIR, *rel.split("/")))
        except OSError:
            return m.group(0)
        return m.group(1) + b"/" + m.group(2) + b"?v=" + _verze_souboru(st).encode() + b'"'
    return _ODKAZ.sub(nahrad, page)


class Handler(BaseHTTPRequestHandler):
    server_version = "ClaudeCodeHub"
    protocol_version = "HTTP/1.1"

    # ---- helpers ----
    def log_message(self, *_args):
        pass  # the terminal belongs to the app, not to request logging

    @property
    def token(self):
        return self.server.token

    def _cookie_token(self):
        """Token left behind by pairing, so the phone's icon opens straight in."""
        for part in self.headers.get("Cookie", "").split(";"):
            name, _, value = part.strip().partition("=")
            if name == COOKIE_NAME:
                return urllib.parse.unquote(value)
        return ""

    def _authorised(self, query):
        given = (query.get("t", [""])[0]
                 or self.headers.get("X-Hub-Token", "")
                 or self._cookie_token())
        return secrets.compare_digest(given, self.token)

    def _secure(self):
        return self.headers.get("X-Forwarded-Proto", "") == "https"

    def _origin_ok(self):
        """Reject an Origin that is not this very page.

        Behind `tailscale serve` the page is https://<stroj>.ts.net while we
        listen on loopback, so the fixed loopback URL is no longer the whole
        story: the addresses the listener was started for are kept on the
        server object, and the request's own Host header is accepted too.
        """
        origin = self.headers.get("Origin", "")
        if not origin:
            return True
        if origin in self.server.origins:
            return True
        host = self.headers.get("X-Forwarded-Host") or self.headers.get("Host", "")
        return bool(host) and urllib.parse.urlsplit(origin).netloc == host

    def _pair_cookie(self):
        """Set-Cookie value that keeps this phone paired."""
        flags = "; Secure" if self._secure() else ""
        return (f"{COOKIE_NAME}={urllib.parse.quote(self.token)}; Path=/; "
                f"Max-Age={COOKIE_MAX_AGE}; SameSite=Lax; HttpOnly{flags}")

    def _send(self, code, body=b"", ctype="text/plain; charset=utf-8", extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        # Vlastní Cache-Control v `extra` má přednost — dřív odcházely oba.
        if not any(k.lower() == "cache-control" for k in (extra or {})):
            self.send_header("Cache-Control", "no-store")
        # Obrana do hloubky: nic se nehádá podle obsahu, cizí stránka hub
        # nezarámuje (clickjacking) a adresa s tokenem neodejde v Refereru.
        for k, v in (("X-Content-Type-Options", "nosniff"), ("X-Frame-Options", "SAMEORIGIN"),
                     ("Referrer-Policy", "same-origin")):
            if not any(x.lower() == k.lower() for x in (extra or {})):
                self.send_header(k, v)
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        extra = None
        # Dlouhá konverzace ve čtení má stovky kB — na telefonu je to znát.
        # Kdo gzip neohlásí (brána, urllib), dostane data jako dřív.
        if len(body) >= 4096 and "gzip" in self.headers.get("Accept-Encoding", ""):
            body = gzip.compress(body, compresslevel=5, mtime=0)
            extra = {"Content-Encoding": "gzip", "Vary": "Accept-Encoding"}
        self._send(code, body, "application/json; charset=utf-8", extra)

    # ---- routes ----
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed.query)
        route = parsed.path

        if route == "/ws":
            return self._websocket(query)
        if route == "/povoleni":
            return self._povoleni(query)
        if route == "/":
            if not self._authorised(query):
                if self.server.remote:
                    return self._send(403, PAIR_AGAIN.encode("utf-8"),
                                      "text/html; charset=utf-8")
                return self._send(403, b"Neplatny token.")
            extra = {"Set-Cookie": self._pair_cookie()} if (
                self.server.remote and query.get("t")) else None
            return self._static("index.html", extra)
        if route.startswith("/api/"):
            if not self._authorised(query):
                return self._send(403, b"Neplatny token.")
            return self._api(route[5:], query)
        # Vlastní název a ikona appky (hub/vzhled.py). Manifest a ikony si
        # prohlížeč bere bez tokenu — nic tajného v nich není.
        if route == "/manifest.webmanifest":
            body = json.dumps(vzhled.manifest(vzhled.zobrazeny(), "/app-ikona/",
                                              vzhled.verze()), ensure_ascii=False)
            return self._send(200, body.encode("utf-8"), "application/manifest+json",
                              {"Cache-Control": "no-cache"})
        m = re.fullmatch(r"/app-ikona/(512|192|32)\.png", route)
        if m:
            with open(vzhled.ikona_nebo_vychozi(int(m.group(1))), "rb") as fh:
                return self._send(200, fh.read(), "image/png",
                                  {"Cache-Control": "public, max-age=86400"})
        return self._static(route.lstrip("/"))

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed.query)
        if parsed.path == "/povoleni":
            return self._povoleni(query)
        if parsed.path == "/sdilet":
            # Sdílení z jiné appky v telefonu (share_target v manifestu) chytá
            # už v telefonu service worker (sw.js). Sem dojde, jen když ho
            # prohlížeč zrovna nemá (smazaná data webu) — tělo se nečte,
            # stránka jen řekne, že to nedošlo, a worker se znovu zaregistruje.
            self.close_connection = True
            return self._send(303, b"", extra={"Location": "/?sdileno=chyba"})
        if not parsed.path.startswith("/api/") or not self._authorised(query):
            return self._send(403, b"Neplatny token.")
        # Změny jen ze stránky hubu. Stránka prostoru na serveru token hubu na
        # počítači zná (`#local=`, cesta zpátky) — bez tohohle by si server
        # mohl sám zapnout třeba přístup Clauda na tenhle počítač.
        if not self._origin_ok():
            return self._send(403, b"Cizi puvod.")
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if length < 0:                                # záporná délka by čtení nechala viset do zavření spojení
            self.close_connection = True
            return self._json({"ok": False, "error": "Neplatná délka požadavku."}, 400)
        if parsed.path == "/api/vault-upload":
            # Soubor do osobního trezoru: tělo je soubor sám, ne JSON.
            # Velikost se hlídá dřív, než se cokoli přečte.
            if length > core.VAULT_UPLOAD_MAX:
                self.close_connection = True
                return self._json({"ok": False, "error": "Soubor je větší než "
                                   f"{core.VAULT_UPLOAD_MAX // (1024 * 1024)} MB."}, 413)
            raw = self.rfile.read(length) if length > 0 else b""
            return self._api("vault-upload", query, {"raw": raw})
        # Největší JSON tělo je nahraný soubor v base64 (core.MAX_UPLOAD) — víc se do paměti nečte.
        if length > core.MAX_UPLOAD * 4 // 3 + 1024 * 1024:
            self.close_connection = True
            return self._json({"ok": False, "error": "Požadavek je moc velký."}, 413)
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            payload = {}
        if not isinstance(payload, dict):             # `[]`, `5`, `"x"` — akce čtou jen objekty
            payload = {}
        return self._api(parsed.path[5:], query, payload)

    def _povoleni(self, query):
        """Hook PermissionRequest z tabu s úkolem ze serveru (tools/povoleni_hook.py).

        Bez tokenu hubu — jen se žetonem, který dostal ten jediný tab v prostředí.
        Žeton umí žádost ohlásit a počkat na odpověď, rozhodnout ne: to smí jen
        člověk (okno hubu, nebo karta v prostoru přes bránu). Jen na loopbacku."""
        if self.server.remote or self.headers.get("Origin"):
            return self._send(404, b"404")
        token = self.headers.get("X-Hub-Povoleni", "")
        if self.command == "POST":
            length = int(self.headers.get("Content-Length") or 0)
            if length > 1024 * 1024:
                return self._send(413, b"")
            try:
                event = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                return self._send(400, b"")
            rid = pocitac.povoleni_new(token, event)
            return self._json({"id": rid}) if rid else self._send(403, b"")
        try:
            wait = float(query.get("wait", ["25"])[0])
        except ValueError:
            wait = 25.0
        reply = pocitac.povoleni_wait(token, query.get("id", [""])[0], wait)
        return self._json(reply) if reply is not None else self._send(404, b"")

    def _static(self, relpath, extra=None):
        # posixpath.normpath + strip leading separators keeps this inside STATIC_DIR
        rel = posixpath.normpath("/" + relpath).lstrip("/")
        full = os.path.join(STATIC_DIR, *rel.split("/"))
        # Porovnání přes commonpath, ne startswith: to by pustilo i sourozence
        # jménem `static-cokoliv`, který do aplikace nepatří.
        full = os.path.abspath(full)
        try:
            inside = os.path.commonpath([full, STATIC_DIR]) == STATIC_DIR
        except ValueError:                       # jiný disk na Windows
            inside = False
        if not inside or not os.path.isfile(full):
            return self._send(404, b"404")
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript",):
            ctype += "; charset=utf-8"
        st = os.stat(full)
        extra = dict(extra or {})
        if rel != "index.html":
            # index.html se upravuje podle vzhledu (a nese cookie párování),
            # ten zůstává no-store. Zbytek se ověřuje ETagem — a co stránka
            # odkazuje s verzí (`?v=`, viz _verzuj), si prohlížeč nechá
            # natrvalo a vůbec se neptá: dřív každé otevření okna znamenalo
            # 28 dotazů „změnilo se?", přes bránu po síti i 300 ms každý.
            etag = f'"{st.st_mtime_ns:x}-{st.st_size:x}"'
            extra["ETag"] = etag
            asked = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get("v", [""])[0]
            extra["Cache-Control"] = ("public, max-age=31536000, immutable"
                                      if asked and asked == _verze_souboru(st) else "no-cache")
            if self.headers.get("If-None-Match") == etag:
                self.send_response(304)
                self.send_header("ETag", etag)
                self.send_header("Cache-Control", extra["Cache-Control"])
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
        with open(full, "rb") as fh:
            body = fh.read()
        if rel == "index.html":
            # Vlastní název a odkazy na vlastní manifest a ikonu (hub/vzhled.py).
            try:
                body = vzhled.uprav_stranku(body)
            except Exception as exc:
                core.log_error("vlastní vzhled stránky", exc)
            if core.on_gateway():
                # Prostor na serveru je modrý od první vteřiny — JS (applyPlace)
                # to jinak dopíše až po načtení stavu a stránka by chvíli
                # blikala jantarově.
                body = body.replace(b'<html lang="cs">', b'<html lang="cs" data-place="server">', 1)
            body = _verzuj(body)
        if (len(body) >= _GZIP_MIN and ctype.startswith(_GZIP_TYPES)
                and "gzip" in self.headers.get("Accept-Encoding", "")):
            body = (gzip.compress(body, compresslevel=6, mtime=0)
                    if rel == "index.html" else _gzipped(full, st, body))
            extra["Content-Encoding"] = "gzip"
            extra["Vary"] = "Accept-Encoding"
        self._send(200, body, ctype, extra)

    def _api(self, name, query, payload=None):
        try:
            return self._api_inner(name, query, payload)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            raise                                   # klient odešel — řeší handle_error, není to chyba akce
        except Exception as exc:
            # Bez tohohle by chyba skončila jen ve stacktrace, který nikdo
            # nevidí — okno na Windows nemá konzoli.
            core.log_error(f"/api/{name} selhalo", exc)
            return self._json({"error": f"Chyba serveru: {exc}"}, 500)

    def _hledani(self, name, query, payload):
        """Hledání v konverzacích (běží na pozadí, tady se jen zahájí / zeptá na stav)
        a stažení konverzace jako .md."""
        if not isinstance(payload, dict):
            payload = {}
        if name == "chat-hledej-zrus":
            hledani.zrus()
            return self._json({"ok": True})
        if name == "chat-hledej":
            if self.command != "POST":
                return self._json({"error": "Jen POST."}, 405)
            try:
                return self._json(dict(hledani.spust(payload.get("q"), payload.get("nastroje") is True), ok=True))
            except ValueError as exc:
                return self._json({"error": str(exc)}, 400)
        if name == "chat-hledej-stav":
            snimek = hledani.stav(payload.get("job"))
            if snimek is None:
                return self._json({"ok": False, "nahrazeno": True})
            bezi = HUB.chat_tabs()
            for r in snimek["vysledky"]:
                r["tab"] = bezi.get(r["id"])
            return self._json(dict(snimek, ok=True))
        # chat-export: soubor ke stažení jako příloha, stejně jako /api/soubor
        chat_id = query.get("id", [""])[0]
        if not cteni.je_id(chat_id):
            return self._json({"error": "Neplatná konverzace."}, 400)
        if not hledani.export_povolen():
            return self._json({"error": "Právě se exportuje jiná konverzace, zkus to za chvíli."}, 429)
        try:
            fname, data = hledani.export_md(chat_id)
        except ValueError as exc:
            return self._json({"error": str(exc)}, 404)
        except Exception as exc:
            core.log_error("export konverzace", exc)
            return self._json({"error": "Export se nepovedl."}, 500)
        finally:
            hledani.export_hotov()
        ascii_name = re.sub(r'[^A-Za-z0-9._-]+', "_", fname) or "konverzace.md"
        return self._send(200, data, "text/markdown; charset=utf-8", {
            "Content-Disposition": f"attachment; filename=\"{ascii_name}\"; "
                                   f"filename*=UTF-8''{urllib.parse.quote(fname)}",
            "Content-Security-Policy": "default-src 'none'; sandbox"})

    def _zrcadlo(self, name, query, payload, guest):
        """Počítač počítá, kopie chatů je na serveru (hub/zrcadlo.py).

        Na počítači: stav kopírování a chaty na serveru (`zrcadlo`), převzetí
        chatu ze serveru (`zrcadlo-prevzit`), vypínač (`zrcadlo-nastav`).
        V prostoru: kde chaty z počítače běží (`zrcadla`), převzetí na server
        (`zrcadlo-na-server`) a `zrcadlo-uvolnit`, které volá jen brána."""
        from . import zrcadlo
        if guest:
            return self._json({"error": "Tohle smí jen vlastník prostoru."}, 403)
        try:
            if name == "zrcadlo":
                return self._json(zrcadlo.prehled(bool(query.get("obnovit"))))
            if name == "zrcadla":
                return self._json({"chaty": zrcadlo.stav_prostoru() if core.on_gateway() else {}})
            if not payload:
                return self._json({"error": "Jen POST."}, 405)
            if name == "zrcadlo-nastav":
                return self._json(zrcadlo.nastav(payload.get("zapnout")))
            if name == "zrcadlo-prevzit":
                return self._json(zrcadlo.prevzit(payload.get("chat")))
            if name == "zrcadlo-na-server":
                return self._json(zrcadlo.na_server(payload.get("chat")))
            # zrcadlo-uvolnit: počítač si bere chat, který běží tady.
            if not core.on_gateway():
                return self._json({"error": "Jen v prostoru na serveru."}, 400)
            ok = zrcadlo.uvolnit_v_prostoru(HUB, payload.get("chat"))
            return self._json({"ok": ok} if ok else {"error": "Claude v chatu pořád pracuje."})
        except ValueError as exc:
            return self._json({"error": str(exc)}, 400)

    def _soubor_ke_stazeni(self, query, guest):
        """Soubor z prostoru ke stažení (čtení → „Stáhnout").

        Jen z domova, složek projektů a složek otevřených tabů (core.
        soubor_ke_stazeni); host z jiného účtu jen z domova a projektů a bez
        skrytých složek. `chat` = volá brána za člověka ze sdíleného chatu:
        pak jen soubory, které Claude v tom chatu zapsal (cteni.soubory_chatu).
        `info=1` jen řekne, jestli soubor je a jak je velký (karta v chatu)."""
        path = query.get("path", [""])[0]
        chat = query.get("chat", [""])[0]
        prisne = bool(guest) or bool(chat)
        extra = () if prisne else [s.path for s in HUB.sessions.values()
                                   if s.path and not getattr(s, "exited", False)]
        full, velikost, proc = core.soubor_ke_stazeni(path, extra, prisne=prisne)
        if full and chat:
            prepis = cteni.path_for(chat) if cteni.je_id(chat) else ""
            povolene = {os.path.realpath(os.path.expanduser(p))
                        for p in (cteni.soubory_chatu(prepis) if prepis else ())}
            if full not in povolene:
                full, proc = "", "Tenhle soubor v chatu nevznikl."
        if query.get("info", [""])[0] == "1":
            return self._json({"ok": bool(full), "size": velikost if full else 0,
                               "name": os.path.basename(full or path), "error": proc})
        if not full:
            return self._json({"error": proc}, 404)
        fname = os.path.basename(full)
        ascii_name = re.sub(r'[^A-Za-z0-9._-]+', "_", fname) or "soubor"
        try:
            fh = open(full, "rb")
        except OSError:
            return self._json({"error": "Soubor nejde přečíst."}, 404)
        with fh:
            self.send_response(200)
            # Vždycky jako příloha: HTML ani SVG z prostoru se pod adresou
            # hubu nikdy neotevře jako stránka (stejně jako vault-file).
            for k, v in (("Content-Type", "application/octet-stream"),
                         ("Content-Length", str(velikost)),
                         ("Content-Disposition", f"attachment; filename=\"{ascii_name}\"; "
                                                 f"filename*=UTF-8''{urllib.parse.quote(fname)}"),
                         ("Content-Security-Policy", "default-src 'none'; sandbox"),
                         ("X-Content-Type-Options", "nosniff"), ("X-Frame-Options", "DENY"),
                         ("Referrer-Policy", "same-origin"), ("Cache-Control", "no-store")):
                self.send_header(k, v)
            self.end_headers()
            if self.command == "HEAD":
                return None
            try:
                while True:
                    kus = fh.read(256 * 1024)
                    if not kus:
                        break
                    self.wfile.write(kus)
            except (OSError, ValueError):
                self.close_connection = True       # prohlížeč stahování zrušil
        return None

    @staticmethod
    def _part(what, fn, default):
        """Kus /api/state, který se nepovedl, nesmí shodit celé okno — jinak se
        hub vůbec nenačte a nedá se ani přejít na server. Zaloguje se a místo
        něj přijde prázdno."""
        try:
            return fn()
        except Exception as exc:
            core.log_error(f"/api/state: {what} selhalo", exc)
            return default

    def _api_inner(self, name, query, payload=None):
        payload = payload or {}
        guest = guest_from(self.headers)
        if guest and guest.get("role") == "predplatne":
            # Správa předplatného do hubu nesmí vůbec — mluví jen s bránou
            # (/gw/pristup/predplatne), ta volá predplatne-host sama.
            return self._json({"error": "Nepovoleno."}, 403)
        if name == "predplatne-host":
            # Správa předplatného hostem (gateway/pristup.py). Volá jen brána
            # přímo, z prohlížeče se sem nedostane (brána cestu blokuje).
            if guest or not core.on_gateway():
                return self._json({"error": "Nepovoleno."}, 403)
            return self._json(predplatne.host_action(str(payload.get("action") or ""), payload,
                                                     HUB.broadcast, HUB.prenacti_chaty))
        if name == "web-host":
            # Web (FTP), který účtu přidělil správce (hub/web.py). Stejně jako
            # predplatne-host volá jen brána přímo, z prohlížeče se sem nedostane.
            if guest or not core.on_gateway():
                return self._json({"error": "Nepovoleno."}, 403)
            from . import web
            return self._json(web.host_action(payload))
        if name == "app-vzhled":
            # Název a ikona appky (hub/vzhled.py) — jen ke čtení.
            if self.command != "POST":
                return self._json(vzhled.stav())
            # Název i ikona jsou pro všechny stejné (Aladin) — měnit nejdou.
            return self._json({"error": "Název a ikona appky se nemění."}, 403)
        if name.startswith("hlas"):
            # Diktování a předčítání (hub/hlas.py) — česky, na tomhle stroji.
            from . import eleven, hlas
            if name == "hlas":
                st = hlas.state()
                el = eleven.status()
                return self._json({**st, "job": core.job_state("hlas"),
                                   "prepis": hlas.dostupny(), "eleven": el,
                                   # Předčítat jde místním hlasem, nebo přes ElevenLabs.
                                   "cteni": st["ready"] or el["active"]})
            if name == "hlas-proud":
                # Zvuk z ElevenLabs proudem (hub/eleven.py): <audio> začne hrát
                # s prvními bajty, nečeká se na celý kus.
                try:
                    up = eleven.proud((query.get("id") or [""])[0])
                except (ValueError, RuntimeError) as exc:
                    return self._json({"error": str(exc)}, 400)
                self.send_response(200)
                self.send_header("Content-Type", "audio/mpeg")
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Connection", "close")
                self.send_header("X-Accel-Buffering", "no")    # nginx před bránou
                self.end_headers()
                self.close_connection = True
                try:
                    with up:
                        while True:
                            kus = up.read1(8192) if hasattr(up, "read1") else up.read(8192)
                            if not kus:
                                break
                            self.wfile.write(kus)
                            self.wfile.flush()
                except (OSError, ValueError):
                    pass                 # prohlížeč přestal poslouchat (stop)
                return None
            if name == "hlas-eleven":
                # Napojení ElevenLabs (hub/eleven.py): klíč se jen ukládá,
                # do prohlížeče jde zpátky nanejvýš jeho konec.
                if self.command != "POST":
                    return self._json(eleven.nabidka(fresh=query.get("fresh", [""])[0] == "1"))
                act = payload.get("action")
                try:
                    if act == "save":
                        return self._json(eleven.save(payload.get("api_key")))
                    if act == "vyber":
                        return self._json(eleven.vyber(
                            payload.get("voice_id"), payload.get("model_id"),
                            payload.get("active") if isinstance(payload.get("active"), bool) else None))
                    if act == "remove":
                        return self._json(eleven.remove())
                except RuntimeError as exc:
                    return self._json({"ok": False, "error": str(exc)})
                return self._json({"error": "Neznámá akce."}, 400)
            if self.command != "POST":
                return self._json({"error": "Jen POST."}, 405)
            if name == "hlas-install":
                return self._json({"ok": hlas.start_install()})
            if name == "hlas-pripravit":
                # Předčítání bez čekání: ElevenLabs → id pro hlas-proud. Bez
                # ElevenLabs (nebo při chybě) {"proud": false} a čte se postaru.
                text = str(payload.get("text") or "").strip()[:hlas.MAX_TEXT]
                voice = str(payload.get("voice_id") or "")
                if text and payload.get("engine") != "piper" and (voice or eleven.aktivni()):
                    try:
                        return self._json({"proud": True, "id": eleven.priprav(
                            text, voice, str(payload.get("model_id") or ""))})
                    except (ValueError, RuntimeError) as exc:
                        return self._json({"proud": False, "error": str(exc)})
                return self._json({"proud": False})
            try:
                if name == "hlas-prepis":
                    wav = base64.b64decode(str(payload.get("wav") or ""), validate=False)
                    return self._json({"text": hlas.prepis(wav)})
                if name == "hlas-rec":
                    text = str(payload.get("text") or "").strip()[:hlas.MAX_TEXT]
                    if not text:
                        raise ValueError("Není co přečíst.")
                    voice = str(payload.get("voice_id") or "")
                    if payload.get("engine") != "piper" and (voice or eleven.aktivni()):
                        try:
                            return self._send(200, eleven.rec(text, voice, str(
                                payload.get("model_id") or "")), "audio/mpeg")
                        except RuntimeError as exc:
                            # Ukázka konkrétního hlasu chybu ukáže; běžné čtení
                            # přejde na místní hlas, když tu je.
                            if voice or not hlas.root():
                                raise
                            core.log(f"hlas: ElevenLabs selhal ({exc}), čtu místním hlasem", "warn")
                    return self._send(200, hlas.rec(text), "audio/wav")
            except (ValueError, RuntimeError) as exc:
                return self._json({"error": str(exc)}, 400)
            except subprocess.TimeoutExpired:
                return self._json({"error": "Hlas to nestihl — zkus kratší kus."}, 504)
            return self._json({"error": "Neznámý požadavek."}, 404)
        if name == "mcp-tool":
            # Napojení z appky Claude: volá jen brána (gateway/mcp.py) po
            # ověření přihlášení a oprávnění. Z prohlížeče sem brána nepustí.
            if payload.get("tool") is None:
                return self._json({"error": "Jen POST."}, 405)
            from . import mcp_tools
            return self._json(mcp_tools.call(str(payload.get("tool")), payload.get("args")))
        if name == "chats":
            # Seznam konverzací jako v oficiální appce (hub/chats.py) a které
            # z nich právě běží v tabu — ty se neotevírají podruhé.
            running = HUB.chat_tabs()
            seznam = [dict(c, tab=running.get(c["id"])) for c in chats.list_chats()]
            # Seznam má stovky kB a mění se zřídka: okno pošle otisk toho, co
            # už má, a když sedí, dostane jen „beze změny".
            import hashlib
            otisk = hashlib.sha1(json.dumps(seznam, sort_keys=True).encode("utf-8")).hexdigest()[:16]
            if query.get("h", [""])[0] == otisk:
                return self._json({"same": True, "h": otisk})
            return self._json({"chats": seznam, "h": otisk})
        if name in ("chat-hledej", "chat-hledej-stav", "chat-hledej-zrus", "chat-export"):
            # Hledání napříč konverzacemi a export do Markdownu (hub/hledani.py).
            # Cizí historii host nečte.
            if guest:
                return self._json({"error": "Historii konverzací smí číst jen vlastník prostoru."}, 403)
            return self._hledani(name, query, payload)
        if name == "chat-delete":
            # Smazání konverzace z panelu Chaty (do koše, hub/chats.py). Jen
            # vlastník prostoru — host cizí historii nemaže. Běžící v tabu ne:
            # Claude Code by přepis hned založil znovu.
            if self.command != "POST":
                return self._json({"error": "Jen POST."}, 405)
            if guest:
                return self._json({"error": "Konverzace smí mazat jen vlastník prostoru."}, 403)
            chat_id = str(payload.get("id") or "")
            if chat_id in HUB.chat_tabs():
                return self._json({"error": "Konverzace je otevřená v tabu — nejdřív ho zavři."}, 409)
            try:
                moved = chats.delete_chat(chat_id)
            except ValueError as exc:
                return self._json({"error": str(exc)}, 400)
            except OSError as exc:
                core.log_error("chaty: smazání", exc)
                return self._json({"error": f"Nepovedlo se smazat: {exc}"}, 500)
            if not moved:
                return self._json({"error": "Konverzace už neexistuje."}, 404)
            return self._json({"ok": True})
        if name == "chat-vetev":
            # Větvení konverzace (hub/vetev.py): nový přepis jen do zvoleného místa,
            # původní se nemění. Zdroj = konverzace podle id (`chat`) nebo tab (`id`).
            if self.command != "POST":
                return self._json({"error": "Jen POST."}, 405)
            if guest:
                return self._json({"error": "Konverzaci smí větvit jen vlastník prostoru."}, 403)
            chat = str(payload.get("chat") or "")
            if chat:
                path = cteni.path_for(chat)
            else:
                try:
                    session = HUB.sessions.get(int(payload.get("id") or 0))
                except (TypeError, ValueError):
                    session = None
                path = core.transcript_for(session) if session else ""
            try:
                return self._json(dict(vetev.rozvetvit(path, str(payload.get("uuid") or "")), ok=True))
            except vetev.VetevChyba as exc:
                return self._json({"error": str(exc)}, 409)
            except Exception as exc:
                core.log_error("chat-vetev", exc)
                return self._json({"error": f"Větev se nepodařilo vytvořit: {exc}"}, 500)
        if name == "bypass":
            # Potvrzení varování k bypassu, které by se jinak Claude Code ptal
            # v každém novém tabu. Jen výslovně (POST s accept: true).
            if payload.get("accept") is not True:
                return self._json({"error": "Chybí potvrzení."}, 400)
            try:
                core.accept_bypass()
            except ValueError as exc:
                return self._json({"error": str(exc)}, 400)
            return self._json({"ok": True})
        if name == "stopka":
            # Týdenní limit od správce a stopka (hub/stopka.py) — pruh nahoře v okně.
            from . import stopka
            return self._json(stopka.stav(cerstve=query.get("fresh", [""])[0] == "1"))
        if name.startswith("fronta"):
            # Fronta zpráv na serveru (hub/fronta.py): uložit, doručit, smazat.
            try:
                session = HUB.sessions.get(int(str(payload.get("tab") or (query.get("tab") or ["0"])[0])))
            except ValueError:
                session = None
            chat = fronta.chat_tabu(session) if session and not session.exited else ""
            if not chat:
                return self._json({"error": "Tab nemá konverzaci, do které by zpráva šla."}, 409)
            if name == "fronta":
                if self.command == "POST":
                    # Stopka od správce (hub/stopka.py): zpráva Claudovi neodejde,
                    # okno řekne proč a do kdy (a nepošle ji jinou cestou).
                    from . import stopka
                    st = stopka.zablokovano()
                    if st:
                        return self._json({"ok": False, "stopka": st, "error": st["text"]})
                    try:
                        return self._json({"ok": True, "zprava": fronta.pridej(chat, payload.get("text"), payload.get("id"))})
                    except ValueError as exc:
                        return self._json({"error": str(exc)}, 400)
                return self._json({"zpravy": fronta.seznam(chat), "pauza": fronta.pauza(chat)})
            if name == "fronta-znovu":
                return self._json({"ok": fronta.znovu(chat, str(payload.get("id") or ""))})
            if name == "fronta-ihned":
                return self._json({"ok": fronta.ihned(chat, str(payload.get("id") or ""))})
            if name == "fronta-zrus":
                return self._json(fronta.smaz(chat, str(payload.get("id") or "")))
            if name == "fronta-uprav":
                return self._json(fronta.uprav(chat, str(payload.get("id") or ""), payload.get("text")))
            if name == "fronta-pauza":
                return self._json({"ok": True, "pauza": fronta.pauza(chat, bool(payload.get("on")))})
        if name == "cteni-pozadi":
            # Úlohy, které Claude v tabu pustil na pozadí a ještě běží.
            try:
                session = HUB.sessions.get(int((query.get("id") or ["0"])[0]))
            except ValueError:
                session = None
            if not session or session.exited:
                return self._json({"tasks": [], "now": int(time.time() * 1000)})
            return self._json({"tasks": cteni.pozadi(core.transcript_for(session),
                                                     int(session.started * 1000)),
                               "now": int(time.time() * 1000)})
        if name == "stav-radek":
            # Model, větev, odhad ceny a start tabu (hub/stavradek.py).
            try:
                session = HUB.sessions.get(int((query.get("id") or ["0"])[0]))
            except ValueError:
                session = None
            return self._json(stavradek.zjisti(session))
        if name in ("cteni", "cteni-agenti"):
            # Konverzace jako text (hub/cteni.py). Buď z tabu, který běží,
            # nebo ze starého přepisu podle id konverzace.
            chat = (query.get("chat") or [""])[0]
            if chat:
                path = cteni.path_for(chat)
            else:
                try:
                    session = HUB.sessions.get(int((query.get("id") or ["0"])[0]))
                except ValueError:
                    session = None
                path = core.transcript_for(session) if session else ""
            if name == "cteni-agenti":
                # Co dělají agenti, které Claude pustil — z jejich přepisů.
                return self._json(cteni.agenti(path))
            try:
                start = int((query.get("from") or ["0"])[0] or 0)
            except ValueError:
                start = 0
            # Přepis se mohl přestěhovat (obnovený tab, core._pokracovani):
            # pozice patří starému souboru, v novém se naváže za poslední
            # zprávu, kterou čtení už ukázalo.
            src = (query.get("src") or [""])[0]
            here = os.path.basename(path)[:-6] if path else ""
            if start and src and here and src != here and cteni.je_id(src):
                start = cteni.navaz(os.path.join(os.path.dirname(path), src + ".jsonl"),
                                    start, path)
            out = cteni.read(path, start, tail=not start)
            out["src"] = here
            return self._json(out)
        if name.startswith("rewind-"):
            # Vrácení souborů k zprávě (hub/rewind.py) — hostům ne.
            if guest:
                return self._json({"error": "Nepovoleno."}, 403)
            if self.command != "POST":
                return self._json({"error": "Jen POST."}, 405)
            out = rewind.api(name, payload)
            return self._json(out, 400 if out.get("error") else 200)
        if name == "auto-model":
            # Druhý názor na model a effort ke zprávě (hub/automodel.py).
            text = payload.get("text") if isinstance(payload, dict) else ""
            return self._json(automodel.classify(text))
        if name == "auto-model-keep":
            # Automatické přepnutí jen pro tab — výchozí v settings.json se vrátí.
            extend = bool(payload.get("extend")) if isinstance(payload, dict) else False
            return self._json(automodel.keep_default(extend))
        if name == "tab-model":
            try:
                session = HUB.sessions.get(int((query.get("id") or ["0"])[0]))
            except ValueError:
                session = None
            if not session:
                return self._json({"model": "", "label": "", "at": ""})
            return self._json(core.tab_model(session))
        if name == "soubory-hledani":
            # Našeptávač `@soubor` v bublině (hub/soubory_hledani.py). Kořen se
            # bere z tabu na serveru, od klienta chodí jen dotaz.
            try:
                session = HUB.sessions.get(int((query.get("id") or ["0"])[0]))
            except ValueError:
                session = None
            koren = soubory_hledani.koren_tabu(session.path or core.HOME) if session else None
            if not koren:
                return self._json({"items": [], "cut": False})
            return self._json(soubory_hledani.hledej(koren, (query.get("q") or [""])[0]))
        if name == "tab-context":
            # Kolik kontextového okna zabírá konverzace v tabu (hub/kontext.py).
            try:
                session = HUB.sessions.get(int((query.get("id") or ["0"])[0]))
            except ValueError:
                session = None
            if not session:
                return self._json(kontext.stav(""))
            try:
                transcript = core.transcript_for(session)
                nastaveni = "[1m]" in core.current_model().lower()
            except Exception:
                return self._json(kontext.stav(""))
            return self._json(kontext.stav(transcript, (query.get("m1") or [""])[0] == "1",
                                           nastaveni))
        if name == "setup":
            # Instalace v okně (hub/setup.py): bez akce jen průběh.
            action = payload.get("action") if isinstance(payload, dict) else ""
            if action == "launch":
                # Hotovo: pustit nainstalovanou appku a tuhle ukončit.
                if not setup.state().get("installed"):
                    return self._json({"error": "Appka ještě není nainstalovaná."}, 400)
                return self._json(restart.relaunch(HUB))
            if action == "start":
                return self._json(setup.start())
            return self._json(setup.state())
        if name == "state":
            if query.get("fresh"):
                core.zahod_cache()           # tlačítko Načíst znovu
            part = self._part
            counts, recent = core.get_memory()
            projects = part("projekty", core.get_projects, [])
            return self._json({
                "projects": projects,
                "memory": {"counts": counts, "recent": recent,
                           "enabled": core.HAS_BRAIN},
                "skills": part("skilly", core.installed_skills, []),
                # Bez verzí (ty stojí spuštění každého CLI) — na nabídku
                # „otevřít v…" a odznak na tabu stačí jméno, barva a PATH.
                "agents": core.agents.detect(core._agents_extra(),
                                             with_version=False),
                "default_agent": core.default_agent(),
                "model": core.current_model(),
                "palette": {"dark": core.DARK, "light": core.LIGHT},
                # Zapnutý motiv (hub/motivy.py): přepsaná paleta a styl navíc.
                "motiv": part("motiv", motivy.data, {"id": ""}),
                "doctor": core.doctor(),
                "user": os.environ.get("USER") or os.environ.get("USERNAME") or "",
                "obsidian": core.has_obsidian(),
                "firma": part("firemní Obsidian", core.firma_state, {"vault": "", "name": ""}),
                "shared": part("sdílené Obsidiany", core.shared_state, []),
                "onboarded": bool(core.CONFIG.get("onboarded")),
                # Hub puštěný instalačkou ke stažení: místo tabů instalace.
                "setup": setup.ACTIVE,
                # Vlastní název a ikona appky (hub/vzhled.py).
                "app": part("vzhled appky", vzhled.stav, {}),
                "config": {"project_dirs": core.CONFIG.get("project_dirs") or [],
                           "brain_dir": core.CONFIG.get("brain_dir") or "",
                           "newtab": core.CONFIG.get("newtab") or {},
                           "show_archived": bool(core.CONFIG.get("show_archived")),
                           "dev_mode": bool(core.CONFIG.get("dev_mode")),
                           # Co je v panelu vlevo (hub.js, syncPanelNav).
                           "panel": core.CONFIG.get("panel") or {},
                           # Agent pro nový chat: "" = zeptat se (když jich je víc), jinak vždy tenhle (hub.js, newChatHere).
                           "novy_chat_agent": core.CONFIG.get("novy_chat_agent") or "",
                           # Kopie chatů na server (hub/zrcadlo.py), výchozí zapnuto.
                           "zrcadlo": core.CONFIG.get("zrcadlo", True) is not False,
                           "server_napojeni": core.CONFIG.get("server_napojeni", True) is not False,
                           # Téma drží hub, ne jen prohlížeč: port hubu se mění
                           # s každým spuštěním a s ním i paměť prohlížeče.
                           "theme": core.CONFIG.get("theme") or "",
                           # Poloha a stav okna s prohlížečem — drží hub, ne
                           # localStorage (port hubu se mění s každým startem).
                           "prohlizec_okno": core.CONFIG.get("prohlizec_okno") or {},
                           "auto_update": core.CONFIG.get("auto_update", True) is not False and not core.TEST_MODE,
                           "na_pozadi": bool(core.CONFIG.get("na_pozadi")),
                           "bez_spanku": bool(core.CONFIG.get("bez_spanku")),
                           "tray": core.CONFIG.get("tray", True) is not False,
                           # Server, na kterém má appka účet, a jestli se má
                           # otevírat rovnou tam. Token sem nepatří — stránka
                           # ho nepotřebuje a /api/state se kreslí všude.
                           "gw_server": core.CONFIG.get("gw_server") or "",
                           "gw_email": (core.CONFIG.get("gw_user") or {}).get("email", ""),
                           "gw_logged_in": bool(core.CONFIG.get("gw_token")),
                           "server_mode": bool(core.CONFIG.get("server_mode")),
                           # Claude ze serveru na tomhle počítači (hub/pocitac.py).
                           "pocitac_access": pocitac.access_level(),
                           "pocitac_asked": bool(core.CONFIG.get("pocitac_asked")),
                           # Claude v prostoru na vlastním předplatném (hub/predplatne.py).
                           "predplatne_skip": bool(core.CONFIG.get("predplatne_skip")),
                           # Tlačítko Nahlásit chybu / Podat návrh (verxee.js).
                           # Klíč je veřejný (stejně skončí ve stránce), bez
                           # něj se tlačítko neukáže.
                           "verxee_debug_klic": core.CONFIG.get("verxee_debug_klic") or "",
                           # Vyplněné jen na instanci běžící na bráně — podle
                           # toho nastavení pozná, že je na serveru.
                           "gateway_user": core.CONFIG.get("gateway_user") or None},
                "cloud": part("cloudové složky", core.cloud_folders, []),
                "vaults": part("vaulty", core.obsidian_vaults, []),
                "memory_link": core.memory_link_path(),
                "suggest_dirs": part("návrhy složek", core.suggest_project_dirs, []),
                "home": core.HOME,
                "version": core.version_info(),
                "vault_git": dict(zip(("is_repo", "remote"),
                                      part("git trezoru", core.vault_git_state, (False, "")))),
                "vault_autosync": bool(core.CONFIG.get("vault_autosync")),
                "memory_autosave": core.autosave_enabled(),
                "autosave_recent": part("poslední uložení", core.autosave_recent, []),
                # Host z jiného účtu (gateway/pristup.py): stránka ukáže pruh
                # „Prostor: … · role“ a tlačítko zpátky do svého.
                "guest": guest_from(self.headers),
            })
        # Náhled trezoru Obsidian (vault.js) — jen ke čtení, jen uvnitř trezoru.
        # `vault=firma` je společný firemní Obsidian (na bráně), jinak osobní.
        asked = query.get("vault", [""])[0]
        which = asked if (asked == "firma" or re.fullmatch(r"sdilene:[a-z0-9][a-z0-9-]{0,49}", asked)
                          or core.is_firma_vault(asked)) else ""
        if name == "firma-klienti":
            # Klienti a podkategorie firmy, jak je prostor vidí (gateway/firmy.py).
            return self._json(core.firma_klienti(query.get("firma", [""])[0]))
        if name == "vault-tree":
            return self._json(core.vault_tree(which))
        if name == "vault-note":
            note = core.vault_note(query.get("path", [""])[0], which)
            if not note:
                return self._json({"error": "Poznámka v trezoru není."}, 404)
            return self._json(note)
        if name == "vault-search":
            return self._json({"results": core.vault_search(query.get("q", [""])[0],
                                                            vault=which)})
        if name == "vault-graph":
            return self._json(core.vault_graph(which))
        if name in ("vault-save", "vault-rename", "vault-delete"):
            # Ve firemním trezoru je Lidé/<já> osobní trezor a Sdílené/<název>
            # sdílený — zápis jde tam, kam cesta doopravdy vede (core.vault_route).
            where, path, prefix = core.vault_route(payload.get("path", ""), which)
        if name == "vault-upload":
            # Jen osobní trezor — ve firemním Lidé/<já> (core.vault_route).
            # Do firemních a sdílených složek nahrává brána (/gw/firma/soubor).
            rel = query.get("path", [""])[0]
            where, sub, prefix = core.vault_route(rel, which)
            if where:
                return self._json({"ok": False, "error": "Sem soubor nahrává brána."}, 400)
            result = core.vault_upload(sub, (payload or {}).get("raw", b""),
                                       query.get("prepsat", [""])[0] == "1")
            return self._json(core.vault_prefixed(result, prefix),
                              409 if result.get("exists") else 200)
        if name == "vault-save":
            # Osobní trezor hub zapíše rovnou. Do firemního a sdílených zapsat
            # neumí (jsou jen ke čtení) — vznikne návrh a kartu potvrdí člověk.
            if where:
                return self._json(core.vault_prefixed(
                    {**core.vault_proposal(path, payload.get("text", ""), where),
                     "proposal": True}, prefix))
            return self._json(core.vault_prefixed(
                core.vault_save(path, payload.get("text", ""), mtime=payload.get("mtime")), prefix))
        if name == "vault-rename":
            to_where, to, _prefix = core.vault_route(payload.get("to", ""), which)
            if to_where != where or _prefix != prefix:
                return self._json({"ok": False, "error": "Přesouvat jde jen v rámci jedné složky Obsidianu."})
            return self._json(core.vault_prefixed(core.vault_rename(path, to, where), prefix))
        if name == "vault-delete":
            return self._json(core.vault_prefixed(core.vault_delete(path, where), prefix))
        if name == "firma":
            # Návrhy do firemního Obsidianu od Clauda (tools/firma.py). Hub je
            # ukáže a umí zahodit; nahrává brána po kliknutí (/gw/firma/publish).
            if payload.get("action") == "discard":
                return self._json({"ok": core.firma_discard(payload.get("id"))})
            return self._json({**core.firma_state(), "pending": core.firma_pending()})
        if name == "vault-file":
            # Obrázky se ukazují v náhledu; cokoli jiného (download=1) jen ke
            # stažení — prohlížeč to pod adresou hubu nikdy neotevře jako stránku.
            download = query.get("download", [""])[0] == "1"
            full = core.vault_path(query.get("path", [""])[0],
                                   None if download else core.VAULT_IMAGES, which)
            if not full:
                return self._send(404, b"404")
            if download:
                with open(full, "rb") as fh:
                    data = fh.read()
                fname = os.path.basename(full)
                ascii_name = re.sub(r'[^A-Za-z0-9._-]+', "_", fname) or "soubor"
                return self._send(200, data, "application/octet-stream", {
                    "Content-Disposition": f"attachment; filename=\"{ascii_name}\"; "
                                           f"filename*=UTF-8''{urllib.parse.quote(fname)}",
                    "Content-Security-Policy": "default-src 'none'; sandbox",
                    "X-Content-Type-Options": "nosniff"})
            ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
            with open(full, "rb") as fh:
                # SVG umí skripty: otevřený přímo jako stránka by běžel pod
                # adresou hubu. Sandbox v CSP mu je vezme, <img> to nevadí.
                return self._send(200, fh.read(), ctype, {
                    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; sandbox",
                    "X-Content-Type-Options": "nosniff"})
        if name == "soubor":
            return self._soubor_ke_stazeni(query, guest)
        if name == "stazene":
            # Co naposledy stáhl vestavěný prohlížeč (hub/stazene.py).
            return self._json({"slozka": core.STAZENE_DIR, "soubory": stazene.posledni()})
        if name in ("mcp-pc", "mcp-pc-nastav", "mcp-pc-obnovit"):
            # Napojení ze serveru v Claudovi na počítači (hub/mcp_pc.py).
            from . import mcp_pc
            if guest or core.on_gateway():
                return self._json({"error": "Jen v appce na počítači."}, 400)
            if name == "mcp-pc":
                return self._json(mcp_pc.prehled())
            if name == "mcp-pc-nastav":
                return self._json(mcp_pc.nastav((payload or {}).get("zapnout")))
            return self._json(mcp_pc.sync())
        if name in ("zrcadlo", "zrcadlo-prevzit", "zrcadlo-nastav", "zrcadla", "zrcadlo-na-server",
                    "zrcadlo-uvolnit"):
            return self._zrcadlo(name, query, payload, guest)
        if name == "soubor-otevrit":
            # Soubor z čtení (u „Stáhnout") rovnou otevřít výchozí aplikací —
            # jen hub na počítači a jen ze stránky na témže počítači; telefon
            # přes Tailscale ani prostor na serveru tím nic otevřít nesmí.
            if core.on_gateway() or guest or self.server.remote:
                return self._json({"error": "Otevřít jde jen v appce na počítači — stáhni si ho."}, 403)
            extra = [s.path for s in HUB.sessions.values()
                     if s.path and not getattr(s, "exited", False)]
            full, _velikost, proc = core.soubor_ke_stazeni(str(payload.get("path") or ""), extra)
            if not full:
                return self._json({"error": proc or "Soubor tu není."}, 404)
            slozka = bool(payload.get("slozka")) or core.je_spustitelny(full)
            ok = core.open_path(os.path.dirname(full) if slozka else full)
            if not ok:
                return self._json({"error": "Soubor se nepodařilo otevřít."}, 500)
            return self._json({"ok": True, "slozka": slozka})
        if name == "open-path":
            target = payload.get("path", "")
            if payload.get("kind") == "brain":
                target = (f"obsidian://open?vault="
                          f"{urllib.parse.quote(core.VAULT_NAME)}"
                          if core.has_obsidian() else core.BRAIN)
            elif payload.get("kind") == "note":
                fname = payload.get("file", "")
                if core.has_obsidian():
                    note = "memory/" + (fname[:-3] if fname.endswith(".md") else fname)
                    target = (f"obsidian://open?vault="
                              f"{urllib.parse.quote(core.VAULT_NAME)}"
                              f"&file={urllib.parse.quote(note)}")
                else:
                    target = os.path.join(core.MEMORY_DIR, fname)
            elif payload.get("kind") == "vault-note":
                # Z náhledu trezoru: tatáž poznámka v aplikaci Obsidian.
                rel = str(payload.get("file", "")).replace("\\", "/")
                full = core.vault_path(rel)
                target = full
                if full and core.has_obsidian():
                    target = (f"obsidian://open?vault="
                              f"{urllib.parse.quote(os.path.basename(core._vault_root()))}"
                              f"&file={urllib.parse.quote(rel[:-3] if rel.endswith('.md') else rel)}")
            return self._json({"ok": core.open_path(target)})
        if name == "image":
            raw = os.path.abspath(os.path.expanduser(
                query.get("path", [""])[0]))
            try:
                inside = os.path.commonpath(
                    [raw, core.IMAGE_DIR]) == core.IMAGE_DIR
            except ValueError:
                inside = False
            if not inside or not os.path.isfile(raw):
                return self._send(404, b"404")
            ctype = mimetypes.guess_type(raw)[0] or "application/octet-stream"
            with open(raw, "rb") as fh:
                return self._send(200, fh.read(), ctype)
        if name == "listdir":
            return self._json(listdir(query.get("path", [""])[0]))
        if name == "clipboard":
            which = (payload.get("which") or query.get("which", ["clipboard"])[0])
            if which not in ("clipboard", "primary"):
                which = "clipboard"
            if "text" in payload:                  # POST = zápis
                return self._json({"ok": core.clipboard_write(
                    str(payload["text"]), which)})
            text = core.clipboard_read(which)      # GET = čtení
            if text is None:
                return self._json(
                    {"error": "Na tomhle stroji chybí nástroj na schránku "
                              "(xclip / wl-clipboard)."}, 501)
            if not text and which == "clipboard":
                # Screenshot na schránce žádný text nemá a terminálu se obrázek
                # podat nedá — jen cesta k odložené kopii.
                path = core.clipboard_image()
                if path:
                    return self._json({"image": path})
            return self._json({"text": text})
        if name == "upload":
            try:
                raw = base64.b64decode(payload.get("data", ""), validate=True)
            except Exception:
                return self._json({"error": "Poškozená data."}, 400)
            if not raw:
                return self._json({"error": "Prázdný soubor."}, 400)
            if len(raw) > core.MAX_UPLOAD:
                return self._json(
                    {"error": f"Soubor je větší než "
                              f"{core.MAX_UPLOAD // (1024 * 1024)} MB."}, 413)
            jmeno = str(payload.get("name") or "")
            try:
                # Obrázky (a PDF, text) do hub-images kvůli náhledu, ostatní
                # dokumenty si drží svou příponu ve vlastní složce.
                save = core.save_priloha if core.je_dokument(jmeno) else core.save_upload
                return self._json({"path": save(jmeno, raw)})
            except Exception as exc:
                return self._json({"error": f"Nepodařilo se uložit: {exc}"}, 500)
        if name == "account":
            # Účet na bráně: ověření adresy, přihlášení, stav a přechod okna
            # do prostoru na serveru.
            payload = payload or {}
            action = payload.get("action", "status")
            if action == "probe":
                return self._json(account.probe(payload.get("server", "")))
            if action == "login":
                return self._json(account.login(
                    payload.get("server", ""),
                    payload.get("email", ""),
                    payload.get("password", "")))
            if action == "browser-start":
                return self._json(account.browser_start(payload.get("server", "")))
            if action == "browser-poll":
                return self._json(account.browser_poll(payload.get("server", ""), payload.get("n", "")))
            if action == "2fa":
                return self._json(account.second_factor(
                    payload.get("server", ""), payload.get("ticket", ""),
                    payload.get("code", "")))
            if action == "logout":
                res = account.logout()
                pocitac.wake()
                return self._json(res)
            if action in ("handoff", "connect"):
                # `connect` = přejít na server a pamatovat si to: appka se
                # příště otevře rovnou tam. Zapíše se, až když předání vyšlo —
                # jinak by se příští start pokoušel o server, který nechce.
                res = account.handoff(timeout=8)
                if action == "connect" and res.get("url"):
                    core.save_config({"server_mode": True})
                return self._json(res)
            if action == "local":
                core.save_config({"server_mode": False})
                return self._json({"ok": True})
            return self._json(account.status(
                timeout=5 if payload.get("quick") else account.TIMEOUT))
        if name == "restart":
            # Restart po aktualizaci: uloží otevřené taby, pustí novou instanci
            # a tuhle ukončí. Obnovu pak udělá stránka v novém okně (/api/restore).
            if core.on_gateway():
                return self._json({"error": "Prostor na serveru restartuje server sám."}, 400)
            return self._json(restart.relaunch(HUB))
        if name == "prostor-snapshot":
            # Prostor na serveru se přepíná na novou verzi (restartuje ho brána,
            # /gw/restart). Před tím se uloží taby — nová instance je otevře
            # znovu a Claude v nich pokračuje v konverzaci (/api/restore).
            if not core.on_gateway():
                return self._json({"error": "Jen v prostoru na serveru."}, 400)
            reason = "uspani" if (payload or {}).get("duvod") == "uspani" else "restart"
            return self._json({"tabs": restart.snapshot(HUB, reason)})
        if name == "restore":
            # Co bylo otevřené před restartem, zavřením appky nebo uspáním
            # prostoru. Čte se jednou, pak je soubor pryč.
            tabs, reason = restart.take()
            return self._json({"tabs": tabs, "duvod": reason})
        if name == "clockify":
            # Ruční stopky u projektu (hub/clockify.py). Běží mimo agenta —
            # člověk klikne Start, po Stopu je čas v Clockify.
            payload = payload or {}
            path = payload.get("path") or query.get("path", [""])[0] or ""
            action = payload.get("action") or ""
            if action == "start":
                return self._json(clockify.start(
                    path, payload.get("project") or "", payload.get("description") or ""))
            if action == "stop":
                return self._json(clockify.stop(path))
            if action == "choose":
                return self._json(clockify.choose(path, payload.get("project") or ""))
            return self._json(clockify.status(path))
        if name == "jev":
            # Jev (TypeSafe) přes Cloudflare — údaje vyplněné v Nastavení → AI
            # agenti. Uloží se jen ty, se kterými model doopravdy odpoví.
            payload = payload or {}
            action = payload.get("action") or ""
            if action == "save":
                res = jev.save(payload.get("account_id"), payload.get("api_token"))
                return self._json(res, 200 if res.get("ok") else 400)
            if action == "remove":
                return self._json(jev.remove())
            return self._json(jev.status())
        if name == "gemini":
            # Gemini (Google AI Pro) jako pomocník Clauda — stav, vypínač a
            # srovnání napojení (hub/gemini.py). Čtení stavu rovnou srovná
            # registraci: po instalaci a přihlášení v tabu stačí otevřít kartu.
            from . import gemini
            payload = payload or {}
            if "zapnout" in payload:
                return self._json(gemini.nastav(bool(payload.get("zapnout"))))
            gemini.sync()
            return self._json(gemini.stav())
        if name == "predplatne":
            # Claude v prostoru na vlastním předplatném: propojení přes
            # `claude setup-token` na tomhle počítači a stav u brány.
            if core.on_gateway():
                return self._json({"error": "Předplatné se připojuje z appky na počítači."}, 400)
            action = payload.get("action") or ""
            # Víc předplatných: přidání, přepnutí, přejmenování, odebrání,
            # limity — v prostoru i pro Claude Code na tomhle počítači.
            out = predplatne.akce(action, payload, query, HUB.prenacti_chaty)
            if out is not None:
                return self._json(out)
            if action == "code":
                return self._json(predplatne.send_code(payload.get("code")))
            if action == "cancel":
                return self._json(predplatne.cancel())
            if action == "skip":
                predplatne.skip(True)
                return self._json({"ok": True})
            if action == "disconnect":
                return self._json(predplatne.disconnect())
            if query.get("quick"):
                return self._json({"connect": dict(predplatne.STATE)})
            return self._json(predplatne.status())
        if name == "pocitac":
            # Claude ze serveru na tomhle počítači: stav mostu a volba přístupu.
            action = payload.get("action") or ""
            if action in ("ukol-spustit", "ukol-zahodit"):
                # Úkol na později spouští člověk tady, ne prostor na serveru.
                if core.on_gateway():
                    return self._json({"error": "Úkoly ze serveru se spouštějí v appce na počítači."}, 400)
                try:
                    run = pocitac.start_ukol if action == "ukol-spustit" else pocitac.dismiss_ukol
                    return self._json(run(payload.get("id")))
                except ValueError as exc:
                    return self._json({"error": str(exc)}, 400)
            if action in ("rozhodnout", "zrusit-povoleni"):
                # Souhlas dává člověk tady v appce na počítači, nebo v prostoru přes
                # bránu (cookie) — ne prostor sám, ve kterém běží Claude.
                if core.on_gateway():
                    return self._json({"error": "O souhlasu se rozhoduje v appce na počítači."}, 400)
                try:
                    if action == "zrusit-povoleni":
                        return self._json(pocitac.revoke_grant())
                    return self._json(pocitac.decide(payload.get("id"), str(payload.get("volba") or ""),
                                                      odpovedi=payload.get("odpovedi")))
                except ValueError as exc:
                    return self._json({"error": str(exc)}, 400)
            if "access" in payload:
                if core.on_gateway():
                    return self._json({"error": "Tohle se nastavuje v appce na počítači."}, 400)
                try:
                    return self._json(pocitac.set_access(payload.get("access")))
                except ValueError as exc:
                    return self._json({"error": str(exc)}, 400)
            return self._json(pocitac.status())
        if name == "remote":
            # Telefon: stav, párovací QR a zapnutí/vypnutí druhého listeneru.
            action = (payload or {}).get("action", "")
            if action == "enable":
                core.save_config({"remote_enabled": True})
                state = start_remote()
            elif action == "disable":
                core.save_config({"remote_enabled": False})
                stop_remote()
                state = remote_status()
            elif action == "rotate":
                remote.rotate()
                state = start_remote() if REMOTE["server"] or \
                    core.CONFIG.get("remote_enabled") else remote_status()
            elif action == "port":
                try:
                    wanted = int((payload or {}).get("port") or 0)
                except (TypeError, ValueError):
                    wanted = 0
                if not 1024 <= wanted <= 65535:
                    return self._json({"error": "Port musí být 1024-65535."}, 400)
                core.save_config({"remote_port": wanted})
                state = start_remote() if core.CONFIG.get("remote_enabled") \
                    else remote_status()
            else:
                state = remote_status()
            if state.get("url"):
                try:
                    state["qr"] = qr.svg(state["url"])
                except Exception as exc:
                    core.log_error("QR kód se nepovedl", exc)
            return self._json(state)
        if name == "config":
            allowed = ("project_dirs", "brain_dir", "onboarded", "vault_autosync",
                       "newtab", "extra_projects", "show_archived",
                       "agents", "default_agent", "project_agents",
                       "remote_keep_running", "dev_mode", "memory_autosave",
                       "hlas_model", "theme", "prohlizec_okno", "na_pozadi", "bez_spanku", "tray",
                       "auto_update", "verxee_debug_klic", "panel", "novy_chat_agent")
            updates = {k: v for k, v in payload.items() if k in allowed}
            if "panel" in updates:
                # Položky panelu vlevo (Nastavení → Přizpůsobit → Panel vlevo).
                if not isinstance(updates["panel"], dict):
                    return self._json({"error": "Neznámé nastavení panelu."}, 400)
                panel = dict(core.CONFIG.get("panel") or {})
                panel.update({k: bool(v) for k, v in updates["panel"].items()
                              if k in ("projekty", "obsidian", "terminal")})
                updates["panel"] = panel
            if "verxee_debug_klic" in updates:
                # Na bráně klíč do prostorů píše správce — prostor by si ho
                # přepsal jen do příštího startu.
                if core.on_gateway():
                    return self._json({"error": "Klíč na serveru nastavuje správce."}, 403)
                klic = str(updates["verxee_debug_klic"] or "").strip()
                if klic and not core.VERXEE_KLIC.fullmatch(klic):
                    return self._json({"error": "Klíč vypadá jako vxd_ a 24 znaků 0–9 a a–f."}, 400)
                updates["verxee_debug_klic"] = klic
            if "novy_chat_agent" in updates and not re.fullmatch(r"[a-z0-9_-]{0,40}", str(updates["novy_chat_agent"])):
                return self._json({"error": "Neznámý agent."}, 400)
            if "theme" in updates and updates["theme"] not in ("", "dark", "light"):
                return self._json({"error": "Neznámé téma."}, 400)
            if "hlas_model" in updates and updates["hlas_model"] not in ("turbo", "small"):
                return self._json({"error": "Neznámý model pro přepis."}, 400)
            if not updates:
                return self._json({"error": "Nic k uložení."}, 400)
            try:
                core.save_config(updates)
                if "bez_spanku" in updates or "tray" in updates:
                    pozadi.sync()
            except Exception as exc:
                return self._json({"error": f"Konfig nejde zapsat: {exc}"}, 500)
            return self._json({"ok": True, "brain_dir": core.BRAIN,
                               "project_dirs": core.PROJECT_DIRS})
        if name == "agents":
            # Verze se zjišťují spuštěním každého CLI, takže to jede na pozadí
            # stejně jako MCP — stránka se na to nesmí zdržet.
            state = core.job_state("agents")
            if query.get("refresh") or not state.get("done"):
                core.start_job("agents", lambda: core.agents_state())
                state = core.job_state("agents")
            if state.get("done"):
                return self._json({"running": False, **(state.get("result") or {})})
            return self._json({"running": True, "step": state.get("step") or ""})
        if name == "project":
            return self._project(payload)
        if name == "vault":
            return self._vault(payload)
        if name == "update-check":
            # Síťový dotaz je zvlášť, aby se na něj nečekalo při každém načtení.
            return self._json(core.version_info(check_remote=True))
        if name == "update":
            if core.on_gateway():
                # Na serveru aktualizuje server sám (gateway/update.sh). Instalace
                # do domova prostoru by nic nezměnila, jen by mátla verzí.
                return self._json({"started": False, "running": False, "result": {
                    "ok": False,
                    "detail": "Hub na serveru se aktualizuje sám každou noc."}})
            started = core.start_update()
            return self._json({"started": started, **core.update_state()})
        if name == "log":
            if "text" in payload:
                # Chyby ze stránky patří do stejného souboru jako ty ze serveru,
                # jinak by se problém hledal na dvou místech.
                core.log(f"stránka: {str(payload['text'])[:500]}",
                         payload.get("level") or "error")
                return self._json({"ok": True})
            return self._json({"lines": core.log_tail(
                int(query.get("lines", ["300"])[0] or 300))})
        if name == "log-clear":
            core.log_clear()
            return self._json({"ok": True})
        if name == "report":
            return self._json({"text": core.report_bundle()})
        if name == "stats":
            # První výpočet čte skoro gigabajt přepisů, takže na pozadí;
            # další už jedou z mezipaměti a vrátí se hned.
            cached = core.job_state("stats")
            if cached.get("running"):
                return self._json({"running": True, "step": cached.get("step", "")})
            if query.get("refresh") or not (cached.get("result") or {}).get("tokens"):
                core.start_job("stats", lambda: stats.collect(
                    progress=lambda m: core.job_step("stats", m)))
                return self._json({"running": True, "step": "počítám…"})
            return self._json({"running": False, **(cached.get("result") or {})})
        if name == "prenos-stav":
            # Chaty, které sem přišly z druhé strany a čekají na otevření.
            return self._json({"cekajici": prenos.cekajici(core.CLAUDE_DIR),
                               "ukoly": [u for u in pocitac.ukoly()
                                         if u.get("kind") == "chat" and u.get("state") == "ceka"]
                               if not core.on_gateway() else []})
        if name == "prenos":
            # Přenos chatu: počítač → server, server → počítač, souhlas s příjmem, hotovo.
            akce = str((payload or {}).get("akce") or "")
            chat = str((payload or {}).get("chat") or "")
            try:
                if akce == "na-server":
                    return self._json(prenos_hub.na_server(chat, (payload or {}).get("titulek", "")))
                if akce == "na-pocitac":
                    return self._json(prenos_hub.na_pocitac(chat, (payload or {}).get("titulek", "")))
                if akce == "prijmout":
                    return self._json(prenos_hub.prijmout(str((payload or {}).get("id") or "")))
                if akce == "hotovo":
                    prenos.odeber_cekajici(core.CLAUDE_DIR, chat)
                    return self._json({"ok": True})
            except ValueError as exc:
                return self._json({"ok": False, "error": str(exc)}, 400)
            return self._json({"error": "Neznámá akce."}, 400)
        if name == "plan-usage":
            # Zbývající limit předplatného (5 hodin, týden) — jako /usage v Claude Code.
            from . import usage
            refresh = (query.get("refresh") or [""])[0] == "1"
            # Na počítači s vybraným dalším předplatným limit toho, na čem jedou taby.
            vybrane = predplatne.lokal_limit_aktivni(refresh)
            return self._json(vybrane if vybrane is not None else usage.own(refresh=refresh))
        if name == "project-usage":
            # Tokeny a cena u každého projektu — z mezipaměti, bez čtení přepisů.
            usage = stats.project_usage()
            if usage["pending"] and not core.job_state("stats").get("running"):
                core.start_job("stats", lambda: stats.collect(
                    progress=lambda m: core.job_step("stats", m)))
            return self._json(usage)
        if name == "project-stats":
            path = (query.get("path") or [""])[0]
            if not path:
                return self._json({"error": "chybí cesta"}, 400)
            return self._json(stats.project_detail(path))
        if name == "pluginy":
            # Pluginy Claude Code (katalog, instalace) a pluginy appky (hub/pluginy.py).
            from . import pluginy
            co = payload.get("akce") or ""
            if not co and (query.get("jen") or [""])[0] == "appka":
                return self._json(pluginy.appka_seznam())
            if not co and (query.get("jen") or [""])[0] == "napojeni":
                return self._json(pluginy.napojeni_katalog((query.get("q") or [""])[0]))
            if not co and (query.get("jen") or [""])[0] == "skilly":
                return self._json(pluginy.skilly((query.get("zdroj") or [""])[0], bool(query.get("refresh"))))
            if not co and (query.get("jen") or [""])[0] == "skill":
                return self._json(pluginy.skill_detail((query.get("zdroj") or [""])[0], (query.get("name") or [""])[0],
                                                       (query.get("kde") or [""])[0]))
            # Aladin Market (hub/market.py): motivy, katalog z GitHubu, skilly Aladina.
            if not co and (query.get("jen") or [""])[0] == "motivy":
                return self._json(motivy.seznam())
            if not co and (query.get("jen") or [""])[0] == "market":
                from . import market
                return self._json(market.katalog(bool(query.get("refresh"))))
            if not co and (query.get("jen") or [""])[0] == "paluba-skilly":
                from . import market
                return self._json(market.skilly_paluby())
            if co.startswith(("motiv-", "market-", "skill-")):
                from . import market
                if co == "motiv-nastav":
                    out = motivy.nastav(payload.get("id", ""))
                elif co in ("motiv-pridej", "motiv-odeber"):
                    out = (motivy.pridej(payload.get("url", "")) if co == "motiv-pridej"
                           else motivy.odeber(payload.get("id", "")))
                    out = {**out, "motivy": motivy.seznam()}
                elif co == "market-instaluj":
                    out = market.instaluj(payload.get("druh", ""), payload.get("id", ""))
                    out = {**out, "appka": pluginy.appka_seznam(), "motivy": motivy.seznam()}
                elif co in ("skill-instaluj", "skill-odeber"):
                    out = (market.skill_instaluj if co == "skill-instaluj" else market.skill_odeber)(
                        payload.get("name", ""))
                    out = {**out, **market.skilly_paluby()}
                else:
                    return self._json({"ok": False, "message": "Neznámá akce."}, 400)
                return self._json(out, 200 if out.get("ok") else 400)
            if not co:
                obnovit = bool(query.get("refresh"))
                return self._json({"cc": pluginy.katalog(obnovit), "appka": pluginy.appka_seznam()})
            if co.startswith("appka-"):
                pid = payload.get("id", "")
                res = {"appka-zapni": lambda: pluginy.appka_zapni(pid, True),
                       "appka-vypni": lambda: pluginy.appka_zapni(pid, False),
                       "appka-pridej": lambda: pluginy.appka_pridej(payload.get("url", "")),
                       "appka-odeber": lambda: pluginy.appka_odeber(pid),
                       "appka-ukazka": pluginy.appka_ukazka}.get(co)
                if not res:
                    return self._json({"ok": False, "message": "Neznámá akce."}, 400)
                out = res()
                return self._json({**out, "appka": pluginy.appka_seznam()}, 200 if out.get("ok") else 400)
            out = pluginy.akce(co, payload.get("id", ""), payload.get("sha", ""), payload.get("zdroj", ""))
            return self._json(out, 200 if out.get("ok") or out.get("potvrdit") else 400)
        if name == "okno-otevrit":
            # Klik na ikonku v liště (hub/tray.py): ukázat okno appky. Okno
            # z cizího procesu dopředu vytáhnout nejde (Wayland to nedovolí),
            # tak se otevřené okno zavře a otevře znovu — taby běží dál tady.
            if core.on_gateway() or self.command != "POST":
                return self._json({"error": "Jen v appce na počítači."}, 400)

            def otevri():
                from . import restart
                if HUB.clients > 0:
                    # Okno se zavře samo (hub.js, za 1,2 s). Podle procesu ho
                    # najít nejde: po restartu má Chromium v příkazu starou adresu.
                    HUB.broadcast({"t": "okno-zavri"})
                    time.sleep(1.6)
                restart._spawn()
            threading.Thread(target=otevri, daemon=True).start()
            return self._json({"ok": True})
        if name == "konec-info":
            # Ikonka v liště se před ukončením ptá: kolik chatů se zavře a jestli
            # v některém Claude zrovna pracuje (hub/tray.py, _potvrd_konec).
            from . import mcp_tools
            taby = [s for s in list(HUB.sessions.values()) if not getattr(s, "exited", False)]
            pracuje = 0
            for s in taby:
                if s.kind == "project" or s.kind.startswith("slash:"):
                    try:
                        cesta = core.transcript_for(s)
                        pracuje += bool(cesta and mcp_tools._pracuje(cesta))
                    except Exception:
                        pass
            return self._json({"taby": len(taby), "pracuje": pracuje})
        if name == "quit":
            # Úplné ukončení appky, i když běží na pozadí (Nastavení → Ostatní,
            # ikonka v liště). Taby se napřed uloží jako při zavření okna —
            # po dalším spuštění se vrátí a paměť se doplní (HUB.shutdown);
            # dřív os._exit všechno utnul bez uložení.
            if core.on_gateway():
                return self._json({"error": "Prostor na serveru ukončuje server."}, 400)

            def konec():
                try:
                    HUB.broadcast({"t": "okno-zavri"})
                    HUB.shutdown(keep=True)
                except Exception as exc:
                    core.log_error("uložení před ukončením", exc)
                pozadi.konec()
            threading.Timer(0.3, konec).start()
            return self._json({"ok": True})
        if name == "claude-ucet":
            # Pod jakým účtem claude.ai je Claude Code přihlášený (Nastavení → Účet).
            return self._json({"account": core.claude_account()})
        if name == "logo":
            from . import pluginy
            hit = pluginy.logo((query.get("k") or [""])[0])
            if not hit:
                return self._send(404, b"", "text/plain", {"Cache-Control": "private, max-age=3600"})
            return self._send(200, hit[0], hit[1], {"Cache-Control": "private, max-age=604800",
                                                    "Content-Security-Policy": "default-src 'none'"})
        if name == "plugin-soubor":
            # Soubory zapnutého pluginu appky (JS, CSS, obrázky) — jen s tokenem.
            from . import pluginy
            hit = pluginy.appka_soubor((query.get("id") or [""])[0], (query.get("f") or [""])[0])
            if not hit:
                return self._send(404, b"404")
            with open(hit[0], "rb") as fh:
                return self._send(200, fh.read(), hit[1])
        if name == "motiv-soubor":
            # Styl motivu a co si z něj bere (fonty, obrázky) — jen s tokenem.
            hit = motivy.soubor((query.get("id") or [""])[0], (query.get("f") or [""])[0])
            if not hit:
                return self._send(404, b"404")
            with open(hit[0], "rb") as fh:
                return self._send(200, fh.read(), hit[1], {"Cache-Control": "private, max-age=3600"})
        if name == "connect":
            # Služby pro člověka — Freelo, Canva, Ecomail, Google, i více účtů
            # (hub/connect.py). Nad technickým katalogem /api/mcp.
            action = payload.get("action") or ""
            if action == "add":
                result = connect.add_account(payload.get("service", ""),
                                             payload.get("label", ""),
                                             payload.get("account", ""),
                                             payload.get("client_id", ""),
                                             payload.get("client_secret", ""))
                return self._json(result, 200 if result.get("ok") else 400)
            if action == "login":
                result = connect.login_start(payload.get("name", ""))
                return self._json(result, 200 if result.get("ok") else 400)
            if action == "finish":
                return self._json(connect.login_finish(payload.get("id", ""),
                                                       payload.get("url", "")))
            if action == "status":
                return self._json(connect.login_status(payload.get("id", "")))
            if action == "cancel":
                return self._json(connect.login_cancel(payload.get("id", "")))
            if action == "remove":
                result = connect.remove_account(payload.get("service", ""),
                                                payload.get("name", ""))
                return self._json(result, 200 if result.get("ok") else 400)
            if action == "shared-sync":
                # Sdílená napojení se změnila (nastavení mluví s bránou samo);
                # srovnat registrace v Claude Code.
                return self._json({"ok": True, "count": connect.sync_shared()})
            if action == "google-client":
                result = connect.save_google_client(payload.get("client_id", ""),
                                                    payload.get("client_secret", ""))
                return self._json(result, 200 if result.get("ok") else 400)
            return self._json(connect.services(
                bool(query.get("refresh") or payload.get("refresh"))))
        if name == "mcp":
            # Napojení na cizí služby. Zdravotní kontrola oslovuje každý server
            # zvlášť (~10 s), takže stejný postup jako u statistik: spočítat na
            # pozadí a stránce vracet, co už je hotové.
            action = payload.get("action") or ""
            if action == "add":
                result = core.mcp_add(payload.get("name", ""),
                                      payload.get("values") or {},
                                      payload.get("scope") or "user",
                                      payload.get("path") or "")
                return self._json(result, 200 if result.get("ok") else 400)
            if action == "remove":
                result = core.mcp_remove(payload.get("name", ""),
                                         payload.get("scope") or "user",
                                         payload.get("path") or "")
                return self._json(result, 200 if result.get("ok") else 400)
            cached = core.job_state("mcp")
            # Účet se čte ze souboru, ne ze sítě — posílá se i do rozdělané
            # kontroly, ať je hned vidět, ke komu konektory patří.
            acct = core.claude_account()
            if cached.get("running"):
                return self._json({"running": True, "catalog": core.MCP_CATALOG,
                                   "account": acct,
                                   "step": cached.get("step", "")})
            done = cached.get("result") or {}
            if query.get("refresh") or payload.get("refresh") or not done:
                core.start_job("mcp", core.mcp_list)
                return self._json({"running": True, "catalog": core.MCP_CATALOG,
                                   "account": acct,
                                   "step": "ptám se serverů…"})
            return self._json({"running": False, "catalog": core.MCP_CATALOG,
                               "account": acct, **done})
        if name == "update-status":
            return self._json(core.update_state())
        if name == "job":
            return self._json(core.job_state(
                query.get("name", ["vault"])[0]))
        return self._json({"error": "unknown"}, 404)

    def _project(self, payload):
        """Štítek, briefing, archivace, přidání a odebrání projektu.

        „Odebrat" znamená odebrat z Hubu — na složku se nesahá. Mazat cizí
        práci z launcheru je poslední věc, kterou by kdo čekal.
        """
        action = payload.get("action")
        path = os.path.abspath(os.path.expanduser(str(payload.get("path", ""))))
        if not path or path == os.path.abspath(os.sep):
            return self._json({"error": "Chybí cesta."}, 400)
        try:
            if action == "save":
                # Zapisovat CLAUDE.md kamkoli na disk není potřeba — omezíme se
                # na složky, které hub zná jako projekty.
                known = {os.path.abspath(pr["path"]) for pr in core.get_projects()}
                if path not in known:
                    return self._json(
                        {"error": "Tuhle složku hub nezná jako projekt."}, 400)
                updates = {}
                for key in ("label", "brief", "group", "repo", "image"):
                    if key in payload:
                        updates[key] = str(payload[key]).strip()
                if "archived" in payload:
                    updates["archived"] = bool(payload["archived"])
                core.set_project_meta(path, updates)
                written = None
                if "brief" in updates and os.path.isdir(path):
                    written = core.write_briefing(path, updates["brief"])
                return self._json({"ok": True, "briefing": written})
            if action == "add":
                if not os.path.isdir(path):
                    return self._json({"error": "Taková složka není."}, 400)
                extra = list(core.CONFIG.get("extra_projects") or [])
                if path not in extra:
                    extra.append(path)
                    core.save_config({"extra_projects": extra})
                return self._json({"ok": True, "path": path})
            if action == "remove":
                extra = [p for p in (core.CONFIG.get("extra_projects") or [])
                         if os.path.abspath(os.path.expanduser(p)) != path]
                core.save_config({"extra_projects": extra})
                data = core.load_projects()
                data.pop(path, None)
                core.save_projects(data)
                # Uvnitř nastavených složek ho sken najde znovu — pak zbývá archiv.
                still = any(os.path.abspath(p["path"]) == path
                            for p in core.get_projects())
                return self._json({"ok": True, "rescanned": still})
        except Exception as exc:
            return self._json({"error": f"Nepovedlo se: {exc}"}, 500)
        return self._json({"error": "Neznámá akce."}, 400)

    def _vault(self, payload):
        """Založit / vybrat / přesunout vault, nebo z něj udělat git zálohu."""
        action = payload.get("action")
        path = os.path.expanduser(str(payload.get("path", "")))
        try:
            if action == "create":
                if not path:
                    return self._json({"error": "Chybí cesta."}, 400)
                for sub in ("memory", "skills", ".obsidian"):
                    os.makedirs(os.path.join(path, sub), exist_ok=True)
                index = os.path.join(path, "memory", "MEMORY.md")
                if not os.path.isfile(index):
                    with open(index, "w", encoding="utf-8") as fh:
                        fh.write(core.EMPTY_MEMORY_INDEX)
                core.save_config({"brain_dir": path})
                link = core.link_memory(path)
                return self._json({"ok": True, "path": core.BRAIN, **link})
            if action == "use":
                if not os.path.isdir(path):
                    return self._json({"error": "Taková složka není."}, 400)
                core.save_config({"brain_dir": path})
                link = core.link_memory(path)
                return self._json({"ok": True, "path": core.BRAIN,
                                   "has_memory": core.HAS_BRAIN, **link})
            if action == "clone":
                repo = str(payload.get("repo", "")).strip()
                if not repo:
                    return self._json({"error": "Chybí adresa repa."}, 400)
                parent = path or os.path.join(core.HOME, "Obsidian")
                target = core.clone_vault(repo, parent)
                core.save_config({"brain_dir": target})
                link = core.link_memory(target)
                return self._json({"ok": True, "path": target, **link})
            # Přesun i první push trvají; držet na nich požadavek znamená, že
            # se stránka zasekne a po zavření okna se výsledek nemá kam vrátit.
            if action == "move":
                started = core.start_job(
                    "vault", lambda: {"ok": True, **core.move_vault(path)})
                return self._json({"started": started, **core.job_state("vault")})
            if action == "git":
                name = str(payload.get("repo") or "claude-brain").strip()
                started = core.start_job("vault", lambda: dict(
                    zip(("ok", "detail"), core.vault_git_setup(name))))
                return self._json({"started": started, **core.job_state("vault")})
        except ValueError as exc:
            return self._json({"error": str(exc)}, 400)
        except Exception as exc:
            return self._json({"error": f"Nepovedlo se: {exc}"}, 500)
        return self._json({"error": "Neznámá akce."}, 400)

    # ---- websocket ----
    def _websocket(self, query):
        if not self._authorised(query) or not self._origin_ok():
            return self._send(403, b"Neplatny token.")
        guest = guest_from(self.headers)
        if guest and guest.get("role") == "predplatne":
            # Správa předplatného terminál ani Clauda nedostane (druhá hráz
            # za bránou, gateway/pristup.py).
            return self._send(403, b"Nepovoleno.")
        key = self.headers.get("Sec-WebSocket-Key", "")
        if not key:
            return self._send(400, b"Neni websocket.")
        accept = base64.b64encode(
            hashlib.sha1((key + WS_GUID).encode()).digest()).decode()
        self.wfile.write(
            b"HTTP/1.1 101 Switching Protocols\r\n"
            b"Upgrade: websocket\r\nConnection: Upgrade\r\n"
            b"Sec-WebSocket-Accept: " + accept.encode() + b"\r\n\r\n")
        self.wfile.flush()
        self.close_connection = True  # we own the socket from here on

        conn = WSConn(self.connection)
        conn.guest = guest_from(self.headers)
        HUB.clients += 1
        HUB.conns.add(conn)
        HUB.last_empty_at = None
        try:
            self._ws_loop(conn)
        finally:
            HUB.clients -= 1
            HUB.conns.discard(conn)
            if HUB.clients <= 0:
                HUB.last_empty_at = time.time()
            for session in list(HUB.sessions.values()):
                session.detach(conn)
            prohlizec.odpoj(conn)
            conn.close()

    def _ws_loop(self, conn):
        buffer, buf_opcode = bytearray(), None
        while not conn.closed:
            frame = read_frame(self.rfile)
            if frame is None:
                return
            fin, opcode, payload = frame
            if opcode == 0x8:            # close
                return
            if opcode == 0x9:            # ping
                conn.send(payload, 0xA)
                continue
            if opcode == 0xA:            # pong
                continue
            if opcode == 0x0:            # continuation
                buffer += payload
            else:
                buffer, buf_opcode = bytearray(payload), opcode
            if not fin:
                continue
            message, buffer = bytes(buffer), bytearray()
            if buf_opcode == 0x1:
                self._ws_message(conn, message)

    def _ws_message(self, conn, raw):
        try:
            msg = json.loads(raw.decode("utf-8", "replace"))
        except Exception:
            return
        kind = msg.get("t")
        sid = msg.get("id")
        session = HUB.sessions.get(sid)
        guest = getattr(conn, "guest", None)
        if guest and guest.get("role") not in ("pomocnik", "plny"):
            return
        if guest and guest.get("role") == "pomocnik" and kind == "close" \
                and session and not guest_owns(guest, session):
            # Pomocník nezavírá vlastníkovi rozdělanou práci.
            conn.send_json({"t": "error", "d": "Cizí tab zavřít nemůžeš."})
            return

        if kind == "hello":
            # Verze: stránka po aktualizaci serveru jen znovu připojí websocket
            # a jela by se starým JavaScriptem — podle tohohle se načte znovu.
            conn.send_json({"t": "sessions", "version": core.version(),
                            "list": [s.info() for s in HUB.sessions.values()]})
            if guest and not getattr(conn, "ohlaseno", False):
                conn.ohlaseno = True
                HUB.broadcast({"t": "host-vstup", "name": guest.get("name") or guest.get("email"),
                               "role": guest.get("label") or guest.get("role")}, skip=conn)
        elif kind == "open":
            try:
                session = HUB.open(msg.get("kind", "project"), msg.get("path", ""),
                                   msg.get("title", "shell"),
                                   int(msg.get("cols", 80)), int(msg.get("rows", 24)),
                                   agent=str(msg.get("agent", "") or ""),
                                   model=str(msg.get("model", "") or ""),
                                   resume=str(msg.get("resume", "") or ""),
                                   fork=msg.get("fork") is True,
                                   vault=str(msg.get("vault", "") or ""),
                                   # Úvodní zpráva: z okna se čtením se dá
                                   # rovnou napsat a konverzace s ní pokračuje.
                                   prompt=str(msg.get("prompt", "") or "")[:20000],
                                   # Obnova po restartu: tab mimo zrak se
                                   # založí uspaný (Session, `spici`).
                                   spici=msg.get("spici") is True)
            except (pty_backend.PtyUnavailable, core.BashMissing) as exc:
                core.log_error("tab se nepodařilo otevřít", exc)
                conn.send_json({"t": "error", "ref": msg.get("ref"), "d": str(exc)})
                return
            except Exception as exc:
                core.log_error("tab se nepodařilo otevřít", exc)
                conn.send_json({"t": "error", "ref": msg.get("ref"),
                                "d": f"Nepodařilo se otevřít terminál: {exc}"})
                return
            if guest:
                session.guest = {"id": guest["id"], "name": guest.get("name") or "",
                                 "email": guest.get("email") or ""}
            core.log(f"tab otevřen: {session.kind} {session.path or '~'}"
                     + (f" [{session.agent}]" if session.agent else ""))
            conn.send_json({"t": "opened", "ref": msg.get("ref"), **session.info()})
            # Rozměr drží okno, které tab otevřelo — zatím ho nikdo jiný nevidí.
            session.resize(conn, int(msg.get("cols", 80)), int(msg.get("rows", 24)))
            session.attach(conn)
            # Tentýž hub může mít otevřený i jiné okno (appka + prohlížeč).
            HUB.broadcast({"t": "tab-opened", **session.info()}, skip=conn)
        elif kind == "br":
            # Prohlížeč v appce (hub/prohlizec.py): obraz ven, myš a klávesy dovnitř.
            prohlizec.zprava(conn, msg, server=bool(core.CONFIG.get("server_mode")))
        elif kind == "attach" and session:
            session.attach(conn)
        elif kind == "in" and session:
            session.drive(conn)
            session.write(msg.get("d", "").encode("utf-8"))
        elif kind == "focus" and session:
            session.drive(conn)
            if session.uspany:
                session.probud()
        elif kind == "zije" and session:
            session.posledni = time.time()       # stránka se na tab dívá
        elif kind == "wake" and session:
            # Stránka tab otevřela (i bez fokusu okna, třeba na telefonu).
            if session.uspany:
                session.probud()
        elif kind == "resize" and session:
            session.resize(conn, int(msg.get("cols", 80)), int(msg.get("rows", 24)))
        elif kind == "rename" and session:
            session.title = str(msg.get("title", session.title))[:60]
            session.vlastni_nazev = True      # ručně přejmenovaný tab se sám nepřejmenuje
            HUB.broadcast({"t": "tab-renamed", "id": sid, "title": session.title},
                          skip=conn)
        elif kind == "close" and session:
            HUB.close(sid)
            HUB.broadcast({"t": "tab-closed", "id": sid}, skip=conn)


def listdir(path):
    """Directory listing for the folder picker (no native dialog in a browser)."""
    path = os.path.expanduser(path or core.HOME)
    if not os.path.isdir(path):
        path = core.HOME
    path = os.path.abspath(path)
    try:
        names = sorted(n for n in os.listdir(path)
                       if not n.startswith(".") and
                       os.path.isdir(os.path.join(path, n)))
    except Exception:
        names = []
    roots = [{"name": "Domů", "path": core.HOME}]
    roots += [{"name": os.path.basename(p) or p, "path": p} for p in core.PROJECT_DIRS]
    if core.IS_WINDOWS:
        roots += [{"name": f"{d}:", "path": f"{d}:\\"} for d in string.ascii_uppercase
                  if os.path.isdir(f"{d}:\\")]
    parent = os.path.dirname(path)
    return {
        "path": path,
        "parent": parent if parent and parent != path else "",
        "dirs": [{"name": n, "path": os.path.join(path, n)} for n in names],
        "roots": roots,
    }


class HubHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    # Prohlížeč (a přes bránu i reverzní proxy) otevře najednou spoustu spojení
    # na assety. Výchozí backlog 5 některé odmítne (RemoteDisconnected) — proto
    # větší fronta příchozích spojení.
    request_queue_size = 128

    def __init__(self, token, address=("127.0.0.1", 0), remote=False):
        super().__init__(address, Handler)
        self.token = token
        self.remote = remote
        self.origins = set()

    def handle_error(self, request, client_address):
        # Klient zavřel spojení uprostřed odpovědi (zavřená karta, telefon bez
        # signálu, proxy): není to chyba hubu a výchozí výpis traceback do logu
        # jen zahlcuje skutečné chyby.
        exc = sys.exc_info()[1]
        if isinstance(exc, (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError)):
            return
        super().handle_error(request, client_address)

    def server_bind(self):
        # HTTPServer.server_bind volá socket.getfqdn() — zpětný dotaz do DNS na
        # jméno počítače. Na některých sítích (VPN, macOS v CI) trvá desítky
        # sekund a o tolik se zdrží každý start hubu, i restart po aktualizaci.
        # Jméno serveru hub k ničemu nepotřebuje, stačí adresa.
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = host
        self.server_port = port


def _otevrit_server(token):
    """Hub po každém startu na stejném portu, když je volný.

    Prohlížeč drží uložené věci (poslední chat, rozepsané zprávy, připnuté
    chaty, motiv) podle adresy včetně portu; při náhodném portu by po každém
    zavření a otevření appky začal znovu prázdný. Zkušební hub (--test) a
    obsazený port berou náhodný, jako dřív."""
    soubor = os.environ.get("HUB_PORT_SOUBOR") or os.path.join(core.CLAUDE_DIR, "hub-port")
    zkouska = "--test" in sys.argv and not os.environ.get("HUB_PORT_SOUBOR")
    if not zkouska:
        try:
            with open(soubor, encoding="utf-8") as fh:
                return HubHTTPServer(token, ("127.0.0.1", int(fh.read().strip())))
        except (OSError, ValueError, OverflowError):
            pass
    httpd = HubHTTPServer(token)
    if not zkouska:
        try:
            with open(soubor, "w", encoding="utf-8") as fh:
                fh.write(str(httpd.server_address[1]))
        except OSError:
            pass
    return httpd


def _zahrat_doctor():
    """Co chce první /api/state (agenti, projekty s gitem, obsluha obsidian://),
    se připraví souběžně se startem okna, ať první načtení stránky nečeká."""
    def jedno(fn, co):
        try:
            fn()
        except Exception as exc:      # noqa: BLE001 — jen příprava, nic nesmí shodit
            core.log_error(f"start: příprava {co}", exc)
    for fn, co in ((core.doctor, "doctor()"), (core.get_projects, "projektů"),
                   (core.has_obsidian, "Obsidianu"), (core.get_memory, "paměti")):
        threading.Thread(target=jedno, args=(fn, co), daemon=True).start()

    # Katalog pluginů (~2 s přes `claude plugin list`) a skilly až po chvíli,
    # ať nezdržují start — v Nastavení jsou pak hned.
    def pozdeji():
        time.sleep(8)
        from . import pluginy
        jedno(pluginy.katalog, "katalogu pluginů")
        jedno(pluginy.skilly, "skillů")
    threading.Thread(target=pozdeji, daemon=True).start()


def start():
    """Start the server on a random loopback port. Returns (server, url)."""
    core.omez_arenas()               # glibc: míň arén = menší RSS hubu (děti: MALLOC_ARENA_MAX)
    # V prostoru na serveru: připravit Claude Code na přihlášení z prostředí
    # dřív, než stránka stihne otevřít první tab.
    try:
        predplatne.prepare_space()
    except Exception as exc:
        core.log_error("prostor: příprava Claude Code selhala", exc)
    # Web od správce (HUB_WEB) do ~/.claude/hub-web.json — dřív, než se otevře
    # první tab: ten si podle souboru skládá pokyn a proměnnou už nezdědí.
    try:
        from . import web
        web.pri_startu()
    except Exception as exc:
        core.log_error("prostor: zápis webu selhal", exc)
    token = secrets.token_urlsafe(24)
    httpd = _otevrit_server(token)
    port = httpd.server_address[1]
    httpd.origins = {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}
    threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.2},
                     daemon=True).start()
    threading.Thread(target=watch_autosave, daemon=True).start()
    prohlizec.INSTANCE = core.INSTANCE
    prohlizec.CESTY = lambda: {sid: (s.path or "") for sid, s in HUB.sessions.items()
                               if not getattr(s, "exited", False)}
    prohlizec.CHATY = lambda: {sid: (getattr(s, "chat_id", "") or "") for sid, s in HUB.sessions.items()
                               if not getattr(s, "exited", False)}
    threading.Thread(target=prohlizec.migruj, args=(core.log,), daemon=True).start()
    # Co se stáhne ve vestavěném prohlížeči, jde do ~/Stažené a okna to nabídnou.
    stazene.spust(HUB.broadcast)
    fronta.spust()
    # Nepoužívaný prohlížeč zavřít — sám se spustí, až ho někdo bude chtít.
    if prohlizec.povoleno(bool(core.CONFIG.get("server_mode"))):
        threading.Thread(target=prohlizec.hlidej_necinnost, args=(core.log,), daemon=True).start()
    threading.Thread(target=chats.warm_up, daemon=True).start()
    threading.Thread(target=HUB.hlidej_nazvy, name="nazvy-tabu", daemon=True).start()
    # První /api/state chce doctor() (agenti v PATH) — ať je připravený, než
    # se okno rozjede, místo aby na něj čekalo první načtení stránky.
    threading.Thread(target=_zahrat_doctor, daemon=True).start()
    # Taby průběžně na disk — po pádu nebo zabití se appka vrátí, kde byla.
    threading.Thread(target=restart.keep_saving,
                     args=(HUB, "uspani" if core.on_gateway() else "zavreni"),
                     daemon=True).start()
    # Most na počítač: na počítači čeká na úkoly od Clauda ze serveru (když je
    # zapnutý), v prostoru na serveru napojí Claude Code jeho MCP server.
    pocitac.OPEN_TAB = open_ukol_tab
    pocitac.NOTIFY = HUB.broadcast
    pocitac.HOOK_URL = f"http://127.0.0.1:{port}"
    pocitac.start()
    # Počítač počítá, kopie chatů je na serveru (hub/zrcadlo.py).
    from . import zrcadlo
    zrcadlo.start(HUB)
    # Napojení ze serveru i v Claudovi na počítači (hub/mcp_pc.py).
    from . import mcp_pc
    mcp_pc.start()
    # Spotřeba z počítače do prostoru na serveru (hub/spotreba_pc.py) a v prostoru
    # hook, který hlídá stopku od správce i v terminálu (hub/stopka.py).
    from . import spotreba_pc, stopka
    spotreba_pc.start()
    stopka.pri_startu()          # rychlé (jeden soubor) a dřív, než se otevře první tab
    connect.start_shared_sync()
    # Gemini jako pomocník Clauda: napojit/odpojit MCP podle toho, jestli je
    # agy nainstalovaný (hub/gemini.py). Zkušební hub nic nemění.
    from . import gemini
    gemini.spust_na_pozadi()
    if core.on_gateway():
        threading.Thread(target=core.hlidej_pamet, args=(HUB.broadcast,),
                         daemon=True).start()
        threading.Thread(target=core.hlidej_skripty, daemon=True).start()
    # Nečinné taby uspat (hub/uspavani.py) — v prostoru na serveru výchozí.
    threading.Thread(target=uspavani.hlidej, args=(HUB,), daemon=True).start()
    # Vše vytvořené při startu (moduly, konfigurace) se vyřadí ze sledování
    # cyklického sběrače — jeho průchody jsou pak levnější.
    try:
        import gc
        gc.collect()
        gc.freeze()
    except Exception:
        pass
    return httpd, f"http://127.0.0.1:{port}/?t={urllib.parse.quote(token)}"



def open_ukol_tab(title, path, prompt, ukol=""):
    """Tab s Claude Code pro úkol, který nechal Claude ze serveru (hub/pocitac.py).

    Otevírá ho hub, ne okno — okno může být zrovna v prostoru na serveru, nebo
    zavřené. Otevřená okna tab dostanou jako každý tab z jiného okna."""
    session = HUB.open("project", path, title, 120, 32, agent="claude", prompt=prompt, ukol=ukol)
    core.log(f"tab otevřen: úkol ze serveru {session.path or '~'} [claude]")
    HUB.broadcast({"t": "tab-opened", **session.info()})
    return session.info()


def watch_autosave():
    """Hlídá log automatického ukládání a každé uložení ohlásí oknům.

    Ukládá se v odpojeném procesu (tab už je zavřený, hub možná taky), takže
    jiná cesta zpátky než přes soubor není. Stačí se dívat na jeho velikost.
    """
    try:
        seen = os.path.getsize(core.AUTOSAVE_LOG)
    except OSError:
        seen = 0
    while True:
        time.sleep(4)
        try:
            size = os.path.getsize(core.AUTOSAVE_LOG)
        except OSError:
            continue
        if size < seen:                  # log se zkrátil (rotace)
            seen = 0
        if size == seen:
            continue
        try:
            with open(core.AUTOSAVE_LOG, "rb") as fh:
                fh.seek(seen)
                chunk = fh.read(size - seen)
        except OSError:
            continue
        seen = size
        for line in chunk.decode("utf-8", "replace").splitlines():
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if entry.get("status") == "saved":
                HUB.broadcast({"t": "memory-saved", "files": entry.get("files") or [],
                               "projects": entry.get("projects") or []})
            elif entry.get("status") == "error":
                core.log(f"paměť se neuložila: {entry.get('detail')}", "warn")


# ── the phone listener ───────────────────────────────────────────────────────
# Kept apart from the desktop one on purpose: turning the phone access on or off
# must never touch the loopback server the open window is talking to.
REMOTE = {"server": None, "mode": "", "url": "", "note": ""}
_REMOTE_LOCK = threading.Lock()


def remote_status():
    """Everything the settings screen needs to draw the phone section."""
    tail = remote.tailscale_state()
    with _REMOTE_LOCK:
        running = REMOTE["server"] is not None
        state = {"running": running, "mode": REMOTE["mode"],
                 "url": REMOTE["url"], "note": REMOTE["note"]}
    state["port"] = remote.port()
    state["tailscale"] = tail
    state["wanted"] = bool(core.CONFIG.get("remote_enabled"))
    return state


def start_remote():
    """Bring the phone listener up. Returns the same dict as remote_status()."""
    stop_remote()
    tail = remote.tailscale_state()
    if not tail["installed"]:
        return _remote_failed("Tailscale na tomhle stroji není nainstalovaný.")
    if not tail["running"]:
        return _remote_failed("Tailscale běží, ale nejsi přihlášený — spusť "
                              "`tailscale up`.")

    token = remote.token()
    port = remote.port()
    note = ""
    mode = "serve"
    address = ("127.0.0.1", port)
    ok, message = remote.serve_start(port)
    if not ok:
        # No TLS front — bind straight to the tailnet address instead.
        if not tail["ip"]:
            return _remote_failed(message or "Tailscale nemá přidělenou adresu.")
        mode, address, note = "bind", (tail["ip"], port), message

    try:
        httpd = HubHTTPServer(token, address, remote=True)
    except OSError as exc:
        if mode == "serve":
            remote.serve_stop()
        return _remote_failed(f"Port {port} nejde obsadit: {exc}")

    httpd.origins = {f"https://{tail['host']}", f"http://{tail['host']}",
                     f"http://{tail['ip']}:{port}", f"http://127.0.0.1:{port}"}
    threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.2},
                     daemon=True).start()
    with _REMOTE_LOCK:
        REMOTE.update(server=httpd, mode=mode, note=note,
                      url=remote.url(mode, tail["host"], tail["ip"], port, token))
    core.log(f"telefon: {mode} na {address[0]}:{port}")
    return remote_status()


def _remote_failed(note):
    with _REMOTE_LOCK:
        REMOTE.update(server=None, mode="", url="", note=note)
    core.log(f"telefon: nespuštěno — {note}")
    return remote_status()


def stop_remote():
    with _REMOTE_LOCK:
        httpd, mode = REMOTE["server"], REMOTE["mode"]
        REMOTE.update(server=None, mode="", url="", note="")
    if httpd:
        httpd.shutdown()
        httpd.server_close()
    if mode == "serve":
        remote.serve_stop()
