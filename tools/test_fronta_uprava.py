#!/usr/bin/env python3
"""Zkouška úpravy a smazání zprávy ve frontě (hub/fronta.py): stdlib, bez sítě,
bez zásahu do skutečného ~/.claude (CLAUDE_CONFIG_DIR = dočasná složka).

    python3 tools/test_fronta_uprava.py
"""
import json
import os
import random
import sys
import tempfile
import threading
import unittest

TMP = tempfile.mkdtemp(prefix="fronta-test-")
os.environ["CLAUDE_CONFIG_DIR"] = TMP
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from hub import core, fronta  # noqa: E402

assert core.CLAUDE_DIR.startswith(TMP), core.CLAUDE_DIR


class Pty:
    def __init__(self):
        self.zapsano = []

    def write(self, data):
        self.zapsano.append(bytes(data))


class Sess:
    id = 7
    kind = "project"
    exited = False
    uspany = False
    started = 0.0
    _fronta = None
    _vystup_at = 1.0
    chat_id = "chat1"
    resume = ""

    def __init__(self):
        self.pty = Pty()
        self.buffer = b""
        self.posledni = 0


def cisto():
    fronta._polozky.clear()
    fronta._pauza.clear()
    fronta._nacteno = True
    if os.path.exists(fronta.SOUBOR):
        os.remove(fronta.SOUBOR)


def ze_souboru():
    with open(fronta.SOUBOR, encoding="utf-8") as fh:
        return json.load(fh)["zpravy"]


class Zaklad(unittest.TestCase):
    def setUp(self):
        cisto()
        self.oznameno = []
        self.puvodni = (fronta._oznam, fronta._session, fronta._napis, fronta._pripraven,
                        fronta._v_prepisu, core.transcript_for)
        fronta._oznam = lambda s, p, stav, text=None: self.oznameno.append((p["id"], stav, text))
        self.s = Sess()
        fronta._session = lambda chat: self.s if chat == "chat1" else None
        fronta._napis = lambda s, text: s.pty.write(text.encode())
        fronta._v_prepisu = lambda cesta, p: False
        core.transcript_for = lambda s: "/nonexistent"

    def tearDown(self):
        (fronta._oznam, fronta._session, fronta._napis, fronta._pripraven,
         fronta._v_prepisu, core.transcript_for) = self.puvodni


class Uprava(Zaklad):
    def test_uprav_ceka(self):
        fronta.pridej("chat1", "ahoj", "a1")
        r = fronta.uprav("chat1", "a1", "nový text")
        self.assertTrue(r["ok"])
        self.assertEqual(r["text"], "nový text")
        self.assertEqual(ze_souboru()[0]["text"], "nový text")          # přežije restart hubu
        self.assertIn(("a1", "upraveno", "nový text"), self.oznameno)

    def test_restart_mezi_upravou_a_dorucenim(self):
        fronta.pridej("chat1", "ahoj", "a1")
        fronta.uprav("chat1", "a1", "po úpravě")
        fronta._polozky.clear()
        fronta._nacteno = False                                        # „restart“
        self.assertEqual(fronta.seznam("chat1")[0]["text"], "po úpravě")

    def test_stejny_text_bez_zmeny(self):
        fronta.pridej("chat1", "ahoj", "a1")
        r = fronta.uprav("chat1", "a1", "ahoj")
        self.assertTrue(r["ok"])
        self.assertFalse(r["zmena"])
        self.assertEqual(self.oznameno, [])

    def test_prazdny_text_zamitnut(self):
        fronta.pridej("chat1", "ahoj", "a1")
        for t in ("", "   \n", "\x00\x01\x02", None):
            self.assertFalse(fronta.uprav("chat1", "a1", t)["ok"], repr(t))
        self.assertEqual(fronta.seznam("chat1")[0]["text"], "ahoj")

    def test_obrovsky_text(self):
        fronta.pridej("chat1", "ahoj", "a1")
        self.assertFalse(fronta.uprav("chat1", "a1", "x" * 20001)["ok"])
        self.assertTrue(fronta.uprav("chat1", "a1", "x" * 20000)["ok"])
        self.assertFalse(fronta.uprav("chat1", "a1", "x" * 25000 + "\x00")["ok"])
        self.assertEqual(len(fronta.seznam("chat1")[0]["text"]), 20000)

    def test_znaky_nul_a_unicode(self):
        fronta.pridej("chat1", "ahoj", "a1")
        r = fronta.uprav("chat1", "a1", "a\x00b\x1b[31m\r\nč😀‮")
        self.assertTrue(r["ok"])
        for z in ("\x00", "\x1b", "\r"):
            self.assertNotIn(z, r["text"])
        self.assertEqual(ze_souboru()[0]["text"], r["text"])

    def test_prikaz_zamitnut(self):
        fronta.pridej("chat1", "ahoj", "a1")
        self.assertFalse(fronta.uprav("chat1", "a1", "/clear")["ok"])
        self.assertFalse(fronta.uprav("chat1", "a1", "  !ls")["ok"])

    def test_neexistujici_a_cizi_chat(self):
        fronta.pridej("chat1", "ahoj", "a1")
        self.assertEqual(fronta.uprav("chat1", "nic", "x")["stav"], "neni")
        self.assertEqual(fronta.uprav("chat2", "a1", "x")["stav"], "neni")
        self.assertEqual(fronta.smaz("chat1", "nic")["stav"], "neni")
        self.assertEqual(fronta.smaz("chat2", "a1")["stav"], "neni")
        self.assertEqual(len(fronta.seznam("chat1")), 1)

    def test_po_napsani_nejde(self):
        fronta.pridej("chat1", "ahoj", "a1")
        fronta._polozky[0].update(typed=123, stav="odeslano")
        for r in (fronta.uprav("chat1", "a1", "x"), fronta.smaz("chat1", "a1")):
            self.assertFalse(r["ok"])
            self.assertEqual(r["stav"], "odeslano")
            self.assertTrue(r["duvod"])
        fronta._polozky[0].update(stav="nedoslo")
        self.assertFalse(fronta.smaz("chat1", "a1")["ok"])
        self.assertEqual(fronta.seznam("chat1")[0]["text"], "ahoj")

    def test_smaz_a_dvojklik(self):
        fronta.pridej("chat1", "ahoj", "a1")
        self.assertTrue(fronta.smaz("chat1", "a1")["ok"])
        self.assertEqual(ze_souboru(), [])
        r = fronta.smaz("chat1", "a1")
        self.assertFalse(r["ok"])
        self.assertEqual(r["stav"], "neni")
        self.assertEqual([o for o in self.oznameno if o[1] == "smazano"], [("a1", "smazano", None)])

    def test_pauza_neprekazi(self):
        fronta.pridej("chat1", "ahoj", "a1")
        fronta.pauza("chat1", True)
        self.assertTrue(fronta.uprav("chat1", "a1", "b")["ok"])
        self.assertTrue(fronta.smaz("chat1", "a1")["ok"])

    def test_nejde_ulozit_vrati_zpet(self):
        fronta.pridej("chat1", "ahoj", "a1")
        puv = fronta._uloz
        fronta._uloz = lambda: False
        try:
            self.assertFalse(fronta.uprav("chat1", "a1", "b")["ok"])
            self.assertFalse(fronta.smaz("chat1", "a1")["ok"])
        finally:
            fronta._uloz = puv
        self.assertEqual(fronta.seznam("chat1")[0]["text"], "ahoj")

    def test_zrus_zustava_bool(self):
        fronta.pridej("chat1", "ahoj", "a1")
        self.assertIs(fronta.zrus("chat1", "a1"), True)
        self.assertIs(fronta.zrus("chat1", "a1"), False)

    def test_dve_okna_soucasne(self):
        fronta.pridej("chat1", "ahoj", "a1")
        vysl = []
        vlakna = [threading.Thread(target=lambda i=i: vysl.append(fronta.uprav("chat1", "a1", f"text {i}")))
                  for i in range(20)]
        for v in vlakna:
            v.start()
        for v in vlakna:
            v.join()
        self.assertTrue(all(r["ok"] for r in vysl))
        self.assertEqual(len(fronta.seznam("chat1")), 1)
        self.assertEqual(ze_souboru()[0]["text"], fronta.seznam("chat1")[0]["text"])

    def test_upravit_a_smazat_najednou(self):
        fronta.pridej("chat1", "ahoj", "a1")
        vysl = {}
        a = threading.Thread(target=lambda: vysl.update(u=fronta.uprav("chat1", "a1", "b")))
        b = threading.Thread(target=lambda: vysl.update(s=fronta.smaz("chat1", "a1")))
        a.start()
        b.start()
        a.join()
        b.join()
        self.assertTrue(vysl["s"]["ok"])
        self.assertEqual(ze_souboru(), [])


class Zavod(Zaklad):
    """Hlídací vlákno vs. okno: zpráva se nesmí napsat starým textem ani po smazání."""

    def _krok_s_zasahem(self, zasah):
        fronta.pridej("chat1", "starý", "a1")

        def pripraven(s, cesta):
            zasah()                                   # okno klikne přesně mezi snímkem a zápisem
            return True
        fronta._pripraven = pripraven
        fronta._krok()

    def test_uprava_tesne_pred_zapisem(self):
        self._krok_s_zasahem(lambda: fronta.uprav("chat1", "a1", "nový"))
        self.assertEqual(self.s.pty.zapsano, ["nový".encode()])
        self.assertGreater(ze_souboru()[0]["typed"], 0)

    def test_smazani_tesne_pred_zapisem(self):
        self._krok_s_zasahem(lambda: fronta.smaz("chat1", "a1"))
        self.assertEqual(self.s.pty.zapsano, [])
        self.assertEqual(ze_souboru(), [])

    def test_po_zapisu_uz_nejde(self):
        fronta.pridej("chat1", "ahoj", "a1")
        fronta._pripraven = lambda s, c: True
        fronta._krok()
        self.assertEqual(self.s.pty.zapsano, [b"ahoj"])
        r = fronta.uprav("chat1", "a1", "jiný")
        self.assertFalse(r["ok"])
        self.assertEqual(r["stav"], "odeslano")
        self.assertFalse(fronta.smaz("chat1", "a1")["ok"])

    def test_zapis_selhal_zprava_znovu_upravitelna(self):
        fronta.pridej("chat1", "ahoj", "a1")
        fronta._pripraven = lambda s, c: True

        def spadne(s, t):
            raise OSError("pty zaniklo")
        fronta._napis = spadne
        fronta._krok()
        self.assertEqual(fronta._polozky[0]["typed"], 0)
        self.assertEqual(fronta._polozky[0]["pokusy"], 0)
        self.assertTrue(fronta.uprav("chat1", "a1", "b")["ok"])

    def test_hon_vlaken(self):
        """Náhodné úpravy/smazání proti běžícímu _krok: nikdy dvakrát, nikdy starý text po úspěšné úpravě."""
        for _ in range(80):
            cisto()
            self.s.pty.zapsano.clear()
            fronta.pridej("chat1", "v0", "a1")
            fronta._pripraven = lambda s, c: True
            start = threading.Event()
            vysl = []

            def okno():
                start.wait()
                vysl.append(fronta.uprav("chat1", "a1", "v1"))
                if random.random() < 0.5:
                    vysl.append(fronta.smaz("chat1", "a1"))
            v = threading.Thread(target=okno)
            v.start()
            start.set()
            fronta._krok()
            v.join()
            self.assertLessEqual(len(self.s.pty.zapsano), 1)
            u = vysl[0]
            if self.s.pty.zapsano:
                if self.s.pty.zapsano[0] == b"v0":
                    self.assertFalse(u["ok"])         # napsáno staré = úprava musela být zamítnutá
                else:
                    self.assertTrue(u["ok"])
                for r in vysl[1:]:
                    self.assertFalse(r["ok"])         # po zápisu už smazat nejde
            else:
                self.assertTrue(all(r["ok"] for r in vysl))   # smazáno dřív, než se psalo


if __name__ == "__main__":
    unittest.main(verbosity=1)
