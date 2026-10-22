"""Ukazatel kontextu: kolik kontextového okna zabírá konverzace v tabu.

Číslo se bere z posledního záznamu `assistant` v přepisu (message.usage):
vstup + čtení z cache + zápis do cache = to, co model při poslední odpovědi
dostal, tedy aktuální velikost kontextu. Čte se jen konec souboru — přepis
dlouhé session má desítky MB — a výsledek se krátce pamatuje podle mtime+velikosti.
Žádná výjimka z tohohle modulu ven nejde: nejhorší případ je prázdná odpověď.
"""
import json
import os
import threading

OKNO_ZAKLAD = 200_000
OKNO_1M = 1_000_000

# Kolik z konce přepisu se čte: nejdřív malý kus, a když v něm poslední odpověď
# není (jeden řádek s výsledkem nástroje umí mít megabajty), postupně větší.
_CTENI = (256 * 1024, 2 * 1024 * 1024, 8 * 1024 * 1024)

_LOCK = threading.Lock()
_CACHE = {}          # přepis → (mtime_ns, velikost, výsledek)
_JE_1M = {}          # přepis → True, jakmile se u něj okno 1M prokázalo
_MAX_CACHE = 128


def _cislo(v):
    """Počet tokenů z pole usage; cokoli jiného než nezáporné číslo je 0."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return 0
    return int(v) if v > 0 else 0


def _pre(e):
    """Velikost kontextu před /compact z compactMetadata, nebo 0."""
    meta = e.get("compactMetadata") if isinstance(e, dict) else None
    return _cislo(meta.get("preTokens")) if isinstance(meta, dict) else 0


def _z_konce(path, kus):
    """Poslední `kus` bajtů souboru po řádcích (od nejnovějšího) a jestli je to celý soubor."""
    with open(path, "rb") as fh:
        fh.seek(0, os.SEEK_END)
        konec = fh.tell()
        start = max(0, konec - kus)
        fh.seek(start)
        data = fh.read(kus)
    radky = data.split(b"\n")
    if start > 0 and radky:
        radky = radky[1:]          # první řádek je uříznutý uprostřed
    radky.reverse()
    return radky, start == 0


def _najdi(path):
    """({"tokens", "model", "compact"}, celý_soubor) z konce přepisu, nebo (None, …)."""
    cele = False
    for kus in _CTENI:
        radky, cele = _z_konce(path, kus)
        for i, raw in enumerate(radky):
            if b'"compact_boundary"' in raw or b'"isCompactSummary"' in raw:
                try:
                    e = json.loads(raw)
                except ValueError:
                    continue          # rozepsaný řádek
                if not isinstance(e, dict):
                    continue
                if e.get("isCompactSummary") is True or e.get("subtype") == "compact_boundary":
                    pre = _pre(e)
                    # Souhrn leží až za hranicí; velikost před zhuštěním má hranice.
                    for dalsi in radky[i + 1:i + 21]:
                        if pre or b'"compact_boundary"' not in dalsi:
                            continue
                        try:
                            pre = _pre(json.loads(dalsi))
                        except ValueError:
                            pass
                    # Po /compact nové číslo není, dokud Claude neodpoví.
                    return {"tokens": 0, "model": "", "compact": True, "pre": pre}, cele
                continue
            if b'"usage"' not in raw or b'"assistant"' not in raw:
                continue
            try:
                e = json.loads(raw)
            except ValueError:
                continue              # rozepsaný řádek nebo smetí
            if not isinstance(e, dict) or e.get("type") != "assistant":
                continue
            if e.get("isSidechain") is True or e.get("isApiErrorMessage") is True:
                continue              # agenti a hlášky o chybě nejsou kontext hlavní konverzace
            msg = e.get("message")
            if not isinstance(msg, dict):
                continue
            usage = msg.get("usage")
            if not isinstance(usage, dict):
                continue
            model = msg.get("model")
            model = model if isinstance(model, str) else ""
            if model == "<synthetic>":
                continue
            total = (_cislo(usage.get("input_tokens"))
                     + _cislo(usage.get("cache_read_input_tokens"))
                     + _cislo(usage.get("cache_creation_input_tokens")))
            if total <= 0:
                continue              # prázdné usage (přerušená odpověď)
            return {"tokens": total, "model": model, "compact": False, "pre": 0}, cele
        if cele:
            break
    return None, cele


def _okno(path, nalezeno, hint_1m, nastaveni):
    """(velikost okna nebo 0, jisté?) — 0 = neznámý model, ukáží se jen tokeny."""
    model = nalezeno.get("model") or ""
    if model and not model.startswith("claude-"):
        return 0, False           # cizí model: velikost okna neznáme, ani podle hlavičky
    if hint_1m or nastaveni or nalezeno["tokens"] > OKNO_ZAKLAD or nalezeno.get("pre", 0) > OKNO_ZAKLAD:
        _JE_1M[path] = True
    if _JE_1M.get(path):
        return OKNO_1M, True
    # Přepis nenese „[1m]". Bez důkazu se bere základní okno, ale jako odhad.
    return OKNO_ZAKLAD, False


def stav(path, hint_1m=False, nastaveni_1m=False):
    """Slovník pro ukazatel. Vždy platný; `tokens` None = zatím nic k ukázání."""
    prazdny = {"tokens": None, "window": 0, "sure": False, "model": "", "compact": False}
    try:
        if not path or not os.path.isfile(path):
            return prazdny
        st = os.stat(path)
        klic = (st.st_mtime_ns, st.st_size, bool(hint_1m), bool(nastaveni_1m))
        with _LOCK:
            hit = _CACHE.get(path)
            if hit and hit[0] == klic:
                return dict(hit[1])
        nalezeno, _ = _najdi(path)
        if nalezeno is None:
            out = dict(prazdny)
        else:
            with _LOCK:
                okno, jiste = _okno(path, nalezeno, hint_1m, nastaveni_1m)
            out = {"tokens": None if nalezeno["compact"] else nalezeno["tokens"],
                   "window": okno, "sure": jiste, "model": nalezeno["model"],
                   "compact": nalezeno["compact"]}
        with _LOCK:
            if len(_CACHE) >= _MAX_CACHE:
                _CACHE.clear()
            _CACHE[path] = (klic, dict(out))
        return out
    except Exception:           # nečitelný soubor, práva, mizející přepis…
        return prazdny
