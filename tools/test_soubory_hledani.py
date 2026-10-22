#!/usr/bin/env python3
"""Zkouška našeptávače souborů (hub/soubory_hledani.py): stdlib, bez sítě,
bez zásahu do skutečného ~/.claude. Spuštění: python3 tools/test_soubory_hledani.py"""
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from hub import soubory_hledani as sh  # noqa: E402


def touch(base, rel, text="x"):
    p = os.path.join(base, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w") as f:
        f.write(text)


class Base(unittest.TestCase):
    def setUp(self):
        sh._cache.clear()
        self.tmp = tempfile.TemporaryDirectory()
        self.root = os.path.realpath(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def paths(self, q, **kw):
        sh._cache.clear()
        return [i["path"] for i in sh.hledej(self.root, q, **kw)["items"]]


class BezGitu(Base):
    def test_zaklad_a_vynechane_slozky(self):
        for r in ("src/app.py", "src/lib/util.py", "README.md", "node_modules/x/index.js",
                  "venv/bin/python", ".git/config", "vendor/a.php", "build/o.js", "dist/o.js",
                  "__pycache__/a.pyc", "moje složka/a b.txt"):
            touch(self.root, r)
        v = self.paths("")
        self.assertIn("src/", v)
        self.assertIn("README.md", v)
        for bad in ("node_modules", "venv", ".git", "vendor", "build", "dist", "__pycache__"):
            self.assertFalse(any(bad in p for p in self.paths(bad[:3])), bad)
        self.assertIn("src/app.py", self.paths("app"))
        self.assertIn("moje složka/a b.txt", self.paths("a b"))
        self.assertIn("src/lib/util.py", self.paths("slu"))      # fuzzy

    def test_zlomyslne_dotazy(self):
        touch(self.root, "a.txt")
        for q in ("..", "../..", "/etc/passwd", "\x00", "a\x00b", "x" * 10000, "../../etc",
                  "~", "*", "[", "\n", "\U0001F600", None, 5):
            for p in self.paths(q):
                self.assertFalse(p.startswith("/") or ".." in p.split("/"), (q, p))
        self.assertEqual(self.paths("/etc/passwd"), [])

    def test_symlinky_ven_se_nenasleduji(self):
        out = tempfile.mkdtemp()
        try:
            touch(out, "tajne.txt")
            os.symlink(out, os.path.join(self.root, "odkaz"))
            os.symlink(os.path.join(out, "tajne.txt"), os.path.join(self.root, "tajny_soubor"))
            touch(self.root, "ok.txt")
            v = self.paths("") + self.paths("tajn") + self.paths("odkaz")
            self.assertFalse(any("tajn" in p or "odkaz" in p for p in v), v)
            self.assertIn("ok.txt", self.paths("ok"))
        finally:
            shutil.rmtree(out)

    def test_podivna_jmena(self):
        touch(self.root, "nový\nřádek.txt")
        touch(self.root, 'a "b".txt')
        touch(self.root, "ab.txt")
        v = self.paths("")
        self.assertNotIn("nový\nřádek.txt", v)
        self.assertNotIn('a "b".txt', v)
        self.assertIn("ab.txt", v)

    def test_limit_a_cas(self):
        for i in range(300):
            touch(self.root, "d%d/f%d.txt" % (i % 7, i))
        self.assertLessEqual(len(self.paths("f")), 30)
        t = time.monotonic()
        r = sh.hledej(self.root, "f", cas=0.0)
        self.assertLess(time.monotonic() - t, 1.0)
        self.assertIsInstance(r["items"], list)

    def test_hloubka(self):
        touch(self.root, "/".join("l%d" % i for i in range(20)) + "/hluboko.txt")
        self.assertEqual(self.paths("hluboko"), [])

    def test_koren(self):
        self.assertIsNone(sh.koren_tabu("/"))
        self.assertIsNone(sh.koren_tabu(""))
        self.assertIsNone(sh.koren_tabu(None))
        self.assertIsNone(sh.koren_tabu(os.path.join(self.root, "neni")))
        self.assertIsNone(sh.koren_tabu("a\x00b"))
        self.assertEqual(sh.koren_tabu(self.root), self.root)

    def test_soubeh(self):
        for i in range(200):
            touch(self.root, "s/f%d.txt" % i)
        err = []

        def run():
            try:
                for q in ("f1", "", "s/f", "zzz"):
                    sh.hledej(self.root, q)
            except Exception as e:  # pragma: no cover
                err.append(e)
        ts = [threading.Thread(target=run) for _ in range(16)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(10)
        self.assertEqual(err, [])

    def test_cache(self):
        touch(self.root, "a.txt")
        sh.hledej(self.root, "a")
        touch(self.root, "novy.txt")
        self.assertEqual(sh.hledej(self.root, "novy")["items"], [])     # ještě z paměti
        sh._cache.clear()
        self.assertTrue(sh.hledej(self.root, "novy")["items"])


@unittest.skipUnless(shutil.which("git"), "bez gitu")
class SGitem(Base):
    def test_gitignore(self):
        subprocess.run(["git", "init", "-q", self.root], check=True)
        touch(self.root, ".gitignore", "tajne/\n*.log\n")
        for r in ("tajne/a.txt", "x.log", "src/ok.py", "node_modules/p/i.js", "podsl/dobry.txt"):
            touch(self.root, r)
        self.assertIn("src/", self.paths(""))
        self.assertFalse(any(p.startswith("tajne") for p in self.paths("a")))
        self.assertEqual(self.paths("x.log"), [])
        self.assertEqual(self.paths("node_modules"), [])
        self.assertIn("src/ok.py", self.paths("ok"))

    def test_podslozka_repa(self):
        subprocess.run(["git", "init", "-q", self.root], check=True)
        touch(self.root, "sub/in.txt")
        touch(self.root, "out.txt")
        sh._cache.clear()
        self.assertEqual([i["path"] for i in sh.hledej(os.path.join(self.root, "sub"), "")["items"]], ["in.txt"])

    def test_poskozeny_git(self):
        os.makedirs(os.path.join(self.root, ".git"))      # není to opravdový repozitář
        touch(self.root, "a.txt")
        self.assertIn("a.txt", self.paths("a"))


if __name__ == "__main__":
    unittest.main(verbosity=1)
