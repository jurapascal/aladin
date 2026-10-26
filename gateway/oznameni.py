"""
Oznámení pro lidi v prostorech — zvonek v liště hubu (hub/static/oznameni.js).

Dřív hub uměl jen šestivteřinovou hlášku dole. Na to, že někomu dochází místo
na serveru, nestačí: zmizí dřív, než ji člověk přečte, a správce se nedozví,
jestli ji vůbec viděl. Tady oznámení zůstane, dokud ho člověk nezavře, a u
každého je vidět, kdy ho přečetl, kdy na něj klikl a kdy ho zavřel.

Jedno oznámení:

    id, uid, email     komu (číslo účtu je klíč, e-mail jen pro čtení záznamu)
    created            kdy vzniklo (unix čas)
    kind               druh — `uloziste` (místo na disku), `zprava` (od admina)…
    sender             od koho: „Claudík", jméno admina, „Systém"
    title, text        nadpis a text pro člověka
    severity           info / varovani / vazne
    action             {"label", "prompt"} — tlačítko, které otevře nový chat
                       s Claudem a pošle mu `prompt` (nebo nic)
    read_at            kdy si ho přečetl (otevřel zvonek)
    acted_at           kdy klikl na akci („Vyřešit s Claudem")
    dismissed_at       kdy ho zavřel (nebo ho nahradilo novější téhož druhu)
    replaced_by        id novějšího oznámení, které ho nahradilo

Leží v SQLite (`oznameni.db` u brány): zapisuje brána z víc vláken, noční úklid
z jiného procesu a `claude-hub-admin` z třetího — databáze to srovná sama.
Do prostoru se nic z toho nedostane, hub si oznámení bere přes `/gw/oznameni`
s přihlašovací cookie brány.

Modul je obecný: úložiště (gateway/uloziste.py) je první, kdo oznámení posílá,
další části brány můžou volat `pridat()` stejně.
"""
import contextlib
import json
import os
import re
import sqlite3
import time

from . import config

DB = os.environ.get("HUB_GW_OZNAMENI_DB", os.path.join(config.GATEWAY_DIR, "oznameni.db"))
SEVERITIES = ("info", "varovani", "vazne")
MAX_TITLE = 120
MAX_TEXT = 2000
MAX_PROMPT = 8000
MAX_LABEL = 40
# Zavřená oznámení se v zvonku ukazují jako „Starší" ještě tak dlouho, pak už jen
# adminům v záznamu.
STARSI_DNI = 30

_SCHEMA = """
CREATE TABLE IF NOT EXISTS oznameni (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    uid INTEGER NOT NULL,
    email TEXT NOT NULL DEFAULT '',
    created REAL NOT NULL,
    kind TEXT NOT NULL DEFAULT 'zprava',
    sender TEXT NOT NULL DEFAULT '',
    sender_email TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL,
    text TEXT NOT NULL DEFAULT '',
    severity TEXT NOT NULL DEFAULT 'info',
    action_label TEXT NOT NULL DEFAULT '',
    action_prompt TEXT NOT NULL DEFAULT '',
    read_at REAL,
    acted_at REAL,
    dismissed_at REAL,
    replaced_by INTEGER,
    batch TEXT NOT NULL DEFAULT '',
    meta TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS oznameni_uid ON oznameni (uid, dismissed_at);
CREATE INDEX IF NOT EXISTS oznameni_kind ON oznameni (uid, kind, created);
"""

# Řídicí znaky (kromě nového řádku a tabulátoru) do textu pro člověka nepatří —
# text může pocházet i od Clauda, který četl jména souborů z domova.
_RIDICI = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


@contextlib.contextmanager
def _db():
    os.makedirs(os.path.dirname(os.path.abspath(DB)), exist_ok=True)
    con = sqlite3.connect(DB, timeout=15)
    con.row_factory = sqlite3.Row
    try:
        con.execute("PRAGMA journal_mode=WAL")
        con.executescript(_SCHEMA)
        yield con
        con.commit()
    finally:
        con.close()


def _clean(text, limit):
    text = _RIDICI.sub("", str(text or "")).replace("\r\n", "\n").strip()
    return text[:limit]


def _row(r):
    meta = {}
    try:
        meta = json.loads(r["meta"] or "{}")
    except ValueError:
        pass
    out = {"id": r["id"], "uid": r["uid"], "email": r["email"], "created": r["created"],
           "kind": r["kind"], "sender": r["sender"], "title": r["title"], "text": r["text"],
           "severity": r["severity"], "read_at": r["read_at"], "acted_at": r["acted_at"],
           "dismissed_at": r["dismissed_at"], "replaced_by": r["replaced_by"],
           "batch": r["batch"], "meta": meta if isinstance(meta, dict) else {}}
    out["action"] = ({"label": r["action_label"] or "Vyřešit s Claudem", "prompt": r["action_prompt"]}
                     if r["action_prompt"] else None)
    return out


def pridat(uid, email, title, text="", kind="zprava", sender="Systém", sender_email="",
           severity="info", action=None, meta=None, batch="", replace_kind=False):
    """Založí oznámení pro účet `uid`. Vrací jeho id.

    `replace_kind` = starší nezavřená oznámení téhož druhu se zavřou jako
    nahrazená tímhle (zvonek pak neukazuje tři verze téhož „dochází místo")."""
    title = _clean(title, MAX_TITLE)
    if not title:
        raise ValueError("Oznámení potřebuje nadpis.")
    severity = severity if severity in SEVERITIES else "info"
    label = prompt = ""
    if action and _clean(action.get("prompt"), MAX_PROMPT):
        prompt = _clean(action.get("prompt"), MAX_PROMPT)
        label = _clean(action.get("label"), MAX_LABEL) or "Vyřešit s Claudem"
    now = time.time()
    with _db() as con:
        cur = con.execute(
            "INSERT INTO oznameni (uid, email, created, kind, sender, sender_email, title, text,"
            " severity, action_label, action_prompt, batch, meta) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (int(uid), str(email or "").lower(), now, _clean(kind, 30) or "zprava",
             _clean(sender, 80), str(sender_email or "").lower(), title, _clean(text, MAX_TEXT),
             severity, label, prompt, _clean(batch, 40),
             json.dumps(meta or {}, ensure_ascii=False)))
        new_id = cur.lastrowid
        if replace_kind:
            con.execute("UPDATE oznameni SET dismissed_at = ?, replaced_by = ?"
                        " WHERE uid = ? AND kind = ? AND id != ? AND dismissed_at IS NULL",
                        (now, new_id, int(uid), kind, new_id))
    return new_id


def pro(uid):
    """Zvonek člověka: nezavřená oznámení (nejnovější nahoře), pár nedávno
    zavřených a počet nepřečtených."""
    hranice = time.time() - STARSI_DNI * 86400
    with _db() as con:
        aktivni = [_row(r) for r in con.execute(
            "SELECT * FROM oznameni WHERE uid = ? AND dismissed_at IS NULL"
            " ORDER BY created DESC LIMIT 50", (int(uid),))]
        starsi = [_row(r) for r in con.execute(
            "SELECT * FROM oznameni WHERE uid = ? AND dismissed_at IS NOT NULL"
            " AND created >= ? ORDER BY created DESC LIMIT 20", (int(uid), hranice))]
    # Do prohlížeče jen to, co člověk potřebuje — meta jsou čísla pro adminy.
    for o in aktivni + starsi:
        o.pop("meta", None)
        o.pop("batch", None)
    return {"oznameni": aktivni, "starsi": starsi,
            "neprectene": sum(1 for o in aktivni if not o["read_at"])}


_POLE = {"precteno": "read_at", "vyreseno": "acted_at", "zavrit": "dismissed_at"}


def oznacit(uid, ids, co):
    """Přečteno / vyřešeno / zavřeno — jen u oznámení toho člověka. Co už
    jednou nastalo, se nepřepisuje (admin vidí první klik). Vrací počet změn."""
    pole = _POLE.get(co)
    if not pole:
        raise ValueError("Neznámá akce u oznámení.")
    try:
        ids = [int(i) for i in (ids if isinstance(ids, (list, tuple)) else [ids])][:200]
    except (TypeError, ValueError):
        raise ValueError("Neplatné číslo oznámení.") from None
    if not ids:
        return 0
    now = time.time()
    marks = ",".join("?" * len(ids))
    with _db() as con:
        cur = con.execute(f"UPDATE oznameni SET {pole} = ? WHERE uid = ? AND {pole} IS NULL"
                          f" AND id IN ({marks})", (now, int(uid), *ids))
        n = cur.rowcount
        # Kliknout na akci nebo zavřít = taky přečíst.
        if pole != "read_at":
            con.execute(f"UPDATE oznameni SET read_at = ? WHERE uid = ? AND read_at IS NULL"
                        f" AND id IN ({marks})", (now, int(uid), *ids))
    return n


def posledni(uid, kind):
    """Poslední oznámení daného druhu pro účet (i zavřené), nebo None."""
    with _db() as con:
        r = con.execute("SELECT * FROM oznameni WHERE uid = ? AND kind = ?"
                        " ORDER BY created DESC LIMIT 1", (int(uid), kind)).fetchone()
    return _row(r) if r else None


def vse(limit=300, uid=None, kind=None):
    """Záznam pro adminy: kdo co dostal a co s tím udělal (nejnovější nahoře)."""
    sql, args = "SELECT * FROM oznameni", []
    where = []
    if uid is not None:
        where.append("uid = ?")
        args.append(int(uid))
    if kind:
        where.append("kind = ?")
        args.append(kind)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY created DESC LIMIT ?"
    args.append(max(1, min(int(limit), 2000)))
    with _db() as con:
        return [_row(r) for r in con.execute(sql, args)]


def stav(o):
    """Jedním slovem, kde oznámení je — pro záznam adminů."""
    if o.get("replaced_by"):
        return "nahrazeno"
    if o.get("acted_at"):
        return "vyreseno"
    if o.get("dismissed_at"):
        return "zavreno"
    if o.get("read_at"):
        return "precteno"
    return "neprecteno"


def poslat(accounts, admin, komu, title, text="", prompt="", label="", severity="info"):
    """Ruční oznámení od admina vybraným lidem (`komu` = seznam čísel účtů,
    nebo "vsichni"). Vrací {"poslano": počet, "batch": značka}."""
    lide = [dict(r) for r in accounts.list() if not r["disabled"]]
    if komu == "vsichni":
        cil = lide
    else:
        try:
            chci = {int(i) for i in (komu or [])}
        except (TypeError, ValueError):
            raise ValueError("Neplatný výběr lidí.") from None
        cil = [u for u in lide if u["id"] in chci]
    if not cil:
        raise ValueError("Vyber, komu se má oznámení poslat.")
    if not _clean(title, MAX_TITLE):
        raise ValueError("Oznámení potřebuje nadpis.")
    batch = time.strftime("%Y%m%d-%H%M%S") + "-" + os.urandom(3).hex()
    kdo = (admin.get("name") or admin.get("email") or "Admin").strip()
    action = {"label": label or "Vyřešit s Claudem", "prompt": prompt} if str(prompt or "").strip() else None
    for u in cil:
        pridat(u["id"], u["email"], title, text, kind="zprava", sender=kdo,
               sender_email=admin.get("email", ""), severity=severity, action=action, batch=batch)
    return {"poslano": len(cil), "batch": batch}
