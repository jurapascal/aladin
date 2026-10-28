"""
Statistiky používání Claude Code — z toho, co si sám ukládá na disk.

Zdroje jsou dva a chovají se úplně jinak:

* `~/.claude/history.jsonl` — každý odeslaný prompt s časem a projektem.
  Pár megabajtů, přečte se za zlomek sekundy.
* `~/.claude/projects/<slug>/*.jsonl` — přepisy sezení, a v nich u každé
  odpovědi `message.usage` s tokeny. Skoro gigabajt, projít to celé trvá
  půl minuty.

Proto se to počítá **přírůstkově**: u každého souboru si pamatujeme velikost
a čas změny a znovu čteme jen to, co přibylo nebo se změnilo. Hotové součty
leží v `~/.claude/hub-stats.json`. Řádky, které nemají v textu `"usage"`, se
ani neparsují — to samo ušetří většinu práce.
"""
import json
import os
import time
from collections import Counter, defaultdict

from . import core, pricing

CACHE_PATH = os.path.join(core.CLAUDE_DIR, "hub-stats.json")
HISTORY_PATH = os.path.join(core.CLAUDE_DIR, "history.jsonl")
PROJECTS_ROOT = os.path.join(core.CLAUDE_DIR, "projects")
CACHE_VERSION = 4          # 2: tokeny podle modelu (cena), 3: cena podle dne, 4: odpověď se počítá jednou
DAYS_KEPT = 120


def _empty():
    return {"in": 0, "out": 0, "cache_w": 0, "cache_r": 0, "think": 0, "answers": 0}


def _add(into, other):
    for key in into:
        into[key] += other.get(key, 0)


def _scan_file(path):
    """Tokeny a dny z jednoho přepisu sezení."""
    totals = _empty()
    models = {}
    days = Counter()
    dcost = Counter()
    first = last = 0
    # Claude Code zapíše jednu odpověď víckrát — po jednom řádku na každý blok
    # (myšlení, každé volání nástroje) — a všechny nesou tytéž `usage`. Počítat
    # je každý zvlášť zdvojnásobovalo i ztrojnásobovalo tokeny, odpovědi i cenu,
    # proto se odpověď bere jednou (podle id zprávy a požadavku; při rozdílu
    # větší výstup).
    unikatni = {}
    poradi = []
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            # Předfiltr řetězcem: naprostá většina řádků jsou přílohy a snímky
            # souborů, a parsovat je jen proto, abychom je zahodili, je drahé.
            if '"usage"' not in line:
                continue
            try:
                entry = json.loads(line)
            except Exception:
                continue
            msg = entry.get("message") or {}
            usage = msg.get("usage")
            if not isinstance(usage, dict):
                continue
            mid = msg.get("id")
            key = (mid, entry.get("requestId")) if mid else None
            if key is not None and key in unikatni:
                old = unikatni[key]
                if (usage.get("output_tokens") or 0) > (old["usage"].get("output_tokens") or 0):
                    old["usage"] = usage
                continue
            rec = {"usage": usage, "model": msg.get("model"), "stamp": entry.get("timestamp") or ""}
            if key is not None:
                unikatni[key] = rec
            poradi.append(rec)
    for rec in poradi:
        usage = rec["usage"]
        tok = {"in": usage.get("input_tokens") or 0, "out": usage.get("output_tokens") or 0,
               "cache_w": usage.get("cache_creation_input_tokens") or 0,
               "cache_r": usage.get("cache_read_input_tokens") or 0}
        totals["answers"] += 1
        for k, v in tok.items():
            totals[k] += v
        totals["think"] += ((usage.get("output_tokens_details") or {}).get("thinking_tokens") or 0)
        # Podle modelu, ať se dá spočítat cena (ceny se u modelů liší).
        m = models.setdefault(str(rec["model"] or "?"), _empty())
        m["answers"] += 1
        for k, v in tok.items():
            m[k] += v
        price = pricing.cost(rec["model"], tok)
        stamp = rec["stamp"]
        if isinstance(stamp, str) and len(stamp) >= 10:
            days[stamp[:10]] += tok["out"]
            dcost[stamp[:10]] += price
            first = first or stamp
            last = stamp
    return {"totals": totals, "models": models, "days": dict(days),
            "dcost": {d: round(c, 4) for d, c in dcost.items()}, "first": first, "last": last}


def _load_cache():
    try:
        with open(CACHE_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
        if data.get("version") == CACHE_VERSION:
            return data
    except Exception:
        pass
    return {"version": CACHE_VERSION, "files": {}}


def _save_cache(cache):
    try:
        tmp = CACHE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(cache, fh)
        os.replace(tmp, CACHE_PATH)
    except Exception:
        pass


def slugify(path):
    """Cesta → jméno složky, jaké pro ni Claude Code používá."""
    return path.replace("\\", "/").replace(":", "").replace("/", "-")


def _slug_to_path(slug, known):
    """Ze jména složky sezení zpátky na projekt.

    Slug vznikl nahrazením oddělovačů pomlčkami, takže zpětně je nejednoznačný
    (pomlčka mohla být i v názvu). Porovnáváme proto se skutečnými cestami
    projektů — a když nic nesedí, vrátíme aspoň poslední kus.
    """
    for path in known:
        if slug == slugify(path):
            return path
    # Nic nesedělo — zkusíme slug rozbalit zpátky. U jmen s pomlčkou to nemusí
    # vyjít, ale pro běžné cesty (domovská složka) je to přesně ono.
    guess = "/" + slug.lstrip("-").replace("-", "/")
    return guess if os.path.isdir(guess) else ""


def _history():
    """Prompty: kolik, kdy během dne, ve kterých dnech a projektech."""
    hours = Counter()
    days = Counter()
    projects = Counter()
    weekdays = Counter()
    total = 0
    try:
        with open(HISTORY_PATH, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    entry = json.loads(line)
                except Exception:
                    continue
                stamp = entry.get("timestamp")
                if not stamp:
                    continue
                total += 1
                when = time.localtime(stamp / 1000)
                hours[when.tm_hour] += 1
                days[time.strftime("%Y-%m-%d", when)] += 1
                weekdays[when.tm_wday] += 1
                project = entry.get("project") or ""
                if project:
                    projects[slugify(project)] += 1
    except OSError:
        pass
    return {"prompts": total, "hours": hours, "days": days,
            "projects": projects, "weekdays": weekdays}


GITHUB_CACHE = os.path.join(core.CLAUDE_DIR, "hub-github.json")
GITHUB_TTL = 3600      # hodina stačí; commity nepřibývají po sekundách


def github(force=False):
    """Přehled z GitHubu přes `gh`. Bez přihlášeného gh vrací prázdno.

    Jeden dotaz na GraphQL vytáhne i kalendář příspěvků po dnech, takže se
    nemusí chodit pro každý repozitář zvlášť. Výsledek se hodinu drží, aby
    otevření statistik neznamenalo pokaždé volání po síti.
    """
    if not force:
        try:
            with open(GITHUB_CACHE, encoding="utf-8") as fh:
                cached = json.load(fh)
            if time.time() - cached.get("fetched", 0) < GITHUB_TTL:
                return cached
        except Exception:
            pass

    import shutil
    import subprocess
    if not shutil.which("gh"):
        return {"ok": False, "detail": "GitHub CLI (gh) není nainstalované."}
    query = """
    query {
      viewer {
        login
        repositories(privacy: PRIVATE) { totalCount }
        contributionsCollection {
          totalCommitContributions
          totalRepositoriesWithContributedCommits
          contributionCalendar {
            totalContributions
            weeks { contributionDays { date contributionCount } }
          }
        }
      }
    }"""
    try:
        r = subprocess.run(["gh", "api", "graphql", "-f", f"query={query}"],
                           capture_output=True, text=True, timeout=25)
        if r.returncode != 0:
            return {"ok": False,
                    "detail": (r.stderr or "gh selhalo").strip()[:200]}
        viewer = json.loads(r.stdout)["data"]["viewer"]
    except Exception as exc:
        return {"ok": False, "detail": str(exc)[:200]}

    contrib = viewer["contributionsCollection"]
    calendar = contrib["contributionCalendar"]
    days = [d for week in calendar["weeks"] for d in week["contributionDays"]]
    result = {
        "ok": True,
        "fetched": int(time.time()),
        "login": viewer["login"],
        "private_repos": viewer["repositories"]["totalCount"],
        "commits_year": contrib["totalCommitContributions"],
        "repos_touched": contrib["totalRepositoriesWithContributedCommits"],
        "contributions": calendar["totalContributions"],
        "days": [{"day": d["date"], "count": d["contributionCount"]}
                 for d in days],
    }
    try:
        with open(GITHUB_CACHE, "w", encoding="utf-8") as fh:
            json.dump(result, fh)
    except Exception:
        pass
    return result


def _file_cost(cached):
    """Odhad ceny jednoho přepisu podle modelů (pricing.py)."""
    return sum(pricing.cost(m, u) for m, u in (cached.get("models") or {}).items())


def usage_by_slug(files):
    """Součty tokenů a ceny podle složky sezení z hotové mezipaměti."""
    out = defaultdict(lambda: {**_empty(), "cost": 0.0, "sessions": 0, "last": ""})
    for cached in files.values():
        slug = cached.get("slug")
        if not slug:
            continue
        u = out[slug]
        t = cached.get("totals") or {}
        for key in ("in", "out", "cache_w", "cache_r", "think", "answers"):
            u[key] += t.get(key, 0)
        u["cost"] += _file_cost(cached)
        u["sessions"] += 1
        if (cached.get("last") or "") > u["last"]:
            u["last"] = cached.get("last") or ""
    return out


def project_usage():
    """Využití tokenů a cena u každého projektu — jen z mezipaměti, bez čtení
    přepisů (vteřiny místo půl minuty). `pending` = mezipaměť je stará nebo
    prázdná, je potřeba přepočítat (collect)."""
    cache = _load_cache()
    files = cache.get("files") or {}
    pending = (not files) or any("models" not in f for f in files.values())
    known = [p["path"] for p in core.get_projects()]
    result = {}
    for slug, u in usage_by_slug(files).items():
        path = _slug_to_path(slug, known)
        if not path:
            continue
        result[path] = {"in": u["in"], "out": u["out"], "cache_w": u["cache_w"],
                        "cache_r": u["cache_r"], "answers": u["answers"],
                        "cost": round(u["cost"], 2), "sessions": u["sessions"], "last": u["last"]}
    return {"pending": pending, "projects": result,
            "note": "Cena je odhad podle ceníku API; na předplatném se po tokenech neplatí."}


def project_detail(path):
    """Statistiky jednoho projektu z mezipaměti: součty, modely, dny, nejdražší
    sezení a počet zpráv z historie."""
    cache = _load_cache()
    slug = slugify(os.path.abspath(os.path.expanduser(path or "")))
    mine = {k: f for k, f in (cache.get("files") or {}).items() if f.get("slug") == slug}
    total = _empty()
    cost = 0.0
    models = defaultdict(lambda: {**_empty(), "cost": 0.0})
    days = defaultdict(lambda: {"out": 0, "cost": 0.0})
    sessions = []
    first = last = ""
    for key, f in mine.items():
        t = f.get("totals") or {}
        for k in total:
            total[k] += t.get(k, 0)
        c = _file_cost(f)
        cost += c
        for model, u in (f.get("models") or {}).items():
            m = models[model]
            for k in _empty():
                m[k] += u.get(k, 0)
            m["cost"] += pricing.cost(model, u)
        for d, out in (f.get("days") or {}).items():
            days[d]["out"] += out
        for d, cc in (f.get("dcost") or {}).items():
            days[d]["cost"] += cc
        if f.get("first") and (not first or f["first"] < first):
            first = f["first"]
        if (f.get("last") or "") > last:
            last = f.get("last") or ""
        sessions.append({"id": os.path.basename(key)[:-6], "first": f.get("first") or "",
                         "last": f.get("last") or "", "out": t.get("out", 0),
                         "answers": t.get("answers", 0), "cost": round(c, 2)})
    sessions.sort(key=lambda s: -s["cost"])
    hist = _history()
    day_list = [{"day": d, "out": v["out"], "cost": round(v["cost"], 2)} for d, v in sorted(days.items())][-60:]
    return {
        "path": path, "name": os.path.basename(str(path).rstrip("/\\")) or str(path),
        "tokens": total, "cost": round(cost, 2),
        "models": [{"model": m, "cost": round(v["cost"], 2), "out": v["out"], "answers": v["answers"]}
                   for m, v in sorted(models.items(), key=lambda x: -x[1]["cost"])
                   if v["answers"] and not m.startswith("<")],
        "days": day_list, "active_days": len(days), "sessions_count": len(sessions),
        "sessions": sessions[:8], "first": first, "last": last,
        "prompts": hist["projects"].get(slug, 0),
        "pending": bool(mine) and any("dcost" not in f for f in mine.values()),
        "note": "Cena je odhad podle ceníku API; na předplatném se po tokenech neplatí.",
    }


def collect(progress=None):
    """Spočítá statistiky. Vrací hotový slovník pro UI."""
    cache = _load_cache()
    files = cache["files"]
    known = [p["path"] for p in core.get_projects()]

    per_project = defaultdict(_empty)
    cost_by_slug = defaultdict(float)
    cost_by_model = defaultdict(float)
    grand = _empty()
    token_days = Counter()
    sessions = 0
    changed = 0

    entries = []
    try:
        for slug in os.listdir(PROJECTS_ROOT):
            folder = os.path.join(PROJECTS_ROOT, slug)
            if not os.path.isdir(folder):
                continue
            for name in os.listdir(folder):
                if name.endswith(".jsonl"):
                    entries.append((slug, os.path.join(folder, name)))
    except OSError:
        pass

    for index, (slug, path) in enumerate(entries):
        try:
            stat = os.stat(path)
        except OSError:
            continue
        key = path
        cached = files.get(key)
        fresh = (cached and cached.get("size") == stat.st_size
                 and cached.get("mtime") == int(stat.st_mtime))
        if not fresh:
            changed += 1
            if progress:
                progress(f"čtu sezení {index + 1}/{len(entries)}…")
            try:
                result = _scan_file(path)
            except OSError:
                continue
            cached = {"size": stat.st_size, "mtime": int(stat.st_mtime),
                      "slug": slug, **result}
            files[key] = cached

        sessions += 1
        _add(grand, cached["totals"])
        _add(per_project[slug], cached["totals"])
        cost_by_slug[slug] += _file_cost(cached)
        for model, u in (cached.get("models") or {}).items():
            cost_by_model[model] += pricing.cost(model, u)
        for day, out in (cached.get("days") or {}).items():
            token_days[day] += out

    # Soubory, které mezitím zmizely, ať v mezipaměti nestraší
    for gone in set(files) - {p for _, p in entries}:
        files.pop(gone, None)
    _save_cache(cache)

    hist = _history()

    projects = []
    for slug, totals in per_project.items():
        path = _slug_to_path(slug, known)
        name = os.path.basename(path.rstrip("/\\")) if path else slug.strip("-")
        projects.append({
            "name": name or slug, "path": path,
            "out": totals["out"], "answers": totals["answers"],
            "in": totals["in"], "cache_w": totals["cache_w"], "cache_r": totals["cache_r"],
            "cost": round(cost_by_slug[slug], 2),
            "prompts": hist["projects"].get(slug, 0),
        })
    projects.sort(key=lambda p: -p["out"])

    today = time.strftime("%Y-%m-%d")
    recent_days = sorted(set(list(token_days) + list(hist["days"])))[-DAYS_KEPT:]

    return {
        "tokens": grand,
        "cost": round(sum(cost_by_slug.values()), 2),
        "cost_by_model": {m: round(c, 2) for m, c in sorted(cost_by_model.items(), key=lambda x: -x[1]) if c >= 0.01},
        "sessions": sessions,
        "prompts": hist["prompts"],
        "active_days": len(hist["days"]),
        "hours": [hist["hours"].get(h, 0) for h in range(24)],
        "weekdays": [hist["weekdays"].get(d, 0) for d in range(7)],
        "days": [{"day": d, "prompts": hist["days"].get(d, 0),
                  "out": token_days.get(d, 0)} for d in recent_days],
        "projects": projects[:12],
        "today": today,
        "rescanned": changed,
        "generated": int(time.time()),
        "github": github(),
    }
