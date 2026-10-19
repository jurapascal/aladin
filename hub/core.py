"""
Platform-agnostic core of the hub: config, project/memory scanning, palettes.

Everything that differs between Linux, macOS and Windows is isolated here so
the server and the UI never have to know which OS they are running on.

The Windows story in one line: we run the same bash scripts everywhere by
going through Git for Windows' bash.exe, so `claude-wrapper.sh` and every
bash-based slash command work unchanged on all three platforms.
"""
import datetime
import functools
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import uuid

from . import agents

IS_WINDOWS = os.name == "nt"
# Hub puštěný instalačkou ke stažení před instalací (hub/setup.py).
SETUP_MODE = "--setup" in sys.argv[1:]
IS_MAC = sys.platform == "darwin"



def _refresh_windows_path():
    """PATH z registru (systém + uživatel) navíc k tomu, co proces zdědil.

    Proces si PATH přečte jednou při startu. Když se mezitím něco doinstaluje
    (instalace v okně pustí winget — Claude Code, Git, gh), hub by to až do
    odhlášení z Windows „neviděl" a hlásil by, že žádný agent není. Naměřeno
    ve virtuálce s Windows 11: Claude Code ve složce WinGet/Links, hub bez agenta."""
    try:
        import winreg
    except ImportError:
        return
    parts = []
    for root, key in ((winreg.HKEY_LOCAL_MACHINE,
                       r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),
                      (winreg.HKEY_CURRENT_USER, "Environment")):
        try:
            with winreg.OpenKey(root, key) as k:
                value, _type = winreg.QueryValueEx(k, "Path")
            parts += [os.path.expandvars(p) for p in str(value).split(";") if p]
        except OSError:
            continue
    have = os.environ.get("PATH", "").split(os.pathsep)
    seen = {p.lower() for p in have}
    extra = [p for p in parts if p.lower() not in seen]
    if extra:
        os.environ["PATH"] = os.pathsep.join(have + extra)


if IS_WINDOWS:
    _refresh_windows_path()

HOME = os.path.expanduser("~")
CLAUDE_DIR = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(HOME, ".claude")
# Zkušební hub (`claude-hub.py --test`, HUB_TEST=1) vedle toho opravdového:
# jen okno na prohlížení. Neobnovuje ani neukládá taby (vzal by opravdovému
# hubu jeho uložené taby a pustil druhého Clauda na tytéž konverzace)
# a nepřipojuje se k bráně jako tenhle počítač.
TEST_MODE = os.environ.get("HUB_TEST") == "1"
# Okno drží jiná appka (desktop/, `claude-hub.py --vlastni-okno`). Restart po
# aktualizaci pak novou instanci pouští stejně a okna nezavírá — přepne je appka.
VLASTNI_OKNO = False
CONFIG_PATH = os.path.join(CLAUDE_DIR, "hub-config.json")

# Everything machine-specific lives in hub-config.json — see hub-config.example.json.
# Missing file or missing keys → these defaults, all of them optional at runtime.
# Výchozí klíč tlačítka Nahlásit chybu: každý hub hlásí do portálu Verxee Debug
# sám, bez nastavování. Klíč je veřejný, chrání ho seznam domén (127.0.0.1,
# localhost, test.alba-rosa.cz). Prázdný řetězec v hub-config.json tlačítko vypne.
VERXEE_KLIC_VYCHOZI = "vxd_acc213c5649c28723f438849"

DEFAULTS = {
    "project_dirs": ["~/Desktop", "~/Projects", "~/dev", "/opt/lampp/htdocs"],
    "brain_dir": "~/Obsidian/Claude-Brain",
    "icon": "~/.local/share/icons/claude-code.png",
    "ftp_deploy_script": "~/.claude/ftp-deploy.sh",
    "bash": "",     # Windows: path to Git for Windows bash.exe; empty = autodetect
    "browser": "",  # empty = autodetect an app-window capable browser
    # Čím okno otevřít: "chromium", "webkit", "browser". Prázdné = pořadí
    # z window.open_window. Totéž co `--window=`, jen se to pamatuje.
    "window": "",
    # Telefon přes Tailscale — dokud si to uživatel nezapne, žádný druhý
    # listener nevzniká a chování hubu je přesně jako dřív.
    "remote_enabled": False,
    "remote_port": 8760,
    "remote_token": "",       # dlouhodobý token spárovaného telefonu
    "remote_keep_running": False,  # nechat server běžet i po zavření okna
    "na_pozadi": False,       # po zavření okna běžet dál (hub/pozadi.py)
    "bez_spanku": False,      # dokud appka běží, počítač neusne
    "tray": True,             # ikonka v oznamovací oblasti panelu (hub/tray.py)
    # Vývojářský režim: nasazování a práce s GitHubem. Vypnuto znamená, že se
    # hub o nic z toho nepokouší — ani se nenabízí, ani nic neinstaluje.
    "dev_mode": False,
    # Účet na bráně (serverový hub). Heslo se neukládá — jen token zařízení,
    # který brána vydá při přihlášení a kdykoli zneplatní.
    "gw_server": "",
    "gw_token": "",
    "gw_user": None,
    # Že se má appka otevírat rovnou v prostoru na serveru (`gw_server`).
    # Zapíná se, až když přihlášení i předání okna vyšlo — sama adresa nic
    # nepřepíná, kdo si ji jen zkusil, o hub na počítači nepřijde.
    "server_mode": False,
    # Claude z prostoru na serveru na tomhle počítači (hub/pocitac.py):
    # "" vypnuto, "cteni" jen čtení, "vse" i zápis a příkazy. `pocitac_asked`
    # = člověk už volbu jednou viděl; `pocitac_id` rozlišuje stroje u brány.
    "pocitac_access": "",
    "pocitac_asked": False,
    "pocitac_id": "",
    # Claude v prostoru na vlastním předplatném (hub/predplatne.py): true = člověk
    # automatické propojení před vstupem do prostoru odmítl, už se samo neptá.
    "predplatne_skip": False,
    # Tlačítko Nahlásit chybu / Podat návrh (Verxee Debug, hub/static/verxee.js):
    # veřejný klíč `vxd_…` z https://alba-rosa.cz/debug/ → Klíče a domény.
    # Prázdný = tlačítko se neukáže a skript se vůbec nenačte. Na bráně ho
    # do prostorů píše správce (`claude-hub-admin verxee set`).
    "verxee_debug_klic": VERXEE_KLIC_VYCHOZI,
}

VERXEE_KLIC = re.compile(r"vxd_[a-f0-9]{24}")


def load_config():
    cfg = dict(DEFAULTS)
    raw = {}
    try:
        # utf-8-sig: PowerShell 5.1 rád píše BOM a json.load by na něm spadl
        with open(CONFIG_PATH, encoding="utf-8-sig") as fh:
            raw = json.load(fh)
        # `if v` tu drží prázdné hodnoty stranou, ať se nezahodí výchozí cesta
        # kvůli klíči, který v souboru zbyl prázdný. Na `false` to ale platit
        # nesmí: vypnutý přepínač se tím zahodil jako „nic" a příště se načetl
        # zase zapnutý — v nastavení to vypadalo, že se odkliknutí neudrželo.
        cfg.update({k: v for k, v in raw.items() if v or isinstance(v, bool)})
    except Exception:
        pass  # no config yet → defaults; the app must never fail to start on this
    # Vývojářský režim přibyl ve 2.1.0 a výchozí vypnuto platí jen pro nové
    # instalace, kterých se na něj zeptá průvodce. Kdo hub nastavený už má a
    # klíč v souboru nemá, ten Deploy, Push, Projekty, Taby, Paměť i Logy
    # celou dobu používal — aktualizace mu je schovat nesmí.
    # V prostoru na serveru (hub-config píše brána, `gateway_user`) ale platí
    # jednoduchý režim: členové týmu jsou většinou lidé, kteří s Claudem jen
    # píšou — terminál a GitHub si zapne, kdo je chce (Pro pokročilé).
    if "dev_mode" not in raw and raw.get("onboarded") and not raw.get("gateway_user"):
        cfg["dev_mode"] = True
    # 2.1.0 si adresu serveru z uvítání psala do `server_url`. Přihlášení teď
    # bydlí v `gw_*`, tak ať ji člověk nemusí psát znovu.
    if raw.get("server_url") and not raw.get("gw_server"):
        cfg["gw_server"] = raw["server_url"]
    return cfg


CONFIG = load_config()
WRAPPER = os.path.join(CLAUDE_DIR, "claude-wrapper.sh")
# Společný wrapper pro všechny agenty; claude-wrapper.sh je od 2.0.0 jen
# zkratka na něj a zůstává kvůli starším instalacím.
AGENT_WRAPPER = os.path.join(CLAUDE_DIR, "agent-wrapper.sh")
BRAIN = os.path.expanduser(CONFIG["brain_dir"])
MEMORY_DIR = os.path.join(BRAIN, "memory")
VAULT_NAME = os.path.basename(BRAIN.replace("\\", "/").rstrip("/"))  # obsidian:// URIs
ICON_PATH = os.path.expanduser(CONFIG["icon"])
FTP_DEPLOY = os.path.expanduser(CONFIG["ftp_deploy_script"])
SKILLS_DIR = os.path.join(CLAUDE_DIR, "skills")  # slash commands live here
# Where a pasted or dropped image is parked so the tab can hand Claude a path.
IMAGE_DIR = os.path.join(CLAUDE_DIR, "hub-images")
MAX_UPLOAD = 25 * 1024 * 1024
KEEP_IMAGES = 200
# Only keep folders that actually exist — a stock config lists several candidates.
PROJECT_DIRS = [p for p in (os.path.expanduser(d) for d in CONFIG["project_dirs"])
                if os.path.isdir(p)]
HAS_BRAIN = os.path.isdir(MEMORY_DIR)  # no vault → the whole memory UI stays hidden

# Hide the console window Windows would otherwise flash for every git call.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if IS_WINDOWS else 0


LOG_PATH = os.path.join(CLAUDE_DIR, "hub.log")
LOG_MAX = 1_000_000          # přes megabajt už se v tom nikdo nevyzná
LOG_KEEP = os.path.join(CLAUDE_DIR, "hub.log.1")
_LOG_LOCK = threading.Lock()


def log(message, level="info"):
    """Zapíše řádek do ~/.claude/hub.log.

    Zástupce na Windows běží přes pythonw.exe, který nemá konzoli — bez tohohle
    by po nepovedeném startu nezbylo vůbec nic ke čtení. Proto se sem logují
    i chyby z obsluhy požadavků a z běhů na pozadí, a proto se to dá vypsat
    přímo v aplikaci.

    Soubor se po megabajtu odloží stranou; drží se jeden předchozí, takže
    historie nezmizí, ale ani neroste donekonečna.
    """
    try:
        import datetime
        line = (f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S}  "
                f"{level.upper():<5} {message}\n")
        with _LOG_LOCK:
            try:
                if os.path.getsize(LOG_PATH) > LOG_MAX:
                    os.replace(LOG_PATH, LOG_KEEP)
            except OSError:
                pass
            os.makedirs(CLAUDE_DIR, exist_ok=True)
            with open(LOG_PATH, "a", encoding="utf-8") as fh:
                fh.write(line)
    except Exception:
        pass          # log, který shodí aplikaci, je horší než žádný


def log_error(message, exc=None):
    if exc is not None:
        message = f"{message}: {type(exc).__name__}: {exc}"
    log(message, "error")


def log_tail(lines=300):
    """Posledních `lines` řádků logu, i přes odložený soubor."""
    out = []
    for path in (LOG_KEEP, LOG_PATH):
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                out.extend(fh.read().splitlines())
        except OSError:
            continue
    return out[-lines:]


def log_clear():
    for path in (LOG_PATH, LOG_KEEP):
        try:
            os.remove(path)
        except OSError:
            pass
    log("log vymazán z aplikace")


def report_bundle():
    """Jeden text pro nahlášení problému: co je na stroji + poslední log.

    Cesty a jména projektů v tom být můžou — je to soubor pro člověka, ne pro
    odeslání někam ven, a aplikace ho nikam sama neposílá.
    """
    import datetime
    info = doctor()
    head = [
        "Paluba — hlášení",
        f"pořízeno   {datetime.datetime.now():%Y-%m-%d %H:%M:%S}",
        f"verze      {version()}",
        "",
        "Prostředí",
        "-" * 44,
    ]
    for key, value in info.items():
        head.append(f"  {key:<18} {value}")
    head += ["", "Log (posledních 300 řádků)", "-" * 44]
    return "\n".join(head + log_tail(300)) + "\n"


# ── Running helper programs ──────────────────────────────────────────────────
def run(argv, cwd=None, timeout=5):
    """Run argv and return stdout, or '' on any failure. Never raises."""
    try:
        r = subprocess.run(argv, capture_output=True, text=True, cwd=cwd,
                           timeout=timeout, creationflags=_NO_WINDOW)
        return r.stdout.strip()
    except Exception:
        return ""


def find_bash():
    """Absolute path to a bash we can run scripts with.

    On Windows this must be Git for Windows' bash.exe. `where bash` there often
    finds C:\\Windows\\System32\\bash.exe, which is the WSL launcher and not a
    shell we can drive through ConPTY — so System32 is explicitly skipped.
    """
    if CONFIG.get("bash") and os.path.isfile(os.path.expanduser(CONFIG["bash"])):
        return os.path.expanduser(CONFIG["bash"])
    if not IS_WINDOWS:
        return shutil.which("bash") or "/bin/bash"

    env_bash = os.environ.get("CLAUDE_CODE_GIT_BASH_PATH", "")
    candidates = [env_bash] if env_bash else []
    for base in (os.environ.get("ProgramFiles", r"C:\Program Files"),
                 os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
                 os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs")):
        if base:
            candidates.append(os.path.join(base, "Git", "bin", "bash.exe"))
    found = shutil.which("bash")
    if found and "system32" not in found.lower():  # System32\bash.exe = WSL
        candidates.append(found)
    for c in candidates:
        if c and os.path.isfile(c):
            return c
    return ""


def find_git():
    """git executable — falls back to the copy shipped with Git for Windows."""
    found = shutil.which("git")
    if found:
        return found
    bash = find_bash()
    if bash:  # <git>/bin/bash.exe → <git>/cmd/git.exe
        cand = os.path.join(os.path.dirname(os.path.dirname(bash)), "cmd", "git.exe")
        if os.path.isfile(cand):
            return cand
    return ""


BASH = find_bash()
GIT = find_git()


def to_shell_path(path):
    """Windows path → the /c/Users/... form Git Bash understands. No-op elsewhere."""
    if not IS_WINDOWS:
        return path
    p = os.path.abspath(path).replace("\\", "/")
    if len(p) > 1 and p[1] == ":":
        p = "/" + p[0].lower() + p[2:]
    return p


class BashMissing(RuntimeError):
    """No usable bash on this machine — on Windows that means Git for Windows."""


def bash_argv(script):
    """argv that runs a bash snippet, with the environment bash needs.

    -l gives Git Bash a usable PATH; CHERE_INVOKING keeps it in the directory we
    spawned it in instead of jumping to the home dir (Windows only, harmless
    elsewhere).
    """
    if not BASH:
        raise BashMissing(
            "Nenašel jsem bash — nainstaluj Git for Windows: winget install Git.Git"
            if IS_WINDOWS else "Nenašel jsem bash.")
    return [BASH, "-l", "-c", script]


def _installed_locales():
    """Locale names this machine actually has, normalised to lowercase .utf-8."""
    names = set()
    for line in run(["locale", "-a"], timeout=3).splitlines():
        names.add(line.strip().lower().replace(".utf8", ".utf-8"))
    return names


def utf8_locale(current, installed=None):
    """A UTF-8 locale name to use in place of `current`, or `current` if it is
    already UTF-8.

    A tab that starts under LANG=C truncates every accented character on its way
    through readline and the Claude Code TUI, and the app is launched from a
    desktop shortcut, which is exactly where a stripped environment comes from.
    We keep the language when there is one and it is actually generated here —
    naming a locale the machine does not have only earns a setlocale warning on
    every shell start.
    """
    low = (current or "").lower()
    if "utf-8" in low or "utf8" in low:
        return current
    if IS_WINDOWS:                       # Git Bash honours the name, not a locale DB
        return "en_US.UTF-8"
    if installed is None:
        installed = _installed_locales()
    lang = low.split(".")[0].split("@")[0]
    candidates = []
    if lang and lang not in ("c", "posix"):
        candidates.append(lang + ".utf-8")
    candidates += ["c.utf-8", "en_us.utf-8"]
    for cand in candidates:
        if cand in installed:
            # Give it back in the conventional casing: cs_CZ.UTF-8, C.UTF-8
            head, _, _ = cand.partition(".")
            return ("C" if head == "c" else
                    (head.split("_")[0] + "_" + head.split("_")[1].upper()
                     if "_" in head else head)) + ".UTF-8"
    return current or "C.UTF-8"


def child_env():
    env = dict(os.environ)
    env["TERM"] = "xterm-256color"
    env["COLORTERM"] = "truecolor"
    env["CHERE_INVOKING"] = "1"
    # Everything in a tab — bash, the Claude Code TUI, the hooks — has to agree
    # that the bytes on the wire are UTF-8, or diacritics arrive as mojibake.
    installed = None if IS_WINDOWS else _installed_locales()
    for var in ("LC_ALL", "LC_CTYPE", "LANG"):
        if env.get(var):
            env[var] = utf8_locale(env[var], installed)
    if not env.get("LANG") and not env.get("LC_ALL"):
        env["LANG"] = utf8_locale("", installed)
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("PYTHONUTF8", "1")
    env.pop("LINES", None)
    env.pop("COLUMNS", None)
    # Značky běžícího Claude Code. Když se hub spustí z jeho terminálu (nebo
    # ho odtamtud pustí sám Claude), zdědí je každý tab — a Claude Code v tabu
    # se pak tváří jako podsezení: vypne si ukládání přepisu („Transcript
    # saving is off") a napojí se na cizí sezení. Tab je samostatná konverzace,
    # tak do něj tyhle stopy nepatří; bez přepisu by navíc nebylo z čeho
    # skládat čtení (hub/cteni.py).
    for mark in ("CLAUDE_CODE_CHILD_SESSION", "CLAUDE_CODE_SESSION_ID",
                 "CLAUDE_CODE_SESSION_ATTENDED", "CLAUDE_CODE_MESSAGING_SOCKET",
                 "CLAUDE_CODE_MESSAGING_TOKEN", "CLAUDE_CODE_ENTRYPOINT",
                 "CLAUDE_PID"):
        env.pop(mark, None)
    return env


# ── Opening things in the desktop's default app ──────────────────────────────
# Odkazy z výpisu terminálu chodí sem, a výpis může pocházet z cizího repa.
# Systémový handler otevře leccos — `file://…​.desktop` se pod ním klidně spustí —
# takže se pouští jen schémata, u kterých je jasné, co udělají.
SAFE_SCHEMES = ("http://", "https://", "mailto:", "obsidian://")


def open_path(path):
    """Otevře složku, soubor nebo URI výchozí aplikací. False = neotevřeno."""
    path = str(path)
    if "://" in path.split("?", 1)[0] or path.startswith("mailto:"):
        if not path.startswith(SAFE_SCHEMES):
            log(f"open_path odmítl schéma: {path[:80]}")
            return False
    elif not os.path.exists(path):
        return False
    try:
        if IS_WINDOWS:
            if "://" in path or path.startswith("mailto:"):
                subprocess.Popen(["cmd", "/c", "start", "", path], shell=False,
                                 creationflags=_NO_WINDOW)
            else:
                os.startfile(path)  # noqa: S606 — the whole point of this function
        elif IS_MAC:
            subprocess.Popen(["open", "--", path])
        else:
            subprocess.Popen(["xdg-open", path],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception:
        return False


_HAS_OBSIDIAN = [0.0, None]


def _slouc(fn):
    """Souběžná volání se sloučí: kdo přijde, když výpočet už běží, počká na
    jeho výsledek místo druhého výpočtu. (Zahřívání při startu a první
    načtení stránky by jinak totéž počítaly dvakrát a navzájem se brzdily.)"""
    lock = threading.Lock()
    stav = {"ev": None, "res": None, "err": None}

    @functools.wraps(fn)
    def obal(*a, **k):
        if a or k:
            return fn(*a, **k)
        with lock:
            ev = stav["ev"]
            vedu = ev is None
            if vedu:
                ev = stav["ev"] = threading.Event()
        if not vedu:
            ev.wait()
            if stav["err"] is not None:
                raise stav["err"]
            return stav["res"]
        try:
            stav["res"], stav["err"] = fn(), None
            return stav["res"]
        except BaseException as exc:
            stav["err"] = exc
            raise
        finally:
            with lock:
                stav["ev"] = None
            ev.set()
    return obal


def has_obsidian():
    """True if an obsidian:// URL handler is registered on this machine.

    Cached for a few minutes — on Linux it's an `xdg-mime` call and the
    answer is needed on every page load."""
    now = time.monotonic()
    if _HAS_OBSIDIAN[1] is not None:
        if now - _HAS_OBSIDIAN[0] >= 300:
            # Stará odpověď platí, nová se zjistí na pozadí — stránka nečeká.
            _HAS_OBSIDIAN[0] = now
            threading.Thread(target=lambda: _HAS_OBSIDIAN.__setitem__(1, _has_obsidian()),
                             daemon=True).start()
        return _HAS_OBSIDIAN[1]
    _HAS_OBSIDIAN[0], _HAS_OBSIDIAN[1] = now, _has_obsidian()
    return _HAS_OBSIDIAN[1]


_OBSIDIAN_MIME = "x-scheme-handler/obsidian"


def _obsidian_v_souborech():
    """Obsluha obsidian:// podle mimeapps.list a .desktop souborů — totéž, co
    čte `xdg-mime`, jen bez spouštění shellového skriptu (ten trvá ~0,3 s)."""
    data = os.environ.get("XDG_DATA_HOME") or os.path.join(HOME, ".local", "share")
    conf = os.environ.get("XDG_CONFIG_HOME") or os.path.join(HOME, ".config")
    sys_data = (os.environ.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share").split(":")
    seznamy = [os.path.join(conf, "mimeapps.list"), os.path.join(data, "applications", "mimeapps.list")] + \
              [os.path.join(d, "applications", "mimeapps.list") for d in sys_data]
    for path in seznamy:
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if line.startswith(_OBSIDIAN_MIME + "=") and line.split("=", 1)[1].strip(" ;\n"):
                        return True
        except OSError:
            continue
    slozky = [os.path.join(data, "applications"), os.path.join(data, "flatpak", "exports", "share", "applications"),
              "/var/lib/flatpak/exports/share/applications"] + [os.path.join(d, "applications") for d in sys_data]
    for d in slozky:
        try:
            names = [n for n in os.listdir(d) if n.endswith(".desktop") and "obsidian" in n.lower()]
        except OSError:
            continue
        for n in names:
            try:
                with open(os.path.join(d, n), encoding="utf-8", errors="replace") as fh:
                    if _OBSIDIAN_MIME in fh.read():
                        return True
            except OSError:
                continue
    return False


@_slouc
def _has_obsidian():
    try:
        if IS_WINDOWS:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, "obsidian"):
                return True
        if IS_MAC:
            return os.path.isdir("/Applications/Obsidian.app")
        if _obsidian_v_souborech():
            return True
        return bool(run(["xdg-mime", "query", "default",
                         "x-scheme-handler/obsidian"], timeout=3))
    except Exception:
        return False


# ── Palettes (GitHub-style dark & light) ─────────────────────────────────────
# Single source of truth: the CSS reads these as custom properties and xterm.js
# reads the same values as its terminal theme.
DARK = {
    "AMBER": "#e0843c", "BG": "#0d1117", "BG_SIDEBAR": "#161b22",
    "BG_CARD": "#1b2129", "FG": "#d0d0d0", "FG_BRIGHT": "#e6edf3",
    "DIM": "#8b949e", "GREEN": "#3fb950", "RED": "#f85149",
    "CARD_HOVER": "#242b35", "BORDER": "#30363d", "SECTION": "#6e7681",
    # Kde se pracuje a nad čím: počítač si drží jantarovou, prostor na serveru
    # jede v modré jako cloud a firemní trezor ve fialové. Ať je na první pohled
    # vidět, kam se to, co tu napíšu, uloží.
    "CLOUD": "#4c97f0", "FIRMA": "#a371f7",
    # Krok pro grafy: prošel pásem světlosti i kontrastem na tmavém podkladu.
    # Druhý je pro prostor na serveru — sloupce tam nemají zůstat jantarové,
    # když je podle nich celé okno modré.
    "CHART": "#cf752e", "CHART_CLOUD": "#4c97f0",
    # 16-colour terminal palette (Afterglow)
    "TERM_PALETTE": [
        "#151515", "#ac4142", "#7e8e50", "#e5b567", "#6c99bb", "#9f4e85",
        "#7dd6cf", "#d0d0d0", "#505050", "#ac4142", "#7e8e50", "#e5b567",
        "#6c99bb", "#9f4e85", "#7dd6cf", "#f5f5f5",
    ],
}
LIGHT = {
    "AMBER": "#bc5c1c", "BG": "#ffffff", "BG_SIDEBAR": "#f6f8fa",
    "BG_CARD": "#ffffff", "FG": "#24292f", "FG_BRIGHT": "#1f2328",
    "DIM": "#656d76", "GREEN": "#1a7f37", "RED": "#cf222e",
    "CARD_HOVER": "#eef1f4", "BORDER": "#d0d7de", "SECTION": "#8c959f",
    "CLOUD": "#0969da", "FIRMA": "#8250df",
    "CHART": "#bc5c1c", "CHART_CLOUD": "#0969da",
    # 16-colour terminal palette (GitHub Light)
    "TERM_PALETTE": [
        "#24292e", "#cf222e", "#116329", "#953800", "#0969da", "#8250df",
        "#1b7c83", "#6e7781", "#57606a", "#a40e26", "#1a7f37", "#633c01",
        "#218bff", "#a475f9", "#3192aa", "#8c959f",
    ],
}


def hex_rgb(color):
    """#4c97f0 → "76;151;240" — barva pro escape sekvenci v terminálu."""
    h = str(color or "").lstrip("#")
    if len(h) != 6:
        return ""
    try:
        return ";".join(str(int(h[i:i + 2], 16)) for i in (0, 2, 4))
    except ValueError:
        return ""


# ── Data gathering ───────────────────────────────────────────────────────────
# Složky projektů, do kterých hub při posledním skenu nesměl (macOS bez
# povolení k Ploše či Dokumentům). Ukážou se na úvodní obrazovce (doctor).
BLOCKED_DIRS = []


@_slouc
def get_projects():
    projects, blocked = [], []
    for base in PROJECT_DIRS:
        if not os.path.isdir(base):
            continue
        try:
            names = sorted(os.listdir(base))
        except OSError as exc:
            # Jedna nečitelná složka nesmí shodit celé okno — na macOS takhle
            # dopadne Plocha, dokud aplikaci nepovolíš přístup k souborům.
            log(f"projekty: do {base} nejde nahlédnout — {exc}", "warn")
            blocked.append(base)
            continue
        for d in names:
            path = os.path.join(base, d)
            if not os.path.isdir(path):
                continue
            try:
                entries = os.listdir(path)
            except Exception:
                continue
            is_git = os.path.isdir(os.path.join(path, ".git"))
            has_php = any(f.endswith(".php") for f in entries
                          if os.path.isfile(os.path.join(path, f)))
            has_liquid = (os.path.isdir(os.path.join(path, "sections")) or
                          os.path.isdir(os.path.join(path, "templates")))
            has_pkg = os.path.isfile(os.path.join(path, "package.json"))
            has_composer = os.path.isfile(os.path.join(path, "composer.json"))
            if not (is_git or has_php or has_liquid or has_pkg or has_composer):
                continue
            if has_liquid:
                ptype = "Shopify"
            elif has_composer or has_php:
                ptype = "PHP"
            elif has_pkg:
                ptype = "Node"
            else:
                ptype = "Git"
            projects.append({
                "name": d, "path": path, "type": ptype,
                "branch": "", "dirty": 0, "git": is_git,
                "deployable": os.path.isfile(os.path.join(path, ".ftp-deploy.json")),
                "remote": "",
            })

    # Ručně přidané složky se skenem nenajdou — leží mimo nastavené cesty.
    known = {p["path"] for p in projects}
    for extra in CONFIG.get("extra_projects") or []:
        path = os.path.expanduser(extra)
        if not os.path.isdir(path) or path in known:
            continue
        projects.append({
            "name": os.path.basename(path.rstrip("/\\")) or path, "path": path,
            "type": "Složka", "branch": "", "dirty": 0, "manual": True,
            "git": os.path.isdir(os.path.join(path, ".git")),
            "deployable": os.path.isfile(os.path.join(path, ".ftp-deploy.json")),
        })

    # Git u všech projektů najednou: dřív šly tři příkazy gitu za každý projekt
    # jeden po druhém a s třiceti projekty se na seznam čekalo přes půl vteřiny.
    stavy = _git_stavy([p["path"] for p in projects if p.pop("git") and GIT])
    for proj in projects:
        branch, dirty_count, remote = stavy.get(proj["path"], ("", 0, ""))
        proj["branch"], proj["dirty"] = branch, dirty_count
        if proj.get("manual"):
            proj["type"] = "Git" if branch else "Složka"
        else:
            proj["remote"] = remote

    meta = load_projects()
    for proj in projects:
        # Kdy se na projektu naposledy dělalo. `.git` je nepoužitelné — dotkne
        # se ho každý `git status`, který si pro panel pouštíme sami, takže by
        # všechny projekty vycházely jako „právě teď". Bereme proto obsah
        # pracovní kopie, a jen z první úrovně: procházet celý strom by
        # u velkých repozitářů stálo víc, než kolik ten údaj vydá.
        newest = 0
        try:
            for name in os.listdir(proj["path"]):
                if name in (".git", "node_modules", "vendor", ".venv"):
                    continue
                try:
                    newest = max(newest, os.path.getmtime(
                        os.path.join(proj["path"], name)))
                except OSError:
                    pass
        except OSError:
            pass
        proj["mtime"] = int(newest)
        info = meta.get(os.path.abspath(proj["path"]), {})
        proj["label"] = info.get("label", "")
        proj["brief"] = info.get("brief", "")
        proj["group"] = info.get("group", "")
        proj["image"] = info.get("image", "")
        # Ručně zadané repo přebíjí to z gitu — někdo může chtít ukázat jinam.
        proj["repo"] = info.get("repo") or proj.get("remote", "")
        proj["archived"] = bool(info.get("archived"))
    projects.sort(key=lambda p: (p["archived"], (p["group"] or "").lower(),
                                 (p["label"] or p["name"]).lower()))
    BLOCKED_DIRS[:] = blocked
    return projects


_GIT_CACHE = {}               # cesta → (kdy, (větev, změněných souborů, owner/repo))
_GIT_CACHE_S = 3.0           # stav a projekty chce stránka při načtení zároveň
_GIT_LOCK = threading.Lock()


def _git_head(path):
    """Větev přímo z .git/HEAD — bez spouštění gitu. Odpojená HEAD = ''."""
    try:
        with open(os.path.join(path, ".git", "HEAD"), encoding="utf-8") as fh:
            head = fh.read().strip()
    except OSError:
        return None
    return head[len("ref: refs/heads/"):] if head.startswith("ref: refs/heads/") else ""


def _git_origin(path):
    """Adresa remote `origin` z .git/config, None když se nedá přečíst."""
    try:
        with open(os.path.join(path, ".git", "config"), encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError:
        return None
    if "insteadof" in text.lower():
        return None              # přepisy adres umí jen git sám
    section = ""
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("["):
            section = line
            continue
        if re.fullmatch(r'\[remote\s+"origin"\]', section) and line.split("=", 1)[0].strip().lower() == "url":
            return line.split("=", 1)[1].strip() if "=" in line else ""
    return ""


def _git_stav(path):
    branch = _git_head(path)
    if branch is None:
        branch = run([GIT, "branch", "--show-current"], cwd=path)
    status = run([GIT, "status", "--porcelain"], cwd=path)
    dirty = len(status.split("\n")) if status else 0
    origin = _git_origin(path)
    if origin is None:
        origin = run([GIT, "remote", "get-url", "origin"], cwd=path)
    return branch, dirty, github_slug(origin)


def _git_stavy(paths):
    """{cesta: (větev, změněných, owner/repo)} — `git status` paralelně."""
    from concurrent.futures import ThreadPoolExecutor
    now = time.monotonic()
    out, todo = {}, []
    with _GIT_LOCK:
        for path in paths:
            hit = _GIT_CACHE.get(path)
            if hit and now - hit[0] < _GIT_CACHE_S:
                out[path] = hit[1]
            else:
                todo.append(path)
    if todo:
        with ThreadPoolExecutor(max_workers=min(8, len(todo))) as pool:
            fresh = dict(zip(todo, pool.map(_git_stav, todo)))
        with _GIT_LOCK:
            for path, val in fresh.items():
                _GIT_CACHE[path] = (now, val)
        out.update(fresh)
    return out


def _note_title(path):
    """Human title for a memory note: frontmatter description, else # heading, else name."""
    try:
        desc = heading = ""
        in_fm = False
        with open(path, encoding="utf-8") as fh:
            for i, line in enumerate(fh):
                s = line.strip()
                if i == 0 and s == "---":
                    in_fm = True
                    continue
                if in_fm:
                    if s == "---":
                        in_fm = False
                    elif s.startswith("description:"):
                        desc = s.split(":", 1)[1].strip()
                elif s.startswith("# "):
                    heading = s[2:].strip()
                    break
        return desc or heading or os.path.basename(path)[:-3]
    except Exception:
        return os.path.basename(path)[:-3]


@_slouc
def get_memory():
    """(counts, recent) for the per-note memory vault; recent = newest 8 notes."""
    kinds = {"learnings": "learning_", "errors": "error_", "wins": "win_"}
    counts = {k: 0 for k in kinds}
    entries = []
    try:
        names = os.listdir(MEMORY_DIR)
    except Exception:
        names = []
    for fname in names:
        if not fname.endswith(".md"):
            continue
        for kind, prefix in kinds.items():
            if fname.startswith(prefix):
                counts[kind] += 1
                fpath = os.path.join(MEMORY_DIR, fname)
                try:
                    mtime = os.path.getmtime(fpath)
                except Exception:
                    mtime = 0
                entries.append((mtime, kind, _note_title(fpath), fname))
                break
    entries.sort(key=lambda x: x[0], reverse=True)  # newest first
    recent = [{"kind": k, "title": t, "file": f} for _, k, t, f in entries[:8]]
    return counts, recent


# ── náhled trezoru (vault.js) ────────────────────────────────────────────────
# Na serveru ani bez aplikace Obsidian se trezor nemá čím otevřít, tak ho hub
# ukáže sám. Jen ke čtení a jen uvnitř trezoru: cesta z prohlížeče se skládá
# po částech, `..`, absolutní cesty a skryté složky (.obsidian, .git) neprojdou.
VAULT_SKIP = {".obsidian", ".git", ".trash", ".smart-env", "node_modules"}
VAULT_IMAGES = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp", ".avif")
VAULT_MAX_NOTE = 4 * 1024 * 1024
VAULT_MAX_FILES = 20000
_VAULT_LINK = re.compile(r"\[\[([^\]|#\n]+)|\]\(<?([^)\s>]+?\.md)(?:#[^)\s]*)?>?\)")
# Štítek jako v Obsidianu: #jméno s aspoň jedním písmenem, ne nadpis („# Nadpis“).
_VAULT_TAG = re.compile(r"(?:^|[\s(\[])#([\w/-]*[^\W\d_][\w/-]*)", re.UNICODE)
_VAULT_LINKS = {}      # plná cesta → (mtime, {jména odkazů}, {štítky})


def company_vault():
    """Firemní Obsidian společný pro všechny účty na bráně (hub-config
    `company_vault`), nebo ''. V prostoru je jen ke čtení."""
    path = CONFIG.get("company_vault") or ""
    return os.path.abspath(os.path.expanduser(path)) if path else ""


_FIRMA_SLUG = re.compile(r"[a-z0-9][a-z0-9-]{0,39}")


def firmy_list():
    """Firmy, kde je uživatel členem (hub-config `firmy` od brány,
    gateway/firmy.py): [{zkratka, nazev, cesta, role, mazat, klienti}]. Dnešní
    firemní trezor je mezi nimi jako `firma`."""
    out = []
    for f in CONFIG.get("firmy") or []:
        if (isinstance(f, dict) and _FIRMA_SLUG.fullmatch(str(f.get("zkratka") or ""))
                and f.get("cesta")):
            out.append({"zkratka": f["zkratka"], "nazev": str(f.get("nazev") or f["zkratka"]),
                        "cesta": os.path.abspath(str(f["cesta"])),
                        "role": str(f.get("role") or "host"), "mazat": bool(f.get("mazat")),
                        "klienti": bool(f.get("klienti")),
                        # klienti (agentura: Klienti/klient/podkategorie) / projekty (Projekty/projekt)
                        "druh": "projekty" if f.get("druh") == "projekty" else "klienti"})
    return out


def firma_info(vault):
    """Firma trezoru `firma:<zkratka>` (dnešní `firma` taky), nebo None."""
    vault = str(vault or "")
    slug = "firma" if vault == "firma" else vault[len("firma:"):] if vault.startswith("firma:") else ""
    if not slug:
        return None
    return next((f for f in firmy_list() if f["zkratka"] == slug), None)


def firma_root(slug):
    """Kořen trezoru firmy `slug`, nebo '' (do firmy uživatel nepatří)."""
    f = firma_info("firma:" + str(slug or ""))
    return f["cesta"] if f else ""


# Pracovní složky chatů firem: ~/firmy/<zkratka>[/<klient>/<podkategorie>].
# Kód a soubory z práce pro firmu tak leží odděleně od osobních projektů i od
# jiné firmy — a podle složky chatu hub pozná, do které firmy chat patří.
FIRMY_PRACE = os.path.join(HOME, "firmy")


def firma_klienti(slug):
    """Klienti a podkategorie firmy, které prostor vidí: {druh, klienti:
    [{nazev, podkategorie}]}; u firmy s projekty jsou projekty jako klienti
    bez podkategorií. Čte se trezor, jak je v sandboxu — skryté tu vůbec nejsou."""
    f = firma_info("firma:" + str(slug or ""))
    root = f["cesta"] if f else ""
    projekty = bool(f) and f["druh"] == "projekty"
    base = os.path.join(root, "Projekty" if projekty else "Klienti") if root else ""

    def dirs(path):
        try:
            return sorted((n for n in os.listdir(path) if not n.startswith(".")
                           and os.path.isdir(os.path.join(path, n))),
                          key=lambda n: n.lower())
        except OSError:
            return []
    return {"druh": "projekty" if projekty else "klienti",
            "klienti": [{"nazev": k, "podkategorie": [] if projekty else dirs(os.path.join(base, k))}
                        for k in dirs(base)] if base else []}


def firma_prace(path, slug):
    """(klient, podkategorie), když je `path` pracovní složka chatu firmy
    (~/firmy/<zkratka>/<klient>/<podkategorie>); jinak (None, None)."""
    root = os.path.join(FIRMY_PRACE, slug)
    real = os.path.abspath(str(path or ""))
    if real != root and not real.startswith(root + os.sep):
        return None, None
    parts = [p for p in os.path.relpath(real, root).split(os.sep) if p not in ("", ".")]
    return (parts[0] if parts else None), (parts[1] if len(parts) > 1 else None)


def is_firma_vault(vault):
    """Je `vault` trezor firmy (dnešní `firma` i `firma:<zkratka>`)?"""
    vault = str(vault or "")
    return vault == "firma" or (vault.startswith("firma:") and bool(firma_root(vault[6:])))


def shared_state():
    """Sdílené Obsidiany, které má prostor svázané (hub-config od brány)."""
    out = []
    for v in CONFIG.get("shared_vaults") or []:
        if isinstance(v, dict) and v.get("slug") and v.get("path"):
            out.append({"slug": v["slug"], "name": v.get("name") or v["slug"],
                        "owner": v.get("owner", ""), "is_owner": bool(v.get("is_owner")),
                        "members": v.get("members") or []})
    return out


def company_links():
    """Osobní a sdílené složky ve firemním trezoru, které prostor má
    (Lidé/<jméno>, Sdílené/<název>; hub-config od brány, gateway/slozky.py)."""
    out = []
    for v in CONFIG.get("company_links") or []:
        if isinstance(v, dict) and v.get("path") and v.get("kind") in ("osobni", "sdilene"):
            out.append({k: v.get(k) for k in ("path", "kind", "slug", "name", "members")
                        if v.get(k) is not None})
    return out


def vault_route(rel, vault=""):
    """Kam doopravdy patří zápis na cestu `rel` trezoru `vault`.

    Ve firemním trezoru jsou `Lidé/<já>` a `Sdílené/<název>` odkazy na osobní
    a sdílený trezor — zápis tam jde rovnou do osobního (vault '') nebo jako
    návrh do sdíleného. Vrací (vault, cesta v něm, předpona pro odpověď)."""
    if vault != "firma":
        return vault, rel, ""
    path = str(rel or "").replace("\\", "/").strip().strip("/")
    for link in company_links():
        prefix = link["path"] + "/"
        if not path.startswith(prefix):
            continue
        if link["kind"] == "osobni":
            return "", path[len(prefix):], prefix
        if link.get("slug"):
            return "sdilene:" + link["slug"], path[len(prefix):], prefix
    return vault, rel, ""


def vault_prefixed(result, prefix):
    """Cesty v odpovědi zápisu zpátky do pohledu firemního trezoru."""
    if prefix and isinstance(result, dict):
        for key in ("path", "was"):
            if result.get(key):
                result[key] = prefix + result[key]
    return result


def _vault_root(vault=""):
    """Kořen trezoru: osobní (BRAIN), `firma` = firemní, `sdilene:<zkratka>` =
    sdílený Obsidian svázaný do prostoru ('' když takový není)."""
    if vault == "firma":
        return company_vault()
    if str(vault).startswith("firma:"):
        return firma_root(str(vault)[len("firma:"):])
    if str(vault).startswith("sdilene:"):
        slug = str(vault)[len("sdilene:"):]
        for v in CONFIG.get("shared_vaults") or []:
            if isinstance(v, dict) and v.get("slug") == slug and v.get("path"):
                return os.path.abspath(v["path"])
        return ""
    return os.path.abspath(os.path.expanduser(BRAIN))


def vault_path(rel, exts=(".md",), vault=""):
    """Plná cesta k souboru trezoru s danou příponou (None = jakoukoli), nebo ''."""
    root = _vault_root(vault)
    rel = str(rel or "").replace("\\", "/").strip()
    if not root or not rel or rel.startswith("/") or re.match(r"^[A-Za-z]:", rel):
        return ""
    parts = [p for p in rel.split("/") if p not in ("", ".")]
    if not parts or any(p == ".." or p.startswith(".") or p in VAULT_SKIP for p in parts):
        return ""
    # Cizí Lidé/… a Sdílené/… prostor nemá (sandbox je skryje); bez sandboxu
    # by odkaz vedl do cizího domova — tak ani nezkoušet.
    if (vault == "firma" and parts[0] in ("Lidé", "Sdílené")
            and "/".join(parts[:2]) not in {v["path"] for v in company_links()}):
        return ""
    full = os.path.join(root, *parts)
    if exts is not None and not full.lower().endswith(tuple(exts)):
        return ""
    if not os.path.isfile(full):
        return ""
    return full


def _walk_vault(vault=""):
    """(relativní cesta, plná cesta, mtime) souborů trezoru, bez skrytých složek."""
    root = _vault_root(vault)
    if not root:
        return
    seen = 0
    # Ve firemním trezoru se jde i do odkazů Lidé/<já> a Sdílené/<název>
    # (osobní a sdílené trezory); jinde odkazy dál nenásledujeme.
    links = {os.path.join(root, *v["path"].split("/")) for v in company_links()} \
        if vault == "firma" else set()
    for base, dirs, files in os.walk(root, followlinks=bool(links)):
        dirs[:] = sorted(d for d in dirs if not d.startswith(".") and d not in VAULT_SKIP
                         and (not os.path.islink(os.path.join(base, d))
                              or os.path.join(base, d) in links))
        for name in sorted(files):
            if name.startswith("."):
                continue
            full = os.path.join(base, name)
            try:
                mtime = os.path.getmtime(full)
            except OSError:
                continue
            yield os.path.relpath(full, root).replace(os.sep, "/"), full, mtime
            seen += 1
            if seen >= VAULT_MAX_FILES:
                return


def vault_tree(vault=""):
    """Poznámky a obrázky trezoru pro náhled."""
    notes, images, files = [], [], []
    for rel, full, mtime in _walk_vault(vault):
        low = rel.lower()
        if low.endswith(".md"):
            notes.append({"path": rel, "mtime": int(mtime)})
            continue
        if low.endswith(VAULT_IMAGES):
            images.append(rel)
        if low.endswith(".hub-tmp"):
            continue
        try:
            size = os.path.getsize(full)
        except OSError:
            continue
        files.append({"path": rel, "mtime": int(mtime), "size": size})
    root = _vault_root(vault)
    f = firma_info(vault) if str(vault).startswith("firma:") else None
    return {"name": f["nazev"] if f else os.path.basename(root.rstrip(os.sep)),
            "exists": bool(root) and os.path.isdir(root),
            "notes": notes, "images": images, "files": files,
            "slozky": company_links() if vault == "firma" else []}


def _note_meta(full, mtime):
    """(jména, na která poznámka odkazuje; její štítky). Podle mtime
    z mezipaměti — „Odkazuje sem" i graf prochází celý trezor."""
    cached = _VAULT_LINKS.get(full)
    if cached and cached[0] == mtime:
        return cached[1], cached[2]
    from urllib.parse import unquote
    try:
        with open(full, encoding="utf-8", errors="replace") as fh:
            text = fh.read(VAULT_MAX_NOTE)
    except OSError:
        text = ""
    names = set()
    for wiki, md in _VAULT_LINK.findall(text):
        target = (wiki or unquote(md)).strip().replace("\\", "/")
        target = re.sub(r"\.md$", "", target, flags=re.I)
        names.add(target.rsplit("/", 1)[-1].lower())
    tags = {t.lower() for t in _VAULT_TAG.findall(text)}
    _VAULT_LINKS[full] = (mtime, names, tags)
    return names, tags


def _note_links(full, mtime):
    """Jména poznámek, na která poznámka odkazuje ([[…]] i [..](….md))."""
    return _note_meta(full, mtime)[0]


def vault_note(rel, vault=""):
    """Poznámka pro náhled: text, kdy se měnila a které poznámky na ni odkazují."""
    full = vault_path(rel, vault=vault)
    if not full:
        return None
    try:
        size = os.path.getsize(full)
        mtime = os.path.getmtime(full)
        with open(full, encoding="utf-8", errors="replace") as fh:
            text = fh.read(VAULT_MAX_NOTE)
    except OSError:
        return None
    path = os.path.relpath(full, _vault_root(vault)).replace(os.sep, "/")
    name = os.path.splitext(os.path.basename(path))[0].lower()
    backlinks = [r for r, f, m in _walk_vault(vault)
                 if r.lower().endswith(".md") and f != full and name in _note_links(f, m)]
    return {"path": path, "text": text, "mtime": int(mtime),
            "truncated": size > VAULT_MAX_NOTE, "backlinks": backlinks[:300]}


def _fold(text):
    import unicodedata
    return "".join(c for c in unicodedata.normalize("NFD", str(text))
                   if not unicodedata.combining(c)).lower()


def vault_search(query, limit=60, vault=""):
    """Poznámky, které obsahují všechna slova (bez ohledu na diakritiku), s úryvkem."""
    words = _fold(query).split()
    if not words:
        return []
    out = []
    for rel, full, _mtime in _walk_vault(vault):
        if not rel.lower().endswith(".md"):
            continue
        try:
            with open(full, encoding="utf-8", errors="replace") as fh:
                text = fh.read(VAULT_MAX_NOTE)
        except OSError:
            continue
        folded = _fold(text)
        if not all(w in folded or w in _fold(rel) for w in words):
            continue
        at = folded.find(words[0])
        flat = " ".join(text.split())
        if at >= 0 and len(folded) == len(text):
            snippet = " ".join(text[max(0, at - 50):at + 90].split())
        else:
            snippet = flat[:140]
        out.append({"path": rel, "snippet": snippet})
        if len(out) >= limit:
            break
    return out


# ── zápis do trezoru a graf (vault.js) ───────────────────────────────────────
# Osobní trezor patří uživateli, do něj hub zapisuje rovnou. Firemní a sdílené
# jsou v prostoru jen ke čtení: z editoru vznikne stejný návrh jako od Clauda
# (~/.firma/ke-schvaleni/) a zapisuje je brána, až se karta v hubu potvrdí.
VAULT_MAX_SAVE = 2 * 1024 * 1024
VAULT_MAX_GRAPH_LINKS = 60000


def vault_target(rel, vault=""):
    """Plná cesta pro zápis poznámky (i takové, která ještě není), nebo ''."""
    root = _vault_root(vault)
    rel = str(rel or "").replace("\\", "/").strip()
    if not root or not rel or len(rel) > 300 or rel.startswith("/") or re.match(r"^[A-Za-z]:", rel):
        return ""
    parts = [p for p in rel.split("/") if p not in ("", ".")]
    if not parts or any(p == ".." or p.startswith(".") or p in VAULT_SKIP for p in parts):
        return ""
    if not parts[-1].lower().endswith(".md"):
        parts[-1] += ".md"
    full = os.path.join(root, *parts)
    if os.path.islink(full):
        return ""
    # Odkaz ve složce by pustil zápis ven z trezoru — porovnat skutečné cesty.
    real_root = os.path.realpath(root)
    at = os.path.dirname(full)
    while not os.path.isdir(at) and os.path.dirname(at) != at:
        at = os.path.dirname(at)
    try:
        if os.path.commonpath([os.path.realpath(at), real_root]) != real_root:
            return ""
    except ValueError:
        return ""
    return full


def vault_rel(full, vault=""):
    return os.path.relpath(full, _vault_root(vault)).replace(os.sep, "/")


def vault_save(rel, text, vault="", mtime=None):
    """Uloží poznámku do osobního trezoru. `mtime` = jak stará byla při otevření:
    když se mezitím změnila, nic se nepřepíše a vrátí se `conflict`."""
    if vault:
        return {"ok": False, "error": "Do tohohle Obsidianu zapisuje až brána po potvrzení."}
    text = str(text if text is not None else "")
    if len(text.encode("utf-8")) > VAULT_MAX_SAVE:
        return {"ok": False, "error": "Poznámka je moc velká (víc než 2 MB)."}
    full = vault_target(rel, vault)
    if not full:
        return {"ok": False, "error": "Tuhle cestu v trezoru uložit nejde."}
    exists = os.path.isfile(full)
    if exists and mtime is not None:
        try:
            if int(os.path.getmtime(full)) > int(mtime):
                return {"ok": False, "conflict": True, "path": vault_rel(full, vault),
                        "error": "Poznámka se mezitím změnila jinde."}
        except (OSError, TypeError, ValueError):
            pass
    try:
        os.makedirs(os.path.dirname(full), exist_ok=True)
        tmp = full + ".hub-tmp"
        with open(tmp, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
        os.replace(tmp, full)
        saved = int(os.path.getmtime(full))
    except OSError as exc:
        return {"ok": False, "error": f"Uložit se nepodařilo: {exc}"}
    _VAULT_LINKS.pop(full, None)
    return {"ok": True, "path": vault_rel(full, vault), "mtime": saved, "created": not exists}


def _relink_note(old_rel, new_rel):
    """Po přejmenování přepíše odkazy na poznámku v ostatních poznámkách.
    Vrací, kolika souborů se to týkalo — jako „aktualizovat odkazy" v Obsidianu."""
    old_base = os.path.splitext(os.path.basename(old_rel))[0]
    new_base = os.path.splitext(os.path.basename(new_rel))[0]
    old_noext = old_rel[:-3] if old_rel.lower().endswith(".md") else old_rel
    new_noext = new_rel[:-3] if new_rel.lower().endswith(".md") else new_rel
    from urllib.parse import quote, unquote
    # [[jméno]], [[cesta/jméno|popisek]], [odkaz](cesta/jméno.md)
    wiki = re.compile(r"\[\[([^\]\n|#]+)")
    mdlink = re.compile(r"\]\(<?([^)\s>]+?)(#[^)\s>]*)?>?\)")
    changed = 0
    for rel, full, _mtime in _walk_vault():
        if not rel.lower().endswith(".md"):
            continue
        try:
            with open(full, encoding="utf-8") as fh:
                text = fh.read(VAULT_MAX_NOTE)
        except (OSError, UnicodeDecodeError):
            continue

        def fix_wiki(m):
            target = m.group(1).strip()
            if target.lower() in (old_base.lower(), old_noext.lower()):
                return "[[" + (new_noext if "/" in target else new_base)
            return m.group(0)

        def fix_md(m):
            target = m.group(1)
            plain = unquote(target)
            if plain.lower() in (old_rel.lower(), old_noext.lower()):
                out = new_rel if plain.lower() == old_rel.lower() else new_noext
                return "](" + (quote(out) if "%" in target else out) + (m.group(2) or "") + ")"
            return m.group(0)

        fixed = mdlink.sub(fix_md, wiki.sub(fix_wiki, text))
        if fixed == text:
            continue
        try:
            with open(full, "w", encoding="utf-8", newline="") as fh:
                fh.write(fixed)
            _VAULT_LINKS.pop(full, None)
            changed += 1
        except OSError:
            continue
    return changed


def vault_rename(rel, new_rel, vault=""):
    """Přejmenuje (i přesune) poznámku v osobním trezoru a opraví odkazy na ni."""
    if vault:
        return {"ok": False, "error": "Přejmenovat jde jen v osobním Obsidianu."}
    src = vault_path(rel)
    if not src:
        return {"ok": False, "error": "Taková poznámka v trezoru není."}
    dst = vault_target(new_rel)
    if not dst:
        return {"ok": False, "error": "Nové jméno v trezoru uložit nejde."}
    if os.path.normcase(src) == os.path.normcase(dst):
        return {"ok": True, "path": vault_rel(src), "relinked": 0}
    if os.path.exists(dst):
        return {"ok": False, "error": "Poznámka s tímhle jménem už tu je."}
    old_path, new_path = vault_rel(src), vault_rel(dst)
    try:
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        os.rename(src, dst)
    except OSError as exc:
        return {"ok": False, "error": f"Přejmenovat se nepodařilo: {exc}"}
    _VAULT_LINKS.pop(src, None)
    return {"ok": True, "path": new_path, "was": old_path,
            "relinked": _relink_note(old_path, new_path)}


def vault_delete(rel, vault=""):
    """Odloží poznámku do koše trezoru (.trash) — smazat úplně jde v Obsidianu."""
    if vault:
        return {"ok": False, "error": "Mazat jde jen v osobním Obsidianu."}
    src = vault_path(rel)
    if not src:
        return {"ok": False, "error": "Taková poznámka v trezoru není."}
    trash = os.path.join(_vault_root(), ".trash")
    # Dvě smazání stejného jména v jedné vteřině by se v koši přepsala.
    first = time.strftime("%Y%m%d-%H%M%S") + "-" + os.path.basename(src)
    name, nth = first, 2
    while os.path.exists(os.path.join(trash, name)):
        stem, ext = os.path.splitext(first)
        name = f"{stem}-{nth}{ext}"
        nth += 1
    try:
        os.makedirs(trash, exist_ok=True)
        os.rename(src, os.path.join(trash, name))
    except OSError as exc:
        return {"ok": False, "error": f"Smazat se nepodařilo: {exc}"}
    _VAULT_LINKS.pop(src, None)
    return {"ok": True, "path": vault_rel(src), "trash": ".trash/" + name}


VAULT_UPLOAD_MAX = 20 * 1024 * 1024
_UPLOAD_BAD = re.compile(r"[\x00-\x1f\x7f/\\:*?\"<>|]")


def vault_upload(rel, raw, overwrite=False):
    """Soubor (jakýkoli) do osobního trezoru — z tlačítka Nahrát soubor.

    Jen osobní: do firemního a sdílených nahrává brána (gateway/soubory.py).
    Cesta se ověřuje po částech, cíl nesmí být odkaz ani mimo trezor, strop
    na soubor i na celý trezor (`vault_quota_mb`, výchozí 2 GB) — na serveru
    sdílí disk celý tým. Soubor se nespouští: 0644."""
    if not isinstance(raw, (bytes, bytearray)) or not raw:
        return {"ok": False, "error": "Soubor je prázdný."}
    if len(raw) > VAULT_UPLOAD_MAX:
        return {"ok": False, "error": f"Soubor je větší než {VAULT_UPLOAD_MAX // (1024 * 1024)} MB."}
    raw_rel = str(rel or "").replace("\\", "/")
    parts = [p.strip() for p in raw_rel.split("/") if p.strip() not in ("", ".")]
    if (not parts or raw_rel.startswith("/") or re.match(r"^[A-Za-z]:", raw_rel) or len(raw_rel) > 300
            or any(p == ".." or p.startswith(".") or _UPLOAD_BAD.search(p) or len(p) > 150
                   or p in VAULT_SKIP for p in parts)):
        return {"ok": False, "error": "Tohle jméno nebo složka v trezoru nejde."}
    root = _vault_root("")
    real_root = os.path.realpath(root)
    full = os.path.join(root, *parts)
    at = os.path.dirname(full)
    while not os.path.isdir(at) and os.path.dirname(at) != at:
        at = os.path.dirname(at)
    try:
        if os.path.commonpath([os.path.realpath(at), real_root]) != real_root:
            return {"ok": False, "error": "Tuhle cestu v trezoru uložit nejde."}
    except ValueError:
        return {"ok": False, "error": "Tuhle cestu v trezoru uložit nejde."}
    if os.path.islink(full) or (os.path.exists(full) and not os.path.isfile(full)):
        return {"ok": False, "error": "Na téhle cestě je něco, co přepsat nejde."}
    existed = os.path.exists(full)
    if existed and not overwrite:
        return {"ok": False, "exists": True, "path": vault_rel(full)}
    quota = int(CONFIG.get("vault_quota_mb") or 2048) * 1024 * 1024
    used = 0
    for _rel, f, _m in _walk_vault(""):
        try:
            used += os.path.getsize(f)
        except OSError:
            pass
    if used + len(raw) - (os.path.getsize(full) if existed else 0) > quota:
        return {"ok": False, "error": f"Trezor je plný (strop {quota // (1024 * 1024)} MB)."}
    try:
        os.makedirs(os.path.dirname(full), exist_ok=True)
        tmp = full + ".hub-tmp"
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o644)
        with os.fdopen(fd, "wb") as fh:
            fh.write(raw)
        os.replace(tmp, full)
    except OSError as exc:
        return {"ok": False, "error": f"Uložit se nepodařilo: {exc}"}
    return {"ok": True, "path": vault_rel(full), "overwritten": existed, "size": len(raw)}


def vault_proposal(rel, text, vault):
    """Návrh zápisu do firemního nebo sdíleného Obsidianu — stejný soubor, jaký
    píše Claude (tools/firma.py, tools/sdilene.py). Potvrzuje se kartou v hubu."""
    root = _vault_root(vault)
    if not root or not vault:
        return {"ok": False, "error": "Takový Obsidian tu není."}
    full = vault_target(rel, vault)
    if not full:
        return {"ok": False, "error": "Tuhle cestu v Obsidianu uložit nejde."}
    text = str(text if text is not None else "")
    if not text.strip():
        return {"ok": False, "error": "Poznámka je prázdná — nic se nenavrhlo."}
    if len(text.encode("utf-8")) > VAULT_MAX_SAVE:
        return {"ok": False, "error": "Poznámka je moc velká (víc než 2 MB)."}
    target = vault_rel(full, vault)
    who = (CONFIG.get("gateway_user") or {}).get("email", "")
    data = {"cil": target, "text": text, "autor": who,
            "vytvoreno": time.strftime("%Y-%m-%d %H:%M:%S")}
    if str(vault).startswith("firma:"):
        data["firma"] = vault[len("firma:"):]
    elif vault != "firma":
        slug = vault[len("sdilene:"):]
        info = next((v for v in shared_state() if v["slug"] == slug), None)
        if not info:
            return {"ok": False, "error": "Takový sdílený Obsidian tu není."}
        data.update({"druh": "sdilene-zapis", "vault": slug, "nazev": info["name"],
                     "lide": [m.get("name") or m.get("email", "") for m in info["members"]]})
    pid = time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(3)
    try:
        os.makedirs(FIRMA_PENDING, exist_ok=True)
        tmp = os.path.join(FIRMA_PENDING, pid + ".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False)
        os.replace(tmp, os.path.join(FIRMA_PENDING, pid + ".json"))
    except OSError as exc:
        return {"ok": False, "error": f"Návrh se nepodařilo připravit: {exc}"}
    return {"ok": True, "id": pid, "path": target,
            "prepise": os.path.isfile(full)}


def _graph_settings(vault=""):
    """Barevné skupiny a nastavení grafu z Obsidianu (.obsidian/graph.json)."""
    root = _vault_root(vault)
    if not root:
        return {}
    try:
        with open(os.path.join(root, ".obsidian", "graph.json"), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    groups = []
    for g in data.get("colorGroups") or []:
        if not isinstance(g, dict) or not g.get("query"):
            continue
        rgb = (g.get("color") or {}).get("rgb")
        groups.append({"query": str(g["query"])[:200],
                       "color": "#%06x" % (int(rgb) & 0xFFFFFF) if isinstance(rgb, int) else ""})
    out = {"groups": groups[:30]}
    for key in ("showTags", "showOrphans", "hideUnresolved", "showArrow",
                "nodeSizeMultiplier", "lineSizeMultiplier", "textFadeMultiplier",
                "centerStrength", "repelStrength", "linkStrength", "linkDistance"):
        if key in data and isinstance(data[key], (bool, int, float)):
            out[key] = data[key]
    return out


def vault_graph(vault=""):
    """Graf trezoru jako v Obsidianu: poznámky, jejich odkazy a štítky.

    Odkazy se párují po jménu souboru (kratší cesta vyhrává, jako v Obsidianu);
    na co odkaz nesedí, je „nenalezená" poznámka — Obsidian ji taky kreslí.
    """
    notes, tags_of = [], []
    for rel, full, mtime in _walk_vault(vault):
        if not rel.lower().endswith(".md"):
            continue
        links, tags = _note_meta(full, mtime)
        notes.append({"path": rel, "links": links})
        tags_of.append(sorted(tags)[:20])
    by_name = {}
    for i, n in enumerate(notes):
        key = n["path"].rsplit("/", 1)[-1][:-3].lower()
        if key not in by_name or len(n["path"]) < len(notes[by_name[key]]["path"]):
            by_name[key] = i
    nodes = [{"path": n["path"], "tags": t} for n, t in zip(notes, tags_of)]
    missing = {}
    links = []
    for i, n in enumerate(notes):
        for name in n["links"]:
            j = by_name.get(name)
            if j is None:
                if name not in missing:
                    missing[name] = len(nodes)
                    nodes.append({"path": name, "missing": True, "tags": []})
                j = missing[name]
            if j != i:
                links.append([i, j])
            if len(links) >= VAULT_MAX_GRAPH_LINKS:
                break
        if len(links) >= VAULT_MAX_GRAPH_LINKS:
            break
    root = _vault_root(vault)
    return {"name": os.path.basename(root.rstrip(os.sep)) if root else "",
            "nodes": nodes, "links": links, "settings": _graph_settings(vault)}


# ── návrhy do firemního Obsidianu ────────────────────────────────────────────
# Claude je připraví nástrojem tools/firma.py do ~/.firma/ke-schvaleni/. Hub je
# ukáže s náhledem a umí je zahodit; nahrává je brána po kliknutí na Nahrát
# (/gw/firma/publish) — do firemního trezoru prostor sám zapsat nemůže.
FIRMA_PENDING = os.path.join(HOME, ".firma", "ke-schvaleni")
_FIRMA_ID = re.compile(r"\d{8}-\d{6}-[0-9a-f]{6}")
# Co vypadá jako přihlašovací údaj: z firemního tabu se to nenahraje tiše.
_SECRET = re.compile(
    r"sk-ant-[A-Za-z0-9_-]{8,}|AIza[0-9A-Za-z_-]{20,}|gh[pousr]_[A-Za-z0-9]{20,}"
    r"|xox[baprs]-[A-Za-z0-9-]{10,}|-----BEGIN [A-Z ]*PRIVATE KEY-----"
    r"|(?i:heslo|password|passwd|api[ _-]?key|secret|token|refresh[ _-]?token)"
    r"\s*[:=]\s*\S{6,}")


def firma_state():
    vault = company_vault()
    # Přístup (none/read/write) a role nastavuje brána v hub-config — vault.js
    # podle nich schová úpravy a adminům ukáže správu přístupů.
    who = CONFIG.get("gateway_user") or {}
    return {"vault": vault, "name": os.path.basename(vault) if vault else "",
            "level": CONFIG.get("company_level") or ("write" if vault else "none"),
            "admin": isinstance(who, dict) and who.get("role") == "admin",
            # Firmy (gateway/firmy.py) — přepínač v okně a taby firem.
            "firmy": [{k: v for k, v in f.items() if k != "cesta"} for f in firmy_list()],
            "zakladat": bool(CONFIG.get("firmy_zakladat"))}


def firma_pending():
    """Návrhy čekající na potvrzení, nejstarší první."""
    if not company_vault() and not firmy_list():
        return []
    try:
        names = sorted(os.listdir(FIRMA_PENDING))
    except OSError:
        return []
    out = []
    for name in names:
        pid = name[:-5] if name.endswith(".json") else ""
        if not _FIRMA_ID.fullmatch(pid):
            continue
        path = os.path.join(FIRMA_PENDING, name)
        try:
            if os.path.islink(path) or os.path.getsize(path) > 1024 * 1024:
                continue
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        kind = str(data.get("druh") or "firma")
        writes = kind in ("firma", "sdilene-zapis")
        if writes and not isinstance(data.get("text"), str):
            continue
        rel = str(data.get("cil") or "")
        firma = str(data.get("firma") or "")
        where = (("firma:" + firma if firma else "firma") if kind == "firma"
                 else "sdilene:" + str(data.get("vault") or ""))
        texts = lambda key: [str(x) for x in data.get(key) or [] if isinstance(x, (str, int))][:50]
        # Z firemního tabu se nahrává bez karty — ale co vypadá jako heslo nebo
        # klíč, se tiše nenahraje: hub se na to zeptá kartou.
        auto = bool(data.get("auto")) and kind == "firma"
        citlive = auto and bool(_SECRET.search(str(data.get("text") or "")))
        out.append({"id": pid, "druh": kind, "auto": auto and not citlive, "citlive": citlive,
                    "firma": firma, "firma_nazev": (firma_info(where) or {}).get("nazev", ""),
                    "cil": rel, "text": data.get("text") or "",
                    "vault": str(data.get("vault") or ""), "nazev": str(data.get("nazev") or ""),
                    "lide": texts("lide"), "pridat": texts("pridat"), "odebrat": texts("odebrat"),
                    "autor": str(data.get("autor") or ""),
                    "vytvoreno": str(data.get("vytvoreno") or ""),
                    "prepise": bool(writes and vault_path(rel, vault=where))})
    return out


def firma_discard(pid):
    """Zahodí návrh. False = žádný takový není."""
    if not _FIRMA_ID.fullmatch(str(pid or "")):
        return False
    try:
        os.remove(os.path.join(FIRMA_PENDING, str(pid) + ".json"))
        return True
    except OSError:
        return False


_SAFE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg", ".avif",
             ".pdf", ".txt", ".md", ".csv", ".json", ".log"}


def save_upload(name, raw):
    """Park a pasted/dropped file on disk and return its path.

    The browser never tells us where a dropped file really lives, and a pasted
    screenshot has no path at all — so the only way to get one into a tab is to
    write our own copy and type that path at the prompt.
    """
    import datetime
    import re
    os.makedirs(IMAGE_DIR, exist_ok=True)
    stem, ext = os.path.splitext(os.path.basename(name or "obrazek.png"))
    ext = ext.lower() if ext.lower() in _SAFE_EXT else ".png"
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", stem).strip("-.")[:40] or "obrazek"
    path = os.path.join(
        IMAGE_DIR, f"{datetime.datetime.now():%Y%m%d-%H%M%S}-{stem}{ext}")
    n = 1
    while os.path.exists(path):
        path = path[:-len(ext)] + f"-{n}" + ext
        n += 1
    with open(path, "wb") as fh:
        fh.write(raw)
    _prune_images()
    return path


def _prune_images():
    """Keep the folder from growing forever — screenshots add up fast."""
    try:
        files = [os.path.join(IMAGE_DIR, n) for n in os.listdir(IMAGE_DIR)]
        files = [f for f in files if os.path.isfile(f)]
        for old in sorted(files, key=os.path.getmtime, reverse=True)[KEEP_IMAGES:]:
            os.remove(old)
    except Exception:
        pass


# ── Konfigurace za běhu ──────────────────────────────────────────────────────
def refresh():
    """Načte hub-config.json znovu a přepočítá, co z něj plyne.

    Onboarding mění cesty za běhu aplikace. Bez tohohle by se změna projevila
    až po restartu, protože odvozené hodnoty vznikají při importu.
    """
    global CONFIG, BRAIN, MEMORY_DIR, VAULT_NAME, ICON_PATH, FTP_DEPLOY
    global PROJECT_DIRS, HAS_BRAIN
    CONFIG = load_config()
    BRAIN = os.path.expanduser(CONFIG["brain_dir"])
    MEMORY_DIR = os.path.join(BRAIN, "memory")
    VAULT_NAME = os.path.basename(BRAIN.replace("\\", "/").rstrip("/"))
    ICON_PATH = os.path.expanduser(CONFIG["icon"])
    FTP_DEPLOY = os.path.expanduser(CONFIG["ftp_deploy_script"])
    PROJECT_DIRS = [p for p in (os.path.expanduser(d) for d in CONFIG["project_dirs"])
                    if os.path.isdir(p)]
    HAS_BRAIN = os.path.isdir(MEMORY_DIR)


def save_config(updates):
    """Zapíše změny do hub-config.json (jen předané klíče) a zavolá refresh()."""
    cfg = dict(load_config())
    cfg.update(updates)
    if SETUP_MODE:
        # Instalace v okně (hub/setup.py): konfig založí až instalačka — kdyby
        # existoval dřív, nechala by ho být a nezjistila složky projektů.
        return cfg
    # Nechceme do souboru vrátit výchozí hodnoty, které tam uživatel nemá —
    # zapisuje se to, co v něm bylo, plus změna.
    os.makedirs(CLAUDE_DIR, exist_ok=True)
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    os.replace(tmp, CONFIG_PATH)
    refresh()
    return cfg


def suggest_project_dirs():
    """Složky, kde na tomhle stroji nejspíš bydlí projekty."""
    if IS_WINDOWS:
        cands = [os.path.join(HOME, d) for d in
                 ("Desktop", "Documents", r"source\repos", "projects", "dev", "code")]
        cands += [r"C:\xampp\htdocs", r"C:\wamp64\www", r"C:\laragon\www"]
    else:
        cands = [os.path.join(HOME, d) for d in
                 ("Desktop", "Plocha", "Projects", "projects", "dev", "code",
                  "git", "src", "www")]
        cands += ["/opt/lampp/htdocs", "/var/www/html"]
    return [p for p in cands if os.path.isdir(p)]


# ── Projekty: štítky, briefing, archiv ──────────────────────────────────────
# Panel se plní skenem složek, ale co si o projektu myslí člověk, z disku
# vyčíst nejde. Drží se to vedle, klíčované cestou, aby se skenování a poznámky
# navzájem nepřepisovaly.
PROJECTS_PATH = os.path.join(CLAUDE_DIR, "hub-projects.json")

BRIEF_START = "<!-- hub:briefing -->"
BRIEF_END = "<!-- /hub:briefing -->"


def github_slug(url):
    """Z git adresy udělá `owner/repo`, nebo prázdno, když to není GitHub."""
    url = (url or "").strip()
    if not url:
        return ""
    m = re.search(r"github\.com[:/]+([^/]+)/(.+?)(?:\.git)?/?$", url)
    return f"{m.group(1)}/{m.group(2)}" if m else ""


def load_projects():
    try:
        with open(PROJECTS_PATH, encoding="utf-8-sig") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_projects(data):
    os.makedirs(CLAUDE_DIR, exist_ok=True)
    tmp = PROJECTS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    try:
        os.chmod(tmp, 0o600)      # briefingy bývají o klientech
    except OSError:
        pass
    os.replace(tmp, PROJECTS_PATH)


def project_meta(path):
    return load_projects().get(os.path.abspath(path), {})


def set_project_meta(path, updates):
    """Uloží štítek/briefing/archivaci k projektu. Prázdné hodnoty klíč smažou."""
    path = os.path.abspath(os.path.expanduser(path))
    data = load_projects()
    entry = dict(data.get(path, {}))
    for key, value in updates.items():
        if value in ("", None, False) and key != "archived":
            entry.pop(key, None)
        else:
            entry[key] = value
    if entry:
        data[path] = entry
    else:
        data.pop(path, None)
    save_projects(data)
    return entry


def write_briefing(path, text):
    """Vloží briefing do CLAUDE.md projektu, aby ho Claude Code sám přečetl.

    Do cizího obsahu se nesahá: blok je ohraničený značkami a při dalším uložení
    se jen vymění. Když CLAUDE.md ještě není, založí se.
    """
    path = os.path.abspath(os.path.expanduser(path))
    target = os.path.join(path, "CLAUDE.md")
    block = f"{BRIEF_START}\n## O projektu\n\n{text.strip()}\n{BRIEF_END}"
    try:
        existing = ""
        if os.path.isfile(target):
            with open(target, encoding="utf-8") as fh:
                existing = fh.read()
        if BRIEF_START in existing and BRIEF_END in existing:
            head = existing.split(BRIEF_START)[0]
            tail = existing.split(BRIEF_END, 1)[1]
            new = head + block + tail
        elif existing.strip():
            new = existing.rstrip() + "\n\n" + block + "\n"
        else:
            new = block + "\n"
        if not text.strip():           # briefing smazán → vyhodit i blok
            if BRIEF_START in existing and BRIEF_END in existing:
                new = (existing.split(BRIEF_START)[0].rstrip() + "\n" +
                       existing.split(BRIEF_END, 1)[1].lstrip())
                new = new.strip() + "\n" if new.strip() else ""
            else:
                return {"ok": True, "written": False}
        if new.strip():
            with open(target, "w", encoding="utf-8") as fh:
                fh.write(new)
        elif os.path.isfile(target):
            os.remove(target)          # zbyl by prázdný soubor
        return {"ok": True, "written": True, "file": target}
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:200]}


# ── Existující Obsidian paměť ────────────────────────────────────────────────
# Obsidian si seznam vaultů vede sám; je to nejspolehlivější zdroj, protože
# ví i o těch, které leží mimo obvyklé složky.
OBSIDIAN_CONFIGS = [
    "~/.config/obsidian/obsidian.json",                                  # nativní
    "~/.var/app/md.obsidian.Obsidian/config/obsidian/obsidian.json",     # flatpak
    "~/snap/obsidian/current/.config/obsidian/obsidian.json",            # snap
    "~/Library/Application Support/obsidian/obsidian.json",              # macOS
]


def _count_notes(folder):
    try:
        return len([n for n in os.listdir(folder) if n.endswith(".md")])
    except OSError:
        return 0          # není, nebo do ní macOS nepustí


def obsidian_vaults():
    """Vaulty, o kterých na tomhle stroji víme. Nejdřív ty od Obsidianu."""
    found, seen = [], set()

    def add(path, source):
        path = os.path.abspath(os.path.expanduser(path))
        if path in seen or not os.path.isdir(path):
            return
        seen.add(path)
        found.append({
            "path": path,
            "name": os.path.basename(path),
            "source": source,
            # Paměť hubu žije v podsložce memory/ — když tam je, je vault
            # rovnou použitelný; když ne, dá se založit.
            "notes": _count_notes(os.path.join(path, "memory")),
            "has_memory": os.path.isdir(os.path.join(path, "memory")),
        })

    configs = list(OBSIDIAN_CONFIGS)
    if IS_WINDOWS and os.environ.get("APPDATA"):
        configs.insert(0, os.path.join(os.environ["APPDATA"], "obsidian",
                                       "obsidian.json"))
    for cfg in configs:
        try:
            with open(os.path.expanduser(cfg), encoding="utf-8") as fh:
                data = json.load(fh)
            for entry in (data.get("vaults") or {}).values():
                if entry.get("path"):
                    add(entry["path"], "Obsidian")
        except Exception:
            continue

    # Záloha pro toho, kdo Obsidian nemá nainstalovaný, ale složku už má.
    import glob
    for pattern in ("~/Obsidian/*", "~/Documents/Obsidian/*", "~/obsidian/*",
                    "~/Nextcloud/*", "~/Dropbox/*", "~/OneDrive/*"):
        for path in sorted(glob.glob(os.path.expanduser(pattern))):
            if os.path.isdir(os.path.join(path, ".obsidian")) or \
                    os.path.isdir(os.path.join(path, "memory")):
                add(path, "na disku")
    return found


def memory_link_path():
    """Kam Claude Code ukládá vlastní paměť pro domovskou složku.

    Jméno složky je odvozené z cesty, ale hádat ho je krajní řešení — když už
    ji Claude Code jednou založil, použijeme tu jeho. Na Windows se navíc do
    jména promítá i disk, takže dohad by mohl sedět jen náhodou.
    """
    projects = os.path.join(CLAUDE_DIR, "projects")
    home_real = os.path.realpath(HOME)
    try:
        for name in os.listdir(projects):
            candidate = os.path.join(projects, name, "memory")
            # existující složka pro domovský adresář pozná podle jména
            plain = name.replace("-", "").lower()
            if plain and plain == home_real.replace(os.sep, "").replace(
                    "/", "").replace(":", "").lower():
                return candidate
    except OSError:
        pass
    slug = home_real.replace("\\", "/").replace(":", "").replace("/", "-")
    return os.path.join(projects, slug, "memory")


def _make_link(target, link):
    """Symlink, a na Windows křižovatka, když symlink nejde.

    Symlink na Windows chce buď práva správce, nebo zapnutý vývojářský režim —
    křižovatka (junction) nechce nic a pro složku dělá totéž.
    """
    try:
        os.symlink(target, link, target_is_directory=True)
        return "symlink"
    except (OSError, NotImplementedError, AttributeError):
        if not IS_WINDOWS:
            raise
    r = subprocess.run(["cmd", "/c", "mklink", "/J", link, target],
                       capture_output=True, text=True, creationflags=_NO_WINDOW)
    if r.returncode != 0:
        raise OSError((r.stderr or r.stdout or "mklink selhal").strip()[:200])
    return "junction"


def _is_link(path):
    """islink() na Windows křižovatku nepozná, ale readlink() ji přečte."""
    if os.path.islink(path):
        return True
    if IS_WINDOWS and os.path.isdir(path):
        try:
            os.readlink(path)
            return True
        except OSError:
            return False
    return False


def link_memory(vault):
    """Napojí paměť Claude Code na vault. Vrací popis toho, co se stalo.

    Samotný `brain_dir` v konfigu řídí jen hub — vlastní paměť si Claude Code
    hledá v `~/.claude/projects/<slug>/memory`. Teprve tenhle odkaz z toho
    udělá jedno a totéž, což je celý smysl „napojit existující paměť".
    """
    vault = os.path.abspath(os.path.expanduser(vault))
    memory = os.path.join(vault, "memory")
    os.makedirs(memory, exist_ok=True)
    index = os.path.join(memory, "MEMORY.md")
    if not os.path.isfile(index):
        with open(index, "w", encoding="utf-8") as fh:
            fh.write(EMPTY_MEMORY_INDEX)

    link = memory_link_path()
    os.makedirs(os.path.dirname(link), exist_ok=True)
    moved = ""
    if _is_link(link):
        if os.path.realpath(link) == os.path.realpath(memory):
            return {"linked": link, "target": memory, "moved": "", "how": "beze změny"}
        try:
            os.unlink(link)
        except OSError:
            os.rmdir(link)                 # křižovatka se ruší jako složka
    elif os.path.isdir(link):
        # Skutečná složka s poznámkami se nemaže — odsune se stranou.
        if os.listdir(link):
            import datetime
            moved = f"{link}-backup-{datetime.datetime.now():%Y%m%d-%H%M%S}"
            shutil.move(link, moved)
        else:
            os.rmdir(link)
    elif os.path.exists(link):
        os.unlink(link)
    how = _make_link(memory, link)
    return {"linked": link, "target": memory, "moved": moved, "how": how}


def clone_vault(repo, parent):
    """Naklonuje vault z gitu. repo = owner/repo nebo celá URL."""
    repo = _check_repo(repo)
    parent = os.path.abspath(os.path.expanduser(parent))
    name = os.path.basename(repo.rstrip("/")).replace(".git", "")
    if not name or name in (".", ".."):
        raise ValueError("Z adresy nejde odvodit jméno složky.")
    target = os.path.join(parent, name)
    if os.path.exists(target):
        raise ValueError(f"{target} už existuje.")
    if not GIT:
        raise ValueError("Na stroji není git.")
    os.makedirs(parent, exist_ok=True)
    url = repo if ("://" in repo or repo.startswith("git@")) \
        else f"https://github.com/{repo}.git"
    if shutil.which("gh") and "://" not in repo and not repo.startswith("git@"):
        cmd = ["gh", "repo", "clone", "--", repo, target]   # umí i privátní repo
    else:
        cmd = [GIT, "clone", "--quiet", "--", url, target]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if r.returncode != 0:
        raise ValueError((r.stderr or "klonování nevyšlo").strip()[:300])
    return target


# ── Paměť v cloudu ───────────────────────────────────────────────────────────
# Vault je obyčejná složka s markdownem, takže „napojení na cloud" znamená
# jediné: ať leží uvnitř složky, kterou už nějaký klient synchronizuje.
CLOUD_DIRS = [
    ("OneDrive", ["~/OneDrive", "~/OneDrive - *", "~/Onedrive"]),
    ("Dropbox", ["~/Dropbox"]),
    ("Disk Google", ["~/Google Drive", "~/GoogleDrive", "~/Insync/*"]),
    ("Nextcloud", ["~/Nextcloud", "~/ownCloud"]),
    ("pCloud", ["~/pCloudDrive"]),
    ("MEGA", ["~/MEGA"]),
    ("Syncthing", ["~/Sync"]),
    ("Proton Drive", ["~/Proton Drive", "~/ProtonDrive"]),
    ("iCloud", ["~/Library/Mobile Documents/com~apple~CloudDocs"]),
]


def cloud_folders():
    """Složky synchronizovaných klientů, které na tomhle stroji opravdu jsou."""
    import glob
    found = []
    for name, patterns in CLOUD_DIRS:
        for pattern in patterns:
            for path in sorted(glob.glob(os.path.expanduser(pattern))):
                if os.path.isdir(path):
                    found.append({"name": name, "path": path})
    if IS_WINDOWS and os.environ.get("OneDrive"):
        p = os.environ["OneDrive"]
        if os.path.isdir(p) and not any(f["path"] == p for f in found):
            found.insert(0, {"name": "OneDrive", "path": p})
    return found


def _relink(old_root, new_root):
    """Přesměruje symlinky, které mířily do staré cesty vaultu.

    Paměť Claude Code je symlink do vaultu (`~/.claude/projects/<…>/memory`);
    kdyby po přesunu zůstal viset na staré cestě, paměť by prostě oslepla.
    """
    fixed = []
    base = os.path.join(CLAUDE_DIR, "projects")
    for root, dirs, files in os.walk(base):
        for name in list(dirs) + list(files):
            link = os.path.join(root, name)
            if not os.path.islink(link):
                continue
            target = os.readlink(link)
            if not target.startswith(old_root):
                continue
            new_target = new_root + target[len(old_root):]
            os.unlink(link)
            os.symlink(new_target, link)
            fixed.append(link)
    return fixed


def move_vault(target_parent):
    """Přesune vault do zadané složky a všechno na něj přesměruje."""
    src = os.path.abspath(BRAIN)
    parent = os.path.abspath(os.path.expanduser(target_parent))
    if not os.path.isdir(src):
        raise ValueError("Vault na původním místě není.")
    dst = os.path.join(parent, os.path.basename(src))
    if os.path.abspath(dst) == src:
        raise ValueError("Vault už tam je.")
    if os.path.exists(dst):
        raise ValueError(f"{dst} už existuje — přesun by přepsal cizí data.")
    os.makedirs(parent, exist_ok=True)
    shutil.move(src, dst)
    relinked = _relink(src, dst)
    save_config({"brain_dir": dst})
    return {"path": dst, "relinked": relinked}


# ── Paměť v gitu ─────────────────────────────────────────────────────────────
EMPTY_MEMORY_INDEX = """# Paměť

Jeden soubor = jedna poznámka. Sem patří jenom **rozcestník**: na každou
poznámku jeden řádek do 120 znaků, detail žije v odkazovaném souboru.
Když index přeroste zhruba 25 kB, načte se ho jen kus a na zbytek se zapomene.

## Projekty a reference

## Poznatky (learnings)

## Chyby (errors)

## Úspěchy (wins)
"""

VAULT_GITIGNORE = """# Obsidian si sem ukládá stav okna — do historie nepatří
.obsidian/workspace*
.obsidian/cache
.trash/
.DS_Store
"""


def vault_git_state():
    """(je_repo, remote_url) pro vault."""
    if not os.path.isdir(BRAIN):
        return False, ""
    if not os.path.isdir(os.path.join(BRAIN, ".git")):
        return False, ""
    return True, run([GIT, "remote", "get-url", "origin"], cwd=BRAIN) if GIT else ""


NAME_RE = re.compile(r"^[A-Za-z0-9][\w.-]{0,99}$")


def vault_git_setup(repo_name):
    """Založí z vaultu privátní repo na GitHubu a pošle tam první commit.

    Vrací (ok, hlášku). Nikdy nevyhazuje — onboarding musí umět pokračovat
    i když tohle nevyjde.
    """
    repo_name = str(repo_name).strip()
    if not NAME_RE.match(repo_name):
        return False, "Jméno repa smí být jen písmena, číslice, tečka a pomlčka."
    if not GIT:
        return False, "Na stroji není git."
    if not shutil.which("gh"):
        return False, "Chybí GitHub CLI (gh)."
    if not os.path.isdir(BRAIN):
        return False, "Vault neexistuje."
    try:
        if subprocess.run(["gh", "auth", "status"], capture_output=True,
                          timeout=20).returncode != 0:
            return False, "gh není přihlášený — spusť: gh auth login"
    except Exception:
        return False, "gh se nepodařilo spustit."

    gitignore = os.path.join(BRAIN, ".gitignore")
    if not os.path.isfile(gitignore):
        with open(gitignore, "w", encoding="utf-8") as fh:
            fh.write(VAULT_GITIGNORE)

    try:
        if not os.path.isdir(os.path.join(BRAIN, ".git")):
            job_step("vault", "zakládám repo…")
            subprocess.run([GIT, "init", "-q"], cwd=BRAIN, check=True,
                           capture_output=True, timeout=30)
        job_step("vault", "přidávám poznámky…")
        subprocess.run([GIT, "add", "-A"], cwd=BRAIN, check=True,
                       capture_output=True, timeout=120)
        # Prázdný commit projde taky — jde o to mít co pushnout.
        subprocess.run([GIT, "commit", "-q", "--allow-empty",
                        "-m", "paměť: první záloha"],
                       cwd=BRAIN, capture_output=True, timeout=120)
        job_step("vault", "posílám na GitHub…")
        existing = run([GIT, "remote", "get-url", "origin"], cwd=BRAIN)
        if not existing:
            r = subprocess.run(
                ["gh", "repo", "create", "--private",
                 "--source", ".", "--push", "--", repo_name],
                cwd=BRAIN, capture_output=True, text=True, timeout=300)
            if r.returncode != 0:
                return False, (r.stderr or r.stdout or "gh repo create selhalo").strip()[:300]
        else:
            r = subprocess.run([GIT, "push", "-u", "origin", "HEAD"], cwd=BRAIN,
                               capture_output=True, text=True, timeout=300)
            if r.returncode != 0:
                return False, (r.stderr or "push selhal").strip()[:300]
    except subprocess.CalledProcessError as exc:
        return False, (exc.stderr or b"").decode("utf-8", "replace")[:300] or str(exc)
    except Exception as exc:
        return False, str(exc)[:300]

    save_config({"vault_autosync": True})
    return True, run([GIT, "remote", "get-url", "origin"], cwd=BRAIN)


def vault_git_push(message="paměť: automatická záloha"):
    """Commit + push vaultu. Používá to Stop hook, takže musí být tichý."""
    if not GIT or not os.path.isdir(os.path.join(BRAIN, ".git")):
        return False, "vault není git repo"
    try:
        if not run([GIT, "status", "--porcelain"], cwd=BRAIN):
            return True, "beze změn"
        subprocess.run([GIT, "add", "-A"], cwd=BRAIN, capture_output=True, timeout=120)
        subprocess.run([GIT, "commit", "-q", "-m", message], cwd=BRAIN,
                       capture_output=True, timeout=120)
        r = subprocess.run([GIT, "push", "-q", "origin", "HEAD"], cwd=BRAIN,
                           capture_output=True, text=True, timeout=300)
        if r.returncode != 0:
            return False, (r.stderr or "push selhal").strip()[:200]
        return True, "posláno"
    except Exception as exc:
        return False, str(exc)[:200]


# ── Verze a aktualizace ──────────────────────────────────────────────────────
# „Aktualizovat" a „načíst znovu" jsou dvě různé věci: ⟳ v hlavičce jen přečte
# projekty a paměť, tohle mění samotnou aplikaci. Proto se to jmenuje jinak
# a proto se tu drží číslo verze, ať je vidět, co je nainstalované.
REPO = "jurapascal/claude-code-hub"
SRC_DIR = os.path.join(CLAUDE_DIR, "hub-src")


def on_gateway():
    """Běží hub v prostoru na serveru (brána)? Zdroj pak nepatří uživateli:
    aktualizuje ho server sám (gateway/update.sh), ne tlačítko v prostoru."""
    return bool(CONFIG.get("gateway_user"))


def pamet_prostoru():
    """(použito, strop, počet OOM zabití) cgroupy, ve které hub běží, nebo None.

    Na serveru běží prostor jako scope s MemoryMax; jádro po jeho překročení
    zabíjí procesy a bez téhle stopy v logu nezbyde, proč tab zmizel."""
    try:
        with open("/proc/self/cgroup", encoding="ascii") as fh:
            rel = fh.read().strip().split("::", 1)[1]
    except (OSError, IndexError):
        return None
    base = "/sys/fs/cgroup"
    parts = rel.strip("/").split("/")
    while parts:
        d = os.path.join(base, *parts)
        try:
            with open(os.path.join(d, "memory.max"), encoding="ascii") as fh:
                raw = fh.read().strip()
            if raw.isdigit():
                with open(os.path.join(d, "memory.current"), encoding="ascii") as fh:
                    cur = int(fh.read())
                kills = 0
                with open(os.path.join(d, "memory.events"), encoding="ascii") as fh:
                    for line in fh:
                        if line.startswith("oom_kill "):
                            kills = int(line.split()[1])
                return cur, int(raw), kills
        except (OSError, ValueError):
            pass
        parts.pop()
    return None


def hlidej_pamet(notify, every=30, warn=0.85):
    """Smyčka na pozadí: zapíše OOM zabití do hub.log a přes `notify` pošle
    oknům varování, když se prostor blíží stropu (a znovu až po poklesu)."""
    import time
    last_kills, warned = None, False
    while True:
        time.sleep(every)
        st = pamet_prostoru()
        if not st:
            return
        cur, mx, kills = st
        if last_kills is not None and kills > last_kills:
            log(f"pamet: jádro zabilo {kills - last_kills} proces(ů) na stropu prostoru "
                f"({cur // 2**20} z {mx // 2**20} MB)", "warn")
            notify({"t": "pamet", "stav": "oom", "pouzito": cur // 2**20,
                    "strop": mx // 2**20})
        last_kills = kills
        if cur >= mx * warn and not warned:
            warned = True
            log(f"pamet: prostor je na {cur * 100 // mx} % stropu "
                f"({cur // 2**20} z {mx // 2**20} MB)", "warn")
            notify({"t": "pamet", "stav": "blizko", "pouzito": cur // 2**20,
                    "strop": mx // 2**20})
        elif cur < mx * (warn - 0.1):
            warned = False


# Komu jádro sáhne první, když prostor narazí na strop paměti. Rozhoduje
# oom_score_adj (+ kolik proces zabírá); prostor ho má všude stejný (200),
# takže padal i Claude v otevřeném tabu, zatímco skript s desítkami
# `claude -p` jel dál. Zvednout hodnotu smí proces sám sobě bez práv.
OOM_SKRIPT = 900


def chran_taby():
    """Claude, který neběží přímo v tabu (`claude -p` ze skriptu, z Bash
    nástroje jiného Clauda), dostane vysoké oom_score_adj — při nedostatku
    paměti jde první on, ne rozdělaná práce v tabu. Tab se pozná podle rodiče:
    v tabu Clauda spouští agent-wrapper.sh. Vrací, kolik procesů označil."""
    n = 0
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            with open(f"/proc/{pid}/comm", encoding="utf-8") as fh:
                if fh.read().strip() != "claude":
                    continue
            with open(f"/proc/{pid}/oom_score_adj", encoding="ascii") as fh:
                if int(fh.read().strip() or 0) >= OOM_SKRIPT:
                    continue
            with open(f"/proc/{pid}/stat", encoding="utf-8") as fh:
                ppid = fh.read().rsplit(")", 1)[1].split()[1]
            with open(f"/proc/{ppid}/cmdline", "rb") as fh:
                if b"agent-wrapper.sh" in fh.read():
                    continue
            with open(f"/proc/{pid}/oom_score_adj", "w", encoding="ascii") as fh:
                fh.write(str(OOM_SKRIPT))
            n += 1
        except (OSError, ValueError, IndexError):
            continue
    return n


def hlidej_skripty(every=5):
    """Smyčka na pozadí (jen v prostoru na serveru): chran_taby() každých
    pár vteřin — skript jich pustí desítky za minutu."""
    import time
    while True:
        try:
            n = chran_taby()
            if n:
                log(f"pamet: {n}× claude mimo tab má přednost při nedostatku paměti (oom_score_adj {OOM_SKRIPT})")
        except Exception as exc:
            log_error("ochrana tabů před skripty", exc)
        time.sleep(every)


# Kam noční aktualizace brány zapisuje, jak dopadla. /etc je v sandboxu vidět
# ke čtení, takže to prostor přečte, i když na nic jiného mimo domov nedosáhne.
GATEWAY_UPDATE_STATUS = "/etc/claude-hub/update.json"


def gateway_update_status():
    """Co naposledy udělala noční aktualizace serveru ({} = zatím nic)."""
    try:
        with open(GATEWAY_UPDATE_STATUS, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def version():
    """Verze, která je NAINSTALOVANÁ — čte se ze souboru, ne z importu.

    Běžící proces si modul drží v paměti z okamžiku startu, takže po aktualizaci
    hlásil pořád tu starou: „aktualizováno" a hned pod tím „je dostupná novější".
    """
    path = os.path.join(CLAUDE_DIR, "hub", "__init__.py")
    if TEST_MODE:
        # Zkušební instance má vlastní kód; verze nainstalovaného hubu vedle ní
        # s tím nesouvisí — porovnávala se s GitHubem a spustila aktualizaci
        # skutečné instalace.
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "__init__.py")
    if on_gateway():
        # V prostoru běží hub ze zdroje serveru. Kopie v domově (po dřívějším
        # tlačítku Aktualizovat) s tím nemá nic společného — hlásila 2.4.2 nad
        # serverem, který už jel na 2.4.3.
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "__init__.py")
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("__version__"):
                    return line.split("=", 1)[1].strip().strip("\"'")
    except Exception:
        pass
    from . import __version__      # spouštěno z klonu, ne z instalace
    return __version__


def _as_tuple(text):
    """'v1.2.3' → (1, 2, 3). Co nejde přečíst, je (0,), tedy nejstarší."""
    parts = []
    for chunk in str(text or "").lstrip("vV").split("."):
        digits = "".join(c for c in chunk if c.isdigit())
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts) or (0,)


def source_dir():
    """Složka se zdrojem, ze kterého se hub instaluje."""
    for cand in (CONFIG.get("src_dir"), SRC_DIR):
        if not cand:
            continue
        path = os.path.expanduser(cand)
        if os.path.isfile(os.path.join(path, "install.sh")):
            return path
    return ""


def _latest_tag_git():
    """Nejvyšší značka z `git ls-remote`, nebo ''. Na limit API GitHubu se
    nevztahuje — a aktualizace sama jede přes git pull, takže co vidí git,
    to se i stáhne."""
    if not GIT:
        return ""
    out = run([GIT, "ls-remote", "--tags", "--refs", f"https://github.com/{REPO}.git"],
              timeout=15)
    best = ""
    for line in out.splitlines():
        tag = line.rsplit("refs/tags/", 1)[1] if "refs/tags/" in line else ""
        if tag and (not best or _as_tuple(tag) > _as_tuple(best)):
            best = tag
    return best


def latest_version():
    """(nejnovější vydaná verze, důvod prázdna).

    Ptá se na obě místa a bere z nich **tu vyšší**, ne první, která odpoví.
    Dřív měl přednost `releases/latest`, takže když se vydávalo jen značkou,
    hub tvrdil „máš nejnovější" nad verzí o tři vydání starší — 1.3.2 až 1.3.4
    se takhle nikdy nikomu nenabídly. Značka i release jsou vydání, rozhoduje
    číslo.

    Rozlišuje „není síť" od „repo zatím nic nevydalo" — pro toho, kdo se dívá
    do nastavení, je to úplně jiná zpráva.
    """
    import urllib.error
    import urllib.request
    reachable = False
    limited_until = ""
    best = ""
    for url, key in ((f"https://api.github.com/repos/{REPO}/releases/latest", "tag_name"),
                     (f"https://api.github.com/repos/{REPO}/tags", None)):
        try:
            req = urllib.request.Request(url, headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": "claude-code-hub"})
            with urllib.request.urlopen(req, timeout=6) as r:
                data = json.load(r)
            reachable = True
            if key:
                tags = [data.get(key)]
            else:
                # /tags vrací pořadí, na které se nedá spolehnout — projdeme vše.
                tags = [t.get("name") for t in data] if isinstance(data, list) else []
            for tag in tags:
                if tag and (not best or _as_tuple(tag) > _as_tuple(best)):
                    best = str(tag)
        except urllib.error.HTTPError as exc:
            reachable = True          # server odpověděl, jen tam nic není
            # …ledaže odmítl kvůli limitu. Bez přihlášení pustí GitHub 60 dotazů
            # za hodinu na adresu a za jednou adresou bývá víc lidí i nástrojů —
            # hub pak tvrdil „Repo zatím nemá žádné vydání", i když vydání bylo.
            if exc.code in (403, 429) and exc.headers.get("X-RateLimit-Remaining") == "0":
                limited_until = exc.headers.get("X-RateLimit-Reset") or "?"
            continue
        except Exception:
            continue
    if not best:
        best = _latest_tag_git()
    if best:
        return best, ""
    if limited_until:
        import time
        try:
            when = "po " + time.strftime("%H:%M", time.localtime(int(limited_until)))
        except ValueError:
            when = "za hodinu"
        return "", ("GitHub teď odmítá dotazy — z téhle adresy je vyčerpaný limit "
                    f"60 za hodinu. Zkus to znovu {when}.")
    return "", ("Repo zatím nemá žádné vydání." if reachable
                else "Nepodařilo se spojit s GitHubem.")


def version_info(check_remote=False):
    src = source_dir()
    info = {"version": version(), "src": src, "repo": REPO, "latest": "",
            "update_available": False}
    if on_gateway():
        # Na serveru se nic nekontroluje ani nenabízí: aktualizuje server sám.
        info["managed"] = True
        info["server"] = gateway_update_status()
        return info
    if src and GIT:
        info["commit"] = run([GIT, "-C", src, "rev-parse", "--short", "HEAD"])
    if check_remote:
        latest, why = latest_version()
        info["latest"] = latest
        info["why"] = why
        info["update_available"] = bool(
            latest and _as_tuple(latest) > _as_tuple(version()))
    return info


def _fetch_source():
    """Zajistí, že v ~/.claude/hub-src je aktuální zdroj. Vrací (ok, detail).

    Klon se používá, když už tam je; jinak se stáhne tarball, takže aktualizace
    funguje i tomu, kdo hub dostal jako ZIP a git nemá.
    """
    if GIT and os.path.isdir(os.path.join(SRC_DIR, ".git")):
        r = subprocess.run([GIT, "-C", SRC_DIR, "pull", "--ff-only", "--quiet"],
                           capture_output=True, text=True, timeout=180)
        if r.returncode != 0:
            return False, (r.stderr or "git pull selhal").strip()[:300]
        return True, "git pull"
    if GIT:
        parent = os.path.dirname(SRC_DIR)
        os.makedirs(parent, exist_ok=True)
        if os.path.isdir(SRC_DIR):
            shutil.rmtree(SRC_DIR, ignore_errors=True)
        r = subprocess.run([GIT, "clone", "--quiet", "--depth", "1",
                            f"https://github.com/{REPO}.git", SRC_DIR],
                           capture_output=True, text=True, timeout=300)
        if r.returncode == 0:
            return True, "klon"
        return False, (r.stderr or "klonování selhalo").strip()[:300]

    import tarfile
    import tempfile
    import urllib.request
    url = f"https://codeload.github.com/{REPO}/tar.gz/refs/heads/main"
    try:
        with tempfile.TemporaryDirectory() as tmp:
            archive = os.path.join(tmp, "hub.tgz")
            urllib.request.urlretrieve(url, archive)
            with tarfile.open(archive) as tf:
                # extractall bez filtru umí zapsat i mimo cílovou složku,
                # když archiv obsahuje `../` nebo absolutní cesty.
                try:
                    tf.extractall(tmp, filter="data")        # Python 3.12+
                except TypeError:
                    for member in tf.getmembers():
                        dest = os.path.realpath(os.path.join(tmp, member.name))
                        if not dest.startswith(os.path.realpath(tmp) + os.sep):
                            raise ValueError("archiv chtěl zapsat mimo složku")
                        if member.issym() or member.islnk():
                            raise ValueError("archiv obsahuje odkazy")
                    tf.extractall(tmp)
            inner = [os.path.join(tmp, n) for n in os.listdir(tmp)
                     if os.path.isdir(os.path.join(tmp, n))]
            if not inner:
                return False, "stažený balík byl prázdný"
            if os.path.isdir(SRC_DIR):
                shutil.rmtree(SRC_DIR, ignore_errors=True)
            shutil.move(inner[0], SRC_DIR)
        return True, "tarball"
    except Exception as exc:
        return False, f"stažení nevyšlo: {exc}"[:300]


# Cokoli, co trvá déle než okamžik, se nesmí dělat uvnitř HTTP požadavku:
# stránka na něm visí, a když se okno mezitím zavře nebo načte znovu, výsledek
# se nemá kam vrátit. Přesně to se stalo u aktualizace i u zálohy paměti do
# gitu (`gh repo create --push` na patnáctimegovém vaultu chvíli trvá).
# Běží proto na pozadí pod jménem a stav se odečítá.
_JOBS = {}
_JOBS_LOCK = threading.Lock()


def job_state(name):
    with _JOBS_LOCK:
        return dict(_JOBS.get(name) or
                    {"running": False, "done": False, "result": None, "step": ""})


def job_step(name, text):
    with _JOBS_LOCK:
        if name in _JOBS:
            _JOBS[name]["step"] = text


def start_job(name, fn):
    """Spustí fn() na pozadí pod jménem. False, když už jedna běží."""
    with _JOBS_LOCK:
        if (_JOBS.get(name) or {}).get("running"):
            return False
        _JOBS[name] = {"running": True, "done": False, "result": None,
                       "step": "začínám…"}

    log(f"úloha {name}: start")

    def worker():
        started = time.time()
        try:
            result = fn()
        except Exception as exc:          # pojistka, ať stav nezůstane viset
            log_error(f"úloha {name} spadla", exc)
            result = {"ok": False, "detail": str(exc)[:300]}
        took = time.time() - started
        if isinstance(result, dict) and result.get("ok") is False:
            log(f"úloha {name}: neúspěch za {took:.1f}s — "
                f"{str(result.get('detail'))[:200]}", "warn")
        else:
            log(f"úloha {name}: hotovo za {took:.1f}s")
        with _JOBS_LOCK:
            _JOBS[name] = {"running": False, "done": True, "result": result,
                           "step": ""}

    threading.Thread(target=worker, daemon=True).start()
    return True


def update_state():
    return job_state("update")


def start_update():
    if TEST_MODE:
        # Zkušební kopie nic neinstaluje: update_hub přepisuje ~/.claude.
        return start_job("update", lambda: {
            "ok": False, "detail": "Zkušební instance (--test) se neaktualizuje."})
    return start_job("update", update_hub)


def bundled_runtime():
    """Běží hub na Pythonu přibaleném v instalačce ke stažení (packaging/)?
    Pozná se podle značky, kterou tam instalačka nechá."""
    return os.path.isfile(os.path.join(sys.prefix, "HUB_RUNTIME"))


def update_hub():
    """Stáhne nejnovější verzi a přeinstaluje ji. Nikdy nevyhazuje."""
    was = version()
    job_step("update", "stahuju…")
    ok, detail = _fetch_source()
    if not ok:
        return {"ok": False, "detail": detail}
    job_step("update", "instaluju…")
    installer = os.path.join(SRC_DIR, "install.sh")
    if IS_WINDOWS:
        installer = os.path.join(SRC_DIR, "install.ps1")
        argv = ["powershell", "-ExecutionPolicy", "Bypass", "-File", installer, "-Update"]
    else:
        if not BASH:
            return {"ok": False, "detail": "Na stroji není bash."}
        argv = [BASH, installer, "--update"]
    # --update/-Update = jen soubory appky, příkazy a hooky. Celá instalace
    # (skilly, Obsidian, gh, Playwright, kontrola) se při každé aktualizaci
    # pouštěla znovu a trvala zbytečně dlouho — stroj už nastavený je.
    if not os.path.isfile(installer):
        return {"ok": False, "detail": "Ve staženém zdroji chybí instalačka."}
    env = dict(os.environ)
    if bundled_runtime():
        # Hub z instalačky ke stažení jede na přibaleném Pythonu — ať na něj
        # hooky a zástupce míří i po aktualizaci (v systému jiný být nemusí).
        from . import setup
        env = setup.installer_env()
    try:
        r = subprocess.run(argv, cwd=SRC_DIR, capture_output=True, text=True,
                           timeout=1800, env=env)
    except subprocess.TimeoutExpired:
        return {"ok": False, "detail": "Aktualizace trvala moc dlouho."}
    except Exception as exc:
        return {"ok": False, "detail": str(exc)[:300]}
    if r.returncode != 0:
        return {"ok": False,
                "detail": ("Instalace selhala:\n" + (r.stderr or r.stdout))[:500]}
    now = version()                # ze souboru, tedy to, co je fakticky nasazené
    changed = bool(now and now != was)
    return {"ok": True, "changed": changed, "was": was, "now": now or was,
            "restart": changed,
            "detail": (f"Nainstalována verze {now}." if changed
                       else f"Verze {was} je nejnovější.")}


# ── Systémová schránka ───────────────────────────────────────────────────────
# WebKitGTK (okno hubu na Linuxu) odmítá navigator.clipboard bez uživatelského
# gesta a document.execCommand('copy') tam vrací false, takže se v tabu nedalo
# kopírovat vůbec. Hub ale běží jako místní proces, takže na schránku dosáhne
# přímo — a rovnou i na PRIMARY, na kterou je člověk na Linuxu zvyklý.
def _clipboard_tools(which):
    """(příkaz pro čtení, příkaz pro zápis) pro tenhle stroj, nebo (None, None)."""
    primary = which == "primary"
    if IS_WINDOWS:
        if primary:
            return None, None                      # Windows PRIMARY nezná
        # Naměřeno na Windows 11 Pro (kontrola ve virtuálce): „příliš žluťoučký
        # kůň" projde tam a zpět jako „p??li? ?lu?ou?k? k??". PowerShell sype
        # stdout v kódování konzole (OEM), ne v UTF-8, a clip.exe čte stdin
        # stejně — Unicode pozná jen podle UTF-16LE BOM. Obojí se musí říct.
        return (["powershell", "-NoProfile", "-Command",
                 "[Console]::OutputEncoding=[Text.Encoding]::UTF8; "
                 "Get-Clipboard -Raw"],
                ["clip"])
    if IS_MAC:
        if primary:
            return None, None
        return (["pbpaste"], ["pbcopy"])
    if shutil.which("wl-copy") and shutil.which("wl-paste"):
        sel = ["--primary"] if primary else []
        return (["wl-paste", "--no-newline"] + sel, ["wl-copy"] + sel)
    if shutil.which("xclip"):
        target = "primary" if primary else "clipboard"
        return (["xclip", "-selection", target, "-o"],
                ["xclip", "-selection", target, "-i"])
    if shutil.which("xsel"):
        flag = "--primary" if primary else "--clipboard"
        return (["xsel", flag, "-o"], ["xsel", flag, "-i"])
    return None, None


# xclip je hotový v řádu milisekund, jenže na Windows se spouští PowerShell a
# ten studený startuje klidně několik sekund. Se čtyřmi sekundami tam čtení
# schránky padalo na TimeoutExpired a hub z toho hlásil prázdno — naměřeno na
# Windows 11 Pro, kde `clipboard_read()` vracelo None u textu, který na schránce
# prokazatelně byl.
_CLIP_TIMEOUT = 20 if IS_WINDOWS else 4


def clipboard_read(which="clipboard"):
    read_cmd, _ = _clipboard_tools(which)
    if not read_cmd:
        return None
    try:
        r = subprocess.run(read_cmd, capture_output=True, timeout=_CLIP_TIMEOUT,
                           creationflags=_NO_WINDOW)
        # Screenshot na schránce nemusí vyjít naprázdno, jak se tu dřív čekalo.
        # Když vlastník výběru žádný textový cíl nenabízí, `xclip -o` sáhne
        # po prvním, který má — a vrátí rovnou binárku PNG (naměřeno na
        # Waylandu, kde schránku mostí XWayland). Dekódovat ji „s náhradami“
        # znamenalo napsat na prompt smetí místo cesty k obrázku, takže co
        # není čistý text, končí tady jako prázdno — a volající sáhne po
        # clipboard_image().
        try:
            text = r.stdout.decode("utf-8")
        except UnicodeDecodeError:
            return ""
        if "\x00" in text:
            return ""
        if IS_WINDOWS:
            # Get-Clipboard vrací řádky s CRLF a jeden navíc na konci
            text = text.replace("\r\n", "\n")
            if text.endswith("\n"):
                text = text[:-1]
        return text
    except Exception:
        return None


# Obrázek na schránce (screenshot) je zvláštní případ: `xclip -o` na něm hlásí
# „target STRING not available" a vrátí prázdno, takže Ctrl+V vyzní naprázdno.
# Terminálu se ale obrázek podat nedá — jen cesta k němu. Odložíme si ho tedy
# na disk stejně jako vložený nebo přetažený soubor a vrátíme cestu.
_CLIP_IMAGE_TYPES = (("image/png", ".png"), ("image/jpeg", ".jpg"),
                     ("image/webp", ".webp"), ("image/bmp", ".bmp"))


def _clipboard_image_unix():
    if shutil.which("wl-paste"):
        r = subprocess.run(["wl-paste", "--list-types"],
                           capture_output=True, timeout=4)
        have = r.stdout.decode("utf-8", "replace").split()
        for mime, ext in _CLIP_IMAGE_TYPES:
            if mime in have:
                r = subprocess.run(["wl-paste", "--type", mime],
                                   capture_output=True, timeout=8)
                return r.stdout, ext
        return None, None
    if shutil.which("xclip"):
        r = subprocess.run(["xclip", "-selection", "clipboard", "-t", "TARGETS", "-o"],
                           capture_output=True, timeout=4)
        have = r.stdout.decode("utf-8", "replace").split()
        for mime, ext in _CLIP_IMAGE_TYPES:
            if mime in have:
                r = subprocess.run(["xclip", "-selection", "clipboard", "-t", mime, "-o"],
                                   capture_output=True, timeout=8)
                return r.stdout, ext
    return None, None                      # xsel o cílech neumí říct nic


def _clipboard_image_windows():
    """Snipping Tool a Win+Shift+S nechávají obrázek jen jako bitmapu."""
    import tempfile
    fd, tmp = tempfile.mkstemp(suffix=".png")
    os.close(fd)
    try:
        script = ("Add-Type -AssemblyName System.Windows.Forms,System.Drawing; "
                  "$i=[Windows.Forms.Clipboard]::GetImage(); "
                  "if($i){$i.Save('%s',"
                  "[System.Drawing.Imaging.ImageFormat]::Png)}"
                  % tmp.replace("'", "''"))
        # -STA: bez apartmentu pro jedno vlákno schránku Windows nepustí.
        subprocess.run(["powershell", "-NoProfile", "-STA", "-Command", script],
                       capture_output=True, timeout=12, creationflags=_NO_WINDOW)
        if os.path.getsize(tmp):
            with open(tmp, "rb") as fh:
                return fh.read(), ".png"
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    return None, None


def _clipboard_image_mac():
    if shutil.which("pngpaste"):
        r = subprocess.run(["pngpaste", "-"], capture_output=True, timeout=8)
        if r.returncode == 0 and r.stdout:
            return r.stdout, ".png"
        return None, None
    import tempfile
    fd, tmp = tempfile.mkstemp(suffix=".png")
    os.close(fd)
    try:
        script = ('try\n'
                  '  set d to the clipboard as \u00abclass PNGf\u00bb\n'
                  'on error\n'
                  '  return\n'
                  'end try\n'
                  'set f to open for access POSIX file "%s" with write permission\n'
                  'write d to f\n'
                  'close access f' % tmp)
        subprocess.run(["osascript", "-e", script], capture_output=True, timeout=10)
        if os.path.getsize(tmp):
            with open(tmp, "rb") as fh:
                return fh.read(), ".png"
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    return None, None


def clipboard_image():
    """Cesta k obrázku odloženému ze schránky, nebo None, když tam žádný není."""
    try:
        if IS_WINDOWS:
            raw, ext = _clipboard_image_windows()
        elif IS_MAC:
            raw, ext = _clipboard_image_mac()
        else:
            raw, ext = _clipboard_image_unix()
    except Exception:
        return None
    if not raw or len(raw) > MAX_UPLOAD:
        return None
    try:
        return save_upload("schranka" + ext, raw)
    except Exception:
        return None


def clipboard_write(text, which="clipboard"):
    """Zapíše text do schránky. True, když se to povedlo.

    Pod X (a tedy i XWayland) drží obsah výběru **proces, který ho zapsal** —
    `xclip -i` se proto po zápisu odpojí a běží dál, dokud výběr někdo
    nepřepíše. Čekat na jeho konec by znamenalo čekat až do timeoutu a pak
    hlásit neúspěch u zápisu, který ve skutečnosti prošel. `clip` na Windows
    a `pbcopy` na macOS se naopak ukončí hned, takže krátké čekání rozliší
    obojí: TimeoutExpired tady znamená „drží výběr", ne chybu.
    """
    _, write_cmd = _clipboard_tools(which)
    if not write_cmd:
        return False
    try:
        proc = subprocess.Popen(write_cmd, stdin=subprocess.PIPE,
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL,
                                creationflags=_NO_WINDOW)
    except Exception:
        return False
    # clip.exe bere UTF-8 jako OEM znaky; BOM je jediné, čemu uvěří.
    payload = (b"\xff\xfe" + text.encode("utf-16-le") if IS_WINDOWS
               else text.encode("utf-8"))
    # Na Windows a macOS se čeká na skutečný konec procesu (a tedy na skutečný
    # návratový kód); pod X je čekání jen krátké, protože tam `xclip` schválně
    # běží dál a doběhnout nemá proč.
    wait = _CLIP_TIMEOUT if (IS_WINDOWS or IS_MAC) else 1.0
    try:
        proc.communicate(input=payload, timeout=wait)
        return proc.returncode == 0
    except subprocess.TimeoutExpired:
        return True          # drží výběr, jak má
    except Exception:
        return False


def has_clipboard():
    return _clipboard_tools("clipboard")[0] is not None


def current_model():
    """Model, se kterým Claude Code startuje nové sessions.

    `/model <jméno>` si volbu ukládá do ~/.claude/settings.json, takže je to
    jediné místo, kde se dá zjistit, co je vybrané, bez čtení terminálu.
    Prázdný řetězec = v souboru nic není a rozhoduje Claude Code sám.
    """
    try:
        with open(os.path.join(CLAUDE_DIR, "settings.json"),
                  encoding="utf-8-sig") as fh:
            model = json.load(fh).get("model")
    except (OSError, ValueError):
        return ""
    return str(model) if isinstance(model, str) else ""


# Tímhle klíčem si Claude Code v ~/.claude/settings.json pamatuje, že člověk
# potvrdil varování k bypassu.
BYPASS_ACK = "skipDangerousModePermissionPrompt"


def bypass_accepted():
    """Potvrdil už člověk varování k bypassu? Bez toho se Claude Code
    s možností bypassu při každém startu anglicky ptá a předvybrané má
    „No, exit" — Enter ho ukončí. Proto se možnost dává jen po potvrzení."""
    try:
        with open(os.path.join(CLAUDE_DIR, "settings.json"),
                  encoding="utf-8-sig") as fh:
            return json.load(fh).get(BYPASS_ACK) is True
    except (OSError, ValueError, AttributeError):
        return False


def accept_bypass():
    """Zapíše potvrzení bypassu do ~/.claude/settings.json (jako Claude Code
    sám). Soubor, který nejde přečíst, se nepřepisuje — o nastavení nepřijít."""
    path = os.path.join(CLAUDE_DIR, "settings.json")
    try:
        with open(path, encoding="utf-8-sig") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        data = {}
    except (OSError, ValueError):
        raise ValueError("~/.claude/settings.json nejde přečíst — "
                         "bypass nezapínám, ať o nastavení nepřijdeš.")
    if not isinstance(data, dict):
        raise ValueError("~/.claude/settings.json nemá obvyklý tvar — bypass nezapínám.")
    if data.get(BYPASS_ACK) is True:
        return
    data[BYPASS_ACK] = True
    os.makedirs(CLAUDE_DIR, exist_ok=True)
    tmp = path + ".hub-tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    os.replace(tmp, path)


def trust_folder(path):
    """Označí složku v ~/.claude.json jako důvěryhodnou, ať se Claude Code při
    každém novém tabu neptá „Do you trust the files in this folder?“ — tab
    otevírá člověk sám nad svou složkou, tím už souhlasil.

    ~/.claude.json přepisuje za běhu i Claude Code, takže opatrně: nic, když
    je složka už důvěryhodná; soubor, který nejde přečíst, se nepřepisuje; a
    když se mezi čtením a zápisem změnil, zkusí se to znovu nad novým obsahem.
    Vrací True, když je složka po volání důvěryhodná."""
    if not path:
        return False
    target = os.path.join(HOME, ".claude.json")
    key = os.path.abspath(os.path.expanduser(path))
    for _pokus in range(3):
        try:
            before = os.stat(target).st_mtime_ns
            with open(target, encoding="utf-8") as fh:
                data = json.load(fh)
        except FileNotFoundError:
            before, data = None, {}
        except (OSError, ValueError):
            return False
        if not isinstance(data, dict):
            return False
        projects = data.setdefault("projects", {})
        if not isinstance(projects, dict):
            return False
        entry = projects.setdefault(key, {})
        if not isinstance(entry, dict):
            return False
        if entry.get("hasTrustDialogAccepted") is True:
            return True
        entry["hasTrustDialogAccepted"] = True
        tmp = target + ".hub-tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=2)
            now = os.stat(target).st_mtime_ns if before is not None else None
            if now != before:
                os.remove(tmp)
                continue                  # Claude Code mezitím zapsal — znovu
            os.chmod(tmp, 0o600)
            os.replace(tmp, target)
            return True
        except OSError:
            try:
                os.remove(tmp)
            except OSError:
                pass
            return False
    return False


_MODEL_ID = re.compile(r"^claude-([a-z]+)-(\d+)(?:-(\d{1,2}))?(?:-\d{8})?(?:\[[^\]]*\])?$")


def model_label(model_id):
    """Jméno modelu pro člověka: `claude-opus-5` → `Opus 5`,
    `claude-haiku-4-5-20251001` → `Haiku 4.5`. Neznámý tvar vrátí, jak je."""
    text = str(model_id or "").strip()
    m = _MODEL_ID.match(text.lower())
    if not m:
        return text
    family, major, minor = m.groups()
    return f"{family.capitalize()} {major}" + (f".{minor}" if minor else "")


# tag tabu → (přepis konverzace, kdy se hledal). Přepis se hledá znovu po půl
# minutě: /clear nebo /resume v tabu začne novou session s novým souborem.
_TAB_TRANSCRIPTS = {}
TRANSCRIPT_RECHECK = 30.0


def _tab_transcript(sid, cwd, started):
    """Přepis konverzace, která běží v tabu, nebo ''.

    Přesně to ví Stop hook (memory-autosave.py): k session zapíše HUB_TAB. Bez
    něj (automatické ukládání vypnuté) se vezme nejnovější přepis ze složky
    projektu, který vznikl až po startu tabu — u dvou tabů v jednom projektu
    to může být ten druhý, ale lepší odhad než nic.
    """
    tag = tab_tag(sid)
    cached = _TAB_TRANSCRIPTS.get(tag)
    if cached and time.time() - cached[1] < TRANSCRIPT_RECHECK \
            and os.path.isfile(cached[0]):
        return cached[0]
    found, last = "", -1.0
    try:
        names = os.listdir(AUTOSAVE_DIR)
    except OSError:
        names = []
    for name in names:
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(AUTOSAVE_DIR, name), encoding="utf-8") as fh:
                info = json.load(fh)
        except (OSError, ValueError):
            continue
        if not isinstance(info, dict) or info.get("tab") != tag:
            continue
        path = info.get("transcript") or ""
        when = info.get("last") if isinstance(info.get("last"), (int, float)) else 0
        if os.path.isfile(path) and when >= last:
            found, last = path, when
    if not found and cwd:
        folder = os.path.join(CLAUDE_DIR, "projects",
                              re.sub(r"[^A-Za-z0-9]", "-", os.path.realpath(cwd)))
        newest = 0.0
        try:
            with os.scandir(folder) as entries:
                for entry in entries:
                    if not entry.name.endswith(".jsonl"):
                        continue
                    mtime = entry.stat().st_mtime
                    # Změněný po startu tabu nestačí (přepis mohl upravit
                    # kdokoli jiný) — musí i začínat až po něm.
                    if mtime >= started - 2 and mtime > newest \
                            and _first_time(entry.path) >= started - 5:
                        found, newest = entry.path, mtime
        except OSError:
            pass
    if found:
        _TAB_TRANSCRIPTS[tag] = (found, time.time())
    return found


def transcript_path(cwd, chat_id):
    """Kde Claude Code vede konverzaci `chat_id` spuštěnou ve složce `cwd`."""
    if not chat_id:
        return ""
    folder = re.sub(r"[^A-Za-z0-9]", "-", os.path.realpath(cwd or HOME))
    return os.path.join(CLAUDE_DIR, "projects", folder, chat_id + ".jsonl")


def transcript_for(session):
    """Přepis konverzace, kterou vede tab (nebo ''). Sezení zná server, cestu
    k přepisu tenhle modul — čtení (hub/cteni.py) potřebuje obojí.

    Když si id konverzace zvolil hub sám (--session-id), je cesta jistá hned
    od prvního řádku. Jinak se hledá jako dosud: podle záznamu od Stop hooku,
    nebo podle nejnovějšího přepisu ve složce projektu.
    """
    if not session:
        return ""
    cwd = session.path or HOME
    chat_id = getattr(session, "chat_id", "")
    if chat_id:
        path = transcript_path(cwd, chat_id)
        if os.path.isfile(path):
            # Obnovený tab: Claude Code při --resume umí založit nový přepis
            # s novým id a historii do něj zkopírovat. Starý pak stojí a čtení
            # by nové zprávy nevidělo („zprávu zatím nepřevzal“). Pokračování
            # se hledá jen občas — čtení se ptá každou vteřinu.
            now = time.time()
            if now - getattr(session, "navaz_at", 0) >= NAVAZ_KAZDYCH:
                session.navaz_at = now
                rodina = getattr(session, "rodina", None) or [chat_id]
                novy = _pokracovani(path, rodina, getattr(session, "started", 0))
                if novy:
                    session.chat_id = os.path.basename(novy)[:-6]
                    session.rodina = rodina + [session.chat_id]
                    return novy
            return path
    return _tab_transcript(session.id, cwd, session.started)


NAVAZ_KAZDYCH = 4.0


def _pokracovani(path, rodina, started=0):
    """Novější přepis ve stejné složce, který navazuje na některé z id v `rodina`
    (Claude Code ho založil při obnovení tabu), nebo ''. Pozná se podle toho,
    že staré id je v jeho začátku — zkopírovaná historie ho nese dál.

    Bere se jen přepis změněný až po startu tabu (`started`): při obnovení po
    pádu by jinak „novější“ kopie z předchozího běhu přebila originál, do
    kterého tab zrovna píše, a po dalším restartu by se Claude vrátil do kopie."""
    try:
        base = os.path.getmtime(path)
        entries = list(os.scandir(os.path.dirname(path)))
    except OSError:
        return ""
    needles = [('"%s"' % r).encode("ascii") for r in rodina]
    mine = {r + ".jsonl" for r in rodina}
    best, best_m = "", base
    for e in entries:
        if not e.name.endswith(".jsonl") or e.name in mine:
            continue
        try:
            m = e.stat().st_mtime
        except OSError:
            continue
        if m <= best_m or (started and m < started):
            continue
        try:
            with open(e.path, "rb") as fh:
                head = fh.read(256 * 1024)
        except OSError:
            continue
        if any(n in head for n in needles):
            best, best_m = e.path, m
    return best


def _first_time(transcript):
    """Čas prvního záznamu přepisu (epoch), 0 = nevíme."""
    try:
        with open(transcript, "rb") as fh:
            head = fh.read(32768)
    except OSError:
        return 0.0
    for raw in head.splitlines():
        try:
            stamp = json.loads(raw).get("timestamp")
        except (ValueError, AttributeError):
            continue
        if isinstance(stamp, str):
            try:
                return datetime.datetime.fromisoformat(
                    stamp.replace("Z", "+00:00")).timestamp()
            except ValueError:
                return 0.0
    return 0.0


def _last_model(transcript):
    """(id modelu, čas) poslední odpovědi v přepisu. Čte se jen konec souboru —
    přepis dlouhé session má desítky MB."""
    try:
        with open(transcript, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - 512 * 1024))
            chunk = fh.read()
    except OSError:
        return "", ""
    for raw in reversed(chunk.splitlines()):
        if b'"assistant"' not in raw or b'"model"' not in raw:
            continue
        try:
            entry = json.loads(raw)
        except ValueError:
            continue              # první řádek useknutý uprostřed
        msg = entry.get("message") if isinstance(entry, dict) else None
        model = msg.get("model") if isinstance(msg, dict) else ""
        # `<synthetic>` píše Claude Code u hlášek, které nepsal model.
        if entry.get("type") == "assistant" and isinstance(model, str) \
                and model.startswith("claude-"):
            return model, str(entry.get("timestamp") or "")
    return "", ""


def tab_model(sid, cwd, started):
    """Model, kterým Claude v tabu doopravdy odpověděl naposledy.

    V settings.json je jen volba (a u výchozí nic) — co skutečně běží, ví až
    přepis konverzace. `at` je čas té odpovědi, podle něj bublina pozná, že
    po přepnutí modelu přišla nová.
    """
    transcript = _tab_transcript(sid, cwd, started)
    model, at = _last_model(transcript) if transcript else ("", "")
    return {"model": model, "label": model_label(model), "at": at}


def installed_skills():
    """Slash commands actually installed in ~/.claude/skills/."""
    try:
        return sorted(n for n in os.listdir(SKILLS_DIR)
                      if os.path.isfile(os.path.join(SKILLS_DIR, n, "SKILL.md")))
    except Exception:
        return []


# ── Terminal command builders ────────────────────────────────────────────────
# Every tab is a bash session, on every platform. Paths are converted to the
# shell's own form so the same snippet works under Git Bash too.
def sh_quote(text):
    """Uzavře řetězec tak, aby ho bash vzal doslova.

    Do příkazu pro tab se skládají cesty ze skenu složek, a ty nemusí být
    nevinné: složka pojmenovaná `projekt$(rm -rf ~)x` — což vznikne třeba
    naklonováním cizího repa — by se v dvojitých uvozovkách vyhodnotila a
    spustila ve chvíli, kdy na ni člověk v panelu klikne. Jednoduché uvozovky
    to zastaví; jediné, co v nich má význam, je apostrof sám.
    """
    return "'" + str(text).replace("'", "'\\''") + "'"


# ── AI agenti ────────────────────────────────────────────────────────────────
# Katalog je v agents.py; tady je jen to, co potřebuje běžící hub — která volba
# platí, čím se tab spustí a co se pošle do UI.
def _agents_extra():
    """Vlastní agenti dopsaní do hub-config.json (klíč "agents")."""
    extra = CONFIG.get("agents")
    return extra if isinstance(extra, dict) else {}


def default_agent():
    """Kterým agentem se otevírá projekt, když se nevybere jinak."""
    want = str(CONFIG.get("default_agent") or "claude").strip().lower()
    cat = agents.catalog(_agents_extra())
    return want if want in cat else ("claude" if "claude" in cat else
                                     (agents.order(cat) or ["claude"])[0])


def agent_spec(agent_id=""):
    """Záznam agenta. Neznámé jméno spadne na výchozího — tab se otevře vždy."""
    extra = _agents_extra()
    spec = agents.resolve(agent_id, extra) if agent_id else None
    if not spec:
        spec = agents.resolve(default_agent(), extra) or agents.resolve("claude")
    return spec


def project_agent(path):
    """Agent zapamatovaný u konkrétního projektu, jinak výchozí."""
    saved = CONFIG.get("project_agents")
    if isinstance(saved, dict) and path:
        want = str(saved.get(path) or "").strip().lower()
        if want and want in agents.catalog(_agents_extra()):
            return want
    return default_agent()


def agents_state(with_version=True):
    """Co je na stroji k dispozici — pro nastavení, uvítací obrazovku i doctor."""
    return {
        "agents": agents.detect(_agents_extra(), with_version),
        "default": default_agent(),
        "ollama": agents.ollama_state(),
    }


def _wrapper_path():
    """Společný wrapper: z instalace, jinak ten vedle běžícího kódu.

    Druhá cesta je pro běh z klonu bez instalace — a hlavně pojistka proti
    tichému omylu: kdyby se sáhlo po starém claude-wrapper.sh, spustil by se
    Claude Code i v tabu, který si člověk otevřel jako Codex. Tab by se tvářil
    správně a běželo by v něm něco jiného, což je horší než chyba.
    """
    if os.path.isfile(AGENT_WRAPPER):
        return AGENT_WRAPPER
    here = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "agent-wrapper.sh")
    return here if os.path.isfile(here) else ""


# Id konverzace Claude Code (jméno přepisu). Z prohlížeče do příkazu jde jen tohle.
SESSION_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


# Pokyn pro firemní tab. Souhlas se zápisem dal uživatel tím, že tab otevřel —
# platí ale jen na to, o co si v té konverzaci řekne: osobní poznámky, přístupy
# ani nastavení napojení do společného trezoru nepatří.
FIRMA_TAB_PROMPT = """Jsi ve firemním tabu Paluby (HUB_VAULT=firma).

- Pracuješ nad **firemním Obsidianem** celého týmu: čti z něj a nové poznámky do
  něj posílej příkazem `python3 {tool} navrh "cesta/nazev.md" - ` (obsah na
  standardní vstup). V tomhle tabu se poznámka nahraje rovnou, bez karty.
- Nahrávej **jen to, o co uživatel v téhle konverzaci požádal**. Nikdy tam sám
  od sebe nekopíruj jeho osobní poznámky z osobního Obsidianu.
- Do firemního Obsidianu **nikdy** nedávej hesla, klíče, tokeny, přístupy ani
  nastavení napojení (MCP, Google, účty) — ani když o to uživatel požádá
  mimochodem; nejdřív se zeptej a napiš, proč to tam nepatří.
- Osobní Obsidian uživatele je jeho — pro práci s ním má vedle tab
  „Server osobní"."""


FIRMA_TAB_PROMPT_FIRMY = """Jsi ve firemním tabu Paluby pro firmu **{nazev}**
(HUB_VAULT=firma:{zkratka}, trezor `{cesta}`, tvoje role: {role}).

- Pracuješ jen pro tuhle firmu. Čti z jejího Obsidianu; nic z ní nepiš do jiné
  firmy ani do osobního Obsidianu a nic z jiných firem nenos sem.
- Než do složky firmy cokoli zapíšeš, přečti si `CLAUDE.md` v ní a ve všech
  složkách nad ní až po kořen trezoru firmy, a drž se jich.
- Práce je v `Klienti/<klient>/<podkategorie>/` (agentura), nebo v
  `Projekty/<projekt>/` (firma pro sebe). Vidíš jen to, k čemu má uživatel
  přístup; o další přístup se žádá kapitán firmy.
- Nové poznámky posílej `python3 {tool} navrh "cesta/nazev.md" - ` (obsah na
  standardní vstup) — v tomhle tabu se nahrají rovnou, bez karty.{host}
- Nahrávej jen to, o co uživatel v téhle konverzaci požádal. **Nikdy** do firmy
  nedávej hesla, klíče, tokeny ani přístupy — patří do napojení (MCP)."""


def firma_tab_prompt(vault="firma", path=""):
    tool = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "tools", "firma.py")
    f = firma_info(vault)
    if vault == "firma" or not f:
        return FIRMA_TAB_PROMPT.format(tool=tool)
    host = ("\n- Ve firmě jsi **host** — jen čteš; zápis brána odmítne."
            if f["role"] == "host" else "")
    text = FIRMA_TAB_PROMPT_FIRMY.format(nazev=f["nazev"], zkratka=f["zkratka"], cesta=f["cesta"],
                                         role={"kapitan": "kapitán", "clen": "člen"}.get(
                                             f["role"], f["role"]), tool=tool, host=host)
    klient, podkat = firma_prace(path, f["zkratka"])
    if klient:
        # Firma s projekty má jednu úroveň (Projekty/<projekt>), agentura dvě.
        rel = ("Projekty/" + klient if f["druh"] == "projekty"
               else "Klienti/" + klient + ("/" + podkat if podkat else ""))
        text += (f"\n\n**Tenhle chat je pro {klient}" + (f" › {podkat}" if podkat else "") + "**. "
                 f"Dokumentace k němu je v `{f['cesta']}/{rel}/` — než začneš, přečti si tam "
                 "`CLAUDE.md` (i ve složkách nad ní) a co už je hotové. Nové poznámky k téhle práci "
                 f"patří do `{rel}/`. Pracovní soubory (kód, exporty) dělej v aktuální složce "
                 f"`{os.path.abspath(path)}` — ta je jen v prostoru uživatele.")
    return text


def cmd_agent(path, agent_id="", slash="", model="", resume="", fork=False, vault="",
              prompt="", launch=None):
    """(příkaz pro tab, proměnné prostředí) — tudy se spouští každý agent.
    `resume` = pokračovat v uložené konverzaci (seznam konverzací), `fork` =
    jako její kopie, `prompt` = úvodní zpráva (úkol ze serveru, hub/pocitac.py)."""
    spec = agent_spec(agent_id)
    # Možnost bypassu jen po potvrzeném varování (bypass_accepted) — jinak by
    # se Claude Code při každém startu ptal a Enter by ho ukončil.
    bypass = bool(agents.bypass_arg(spec)) and bypass_accepted()
    # Tab s úkolem ze serveru (hub/pocitac.py, tab_launch): hook pro souhlas
    # ze serveru, mimo plný přístup bez bypassu a v režimu `default`.
    if launch and (spec.get("id") or "") == "claude":
        bypass = bypass and launch["bypass"]
    else:
        launch = None
    resume = str(resume or "") if SESSION_ID.fullmatch(str(resume or "")) else ""
    # Id nové konverzace si volí hub. Pak ví hned, do kterého přepisu tab píše,
    # a čtení (hub/cteni.py) ho nemusí hádat podle času souborů. U pokračování
    # i kopie si id volí Claude Code sám, tam se nic nevnucuje.
    session = "" if resume else str(uuid.uuid4())
    argv = agents.launch_args(spec, model, slash or prompt, bypass=bypass, resume=resume,
                              fork=fork, session=session, extra=(launch or {}).get("args"))
    # Firemní tab: agent dostane pokyn pro tohle sezení a nástroj firma.py podle
    # HUB_VAULT pozná, že se nahrává rovnou.
    firma = (vault == "firma" and bool(company_vault())) or (
        str(vault).startswith("firma:") and is_firma_vault(vault))
    if firma:
        # Pracovní složka chatu firmy (~/firmy/<zkratka>/…) vzniká, až je potřeba.
        f = firma_info(vault)
        root = os.path.join(FIRMY_PRACE, f["zkratka"]) if f else ""
        where = os.path.abspath(str(path or ""))
        if root and (where == root or where.startswith(root + os.sep)):
            try:
                os.makedirs(where, exist_ok=True)
            except OSError:
                pass
    if firma and spec.get("system_arg"):
        argv += [part.replace("{text}", firma_tab_prompt(vault, path))
                 for part in spec["system_arg"].split(" ") if part]
    wrapper = _wrapper_path()
    if not wrapper:
        # Radši prázdný shell s vysvětlením než spustit někoho jiného.
        return (f'cd {sh_quote(to_shell_path(path or HOME))}; '
                f'echo "Chybí agent-wrapper.sh — spusť instalaci hubu znovu '
                f'(install.sh)."; exec bash'), {}
    p = sh_quote(to_shell_path(path or HOME))
    w = sh_quote(to_shell_path(wrapper))
    args = "".join(" " + sh_quote(a) for a in argv)
    script = (f'cd {p} && bash {w}{args}; '
              f'echo; echo "[ session ukončena — tab zůstává jako shell, claude + Enter ho spustí znovu ]"; exec bash')
    env = agents.env_for(spec, model)
    # Barva úvodní hlavičky v tabu říká totéž co tečka na tabu: KDE se píše.
    # Nad firemním trezorem fialově, v prostoru na serveru modře, na počítači
    # jantarově. Jiný než výchozí agent si drží svou barvu — podle ní se pozná.
    accent = ""
    if firma:
        accent = DARK["FIRMA"]
    elif on_gateway() and (spec.get("id") or "") == default_agent():
        accent = DARK["CLOUD"]
    if accent:
        env["HUB_AGENT_RGB"] = hex_rgb(accent)
    if bypass:
        env["HUB_AGENT_BYPASS"] = "1"     # tab ví, že bypass v něm jde zapnout
    if resume and spec.get("resume_arg"):
        env["HUB_AGENT_RESUME"] = resume  # ve které konverzaci tab pokračuje
    if firma:
        env["HUB_VAULT"] = vault
    if session and spec.get("session_arg"):
        env["HUB_SESSION_ID"] = session
    if launch:
        env.update(launch["env"])
    return script, env


def cmd_install(agent_id):
    """Instalace agenta běží jako tab, ne skrytě na pozadí.

    Instalační příkazy stahují a spouštějí cizí kód (npm, curl | bash), takže
    je správně, aby bylo vidět, co se děje, a dalo se to přerušit.
    """
    spec = agent_spec(agent_id)
    cmd = agents.install_cmd(spec)
    label = spec.get("label") or spec.get("id") or "agent"
    if not cmd:
        return (f'echo "Pro {sh_quote(label)} tady instalační příkaz nemám."; exec bash')
    return (f'echo "Instaluji {label}:"; echo "  {cmd}"; echo; '
            f'{cmd}; '
            f'echo; echo "[ hotovo — zavři tab a agent se objeví v nabídce ]"; exec bash')


def cmd_auth(agent_id):
    """Přihlášení k agentovi — spustí jeho vlastní příkaz nebo rovnou jeho TUI."""
    spec = agent_spec(agent_id)
    auth = spec.get("auth") or {}
    cmd = auth.get("cmd")
    if not cmd:
        note = auth.get("note") or "Tenhle agent přihlášení nepotřebuje."
        return f'echo {sh_quote(note)}; exec bash'
    hint = auth.get("slash")
    lead = f'echo "Pak zvol {hint}."; ' if hint else ""
    return f'{lead}{cmd}; echo; exec bash'


def cmd_project(path, slash=""):
    """Výchozí agent nad projektem. Kdo chce jiného, volá cmd_agent()."""
    script, _env = cmd_agent(path, "", slash)
    return script


def cmd_shell():
    return "exec bash"


def cmd_deploy(path):
    """FTP script when the project has .ftp-deploy.json, otherwise the /deploy skill."""
    if os.path.isfile(os.path.join(path, ".ftp-deploy.json")) and \
            os.path.isfile(FTP_DEPLOY):
        return (f'bash {sh_quote(to_shell_path(FTP_DEPLOY))} '
                f'{sh_quote(to_shell_path(path))}; '
                f'echo; echo "[ deploy hotový — tab zůstává jako shell ]"; exec bash')
    return cmd_project(path, "/deploy")


def link_selftest():
    """Zkusí nanečisto vyrobit odkaz na složku. (jak, detail)

    Na Windows je to jediná odpověď, která něco znamená: symlink tam chce práva
    správce nebo vývojářský režim, křižovatka nechce nic — a co z toho na tomhle
    stroji projde, se nedá uhádnout, jen vyzkoušet.
    """
    import tempfile
    try:
        with tempfile.TemporaryDirectory() as tmp:
            target = os.path.join(tmp, "cil")
            os.makedirs(target)
            link = os.path.join(tmp, "odkaz")
            how = _make_link(target, link)
            ok = os.path.realpath(link) == os.path.realpath(target)
            return (how if ok else ""), ("" if ok else "odkaz nevede na cíl")
    except Exception as exc:
        return "", str(exc)[:200]


def memory_link_state():
    """Jak je na tom napojení paměti: (stav, detail).

    Hádaná složka je slabé místo na Windows — jméno si Claude Code tvoří sám
    a my ho můžeme leda najít. Když tam ještě není, je poctivější to říct než
    tvářit se, že je napojeno.
    """
    link = memory_link_path()
    parent = os.path.dirname(link)
    if _is_link(link):
        return "napojeno", os.path.realpath(link)
    if os.path.isdir(link):
        return "vlastní složka", link
    if os.path.isdir(parent):
        return "nenapojeno", parent
    return "složka Claude Code zatím není", parent


def browser_profile():
    """Společný profil prohlížeče pro Playwright MCP.

    Musí sedět s tools/playwright_profile.py — tam ho zakládají obě instalačky.
    """
    return os.path.join(CLAUDE_DIR, "browser-profile")


def browser_state():
    """Jak je na tom prohlížeč pro Claude Code: (stav, detail).

    Playwright MCP si bez `--user-data-dir` odvozuje profil z pracovní složky,
    takže každý projekt dostane vlastní prohlížeč a přihlášení (Google a spol.)
    zmizí s přepnutím tabu. Registraci drží ~/.claude.json.
    """
    for path in (CLAUDE_DIR.rstrip("/\\") + ".json",
                 os.path.join(CLAUDE_DIR, ".claude.json")):
        try:
            with open(path, encoding="utf-8-sig") as fh:
                servers = json.load(fh).get("mcpServers") or {}
        except (OSError, ValueError):
            continue
        entry = servers.get("playwright")
        if not entry:
            return "neregistrovaný", "volitelný — přidá ho instalačka"
        args = " ".join(str(a) for a in (entry.get("args") or []))
        if "--user-data-dir" in args:
            return "přihlášení se drží", browser_profile()
        return "profil podle složky", "přihlášení se ztrácí — spusť instalačku znovu"
    return "neregistrovaný", "~/.claude.json se nepodařilo přečíst"


# ── Napojení na cizí služby (MCP) ────────────────────────────────────────────
# Zdrojem pravdy je `claude mcp list`. Jako jediný ví i o konektorech z účtu
# („claude.ai …"), které na disku v žádném souboru nejsou, a rovnou každý server
# zkusí oslovit — takže odpoví i na to, co se jen tváří zaregistrovaně. Stojí to
# jednotky sekund, proto to jede na pozadí a výsledek se drží v úloze "mcp".

# Co se dá přidat jedním klikem. Klíč = jméno serveru u `claude mcp add`.
#
# Dva druhy: `http` je hotová služba na síti, `stdio` je program, který si
# Claude Code spustí sám (uvx/npx si balíček stáhnou při prvním spuštění, nic
# se nemusí instalovat dopředu). `fields` jsou údaje, na které se hub zeptá —
# `target` říká, kam patří: do proměnné prostředí, do hlavičky, nebo za příkaz
# jako argument.
#
# Do katalogu patří jen to, co je opravdu k mání: každý balíček tady byl
# ověřený v registru, každá adresa oslovená.
MCP_CATALOG = {
    "google-workspace": {
        "label": "Google Workspace",
        # Pro člověka je Google v Napojení → služby (hub/connect.py), s účty
        # a přihlášením. Tady zůstává kvůli návodu na klienta OAuth.
        "service": "google",
        "note": "Gmail, Disk, Dokumenty, Tabulky, Kalendář, Slides, Formuláře, "
                "Úkoly, Kontakty a Apps Script — čtení i zápis do buněk.",
        "kind": "stdio",
        "needs": "uvx",
        "command": "uvx",
        "args": ["workspace-mcp", "--tool-tier", "extended"],
        "source": "taylorwilsdon/google_workspace_mcp",
        "license": "MIT",
        "fields": [
            {"name": "GOOGLE_OAUTH_CLIENT_ID", "target": "env",
             "label": "Client ID",
             "help": "Google Cloud Console → API a služby → Přihlašovací údaje "
                     "→ Vytvořit → ID klienta OAuth → typ Desktopová aplikace"},
            {"name": "GOOGLE_OAUTH_CLIENT_SECRET", "target": "env",
             "label": "Client secret", "secret": True,
             "help": "Ze stejné obrazovky jako Client ID."},
        ],
        "docs": "https://github.com/taylorwilsdon/google_workspace_mcp",
        # Google klienta OAuth nerozdá — musí si ho každý naklikat ve své
        # konzoli. Průvodce aspoň ví, kam přesně, takže se nikde nehledá.
        "setup": [
            {"title": "Založ projekt v Google Cloudu",
             "text": "Na jméně nezáleží, třeba „Claude Code“. Když už nějaký "
                     "projekt máš, tenhle krok přeskoč.",
             "button": "Otevřít konzoli",
             "url": "https://console.cloud.google.com/projectcreate"},
            {"title": "Zapni API, na která to sahá",
             "text": "Jedním kliknutím všech deset naráz: Disk, Tabulky, "
                     "Dokumenty, Gmail, Kalendář, Slides, Úkoly, Kontakty, "
                     "Formuláře a Apps Script. Nahoře zkontroluj, že jsi ve "
                     "správném projektu.",
             "button": "Zapnout API",
             "url": "https://console.cloud.google.com/flows/enableapi?apiid="
                    "drive.googleapis.com,sheets.googleapis.com,"
                    "docs.googleapis.com,gmail.googleapis.com,"
                    "calendar-json.googleapis.com,slides.googleapis.com,"
                    "tasks.googleapis.com,people.googleapis.com,"
                    "forms.googleapis.com,script.googleapis.com"},
            {"title": "Vyplň přihlašovací obrazovku",
             "text": "Typ Externí, název aplikace a tvůj e-mail. Pak se ještě "
                     "přidej mezi testovací uživatele — k vlastní aplikaci tě "
                     "Google jinak nepustí.",
             "button": "Otevřít obrazovku",
             "url": "https://console.cloud.google.com/apis/credentials/consent"},
            {"title": "Vytvoř klienta OAuth",
             "text": "Typ Desktopová aplikace. Adresy pro přesměrování se u ní "
                     "neřeší. Po vytvoření se ukáže Client ID a Client secret "
                     "— ty patří sem dolů.",
             "button": "Vytvořit klienta",
             "url": "https://console.cloud.google.com/apis/credentials/"
                    "oauthclient"},
        ],
        "warn": "Při prvním použití tě Google pošle odsouhlasit oprávnění a "
                "cestou ukáže „Tuto aplikaci Google neověřil“. U vlastního "
                "klienta v testovacím režimu je to normální — Rozšířená "
                "nastavení → Přejít na…",
    },
    "google-sheets": {
        "label": "Google Tabulky",
        "note": "Jen tabulky, zato pořádně — čte i zapisuje rozsahy buněk a "
                "umí rozeznat vzorec od vypočtené hodnoty.",
        "kind": "stdio",
        "needs": "uvx",
        "command": "uvx",
        "args": ["mcp-google-sheets@latest"],
        "source": "xing5/mcp-google-sheets",
        "license": "MIT",
        "fields": [
            {"name": "SERVICE_ACCOUNT_PATH", "target": "env",
             "label": "Cesta k JSON klíči služebního účtu",
             "help": "Google Cloud Console → Servisní účty → Klíče → "
                     "Přidat klíč (JSON). Tabulku pak nasdílej na e-mail "
                     "toho účtu."},
        ],
        "docs": "https://github.com/xing5/mcp-google-sheets",
        "setup": [
            {"title": "Založ servisní účet",
             "text": "Je to „robot“ s vlastní e-mailovou adresou. Na rozdíl od "
                     "OAuth se nepřihlašuje člověk, takže odpadá souhlasná "
                     "obrazovka.",
             "button": "Vytvořit účet",
             "url": "https://console.cloud.google.com/iam-admin/"
                    "serviceaccounts/create"},
            {"title": "Stáhni mu klíč",
             "text": "U hotového účtu → Klíče → Přidat klíč → JSON. Soubor si "
                     "ulož někam, kde zůstane — jeho cesta patří sem dolů.",
             "button": "Otevřít účty",
             "url": "https://console.cloud.google.com/iam-admin/"
                    "serviceaccounts"},
            {"title": "Nasdílej mu tabulku",
             "text": "V tabulce dej Sdílet a vlož e-mail servisního účtu "
                     "(končí na .iam.gserviceaccount.com). Bez toho robot "
                     "nevidí nic — a to je záměr."},
        ],
    },
    "context7": {
        "label": "Context7",
        "note": "Aktuální dokumentace knihoven přímo do kontextu — místo "
                "hádání z paměti, která je ke dni vydání modelu.",
        "kind": "http",
        "url": "https://mcp.context7.com/mcp",
        "source": "upstash/context7",
        "license": "MIT",
        "docs": "https://github.com/upstash/context7",
    },
    "fetch": {
        "label": "Fetch",
        "note": "Stáhne stránku a převede ji na text, se kterým se dá "
                "pracovat. Bez klíčů a bez přihlašování.",
        "kind": "stdio",
        "needs": "uvx",
        "command": "uvx",
        "args": ["mcp-server-fetch"],
        "source": "modelcontextprotocol/servers",
        "license": "MIT",
        "docs": "https://github.com/modelcontextprotocol/servers/tree/main/"
                "src/fetch",
    },
    "filesystem": {
        "label": "Souborový systém",
        "note": "Čtení a zápis v jedné vybrané složce — i mimo projekt, "
                "ve kterém je zrovna otevřený tab.",
        "kind": "stdio",
        "needs": "npx",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-filesystem"],
        "source": "modelcontextprotocol/servers",
        "license": "MIT",
        "fields": [
            {"name": "path", "target": "arg", "label": "Složka",
             "help": "Server dovnitř téhle složky vidí a nikam jinam."},
        ],
        "docs": "https://github.com/modelcontextprotocol/servers/tree/main/"
                "src/filesystem",
    },
    "memory": {
        "label": "Paměť (graf znalostí)",
        "note": "Trvalý graf entit a vztahů. Vedle Obsidian paměti hubu — "
                "tahle je strojová, ta v Obsidianu psaná pro člověka.",
        "kind": "stdio",
        "needs": "npx",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-memory"],
        "source": "modelcontextprotocol/servers",
        "license": "MIT",
        "docs": "https://github.com/modelcontextprotocol/servers/tree/main/"
                "src/memory",
    },
    "sequential-thinking": {
        "label": "Sekvenční uvažování",
        "note": "Rozloží těžší úlohu na kroky, ke kterým se dá vracet "
                "a opravovat je.",
        "kind": "stdio",
        "needs": "npx",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-sequential-thinking"],
        "source": "modelcontextprotocol/servers",
        "license": "MIT",
        "docs": "https://github.com/modelcontextprotocol/servers/tree/main/"
                "src/sequentialthinking",
    },
    "clockify": {
        "label": "Clockify",
        "note": "Výkazy času, projekty a spuštěné stopky přímo z Claude Code.",
        "kind": "http",
        "url": "https://api.clockify.me/mcp-server/mcp",
        "fields": [
            {"name": "x-api-key", "target": "header", "secret": True,
             "label": "API klíč z Clockify",
             "help": "Clockify → foto profilu → Preferences → Advanced → "
                     "Manage API keys → Generate New"},
        ],
        "docs": "https://clockify.me/help/integrations-and-add-ons/"
                "use-clockify-mcp-server-to-connect-to-ai-agent",
        "setup": [
            {"title": "Vygeneruj API klíč",
             "text": "Preferences → Advanced → Manage API keys → Generate New. "
                     "Klíč se ukáže jednou, tak ho rovnou vlož sem dolů.",
             "button": "Otevřít Clockify",
             "url": "https://app.clockify.me/user/settings"},
        ],
    },
}


_MCP_STATES = (
    # (co hledat ve zbytku řádku, stav, český popis)
    ("needs authentication", "auth", "chce přihlásit"),
    ("authentication", "auth", "chce přihlásit"),
    ("failed", "fail", "nepřipojeno"),
    ("connected", "ok", "připojeno"),
)


def _mcp_status(text):
    low = text.lower()
    for needle, state, label in _MCP_STATES:
        if needle in low:
            return state, label
    return "unknown", text.strip("✔✘✗! ").strip() or "neznámý stav"


def _claude_json():
    """Obsah ~/.claude.json — vlastní soubor Claude Code, my do něj nesaháme.

    Se `CLAUDE_CONFIG_DIR` leží uvnitř té složky, jinak vedle ní. Bere se první,
    který se povede přečíst; prázdno znamená „ještě nic nenapsal".
    """
    for path in (CLAUDE_DIR.rstrip("/\\") + ".json",
                 os.path.join(CLAUDE_DIR, ".claude.json")):
        try:
            with open(path, encoding="utf-8-sig") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            continue
        return data if isinstance(data, dict) else {}
    return {}


# Jak se jmenuje předplatné, které si Claude Code zapsal jako typ organizace.
_PLANS = {"claude_max": "Max", "claude_pro": "Pro", "claude_free": "Free",
          "claude_team": "Team", "claude_enterprise": "Enterprise"}


def claude_account():
    """Pod jakým účtem claude.ai je Claude Code na tomhle stroji přihlášený.

    Konektory („claude.ai Gmail" a spol.) nejsou v žádném souboru — visí na
    účtu. Druhá schránka se k nim proto nepřidá vedle první: napojí se
    v claude.ai pod tím účtem, nebo se přepne účet celý. Aby to v seznamu
    nevisely bez souvislosti, ukáže hub aspoň, ke komu patří.
    """
    acct = _claude_json().get("oauthAccount")
    if not isinstance(acct, dict):
        return {}
    email = acct.get("emailAddress") or ""
    org = acct.get("organizationName") or ""
    # Osobní účet má organizaci pojmenovanou po e-mailu — dvakrát totéž nikomu
    # nic neřekne.
    if email and org.startswith(email):
        org = ""
    return {"email": email,
            "name": acct.get("fullName") or acct.get("displayName") or "",
            "org": org,
            "plan": _PLANS.get(acct.get("organizationType") or "", "")}


def mcp_scopes():
    """Odkud se který server registruje — čte se jen z disku, bez sítě.

    `claude mcp list` scope neukazuje, ale právě podle něj se pozná, co si
    můžeme odregistrovat sami (user scope) a co leží v účtu na claude.ai.
    """
    user, project = set(), {}
    data = _claude_json()
    user |= set((data.get("mcpServers") or {}).keys())
    for proj, meta in (data.get("projects") or {}).items():
        if not isinstance(meta, dict):
            continue
        for name in (meta.get("mcpServers") or {}):
            project.setdefault(name, proj)
    return user, project


def mcp_project_files():
    """Servery z `.mcp.json` v projektech — platí jen v té složce.

    V seznamu z hubu (běží z domovské složky) se neobjeví, takže by jinak
    vypadaly jako neexistující. Tady jsou vidět i s tím, kam patří.
    """
    found = []
    for base in PROJECT_DIRS:
        if not os.path.isdir(base):
            continue
        try:
            entries = sorted(os.listdir(base))
        except OSError:
            continue
        for d in entries:
            path = os.path.join(base, d, ".mcp.json")
            if not os.path.isfile(path):
                continue
            try:
                with open(path, encoding="utf-8-sig") as fh:
                    servers = json.load(fh).get("mcpServers") or {}
            except (OSError, ValueError):
                continue
            for name, entry in servers.items():
                found.append({"name": name, "project": os.path.join(base, d),
                              "target": _mcp_target(entry)})
    return found


def _mcp_target(entry):
    """Krátký popis, na co server sahá — URL, nebo příkaz, který se spouští."""
    if not isinstance(entry, dict):
        return ""
    if entry.get("url"):
        return entry["url"]
    argv = [entry.get("command") or ""] + [str(a) for a in (entry.get("args") or [])]
    return " ".join(a for a in argv if a).strip()


def mcp_list():
    """Seznam napojení i s tím, jestli odpovídají. Nikdy nevyhazuje."""
    claude = shutil.which("claude")
    if not claude:
        return {"ok": False, "detail": "Claude Code CLI (claude) není v PATH.",
                "servers": [], "counts": {}}

    # Zdravotní kontrola oslovuje každý server zvlášť; 9 s bývá běžných,
    # minuta je strop, aby se úloha nezasekla na jednom mrtvém serveru.
    out = run([claude, "mcp", "list"], cwd=HOME, timeout=90)
    user, project = mcp_scopes()
    servers = []
    for line in out.split("\n"):
        line = line.strip()
        if not line or ":" not in line or " - " not in line:
            continue          # hlavička „Checking MCP server health…" a prázdné
        name, rest = line.split(":", 1)
        target, _, status_text = rest.rpartition(" - ")
        name = name.strip()
        state, label = _mcp_status(status_text)
        if name.startswith("claude.ai "):
            scope, where = "account", "účet claude.ai"
        elif name in user:
            scope, where = "user", "všechny projekty"
        elif name in project:
            scope, where = "project", project[name]
        else:
            scope, where = "", ""
        servers.append({"name": name, "target": target.strip(),
                        "state": state, "status": label,
                        "scope": scope, "where": where,
                        "removable": scope == "user" and name not in ("playwright", "pocitac")
                        and not name.startswith("sdilene-")})

    seen = {s["name"] for s in servers}
    for entry in mcp_project_files():
        if entry["name"] in seen:
            continue
        servers.append({"name": entry["name"], "target": entry["target"],
                        "state": "local", "status": "jen v tomhle projektu",
                        "scope": "project", "where": entry["project"],
                        "removable": False})

    counts = {"total": len(servers)}
    for s in servers:
        counts[s["state"]] = counts.get(s["state"], 0) + 1
    # Co z katalogu ještě chybí — UI z toho dělá tlačítka „Přidat".
    # Co má kartu mezi službami (hub/connect.py), se tu podruhé nenabízí.
    # `clockify-krekrrr` a `clockify-osobni` jsou pořád Clockify: kdo má víc
    # účtů, pojmenuje si servery po nich a katalog je nemá nabízet znovu.
    missing = [k for k in MCP_CATALOG if not MCP_CATALOG[k].get("service") and not any(
        s["name"] == k or s["name"].startswith((k + " ", k + "-")) for s in servers)]
    return {"ok": True, "servers": servers, "counts": counts,
            "available": missing, "checked": time.time()}


def mcp_prereq(spec):
    """Čím se server spouští, když je to program. '' = není co hlídat."""
    need = spec.get("needs") or ""
    if not need or shutil.which(need):
        return ""
    hints = {"uvx": "uv (uvx) — https://docs.astral.sh/uv/",
             "npx": "Node.js (npx) — https://nodejs.org"}
    return hints.get(need, need)


def mcp_add(name, values=None, scope="user", path=""):
    """Zaregistruje server z katalogu. Vrací {ok, detail}.

    `values` jsou odpovědi na `fields` ze spisu, klíčované jménem pole. Scope
    `user` platí všude, `project` zapíše .mcp.json do složky projektu — ten se
    pak veze s repem, takže ho má i další člověk v týmu.
    """
    spec = MCP_CATALOG.get(name)
    if not spec:
        return {"ok": False, "detail": f"Napojení {name} neznám."}
    claude = shutil.which("claude")
    if not claude:
        return {"ok": False, "detail": "Claude Code CLI (claude) není v PATH."}
    if scope not in ("user", "project", "local"):
        return {"ok": False, "detail": f"Neznámý rozsah {scope}."}

    missing = mcp_prereq(spec)
    if missing:
        return {"ok": False, "detail": f"Chybí {missing}"}

    cwd = HOME
    if scope == "project":
        cwd = os.path.expanduser(path or "")
        if not os.path.isdir(cwd):
            return {"ok": False, "detail": "Pro rozsah projektu chybí složka."}

    values = values or {}
    env, headers, extra_args = [], [], []
    for field in spec.get("fields") or []:
        key = field["name"]
        value = str(values.get(key) or "").strip()
        if not value:
            return {"ok": False, "detail": f"Chybí {field.get('label') or key}."}
        target = field.get("target") or "env"
        if target == "env":
            env += ["-e", f"{key}={value}"]
        elif target == "header":
            headers += ["--header", f"{key}: {value}"]
        else:
            extra_args.append(value)

    if spec.get("kind") == "stdio":
        # `--` odděluje spouštěný příkaz; bez něj by si jeho přepínače přebral
        # `claude mcp add` sám.
        argv = ([claude, "mcp", "add", name, "-s", scope] + env +
                ["--", spec["command"]] + list(spec.get("args") or []) +
                extra_args)
    else:
        argv = ([claude, "mcp", "add", name, spec["url"], "-s", scope,
                 "--transport", "http"] + headers)

    try:
        r = subprocess.run(argv, capture_output=True, text=True, cwd=cwd,
                           timeout=60, creationflags=_NO_WINDOW)
    except Exception as exc:
        return {"ok": False, "detail": str(exc)[:200]}
    if r.returncode != 0:
        # Klíč do logu nepatří, tak jen to, co řeklo CLI.
        detail = (r.stderr or r.stdout or "").strip()[:300] or "nepovedlo se"
        log(f"MCP {name}: registrace selhala — {detail}", "warn")
        return {"ok": False, "detail": detail}
    kde = "globálně" if scope == "user" else f"v {os.path.basename(cwd)}"
    log(f"MCP {name}: zaregistrováno ({scope})")
    return {"ok": True, "detail": f"{spec['label']} je napojený {kde}."}


def mcp_remove(name, scope="user", path=""):
    """Odregistruje server. Projektový se odebírá ve složce, kde je zapsaný."""
    claude = shutil.which("claude")
    if not claude:
        return {"ok": False, "detail": "Claude Code CLI (claude) není v PATH."}
    # Součást appky (prohlížeč, spojení s počítačem, sdílená napojení) —
    # spravuje je hub sám, odebráním by se rozbil.
    if name in ("playwright", "pocitac") or str(name).startswith("sdilene-"):
        return {"ok": False, "detail": "Tohle napojení je součást appky — odebrat nejde."}
    if scope == "project":
        cwd = os.path.expanduser(path or "")
        if not os.path.isdir(cwd):
            return {"ok": False, "detail": "Chybí složka projektu."}
    else:
        scope, cwd = "user", HOME
        user, _ = mcp_scopes()
        if name not in user:
            return {"ok": False, "detail": "Tenhle server tu nezaložil hub — "
                                           "odeber ho tam, kde je zapsaný."}
    out = run([claude, "mcp", "remove", name, "-s", scope], cwd=cwd, timeout=30)
    log(f"MCP {name}: odebráno ({scope})")
    return {"ok": True, "detail": out or f"{name} odebrán."}


def doctor():
    """What the hub found on this machine — shown in the UI when something is off."""
    how, detail = link_selftest()
    stav, kde = memory_link_state()
    browser, browser_detail = browser_state()
    # Jen jména z disku — jestli servery opravdu odpovídají, se ptá /api/mcp
    # na pozadí. Tenhle výpis se čte při každém načtení stránky a čekat na síť
    # by znamenalo čekat na každý mrtvý server.
    mcp_user, mcp_project = mcp_scopes()
    return {
        "link": how, "link_error": detail,
        "memory_link": stav, "memory_link_path": kde,
        "browser_mcp": browser, "browser_mcp_detail": browser_detail,
        "mcp_user": sorted(mcp_user),
        "mcp_project": sorted(mcp_project),
        "platform": "windows" if IS_WINDOWS else ("mac" if IS_MAC else "linux"),
        "bash": BASH,
        "git": GIT,
        "claude": shutil.which("claude") or "",
        # Jen co je v PATH — verze ani stav Ollamy se sem nedávají schválně:
        # tenhle výpis se čte při každém načtení stránky a ptát se šesti CLI
        # na --version by z toho udělalo čekání. Od toho je /api/agents.
        "agents": {a["id"]: a["path"]
                   for a in agents.detect(_agents_extra(), with_version=False)},
        "default_agent": default_agent(),
        "wrapper": WRAPPER if os.path.isfile(WRAPPER) else "",
        "agent_wrapper": _wrapper_path(),
        "ftp_deploy": FTP_DEPLOY if os.path.isfile(FTP_DEPLOY) else "",
        "brain": BRAIN if HAS_BRAIN else "",
        "clipboard": (_clipboard_tools("clipboard")[1] or [""])[0],
        "project_dirs": PROJECT_DIRS,
        "blocked_dirs": list(BLOCKED_DIRS),
    }


# ── Paměť se ukládá sama ─────────────────────────────────────────────────────
# Dřív se při zavírání tabu ptalo „uložit postup?" a do Brainu se sypal syrový
# konec výpisu terminálu (hub-postup.md narostl za týden na 2,9 MB). Teď to dělá
# hooks/memory-autosave.py: z přepisu konverzace si Claude na pozadí sám doplní
# poznámku k projektu a případný poznatek. Hub mu jen řekne, že se tab zavřel —
# session v tabu se pozná podle HUB_TAB, které Stop hook zapíše k session.
AUTOSAVE_DIR = os.path.join(CLAUDE_DIR, "hub-autosave")
AUTOSAVE_LOG = os.path.join(AUTOSAVE_DIR, "log.jsonl")
AUTOSAVE_SCRIPT = os.path.join(CLAUDE_DIR, "hooks", "memory-autosave.py")
# Id téhle instance hubu: čísla tabů začínají po každém startu od jedničky,
# takže samotné číslo by spojilo tab se session z minulého spuštění.
INSTANCE = os.urandom(4).hex()


def autosave_enabled():
    return HAS_BRAIN and CONFIG.get("memory_autosave", True) is not False


def _autosave_script():
    if os.path.isfile(AUTOSAVE_SCRIPT):
        return AUTOSAVE_SCRIPT
    here = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "hooks", "memory-autosave.py")
    return here if os.path.isfile(here) else ""


def tab_tag(sid):
    return f"{INSTANCE}:{sid}"


def autosave_tabs_closed(sids):
    """Zavřené taby → uložit jejich session. Nečeká se: běží to minuty."""
    script = _autosave_script()
    if not sids or not script or not autosave_enabled():
        return
    python = sys.executable or "python3"
    kwargs = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
              "stderr": subprocess.DEVNULL, "close_fds": True, "cwd": HOME}
    if IS_WINDOWS:
        quiet = os.path.join(os.path.dirname(python), "pythonw.exe")
        python = quiet if os.path.isfile(quiet) else python
        kwargs["creationflags"] = (subprocess.DETACHED_PROCESS |
                                   subprocess.CREATE_NEW_PROCESS_GROUP)
    else:
        # Vlastní session: hub může skončit hned po zavření okna a ukládání
        # nesmí skončit s ním.
        kwargs["start_new_session"] = True
    try:
        subprocess.Popen([python, script, "--tab", *[tab_tag(s) for s in sids]],
                         **kwargs)
    except OSError as exc:
        log_error("Automatické ukládání paměti se nespustilo", exc)


def autosave_recent(limit=6):
    """Poslední uložení do paměti, nejnovější první (jen ta, co něco zapsala
    nebo selhala — „nebylo co ukládat" nikoho nezajímá)."""
    try:
        with open(AUTOSAVE_LOG, encoding="utf-8") as fh:
            lines = fh.readlines()[-200:]
    except OSError:
        return []
    out = []
    for line in reversed(lines):
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if entry.get("status") in ("saved", "error"):
            out.append(entry)
        if len(out) >= limit:
            break
    return out
