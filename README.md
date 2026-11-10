# Basketball quarter-results collector

Collects quarter-by-quarter basketball results from Global Sports Archive for
every league that publishes them, for the 2025/26 season onward. Results are
kept as CSV files in `data/` and pushed to a Google Sheet. Results only: no
odds, no wagering.

## Status of this version

Built from the page layout the working ACB prototype was proven against, and
tested end to end against a local stand-in site. It has **not yet run against
the live site**. Run `probe` first; it fetches about ten pages and saves them
so the page readers can be checked and tuned before anything larger runs.

## Ground rules built in

- One request at a time, with a 3-second pause after each.
- `robots.txt` is read first and obeyed. If it rules out a page the collector
  needs, the run stops.
- It identifies itself plainly. Add a contact address in `config.yaml`
  (`identity.contact`) or as the `COLLECTOR_CONTACT` repository variable.
- A refusal (HTTP 401/403) stops the run and is not retried. "Slow down"
  responses (429/503) are honoured, and the run stops if they persist.
- No page is fetched twice: collected games are never requested again.

Read the site's terms of use before the full harvest, and keep this
repository private. The data is for personal research.

## First run, in order

```
pip install -r requirements.txt
python run.py probe        # ~10 requests; writes probe/
python run.py discover     # finds competitions, tests each for quarter scores
python run.py status
python run.py harvest      # the long one; stop and restart freely
python run.py validate
python run.py export --sheets
```

After `discover`, review `data/leagues.csv`. `has_quarters` is `yes`, `no` or
`unknown`. Set `include` to `no` on any league you do not want.

`harvest` saves progress as it goes. Ctrl+C is safe; running it again
continues where it stopped. `python run.py status` shows what is left and
roughly how long it will take.

## Commands

| Command | What it does |
| --- | --- |
| `probe` | Fetches a handful of pages into `probe/` with a `report.json` |
| `discover` | Walks the basketball index, finds each competition's seasons, tests finished games for a Q1-Q4 scoreboard |
| `harvest` | Reads schedules and match reports for leagues marked `yes` (`--league NAME` for one) |
| `update` | The scheduled run: new seasons of known leagues, untested leagues, then `harvest` |
| `validate` | Re-checks every game; writes `data/exceptions.csv` and `data/summary.json` |
| `export` | `--quarters` writes `exports/quarters_<season>.csv` (four rows per game); `--sheets` updates the Google Sheet (`--force` rewrites every tab) |
| `status` | Counts and time remaining |
| `reparse` | Rebuilds games from match pages already saved in `cache/`; downloads nothing |

`--max-minutes N` and `--max-requests N` stop a run cleanly at a limit.

## Where it runs

**Your PC** for the first full harvest. It is tens of thousands of pages and
takes days at this pace, which is more than GitHub's free allowance for a
private repository.

**GitHub Actions** for the weekly top-up (`.github/workflows/update.yml`,
Tuesdays 09:23 UTC, up to 240 minutes). It does nothing until
`data/leagues.csv` exists in the repository. It can also be started by hand
from the Actions tab with a choice of command.

Do not run both at once. Pull before a local run and push after it.

## Data files

| File | Contents |
| --- | --- |
| `data/competitions.csv` | Every basketball competition found |
| `data/leagues.csv` | One row per competition and season: quarter scores yes/no, counts, closed or not |
| `data/seasons/<season>/games.csv` | One row per game: Q1-Q4 for each side, overtime, finals, check flags |
| `data/seasons/<season>/schedule.csv` | Every game listed, with status (`pending`, `done`, `no_scoreboard`, `other_periods`, `error`, `gave_up`) |
| `data/seasons/<season>/rounds.csv` | Schedule pages read, and when |
| `data/exceptions.csv` | Games flagged by a check |
| `data/summary.json`, `data/last_run.json` | Totals, and how the last run ended |

Quarter scores are per quarter, not cumulative. Overtime is held separately:
`home_ot` / `away_ot` are overtime totals and `ot_detail` lists each period
(`11-11 12-10`).

## Check flags

Flagged games are kept and listed for review. Nothing is corrected
automatically.

| Flag | Meaning |
| --- | --- |
| `PERIOD_SUM_MISMATCH` | Quarters plus overtime do not add up to the final. The site sometimes omits a second overtime; Q1-Q4 are still usable |
| `OT_WITHOUT_TIE` | Overtime is listed but regulation was not tied |
| `TIED_FINAL` | Final score is level (possible in two-leg ties) |
| `SUSPECT_VALUE` | A quarter above 60, a 0-0 quarter, or a final under 20 |
| `BAD_VALUE` | A score is missing or not a number |

## Google Sheets

The Sheet gets one tab per season (`Games 2025-2026`, ...), plus `Leagues` and
`Exceptions`. The CSV files remain the master record.

1. In Google Cloud, create a project and enable the **Google Sheets API**.
2. Create a **service account**, add a **JSON key**, and download it.
3. Create the Google Sheet and share it with the service account's email
   address as Editor.
4. Copy the spreadsheet ID from the Sheet's address (between `/d/` and `/edit`).
5. Local runs: save the key as `service_account.json` in this folder (it is
   git-ignored) and put the ID in `config.yaml` under `sheets.spreadsheet_id`.
6. GitHub runs: add repository secrets `GOOGLE_SERVICE_ACCOUNT_JSON` (the whole
   key file) and `SHEET_ID`.

Until that is done, `export --sheets` reports that it is not set up and skips.

## Settings

`config.yaml` holds the pace, the season labels to collect, discovery limits
and retry windows. Seasons are written as the site writes them: `2025/2026`
for leagues that span two years, `2025` or `2026` for calendar-year leagues.

## Layout

```
run.py                  command line
collector/fetch.py      paced, robots-aware downloading
collector/parse.py      links and scoreboards out of pages
collector/discover.py   competitions, seasons, quarter-score test, probe
collector/harvest.py    schedules and match reports
collector/validate.py   checks and summary
collector/export.py     quarter CSV and Google Sheets
collector/store.py      the CSV records
```
