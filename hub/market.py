"""
Aladin Market — vlastní katalog rozšíření Aladina (Nastavení → Napojení
a pluginy → Aladin Market). Vedle Claude Code Marketplace (hub/pluginy.py),
který obsluhuje `claude plugin …`, tohle je obchod se vším, co patří appce:

- **Pluginy appky** a **motivy** z GitHub repa katalogu (`market_repo`
  v hub-config, výchozí jurapascal/aladin-marketplace). V kořeni repa leží
  `katalog.json`, každá položka má svou složku:

    {"verze": 1,
     "pluginy": [{"id", "name", "description", "author", "version", "path": "pluginy/<id>"}],
     "motivy":  [{"id", "name", "description", "author", "version",
                  "nahled": ["#rrggbb", …], "path": "motivy/<id>"}]}

  Instaluje se z tarballu větve main — jen složka položky, jen běžné soubory
  (žádné odkazy, zařízení ani cesty ven). Plugin je po instalaci vypnutý
  (stejně jako appka_pridej), motiv se sám nezapíná.

- **Skilly Aladina** — slash příkazy, které appka přibaluje ve `skills/`
  repa. Instalačka je dává jen při první instalaci; tady si je jde
  doinstalovat (nebo odebrat) jednotlivě. Šablona se vyrenderuje stejně
  jako v install.sh. Skilly patří uživateli — existující se nepřepisuje.
"""
import io
import json
import os
import re
import shutil
import sys
import tarfile
import tempfile
import threading
import time
import urllib.error
import urllib.request

from . import core, motivy, pluginy

ID_RE = pluginy.ID_RE
REPO_RE = re.compile(r"^[A-Za-z0-9-]{1,39}/[A-Za-z0-9._-]{1,100}$")
CAST_RE = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
VYCHOZI_REPO = "jurapascal/aladin-marketplace"
VETEV = "main"
KATALOG_TTL = 600
KATALOG_MAX = 2 * 1024 * 1024
TARBALL_MAX = 50 * 1024 * 1024
# Co se z tarballu smí rozbalit pro jednu položku.
POLOZKA_MAX = 20 * 1024 * 1024
POLOZKA_SOUBORU = 500
NEDOSTUPNY = "Katalog Aladin Marketu zatím není k dispozici."

DRUHY = {"pluginy": "plugin.json", "motivy": "motiv.json"}

_KATALOG = {"at": 0.0, "repo": "", "data": None}
_KATALOG_LOCK = threading.Lock()
_INSTALACE_LOCK = threading.Lock()


class _Chyba(Exception):
    """Chyba, jejíž text jde rovnou člověku."""


def repo():
    r = str(core.CONFIG.get("market_repo") or "").strip()
    return r if REPO_RE.match(r) else VYCHOZI_REPO


def _stahni(url, limit, timeout=10):
    """Bajty z adresy, nejvýš `limit` — co je větší, se odmítne celé."""
    req = urllib.request.Request(url, headers={"User-Agent": "aladin-marketplace"})
    with urllib.request.urlopen(req, timeout=timeout) as res:
        data = res.read(limit + 1)
    if len(data) > limit:
        raise _Chyba("Stažený soubor je nečekaně velký — nechávám ho být.")
    return data


def _stahni_tarball(r):
    """Celé repo katalogu (větev main) jako tar.gz."""
    return _stahni(f"https://codeload.github.com/{r}/tar.gz/refs/heads/{VETEV}", TARBALL_MAX, timeout=60)


def _platna_cesta(path):
    """`motivy/<id>` — relativní, bez `..`, jen obyčejné názvy složek."""
    path = str(path or "").strip().rstrip("/")
    casti = path.split("/") if path else []
    if not casti or path.startswith("/") or "\\" in path or any(c in (".", "..") or not CAST_RE.match(c) for c in casti):
        return ""
    return "/".join(casti)


def _text(v, n):
    return str(v or "")[:n] if isinstance(v, (str, int, float)) else ""


def _polozky(raw, druh):
    out, videne = [], set()
    for p in raw if isinstance(raw, list) else []:
        if not isinstance(p, dict):
            continue
        pid, path = str(p.get("id") or ""), _platna_cesta(p.get("path"))
        if not ID_RE.match(pid) or not path or pid in videne:
            continue
        videne.add(pid)
        item = {"id": pid, "name": _text(p.get("name"), 80) or pid,
                "description": _text(p.get("description"), 400),
                "author": _text(p.get("author"), 80), "version": _text(p.get("version"), 20),
                "path": path}
        if druh == "motivy":
            item["nahled"] = [c for c in (p.get("nahled") or []) if isinstance(c, str) and HEX_RE.match(c)][:5]
        out.append(item)
    return out


def _nainstalovano(druh, pid):
    base = pluginy.slozka() if druh == "pluginy" else motivy.slozka()
    return os.path.isdir(os.path.join(base, pid))


def katalog(obnovit=False):
    """Katalog z GitHubu (drží se 10 minut) a u každé položky, jestli už tu je."""
    r = repo()
    with _KATALOG_LOCK:
        hit = _KATALOG["data"] if _KATALOG["repo"] == r else None
        fresh = hit is not None and time.time() - _KATALOG["at"] < KATALOG_TTL
    if obnovit or not fresh:
        try:
            raw = _stahni(f"https://raw.githubusercontent.com/{r}/{VETEV}/katalog.json", KATALOG_MAX)
            data = json.loads(raw.decode("utf-8-sig"))
            if not isinstance(data, dict):
                raise ValueError("katalog.json není objekt")
            hit = {"pluginy": _polozky(data.get("pluginy"), "pluginy"),
                   "motivy": _polozky(data.get("motivy"), "motivy")}
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return {"ok": False, "repo": r, "pluginy": [], "motivy": [], "error": NEDOSTUPNY}
            return {"ok": False, "repo": r, "pluginy": [], "motivy": [],
                    "error": f"Katalog Aladin Marketu teď neodpovídá (HTTP {exc.code})."}
        except _Chyba as exc:
            return {"ok": False, "repo": r, "pluginy": [], "motivy": [], "error": str(exc)}
        except ValueError:
            return {"ok": False, "repo": r, "pluginy": [], "motivy": [],
                    "error": "Katalog Aladin Marketu je rozbitý (katalog.json není platný JSON)."}
        except Exception as exc:
            return {"ok": False, "repo": r, "pluginy": [], "motivy": [],
                    "error": f"Katalog Aladin Marketu teď neodpovídá: {exc}"}
        with _KATALOG_LOCK:
            _KATALOG.update(at=time.time(), repo=r, data=hit)
    return {"ok": True, "repo": r,
            **{druh: [{**p, "installed": _nainstalovano(druh, p["id"])} for p in hit[druh]]
               for druh in DRUHY}}


def _rozbal(data, path, cil):
    """Rozbalí z tarballu repa jen složku `path` do `cil` (už existující).

    Kořen tarballu z GitHubu je `<repo>-<větev>/`. Bezpečnost: žádná
    absolutní cesta ani `..` nikde v archivu; uvnitř složky položky jen
    běžné soubory a složky (žádné odkazy, hardlinky, zařízení, roury),
    nejvýš POLOZKA_SOUBORU souborů a POLOZKA_MAX bajtů."""
    try:
        tf = tarfile.open(fileobj=io.BytesIO(data), mode="r:gz")
    except (tarfile.TarError, OSError, EOFError):
        raise _Chyba("Balíček z katalogu nejde rozbalit.")
    with tf:
        try:
            members = tf.getmembers()
        except (tarfile.TarError, OSError, EOFError):
            raise _Chyba("Balíček z katalogu je poškozený.")
        koren = ""
        for m in members:
            name = m.name.replace("\\", "/")
            casti = [c for c in name.split("/") if c not in ("", ".")]
            if name.startswith("/") or re.match(r"^[A-Za-z]:", name) or ".." in casti:
                raise _Chyba("Balíček obsahuje cestu mimo svou složku — instalace zrušena.")
            if casti and not koren:
                koren = casti[0]
        if not koren:
            raise _Chyba("Balíček z katalogu je prázdný.")
        pref = [koren] + path.split("/")
        soubory, velikost, nalezeno = 0, 0, False
        for m in members:
            casti = [c for c in m.name.replace("\\", "/").split("/") if c not in ("", ".")]
            if casti[:len(pref)] != pref:
                continue
            nalezeno = True
            rel = casti[len(pref):]
            if not m.isdir() and not m.isreg():
                raise _Chyba("Balíček obsahuje odkaz nebo zvláštní soubor — instalace zrušena.")
            if not rel:
                continue                      # sama složka položky
            dest = os.path.join(cil, *rel)
            if m.isdir():
                os.makedirs(dest, exist_ok=True)
                continue
            soubory += 1
            velikost += max(0, m.size)
            if soubory > POLOZKA_SOUBORU or velikost > POLOZKA_MAX:
                raise _Chyba("Položka katalogu je moc velká — instalace zrušena.")
            src = tf.extractfile(m)
            if src is None:
                raise _Chyba("Balíček z katalogu je poškozený.")
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with src, open(dest, "wb") as fh:
                shutil.copyfileobj(src, fh)
        if not nalezeno:
            raise _Chyba("Položka v balíčku katalogu chybí.")


def instaluj(druh, pid):
    """Plugin appky nebo motiv z katalogu. {"ok", "id", "message"}."""
    if druh not in DRUHY:
        return {"ok": False, "message": "Neznámý druh položky."}
    pid = str(pid or "")
    if not ID_RE.match(pid):
        return {"ok": False, "message": "Taková položka v katalogu není."}
    kat = katalog()
    if not kat.get("ok"):
        return {"ok": False, "message": kat.get("error") or NEDOSTUPNY}
    polozka = next((p for p in kat[druh] if p["id"] == pid), None)
    if not polozka:
        return {"ok": False, "message": "Taková položka v katalogu není."}
    co = "Plugin" if druh == "pluginy" else "Motiv"
    base = pluginy.slozka() if druh == "pluginy" else motivy.slozka()
    cil = os.path.join(base, pid)
    with _INSTALACE_LOCK:
        if os.path.lexists(cil):
            return {"ok": False, "message": f"{co} „{polozka['name']}“ už je nainstalovaný."}
        os.makedirs(base, exist_ok=True)
        tmp = tempfile.mkdtemp(prefix=f".{pid}-", dir=base)
        try:
            try:
                data = _stahni_tarball(kat["repo"])
            except urllib.error.HTTPError as exc:
                raise _Chyba(NEDOSTUPNY if exc.code == 404 else f"Stažení se nepovedlo (HTTP {exc.code}).")
            except (urllib.error.URLError, OSError) as exc:
                raise _Chyba(f"Stažení se nepovedlo: {getattr(exc, 'reason', exc)}")
            _rozbal(data, polozka["path"], tmp)
            try:
                with open(os.path.join(tmp, DRUHY[druh]), encoding="utf-8-sig") as fh:
                    manifest = json.load(fh)
            except (OSError, ValueError):
                manifest = None
            if not isinstance(manifest, dict):
                raise _Chyba(f"V položce chybí {DRUHY[druh]} — tohle není {co.lower()} Aladina.")
            os.chmod(tmp, 0o755)              # mkdtemp zakládá 0700
            os.rename(tmp, cil)
        except _Chyba as exc:
            shutil.rmtree(tmp, ignore_errors=True)
            core.log(f"market: {druh}/{pid} se nenainstaloval: {exc}", "warn")
            return {"ok": False, "message": str(exc)}
        except Exception:
            shutil.rmtree(tmp, ignore_errors=True)
            raise
    if druh == "pluginy" and pid in pluginy.zapnute():
        # Zbytek po ručně smazané složce: nový plugin se sám zapnout nesmí.
        core.save_config({"hub_plugins_enabled": [p for p in pluginy.zapnute() if p != pid]})
    core.log(f"market: {druh}/{pid} nainstalován z {kat['repo']}")
    if druh == "pluginy":
        msg = "Plugin je nainstalovaný a vypnutý — zapni ho v pluginech appky, až budeš chtít."
    else:
        msg = "Motiv je nainstalovaný — zapneš ho v Motivech."
    return {"ok": True, "id": pid, "message": msg}


# ── Skilly Aladina ────────────────────────────────────────────────────────────
VAULT_ONLY = {"save", "learn", "project", "skill"}   # bez vaultu nedávají smysl
VYNECHAT = {"jev"}                                   # AI model — patří do napojení
SKILL_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


def _zdroj_skillu():
    """Složka se šablonami skillů: klon repa v ~/.claude/hub-src, záložně
    skills/ vedle hub/ (hub puštěný přímo z repa). Nainstalovaný hub leží
    v ~/.claude/hub — „vedle" je tam ~/.claude/skills, tedy cíl, ne zdroj."""
    cilova = os.path.realpath(core.SKILLS_DIR)
    for d in (os.path.join(core.SRC_DIR, "skills"),
              os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "skills")):
        if os.path.isdir(d) and os.path.realpath(d) != cilova:
            return d
    return ""


def _sablony():
    """{jméno: složka šablony} skillů Aladina."""
    zdroj = _zdroj_skillu()
    out = {}
    try:
        names = sorted(os.listdir(zdroj)) if zdroj else []
    except OSError:
        names = []
    for n in names:
        d = os.path.join(zdroj, n)
        if SKILL_RE.match(n) and n not in VYNECHAT and not os.path.islink(d) \
                and os.path.isfile(os.path.join(d, "SKILL.md")):
            out[n] = d
    return out


def skilly_paluby():
    out = []
    for name, d in _sablony().items():
        _, desc = pluginy._frontmatter(os.path.join(d, "SKILL.md"))
        vault = name in VAULT_ONLY
        out.append({"name": name, "description": desc,
                    "installed": os.path.isfile(os.path.join(core.SKILLS_DIR, name, "SKILL.md")),
                    "vault": vault, "available": not vault or core.HAS_BRAIN})
    res = {"skills": out}
    if not out:
        res["error"] = "Skilly Aladina tu nejsou — chybí zdroj appky (~/.claude/hub-src)."
    return res


def _vyrenderuj(text):
    """Šablona skillu → SKILL.md s cestami tohohle stroje (jako install.sh)."""
    state = os.path.join(core.MEMORY_DIR if core.HAS_BRAIN else core.CLAUDE_DIR, "session-state.md")
    hodnoty = {
        "{{MEMORY_DIR}}": core.MEMORY_DIR,
        "{{PROJECT_DIRS}}": ", ".join(core.PROJECT_DIRS),
        "{{SKILLS_DIR}}": os.path.join(core.BRAIN, "skills"),
        "{{CLAUDE_DIR}}": core.CLAUDE_DIR,
        "{{FTP_DEPLOY}}": os.path.join(core.CLAUDE_DIR, "ftp-deploy.sh"),
        "{{STATE_FILE}}": state,
        "{{PYTHON}}": sys.executable,
    }
    for k, v in hodnoty.items():
        text = text.replace(k, v)
    return text


def _zapomen_skilly():
    pluginy._SKILLY_KESH.pop("moje", None)


def skill_instaluj(name):
    name = str(name or "")
    sablona = _sablony().get(name)
    if not sablona:
        return {"ok": False, "message": "Takový skill Aladin nemá."}
    if name in VAULT_ONLY and not core.HAS_BRAIN:
        return {"ok": False, "message": f"/{name} potřebuje Obsidian vault s pamětí — nejdřív ho nastav."}
    cil = os.path.join(core.SKILLS_DIR, name)
    if os.path.lexists(cil):
        return {"ok": False, "message": f"/{name} už máš — nechávám ho, jak je."}
    try:
        with open(os.path.join(sablona, "SKILL.md"), encoding="utf-8") as fh:
            text = _vyrenderuj(fh.read())
    except (OSError, UnicodeDecodeError) as exc:
        return {"ok": False, "message": f"Šablona skillu nejde přečíst: {exc}"}
    os.makedirs(core.SKILLS_DIR, exist_ok=True)
    tmp = tempfile.mkdtemp(prefix=f".{name}-", dir=core.SKILLS_DIR)
    try:
        # Ostatní soubory skillu (reference, skripty) beze změny; odkazy ne.
        for root, dirs, files in os.walk(sablona):
            dirs[:] = [d for d in dirs if not d.startswith(".") and d != "__pycache__"
                       and not os.path.islink(os.path.join(root, d))]
            rel = os.path.relpath(root, sablona)
            for f in files:
                src = os.path.join(root, f)
                if (rel == "." and f == "SKILL.md") or f.startswith(".") or not os.path.isfile(src) \
                        or os.path.islink(src):
                    continue
                dest = os.path.join(tmp, rel, f)
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                shutil.copyfile(src, dest)
        with open(os.path.join(tmp, "SKILL.md"), "w", encoding="utf-8") as fh:
            fh.write(text)
        os.chmod(tmp, 0o755)
        os.rename(tmp, cil)
    except OSError as exc:
        shutil.rmtree(tmp, ignore_errors=True)
        return {"ok": False, "message": f"Skill se nepovedlo zapsat: {exc}"}
    _zapomen_skilly()
    core.log(f"skilly Aladina: /{name} nainstalován")
    return {"ok": True, "message": f"/{name} je nainstalovaný — funguje v nově otevřených chatech."}


def skill_odeber(name):
    name = str(name or "")
    if name not in _sablony():
        return {"ok": False, "message": "Odebrat tu jde jen skill Aladina."}
    cil = os.path.join(core.SKILLS_DIR, name)
    root = os.path.realpath(core.SKILLS_DIR)
    if os.path.islink(cil) or not os.path.isdir(cil) or os.path.dirname(os.path.realpath(cil)) != root:
        return {"ok": False, "message": f"/{name} tu není nainstalovaný."}
    shutil.rmtree(cil)
    _zapomen_skilly()
    core.log(f"skilly Aladina: /{name} odebrán")
    return {"ok": True, "message": f"/{name} je pryč."}
