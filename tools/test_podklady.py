"""Testy gateway/podklady.py a úzkého rozsahu OAuth `podklady` — výběr složek,
soukromé poznámky, hledání jen v povolených složkách, odkazy ven z trezoru,
rozsah přenesený z kódu přes obnovení tokenu a MCP, které s takovým tokenem
neukáže ani nespustí nic jiného než čtení podkladů.

    python3 tools/test_podklady.py
"""
import json
import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
TMP = tempfile.mkdtemp(prefix="hub-podklady-test-")
os.environ["HUB_GW_DIR"] = os.path.join(TMP, "gateway")
os.environ["HUB_GW_USERS"] = os.path.join(TMP, "users")
os.environ.pop("HUB_GW_PODKLADY_REDIRECTS", None)

from gateway import mcp, podklady, workspace  # noqa: E402
from gateway.accounts import Accounts  # noqa: E402

USER = {"id": 7, "email": "tester@example.com", "name": "Tester", "vault": "Brain"}


def note(rel, text):
    path = os.path.join(VAULT, *rel.split("/"))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


class Zaklad(unittest.TestCase):
    def setUp(self):
        global VAULT
        home = workspace.home_for(USER)
        shutil.rmtree(home, ignore_errors=True)
        VAULT = workspace.vault_dir(USER, home)
        os.makedirs(VAULT)
        note("Firma/Otevírací doba.md", "# Otevírací doba\nV sobotu máme otevřeno od 9 do 12 hodin.\n")
        note("Firma/Ceník.md", "Tisk vizitek stojí 2 Kč za kus, minimum 100 kusů.\n")
        note("Firma/Interní/Hesla.md", "---\nsoukrome: true\n---\nheslo k wifi: tajne123\n")
        note("Firma/Tagged.md", "---\ntags:\n  - prace\n  - soukrome\n---\nsobota otevřeno tajně\n")
        note("Firma/Body.md", "Sobota otevřeno #citlive\n")
        note("Firma/Asistent ne.md", "---\nasistent: ne\n---\nsobota otevřeno\n")
        note("Osobní/Deník.md", "V sobotu jsem byl u doktora.\n")
        note(".obsidian/app.md", "sobota")
        note("Kořen.md", "sobota v kořeni")
        outside = os.path.join(TMP, "venku")
        os.makedirs(outside, exist_ok=True)
        with open(os.path.join(outside, "Cizí.md"), "w", encoding="utf-8") as fh:
            fh.write("sobota cizí tajemství\n")
        os.symlink(outside, os.path.join(VAULT, "Firma", "Odkaz"))
        os.symlink(os.path.join(outside, "Cizí.md"), os.path.join(VAULT, "Firma", "Odkaz.md"))


class Slozky(Zaklad):
    def test_folders_skip_hidden_and_links(self):
        cesty = {f["cesta"]: f["poznamek"] for f in podklady.folders(USER)}
        self.assertIn("Firma", cesty)
        self.assertIn("Firma/Interní", cesty)
        self.assertIn("Osobní", cesty)
        self.assertNotIn(".obsidian", cesty)
        self.assertNotIn("Firma/Odkaz", cesty)

    def test_granted_choice_only_existing(self):
        self.assertEqual(podklady.granted_choice(USER, ["Firma", "Firma/Interní", "Neni", "../x"]),
                         ["Firma"])

    def test_listing_hides_private(self):
        out = podklady.sources_listing(USER, ["Firma"])[0]["poznamky"]
        self.assertIn("Firma/Ceník.md", out)
        for hidden in ("Firma/Interní/Hesla.md", "Firma/Tagged.md", "Firma/Body.md",
                       "Firma/Asistent ne.md", "Firma/Odkaz.md"):
            self.assertNotIn(hidden, out)


class Hledani(Zaklad):
    def test_finds_inflected_and_without_diacritics(self):
        out = podklady.search(USER, ["Firma"], "Máte otevřeno v sobotu?")["vysledky"]
        self.assertEqual(out[0]["cesta"], "Firma/Otevírací doba.md")
        self.assertIn("od 9 do 12", out[0]["uryvek"])

    def test_only_granted_and_never_private_or_linked(self):
        paths = [r["cesta"] for r in podklady.search(USER, ["Firma"], "sobota")["vysledky"]]
        self.assertNotIn("Osobní/Deník.md", paths)
        self.assertNotIn("Kořen.md", paths)
        for p in paths:
            self.assertTrue(p.startswith("Firma/"))
            self.assertNotIn(p, ("Firma/Tagged.md", "Firma/Body.md", "Firma/Asistent ne.md",
                                 "Firma/Odkaz.md"))
        out = json.dumps(podklady.search(USER, ["Firma"], "tajemství heslo wifi"), ensure_ascii=False)
        self.assertNotIn("tajne123", out)
        self.assertNotIn("cizí", out)

    def test_app_can_only_narrow(self):
        out = podklady.search(USER, ["Firma"], "sobota", wanted=["Osobní", "Firma/Ceník.md"])
        self.assertEqual(out["vysledky"], [])
        out = podklady.search(USER, ["Firma"], "vizitek", wanted=["Firma/Ceník.md", "../Osobní"])
        self.assertEqual([r["cesta"] for r in out["vysledky"]], ["Firma/Ceník.md"])

    def test_empty_query(self):
        with self.assertRaises(podklady.Chyba):
            podklady.search(USER, ["Firma"], "a se na")


class Tokeny(unittest.TestCase):
    def test_scope_survives_refresh(self):
        a = Accounts(os.path.join(TMP, "accounts.db"))
        uid = a.add("owner@example.com", "heslo-heslo-1")
        user = a.by_id(uid)
        client = a.oauth_client_add("Přepis", ["https://example.com/cb"])
        code = a.oauth_code_new(user, client, "https://example.com/cb", "x" * 43,
                                "https://hub/mcp", podklady.SCOPE, ["Firma"])
        found = a.oauth_code_take(code)
        out = a.oauth_issue(found["user_id"], client, found["resource"], found["scope"],
                            found["sources"])
        self.assertEqual(out["scope"], podklady.SCOPE)
        again = a.oauth_refresh(out["refresh_token"], client)
        who = a.oauth_user(again["access_token"], "https://hub/mcp")
        self.assertEqual(who["scope"], podklady.SCOPE)
        self.assertEqual(who["sources"], ["Firma"])
        # Celý prostor bez rozsahu jako dřív.
        full = a.oauth_issue(uid, client, "https://hub/mcp")
        self.assertEqual(a.oauth_user(full["access_token"], "https://hub/mcp")["scope"], "prostor")


class Mcp(Zaklad):
    def narrow_user(self):
        return {**USER, "scope": podklady.SCOPE, "sources": ["Firma"], "client_id": "x"}

    def test_lists_only_podklady_tools(self):
        reply = mcp._handle(None, self.narrow_user(), {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        self.assertEqual({t["name"] for t in reply["result"]["tools"]}, podklady.TOOL_NAMES)

    def test_refuses_other_tools(self):
        for name in ("prikaz", "soubor_cti", "obsidian_cti", "konverzace"):
            reply = mcp._handle(None, self.narrow_user(), {
                "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": name, "arguments": {"prikaz": "id", "cesta": "Firma/Ceník.md"}}})
            self.assertTrue(reply["result"]["isError"], name)

    def test_search_through_mcp(self):
        reply = mcp._handle(None, self.narrow_user(), {
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "podklady_hledat", "arguments": {"dotaz": "cena vizitek"}}})
        self.assertFalse(reply["result"]["isError"])
        self.assertIn("Ceník", reply["result"]["content"][0]["text"])


class Aplikace(unittest.TestCase):
    def test_prepis_allowed_by_default_and_can_be_switched_off(self):
        prepis = "https://alba-rosa.cz/prepis/aladin.php"
        self.assertTrue(podklady.redirect_ok(prepis))
        self.assertFalse(mcp.redirect_ok(prepis))          # nikdy celý prostor
        self.assertTrue(podklady.app_remove(prepis))
        self.assertFalse(podklady.redirect_ok(prepis))
        self.assertFalse(podklady.app_remove(prepis))
        podklady.app_add(prepis)
        self.assertTrue(podklady.redirect_ok(prepis))

    def test_app_redirects(self):
        self.assertFalse(podklady.redirect_ok("https://example.com/prepis/aladin.php"))
        podklady.app_add("https://example.com/prepis/aladin.php")
        self.assertTrue(podklady.redirect_ok("https://example.com/prepis/aladin.php"))
        # Adresa aplikace neplatí pro celý prostor.
        self.assertFalse(mcp.redirect_ok("https://example.com/prepis/aladin.php"))
        with self.assertRaises(ValueError):
            podklady.app_add("http://example.com/x")
        self.assertTrue(podklady.app_remove("https://example.com/prepis/aladin.php"))
        self.assertFalse(podklady.redirect_ok("https://example.com/prepis/aladin.php"))


if __name__ == "__main__":
    try:
        unittest.main(verbosity=2)
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
