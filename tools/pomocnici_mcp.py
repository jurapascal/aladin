#!/usr/bin/env python3
"""
MCP server „pomocnici" — Claude si přes něj volá jiné AI agenty na pomoc.

Nástroje mcp__pomocnici__*: `seznam` (kdo je k dispozici, v čem je dobrý, jaké
má modely) a `zeptat` (zadá úkol vybranému pomocníkovi a vrátí odpověď).
Pomocníci: Codex, opencode, aider a Ollama (lokální modely). Koho Claude smí
volat, určuje člověk v Nastavení → AI agenti (hub-config.json, `pomocnici`);
hlavní model si z seznamu vybere sám, kdo se na úkol hodí nejlíp.
Gemini má vlastní server (gemini_mcp.py). Jen standardní knihovna.

Protokol: JSON-RPC 2.0 po řádcích na stdin/stdout.
"""
import json
import os
import shutil
import subprocess
import sys
import threading
import time

NAME = "pomocnici"
VERSION = "1.0.0"
MAX_TEXT = 80 * 1024
TIMEOUT = 20 * 60
IS_WINDOWS = os.name == "nt"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if IS_WINDOWS else 0

CLAUDE_DIR = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")
CONFIG = os.path.join(CLAUDE_DIR, "hub-config.json")

# Silné stránky jsou sepsané pro hlavní model: podle nich si vybírá.
POPIS = {
    "codex": {
        "nazev": "Codex (OpenAI)",
        "bin": "codex",
        "umi": "Kód: nové funkce, opravy chyb, refaktoring, testy, code review. Pracuje nad složkou "
               "projektu a umí soubory měnit. Jede na předplatném ChatGPT — vlastní limit, šetří tvůj.",
        "soubory": True,
    },
    "opencode": {
        "nazev": "opencode",
        "bin": "opencode",
        "umi": "Otevřený agent nad složkou projektu, jde mu podstrčit libovolný model (i zdarma dostupné "
               "a lokální). Hodí se na hromadné a rutinní úpravy souborů a na druhý názor jiného modelu.",
        "soubory": True,
    },
    "aider": {
        "nazev": "aider",
        "bin": "aider",
        "umi": "Malé cílené změny kódu přes git (po každé úpravě commit). Dobrý na jasně zadané úpravy "
               "pár souborů. Potřebuje nastavený model a klíč u poskytovatele.",
        "soubory": True,
    },
    "ollama": {
        "nazev": "Ollama (lokální modely)",
        "bin": "ollama",
        "umi": "Lokální modely na počítači — zdarma a bez odesílání dat ven. JEN TEXT: shrnutí, překlad, "
               "třídění, koncepty. Soubory nevidí ani nemění; text, který má zpracovat, mu dej do zadání.",
        "soubory": False,
    },
}

INSTRUCTIONS = """\
Nástroje mcp__pomocnici__* volají jiné AI agenty, které má uživatel na počítači.
Vždy nejdřív zavolej `seznam`: ukáže, koho smíš volat, v čem je dobrý a jaké má modely.
Z něj SÁM vyber nejvhodnějšího pro daný úkol (a model), místo abys dělal všechno sám —
hlavně hromadnou a rutinní práci, kód nad složkou (Codex, opencode, aider) nebo čistě
textovou práci na lokálním modelu (Ollama). Jádro, rozhodování a komunikaci s uživatelem
si nech.

Pomocníci smějí v projektu měnit soubory (kromě Ollamy, která soubory nevidí). Dej jim jasné
zadání s tvarem odpovědi, po nich výsledek zkontroluj (git diff, přečti soubor), než ho předáš
dál. Když pomocník hlásí chybu (nepřihlášený, bez modelu), řekni to uživateli i s cestou
v Nastavení → AI agenti a pokračuj sám."""

TOOLS = [
    {"name": "seznam",
     "description": "Vypíše pomocníky, které smíš volat: kdo je nainstalovaný, v čem je dobrý a jaké má "
                    "modely. Zavolej před každým `zeptat`, ať vybereš toho nejlepšího.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "zeptat",
     "description": "Zadá úkol vybranému pomocníkovi (codex, opencode, aider, ollama) a vrátí jeho odpověď. "
                    "Codex, opencode a aider pracují nad složkou a mohou měnit soubory; Ollama je jen "
                    "textový chat. Trvá desítky vteřin až minuty.",
     "inputSchema": {"type": "object", "required": ["agent", "zadani"], "properties": {
         "agent": {"type": "string", "enum": list(POPIS),
                   "description": "Kdo úkol dostane — vybereš ze `seznam`."},
         "zadani": {"type": "string",
                    "description": "Co má udělat, včetně tvaru odpovědi. Samostatné: pomocník nevidí "
                                   "tuhle konverzaci."},
         "model": {"type": "string",
                   "description": "Model ze `seznam` (u Ollamy povinný, jinak výchozí pomocníkův)."},
         "slozka": {"type": "string",
                    "description": "Složka, nad kterou pracuje (výchozí: aktuální projekt)."},
         "jen_cist": {"type": "boolean",
                      "description": "Nic neměnit, jen číst a radit (výchozí ne — smí měnit)."}}}},
]


class Chyba(Exception):
    pass


def _config():
    try:
        with open(CONFIG, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _povoleni():
    seznam = _config().get("pomocnici")
    return [a for a in (seznam if isinstance(seznam, list) else []) if a in POPIS]


def _najdi(binarka):
    cesta = shutil.which(binarka)
    if cesta:
        return cesta
    for extra in (os.path.join(os.path.expanduser("~"), ".local", "bin"),
                  os.path.join(os.path.expanduser("~"), ".opencode", "bin"),
                  "/usr/local/bin", "/opt/homebrew/bin"):
        kandidat = shutil.which(binarka, path=extra)
        if kandidat:
            return kandidat
    return ""


def _spust(argv, cwd=None, timeout=15):
    try:
        r = subprocess.run(argv, capture_output=True, text=True, cwd=cwd, timeout=timeout,
                           creationflags=NO_WINDOW, stdin=subprocess.DEVNULL)
        return r.returncode, (r.stdout or ""), (r.stderr or "")
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, "", str(exc)


def _modely(agent, cesta):
    if agent == "ollama":
        kod, out, _ = _spust([cesta, "list"], timeout=10)
        radky = [r.split()[0] for r in out.splitlines()[1:] if r.strip()] if kod == 0 else []
        return radky
    if agent == "opencode":
        kod, out, _ = _spust([cesta, "models"], timeout=12)
        return [r.strip() for r in out.splitlines() if "/" in r][:40] if kod == 0 else []
    return []


def t_seznam(_a):
    povolene = _povoleni()
    if not povolene:
        return ("Uživatel zatím žádného pomocníka nepovolil. Může to udělat v Aladinovi: "
                "Nastavení → AI agenti → Spolupráce modelů. Pokračuj sám.")
    casti = []
    for agent in povolene:
        p = POPIS[agent]
        cesta = _najdi(p["bin"])
        if not cesta:
            casti.append(f"## {agent} — {p['nazev']}\nNení nainstalovaný (Nastavení → AI agenti).")
            continue
        modely = _modely(agent, cesta)
        radek = [f"## {agent} — {p['nazev']}", p["umi"],
                 "Soubory: " + ("umí číst i měnit" if p["soubory"] else "nevidí je (jen text)")]
        if modely:
            radek.append("Modely: " + ", ".join(modely))
        elif agent == "ollama":
            radek.append("Modely: žádný lokální model není stažený — `ollama pull <model>`.")
        else:
            radek.append("Modely: výchozí nastavení agenta (parametr model nech prázdný).")
        casti.append("\n".join(radek))
    return "\n\n".join(casti)


def _argv(agent, cesta, zadani, model, slozka, jen_cist):
    if agent == "codex":
        argv = [cesta, "exec", "--skip-git-repo-check", "-C", slozka,
                "-s", "read-only" if jen_cist else "workspace-write"]
        if model:
            argv += ["-m", model]
        return argv + [zadani]
    if agent == "opencode":
        argv = [cesta, "run"]
        if model:
            argv += ["-m", model]
        pokyn = ("Jen čti a radi, žádné soubory neměň.\n\n" if jen_cist else "") + zadani
        return argv + [pokyn]
    if agent == "aider":
        argv = [cesta, "--yes-always", "--no-pretty", "--no-stream", "--message",
                ("/ask " if jen_cist else "") + zadani]
        if model:
            argv += ["--model", model]
        return argv
    if agent == "ollama":
        if not model:
            raise Chyba("U Ollamy je potřeba zvolit model (viz `seznam`).")
        return [cesta, "run", model, zadani]
    raise Chyba("Neznámý pomocník.")


def t_zeptat(a):
    agent = str(a.get("agent") or "")
    zadani = str(a.get("zadani") or "").strip()
    if agent not in POPIS:
        raise Chyba("Neznámý pomocník — vyber ze `seznam`.")
    if not zadani:
        raise Chyba("Chybí zadání.")
    if agent not in _povoleni():
        raise Chyba(f"{POPIS[agent]['nazev']} uživatel nepovolil (Nastavení → AI agenti → Spolupráce modelů).")
    cesta = _najdi(POPIS[agent]["bin"])
    if not cesta:
        raise Chyba(f"{POPIS[agent]['nazev']} není nainstalovaný (Nastavení → AI agenti).")
    slozka = str(a.get("slozka") or "").strip() or os.getcwd()
    if not os.path.isdir(slozka):
        raise Chyba(f"Složka neexistuje: {slozka}")
    model = str(a.get("model") or "").strip()
    jen_cist = bool(a.get("jen_cist"))
    argv = _argv(agent, cesta, zadani, model, slozka, jen_cist)
    start = time.time()
    try:
        r = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", errors="replace",
                           cwd=slozka, timeout=TIMEOUT, creationflags=NO_WINDOW,
                           stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        raise Chyba(f"{POPIS[agent]['nazev']} neodpověděl do {TIMEOUT // 60} minut.")
    except OSError as exc:
        raise Chyba(f"{POPIS[agent]['nazev']} se nepodařilo spustit: {exc}")
    text = (r.stdout or "").strip()
    chyba = (r.stderr or "").strip()
    if r.returncode != 0 and not text:
        tip = ""
        if any(x in chyba.lower() for x in ("401", "unauthorized", "login", "api key", "api_key", "not logged")):
            tip = (" Nejspíš není přihlášený nebo nemá klíč — Nastavení → AI agenti → Přihlásit. "
                   "Řekni to uživateli a pokračuj sám.")
        raise Chyba(f"{POPIS[agent]['nazev']} skončil chybou ({r.returncode}): {chyba[-500:] or 'bez hlášky'}{tip}")
    paticka = f"\n\n— {POPIS[agent]['nazev']}" + (f", model {model}" if model else "") + \
              f", {int(time.time() - start)} s" + (", jen čtení" if jen_cist else "")
    if agent != "ollama" and not jen_cist:
        paticka += ". Mohl změnit soubory ve složce — zkontroluj (git diff)."
    return (text or "(prázdná odpověď)") + paticka


HANDLERS = {"seznam": t_seznam, "zeptat": t_zeptat}
_OUT = threading.Lock()


def send(msg):
    with _OUT:
        sys.stdout.write(json.dumps(msg, ensure_ascii=False) + "\n")
        sys.stdout.flush()


def call_tool(rid, params):
    handler = HANDLERS.get(params.get("name"))
    args = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
    if not handler:
        return send({"jsonrpc": "2.0", "id": rid,
                     "error": {"code": -32602, "message": f"Neznámý nástroj {params.get('name')}"}})
    try:
        out, error = handler(args), False
    except Chyba as exc:
        out, error = str(exc), True
    except Exception as exc:
        out, error = f"Nástroj selhal: {exc}", True
    if len(out) > MAX_TEXT:
        out = out[:MAX_TEXT] + "\n… (zkráceno)"
    send({"jsonrpc": "2.0", "id": rid,
          "result": {"content": [{"type": "text", "text": out}], "isError": error}})


def handle(msg):
    method, rid = msg.get("method"), msg.get("id")
    if method == "initialize":
        asked = (msg.get("params") or {}).get("protocolVersion") or "2025-06-18"
        return send({"jsonrpc": "2.0", "id": rid, "result": {
            "protocolVersion": asked, "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": NAME, "version": VERSION}, "instructions": INSTRUCTIONS}})
    if method == "tools/list":
        return send({"jsonrpc": "2.0", "id": rid, "result": {"tools": TOOLS}})
    if method == "tools/call":
        threading.Thread(target=call_tool, args=(rid, msg.get("params") or {}), daemon=True).start()
        return None
    if method == "ping":
        return send({"jsonrpc": "2.0", "id": rid, "result": {}})
    if rid is not None:
        return send({"jsonrpc": "2.0", "id": rid,
                     "error": {"code": -32601, "message": f"Metodu {method} neznám."}})
    return None


def main():
    for line in sys.stdin:
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        if isinstance(msg, dict):
            handle(msg)


if __name__ == "__main__":
    main()
