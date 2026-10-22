"""Našeptávač souborů pro `@` v bublině na psaní (composer.js).

Vrací soubory a složky z pracovní složky projektu tabu. Kořen určuje server
(session.path), od klienta chodí jen dotaz — žádná cesta, žádné `..`.

Seznam souborů: `git ls-files --cached --others --exclude-standard` (respektuje
.gitignore), jinak omezený os.scandir (hloubka, počet, čas). Obří složky
(node_modules, .git, vendor, venv, build, dist …) se nikdy neprocházejí.
Seznam se pár vteřin drží v paměti, ať rychlé psaní nehledá pořád dokola.
"""
import os
import shutil
import subprocess
import threading
import time

LIMIT = 30                  # kolik shod se vrací nejvýš
CAS_DOTAZU = 1.0            # strop na jedno hledání (vteřiny)
CAS_GITU = 1.5              # strop na git ls-files
CACHE_SEC = 5.0             # jak dlouho se drží seznam souborů
CACHE_KOREN = 8             # kolik kořenů najednou
MAX_POLOZEK = 60000         # strop počtu souborů v seznamu
MAX_HLOUBKA = 8             # hloubka záložního procházení
MAX_DOTAZ = 120             # delší dotaz se ořízne

# Složky, do kterých se nikdy nelezne (ani z gitu, ani při procházení).
VYNECHAT = frozenset({
    "node_modules", ".git", ".hg", ".svn", "vendor", "__pycache__", "venv", ".venv",
    "env", ".tox", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".cache", "build", "dist",
    "target", ".next", ".nuxt", ".gradle", ".idea", "bower_components", "site-packages",
    ".npm", ".cargo", ".rustup", ".m2", "Pods", ".terraform", "coverage",
})

_zamek = threading.Lock()
_cache = {}                 # kořen -> (čas, seznam)
_zamky_kore = {}            # kořen -> zámek stavby (jedna stavba naráz)


def _ok_jmeno(rel):
    """Cesta bez řídicích znaků, `..` a bez uvozovky ve jménu s mezerou
    (takovou Claude Code v `@"…"` zapsat neumí)."""
    if not rel or len(rel) > 400 or "\x7f" in rel:
        return False
    if any(ord(c) < 32 for c in rel):
        return False
    if rel.startswith("/"):
        return False
    cast = rel.rstrip("/").split("/")
    if any(c in ("", ".", "..") or c in VYNECHAT for c in cast):
        return False
    if '"' in rel and any(c.isspace() for c in rel):
        return False
    return True


def _git(koren, deadline):
    """Seznam z gitu, nebo None (není git, není repo, vypršel čas)."""
    git = shutil.which("git")
    if not git:
        return None
    env = dict(os.environ, GIT_OPTIONAL_LOCKS="0", GIT_TERMINAL_PROMPT="0")
    try:
        proc = subprocess.run(
            [git, "-C", koren, "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=max(0.2, min(CAS_GITU, deadline - time.monotonic())), env=env, check=False)
    except (subprocess.TimeoutExpired, OSError, ValueError):
        return None
    if proc.returncode != 0:
        return None
    out = []
    for raw in proc.stdout.split(b"\0"):
        if not raw:
            continue
        try:
            rel = raw.decode("utf-8")
        except UnicodeDecodeError:
            continue
        if _ok_jmeno(rel):
            out.append(rel)
            if len(out) >= MAX_POLOZEK:
                break
    return out


def _scandir(koren, deadline):
    """Záložní procházení bez gitu: do šířky, omezená hloubka/počet/čas,
    symlinky na složky se nenásledují."""
    out = []
    fronta = [("", 0)]
    while fronta and len(out) < MAX_POLOZEK and time.monotonic() < deadline:
        rel_dir, hloubka = fronta.pop(0)
        try:
            with os.scandir(os.path.join(koren, rel_dir) if rel_dir else koren) as it:
                for e in it:
                    if time.monotonic() >= deadline or len(out) >= MAX_POLOZEK:
                        break
                    jmeno = e.name
                    if jmeno in VYNECHAT or (jmeno.startswith(".") and jmeno not in (".github", ".claude")):
                        continue
                    rel = rel_dir + "/" + jmeno if rel_dir else jmeno
                    try:
                        if e.is_symlink():
                            # symlink na soubor v kořeni ještě projde, na složku ne
                            if e.is_dir(follow_symlinks=True):
                                continue
                        elif e.is_dir(follow_symlinks=False):
                            if hloubka + 1 < MAX_HLOUBKA:
                                fronta.append((rel, hloubka + 1))
                            continue
                    except OSError:
                        continue
                    if _ok_jmeno(rel):
                        out.append(rel)
        except OSError:
            continue
    return out


def _se_slozkami(soubory):
    """K souborům přidá nadřazené složky (jako `dir/`), ať jdou odkázat i ty."""
    slozky = set()
    for rel in soubory:
        i = rel.rfind("/")
        while i > 0:
            d = rel[:i]
            if d in slozky:
                break
            slozky.add(d)
            i = d.rfind("/")
    return sorted(d + "/" for d in slozky) + soubory


def seznam(koren, deadline):
    """Seznam souborů a složek kořene (z paměti, pokud je čerstvý)."""
    now = time.monotonic()
    with _zamek:
        hit = _cache.get(koren)
        if hit and now - hit[0] < CACHE_SEC:
            return hit[1]
        zamek = _zamky_kore.setdefault(koren, threading.Lock())
    # Jedna stavba naráz — ostatní počkají (nejvýš do konce svého času) a vezmou hotové.
    if not zamek.acquire(timeout=max(0.05, deadline - time.monotonic())):
        return []
    try:
        with _zamek:
            hit = _cache.get(koren)
            if hit and time.monotonic() - hit[0] < CACHE_SEC:
                return hit[1]
        polozky = _git(koren, deadline)
        if polozky is None:
            polozky = _scandir(koren, deadline)
        polozky = _se_slozkami(polozky)
        with _zamek:
            if len(_cache) >= CACHE_KOREN:
                _cache.pop(min(_cache, key=lambda k: _cache[k][0]), None)
            _cache[koren] = (time.monotonic(), polozky)
        return polozky
    finally:
        zamek.release()


def _skore(rel, q, qs):
    """Čím vyšší, tím lepší shoda; 0 = nepasuje. `q` malými písmeny, `qs` = části po mezerách."""
    low = rel.lower()
    base = low.rstrip("/").rsplit("/", 1)[-1]
    # každá část dotazu (oddělená mezerou) musí sedět
    total = 0
    for part in qs:
        if base.startswith(part):
            s = 1000
        elif part in base:
            s = 800
        elif part in low:
            s = 500
        else:
            # pořadí znaků (fuzzy): nejdřív v názvu, pak v cestě
            s = 0
            for text, bonus in ((base, 300), (low, 150)):
                i = -1
                ok = True
                for ch in part:
                    i = text.find(ch, i + 1)
                    if i < 0:
                        ok = False
                        break
                if ok:
                    s = bonus
                    break
            if not s:
                return 0
        total += s
    # kratší cesta a menší hloubka dopředu, složky těsně před soubory
    return total - len(rel) * 0.5 - low.count("/") * 5


def hledej(koren, dotaz, limit=LIMIT, cas=CAS_DOTAZU):
    """{"items": [{"path", "dir"}], "cut": bool}. `koren` už musí být ověřený adresář."""
    q = (dotaz if isinstance(dotaz, str) else "")[:MAX_DOTAZ].replace("\x00", "").strip().lower()
    if q.startswith("./"):
        q = q[2:]
    # Stavba seznamu a samotné hledání mají každá svůj čas, ať pomalá stavba
    # (velká složka bez gitu) neukousne celé hledání.
    polozky = seznam(koren, time.monotonic() + cas)
    deadline = time.monotonic() + cas * 0.6
    qs = q.split()
    if not qs:
        # bez dotazu: první úroveň (nejkratší cesty), složky nejdřív
        vyber = sorted((p for p in polozky if p.rstrip("/").count("/") == 0),
                       key=lambda p: (not p.endswith("/"), p.lower()))[:limit]
        cut = False
    else:
        shody = []
        cut = False
        for n, rel in enumerate(polozky):
            if n & 511 == 0 and time.monotonic() > deadline:
                cut = True
                break
            s = _skore(rel, q, qs)
            if s > 0:
                shody.append((-s, rel))
        shody.sort()
        vyber = [r for _, r in shody[:limit * 2]]
    items = []
    real = os.path.realpath(koren)
    for rel in vyber:
        # Poslední pojistka: symlink nesmí vést mimo kořen.
        full = os.path.join(koren, rel.rstrip("/"))
        try:
            if os.path.islink(full):
                rp = os.path.realpath(full)
                if rp != real and not rp.startswith(real + os.sep):
                    continue
        except OSError:
            continue
        items.append({"path": rel.rstrip("/") + ("/" if rel.endswith("/") else ""), "dir": rel.endswith("/")})
        if len(items) >= limit:
            break
    return {"items": items, "cut": cut}


def koren_tabu(path):
    """Ověřený kořen hledání z cesty tabu, nebo None (neexistuje, `/`, nečitelný)."""
    if not path or not isinstance(path, str):
        return None
    try:
        real = os.path.realpath(path)
        if not os.path.isdir(real) or real == os.path.abspath(os.sep) or "\x00" in real:
            return None
        if not os.access(real, os.R_OK | os.X_OK):
            return None
    except (OSError, ValueError):
        return None
    return real
