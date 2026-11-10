"""Work out which leagues carry quarter scores, and the small site check ("probe").

Leagues are found by reading the daily match lists (see harvest.scan_days).
Each league-season is then tested on a few finished games: if any shows a
Q1-Q4 scoreboard it is marked yes; if several show none, or show halves, no.
"""
from __future__ import annotations

import datetime
import json
from pathlib import Path

from . import parse
from . import store as st
from .fetch import PageError
from .harvest import DAILY_LIST_PATH, collect_one, is_due, scan_days
from .validate import league_totals

EVIDENCE = (st.DONE, st.NO_SCOREBOARD, st.OTHER_PERIODS, st.GAVE_UP)      # the page was read


def _spread(rows, count):
    """Pick up to `count` rows spaced evenly through the list."""
    if len(rows) <= count:
        return list(rows)
    step = (len(rows) - 1) / (count - 1) if count > 1 else 0
    return [rows[round(i * step)] for i in range(count)]


def sample_league(fetcher, store, cfg, league, rows):
    """Decide whether this league-season shows quarter scores. `rows` are its listed games."""
    wanted = int(cfg["discovery"]["sample_games"])
    played = sorted((r for r in rows if r["list_status"] == st.PLAYED), key=st.by_date)
    outcomes = [r["status"] for r in played if r["status"] in EVIDENCE][:wanted]
    for row in _spread([r for r in played if is_due(r, cfg)], wanted):
        if st.DONE in outcomes or len(outcomes) >= wanted:
            break
        status = collect_one(fetcher, store, row, league, cfg)
        if status in EVIDENCE:
            outcomes.append(status)
    league["sampled"] = st.stamp()
    kinds = ", ".join(sorted(set(outcomes)))
    untested = [r for r in played if r["status"] not in EVIDENCE]
    if st.DONE in outcomes:
        league["has_quarters"], league["note"] = "yes", ""
    elif len(outcomes) >= wanted or (outcomes and not untested):
        league["has_quarters"] = "no"
        detail = next((r["detail"] for r in played if r["status"] == st.OTHER_PERIODS and r["detail"]), "")
        league["note"] = f"{len(outcomes)} finished games tested: {kinds}" + (f" ({detail})" if detail else "")
    else:
        league["note"] = (f"only {len(outcomes)} finished games tested so far: {kinds}" if outcomes
                          else "no finished games ready to test yet")


def sample_all(fetcher, store, cfg, log=print):
    by_league = {}
    for row in store.schedule.values():
        by_league.setdefault((row["comp_id"], row["season"]), []).append(row)
    todo = [l for l in store.leagues.values()
            if l["has_quarters"] == "unknown" and l["include"].strip().lower() != "no"
            and st.hours_since(l["sampled"]) > 72]
    todo.sort(key=lambda l: (l["name"].lower(), l["season"]))
    log(f"Testing {len(todo)} league-seasons for quarter scores.")
    try:
        for n, league in enumerate(todo, 1):
            sample_league(fetcher, store, cfg, league, by_league.get((league["comp_id"], league["season"]), []))
            if n % 10 == 0:
                store.save()
                log(f"  tested {n}/{len(todo)}")
    finally:
        league_totals(store)
        store.save()
    counts = {}
    for league in store.leagues.values():
        counts[league["has_quarters"]] = counts.get(league["has_quarters"], 0) + 1
    log("League-seasons with quarter scores: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))


def discover(fetcher, store, cfg, log=print):
    """Read the daily lists, then test each league-season for quarter scores."""
    scan_days(fetcher, store, cfg, log)
    sample_all(fetcher, store, cfg, log)


# --------------------------------------------------------------------- probe

def probe(fetcher, cfg, root, log=print):
    """Fetch one day's match list and three match reports, and save them.

    A quick check that the site still accepts the collector and that the page
    readers still work. Writes probe/*.html and probe/report.json.
    """
    folder = Path(root) / "probe"
    folder.mkdir(exist_ok=True)
    report = {"started": st.stamp(), "steps": []}
    try:
        day = (st.today() - datetime.timedelta(days=3)).isoformat()
        text = fetcher.get(DAILY_LIST_PATH.format(day=day))
        (folder / "daily_list.html").write_text(text, encoding="utf-8")
        items = parse.parse_daily(text)
        leagues = {}
        for item in items:
            leagues.setdefault(item["comp_id"], item)
        statuses = {}
        for item in items:
            statuses[item["list_status"]] = statuses.get(item["list_status"], 0) + 1
        report["steps"].append({"step": "daily_list", "day": day, "bytes": len(text), "games": len(items),
                                "competitions": len(leagues), "list_statuses": statuses})
        log(f"daily list {day}: {len(items)} games in {len(leagues)} competitions")
        played = [i for i in leagues.values() if i["list_status"] == st.PLAYED]
        for n, item in enumerate(_spread(played, 3), 1):
            page = fetcher.get(item["url"])
            (folder / f"match_report_{n}.html").write_text(page, encoding="utf-8")
            result = parse.parse_scoreboard(page)
            report["steps"].append({"step": f"match_report_{n}", "competition": item["title"],
                                    "path": item["url"], "list_score": f"{item['list_home']}-{item['list_away']}",
                                    "scoreboard": result})
            log(f"match report {n} ({item['title']}): scoreboard {result['status']} {result.get('detail', '')}")
    except PageError as exc:
        report["error"] = str(exc)
        log(f"Probe page failed: {exc}")
    finally:
        report["robots_txt"] = fetcher.robots_text
        report["fetch"] = fetcher.stats()
        report["finished"] = st.stamp()
        (folder / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        log(f"Probe written to {folder} ({fetcher.requests} requests).")
    return report
