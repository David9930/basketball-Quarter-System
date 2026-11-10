"""Read the site's daily match list and its match-report scoreboards.

Both readers were checked against pages saved from the live site
(see probe/).
"""
from __future__ import annotations

import re

from lxml import html

_SEG = r"""[^/"'\s<>\\?#]+"""
COMP_RE = re.compile(rf"/competition/basketball/({_SEG})/({_SEG})/(\d+)/?")
MATCH_RE = re.compile(rf"/match/basketball/(\d{{4}}-\d{{2}}-\d{{2}})/({_SEG})/(\d+)/?")
SEASON_IN_TITLE_RE = re.compile(r"\b((?:19|20)\d{2})(?:/((?:19|20)\d{2}))?\b")
SLUG_SEASON_RE = re.compile(r"^(.*?)-((?:19|20)\d{2})(?:-((?:19|20)\d{2}))?(?:-.*)?$")
QUARTER_LABEL_RE = re.compile(r"^(?:Q[1-4]|[1-4]Q|[1-4])$", re.I)
SCORE_RE = re.compile(r"^(\d+)\s*:\s*(\d+)$")
TEAM_LINK_RE = re.compile(r"gotoUrl\((\d+)\s*,\s*['\"]([^'\"]*)")
TITLE_RE = re.compile(r"Match Report of (.*?) vs (.*?) - \d{4}-\d{2}-\d{2}")


def _text(node):
    return " ".join(node.text_content().split())


def split_title(title, slug=""):
    """'NBA 2026/2027' -> ('NBA', '2026/2027'); 'Liga UNO 2026' -> ('Liga UNO', '2026').

    Falls back to the address name ('liga-endesa-2025-2026') when the title
    carries no year. Returns an empty season if neither does.
    """
    found = list(SEASON_IN_TITLE_RE.finditer(title or ""))
    if found:
        m = found[-1]
        season = f"{m.group(1)}/{m.group(2)}" if m.group(2) else m.group(1)
        return (title[:m.start()].strip(" -") or title.strip()), season
    m = SLUG_SEASON_RE.match(slug or "")
    if m:
        first, second = m.group(2), m.group(3)
        season = f"{first}/{second}" if second and int(second) == int(first) + 1 else (second or first)
        return (title or m.group(1).replace("-", " ").title()).strip(), season
    return (title or slug.replace("-", " ").title()).strip(), ""


def parse_daily(text):
    """Every game in one day's match list, with the competition it sits under.

    Each item: game_id, date, url, comp_id, competition title, address name,
    stage, round_id, list status (Played, Postponed, Fixture, ...), kickoff,
    team names and IDs, and the final score shown in the list.
    """
    doc = html.fromstring(text)
    games, current = [], None
    for a in doc.iter("a"):
        href = a.get("href") or ""
        m = MATCH_RE.search(href)
        if m and "gsa-match-link" in (a.get("class") or ""):
            comp_id = re.search(r"(?:^|\s)c(\d+)(?:\s|$)", a.get("class") or "")
            box = a.find(".//div")
            status = ((box.get("class") or "").split() or [""])[0] if box is not None else ""
            item = {"game_id": m.group(3), "date": m.group(1),
                    "url": f"/match/basketball/{m.group(1)}/{m.group(2)}/{m.group(3)}/",
                    "comp_id": comp_id.group(1) if comp_id else "", "list_status": status,
                    "kickoff": "", "list_home": "", "list_away": ""}
            item.update(current or {"title": "", "slug": "", "stage": "", "round_id": "", "comp_url": ""})
            times = a.xpath('.//span[contains(@class,"kickoff_time")]')
            if times:
                item["kickoff"] = _text(times[0])
            for side, key in (("a", "home"), ("b", "away")):
                spans = a.xpath(f'.//span[contains(@class,"team_{side}_name")]')
                name, team_id = "", ""
                if spans:
                    full = spans[0].xpath('.//span[contains(@class,"team_full")]')
                    name = _text(full[0] if full else spans[0])
                    link = TEAM_LINK_RE.search(spans[0].get("onclick") or "")
                    team_id = link.group(1) if link else ""
                item[f"{key}_team"], item[f"{key}_team_id"] = name, team_id
            for span in a.xpath('.//span[contains(@class,"match_score")]'):
                if "match_stat_con" in (span.get("class") or ""):
                    continue
                score = SCORE_RE.match(_text(span))
                if score:
                    item["list_home"], item["list_away"] = score.group(1), score.group(2)
                break
            games.append(item)
            continue
        c = COMP_RE.search(href)
        if c:
            path = f"/competition/basketball/{c.group(1)}/{c.group(2)}/{c.group(3)}/"
            if current is None or current["comp_url"] != path:
                current = {"title": "", "slug": c.group(1), "stage": c.group(2),
                           "round_id": c.group(3), "comp_url": path}
            label = _text(a)
            if label:
                current["title"] = label
    return games


def page_title(text):
    m = re.search(r"<title[^>]*>(.*?)</title>", text, re.S | re.I)
    if not m:
        return ""
    return " ".join(html.fromstring(f"<p>{m.group(1)}</p>").text_content().split())[:160]


def _number(value):
    value = value.strip()
    return int(value) if value.isdigit() else None


def parse_scoreboard(text):
    """Read the period scoreboard from a match report.

    Returns a dict whose 'status' is one of:
      ok             four quarters, any overtime, and finals were read
      no_scoreboard  the page has no period scoreboard
      incomplete     a scoreboard exists but a quarter or total is blank
      other_periods  the game is not scored in four quarters (e.g. halves)
    'label' is the scoreboard's own status text, normally 'Final'.
    """
    doc = html.fromstring(text)
    cells = doc.xpath('//div[contains(concat(" ", normalize-space(@class), " "), " bas_scores ")]')
    values = [_text(c) for c in cells]
    rows = []
    for cell in cells:
        parent = cell.getparent()
        if parent is not None and not any(parent is r for r in rows):
            rows.append(parent)
    home_team, away_team = _board_teams(doc, rows)
    label = ""
    if rows:
        first = rows[0].find("./div")
        if first is not None and "bas_scores" not in (first.get("class") or ""):
            label = _text(first)
    out = {"status": "no_scoreboard", "detail": "", "label": label,
           "home_team": home_team, "away_team": away_team}
    if "TOT" not in values:
        return out
    width = values.index("TOT") + 1
    headers = values[:width]
    out["headers"] = headers
    labels = headers[:-1]
    if len(labels) < 4 or not all(QUARTER_LABEL_RE.match(x) for x in labels[:4]):
        return {**out, "status": "other_periods", "detail": "periods: " + " ".join(headers)}
    if len(values) < width * 3:
        return {**out, "status": "incomplete", "detail": "scoreboard rows missing"}
    home = [_number(v) for v in values[width:width * 2]]
    away = [_number(v) for v in values[width * 2:width * 3]]
    if None in home[:4] or None in away[:4] or home[-1] is None or away[-1] is None:
        return {**out, "status": "incomplete", "detail": "blank quarter or total"}
    overtime = []
    for h, a in zip(home[4:-1], away[4:-1]):
        if h is None and a is None:
            continue                       # an empty overtime column
        if h is None or a is None:
            return {**out, "status": "incomplete", "detail": "blank overtime score"}
        overtime.append((h, a))
    return {**out, "status": "ok", "home_q": home[:4], "away_q": away[:4], "overtime": overtime,
            "home_final": home[-1], "away_final": away[-1]}


def _board_teams(doc, rows):
    """Team names from the scoreboard rows, falling back to the page title."""
    names = []
    for row in rows[1:3]:
        spans = row.xpath('.//span[contains(@class,"full_name")]')
        names.append(_text(spans[0]) if spans else "")
    names += ["", ""]
    if names[0] and names[1]:
        return names[0], names[1]
    m = TITLE_RE.search(doc.xpath("string(//title)") or "")
    if m:
        return names[0] or m.group(1).strip(), names[1] or m.group(2).strip()
    return names[0], names[1]
