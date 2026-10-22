"""
Stavový řádek tabu — model, větev, odhad ceny a čas, spočítané v hubu.

Claude Code má vlastní statusline, ale ta závisí na uživatelově nastavení
a v čtení (cteni.js) není vidět. Hub má data po ruce sám: model a spotřebu
čte z přepisu konverzace, větev a změny z gitu.

Zásady (řádek je jen ozdoba, nesmí nikdy nic zdržet ani shodit):

* Spotřeba se čte **přírůstkově** — pamatuje se bajt, kam se minule došlo,
  a čte se jen nový konec. Jedno volání přečte nejvýš `BUDGET` bajtů, obří
  přepis se tak dopočítává po kouscích (`partial`), ne jedním dlouhým čtením.
* Claude Code zapisuje jednu odpověď do víc řádků (po jednom za blok) se
  stejným `message.id` a stejným `usage`. Kdyby se sčítaly všechny, vyšlo by
  to skoro dvojnásobné — počítá se každé id jednou (posledních pár set se drží).
* Git se pouští jen když složka opravdu je repo (hledá se `.git` bez gitu),
  s časovým limitem 2 s, bez zámku indexu (`GIT_OPTIONAL_LOCKS=0`) a na
  jednu složku nejvýš jednou za ~6 s. Po vypršení limitu (obří repo) se
  minutu nezkouší znovu. Větev se čte přímo z `.git/HEAD`, takže funguje
  i bez gitu v PATH.
* Cena jen tehdy, když všechny použité modely zná `pricing.py`; jinak jen tokeny.
"""
import collections
import json
import os
import subprocess
import threading
import time

from . import core, pricing

BUDGET = 24 * 1024 * 1024        # kolik bajtů přepisu se přečte za jedno volání
GIT_LIMIT = 2.0                  # s — jak dlouho se čeká na `git status`
GIT_TTL = 6.0                    # s — nejdřív po této době znovu spustit git
GIT_TTL_POMALY = 60.0            # s — po vypršení limitu (obří repo)
RECENT_IDS = 256                 # kolik posledních `message.id` se drží kvůli duplicitám
MAX_PREPISU = 200                # kolik přepisů má v paměti součty
MAX_REPO = 100                   # a kolik repozitářů má v paměti stav gitu
MAX_HLEDANI = 40                 # kolik složek nahoru se hledá `.git`

_ZAMEK = threading.Lock()
_SPOTREBA = {}                   # cesta přepisu → stav čtení (viz _novy_stav)
_GIT = {}                        # kořen repa → {"t", "ttl", "dirty", "zamek"}


def _cislo(v):
    """Počet tokenů z přepisu — cokoli jiného než nezáporné číslo je 0."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return 0
    return int(v) if v > 0 else 0


# ── spotřeba z přepisu ───────────────────────────────────────────────────────

def _novy_stav(ino):
    return {"pos": 0, "ino": ino, "hlava": b"", "midline": False, "model": "", "answers": 0,
            "models": {}, "recent": collections.OrderedDict(), "zamek": threading.Lock()}


def _uziti(usage):
    return (_cislo(usage.get("input_tokens")), _cislo(usage.get("output_tokens")),
            _cislo(usage.get("cache_creation_input_tokens")),
            _cislo(usage.get("cache_read_input_tokens")))


def _pricti(stav, model, uz, znamenko):
    m = stav["models"].setdefault(model, [0, 0, 0, 0])
    for i in range(4):
        m[i] += znamenko * uz[i]


def _zapocti(stav, entry):
    if entry.get("type") != "assistant":
        return
    zprava = entry.get("message")
    if not isinstance(zprava, dict) or not isinstance(zprava.get("usage"), dict):
        return
    model = zprava.get("model")
    model = model if isinstance(model, str) and model else "?"
    uz = _uziti(zprava["usage"])
    mid = zprava.get("id")
    recent = stav["recent"]
    if isinstance(mid, str) and mid:
        stary = recent.pop(mid, None)
        if stary:                       # stejná odpověď podruhé (další blok) — nahradí se
            _pricti(stav, stary[0], stary[1], -1)
            stav["answers"] -= 1
        recent[mid] = (model, uz)
        while len(recent) > RECENT_IDS:
            recent.popitem(last=False)
    _pricti(stav, model, uz, 1)
    stav["answers"] += 1
    # `<synthetic>` píše Claude Code u hlášek, které nepsal model.
    if model != "?" and not model.startswith("<"):
        stav["model"] = model


def _dopocti(stav, cesta, velikost):
    """Přečte kus přepisu za `pos`. Vrací True, když je přepis dočtený."""
    with open(cesta, "rb") as fh:
        fh.seek(stav["pos"])
        syrove = fh.read(BUDGET)
    if not syrove:
        return True
    konec = syrove.rfind(b"\n")
    plny = len(syrove) >= BUDGET
    if konec < 0:
        if not plny:
            return True                 # nedopsaný řádek — počká se na příště
        # Jediný řádek delší než rozpočet (obrázek v base64): přeskočí se celý.
        stav["pos"] += len(syrove)
        stav["midline"] = True
        return False
    kus = syrove[:konec + 1]
    if stav["midline"]:
        zacatek = kus.find(b"\n")
        kus = kus[zacatek + 1:] if zacatek >= 0 else b""
        stav["midline"] = False
    for radek in kus.split(b"\n"):
        # Předfiltr: většina řádků je příloha nebo výsledek nástroje.
        if b'"usage"' not in radek or b'"assistant"' not in radek:
            continue
        try:
            entry = json.loads(radek)
        except ValueError:
            continue                    # poškozený řádek se přeskočí
        if isinstance(entry, dict):
            try:
                _zapocti(stav, entry)
            except Exception:
                continue
    stav["pos"] += konec + 1
    return stav["pos"] >= velikost


def spotreba(cesta):
    """Součty z přepisu: (stav, dokonceno). Nikdy nevyhodí výjimku."""
    if not cesta or not isinstance(cesta, str) or not cesta.endswith(".jsonl"):
        return None, True
    try:
        st = os.stat(cesta)
    except (OSError, ValueError):
        return None, True
    try:
        with open(cesta, "rb") as fh:
            hlava = fh.read(512)
    except (OSError, ValueError):
        return None, True
    with _ZAMEK:
        stav = _SPOTREBA.get(cesta)
        # Soubor, který někdo přepsal (kratší, jiný inode, jiný začátek), se čte znovu od začátku.
        if (stav is None or st.st_size < stav["pos"] or stav["ino"] != st.st_ino
                or not hlava.startswith(stav["hlava"])):
            stav = _novy_stav(st.st_ino)       # nový nebo přepsaný soubor — od začátku
            if len(_SPOTREBA) >= MAX_PREPISU:
                _SPOTREBA.clear()
            _SPOTREBA[cesta] = stav
        stav["hlava"] = hlava
    # Dva taby nad jedním přepisem nečtou souběžně — druhý počká a najde hotovo.
    with stav["zamek"]:
        try:
            hotovo = True
            if st.st_size > stav["pos"]:
                hotovo = _dopocti(stav, cesta, st.st_size)
        except (OSError, ValueError):
            hotovo = True
    return stav, hotovo


# ── git ──────────────────────────────────────────────────────────────────────

def _najdi_repo(cwd):
    """(kořen repa, cesta k HEAD) nebo (None, None). Bez spouštění gitu."""
    try:
        aktualni = os.path.realpath(cwd)
    except (OSError, ValueError):
        return None, None
    for _ in range(MAX_HLEDANI):
        kandidat = os.path.join(aktualni, ".git")
        try:
            if os.path.isdir(kandidat):
                return aktualni, os.path.join(kandidat, "HEAD")
            if os.path.isfile(kandidat):        # worktree a submoduly: „gitdir: …"
                with open(kandidat, encoding="utf-8", errors="replace") as fh:
                    radek = fh.readline().strip()
                if radek.startswith("gitdir:"):
                    adresar = radek[7:].strip()
                    if not os.path.isabs(adresar):
                        adresar = os.path.join(aktualni, adresar)
                    return aktualni, os.path.join(adresar, "HEAD")
                return aktualni, None
        except (OSError, ValueError):
            return None, None
        rodic = os.path.dirname(aktualni)
        if rodic == aktualni:
            break
        aktualni = rodic
    return None, None


def _hlava(head):
    """(větev, odpojená, zkratka commitu) z .git/HEAD; (None, ...) když nejde přečíst."""
    if not head:
        return None, False, ""
    try:
        with open(head, encoding="utf-8", errors="replace") as fh:
            text = fh.readline().strip()
    except (OSError, ValueError):
        return None, False, ""
    if text.startswith("ref:"):
        ref = text[4:].strip()
        return (ref[len("refs/heads/"):] if ref.startswith("refs/heads/") else ref), False, ""
    return "", True, text[:7]


def _pocet_zmen(koren):
    """Počet změněných a nových souborů, None když git nejde (limit, chyba)."""
    if not core.GIT:
        return None
    env = dict(os.environ, GIT_OPTIONAL_LOCKS="0", GIT_TERMINAL_PROMPT="0", LC_ALL="C")
    try:
        r = subprocess.run([core.GIT, "status", "--porcelain=v1", "--untracked-files=normal"],
                           capture_output=True, cwd=koren, timeout=GIT_LIMIT, env=env,
                           stdin=subprocess.DEVNULL,
                           creationflags=getattr(core, "_NO_WINDOW", 0))
    except subprocess.TimeoutExpired:
        return "limit"
    except Exception:
        return None
    if r.returncode != 0:
        return None
    return sum(1 for radek in r.stdout.split(b"\n") if radek.strip())


def git_stav(cwd):
    """{"git": bool, "branch", "detached", "dirty"} — dirty je None, když se nezjistilo."""
    out = {"git": False, "branch": "", "detached": False, "dirty": None}
    if not cwd or not isinstance(cwd, str) or not os.path.isdir(cwd):
        return out
    koren, head = _najdi_repo(cwd)
    if not koren:
        return out
    out["git"] = True
    vetev, odpojena, zkratka = _hlava(head)
    if vetev is not None:
        out["branch"] = zkratka if odpojena else vetev
        out["detached"] = odpojena
    # Změny: z paměti, dokud je čerstvá; jinak jeden přístup, ostatní vidí starou hodnotu.
    ted = time.monotonic()
    with _ZAMEK:
        zaznam = _GIT.get(koren)
        if zaznam is None:
            if len(_GIT) >= MAX_REPO:
                _GIT.clear()
            zaznam = _GIT[koren] = {"t": 0.0, "ttl": GIT_TTL, "dirty": None,
                                    "zamek": threading.Lock()}
    if ted - zaznam["t"] >= zaznam["ttl"] and zaznam["zamek"].acquire(blocking=False):
        try:
            if time.monotonic() - zaznam["t"] >= zaznam["ttl"]:
                pocet = _pocet_zmen(koren)
                zaznam["ttl"] = GIT_TTL_POMALY if pocet == "limit" else GIT_TTL
                zaznam["dirty"] = None if pocet == "limit" else pocet
                zaznam["t"] = time.monotonic()
        finally:
            zaznam["zamek"].release()
    out["dirty"] = zaznam["dirty"]
    return out


# ── dohromady ────────────────────────────────────────────────────────────────

def zjisti_z(cwd, prepis, started):
    """Podklady pro řádek. `started` je čas startu tabu v sekundách."""
    ted = int(time.time() * 1000)
    out = {"ok": True, "now": ted, "started": int(started * 1000) if started else 0,
           "model": "", "label": "", "tokens": 0, "cost": None, "answers": 0,
           "partial": False, "usage": [0, 0, 0, 0]}
    out.update(git_stav(cwd))
    stav, hotovo = spotreba(prepis)
    out["partial"] = not hotovo
    if stav:
        with stav["zamek"]:
            modely = {k: list(v) for k, v in stav["models"].items()}
            out["model"], out["answers"] = stav["model"], max(0, stav["answers"])
        soucet = [max(0, sum(v[i] for v in modely.values())) for i in range(4)]
        out["usage"] = soucet
        out["tokens"] = soucet[0] + soucet[1] + soucet[2]   # bez čtení z cache — to jen zkresluje
        pouzite = {k: v for k, v in modely.items() if any(v)}
        # Cena jen když ceník zná všechny použité modely (jinak by byla vymyšlená).
        if pouzite and all(pricing.known(k) for k in pouzite):
            out["cost"] = round(sum(pricing.cost(k, {"in": v[0], "out": v[1], "cache_w": v[2],
                                                     "cache_r": v[3]})
                                    for k, v in pouzite.items()), 4)
    if not out["model"] and prepis:
        try:
            out["model"] = core._last_model(prepis)[0]
        except Exception:
            pass
    out["label"] = core.model_label(out["model"]) if out["model"] else ""
    return out


def zjisti(session):
    """Řádek pro tab (session ze serveru). Chyba = {"ok": False, "chyba": …}, ne výjimka."""
    if not session:
        return {"ok": False, "chyba": "Tab nebyl nalezen."}
    try:
        cwd = getattr(session, "path", None) or core.HOME
        return zjisti_z(cwd, core.transcript_for(session), getattr(session, "started", 0))
    except Exception as exc:
        return {"ok": False, "chyba": f"Stavový řádek se nepodařilo spočítat ({type(exc).__name__})."}
