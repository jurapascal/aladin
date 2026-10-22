"""
Vrácení změn — soubory zpátky do stavu, v jakém byly u některé zprávy.

Claude Code si před úpravou souboru ukládá jeho dřívější stav do
`~/.claude/file-history/<id konverzace>/<otisk>@v<N>` a do přepisu píše řádky
`file-history-snapshot`:

    {"type":"file-history-snapshot","messageId":"<uuid zprávy člověka>",
     "snapshot":{"trackedFileBackups":{"<cesta>":{"backupFileName":"<otisk>@v2",
                 "version":2,"backupTime":"…","realParentDir":"/abs/složka"}}}}

Snapshot ke zprávě M říká, jak vypadaly soubory, které Claude Code v tu chvíli
už hlídal (`backupFileName` null = soubor tehdy neexistoval). Soubor, který
začal hlídat až po M, ve snapshotu M chybí — jeho stav před první úpravou je
verze `@v1`; když `@v1` neexistuje a soubor se objevil jako `@v2`, vytvořil ho
Claude (Write) a před M nebyl. Otisk v názvu zálohy je sha256 absolutní cesty,
čehož se využívá jako pojistka: záloha, která nepatří k cestě, se nepoužije.

Dvě kola, nikdy nic naslepo:

    prehled   spočítá, co by se stalo (soubory, +/− řádků, diff) a nic nemění;
              vrátí `token` s otiskem aktuálního stavu všech souborů
    proved    s tokenem plán spočítá znovu; když se kterýkoli soubor mezitím
              změnil, nepřepíše nic. Před přepsáním uloží současný stav do
              `<config>/hub-rewind/<čas>/` (jde „vrátit vrácení“ — `zpet`).
              Smazání souboru, který před M neexistoval, chce navíc `smazat`.

Konverzace se nemění ani nezkracuje, jen soubory. Zapisuje se výhradně na
cesty, které eviduje snapshot (nebo úprava z přepisu, ke které má Claude Code
v file-history zálohu), nikdy do ~/.claude/projects ani file-history.
"""
import difflib
import hashlib
import itertools
import json
import os
import re
import secrets
import shutil
import tempfile
import threading
import time

from . import core

_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_ZALOHA = re.compile(r"[0-9a-f]{16}@v[0-9]{1,6}")
_ID_ZALOHY = re.compile(r"[0-9]{8}-[0-9]{6}-[0-9a-f]{8}")

MAX_SOUBOR = 100 * 1024 * 1024       # větší soubor se nevrací (ani nezálohuje)
MAX_DIFF_BAJTU = 400_000             # nad tím se diff nepočítá (difflib je kvadratický)
MAX_DIFF_RADKU = 8000
MAX_DIFF_ZNAKU = 30_000              # kolik diffu se pošle do stránky na soubor
MAX_SOUBORU = 500                    # kolik souborů přehled ukáže
MAX_RADEK = 1_000_000                # řádek přepisu delší než tohle se k úpravám nečte
PLAN_TTL = 600
MAX_PLANU = 20

_zamek = threading.Lock()            # vrací se vždy jen jedno
_plany = {}                          # token → {"cas", "chat", "uuid", "otisk"}
_plany_zamek = threading.Lock()


class Chyba(Exception):
    """Srozumitelná hláška pro člověka (česky)."""


# ── přepis ───────────────────────────────────────────────────────────────────
def oznac(entry, bloky, chat):
    """Zpráva člověka ve čtení dostane `uuid` (podle něj se hledá snapshot)
    a `chat` — bez toho by se u bubliny nedalo říct, kam se vracet."""
    try:
        if entry.get("type") != "user":
            return bloky
        uid = entry.get("uuid")
        if not (isinstance(uid, str) and _UUID.fullmatch(uid)):
            return bloky
        for b in bloky:
            if b.get("kind") == "me" and not b.get("mid") and not b.get("tichy"):
                b["uuid"] = uid
                b["chat"] = chat
                break
    except Exception:                      # označení nesmí rozbít čtení konverzace
        pass
    return bloky


def _prepis(chat):
    from . import cteni                    # cteni importuje tenhle modul
    if not cteni.je_id(chat):
        raise Chyba("Neplatné id konverzace.")
    path = cteni.path_for(chat)
    if not path:
        raise Chyba("Přepis konverzace se nenašel.")
    return path


def _projdi_prepis(path, uuid):
    """(snapshot zprávy `uuid`, snapshoty od ní dál, úpravy z přepisu od ní dál).
    Čte po řádcích, JSON se parsuje jen u snapshotů a úprav souborů — přepis
    může mít stovky MB, ale většinou jsou to obrázky v base64."""
    snapshoty = {}                         # messageId → trackedFileBackups (pořadí = první výskyt)
    od = False
    upravy = []
    with open(path, "rb") as fh:
        for radek in fh:
            if b'"file-history-snapshot"' in radek[:100]:
                try:
                    d = json.loads(radek)
                    mid = d["messageId"]
                    bk = d["snapshot"]["trackedFileBackups"]
                except (ValueError, KeyError, TypeError):
                    continue               # rozepsaný nebo cizí tvar
                if not (isinstance(mid, str) and isinstance(bk, dict)):
                    continue
                if mid == uuid:
                    od = True
                if od:
                    snapshoty[mid] = bk    # `isSnapshotUpdate` přepíše starší tvar
            elif (od and len(radek) < MAX_RADEK and b'"tool_use"' in radek
                  and (b'"file_path"' in radek or b'"notebook_path"' in radek)):
                try:
                    d = json.loads(radek)
                    cast = d["message"]["content"]
                except (ValueError, KeyError, TypeError):
                    continue
                if not isinstance(cast, list):
                    continue
                for c in cast:
                    if (isinstance(c, dict) and c.get("type") == "tool_use"
                            and c.get("name") in ("Edit", "Write", "MultiEdit", "NotebookEdit")
                            and isinstance(c.get("input"), dict)):
                        p = c["input"].get("file_path") or c["input"].get("notebook_path")
                        if isinstance(p, str):
                            upravy.append(p)
    if uuid not in snapshoty:
        raise Chyba("K téhle zprávě Claude Code žádný snapshot souborů neuložil — "
                    "vrátit se k ní nejde.")
    return snapshoty.pop(uuid), list(snapshoty.values()), upravy


# ── cesty ────────────────────────────────────────────────────────────────────
def _zakazano():
    """Složky, kam se nikdy nezapisuje: přepisy konverzací, záloha Claude Code
    a naše vlastní zálohy."""
    base = os.path.realpath(core.CLAUDE_DIR)
    return [os.path.join(base, n) for n in ("projects", "file-history", "hub-rewind")]


def _uvnitr(path, slozka):
    try:
        return os.path.commonpath([path, slozka]) == slozka
    except ValueError:
        return False


def _cesta(klic, b):
    """Absolutní cesta souboru ze snapshotu, nebo None, když vypadá podezřele."""
    if not isinstance(klic, str) or not klic or "\0" in klic or not isinstance(b, dict):
        return None
    rpd = b.get("realParentDir")
    if rpd is not None and (not isinstance(rpd, str) or "\0" in rpd):
        return None
    if os.path.isabs(klic):
        p = klic
        if rpd and os.path.normpath(rpd) != os.path.dirname(os.path.normpath(p)):
            return None
    else:
        if not rpd or not os.path.isabs(rpd):
            return None
        rel = os.path.dirname(os.path.normpath(klic))
        if rel not in ("", ".") and not os.path.normpath(rpd).endswith(os.sep + rel):
            return None
        p = os.path.join(rpd, os.path.basename(klic))
    if ".." in re.split(r"[\\/]", p) or os.path.normpath(p) != p:
        return None
    try:
        p.encode("utf-8")                  # osamocený surrogát by se nedal ani zapsat
    except UnicodeEncodeError:
        return None
    if os.path.basename(p) in ("", ".", ".."):
        return None
    real = os.path.join(os.path.realpath(os.path.dirname(p)), os.path.basename(p))
    if any(_uvnitr(real, z) for z in _zakazano()):
        return None
    return p


def _otisk_cesty(p):
    return hashlib.sha256(p.encode("utf-8")).hexdigest()[:16]


def _slozka_zaloh(chat):
    return os.path.join(core.CLAUDE_DIR, "file-history", chat)


# ── stav souborů ─────────────────────────────────────────────────────────────
def _sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for kus in iter(lambda: fh.read(1 << 20), b""):
            h.update(kus)
    return h.hexdigest()


def _stav(path):
    """('absent',) | ('file', sha, size) | ('error', důvod). Odkaz ani složku
    nepřepisujeme."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return ("absent",)
    except OSError as exc:
        return ("error", "soubor nejde přečíst (%s)" % exc.strerror)
    import stat as _s
    if _s.S_ISLNK(st.st_mode):
        return ("error", "je to symbolický odkaz")
    if not _s.S_ISREG(st.st_mode):
        return ("error", "není to obyčejný soubor")
    if st.st_size > MAX_SOUBOR:
        return ("error", "soubor je příliš velký")
    try:
        return ("file", _sha(path), st.st_size)
    except OSError as exc:
        return ("error", "soubor nejde přečíst (%s)" % exc.strerror)


def _rozdil(nyni, cil):
    """Diff „současný soubor → cílový“ (to, co vrácení udělá) a počet řádků."""
    out = {"plus": None, "minus": None, "diff": None}
    def cti(cesta):
        if cesta is None:
            return b""
        with open(cesta, "rb") as fh:
            return fh.read(MAX_DIFF_BAJTU + 1)
    try:
        a, b = cti(nyni), cti(cil)
    except OSError:
        return out
    if len(a) > MAX_DIFF_BAJTU or len(b) > MAX_DIFF_BAJTU:
        out["bez_diffu"] = "soubor je příliš velký na zobrazení rozdílů"
        return out
    if b"\0" in a[:8000] or b"\0" in b[:8000]:
        out["bez_diffu"] = "binární soubor"
        return out
    ra = a.decode("utf-8", "replace").splitlines()
    rb = b.decode("utf-8", "replace").splitlines()
    if len(ra) > MAX_DIFF_RADKU or len(rb) > MAX_DIFF_RADKU:
        out["bez_diffu"] = "soubor má příliš mnoho řádků na zobrazení rozdílů"
        return out
    plus = minus = 0
    kusy, znaku = [], 0
    for radek in itertools.islice(difflib.unified_diff(ra, rb, "teď", "po vrácení", n=2, lineterm=""), 2, None):
        if radek.startswith("+"):
            plus += 1
        elif radek.startswith("-"):
            minus += 1
        if znaku < MAX_DIFF_ZNAKU:
            kusy.append(radek)
            znaku += len(radek) + 1
    out.update(plus=plus, minus=minus, diff="\n".join(kusy)[:MAX_DIFF_ZNAKU])
    if znaku >= MAX_DIFF_ZNAKU:
        out["diff"] += "\n… (zbytek rozdílů se nevešel)"
    return out


# ── plán ─────────────────────────────────────────────────────────────────────
def _cil_z_m(p, b, slozka):
    """Cíl ze záznamu snapshotu zprávy: ('absent',) | ('file', cesta) | ('error', důvod)."""
    jmeno = b.get("backupFileName")
    if jmeno is None:
        return ("absent",)
    if not (isinstance(jmeno, str) and _ZALOHA.fullmatch(jmeno)):
        return ("error", "neplatný název zálohy")
    if jmeno.split("@")[0] != _otisk_cesty(p):
        return ("error", "záloha nepatří k téhle cestě")
    zal = os.path.join(slozka, jmeno)
    return ("file", zal) if os.path.isfile(zal) else ("error", "záloha v file-history chybí")


def _cil_pred_prvni_upravou(p, nejnizsi_verze, slozka):
    """Soubor, který Claude Code začal hlídat až po zprávě: stav před první úpravou."""
    h = _otisk_cesty(p)
    v1 = os.path.join(slozka, h + "@v1")
    if os.path.isfile(v1):
        return ("file", v1)
    if nejnizsi_verze == 2:
        return ("absent",)                   # první hlídaná verze je po vytvoření
    return ("error", "záloha původního stavu (v1) chybí")


def _sestav(chat, uuid):
    """Kompletní plán: seznam položek podle cest. Nic nemění."""
    if not _UUID.fullmatch(str(uuid or "")):
        raise Chyba("Neplatné id zprávy.")
    path = _prepis(chat)
    try:
        m, pozdeji, upravy = _projdi_prepis(path, uuid)
    except OSError as exc:
        raise Chyba("Přepis konverzace nejde přečíst (%s)." % exc.strerror)
    slozka = _slozka_zaloh(chat)

    polozky = {}                           # cesta → {"cil": …, "zdroj": …}
    preskoceno = []                        # (popis, důvod) — cesta, kterou nešlo ani vyhodnotit

    for klic, b in m.items():
        p = _cesta(klic, b)
        if p is None:
            preskoceno.append((str(klic)[:300], "cesta ve snapshotu nejde bezpečně ověřit (odkaz, ..)"))
            continue
        polozky[p] = {"cil": _cil_z_m(p, b, slozka), "zdroj": "snapshot"}

    z_m = set(polozky)
    nejnizsi = {}                          # cesta → nejnižší verze zálohy ve snapshotech po M
    nova = {}
    for bk in pozdeji:
        if not isinstance(bk, dict):
            continue
        for klic, b in bk.items():
            p = _cesta(klic, b)
            if p is None:
                preskoceno.append((str(klic)[:300], "cesta ve snapshotu nejde bezpečně ověřit (odkaz, ..)"))
                continue
            if p in z_m:
                continue                   # ten je ve snapshotu M
            ver = b.get("version")
            ver = ver if isinstance(ver, int) and not isinstance(ver, bool) else 0
            nejnizsi[p] = min(nejnizsi.get(p, ver), ver)
            nova[p] = True
    for p in nova:
        polozky[p] = {"cil": _cil_pred_prvni_upravou(p, nejnizsi.get(p, 0), slozka),
                      "zdroj": "snapshot"}

    for klic in upravy:                    # úpravy z posledního kola — snapshot teprve přijde
        if "\0" in klic or not os.path.isabs(klic):
            continue
        p = _cesta(klic, {})
        if p is None or p in polozky:
            continue
        v1 = os.path.join(slozka, _otisk_cesty(p) + "@v1")
        if os.path.isfile(v1):             # důkaz, že Claude Code soubor hlídá
            polozky[p] = {"cil": ("file", v1), "zdroj": "prepis"}

    out = []
    for p, pol in polozky.items():
        nyni = _stav(p)
        cil = pol["cil"]
        pol_out = {"path": p, "zdroj": pol["zdroj"], "nyni": nyni, "cil": cil}
        if nyni[0] == "error":
            pol_out.update(akce="preskocit", duvod=nyni[1])
        elif cil[0] == "error":
            pol_out.update(akce="preskocit", duvod=cil[1])
        elif cil[0] == "absent":
            if nyni[0] == "absent":
                pol_out.update(akce="beze_zmeny")
            else:
                pol_out.update(akce="smazat")
        else:
            cs = _stav(cil[1])
            if cs[0] != "file":
                pol_out.update(akce="preskocit", duvod="záloha nejde přečíst")
            else:
                pol_out["cil_sha"] = cs[1]
                if nyni[0] == "file" and nyni[1] == cs[1]:
                    pol_out.update(akce="beze_zmeny")
                else:
                    pol_out.update(akce="obnovit")
        out.append(pol_out)
    out.sort(key=lambda x: x["path"])
    return out, list(dict.fromkeys(preskoceno))


def _otisk_planu(chat, uuid, polozky):
    h = hashlib.sha256()
    h.update(json.dumps([chat, uuid, [[x["path"], x["akce"], x["nyni"][1] if x["nyni"][0] == "file" else x["nyni"][0],
                                       x.get("cil_sha") or x["cil"][0]] for x in polozky]],
                        ensure_ascii=False, sort_keys=True).encode("utf-8", "replace"))
    return h.hexdigest()


# ── přehled ──────────────────────────────────────────────────────────────────
def prehled(chat, uuid):
    polozky, preskoceno = _sestav(chat, uuid)
    soubory = []
    for x in polozky:
        if x["akce"] == "beze_zmeny":
            continue
        zaznam = {"path": x["path"], "akce": x["akce"], "zdroj": x["zdroj"]}
        if x.get("duvod"):
            zaznam["duvod"] = x["duvod"]
        if x["akce"] in ("obnovit", "smazat"):
            nyni = x["path"] if x["nyni"][0] == "file" else None
            cil = x["cil"][1] if x["cil"][0] == "file" else None
            zaznam.update(_rozdil(nyni, cil))
            zaznam["existuje"] = nyni is not None
        soubory.append(zaznam)
    for popis, duvod in preskoceno:
        soubory.append({"path": popis, "akce": "preskocit", "duvod": duvod})
    token = secrets.token_hex(16)
    with _plany_zamek:
        ted = time.time()
        for t in [t for t, v in _plany.items() if ted - v["cas"] > PLAN_TTL]:
            del _plany[t]
        while len(_plany) >= MAX_PLANU:
            del _plany[min(_plany, key=lambda t: _plany[t]["cas"])]
        _plany[token] = {"cas": ted, "chat": chat, "uuid": uuid,
                         "otisk": _otisk_planu(chat, uuid, polozky)}
    return {"ok": True, "token": token, "soubory": soubory[:MAX_SOUBORU],
            "celkem": len(soubory), "bez_zmeny": sum(1 for x in polozky if x["akce"] == "beze_zmeny"),
            "obnovit": sum(1 for x in polozky if x["akce"] == "obnovit"),
            "smazat": sum(1 for x in polozky if x["akce"] == "smazat"),
            "preskocit": sum(1 for x in polozky if x["akce"] == "preskocit") + len(preskoceno)}


# ── provedení ────────────────────────────────────────────────────────────────
def _zapis(cil_cesta, zdroj, ocekavana_sha):
    """Atomicky: dočasný soubor vedle cíle → kontrola → os.replace."""
    slozka = os.path.dirname(cil_cesta)
    os.makedirs(slozka, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=slozka, prefix=".rewind-")
    try:
        with os.fdopen(fd, "wb") as out, open(zdroj, "rb") as src:
            shutil.copyfileobj(src, out)
        if _sha(tmp) != ocekavana_sha:
            raise Chyba("záloha se mezitím změnila")
        try:
            shutil.copymode(cil_cesta, tmp)          # zachová práva stávajícího souboru
        except OSError:
            os.chmod(tmp, 0o644)
        os.replace(tmp, cil_cesta)
        tmp = None
    finally:
        if tmp:
            try:
                os.remove(tmp)
            except OSError:
                pass


def _uloz_manifest(slozka, manifest):
    tmp = os.path.join(slozka, "manifest.json.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, os.path.join(slozka, "manifest.json"))


def proved(chat, uuid, token, smazat=False):
    if not _zamek.acquire(blocking=False):
        raise Chyba("Vrácení právě běží, počkej, až doběhne.")
    try:
        with _plany_zamek:
            plan = _plany.pop(str(token or ""), None)       # token platí jednou
        if not plan or time.time() - plan["cas"] > PLAN_TTL:
            raise Chyba("Přehled vypršel. Otevři ho znovu a potvrď znovu.")
        if plan["chat"] != chat or plan["uuid"] != uuid:
            raise Chyba("Přehled patří k jiné zprávě. Otevři ho znovu.")
        polozky, _ = _sestav(chat, uuid)
        if _otisk_planu(chat, uuid, polozky) != plan["otisk"]:
            raise Chyba("Soubory se od přehledu změnily (nebo Claude zrovna pracuje). "
                        "Nic jsem nepřepsal — otevři přehled znovu.")
        delat = [x for x in polozky if x["akce"] == "obnovit" or (x["akce"] == "smazat" and smazat is True)]
        vynechano = [{"path": x["path"], "duvod": "smazání nebylo potvrzeno"}
                     for x in polozky if x["akce"] == "smazat" and smazat is not True]
        vynechano += [{"path": x["path"], "duvod": x.get("duvod", "")}
                      for x in polozky if x["akce"] == "preskocit"]
        if not delat:
            return {"ok": True, "vraceno": 0, "smazano": 0, "preskoceno": vynechano, "zaloha": ""}

        id_zalohy = time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(4)
        slozka = os.path.join(core.CLAUDE_DIR, "hub-rewind", id_zalohy)
        try:
            os.makedirs(os.path.join(slozka, "files"), mode=0o700)
            manifest = {"chat": chat, "uuid": uuid, "cas": int(time.time()), "zpet": False, "polozky": []}
            for i, x in enumerate(delat):
                pol = {"path": x["path"], "akce": x["akce"], "stav": "ceka", "soubor": "",
                       "pred": x["nyni"][1] if x["nyni"][0] == "file" else "absent",
                       "po": x["cil_sha"] if x["akce"] == "obnovit" else "absent"}
                if x["nyni"][0] == "file":           # současný stav se uloží předtím, než se cokoli změní
                    pol["soubor"] = str(i)
                    shutil.copy2(x["path"], os.path.join(slozka, "files", str(i)))
                    if _sha(os.path.join(slozka, "files", str(i))) != pol["pred"]:
                        raise Chyba("soubor %s se při zálohování změnil" % x["path"])
                manifest["polozky"].append(pol)
            _uloz_manifest(slozka, manifest)
        except Chyba:
            raise
        except OSError as exc:
            raise Chyba("Nepodařilo se uložit zálohu současného stavu (%s) — nic jsem nepřepsal." % exc.strerror)

        vraceno = smazano = 0
        for x, pol in zip(delat, manifest["polozky"]):
            try:
                ted = _stav(x["path"])
                if ted != x["nyni"]:                  # poslední pojistka těsně před zápisem
                    raise Chyba("soubor se těsně před zápisem změnil")
                if x["akce"] == "obnovit":
                    _zapis(x["path"], x["cil"][1], x["cil_sha"])
                    vraceno += 1
                else:
                    os.remove(x["path"])
                    smazano += 1
                pol["stav"] = "hotovo"
            except Chyba as exc:
                pol["stav"] = "preskoceno"
                vynechano.append({"path": x["path"], "duvod": str(exc)})
            except OSError as exc:
                pol["stav"] = "preskoceno"
                vynechano.append({"path": x["path"], "duvod": "nejde zapsat (%s)" % exc.strerror})
            try:
                _uloz_manifest(slozka, manifest)
            except OSError:
                pass
        return {"ok": True, "vraceno": vraceno, "smazano": smazano, "preskoceno": vynechano,
                "zaloha": id_zalohy}
    finally:
        _zamek.release()


def zpet(id_zalohy):
    """„Vrátit vrácení“: položky, které jsou dál přesně ve stavu po vrácení,
    se vrátí do stavu před ním. Co se mezitím změnilo, se nechá být."""
    if not _ID_ZALOHY.fullmatch(str(id_zalohy or "")):
        raise Chyba("Neplatné id zálohy.")
    if not _zamek.acquire(blocking=False):
        raise Chyba("Vrácení právě běží, počkej, až doběhne.")
    try:
        slozka = os.path.join(core.CLAUDE_DIR, "hub-rewind", id_zalohy)
        try:
            with open(os.path.join(slozka, "manifest.json"), encoding="utf-8") as fh:
                manifest = json.load(fh)
        except (OSError, ValueError):
            raise Chyba("Záloha se nenašla nebo je poškozená.")
        if not isinstance(manifest, dict) or not isinstance(manifest.get("polozky"), list):
            raise Chyba("Záloha je poškozená.")
        if manifest.get("zpet"):
            raise Chyba("Tohle vrácení už bylo vzato zpět.")
        zakazano = _zakazano()
        obnoveno = 0
        vynechano = []
        for pol in manifest["polozky"]:
            try:
                p = pol["path"]
                if (not isinstance(p, str) or not os.path.isabs(p) or os.path.normpath(p) != p
                        or any(_uvnitr(os.path.join(os.path.realpath(os.path.dirname(p)),
                                                    os.path.basename(p)), z) for z in zakazano)):
                    raise Chyba("podezřelá cesta v záloze")
                if pol.get("stav") != "hotovo":
                    continue                      # tohle vrácení nic nezměnilo
                ted = _stav(p)
                ocekavano = ("absent",) if pol["po"] == "absent" else ("file", pol["po"])
                if ted[0] == "error" or ted[:2] != ocekavano[:2]:
                    raise Chyba("soubor se po vrácení změnil, nechávám ho být")
                if pol["pred"] == "absent":
                    os.remove(p)
                else:
                    if not (isinstance(pol.get("soubor"), str) and pol["soubor"].isdigit()):
                        raise Chyba("záloha souboru chybí")
                    zal = os.path.join(slozka, "files", pol["soubor"])
                    _zapis(p, zal, pol["pred"])
                obnoveno += 1
            except Chyba as exc:
                vynechano.append({"path": str(pol.get("path", "?")) if isinstance(pol, dict) else "?",
                                  "duvod": str(exc)})
            except (OSError, KeyError, TypeError) as exc:
                vynechano.append({"path": str(pol.get("path", "?")) if isinstance(pol, dict) else "?",
                                  "duvod": "nejde zapsat (%s)" % (getattr(exc, "strerror", None) or exc)})
        manifest["zpet"] = True
        try:
            _uloz_manifest(slozka, manifest)
        except OSError:
            pass
        return {"ok": True, "obnoveno": obnoveno, "preskoceno": vynechano}
    finally:
        _zamek.release()


# ── API ──────────────────────────────────────────────────────────────────────
def api(name, payload):
    """Vstup ze server.py. Vrací dict; při chybě {"error": "česká věta"}."""
    try:
        if not isinstance(payload, dict):
            raise Chyba("Neplatný požadavek.")
        chat = str(payload.get("chat") or "")
        uuid = str(payload.get("uuid") or "")
        if name == "rewind-prehled":
            return prehled(chat, uuid)
        if name == "rewind-proved":
            return proved(chat, uuid, payload.get("token"), payload.get("smazat") is True)
        if name == "rewind-zpet":
            return zpet(payload.get("zaloha"))
        raise Chyba("Neznámá akce.")
    except Chyba as exc:
        return {"error": str(exc)}
    except Exception as exc:                  # nic z toho nesmí shodit hub
        core.log_error("rewind: %s selhalo" % name, exc)
        return {"error": "Vrácení se nepovedlo (%s: %s). Nic se nemělo změnit — zkontroluj hub.log." %
                         (type(exc).__name__, exc)}
