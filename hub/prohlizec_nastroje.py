"""
Lehký prohlížeč pro Clauda i v appce na počítači: Lightpanda a agent-browser.

Na serveru je instaluje gateway/install.sh jednou pro všechny prostory
(/usr/local/bin). Na počítači si je hub při startu stáhne sám na pozadí do
~/.claude/prohlizec-nastroje — nic se nebalí do instalaček (Lightpanda je
AGPL, spouští se jen jako samostatný program). Verze napevno, každý soubor
ověřený otiskem SHA-256 z vydání na GitHubu; nesedí → smaže se.

Lightpanda je jen pro Linux a Mac; na Windows je agent-browser s Chromiem.
Spouštěče (`lightpanda`, `agent-browser`, `agent-browser-lp`) leží v bin/
a hub tu složku dává na začátek PATH, takže je Claude v tabech najde.
"""
import hashlib
import os
import platform
import shutil
import sys
import threading
import urllib.request

from . import core

DIR = os.path.join(core.CLAUDE_DIR, "prohlizec-nastroje")
BIN = os.path.join(DIR, "bin")

AGENT_BROWSER_VERZE = "0.43.0"
AGENT_BROWSER = {    # platforma → (soubor ve vydání, sha256)
    "linux-x64": ("agent-browser-linux-x64",
                  "8e69c9fb662514c9cf36e4911e94e913742ba066e55102486350e049dd847612"),
    "linux-arm64": ("agent-browser-linux-arm64",
                    "34be55c2b92d8d199fb9d1d62a905ee5b6ac5b1f3e8c24e5b20597f370d905e5"),
    "darwin-arm64": ("agent-browser-darwin-arm64",
                     "05338a8eeddf0b3f9ebf0c87a6c21aedc10685608c68618e1a8483c43eae9c3f"),
    "darwin-x64": ("agent-browser-darwin-x64",
                   "49a5ab27297213c1bc0005701a6a4542399abfc9d2cb7520ab519ebb94b8b13d"),
    "win32-x64": ("agent-browser-win32-x64.exe",
                  "ebcef5ebea93c7ccf0fe3f8c18eee908ae9174dfef8e52a3dda6f24a59f630dc"),
}
LIGHTPANDA_VERZE = "1.0.5"
LIGHTPANDA = {
    "linux-x64": ("lightpanda-x86_64-linux",
                  "0ec21fa4867ab9e82b35285ff4ab0e7336c6c1d89f26fe991f27d73c8a678bd7"),
    "linux-arm64": ("lightpanda-aarch64-linux",
                    "7527391c479f323eb9111b834b012d91a89057528c28ef027741b21739b12299"),
    "darwin-arm64": ("lightpanda-aarch64-macos",
                     "eec3781086ee512320a04b036f092806a8a959944ea78fc8f4fe7dfe215a84b4"),
    "darwin-x64": ("lightpanda-x86_64-macos",
                   "2b75c9fee5c36d5d840f7938df6a532074bec03bf0485c3130b28597999a66fe"),
}
URL_AB = "https://github.com/vercel-labs/agent-browser/releases/download/v{v}/{f}"
URL_LP = "https://github.com/lightpanda-io/browser/releases/download/{v}/{f}"


def _platforma():
    stroj = platform.machine().lower()
    arch = "arm64" if stroj in ("arm64", "aarch64") else "x64" if stroj in ("x86_64", "amd64") else ""
    if not arch:
        return ""
    if sys.platform.startswith("linux"):
        return f"linux-{arch}"
    if sys.platform == "darwin":
        return f"darwin-{arch}"
    if core.IS_WINDOWS:
        return "win32-x64" if arch == "x64" else ""
    return ""


def _stahni(url, cil, sha):
    """Stáhne do `cil` přes dočasný soubor; vrátí True jen s odpovídajícím otiskem."""
    tmp = cil + ".tmp"
    h = hashlib.sha256()
    try:
        with urllib.request.urlopen(url, timeout=120) as res, open(tmp, "wb") as fh:
            while True:
                kus = res.read(1 << 16)
                if not kus:
                    break
                h.update(kus)
                fh.write(kus)
    except OSError as exc:
        core.log(f"prohlížeč: stažení {os.path.basename(cil)} selhalo ({exc})", "warn")
        _smaz(tmp)
        return False
    if h.hexdigest() != sha:
        core.log(f"prohlížeč: {os.path.basename(cil)} nesedí otisk — smazáno", "warn")
        _smaz(tmp)
        return False
    os.chmod(tmp, 0o755)
    os.replace(tmp, cil)
    return True


def _smaz(path):
    try:
        os.remove(path)
    except OSError:
        pass


def _verze_ok(name, verze):
    try:
        with open(os.path.join(DIR, name + ".verze"), encoding="utf-8") as fh:
            return fh.read().strip() == verze
    except OSError:
        return False


def _zapis_verzi(name, verze):
    with open(os.path.join(DIR, name + ".verze"), "w", encoding="utf-8") as fh:
        fh.write(verze + "\n")


def _chromium():
    """Chromium z Playwrightu (stahuje ho hub pro Playwright MCP): headless shell, jinak plný."""
    import glob
    roots = [os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or ""]
    if core.IS_WINDOWS:
        roots.append(os.path.join(os.environ.get("LOCALAPPDATA", ""), "ms-playwright"))
    elif sys.platform == "darwin":
        roots.append(os.path.expanduser("~/Library/Caches/ms-playwright"))
    else:
        roots.append(os.path.expanduser("~/.cache/ms-playwright"))
    vzory = ["chromium_headless_shell-*/chrome-headless-shell-*/chrome-headless-shell*",
             "chromium-*/chrome-linux*/chrome", "chromium-*/chrome-win*/chrome.exe",
             "chromium-*/chrome-mac*/*.app/Contents/MacOS/*"]
    for root in [r for r in roots if r and os.path.isdir(r)]:
        for vzor in vzory:
            hits = sorted(p for p in glob.glob(os.path.join(root, vzor)) if os.path.isfile(p))
            if hits:
                return hits[-1]
    return ""


def _spoustece(ab, lp):
    """Spouštěče v bin/. Chromium se hledá při každém startu hubu znovu."""
    os.makedirs(BIN, exist_ok=True)
    chrome = _chromium()
    if core.IS_WINDOWS:
        if ab:
            radky = ["@echo off", "set AGENT_BROWSER_NO_VERSION_CHECK=1",
                     'if not defined AGENT_BROWSER_SESSION set AGENT_BROWSER_SESSION=chromium']
            if chrome:
                radky.append(f'if not defined AGENT_BROWSER_EXECUTABLE_PATH set "AGENT_BROWSER_EXECUTABLE_PATH={chrome}"')
            radky.append(f'"{ab}" %*')
            with open(os.path.join(BIN, "agent-browser.cmd"), "w", encoding="utf-8") as fh:
                fh.write("\r\n".join(radky) + "\r\n")
        return

    def skript(name, telo):
        p = os.path.join(BIN, name)
        with open(p, "w", encoding="utf-8") as fh:
            fh.write("#!/bin/bash\n# Aladin (hub/prohlizec_nastroje.py) — vzniká při startu hubu.\n" + telo)
        os.chmod(p, 0o755)

    if lp:
        skript("lightpanda", f'export LIGHTPANDA_DISABLE_TELEMETRY=true\nexec "{lp}" "$@"\n')
    if ab:
        spolecne = ('export AGENT_BROWSER_NO_VERSION_CHECK=1\n'
                    'export AGENT_BROWSER_IDLE_TIMEOUT_MS="${AGENT_BROWSER_IDLE_TIMEOUT_MS:-300000}"\n')
        skript("agent-browser", spolecne +
               'export AGENT_BROWSER_SESSION="${AGENT_BROWSER_SESSION:-chromium}"\n' +
               (f'export AGENT_BROWSER_EXECUTABLE_PATH="${{AGENT_BROWSER_EXECUTABLE_PATH:-{chrome}}}"\n'
                if chrome else "") +
               f'exec "{ab}" "$@"\n')
        if lp:
            skript("agent-browser-lp", spolecne +
                   'export LIGHTPANDA_DISABLE_TELEMETRY=true\n'
                   'export AGENT_BROWSER_SESSION="${AGENT_BROWSER_SESSION:-lightpanda}"\n'
                   'export AGENT_BROWSER_ENGINE=lightpanda\n'
                   f'export AGENT_BROWSER_EXECUTABLE_PATH="{lp}"\n'
                   f'exec "{ab}" "$@"\n')


def _priprav():
    plat = _platforma()
    if not plat:
        return
    os.makedirs(DIR, exist_ok=True)
    ab = os.path.join(DIR, "agent-browser" + (".exe" if core.IS_WINDOWS else ""))
    if plat in AGENT_BROWSER and not (_verze_ok("agent-browser", AGENT_BROWSER_VERZE) and os.path.isfile(ab)):
        soubor, sha = AGENT_BROWSER[plat]
        if _stahni(URL_AB.format(v=AGENT_BROWSER_VERZE, f=soubor), ab, sha):
            _zapis_verzi("agent-browser", AGENT_BROWSER_VERZE)
            core.log(f"prohlížeč: agent-browser {AGENT_BROWSER_VERZE} připravený")
    lp = os.path.join(DIR, "lightpanda")
    if plat in LIGHTPANDA and not (_verze_ok("lightpanda", LIGHTPANDA_VERZE) and os.path.isfile(lp)):
        soubor, sha = LIGHTPANDA[plat]
        if _stahni(URL_LP.format(v=LIGHTPANDA_VERZE, f=soubor), lp, sha):
            _zapis_verzi("lightpanda", LIGHTPANDA_VERZE)
            core.log(f"prohlížeč: Lightpanda {LIGHTPANDA_VERZE} připravená")
    _spoustece(ab if os.path.isfile(ab) else "", lp if os.path.isfile(lp) else "")


def start():
    """Při startu hubu na počítači: bin/ do PATH a stažení na pozadí (poprvé ~100 MB)."""
    if core.TEST_MODE or core.on_gateway():
        return                       # server má nástroje centrálně (gateway/install.sh)
    cesta = os.environ.get("PATH", "")
    if BIN not in cesta.split(os.pathsep):
        os.environ["PATH"] = BIN + os.pathsep + cesta

    def bezpecne():
        try:
            _priprav()
        except Exception as exc:     # nic z toho nesmí shodit hub
            core.log_error("prohlížeč: příprava", exc)

    threading.Thread(target=bezpecne, name="prohlizec-nastroje", daemon=True).start()


def dostupne():
    """(agent-browser, lightpanda) — jsou na PATH (server i počítač)?"""
    return bool(shutil.which("agent-browser")), bool(shutil.which("agent-browser-lp"))
