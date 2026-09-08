# Data preparation

Most draft-room operators should receive prepared private files rather than
regenerate rankings. This page separates the minimum transfer from the
maintainer workflow.

## Input and output map

The draft room requires:

```text
data/YEAR/LEAGUE/config.json
data/YEAR/LEAGUE/clean.csv
```

The optional season-wide alias file is:

```text
data/YEAR/source_player_map.json
```

Every league config must select a verified FFC scoring format or explicitly
disable FFC. When enabled, the app creates
`data/YEAR/LEAGUE/.ffc_adp.json`, a private atomic cache scoped to that year,
team count, and format. No login or API key is used.

`source_player_map.json` maps source names to canonical projection names. The
cleaner and draft room share the same deterministic matcher: normalized exact
name first, then a confirmed alias. Available position and NFL team values must
also agree. Fuzzy results are suggestions shown by the interactive cleaner and
are saved only after explicit confirmation; the live room never makes a fuzzy
guess. Ambiguous canonical names and alias collisions stop the operation.

For example, a confirmed FFC spelling difference has this shape:

```json
{
  "FFC Player Name": "Projection Player Name"
}
```

The value must match a player in the projection rankings. Keep existing
confirmed entries when adding another alias because the file is season-wide.

What the repository can create:

| Artifact | Generated? | Prerequisites |
| --- | --- | --- |
| League directory | No | Create it with `mkdir -p data/YEAR/LEAGUE`. |
| `config.json` | No | Copy the matching example and verify every league rule and platform field. |
| `raw.csv` | Yes | R, `ffanalytics`, `jsonlite`, and a private season scoring profile. It can also be supplied manually. |
| `.ffc_adp.json` | Yes | Internet access, or retain a matching cache for explicit offline mode. |
| `clean.csv` | Yes | `raw.csv`, `config.json`, and available FFC data unless FFC is explicitly disabled. |
| `source_player_map.json` | Partly | Confirmed aliases can be added by the cleaner and lineup workflow. |
| Working CSVs and session JSON | Yes | Starting and operating the draft room creates and updates them. |

None of these paths reads `.env` or logs in to Sleeper. The R script retrieves
projection sources through `ffanalytics`; its use of FantasyPros as one member
of a projection ensemble is unrelated to ADP and remains supported.

## `clean.csv` contract

A file produced by the current cleaner contains:

```text
Player, Team, Position, Bye, VOR_Floor, VOR_Points, VOR_Ceiling,
Floor, Points, Ceiling, FLEX_VOR_Floor, FLEX_VOR_Points,
FLEX_VOR_Ceiling, Std Dev, ADP, ADP Source, ADP Std Dev,
ADP Samples, ADP Earliest, ADP Latest
```

The first unnamed CSV column is the saved row index. Existing older
`clean.csv` files remain readable because the distribution provenance columns
are optional at runtime. Player names must be nonblank and unambiguous after
normalization. Draft-room positions are `QB`, `RB`, `WR`, and `TE`; K and DST
rows in `raw.csv` and the FFC response are deliberately excluded from the
ranked pool, then tracked separately as unranked selections during the draft.
FLEX VOR uses only the positions declared in `flex_positions`, including their
configured base starters when choosing the replacement tier.

FFC values populate `ADP` and `Bye`; the additional ADP columns preserve the
distribution used during preparation. Players absent from FFC remain in the
rankings with blank FFC fields and use the draft room's legacy fallback.
FFC does not expose a source roster template or a per-league kicker switch, so
its distributions cannot be recalibrated exactly for leagues that omit or add
K/DST slots. See the limitation in the
[configuration reference](configuration.md#ffc-adp-feed-and-availability-model).

## Prepare season rankings

Place these private inputs in the league directory:

- `raw.csv` — season projections with `player`, `team`, `position`, `points`,
  `floor`, `ceiling`, and `sd_pts` columns.
- `config.json` — team count, lineup rules, explicit FFC format, and draft-room
  settings described in [Configuration reference](configuration.md).

From the repository root, run:

```bash
uv run python -m fantasyfootball.clean_data
```

Enter the year and exact league directory name. The cleaner calculates value
over replacement, loads or refreshes the league-scoped FFC cache, performs safe
player matching, and writes `clean.csv` plus an initial `live_draft.csv`.
Review every fuzzy suggestion. Answering no leaves the player unmatched;
nothing is silently accepted. The final summary reports matched, conflicting,
and unmatched source players.

For a disconnected preparation run, first obtain a matching cache while
online, then set `adp_model.offline` to `true`. If the matching cache is absent
or invalid, preparation stops instead of producing rankings from an unintended
population. `{"source": "disabled"}` is an explicit escape hatch and produces
blank FFC fields; it does not restore the removed manual ADP-import workflow.

The tracked R script can generate `raw.csv` for every league described in the
private `data/YEAR/scoring_profiles.json`:

```bash
Rscript scripts/generate_ffanalytics_projections.R --year=YEAR
```

It requires R plus the `ffanalytics` and `jsonlite` packages, stores a private
season cache at `data/YEAR/ffanalytics_scrape.rds`, and overwrites each listed
league's `raw.csv`. Use `--refresh` only when a fresh source scrape is intended.
R is not required to install the app, use supplied rankings, fetch FFC ADP, or
run either draft-room adapter.

## Before transferring rankings

1. Verify `teams`, `draft_rounds`, `positions`, `flex_positions`, and the FFC
   format against the platform's current settings.
2. Open `clean.csv`; confirm it is non-empty and has the complete core columns.
3. Check the cleaner's conflict and unmatched counts, blank or duplicate names,
   unexpected positions, and FFC coverage through the rounds that matter.
4. Launch practice mode and confirm the FFC badge and displayed ADP values.
5. Exercise both platform synchronization and manual fallback with synthetic or
   disposable state; never modify saved live history for a rehearsal.
6. Stop the server before copying files. Transfer only `config.json`,
   `clean.csv`, and the optional season alias file unless an intentional resume
   or offline cache is being transferred.

## Weekly projection files

Scrape weekly projections for every saved scoring profile with:

```bash
Rscript scripts/generate_ffanalytics_projections.R --year=2026 --week=1 --refresh
```

Replace the year and week as needed. Weekly mode supports weeks 1–18 and
includes QB, RB, WR, TE, K, and DST. It scores the downloaded statistics using
each league's saved rules and writes the weekday-stamped files below. Its
`data/YEAR/ffanalytics_scrape_wkWEEK.rds` cache is separate from the season
cache; it does not overwrite `raw.csv` or draft rankings. Omit `--refresh` to
reuse the matching weekly cache. Omitting `--week` retains season mode.

In-season lineup analysis uses this current destination pattern:

```text
data/YEAR/LEAGUE/weekly_projections/projections_YEAR_wkWEEK*.csv
```

For a selected year and week, the downloaded base name is
`projections_YEAR_wkWEEK.csv`. The tool moves it from `Downloads` to a
weekday-stamped name such as `projections_YEAR_wkWEEK_d1.csv`; among those
stamped files, the lexically latest match is used. The older per-week directory
layout is obsolete.

Run the lineup tool with explicit values:

```bash
uv run python -m fantasyfootball.set_lineup \
  --year YEAR \
  --week WEEK \
  --league LEAGUE
```

The tool first looks in the current user's `Downloads` directory for the base
projection filename and, if found, moves it into `weekly_projections/` with a
weekday suffix.
