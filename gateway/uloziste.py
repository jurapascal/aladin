"""
Místo na disku po lidech — co komu kolik zabírá, limit na člověka, co jde
uvolnit, a noční úklid s oznámeními.

Disk brány je jeden pro všechny (weby, zálohy, prostory). Když se zaplní,
padá práce všem a záloha se neudělá — a dřív se nedalo říct, kdo kolik bere
a čím. Tady se to počítá:

* **Rozbor domova** (`skenuj_domov`): projde domov prostoru složku po složce
  (bez následování odkazů, bez cizích oddílů, velikost podle bloků jako `du`)
  a rozdělí místo do kategorií — Chaty Clauda, Mezipaměti, Projekty a složky,
  Obsidian, Stažené, Programy a nastavení, Ostatní. K tomu nálezy: velké
  mezipaměti a kdy se naposledy použily, node_modules a .venv v projektech,
  na které se dlouho nesáhlo, stejné soubory ve vlastním domově, staré
  archivy, snímky obrazovky, velké soubory a staré chaty. **Nic se nemaže** —
  nálezy jsou podklad pro člověka a jeho Clauda.
* **Limit** na člověka (`HUB_GW_KVOTA_GB`, výchozí 3 GB; admin mění v Nastavení
  → Úložiště nebo `claude-hub-admin uloziste <e-mail> --limit 5`). Limit nic
  nevypíná — od 80 % je člověk „blízko", nad ním „nad" a dostane oznámení.
* **Soubory sdílené mezi domovy** (po sloučení mezipamětí, gateway/uklid.py)
  se počítají poměrem: soubor, který mají tři lidé, započte každému třetinu.
  Součet přes lidi tak sedí na skutečné místo na disku.

Procházení 27 GB a statisíců souborů trvá desítky vteřin a bere procesor, proto
ho brána nikdy nedělá ve vlákně, které obsluhuje požadavky: pouští samostatný
proces (`python3 -m gateway.uloziste …`) s nejnižší prioritou (nice, ionice)
a výsledek si přečte ze `sken.json`. Běh hlídá zámek, takže noční úklid,
tlačítko „Přepočítat" i `claude-hub-admin` se nikdy nepotkají.

Noc (`noc`): sloučení stejných souborů v mezipaměti (uklid.py) → rozbor všech
domovů → oznámení lidem, kterým dochází místo nebo mají hodně co uvolnit
(rozbor.py napíše text i zadání pro jejich Clauda).
"""
import fcntl
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import threading
import time

from . import config, workspace

MB = 1024 * 1024
GB = 1024 * MB
DIR = os.environ.get("HUB_GW_ULOZISTE_DIR", os.path.join(config.GATEWAY_DIR, "uloziste"))
SKEN = os.path.join(DIR, "sken.json")
LIMITY = os.path.join(DIR, "limity.json")
STAV = os.path.join(DIR, "stav.json")
ZAMEK = os.path.join(DIR, "beh.lock")
# Velikost záloh zjišťuje hlídač zdraví (běží jako root, zálohy brána nepřečte).
VELIKOSTI_ROOT = "/var/lib/claude-hub/velikosti.json"

KVOTA = int(float(os.environ.get("HUB_GW_KVOTA_GB", "3")) * GB)
BLIZKO = 0.8
# Noční běh: od kolika hodin (po záloze v 1:30) a jak dlouho je okno. Kdo bránu
# zrovna v noci restartoval, dočká se další noci — přes den se nic těžkého nespouští.
NOC_HODINA = int(os.environ.get("HUB_GW_UKLID_HODINA", "3"))
NOC_OKNO = 3
STARE_DNI = 30               # projekt / mezipaměť bez použití tak dlouho = kandidát
MEZIPAMET_DNI = 14           # mezipaměť nepoužitá tak dlouho se počítá do „jde uvolnit"
OSOBNI_PRESKEN = 5 * 60      # člověk si svůj domov přepočítá nejdřív za 5 minut

KATEGORIE = (
    ("chaty", "Chaty Clauda"),
    ("mezipameti", "Mezipaměti"),
    ("projekty", "Projekty a složky"),
    ("obsidian", "Obsidian"),
    ("stazene", "Stažené"),
    ("programy", "Programy a nastavení"),
    ("ostatni", "Ostatní"),
)
NAZVY = dict(KATEGORIE)

# Cesty v domově, od kterých dolů platí jiná kategorie, než má rodič. Co tu
# není, zdědí kategorii po složce nad sebou; nejvyšší úroveň viz _kat_nahore.
_PRAVIDLA = {
    (".claude", "projects"): "chaty",
    (".claude", "hub-images"): "chaty",
    (".claude", "file-history"): "chaty",
    (".claude", "todos"): "chaty",
    (".claude", "shell-snapshots"): "chaty",
    (".cache",): "mezipameti",
    (".npm",): "mezipameti",
    (".pnpm-store",): "mezipameti",
    (".local", "share", "pnpm", "store"): "mezipameti",
    (".bun", "install", "cache"): "mezipameti",
    (".cargo", "registry"): "mezipameti",
    (".nuget", "packages"): "mezipameti",
    (".local", "share", "NuGet"): "mezipameti",
    (".gradle", "caches"): "mezipameti",
    (".m2", "repository"): "mezipameti",
    (".yarn", "berry", "cache"): "mezipameti",
    (".playwright-mcp",): "mezipameti",
}
# Mezipaměti, které se v nálezech hlásí po podsložkách (`.cache/uv`,
# `.cache/puppeteer`), ne jako jeden balík.
_PO_PODSLOZKACH = {(".cache",)}
STAZENE = {"Stažené", "Downloads", "Stažené soubory"}
REGEN = {"node_modules": "node_modules", ".venv": "venv", "venv": "venv"}
ARCHIVY = (".zip", ".tar", ".tgz", ".gz", ".bz2", ".xz", ".zst", ".7z", ".rar", ".iso", ".dmg")
_SNIMEK = re.compile(r"(?i)^(screenshot|screen[-_ ]?shot|sn[ií]mek|obrazovka|page-\d|scr[-_])"
                     r".*\.(png|jpe?g|webp)$")
_OBRAZEK = re.compile(r"(?i)\.(png|jpe?g|webp|gif)$")
DUP_MIN = 5 * MB             # menší stejné soubory nestojí za řeč
DUP_ROZPOCET = 3 * GB        # kolik se smí přečíst kvůli hledání stejných souborů v jednom domově
MAX_HLOUBKA = 400

_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_DIRFL = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | _CLOEXEC
_NOATIME = getattr(os, "O_NOATIME", 0)


# ── pomůcky ──────────────────────────────────────────────────────────────────
def _nacti(path):
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _ulozit(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False)
    os.replace(tmp, path)


def velikost(b):
    """Bajty česky: „840 MB", „1,2 GB"."""
    b = float(b or 0)
    if b >= GB:
        return f"{b / GB:.1f} GB".replace(".", ",")
    if b >= MB:
        return f"{b / MB:.0f} MB"
    return f"{max(1, round(b / 1024))} kB" if b else "0 MB"


def otevrit_cteni(dir_fd, name):
    """Soubor ke čtení relativně ke složce, bez odkazu a bez změny času
    přístupu (O_NOATIME) — jinak by noční čtení každou mezipaměť „použilo"
    a stará by nikdy nevypadala staře. O_NOATIME smí jen vlastník souboru;
    u cizího se otevře bez něj."""
    flags = os.O_RDONLY | os.O_NOFOLLOW | _CLOEXEC
    try:
        return os.open(name, flags | _NOATIME, dir_fd=dir_fd)
    except PermissionError:
        return os.open(name, flags, dir_fd=dir_fd)


def sha256_fd(fd):
    h = hashlib.sha256()
    with os.fdopen(fd, "rb", closefd=True) as fh:
        for kus in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(kus)
    return h.hexdigest()


def sha256_v_domove(home, rel):
    parts = rel.split("/")
    try:
        dfd = os.open(home, _DIRFL)
        try:
            for p in parts[:-1]:
                nfd = os.open(p, _DIRFL, dir_fd=dfd)
                os.close(dfd)
                dfd = nfd
            return sha256_fd(otevrit_cteni(dfd, parts[-1]))
        finally:
            os.close(dfd)
    except OSError:
        return ""


# ── limity ───────────────────────────────────────────────────────────────────
def limity():
    """{e-mail: bajty} — vlastní limity od admina. 0 = bez limitu."""
    out = {}
    for email, v in _nacti(LIMITY).items():
        try:
            out[str(email).lower()] = max(0, int(v))
        except (TypeError, ValueError):
            pass
    return out


def kvota_pro(email):
    return limity().get(str(email or "").lower(), KVOTA)


def set_limit(email, gb):
    """Limit v GB. Prázdné nebo „výchozí" = výchozí limit, 0 = bez limitu."""
    email = str(email or "").strip().lower()
    if not email:
        raise ValueError("Chybí e-mail.")
    raw = str(gb if gb is not None else "").strip().lower().replace(",", ".")
    data = _nacti(LIMITY)
    if raw in ("", "vychozi", "výchozí", "default"):
        data.pop(email, None)
        _ulozit(LIMITY, data)
        return KVOTA
    try:
        val = float(raw)
    except ValueError:
        raise ValueError("Limit musí být číslo v GB (0 = bez limitu).") from None
    if val < 0 or val > 10000:
        raise ValueError("Limit musí být mezi 0 a 10 000 GB.")
    data[email] = int(val * GB)
    _ulozit(LIMITY, data)
    return data[email]


def stav_kvoty(pouzito, kvota):
    if not kvota:
        return ""
    if pouzito >= kvota:
        return "nad"
    return "blizko" if pouzito >= BLIZKO * kvota else "ok"


# ── rozbor jednoho domova ────────────────────────────────────────────────────
def _kat_nahore(name, is_dir):
    if not is_dir:
        return "ostatni"
    if name == "Obsidian":
        return "obsidian"
    if name in STAZENE:
        return "stazene"
    if name.startswith("."):
        return "programy"
    return "projekty"


class _Domov:
    """Jeden průchod domovem. Složky se otevírají relativně k rodiči
    s O_NOFOLLOW (jako safefs) — domov patří prostoru a ten v něm může
    kdykoli prohodit složku za odkaz kamkoli na server."""

    def __init__(self, home, now=None):
        self.home = home
        self.now = now or time.time()
        self.kat = {k: 0 for k, _ in KATEGORIE}
        self.souboru = 0
        self.chyby = 0
        self.inody = {}              # ino -> [bajty, viděno, nlink, kategorie]
        self.slozky = {}             # nejvyšší úroveň: jméno -> záznam
        self.pod = {}                # nejvyšší úroveň: jméno -> {podsložka: bajty}
        self.mezipameti = []
        self.regen = []
        self.regen_cekaji = {}       # projekt (rodič node_modules) -> záznamy bez data změny
        self.archivy = []
        self.velke = []
        self.dup = []                # (st_size, bajty, rel, ino)
        self.obrazky = {}            # složka -> [počet, bajty, nejnovější]
        self.stare_chaty = [0, 0]
        self.dev = None

    def projdi(self):
        fd = os.open(self.home, _DIRFL)
        try:
            st = os.fstat(fd)
            self.dev = st.st_dev
            self._slozka(fd, (), "ostatni", False, 0)
        finally:
            os.close(fd)

    def _slozka(self, fd, parts, kat, v_regen, hloubka):
        """Vrací (bajty, nejnovější změna mimo node_modules/.venv, nejnovější použití)."""
        bajtu = 0
        zmena = 0.0
        pouziti = 0.0
        try:
            entries = list(os.scandir(fd))
        except OSError:
            self.chyby += 1
            return 0, 0.0, 0.0
        for e in entries:
            name = e.name
            try:
                st = e.stat(follow_symlinks=False)
            except OSError:
                continue
            mode = st.st_mode
            if stat.S_ISDIR(mode):
                if st.st_dev != self.dev:
                    continue                      # jiný oddíl (přivázaná složka)
                cparts = parts + (name,)
                ckat = _PRAVIDLA.get(cparts) or (_kat_nahore(name, True) if not parts else kat)
                regen = REGEN.get(name) if not v_regen and ckat not in ("mezipameti", "programy") else None
                b, m, a = st.st_blocks * 512, 0.0, 0.0
                if hloubka < MAX_HLOUBKA:
                    try:
                        cfd = os.open(name, _DIRFL, dir_fd=fd)
                    except OSError:
                        cfd = -1
                        self.chyby += 1
                    if cfd >= 0:
                        try:
                            sb, m, a = self._slozka(cfd, cparts, ckat, v_regen or bool(regen), hloubka + 1)
                            b += sb
                        finally:
                            os.close(cfd)
                else:
                    self.chyby += 1
                bajtu += b
                pouziti = max(pouziti, a)
                if regen:
                    # node_modules a .venv jdou znovu nainstalovat; aktivitu
                    # projektu podle nich nepoznáš (npm install je „změní").
                    zaz = {"druh": regen, "cesta": "/".join(cparts), "bajtu": b,
                           "projekt": "/".join(parts) or "."}
                    self.regen.append(zaz)
                    self.regen_cekaji.setdefault(parts, []).append(zaz)
                else:
                    zmena = max(zmena, m)
                self._zaznam_slozky(parts, cparts, ckat, kat, b, m, a)
            elif stat.S_ISREG(mode):
                velk = st.st_blocks * 512
                self.souboru += 1
                if st.st_nlink > 1:
                    zaz = self.inody.get(st.st_ino)
                    if zaz:
                        zaz[1] += 1
                        velk = 0                  # v tomhle domově už započtený
                    else:
                        self.inody[st.st_ino] = [velk, 1, st.st_nlink, kat]
                else:
                    self.kat[kat] += velk
                bajtu += velk
                zmena = max(zmena, st.st_mtime)
                pouziti = max(pouziti, st.st_atime, st.st_mtime)
                if velk and kat != "mezipameti" and not v_regen:
                    self._soubor(parts, name, st, velk, kat)
            # Odkazy, zařízení a roury místo nezabírají (odkaz pár bajtů).
        # Projekt s node_modules / .venv: jak dávno se v něm doopravdy pracovalo.
        for zaz in self.regen_cekaji.pop(parts, ()):
            zaz["zmeneno"] = zmena
        return bajtu, zmena, pouziti

    def _zaznam_slozky(self, parts, cparts, ckat, kat, b, m, a):
        if not parts:
            z = self.slozky.setdefault(cparts[0], {})
            z.update({"cesta": cparts[0], "bajtu": b, "kat": ckat, "zmeneno": m})
        elif len(parts) == 1:
            self.pod.setdefault(parts[0], {})[cparts[1]] = b
        # Mezipaměť: složka, kde kategorie přešla na mezipaměti (u `.cache`
        # až její podsložky). Hlásí se, když za něco stojí.
        jde = (ckat == "mezipameti" and kat != "mezipameti" and cparts not in _PO_PODSLOZKACH) \
            or (parts in _PO_PODSLOZKACH)
        if jde and b >= 50 * MB:
            self.mezipameti.append({"cesta": "/".join(cparts), "bajtu": b, "pouzito": a})

    def _soubor(self, parts, name, st, velk, kat):
        rel = "/".join(parts + (name,))
        low = name.lower()
        if velk >= 10 * MB and low.endswith(ARCHIVY):
            self.archivy.append({"cesta": rel, "bajtu": velk, "zmeneno": st.st_mtime})
        if velk >= 100 * MB:
            self.velke.append({"cesta": rel, "bajtu": velk, "zmeneno": st.st_mtime})
        # Stejné kopie jen v tom, co patří člověku — v programech a nastavení
        # bývají kopie schválně a mazat je nemá smysl radit.
        if st.st_size >= DUP_MIN and kat in ("projekty", "stazene", "obsidian", "ostatni") \
                and ".git" not in parts:
            self.dup.append((st.st_size, velk, rel, st.st_ino))
        if kat == "chaty":
            stari = self.now - st.st_mtime
            if parts[:2] == (".claude", "projects") and low.endswith(".jsonl") and stari > 90 * 86400:
                self.stare_chaty[0] += 1
                self.stare_chaty[1] += velk
            elif parts[:2] == (".claude", "hub-images") and stari > 60 * 86400:
                self._obrazek(".claude/hub-images", velk, st.st_mtime)
        elif _SNIMEK.match(name) or (_OBRAZEK.search(name) and parts and parts[-1].lower() in ("screenshots", "snimky")):
            self._obrazek("/".join(parts) or ".", velk, st.st_mtime)

    def _obrazek(self, slozka, velk, mtime):
        z = self.obrazky.setdefault(slozka, [0, 0, 0.0])
        z[0] += 1
        z[1] += velk
        z[2] = max(z[2], mtime)

    def _sdilene(self):
        """Soubory s víc odkazy: co má všechny odkazy doma, započte se celé; co
        sdílí s jinými domovy (sloučené mezipaměti), jen poměrem."""
        usetreno = 0
        for velk, videno, nlink, kat in self.inody.values():
            if videno >= nlink:
                self.kat[kat] += velk
            else:
                podil = velk * videno / nlink
                self.kat[kat] += int(podil)
                usetreno += int(velk - podil)
        self.inody = {}
        return usetreno

    def _duplicity(self):
        skupiny = {}
        for size, velk, rel, ino in self.dup:
            skupiny.setdefault(size, {}).setdefault(ino, (rel, velk))
        out = []
        rozpocet = DUP_ROZPOCET
        for size in sorted(skupiny, reverse=True):
            inody = skupiny[size]
            if len(inody) < 2:
                continue
            podle = {}
            for _ino, (rel, velk) in sorted(inody.items(), key=lambda kv: kv[1][0]):
                if rozpocet <= 0:
                    break
                rozpocet -= size
                h = sha256_v_domove(self.home, rel)
                if h:
                    podle.setdefault(h, []).append((rel, velk))
            for kopie in podle.values():
                if len(kopie) > 1:
                    velk = kopie[0][1]
                    out.append({"druh": "duplicity", "cesta": kopie[0][0],
                                "kopie": [r for r, _ in kopie][:6], "pocet": len(kopie),
                                "bajtu": velk * (len(kopie) - 1), "uvolnit": True})
        self.dup = []
        return out

    def vysledek(self):
        usetreno = self._sdilene()
        dni = lambda t: int((self.now - t) // 86400) if t else None   # noqa: E731
        nalezy = []
        for c in sorted(self.mezipameti, key=lambda c: -c["bajtu"]):
            d = dni(c["pouzito"])
            nalezy.append({"druh": "mezipamet", "cesta": c["cesta"], "bajtu": c["bajtu"], "dni": d,
                           "uvolnit": d is not None and d >= MEZIPAMET_DNI})
        for r in sorted(self.regen, key=lambda r: -r["bajtu"])[:15]:
            if r["bajtu"] < 20 * MB:
                continue
            d = dni(r.get("zmeneno"))
            nalezy.append({"druh": r["druh"], "cesta": r["cesta"], "bajtu": r["bajtu"], "dni": d,
                           "projekt": r["projekt"], "uvolnit": d is not None and d >= STARE_DNI})
        nalezy += sorted(self._duplicity(), key=lambda d: -d["bajtu"])[:10]
        for x in sorted(self.archivy, key=lambda x: -x["bajtu"])[:10]:
            nalezy.append({"druh": "archiv", "cesta": x["cesta"], "bajtu": x["bajtu"],
                           "dni": dni(x["zmeneno"]), "uvolnit": False})
        for slozka, (pocet, bajtu, nejnovejsi) in sorted(self.obrazky.items(), key=lambda kv: -kv[1][1])[:5]:
            if bajtu < 20 * MB and pocet < 50:
                continue
            d = dni(nejnovejsi)
            # Snímky z prohlížeče Clauda (.playwright-mcp) jsou jen mezivýsledky.
            nalezy.append({"druh": "obrazky", "cesta": slozka, "bajtu": bajtu, "pocet": pocet, "dni": d,
                           "uvolnit": slozka.startswith(".playwright-mcp") and (d or 0) >= MEZIPAMET_DNI})
        for x in sorted(self.velke, key=lambda x: -x["bajtu"])[:10]:
            if x["cesta"].lower().endswith(ARCHIVY):
                continue
            nalezy.append({"druh": "velky", "cesta": x["cesta"], "bajtu": x["bajtu"],
                           "dni": dni(x["zmeneno"]), "uvolnit": False})
        if self.stare_chaty[1] >= 50 * MB:
            nalezy.append({"druh": "chaty", "cesta": ".claude/projects", "bajtu": self.stare_chaty[1],
                           "pocet": self.stare_chaty[0], "dni": 90, "uvolnit": False})
        # .playwright-mcp je už v mezipaměti jako celek — snímky z něj by se počítaly dvakrát.
        if any(n["druh"] == "mezipamet" and n["cesta"] == ".playwright-mcp" for n in nalezy):
            nalezy = [n for n in nalezy if not (n["druh"] == "obrazky" and n["cesta"].startswith(".playwright-mcp"))]
        celkem = sum(self.kat.values())
        slozky = []
        for name, z in self.slozky.items():
            if "bajtu" not in z:
                continue
            pod = sorted(self.pod.get(name, {}).items(), key=lambda kv: -kv[1])[:6]
            slozky.append({"cesta": name, "bajtu": z["bajtu"], "kat": z["kat"],
                           "dni": dni(z.get("zmeneno")),
                           "pod": [{"cesta": f"{name}/{k}", "bajtu": v} for k, v in pod if v >= MB]})
        slozky.sort(key=lambda s: -s["bajtu"])
        return {"celkem": celkem, "usetreno": usetreno, "souboru": self.souboru, "chyby": self.chyby,
                "kategorie": dict(self.kat), "slozky": slozky[:15], "nalezy": nalezy,
                "lze_uvolnit": sum(n["bajtu"] for n in nalezy if n["uvolnit"]),
                "ke_zvazeni": sum(n["bajtu"] for n in nalezy if not n["uvolnit"])}


def skenuj_domov(home, now=None):
    d = _Domov(home, now)
    start = time.time()
    d.projdi()
    out = d.vysledek()
    out["cas"] = int(time.time())
    out["trvalo"] = round(time.time() - start, 1)
    return out


def _du(path):
    """Velikost složky mimo domovy (společné Obsidiany, odložené domovy)."""
    try:
        d = _Domov(path)
        d.projdi()
        d._sdilene()
        return sum(d.kat.values())
    except OSError:
        return 0


def skenuj(users, jen=None, log=print):
    """Rozbor domovů `users` (řádky účtů) do sken.json. `jen` = čísla účtů,
    jejichž domov se přepočítá; ostatní zůstanou z minula."""
    start = time.time()
    stare = _nacti(SKEN)
    lide = dict(stare.get("lide") or {}) if jen else {}
    for u in users:
        if jen and u["id"] not in jen:
            continue
        home = workspace.home_for(u)
        if not os.path.isdir(home):
            continue
        try:
            rec = skenuj_domov(home)
        except OSError as exc:
            log(f"úložiště: {u['email']}: {exc}")
            continue
        rec.update({"uid": u["id"], "email": u["email"], "jmeno": u.get("name") or "",
                    "domov": os.path.basename(home)})
        lide[str(u["id"])] = rec
        log(f"úložiště: {u['email']} {velikost(rec['celkem'])} za {rec['trvalo']} s")
    platne = {str(u["id"]) for u in users}
    lide = {k: v for k, v in lide.items() if k in platne}
    server = dict(stare.get("server") or {})
    if not jen or not server:
        server["spolecne"] = sum(_du(p) for p in (config.COMPANY_DIR, config.FIRMY_DIR, config.SHARED_DIR)
                                 if os.path.isdir(p))
        try:
            server["odlozene"] = sum(_du(e.path) for e in os.scandir(workspace.USERS_ROOT)
                                     if e.name.startswith("_smazany-") and e.is_dir(follow_symlinks=False))
        except OSError:
            server["odlozene"] = 0
    server.update(_disk(lide, server))
    out = {"cas": int(time.time()), "trvalo": round(time.time() - start, 1),
           "lide": lide, "server": server}
    _ulozit(SKEN, out)
    return out


def _disk(lide, server):
    try:
        u = shutil.disk_usage(workspace.USERS_ROOT)
    except OSError:
        return {}
    zalohy = _nacti(VELIKOSTI_ROOT).get("zalohy")
    lidi = sum(int(r.get("celkem") or 0) for r in lide.values())
    znamo = lidi + int(server.get("spolecne") or 0) + int(server.get("odlozene") or 0) + int(zalohy or 0)
    return {"celkem": u.total, "pouzito": u.used, "volno": u.free, "lide": lidi,
            "zalohy": zalohy, "system": max(0, u.used - znamo)}


# ── pro stránku (Nastavení → Úložiště) ───────────────────────────────────────
def _kategorie_list(kat):
    return [{"id": k, "nazev": n, "bajtu": int((kat or {}).get(k) or 0)} for k, n in KATEGORIE]


def _clovek(u, rec, plne=True):
    kvota = kvota_pro(u["email"])
    pouzito = int((rec or {}).get("celkem") or 0)
    out = {"id": u["id"], "email": u["email"], "jmeno": u.get("name") or "",
           "kvota": kvota, "pouzito": pouzito, "stav": stav_kvoty(pouzito, kvota) if rec else "",
           "procent": round(pouzito / kvota * 100) if kvota and rec else None,
           "zmereno": bool(rec), "cas": (rec or {}).get("cas"),
           "usetreno": int((rec or {}).get("usetreno") or 0),
           "lze_uvolnit": int((rec or {}).get("lze_uvolnit") or 0),
           "ke_zvazeni": int((rec or {}).get("ke_zvazeni") or 0),
           "kategorie": _kategorie_list((rec or {}).get("kategorie")),
           "slozky": [{k: s[k] for k in ("cesta", "bajtu", "kat", "dni")} for s in (rec or {}).get("slozky", [])][:10]}
    if plne:
        from . import rozbor
        out["slozky"] = (rec or {}).get("slozky", [])
        out["nalezy"] = [dict(n, popis=rozbor.popis(n)) for n in (rec or {}).get("nalezy", [])]
    else:
        out["nalezu"] = len((rec or {}).get("nalezy", []))
    return out


def prehled(user, users, admin=False):
    """Data pro Nastavení → Úložiště. Každý vidí sebe i s nálezy; admin navíc
    celý server a lidi (kategorie a největší složky, bez seznamu souborů)."""
    from . import oznameni, rozbor
    data = _nacti(SKEN)
    lide = data.get("lide") or {}
    rec = lide.get(str(user["id"]))
    ja = _clovek(user, rec)
    if rec:
        ja["prompt"] = rozbor.prompt_sablona(ja)
    out = {"ja": ja, "kategorie": [list(k) for k in KATEGORIE], "cas": data.get("cas"),
           "bezi": bezi(), "admin": bool(admin), "kvota_vychozi": KVOTA,
           "blizko": BLIZKO}
    if admin:
        out["server"] = data.get("server") or {}
        rows = []
        for u in users:
            if u.get("disabled"):
                continue
            r = _clovek(u, lide.get(str(u["id"])), plne=False)
            last = oznameni.posledni(u["id"], "uloziste")
            if last:
                r["oznameni"] = {"created": last["created"], "stav": oznameni.stav(last)}
            rows.append(r)
        rows.sort(key=lambda r: -r["pouzito"])
        out["lide"] = rows
        st = _nacti(STAV)
        out["uklid"] = st.get("uklid") or None
        out["noc"] = st.get("noc") or None
        out["oznameni_beh"] = st.get("oznameni") or None
    return out


def posledni_sken():
    return _nacti(SKEN)


def pouzito_vsech():
    """{číslo účtu: bajty} z posledního rozboru — záznam oznámení u nich
    ukazuje, kolik člověk zabírá teď proti době oznámení."""
    out = {}
    for k, r in (_nacti(SKEN).get("lide") or {}).items():
        try:
            out[int(k)] = int(r.get("celkem") or 0)
        except (TypeError, ValueError):
            pass
    return out


# ── běh na pozadí ────────────────────────────────────────────────────────────
class Zamek:
    """Jen jeden běh naráz (brána, noc, claude-hub-admin). `with Zamek(akce)
    as z:` — `z.mam` řekne, jestli se zámek podařilo vzít."""

    def __init__(self, akce):
        self.akce = akce
        self.fh = None
        self.mam = False

    def __enter__(self):
        os.makedirs(DIR, exist_ok=True)
        self.fh = open(ZAMEK, "a")
        try:
            fcntl.flock(self.fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.mam = True
            zapsat_stav(bezi={"akce": self.akce, "od": int(time.time()), "pid": os.getpid()})
        except OSError:
            self.mam = False
        return self

    def __exit__(self, *_exc):
        if self.mam:
            zapsat_stav(bezi=None)
            fcntl.flock(self.fh, fcntl.LOCK_UN)
        self.fh.close()


def zapsat_stav(**kv):
    st = _nacti(STAV)
    st.update(kv)
    try:
        _ulozit(STAV, st)
    except OSError:
        pass


_procesy = []                 # (Popen, akce, od) — co brána pustila
_spoustim = threading.Lock()


def bezi():
    """{"akce", "od"} běhu, který právě drží zámek, nebo None."""
    try:
        os.makedirs(DIR, exist_ok=True)
        with open(ZAMEK, "a") as fh:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                info = _nacti(STAV).get("bezi") or {"akce": "?"}
                return {"akce": info.get("akce"), "od": info.get("od")}
            fcntl.flock(fh, fcntl.LOCK_UN)
    except OSError:
        pass
    # Proces, který brána právě pustila, si zámek bere až po startu Pythonu —
    # do té doby by druhé kliknutí pustilo další.
    for p, akce, od in _procesy:
        if p.poll() is None:
            return {"akce": akce, "od": od}
    return None


def spustit(akce, uid=None):
    """Pustí `python3 -m gateway.uloziste <akce>` na pozadí s nejnižší
    prioritou. Vrací False, když už něco běží."""
    with _spoustim:
        return _spustit(akce, uid)


def _spustit(akce, uid):
    if bezi():
        return False
    argv = [sys.executable, "-m", "gateway.uloziste", akce]
    if uid is not None:
        argv += ["--uid", str(int(uid))]
    if shutil.which("ionice"):
        argv = ["ionice", "-c", "3"] + argv
    if shutil.which("nice"):
        argv = ["nice", "-n", "19"] + argv
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    os.makedirs(DIR, exist_ok=True)
    with open(os.path.join(DIR, "beh.log"), "a", encoding="utf-8") as log:
        log.write(time.strftime("%Y-%m-%d %H:%M:%S") + f" spouštím {akce}\n")
        log.flush()
        p = subprocess.Popen(argv, cwd=root, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                             start_new_session=True)
    # Dokončené procesy se sklidí (poll), ať po nich nezůstávají zombie.
    _procesy[:] = [x for x in _procesy if x[0].poll() is None] + [(p, akce, int(time.time()))]
    return True


def hlidac():
    """Vlákno brány: v noci (od NOC_HODINA) jednou spustí úklid, rozbor
    a oznámení; po startu brány bez rozboru spustí aspoň rozbor."""
    def smycka():
        time.sleep(180)
        while True:
            try:
                ted = time.localtime()
                dnes = time.strftime("%Y-%m-%d", ted)
                noc = (_nacti(STAV).get("noc") or {}).get("den")
                if NOC_HODINA <= ted.tm_hour < NOC_HODINA + NOC_OKNO and noc != dnes:
                    spustit("noc")
                elif not os.path.exists(SKEN):
                    spustit("sken")
                _procesy[:] = [x for x in _procesy if x[0].poll() is None]
            except Exception as exc:              # hlídač nesmí spadnout s bránou
                print("úložiště:", exc, flush=True)
            time.sleep(600)
    threading.Thread(target=smycka, daemon=True, name="uloziste").start()


# ── oznámení ─────────────────────────────────────────────────────────────────
_PORADI = {"": 0, "ok": 0, "blizko": 1, "nad": 2}
UPOZORNIT_LZE = int(os.environ.get("HUB_GW_UKLID_UPOZORNIT_MB", "1024")) * MB
OZNAMENI_DNI = float(os.environ.get("HUB_GW_OZNAMENI_DNI", "4"))


def proc_upozornit(c, last, now=None):
    """Má člověk `c` (_clovek) dostat oznámení o místě? Vrací důvod, nebo ''.

    Nespamovat: nejvýš jedno za OZNAMENI_DNI dní — dřív jen když se stav
    zhoršil (ok → blízko → nad). Nepřečtené předchozí se nepřipomíná."""
    now = now or time.time()
    potiz = c["stav"] in ("blizko", "nad") or c["lze_uvolnit"] >= UPOZORNIT_LZE
    if not c["zmereno"] or not potiz:
        return ""
    if not last:
        return "poprvé"
    minule = (last.get("meta") or {}).get("stav", "")
    if _PORADI.get(c["stav"], 0) > _PORADI.get(minule, 0):
        return "zhoršilo se"
    if now - last["created"] < OZNAMENI_DNI * 86400:
        return ""
    if not last.get("read_at") and not last.get("dismissed_at"):
        return ""
    return "připomenutí"


def upozornit(users, suchy=False, log=print):
    """Projde poslední rozbor a pošle oznámení, komu je potřeba. Vrací seznam
    {"email", "duvod", "claude", "title"}."""
    from . import oznameni, rozbor
    lide = _nacti(SKEN).get("lide") or {}
    vysledek = []
    for u in users:
        if u.get("disabled"):
            continue
        rec = lide.get(str(u["id"]))
        if not rec:
            continue
        c = _clovek(u, rec)
        last = oznameni.posledni(u["id"], "uloziste")
        duvod = proc_upozornit(c, last)
        if not duvod:
            continue
        if suchy:
            vysledek.append({"email": u["email"], "duvod": duvod, "claude": None,
                             "title": "", "stav": c["stav"], "lze": c["lze_uvolnit"]})
            log(f"oznámení (nanečisto): {u['email']} — {duvod}, {c['stav'] or 'bez limitu'}, "
                f"jde uvolnit {velikost(c['lze_uvolnit'])}")
            continue
        o = rozbor.napsat(c, u)
        meta = {"stav": c["stav"], "pouzito": c["pouzito"], "kvota": c["kvota"],
                "lze_uvolnit": c["lze_uvolnit"], "claude": o["claude"], "duvod": duvod}
        oznameni.pridat(u["id"], u["email"], o["title"], o["text"], kind="uloziste",
                        sender="Claudík" if o["claude"] else "Systém",
                        severity="vazne" if c["stav"] == "nad" else "varovani",
                        action={"label": "Vyřešit s Claudem", "prompt": o["prompt"]},
                        meta=meta, replace_kind=True)
        vysledek.append({"email": u["email"], "duvod": duvod, "claude": o["claude"],
                         "title": o["title"], "stav": c["stav"], "lze": c["lze_uvolnit"]})
        log(f"oznámení: {u['email']} — {duvod} ({'Claude' if o['claude'] else 'šablona'})")
    zapsat_stav(oznameni={"cas": int(time.time()), "poslano": 0 if suchy else len(vysledek),
                          "suchy": suchy, "lide": [v["email"] for v in vysledek]})
    return vysledek


def noc(users, log=print):
    """Noční běh: sloučit mezipaměti, přepočítat, upozornit."""
    from . import uklid
    start = time.time()
    # Den se zapíše hned: kdyby běh spadl, hlídač by ho jinak do rána pouštěl
    # každých deset minut znovu.
    zapsat_stav(noc={"den": time.strftime("%Y-%m-%d"), "zacatek": int(start)})
    try:
        u = uklid.sloucit(users, log=log)
        zapsat_stav(uklid=uklid.shrnuti(u))
    except Exception as exc:                      # úklid nesmí zastavit rozbor
        log(f"úklid: {exc}")
    # Velké staré soubory do cloudu dřív, než se počítá — ať rozbor i oznámení
    # vidí, co po přesunu zbylo (cloud.py, jen účty se zapnutou automatikou).
    try:
        from . import cloud
        cloud.automaticky(users, log=log)
    except Exception as exc:                      # cloud nesmí zastavit rozbor
        log(f"cloud: {exc}")
    skenuj(users, log=log)
    poslano = upozornit(users, log=log)
    zapsat_stav(noc={"den": time.strftime("%Y-%m-%d"), "cas": int(time.time()),
                     "trvalo": round(time.time() - start), "oznameni": len(poslano)})


def _users():
    from .accounts import Accounts
    return [dict(r) for r in Accounts(config.DB_PATH).list()]


def main(argv=None):
    """Spouští brána na pozadí: python3 -m gateway.uloziste sken|noc|uklid|rozbor [--uid N]."""
    import argparse
    p = argparse.ArgumentParser(prog="gateway.uloziste")
    p.add_argument("akce", choices=("sken", "noc", "uklid", "rozbor"))
    p.add_argument("--uid", type=int, default=None)
    a = p.parse_args(argv)
    sys.setrecursionlimit(max(sys.getrecursionlimit(), MAX_HLOUBKA * 3 + 200))
    log = lambda text: print(time.strftime("%H:%M:%S"), text, flush=True)   # noqa: E731
    with Zamek(a.akce) as z:
        if not z.mam:
            log("už běží jiný rozbor nebo úklid — končím")
            return 0
        users = _users()
        if a.akce == "sken":
            if a.uid is not None:
                rec = (_nacti(SKEN).get("lide") or {}).get(str(a.uid)) or {}
                if time.time() - float(rec.get("cas") or 0) < OSOBNI_PRESKEN:
                    log("domov se počítal před chvílí — nechávám")
                    return 0
            skenuj(users, jen={a.uid} if a.uid is not None else None, log=log)
        elif a.akce == "noc":
            noc(users, log=log)
        elif a.akce == "uklid":
            from . import uklid
            zapsat_stav(uklid=uklid.shrnuti(uklid.sloucit(users, log=log)))
            skenuj(users, log=log)
        elif a.akce == "rozbor":
            upozornit(users, log=log)
    return 0


if __name__ == "__main__":
    sys.exit(main())
