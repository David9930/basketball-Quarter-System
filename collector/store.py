"""The collector's records: plain CSV files under data/.

  data/leagues.csv            one row per competition and season
  data/days.csv               which dates' match lists have been read
  data/schedule/YYYY-MM.csv   every game listed, with its collection status
  data/games/YYYY-MM.csv      one row per game with quarter scores

Games are filed by month so that finished months stop changing. Files are
written whole and swapped into place, so an interrupted run never leaves a
half-written file.
"""
from __future__ import annotations

import csv
import datetime
import os
from pathlib import Path

LEAGUE_FIELDS = ["comp_id", "season", "name", "has_quarters", "include", "games_listed", "games_played",
                 "games_collected", "first_date", "last_date", "sampled", "note", "title", "url"]
DAY_FIELDS = ["date", "fetched", "games", "open"]
SCHEDULE_FIELDS = ["game_id", "date", "kickoff", "comp_id", "season", "stage", "round_id", "url",
                   "list_status", "home_team", "away_team", "home_team_id", "away_team_id",
                   "list_home", "list_away", "status", "attempts", "last_try", "detail"]
GAME_FIELDS = ["game_id", "date", "kickoff", "comp_id", "competition", "season", "stage",
               "home_team", "away_team", "home_team_id", "away_team_id",
               "home_q1", "away_q1", "home_q2", "away_q2", "home_q3", "away_q3", "home_q4", "away_q4",
               "overtime_periods", "home_ot", "away_ot", "ot_detail",
               "home_final", "away_final", "winner", "result_note", "check", "source_url", "retrieved"]

# Collection statuses for a listed game.
PENDING, DONE, NO_SCOREBOARD, OTHER_PERIODS, ERROR, GAVE_UP = (
    "pending", "done", "no_scoreboard", "other_periods", "error", "gave_up")
OPEN_STATUSES = (PENDING, NO_SCOREBOARD, ERROR)
# The site's own word for a game that has been played.
PLAYED = "Played"
# List statuses that will not change any more.
SETTLED_LIST = (PLAYED, "Postponed", "Cancelled", "Awarded", "Retired", "Abandoned")


def now_utc():
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)


def stamp():
    return now_utc().strftime("%Y-%m-%dT%H:%M:%SZ")


def today():
    return now_utc().date()


def hours_since(text):
    """Hours since a stamp() value; a very large number if blank or unreadable."""
    if not text:
        return 1e9
    try:
        then = datetime.datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc)
    except ValueError:
        return 1e9
    return (now_utc() - then).total_seconds() / 3600


def to_date(text):
    try:
        return datetime.date.fromisoformat(str(text))
    except (TypeError, ValueError):
        return None


def read_csv(path, fields):
    if not path.exists():
        return []
    with path.open(encoding="utf-8-sig", newline="") as f:
        return [{k: (row.get(k) or "") for k in fields} for row in csv.DictReader(f)]


def write_csv(path, fields, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    os.replace(tmp, path)


def by_date(row):
    return (row["date"], row["game_id"].zfill(12))


class Store:
    def __init__(self, root):
        self.dir = Path(root) / "data"
        self.leagues = {}        # (comp_id, season) -> row
        self.days = {}           # date -> row
        self.schedule = {}       # game_id -> row
        self.games = {}          # game_id -> row
        self._dirty = {"schedule": set(), "games": set()}

    def load(self):
        self.leagues = {(r["comp_id"], r["season"]): r for r in read_csv(self.dir / "leagues.csv", LEAGUE_FIELDS)}
        self.days = {r["date"]: r for r in read_csv(self.dir / "days.csv", DAY_FIELDS)}
        for kind, fields, target in (("schedule", SCHEDULE_FIELDS, self.schedule),
                                     ("games", GAME_FIELDS, self.games)):
            folder = self.dir / kind
            if folder.exists():
                for file in sorted(folder.glob("*.csv")):
                    for row in read_csv(file, fields):
                        target[row["game_id"]] = row
        return self

    def touch(self, kind, date):
        """Note that a month's file needs rewriting."""
        self._dirty[kind].add(str(date)[:7])

    def save(self):
        write_csv(self.dir / "leagues.csv", LEAGUE_FIELDS,
                  sorted(self.leagues.values(), key=lambda r: (r["name"].lower(), r["season"], r["comp_id"])))
        write_csv(self.dir / "days.csv", DAY_FIELDS, sorted(self.days.values(), key=lambda r: r["date"]))
        for kind, fields, rows in (("schedule", SCHEDULE_FIELDS, self.schedule),
                                   ("games", GAME_FIELDS, self.games)):
            months = self._dirty[kind]
            if not months:
                continue
            grouped = {m: [] for m in months}
            for row in rows.values():
                if row["date"][:7] in grouped:
                    grouped[row["date"][:7]].append(row)
            for month, items in grouped.items():
                write_csv(self.dir / kind / f"{month}.csv", fields, sorted(items, key=by_date))
            months.clear()
