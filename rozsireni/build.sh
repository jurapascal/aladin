#!/usr/bin/env bash
# Builds the Aladin browser extension for every browser into rozsireni/dist/.
#   aladin-chrome-edge.zip  Chrome Web Store, Edge Add-ons (side panel)
#   aladin-opera.zip        Opera add-ons (sidebar)
#   aladin-firefox.zip      AMO (sidebar) — signing on addons.mozilla.org
#   safari/                 for `xcrun safari-web-extension-converter` on a Mac
set -euo pipefail
cd "$(dirname "$0")"
VERZE="${1:-$(python3 -c 'import json;print(json.load(open("manifesty/spolecne.json"))["version"])')}"
rm -rf dist && mkdir -p dist
for cil in chromium opera firefox safari; do
  out="dist/$cil"
  mkdir -p "$out"
  cp -r src/. "$out/"
  python3 - "$cil" "$VERZE" "$out/manifest.json" <<'PY'
import json, sys
cil, verze, cesta = sys.argv[1:]
m = json.load(open("manifesty/spolecne.json"))
m.update(json.load(open(f"manifesty/{cil}.json")))
m["version"] = verze
json.dump(m, open(cesta, "w"), ensure_ascii=False, indent=2)
PY
done
(cd dist/chromium && zip -qr ../aladin-chrome-edge.zip .)
(cd dist/opera && zip -qr ../aladin-opera.zip .)
(cd dist/firefox && zip -qr ../aladin-firefox.zip .)
mv dist/safari dist/safari-zdroj
rm -rf dist/chromium-tmp
echo "Hotovo: $(ls dist)"
