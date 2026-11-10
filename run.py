#!/usr/bin/env python3
"""Basketball quarter-results collector.

  python run.py probe      fetch one day's list and three match reports into probe/
  python run.py discover   read the daily match lists; test each league for quarter scores
  python run.py harvest    fetch match reports for leagues with quarter scores
  python run.py update     scheduled run: new days, untested leagues, then harvest
  python run.py validate   re-check all games; write exceptions.csv and summary.json
  python run.py export     --csv (per-season files in exports/) and/or --sheets
  python run.py status     what has been collected and what is left
  python run.py reparse    rebuild games from match pages already saved locally
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
from pathlib import Path

import yaml

from collector import discover, export, harvest, validate
from collector import store as st
from collector.fetch import BudgetReached, Fetcher, StopRun

ROOT = Path(__file__).resolve().parent


def log(message):
    print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] {message}", flush=True)


def load_config(path):
    cfg = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    contact = os.environ.get("COLLECTOR_CONTACT", "").strip()
    if contact:
        cfg["identity"]["contact"] = contact
    if os.environ.get("COLLECTOR_NO_CACHE"):
        cfg["harvest"]["cache_pages"] = False
    cfg["seasons"] = [str(s) for s in cfg["seasons"]]
    cfg["start_date"] = str(cfg["start_date"])
    return cfg


def status(store, cfg):
    delay = float(cfg["pace"]["delay_seconds"])
    counts = {}
    for league in store.leagues.values():
        counts[league["has_quarters"]] = counts.get(league["has_quarters"], 0) + 1
    days = sorted(store.days)
    print(f"Daily lists read: {len(days)}" + (f" ({days[0]} to {days[-1]})" if days else ""))
    print("League-seasons with quarter scores: "
          + (", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "none yet"))
    print(f"Games listed: {len(store.schedule)}; games collected with quarters: {len(store.games)}")
    rows, leagues = harvest.due_rows(store, cfg)
    by_season = {}
    for row in rows:
        by_season[row["season"]] = by_season.get(row["season"], 0) + 1
    print(f"Match reports ready to fetch: {len(rows)} in {len(leagues)} league-seasons"
          + (" (" + ", ".join(f"{k}: {v}" for k, v in sorted(by_season.items())) + ")" if rows else ""))
    if rows:
        print(f"That is about {len(rows) * (delay + 1.5) / 3600:.1f} hours at one request every {delay:g}s.")
    untested = sum(1 for l in store.leagues.values() if l["has_quarters"] == "unknown")
    if untested:
        print(f"League-seasons not yet tested for quarter scores: {untested}")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["probe", "discover", "harvest", "update", "validate",
                                            "export", "status", "reparse"])
    parser.add_argument("--config", default=str(ROOT / "config.yaml"))
    parser.add_argument("--max-minutes", type=float, default=0, help="stop cleanly after this long")
    parser.add_argument("--max-requests", type=int, default=0, help="stop cleanly after this many requests")
    parser.add_argument("--league", default="", help="harvest only this league (its comp_id, or part of its name)")
    parser.add_argument("--csv", action="store_true", help="export: write per-season CSV files to exports/")
    parser.add_argument("--sheets", action="store_true", help="export: update the Google Sheet")
    parser.add_argument("--force", action="store_true", help="export: rewrite every Sheet tab")
    args = parser.parse_args(argv)

    cfg = load_config(args.config)
    store = st.Store(ROOT).load()

    if args.command == "status":
        status(store, cfg)
        return 0
    if args.command == "validate":
        validate.validate(store, log)
        return 0
    if args.command == "export":
        if not (args.csv or args.sheets):
            parser.error("export needs --csv and/or --sheets")
        if args.csv:
            export.write_csv_exports(store, ROOT, log)
        if args.sheets:
            export.push_sheets(store, cfg, ROOT, force=args.force, log=log)
        return 0

    fetcher = Fetcher(cfg, ROOT, max_minutes=args.max_minutes, max_requests=args.max_requests, log=log)
    if args.command == "reparse":
        harvest.reparse(fetcher, store, log)
        validate.validate(store, log)
        return 0
    if args.command == "harvest" and not store.leagues:
        log("No league list yet. Run 'python run.py discover' first.")
        return 0
    if args.command == "update" and not store.days:
        log("Nothing collected yet. Run 'python run.py discover' first.")
        return 0

    started, outcome, code = st.stamp(), "completed", 0
    log(f"Identifying as: {fetcher.user_agent}")
    try:
        if args.command == "probe":
            discover.probe(fetcher, cfg, ROOT, log)
        elif args.command == "discover":
            discover.discover(fetcher, store, cfg, log)
        elif args.command == "harvest":
            harvest.collect(fetcher, store, cfg, log, only=args.league or None)
        elif args.command == "update":
            discover.discover(fetcher, store, cfg, log)
            harvest.collect(fetcher, store, cfg, log, only=args.league or None)
    except BudgetReached as exc:
        outcome = f"paused: {exc}; run again to continue"
    except StopRun as exc:
        outcome, code = f"stopped: {exc}", 2
    except KeyboardInterrupt:
        outcome, code = "interrupted; progress saved, run again to continue", 130
    finally:
        if args.command != "probe":
            store.save()
    log(f"Run {outcome.rstrip('.')}. Requests made: {fetcher.requests}; reused from disk: {fetcher.cache_hits}.")
    if args.command != "probe":
        record = {"command": args.command, "started": started, "finished": st.stamp(),
                  "outcome": outcome, **fetcher.stats()}
        (store.dir / "last_run.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
    return code


if __name__ == "__main__":
    sys.exit(main())
