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


def zapis(url):
    """Značka „tahle instance běží" — podle ní se druhé spuštění připojí k ní."""
    if core.TEST_MODE:
        return
    try:
        tmp = soubor() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"pid": os.getpid(), "url": url, "ts": time.time()}, fh)
        os.chmod(tmp, 0o600)                      # v adrese je token
        os.replace(tmp, soubor())
    except OSError:
        pass


def smaz():
    try:
        with open(soubor(), encoding="utf-8") as fh:
            if json.load(fh).get("pid") != os.getpid():
                return                            # patří jiné instanci
        os.remove(soubor())
    except (OSError, ValueError):
        pass


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
        urllib.request.urlopen(url, timeout=2).close()
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
             "--why=Appka běží na pozadí", "--mode=block", "sleep", "infinity"],
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
