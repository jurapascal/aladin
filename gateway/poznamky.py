"""
Kdo vidí kterou poznámku a složku ve firemním Obsidianu.

Bez záznamu poznámku vidí každý, kdo vidí firemní Obsidian (gateway/accounts.py:
none / read / write). U poznámky i u složky jde vybrat konkrétní lidi — pak ji
vidí jen oni a správci poznámek. Pravidlo složky platí na všechno v ní, i na
poznámky, které do ní přibudou později; poznámka ve složce musí projít
pravidlem složky i svým vlastním.

Složky `Lidé/` a `Sdílené/` sem nepatří — kdo je vidí, určuje vlastník
(gateway/slozky.py): osobní složku jen on, sdílenou její členové. Nastavovat to smí jen **správci poznámek**: pevný
seznam účtů (`claude-hub-admin poznamky spravci …`), nezávislý na roli admin.

Skrytí je natvrdo: prostor skrytou poznámku vůbec nemá. Firemní trezor se do
sandboxu přiváže celý jen ke čtení a přes složky, ve kterých je něco skrytého,
se položí prázdný tmpfs, do kterého se znovu přivážou jen viditelné položky
(`mask_args`). Claude, terminál ani hub v prostoru tak o skryté poznámce nevědí.
Změna se projeví při dalším startu prostoru; kdo přístup ztratil a zrovna
nepracuje, tomu brána prostor zastaví hned.

Registr leží vedle trezoru, ne v něm (COMPANY_DIR/poznamky-pristupy.json), podle
id účtů a relativních cest (`poznamky` a `slozky`). Přesun poznámky mimo bránu (git, Obsidian na
počítači správce) omezení nepřenese — poznámka na nové cestě je pro všechny.

Každá firma (gateway/firmy.py) má registr ve své složce. U dnešní firmy
(`firma`) jsou správci poznámek pevný seznam jako dřív, u nových firem jsou
to její generálové. Klienty a podkategorie (výchozí skrytí) hlídá firmy.py —
tady se jen přidají ke skrytým cestám.
"""
import contextlib
import fcntl
import json
import os
import re
import threading
import time

from . import config, firmy, shared, slozky

LEGACY = firmy.LEGACY
REGISTRY = os.path.join(config.COMPANY_DIR, "poznamky-pristupy.json")
LOG = os.path.join(config.COMPANY_DIR, "poznamky-pristupy.jsonl")
_LOCK = threading.Lock()


def _registry(firma=LEGACY):
    if firma == LEGACY:
        return REGISTRY
    return os.path.join(firmy.firma_dir(firma), "poznamky-pristupy.json")


def _logpath(firma=LEGACY):
    if firma == LEGACY:
        return LOG
    return os.path.join(firmy.firma_dir(firma), "poznamky-pristupy.jsonl")
# Místo prázdného seznamu u poznámky, kterou vidí jen správci.
NOBODY = -1


@contextlib.contextmanager
def _locked(firma=LEGACY):
    with _LOCK:
        os.makedirs(os.path.dirname(_registry(firma)), exist_ok=True)
        with open(_registry(firma) + ".lock", "a") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)


def _load(firma=LEGACY):
    try:
        with open(_registry(firma), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    managers = [int(u) for u in data.get("spravci") or [] if isinstance(u, int)]
    if firma != LEGACY:
        managers = firmy.kapitani(firma)     # u nových firem spravují generálové
    out = {"spravci": managers}
    for key in ("poznamky", "slozky"):
        rules = data.get(key) if isinstance(data.get(key), dict) else {}
        out[key] = {str(rel): [int(u) for u in uids if isinstance(u, int)]
                    for rel, uids in rules.items() if isinstance(uids, list)}
    return out


def _save(data, firma=LEGACY):
    if firma != LEGACY:
        data = {**data, "spravci": []}
    tmp = _registry(firma) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2, sort_keys=True)
    os.replace(tmp, _registry(firma))


def _log(user, rel, before, after, firma=LEGACY):
    before = [u for u in before or [] if u != NOBODY] if before else None
    after = [u for u in after if u != NOBODY] if after is not None else None
    entry = {"cas": time.strftime("%Y-%m-%d %H:%M:%S"), "email": (user or {}).get("email", ""),
             "poznamka": rel, "z": ([_email(u) for u in before] or "jen správci") if before is not None else "všichni",
             "na": ([_email(u) for u in after] or "jen správci") if after is not None else "všichni"}
    try:
        with open(_logpath(firma), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _account(uid):
    return shared.ACCOUNTS.by_id(uid) if shared.ACCOUNTS and uid else None


def _email(uid):
    u = _account(uid)
    return u["email"] if u else f"#{uid}"


def _rel(rel):
    from .workspace import company_rel
    return company_rel(rel)


def _dir_rel(rel):
    """Cesta ke složce ve firemním trezoru — jako company_rel, jen bez .md."""
    raw = str(rel or "").replace("\\", "/").strip()
    parts = [p for p in raw.strip("/").split("/") if p not in ("", ".")]
    if (not parts or len(raw) > 300 or raw.startswith("/") or re.match(r"^[A-Za-z]:", raw)
            or any(p == ".." or p.startswith(".") for p in parts)):
        raise ValueError(f"Neplatná cesta ve firemním Obsidianu: {raw or '(prázdná)'}")
    return "/".join(parts)


# ── kdo co smí ───────────────────────────────────────────────────────────────
def managers(firma=LEGACY):
    return list(_load(firma)["spravci"])


def is_manager(user, firma=LEGACY):
    return bool(user) and int(user["id"]) in _load(firma)["spravci"]


def _sees(data, uid, rel):
    """Vidí `uid` cestu `rel`? Musí projít pravidly všech složek nad ní
    i vlastním (poznámky, nebo složky)."""
    if uid in data["spravci"]:
        return True
    parts = rel.split("/")
    for i in range(1, len(parts) + 1):
        path = "/".join(parts[:i])
        allowed = data["slozky"].get(path)
        if i == len(parts):
            allowed = data["poznamky"].get(path) or allowed
        if allowed and uid not in allowed:
            return False
    return True


def allowed(user, rel, firma=LEGACY):
    """Smí `user` poznámku `rel` vidět (a tedy i přepsat)? I podle klientů
    a podkategorií firmy (firmy.vidi)."""
    try:
        rel = _rel(rel)
    except ValueError:
        return True                      # neplatnou cestu odmítne až zápis sám
    return _sees(_load(firma), int(user["id"]), rel) and firmy.vidi(user, firma, rel)


def allowed_path(user, rel, firma=LEGACY):
    """Jako `allowed`, jen pro jakýkoli soubor (ne jen .md) — nahrávání."""
    return _sees(_load(firma), int(user["id"]), str(rel)) and firmy.vidi(user, firma, str(rel))


def hidden_for(user, data=None, firma=LEGACY):
    """Relativní cesty poznámek a složek, které `user` nevidí a v trezoru
    opravdu jsou — i cizí osobní a sdílené složky (gateway/slozky.py)
    a klienti či podkategorie bez přístupu (gateway/firmy.py).
    Co leží ve skryté složce, se už nevypisuje: skryje se s ní."""
    data = data or _load(firma)
    uid = int(user["id"])
    vault = firmy.vault_dir(firma)

    def there(rel, check):
        path = os.path.join(vault, *rel.split("/"))
        return check(path) and not os.path.islink(path)

    hidden = {rel for rel in data["poznamky"] if there(rel, os.path.isfile)} | \
             {rel for rel in data["slozky"] if there(rel, os.path.isdir)}
    hidden = {rel for rel in hidden if not _sees(data, uid, rel)}
    hidden |= set(firmy.skryte(user, firma)[0])
    hidden = {rel for rel in hidden
              if not any(rel.startswith(h + "/") for h in hidden)}
    if firma == LEGACY:
        hidden |= set(slozky.hidden_for(user))
    return sorted(hidden)


def always_masked(user, firma=LEGACY):
    """Složky, přes které se má uživateli položit tmpfs, i když v nich nic
    skrytého není — kvůli novým klientům a podkategoriím (firmy.skryte)."""
    return firmy.skryte(user, firma)[1]


def masked_dirs(firma=LEGACY):
    """Složky (relativně k trezoru), ve kterých je nějaká omezená poznámka nebo
    složka — aspoň pro někoho v nich leží tmpfs s jednotlivě přivázanými soubory."""
    data = _load(firma)
    return ({os.path.dirname(rel) for rel in data["poznamky"]}
            | {os.path.dirname(rel) for rel in data["slozky"]}
            | (set(slozky.ROOTS) if firma == LEGACY else set()) | firmy.maskovane(firma))


# ── sandbox ──────────────────────────────────────────────────────────────────
def mask_args(vault, hidden, always=()):
    """Argumenty bwrap, které ze svázaného trezoru `vault` vynechají `hidden`.
    Přes složky z `always` se tmpfs položí, i když v nich nic skryté není.

    Musí přijít až po `--ro-bind vault vault`. Každá složka, ve které je něco
    skrytého, dostane přes sebe prázdný tmpfs a do něj se přivážou zpátky jen
    viditelné položky (podsložky celé, takže nové soubory v nich jsou vidět
    hned). Rodiče jdou před dětmi — podsložka, ve které je taky něco skrytého,
    se nejdřív přiváže a pak se přes ni položí vlastní tmpfs. Odkazy se
    nepřivazují, ale vyrobí znovu: bind by je na serveru následoval kamkoli.
    """
    vault = os.path.realpath(vault)
    by_dir = {}
    for rel in hidden:
        parts = rel.split("/")
        by_dir.setdefault("/".join(parts[:-1]), set()).add(parts[-1])
    for rel in always:
        by_dir.setdefault(rel, set())
    args = []
    for rel_dir in sorted(by_dir, key=lambda d: (d.count("/") if d else -1, d)):
        src = os.path.join(vault, *rel_dir.split("/")) if rel_dir else vault
        if os.path.realpath(src) != src or not os.path.isdir(src):
            continue                     # odkaz nebo nic — nepřivazovat
        try:
            names = sorted(os.listdir(src))
        except OSError:
            continue
        args += ["--tmpfs", src]
        for name in names:
            if name in by_dir[rel_dir]:
                continue
            path = os.path.join(src, name)
            if os.path.islink(path):
                args += ["--symlink", os.readlink(path), path]
            elif os.path.isdir(path) or os.path.isfile(path):
                args += ["--ro-bind", path, path]
        args += ["--remount-ro", src]
    return args


# ── správa ───────────────────────────────────────────────────────────────────
def people(firma=LEGACY):
    """Kdo je v týmu a jestli vidí firemní Obsidian — z toho se vybírá."""
    if not shared.ACCOUNTS:
        return []
    data = _load(firma)
    if firma != LEGACY:
        level = {"kapitan": "write", "clen": "write", "host": "read"}
        return [{"email": m["email"], "name": m["name"], "firma": level[m["role"]],
                 "spravce": m["id"] in data["spravci"]} for m in firmy.clenove(firma)]
    out = []
    for r in shared.ACCOUNTS.list():
        if r["disabled"]:
            continue
        level = shared.ACCOUNTS.company_level(r)
        out.append({"email": r["email"], "name": r["name"], "firma": level,
                    "spravce": r["id"] in data["spravci"]})
    return out


def listing(firma=LEGACY):
    """Omezené poznámky i složky a kdo je vidí (pro správce). Složky jsou
    bez .md, podle toho se od poznámek poznají."""
    data = _load(firma)
    out = {}
    for rel, uids in sorted({**data["slozky"], **data["poznamky"]}.items()):
        out[rel] = [{"email": u["email"], "name": u.get("name", "")}
                    for u in (_account(i) for i in uids) if u]
    return out


def folders(firma=LEGACY):
    """Cesty omezených složek."""
    return sorted(_load(firma)["slozky"])


def _resolve(emails):
    ids, unknown = [], []
    for email in emails or []:
        email = str(email or "").strip().lower()
        if not email:
            continue
        u = shared.ACCOUNTS.get(email) if shared.ACCOUNTS else None
        if not u or u.get("disabled"):
            unknown.append(email)
        elif u["id"] not in ids:
            ids.append(u["id"])
    return ids, unknown


def _target(rel, firma=LEGACY):
    """(druh, cesta): složka (`slozky`), nebo poznámka (`poznamky`)."""
    vault = firmy.vault_dir(firma)
    raw = str(rel or "").replace("\\", "/").strip().strip("/")
    if firma == LEGACY and slozky.special(raw):
        raise ValueError("Kdo vidí osobní a sdílené složky, určuje jejich vlastník — "
                         f"v {slozky.PEOPLE}/ a {slozky.SHARED}/ se to tady nenastavuje.")
    if not raw.lower().endswith(".md"):
        folder = _dir_rel(raw)
        path = os.path.join(vault, *folder.split("/"))
        if os.path.isdir(path) and not os.path.islink(path):
            return "slozky", folder
    rel = _rel(raw)
    if not os.path.isfile(os.path.join(vault, *rel.split("/"))):
        raise ValueError(f"{rel} ve firemním Obsidianu není.")
    return "poznamky", rel


def set_note(user, rel, emails, only=None, firma=LEGACY):
    """Nastaví, kdo poznámku (nebo složku) vidí. `only` False (nebo prázdný
    seznam bez `only`) = všichni, omezení se zruší. `only` s prázdným
    seznamem = jen správci poznámek.

    Vrací {"ok", "path", "slozka", "people", "revoked": [id účtů, které ji
    přestaly vidět], "granted": [id, které ji nově vidí]}."""
    if not is_manager(user, firma):
        raise PermissionError("Kdo vidí poznámku, nastavují jen správci poznámek.")
    kind, rel = _target(rel, firma)
    if isinstance(emails, str):
        emails = emails.replace(",", " ").split()
    ids, unknown = _resolve(emails)
    if unknown:
        raise ValueError("Tihle lidé tu účet nemají: " + ", ".join(unknown))
    everyone = [r["id"] for r in shared.ACCOUNTS.list()] if shared.ACCOUNTS else []
    if only is None:
        only = bool(ids)
    with _locked(firma):
        data = _load(firma)
        before = data[kind].get(rel, [])
        saw = {u for u in everyone if _sees(data, u, rel)}
        if only:
            # -1 = nikdo navíc; prázdný seznam by znamenal „všichni“.
            data[kind][rel] = sorted(ids) or [NOBODY]
        else:
            ids = []
            data[kind].pop(rel, None)
        sees = {u for u in everyone if _sees(data, u, rel)}
        _save(data, firma)
    _log(user, rel, before, ids if only else None, firma)
    return {"ok": True, "path": rel, "slozka": kind == "slozky", "jen": bool(only),
            "people": [_email(i) for i in ids],
            "revoked": sorted(saw - sees), "granted": sorted(sees - saw)}


def set_managers(emails):
    """Správci poznámek (claude-hub-admin). Nahradí celý seznam."""
    ids, unknown = _resolve(emails)
    if unknown:
        raise ValueError("Tihle lidé tu účet nemají: " + ", ".join(unknown))
    with _locked():
        data = _load()
        data["spravci"] = ids
        _save(data)
    return [_email(i) for i in ids]


def forget_user(uid):
    """Smazaný účet: pryč ze správců i ze všech poznámek (ve všech firmách)."""
    for firma in firmy.slugs():
        _forget_user(uid, firma)


def _forget_user(uid, firma):
    """Poznámka, kterou by pak neviděl nikdo, zůstane omezená — vidí ji správci."""
    if not os.path.exists(_registry(firma)):
        return
    with _locked(firma):
        data = _load(firma)
        changed = uid in data["spravci"]
        data["spravci"] = [u for u in data["spravci"] if u != uid]
        for rules in (data["poznamky"], data["slozky"]):
            for rel, uids in rules.items():
                if uid in uids:
                    rules[rel] = [u for u in uids if u != uid] or [NOBODY]
                    changed = True
        if changed:
            _save(data, firma)
