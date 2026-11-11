# Basketball quarter-results collector

Collects quarter-by-quarter basketball results from Global Sports Archive for
every league that publishes them, for the 2025/26 season onward. Results are
kept as CSV files in `data/` and pushed to a Google Sheet. Results only: no
odds, no wagering.

## How it works

1. **Daily lists.** The site has one list of every basketball game per date.
   The collector reads one list per day (one request each). That gives every
   game, its league, teams and final score.
2. **Quarter-score test.** For each league and season it opens a few finished
   games. A filled Q1-Q4 scoreboard marks the league `yes`. Halves mark it
   `no` after three games. Blank scoreboards mark it `no` after three games,
   or eight for a league with 40 or more games, because the site's coverage
   is patchy.
3. **Match reports.** For leagues marked `yes`, it opens each finished game's
   match report (one request each) and records the quarters.

The page readers were checked against pages saved from the live site.

## Ground rules built in

- One request at a time, with a 3-second pause after each.
- `robots.txt` is read first and obeyed. If it rules out a page the collector
  needs, the run stops.
- It identifies itself plainly. Add a contact address in `config.yaml`
  (`identity.contact`) or as the `COLLECTOR_CONTACT` repository variable.
- A refusal (HTTP 401/403) stops the run and is not retried. "Slow down"
  responses (429/503) are honoured, and the run stops if they persist.
- No page is fetched twice: a day or game already dealt with is not requested
  again.

The site belongs to Data Sports Group, which supplies sports data
commercially. Keep this repository private and the data for personal research.

## Running it

```
pip install -r requirements.txt
python run.py discover     # daily lists, then the quarter-score test
python run.py status       # what is left, and roughly how long
python run.py harvest      # match reports; the long one
python run.py validate
python run.py export --sheets
```

After `discover`, review `data/leagues.csv`. Set `include` to `no` on any
league you do not want before running `harvest`. To collect a league the test
ruled out, set its `has_quarters` to `yes`; games with blank scoreboards are
then tried once and skipped.

Every command saves progress as it goes. Ctrl+C is safe; running it again
continues where it stopped.

| Command | What it does |
| --- | --- |
| `probe` | Fetches one day's list and three match reports into `probe/`; a quick check that the site accepts the collector and the readers still work |
| `discover` | Reads daily lists from `start_date` to two days ago, then tests each league-season for quarter scores |
| `harvest` | Fetches match reports for leagues marked `yes`, oldest first (`--league 933` or `--league nba` for one league) |
| `update` | The scheduled run: `discover`, then `harvest` |
| `validate` | Re-checks every game; writes `data/exceptions.csv` and `data/summary.json` |
| `export` | `--csv` writes per-season files to `exports/` (one row per game, and four rows per game); `--sheets` updates the Google Sheet (`--force` rewrites every tab) |
| `status` | Counts and time remaining |
| `reparse` | Rebuilds games from match pages already saved in `cache/`; downloads nothing |

`--max-minutes N` and `--max-requests N` stop a run cleanly at a limit.

## Where it runs

**GitHub Actions** (`.github/workflows/update.yml`): each run works for up to
240 minutes in 40-minute chunks and pushes its progress after every chunk. It
can also be started by hand from the Actions tab with a choice of command.

- During the first full harvest the schedule is every six hours (01:17,
  07:17, 13:17 and 19:17 UTC). About seven runs clear the backlog, using
  roughly 1,600 Actions minutes.
- After that the schedule goes back to weekly (Tuesdays 09:23 UTC). Both
  lines are in the workflow file.

You can edit `data/leagues.csv` on GitHub while a run is going; your edit
wins and takes effect from the next chunk.

**Your PC** works too: `python run.py harvest`. Do not run both at once. Pull
before a local run and push after it.

## Data files

| File | Contents |
| --- | --- |
| `data/leagues.csv` | One row per league and season: quarter scores yes/no/unknown, your `include` choice, game counts |
| `data/games/YYYY-MM.csv` | One row per game: Q1-Q4 for each side, overtime, finals, check flags |
| `data/schedule/YYYY-MM.csv` | Every game listed, with teams, the list's final score, and collection status |
| `data/days.csv` | Which dates have been read |
| `data/exceptions.csv` | Games flagged by a check |
| `data/summary.json`, `data/last_run.json` | Totals, and how the last run ended |
| `probe/examples/` | A few unusual match pages (overtime, no scoreboard, halves) kept for checking the readers |

Notes on the data:

- Dates and kickoff times are the site's, which are UTC. A North American
  evening game is filed under the following day.
- Quarter scores are per quarter, not cumulative. Regulation score is the sum
  of the four quarters.
- Overtime is held separately. `overtime` is `yes` or `no`; `home_ot` /
  `away_ot` are overtime points; `ot_detail` lists each period the site shows
  (`11-11 10-9`) and `overtime_periods` counts them. Where regulation ended
  level and the site does not list every overtime period, overtime points are
  taken as final minus regulation and the game is flagged `OT_NOT_ITEMISED`.
- `comp_id` is the site's own league number. It stays the same when a league
  changes sponsor name, so use it to group a league across seasons.
- `season` is the label the site gives the competition (`2025/2026`, or a
  single year for calendar-year leagues).
- Schedule files hold final scores for every listed game, including leagues
  without quarter scores.

## Check flags

Flagged games are kept and listed for review. Quarter scores are never
altered.

| Flag | Meaning |
| --- | --- |
| `OT_NOT_ITEMISED` | Overtime was played but the site does not list all of its periods. Quarters are complete; overtime points are final minus regulation |
| `PERIOD_SUM_MISMATCH` | Quarters plus overtime do not add up to the final and regulation was not level, so the figures cannot be reconciled |
| `LIST_SCORE_MISMATCH` | The match report's final differs from the daily list's final |
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

`config.yaml` holds the pace, the first date to read (`start_date`), the
season labels to keep, and the retry windows.

## Layout

```
run.py                  command line
collector/fetch.py      paced, robots-aware downloading
collector/parse.py      daily lists and scoreboards out of pages
collector/discover.py   quarter-score test, probe
collector/harvest.py    daily lists and match reports
collector/validate.py   checks and summary
collector/export.py     CSV exports and Google Sheets
collector/store.py      the CSV records
```
