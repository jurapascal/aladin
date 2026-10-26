"""
Limity víc předplatných naráz — u každého, kolik zbývá z 5 hodin a z týdne.

Kdo má předplatných víc (Max v práci, Pro doma), potřebuje u každého vidět,
které má ještě volno, než na něj přepne. Čísla bere hub/usage.py (`fetch`)
přímo od Anthropicu; tady se jen drží, aby se pro každé předplatné ptalo
nejvýš jednou za pár minut, a ptá se na pozadí, aby stránka nečekala.

Používá to brána (předplatná v prostoru — tokeny z ní nikdy neodejdou, ven
jdou jen procenta) i hub na počítači (předplatná pro Claude Code na počítači,
hub/predplatne.py). Klíčem paměti je otisk tokenu, ne token.
"""
import hashlib
import threading
import time

TTL = 300           # tolik vteřin se limit drží
MIN_OBNOVA = 30     # „Obnovit“ dřív než po tolika vteřinách se neptá znovu
ZAHODIT = 86400     # limity dávno odebraných předplatných z paměti pryč
# Jen tohle jde ven — ať se do odpovědi nedostane nic, co fetch přidá navíc.
POLE = ("ok", "session", "week", "week_sonnet", "week_opus", "chyba", "druh")


def otisk(token):
    return hashlib.sha256(str(token or "").encode()).hexdigest()[:16]


class Limity:
    def __init__(self, ttl=TTL, fetch=None):
        self.ttl = ttl
        self._fetch = fetch
        self._lock = threading.Lock()
        self._pamet = {}            # otisk → {"t": kdy, "data": {...}}
        self._bezi = {}             # otisk → vlákno, které se na něj právě ptá

    def _zjisti(self, token, klic):
        try:
            fetch = self._fetch
            if fetch is None:
                from . import usage
                fetch = usage.fetch
            raw = fetch(token) or {}
            data = {k: raw.get(k) for k in POLE if k in raw}
        except Exception:
            data = {"ok": False, "druh": "chyba", "chyba": "Limit se teď nepodařilo zjistit."}
        data["cas"] = int(time.time())
        with self._lock:
            self._pamet[klic] = {"t": time.time(), "data": data}
            self._bezi.pop(klic, None)

    def zjisti(self, tokeny, cekat=0.0, obnovit=False):
        """`tokeny` = {id: token}. Vrací ({id: limit}, jestli se ještě počítá).

        Co je starší než TTL (nebo `obnovit`), se začne zjišťovat na pozadí;
        `cekat` = kolik vteřin nanejvýš počkat na čerstvá čísla."""
        ted = time.time()
        nova, vlakna = [], []
        with self._lock:
            for kl in [k for k, v in self._pamet.items() if ted - v["t"] > ZAHODIT]:
                del self._pamet[kl]
            for token in tokeny.values():
                kl = otisk(token)
                c = self._pamet.get(kl)
                stare = (not c or ted - c["t"] > self.ttl
                         or (obnovit and ted - c["t"] > MIN_OBNOVA))
                if stare and kl not in self._bezi:
                    t = threading.Thread(target=self._zjisti, args=(token, kl), daemon=True)
                    self._bezi[kl] = t
                    nova.append(t)
                if kl in self._bezi:
                    vlakna.append(self._bezi[kl])
        for t in nova:
            t.start()
        if cekat:
            # Počká i na dotazy, které začal dřívější požadavek.
            konec = ted + cekat
            for t in vlakna:
                t.join(max(0.0, konec - time.time()))
        out = {}
        with self._lock:
            for sid, token in tokeny.items():
                c = self._pamet.get(otisk(token))
                if c:
                    out[sid] = dict(c["data"])
            pocita = any(otisk(t) in self._bezi for t in tokeny.values())
        return out, pocita
