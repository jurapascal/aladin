"""
Podklady pro cizí aplikace — čtení vybraných složek osobního Obsidianu.

Napojení z appky Claude (gateway/mcp.py) dává celý prostor: soubory, příkazy,
konverzace. To je pro aplikaci, která má z poznámek jen odpovídat (telefonní
asistent v Přepisu), moc. Proto má OAuth brány druhý, úzký rozsah `podklady`:

* aplikace se přihlásí stejně (heslo + kód z aplikace + souhlas), ale na
  souhlasu člověk **zaškrtne složky** svého osobního Obsidianu, které smí
  číst. Nic jiného token neumí — žádný zápis, soubory, příkazy, projekty,
  konverzace ani firemní a sdílené Obsidiany;
* složky se uloží k přihlášení (rodina tokenů v accounts.py) a brána je
  kontroluje při každém volání. Aplikace si je sama rozšířit nemůže, jen
  zúžit (parametr `zdroje`); víc složek = napojit znovu;
* poznámka označená jako soukromá se nenabízí ani neprohledává: ve
  frontmatteru `soukrome: true` (také `private`, `citlive`, `tajne`) nebo
  `asistent: ne`, případně štítek `#soukrome`, `#private`, `#citlive`, `#tajne`;
* adresy, na které smí přihlášení s tímhle rozsahem vrátit kód: vestavěné
  (`VYCHOZI` — Přepis, ať jde napojit na každý server s touhle verzí bez
  zásahu správce), plus `aplikace.json` vedle databáze (`claude-hub-admin
  aplikace add|remove`; vestavěnou jde vypnout stejně) a `HUB_GW_PODKLADY_REDIRECTS`.
  Rozsah `prostor` přes ně dostat nejde.

Čte se rovnou z disku, bez prostoru uživatele (hovor nečeká na start
sandboxu). Domov ale patří session a ta v něm může nechat odkaz kamkoli,
proto se jde jen přes deskriptory složek s O_NOFOLLOW (jako gateway/safefs.py)
a odkazy se přeskakují.
"""
import json
import os
import re
import stat
import time
import unicodedata

from . import config, safefs, workspace

SCOPE = "podklady"
APPS_FILE = os.path.join(config.GATEWAY_DIR, "aplikace.json")
# Aplikace povolené bez nastavování. Jen čtení vybraných složek, jen po
# přihlášení s kódem z aplikace a souhlasu — a správce je může vypnout.
VYCHOZI = ("https://alba-rosa.cz/prepis/aladin.php",)

SKIP = {".obsidian", ".git", ".trash", ".smart-env", "node_modules"}
MAX_DEPTH = 12
MAX_FILES = 6000               # kolik poznámek jedno hledání nejvýš projde
MAX_NOTE = 512 * 1024          # z poznámky se čte nejvýš tolik
LIST_NOTES = 400               # kolik poznámek ze složky vrátí výpis
SEARCH_SECONDS = 2.5           # hledání běží při hovoru: radši méně než pozdě
LIST_SECONDS = 4.0
MAX_SOURCES = 40
SNIPPET = 360                  # znaků kolem nalezeného místa
MAX_RESULTS = 8

_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_DIR = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | _CLOEXEC
_FILE = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | _CLOEXEC


class Chyba(Exception):
    """Chyba, kterou má vidět aplikace."""


# ── adresy aplikací (kam smí přihlášení s rozsahem podklady vrátit kód) ──────
def _load_apps():
    """(přidané adresy, vypnuté vestavěné) z aplikace.json."""
    try:
        with open(APPS_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}

    def clean(key):
        return {u for u in data.get(key) or [] if isinstance(u, str) and u.startswith("https://")
                and len(u) < 500}
    return clean("redirects"), clean("vypnute")


def redirects():
    added, off = _load_apps()
    out = {u.strip() for u in os.environ.get("HUB_GW_PODKLADY_REDIRECTS", "").split(",")
           if u.strip().startswith("https://")}
    return out | added | (set(VYCHOZI) - off)


def redirect_ok(uri):
    return isinstance(uri, str) and uri in redirects()


def _save_apps(added, off):
    os.makedirs(config.GATEWAY_DIR, exist_ok=True)
    tmp = APPS_FILE + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump({"redirects": sorted(added), "vypnute": sorted(off)}, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, APPS_FILE)


def app_add(uri):
    uri = (uri or "").strip()
    if not re.fullmatch(r"https://[^\s#]{4,490}", uri):
        raise ValueError("Adresa musí začínat https:// a nesmí mít mezery ani #.")
    added, off = _load_apps()
    if uri in VYCHOZI:
        _save_apps(added, off - {uri})          # vestavěnou zase zapnout
    else:
        _save_apps(added | {uri}, off)


def app_remove(uri):
    """Odebere přidanou adresu, nebo vypne vestavěnou. False = nebyla povolená."""
    uri = (uri or "").strip()
    added, off = _load_apps()
    if uri in VYCHOZI and uri not in off:
        _save_apps(added - {uri}, off | {uri})
        return True
    if uri not in added:
        return False
    _save_apps(added - {uri}, off)
    return True


# ── cesty a čtení bez odkazů ─────────────────────────────────────────────────
def _parts(rel):
    rel = str(rel or "").replace("\\", "/").strip().strip("/")
    parts = [p for p in rel.split("/") if p not in ("", ".")]
    if (not parts or len(rel) > 300 or any(p == ".." or p.startswith(".") or p in SKIP
                                           for p in parts)):
        raise Chyba(f"Neplatná cesta: {rel[:80]!r}")
    return parts


def clean(rel):
    """Cesta v trezoru v jednom tvaru („Firma/Ceník.md“), nebo Chyba."""
    return "/".join(_parts(rel))


def _open_vault(user, parts=()):
    home = workspace.home_for(user)
    return safefs.open_dir(home, ["Obsidian", workspace.vault_name(user), *parts])


def _walk(fd, prefix, state, depth=0):
    """(relativní cesta, fd složky, jméno) poznámek .md pod `fd`; odkazy ne."""
    try:
        entries = sorted(os.scandir(fd), key=lambda e: e.name)
    except OSError:
        return
    for e in entries:
        if state["files"] >= MAX_FILES or time.monotonic() > state["deadline"]:
            state["cut"] = True
            return
        if e.name.startswith(".") or e.name in SKIP:
            continue
        rel = f"{prefix}/{e.name}" if prefix else e.name
        try:
            if e.is_dir(follow_symlinks=False):
                if depth >= MAX_DEPTH:
                    continue
                try:
                    sub = os.open(e.name, _DIR, dir_fd=fd)
                except OSError:
                    continue
                try:
                    yield from _walk(sub, rel, state, depth + 1)
                finally:
                    os.close(sub)
            elif e.is_file(follow_symlinks=False) and e.name.lower().endswith(".md"):
                state["files"] += 1
                yield rel, fd, e.name
        except OSError:
            continue


def _read(dir_fd, name):
    try:
        fd = os.open(name, _FILE, dir_fd=dir_fd)
    except OSError:
        return None
    with os.fdopen(fd, "rb") as fh:
        if not stat.S_ISREG(os.fstat(fh.fileno()).st_mode):
            return None
        return fh.read(MAX_NOTE).decode("utf-8", "replace")


def _notes(user, sources, seconds):
    """Poznámky ve zdrojích (složky nebo jednotlivé poznámky):
    generátor (cesta, text) + stav (`cut` = nestihlo se všechno)."""
    state = {"files": 0, "deadline": time.monotonic() + seconds, "cut": False}

    def gen():
        seen = set()
        for src in sources:
            parts = src.split("/")
            if src.lower().endswith(".md"):
                try:
                    fd = _open_vault(user, parts[:-1])
                except OSError:
                    continue
                try:
                    text = _read(fd, parts[-1])
                finally:
                    os.close(fd)
                if text is not None and src not in seen:
                    seen.add(src)
                    yield src, text
                continue
            try:
                fd = _open_vault(user, parts)
            except OSError:
                continue
            try:
                for rel, dfd, name in _walk(fd, src, state):
                    if rel in seen:
                        continue
                    seen.add(rel)
                    text = _read(dfd, name)
                    if text is not None:
                        yield rel, text
            finally:
                os.close(fd)
    return gen(), state


# ── soukromé poznámky ────────────────────────────────────────────────────────
_TABLE = {0x130: "I"}
for _cp in range(0xC0, 0x250):
    _base = unicodedata.normalize("NFD", chr(_cp))[0]
    if _base != chr(_cp) and _base.isascii():
        _TABLE[_cp] = _base


def fold(text):
    """Malá písmena bez diakritiky, stejně dlouhý text (úryvky sedí na originál)."""
    return str(text).translate(_TABLE).lower()


_FRONT = re.compile(r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(\r?\n|\Z)", re.S)
_PRIVATE_KEY = re.compile(r"^\s*(soukrome|private|citlive|tajne)\s*:\s*(true|ano|yes|1)\s*$", re.M)
_ASSISTANT_NO = re.compile(r"^\s*(asistent|assistant|telefon)\s*:\s*(false|ne|no|0)\s*$", re.M)
_TAG = re.compile(r"(^|\s)#(soukrome|private|citlive|tajne)\b")
_TAGS_KEY = re.compile(r"^\s*(tags|stitky)\s*:", re.M)
_PRIVATE_WORD = re.compile(r"\b(soukrome|private|citlive|tajne)\b")


def split_front(text):
    m = _FRONT.match(text)
    return (m.group(1), text[m.end():]) if m else ("", text)


def is_private(text):
    """Soukromá poznámka se nenabízí ani neprohledává. Ve štítcích frontmatteru
    se bere opatrně všechno od řádku `tags:` dál."""
    front, body = split_front(text)
    f = fold(front)
    if _PRIVATE_KEY.search(f) or _ASSISTANT_NO.search(f):
        return True
    m = _TAGS_KEY.search(f)
    if m and _PRIVATE_WORD.search(f[m.end():]):
        return True
    return bool(_TAG.search(fold(body)))


# ── složky k výběru na souhlasu a zdroje přihlášení ──────────────────────────
def folders(user, depth=2):
    """Složky osobního Obsidianu (do hloubky `depth`) s počtem poznámek."""
    try:
        fd = _open_vault(user)
    except OSError:
        return []
    counts = {}
    state = {"files": 0, "deadline": time.monotonic() + LIST_SECONDS, "cut": False}
    try:
        for rel, _dfd, _name in _walk(fd, "", state):
            parts = rel.split("/")[:-1]
            for i in range(1, min(depth, len(parts)) + 1):
                key = "/".join(parts[:i])
                counts[key] = counts.get(key, 0) + 1
    finally:
        os.close(fd)
    return [{"cesta": k, "poznamek": v} for k, v in sorted(counts.items(), key=lambda x: fold(x[0]))][:400]


def _inside(rel, folder):
    return rel == folder or rel.startswith(folder + "/")


def normalize_sources(raw):
    """Seznam složek bez duplicit a bez vnořených (A/B, když je tam A)."""
    out = []
    for r in raw or []:
        try:
            c = clean(r)
        except Chyba:
            continue
        if c not in out:
            out.append(c)
    out.sort(key=lambda s: (s.count("/"), s))
    kept = []
    for s in out:
        if not any(_inside(s, k) for k in kept):
            kept.append(s)
    return kept[:MAX_SOURCES]


def granted_choice(user, chosen):
    """Ze zaškrtnutých složek na souhlasu jen ty, které ve trezoru opravdu jsou."""
    have = {f["cesta"] for f in folders(user)}
    return normalize_sources([c for c in chosen if c in have])


def _narrow(granted, wanted):
    """Zdroje, které aplikace chce (`wanted`), oříznuté na povolené složky."""
    if wanted is None:
        return list(granted)
    if not isinstance(wanted, list):
        raise Chyba("zdroje musí být seznam cest.")
    out = []
    for w in wanted[:200]:
        try:
            c = clean(w)
        except Chyba:
            continue
        if any(_inside(c, g) for g in granted) and c not in out:
            out.append(c)
    return out


# ── nástroje ─────────────────────────────────────────────────────────────────
def _s(**props):
    required = [k for k, v in props.items() if v.pop("_req", False)]
    return {"type": "object", "properties": props, "required": required,
            "additionalProperties": False}


TOOLS = [
    {"name": "podklady_zdroje", "title": "Povolené podklady",
     "description": "Účet a složky Obsidianu, které smí tahle aplikace číst, s poznámkami v nich.",
     "inputSchema": _s(), "annotations": {"readOnlyHint": True}},
    {"name": "podklady_hledat", "title": "Hledat v podkladech",
     "description": "Najde v povolených poznámkách ty, které se týkají dotazu, a vrátí krátké "
                    "úryvky. `zdroje` hledání zúží na vybrané složky nebo poznámky.",
     "inputSchema": _s(dotaz={"type": "string", "_req": True},
                       zdroje={"type": "array", "items": {"type": "string"}},
                       limit={"type": "integer", "minimum": 1, "maximum": MAX_RESULTS}),
     "annotations": {"readOnlyHint": True}},
]
TOOL_NAMES = {t["name"] for t in TOOLS}
INSTRUCTIONS = ("Jen čtení vybraných složek osobního Obsidianu, které uživatel aplikaci "
                "povolil. Začni nástrojem `podklady_zdroje`.")

STOP = {
    "a", "aby", "ale", "ani", "ano", "asi", "bez", "byl", "byla", "bylo", "byt", "co", "dal",
    "do", "jak", "jake", "jaky", "jaka", "jsem", "jsi", "jste", "jsou", "kde", "kdy",
    "kdo", "ktery", "ktera", "ktere", "mate", "mam", "mi", "mne", "mit", "muze", "muzu", "na",
    "nebo", "nejak", "neco", "nic", "od", "po", "pod", "pro", "proc", "pri", "se", "si", "taky",
    "tak", "ten", "tam", "to", "tu", "uz", "ve", "vam", "vas", "vy", "za", "ze", "chci", "chtel",
    "chtela", "prosim", "dobry", "den", "rict", "vedet", "zeptat", "the", "and",
}


def terms(query):
    """Hledaná slova: bez diakritiky a výplňových slov, s uříznutou koncovkou
    (čeština skloňuje: „ceníku“ najde „ceník“)."""
    out = []
    for w in re.findall(r"\w+", fold(query)):
        if w in STOP or (len(w) < 3 and not (w.isdigit() and len(w) >= 2)):
            continue
        stem = w[:max(4, len(w) - 2)] if len(w) > 5 and not w.isdigit() else w
        if stem not in out:
            out.append(stem)
    return out[:10]


def _snippet(text, folded, positions):
    """Až dva úryvky kolem nalezených míst, oříznuté na celá slova."""
    windows = []
    for p in sorted(positions):
        a, b = max(0, p - SNIPPET // 3), min(len(text), p + SNIPPET * 2 // 3)
        if windows and a <= windows[-1][1]:
            windows[-1] = (windows[-1][0], max(windows[-1][1], b))
        else:
            windows.append((a, b))
        if len(windows) >= 2:
            break
    parts = []
    for a, b in windows[:2]:
        chunk = text[a:b]
        if a > 0 and " " in chunk[:30]:
            chunk = chunk[chunk.index(" ") + 1:]
        if b < len(text) and " " in chunk[-30:]:
            chunk = chunk[:chunk.rindex(" ")]
        parts.append(" ".join(chunk.split()))
    return " … ".join(p for p in parts if p)


def search(user, granted, query, wanted=None, limit=5):
    words = terms(query)
    if not words:
        raise Chyba("Dotaz nemá žádné hledatelné slovo.")
    sources = _narrow(granted, wanted)
    if not sources:
        return {"vysledky": [], "prohledano": 0, "zkraceno": False}
    try:
        limit = max(1, min(int(limit or 5), MAX_RESULTS))
    except (TypeError, ValueError):
        limit = 5
    notes, state = _notes(user, sources, SEARCH_SECONDS)
    scored = []
    count = 0
    for rel, text in notes:
        count += 1
        if is_private(text):
            continue
        _front, body = split_front(text)
        folded = fold(body)
        name = fold(rel)
        hits, score, positions = 0, 0, []
        for w in words:
            in_name = w in name
            n = folded.count(w)
            if n or in_name:
                hits += 1
                score += min(n, 5) + (3 if in_name else 0)
                if n:
                    positions.append(folded.find(w))
        if hits:
            scored.append((hits, score, rel, body, folded, positions))
    scored.sort(key=lambda s: (-s[0], -s[1], s[2]))
    out = []
    for hits, score, rel, body, folded, positions in scored[:limit]:
        snippet = _snippet(body, folded, positions) if positions else " ".join(body.split())[:SNIPPET]
        out.append({"cesta": rel, "nazev": os.path.splitext(rel.rsplit("/", 1)[-1])[0],
                    "uryvek": snippet, "shoda": f"{hits}/{len(words)}"})
    return {"vysledky": out, "prohledano": count, "zkraceno": state["cut"]}


def sources_listing(user, granted):
    """Povolené složky a v nich poznámky (bez soukromých)."""
    out = []
    deadline = time.monotonic() + LIST_SECONDS
    for g in granted:
        notes, state = _notes(user, [g], max(0.2, deadline - time.monotonic()))
        paths = []
        for rel, text in notes:
            if is_private(text):
                continue
            if len(paths) >= LIST_NOTES:
                state["cut"] = True
                break
            paths.append(rel)
        out.append({"cesta": g, "poznamky": paths, "zkraceno": state["cut"]})
    return out


def run_tool(user, name, args):
    """Nástroje pro token s rozsahem `podklady`. `user["sources"]` = povolené složky."""
    if name not in TOOL_NAMES:
        raise Chyba(f"Tohle napojení smí jen číst povolené podklady, nástroj {name} ne.")
    if not isinstance(args, dict):
        args = {}
    granted = normalize_sources(user.get("sources") or [])
    if name == "podklady_zdroje":
        return {"ucet": user.get("email", ""), "jmeno": user.get("name", ""),
                "zdroje": sources_listing(user, granted)}
    dotaz = str(args.get("dotaz") or "").strip()[:300]
    if not dotaz:
        raise Chyba("Chybí dotaz.")
    return search(user, granted, dotaz, args.get("zdroje"), args.get("limit"))
