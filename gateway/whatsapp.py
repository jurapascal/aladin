"""
WhatsApp pro firmy — rozhodování, kdo smí botovi psát a co se s tím stane.

Tohle je mozek. Samotné spojení s WhatsAppem (párování, příjem a odesílání
zpráv) dělá wa-bridge (`gateway/wa-bridge/`, Node + Baileys); o právech neví
nic a každou událost předá sem. Tady se rozhodne, jestli se odpoví, komu a
s jakými právy. Návrh: NAVRH-WhatsApp.md.

Pravidla, na kterých to stojí:

* **Jedno číslo bota = jedna firma.** Skupina patří právě jedné firmě.
* **Důvěra jde od lidí z firmy, ne od čísla.** Soukromě odpovídáme jen člověku,
  jehož číslo je ověřené u účtu a kdo je ve firmě (gateway/firmy.py). Neznámé
  číslo = ticho.
* **Skupinu povolí jen člověk z firmy**, který bota přidá (nebo `/povol`).
  Kdo skupinu povolil a přestal být členem, tomu skupina přestane platit.
* **Ve skupině nejnižší práva**: žádné nástroje, žádný trezor.
* **Číslo není heslo**: ověřuje se jednorázovým kódem z hubu.

Registr leží mimo trezory (FIRMY_DIR/.whatsapp.json), podle id účtů, a mění ho
jen tenhle modul. Texty zpráv se nikam neukládají; záznam (whatsapp.jsonl) je
bez obsahu.
"""
import contextlib
import fcntl
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time

from . import config, firmy, shared

KOD_PLATI = 10 * 60          # jak dlouho jde ověřit číslo kódem
KOD_POKUSY = 5               # kolik špatných kódů, než se kód zahodí
LIMIT_HODINA = 30            # zpráv na člověka za hodinu
LIMIT_DEN = 200              # zpráv na číslo bota za den
TELEFON = re.compile(r"\d{7,15}")
_LOCK = threading.Lock()
_POUZITI = {}                # (slug, klíč) -> [časy]; jen v paměti, po restartu čisté
_POKUSY = {}                 # uid -> špatných kódů


def _registr():
    return os.path.join(config.FIRMY_DIR, ".whatsapp.json")


def _zaznam():
    return os.path.join(config.FIRMY_DIR, "whatsapp.jsonl")


@contextlib.contextmanager
def _locked():
    with _LOCK:
        os.makedirs(config.FIRMY_DIR, exist_ok=True)
        with open(_registr() + ".lock", "a") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)


def _load():
    try:
        with open(_registr(), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    out = {}
    for klic in ("cisla", "propojeni", "skupiny", "blokace"):
        out[klic] = data.get(klic) if isinstance(data.get(klic), dict) else {}
    return out


def _save(data):
    tmp = _registr() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2, sort_keys=True)
    os.chmod(tmp, 0o600)
    os.replace(tmp, _registr())


def zaznam(akce, slug="", kdo="", **extra):
    """Řádek do whatsapp.jsonl: co se stalo, bez obsahu zpráv."""
    entry = {"cas": time.strftime("%Y-%m-%d %H:%M:%S"), "akce": akce, "firma": slug,
             "kdo": kdo, **extra}
    try:
        os.makedirs(config.FIRMY_DIR, exist_ok=True)
        with open(_zaznam(), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


def telefon(text):
    """Číslice telefonu (s předvolbou, bez +), nebo '' když to číslo není."""
    cisla = re.sub(r"\D", "", str(text or ""))
    return cisla if TELEFON.fullmatch(cisla) else ""


def _ucty():
    return shared.ACCOUNTS


def _ucet(uid):
    ucty = _ucty()
    return ucty.by_id(int(uid)) if ucty and str(uid).isdigit() else None


def _kapitan(user, slug):
    if not user or firmy.role(user, slug) != "kapitan":
        raise PermissionError("WhatsApp firmy spravuje jen její generál.")


# ── číslo bota firmy ─────────────────────────────────────────────────────────
def cislo_firmy(slug):
    """Záznam čísla bota firmy, nebo None."""
    return _load()["cisla"].get(slug)


def firma_podle_cisla(tel):
    """Zkratka firmy, které patří číslo bota, nebo ''."""
    tel = telefon(tel)
    for slug, entry in _load()["cisla"].items():
        if tel and entry.get("telefon") == tel:
            return slug
    return ""


def nastavit_cislo(user, slug, tel):
    """Generál zapíše číslo bota, které právě spároval. Jedno číslo = jedna firma."""
    _kapitan(user, slug)
    tel = telefon(tel)
    if not tel:
        raise ValueError("To není telefonní číslo (předvolba a číslice).")
    with _locked():
        data = _load()
        for jina, entry in data["cisla"].items():
            if jina != slug and entry.get("telefon") == tel:
                raise ValueError("Tohle číslo už patří jiné firmě.")
        data["cisla"][slug] = {**data["cisla"].get(slug, {}), "telefon": tel,
                               "sparoval": int(user["id"]), "cas": int(time.time())}
        data["cisla"][slug].setdefault("odejit_z_cizich", True)
        data["cisla"][slug].setdefault("skupiny_jen_firma", False)
        _save(data)
    zaznam("sparovano", slug, user.get("email", ""))
    return data["cisla"][slug]


def odpojit_cislo(user, slug):
    """Odpojení: číslo, povolené skupiny a blokace firmy pryč. Propojení lidí zůstává."""
    _kapitan(user, slug)
    with _locked():
        data = _load()
        data["cisla"].pop(slug, None)
        data["skupiny"].pop(slug, None)
        data["blokace"].pop(slug, None)
        _save(data)
    zaznam("odpojeno", slug, user.get("email", ""))


def nastaveni(user, slug, **zmeny):
    """Volby firmy: odejit_z_cizich, skupiny_jen_firma, limit_hodina, limit_den."""
    _kapitan(user, slug)
    povolene = {"odejit_z_cizich": bool, "skupiny_jen_firma": bool,
                "limit_hodina": int, "limit_den": int}
    with _locked():
        data = _load()
        entry = data["cisla"].get(slug)
        if not entry:
            raise ValueError("Firma nemá spárované číslo.")
        for klic, hodnota in zmeny.items():
            if klic not in povolene:
                raise ValueError(f"Neznámá volba {klic}.")
            hodnota = povolene[klic](hodnota)
            if povolene[klic] is int and not 1 <= hodnota <= 100000:
                raise ValueError("Limit musí být kladné číslo.")
            entry[klic] = hodnota
        _save(data)
    return entry


# ── číslo člověka a jeho ověření ─────────────────────────────────────────────
def _hash_kodu(kod, sul):
    return hashlib.sha256((sul + str(kod)).encode()).hexdigest()


def zacit_overeni(user, tel):
    """Vygeneruje kód, který člověk pošle botovi ze svého čísla. Vrací kód
    (jen v odpovědi hubu; uložený je jen otisk)."""
    tel = telefon(tel)
    if not tel:
        raise ValueError("To není telefonní číslo (předvolba a číslice).")
    with _locked():
        data = _load()
        for uid, entry in data["propojeni"].items():
            if entry.get("telefon") == tel and entry.get("overeno") and uid != str(user["id"]):
                raise ValueError("Tohle číslo už je spojené s jiným účtem.")
        kod = f"{secrets.randbelow(10**6):06d}"
        sul = secrets.token_hex(8)
        data["propojeni"][str(int(user["id"]))] = {
            "telefon": tel, "overeno": 0, "sul": sul, "kod": _hash_kodu(kod, sul),
            "plati_do": int(time.time()) + KOD_PLATI}
        _save(data)
    _POKUSY.pop(int(user["id"]), None)
    return kod


def over_kod(slug, tel, kod):
    """Bot dostal ze soukromého čísla kód. Vrací uid, když seděl a člověk je
    členem firmy bota; jinak None (bez rozlišení proč — nic se nesmí prozradit)."""
    tel = telefon(tel)
    kod = re.sub(r"\D", "", str(kod or ""))
    if not tel or len(kod) != 6:
        return None
    with _locked():
        data = _load()
        for uid, entry in data["propojeni"].items():
            if entry.get("telefon") != tel or entry.get("overeno"):
                continue
            if time.time() > entry.get("plati_do", 0):
                return None
            if _POKUSY.get(int(uid), 0) >= KOD_POKUSY:
                return None
            if not hmac.compare_digest(_hash_kodu(kod, entry.get("sul", "")), entry.get("kod", "")):
                _POKUSY[int(uid)] = _POKUSY.get(int(uid), 0) + 1
                return None
            user = _ucet(uid)
            if not user or not firmy.role(user, slug):
                return None
            entry.update({"overeno": int(time.time()), "kod": "", "sul": "", "plati_do": 0})
            _save(data)
            zaznam("overeno", slug, user.get("email", ""))
            return int(uid)
    return None


def zrusit_propojeni(user, uid=None):
    """Člověk si ruší své číslo, nebo generál/admin cizí."""
    uid = int(uid or user["id"])
    if uid != int(user["id"]) and user.get("role") != "admin":
        if not any(firmy.role(user, s) == "kapitan" and firmy.role(_ucet(uid), s)
                   for s in firmy.slugs()):
            raise PermissionError("Cizí propojení smí zrušit jen generál jeho firmy.")
    with _locked():
        data = _load()
        data["propojeni"].pop(str(uid), None)
        _save(data)
    zaznam("propojeni-zruseno", "", user.get("email", ""), komu=uid)


def ucet_podle_cisla(tel, slug):
    """Účet s ověřeným číslem, který je členem firmy `slug`; jinak None."""
    tel = telefon(tel)
    if not tel:
        return None
    for uid, entry in _load()["propojeni"].items():
        if entry.get("telefon") == tel and entry.get("overeno"):
            user = _ucet(uid)
            if user and firmy.role(user, slug):
                return user
    return None


# ── blokace a skupiny ────────────────────────────────────────────────────────
def zablokovat(user, slug, hodnota, typ="cislo"):
    _kapitan(user, slug)
    if typ == "cislo":
        hodnota = telefon(hodnota)
    elif not str(hodnota or "").endswith("@g.us"):
        hodnota = ""
    if not hodnota:
        raise ValueError("Co mám zablokovat? Číslo, nebo skupinu.")
    with _locked():
        data = _load()
        seznam = data["blokace"].setdefault(slug, {"cisla": [], "skupiny": []})[
            "cisla" if typ == "cislo" else "skupiny"]
        if hodnota not in seznam:
            seznam.append(hodnota)
        _save(data)
    zaznam("zablokovano", slug, user.get("email", ""), typ=typ)


def odblokovat(user, slug, hodnota, typ="cislo"):
    _kapitan(user, slug)
    hodnota = telefon(hodnota) if typ == "cislo" else str(hodnota or "")
    with _locked():
        data = _load()
        seznam = data["blokace"].get(slug, {}).get("cisla" if typ == "cislo" else "skupiny", [])
        if hodnota in seznam:
            seznam.remove(hodnota)
        _save(data)
    zaznam("odblokovano", slug, user.get("email", ""), typ=typ)


def zablokovano(slug, hodnota, typ="cislo"):
    seznam = _load()["blokace"].get(slug, {}).get("cisla" if typ == "cislo" else "skupiny", [])
    return (telefon(hodnota) if typ == "cislo" else hodnota) in seznam


def povolit_skupinu(slug, jid, tel_povolujiciho):
    """Skupinu povolí člen firmy (přidal bota, nebo napsal /povol). Vrací uid
    toho, kdo ji povolil, nebo None, když nebyl členem / ověřený."""
    if not str(jid or "").endswith("@g.us") or zablokovano(slug, jid, "skupina"):
        return None
    user = ucet_podle_cisla(tel_povolujiciho, slug)
    if not user:
        return None
    with _locked():
        data = _load()
        data["skupiny"].setdefault(slug, {})[jid] = {"povolil": int(user["id"]),
                                                      "cas": int(time.time())}
        _save(data)
    zaznam("skupina-povolena", slug, user.get("email", ""))
    return int(user["id"])


def zakazat_skupinu(slug, jid):
    with _locked():
        data = _load()
        data["skupiny"].get(slug, {}).pop(jid, None)
        _save(data)
    zaznam("skupina-vypnuta", slug)


def skupina_povolena(slug, jid):
    """Platí, jen když ji povolil někdo, kdo je dál členem firmy."""
    entry = _load()["skupiny"].get(slug, {}).get(jid)
    if not entry or zablokovano(slug, jid, "skupina"):
        return False
    user = _ucet(entry.get("povolil"))
    return bool(user and firmy.role(user, slug))


# ── limity ───────────────────────────────────────────────────────────────────
def smi_odpovedet(slug, klic):
    """Počítadlo zpráv: hodinový limit na člověka/skupinu a denní na číslo.
    Volá se jednou na zprávu, na kterou bot opravdu odpoví."""
    entry = cislo_firmy(slug) or {}
    hodina = int(entry.get("limit_hodina") or LIMIT_HODINA)
    den = int(entry.get("limit_den") or LIMIT_DEN)
    nyni = time.time()
    with _LOCK:
        moje = [t for t in _POUZITI.get((slug, klic), []) if nyni - t < 3600]
        vsechny = [t for t in _POUZITI.get((slug, "*"), []) if nyni - t < 86400]
        if len(moje) >= hodina or len(vsechny) >= den:
            _POUZITI[(slug, klic)], _POUZITI[(slug, "*")] = moje, vsechny
            return False
        _POUZITI[(slug, klic)] = moje + [nyni]
        _POUZITI[(slug, "*")] = vsechny + [nyni]
    return True


# ── hlavní rozhodnutí ────────────────────────────────────────────────────────
def rozhodni(slug, udalost):
    """Co udělat s příchozí zprávou. `udalost`:

        {"typ": "dm" | "skupina", "od": "<číslo odesílatele>", "skupina": "<jid>",
         "text": "...", "oznacen": bool, "odpoved_botovi": bool}

    Vrací {"akce": ...}:
        ticho    — neodpovídat, nic neukládat
        kontext  — zpráva ve skupině; jen si ji zapamatovat
        kod      — soukromá zpráva s kódem pro ověření (zpracuje over_kod)
        prikaz   — příkaz od oprávněného (`prikaz`, `argument`, `uid`)
        clovek   — odpovědět v prostoru člověka (`uid`)
        skupina  — odpovědět ve skupině bez nástrojů (`od_uid` když je pisatel člen)
        limit    — odpověď by byla, ale limit vyčerpán
    """
    if not cislo_firmy(slug):
        return {"akce": "ticho"}
    od = telefon(udalost.get("od"))
    text = str(udalost.get("text") or "").strip()
    if not od or zablokovano(slug, od):
        return {"akce": "ticho"}
    clen = ucet_podle_cisla(od, slug)
    prikaz = None
    if text.startswith("/"):
        casti = text.split(None, 1)
        prikaz = (casti[0][1:].lower(), casti[1].strip() if len(casti) > 1 else "")

    if udalost.get("typ") == "dm":
        if prikaz and prikaz[0] == "kod":
            return {"akce": "kod", "argument": prikaz[1]}
        if not clen:
            return {"akce": "ticho"}
        if prikaz:
            return {"akce": "prikaz", "prikaz": prikaz[0], "argument": prikaz[1],
                    "uid": int(clen["id"]), "kapitan": firmy.role(clen, slug) == "kapitan"}
        if not smi_odpovedet(slug, f"u{clen['id']}"):
            return {"akce": "limit"}
        return {"akce": "clovek", "uid": int(clen["id"])}

    if udalost.get("typ") != "skupina":
        return {"akce": "ticho"}
    jid = str(udalost.get("skupina") or "")
    if prikaz and prikaz[0] == "povol" and clen and not zablokovano(slug, jid, "skupina"):
        return {"akce": "prikaz", "prikaz": "povol", "argument": jid, "uid": int(clen["id"]),
                "kapitan": firmy.role(clen, slug) == "kapitan"}
    if not skupina_povolena(slug, jid):
        return {"akce": "ticho"}
    if prikaz and prikaz[0] == "zakaz" and clen:
        return {"akce": "prikaz", "prikaz": "zakaz", "argument": jid, "uid": int(clen["id"]),
                "kapitan": firmy.role(clen, slug) == "kapitan"}
    if not (udalost.get("oznacen") or udalost.get("odpoved_botovi")):
        return {"akce": "kontext"}
    if (cislo_firmy(slug) or {}).get("skupiny_jen_firma") and not clen:
        return {"akce": "kontext"}
    if not smi_odpovedet(slug, jid):
        return {"akce": "limit"}
    return {"akce": "skupina", "od_uid": int(clen["id"]) if clen else None}


def pridal_bota(slug, jid, tel_pridavatele):
    """Bota přidal někdo do skupiny. {"akce": "povolit" | "odejit" | "ticho"}."""
    entry = cislo_firmy(slug)
    if not entry:
        return {"akce": "ticho"}
    if povolit_skupinu(slug, jid, tel_pridavatele):
        return {"akce": "povolit"}
    # Neznámý nebo cizí přidavatel: nic se nepovolí. Odejít jen když je to
    # jisté (přidavatel známý a není členem), ne když WhatsApp nic neřekl.
    if telefon(tel_pridavatele) and entry.get("odejit_z_cizich", True):
        return {"akce": "odejit"}
    return {"akce": "ticho"}


# ── úklid ────────────────────────────────────────────────────────────────────
def forget_user(uid):
    """Smazaný účet: pryč jeho číslo i skupiny, které povolil."""
    if not os.path.exists(_registr()):
        return
    with _locked():
        data = _load()
        data["propojeni"].pop(str(uid), None)
        for skupiny in data["skupiny"].values():
            for jid in [j for j, e in skupiny.items() if e.get("povolil") == uid]:
                skupiny.pop(jid)
        _save(data)
