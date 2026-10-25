#!/usr/bin/env python3
"""Postaví ikony pro všechny platformy z jediného zdroje: assets/hub-mark.svg.

Aby ikona nebyla na několika místech ručně: tenhle skript je jediná cesta, jak
`assets/claude-code.png`, `assets/claude-code.ico` a favicony v `hub/static/`
vzniknou. Po úpravě značky ho pusť znovu.

Značka se rasterizuje přes cairosvg. Když není po ruce, vezme se hotové
`assets/claude-code.png` (je to bit po bitu tentýž render, jen uložený).

Windows je tu ten náročný. Formát ICO umí obrázek uložit dvěma způsoby — jako BMP
(DIB) nebo jako PNG — a shell na malých velikostech spolehlivě kreslí jen BMP;
PNG se v ICO smí použít až na 256×256, kde by DIB zbytečně nafoukl soubor.
Tenhle skript to tak i píše a na konci ověří, že to tak doopravdy dopadlo.

Použití:  python3 tools/make-icons.py
"""
import os
import struct
import sys

try:
    from PIL import Image
except ImportError:
    sys.exit("Chybí Pillow: pip install --user Pillow")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SVG = os.path.join(ROOT, "assets", "hub-mark.svg")
PNG = os.path.join(ROOT, "assets", "claude-code.png")     # ikona okna na Linuxu
ICO = os.path.join(ROOT, "assets", "claude-code.ico")     # zástupci na Windows
STATIC = os.path.join(ROOT, "hub", "static")
MASTER = 1024

# 16–64 kreslí nabídka Start, panel a Alt+Tab; 128/256 jde do dlaždic a náhledů.
BMP_SIZES = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64)]
PNG_SIZES = [(128, 128), (256, 256)]


def _frames(src):
    """Zvlášť zmenšený obrázek pro každou velikost — LANCZOS, ne co zbyde."""
    out = {}
    for size in BMP_SIZES + PNG_SIZES:
        out[size] = src.resize(size, Image.Resampling.LANCZOS)
    return out


def _ico_entry(im, as_png):
    """Jedna položka ICO: (hlavička bez offsetu, data)."""
    import io
    buf = io.BytesIO()
    if as_png:
        im.save(buf, format="PNG", optimize=True)
    else:
        # BMP v ICO: hlavička s dvojnásobnou výškou (barvy + AND maska),
        # žádná souborová hlavička BMP a maska se dopisuje ručně.
        w, h = im.size
        px = im.load()
        header = struct.pack("<IiiHHIIiiII", 40, w, h * 2, 1, 32, 0, 0, 0, 0, 0, 0)
        rows = []
        for y in range(h - 1, -1, -1):          # BMP jde zdola nahoru
            row = bytearray()
            for x in range(w):
                r, g, b, a = px[x, y]
                row += bytes((b, g, r, a))      # BGRA
            rows.append(bytes(row))
        # AND maska: dnes ji kreslí alfa, ale hlavička ji pořád čeká.
        mask_stride = ((w + 31) // 32) * 4
        mask = b"\x00" * (mask_stride * h)
        buf.write(header + b"".join(rows) + mask)
    data = buf.getvalue()
    w, h = im.size
    entry = struct.pack("<BBBBHHI", w % 256, h % 256, 0, 0, 1, 32, len(data))
    return entry, data


def write_ico(path, frames):
    order = BMP_SIZES + PNG_SIZES
    entries, blobs = [], []
    for size in order:
        entries.append(_ico_entry(frames[size], as_png=size in PNG_SIZES))
    offset = 6 + 16 * len(order)
    out = bytearray(struct.pack("<HHH", 0, 1, len(order)))
    for entry, data in entries:
        out += entry + struct.pack("<I", offset)
        offset += len(data)
        blobs.append(data)
    out += b"".join(blobs)
    with open(path, "wb") as fh:
        fh.write(bytes(out))


def verify_ico(path):
    """Přečíst zpátky, co jsme napsali — hlavně čím je která velikost uložená."""
    with open(path, "rb") as fh:
        raw = fh.read()
    _, _, count = struct.unpack_from("<HHH", raw, 0)
    seen = []
    for i in range(count):
        w, h, _, _, _, _, size, off = struct.unpack_from("<BBBBHHII", raw, 6 + 16 * i)
        w, h = w or 256, h or 256
        kind = "PNG" if raw[off:off + 8] == b"\x89PNG\r\n\x1a\n" else "BMP"
        seen.append((w, h, kind, size))
    return seen


def _master():
    """Předloha 1024×1024 = assets/claude-code.png (ikona Aladina: černý džin na bílém).

    Dřív se kreslila z assets/hub-mark.svg, ale ikona je teď hotový obrázek
    a SVG by ji přepsalo starou značkou. Zdrojem je tedy PNG; chceš-li ikonu
    změnit, nahraď ho a pusť skript znovu."""
    if not os.path.isfile(PNG):
        sys.exit("Chybí " + PNG)
    return Image.open(PNG).convert("RGBA"), False


# Pozadí maskovatelné ikony. Android si z ní vyřízne kolečko nebo čtvereček
# podle launcheru, takže značka musí sedět v prostředních 80 % a zbytek být
# plocha — průhlednost by se na ploše projevila jako díra.
MASKABLE_BG = (255, 255, 255, 255)      # bílé pozadí ikony Aladina


def write_pwa_icons(src):
    """Ikony pro manifest: dvě běžné a jedna maskovatelná."""
    for size in (192, 512):
        src.resize((size, size), Image.LANCZOS).save(
            os.path.join(STATIC, "icon-%d.png" % size),
            format="PNG", optimize=True)

    canvas = Image.new("RGBA", (512, 512), MASKABLE_BG)
    inner = int(512 * 0.70)                       # značka do bezpečné zóny
    mark = src.resize((inner, inner), Image.LANCZOS)
    canvas.paste(mark, ((512 - inner) // 2,) * 2, mark)
    canvas.save(os.path.join(STATIC, "icon-512-maskable.png"),
                format="PNG", optimize=True)


# Ikony aplikace pro Android. Legacy ikona je značka na celý čtverec, pro
# adaptivní se kreslí zvlášť popředí (značka v bezpečné zóně) a pozadí (barva).
ANDROID_RES = os.path.join(ROOT, "mobile", "android", "app", "src", "main", "res")
ANDROID_DPI = {"mdpi": 1, "hdpi": 1.5, "xhdpi": 2, "xxhdpi": 3, "xxxhdpi": 4}


def write_android_icons(src):
    """Spouštěče pro APK. Když složka s aplikací není, tiše se přeskočí."""
    if not os.path.isdir(ANDROID_RES):
        return False
    for name, scale in ANDROID_DPI.items():
        folder = os.path.join(ANDROID_RES, "mipmap-" + name)
        os.makedirs(folder, exist_ok=True)
        legacy = int(48 * scale)
        src.resize((legacy, legacy), Image.LANCZOS).save(
            os.path.join(folder, "ic_launcher.png"), format="PNG", optimize=True)

        # Popředí adaptivní ikony je 108 dp, ale vidět je jen prostředních 72 —
        # zbytek si systém ořízne podle tvaru, který má launcher rád.
        full = int(108 * scale)
        inner = int(full * 0.58)
        canvas = Image.new("RGBA", (full, full), (0, 0, 0, 0))
        mark = src.resize((inner, inner), Image.LANCZOS)
        canvas.paste(mark, ((full - inner) // 2,) * 2, mark)
        canvas.save(os.path.join(folder, "ic_launcher_foreground.png"),
                    format="PNG", optimize=True)

    anydpi = os.path.join(ANDROID_RES, "mipmap-anydpi-v26")
    os.makedirs(anydpi, exist_ok=True)
    with open(os.path.join(anydpi, "ic_launcher.xml"), "w", encoding="utf-8") as fh:
        fh.write('<?xml version="1.0" encoding="utf-8"?>\n'
                 '<adaptive-icon xmlns:android='
                 '"http://schemas.android.com/apk/res/android">\n'
                 '    <background android:drawable="@color/ikona_bg"/>\n'
                 '    <foreground android:drawable="@mipmap/ic_launcher_foreground"/>\n'
                 '</adaptive-icon>\n')
    return True


def main():
    src, from_svg = _master()
    if src.size[0] != src.size[1]:
        sys.exit("Předloha musí být čtverec, je %dx%d" % src.size)
    if from_svg:
        src.save(PNG, format="PNG", optimize=True)
    frames = _frames(src)

    write_ico(ICO, frames)
    write_ico(os.path.join(STATIC, "favicon.ico"), frames)
    # Prohlížeč (a tedy okno v --app režimu) sáhne po největším, co dostane.
    frames[(256, 256)].save(os.path.join(STATIC, "icon-256.png"),
                            format="PNG", optimize=True)
    frames[(32, 32)].save(os.path.join(STATIC, "icon-32.png"),
                          format="PNG", optimize=True)
    write_pwa_icons(src)
    android = write_android_icons(src)

    for w, h, kind, size in verify_ico(ICO):
        want = "PNG" if (w, h) in PNG_SIZES else "BMP"
        flag = "ok" if kind == want else "ŠPATNĚ (čekal " + want + ")"
        print("  %3dx%-3d  %s  %6d B  %s" % (w, h, kind, size, flag))
    print("hotovo: assets/claude-code.{png,ico}"
          " + hub/static/{favicon.ico,icon-32.png,icon-256.png,"
          "icon-192.png,icon-512.png,icon-512-maskable.png}"
          + (" + mobile/android/.../mipmap-*" if android else ""))


if __name__ == "__main__":
    main()
