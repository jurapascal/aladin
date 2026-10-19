"""
Každý chat má v prohlížeči vlastní karty.

Všechny sessions Claude Code sdílejí jeden Chromium (hub/prohlizec.py) a tím
i jeden profil: přihlášení, cookies, uložená hesla. Playwright MCP ale po
připojení přes CDP převezme všechny karty, které v prohlížeči jsou, a první
z nich si vezme jako svou. Dva chaty by si tak přetahovaly jednu stránku.

Most (tools/playwright_bridge.py) proto Playwright MCP nepřipojí na Chromium
přímo, ale přes tenhle proxy. Ten pustí k Playwrightu jen karty, které si
session sama otevřela (a okna, která z nich vyskočila); cizí karty před ní
schová. Co komu patří, zapisuje do `~/.claude/browser-sessions/<pid>.json`
spolu se značkou tabu z hubu (HUB_TAB) a id konverzace, takže hub ví, ke
kterému chatu karta patří, a dá se mezi nimi přepínat. Po restartu appky
karty převezme ta session, která pokračuje v téže konverzaci — ne první,
která naběhne ve stejné složce.

Jen standardní knihovna. Poslouchá jen na loopbacku a jen na adrese s náhodným
tokenem. `Browser.close` od session nepustí — prohlížeč je společný.
"""
import base64
import hashlib
import json
import os
import re
import secrets
import socket
import struct
import threading
import time

from . import prohlizec

PROXY_ID = 2_000_000_000          # od tohohle čísla jsou příkazy, které posílá sám proxy
_KONEC_SES = re.compile(r',"sessionId":"([^"]+)"\}$')


def slozka():
    return os.path.join(prohlizec.claude_dir(), "browser-sessions")


class _Klient:
    """Serverová strana websocketu (spojení od Playwrightu)."""

    def __init__(self, sock, zbytek=b""):
        self.sock = sock
        self._buf = zbytek
        self._lock = threading.Lock()
        self.closed = False

    def _read(self, n):
        while len(self._buf) < n:
            chunk = self.sock.recv(1 << 16)
            if not chunk:
                raise OSError("spojení zavřeno")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def recv(self):
        data, op0 = b"", None
        while True:
            b1, b2 = self._read(2)
            fin, op = b1 & 0x80, b1 & 0x0F
            n = b2 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._read(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._read(8))[0]
            mask = self._read(4) if b2 & 0x80 else b""
            payload = self._read(n)
            if mask:
                payload = _odmaskuj(payload, mask)
            if op == 0x8:
                return None
            if op == 0x9:
                self._send(payload, 0xA)
                continue
            if op == 0xA:
                continue
            if op != 0x0:
                op0, data = op, payload
            else:
                data += payload
            if fin:
                return data.decode("utf-8", "replace") if op0 == 0x1 else ""

    def _send(self, payload, op=0x1):
        head = bytearray([0x80 | op])
        n = len(payload)
        if n < 126:
            head.append(n)
        elif n < 65536:
            head += bytes([126]) + struct.pack(">H", n)
        else:
            head += bytes([127]) + struct.pack(">Q", n)
        with self._lock:
            self.sock.sendall(bytes(head) + payload)

    def send(self, text):
        self._send(text.encode("utf-8"))

    def close(self):
        if self.closed:
            return
        self.closed = True
        try:
            self._send(b"", 0x8)
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass


def _odmaskuj(data, mask):
    # XOR po celých číslech je řádově rychlejší než po bajtech (snímky mají megabajty).
    n = len(data)
    m = int.from_bytes((mask * (n // 4 + 1))[:n], "little")
    return (int.from_bytes(data, "little") ^ m).to_bytes(n, "little")


class Evidence:
    """Které karty patří téhle session — sdílené všemi jejími spojeními."""

    def __init__(self, znacka, chat=""):
        self.znacka = znacka
        self.chat = chat
        self.karty = set()
        self._lock = threading.Lock()
        self.soubor = os.path.join(slozka(), f"{os.getpid()}.json")

    def pridej(self, tid):
        with self._lock:
            if tid in self.karty:
                return
            self.karty.add(tid)
        self.uloz()

    def odeber(self, tid):
        with self._lock:
            if tid not in self.karty:
                return
            self.karty.discard(tid)
        self.uloz()

    def uloz(self):
        try:
            os.makedirs(slozka(), exist_ok=True)
            tmp = self.soubor + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"tab": self.znacka, "chat": self.chat, "pid": os.getpid(),
                           "pages": sorted(self.karty), "cwd": os.getcwd(), "ts": time.time()}, fh)
            os.replace(tmp, self.soubor)
        except OSError:
            pass

    def uklid(self, zavrit_karty=False):
        """Session končí. Karty se NEzavírají: při restartu appky nebo aktualizaci
        končí všechny sessions naráz a po obnově by člověk přišel o rozdělanou
        práci v prohlížeči (přihlášení, vyplněné formuláře). Zápis zůstane jako
        „osiřelý" a převezme ho nová session ve stejné složce (`adoptuj`);
        co nikdo nepřevezme, hub za den zavře (`vlastnici`)."""
        if zavrit_karty:
            for tid in list(self.karty):
                try:
                    prohlizec.zavri_kartu(tid)
                except Exception:
                    pass
            try:
                os.remove(self.soubor)
            except OSError:
                pass
            return
        self.uloz()

    def adoptuj(self):
        """Převezme karty osiřelých zápisů téže konverzace (po restartu appky
        se session otevře znovu a prohlížeč pokračuje tam, kde skončil).
        Zápis bez id konverzace (starší verze, Claude mimo hub) se páruje
        podle složky jako dřív — jen když ani tahle session id nemá. Podle
        složky si dřív první chat v ~ vzal karty všech ostatních chatů v ~."""
        try:
            zive = {p["id"] for p in prohlizec._get("/json/list", 1.5) if p.get("type") == "page"}
            names = os.listdir(slozka())
        except Exception:
            return
        moje = os.path.abspath(os.getcwd())
        for name in names:
            path = os.path.join(slozka(), name)
            if not name.endswith(".json") or path == self.soubor:
                continue
            try:
                with open(path, encoding="utf-8") as fh:
                    d = json.load(fh)
            except (OSError, ValueError):
                continue
            pid = d.get("pid")
            if not isinstance(pid, int) or _zije(pid):
                continue
            if self.chat or d.get("chat"):
                if d.get("chat") != self.chat:
                    continue
            elif os.path.abspath(d.get("cwd") or "") != moje:
                continue
            for tid in d.get("pages") or []:
                if tid in zive:
                    with self._lock:
                        self.karty.add(tid)
            try:
                os.remove(path)
            except OSError:
                pass
        self.uloz()


class Spojeni:
    """Jedno spojení Playwrightu ↔ Chromium s filtrem cizích karet."""

    def __init__(self, klient, evidence):
        self.k = klient
        self.ev = evidence
        self.ch = None
        self._lock = threading.Lock()
        self._vytvari = set()      # id požadavků Target.createTarget, na které se čeká
        self._seznam = set()       # id požadavků Target.getTargets
        self._drzene = []          # [(targetId, sessionId, zpráva)] — čeká se, jestli je karta naše
        self._drzene_ses = {}      # sessionId → zprávy, které přišly za ní
        self._blok = set()         # sessionId cizích karet, od kterých jsme se odpojili
        self._cizi = set()         # targetId cizích karet
        self._sesn = {}            # sessionId → targetId našich karet
        self._pid = PROXY_ID

    def bez(self):
        try:
            prohlizec.ensure()
            info = prohlizec._get("/json/version", 3)
            self.ch = prohlizec._WS(info["webSocketDebuggerUrl"])
        except Exception:
            self.k.close()
            return
        t = threading.Thread(target=self._z_chromia, daemon=True)
        t.start()
        try:
            while True:
                raw = self.k.recv()
                if raw is None:
                    break
                self._od_klienta(raw)
        except OSError:
            pass
        finally:
            self.ch.close()
            self.k.close()

    # Playwright → Chromium
    def _od_klienta(self, raw):
        try:
            m = json.loads(raw)
        except ValueError:
            return
        if not m.get("sessionId"):
            met = m.get("method")
            if met == "Browser.close":
                # Prohlížeč je společný — zavřít ho nesmí jedna session ostatním.
                self.k.send(json.dumps({"id": m.get("id"), "result": {}}))
                return
            with self._lock:
                if met == "Target.createTarget":
                    self._vytvari.add(m.get("id"))
                elif met == "Target.getTargets":
                    self._seznam.add(m.get("id"))
        elif m["sessionId"] in self._blok:
            return
        self.ch.send(raw)

    def _sam(self, method, sid=None, **params):
        """Příkaz od proxy samotného (`sid` = poslat do session karty)."""
        with self._lock:
            self._pid += 1
            mid = self._pid
        msg = {"id": mid, "method": method, "params": params}
        if sid:
            msg["sessionId"] = sid
        self.ch.send(json.dumps(msg))

    def _zablokuj(self, tid, sid, ceka):
        self._cizi.add(tid)
        self._blok.add(sid)
        self._drzene_ses.pop(sid, None)
        if ceka:
            self._sam("Runtime.runIfWaitingForDebugger", sid=sid)
        self._sam("Target.detachFromTarget", sessionId=sid)      # na browser session, sessionId v params

    def _pust(self, tid, sid, raw):
        self.ev.pridej(tid)
        self._sesn[sid] = tid
        self.k.send(raw)
        for z in self._drzene_ses.pop(sid, []):
            self.k.send(z)

    def _rozhodni_drzene(self, nase=None):
        """Po odpovědi na createTarget: naše karta jde dál (před odpovědí —
        Playwright ji v tu chvíli už musí znát), cizí se schovají, až se
        nečeká na žádnou další."""
        zbyva = []
        for tid, sid, raw, ceka in self._drzene:
            if tid == nase:
                self._pust(tid, sid, raw)
            elif not self._vytvari:
                self._zablokuj(tid, sid, ceka)
            else:
                zbyva.append((tid, sid, raw, ceka))
        self._drzene = zbyva

    # Chromium → Playwright
    def _z_chromia(self):
        try:
            while True:
                raw = self.ch.recv()
                if raw is None:
                    break
                if raw:
                    self._od_chromia(raw)
        except OSError:
            pass
        finally:
            self.k.close()

    def _od_chromia(self, raw):
        # Rychlá cesta: zprávy pro konkrétní kartu (snímky, události stránky)
        # se neparsují celé — Chromium píše sessionId vždycky na konec.
        if '"method":"Target.' not in raw[:200]:
            konec = _KONEC_SES.search(raw[-160:])
            if konec:
                sid = konec.group(1)
                if sid in self._blok:
                    return
                if sid in self._drzene_ses:
                    self._drzene_ses[sid].append(raw)
                    return
                self.k.send(raw)
                return
        try:
            m = json.loads(raw)
        except ValueError:
            return
        sid = m.get("sessionId")
        if sid:
            if sid in self._blok:
                return
            if sid in self._drzene_ses:
                self._drzene_ses[sid].append(raw)
                return
            self.k.send(raw)        # karta už prošla filtrem (iframe, worker…)
            return
        mid = m.get("id")
        if mid is not None:
            if mid > PROXY_ID:
                return
            with self._lock:
                vytvari = mid in self._vytvari
                self._vytvari.discard(mid)
                seznam = mid in self._seznam
                self._seznam.discard(mid)
            if vytvari:
                self._rozhodni_drzene(((m.get("result") or {}).get("targetId")))
            elif seznam and m.get("result"):
                m["result"]["targetInfos"] = [t for t in m["result"].get("targetInfos") or []
                                              if t.get("type") != "page" or t.get("targetId") in self.ev.karty]
                raw = json.dumps(m)
            self.k.send(raw)
            return
        met = m.get("method")
        par = m.get("params") or {}
        if met == "Target.attachedToTarget":
            ti = par.get("targetInfo") or {}
            tid, s = ti.get("targetId"), par.get("sessionId")
            if ti.get("type") != "page":
                self.k.send(raw)
            elif tid in self.ev.karty or ti.get("openerId") in self.ev.karty:
                self._pust(tid, s, raw)          # naše karta, nebo okno, které z ní vyskočilo
            elif self._vytvari:
                self._drzene.append((tid, s, raw, bool(par.get("waitingForDebugger"))))
                self._drzene_ses[s] = []
            else:
                self._zablokuj(tid, s, bool(par.get("waitingForDebugger")))
            return
        if met == "Target.detachedFromTarget":
            s = par.get("sessionId")
            if s in self._blok:
                self._blok.discard(s)
                return
            tid = self._sesn.pop(s, None)
            if tid:
                self.ev.odeber(tid)
            self.k.send(raw)
            return
        if met in ("Target.targetCreated", "Target.targetInfoChanged"):
            ti = par.get("targetInfo") or {}
            if ti.get("type") == "page" and ti.get("targetId") not in self.ev.karty:
                return
        elif met in ("Target.targetDestroyed", "Target.targetCrashed"):
            if par.get("targetId") in self._cizi:
                if met == "Target.targetDestroyed":
                    self._cizi.discard(par.get("targetId"))
                return
            if met == "Target.targetDestroyed":
                self.ev.odeber(par.get("targetId"))
        self.k.send(raw)


class Proxy:
    """Naslouchá na 127.0.0.1 na náhodném portu, adresa s tokenem v cestě."""

    def __init__(self, znacka="", chat=""):
        self.ev = Evidence(znacka, chat)
        self.token = secrets.token_hex(16)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.bind((prohlizec.HOST, 0))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]

    @property
    def endpoint(self):
        return f"ws://{prohlizec.HOST}:{self.port}/devtools/browser/{self.token}"

    def start(self):
        self.ev.adoptuj()
        self.ev.uloz()
        threading.Thread(target=self._prijimej, name="cdp-proxy", daemon=True).start()
        return self

    def _prijimej(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._obsluz, args=(conn,), daemon=True).start()

    def _obsluz(self, conn):
        try:
            conn.settimeout(10)
            head = b""
            while b"\r\n\r\n" not in head:
                chunk = conn.recv(4096)
                if not chunk or len(head) > 65536:
                    conn.close()
                    return
                head += chunk
            text, zbytek = head.split(b"\r\n\r\n", 1)
            radky = text.decode("latin-1").split("\r\n")
            cesta = radky[0].split(" ")[1] if len(radky[0].split(" ")) > 1 else ""
            hlav = {r.split(":", 1)[0].strip().lower(): r.split(":", 1)[1].strip() for r in radky[1:] if ":" in r}
            if not secrets.compare_digest(cesta, f"/devtools/browser/{self.token}") or "sec-websocket-key" not in hlav:
                conn.sendall(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
                conn.close()
                return
            accept = base64.b64encode(hashlib.sha1((hlav["sec-websocket-key"] +
                                                    "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
            conn.sendall(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                          f"Sec-WebSocket-Accept: {accept}\r\n\r\n").encode())
            conn.settimeout(None)
            Spojeni(_Klient(conn, zbytek), self.ev).bez()
        except OSError:
            try:
                conn.close()
            except OSError:
                pass

    def stop(self, zavrit_karty=False):
        try:
            self.sock.close()
        except OSError:
            pass
        self.ev.uklid(zavrit_karty)


def vlastnici():
    """targetId → značka tabu (HUB_TAB) podle zápisů běžících mostů.
    Zápisy po mostech, které už neběží, se smažou."""
    out = {}
    try:
        names = os.listdir(slozka())
    except OSError:
        return out
    for name in names:
        if not name.endswith(".json"):
            continue
        path = os.path.join(slozka(), name)
        try:
            with open(path, encoding="utf-8") as fh:
                d = json.load(fh)
        except (OSError, ValueError):
            continue
        pid = d.get("pid")
        if isinstance(pid, int) and not _zije(pid):
            # Osiřelý zápis: karty nikdo neovládá, ale čekají na novou session
            # ve stejné složce. Co zůstane dýl než den, se zavře.
            if time.time() - float(d.get("ts") or 0) > 86400:
                for tid in d.get("pages") or []:
                    try:
                        prohlizec.zavri_kartu(tid)
                    except Exception:
                        pass
                try:
                    os.remove(path)
                except OSError:
                    pass
            continue
        for tid in d.get("pages") or []:
            out[tid] = d.get("tab") or ""
    return out


def sirotci():
    """[(cwd, [targetId…], chat)] ze zápisů mostů, které už neběží (po restartu appky).
    Karty tam čekají na novou session; hub je do té doby přiřadí tabu ve stejné
    složce, ať je ikonka a lišta vidět hned a ne až po startu Clauda."""
    out = []
    try:
        names = os.listdir(slozka())
    except OSError:
        return out
    for name in names:
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(slozka(), name), encoding="utf-8") as fh:
                d = json.load(fh)
        except (OSError, ValueError):
            continue
        pid = d.get("pid")
        if isinstance(pid, int) and not _zije(pid) and d.get("pages"):
            out.append((os.path.abspath(d.get("cwd") or ""), list(d["pages"]), d.get("chat") or ""))
    return out


def _zije(pid):
    if os.name == "nt":
        try:
            import ctypes
            h = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)   # QUERY_LIMITED_INFORMATION
            if not h:
                return False
            code = ctypes.c_ulong()
            ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code))
            ctypes.windll.kernel32.CloseHandle(h)
            return code.value == 259                                      # STILL_ACTIVE
        except Exception:
            return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
