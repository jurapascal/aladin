"""
Spotřeba Claude Code po lidech — pro admina (Statistiky → Tým) a příkaz
`claude-hub-admin spotreba`.

Čísla jsou z přepisů sezení, které si Claude Code každému ukládá do domova
(`~/.claude/projects/*/*.jsonl`) — stejný zdroj jako Statistiky v hubu
(hub/stats.py). Je to odhad podle ceníku API, ne faktura: na předplatném se
po tokenech neplatí, ale dá se podle něj srovnat, kdo kolik čerpá ze
společného limitu.

Počítá se přírůstkově a na pozadí: přepis, který se od minula nezměnil, se
nečte znovu, a první průchod (stovky MB) neblokuje odpověď — stránka dostane
„počítám“ a za chvíli čísla. Čte se jen to, co se za poslední týden změnilo.

**Spotřeba z počítače.** Kdo je v appce na počítači přihlášený k serveru,
tomu hub posílá souhrn svých přepisů za poslední týden (hub/spotreba_pc.py,
`POST /gw/spotreba/pocitac`, token zařízení) — po sezeních a dnech. Do
týdne a limitů se počítá jen počítač, který jede na stejném předplatném jako
prostor (`_stejne`); ostatní je vidět zvlášť. Chat přenesený mezi počítačem
a serverem (hub/prenos.py) má na obou stranách stejné id: za každé sezení
a den se bere větší ze dvou čísel, takže se nepočítá dvakrát. Pokračoval-li
někdo v tomtéž chatu na obou stranách zároveň, je to o kus míň — odhad.

**Limity** (každý zvlášť, libovolná kombinace, týden = posledních 7 dní):

* `usd` — odhad v USD (původní strop; starý zápis je jen číslo = tohle),
* `tokeny` — napsané tokeny (výstup) za týden,
* `pct` — kolik procent týdenního limitu předplatného smí člověk vyčerpat.
  Kdo má v prostoru předplatné (připojené z appky, nebo přihlášené přímo
  v prostoru), má skutečná procenta od Anthropicu (hub/usage.py). Sdílí-li
  jedno přihlášení víc lidí, dělí se procenta podle jejich podílu na ceně.
  Na klíči API procenta nejsou — počítá se podíl na spotřebě celého týmu.

**Stopka.** Limit může mít zapnutou stopku: po překročení brána nepustí do
Clauda v prostoru další zprávy (hub/stopka.py, hook tools/stopka_hook.py),
dokud limit sám nepovolí (týden se posune / Anthropic obnoví týdenní limit),
správce ho nezvedne nebo nepovolí výjimku do konce týdne. Rozdělaná práce
doběhne. Bez stopky limit jen upozorňuje — adminovi i člověku samotnému.
Hlídač na pozadí (`sledovani`) přepočítává po pár minutách i bez otevřených
Statistik a počítá, kolikrát kdo přes limit přešel.
"""
import calendar
import hashlib
import json
import os
import re
import threading
import time

from hub import stats

from . import config, safefs, workspace

CACHE = os.path.join(config.GATEWAY_DIR, "spotreba-cache.json")
LIMITY = os.path.join(config.GATEWAY_DIR, "spotreba-limity.json")
PRESLO = os.path.join(config.GATEWAY_DIR, "spotreba-preslo.json")   # kolikrát přes strop
VYJIMKY = os.path.join(config.GATEWAY_DIR, "spotreba-vyjimky.json")  # „povolit do konce týdne“
POCITACE = os.path.join(config.GATEWAY_DIR, "spotreba-pocitace.json")  # souhrny z počítačů
DNI = 7
STARE = 8 * 86400            # přepis starší než tohle se do týdne nevejde
OBNOVA = 300                 # nejdřív za 5 minut znovu projít disk
BLIZKO = 0.8                 # od kolika procent stropu se varuje
POCITAC_ZAPOMENOUT = 14 * 86400   # počítač, který se tak dlouho neozval, se zahodí
POCITAC_RELACI = 3000        # strop sezení v jednom souhrnu z počítače
DRUHY = ("usd", "pct", "tokeny")

_lock = threading.Lock()
_stav = {"cas": 0, "bezi": False, "lide": {}}
_pc_lock = threading.Lock()
_SID = re.compile(r"[A-Za-z0-9_-]{1,80}")
_DEN = re.compile(r"\d{4}-\d{2}-\d{2}")
_TOKEN = re.compile(r"sk-ant-[A-Za-z0-9_-]{20,400}")


def _load(path):
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    os.replace(tmp, path)


def _cislo(raw, nazev, strop, cele=False):
    """Číslo z formuláře nebo příkazové řádky; prázdné = 0 (bez limitu)."""
    try:
        v = float(str(raw if raw is not None else "").replace(",", ".").replace(" ", "") or 0)
    except ValueError:
        raise ValueError(f"{nazev} musí být číslo.") from None
    if v < 0 or v > strop:
        raise ValueError(f"{nazev} musí být mezi 0 a {strop:,.0f}.".replace(",", " "))
    return int(v) if cele else round(v, 2)


# ── limity ───────────────────────────────────────────────────────────────────
def _limit_z(v):
    """Záznam z souboru → {"usd", "pct", "tokeny", "stopka"}, nebo None.
    Starý zápis (jen číslo) je strop v USD bez stopky."""
    if isinstance(v, (int, float, str)) and not isinstance(v, bool):
        try:
            v = {"usd": float(v)}
        except ValueError:
            return None
    if not isinstance(v, dict):
        return None
    out = {}
    for k in DRUHY:
        try:
            x = float(v.get(k) or 0)
        except (TypeError, ValueError):
            x = 0
        out[k] = (int(x) if k == "tokeny" else round(x, 2)) if x > 0 else 0
    if not any(out[k] for k in DRUHY):
        return None
    out["stopka"] = v.get("stopka") is True
    return out


def limity():
    """{e-mail: {"usd", "pct", "tokeny", "stopka"}} — jen ti, kdo nějaký limit mají."""
    out = {}
    for email, v in _load(LIMITY).items():
        lim = _limit_z(v)
        if lim:
            out[str(email).lower()] = lim
    return out


_NIC = object()


def set_limity(email, usd=_NIC, pct=_NIC, tokeny=_NIC, stopka=_NIC):
    """Změní limity člověka; co se nepředá, zůstává. 0 / prázdné = ten druh
    limitu nemá. Vrací, co teď platí ({"usd": 0, …} = bez limitu)."""
    email = str(email or "").strip().lower()
    if not email:
        raise ValueError("Chybí e-mail.")
    data = {str(k).lower(): v for k, v in _load(LIMITY).items()}
    lim = _limit_z(data.get(email)) or {"usd": 0, "pct": 0, "tokeny": 0, "stopka": False}
    if usd is not _NIC:
        lim["usd"] = _cislo(usd, "Limit v USD", 100000)
    if pct is not _NIC:
        lim["pct"] = _cislo(pct, "Limit v procentech", 100)
    if tokeny is not _NIC:
        lim["tokeny"] = _cislo(tokeny, "Limit tokenů", 10 ** 10, cele=True)
    if stopka is not _NIC:
        lim["stopka"] = stopka is True or str(stopka).lower() in ("1", "true", "ano", "on")
    if any(lim[k] for k in DRUHY):
        # Jen USD bez stopky se zapisuje postaru (číslo) — soubor pak přečte
        # i starší brána, kdyby se verze vracela.
        data[email] = lim["usd"] if not (lim["pct"] or lim["tokeny"] or lim["stopka"]) else lim
    else:
        data.pop(email, None)
        lim["stopka"] = False
    _save(LIMITY, data)
    _zneplatni()
    return lim


def set_limit(email, usd):
    """Týdenní strop v USD (ostatní druhy limitu zůstávají); 0 nebo prázdné = bez stropu."""
    return set_limity(email, usd=usd)["usd"]


# ── výjimka „povolit do konce týdne“ ─────────────────────────────────────────
def konec_tydne(now=None):
    """Příští pondělí 0:00 místního času serveru — konec týdne, jak ho řekne člověk."""
    now = time.time() if now is None else now
    t = time.localtime(now)
    dnes = time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 0, 0, 0, 0, 0, -1))
    # +3 h: přes změnu času by „za 7 dní“ mohlo padnout na neděli 23:00.
    cil = time.localtime(dnes + (7 - t.tm_wday) * 86400 + 3 * 3600)
    return int(time.mktime((cil.tm_year, cil.tm_mon, cil.tm_mday, 0, 0, 0, 0, 0, -1)))


def vyjimky(now=None):
    """{e-mail: {"do", "kdo", "kdy"}} — jen platné výjimky."""
    now = time.time() if now is None else now
    out = {}
    for email, v in _load(VYJIMKY).items():
        try:
            if isinstance(v, dict) and float(v.get("do") or 0) > now:
                out[str(email).lower()] = v
        except (TypeError, ValueError):
            continue
    return out


def povolit(email, kdo=""):
    """Stopka se do konce týdne nezapne (limit dál ukazuje, kolik kdo čerpá)."""
    email = str(email or "").strip().lower()
    if not email:
        raise ValueError("Chybí e-mail.")
    data = {k: v for k, v in vyjimky().items()}
    data[email] = {"do": konec_tydne(), "kdo": str(kdo or "")[:120], "kdy": int(time.time())}
    _save(VYJIMKY, data)
    _zneplatni()
    return data[email]["do"]


def zrusit_vyjimku(email):
    email = str(email or "").strip().lower()
    data = vyjimky()
    had = data.pop(email, None) is not None
    _save(VYJIMKY, data)
    _zneplatni()
    return had


# ── souhrny z počítačů ───────────────────────────────────────────────────────
_pc_memo = {"klic": None, "data": {}}


def _pocitace():
    """Obsah POCITACE; drží se v paměti, dokud se soubor nezmění (čte se
    při každém dotazu na stopku)."""
    try:
        st = os.stat(POCITACE)
        klic = (st.st_mtime_ns, st.st_size)
    except OSError:
        return {}
    with _pc_lock:
        if _pc_memo["klic"] != klic:
            _pc_memo.update(klic=klic, data=_load(POCITACE))
        return _pc_memo["data"]


def uloz_pocitac(user, form, now=None):
    """Souhrn spotřeby z hubu na počítači (hub/spotreba_pc.py). Vrací nic
    nebo vyhodí ValueError s větou pro člověka."""
    now = time.time() if now is None else now
    pc = form.get("pocitac") if isinstance(form.get("pocitac"), dict) else {}
    pid = str(pc.get("id") or "")
    if not re.fullmatch(r"[a-f0-9]{16}", pid):
        raise ValueError("Chybí id počítače.")
    nazev = " ".join(str(pc.get("nazev") or "počítač").split())[:60] or "počítač"
    ucet = form.get("ucet") if isinstance(form.get("ucet"), dict) else {}
    uuid = str(ucet.get("uuid") or "")
    uuid = uuid if re.fullmatch(r"[A-Za-z0-9-]{8,64}", uuid) else ""
    relace_in = form.get("relace") if isinstance(form.get("relace"), dict) else {}
    if len(relace_in) > POCITAC_RELACI:
        raise ValueError("Moc sezení v jednom souhrnu.")
    hranice = time.strftime("%Y-%m-%d", time.gmtime(now - STARE))
    zitra = time.strftime("%Y-%m-%d", time.gmtime(now + 86400))
    relace = {}
    for sid, dny in relace_in.items():
        if not _SID.fullmatch(str(sid)) or not isinstance(dny, dict):
            continue
        cisty = {}
        for den, v in list(dny.items())[:10]:
            if not _DEN.fullmatch(str(den)) or not (hranice <= den <= zitra):
                continue
            try:
                cost, out = float(v[0]), int(v[1])
            except (TypeError, ValueError, IndexError, KeyError):
                continue
            if 0 <= cost <= 100000 and 0 <= out <= 10 ** 9:
                cisty[den] = [round(cost, 4), out]
        if cisty:
            relace[str(sid)] = cisty
    email = user["email"].lower()
    with _pc_lock:
        data = _load(POCITACE)
        lidi = data.setdefault(email, {})
        lidi[pid] = {"nazev": nazev, "cas": int(now), "ucet": uuid, "relace": relace}
        for e in list(data):
            for p in list(data[e]):
                if now - float(data[e][p].get("cas") or 0) > POCITAC_ZAPOMENOUT:
                    data[e].pop(p)
            if not data[e]:
                data.pop(e)
        _save(POCITACE, data)
        _pc_memo["klic"] = None
    _zneplatni()


# ── spotřeba v prostorech (přepisy na serveru) ───────────────────────────────
def _prepisy(home):
    """Přepisy sezení z posledního týdne; symlinky se přeskakují (domov je
    psatelný pro prostor člověka, tak ať brána nečte, kam ukáže on)."""
    root = os.path.join(home, ".claude", "projects")
    hranice = time.time() - STARE
    try:
        slozky = [e for e in os.scandir(root) if e.is_dir(follow_symlinks=False)]
    except OSError:
        return
    for slozka in slozky:
        try:
            for e in os.scandir(slozka.path):
                if e.name.endswith(".jsonl") and e.is_file(follow_symlinks=False):
                    st = e.stat(follow_symlinks=False)
                    if st.st_mtime >= hranice:
                        yield e.path, st
        except OSError:
            continue


def _clovek(home, cache):
    """{id sezení: {den: [cena, tokeny]}} z přepisů v domově. Po sezeních,
    ne jen po dnech — podle id se pak pozná chat přenesený z počítače."""
    relace = {}
    soubory = set()
    for path, st in _prepisy(home):
        soubory.add(path)
        c = cache.get(path)
        if not (c and c.get("size") == st.st_size and c.get("mtime") == int(st.st_mtime)):
            try:
                r = stats._scan_file(path)
            except OSError:
                continue
            c = cache[path] = {"size": st.st_size, "mtime": int(st.st_mtime),
                               "days": r["days"], "dcost": r["dcost"]}
        sid = os.path.basename(path)[:-6]
        per = relace.setdefault(sid, {})
        for den, usd in (c.get("dcost") or {}).items():
            out = (c.get("days") or {}).get(den, 0)
            old = per.get(den) or [0.0, 0]
            # Stejné id dvakrát (kopie chatu v jiné složce) je tentýž chat.
            per[den] = [max(old[0], usd), max(old[1], out)]
    return relace, soubory


def _projdi(users):
    cache = _load(CACHE)
    videne = set()
    lide = {}
    for u in users:
        try:
            home = workspace.home_for(u)
        except OSError:
            continue
        relace, soubory = _clovek(home, cache)
        videne |= soubory
        lide[u["email"].lower()] = relace
    for gone in set(cache) - videne:
        cache.pop(gone, None)
    try:
        _save(CACHE, cache)
    except OSError:
        pass
    return lide


def preslo():
    """{e-mail: {"pocet": kolikrát přes strop, "stav": poslední stav, "naposledy": kdy,
    "stopka": platila stopka, "stopka_do": kdy se měla uvolnit}}"""
    return _load(PRESLO)


def _zapocti(radky):
    """Přechod z „v pořádku / blízko“ na „nad stropem“ je jedno překročení.
    Člověk, který zůstane nad, se nepočítá znovu; až klesne a zase přeleze,
    připočte se další. Změnu stavu zapíše na disk — i stopku, ať po restartu
    brány platí hned, ne až po prvním přepočtu."""
    data = preslo()
    zmena = False
    for r in radky:
        email = r["email"].lower()
        zaznam = data.get(email) or {"pocet": 0, "stav": "", "naposledy": ""}
        stav = r["stav"]
        stopka = bool(r.get("stopka"))
        stopka_do = int((r.get("stopka") or {}).get("do") or 0)
        if stav != zaznam.get("stav") or stopka != bool(zaznam.get("stopka")) \
                or stopka_do != int(zaznam.get("stopka_do") or 0):
            if stav == "nad" and zaznam.get("stav") != "nad":
                zaznam["pocet"] = int(zaznam.get("pocet") or 0) + 1
                zaznam["naposledy"] = time.strftime("%Y-%m-%d %H:%M")
            zaznam["stav"] = stav
            zaznam["stopka"] = stopka
            zaznam["stopka_do"] = stopka_do
            data[email] = zaznam
            zmena = True
    if zmena:
        try:
            _save(PRESLO, data)
        except OSError:
            pass


def _na_pozadi(users):
    try:
        lide = _projdi(users)
        with _lock:
            _stav["lide"] = lide
            _stav["cas"] = time.time()
        _zneplatni()
        _zapocti(_radky(users))
    except Exception as exc:                      # hlídač nesmí spadnout s bránou
        print("spotřeba:", exc, flush=True)
    finally:
        with _lock:
            _stav["bezi"] = False


def sledovani(get_users, kazdych=300):
    """Vlákno brány: každých pár minut přepočítá spotřebu (a procenta u těch,
    kdo mají limit v procentech), ať se překročení i stopka projeví, i když
    Statistiky nikdo nemá otevřené."""
    def smycka():
        time.sleep(30)
        while True:
            with _lock:
                volno = not _stav["bezi"]
                if volno:
                    _stav["bezi"] = True
            if volno:
                try:
                    users = [dict(u) for u in get_users()]
                    lim = limity()
                    s_procenty = [u for u in users if (lim.get(u["email"].lower()) or {}).get("pct")]
                    if s_procenty:
                        _plan_obnov(s_procenty)
                    _na_pozadi(users)
                except Exception as exc:
                    print("spotřeba:", exc, flush=True)
                    with _lock:
                        _stav["bezi"] = False
            time.sleep(kazdych)
    threading.Thread(target=smycka, daemon=True).start()


# ── využití předplatného (hub/usage.py) po lidech ───────────────────────────
# Brána drží token každého, kdo připojil vlastní předplatné (workspace.predplatne);
# kdo se přihlásil přímo v prostoru, má přihlášení v domově. Pro admina z toho
# jde ukázat, kdo je jak blízko skutečnému limitu, a z toho počítat limit
# v procentech; ostatním jen jejich vlastní. Odpovědi se drží 5 minut a berou
# se na pozadí. Sdílené přihlášení brány (dřívější `central`) už neexistuje —
# workspace.ensure jeho odkazy z domovů uklízí.
PLAN_OBNOVA = 300
_plan = {"lide": {}, "klic": {}, "cas": {}, "bezi": False}


def _ucet_prostoru(user):
    """(token, id účtu Claude) přihlášení v domově prostoru („ucet“). Čte se
    bez následování odkazů — domov patří prostoru."""
    try:
        home = workspace.home_for(user)
    except OSError:
        return "", ""
    token = uuid = ""
    raw = safefs.read_text(home, ".claude/.credentials.json", limit=256 * 1024)
    try:
        oauth = (json.loads(raw) or {}).get("claudeAiOauth") or {} if raw else {}
        tok = str(oauth.get("accessToken") or "")
        exp = float(oauth.get("expiresAt") or 0) / 1000
        # Prošlý token by Anthropic jen odmítl — obnovit ho smí jen Claude
        # Code v prostoru (obnovení by ho odhlásilo).
        if _TOKEN.fullmatch(tok) and (not exp or exp > time.time()):
            token = tok
    except (ValueError, AttributeError, TypeError):
        pass
    raw = safefs.read_text(home, ".claude.json", limit=4 * 1024 * 1024)
    try:
        acc = (json.loads(raw) or {}).get("oauthAccount") or {} if raw else {}
        uuid = str(acc.get("accountUuid") or "")[:64]
    except (ValueError, AttributeError, TypeError):
        pass
    return token, uuid


def _rezim(user):
    try:
        return workspace.claude_state(user).get("mode") or "zadne"
    except OSError:
        return "zadne"


def _plan_obnov(users):
    """Zeptá se Anthropicu na využití předplatného u každého z `users`, kdo
    nějaké má. Neúspěch nesmaže poslední dobrou odpověď, dokud se týdenní
    limit neobnoví — do té doby procenta jen rostou, takže je to dolní mez."""
    from hub import usage
    for u in users:
        mode = _rezim(u)
        if mode == "predplatne":
            sub = workspace.predplatne(u) or {}
            token, uuid = str(sub.get("token") or ""), ""
        elif mode == "ucet":
            token, uuid = _ucet_prostoru(u)
        else:
            token = uuid = ""
        with _lock:
            stare = _plan["lide"].get(u["id"])
        if not token:
            r = {"ok": False, "druh": "bez-prihlaseni", "chyba": "Přihlášení v prostoru teď nejde přečíst."}
        else:
            r = usage.fetch(token)
        r["cas"] = int(time.time())
        r["rezim"] = mode
        if not r.get("ok") and stare and stare.get("ok") and _resets_ts((stare.get("week") or {})
                                                                         .get("resets")) > time.time():
            r = dict(stare, stare=True, chyba=r.get("chyba") or "")
        with _lock:
            _plan["lide"][u["id"]] = r
            _plan["cas"][u["id"]] = time.time()
            if token:
                _plan["klic"][u["id"]] = uuid or "tok:" + hashlib.sha256(token.encode()).hexdigest()[:16]
    _zneplatni()


def _plan_pro(users):
    """Poslední známé využití předplatného po lidech; zastaralé se obnoví na pozadí."""
    with _lock:
        stale = [u for u in users if time.time() - _plan["cas"].get(u["id"], 0) > PLAN_OBNOVA]
        if stale and not _plan["bezi"]:
            _plan["bezi"] = True

            def bez():
                try:
                    _plan_obnov(stale)
                finally:
                    with _lock:
                        _plan["bezi"] = False
            threading.Thread(target=bez, daemon=True).start()
        return dict(_plan["lide"]), dict(_plan["klic"])


def _resets_ts(iso):
    if not iso:
        return 0
    try:
        from datetime import datetime
        return datetime.fromisoformat(str(iso).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return 0


# ── výpočet: řádky po lidech, stav limitu, stopka ────────────────────────────
_memo = {"klic": None, "t": 0, "radky": None}
MEMO = 20       # dotaz na stopku chodí s každou zprávou — počítat znovu nejdřív za tolik vteřin


def _zneplatni():
    _memo["klic"] = None


def _stejne(user, rezim, uuid_pc, ucty):
    """Jede počítač na stejném předplatném jako prostor? Jen pak se jeho
    spotřeba počítá do limitů. Předplatné připojené z appky vzniklo na
    počítači toho člověka (`claude setup-token`) — bere se jako stejné.
    U přihlášení v prostoru rozhodne id účtu Claude."""
    if rezim == "predplatne":
        return True
    if rezim == "ucet":
        if user["id"] not in ucty:
            ucty[user["id"]] = _ucet_prostoru(user)[1]
        return bool(uuid_pc) and uuid_pc == ucty[user["id"]]
    return False


def pocitac_stejny(user, pid):
    """Jede tenhle počítač (id z hubu) na stejném předplatném jako prostor?"""
    d = (_pocitace().get(user["email"].lower()) or {}).get(pid) or {}
    return _stejne(user, _rezim(user), d.get("ucet") or "", {})


def _uvolneni(dny, klic, limit, now):
    """Kdy klesne součet za posledních 7 dní pod limit, když už nic nepřibude
    (týden se posouvá po dnech, půlnoc UTC)."""
    dnes = calendar.timegm(time.gmtime(now)[:3] + (0, 0, 0))
    for k in range(1, DNI + 1):
        od = time.strftime("%Y-%m-%d", time.gmtime(dnes + (k - DNI + 1) * 86400))
        if sum(d[klic] for den, d in dny.items() if den >= od) < limit:
            return dnes + k * 86400
    return dnes + DNI * 86400


DNY_TYDNE = ("v pondělí", "v úterý", "ve středu", "ve čtvrtek", "v pátek", "v sobotu", "v neděli")


def kdy_text(ts, now=None):
    """„dnes v 17:00“, „zítra v 1:00“, „v pondělí 3. 11. v 1:00“ — čas serveru."""
    now = time.time() if now is None else now
    if not ts:
        return ""
    t, n = time.localtime(ts), time.localtime(now)
    hod = f"{t.tm_hour}:{t.tm_min:02d}"
    if (t.tm_year, t.tm_yday) == (n.tm_year, n.tm_yday):
        return "dnes v " + hod
    zitra = time.localtime(now + 86400)
    if (t.tm_year, t.tm_yday) == (zitra.tm_year, zitra.tm_yday):
        return "zítra v " + hod
    return f"{DNY_TYDNE[t.tm_wday]} {t.tm_mday}. {t.tm_mon}. v {hod}"


def _tok(n):
    n = int(n or 0)
    if n >= 10 ** 6:
        return f"{n / 10 ** 6:.1f}".rstrip("0").rstrip(".").replace(".", ",") + " mil."
    return f"{n:,}".replace(",", " ")


def _duvody(r, lim):
    out = []
    for k in r["nad"]:
        if k == "usd":
            out.append(f"odhadem {r['tyden']:.0f} $ z {lim['usd']:g} $")
        elif k == "tokeny":
            out.append(f"{_tok(r['tokenu'])} z {_tok(lim['tokeny'])} tokenů")
        elif k == "pct":
            co = "podílu na spotřebě týmu" if r["procenta"]["zdroj"] == "tym" else "týdenního limitu předplatného"
            out.append(f"{r['procenta']['pct']:.0f} % z povolených {lim['pct']:g} % {co}")
    return out


def stopka_text(r, lim, do, now=None):
    kdy = kdy_text(do, now)
    return ("Správce ti nastavil týdenní limit na Clauda a ten je vyčerpaný ("
            + ", ".join(_duvody(r, lim)) + "). Nové zprávy Claudovi teď neodejdou"
            + (f" — uvolní se {kdy}, dřív jen když správce limit zvedne nebo povolí výjimku."
               if kdy else ", dokud správce limit nezvedne nebo nepovolí výjimku, nebo dokud spotřeba"
                           " v týdnu neklesne.")
            + " Rozdělaná práce doběhne.")


def _radky(users, now=None):
    """Řádek pro každého z `users`: spotřeba za týden (server + počítač na
    stejném předplatném), procenta, limity a stopka. Drží se MEMO vteřin."""
    klic = tuple((u["id"], u["email"].lower(), bool(u.get("disabled"))) for u in users)
    if now is None:
        with _lock:
            if _memo["klic"] == klic and time.time() - _memo["t"] < MEMO:
                return [dict(r) for r in _memo["radky"]]
        now = time.time()
        pamatuj = True
    else:
        pamatuj = False
    with _lock:
        lide, cas = dict(_stav["lide"]), _stav["cas"]
        plan, plan_klic = dict(_plan["lide"]), dict(_plan["klic"])
    pcs = _pocitace()
    lim_vse, vyj, kolikrat = limity(), vyjimky(now), preslo()
    hranice = time.strftime("%Y-%m-%d", time.gmtime(now - (DNI - 1) * 86400))
    dnes = time.strftime("%Y-%m-%d", time.gmtime(now))
    ucty = {}
    out = []
    for u in users:
        email = u["email"].lower()
        rezim = _rezim(u)
        srv = lide.get(email) or {}
        stejne_rel, jine_rel, zarizeni = {}, {}, []
        for pid, d in (pcs.get(email) or {}).items():
            stejne = _stejne(u, rezim, d.get("ucet") or "", ucty)
            zarizeni.append({"nazev": d.get("nazev") or "počítač", "cas": int(d.get("cas") or 0),
                             "stejne": stejne})
            cil = stejne_rel if stejne else jine_rel
            for sid, per in (d.get("relace") or {}).items():
                for den, v in per.items():
                    old = cil.setdefault(sid, {}).get(den) or [0.0, 0]
                    cil[sid][den] = [max(old[0], v[0]), max(old[1], v[1])]
        dny = {}
        for sid in set(srv) | set(stejne_rel):
            s, p = srv.get(sid) or {}, stejne_rel.get(sid) or {}
            for den in set(s) | set(p):
                if den < hranice:
                    continue
                sc, so = s.get(den) or (0.0, 0)
                pc, po = p.get(den) or (0.0, 0)
                d = dny.setdefault(den, {"cost": 0.0, "out": 0, "cost_pc": 0.0, "out_pc": 0})
                d["cost"] += max(sc, pc)
                d["out"] += max(so, po)
                d["cost_pc"] += max(0.0, pc - sc)
                d["out_pc"] += max(0, po - so)
        jinde = [0.0, 0]
        for sid, per in jine_rel.items():
            for den, (pc, po) in per.items():
                if den >= hranice:
                    sc, so = (srv.get(sid) or {}).get(den) or (0.0, 0)
                    jinde[0] += max(0.0, pc - sc)
                    jinde[1] += max(0, po - so)
        tyden = sum(d["cost"] for d in dny.values())
        tokenu = sum(d["out"] for d in dny.values())
        tyden_pc = sum(d["cost_pc"] for d in dny.values())
        tokenu_pc = sum(d["out_pc"] for d in dny.values())
        p = plan.get(u["id"])
        out.append({
            "id": u["id"], "email": u["email"], "name": u.get("name") or "",
            "tyden": round(tyden, 2), "tokenu": tokenu,
            "dnes": round((dny.get(dnes) or {}).get("cost", 0.0), 2),
            "dny": [{"den": den, "cost": round(d["cost"], 2), "cost_pc": round(d["cost_pc"], 2)}
                    for den, d in sorted(dny.items())],
            "server": {"tyden": round(tyden - tyden_pc, 2), "tokenu": tokenu - tokenu_pc},
            "pocitac": {"tyden": round(tyden_pc, 2), "tokenu": tokenu_pc,
                        "jinde_tyden": round(jinde[0], 2), "jinde_tokenu": jinde[1],
                        "zarizeni": sorted(zarizeni, key=lambda z: -z["cas"])},
            "rezim": rezim,
            "preslo": int((kolikrat.get(email) or {}).get("pocet") or 0),
            "preslo_kdy": (kolikrat.get(email) or {}).get("naposledy") or "",
            "zablokovany": bool(u.get("disabled")),
            "plan": p,
            "_dny": dny, "_klic": plan_klic.get(u["id"]) if p and p.get("ok") else None,
        })
    soucet = sum(r["tyden"] for r in out)
    skupiny = {}
    for r in out:
        if r["_klic"] and (r["plan"].get("week") or {}).get("pct") is not None:
            skupiny.setdefault(r["_klic"], []).append(r)
    for r in out:
        r["podil"] = round(r["tyden"] / soucet, 3) if soucet else 0
        week = (r["plan"] or {}).get("week") if r["_klic"] else None
        if week and week.get("pct") is not None:
            clenove = skupiny[r["_klic"]]
            if len(clenove) > 1:
                cena = sum(m["tyden"] for m in clenove)
                dil = r["tyden"] / cena if cena else 1 / len(clenove)
                r["procenta"] = {"pct": round(week["pct"] * dil, 1), "zdroj": "sdileny",
                                 "ucet_pct": week["pct"], "lidi": len(clenove), "resets": week.get("resets")}
            else:
                r["procenta"] = {"pct": week["pct"], "zdroj": r["rezim"], "resets": week.get("resets")}
        elif r["rezim"] in ("api", "zadne"):
            r["procenta"] = {"pct": round(r["podil"] * 100, 1), "zdroj": "tym", "resets": None}
        else:
            # Předplatné tu je, ale Anthropic teď procenta neřekl — limit
            # v procentech se do té doby nevyhodnocuje (radši nic než omyl).
            r["procenta"] = {"pct": None, "zdroj": r["rezim"], "resets": None}
        _vyhodnot(r, lim_vse.get(r["email"].lower()), vyj.get(r["email"].lower()),
                  kolikrat.get(r["email"].lower()) or {}, bool(cas), now)
    for r in out:
        r.pop("_dny", None)
        r.pop("_klic", None)
    out.sort(key=lambda r: -r["tyden"])
    if pamatuj:
        with _lock:
            _memo.update(klic=klic, t=time.time(), radky=out)
        return [dict(r) for r in out]
    return out


def _vyhodnot(r, lim, vyj, zaznam, spocitano, now):
    """Doplní do řádku limit, stav (ok / blizko / nad), co je překročené
    a jestli platí stopka (a do kdy)."""
    r["limit"] = (lim or {}).get("usd", 0)          # USD — čte i starší hub
    r["limity"] = lim or {"usd": 0, "pct": 0, "tokeny": 0, "stopka": False}
    r["vyjimka"] = {"do": int(vyj["do"]), "kdo": vyj.get("kdo") or ""} if vyj else None
    r["nad"], r["stav"], r["stopka"] = [], "", None
    if not lim:
        return
    pomery = {}
    if lim["usd"]:
        pomery["usd"] = r["tyden"] / lim["usd"]
    if lim["tokeny"]:
        pomery["tokeny"] = r["tokenu"] / lim["tokeny"]
    if lim["pct"] and r["procenta"]["pct"] is not None:
        pomery["pct"] = r["procenta"]["pct"] / lim["pct"]
    r["pomery"] = {k: round(v, 3) for k, v in pomery.items()}
    r["nad"] = [k for k in DRUHY if pomery.get(k, 0) >= 1]
    r["stav"] = "nad" if r["nad"] else ("blizko" if max(pomery.values(), default=0) >= BLIZKO else "ok")
    if not lim["stopka"] or vyj or r["zablokovany"]:
        return
    if not spocitano and not r["nad"]:
        # Brána se právě rozjíždí a přepisy ještě nepřečetla (čísla jsou zatím
        # jen dolní mez) — platí stopka, jak ji hlídač zapsal naposledy. Jinak
        # by restart brány stopku na pár minut zrušil.
        if zaznam.get("stopka") and float(zaznam.get("stopka_do") or 0) > now:
            do = int(zaznam["stopka_do"])
            r["stopka"] = {"do": do, "kdy": kdy_text(do, now),
                           "text": "Správce ti nastavil týdenní limit na Clauda a ten je vyčerpaný. "
                                   f"Nové zprávy Claudovi teď neodejdou — uvolní se {kdy_text(do, now)}, "
                                   "dřív jen když správce limit zvedne nebo povolí výjimku."}
        return
    if not r["nad"]:
        return
    kdy = []
    for k in r["nad"]:
        if k == "usd":
            kdy.append(_uvolneni(r["_dny"], "cost", lim["usd"], now))
        elif k == "tokeny":
            kdy.append(_uvolneni(r["_dny"], "out", lim["tokeny"], now))
        elif r["procenta"]["zdroj"] != "tym":
            kdy.append(_resets_ts(r["procenta"].get("resets")) or 0)
        else:
            kdy.append(0)          # podíl na týmu se uvolní, až přidají ostatní — neví se kdy
    do = int(max(kdy)) if kdy and min(kdy) > 0 else 0
    r["stopka"] = {"do": do, "kdy": kdy_text(do, now), "druhy": list(r["nad"]),
                   "text": stopka_text(r, lim, do, now)}


def prehled(users, only=None, wait=0.0):
    """Spotřeba za posledních 7 dní po lidech. `users` = řádky účtů,
    `only` = e-mail, pro kterého se vrací jen jeden řádek (běžný člověk).
    Vrací {"lide": [...], "pocita": bool, "stari": sekundy}."""
    with _lock:
        stale = time.time() - _stav["cas"] > OBNOVA
        if stale and not _stav["bezi"]:
            _stav["bezi"] = True
            threading.Thread(target=_na_pozadi, args=([dict(u) for u in users],),
                             daemon=True).start()
    if wait:
        end = time.time() + wait
        while time.time() < end and not _stav["cas"]:
            time.sleep(0.2)
    with _lock:
        cas, bezi = _stav["cas"], _stav["bezi"]
    _plan_pro(users)
    radky = _radky(users)
    soucet = sum(r["tyden"] for r in radky)
    if only:
        radky = [r for r in radky if r["email"].lower() == only.lower()]
    return {"lide": radky, "soucet": round(soucet, 2),
            "pocita": bool(bezi and not cas), "stari": int(time.time() - cas) if cas else None}


def _pro_cloveka(r):
    """Co z řádku smí vidět člověk sám o sobě (hub v prostoru, appka na počítači)."""
    keys = ("tyden", "tokenu", "dnes", "limit", "limity", "stav", "nad", "procenta", "preslo",
            "vyjimka", "server", "pocitac", "pomery")
    return {k: r.get(k) for k in keys}


def stopka_pro(users, user):
    """Stav jednoho člověka pro hub v prostoru a hook (`/gw/spotreba/stopka`)
    a pro appku na počítači. Nikdy nečeká na přepočet — bere poslední známá
    čísla a přepočet nanejvýš spustí na pozadí."""
    with _lock:
        stale = time.time() - _stav["cas"] > OBNOVA
        if stale and not _stav["bezi"]:
            _stav["bezi"] = True
            threading.Thread(target=_na_pozadi, args=([dict(u) for u in users],),
                             daemon=True).start()
    email = user["email"].lower()
    r = next((x for x in _radky(users) if x["email"].lower() == email), None)
    if not r:
        return {"radek": None, "stopka": None}
    return {"radek": _pro_cloveka(r), "stopka": r.get("stopka")}
