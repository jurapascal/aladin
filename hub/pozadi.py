"""
Běh na pozadí: appka nemusí skončit se zavřením okna a počítač nemusí usnout.

* `na_pozadi` — zavřené okno ukončí jen okno. Server s taby (a Claudem v nich)
  běží dál a když appku spustíš znovu, otevře se okno k téhle běžící instanci
  (zápis `~/.claude/hub-bezi.json`), ne druhá appka — dvě instance nad stejnými
  chaty by si přepisovaly konverzace.
* `tray` (výchozí zapnuto) — ikonka v oznamovací oblasti panelu (hub/tray.py):
  appka je vidět i se zavřeným oknem, klik okno otevře, v nabídce je Ukončit.
* `bez_spanku` — dokud appka běží, počítač neusne (ani při zavření víka, kde to
  systém dovolí). Linux: zámek `systemd-inhibit`, macOS: `caffeinate`,
  Windows: `SetThreadExecutionState`. Ručně vyvolaný spánek (nabídka, tlačítko)
  žádná aplikace zakázat nemůže.
"""
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.request

from . import core

IS_WINDOWS = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"
_zamek = {"proc": None, "vlakno": None, "stop": None}
_ikonka = {"proc": None, "url": ""}


def soubor():
    return os.path.join(core.CLAUDE_DIR, "hub-bezi.json")


def _instance_dir():
    """Each running instance leaves `<pid>.json` here (url with token, 0600).
    `hub-bezi.json` only knows the newest one; this lets a fresh start find
    and quit stray instances left over from a race or a hang."""
    return os.path.join(core.CLAUDE_DIR, "hub-instance")


def zapis(url):
    """Značka „tahle instance běží" — podle ní se druhé spuštění připojí k ní."""
    if core.TEST_MODE:
        return
    zaznam = {"pid": os.getpid(), "url": url, "ts": time.time()}
    for cil in (soubor(), os.path.join(_instance_dir(), f"{os.getpid()}.json")):
        try:
            os.makedirs(os.path.dirname(cil), mode=0o700, exist_ok=True)
            tmp = cil + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(zaznam, fh)
            os.chmod(tmp, 0o600)                  # v adrese je token
            os.replace(tmp, cil)
        except OSError:
            pass


def smaz():
    try:
        os.remove(os.path.join(_instance_dir(), f"{os.getpid()}.json"))
    except OSError:
        pass
    try:
        with open(soubor(), encoding="utf-8") as fh:
            if json.load(fh).get("pid") != os.getpid():
                return                            # patří jiné instanci
        os.remove(soubor())
    except (OSError, ValueError):
        pass


# ── jediná instance ───────────────────────────────────────────────────────────
_ZAMEK = {"fh": None}


def zamek(cekat=0.0):
    """Zámek „tohle je ta jediná appka na počítači" (soubor ~/.claude/hub.lock).

    Dřív se druhé spuštění řídilo jen podle `hub-bezi.json`: dvojklik na ikonu
    nebo spuštění během pomalého startu (zápis přijde až po startu serveru)
    pustil druhou instanci na jiném portu. Dvě appky nad stejnými chaty pak
    hlásily bráně stejný počítač — server viděl jednou jednu, jednou druhou,
    ukončení jedné odpojilo i tu druhou a v liště zůstávala appka, která nešla
    zavřít. Zámek drží jádro systému; po pádu procesu zmizí sám.

    `cekat` = kolik sekund zkoušet (restart: stará instance ještě doběhne).
    Vrací True, když je zámek náš."""
    if core.TEST_MODE or _ZAMEK["fh"] is not None:
        return True
    path = os.path.join(core.CLAUDE_DIR, "hub.lock")
    try:
        os.makedirs(core.CLAUDE_DIR, exist_ok=True)
        fh = open(path, "a+")
    except OSError:
        return True                               # bez zámku jako dřív, ať appka jede
    konec = time.time() + max(0.0, cekat)
    while True:
        try:
            if IS_WINDOWS:
                import msvcrt
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            _ZAMEK["fh"] = fh
            return True
        except OSError:
            if time.time() >= konec:
                fh.close()
                return False
            time.sleep(0.25)


def pockej_na_bezici(sekund=25.0):
    """Adresa instance, která drží zámek, jakmile začne odpovídat (nebo '')."""
    konec = time.time() + sekund
    while time.time() < konec:
        url = bezici_url()
        if url:
            return url
        time.sleep(0.5)
    return ""


def _pid_zije(pid):
    if pid <= 0 or pid == os.getpid():
        return False
    if IS_WINDOWS:
        try:
            import ctypes
            h = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)   # QUERY_LIMITED_INFORMATION
            if not h:
                return False
            code = ctypes.c_ulong()
            ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code))
            ctypes.windll.kernel32.CloseHandle(h)
            return code.value == 259                                     # STILL_ACTIVE
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def uklid_dalsi_instance():
    """Po startu s vlastním zámkem: jiné instance, které tu zůstaly (stará verze
    bez zámku, závod při spuštění), se požádají o řádné ukončení — uloží taby
    a skončí. Vrací počet ukončených."""
    if core.TEST_MODE:
        return 0
    try:
        jmena = os.listdir(_instance_dir())
    except OSError:
        return 0
    hotovo = 0
    for jmeno in jmena:
        if not jmeno.endswith(".json"):
            continue
        cesta = os.path.join(_instance_dir(), jmeno)
        try:
            with open(cesta, encoding="utf-8") as fh:
                d = json.load(fh)
            pid, url = int(d.get("pid") or 0), str(d.get("url") or "")
        except (OSError, ValueError, TypeError):
            pid, url = 0, ""
        if pid == os.getpid():
            continue
        if not _pid_zije(pid) or not url.startswith("http://127.0.0.1:"):
            try:
                os.remove(cesta)                  # po pádu zůstal starý zápis
            except OSError:
                pass
            continue
        try:
            import urllib.parse
            u = urllib.parse.urlsplit(url)
            t = (urllib.parse.parse_qs(u.query).get("t") or [""])[0]
            req = urllib.request.Request(
                f"http://{u.netloc}/api/quit?t={urllib.parse.quote(t)}", data=b"{}", method="POST",
                headers={"Content-Type": "application/json", "Origin": f"http://{u.netloc}"})
            urllib.request.urlopen(req, timeout=5).close()
            hotovo += 1
            core.log(f"start: ukončena další instance appky (pid {pid})", "warn")
        except Exception as exc:
            core.log(f"start: další instanci (pid {pid}) nejde ukončit: {exc}", "warn")
    return hotovo


def _zaznam():
    """(pid, url) z `hub-bezi.json` — instance, ke které se mají připojovat okna."""
    try:
        with open(soubor(), encoding="utf-8") as fh:
            d = json.load(fh)
        return int(d.get("pid") or 0), str(d.get("url") or "")
    except (OSError, ValueError, TypeError):
        return 0, ""


HLIDAC_KAZDYCH = 30        # s
HLIDAC_NAVIC = 2           # tolik kol po sobě bez okna a bez práce = končím


def hlidej_jedinou(hub, url, pracuje=None):
    """Jen jedna appka na počítači, i když se dvě přece jen rozběhly (appka
    pustila hub během restartu, stará instance se při ukončení zasekla).
    Platí ta, kterou zná `hub-bezi.json` (spustila se poslední a odpovídá);
    každá jiná přepne svá okna na ni, a jakmile nemá okno ani rozdělanou práci
    Clauda, skončí. Dřív zůstávaly obě a obě otevíraly okna (Adam Uher,
    6. 11. 2026: na bránu chodily dvě řady dotazů z jednoho počítače)."""
    if core.TEST_MODE or core.on_gateway():
        return
    navic = 0
    while True:
        time.sleep(HLIDAC_KAZDYCH)
        try:
            if _ZAMEK["fh"] is None:
                zamek()                           # stará instance mezitím skončila?
            pid, jeho = _zaznam()
            if pid == os.getpid():
                navic = 0
                continue
            if not pid or not _pid_zije(pid):
                zapis(url)                        # značka chybí / patří mrtvé instanci
                navic = 0
                continue
            if not bezici_url():
                navic = 0                         # ta druhá neodpovídá — zůstat
                continue
            if hub.clients > 0:
                hub.broadcast({"t": "okno-jdi", "url": jeho})
                core.log(f"jediná appka: okna přecházejí na instanci {pid}", "warn")
                navic = 0
                continue
            if pracuje and pracuje():
                navic = 0                         # Claude tu dopisuje — počkat
                continue
            navic += 1
            if navic < HLIDAC_NAVIC:
                continue
            core.log(f"jediná appka: běží i instance {pid} — tahle končí", "warn")
            konec = threading.Thread(target=lambda: hub.shutdown(keep=False), daemon=True)
            konec.start()
            konec.join(10)
            smaz()
            os._exit(0)
        except Exception as exc:                   # hlídač nesmí umřít
            core.log_error("jediná appka", exc)


def bezici_url():
    """Adresa už běžící instance (jen když opravdu odpovídá), nebo ''."""
    try:
        with open(soubor(), encoding="utf-8") as fh:
            d = json.load(fh)
        pid, url = int(d.get("pid")), str(d.get("url") or "")
    except (OSError, ValueError, TypeError):
        return ""
    if pid == os.getpid() or not url.startswith("http://127.0.0.1:"):
        return ""
    try:
        # Zaneprázdněná appka (desítky tabů, start po aktualizaci) odpovídá
        # i několik sekund — po 2 s se dřív pustila druhá instance.
        urllib.request.urlopen(url, timeout=6).close()
        return url
    except Exception:
        return ""                                  # po pádu zůstal starý zápis


# ── spánek ────────────────────────────────────────────────────────────────────
def spanek(zapnout):
    """Drží / pouští zámek proti spánku. Bezpečné volat opakovaně."""
    if zapnout == bool(_zamek["proc"] or _zamek["vlakno"]):
        return
    if not zapnout:
        p, ev = _zamek["proc"], _zamek["stop"]
        _zamek.update(proc=None, vlakno=None, stop=None)
        if p:
            try:
                p.terminate()
            except OSError:
                pass
        if ev:
            ev.set()
        core.log("spánek: zámek uvolněn")
        return
    if IS_WINDOWS:
        ev = threading.Event()

        def drz():
            import ctypes
            ES = 0x80000000 | 0x00000001 | 0x00000040      # CONTINUOUS | SYSTEM_REQUIRED | AWAYMODE
            while not ev.wait(30):
                ctypes.windll.kernel32.SetThreadExecutionState(ES)
            ctypes.windll.kernel32.SetThreadExecutionState(0x80000000)
        t = threading.Thread(target=drz, name="bez-spanku", daemon=True)
        t.start()
        _zamek.update(vlakno=t, stop=ev)
    elif IS_MAC:
        if shutil.which("caffeinate"):
            _zamek["proc"] = subprocess.Popen(["caffeinate", "-i", "-s", "-w", str(os.getpid())],
                                              stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    elif shutil.which("systemd-inhibit"):
        # `handle-lid-switch`: zavřené víko spánek nespustí (pokud to systém neřídí jinak).
        _zamek["proc"] = subprocess.Popen(
            ["systemd-inhibit", "--what=sleep:idle:handle-lid-switch", "--who=Aladin",
             "--why=Appka běží na pozadí", "--mode=block",
             # Drží, jen dokud žije hub (jako caffeinate -w na Macu). Se
             # `sleep infinity` zámek přežil pád hubu a spánek blokoval dál.
             "tail", f"--pid={os.getpid()}", "-f", "/dev/null"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    core.log("spánek: počítač nebude usínat, dokud appka běží")


# ── ikonka v liště ────────────────────────────────────────────────────────────
def ikonka_url(url):
    """Adresa, na kterou ikonka volá (jen u appky s oknem, ne u prostoru na serveru)."""
    _ikonka["url"] = url


def ikonka(zapnout):
    p = _ikonka["proc"]
    bezi = p is not None and p.poll() is None
    if zapnout and not bezi and _ikonka["url"]:
        from . import tray
        _ikonka["proc"] = tray.spust(_ikonka["url"])
        if _ikonka["proc"]:
            core.log("tray: ikonka v liště spuštěna")
    elif not zapnout and bezi:
        try:
            p.terminate()
        except OSError:
            pass
        _ikonka["proc"] = None


def ikonka_znovu():
    """Nový název nebo ikona appky → ikonka v liště se pustí znovu."""
    ikonka(False)
    ikonka(core.CONFIG.get("tray", True) is not False)


def sync():
    """Srovná běžící stav s nastavením (po startu a po změně nastavení)."""
    spanek(bool(core.CONFIG.get("bez_spanku")))
    ikonka(core.CONFIG.get("tray", True) is not False)


def konec():
    """Úplné ukončení appky (i z pozadí)."""
    spanek(False)
    ikonka(False)
    smaz()
    threading.Timer(0.4, lambda: os._exit(0)).start()
