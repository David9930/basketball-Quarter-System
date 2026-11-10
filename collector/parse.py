"""Read links and scoreboards out of Global Sports Archive pages.

Links are found by address pattern anywhere in the page source, so they are
picked up whether they sit in a menu, a drop-down or a script. The scoreboard
reader follows the layout the original ACB prototype was proven against.
"""
from __future__ import annotations

import json
import re

from lxml import html

_SEG = r"""[^/"'\s<>\\?#]+"""
COMP_RE = re.compile(rf"/competition/basketball/({_SEG})/({_SEG})/(\d+)/?")
MATCH_RE = re.compile(rf"/match/basketball/(\d{{4}}-\d{{2}}-\d{{2}})/({_SEG})/(\d+)/?")
LISTING_RE = re.compile(r"""/competitions/basketball/[^"'\s<>\\?#]*""")
SLUG_SEASON_RE = re.compile(r"^(.*?)-(\d{4})(?:-(\d{4}))?$")
QUARTER_LABEL_RE = re.compile(r"^(?:Q[1-4]|[1-4]Q|[1-4])$", re.I)
TITLE_RE = re.compile(r"Match Report of (.*?) vs (.*?) - \d{4}-\d{2}-\d{2}")


def _clean(text):
    return text.replace("\\/", "/")


def split_slug(slug):
    """'liga-endesa-2025-2026' -> ('liga-endesa', '2025/2026'); 'wnba-2025' -> ('wnba', '2025')."""
    m = SLUG_SEASON_RE.match(slug)
    if not m:
        return slug, ""
    name, first, second = m.group(1), m.group(2), m.group(3)
    if second is None:
        return name, first
    if int(second) == int(first) + 1:
        return name, f"{first}/{second}"
    return f"{name}-{first}", second


def competition_links(text):
    """Every competition stage link in the page, one per round ID."""
    found = {}
    for m in COMP_RE.finditer(_clean(text)):
        slug, stage, round_id = m.group(1), m.group(2), m.group(3)
        if round_id in found:
            continue
        competition, season = split_slug(slug)
        found[round_id] = {
            "competition": competition, "season": season, "slug": slug, "stage": stage,
            "round_id": round_id, "path": f"/competition/basketball/{slug}/{stage}/{round_id}/",
        }
    return list(found.values())


def match_links(text):
    """Every match link in the page, one per game ID."""
    found = {}
    for m in MATCH_RE.finditer(_clean(text)):
        date, slug, game_id = m.group(1), m.group(2), m.group(3)
        found.setdefault(game_id, {
            "game_id": game_id, "date": date,
            "path": f"/match/basketball/{date}/{slug}/{game_id}/",
        })
    return list(found.values())


def listing_links(text):
    """Links to basketball competition-list pages (the index and any sub-lists)."""
    seen = []
    for m in LISTING_RE.finditer(_clean(text)):
        path = m.group(0)
        if not path.endswith("/"):
            path += "/"
        if path not in seen:
            seen.append(path)
    return seen


def listing_area(path):
    """Best-effort area name from a listing address such as /competitions/basketball/spain/."""
    parts = [p for p in path.split("/") if p][2:]
    for part in parts:
        if re.search(r"[a-zA-Z]", part):
            return part.replace("-", " ").title()
    return ""


def ajax_html(text):
    """The schedule widget answers with JSON holding an HTML fragment."""
    try:
        payload = json.loads(text)
    except ValueError:
        return text
    if isinstance(payload, dict) and isinstance(payload.get("result"), str):
        return payload["result"]
    return text


def max_week(text):
    """Number of rounds (or pages) the schedule widget offers; 0 if none."""
    values = []
    try:
        doc = html.fromstring(text)
        values = doc.xpath('//input[@id="maxweek"]/@value')
    except Exception:
        pass
    if not values:
        values = re.findall(r"""id=["']maxweek["'][^>]*?value=["'](\d+)""", text)
        values += re.findall(r"""value=["'](\d+)["'][^>]*?id=["']maxweek["']""", text)
    numbers = [int(v) for v in values if str(v).strip().isdigit()]
    return max(numbers) if numbers else 0


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
    """
    doc = html.fromstring(text)
    cells = doc.xpath('//div[contains(concat(" ", normalize-space(@class), " "), " bas_scores ")]')
    values = [" ".join(c.text_content().split()) for c in cells]
    home_team, away_team, home_id, away_id = _teams(doc, cells)
    out = {"status": "no_scoreboard", "detail": "", "home_team": home_team, "away_team": away_team,
           "home_team_id": home_id, "away_team_id": away_id}
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


def _teams(doc, cells):
    """Team names from the page title, falling back to the scoreboard rows."""
    rows = []
    for cell in cells:
        parent = cell.getparent()
        if parent is not None and not any(parent is r for r in rows):
            rows.append(parent)
    names, ids = [], []
    for row in rows[1:3]:
        spans = row.xpath('.//span[contains(@class,"team_full") or contains(@class,"team_name")]')
        if spans:
            names.append(" ".join(spans[0].text_content().split()))
        else:
            clone = html.fromstring(html.tostring(row))
            for node in clone.xpath('.//*[contains(@class,"bas_scores")]'):
                node.drop_tree()
            names.append(" ".join(clone.text_content().split()))
        onclick = " ".join(row.xpath(".//@onclick"))
        found = re.findall(r"(?:gotoUrl|gotoTeam)\((\d+)", onclick)
        ids.append(found[0] if found else "")
    names += ["", ""]
    ids += ["", ""]
    title = doc.xpath("string(//title)")
    m = TITLE_RE.search(title or "")
    if m:
        return m.group(1).strip(), m.group(2).strip(), ids[0], ids[1]
    return names[0], names[1], ids[0], ids[1]
