"""
Spouštěč instalaček ke stažení (Setup.exe, AppImage, .dmg).

Každá instalačka nese vedle sebe tři věci:

    <balík>/launcher.py   tenhle soubor
    <balík>/python/       přenosný Python (python-build-standalone)
    <balík>/hub/          zdroj hubu z vydané verze (obsah repa)

Po dvojkliku:

  1. Najde Python, na kterém má hub běžet napořád. Na Windows je to ten
     z balíku (Setup.exe ho nainstaluje na stálé místo). Na Linuxu systémový
     python3, když je — ten umí i nativní okno přes WebKitGTK; jinak kopie
     z balíku. Na Macu vždycky kopie z balíku do ~/.claude/runtime, protože
     /usr/bin/python3 tam je jen stub, který chce doinstalovat nástroje Xcode.
     AppImage i .dmg se připojují pokaždé jinam, proto kopie, ne odkaz.
  2. Když je hub už nainstalovaný ve stejné nebo novější verzi (aktualizoval
     se sám z appky), pustí ho a víc nic.
  3. Jinak nakopíruje zdroj do ~/.claude/hub-src a pustí hub odtamtud
     s `--setup`: okno ukáže instalaci s průběhem (hub/setup.py) a na konci
     se restartuje do nainstalované appky, kde naváže průvodce.

Jen standardní knihovna — běží na tom Pythonu, který si přinesl.
"""
import os
import re
import shutil
import subprocess
import sys

BUNDLE = os.path.dirname(os.path.abspath(__file__))
PAYLOAD = os.path.join(BUNDLE, "hub")
RUNTIME = os.path.join(BUNDLE, "python")
IS_WINDOWS = os.name == "nt"
IS_MAC = sys.platform == "darwin"

HOME = os.path.expanduser("~")
CLAUDE_DIR = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(HOME, ".claude")
SRC_DIR = os.path.join(CLAUDE_DIR, "hub-src")
STABLE_RUNTIME = os.path.join(CLAUDE_DIR, "runtime", "python")
MARK = "HUB_RUNTIME"          # značka přibaleného Pythonu (hub/core.py bundled_runtime)
LOG = os.path.join(CLAUDE_DIR, "hub-launcher.log")


def log(text):
    try:
        os.makedirs(CLAUDE_DIR, exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as fh:
            fh.write(text.rstrip() + "\n")
    except OSError:
        pass


def version_of(root):
    """Verze hubu ve složce (`hub/__init__.py`), jako n-tice; () když není."""
    try:
        with open(os.path.join(root, "hub", "__init__.py"), encoding="utf-8") as fh:
            m = re.search(r'__version__\s*=\s*"([^"]+)"', fh.read())
    except OSError:
        return ()
    if not m:
        return ()
    return tuple(int(p) if p.isdigit() else 0 for p in m.group(1).split("."))


def python_in(runtime):
    if IS_WINDOWS:
        return os.path.join(runtime, "python.exe")
    return os.path.join(runtime, "bin", "python3")


def read(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return ""


def system_python():
    """Systémový python3 ≥ 3.9 na Linuxu, nebo ''."""
    found = shutil.which("python3")
    if not found or os.path.realpath(found).startswith(os.path.realpath(BUNDLE)):
        return ""
    try:
        r = subprocess.run([found, "-c", "import sys; print(sys.version_info >= (3, 9))"],
                           capture_output=True, text=True, timeout=15)
    except Exception:
        return ""
    return found if r.stdout.strip() == "True" else ""


def stable_python():
    """Python, na kterém hub poběží napořád (viz docstring modulu, bod 1)."""
    if IS_WINDOWS:
        return python_in(RUNTIME)
    # HUB_PYTHON_BUNDLED=1 vynutí přibalený i na Linuxu (zkouška „stroj bez Pythonu").
    if not IS_MAC and os.environ.get("HUB_PYTHON_BUNDLED") != "1":
        found = system_python()
        if found:
            return found
    want = read(os.path.join(RUNTIME, MARK))
    have = read(os.path.join(STABLE_RUNTIME, MARK))
    target = python_in(STABLE_RUNTIME)
    if want and want == have and os.path.isfile(target):
        return target
    log(f"kopíruju Python do {STABLE_RUNTIME}")
    tmp = STABLE_RUNTIME + ".new"
    shutil.rmtree(tmp, ignore_errors=True)
    shutil.copytree(RUNTIME, tmp, symlinks=True)
    shutil.rmtree(STABLE_RUNTIME, ignore_errors=True)
    os.replace(tmp, STABLE_RUNTIME)
    return target


def sync_source():
    """Zdroj z balíku do ~/.claude/hub-src (odtud instaluje i aktualizace)."""
    tmp = SRC_DIR + ".new"
    shutil.rmtree(tmp, ignore_errors=True)
    shutil.copytree(PAYLOAD, tmp, symlinks=True)
    if os.path.isdir(SRC_DIR):
        old = SRC_DIR + ".old"
        shutil.rmtree(old, ignore_errors=True)
        try:
            os.replace(SRC_DIR, old)
        except OSError:
            # Složku drží jiný proces (běžící hub z předchozí instalace) — nový
            # zdroj se nakopíruje přes starý, ať v něm nic nechybí.
            log("hub-src nejde odsunout, kopíruji přes něj")
            shutil.copytree(tmp, SRC_DIR, dirs_exist_ok=True, symlinks=True)
            shutil.rmtree(tmp, ignore_errors=True)
            return
        shutil.rmtree(old, ignore_errors=True)
    os.replace(tmp, SRC_DIR)


def env_for(python):
    env = dict(os.environ)
    env["HUB_PYTHON"] = python
    extra = [os.path.dirname(python)]
    if not IS_WINDOWS:
        # Appka z Finderu / z nabídky dostane jen /usr/bin:/bin — instalačka by
        # nenašla git, node ani brew a hub ne Claude Code v ~/.local/bin.
        for d in (os.path.join(HOME, ".local", "bin"), "/opt/homebrew/bin",
                  "/usr/local/bin", "/usr/bin", "/bin"):
            if os.path.isdir(d):
                extra.append(d)
    env["PATH"] = os.pathsep.join(extra + [env.get("PATH", "")])
    # AppImage si nastavuje vlastní knihovny; hub a jeho taby je dědit nemají.
    for key in ("PYTHONHOME", "PYTHONPATH", "LD_LIBRARY_PATH"):
        env.pop(key, None)
    return env


def run(argv, env):
    log("spouštím: " + " ".join(argv))
    if IS_WINDOWS:
        # Odpojený proces: Setup.exe čeká jen na tenhle spouštěč, ne na hub.
        subprocess.Popen(argv, env=env, cwd=HOME,
                         creationflags=0x00000008 | 0x00000200, close_fds=True)
        return 0
    os.chdir(HOME)
    os.execve(argv[0], argv, env)
    return 0


def main():
    args = sys.argv[1:]
    try:
        python = stable_python()
    except Exception as exc:
        log(f"CHYBA: Python se nepodařilo připravit: {exc!r}")
        raise
    if IS_WINDOWS and os.path.isfile(os.path.join(os.path.dirname(python), "pythonw.exe")):
        window_python = os.path.join(os.path.dirname(python), "pythonw.exe")
    else:
        window_python = python
    env = env_for(python)

    installed = os.path.join(CLAUDE_DIR, "claude-hub.py")
    have = version_of(CLAUDE_DIR)
    ship = version_of(PAYLOAD)
    if os.path.isfile(installed) and have and have >= ship:
        return run([window_python, installed] + args, env)

    log(f"instalace: nainstalováno {have or '-'}, v balíku {ship}")
    sync_source()
    return run([window_python, os.path.join(SRC_DIR, "claude-hub.py"), "--setup"] + args, env)


if __name__ == "__main__":
    sys.exit(main())
