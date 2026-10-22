#!/bin/bash
# Aladin — jednořádková instalace.
#
#     curl -fsSL https://raw.githubusercontent.com/jurapascal/claude-code-hub/main/get.sh | bash
#
# Stáhne repo do ~/.claude/hub-src a spustí install.sh. Nic jiného nedělá —
# všechna rozhodnutí (co doinstalovat, kde máš projekty, paměť) padají tam.
#
# Proměnné pro nestandardní případy:
#     HUB_REPO=owner/repo   HUB_BRANCH=main   HUB_DEST=~/jinam   HUB_YES=1
set -eu

REPO="${HUB_REPO:-jurapascal/claude-code-hub}"
BRANCH="${HUB_BRANCH:-main}"
DEST="${HUB_DEST:-$HOME/.claude/hub-src}"

A="\033[38;5;208m"; G="\033[38;5;114m"; Y="\033[38;5;180m"; D="\033[2m"; R="\033[0m"
ok()   { echo -e "  ${G}✓${R} $1"; }
info() { echo -e "  ${A}▸${R} $1"; }
die()  { echo -e "  ${Y}⚠${R} $1" >&2; exit 1; }

echo ""
echo -e "  ${A}✦${R} Aladin"
echo -e "  ${D}────────────────────────────────────${R}"

command -v python3 >/dev/null 2>&1 || die "Chybí python3.
     Debian/Ubuntu/Zorin: sudo apt install python3
     Fedora:              sudo dnf install python3
     macOS:               brew install python"

# Git když je (jde pak updatovat přes git pull), jinak stačí tarball.
if command -v git >/dev/null 2>&1; then
    if [ -d "$DEST/.git" ]; then
        info "aktualizuju $DEST"
        git -C "$DEST" fetch --quiet origin "$BRANCH" \
            && git -C "$DEST" reset --quiet --hard "origin/$BRANCH" \
            || die "update nevyšel — smaž $DEST a spusť to znovu"
    else
        info "stahuju $REPO"
        rm -rf "$DEST"
        mkdir -p "$(dirname "$DEST")"
        git clone --quiet --depth 1 --branch "$BRANCH" \
            "https://github.com/$REPO.git" "$DEST" \
            || die "klonování nevyšlo — je repo veřejné a máš internet?"
    fi
else
    info "stahuju $REPO (bez gitu, přes tarball)"
    TMP="$(mktemp -d)"
    trap 'rm -rf "$TMP"' EXIT
    URL="https://codeload.github.com/$REPO/tar.gz/refs/heads/$BRANCH"
    if command -v curl >/dev/null 2>&1; then
        curl -fsSL "$URL" -o "$TMP/hub.tgz" || die "stahování nevyšlo: $URL"
    elif command -v wget >/dev/null 2>&1; then
        wget -qO "$TMP/hub.tgz" "$URL" || die "stahování nevyšlo: $URL"
    else
        die "chybí curl i wget"
    fi
    command -v tar >/dev/null 2>&1 || die "chybí tar"
    rm -rf "$DEST"
    mkdir -p "$DEST"
    tar -xzf "$TMP/hub.tgz" -C "$DEST" --strip-components=1 \
        || die "rozbalení nevyšlo"
fi
ok "zdroj v $DEST"

[ -f "$DEST/install.sh" ] || die "v $DEST není install.sh — něco se stáhlo špatně"

# Spuštěné přes `curl | bash` je na stdin tenhle skript, ne klávesnice, takže
# by se instalačka neměla koho zeptat. /dev/tty je pořád terminál uživatele.
ARGS=""
[ -n "${HUB_YES:-}" ] && ARGS="--yes"
echo ""
if [ -e /dev/tty ] && [ -z "${HUB_YES:-}" ]; then
    bash "$DEST/install.sh" $ARGS < /dev/tty
else
    bash "$DEST/install.sh" ${ARGS:---yes}
fi
