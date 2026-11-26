#!/usr/bin/env python3
"""Zkouška „Stáhnout z GitHubu" v průvodci (hub/core.py clone_vault): adresa
repa se ověří dřív, než se volá git, a špatná vrátí srozumitelnou chybu, ne
NameError. Bez sítě, v dočasné složce.   python3 tools/test_clone_vault.py"""
import os
import sys
import tempfile
import unittest

TMP = tempfile.mkdtemp(prefix="clone-vault-test-")
os.environ["CLAUDE_CONFIG_DIR"] = TMP
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from hub import core  # noqa: E402


class Adresa(unittest.TestCase):
    def test_dobre(self):
        for repo in ("jmeno/pamet", "a.b/c-d_e.git", "https://github.com/jmeno/pamet",
                     "https://github.com/jmeno/pamet.git", "git@github.com:jmeno/pamet.git"):
            self.assertEqual(core._check_repo(" " + repo + " "), repo)

    def test_spatne(self):
        for repo in ("", "pamet", "-u/x", "--upload-pack=touch /tmp/x", "ext::sh -c id",
                     "file:///etc", "http://github.com/a/b", "a/b c", "../x", "a/..", None, 5):
            with self.assertRaises(ValueError, msg=repr(repo)):
                core._check_repo(repo)

    def test_clone_bez_site(self):
        # Špatná adresa skončí ValueError (okno ukáže hlášku), ne NameError.
        with self.assertRaises(ValueError):
            core.clone_vault("ext::sh -c id", os.path.join(TMP, "vault"))
        self.assertFalse(os.path.exists(os.path.join(TMP, "vault")))


if __name__ == "__main__":
    unittest.main()
