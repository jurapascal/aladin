#!/usr/bin/env python3
"""Návrh do firemního Obsidianu — pro Claude Code v prostoru na bráně.

    python3 firma.py navrh CÍL [SOUBOR | -] --potvrzeno   připraví poznámku ke schválení
    python3 firma.py seznam                                co čeká na potvrzení
    python3 firma.py kde                                   kde je firemní trezor
    python3 firma.py firmy                                 firmy, kde je uživatel členem

Další firmy (gateway/firmy.py): `--firma <zkratka>` u navrh a kde. Ve firemním
tabu firmy (HUB_VAULT=firma:<zkratka>) se firma bere z něj.

Nic nenahrává. Návrh uloží do ~/.firma/ke-schvaleni/ a hub ho uživateli ukáže
s náhledem a tlačítkem Nahrát. Firemní trezor je v prostoru jen ke čtení
a zapisuje do něj až brána po tom kliknutí (gateway/workspace.py,
publish_company).

Dvojí kontrola: bez `--potvrzeno` návrh nevznikne. Claude se má nejdřív
v chatu zeptat, co a kam nahraje, a přepínač přidat až po výslovném „ano" —
karta v hubu je pak druhé potvrzení.
"""
import json
import os
import re
import secrets
import sys
import time

HOME = os.path.expanduser("~")
PENDING = os.path.join(HOME, ".firma", "ke-schvaleni")
CLAUDE_DIR = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(HOME, ".claude")
MAX_TEXT = 512 * 1024


def fail(message):
    print(message, file=sys.stderr)
    sys.exit(1)


def config():
    try:
        with open(os.path.join(CLAUDE_DIR, "hub-config.json"), encoding="utf-8-sig") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def company_vault(firma=""):
    if firma:
        f = next((f for f in config().get("firmy") or []
                  if isinstance(f, dict) and f.get("zkratka") == firma), None)
        if not f or not f.get("cesta"):
            fail(f"Do firmy „{firma}“ tenhle účet nepatří (python3 firma.py firmy vypíše, do kterých ano).")
        return f["cesta"]
    vault = config().get("company_vault") or ""
    if not vault:
        fail("Firemní Obsidian tu není — je jen v prostorech na serveru (bráně).")
    return vault


def _firma_arg(args):
    """(firma, zbytek argumentů): `--firma X`, jinak z HUB_VAULT=firma:X."""
    firma, rest, i = "", [], 0
    while i < len(args):
        if args[i] == "--firma" and i + 1 < len(args):
            firma, i = args[i + 1], i + 2
            continue
        rest.append(args[i])
        i += 1
    tab = os.environ.get("HUB_VAULT", "")
    if not firma and tab.startswith("firma:"):
        firma = tab[len("firma:"):]
    return ("" if firma == "firma" else firma), rest


def target(rel):
    """Stejná pravidla jako brána (workspace.company_rel): relativní .md bez ..
    a skrytých částí. Chybu je lepší ukázat hned, ne až po kliknutí."""
    raw = str(rel or "").replace("\\", "/").strip()
    rel = raw.strip("/")
    if rel and not rel.lower().endswith(".md"):
        rel += ".md"
    parts = [p for p in rel.split("/") if p not in ("", ".")]
    if (not parts or len(rel) > 300 or raw.startswith("/") or re.match(r"^[A-Za-z]:", rel)
            or any(p == ".." or p.startswith(".") for p in parts)):
        fail(f"Neplatná cesta ve firemním Obsidianu: {rel or '(prázdná)'} — "
             "zadej relativní cestu, třeba postupy/fakturace.md.")
    return "/".join(parts)


def navrh(args):
    # Firemní tab v hubu (HUB_VAULT=firma): souhlas dal uživatel tím, že ho
    # otevřel — poznámka se nahraje rovnou, bez ptaní a bez karty.
    tab = os.environ.get("HUB_VAULT", "")
    firma, args = _firma_arg(args)
    # Firemní tab jiné firmy, než kam se má zapsat, souhlas nedává.
    auto = tab == ("firma:" + firma if firma else "firma")
    confirmed = auto or "--potvrzeno" in args
    args = [a for a in args if a != "--potvrzeno"]
    if not args:
        fail("Použití: firma.py navrh [--firma ZKRATKA] CÍL [SOUBOR | -] --potvrzeno")
    rel = target(args[0])
    vault = company_vault(firma)
    # Jen ke čtení: brána by návrh stejně odmítla, tak ať to Claude ví hned.
    role = next((f.get("role") for f in config().get("firmy") or []
                 if isinstance(f, dict) and f.get("zkratka") == (firma or "firma")), "")
    if config().get("company_level") == "read" and not firma or role == "host":
        fail("Tenhle účet má tenhle firemní Obsidian jen ke čtení — zapisovat do něj nejde. "
             "Řekni uživateli, ať požádá admina nebo generála firmy o právo zápisu.")
    if not confirmed:
        exists = os.path.exists(os.path.join(vault, *rel.split("/")))
        print("Ještě nic nevzniklo — nejdřív se zeptej uživatele. Napiš mu v chatu, co "
              f"přesně do firemního Obsidianu nahraješ (stručné shrnutí obsahu) a kam: {rel}"
              + (" — a že tím PŘEPÍŠEŠ existující poznámku" if exists else "")
              + (f" ve firmě „{firma}“. Uvidí to každý, kdo do té složky firmy vidí."
                 if firma else ". Firemní Obsidian uvidí celý tým.")
              + " Až to výslovně potvrdí, spusť příkaz znovu s --potvrzeno.", file=sys.stderr)
        sys.exit(3)
    source = args[1] if len(args) > 1 else "-"
    try:
        if source == "-":
            text = sys.stdin.read()
        else:
            with open(source, encoding="utf-8") as fh:
                text = fh.read()
    except OSError as exc:
        fail(f"Soubor s poznámkou nejde přečíst: {exc}")
    if not text.strip():
        fail("Poznámka je prázdná — nic se nenavrhlo.")
    if len(text.encode("utf-8")) > MAX_TEXT:
        fail("Poznámka je na firemní Obsidian moc velká (víc než 512 kB).")
    exists = os.path.exists(os.path.join(vault, *rel.split("/")))
    os.makedirs(PENDING, exist_ok=True)
    pid = time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(3)
    who = (config().get("gateway_user") or {}).get("email", "")
    data = {"cil": rel, "text": text, "autor": who,
            "vytvoreno": time.strftime("%Y-%m-%d %H:%M:%S")}
    if auto:
        data["auto"] = True
    if firma:
        data["firma"] = firma
    # Nejdřív dočasný soubor: hub čte jen hotové .json, rozepsaný návrh nevidí.
    tmp = os.path.join(PENDING, pid + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False)
    os.replace(tmp, os.path.join(PENDING, pid + ".json"))
    print(f"Návrh je připravený: {rel}" + (" (přepíše existující poznámku)" if exists else ""))
    if auto:
        print("Firemní tab: hub ji nahraje rovnou a napíše to dole v okně. "
              "Nahrávej jen to, o co uživatel požádal — osobní poznámky, hesla, "
              "klíče ani nastavení napojení do firemního Obsidianu nepatří.")
    else:
        print("Hub teď uživateli ukázal kartu s náhledem. Do firemního Obsidianu se "
              "poznámka nahraje, až ji potvrdí tlačítkem Nahrát.")


def seznam():
    try:
        names = sorted(n for n in os.listdir(PENDING) if n.endswith(".json"))
    except OSError:
        names = []
    if not names:
        print("Nic nečeká na potvrzení.")
        return
    for name in names:
        try:
            with open(os.path.join(PENDING, name), encoding="utf-8") as fh:
                data = json.load(fh)
            print(f"{data.get('vytvoreno', '')}  {data.get('cil', '')}")
        except (OSError, ValueError):
            continue


def main(argv):
    cmd = argv[0] if argv else ""
    if cmd == "navrh":
        navrh(argv[1:])
    elif cmd == "seznam":
        seznam()
    elif cmd == "kde":
        print(company_vault(_firma_arg(argv[1:])[0]))
    elif cmd == "firmy":
        for f in config().get("firmy") or []:
            if isinstance(f, dict):
                print(f"{f.get('zkratka', ''):<20} {f.get('nazev', ''):<28} "
                      f"{f.get('role', '')}  {f.get('cesta', '')}")
    else:
        print(__doc__.strip())
        sys.exit(0 if cmd in ("-h", "--help", "") else 2)


if __name__ == "__main__":
    main(sys.argv[1:])
