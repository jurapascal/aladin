#!/usr/bin/env python3
"""
Spouštěč Playwright MCP pro vestavěný prohlížeč.

Zaregistrovaný jako MCP `playwright` místo holého `npx @playwright/mcp`. Než
pustí Playwright MCP, zajistí, že běží sdílený Chromium (hub/prohlizec.py), a
nasměruje MCP na jeho ladicí port (`--cdp-endpoint`). Tentýž prohlížeč pak vidí
člověk v okně v appce a může mu do toho sáhnout.

Funguje i bez spuštěného hubu: prohlížeč si spustí sám, stejný profil jako dřív
(`~/.claude/browser-profile`), takže se nic neztratí. Když se Chromium spustit
nepodaří, jede Playwright MCP po staru s vlastním prohlížečem — Claude bez
prohlížeče nezůstane.

    python3 tools/playwright_bridge.py            # spustí MCP (stdio)
    python3 tools/playwright_bridge.py --register # přepíše registraci v Claude Code
    python3 tools/playwright_bridge.py --install  # stáhne Chromium pro tuhle verzi MCP
    python3 tools/playwright_bridge.py --install --deps   # + systémové knihovny (Linux, root/sudo)

Instalačky (install.sh, install.ps1, gateway/install.sh) volají `--install`,
ať se prohlížeč stahuje přesně pro verzi, kterou most pouští — dřív stahovaly
pro @latest a most pak nový Chromium nenašel.
"""
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PW_MCP = "@playwright/mcp@0.0.83"   # pevná verze: @latest se při každém startu ptá npm a občas rozbije revizi prohlížeče
sys.path.insert(0, os.path.dirname(HERE))

from hub import prohlizec  # noqa: E402


def register():
    claude = shutil.which("claude")
    if not claude:
        print("claude nenalezen", file=sys.stderr)
        return 1
    subprocess.run([claude, "mcp", "remove", "playwright", "-s", "user"], capture_output=True)
    res = subprocess.run([claude, "mcp", "add", "playwright", "-s", "user", "--",
                          sys.executable or "python3", os.path.join(HERE, "playwright_bridge.py")])
    return res.returncode


def _pust(args, proxy):
    """Playwright MCP jako dítě (stdio dědí) a proxy ve vláknech tohohle
    procesu. Most skončí s ním a po sobě zavře karty své session."""
    import signal
    try:
        child = subprocess.Popen(args)
    except OSError as exc:
        print(f"playwright_bridge: {exc}", file=sys.stderr)
        proxy.stop()
        return 1

    def konec(*_):
        try:
            child.terminate()
        except OSError:
            pass
    for sig in (getattr(signal, "SIGTERM", None), getattr(signal, "SIGHUP", None)):
        if sig is not None:
            try:
                signal.signal(sig, konec)
            except (ValueError, OSError):
                pass
    try:
        while True:
            try:
                return child.wait()
            except KeyboardInterrupt:
                konec()
    finally:
        proxy.stop()


def _npx():
    return shutil.which("npx") or shutil.which("npx.cmd") or "npx"


def _verze_core():
    """Verze playwright-core, na které jede PW_MCP (kvůli install-deps)."""
    npm = shutil.which("npm") or shutil.which("npm.cmd")
    if not npm:
        return ""
    try:
        out = subprocess.run([npm, "view", PW_MCP, "dependencies", "--json"], capture_output=True,
                             text=True, timeout=60).stdout
        import json
        return (json.loads(out or "{}") or {}).get("playwright-core", "")
    except Exception:
        return ""


def install(deps=False):
    """Chromium pro PW_MCP do ~/.cache/ms-playwright (nebo PLAYWRIGHT_BROWSERS_PATH);
    s `deps` na Linuxu i systémové knihovny (nss, atk, cups, gbm, pango, fonty)
    — jako root rovnou, jinak přes `sudo -n`; bez práv vypíše příkaz."""
    npx = _npx()
    # Bez úklidu: Playwright jinak smaže „nepoužívané" verze — i tu, ze které
    # zrovna běží vestavěný prohlížeč (naměřeno: smazal chromium-1246).
    env = {**os.environ, "PLAYWRIGHT_SKIP_BROWSER_GC": "1"}
    rc = subprocess.call([npx, "-y", PW_MCP, "install-browser", "chromium"], env=env)
    if rc != 0:
        print("playwright_bridge: Chromium se nestáhl", file=sys.stderr)
        return rc
    if not (deps and sys.platform.startswith("linux")):
        return 0
    ver = _verze_core()
    cmd = [npx, "-y", f"playwright-core@{ver}" if ver else "playwright-core", "install-deps", "chromium"]
    if hasattr(os, "geteuid") and os.geteuid() != 0:
        if subprocess.call(["sudo", "-n", "true"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) != 0:
            print("KNIHOVNY: sudo " + " ".join(cmd), file=sys.stderr)
            return 3
        cmd = ["sudo", "-n", "env", "PATH=" + os.environ.get("PATH", "")] + cmd
    return subprocess.call(cmd)


def main():
    if "--register" in sys.argv:
        return register()
    if "--install" in sys.argv:
        return install(deps="--deps" in sys.argv)
    npx = shutil.which("npx") or "npx"
    profile = prohlizec.profile_dir()
    ok = False
    if prohlizec.povoleno(server=prohlizec.na_serveru()):
        ok, why = prohlizec.ensure()
        if not ok:
            print(f"playwright_bridge: {why} — jedu s vlastním prohlížečem", file=sys.stderr)
    if ok:
        # Každá session vidí jen své karty (hub/prohlizec_proxy.py); profil
        # a přihlášení zůstávají společné. HUB_TAB řekne hubu, ke kterému
        # tabu karty patří (Claude Code ho MCP serveru předá v prostředí),
        # id konverzace, která session karty po restartu appky převezme.
        from hub import prohlizec_proxy
        chat = os.environ.get("HUB_SESSION_ID") or os.environ.get("HUB_AGENT_RESUME") or ""
        try:
            proxy = prohlizec_proxy.Proxy(os.environ.get("HUB_TAB", ""), chat).start()
        except OSError as exc:
            print(f"playwright_bridge: proxy nejde spustit ({exc}) — připojuji se přímo", file=sys.stderr)
            proxy = None
        if proxy:
            return _pust([npx, PW_MCP, "--cdp-endpoint", proxy.endpoint], proxy)
        args = [npx, PW_MCP, "--cdp-endpoint", prohlizec.endpoint()]
    else:
        args = [npx, PW_MCP, "--browser", "chromium", "--user-data-dir", profile]
    if os.name == "nt":
        return subprocess.call(args)
    os.execvp(args[0], args)


if __name__ == "__main__":
    sys.exit(main() or 0)
