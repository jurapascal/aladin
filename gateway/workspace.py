"""
Domov uživatele na bráně — jeho vault, jeho konfigurace hubu, jeho session.

Každý účet dostane vlastní domovskou složku pojmenovanou podle e-mailu
(`/home/hub/users/boucnik.jiri`, do 2.5.3 `u<id>` — viz migrate_home), a v ní:

* **Obsidian vault** s jeho pamětí. Je jen jeho: izolace (`isolation.py`) pustí
  session tak, že mimo tenhle domov nevidí — takže paměť jednoho člověka se
  k druhému nedostane, i když oba běží na jednom stroji.
* **`.claude/hub-config.json`**, aby se jeho instance hubu nastavila sama:
  paměť míří do jeho vaultu, projekty do jeho složky. Hub žádné okno neotvírá,
  brána ho pustí s `--no-browser` a mluví s ním přes proxy.
* **přihlášení Claude Code** (session_env). Přednost má **vlastní předplatné**
  toho člověka: appka na počítači ho připojí sama (`claude setup-token`, token
  na rok, viz predplatne) a prostor ho dostane jako `CLAUDE_CODE_OAUTH_TOKEN`.
  Bez něj na `central` (výchozí) klíč API (`ANTHROPIC_API_KEY`) — vlastní klíč
  účtu, nebo společný klíč brány (viz api_key); platí se podle spotřeby. Když
  není ani klíč, přihlásí se člověk v prostoru sám (`/login`). Sdílet jedno
  předplatné mezi víc lidí je proti podmínkám Anthropicu, proto token patří
  vždycky jen účtu, který ho připojil.

Do domova brána zapisuje jen přes `safefs` — domov patří session a ta v něm
může nechat odkaz kamkoli na server (viz safefs).

Cesty jdou přebít proměnnými prostředí, aby šel modul otestovat i mimo server.
"""
import contextlib
import fcntl
import hashlib
import json
import os
import re
import secrets
import stat
import threading
import time

from . import config
from . import firmy
from . import safefs
from . import poznamky
from . import shared
from . import slozky
from .isolation import SCOPE_PREFIX

HUB_HOME = os.environ.get("HUB_GW_HOME", "/home/hub")
USERS_ROOT = os.environ.get("HUB_GW_USERS", os.path.join(HUB_HOME, "users"))
# Zdroj hubu, který se pouští každému uživateli. Do sandboxu se přiváže jen ke
# čtení — session do něj nesmí zapisovat.
REPO_DIR = os.environ.get("HUB_GW_REPO",
                          os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# Připravené verze hubu, každá ve své složce (gateway/update.sh `pripravit`,
# install.sh `setup_verze`). Nový prostor jede z nejnovější — přiváže se do
# sandboxu na místo REPO_DIR, takže uvnitř se nic nemění (i cesty zapsané
# v konfiguraci MCP dál vedou na REPO_DIR). Běžící prostor jede ze své složky
# dál, i když se zdroj mezitím přepne: nic se mu nevymění pod rukama.
VERZE_DIR = os.environ.get("HUB_GW_VERZE", "/opt/claude-code-hub-verze")
# Sdílené přihlášení Claude Code z dob dřívějšího `central`. Už se nepoužívá —
# ensure() jen uklízí symlinky, které na něj v domovech zůstaly.
SHARED_CLAUDE = os.environ.get("HUB_GW_CLAUDE", os.path.join(HUB_HOME, ".claude"))
SHARED_CRED = os.path.join(SHARED_CLAUDE, ".credentials.json")
PYTHON = os.environ.get("HUB_GW_PYTHON", "python3")

EMPTY_MEMORY = """\
## 🗺 Rozcestníky

## Projekty a reference

## Poznatky (learnings)

## Chyby (errors)

## Úspěchy (wins)
"""


def slug(user):
    """Jméno složky domova do 2.5.3 (`u7`). Dnes jen pro starý domov, který
    ještě čeká na přejmenování, a pro odkaz na starou cestu v sandboxu."""
    return "u%d" % int(user["id"])


# Znaky, které jdou do jména systemd jednotky tak, jak jsou. Ostatní se píšou
# jako `\xNN` (stejně jako systemd-escape).
_UNIT_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789.-")


def _unit_escape(text):
    out = []
    for ch in text:
        if ch in _UNIT_CHARS:
            out.append(ch)
        else:
            out.extend("\\x%02x" % b for b in ch.encode("utf-8"))
    return "".join(out)


def legacy_unit_name(user):
    """Jméno scope do 2.5.1 (`claude-hub-u7`). Prostor s ním může běžet, dokud
    se brána po aktualizaci nerestartuje, takže ho správa musí dál poznat."""
    return SCOPE_PREFIX + slug(user)


def unit_name(user):
    """Jméno systemd scope, ve které prostor běží — podle e-mailu, ať je ze
    `sessions` hned vidět, čí je (`claude-hub-boucnik.jiri_gmail.com`).

    Zavináč systemd ve jménu nepřijme, proto `_`. Všechno mimo [a-z0-9.-]
    (i `_` z e-mailu) jde jako `\\xNN`, takže dva různé e-maily nikdy nedostanou
    stejné jméno. E-mail je v databázi unikátní a malými písmeny.
    """
    email = (user.get("email") or "").strip().lower()
    local, at, domain = email.rpartition("@")
    name = SCOPE_PREFIX + _unit_escape(local) + "_" + _unit_escape(domain)
    # Bez e-mailu, nebo tak dlouhý, že by přetekl limit systemd (255 i s .scope).
    if not at or not local or not domain or len(name) > 200:
        return legacy_unit_name(user)
    return name


# Kdo má kterou složku domova: {"boucnik.jiri": 1}. Leží vedle domovů, ne
# v nich — do svého domova session zapisuje, a kdyby šlo přiřazení změnit
# odtamtud, dal by si člověk cizí složku.
REGISTRY = os.path.join(USERS_ROOT, ".domovy.json")
_REGISTRY_LOCK = threading.Lock()


@contextlib.contextmanager
def _locked():
    """Registr mění brána (víc vláken) i claude-hub-admin (jiný proces)."""
    with _REGISTRY_LOCK:
        os.makedirs(USERS_ROOT, exist_ok=True)
        with open(REGISTRY + ".lock", "a") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)


def _registry():
    try:
        with open(REGISTRY, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if isinstance(v, int)}


def _save_registry(reg):
    tmp = REGISTRY + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(reg, fh, ensure_ascii=False, indent=2, sort_keys=True)
    os.replace(tmp, REGISTRY)


def _registered(reg, user):
    uid = int(user["id"])
    return next((name for name, owner in sorted(reg.items()) if owner == uid), "")


def _home_names(user):
    """Jména složky, o která se účet uchází, v tomhle pořadí: část e-mailu
    před zavináčem, pak s doménou, pak s číslem účtu. `u<číslo>` ne — tak se
    jmenovaly domovy do 2.5.3 a mohl by to být cizí."""
    def clean(text):
        text = re.sub(r"[^a-z0-9._-]", "-", text.lower())
        return re.sub(r"-{2,}", "-", text).strip("._-")[:40]
    local, _at, domain = (user.get("email") or "").strip().rpartition("@")
    base = clean(local)
    names = []
    if base and not re.fullmatch(r"u\d+", base):
        names.append(base)
        if clean(domain):
            names.append(f"{base}_{clean(domain)}")
    names.append(f"{base or 'ucet'}-{int(user['id'])}")
    return names


def _owner_email(path):
    try:
        owner = json.loads(safefs.read_text(path, ".claude/hub-config.json") or "{}")
        return str((owner.get("gateway_user") or {}).get("email") or "").strip().lower()
    except (OSError, ValueError, AttributeError):
        return ""


def _pick_home_name(user, reg):
    mine = _registered(reg, user)
    if mine:
        return mine
    email = (user.get("email") or "").strip().lower()
    for name in _home_names(user):
        if name in reg:
            continue
        path = os.path.join(USERS_ROOT, name)
        if not os.path.lexists(path):
            return name
        # Složka bez záznamu (registr se ztratil): vezme si ji jen ten, komu
        # podle hub-config.json patří. Kdo by si tam cizí e-mail zapsal sám,
        # nic nezíská — přišel by jen o vlastní domov.
        if os.path.isdir(path) and email and _owner_email(path) == email:
            return name
    return slug(user)


def home_for(user):
    """Domov účtu: složka podle e-mailu (`/home/hub/users/boucnik.jiri`).

    Dokud se starý `u<id>` nepřejmenoval — prostor od aktualizace ještě
    neběžel, přejmenovává ho až migrate_home před startem — je to on.
    """
    reg = _registry()
    name = _registered(reg, user)
    if name and os.path.isdir(os.path.join(USERS_ROOT, name)):
        return os.path.join(USERS_ROOT, name)
    legacy = os.path.join(USERS_ROOT, slug(user))
    if os.path.isdir(legacy) or not name:
        return legacy
    return os.path.join(USERS_ROOT, name)


def migrate_home(user):
    """Přidělí domovu jméno podle e-mailu a starý `u<id>` na něj přejmenuje.
    Vrací cestu domova.

    Volá se jen před startem prostoru, kdy prokazatelně neběží: přejmenovat
    složku, ze které zrovna jede Claude Code, by mu vzalo půdu pod nohama.
    Jméno se do registru zapíše dřív, než se složka přejmenuje — kdyby se to
    přerušilo mezi tím, další start pod stejným jménem dokončí, co zbylo.
    """
    uid = int(user["id"])
    with _locked():
        reg = _registry()
        name = _pick_home_name(user, reg)
        if reg.get(name) != uid:
            reg = {k: v for k, v in reg.items() if v != uid}
            reg[name] = uid
            _save_registry(reg)
        target = os.path.join(USERS_ROOT, name)
        legacy = os.path.join(USERS_ROOT, slug(user))
        if target != legacy and os.path.isdir(legacy) and not os.path.lexists(target):
            os.rename(legacy, target)
            rewrite_paths(target, legacy, target)
        return target


# Kam se při přejmenování domova nesahá: cache mají tisíce souborů a cesty
# v nich (skripty uv, npx) dál fungují přes odkaz, který na starém místě
# vyrobí sandbox (session_spec, aliases).
_SKIP_DIRS = {".npm", ".cache", "node_modules", ".git"}
_MAX_REWRITE = 64 * 1024 * 1024


def claude_slug(path):
    """Jak Claude Code pojmenuje složku projektu: cokoli mimo písmena a číslice
    je pomlčka (`/home/hub/users/boucnik.jiri` → `-home-hub-users-boucnik-jiri`)."""
    return re.sub(r"[^A-Za-z0-9]", "-", path)


def _retarget(link, old, new):
    target = os.readlink(link)
    if target == old or target.startswith(old + "/"):
        os.remove(link)
        os.symlink(new + target[len(old):], link)
        return True
    return False


def rewrite_paths(home, old, new):
    """Po přejmenování domova přepíše starou cestu na novou: v textových
    souborech (nastavení, skilly, přepisy konverzací), v odkazech a ve jménech
    složek projektů Claude Code. Vrací počet změněných souborů."""
    path_re = re.compile(re.escape(old.encode()) + rb"(?![A-Za-z0-9._-])")
    old_slug, new_slug = claude_slug(old), claude_slug(new)
    slug_re = re.compile(re.escape(old_slug.encode()) + rb"(?![A-Za-z0-9])")
    changed = 0
    for root, dirs, files in os.walk(home):
        keep = []
        for d in dirs:
            full = os.path.join(root, d)
            if os.path.islink(full):
                changed += _retarget(full, old, new)
            elif d not in _SKIP_DIRS:
                keep.append(d)
        dirs[:] = keep
        for name in files:
            full = os.path.join(root, name)
            if os.path.islink(full):
                changed += _retarget(full, old, new)
                continue
            rel = os.path.relpath(full, home)
            st = safefs.lstat(home, rel)
            if not st or not stat.S_ISREG(st.st_mode) or st.st_size > _MAX_REWRITE:
                continue
            data = safefs.read_bytes(home, rel, _MAX_REWRITE)
            if data is None or b"\0" in data[:8192]:
                continue                  # zmizel, odkaz, nebo binární soubor
            fixed = slug_re.sub(new_slug.encode(), path_re.sub(new.encode(), data))
            if fixed == data:
                continue
            try:
                safefs.write_bytes(home, rel, fixed, mode=stat.S_IMODE(st.st_mode), heal=False)
                changed += 1
            except OSError:
                pass
    # Složky projektů Claude Code nesou cestu ve jméně — bez přejmenování by
    # přišel o historii konverzací i paměť projektu.
    try:
        pfd = safefs.open_dir(home, [".claude", "projects"])
    except OSError:
        return changed
    try:
        for entry in os.listdir(pfd):
            if entry == old_slug or entry.startswith(old_slug + "-"):
                dst = new_slug + entry[len(old_slug):]
                try:
                    os.lstat(dst, dir_fd=pfd)
                except FileNotFoundError:
                    os.rename(entry, dst, src_dir_fd=pfd, dst_dir_fd=pfd)
    finally:
        os.close(pfd)
    return changed


def retire(user):
    """Odloží domov smazaného účtu stranou. Vrací novou cestu, nebo ''.

    Nestačí ho nechat ležet: nový účet se stejným e-mailem (nebo po smazání
    posledního účtu i se stejným id) by dostal stejnou složku a zdědil paměť,
    projekty i napojení toho předchozího. Data se nemažou, jen přestanou být
    na cestě, kterou může dostat někdo jiný — a jméno se v registru uvolní.
    """
    uid = int(user["id"])
    with _locked():
        home = home_for(user)
        reg = _registry()
        if uid in reg.values():
            _save_registry({k: v for k, v in reg.items() if v != uid})
        if not os.path.isdir(home):
            return ""
        stamp = time.strftime("%Y%m%d-%H%M%S")
        target = os.path.join(USERS_ROOT, f"_smazany-{os.path.basename(home)}-{stamp}")
        os.rename(home, target)
        return target


def vault_name(user):
    """Jak se jmenuje jeho Obsidian paměť. Když si nic nezvolil, odvodí se
    z e-mailu (část před zavináčem), ať to není holé `u7`."""
    name = (user.get("vault") or "").strip()
    if not name:
        name = (user.get("email") or "vault").split("@")[0]
    # Do jména složky pustíme jen to, co v cestě nezlobí.
    name = re.sub(r"[^\w.\- ]", "", name).strip() or "Brain"
    return name


def vault_dir(user, home=None):
    home = home or home_for(user)
    return os.path.join(home, "Obsidian", vault_name(user))


def claude_auth(user):
    """`central` (klíč API brány), nebo `own` (vlastní přihlášení v jeho domově)."""
    return "own" if (user.get("claude_auth") or "central") == "own" else "central"


def api_key(user=None):
    """Klíč API pro prostor toho účtu, nebo ''.

    Nejdřív vlastní klíč účtu (`api-keys/<id>`), a když ho nemá, společný klíč
    brány. Vlastní klíč dává každému svůj strop v Anthropic Console a nikdo
    v prostoru nepřečte klíč kolegy. Bez `user` vrací jen ten společný.

    Čte se při každém startu prostoru, takže `claude-hub-admin apikey set`
    platí od dalšího startu bez restartu brány."""
    paths = []
    uid = (user or {}).get("id")
    if uid:
        paths.append(os.path.join(config.API_KEYS_DIR, str(uid)))
    paths.append(config.API_KEY_FILE)
    for path in paths:
        try:
            with open(path, encoding="utf-8") as fh:
                key = fh.read().strip()
        except OSError:
            continue
        if key:
            return key
    return ""


# ── vlastní předplatné Claude ────────────────────────────────────────────────
# Token z `claude setup-token` (platí rok, jen na používání Clauda). Připojuje ho
# appka na počítači toho člověka, případně správce (`claude-hub-admin
# predplatne set`). Leží u brány podle čísla účtu, 0600 ve složce 0700 — do
# prostoru přijde jen tomu, komu patří, jako CLAUDE_CODE_OAUTH_TOKEN.
OAUTH_TOKEN_RE = re.compile(r"sk-ant-oat01-[A-Za-z0-9_-]{20,400}")
TOKEN_DAYS = 365


def _predplatne_path(user):
    return os.path.join(config.CLAUDE_TOKENS_DIR, "%d.json" % int(user["id"]))


def predplatne(user):
    """{"token", "created", "label"} připojeného předplatného, nebo None."""
    try:
        with open(_predplatne_path(user), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not OAUTH_TOKEN_RE.fullmatch(str(data.get("token") or "")):
        return None
    return data


def save_predplatne(user, token, label=""):
    token = str(token or "").strip()
    if not OAUTH_TOKEN_RE.fullmatch(token):
        raise ValueError("Tohle nevypadá jako token předplatného Claude (sk-ant-oat01-…).")
    os.makedirs(config.CLAUDE_TOKENS_DIR, mode=0o700, exist_ok=True)
    os.chmod(config.CLAUDE_TOKENS_DIR, 0o700)
    path = _predplatne_path(user)
    tmp = f"{path}.{secrets.token_hex(4)}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump({"token": token, "created": int(time.time()),
                   "label": str(label or "")[:80]}, fh)
    os.replace(tmp, path)


def remove_predplatne(user):
    try:
        os.remove(_predplatne_path(user))
        return True
    except FileNotFoundError:
        return False


def auth_mark(user):
    """Otisk přihlášení, se kterým by prostor nastartoval teď — podle něj se
    pozná, že běžící prostor potřebuje restart (připojené předplatné)."""
    env = session_env(user)
    raw = env.get("CLAUDE_CODE_OAUTH_TOKEN") or env.get("ANTHROPIC_API_KEY") or ""
    return hashlib.sha256(raw.encode()).hexdigest()[:16] if raw else ""


def claude_state(user):
    """Na čem Claude v prostoru jede — pro appku i nastavení v prostoru.

    `mode`: predplatne (vlastní předplatné přes bránu), api (klíč API),
    ucet (přihlásil se v prostoru sám), zadne (musí se přihlásit)."""
    sub = predplatne(user)
    if sub:
        created = int(sub.get("created") or 0)
        expires = created + TOKEN_DAYS * 86400 if created else 0
        return {"mode": "predplatne", "since": created, "expires": expires,
                "label": sub.get("label") or "",
                "expiring": bool(expires) and expires - time.time() < 30 * 86400}
    if claude_auth(user) == "central" and api_key(user):
        return {"mode": "api"}
    home = home_for(user)
    if os.path.isdir(home) and safefs.is_file(home, ".claude/.credentials.json"):
        return {"mode": "ucet"}
    return {"mode": "zadne"}


def google_client():
    """(client_id, client_secret) klienta OAuth pro Google, nebo ('', '')."""
    try:
        with open(config.GOOGLE_CLIENT_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
        return ((data.get("client_id") or "").strip(),
                (data.get("client_secret") or "").strip())
    except (OSError, ValueError, AttributeError):
        return "", ""


PROHLIZECE = "/usr/local/share/ms-playwright"


def session_env(user):
    """Proměnné prostředí navíc pro prostor: přihlášení Clauda a klient OAuth
    pro napojení na Google (všem — účty si každý přidává sám).

    Přihlášení: vlastní předplatné má přednost před klíčem API — kdo si ho
    připojil, chce jet na něm. Klíč API jen na `central`."""
    env = {}
    sub = predplatne(user)
    key = api_key(user) if claude_auth(user) == "central" else ""
    if sub:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = sub["token"]
    elif key:
        env["ANTHROPIC_API_KEY"] = key
    cid, secret = google_client()
    if cid and secret:
        env["GOOGLE_OAUTH_CLIENT_ID"] = cid
        env["GOOGLE_OAUTH_CLIENT_SECRET"] = secret
    # Společný Chromium pro Playwright (gateway/install.sh, setup_prohlizec) —
    # jinak by si ho každý stahoval do svého domova (~300 MB na člověka).
    if os.path.isdir(PROHLIZECE):
        env["PLAYWRIGHT_BROWSERS_PATH"] = PROHLIZECE
    return env


def pwa_id(user):
    """Tajný kód účtu do adres manifestu a ikon appky (vlastní název a ikona,
    hub/vzhled.py). Prohlížeč je stahuje bez přihlášení — podle kódu brána
    pozná, čí jsou, a z e-mailu ani čísla účtu se kód odvodit nedá."""
    import hashlib
    import hmac
    import secrets
    path = os.path.join(config.GATEWAY_DIR, "pwa-klic")
    try:
        with open(path, "rb") as fh:
            key = fh.read()
    except FileNotFoundError:
        key = b""
    if len(key) < 32:
        key = secrets.token_bytes(32)
        fd = os.open(path + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(key)
        os.replace(path + ".tmp", path)
    return hmac.new(key, str(user["id"]).encode(), hashlib.sha256).hexdigest()[:24]


VERXEE_KLIC = re.compile(r"vxd_[a-f0-9]{24}")


def verxee_klic():
    """Klíč tlačítka Nahlásit chybu / Podat návrh, nebo "" (claude-hub-admin verxee)."""
    try:
        with open(config.VERXEE_KLIC_FILE, encoding="ascii") as fh:
            klic = fh.read().strip()
    except (OSError, ValueError):
        return ""
    return klic if VERXEE_KLIC.fullmatch(klic) else ""


def _hub_config(user, home):
    vault = vault_dir(user, home)
    return {
        # Paměť míří do jeho vaultu; projekty do jeho složky.
        "brain_dir": vault,
        "project_dirs": [os.path.join(home, "projects")],
        # Hub běží na serveru bez okna — žádný prohlížeč neotevírá.
        "browser": "",
        "bash": "/bin/bash",
        # Onboarding už proběhl (nastavila ho brána), ať uživatele nevítá
        # průvodce prvním spuštěním.
        "onboarded": True,
        # Kdo to je — hub si to jinam nepíše, hodí se pro hlavičku.
        # Kód pro manifest a ikony appky na ploše (vlastní název a ikona).
        "pwa_id": pwa_id(user),
        "gateway_user": {"email": user.get("email", ""),
                         "name": user.get("name", ""),
                         "role": user.get("role", "user")},
        # Tlačítko Nahlásit chybu / Podat návrh — klíč pro celou bránu, prostor
        # si ho sám nemění (hub/server.py to na bráně odmítne).
        "verxee_debug_klic": verxee_klic(),
        # Společný firemní Obsidian — hub ho ukáže v panelu. Kdo k němu
        # přístup nemá, tomu se neukáže vůbec (a do sandboxu se nepřiváže).
        "company_vault": config.COMPANY_VAULT if company_level(user) != "none" else "",
        "company_level": company_level(user),
        # Osobní a sdílené složky ve firemním trezoru (Lidé/, Sdílené/), které
        # v prostoru má — hub podle nich pozná, kam se v nich zapisuje.
        "company_links": slozky.visible_for(user) if company_level(user) != "none" else [],
        # Sdílené Obsidiany, kde je členem (svázané při startu), a kdo je
        # v týmu — z toho vybírá tools/sdilene.py.
        "shared_vaults": shared.vaults_for(user),
        "people": shared.people(),
        # Firmy, kde je členem (gateway/firmy.py) — přepínač v okně hubu
        # a taby firem. Dnešní firemní trezor je mezi nimi jako `firma`.
        "firmy": firmy.firmy_for(user),
        "firmy_zakladat": firmy.muze_zakladat(user),
    }


def _verze(text):
    try:
        return tuple(int(x) for x in str(text).split("."))
    except ValueError:
        return None


def verze_zdroje(root):
    """__version__ z hubu ve složce `root` ('' = nevíme)."""
    try:
        with open(os.path.join(root, "hub", "__init__.py"), encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("__version__"):
                    return line.split("=", 1)[1].strip().strip("\"'")
    except OSError:
        pass
    return ""


def code_dir():
    """(složka, verze), ze které se pustí nový prostor.

    Nejnovější hotová připravená verze (má `.hotovo`), když je aspoň tak nová
    jako zdroj brány; jinak zdroj sám — stroj, kde se verze nepřipravují
    (vývoj, starší instalace)."""
    zdroj = verze_zdroje(REPO_DIR)
    nejlepsi = None
    try:
        jmena = os.listdir(VERZE_DIR)
    except OSError:
        jmena = []
    for jmeno in jmena:
        v = _verze(jmeno)
        if v is None or not os.path.isfile(os.path.join(VERZE_DIR, jmeno, ".hotovo")):
            continue
        if nejlepsi is None or v > nejlepsi[0]:
            nejlepsi = (v, jmeno)
    if nejlepsi and nejlepsi[0] >= (_verze(zdroj) or ()):
        return os.path.join(VERZE_DIR, nejlepsi[1]), nejlepsi[1]
    return REPO_DIR, zdroj


def ensure(user, kod=REPO_DIR):
    """Založí (nebo doplní) domov uživatele. Vrací slovník s cestami.

    Voláno při každém přihlášení — je to idempotentní: co existuje, nechá být.
    `kod` = složka hubu, ze které prostor pojede (code_dir).
    """
    home = home_for(user)
    vault = vault_dir(user, home)
    claude = os.path.join(home, ".claude")
    projects = os.path.join(home, "projects")

    # Domov zakládá brána ve složce, kam session nevidí — tady odkaz být nemůže.
    # 0700: prostory sice od sebe dělí sandbox (cizí domov se do něj vůbec
    # nepřiváže), ale všechny běží pod týmž systémovým účtem `hub`. Bez tohohle
    # by stačilo bwrap obejít a domovy by ležely otevřené. Druhá vrstva navíc,
    # ne náhrada sandboxu. `chmod` i na existující: starší domovy vznikly s 775.
    os.makedirs(home, mode=0o700, exist_ok=True)
    try:
        os.chmod(home, 0o700)
    except OSError:
        pass
    # Všechno uvnitř už patří session: jen přes safefs (odkazy se nenásledují).
    vault_rel = os.path.relpath(vault, home)
    for rel in (os.path.join(vault_rel, "memory"), os.path.join(vault_rel, "skills"),
                os.path.join(vault_rel, ".obsidian"), "projects"):
        safefs.makedirs(home, rel)
    safefs.create_text(home, os.path.join(vault_rel, "memory", "MEMORY.md"), EMPTY_MEMORY)

    # hub-config.json doplňujeme při každém přihlášení: e-mail, jméno i vault
    # se mohly změnit ve správě účtů a hub si je čte odsud. Co si v prostoru
    # nastavil člověk sám (vývojářský režim, výchozí agent, archiv, …), ale
    # musí zůstat — dřív se celý soubor přepsal a každé přihlášení nastavení
    # vrátilo do výchozího stavu, takže se přepínače „samy překlikávaly".
    # Když je ~/.claude odkaz, odloží se stranou — hub bez vlastní
    # konfigurace nenastartuje.
    try:
        stary = json.loads(safefs.read_text(home, ".claude/hub-config.json") or "{}")
    except ValueError:
        stary = {}
    if not isinstance(stary, dict):
        stary = {}
    safefs.write_text(home, ".claude/hub-config.json",
                      json.dumps({**stary, **_hub_config(user, home)},
                                 ensure_ascii=False, indent=2))

    # Wrapper, kterým se v prostoru spouští agent, patří k běžícímu hubu, ne
    # k době, kdy domov vznikl. Bez tohohle si každý starší domov nesl svou
    # kopii z instalace dál a nová verze hubu se v tabu vůbec neprojevila —
    # hub sahá nejdřív po ~/.claude/agent-wrapper.sh, teprve pak po zdroji.
    try:
        with open(os.path.join(kod, "agent-wrapper.sh"), encoding="utf-8") as fh:
            wrapper = fh.read()
    except OSError:
        wrapper = ""
    if wrapper and safefs.read_text(home, ".claude/agent-wrapper.sh") != wrapper:
        safefs.write_text(home, ".claude/agent-wrapper.sh", wrapper, mode=0o755)

    # Přihlášení Claude Code přichází v prostředí (session_env) a předschválí
    # ho hub v prostoru sám (hub/predplatne.py) — do ~/.claude.json brána
    # nesahá. Symlink na sdílené přihlášení z dřívějšího central se ruší:
    # jedno osobní předplatné pro víc lidí je proti podmínkám.
    try:
        cfd = safefs.open_dir(home, [".claude"])
        try:
            if os.readlink(".credentials.json", dir_fd=cfd) == SHARED_CRED:
                os.unlink(".credentials.json", dir_fd=cfd)
        finally:
            os.close(cfd)
    except OSError:
        pass

    # Firemní Obsidian: trezor, pokyny pro Clauda a přístup ke čtení. Bez něj
    # prostor nastartuje taky — jen o firemním nebude vědět.
    try:
        ensure_company_vault()
        level = company_level(user)
        _company_claude_md(home, level)
        mine = shared.vaults_for(user)
        other = [f for f in firmy.firmy_for(user) if f["zkratka"] != firmy.LEGACY]
        _company_settings(home, [v["path"] for v in mine] + [f["cesta"] for f in other],
                          company=level != "none")
        _shared_claude_md(home, mine)
        _firmy_claude_md(home, other, level != "none")
    except (OSError, ValueError):
        pass

    return {"home": home, "vault": vault, "claude": claude,
            "projects": projects}


# ── firemní Obsidian ─────────────────────────────────────────────────────────
# Jeden trezor pro všechny. V prostorech je svázaný jen ke čtení, takže Claude
# do něj sám nezapíše. Poznámku připraví nástrojem tools/firma.py do
# ~/.firma/ke-schvaleni/, hub ji ukáže s tlačítkem Nahrát a zapíše až brána
# (publish_company) — na požadavek z prohlížeče s přihlašovací cookie, kterou
# session nemá.
PENDING = os.path.join(".firma", "ke-schvaleni")
PENDING_ID = re.compile(r"\d{8}-\d{6}-[0-9a-f]{6}")
MAX_PROPOSAL = 1024 * 1024
FIRMA_MARK = ("<!-- claude-hub:firma -->", "<!-- /claude-hub:firma -->")

COMPANY_README = """# Firemní Obsidian

Společné know-how celého týmu. Každý má vedle toho svůj osobní trezor.

Nahrává se přes Claude Code: řekni mu „nahraj to do firemního", hub ukáže
kartu s náhledem a poznámka se uloží, až ji potvrdíš tlačítkem **Nahrát**.
"""


def ensure_company_vault():
    """Založí firemní trezor, když ještě není. Vrací jeho cestu."""
    vault = config.COMPANY_VAULT
    os.makedirs(vault, exist_ok=True)
    readme = os.path.join(vault, "README.md")
    if not os.path.exists(readme) and not [n for n in os.listdir(vault)
                                           if not n.startswith(".")]:
        with open(readme, "w", encoding="utf-8") as fh:
            fh.write(COMPANY_README)
    # Skilly patří do trezoru, ať je má celý tým odtamtud jen ke čtení. Složka
    # se zakládá až po README — prázdný trezor se pozná podle toho, že v něm
    # nic není. Naplní ji `claude-hub-admin skills install`.
    os.makedirs(config.COMPANY_SKILLS, exist_ok=True)
    # Sdílené/<název> — odkazy na sdílené trezory (a úklid starých Lidé/).
    # Bez nich firemní trezor funguje dál, jen v něm ty složky nejsou.
    try:
        slozky.sync(vault)
        _gitignore_links(vault)
    except (OSError, ValueError):
        pass
    return vault


def _gitignore_links(vault):
    """Když je firemní trezor v gitu, odkazy do domovů do něj nepatří."""
    if not os.path.isdir(os.path.join(vault, ".git")):
        return
    path = os.path.join(vault, ".gitignore")
    if os.path.islink(path):
        return
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        text = ""
    lines = text.splitlines()
    missing = [f"/{top}/" for top in slozky.ROOTS if f"/{top}/" not in lines]
    if missing:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(("\n" if text and not text.endswith("\n") else "") + "\n".join(missing) + "\n")


def company_skill_categories():
    """Kategorie firemních skillů — názvy složek v `<trezor>/skills`.

    Skilly samotné se nevypisují: jsou jich stovky a v pokynech by sežraly
    kontext. Kategorie Claudovi stačí, aby věděl, co tam je, a šel si přečíst
    ten, který potřebuje. Složky od `_` a `.` jsou pomocné (`_guides`)."""
    try:
        return [d for d in sorted(os.listdir(config.COMPANY_SKILLS))
                if not d.startswith(("_", "."))
                and os.path.isdir(os.path.join(config.COMPANY_SKILLS, d))]
    except OSError:
        return []


def _company_skills_text():
    """Odstavec o firemních skillech do bloku v CLAUDE.md. Bez skillů prázdný —
    ať Clauda neposíláme do složky, kde nic není."""
    cats = company_skill_categories()
    if not cats:
        return ""
    return f"""
**Firemní skilly.** Hotové postupy na konkrétní práci leží v trezoru
v `{config.COMPANY_SKILLS}/<kategorie>/<skill>/SKILL.md`.
Kategorie: {", ".join(cats)}.
Je jich hodně — **nenačítej je všechny**. Podle zadání vyber nejvýš
dva tři a jejich `SKILL.md` si přečti **než** začneš pracovat; co je u skillu
v `references/`, čti teprve když potřebuješ detail. Jsou jen ke čtení a platí
pro celý tým — když některý chybí nebo je v něm chyba, řekni to uživateli,
zavádí je správce serveru (`claude-hub-admin skills`).
"""


def company_level(user):
    """Přístup účtu k firemnímu Obsidianu: none / read / write (accounts.py).
    Bez databáze (testy, nástroje bez brány) platí výchozí úroveň."""
    from .accounts import COMPANY_DEFAULT
    if shared.ACCOUNTS is None:
        return "write" if (user or {}).get("role") == "admin" else COMPANY_DEFAULT
    return shared.ACCOUNTS.company_level(user)


READ_ONLY_BLOCK = """{mark}
## Firemní Obsidian

Vedle osobního trezoru je společný **firemní Obsidian** celého týmu:
`{vault}`. Tenhle uživatel ho má **jen ke čtení** — firemní postupy, kontakty
a know-how v něm hledej a čti, ale nic do něj nenavrhuj ani nenahrávej
(`tools/firma.py` návrh odmítne). Když by uživatel chtěl něco do firemního
Obsidianu zapsat, řekni mu, ať požádá admina o právo zápisu.
{skills}{links}{end}
"""


def _links_text():
    return f"""
**Složka {slozky.SHARED}/.** `{slozky.SHARED}/<název>` jsou sdílené Obsidiany, jejichž
je uživatel členem (zápis přes `tools/sdilene.py`, viz níž), ne přes `tools/firma.py`.
Osobní Obsidian uživatele ve firemním není — je jen jeho, zvlášť.
"""


def _company_block(level="write"):
    if level == "read":
        return READ_ONLY_BLOCK.format(mark=FIRMA_MARK[0], vault=config.COMPANY_VAULT,
                                      skills=_company_skills_text(), links=_links_text(),
                                      end=FIRMA_MARK[1])
    tool = os.path.join(REPO_DIR, "tools", "firma.py")
    return f"""{FIRMA_MARK[0]}
## Firemní Obsidian

Vedle osobního trezoru tohohle uživatele je společný **firemní Obsidian**
celého týmu: `{config.COMPANY_VAULT}`. Je jen ke čtení — firemní postupy,
kontakty a know-how hledej a čti tam.
{_company_skills_text()}{_links_text()}
Zapisovat do něj přímo nejde, jde to jen nástrojem níž. Jak to funguje, závisí
na tom, ve kterém tabu hubu běžíš — poznáš to podle proměnné `HUB_VAULT`:

**Firemní tab (`HUB_VAULT=firma`).** Uživatel ho otevřel proto, aby se pracovalo
nad firemním Obsidianem, a tím dal souhlas se zápisem. Poznámku připrav a rovnou
pošli: `python3 {tool} navrh "postupy/fakturace.md" - ` (obsah na standardní
vstup, jde i soubor místo `-`). Hub ji nahraje hned a napíše to dole v okně —
na nic se neptej a nečekej na kartu.

**Osobní tab (bez `HUB_VAULT`).** Tam je firemní Obsidian jen ke čtení a zápis
se potvrzuje:

1. Připrav poznámku v Markdownu a vyber pro ni cestu podle struktury, která už
   ve firemním trezoru je (třeba `postupy/fakturace.md`).
2. **Zeptej se v chatu:** stručně shrň, co do firemního nahraješ, napiš cílovou
   cestu a jestli tím přepíšeš existující poznámku (podívej se, jestli tam už
   je). Připomeň, že firemní Obsidian uvidí celý tým. Počkej na výslovné „ano".
3. Teprve pak pošli poznámku ke schválení:
   `python3 {tool} navrh "postupy/fakturace.md" poznamka.md --potvrzeno`.
   Přepínač `--potvrzeno` nikdy nepřidávej bez souhlasu uživatele v téhle
   konverzaci.
4. Řekni uživateli, že mu hub ukázal kartu s náhledem: poznámka se nahraje, až
   ji potvrdí tlačítkem **Nahrát**. Sám ji potvrdit nemůžeš.

Existující firemní poznámku upravíš tak, že pošleš celý nový obsah na stejnou
cestu — hub upozorní, že se přepíše.

**Co do firemního Obsidianu nikdy nepatří (ani ve firemním tabu):** hesla,
klíče, tokeny, přístupy a nastavení napojení (MCP, Google, účty) a osobní
poznámky uživatele z jeho osobního trezoru. Nahrávej vždy jen to, o co si
v téhle konverzaci řekl; když by z úkolu vyplývalo něco osobního, nejdřív se
zeptej. Hub navíc poznámku, která vypadá jako přihlašovací údaj, sám nenahraje
a zeptá se kartou.
{FIRMA_MARK[1]}
"""


def _company_claude_md(home, level="write"):
    """Pokyny k firemnímu Obsidianu v ~/.claude/CLAUDE.md prostoru. Mezi
    značkami se vždy přepíšou, zbytek souboru patří uživateli a zůstane.
    Bez přístupu (`none`) se blok smaže — Claude o trezoru nemá vědět."""
    text = safefs.read_text(home, ".claude/CLAUDE.md")
    if text is None:
        if safefs.is_file(home, ".claude/CLAUDE.md"):
            return                       # nečitelný (velký, jiné kódování) — nechat být
        text = ""                        # chybí, nebo je to odkaz — nahradí se
    block = _company_block(level) if level != "none" else ""
    start, end = text.find(FIRMA_MARK[0]), text.find(FIRMA_MARK[1])
    if start >= 0 and end > start:
        new = text[:start] + block.rstrip("\n") + text[end + len(FIRMA_MARK[1]):]
    elif block:
        new = (text.rstrip("\n") + "\n\n" if text.strip() else "") + block
    else:
        new = text
    if new != text:
        safefs.write_text(home, ".claude/CLAUDE.md", new)


def _company_settings(home, extra=(), company=True):
    """Firemní trezor (a sdílené Obsidiany z `extra`) mezi složkami, které Claude
    Code smí číst bez ptaní (permissions.additionalDirectories). Nečitelné
    nastavení se nepřepisuje."""
    rel = ".claude/settings.json"
    if not safefs.lstat(home, rel) or not safefs.is_file(home, rel):
        data = {}                        # chybí, nebo je to odkaz — nahradí se
    else:
        raw = safefs.read_text(home, rel, encoding="utf-8-sig")
        try:
            data = json.loads(raw) if raw is not None else None
        except ValueError:
            data = None
        if data is None:
            return
    if not isinstance(data, dict):
        return
    perms = data.get("permissions", {})
    if not isinstance(perms, dict):
        return
    dirs = perms.get("additionalDirectories", [])
    if not isinstance(dirs, list):
        return
    want = ([config.COMPANY_VAULT] if company else []) + list(extra)
    missing = [d for d in want if d not in dirs]
    # Bez přístupu k firemnímu se jeho cesta z povolených složek odebere —
    # stejně tak trezor firmy, ze které člověk odešel.
    kept = [d for d in dirs if (company or d != config.COMPANY_VAULT)
            and not (str(d).startswith(config.FIRMY_DIR + os.sep) and d not in want)]
    if not missing and len(kept) == len(dirs):
        return
    perms["additionalDirectories"] = kept + missing
    data["permissions"] = perms
    safefs.write_text(home, rel, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def company_rel(rel):
    """Cesta ve firemním trezoru: relativní, .md, bez `..` a skrytých částí.
    Absolutní cesta se odmítne — tiše ji brát jako relativní by jen mátlo."""
    raw = str(rel or "").replace("\\", "/").strip()
    rel = raw.strip("/")
    if rel and not rel.lower().endswith(".md"):
        rel += ".md"
    parts = [p for p in rel.split("/") if p not in ("", ".")]
    if (not parts or len(rel) > 300 or raw.startswith("/") or re.match(r"^[A-Za-z]:", rel)
            or any(p == ".." or p.startswith(".") for p in parts)):
        raise ValueError(f"Neplatná cesta ve firemním Obsidianu: {rel or '(prázdná)'}")
    return "/".join(parts)


def publish_company(user, pid, overwrite=False):
    """Nahraje návrh uživatele do firemního trezoru. Volá ji brána po kliknutí
    na Nahrát. Vrací {"ok", "path", ...}; když poznámka už existuje a
    `overwrite` není, nic nezapíše a vrátí `exists`.

    Návrh leží v domově uživatele, kam session zapisuje — může to být
    podvržený odkaz kamkoli na serveru. Čte se proto jen obyčejný soubor přímo
    na své cestě, bez následování odkazů.
    """
    data = _read_proposal(user, pid)
    firma = _firma(data.get("firma"))
    if not firmy.zapis(user, firma):
        raise ValueError(_NO_WRITE)
    if not isinstance(data.get("text"), str):
        raise ValueError("Návrh je poškozený.")
    src = os.path.join(os.path.realpath(home_for(user)), PENDING, str(pid) + ".json")
    result = write_company(user, data.get("cil"), data["text"], overwrite, firma=firma)
    if result.get("ok"):
        try:
            os.remove(src)
        except OSError:
            pass
    return result


_NO_WRITE = "Do firemního Obsidianu nemáš právo zapisovat — požádej admina nebo kapitána firmy."


def _firma(slug):
    """Zkratka firmy z návrhu nebo požadavku — bez ní dnešní firma."""
    slug = str(slug or firmy.LEGACY)
    if not firmy.exists(slug):
        raise ValueError("Taková firma tu není.")
    return slug


def write_company(user, rel, text, overwrite=False, via="", firma=firmy.LEGACY):
    """Zapíše poznámku do trezoru firmy `firma` za účet `user` — jen s právem
    zápisu. Sdílí ji potvrzovací karta v hubu i napojení z appky Claude (`via`
    se zapíše do záznamu o nahráních)."""
    firma = _firma(firma)
    if not firmy.zapis(user, firma):
        raise ValueError(_NO_WRITE)
    if not isinstance(text, str) or len(text.encode("utf-8")) > MAX_PROPOSAL:
        raise ValueError("Poznámka chybí, nebo je moc velká.")
    rel = company_rel(rel)
    if firma == firmy.LEGACY and slozky.special(rel):
        raise ValueError(f"{slozky.PEOPLE}/ a {slozky.SHARED}/ nejsou firemní poznámky: do "
                         "osobního Obsidianu se zapisuje rovnou, do sdíleného přes "
                         "sdílený Obsidian (tools/sdilene.py).")
    vault = os.path.realpath(ensure_company_vault() if firma == firmy.LEGACY
                             else firmy.vault_dir(firma))
    target = os.path.join(vault, *rel.split("/"))
    if os.path.commonpath([os.path.realpath(os.path.dirname(target)), vault]) != vault:
        raise ValueError("Neplatná cesta ve firemním Obsidianu.")
    if not poznamky.allowed(user, rel, firma):
        raise ValueError("Na téhle cestě je poznámka nebo složka, ke které nemáš přístup "
                         "— vyber jinou.")
    existed = os.path.exists(target)
    if existed and not overwrite:
        return {"ok": False, "exists": True, "path": rel}
    os.makedirs(os.path.dirname(target), exist_ok=True)
    if existed and os.path.dirname(rel) in poznamky.masked_dirs(firma):
        # Ve složce se skrytou poznámkou mají prostory každý soubor přivázaný
        # zvlášť (poznamky.mask_args) — nový soubor přes rename by v nich
        # zůstal ve staré podobě až do restartu. Přepsat na místě.
        with open(target, "r+", encoding="utf-8") as fh:
            fh.write(text)
            fh.truncate()
    else:
        tmp = target + ".hub-tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, target)
    entry = {"cas": time.strftime("%Y-%m-%d %H:%M:%S"), "email": user.get("email", ""),
             "cil": rel, "prepsano": existed, "znaku": len(text)}
    if via:
        entry["pres"] = via
    try:
        with open(os.path.join(firmy.firma_dir(firma), "nahrano.jsonl"), "a",
                  encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass
    return {"ok": True, "path": rel, "overwritten": existed}


def upload_company(user, rel, raw, overwrite=False, firma=firmy.LEGACY):
    """Soubor (jakýkoli) do trezoru firmy — nahrává ho člověk v hubu
    (gateway/soubory.py). Stejná práva jako poznámka: zápis a viditelnost."""
    from . import soubory
    firma = _firma(firma)
    if not firmy.zapis(user, firma):
        raise ValueError(_NO_WRITE)
    rel = soubory.clean_rel(rel)
    if firma == firmy.LEGACY and slozky.special(rel):
        raise ValueError(f"Do {slozky.SHARED}/ nahrávej jako do sdíleného Obsidianu, "
                         "osobní poznámky do osobního Obsidianu.")
    if not poznamky.allowed_path(user, rel, firma):
        raise ValueError("Do téhle složky nemáš přístup — vyber jinou.")
    in_place = os.path.dirname(rel) in poznamky.masked_dirs(firma)
    vault = ensure_company_vault() if firma == firmy.LEGACY else firmy.vault_dir(firma)
    result = soubory.write(vault, rel, raw, overwrite,
                           quota=soubory.COMPANY_QUOTA, in_place=in_place)
    if result.get("ok"):
        soubory.log(os.path.join(firmy.firma_dir(firma), "nahrano.jsonl"), user,
                    "firma" if firma == firmy.LEGACY else "firma:" + firma, result)
    return result


def delete_company(user, rel, firma=firmy.LEGACY):
    """Smaže poznámku nebo soubor z trezoru firmy — kapitán, nebo člen s právem
    mazat (firmy.mazat). Nic nemizí natrvalo: soubor se odloží do složky firmy
    mimo trezor (`smazano/`), odkud ho správce vrátí. Mazání klientů
    a podkategorií je v Nastavení → Firmy (firmy.smazat_slozku)."""
    from . import soubory
    firma = _firma(firma)
    if not firmy.mazat(user, firma):
        raise ValueError("Mazat ve firmě smí kapitán a ti, komu to kapitán povolí.")
    rel = soubory.clean_rel(rel)
    if firma == firmy.LEGACY and slozky.special(rel):
        raise ValueError("Osobní a sdílené složky se tady nemažou.")
    if not poznamky.allowed_path(user, rel, firma):
        raise ValueError("K téhle poznámce nemáš přístup.")
    vault = os.path.realpath(ensure_company_vault() if firma == firmy.LEGACY
                             else firmy.vault_dir(firma))
    src = os.path.join(vault, *rel.split("/"))
    if (os.path.islink(src) or not os.path.isfile(src)
            or os.path.commonpath([os.path.realpath(os.path.dirname(src)), vault]) != vault):
        raise ValueError("Taková poznámka tu není.")
    aside = os.path.join(firmy.firma_dir(firma), "smazano")
    os.makedirs(aside, exist_ok=True)
    os.rename(src, os.path.join(aside, time.strftime("%Y%m%d-%H%M%S-") + rel.replace("/", "__")))
    try:
        with open(os.path.join(firmy.firma_dir(firma), "nahrano.jsonl"), "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"cas": time.strftime("%Y-%m-%d %H:%M:%S"), "email": user.get("email", ""),
                                 "smazano": rel}, ensure_ascii=False) + "\n")
    except OSError:
        pass
    return {"ok": True, "path": rel}


# ── sdílené Obsidiany: pokyny pro Clauda a provedení návrhů ──────────────────
SHARED_MARK = ("<!-- claude-hub:sdilene -->", "<!-- /claude-hub:sdilene -->")


def _shared_block(mine):
    tool = os.path.join(REPO_DIR, "tools", "sdilene.py")
    if mine:
        rows = "\n".join(
            f"- **{v['name']}** (`{v['slug']}`, `{v['path']}`) — členové: "
            + ", ".join(m["name"] or m["email"] for m in v["members"])
            + (" · založil jsi ho ty" if v["is_owner"] else f" · založil {v['owner']}")
            for v in mine)
        have = "Tenhle uživatel je členem těchto (jen ke čtení — čti a hledej v nich):\n\n" + rows
    else:
        have = "Tenhle uživatel zatím není členem žádného."
    return f"""{SHARED_MARK[0]}
## Sdílené Obsidiany

Kromě osobního a firemního Obsidianu jsou sdílené Obsidiany jen pro vybrané lidi.
{have}

Všechno níže jen na požádání uživatele a vždy se **nejdřív zeptej v chatu**:
u založení na název a pro které lidi (nabídni lidi z týmu:
`python3 {tool} lide`), u zápisu do kterého sdíleného Obsidianu (vyjmenuj ty
výše), co a kam — a řekni, kdo to uvidí. Přepínač `--potvrzeno` přidej až po
výslovném „ano“. Pak mu hub ukáže kartu k potvrzení; sám ji potvrdit nemůžeš.

- Založit: `python3 {tool} zalozit "Marketing" --lide petr@firma.cz,jana@firma.cz --potvrzeno`
- Uložit poznámku: `python3 {tool} navrh marketing "kampane/zari.md" poznamka.md --potvrzeno`
  (obsah jde i na standardní vstup: `-`)
- Změnit členy (jen zakladatel): `python3 {tool} clenove marketing --pridat a@firma.cz --odebrat b@firma.cz --potvrzeno`
- Odejít (člen): `python3 {tool} odejit marketing --potvrzeno`
- Smazat (zakladatel): `python3 {tool} smazat marketing --potvrzeno`

Nový sdílený Obsidian i změna členů se v prostoru projeví až po jeho restartu —
hub restart nabídne v panelu Sdílené Obsidiany.
{SHARED_MARK[1]}
"""


def _shared_claude_md(home, mine):
    """Pokyny ke sdíleným Obsidianům v ~/.claude/CLAUDE.md — mezi značkami se
    přepíšou, zbytek souboru zůstane."""
    text = safefs.read_text(home, ".claude/CLAUDE.md")
    if text is None:
        if safefs.is_file(home, ".claude/CLAUDE.md"):
            return
        text = ""
    block = _shared_block(mine)
    start, end = text.find(SHARED_MARK[0]), text.find(SHARED_MARK[1])
    if start >= 0 and end > start:
        new = text[:start] + block.rstrip("\n") + text[end + len(SHARED_MARK[1]):]
    else:
        new = (text.rstrip("\n") + "\n\n" if text.strip() else "") + block
    if new != text:
        safefs.write_text(home, ".claude/CLAUDE.md", new)


# ── další firmy: pokyny pro Clauda ───────────────────────────────────────────
FIRMY_MARK = ("<!-- claude-hub:firmy -->", "<!-- /claude-hub:firmy -->")


def _skill_categories(path):
    try:
        return [d for d in sorted(os.listdir(path)) if not d.startswith(("_", "."))
                and os.path.isdir(os.path.join(path, d))]
    except OSError:
        return []


def _firmy_block(other, legacy_member=False):
    """Pokyny k firmám (gateway/firmy.py) — kromě dnešního firemního
    Obsidianu, ten má vlastní blok. Bez firem prázdné."""
    if not other:
        return ""
    tool = os.path.join(REPO_DIR, "tools", "firma.py")
    rows = []
    for f in other:
        cats = _skill_categories(os.path.join(f["cesta"], "skills"))
        struktura = (f"`{firmy.KLIENTI}/<klient>/<podkategorie>/`" if f.get("druh") != "projekty"
                     else f"`{firmy.PROJEKTY}/<projekt>/`")
        rows.append(f"- **{f['nazev']}** (zkratka `{f['zkratka']}`, `{f['cesta']}`) — role: "
                    + firmy.ROLE_LABELS.get(f["role"], f["role"])
                    + (" (jen čtení)" if f["role"] == "host" else "")
                    + f"; práce je v {struktura}"
                    + (f"; skilly firmy: {', '.join(cats)}" if cats else ""))
    common = ""
    if not legacy_member and _skill_categories(config.COMPANY_SKILLS):
        common = (f"\nSpolečné skilly pro všechny firmy jsou v `{config.COMPANY_SKILLS}"
                  "/<kategorie>/<skill>/SKILL.md` — podle zadání vyber nejvýš dva tři a přečti "
                  "jejich `SKILL.md`, než začneš.\n")
    return f"""{FIRMY_MARK[0]}
## Firmy

Tenhle uživatel je členem firem — každá má vlastní Obsidian (jen ke čtení):

{chr(10).join(rows)}
{common}
**Struktura.** Obecné věci firmy jsou v kořeni jejího trezoru. Agentura má práci
pro klienty v `{firmy.KLIENTI}/<klient>/<podkategorie>/` (třeba `{firmy.KLIENTI}/Pytlík/Web/`),
firma, která pracuje pro sebe, v `{firmy.PROJEKTY}/<projekt>/` (u každé firmy výš).
Vidíš jen to, k čemu má uživatel přístup — co nevidíš, neexistuje pro tebe;
o přístup se žádá kapitána firmy.

**Pravidla složek.** Než do složky firmy cokoli zapíšeš, přečti si `CLAUDE.md`
v ní a ve všech složkách nad ní až po kořen trezoru firmy, a drž se jich.

**Firmy nemíchej.** Co patří jedné firmě, nepiš do jiné, ani do osobního
Obsidianu. Kterou firmu tab řeší, poznáš podle `HUB_VAULT=firma:<zkratka>`.

**Zápis** jde jen nástrojem (trezory jsou jen ke čtení), host nezapisuje:

- Firemní tab (`HUB_VAULT=firma:<zkratka>`): poznámku připrav a rovnou pošli
  `python3 {tool} navrh "Klienti/Pytlík/Web/stav.md" - ` (obsah na standardní
  vstup) — nahraje se hned do firmy tabu.
- Osobní tab: nejdřív se v chatu zeptej (co, kam, do které firmy, kdo to uvidí),
  po výslovném „ano“ `python3 {tool} navrh --firma <zkratka> "cesta.md" soubor.md
  --potvrzeno`; uživatel to potvrdí kartou v hubu.

Hesla, klíče, tokeny a přístupy do firem nikdy nepiš — patří do napojení (MCP),
která firma sdílí.
{FIRMY_MARK[1]}
"""


def _firmy_claude_md(home, other, legacy_member=False):
    """Blok o firmách v ~/.claude/CLAUDE.md — mezi značkami se přepíše,
    bez firem se smaže, zbytek souboru zůstane."""
    text = safefs.read_text(home, ".claude/CLAUDE.md")
    if text is None:
        if safefs.is_file(home, ".claude/CLAUDE.md"):
            return
        text = ""
    block = _firmy_block(other, legacy_member)
    start, end = text.find(FIRMY_MARK[0]), text.find(FIRMY_MARK[1])
    if start >= 0 and end > start:
        new = text[:start] + block.rstrip("\n") + text[end + len(FIRMY_MARK[1]):]
        if not block:
            new = new.rstrip("\n") + "\n" if new.strip() else ""
    elif block:
        new = (text.rstrip("\n") + "\n\n" if text.strip() else "") + block
    else:
        new = text
    if new != text:
        safefs.write_text(home, ".claude/CLAUDE.md", new)


def apply_proposal(user, pid, overwrite=False):
    """Provede návrh z karty v hubu: firemní Obsidian, nebo sdílený (druh
    sdilene-*). Oprávnění ověřuje shared.py podle registru, ne podle návrhu."""
    data = _read_proposal(user, pid)
    kind = data.get("druh") or "firma"
    if kind == "firma":
        return publish_company(user, pid, overwrite)
    if kind == "sdilene-zapis":
        result = shared.write_note(user, data.get("vault"), data.get("cil"), data.get("text"), overwrite)
    elif kind == "sdilene-zalozit":
        result = shared.create(user, data.get("nazev"), data.get("emaily"))
    elif kind == "sdilene-clenove":
        result = shared.change_members(user, data.get("vault"), data.get("emaily_pridat"),
                                       data.get("emaily_odebrat"))
    elif kind == "sdilene-odejit":
        result = shared.leave(user, data.get("vault"))
    elif kind == "sdilene-smazat":
        result = shared.delete(user, data.get("vault"))
    else:
        raise ValueError("Neznámý návrh.")
    if kind in ("sdilene-zalozit", "sdilene-smazat") and result.get("ok"):
        try:
            slozky.sync()                # Sdílené/<název> ve firemním trezoru
        except (OSError, ValueError):
            pass
    if result.get("ok"):
        try:
            os.remove(os.path.join(os.path.realpath(home_for(user)), PENDING, str(pid) + ".json"))
        except OSError:
            pass
    return result


def _read_proposal(user, pid):
    """Návrh z domova uživatele — bez následování odkazů (domov patří session)."""
    pid = str(pid or "")
    if not PENDING_ID.fullmatch(pid):
        raise ValueError("Neplatný návrh.")
    home = os.path.realpath(home_for(user))
    src = os.path.join(home, PENDING, pid + ".json")
    gone = ValueError("Návrh už není — nejspíš byl nahraný nebo zahozený.")
    if os.path.realpath(src) != src:
        raise gone
    try:
        fd = os.open(src, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        raise gone from None
    with os.fdopen(fd, "rb") as fh:
        raw = fh.read(MAX_PROPOSAL + 1)
    if len(raw) > MAX_PROPOSAL:
        raise ValueError("Návrh je moc velký.")
    try:
        data = json.loads(raw.decode("utf-8"))
    except ValueError:
        raise ValueError("Návrh je poškozený.") from None
    if not isinstance(data, dict):
        raise ValueError("Návrh je poškozený.")
    return data


def session_spec(user, isolation, mode):
    """Co předat pty backendu, aby se spustila izolovaná instance hubu.

    Vrací (argv, home, unit, verze, skryté). `argv` je už obalené izolací a limity;
    `home` je pracovní složka i jediné zapisovatelné místo session; `unit` je
    jméno systemd scope, podle kterého se prostor pozná a zastaví (prázdné,
    když na stroji systemd-run není a limity se nepoužijí); `verze` = verze
    hubu, na které prostor pojede; `skryté` = {zkratka firmy: cesty}, které
    v prostoru nejsou (gateway/poznamky.py, gateway/firmy.py).
    """
    # Bez sandboxu se jiná složka na místo REPO_DIR přivázat nedá — tam jede
    # prostor přímo ze zdroje.
    kod, verze = code_dir() if mode in ("bwrap", "docker") else (REPO_DIR, verze_zdroje(REPO_DIR))
    paths = ensure(user, kod)
    home = paths["home"]
    inner = [PYTHON, os.path.join(REPO_DIR, "claude-hub.py"), "--no-browser"]
    # Hub ke čtení — připravená verze pod jménem REPO_DIR. Nic víc: klíč API
    # přijde v prostředí (session_env), žádný soubor mimo domov se do sandboxu
    # nepřivazuje. Firemní Obsidian taky jen ke čtení: zapisuje do něj brána
    # po potvrzení.
    # Bez přístupu k firemnímu se do sandboxu vůbec nepřiváže — neuvidí ho
    # ani Claude, ani terminál.
    legacy_member = company_level(user) != "none"
    company = [config.COMPANY_VAULT] if legacy_member else []
    # Poznámky, které uživatel vidět nemá, v sandboxu vůbec nejsou. Jinak než
    # přes bwrap je vynechat neumíme — tam se radši nepřiváže celý trezor.
    hidden = poznamky.hidden_for(user) if company else []
    always = poznamky.always_masked(user) if company else []
    masks = (poznamky.mask_args(config.COMPANY_VAULT, hidden, always)
             if (hidden or always) and mode == "bwrap" else [])
    # Cizí odkazy v Lidé/ a Sdílené/ vedou v sandboxu do prázdna — kvůli nim
    # celý trezor schovávat nemusíme. Kvůli omezené poznámce ano.
    if mode != "bwrap" and ([h for h in hidden if not slozky.special(h)] or always):
        company = []
    # Co bylo při startu skryté, po firmách, kde je členem — podle toho brána
    # pozná, že se přístupy změnily a prostor chce restart (i u firmy, která se
    # bez bwrap nepřivázala).
    skryte = {firmy.LEGACY: hidden} if legacy_member else {}
    # Další firmy (gateway/firmy.py): každá jen ke čtení, se svými skrytými
    # klienty, podkategoriemi a poznámkami. Kdo ve firmě není, nemá ji vůbec.
    other = []
    for f in firmy.firmy_for(user):
        if f["zkratka"] == firmy.LEGACY:
            continue
        f_hidden = poznamky.hidden_for(user, firma=f["zkratka"])
        f_always = poznamky.always_masked(user, f["zkratka"])
        skryte[f["zkratka"]] = f_hidden
        if (f_hidden or f_always) and mode != "bwrap":
            continue
        other.append(f["cesta"])
        if f_hidden or f_always:
            masks += poznamky.mask_args(f["cesta"], f_hidden, f_always)
    # Skilly z dnešního firemního trezoru jsou pro všechny firmy — kdo do něj
    # nepatří, dostane aspoň je (jen ke čtení), pokud v nich nic omezené není.
    if other and not company and _skills_open():
        other.append(config.COMPANY_SKILLS)
    extra_ro = ([(kod, REPO_DIR)] + company + [v["path"] for v in shared.vaults_for(user)]
                + other)
    unit = unit_name(user)
    # Stará cesta u<id> vede v sandboxu na nový domov: cache (uv, npx) mají
    # absolutní cesty zapečené uvnitř a přepisovat je by bylo křehké.
    legacy = os.path.join(USERS_ROOT, slug(user))
    argv = isolation.wrap(mode, inner, home, extra_ro=extra_ro, unit=unit,
                          aliases=[legacy] if legacy != home else [],
                          ucet=slug(user), masks=masks)
    if argv[:1] not in (["systemd-run"], ["sudo"]):
        unit = ""                 # bez scope (docker, stroj bez systemd)
    return argv, home, unit, verze, skryte


def _skills_open():
    """Firemní skilly jdou přivázat i lidem mimo dnešní firmu: složka je
    a žádná poznámka ani složka v ní není omezená (poznamky.py)."""
    if not os.path.isdir(config.COMPANY_SKILLS):
        return False
    data = poznamky._load()
    return not any(rel == "skills" or rel.startswith("skills/")
                   for rel in list(data["poznamky"]) + list(data["slozky"]))
