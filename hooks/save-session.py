#!/usr/bin/env python3
"""
Auto-save session state when Claude Code exits.
Captures: recent files, git status, working directory, project context.
Works even when the user just closes the window.

Paths come from ~/.claude/hub-config.json (project_dirs, brain_dir); without a
config it falls back to sensible defaults and writes the state into ~/.claude/.
"""
import os
import json
import shutil
import subprocess
import datetime

HOME = os.path.expanduser("~")
CLAUDE_DIR = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(HOME, ".claude")
CONFIG_PATH = os.path.join(CLAUDE_DIR, "hub-config.json")

DEFAULTS = {
    "project_dirs": ["~/Desktop", "~/Projects", "~/dev", "/opt/lampp/htdocs"],
    "brain_dir": "~/Obsidian/Claude-Brain",
}


def load_config():
    cfg = dict(DEFAULTS)
    try:
        with open(CONFIG_PATH, encoding="utf-8") as fh:
            cfg.update({k: v for k, v in json.load(fh).items() if v})
    except Exception:
        pass
    return cfg


CONFIG = load_config()
PROJECT_DIRS = [p for p in (os.path.expanduser(d) for d in CONFIG["project_dirs"])
                if os.path.isdir(p)]
MEMORY_DIR = os.path.join(os.path.expanduser(CONFIG["brain_dir"]), "memory")
# Vault when it exists, otherwise keep the state next to the Claude config.
STATE_DIR = MEMORY_DIR if os.path.isdir(MEMORY_DIR) else CLAUDE_DIR
STATE_FILE = os.path.join(STATE_DIR, "session-state.md")


# Windows would flash a console window for every git call without this.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


def run(cmd, cwd=None, timeout=3):
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True,
                           cwd=cwd, timeout=timeout, creationflags=_NO_WINDOW)
        return r.stdout.strip()
    except Exception:
        return ""


def find_active_projects():
    """Git repos in the configured project folders, with their current state."""
    projects = []
    for base in PROJECT_DIRS:
        for d in sorted(os.listdir(base)):
            path = os.path.join(base, d)
            if not os.path.isdir(os.path.join(path, ".git")):
                continue
            status = run("git status --porcelain", cwd=path)
            branch = run("git branch --show-current", cwd=path)
            last_commit = run("git log -1 --format='%ar: %s'", cwd=path)

            if status or last_commit:
                projects.append({
                    "name": d,
                    "path": path,
                    "branch": branch,
                    "dirty": bool(status),
                    "changed_files": len(status.split('\n')) if status else 0,
                    "last_commit": last_commit,
                })
    return projects


def find_recent_files():
    """Files modified in the last 2 hours across the configured project folders."""
    recent = []
    for sd in PROJECT_DIRS:
        result = run(f"find '{sd}' -maxdepth 4 -type f -mmin -120 "
                     f"-not -path '*/node_modules/*' -not -path '*/.git/*' "
                     f"-not -path '*/vendor/*' -not -name '*.log' "
                     f"2>/dev/null | head -20")
        if result:
            recent.extend(result.split('\n'))

    return recent[:15]


def shorten(path):
    for base in PROJECT_DIRS:
        if path.startswith(base + "/"):
            return path[len(base) + 1:]
    return path.replace(HOME + "/", "~/")


def main():
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    projects = find_active_projects()
    recent = find_recent_files()

    # Build project table
    proj_table = "| Projekt | Branch | Stav | Poslední commit |\n"
    proj_table += "|---------|--------|------|----------------|\n"

    active_projects = []
    for p in projects:
        status = "🔴 Uncommitted" if p["dirty"] else "✅ Clean"
        if p["dirty"]:
            status += f" ({p['changed_files']} files)"
        proj_table += f"| {p['name']} | {p['branch']} | {status} | {p['last_commit']} |\n"
        if p["dirty"]:
            active_projects.append(p["name"])

    if not projects:
        proj_table += "| (žádné git projekty) | — | — | — |\n"

    # Build recent files section
    recent_section = ""
    if recent:
        recent_section = "\n## Naposledy upravené soubory\n\n"
        for f in recent:
            recent_section += f"- `{shorten(f)}`\n"

    # Determine what was likely worked on
    summary = "Neznámý kontext"
    if active_projects:
        summary = f"Práce na: {', '.join(active_projects)}"
    elif recent:
        dirs = set(os.path.basename(os.path.dirname(f)) for f in recent)
        if dirs:
            summary = f"Editováno v: {', '.join(sorted(dirs))}"

    # Read existing state to preserve manual "Další kroky" if any
    next_steps = "- (auto-detected, no manual notes)"
    if os.path.isfile(STATE_FILE):
        old = open(STATE_FILE).read()
        if "## Další kroky" in old:
            steps_section = old.split("## Další kroky")[-1].strip()
            lines = [l for l in steps_section.split("\n") if l.strip().startswith("- [")]
            if lines:
                next_steps = "\n".join(lines)

    scanned = ", ".join(PROJECT_DIRS) or "(žádná složka nenakonfigurována)"
    content = f"""# Session State

Automaticky uloženo při ukončení session.

---

## Poslední session

- **Datum**: {now}
- **Kontext**: {summary}

## Projekty ({scanned})

{proj_table}
{recent_section}
## Další kroky

{next_steps}
"""

    os.makedirs(STATE_DIR, exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        f.write(content)


def sync_vault():
    """Pošle paměť do gitu, když si to uživatel v nastavení zapnul.

    Běží jako poslední krok Stop hooku, takže musí být tichý a rychlý: když
    není co poslat nebo push nevyjde, session tím nesmí spadnout.
    """
    if not CONFIG.get("vault_autosync"):
        return
    brain = os.path.expanduser(CONFIG.get("brain_dir") or "")
    if not brain or not os.path.isdir(os.path.join(brain, ".git")):
        return
    git = shutil.which("git")
    if not git:
        return
    try:
        changed = subprocess.run([git, "status", "--porcelain"], cwd=brain,
                                 capture_output=True, text=True, timeout=30,
                                 creationflags=_NO_WINDOW)
        if not changed.stdout.strip():
            return
        stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        subprocess.run([git, "add", "-A"], cwd=brain,
                       capture_output=True, timeout=120,
                       creationflags=_NO_WINDOW)
        subprocess.run([git, "commit", "-q", "-m", f"paměť: {stamp}"], cwd=brain,
                       capture_output=True, timeout=120,
                       creationflags=_NO_WINDOW)
        subprocess.run([git, "push", "-q", "origin", "HEAD"], cwd=brain,
                       capture_output=True, timeout=180,
                       creationflags=_NO_WINDOW)
    except Exception:
        pass                      # záloha se nepovedla — session tím nekončí hůř


if __name__ == "__main__":
    main()
    sync_vault()
