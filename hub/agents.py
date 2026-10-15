"""Katalog AI agentů, které hub umí otevřít v tabu.

Do 1.6 uměl hub jediné CLI: `cmd_project()` složil `bash claude-wrapper.sh`
a wrapper spustil `claude`. Tady je to samé, ale pro libovolný nástroj —
jedno místo, které ví, jak se který agent jmenuje, čím se instaluje, jak se
přihlašuje a co umí jeho bublina.

Katalog je zároveň to, co se posílá do UI (stejně jako `MCP_CATALOG` v core),
takže frontend jen renderuje, co dostane, a novým agentem se nic nepřepisuje.

Vlastní CLI se přidává do `~/.claude/hub-config.json`:

    "agents": {"crush": {"label": "Crush", "bin": "crush", "color": "#c084fc"}}

Klíč, který už v katalogu je, se doplní (merge), takže se dá přepsat jen barva
nebo instalační příkaz, ne celý záznam.
"""

import json
import os
import re
import shutil
import subprocess
import sys

IS_WINDOWS = os.name == "nt"
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if IS_WINDOWS else 0

OLLAMA_HOST = os.environ.get("OLLAMA_HOST") or "http://127.0.0.1:11434"

# Modely se úmyslně nevypisují u agentů, jejichž nabídku neumíme spolehlivě
# znát — u nich chip v bublině zavolá jejich vlastní `/model` a vybírá se
# v jejich TUI. Vypsat sem jména, která se u poskytovatele mění každé dva
# měsíce, by znamenalo nabízet modely, které už neexistují.
CATALOG = {
    "claude": {
        "label": "Claude Code",
        "short": "claude",
        "color": "#d97757",
        "ansi": 208,
        "bin": "claude",
        "note": "Agent od Anthropicu. Jede na předplatném Max/Pro nebo na API klíči.",
        "install": {
            "linux": "curl -fsSL https://claude.ai/install.sh | bash",
            "mac": "curl -fsSL https://claude.ai/install.sh | bash",
            "windows": "winget install --id Anthropic.ClaudeCode",
        },
        "auth": {"cmd": "claude", "slash": "/login",
                 "note": "Spustí se Claude Code, přihlášení je přes /login."},
        # Záloha, když se binárka Claude Code nedá přečíst (claude_models).
        "models": [["Opus 5.5", "opus"], ["Sonnet 5.5", "sonnet"],
                   ["Haiku 4.5", "haiku"], ["Fable 5.1", "fable"]],
        "models_from_bin": True,
        "model_arg": "--model {model}",
        "model_cmd": "/model {model}",
        # Bypass mezi režimy (Shift+Tab), ale nestartuje se v něm — zapne ho
        # člověk sám v bublině. Bez toho ho Claude Code do cyklu nepustí.
        "bypass_arg": "--allow-dangerously-skip-permissions",
        # Pokyn jen pro tohle sezení — hub jím říká, nad kterým Obsidianem tab jede.
        "system_arg": "--append-system-prompt {text}",
        # Pokračování v uložené konverzaci (seznam konverzací v hubu) a kopie
        # konverzace, která možná ještě běží jinde.
        "resume_arg": "--resume {id}",
        "fork_arg": "--fork-session",
        # Id konverzace si volí hub, ne Claude Code. Díky tomu ví, do kterého
        # přepisu tab píše, hned — a nemusí ho hádat podle času souborů.
        # Čtení (hub/cteni.py) z toho přepisu skládá konverzaci v okně.
        "session_arg": "--session-id {id}",
        "slash": ["/clear", "/compact", "/context", "/model", "/status",
                  "/resume", "/cost", "/help"],
        "skills": True,      # umí naše ~/.claude/skills
        "composer": "full",  # bublina umí dialogy, režimy i výšku pole
    },
    "codex": {
        "label": "Codex",
        "short": "codex",
        "color": "#10a37f",
        "ansi": 42,
        "bin": "codex",
        "note": "Agent od OpenAI. Jede na předplatném ChatGPT nebo na API klíči.",
        "install": {
            "linux": "npm install -g @openai/codex",
            "mac": "npm install -g @openai/codex",
            "windows": "npm install -g @openai/codex",
        },
        "auth": {"cmd": "codex login",
                 "note": "Přihlášení účtem ChatGPT; na stroji bez prohlížeče "
                         "má codex login kód do jiného zařízení."},
        "models": [],
        "model_arg": "-m {model}",
        "model_cmd": "/model",
        "slash": ["/model", "/approvals", "/new", "/status", "/help"],
        "skills": False,
        "composer": "basic",
    },
    "gemini": {
        "label": "Gemini",
        "short": "gemini",
        "color": "#4285f4",
        "ansi": 33,
        "bin": "gemini",
        "note": "Agent od Googlu. Přihlášení Google účtem nebo klíč z AI Studia.",
        "install": {
            "linux": "npm install -g @google/gemini-cli",
            "mac": "npm install -g @google/gemini-cli",
            "windows": "npm install -g @google/gemini-cli",
        },
        "auth": {"cmd": "gemini", "slash": "/auth",
                 "note": "Spustí se Gemini CLI, účet se vybírá v /auth."},
        "models": [],
        "model_arg": "-m {model}",
        "model_cmd": "/model",
        "slash": ["/auth", "/model", "/clear", "/stats", "/tools",
                  "/memory", "/help"],
        "skills": False,
        "composer": "basic",
    },
    "opencode": {
        "label": "opencode",
        "short": "opencode",
        "color": "#8b5cf6",
        "ansi": 141,
        "bin": "opencode",
        "note": "Otevřený agent, kterému se dá podstrčit jakýkoli model — "
                "včetně lokálního z Ollamy.",
        "install": {
            "linux": "curl -fsSL https://opencode.ai/install | bash",
            "mac": "curl -fsSL https://opencode.ai/install | bash",
            "windows": "npm install -g opencode-ai",
        },
        "auth": {"cmd": "opencode auth login",
                 "note": "Vybere se poskytovatel a vloží klíč. "
                         "Pro lokální modely přes Ollamu není potřeba nic."},
        "models": "ollama",          # k vlastní nabídce přidá lokální modely
        "model_prefix": "ollama/",   # jak opencode pojmenovává modely z Ollamy
        "model_arg": "--model {model}",
        "model_cmd": "/models",
        "slash": ["/models", "/sessions", "/new", "/share", "/help"],
        "skills": False,
        "composer": "basic",
    },
    "aider": {
        "label": "aider",
        "short": "aider",
        "color": "#14b8a6",
        "ansi": 43,
        "bin": "aider",
        "note": "Párové programování v terminálu, pracuje přímo s gitem.",
        "install": {
            "linux": "uv tool install --force --python python3.12 "
                     "--with pip aider-chat@latest",
            "mac": "uv tool install --force --python python3.12 "
                   "--with pip aider-chat@latest",
            "windows": "uv tool install --force --with pip aider-chat@latest",
        },
        "auth": {"env": ["OPENAI_API_KEY", "ANTHROPIC_API_KEY"],
                 "note": "Bere klíč z prostředí, nebo jede na lokálním modelu "
                         "z Ollamy, kde žádný klíč netřeba."},
        "models": "ollama",
        # Dokumentace aidera doporučuje ollama_chat/ místo ollama/ — přes chat
        # endpoint dává lokální model výrazně lepší výsledky.
        "model_prefix": "ollama_chat/",
        "model_arg": "--model {model}",
        "model_cmd": "/model",
        "slash": ["/add", "/drop", "/diff", "/undo", "/model", "/help"],
        "skills": False,
        "composer": "basic",
    },
    "ollama": {
        "label": "Ollama",
        "short": "ollama",
        "color": "#94a3b8",
        "ansi": 245,
        "bin": "ollama",
        "note": "Lokální model přímo v tabu. Neumí sahat na soubory — "
                "je to chat, ne agent.",
        "install": {
            "linux": "curl -fsSL https://ollama.com/install.sh | sh",
            "mac": "brew install ollama",
            "windows": "winget install --id Ollama.Ollama",
        },
        "auth": {"note": "Nic — běží u tebe na počítači."},
        "models": "ollama",
        "needs_model": True,     # `ollama run` bez modelu nedává smysl
        "run_arg": "run {model}",
        "models_only": True,     # nabídka je jen z toho, co je stažené
        "slash": ["/bye", "/clear", "/set", "/show", "/help"],
        "skills": False,
        "composer": "basic",
    },
}

ORDER = ["claude", "codex", "gemini", "opencode", "aider", "ollama"]


def platform_key():
    if IS_WINDOWS:
        return "windows"
    import sys
    return "mac" if sys.platform == "darwin" else "linux"


def catalog(extra=None):
    """Vestavěný katalog plus to, co si člověk dopsal do hub-config.json."""
    out = {k: dict(v) for k, v in CATALOG.items()}
    for key, spec in (extra or {}).items():
        if not isinstance(spec, dict):
            continue
        key = str(key).strip().lower()
        if not key or not key.replace("-", "").replace("_", "").isalnum():
            continue  # jméno agenta jde do jména proměnné i do příkazu
        base = out.get(key, {"label": key, "short": key, "color": "#94a3b8",
                             "bin": key, "composer": "basic", "skills": False,
                             "models": [], "slash": []})
        base.update(spec)
        base.setdefault("bin", key)
        out[key] = base
    return out


def order(cat):
    """Pořadí pro UI: nejdřív ti vestavění, pak vlastní podle abecedy."""
    known = [k for k in ORDER if k in cat]
    return known + sorted(k for k in cat if k not in ORDER)


def resolve(agent_id, extra=None):
    """Záznam agenta i s doplněným id, nebo None."""
    cat = catalog(extra)
    spec = cat.get((agent_id or "").strip().lower())
    if not spec:
        return None
    spec = dict(spec)
    spec["id"] = (agent_id or "").strip().lower()
    return spec


def extra_bin_dirs():
    """Kam instalátory agentů kladou binárky, aniž by je uviděl náš shell.

    Naměřeno na opencode: jeho instalátor si přidá cestu do ~/.bashrc, jenže
    tab startuje `bash -l -c`, a ten .bashrc nečte. Nainstalovaný agent by tak
    zůstal „nenalezený" až do odhlášení — a v okně spuštěném ze zástupce
    i potom, protože to prostředí z profilu nedostane vůbec.
    """
    home = os.path.expanduser("~")
    names = [".local/bin", "bin", ".opencode/bin", ".bun/bin", ".cargo/bin",
             ".deno/bin", ".npm-global/bin", ".local/share/pnpm"]
    dirs = [os.path.join(home, n) for n in names]
    if sys.platform == "darwin":
        dirs += ["/opt/homebrew/bin", "/usr/local/bin"]
    if os.name == "nt" and os.environ.get("LOCALAPPDATA"):
        # winget dává odkazy na nainstalované CLI sem (Claude Code, gh).
        dirs.append(os.path.join(os.environ["LOCALAPPDATA"], "Microsoft", "WinGet", "Links"))
    return [d for d in dirs if os.path.isdir(d)]


def which(spec):
    """Plná cesta k binárce agenta — z PATH, jinak z těch obvyklých složek."""
    name = spec.get("bin") or ""
    if not name:
        return ""
    found = shutil.which(name)
    if found:
        return found
    for folder in extra_bin_dirs():
        for suffix in ((".exe", ".cmd", "") if IS_WINDOWS else ("",)):
            cand = os.path.join(folder, name + suffix)
            if os.path.isfile(cand) and os.access(cand, os.X_OK):
                return cand
    return ""


_NPM_PREFIX = None

# Co se zjišťuje spuštěním CLI (`npm config get prefix`, `claude --help`) se
# drží i na disku: při každém startu hubu by to bylo ~1 s čekání. Klíčem je
# cesta a mtime binárky — po aktualizaci npm/agenta se zeptá znovu.
_DISK = None


def _disk_path():
    base = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")
    return os.path.join(base, "hub-agents-cache.json")


def _disk():
    global _DISK
    if _DISK is None:
        try:
            with open(_disk_path(), encoding="utf-8") as fh:
                _DISK = json.load(fh)
            if not isinstance(_DISK, dict):
                _DISK = {}
        except (OSError, ValueError):
            _DISK = {}
    return _DISK


def _disk_set(key, value):
    d = _disk()
    if d.get(key) == value:
        return
    d[key] = value
    try:
        tmp = _disk_path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(d, fh)
        os.replace(tmp, _disk_path())
    except OSError:
        pass


def _stamp(path):
    try:
        return f"{path}|{os.path.getmtime(path)}"
    except OSError:
        return ""


def npm_user_prefix():
    """Kam smí `npm install -g` psát bez práv správce. Prázdno = stačí systémový.

    Na tomhle typu instalace (node z distribuce, prefix /usr) skončí `npm -g`
    na EACCES a tlačítko Nainstalovat by jen vypsalo chybu. Uživatelský prefix
    to řeší bez sudo — a ~/.local/bin bývá v PATH, takže binárku pak vidíme.
    """
    global _NPM_PREFIX
    if _NPM_PREFIX is not None:
        return _NPM_PREFIX
    _NPM_PREFIX = ""
    npm = shutil.which("npm")
    stamp = "npm-prefix|" + _stamp(os.path.realpath(npm)) if npm else ""
    hit = _disk().get(stamp) if stamp else None
    if isinstance(hit, str):
        _NPM_PREFIX = hit
        return _NPM_PREFIX
    if npm:
        try:
            r = subprocess.run([npm, "config", "get", "prefix"], capture_output=True,
                               text=True, timeout=6, creationflags=_NO_WINDOW)
            prefix = (r.stdout or "").strip()
            probe = os.path.join(prefix, "lib")
            if prefix and not os.access(probe if os.path.isdir(probe) else prefix,
                                        os.W_OK):
                _NPM_PREFIX = os.path.join(os.path.expanduser("~"), ".local")
            _disk_set(stamp, _NPM_PREFIX)
        except Exception:
            pass
    return _NPM_PREFIX


def install_cmd(spec):
    inst = spec.get("install") or {}
    cmd = inst if isinstance(inst, str) else (inst.get(platform_key()) or "")
    if cmd.startswith("npm install -g "):
        prefix = npm_user_prefix()
        if prefix:
            cmd = cmd.replace("npm install -g ",
                              f'npm install -g --prefix "{prefix}" ', 1)
    return cmd


def _version(path, spec):
    """Verze agenta, když ji řekne rychle. Prázdno není chyba."""
    if not path:
        return ""
    try:
        r = subprocess.run([path, "--version"], capture_output=True, text=True,
                           timeout=6, creationflags=_NO_WINDOW)
        out = (r.stdout or r.stderr or "").strip().splitlines()
        if not out:
            return ""
        # `codex-cli 0.4.2` i `aider 0.86.1` — chceme jen to číslo
        first = out[0].strip()
        for word in first.split():
            if word[:1].isdigit():
                return word
        return first[:40]
    except Exception:
        return ""


def detect(extra=None, with_version=True):
    """Co je na tomhle stroji doopravdy k dispozici.

    Vrací seznam v pořadí pro UI. `path` prázdné = není v PATH, a UI podle toho
    nabídne instalaci místo otevření tabu.
    """
    cat = catalog(extra)
    out = []
    for key in order(cat):
        spec = cat[key]
        path = which(spec)
        out.append({
            "id": key,
            "label": spec.get("label") or key,
            "short": spec.get("short") or key,
            "color": spec.get("color") or "#94a3b8",
            "note": spec.get("note") or "",
            "bin": spec.get("bin") or key,
            "path": path,
            "version": _version(path, spec) if (path and with_version) else "",
            "install": install_cmd(spec),
            "auth": spec.get("auth") or {},
            "models": models_for(spec, path),
            "needs_model": bool(spec.get("needs_model")),
            "slash": spec.get("slash") or [],
            "skills": bool(spec.get("skills")),
            "composer": spec.get("composer") or "basic",
            "model_cmd": spec.get("model_cmd") or "",
            "bypass": bool(bypass_arg(spec)),
        })
    return out


# ── Ollama ───────────────────────────────────────────────────────────────────
_OLLAMA_CACHE = [0.0, None]     # (kdy, seznam) — `ollama list` trvá desetiny sekundy
OLLAMA_TTL = 30.0


def ollama_models(fresh=False):
    """Stažené lokální modely. Prázdný seznam = neběží nebo nic stažené.

    Volá se při každém načtení stránky (nabídka modelů u tří agentů), takže
    se výsledek chvíli drží — jinak by každé otevření hubu spustilo
    `ollama list` třikrát. Nastavení si říká o čerstvý (`fresh`)."""
    import time
    if not fresh and _OLLAMA_CACHE[1] is not None and time.monotonic() - _OLLAMA_CACHE[0] < OLLAMA_TTL:
        return list(_OLLAMA_CACHE[1])
    names = _ollama_list()
    _OLLAMA_CACHE[0], _OLLAMA_CACHE[1] = time.monotonic(), names
    return list(names)


def _ollama_list():
    path = shutil.which("ollama")
    if not path:
        return []
    try:
        r = subprocess.run([path, "list"], capture_output=True, text=True,
                           timeout=6, creationflags=_NO_WINDOW)
        names = []
        for line in (r.stdout or "").splitlines()[1:]:   # první řádek je hlavička
            name = line.split()[0] if line.split() else ""
            if name:
                names.append(name)
        return names
    except Exception:
        return []


def ollama_state():
    """(běží, kolik modelů, seznam) — pro nastavení i pro --doctor."""
    path = shutil.which("ollama")
    if not path:
        return {"installed": False, "running": False, "models": []}
    running = False
    try:
        import urllib.request
        with urllib.request.urlopen(OLLAMA_HOST + "/api/tags", timeout=2):
            running = True
    except Exception:
        running = False
    return {"installed": True, "running": running, "models": ollama_models(fresh=True)}


# ── Modely Claude Code ──────────────────────────────────────────────────────
# Seznam modelů napevno v hubu zastarával s každým vydáním Anthropicu (v nabídce
# pořád „Sonnet 5", když alias už dávno jel na 5.5). Claude Code ale zná všechny
# modely, na které umí přepnout — má jejich id přímo v sobě. Nová verze Claude
# Code s novým modelem ho tak přinese do nabídky sama, bez vydání hubu.
MODEL_FAMILIES = ("opus", "sonnet", "haiku", "fable")
_MODEL_RE = re.compile(rb"claude-(opus|sonnet|haiku|fable)-(\d{1,2})(?:-(\d))?(?![0-9a-z])")
_BIN_MODELS = {}
_MODELS_CACHE = os.path.join(os.path.expanduser("~"), ".claude", "hub-models.json")
MIN_MAJOR = 4          # tři-krát-cokoli už Claude Code nepustí


def _scan_models(real):
    """Id modelů v binárce Claude Code → {(rodina, major, minor): počet}."""
    found = {}
    tail = b""
    with open(real, "rb") as fh:
        while True:
            chunk = fh.read(16 * 1024 * 1024)
            if not chunk:
                break
            data = tail + chunk
            for m in _MODEL_RE.finditer(data):
                if m.end() > len(data) - 40 and len(chunk) == 16 * 1024 * 1024:
                    continue          # může pokračovat v dalším kusu
                fam = m.group(1).decode()
                major = int(m.group(2))
                minor = int(m.group(3)) if m.group(3) else 0
                key = (fam, major, minor)
                found[key] = found.get(key, 0) + 1
            tail = data[-40:]
    return found


def _label(fam, major, minor):
    return f"{fam.capitalize()} {major}" + (f".{minor}" if minor else "")


def claude_models(path):
    """Nabídka modelů podle toho, co zná nainstalovaný Claude Code.

    Nejnovější model každé rodiny se vybírá aliasem (`opus`…), starší plným
    id; třetí položka je skupina ("" = hlavní, "old" = starší). Prohledání
    binárky (stovky MB) stojí vteřinu, tak se výsledek pamatuje podle mtime
    — i na disku, ať se neopakuje po každém startu hubu.
    """
    try:
        real = os.path.realpath(path)
        st = os.stat(real)
    except (OSError, TypeError):
        return []
    stamp = f"{real}|{int(st.st_mtime)}|{st.st_size}"
    if _BIN_MODELS.get("stamp") == stamp:
        return [list(m) for m in _BIN_MODELS["models"]]
    try:
        with open(_MODELS_CACHE, encoding="utf-8") as fh:
            saved = json.load(fh)
        if saved.get("stamp") == stamp and isinstance(saved.get("models"), list):
            _BIN_MODELS.update(saved)
            return [list(m) for m in saved["models"]]
    except (OSError, ValueError, AttributeError):
        pass
    try:
        found = _scan_models(real)
    except OSError:
        return []
    main, old = [], []
    for fam in MODEL_FAMILIES:
        # Jedna zmínka bývá text v nápovědě nebo changelogu, ne model.
        verze = sorted({(ma, mi) for (f, ma, mi), n in found.items()
                        if f == fam and ma >= MIN_MAJOR and n >= 2}, reverse=True)
        for i, (ma, mi) in enumerate(verze):
            if i == 0:
                main.append([_label(fam, ma, mi), fam, ""])
            else:
                # `claude-opus-4` samotné není id; Claude Code zná `-4-0`.
                ident = f"claude-{fam}-{ma}-{mi}" if (mi or ma == 4) else f"claude-{fam}-{ma}"
                old.append([_label(fam, ma, mi), ident, "old"])
    models = main + old
    if not main:
        return []
    _BIN_MODELS.update({"stamp": stamp, "models": models})
    try:
        tmp = _MODELS_CACHE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"stamp": stamp, "models": models}, fh)
        os.replace(tmp, _MODELS_CACHE)
    except OSError:
        pass
    return [list(m) for m in models]


def models_for(spec, path=""):
    """Nabídka modelů pro chip v bublině.

    U Claudea z nainstalovaného Claude Code (claude_models), jinak statický
    seznam z katalogu; lokální modely u všeho, co umí Ollamu, a prázdno tam,
    kde si nabídku drží agent sám — tam chip zavolá jeho vlastní `/model`
    a vybírá se v jeho TUI.
    """
    if spec.get("models_from_bin") and path:
        found = claude_models(path)
        if found:
            return found
    models = spec.get("models")
    if isinstance(models, list):
        return [list(m) for m in models]
    if models == "ollama":
        prefix = spec.get("model_prefix") or ""
        return [[m, prefix + m] for m in ollama_models()]
    return []


# ── Jak se agent doopravdy spustí ────────────────────────────────────────────
def env_for(spec, model=""):
    """HUB_AGENT_* proměnné pro agent-wrapper.sh.

    Wrapper tím pádem nemusí znát katalog ani parsovat JSON — dostane hotovou
    identitu a jen ji vypíše v hlavičce, případně poradí instalaci.
    """
    auth = spec.get("auth") or {}
    hint = auth.get("cmd") or ""
    if hint and auth.get("slash"):
        hint = f'{hint} → {auth["slash"]}'
    # Do binárky se dosazuje plná cesta, když ji známe — jinak by ji `bash -l`
    # nenašel u agenta, který bydlí mimo systémové cesty.
    path = which(spec)
    extra = extra_bin_dirs()
    env_path = os.pathsep.join(extra + [os.environ.get("PATH", "")]) if extra \
        else os.environ.get("PATH", "")
    return {
        "PATH": env_path,
        "HUB_AGENT_ID": spec.get("id") or spec.get("bin") or "",
        "HUB_AGENT_BIN": path or spec.get("bin") or "",
        "HUB_AGENT_LABEL": (spec.get("label") or spec.get("bin") or "").lower(),
        "HUB_AGENT_ANSI": str(spec.get("ansi") or 208),
        "HUB_AGENT_INSTALL": install_cmd(spec),
        "HUB_AGENT_AUTH": hint,
        "HUB_AGENT_BRAIN": "1" if spec.get("skills") else "0",
        # Co si o tabu odnese UI: který model v něm doopravdy běží.
        "HUB_AGENT_MODEL": resolved_model(spec, model),
    }


def resolved_model(spec, model=""):
    """Model, se kterým se tab doopravdy spustí.

    Ollama bez modelu nemá co pustit, tak dostane první stažený — a UI se to
    musí dozvědět, jinak by chip v bublině hlásil „výchozí" u tabu, který běží
    na konkrétním modelu.
    """
    if model:
        return model
    if spec.get("needs_model"):
        first = (models_for(spec) or [["", ""]])[0][1]
        return first or ""
    return ""


# (cesta k binárce, mtime, přepínač) → zná ho? `--help` se pouští jednou na
# verzi: po aktualizaci agenta se mtime změní a zeptá se znovu.
_KNOWS_FLAG = {}


def _knows_flag(spec, flag):
    path = which(spec)
    try:
        key = (path, os.path.getmtime(path), flag)
    except OSError:
        return False
    disk_key = "flag|" + _stamp(os.path.realpath(path)) + "|" + flag
    if key not in _KNOWS_FLAG and isinstance(_disk().get(disk_key), bool):
        _KNOWS_FLAG[key] = _disk()[disk_key]
    if key not in _KNOWS_FLAG:
        try:
            r = subprocess.run([path, "--help"], capture_output=True, text=True,
                               timeout=10, stdin=subprocess.DEVNULL,
                               creationflags=_NO_WINDOW)
            _KNOWS_FLAG[key] = flag in (r.stdout or "") + (r.stderr or "")
            _disk_set(disk_key, _KNOWS_FLAG[key])
        except Exception:
            _KNOWS_FLAG[key] = False
    return _KNOWS_FLAG[key]


def bypass_arg(spec):
    """Přepínač, se kterým agent nabídne bypass mezi režimy, nebo ''.

    Jen když ho nainstalovaná verze zná: neznámý přepínač by agenta vůbec
    nespustil a tab by zůstal prázdný. Ze stejného důvodu ne pod rootem —
    Claude Code tam bypass odmítá (IS_SANDBOX je výjimka, kterou zná sám).
    """
    arg = spec.get("bypass_arg") or ""
    if not arg:
        return ""
    if hasattr(os, "geteuid") and os.geteuid() == 0 \
            and not os.environ.get("IS_SANDBOX"):
        return ""
    return arg if _knows_flag(spec, arg) else ""


def launch_args(spec, model="", prompt="", bypass=True, resume="", fork=False,
                session="", extra=None):
    """Argumenty za jméno binárky: bypass v nabídce (když `bypass` a agent ho
    umí), pokračování v konverzaci (`resume`, s `fork` jako kopie), model
    a úvodní prompt. `session` = id nové konverzace, které si hub zvolil sám."""
    args = [bypass_arg(spec)] if bypass and bypass_arg(spec) else []
    if resume and spec.get("resume_arg"):
        args += [part.replace("{id}", resume) for part in spec["resume_arg"].split(" ") if part]
        if fork and spec.get("fork_arg"):
            args.append(spec["fork_arg"])
    elif session and spec.get("session_arg"):
        args += [part.replace("{id}", session)
                 for part in spec["session_arg"].split(" ") if part]
    model = resolved_model(spec, model)
    if model:
        # `ollama run <model>` je celý příkaz, ne přepínač
        template = spec.get("run_arg") or spec.get("model_arg") or ""
        if template:
            args += [part.replace("{model}", model)
                     for part in template.split(" ") if part]
    # Přepínače navíc (tab s úkolem ze serveru: --settings, --permission-mode).
    args += list(extra or [])
    if prompt:
        args.append(prompt)
    return args
