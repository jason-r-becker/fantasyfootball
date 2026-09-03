# Draft room setup

This guide covers setting up any supported league on another computer. Replace
`YEAR`, `LEAGUE`, and `DRAFT_SLOT` in commands with the values for the draft.

## 1. Install the project

Install Git and [uv](https://docs.astral.sh/uv/getting-started/installation/),
then clone the repository and install the locked environment:

```bash
git clone https://github.com/jason-r-becker/fantasyfootball.git
cd fantasyfootball
uv sync
```

Run commands from the repository root. A separate virtual-environment command
is not needed.

## 2. Prepare the league inputs

Create the league directory:

```bash
mkdir -p data/YEAR/LEAGUE
```

The draft room needs this layout:

```text
data/
└── YEAR/
    ├── source_player_map.json       # optional, recommended
    └── LEAGUE/
        ├── config.json              # required
        ├── clean.csv                # required
        └── adp.csv                  # optional, recommended
```

These files may be supplied by a rankings preparer, or most can be built using
the repository:

- Copy and complete the
  [Sleeper example](../examples/sleeper-config.example.json) or
  [ESPN example](../examples/espn-config.example.json) for `config.json`. The
  interactive cleaner can create a smaller config, but it does not ask for
  platform IDs, draft rounds, or draft slot.
- Generate `raw.csv` with the R projection script when a season
  `scoring_profiles.json` is available.
- Obtain `adp.csv` separately.
- Run `uv run python -m fantasyfootball.clean_data` to generate `clean.csv`
  and the initial `live_draft.csv`.

See [Data preparation](data-preparation.md) for exact prerequisites and outputs.

## 3. Verify the draft configuration

Before launching:

1. Match `teams`, `draft_rounds`, and the `positions` counts to the platform.
2. Confirm the draft is a standard snake draft.
3. Set `site` to `Sleeper` or `ESPN`.
4. Add the platform identifiers described in
   [Configuration reference](configuration.md).
5. Once the order is available, set the user's 1-based `draft_slot` or pass it
   with `--pick`.

The app can run in manual-only mode without platform IDs. A draft slot is still
needed for turn and roster projections.

## 4. Rehearse practice mode

```bash
uv run fantasy-draft \
  --year YEAR \
  --league LEAGUE \
  --practice \
  --pick DRAFT_SLOT
```

The terminal prints the exact URL and files in use. Work through this checklist:

1. Confirm the page shows the correct league and year and starts at the expected
   pick.
2. If platform sync is configured, click **Sync now**. Practice mode does not
   poll automatically.
3. Mark one ranked player taken, mark one pick as **Mine**, then undo and edit a
   pick.
4. Record one **+ K** and one **+ D/ST** selection.
5. Confirm **FLEX** shows eligible running backs and wide receivers.
6. Stop with `Ctrl+C`, restart with the same command, and confirm the practice
   picks resume.
7. Click **Reset practice draft** and confirm the practice board returns to its
   starting state.

The manual fallback can be rehearsed without contacting the platform:

```bash
uv run fantasy-draft \
  --year YEAR \
  --league LEAGUE \
  --practice \
  --pick DRAFT_SLOT \
  --simulate-api-down
```

If port 8765 is occupied, the app chooses a free port and prints the replacement
URL. A fixed alternative can be requested with `--port 8766`.

## 5. Run the live draft

```bash
uv run fantasy-draft \
  --year YEAR \
  --league LEAGUE \
  --live \
  --pick DRAFT_SLOT
```

Keep the platform draft page open separately and submit selections there. Live
mode polls every three seconds when synchronization is configured. Use the
manual controls during delays or outages, then use **Sync now** and review any
reported conflicts.

## 6. Stop and resume

Stop the server with `Ctrl+C`. Completed actions have already written the
session JSON, remaining-player CSV, and chronological pick log. Restart with the
same mode, year, league, slot, and round count to resume.

Practice and live state are independent. Starting without either mode flag also
selects practice, but using `--practice` makes the command clearer.
