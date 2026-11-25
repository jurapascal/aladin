"""
Odchozí e-maily brány (zatím jen pozvánky z Nastavení → Účet → Členové).

Brána sama poštu neumí — posílá přes SMTP schránku, kterou správce serveru
nastaví jednou: `claude-hub-admin smtp set`. Údaje leží v `smtp.json` (0600,
čte jen uživatel hub); heslo se stránce ani do záznamu nikdy nedostane.
Bez nastavené schránky pozvánka nespadne: účet se založí a správce dostane
odkaz, který pošle sám.
"""
import json
import os
import re
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr, make_msgid

from . import config

SMTP_FILE = os.environ.get("HUB_GW_SMTP_FILE", os.path.join(config.GATEWAY_DIR, "smtp.json"))
TIMEOUT = 20


def nastaveni():
    """{host, port, user, password, from, name} nebo None."""
    try:
        with open(SMTP_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not data.get("host") or not data.get("from"):
        return None
    return data


def verejne():
    """Co smí vidět stránka a výpis: odkud se posílá, bez hesla."""
    s = nastaveni()
    if not s:
        return None
    return {"host": s["host"], "port": int(s.get("port") or 587), "od": s["from"],
            "jmeno": s.get("name") or "", "user": s.get("user") or "",
            "heslo": bool(s.get("password"))}


def uloz(host, port, user, password, odesilatel, jmeno=""):
    host = (host or "").strip()
    odesilatel = (odesilatel or "").strip()
    if not host or "@" not in odesilatel:
        raise ValueError("Vyplň SMTP server a adresu odesílatele.")
    if not re.fullmatch(r"[A-Za-z0-9.-]{1,253}", host):
        raise ValueError("SMTP server má být jen adresa, třeba smtp.firma.cz.")
    if re.search(r"[\r\n<>]", odesilatel + (jmeno or "") + (user or "")) or len(odesilatel) > 200:
        raise ValueError("Adresa odesílatele vypadá divně.")
    try:
        port = int(port or 587)
    except (TypeError, ValueError):
        raise ValueError("Port má být číslo (obvykle 587 nebo 465).") from None
    if not 0 < port < 65536:
        raise ValueError("Port má být číslo (obvykle 587 nebo 465).")
    data = {"host": host, "port": port, "user": (user or "").strip(),
            "password": password or "", "from": odesilatel, "name": (jmeno or "").strip()}
    os.makedirs(os.path.dirname(SMTP_FILE), exist_ok=True)
    tmp = SMTP_FILE + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False)
    os.replace(tmp, SMTP_FILE)
    return data


def smaz():
    try:
        os.remove(SMTP_FILE)
        return True
    except FileNotFoundError:
        return False


def poslat(komu, predmet, text, html_text=None, odpovedet=None):
    """Pošle jeden e-mail. Vyhodí RuntimeError s lidskou hláškou, když to nejde."""
    s = nastaveni()
    if not s:
        raise RuntimeError("Server nemá nastavené odesílání e-mailů.")
    msg = EmailMessage()
    msg["Subject"] = predmet
    msg["From"] = formataddr((s.get("name") or "Aladin", s["from"]))
    msg["To"] = komu
    if odpovedet:
        msg["Reply-To"] = odpovedet
    msg["Message-ID"] = make_msgid(domain=s["from"].split("@")[-1])
    msg.set_content(text)
    if html_text:
        msg.add_alternative(html_text, subtype="html")
    port = int(s.get("port") or 587)
    ctx = ssl.create_default_context()
    try:
        if port == 465:
            srv = smtplib.SMTP_SSL(s["host"], port, timeout=TIMEOUT, context=ctx)
        else:
            srv = smtplib.SMTP(s["host"], port, timeout=TIMEOUT)
        with srv:
            if port != 465:
                srv.ehlo()
                if srv.has_extn("starttls"):
                    srv.starttls(context=ctx)
                    srv.ehlo()
            if s.get("user"):
                srv.login(s["user"], s.get("password") or "")
            srv.send_message(msg)
    except smtplib.SMTPAuthenticationError:
        raise RuntimeError("Schránka pro odesílání odmítla přihlášení (špatné heslo?).") from None
    except smtplib.SMTPRecipientsRefused:
        raise RuntimeError(f"Na adresu {komu} se e-mail poslat nedá.") from None
    except (smtplib.SMTPException, OSError) as exc:
        raise RuntimeError(f"E-mail se nepovedlo odeslat ({exc.__class__.__name__}).") from None
