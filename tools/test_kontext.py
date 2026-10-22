#!/usr/bin/env python3
"""Zkouška ukazatele kontextu (hub/kontext.py) — snaží se ho shodit.

Spuštění: python3 tools/test_kontext.py   (stdlib, bez sítě, jen dočasná složka)
"""
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from hub import kontext  # noqa: E402


def asi(usage, model="claude-opus-5-5", **extra):
    e = {"type": "assistant", "message": {"model": model, "usage": usage}}
    e.update(extra)
    return json.dumps(e)


def user(text="ahoj"):
    return json.dumps({"type": "user", "message": {"content": text}})


class Zaklad(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="kontext-")
        self.n = 0
        kontext._CACHE.clear()
        kontext._JE_1M.clear()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def soubor(self, radky, konec="\n", raw=None):
        self.n += 1
        p = os.path.join(self.dir, "s %d.jsonl" % self.n)   # mezera v cestě schválně
        with open(p, "wb") as fh:
            fh.write(raw if raw is not None else ("\n".join(radky) + konec).encode())
        return p


class Soucet(Zaklad):
    def test_soucet_vstupu(self):
        p = self.soubor([user(), asi({"input_tokens": 3, "cache_read_input_tokens": 1000,
                                      "cache_creation_input_tokens": 200, "output_tokens": 999})])
        r = kontext.stav(p)
        self.assertEqual(r["tokens"], 1203)          # výstup se nepočítá
        self.assertEqual(r["window"], 200000)
        self.assertFalse(r["sure"])                  # okno je odhad

    def test_posledni_vyhrava(self):
        p = self.soubor([asi({"input_tokens": 10}), asi({"input_tokens": 50})])
        self.assertEqual(kontext.stav(p)["tokens"], 50)

    def test_chybejici_pole(self):
        p = self.soubor([asi({"cache_read_input_tokens": 7})])
        self.assertEqual(kontext.stav(p)["tokens"], 7)

    def test_divna_pole(self):
        p = self.soubor([asi({"input_tokens": 5}),
                         asi({"input_tokens": "x", "cache_read_input_tokens": None,
                              "cache_creation_input_tokens": -9, "output_tokens": [1]}),
                         asi({"input_tokens": True, "cache_read_input_tokens": {}})])
        # všechny novější záznamy dávají 0 → bere se poslední použitelný
        self.assertEqual(kontext.stav(p)["tokens"], 5)

    def test_usage_neni_slovnik(self):
        for u in (None, 5, "x", [1, 2]):
            p = self.soubor([asi({"input_tokens": 9}), json.dumps(
                {"type": "assistant", "message": {"model": "m", "usage": u}})])
            self.assertEqual(kontext.stav(p)["tokens"], 9)

    def test_message_neni_slovnik(self):
        p = self.soubor([asi({"input_tokens": 9}),
                         json.dumps({"type": "assistant", "message": "text usage"})])
        self.assertEqual(kontext.stav(p)["tokens"], 9)

    def test_bez_usage(self):
        p = self.soubor([user(), json.dumps({"type": "assistant", "message": {"content": []}})])
        r = kontext.stav(p)
        self.assertIsNone(r["tokens"])

    def test_sidechain_se_nepocita(self):
        p = self.soubor([asi({"input_tokens": 100}),
                         asi({"input_tokens": 99999}, isSidechain=True)])
        self.assertEqual(kontext.stav(p)["tokens"], 100)

    def test_synthetic_a_chyba_api(self):
        p = self.soubor([asi({"input_tokens": 100}),
                         asi({"input_tokens": 5}, model="<synthetic>"),
                         asi({"input_tokens": 6}, isApiErrorMessage=True)])
        self.assertEqual(kontext.stav(p)["tokens"], 100)

    def test_prazdne_usage_se_preskoci(self):
        p = self.soubor([asi({"input_tokens": 100}), asi({"input_tokens": 0, "output_tokens": 0})])
        self.assertEqual(kontext.stav(p)["tokens"], 100)


class Soubory(Zaklad):
    def test_nenalezeny_a_prazdny(self):
        self.assertIsNone(kontext.stav("")["tokens"])
        self.assertIsNone(kontext.stav(None)["tokens"])
        self.assertIsNone(kontext.stav(os.path.join(self.dir, "../nic.jsonl"))["tokens"])
        self.assertIsNone(kontext.stav(self.dir)["tokens"])          # složka
        self.assertIsNone(kontext.stav("a\x00b")["tokens"])          # nulový bajt v cestě
        self.assertIsNone(kontext.stav(self.soubor([], raw=b""))["tokens"])

    def test_rozpulena_posledni_radka(self):
        dobry = asi({"input_tokens": 42})
        pul = asi({"input_tokens": 777})[:40]
        p = self.soubor([dobry, pul], konec="")
        self.assertEqual(kontext.stav(p)["tokens"], 42)

    def test_rozpulena_prvni_radka_v_ocasu(self):
        # soubor větší než první čtený kus: ocas začíná uprostřed řádku
        velky = user("x" * 300000)
        p = self.soubor([asi({"input_tokens": 1}), velky, asi({"input_tokens": 2}), velky])
        r = kontext.stav(p)
        self.assertEqual(r["tokens"], 2)      # poslední odpověď je za velkým řádkem

    def test_odpoved_dal_nez_prvni_kus(self):
        velky = user("x" * (600 * 1024))
        p = self.soubor([asi({"input_tokens": 11}), velky, velky])
        self.assertEqual(kontext.stav(p)["tokens"], 11)

    def test_odpoved_je_jen_uvnitr_obrovskeho_souboru_mimo_ocas(self):
        velky = user("x" * (9 * 1024 * 1024))
        p = self.soubor([asi({"input_tokens": 11}), velky])
        self.assertIsNone(kontext.stav(p)["tokens"])     # nečte se celý soubor, hlásí nic

    def test_smeti_a_nul_bajty(self):
        raw = b"\x00\x00\xff\xfe nesmysl\n" + asi({"input_tokens": 8}).encode() + b"\n\x00\x00{{{\n"
        self.assertEqual(kontext.stav(self.soubor([], raw=raw))["tokens"], 8)

    def test_unicode(self):
        p = self.soubor([asi({"input_tokens": 3}),
                         user("Příliš žluťoučký kůň 🐎   \udcff".encode("utf-8", "replace").decode())])
        self.assertEqual(kontext.stav(p)["tokens"], 3)

    def test_json_ne_slovnik(self):
        p = self.soubor(["[1,2]", "null", '"assistant usage"', asi({"input_tokens": 4})])
        self.assertEqual(kontext.stav(p)["tokens"], 4)

    def test_neexistuje_po_smazani(self):
        p = self.soubor([asi({"input_tokens": 4})])
        kontext.stav(p)
        os.remove(p)
        self.assertIsNone(kontext.stav(p)["tokens"])

    @unittest.skipIf(os.geteuid() == 0, "root čte všechno")
    def test_bez_prav(self):
        p = self.soubor([asi({"input_tokens": 4})])
        os.chmod(p, 0)
        try:
            self.assertIsNone(kontext.stav(p)["tokens"])
        finally:
            os.chmod(p, 0o600)


class Okno(Zaklad):
    def test_nad_200k_je_1m(self):
        p = self.soubor([asi({"input_tokens": 250000})])
        r = kontext.stav(p)
        self.assertEqual((r["window"], r["sure"]), (1000000, True))

    def test_lepkave_1m(self):
        p = self.soubor([asi({"input_tokens": 250000})])
        kontext.stav(p)
        with open(p, "a") as fh:
            fh.write(asi({"input_tokens": 30000}) + "\n")
        r = kontext.stav(p)                      # zase pod 200k, ale okno už je prokázané
        self.assertEqual((r["tokens"], r["window"]), (30000, 1000000))

    def test_hint_a_nastaveni(self):
        p = self.soubor([asi({"input_tokens": 1000})])
        self.assertEqual(kontext.stav(p, hint_1m=True)["window"], 1000000)
        p2 = self.soubor([asi({"input_tokens": 1000})])
        self.assertEqual(kontext.stav(p2, nastaveni_1m=True)["window"], 1000000)

    def test_neznamy_model_bez_procent(self):
        p = self.soubor([asi({"input_tokens": 1000}, model="gpt-9")])
        r = kontext.stav(p)
        self.assertEqual((r["tokens"], r["window"]), (1000, 0))

    def test_neznamy_model_ani_s_hintem_1m(self):
        p = self.soubor([asi({"input_tokens": 300000}, model="gpt-9")])
        self.assertEqual(kontext.stav(p, hint_1m=True, nastaveni_1m=True)["window"], 0)

    def test_model_bez_jmena(self):
        p = self.soubor([json.dumps({"type": "assistant", "message": {"usage": {"input_tokens": 5}}})])
        r = kontext.stav(p)
        self.assertEqual(r["tokens"], 5)       # jméno modelu chybí → základní odhad, ne pád
        self.assertFalse(r["sure"])


class Compact(Zaklad):
    def hranice(self, pre=300000):
        return json.dumps({"type": "system", "subtype": "compact_boundary",
                           "compactMetadata": {"trigger": "auto", "preTokens": pre}})

    def test_po_compactu_se_cislo_srovna(self):
        p = self.soubor([asi({"input_tokens": 300000}), self.hranice(),
                         json.dumps({"type": "user", "isCompactSummary": True})])
        r = kontext.stav(p)
        self.assertTrue(r["compact"])
        self.assertIsNone(r["tokens"])          # staré číslo se neukazuje
        self.assertEqual(r["window"], 1000000)  # pre 300k okno prokázalo

    def test_dalsi_odpoved_cislo_obnovi(self):
        p = self.soubor([asi({"input_tokens": 300000}), self.hranice(),
                         asi({"input_tokens": 28000})])
        r = kontext.stav(p)
        self.assertFalse(r["compact"])
        self.assertEqual(r["tokens"], 28000)

    def test_summary_bez_boundary(self):
        p = self.soubor([asi({"input_tokens": 90}), json.dumps({"type": "user", "isCompactSummary": True})])
        self.assertTrue(kontext.stav(p)["compact"])

    def test_text_zminujici_compact_neni_compact(self):
        p = self.soubor([asi({"input_tokens": 90}),
                         user('mluvím o "compact_boundary" a "isCompactSummary"')])
        r = kontext.stav(p)
        self.assertFalse(r["compact"])
        self.assertEqual(r["tokens"], 90)

    def test_pre_tokens_nesmysl(self):
        p = self.soubor([self.hranice("abc")])
        self.assertTrue(kontext.stav(p)["compact"])
        p = self.soubor([json.dumps({"type": "system", "subtype": "compact_boundary",
                                     "compactMetadata": "x"})])
        self.assertTrue(kontext.stav(p)["compact"])


class Cache(Zaklad):
    def test_cache_podle_mtime_a_velikosti(self):
        p = self.soubor([asi({"input_tokens": 1})])
        self.assertEqual(kontext.stav(p)["tokens"], 1)
        orig = kontext._najdi
        kontext._najdi = lambda *_: self.fail("měla se použít cache")
        try:
            self.assertEqual(kontext.stav(p)["tokens"], 1)
        finally:
            kontext._najdi = orig
        with open(p, "a") as fh:
            fh.write(asi({"input_tokens": 2}) + "\n")
        self.assertEqual(kontext.stav(p)["tokens"], 2)

    def test_cache_nema_neomezeny_rust(self):
        for i in range(kontext._MAX_CACHE + 20):
            kontext.stav(self.soubor([asi({"input_tokens": i + 1})]))
        self.assertLessEqual(len(kontext._CACHE), kontext._MAX_CACHE)

    def test_vysledek_z_cache_jde_menit_bez_skody(self):
        p = self.soubor([asi({"input_tokens": 1})])
        kontext.stav(p)["tokens"] = 999
        self.assertEqual(kontext.stav(p)["tokens"], 1)

    def test_soubezne_dotazy_a_zapis(self):
        p = self.soubor([asi({"input_tokens": 1})])
        chyby = []

        def pisar():
            for i in range(200):
                with open(p, "a") as fh:
                    fh.write(asi({"input_tokens": i + 2}) + "\n")
                    if i % 7 == 0:
                        fh.write(asi({"input_tokens": 5})[:30])   # rozepsáno, bez \n
                        fh.flush()

        def ctenar():
            try:
                for _ in range(300):
                    r = kontext.stav(p)
                    assert r["tokens"] is None or r["tokens"] >= 1
            except Exception as e:      # noqa: BLE001
                chyby.append(e)

        vl = [threading.Thread(target=pisar)] + [threading.Thread(target=ctenar) for _ in range(8)]
        [v.start() for v in vl]
        [v.join(30) for v in vl]
        self.assertEqual(chyby, [])

    def test_rychlost_na_velkem_prepisu(self):
        # 60 MB přepis: čtení konce nesmí trvat zlomek toho, co celý soubor
        radek = user("y" * 100000)
        p = self.soubor([asi({"input_tokens": 77})] + [radek] * 3)
        with open(p, "ab") as fh:
            blok = (radek + "\n").encode() * 100
            for _ in range(6):
                fh.write(blok)
        self.assertGreater(os.path.getsize(p), 50 * 1024 * 1024)
        t = time.time()
        r = kontext.stav(p)
        self.assertLess(time.time() - t, 2.0)
        self.assertIsNone(r["tokens"])       # odpověď je daleko před koncem — žádný pád a žádné čtení všeho


if __name__ == "__main__":
    unittest.main(verbosity=1)
