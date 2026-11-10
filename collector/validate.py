"""Checks on collected games. Problems are flagged for review, never auto-corrected."""
from __future__ import annotations

import collections
import json

from . import store as st

EXCEPTION_FIELDS = ["game_id", "date", "competition", "season", "home_team", "away_team",
                    "check", "detail", "source_url"]

# What each flag means (also written into the README).
FLAGS = {
    "PERIOD_SUM_MISMATCH": "quarters plus overtime do not add up to the final score",
    "OT_WITHOUT_TIE": "overtime is listed but regulation was not tied",
    "TIED_FINAL": "final score is level (possible in two-leg ties; otherwise suspect)",
    "SUSPECT_VALUE": "an unusual score: a quarter above 60, a 0-0 quarter, or a final under 20",
    "BAD_VALUE": "a score is missing or not a whole number",
}


def _ints(game, keys):
    return [int(game[k]) for k in keys]


def check_game(game):
    """Return (flags, detail) for one game row."""
    try:
        hq = _ints(game, [f"home_q{i}" for i in range(1, 5)])
        aq = _ints(game, [f"away_q{i}" for i in range(1, 5)])
        h_ot, a_ot = int(game["home_ot"] or 0), int(game["away_ot"] or 0)
        h_final, a_final = int(game["home_final"]), int(game["away_final"])
        ot_periods = int(game["overtime_periods"] or 0)
    except (KeyError, TypeError, ValueError):
        return ["BAD_VALUE"], "a score could not be read as a number"
    flags, notes = [], []
    h_gap, a_gap = h_final - sum(hq) - h_ot, a_final - sum(aq) - a_ot
    if h_gap or a_gap:
        flags.append("PERIOD_SUM_MISMATCH")
        notes.append(f"unlisted points home {h_gap:+d}, away {a_gap:+d}")
    if ot_periods and sum(hq) != sum(aq):
        flags.append("OT_WITHOUT_TIE")
        notes.append(f"regulation {sum(hq)}-{sum(aq)}")
    if h_final == a_final:
        flags.append("TIED_FINAL")
    if (max(hq + aq) > 60 or any(h == 0 and a == 0 for h, a in zip(hq, aq))
            or min(h_final, a_final) < 20):
        flags.append("SUSPECT_VALUE")
    return flags, "; ".join(notes)


def check_text(game):
    flags, _ = check_game(game)
    return ";".join(flags) if flags else "OK"


def validate(store, log=print):
    """Re-check every game, write data/exceptions.csv and data/summary.json."""
    exceptions, seasons = [], {}
    for name in store.season_names():
        season = store.season(name)
        statuses = collections.Counter(r["status"] for r in season.schedule.values())
        flagged = collections.Counter()
        by_competition = collections.Counter()
        for game in season.games.values():
            by_competition[game["competition"]] += 1
            flags, detail = check_game(game)
            text = ";".join(flags) if flags else "OK"
            if game["check"] != text:
                game["check"] = text
                season.dirty = True
            for flag in flags:
                flagged[flag] += 1
            if flags:
                exceptions.append({**{k: game.get(k, "") for k in EXCEPTION_FIELDS},
                                   "season": game["season"], "check": text, "detail": detail})
        missing = [gid for gid, r in season.schedule.items()
                   if r["status"] == st.DONE and gid not in season.games]
        orphans = [gid for gid in season.games if gid not in season.schedule]
        for gid in missing:                       # collected mark without a record: collect again
            season.schedule[gid]["status"] = st.PENDING
            season.dirty = True
        dates = sorted(g["date"] for g in season.games.values())
        seasons[name] = {
            "leagues_with_games": len(by_competition),
            "games_listed": len(season.schedule),
            "games_collected": len(season.games),
            "quarter_records": len(season.games) * 4,
            "first_game": dates[0] if dates else None,
            "last_game": dates[-1] if dates else None,
            "overtime_games": sum(1 for g in season.games.values() if int(g["overtime_periods"] or 0) > 0),
            "schedule_status": dict(sorted(statuses.items())),
            "flagged": dict(sorted(flagged.items())),
            "requeued_missing_records": len(missing),
            "games_without_schedule_row": len(orphans),
        }
    leagues = collections.Counter(r["has_quarters"] or "unknown" for r in store.leagues.values())
    summary = {
        "generated": st.stamp(),
        "competitions_found": len(store.competitions),
        "league_seasons": dict(sorted(leagues.items())),
        "games_collected": sum(s["games_collected"] for s in seasons.values()),
        "games_flagged": len(exceptions),
        "seasons": seasons,
        "flag_meanings": FLAGS,
    }
    exceptions.sort(key=lambda r: (r["date"], r["game_id"]))
    st.write_csv(store.dir / "exceptions.csv", EXCEPTION_FIELDS, exceptions)
    (store.dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    store.save()
    log(f"Validated {summary['games_collected']} games; {len(exceptions)} flagged for review "
        f"(see data/exceptions.csv).")
    return summary
