"""
Most do prohlížeče uživatele — Claude v prostoru vidí, co má člověk otevřené
v Chromu, Firefoxu, Edgi… s rozšířením Aladin.

Stejně jako most na počítač (gateway/pocitac.py) staví spojení prohlížeč:
otevřený panel rozšíření se tokenem zařízení ptá brány, jestli pro něj něco
nemá (`/gw/prohlizec/poll`, dotaz se drží až POLL_WAIT), úkol provede a výsledek
pošle zpátky (`/gw/prohlizec/vysledek`). Prostor volá přes most na počítač
(tools/pocitac_mcp.py → `/gw/pocitac/volani`, op `prohlizec`) a brána úkol
předá prohlížeči téhož účtu.

Prohlížeč je „připojený“, jen dokud má člověk otevřený panel Aladina — zavřením
panelu Claude přestane vidět. Čtení (karty, text a HTML stránky, screenshot)
jde hned; akce (klik, psaní, otevření adresy, posun) jen se souhlasem člověka
v panelu, podle jeho nastavení. Banky a platební brány rozšíření odmítne.
"""
import re
import secrets
import threading
import time

CTENI = frozenset({"karty", "stranka", "snimek"})
# Akce na stránce: rozšíření se před každou zeptá člověka (nebo podle jeho nastavení).
AKCE = frozenset({"klik", "psat", "otevrit", "posun"})
OPS = CTENI | AKCE
POLL_WAIT = 25
ONLINE_GRACE = 40
FORGET_AFTER = 6 * 3600
MAX_QUEUE = 16
TIMEOUT = 45                # rozšíření odpovídá hned; déle = panel se mezitím zavřel
TIMEOUT_AKCE = 160          # u akce čeká rozšíření až dvě minuty na souhlas člověka

_ID = re.compile(r"[A-Za-z0-9_-]{8,64}")


def _text(value, limit=200):
    return str(value or "").strip()[:limit]


class Prohlizec:
    def __init__(self, uid, pid):
        self.uid = uid
        self.id = pid
        self.name = ""
        self.browser = ""
        self.version = ""
        self.polling = 0
        self.last_seen = 0.0
        self.queue = []
        self.last = None

    def online(self, now):
        return self.polling > 0 or now - self.last_seen < ONLINE_GRACE

    def public(self, now):
        return {"id": self.id, "name": self.name, "browser": self.browser, "version": self.version,
                "online": self.online(now), "seen_ago": 0 if self.polling else int(now - self.last_seen),
                "last": dict(self.last, ago=int(now - self.last["at"])) if self.last else None}


class Broker:
    def __init__(self):
        self._cond = threading.Condition()
        self.items = {}          # (uid, id) -> Prohlizec
        self._waiting = {}       # id úkolu -> {"event", "result", "uid", "pid"}

    # ── strana prohlížeče ────────────────────────────────────────────────────
    def poll(self, uid, info, wait=POLL_WAIT):
        info = info if isinstance(info, dict) else {}
        pid = str(info.get("id") or "")
        if not _ID.fullmatch(pid):
            raise ValueError("Chybí nebo nesedí id prohlížeče.")
        with self._cond:
            p = self.items.get((uid, pid))
            if p is None:
                p = self.items[(uid, pid)] = Prohlizec(uid, pid)
            p.name = _text(info.get("name"), 80) or "prohlížeč"
            p.browser = _text(info.get("browser"), 40)
            p.version = _text(info.get("version"), 20)
            p.polling += 1
            try:
                deadline = time.time() + max(0.0, min(float(wait), POLL_WAIT))
                while not p.queue:
                    left = deadline - time.time()
                    if left <= 0:
                        break
                    self._cond.wait(left)
                tasks, p.queue = p.queue[:4], p.queue[4:]
            finally:
                p.polling -= 1
                p.last_seen = time.time()
            self._forget_old()
        return tasks

    def requeue(self, uid, pid, tasks):
        with self._cond:
            p = self.items.get((uid, str(pid or "")))
            if p and tasks:
                p.queue[:0] = tasks
                self._cond.notify_all()

    def bye(self, uid, pid):
        with self._cond:
            p = self.items.pop((uid, str(pid or "")), None)
            if p:
                self._drop(p, "Panel Aladina v prohlížeči se právě zavřel.")
        return bool(p)

    def reply(self, uid, pid, rid, result):
        with self._cond:
            slot = self._waiting.get(str(rid or ""))
            if not slot or slot["uid"] != uid or slot["pid"] != str(pid or ""):
                return False
            slot["result"] = result
            slot["event"].set()
            return True

    # ── strana prostoru ──────────────────────────────────────────────────────
    def list(self, uid):
        now = time.time()
        with self._cond:
            mine = [p for (u, _), p in self.items.items() if u == uid and p.online(now)]
            return [p.public(now) for p in sorted(mine, key=lambda p: -p.last_seen)]

    def call(self, uid, op, args, target=""):
        """Úkol pro prohlížeč a čekání na výsledek. Nikdy nevyhodí výjimku."""
        if op not in OPS:
            return {"ok": False, "error": f"Neznámý úkol pro prohlížeč {op!r}."}
        args = args if isinstance(args, dict) else {}
        now = time.time()
        with self._cond:
            p, error = self._pick(uid, _text(target, 80), now)
            if error:
                return {"ok": False, "error": error}
            if len(p.queue) >= MAX_QUEUE:
                return {"ok": False, "prohlizec": p.name, "error": "Na prohlížeč čeká moc úkolů, zkus to za chvíli."}
            rid = secrets.token_hex(8)
            slot = {"event": threading.Event(), "result": None, "uid": uid, "pid": p.id}
            self._waiting[rid] = slot
            p.queue.append({"id": rid, "op": op, "args": args})
            p.last = {"op": op, "at": now}
            self._cond.notify_all()
        limit = TIMEOUT_AKCE if op in AKCE else TIMEOUT
        done = slot["event"].wait(limit)
        with self._cond:
            self._waiting.pop(rid, None)
            queued = any(t["id"] == rid for t in p.queue)
            if queued:
                p.queue = [t for t in p.queue if t["id"] != rid]
        if not done:
            return {"ok": False, "prohlizec": p.name, "error": (
                "Prohlížeč úkol nepřevzal — panel Aladina se asi zavřel." if queued
                else f"Prohlížeč neodpověděl do {limit} s.")}
        result = slot["result"] or {}
        if result.get("ok"):
            return {"ok": True, "prohlizec": p.name, "result": result.get("result")}
        return {"ok": False, "prohlizec": p.name, "error": _text(result.get("error"), 2000) or "V prohlížeči se to nepovedlo."}

    def _pick(self, uid, target, now):
        mine = sorted([p for (u, _), p in self.items.items() if u == uid and p.online(now)],
                      key=lambda p: -p.last_seen)
        if target:
            low = target.lower()
            hits = [p for p in mine if p.id == target or p.name.lower() == low]
            if not hits:
                names = ", ".join(p.name for p in mine) or "žádný"
                return None, f"Prohlížeč {target!r} není připojený. Připojené: {names}."
            return hits[0], ""
        if not mine:
            return None, ("Žádný prohlížeč uživatele teď není připojený. Uživatel musí mít "
                          "v prohlížeči otevřený panel rozšíření Aladin (ikona Aladina v liště nebo "
                          "Alt+Shift+A), přihlášený k tomuhle serveru, a v jeho nastavení zapnuté "
                          "„Aladin smí sám nahlížet do prohlížeče“.")
        # Víc otevřených panelů: ten, který se ozval naposled (člověk je u něj).
        return mine[0], ""

    def _drop(self, p, why):
        for task in p.queue:
            slot = self._waiting.get(task["id"])
            if slot:
                slot["result"] = {"ok": False, "error": why}
                slot["event"].set()
        p.queue = []

    def _forget_old(self):
        now = time.time()
        for key, p in list(self.items.items()):
            if not p.polling and now - p.last_seen > FORGET_AFTER:
                self.items.pop(key, None)
