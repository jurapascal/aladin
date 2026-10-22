#!/usr/bin/env python3
"""Server nesmí psát zprávu do terminálu, dokud do něj před chvílí psalo okno
(automatické /model a /effort, odpověď na dotaz): dva zapisovače naráz se
v poli Claude Code proplétají. Spuštění: python3 tools/test_fronta_okno.py"""
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from hub import fronta  # noqa: E402


class Falesna:
    uspany = False
    _fronta = None

    def __init__(self, vstup_pred=None):
        self.started = time.time() - 60
        self._vystup_at = time.time() - 30
        self._vstup_at = 0.0 if vstup_pred is None else time.time() - vstup_pred


class OknoPise(unittest.TestCase):
    cesta = "/nonexistent/prepis.jsonl"

    def test_klid_pripraven(self):
        self.assertTrue(fronta._pripraven(Falesna(), self.cesta))

    def test_okno_pise_prave_ted(self):
        self.assertFalse(fronta._pripraven(Falesna(vstup_pred=0.1), self.cesta))

    def test_okno_psalo_pred_chvili(self):
        self.assertFalse(fronta._pripraven(Falesna(vstup_pred=fronta.VSTUP_KLID_S - 0.5), self.cesta))

    def test_okno_psalo_davno(self):
        self.assertTrue(fronta._pripraven(Falesna(vstup_pred=fronta.VSTUP_KLID_S + 1), self.cesta))

    def test_session_bez_atributu(self):
        s = Falesna()
        del s._vstup_at                         # starší objekt bez pole nesmí spadnout
        self.assertTrue(fronta._pripraven(s, self.cesta))

    def test_za_behu_taky_ceka(self):
        s = Falesna(vstup_pred=0.2)
        self.assertTrue(fronta._okno_pise(s))


if __name__ == "__main__":
    unittest.main()
