"""Samostatná appka (desktop/, Electron) jako okno hubu místo Chromu.

Instalačky ji ke každému vydání přikládají pro Windows, Mac a Linux
(.github/workflows/instalacky.yml, job „okno"). Hub si ji po startu sám stáhne
do ~/.claude/appka a okno pak otevírá v ní (window.open_window): v liště je
Claude Code Hub místo Chromu, vlastní menu a Ctrl/Cmd+N otevře další okno.
Poprvé stažená appka hned převezme okno Chromu (claude-hub.py, _appka) — na
téže instanci, takže taby i rozdělaná práce Clauda běží dál.

Kdo ji nechce, vypne ji v hub-config.json („appka": false). Když se stáhnout
nedá (bez sítě, vydání ji ještě nemá, jiný procesor) nebo nenaběhne, jede se
dál v okně Chromu jako dřív. Na serveru (prostor brány) a bez displeje se
nestahuje vůbec.
"""
import json
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.request
import zipfile

from . import core

DIR = os.path.join(core.CLAUDE_DIR, "appka")
# Odkud se bere. Pevná jména příloh v posledním vydání; zkoušky (desktop.yml)
# sem dají místní složku se sestavenou appkou.
ZDROJ = os.environ.get("HUB_APPKA_ZDROJ") or \
    f"https://github.com/{core.REPO}/releases/latest/download"

_LOCK = threading.Lock()
SELHALA = False          # appka v tomhle běhu nenaběhla → okna zase v Chromu


def balik():
    """Jméno přílohy vydání pro tenhle stroj, nebo '' (appka pro něj není)."""
    stroj = platform.machine().lower()
    if core.IS_WINDOWS:
        return "Claude-Code-Hub-Okno-win-x64.zip" if stroj in ("amd64", "x86_64") else ""
    if core.IS_MAC:
        return "Claude-Code-Hub-Okno-mac-%s.zip" % ("arm64" if stroj == "arm64" else "x64")
    if sys.platform.startswith("linux") and stroj in ("x86_64", "amd64"):
        return "Claude-Code-Hub-Okno-linux-x64.tar.gz"
    return ""


def zapnuta():
    """Má se appka na tomhle stroji používat (a stahovat)?"""
    if SELHALA or core.CONFIG.get("appka") is False:
        return False
    if core.TEST_MODE or core.on_gateway():
        return False
    if sys.platform.startswith("linux") and not (
            os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        return False            # server bez displeje, SSH
    return bool(balik())


def _stav():
    """{"version", "slozka"} nainstalované appky (appka/verze.json), nebo {}."""
    try:
        with open(os.path.join(DIR, "verze.json"), encoding="utf-8") as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def spustitelny():
    """Cesta k nainstalované appce, nebo ''.

    Každá verze má vlastní složku (appka/v1.1.0/) a verze.json říká, která
    platí: na Windows nejde přepsat ani přejmenovat složku běžící appky, takže
    se nová verze rozbalí vedle a jen se přepne."""
    slozka = os.path.join(DIR, str(_stav().get("slozka") or "-"))
    # Od 2.92 se appka jmenuje Aladin; starší verze v appka/ mají ještě
    # původní název Claude Code Hub, tak se zkusí oba.
    if core.IS_MAC:
        kandidati = [os.path.join(slozka, f"{n}.app", "Contents", "MacOS", n)
                     for n in ("Aladin", "Claude Code Hub")]
    elif core.IS_WINDOWS:
        kandidati = [os.path.join(slozka, f"{n}.exe") for n in ("Aladin", "Claude Code Hub")]
    else:
        kandidati = [os.path.join(slozka, "claude-code-hub")]
    cesta = next((c for c in kandidati if os.path.isfile(c)), "")
    return cesta if os.path.isfile(cesta) else ""


def verze():
    return str(_stav().get("version") or "")


def _stahni(jmeno, cil):
    if os.path.isdir(ZDROJ):
        shutil.copyfile(os.path.join(ZDROJ, jmeno), cil)
        return
    req = urllib.request.Request(ZDROJ.rstrip("/") + "/" + jmeno,
                                 headers={"User-Agent": "claude-code-hub"})
    with urllib.request.urlopen(req, timeout=60) as r, open(cil, "wb") as out:
        shutil.copyfileobj(r, out, 1 << 20)


def nejnovejsi():
    """Verze appky v posledním vydání, nebo '' (vydání ji nemá, není síť)."""
    try:
        with tempfile.TemporaryDirectory() as tmp:
            cesta = os.path.join(tmp, "okno.json")
            _stahni("okno.json", cesta)
            with open(cesta, encoding="utf-8") as fh:
                return str(json.load(fh).get("version") or "")
    except Exception:
        return ""


def _rozbal(archiv, cil):
    if core.IS_MAC:
        # ditto, ne zipfile: appka pro Mac má v sobě odkazy (frameworky)
        # a podpis, které by zipfile rozbil.
        subprocess.run(["ditto", "-x", "-k", archiv, cil], check=True,
                       capture_output=True, timeout=600)
    elif archiv.endswith(".zip"):
        with zipfile.ZipFile(archiv) as z:
            z.extractall(cil)                # zipfile sám odmítne ../ a absolutní cesty
    else:
        with tarfile.open(archiv) as tf:
            try:
                tf.extractall(cil, filter="data")          # Python 3.12+
            except TypeError:
                koren = os.path.realpath(cil) + os.sep
                for m in tf.getmembers():
                    if not os.path.realpath(os.path.join(cil, m.name)).startswith(koren):
                        raise ValueError("archiv chtěl zapsat mimo složku")
                tf.extractall(cil)
    # tar.gz z electron-builderu má všechno v jedné složce — o patro výš.
    obsah = os.listdir(cil)
    if len(obsah) == 1 and not obsah[0].endswith(".app") and \
            os.path.isdir(os.path.join(cil, obsah[0])):
        vnitrek = os.path.join(cil, obsah[0])
        for n in os.listdir(vnitrek):
            shutil.move(os.path.join(vnitrek, n), cil)
        os.rmdir(vnitrek)


def nainstaluj(ver):
    """Stáhne appku do vlastní složky a přepne na ni. Vyhazuje při chybě."""
    jmeno = balik()
    os.makedirs(DIR, exist_ok=True)
    nazev = "v" + "".join(c for c in ver if c.isalnum() or c in ".-_")
    cil = os.path.join(DIR, nazev)
    rozbalovani = cil + ".nova"
    shutil.rmtree(rozbalovani, ignore_errors=True)
    os.makedirs(rozbalovani)
    try:
        with tempfile.TemporaryDirectory(dir=DIR) as tmp:
            archiv = os.path.join(tmp, jmeno)
            _stahni(jmeno, archiv)
            _rozbal(archiv, rozbalovani)
        shutil.rmtree(cil, ignore_errors=True)
        os.rename(rozbalovani, cil)
    finally:
        shutil.rmtree(rozbalovani, ignore_errors=True)
    # Přepnutí je jediný zápis souboru — rozbitá napůl stažená verze se tak
    # nikdy nestane tou platnou.
    tmp = os.path.join(DIR, "verze.json.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"version": ver, "slozka": nazev, "ts": int(time.time())}, fh)
    os.replace(tmp, os.path.join(DIR, "verze.json"))
    # Starší verze pryč; ta, ze které appka zrovna běží, zůstane do příště.
    for n in os.listdir(DIR):
        if n.startswith("v") and n != nazev and os.path.isdir(os.path.join(DIR, n)):
            shutil.rmtree(os.path.join(DIR, n), ignore_errors=True)


def zajisti():
    """Doinstaluje nebo aktualizuje appku z posledního vydání.

    Vrací 'nova' (poprvé), 'aktualizovana', nebo '' (nic se nedělo)."""
    if not zapnuta():
        return ""
    with _LOCK:
        ver = nejnovejsi()
        if not ver:
            return ""
        mela = verze() if spustitelny() else ""
        if mela == ver:
            return ""
        try:
            nainstaluj(ver)
        except Exception as exc:
            core.log(f"appka: instalace {ver} nevyšla ({exc})", "warn")
            return ""
        if not spustitelny():
            core.log(f"appka: v balíku {balik()} chybí spustitelný soubor", "warn")
            return ""
        core.log(f"appka: nainstalovaná verze {ver}")
        return "aktualizovana" if mela else "nova"


def spust(url):
    """Otevře okno appky k hubu na `url`. Vrací proces appky."""
    argv = [spustitelny(), f"--hub-url={url}"]
    # Appka si nese vlastní knihovny. Prostředí hubu z AppImage (APPIMAGE, …)
    # nebo cizí knihovny by ji zmátly; ELECTRON_RUN_AS_NODE by z ní udělal Node
    # bez okna.
    env = {k: v for k, v in os.environ.items() if k not in (
        "APPIMAGE", "APPDIR", "ARGV0", "OWD", "LD_LIBRARY_PATH", "LD_PRELOAD",
        "PYTHONHOME", "PYTHONPATH", "ELECTRON_RUN_AS_NODE", "ELECTRON_NO_ATTACH_CONSOLE")}
    kwargs = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
              "stderr": subprocess.DEVNULL, "close_fds": True, "cwd": core.HOME, "env": env}
    if core.IS_WINDOWS:
        kwargs["creationflags"] = 0x00000200          # CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    return subprocess.Popen(argv, **kwargs)
