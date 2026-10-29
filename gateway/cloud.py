"""
Cloud prostoru — Google Disk nebo OneDrive člověka jako složka v jeho domově.

Disk brány je jeden pro všechny a pořád dochází. Lidé ale mají vlastní cloud,
kam se vejde mnohem víc. Tady se ten cloud připojí do prostoru jako obyčejná
složka (`~/Cloud/Google Disk`, `~/Cloud/OneDrive`), takže:

* **Claude v něm čte a hledá** jako v kterékoli jiné složce — podklady, smlouvy,
  fotky. Blok v ~/.claude/CLAUDE.md mu řekne, že tam je.
* **Velké věci jdou z disku brány pryč**: „Přesunout do cloudu" pošle složku
  nebo soubor do `<cloud>/Aladin/<cesta>` a na původním místě nechá odkaz, takže
  cesty dál fungují. Vrátit jde stejně snadno.
* **Automaticky**: po prvním ručním přesunu jde zapnout noční přesun velkých
  souborů, na které dlouho nikdo nesáhl (`automaticky`, volá uloziste.noc).

Jak to drží pohromadě:

* Připojuje **rclone** (`rclone mount`) jako transientní služba systemd
  uživatele brány (`claude-hub-cloud-<účet>-<služba>`). Uvnitř sandboxu prostoru
  (bwrap) to nejde — připojí se zvenku do domova a sandbox ho vidí, protože
  domov je do něj přivázaný i s podřízenými připojeními (ověřeno i pro disk
  připojený až za běhu prostoru).
* **Přihlášení**: Google se vezme z napojení Google, které člověk už má
  (workspace-mcp, rozsah `drive`) — žádné druhé přihlašování. OneDrive přes
  kód zařízení (microsoft.com/devicelogin) s aplikací správce
  (`HUB_GW_ONEDRIVE_CLIENT_ID`).
* Konfigurace rclone s tokeny i stav leží **mimo domov** (`<brána>/cloud/`):
  do domova session zapisuje, a kdyby si mohla přepsat, kam a co se přesouvá,
  přesouvala by brána cizí věci.
* Mezipaměť rclone má strop (`HUB_GW_CLOUD_CACHE_MAX`), ať cloud disk brány
  nezaplní místo, aby ho šetřil.

Brána tu je jediná, kdo s cloudem sahá na domov, a dělá to přes safefs —
odkaz, který si session nastraží, nikam jinam nepovede.
"""
import configparser
import datetime
import fcntl
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from . import config, safefs, workspace

MB = 1024 * 1024
GB = 1024 * MB
DIR = os.environ.get("HUB_GW_CLOUD_DIR", os.path.join(config.GATEWAY_DIR, "cloud"))
RCLONE = os.environ.get("HUB_GW_RCLONE") or shutil.which("rclone") or "rclone"
SLOZKA = "Cloud"                 # ~/Cloud/<název služby>
VE_CLOUDU = "Aladin"             # kam v cloudu jdou věci přesunuté z prostoru
CACHE_MAX = os.environ.get("HUB_GW_CLOUD_CACHE_MAX", "512M")
# Automatický přesun: soubory aspoň AUTO_MB, nezměněné ani neotevřené AUTO_DNI
# dní, za noc nejvýš AUTO_STROP_GB (první noc by jinak mohla jet do rána).
AUTO_MB = int(os.environ.get("HUB_GW_CLOUD_AUTO_MB", "50"))
AUTO_DNI = int(os.environ.get("HUB_GW_CLOUD_AUTO_DNI", "30"))
AUTO_STROP_GB = int(os.environ.get("HUB_GW_CLOUD_AUTO_STROP_GB", "20"))
UNIT_PREFIX = "claude-hub-cloud-"

SLUZBY = {
    "google": {"nazev": "Google Disk", "typ": "drive"},
    "onedrive": {"nazev": "OneDrive", "typ": "onedrive"},
}

# Co se z domova přesouvat nesmí: nastavení, přihlášení, paměť, cloud sám.
# Skryté složky (.claude, .config, .ssh…) se nepřesouvají nikdy.
NEPRESOUVAT = {SLOZKA, "Obsidian"}
# Kudy automatika nechodí — věci, které si nástroje přepisují samy.
AUTO_PRESKOCIT = {"node_modules", ".venv", "venv", "__pycache__", "site-packages", "vendor"}

MS_CLIENT = os.environ.get("HUB_GW_ONEDRIVE_CLIENT_ID", "").strip()
MS_LOGIN = "https://login.microsoftonline.com/common/oauth2/v2.0"
MS_SCOPES = "Files.ReadWrite.All offline_access User.Read"
MS_GRAPH = "https://graph.microsoft.com/v1.0"

CLOUD_MARK = ("<!-- hub:cloud -->", "<!-- /hub:cloud -->")

_zamek = threading.Lock()
_ceka_onedrive = {}              # klíč účtu → rozběhnuté přihlášení kódem zařízení
_procesy = []


# ── stav účtu ────────────────────────────────────────────────────────────────

def klic(user):
    """Jméno složky účtu v DIR. Podle id (e-mail se může změnit)."""
    return "u%d" % int(user["id"])


def _dir(user):
    path = os.path.join(DIR, klic(user))
    os.makedirs(path, mode=0o700, exist_ok=True)
    return path


def _conf(user):
    return os.path.join(_dir(user), "rclone.conf")


def _stav_path(user):
    return os.path.join(_dir(user), "stav.json")


def stav(user):
    try:
        with open(_stav_path(user), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        data = {}
    data.setdefault("sluzby", {})
    data.setdefault("presunuto", [])
    data.setdefault("auto", False)
    data.setdefault("prvni", 0)
    return data


def _uloz_stav(user, data):
    path = _stav_path(user)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def _uprav_stav(user, fn):
    with _zamek:
        data = stav(user)
        fn(data)
        _uloz_stav(user, data)
        return data


def _rclone_conf_zapis(user, sekce, hodnoty):
    """Sekce v rclone.conf účtu (0600, mimo domov). `hodnoty` None = smazat."""
    path = _conf(user)
    cp = configparser.ConfigParser(interpolation=None)
    cp.read(path, encoding="utf-8")
    if hodnoty is None:
        cp.remove_section(sekce)
    else:
        if cp.has_section(sekce):
            cp.remove_section(sekce)
        cp.add_section(sekce)
        for k, v in hodnoty.items():
            cp.set(sekce, k, str(v))
    fd = os.open(path + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        cp.write(fh)
    os.replace(path + ".tmp", path)


def _rclone(user, *args, timeout=60, check=True):
    cmd = [RCLONE, "--config", _conf(user), *args]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                             stdin=subprocess.DEVNULL)
    except FileNotFoundError:
        raise RuntimeError("Na serveru chybí rclone — správce ho doinstaluje (apt install rclone).") from None
    except subprocess.TimeoutExpired:
        raise RuntimeError("Cloud neodpověděl včas — zkus to za chvíli znovu.") from None
    if check and res.returncode != 0:
        chyba = (res.stderr or res.stdout or "").strip().splitlines()
        raise RuntimeError("rclone: " + (chyba[-1] if chyba else f"skončil s kódem {res.returncode}"))
    return res


# ── připojení služeb ─────────────────────────────────────────────────────────

def _google_ucty(home):
    """Napojení Google z domova (workspace-mcp): [(e-mail, data)] s refresh tokenem."""
    rel = ".google_workspace_mcp/credentials"
    try:
        fd = safefs.open_dir(home, safefs._parts(rel))
    except OSError:
        return []
    try:
        names = sorted(os.listdir(fd))
    finally:
        os.close(fd)
    out = []
    for fn in names:
        if not fn.endswith(".json"):
            continue
        email = urllib.parse.unquote(fn[:-5])
        text = safefs.read_text(home, rel + "/" + fn)
        try:
            data = json.loads(text or "")
        except ValueError:
            continue
        if isinstance(data, dict) and data.get("refresh_token") and "@" in email:
            out.append((email, data))
    return out


def pripojit_google(user, email=""):
    """Google Disk z už připojeného napojení Google. Vrací e-mail účtu."""
    home = workspace.home_for(user)
    ucty = _google_ucty(home)
    if email:
        ucty = [u for u in ucty if u[0].lower() == email.lower()]
    if not ucty:
        raise ValueError("Nejdřív připoj Google v Nastavení → Napojení, Disk se vezme odtamtud.")
    email, data = ucty[0]
    scopes = data.get("scopes") or []
    if isinstance(scopes, str):
        scopes = scopes.split()
    if scopes and not any(s.endswith("/auth/drive") for s in scopes):
        raise ValueError("Google je připojený bez přístupu k Disku — v Napojení se přihlas znovu.")
    if not (data.get("client_id") and data.get("client_secret")):
        raise ValueError("Napojení Google nemá údaje klienta — v Napojení se přihlas znovu.")
    expiry = str(data.get("expiry") or "")
    token = {"access_token": data.get("token") or "", "token_type": "Bearer",
             "refresh_token": data["refresh_token"],
             # workspace-mcp píše čas bez zóny (UTC); rclone chce RFC 3339.
             "expiry": (expiry + "Z") if expiry and not re.search(r"(Z|[+-]\d\d:\d\d)$", expiry)
             else (expiry or "2000-01-01T00:00:00Z")}
    _rclone_conf_zapis(user, "google", {
        "type": "drive", "client_id": data["client_id"], "client_secret": data["client_secret"],
        "scope": "drive", "token": json.dumps(token)})
    try:
        _rclone(user, "lsf", "google:", "--max-depth", "1", "--dirs-only", timeout=45)
    except RuntimeError:
        _rclone_conf_zapis(user, "google", None)
        raise
    _uprav_stav(user, lambda d: d["sluzby"].__setitem__(
        "google", {"ucet": email, "od": int(time.time())}))
    pripojit_disk(user, "google")
    claude_md(user)
    return email


def _post_form(url, data, timeout=20):
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        try:
            return json.loads(exc.read().decode("utf-8") or "{}")
        except ValueError:
            return {"error": f"HTTP {exc.code}"}


def onedrive_nastaven():
    return bool(MS_CLIENT)


def onedrive_start(user):
    """Přihlášení k OneDrivu kódem zařízení. Vrací kód a adresu pro člověka."""
    if not MS_CLIENT:
        raise ValueError("OneDrive zatím správce nenastavil (chybí aplikace u Microsoftu).")
    r = _post_form(MS_LOGIN + "/devicecode", {"client_id": MS_CLIENT, "scope": MS_SCOPES})
    if not r.get("device_code"):
        raise RuntimeError(r.get("error_description") or r.get("error") or "Microsoft přihlášení nezačal.")
    _ceka_onedrive[klic(user)] = {"device_code": r["device_code"],
                                  "do": time.time() + int(r.get("expires_in") or 900),
                                  "interval": int(r.get("interval") or 5)}
    return {"kod": r.get("user_code"), "url": r.get("verification_uri") or "https://microsoft.com/devicelogin",
            "platnost": int(r.get("expires_in") or 900)}


def onedrive_hotovo(user):
    """Dokončilo se přihlášení kódem? {'ceka': True} / {'ucet': …}."""
    k = klic(user)
    ceka = _ceka_onedrive.get(k)
    if not ceka:
        raise ValueError("Přihlášení k OneDrivu neběží — začni znovu.")
    if time.time() > ceka["do"]:
        _ceka_onedrive.pop(k, None)
        raise ValueError("Kód vypršel — začni znovu.")
    r = _post_form(MS_LOGIN + "/token", {
        "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
        "client_id": MS_CLIENT, "device_code": ceka["device_code"]})
    if r.get("error") in ("authorization_pending", "slow_down"):
        return {"ceka": True}
    if not r.get("access_token"):
        _ceka_onedrive.pop(k, None)
        raise RuntimeError(r.get("error_description") or r.get("error") or "Přihlášení se nepovedlo.")
    _ceka_onedrive.pop(k, None)
    req = urllib.request.Request(MS_GRAPH + "/me/drive",
                                 headers={"Authorization": "Bearer " + r["access_token"]})
    with urllib.request.urlopen(req, timeout=20) as resp:
        drive = json.loads(resp.read().decode("utf-8"))
    ucet = ((drive.get("owner") or {}).get("user") or {}).get("email") or \
        ((drive.get("owner") or {}).get("user") or {}).get("displayName") or "OneDrive"
    expiry = (datetime.datetime.now(datetime.timezone.utc)
              + datetime.timedelta(seconds=int(r.get("expires_in") or 3600)))
    token = {"access_token": r["access_token"], "token_type": "Bearer",
             "refresh_token": r.get("refresh_token") or "",
             "expiry": expiry.strftime("%Y-%m-%dT%H:%M:%SZ")}
    _rclone_conf_zapis(user, "onedrive", {
        "type": "onedrive", "client_id": MS_CLIENT, "client_secret": "",
        "token": json.dumps(token), "drive_id": drive.get("id") or "",
        "drive_type": drive.get("driveType") or "personal"})
    _uprav_stav(user, lambda d: d["sluzby"].__setitem__(
        "onedrive", {"ucet": ucet, "od": int(time.time())}))
    pripojit_disk(user, "onedrive")
    claude_md(user)
    return {"ucet": ucet}


def odpojit(user, sluzba):
    """Odpojí službu: složka zmizí, přihlášení se smaže. Věci přesunuté do
    cloudu tam zůstávají — odkazy na ně v prostoru přestanou fungovat."""
    if sluzba not in SLUZBY:
        raise ValueError("Neznámý cloud.")
    odpojit_disk(user, sluzba)
    _rclone_conf_zapis(user, sluzba, None)

    def fn(d):
        d["sluzby"].pop(sluzba, None)
        if not d["sluzby"]:
            d["auto"] = False
    _uprav_stav(user, fn)
    claude_md(user)


# ── složka v domově (rclone mount) ───────────────────────────────────────────

def _env():
    env = dict(os.environ)
    env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    env.setdefault("DBUS_SESSION_BUS_ADDRESS", f"unix:path={env['XDG_RUNTIME_DIR']}/bus")
    return env


def unit(user, sluzba):
    return UNIT_PREFIX + klic(user) + "-" + sluzba


def mountpoint(user, sluzba, home=None):
    home = home or workspace.home_for(user)
    return os.path.join(home, SLOZKA, SLUZBY[sluzba]["nazev"])


def je_pripojeno(path):
    try:
        return os.path.ismount(path)
    except OSError:
        return False


def pripojit_disk(user, sluzba, home=None):
    """Připojí službu do ~/Cloud/<název>. Běží-li už, nic nedělá."""
    home = home or workspace.home_for(user)
    mp = mountpoint(user, sluzba, home)
    if je_pripojeno(mp):
        return True
    # Skutečné složky, žádný odkaz: session si jinak může `Cloud` nastražit
    # jako odkaz a připojit cloud někomu jinému do domova.
    fd = safefs.open_dir(home, [SLOZKA, SLUZBY[sluzba]["nazev"]], create=True, heal=True, mode=0o700)
    os.close(fd)
    if os.path.realpath(mp) != mp:
        raise RuntimeError("Složka pro cloud není skutečná složka.")
    cache = os.path.join(_dir(user), "mezipamet")
    os.makedirs(cache, mode=0o700, exist_ok=True)
    u = unit(user, sluzba)
    subprocess.run(["systemctl", "--user", "reset-failed", u + ".service"],
                   capture_output=True, env=_env(), timeout=15)
    cmd = ["systemd-run", "--user", "--unit", u, "--collect",
           "-p", "Restart=on-failure", "-p", "RestartSec=30",
           "-p", "Nice=10", "-p", "MemoryMax=400M",
           RCLONE, "mount", sluzba + ":", mp,
           "--config", _conf(user), "--cache-dir", cache,
           "--vfs-cache-mode", "writes", "--vfs-cache-max-size", CACHE_MAX,
           "--vfs-cache-max-age", "24h", "--dir-cache-time", "1m",
           "--umask", "077", "--log-level", "NOTICE",
           # Co do prázdné složky spadlo, dokud cloud nebyl připojený, nesmí
           # připojení zablokovat (zůstane jen schované pod ním).
           "--allow-non-empty",
           "--log-file", os.path.join(_dir(user), sluzba + ".log")]
    res = subprocess.run(cmd, capture_output=True, text=True, env=_env(), timeout=30)
    if res.returncode != 0:
        raise RuntimeError("Cloud se nepřipojil: " + (res.stderr or "").strip()[-200:])
    for _ in range(40):
        if je_pripojeno(mp):
            return True
        time.sleep(0.25)
    return je_pripojeno(mp)


def odpojit_disk(user, sluzba, home=None):
    home = home or workspace.home_for(user)
    mp = mountpoint(user, sluzba, home)
    subprocess.run(["systemctl", "--user", "stop", unit(user, sluzba) + ".service"],
                   capture_output=True, env=_env(), timeout=30)
    if je_pripojeno(mp):
        for prog in ("fusermount3", "fusermount"):
            if shutil.which(prog):
                subprocess.run([prog, "-uz", mp], capture_output=True, timeout=15)
                break


def pred_startem(user, home):
    """Volá brána před spuštěním prostoru: připojí cloudy a obnoví blok
    v CLAUDE.md. Nikdy nesmí zastavit start — chyba jen do logu."""
    d = stav(user)
    for sluzba in list(d["sluzby"]):
        try:
            pripojit_disk(user, sluzba, home)
        except Exception as exc:                  # noqa: BLE001 — start má proběhnout i bez cloudu
            print(f"cloud: {sluzba} pro {user.get('email')}: {exc}", file=sys.stderr, flush=True)
    try:
        claude_md(user, home)
    except Exception as exc:                      # noqa: BLE001
        print(f"cloud: CLAUDE.md pro {user.get('email')}: {exc}", file=sys.stderr, flush=True)


# ── přesun do cloudu a zpátky ────────────────────────────────────────────────

def _cista_cesta(rel):
    """Relativní cesta v domově, kterou smí brána poslat do cloudu."""
    rel = str(rel or "").strip()
    if rel.startswith("/"):
        raise ValueError("Cesta musí být v domově (~/…).")
    parts = safefs._parts(rel[1:] if rel.startswith("~") else rel)
    if parts[0] in NEPRESOUVAT or any(p.startswith(".") for p in parts):
        raise ValueError("Tohle se do cloudu přesouvat nedá (nastavení, paměť nebo cloud sám).")
    return "/".join(parts)


def _velikost(path):
    st = os.lstat(path)
    if not stat.S_ISDIR(st.st_mode):
        return st.st_blocks * 512
    celkem = 0
    for koren, dirs, files in os.walk(path):
        for n in files + dirs:
            try:
                celkem += os.lstat(os.path.join(koren, n)).st_blocks * 512
            except OSError:
                pass
    return celkem


def _sluzba_pro(d, sluzba=""):
    if sluzba:
        if sluzba not in d["sluzby"]:
            raise ValueError("Tenhle cloud nemáš připojený.")
        return sluzba
    for s in SLUZBY:
        if s in d["sluzby"]:
            return s
    raise ValueError("Nejdřív si připoj Google Disk nebo OneDrive.")


def presunout(user, rel, sluzba="", auto=False, log=print):
    """Pošle soubor/složku z domova do <cloud>/Aladin/<rel> a nechá na jejím
    místě odkaz do připojené složky. Vrací záznam o přesunu."""
    home = workspace.home_for(user)
    rel = _cista_cesta(rel)
    d = stav(user)
    sluzba = _sluzba_pro(d, sluzba)
    src = os.path.join(home, rel)
    # Žádný díl cesty nesmí být odkaz (cizí domov, systém…), cíl musí existovat.
    parts = rel.split("/")
    fd = safefs.open_dir(home, parts[:-1])
    try:
        st = os.stat(parts[-1], dir_fd=fd, follow_symlinks=False)
    finally:
        os.close(fd)
    if stat.S_ISLNK(st.st_mode):
        raise ValueError("Tohle už je odkaz (možná už v cloudu je).")
    if not (stat.S_ISDIR(st.st_mode) or stat.S_ISREG(st.st_mode)):
        raise ValueError("Přesouvat jde jen soubory a složky.")
    if os.path.realpath(src) != src:
        raise ValueError("Cesta vede přes odkaz — přesouvat nejde.")
    bajtu = _velikost(src)
    cil = f"{sluzba}:{VE_CLOUDU}/{rel}"
    log(f"přesouvám ~/{rel} ({bajtu // MB} MB) → {SLUZBY[sluzba]['nazev']}")
    if stat.S_ISDIR(st.st_mode):
        # --links: odkazy uvnitř složky jdou jako odkazy (.rclonelink), ne
        # jejich cíl — jinak by se do cloudu dostalo i to, na co jen ukazují.
        _rclone(user, "move", src, cil, "--delete-empty-src-dirs", "--links", "--transfers", "2",
                timeout=12 * 3600)
        zbylo = os.path.isdir(src) and any(os.scandir(src))
        if zbylo:
            raise RuntimeError("Část souborů se nepřesunula — zkus to znovu, nic se neztratilo.")
        if os.path.isdir(src):
            os.rmdir(src)
    else:
        _rclone(user, "moveto", src, cil, timeout=6 * 3600)
        if os.path.lexists(src):
            raise RuntimeError("Soubor se nepřesunul — zkus to znovu, nic se neztratilo.")
    odkaz = os.path.join(mountpoint(user, sluzba, home), VE_CLOUDU, rel)
    fd = safefs.open_dir(home, parts[:-1])
    try:
        os.symlink(odkaz, parts[-1], dir_fd=fd)
    finally:
        os.close(fd)
    zaznam = {"cesta": rel, "bajtu": bajtu, "kam": sluzba, "kdy": int(time.time()),
              "auto": bool(auto), "slozka": stat.S_ISDIR(st.st_mode)}

    def fn(dd):
        dd["presunuto"] = [z for z in dd["presunuto"] if z.get("cesta") != rel] + [zaznam]
        if not auto and not dd.get("prvni"):
            dd["prvni"] = int(time.time())
    _uprav_stav(user, fn)
    return zaznam


def vratit(user, rel, log=print):
    """Vrátí přesunutou věc z cloudu zpátky do domova (místo odkazu)."""
    home = workspace.home_for(user)
    rel = _cista_cesta(rel)
    d = stav(user)
    zaznam = next((z for z in d["presunuto"] if z.get("cesta") == rel), None)
    if not zaznam:
        raise ValueError("Tohle v seznamu přesunutých není.")
    sluzba = zaznam["kam"]
    if sluzba not in d["sluzby"]:
        raise ValueError(f"{SLUZBY[sluzba]['nazev']} není připojený — připoj ho a zkus to znovu.")
    parts = rel.split("/")
    fd = safefs.open_dir(home, parts[:-1], create=True)
    try:
        try:
            st = os.stat(parts[-1], dir_fd=fd, follow_symlinks=False)
            if not stat.S_ISLNK(st.st_mode):
                raise ValueError(f"Na místě ~/{rel} už je něco jiného — přesuň to stranou a zkus znovu.")
            os.unlink(parts[-1], dir_fd=fd)
        except FileNotFoundError:
            pass
    finally:
        os.close(fd)
    src = f"{sluzba}:{VE_CLOUDU}/{rel}"
    dst = os.path.join(home, rel)
    log(f"vracím ~/{rel} z {SLUZBY[sluzba]['nazev']}")
    if zaznam.get("slozka"):
        _rclone(user, "move", src, dst, "--delete-empty-src-dirs", "--links", timeout=12 * 3600)
    else:
        _rclone(user, "moveto", src, dst, timeout=6 * 3600)
    _uprav_stav(user, lambda dd: dd.__setitem__(
        "presunuto", [z for z in dd["presunuto"] if z.get("cesta") != rel]))


# ── běh na pozadí (přesun velké složky trvá i hodiny) ────────────────────────

def _beh_path(user):
    return os.path.join(_dir(user), "beh.json")


def beh(user):
    """Co se pro účet právě přesouvá (nebo jak dopadl poslední přesun)."""
    try:
        with open(_beh_path(user), encoding="utf-8") as fh:
            b = json.load(fh)
    except (OSError, ValueError):
        return None
    if b.get("bezi") and not _pid_zije(b.get("pid")):
        b["bezi"] = False
        b.setdefault("chyba", "Přesun se přerušil — spusť ho znovu, nic se neztratilo.")
    return b


def _pid_zije(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, TypeError, ValueError):
        return False


def spustit(user, akce, rel, sluzba=""):
    """Pustí přesun / vrácení jako samostatný proces. False = už něco běží."""
    b = beh(user)
    if b and b.get("bezi"):
        return False
    _cista_cesta(rel)                       # nesmysl odmítnout hned, ne až v procesu
    argv = [sys.executable, "-m", "gateway.cloud", akce, "--uid", str(int(user["id"])), "--cesta", rel]
    if sluzba:
        argv += ["--sluzba", sluzba]
    if shutil.which("ionice"):
        argv = ["ionice", "-c", "3"] + argv
    if shutil.which("nice"):
        argv = ["nice", "-n", "19"] + argv
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(_dir(user), "beh.log"), "a", encoding="utf-8") as log:
        log.write(time.strftime("%Y-%m-%d %H:%M:%S") + f" {akce} {rel}\n")
        log.flush()
        p = subprocess.Popen(argv, cwd=root, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                             start_new_session=True)
    _procesy[:] = [x for x in _procesy if x.poll() is None] + [p]
    _zapis_beh(user, {"bezi": True, "pid": p.pid, "akce": akce, "cesta": rel, "od": int(time.time())})
    return True


def _zapis_beh(user, data):
    path = _beh_path(user)
    with open(path + ".tmp", "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False)
    os.replace(path + ".tmp", path)


# ── automatika (noc) ─────────────────────────────────────────────────────────

def kandidati(home, ted=None, min_bajtu=None, dni=None):
    """Velké soubory, na které dlouho nikdo nesáhl: [(rel, bajtu)], největší
    první. Neleze do skrytých složek, gitových repozitářů, cloudu, paměti ani
    do složek, které si nástroje přepisují samy; odkazy nenásleduje."""
    ted = ted or time.time()
    min_bajtu = AUTO_MB * MB if min_bajtu is None else min_bajtu
    hranice = ted - (AUTO_DNI if dni is None else dni) * 86400
    out = []
    zasobnik = [""]
    while zasobnik:
        rel = zasobnik.pop()
        path = os.path.join(home, rel) if rel else home
        try:
            with os.scandir(path) as it:
                polozky = list(it)
        except OSError:
            continue
        if rel and any(e.name == ".git" for e in polozky):
            continue                           # repozitář — odkaz by změnil jeho obsah
        for e in polozky:
            if e.name.startswith(".") or (not rel and e.name in NEPRESOUVAT) or e.name in AUTO_PRESKOCIT:
                continue
            try:
                st = e.stat(follow_symlinks=False)
            except OSError:
                continue
            sub = (rel + "/" + e.name) if rel else e.name
            if stat.S_ISDIR(st.st_mode):
                zasobnik.append(sub)
            elif stat.S_ISREG(st.st_mode) and st.st_size >= min_bajtu \
                    and max(st.st_mtime, st.st_atime) < hranice and st.st_nlink == 1:
                out.append((sub, st.st_blocks * 512))
    out.sort(key=lambda x: -x[1])
    return out


def automaticky(users, log=print):
    """Noční přesun pro účty se zapnutou automatikou (po prvním ručním přesunu)."""
    souhrn = {}
    for u in users:
        if u.get("disabled"):
            continue
        d = stav(u)
        if not (d.get("auto") and d.get("prvni") and d["sluzby"]):
            continue
        b = beh(u)
        if b and b.get("bezi"):
            continue
        home = workspace.home_for(u)
        try:
            pripojit_disk(u, _sluzba_pro(d), home)
        except Exception as exc:              # noqa: BLE001 — bez složky odkazy nepovedou nikam
            log(f"cloud {u['email']}: nepřipojeno ({exc}) — dnes nic")
            continue
        strop, presunuto, chyby = AUTO_STROP_GB * GB, 0, 0
        for rel, bajtu in kandidati(home):
            if presunuto + bajtu > strop:
                break
            try:
                presunout(u, rel, auto=True, log=log)
                presunuto += bajtu
            except Exception as exc:          # noqa: BLE001 — jeden soubor nesmí zastavit noc
                chyby += 1
                log(f"cloud {u['email']}: ~/{rel}: {exc}")
        souhrn[u["email"]] = presunuto
        _uprav_stav(u, lambda dd: dd.__setitem__(
            "posledni_auto", {"kdy": int(time.time()), "bajtu": presunuto, "chyby": chyby}))
        if presunuto:
            log(f"cloud {u['email']}: přesunuto {presunuto // MB} MB")
    return souhrn


# ── Claude v prostoru ────────────────────────────────────────────────────────

def _blok(user, d, home):
    if not d["sluzby"]:
        return ""
    radky = []
    for s in SLUZBY:
        if s in d["sluzby"]:
            radky.append(f"- `~/{SLOZKA}/{SLUZBY[s]['nazev']}` — {SLUZBY[s]['nazev']} uživatele "
                         f"({d['sluzby'][s].get('ucet') or 'připojený účet'})")
    return f"""{CLOUD_MARK[0]}
## Cloud uživatele

Uživatel má v prostoru připojený svůj cloud jako obyčejnou složku:

{chr(10).join(radky)}

- Hledej a čti v ní podklady stejně jako jinde (Grep, Read) — je to živý cloud,
  takže první otevření souboru může chvíli trvat. Velké výsledky (videa, exporty,
  archivy) ukládej rovnou sem, ne do domova: disk serveru je malý.
- Soubory a složky, které jsou v domově **odkazem** do `~/{SLOZKA}/…/{VE_CLOUDU}/`,
  jsou přesunuté do cloudu kvůli místu. Otevírají se normálně, nemaž je a neber
  odkaz jako chybu.
{CLOUD_MARK[1]}
"""


def claude_md(user, home=None):
    home = home or workspace.home_for(user)
    text = safefs.read_text(home, ".claude/CLAUDE.md")
    if text is None:
        if safefs.is_file(home, ".claude/CLAUDE.md"):
            return
        text = ""
    block = _blok(user, stav(user), home)
    start, end = text.find(CLOUD_MARK[0]), text.find(CLOUD_MARK[1])
    if start >= 0 and end > start:
        new = text[:start] + block.rstrip("\n") + text[end + len(CLOUD_MARK[1]):]
        if not block:
            new = new.rstrip("\n") + "\n" if new.strip() else ""
    elif block:
        new = (text.rstrip("\n") + "\n\n" if text.strip() else "") + block
    else:
        new = text
    if new != text:
        safefs.makedirs(home, ".claude")
        safefs.write_text(home, ".claude/CLAUDE.md", new)


# ── přehled pro Nastavení → Úložiště ─────────────────────────────────────────

def prehled(user):
    d = stav(user)
    home = workspace.home_for(user)
    sluzby = []
    for s, info in SLUZBY.items():
        x = d["sluzby"].get(s)
        sluzby.append({"id": s, "nazev": info["nazev"], "pripojeno": bool(x),
                       "ucet": (x or {}).get("ucet", ""),
                       "slozka": f"~/{SLOZKA}/{info['nazev']}",
                       "bezi": bool(x) and je_pripojeno(mountpoint(user, s, home)),
                       "lze": s != "onedrive" or onedrive_nastaven()})
    google = [e for e, _ in _google_ucty(home)]
    return {"sluzby": sluzby, "google_ucty": google, "auto": bool(d.get("auto")),
            "prvni": d.get("prvni") or 0, "presunuto": sorted(d["presunuto"], key=lambda z: -z.get("kdy", 0)),
            "posledni_auto": d.get("posledni_auto"), "beh": beh(user),
            "pravidla": {"mb": AUTO_MB, "dni": AUTO_DNI, "strop_gb": AUTO_STROP_GB},
            "rclone": bool(shutil.which(RCLONE) or os.path.exists(RCLONE))}


def nastavit_auto(user, zapnout):
    d = stav(user)
    if zapnout and not d["sluzby"]:
        raise ValueError("Nejdřív si připoj Google Disk nebo OneDrive.")
    if zapnout and not d.get("prvni"):
        raise ValueError("Nejdřív udělej první přesun ručně — pak se dá zapnout automatika.")
    _uprav_stav(user, lambda dd: dd.__setitem__("auto", bool(zapnout)))


# ── příkazová řádka (proces na pozadí) ───────────────────────────────────────

def _user(uid):
    from .accounts import Accounts
    for r in Accounts(config.DB_PATH).list():
        if int(r["id"]) == int(uid):
            return dict(r)
    raise SystemExit(f"účet {uid} neexistuje")


def main(argv=None):
    """python3 -m gateway.cloud presun|vratit --uid N --cesta REL [--sluzba S] | auto"""
    import argparse
    p = argparse.ArgumentParser(prog="gateway.cloud")
    p.add_argument("akce", choices=("presun", "vratit", "auto"))
    p.add_argument("--uid", type=int)
    p.add_argument("--cesta", default="")
    p.add_argument("--sluzba", default="")
    a = p.parse_args(argv)
    log = lambda text: print(time.strftime("%H:%M:%S"), text, flush=True)   # noqa: E731
    if a.akce == "auto":
        from .accounts import Accounts
        automaticky([dict(r) for r in Accounts(config.DB_PATH).list()], log=log)
        return 0
    user = _user(a.uid)
    zacatek = int(time.time())
    zamek = open(os.path.join(_dir(user), "beh.lock"), "w")
    try:
        fcntl.flock(zamek, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log("už běží jiný přesun — končím")
        return 0
    try:
        if a.akce == "presun":
            z = presunout(user, a.cesta, a.sluzba, log=log)
            _zapis_beh(user, {"bezi": False, "akce": a.akce, "cesta": a.cesta, "od": zacatek,
                              "konec": int(time.time()), "bajtu": z["bajtu"]})
        else:
            vratit(user, a.cesta, log=log)
            _zapis_beh(user, {"bezi": False, "akce": a.akce, "cesta": a.cesta, "od": zacatek,
                              "konec": int(time.time())})
    except Exception as exc:                  # noqa: BLE001 — chyba musí do stavu, ne jen do logu
        log(f"chyba: {exc}")
        _zapis_beh(user, {"bezi": False, "akce": a.akce, "cesta": a.cesta, "od": zacatek,
                          "konec": int(time.time()), "chyba": str(exc)})
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
