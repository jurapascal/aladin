"""
Víc firem na jedné bráně — každá s vlastním trezorem, lidmi a přístupy.

Firma je trezor (Obsidian) se skilly a lidmi. Lidé mají ve firmě roli:

* `kapitan` — všechno: lidi, role, klienti, podkategorie, přístupy, mazání,
* `clen` — čte a zapisuje tam, kam vidí; maže jen s příznakem `mazat`,
* `host` — jen čte tam, kam vidí.

Firma je jednoho ze dvou **druhů** (`druh`, volí se při založení):

* `klienti` — agentura, pracuje pro klienty: `Klienti/<klient>/<podkategorie>`
  (Pytlík / Web). Přístup ke klientovi = soubory přímo v jeho složce, ne
  podkategorie; podkategorie chce i přístup ke klientovi.
* `projekty` — firma pracuje pro sebe, klienty nemá: `Projekty/<projekt>`
  (Good Food / Web). Jedna úroveň, přístup po projektech.

Klient, podkategorie i projekt jsou ve výchozím stavu **skryté** — kdo je má
vidět, nastaví generál (`pristupy`). Všechno mimo kořenovou složku (Klienti/,
resp. Projekty/) vidí každý člen. Skrytí je natvrdo, stejně jako omezená
poznámka (poznamky.mask_args): v sandboxu skrytá složka vůbec není. Nejhlubší
úroveň (podkategorie, resp. projekt) je zároveň „skupina“ — příjemce sdílených
napojení a chatů.

Dnešní firemní trezor je firma `firma` (LEGACY) — nic se nestěhuje. Lidé se
pro ni odvozují z dnešních přístupů (accounts.company_level: admin → generál,
write → člen, read → host, none → není členem) a `Klienti/` v ní platí, až to
generál zapne (`klienti`) — trezor, kde už složka Klienti je, se tak nikomu
nezavře sám od sebe.

Registr leží mimo trezory (FIRMY_DIR/.firmy.json), podle id účtů. Prostor ho
nevidí, takže si přístup sám nepřidá; mění ho jen tenhle modul.
"""
import contextlib
import fcntl
import json
import os
import re
import threading
import time
import unicodedata

from . import config, shared

LEGACY = "firma"
KLIENTI = "Klienti"
PROJEKTY = "Projekty"
# Druh firmy → (kořenová složka, kolik úrovní pod ní se zpřístupňuje).
DRUHY = {"klienti": (KLIENTI, 2), "projekty": (PROJEKTY, 1)}
ROLES = ("kapitan", "clen", "host")
ROLE_LABELS = {"kapitan": "generál", "clen": "člen", "host": "host"}
SLUG = re.compile(r"[a-z0-9][a-z0-9-]{0,39}")
REGISTRY = os.path.join(config.FIRMY_DIR, ".firmy.json")
LOG = os.path.join(config.FIRMY_DIR, "zmeny.jsonl")
_LOCK = threading.Lock()

# Výchozí pravidla pro Clauda v nové podkategorii / novém projektu; generál je
# pak upraví jako každou jinou poznámku.
PODKATEGORIE_CLAUDE_MD = """# {nazev}

Pravidla pro Clauda v téhle složce (platí i pro všechno pod ní).

- Sem patří dokumentace k práci „{nazev}“{pro}: co se dělá, jak, odkazy,
  rozhodnutí a stav — tak, aby na to kdokoli s přístupem navázal.
- Hesla, klíče a přihlašovací údaje sem nikdy nepiš — patří do napojení (MCP).
- Než sem zapíšeš, přečti si `CLAUDE.md` i ve složkách nad touhle.
"""


@contextlib.contextmanager
def _locked():
    with _LOCK:
        os.makedirs(config.FIRMY_DIR, exist_ok=True)
        with open(REGISTRY + ".lock", "a") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)


def _load():
    try:
        with open(REGISTRY, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    firmy = data.get("firmy") if isinstance(data.get("firmy"), dict) else {}
    out = {"firmy": {}, "zakladatele": [int(u) for u in data.get("zakladatele") or []
                                         if isinstance(u, int)]}
    for slug, entry in firmy.items():
        if not isinstance(entry, dict) or not (slug == LEGACY or SLUG.fullmatch(str(slug))):
            continue
        lide = entry.get("lide") if isinstance(entry.get("lide"), dict) else {}
        pristupy = entry.get("pristupy") if isinstance(entry.get("pristupy"), dict) else {}
        out["firmy"][slug] = {
            **entry,
            "lide": {str(uid): {"role": v.get("role"), "mazat": bool(v.get("mazat"))}
                     for uid, v in lide.items()
                     if isinstance(v, dict) and v.get("role") in ROLES and str(uid).isdigit()},
            "pristupy": {str(rel): [int(u) for u in uids if isinstance(u, int)]
                         for rel, uids in pristupy.items() if isinstance(uids, list)},
        }
    return out


def _save(data):
    tmp = REGISTRY + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2, sort_keys=True)
    os.replace(tmp, REGISTRY)


def _log(user, action, slug, **extra):
    entry = {"cas": time.strftime("%Y-%m-%d %H:%M:%S"), "email": (user or {}).get("email", ""),
             "akce": action, "firma": slug, **extra}
    try:
        os.makedirs(config.FIRMY_DIR, exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


# ── kde firma leží ───────────────────────────────────────────────────────────
def firma_dir(slug):
    """Složka firmy: registr poznámek, záznamy, uložené chaty. Trezor je v ní."""
    if slug == LEGACY:
        return config.COMPANY_DIR
    return os.path.join(config.FIRMY_DIR, slug)


def vault_dir(slug):
    if slug == LEGACY:
        return config.COMPANY_VAULT
    return os.path.join(firma_dir(slug), "Brain")


def slugs(data=None):
    """Všechny firmy: dnešní (je vždycky) a založené, podle zkratky."""
    data = data or _load()
    return [LEGACY] + sorted(s for s in data["firmy"] if s != LEGACY
                             and os.path.isdir(vault_dir(s)))


def exists(slug):
    return slug in slugs()


def nazev(slug, data=None):
    entry = (data or _load())["firmy"].get(slug) or {}
    if entry.get("nazev"):
        return entry["nazev"]
    # Dnešní firma bez vlastního názvu: „Firma" (jméno složky trezoru nic neříká).
    return "Firma" if slug == LEGACY else slug


def druh(slug, data=None):
    """`klienti` (agentura: Klienti/klient/podkategorie), nebo `projekty`
    (firma pro sebe: Projekty/projekt). Bez záznamu klienti."""
    d = ((data or _load())["firmy"].get(slug) or {}).get("druh")
    return d if d in DRUHY else "klienti"


def koren(slug, data=None):
    """Kořenová složka, pod kterou se zpřístupňuje (Klienti, nebo Projekty)."""
    return DRUHY[druh(slug, data)][0]


def hloubka(slug, data=None):
    """Kolik úrovní pod kořenem se zpřístupňuje (klienti 2, projekty 1)."""
    return DRUHY[druh(slug, data)][1]


def klienti_zapnute(slug, data=None):
    """Platí ve firmě skrývání klientů a podkategorií? Nové firmy vždycky,
    dnešní jen po zapnutí."""
    if slug != LEGACY:
        return True
    return bool(((data or _load())["firmy"].get(LEGACY) or {}).get("klienti"))


# ── kdo je ve firmě a s jakou rolí ───────────────────────────────────────────
def _accounts():
    return shared.ACCOUNTS


def role(user, slug, data=None):
    """kapitan / clen / host, nebo '' (není členem)."""
    if not user:
        return ""
    data = data or _load()
    entry = data["firmy"].get(slug) or {}
    mine = (entry.get("lide") or {}).get(str(int(user["id"])))
    if slug != LEGACY:
        return mine["role"] if mine and os.path.isdir(vault_dir(slug)) else ""
    # Dnešní firma: z přístupů (accounts.company_level). Generál navíc i ten,
    # koho tak výslovně nastavil jiný generál.
    from .workspace import company_level
    level = company_level(user)
    if level == "none":
        return ""
    if user.get("role") == "admin" or (mine and mine["role"] == "kapitan"):
        return "kapitan"
    return "clen" if level == "write" else "host"


def mazat(user, slug, data=None):
    """Smí mazat a přejmenovávat? Generál vždycky, člen s příznakem."""
    data = data or _load()
    r = role(user, slug, data)
    if r == "kapitan":
        return True
    mine = ((data["firmy"].get(slug) or {}).get("lide") or {}).get(str(int(user["id"])))
    return r == "clen" and bool(mine and mine.get("mazat"))


def zapis(user, slug, data=None):
    return role(user, slug, data) in ("kapitan", "clen")


def kapitani(slug, data=None):
    """Id generálů firmy (u dnešní firmy admini a výslovně nastavení)."""
    data = data or _load()
    accounts = _accounts()
    if not accounts:
        return []
    return [r["id"] for r in accounts.list()
            if not r["disabled"] and role(dict(r), slug, data) == "kapitan"]


def clenove(slug, data=None):
    """[{id, email, name, role, mazat}] — kdo ve firmě je."""
    data = data or _load()
    accounts = _accounts()
    if not accounts:
        return []
    out = []
    for r in accounts.list():
        if r["disabled"]:
            continue
        rr = role(dict(r), slug, data)
        if rr:
            out.append({"id": r["id"], "email": r["email"], "name": r["name"] or "",
                        "role": rr, "mazat": mazat(dict(r), slug, data)})
    return out


def firmy_for(user, data=None):
    """Firmy, kde je uživatel členem — pro hub (přepínač) a sandbox."""
    data = data or _load()
    out = []
    for slug in slugs(data):
        r = role(user, slug, data)
        if r:
            out.append({"zkratka": slug, "nazev": nazev(slug, data), "cesta": vault_dir(slug),
                        "role": r, "mazat": mazat(user, slug, data),
                        "klienti": klienti_zapnute(slug, data), "druh": druh(slug, data)})
    return out


def muze_zakladat(user, data=None):
    if not user:
        return False
    if user.get("role") == "admin":
        return True
    return int(user["id"]) in (data or _load())["zakladatele"]


# ── klienti a podkategorie: kdo co vidí ──────────────────────────────────────
def _isdir(slug, rel):
    path = os.path.join(vault_dir(slug), *rel.split("/"))
    return os.path.isdir(path) and not os.path.islink(path)


def _scope(slug, rel, data=None):
    """Složky, do kterých musí člověk vidět, aby viděl `rel` — od kořene dolů.

    Klienti: `Klienti/Pytlík/brief.md` → (Klienti/Pytlík,), `Klienti/Pytlík/
    Web/x.md` → (Klienti/Pytlík, Klienti/Pytlík/Web). Projekty: `Projekty/Web/
    x.md` → (Projekty/Web,). Soubor přímo v kořeni (Klienti/x.md) není nic."""
    parts = [p for p in str(rel or "").replace("\\", "/").strip("/").split("/") if p]
    if len(parts) < 2 or parts[0] != koren(slug, data):
        return ()
    out = []
    for n in range(2, hloubka(slug, data) + 2):
        if len(parts) > n or (len(parts) == n and _isdir(slug, "/".join(parts))):
            out.append("/".join(parts[:n]))
    return tuple(out)


def vidi(user, slug, rel, data=None):
    """Vidí `user` cestu `rel` ve firmě podle klientů, podkategorií a projektů?
    (Omezení jednotlivých poznámek hlídá poznamky.py zvlášť.)"""
    data = data or _load()
    r = role(user, slug, data)
    if not r:
        return False
    if r == "kapitan" or not klienti_zapnute(slug, data):
        return True
    pristupy = (data["firmy"].get(slug) or {}).get("pristupy") or {}
    uid = int(user["id"])
    return all(uid in pristupy.get(scope, []) for scope in _scope(slug, rel, data))


def _subdirs(slug, rel):
    base = os.path.join(vault_dir(slug), *rel.split("/"))
    try:
        names = sorted(os.listdir(base))
    except OSError:
        return []
    return [n for n in names if not n.startswith(".")
            and os.path.isdir(os.path.join(base, n)) and not os.path.islink(os.path.join(base, n))]


def klienti(slug, data=None):
    """{klient: [podkategorie]} podle složek na disku; u firmy s projekty
    {projekt: []}."""
    root = koren(slug, data)
    if hloubka(slug, data) == 1:
        return {k: [] for k in _subdirs(slug, root)}
    return {k: _subdirs(slug, f"{root}/{k}") for k in _subdirs(slug, root)}


def skryte(user, slug, data=None):
    """(skryté cesty, složky, přes které se má tmpfs položit vždycky).

    Druhá část je kvůli novým složkám: klient, podkategorie nebo projekt
    založený až po startu prostoru by se jinak v přivázané složce objevil
    hned — a bez přístupu. Přes kořen (Klienti/, Projekty/) a viditelné
    klienty se proto u člena tmpfs položí vždycky a nová složka se ukáže až
    po restartu (a jen s přístupem). Viditelná podkategorie / projekt se
    přiváže celá — nové soubory v ní jsou vidět hned."""
    data = data or _load()
    if role(user, slug, data) in ("", "kapitan") or not klienti_zapnute(slug, data):
        return [], []
    pristupy = (data["firmy"].get(slug) or {}).get("pristupy") or {}
    uid = int(user["id"])
    root = koren(slug, data)
    hidden, always = [], []
    if _isdir(slug, root):
        always.append(root)
    for klient, podkategorie in klienti(slug, data).items():
        rel = f"{root}/{klient}"
        if uid not in pristupy.get(rel, []):
            hidden.append(rel)
            continue
        if hloubka(slug, data) == 2:
            always.append(rel)
            hidden += [f"{rel}/{p}" for p in podkategorie
                       if uid not in pristupy.get(f"{rel}/{p}", [])]
    return hidden, always


def maskovane(slug, data=None):
    """Složky, přes které aspoň někomu leží tmpfs (zápis tam jde na místo)."""
    data = data or _load()
    root = koren(slug, data)
    if not klienti_zapnute(slug, data) or not _isdir(slug, root):
        return set()
    if hloubka(slug, data) == 1:
        return {root}
    return {root} | {f"{root}/{k}" for k in klienti(slug, data)}


def pristupy(slug, data=None):
    """{cesta klienta/podkategorie/projektu: [id]} — pro generála."""
    return dict(((data or _load())["firmy"].get(slug) or {}).get("pristupy") or {})


# ── skupiny: podkategorie / projekt jako příjemce sdílení (napojení, chaty) ──
def skupina_id(slug, rel):
    return f"{slug}:{rel}"


def skupina(raw):
    """`firma:Klienti/klient/podkategorie` (nebo `firma:Projekty/projekt`) →
    (firma, cesta), nebo ValueError. Skupina je vždycky nejhlubší úroveň."""
    slug, _, rel = str(raw or "").partition(":")
    parts = rel.split("/")
    if (not exists(slug) or len(parts) != hloubka(slug) + 1 or parts[0] != koren(slug)
            or any(not p or p.startswith(".") or p == ".." for p in parts)
            or not _isdir(slug, rel)):
        raise ValueError("Taková podkategorie ani projekt tu není.")
    return slug, rel


def ve_skupine(user, raw, data=None):
    """Patří `user` do podkategorie / projektu (vidí ho)? Neplatná skupina = ne."""
    try:
        slug, rel = skupina(raw)
    except ValueError:
        return False
    return vidi(user, slug, rel + "/_", data)


def skupina_nazev(raw, data=None):
    slug, _, rel = str(raw or "").partition(":")
    return " › ".join([nazev(slug, data)] + rel.split("/")[1:])


def skupiny_pro(user, data=None):
    """Podkategorie a projekty, do kterých `user` smí sdílet: vidí je a ve
    firmě zapisuje (host ne). [{id, nazev}]."""
    data = data or _load()
    out = []
    for slug in slugs(data):
        if not zapis(user, slug, data) or not klienti_zapnute(slug, data):
            continue
        root = koren(slug, data)
        for klient, podkategorie in klienti(slug, data).items():
            rels = ([f"{root}/{klient}/{p}" for p in podkategorie] if hloubka(slug, data) == 2
                    else [f"{root}/{klient}"])
            for rel in rels:
                if vidi(user, slug, rel + "/_", data):
                    out.append({"id": skupina_id(slug, rel),
                                "nazev": skupina_nazev(skupina_id(slug, rel), data)})
    return out


# ── správa ───────────────────────────────────────────────────────────────────
def _name(text, what="Název"):
    text = re.sub(r"[\x00-\x1f/\\:*?\"<>|]", " ", str(text or ""))
    text = " ".join(text.split()).strip(". ")[:60]
    if not text:
        raise ValueError(f"{what} chybí.")
    return text


def _slugify(name, taken):
    base = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    base = re.sub(r"[^a-z0-9]+", "-", base).strip("-")[:30] or "firma"
    slug, n = base, 2
    while slug in taken or slug == LEGACY or os.path.lexists(firma_dir(slug)):
        slug, n = f"{base}-{n}", n + 1
    return slug


def _need_captain(user, slug, data):
    if not exists(slug):
        raise ValueError("Taková firma tu není.")
    if role(user, slug, data) != "kapitan":
        raise PermissionError("Tohle může jen generál firmy.")


def zalozit(user, name, kapitan=None, druh_firmy="klienti"):
    """Nová firma. Zakladatel je její generál (`kapitan` = jiný účet, jen
    claude-hub-admin — tam se oprávnění zakládat nekontroluje). `druh_firmy`:
    `klienti` (agentura) nebo `projekty` (firma pro sebe), viz DRUHY."""
    name = _name(name, "Název firmy")
    if druh_firmy not in DRUHY:
        raise ValueError("Neznámý druh firmy.")
    root = DRUHY[druh_firmy][0]
    where = (f"klienti jsou ve složce `{KLIENTI}/` (klient › podkategorie)" if druh_firmy == "klienti"
             else f"projekty jsou ve složce `{PROJEKTY}/`")
    work = (f"Práce pro klienta patří do `{KLIENTI}/<klient>/<podkategorie>/`" if druh_firmy == "klienti"
            else f"Práce na projektu patří do `{PROJEKTY}/<projekt>/`")
    with _locked():
        data = _load()
        if kapitan is None and not muze_zakladat(user, data):
            raise PermissionError("Zakládat firmy smí admin a ti, komu to admin povolí.")
        if kapitan is not None:
            user = kapitan
        if any(nazev(s, data).lower() == name.lower() for s in slugs(data)):
            raise ValueError(f"Firma „{name}“ už tu je.")
        slug = _slugify(name, data["firmy"])
        vault = vault_dir(slug)
        os.makedirs(os.path.join(vault, root))
        os.makedirs(os.path.join(vault, "skills"))
        with open(os.path.join(vault, "README.md"), "w", encoding="utf-8") as fh:
            fh.write(f"# {name}\n\nObsidian firmy {name}. Obecné věci firmy vidí každý člen; "
                     f"{where} a vidí je jen ti, komu je generál zpřístupní.\n")
        with open(os.path.join(vault, "CLAUDE.md"), "w", encoding="utf-8") as fh:
            fh.write(f"# {name}\n\nPravidla pro Clauda v celé firmě. Generál je tu doplní.\n\n"
                     "- Hesla, klíče a přihlašovací údaje sem nikdy nepiš — patří do napojení.\n"
                     f"- {work}.\n")
        data["firmy"][slug] = {"nazev": name, "zalozil": int(user["id"]), "druh": druh_firmy,
                               "zalozeno": time.strftime("%Y-%m-%d %H:%M:%S"),
                               "lide": {str(int(user["id"])): {"role": "kapitan"}},
                               "pristupy": {}}
        _save(data)
    _log(user, "zalozit", slug, nazev=name)
    return {"ok": True, "zkratka": slug, "nazev": name, "affected": [int(user["id"])],
            "message": f"Firma „{name}“ je založená. Do tvého prostoru se načte po jeho restartu."}


def prejmenovat_firmu(user, slug, name):
    name = _name(name, "Název firmy")
    with _locked():
        data = _load()
        _need_captain(user, slug, data)
        data["firmy"].setdefault(slug, {"lide": {}, "pristupy": {}})["nazev"] = name
        _save(data)
    _log(user, "prejmenovat-firmu", slug, nazev=name)
    return {"ok": True}


def smazat_firmu(user, slug):
    """Smaže firmu — generál, nebo admin. Nic nemizí natrvalo: celá složka
    firmy (trezor, chaty, záznamy) se odloží do FIRMY_DIR/_smazana-<zkratka>-
    <čas>, odkud ji správce vrátí. Dnešní firmu smazat nejde."""
    if slug == LEGACY:
        raise ValueError("Dnešní firemní Obsidian smazat nejde — jen přejmenovat.")
    with _locked():
        data = _load()
        if not exists(slug) or slug not in data["firmy"]:
            raise ValueError("Taková firma tu není.")
        if role(user, slug, data) != "kapitan" and (user or {}).get("role") != "admin":
            raise PermissionError("Firmu smaže jen její generál nebo admin.")
        lidi = [m["id"] for m in clenove(slug, data)]
        stamp = time.strftime("%Y%m%d-%H%M%S")
        os.rename(firma_dir(slug), os.path.join(config.FIRMY_DIR, f"_smazana-{slug}-{stamp}"))
        name = nazev(slug, data)
        del data["firmy"][slug]
        _save(data)
    _log(user, "smazat-firmu", slug, nazev=name)
    return {"ok": True, "affected": lidi, "revoked": [u for u in lidi if u != int(user["id"])],
            "message": f"Firma „{name}“ je smazaná. Na serveru zůstala stranou, správce ji umí vrátit."}


def nastavit_druh(user, slug, novy):
    """Změní druh firmy (klienti ↔ projekty). Jen dokud je kořenová složka
    prázdná: plná složka by po změně přestala být skrytá a viděl by ji každý
    člen. Nový kořen se založí, starý (prázdný) zůstane."""
    if novy not in DRUHY:
        raise ValueError("Neznámý druh firmy.")
    with _locked():
        data = _load()
        _need_captain(user, slug, data)
        if druh(slug, data) == novy:
            return {"ok": True}
        if klienti(slug, data):
            raise ValueError(f"Ve složce {koren(slug, data)}/ už něco je — druh firmy jde změnit, "
                             "jen dokud je prázdná (jinak by ji uviděli všichni).")
        data["firmy"].setdefault(slug, {"lide": {}, "pristupy": {}})["druh"] = novy
        os.makedirs(os.path.join(vault_dir(slug), DRUHY[novy][0]), exist_ok=True)
        _save(data)
    _log(user, "druh", slug, na=novy)
    return {"ok": True, "affected": [m["id"] for m in clenove(slug)]}


def nastavit_klienty(user, slug, zapnout):
    """Dnešní firma: zapnout skrývání klientů a podkategorií."""
    if slug != LEGACY:
        raise ValueError("V nových firmách platí vždycky.")
    with _locked():
        data = _load()
        _need_captain(user, slug, data)
        data["firmy"].setdefault(slug, {"lide": {}, "pristupy": {}})["klienti"] = bool(zapnout)
        _save(data)
    _log(user, "klienti", slug, zapnuto=bool(zapnout))
    return {"ok": True, "affected": [m["id"] for m in clenove(slug)]}


def nastavit_cloveka(user, slug, uid, new_role, smi_mazat=False):
    """Přidá člověka do firmy, změní mu roli, nebo ho odebere (`new_role` '')."""
    accounts = _accounts()
    target = accounts.by_id(int(uid or 0)) if accounts else None
    if not target or target.get("disabled"):
        raise ValueError("Takový účet tu není.")
    if new_role and new_role not in ROLES:
        raise ValueError("Neznámá role.")
    with _locked():
        data = _load()
        _need_captain(user, slug, data)
        before = role(target, slug, data)
        if target["id"] == user["id"] and new_role != "kapitan":
            raise ValueError("Sám sobě generála nevezmeš — požádej jiného generála.")
        if before == "kapitan" and new_role != "kapitan" and not [
                k for k in kapitani(slug, data) if k != target["id"]]:
            raise ValueError("Firma musí mít aspoň jednoho generála.")
        if slug == LEGACY:
            _legacy_role(user, target, new_role, data, smi_mazat)
        else:
            lide = data["firmy"][slug].setdefault("lide", {})
            if new_role:
                lide[str(target["id"])] = {"role": new_role,
                                           "mazat": bool(smi_mazat) and new_role == "clen"}
            else:
                lide.pop(str(target["id"]), None)
                _forget_access(data["firmy"][slug], target["id"])
        _save(data)
    _log(user, "clovek", slug, komu=target["email"], z=before, na=new_role or "odebrán",
         mazat=bool(smi_mazat))
    return {"ok": True, "affected": [target["id"]],
            "revoked": [target["id"]] if before and not new_role else []}


def _legacy_role(user, target, new_role, data, smi_mazat=False):
    """Dnešní firma: role se zapisuje do správy přístupů (accounts), aby
    tlačítko Přístupy i claude-hub-admin firma ukazovaly totéž."""
    accounts = _accounts()
    entry = data["firmy"].setdefault(LEGACY, {"lide": {}, "pristupy": {}})
    lide = entry.setdefault("lide", {})
    if target.get("role") == "admin":
        if new_role != "kapitan":
            raise ValueError("Admin je v dnešní firmě generálem vždycky.")
        return
    level = {"kapitan": "write", "clen": "write", "host": "read", "": "none"}[new_role or ""]
    admin = {"role": "admin", "email": user.get("email", "")}
    accounts.set_company_level(admin, target["id"], level)
    if new_role in ("kapitan", "clen"):
        lide[str(target["id"])] = {"role": new_role,
                                   "mazat": bool(smi_mazat) and new_role == "clen"}
    else:
        lide.pop(str(target["id"]), None)
    if not new_role:
        _forget_access(entry, target["id"])


def nastavit_mazani(user, slug, uid, smi):
    with _locked():
        data = _load()
        _need_captain(user, slug, data)
        entry = data["firmy"].setdefault(slug, {"lide": {}, "pristupy": {}})
        accounts = _accounts()
        target = accounts.by_id(int(uid or 0)) if accounts else None
        if not target or role(target, slug, data) != "clen":
            raise ValueError("Mazání se nastavuje jen členům.")
        entry.setdefault("lide", {}).setdefault(str(target["id"]), {"role": "clen"})["mazat"] = bool(smi)
        _save(data)
    _log(user, "mazat", slug, komu=target["email"], smi=bool(smi))
    return {"ok": True}


def _forget_access(entry, uid):
    for rel, uids in list((entry.get("pristupy") or {}).items()):
        if uid in uids:
            entry["pristupy"][rel] = [u for u in uids if u != uid]


def _client_rel(slug, klient, podkategorie="", data=None):
    """Cesta nového klienta / podkategorie (Klienti/…), nebo projektu (Projekty/…)."""
    if hloubka(slug, data) == 1:
        if podkategorie:
            raise ValueError("Firma pracuje s projekty — podkategorie tu nejsou.")
        return f"{PROJEKTY}/{_name(klient, 'Název projektu')}"
    rel = f"{KLIENTI}/{_name(klient, 'Název klienta')}"
    if podkategorie:
        rel += "/" + _name(podkategorie, "Název podkategorie")
    return rel


def zalozit_slozku(user, slug, klient, podkategorie=""):
    """Nový klient (bez podkategorie), podkategorie u klienta, nebo projekt
    (u firmy s projekty). Nejhlubší úroveň dostane výchozí CLAUDE.md."""
    with _locked():
        data = _load()
        _need_captain(user, slug, data)
        if not klienti_zapnute(slug, data):
            raise ValueError("V téhle firmě nejsou klienti zapnutí.")
        rel = _client_rel(slug, klient, podkategorie, data)
        path = os.path.join(vault_dir(slug), *rel.split("/"))
        if podkategorie and not _isdir(slug, rel.rsplit("/", 1)[0]):
            raise ValueError("Takový klient tu není.")
        if os.path.lexists(path):
            raise ValueError("Taková složka už tu je.")
        os.makedirs(path)
        parts = rel.split("/")
        if len(parts) == hloubka(slug, data) + 1:
            pro = f" pro klienta {parts[1]}" if len(parts) == 3 else f" (firma {nazev(slug, data)})"
            with open(os.path.join(path, "CLAUDE.md"), "w", encoding="utf-8") as fh:
                fh.write(PODKATEGORIE_CLAUDE_MD.format(nazev=parts[-1], pro=pro))
        data["firmy"].setdefault(slug, {"lide": {}, "pristupy": {}}).setdefault(
            "pristupy", {}).setdefault(rel, [])
        _save(data)
    _log(user, "slozka", slug, cesta=rel)
    return {"ok": True, "cesta": rel}


def _cil(slug, rel, data):
    """(cesta, všechny úrovně nad ní) klienta / podkategorie / projektu, nebo ValueError."""
    scopes = _scope(slug, rel, data)
    if not scopes or not _isdir(slug, scopes[-1]):
        raise ValueError("Takový klient, podkategorie ani projekt tu není.")
    return scopes[-1], scopes


def nastavit_pristup(user, slug, rel, uid, ano):
    """Zpřístupní (nebo zavře) klienta, podkategorii či projekt jednomu člověku.
    Podkategorie s sebou zpřístupní i klienta (bez něj by nebyla vidět);
    zavřený klient zavře i všechny své podkategorie."""
    accounts = _accounts()
    target = accounts.by_id(int(uid or 0)) if accounts else None
    with _locked():
        data = _load()
        _need_captain(user, slug, data)
        if not target or not role(target, slug, data):
            raise ValueError("Tenhle člověk ve firmě není — nejdřív ho přidej.")
        rel, scopes = _cil(slug, rel, data)
        prist = data["firmy"].setdefault(slug, {"lide": {}, "pristupy": {}}).setdefault("pristupy", {})
        uid = target["id"]
        if ano:
            for scope in scopes:
                if uid not in prist.setdefault(scope, []):
                    prist[scope].append(uid)
        else:
            for scope in list(prist):
                if scope == rel or scope.startswith(rel + "/"):
                    prist[scope] = [u for u in prist[scope] if u != uid]
        _save(data)
    _log(user, "pristup", slug, komu=target["email"], cesta=rel, ano=bool(ano))
    return {"ok": True, "affected": [uid], "revoked": [] if ano else [uid]}


def prejmenovat_slozku(user, slug, rel, name):
    """Přejmenuje klienta, podkategorii nebo projekt i s přístupy."""
    with _locked():
        data = _load()
        _need_captain(user, slug, data)
        rel, _scopes = _cil(slug, rel, data)
        new = rel.rsplit("/", 1)[0] + "/" + _name(name)
        src = os.path.join(vault_dir(slug), *rel.split("/"))
        dst = os.path.join(vault_dir(slug), *new.split("/"))
        if os.path.lexists(dst):
            raise ValueError("Složka s tímhle názvem už tu je.")
        os.rename(src, dst)
        prist = data["firmy"].setdefault(slug, {"lide": {}, "pristupy": {}}).setdefault("pristupy", {})
        for scope in list(prist):
            if scope == rel or scope.startswith(rel + "/"):
                prist[new + scope[len(rel):]] = prist.pop(scope)
        _save(data)
    _log(user, "prejmenovat", slug, z=rel, na=new)
    return {"ok": True, "cesta": new, "affected": [m["id"] for m in clenove(slug)]}


def smazat_slozku(user, slug, rel):
    """Klienta, podkategorii nebo projekt odloží stranou (mimo trezor, jde vrátit)."""
    with _locked():
        data = _load()
        _need_captain(user, slug, data)
        rel, _scopes = _cil(slug, rel, data)
        aside = os.path.join(firma_dir(slug), "smazano")
        os.makedirs(aside, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        os.rename(os.path.join(vault_dir(slug), *rel.split("/")),
                  os.path.join(aside, f"{stamp}-" + rel.replace("/", "__")))
        prist = (data["firmy"].get(slug) or {}).get("pristupy") or {}
        for scope in list(prist):
            if scope == rel or scope.startswith(rel + "/"):
                del prist[scope]
        _save(data)
    _log(user, "smazat", slug, cesta=rel)
    return {"ok": True}


def nastavit_zakladatele(admin, uid, ano):
    if not admin or admin.get("role") != "admin":
        raise PermissionError("Kdo smí zakládat firmy, nastavuje admin.")
    with _locked():
        data = _load()
        ids = [u for u in data["zakladatele"] if u != int(uid)]
        if ano:
            ids.append(int(uid))
        data["zakladatele"] = sorted(ids)
        _save(data)
    return {"ok": True}


def prehled(user, slug, data=None):
    """Pro generála: lidé, klienti a podkategorie (nebo projekty) a kdo kam vidí."""
    data = data or _load()
    _need_captain(user, slug, data)
    return {"zkratka": slug, "nazev": nazev(slug, data), "klienti_zapnute": klienti_zapnute(slug, data),
            "druh": druh(slug, data), "lide": clenove(slug, data), "klienti": klienti(slug, data),
            "pristupy": pristupy(slug, data)}


def admin_kapitan(slug, uid):
    """claude-hub-admin: udělá z účtu generála firmy bez ohledu na to, kdo je
    přihlášený — záchrana firmy, jejíž generál odešel."""
    if slug == LEGACY:
        raise ValueError("V dnešní firmě jsou generálové admini — claude-hub-admin role <e-mail> admin.")
    with _locked():
        data = _load()
        if slug not in data["firmy"] or not os.path.isdir(vault_dir(slug)):
            raise ValueError("Taková firma tu není.")
        data["firmy"][slug].setdefault("lide", {})[str(int(uid))] = {"role": "kapitan"}
        _save(data)
    _log({"email": "claude-hub-admin"}, "kapitan", slug, komu=int(uid))


def forget_user(uid):
    """Smazaný účet: pryč ze všech firem a přístupů."""
    if not os.path.exists(REGISTRY):
        return
    with _locked():
        data = _load()
        for entry in data["firmy"].values():
            (entry.get("lide") or {}).pop(str(uid), None)
            _forget_access(entry, uid)
        data["zakladatele"] = [u for u in data["zakladatele"] if u != uid]
        _save(data)
