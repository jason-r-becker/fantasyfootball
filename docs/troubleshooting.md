# Troubleshooting

Run draft-room commands from the repository root. The terminal's error and
printed URL are the most reliable starting points.

## Startup errors

### `Missing league configuration`

The exact `data/YEAR/LEAGUE/config.json` path does not exist. Check `--year`,
the case-sensitive league name, and the private-file transfer. League names may
contain only letters, numbers, underscores, and hyphens and may not begin with
punctuation.

### `Missing draft rankings`

The exact `data/YEAR/LEAGUE/clean.csv` path is missing. `raw.csv` and `adp.csv`
do not replace it; ask the rankings preparer for the generated `clean.csv` or
follow [Data preparation](data-preparation.md).

### `Draft slot is missing` or `Draft slot must be between ...`

The platform may not have published the order yet, or the private config still
has a placeholder. Once published, set `draft_slot` to an integer from 1
through `teams`, or pass `--pick NUMBER`. An ESPN `team_id` is not a draft slot.
Never infer the slot from last season.

### `Invalid or empty draft rankings`

`clean.csv` must contain data and a `Player` column. Restore the privately
supplied file. Other missing columns may surface later when rendering analysis;
compare it with the full contract in [Data preparation](data-preparation.md).

### JSON or integer conversion traceback

Validate `config.json` as JSON and replace every string placeholder that the app
expects to be an integer. This command checks JSON syntax without printing its
private contents:

```bash
uv run python -m json.tool data/YEAR/LEAGUE/config.json >/dev/null
```

## Platform synchronization

### The sync button is disabled

For Sleeper, `site` must be `Sleeper` and `draft_id` must be non-empty. For ESPN,
`site` must be `ESPN` and `league_id` must be non-empty. Check the private file
against [Configuration reference](configuration.md).

### `Sleeper sync needs draft_id`

The selected config has no `draft_id`. Do not substitute `league_id`; retrieve
the exact draft ID for the selected season through Sleeper's league-drafts
endpoint.

### `ESPN draft API request failed`

Confirm the year and `league_id`. A private league also needs current `swid`
and `espn_s2` cookies copied from a browser session signed in to an account that
can open the league. The displayed error is intentionally generic so an
upstream authentication response cannot expose either cookie.

The first successful sync containing picks also loads ESPN's season player
directory. Later syncs during the same app process reuse that map and request
only the draft detail.

### API offline or request failed

Continue manually. Ranked player actions, K/DST fallbacks, edit/undo,
spreadsheet refresh, optimization, and persistence stay available. Live mode
retries automatically every three seconds; practice mode retries only when you
click **Sync now**.

After service returns, sync and inspect any conflict or out-of-pool count.
Manual entries are not overwritten automatically.

### A platform pick is unmatched

The pick still advances the clock but does not remove a ranked player. This is
expected for K/DST or another player absent from `clean.csv`. If an offensive
player should match, verify names in `clean.csv` and the optional
`data/YEAR/source_player_map.json`, then correct the local log with **Edit**.

### Wrong team appears under **Your team**

For Sleeper, verify `user_id`; for ESPN, verify `team_id`. `draft_slot` controls
calculated snake turns, while the platform user/team field identifies API picks
as yours. They solve different problems.

## Browser and port issues

### Port 8765 is already in use

The app automatically binds a free port, prints it, and opens that URL. Use the
terminal's `Draft board:` URL. If a page at port 8765 returns 404, it may belong
to the other program, not this app.

Request a known alternative with:

```bash
uv run fantasy-draft \
  --year YEAR \
  --league LEAGUE \
  --practice \
  --pick DRAFT_SLOT \
  --port 8766
```

### The browser does not open

Copy the printed `Draft board:` URL into a browser, or intentionally suppress
automatic opening with `--no-browser`. Leave the terminal process running.

## Spreadsheet and saved-state issues

### A deleted CSV row was not imported

Edit the working file named on the page, not `clean.csv`, save it as CSV while
retaining the `Player` column, then click **Refresh deletions**. Practice uses
`practice_live_draft.csv`; live uses `live_draft.csv`.

### A restored row remains drafted

Row restoration reverses only picks originally imported from spreadsheet
deletions. API and web-entered picks must be corrected with **Undo** or **Edit**.

### Old picks return after restart

The hidden `.draft_app.MODE.json` session is designed to resume. Ensure the
terminal reports the intended mode. In practice, use **Reset practice draft**.
There is deliberately no live reset; back up the league directory and diagnose
the pick log before making any manual file change.

### `Saved draft session does not match the requested draft`

A session file was moved or renamed across modes, years, or leagues. Put it back
with its original draft or restore the correct private backup. Do not reuse a
session from another league.

### State after an abrupt shutdown looks inconsistent

Each file replacement is atomic, but the session, working CSV, and pick log are
three sequential writes. Restart the identical mode first; it reconstructs the
CSV views from saved state and imports intentional spreadsheet changes. Compare
the displayed log with the platform before making corrections.

## Installation and contributor checks

If `uv` cannot resolve or run the environment, confirm Python 3.14+ support and
rerun `uv sync`. Developers should use `./setup.sh`. For the complete validation
commands, see [Development and validation](development.md).
