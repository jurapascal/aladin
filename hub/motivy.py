"""
Motivy — jak Aladin vypadá (Nastavení → Napojení a pluginy → Aladin Market → Motivy).

Motiv je složka s `motiv.json` a volitelně `motiv.css`:

    {"name": "Námořní", "version": "1.0.0", "description": "…", "author": "…",
     "nahled": ["#0b1a2b", "#d4a24c", "#e6edf3"],
     "barvy": {"dark": {"BG": "#0b1a2b", "AMBER": "#d4a24c", …},
               "light": {…}},
     "style": "motiv.css"}

- `barvy` přepisují paletu z hub/core.py (DARK / LIGHT) — tytéž klíče, takže
  se podle nich obarví okno i terminál. Co v motivu chybí, zůstane výchozí.
- `motiv.css` se načte za hub.css: fonty, zaoblení, šířky panelů i rozložení.
  Je to jen CSS, žádný skript — ale může stáhnout obrázek nebo font odjinud,
  proto se motivy berou jen z vlastního katalogu (hub/market.py) nebo z gitu.

Přibalené motivy leží v hub/motivy/<id>/, nainstalované v ~/.claude/hub-motivy/<id>/.
Zapnutý je jeden (`motiv` v hub-config); prázdný = výchozí vzhled Aladina.
"""
import json
import os
import re
import shutil
import subprocess

from . import core

ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
PRIBALENE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "motivy")
KLICE = set(core.DARK) - {"TERM_PALETTE"}
SOUBORY = {".css": "text/css; charset=utf-8", ".svg": "image/svg+xml", ".png": "image/png",
           ".jpg": "image/jpeg", ".webp": "image/webp", ".woff2": "font/woff2", ".woff": "font/woff"}


def slozka():
    return os.path.join(core.CLAUDE_DIR, "hub-motivy")


def _kde(mid):
    """Složka motivu: nainstalovaný má přednost před přibaleným téhož jména."""
    if not ID_RE.match(str(mid or "")):
        return ""
    for base in (slozka(), PRIBALENE):
        path = os.path.join(base, mid)
        if os.path.isfile(os.path.join(path, "motiv.json")):
            return path
    return ""


def _manifest(path):
    try:
        with open(os.path.join(path, "motiv.json"), encoding="utf-8-sig") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _barvy(raw):
    """Jen známé klíče palety a jen #rrggbb — nic, co by se dostalo do stylu jinak."""
    out = {}
    for rezim in ("dark", "light"):
        src = (raw or {}).get(rezim) if isinstance(raw, dict) else None
        if not isinstance(src, dict):
            continue
        pal = {k: v for k, v in src.items() if k in KLICE and isinstance(v, str) and HEX_RE.match(v)}
        term = src.get("TERM_PALETTE")
        if isinstance(term, list) and len(term) == 16 and all(isinstance(c, str) and HEX_RE.match(c) for c in term):
            pal["TERM_PALETTE"] = term
        if pal:
            out[rezim] = pal
    return out


def _styl(name):
    name = str(name or "")
    if not name or name.startswith(("/", "\\")) or ".." in name.replace("\\", "/").split("/"):
        return ""
    return name if name.lower().endswith(".css") else ""


def _polozka(mid, path, vlastni):
    m = _manifest(path)
    if m is None:
        return {"id": mid, "name": mid, "error": "Chybí nebo je rozbitý motiv.json.", "vlastni": vlastni}
    nahled = [c for c in (m.get("nahled") or []) if isinstance(c, str) and HEX_RE.match(c)][:5]
    return {"id": mid, "name": str(m.get("name") or mid)[:80],
            "version": str(m.get("version") or "")[:20],
            "description": str(m.get("description") or "")[:400],
            "author": str(m.get("author") or "")[:80],
            "nahled": nahled, "vlastni": vlastni}


# Kdo si motiv nikdy nevybral (v konfigu klíč `motiv` chybí), dostane tenhle.
# Výchozí vzhled Aladina zůstává na výběr — uložené `motiv: ""` ho zapne.
VYCHOZI_MOTIV = "claude"


def aktivni():
    mid = core.CONFIG.get("motiv") if "motiv" in core.CONFIG else VYCHOZI_MOTIV
    mid = str(mid or "")
    return mid if _kde(mid) else ""


def seznam():
    """Přibalené i nainstalované motivy; nainstalovaný přebije přibalený téhož id."""
    out = {}
    for base, vlastni in ((PRIBALENE, False), (slozka(), True)):
        try:
            names = sorted(os.listdir(base))
        except OSError:
            continue
        for mid in names:
            path = os.path.join(base, mid)
            if ID_RE.match(mid) and os.path.isdir(path):
                out[mid] = _polozka(mid, path, vlastni)
    akt = aktivni()
    for p in out.values():
        p["aktivni"] = p["id"] == akt
    return {"motivy": list(out.values()), "aktivni": akt, "dir": slozka()}


def _verze_souboru(mid, styl):
    """Nejnovější změna motiv.json nebo jeho CSS — adresa stylu se mění s ním,
    jinak prohlížeč drží starý motiv.css, i když se změnil jen on."""
    base = _kde(mid)
    stamps = []
    for name in ("motiv.json", styl):
        try:
            stamps.append(int(os.path.getmtime(os.path.join(base, name))))
        except (OSError, TypeError):
            pass
    return max(stamps) if stamps else 0


def data():
    """Zapnutý motiv pro okno: přepsaná paleta a adresa stylu (do stavu hubu)."""
    mid = aktivni()
    if not mid:
        return {"id": ""}
    m = _manifest(_kde(mid)) or {}
    return {"id": mid, "barvy": _barvy(m.get("barvy")), "style": _styl(m.get("style")),
            "v": _verze_souboru(mid, _styl(m.get("style")))}


def nastav(mid):
    mid = str(mid or "")
    if mid and not _kde(mid):
        return {"ok": False, "message": "Takový motiv tu není."}
    core.save_config({"motiv": mid})
    return {"ok": True, "motiv": data()}


def pridej(url):
    """Motiv z gitu (GitHub URL / owner/repo)."""
    url = str(url or "").strip()
    if re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", url):
        url = f"https://github.com/{url}.git"
    if not re.match(r"^https://[A-Za-z0-9.-]+/[^\s]+$", url):
        return {"ok": False, "message": "Zadej adresu repozitáře (https://… nebo owner/repo)."}
    git = shutil.which("git")
    if not git:
        return {"ok": False, "message": "Na tomhle stroji chybí git."}
    mid = re.sub(r"[^a-z0-9._-]+", "-", url.rstrip("/").rsplit("/", 1)[-1].lower().removesuffix(".git")).strip("-")[:60]
    if not ID_RE.match(mid or ""):
        return {"ok": False, "message": "Z adresy nejde poznat název motivu."}
    cil = os.path.join(slozka(), mid)
    if os.path.exists(cil):
        return {"ok": False, "message": f"Motiv „{mid}“ už tu je."}
    os.makedirs(slozka(), exist_ok=True)
    try:
        r = subprocess.run([git, "clone", "--depth", "1", "--", url, cil], capture_output=True, text=True,
                           timeout=180, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "message": f"Stažení se nepovedlo: {exc}"}
    if r.returncode != 0:
        shutil.rmtree(cil, ignore_errors=True)
        return {"ok": False, "message": "Stažení se nepovedlo: " + (r.stderr or "").strip()[-300:]}
    if _manifest(cil) is None:
        shutil.rmtree(cil, ignore_errors=True)
        return {"ok": False, "message": "V repozitáři není motiv.json — tohle není motiv."}
    core.log(f"motiv {mid}: přidán z {url}")
    return {"ok": True, "id": mid}


def odeber(mid):
    if not ID_RE.match(str(mid or "")):
        return {"ok": False, "message": "Takový motiv tu není."}
    cil = os.path.join(slozka(), mid)
    root = os.path.realpath(slozka())
    if not os.path.isdir(cil) or os.path.islink(cil) or os.path.dirname(os.path.realpath(cil)) != root:
        return {"ok": False, "message": "Přibalený motiv odebrat nejde."}
    if core.CONFIG.get("motiv") == mid:
        core.save_config({"motiv": ""})
    shutil.rmtree(cil)
    core.log(f"motiv {mid}: odebrán")
    return {"ok": True}


def soubor(mid, rel):
    """(cesta, content-type) souboru motivu — styl a to, co si z něj bere."""
    base = _kde(mid)
    rel = str(rel or "")
    if not base or not rel or ".." in rel.replace("\\", "/").split("/"):
        return None
    ctype = SOUBORY.get(os.path.splitext(rel)[1].lower())
    if not ctype:
        return None
    base = os.path.realpath(base)
    full = os.path.realpath(os.path.join(base, *rel.split("/")))
    if os.path.commonpath([base, full]) != base or not os.path.isfile(full):
        return None
    return full, ctype
