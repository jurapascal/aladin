#!/usr/bin/env python3
"""Zkouška, že obnovená konverzace (nový přepis s okopírovanou historií) nenahradí
seznam dvojicí, a že větev zůstane vedle původní (hub/chats.py). Bez sítě,
v dočasné složce — skutečné ~/.claude se nečte.   python3 tools/test_chaty_kopie.py"""
import json
import os
import sys
import tempfile
import unittest

TMP = tempfile.mkdtemp(prefix="chaty-kopie-test-")
os.environ["CLAUDE_CONFIG_DIR"] = TMP
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from hub import chats  # noqa: E402

PUVODNI = "aaaaaaaa-1111-1111-1111-111111111111"
KOPIE = "bbbbbbbb-2222-2222-2222-222222222222"
VETEV = "cccccccc-3333-3333-3333-333333333333"
JINY = "dddddddd-4444-4444-4444-444444444444"
PROJ = os.path.join(TMP, "projects", "-tmp-proj")
os.makedirs(PROJ)


def zapis(chat_id, radky, stari):
    cesta = os.path.join(PROJ, chat_id + ".jsonl")
    with open(cesta, "w", encoding="utf-8") as fh:
        for r in radky:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    os.utime(cesta, (stari, stari))


def zprava(chat_id, text, session_id=None):
    e = {"type": "user", "cwd": "/tmp", "sessionId": chat_id,
         "message": {"role": "user", "content": text}}
    if session_id:
        e["session_id"] = session_id
    return e


class Kopie(unittest.TestCase):
    def setUp(self):
        chats._CACHE.clear()
        for f in os.listdir(PROJ):
            os.remove(os.path.join(PROJ, f))

    def idecka(self):
        return [c["id"] for c in chats.list_chats()]

    def test_kopie_nahradi_puvodni(self):
        zapis(PUVODNI, [zprava(PUVODNI, "Aplikace Tendry")], 1000)
        zapis(KOPIE, [zprava(KOPIE, "Aplikace Tendry", PUVODNI),
                      zprava(KOPIE, "dál", None)], 2000)
        self.assertEqual(self.idecka(), [KOPIE])

    def test_vetev_zustane_vedle_puvodni(self):
        # větev přepisuje sessionId i session_id na nové, na původní neodkazuje
        zapis(PUVODNI, [zprava(PUVODNI, "Hlavní")], 1000)
        zapis(VETEV, [zprava(VETEV, "Hlavní", VETEV)], 2000)
        self.assertEqual(sorted(self.idecka()), sorted([PUVODNI, VETEV]))

    def test_puvodni_psana_po_kopii_zustane(self):
        zapis(KOPIE, [zprava(KOPIE, "Téma", PUVODNI)], 1000)
        zapis(PUVODNI, [zprava(PUVODNI, "Téma")], 3000)
        self.assertEqual(sorted(self.idecka()), sorted([PUVODNI, KOPIE]))

    def test_nesouvisejici_a_chybejici_rodic(self):
        zapis(JINY, [zprava(JINY, "Něco jiného")], 1500)
        zapis(KOPIE, [zprava(KOPIE, "Téma", PUVODNI)], 2000)      # původní už není
        self.assertEqual(sorted(self.idecka()), sorted([JINY, KOPIE]))

    def test_retez_kopii(self):
        zapis(PUVODNI, [zprava(PUVODNI, "Téma")], 1000)
        zapis(KOPIE, [zprava(KOPIE, "Téma", PUVODNI)], 2000)
        zapis(VETEV, [zprava(VETEV, "Téma", PUVODNI), zprava(VETEV, "x", KOPIE)], 3000)
        self.assertEqual(self.idecka(), [VETEV])

    def test_vystup_bez_pomocneho_pole(self):
        zapis(KOPIE, [zprava(KOPIE, "Téma", PUVODNI)], 2000)
        self.assertNotIn("parents", chats.list_chats()[0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
