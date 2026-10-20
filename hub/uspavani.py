"""Uspávání nečinných tabů — ať prostor nedrží paměť za chaty, na které se nikdo nedívá.

Každý tab s Claudem je jeden proces `claude` (~300 MB) a k němu MCP servery
(~25 MB každý). Na serveru jich člověk mívá otevřených pět až sedm, většina
z nich hodiny jen stojí. Tenhle modul takový tab po čase bez práce „uspí":
proces ukončí a konverzaci si zapamatuje. Jakmile člověk tab otevře nebo do
něj napíše (i přes bublinu, most z počítače nebo MCP), hub pustí Clauda znovu
s `--resume` téže konverzace (server.Session.probud) a co mezitím přišlo,
doručí, až Claude naběhne.

Uspává se jen tab, u kterého to nic nerozbije:
  - běží v něm Claude Code (ne jiný agent, ne holý shell po /exit),
  - nepracuje (přepis nekončí uprostřed tahu, nečeká na souhlas ani odpověď
    na otázku — to je v přepisu nástroj bez výsledku),
  - nemá úlohy na pozadí ani zprávy ve frontě,
  - není to tab s úkolem ze serveru (hub/pocitac.py) — ten má vlastní hooky,
  - dlouho do něj nikdo nepsal a nic nevypsal.

Jak dlouho: `uspat_taby_min` v hub-config.json (nebo HUB_USPAT_TABY_MIN).
Výchozí je 30 minut v prostoru na serveru a vypnuto na počítači. Když je
prostor blízko stropu paměti, uspává se už po pár minutách klidu.
"""
import os
import time

from . import core

# Výchozí doba klidu v minutách: v prostoru na serveru / na počítači (0 = vypnuto).
VYCHOZI_SERVER = 30
VYCHOZI_POCITAC = 0
# Prostor nad tímhle podílem stropu (bez cache souborů) uspává dřív.
TLAK = 0.7
TLAK_MIN = 5
KAZDYCH = 60


def limit():
    """Kolik vteřin klidu stačí k uspání; 0 = vypnuto."""
    raw = os.environ.get("HUB_USPAT_TABY_MIN", "")
    if not raw:
        raw = core.CONFIG.get("uspat_taby_min", "")
    try:
        minut = float(raw) if raw not in ("", None) else None
    except (TypeError, ValueError):
        minut = None
    if minut is None:
        minut = VYCHOZI_SERVER if core.on_gateway() else VYCHOZI_POCITAC
    return max(0.0, minut) * 60


def podporovano():
    """Proces v tabu se pozná jen přes /proc (Linux) — jinde se neuspává."""
    return os.path.isdir("/proc/self") and not core.IS_WINDOWS


def _procesy_relace(sid):
    """[(pid, comm, rss_kB)] procesů v relaci `sid` (tab = vlastní relace)."""
    out = []
    try:
        pids = [p for p in os.listdir("/proc") if p.isdigit()]
    except OSError:
        return out
    for pid in pids:
        try:
            with open(f"/proc/{pid}/stat", encoding="utf-8", errors="replace") as fh:
                stat = fh.read()
            comm = stat[stat.index("(") + 1:stat.rindex(")")]
            pole = stat[stat.rindex(")") + 2:].split()
            if int(pole[3]) != sid:          # pole 6 ve stat = id relace
                continue
            rss = int(pole[21]) * (os.sysconf("SC_PAGE_SIZE") // 1024)
            out.append((int(pid), comm, rss))
        except (OSError, ValueError, IndexError):
            continue
    return out


def bezi_claude(session):
    """Běží v tabu doopravdy Claude Code? Po /exit z něj zbyde shell."""
    proc = getattr(session.pty, "proc", None)
    if proc is None or proc.poll() is not None:
        return False
    return any(comm == "claude" for _, comm, _ in _procesy_relace(proc.pid))


def pamet_tabu(session):
    """Kolik MB drží procesy tabu (Claude, MCP servery, shell)."""
    proc = getattr(session.pty, "proc", None)
    if proc is None:
        return 0
    return sum(rss for _, _, rss in _procesy_relace(proc.pid)) // 1024


def tlak():
    """Podíl stropu prostoru, který drží procesy (bez cache souborů); 0 = nevíme."""
    try:
        st = core.pamet_prostoru()
        if not st:
            return 0.0
        cur, mx, _ = st
        anon = core.pamet_anon()
        return (anon if anon else cur) / mx if mx else 0.0
    except Exception:
        return 0.0


def ma_konverzaci(path):
    """Má přepis aspoň jedno zadání od člověka? Prázdná session (tab otevřený,
    nic nenapsáno) se přes --resume obnovit nedá — Claude Code ji nenajde."""
    from . import restart
    import json
    try:
        with open(path, "rb") as fh:
            for line in fh:
                if b'"user"' not in line:
                    continue
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if restart._je_zadani(entry):
                    return True
    except OSError:
        pass
    return False


def muze_spat(session, klid):
    """(ano/ne, id konverzace k obnově nebo '')."""
    from . import cteni, restart
    if session.exited or getattr(session, "uspany", False):
        return False, ""
    if not (session.kind == "project" or session.kind.startswith("slash:")):
        return False, ""
    if (session.agent or core.default_agent()) != "claude":
        return False, ""
    if getattr(session, "ukol", None):
        return False, ""
    if time.time() - session.posledni < klid:
        return False, ""
    if not bezi_claude(session):
        return False, ""
    path = core.transcript_for(session)
    if not path or not os.path.isfile(path):
        # Nový tab bez jediné zprávy: Claude se po probuzení pustí znovu čistý.
        return True, ""
    od_ms = int(session.started * 1000)
    try:
        if restart._uprostred_prace(path, od_ms) or cteni.pozadi(path, od_ms):
            return False, ""
        fronta, _ = restart._fronta_a_prace(path, od_ms)
        if fronta:
            return False, ""
    except Exception as exc:
        core.log_error("uspávání: stav tabu", exc)
        return False, ""
    if not ma_konverzaci(path):
        return True, ""
    return True, os.path.basename(path)[:-6]


# Hlídač paměti jednoho Claudea: proces, který přeroste tenhle strop (MB),
# se v klidu uspí a tím vrátí paměť (po probuzení běží znovu od nuly, nad toutéž
# konverzací). `claude_rss_mb` v hub-config.json nebo HUB_CLAUDE_RSS_MB;
# 0 = vypnuto. Výchozí je 1536 MB v prostoru na serveru, na počítači vypnuto.
RSS_VYCHOZI = 1536
RSS_KLID = 120          # vteřin bez vstupu i výstupu, než se přerostlý tab uspí
TRIM_KAZDYCH = 300      # malloc_trim hubu každých pět minut


def rss_limit_mb():
    raw = os.environ.get("HUB_CLAUDE_RSS_MB", "")
    if not raw:
        raw = core.CONFIG.get("claude_rss_mb", "")
    try:
        mb = float(raw) if raw not in ("", None) else None
    except (TypeError, ValueError):
        mb = None
    if mb is None:
        mb = RSS_VYCHOZI if core.on_gateway() else 0
    return max(0.0, mb)


def rss_clauda(session):
    """Největší RSS (MB) procesu `claude` v tabu; 0 = nic neběží."""
    proc = getattr(session.pty, "proc", None)
    if proc is None or proc.poll() is not None:
        return 0
    return max((rss for _, comm, rss in _procesy_relace(proc.pid) if comm == "claude"),
               default=0) // 1024


def hlidej_rss(hub):
    """Uspí nečinné taby, jejichž Claude přerostl strop paměti. Stejná kritéria
    jako u uspávání po klidu (muze_spat): nepracuje, nečeká na souhlas ani otázku,
    nemá úlohy na pozadí."""
    strop = rss_limit_mb()
    if not strop:
        return
    for session in list(hub.sessions.values()):
        mb = rss_clauda(session)
        if mb < strop:
            continue
        ano, chat = muze_spat(session, RSS_KLID)
        if not ano:
            continue
        if session.uspi(chat):
            core.log(f"tab {session.id}: Claude držel {mb} MB (strop {int(strop)} MB), "
                     f"uspaný v klidu — uvolněno ~{mb} MB"
                     + (f" (konverzace {chat[:8]})" if chat else " (bez konverzace)"), "warn")
            hub.broadcast({"t": "tab-uspany", "id": session.id, "on": True})


def hlidej(hub, every=KAZDYCH):
    """Smyčka na pozadí: projde taby a nečinné uspí."""
    if not podporovano():
        return
    trim_at = time.time()
    while True:
        time.sleep(every)
        try:
            if time.time() - trim_at >= TRIM_KAZDYCH:
                trim_at = time.time()
                core.vrat_pamet_systemu()
        except Exception as exc:
            core.log_error("malloc_trim", exc)
        try:
            hlidej_rss(hub)
        except Exception as exc:
            core.log_error("hlídač paměti Claudea", exc)
        try:
            klid = limit()
            if not klid:
                continue
            if tlak() >= TLAK:
                klid = min(klid, TLAK_MIN * 60)
            for session in list(hub.sessions.values()):
                ano, chat = muze_spat(session, klid)
                if not ano:
                    continue
                mb = pamet_tabu(session)
                if session.uspi(chat):
                    core.log(f"tab {session.id}: uspaný po {int(klid // 60)} min klidu, "
                             f"uvolněno ~{mb} MB"
                             + (f" (konverzace {chat[:8]})" if chat else " (bez konverzace)"))
                    hub.broadcast({"t": "tab-uspany", "id": session.id, "on": True})
        except Exception as exc:
            core.log_error("uspávání tabů", exc)
