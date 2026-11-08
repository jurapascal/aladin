#!/bin/bash
# Hub agent wrapper
# - Shows a boot sequence (project, optional Obsidian Brain, git, hooks)
# - Restarts after an accidental ctrl+c
#
# Which agent this starts is decided by the hub, not by this script: everything
# comes in as HUB_AGENT_* environment variables. That keeps the catalogue in one
# place (hub/agents.py) instead of duplicating it in shell, and it means a new
# agent needs no change here at all.
#
#   HUB_AGENT_ID       claude | codex | gemini | …
#   HUB_AGENT_BIN      binárka, která se má spustit
#   HUB_AGENT_LABEL    jak se jmenuje v hlavičce
#   HUB_AGENT_ANSI     číslo z 256barevné palety pro tu hlavičku
#   HUB_AGENT_INSTALL  co poradit, když binárka není v PATH
#   HUB_AGENT_AUTH     co poradit k přihlášení
#   HUB_AGENT_BRAIN    1 = agent čte Obsidian Brain a hooky (jen Claude Code)
#
# Argumenty za skriptem se předají agentovi (úvodní prompt, --model, …).

CLAUDE_DIR="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
CONFIG="$CLAUDE_DIR/hub-config.json"

AGENT_ID="${HUB_AGENT_ID:-claude}"
AGENT_BIN="${HUB_AGENT_BIN:-claude}"
# Windows cesta (C:\...\claude.CMD) od staršího hubu: Git Bash ji přes
# `command -v` nenajde a tab by skončil holým shellem. Převést na /c/... a u npm
# shimu (.cmd/.bat) vzít bashový shim vedle něj. Hub to dělá i sám (core.shell_bin).
case "$AGENT_BIN" in
    [A-Za-z]:*)
        command -v cygpath >/dev/null 2>&1 && AGENT_BIN="$(cygpath -u "$AGENT_BIN")"
        case "$AGENT_BIN" in
            *.cmd|*.CMD|*.Cmd|*.bat|*.BAT) [ -f "${AGENT_BIN%.*}" ] && AGENT_BIN="${AGENT_BIN%.*}" ;;
        esac ;;
esac
AGENT_LABEL="${HUB_AGENT_LABEL:-$AGENT_BIN}"
AGENT_ANSI="${HUB_AGENT_ANSI:-208}"
AGENT_INSTALL="${HUB_AGENT_INSTALL:-}"
AGENT_AUTH="${HUB_AGENT_AUTH:-}"
AGENT_BRAIN="${HUB_AGENT_BRAIN:-0}"

# Windows has no `python3` — only `python`/`py`, and the WindowsApps stub of the
# same name just opens the Store, so each candidate has to be proven by running it.
PY=""
for cand in python3 python py; do
    command -v "$cand" >/dev/null 2>&1 || continue
    if [ "$("$cand" -c "print('ok')" 2>/dev/null)" = "ok" ]; then
        PY="$cand"
        break
    fi
done

# brain_dir from the config, default ~/Obsidian/Claude-Brain
BRAIN=$([ -n "$PY" ] && "$PY" - "$CONFIG" <<'PYEOF' 2>/dev/null
import json, os, sys
try:
    cfg = json.load(open(sys.argv[1]))
except Exception:
    cfg = {}
print(os.path.expanduser(cfg.get("brain_dir") or "~/Obsidian/Claude-Brain"))
PYEOF
)
[ -z "$BRAIN" ] && BRAIN="$HOME/Obsidian/Claude-Brain"

# ── Colors ──
# Barva hlavičky: HUB_AGENT_RGB (barva prostředí z hubu — modrá v prostoru,
# fialová nad firemním trezorem) má přednost před barvou agenta z katalogu.
A="\033[38;5;${AGENT_ANSI}m"  # barva agenta (primary)
case "$HUB_AGENT_RGB" in
    *[!0-9\;]*|"") ;;                       # nic nebo podezřelý text = beze změny
    *) A="\033[38;2;${HUB_AGENT_RGB}m" ;;
esac
G="\033[38;5;114m"  # soft green
Y="\033[38;5;180m"  # warm peach
D="\033[2m"         # dim
B="\033[1m"         # bold
W="\033[38;5;252m"  # cream
R="\033[0m"         # reset

# ── Boot Sequence ──
boot_sequence() {
    clear
    echo ""
    echo -e "  ${B}${A}✦${R}  ${B}${W}${AGENT_LABEL}${R}"
    echo -e "  ${D}──────────────────────────────────────${R}"
    echo ""
    echo -e "  ${A}▸${R} ${W}$(basename "$(pwd)")${R}"
    echo -e "    ${D}$(pwd)${R}"
    echo ""

    # Loading animation helper
    show_step() {
        local msg="$1"
        echo -ne "  ${D}  ${msg}...${R}"
    }
    done_step() {
        local msg="$1"
        local detail="$2"
        echo -e "\r  ${A}✦${R} ${W}${msg}${R} ${D}${detail}${R}      "
    }

    # 1. Skills + 2. Memory — jen pro agenta, který si Brain doopravdy čte.
    # Codexovi nebo Gemini je naše paměť k ničemu, tak ať se jí nechlubíme.
    if [ "$AGENT_BRAIN" = "1" ] && [ -d "$BRAIN" ]; then
        show_step "Loading Obsidian Brain skills"
        SKILL_COUNT=$(find "$BRAIN/skills" -name "SKILL.md" 2>/dev/null | wc -l)
        CAT_COUNT=$(find "$BRAIN/skills" -maxdepth 1 -type d ! -name "_*" ! -name "skills" 2>/dev/null | wc -l)
        done_step "Skills loaded" "(${SKILL_COUNT} skills, ${CAT_COUNT} categories)"

        show_step "Reading memory"
        MEM_DIR="$BRAIN/memory"
        NOTE_COUNT=$(find "$MEM_DIR" -maxdepth 1 -name '*.md' ! -name 'MEMORY.md' ! -name 'session-state.md' 2>/dev/null | wc -l)
        LEARN_COUNT=$(find "$MEM_DIR" -maxdepth 1 -name 'learning_*.md' 2>/dev/null | wc -l)
        ERR_COUNT=$(find "$MEM_DIR" -maxdepth 1 -name 'error_*.md' 2>/dev/null | wc -l)
        done_step "Memory loaded" "(${NOTE_COUNT} notes • ${LEARN_COUNT} learnings, ${ERR_COUNT} errors)"

        # 3. Session state
        show_step "Loading session state"
        if [ -f "$MEM_DIR/session-state.md" ]; then
            LAST_DATE=$(grep "^\- \*\*Datum\*\*:" "$MEM_DIR/session-state.md" 2>/dev/null | head -1 | sed 's/.*: //')
            done_step "Session state" "(last: ${LAST_DATE})"
        else
            done_step "Session state" "(no previous session)"
        fi
    fi

    # 4. Git status
    show_step "Checking git status"
    if [ -d .git ]; then
        BRANCH=$(git branch --show-current 2>/dev/null)
        CHANGES=$(git status --porcelain 2>/dev/null | wc -l)
        if [ "$CHANGES" -gt 0 ]; then
            done_step "Git: ${Y}${BRANCH}${R}" "${Y}${CHANGES} uncommitted changes${R}"
        else
            done_step "Git: ${G}${BRANCH}${R}" "clean"
        fi
    else
        done_step "Git" "(not a git repo)"
    fi

    # 5. Hooks — taky jen Claude Code, hooky jsou jeho settings.json
    if [ "$AGENT_BRAIN" = "1" ]; then
        show_step "Initializing hooks"
        HOOK_COUNT=0
        [ -f "$CLAUDE_DIR/settings.json" ] && [ -n "$PY" ] && HOOK_COUNT=$("$PY" -c "
import json, sys
try:
    d = json.load(open(sys.argv[1]))
    print(sum(len(v) for v in d.get('hooks', {}).values()))
except Exception:
    print(0)
" "$CLAUDE_DIR/settings.json" 2>/dev/null || echo 0)
        done_step "Hooks ready" "(${HOOK_COUNT} active)"
    fi

    echo ""
    echo -e "  ${D}──────────────────────────────────────${R}"
    echo -e "  ${A}✦${R} ${W}ready${R}"
    echo ""
    sleep 0.3
}

# ── Missing CLI → tell the user instead of failing silently ──
if ! command -v "$AGENT_BIN" >/dev/null 2>&1; then
    echo ""
    echo -e "  ${Y}⚠ ${AGENT_LABEL} ('${AGENT_BIN}') nenalezeno v PATH${R}"
    [ -n "$AGENT_INSTALL" ] && echo -e "  ${D}Nainstaluj: ${AGENT_INSTALL}${R}"
    [ -n "$AGENT_AUTH" ] && echo -e "  ${D}Pak přihlášení: ${AGENT_AUTH}${R}"
    echo -e "  ${D}Nebo v Hubu: ⚙ → AI agenti → Nainstalovat${R}"
    echo ""
    exec bash
fi

# ── Po násilném konci ──
# Kód 137 = SIGKILL. Nejčastěji ho pošle jádro, když dojde paměť — na serveru
# má prostor strop na všechny taby dohromady. Dřív tab prostě zešedl a nikdo
# nevěděl proč; teď se to napíše (hub z toho udělá hlášku ve čtení, viz
# composer.js KILLED) a Claude Code v konverzaci pokračuje.
#
# Argumenty pro pokračování: --session-id X → --resume X a úvodní prompt se
# podruhé neposílá. Kopie konverzace (--fork-session) se neobnovuje — nové id
# si zvolil Claude Code sám a wrapper ho nezná.
resume_args() {
    RESUME=()
    local have="" i=0 a
    local args=("$@")
    while [ "$i" -lt "${#args[@]}" ]; do
        a="${args[$i]}"
        case "$a" in
            --session-id|--resume)
                have="${args[$((i + 1))]}"
                RESUME+=(--resume "$have")
                i=$((i + 2)); continue ;;
            --fork-session) return 1 ;;
            --model|--append-system-prompt)
                RESUME+=("$a" "${args[$((i + 1))]}")
                i=$((i + 2)); continue ;;
            --*) RESUME+=("$a") ;;
            *) ;;                       # úvodní prompt
        esac
        i=$((i + 1))
    done
    [ -n "$have" ]
}

# ── Odhlášený Claude Code ──
# Claude Code, kterému se nepovede obnovit přihlášení, vyprázdní v
# ~/.claude/.credentials.json accessToken i refreshToken. Tab pak naběhne, ale
# na první zprávu odpoví jen „OAuth session expired“ a /login z pole na psaní
# se v Aladinu nedá dokončit — na PC se to opakovalo po aktualizacích (8. 11.
# 2026, všechny 3 počítače). Tady se to pozná dřív: když v souboru přihlášení
# není ani jeden token a přihlášení nejde z prostředí, spustí se rovnou
# `claude auth login` (otevře prohlížeč, stačí kliknout Authorize).
claude_odhlaseny() {
    [ "$AGENT_ID" = "claude" ] || return 1
    [ -n "${CLAUDE_CODE_OAUTH_TOKEN:-}${ANTHROPIC_API_KEY:-}${ANTHROPIC_AUTH_TOKEN:-}" ] && return 1
    [ -n "$PY" ] && [ -f "$CLAUDE_DIR/.credentials.json" ] || return 1
    "$PY" - "$CLAUDE_DIR/.credentials.json" <<'PYEOF' 2>/dev/null
import json, sys
try:
    o = json.load(open(sys.argv[1], encoding="utf-8")).get("claudeAiOauth")
except Exception:
    sys.exit(1)
sys.exit(0 if isinstance(o, dict) and not o.get("accessToken") and not o.get("refreshToken") else 1)
PYEOF
}

if claude_odhlaseny; then
    echo ""
    echo -e "  ${Y}⚠ Claude Code na tomhle počítači není přihlášený${R}"
    echo -e "  ${D}Otevírám přihlášení v prohlížeči — klikni na Authorize.${R}"
    echo ""
    "$AGENT_BIN" auth login --claudeai
    echo ""
fi

# ── Main Loop ──
FIRST_RUN=true
KILLS=0
ARGS=("$@")

while true; do
    if $FIRST_RUN; then
        boot_sequence
        FIRST_RUN=false
    fi

    STARTED=$SECONDS
    "$AGENT_BIN" "${ARGS[@]}"
    EXIT_CODE=$?

    if [ $EXIT_CODE -eq 137 ] && [ "$AGENT_ID" = "claude" ]; then
        KILLS=$((KILLS + 1))
        echo ""
        echo -e "  ${Y}⚠ Claude Code byl násilně ukončen${R} ${D}(SIGKILL)${R}"
        echo -e "  ${D}Nejčastěji došla paměť — na serveru má prostor strop pro všechny taby${R}"
        echo -e "  ${D}dohromady. Pomůže zavřít taby, které zrovna nepotřebuješ.${R}"
        # Pokračuje se jen po delším běhu a nejvýš třikrát: když ho jádro
        # zabije hned po startu, dokola by se to jen točilo.
        if [ "$KILLS" -le 3 ] && [ $((SECONDS - STARTED)) -ge 20 ] && resume_args "${ARGS[@]}"; then
            echo -e "  ${A}✦${R} ${W}konverzace je uložená, pokračuju v ní…${R}"
            echo ""
            ARGS=("${RESUME[@]}")
            sleep 2
            continue
        fi
        echo -e "  ${D}Konverzaci otevřeš znovu v seznamu Konverzace.${R}"
        echo ""
        break
    fi

    # Exit code 130 = SIGINT (ctrl+c)
    if [ $EXIT_CODE -eq 130 ] || [ $EXIT_CODE -eq 2 ]; then
        echo ""
        echo -e "  ${Y}⚠ interrupted${R}"
        echo -e "  ${D}restarting in 2s... ctrl+c again to quit${R}"
        echo ""

        trap "echo '  Bye!'; exit 0" INT
        sleep 2
        trap - INT

        echo -e "  ${A}✦${R} ${W}restarting...${R}"
        echo ""
        continue
    fi

    break
done
