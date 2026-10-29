"""Testy gateway/cloud.py — přesun do cloudu a zpátky, výběr pro automatiku,
blok v CLAUDE.md a obrana cest. Cloud hraje místní složka (rclone typ `local`),
takže test nepotřebuje účet u Googlu ani Microsoftu. Připojování (rclone mount
přes systemd) se tu netestuje — pripojit_disk je nahrazené.

    python3 tools/test_cloud.py
"""
import json
import os
import shutil
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
TMP = tempfile.mkdtemp(prefix="hub-cloud-test-")
os.environ["HUB_GW_DIR"] = os.path.join(TMP, "gateway")
os.environ["HUB_GW_CLOUD_DIR"] = os.path.join(TMP, "gateway", "cloud")
os.environ["HUB_GW_USERS"] = os.path.join(TMP, "users")

from gateway import cloud, workspace  # noqa: E402

RCLONE = shutil.which(os.environ.get("HUB_GW_RCLONE") or "rclone")
USER = {"id": 7, "email": "tester@example.com"}
MB = 1024 * 1024


def napis(path, bajtu=10, stary=False):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(b"x" * bajtu)
    if stary:
        t = time.time() - 90 * 86400
        os.utime(path, (t, t))


class Zaklad(unittest.TestCase):
    def setUp(self):
        self.home = os.path.join(TMP, "users", "tester")
        self.mrak = os.path.join(TMP, "mrak")                 # „cloud" = místní složka
        for p in (self.home, self.mrak, os.environ["HUB_GW_CLOUD_DIR"]):
            shutil.rmtree(p, ignore_errors=True)
            os.makedirs(p)
        self._home_for = workspace.home_for
        self._mount = cloud.pripojit_disk
        workspace.home_for = lambda user: self.home
        cloud.pripojit_disk = lambda *a, **k: True

    def tearDown(self):
        workspace.home_for = self._home_for
        cloud.pripojit_disk = self._mount

    def pripoj(self):
        cloud._rclone_conf_zapis(USER, "google", {"type": "alias", "remote": self.mrak})
        cloud._uprav_stav(USER, lambda d: d["sluzby"].__setitem__("google", {"ucet": "tester@gmail.com"}))


class Cesty(Zaklad):
    def test_odmita_nebezpecne(self):
        for spatne in ("../jiny", "/etc/passwd", ".claude", ".ssh/id_rsa", "Cloud/Google Disk",
                       "Obsidian/tester", "projekt/.git", ""):
            with self.subTest(cesta=spatne):
                with self.assertRaises(ValueError):
                    cloud._cista_cesta(spatne)

    def test_prijima_bezne(self):
        self.assertEqual(cloud._cista_cesta("~/videa/raw"), "videa/raw")
        self.assertEqual(cloud._cista_cesta("data.zip"), "data.zip")


class Kandidati(Zaklad):
    def test_vyber(self):
        napis(os.path.join(self.home, "videa", "velky.mp4"), 3 * MB, stary=True)
        napis(os.path.join(self.home, "videa", "novy.mp4"), 3 * MB, stary=False)
        napis(os.path.join(self.home, "videa", "maly.txt"), 10, stary=True)
        napis(os.path.join(self.home, ".cache", "x.bin"), 3 * MB, stary=True)
        napis(os.path.join(self.home, "projekt", ".git", "HEAD"), 10)
        napis(os.path.join(self.home, "projekt", "data.bin"), 3 * MB, stary=True)
        napis(os.path.join(self.home, "web", "node_modules", "a.bin"), 3 * MB, stary=True)
        napis(os.path.join(self.home, "Obsidian", "Brain", "obr.png"), 3 * MB, stary=True)
        os.symlink("/etc/hostname", os.path.join(self.home, "videa", "odkaz"))
        rel = [r for r, _ in cloud.kandidati(self.home, min_bajtu=MB, dni=30)]
        self.assertEqual(rel, ["videa/velky.mp4"])


class ClaudeMd(Zaklad):
    def test_blok_pridat_a_odebrat(self):
        os.makedirs(os.path.join(self.home, ".claude"))
        with open(os.path.join(self.home, ".claude", "CLAUDE.md"), "w") as fh:
            fh.write("# Moje pravidla\n")
        self.pripoj()
        cloud.claude_md(USER)
        text = open(os.path.join(self.home, ".claude", "CLAUDE.md")).read()
        self.assertIn("# Moje pravidla", text)
        self.assertIn("~/Cloud/Google Disk", text)
        cloud.claude_md(USER)                                   # podruhé se neduplikuje
        self.assertEqual(open(os.path.join(self.home, ".claude", "CLAUDE.md")).read(), text)
        cloud._uprav_stav(USER, lambda d: d["sluzby"].clear())
        cloud.claude_md(USER)
        text = open(os.path.join(self.home, ".claude", "CLAUDE.md")).read()
        self.assertEqual(text.strip(), "# Moje pravidla")


@unittest.skipUnless(RCLONE, "rclone není nainstalovaný")
class Presun(Zaklad):
    def setUp(self):
        super().setUp()
        cloud.RCLONE = RCLONE
        self.pripoj()

    def test_slozka_tam_a_zpet(self):
        napis(os.path.join(self.home, "videa", "a.mp4"), 2000)
        napis(os.path.join(self.home, "videa", "sub", "b.mp4"), 3000)
        os.symlink("a.mp4", os.path.join(self.home, "videa", "odkaz-na-a"))
        z = cloud.presunout(USER, "videa", log=lambda *_: None)
        src = os.path.join(self.home, "videa")
        self.assertTrue(os.path.islink(src))
        self.assertEqual(os.readlink(src), os.path.join(self.home, "Cloud", "Google Disk", "Aladin", "videa"))
        self.assertTrue(os.path.isfile(os.path.join(self.mrak, "Aladin", "videa", "sub", "b.mp4")))
        self.assertTrue(z["slozka"])
        d = cloud.stav(USER)
        self.assertTrue(d["prvni"])
        self.assertEqual([x["cesta"] for x in d["presunuto"]], ["videa"])
        cloud.vratit(USER, "videa", log=lambda *_: None)
        self.assertFalse(os.path.islink(src))
        self.assertEqual(os.path.getsize(os.path.join(src, "sub", "b.mp4")), 3000)
        self.assertTrue(os.path.islink(os.path.join(src, "odkaz-na-a")))   # odkaz zůstal odkazem
        self.assertEqual(cloud.stav(USER)["presunuto"], [])

    def test_soubor(self):
        napis(os.path.join(self.home, "export.zip"), 5000)
        cloud.presunout(USER, "export.zip", log=lambda *_: None)
        self.assertTrue(os.path.islink(os.path.join(self.home, "export.zip")))
        self.assertTrue(os.path.isfile(os.path.join(self.mrak, "Aladin", "export.zip")))
        cloud.vratit(USER, "export.zip", log=lambda *_: None)
        self.assertEqual(os.path.getsize(os.path.join(self.home, "export.zip")), 5000)

    def test_cesta_pres_odkaz(self):
        venku = os.path.join(TMP, "cizi")
        napis(os.path.join(venku, "tajne.txt"), 100)
        os.symlink(venku, os.path.join(self.home, "past"))
        with self.assertRaises((ValueError, OSError)):
            cloud.presunout(USER, "past/tajne.txt", log=lambda *_: None)
        self.assertTrue(os.path.isfile(os.path.join(venku, "tajne.txt")))

    def test_automatika(self):
        napis(os.path.join(self.home, "videa", "stary.mp4"), 3 * MB, stary=True)
        napis(os.path.join(self.home, "videa", "novy.mp4"), 3 * MB)
        cloud.AUTO_MB = 1
        user = dict(USER)
        # Bez prvního ručního přesunu a bez zapnutí nic.
        self.assertEqual(cloud.automaticky([user], log=lambda *_: None), {})
        with self.assertRaises(ValueError):
            cloud.nastavit_auto(user, True)
        cloud._uprav_stav(user, lambda d: d.update(prvni=1))
        cloud.nastavit_auto(user, True)
        souhrn = cloud.automaticky([user], log=lambda *_: None)
        self.assertGreater(souhrn[user["email"]], 0)
        self.assertTrue(os.path.islink(os.path.join(self.home, "videa", "stary.mp4")))
        self.assertFalse(os.path.islink(os.path.join(self.home, "videa", "novy.mp4")))
        self.assertTrue(cloud.stav(user)["presunuto"][0]["auto"])


if __name__ == "__main__":
    try:
        unittest.main(verbosity=1)
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
