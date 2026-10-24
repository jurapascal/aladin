#!/bin/bash
# Aktualizace brány Claude Code Hub. Pouští ji claude-hub-update.timer
# (nastaví gateway/install.sh) jako root, každou hodinu.
#
# Ve dvou krocích, ať se nikomu nic neutne:
#
#  1. Nová verze se PŘIPRAVÍ hned, i když lidi pracují: vybalí se ze značky
#     na GitHubu do vlastní složky /opt/claude-code-hub-verze/<verze>.
#     Běžících prostorů se to nedotkne — každý jede ze své složky. Brána z té
#     nejnovější pouští každý prostor, který startuje znovu, a v prostoru se
#     ukáže „Na serveru je nová verze · Aktualizovat · Později". Aktualizovat
#     = hub si uloží taby, prostor se restartuje a taby se vrátí i s
#     konverzacemi (hub/static/hub.js, serverUpdate).
#  2. Bránu samotnou (zdroj, install.sh, restart služby) přepne, až nepoběží
#     žádný prostor: restart brány by prostory zastavil.
#
# Jak to dopadlo, zapíše do /etc/claude-hub/update.json (`ready` = nejnovější
# připravená verze) — prostor to ukáže v Nastavení → Aktualizace.
#
# Ručně: claude-hub-update
#        claude-hub-update --hned   # nečekat: zastavit prostory a bránu přepnout
#                                   # hned (rozdělanou práci v nich to přeruší)
#
# Celé ve funkcích a spouštěné až posledním řádkem: install.sh tenhle soubor
# během běhu přepíše novou verzí a bash by jinak dočítal už jiný obsah.
set -uo pipefail

CONF=/etc/claude-hub/install.conf
STATUS=/etc/claude-hub/update.json
LOG=/var/log/claude-hub-update.log
REPO_URL="https://github.com/jurapascal/aladin.git"
# Připravené verze pro prostory. Musí sedět s gateway/workspace.py (VERZE_DIR).
VERZE_DIR=/opt/claude-code-hub-verze
# Kolik starších verzí nechat. Ta, ze které ještě jede nějaký prostor, se
# nemaže nikdy — smazané soubory by mu zmizely pod rukama.
VERZE_NECHAT=3

# status <ok:true|false> <věta> [nová verze] [připravená verze]
status() {
    python3 - "$STATUS" "$1" "$2" "${3:-}" "${4:-}" <<'EOF'
import json, os, sys, time
path, ok, detail, version, ready = sys.argv[1:6]
try:
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
except Exception:
    data = {}
data.update({"checked_at": int(time.time()), "ok": ok == "true", "detail": detail})
if version:
    data.update({"version": version, "updated_at": int(time.time())})
if ready:
    data.update({"ready": ready})
tmp = path + ".tmp"
with open(tmp, "w", encoding="utf-8") as fh:
    json.dump(data, fh, ensure_ascii=False)
os.chmod(tmp, 0o644)
os.replace(tmp, path)
EOF
    echo "$(date '+%F %T') $2" >>"$LOG"
}

# Vybalí značku do $VERZE_DIR/<verze>. Nejdřív vedle a přejmenováním — prostor
# nesmí nikdy vidět napůl vybalenou složku; hotová má v sobě .hotovo.
pripravit() {
    local tag="$1" ver="${1#v}" cil tmp
    cil="$VERZE_DIR/$ver"
    [ -f "$cil/.hotovo" ] && return 0
    install -d -m 755 "$VERZE_DIR"
    tmp="$(mktemp -d "$VERZE_DIR/.tmp-$ver-XXXXXX")" || return 1
    if ! git -C "$REPO_DIR" archive "$tag" | tar -x -C "$tmp"; then
        rm -rf "$tmp"
        return 1
    fi
    chmod -R a+rX,go-w "$tmp"
    git -C "$REPO_DIR" rev-list -n 1 "$tag" >"$tmp/.hotovo"
    rm -rf "$cil"
    mv "$tmp" "$cil"
    echo "$(date '+%F %T') připravena verze $ver" >>"$LOG"
}

# Starší verze pryč — kromě posledních $VERZE_NECHAT a těch, ze kterých ještě
# jede prostor (je vidět v přípojných bodech jeho sandboxu).
uklidit() {
    local ver
    [ -d "$VERZE_DIR" ] || return 0
    rm -rf "$VERZE_DIR"/.tmp-* 2>/dev/null
    for ver in $(ls -1 "$VERZE_DIR" | grep -E '^[0-9]+\.[0-9]+\.[0-9]+$' | sort -V | head -n "-$VERZE_NECHAT"); do
        if grep -qs " $VERZE_DIR/$ver " /proc/[0-9]*/mountinfo; then
            continue
        fi
        rm -rf "${VERZE_DIR:?}/$ver"
        echo "$(date '+%F %T') smazána stará verze $ver" >>"$LOG"
    done
}

# Claude Code v /usr. Prostory ho mají jen ke čtení, takže se sám neaktualizuje
# nikdy — bez tohohle zůstal na verzi z instalace (2.1.266) a v prostorech
# chyběly nové modely. Běžící Claude jede dál na starém, nová konverzace už na
# novém; npm soubory vyměňuje přejmenováním, běžícím procesům je nesebere.
aktualizovat_claude() {
    local mam nejnovejsi
    command -v npm >/dev/null 2>&1 || return 0
    mam="$(claude --version 2>/dev/null | awk '{print $1}')"
    nejnovejsi="$(timeout 60 npm view @anthropic-ai/claude-code version 2>/dev/null)"
    [ -n "$nejnovejsi" ] || return 0
    [ "$mam" = "$nejnovejsi" ] && return 0
    if timeout 600 npm install -g --no-fund --no-audit "@anthropic-ai/claude-code@$nejnovejsi" \
            >>"$LOG" 2>&1; then
        echo "$(date '+%F %T') Claude Code ${mam:-?} → $nejnovejsi" >>"$LOG"
    else
        echo "$(date '+%F %T') Claude Code se nepodařilo aktualizovat na $nejnovejsi" >>"$LOG"
    fi
}

main() {
    [ "$(id -u)" -eq 0 ] || { echo "Pusť jako root." >&2; exit 1; }
    [ -f "$CONF" ] || { echo "Chybí $CONF — pusť gateway/install.sh." >&2; exit 1; }
    DOMAIN="" EMAIL="" REPO_DIR="" TLS=true
    # shellcheck disable=SC1090
    . "$CONF"
    install -d -m 755 /etc/claude-hub
    aktualizovat_claude

    if [ ! -d "$REPO_DIR/.git" ]; then
        status false "Zdroj v $REPO_DIR není git — aktualizace přeskočena (pusť gateway/install.sh)."
        return 0
    fi

    local latest current running
    latest="$(git ls-remote --tags --refs "$REPO_URL" 2>/dev/null \
        | sed 's#.*refs/tags/##' | grep -E '^v[0-9]+\.[0-9]+\.[0-9]+$' | sort -V | tail -n1)"
    if [ -z "$latest" ]; then
        status false "GitHub neodpověděl — zkusím to při dalším pokusu."
        return 0
    fi
    current="$(git -C "$REPO_DIR" describe --tags --exact-match 2>/dev/null || true)"
    if [ "$current" = "$latest" ]; then
        # I verze, na které server už je, má mít svou složku — z ní brána
        # pouští prostory (starší instalace ji ještě neměla).
        pripravit "$latest" >>"$LOG" 2>&1 || true
        uklidit
        status true "Server má nejnovější verzi ${latest#v}." "" "${latest#v}"
        return 0
    fi

    # 1. Připravit — běžícím prostorům nic nevadí. Stahuje se jen do .git,
    #    pracovní strom zdroje (a tím brána) zůstává, jak je.
    if ! { git -C "$REPO_DIR" fetch --tags --quiet origin && pripravit "$latest"; } >>"$LOG" 2>&1; then
        status false "Novou verzi ${latest#v} se nepodařilo připravit — podrobnosti v $LOG."
        return 1
    fi
    uklidit

    # 2. Bránu přepnout, až neběží žádný prostor. S --hned je zastaví správce
    # sám — otevřený prohlížeč si prostor hned spustí znovu, proto dokola,
    # dokud nejsou všechny dole, a hned potom přepnutí (install.sh je krátký).
    if [ "$HNED" = 1 ]; then
        local pokus
        for pokus in 1 2 3 4 5; do
            claude-hub-admin sessions 2>/dev/null | awk '/^claude-hub-/{print $NF}' |
                while read -r email; do claude-hub-admin stop "$email" >/dev/null 2>&1; done
            [ "$(claude-hub-admin sessions 2>/dev/null | grep -c '^claude-hub-' || true)" -eq 0 ] && break
            sleep 1
        done
    fi
    running="$(claude-hub-admin sessions 2>/dev/null | grep -c '^claude-hub-' || true)"
    if [ "${running:-0}" -gt 0 ] && [ "$HNED" != 1 ]; then
        status true "Verze ${latest#v} je připravená — prostory se na ni přepnou, až je lidi aktualizují. Brána se přepne, až nepoběží žádný prostor (teď běží: $running)." "" "${latest#v}"
        return 0
    fi

    local tls=()
    [ "$TLS" = "false" ] && tls=(--no-tls)
    local email=()
    [ -n "$EMAIL" ] && email=(--email "$EMAIL")
    if ! {
        echo "=== $(date '+%F %T') ${current:-?} → $latest"
        git -C "$REPO_DIR" fetch --tags --quiet origin &&
            git -C "$REPO_DIR" checkout --quiet "$latest" &&
            bash "$REPO_DIR/gateway/install.sh" --domain "$DOMAIN" "${email[@]}" \
                --ref "$latest" "${tls[@]}"
    } >>"$LOG" 2>&1; then
        status false "Aktualizace na ${latest#v} selhala — podrobnosti v $LOG."
        return 1
    fi

    # install.sh bránu nerestartuje (zdroj už byl přepnutý předem), tak tady —
    # nikdo nepracuje, ověřeno o kus výš.
    local uid
    uid="$(id -u hub)"
    # Log patří rootovi (běží to jako root) — přesměrování mimo sudo je záměr.
    # shellcheck disable=SC2024
    sudo -u hub XDG_RUNTIME_DIR="/run/user/$uid" \
        DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$uid/bus" \
        systemctl --user restart claude-hub-gateway >>"$LOG" 2>&1
    status true "Aktualizováno na ${latest#v}." "${latest#v}" "${latest#v}"
}

HNED=0
[ "${1:-}" = "--hned" ] && HNED=1
main "$@"
exit
