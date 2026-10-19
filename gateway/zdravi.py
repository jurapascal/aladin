#!/usr/bin/env python3
"""
Hlídač zdraví brány — každou hodinu (claude-hub-zdravi.timer, běží jako root).

Dřív se o potížích na bráně správce dozvídal od lidí („padá mi to") nebo
vůbec: záloha pět dní nevznikla kvůli plnému disku a nikdo o tom nevěděl.
Tohle zkontroluje, co se dá zjistit bez lidí, a když je něco špatně, pošle
hlášení do portálu Verxee Debug (stejný klíč jako tlačítko Nahlásit chybu
v prostorech) — tam ho vidí správce a zpracuje skill /chyby.

    disk      volné místo na disku
    paměť     dostupná paměť stroje
    záloha    poslední záloha starší než 36 h (nebo chyba v jejím logu)
    brána     služba claude-hub-gateway neběží
    paměť:X   jádro v prostoru X za den zabilo Clauda kvůli paměti
    zamrzá:X  stránka v prostoru X za den zamrzla (hlídač v hub.js)
    chyby:X   stránka v prostoru X za den nahlásila stovky chyb

Stejný problém se znovu hlásí nejdřív za 24 h, ať hlídač neotravuje.

    claude-hub-zdravi              zkontroluje a případně nahlásí
    claude-hub-zdravi --vypis      jen vypíše, nic neposílá

Jen standardní knihovna.
"""
import datetime
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request

CONF = "/etc/claude-hub/install.conf"
STAV = "/var/lib/claude-hub/zdravi.json"
USERS = "/home/hub/users"
ZALOHY = "/var/backups/claude-hub"
ZALOHA_LOG = "/var/log/claude-hub-zaloha.log"
KLIC_FILE = "/home/hub/gateway/verxee-debug-klic"
KLIC_VYCHOZI = "vxd_acc213c5649c28723f438849"
API = "https://alba-rosa.cz/debug/api.php?a=nahlasit"

DISK_MIN_GB = 8            # pod tolik volného místa (nebo pod 10 %) je to problém
PAMET_MIN = 0.10           # dostupná paměť pod 10 % celkové
ZALOHA_MAX_H = 36
OOM_MIN = 3                # zabití za den v jednom prostoru
ZAMRZA_MIN = 3             # zamrznutí stránky za den v jednom prostoru
CHYBY_MIN = 300            # chyb stránky za den v jednom prostoru
ZNOVU_PO = 24 * 3600


def _conf():
    out = {}
    try:
        with open(CONF, encoding="utf-8") as fh:
            for radek in fh:
                m = re.match(r"^([A-Z_]+)=['\"]?([^'\"\n]*)", radek)
                if m:
                    out[m.group(1)] = m.group(2)
    except OSError:
        pass
    return out


def _klic():
    """Stejně jako gateway/workspace.py verxee_klic(): soubor správce, jinak výchozí."""
    try:
        with open(KLIC_FILE, encoding="ascii") as fh:
            klic = fh.read().strip()
    except OSError:
        klic = ""
    return klic if re.fullmatch(r"vxd_[a-f0-9]{24}", klic) else KLIC_VYCHOZI


def disk():
    u = shutil.disk_usage("/")
    volno_gb = u.free / 1024 ** 3
    if volno_gb < DISK_MIN_GB or u.free / u.total < 0.10:
        return [("disk", f"Na disku brány zbývá {volno_gb:.1f} GB ({u.free / u.total:.0%}). "
                         "Až dojde, prostorům spadne práce a záloha se neudělá. "
                         "Uvolnit: cache (`uv cache prune`, `npm cache clean --force` v domovech "
                         "/home/hub/users/*), staré zálohy, velká data v prostorech (`du -xsh /home/hub/users/*`).")]
    return []


def pamet():
    info = {}
    try:
        with open("/proc/meminfo", encoding="ascii") as fh:
            for radek in fh:
                k, v = radek.split(":", 1)
                info[k] = int(v.split()[0])
    except (OSError, ValueError):
        return []
    celkem, dostupna = info.get("MemTotal", 0), info.get("MemAvailable", 0)
    if celkem and dostupna / celkem < PAMET_MIN:
        return [("pamet", f"Stroji zbývá {dostupna // 1024} MB dostupné paměti z {celkem // 1024} MB.")]
    return []


def zaloha():
    soubory = glob.glob(os.path.join(ZALOHY, "hub-*.tar.zst.gpg"))
    posledni = max((os.path.getmtime(f) for f in soubory), default=0)
    if time.time() - posledni < ZALOHA_MAX_H * 3600:
        return []
    duvod = ""
    try:
        with open(ZALOHA_LOG, encoding="utf-8", errors="replace") as fh:
            radky = [r.strip() for r in fh if "CHYBA" in r]
        duvod = radky[-1] if radky else ""
    except OSError:
        pass
    kdy = datetime.datetime.fromtimestamp(posledni).strftime("%d. %m. %H:%M") if posledni else "žádná"
    return [("zaloha", f"Poslední záloha brány: {kdy}." + (f" Poslední chyba: {duvod}" if duvod else ""))]


def brana():
    try:
        r = subprocess.run(["systemctl", "--user", "-M", "hub@", "is-active", "claude-hub-gateway"],
                           capture_output=True, text=True, timeout=20)
        stav = r.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return []
    if stav and stav != "active":
        return [("brana", f"Služba claude-hub-gateway je ve stavu „{stav}\" — prostory se nedají otevřít.")]
    return []


def oom():
    """Zabití kvůli paměti za den, po prostorech (scope claude-hub-<e-mail>)."""
    try:
        r = subprocess.run(["journalctl", "-k", "--since", "-24h", "-o", "cat", "--no-pager"],
                           capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return []
    pocty = {}
    for m in re.finditer(r"oom-kill:.*?oom_memcg=\S*?claude-hub-([^/,\s]+?)\.scope", r.stdout):
        pocty[m.group(1)] = pocty.get(m.group(1), 0) + 1
    out = []
    for kdo, n in sorted(pocty.items()):
        if n >= OOM_MIN:
            out.append((f"oom:{kdo}", f"Prostor {kdo}: jádro za den {n}× zabilo proces kvůli paměti "
                                      "(strop prostoru). Lidem se zavírají taby — obvykle hodně tabů naráz "
                                      "nebo skript, který pouští `claude -p` po desítkách."))
    return out


def stranky():
    """Zamrznutí a chyby stránky za den z hub.log každého prostoru."""
    od = (datetime.datetime.now() - datetime.timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
    out = []
    for domov in sorted(glob.glob(os.path.join(USERS, "*"))):
        kdo = os.path.basename(domov)
        zamrzlo = chyb = 0
        for log in (os.path.join(domov, ".claude", "hub.log.1"), os.path.join(domov, ".claude", "hub.log")):
            try:
                with open(log, encoding="utf-8", errors="replace") as fh:
                    for radek in fh:
                        if radek[:19] < od or "stránka:" not in radek:
                            continue
                        if "stránka neodpovídá" in radek:
                            zamrzlo += 1
                        elif " ERROR " in radek[19:27]:
                            chyb += 1
            except OSError:
                continue
        if zamrzlo >= ZAMRZA_MIN:
            out.append((f"zamrza:{kdo}", f"Prostor {kdo}: stránka za den {zamrzlo}× přestala odpovídat "
                                         f"(podrobnosti v {domov}/.claude/hub.log, řádky „stránka neodpovídá\")."))
        if chyb >= CHYBY_MIN:
            out.append((f"chyby:{kdo}", f"Prostor {kdo}: stránka za den nahlásila {chyb} chyb "
                                        f"(grep \"ERROR stránka\" {domov}/.claude/hub.log)."))
    return out


def zkontroluj():
    problemy = []
    for kontrola in (disk, pamet, zaloha, brana, oom, stranky):
        try:
            problemy += kontrola()
        except Exception as exc:           # jedna rozbitá kontrola nesmí shodit ostatní
            problemy.append((f"hlidac:{kontrola.__name__}", f"Kontrola {kontrola.__name__} spadla: {exc}"))
    return problemy


def nahlas(problemy, nove, domena):
    klic = _klic()
    popis = ("Hlídač zdraví brány našel problém:\n\n" +
             "\n".join(f"• {text}" for k, text in problemy if k in nove) +
             ("\n\nTrvá dál (už hlášeno):\n" + "\n".join(f"• {text}" for k, text in problemy if k not in nove)
              if len(nove) < len(problemy) else "") +
             "\n\nRučně: claude-hub-zdravi --vypis")
    adresa = f"https://{domena}/"
    data = {"druh": "chyba", "typ": "pomalost" if any(k.startswith(("zamrza", "oom", "pamet")) for k in nove) else "jine",
            "priorita": "blokuje" if any(k in ("disk", "brana") for k in nove) else "vadi",
            "titulek": "Brána: " + ", ".join(k.split(":")[0] for k in nove),
            "popis": popis[:4900], "url": adresa, "kde": "administrace",
            "uzivatel": {"jmeno": "Hlídač brány"}}
    req = urllib.request.Request(API, data=urllib.parse.urlencode({"data": json.dumps(data, ensure_ascii=False)}).encode(),
                                 headers={"X-Verxee-Chyby": "1", "X-Debug-Klic": klic, "X-Debug-Web": "claude-code-hub",
                                          "Origin": adresa.rstrip("/"), "Referer": adresa,
                                          "User-Agent": "claude-hub-zdravi"})
    try:
        with urllib.request.urlopen(req, timeout=30) as res:
            odp = json.loads(res.read().decode("utf-8", "replace") or "{}")
        return bool(odp.get("ok")), odp.get("chyba", "")
    except Exception as exc:
        return False, str(exc)


def main():
    problemy = zkontroluj()
    if "--vypis" in sys.argv:
        for k, text in problemy:
            print(f"[{k}] {text}")
        if not problemy:
            print("Brána je v pořádku.")
        return 0
    try:
        with open(STAV, encoding="utf-8") as fh:
            stav = json.load(fh)
    except (OSError, ValueError):
        stav = {}
    ted = time.time()
    nove = [k for k, _ in problemy if ted - float(stav.get(k, 0)) > ZNOVU_PO]
    # Vyřešené se zapomenou — když se vrátí, ohlásí se hned.
    stav = {k: v for k, v in stav.items() if k in {k for k, _ in problemy}}
    if nove:
        ok, proc = nahlas(problemy, nove, _conf().get("DOMAIN") or "localhost")
        if ok:
            for k in nove:
                stav[k] = ted
        else:
            print(f"claude-hub-zdravi: hlášení se neodeslalo ({proc})", file=sys.stderr)
    os.makedirs(os.path.dirname(STAV), exist_ok=True)
    with open(STAV + ".tmp", "w", encoding="utf-8") as fh:
        json.dump(stav, fh)
    os.replace(STAV + ".tmp", STAV)
    for k, text in problemy:
        print(f"[{k}] {text}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
