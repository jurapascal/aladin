#!/bin/bash
# Balíček k rozeslání: celý hub z posledního commitu + návod pro příjemce.
# Kdo dostane ZIP, nepotřebuje git ani účet na GitHubu — rozbalí a spustí instalačku.
#
#     bash make-zip.sh [cílová složka]     (výchozí: ~/Desktop)
set -eu

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT_DIR="${1:-$HOME/Desktop}"
ZIP="$OUT_DIR/claude-code-hub-$(date +%Y-%m-%d).zip"

cd "$SRC"
if [ -n "$(git status --porcelain)" ]; then
    echo "  ⚠ pozor: máš nezacommitované změny — do balíku jdou jen commitnuté soubory"
fi

mkdir -p "$OUT_DIR"
rm -f "$ZIP"
git archive --format=zip --prefix=claude-code-hub/ -o "$ZIP" HEAD

# Návod pro příjemce se do repa neplete, žije jen v balíku. CRLF kvůli Poznámkovému bloku.
TMP="$(mktemp -d)"
mkdir -p "$TMP/claude-code-hub"
cat > "$TMP/claude-code-hub/ZACNI-TADY.txt" <<'EOF'
Aladin — instalace bez GitHubu
=======================================

Rozbal tenhle ZIP kamkoli (třeba na plochu) a pusť instalačku.
Nic z toho nepotřebuje práva správce ani účet na GitHubu.


WINDOWS
-------
1. Rozbal ZIP  ->  vznikne složka claude-code-hub
2. Otevři v ní PowerShell (Shift + pravý klik do složky -> "Otevřít okno
   PowerShellu zde"), nebo napiš:

       cd "$env:USERPROFILE\Desktop\claude-code-hub"

3. Spusť:

       powershell -ExecutionPolicy Bypass -File install.ps1

4. Instalačka se cestou zeptá, co doinstalovat (Python, Git for Windows,
   Claude Code, Obsidian, GitHub CLI - všechno přes winget). Klidně odpovídej
   "a" u všeho, co chceš mít.
5. Na konci nabídne přihlášení do Claude Code: projdi /login a napiš /exit.
6. Spusť "Aladin" z nabídky Start.

Pozor: když si necháš něco doinstalovat, ZAVŘI POTOM POWERSHELL a spusť
install.ps1 ještě jednou. Podruhé už instalačka nové programy uvidí všechny
a doplní, co napoprvé přeskočila (třeba Playwright MCP).


LINUX / macOS
-------------
       cd ~/Desktop/claude-code-hub
       bash install.sh


KDYŽ NĚCO NEHRAJE
-----------------
Instalačka na konci sama vypíše "kontrola prostředí" - co na stroji je
a co chybí. Kdykoli později to samé:

       python "$env:USERPROFILE\.claude\claude-hub.py" --doctor      (Windows)
       python3 ~/.claude/claude-hub.py --doctor                      (Linux/macOS)

Kdyby okno hubu zůstalo prázdné, důvod je v logu:

       %USERPROFILE%\.claude\hub.log      (Windows)
       ~/.claude/hub.log                  (Linux/macOS)

Ten log a výpis z --doctor pošli zpátky, dá se z nich poznat úplně všechno.


CO SE KAM NAINSTALUJE
---------------------
Všechno do ~/.claude (na Windows %USERPROFILE%\.claude): appka hubu, slash
příkazy a zástupce. Existující soubory se zálohují (*.backup-<datum>),
do settings.json instalačka nesahá.

Žádná data, projekty ani hesla v tomhle balíku nejsou - je to jen instalačka.
EOF

if command -v unix2dos >/dev/null 2>&1; then
    unix2dos -q "$TMP/claude-code-hub/ZACNI-TADY.txt"
else
    python3 -c "
import io, sys
p = sys.argv[1]
text = io.open(p, encoding='utf-8', newline='').read().replace('\r\n', '\n').replace('\n', '\r\n')
io.open(p, 'w', encoding='utf-8', newline='').write(text)" "$TMP/claude-code-hub/ZACNI-TADY.txt"
fi

(cd "$TMP" && zip -q "$ZIP" claude-code-hub/ZACNI-TADY.txt)
rm -rf "$TMP"

echo "  ✓ balík: $ZIP  ($(du -h "$ZIP" | cut -f1))"
echo "    obsahuje commit $(git rev-parse --short HEAD)"
