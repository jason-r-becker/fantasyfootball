# Draft room usage

The web draft room is a local decision and tracking companion. It reads
platform picks and maintains local state; it never submits selections.

## Practice versus live

| Behavior | Practice | Live |
| --- | --- | --- |
| Explicit flag | `--practice` | `--live` |
| Platform updates | **Sync now** only | Automatic every 3 seconds, plus **Sync now** |
| Remaining-player file | `practice_live_draft.csv` | `live_draft.csv` |
| Pick log | `practice_drafted_players.csv` | `drafted_players.csv` |
| Session | `.draft_app.practice.json` | `.draft_app.live.json` |
| Reset button | Available | Not available |

Omitting both mode flags selects practice. Prefer the explicit flag so the
terminal command records your intent.

## What happens at startup

The app reads `config.json` and immutable rankings from `clean.csv`. It then:

1. fills missing platform ADP values from matching `Player`/`AVG` rows in
   `adp.csv`, when available;
2. loads season aliases from `data/YEAR/source_player_map.json`, when available;
3. creates or resumes the selected mode's hidden session JSON;
4. imports deletions or restorations already present in that mode's working CSV;
5. rewrites the working CSV and chronological pick log from current state.

On the first ESPN synchronization that contains picks, the app downloads the
season player-name map once. Later synchronization requests in the same process
fetch only the current draft detail. Restarting the app clears this memory-only
cache.

Because an existing working CSV is read on startup, close spreadsheet editors
cleanly and inspect unexpected row deletions before continuing.

## Track picks

- **Sync now** reads all currently published platform picks and merges missing
  ones by overall pick number.
- **Mark taken** records the next unfilled overall pick. Select **Mine** when
  making a manual entry for your roster; it defaults on when the configured
  snake slot is on the clock.
- Clicking **Mark taken** on the big board records the player but uses computed
  pick ownership for the **Mine** value.
- **+ K** and **+ D/ST** advance the next pick without removing a player from
  `clean.csv`, because those positions are outside the ranked offensive pool.
- **Undo** removes a local pick and restores a matched player to the board.
- **Edit** replaces a recorded pick with a ranked player and locks the
  correction against later platform conflicts.
- **Refresh deletions** treats rows manually deleted from the working CSV as
  drafted. Restoring a row reverses only a pick whose source was the
  spreadsheet; it does not silently remove API or web-entered picks.

Manual web entries, manual K/DST entries, spreadsheet deletions, and edits are
locked. A later API sync reports a conflict at that pick number rather than
overwriting local intent. Resolve the local entry with **Edit** or **Undo**, then
sync again.

An API player absent from `clean.csv` still advances the pick counter and
appears as unmatched in the log, but it cannot remove a ranked row. This is the
normal path for kickers, defenses, or an out-of-pool player received from the
platform.

## Use the analysis

The board ranks the available pool by the selected metric. The default is
`VOR_Points`; changing the board metric also changes the optimizer metric.

The **FLEX** board and optimizer treat only RB and WR as FLEX-eligible. TE is
not included, even if a platform league permits it. K and DST are not optimized.

The projected-team optimizer runs automatically on your turns. After any run
completes in under 500 ms, it switches to automatic calculation after every
pick for the rest of that saved session. The league-strength forecast becomes
active in round 4 after that fast mode is reached. **Avoid** removes a player
from decision-support results without marking that player drafted.

The drop-off chart has browser-local controls for metric, Y-axis bounds, and
position-rank count. These chart settings do not change the optimizer. Resetting
them restores `VOR_Points`, automatic bounds, and 18 ranks.

## Files and persistence

All generated files live beside the league config and rankings:

| File | Purpose |
| --- | --- |
| `.draft_app.practice.json` | Resumable practice state. |
| `practice_live_draft.csv` | Remaining ranked players in practice. |
| `practice_drafted_players.csv` | Chronological practice pick log. |
| `.draft_app.live.json` | Resumable live state. |
| `live_draft.csv` | Remaining ranked players in live mode and legacy spreadsheet fallback. |
| `drafted_players.csv` | Chronological live pick log, including source, match, lock, team, and timestamp metadata. |

Pick changes, exclusions, metric changes, spreadsheet imports, and practice
resets immediately rewrite the session, available-player CSV, and pick-log CSV.
Each individual file is written to a temporary file and then atomically
replaced, so readers do not see a partially written file. The three files are
updated sequentially rather than as one cross-file transaction; after an abrupt
power loss, restart the same mode and verify all three views. Optimizer timing
and fetched team labels update only the session JSON because they do not change
either CSV.

`clean.csv` is never reduced as picks arrive. It remains the source rankings
used to reconstruct the working CSV. Practice mode does not write the live
session or live CSVs.

## API-offline fallback

If the status says **API offline — manual ready**:

1. Keep entering ranked players with **Mark taken**.
2. Use **+ K** or **+ D/ST** for selections outside the ranked pool.
3. Optionally delete ranked-player rows in the displayed working CSV and click
   **Refresh deletions**.
4. When connectivity returns, click **Sync now**. Review conflicts instead of
   replacing manual work blindly.

Draft tracking and local analysis do not depend on a successful sync request.
