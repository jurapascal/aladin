"""
Účet na bráně — appka jako okno do prostoru na serveru.

Hub běží na dvou místech a je to schválně: na počítači sahá na tvoje projekty,
na serveru běží pořád a je dostupný odkudkoli. Tenhle modul drží to třetí —
**že jsi to na obou stranách ty**.

Tok, kterým appka projde, když si člověk vybere server:

1. `probe()` — ověří adresu dřív, než se napíše heslo. Nestačí, že „něco
   odpovídá": na překlepnuté adrese by se heslo psalo do cizího webu.
2. `login()` — heslo se pošle jednou a zpátky přijde token zařízení.
3. `handoff()` — token se vymění za jednorázový kód do adresy okna, a okno je
   pak přihlášené i bez hesla. Totéž se děje při každém dalším spuštění.

Dvě věci, které stojí za vysvětlení:

* **Heslo se nikam neukládá.** V konfiguraci leží jen token zařízení; heslo
  hub nezná ani vteřinu po přihlášení.

* **Přepnutí na server nejde přes token v adrese.** Prohlížeč potřebuje cookie
  na doméně brány, ale token v odkazu by skončil v historii. Brána proto token
  vymění za jednorázový kód s minutovou platností (`/gw/handoff`), a ten se
  při prvním použití zahodí.

Když je server nedostupný, hub se netváří jako odhlášený — vrátí poslední
známý účet s `offline: True`. Odhlášení kvůli výpadku sítě by znamenalo psát
heslo znovu pokaždé, když je vlak v tunelu.
"""
import datetime
import email.utils
import json
import os
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request

from . import core

TIMEOUT = 12
UA = "claude-code-hub"

# Druh chyby — podle něj se appka rozhoduje, co ukázat. Věta pro člověka jde
# vedle; porovnávat věty mezi sebou (jak se to dělalo dřív) se rozbije první
# úpravou textu.
OFFLINE = "offline"      # server se nedá zastihnout
AUTH = "auth"            # server žije, ale token nezná
HTTP = "http"            # server odpověděl chybou


def normalize(raw):
    """Z čehokoli, co člověk napíše, udělá kořen brány.

    `test.alba-rosa.cz` → `https://test.alba-rosa.cz`; vložená adresa i s
    cestou (`…/login`) se ořízne, jinak by se API volalo o patro níž.
    """
    raw = (raw or "").strip()
    if not raw:
        return ""
    if "://" not in raw:
        raw = "https://" + raw
    parts = urllib.parse.urlsplit(raw)
    if not parts.netloc:
        return ""
    return f"{parts.scheme}://{parts.netloc}"


def _base():
    """Adresa brány z konfigurace, bez lomítka na konci."""
    return normalize(core.CONFIG.get("gw_server") or "")


def _host(base):
    return urllib.parse.urlsplit(base).netloc or base


# Ověření certifikátu: nejdřív úložištěm systému, pak teprve po svém.
_CTX = None
_CTX_OS = False


def _context():
    """Jak se ověřuje certifikát brány.

    Python si při startu udělá kopii seznamu důvěryhodných certifikátů a dál
    se drží jí: stačí v úložišti jeden propadlý kořen a spojení skončí na
    „certificate has expired", i když prohlížeč na tomtéž stroji tutéž adresu
    otevře bez mrknutí — ten se ptá Windows a ty si cestu najdou jinudy.
    S balíčkem `truststore` se ptáme stejně jako prohlížeč. Když není,
    zůstává výchozí ověření (a `--doctor` to řekne).
    """
    global _CTX, _CTX_OS
    if _CTX is None:
        try:
            import truststore
            _CTX = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            _CTX_OS = True
        except Exception:
            _CTX = ssl.create_default_context()
            _CTX_OS = False
    return _CTX


def _ca_store_hint():
    """Kde tenhle počítač bere důvěryhodné certifikáty — a co s tím, když je nemá.

    Ta samá adresa chodí v prohlížeči a v hubu ne, protože Python si důvěru
    nebere z prohlížeče. Na Macu ji build z python.org nemá vůbec, dokud
    nespustíš Install Certificates.command; na Windows kořeny doplňují
    aktualizace; na Linuxu je nese balíček ca-certificates.
    """
    for name in ("SSL_CERT_FILE", "SSL_CERT_DIR"):
        path = os.environ.get(name)
        if path and not os.path.exists(path):
            return (f"proměnná {name} ukazuje na {path}, což tu není. "
                    "Smaž ji, nebo ji naveď na skutečný seznam certifikátů.")
    try:
        empty = not ssl.create_default_context().cert_store_stats().get("x509_ca")
    except Exception:
        empty = False
    if empty:
        if core.IS_MAC:
            return ("tenhle Python nemá žádné důvěryhodné certifikáty. Otevři "
                    "složku Applications/Python 3.x a spusť "
                    "Install Certificates.command.")
        if core.IS_WINDOWS:
            return ("tenhle Python nemá žádné důvěryhodné certifikáty — "
                    "přeinstaluj ho z python.org.")
        return ("tenhle počítač nemá žádné důvěryhodné certifikáty: "
                "sudo apt install --reinstall ca-certificates.")
    if core.IS_MAC:
        return ("doinstaluj aktualizace systému, nebo v Applications/Python 3.x "
                "spusť Install Certificates.command.")
    if core.IS_WINDOWS:
        return ("doinstaluj aktualizace Windows (doplňují kořenové certifikáty) "
                "a zkus v antiviru vypnout kontrolu HTTPS.")
    return "aktualizuj certifikáty: sudo apt install --reinstall ca-certificates."


# Časy v certifikátu: UTCTime (do roku 2049) a GeneralizedTime, každý pevné
# délky — podle toho se v DER poznají.
_TIME_TAGS = {0x17: (13, "%y%m%d%H%M%SZ"), 0x18: (15, "%Y%m%d%H%M%SZ")}


def _der_validity(der):
    """Od kdy do kdy certifikát platí — vyčtené přímo z DER.

    Platnost sedí v certifikátu před rozšířeními, takže první dva časy, na
    které se v bajtech narazí, jsou právě notBefore a notAfter. Je to málo
    kódu a nepotřebuje to knihovnu navíc; když se netrefí, vrátí (None, None)
    a hláška zůstane obecná.
    """
    found = []
    i = 0
    while i < len(der) - 2 and len(found) < 2:
        size, fmt = _TIME_TAGS.get(der[i], (0, ""))
        if size and der[i + 1] == size:
            raw = der[i + 2:i + 2 + size].decode("ascii", "ignore")
            try:
                found.append(datetime.datetime.strptime(raw, fmt)
                             .replace(tzinfo=datetime.timezone.utc))
                i += 2 + size
                continue
            except ValueError:
                pass
        i += 1
    return (found[0], found[1]) if len(found) == 2 else (None, None)


def _peer_validity(host, timeout=5):
    """Platnost certifikátu, který server ukazuje — jen pro text chyby.

    Ověření je tady schválně vypnuté: ptáme se právě proto, že neprošlo.
    Spojení se nepoužije na nic dalšího a nic se po něm neposílá.
    """
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    name, _, port = host.partition(":")
    try:
        with socket.create_connection((name, int(port or 443)), timeout) as raw:
            with ctx.wrap_socket(raw, server_hostname=name) as tls:
                der = tls.getpeercert(True)
    except Exception:
        return None, None
    return _der_validity(der or b"")


def _den(stamp):
    """8. 12. 2026 — v UTC, jak je datum napsané v certifikátu, a bez %-d
    (to na Windows není)."""
    return f"{stamp.day}. {stamp.month}. {stamp.year}"


def _clock_drift(host, timeout=5):
    """O kolik jdou hodiny v počítači vedle proti serveru — nebo None.

    Ptáme se bez ověření certifikátu schválně: voláme to právě proto, že
    ověření neprošlo, a z odpovědi čteme jedinou věc — hlavičku `Date`.
    Nic se neposílá (HEAD bez tokenu) a odpověď se nikam nepromítne než do
    textu chyby. Bez tohohle nejde odlišit propadlý certifikát od počítače,
    který si myslí, že je jiný rok.
    """
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    req = urllib.request.Request(f"https://{host}/gw/info", method="HEAD")
    req.add_header("User-Agent", UA)
    try:
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as res:
                stamp = res.headers.get("Date") or ""
        except urllib.error.HTTPError as exc:
            stamp = exc.headers.get("Date") or ""
        there = email.utils.parsedate_to_datetime(stamp)
    except Exception:
        return None
    if there is None:
        return None
    return datetime.datetime.now(datetime.timezone.utc) - there


def _kolik(count, one, few, many):
    """3 dny, ne 3 dní — čeština počítá do čtyř jinak."""
    return f"{count} " + (one if count == 1 else few if 2 <= count <= 4 else many)


def _drift_words(drift):
    """„3 měsíce napřed" — o kolik a kterým směrem, lidsky."""
    seconds = abs(drift.total_seconds())
    days = seconds / 86400
    if days >= 60:
        size = _kolik(round(days / 30), "měsíc", "měsíce", "měsíců")
    elif days >= 2:
        size = _kolik(round(days), "den", "dny", "dní")
    else:
        size = _kolik(round(seconds / 3600), "hodinu", "hodiny", "hodin")
    return size + (" napřed" if drift.total_seconds() > 0 else " pozadu")


def ca_state():
    """Čím se ověřuje certifikát brány — řádek do `--doctor`.

    Nula kořenů znamená, že na server přes HTTPS nedosáhne nic, i když
    v prohlížeči na tomtéž stroji adresa chodí.
    """
    _context()
    if _CTX_OS:
        return "úložiště systému (truststore)"
    try:
        count = ssl.create_default_context().cert_store_stats().get("x509_ca", 0)
    except Exception as exc:
        return f"nejdou načíst ({exc})"
    if not count:
        return "ŽÁDNÉ — " + _ca_store_hint()
    hint = ("  (kopie z úložiště Windows; propadlý kořen v ní shodí i platný "
            "certifikát — spolehlivější je pip install truststore)"
            if core.IS_WINDOWS else "")
    return _kolik(count, "kořen", "kořeny", "kořenů") + hint


def _stale_root_hint():
    """Propadlý kořen v úložišti — jak ho na téhle platformě obejít."""
    if core.IS_WINDOWS:
        if not _CTX_OS:
            return ("Spusť pip install truststore — cestu pak hledají samotné "
                    "Windows, stejně jako prohlížeči, a propadlou kotvu obejdou. "
                    "Nebo ji smaž v certmgr.msc.")
        return ("Pusť aktualizace Windows; když to nepomůže, otevři certmgr.msc "
                "→ Důvěryhodné kořenové certifikační autority a smaž propadlé "
                "(typicky DST Root CA X3).")
    if core.IS_MAC:
        return ("Pusť aktualizace systému a v Applications/Python 3.x spusť "
                "Install Certificates.command.")
    return "Obnov je: sudo apt install --reinstall ca-certificates."


def _tls_reason(err, host):
    """Certifikát neprošel — čí je to chyba a co se s tím dá dělat.

    Věta „nemá platný certifikát" sváděla na server. Jenže když tatáž adresa
    jinde chodí, je server v pořádku a nedůvěřuje mu jen tenhle počítač —
    proto se rozlišuje podle důvodu, který vrátí OpenSSL.
    """
    code = getattr(err, "verify_code", 0)
    msg = (getattr(err, "verify_message", "") or str(err) or "").lower()
    if code in (9, 10) or "expired" in msg or "not yet valid" in msg:
        now = datetime.datetime.now(datetime.timezone.utc)
        start, end = _peer_validity(host)
        # Papír serveru platí, a přesto „propadlý": propadlo něco v řetězu
        # důvěry na tomhle počítači, ne na serveru.
        if start and end and start <= now <= end:
            return (f"Certifikát {host} platí ({_den(start)} – {_den(end)}), ale "
                    "tenhle počítač ho odmítá kvůli propadlému certifikátu ve "
                    "svém úložišti. " + _stale_root_hint())
        # Hodiny napřed nebo pozadu vypadají přesně jako propadlý certifikát,
        # tak se server rovnou zeptáme, kolik je u něj hodin.
        drift = _clock_drift(host)
        if drift is not None and abs(drift.total_seconds()) > 43200:
            return (f"Hodiny v tomhle počítači jdou o {_drift_words(drift)} proti "
                    f"serveru — proto mu certifikát {host} připadá neplatný. "
                    "Srovnej v počítači datum a čas.")
        if end and now > end:
            return (f"Certifikát pro {host} propadl {_den(end)}. Obnov ho na "
                    "serveru: sudo certbot renew.")
        if start and now < start:
            return f"Certifikát pro {host} začne platit až {_den(start)}."
        return (f"Certifikát pro {host} neplatí. Jestli jinde chodí, má tenhle "
                "počítač špatné datum — zkontroluj hodiny.")
    if code == 62 or "hostname mismatch" in msg or "doesn't match" in msg:
        return (f"Certifikát na {host} je vydaný na jinou adresu. Zkontroluj, "
                "jestli je adresa napsaná správně.")
    # Kód z OpenSSL rozlišuje, kde v řetězu to prasklo; text se čte jen tehdy,
    # když kód nepřišel (jiná knihovna, starší Python).
    signed = msg.replace("-", " ")
    if code == 19 or (not code and "self signed certificate in" in signed):
        return (f"Certifikát {host} vede ke kořeni, kterému tenhle počítač nevěří. "
                "Obvykle to dělá antivirus s kontrolou HTTPS nebo firemní firewall — "
                f"vypni u něj kontrolu HTTPS. Jinak {_ca_store_hint()}")
    if code == 18 or (not code and "self signed certificate" in signed):
        return (f"Certifikát na {host} si server vystavil sám, nikdo za něj neručí. "
                "Nasaď na bránu certifikát od Let's Encrypt (gateway/install.sh).")
    if code in (2, 20, 21) or "local issuer" in msg or "unable to get" in msg:
        return (f"Vydavatele certifikátu {host} tenhle počítač nezná. Server je "
                f"nejspíš v pořádku — {_ca_store_hint()}")
    detail = getattr(err, "verify_message", "") or "ověření selhalo"
    return f"Certifikát {host} neprošel ověřením: {detail}."


def _offline_reason(exc, base):
    """Proč se k serveru nedá dostat — tak, aby se podle toho dalo něco udělat."""
    host = _host(base)
    reason = getattr(exc, "reason", exc)
    if isinstance(reason, socket.gaierror):
        return f"Adresu {host} se nepodařilo najít. Je správně napsaná?"
    if isinstance(reason, ssl.SSLCertVerificationError):
        return _tls_reason(reason, host)
    if isinstance(reason, ConnectionRefusedError):
        return f"{host} odmítl spojení. Server asi neběží."
    if isinstance(reason, (socket.timeout, TimeoutError)):
        return f"{host} neodpověděl včas."
    return f"Server {host} není dostupný ({reason})."


def _call(path, token="", payload=None, method=None, base=None, timeout=TIMEOUT):
    """Zavolá bránu. Vrací (data, chyba, druh) — chyba je hotová věta pro člověka."""
    base = base or _base()
    if not base:
        return None, "Není nastavená adresa serveru.", HTTP
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(base + path, data=data, method=method)
    req.add_header("Accept", "application/json")
    req.add_header("User-Agent", UA)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_context()) as res:
            body = res.read().decode("utf-8", "replace")
        return (json.loads(body) if body else {}), "", ""
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode("utf-8", "replace"))
            note = detail.get("error") or ""
        except Exception:
            note = ""
        if exc.code == 401:
            return None, note or "Přihlášení vypršelo.", AUTH
        if exc.code in (502, 503, 504):
            # nginx běží, brána za ním ne — pro člověka je to totéž co výpadek.
            return None, f"Server {_host(base)} teď neodpovídá ({exc.code}).", OFFLINE
        return None, note or f"Server odpověděl {exc.code}.", HTTP
    except (urllib.error.URLError, OSError) as exc:
        return None, _offline_reason(exc, base), OFFLINE
    except ValueError:
        return None, "Server neodpověděl tak, jak by brána Aladina měla.", HTTP
    except Exception as exc:
        return None, f"Nepovedlo se: {exc}", HTTP


def probe(server):
    """Je na adrese brána Aladina? Volá se před přihlášením.

    Vrací {"ok", "server", "host"} nebo {"error", "server"}.
    """
    base = normalize(server)
    if not base:
        return {"error": "Zadej adresu serveru.", "server": ""}
    out = {"server": base, "host": _host(base)}
    data, err, kind = _call("/gw/info", base=base, timeout=8)
    if data and data.get("app") == "claude-code-hub":
        return {**out, "ok": True, "version": data.get("version") or ""}
    if kind == OFFLINE:
        return {**out, "error": err}
    # Brána z doby před /gw/info na ni odpoví 401, protože ji nezná. Pozná se
    # tedy podle toho, jak odmítne cizí token — to umí od první verze.
    data, err, kind = _call("/gw/me", token="-", base=base, timeout=8)
    if kind == AUTH and err == "Neplatný token.":
        return {**out, "ok": True, "version": ""}
    return {**out, "error": f"Na {_host(base)} je něco jiného než server "
                             "Aladina. Zkontroluj adresu."}


def status(timeout=TIMEOUT):
    """Stav přihlášení. Nikdy nevyhodí výjimku — kreslí se z toho nastavení."""
    server = _base()
    token = core.CONFIG.get("gw_token") or ""
    cached = core.CONFIG.get("gw_user") or None
    base = {"server": server, "host": _host(server) if server else "",
            "server_mode": bool(core.CONFIG.get("server_mode"))}
    if not server or not token:
        out = {**base, "logged_in": False, "user": cached if server else None}
        if server and base["server_mode"]:
            # Token zahodil už launcher, když ho brána odmítla — appka se ale
            # pořád chce otevírat na serveru, tak ať je vidět proč se ptá.
            out["note"] = "Přihlášení na serveru skončilo, přihlas se znovu."
        return out

    data, err, kind = _call("/gw/me", token=token, timeout=timeout)
    if data and data.get("user"):
        user = data["user"]
        # Jméno nebo vault se mohly na serveru změnit — ať to nastavení ukazuje
        # to, co platí teď, ne co platilo při přihlášení.
        if user != cached:
            core.save_config({"gw_user": user})
        return {**base, "logged_in": True, "user": user}

    if kind == AUTH:
        # Token brána nezná (odhlášení z jiného zařízení, změna hesla) —
        # držet ho dál by jen mátlo. Uživatele si necháme: předvyplní e-mail.
        core.save_config({"gw_token": ""})
        return {**base, "logged_in": False, "user": cached, "expired": True,
                "note": "Přihlášení na serveru skončilo, přihlas se znovu."}

    return {**base, "logged_in": bool(cached), "user": cached,
            "offline": True, "note": err}


def login(server, email, password):
    base = normalize(server)
    if not base:
        return {"error": "Zadej adresu serveru."}
    data, err, kind = _call("/login", base=base,
                            payload={"email": (email or "").strip(),
                                     "password": password or ""})
    if data and data.get("need") in ("totp", "setup") and data.get("ticket"):
        # Heslo sedí, brána chce ještě kód z aplikace (nebo ho teprve nastavit).
        # Nic se neukládá, dokud neprojde i druhý krok (second_factor).
        out = {"need": data["need"], "ticket": str(data["ticket"]), "server": base}
        if data["need"] == "setup":
            from . import qr
            secret = str(data.get("secret") or "")
            link = str(data.get("uri") or "")
            if not secret or not link.startswith("otpauth://totp/"):
                return {"error": "Server poslal neplatné nastavení ověřování."}
            # QR se kreslí tady z odkazu, ne jako hotové SVG ze serveru — do
            # stránky se tak nedostane nic, co by server mohl podstrčit.
            out.update(secret=secret, qr=qr.svg(link, quiet=2, scale=5),
                       secret_grouped=" ".join(secret[i:i + 4]
                                               for i in range(0, len(secret), 4)))
        return out
    if not data or not data.get("token"):
        return {"error": err or "Přihlášení se nepovedlo."}
    # Adresa se ukládá až po úspěchu: nepovedený pokus nesmí přepsat server,
    # na kterém je člověk přihlášený.
    core.save_config({"gw_server": base, "gw_token": data["token"],
                      "gw_user": data.get("user")})
    return status()


def browser_start(server):
    """Přihlášení v prohlížeči (passkey, heslo + kód): appka založí na bráně
    nonce a vrátí adresu, kterou má člověk otevřít. Token si pak vyzvedne
    `browser_poll` — prohlížeč ho nikdy nevidí."""
    import socket
    base = normalize(server)
    if not base:
        return {"error": "Zadej adresu serveru."}
    data, err, _kind = _call("/gw/zarizeni", base=base, payload={"label": socket.gethostname()})
    if not data or not data.get("n"):
        return {"error": err or "Server přihlášení v prohlížeči neumí — aktualizuj ho."}
    return {"n": data["n"], "url": base + "/zarizeni?n=" + urllib.parse.quote(str(data["n"])), "server": base}


def browser_poll(server, nonce):
    base = normalize(server)
    if not base or not nonce:
        return {"error": "Přihlášení vypršelo, začni znovu."}
    data, err, kind = _call("/gw/zarizeni?n=" + urllib.parse.quote(str(nonce)), base=base)
    if kind == HTTP and err:
        return {"error": err}
    if not data:
        return {"error": err or "Server neodpovídá."}
    if data.get("stav") == "hotovo" and data.get("token"):
        core.save_config({"gw_server": base, "gw_token": data["token"], "gw_user": data.get("user")})
        return {"stav": "hotovo", **status()}
    return {"stav": "ceka"}


def second_factor(server, ticket, code):
    """Druhý krok přihlášení: kód z aplikace (nebo záložní), při prvním
    přihlášení zároveň zapnutí ověřování. Po úspěchu uloží token jako login()."""
    base = normalize(server)
    if not base or not ticket:
        return {"error": "Přihlášení vypršelo — zadej znovu heslo."}
    data, err, _kind = _call("/login/2fa", base=base,
                             payload={"ticket": str(ticket), "code": str(code or "")})
    if not data or not data.get("token"):
        return {"error": err or "Ověření se nepovedlo."}
    core.save_config({"gw_server": base, "gw_token": data["token"],
                      "gw_user": data.get("user")})
    out = status()
    if isinstance(data.get("recovery"), list):
        out["recovery"] = [str(c) for c in data["recovery"]][:20]
    return out


def logout():
    """Odhlásí i na serveru, ať token nezůstane platný po zbytek měsíce."""
    token = core.CONFIG.get("gw_token") or ""
    if token:
        base = _base()
        if base:
            try:
                req = urllib.request.Request(base + "/logout", method="GET")
                # `/logout` na bráně čte cookie, ne hlavičku — token jí tedy
                # podáme tak, jak ho čeká od prohlížeče.
                req.add_header("Cookie", "gw_session=" +
                               urllib.parse.quote(token))
                req.add_header("User-Agent", UA)
                urllib.request.urlopen(req, timeout=TIMEOUT).close()
            except Exception:
                pass  # token si stejně zahodíme; víc udělat nejde
    # Po odhlášení se appka spouští zase na počítači; adresa i e-mail zůstanou
    # zapamatované, ať se dá přihlásit zpátky bez vypisování.
    core.save_config({"gw_token": "", "server_mode": False})
    return status()


def handoff(timeout=TIMEOUT):
    """Adresa, po jejímž otevření je okno přihlášené na serveru.

    Vrací {"url"} nebo {"error", "kind"} — podle druhu appka pozná, jestli
    nabídnout „zkusit znovu", nebo přihlášení.
    """
    token = core.CONFIG.get("gw_token") or ""
    if not _base() or not token:
        return {"error": "Nejsi přihlášený.", "kind": AUTH}
    data, err, kind = _call("/gw/handoff", token=token, payload={},
                            timeout=timeout)
    if not data or not data.get("url"):
        if kind == AUTH:
            core.save_config({"gw_token": ""})
        return {"error": err or "Nepovedlo se připravit přihlášení.",
                "kind": kind or HTTP}
    return {"url": data["url"]}
