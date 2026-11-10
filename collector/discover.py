"""Find basketball competitions and work out which ones carry quarter scores.

Three steps, each of which picks up where an earlier run stopped:
  1. follow listing pages from the basketball index and note every competition;
  2. open each competition once to find its seasons;
  3. test a few finished games per league-season for a Q1-Q4 scoreboard.
"""
from __future__ import annotations

import datetime
import json
import re
from pathlib import Path

from . import parse
from . import store as st
from .fetch import PageError
from .harvest import AJAX_PATH, collect_one, upsert_game, update_league_totals

LISTING_CACHE_HOURS = 24
DAILY_LIST_PATH = "/sport_ajax_v3.php?d={day}&widget_client_id=1&sp=basketball"
DAILY_EXPAND_PATH = "/comp_expand_ajax.php"
PROBE_CLASSES = ["bas_match", "bas_scoreboard", "bas_scores", "bas_team_name", "period_score",
                 "inning_container", "innin_col", "comp_cont"]


# ------------------------------------------------------- step 1: competitions

def note_links(store, links, area=""):
    """Record competitions seen in a page. Returns how many were new."""
    new = 0
    for link in links:
        row = store.competitions.get(link["competition"])
        if row is None:
            row = store.competitions[link["competition"]] = {
                "competition": link["competition"], "name": "", "area": area, "url": link["path"],
                "seasons_seen": "", "last_checked": ""}
            new += 1
        if area and not row["area"]:
            row["area"] = area
        seasons = set(filter(None, row["seasons_seen"].split(" ")))
        if link["season"] and link["season"] not in seasons:
            seasons.add(link["season"])
            row["seasons_seen"] = " ".join(sorted(seasons))
            if link["season"] >= max(seasons):
                row["url"] = link["path"]          # keep the newest season's page
    return new


def crawl_listings(fetcher, store, cfg, log):
    site, limit = cfg["site"], int(cfg["discovery"]["max_listing_pages"])
    queue = [site["index_path"]] + list(site.get("seed_paths") or [])
    seen = set()
    while queue and len(seen) < limit:
        path = queue.pop(0)
        if path in seen:
            continue
        seen.add(path)
        try:
            text = fetcher.get(path, cache=True, max_age_hours=LISTING_CACHE_HOURS)
        except PageError as exc:
            log(f"  listing page skipped ({exc}): {path}")
            continue
        area = parse.listing_area(path) if path.startswith("/competitions/") else ""
        note_links(store, parse.competition_links(text), area)
        for link in parse.listing_links(text):
            if link not in seen:
                queue.append(link)
    log(f"Listing pages read: {len(seen)}; competitions known: {len(store.competitions)}.")


# ----------------------------------------------------------- step 2: seasons

def display_name(slug):
    """'liga-endesa' -> 'Liga Endesa'; a short single word such as 'nba' -> 'NBA'."""
    if "-" not in slug and len(slug) <= 4:
        return slug.upper()
    return slug.replace("-", " ").title()


def add_league(store, competition, link):
    key = (link["competition"], link["season"])
    if key in store.leagues:
        return False
    store.leagues[key] = {
        "competition": link["competition"], "season": link["season"],
        "name": display_name(link["competition"]),
        "area": competition["area"], "url": link["path"], "has_quarters": "unknown", "include": "yes",
        "sampled": "", "games_listed": "0", "games_collected": "0", "last_harvested": "",
        "closed": "no", "note": ""}
    return True


def expand_competition(fetcher, store, cfg, competition, log):
    """Open one competition page; add league rows for the wanted seasons it offers."""
    text = fetcher.get(competition["url"], cache=True, max_age_hours=LISTING_CACHE_HOURS)
    links = parse.competition_links(text)
    note_links(store, links)                      # sibling competitions are a free find
    if not competition["name"]:
        competition["name"] = parse.page_title(text) or competition["competition"]
    competition["last_checked"] = st.stamp()
    wanted = set(cfg["seasons"])
    added = 0
    own = [l for l in links if l["competition"] == competition["competition"]]
    own += parse.competition_links(competition["url"])          # the page's own season
    for link in own:
        if link["season"] in wanted and add_league(store, competition, link):
            added += 1
    return added


def expand_all(fetcher, store, cfg, log, known_only=False):
    """Expand competitions not checked recently. New ones found on the way are queued too."""
    recheck_hours = 24 * float(cfg["discovery"]["season_check_days"])
    limit = int(cfg["discovery"]["max_competitions"])
    wanted = set(cfg["seasons"])
    done = set()
    examined = added = 0
    while examined < limit:
        todo = []
        for row in store.competitions.values():
            if row["competition"] in done or st.hours_since(row["last_checked"]) <= recheck_hours:
                continue
            have = {s for (c, s) in store.leagues if c == row["competition"]}
            if known_only and (not have or wanted <= have):
                continue                          # update runs only look for a newer season
            todo.append(row)
        if not todo:
            break
        for row in sorted(todo, key=lambda r: r["competition"]):
            done.add(row["competition"])
            examined += 1
            try:
                added += expand_competition(fetcher, store, cfg, row, log)
            except PageError as exc:
                row["last_checked"] = st.stamp()
                log(f"  competition skipped ({exc}): {row['competition']}")
            if examined % 25 == 0:
                store.save()
                log(f"  competitions examined: {examined}; league-seasons added: {added}")
            if examined >= limit:
                break
        if known_only:
            break
    store.save()
    log(f"Competitions examined: {examined}; league-seasons added: {added}; "
        f"league-seasons known: {len(store.leagues)}.")


# ---------------------------------------------------- step 3: quarter scores

def _spread(rows, count):
    """Pick up to `count` rows spaced evenly through the list."""
    if len(rows) <= count:
        return list(rows)
    step = (len(rows) - 1) / (count - 1) if count > 1 else 0
    return [rows[round(i * step)] for i in range(count)]


def _finished(season, competition, cfg):
    cutoff = st.today() - datetime.timedelta(days=int(cfg["harvest"]["settle_days"]))
    rows = [r for r in season.schedule.values()
            if r["competition"] == competition and (st.to_date(r["date"]) or st.today()) <= cutoff]
    return sorted(rows, key=lambda r: (r["date"], r["game_id"]))


def sample_league(fetcher, store, cfg, league, log):
    """Decide whether this league-season shows quarter scores."""
    competition, season = league["competition"], store.season(league["season"])
    this = next(iter(parse.competition_links(league["url"])), None)
    if this is None:
        league["note"] = "unrecognised address"
        return
    text = fetcher.get(league["url"], cache=True, max_age_hours=LISTING_CACHE_HOURS)
    for link in parse.match_links(text):
        upsert_game(season, link, competition, this["stage"], this["round_id"], "0")
    if not _finished(season, competition, cfg) and parse.max_week(text) >= 1:
        form = {"week": "1", "round": this["round_id"], "sport": "basketball", "widget_client_id": "1"}
        first = parse.ajax_html(fetcher.get(AJAX_PATH, data=form))
        for link in parse.match_links(first):
            upsert_game(season, link, competition, this["stage"], this["round_id"], "1")
    league["sampled"] = st.stamp()
    finished = _finished(season, competition, cfg)
    if not finished:
        league["note"] = "no finished games listed yet"
        return
    wanted = int(cfg["discovery"]["sample_games"])
    evidence = (st.DONE, st.NO_SCOREBOARD, st.OTHER_PERIODS, st.GAVE_UP)   # the page was read
    outcomes = [r["status"] for r in finished if r["status"] in evidence][:wanted]
    for row in _spread([r for r in finished if r["status"] == st.PENDING], wanted):
        if st.DONE in outcomes or len(outcomes) >= wanted:
            break
        status = collect_one(fetcher, season, row, league, cfg)
        if status in evidence:
            outcomes.append(status)
    listed = [r for r in season.schedule.values() if r["competition"] == competition]
    nothing_left = (bool(outcomes) and len(listed) == len(finished)
                    and all(r["status"] in evidence for r in finished))
    kinds = ", ".join(sorted(set(outcomes)))
    if st.DONE in outcomes:
        league["has_quarters"], league["note"] = "yes", ""
    elif len(outcomes) >= wanted or nothing_left:
        league["has_quarters"] = "no"
        detail = next((r["detail"] for r in finished if r["status"] == st.OTHER_PERIODS and r["detail"]), "")
        league["note"] = f"{len(outcomes)} finished games tested: {kinds}" + (f" ({detail})" if detail else "")
    else:
        league["note"] = f"only {len(outcomes)} finished games so far: {kinds}"
    update_league_totals(season, league, cfg)


def sample_all(fetcher, store, cfg, log):
    todo = [l for l in store.leagues.values()
            if l["has_quarters"] == "unknown" and l["include"].strip().lower() != "no"
            and st.hours_since(l["sampled"]) > 72]
    todo.sort(key=lambda l: (l["competition"], l["season"]))
    log(f"Testing {len(todo)} league-seasons for quarter scores.")
    for n, league in enumerate(todo, 1):
        try:
            sample_league(fetcher, store, cfg, league, log)
        except PageError as exc:
            league["sampled"], league["note"] = st.stamp(), f"could not be tested: {exc}"[:120]
        if n % 10 == 0:
            store.save()
            log(f"  tested {n}/{len(todo)}")
    store.save()
    counts = {}
    for league in store.leagues.values():
        counts[league["has_quarters"]] = counts.get(league["has_quarters"], 0) + 1
    log("League-seasons with quarter scores: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))


def discover(fetcher, store, cfg, log=print):
    try:
        crawl_listings(fetcher, store, cfg, log)
        store.save()
        expand_all(fetcher, store, cfg, log)
        sample_all(fetcher, store, cfg, log)
    finally:
        store.save()


def refresh(fetcher, store, cfg, log=print):
    """Light version for scheduled runs: new seasons of known leagues, then untested leagues."""
    try:
        expand_all(fetcher, store, cfg, log, known_only=True)
        sample_all(fetcher, store, cfg, log)
    finally:
        store.save()


# --------------------------------------------------------------------- probe

def probe(fetcher, cfg, root, log=print):
    """Fetch a handful of pages and save them, so the page readers can be checked.

    Writes probe/*.html and probe/report.json. About a dozen requests.

    Besides the competition and match pages, it saves one day's match list (the
    page the site's own daily view loads). If that list carries period scores,
    whole days can be collected with one request instead of one per game.
    """
    folder = Path(root) / "probe"
    folder.mkdir(exist_ok=True)
    report = {"started": st.stamp(), "steps": []}

    def step(name, path, data=None):
        entry = {"step": name, "path": path}
        report["steps"].append(entry)
        try:
            text = fetcher.get(path, data=data)
        except PageError as exc:
            entry["error"] = str(exc)
            log(f"{name}: FAILED ({exc})")
            return ""
        body = parse.ajax_html(text) if data else text
        (folder / f"{len(report['steps']):02d}_{name}.html").write_text(text, encoding="utf-8")
        comps, games = parse.competition_links(body), parse.match_links(body)
        entry.update({
            "bytes": len(text), "title": parse.page_title(body), "max_week": parse.max_week(body),
            "competition_links": len(comps), "competitions": len({c["competition"] for c in comps}),
            "seasons_seen": sorted({c["season"] for c in comps})[-12:],
            "listing_links": len(parse.listing_links(body)), "match_links": len(games),
            "sample_competition_links": [c["path"] for c in comps[:8]],
            "sample_listing_links": parse.listing_links(body)[:8],
            "sample_match_links": [g["path"] for g in games[:4]],
            "class_counts": {c: len(re.findall(rf'class="[^"]*\b{c}', body)) for c in PROBE_CLASSES},
            "expand_ids": len(set(re.findall(r"expandComp\((\d+)", body))),
        })
        log(f"{name}: {len(text)} bytes, {len(comps)} competition links, {len(games)} match links")
        return body

    def match_step(name, body):
        cutoff = st.today() - datetime.timedelta(days=int(cfg["harvest"]["settle_days"]))
        past = [g for g in parse.match_links(body) if (st.to_date(g["date"]) or st.today()) <= cutoff]
        if not past:
            report["steps"].append({"step": name, "error": "no finished match link on the page"})
            return
        text = step(name, past[-1]["path"])
        if text:
            result = parse.parse_scoreboard(text)
            report["steps"][-1]["scoreboard"] = {k: v for k, v in result.items()}
            log(f"{name}: scoreboard {result['status']} {result.get('detail', '')}")

    try:
        index = step("index", cfg["site"]["index_path"])
        listings = [l for l in parse.listing_links(index) if l != cfg["site"]["index_path"]]
        if listings:
            step("listing", listings[0])
        day = (st.today() - datetime.timedelta(days=3)).isoformat()
        daily = step("daily_list", DAILY_LIST_PATH.format(day=day))
        collapsed = re.findall(r"expandComp\((\d+)", daily)
        if collapsed:
            step("daily_expand", DAILY_EXPAND_PATH,
                 data={"compid": collapsed[-1], "d": day, "widget_client_id": "1"})
        season_page = step("season_page", cfg["probe"]["season_page"])
        this = next(iter(parse.competition_links(cfg["probe"]["season_page"])), None)
        if this:
            form = {"week": "1", "round": this["round_id"], "sport": "basketball", "widget_client_id": "1"}
            week = step("round_list", AJAX_PATH, data=form)
            match_step("match_report", week or season_page)
        stage = step("stage_page", cfg["probe"]["stage_page"])
        match_step("stage_match_report", stage)
        others = [c for c in parse.competition_links(index)
                  if this is None or c["competition"] != this["competition"]]
        if others:
            other = step("other_competition", others[0]["path"])
            match_step("other_match_report", other)
    finally:
        report["robots_txt"] = fetcher.robots_text
        report["fetch"] = fetcher.stats()
        report["finished"] = st.stamp()
        (folder / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        log(f"Probe written to {folder} ({fetcher.requests} requests).")
    return report
