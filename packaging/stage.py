"""
Připraví obsah instalačky ke stažení — pro všechny tři systémy stejně.

    python3 packaging/stage.py <cíl> <triple> [--pip balík …]

Do <cíl> dá:
    launcher.py    spouštěč (packaging/launcher.py)
    python/        přenosný Python z python-build-standalone pro <triple>
                   (x86_64-unknown-linux-gnu, x86_64-pc-windows-msvc,
                   aarch64-apple-darwin, x86_64-apple-darwin) + značka
                   HUB_RUNTIME s názvem sestavení (podle ní launcher pozná,
                   jestli už má kopii aktuální)
    hub/           zdroj hubu z aktuálního commitu (`git archive`)

`--pip` doinstaluje balíky do přibaleného Pythonu (Windows: pywinpty pro
terminál, truststore pro certifikáty z úložiště Windows).

Jen standardní knihovna; GITHUB_TOKEN (je-li) se použije proti limitu API.
"""
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import urllib.request

REPO_PBS = "astral-sh/python-build-standalone"
MINOR = "3.12"
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def _get(url, accept="application/vnd.github+json"):
    headers = {"User-Agent": "claude-code-hub-packaging", "Accept": accept}
    token = os.environ.get("GITHUB_TOKEN")
    if token and "api.github.com" in url:
        headers["Authorization"] = "Bearer " + token
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=120) as r:
        return r.read()


def python_asset(triple):
    """(název, URL) nejnovějšího sestavení CPythonu MINOR pro `triple`."""
    pattern = re.compile(r"^cpython-%s\.\d+\+\d+-%s-install_only\.tar\.gz$"
                         % (re.escape(MINOR), re.escape(triple)))
    release = json.loads(_get(f"https://api.github.com/repos/{REPO_PBS}/releases/latest"))
    for asset in release.get("assets", []):
        if pattern.match(asset["name"]):
            return asset["name"], asset["browser_download_url"]
    raise SystemExit(f"python-build-standalone nemá {MINOR} pro {triple} "
                     f"ve vydání {release.get('tag_name')}")


def fetch_python(dest, triple):
    name, url = python_asset(triple)
    print(f"stahuju {name}")
    data = _get(url, accept="application/octet-stream")
    with tarfile.open(fileobj=io.BytesIO(data)) as tf:
        tf.extractall(dest, filter="data")         # archiv má kořen python/
    with open(os.path.join(dest, "python", "HUB_RUNTIME"), "w", encoding="utf-8") as fh:
        fh.write(name + "\n")


def python_exe(dest):
    if os.name == "nt":
        return os.path.join(dest, "python", "python.exe")
    return os.path.join(dest, "python", "bin", "python3")


def stage_source(dest):
    """Zdroj hubu z HEAD — jen to, co je v gitu, bez pracovních souborů."""
    archive = subprocess.run(["git", "-C", ROOT, "archive", "--format=tar", "HEAD"],
                             check=True, capture_output=True).stdout
    target = os.path.join(dest, "hub")
    os.makedirs(target)
    with tarfile.open(fileobj=io.BytesIO(archive)) as tf:
        tf.extractall(target, filter="data")
    # Do instalačky nepatří to, co se na počítači nepouští.
    for sub in ("mobile", ".github", "legacy", ".playwright-mcp"):
        shutil.rmtree(os.path.join(target, sub), ignore_errors=True)


def main():
    args = sys.argv[1:]
    if len(args) < 2:
        raise SystemExit(__doc__)
    dest, triple = args[0], args[1]
    pips = args[args.index("--pip") + 1:] if "--pip" in args else []
    shutil.rmtree(dest, ignore_errors=True)
    os.makedirs(dest)
    shutil.copy2(os.path.join(HERE, "launcher.py"), dest)
    fetch_python(dest, triple)
    if pips:
        subprocess.run([python_exe(dest), "-m", "pip", "install", "--no-warn-script-location",
                        "--disable-pip-version-check", *pips], check=True)
    stage_source(dest)
    with open(os.path.join(dest, "hub", "hub", "__init__.py"), encoding="utf-8") as fh:
        version = re.search(r'__version__\s*=\s*"([^"]+)"', fh.read()).group(1)
    print(f"hotovo: {dest} (hub {version})")
    gh_out = os.environ.get("GITHUB_OUTPUT")
    if gh_out:
        with open(gh_out, "a", encoding="utf-8") as fh:
            fh.write(f"version={version}\n")


if __name__ == "__main__":
    main()
