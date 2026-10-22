#!/bin/bash
# AppImage pro Linux: jeden soubor, stáhnout, povolit spuštění a dvojklik.
#
#     bash packaging/linux/build.sh [výstupní složka]
#
# Uvnitř je přenosný Python, zdroj hubu a spouštěč (packaging/stage.py).
# AppRun jen pustí spouštěč; ten si na Linuxu radši vezme systémový python3
# (umí nativní okno přes WebKitGTK) a přibalený jen tam, kde žádný není.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
OUT="${1:-$ROOT/dist}"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
APPDIR="$WORK/ClaudeCodeHub.AppDir"
ARCH="${ARCH:-x86_64}"

python3 "$ROOT/packaging/stage.py" "$APPDIR" "$ARCH-unknown-linux-gnu"

cat > "$APPDIR/AppRun" <<'RUN'
#!/bin/sh
HERE="$(dirname "$(readlink -f "$0")")"
exec "$HERE/python/bin/python3" "$HERE/launcher.py" "$@"
RUN
chmod +x "$APPDIR/AppRun"

cp "$ROOT/assets/claude-code.png" "$APPDIR/claude-code-hub.png"
cat > "$APPDIR/claude-code-hub.desktop" <<'DESK'
[Desktop Entry]
Type=Application
Name=Aladin
Comment=Aladin — projekty a paměť v jednom okně s taby
Icon=claude-code-hub
Exec=AppRun
Terminal=false
Categories=Development;Utility;
DESK

TOOL="$WORK/appimagetool"
curl -fsSL -o "$TOOL" \
    "https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-$ARCH.AppImage"
chmod +x "$TOOL"

mkdir -p "$OUT"
# Na CI není FUSE — nástroj se rozbalí a pustí bez připojení.
APPIMAGE_EXTRACT_AND_RUN=1 ARCH="$ARCH" "$TOOL" --no-appstream "$APPDIR" \
    "$OUT/Claude-Code-Hub-$ARCH.AppImage"
echo "hotovo: $OUT/Claude-Code-Hub-$ARCH.AppImage"
