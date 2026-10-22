#!/bin/bash
# .dmg pro macOS: otevřít, přetáhnout do Aplikací, spustit.
#
#     bash packaging/mac/build.sh [výstupní složka]      (jen na macOS)
#
# Uvnitř appky je přenosný Python, zdroj hubu a spouštěč (packaging/stage.py).
# Architekturu bere podle stroje, na kterém běží (arm64 = Apple Silicon),
# nebo z MAC_ARCH.
# Appka není podepsaná certifikátem Applu — jen ad-hoc podpisem, bez kterého
# by na Apple Silicon nešla spustit vůbec. Poprvé ji proto macOS zastaví
# a pustí ji až Nastavení → Soukromí a zabezpečení → Přesto otevřít.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
OUT="${1:-$ROOT/dist}"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# MAC_ARCH=x86_64 postaví verzi pro Intel i na Apple Silicon (Python je jen
# soubory, nic se nepřekládá).
case "${MAC_ARCH:-$(uname -m)}" in
    arm64)  TRIPLE=aarch64-apple-darwin; LABEL=apple-silicon ;;
    x86_64) TRIPLE=x86_64-apple-darwin;  LABEL=intel ;;
    *) echo "neznámá architektura ${MAC_ARCH:-$(uname -m)}"; exit 1 ;;
esac

APP="$WORK/Aladin.app"
RES="$APP/Contents/Resources"
mkdir -p "$APP/Contents/MacOS"
python3 "$ROOT/packaging/stage.py" "$RES" "$TRIPLE"
VERSION="$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$RES/hub/hub/__init__.py")"

cat > "$APP/Contents/MacOS/claude-code-hub" <<'RUN'
#!/bin/bash
RES="$(cd "$(dirname "$0")/../Resources" && pwd)"
exec "$RES/python/bin/python3" "$RES/launcher.py" "$@"
RUN
chmod +x "$APP/Contents/MacOS/claude-code-hub"

# Ikona: .icns z PNG přes iconutil.
ICONSET="$WORK/AppIcon.iconset"
mkdir -p "$ICONSET"
for size in 16 32 128 256 512; do
    sips -z $size $size "$ROOT/assets/claude-code.png" --out "$ICONSET/icon_${size}x${size}.png" >/dev/null
    double=$((size * 2))
    sips -z $double $double "$ROOT/assets/claude-code.png" --out "$ICONSET/icon_${size}x${size}@2x.png" >/dev/null
done
iconutil -c icns "$ICONSET" -o "$RES/AppIcon.icns"

cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key><string>Aladin</string>
    <key>CFBundleDisplayName</key><string>Aladin</string>
    <key>CFBundleIdentifier</key><string>com.github.jurapascal.claude-code-hub</string>
    <key>CFBundleVersion</key><string>$VERSION</string>
    <key>CFBundleShortVersionString</key><string>$VERSION</string>
    <key>CFBundlePackageType</key><string>APPL</string>
    <key>CFBundleExecutable</key><string>claude-code-hub</string>
    <key>CFBundleIconFile</key><string>AppIcon</string>
    <key>LSMinimumSystemVersion</key><string>11.0</string>
    <key>NSHighResolutionCapable</key><true/>
    <key>NSMicrophoneUsageDescription</key><string>Diktování zpráv do Claude Code.</string>
</dict>
</plist>
PLIST

codesign --force --deep --sign - "$APP"

DMGDIR="$WORK/dmg"
mkdir -p "$DMGDIR" "$OUT"
cp -R "$APP" "$DMGDIR/"
ln -s /Applications "$DMGDIR/Applications"
hdiutil create -volname "Aladin" -srcfolder "$DMGDIR" -ov -format UDZO \
    "$OUT/Claude-Code-Hub-mac-$LABEL.dmg"
echo "hotovo: $OUT/Claude-Code-Hub-mac-$LABEL.dmg"
