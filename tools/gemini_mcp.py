#!/usr/bin/env python3
"""
MCP server „gemini" — Claude si přes něj volá Gemini (Google AI Pro) na pomoc.

Nástroje mcp__gemini__*: `obrazek` (fotorealistický obrázek), `prepis`
(nahrávka → text), `video` (popis videa) a `zeptat` (obecné zadání s přílohami).
Samotné volání `agy` je v gemini_pomoc.py — tentýž kód jede i jako příkaz
`gemini-pomoc` v terminálu. Hub server napojí sám (hub/gemini.py), když je
Antigravity CLI nainstalovaný a přihlášený. Jen standardní knihovna.

Protokol: JSON-RPC 2.0 po řádcích na stdin/stdout.
"""
import json
import os
import sys
import threading



class _LenyPomocnik:
    """gemini_pomoc (subprocess, uuid…) až při prvním volání nástroje. Server
    běží v každém tabu s Claudem — i v těch, které Gemini nikdy nezavolají,
    a v prostoru na serveru se paměť počítá za všechny taby dohromady."""
    _mod = None

    def __getattr__(self, name):
        if _LenyPomocnik._mod is None:
            sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
            import gemini_pomoc
            _LenyPomocnik._mod = gemini_pomoc
        return getattr(_LenyPomocnik._mod, name)


gp = _LenyPomocnik()

NAME = "gemini"
VERSION = "1.0.0"
MAX_TEXT = 80 * 1024

# Kdy Gemini a kdy ne — podle srovnání na 17 úlohách z tiskárny (25. 10. 2026):
# kancelářská práce nastejno, obrázky, zvuk a video umí jen Gemini, Claude je
# rychlejší a lepší v překladu. A hlavně: limity jsou dva oddělené.
INSTRUCTIONS = """\
Nástroje mcp__gemini__* volají Gemini od Googlu (Antigravity CLI) na předplatném
Google AI Pro uživatele. Gemini má **vlastní týdenní limit**, nezávislý na tvém
(Flash ~5 000 krátkých dotazů nebo ~2 000 s PDF či fotkou týdně, Argon ~300).

Pošli práci Gemini, když jde o:
- **obrázek** — fotorealistický vizuál, mockup (tričko, hrnek, plakát na zdi),
  produktová fotka → `obrazek`. Neskládej ho z canvasu, SVG ani HTML: Gemini
  ho udělá za necelou minutu a vypadá jako fotka.
- **nahrávku nebo hlasovou zprávu** (mp3, m4a, ogg/opus, wav) → `prepis`;
  **video** → `video`. Ty sám zvuk ani video nevnímáš.
- **hromadnou rutinu** — desítky fotek, PDF, faktur, tabulek → `zeptat`,
  hlavně když je tvůj týdenní limit vysoko (uživatel ho vidí v Aladinovi).

Nech si: překlady a texty, kde záleží na jazyku (jsi lepší), krátké rychlé
odpovědi (jsi rychlejší: ~10 s proti 16–24 s) a rozhodování za uživatele.

- Model `flash` je výchozí (rychlý, velký limit). `argon` je silnější — na
  faktury, inventury, rozměry a čísla z dokumentů (Flash jednou spletl
  rozměry), ale limit má jen ~300 týdně.
- Na web Gemini sáhne jen s `web: true`.
- Soubory se mu posílají kopií; pošli jen to, co úkol potřebuje. Výsledek
  si zkontroluj (u obrázku ho otevři přes Read), než ho předáš dál.
- Když nástroj hlásí, že Gemini není přihlášený nebo nainstalovaný, řekni to
  uživateli i s cestou v Nastavení a pokračuj bez Gemini."""

_MODEL = {"type": "string", "enum": ["flash", "argon"],
          "description": "flash = rychlý, velký limit (výchozí); argon = silnější na čísla "
                         "a dokumenty, limit ~300 týdně."}

TOOLS = [
    {"name": "obrazek",
     "description": "Vygeneruje fotorealistický obrázek přes Gemini (Google AI Pro): mockup "
                    "trička nebo plakátu, produktovou fotku, vizuál na web. Volitelně podle "
                    "předloh (logo, fotka produktu). Uloží PNG/JPG do složky (výchozí: aktuální "
                    "projekt) a vrátí cesty. Trvá kolem minuty. Použij místo kreslení obrázku "
                    "kódem (canvas, SVG).",
     "inputSchema": {"type": "object", "required": ["popis"], "properties": {
         "popis": {"type": "string",
                   "description": "Co má na obrázku být — předmět, prostředí, světlo, úhel, "
                                  "formát. Klidně česky a podrobně."},
         "predlohy": {"type": "array", "items": {"type": "string"},
                      "description": "Cesty k obrázkům, podle kterých se má řídit (logo, produkt)."},
         "slozka": {"type": "string",
                    "description": "Kam obrázek uložit. Výchozí: složka projektu."},
         "nazev": {"type": "string",
                   "description": "Základ jména souboru (bez přípony), třeba tricko-mockup."}}}},
    {"name": "prepis",
     "description": "Přepíše nahrávku nebo hlasovou zprávu (mp3, m4a, ogg/opus z WhatsAppu, "
                    "wav, webm…) na text přes Gemini. Ty sám zvuk neslyšíš — na nahrávky vždycky "
                    "tenhle nástroj.",
     "inputSchema": {"type": "object", "required": ["soubor"], "properties": {
         "soubor": {"type": "string", "description": "Cesta k nahrávce."},
         "jazyk": {"type": "string", "description": "Jazyk nahrávky, když ho víš (jinak se pozná sám)."},
         "casy": {"type": "boolean", "description": "Časové značky [mm:ss] u úseků."},
         "model": _MODEL}}},
    {"name": "video",
     "description": "Popíše video (mp4, mov, webm…) přes Gemini: co je vidět, texty na obrazovce "
                    "a doslovný přepis řeči s časy. Ty sám video nevidíš.",
     "inputSchema": {"type": "object", "required": ["soubor"], "properties": {
         "soubor": {"type": "string", "description": "Cesta k videu (nejvýš 300 MB)."},
         "otazka": {"type": "string", "description": "Na co se zaměřit (nepovinné)."},
         "model": _MODEL}}},
    {"name": "zeptat",
     "description": "Obecné zadání pro Gemini, volitelně s přílohami (PDF, fotky, tabulky, "
                    "dokumenty) a webem. Na hromadnou a rutinní práci, která by ubrala z tvého "
                    "týdenního limitu — Gemini má vlastní.",
     "inputSchema": {"type": "object", "required": ["zadani"], "properties": {
         "zadani": {"type": "string", "description": "Co má Gemini udělat, včetně tvaru odpovědi."},
         "soubory": {"type": "array", "items": {"type": "string"},
                     "description": "Cesty k přílohám (nejvýš 20 souborů, 600 MB)."},
         "model": _MODEL,
         "web": {"type": "boolean", "description": "Smí hledat na webu (výchozí ne)."},
         "slozka": {"type": "string",
                    "description": "Kam uložit soubory, které Gemini vytvoří (výchozí: projekt)."}}}},
]


def _seznam(value):
    if isinstance(value, str):
        return [value] if value.strip() else []
    return [str(v) for v in (value or []) if str(v).strip()]


def _vysledek(v, nadpis=""):
    casti = []
    if v.get("obrazky"):
        casti.append("Uloženo:\n" + "\n".join("- " + p for p in v["obrazky"]))
    if v.get("text"):
        casti.append((nadpis + "\n" if nadpis else "") + v["text"])
    casti.append(gp.paticka(v))
    return "\n\n".join(casti)


def t_obrazek(a):
    if not str(a.get("popis") or "").strip():
        raise gp.Chyba("Chybí popis obrázku.")
    v = gp.obrazek(a["popis"], _seznam(a.get("predlohy")), slozka=a.get("slozka") or None,
                   nazev=a.get("nazev") or "")
    return _vysledek(v, "Gemini k obrázku:")


def t_prepis(a):
    if not str(a.get("soubor") or "").strip():
        raise gp.Chyba("Chybí cesta k nahrávce.")
    v = gp.prepis(a["soubor"], jazyk=a.get("jazyk") or "", casy=bool(a.get("casy")),
                  model=a.get("model") or "flash")
    return _vysledek(v, "Přepis:")


def t_video(a):
    if not str(a.get("soubor") or "").strip():
        raise gp.Chyba("Chybí cesta k videu.")
    v = gp.video(a["soubor"], otazka=a.get("otazka") or "", model=a.get("model") or "flash")
    return _vysledek(v)


def t_zeptat(a):
    if not str(a.get("zadani") or "").strip():
        raise gp.Chyba("Chybí zadání.")
    v = gp.zeptat(a["zadani"], _seznam(a.get("soubory")), model=a.get("model") or "flash",
                  web=bool(a.get("web")), slozka=a.get("slozka") or None)
    return _vysledek(v)


HANDLERS = {"obrazek": t_obrazek, "prepis": t_prepis, "video": t_video, "zeptat": t_zeptat}
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
    except gp.Chyba as exc:
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
        # Volání trvá desítky vteřin až minuty — ve vlákně, ať mezitím projde ping.
        threading.Thread(target=call_tool, args=(rid, msg.get("params") or {}), daemon=True).start()
        return None
    if method == "ping":
        return send({"jsonrpc": "2.0", "id": rid, "result": {}})
    if rid is not None:
        return send({"jsonrpc": "2.0", "id": rid,
                     "error": {"code": -32601, "message": f"Metodu {method} neznám."}})
    return None


def main():
    try:
        for line in sys.stdin:
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if isinstance(msg, dict):
                handle(msg)
    finally:
        # Claude zavřel tab: rozběhnuté agy by jinak doběhly naprázdno.
        if _LenyPomocnik._mod is not None:
            gp.ukonci_vse()


if __name__ == "__main__":
    main()
