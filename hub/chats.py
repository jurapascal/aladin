"""
Seznam konverzací s Claude Code — jako seznam chatů v oficiální appce.

Claude Code si každou konverzaci ukládá do ~/.claude/projects/<složka>/<id>.jsonl
a sám do ní zapisuje i název (`ai-title`, po přejmenování `custom-title`). Hub
z toho skládá seznam: název, projekt, kdy se naposledy změnila. Otevření je
`claude --resume <id>` ve složce, kde konverzace vznikla (core.cmd_agent).

Přepisy mívají i stovky MB, takže se čte jen začátek (první zadání, složka)
a konec (poslední název) a výsledek se drží v paměti podle velikosti a času
změny. Podkonverzace subagentů (`<id>/subagents/`) do seznamu nepatří.
"""
import json
import os
import re
import stat
import threading
import time

from . import core

HEAD = 128 * 1024
TAIL = 256 * 1024
_CACHE = {}                      # cesta -> (velikost, mtime, info)
_LOCK = threading.Lock()
# Mezipaměť přežije restart hubu: bez ní první načtení seznamu po startu četlo
# začátek a konec každého přepisu (stovky souborů, přes vteřinu čekání).
_DISK_VERSION = 1
_disk_loaded = False
_disk_saved = [0.0]              # kdy naposled — rozepsaný chat se mění pořád
# Značky, které Claude Code vkládá do uživatelských zpráv a člověk je nepsal.
# `task-notification` = hláška, že doběhla úloha na pozadí — v seznamu i ve
# čtení by se jinak tvářila jako něco, co člověk napsal.
_NOISE = re.compile(r"<(system-reminder|local-command-stdout|local-command-stderr|"
                    r"command-message|task-notification)>.*?</\1>", re.S)


def _root():
    return os.path.join(core.CLAUDE_DIR, "projects")


def _entries(raw, partial_start):
    lines = raw.split(b"\n")
    if partial_start and lines:
        lines = lines[1:]            # první řádek konce souboru bývá useknutý
    for line in lines:
        line = line.strip()
        if not line.startswith(b"{"):
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict):
            yield entry


def prompt_text(entry):
    """Co člověk do konverzace napsal, nebo '' (meta zprávy, výsledky nástrojů,
    podkonverzace, upozornění Claude Code)."""
    if entry.get("type") != "user" or entry.get("isMeta") or entry.get("isSidechain"):
        return ""
    content = (entry.get("message") or {}).get("content")
    if isinstance(content, list):
        content = " ".join(part.get("text", "") for part in content
                           if isinstance(part, dict) and part.get("type") == "text")
    if not isinstance(content, str):
        return ""
    command = re.search(r"<command-name>(.*?)</command-name>", content, re.S)
    if command:
        args = re.search(r"<command-args>(.*?)</command-args>", content, re.S)
        text = command.group(1) + " " + (args.group(1) if args else "")
    else:
        text = _NOISE.sub(" ", content)
    text = " ".join(text.split())
    if not text or text.startswith(("Caveat:", "[Request interrupted")):
        return ""
    # Přepnutí modelu (i automatické z bubliny) chat nepojmenovává.
    if text.split(" ", 1)[0] in ("/model", "/effort"):
        return ""
    return text[:200]


def _info(path, size):
    try:
        with open(path, "rb") as fh:
            head = fh.read(HEAD)
            tail = b""
            if size > HEAD:
                fh.seek(max(HEAD, size - TAIL))
                tail = fh.read(TAIL)
    except OSError:
        return None
    title = custom = prompt = cwd = ""
    entries = list(_entries(head, False))
    if tail:
        entries += list(_entries(tail, True))
    for entry in entries:            # později zapsaný název vyhrává
        kind = entry.get("type")
        if kind == "ai-title" and isinstance(entry.get("aiTitle"), str):
            title = entry["aiTitle"]
        elif kind == "custom-title" and isinstance(entry.get("customTitle"), str):
            custom = entry["customTitle"]
        if not cwd and isinstance(entry.get("cwd"), str):
            cwd = entry["cwd"]
        if not prompt:
            prompt = prompt_text(entry)
    name = " ".join((custom or title or prompt).split())
    if not name:
        return None                  # prázdná konverzace (tab otevřený a hned zavřený)
    return {"id": os.path.basename(path)[:-6], "title": name[:120], "prompt": prompt,
            "cwd": cwd, "project": os.path.basename(cwd.rstrip("/\\")) if cwd else ""}


def _disk_path():
    return os.path.join(core.CLAUDE_DIR, "hub-chats-cache.json")


def _load_disk():
    global _disk_loaded
    with _LOCK:
        if _disk_loaded:
            return
        _disk_loaded = True
    try:
        with open(_disk_path(), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return
    if not isinstance(data, dict) or data.get("v") != _DISK_VERSION:
        return
    items = data.get("items")
    if not isinstance(items, dict):
        return
    with _LOCK:
        for path, row in items.items():
            if (isinstance(row, list) and len(row) == 3 and path not in _CACHE
                    and (row[2] is None or isinstance(row[2], dict))):
                _CACHE[path] = (row[0], row[1], row[2])


def _save_disk():
    with _LOCK:
        items = {p: list(v) for p, v in _CACHE.items()}
    tmp = _disk_path() + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"v": _DISK_VERSION, "items": items}, fh, ensure_ascii=False,
                      separators=(",", ":"))
        os.replace(tmp, _disk_path())
    except OSError:
        pass


def warm_up():
    """Při startu hubu: seznam připravit dřív, než si o něj stránka řekne."""
    try:
        list_chats()
    except Exception as exc:
        core.log_error("chaty: příprava seznamu", exc)


def list_chats(limit=1000):
    """Konverzace od nejnověji změněné: id, title, prompt, cwd, project,
    exists (složka pořád je), updated (epoch), size."""
    _load_disk()
    changed = False
    root = _root()
    try:
        folders = os.listdir(root)
    except OSError:
        return []
    out, seen = [], set()
    for folder in folders:
        base = os.path.join(root, folder)
        try:
            names = os.listdir(base)
        except OSError:
            continue
        for name in names:
            if not name.endswith(".jsonl") or not core.SESSION_ID.fullmatch(name[:-6]):
                continue
            path = os.path.join(base, name)
            try:
                st = os.stat(path)
            except OSError:
                continue
            if not stat.S_ISREG(st.st_mode):
                continue
            seen.add(path)
            with _LOCK:
                cached = _CACHE.get(path)
            if cached and cached[0] == st.st_size and cached[1] == st.st_mtime:
                info = cached[2]
            else:
                info = _info(path, st.st_size)
                changed = True
                with _LOCK:
                    _CACHE[path] = (st.st_size, st.st_mtime, info)
            if info:
                out.append(dict(info, updated=int(st.st_mtime), size=st.st_size,
                                exists=bool(info["cwd"]) and os.path.isdir(info["cwd"])))
    with _LOCK:
        for gone in [p for p in _CACHE if p not in seen]:
            _CACHE.pop(gone, None)
            changed = True
    if changed and time.monotonic() - _disk_saved[0] > 30:
        _disk_saved[0] = time.monotonic()
        _save_disk()
    out.sort(key=lambda c: c["updated"], reverse=True)
    return out[:limit]


def delete_chat(chat_id):
    """Smazat konverzaci ze seznamu: přepis (a podkonverzace `<id>/`) se
    přesune do koše ~/.claude/hub-kos/chaty/, ne natvrdo — omyl jde vrátit
    přesunem zpět. Vrací počet přesunutých položek (0 = nenalezeno)."""
    if not core.SESSION_ID.fullmatch(str(chat_id or "")):
        raise ValueError("Neplatná konverzace.")
    root = _root()
    kos = os.path.join(core.CLAUDE_DIR, "hub-kos", "chaty")
    stamp = time.strftime("%Y%m%d-%H%M%S")
    moved = 0
    try:
        folders = os.listdir(root)
    except OSError:
        return 0
    for folder in folders:
        base = os.path.join(root, folder)
        for name in (chat_id + ".jsonl", chat_id):
            src = os.path.join(base, name)
            if not os.path.lexists(src) or os.path.islink(src):
                continue
            dst_dir = os.path.join(kos, folder)
            os.makedirs(dst_dir, exist_ok=True)
            os.replace(src, os.path.join(dst_dir, f"{stamp}-{name}"))
            moved += 1
            with _LOCK:
                _CACHE.pop(src, None)
    return moved
