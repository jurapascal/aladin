"""Restart hubu po aktualizaci — a taby zpátky tam, kde byly.

Aktualizace vymění soubory pod běžícím procesem, takže nová verze se projeví
až po startu. Dřív si to člověk musel zavřít a otevřít sám a přišel při tom
o všechny otevřené taby. Tenhle modul to dělá za něj:

  1. `snapshot(hub)`  zapíše, co je otevřené — u agentů i id konverzace,
                      takže se dá pokračovat přes `claude --resume`
  2. `relaunch()`     pustí novou instanci a tuhle ukončí
  3. `take()`         nová instance si stav jednou přečte a soubor zahodí

Proč nová instance a ne `execv`: okno na Linuxu běží v tomhle procesu
(WebKitGTK) a jeho smyčka se z vlákna obsluhy požadavku bezpečně ukončit nedá.
Nový proces si otevře vlastní okno a tenhle pak může spadnout celý.

Terminály restart nepřežijí — pseudoterminál patří procesu, který umírá s ním.
Proto se neobnovuje „běžící program", ale konverzace: Claude Code naváže tam,
kde skončil, což je přesně to, o co při restartu jde.
"""
import json
import os
import subprocess
import sys
import threading
import time

from . import core

STATE_PATH = os.path.join(core.CLAUDE_DIR, "hub-restore.json")
LAUNCHER = os.path.join(core.CLAUDE_DIR, "claude-hub.py")

# Po aktualizaci: starší soubor nemá cenu obnovovat — když se hub nepustil
# hodinu, člověk už dávno dělá něco jiného a taby z minula by ho jen zmátly.
MAX_AGE = 3600
# Po zavření appky (počítač) nebo uspání prostoru (server) se ale pokračuje
# tam, kde člověk skončil, i druhý den nebo po víkendu.
MAX_AGE_CLOSED = 14 * 24 * 3600
REASONS = ("restart", "zavreni", "uspani")
# Uložené taby z minula, které si stránka ještě nevzala (take) — průběžné
# ukládání je do té doby nesmí přepsat prázdným stavem čerstvého startu.
_TAKEN = not os.path.exists(STATE_PATH)


def _resume_for(hub, session):
    """Id konverzace, kterou tab drží (nebo ''), ať se dá pokračovat.

    Stejná cesta jako u čtení (core.transcript_for): id, které si hub pro tab
    zvolil sám (--session-id), jinak značka HUB_TAB od Stop hooku a nakonec
    nejnovější přepis ze složky projektu. Bez toho prvního se dva taby nad
    jedním projektem po restartu vrátily do téže (nejnovější) konverzace.
    """
    try:
        transcript = core.transcript_for(session)
        if transcript:
            return os.path.basename(transcript)[:-6]
    except Exception:
        pass
    return session.resume or ""


# Konec přepisu, ve kterém se hledá, jestli Claude zrovna pracoval.
_KONEC_PREPISU = 4 * 1024 * 1024


def _je_zadani(entry):
    """Zadání od člověka (ne výsledek nástroje, meta řádek ani podkonverzace)."""
    if entry.get("type") != "user" or entry.get("isMeta") or entry.get("isSidechain") \
            or entry.get("isCompactSummary"):
        return False
    obsah = (entry.get("message") or {}).get("content")
    if isinstance(obsah, list) and any(isinstance(c, dict) and c.get("type") == "tool_result"
                                       for c in obsah):
        return False
    return True


def _fronta_a_prace(path, od_ms):
    """(zprávy z fronty, které Claude ještě neviděl; jestli byl uprostřed práce).

    Frontu drží Claude Code jen v paměti: co člověk poslal, zatímco Claude
    pracoval, restart smaže a v přepisu po tom zbyde jen `enqueue` bez
    `dequeue`/`remove`. Počítá se jen od startu tabu — starší ztracené zprávy
    už ukázalo čtení jako nedoručené a člověk je mezitím poslal znovu.
    """
    from . import cteni
    if not path or not os.path.isfile(path):
        return [], False
    cekaji, po_vyjmuti = [], False
    try:
        with open(path, "rb") as fh:
            for line in fh:
                fronta = b"queue-operation" in line[:200]
                if not fronta and (b'"user"' not in line or b"tool_use_id" in line):
                    continue
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if od_ms and cteni._ms(entry.get("timestamp")) < od_ms - 5000:
                    continue
                if not fronta:
                    if _je_zadani(entry):
                        # Zadání, které nepřišlo z fronty: fronta se mezitím
                        # ztratila jinak (a čtení to ukázalo).
                        if not po_vyjmuti:
                            cekaji = []
                        po_vyjmuti = False
                    continue
                # Ve frontě jsou i hlášky o úlohách a zprávy od jiných session —
                # vyjímají se ve stejném pořadí, takže se drží všechno a od
                # člověka se vybere až nakonec.
                op, obsah = entry.get("operation"), entry.get("content")
                if op == "enqueue":
                    cekaji.append(obsah)
                elif op == "remove":
                    if obsah in cekaji:
                        cekaji.remove(obsah)
                elif op == "dequeue":
                    if cekaji:
                        cekaji.pop(0)
                    po_vyjmuti = True
                elif op == "popAll":
                    cekaji = []
    except OSError:
        return [], False
    od_cloveka = [z for z in cekaji if isinstance(z, str) and "<task-notification>" not in z
                  and cteni._zprava_cloveka(z)]
    return [cteni._text_cloveka(z) for z in od_cloveka], _uprostred_prace(path, od_ms)


def _uprostred_prace(path, od_ms):
    """Skončil přepis uprostřed tahu — nástroj bez výsledku, výsledek bez
    odpovědi, zadání bez odpovědi? Pak restart Clauda přerušil v práci."""
    from . import cteni
    try:
        with open(path, "rb") as fh:
            fh.seek(max(0, os.path.getsize(path) - _KONEC_PREPISU))
            radky = fh.read().splitlines()
    except OSError:
        return False
    for line in reversed(radky):
        if b'"user"' not in line and b'"assistant"' not in line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        druh = entry.get("type")
        if druh not in ("user", "assistant") or entry.get("isSidechain") or entry.get("isMeta"):
            continue
        if od_ms and cteni._ms(entry.get("timestamp")) < od_ms - 5000:
            return False
        if druh == "assistant":
            if entry.get("isApiErrorMessage"):
                return False
            zprava = entry.get("message") or {}
            obsah = zprava.get("content") or []
            return zprava.get("stop_reason") == "tool_use" or any(
                isinstance(c, dict) and c.get("type") == "tool_use" for c in obsah)
        if not _je_zadani(entry):
            return True                     # výsledek nástroje bez odpovědi
        # Zadání bez odpovědi. „[Request interrupted…“ (Esc) a podobné
        # vynechá _text_cloveka — to zastavil člověk sám.
        return bool(cteni._text_cloveka((entry.get("message") or {}).get("content")))
    return False


def _pokracuj(session, reason="restart"):
    """Zpráva pro Clauda po obnovení tabu, nebo ''.

    Restart (aktualizace, uspání prostoru, pád) Claudovi bere tři věci, o
    kterých by sám nevěděl:
      - práci uprostřed tahu — stál by a čekal, až mu někdo napíše,
      - úlohy na pozadí (příkazy, hlídání, agenty) — jsou to děti procesu
        Claude Code, čekal by na hlášky, které už nepřijdou,
      - zprávy ve frontě — člověk je poslal, ale Claude je ještě neviděl.
    Úmyslné zavření appky na počítači práci nenavazuje (člověk ji zastavil),
    zprávy z fronty ale doručí — ty poslal, aby je Claude dostal.
    """
    try:
        from . import cteni
        cesta = core.transcript_for(session)
        od_ms = int(session.started * 1000)
        ulohy = cteni.pozadi(cesta, od_ms)
        zpravy, prerusen = _fronta_a_prace(cesta, od_ms)
    except Exception as exc:
        core.log_error("stav tabu pro obnovu", exc)
        return ""
    if reason == "zavreni":
        prerusen, ulohy = False, []
    zpravy = [z for z in zpravy if z.strip()]
    if not (ulohy or zpravy or prerusen):
        return ""
    kusy = ["[hub] Hub se mezitím restartoval (aktualizace nebo uspání prostoru)"
            + (" a přerušil tě uprostřed práce." if prerusen or ulohy else ".")]
    if ulohy:
        radky = []
        for u in ulohy[:8]:
            druh = {"Agent": "agent", "Monitor": "hlídání",
                    "ScheduleWakeup": "naplánované probuzení (ScheduleWakeup)"}.get(u.get("name"), "příkaz")
            co = (u.get("detail") or u.get("title") or "").strip().replace("\n", " ")
            radky.append(f"- {druh}: {co[:300]}")
        kusy.append("Tyhle úlohy na pozadí se tím zastavily — hlášky o jejich doběhnutí už "
                    "nepřijdou:\n" + "\n".join(radky) +
                    "\nZkontroluj, co z toho je pořád potřeba, a pusť to znovu.")
    if zpravy:
        # Oddělovače `---` čte i hub/cteni.py: podle nich pozná, které bubliny
        # z fronty tahle zpráva doručila.
        kusy.append("Uživatel ti mezitím poslal " +
                    ("zprávu, kterou" if len(zpravy) == 1 else "zprávy, které") +
                    " restart smazal z fronty — ještě jsi " +
                    ("ji" if len(zpravy) == 1 else "je") + " neviděl:\n" +
                    "".join(f"---\n{z.strip()}\n" for z in zpravy) + "---")
    kusy.append("Pokračuj tam, kde jsi skončil, a uživateli krátce oznam, že navazuješ."
                if prerusen or ulohy else "Odpověz na to.")
    return "\n\n".join(kusy)


def snapshot(hub, reason="restart", quiet=False):
    """Zapíše otevřené taby. Vrací, kolik jich bylo.

    `reason`: restart (aktualizace), zavreni (zavřená appka na počítači),
    uspani (brána zastavuje prostor) — podle toho jak starý stav se ještě
    obnoví a co stránka řekne."""
    if core.TEST_MODE:
        return 0
    tabs = []
    for session in list(hub.sessions.values()):
        if session.exited:
            continue
        agent = session.agent or ""
        # Konverzaci má smysl hledat jen u Clauda — ostatní agenti `--resume`
        # neumí a holý terminál nemá co obnovovat.
        resume = ""
        prompt = ""
        if session.kind == "project" or session.kind.startswith("slash:"):
            if (agent or core.default_agent()) == "claude":
                resume = _resume_for(hub, session)
                prompt = _pokracuj(session, reason)
        tabs.append({
            "kind": session.kind,
            "path": session.path or "",
            "title": session.title or "",
            "agent": agent,
            "model": session.model or "",
            "resume": resume,
            # Co Claude nechal běžet na pozadí — restart to zastaví, po
            # obnovení mu to hub připomene, ať to pustí znovu a pokračuje.
            "prompt": prompt,
            # Firemní tab (trezor firmy) má zůstat firemní i po restartu.
            "vault": getattr(session, "vault", "") or "",
        })
    data = {"at": int(time.time()), "version": core.version(), "tabs": tabs,
            "duvod": reason if reason in REASONS else "restart"}
    try:
        tmp = STATE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, STATE_PATH)
    except OSError as exc:
        core.log(f"restart: stav tabů se nepodařilo uložit: {exc}", "warn")
        return 0
    if not quiet:
        core.log(f"restart: uloženo {len(tabs)} tabů")
    return len(tabs)


def keep_saving(hub, reason, every=60):
    """Ukládá taby průběžně (vlákno). Zavření appky i uspání prostoru je uloží
    samo; tohle je pro pád, zabití procesu (nedostatek paměti) a vypnutý
    počítač — ať se i pak dá pokračovat. Nejvýš minuta zpátky."""
    while True:
        time.sleep(every)
        if not _TAKEN or core.TEST_MODE:
            continue
        try:
            snapshot(hub, reason, quiet=True)
        except Exception:
            pass


def take():
    """Přečte uložené taby a soubor smaže — obnovuje se jednou, ne pokaždé.
    Vrací (taby, důvod)."""
    global _TAKEN
    _TAKEN = True
    if core.TEST_MODE:
        return [], ""
    try:
        with open(STATE_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return [], ""
    try:
        os.remove(STATE_PATH)
    except OSError:
        pass
    if not isinstance(data, dict):
        return [], ""
    reason = data.get("duvod") if data.get("duvod") in REASONS else "restart"
    limit = MAX_AGE if reason == "restart" else MAX_AGE_CLOSED
    if time.time() - int(data.get("at") or 0) > limit:
        core.log("restart: uložené taby jsou staré, neobnovuju")
        return [], ""
    tabs = data.get("tabs")
    return (tabs if isinstance(tabs, list) else []), reason


def _spawn(*extra):
    """Pustí novou instanci hubu, odpojenou od téhle."""
    zdroj = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                         os.pardir, "claude-hub.py"))
    launcher = LAUNCHER if os.path.isfile(LAUNCHER) else zdroj
    if core.TEST_MODE:
        # Zkušební instance se restartuje sama ze sebe a zase jako zkušební —
        # ne přes nainstalovaný spouštěč, který by pustil skutečný hub.
        launcher = zdroj
        extra = (*extra, "--test")
    if not os.path.isfile(launcher):
        raise RuntimeError("Nenašel jsem claude-hub.py, ze kterého se hub pouští.")
    argv = [sys.executable, launcher, *extra]
    kwargs = {"cwd": core.HOME, "close_fds": True}
    if os.name == "nt":
        # Konzole bez okna (CREATE_NO_WINDOW), ne odpojený proces: v procesu bez
        # konzole si pywinpty u každého terminálu založí vlastní a na Windows 11
        # z ní zůstane okno terminálu (claude-hub.py, _bez_okna_konzole). Vlastní
        # skupina procesů — ať nová instance nezmizí s touhle.
        python = os.path.join(os.path.dirname(sys.executable or ""), "python.exe")
        if os.path.isfile(python):
            argv[0] = python
        kwargs["creationflags"] = 0x08000000 | 0x00000200
        kwargs["env"] = {**os.environ, "HUB_BEZ_OKNA_KONZOLE": "1", "HUB_RESTART": "1"}
        kwargs.update(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        kwargs["start_new_session"] = True
        # Nová instance pozná, že jde o restart (claude-hub.py), ne o druhé spuštění.
        kwargs["env"] = {**os.environ, "HUB_RESTART": "1"}
    subprocess.Popen(argv, **kwargs)


def _zavri_taby(hub):
    try:
        hub.shutdown(keep=False)
    except Exception:
        pass


def relaunch(hub, delay=0.6):
    """Uloží taby, pustí novou instanci a tuhle ukončí.

    Vrací hned, ať stihne odejít odpověď na požadavek — vypnutí běží ve vlákně.
    """
    count = snapshot(hub)

    def go():
        time.sleep(delay)
        # Značka „běžím na pozadí" pryč dřív než nová instance: jinak by se
        # připojila k téhle, která za chvíli skončí, a appka by zmizela.
        try:
            from . import pozadi, window
            stara = ""
            try:
                with open(pozadi.soubor(), encoding="utf-8") as fh:
                    stara = json.load(fh).get("url", "")
            except (OSError, ValueError):
                pass
            pozadi.smaz()
        except Exception:
            stara = ""
        try:
            # Nová instance okno sama neotvírá — stávající okno se na ni přepne
            # (rychlejší než zavřít okno a čekat, až Chromium otevře nové).
            # Okna appky (desktop/) si nová instance nesmí brát Chromem.
            _spawn("--vlastni-okno" if core.VLASTNI_OKNO else "--bez-okna")
        except Exception as exc:
            core.log(f"restart: nová instance nenaběhla: {exc}", "error")
            return
        # Počká, až nová instance odpovídá, a otevřené okno na ni přepne.
        # Když to nevyjde (nenaběhla včas), nová si okno otevře sama a tohle
        # se zavře.
        nova = ""
        try:
            # Na Windows start trvá i přes 10 s (antivir kontroluje Python) —
            # po 6 s se dřív okno zavřelo a nová instance pak otevřela další.
            konec_cekani = time.time() + 25
            while time.time() < konec_cekani:
                nova = pozadi.bezici_url()
                if nova:
                    break
                time.sleep(0.2)
        except Exception:
            nova = ""
        try:
            if nova:
                hub.broadcast({"t": "okno-jdi", "url": nova})
            elif not core.VLASTNI_OKNO:     # appka okna přepne sama, až nová naběhne
                hub.broadcast({"t": "okno-zavri"})
        except Exception:
            pass
        time.sleep(0.4)                     # ať zpráva stihne odejít
        core.log("restart: nová instance spuštěna, končím")
        # Okno, které se nepřepnulo (nová instance nenaběhla), se zavře — nová
        # si otevře vlastní. Jinak by tu zůstala dvě. Okna appky ne: ta si
        # novou instanci najdou sama (desktop/main.js, hlídač).
        if nova:
            core.log("restart: okno přepnuto na novou instanci")
        elif not core.VLASTNI_OKNO:
            try:
                n = window.zavri_okno(stara)
                if n:
                    core.log(f"restart: zavřeno staré okno ({n})")
            except Exception:
                pass
        # Terminály stejně umírají s procesem; tohle je pošle spát řízeně,
        # ať po sobě Claude Code stihne zavřít přepisy. Nejvýš 10 s: zavírání
        # terminálu se na Windows umí zaseknout a stará instance pak běžela dál
        # vedle nové (dvě instance, dvakrát okna — Adam Uher, 6. 11. 2026).
        konec = threading.Thread(target=lambda: _zavri_taby(hub), daemon=True)
        konec.start()
        konec.join(10)
        # os._exit, protože okno drží hlavní smyčku a obyčejný sys.exit by
        # z tohohle vlákna proces neukončil.
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(0)

    threading.Thread(target=go, daemon=True).start()
    return {"ok": True, "tabs": count}
