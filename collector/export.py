"""Outputs built from the collected games: a quarter-by-quarter CSV and Google Sheets."""
from __future__ import annotations

import json
import os
from pathlib import Path

from . import store as st
from .validate import EXCEPTION_FIELDS

QUARTER_FIELDS = ["game_id", "date", "competition", "competition_name", "area", "season", "stage",
                  "home_team", "away_team", "quarter", "home_points", "away_points", "total_points",
                  "home_margin", "quarter_result", "check"]
NUMERIC = {"home_q1", "away_q1", "home_q2", "away_q2", "home_q3", "away_q3", "home_q4", "away_q4",
           "overtime_periods", "home_ot", "away_ot", "home_final", "away_final", "week",
           "games_listed", "games_collected"}
CHUNK_ROWS = 5000


def quarter_rows(game):
    for q in range(1, 5):
        home, away = int(game[f"home_q{q}"]), int(game[f"away_q{q}"])
        yield {**{k: game.get(k, "") for k in QUARTER_FIELDS}, "quarter": q,
               "home_points": home, "away_points": away, "total_points": home + away,
               "home_margin": home - away,
               "quarter_result": "Home" if home > away else "Away" if away > home else "Tie"}


def write_quarters(store, root, log=print):
    """exports/quarters_<season>.csv: four rows per game."""
    folder = Path(root) / "exports"
    for name in store.season_names():
        games = sorted(store.season(name).games.values(), key=lambda g: (g["date"], g["game_id"].zfill(12)))
        if not games:
            continue
        rows = [row for game in games for row in quarter_rows(game)]
        st.write_csv(folder / f"quarters_{name}.csv", QUARTER_FIELDS, rows)
        log(f"exports/quarters_{name}.csv: {len(rows)} rows")


# -------------------------------------------------------------- Google Sheets

def _cell(field, value):
    if field in NUMERIC and str(value).lstrip("-").isdigit():
        return int(value)
    return value


def table(fields, rows):
    return [list(fields)] + [[_cell(f, row.get(f, "")) for f in fields] for row in rows]


def sheet_tabs(store):
    """(tab name, table) pairs, newest season first."""
    tabs = []
    for name in sorted(store.season_names(), reverse=True):
        games = sorted(store.season(name).games.values(), key=lambda g: (g["date"], g["game_id"].zfill(12)))
        if games:
            tabs.append((f"Games {name}", table(st.GAME_FIELDS, games)))
    leagues = sorted(store.leagues.values(), key=lambda r: (r["competition"], r["season"]))
    tabs.append(("Leagues", table(st.LEAGUE_FIELDS, leagues)))
    exceptions = st.read_csv(store.dir / "exceptions.csv", EXCEPTION_FIELDS)
    tabs.append(("Exceptions", table(EXCEPTION_FIELDS, exceptions)))
    return tabs


def _credentials(cfg, root):
    raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    if raw:
        return json.loads(raw)
    file = Path(root) / (cfg["sheets"].get("credentials_file") or "service_account.json")
    if file.exists():
        return json.loads(file.read_text(encoding="utf-8"))
    return None


def push_sheets(store, cfg, root, force=False, log=print):
    """Write the game tables to the Google Sheet. Returns False if not set up."""
    sheet_id = (os.environ.get("SHEET_ID") or cfg["sheets"].get("spreadsheet_id") or "").strip()
    info = _credentials(cfg, root)
    if not sheet_id or info is None:
        log("Google Sheets is not set up (need a spreadsheet ID and a service-account key); skipped.")
        return False
    import gspread
    from google.oauth2.service_account import Credentials

    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    client = gspread.authorize(Credentials.from_service_account_info(info, scopes=scopes))
    book = client.open_by_key(sheet_id)
    existing = {ws.title: ws for ws in book.worksheets()}
    budget = int(cfg["sheets"]["max_cells"])
    used = 0
    for title, rows in sheet_tabs(store):
        cells = len(rows) * len(rows[0])
        if used + cells > budget:
            log(f"'{title}' left out: it would pass the {budget:,}-cell limit. The CSV files hold everything.")
            continue
        used += cells
        ws = existing.get(title)
        if ws is not None and not force and title.startswith("Games ") and ws.row_count == len(rows):
            log(f"'{title}': unchanged ({len(rows) - 1} games)")
            continue
        if ws is None:
            ws = book.add_worksheet(title=title, rows=len(rows), cols=len(rows[0]))
        ws.clear()
        ws.resize(rows=max(len(rows), 2), cols=len(rows[0]))
        for start in range(0, len(rows), CHUNK_ROWS):
            ws.update(values=rows[start:start + CHUNK_ROWS], range_name=f"A{start + 1}",
                      value_input_option="RAW")
        ws.freeze(rows=1)
        log(f"'{title}': wrote {len(rows) - 1} rows")
    return True
