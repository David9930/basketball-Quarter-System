"""Read schedules and match reports for leagues known to carry quarter scores.

Everything is incremental: a schedule page or game already dealt with is not
fetched again, so a run can be stopped and restarted at any point.
"""
from __future__ import annotations

import datetime

from . import parse
from . import store as st
from .fetch import PageError
from .validate import check_text

MAX_STAGE_PAGES = 60          # safety limit on stage pages per league-season
AJAX_PATH = "/comp_ajax.php"


# ----------------------------------------------------------------- schedules

def upsert_game(season, link, competition, stage, round_id, week):
    """Add a listed game, or update it if the site moved it."""
    row = season.schedule.get(link["game_id"])
    if row is None:
        season.schedule[link["game_id"]] = {
            "game_id": link["game_id"], "date": link["date"], "competition": competition,
            "stage": stage, "round_id": round_id, "week": week, "url": link["path"],
            "status": st.PENDING, "attempts": "0", "last_try": "", "detail": "",
        }
        season.dirty = True
        return
    if row["competition"] != competition:
        return                                  # listed on another competition's page
    if row["date"] != link["date"] or row["url"] != link["path"]:
        row["date"], row["url"] = link["date"], link["path"]
        if row["status"] in (st.NO_SCOREBOARD, st.GAVE_UP, st.ERROR):
            row.update(status=st.PENDING, attempts="0", detail="rescheduled")
        season.dirty = True
    if week != "0" and (row["week"] != week or row["round_id"] != round_id):
        row.update(week=week, round_id=round_id, stage=stage)
        season.dirty = True


def _mark_round(season, competition, stage, round_id, week, listed=None):
    key = (competition, round_id, week)
    row = season.rounds.setdefault(key, {"competition": competition, "stage": stage,
                                         "round_id": round_id, "week": week,
                                         "listed": "", "last_fetched": ""})
    if listed is not None:
        row["listed"], row["last_fetched"] = str(listed), st.stamp()
    season.dirty = True
    return row


def read_stages(fetcher, season, league, log):
    """Walk the league-season's stage pages; note each stage's rounds and games."""
    competition, label = league["competition"], league["season"]
    queue, seen = [league["url"]], set()
    while queue and len(seen) < MAX_STAGE_PAGES:
        path = queue.pop(0)
        if path in seen:
            continue
        seen.add(path)
        try:
            text = fetcher.get(path)
        except PageError as exc:
            log(f"  stage page skipped ({exc}): {path}")
            continue
        links = parse.competition_links(text)
        this = next((l for l in parse.competition_links(path)), None)
        for link in links:
            if link["competition"] == competition and link["season"] == label and link["path"] not in seen:
                queue.append(link["path"])
        if this is None:
            continue
        games = parse.match_links(text)
        for game in games:
            upsert_game(season, game, competition, this["stage"], this["round_id"], "0")
        _mark_round(season, competition, this["stage"], this["round_id"], "0", listed=len(games))
        for week in range(1, parse.max_week(text) + 1):
            _mark_round(season, competition, this["stage"], this["round_id"], str(week))
    return len(seen)


def _round_is_due(row, games, cfg, closed):
    if not row["last_fetched"]:
        return True
    if closed:
        return False
    age = st.hours_since(row["last_fetched"])
    if not games:
        return age > 24 * 6
    overdue = any(g["status"] in st.OPEN_STATUSES and (st.to_date(g["date"]) or st.today()) < st.today()
                  for g in games)
    return overdue and age > float(cfg["harvest"]["refresh_hours"])


def read_rounds(fetcher, season, league, cfg, log):
    """Fetch each round's game list where it has not been read or may have changed."""
    competition = league["competition"]
    by_round = {}
    for game in season.schedule.values():
        if game["competition"] == competition:
            by_round.setdefault((game["round_id"], game["week"]), []).append(game)
    fetched = 0
    rounds = [r for r in season.rounds.values() if r["competition"] == competition and r["week"] != "0"]
    for row in sorted(rounds, key=lambda r: (r["round_id"], int(r["week"]))):
        games = by_round.get((row["round_id"], row["week"]), [])
        if not _round_is_due(row, games, cfg, league["closed"] == "yes"):
            continue
        form = {"week": row["week"], "round": row["round_id"], "sport": "basketball", "widget_client_id": "1"}
        try:
            text = parse.ajax_html(fetcher.get(AJAX_PATH, data=form))
        except PageError as exc:
            log(f"  round skipped ({exc}): {competition} round {row['round_id']} week {row['week']}")
            continue
        links = parse.match_links(text)
        for link in links:
            upsert_game(season, link, competition, row["stage"], row["round_id"], row["week"])
        _mark_round(season, competition, row["stage"], row["round_id"], row["week"], listed=len(links))
        fetched += 1
    return fetched


# --------------------------------------------------------------------- games

def build_game(row, league, result, base_url=""):
    home_q, away_q, overtime = result["home_q"], result["away_q"], result["overtime"]
    h_final, a_final = result["home_final"], result["away_final"]
    game = {
        "game_id": row["game_id"], "date": row["date"], "competition": league["competition"],
        "competition_name": league["name"], "area": league["area"], "season": league["season"],
        "stage": row["stage"], "week": "" if row["week"] == "0" else row["week"],
        "home_team": result["home_team"], "away_team": result["away_team"],
        "home_team_id": result["home_team_id"], "away_team_id": result["away_team_id"],
        "overtime_periods": str(len(overtime)),
        "home_ot": str(sum(h for h, _ in overtime)), "away_ot": str(sum(a for _, a in overtime)),
        "ot_detail": " ".join(f"{h}-{a}" for h, a in overtime),
        "home_final": str(h_final), "away_final": str(a_final),
        "winner": "home" if h_final > a_final else "away" if a_final > h_final else "tie",
        "source_url": base_url + row["url"], "retrieved": st.today().isoformat(),
    }
    for i in range(4):
        game[f"home_q{i + 1}"], game[f"away_q{i + 1}"] = str(home_q[i]), str(away_q[i])
    game["check"] = check_text(game)
    return game


def apply_result(season, row, league, result, base_url=""):
    """Record what a match page showed. Returns the row's new status."""
    row["last_try"] = st.stamp()
    row["attempts"] = str(int(row["attempts"] or 0) + 1)
    row["detail"] = result.get("detail", "")
    if result["status"] == "ok":
        season.games[row["game_id"]] = build_game(row, league, result, base_url)
        row["status"] = st.DONE
    elif result["status"] == "other_periods":
        row["status"] = st.OTHER_PERIODS
    else:
        row["status"] = st.NO_SCOREBOARD
        row["detail"] = row["detail"] or result["status"]
    season.dirty = True
    return row["status"]


def collect_one(fetcher, season, row, league, cfg):
    """Fetch and record one game. Returns the row's new status."""
    keep = bool(cfg["harvest"]["cache_match_pages"])
    try:
        text = fetcher.get(row["url"], cache=keep, refresh=int(row["attempts"] or 0) > 0)
    except PageError as exc:
        row.update(status=st.ERROR, attempts=str(int(row["attempts"] or 0) + 1),
                   last_try=st.stamp(), detail=str(exc)[:120])
        season.dirty = True
        return st.ERROR
    return apply_result(season, row, league, parse.parse_scoreboard(text), fetcher.base)


def is_due(row, cfg):
    """Should this game be fetched now?"""
    if row["status"] not in st.OPEN_STATUSES:
        return False
    date = st.to_date(row["date"])
    if date is None or date > st.today() - datetime.timedelta(days=int(cfg["harvest"]["settle_days"])):
        return False
    if row["status"] == st.PENDING:
        return True
    return st.hours_since(row["last_try"]) > float(cfg["harvest"]["refresh_hours"])


def retire_stale(season, competition, cfg):
    """Stop retrying games that never produced a scoreboard."""
    cutoff = st.today() - datetime.timedelta(days=int(cfg["harvest"]["retry_days"]))
    for row in season.schedule.values():
        if row["competition"] != competition or row["status"] not in (st.NO_SCOREBOARD, st.ERROR):
            continue
        date = st.to_date(row["date"])
        tried = int(row["attempts"] or 0)
        if (date and date < cutoff and tried >= 1) or tried >= 6:
            row["status"] = st.GAVE_UP
            season.dirty = True


def collect_games(fetcher, store, season, league, cfg, log):
    competition = league["competition"]
    rows = sorted((r for r in season.schedule.values()
                   if r["competition"] == competition and is_due(r, cfg)),
                  key=lambda r: (r["date"], r["game_id"]))
    done = 0
    for n, row in enumerate(rows, 1):
        if collect_one(fetcher, season, row, league, cfg) == st.DONE:
            done += 1
        if n % int(cfg["harvest"]["save_every"]) == 0:
            store.save()
            log(f"  {competition} {league['season']}: {n}/{len(rows)} games tried, {done} collected")
    retire_stale(season, competition, cfg)
    return len(rows), done


def update_league_totals(season, league, cfg):
    competition = league["competition"]
    rows = [r for r in season.schedule.values() if r["competition"] == competition]
    league["games_listed"] = str(len(rows))
    league["games_collected"] = str(sum(1 for r in rows if r["status"] == st.DONE))
    rounds = [r for r in season.rounds.values() if r["competition"] == competition]
    dates = [d for d in (st.to_date(r["date"]) for r in rows) if d]
    quiet_since = st.today() - datetime.timedelta(days=int(cfg["harvest"]["season_closed_after_days"]))
    finished = bool(rows and rounds and dates
                    and all(r["last_fetched"] for r in rounds)
                    and not any(r["status"] in st.OPEN_STATUSES for r in rows)
                    and max(dates) < quiet_since)
    league["closed"] = "yes" if finished else "no"


def wanted(league):
    return (league["has_quarters"] == "yes" and league["include"].strip().lower() != "no"
            and league["closed"] != "yes")


def harvest_league(fetcher, store, league, cfg, log):
    season = store.season(league["season"])
    has_rounds = any(r["competition"] == league["competition"] for r in season.rounds.values())
    if not has_rounds or st.hours_since(league["last_harvested"]) > float(cfg["harvest"]["refresh_hours"]):
        read_stages(fetcher, season, league, log)
    read_rounds(fetcher, season, league, cfg, log)
    tried, done = collect_games(fetcher, store, season, league, cfg, log)
    league["last_harvested"] = st.stamp()
    update_league_totals(season, league, cfg)
    log(f"{league['competition']} {league['season']}: {league['games_collected']}/{league['games_listed']} "
        f"games collected (+{done} this run){' - season closed' if league['closed'] == 'yes' else ''}")


def harvest(fetcher, store, cfg, log=print, only=None):
    """Harvest every wanted league, least recently visited first."""
    leagues = [l for l in store.leagues.values() if wanted(l) and (not only or l["competition"] == only)]
    leagues.sort(key=lambda l: (l["last_harvested"], l["competition"], l["season"]))
    log(f"Harvesting {len(leagues)} league-seasons.")
    try:
        for league in leagues:
            try:
                harvest_league(fetcher, store, league, cfg, log)
            finally:
                update_league_totals(store.season(league["season"]), league, cfg)
            store.save()
    finally:
        store.save()
    return len(leagues)


# ------------------------------------------------------------------- reparse

def reparse(fetcher, store, log=print):
    """Rebuild game records from match pages already saved on this machine.

    Use after a parser fix. Nothing is downloaded.
    """
    rebuilt = missing = 0
    for name in store.season_names():
        season = store.season(name)
        for row in season.schedule.values():
            if row["status"] not in (st.DONE, st.NO_SCOREBOARD, st.OTHER_PERIODS):
                continue
            league = store.leagues.get((row["competition"], name.replace("-", "/")))
            text = fetcher.cached(row["url"])
            if text is None or league is None:
                missing += 1
                continue
            attempts = row["attempts"]
            season.games.pop(row["game_id"], None)
            apply_result(season, row, league, parse.parse_scoreboard(text), fetcher.base)
            row["attempts"] = attempts
            rebuilt += 1
    store.save()
    log(f"Re-read {rebuilt} saved match pages; {missing} had no saved copy.")
    return rebuilt
