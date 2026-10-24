#!/bin/bash
# Aladin — instalačka pro Linux a macOS.
#
# Repo je jen instalačka: obsah (projekty, paměť) má každý svůj na disku.
# Skript zjistí, kde ho má, uloží to do ~/.claude/hub-config.json a podle
# toho vyrenderuje aplikaci i slash příkazy. Nic nepřepíše bez zálohy
# a do ~/.claude/settings.json nesahá.
#
# Ve výchozím režimu doinstaluje všechno, co k hubu patří (Obsidian, GitHub CLI,
# Playwright MCP, hooky) a ptá se jen na to, co uhádnout nejde: kde máš paměť,
# jestli chceš bypass režim a jestli se má hned přihlásit.
#
# Windows má vlastní install.ps1.
#
# Použití: bash install.sh [--yes] [--minimal] [--update]
#     --yes      bez otázek — doinstaluje, co jde, přihlášení a bypass přeskočí
#     --minimal  jen hub: nic nedoinstalovává, do settings.json nesahá
#     --update   aktualizace z tlačítka v hubu: jen soubory appky, příkazy
#                a hooky. Skilly, Obsidian, gh, Playwright ani kontrola se
#                znovu neřeší — ty už nainstalované jsou.
set -u

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLAUDE_DIR="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
ICON_DIR="$HOME/.local/share/icons"
APP_DIR="$HOME/.local/share/applications"
STAMP="$(date +%Y%m%d-%H%M%S)"
CLONED_VAULT=""
CONFIG="$CLAUDE_DIR/hub-config.json"

ASSUME_YES=false
MINIMAL=false
APP=false
UPDATE=false
for arg in "$@"; do
    case "$arg" in
        --yes) ASSUME_YES=true ;;
        --minimal) MINIMAL=true ;;
        # Aktualizace z hubu (core.update_hub): stroj je nastavený, jde jen o
        # nové soubory. Dřív se pouštěla celá instalace včetně stahování
        # skillů a Playwrightu a trvala zbytečně dlouho.
        --update) ASSUME_YES=true; UPDATE=true ;;
        # Instalačka ke stažení (AppImage, .dmg) pouští tohle z okna hubu
        # (hub/setup.py): bez otázek a navíc doinstaluje Claude Code.
        --app) ASSUME_YES=true; APP=true ;;
    esac
done

# Python, na který mají mířit hooky, slash příkazy a položka v nabídce.
# Instalačka ke stažení nese vlastní (HUB_PYTHON) — na Macu jiný být nemusí.
PY="${HUB_PYTHON:-python3}"
command -v "$PY" >/dev/null 2>&1 || [ -x "$PY" ] || PY=python3
# Přes `curl … | bash` je na stdin skript, ne klávesnice. Terminál uživatele
# je pořád na /dev/tty, takže se dá ptát dál.
# Zkouška v subshellu: existence /dev/tty nestačí (bez řídicího terminálu se
# otevřít nedá) a chybu neúspěšného `exec <` už nejde umlčet po faktu.
if [ ! -t 0 ] && (exec < /dev/tty) 2>/dev/null; then exec < /dev/tty; fi
[ -t 0 ] || ASSUME_YES=true   # opravdu bez terminálu se neptáme

A="\033[38;5;208m"; G="\033[38;5;114m"; Y="\033[38;5;180m"; D="\033[2m"; R="\033[0m"
ok()   { echo -e "  ${G}✓${R} $1"; }
info() { echo -e "  ${A}▸${R} $1"; }
warn() { echo -e "  ${Y}⚠${R} $1"; }

echo ""
if $UPDATE; then
    echo -e "  ${A}✦${R} Aladin — aktualizace"
else
    echo -e "  ${A}✦${R} Aladin — instalace"
fi
echo -e "  ${D}────────────────────────────────────${R}"

# ── 1. Závislosti ────────────────────────────────────────────────────────────
# Hub je web appka v lokálním okně: stačí Python 3 ze standardní knihovny.
# GTK ani VTE už potřeba nejsou.
if ! command -v "$PY" >/dev/null 2>&1 && [ ! -x "$PY" ]; then
    warn "Chybí python3"
    echo -e "     ${D}Debian/Ubuntu/Zorin: sudo apt install python3${R}"
    echo -e "     ${D}Fedora:              sudo dnf install python3${R}"
    exit 1
fi
ok "Python 3 ($("$PY" --version 2>&1 | cut -d' ' -f2))"

if ! $UPDATE; then
# Diakritika: tab, který naběhne pod LANG=C, ořeže každý znak s háčkem. Hub si
# UTF-8 locale dopočítá sám (hub/core.py), tady jde jen o to říct, když ho
# systém nemá vůbec z čeho vzít.
case "${LC_ALL:-${LANG:-}}" in
    *UTF-8*|*utf8*|*UTF8*|*utf-8*) ok "locale: ${LC_ALL:-$LANG}" ;;
    *)
        if locale -a 2>/dev/null | grep -qiE '(^|\.)(c|C)\.utf-?8$|utf-?8$'; then
            info "locale je ${LC_ALL:-${LANG:-nenastavené}} — hub si pro taby vynutí UTF-8 sám"
        else
            warn "na stroji není žádné UTF-8 locale — čeština se bude v terminálu lámat"
            echo -e "     ${D}Debian/Ubuntu: sudo locale-gen cs_CZ.UTF-8 && sudo update-locale${R}"
        fi
        ;;
esac

# Okno: chromium v --app režimu, nebo WebKitGTK, jinak zůstane obyčejná záložka.
WINDOW_HOST=""
for b in google-chrome google-chrome-stable chromium chromium-browser \
         brave-browser microsoft-edge vivaldi; do
    command -v "$b" >/dev/null 2>&1 && { WINDOW_HOST="$b"; break; }
done
if [ -n "$WINDOW_HOST" ]; then
    ok "okno: $WINDOW_HOST (--app režim)"
elif python3 -c "
import gi
for gtk, wk, name in (('4.0','6.0','WebKit'), ('3.0','4.1','WebKit2')):
    try:
        gi.require_version('Gtk', gtk); gi.require_version(name, wk)
        __import__('gi.repository', fromlist=['Gtk', name]); raise SystemExit(0)
    except SystemExit: raise
    except Exception: pass
raise SystemExit(1)" 2>/dev/null; then
    ok "okno: WebKitGTK (bez prohlížeče)"
else
    warn "žádný chromium ani WebKitGTK — hub se otevře jako záložka ve výchozím prohlížeči"
    echo -e "     ${D}Nativní okno: sudo apt install gir1.2-webkit2-4.1  (nebo chromium)${R}"
fi

if ! command -v claude >/dev/null 2>&1 && $APP; then
    # Oficiální instalačka Claude Code jde do ~/.local/bin, bez sudo.
    info "instaluju Claude Code…"
    if curl -fsSL https://claude.ai/install.sh | bash >/dev/null 2>&1; then
        export PATH="$HOME/.local/bin:$PATH"
    fi
fi
if command -v claude >/dev/null 2>&1; then
    ok "Claude Code CLI ($(command -v claude))"
else
    warn "Claude Code CLI ('claude') není v PATH — Hub se spustí, ale taby zůstanou v shellu."
    echo -e "     ${D}curl -fsSL https://claude.ai/install.sh | bash${R}"
fi

# Node.js: bez něj nejede prohlížeč pro Clauda (Playwright MCP) ani další MCP
# servery přes npx. Na Linuxu se bere oficiální balík do ~/.local — bez sudo,
# takže to zvládne i instalačka ke stažení, která se na heslo zeptat neumí.
install_node_user() {
    local arch base file tmp
    case "$(uname -m)" in
        x86_64|amd64) arch=x64 ;;
        aarch64|arm64) arch=arm64 ;;
        *) return 1 ;;
    esac
    base="https://nodejs.org/dist/latest-v22.x"
    file="$(curl -fsSL "$base/SHASUMS256.txt" 2>/dev/null | grep -o "node-v[0-9.]*-linux-$arch\.tar\.xz" | head -1)"
    [ -n "$file" ] || return 1
    tmp="$(mktemp -d)"
    curl -fsSL "$base/$file" -o "$tmp/$file" || { rm -rf "$tmp"; return 1; }
    (cd "$tmp" && curl -fsSL "$base/SHASUMS256.txt" | grep " $file\$" | sha256sum -c - >/dev/null 2>&1) \
        || { rm -rf "$tmp"; return 1; }
    rm -rf "${HOME:?}/.local/lib/nodejs"
    mkdir -p "$HOME/.local/lib/nodejs" "$HOME/.local/bin"
    tar -xJf "$tmp/$file" -C "$HOME/.local/lib/nodejs" --strip-components=1 || { rm -rf "$tmp"; return 1; }
    rm -rf "$tmp"
    for b in node npm npx corepack; do ln -sf "$HOME/.local/lib/nodejs/bin/$b" "$HOME/.local/bin/$b"; done
    export PATH="$HOME/.local/bin:$PATH"
}
NODE_MAJOR="$(node -v 2>/dev/null | sed 's/^v//; s/\..*//')"
if [ -n "$NODE_MAJOR" ] && [ "$NODE_MAJOR" -ge 20 ]; then
    ok "Node.js $(node -v)"
elif $MINIMAL; then
    info "--minimal: Node.js nedoinstalovávám"
else
    info "doinstalovávám Node.js 22 ${D}(prohlížeč pro Clauda, MCP servery)${R}"
    if [ "$(uname)" = "Darwin" ]; then
        command -v brew >/dev/null 2>&1 && brew install node >/dev/null 2>&1
    else
        install_node_user
    fi
    NODE_MAJOR="$(node -v 2>/dev/null | sed 's/^v//; s/\..*//')"
    if [ -n "$NODE_MAJOR" ] && [ "$NODE_MAJOR" -ge 20 ]; then ok "Node.js $(node -v)"
    else warn "Node.js se nepodařilo nainstalovat — stáhni ho z https://nodejs.org"; fi
fi

# git: skilly do vaultu, klon vaultu, verze projektů v panelu.
if command -v git >/dev/null 2>&1; then
    ok "git"
elif ! $MINIMAL; then
    info "doinstalovávám git"
    if [ "$(uname)" = "Darwin" ]; then
        { command -v brew >/dev/null 2>&1 && brew install git >/dev/null 2>&1; } || xcode-select --install >/dev/null 2>&1
    elif command -v apt-get >/dev/null 2>&1; then sudo -n apt-get install -y git >/dev/null 2>&1 || { [ -t 0 ] && sudo apt-get install -y git; }
    elif command -v dnf >/dev/null 2>&1; then sudo -n dnf install -y git >/dev/null 2>&1 || { [ -t 0 ] && sudo dnf install -y git; }
    elif command -v pacman >/dev/null 2>&1; then sudo -n pacman -S --noconfirm git >/dev/null 2>&1 || { [ -t 0 ] && sudo pacman -S --noconfirm git; }
    fi
    command -v git >/dev/null 2>&1 && ok "git nainstalován" || warn "git se nenainstaloval — doinstaluj ho ručně (apt install git)"
fi
fi   # ! $UPDATE

# ── 2. Obsidian a GitHub CLI ─────────────────────────────────────────────────
# Hub běží i bez obojího, ale bez Obsidianu nemá paměť kde bydlet (/save, /learn,
# /project) a bez `gh` se z čerstvého stroje nedá klonovat ani pushovat. Nikdy
# neinstalujeme potichu: s --yes se jen vypíše, co si má člověk doinstalovat.
ask() {
    $ASSUME_YES && return 1
    local answer
    read -r -p "     $1 [a/N]: " answer
    [[ "$answer" =~ ^[aAyY] ]]
}

have_obsidian() {
    command -v obsidian >/dev/null 2>&1 && return 0
    flatpak info md.obsidian.Obsidian >/dev/null 2>&1 && return 0
    snap list obsidian >/dev/null 2>&1 && return 0
    [ -d /Applications/Obsidian.app ] && return 0
    ls "$HOME"/Applications/Obsidian*.AppImage >/dev/null 2>&1 && return 0
    return 1
}

install_obsidian() {
    if [ "$(uname)" = "Darwin" ]; then
        command -v brew >/dev/null 2>&1 && brew install --cask obsidian && return 0
    elif command -v flatpak >/dev/null 2>&1; then
        flatpak remote-add --if-not-exists --user \
            flathub https://flathub.org/repo/flathub.flatpakrepo >/dev/null 2>&1
        flatpak install -y --user flathub md.obsidian.Obsidian && return 0
    elif command -v snap >/dev/null 2>&1; then
        sudo snap install obsidian --classic && return 0
    fi
    return 1
}

install_clipboard() {
    # Na Wayland sedí wl-clipboard, pod X xclip; obojí je malé, tak ať je co je.
    local pkgs="wl-clipboard xclip"
    [ "${XDG_SESSION_TYPE:-}" = "x11" ] && pkgs="xclip wl-clipboard"
    for pkg in $pkgs; do
        if command -v apt-get >/dev/null 2>&1 && apt-get install -s "$pkg" >/dev/null 2>&1; then
            sudo apt-get install -y "$pkg" && return 0
        elif command -v dnf >/dev/null 2>&1; then
            sudo dnf install -y "$pkg" && return 0
        elif command -v pacman >/dev/null 2>&1; then
            sudo pacman -S --noconfirm "$pkg" && return 0
        fi
    done
    return 1
}

pkg_install() {
    # apt / dnf / pacman, bez roota jen když to jde (sudo -n), jinak na terminálu s dotazem.
    local pkg="$1"
    if command -v apt-get >/dev/null 2>&1 && apt-get install -s "$pkg" >/dev/null 2>&1; then
        sudo -n apt-get install -y "$pkg" >/dev/null 2>&1 || { [ -t 0 ] && sudo apt-get install -y "$pkg"; }
    elif command -v dnf >/dev/null 2>&1; then
        sudo -n dnf install -y "$pkg" >/dev/null 2>&1 || { [ -t 0 ] && sudo dnf install -y "$pkg"; }
    elif command -v pacman >/dev/null 2>&1; then
        sudo -n pacman -S --noconfirm "$pkg" >/dev/null 2>&1 || { [ -t 0 ] && sudo pacman -S --noconfirm "$pkg"; }
    else
        return 1
    fi
}

# Claude ze serveru smí (podle volby v appce) vidět obrazovku a schránku. Na Waylandu
# na to X nástroje nestačí: wl-clipboard na schránku a nástroj podle prostředí na snímek.
install_pc_tools() {
    [ "$(uname)" = "Darwin" ] && return 0              # screencapture, pbcopy jsou v systému
    local wayland=0 shot_cmd shot_pkg
    { [ "${XDG_SESSION_TYPE:-}" = "wayland" ] || [ -n "${WAYLAND_DISPLAY:-}" ]; } && wayland=1
    if [ "$wayland" = 1 ] && ! command -v wl-copy >/dev/null 2>&1; then
        info "doinstalovávám wl-clipboard ${D}(schránka pro Clauda ze serveru)${R}"
        pkg_install wl-clipboard >/dev/null 2>&1 && ok "wl-clipboard nainstalován" \
            || warn "nevyšlo — doinstaluj ručně: apt install wl-clipboard"
    fi
    if command -v gnome-screenshot >/dev/null 2>&1 || command -v grim >/dev/null 2>&1 \
            || command -v spectacle >/dev/null 2>&1 || command -v maim >/dev/null 2>&1 \
            || command -v scrot >/dev/null 2>&1; then
        return 0
    fi
    case "${XDG_CURRENT_DESKTOP:-}" in
        *GNOME*|*Unity*|*Zorin*|*Cinnamon*|*Budgie*) shot_cmd=gnome-screenshot; shot_pkg=gnome-screenshot ;;
        *KDE*)                                        shot_cmd=spectacle;        shot_pkg=spectacle ;;
        *) if [ "$wayland" = 1 ]; then shot_cmd=grim; shot_pkg=grim; else shot_cmd=scrot; shot_pkg=scrot; fi ;;
    esac
    info "doinstalovávám $shot_pkg ${D}(snímek obrazovky pro Clauda ze serveru)${R}"
    pkg_install "$shot_pkg" >/dev/null 2>&1 && ok "$shot_pkg nainstalován" \
        || warn "nevyšlo — doinstaluj ručně: apt install $shot_pkg"
}

install_gh() {
    if [ "$(uname)" = "Darwin" ]; then
        command -v brew >/dev/null 2>&1 && brew install gh && return 0
    # `apt-get install -s` je simulace: běží bez roota a nezávisí na jazyku výpisu
    elif command -v apt-get >/dev/null 2>&1 && apt-get install -s gh >/dev/null 2>&1; then
        sudo apt-get install -y gh && return 0
    elif command -v dnf >/dev/null 2>&1; then
        sudo dnf install -y gh && return 0
    elif command -v pacman >/dev/null 2>&1; then
        sudo pacman -S --noconfirm github-cli && return 0
    elif command -v snap >/dev/null 2>&1; then
        sudo snap install gh && return 0
    fi
    return 1
}

clone_vault() {  # clone_vault <owner/repo|URL> <rodičovská složka> → $CLONED_VAULT
    local repo="$1" parent="$2" name target
    name="$(basename "${repo%.git}")"
    target="$parent/$name"
    [ -d "$target" ] && { warn "$target už existuje"; return 1; }
    mkdir -p "$parent"
    # gh umí i privátní repo, na které má přihlášený účet přístup jako kolaborátor
    if command -v gh >/dev/null 2>&1 && gh auth status >/dev/null 2>&1; then
        gh repo clone "$repo" "$target" >/dev/null 2>&1 || return 1
    else
        case "$repo" in
            *://*|git@*) git clone "$repo" "$target" >/dev/null 2>&1 || return 1 ;;
            *) git clone "https://github.com/$repo.git" "$target" >/dev/null 2>&1 || return 1 ;;
        esac
    fi
    CLONED_VAULT="$target"
    # vault z gitu nemusí mít memory/ (třeba když se commitovalo jen skills/)
    [ -d "$target/memory" ] || create_vault "$target"
    return 0
}

create_vault() {  # create_vault <cesta> — prázdný vault, ať má paměť kam psát
    mkdir -p "$1/memory" "$1/skills" "$1/.obsidian"
    if [ ! -f "$1/memory/MEMORY.md" ] && [ -f "$SRC/assets/vault/MEMORY.md" ]; then
        cp "$SRC/assets/vault/MEMORY.md" "$1/memory/MEMORY.md"
    fi
}

detect_vault() {
    # Obsidian vault = složka s .obsidian/ nebo s podsložkou memory/
    for c in "$HOME/Obsidian"/*/ "$HOME/Documents/Obsidian"/*/ "$HOME/obsidian"/*/; do
        [ -d "$c" ] || continue
        if [ -d "${c}.obsidian" ] || [ -d "${c}memory" ]; then
            echo "${c%/}"; return
        fi
    done
    echo ""
}

echo ""
if $UPDATE; then
    :   # aktualizace: programy i vault už stroj má
elif $MINIMAL; then
    info "--minimal: Obsidian ani gh nedoinstalovávám"
else
    # Doinstalovává se bez ptaní: obojí k hubu patří (paměť, push) a otázka
    # navíc byla hlavní důvod, proč instalace nebyla „rychlá".
    if have_obsidian; then
        ok "Obsidian"
    else
        info "doinstalovávám Obsidian ${D}(paměť: /save, /learn, /project)${R}"
        if install_obsidian >/dev/null 2>&1; then ok "Obsidian nainstalován"
        else warn "nevyšlo — stáhni ho z https://obsidian.md/download"; fi
    fi

    # Bez tohohle se v tabu nedá kopírovat: WebKitGTK stránku ke schránce
    # nepustí, takže na ni sahá server hubu — a potřebuje k tomu nástroj.
    if [ "$(uname)" != "Darwin" ] && ! command -v wl-copy >/dev/null 2>&1 \
            && ! command -v xclip >/dev/null 2>&1 && ! command -v xsel >/dev/null 2>&1; then
        info "doinstalovávám nástroj na schránku ${D}(kopírování v tabu)${R}"
        if install_clipboard >/dev/null 2>&1; then ok "schránka připravena"
        else warn "nevyšlo — doinstaluj ručně: apt install wl-clipboard  (nebo xclip)"; fi
    fi

    install_pc_tools

    if command -v gh >/dev/null 2>&1; then
        ok "GitHub CLI ($(command -v gh))"
    else
        info "doinstalovávám GitHub CLI ${D}(klonování a push)${R}"
        if install_gh >/dev/null 2>&1; then ok "gh nainstalován"
        else warn "nevyšlo — návod: https://github.com/cli/cli#installation"; fi
    fi
fi
if $UPDATE; then
    :
elif command -v gh >/dev/null 2>&1 && ! gh auth status >/dev/null 2>&1; then
    info "gh není přihlášený k GitHubu"
    if ask "Přihlásit se teď (otevře prohlížeč)?"; then
        gh auth login || warn "přihlášení nedoběhlo — kdykoli později: gh auth login"
        gh auth setup-git >/dev/null 2>&1 && ok "git umí přes gh i push přes HTTPS"
    else
        echo -e "     ${D}Později: gh auth login${R}"
    fi
elif command -v gh >/dev/null 2>&1; then
    ok "gh přihlášený ($(gh api user --jq .login 2>/dev/null || echo 'ok'))"
fi

# Vault až po gh: cizí stroj si tvůj vault naklonuje z privátního repa, a na to
# musí být `gh` napřed přihlášený.
VAULT_FOUND=""
$UPDATE || VAULT_FOUND="$(detect_vault)"
if $UPDATE; then
    :
elif [ -n "$VAULT_FOUND" ]; then
    ok "vault: $VAULT_FOUND"
elif $APP; then
    info "paměť (Obsidian vault) nastavíš v průvodci hned po instalaci"
elif $ASSUME_YES; then
    info "bez vaultu — paměť zůstane vypnutá (spusť instalačku bez --yes a založíš ho)"
else
    echo ""
    info "Máš paměť (Obsidian vault) v gitu? ${D}owner/repo nebo URL — Enter = nemám${R}"
    read -r -p "     []: " VAULT_REPO
    if [ -n "$VAULT_REPO" ]; then
        if clone_vault "$VAULT_REPO" "$HOME/Obsidian"; then
            ok "vault naklonován: $CLONED_VAULT  ${D}(v Obsidianu: Open folder as vault)${R}"
        else
            warn "klonování nevyšlo — ověř přístup: gh repo view $VAULT_REPO"
        fi
    elif ask "Založit prázdný vault $HOME/Obsidian/Claude-Brain pro paměť?"; then
        create_vault "$HOME/Obsidian/Claude-Brain"
        ok "vault založen: $HOME/Obsidian/Claude-Brain  ${D}(v Obsidianu: Open folder as vault)${R}"
    else
        info "bez vaultu poběží hub taky, jen bez paměti"
    fi
fi

# ── 3. Kde má tenhle počítač co ──────────────────────────────────────────────
detect_project_dirs() {
    local found=""
    for c in "$HOME/Desktop" "$HOME/Plocha" "$HOME/Projects" "$HOME/projects" \
             "$HOME/dev" "$HOME/code" "$HOME/git" "$HOME/src" "$HOME/www" \
             "/opt/lampp/htdocs" "/var/www/html"; do
        [ -d "$c" ] && found="$found${found:+, }$c"
    done
    echo "$found"
}

if [ -f "$CONFIG" ]; then
    ok "konfig už existuje — nechávám ho být ($CONFIG)"
else
    PROJECT_DIRS_CSV="$(detect_project_dirs)"
    VAULT="$(detect_vault)"

    if ! $ASSUME_YES; then
        echo ""
        info "Kde máš projekty? ${D}(čárkou oddělený seznam)${R}"
        read -r -p "     [${PROJECT_DIRS_CSV}]: " ANSWER
        [ -n "$ANSWER" ] && PROJECT_DIRS_CSV="$ANSWER"

        info "Obsidian vault s pamětí? ${D}(Enter = nechat prázdné, paměť se vypne)${R}"
        read -r -p "     [${VAULT}]: " ANSWER
        [ -n "$ANSWER" ] && VAULT="$ANSWER"
        echo ""
    fi

    "$PY" - "$CONFIG" "$PROJECT_DIRS_CSV" "$VAULT" "$ICON_DIR/claude-code.png" \
             "$CLAUDE_DIR/ftp-deploy.sh" <<'PYEOF'
import json, os, sys
cfg_path, dirs_csv, vault, icon, ftp = sys.argv[1:6]
dirs = [d.strip() for d in dirs_csv.split(",") if d.strip()]
cfg = {
    "project_dirs": dirs or ["~/Desktop", "~/Projects"],
    "brain_dir": vault.strip() or "~/Obsidian/Claude-Brain",
    "icon": icon,
    "ftp_deploy_script": ftp,
}
os.makedirs(os.path.dirname(cfg_path), exist_ok=True)
with open(cfg_path, "w", encoding="utf-8") as fh:
    json.dump(cfg, fh, ensure_ascii=False, indent=2)
    fh.write("\n")
PYEOF
    ok "konfig zapsán: $CONFIG"
fi

# Co skutečně stojí v konfigu (i když ho instalačka teď nepsala) — šablony
# skillů to potřebují, aby /newsletter a spol. hledaly projekty na správném místě.
PROJECT_DIRS_LIST="$("$PY" -c "
import json, os, sys
try:
    cfg = json.load(open(sys.argv[1]))
except Exception:
    cfg = {}
dirs = [os.path.expanduser(d) for d in cfg.get('project_dirs') or []]
print(', '.join(d for d in dirs if os.path.isdir(d)))" "$CONFIG" 2>/dev/null)"

VAULT="$("$PY" -c "
import json, os, sys
try:
    cfg = json.load(open(sys.argv[1]))
except Exception:
    cfg = {}
print(os.path.expanduser(cfg.get('brain_dir') or ''))" "$CONFIG" 2>/dev/null)"
MEMORY_DIR="$VAULT/memory"
BRAIN_SKILLS="$VAULT/skills"
HAS_VAULT=false
[ -n "$VAULT" ] && [ -d "$MEMORY_DIR" ] && HAS_VAULT=true

if $HAS_VAULT; then
    ok "paměť: $MEMORY_DIR"
else
    info "bez Obsidian vaultu — paměťové příkazy a panel paměti se přeskočí"
fi

# ── 3b. Skilly do vaultu ─────────────────────────────────────────────────────
# 571 hotových postupů, ze kterých si Claude sám vybírá. Existující složku
# nikdy nepřepisujeme — kdo si tam něco napsal, o to nesmí přijít.
SKILLS_REPO="${HUB_SKILLS_REPO:-jurapascal/paluba-market}"

install_skills() {
    local target="$1"
    if [ -d "$target/.git" ]; then
        git -C "$target" pull --ff-only --quiet 2>/dev/null && return 0
        return 1
    fi
    git clone --quiet --depth 1 "https://github.com/$SKILLS_REPO.git" "$target"
}

if $UPDATE; then
    :   # skilly jsou obsah, ne appka — aktualizace je znovu nestahuje
elif $MINIMAL; then
    info "--minimal: skilly nestahuju"
elif ! $HAS_VAULT; then
    :   # bez vaultu nemají kam
elif [ -d "$BRAIN_SKILLS" ] && [ -n "$(ls -A "$BRAIN_SKILLS" 2>/dev/null)" ]; then
    if [ -d "$BRAIN_SKILLS/.git" ]; then
        info "aktualizuju skilly"
        install_skills "$BRAIN_SKILLS" >/dev/null 2>&1 \
            && ok "skilly aktuální ($(find "$BRAIN_SKILLS" -name SKILL.md | wc -l))" \
            || warn "skilly se nepodařilo aktualizovat"
    else
        ok "skilly už máš ($(find "$BRAIN_SKILLS" -name SKILL.md 2>/dev/null | wc -l)) — nechávám je být"
    fi
elif ! command -v git >/dev/null 2>&1; then
    info "skilly přeskočeny — chybí git"
else
    info "stahuju skilly ${D}(hotové postupy, ze kterých si Claude vybírá)${R}"
    if install_skills "$BRAIN_SKILLS" >/dev/null 2>&1; then
        ok "skilly: $(find "$BRAIN_SKILLS" -name SKILL.md | wc -l) v $BRAIN_SKILLS"
    else
        warn "stažení nevyšlo — ručně: git clone https://github.com/$SKILLS_REPO.git $BRAIN_SKILLS"
    fi
fi

# ── 4. Aplikace do ~/.claude ─────────────────────────────────────────────────
mkdir -p "$CLAUDE_DIR/hooks" "$CLAUDE_DIR/skills" "$ICON_DIR" "$APP_DIR"

copy() {  # copy <zdroj> <cíl> — existující jiný soubor zazálohuje
    local src="$1" dst="$2"
    if [ -f "$dst" ] && ! cmp -s "$src" "$dst"; then
        cp "$dst" "$dst.backup-$STAMP"
        info "záloha: $(basename "$dst").backup-$STAMP"
    fi
    cp "$src" "$dst"
}

copy "$SRC/claude-hub.py"          "$CLAUDE_DIR/claude-hub.py"
copy "$SRC/claude-wrapper.sh"      "$CLAUDE_DIR/claude-wrapper.sh"
copy "$SRC/agent-wrapper.sh"       "$CLAUDE_DIR/agent-wrapper.sh"
copy "$SRC/hooks/save-session.py"  "$CLAUDE_DIR/hooks/save-session.py"
copy "$SRC/hooks/session-start.py" "$CLAUDE_DIR/hooks/session-start.py"
copy "$SRC/hooks/memory-autosave.py" "$CLAUDE_DIR/hooks/memory-autosave.py"
# hub/ a tools/ jsou celé naše — nahrazují se vcelku, aby po updatu nezůstaly
# staré soubory. tools/ potřebuje sekce 7 (merge settings.json) a je fajn ho mít
# po ruce i bez repa (memory_index_trim.py).
rm -rf "$CLAUDE_DIR/hub" "$CLAUDE_DIR/tools"
cp -r "$SRC/hub" "$CLAUDE_DIR/hub"
cp -r "$SRC/tools" "$CLAUDE_DIR/tools"
find "$CLAUDE_DIR/hub" "$CLAUDE_DIR/tools" -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null
chmod +x "$CLAUDE_DIR/claude-hub.py" "$CLAUDE_DIR/claude-wrapper.sh" \
         "$CLAUDE_DIR/agent-wrapper.sh" \
         "$CLAUDE_DIR/hooks/save-session.py" "$CLAUDE_DIR/hooks/session-start.py" \
         "$CLAUDE_DIR/hooks/memory-autosave.py"
ok "aplikace v $CLAUDE_DIR"

# ── 5. Slash příkazy ─────────────────────────────────────────────────────────
# Claude Code ≥ 2.1 čte vlastní příkazy z ~/.claude/skills/<jméno>/SKILL.md
# (složka ~/.claude/commands/ se v tomhle buildu ignoruje). Šablony ze skills/
# se sem vyrenderují s cestami z konfigu.
STATE_FILE="$CLAUDE_DIR/session-state.md"
$HAS_VAULT && STATE_FILE="$MEMORY_DIR/session-state.md"
VAULT_ONLY=" save learn project skill "   # bez vaultu nedávají smysl
INSTALLED=""

# Skilly patří uživateli: aktualizace na ně nesahá vůbec a nová instalace
# přidá jen ty, které ještě nemá — vlastní úpravy ani smazané se nevrací.
for dir in "$SRC"/skills/*/; do
    $UPDATE && break
    name="$(basename "$dir")"
    [ -f "$dir/SKILL.md" ] || continue
    if ! $HAS_VAULT && [[ "$VAULT_ONLY" == *" $name "* ]]; then
        continue
    fi
    [ -e "$CLAUDE_DIR/skills/$name" ] && continue
    mkdir -p "$CLAUDE_DIR/skills/$name"
    sed -e "s|{{MEMORY_DIR}}|$MEMORY_DIR|g" \
        -e "s|{{PROJECT_DIRS}}|$PROJECT_DIRS_LIST|g" \
        -e "s|{{SKILLS_DIR}}|$BRAIN_SKILLS|g" \
        -e "s|{{CLAUDE_DIR}}|$CLAUDE_DIR|g" \
        -e "s|{{FTP_DEPLOY}}|$CLAUDE_DIR/ftp-deploy.sh|g" \
        -e "s|{{STATE_FILE}}|$STATE_FILE|g" \
        -e "s|{{PYTHON}}|$PY|g" \
        "$dir/SKILL.md" > "$CLAUDE_DIR/skills/$name/SKILL.md"
    INSTALLED="$INSTALLED /$name"
done
if $UPDATE; then
    ok "skilly nechávám, jak je máš"
else
    ok "slash příkazy:${INSTALLED:- (všechny už máš)}"
fi

if ls "$CLAUDE_DIR"/commands/*.md >/dev/null 2>&1; then
    warn "$CLAUDE_DIR/commands/ tenhle build Claude Code nečte — příkazy teď běží ze skills/"
fi

# ── 6. Ikona + položka v nabídce ─────────────────────────────────────────────
cp "$SRC/assets/claude-code.png" "$ICON_DIR/claude-code.png" 2>/dev/null
# Ikona v nabídce pod vlastním jménem: GNOME si obrázek pamatuje podle cesty,
# takže přepsaný claude-code.png by na liště zůstal ten starý (loďka).
cp "$SRC/assets/claude-code.png" "$ICON_DIR/aladin.png" 2>/dev/null && \
    ok "ikona v $ICON_DIR/aladin.png"

cat > "$APP_DIR/claude-code-hub.desktop" <<EOF
[Desktop Entry]
Version=1.0
Type=Application
Name=Aladin
Comment=Aladin — projekty a paměť v jednom okně s taby
Icon=$ICON_DIR/aladin.png
Terminal=false
Categories=Development;Utility;
Exec="$PY" "$CLAUDE_DIR/claude-hub.py"
StartupNotify=true
StartupWMClass=aladin
EOF
chmod +x "$APP_DIR/claude-code-hub.desktop"
update-desktop-database "$APP_DIR" >/dev/null 2>&1
ok "položka v nabídce aplikací: Aladin"

# ── 7. Hooky a režim oprávnění v settings.json ───────────────────────────────
# settings.json je uživatelův (klíče, model, vlastní hooky), takže se do něj
# nesází šablona — tools/settings_merge.py přidá jen to, co chybí, a předtím
# udělá zálohu. Vlastního hooku na stejnou událost se nedotkne.
BYPASS_FLAG=""
if ! $MINIMAL && ! $ASSUME_YES; then
    echo ""
    info "Zapnout ${A}bypass režim${R}? ${D}Claude pak nebude ptát na potvrzení u každého${R}"
    echo -e "     ${D}příkazu a úpravy souboru — rychlejší práce, ale běží bez brzdy.${R}"
    echo -e "     ${D}Zapni jen na vlastním stroji, kde víš, co ti Claude spouští.${R}"
    echo -e "     ${D}Kdykoli později: /permissions v Claude Code.${R}"
    ask "Zapnout bypass režim?" && BYPASS_FLAG="--bypass"
fi

if $MINIMAL; then
    info "--minimal: do settings.json nesahám"
else
    "$PY" "$CLAUDE_DIR/tools/settings_merge.py" --claude-dir "$CLAUDE_DIR" \
        --python "$PY" --hooks $BYPASS_FLAG 2>&1 | while read -r line; do
            case "$line" in
                chyba:*)  warn "${line#chyba: }" ;;
                *"nechávám být"*|*"beze změny"*|*"už "*) ok "$line" ;;
                *"vlastní hook"*) warn "$line" ;;
                *) info "$line" ;;
            esac
        done
fi

# ── 8. Playwright MCP (volitelné) ────────────────────────────────────────────
# Prohlížeč pro Claude Code — klikání, konzole a screenshoty přes accessibility
# tree místo vlastních Puppeteer skriptů. Registruje se do user scope
# (~/.claude.json), takže platí ve všech projektech. Bez zeptání se neinstaluje:
# poprvé stahuje ~115 MB prohlížeče.
#
# Profil prohlížeče se přibíjí na jedno místo. Bez `--user-data-dir` si ho
# Playwright MCP odvozuje z pracovní složky (mcp-<kanál>-<hash cwd>), takže
# každý tab v hubu dostal vlastní prohlížeč a přihlášení do Googlu bylo po
# přepnutí projektu pryč. tools/playwright_profile.py profil připraví a
# přihlášení převezme z toho nejpoužívanějšího ze starých.
if ! $UPDATE; then
PW_TOOL="$CLAUDE_DIR/tools/playwright_profile.py"
PW_PROFILE="$("$PY" "$PW_TOOL" --claude-dir "$CLAUDE_DIR" --path 2>/dev/null)"

prepare_playwright_profile() {
    "$PY" "$PW_TOOL" --claude-dir "$CLAUDE_DIR" 2>&1 | while read -r line; do
        case "$line" in
            chyba:*)     warn "${line#chyba: }" ;;
            varování:*)  warn "${line#varování: }" ;;
            *)           ok "$line" ;;
        esac
    done
}

register_playwright_mcp() {
    # --browser chromium = bundlovaný Chromium z ~/.cache/ms-playwright;
    # výchozí kanál "chrome" by chtěl systémový Google Chrome.
    # Přes tools/playwright_bridge.py: Chromium sdílený s oknem v appce
    # (hub/prohlizec.py). Bez něj jede Playwright MCP po staru.
    local bridge="$CLAUDE_DIR/tools/playwright_bridge.py"
    if [ -f "$bridge" ]; then
        claude mcp remove playwright -s user >/dev/null 2>&1 || true
        if claude mcp add playwright -s user -- "${PY:-python3}" "$bridge" >/dev/null 2>&1; then
            ok "playwright MCP zaregistrován přes vestavěný prohlížeč, profil ${D}$PW_PROFILE${R}"
            return 0
        fi
    fi
    if ! claude mcp add playwright -s user -- \
            npx @playwright/mcp@latest --browser chromium \
            --user-data-dir "$PW_PROFILE" >/dev/null 2>&1; then
        warn "'claude mcp add playwright' selhalo — přidej si ho ručně"
        return 1
    fi
    ok "playwright MCP zaregistrován (user scope), profil ${D}$PW_PROFILE${R}"
}

download_playwright_browser() {
    # Verze prohlížeče se váže na verzi MCP serveru, kterou pouští most
    # (tools/playwright_bridge.py, PW_MCP) — proto se stahuje přes něj.
    # Na Linuxu i systémové knihovny Chromia (nss, atk, cups, gbm, pango…):
    # bez nich se prohlížeč vůbec nespustí.
    local bridge="$CLAUDE_DIR/tools/playwright_bridge.py" out rc
    [ -f "$bridge" ] || bridge="$SRC/tools/playwright_bridge.py"
    info "připravuju prohlížeč pro Clauda ${D}(~190 MB poprvé, pak jen co chybí)${R}…"
    out="$("$PY" "$bridge" --install --deps 2>&1 >/dev/null)"; rc=$?
    if [ $rc -eq 0 ]; then
        ok "prohlížeč připraven"
    elif [ $rc -eq 3 ]; then
        ok "prohlížeč stažen"
        local cmd; cmd="$(printf '%s\n' "$out" | sed -n 's/^KNIHOVNY: //p' | head -1)"
        if [ -t 0 ] && [ -n "$cmd" ]; then
            info "prohlížeč potřebuje systémové knihovny — zeptá se na heslo (sudo)"
            if eval "$cmd" >/dev/null; then ok "knihovny prohlížeče nainstalované"
            else warn "knihovny se nenainstalovaly — dožeň to: $cmd"; fi
        elif [ -n "$cmd" ] && command -v pkexec >/dev/null 2>&1 \
                && [ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ]; then
            # Instalačka ke stažení nemá terminál — heslo si řekne okno systému.
            info "prohlížeč potřebuje systémové knihovny — systém se zeptá na heslo"
            if eval "pkexec env PATH=\"\$PATH\" ${cmd#sudo }" >/dev/null 2>&1; then ok "knihovny prohlížeče nainstalované"
            else warn "knihovny se nenainstalovaly — spusť jednou: ${D}$cmd${R}"; fi
        else
            warn "chybí systémové knihovny prohlížeče — spusť jednou: ${D}$cmd${R}"
        fi
    else
        warn "prohlížeč se nestáhl — dožeň to: $PY $bridge --install --deps"
    fi
}

NODE_MAJOR="$(node -v 2>/dev/null | sed 's/^v//; s/\..*//')"
PW_REGISTERED=false
PW_PINNED=false
if command -v claude >/dev/null 2>&1; then
    if PW_CURRENT="$(claude mcp get playwright 2>/dev/null)"; then
        PW_REGISTERED=true
        case "$PW_CURRENT" in *--user-data-dir*|*playwright_bridge*) PW_PINNED=true ;; esac
    fi
fi

echo ""
if $MINIMAL; then
    info "--minimal: Playwright MCP přeskočen"
elif ! command -v claude >/dev/null 2>&1; then
    info "Playwright MCP přeskočen — chybí Claude Code CLI"
elif [ -z "$NODE_MAJOR" ] || [ "$NODE_MAJOR" -lt 20 ]; then
    warn "Playwright MCP přeskočen — chce Node.js 20+ (teď: ${NODE_MAJOR:-žádný})"
    echo -e "     ${D}Doinstaluj Node 20+ a spusť instalačku znovu.${R}"
elif $PW_REGISTERED && $PW_PINNED; then
    # Profil se zkouší i tady: když se přenos přihlášení posledně nepovedl,
    # protože nad starým profilem běžel prohlížeč, dožene se to teď.
    ok "playwright MCP už je zaregistrovaný a přihlášení se drží"
    prepare_playwright_profile
    # Nová verze hubu může chtít novější Chromium — stáhne se jen co chybí.
    download_playwright_browser
elif $PW_REGISTERED; then
    # Starší registrace bez --user-data-dir: prohlížeč se odhlašoval s každou
    # změnou složky. Přeregistrovat a dostáhnout prohlížeč pro tuhle verzi.
    info "opravuju Playwright MCP — přihlášení se ztrácelo s každou složkou"
    prepare_playwright_profile
    claude mcp remove playwright -s user >/dev/null 2>&1 || true
    register_playwright_mcp && download_playwright_browser
else
    prepare_playwright_profile
    register_playwright_mcp && download_playwright_browser
fi

# ── 8a. Hlas (diktování a předčítání) ────────────────────────────────────────
# faster-whisper + Piper do vlastního venvu (hub/hlas.py), i s modely. Dřív
# jen tlačítkem v Nastavení → Hlas; instalačka ke stažení ho dá rovnou.
echo ""
HLAS_OK="$("$PY" -c "import sys; sys.path.insert(0, '$CLAUDE_DIR'); from hub import hlas; print(1 if hlas.root() else 0)" 2>/dev/null)"
if $MINIMAL || $UPDATE; then
    :
elif [ "$HLAS_OK" = "1" ]; then
    ok "hlas (diktování a předčítání)"
elif $APP || ask "Nainstalovat hlas — diktování a předčítání česky (~2,5 GB, modely)?"; then
    info "instaluju hlas ${D}(pár minut, stahují se modely ~2,5 GB)${R}…"
    if (cd "$CLAUDE_DIR" && "$PY" -m hub.hlas install >/dev/null 2>&1); then ok "hlas připravený"
    else warn "hlas se nenainstaloval — jde to i později: Nastavení → Hlas"; fi
else
    info "hlas přeskočen — jde doinstalovat v Nastavení → Hlas"
fi

# ── 8c. Další AI agenti a lokální modely ─────────────────────────────────────
# Codex, Gemini, opencode, aider, Ollama (+ základní modely do Ollamy ~7 GB)
# — tools/install_agents.py, ze stejného katalogu, který ukazuje appka.
echo ""
if $MINIMAL || $UPDATE; then
    :
elif $APP || ask "Nainstalovat i další AI agenty (Codex, Gemini, opencode, aider, Ollama) a lokální modely (~7 GB)?"; then
    info "instaluju další AI agenty a modely ${D}(může to trvat několik minut)${R}…"
    while IFS= read -r line; do
        case "$line" in
            ok:*)         ok "${line#ok: }" ;;
            chyba:*|přeskočeno:*) warn "${line#*: }" ;;
            instaluju:*|stahuju:*) info "${line#*: }" ;;
        esac
    done < <("$PY" "$CLAUDE_DIR/tools/install_agents.py" 2>&1)
else
    info "další agenti přeskočeni — jdou doinstalovat v Nastavení → AI agenti"
fi

# ── 8b. Clockify MCP (volitelné) ─────────────────────────────────────────────
# Výkazy času a projekty z Clockify přímo v Claude Code. HTTP server, ověřuje
# se hlavičkou x-api-key — klíč se bere jen tady, do repa ani do logu se
# nedostane (zapíše si ho Claude Code do ~/.claude.json, jako u ostatních).
# Klíč: Clockify → foto profilu → Preferences → Advanced → Manage API keys.
CLOCKIFY_URL="https://api.clockify.me/mcp-server/mcp"

echo ""
if $MINIMAL; then
    info "--minimal: Clockify MCP přeskočen"
elif ! command -v claude >/dev/null 2>&1; then
    info "Clockify MCP přeskočen — chybí Claude Code CLI"
elif claude mcp get clockify >/dev/null 2>&1; then
    ok "clockify MCP už je zaregistrovaný"
elif [ -n "${CLOCKIFY_API_KEY:-}" ]; then
    # Neinteraktivní cesta: klíč z prostředí, ať jde instalačka i s --yes.
    if claude mcp add clockify "$CLOCKIFY_URL" -s user --transport http \
            --header "x-api-key: $CLOCKIFY_API_KEY" >/dev/null 2>&1; then
        ok "clockify MCP zaregistrován z CLOCKIFY_API_KEY (user scope)"
    else
        warn "'claude mcp add clockify' selhalo — zkus to z Nastavení v hubu"
    fi
elif $ASSUME_YES; then
    info "Clockify MCP přeskočen — klíč nemám (nastav CLOCKIFY_API_KEY, nebo ho zadej v hubu)"
elif ask "Napojit Clockify (výkazy času)? Budeš potřebovat API klíč"; then
    read -r -p "     API klíč z Clockify: " CLOCKIFY_KEY
    if [ -z "$CLOCKIFY_KEY" ]; then
        info "bez klíče to nejde — přidáš ho kdykoli v hubu: Nastavení → Napojení (MCP)"
    elif claude mcp add clockify "$CLOCKIFY_URL" -s user --transport http \
            --header "x-api-key: $CLOCKIFY_KEY" >/dev/null 2>&1; then
        ok "clockify MCP zaregistrován (user scope)"
    else
        warn "'claude mcp add clockify' selhalo — zkus to z Nastavení v hubu"
    fi
    unset CLOCKIFY_KEY
else
    echo -e "     ${D}Později: Nastavení → Napojení (MCP) → Přidat Clockify${R}"
fi

# ── 9. Přihlášení do Claude Code ─────────────────────────────────────────────
# Bez přihlášení se v každém tabu objeví login obrazovka. Spustit `claude` tady
# je nejrychlejší cesta: uživatel projde /login a dá /exit.
if command -v claude >/dev/null 2>&1; then
    if [ -f "$CLAUDE_DIR/.credentials.json" ] || [ -n "${ANTHROPIC_API_KEY:-}" ]; then
        ok "Claude Code je přihlášený"
    elif ask "Přihlásit se teď do Claude Code? (spustí claude, projdi /login a dej /exit)"; then
        claude || true
    else
        echo -e "     ${D}Později: spusť claude a napiš /login${R}"
    fi
fi

# Závěrečná kontrola: jeden výpis, ze kterého je vidět, co na stroji opravdu je.
"$PY" "$CLAUDE_DIR/claude-hub.py" --doctor || true
fi   # ! $UPDATE — Playwright, Clockify, přihlášení a kontrola

if $UPDATE; then
    echo ""
    echo -e "  ${A}✦${R} Aktualizováno."
    echo ""
    exit 0
fi

echo ""
echo -e "  ${A}✦${R} Hotovo. Spusť: ${D}python3 $CLAUDE_DIR/claude-hub.py${R}  (nebo ikonu Aladin v nabídce)"
echo -e "  ${D}Kontrola prostředí:  python3 $CLAUDE_DIR/claude-hub.py --doctor${R}"
echo -e "  ${D}Když okno zůstane prázdné, důvod je v $CLAUDE_DIR/hub.log${R}"
echo ""
