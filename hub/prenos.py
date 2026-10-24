"""
Přenos chatu mezi počítačem a serverem.

Chat je v Claude Code soubor `<id>.jsonl` (a složka s pomocníky `<id>/subagents`)
ve složce `~/.claude/projects/<cesta projektu s pomlčkami>/`. Když ho zkopíruješ
na druhý stroj do správné složky, `claude --resume <id>` v ní pokračuje — i s tím,
co už bylo řečeno. Tenhle modul balí chat do jednoho zip souboru (přepis se
stlačí na pětinu, takže se vejde do limitů brány) a na druhé straně ho rozbalí
do složky projektu, ve které se bude pokračovat, a přepíše v něm cestu (`cwd`),
jinak by hub tvrdil, že složka chatu neexistuje.

Bez závislostí na zbytku hubu: používá ho hub (počítač i prostor) i brána.
Nic se nemaže ani nepřepisuje, co už tam je — stejný chat se přenese znovu
a přepíše sám sebe; cizí chat se stejným id tu být nemůže (id je UUID).
"""
import io
import json
import os
import re
import time
import zipfile

SESSION_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
# Strop balíčku. Přepisy s obrázky se stlačí jen asi na polovinu, takže chat,
# který Claude Code ještě unese, má i po stlačení stovky MB. Jde po kusech
# (CAST níž) a rozbaluje se proudem, takže se celý nikdy nedrží v paměti
# rozbalený.
MAX_ZIP = 300 * 1024 * 1024
CAST = 7 * 1024 * 1024               # po kolika bajtech se balíček dělí na kusy pro bránu
MAX_KUSU = -(-MAX_ZIP // CAST)       # kolik kusů se nejvýš přijme
MAX_ROZBALENO = 3 * 1024 * 1024 * 1024  # ochrana před „zip bombou"
MAX_SOUBORU = 400
SUBAGENT = re.compile(r"[A-Za-z0-9._-]{1,120}")


def slug(cwd):
    """Jak Claude Code pojmenuje složku projektu: cokoli mimo písmena a číslice je pomlčka."""
    return re.sub(r"[^A-Za-z0-9]", "-", os.path.realpath(cwd) if os.path.isdir(cwd) else str(cwd))


def najdi(claude_dir, chat_id):
    """Cesta k přepisu chatu a složka s pomocníky ('' když není)."""
    if not SESSION_ID.fullmatch(str(chat_id or "")):
        return "", ""
    root = os.path.join(claude_dir, "projects")
    try:
        folders = os.listdir(root)
    except OSError:
        return "", ""
    for folder in folders:
        path = os.path.join(root, folder, chat_id + ".jsonl")
        if os.path.isfile(path):
            sub = os.path.join(root, folder, chat_id, "subagents")
            return path, (sub if os.path.isdir(sub) else "")
    return "", ""


def _cwd_z_prepisu(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for i, line in enumerate(fh):
                if '"cwd"' in line:
                    try:
                        cwd = json.loads(line).get("cwd")
                    except ValueError:
                        continue
                    if isinstance(cwd, str):
                        return cwd
                if i > 300:
                    break
    except OSError:
        pass
    return ""


def zabal(claude_dir, chat_id, titulek="", odkud=""):
    """Zip s chatem: `meta.json`, `<id>.jsonl`, `<id>/subagents/*`. → (bajty, meta)."""
    path, sub = najdi(claude_dir, chat_id)
    if not path:
        raise ValueError("Tenhle chat tu nemám — nejspíš ještě nemá žádnou zprávu.")
    meta = {"id": chat_id, "title": " ".join(str(titulek or "").split())[:120] or "Chat",
            "cwd": _cwd_z_prepisu(path), "from": str(odkud or "")[:80], "created": int(time.time())}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        z.writestr("meta.json", json.dumps(meta, ensure_ascii=False))
        z.write(path, chat_id + ".jsonl")
        if sub:
            for name in sorted(os.listdir(sub))[:MAX_SOUBORU]:
                full = os.path.join(sub, name)
                if os.path.isfile(full) and SUBAGENT.fullmatch(name):
                    z.write(full, f"{chat_id}/subagents/{name}")
    data = buf.getvalue()
    if len(data) > MAX_ZIP:
        raise ValueError(
            f"Chat je i po stlačení {len(data) // (1024 * 1024)} MB — přes bránu se vejde nejvýš "
            f"{MAX_ZIP // (1024 * 1024)} MB. Začni v novém chatu a tenhle si nech.")
    return data, meta


def kusy(data):
    """Balíček rozdělený na kusy pro bránu (každý ≤ CAST). Pohledy do `data`,
    ne kopie — u balíčku o stovkách MB by se paměť zdvojila."""
    view = memoryview(data)
    return [view[i:i + CAST] for i in range(0, len(data), CAST)] or [b""]


def _zip(src):
    """Balíček z bajtů nebo ze souboru na disku (kusy složené do souboru)."""
    try:
        return zipfile.ZipFile(io.BytesIO(src) if isinstance(src, (bytes, bytearray)) else src)
    except (zipfile.BadZipFile, OSError):
        raise ValueError("Balíček chatu je poškozený.") from None


def _prepis_cwd(fh, cwd):
    """Řádky přepisu mají `cwd` — přepíše se na složku, ve které se bude pokračovat.
    Čte a vrací po řádcích, přepis o stovkách MB se do paměti nenačte celý."""
    for raw in fh:
        if b'"cwd"' in raw:
            konec = b"\n" if raw.endswith(b"\n") else b""
            try:
                obj = json.loads(raw)
            except ValueError:
                yield raw
                continue
            if isinstance(obj, dict) and isinstance(obj.get("cwd"), str):
                obj["cwd"] = cwd
                raw = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + konec
        yield raw


def _proud(z, info):
    """Obsah souboru v balíčku po kusech."""
    with z.open(info) as fh:
        while True:
            kus = fh.read(1024 * 1024)
            if not kus:
                return
            yield kus


def rozbal(src, cwd, zapis):
    """Rozbalí chat pro pokračování ve složce `cwd`. `src` jsou bajty balíčku
    nebo cesta k němu. `zapis(rel, kusy)` ukládá soubor (iterátor bajtů)
    relativně ke složce projektu (`projects/<slug(cwd)>/`). Vrací `meta`;
    neplatný zip → ValueError."""
    z = _zip(src)
    infos = z.infolist()
    if len(infos) > MAX_SOUBORU + 5 or sum(i.file_size for i in infos) > MAX_ROZBALENO:
        raise ValueError("Balíček chatu je podezřele velký.")
    try:
        meta = json.loads(z.read("meta.json").decode("utf-8"))
    except (KeyError, ValueError):
        raise ValueError("Balíček chatu nemá popis.") from None
    cid = str(meta.get("id") or "")
    if not SESSION_ID.fullmatch(cid):
        raise ValueError("Balíček chatu má neplatné číslo.")
    sub = re.compile(re.escape(cid) + r"/subagents/(" + SUBAGENT.pattern + ")")
    nasel = False
    for info in infos:
        name = info.filename
        if name == cid + ".jsonl":
            with z.open(info) as fh:
                zapis(name, _prepis_cwd(fh, cwd))
            nasel = True
        elif sub.fullmatch(name):
            if name.endswith(".jsonl"):
                with z.open(info) as fh:
                    zapis(name, _prepis_cwd(fh, cwd))
            else:
                zapis(name, _proud(z, info))
    if not nasel:
        raise ValueError("V balíčku chybí přepis chatu.")
    return {"id": cid, "title": " ".join(str(meta.get("title") or "").split())[:120] or "Chat",
            "from": str(meta.get("from") or "")[:80]}


def rozbal_na_disk(src, claude_dir, cwd):
    """Totéž pro počítač: soubory rovnou do `<claude_dir>/projects/<slug(cwd)>/`.

    Chat, který tu už je (poslal se tam a vrací se), se přepíše na místě — ve
    své původní složce a s původním `cwd`. Jinak by tu byly dva přepisy
    stejného chatu a hub by četl ten starý. Předchozí verze zůstane vedle
    jako `<id>.jsonl.bak`."""
    folder = os.path.join(claude_dir, "projects", slug(cwd))
    try:
        cid = json.loads(_zip(src).read("meta.json").decode("utf-8")).get("id", "")
    except (KeyError, ValueError):
        cid = ""
    stary, _sub = najdi(claude_dir, cid)
    if stary:
        folder = os.path.dirname(stary)
        cwd = _cwd_z_prepisu(stary) or cwd
        try:
            import shutil
            shutil.copyfile(stary, stary + ".bak")
        except OSError:
            pass

    def zapis(rel, body):
        target = os.path.join(folder, *rel.split("/"))
        os.makedirs(os.path.dirname(target), exist_ok=True)
        tmp = target + ".prenos-tmp"
        with open(tmp, "wb") as fh:
            for kus in body:
                fh.write(kus)
        os.replace(tmp, target)
    return rozbal(src, cwd, zapis)


def sloz(kusy_cesty, cil):
    """Kusy balíčku (soubory v pořadí) složí do jednoho souboru `cil` bez
    načtení do paměti. Vrací `cil`."""
    import shutil
    tmp = cil + ".tmp"
    with open(tmp, "wb") as out:
        for cesta in kusy_cesty:
            with open(cesta, "rb") as fh:
                shutil.copyfileobj(fh, out, 1024 * 1024)
    os.replace(tmp, cil)
    return cil


# ── chaty, které přišly z druhé strany a čekají na otevření ──────────────────
def _marker(claude_dir):
    return os.path.join(claude_dir, "hub-prenos.json")


def cekajici(claude_dir):
    try:
        with open(_marker(claude_dir), encoding="utf-8") as fh:
            items = json.load(fh)
    except (OSError, ValueError):
        return []
    return [i for i in items if isinstance(i, dict) and SESSION_ID.fullmatch(str(i.get("id") or ""))] \
        if isinstance(items, list) else []


def pridej_cekajici(claude_dir, meta):
    items = [i for i in cekajici(claude_dir) if i.get("id") != meta["id"]]
    items.append({"id": meta["id"], "title": meta.get("title", "Chat"), "from": meta.get("from", ""),
                  "at": int(time.time())})
    _uloz(claude_dir, items[-20:])


def odeber_cekajici(claude_dir, chat_id):
    _uloz(claude_dir, [i for i in cekajici(claude_dir) if i.get("id") != chat_id])


def _uloz(claude_dir, items):
    os.makedirs(claude_dir, exist_ok=True)
    tmp = _marker(claude_dir) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(items, fh, ensure_ascii=False)
    os.replace(tmp, _marker(claude_dir))
