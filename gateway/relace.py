"""
Sdílené relace — majitel sdílí rozběhnutý chat s vybranými lidmi.

Kolega pak chat živě sleduje (konverzace, nástroje, kód, který Claude napsal),
vidí, že je sdílený a že Claude zrovna pracuje, a když na to má právo, píše do
něj taky. Chat zůstává v prostoru majitele: nic se nekopíruje a kolegovi se
neotevírá cizí hub — brána za něj čte z majitelova hubu a k majiteli se jeho
zprávy i vrací (hub/mcp_tools.py: sdilet_cteni, sdilet_poslat).

Kdo co smí, rozhoduje jen tenhle modul, ne prostor:

* sdílet a měnit členy, zrušit sdílení: majitel chatu (a správce),
* číst: majitel a členové,
* psát: majitel a členové s právem psát.

Registr drží brána mimo domovy (GATEWAY_DIR/relace.json) podle id účtů.
Kdo se zrovna dívá, se drží jen v paměti — je to „právě teď“, ne historie.

**Podkategorie firmy** (gateway/firmy.py) může být příjemcem stejně jako člověk:
chat pak vidí každý, kdo do podkategorie vidí (`skupiny`, psát `skupiny_psat`).
Vedle živého sdílení jde chat do podkategorie i **uložit** — kopie přepisu
leží ve složce firmy (`<firma>/chaty/`, mimo trezor) a kdokoli s přístupem
k podkategorii na ni naváže přes „Pokračovat u sebe“, i když autor z firmy
odešel nebo má prostor vypnutý. Nic se neukládá samo, jen na pokyn.
"""
import contextlib
import fcntl
import json
import os
import re
import secrets
import threading
import time

from . import config, firmy, shared

REGISTRY = os.path.join(config.GATEWAY_DIR, "relace.json")
LOG = os.path.join(config.GATEWAY_DIR, "relace.jsonl")
_LOCK = threading.Lock()
_DIVA = {}                    # id relace → {uid: čas posledního dotazu}
DIVA_S = 15.0                # po jak dlouhém tichu už se člověk nedívá
_PISE = {}                    # id relace → {uid: čas, kdy naposled psal}
PISE_S = 6.0                 # tak dlouho po posledním úhozu ještě „píše"
# Oznámení „X s tebou sdílí chat" se ukáže jednou na člověka (ne na zařízení).
# Kdo ho viděl, je v záznamu relace (`oznameno`). Sdílení starší než tahle
# evidence se berou jako oznámená — jinak by po aktualizaci vyskočila všechna znovu.
OZNAMENI_OD = 1793055600     # 27. 10. 2026 0:00 SEČ


@contextlib.contextmanager
def _locked():
    with _LOCK:
        os.makedirs(os.path.dirname(REGISTRY), exist_ok=True)
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
        return {}
    rel = data.get("relace") if isinstance(data, dict) else None
    return rel if isinstance(rel, dict) else {}


def _save(rel):
    tmp = REGISTRY + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"relace": rel}, fh, ensure_ascii=False, indent=2, sort_keys=True)
    os.replace(tmp, REGISTRY)


def _log(user, action, rid, **extra):
    entry = {"cas": time.strftime("%Y-%m-%d %H:%M:%S"), "email": user.get("email", ""),
             "akce": action, "relace": rid, **extra}
    try:
        with open(LOG, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _account(uid):
    return shared.ACCOUNTS.by_id(uid) if shared.ACCOUNTS and uid else None


def _jmeno(uid):
    u = _account(uid)
    return ((u.get("name") or u["email"]) if u else "")


def _je_spravce(user):
    return user.get("role") == "admin"


def role(user, entry):
    """'majitel' | 'pise' | 'cte' | '' — co uživatel s relací smí. Jmenovitě,
    nebo přes podkategorii firmy, do které vidí."""
    uid = int(user["id"])
    if entry.get("owner") == uid:
        return "majitel"
    if uid in entry.get("psat", []):
        return "pise"
    groups = [g for g in entry.get("skupiny") or [] if firmy.ve_skupine(user, g)]
    if any(g in (entry.get("skupiny_psat") or []) for g in groups):
        return "pise"
    if uid in entry.get("members", []) or groups:
        return "cte"
    return ""


def _public(rid, entry, user):
    uid = int(user["id"])
    owner = _account(entry.get("owner"))
    members = []
    for mid in entry.get("members", []):
        u = _account(mid)
        if u:
            members.append({"email": u["email"], "name": u.get("name", ""),
                            "pise": mid in entry.get("psat", [])})
    teď = time.time()
    # Dívá se každý kromě mě, i majitel (ten se hlásí z vlastního hubu).
    diva = [_jmeno(m) for m, t in (_DIVA.get(rid) or {}).items()
            if teď - t < DIVA_S and m != uid]
    pisou = [_jmeno(m) for m, t in (_PISE.get(rid) or {}).items()
             if teď - t < PISE_S and m != uid]
    return {"id": rid, "chat": entry.get("chat", ""), "titulek": entry.get("titulek") or "Chat",
            "majitel": (owner.get("name") or owner["email"]) if owner else "",
            "role": role(user, entry) or ("spravce" if _je_spravce(user) else ""),
            "clenove": members, "diva": diva, "pisou": pisou, "vytvoreno": entry.get("vytvoreno", 0),
            "oznameno": (uid in entry["oznameno"]) if isinstance(entry.get("oznameno"), list)
                        else entry.get("vytvoreno", 0) < OZNAMENI_OD,
            "skupiny": [{"id": g, "nazev": firmy.skupina_nazev(g),
                         "pise": g in (entry.get("skupiny_psat") or [])}
                        for g in entry.get("skupiny") or []]}


def seznam(user):
    """{moje: relace, kterou jsem založil, semnou: relace, kde jsem člen}."""
    uid = int(user["id"])
    rel = _load()
    moje, semnou = [], []
    for rid, entry in sorted(rel.items(), key=lambda x: -x[1].get("vytvoreno", 0)):
        if entry.get("owner") == uid:
            moje.append(_public(rid, entry, user))
        elif role(user, entry):
            semnou.append(_public(rid, entry, user))
    return {"moje": moje, "semnou": semnou, "ulozene": ulozene(user)}


def _skupiny(user, raw):
    """Podkategorie z formuláře — jen ty, kam `user` smí sdílet."""
    if isinstance(raw, str):
        raw = [g for g in raw.split(",") if g.strip()]
    allowed = {g["id"] for g in firmy.skupiny_pro(user)}
    out = []
    for g in raw or []:
        g = str(g or "").strip()
        if g not in allowed:
            raise ValueError("Do téhle podkategorie sdílet nemůžeš (nevidíš ji, nebo jsi ve firmě host).")
        if g not in out:
            out.append(g)
    return out


def _chat_id(chat):
    chat = str(chat or "").strip()
    if not chat or len(chat) > 80 or not all(c.isalnum() or c in "-_" for c in chat):
        raise ValueError("Tenhle chat se nedá sdílet (chybí jeho číslo) — pošli v něm aspoň jednu zprávu.")
    return chat


def zalozit(user, chat, titulek, emails, psat_emails=(), skupiny=(), skupiny_psat=()):
    """Nová sdílená relace nad chatem majitele (nebo změna členů, když už je).
    Příjemci jsou lidé (`emails`) i podkategorie firem (`skupiny`)."""
    chat = _chat_id(chat)
    ids, neznami = shared._resolve(emails)
    psat, _ = shared._resolve(psat_emails)
    uid = int(user["id"])
    ids = [m for m in ids if m != uid]
    groups = _skupiny(user, skupiny)
    groups_psat = [g for g in _skupiny(user, skupiny_psat) if g in groups]
    if not ids and not groups:
        raise ValueError("Vyber aspoň jednoho kolegu nebo podkategorii, se kterými chat sdílíš.")
    psat = [m for m in psat if m in ids]
    with _locked():
        rel = _load()
        rid = next((r for r, e in rel.items() if e.get("owner") == uid and e.get("chat") == chat), None)
        novy = rid is None
        if novy:
            rid = secrets.token_urlsafe(9)
            rel[rid] = {"owner": uid, "chat": chat, "vytvoreno": int(time.time()), "oznameno": []}
        elif not isinstance(rel[rid].get("oznameno"), list):
            # Sdílení z doby před evidencí oznámení: dosavadní členové ho znají,
            # nově přidaní se dozvědí.
            rel[rid]["oznameno"] = list(rel[rid].get("members", []))
        rel[rid].update(members=ids, psat=psat, titulek=str(titulek or "")[:120],
                        skupiny=groups, skupiny_psat=groups_psat)
        _save(rel)
    _log(user, "zalozeno" if novy else "zmeneno", rid, clenove=len(ids), psat=len(psat),
         podkategorie=[firmy.skupina_nazev(g) for g in groups])
    zprava = ("Chat je sdílený." if novy else "Sdílení je upravené.")
    if neznami:
        zprava += " Neznámé adresy: " + ", ".join(neznami) + "."
    return {"ok": True, "id": rid, "message": zprava}


def zrusit(user, rid):
    with _locked():
        rel = _load()
        entry = rel.get(rid)
        if not entry:
            raise ValueError("Tohle sdílení už neexistuje.")
        if entry.get("owner") != int(user["id"]) and not _je_spravce(user):
            raise ValueError("Sdílení může zrušit jen ten, kdo chat sdílí.")
        del rel[rid]
        _save(rel)
    _DIVA.pop(rid, None)
    _log(user, "zruseno", rid)
    return {"ok": True, "message": "Sdílení je zrušené."}


def odejit(user, rid):
    uid = int(user["id"])
    with _locked():
        rel = _load()
        entry = rel.get(rid)
        if not entry or uid not in entry.get("members", []):
            raise ValueError("V téhle relaci nejsi jmenovitě — vidíš ji přes podkategorii firmy."
                             if entry and role(user, entry) else "V téhle relaci nejsi.")
        entry["members"] = [m for m in entry["members"] if m != uid]
        entry["psat"] = [m for m in entry.get("psat", []) if m != uid]
        if not entry["members"] and not entry.get("skupiny"):
            del rel[rid]
        _save(rel)
    _log(user, "odchod", rid)
    return {"ok": True, "message": "Odešel jsi ze sdílení."}


def oznameno(user, rids):
    """Člověk viděl oznámení o sdílených chatech — podruhé se mu neukáže,
    ani na jiném zařízení."""
    uid = int(user["id"])
    with _locked():
        rel = _load()
        zmena = False
        for rid in rids or []:
            entry = rel.get(str(rid))
            if entry and role(user, entry) and uid not in (entry.get("oznameno") or []):
                entry["oznameno"] = (entry.get("oznameno") or []) + [uid]
                zmena = True
        if zmena:
            _save(rel)
    return {"ok": True}


def ziskat(user, rid, psat=False):
    """Záznam relace, k němuž má uživatel přístup — nebo ValueError. Zápis
    (`psat=True`) smí majitel a členové s právem psát. Tímhle projde každé
    čtení i zápis z prohlížeče; nic jiného nevolá hub majitele."""
    entry = _load().get(str(rid or ""))
    r = role(user, entry) if entry else ""
    if not r or (psat and r == "cte"):
        raise ValueError("K téhle relaci nemáš přístup.")
    return entry, r


def dival_se(user, rid):
    """Zaznamená, že se uživatel právě dívá (ukazuje se majiteli i ostatním)."""
    _DIVA.setdefault(rid, {})[int(user["id"])] = time.time()


def pritomnost(user, rid, pise=False):
    """Člověk je v chatu (a případně právě píše). Hlásí se z okna každých pár
    sekund; píše smí jen ten, kdo smí psát — ostatním se „píše" nikdy neukáže."""
    entry, r = ziskat(user, rid)
    uid = int(user["id"])
    teď = time.time()
    _DIVA.setdefault(rid, {})[uid] = teď
    if pise and r in ("majitel", "pise"):
        _PISE.setdefault(rid, {})[uid] = teď
    else:
        (_PISE.get(rid) or {}).pop(uid, None)
    return {"ok": True}


def jmeno(user):
    return user.get("name") or user.get("email") or "?"


def forget_user(uid):
    """Smazaný účet: jeho relace zmizí, z cizích odejde."""
    with _locked():
        rel = _load()
        zmena = False
        for rid, entry in list(rel.items()):
            if entry.get("owner") == uid:
                del rel[rid]
                zmena = True
            elif uid in entry.get("members", []):
                entry["members"] = [m for m in entry["members"] if m != uid]
                entry["psat"] = [m for m in entry.get("psat", []) if m != uid]
                zmena = True
                if not entry["members"] and not entry.get("skupiny"):
                    del rel[rid]
        if zmena:
            _save(rel)


# ── chaty uložené do podkategorie ────────────────────────────────────────────
ULOZENY = re.compile(r"[A-Za-z0-9_-]{8,40}")


def _chaty_dir(slug):
    return os.path.join(firmy.firma_dir(slug), "chaty")


def ulozit(user, chat, titulek, skupiny):
    """Kopie chatu `user` do podkategorií firem — na pokyn, ne sama. Zip leží
    ve složce firmy mimo trezor; navazuje se na něj přes „Pokračovat u sebe“."""
    from hub import prenos
    from . import workspace
    chat = _chat_id(chat)
    groups = _skupiny(user, skupiny)
    if not groups:
        raise ValueError("Vyber podkategorii, kam chat uložit.")
    data, meta = prenos.zabal(os.path.join(workspace.home_for(user), ".claude"), chat,
                              titulek or "Chat", odkud=jmeno(user))
    for g in groups:
        slug, rel = firmy.skupina(g)
        folder = _chaty_dir(slug)
        os.makedirs(folder, mode=0o700, exist_ok=True)
        sid = secrets.token_urlsafe(12)
        with open(os.path.join(folder, sid + ".zip"), "wb") as fh:
            fh.write(data)
        info = {"skupina": g, "owner": int(user["id"]), "chat": chat,
                "titulek": meta.get("title") or "Chat", "vytvoreno": int(time.time())}
        with open(os.path.join(folder, sid + ".json"), "w", encoding="utf-8") as fh:
            json.dump(info, fh, ensure_ascii=False)
        _log(user, "ulozeno", sid, podkategorie=firmy.skupina_nazev(g))
    names = ", ".join(firmy.skupina_nazev(g) for g in groups)
    return {"ok": True, "message": f"Chat je uložený do: {names}. Kdo tam vidí, naváže na něj "
                                   "v Sdílených chatech tlačítkem Pokračovat u sebe."}


def _ulozene_vse():
    """[(sid, slug, info)] — všechny uložené chaty všech firem."""
    out = []
    for slug in firmy.slugs():
        folder = _chaty_dir(slug)
        try:
            names = os.listdir(folder)
        except OSError:
            continue
        for name in names:
            sid = name[:-5] if name.endswith(".json") else ""
            if not ULOZENY.fullmatch(sid):
                continue
            try:
                with open(os.path.join(folder, name), encoding="utf-8") as fh:
                    info = json.load(fh)
            except (OSError, ValueError):
                continue
            if isinstance(info, dict) and str(info.get("skupina") or "").startswith(slug + ":"):
                out.append((sid, slug, info))
    return out


def ulozene(user):
    """Uložené chaty v podkategoriích, do kterých `user` vidí."""
    data = firmy._load()
    out = []
    for sid, slug, info in sorted(_ulozene_vse(), key=lambda x: -x[2].get("vytvoreno", 0)):
        if not firmy.ve_skupine(user, info["skupina"], data):
            continue
        out.append({"id": sid, "titulek": info.get("titulek") or "Chat", "firma": slug,
                    "skupina": firmy.skupina_nazev(info["skupina"], data),
                    "autor": _jmeno(info.get("owner")) or "(už tu není)",
                    "moje": info.get("owner") == int(user["id"]),
                    "vytvoreno": info.get("vytvoreno", 0)})
    return out


def ulozeny(user, sid):
    """(info, cesta k zipu) uloženého chatu, ke kterému má `user` přístup."""
    sid = str(sid or "")
    for s, slug, info in _ulozene_vse():
        if s == sid and firmy.ve_skupine(user, info["skupina"]):
            return info, os.path.join(_chaty_dir(slug), sid + ".zip")
    raise ValueError("K tomuhle uloženému chatu nemáš přístup.")


def smazat_ulozeny(user, sid):
    """Smaže uložený chat — jeho autor, nebo kapitán firmy."""
    sid = str(sid or "")
    for s, slug, info in _ulozene_vse():
        if s != sid:
            continue
        if info.get("owner") != int(user["id"]) and firmy.role(user, slug) != "kapitan":
            raise ValueError("Uložený chat smaže jen ten, kdo ho uložil, nebo kapitán firmy.")
        for ext in (".zip", ".json"):
            try:
                os.remove(os.path.join(_chaty_dir(slug), sid + ext))
            except OSError:
                pass
        _log(user, "smazano-ulozeny", sid)
        return {"ok": True, "message": "Uložený chat je smazaný."}
    raise ValueError("Takový uložený chat tu není.")


_PSANI = {}                   # uid → časy posledních zpráv (proti zahlcení)


def smi_poslat(user):
    """Nejvýš 6 zpráv za 20 s na člověka — Claude Code to stejně nestíhá číst."""
    teď = time.time()
    casy = [t for t in _PSANI.get(int(user["id"]), []) if teď - t < 20]
    if len(casy) >= 6:
        _PSANI[int(user["id"])] = casy
        return False
    casy.append(teď)
    _PSANI[int(user["id"])] = casy
    return True


def verejne(user, rid, entry):
    return _public(rid, entry, user)


def pridej_stav(result, pub, moje_role, user=None, majitel_offline=False):
    """K odpovědi s přepisem přidá, kdo chat sdílí, kdo se dívá, co smím a jak se
    jmenuju (podle toho viewer pozná, které bubliny jsou moje)."""
    psat = moje_role in ("majitel", "pise") and not majitel_offline
    result["relace"] = {"majitel": pub["majitel"], "titulek": pub["titulek"],
                        "clenove": pub["clenove"], "diva": pub["diva"], "pisou": pub["pisou"],
                        "role": moje_role, "psat": psat, "ja": jmeno(user) if user else "",
                        "majitel_offline": bool(majitel_offline)}


def prepis_offline(owner, chat):
    """Přepis sdíleného chatu přímo z domova majitele — pro chvíli, kdy mu
    prostor neběží. Jen čtení a jen soubor, který opravdu leží v jeho domově
    (prostor smí v domově zakládat odkazy, brána je nenásleduje)."""
    from hub import cteni
    from . import workspace
    if not cteni.je_id(chat) or not owner:
        return ""
    home = workspace.home_for(owner)
    root = os.path.join(home, ".claude", "projects")
    real_home = os.path.realpath(home) + os.sep
    try:
        folders = os.listdir(root)
    except OSError:
        return ""
    for folder in folders:
        path = os.path.join(root, folder, chat + ".jsonl")
        if os.path.isfile(path) and os.path.realpath(path).startswith(real_home):
            return path
    return ""


_ZAPNUTI = {}                 # (uid, relace) → čas posledního zapnutí


def smi_zapnout(user, rid):
    """Zapnout stejný chat znovu jde nejdřív za 30 s — spuštění prostoru něco stojí."""
    klic = (int(user["id"]), str(rid))
    teď = time.time()
    if teď - _ZAPNUTI.get(klic, 0) < 30:
        return False
    _ZAPNUTI[klic] = teď
    return True
