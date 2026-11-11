"""Checks on collected games. Problems are flagged for review, never auto-corrected."""
from __future__ import annotations

import collections
import json

from . import store as st

EXCEPTION_FIELDS = ["game_id", "date", "competition", "season", "home_team", "away_team",
                    "check", "detail", "source_url"]

FLAGS = {
    "OT_NOT_ITEMISED": "overtime was played but the site does not list all of its periods; "
                       "quarters are complete and overtime points are final minus regulation",
    "PERIOD_SUM_MISMATCH": "quarters plus overtime do not add up to the final score",
    "LIST_SCORE_MISMATCH": "the match report's final differs from the daily list's final",
    "OT_WITHOUT_TIE": "overtime is listed but regulation was not tied",
    "TIED_FINAL": "final score is level (possible in two-leg ties; otherwise suspect)",
    "SUSPECT_VALUE": "an unusual score: a quarter above 60, a 0-0 quarter, or a final under 20",
    "BAD_VALUE": "a score is missing or not a whole number",
}


def _ints(game, keys):
    return [int(game[k]) for k in keys]


def overtime_state(hq, aq, itemised, h_final, a_final, label=""):
    """Classify a game's overtime from its scoreboard.

    `itemised` is the list of (home, away) overtime periods the site shows.
    Returns (kind, home_ot, away_ot) where kind is:
      none          no overtime; quarters add up to the final
      itemised      overtime periods are listed and everything adds up
      not_itemised  regulation ended level and the final is higher, but the
                    site lists no overtime periods, or fewer than were played.
                    Overtime points are then final minus regulation.
      mismatch      the figures do not reconcile
    """
    reg_h, reg_a = sum(hq), sum(aq)
    rem_h, rem_a = h_final - reg_h, a_final - reg_a
    it_h, it_a = sum(h for h, _ in itemised), sum(a for _, a in itemised)
    if (rem_h, rem_a) == (it_h, it_a):
        return ("itemised" if itemised else "none"), it_h, it_a
    level_so_far = reg_h == reg_a and it_h == it_a
    if level_so_far and rem_h >= it_h and rem_a >= it_a:
        return "not_itemised", rem_h, rem_a
    return "mismatch", it_h, it_a


def read_ot_detail(text):
    """'11-11 12-10' -> [(11, 11), (12, 10)]"""
    pairs = []
    for part in (text or "").split():
        h, _, a = part.partition("-")
        if h.isdigit() and a.isdigit():
            pairs.append((int(h), int(a)))
    return pairs


def check_game(game, listed=None):
    """Return (flags, detail) for one game row. `listed` is its schedule row."""
    try:
        hq = _ints(game, [f"home_q{i}" for i in range(1, 5)])
        aq = _ints(game, [f"away_q{i}" for i in range(1, 5)])
        h_final, a_final = int(game["home_final"]), int(game["away_final"])
    except (KeyError, TypeError, ValueError):
        return ["BAD_VALUE"], "a score could not be read as a number"
    itemised = read_ot_detail(game.get("ot_detail", ""))
    ot_periods = len(itemised)
    kind, h_ot, a_ot = overtime_state(hq, aq, itemised, h_final, a_final)
    flags, notes = [], []
    if kind == "not_itemised":
        flags.append("OT_NOT_ITEMISED")
        notes.append(f"regulation {sum(hq)}-{sum(aq)}, overtime {h_ot}-{a_ot}"
                     + (f", of which listed {game['ot_detail']}" if itemised else ""))
    elif kind == "mismatch":
        h_gap, a_gap = h_final - sum(hq) - h_ot, a_final - sum(aq) - a_ot
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


def set_overtime(game, label=""):
    """Fill the overtime columns from the quarters, listed overtime periods and finals."""
    hq = _ints(game, [f"home_q{i}" for i in range(1, 5)])
    aq = _ints(game, [f"away_q{i}" for i in range(1, 5)])
    itemised = read_ot_detail(game.get("ot_detail", ""))
    kind, h_ot, a_ot = overtime_state(hq, aq, itemised, int(game["home_final"]), int(game["away_final"]))
    played = kind in ("itemised", "not_itemised") or bool(itemised) or "OT" in label.upper().split()
    game["overtime"] = "yes" if played else "no"
    game["overtime_periods"] = str(len(itemised))
    game["home_ot"], game["away_ot"] = str(h_ot), str(a_ot)
    game["result_note"] = "" if label.strip().lower().startswith("final") else label.strip()


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
        before = dict(game)
        try:
            set_overtime(game, game.get("result_note", "") or ("FINAL OT" if game.get("overtime") == "yes" else ""))
        except (KeyError, TypeError, ValueError):
            pass
        flags, detail = check_game(game, listed)
        text = ";".join(flags) if flags else "OK"
        game["check"] = text
        if game != before:
            store.touch("games", game["date"])
        s = seasons.setdefault(game["season"], {"games": 0, "overtime_games": 0, "flagged": 0,
                                                "first_game": game["date"], "last_game": game["date"]})
        s["games"] += 1
        s["overtime_games"] += game["overtime"] == "yes"
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
