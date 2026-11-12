"""Testy gateway/wa_provoz.py — co se stane s událostí z bridge: příkazy,
ověření čísla, skupiny, odmítnutí cizích a stav spojení. Claude hraje atrapa,
takže test nepotřebuje přihlášení ani síť.

    python3 tools/test_wa_provoz.py
"""
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))
TMP = tempfile.mkdtemp(prefix="hub-wa-provoz-test-")
os.environ["HUB_GW_DIR"] = os.path.join(TMP, "gateway")
os.environ["HUB_GW_FIRMY_DIR"] = os.path.join(TMP, "firmy")
os.environ["HUB_GW_ULOZISTE_DIR"] = os.path.join(TMP, "uloziste")

import test_whatsapp as base  # noqa: E402
from gateway import wa_provoz as wp, whatsapp as wa  # noqa: E402

KAPITAN, CLEN, CIZI, BOT_A = base.KAPITAN, base.CLEN, base.CIZI, base.BOT_A
TEL_K, TEL_C, TEL_CIZI = "420777000001", "420777000002", "420777000003"
GRP = "120363@g.us"


def dm(od, text, chat=None):
    return {"typ": "dm", "od": od, "chat": chat or od + "@s.whatsapp.net", "text": text}


def sk(od, text, oznacen=True, jmeno="Eva"):
    return {"typ": "skupina", "od": od, "chat": GRP, "skupina": GRP, "text": text,
            "oznacen": oznacen, "odpoved_botovi": False, "jmeno": jmeno}


class Provoz(base.Zaklad):
    def setUp(self):
        super().setUp()
        self._zeptat, self._nazev = wp._zeptat_se, wp._nazev_firmy
        self.dotazy = []
        wp._zeptat_se = lambda slug, system, hist, text: (self.dotazy.append((system, hist, text)) or "Odpověď.")
        wp._nazev_firmy = lambda slug: "Firma A"
        wp._HISTORIE.clear()
        wp._STAV.clear()
        wp._LIMIT_OZNAMENO.clear()
        self.overit(KAPITAN, TEL_K)
        self.overit(CLEN, TEL_C)

    def tearDown(self):
        wp._zeptat_se, wp._nazev_firmy = self._zeptat, self._nazev
        super().tearDown()

    def texty(self, out):
        return [o["text"] for o in out["odpovedi"]]

    def test_cizi_cislo_ticho_a_claude_se_nevola(self):
        out = wp.udalost("a", dm(TEL_CIZI, "ahoj"))
        self.assertEqual(out, {"odpovedi": [], "opustit": ""})
        self.assertEqual(self.dotazy, [])

    def test_clovek_z_firmy_dostane_odpoved_a_pamet(self):
        self.assertEqual(self.texty(wp.udalost("a", dm(TEL_C, "ahoj"))), ["Odpověď."])
        wp.udalost("a", dm(TEL_C, "a ještě"))
        self.assertEqual(self.dotazy[1][1], [("Ty", "ahoj"), ("Asistent", "Odpověď.")])
        self.assertIn("soukromě", self.dotazy[0][0])
        self.assertIn("Nemáš žádné nástroje", self.dotazy[0][0])

    def test_nova_zapomene_historii(self):
        wp.udalost("a", dm(TEL_C, "ahoj"))
        wp.udalost("a", dm(TEL_C, "/nova"))
        wp.udalost("a", dm(TEL_C, "znovu"))
        self.assertEqual(self.dotazy[-1][1], [])

    def test_kod_overi_cislo_cizinec_nic_nedostane(self):
        kod = wa.zacit_overeni(base.OBEMA, "420777000009")
        out = wp.udalost("a", dm("420777000009", "/kod " + kod))
        self.assertIn("propojené", self.texty(out)[0])
        out = wp.udalost("a", dm(TEL_CIZI, "/kod 123456"))
        self.assertEqual(out["odpovedi"], [])

    def test_kapitan_blokuje_a_odblokuje_cislo(self):
        out = wp.udalost("a", dm(TEL_K, "/zakaz +" + TEL_C))
        self.assertIn("zablokované", self.texty(out)[0])
        self.assertEqual(wp.udalost("a", dm(TEL_C, "ahoj"))["odpovedi"], [])
        out = wp.udalost("a", dm(TEL_K, "/povol +" + TEL_C))
        self.assertIn("odblokované", self.texty(out)[0])
        self.assertEqual(self.texty(wp.udalost("a", dm(TEL_C, "ahoj"))), ["Odpověď."])

    def test_clen_nesmi_blokovat(self):
        out = wp.udalost("a", dm(TEL_C, "/zakaz +" + TEL_CIZI))
        self.assertIn("generál", self.texty(out)[0])
        self.assertFalse(wa.zablokovano("a", TEL_CIZI))

    def test_skupina_pridal_clen_povolena_a_odpovi_jen_na_oznaceni(self):
        r = wp.udalost("a", {"druh": "pridan", "skupina": GRP, "pridavatel": TEL_C})
        self.assertEqual(r["opustit"], "")
        self.assertEqual(self.texty(wp.udalost("a", sk(TEL_CIZI, "co je nového?"))), ["Odpověď."])
        self.assertEqual(wp.udalost("a", sk(TEL_CIZI, "jen si povídáme", oznacen=False))["odpovedi"], [])
        system, hist, text = self.dotazy[0]
        self.assertIn("skupinového chatu", system)
        self.assertEqual(text, "Eva: co je nového?")

    def test_skupina_pridal_cizi_opusti(self):
        r = wp.udalost("a", {"druh": "pridan", "skupina": GRP, "pridavatel": TEL_CIZI})
        self.assertEqual(r["opustit"], GRP)
        self.assertEqual(wp.udalost("a", sk(TEL_C, "ahoj"))["odpovedi"], [])

    def test_skupina_neznamy_pridavatel_neodchazi_a_mlci(self):
        r = wp.udalost("a", {"druh": "pridan", "skupina": GRP, "pridavatel": ""})
        self.assertEqual(r["opustit"], "")
        self.assertEqual(wp.udalost("a", sk(TEL_C, "ahoj"))["odpovedi"], [])

    def test_povol_a_zakaz_ve_skupine(self):
        out = wp.udalost("a", sk(TEL_K, "/povol", oznacen=False))
        self.assertIn("povolená", self.texty(out)[0])
        out = wp.udalost("a", sk(TEL_K, "/zakaz", oznacen=False))
        self.assertIn("vypnul", self.texty(out)[0])
        self.assertEqual(wp.udalost("a", sk(TEL_C, "ahoj"))["odpovedi"], [])
        # člen /povol nesmí
        out = wp.udalost("a", sk(TEL_C, "/povol", oznacen=False))
        self.assertIn("generál", self.texty(out)[0])

    def test_limit_upozorni_jednou(self):
        e = wa._load()
        wa.nastaveni(KAPITAN, "a", limit_hodina=1)
        self.assertEqual(len(wp.udalost("a", dm(TEL_C, "1"))["odpovedi"]), 1)
        druha = wp.udalost("a", dm(TEL_C, "2"))
        self.assertIn("později", self.texty(druha)[0])
        self.assertEqual(wp.udalost("a", dm(TEL_C, "3"))["odpovedi"], [])

    def test_chyba_clauda_neni_ticho(self):
        wp._zeptat_se = lambda *a: ""
        self.assertIn("nedaří", self.texty(wp.udalost("a", dm(TEL_C, "ahoj")))[0])

    def test_firma_bez_cisla_nic(self):
        self.assertEqual(wp.udalost("b", dm(TEL_C, "ahoj"))["odpovedi"], [])

    def test_stav_bridge_a_token(self):
        self.assertFalse(wp.stav("a")["bridge"])
        wp.zapsat_stav("a", {"pripojeno": False, "qr": "data:image/png;base64,AAA"})
        s = wp.stav("a")
        self.assertTrue(s["bridge"] and not s["pripojeno"])
        self.assertEqual(s["qr"], "data:image/png;base64,AAA")
        wp.zapsat_stav("neexistuje", {"pripojeno": True})
        self.assertNotIn("neexistuje", wp._STAV)
        t = wp.token()
        self.assertEqual(len(t), 64)
        self.assertTrue(wp.token_ok(t))
        self.assertFalse(wp.token_ok("špatný"))
        self.assertFalse(wp.token_ok(""))
        self.assertEqual(oct(os.stat(wp._token_cesta()).st_mode & 0o777), "0o600")

    def test_prehled_jen_pro_generala_a_bez_otisku_kodu(self):
        with self.assertRaises(PermissionError):
            wp.prehled(CLEN, "a")
        wp.udalost("a", {"druh": "pridan", "skupina": GRP, "pridavatel": TEL_C})
        p = wp.prehled(KAPITAN, "a")
        self.assertEqual(p["cislo"]["telefon"], BOT_A)
        self.assertEqual(p["skupiny"][0]["jid"], GRP)
        self.assertNotIn("kod", repr(wp.moje(CLEN)))

    def test_seznam_pro_bridge(self):
        self.assertEqual(wp.seznam_pro_bridge(), [{"firma": "a", "telefon": BOT_A}])

    def test_zaznam_bez_textu_zprav(self):
        wp.udalost("a", dm(TEL_C, "tajný obsah zprávy"))
        with open(wa._zaznam(), encoding="utf-8") as fh:
            self.assertNotIn("tajný obsah", fh.read())


if __name__ == "__main__":
    unittest.main(verbosity=1)
