"""
Prohlížeč pro Claude Code, který je vidět přímo v appce.

Playwright MCP si normálně pouští vlastní (neviditelný) Chromium. Tady běží
jeden sdílený Chromium s ladicím portem na 127.0.0.1 a Playwright MCP se k němu
připojí (`--cdp-endpoint`, viz tools/playwright_bridge.py). Hub pak z téhož
prohlížeče přes CDP posílá obraz do plovoucího okna v appce (static/prohlizec.js)
a posílá zpátky klikání a psaní — člověk tak může něco dokončit sám (přihlášení,
captcha) a Claude pokračuje v té samé stránce.

Modul nemá žádné závislosti mimo standardní knihovnu a nic z hubu neimportuje,
aby ho uměl spustit i samotný most (tools/playwright_bridge.py).

Bezpečnost: ladicí port poslouchá jen na loopbacku a nemá přihlášení. Na
počítači s jedním uživatelem to stačí; na sdíleném serveru ho vidí všechny
prostory, proto je tam okno vypnuté, dokud správce nenastaví HUB_PROHLIZEC=1
(např. po oddělení sítě prostorů).
"""
import base64
import glob
import hashlib
import json
import os
import re
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request

CESTY = lambda: {}         # nastaví hub: id tabu → složka (osiřelé karty se přiřadí podle ní)
CHATY = lambda: {}         # nastaví hub: id tabu → id konverzace (přednost před složkou)
INSTANCE = ""              # nastaví hub (core.INSTANCE) — podle ní pozná své taby v HUB_TAB
IS_WINDOWS = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"
HOST = "127.0.0.1"


def claude_dir():
    return os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")


def profile_dir():
    """Stejný profil jako dřív u Playwright MCP — přihlášení se nemění."""
    return os.path.join(claude_dir(), "browser-profile")


def port():
    """Stálý port na uživatele — víc účtů na jednom stroji se nepere."""
    env = os.environ.get("HUB_BROWSER_PORT", "")
    if env.isdigit():
        return int(env)
    if IS_WINDOWS or not hasattr(os, "getuid"):
        return 9333
    return 9300 + os.getuid() % 600


def endpoint():
    return f"http://{HOST}:{port()}"


def povoleno(server=False):
    """Na sdíleném serveru (prostor brány) je okno vypnuté, dokud to správce
    nepovolí: všechny prostory tam běží pod jedním systémovým účtem a sdílejí
    loopback, takže ladicí port jednoho vidí ostatní."""
    if os.environ.get("HUB_PROHLIZEC") == "1":
        return True
    if os.environ.get("HUB_PROHLIZEC") == "0":
        return False
    return not server


def _version_key(path):
    m = re.search(r"chromium-(\d+)", path)
    return int(m.group(1)) if m else 0


def find_chromium():
    """Nejnovější Chromium od Playwrightu, jinak systémový Chrome. '' = nic."""
    cache = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    home = os.path.expanduser("~")
    if IS_WINDOWS:
        roots = [cache or os.path.join(os.environ.get("LOCALAPPDATA", home), "ms-playwright")]
        pats = ["chromium-*/chrome-win*/chrome.exe"]
        system = [r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                  r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
                  r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"]
    elif IS_MAC:
        roots = [cache or os.path.join(home, "Library/Caches/ms-playwright")]
        pats = ["chromium-*/chrome-mac*/Chromium.app/Contents/MacOS/Chromium"]
        system = ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"]
    else:
        roots = [cache or os.path.join(home, ".cache/ms-playwright")]
        pats = ["chromium-*/chrome-linux*/chrome"]
        system = []
    found = []
    for root in roots:
        for pat in pats:
            found += glob.glob(os.path.join(root, pat))
    # Profil odmítne běžet ve starším Chrome, než v jakém byl naposled otevřen.
    found.sort(key=_version_key, reverse=True)
    for path in found:
        if os.access(path, os.X_OK):
            return path
    for path in system:
        if os.path.isfile(path):
            return path
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "microsoft-edge"):
        hit = shutil.which(name)
        if hit:
            return hit
    return ""


def _get(path, timeout=1.5):
    with urllib.request.urlopen(endpoint() + path, timeout=timeout) as res:
        return json.loads(res.read().decode("utf-8", "replace") or "null")


def alive():
    try:
        return bool(_get("/json/version", 0.6))
    except Exception:
        return False


def zavri_kartu(tid):
    if re.match(r"^[A-Za-z0-9-]+$", str(tid)):
        urllib.request.urlopen(endpoint() + "/json/close/" + tid, timeout=2).read()


def nova_karta():
    """Prázdná karta; vrací její id ('' když to nejde)."""
    try:
        with urllib.request.urlopen(urllib.request.Request(endpoint() + "/json/new?about:blank", method="PUT"),
                                    timeout=2) as res:
            return (json.loads(res.read().decode("utf-8", "replace") or "{}") or {}).get("id", "")
    except Exception:
        return ""


_launch_lock = threading.Lock()
# Kolik procesů stránek smí Chromium mít (měkký strop, viz ensure).
RENDERERU = 4
# Po kolika minutách bez použití se prohlížeč zavře (0 = nikdy). Spustí se
# zase sám, jakmile ho Claude (most, hub/prohlizec_proxy.py) nebo okno
# v appce bude chtít; přihlášení zůstávají v profilu.
NECINNOST_MIN = 20
_AKTIVITA_KAZDYCH = 20.0
_aktivita_zapsana = 0.0


def _aktivita_soubor():
    return os.path.join(claude_dir(), "browser-sessions", ".aktivita")


def oznac_aktivitu():
    """Prohlížeč se právě používá. Zapisuje i most v jiném procesu, proto
    soubor (jeho čas změny), a nejvýš jednou za pár vteřin."""
    global _aktivita_zapsana
    now = time.time()
    if now - _aktivita_zapsana < _AKTIVITA_KAZDYCH:
        return
    _aktivita_zapsana = now
    path = _aktivita_soubor()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="ascii"):
            pass
        os.utime(path, None)
    except OSError:
        pass


def posledni_aktivita():
    try:
        return os.path.getmtime(_aktivita_soubor())
    except OSError:
        return 0.0


def necinnost():
    """Vteřiny bez použití, po kterých se prohlížeč zavře; 0 = nezavírat."""
    raw = os.environ.get("HUB_PROHLIZEC_NECINNOST_MIN", "")
    try:
        minut = float(raw) if raw else NECINNOST_MIN
    except ValueError:
        minut = NECINNOST_MIN
    return max(0.0, minut) * 60


COOKIES_DNI = 90
_cookies_lock = threading.Lock()
_PROC = None                 # proces, který jsme spustili (kvůli nouzovému ukončení)


def _cookies_soubor():
    return os.path.join(profile_dir(), "hub-cookies.json")


def _cdp_prikaz(method, params=None, timeout=8):
    """Jeden příkaz na úrovni prohlížeče (ne karty) → result, nebo None."""
    ws = None
    try:
        info = _get("/json/version", 2)
        ws = _WS(info["webSocketDebuggerUrl"])
        ws.sock.settimeout(timeout)
        ws.send(json.dumps({"id": 1, "method": method, "params": params or {}}))
        while True:
            raw = ws.recv()
            if raw is None:
                return None
            msg = json.loads(raw)
            if msg.get("id") == 1:
                return msg.get("result")
    except Exception:
        return None
    finally:
        if ws:
            ws.close()


def uloz_cookies():
    """Cookies z běžícího prohlížeče do profilu (0600). Přihlášení tak přežije
    pád, restart i poškozenou cookie databázi. Bez nich by se po každém
    zavření nečinného prohlížeče odhlašovalo. Cookies bez expirace dostanou
    +90 dní (jako storageState u Playwrightu)."""
    res = _cdp_prikaz("Storage.getCookies")
    cookies = (res or {}).get("cookies")
    if not cookies:
        return False                     # nic nepřepisovat prázdným
    exp = time.time() + COOKIES_DNI * 86400
    for c in cookies:
        if c.get("session") or not c.get("expires") or c.get("expires", 0) < 0:
            c["expires"] = exp
        c.pop("session", None)
        c.pop("size", None)
    path = _cookies_soubor()
    with _cookies_lock:
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + ".tmp"
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(cookies, fh)
            os.replace(tmp, path)
            os.chmod(path, 0o600)
        except OSError:
            return False
    return True


def vrat_cookies():
    """Po startu vrátí uložené cookies, které prohlížeč nemá (nic nepřepisuje)."""
    try:
        with open(_cookies_soubor(), encoding="utf-8") as fh:
            ulozene = json.load(fh)
    except (OSError, ValueError):
        return 0
    if not isinstance(ulozene, list) or not ulozene:
        return 0
    maji = _cdp_prikaz("Storage.getCookies")
    if maji is None:
        return 0
    klic = lambda c: (c.get("name"), c.get("domain"), c.get("path"))
    existuji = {klic(c) for c in maji.get("cookies", [])}
    chybi = [c for c in ulozene if isinstance(c, dict) and klic(c) not in existuji
             and c.get("expires", 0) > time.time()]
    if chybi and _cdp_prikaz("Storage.setCookies", {"cookies": chybi}, 15) is not None:
        return len(chybi)
    return 0


def zavri_prohlizec():
    """Slušně vypne Chromium: uloží cookies, pošle Browser.close a počká, až
    prohlížeč sám skončí a zapíše profil (cookie databáze). Teprve po ~10 s
    ho ukončí signálem. Vrací, jestli už neběží."""
    if not alive():
        return True
    try:
        uloz_cookies()
    except Exception:
        pass
    _cdp_prikaz("Browser.close", timeout=5)
    for _ in range(40):
        if not alive():
            return True
        time.sleep(0.25)
    # Nereaguje: SIGTERM (Chromium se ještě stihne uklidit), SIGKILL až nakonec.
    proc = _PROC
    if proc is not None and proc.poll() is None:
        for sig_name, cekat in (("SIGTERM", 5), ("SIGKILL", 3)):
            try:
                os.killpg(proc.pid, getattr(__import__("signal"), sig_name))
            except Exception:
                pass
            for _ in range(cekat * 4):
                if not alive():
                    return True
                time.sleep(0.25)
    return not alive()


# Karty, na které nikdo dlouho nesáhl (Claude přes most ani člověk v okně),
# se zavřou a jejich adresa se zapamatuje (`odlozene`). Každá karta je
# vlastní proces stránky (80–600 MB) a Claude je po sobě nezavírá.
KARTA_NECINNOST_MIN = 30
MAX_KARET = 8               # víc živých karet → zavřou se nejdéle nepoužité
KARTA_CHRANENA = 120        # karta použitá před méně než tolika vteřinami se nezavírá nikdy
ODLOZENE_MAX = 50
# Spadlý Chromium se pustí znovu, jen když ho někdo nedávno používal, a ne
# donekonečna: nejvýš PADY_MAX× za PADY_OKNO vteřin.
PADY_MAX = 3
PADY_OKNO = 600


def _odlozene_soubor():
    # Mimo browser-sessions/: tam čte mosty *.json a seznam by je zmátl.
    return os.path.join(claude_dir(), "browser-odlozene.json")


def _je_web(p):
    return bool(re.match(r"^https?:", p.get("url", "") or ""))


def odlozene():
    """Zavřené karty (nečinnost, strop, pád prohlížeče) — nejnovější první:
    [{url, title, tab, at, duvod}]. Otevřou se znovu přes `obnov_odlozenou`."""
    try:
        with open(_odlozene_soubor(), encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def _uloz_odlozene(data):
    path = _odlozene_soubor()
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data[:ODLOZENE_MAX], fh, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError:
        pass


def _odloz(karty, duvod):
    now = int(time.time())
    nove = [{"url": k.get("url", ""), "title": k.get("title", ""), "tab": k.get("tab", ""),
             "at": now, "duvod": duvod} for k in karty if _je_web(k)]
    if not nove:
        return
    urls = {k["url"] for k in nove}
    _uloz_odlozene(nove + [k for k in odlozene() if k.get("url") not in urls])


def obnov_odlozenou(url):
    """Otevře zapamatovanou kartu znovu (spustí i prohlížeč). Vrací id karty."""
    if not re.match(r"^https?:", str(url or "")):
        return ""
    ok, _ = ensure()
    if not ok:
        return ""
    try:
        req = urllib.request.Request(endpoint() + "/json/new?" + urllib.parse.quote(url, safe=""),
                                     method="PUT")
        with urllib.request.urlopen(req, timeout=5) as res:
            tid = (json.loads(res.read().decode("utf-8", "replace") or "{}") or {}).get("id", "")
    except Exception:
        return ""
    _uloz_odlozene([k for k in odlozene() if k.get("url") != url])
    return tid


def _aktivita_mostu():
    """targetId → kdy ho naposled použil Claude (zápisy mostů, prohlizec_proxy)."""
    try:
        from . import prohlizec_proxy as px
    except ImportError:
        try:
            import prohlizec_proxy as px          # spuštěno mimo balíček
        except ImportError:
            return {}
    try:
        return px.pouziti_karet()
    except Exception:
        return {}


class _Karty:
    """Co hlídač o kartách ví mezi dvěma koly."""

    def __init__(self):
        self.pouziti = {}       # id karty → kdy se naposled něco dělo (adresa, titulek, okno, most)
        self.otisk = {}         # id karty → (url, title) z minula
        self.posledni = []      # poslední seznam karet (zapamatuje se po pádu)

    def projdi(self, strany, now):
        most = _aktivita_mostu()
        cil = OKNO._target if OKNO.pouziva_se() else ""
        for p in strany:
            tid = p["id"]
            otisk = (p.get("url", ""), p.get("title", ""))
            if tid not in self.pouziti or self.otisk.get(tid) != otisk or tid == cil:
                self.pouziti[tid] = now
            self.pouziti[tid] = max(self.pouziti[tid], most.get(tid, 0))
            self.otisk[tid] = otisk
        zive = {p["id"] for p in strany}
        for tid in list(self.pouziti):
            if tid not in zive:
                self.pouziti.pop(tid, None)
                self.otisk.pop(tid, None)
        self.posledni = [{"url": p.get("url", ""), "title": p.get("title", ""),
                          "tab": OKNO._majitel(p["id"])} for p in strany]

    def k_zavreni(self, strany, now):
        """Karty k zavření: dlouho nepoužité, a nad MAX_KARET ty nejstarší."""
        limit = KARTA_NECINNOST_MIN * 60
        cil = OKNO._target if OKNO.viewers else ""
        kandidati = []
        for p in strany:
            tid = p["id"]
            if tid == cil or tid in OKNO._potreby or not _je_web(p):
                continue      # na tu se kouká / člověk se v ní přihlašuje / prázdná nic nestojí
            kdy = self.pouziti.get(tid, now)
            if now - kdy >= KARTA_CHRANENA:
                kandidati.append((kdy, p))
        kandidati.sort(key=lambda x: x[0])
        zavrit = [p for kdy, p in kandidati if limit and now - kdy >= limit]
        navic = sum(1 for p in strany if _je_web(p)) - len(zavrit) - MAX_KARET
        for _, p in kandidati:
            if navic <= 0:
                break
            if p not in zavrit:
                zavrit.append(p)
                navic -= 1
        return zavrit


def hlidej_necinnost(log=lambda *a: None, every=15):
    """Smyčka na pozadí v hubu, která drží vestavěný prohlížeč při zemi:

      - karty, na které nikdo KARTA_NECINNOST_MIN minut nesáhl, a nejstarší
        karty nad MAX_KARET zavře a jejich adresy si zapamatuje (`odlozene`),
      - prohlížeč, který nikdo nepoužívá (Claude přes most ani člověk
        v okně appky), po NECINNOST_MIN minutách zavře celý — spustí se sám,
        až ho bude někdo chtít (most při prvním příkazu, okno při otevření),
      - spadlý prohlížeč, který se nedávno používal, pustí znovu (nejvýš
        PADY_MAX× za PADY_OKNO) a jeho karty zapamatuje.
    """
    start = time.time()
    karty = _Karty()
    bezel = False
    zavrel_jsem = False
    pady = []
    kolo = 0
    cookies_at = 0.0
    while True:
        time.sleep(every)
        kolo += 1
        try:
            now = time.time()
            zije = alive()
            if OKNO.pouziva_se():
                OKNO.posledni = now
            last = max(start, posledni_aktivita(), OKNO.posledni,
                       max(karty.pouziti.values(), default=0))
            if bezel and not zije and not zavrel_jsem:
                _odloz(karty.posledni, "pad")
                karty = _Karty()
                pady = [t for t in pady if now - t < PADY_OKNO] + [now]
                if now - last < (necinnost() or 3600) and len(pady) <= PADY_MAX:
                    ok, why = ensure()
                    log("prohlížeč: spadl, spuštěný znovu (adresy karet zapamatované)" if ok
                        else f"prohlížeč: spadl a znovu nenaběhl: {why}")
                    zije = ok
                else:
                    log("prohlížeč: spadl (znovu se spustí, až bude potřeba)")
            if zije:
                zavrel_jsem = False
            bezel = zije
            if zije and now - last < 1800 and now - cookies_at >= 300:
                cookies_at = now          # přihlášení nesmí záviset na tom, že se prohlížeč zavře slušně
                uloz_cookies()
            if not zije or kolo % max(1, int(60 // every)):
                continue                  # karty a nečinnost stačí jednou za minutu
            try:
                data = _get("/json/list", 2)
            except Exception:
                continue
            strany = [p for p in data if p.get("type") == "page"]
            karty.projdi(strany, now)
            zavrit = karty.k_zavreni(strany, now)
            if zavrit and len(zavrit) >= len(strany):
                nova_karta()              # poslední karta nesmí zmizet — prohlížeč by skončil
            for p in zavrit:
                try:
                    zavri_kartu(p["id"])
                except Exception:
                    pass
            if zavrit:
                _odloz([{"url": p.get("url", ""), "title": p.get("title", ""),
                         "tab": OKNO._majitel(p["id"])} for p in zavrit], "necinnost")
                log(f"prohlížeč: zavřeno {len(zavrit)} nepoužívaných karet (adresy zapamatované)")
            limit = necinnost()
            if not limit or OKNO.pouziva_se() or now - last < limit:
                continue
            zavrel_jsem = True
            if zavri_prohlizec():
                bezel = False
                karty = _Karty()
                log(f"prohlížeč: zavřený po {int(limit // 60)} min bez použití (spustí se sám, až bude potřeba)")
            else:
                zavrel_jsem = False
        except Exception as exc:          # noqa: BLE001 — pozadí, nic nesmí shodit
            log(f"prohlížeč: hlídání: {exc}")


def _verze_chromia(exe):
    """Hlavní verze Chromia (154) z `chrome --version`; '' když nejde zjistit."""
    try:
        out = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=8,
                             creationflags=0x08000000 if IS_WINDOWS else 0).stdout
        v = re.search(r"(\d+)\.\d+\.\d+", out or "")
        if v:
            return v.group(1)
    except Exception:
        pass
    return ""


def user_agent(exe):
    """Obyčejný User-Agent desktopového Chromu. Bezhlavý Chromium se hlásí
    jako „HeadlessChrome" — a podle toho ho weby (GitHub, Google…) zablokují
    jako robota („Přístup je dočasně omezen")."""
    ver = _verze_chromia(exe) or "140"
    if IS_WINDOWS:
        os_part = "Windows NT 10.0; Win64; x64"
    elif IS_MAC:
        os_part = "Macintosh; Intel Mac OS X 10_15_7"
    else:
        os_part = "X11; Linux x86_64"
    return (f"Mozilla/5.0 ({os_part}) AppleWebKit/537.36 (KHTML, like Gecko) "
            f"Chrome/{ver}.0.0.0 Safari/537.36")


# Nad tímhle podílem využité paměti se nový Chromium nespouští — jeden by
# dorazil systém (nebo strop prostoru) a jádro by zabilo něco důležitějšího.
PAMET_STROP = 0.85


def vytizeni_pameti():
    """Podíl využité paměti 0–1 (větší z celého systému a stropu prostoru,
    bez cache souborů); 0.0 = nevíme (Windows, bez /proc)."""
    podil = 0.0
    try:
        total = avail = 0
        with open("/proc/meminfo", encoding="ascii") as fh:
            for line in fh:
                if line.startswith("MemTotal:"):
                    total = int(line.split()[1])
                elif line.startswith("MemAvailable:"):
                    avail = int(line.split()[1])
        if total:
            podil = 1.0 - avail / total
    except (OSError, ValueError, IndexError):
        pass
    try:
        from . import core
        st = core.pamet_prostoru()
        if st:
            cur, mx, _ = st
            anon = core.pamet_anon()
            if mx:
                podil = max(podil, (anon if anon else cur) / mx)
    except Exception:
        pass
    return podil


def ensure(wait=15.0):
    """Zajistí běžící prohlížeč. Vrací (ok, zpráva)."""
    with _launch_lock:
        if alive():
            return True, ""
        podil = vytizeni_pameti()
        if podil > PAMET_STROP:
            return False, (f"Prohlížeč teď nespouštím — paměť je zaplněná na {int(podil * 100)} %. "
                           "Zavři nepoužívané taby (nebo je nech uspat) a zkus to znovu.")
        exe = find_chromium()
        if not exe:
            return False, "Nenašel jsem Chromium (spusť instalačku nebo: npx @playwright/mcp install-browser chrome-for-testing)."
        os.makedirs(profile_dir(), exist_ok=True)
        # Bez --remote-allow-origins: webová stránka (ta pošle Origin vždycky)
        # se k ladicímu portu nepřipojí; Playwright ani hub Origin neposílají.
        args = [exe, f"--remote-debugging-port={port()}", f"--remote-debugging-address={HOST}",
                f"--user-data-dir={profile_dir()}",
                "--no-first-run", "--no-default-browser-check", "--headless=new",
                # Ať se nepozná, že je bezhlavý: běžný User-Agent, obrazovka
                # větší než okno (bez toho hlásí 800×600) a bez příznaku automatu.
                f"--user-agent={user_agent(exe)}", "--screen-info={1920x1080}",
                "--disable-blink-features=AutomationControlled",
                "--window-size=1600,1000", "--disable-background-timer-throttling",
                "--disable-backgrounding-occluded-windows", "--disable-renderer-backgrounding",
                # BackForwardCache: každá karta by v paměti držela i stránky,
                # ze kterých se odešlo (zpět se stejně kliká málokdy).
                "--disable-features=Translate,MediaRouter,OptimizationHints,BackForwardCache,"
                "AutofillServerCommunication,InterestFeedContentSuggestions",
                # Víc karet sdílí procesy stránek — každý je 80–600 MB. Izolace
                # webů (site isolation) zůstává, strop je jen měkký.
                f"--renderer-process-limit={RENDERERU}",
                # Cookies nešifrovat klíčem z keyringu: při jeho změně (nová
                # session, jiný stroj) by byly nečitelné = odhlášení všude.
                "--password-store=basic",
                "--disable-background-networking", "--disable-breakpad", "--disable-sync",
                "--js-flags=--max-old-space-size=512",
                "--disable-extensions",
                "--disable-component-update", "--disable-default-apps", "--force-color-profile=srgb", "about:blank"]
        # Malé /dev/shm (kontejnery, sandbox prostoru): sdílená paměť pro
        # snímky by došla a stránky by padaly — pak radši /tmp.
        try:
            st = os.statvfs("/dev/shm")
            if st.f_blocks * st.f_frsize < 512 * 2**20:
                args.insert(1, "--disable-dev-shm-usage")
        except (OSError, AttributeError):
            if not IS_WINDOWS:
                args.insert(1, "--disable-dev-shm-usage")
        # Pod rootem Chromium sandbox odmítne; v prostoru brány už běží
        # v bwrap a vlastní sandbox (další jmenné prostory) tam nenaběhne.
        if not IS_WINDOWS and hasattr(os, "geteuid") and (os.geteuid() == 0 or na_serveru()):
            args.insert(1, "--no-sandbox")
        kw = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
        if IS_WINDOWS:
            kw["creationflags"] = 0x00000008 | 0x00000200        # DETACHED | NEW_GROUP
        else:
            kw["start_new_session"] = True
        try:
            proc = subprocess.Popen(args, **kw)
            global _PROC
            _PROC = proc
        except OSError as exc:
            return False, f"Prohlížeč se nepodařilo spustit: {exc}"
        end = time.time() + wait
        while time.time() < end:
            if alive():
                try:
                    vrat_cookies()
                except Exception:
                    pass
                return True, ""
            if proc.poll() is not None:
                # Souběh: prohlížeč chtěly naráz dva mosty (spouští se až při
                # prvním použití) — druhý Chromium nad zamčeným profilem hned
                # skončí, ale ten první za chvíli odpoví.
                for _ in range(20):
                    if alive():
                        return True, ""
                    time.sleep(0.25)
                return False, "Prohlížeč hned skončil — nad profilem nejspíš běží jiný Chrome."
            time.sleep(0.25)
        return False, "Prohlížeč se nerozjel včas."


# ── Minimální websocket klient (jen standardní knihovna) ─────────────────────

SROVNAT_OKNO = False         # appka nemění velikost skutečného okna Chromia
MIN_SIRKA = 1024             # užší panel nedostane mobilní web — stránka se jen zmenší

class _WS:
    def __init__(self, url):
        m = re.match(r"ws://([^/:]+):(\d+)(/.*)$", url)
        if not m:
            raise ValueError("divná adresa " + url)
        self.sock = socket.create_connection((m.group(1), int(m.group(2))), timeout=5)
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall((f"GET {m.group(3)} HTTP/1.1\r\nHost: {m.group(1)}:{m.group(2)}\r\n"
                           f"Upgrade: websocket\r\nConnection: Upgrade\r\n"
                           f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = self.sock.recv(1024)
            if not chunk:
                raise OSError("spojení zavřeno při handshaku")
            head += chunk
        if b" 101 " not in head.split(b"\r\n", 1)[0]:
            raise OSError("handshake odmítnut")
        self._buf = head.split(b"\r\n\r\n", 1)[1]
        self.sock.settimeout(None)
        if sys.platform.startswith("linux"):
            # Zápis do zaseknutého Chromia nesmí viset navěky (čtení dál čeká
            # bez limitu — rozbité čtení uprostřed rámce by rozhodilo spojení).
            try:
                self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDTIMEO, struct.pack("ll", 5, 0))
            except OSError:
                pass
        self._lock = threading.Lock()
        self.closed = False

    def _read(self, n):
        while len(self._buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise OSError("spojení zavřeno")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def recv(self):
        """Jedna celá textová zpráva (nebo None, když spojení skončilo)."""
        data, opcode0 = b"", None
        while True:
            b1, b2 = self._read(2)
            fin, opcode = b1 & 0x80, b1 & 0x0F
            n = b2 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._read(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._read(8))[0]
            payload = self._read(n)
            if opcode == 0x8:
                return None
            if opcode == 0x9:
                self._send(payload, 0xA)
                continue
            if opcode == 0xA:
                continue
            if opcode != 0x0:
                opcode0, data = opcode, payload
            else:
                data += payload
            if fin:
                return data.decode("utf-8", "replace") if opcode0 == 0x1 else ""

    def _send(self, payload, opcode=0x1):
        head = bytearray([0x80 | opcode])
        n = len(payload)
        if n < 126:
            head.append(0x80 | n)
        elif n < 65536:
            head += bytes([0x80 | 126]) + struct.pack(">H", n)
        else:
            head += bytes([0x80 | 127]) + struct.pack(">Q", n)
        mask = os.urandom(4)
        body = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        with self._lock:
            self.sock.sendall(bytes(head) + mask + body)

    def send(self, text):
        self._send(text.encode("utf-8"))

    def close(self):
        self.closed = True
        try:
            self.sock.shutdown(2)
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass


# ── Okno v appce: obraz ven, vstup dovnitř ───────────────────────────────────

# Co na stránce potřebuje člověka: captcha hned, přihlášení / kód až když na
# stránce chvíli vydrží (Claude heslo může vyplnit sám a pole pak zmizí).
POTREBA_JS = r"""(() => {
  const vis = (e) => { const r = e.getBoundingClientRect(); if (r.width < 3 || r.height < 3) return false;
    const s = getComputedStyle(e); return s.visibility !== 'hidden' && s.display !== 'none' && +s.opacity > 0.05; };
  const je = (sel) => { try { return [...document.querySelectorAll(sel)].some(vis); } catch (_) { return false; } };
  if (je('iframe[src*="recaptcha"]:not([src*="size=invisible"]), iframe[src*="hcaptcha.com"], ' +
         'iframe[src*="challenges.cloudflare.com"], iframe[title*="captcha" i], ' +
         'iframe[src*="captcha-delivery.com"], iframe[src*="arkoselabs.com"], iframe[src*="funcaptcha"], ' +
         '#px-captcha, iframe[src*="geetest"], iframe[src*="perimeterx"]')) return 'captcha';
  if (/^(Just a moment|Chvíli strpení|Attention Required)/i.test(document.title) &&
      document.querySelector('#challenge-form, #cf-challenge-running, .cf-turnstile, #challenge-stage')) return 'captcha';
  if (je('input[autocomplete="one-time-code"]')) return 'kod';
  if (je('input[type="password"]')) return 'heslo';
  return '';
})()"""
POTREBA_VYDRZ = {"captcha": 0, "kod": 2, "heslo": 2}     # kolik kontrol po sobě (à ~2 s)


def _vyhodnot(tid, js, timeout=1.5):
    """Runtime.evaluate na kartě přes krátké vlastní spojení; None = nejde."""
    try:
        cdp = _WS(f"ws://{HOST}:{port()}/devtools/page/{tid}")
    except Exception:
        return None
    try:
        cdp.sock.settimeout(timeout)
        cdp.send(json.dumps({"id": 1, "method": "Runtime.evaluate",
                             "params": {"expression": js, "returnByValue": True, "timeout": 800}}))
        while True:
            raw = cdp.recv()
            if raw is None:
                return None
            d = json.loads(raw or "{}")
            if d.get("id") == 1:
                return ((d.get("result") or {}).get("result") or {}).get("value")
    except Exception:
        return None
    finally:
        cdp.close()


KLAVESY = {"Enter": 13, "Backspace": 8, "Tab": 9, "Escape": 27, "Delete": 46,
           "ArrowLeft": 37, "ArrowUp": 38, "ArrowRight": 39, "ArrowDown": 40,
           "Home": 36, "End": 35, "PageUp": 33, "PageDown": 34, "Insert": 45}


# ── Plynulý obraz: snímky bez fronty ─────────────────────────────────────────
# Snímek jde do okna binárně (JPEG bez base64 a JSONu) a Chromu se potvrdí až
# ve chvíli, kdy ho okno vykreslilo (`ack`). Na cestě je tak nejvýš HLOUBKA
# snímků navíc: pomalé spojení nebo zaneprázdněná stránka dostane méně snímků,
# ale vždycky ten nejnovější — nic se nehromadí a obraz nezaostává za stránkou.
# Schované okno nepotvrzuje, takže Chrome přestane snímat úplně sám.
KVALITA = 85                 # JPEG v klidu (text ostrý)
KVALITA_ROLOVANI = 50        # při rolování kolečkem — poloviční data, plynulejší pohyb
HLOUBKA = 1                  # kolik snímků smí být navíc na cestě do okna
DRZET_NEJDEL = 2.0           # s — nepotvrzený snímek se pak Chromu potvrdí stejně
ODPOVED_NEJDEL = 10.0        # s — CDP dotaz bez odpovědi se zahodí (cb dostane None)


class _Posilac:
    """Snímky jednomu spojení hubu na vlastním vlákně. Drží se jen poslední —
    starší se zahodí, takže pomalé spojení (telefon, brána) nezdrží Chrome
    ani ostatní okna a v paměti nic nebobtná."""

    def __init__(self, conn, zahozeno):
        self.conn = conn
        self._zahozeno = zahozeno
        self._lock = threading.Lock()
        self._data = None
        self._ev = threading.Event()
        self.zije = True
        threading.Thread(target=self._bez, name="prohlizec-obraz", daemon=True).start()

    def dej(self, data):
        with self._lock:
            stary, self._data = self._data, data
        if stary is not None:
            self._zahozeno()             # okno ho nikdy neuvidí — počítá se jako vyřízený
        self._ev.set()

    def stop(self):
        self.zije = False
        self._ev.set()

    def _bez(self):
        while True:
            self._ev.wait()
            self._ev.clear()
            if not self.zije or getattr(self.conn, "closed", False):
                return
            with self._lock:
                data, self._data = self._data, None
            if data is None:
                continue
            try:
                self.conn.send(data, 0x2)
            except Exception:
                self.zije = False
                return


class Okno:
    """Jedno vlákno na prohlížeč, kolik je pohledů. `viewers` = spojení hubu."""

    def __init__(self):
        self.viewers = set()
        self._lock = threading.Lock()
        self._thread = None
        self._cdp = None
        self._next_id = 0
        self._target = ""
        self._info = None
        self._frames = 0
        self._cekajici = []        # příkazy poslané dřív, než se prohlížeč připojil
        self._videne = None        # id stránek, které už jsme viděli (nová = Claude ji otevřel)
        self._poradi = {}          # id stránky → pořadí, v jakém se objevila (karty zleva doprava)
        self._odpovedi = {}        # id požadavku → funkce, která dostane odpověď
        self._nav = {"back": False, "fwd": False}   # lze jít zpět / vpřed
        self._nacita = False       # stránka se načítá (kolečko na kartě, stop místo reload)
        self._velikost = None      # (šířka, výška, devicePixelRatio) plochy okna v appce
        self._fit_cekajici = None
        self._vidi = {}            # spojení → True, když okno je rozbalené (jinak se obraz neposílá)
        self._sleduj = None        # sid tabu, jehož karty okno ukazuje (None = všechny)
        self._rucni = {}           # id karty → sid tabu, ke kterému si ji člověk otevřel sám
        self._vlastnici = {}       # id karty → sid tabu (z mostů, hub/prohlizec_proxy.py)
        self._vlastnici_cas = 0.0
        self._potreby = {}         # id karty → důvod ('heslo' | 'kod' | 'captcha')
        self._potreby_pocet = {}   # id karty → (důvod, kolik kontrol po sobě)
        self._bez_majitele = {}    # id nové karty → kdy se objevila (most ji ještě nezapsal)
        self._hlidac = None
        self._insp_cdp = None      # spojení, na kterém je zapnutá doména Inspector
        self._pady_karty = []      # kdy se stránka naposled zhroutila (omezuje opakované načítání)
        self.posledni = 0.0        # kdy naposled člověk v okně něco dělal (hlidej_necinnost)
        self._init_obraz()

    def pouziva_se(self):
        """Kouká se někdo na rozbalené okno prohlížeče?"""
        return any(self._vidi.values())

    # -- pohledy
    def pridej(self, conn, pasivne=False):
        # Pasivní pohled (appka jen zjišťuje, jestli má chat karty): prohlížeč se
        # kvůli tomu nespouští — když neběží, není co ukázat.
        if pasivne and not alive():
            try:
                conn.send_json({"t": "br-nic"})
            except Exception:
                pass
            return
        self.posledni = time.time()
        with self._lock:
            self.viewers.add(conn)
            start = not (self._thread and self._thread.is_alive())
            if not start and not pasivne and not alive():
                # Prohlížeč se mezitím zavřel kvůli nečinnosti — okno ho chce zpátky.
                threading.Thread(target=ensure, daemon=True).start()
            if start:
                self._thread = threading.Thread(target=self._bez, name="prohlizec", daemon=True)
                self._thread.start()
            if not (self._hlidac and self._hlidac.is_alive()):
                self._hlidac = threading.Thread(target=self._hlidej_potreby, name="prohlizec-potreby", daemon=True)
                self._hlidac.start()
        self._info = None          # nový pohled chce hned znát stav

    def odeber(self, conn):
        with self._lock:
            self.viewers.discard(conn)
            had = any(self._vidi.values())
            self._vidi.pop(conn, None)
        self._pustit_posilace(conn)
        if had and not any(self._vidi.values()):
            self._posli("Page.stopScreencast")
            self._uvolni_snimky()

    def vidi(self, conn, on, pauza=False):
        """Rozbalené okno chce obraz, minimalizované jen stav karet.
        `pauza` = okno je jen schované (jiný tab prohlížeče, jiný chat):
        obraz se zastaví, ale stránka si nechá velikost okna — po návratu
        se nic nepřeskládá."""
        with self._lock:
            pred = any(self._vidi.values())
            self._vidi[conn] = bool(on)
            ted = any(self._vidi.values())
        if ted and not pred:
            self._emuluj()
            self._spust_obraz()
        elif pred and not ted:
            self._posli("Page.stopScreencast")
            self._uvolni_snimky()
            if not pauza:
                # Minimalizované: stránka se vrátí do svého okna, Claude ji vidí normálně.
                self._posli("Emulation.clearDeviceMetricsOverride")

    # -- komu která karta patří
    def _majitel(self, tid):
        return self._vlastnici.get(tid) or self._rucni.get(tid) or ""

    def _nacti_vlastniky(self):
        if time.time() - self._vlastnici_cas < 0.9:
            return
        self._vlastnici_cas = time.time()
        try:
            from . import prohlizec_proxy as px
        except ImportError:
            try:
                import prohlizec_proxy as px          # spuštěno mimo balíček
            except ImportError:
                return
        pre = INSTANCE + ":" if INSTANCE else None
        out = {}
        for tid, znacka in px.vlastnici().items():
            if pre and znacka.startswith(pre):
                out[tid] = znacka[len(pre):]
            elif znacka:
                out[tid] = "?"                         # jiná instance hubu / terminál
        # Osiřelé karty (most po restartu ještě nenaběhl) → tab, který pokračuje
        # v téže konverzaci; zápis bez id konverzace → tab ve stejné složce,
        # jen když je takový jediný. Id tabu jako text — stejně jako z HUB_TAB.
        try:
            cesty = CESTY() or {}
            chaty = CHATY() or {}
            for cwd, pages, chat in px.sirotci():
                if chat:
                    shoda = [sid for sid, c in chaty.items() if c == chat]
                else:
                    shoda = [sid for sid, c in cesty.items() if os.path.abspath(c or "") == cwd]
                if len(shoda) == 1:
                    for tid in pages:
                        out.setdefault(tid, str(shoda[0]))
        except Exception:
            pass
        if out != self._vlastnici:
            self._vlastnici = out
            self._info = None

    def _hlidej_potreby(self):
        """Každé ~2 s se podívá na karty: přihlášení, kód nebo captcha?"""
        while self.viewers:
            try:
                strany = self._strany()
                ids = set()
                for p in strany[:12]:
                    tid = p["id"]
                    ids.add(tid)
                    url = p.get("url", "")
                    duvod = "" if not re.match(r"^https?:", url) else (_vyhodnot(tid, POTREBA_JS) or "")
                    pred, n = self._potreby_pocet.get(tid, ("", 0))
                    n = n + 1 if duvod == pred else 1
                    self._potreby_pocet[tid] = (duvod, n)
                    hotovo = duvod and n > POTREBA_VYDRZ.get(duvod, 2)
                    if hotovo and self._potreby.get(tid) != duvod:
                        self._potreby[tid] = duvod
                        self._info = None
                    elif not duvod and tid in self._potreby:
                        self._potreby.pop(tid, None)
                        self._info = None
                for tid in list(self._potreby_pocet):
                    if tid not in ids:
                        self._potreby_pocet.pop(tid, None)
                        if self._potreby.pop(tid, None):
                            self._info = None
                for tid in list(self._rucni):
                    if tid not in ids:
                        self._rucni.pop(tid, None)
            except Exception:
                pass
            time.sleep(2.0)

    def _vsem(self, obj):
        for conn in list(self.viewers):
            try:
                conn.send_json(obj)
            except Exception:
                self.viewers.discard(conn)

    # -- CDP
    def _posli(self, method, **params):
        cdp = self._cdp
        if not cdp or cdp.closed:
            return
        self._next_id += 1
        try:
            cdp.send(json.dumps({"id": self._next_id, "method": method, "params": params}))
        except OSError:
            pass

    def _dotaz(self, method, cb=None, **params):
        """Požadavek, jehož odpověď chceme (cb dostane `result` nebo None)."""
        cdp = self._cdp
        if not cdp or cdp.closed:
            return
        self._next_id += 1
        rid = self._next_id
        if cb:
            self._odpovedi[rid] = (cb, time.time())
        try:
            cdp.send(json.dumps({"id": rid, "method": method, "params": params}))
        except OSError:
            self._odpovedi.pop(rid, None)

    def _obnov_historii(self):
        def hotovo(res):
            if not res:
                return
            i = res.get("currentIndex", 0)
            n = len(res.get("entries") or [])
            nav = {"back": i > 0, "fwd": i < n - 1}
            if nav != self._nav:
                self._nav = nav
                self._info = None          # překreslí se s novým stavem šipek
        self._dotaz("Page.getNavigationHistory", hotovo)

    def _udalost(self, msg):
        """Události stránky: načítání, historie, dialogy JavaScriptu."""
        m = msg.get("method")
        par = msg.get("params") or {}
        # Pád stránky (renderer) hlásí až zapnutá doména Inspector; zapíná se tady,
        # u první události nového spojení, ať se nesahá do _stream.
        if self._cdp is not None and self._insp_cdp is not self._cdp:
            self._insp_cdp = self._cdp
            self._posli("Inspector.enable")
        if m == "Inspector.targetCrashed":
            now = time.time()
            self._pady_karty = [t for t in self._pady_karty if now - t < PADY_OKNO] + [now]
            if len(self._pady_karty) <= PADY_MAX:
                # Tentýž profil, tatáž karta: stačí ji načíst znovu.
                self._posli("Page.reload")
            return
        if m == "Page.frameStartedLoading":
            self._nacita = True
            self._info = None
        elif m == "Page.frameStoppedLoading":
            self._nacita = False
            self._info = None
            self._obnov_historii()
        elif m in ("Page.frameNavigated", "Page.navigatedWithinDocument"):
            self._obnov_historii()
            ram = par.get("frame") or {}
            if m == "Page.frameNavigated" and not ram.get("parentId") and ram.get("url"):
                self._url_cile = ram["url"]          # kam se vrátit, kdyby stránka spadla
        elif m == "Page.javascriptDialogOpening":
            self._vsem({"t": "br-dialog", "typ": par.get("type", "alert"), "zprava": par.get("message", ""),
                        "vychozi": par.get("defaultPrompt", ""), "url": par.get("url", "")})
        elif m == "Page.javascriptDialogClosed":
            self._vsem({"t": "br-dialog", "zavrit": True})
        elif m == "Inspector.targetCrashed":
            self._spadla()
        elif m == "Inspector.detached":
            cdp = self._cdp
            if cdp:
                cdp.close()        # karta zmizela / převzal ji někdo jiný — smyčka se připojí znovu

    def _strany(self):
        try:
            data = _get("/json/list", 1.5)
        except Exception:
            return []
        strany = [p for p in data if p.get("type") == "page" and p.get("webSocketDebuggerUrl")]
        self._nacti_vlastniky()
        # Chromium je vrací podle poslední aktivity — v okně mají karty zůstat
        # tam, kde vznikly, nejstarší vlevo, ať se při přepínání neskáčou.
        for p in strany:
            self._poradi.setdefault(p["id"], len(self._poradi))
        strany.sort(key=lambda p: self._poradi[p["id"]])
        return strany

    def _bez(self):
        ok, zprava = ensure()
        if not ok:
            self._vsem({"t": "br-stav", "ok": False, "zprava": zprava})
            return
        self._vsem({"t": "br-stav", "ok": True})
        while self.viewers:
            strany = self._strany()
            if not strany:
                self._posli_info([], None)
                time.sleep(0.8)
                continue
            nova = self._nova(strany)
            cil = next((p for p in strany if p["id"] == self._target), None)
            if nova:
                cil = nova                     # Claude otevřel novou kartu — okno jde za ní
            elif cil is None:
                moje = [p for p in strany if self._sleduj is None or self._majitel(p["id"]) == self._sleduj]
                cil = (moje or strany)[-1]
            self._target = cil["id"]
            self._posli_info(strany, cil)
            try:
                self._stream(cil)
            except Exception:
                pass
            time.sleep(0.4)
        self._cdp = None

    def _posli_info(self, strany, aktivni):
        info = {"t": "br-info", "active": aktivni["id"] if aktivni else "",
                "url": aktivni.get("url", "") if aktivni else "",
                "title": aktivni.get("title", "") if aktivni else "",
                "back": self._nav["back"], "fwd": self._nav["fwd"], "loading": self._nacita,
                "pages": [{"id": p["id"], "title": p.get("title", ""), "url": p.get("url", ""),
                           "icon": p.get("faviconUrl", ""), "tab": self._majitel(p["id"]),
                           "potreba": self._potreby.get(p["id"], "")} for p in strany]}
        sig = json.dumps(info, sort_keys=True)
        if sig != self._info:
            self._info = sig
            self._vsem(info)

    def _stream(self, cil):
        cdp = _WS(cil["webSocketDebuggerUrl"])
        self._cdp = cdp
        self._url_cile = cil.get("url", "")
        self._uvolni_snimky()
        self._posli("Page.enable")
        self._posli("Inspector.enable")        # pád stránky (Inspector.targetCrashed)
        self._nacita = False
        self._nav = {"back": False, "fwd": False}
        if any(self._vidi.values()):
            self._emuluj()
            self._spust_obraz()
        self._obnov_historii()
        # Co člověk napsal, než se spojení rozjelo (adresa hned po otevření okna).
        cekajici, self._cekajici = self._cekajici, []
        for method, params in cekajici:
            self._posli(method, **params)
        stop = threading.Event()

        def hlidej():
            # Mění se stránka / adresa / aktivní záložka? Pak se přepne obraz.
            while not stop.wait(1.0):
                if not self.viewers:
                    break
                strany = self._strany()
                akt = next((p for p in strany if p["id"] == self._target), None)
                if akt is None or self._nova(strany, vzit=False):
                    break
                self._posli_info(strany, akt)
                self._hlidej_casy()
            stop.set()
            cdp.close()
        threading.Thread(target=hlidej, daemon=True).start()
        try:
            while not stop.is_set() and self.viewers:
                raw = cdp.recv()
                if raw is None:
                    break
                try:
                    msg = json.loads(raw)
                except ValueError:
                    continue
                if msg.get("id") in self._odpovedi:
                    self._odpovedi.pop(msg["id"])[0](msg.get("result"))
                elif msg.get("method") == "Page.screencastFrame":
                    self._snimek(msg["params"])
                elif msg.get("method"):
                    self._udalost(msg)
        finally:
            stop.set()
            cdp.close()

    # -- velikost: stránka se kreslí přesně v rozlišení plochy okna v appce
    def _emuluj(self):
        """Ostrý obraz: stránka dostane viewport o velikosti plochy okna
        a hustotu pixelů displeje (HiDPI), takže snímek jde na plátno 1:1 —
        dřív se kreslila v pevném okně 1600×1000 a Chrome ji zmenšoval, což
        rozmazalo text. Užší plocha než MIN_SIRKA dostane desktopový web
        vykreslený rovnou zmenšený (menší písmo, ale pořád ostré, ne
        rozmazané zmenšení bitmapy). Platí jen na spojení hubu: zavřením
        okna (nebo přepnutím karty) se emulace sama zruší."""
        if not self._velikost:
            return
        css_w, css_h, dpr = self._viewport()
        mobil = self._mobil
        self._posli("Emulation.setDeviceMetricsOverride", width=css_w, height=css_h,
                    deviceScaleFactor=dpr, mobile=mobil,
                    screenWidth=css_w if mobil else max(css_w, 1920),
                    screenHeight=css_h if mobil else max(css_h, 1080))

    def _viewport(self):
        w, h, dpr = self._velikost or (1600, 1000, 1.0)
        # Živý obraz je beztak v CSS pixelech (viz _spust_obraz), takže vyšší
        # hustota by jen zčtyřnásobila kreslení v bezhlavém Chromu (software).
        dpr = 1.0
        if self._mobil:
            return w, h, dpr                  # telefon (poměr 9:16): přesně plocha okna
        css_w = max(w, MIN_SIRKA)
        return css_w, max(200, round(h * css_w / w)), dpr

    def _spust_obraz(self):
        # Bezhlavý Chrome posílá živý obraz v CSS pixelech stránky (hustotu
        # displeje ignoruje, naměřeno). Užší okno než stránka (MIN_SIRKA)
        # dostane snímek zmenšený už v Chromu — menší data, plátno kreslí 1:1.
        css_w, css_h, _ = self._viewport()
        w, h, _ = self._velikost or (css_w, css_h, 1.0)
        self._uvolni_snimky()
        self._posli("Page.stopScreencast")
        self._posli("Page.startScreencast", format="jpeg",
                    quality=KVALITA_ROLOVANI if self._rychle else KVALITA,
                    maxWidth=min(css_w, w), maxHeight=min(css_h, h),
                    everyNthFrame=2 if self._rychle else 1)

    # -- plynulý obraz (viz KVALITA, _Posilac)
    def _init_obraz(self):
        self._mobil = False            # okno v poměru 9:16 — stránka jako na telefonu
        self._rychle = False           # právě se roluje → nižší kvalita snímků
        self._rolovani_do = 0.0
        self._posilace = {}            # spojení → _Posilac
        self._snimky_lock = threading.Lock()
        self._v_letu = 0               # snímky poslané do okna, které ještě nepotvrdilo
        self._drzene = []              # [(sessionId, kdy)] — Chromu zatím nepotvrzené
        self._url_cile = ""
        self._pady = []                # kdy stránka spadla (proti nekonečnému obnovování)

    def _posilac(self, conn):
        pos = self._posilace.get(conn)
        if pos is None or not pos.zije:
            pos = self._posilace[conn] = _Posilac(conn, self._potvrzeno)
        return pos

    def _pustit_posilace(self, conn):
        pos = self._posilace.pop(conn, None)
        if pos:
            pos.stop()

    def _uvolni_snimky(self):
        """Nové snímání (jiná karta, velikost, kvalita): staré potvrzování neplatí."""
        with self._snimky_lock:
            self._v_letu = 0
            self._drzene = []

    def _snimek(self, p):
        meta = p.get("metadata") or {}
        w, h = meta.get("deviceWidth", 0), meta.get("deviceHeight", 0)
        with self._lock:
            divaci = [c for c in self.viewers if self._vidi.get(c)]
        nove = [c for c in divaci if getattr(c, "br_bin", False)]
        if nove:
            try:
                jpeg = base64.b64decode(p.get("data") or "")
            except (ValueError, TypeError):
                jpeg = b""
            hlava = json.dumps({"w": w, "h": h}).encode()
            data = b"BR" + struct.pack(">H", len(hlava)) + hlava + jpeg
            for conn in nove:
                self._posilac(conn).dej(data)
        for conn in divaci:
            if conn not in nove:           # okno ze staré verze stránky (do reloadu)
                try:
                    conn.send_json({"t": "br-frame", "d": p.get("data", ""), "w": w, "h": h})
                except Exception:
                    pass
        sid = p.get("sessionId")
        with self._snimky_lock:
            if nove:
                self._v_letu += 1
                if self._v_letu > HLOUBKA:
                    self._drzene.append((sid, time.time()))
                    return
        self._posli("Page.screencastFrameAck", sessionId=sid)

    def _potvrzeno(self):
        """Okno snímek vykreslilo (nebo ho zahodilo) — Chrome smí poslat další."""
        with self._snimky_lock:
            self._v_letu = max(0, self._v_letu - 1)
            sid = self._drzene.pop(0)[0] if self._drzene else None
        if sid is not None:
            self._posli("Page.screencastFrameAck", sessionId=sid)

    def _hlidej_casy(self):
        """Nic nesmí viset: nepotvrzený snímek ani dotaz bez odpovědi."""
        ted = time.time()
        with self._snimky_lock:
            stare = [sid for sid, kdy in self._drzene if ted - kdy > DRZET_NEJDEL]
            self._drzene = [(sid, kdy) for sid, kdy in self._drzene if ted - kdy <= DRZET_NEJDEL]
            if stare:
                self._v_letu = len(self._drzene)
        for sid in stare:
            self._posli("Page.screencastFrameAck", sessionId=sid)
        for rid, (cb, kdy) in list(self._odpovedi.items()):
            if ted - kdy > ODPOVED_NEJDEL and self._odpovedi.pop(rid, None):
                try:
                    cb(None)
                except Exception:
                    pass

    def _rolovani(self):
        """Kolečko: snímky v nižší kvalitě, po doběhnutí zase ostré."""
        self._rolovani_do = time.time() + 0.45
        if self._rychle or not any(self._vidi.values()):
            return
        self._rychle = True
        self._spust_obraz()

        def po():
            while time.time() < self._rolovani_do:
                time.sleep(0.1)
            self._rychle = False
            if any(self._vidi.values()):
                self._spust_obraz()        # restart pošle hned jeden ostrý snímek
        threading.Thread(target=po, name="prohlizec-rolovani", daemon=True).start()

    def _spadla(self):
        """Proces stránky spadl (Inspector.targetCrashed): stránka se načte
        znovu sama, okno mezitím řekne, co se děje. Když padá pořád, nechá
        se to na člověku."""
        ted = time.time()
        self._pady = [t for t in self._pady if ted - t < 60] + [ted]
        if len(self._pady) > 3:
            self._vsem({"t": "br-stav", "ok": False, "pad": True,
                        "zprava": "Stránka opakovaně padá — zkus ji načíst znovu (Ctrl+R)."})
            return
        self._vsem({"t": "br-stav", "ok": False, "pad": True, "zprava": "Stránka spadla — obnovuji…"})
        url = self._url_cile

        def obnov():
            time.sleep(0.6)
            if re.match(r"^https?:", url or ""):
                self._posli("Page.navigate", url=url)
            else:
                self._posli("Page.reload")
            time.sleep(1.0)
            if any(self._vidi.values()):
                self._emuluj()
                self._spust_obraz()
        threading.Thread(target=obnov, name="prohlizec-obnova", daemon=True).start()

    def _srovnej_okno(self, w, h):
        """Skutečné okno Chromia dostane rozměr plochy, ať stránka vidí tu
        velikost, kterou člověk vidí (responzivní web, nic neuříznutého, 1:1
        bez zmenšování). Výška okna obsahuje i lištu prohlížeče, kterou
        screencast nevidí, tak se její tloušťka změří a odečte. Běží
        na vlastním spojení — Claude přitom nic nevidí a nic se nepřeruší.

        VYPNUTO: skutečné okno Chromia se nechává na pokoji (člověk ho vidí
        a nechce, aby mu appka měnila rozměry). Obraz v appce se přizpůsobí
        velikosti okna Chromia — plátno zachovává poměr stran a myš se
        přepočítává podle rozměrů snímku."""
        if not SROVNAT_OKNO:
            return
        cil = self._target
        if not cil:
            return

        def prace():
            try:
                cdp = _WS(f"ws://{HOST}:{port()}/devtools/page/{cil}")
            except Exception:
                return
            try:
                seq = [0]

                def zavolej(method, **params):
                    seq[0] += 1
                    cdp.send(json.dumps({"id": seq[0], "method": method, "params": params}))
                    while True:
                        raw = cdp.recv()
                        if raw is None:
                            return None
                        d = json.loads(raw)
                        if d.get("id") == seq[0]:
                            return d.get("result")
                win = zavolej("Browser.getWindowForTarget")
                if not win:
                    return
                wid = win["windowId"]
                zavolej("Browser.setWindowBounds", windowId=wid, bounds={"windowState": "normal"})
                zavolej("Browser.setWindowBounds", windowId=wid, bounds={"width": w + 0, "height": h + 140})
                vnitrek = zavolej("Runtime.evaluate", expression="[innerWidth,innerHeight].join(',')")
                try:
                    iw, ih = [int(x) for x in vnitrek["result"]["value"].split(",")]
                except Exception:
                    return
                if (iw, ih) != (w, h):
                    # Okno = obsah + rámeček; dorovnat o naměřený rozdíl.
                    zavolej("Browser.setWindowBounds", windowId=wid,
                            bounds={"width": max(200, w + (w - iw) + 0), "height": h + 140 + (h - ih)})
            except Exception:
                pass
            finally:
                cdp.close()
        threading.Thread(target=prace, daemon=True).start()

    def _nova(self, strany, vzit=True):
        """Stránka, která se objevila od posledního pohledu (`vzit` ji označí
        za viděnou). Při prvním pohledu se jen zapamatují ty, které už jsou."""
        ids = [p["id"] for p in strany]
        if self._videne is None:
            self._videne = set(ids)
            return None
        nove = [p for p in strany if p["id"] not in self._videne]
        if self._sleduj is not None:
            # Karta jiného chatu okno nepřepne — jen se objeví u svého tabu.
            # Most zapisuje vlastníka až chvilku po vzniku karty: bez majitele
            # se na ni ~3 s počká, jinak by se Claudova nová karta brala jako
            # cizí a okno by za ní nikdy nešlo.
            ted = time.time()
            for tid in [t for t in self._bez_majitele if t not in ids]:
                self._bez_majitele.pop(tid, None)      # karta mezitím zavřená
            for p in nove:
                if self._majitel(p["id"]):
                    self._bez_majitele.pop(p["id"], None)
                else:
                    self._bez_majitele.setdefault(p["id"], ted)
            cekaji = [p for p in nove if not self._majitel(p["id"])
                      and ted - self._bez_majitele.get(p["id"], ted) < 3.0]
            cizi = [p for p in nove if p not in cekaji and self._majitel(p["id"]) != self._sleduj]
            if cizi and vzit:
                self._videne |= {p["id"] for p in cizi}
            nove = [p for p in nove if p not in cizi and p not in cekaji]
        if nove and vzit:
            # Viděné jsou všechny kromě těch, na jejichž majitele se ještě čeká.
            cekaji_ids = {tid for tid in self._bez_majitele if not self._majitel(tid)}
            self._videne |= set(ids) - cekaji_ids
            self._target = nove[0]["id"]
        return nove[0] if nove else None

    # -- vstup od člověka
    def vstup(self, msg):
        self.posledni = time.time()
        a = msg.get("a")
        if a == "mouse":
            typ = {"down": "mousePressed", "up": "mouseReleased", "move": "mouseMoved"}.get(msg.get("type"))
            if typ:
                btn = {0: "left", 1: "middle", 2: "right"}.get(msg.get("button", 0), "left")
                self._posli("Input.dispatchMouseEvent", type=typ, x=float(msg.get("x", 0)), y=float(msg.get("y", 0)),
                            button=btn if typ != "mouseMoved" else "none", clickCount=int(msg.get("clicks", 1)),
                            modifiers=int(msg.get("mod", 0)))
        elif a == "wheel":
            self._rolovani()
            self._posli("Input.dispatchMouseEvent", type="mouseWheel", x=float(msg.get("x", 0)), y=float(msg.get("y", 0)),
                        deltaX=float(msg.get("dx", 0)), deltaY=float(msg.get("dy", 0)))
        elif a == "ack":
            self._potvrzeno()
        elif a == "key":
            key = str(msg.get("key", ""))
            vk = KLAVESY.get(key) or int(msg.get("vk") or 0)
            mod = int(msg.get("mod", 0))
            text = key if len(key) == 1 and not (mod & 0b1011) else ("\r" if key == "Enter" else "")
            if msg.get("type") == "down":
                self._posli("Input.dispatchKeyEvent", type="keyDown" if text else "rawKeyDown", key=key,
                            code=str(msg.get("code", "")), windowsVirtualKeyCode=vk, nativeVirtualKeyCode=vk,
                            modifiers=mod, **({"text": text} if text else {}))
            else:
                self._posli("Input.dispatchKeyEvent", type="keyUp", key=key, code=str(msg.get("code", "")),
                            windowsVirtualKeyCode=vk, nativeVirtualKeyCode=vk, modifiers=mod)
        elif a == "text":
            text = str(msg.get("text", ""))[:20000]
            if text:
                self._posli("Input.insertText", text=text)
        elif a == "size":
            try:
                w = max(320, min(int(msg.get("w", 0)), 4000))
                h = max(200, min(int(msg.get("h", 0)), 3000))
                dpr = max(1.0, min(float(msg.get("dpr") or 1), 3.0))
            except (TypeError, ValueError):
                return
            mobil = bool(msg.get("mobil"))
            if (w, h, dpr) != self._velikost or mobil != self._mobil:
                self._velikost = (w, h, dpr)
                self._mobil = mobil
                if any(self._vidi.values()):
                    self._emuluj()
                    self._spust_obraz()
        elif a == "stop":
            self._posli("Page.stopLoading")
        elif a == "closetab":
            tid = str(msg.get("id", ""))
            if re.match(r"^[A-Za-z0-9-]+$", tid):
                try:
                    if len(self._strany()) <= 1:       # poslední karta se nezavírá, jen vyprázdní
                        nova_karta()
                    zavri_kartu(tid)
                except Exception:
                    pass
                self._info = None
        elif a == "copy":
            def vybrano(res):
                text = ((res or {}).get("result") or {}).get("value") or ""
                self._vsem({"t": "br-copy", "text": str(text)[:200000]})
            self._dotaz("Runtime.evaluate", vybrano, expression="String(getSelection())")
        elif a == "dialog":
            self._posli("Page.handleJavaScriptDialog", accept=bool(msg.get("accept")),
                        promptText=str(msg.get("text", ""))[:10000])
        elif a == "go":
            url = str(msg.get("url", "")).strip()
            if url and not re.match(r"^(https?|about|chrome):", url, re.I):
                # „abc.cz/x" je adresa, „jak se peče chleba" hledání (jako v Chromu).
                hostitel = url.split("/")[0]
                if re.match(r"^(localhost|\d{1,3}(\.\d{1,3}){3})(:\d+)?$", hostitel, re.I):
                    url = "http://" + url
                elif " " in url or "." not in hostitel:
                    url = "https://www.google.com/search?q=" + urllib.parse.quote(url)
                else:
                    url = "https://" + url
            if url and re.match(r"^(https?|about|chrome):", url, re.I):
                if self._cdp is None or self._cdp.closed:
                    self._cekajici = [("Page.navigate", {"url": url})]   # jen poslední adresa
                else:
                    self._posli("Page.navigate", url=url)
        elif a in ("back", "forward"):
            self._posli("Runtime.evaluate", expression="history.%s()" % ("back" if a == "back" else "forward"))
        elif a == "reload":
            self._posli("Page.reload")
        elif a == "tab":
            tid = str(msg.get("id", ""))
            if re.match(r"^[A-Za-z0-9-]+$", tid):
                try:
                    urllib.request.urlopen(endpoint() + "/json/activate/" + tid, timeout=2).read()
                except Exception:
                    pass
                self._target = tid
                if self._videne is not None:
                    self._videne.add(tid)
                cdp = self._cdp
                if cdp:
                    cdp.close()      # smyčka se znovu připojí na novou záložku
        elif a == "newtab":
            tid = nova_karta()
            if tid:
                if self._sleduj:
                    self._rucni[tid] = self._sleduj    # patří k chatu, který je v okně vybraný
                # Okno hned na novou kartu (jako Ctrl+T v Chromu) — dřív ji smyčka
                # mohla vzít za cizí a okno zůstalo na staré.
                self._target = tid
                if self._videne is not None:
                    self._videne.add(tid)
                cdp = self._cdp
                if cdp:
                    cdp.close()
                self._info = None
        elif a == "sleduj":
            tab = msg.get("tab")
            self._sleduj = str(tab) if tab else None
            self._info = None


OKNO = Okno()


def zprava(conn, msg, server=False):
    """Volá hub pro každou websocketovou zprávu typu `br`."""
    if not povoleno(server):
        conn.send_json({"t": "br-stav", "ok": False,
                        "zprava": "Prohlížeč v appce je na tomhle serveru zatím vypnutý."})
        return
    a = msg.get("a")
    if a == "open":
        # Nové okno umí binární snímky a potvrzuje vykreslení (plynulý obraz).
        conn.br_bin = bool(msg.get("bin"))
        OKNO.pridej(conn, bool(msg.get("pasivne")))
    elif a == "close":
        OKNO.odeber(conn)
    elif a == "vidi":
        OKNO.vidi(conn, msg.get("on"), bool(msg.get("pauza")))
    else:
        OKNO.vstup(msg)


def odpoj(conn):
    OKNO.odeber(conn)


# ── napojení Playwright MCP na tenhle prohlížeč ──────────────────────────────

def na_serveru():
    """Běží hub jako prostor brány? Brána to zapisuje do hub-config.json."""
    try:
        with open(os.path.join(claude_dir(), "hub-config.json"), encoding="utf-8-sig") as fh:
            return bool(json.load(fh).get("server_mode"))
    except (OSError, ValueError):
        return False


def _zaznam():
    """Záznam `playwright` z ~/.claude.json: dict, nebo None. (Claude Code ho
    drží vedle složky, nebo v ní, když je nastavená CLAUDE_CONFIG_DIR.)"""
    cd = claude_dir().rstrip("/\\")
    for path in (cd + ".json", os.path.join(cd, ".claude.json")):
        try:
            with open(path, encoding="utf-8-sig") as fh:
                servers = json.load(fh).get("mcpServers") or {}
        except (OSError, ValueError):
            continue
        return servers.get("playwright") or None
    return None


def registrace():
    """Jak je v Claude Code zaregistrovaný Playwright MCP: most | jine | zadna."""
    entry = _zaznam()
    if not entry:
        return "zadna"
    text = " ".join([str(entry.get("command", ""))] + [str(a) for a in entry.get("args") or []])
    return "most" if "playwright_bridge" in text else "jine"


def migruj(log=lambda *a: None):
    """Jednou po aktualizaci: Playwright MCP zaregistrovaný po staru (vlastní
    neviditelný prohlížeč) se přepne na most, ať je prohlížeč vidět v appce.
    Co už člověk nastavil jinak (jiný server pod jménem `playwright`), nechá
    být. Běží na pozadí a nikdy nespadne."""
    try:
        if not povoleno(na_serveru()) or registrace() != "jine":
            return
        entry = _zaznam() or {}
        args = " ".join(str(a) for a in entry.get("args") or [])
        if "@playwright/mcp" not in args or "--cdp-endpoint" in args:
            return                           # vlastní nastavení — nesahat
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        bridge = os.path.join(here, "tools", "playwright_bridge.py")
        if not os.path.isfile(bridge) or not shutil.which("claude"):
            return
        res = subprocess.run([sys.executable or "python3", bridge, "--register"],
                             capture_output=True, text=True, timeout=60)
        log("Playwright MCP přepnutý na prohlížeč v appce" if res.returncode == 0
            else "přepnutí Playwright MCP se nepovedlo: " + (res.stderr or res.stdout).strip()[:200])
    except Exception as exc:          # noqa: BLE001 — pozadí, nic nesmí shodit
        log(f"přepnutí Playwright MCP: {exc}")
