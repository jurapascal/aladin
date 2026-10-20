"""
Nástroje pro napojení z appky Claude (MCP) — díl, který běží v prostoru.

Protokol, přihlášení a oprávnění drží brána (gateway/mcp.py). Tohle je jen
to, co se dá udělat **uvnitř** prostoru uživatele: hub tu běží v sandboxu,
ve kterém je zapisovatelný jen jeho domov a firemní či sdílené trezory jsou
přivázané jen ke čtení (a jen ty, na které má právo). Co tenhle modul
přečte nebo spustí, tedy nikdy nesáhne na cizí prostor — to hlídá sandbox,
ne tenhle kód. Cesty se tu navíc drží v domově, aby nástroj nečetl ani to,
co sandbox ukazuje ke čtení (kód hubu, systém).

Volá ho jen brána (`/api/mcp` s tokenem instance, který prohlížeč nemá);
zápis do firemního a sdílených trezorů dělá brána sama, sem se nedostane.
"""
import os
import re
import subprocess

from . import chats, core, cteni

MAX_READ = 512 * 1024          # kolik textu jde najednou do odpovědi
MAX_WRITE = 2 * 1024 * 1024
MAX_OUTPUT = 100 * 1024        # výstup příkazu
CMD_TIMEOUT = 120
CMD_TIMEOUT_MAX = 600
# Proměnné, které příkaz nedostane: přihlášení Clauda a klíče. Výstup příkazu
# odchází do konverzace v appce — kdyby ho někdo podstrčeným textem
# přemluvil vypsat prostředí, nemá co vynést.
SECRET_ENV = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|COOKIE|AUTH)", re.I)


class ToolError(Exception):
    """Chyba, kterou má vidět Claude (špatná cesta, chybějící soubor…)."""


def _home():
    return os.path.realpath(core.HOME)


def _in_home(rel, must_exist=True):
    """Cesta uvnitř domova (relativní k němu, nebo absolutní v něm).
    Odkazy se rozbalí a výsledek musí zůstat v domově."""
    home = _home()
    raw = str(rel or "").strip() or "."
    full = os.path.realpath(raw if os.path.isabs(raw) else os.path.join(home, raw))
    if os.path.commonpath([full, home]) != home:
        raise ToolError("Mimo tvůj prostor to nejde — cesta musí být v domově "
                        f"({home}).")
    if must_exist and not os.path.exists(full):
        raise ToolError(f"Tohle v prostoru není: {raw}")
    return full


def _vault(which):
    """osobni / firma / sdilene:<zkratka> → jak mu říká core ('' = osobní)."""
    which = str(which or "osobni").strip()
    if which in ("", "osobni", "osobní"):
        return ""
    if which == "firma":
        if not core.company_vault():
            raise ToolError("Firemní Obsidian tenhle účet nevidí.")
        return "firma"
    if re.fullmatch(r"firma:[a-z0-9][a-z0-9-]{0,39}", which):
        if not core.firma_root(which[6:]):
            raise ToolError("Do téhle firmy tenhle účet nepatří.")
        return which
    if re.fullmatch(r"sdilene:[a-z0-9][a-z0-9-]{0,49}", which):
        if which[8:] not in {v["slug"] for v in core.shared_state()}:
            raise ToolError("Do tohohle sdíleného Obsidianu nepatříš.")
        return which
    raise ToolError("Neznámý Obsidian — použij osobni, firma nebo sdilene:<zkratka>.")


# ── projekty a konverzace ────────────────────────────────────────────────────
def projekty(_args):
    out = []
    for p in core.get_projects():
        if p.get("archived"):
            continue
        out.append({"jmeno": p.get("label") or p["name"], "cesta": p["path"],
                    "typ": p.get("type", ""), "vetev": p.get("branch", ""),
                    "necommitnute": p.get("dirty", 0)})
    return {"projekty": out}


def konverzace(args):
    hledat = core._fold(args.get("hledat") or "")
    try:
        limit = max(1, min(int(args.get("limit") or 30), 200))
    except (TypeError, ValueError):
        limit = 30
    out = []
    for c in chats.list_chats():
        text = core._fold(" ".join([c.get("title") or "", c.get("prompt") or "",
                                    c.get("project") or ""]))
        if hledat and hledat not in text:
            continue
        out.append({"id": c["id"], "nazev": c.get("title") or "", "projekt": c.get("project") or "",
                    "zacatek": (c.get("prompt") or "")[:200], "zmeneno": c.get("updated")})
        if len(out) >= limit:
            break
    return {"konverzace": out}


def konverzace_cti(args):
    cid = str(args.get("id") or "")
    if not re.fullmatch(r"[0-9a-f-]{8,64}", cid):
        raise ToolError("Chybí id konverzace (z nástroje konverzace).")
    path = cteni.path_for(cid)
    if not path:
        raise ToolError("Takovou konverzaci v prostoru nemáš.")
    # Od konce: u dlouhé konverzace je podstatné, kde skončila.
    data = cteni.read(path, 0, tail=True)
    lines, size = [], 0
    for b in data.get("blocks") or []:
        kind = b.get("kind")
        if kind == "me":
            line = "## Uživatel\n" + (b.get("text") or "")
        elif kind == "say":
            line = "## Claude\n" + (b.get("text") or "")
        elif kind == "tool":
            line = f"- nástroj {b.get('name', '')}: {(b.get('title') or '')[:200]}"
        else:
            continue
        size += len(line)
        lines.append(line)
    text = "\n\n".join(lines)
    if len(text) > MAX_READ:
        text = "… (začátek vynechán)\n\n" + text[-MAX_READ:]
    return {"id": cid, "text": text}


# ── Obsidian ─────────────────────────────────────────────────────────────────
def obsidian_seznam(args):
    vault = _vault(args.get("obsidian"))
    tree = core.vault_tree(vault)
    notes = [n["path"] for n in tree.get("notes") or []]
    return {"obsidian": tree.get("name", ""), "poznamky": notes[:2000],
            "zkraceno": len(notes) > 2000}


def obsidian_hledat(args):
    vault = _vault(args.get("obsidian"))
    q = str(args.get("dotaz") or "").strip()
    if not q:
        raise ToolError("Chybí dotaz.")
    return {"vysledky": core.vault_search(q, limit=40, vault=vault)}


def obsidian_cti(args):
    vault = _vault(args.get("obsidian"))
    note = core.vault_note(str(args.get("cesta") or ""), vault)
    if not note:
        raise ToolError("Taková poznámka v Obsidianu není.")
    return {"cesta": note["path"], "text": note["text"], "zkraceno": note.get("truncated", False)}


def obsidian_zapis_osobni(args):
    """Zápis do osobního trezoru — firemní a sdílené zapisuje brána."""
    rel = str(args.get("cesta") or "")
    text = args.get("text")
    if not isinstance(text, str):
        raise ToolError("Chybí text poznámky.")
    full = core.vault_target(rel, "")
    if not full:
        raise ToolError("Tuhle cestu v trezoru uložit nejde.")
    if os.path.isfile(full) and not args.get("prepsat"):
        return {"ok": False, "existuje": True,
                "zprava": "Poznámka už existuje — pošli znovu s prepsat: true, "
                          "pokud ji chceš přepsat."}
    result = core.vault_save(rel, text)
    if not result.get("ok"):
        raise ToolError(result.get("error") or "Uložit se nepodařilo.")
    return {"ok": True, "cesta": result.get("path", rel)}


# ── soubory a příkazy v prostoru ─────────────────────────────────────────────
def slozka(args):
    full = _in_home(args.get("cesta"))
    if not os.path.isdir(full):
        raise ToolError("Tohle není složka.")
    items = []
    with os.scandir(full) as it:
        for e in sorted(it, key=lambda e: e.name.lower()):
            if len(items) >= 1000:
                break
            try:
                is_dir = e.is_dir(follow_symlinks=False)
                size = 0 if is_dir else e.stat(follow_symlinks=False).st_size
            except OSError:
                continue
            items.append({"jmeno": e.name + ("/" if is_dir else ""), "velikost": size})
    return {"cesta": full, "polozky": items}


def soubor_cti(args):
    full = _in_home(args.get("cesta"))
    if not os.path.isfile(full):
        raise ToolError("Tohle není soubor.")
    size = os.path.getsize(full)
    with open(full, "rb") as fh:
        raw = fh.read(MAX_READ)
    if b"\0" in raw[:8192]:
        raise ToolError(f"Soubor je binární ({size} B) — přečíst jde jen text.")
    return {"cesta": full, "text": raw.decode("utf-8", "replace"),
            "zkraceno": size > MAX_READ}


def soubor_zapis(args):
    text = args.get("text")
    if not isinstance(text, str):
        raise ToolError("Chybí text souboru.")
    data = text.encode("utf-8")
    if len(data) > MAX_WRITE:
        raise ToolError("Soubor je moc velký (víc než 2 MB).")
    full = _in_home(args.get("cesta"), must_exist=False)
    parent = os.path.dirname(full)
    # Rodič se kontroluje znovu až po založení — odkaz v cestě by jinak mohl
    # vést ven z domova.
    os.makedirs(parent, exist_ok=True)
    _in_home(parent)
    if os.path.islink(full):
        raise ToolError("Na místě souboru je odkaz — přes něj se nezapisuje.")
    if os.path.exists(full) and not args.get("prepsat"):
        return {"ok": False, "existuje": True,
                "zprava": "Soubor už existuje — pošli znovu s prepsat: true."}
    tmp = full + ".hub-tmp"
    with open(tmp, "wb") as fh:
        fh.write(data)
    os.replace(tmp, full)
    return {"ok": True, "cesta": full, "bajtu": len(data)}


def prikaz(args):
    cmd = str(args.get("prikaz") or "").strip()
    if not cmd:
        raise ToolError("Chybí příkaz.")
    cwd = _in_home(args.get("slozka") or ".")
    if not os.path.isdir(cwd):
        raise ToolError("Pracovní složka není složka.")
    try:
        timeout = max(1, min(int(args.get("limit_sekund") or CMD_TIMEOUT), CMD_TIMEOUT_MAX))
    except (TypeError, ValueError):
        timeout = CMD_TIMEOUT
    env = {k: v for k, v in os.environ.items() if not SECRET_ENV.search(k)}
    env["HUB_MCP"] = "1"
    try:
        proc = subprocess.run(["/bin/bash", "-lc", cmd], cwd=cwd, env=env,
                              stdin=subprocess.DEVNULL, capture_output=True,
                              timeout=timeout)
        code, out, err = proc.returncode, proc.stdout, proc.stderr
        timed_out = False
    except subprocess.TimeoutExpired as exc:
        code, out, err = -1, exc.stdout or b"", exc.stderr or b""
        timed_out = True

    def cut(b):
        text = b.decode("utf-8", "replace")
        return ("… (začátek vynechán)\n" + text[-MAX_OUTPUT:]) if len(text) > MAX_OUTPUT else text
    return {"kod": code, "vystup": cut(out), "chyby": cut(err),
            "vyprsel_cas": timed_out, "slozka": cwd}


# ── Sdílené relace (gateway/relace.py) ──────────────────────────────────────
# Brána už ověřila, že volající smí číst (nebo psát). Tady se jen čte přepis
# chatu majitele a píše do jeho terminálu.
MAX_SDILENA_ZPRAVA = 4000
_PRACUJE_MTIME_S = 5          # přepis se před chvílí měnil → Claude pracuje
_PRACUJE_STARE_S = 900        # rozepsané déle než čtvrt hodiny = spíš zaseklé / opuštěné


def _relace_session(cid):
    from . import server
    for s in list(server.HUB.sessions.values()):
        if not s.exited and (getattr(s, "chat_id", "") == cid or s.resume == cid):
            return s
    return None


def _relace_prepis(cid):
    s = _relace_session(cid)
    if s:
        try:
            path = core.transcript_for(s)
            if path and os.path.isfile(path):
                return path
        except Exception:
            pass
    return cteni.path_for(cid)


def _pracuje(path):
    """Claude v téhle konverzaci právě pracuje? Podle konce přepisu: poslední
    zpráva je od člověka nebo výsledek nástroje, nebo odpověď bez konce tahu."""
    import json
    import time
    try:
        age = time.time() - os.path.getmtime(path)
        if age < _PRACUJE_MTIME_S:
            return True
        if age > _PRACUJE_STARE_S:
            return False
        with open(path, "rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - 65536))
            tail = fh.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return False
    for line in reversed(tail):
        try:
            e = json.loads(line)
        except ValueError:
            continue
        t = e.get("type")
        if t not in ("user", "assistant") or e.get("isSidechain") or e.get("isMeta"):
            continue
        if t == "user":
            return True
        return (e.get("message") or {}).get("stop_reason") in (None, "tool_use")
    return False


def session_seznam(_args):
    """Otevřené taby s Claude Code v prostoru — pro Clauda z počítače (hub/pocitac.py,
    tools/server_mcp.py): číslo konverzace, název, složka a jestli Claude pracuje."""
    from . import server
    out = []
    for s in list(server.HUB.sessions.values()):
        if s.exited or not (s.kind == "project" or s.kind.startswith("slash:")):
            continue
        chat = getattr(s, "chat_id", "") or s.resume or ""
        path = ""
        try:
            path = core.transcript_for(s)
        except Exception:
            pass
        out.append({"chat": chat, "tab": s.id, "title": s.title, "path": s.path,
                    "working": bool(path and _pracuje(path))})
    return {"sessions": out}


def sdilet_cteni(args):
    cid = str(args.get("chat") or "")
    if not cteni.je_id(cid):
        raise ToolError("Chybí číslo chatu.")
    path = _relace_prepis(cid)
    if not path:
        raise ToolError("Chat v prostoru majitele už není.")
    try:
        start = max(0, int(args.get("from") or 0))
    except (TypeError, ValueError):
        start = 0
    data = cteni.read(path, start, tail=not start)
    data["pracuje"] = _pracuje(path)
    data["bezi"] = _relace_session(cid) is not None
    return data


def sdilet_otevrit(args):
    """Chat, který majitel zavřel (nebo mu prostor spal), se znovu otevře v jeho
    hubu — tak jako by ho otevřel ze seznamu konverzací. Claude naváže, kde
    skončil, a kolegové s právem psát do něj mohou psát."""
    cid = str(args.get("chat") or "")
    if not cteni.je_id(cid):
        raise ToolError("Chybí číslo chatu.")
    if _relace_session(cid):
        return {"bezi": True, "otevreno": False}
    path = cteni.path_for(cid)
    if not path:
        raise ToolError("Chat v prostoru majitele už není.")
    from . import prenos, server
    cwd = prenos._cwd_z_prepisu(path)
    if not cwd or not os.path.isdir(cwd):
        cwd = core.HOME
    title = " ".join(str(args.get("title") or "Sdílený chat").split())[:80]
    server.HUB.open("project", cwd, title, 120, 32, agent="claude", resume=cid)
    core.log(f"sdílení: chat {cid[:8]} znovu otevřen ({title})")
    return {"bezi": True, "otevreno": True}


def sdilet_poslat(args):
    import threading
    cid = str(args.get("chat") or "")
    if not cteni.je_id(cid):
        raise ToolError("Chybí číslo chatu.")
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", str(args.get("text") or "")).strip()
    if not text:
        raise ToolError("Zpráva je prázdná.")
    if len(text) > MAX_SDILENA_ZPRAVA:
        raise ToolError("Zpráva je moc dlouhá.")
    s = _relace_session(cid)
    if not s:
        raise ToolError("Majitel má chat zavřený — zpráva by nikam nedošla.")
    kdo = re.sub(r"[\r\n\[\]]", " ", str(args.get("kdo") or "")).strip()[:60]
    # Claude i majitel pak vidí, od koho zpráva je.
    body = (f"[{kdo}] " if kdo else "") + text
    data = ("\x1b[200~" + body + "\x1b[201~") if "\n" in body else body
    # Přes write(): uspaný tab (hub/uspavani.py) se probudí a zprávu dostane.
    s.write(data.encode("utf-8"))
    # Enter zvlášť a o chvíli později: slepený s textem by ho Claude Code přebral
    # jako součást vloženého textu (stejně to dělá bublina v composer.js).
    threading.Timer(0.25, lambda: None if s.exited else s.write(b"\r")).start()
    return {"odeslano": True}


def prenos_prijmout(args):
    """Kus balíčku s chatem z počítače (volá brána, hub/prenos.py). Kusy se
    skládají v domově; s posledním se chat rozbalí do složky projektů tohoto
    prostoru a ohlásí se k otevření. Zapisuje hub sám, takže soubory patří
    jemu a Claude Code do chatu může dál psát."""
    import base64
    import time
    from . import prenos
    cid = str(args.get("id") or "")
    if not prenos.SESSION_ID.fullmatch(cid):
        raise ToolError("Chybí číslo chatu.")
    try:
        part, parts = int(args.get("part")), int(args.get("parts"))
    except (TypeError, ValueError):
        raise ToolError("Chybí pořadí kusu.") from None
    if not (1 <= parts <= 8 and 1 <= part <= parts):
        raise ToolError("Neplatné pořadí kusu.")
    try:
        body = base64.b64decode(str(args.get("zip") or ""), validate=True)
    except ValueError:
        raise ToolError("Kus balíčku je poškozený.") from None
    tmp = os.path.join(core.CLAUDE_DIR, "prenos-tmp")
    os.makedirs(tmp, exist_ok=True)
    for name in os.listdir(tmp):                      # zapomenuté kusy po hodině pryč
        full = os.path.join(tmp, name)
        try:
            if time.time() - os.path.getmtime(full) > 3600:
                os.remove(full)
        except OSError:
            pass
    with open(os.path.join(tmp, f"{cid}.{part}"), "wb") as fh:
        fh.write(body)
    chunks = [os.path.join(tmp, f"{cid}.{i}") for i in range(1, parts + 1)]
    if not all(os.path.isfile(c) for c in chunks):
        return {"hotovo": False}
    data = b""
    for c in chunks:
        with open(c, "rb") as fh:
            data += fh.read()
    for c in chunks:
        try:
            os.remove(c)
        except OSError:
            pass
    try:
        meta = prenos.rozbal_na_disk(data, core.CLAUDE_DIR, core.HOME)
    except ValueError as exc:
        raise ToolError(str(exc)) from None
    if args.get("title"):
        meta["title"] = " ".join(str(args["title"]).split())[:120]
    meta["from"] = str(args.get("from") or "")[:80]
    prenos.pridej_cekajici(core.CLAUDE_DIR, meta)
    core.log(f"přenos: chat „{meta['title']}\" z počítače {meta['from']} je tu")
    return {"hotovo": True, "id": meta["id"], "title": meta["title"]}


TOOLS = {
    "projekty": projekty,
    "konverzace": konverzace,
    "konverzace_cti": konverzace_cti,
    "obsidian_seznam": obsidian_seznam,
    "obsidian_hledat": obsidian_hledat,
    "obsidian_cti": obsidian_cti,
    "obsidian_zapis_osobni": obsidian_zapis_osobni,
    "slozka": slozka,
    "soubor_cti": soubor_cti,
    "soubor_zapis": soubor_zapis,
    "prikaz": prikaz,
    "session_seznam": session_seznam,
    "sdilet_cteni": sdilet_cteni,
    "sdilet_poslat": sdilet_poslat,
    "sdilet_otevrit": sdilet_otevrit,
    "prenos_prijmout": prenos_prijmout,
}


def call(name, args):
    """{"ok": True, "result": …} nebo {"ok": False, "error": "…"}."""
    fn = TOOLS.get(name)
    if not fn:
        return {"ok": False, "error": f"Neznámý nástroj: {name}"}
    if not isinstance(args, dict):
        args = {}
    try:
        return {"ok": True, "result": fn(args)}
    except ToolError as exc:
        return {"ok": False, "error": str(exc)}
    except OSError as exc:
        return {"ok": False, "error": f"{exc.strerror or exc}"}
