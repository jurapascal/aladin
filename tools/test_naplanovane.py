#!/usr/bin/env python3
"""Naplánované úlohy (hub/naplanovane.py): další termín, dohánění propadlých
běhů a spuštění do nového tabu. Spuštění: python3 tools/test_naplanovane.py"""
import datetime as dt
import os
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from hub import core, naplanovane as N  # noqa: E402

# Pátek 27. 11. 2026 10:30
PA = dt.datetime(2026, 11, 27, 10, 30)


class Termin(unittest.TestCase):
    def test_denne_dnes_pozdeji(self):
        self.assertEqual(N.dalsi_beh({"typ": "denne", "cas": "14:00"}, PA), PA.replace(hour=14, minute=0))

    def test_denne_uz_bylo_zitra(self):
        self.assertEqual(N.dalsi_beh({"typ": "denne", "cas": "07:00"}, PA),
                         dt.datetime(2026, 11, 28, 7, 0))

    def test_presne_ted_neni_dalsi(self):
        self.assertEqual(N.dalsi_beh({"typ": "denne", "cas": "10:30"}, PA),
                         dt.datetime(2026, 11, 28, 10, 30))

    def test_pracovni_preskoci_vikend(self):
        self.assertEqual(N.dalsi_beh({"typ": "pracovni", "cas": "07:00"}, PA),
                         dt.datetime(2026, 11, 30, 7, 0))       # pondělí

    def test_tydne_vybrane_dny(self):
        self.assertEqual(N.dalsi_beh({"typ": "tydne", "cas": "09:00", "dny": [2]}, PA),
                         dt.datetime(2026, 12, 2, 9, 0))        # středa

    def test_hodinove(self):
        self.assertEqual(N.dalsi_beh({"typ": "hodinove", "cas": "00:15"}, PA), PA.replace(hour=11, minute=15))
        self.assertEqual(N.dalsi_beh({"typ": "hodinove", "cas": "00:45"}, PA), PA.replace(minute=45))

    def test_hodinove_jen_v_pondeli(self):
        self.assertEqual(N.dalsi_beh({"typ": "hodinove", "cas": "00:00", "dny": [0]}, PA),
                         dt.datetime(2026, 11, 30, 0, 0))

    def test_mesicne_pres_konec_roku(self):
        self.assertEqual(N.dalsi_beh({"typ": "mesicne", "cas": "08:00", "den": 1},
                                     dt.datetime(2026, 12, 5, 9, 0)), dt.datetime(2027, 1, 1, 8, 0))

    def test_nesmysl_se_opravi(self):
        self.assertEqual(N._cisty_kdy({"typ": "x", "cas": "99:99", "dny": ["a", 9]}),
                         {"typ": "denne", "cas": "08:00"})

    def test_popis(self):
        self.assertEqual(N.popis({"typ": "tydne", "cas": "09:05", "dny": [0, 2]}), "po, st v 9:05")


class FalesnaSession:
    def __init__(self, sid, title):
        self.id, self.title, self.exited, self.vlastni_nazev = sid, title, False, False

    def info(self):
        return {"id": self.id, "title": self.title}


class FalesnyHub:
    def __init__(self):
        self.sessions, self.otevreno, self.zpravy = {}, [], []

    def open(self, kind, path, title, cols, rows, **kw):
        s = FalesnaSession(len(self.sessions) + 1, title)
        self.sessions[s.id] = s
        self.otevreno.append((path, title, kw.get("prompt"), kw.get("model")))
        return s

    def broadcast(self, msg):
        self.zpravy.append(msg)


class Ulohy(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self._claude = core.CLAUDE_DIR
        core.CLAUDE_DIR = self.dir

    def tearDown(self):
        core.CLAUDE_DIR = self._claude
        shutil.rmtree(self.dir, ignore_errors=True)

    def nova(self, **kw):
        return N.uloz({"nazev": "Ranní", "zadani": "Projdi objednávky", "slozka": self.dir,
                       "kdy": {"typ": "denne", "cas": "07:00"}, **kw})

    def test_uloz_a_seznam(self):
        u = self.nova()
        self.assertEqual([x["id"] for x in N.seznam()], [u["id"]])
        self.assertEqual(u["popis"], "každý den v 7:00")
        self.assertTrue(u["dalsi"] > time.time())

    def test_bez_zadani_ne(self):
        with self.assertRaises(N.Chyba):
            N.uloz({"zadani": "  ", "slozka": self.dir})

    def test_neexistujici_slozka_ne(self):
        with self.assertRaises(N.Chyba):
            self.nova(slozka=os.path.join(self.dir, "neni"))

    def test_splatna_po_terminu(self):
        u = self.nova()
        od = dt.datetime.fromtimestamp(u["od"])
        # Den poté v 7:01 — termín 7:00 je splatný.
        zitra = (od + dt.timedelta(days=1)).replace(hour=7, minute=1)
        self.assertEqual(N._splatne(zitra), [u["id"]])

    def test_dlouho_propadla_se_nedohani(self):
        u = self.nova()
        od = dt.datetime.fromtimestamp(u["od"])
        pozde = (od + dt.timedelta(days=3)).replace(hour=23)
        self.assertEqual(N._splatne(pozde), [])
        self.assertEqual(N._splatne(pozde), [])            # a podruhé už vůbec ne

    def test_vypnuta_nebezi(self):
        u = self.nova(zapnuto=False)
        od = dt.datetime.fromtimestamp(u["od"])
        self.assertEqual(N._splatne((od + dt.timedelta(days=1)).replace(hour=7, minute=1)), [])

    def test_spust_otevre_tab_a_nepusti_dvakrat(self):
        u = self.nova(model="sonnet")
        hub = FalesnyHub()
        N.spust(u["id"], hub)
        self.assertEqual(hub.otevreno, [(self.dir, "🕒 Ranní", "Projdi objednávky", "sonnet")])
        self.assertEqual(hub.zpravy[0]["t"], "tab-opened")
        # Minulý běh pořád pracuje → druhý se nespustí.
        orig = N._bezi_jeste
        N._bezi_jeste = lambda h, x: True
        try:
            with self.assertRaises(N.Chyba):
                N.spust(u["id"], hub)
        finally:
            N._bezi_jeste = orig
        self.assertEqual(N.seznam()[0]["behu"], 1)

    def test_smaz(self):
        u = self.nova()
        N.smaz(u["id"])
        self.assertEqual(N.seznam(), [])


if __name__ == "__main__":
    unittest.main()
