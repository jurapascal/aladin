#!/usr/bin/env python3
"""
gemini-pomoc — jedno zadání pro Gemini (Google AI Pro) přes Antigravity CLI (`agy`).

    gemini-pomoc "Přepiš tuhle hlasovku" zprava.ogg
    gemini-pomoc --ukol obrazek "Bílé tričko s logem na ramínku, studiové světlo" logo.png
    gemini-pomoc --argon "Vypiš z faktury položky a ceny jako tabulku" faktura.pdf
    gemini-pomoc --web "Jaké formáty plakátů nabízí konkurence?"
    gemini-pomoc --stav

Proč to vůbec je: Gemini na předplatném Google AI Pro má vlastní týdenní limit,
nezávislý na tom Claudeově, a umí, co Claude neumí — fotorealistické obrázky,
přepis nahrávek a popis videí (srovnání Tomáš, 25. 10. 2026: tričko 50 s proti
5,6 min kreslení přes canvas, Claude měl týden ze 82 % pryč, Gemini skoro nic).

Tenhle soubor je jediné místo, které ví, jak se `agy` volá. Používá ho MCP
server `gemini_mcp.py` (nástroje mcp__gemini__* pro Clauda), hub (stav
v Nastavení → AI agenti) i příkaz `gemini-pomoc` v terminálu — hub na něj dá
odkaz do ~/.local/bin. Jen standardní knihovna.

Jak se volá: `agy -p … --output-format json --sandbox` v pevné pracovní složce
(~/.cache/aladin-gemini). Pevné proto, že `agy` si každou novou složku zapisuje
do trustedWorkspaces ve svém settings.json — s dočasnou složkou na každé volání
by ten seznam rostl donekonečna. Soubory k zadání jdou do podsložky pro jedno
volání (kopie nebo pevný odkaz), takže souběžná volání si nepřekážejí a Gemini
nevidí nic jiného z disku.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid

IS_WINDOWS = os.name == "nt"
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if IS_WINDOWS else 0

HOME = os.path.expanduser("~")
AGY_DIR = os.path.join(HOME, ".gemini", "antigravity-cli")
# Na stroji bez klíčenky (server, prostor) leží přihlášení tady. Na počítači
# s klíčenkou (GNOME Keyring, Keychain, Credential Manager) soubor není —
# proto prihlaseni() umí říct i „nevím".
TOKEN = os.path.join(AGY_DIR, "antigravity-oauth-token")
SETTINGS = os.path.join(AGY_DIR, "settings.json")
BRAIN = os.path.join(AGY_DIR, "brain")
PRACE = os.path.join(HOME, ".cache", "aladin-gemini")
CLAUDE_DIR = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(HOME, ".claude")
STAV = os.path.join(CLAUDE_DIR, "hub-gemini.json")

INSTALL = ('powershell -NoProfile -ExecutionPolicy Bypass -Command '
           '"irm https://antigravity.google/cli/install.ps1 | iex"') if IS_WINDOWS \
    else "curl -fsSL https://antigravity.google/cli/install.sh | bash"

# Kdyby `agy models` nic neřekl. Flash je výchozí model agy, takže u něj se
# radši nepošle nic než staré jméno; Argon se bez jména vybrat nedá.
ZALOZNI_ARGON = "gemini-4-argon-medium"

# Strop na vstupy: Gemini dostává kopii, ať se disk nezaplní a volání nevisí
# na nahrávání gigabajtů. Video do 300 MB je zhruba hodina v rozumné kvalitě.
MAX_SOUBOR = 300 * 1024 * 1024
MAX_CELKEM = 600 * 1024 * 1024
MAX_SOUBORU = 20
MAX_ZADANI = 100_000
OBRAZKY = (".png", ".jpg", ".jpeg", ".webp")

# Jak dlouho smí které volání trvat (s). Obrázek trval v testu 50 s, přepis
# dlouhé nahrávky i video jsou pomalejší.
LIMITY = {"zeptat": 300, "obrazek": 600, "prepis": 600, "video": 900}

NEPRIHLASENY = ("Gemini není přihlášený — Nastavení → AI agenti → Gemini → Přihlásit "
                "(v terminálu stačí spustit `agy` a přihlásit se Google účtem).")
NENAINSTALOVANY = ("Gemini (Antigravity CLI, příkaz `agy`) tu není nainstalovaný — "
                   "Nastavení → AI agenti → Gemini → Nainstalovat, nebo: " + INSTALL)


class Chyba(Exception):
    """Chyba s českou zprávou pro člověka. `druh`: auth | limit | cas | jine."""

    def __init__(self, zprava, druh="jine"):
        super().__init__(zprava)
        self.druh = druh


# ── kde je agy a jestli je přihlášený ───────────────────────────────────────
def najdi_agy():
    """Plná cesta k `agy`, nebo ''. Instalačka ho dává mimo systémové cesty
    (~/.local/bin, na Windows %LOCALAPPDATA%\\agy\\bin) a MCP server ani hub
    nemusí mít PATH z profilu."""
    found = shutil.which("agy")
    if found:
        return found
    cands = [os.path.join(HOME, ".local", "bin", "agy")]
    if IS_WINDOWS:
        local = os.environ.get("LOCALAPPDATA") or os.path.join(HOME, "AppData", "Local")
        cands = [os.path.join(local, "agy", "bin", "agy" + s) for s in (".exe", ".cmd", "")]
    for cand in cands:
        if os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    return ""


def _mtime(path):
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


def nacti_stav():
    try:
        with open(STAV, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _zapis_stav(data):
    try:
        os.makedirs(CLAUDE_DIR, exist_ok=True)
        tmp = f"{STAV}.{os.getpid()}.{threading.get_ident()}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, STAV)
    except OSError:
        pass


_STAV_LOCK = threading.Lock()


def _uprav_stav(fn):
    with _STAV_LOCK:
        data = nacti_stav()
        fn(data)
        _zapis_stav(data)


def prihlaseni():
    """True = přihlášený, False = určitě ne, None = nedá se poznat bez volání.

    Bez volání Gemini (stálo by kus limitu) se to pozná jen nepřímo: soubor
    s přihlášením, nebo jak dopadlo poslední volání přes tenhle nástroj.
    Klíčenka na počítači do souboru nic nepíše — tam je to do prvního volání
    „nevím"."""
    if not os.path.isdir(AGY_DIR):
        return False                  # agy se tu ještě nikdy nespustil
    posledni = nacti_stav().get("posledni") or {}
    tok = _mtime(TOKEN)
    if posledni.get("auth_chyba") and float(posledni.get("t") or 0) >= tok:
        return False                  # odmítl přihlášení až po posledním tokenu
    if tok or posledni.get("ok"):
        return True
    return None


def zrus_chybu_prihlaseni():
    """Člověk se jde přihlásit — stará chyba přihlášení už nemá platit."""
    def fn(data):
        if (data.get("posledni") or {}).get("auth_chyba"):
            data["posledni"]["auth_chyba"] = False
    _uprav_stav(fn)


def tyden():
    """Kolik volání šlo tento týden přes tenhle nástroj (podle modelu)."""
    data = nacti_stav()
    if data.get("tyden") != _tyden_klic():
        return {}
    return dict(data.get("pocty") or {})


def _tyden_klic():
    import datetime
    rok, cislo = datetime.date.today().isocalendar()[:2]
    return f"{rok}-W{cislo:02d}"


def _zapis_volani(ok, auth_chyba=False, chyba="", model=""):
    def fn(data):
        data["posledni"] = {"t": time.time(), "ok": bool(ok), "auth_chyba": bool(auth_chyba),
                            "chyba": str(chyba or "")[:300]}
        if ok:
            klic = _tyden_klic()
            if data.get("tyden") != klic:
                data["tyden"], data["pocty"] = klic, {}
            pocty = data.setdefault("pocty", {})
            jmeno = model or "flash"
            pocty[jmeno] = int(pocty.get(jmeno) or 0) + 1
    _uprav_stav(fn)


# ── modely ───────────────────────────────────────────────────────────────────
_MODEL_RE = re.compile(r"\bgemini-\d[\w.\-]*", re.I)


def modely(agy="", cerstve=False, ttl=86400):
    """Id modelů Gemini z `agy models` (bez Claudeových — ty mají v agy jen
    drobný limit). Výsledek se drží den v hub-gemini.json; `cerstve` = zeptat
    se, i když je seznam prázdný nebo starý. Bez `cerstve` nikdy nic nespouští."""
    data = nacti_stav().get("modely") or {}
    ids = [str(m) for m in (data.get("ids") or []) if isinstance(m, str)]
    if not cerstve or (ids and time.time() - float(data.get("t") or 0) < ttl):
        return ids
    agy = agy or najdi_agy()
    if not agy:
        return ids
    try:
        r = subprocess.run([agy, "models"], capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=25, stdin=subprocess.DEVNULL,
                           cwd=HOME, creationflags=_NO_WINDOW)
        text = (r.stdout or "") + "\n" + (r.stderr or "")
    except (OSError, subprocess.TimeoutExpired):
        return ids
    nove = []
    for m in _MODEL_RE.findall(text):
        m = m.rstrip(".-").lower()
        if m not in nove:
            nove.append(m)
    if nove:
        def fn(stav):
            stav["modely"] = {"t": time.time(), "ids": nove}
        _uprav_stav(fn)
        return nove
    return ids


def _nejlepsi(ids, rodina):
    kand = [m for m in ids if rodina in m]
    # „-medium" je to, s čím jede agy jako výchozí; vyšší verze má přednost.
    kand.sort(key=lambda m: ("medium" in m, _verze(m)), reverse=True)
    return kand[0] if kand else ""


def _verze(model_id):
    m = re.match(r"gemini-(\d+)(?:\.(\d+))?", model_id)
    return (int(m.group(1)), int(m.group(2) or 0)) if m else (0, 0)


def vyber_model(volba, agy=""):
    """(id pro --model nebo '', krátké jméno pro výpis). flash | argon | id."""
    volba = str(volba or "flash").strip().lower()
    if volba in ("", "flash", "rychly", "rychlý"):
        return _nejlepsi(modely(agy), "flash"), "flash"
    if volba in ("argon", "silny", "silný", "pro"):
        return _nejlepsi(modely(agy, cerstve=True), "argon") or ZALOZNI_ARGON, "argon"
    if re.fullmatch(r"[a-z0-9][\w.\-]{2,80}", volba):
        return volba, volba
    raise Chyba(f"Neznámý model „{volba}“ — použij flash, argon nebo id z `agy models`.")


def popis_modelu(model_id):
    """gemini-3.8-flash-medium → Gemini 3.8 Flash · medium"""
    m = re.fullmatch(r"gemini-([\d.]+)-([a-z]+)(?:-(.+))?", model_id or "")
    if not m:
        return model_id
    return f"Gemini {m.group(1)} {m.group(2).capitalize()}" + (f" · {m.group(3)}" if m.group(3) else "")


# ── nastavení agy ────────────────────────────────────────────────────────────
def _priprav_nastaveni():
    """toolPermission: proceed-in-sandbox, když ho člověk nemá nastavený jinak.

    Bez něj se v neinteraktivním běhu nesmí pouštět ani grep — u delšího
    souboru se Gemini zasekne na čtení po kouscích. Soubor si agy píše i sám
    (trustedWorkspaces, motiv), takže se jen doplní chybějící klíč; když
    soubor ještě není (agy se nespustil) nebo se nedá přečíst, nesahá se na něj."""
    try:
        with open(SETTINGS, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return
    if not isinstance(data, dict) or "toolPermission" in data:
        return
    data["toolPermission"] = "proceed-in-sandbox"
    try:
        tmp = SETTINGS + ".aladin.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, SETTINGS)
    except OSError:
        pass


# ── jedno volání ─────────────────────────────────────────────────────────────
_BEZI = set()
_BEZI_LOCK = threading.Lock()


def ukonci_vse():
    """MCP server končí (Claude zavřel tab) — rozběhnuté agy nenechat viset."""
    with _BEZI_LOCK:
        procs = list(_BEZI)
    for p in procs:
        try:
            p.kill()
        except OSError:
            pass


def _bezpecne_jmeno(jmeno):
    base = os.path.basename(str(jmeno)) or "soubor"
    base = re.sub(r"[^\w.\-]+", "_", base, flags=re.UNICODE).strip("._") or "soubor"
    return base[:120]


def _prenes(src, dst):
    """Pevný odkaz (bez kopírování gigabajtů), jinak kopie."""
    try:
        os.link(src, dst)
    except OSError:
        shutil.copy2(src, dst)


def _uklid_stare():
    """Složky po volání, které nedoběhlo (pád, zabitý proces) — po dni pryč."""
    try:
        for name in os.listdir(PRACE):
            path = os.path.join(PRACE, name)
            if os.path.isdir(path) and time.time() - _mtime(path) > 86400:
                shutil.rmtree(path, ignore_errors=True)
    except OSError:
        pass


def _priprav_vstupy(soubory, misto, ident):
    soubory = [str(s) for s in (soubory or []) if str(s).strip()]
    if len(soubory) > MAX_SOUBORU:
        raise Chyba(f"Najednou jde poslat nejvýš {MAX_SOUBORU} souborů.")
    celkem, cesty, jmena = 0, [], set()
    for src in soubory:
        src = os.path.abspath(os.path.expanduser(src))
        if not os.path.isfile(src):
            raise Chyba(f"Soubor {src} neexistuje.")
        velikost = os.path.getsize(src)
        if velikost > MAX_SOUBOR:
            raise Chyba(f"{os.path.basename(src)} má {velikost // (1024 * 1024)} MB — "
                        f"Gemini se posílá nejvýš {MAX_SOUBOR // (1024 * 1024)} MB na soubor.")
        celkem += velikost
        if celkem > MAX_CELKEM:
            raise Chyba(f"Soubory mají dohromady přes {MAX_CELKEM // (1024 * 1024)} MB — pošli je po částech.")
        jmeno = _bezpecne_jmeno(src)
        koren, pripona = os.path.splitext(jmeno)
        n = 2
        while jmeno.lower() in jmena:
            jmeno = f"{koren}-{n}{pripona}"
            n += 1
        jmena.add(jmeno.lower())
        _prenes(src, os.path.join(misto, jmeno))
        cesty.append(f"./{ident}/{jmeno}")
    return cesty


def _json_z(text):
    """Obálka z --output-format json; před ní může být hláška na stdout."""
    text = (text or "").strip()
    if not text:
        return None
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else None
    except ValueError:
        pass
    for line in reversed(text.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                data = json.loads(line)
                if isinstance(data, dict):
                    return data
            except ValueError:
                continue
    a, b = text.find("{"), text.rfind("}")
    if 0 <= a < b:
        try:
            data = json.loads(text[a:b + 1])
            return data if isinstance(data, dict) else None
        except ValueError:
            pass
    return None


_AUTH_RE = re.compile(r"authenticat|not logged|log ?in|sign ?in|unauthori[sz]ed|"
                      r"credential|oauth|token (?:expired|invalid)|\b401\b", re.I)
_LIMIT_RE = re.compile(r"quota|rate.?limit|resource.?exhausted|\b429\b|usage limit|"
                       r"limit (?:reached|exceeded)|out of credits", re.I)
_MODEL_CHYBA_RE = re.compile(r"(unknown|invalid|unsupported) model|model .{0,40}not (found|available)", re.I)


def _preloz_chybu(text, model_kratky):
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    if _AUTH_RE.search(text):
        return Chyba(NEPRIHLASENY, "auth")
    if _LIMIT_RE.search(text):
        rada = (" Zkus to s Flashem (model flash), ten má limit mnohem větší."
                if model_kratky == "argon" else " Udělej to sám, nebo to zkus za pár dní.")
        return Chyba("Týdenní limit Gemini (Google AI Pro) je vyčerpaný." + rada + f" ({text[:200]})", "limit")
    if _MODEL_CHYBA_RE.search(text):
        return Chyba(f"Model {model_kratky} tu agy nezná — seznam ukáže `agy models`. ({text[:200]})")
    return Chyba("Gemini selhal: " + (text[:500] or "bez vysvětlení (agy skončil s chybou)."))


def _nove_obrazky(slozky, od):
    found = []
    for slozka in slozky:
        if not slozka or not os.path.isdir(slozka):
            continue
        for root, _dirs, files in os.walk(slozka):
            for name in sorted(files):
                path = os.path.join(root, name)
                if name.lower().endswith(OBRAZKY) and _mtime(path) >= od - 2 \
                        and os.path.getsize(path) > 0:
                    found.append(path)
    # Stejný obrázek bývá v brain i ve výstupu (Gemini ho tam zkopíroval).
    vysledek, videne = [], set()
    for path in found:
        klic = (os.path.getsize(path), os.path.splitext(path)[1].lower())
        try:
            with open(path, "rb") as fh:
                klic += (hash(fh.read(65536)),)
        except OSError:
            continue
        if klic not in videne:
            videne.add(klic)
            vysledek.append(path)
    return vysledek


def _slug(text):
    import unicodedata
    text = unicodedata.normalize("NFKD", str(text or "")).encode("ascii", "ignore").decode()
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return text[:40].strip("-")


def _uloz_obrazky(obrazky, slozka, nazev):
    if not obrazky:
        return []
    slozka = os.path.abspath(os.path.expanduser(slozka or os.getcwd()))
    try:
        os.makedirs(slozka, exist_ok=True)
    except OSError as exc:
        raise Chyba(f"Do složky {slozka} nejde ukládat: {exc}") from None
    zaklad = (_slug(nazev) or "gemini-obrazek") + "-" + time.strftime("%Y%m%d-%H%M%S")
    ulozene = []
    for i, src in enumerate(obrazky):
        pripona = os.path.splitext(src)[1].lower()
        pripona = ".jpg" if pripona == ".jpeg" else pripona
        cil = os.path.join(slozka, zaklad + (f"-{i + 1}" if len(obrazky) > 1 else "") + pripona)
        n = 2
        while os.path.exists(cil):
            cil = os.path.join(slozka, f"{zaklad}-{i + 1}-{n}{pripona}")
            n += 1
        shutil.copy2(src, cil)
        ulozene.append(cil)
    return ulozene


def spust(prompt, soubory=(), model="flash", ukol="zeptat", slozka=None, nazev="",
          stavet_prompt=None, timeout=None):
    """Zavolá Gemini. Vrací dict: text, obrazky (uložené cesty), model, tokeny,
    sekundy, konverzace. Chyby jako Chyba s českou zprávou.

    `stavet_prompt(cesty, ident)` složí výsledné zadání, když potřebuje znát,
    kde v pracovní složce leží přiložené soubory; jinak se k `prompt` jen
    připíše jejich seznam."""
    agy = najdi_agy()
    if not agy:
        raise Chyba(NENAINSTALOVANY)
    if len(str(prompt or "")) > MAX_ZADANI:
        raise Chyba(f"Zadání je moc dlouhé (přes {MAX_ZADANI} znaků) — dej text do souboru a přilož ho.")
    model_id, model_kratky = vyber_model(model, agy)
    limit = int(timeout or LIMITY.get(ukol) or 300)
    os.makedirs(PRACE, exist_ok=True)
    _uklid_stare()
    _priprav_nastaveni()
    ident = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
    misto = os.path.join(PRACE, ident)
    os.makedirs(os.path.join(misto, "vystup"))
    try:
        cesty = _priprav_vstupy(soubory, misto, ident)
        if stavet_prompt:
            text = stavet_prompt(cesty, ident)
        else:
            text = str(prompt or "").strip()
            if cesty:
                text += "\n\nPřiložené soubory: " + ", ".join(cesty)
            text += (f"\n\nKdyž vytváříš soubory (obrázky, tabulky), ulož je do složky ./{ident}/vystup/. "
                     f"Nic jiného na disku neměň.")
        argv = [agy, "-p", text, "--output-format", "json", "--sandbox",
                "--print-timeout", f"{limit}s"]
        if model_id:
            argv += ["--model", model_id]
        zacatek = time.time()
        try:
            proc = subprocess.Popen(argv, cwd=PRACE, stdin=subprocess.DEVNULL,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    text=True, encoding="utf-8", errors="replace",
                                    creationflags=_NO_WINDOW)
        except OSError as exc:
            raise Chyba(f"agy nejde spustit: {exc}") from None
        with _BEZI_LOCK:
            _BEZI.add(proc)
        try:
            out, err = proc.communicate(timeout=limit + 45)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            raise Chyba(f"Gemini neodpověděl do {limit} s — zkus menší soubor nebo kratší zadání.", "cas") from None
        finally:
            with _BEZI_LOCK:
                _BEZI.discard(proc)
        data = _json_z(out)
        status = str((data or {}).get("status") or "")
        if not data or status != "SUCCESS":
            chyba = (data or {}).get("error")
            if isinstance(chyba, dict):
                chyba = chyba.get("message") or json.dumps(chyba, ensure_ascii=False)
            duvod = " ".join(x for x in (str(chyba or ""), (err or "").strip()[-800:],
                                         "" if data else (out or "").strip()[-300:]) if x)
            if status in ("CANCELED", "INTERRUPTED") and not duvod:
                duvod = "volání bylo přerušeno"
            exc = _preloz_chybu(duvod or f"agy skončil kódem {proc.returncode}", model_kratky)
            _zapis_volani(False, auth_chyba=exc.druh == "auth", chyba=str(exc))
            raise exc
        konverzace = str(data.get("conversation_id") or "")
        kde = [os.path.join(misto, "vystup"), misto]
        if konverzace and re.fullmatch(r"[\w\-]{4,100}", konverzace):
            kde.append(os.path.join(BRAIN, konverzace))
        obrazky = _nove_obrazky(kde, zacatek)
        if not obrazky and ukol == "obrazek" and not konverzace:
            # Bez id konverzace nevíme, ve které složce brain obrázek je —
            # vezme se, co v brain vzniklo během tohohle volání.
            obrazky = _nove_obrazky([BRAIN], zacatek)
        # Přiložené předlohy nejsou výsledek, i když jsou to obrázky.
        vstupy = {os.path.normcase(os.path.join(misto, os.path.basename(c))) for c in cesty}
        obrazky = [o for o in obrazky if os.path.normcase(o) not in vstupy]
        ulozene = _uloz_obrazky(obrazky, slozka, nazev or ukol)
        usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        _zapis_volani(True, model=model_kratky)
        return {"text": str(data.get("response") or "").strip(), "obrazky": ulozene,
                "model": popis_modelu(model_id) if model_id else model_kratky.capitalize(),
                "model_kratky": model_kratky,
                "tokeny": int(usage.get("total_tokens") or 0),
                "sekundy": round(float(data.get("duration_seconds") or (time.time() - zacatek)), 1),
                "konverzace": konverzace}
    finally:
        shutil.rmtree(misto, ignore_errors=True)


# ── hotová zadání ────────────────────────────────────────────────────────────
def obrazek(popis, predlohy=(), slozka=None, nazev="", model="flash"):
    def skladej(cesty, ident):
        text = ("Vygeneruj fotorealistický obrázek podle tohoto zadání:\n\n"
                f"{str(popis).strip()}\n")
        if cesty:
            text += ("\nJako předlohu použij přiložené obrázky (logo, produkt, styl) — "
                     "drž se jejich podoby, barev a textů: " + ", ".join(cesty) + "\n")
        text += (f"\nHotový obrázek ulož jako PNG nebo JPG do složky ./{ident}/vystup/. "
                 "Nic jiného na disku neměň. Na konec napiš jednou větou, co na obrázku je.")
        return text
    vysledek = spust("", predlohy, model=model, ukol="obrazek", slozka=slozka,
                     nazev=nazev or popis, stavet_prompt=skladej)
    if not vysledek["obrazky"]:
        raise Chyba("Gemini odpověděl, ale žádný obrázek nevznikl"
                    + (f": {vysledek['text'][:400]}" if vysledek["text"] else ".")
                    + " Zkus zadání přeformulovat (bez jmen skutečných lidí a cizích značek).")
    return vysledek


def prepis(soubor, jazyk="", casy=False, model="flash"):
    def skladej(cesty, _ident):
        text = (f"Přepiš doslova, co zaznívá v nahrávce {cesty[0]}\n"
                "- Piš v jazyce nahrávky, nic nepřekládej ani nezkracuj.\n"
                "- Když mluví víc lidí, označ je (Mluvčí 1:, Mluvčí 2:…).\n")
        if casy:
            text += "- Před každý úsek dej časovou značku [mm:ss].\n"
        if jazyk:
            text += f"- Nahrávka je nejspíš v jazyce: {jazyk}.\n"
        text += ("Vrať jen samotný přepis, bez úvodu a komentářů. Když v nahrávce není "
                 "řeč nebo jí nejde rozumět, napiš to jednou větou.")
        return text
    return spust("", [soubor], model=model, ukol="prepis", stavet_prompt=skladej)


def video(soubor, otazka="", model="flash"):
    def skladej(cesty, _ident):
        text = (f"Podívej se na video {cesty[0]} a popiš ho:\n"
                "1. Co je ve videu vidět — scény, lidé, předměty, texty a nápisy na obrazovce, "
                "s časovými značkami [mm:ss].\n"
                "2. Doslovný přepis toho, co se ve videu říká, s časovými značkami "
                "(v původním jazyce).\n")
        if otazka:
            text += f"\nZaměř se hlavně na tohle: {str(otazka).strip()}\n"
        text += "\nPopis piš česky, přepis řeči nech v původním jazyce."
        return text
    return spust("", [soubor], model=model, ukol="video", stavet_prompt=skladej)


def zeptat(zadani, soubory=(), model="flash", web=False, slozka=None):
    def skladej(cesty, ident):
        text = str(zadani or "").strip()
        if cesty:
            text += "\n\nPřiložené soubory: " + ", ".join(cesty)
        # Bez výslovného svolení Gemini na web nesáhne a odpoví „NEVÍM".
        text += ("\n\nSmíš hledat na webu a otevírat stránky; u faktů z webu uveď adresu zdroje."
                 if web else "\n\nNehledej na webu, vystač si se zadáním a přílohami.")
        text += (f"\nKdyž vytváříš soubory (obrázky, tabulky), ulož je do složky ./{ident}/vystup/. "
                 "Nic jiného na disku neměň.")
        return text
    return spust("", soubory, model=model, ukol="zeptat", slozka=slozka,
                 nazev="gemini", stavet_prompt=skladej)


def paticka(v):
    """(Gemini 3.8 Flash · 16 s · 1 234 tokenů)"""
    casti = [v.get("model") or "Gemini", f"{v.get('sekundy', 0):g} s"]
    if v.get("tokeny"):
        casti.append(f"{v['tokeny']:,} tokenů".replace(",", " "))
    return "(" + " · ".join(casti) + ")"


# ── příkaz gemini-pomoc ──────────────────────────────────────────────────────
def _stav_text():
    agy = najdi_agy()
    if not agy:
        return NENAINSTALOVANY, 1
    prihl = prihlaseni()
    radky = [f"agy: {agy}",
             "přihlášení: " + {True: "ano", False: "ne — spusť `agy` a přihlas se Google účtem",
                               None: "nejspíš ano (přihlášení je v klíčence, ověří ho první volání)"}[prihl]]
    pocty = tyden()
    if pocty:
        radky.append("tento týden přes Aladina: " + ", ".join(f"{k} {v}×" for k, v in sorted(pocty.items())))
    ids = modely(agy)
    if ids:
        radky.append("modely: " + ", ".join(ids))
    return "\n".join(radky), (1 if prihl is False else 0)


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(
        prog="gemini-pomoc",
        description="Jedno zadání pro Gemini (Google AI Pro) přes Antigravity CLI. "
                    "Obrázky, přepis nahrávek, popis videí a hromadná práce — na vlastním "
                    "týdenním limitu Googlu, ne Claudeově.")
    ap.add_argument("zadani", nargs="?", help="co má Gemini udělat (u --ukol obrazek popis obrázku)")
    ap.add_argument("soubory", nargs="*", help="přílohy: fotky, PDF, nahrávky, video…")
    ap.add_argument("--ukol", choices=("zeptat", "obrazek", "prepis", "video"), default="zeptat",
                    help="obrazek = vygenerovat obrázek, prepis = přepsat nahrávku, video = popsat video")
    ap.add_argument("-m", "--model", default="flash",
                    help="flash (výchozí, rychlý, velký limit) | argon (silnější, ~300/týden) | id")
    ap.add_argument("--argon", action="store_true", help="zkratka pro --model argon")
    ap.add_argument("--web", action="store_true", help="Gemini smí hledat na webu")
    ap.add_argument("--casy", action="store_true", help="u přepisu časové značky [mm:ss]")
    ap.add_argument("-o", "--do", dest="slozka", default=None,
                    help="kam uložit vytvořené obrázky (výchozí: aktuální složka)")
    ap.add_argument("--json", action="store_true", help="výsledek jako JSON")
    ap.add_argument("--stav", action="store_true", help="jen vypsat, jestli je Gemini připravený")
    a = ap.parse_args(argv)
    if a.stav:
        text, kod = _stav_text()
        print(text)
        return kod
    if not a.zadani:
        ap.print_usage(sys.stderr)
        return 2
    model = "argon" if a.argon else a.model
    try:
        if a.ukol == "obrazek":
            v = obrazek(a.zadani, a.soubory, slozka=a.slozka, model=model)
        elif a.ukol in ("prepis", "video"):
            # U přepisu a videa je soubor to hlavní: `gemini-pomoc --ukol prepis zprava.ogg`.
            soubory = a.soubory or [a.zadani]
            pozn = a.zadani if a.soubory else ""
            if a.ukol == "prepis":
                v = prepis(soubory[0], jazyk=pozn, casy=a.casy, model=model)
            else:
                v = video(soubory[0], otazka=pozn, model=model)
        else:
            v = zeptat(a.zadani, a.soubory, model=model, web=a.web, slozka=a.slozka)
    except Chyba as exc:
        if a.json:
            print(json.dumps({"ok": False, "error": str(exc), "druh": exc.druh}, ensure_ascii=False))
        else:
            print(str(exc), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        ukonci_vse()
        return 130
    if a.json:
        print(json.dumps({"ok": True, **v}, ensure_ascii=False, indent=1))
        return 0
    if v["text"]:
        print(v["text"])
    if v["obrazky"]:
        print("\nUloženo:" if v["text"] else "Uloženo:")
        for path in v["obrazky"]:
            print("  " + path)
    print(paticka(v), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
