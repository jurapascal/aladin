"""Testy gateway/whatsapp.py — kdo smí botovi psát, ověření čísla kódem, povolení
skupin, blokace a limity. Účty a role hrají atrapy, takže test nepotřebuje
databázi ani běžící bránu.

    python3 tools/test_whatsapp.py
"""
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
TMP = tempfile.mkdtemp(prefix="hub-wa-test-")
os.environ["HUB_GW_DIR"] = os.path.join(TMP, "gateway")
os.environ["HUB_GW_FIRMY_DIR"] = os.path.join(TMP, "firmy")

from gateway import firmy, shared, whatsapp as wa  # noqa: E402

BOT_A, BOT_B = "420700000001", "420700000002"
KAPITAN = {"id": 1, "email": "kapitan@x.cz", "role": "user"}
CLEN = {"id": 2, "email": "clen@x.cz", "role": "user"}
OBEMA = {"id": 3, "email": "obema@x.cz", "role": "user"}
CIZI = {"id": 4, "email": "cizi@x.cz", "role": "user"}
UCTY = {u["id"]: u for u in (KAPITAN, CLEN, OBEMA, CIZI)}
# firma -> id účtu -> role
ROLE = {"a": {1: "kapitan", 2: "clen", 3: "clen"}, "b": {3: "host"}}


class Ucty:
    def by_id(self, uid):
        return UCTY.get(int(uid))


def role(user, slug, data=None):
    return ROLE.get(slug, {}).get((user or {}).get("id"), "")


class Zaklad(unittest.TestCase):
    def setUp(self):
        for soubor in os.listdir(TMP):
            pass
        reg = wa._registr()
        if os.path.exists(reg):
            os.remove(reg)
        wa._POUZITI.clear()
        wa._POKUSY.clear()
        self._role, self._ucty, self._slugs = firmy.role, shared.ACCOUNTS, firmy.slugs
        firmy.role, shared.ACCOUNTS = role, Ucty()
        firmy.slugs = lambda data=None: list(ROLE)
        wa.nastavit_cislo(KAPITAN, "a", BOT_A)

    def tearDown(self):
        firmy.role, shared.ACCOUNTS, firmy.slugs = self._role, self._ucty, self._slugs

    def overit(self, user, tel, slug="a"):
        kod = wa.zacit_overeni(user, tel)
        self.assertEqual(wa.over_kod(slug, tel, kod), user["id"])


class Cisla(Zaklad):
    def test_telefon(self):
        self.assertEqual(wa.telefon("+420 777 123 456"), "420777123456")
        self.assertEqual(wa.telefon("abc"), "")
        self.assertEqual(wa.telefon("123"), "")

    def test_cislo_jen_pro_generala(self):
        with self.assertRaises(PermissionError):
            wa.nastavit_cislo(CLEN, "a", "420700000009")
        with self.assertRaises(PermissionError):
            wa.nastavit_cislo(CIZI, "a", "420700000009")

    def test_cislo_patri_jedne_firme(self):
        ROLE["b"][1] = "kapitan"
        try:
            with self.assertRaises(ValueError):
                wa.nastavit_cislo(KAPITAN, "b", BOT_A)
        finally:
            ROLE["b"].pop(1)

    def test_odpojeni_smaze_skupiny(self):
        self.overit(CLEN, "420777000002")
        wa.povolit_skupinu("a", "g1@g.us", "420777000002")
        wa.odpojit_cislo(KAPITAN, "a")
        self.assertIsNone(wa.cislo_firmy("a"))
        self.assertFalse(wa.skupina_povolena("a", "g1@g.us"))


class Overeni(Zaklad):
    def test_spravny_kod(self):
        self.overit(CLEN, "420777000002")
        self.assertEqual(wa.ucet_podle_cisla("420777000002", "a")["id"], 2)

    def test_spatny_kod_a_pokusy(self):
        kod = wa.zacit_overeni(CLEN, "420777000002")
        spatny = "000000" if kod != "000000" else "111111"
        for _ in range(wa.KOD_POKUSY):
            self.assertIsNone(wa.over_kod("a", "420777000002", spatny))
        # po vyčerpání pokusů neprojde ani správný kód
        self.assertIsNone(wa.over_kod("a", "420777000002", kod))

    def test_kod_plati_omezene(self):
        kod = wa.zacit_overeni(CLEN, "420777000002")
        import time as _t
        real = _t.time
        wa.time.time = lambda: real() + wa.KOD_PLATI + 5
        try:
            self.assertIsNone(wa.over_kod("a", "420777000002", kod))
        finally:
            wa.time.time = real

    def test_kod_pouzit_jednou(self):
        kod = wa.zacit_overeni(CLEN, "420777000002")
        self.assertEqual(wa.over_kod("a", "420777000002", kod), 2)
        self.assertIsNone(wa.over_kod("a", "420777000002", kod))

    def test_cizi_nesmi_overit_do_firmy(self):
        kod = wa.zacit_overeni(CIZI, "420777000004")
        self.assertIsNone(wa.over_kod("a", "420777000004", kod))
        self.assertIsNone(wa.ucet_podle_cisla("420777000004", "a"))

    def test_cislo_jen_u_jednoho_uctu(self):
        self.overit(CLEN, "420777000002")
        with self.assertRaises(ValueError):
            wa.zacit_overeni(OBEMA, "420777000002")

    def test_odebrani_z_firmy_zneplatni_cislo(self):
        self.overit(CLEN, "420777000002")
        ROLE["a"].pop(2)
        try:
            self.assertIsNone(wa.ucet_podle_cisla("420777000002", "a"))
        finally:
            ROLE["a"][2] = "clen"


class Soukrome(Zaklad):
    def dm(self, od, text="ahoj"):
        return wa.rozhodni("a", {"typ": "dm", "od": od, "text": text})

    def test_neznamy_mlci(self):
        self.assertEqual(self.dm("420999999999")["akce"], "ticho")

    def test_kod_projde_i_neznamemu(self):
        self.assertEqual(self.dm("420999999999", "/kod 123456")["akce"], "kod")

    def test_clen_dostane_odpoved(self):
        self.overit(CLEN, "420777000002")
        r = self.dm("420777000002")
        self.assertEqual((r["akce"], r["uid"]), ("clovek", 2))

    def test_zablokovany_mlci(self):
        self.overit(CLEN, "420777000002")
        wa.zablokovat(KAPITAN, "a", "420777000002")
        self.assertEqual(self.dm("420777000002")["akce"], "ticho")
        wa.odblokovat(KAPITAN, "a", "420777000002")
        self.assertEqual(self.dm("420777000002")["akce"], "clovek")

    def test_prikaz_nese_pravo_generala(self):
        self.overit(CLEN, "420777000002")
        self.overit(KAPITAN, "420777000001")
        self.assertFalse(self.dm("420777000002", "/stav")["kapitan"])
        self.assertTrue(self.dm("420777000001", "/stav")["kapitan"])

    def test_limit_na_cloveka(self):
        self.overit(CLEN, "420777000002")
        wa.nastaveni(KAPITAN, "a", limit_hodina=2)
        self.assertEqual(self.dm("420777000002")["akce"], "clovek")
        self.assertEqual(self.dm("420777000002")["akce"], "clovek")
        self.assertEqual(self.dm("420777000002")["akce"], "limit")

    def test_firma_bez_cisla_mlci(self):
        self.assertEqual(wa.rozhodni("b", {"typ": "dm", "od": "420777000002", "text": "x"})["akce"],
                         "ticho")

    def test_clovek_ve_dvou_firmach_ma_dva_kontexty(self):
        ROLE["b"][1] = "kapitan"
        try:
            wa.nastavit_cislo(KAPITAN, "b", BOT_B)
        finally:
            ROLE["b"].pop(1)
        self.overit(OBEMA, "420777000003")
        a = wa.rozhodni("a", {"typ": "dm", "od": "420777000003", "text": "x"})
        b = wa.rozhodni("b", {"typ": "dm", "od": "420777000003", "text": "x"})
        self.assertEqual((a["akce"], b["akce"]), ("clovek", "clovek"))
        self.assertEqual(wa.firma_podle_cisla(BOT_A), "a")
        self.assertEqual(wa.firma_podle_cisla(BOT_B), "b")


class Skupiny(Zaklad):
    G = "123-456@g.us"

    def skup(self, od, **extra):
        return wa.rozhodni("a", {"typ": "skupina", "od": od, "skupina": self.G,
                                 "text": extra.pop("text", "ahoj"), **extra})

    def test_nepovolena_skupina_mlci(self):
        self.assertEqual(self.skup("420999999999", oznacen=True)["akce"], "ticho")

    def test_clen_pridal_bota(self):
        self.overit(CLEN, "420777000002")
        self.assertEqual(wa.pridal_bota("a", self.G, "420777000002")["akce"], "povolit")
        self.assertEqual(self.skup("420999999999")["akce"], "kontext")
        r = self.skup("420999999999", oznacen=True)
        self.assertEqual((r["akce"], r["od_uid"]), ("skupina", None))

    def test_cizi_pridal_bota_odejit(self):
        self.assertEqual(wa.pridal_bota("a", self.G, "420999999999")["akce"], "odejit")
        self.assertEqual(self.skup("420999999999", oznacen=True)["akce"], "ticho")

    def test_neznamy_pridavatel_ticho_a_bez_odchodu(self):
        self.assertEqual(wa.pridal_bota("a", self.G, "")["akce"], "ticho")

    def test_odejit_jde_vypnout(self):
        wa.nastaveni(KAPITAN, "a", odejit_z_cizich=False)
        self.assertEqual(wa.pridal_bota("a", self.G, "420999999999")["akce"], "ticho")

    def test_povol_prikazem(self):
        self.overit(CLEN, "420777000002")
        r = self.skup("420777000002", text="/povol")
        self.assertEqual((r["akce"], r["prikaz"]), ("prikaz", "povol"))
        self.assertEqual(self.skup("420999999999", text="/povol")["akce"], "ticho")

    def test_povolujici_odejde_z_firmy_skupina_prestane(self):
        self.overit(CLEN, "420777000002")
        wa.pridal_bota("a", self.G, "420777000002")
        ROLE["a"].pop(2)
        try:
            self.assertEqual(self.skup("420999999999", oznacen=True)["akce"], "ticho")
        finally:
            ROLE["a"][2] = "clen"

    def test_zablokovana_skupina(self):
        self.overit(CLEN, "420777000002")
        wa.pridal_bota("a", self.G, "420777000002")
        wa.zablokovat(KAPITAN, "a", self.G, "skupina")
        self.assertEqual(self.skup("420999999999", oznacen=True)["akce"], "ticho")
        self.assertIsNone(wa.povolit_skupinu("a", self.G, "420777000002"))

    def test_jen_lide_z_firmy(self):
        self.overit(CLEN, "420777000002")
        wa.pridal_bota("a", self.G, "420777000002")
        wa.nastaveni(KAPITAN, "a", skupiny_jen_firma=True)
        self.assertEqual(self.skup("420999999999", oznacen=True)["akce"], "kontext")
        r = self.skup("420777000002", oznacen=True)
        self.assertEqual((r["akce"], r["od_uid"]), ("skupina", 2))

    def test_odpoved_botovi_staci(self):
        self.overit(CLEN, "420777000002")
        wa.pridal_bota("a", self.G, "420777000002")
        self.assertEqual(self.skup("420999999999", odpoved_botovi=True)["akce"], "skupina")

    def test_smazany_ucet_odnese_skupiny(self):
        self.overit(CLEN, "420777000002")
        wa.pridal_bota("a", self.G, "420777000002")
        wa.forget_user(2)
        self.assertFalse(wa.skupina_povolena("a", self.G))
        self.assertIsNone(wa.ucet_podle_cisla("420777000002", "a"))


class Soukromi(Zaklad):
    def test_registr_ma_prava_600_a_zaznam_nema_text(self):
        self.overit(CLEN, "420777000002")
        self.assertEqual(os.stat(wa._registr()).st_mode & 0o777, 0o600)
        wa.rozhodni("a", {"typ": "dm", "od": "420777000002", "text": "tajny-text-zpravy"})
        with open(wa._zaznam(), encoding="utf-8") as fh:
            self.assertNotIn("tajny-text-zpravy", fh.read())


if __name__ == "__main__":
    unittest.main(verbosity=1)
