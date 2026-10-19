#!/usr/bin/env python3
"""
Claude Code Hub — projects, memory and Claude Code sessions in one window.

    +--------------------+-------------------------------------------+
    |  SIDEBAR (hub)     |  TABS                                     |
    |  - project list    |  [ project A ] [ project B ] [ shell ]  + |
    |  - obsidian memory |                                           |
    |                    |  <real terminal running claude>           |
    +--------------------+-------------------------------------------+

The window is a thin host around a local web UI served from 127.0.0.1; the
terminals inside it are real ptys (stdlib `pty` on Linux/macOS, ConPTY through
pywinpty on Windows). One code path, all three platforms.

    python3 claude-hub.py                 open the hub
    python3 claude-hub.py --doctor        print what this machine has, then exit
    python3 claude-hub.py --no-browser    start the server and print the URL
    python3 claude-hub.py --vlastni-okno  like --no-browser, but quits by itself once the
                                          page is closed (window owned by desktop/)
    python3 claude-hub.py --no-browser --test
                                          test copy next to the real hub: no tab
                                          restore/saving, no link to the gateway
    python3 claude-hub.py --window=webkit force a window host
                                          (chromium | webkit | browser)
    python3 claude-hub.py --server=adresa otevírat prostor na serveru
    python3 claude-hub.py --local         otevírat hub na tomhle počítači
"""
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Konzole na Windows píše v systémové stránce (na české instalaci cp1250) a
# `--doctor` na ní padal na UnicodeEncodeError hned u prvního rámečku. Naměřeno
# ve virtuálce s Windows 11 Pro. Znaky, které se do stránky nevejdou, ať radši
# vypadnou jako otazník, než aby shodily celý výpis.
if sys.platform == "win32":
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass

from hub import account, core, pocitac, pozadi, predplatne, pty_backend, server, window  # noqa: E402


MCP_MARKS = {"ok": "+", "auth": "!", "fail": "-", "local": ".",
             "unknown": "?"}


def doctor():
    info = core.doctor()
    ok, detail = pty_backend.selftest()
    # Zdravotní kontrola oslovuje každý MCP server zvlášť, takže tohle je
    # nejpomalejší část výpisu (~10 s). V --doctor to stojí za to: jinak se
    # „napojeno" pozná až tím, že v Claude Code nefunguje.
    mcp = core.mcp_list()
    agents_found = core.agents.detect(core._agents_extra())
    ollama = core.agents.ollama_state()
    browser = window.find_browser()
    webkit = window.has_webkit()
    host = ("chromium (" + os.path.basename(browser) + ")" if browser else
            "webkitgtk" if webkit else "výchozí prohlížeč (jen záložka)")
    print()
    print("  Paluba — kontrola prostředí")
    print("  " + "─" * 42)
    for label, value in [
        ("platforma", info["platform"]),
        # Bez kořenových certifikátů se hub nepřihlásí na server, i když tatáž
        # adresa v prohlížeči chodí — prohlížeč si důvěru vede zvlášť.
        ("certifikáty", account.ca_state()),
        ("bash", info["bash"] or "CHYBÍ"),
        ("git", info["git"] or "chybí"),
        ("AI agenti", _agents_summary(info)),
        ("agent-wrapper.sh", info.get("agent_wrapper") or
         "CHYBÍ — spusť install.sh, jinak se taby otevřou jako holý shell"),
        # Na Windows je tohle jediná odpověď, která něco znamená: symlink tam
        # chce práva správce, křižovatka ne — a co projde, se dá jen vyzkoušet.
        ("odkaz na složku", info["link"] or
         ("NEJDE — " + info["link_error"] + " (paměť nepůjde napojit)")),
        ("paměť napojená", f'{info["memory_link"]}  {info["memory_link_path"]}'),
        ("ftp-deploy.sh", info["ftp_deploy"] or "není"),
        ("Obsidian Brain", info["brain"] or "není (paměť vypnutá)"),
        ("složky projektů", ", ".join(info["project_dirs"]) or "žádné"),
        ("slash příkazy",
         ", ".join("/" + s for s in core.installed_skills()) or "žádné"),
        ("okno", host),
        # Prohlížeč pro Claude Code: zajímavý je jen profil. Bez připnutého
        # profilu se každá přihlášená session ztratí s přepnutím projektu.
        ("prohlížeč (MCP)", f'{info["browser_mcp"]}  {info["browser_mcp_detail"]}'),
        ("napojení (MCP)", _mcp_summary(mcp)),
        ("pty test", "OK" if ok else f"SELHAL — {detail}"),
        ("log", core.LOG_PATH),
    ]:
        print(f"  {label:<20} {value}")
        # Rozpis patří hned pod svůj řádek, ne až za celou tabulku.
        # Rozpis patří hned pod svůj řádek, ne až za celou tabulku.
        if label == "AI agenti":
            for a in agents_found:
                mark = "+" if a["path"] else "-"
                extra = f' {a["version"]}' if a["version"] else ""
                where = a["path"] or ("není v PATH — " + (a["install"] or "?"))
                if a["id"] == "ollama" and a["path"]:
                    where += ("  " + _ollama_note(ollama))
                star = " (výchozí)" if a["id"] == info.get("default_agent") else ""
                print(f'  {"":<20} {mark} {a["label"]}{extra}{star} — {where}')
        if label == "napojení (MCP)":
            for server in mcp.get("servers") or []:
                mark = MCP_MARKS.get(server["state"], "?")
                print(f"  {'':<20} {mark} {server['name']} — {server['status']}")
    print()
    if not info["bash"]:
        print("  ⚠ Bez bash hub neumí spustit tab:")
        print("    winget install Git.Git" if core.IS_WINDOWS
              else "    sudo apt install bash")
        print()
    return 0 if (ok and info["bash"]) else 1


def _ollama_note(state):
    """Ollama je zvláštní: nestačí, že je nainstalovaná — musí i běžet."""
    if not state.get("running"):
        return "(neběží — spusť: ollama serve)"
    n = len(state.get("models") or [])
    slovo = "model" if n == 1 else ("modely" if n < 5 else "modelů")
    jmena = ", ".join((state.get("models") or [])[:3])
    return f"(běží, {n} {slovo}: {jmena})" if n else "(běží, žádný model stažený)"


def _agents_summary(info):
    """Kolik agentů je po ruce — detail se vypisuje pod tím řádkem."""
    found = [k for k, v in (info.get("agents") or {}).items() if v]
    if not found:
        return "žádný (tab bude obyčejný shell)"
    return f'{len(found)} z {len(info.get("agents") or {})} k dispozici'


def _mcp_summary(mcp):
    if not mcp.get("ok"):
        return mcp.get("detail") or "nepodařilo se zjistit"
    c = mcp.get("counts") or {}
    parts = [f'{c.get("ok", 0)} z {c.get("total", 0)} připojeno']
    if c.get("auth"):
        parts.append(f'{c["auth"]}x chce přihlásit')
    if c.get("fail"):
        parts.append(f'{c["fail"]}x nepřipojeno')
    if c.get("local"):
        parts.append(f'{c["local"]}x jen v projektu')
    return ", ".join(parts)


def _window_open(proc):
    """Běží ještě proces okna? U prohlížeče, který okno předal jinam, už ne."""
    return proc is not None and proc.poll() is None


def wait_for_page(proc=None, grace=10, startup=60):
    """Stay alive while the page is open. The page — not the browser process — is
    the signal.

    A chromium launcher that hands its window to an already running browser exits
    within milliseconds. Waiting on that process meant shutting the server down
    while the window was still on screen, and the user got ERR_CONNECTION_REFUSED
    on a window that had never even loaded.

    Výjimka je prostor na serveru: okno pak ukazuje bránu, ne tuhle stránku, a
    websocket se sem nepřipojí vůbec. Server tu ale musí zůstat — taby, které
    na počítači běží dál, a cesta zpátky („Pracovat na tomto počítači") vedou
    právě sem. Dokud je appka v serverovém režimu, drží se proto procesu okna.
    """
    hub = server.HUB
    started = time.time()
    while True:
        time.sleep(0.4)
        if hub.clients > 0:
            continue
        if core.CONFIG.get("server_mode") and _window_open(proc):
            continue
        if hub.last_empty_at is None:
            # Stránka se ještě nepřipojila. V serverovém režimu to nevadí —
            # okno šlo rovnou na bránu —, jinak je to okno, které nenaběhlo.
            if time.time() - started > startup:
                if not core.CONFIG.get("server_mode"):
                    core.log("okno se do %d s nepřipojilo — končím" % startup)
                    if proc is not None:
                        try:
                            proc.terminate()
                        except Exception:
                            pass
                return
            continue
        if time.time() - hub.last_empty_at > grace:
            # S "nechat běžet pro telefon" je zavřené okno jen zavřené okno —
            # server musí zůstat, jinak se z mobilu není kam připojit.
            if core.CONFIG.get("remote_enabled") and \
                    core.CONFIG.get("remote_keep_running"):
                continue
            # „Běžet na pozadí": zavřené okno je jen zavřené okno.
            if core.CONFIG.get("na_pozadi"):
                continue
            core.log("okno zavřeno — končím")
            return


def otevri_okno(target, prefer, appka_ok, vlastni_server=True):
    """window.open_window s pojistkou: okno samostatné appky (hub/appka.py),
    které nenaběhne, se nahradí oknem v prohlížeči. Bez téhle pojistky by po
    rozbité appce nezbylo nic, kam se dívat."""
    host, proc, blocking = window.open_window(target, prefer, appka_ok)
    if host != "appka":
        return host, proc, blocking
    from hub import appka
    if vlastni_server:
        # Server je náš → spolehlivý signál je stránka, která se připojí.
        # Appka, která hned skončí s chybou, nenaběhne — na to se nečeká.
        # Nula je v pořádku: druhé spuštění běžící appky okno předá a skončí.
        konec = time.time() + 30
        while time.time() < konec and server.HUB.clients == 0 and proc.poll() in (None, 0):
            time.sleep(0.25)
        ok = server.HUB.clients > 0
    else:
        # Okno k hubu, který běží v jiném procesu — vidíme jen appku samu.
        # Druhé spuštění běžící appky okno předá a hned skončí s nulou.
        time.sleep(4)
        ok = proc.poll() in (None, 0)
    if ok:
        return host, proc, blocking
    core.log("appka: okno nenaběhlo — otevírám ho v prohlížeči", "warn")
    appka.SELHALA = True
    try:
        proc.kill()
    except Exception:
        pass
    return window.open_window(target, prefer, appka_ok=False)


def _appka_na_pozadi(url):
    """Stáhne nebo aktualizuje samostatnou appku z posledního vydání. Poprvé
    stažená hned převezme okno v prohlížeči — na téže instanci, takže taby
    i rozdělaná práce Clauda běží dál a nic se nerestartuje."""
    from hub import appka
    # Vydání přikládá appku až po sestavení (Instalačky, ~25 min po značce) —
    # kdo se aktualizoval dřív, dostane ji při dalším pokusu, ne až po restartu.
    stav = ""
    for _ in range(48):
        try:
            stav = appka.zajisti()
        except Exception as exc:
            core.log(f"appka: {exc}", "warn")
        if stav or appka.spustitelny() or not appka.zapnuta():
            break
        time.sleep(1800)
    if stav != "nova" or core.VLASTNI_OKNO or not appka.zapnuta():
        return
    hub = server.HUB
    # Stažení bývá rychlejší než start okna v prohlížeči — počkat, až naběhne.
    konec = time.time() + 90
    while time.time() < konec and hub.clients == 0:
        time.sleep(0.5)
    if hub.clients == 0:
        return              # okno otevřené není — příště se otevře rovnou appka
    pred = hub.clients
    try:
        proc = appka.spust(url)
    except OSError as exc:
        core.log(f"appka: nejde spustit ({exc})", "warn")
        return
    konec = time.time() + 40
    while time.time() < konec and hub.clients <= pred:
        time.sleep(0.5)
    if hub.clients > pred:
        core.log("appka: převzala okno, okna v prohlížeči se zavřou")
        hub.broadcast({"t": "appka-prechod"})
    else:
        core.log("appka: okno nenaběhlo, zůstává prohlížeč", "warn")
        appka.SELHALA = True
        try:
            proc.kill()
        except Exception:
            pass


def server_start_url(local_url):
    """Kam otevřít okno v serverovém režimu. None = zůstat na počítači.

    Předání se chystá tady, ještě před oknem: kdyby se to nechalo na stránce,
    bliklo by nejdřív celé UI hubu na počítači a teprve pak by se přeskočilo.
    Když to nevyjde (server nejede, přihlášení vypršelo), otevře se hub na
    počítači a ten člověku řekne proč a co s tím — viz HubServer.gate.

    Adresa počítače jde do fragmentu (`#local=`): ten prohlížeč serveru nikdy
    nepošle, takže token téhle instance neskončí v logu nginx. Hub na serveru
    si ho z adresy přečte a vede přes něj cestu zpátky.
    """
    import urllib.parse
    res = account.handoff(timeout=8)
    if not res.get("url"):
        core.log(f"server: {res.get('error')} — otevírám hub na počítači", "warn")
        return None
    core.log(f"server: otevírám prostor na {account.normalize(core.CONFIG.get('gw_server'))}")
    # `app=` = verze téhle appky: prostor podle ní pozná, co appka umí (server.js).
    from hub import __version__
    return (res["url"] + "#local=" + urllib.parse.quote(local_url, safe="")
            + "&app=" + urllib.parse.quote(__version__))


def main():
    args = sys.argv[1:]
    if "--test" in args:
        core.TEST_MODE = True
    if "--vlastni-okno" in args:
        core.VLASTNI_OKNO = True
    if "--doctor" in args:
        return doctor()

    prefer = core.CONFIG.get("window", "")
    # Okno vybrané na příkazové řádce je výslovné přání — samostatná appka
    # (hub/appka.py) pak ustoupí. Volba "window" v nastavení ne: ta je starší.
    appka_ok = True
    for arg in args:
        if arg.startswith("--window="):
            prefer = arg.split("=", 1)[1]
            appka_ok = False

    # Prostor na serveru: `--server[=adresa]` ho zapne, `--local` vypne.
    # Samo přepnutí nic neověřuje — bez přihlášení se otevře hub na počítači
    # a ukáže přihlášení. `--no-browser` je vždycky hub na počítači: tak ho
    # pouští i brána pro každého uživatele a ta se na server ptát nesmí.
    for arg in args:
        if arg == "--server" or arg.startswith("--server="):
            updates = {"server_mode": True}
            if "=" in arg:
                updates["gw_server"] = account.normalize(arg.split("=", 1)[1])
            core.save_config(updates)
        elif arg == "--local":
            core.save_config({"server_mode": False})

    # Appka už běží na pozadí (zavřené okno) → jen se k ní otevře nové okno.
    # Druhá instance nad stejnými chaty by si přepisovala konverzace.
    if "--no-browser" not in args and "--vlastni-okno" not in args and not core.TEST_MODE:
        bezi = pozadi.bezici_url()
        if bezi:
            core.log("běží na pozadí — otevírám okno k ní")
            host, proc, blocking = otevri_okno(bezi, prefer, appka_ok, vlastni_server=False)
            if blocking:
                blocking()
            return 0

    httpd, url = server.start()
    pozadi.zapis(url)
    if "--no-browser" not in args and "--vlastni-okno" not in args:
        # Aktualizace přepíše spouštěč na výchozí název a ikonu; okno se po
        # restartu jen přepne, takže vlastní vzhled se musí vrátit tady.
        try:
            from hub import vzhled
            vzhled.sync_desktop()
        except Exception as exc:
            core.log(f"vzhled: spouštěč nejde srovnat ({exc})", "warn")
        pozadi.ikonka_url(url)          # ikonka v liště jen u appky s oknem
    pozadi.sync()
    if "--no-browser" not in args and not core.TEST_MODE:
        # Samostatná appka: stáhnout/aktualizovat na pozadí, okno na to nečeká.
        threading.Thread(target=_appka_na_pozadi, args=(url,), daemon=True).start()
    # Celý doctor() tu nevolat — ptá se agentů (CLI) a okno by na to čekalo.
    # Verze v logu: hlídač brány (gateway/zdravi.py) podle ní pozná, jestli
    # chyby stránky v prostoru nejsou už opravené v novější verzi.
    from hub import __version__ as verze
    core.log(f"start: port {httpd.server_address[1]}, platforma {sys.platform}, verze {verze}")
    if core.CONFIG.get("remote_enabled"):
        # Tailscale se ptáme přes CLI, což trvá — okno na to nesmí čekat.
        threading.Thread(target=server.start_remote, daemon=True).start()
    try:
        if "--vlastni-okno" in args:
            # Okno si dělá jiná appka (desktop/): dostane adresu a server žije,
            # dokud se k němu nějaká stránka připojuje — pak se zavře jako dřív.
            print(url, flush=True)
            wait_for_page(None)
            return 0
        if "--no-browser" in args:
            print(url, flush=True)
            while True:
                time.sleep(3600)
        target = url
        # Kdo ještě neodpověděl, jestli má Claude ze serveru sahat i na tenhle
        # počítač, nebo komu v prostoru Claude nemá na čem jet (bez klíče API a
        # bez předplatného), projde jednou přes hub na počítači — ten to
        # vyřeší a pak přejde na server sám (server.js, go).
        if core.CONFIG.get("server_mode") and core.CONFIG.get("pocitac_asked") \
                and not predplatne.should_offer():
            target = server_start_url(url) or url
        if "--bez-okna" in args:
            # Restart po aktualizaci: stávající okno se přepne samo. Kdyby se do
            # 12 s nepřipojilo (zavřeli jsme ho, nemá spojení), otevře se nové.
            deadline = time.time() + 12
            while time.time() < deadline and server.HUB.clients == 0:
                time.sleep(0.2)
            if server.HUB.clients > 0:
                core.log("okno: převzato (přepnuté stávající)")
                wait_for_page(None)
                return 0
        host, proc, blocking = otevri_okno(target, prefer, appka_ok)
        core.log(f"okno: {host}")
        if blocking:
            blocking()          # in-process loop owns the window's lifetime
            # Na pozadí: okno (WebKit je součást procesu) je pryč, server zůstává.
            while core.CONFIG.get("na_pozadi"):
                time.sleep(3600)
        else:
            wait_for_page(proc)
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        core.log(f"CHYBA: {exc!r}")
        raise
    finally:
        pozadi.spanek(False)
        pozadi.smaz()
        pocitac.stop()
        server.stop_remote()
        server.HUB.shutdown()
        httpd.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
