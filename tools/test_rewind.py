#!/usr/bin/env python3
"""Zkouška vrácení souborů (hub/rewind.py) — stdlib, bez sítě, na dočasné složce.

    python3 tools/test_rewind.py

Nesahá na skutečné ~/.claude: CLAUDE_CONFIG_DIR se nastaví na dočasnou složku
dřív, než se modul načte. Snaží se vrácení hlavně rozbít: podvržené cesty,
chybějící zálohy, změna souboru mezi přehledem a vrácením, dvojí použití
tokenu, souběh, poškozený přepis, odkazy, binární soubory…
"""
import hashlib
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest

TMP = tempfile.mkdtemp(prefix="rewind-test-")
os.environ["CLAUDE_CONFIG_DIR"] = os.path.join(TMP, "cfg")
os.makedirs(os.environ["CLAUDE_CONFIG_DIR"])
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hub import core, rewind  # noqa: E402

SID = "11111111-2222-3333-4444-555555555555"
M = "aaaaaaaa-0000-0000-0000-000000000001"     # zpráva, ke které se vracíme
M2 = "aaaaaaaa-0000-0000-0000-000000000002"     # pozdější zpráva
M3 = "aaaaaaaa-0000-0000-0000-000000000003"


def h16(p):
    return hashlib.sha256(p.encode()).hexdigest()[:16]


class Svet:
    """Dočasný projekt + přepis + file-history."""

    def __init__(self, nazev):
        self.root = tempfile.mkdtemp(prefix=nazev + "-", dir=TMP)
        self.proj = os.path.join(self.root, "projekt s mezerou")
        os.makedirs(self.proj)
        self.fh = os.path.join(core.CLAUDE_DIR, "file-history", SID)
        shutil.rmtree(self.fh, ignore_errors=True)
        os.makedirs(self.fh)
        self.slozka = os.path.join(core.CLAUDE_DIR, "projects", "-x")
        os.makedirs(self.slozka, exist_ok=True)
        self.prepis = os.path.join(self.slozka, SID + ".jsonl")
        self.radky = []

    def soubor(self, rel, text, bajty=False):
        p = os.path.join(self.proj, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb" if bajty else "w") as fh:
            fh.write(text)
        return p

    def zaloha(self, p, verze, text):
        jmeno = "%s@v%d" % (h16(p), verze)
        with open(os.path.join(self.fh, jmeno), "wb") as fh:
            fh.write(text if isinstance(text, bytes) else text.encode())
        return jmeno

    def zaznam(self, p, jmeno, verze):
        return {"backupFileName": jmeno, "version": verze, "backupTime": "2026-10-01T00:00:00.000Z",
                "realParentDir": os.path.dirname(p)}

    def snapshot(self, mid, bk, update=False):
        self.radky.append(json.dumps({"type": "file-history-snapshot", "messageId": mid,
                                      "snapshot": {"messageId": mid, "trackedFileBackups": bk,
                                                   "timestamp": "2026-10-01T00:00:00.000Z"},
                                      "isSnapshotUpdate": update}, separators=(",", ":")))

    def zprava(self, mid):
        self.radky.append(json.dumps({"type": "user", "uuid": mid, "message": {"role": "user", "content": "ahoj"}}))

    def zapis(self, konec=""):
        with open(self.prepis, "w") as fh:
            fh.write("\n".join(self.radky) + "\n" + konec)

    def cti(self, p):
        try:
            with open(p, "rb") as fh:
                return fh.read()
        except FileNotFoundError:
            return None


def standard():
    """A: hlídaný už u M (v2), pak upravený. B: hlídaný až po M (v1 je původní stav).
    C: vytvořil ho Claude po M (bez v1). D: u M neexistoval (null), teď existuje."""
    w = Svet("standard")
    a = w.soubor("a.txt", "A nove\nradek 2\n")
    b = w.soubor("sub/b.txt", "B nove\n")
    c = w.soubor("c.txt", "C vytvoreno\n")
    d = w.soubor("d.txt", "D existuje\n")
    za = w.zaloha(a, 2, "A puvodni\nradek 2\n")
    w.zaloha(a, 3, "A mezistav\n")
    w.zaloha(b, 1, "B puvodni\n")
    zb2 = w.zaloha(b, 2, "B mezistav\n")
    zc2 = w.zaloha(c, 2, "C mezistav\n")
    w.snapshot(M, {a: w.zaznam(a, za, 2), d: w.zaznam(d, None, 2)})
    w.zprava(M)
    w.snapshot(M2, {a: w.zaznam(a, "%s@v3" % h16(a), 3), b: w.zaznam(b, zb2, 2),
                    c: w.zaznam(c, zc2, 2), d: w.zaznam(d, None, 2)})
    w.zprava(M2)
    w.zapis()
    return w, a, b, c, d


class Zaklad(unittest.TestCase):
    def setUp(self):
        rewind._plany.clear()

    def prehled(self, uuid=M):
        r = rewind.api("rewind-prehled", {"chat": SID, "uuid": uuid})
        self.assertNotIn("error", r, r)
        return r

    def proved(self, token, **kw):
        return rewind.api("rewind-proved", dict({"chat": SID, "uuid": M, "token": token}, **kw))

    @staticmethod
    def pis(cesta, text):
        with open(cesta, "w") as fh:
            fh.write(text)

    def akce(self, r):
        return {os.path.basename(x["path"]): x["akce"] for x in r["soubory"]}


class Hlavni(Zaklad):
    def test_prehled_nic_nemeni_a_ukaze_diff(self):
        w, a, b, c, d = standard()
        pred = {p: w.cti(p) for p in (a, b, c, d)}
        prepis = w.cti(w.prepis)
        r = self.prehled()
        self.assertEqual(self.akce(r), {"a.txt": "obnovit", "b.txt": "obnovit", "c.txt": "smazat", "d.txt": "smazat"})
        x = [s for s in r["soubory"] if s["path"] == a][0]
        self.assertEqual((x["plus"], x["minus"]), (1, 1))
        self.assertIn("-A nove", x["diff"])
        self.assertIn("+A puvodni", x["diff"])
        self.assertEqual(pred, {p: w.cti(p) for p in (a, b, c, d)})
        self.assertEqual(prepis, w.cti(w.prepis))

    def test_vraceni_bez_smazani_a_zpet(self):
        w, a, b, c, d = standard()
        r = self.proved(self.prehled()["token"])
        self.assertEqual((r["vraceno"], r["smazano"]), (2, 0), r)
        self.assertEqual(w.cti(a), b"A puvodni\nradek 2\n")
        self.assertEqual(w.cti(b), b"B puvodni\n")
        self.assertEqual(w.cti(c), b"C vytvoreno\n")       # smazání nebylo potvrzeno
        self.assertEqual(w.cti(d), b"D existuje\n")
        self.assertTrue(os.path.isfile(os.path.join(core.CLAUDE_DIR, "hub-rewind", r["zaloha"], "manifest.json")))
        z = rewind.api("rewind-zpet", {"zaloha": r["zaloha"]})
        self.assertEqual(z["obnoveno"], 2, z)
        self.assertEqual(w.cti(a), b"A nove\nradek 2\n")
        self.assertEqual(w.cti(b), b"B nove\n")
        self.assertIn("error", rewind.api("rewind-zpet", {"zaloha": r["zaloha"]}))   # podruhé ne

    def test_smazani_s_potvrzenim_a_zpet(self):
        w, a, b, c, d = standard()
        r = self.proved(self.prehled()["token"], smazat=True)
        self.assertEqual((r["vraceno"], r["smazano"]), (2, 2), r)
        self.assertIsNone(w.cti(c))
        self.assertIsNone(w.cti(d))
        z = rewind.api("rewind-zpet", {"zaloha": r["zaloha"]})
        self.assertEqual(z["obnoveno"], 4, z)
        self.assertEqual(w.cti(c), b"C vytvoreno\n")
        self.assertEqual(w.cti(d), b"D existuje\n")

    def test_smazat_musi_byt_pravda_ne_neco_pravdivehmo(self):
        w, a, b, c, d = standard()
        r = self.proved(self.prehled()["token"], smazat="ano")
        self.assertEqual(r["smazano"], 0, r)
        self.assertEqual(w.cti(c), b"C vytvoreno\n")

    def test_zmena_mezi_prehledem_a_vracenim(self):
        w, a, b, c, d = standard()
        t = self.prehled()["token"]
        w.soubor("a.txt", "mezitim jsem to prepsal\n")
        r = self.proved(t, smazat=True)
        self.assertIn("error", r)
        self.assertEqual(w.cti(a), b"mezitim jsem to prepsal\n")
        self.assertEqual(w.cti(b), b"B nove\n")
        self.assertEqual(w.cti(c), b"C vytvoreno\n")

    def test_novy_soubor_od_prehledu_taky_zastavi(self):
        w, a, b, c, d = standard()
        t = self.prehled()["token"]
        os.remove(c)
        self.assertIn("error", self.proved(t))
        self.assertEqual(w.cti(a), b"A nove\nradek 2\n")

    def test_token_jen_jednou_a_jen_pro_svou_zpravu(self):
        w, a, b, c, d = standard()
        t = self.prehled()["token"]
        self.assertIn("error", rewind.api("rewind-proved", {"chat": SID, "uuid": M2, "token": t}))
        t = self.prehled()["token"]
        self.assertNotIn("error", self.proved(t))
        self.assertIn("error", self.proved(t))                 # podruhé už ne
        for spatny in (None, "", "x" * 5000, 5, ["a"], {"a": 1}):
            self.assertIn("error", self.proved(spatny))

    def test_sourucasne_dve_vraceni(self):
        w, a, b, c, d = standard()
        t1, t2 = self.prehled()["token"], self.prehled()["token"]
        out = []
        bar = threading.Barrier(2)

        def jed(t):
            bar.wait()
            out.append(self.proved(t))
        vl = [threading.Thread(target=jed, args=(t,)) for t in (t1, t2)]
        [v.start() for v in vl]
        [v.join() for v in vl]
        self.assertEqual(len(out), 2)
        # nic se nesmí rozbít a soubory jsou ve stavu po vrácení
        self.assertEqual(w.cti(a), b"A puvodni\nradek 2\n")
        self.assertEqual(w.cti(b), b"B puvodni\n")
        self.assertTrue(any(not r.get("error") for r in out), out)

    def test_dvojklik_stejny_token(self):
        w, a, b, c, d = standard()
        t = self.prehled()["token"]
        out = []
        vl = [threading.Thread(target=lambda: out.append(self.proved(t))) for _ in range(6)]
        [v.start() for v in vl]
        [v.join() for v in vl]
        self.assertEqual(sum(1 for r in out if not r.get("error")), 1, out)

    def test_znovu_po_vraceni_je_beze_zmeny(self):
        w, a, b, c, d = standard()
        self.proved(self.prehled()["token"], smazat=True)
        r = self.prehled()
        self.assertEqual(r["soubory"], [])
        self.assertEqual(r["bez_zmeny"], 4)
        out = self.proved(r["token"])
        self.assertEqual((out["vraceno"], out["smazano"], out["zaloha"]), (0, 0, ""))


class Odolnost(Zaklad):
    def test_chybi_zaloha_preskoci(self):
        w, a, b, c, d = standard()
        os.remove(os.path.join(w.fh, "%s@v2" % h16(a)))
        r = self.prehled()
        self.assertEqual(self.akce(r)["a.txt"], "preskocit")
        out = self.proved(r["token"])
        self.assertEqual(w.cti(a), b"A nove\nradek 2\n")
        self.assertEqual(w.cti(b), b"B puvodni\n")
        self.assertTrue(any(p["path"] == a for p in out["preskoceno"]))

    def test_chybi_v1_a_nejde_o_v2_preskoci(self):
        w, a, b, c, d = standard()
        os.remove(os.path.join(w.fh, "%s@v1" % h16(b)))
        # b se objevil ve snapshotu jako v2 → vypadá jako vytvořený Claudem
        self.assertEqual(self.akce(self.prehled())["b.txt"], "smazat")
        w2, a, b, c, d = standard()
        os.remove(os.path.join(w2.fh, "%s@v1" % h16(b)))
        w2.radky = [r.replace('@v2"', '@v7"').replace('"version":2', '"version":7') if h16(b) in r and M2 in r else r
                    for r in w2.radky]
        w2.zapis()
        self.assertEqual(self.akce(self.prehled())["b.txt"], "preskocit")

    def test_zaloha_cizi_cesty(self):
        w, a, b, c, d = standard()
        # záloha jiného souboru podstrčená pod cestu a.txt
        w.radky = [r.replace(h16(a) + "@v2", h16(b) + "@v1") for r in w.radky]
        w.zapis()
        r = self.prehled()
        self.assertEqual(self.akce(r)["a.txt"], "preskocit")
        self.proved(r["token"])
        self.assertEqual(w.cti(a), b"A nove\nradek 2\n")

    def test_podvrzene_cesty(self):
        w = Svet("podvrh")
        cizi = os.path.join(TMP, "cizi.txt")
        with open(cizi, "w") as fh:
            fh.write("NEMENIT")
        zlo = [
            "../cizi.txt", "/etc/passwd\0x", "", "a/../../cizi.txt",
            os.path.join(w.proj, "..", "cizi.txt"),
            os.path.join(core.CLAUDE_DIR, "projects", "-x", SID + ".jsonl"),
            os.path.join(core.CLAUDE_DIR, "file-history", SID, "x"),
            "\ud800.txt", "x" * 5000,
        ]
        bk = {}
        for k in zlo:
            bk[k] = {"backupFileName": None, "version": 1, "realParentDir": w.proj}
        bk["/ciste/jinde.txt"] = {"backupFileName": None, "version": 1, "realParentDir": "/jine"}
        bk["rel/x.txt"] = {"backupFileName": None, "version": 1}              # bez realParentDir
        bk[12] = "nesmysl"
        w.snapshot(M, {k: v for k, v in bk.items() if isinstance(k, str)})
        w.zprava(M)
        w.zapis()
        pred = w.cti(w.prepis)
        r = rewind.api("rewind-prehled", {"chat": SID, "uuid": M})
        self.assertNotIn("error", r, r)
        self.assertEqual([x for x in r["soubory"] if x["akce"] != "preskocit"], [])
        out = self.proved(r["token"], smazat=True)
        self.assertEqual((out.get("vraceno"), out.get("smazano")), (0, 0), out)
        self.assertEqual(w.cti(cizi), b"NEMENIT")
        self.assertEqual(pred, w.cti(w.prepis))

    def test_nikdy_nepise_do_prepisu(self):
        w, a, b, c, d = standard()
        # i kdyby snapshot ukazoval na přepis, nesmí se ho dotknout
        w.snapshot(M, {w.prepis: w.zaznam(w.prepis, None, 2)})
        w.zapis()
        pred = w.cti(w.prepis)
        r = self.prehled()
        self.proved(r["token"], smazat=True)
        self.assertEqual(pred, w.cti(w.prepis))

    def test_symbolicky_odkaz(self):
        w, a, b, c, d = standard()
        cil = os.path.join(TMP, "cil-odkazu.txt")
        with open(cil, "w") as fh:
            fh.write("cil")
        os.remove(a)
        os.symlink(cil, a)
        r = self.prehled()
        self.assertEqual(self.akce(r)["a.txt"], "preskocit")
        self.proved(r["token"], smazat=True)
        self.assertEqual(w.cti(cil), b"cil")
        self.assertTrue(os.path.islink(a))

    def test_binarni_a_velky_soubor_bez_diffu(self):
        w = Svet("bin")
        p = w.soubor("img.bin", b"\x00\x01\x02NOVE", bajty=True)
        big = w.soubor("big.txt", "x\n" * 300_000)
        z1 = w.zaloha(p, 2, b"\x00\xffSTARE")
        z2 = w.zaloha(big, 2, "y\n" * 300_000)
        w.snapshot(M, {p: w.zaznam(p, z1, 2), big: w.zaznam(big, z2, 2)})
        w.zprava(M)
        w.zapis()
        r = self.prehled()
        for x in r["soubory"]:
            self.assertIn("bez_diffu", x)
            self.assertIsNone(x["diff"])
        self.proved(r["token"])
        self.assertEqual(w.cti(p), b"\x00\xffSTARE")

    def test_prava_se_zachovaji(self):
        w, a, b, c, d = standard()
        os.chmod(a, 0o755)
        self.proved(self.prehled()["token"])
        self.assertEqual(os.stat(a).st_mode & 0o777, 0o755)

    def test_nelze_zapsat(self):
        if os.geteuid() == 0:
            self.skipTest("root")
        w, a, b, c, d = standard()
        t = self.prehled()["token"]
        os.chmod(os.path.dirname(b), 0o500)
        try:
            out = self.proved(t, smazat=True)
        finally:
            os.chmod(os.path.dirname(b), 0o755)
        self.assertFalse(out.get("error"), out)
        self.assertTrue(any(p["path"] == b for p in out["preskoceno"]), out)
        self.assertEqual(w.cti(b), b"B nove\n")
        self.assertEqual(w.cti(a), b"A puvodni\nradek 2\n")

    def test_poskozeny_prepis(self):
        w, a, b, c, d = standard()
        w.zapis(konec='{"type":"file-history-snapshot","messageId":"%s","snapshot":{"trackedFileBa' % M3)
        self.assertNotIn("error", rewind.api("rewind-prehled", {"chat": SID, "uuid": M}))
        w.radky.insert(0, "nesmysl {{{")
        w.radky.insert(1, "")
        w.radky.insert(2, '{"type":"file-history-snapshot"}')
        w.radky.insert(3, '{"type":"file-history-snapshot","messageId":5,"snapshot":{"trackedFileBackups":[]}}')
        w.zapis()
        self.assertNotIn("error", rewind.api("rewind-prehled", {"chat": SID, "uuid": M}))

    def test_bez_snapshotu_prazdny_a_spatne_vstupy(self):
        w, a, b, c, d = standard()
        self.assertIn("error", rewind.api("rewind-prehled", {"chat": SID, "uuid": M3}))
        open(w.prepis, "w").close()
        self.assertIn("error", rewind.api("rewind-prehled", {"chat": SID, "uuid": M}))
        for chat, uuid in (("", ""), ("../../etc/passwd", M), (SID, "../x"), (SID, M + "\0"),
                           ("0" * 36, M), (None, None), (5, 5), ([], {})):
            self.assertIn("error", rewind.api("rewind-prehled", {"chat": chat, "uuid": uuid}), (chat, uuid))
        for payload in (None, [], "x", 5, {"zaloha": "../.."}):
            for jmeno in ("rewind-prehled", "rewind-proved", "rewind-zpet", "rewind-nic"):
                self.assertIn("error", rewind.api(jmeno, payload))

    def test_snapshot_update_prepise_starsi(self):
        w, a, b, c, d = standard()
        w.snapshot(M, {}, update=True)           # M nově eviduje jen prázdno
        w.zapis()
        r = self.prehled()
        # a.txt se objevil až jako v3 a v1 chybí → nejde říct, jaký byl → přeskočit, ne hádat
        self.assertEqual(self.akce(r)["a.txt"], "preskocit")

    def test_snapshoty_bez_zmeny_nic_nenabidnou(self):
        w = Svet("prazdny")
        w.snapshot(M, {})
        w.zprava(M)
        w.zapis()
        r = self.prehled()
        self.assertEqual((r["soubory"], r["celkem"]), ([], 0))
        out = self.proved(r["token"], smazat=True)
        self.assertEqual((out["vraceno"], out["smazano"]), (0, 0))

    def test_uprava_z_posledniho_kola(self):
        w = Svet("posledni")
        p = w.soubor("novy.txt", "po uprave\n")
        w.zaloha(p, 1, "pred upravou\n")
        w.snapshot(M, {})
        w.zprava(M)
        w.radky.append(json.dumps({"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": "t1", "name": "Edit", "input": {"file_path": p}}]}}))
        w.zapis()
        r = self.prehled()
        self.assertEqual(self.akce(r), {"novy.txt": "obnovit"})
        self.proved(r["token"])
        self.assertEqual(w.cti(p), b"pred upravou\n")
        # úprava bez zálohy ve file-history se neeviduje → nikdy nic nepřepíše
        w2 = Svet("posledni2")
        q = w2.soubor("jiny.txt", "x\n")
        w2.snapshot(M, {})
        w2.radky.append(json.dumps({"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": "t1", "name": "Write", "input": {"file_path": q}}]}}))
        w2.zapis()
        self.assertEqual(self.prehled()["soubory"], [])

    def test_unicode_a_mezery(self):
        w = Svet("unicode")
        p = w.soubor("složka s mezerou/Žluťoučký kůň ✓.txt", "nove ✓\n")
        z = w.zaloha(p, 2, "původní ✓\n")
        w.snapshot(M, {p: w.zaznam(p, z, 2)})
        w.zprava(M)
        w.zapis()
        r = self.prehled()
        self.proved(r["token"])
        self.assertEqual(w.cti(p), "původní ✓\n".encode())

    def test_relativni_klic(self):
        w = Svet("rel")
        p = w.soubor("src/rel.py", "nove\n")
        z = w.zaloha(p, 2, "stare\n")
        w.snapshot(M, {"src/rel.py": w.zaznam(p, z, 2)})
        w.zprava(M)
        w.zapis()
        self.proved(self.prehled()["token"])
        self.assertEqual(w.cti(p), b"stare\n")

    def test_zpet_po_zmene_nechava_byt(self):
        w, a, b, c, d = standard()
        r = self.proved(self.prehled()["token"], smazat=True)
        w.soubor("a.txt", "uz jsem do toho zase sahl\n")
        z = rewind.api("rewind-zpet", {"zaloha": r["zaloha"]})
        self.assertEqual(w.cti(a), b"uz jsem do toho zase sahl\n")
        self.assertTrue(any(p["path"] == a for p in z["preskoceno"]), z)
        self.assertEqual(z["obnoveno"], 3, z)

    def test_zpet_podvrzeny_manifest(self):
        w, a, b, c, d = standard()
        r = self.proved(self.prehled()["token"])
        cizi = os.path.join(TMP, "cizi2.txt")
        with open(cizi, "w") as fh:
            fh.write("NEMENIT")
        mf = os.path.join(core.CLAUDE_DIR, "hub-rewind", r["zaloha"], "manifest.json")
        with open(mf) as fh:
            data = json.load(fh)
        data["polozky"][0].update(path=cizi, po=hashlib.sha256(b"NEMENIT").hexdigest(), pred="absent", stav="hotovo")
        data["polozky"][1].update(path="../relativni", stav="hotovo")
        with open(mf, "w") as fh:
            json.dump(data, fh)
        z = rewind.api("rewind-zpet", {"zaloha": r["zaloha"]})
        # manifest leží v naší složce (zapisuje ho jen hub); relativní cesta se i tak odmítne
        self.assertTrue(any("podezřelá" in p["duvod"] for p in z["preskoceno"]), z)
        for spatne in ("../..", "x", "20260101-000000-zzzzzzzz", "20260101-000000-abcdef12"):
            self.assertIn("error", rewind.api("rewind-zpet", {"zaloha": spatne}))
        mf2 = os.path.join(core.CLAUDE_DIR, "hub-rewind", "20260101-000000-abcdef12")
        os.makedirs(mf2, exist_ok=True)
        self.pis(os.path.join(mf2, "manifest.json"), "{rozbite")
        self.assertIn("error", rewind.api("rewind-zpet", {"zaloha": "20260101-000000-abcdef12"}))
        self.pis(os.path.join(mf2, "manifest.json"), '{"polozky": 5}')
        self.assertIn("error", rewind.api("rewind-zpet", {"zaloha": "20260101-000000-abcdef12"}))


class Oznaceni(unittest.TestCase):
    def test_oznac(self):
        e = {"type": "user", "uuid": M}
        b = [{"kind": "res"}, {"kind": "me", "text": "x"}]
        rewind.oznac(e, b, SID)
        self.assertEqual(b[1].get("uuid"), M)
        self.assertNotIn("uuid", b[0])
        for entry in ({"type": "assistant", "uuid": M}, {"type": "user", "uuid": "../x"}, {"type": "user"}, {}):
            bb = [{"kind": "me"}]
            rewind.oznac(entry, bb, SID)
            self.assertNotIn("uuid", bb[0])
        self.assertEqual(rewind.oznac(None, [1], SID), [1])    # nikdy nevyhodí


if __name__ == "__main__":
    try:
        unittest.main(verbosity=1, exit=False)
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
