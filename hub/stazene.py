"""
Stažené ve vestavěném prohlížeči — soubory končí v ~/Stažené a hub je nabídne.

Chromium (hub/prohlizec.py) běží bez okna, takže co Claude nebo člověk
v prohlížeči stáhne, by skončilo kdesi v profilu a člověk by o tom nevěděl.
Tenhle modul drží vlastní spojení na celý prohlížeč (CDP, cíl `browser`):
nastaví stahování do jasné složky (core.STAZENE_DIR), poslouchá
Browser.downloadWillBegin / downloadProgress, a když soubor dojede, ohlásí ho
všem oknům hubu (`{"t": "stazeno"}`) — ta ukážou „Stáhnout“ (hub.js, cteni.js).

Prohlížeč se tu nikdy nespouští ani neuspává (to dělá prohlizec.py), jen se
k běžícímu připojí. Když stahování nastaví po nás někdo jiný (Playwright
si ho při připojení přes CDP přepíná do své dočasné složky), soubor se
po dokončení zkopíruje do Stažené — cesta přijde v `filePath`.
"""
import json
import os
import shutil
import threading
import time

from . import core, prohlizec

MAX_POSLEDNICH = 20
_posledni = []                 # [{path, name, size, url, cas}] — nejnovější poslední
_lock = threading.Lock()


def posledni():
    with _lock:
        return list(reversed(_posledni))


def _volne_jmeno(slozka, jmeno):
    """Jméno, které ve složce ještě není (soubor.pdf → soubor (2).pdf)."""
    jmeno = os.path.basename(str(jmeno or "").replace("\\", "/")).strip() or "stazeny-soubor"
    jmeno = "".join(c for c in jmeno if c not in "\0/")[:180] or "stazeny-soubor"
    if jmeno.startswith("."):
        jmeno = "_" + jmeno
    stem, ext = os.path.splitext(jmeno)
    cesta, n = os.path.join(slozka, jmeno), 2
    while os.path.exists(cesta):
        cesta = os.path.join(slozka, f"{stem} ({n}){ext}")
        n += 1
    return cesta


def _dokonceno(info, par):
    """Kde soubor po dokončení je — a když mimo Stažené, kopie tam."""
    slozka = core.STAZENE_DIR
    jmeno = info.get("name") or ""
    kandidati = [par.get("filePath") or "",
                 os.path.join(slozka, info.get("guid") or "-"),     # allowAndName
                 os.path.join(slozka, os.path.basename(jmeno)) if jmeno else ""]
    zdroj = next((p for p in kandidati if p and os.path.isfile(p)), "")
    if not zdroj:
        return ""
    real = os.path.realpath(zdroj)
    if os.path.dirname(real) == os.path.realpath(slozka):
        if os.path.basename(real) == info.get("guid"):
            # Pojmenované podle guid (allowAndName) — přejmenovat na skutečné jméno.
            cil = _volne_jmeno(slozka, jmeno)
            os.replace(real, cil)
            return cil
        return real
    cil = _volne_jmeno(slozka, jmeno or os.path.basename(real))
    shutil.copyfile(real, cil)
    return cil


def _spojeni(oznam):
    ver = prohlizec._get("/json/version", 1.5) or {}
    url = ver.get("webSocketDebuggerUrl") or ""
    if not url:
        return
    os.makedirs(core.STAZENE_DIR, exist_ok=True)
    cdp = prohlizec._WS(url)
    rozjete = {}                       # guid → {name, url}
    try:
        cdp.send(json.dumps({"id": 1, "method": "Browser.setDownloadBehavior",
                             "params": {"behavior": "allow", "downloadPath": core.STAZENE_DIR,
                                        "eventsEnabled": True}}))
        while True:
            raw = cdp.recv()
            if raw is None:
                return
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            m, par = msg.get("method"), msg.get("params") or {}
            if m == "Browser.downloadWillBegin":
                rozjete[par.get("guid")] = {"guid": par.get("guid"), "name": par.get("suggestedFilename") or "",
                                            "url": str(par.get("url") or "")[:500]}
                if len(rozjete) > 200:
                    rozjete.pop(next(iter(rozjete)))
            elif m == "Browser.downloadProgress" and par.get("state") in ("completed", "canceled"):
                info = rozjete.pop(par.get("guid"), None) or {"guid": par.get("guid"), "name": "", "url": ""}
                if par.get("state") != "completed":
                    continue
                try:
                    cesta = _dokonceno(info, par)
                except OSError as exc:
                    core.log(f"stažené: soubor nejde uložit ({exc})")
                    continue
                if not cesta:
                    continue
                zaznam = {"path": cesta, "name": os.path.basename(cesta),
                          "size": os.path.getsize(cesta), "url": info.get("url") or "",
                          "cas": time.time()}
                with _lock:
                    _posledni.append(zaznam)
                    del _posledni[:-MAX_POSLEDNICH]
                core.log(f"stažené: {zaznam['name']} ({zaznam['size']} B)")
                oznam({"t": "stazeno", **zaznam})
    finally:
        cdp.close()


def hlidej(oznam):
    """Vlákno na pozadí: k běžícímu prohlížeči se připojí, jinak čeká."""
    while True:
        try:
            if prohlizec.alive():
                _spojeni(oznam)
        except Exception:
            pass                       # prohlížeč skončil nebo spí — zkusí se znovu
        time.sleep(4.0)


def spust(oznam):
    # Na sdíleném serveru bez povolení prohlížeče (prohlizec.povoleno) se ani
    # nepřipojovat: na loopbacku by mohl být ladicí port cizího prostoru.
    if not prohlizec.povoleno(prohlizec.na_serveru()):
        return
    threading.Thread(target=hlidej, args=(oznam,), name="stazene", daemon=True).start()
