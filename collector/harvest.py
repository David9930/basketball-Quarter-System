"""Read the daily match lists, then the match reports for leagues with quarter scores.

Everything is incremental: a day or a game already dealt with is not fetched
again, so a run can be stopped and restarted at any point.
"""
from __future__ import annotations

import collections
import datetime
from pathlib import Path

from . import parse
from . import store as st
from .fetch import PageError, ReaderAlarm
from .validate import check_text, league_totals, set_overtime

DAILY_LIST_PATH = "/sport_ajax_v3.php?d={day}&widget_client_id=1&sp=basketball"
EXAMPLES_PER_KIND = 3
# Safeguards against the site changing its page layout (see reader_check and collect).
CHECK_GAMES = 3               # at most this many known games are re-read at the start of a run
WINDOW = 40                   # the last this-many match reports are watched...
WINDOW_BAD_SHARE = 0.6        # ...and the run stops if this share of them read as blank or wrong
WINDOW_MIN_LEAGUES = 3        # ...across at least this many leagues


# ---------------------------------------------------------------- safeguards

def reader_check(fetcher, store, log=print):
    """Re-read a game already on file and confirm the page still gives the same scores.

    Run once at the start of a run. If two known games in a row read
    differently, the site's layout has probably changed, and the run stops
    before anything is recorded. One differing game is tolerated, since the
    site does correct scores. Returns True if confirmed, None if it could not
    be checked (nothing on file, or the pages could not be fetched).
    """
    differed = tried = 0
    for game in sorted(store.games.values(), key=st.by_date, reverse=True):
        row = store.schedule.get(game["game_id"])
        if row is None:
            continue
        if tried >= CHECK_GAMES:
            break
        tried += 1
        try:
            result = parse.parse_scoreboard(fetcher.get(row["url"]))
        except PageError:
            continue
        same = (result["status"] == "ok"
                and [str(x) for x in result["home_q"]] == [game[f"home_q{i}"] for i in range(1, 5)]
                and [str(x) for x in result["away_q"]] == [game[f"away_q{i}"] for i in range(1, 5)]
                and (str(result["home_final"]), str(result["away_final"])) == (game["home_final"], game["away_final"]))
        if same:
            return True
        differed += 1
        if differed >= 2:
            raise ReaderAlarm(
                "Reader check failed: two games already on file no longer read the same from the site "
                f"(latest: game {game['game_id']}, scoreboard now reads as '{result['status']}'). "
                "The match page layout has probably changed. Nothing was recorded.")
    if tried:
        log("Reader check could not be completed this time; carrying on.")
    return None


def check_daily_list(text, items, day):
    """Stop if a day's list clearly holds games that the reader could not make sense of."""
    linked = {m.group(3) for m in parse.MATCH_RE.finditer(text.replace("\\/", "/"))}
    whole = [i for i in items if i["comp_id"] and i["home_team"] and i["away_team"] and i["list_status"]]
    if len(linked) >= 5 and len(whole) < 0.75 * len(linked):
        raise ReaderAlarm(
            f"Reader check failed: the match list for {day} links to {len(linked)} games but only "
            f"{len(whole)} could be read in full. The list layout has probably changed. "
            "That day was not recorded.")


# ---------------------------------------------------------------- daily lists

def day_is_due(row, cfg):
    """Should this date's list be read (again)?"""
    if row is None or not row["fetched"]:
        return True
    settled = row["open"] == "0" and row["games"] not in ("", "0")
    if settled:
        return False
    recent = (st.to_date(row["date"]) or st.today()) >= st.today() - datetime.timedelta(
        days=int(cfg["harvest"]["retry_days"]))
    return recent and st.hours_since(row["fetched"]) > float(cfg["harvest"]["refresh_hours"])


def upsert_listed(store, item, name, season):
    """Add a listed game or update it; keep the league row in step."""
    key = (item["comp_id"], season)
    league = store.leagues.get(key)
    if league is None:
        league = store.leagues[key] = {
            "comp_id": item["comp_id"], "season": season, "name": name, "has_quarters": "unknown",
            "include": "yes", "games_listed": "0", "games_played": "0", "games_collected": "0",
            "first_date": "", "last_date": "", "sampled": "", "note": "",
            "title": item["title"], "url": item["comp_url"]}
    league["title"], league["url"] = item["title"] or league["title"], item["comp_url"] or league["url"]
    listed = {k: item[k] for k in ("date", "kickoff", "comp_id", "stage", "round_id", "url", "list_status",
                                   "home_team", "away_team", "home_team_id", "away_team_id",
                                   "list_home", "list_away")}
    row = store.schedule.get(item["game_id"])
    if row is None:
        store.schedule[item["game_id"]] = {"game_id": item["game_id"], "season": season, **listed,
                                           "status": st.PENDING, "attempts": "0", "last_try": "", "detail": ""}
        store.touch("schedule", item["date"])
        return
    if all(row[k] == v for k, v in listed.items()) and row["season"] == season:
        return
    if row["date"] != item["date"]:
        store.touch("schedule", row["date"])            # the game moved to another month's file
        if row["status"] in (st.NO_SCOREBOARD, st.GAVE_UP, st.ERROR):
            row.update(status=st.PENDING, attempts="0", detail="rescheduled")
    row.update(listed, season=season)
    store.touch("schedule", item["date"])


def scan_days(fetcher, store, cfg, log):
    """Read the match list for every date not yet settled, oldest first."""
    wanted = set(cfg["seasons"])
    start = st.to_date(cfg["start_date"])
    end = st.today() - datetime.timedelta(days=int(cfg["harvest"]["settle_days"]))
    keep = bool(cfg["harvest"]["cache_pages"])
    day, read = start, 0
    try:
        while day <= end:
            key = day.isoformat()
            row = store.days.get(key)
            if day_is_due(row, cfg):
                try:
                    text = fetcher.get(DAILY_LIST_PATH.format(day=key), cache=keep, refresh=row is not None)
                except PageError as exc:
                    log(f"  {key}: list not read ({exc})")
                    day += datetime.timedelta(days=1)
                    continue
                items = parse.parse_daily(text)
                check_daily_list(text, items, key)
                items = [i for i in items if i["comp_id"]]
                kept = 0
                for item in items:
                    name, season = parse.split_title(item["title"], item["slug"])
                    season = season or item["date"][:4]
                    if season in wanted:
                        upsert_listed(store, item, name, season)
                        kept += 1
                still_open = sum(1 for i in items if i["list_status"] not in st.SETTLED_LIST)
                store.days[key] = {"date": key, "fetched": st.stamp(), "games": str(len(items)),
                                   "open": str(still_open)}
                read += 1
                if read % 10 == 0:
                    league_totals(store)
                    store.save()
                    log(f"  days read: {read} (up to {key}); games listed: {len(store.schedule)}")
            day += datetime.timedelta(days=1)
    finally:
        league_totals(store)
        store.save()
    log(f"Daily lists read this run: {read}. Games listed: {len(store.schedule)} "
        f"in {len(store.leagues)} league-seasons.")
    return read


# --------------------------------------------------------------------- games

def build_game(row, league, result, base_url=""):
    home_q, away_q, overtime = result["home_q"], result["away_q"], result["overtime"]
    h_final, a_final = result["home_final"], result["away_final"]
    game = {
        "game_id": row["game_id"], "date": row["date"], "kickoff": row["kickoff"],
        "comp_id": row["comp_id"], "competition": league["name"], "season": row["season"],
        "stage": row["stage"],
        "home_team": row["home_team"] or result["home_team"],
        "away_team": row["away_team"] or result["away_team"],
        "home_team_id": row["home_team_id"], "away_team_id": row["away_team_id"],
        "ot_detail": " ".join(f"{h}-{a}" for h, a in overtime),
        "home_final": str(h_final), "away_final": str(a_final),
        "winner": "home" if h_final > a_final else "away" if a_final > h_final else "tie",
        "source_url": base_url + row["url"], "retrieved": st.today().isoformat(),
    }
    for i in range(4):
        game[f"home_q{i + 1}"], game[f"away_q{i + 1}"] = str(home_q[i]), str(away_q[i])
    set_overtime(game, result.get("label", ""))
    game["check"] = check_text(game, row)
    return game


def apply_result(store, row, league, result, base_url=""):
    """Record what a match page showed. Returns the row's new status."""
    row["last_try"] = st.stamp()
    row["attempts"] = str(int(row["attempts"] or 0) + 1)
    row["detail"] = result.get("detail", "")
    if result["status"] == "ok":
        store.games[row["game_id"]] = build_game(row, league, result, base_url)
        store.touch("games", row["date"])
        row["status"] = st.DONE
    elif result["status"] == "other_periods":
        row["status"] = st.OTHER_PERIODS
    else:
        row["status"] = st.NO_SCOREBOARD
        row["detail"] = row["detail"] or result["status"]
    store.touch("schedule", row["date"])
    return row["status"]


def keep_example(root, kind, row, text):
    """Save a few unusual match pages under probe/examples/ so the readers can be checked."""
    folder = Path(root) / "probe" / "examples"
    if len(list(folder.glob(f"{kind}_*.html"))) >= EXAMPLES_PER_KIND:
        return
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{kind}_{row['game_id']}.html").write_text(text, encoding="utf-8")


def collect_one(fetcher, store, row, league, cfg):
    """Fetch and record one game. Returns the row's new status."""
    keep = bool(cfg["harvest"]["cache_pages"])
    try:
        text = fetcher.get(row["url"], cache=keep, refresh=int(row["attempts"] or 0) > 0)
    except PageError as exc:
        row.update(status=st.ERROR, attempts=str(int(row["attempts"] or 0) + 1),
                   last_try=st.stamp(), detail=str(exc)[:120])
        store.touch("schedule", row["date"])
        return st.ERROR
    result = parse.parse_scoreboard(text)
    status = apply_result(store, row, league, result, fetcher.base)
    game = store.games.get(row["game_id"]) if status == st.DONE else None
    if game is None:
        keep_example(fetcher.cache_dir.parent, result["status"], row, text)
    elif "PERIOD_SUM_MISMATCH" in game["check"]:
        keep_example(fetcher.cache_dir.parent, "sum_mismatch", row, text)
    elif "OT_NOT_ITEMISED" in game["check"]:
        keep_example(fetcher.cache_dir.parent, "ot_not_itemised", row, text)
    elif game["overtime"] == "yes":
        keep_example(fetcher.cache_dir.parent, "overtime", row, text)
    return status


def is_due(row, cfg):
    """Should this game's match report be fetched now?"""
    if row["list_status"] != st.PLAYED or row["status"] not in st.OPEN_STATUSES:
        return False
    date = st.to_date(row["date"])
    if date is None or date > st.today() - datetime.timedelta(days=int(cfg["harvest"]["settle_days"])):
        return False
    if row["status"] == st.PENDING:
        return True
    return st.hours_since(row["last_try"]) > float(cfg["harvest"]["refresh_hours"])


def retire_stale(store, cfg):
    """Stop retrying games that never produced a scoreboard."""
    cutoff = st.today() - datetime.timedelta(days=int(cfg["harvest"]["retry_days"]))
    for row in store.schedule.values():
        if row["status"] not in (st.NO_SCOREBOARD, st.ERROR):
            continue
        date = st.to_date(row["date"])
        tried = int(row["attempts"] or 0)
        if (date and date < cutoff and tried >= 1) or tried >= 6:
            row["status"] = st.GAVE_UP
            store.touch("schedule", row["date"])


def wanted(league):
    return league["has_quarters"] == "yes" and league["include"].strip().lower() != "no"


def matches(league, only):
    return not only or only == league["comp_id"] or only.lower() in league["name"].lower()


def due_rows(store, cfg, only=None, recent_days=0):
    """Games whose match report should be fetched now, in the configured order.

    recent_days > 0 keeps only games from the last that-many days, which is
    how a short daily run stays on current games and leaves older ones alone.
    """
    leagues = {k: l for k, l in store.leagues.items() if wanted(l) and matches(l, only)}
    rows = [r for r in store.schedule.values() if (r["comp_id"], r["season"]) in leagues and is_due(r, cfg)]
    if recent_days:
        floor = (st.today() - datetime.timedelta(days=int(recent_days))).isoformat()
        rows = [r for r in rows if r["date"] >= floor]
    rows.sort(key=st.by_date, reverse=cfg["harvest"].get("order", "oldest_first") == "newest_first")
    return rows, leagues


def collect(fetcher, store, cfg, log=print, only=None, recent_days=0):
    """Fetch every due match report in wanted leagues."""
    rows, leagues = due_rows(store, cfg, only, recent_days)
    window = f" from the last {recent_days} days" if recent_days else ""
    log(f"Match reports to fetch{window}: {len(rows)} across {len(leagues)} league-seasons with quarter scores.")
    done = 0
    recent = collections.deque(maxlen=WINDOW)      # (row, its state before the fetch, read badly?)
    try:
        for n, row in enumerate(rows, 1):
            before = dict(row)
            status = collect_one(fetcher, store, row, leagues[(row["comp_id"], row["season"])], cfg)
            if status == st.DONE:
                done += 1
            check = store.games[row["game_id"]]["check"] if status == st.DONE else ""
            badly = (status in (st.NO_SCOREBOARD, st.OTHER_PERIODS)
                     or "LIST_SCORE_MISMATCH" in check or "BAD_VALUE" in check)
            recent.append((row, before, badly))
            bad = [(r, b) for r, b, x in recent if x]
            if (len(recent) == WINDOW and len(bad) >= WINDOW_BAD_SHARE * WINDOW
                    and len({r["comp_id"] for r, _ in bad}) >= WINDOW_MIN_LEAGUES):
                for r, b in bad:                   # put those games back as they were
                    if store.games.pop(r["game_id"], None) is not None:
                        store.touch("games", r["date"])
                    r.clear()
                    r.update(b)
                    store.touch("schedule", r["date"])
                raise ReaderAlarm(
                    f"Reader check failed: {len(bad)} of the last {WINDOW} match reports read as blank or "
                    "wrong, across several leagues that normally have quarter scores. Either the site is "
                    "sending something other than match reports or the page layout has changed "
                    "(see probe/examples/). Those games were put back to be fetched again.")
            if n % int(cfg["harvest"]["save_every"]) == 0:
                store.save()
            if n % 200 == 0:
                log(f"  {n}/{len(rows)} fetched, {done} collected (now at {row['date']})")
    finally:
        retire_stale(store, cfg)
        league_totals(store)
        store.save()
    log(f"Collected {done} of {len(rows)} games this run.")
    return done


# ------------------------------------------------------------------- reparse

def reparse(fetcher, store, log=print):
    """Rebuild game records from match pages already saved on this machine.

    Use after a reader fix. Nothing is downloaded.
    """
    rebuilt = missing = 0
    for row in store.schedule.values():
        if row["status"] not in (st.DONE, st.NO_SCOREBOARD, st.OTHER_PERIODS):
            continue
        league = store.leagues.get((row["comp_id"], row["season"]))
        text = fetcher.cached(row["url"])
        if text is None or league is None:
            missing += 1
            continue
        attempts = row["attempts"]
        if store.games.pop(row["game_id"], None) is not None:
            store.touch("games", row["date"])
        apply_result(store, row, league, parse.parse_scoreboard(text), fetcher.base)
        row["attempts"] = attempts
        rebuilt += 1
    league_totals(store)
    store.save()
    log(f"Re-read {rebuilt} saved match pages; {missing} had no saved copy.")
    return rebuilt
