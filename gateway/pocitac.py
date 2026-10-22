"""
Most do počítače uživatele — Claude v prostoru na serveru dosáhne i na soubory
a příkazy na počítači, ze kterého se k prostoru přihlásil.

Spojení staví počítač, ne server. Hub na počítači (`hub/pocitac.py`) se tokenem
zařízení ptá brány, jestli pro něj něco nemá (`/gw/pocitac/poll`, brána dotaz
drží, dokud úkol nepřijde nebo neuplyne POLL_WAIT), úkol provede a výsledek
pošle zpátky (`/gw/pocitac/vysledek`). Na počítači se tak nic neotevírá — žádný
port ani tunel; projde to každou sítí, kudy projde přihlášení k serveru.

Z prostoru volá MCP server `tools/pocitac_mcp.py` (`/gw/pocitac/volani`) se
žetonem, který prostor dostal od brány při startu v prostředí. Brána úkol
zařadí počítači, počká na odpověď a vrátí ji. Do obsahu nesahá.

Co smí, rozhoduje počítač: přístup (jen čtení / plný) se zapíná v appce na něm
a ověřuje ho u každého úkolu znovu. Brána ho zná jen proto, aby Claude dostal
srozumitelnou odpověď dřív, než úkol vůbec odejde.

Úkoly na později: když počítač připojený není, nechá mu Claude z prostoru
zadání (a soubory). Brána ho drží na disku, a jakmile se počítač ozve, nabídne
mu ho v odpovědi na dotaz (`ukoly`). Počítač si ho stáhne (`/gw/pocitac/ukol`,
take), uloží, potvrdí (prevzato) a dodělá ho Claude Code v tabu na počítači.
Stav se hlásí zpátky, ať Claude v prostoru i člověk vidí, kde úkol je.
"""
import base64
import json
import os
import re
import secrets
import shutil
import threading
import time

READ_OPS = frozenset({"info", "ls", "read", "find", "download", "sessions", "session_read"})
# Bez ptaní povolené i při „jen čtení“: seznam procesů a oznámení v okně appky.
SAFE_OPS = frozenset({"ps", "notify"})
# Mění počítač nebo vidí, co má člověk na obrazovce a ve schránce.
ASK_OPS = frozenset({"write", "edit", "upload", "run", "session_send", "screenshot", "clipboard_get",
                     "clipboard_set", "open"})
ALL_OPS = READ_OPS | SAFE_OPS | ASK_OPS
# „ptat“ = čtení bez ptaní, vše ostatní jen po souhlasu člověka (appka na počítači
# nebo hub v prostoru; rozhoduje počítač, brána jen předává).
ACCESS = {"cteni": READ_OPS | SAFE_OPS, "ptat": ALL_OPS, "vse": ALL_OPS}
ACCESS_LABEL = {"cteni": "jen čtení", "ptat": "ptá se na souhlas", "vse": "plný přístup"}
LEVELS = ("", "cteni", "ptat", "vse")
APPROVE_WAIT = 120      # jak dlouho počítač čeká na souhlas člověka (hub/pocitac.py)
DECISIONS = ("jednou", "30min", "zamitnout")
# Žádost z tabu s úkolem ze serveru (hub/pocitac.py, povoleni_*): Claude Code
# na počítači chce souhlas s nástrojem, ptá se (AskUserQuestion) nebo chce
# schválit plán. Čeká, dokud neodpoví člověk tady nebo v terminálu.
SESSION_DECISIONS = ("povolit", "zamitnout", "odpoved")

POLL_WAIT = 25          # jak dlouho brána drží dotaz počítače, když nemá úkol
ONLINE_GRACE = 40       # počítač, který se tak dlouho neozval, je pryč
FORGET_AFTER = 24 * 3600
MAX_QUEUE = 32          # úkoly čekající na jeden počítač
BATCH = 8               # kolik úkolů si počítač odnese jedním dotazem
RUN_MAX = 600

_ID = re.compile(r"[A-Za-z0-9_-]{8,64}")

# Úkoly na později. Zadání dostane Claude na počítači jako úvodní zprávu tabu —
# příkazová řádka na Windows víc než pár desítek tisíc znaků neunese.
UKOL_TEXT = 8000
UKOL_TITLE = 120
UKOL_FILES = 20
UKOL_BYTES = 15 * 1024 * 1024       # přílohy dohromady, jako jeden přenos mostem
CHAT_BYTES = 45 * 1024 * 1024       # totéž u chatu z prostoru (hub/prenos.py, balíček po kusech)
UKOL_WAITING = 20                   # nevyzvednutých úkolů na účet
UKOL_KEEP = 30 * 86400              # nevyzvednutý úkol po měsíci propadne
UKOL_HISTORY = 14 * 86400           # vyřízený se ukazuje ještě dva týdny
UKOL_STATES = {"ceka": "čeká na počítač", "prevzato": "na počítači čeká na spuštění",
               "spusteno": "spuštěný na počítači", "zahozeno": "zahozený na počítači"}
_UKOL_ID = re.compile(r"[a-f0-9]{16}")


def _text(value, limit=200):
    return str(value or "").strip()[:limit]


def summary(op, args):
    """Krátký popis úkolu pro člověka — štítek v hlavičce prostoru."""
    args = args if isinstance(args, dict) else {}
    if op == "run":
        return _text(args.get("command"), 160)
    if op == "open":
        return _text(args.get("target"), 160)
    if op == "clipboard_set":
        return "schránka"
    if op in ("screenshot", "clipboard_get", "ps"):
        return {"screenshot": "snímek obrazovky", "clipboard_get": "schránka", "ps": "procesy"}[op]
    if op == "notify":
        return _text(args.get("message"), 160)
    if op in ("sessions", "session_read", "session_send"):
        return {"sessions": "sessions", "session_read": f"čtení tabu {_text(args.get('tab'), 10)}",
                "session_send": f"zpráva do tabu {_text(args.get('tab'), 10)}"}[op]
    return _text(args.get("path"), 160)


def timeout_for(op, args):
    """Jak dlouho brána na výsledek čeká. Počítá brána, ne volající."""
    if op == "run":
        try:
            limit = int((args or {}).get("timeout") or 120)
        except (TypeError, ValueError):
            limit = 120
        return max(1, min(RUN_MAX, limit)) + 30
    return 150


def file_name(raw):
    """Jméno přílohy bez cesty a znaků, které Windows v názvu nesnese. "" = neplatné."""
    name = str(raw or "").replace("\\", "/").rsplit("/", 1)[-1]
    name = re.sub(r'[\x00-\x1f<>:"|?*]', "_", name).strip()[:120].rstrip(". ")
    return "" if name in ("", ".", "..") else name


def decode_files(raw, limit=None):
    """Přílohy úkolu [{"name", "data" (base64)}] → ([(jméno, bajty)], chyba)."""
    limit = limit or UKOL_BYTES
    if raw in (None, ""):
        return [], ""
    if not isinstance(raw, list):
        return None, "Přílohy musí být seznam."
    if len(raw) > UKOL_FILES:
        return None, f"K úkolu jde přiložit nejvýš {UKOL_FILES} souborů — zabal je do archivu."
    out, seen, total = [], set(), 0
    for item in raw:
        if not isinstance(item, dict):
            return None, "Nečitelná příloha."
        name = file_name(item.get("name"))
        if not name:
            return None, "Příloha nemá platné jméno."
        encoded = str(item.get("data") or "")
        total += len(encoded) * 3 // 4
        if total > limit + 3:
            return None, (f"Přílohy mají dohromady přes {limit // (1024 * 1024)} MB — "
                          "zabal je nebo pošli jen to podstatné.")
        try:
            data = base64.b64decode(encoded, validate=True)
        except ValueError:
            return None, f"Příloha {name} je poškozená."
        stem, ext = os.path.splitext(name)
        n = 2
        while name.casefold() in seen:
            name = f"{stem} ({n}){ext}"
            n += 1
        seen.add(name.casefold())
        out.append((name, data))
    return out, ""


def _questions(raw):
    """Otázky z AskUserQuestion, jak je poslal počítač — jen co karta ukáže."""
    out = []
    for q in (raw if isinstance(raw, list) else [])[:4]:
        if not isinstance(q, dict) or not _text(q.get("question"), 500):
            continue
        opts = [{"label": _text(o.get("label"), 120), "description": _text(o.get("description"), 300)}
                for o in (q.get("options") if isinstance(q.get("options"), list) else [])[:6]
                if isinstance(o, dict) and _text(o.get("label"), 120)]
        out.append({"question": _text(q.get("question"), 500), "header": _text(q.get("header"), 40),
                    "multi": q.get("multi") is True, "options": opts})
    return out


def _answers(questions, raw):
    """Odpovědi z karty → {text otázky: volba | [volby]}, nebo chyba."""
    if not questions:
        return None, "Na tuhle žádost se neodpovídá výběrem."
    if not isinstance(raw, dict):
        return None, "Vyber odpověď."
    out = {}
    for q in questions:
        value = raw.get(q["question"])
        if isinstance(value, list) and q["multi"]:
            value = [_text(v, 500) for v in value if _text(v, 500)][:6]
        elif isinstance(value, str):
            value = _text(value, 500)
        else:
            value = None
        if not value:
            return None, "Chybí odpověď na otázku: " + q["question"][:80]
        out[q["question"]] = value
    return out, ""


class Computer:
    """Jeden počítač jednoho účtu, jak ho brána zná z jeho dotazů."""

    def __init__(self, uid, cid):
        self.uid = uid
        self.id = cid
        self.name = ""
        self.system = ""
        self.home = ""
        self.user = ""
        self.shell = ""
        self.access = ""
        self.version = ""
        self.polling = 0
        self.last_seen = 0.0
        self.queue = []
        self.pending = []           # žádosti o souhlas, na které počítač čeká
        self.grants = []            # časově omezená povolení: [{"until"}]
        self.directives = []        # co má počítač udělat: rozhodnutí, změna přístupu
        self.last = None            # poslední úkol: {"op", "summary", "at"}
        self.offered = set()        # úkoly na později, o kterých už ví

    def online(self, now):
        return self.polling > 0 or now - self.last_seen < ONLINE_GRACE

    def public(self, now):
        return {"id": self.id, "name": self.name, "system": self.system,
                "home": self.home, "user": self.user, "shell": self.shell,
                "access": self.access,
                "access_label": ACCESS_LABEL.get(self.access, "vypnuto"),
                "pending": list(self.pending), "grants": list(self.grants),
                "version": self.version, "online": self.online(now),
                "seen_ago": 0 if self.polling else int(now - self.last_seen),
                "last": (dict(self.last, ago=int(now - self.last["at"]))
                         if self.last else None)}


class Broker:
    """Fronty úkolů pro počítače a čekání na jejich výsledky."""

    def __init__(self, ukoly_dir=""):
        self._cond = threading.Condition()
        self.computers = {}           # (uid, id počítače) -> Computer
        self._waiting = {}            # id úkolu -> {"event", "result", "uid", "cid"}
        self.ukoly_dir = ukoly_dir    # prázdné = úkoly na později vypnuté
        self._ukoly = {}              # uid -> {id úkolu -> záznam}, načtené z disku

    # ── strana počítače ──────────────────────────────────────────────────────
    def poll(self, uid, info, wait=POLL_WAIT):
        """Počítač se hlásí a čeká na úkoly. Vrací jejich seznam (i prázdný)."""
        info = info if isinstance(info, dict) else {}
        cid = str(info.get("id") or "")
        if not _ID.fullmatch(cid):
            raise ValueError("Chybí nebo nesedí id počítače.")
        with self._cond:
            comp = self.computers.get((uid, cid))
            if comp is None:
                comp = self.computers[(uid, cid)] = Computer(uid, cid)
            comp.name = _text(info.get("name"), 80) or "počítač"
            comp.system = _text(info.get("system"), 80)
            comp.home = _text(info.get("home"), 300)
            comp.user = _text(info.get("user"), 80)
            comp.shell = _text(info.get("shell"), 40)
            comp.version = _text(info.get("version"), 20)
            access = str(info.get("access") or "")
            comp.access = access if access in ACCESS else ""
            if not comp.access:
                self._drop_queue(comp, "Přístup na počítači je vypnutý.")
            self._set_pending(comp, info.get("pending"), info.get("grants"))
            comp.polling += 1
            try:
                deadline = time.time() + max(0.0, min(float(wait), POLL_WAIT))
                # Nový úkol na později vzbudí dotaz hned, ne až po POLL_WAIT.
                while not comp.queue and not comp.directives and not (self._offer_ids(comp) - comp.offered):
                    left = deadline - time.time()
                    if left <= 0:
                        break
                    self._cond.wait(left)
                tasks, comp.queue = comp.queue[:BATCH], comp.queue[BATCH:]
            finally:
                comp.polling -= 1
                comp.last_seen = time.time()
            self._forget_old()
        return tasks

    @staticmethod
    def _set_pending(comp, pending, grants):
        """Co počítač právě čeká od člověka a co už povolil (jen pro zobrazení)."""
        out = []
        for item in (pending if isinstance(pending, list) else [])[:12]:
            if isinstance(item, dict) and _ID.fullmatch(str(item.get("id") or "")):
                session = item.get("kind") == "session"
                entry = {"id": str(item["id"]), "op": _text(item.get("op"), 20),
                         "summary": _text(item.get("summary"), 200),
                         "preview": _text(item.get("preview"), 3000 if session else 600),
                         "at": int(item.get("at") or 0)}
                if session:
                    entry.update(kind="session", tool=_text(item.get("tool"), 80),
                                 tab=_text(item.get("tab"), 80), ukol=_text(item.get("ukol"), 120),
                                 otazky=_questions(item.get("otazky")))
                out.append(entry)
        comp.pending = out
        comp.grants = [{"until": int(g.get("until") or 0)}
                       for g in (grants if isinstance(grants, list) else [])[:4]
                       if isinstance(g, dict)]

    def update_pending(self, uid, cid, pending, grants):
        """Počítač hlásí novou žádost o souhlas hned, ne až při dalším dotazu."""
        with self._cond:
            comp = self.computers.get((uid, str(cid or "")))
            if not comp:
                return False
            self._set_pending(comp, pending, grants)
            return True

    def take_directives(self, uid, cid):
        with self._cond:
            comp = self.computers.get((uid, str(cid or "")))
            if not comp or not comp.directives:
                return []
            out, comp.directives = comp.directives, []
            return out

    def decide(self, uid, cid, rid, choice, odpovedi=None):
        """Člověk v prostoru (cookie, ne žeton prostoru) rozhodl o žádosti počítače.
        Platí jednou — žádost se hned vyřadí, druhé rozhodnutí už nic nenajde."""
        with self._cond:
            comp = self._find(uid, cid)
            if not comp:
                return {"ok": False, "error": "Takový počítač tu není připojený."}
            req = next((p for p in comp.pending if p["id"] == str(rid or "")), None)
            if not req:
                return {"ok": False, "error": "Tuhle žádost už počítač nečeká (vyřízená tady, "
                                              "na počítači, nebo vypršela)."}
            directive = {"typ": "rozhodnuti", "id": str(rid), "volba": choice}
            if req.get("kind") == "session":
                if choice not in SESSION_DECISIONS:
                    return {"ok": False, "error": "Neznámá volba."}
                if choice == "povolit" and req.get("op") == "otazka":
                    return {"ok": False, "error": "Na otázku vyber odpověď."}
                if choice == "odpoved":
                    answers, error = _answers(req.get("otazky") or [], odpovedi)
                    if error:
                        return {"ok": False, "error": error}
                    directive["odpovedi"] = answers
            elif choice not in DECISIONS:
                return {"ok": False, "error": "Neznámá volba."}
            comp.directives.append(directive)
            comp.pending = [p for p in comp.pending if p["id"] != str(rid)]
            self._cond.notify_all()
        return {"ok": True}

    def set_access(self, uid, cid, level):
        """Člověk z prostoru mění přístup na svém počítači. Počítač změnu provede
        a ukáže u sebe upozornění."""
        if level not in LEVELS:
            return {"ok": False, "error": "Neznámá úroveň přístupu."}
        with self._cond:
            comp = self._find(uid, cid)
            if not comp:
                return {"ok": False, "error": "Takový počítač tu není připojený."}
            comp.directives.append({"typ": "pristup", "level": level})
            self._cond.notify_all()
        return {"ok": True}

    def _find(self, uid, cid):
        comp = self.computers.get((uid, str(cid or "")))
        if comp:
            return comp
        mine = [c for (u, _c), c in self.computers.items() if u == uid]
        return mine[0] if len(mine) == 1 and not cid else None

    def bye(self, uid, cid):
        """Počítač se odhlásil (vypnutý přístup, zavřená appka)."""
        with self._cond:
            comp = self.computers.pop((uid, str(cid or "")), None)
            if comp:
                self._drop_queue(comp, "Počítač se právě odpojil.")
        return bool(comp)

    def requeue(self, uid, cid, tasks):
        """Úkoly, které se počítači nepodařilo předat (spadlé spojení)."""
        with self._cond:
            comp = self.computers.get((uid, str(cid or "")))
            if comp and tasks:
                comp.queue[:0] = tasks
                self._cond.notify_all()

    def reply(self, uid, cid, rid, result):
        """Výsledek úkolu od počítače. False = úkol nezná (vypršel, cizí)."""
        with self._cond:
            slot = self._waiting.get(str(rid or ""))
            if not slot or slot["uid"] != uid or slot["cid"] != str(cid or ""):
                return False
            slot["result"] = result
            slot["event"].set()
            return True

    # ── strana prostoru ──────────────────────────────────────────────────────
    def list(self, uid):
        now = time.time()
        with self._cond:
            mine = [c for (u, _cid), c in self.computers.items() if u == uid]
            return [c.public(now) for c in sorted(mine, key=lambda c: -c.last_seen)]

    def call(self, uid, op, args, target=""):
        """Předá úkol počítači a počká na výsledek.

        Vrací {"ok", "result"|"error", "computer"}. Nikdy nevyhodí výjimku —
        chyba je věta, kterou dostane Claude a z ní pozná, co dál.
        """
        if op not in ALL_OPS:
            return {"ok": False, "error": f"Neznámý úkol {op!r}."}
        args = args if isinstance(args, dict) else {}
        timeout = timeout_for(op, args)
        now = time.time()
        with self._cond:
            comp, error = self._pick(uid, _text(target, 80), now)
            if error:
                return {"ok": False, "error": error}
            if op not in ACCESS.get(comp.access, ()):
                return {"ok": False, "computer": comp.name, "error":
                        f"Na počítači {comp.name} je zapnutý jen přístup ke čtení — "
                        "tohle (zápis, spouštění, snímek obrazovky, schránka…) nejde. Když to "
                        "uživatel chce, přepne to buď tady v prostoru (Nastavení → Účet → Tvůj počítač), "
                        "nebo v appce na počítači: Nastavení → Účet → Claude ze serveru na "
                        "tomhle počítači. Doporučená volba je „ptá se na souhlas“."}
            if comp.access == "ptat" and op in ASK_OPS:
                timeout += APPROVE_WAIT        # na souhlas člověka se čeká zvlášť
            if len(comp.queue) >= MAX_QUEUE:
                return {"ok": False, "computer": comp.name,
                        "error": "Na počítač čeká moc úkolů najednou, zkus to za chvíli."}
            rid = secrets.token_hex(8)
            slot = {"event": threading.Event(), "result": None, "uid": uid, "cid": comp.id}
            self._waiting[rid] = slot
            comp.queue.append({"id": rid, "op": op, "args": args})
            comp.last = {"op": op, "summary": summary(op, args), "at": now}
            self._cond.notify_all()
        done = slot["event"].wait(timeout)
        with self._cond:
            self._waiting.pop(rid, None)
            queued = any(t["id"] == rid for t in comp.queue)
            if queued:
                comp.queue = [t for t in comp.queue if t["id"] != rid]
        if not done:
            return {"ok": False, "computer": comp.name, "error": (
                f"Počítač {comp.name} úkol nepřevzal — asi se právě odpojil "
                "(zavřená appka, uspaný počítač, výpadek sítě)." if queued else
                f"Počítač {comp.name} neodpověděl do {timeout} s.")}
        result = slot["result"] or {}
        if result.get("ok"):
            return {"ok": True, "computer": comp.name, "result": result.get("result")}
        return {"ok": False, "computer": comp.name,
                "error": _text(result.get("error"), 2000) or "Na počítači se to nepovedlo."}

    # ── úkoly na později ─────────────────────────────────────────────────────
    def ukol_new(self, uid, args):
        """Claude z prostoru nechává úkol počítači. {"ok", "ukol"} nebo {"ok": False, "error"}."""
        if not self.ukoly_dir:
            return {"ok": False, "error": "Tahle brána úkoly na později neumí."}
        args = args if isinstance(args, dict) else {}
        title = " ".join(str(args.get("title") or "").split())[:UKOL_TITLE]
        text = str(args.get("text") or "").strip()
        kind = "chat" if args.get("kind") == "chat" else ""
        if not title or not text:
            return {"ok": False, "error": "Úkol potřebuje název i zadání."}
        if len(text) > UKOL_TEXT:
            return {"ok": False, "error": (
                f"Zadání má {len(text)} znaků, vejde se nejvýš {UKOL_TEXT}. Zkrať ho, "
                "nebo podrobnosti ulož do souboru a přilož ho.")}
        files, error = decode_files(args.get("files"), CHAT_BYTES if kind else None)
        if error:
            return {"ok": False, "error": error}
        now = time.time()
        with self._cond:
            waiting = sum(1 for r in self._load(uid, now).values() if r["state"] == "ceka")
        if waiting >= UKOL_WAITING:
            return {"ok": False, "error": (
                f"Na počítač už čeká {waiting} úkolů. Než přidáš další, počkej, až si je "
                "vyzvedne, nebo nepotřebné zruš (zrusit_ukol).")}
        record = {"id": secrets.token_hex(8), "title": title, "text": text, "kind": kind,
                  "folder": _text(args.get("folder"), 500),
                  "computer": _text(args.get("computer"), 80),
                  "files": [{"name": name, "size": len(data)} for name, data in files],
                  "created": int(now), "state": "ceka", "by": "", "by_id": "", "changed": 0}
        base = os.path.join(self._user_dir(uid), record["id"])
        try:
            # Přílohy se píšou mimo zámek — patnáct megabajtů nesmí zdržet
            # dotazy ostatních počítačů.
            os.makedirs(os.path.join(base, "soubory"), mode=0o700, exist_ok=True)
            for name, data in files:
                fd = os.open(os.path.join(base, "soubory", name),
                             os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "wb") as fh:
                    fh.write(data)
            self._save(uid, record)
        except OSError as exc:
            shutil.rmtree(base, ignore_errors=True)
            return {"ok": False, "error": f"Úkol se na bráně nepodařilo uložit: {exc}"}
        with self._cond:
            self._load(uid, now)[record["id"]] = record
            self._cond.notify_all()
        return {"ok": True, "ukol": self._public(record)}

    def ukoly(self, uid):
        """Úkoly na později tohohle účtu, nejnovější první."""
        if not self.ukoly_dir:
            return []
        with self._cond:
            items = list(self._load(uid, time.time()).values())
        return [self._public(r) for r in sorted(items, key=lambda r: -r["created"])]

    def ukol_cancel(self, uid, tid):
        with self._cond:
            items = self._load(uid, time.time()) if self.ukoly_dir else {}
            record = items.get(str(tid or ""))
            if not record:
                return {"ok": False, "error": "Takový úkol tu není."}
            if record["state"] != "ceka":
                return {"ok": False, "error": (
                    f"Úkol už si převzal počítač {record['by'] or ''} — tam ho jde zahodit "
                    "(Nastavení → Účet → Úkoly ze serveru).")}
            items.pop(record["id"], None)
        shutil.rmtree(os.path.join(self._user_dir(uid), record["id"]), ignore_errors=True)
        return {"ok": True, "ukol": self._public(record)}

    def ukoly_offer(self, uid, cid):
        """Úkoly, které si tenhle počítač může vzít — patří do odpovědi na dotaz."""
        with self._cond:
            comp = self.computers.get((uid, str(cid or "")))
            if not comp:
                return []
            ids = self._offer_ids(comp)
            comp.offered = ids
            items = self._ukoly.get(uid) or {}
            offer = [items[i] for i in ids if i in items]
        return [{"id": r["id"], "title": r["title"], "created": r["created"],
                 "size": sum(f["size"] for f in r["files"])}
                for r in sorted(offer, key=lambda r: r["created"])]

    def ukol_take(self, uid, cid, tid):
        """Celý úkol i s přílohami pro počítač. Stav se nemění — to až `ukol_mark`."""
        with self._cond:
            comp = self.computers.get((uid, str(cid or "")))
            record = (self._load(uid, time.time()) if self.ukoly_dir else {}).get(str(tid or ""))
            if not comp or not comp.access:
                return {"ok": False, "error": "Počítač není připojený s povoleným přístupem."}
            if not record or record["state"] != "ceka" or not self._for(record, comp):
                return {"ok": False, "gone": True,
                        "error": "Úkol tu už není — vyřízený, zrušený, nebo pro jiný počítač."}
            record = dict(record)
        folder = os.path.join(self._user_dir(uid), record["id"], "soubory")
        files = []
        try:
            for f in record["files"]:
                with open(os.path.join(folder, f["name"]), "rb") as fh:
                    files.append({"name": f["name"],
                                  "data": base64.b64encode(fh.read()).decode("ascii")})
        except OSError:
            return {"ok": False, "gone": True, "error": "Úkol se mezitím zrušil."}
        return {"ok": True, "ukol": dict(self._public(record), text=record["text"], files=files)}

    def ukol_mark(self, uid, cid, tid, state):
        """Počítač hlásí, kde úkol je: prevzato → spusteno / zahozeno."""
        if state not in ("prevzato", "spusteno", "zahozeno"):
            return {"ok": False, "error": "Neznámý stav úkolu."}
        cid = str(cid or "")
        now = time.time()
        with self._cond:
            comp = self.computers.get((uid, cid))
            record = (self._load(uid, now) if self.ukoly_dir else {}).get(str(tid or ""))
            if not record:
                return {"ok": False, "gone": True, "error": "Takový úkol tu není."}
            if record["state"] == "ceka":
                if not comp or not self._for(record, comp):
                    return {"ok": False, "error": "Úkol je pro jiný počítač."}
            elif record["by_id"] != cid:
                return {"ok": False, "error": "Úkol převzal jiný počítač."}
            elif record["state"] in ("spusteno", "zahozeno") or state == "prevzato":
                return {"ok": True, "ukol": self._public(record)}   # už je dál
            if comp:
                record["by"] = comp.name
            record.update(state=state, by_id=cid, changed=int(now))
            try:
                self._save(uid, record)
            except OSError:
                pass                     # stav v paměti platí, zapíše se příště
            public = self._public(record)
        # Přílohy už má počítač u sebe — na bráně nemají co dělat.
        shutil.rmtree(os.path.join(self._user_dir(uid), record["id"], "soubory"),
                      ignore_errors=True)
        return {"ok": True, "ukol": public}

    # ── pomůcky (pod zámkem) ─────────────────────────────────────────────────
    def _user_dir(self, uid):
        return os.path.join(self.ukoly_dir, str(int(uid)))

    def _save(self, uid, record):
        path = os.path.join(self._user_dir(uid), record["id"], "ukol.json")
        tmp = f"{path}.{secrets.token_hex(4)}.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(record, fh, ensure_ascii=False)
        os.replace(tmp, path)

    def _load(self, uid, now):
        """Úkoly účtu (poprvé z disku) bez těch, které propadly."""
        items = self._ukoly.get(uid)
        if items is None:
            items = self._ukoly[uid] = {}
            root = self._user_dir(uid)
            try:
                names = os.listdir(root)
            except OSError:
                names = []
            for name in names:
                try:
                    with open(os.path.join(root, name, "ukol.json"), encoding="utf-8") as fh:
                        record = json.load(fh)
                except (OSError, ValueError):
                    continue
                if isinstance(record, dict) and record.get("id") == name \
                        and _UKOL_ID.fullmatch(name) and record.get("state") in UKOL_STATES:
                    items[name] = record
        for tid, record in list(items.items()):
            if record["state"] == "ceka":
                expired = now - record["created"] > UKOL_KEEP
            else:
                expired = now - record["changed"] > UKOL_HISTORY
            if expired:
                items.pop(tid)
                shutil.rmtree(os.path.join(self._user_dir(uid), tid), ignore_errors=True)
        return items

    def _offer_ids(self, comp):
        if not self.ukoly_dir or not comp.access:
            return set()
        return {tid for tid, r in self._load(comp.uid, time.time()).items()
                if r["state"] == "ceka" and self._for(r, comp)}

    @staticmethod
    def _for(record, comp):
        """Je úkol pro tenhle počítač? Bez určení pro první, který se ozve."""
        target = record["computer"].casefold()
        return not target or target in (comp.name.casefold(), comp.id.casefold())

    @staticmethod
    def _public(record):
        return {"id": record["id"], "title": record["title"], "folder": record["folder"],
                "kind": record.get("kind", ""),
                "computer": record["computer"],
                "files": [f["name"] for f in record["files"]],
                "created": record["created"], "state": record["state"],
                "state_label": UKOL_STATES.get(record["state"], record["state"]),
                "by": record["by"], "changed": record["changed"]}

    def _pick(self, uid, target, now):
        mine = [c for (u, _cid), c in self.computers.items()
                if u == uid and c.online(now) and c.access in ACCESS]
        if target:
            low = target.lower()
            hits = [c for c in mine if c.id == target or c.name.lower() == low]
            if not hits:
                names = ", ".join(c.name for c in mine) or "žádný"
                return None, f"Počítač {target!r} není připojený. Připojené: {names}."
            mine = hits[:1]
        if not mine:
            return None, ("Žádný počítač uživatele teď není připojený. Musí mít na "
                          "počítači otevřenou appku Aladin přihlášenou k "
                          "tomuhle serveru a v ní zapnutý přístup: Nastavení → Účet → "
                          "Claude ze serveru na tomhle počítači. Když to může počkat, "
                          "nech počítači úkol (nechat_ukol) — dodělá ho Claude na "
                          "počítači, jakmile se připojí.")
        if len(mine) > 1:
            names = ", ".join(c.name for c in mine)
            return None, (f"Připojených počítačů je víc ({names}). Řekni, na kterém "
                          "pracovat — parametr `pocitac`.")
        return mine[0], ""

    def _drop_queue(self, comp, why):
        for task in comp.queue:
            slot = self._waiting.get(task["id"])
            if slot:
                slot["result"] = {"ok": False, "error": why}
                slot["event"].set()
        comp.queue = []

    def _forget_old(self):
        now = time.time()
        for key, comp in list(self.computers.items()):
            if not comp.polling and now - comp.last_seen > FORGET_AFTER:
                self.computers.pop(key, None)
