#!/usr/bin/env python3
"""Zkouška stavového řádku (hub/stavradek.py) — stdlib, bez sítě, bez zásahu do ~/.claude.

    python3 tools/test_stavradek.py

Snaží se řádek shodit: rozpůlený a poškozený přepis, duplicitní odpovědi,
zkrácený a vyměněný soubor, neznámý model, složka s mezerami a unicode,
detached HEAD, smazaná složka, git mimo PATH, obří repo (limit), souběh.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest

TMP = tempfile.mkdtemp(prefix="stavradek-test-")
os.environ["CLAUDE_CONFIG_DIR"] = os.path.join(TMP, "claude")      # nikdy skutečné ~/.claude
os.makedirs(os.environ["CLAUDE_CONFIG_DIR"], exist_ok=True)
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from hub import core, stavradek  # noqa: E402


def radek(model="claude-sonnet-5-5", mid="m1", i=100, o=50, cw=0, cr=0, typ="assistant"):
    return json.dumps({"type": typ, "timestamp": "2026-10-22T10:00:00Z", "message": {
        "id": mid, "model": model,
        "usage": {"input_tokens": i, "output_tokens": o,
                  "cache_creation_input_tokens": cw, "cache_read_input_tokens": cr}}}) + "\n"


def zapis(cesta, *casti, mode="w"):
    with open(cesta, mode + "b") as fh:
        for c in casti:
            fh.write(c.encode() if isinstance(c, str) else c)


def git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                   env=dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
                            GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t"))


class Zaklad(unittest.TestCase):
    def setUp(self):
        stavradek._SPOTREBA.clear()
        stavradek._GIT.clear()
        self.dir = tempfile.mkdtemp(dir=TMP)
        self.prepis = os.path.join(self.dir, "a.jsonl")

    def cti(self, prepis=None, cwd=None):
        return stavradek.zjisti_z(cwd or self.dir, prepis or self.prepis, time.time() - 90)


class Spotreba(Zaklad):
    def test_secte_a_spocita_cenu(self):
        zapis(self.prepis, radek(mid="a", i=1000, o=500), radek(mid="b", i=2000, o=1000))
        r = self.cti()
        self.assertEqual(r["usage"], [3000, 1500, 0, 0])
        self.assertEqual(r["tokens"], 4500)
        self.assertEqual(r["label"], "Sonnet 5.5")
        self.assertAlmostEqual(r["cost"], (3000 * 2.0 + 1500 * 10.0) / 1e6, places=6)
        self.assertGreaterEqual(r["now"] - r["started"], 89000)

    def test_duplicitni_radky_jedne_odpovedi_se_pocitaji_jednou(self):
        zapis(self.prepis, radek(mid="a"), radek(mid="a"), radek(mid="a"), radek(mid="b"))
        r = self.cti()
        self.assertEqual(r["answers"], 2)
        self.assertEqual(r["usage"], [200, 100, 0, 0])

    def test_prirustek_cte_jen_novy_konec(self):
        zapis(self.prepis, radek(mid="a"))
        self.cti()
        pos = stavradek._SPOTREBA[self.prepis]["pos"]
        zapis(self.prepis, radek(mid="b"), mode="a")
        r = self.cti()
        self.assertEqual(r["usage"], [200, 100, 0, 0])
        self.assertGreater(stavradek._SPOTREBA[self.prepis]["pos"], pos)

    def test_nedopsana_posledni_radka_pocka(self):
        cely = radek(mid="b")
        zapis(self.prepis, radek(mid="a"), cely[:40])
        self.assertEqual(self.cti()["usage"], [100, 50, 0, 0])
        zapis(self.prepis, cely[40:], mode="a")
        self.assertEqual(self.cti()["usage"], [200, 100, 0, 0])

    def test_poskozene_radky_se_preskoci(self):
        zapis(self.prepis, "nesmysl {\n", b'{"usage": \xff\xfe "assistant"\n', "\x00\x00\n",
              radek(mid="a"), '{"type":"assistant","message":{"usage":[1]}}\n',
              '{"type":"assistant","message":{"id":1,"usage":{"input_tokens":"x","output_tokens":-5}}}\n')
        r = self.cti()
        self.assertEqual(r["usage"][:2], [100, 50])

    def test_zkraceny_nebo_vymeneny_soubor_se_cte_znovu(self):
        zapis(self.prepis, radek(mid="a"), radek(mid="b"), radek(mid="c"))
        self.assertEqual(self.cti()["answers"], 3)
        zapis(self.prepis, radek(mid="z"))                       # zkrácený
        self.assertEqual(self.cti()["answers"], 1)
        os.remove(self.prepis)
        zapis(self.prepis, radek(mid="y"), radek(mid="x"))       # jiný inode
        self.assertEqual(self.cti()["answers"], 2)

    def test_prepsany_soubor_stejne_nebo_vetsi_velikosti(self):
        zapis(self.prepis, radek(mid="a", i=100))
        self.assertEqual(self.cti()["usage"][0], 100)
        # Přepsaný jiným obsahem, delší než původní — pozice by sedla, začátek ne.
        zapis(self.prepis, radek(mid="q", model="claude-opus-5", i=7), radek(mid="r", i=8), radek(mid="s", i=9))
        r = self.cti()
        self.assertEqual(r["usage"][0], 24)
        self.assertEqual(r["answers"], 3)

    def test_chybejici_prazdny_a_neplatny_prepis(self):
        for cesta in ("", None, "/nic/tu/neni.jsonl", self.dir, "x\x00y.jsonl", "bez-pripony"):
            r = stavradek.zjisti_z(self.dir, cesta, 0)
            self.assertTrue(r["ok"])
            self.assertEqual(r["tokens"], 0)
            self.assertIsNone(r["cost"])
        zapis(self.prepis, "")
        self.assertEqual(self.cti()["tokens"], 0)

    def test_prepis_bez_usage(self):
        zapis(self.prepis, '{"type":"user","message":{"content":"ahoj"}}\n',
              '{"type":"assistant","message":{"model":"claude-opus-5","content":[]}}\n')
        r = self.cti()
        self.assertEqual(r["tokens"], 0)
        self.assertIsNone(r["cost"])
        self.assertEqual(r["model"], "claude-opus-5")      # model aspoň z konce přepisu

    def test_neznamy_model_bez_ceny_ale_s_tokeny(self):
        zapis(self.prepis, radek(model="glm-9-ultra", mid="a"), radek(mid="b"))
        r = self.cti()
        self.assertIsNone(r["cost"])
        self.assertEqual(r["tokens"], 300)
        self.assertEqual(r["label"], "Sonnet 5.5")          # poslední známý model
        zapis(self.prepis, radek(model="glm-9-ultra", mid="c"), mode="a")
        self.assertEqual(self.cti()["model"], "glm-9-ultra")
        self.assertEqual(self.cti()["label"], "glm-9-ultra")

    def test_synteticka_hlaska_nezmeni_model(self):
        zapis(self.prepis, radek(mid="a"), radek(model="<synthetic>", mid="b", i=0, o=0))
        r = self.cti()
        self.assertEqual(r["model"], "claude-sonnet-5-5")
        self.assertIsNotNone(r["cost"])

    def test_obri_radek_se_preskoci_a_pocitani_pokracuje(self):
        puvodni = stavradek.BUDGET
        stavradek.BUDGET = 4096
        try:
            zapis(self.prepis, radek(mid="a"), "x" * 20000 + "\n", radek(mid="b"))
            r = None
            for _ in range(30):
                r = self.cti()
                if not r["partial"]:
                    break
            self.assertFalse(r["partial"])
            self.assertEqual(r["answers"], 2)
        finally:
            stavradek.BUDGET = puvodni

    def test_velky_prepis_po_kouscich(self):
        puvodni = stavradek.BUDGET
        stavradek.BUDGET = 50_000
        try:
            zapis(self.prepis, *[radek(mid=f"m{i}") for i in range(2000)])
            prvni = self.cti()
            self.assertTrue(prvni["partial"])
            r = prvni
            for _ in range(200):
                if not r["partial"]:
                    break
                r = self.cti()
            self.assertEqual(r["answers"], 2000)
        finally:
            stavradek.BUDGET = puvodni

    def test_dva_taby_na_jednom_prepisu_soubezne(self):
        zapis(self.prepis, *[radek(mid=f"m{i}") for i in range(3000)])
        vysl = []
        vlakna = [threading.Thread(target=lambda: vysl.append(self.cti()["answers"])) for _ in range(8)]
        [v.start() for v in vlakna]
        [v.join() for v in vlakna]
        self.assertEqual(set(vysl), {3000})

    def test_vyrazeny_ceny_dvou_modelu(self):
        zapis(self.prepis, radek(mid="a", model="claude-opus-5", i=1_000_000, o=0),
              radek(mid="b", model="claude-haiku-4-5", i=1_000_000, o=0))
        self.assertAlmostEqual(self.cti()["cost"], 5.0 + 1.0, places=4)


class Git(Zaklad):
    def repo(self, jmeno="repo"):
        cesta = os.path.join(self.dir, jmeno)
        os.makedirs(cesta)
        git(cesta, "init", "-q", "-b", "hlavni")
        zapis(os.path.join(cesta, "a.txt"), "a")
        git(cesta, "add", "."); git(cesta, "commit", "-qm", "x")
        return cesta

    def test_vetev_a_zmeny(self):
        cesta = self.repo()
        self.assertEqual(self.cti(cwd=cesta)["branch"], "hlavni")
        self.assertEqual(self.cti(cwd=cesta)["dirty"], 0)
        stavradek._GIT.clear()
        zapis(os.path.join(cesta, "b.txt"), "b"); zapis(os.path.join(cesta, "a.txt"), "zmena")
        r = self.cti(cwd=cesta)
        self.assertEqual((r["git"], r["dirty"], r["detached"]), (True, 2, False))

    def test_podslozka_repa(self):
        cesta = self.repo()
        pod = os.path.join(cesta, "x", "y"); os.makedirs(pod)
        self.assertEqual(self.cti(cwd=pod)["branch"], "hlavni")

    def test_slozka_s_mezerami_a_unicode(self):
        cesta = self.repo("můj projekt – žluťoučký kůň ' \" $(x) ;")
        r = self.cti(cwd=cesta)
        self.assertEqual((r["git"], r["branch"], r["dirty"]), (True, "hlavni", 0))

    def test_detached_head(self):
        cesta = self.repo()
        git(cesta, "checkout", "-q", "--detach")
        r = self.cti(cwd=cesta)
        self.assertTrue(r["detached"])
        self.assertEqual(len(r["branch"]), 7)

    def test_worktree_s_gitdir_souborem(self):
        cesta = self.repo()
        wt = os.path.join(self.dir, "wt")
        git(cesta, "worktree", "add", "-q", "-b", "druha", wt)
        self.assertEqual(self.cti(cwd=wt)["branch"], "druha")

    def test_slozka_bez_gitu_smazana_a_nesmysl(self):
        for cwd in (self.dir, os.path.join(self.dir, "neni"), "", None, "a\x00b", 5,
                    "/proc/1/root/nic", "/root/nic"):
            r = self.cti(cwd=cwd)
            self.assertTrue(r["ok"])
            self.assertFalse(r["git"], cwd)
            self.assertIsNone(r["dirty"])

    def test_git_mimo_path_vetev_zustane(self):
        cesta = self.repo()
        puvodni = core.GIT
        try:
            for hodnota in ("", "/neexistuje/git"):
                core.GIT = hodnota
                stavradek._GIT.clear()
                r = self.cti(cwd=cesta)
                self.assertEqual((r["git"], r["branch"], r["dirty"]), (True, "hlavni", None))
        finally:
            core.GIT = puvodni

    def test_poskozeny_git(self):
        cesta = os.path.join(self.dir, "rozbite")
        os.makedirs(os.path.join(cesta, ".git"))                # prázdný .git bez HEAD
        r = self.cti(cwd=cesta)
        self.assertTrue(r["ok"])
        self.assertEqual(r["branch"], "")
        zapis(os.path.join(cesta, ".git", "HEAD"), "\x00\xff nesmysl")
        self.assertTrue(self.cti(cwd=cesta)["ok"])

    def test_git_se_nepousti_castěji_nez_za_ttl(self):
        cesta = self.repo()
        volani = []
        puvodni = stavradek._pocet_zmen
        stavradek._pocet_zmen = lambda k: volani.append(k) or 0
        try:
            for _ in range(50):
                self.cti(cwd=cesta)
            self.assertEqual(len(volani), 1)
            vlakna = [threading.Thread(target=lambda: self.cti(cwd=cesta)) for _ in range(20)]
            stavradek._GIT.clear()
            [v.start() for v in vlakna]
            [v.join() for v in vlakna]
            self.assertEqual(len(volani), 2)                     # souběh = jedno spuštění
        finally:
            stavradek._pocet_zmen = puvodni

    def test_dva_taby_stejna_slozka_sdili_stav(self):
        cesta = self.repo()
        a, b = self.cti(cwd=cesta), self.cti(cwd=cesta)
        self.assertEqual(a["dirty"], b["dirty"])

    def test_obri_repo_limit_a_odklad(self):
        cesta = self.repo()
        puvodni = stavradek.GIT_LIMIT
        # Falešný git, který se zasekne: limit 0,3 s ho musí utnout.
        falesny = os.path.join(self.dir, "pomaly-git")
        zapis(falesny, "#!/bin/sh\nsleep 30\n")
        os.chmod(falesny, 0o755)
        puvodni_git = core.GIT
        core.GIT = falesny
        stavradek.GIT_LIMIT = 0.3
        try:
            t = time.monotonic()
            r = self.cti(cwd=cesta)
            self.assertLess(time.monotonic() - t, 3)
            self.assertEqual((r["branch"], r["dirty"]), ("hlavni", None))
            self.assertEqual(stavradek._GIT[os.path.realpath(cesta)]["ttl"], stavradek.GIT_TTL_POMALY)
            t = time.monotonic()
            self.cti(cwd=cesta)                                  # další dotaz už git nespouští
            self.assertLess(time.monotonic() - t, 0.2)
        finally:
            core.GIT, stavradek.GIT_LIMIT = puvodni_git, puvodni

    def test_100000_souboru(self):
        if os.environ.get("STAVRADEK_RYCHLE"):
            self.skipTest("rychlý běh")
        cesta = os.path.join(self.dir, "obri")
        os.makedirs(cesta)
        git(cesta, "init", "-q", "-b", "main")
        for d in range(100):
            sl = os.path.join(cesta, f"d{d}")
            os.makedirs(sl)
            for f in range(1000):
                open(os.path.join(sl, f"f{f}.txt"), "w").close()
        t = time.monotonic()
        r = self.cti(cwd=cesta)
        vyslo = time.monotonic() - t
        self.assertLess(vyslo, stavradek.GIT_LIMIT + 2)         # nikdy nezdrží déle než limit
        self.assertEqual(r["branch"], "main")
        self.assertTrue(r["dirty"] is None or r["dirty"] >= 100)   # git slučuje nové složky
        print(f"\n  100 000 souborů: {vyslo:.2f} s, dirty={r['dirty']}")


class Zjisti(unittest.TestCase):
    def test_bez_session_a_vadna_session(self):
        self.assertFalse(stavradek.zjisti(None)["ok"])

        class Spatna:
            path = "/tmp"
            started = "ne-cislo"
            id = 1
            chat_id = ""
        r = stavradek.zjisti(Spatna())
        self.assertIn("ok", r)                                  # vrátí slovník, nevyhodí výjimku
        if not r["ok"]:
            self.assertIn("chyba", r)


if __name__ == "__main__":
    try:
        unittest.main(verbosity=1, exit=False)
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
