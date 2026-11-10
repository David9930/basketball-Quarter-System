"""The collector's records: plain CSV files under data/.

  data/competitions.csv              every basketball competition found
  data/leagues.csv                   one row per competition and season
  data/seasons/<season>/rounds.csv   schedule pages read, and when
  data/seasons/<season>/schedule.csv every game listed, with its status
  data/seasons/<season>/games.csv    one row per collected game

Files are written whole and swapped into place, so an interrupted run never
leaves a half-written file.
"""
from __future__ import annotations

import csv
import datetime
import os
from pathlib import Path

COMPETITION_FIELDS = ["competition", "name", "area", "url", "seasons_seen", "last_checked"]
LEAGUE_FIELDS = ["competition", "season", "name", "area", "url", "has_quarters", "include",
                 "sampled", "games_listed", "games_collected", "last_harvested", "closed", "note"]
ROUND_FIELDS = ["competition", "stage", "round_id", "week", "listed", "last_fetched"]
SCHEDULE_FIELDS = ["game_id", "date", "competition", "stage", "round_id", "week", "url",
                   "status", "attempts", "last_try", "detail"]
GAME_FIELDS = ["game_id", "date", "competition", "competition_name", "area", "season", "stage", "week",
               "home_team", "away_team", "home_team_id", "away_team_id",
               "home_q1", "away_q1", "home_q2", "away_q2", "home_q3", "away_q3", "home_q4", "away_q4",
               "overtime_periods", "home_ot", "away_ot", "ot_detail",
               "home_final", "away_final", "winner", "check", "source_url", "retrieved"]

# Schedule statuses.
PENDING, DONE, NO_SCOREBOARD, OTHER_PERIODS, ERROR, GAVE_UP = (
    "pending", "done", "no_scoreboard", "other_periods", "error", "gave_up")
OPEN_STATUSES = (PENDING, NO_SCOREBOARD, ERROR)


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
        return datetime.date.fromisoformat(text)
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


def _week_number(row):
    return int(row["week"]) if str(row["week"]).isdigit() else 0


class Season:
    """Rounds, schedule and games for one season label."""

    def __init__(self, folder):
        self.folder = folder
        self.rounds = {(r["competition"], r["round_id"], r["week"]): r
                       for r in read_csv(folder / "rounds.csv", ROUND_FIELDS)}
        self.schedule = {r["game_id"]: r for r in read_csv(folder / "schedule.csv", SCHEDULE_FIELDS)}
        self.games = {r["game_id"]: r for r in read_csv(folder / "games.csv", GAME_FIELDS)}
        self.dirty = False

    def save(self):
        if not self.dirty:
            return
        write_csv(self.folder / "rounds.csv", ROUND_FIELDS, sorted(
            self.rounds.values(), key=lambda r: (r["competition"], r["stage"], r["round_id"], _week_number(r))))
        by_date = lambda r: (r["date"], r["game_id"].zfill(12))
        write_csv(self.folder / "schedule.csv", SCHEDULE_FIELDS, sorted(self.schedule.values(), key=by_date))
        write_csv(self.folder / "games.csv", GAME_FIELDS, sorted(self.games.values(), key=by_date))
        self.dirty = False


class Store:
    def __init__(self, root):
        self.dir = Path(root) / "data"
        self.competitions = {}
        self.leagues = {}
        self._seasons = {}

    def load(self):
        self.competitions = {r["competition"]: r
                             for r in read_csv(self.dir / "competitions.csv", COMPETITION_FIELDS)}
        self.leagues = {(r["competition"], r["season"]): r
                        for r in read_csv(self.dir / "leagues.csv", LEAGUE_FIELDS)}
        seasons_dir = self.dir / "seasons"
        if seasons_dir.exists():
            for folder in sorted(p for p in seasons_dir.iterdir() if p.is_dir()):
                self._seasons[folder.name] = Season(folder)
        return self

    def season(self, label):
        name = label.replace("/", "-")
        if name not in self._seasons:
            self._seasons[name] = Season(self.dir / "seasons" / name)
        return self._seasons[name]

    def season_names(self):
        return sorted(self._seasons)

    def save(self):
        write_csv(self.dir / "competitions.csv", COMPETITION_FIELDS,
                  sorted(self.competitions.values(), key=lambda r: r["competition"]))
        write_csv(self.dir / "leagues.csv", LEAGUE_FIELDS,
                  sorted(self.leagues.values(), key=lambda r: (r["competition"], r["season"])))
        for season in self._seasons.values():
            season.save()
