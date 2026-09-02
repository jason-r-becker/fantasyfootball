# Fantasy football tools

Python tools for fantasy-football draft preparation, a local live draft room,
and in-season analysis.

## Install

Install [uv](https://docs.astral.sh/uv/getting-started/installation/), clone the
repository, and run:

```bash
uv sync
```

Developers can run `./setup.sh`; pass `--jupyter` only when a user-level
Jupyter kernel installation is wanted.

League data is deliberately private and is not included in Git. For a complete
setup on another computer, including the exact private files to transfer and a
practice checklist, see [Draft room setup](docs/draft-room-setup.md).

## Data

### Drafting
1.  Visit [FF Anaytics](https://apps.fantasyfootballanalytics.net/)
    Settings page and set the scoring rules for all offensive
    categories. Save as a new league. Next, on the Projections
    page, select the current year and for Week select "Season."
    Click the Download button and save as
    `data/{year}/{league_name}/raw.csv` from the root directory
    of the fantasyfootball project.

2.  Visit [Fantasy Pros](https://www.fantasypros.com/nfl/adp/overall.php)
    select the correct scoring format in the dropdown box, and save as
    `data/{year}/{league_name}/adp.csv`


### Managing

Visit [FF Anaytics](https://apps.fantasyfootballanalytics.net/)
and select the correct league from the Settings page.
On the Projections page, select the current year and week.
Click the Download button and save as
`data/{year}/{league_name}/weekly/{week_number}.csv` from the root directory
of the fantasyfootball project.


## Usage

### Drafting

from the `src/fantasyfootball` directory, run
```
python clean_data.py
```
The downloaded projection data will be joined with the ADP data
on player names. Fuzzy Logic is used, with all names below and
certain threshold printed to the screen one at a time. Follow
the instructions to map the troublesome player names. * Note that if an
important player name is incorrect, you can manually enter either
`.csv` file and change it so the mapping works correctly. For example,
"Hollywood Brown" needs to be manually changed to "Marquise Brown" or
vice versa. Once finished,
`data/{year}/{league_name}/clean.csv` and
`data/{year}/{league_name}/live_draft.csv` will be created.

#### Web draft room

The local draft room can sync picks from Sleeper or ESPN, while every manual
action remains available if the platform API is unavailable. Start in practice
mode first; it writes a separate `practice_live_draft.csv` and leaves the real
`live_draft.csv` untouched:

```bash
uv run fantasy-draft --year YEAR --league LEAGUE --practice
```

To rehearse the complete manual workflow with Sleeper deliberately unavailable:

```bash
uv run fantasy-draft --year YEAR --league LEAGUE --practice --simulate-api-down
```

Click **Sync now** to confirm the outage banner, then search or click offensive
players to mark them taken. Use **+ K** or **+ D/ST** to advance the counter for
positions that are not in the scoring pool. Every action atomically updates both
the available-player CSV and a chronological `practice_drafted_players.csv`.

Use `--live` on draft day. In live mode, picks from the API or web interface
are mirrored to the league's existing `live_draft.csv`, so the terminal and
Excel workflow is always ready as a fallback. A chronological pick log is also
written in real time to `drafted_players.csv`:

```bash
uv run fantasy-draft --year YEAR --league LEAGUE --live
```

The draft slot and draft length should be saved as `draft_slot` and
`draft_rounds` in the league's private configuration. Alternatively, pass
`--pick NUMBER` and `--rounds NUMBER` on the command line.
The server listens only on `127.0.0.1:8765` by default and opens the draft room
in a browser. If that port is occupied, it prints and opens a free local port
automatically. Use **Sync now** for the configured platform, click any
player to record a manual pick, or undo a pick and record the correction.
Manual entries are locked against conflicting API updates. The page also shows
the exact CSV path; after deleting rows in Excel, click **Refresh deletions**
to import them into the draft log.

This is a read-only draft companion: it never submits a selection to Sleeper
or ESPN. It reads platform picks and marks those players taken locally. The
decision controls appear above a collapsible big board and include the original
position drop-off chart plus a projected-team optimizer. Optimization runs
automatically when your slot is on the clock and can be rerun manually at any
pick to preview the rest of the starting roster.

The working layout combines a sticky command deck with an off-canvas big-board
drawer. The clock, optimizer lead, manual override, sync, optimization, and
board controls remain visible at the top; **Open board** slides rankings in
from the right without displacing the analysis workspace.

The drop-off chart has independent optional settings for its Y-axis metric,
minimum and maximum Y bounds, and the number of position ranks displayed.
These browser-local settings do not change the optimizer's scoring metric and
can be reset to `VOR_Points` with automatic bounds.

When the platform-specific ADP column is blank for a player, the draft room
uses the FantasyPros consensus `AVG` value as a planning fallback and reports
the fallback count on the board. Existing platform ADP values always win.

Sleeper live sync requires `draft_id`; ESPN sync requires `league_id` and may
require `swid` and `espn_s2` for private leagues. Credentials stay server-side,
are never sent to the browser, and belong only in ignored local files.


### Managing
