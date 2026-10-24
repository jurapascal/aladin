#!/usr/bin/env python3
"""
Správa účtů brány z příkazové řádky.

Běží na serveru pod uživatelem `hub`. První účet je vždycky admin — bez něj by
se do webové správy nedalo dostat. Heslo se buď zadá skrytě (getpass), nebo
přes `--password` (hodí se do skriptu, ale zůstane v historii shellu).

    python3 -m gateway.admin add jmeno@firma.cz --name "Jméno" --role admin
    python3 -m gateway.admin list
    python3 -m gateway.admin passwd jmeno@firma.cz
    python3 -m gateway.admin role jmeno@firma.cz user
    python3 -m gateway.admin jmeno jmeno@firma.cz "Jméno Příjmení"
    python3 -m gateway.admin vault jmeno@firma.cz "Jméno Brain"
    python3 -m gateway.admin disable jmeno@firma.cz
    python3 -m gateway.admin enable jmeno@firma.cz
    python3 -m gateway.admin remove jmeno@firma.cz
    python3 -m gateway.admin spotreba            # kdo kolik čerpá za týden
    python3 -m gateway.admin spotreba jmeno@firma.cz --limit 150   # týdenní strop v USD
    python3 -m gateway.admin sessions            # které prostory běží
    python3 -m gateway.admin stop jmeno@firma.cz # zastavit prostor
    python3 -m gateway.admin stop --orphans      # zastavit osiřelé
    python3 -m gateway.admin auth jmeno@firma.cz central   # klíč API / own
    python3 -m gateway.admin auth --vsechny central
    python3 -m gateway.admin apikey set          # společný klíč pro prostory
    python3 -m gateway.admin apikey set --user jmeno@firma.cz   # klíč jen jeho
    python3 -m gateway.admin apikey status | remove
    python3 -m gateway.admin skills install      # firemní skilly do trezoru
    python3 -m gateway.admin skills status | update
    python3 -m gateway.admin google set          # klient OAuth pro Google (jednou)
    python3 -m gateway.admin predplatne status   # kdo jede na vlastním předplatném Claude
    python3 -m gateway.admin predplatne set jmeno@firma.cz    # token z `claude setup-token`
    python3 -m gateway.admin predplatne remove jmeno@firma.cz
    python3 -m gateway.admin google status | remove
    python3 -m gateway.admin web                  # kdo má přidělený web (FTP)
    python3 -m gateway.admin web jmeno@firma.cz --host ftp.firma.cz --user ftp_jmeno \
        --url https://firma.cz/jmeno [--dir /] [--port 21] [--password-stdin]
    python3 -m gateway.admin web jmeno@firma.cz --remove
    python3 -m gateway.admin zamky               # zablokovaná přihlášení (3 špatné pokusy)
    python3 -m gateway.admin zamky odemknout jmeno@firma.cz   # nebo IP adresa
    python3 -m gateway.admin firma                # kdo vidí firemní Obsidian a kdo zapisuje
    python3 -m gateway.admin firma jmeno@firma.cz read     # none / read / write
    python3 -m gateway.admin mcp                  # napojení z appky Claude
    python3 -m gateway.admin mcp zrusit jmeno@firma.cz     # zrušit jeho napojení
"""
import argparse
import getpass
import json
import os
import shutil
import subprocess
import sys
import time

from . import config, firmy, isolation, mcp_sdilene, poznamky, relace, shared, slozky, workspace
from .accounts import Accounts


def _accounts():
    accounts = Accounts(config.DB_PATH)
    shared.ACCOUNTS = accounts          # jména členů sdílených Obsidianů
    return accounts


def _ask_password(given):
    if given:
        return given
    first = getpass.getpass("Heslo (min. 10 znaků): ")
    again = getpass.getpass("Heslo znovu: ")
    if first != again:
        sys.exit("Hesla se neshodují.")
    return first


def cmd_add(a, args):
    pw = _ask_password(args.password)
    uid = a.add(args.email, pw, name=args.name or "", role=args.role,
                vault=args.vault or "")
    # Domov a vault připravíme rovnou, ať první přihlášení nic nezdržuje —
    # rovnou pod jménem podle e-mailu (prostor ještě nikdy neběžel), jinak by
    # ensure založil starý u<id> a přejmenovával se až při přihlášení.
    user = a.get(args.email)
    workspace.migrate_home(user)
    workspace.ensure(user)
    print(f"Založen účet #{uid}: {args.email} ({args.role})")


def cmd_list(a, args):
    rows = a.list()
    if not rows:
        print("Zatím žádné účty.")
        return
    w = max(len(r["email"]) for r in rows)
    print(f'{"e-mail":<{w}}  role   claude    2fa  vault              stav     web')
    for r in rows:
        stav = "vypnutý" if r["disabled"] else "aktivní"
        auth = r.get("claude_auth") or "central"
        twofa = "ano" if r.get("twofa") else "ne"
        web = workspace.web_public(r)
        print(f'{r["email"]:<{w}}  {r["role"]:<5}  {auth:<8}  {twofa:<3}  '
              f'{(r["vault"] or "-"):<18} {stav:<8} {web["url"] if web else "-"}')


def cmd_spotreba(a, args):
    from . import spotreba
    if args.limit is not None:
        if not args.email:
            raise ValueError("Ke stropu patří e-mail: spotreba <e-mail> --limit 150")
        usd = spotreba.set_limit(args.email, args.limit)
        print(f"{args.email}: týdenní strop " + (f"{usd:g} USD" if usd else "zrušen"))
        return
    print("Počítám spotřebu za posledních 7 dní…", flush=True)
    data = spotreba.prehled(a.list(), args.email or None, wait=600)
    lide = data["lide"]
    if not lide:
        print("Žádní lidé.")
        return
    w = max(len(r["email"]) for r in lide)
    print(f'{"e-mail":<{w}}  7 dní $   dnes $   podíl  strop $   přes limit  stav')
    znacka = {"nad": "NAD STROPEM", "blizko": "u stropu", "ok": "", "": ""}
    for r in lide:
        strop = f'{r["limit"]:g}' if r["limit"] else "-"
        print(f'{r["email"]:<{w}}  {r["tyden"]:>7.2f}  {r["dnes"]:>7.2f}  '
              f'{r["podil"] * 100:>4.0f} %  {strop:>7}   {str(r["preslo"]) + "×":>8}   {znacka[r["stav"]]}')
    print(f'{"celkem":<{w}}  {data["soucet"]:>7.2f}   (odhad podle ceníku API, na předplatném se neplatí)')


def cmd_sdilene(a, args):
    """Sdílené Obsidiany: název, složka, zakladatel a členové."""
    vaults = shared.all_vaults()
    if not vaults:
        print("Zatím žádný sdílený Obsidian.")
        return
    for v in vaults:
        print(f'{v["name"]}  ({v["path"]})')
        print(f'  založil: {v["owner"] or "(účet smazaný — spravuje správce)"}')
        print("  členové: " + (", ".join(m["email"] for m in v["members"]) or "nikdo"))


def cmd_2fa(a, args):
    """Dvoufázové ověření: stav, nebo reset (ztracený telefon i záložní kódy)."""
    if args.action == "status":
        email = (args.email or "").strip().lower()
        rows = [r for r in a.list() if not email or r["email"] == email]
        if not rows:
            raise ValueError(f"{args.email} tu žádný účet nemá." if email else "Zatím žádné účty.")
        w = max(len(r["email"]) for r in rows)
        for r in rows:
            st = a.twofa(r["id"])
            print(f'{r["email"]:<{w}}  ' + (
                f'zapnuté, záložních kódů zbývá {st["recovery_left"]}' if st["enabled"]
                else "nenastavené — nastaví si ho při příštím přihlášení"))
        return
    if not args.email:
        raise ValueError("Zadej e-mail účtu.")
    a.reset_totp(args.email)
    print(f"{args.email}: dvoufázové ověření zrušené a všechna zařízení odhlášená. "
          "Při příštím přihlášení si ho nastaví znovu.")


def cmd_zamky(a, args):
    """Zablokovaná přihlášení (tři špatné pokusy): výpis, nebo odemknutí."""
    if args.action == "odemknout":
        count = a.fail_unlock(args.target)
        print(f"{args.target}: odemčeno ({count} záznamů)." if count
              else f"{args.target}: nic zamčeného ani počítaného tu není.")
        return
    rows = a.fail_list()
    locks = [r for r in rows if r["locked"]]
    now = time.time()
    if not locks:
        print("Nic není zablokované.")
    else:
        w = max(len(r["key"]) for r in locks)
        for r in locks:
            print(f'{r["key"]:<{w}}  zamčeno ještě {max(1, int(r["locked"] - now) // 60)} min')
    counting = len(rows) - len(locks)
    if counting:
        print(f"Počítané nepovedené pokusy bez zámku: {counting} (po hodině se zapomenou).")


def cmd_passwd(a, args):
    a.set_password(args.email, _ask_password(args.password))
    print(f"Heslo pro {args.email} změněno (a všechna zařízení odhlášena).")


def cmd_jmeno(a, args):
    a.set_name(args.email, args.jmeno)
    print(f"{args.email} se teď jmenuje „{' '.join(args.jmeno.split())}“. "
          "V hubu se to ukáže po dalším startu jeho prostoru.")


def cmd_role(a, args):
    a.set_role(args.email, args.role)
    print(f"{args.email} má teď roli {args.role}.")


def cmd_vault(a, args):
    a.set_vault(args.email, args.vault)
    print(f"{args.email} má vault „{args.vault}“.")


def cmd_auth(a, args):
    kde = "klíč API" if args.mode == "central" else "vlastní účet"
    if args.vsechny:
        if args.email:
            raise ValueError("Zadej buď e-mail, nebo --vsechny, ne obojí.")
        rows = a.list()
        for r in rows:
            a.set_auth(r["email"], args.mode)
        print(f"Přepnuto na {kde}: účtů {len(rows)}.")
    elif not args.email:
        raise ValueError("Zadej e-mail účtu, nebo --vsechny.")
    else:
        a.set_auth(args.email, args.mode)
        print(f"{args.email}: Claude se teď ověřuje přes {kde}.")
    print("Projeví se při dalším startu prostoru — běžící zastavíš:"
          " stop <e-mail>.")


def _check_key(key):
    """Ověří klíč u API — seznam modelů nic nestojí. Vrací důvod odmítnutí,
    nebo ''. Bez spojení se klíč uloží i tak; ověří se až v prostoru."""
    import urllib.error
    import urllib.request
    req = urllib.request.Request("https://api.anthropic.com/v1/models", headers={
        "x-api-key": key, "anthropic-version": "2023-06-01"})
    try:
        with urllib.request.urlopen(req, timeout=10):
            return ""
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            return "Anthropic klíč odmítl (neplatný nebo zrušený)."
        return ""
    except Exception:
        print("Klíč se nepodařilo ověřit (bez spojení s API) — ukládám ho i tak.")
        return ""


def _user_or_die(a, email):
    user = a.get(email)
    if not user:
        raise ValueError(f"{email} tu žádný účet nemá.")
    return user


def _key_path(user=None):
    """Kde leží klíč: společný, nebo vlastní podle čísla účtu (e-mail se do
    cesty nedostane)."""
    return (os.path.join(config.API_KEYS_DIR, str(user["id"])) if user
            else config.API_KEY_FILE)


def _key_info(path):
    """(posledních 5 znaků, kdy uložen), nebo ('', '') když tam klíč není."""
    try:
        with open(path, encoding="utf-8") as fh:
            key = fh.read().strip()
        if not key:
            return "", ""
        when = time.strftime("%d. %m. %Y %H:%M",
                             time.localtime(os.path.getmtime(path)))
        return key[-5:], when
    except OSError:
        return "", ""


def _save_key(path, key):
    """Klíč umí přečíst jen uživatel hub (0600 v 0700 složce) — v prostoru je
    pak vidět jedině v prostředí toho, komu patří."""
    parent = os.path.dirname(path)
    os.makedirs(parent, exist_ok=True)
    if parent == config.API_KEYS_DIR:
        os.chmod(parent, 0o700)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(key + "\n")
    os.replace(tmp, path)


def cmd_apikey(a, args):
    """Klíč API pro prostory na `central`. Leží jen u brány (0600, uživatel
    hub) a do prostoru přijde jako ANTHROPIC_API_KEY při jeho startu.

    Bez `--user` je to společný klíč pro každého, kdo svůj nemá. S `--user`
    klíč jednoho účtu: ten pak má v Anthropic Console vlastní strop a kdo si
    klíč v prostoru přečte z prostředí, přečte jen ten svůj."""
    user = _user_or_die(a, args.user) if args.user else None
    path = _key_path(user)
    kdo = user["email"] if user else "Společný klíč"
    if args.action == "status":
        tail, when = _key_info(path)
        if user:
            print(f"{kdo}: " + (f"vlastní klíč …{tail} (uložen {when})" if tail
                                else "vlastní klíč nemá — jede na společném"))
            return
        print(f"Společný klíč: " + (f"…{tail}, uložen {when}" if tail
                                    else "není nastavený"))
        rows = [u for u in a.list()
                if (u.get("claude_auth") or "central") == "central"]
        svoje = 0
        for u in rows:
            t, w = _key_info(_key_path(u))
            svoje += bool(t)
            print(f"  {u['email']:<28} " + (f"vlastní klíč …{t} ({w})" if t
                                            else "společný klíč"))
        print(f"Účtů na klíči API: {len(rows)} (z toho na vlastním: {svoje});"
              f" na vlastním účtu Claude: {len(a.list()) - len(rows)}.")
        if not tail and svoje < len(rows):
            print("Pozor: kdo je na společném klíči a ten není nastavený,"
                  " se v prostoru musí přihlásit sám.")
        return
    if args.action == "remove":
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        print((f"{kdo}: vlastní klíč smazán — od dalšího startu prostoru jede"
               " na společném." if user else
               "Společný klíč smazán. Běžící prostory ho mají do svého"
               " dalšího startu."))
        return
    key = (getpass.getpass(f"Klíč API pro {kdo} (sk-ant-…): ")
           if sys.stdin.isatty() else sys.stdin.readline()).strip()
    if not key.startswith("sk-ant-"):
        raise ValueError("Tohle nevypadá jako klíč API Anthropicu (začíná sk-ant-).")
    problem = _check_key(key)
    if problem:
        raise ValueError(problem)
    _save_key(path, key)
    print(f"{kdo}: klíč uložen (…{key[-5:]}). Platí od dalšího startu prostoru;"
          " běžící zastavíš: stop <e-mail>.")


SKILLS_REPO = os.environ.get("HUB_SKILLS_REPO", "jurapascal/aladin-marketplace")


def _skill_count(root):
    return sum(1 for _d, _s, files in os.walk(root) if "SKILL.md" in files)


def _git(*args):
    """(povedlo se, výstup). Bez shellu — cesty i adresa repozitáře jdou
    argumentem."""
    try:
        done = subprocess.run(("git",) + args, capture_output=True, text=True,
                              timeout=600)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    return done.returncode == 0, (done.stdout + done.stderr).strip()


def _move_aside(root):
    """Stávající skilly odloží vedle trezoru (ne do něj — v Obsidianu by se
    ukázaly dvakrát). Vrací, kam."""
    away = os.path.join(config.COMPANY_DIR,
                        "_skilly-" + time.strftime("%Y%m%d-%H%M%S"))
    shutil.move(root, away)
    os.makedirs(root, exist_ok=True)
    return away


def cmd_skills(a, args):
    """Firemní skilly — `<firemní trezor>/skills`.

    Leží v trezoru, takže je má každý prostor jen ke čtení jako zbytek trezoru
    a nikdo je nemá zvlášť u sebe. Claude o nich ví z pokynů, které brána píše
    do `~/.claude/CLAUDE.md` prostoru při jeho startu (workspace._company_block)
    — nové skilly se tam objeví po dalším startu prostoru."""
    workspace.ensure_company_vault()
    root = config.COMPANY_SKILLS
    clone = os.path.isdir(os.path.join(root, ".git"))
    obsah = [n for n in os.listdir(root) if not n.startswith(".")]

    if args.action == "status":
        print(f"Firemní skilly: {root}")
        if not obsah:
            print("Zatím tam žádné nejsou — zavedeš je:"
                  " claude-hub-admin skills install")
            return
        cats = workspace.company_skill_categories()
        print(f"skillů: {_skill_count(root)}, kategorií: {len(cats)}")
        if cats:
            print("  " + ", ".join(cats))
        if clone:
            ok, out = _git("-C", root, "log", "-1", "--format=%cd %s",
                           "--date=short")
            src = _git("-C", root, "remote", "get-url", "origin")[1]
            print(f"z gitu: {src}" + (f" ({out})" if ok else ""))
            print("aktualizace: claude-hub-admin skills update")
        else:
            print("nejsou z gitu — aktualizuješ je znovu přes"
                  " skills install --from <cesta>")
        return

    if args.action == "update":
        if not clone:
            raise ValueError("Skilly nejsou z gitu — nahraj novou verzi:"
                             " skills install --from <cesta> --force")
        ok, out = _git("-C", root, "pull", "--ff-only")
        if not ok:
            raise ValueError(f"Aktualizace nevyšla: {out or 'git mlčí'}")
        print(f"Skilly aktuální: {_skill_count(root)}."
              " Prostory je uvidí po svém dalším startu.")
        return

    # install
    if obsah and not args.force:
        raise ValueError(f"Ve {root} už skilly jsou ({_skill_count(root)})."
                         " Aktualizuj je: skills update — nebo přepiš:"
                         " skills install --force")
    away = _move_aside(root) if obsah else ""
    if args.src:
        src = os.path.abspath(os.path.expanduser(args.src))
        if not _skill_count(src):
            raise ValueError(f"V {src} žádný SKILL.md není.")
        shutil.copytree(src, root, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns(".git"))
        odkud = src
    else:
        ok, out = _git("clone", "--quiet", "--depth", "1",
                       f"https://github.com/{SKILLS_REPO}.git", root)
        if not ok:
            raise ValueError(f"Stažení nevyšlo: {out or 'git mlčí'}")
        odkud = SKILLS_REPO
    cats = workspace.company_skill_categories()
    print(f"Firemní skilly z {odkud}: {_skill_count(root)} ve {len(cats)}"
          f" kategoriích → {root}")
    if away:
        print(f"Předchozí odloženy do {away} (smaž je, až to prověříš).")
    print("Prostory je uvidí po svém dalším startu — hned to bude:"
          " claude-hub-admin stop <e-mail>.")


def cmd_google(a, args):
    """Klient OAuth pro napojení na Google (Nastavení → Napojení v prostorech).

    Zakládá se jednou pro všechny v Google Cloudu (typ Desktopová aplikace,
    aplikace zveřejněná — v testovacím režimu Google přihlášení po 7 dnech
    zruší). Lidé si pak v prostoru přidávají vlastní Google účty."""
    path = config.GOOGLE_CLIENT_FILE
    if args.action == "status":
        cid, _ = workspace.google_client()
        if cid:
            when = time.strftime("%d. %m. %Y %H:%M", time.localtime(os.path.getmtime(path)))
            print(f"Klient Google: {cid} (uložen {when}).")
        else:
            print("Klient Google není nastavený — Google v prostorech čeká na správce.")
        return
    if args.action == "remove":
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        print("Klient Google smazán. Napojené účty přestanou fungovat po dalším startu prostoru.")
        return
    tty = sys.stdin.isatty()
    cid = (input("Client ID: ") if tty else sys.stdin.readline()).strip()
    secret = (getpass.getpass("Client secret: ") if tty else sys.stdin.readline()).strip()
    if not cid.endswith(".apps.googleusercontent.com"):
        raise ValueError("Client ID končí na .apps.googleusercontent.com — zkontroluj, co jsi vložil.")
    if not secret:
        raise ValueError("Chybí client secret.")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump({"client_id": cid, "client_secret": secret}, fh)
    os.replace(tmp, path)
    print("Klient Google uložen. Platí od dalšího startu prostoru; lidé si pak "
          "v Nastavení → Napojení přidají svoje Google účty.")


def cmd_predplatne(a, args):
    """Vlastní předplatné Claude v prostorech. Běžně ho připojí appka na počítači
    sama (`claude setup-token` u člověka); tohle je pro přehled a pro případ,
    kdy appku nemá — token si vyrobí kdekoli a správce ho vloží."""
    if args.action == "status":
        rows = [_user_or_die(a, args.email)] if args.email else a.list()
        names = {"predplatne": "vlastní předplatné", "api": "klíč API",
                 "ucet": "přihlášený v prostoru", "zadne": "nepřihlášený"}
        for u in rows:
            st = workspace.claude_state(u)
            extra = ""
            if st["mode"] == "predplatne" and st.get("expires"):
                extra = " (platí do " + time.strftime("%d. %m. %Y", time.localtime(st["expires"])) + \
                    (", brzy vyprší — připojit znovu" if st.get("expiring") else "") + ")"
            print(f"  {u['email']:<32} {names[st['mode']]}{extra}")
        return
    if not args.email:
        raise ValueError("Zadej e-mail účtu.")
    user = _user_or_die(a, args.email)
    if args.action == "remove":
        gone = workspace.remove_predplatne(user)
        print(f"{args.email}: " + ("předplatné odpojeno." if gone else "předplatné připojené neměl.")
              + " Běžící prostor ho má do dalšího startu (stop <e-mail>).")
        return
    token = (getpass.getpass("Token z `claude setup-token` (sk-ant-oat01-…): ")
             if sys.stdin.isatty() else sys.stdin.readline()).strip()
    workspace.save_predplatne(user, token, "claude-hub-admin")
    print(f"{args.email}: Claude v prostoru pojede na tomhle předplatném od dalšího startu"
          " prostoru (stop <e-mail> ho uspíší). Token platí rok. Jedno předplatné"
          " patří jednomu člověku — nesdílej ho mezi účty.")


def cmd_web(a, args):
    """Web (FTP) účtu: kam Claude v jeho prostoru zveřejňuje appky a stránky.
    Heslo se zadá skrytě, nebo přijde na standardní vstup (--password-stdin) —
    na příkazové řádce by zůstalo v historii. Běžící prostor ho dostane do
    minuty sám (HubManager.preved_web), další start rovnou."""
    if not args.email:
        rows = [r for r in a.list() if workspace.web_public(r)]
        if not rows:
            print("Web nemá přidělený nikdo.")
        for r in rows:
            w = workspace.web_public(r)
            print(f"  {r['email']:<32} {w['url']}  (ftp {w['user']}@{w['host']}:{w['port']}"
                  f"{w['remote_dir']}, heslo {w['heslo']})")
        return
    user = _user_or_die(a, args.email)
    if args.remove:
        gone = workspace.remove_web(user)
        print(f"{args.email}: " + ("web odebrán — z běžícího prostoru zmizí do minuty."
                                    if gone else "web přidělený neměl."))
        return
    old = workspace.web(user) or {}
    host = args.host or old.get("host", "")
    ftp_user = args.ftp_user or old.get("user", "")
    url = args.url or old.get("url", "")
    if not (host and ftp_user and url):
        raise ValueError("Zadej --host, --user a --url.")
    if args.password_stdin:
        password = sys.stdin.readline().rstrip("\r\n")
    elif sys.stdin.isatty():
        prompt = "Heslo FTP" + (" (Enter = nechat původní)" if old.get("password") else "") + ": "
        password = getpass.getpass(prompt) or old.get("password", "")
    else:
        password = sys.stdin.readline().rstrip("\r\n")
    data = workspace.save_web(user, host, ftp_user, password, url,
                              args.dir if args.dir is not None else old.get("remote_dir", "/"),
                              args.port if args.port is not None else old.get("port", 21),
                              "claude-hub-admin")
    print(f"{args.email}: web {data['url']} (ftp {data['user']}@{data['host']}:{data['port']}"
          f"{data['remote_dir']}). Běžící prostor ho dostane do minuty, jinak při startu —"
          " Claude v nových chatech ví, že tam může zveřejňovat.")


def cmd_verxee(a, args):
    """Tlačítko Nahlásit chybu / Podat návrh (Verxee Debug) v prostorech. Klíč je
    veřejný (skončí ve stránce); v portálu alba-rosa.cz/debug → Klíče a domény
    musí mít mezi doménami adresu brány."""
    path = config.VERXEE_KLIC_FILE
    if args.action == "status":
        klic = workspace.verxee_klic()
        print(("Klíč: " + klic) if klic else "Tlačítko je vypnuté (klíč není nastavený).")
        return
    if args.action == "remove":
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        print("Klíč smazán. Tlačítko zmizí s dalším startem prostoru.")
        return
    klic = (args.klic or "").strip()
    if not workspace.VERXEE_KLIC.fullmatch(klic):
        raise ValueError("Klíč vypadá jako vxd_ a 24 znaků 0–9 a a–f.")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="ascii") as fh:
        fh.write(klic + "\n")
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)
    print("Klíč uložen. Tlačítko se v prostoru ukáže s jeho dalším startem "
          "(stop <e-mail> to uspíší).")


def cmd_disable(a, args):
    a.set_disabled(args.email, True)
    print(f"{args.email} zablokován (a odhlášen).")


def cmd_enable(a, args):
    a.set_disabled(args.email, False)
    print(f"{args.email} znovu povolen.")


def cmd_remove(a, args):
    user = a.get(args.email)
    a.remove(args.email)
    if not user:
        return
    shared.forget_user(user["id"])
    relace.forget_user(user["id"])
    poznamky.forget_user(user["id"])
    firmy.forget_user(user["id"])
    mcp_sdilene.forget_user(user["id"])
    try:
        slozky.sync()                    # pryč i ze sdílených trezorů
    except (OSError, ValueError):
        pass
    # Prostor smazaného účtu nemá komu běžet — a nikdo by ho už nezastavil.
    for name in _units(user):
        isolation.stop_scope(name)
    moved = workspace.retire(user)
    print(f"{args.email} smazán z databáze." +
          (f" Jeho složka je odložená v {moved}." if moved else ""))


def _units(user):
    """Jména scope, pod kterými může prostor účtu běžet: podle e-mailu a to
    staré `claude-hub-u<id>` z doby před restartem brány po aktualizaci."""
    return [workspace.unit_name(user), workspace.legacy_unit_name(user)]


def _by_unit(a):
    return {name: u for u in a.list() for name in _units(u)}


def cmd_sessions(a, args):
    """Běžící prostory a kolik drží paměti. Starší bez jména jsou osiřelé."""
    scopes = isolation.running_scopes()
    if not scopes:
        print("Neběží žádný prostor.")
        return
    by_unit = _by_unit(a)
    for name, _desc in scopes:
        user = by_unit.get(name)
        who = user["email"] if user else "(osiřelý — brána o něm neví)"
        mb = isolation.scope_memory(name) / 1024 / 1024
        print(f"{name:<45} {mb:>7.0f} MB  {who}")


def cmd_stop(a, args):
    if args.orphans:
        known = _by_unit(a)
        names = [n for n, _d in isolation.running_scopes() if n not in known]
    elif not args.email:
        raise ValueError("Zadej e-mail účtu, nebo --orphans.")
    else:
        user = a.get(args.email)
        if not user:
            raise ValueError(f"{args.email} tu žádný účet nemá.")
        running = {n for n, _d in isolation.running_scopes()}
        names = [n for n in _units(user) if n in running] or _units(user)[:1]
    if not names:
        print("Není co zastavit.")
    for name in names:
        ok = isolation.stop_scope(name)
        print(f"{name}: {'zastaveno' if ok else 'NEPODAŘILO SE zastavit'}")


def cmd_firma(a, args):
    """Přístup k firemnímu Obsidianu: výpis, nebo nastavení jednomu účtu.
    Běžně to mění admini přímo ve firemním Obsidianu v hubu."""
    if not args.email:
        names = {"none": "nevidí", "read": "čte", "write": "čte i zapisuje"}
        for p in a.company_list():
            who = f"{p['email']} ({p['name']})" if p["name"] else p["email"]
            extra = " · admin" if p["role"] == "admin" else (f" · nastavil {p['by']}" if p["by"] else "")
            print(f"{who:<48} {names[p['level']]}{extra}")
        return
    if args.level not in ("none", "read", "write"):
        raise ValueError("Úroveň je none, read, nebo write.")
    user = a.get(args.email)
    if not user:
        raise ValueError(f"{args.email} tu žádný účet nemá.")
    _target, old = a.set_company_level({"role": "admin", "email": "claude-hub-admin"},
                                       user["id"], args.level)
    print(f"{args.email}: {old} → {args.level}. Projeví se při dalším startu jeho prostoru"
          " (zápis a napojení z appky Claude hned).")


def cmd_firmy(a, args):
    """Firmy (gateway/firmy.py): výpis, lidé a přístupy jedné firmy, založení,
    kapitán a kdo smí zakládat. Běžně se to dělá v hubu (Nastavení → Firmy)."""
    def who(u):
        return f"{u['email']} ({u['name']})" if u.get("name") else u["email"]
    if args.action == "zalozit":
        user = a.get(args.email or "")
        if not user:
            raise ValueError("Kapitán: --kapitan <e-mail existujícího účtu>.")
        r = firmy.zalozit(None, args.cil, kapitan=user, druh_firmy=args.druh)
        print(f"Firma „{r['nazev']}“ ({r['zkratka']}) založená, kapitán {user['email']}.")
        return
    if args.action == "smazat":
        r = firmy.smazat_firmu({"role": "admin", "email": "claude-hub-admin", "id": 0}, args.cil)
        print(r["message"])
        return
    if args.action == "kapitan":
        user = a.get(args.email or "")
        if not user:
            raise ValueError("Komu: --kapitan <e-mail existujícího účtu>.")
        firmy.admin_kapitan(args.cil, user["id"])
        print(f"{user['email']} je kapitánem firmy {args.cil}.")
        return
    if args.action == "zakladatel":
        user = a.get(args.cil or "")
        if not user:
            raise ValueError("firmy zakladatel <e-mail> [--odebrat]")
        firmy.nastavit_zakladatele({"role": "admin"}, user["id"], not args.odebrat)
        print(f"{user['email']} {'už nesmí' if args.odebrat else 'smí'} zakládat firmy.")
        return
    if args.action:
        slug = args.action
        if not firmy.exists(slug):
            raise ValueError(f"Firma {slug} tu není (claude-hub-admin firmy vypíše, jaké jsou).")
        print(f"{firmy.nazev(slug)} ({slug}) — {firmy.vault_dir(slug)}")
        for m in firmy.clenove(slug):
            extra = " · smí mazat" if m["role"] == "clen" and m["mazat"] else ""
            print(f"  {who(m):<46} {firmy.ROLE_LABELS[m['role']]}{extra}")
        if firmy.klienti_zapnute(slug):
            ids = {m["id"]: m for m in firmy.clenove(slug)}
            prist = firmy.pristupy(slug)
            for klient, podkategorie in firmy.klienti(slug).items():
                root = firmy.koren(slug)
                for rel in [f"{root}/{klient}"] + [f"{root}/{klient}/{p}" for p in podkategorie]:
                    lidi = [ids[u]["email"] for u in prist.get(rel, []) if u in ids]
                    print(f"  {rel:<46} {', '.join(lidi) or 'jen kapitáni'}")
        return
    zakl = [r["email"] for r in a.list() if firmy.muze_zakladat(dict(r)) and r["role"] != "admin"]
    for slug in firmy.slugs():
        lide = firmy.clenove(slug)
        kap = [m["email"] for m in lide if m["role"] == "kapitan"]
        print(f"{slug:<20} {firmy.nazev(slug):<28} {len(lide)} lidí · kapitáni: {', '.join(kap) or '—'}")
    print("Zakládat firmy smí: admini" + (", " + ", ".join(zakl) if zakl else ""))


def cmd_poznamky(a, args):
    """Kdo vidí kterou poznámku firemního Obsidianu a kdo to smí nastavovat.
    Poznámkám se to nastavuje v hubu; tady jen výpis a správci."""
    if args.action == "spravci":
        if args.emails:
            print("Správci poznámek: " + ", ".join(poznamky.set_managers(args.emails)))
        else:
            ids = poznamky.managers()
            names = [(a.by_id(i) or {}).get("email", f"#{i}") for i in ids]
            print("Správci poznámek: " + (", ".join(names) if names else
                  "nikdo — nastav: claude-hub-admin poznamky spravci <e-mail> …"))
        return
    ids = poznamky.managers()
    print("Správci: " + (", ".join((a.by_id(i) or {}).get("email", f"#{i}") for i in ids)
                         or "nikdo"))
    notes = poznamky.listing()
    folders = set(poznamky.folders())
    if not notes:
        print("Všechny poznámky vidí každý, kdo vidí firemní Obsidian.")
    for rel, people in notes.items():
        label = f"{rel}/ (složka)" if rel in folders else rel
        print(f"{label}\n    vidí: " + (", ".join(p["email"] for p in people) or "jen správci"))
    print(f"{slozky.SHARED}/<název> vidí jen členové "
          "sdíleného Obsidianu (claude-hub-admin sdilene).")


def cmd_mcp_sdilene(a, args):
    """Sdílená napojení: kdo je založil a s kým je sdílí (bez tajemství)."""
    items = mcp_sdilene.all_items()
    if not items:
        print("Žádné sdílené napojení.")
    for n in items:
        print(f"{n['name']} ({n['slug']}, {n['kind']}) — založil {n['owner'] or '?'}\n"
              f"    používají: {', '.join(n['members']) or 'nikdo'}")


def cmd_mcp(a, args):
    """Napojení z appky Claude (gateway/mcp.py): výpis a zrušení."""
    if args.action == "zrusit":
        if not args.email:
            raise ValueError("Zadej e-mail účtu.")
        n = a.oauth_revoke_user(args.email)
        print(f"Zrušeno ({n} tokenů). Appka se bude muset přihlásit znovu.")
        return
    rows = a.oauth_logins(args.email or None)
    if not rows:
        print("Žádné napojení z appky Claude.")
        return
    for r in rows:
        seen = time.strftime("%d. %m. %H:%M", time.localtime(r["seen"])) if r["seen"] else "zatím nic"
        made = time.strftime("%d. %m. %Y", time.localtime(r["created"]))
        print(f"{r['email']:<32} {r['client'] or '?':<20} od {made}, naposled {seen}")


def build_parser():
    p = argparse.ArgumentParser(prog="gateway.admin",
                                description="Správa účtů brány.")
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("add", help="založit účet")
    a.add_argument("email")
    a.add_argument("--name", default="")
    a.add_argument("--role", choices=("user", "admin"), default="user")
    a.add_argument("--vault", default="", help="jméno Obsidian paměti")
    a.add_argument("--password", default="", help="jinak se zeptá skrytě")
    a.set_defaults(func=cmd_add)

    sub.add_parser("list", help="vypsat účty").set_defaults(func=cmd_list)

    sp = sub.add_parser("spotreba", help="spotřeba Claude po lidech za týden, týdenní stropy")
    sp.add_argument("email", nargs="?", default="")
    sp.add_argument("--limit", default=None, help="týdenní strop v USD (0 = zrušit)")
    sp.set_defaults(func=cmd_spotreba)

    pw = sub.add_parser("passwd", help="změnit heslo")
    pw.add_argument("email")
    pw.add_argument("--password", default="")
    pw.set_defaults(func=cmd_passwd)

    ro = sub.add_parser("role", help="změnit roli")
    ro.add_argument("email")
    ro.add_argument("role", choices=("user", "admin"))
    ro.set_defaults(func=cmd_role)

    jm = sub.add_parser("jmeno", help="změnit jméno uživatele")
    jm.add_argument("email")
    jm.add_argument("jmeno")
    jm.set_defaults(func=cmd_jmeno)

    va = sub.add_parser("vault", help="přejmenovat vault")
    va.add_argument("email")
    va.add_argument("vault")
    va.set_defaults(func=cmd_vault)

    au = sub.add_parser("auth", help="Claude: klíč API (central) / vlastní účet (own)")
    au.add_argument("email", nargs="?", default="")
    au.add_argument("mode", choices=("central", "own"))
    au.add_argument("--vsechny", action="store_true",
                    help="přepnout všechny účty místo jednoho")
    au.set_defaults(func=cmd_auth)

    ak = sub.add_parser("apikey", help="klíč API pro prostory na central")
    ak.add_argument("action", choices=("set", "status", "remove"))
    ak.add_argument("--user", default="",
                    help="e-mail účtu — klíč jen jeho, místo společného")
    ak.set_defaults(func=cmd_apikey)

    sk = sub.add_parser("skills", help="firemní skilly ve firemním Obsidianu")
    sk.add_argument("action", choices=("install", "update", "status"))
    sk.add_argument("--from", dest="src", default="",
                    help="složka se skilly místo stažení z gitu")
    sk.add_argument("--force", action="store_true",
                    help="přepsat skilly, které tam už jsou")
    sk.set_defaults(func=cmd_skills)

    go = sub.add_parser("google", help="klient OAuth pro napojení na Google")
    go.add_argument("action", choices=("set", "status", "remove"))
    go.set_defaults(func=cmd_google)

    pp = sub.add_parser("predplatne", help="Claude na vlastním předplatném (token setup-token)")
    pp.add_argument("action", choices=("status", "set", "remove"))
    pp.add_argument("email", nargs="?", default="", help="e-mail účtu")
    pp.set_defaults(func=cmd_predplatne)

    wb = sub.add_parser("web", help="web (FTP) účtu, kam Claude v prostoru zveřejňuje")
    wb.add_argument("email", nargs="?", default="", help="e-mail účtu (bez něj výpis)")
    wb.add_argument("--host", default="", help="FTP server, třeba ftp.firma.cz")
    wb.add_argument("--user", dest="ftp_user", default="", help="uživatel FTP")
    wb.add_argument("--url", default="", help="veřejná adresa složky, třeba https://firma.cz/adam")
    wb.add_argument("--dir", default=None, help="složka na FTP, která je na --url (výchozí /)")
    wb.add_argument("--port", type=int, default=None, help="port FTP (výchozí 21, FTP přes TLS)")
    wb.add_argument("--password-stdin", action="store_true",
                    help="heslo z prvního řádku standardního vstupu")
    wb.add_argument("--remove", action="store_true", help="web účtu odebrat")
    wb.set_defaults(func=cmd_web)

    vx = sub.add_parser("verxee", help="tlačítko Nahlásit chybu / Podat návrh (klíč vxd_…)")
    vx.add_argument("action", choices=("status", "set", "remove"))
    vx.add_argument("klic", nargs="?", default="", help="klíč vxd_… z alba-rosa.cz/debug")
    vx.set_defaults(func=cmd_verxee)

    di = sub.add_parser("disable", help="zablokovat účet")
    di.add_argument("email")
    di.set_defaults(func=cmd_disable)

    en = sub.add_parser("enable", help="znovu povolit účet")
    en.add_argument("email")
    en.set_defaults(func=cmd_enable)

    rm = sub.add_parser("remove", help="smazat účet")
    rm.add_argument("email")
    rm.set_defaults(func=cmd_remove)

    sd = sub.add_parser("sdilene", help="sdílené Obsidiany: kdo je založil a kdo v nich je")
    sd.set_defaults(func=cmd_sdilene)

    tf = sub.add_parser("2fa", help="dvoufázové ověření: stav, nebo reset (ztracený telefon)")
    tf.add_argument("action", choices=("status", "reset"))
    tf.add_argument("email", nargs="?", default="", help="e-mail účtu (u status volitelný)")
    tf.set_defaults(func=cmd_2fa)

    zk = sub.add_parser("zamky", help="zablokovaná přihlášení po špatných pokusech: výpis, odemknutí")
    zk.add_argument("action", nargs="?", default="status", choices=("status", "odemknout"))
    zk.add_argument("target", nargs="?", default="", help="e-mail nebo IP adresa (u odemknout)")
    zk.set_defaults(func=cmd_zamky)

    fi = sub.add_parser("firma", help="kdo vidí firemní Obsidian a kdo do něj zapisuje")
    fi.add_argument("email", nargs="?", default="")
    fi.add_argument("level", nargs="?", default="", help="none / read / write")
    fi.set_defaults(func=cmd_firma)

    fy = sub.add_parser("firmy", help="firmy: výpis, lidé a přístupy, založení, kapitán, kdo smí zakládat")
    fy.add_argument("action", nargs="?", default="",
                    help="<zkratka> | zalozit | kapitan | zakladatel | smazat")
    fy.add_argument("cil", nargs="?", default="", help="název firmy / zkratka / e-mail")
    fy.add_argument("--kapitan", dest="email", default="", help="e-mail kapitána")
    fy.add_argument("--odebrat", action="store_true", help="zakladatel: odebrat právo")
    fy.add_argument("--druh", default="klienti", choices=sorted(firmy.DRUHY),
                    help="zalozit: klienti (agentura) nebo projekty (firma pro sebe)")
    fy.set_defaults(func=cmd_firmy)

    pz = sub.add_parser("poznamky", help="kdo vidí kterou firemní poznámku; správci poznámek")
    pz.add_argument("action", nargs="?", default="status", choices=("status", "spravci"))
    pz.add_argument("emails", nargs="*", default=[],
                    help="u spravci: nový seznam správců (nahradí starý)")
    pz.set_defaults(func=cmd_poznamky)

    sub.add_parser("mcp-sdilene", help="sdílená napojení (MCP): kdo je založil a kdo je používá"
                   ).set_defaults(func=cmd_mcp_sdilene)

    mc = sub.add_parser("mcp", help="napojení z appky Claude: výpis, zrušení")
    mc.add_argument("action", nargs="?", default="status", choices=("status", "zrusit"))
    mc.add_argument("email", nargs="?", default="")
    mc.set_defaults(func=cmd_mcp)

    sub.add_parser("sessions", help="které prostory běží a kolik berou paměti"
                   ).set_defaults(func=cmd_sessions)

    st = sub.add_parser("stop", help="zastavit prostor (i s Claude Code v něm)")
    st.add_argument("email", nargs="?", default="", help="e-mail účtu")
    st.add_argument("--orphans", action="store_true",
                    help="zastavit prostory, ke kterým žádný účet nepatří")
    st.set_defaults(func=cmd_stop)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    a = _accounts()
    try:
        args.func(a, args)
    except ValueError as exc:
        sys.exit(f"Chyba: {exc}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
