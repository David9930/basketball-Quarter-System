"""Checks on collected games. Problems are flagged for review, never auto-corrected."""
from __future__ import annotations

import collections
import json

from . import store as st

EXCEPTION_FIELDS = ["game_id", "date", "competition", "season", "home_team", "away_team",
                    "check", "detail", "source_url"]

FLAGS = {
    "PERIOD_SUM_MISMATCH": "quarters plus overtime do not add up to the final score",
    "LIST_SCORE_MISMATCH": "the match report's final differs from the daily list's final",
    "OT_WITHOUT_TIE": "overtime is listed but regulation was not tied",
    "TIED_FINAL": "final score is level (possible in two-leg ties; otherwise suspect)",
    "SUSPECT_VALUE": "an unusual score: a quarter above 60, a 0-0 quarter, or a final under 20",
    "BAD_VALUE": "a score is missing or not a whole number",
}


def _ints(game, keys):
    return [int(game[k]) for k in keys]


def check_game(game, listed=None):
    """Return (flags, detail) for one game row. `listed` is its schedule row."""
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
    if listed and listed.get("list_home", "").isdigit() and listed.get("list_away", "").isdigit():
        if (int(listed["list_home"]), int(listed["list_away"])) != (h_final, a_final):
            flags.append("LIST_SCORE_MISMATCH")
            notes.append(f"daily list shows {listed['list_home']}-{listed['list_away']}")
    if ot_periods and sum(hq) != sum(aq):
        flags.append("OT_WITHOUT_TIE")
        notes.append(f"regulation {sum(hq)}-{sum(aq)}")
    if h_final == a_final:
        flags.append("TIED_FINAL")
    if (max(hq + aq) > 60 or any(h == 0 and a == 0 for h, a in zip(hq, aq))
            or min(h_final, a_final) < 20):
        flags.append("SUSPECT_VALUE")
    return flags, "; ".join(notes)


def check_text(game, listed=None):
    flags, _ = check_game(game, listed)
    return ";".join(flags) if flags else "OK"


def league_totals(store):
    """Recount each league's listed, played and collected games from the schedule."""
    tally = {}
    for row in store.schedule.values():
        t = tally.setdefault((row["comp_id"], row["season"]), {"listed": 0, "played": 0, "done": 0, "dates": []})
        t["listed"] += 1
        if row["list_status"] == st.PLAYED:
            t["played"] += 1
            t["dates"].append(row["date"])
        if row["status"] == st.DONE:
            t["done"] += 1
    for key, league in store.leagues.items():
        t = tally.get(key, {"listed": 0, "played": 0, "done": 0, "dates": []})
        league["games_listed"], league["games_played"] = str(t["listed"]), str(t["played"])
        league["games_collected"] = str(t["done"])
        league["first_date"] = min(t["dates"]) if t["dates"] else ""
        league["last_date"] = max(t["dates"]) if t["dates"] else ""


def validate(store, log=print):
    """Re-check every game; write data/exceptions.csv and data/summary.json."""
    exceptions = []
    flagged = collections.Counter()
    seasons = {}
    for game in store.games.values():
        listed = store.schedule.get(game["game_id"])
        flags, detail = check_game(game, listed)
        text = ";".join(flags) if flags else "OK"
        if game["check"] != text:
            game["check"] = text
            store.touch("games", game["date"])
        s = seasons.setdefault(game["season"], {"games": 0, "overtime_games": 0, "flagged": 0,
                                                "first_game": game["date"], "last_game": game["date"]})
        s["games"] += 1
        s["overtime_games"] += int(game["overtime_periods"] or 0) > 0
        s["first_game"], s["last_game"] = min(s["first_game"], game["date"]), max(s["last_game"], game["date"])
        for flag in flags:
            flagged[flag] += 1
        if flags:
            s["flagged"] += 1
            exceptions.append({**{k: game.get(k, "") for k in EXCEPTION_FIELDS}, "check": text, "detail": detail})
    requeued = 0
    for gid, row in store.schedule.items():
        if row["status"] == st.DONE and gid not in store.games:      # mark without a record
            row["status"] = st.PENDING
            store.touch("schedule", row["date"])
            requeued += 1
    league_totals(store)
    def bucket(row):
        if row["list_status"] != st.PLAYED:
            return f"not played ({row['list_status']})"
        league = store.leagues.get((row["comp_id"], row["season"]), {})
        if row["status"] == st.PENDING and league.get("has_quarters") != "yes":
            return "league untested" if league.get("has_quarters") == "unknown" else "league without quarter scores"
        if row["status"] == st.PENDING and league.get("include", "").strip().lower() == "no":
            return "league excluded"
        return row["status"]

    statuses = collections.Counter(bucket(r) for r in store.schedule.values())
    leagues = collections.Counter(r["has_quarters"] or "unknown" for r in store.leagues.values())
    summary = {
        "generated": st.stamp(),
        "days_read": len(store.days),
        "first_day": min(store.days) if store.days else None,
        "last_day": max(store.days) if store.days else None,
        "empty_days": sorted(d for d, r in store.days.items() if r["games"] in ("", "0")),
        "league_seasons": dict(sorted(leagues.items())),
        "games_listed": len(store.schedule),
        "schedule_status": dict(sorted(statuses.items())),
        "games_collected": len(store.games),
        "quarter_records": len(store.games) * 4,
        "games_flagged": len(exceptions),
        "flags": dict(sorted(flagged.items())),
        "requeued_missing_records": requeued,
        "seasons": dict(sorted(seasons.items())),
        "flag_meanings": FLAGS,
    }
    exceptions.sort(key=lambda r: (r["date"], r["game_id"]))
    st.write_csv(store.dir / "exceptions.csv", EXCEPTION_FIELDS, exceptions)
    (store.dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    store.save()
    log(f"Validated {len(store.games)} games; {len(exceptions)} flagged for review (see data/exceptions.csv).")
    return summary
