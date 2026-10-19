#!/bin/bash
# Noční šifrovaná záloha brány. Pouští ji claude-hub-zaloha.timer (viz
# gateway/install.sh) jako root, jednou denně.
#
# Zálohuje se /home/hub — domovy prostorů (Obsidian, projekty), firemní vault
# a databáze účtů. Cache, node_modules a virtuální prostředí Pythonu se
# vynechávají: jsou to gigabajty, které se stáhnou nebo nainstalují znovu,
# a obnovu by jen zdržely.
#
# ŠIFRUJE SE VEŘEJNÝM KLÍČEM. Server má jen ten veřejný, takže zálohu umí
# zabalit, ale rozbalit ne — a nepotřebuje k tomu žádné heslo. Soukromý klíč
# patří mimo server (u správce v trezoru hesel). Kdo ukradne server i se
# zálohami, nepřečte z nich nic.
#
# Klíč se nasadí jednou:
#     gpg --armor --export <klíč> | ssh root@brána 'cat > /etc/claude-hub/zalohy.asc'
#
# Bez toho souboru se záloha neudělá a jen to řekne — radši žádná záloha než
# nešifrovaná, která by ležela na disku a tvářila se, že je o co opřít.
#
# PLNÁ A ROZDÍLOVÁ. Plná záloha (hub-RRRRMMDD-HHMM.tar.zst.gpg) má přes 9 GB
# a na disku musí chvíli ležet vedle té předchozí — na malém disku se tam
# obě nevejdou a záloha pak dny nevznikala vůbec. Proto se plná dělá jen
# jednou za HUB_ZALOHA_PLNA_PO dní (a jen když se vejde) a ostatní noci
# rozdílová: jen soubory změněné od začátku poslední plné
# (hub-RRRRMMDD-HHMM-rozdil-od-RRRRMMDD-HHMM.tar.zst.gpg). Každá rozdílová
# stačí sama s plnou, ze které vychází — mezi sebou na sobě nezávisí.
#
# Obnova (na stroji, kde je soukromý klíč) — plná a pak nejnovější rozdílová
# k ní; soubory smazané po plné záloze se tím vrátí, nic jiného navíc:
#     gpg -d hub-RRRRMMDD-HHMM.tar.zst.gpg | zstd -d | tar -xf - -C /kam
#     gpg -d hub-…-rozdil-od-RRRRMMDD-HHMM.tar.zst.gpg | zstd -d | tar -xf - -C /kam
#
# MÍSTO NA DISKU HLÍDÁ ZÁPIS SÁM: když během zálohy klesne volné místo pod
# HUB_ZALOHA_MIN_MB, zápis se utne a rozepsaný soubor smaže — plný disk by
# shodil práci lidem v prostorech, ne jen zálohu. Odhad předem je proto jen
# „má cenu to zkoušet", ne pojistka.
#
# Ručně: claude-hub-zaloha
set -uo pipefail

KEY=${HUB_ZALOHA_KEY:-/etc/claude-hub/zalohy.asc}
DEST=${HUB_ZALOHA_DEST:-/var/backups/claude-hub}
SRC=${HUB_ZALOHA_SRC:-/home/hub}
RING=${HUB_ZALOHA_GNUPG:-/etc/claude-hub/zalohy-gnupg}
LOG=${HUB_ZALOHA_LOG:-/var/log/claude-hub-zaloha.log}
KEEP=${HUB_ZALOHA_KEEP:-14}                 # kolik plných záloh se drží
ROZDILY=${HUB_ZALOHA_ROZDILY:-3}            # kolik rozdílových se drží
PLNA_PO=${HUB_ZALOHA_PLNA_PO:-7}            # plná nejdřív po tolika dnech od poslední
REZERVA_MB=${HUB_ZALOHA_REZERVA_MB:-3072}   # kolik místa má po záloze zbýt
MIN_MB=${HUB_ZALOHA_MIN_MB:-$((REZERVA_MB / 2))}  # pod tohle zápis utnout
SLEDUJ_S=${HUB_ZALOHA_SLEDUJ_S:-5}          # jak často hlídat místo při zápisu

log() { printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >>"$LOG"; }
volno() { df --output=avail -B1 "$DEST" | tail -n 1; }
mb() { echo $(( $1 / 1024 / 1024 )); }

# Plné zálohy od nejnovější; rozdílové od nejnovější (případně jen k dané plné).
# (Vzor musí mít přesný počet číslic: hub-[0-9]*-… by chytil i rozdílovou.)
D8='[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]' D4='[0-9][0-9][0-9][0-9]'
plne() { ls -1t "$DEST"/hub-$D8-$D4.tar.zst.gpg 2>/dev/null; }
rozdily() {
    if [ -n "${1:-}" ]; then ls -1t "$DEST"/hub-$D8-$D4-rozdil-od-"$1".tar.zst.gpg 2>/dev/null
    else ls -1t "$DEST"/hub-$D8-$D4-rozdil-od-$D8-$D4.tar.zst.gpg 2>/dev/null; fi
}
# hub-20261019-2245.tar.zst.gpg → 20261019-2245
razitko() { basename "$1" | sed -E 's/^hub-([0-9]{8}-[0-9]{4}).*/\1/'; }
# 20261019-2245 → sekundy od epochy
epocha() { date -d "${1:0:4}-${1:4:2}-${1:6:2} ${1:9:2}:${1:11:2}" +%s; }

# Smaže plnou zálohu i rozdílové, které z ní vycházejí (bez ní jsou k ničemu).
smaz_plnou() {
    local f="$1" r
    rm -f "$f"
    while read -r r; do [ -n "$r" ] && rm -f "$r"; done <<<"$(rozdily "$(razitko "$f")")"
}

# zabal <výstup> [--newer=@epocha]
# Vrací 0 = hotovo, 2 = utnuto kvůli místu, 1 = jiná chyba.
zabal() {
    local out="$1" tmp="$1.part" navic=("${@:2}") flag="$1.part.malo-mista"
    rm -f "$flag"
    # Hlídač místa: když volno klesne pod MIN_MB, zabije gpg (zstd a tar pak
    # skončí na přerušené rouře) a nechá značku, proč.
    (
        while sleep "$SLEDUJ_S"; do
            [ "$(volno)" -gt $((MIN_MB * 1024 * 1024)) ] && continue
            touch "$flag"
            pkill -TERM -f -- "--output $tmp" 2>/dev/null
            break
        done
    ) &
    local hlidac=$!

    # nice/ionice: na stroji zároveň pracují lidé, záloha jim nesmí ubrat výkon.
    # Vynechané cesty jsou cache (stáhnou se znovu), závislosti projektů
    # (node_modules, virtuální prostředí Pythonu — poznají se podle pyvenv.cfg)
    # a koše po smazaných účtech. --exclude-caches-all: složky označené
    # CACHEDIR.TAG (standard pro cache, dělá ho uv, pip, cargo…).
    nice -n 19 ionice -c3 tar -C "$(dirname "$SRC")" -cf - \
            --warning=no-file-changed --warning=no-file-ignored \
            "${navic[@]}" \
            --exclude-caches-all \
            --exclude-tag-all=pyvenv.cfg \
            --exclude='*/.cache' \
            --exclude='*/.npm' \
            --exclude='*/.local/share/uv' \
            --exclude='*/.local/share/pnpm' \
            --exclude='*/.cache/ms-playwright' \
            --exclude='*/node_modules' \
            --exclude='*/.claude/projects/*/[0-9a-f]*.jsonl.bak' \
            --exclude='users/_smazany-*' \
            "$(basename "$SRC")" 2>>"$LOG" \
        | nice -n 19 zstd -3 -T2 -q \
        | GNUPGHOME="$RING" gpg --batch --yes --quiet --trust-model always \
              --encrypt --recipient "$RECIPIENT" --output "$tmp"
    local st=("${PIPESTATUS[@]}")
    kill "$hlidac" 2>/dev/null
    wait "$hlidac" 2>/dev/null

    # tar vrací 1, i když se jen soubor změnil během čtení — u živého serveru
    # běžné a záloha je použitelná. Chyba zstd nebo gpg (třeba plný disk) ale
    # znamená useknutý soubor: ten se nesmí tvářit jako záloha.
    if [ ! -e "$flag" ] && [ "${st[1]}" -eq 0 ] && [ "${st[2]}" -eq 0 ] \
            && [ "${st[0]}" -le 1 ] && [ -s "$tmp" ]; then
        mv -f "$tmp" "$out"
        chmod 600 "$out"
        if [ "${st[0]}" -eq 0 ]; then
            log "hotovo: $(basename "$out") ($(du -h "$out" | cut -f1))"
        else
            log "hotovo s výhradou (soubor se měnil během čtení): $(basename "$out") ($(du -h "$out" | cut -f1))"
        fi
        return 0
    fi
    rm -f "$tmp"
    if [ -e "$flag" ]; then
        rm -f "$flag"
        log "CHYBA: zápis utnut, volné místo kleslo pod $MIN_MB MB — $(basename "$out") jsem smazal, prostory mají přednost"
        return 2
    fi
    log "CHYBA: záloha se nepovedla (tar ${st[0]}, zstd ${st[1]}, gpg ${st[2]}; volno $(df -h "$DEST" | awk 'NR==2{print $4}')), nic jsem nezapsal"
    return 1
}

# Plná záloha. Když se vedle poslední nevejde, smažou se předem starší plné
# nad KEEP-1 (úklid po záloze by je stejně smazal). Poslední dobrou nikdy:
# kdyby nová selhala, nezbyla by žádná. Vrací 3 = nevejde se.
plna() {
    local stamp="$1" posledni="$2" need f
    if [ -n "$posledni" ]; then
        # Odhad: jako poslední +10 % a rezerva. Přesnost tu nehraje roli —
        # skutečné místo hlídá zápis (zabal).
        need=$(( $(stat -c %s "$posledni") * 11 / 10 + REZERVA_MB * 1024 * 1024 ))
        while read -r f; do
            [ -n "$f" ] || continue
            [ "$(volno)" -gt "$need" ] && break
            smaz_plnou "$f"
            log "kvůli místu předem smazána starší plná záloha: $(basename "$f")"
        done <<<"$(plne | tail -n +$((KEEP > 1 ? KEEP : 2)) | tac)"
        if [ "$(volno)" -le "$need" ]; then
            log "plná záloha se nevejde (volno $(mb "$(volno)") MB, potřeba asi $(mb "$need") MB)"
            return 3
        fi
    fi
    log "začínám plnou zálohu (příjemce: $RECIPIENT)"
    zabal "$DEST/hub-$stamp.tar.zst.gpg"
}

# Rozdílová záloha k poslední plné: soubory změněné (obsah i přejmenování,
# tar -N hlídá mtime i ctime) od začátku plné. Razítko plné je čas jejího
# startu zaokrouhlený dolů, takže nic, co se změnilo během ní, nechybí.
rozdilova() {
    local stamp="$1" zaklad="$2" od need f predchozi
    od="$(razitko "$zaklad")"
    predchozi="$(rozdily "$od" | head -n 1)"
    if [ -n "$predchozi" ]; then
        need=$(( $(stat -c %s "$predchozi") * 13 / 10 + REZERVA_MB * 1024 * 1024 ))
    else
        need=$(( (REZERVA_MB + 1024) * 1024 * 1024 ))
    fi
    # Starší rozdílové kvůli místu pryč — nejnovější zůstává, dokud nevznikne nová.
    while read -r f; do
        [ -n "$f" ] || continue
        [ "$(volno)" -gt "$need" ] && break
        rm -f "$f"
        log "kvůli místu předem smazána starší rozdílová záloha: $(basename "$f")"
    done <<<"$(rozdily | tail -n +2 | tac)"
    if [ "$(volno)" -le "$need" ]; then
        log "CHYBA: na disku je $(mb "$(volno)") MB, i na rozdílovou zálohu s rezervou je potřeba asi $(mb "$need") MB — zálohu nedělám, uvolni místo"
        echo "Na disku není místo ani na rozdílovou zálohu (volno $(mb "$(volno)") MB, potřeba $(mb "$need") MB)." >&2
        return 1
    fi
    log "začínám rozdílovou zálohu od $od (příjemce: $RECIPIENT)"
    zabal "$DEST/hub-$stamp-rozdil-od-$od.tar.zst.gpg" "--newer=@$(epocha "$od")"
}

main() {
    if [ ! -s "$KEY" ]; then
        log "CHYBA: chybí veřejný klíč $KEY — záloha se nedělá (nešifrovanou dělat nebudu)"
        echo "Chybí $KEY. Nasaď veřejný klíč, jinak se zálohovat nebude." >&2
        exit 1
    fi
    mkdir -p "$DEST"
    chmod 700 "$DEST"

    # Vlastní klíčenka v /etc: root nemá mít v ~/.gnupg nic, co sem nepatří,
    # a --trust-model always kvůli tomu, že klíč nikdo nepodepsal.
    mkdir -p "$RING"; chmod 700 "$RING"
    GNUPGHOME="$RING" gpg --batch --quiet --import "$KEY" 2>/dev/null
    RECIPIENT="$(GNUPGHOME="$RING" gpg --batch --with-colons --list-keys 2>/dev/null \
                 | awk -F: '/^uid/{print $10; exit}')"
    if [ -z "$RECIPIENT" ]; then
        log "CHYBA: klíč $KEY se nepodařilo načíst"
        exit 1
    fi

    local stamp posledni vek rc
    stamp="$(date +%Y%m%d-%H%M)"
    posledni="$(plne | head -n 1)"
    if [ -n "$posledni" ]; then
        vek=$(( ( $(date +%s) - $(epocha "$(razitko "$posledni")") ) / 86400 ))
    fi
    if [ -z "$posledni" ] || [ "$vek" -ge "$PLNA_PO" ] || [ "${1:-}" = "--plna" ]; then
        plna "$stamp" "$posledni"; rc=$?
        # Plná se nevešla (nebo ji utnul hlídač místa): rozdílová je pořád
        # lepší než žádná — když je od čeho.
        if [ "$rc" -ge 2 ] && [ -n "$posledni" ] && [ -s "$posledni" ]; then
            log "místo plné dělám rozdílovou (plná je z $(razitko "$posledni"), $vek dní)"
            rozdilova "$stamp" "$posledni"; rc=$?
        elif [ "$rc" -eq 3 ]; then
            log "CHYBA: plná záloha se nevejde a žádná starší, od které by šla rozdílová, není — uvolni místo"
            rc=1
        fi
    else
        rozdilova "$stamp" "$posledni"; rc=$?
    fi
    [ "$rc" -eq 0 ] || exit 1

    # Úklid: drží se posledních KEEP plných (i s jejich rozdílovými)
    # a posledních ROZDILY rozdílových. Rozdílové bez své plné jdou pryč.
    local f n=0
    while read -r f; do
        [ -n "$f" ] || continue
        smaz_plnou "$f"; n=$((n + 1))
    done <<<"$(plne | tail -n +$((KEEP + 1)))"
    while read -r f; do
        [ -n "$f" ] || continue
        rm -f "$f"; n=$((n + 1))
    done <<<"$(rozdily | tail -n +$((ROZDILY + 1)))"
    while read -r f; do
        [ -n "$f" ] || continue
        local od="${f##*-rozdil-od-}"; od="${od%%.tar.zst.gpg}"
        [ -e "$DEST/hub-$od.tar.zst.gpg" ] && continue
        rm -f "$f"; n=$((n + 1))
    done <<<"$(rozdily)"
    [ "$n" -gt 0 ] && log "smazáno starých záloh: $n"
    log "volno na disku: $(df -h "$DEST" | awk 'NR==2{print $4}')"
}

main "$@"
