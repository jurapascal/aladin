"""
Soubory (PDF, obrázky, tabulky, cokoli) ve firemním a sdíleném Obsidianu.

Poznámky do společných trezorů nosí Claude návrhem a potvrzuje je karta.
Soubory nahrává jen člověk v prohlížeči: požadavek musí přijít ze stránky
hubu, s přihlašovací cookie brány (do prostoru se nepřeposílá) a s hlavičkou
`X-Hub-Firma`, kterou cizí web bez povolení nepošle. Claude v prostoru tak
soubor do společného trezoru nedostane — trezor má jen ke čtení.

Co se hlídá (brána znovu, nic nevěří prohlížeči):

* právo: firemní jen s právem zápisu a na cestu, kterou člověk vidí
  (poznamky.py, pravidla složek), sdílený jen člen,
* cesta: relativní, bez `..`, skrytých částí a řídicích znaků, žádné
  `Lidé/` a `Sdílené/` (to nejsou firemní složky), cíl nesmí být odkaz
  a rodič musí doopravdy ležet v trezoru,
* velikost jednoho souboru (`HUB_GW_UPLOAD_MB`, 20 MB — nginx pustí 25 MB)
  a strop celého trezoru (`HUB_GW_COMPANY_QUOTA_MB` 5 GB, sdílený
  `HUB_GW_SHARED_QUOTA_MB` 2 GB) — disk sdílí weby, mail i zálohy,
* existující soubor se přepíše jen výslovně (`prepsat`),
* soubor se nikdy nespouští: práva 0644, bez spustitelného bitu; hub ho
  vydává ke stažení (Content-Disposition: attachment, nosniff, sandbox).

Každé nahrání jde do záznamu (kdo, kam, velikost, sha256).
"""
import hashlib
import json
import os
import re
import time


UPLOAD_MAX = int(os.environ.get("HUB_GW_UPLOAD_MB", "20")) * 1024 * 1024
COMPANY_QUOTA = int(os.environ.get("HUB_GW_COMPANY_QUOTA_MB", "5120")) * 1024 * 1024
SHARED_QUOTA = int(os.environ.get("HUB_GW_SHARED_QUOTA_MB", "2048")) * 1024 * 1024
_BAD = re.compile(r"[\x00-\x1f\x7f/\\:*?\"<>|]")
SKIP = {".obsidian", ".git", ".trash", ".smart-env", "node_modules"}


def clean_rel(rel):
    """Relativní cesta souboru v trezoru — každá část zvlášť ověřená.
    Odmítá, nic tiše neopravuje (kromě mezer na krajích)."""
    raw = str(rel or "").replace("\\", "/")
    if raw.startswith("/") or re.match(r"^[A-Za-z]:", raw) or len(raw) > 300:
        raise ValueError("Neplatná cesta souboru.")
    parts = [p.strip() for p in raw.split("/") if p.strip() not in ("", ".")]
    if not parts:
        raise ValueError("Soubor potřebuje jméno.")
    for p in parts:
        if p == ".." or p.startswith(".") or _BAD.search(p) or len(p) > 150 or p in SKIP:
            raise ValueError(f"Tohle jméno v cestě nejde: {p}")
    return "/".join(parts)


def _size(root, skip_links=True):
    total = 0
    for base, dirs, files in os.walk(root):
        if skip_links:
            dirs[:] = [d for d in dirs if not os.path.islink(os.path.join(base, d))]
        for name in files:
            path = os.path.join(base, name)
            try:
                if not os.path.islink(path):
                    total += os.path.getsize(path)
            except OSError:
                pass
    return total


def write(vault, rel, raw, overwrite=False, quota=0, in_place=False):
    """Zapíše `raw` na `rel` do trezoru `vault`. Vrací {"ok", "path", ...}
    nebo {"ok": False, "exists": True}, když soubor je a `overwrite` není."""
    if not isinstance(raw, (bytes, bytearray)) or not raw:
        raise ValueError("Soubor je prázdný.")
    if len(raw) > UPLOAD_MAX:
        raise ValueError(f"Soubor je větší než {UPLOAD_MAX // (1024 * 1024)} MB.")
    rel = clean_rel(rel)
    root = os.path.realpath(vault)
    target = os.path.join(root, *rel.split("/"))
    parent = os.path.dirname(target)
    # Nejbližší existující rodič musí být skutečně v trezoru (odkaz ven ne).
    at = parent
    while not os.path.isdir(at) and os.path.dirname(at) != at:
        at = os.path.dirname(at)
    if os.path.commonpath([os.path.realpath(at), root]) != root:
        raise ValueError("Neplatná cesta v Obsidianu.")
    if os.path.islink(target) or (os.path.exists(target) and not os.path.isfile(target)):
        raise ValueError("Na téhle cestě je něco, co přepsat nejde.")
    existed = os.path.exists(target)
    if existed and not overwrite:
        return {"ok": False, "exists": True, "path": rel}
    if quota:
        grow = len(raw) - (os.path.getsize(target) if existed else 0)
        if _size(root) + grow > quota:
            raise ValueError(f"Obsidian je plný (strop {quota // (1024 * 1024)} MB) — "
                             "uvolni místo, nebo požádej admina.")
    os.makedirs(parent, exist_ok=True)
    if os.path.commonpath([os.path.realpath(parent), root]) != root:
        raise ValueError("Neplatná cesta v Obsidianu.")
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if existed and in_place:
        # Ve složce, kterou prostory mají přivázanou po souborech
        # (poznamky.mask_args), musí zůstat tentýž soubor.
        fd = os.open(target, os.O_WRONLY | os.O_TRUNC | nofollow)
        with os.fdopen(fd, "wb") as fh:
            fh.write(raw)
    else:
        tmp = target + ".hub-tmp"
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | nofollow, 0o644)
        with os.fdopen(fd, "wb") as fh:
            fh.write(raw)
        os.replace(tmp, target)
    try:
        os.chmod(target, 0o644)
    except OSError:
        pass
    return {"ok": True, "path": rel, "overwritten": existed, "size": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest()}


def log(path, user, where, result, via="prohlížeč"):
    entry = {"cas": time.strftime("%Y-%m-%d %H:%M:%S"), "email": (user or {}).get("email", ""),
             "soubor": result.get("path"), "kam": where, "bajtu": result.get("size"),
             "sha256": result.get("sha256"), "prepsano": result.get("overwritten"), "pres": via}
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass
