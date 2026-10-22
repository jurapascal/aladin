#!/usr/bin/env python3
"""Zkouška hledání v konverzacích a exportu (hub/hledani.py). Bez sítě, v
dočasné složce — skutečné ~/.claude se nečte.   python3 tools/test_hledani.py"""
import json
import os
import sys
import tempfile
import threading
import time
import unittest

TMP = tempfile.mkdtemp(prefix="hledani-test-")
os.environ["CLAUDE_CONFIG_DIR"] = TMP
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from hub import hledani  # noqa: E402

ID1 = "11111111-1111-1111-1111-111111111111"
ID2 = "22222222-2222-2222-2222-222222222222"
ID3 = "33333333-3333-3333-3333-333333333333"
PROJ = os.path.join(TMP, "projects", "-home-test")
os.makedirs(PROJ)


def user(text, ts="2026-10-01T10:00:00Z"):
    return json.dumps({"type": "user", "timestamp": ts, "cwd": "/tmp",
                       "message": {"role": "user", "content": text}}, ensure_ascii=False)


def asst(text, ts="2026-10-01T10:00:05Z", extra=()):
    return json.dumps({"type": "assistant", "timestamp": ts, "message": {
        "role": "assistant", "content": [{"type": "text", "text": text}, *extra]}}, ensure_ascii=False)


def zapis(chat_id, radky, raw=b""):
    with open(os.path.join(PROJ, chat_id + ".jsonl"), "wb") as fh:
        for r in radky:
            fh.write(r.encode("utf-8") if isinstance(r, str) else r)
            fh.write(b"\n")
        fh.write(raw)


def hledej(q, nastroje=False, cekat=20):
    j = hledani.spust(q, nastroje)["job"]
    konec = time.time() + cekat
    while time.time() < konec:
        s = hledani.stav(j)
        if s is None or s["hotovo"]:
            return s
        time.sleep(0.02)
    raise AssertionError("hledání neskončilo")


class T(unittest.TestCase):
    def setUp(self):
        for n in os.listdir(PROJ):
            os.remove(os.path.join(PROJ, n))

    def test_diakritika_a_velikost(self):
        zapis(ID1, [user("Příliš žluťoučký kůň"), asst("Odpověď o ŠTÍHLÉM koni")])
        for q in ("prilis", "PŘÍLIŠ", "zlutoucky kun", "stihlem"):
            s = hledej(q)
            self.assertEqual(len(s["vysledky"]), 1, q)
        s = hledej("prilis")
        n = s["vysledky"][0]["nalezy"][0]
        self.assertEqual(n["role"], "me")
        a, d = n["ranges"][0]
        self.assertEqual(n["text"][a:a + d], "Příliš")

    def test_min_delka_a_zle_dotazy(self):
        for q in ("", "a", " a ", "a b", None, "x" * 500):
            with self.assertRaises(ValueError):
                hledani.spust(q)

    def test_poskozene_radky(self):
        zapis(ID1, ["{rozbitý json", "\x00\x01\x02binární", user("platná zpráva s kočkou"),
                    b"\xff\xfe\xfa user assistant {", "[1,2,3]", "{" * 5000],
              raw=user("rozpůlená kočka")[:30].encode())
        s = hledej("kočkou")
        self.assertEqual(s["vysledky"][0]["pocet"], 1)
        self.assertFalse(s["chyba"])

    def test_obri_radek(self):
        obr = user("kocka " + "x" * (3 * 1024 * 1024))
        zapis(ID1, [obr, user("druhá kocka")])
        s = hledej("kocka")
        self.assertEqual(s["vysledky"][0]["pocet"], 1)     # obří přeskočen, další nalezen

    def test_hluboke_vnoreni(self):
        zapis(ID1, ['{"type":"user","message":{"content":' + "[" * 90000 + "]" * 90000 + "}}", user("kocka")])
        self.assertEqual(len(hledej("kocka")["vysledky"]), 1)

    def test_nastroje_jen_na_pozadani(self):
        vys = {"type": "user", "message": {"content": [
            {"type": "tool_result", "content": "tajnyvypis"}]}}
        zapis(ID1, [json.dumps(vys)])
        self.assertEqual(len(hledej("tajnyvypis")["vysledky"]), 0)
        self.assertEqual(len(hledej("tajnyvypis", True)["vysledky"]), 1)

    def test_system_reminder_se_nehleda(self):
        zapis(ID1, [user("ahoj <system-reminder>skrytyobsah</system-reminder>")])
        self.assertEqual(len(hledej("skrytyobsah")["vysledky"]), 0)

    def test_limity(self):
        for i in range(60):
            zapis("%08d-1111-1111-1111-111111111111" % i, [user("hledane slovo " * 5)])
        s = hledej("hledane")
        self.assertLessEqual(len(s["vysledky"]), hledani.LIMIT_CHATU)
        self.assertTrue(s["castecne"])
        self.assertLessEqual(len(s["vysledky"][0]["nalezy"]), hledani.LIMIT_NALEZU)

    def test_neexistujici_projekty(self):
        real = hledani.core.CLAUDE_DIR
        try:
            hledani.core.CLAUDE_DIR = os.path.join(TMP, "nic")
            s = hledej("kocka")
            self.assertTrue(s["hotovo"])
            self.assertEqual(s["vysledky"], [])
        finally:
            hledani.core.CLAUDE_DIR = real

    def test_fifo_a_odkaz_nezablokuje(self):
        os.mkfifo(os.path.join(PROJ, ID2 + ".jsonl"))
        os.symlink("/nonexistent", os.path.join(PROJ, ID3 + ".jsonl"))
        zapis(ID1, [user("kocka")])
        self.assertEqual(len(hledej("kocka")["vysledky"]), 1)

    def test_soubor_mizi(self):
        zapis(ID1, [user("kocka")])
        orig = hledani._projdi_soubor

        def mizici(path, *a):
            os.remove(path)
            return orig(path, *a)
        hledani._projdi_soubor = mizici
        try:
            s = hledej("kocka")
        finally:
            hledani._projdi_soubor = orig
        self.assertTrue(s["hotovo"])
        self.assertFalse(s["chyba"])

    def test_novy_dotaz_zrusi_stary(self):
        zapis(ID1, [user("kocka")])
        j1 = hledani.spust("kocka")["job"]
        j2 = hledani.spust("pes")["job"]
        self.assertIsNone(hledani.stav(j1))
        self.assertIsNotNone(hledani.stav(j2))
        self.assertEqual(sum(t.name.startswith("Thread") and t.is_alive() for t in threading.enumerate()) < 5, True)

    def test_casovy_limit(self):
        zapis(ID1, [user("nic")] * 5)
        old = hledani.LIMIT_CASU
        hledani.LIMIT_CASU = -1
        try:
            s = hledej("kocka")
        finally:
            hledani.LIMIT_CASU = old
        self.assertTrue(s["castecne"])

    def test_velky_soubor_rychlost(self):
        radek = user("běžná zpráva o ničem " * 20)
        with open(os.path.join(PROJ, ID1 + ".jsonl"), "w", encoding="utf-8") as fh:
            for _ in range(30000):
                fh.write(radek + "\n")
            fh.write(user("jehla v kupce sena") + "\n")
        t = time.time()
        s = hledej("jehla")
        self.assertEqual(len(s["vysledky"]), 1)
        self.assertLess(time.time() - t, 20)   # zátěž stroje kolísá

    def test_nazev_souboru(self):
        for t in ("../../etc/passwd", "a/b\\c", "..", "", None, "x" * 500, "Příliš\x00žluťoučký\n"):
            n = hledani.nazev_souboru(t, ID1)
            self.assertNotIn("/", n)
            self.assertNotIn("\\", n)
            self.assertNotIn("..", n)
            self.assertNotIn("\x00", n)
            self.assertTrue(n.endswith(".md"))
            self.assertLess(len(n), 90)
            self.assertFalse(n.startswith("."))

    def test_export(self):
        tool = {"type": "tool_use", "name": "Read", "input": {"file_path": "/etc/hosts"}}
        zapis(ID1, [user("první otázka"), asst("odpověď `kód`", extra=[tool]),
                    user("<system-reminder>x</system-reminder>"), "{rozbité", user("heslo=abc123")],
              raw=b'{"type":"user","mess')
        fn, data = hledani.export_md(ID1)
        txt = data.decode("utf-8")
        self.assertIn("## Ty", txt)
        self.assertIn("## Claude", txt)
        self.assertIn("první otázka", txt)
        self.assertIn("heslo=abc123", txt)             # nic se nemaskuje
        self.assertIn("nástroj: `Read: /etc/hosts`", txt)
        self.assertNotIn("system-reminder", txt)
        self.assertTrue(fn.endswith(".md"))

    def test_export_neexistuje_a_zly_id(self):
        for cid in (ID2, "../../x", "", None):
            with self.assertRaises(ValueError):
                hledani.export_md(cid)

    def test_export_velikost(self):
        old = hledani.EXPORT_MAX
        hledani.EXPORT_MAX = 2000
        try:
            zapis(ID1, [user("řádek %d " % i + "x" * 100) for i in range(200)])
            fn, data = hledani.export_md(ID1)
        finally:
            hledani.EXPORT_MAX = old
        self.assertLess(len(data), 3000)
        self.assertIn("zkrácený", data.decode())


if __name__ == "__main__":
    unittest.main(verbosity=1)
