#!/usr/bin/env python3
"""Basketball quarter-results collector.

  python run.py probe      fetch ~10 pages and save them to probe/ (first check)
  python run.py discover   find competitions and test each for quarter scores
  python run.py harvest    collect schedules and games for leagues with quarter scores
  python run.py update     scheduled run: new seasons, untested leagues, then harvest
  python run.py validate   re-check all games; write exceptions.csv and summary.json
  python run.py export     --quarters (CSV, four rows per game) and/or --sheets
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
        cfg["harvest"]["cache_match_pages"] = False
    cfg["seasons"] = [str(s) for s in cfg["seasons"]]
    return cfg


def status(store, cfg):
    delay = float(cfg["pace"]["delay_seconds"])
    counts = {}
    for league in store.leagues.values():
        counts[league["has_quarters"]] = counts.get(league["has_quarters"], 0) + 1
    print(f"Competitions found: {len(store.competitions)}")
    print("League-seasons: " + (", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "none yet"))
    open_total = 0
    for name in store.season_names():
        season = store.season(name)
        tally = {}
        for row in season.schedule.values():
            tally[row["status"]] = tally.get(row["status"], 0) + 1
        open_now = sum(1 for r in season.schedule.values() if harvest.is_due(r, cfg))
        open_total += open_now
        print(f"  {name}: {len(season.games)} games collected; schedule "
              + (", ".join(f"{k}={v}" for k, v in sorted(tally.items())) or "empty")
              + f"; ready to fetch now: {open_now}")
    unread = sum(1 for l in store.leagues.values() if harvest.wanted(l) and not l["last_harvested"])
    print(f"Leagues with quarter scores not yet harvested: {unread}")
    if open_total:
        print(f"Games ready to fetch: {open_total}, about {open_total * (delay + 1) / 3600:.1f} hours "
              f"at one request every {delay:g}s.")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["probe", "discover", "harvest", "update", "validate",
                                            "export", "status", "reparse"])
    parser.add_argument("--config", default=str(ROOT / "config.yaml"))
    parser.add_argument("--max-minutes", type=float, default=0, help="stop cleanly after this long")
    parser.add_argument("--max-requests", type=int, default=0, help="stop cleanly after this many requests")
    parser.add_argument("--league", default="", help="harvest only this competition (its address name)")
    parser.add_argument("--quarters", action="store_true", help="export: write the four-rows-per-game CSV")
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
        if not (args.quarters or args.sheets):
            parser.error("export needs --quarters and/or --sheets")
        if args.quarters:
            export.write_quarters(store, ROOT, log)
        if args.sheets:
            export.push_sheets(store, cfg, ROOT, force=args.force, log=log)
        return 0

    fetcher = Fetcher(cfg, ROOT, max_minutes=args.max_minutes, max_requests=args.max_requests, log=log)
    if args.command == "reparse":
        harvest.reparse(fetcher, store, log)
        validate.validate(store, log)
        return 0
    if args.command in ("update", "harvest") and not store.leagues:
        log("No league list yet. Run 'python run.py discover' first.")
        return 0

    started, outcome, code = st.stamp(), "completed", 0
    log(f"Identifying as: {fetcher.user_agent}")
    try:
        if args.command == "probe":
            discover.probe(fetcher, cfg, ROOT, log)
        elif args.command == "discover":
            discover.discover(fetcher, store, cfg, log)
        elif args.command == "harvest":
            harvest.harvest(fetcher, store, cfg, log, only=args.league or None)
        elif args.command == "update":
            discover.refresh(fetcher, store, cfg, log)
            harvest.harvest(fetcher, store, cfg, log, only=args.league or None)
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
