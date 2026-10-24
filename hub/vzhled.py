"""
Název a ikona appky — pro všechny stejné: Aladin a zlatá lampa. Platí
v okně i na liště hubu, pro ikonu na ploše telefonu (PWA) a pro spouštěč na
počítači.

Dřív si je šlo změnit (Nastavení → Vzhled, `app_name` v hub-config
a ~/.claude/hub-ikona-*.png). Uložené hodnoty se teď nečtou; nazev()
a ikona() zůstávají kvůli místům, která se na ně ptají (lišta, okno, manifest).
"""
import html
import json
import os

from . import core

VYCHOZI = "Aladin"
VELIKOSTI = (512, 192, 32)
STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")


def nazev():
    """Vlastní název — už žádný, platí VYCHOZI."""
    return ""


def zobrazeny():
    return nazev() or VYCHOZI


def ikona(size):
    """Vlastní ikona — už žádná, platí výchozí z hub/static."""
    return ""


def ikona_nebo_vychozi(size):
    return ikona(size) or os.path.join(STATIC, f"icon-{int(size)}.png")


def verze():
    """Mění se s každou novou ikonou — do adres kvůli cache prohlížeče."""
    times = [os.path.getmtime(p) for p in (ikona(s) for s in VELIKOSTI) if p]
    return int(max(times)) if times else 0


def stav():
    return {"name": nazev(), "shown": zobrazeny(), "default": VYCHOZI,
            "icon": verze(), "pwa_id": str(core.CONFIG.get("pwa_id") or "")}


# ── stránka a manifest ───────────────────────────────────────────────────────
def adresy():
    """Kde si prohlížeč bez přihlášení vezme manifest a ikonu na plochu.
    Na serveru přes bránu s kódem účtu, na počítači z hubu samotného."""
    pid = str(core.CONFIG.get("pwa_id") or "")
    v = verze()
    # Adresa manifestu se nemění (bez verze): Chrome u nainstalované appky
    # hlídá manifest na téže adrese a novou ikonu či název převezme sám —
    # změnu pozná podle adres ikon, ty nesou verzi.
    if core.CONFIG.get("gateway_user") and pid:
        return {"manifest": f"/manifest.webmanifest?u={pid}",
                "touch": f"/app-ikona/{pid}/192.png?v={v}"}
    return {"manifest": "/manifest.webmanifest", "touch": f"/app-ikona/192.png?v={v}"}


def uprav_stranku(page):
    """index.html s vlastním názvem a odkazy na vlastní manifest a ikony."""
    text = page.decode("utf-8")
    name = html.escape(zobrazeny())
    a = adresy()
    v = verze()
    text = text.replace("<title>Aladin</title>", f"<title>{name}</title>", 1)
    text = text.replace('<link rel="manifest" href="/manifest.webmanifest">',
                        f'<link rel="manifest" href="{html.escape(a["manifest"])}">', 1)
    text = text.replace('<link rel="apple-touch-icon" href="/icon-192.png">',
                        f'<link rel="apple-touch-icon" href="{html.escape(a["touch"])}">', 1)
    text = text.replace('<meta name="apple-mobile-web-app-title" content="Aladin">',
                        f'<meta name="apple-mobile-web-app-title" content="{name}">', 1)
    if v:
        text = text.replace('href="/icon-32.png"', f'href="/app-ikona/32.png?v={v}"', 1)
        text = text.replace('href="/icon-256.png"', f'href="/app-ikona/192.png?v={v}"', 1)
    return text.encode("utf-8")


def kratky(name, limit=15):
    """Krátký název pod ikonu na ploše — po celých slovech, ne uprostřed."""
    if len(name) <= limit:
        return name
    out = ""
    for word in name.split():
        if len((out + " " + word).strip()) > limit:
            break
        out = (out + " " + word).strip()
    return out or name[:limit]


def manifest(name, icon_base, version=0):
    """Manifest PWA s vlastním názvem; `icon_base` = kam ikony (bez velikosti)."""
    with open(os.path.join(STATIC, "manifest.webmanifest"), encoding="utf-8") as fh:
        data = json.load(fh)
    data["name"] = name
    data["short_name"] = kratky(name)
    q = f"?v={version}" if version else ""
    data["icons"] = [
        {"src": f"{icon_base}192.png{q}", "sizes": "192x192", "type": "image/png"},
        {"src": f"{icon_base}512.png{q}", "sizes": "512x512", "type": "image/png",
         "purpose": "any"},
        {"src": f"{icon_base}512.png{q}", "sizes": "512x512", "type": "image/png",
         "purpose": "maskable"},
    ]
    return data


# ── počítač: spouštěč a okno ─────────────────────────────────────────────────
# Třída okna, kterou Electron appka i okno z window.py hlásí systému; podle ní
# dock spáruje okno se spouštěčem a vezme z něj ikonu.
WM_CLASS = "aladin"


def sync_desktop():
    """Linux: položka v nabídce aplikací (claude-code-hub.desktop) s vlastním
    názvem a ikonou. Jiné systémy si ikonu okna berou ze stránky."""
    if core.IS_WINDOWS or core.IS_MAC or core.CONFIG.get("gateway_user"):
        return
    path = os.path.expanduser("~/.local/share/applications/claude-code-hub.desktop")
    try:
        with open(path, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return
    icon = ikona(512) or core.ICON_PATH
    out, changed = [], False
    for line in lines:
        new = line
        if line.startswith("Name="):
            new = "Name=" + zobrazeny()
        elif line.startswith("Icon=") and icon:
            new = "Icon=" + icon
        elif line.startswith("StartupWMClass="):
            new = "StartupWMClass=" + WM_CLASS
        changed |= new != line
        out.append(new)
    if changed:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write("\n".join(out) + "\n")
        os.replace(tmp, path)
