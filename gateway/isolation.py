"""
Jak se pouští session uživatele, aby neviděla na cizí.

Claude Code umí číst soubory a spouštět příkazy. Na jednom stroji to je věc
jeho majitele; jakmile na bránu pustíš víc lidí, je to najednou způsob, jak si
můžou přečíst navzájem Obsidian, klíče i zbytek serveru. Izolace proto není
něco, co se doladí potom — bez ní brána nemá běžet.

Tři režimy, každý vrací hotové argv, kterým se session spustí:

  none    bez izolace. Na vývoj na vlastním stroji. Brána ho odmítne pustit,
          jakmile poslouchá jinam než na loopback — viz `check()`.
  bwrap   bubblewrap: session vidí systém jen ke čtení a ze zápisu jenom svůj
          domov. Nepotřebuje démona ani práva správce.
  docker  kontejner na session. Nejtvrdší hranice, ale chce Docker na stroji.

POZOR na jednu věc, kterou izolace nevyřeší: session se prokazuje přihlášením
Claude Code, které leží v domovské složce. Kdo v sandboxu spustí Claude Code,
dosáhne i na ten soubor. Mezi kolegy, kteří si tak jako tak věří, to je únosné;
pro cizí lidi je jediná čistá cesta API klíč, který drží brána a session ho
nikdy nevidí.
"""
import os
import shutil
import subprocess
import time

MODES = ("none", "bwrap", "docker")

# Každý prostor běží ve vlastní systemd scope se jménem podle účtu. Podle jména
# se pak pozná, jestli ještě běží, a zastaví se celý najednou — viz stop_scope.
SCOPE_PREFIX = "claude-hub-"
# Každý prostor pod vlastním systémovým účtem (hub-u<id>) místo společného
# `hub`. Zapíná se HUB_GW_UCTY=1; bez toho se nic nemění. Spouští to úzký
# spouštěč gateway/prostor.py přes sudo — brána sama práva přepnout uživatele
# nemá a dát jí je obecně by znamenalo root pro kohokoli, kdo ji ovládne.
PER_USER = os.environ.get("HUB_GW_UCTY", "0") == "1"
SPOUSTEC = "/usr/local/sbin/claude-hub-prostor"
DOCKER_IMAGE = "claude-hub-workspace"

# Kolik si smí vzít jeden prostor. Strop platí na celý prostor — hub i se
# všemi taby uživatele —, ne na jednu session. Naměřeno 22. 9. 2026: jeden tab
# s Claude Code je kolem 850 MB (Claude ~480 MB + MCP servery, které si pouští)
# a 75 procesů a vláken. Pevných 1,5 GB a 256 úloh tak stačilo sotva na jeden
# tab: s osobním a firemním tabem (otevírají se samy) jádro zabíjelo Clauda
# uprostřed práce — tab se „sám zavřel", u jednoho prostoru 17× za týden.
# Paměť se proto bere podle stroje a stroj jako celek chrání strop na všechny
# prostory dohromady (install.sh → user@<hub>.service, MemoryMax=85 %).
PAMET_PODIL = 45            # % paměti stroje na jeden prostor
PAMET_MIN = 1500            # MB
PAMET_MAX = 6144            # MB


def _pamet_prostoru():
    if os.environ.get("HUB_GW_MEMORY"):
        return os.environ["HUB_GW_MEMORY"]
    try:
        with open("/proc/meminfo", encoding="ascii") as fh:
            for line in fh:
                if line.startswith("MemTotal:"):
                    mb = int(line.split()[1]) // 1024
                    return f"{max(PAMET_MIN, min(PAMET_MAX, mb * PAMET_PODIL // 100))}M"
    except (OSError, ValueError, IndexError):
        pass
    return f"{PAMET_MIN}M"


LIMITS = {"memory": _pamet_prostoru(),
          "tasks": int(os.environ.get("HUB_GW_TASKS") or 1024),
          "cpu_weight": 100}


def available():
    """Které režimy jde na tomhle stroji doopravdy použít."""
    out = ["none"]
    if shutil.which("bwrap"):
        out.append("bwrap")
    if shutil.which("docker"):
        out.append("docker")
    return out


def check(mode, host):
    """Vrátí důvod, proč tenhle režim nepustit, nebo ''.

    Pravidlo je jediné a tvrdé: bez izolace jen na loopback. Brána bez izolace
    vystavená do sítě je vzdálený shell pro kohokoli, kdo uhodne heslo.
    """
    if mode not in MODES:
        return f"Neznámý režim izolace {mode!r}, znám {', '.join(MODES)}."
    if mode not in available():
        return f"Režim {mode} tu není nainstalovaný."
    if mode == "none" and host not in ("127.0.0.1", "localhost", "::1"):
        return ("Bez izolace smí brána poslouchat jen na 127.0.0.1. "
                "Na síť nastav isolation na bwrap nebo docker.")
    return ""


def wrap(mode, argv, home, extra_ro=(), extra_rw=(), limits=None, unit="",
         aliases=(), ucet="", masks=(), tmp=""):
    """argv, kterým se session doopravdy spustí.

    `home` je domov uživatele na bráně — jediné místo, kam smí zapisovat.
    `extra_ro` jsou cesty, které má vidět jen ke čtení (zdroj hubu, sdílené
    přihlášení, když nemá přežít úpravy session). Místo cesty může být dvojice
    (zdroj, cíl) — v sandboxu je pak `zdroj` vidět pod jménem `cíl`.
    `extra_rw` jsou cesty vně domova, do kterých session **smí** zapsat — jediný
    případ je sdílené přihlášení Claude Code, které si samo obnovuje token, a
    obnovený token má vidět i zbytek session (jinak by po expiraci vypadly
    všechny naráz). Drž `extra_rw` co nejmenší; každá cesta v něm je díra
    v izolaci.
    `limits` je strop na paměť, procesy a podíl na procesoru; None = LIMITS.
    `unit` je jméno systemd scope (bez `.scope`), přes které se prostor pozná
    a zastaví; prázdné = náhodné, jako dřív.
    `aliases` jsou cesty, které mají v sandboxu vést na domov (odkaz) — stará
    jména domova, na která ukazují cesty zapečené v cache. Jen u bwrap.
    `masks` jsou hotové argumenty bwrap po přivázání `extra_ro` — skryté
    poznámky firemního trezoru (gateway/poznamky.py). Jen u bwrap.
    `tmp` je složka na disku, která bude v sandboxu jako /tmp; prázdné = /tmp
    v paměti (tmpfs). Jen u bwrap.
    """
    limits = LIMITS if limits is None else limits
    if mode == "none":
        return _limited(list(argv), limits, unit)
    if mode == "bwrap":
        sandbox = _bwrap(argv, home, extra_ro, extra_rw, aliases, masks, tmp)
        if PER_USER and ucet and unit:
            # Scope zakládá root ve SYSTÉMOVÉM manažeru (--uid), ne v uživatelském
            # manažeru účtu hub. Limity si spouštěč nastaví sám, proto `limits`
            # tudy neprochází.
            return ["sudo", "-n", SPOUSTEC, "spustit", ucet, unit, "--"] + sandbox
        return _limited(sandbox, limits, unit)
    if mode == "docker":
        # Docker si limity řeší sám, přes systemd by se počítaly dvakrát.
        return _docker(argv, home, extra_ro, limits, extra_rw)
    raise ValueError(f"Neznámý režim izolace: {mode}")


def _limited(argv, limits, unit=""):
    """Obalí příkaz cgroupou, aby jedna session neshodila zbytek serveru.

    bwrap odděluje, co session *vidí*, ale ne kolik si vezme — paměť ani
    procesy neomezuje vůbec. Na stroji, kde vedle běží weby a mail, je to
    zásadní: bez stropu stačí jedna ukecaná session a OOM killer sáhne po tom,
    co má nejvíc paměti, tedy nejspíš po databázi.

    MemorySwapMax=0 tam není navíc. Se samotným MemoryMax proces neumře, jen
    přeteče do swapu a celý stroj se začne plazit — naměřeno: bez něj 300 MB
    při stropu 200 MB v klidu projde, s ním skončí kódem 137.
    """
    if not limits or not shutil.which("systemd-run"):
        return argv
    scope = ["systemd-run", "--user", "--scope", "--quiet", "--collect"]
    if unit:
        scope += [f"--unit={unit}"]
    if limits.get("memory"):
        scope += ["-p", f"MemoryMax={limits['memory']}", "-p", "MemorySwapMax=0",
                  "-p", "OOMPolicy=continue"]
    if limits.get("tasks"):
        scope += ["-p", f"TasksMax={limits['tasks']}"]
    if limits.get("cpu_weight"):
        scope += ["-p", f"CPUWeight={limits['cpu_weight']}"]
    return scope + argv


def _bwrap(argv, home, extra_ro, extra_rw=(), aliases=(), masks=(), tmp=""):
    # /tmp na disku (workspace.tmp_na_disku), jinak v paměti. Tmpfs se počítá
    # do stropu paměti prostoru — velký soubor v /tmp ho uměl shodit.
    tmp_args = ["--bind", tmp, "/tmp"] if tmp and os.path.isdir(tmp) else ["--tmpfs", "/tmp"]
    cmd = ["bwrap",
           # Systém ke čtení. Zápis nikam mimo domov: i kdyby session někoho
           # napadlo sáhnout na /usr, nemá kam.
           "--ro-bind", "/usr", "/usr",
           "--ro-bind", "/etc", "/etc",
           "--proc", "/proc",
           "--dev", "/dev",
           *tmp_args,
           "--tmpfs", "/run",
           # Domov uživatele je jediné zapisovatelné místo a je na stejné
           # cestě jako venku, aby si Claude Code nestěžoval na jinou realitu.
           "--bind", home, home,
           "--setenv", "HOME", home,
           "--chdir", home,
           # Síť potřebujeme (API Anthropicu), všechno ostatní se odděluje.
           "--unshare-pid", "--unshare-ipc", "--unshare-uts",
           # POZN.: schválně BEZ `--die-with-parent`. Když bránu (systemd user
           # službu) pouští `systemd-run --user --scope`, systemd-run v tom
           # kontextu po založení scope skončí — a die-with-parent by pak
           # sandbox okamžitě zabil (session by umřela pár vteřin po startu).
           # Úklid proto řeší brána sama: zastaví celou scope, viz stop_scope.
           "--new-session"]
    for alias in aliases:
        if alias and alias != home:
            cmd += ["--symlink", home, alias]
    # /bin a /lib jsou na dnešních distribucích symlinky do /usr. Přeskočit je
    # nejde: v sandboxu by pak nebyl ani shell („execvp /bin/sh: No such file“).
    # Symlink se proto uvnitř vyrobí znovu, ne sváže.
    for path in ("/bin", "/sbin", "/lib", "/lib32", "/lib64"):
        if os.path.islink(path):
            cmd += ["--symlink", os.readlink(path), path]
        elif os.path.isdir(path):
            cmd += ["--ro-bind", path, path]
    # DNS. /etc/resolv.conf je na systemd distribucích symlink do /run, jenže
    # /run jsme právě přebili tmpfs — symlink by uvnitř visel do prázdna a
    # session by se nedostala na API. Vázat se proto musí složka, na kterou
    # symlink míří, ne on sám: bind na visící symlink bwrap odmítne
    # („Can't create file at /etc/resolv.conf“).
    resolv = os.path.realpath("/etc/resolv.conf")
    if resolv != "/etc/resolv.conf" and os.path.isfile(resolv):
        cmd += ["--ro-bind", os.path.dirname(resolv), os.path.dirname(resolv)]
    for path in extra_ro:
        # (zdroj, cíl) = přivázat jinou složku na známé místo — tak jede
        # prostor z připravené verze hubu, a uvnitř je to pořád REPO_DIR.
        src, dst = path if isinstance(path, tuple) else (path, path)
        if os.path.exists(src):
            cmd += ["--ro-bind", src, dst]
    for path in extra_rw:
        if os.path.exists(path):
            cmd += ["--bind", path, path]
    return cmd + list(masks) + ["--"] + list(argv)


# ── běžící prostory ──────────────────────────────────────────────────────────
# Proč ne `pkill` podle domova, jak to bylo dřív (naměřeno na Ubuntu 24.04):
#
#  * bwrap s `--unshare-pid` má uvnitř vlastní init — druhý proces bwrap, který
#    je v novém PID namespace číslo 1. Jádro takovému procesu doručí jen signály,
#    na které má obsluhu, a SIGTERM mezi ně nepatří. `pkill` zabil vnější bwrap,
#    vnitřní i s hubem a Claude Code běžel dál, jen už o něm nikdo nevěděl.
#  * Hub ani Claude Code uvnitř domov v příkazové řádce nemají, takže je vzor
#    nenašel vůbec.
#  * Proces, který brána spustí (`systemd-run`), se v kontextu služby od
#    sandboxu odpojí. Brána pak prostor považovala za mrtvý a spustila další.
#
# Scope se všechny tři problémy řeší naráz: je to cgroup, ve které jsou všechny
# procesy prostoru, ať je kdo spustil a jak se jmenují.

def _systemctl(*args, timeout=15):
    # `sudo -u hub python3 -m gateway.admin …` nemá proměnné uživatelské
    # session a `systemctl --user` by se neměl kam připojit.
    env = dict(os.environ)
    env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    env.setdefault("DBUS_SESSION_BUS_ADDRESS",
                   f"unix:path={env['XDG_RUNTIME_DIR']}/bus")
    try:
        # V režimu účtů žijí scope v systémovém manažeru (zakládá je root).
        base = ["systemctl"] if PER_USER else ["systemctl", "--user"]
        return subprocess.run([*base, *args], timeout=timeout,
                              capture_output=True, text=True, env=env)
    except (OSError, subprocess.TimeoutExpired):
        return None


def scope_cgroup(unit):
    """Cesta ke cgroupě běžící scope, nebo ''. Čte se z ní, jestli prostor žije
    — bez spouštění `systemctl` při každém požadavku."""
    if not unit or not shutil.which("systemctl"):
        return ""
    res = _systemctl("show", "-p", "ControlGroup", "--value", unit + ".scope")
    path = (res.stdout.strip() if res and res.returncode == 0 else "")
    return ("/sys/fs/cgroup" + path) if path else ""


def cgroup_alive(cgroup):
    """Běží v cgroupě ještě nějaký proces? Scope s `--collect` po posledním
    procesu zmizí i se složkou."""
    try:
        with open(os.path.join(cgroup, "cgroup.procs")) as fh:
            return bool(fh.read().strip())
    except OSError:
        return False


def scope_active(unit):
    res = _systemctl("is-active", "--quiet", unit + ".scope", timeout=5)
    return bool(res) and res.returncode == 0


def stop_scope(unit, grace=8):
    """Zastaví celý prostor: napřed slušně, pak natvrdo.

    SIGTERM dostanou všechny procesy scope — hub uloží, co má, a skončí, a
    s ním i init sandboxu. Co do `grace` sekund nedoběhne, dostane SIGKILL;
    ten jádro initu namespace doručí vždycky a zbytek namespace padne s ním.
    Vrací True, když scope už neběží.
    """
    if not unit or not shutil.which("systemctl"):
        return False
    name = unit + ".scope"
    if not scope_active(unit):
        return True
    if PER_USER:
        # Scope patří rootovi a procesy uvnitř cizímu uid — účet `hub` je sám
        # nezabije. Zastavení proto jde přes tentýž úzký spouštěč.
        subprocess.run(["sudo", "-n", SPOUSTEC, "zastavit", unit],
                       capture_output=True, timeout=30)
        for _ in range(int(grace / 0.3) + 1):
            if not scope_active(unit):
                return True
            time.sleep(0.3)
        return not scope_active(unit)
    _systemctl("kill", "--signal=SIGTERM", name)
    deadline = time.time() + grace
    while time.time() < deadline:
        if not scope_active(unit):
            return True
        time.sleep(0.3)
    _systemctl("kill", "--signal=SIGKILL", name)
    for _ in range(20):
        if not scope_active(unit):
            return True
        time.sleep(0.25)
    return False


def running_scopes(marker="claude-hub.py --no-browser"):
    """Běžící prostory: [(jméno bez .scope, popis)].

    Kromě pojmenovaných (`claude-hub-u<id>`) i ty z doby před pojmenováním,
    které mají náhodné `run-r….scope` — poznají se podle příkazu v popisu.
    """
    res = _systemctl("list-units", "--type=scope", "--state=active",
                     "--plain", "--no-legend", "--no-pager")
    out = []
    for line in (res.stdout.splitlines() if res and res.returncode == 0 else []):
        parts = line.split(None, 4)
        if not parts or not parts[0].endswith(".scope"):
            continue
        name = parts[0][:-len(".scope")]
        desc = parts[4] if len(parts) > 4 else ""
        if name.startswith(SCOPE_PREFIX) or marker in desc:
            out.append((name, desc))
    return out


def scope_memory(unit):
    """Kolik paměti prostor zrovna drží, v bajtech (0 = nevíme)."""
    try:
        with open(os.path.join(scope_cgroup(unit), "memory.current")) as fh:
            return int(fh.read().strip() or 0)
    except (OSError, ValueError):
        return 0


def _docker(argv, home, extra_ro, limits=None, extra_rw=()):
    limits = LIMITS if limits is None else limits
    cmd = ["docker", "run", "--rm", "-i",
           "--network", "bridge",
           # Bez práv navíc a bez možnosti si je vzít.
           "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
           # Aby jeden uživatel nemohl serveru sníst paměť ani procesor.
           "--memory", str(limits.get("memory", LIMITS["memory"])),
           "--memory-swap", str(limits.get("memory", LIMITS["memory"])),
           "--pids-limit", str(limits.get("tasks", LIMITS["tasks"])),
           "-v", f"{home}:{home}:rw",
           "-e", f"HOME={home}",
           "-w", home]
    for path in extra_ro:
        src, dst = path if isinstance(path, tuple) else (path, path)
        if os.path.exists(src):
            cmd += ["-v", f"{src}:{dst}:ro"]
    for path in extra_rw:
        if os.path.exists(path):
            cmd += ["-v", f"{path}:{path}:rw"]
    return cmd + [DOCKER_IMAGE] + list(argv)
