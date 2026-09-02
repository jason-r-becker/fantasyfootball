# Draft room setup

This guide is for running the draft room on another computer with files shared
privately by the person who prepared the rankings. League data and settings are
intentionally excluded from Git.

## What to install

Install Git and [uv](https://docs.astral.sh/uv/getting-started/installation/),
then clone the repository and install its locked Python environment:

```bash
git clone REPOSITORY_URL
cd fantasyfootball
uv sync
```

Python is managed by uv; a separate virtual-environment command is not needed.

## Put the private league files in place

Choose a short league name containing letters, numbers, underscores, or hyphens.
For a league named `friends_league` in 2026, create:

```text
data/
└── 2026/
    └── friends_league/
        ├── config.json
        └── clean.csv
```

Receive those two files directly from the rankings preparer and place them in
that directory. Do not rename `config.json` or `clean.csv`. Do not commit the
`data/` directory; it is ignored because it holds personal settings, rankings,
and live draft history.

`config.json` must describe this specific Sleeper draft. A placeholder-only
reference is available at `examples/sleeper-config.example.json`. Verify:

- `teams` matches the league's team count.
- `positions` contains the starting `QB`, `RB`, `WR`, `TE`, and `FLEX` counts.
- `draft_rounds` matches the total number of rounds.
- `draft_slot` is this user's slot, from 1 through the team count.
- `site` is `Sleeper`.
- `user_id`, `league_id`, and `draft_id` belong to this season and league.

Sleeper IDs allow read-only synchronization; they are not passwords. They still
belong in the ignored local config rather than in source control. No Sleeper
password, session cookie, or API key is needed.

The supplied `clean.csv` is the rankings source of truth. On first launch, the
app creates its working CSV and draft state next to it. To replace projections,
stop the app and coordinate with the rankings preparer instead of editing the
column structure during a draft.

## Rehearse safely

Run practice mode first:

```bash
uv run fantasy-draft --year 2026 --league friends_league --practice
```

The browser should open automatically. Practice mode writes only
`practice_live_draft.csv`, `practice_drafted_players.csv`, and a hidden practice
session file. It does not modify `live_draft.csv`.

Test these actions before draft day:

1. Click **Sync now** and confirm Sleeper picks load.
2. Mark and undo a player manually.
3. Record one **+ K** and one **+ D/ST** fallback pick.
4. Confirm the big board's **FLEX** tab shows eligible running backs and wide
   receivers.
5. Stop the server with `Ctrl+C`, restart it, and confirm the practice picks
   return.

To rehearse without network access:

```bash
uv run fantasy-draft --year 2026 --league friends_league --practice --simulate-api-down
```

If port 8765 is busy, the app prints and opens a free replacement port. A fixed
alternative can be selected with `--port 8766`.

## Run the live draft

Only after the practice check succeeds, start live mode:

```bash
uv run fantasy-draft --year 2026 --league friends_league --live
```

Live mode is still a read-only Sleeper companion: it downloads picks but never
submits a pick to Sleeper. It does write local state immediately:

- `live_draft.csv` contains the remaining ranked players.
- `drafted_players.csv` contains the chronological pick log.
- `.draft_app.live.json` is the resumable session state.

Keep the Sleeper draft page open separately and make actual selections there.
Use the web app's manual controls if Sleeper synchronization is delayed or
unavailable. Stop the local server with `Ctrl+C`; all completed actions have
already been saved atomically.

## Safety boundaries

- Leave the default host, `127.0.0.1`, unchanged. It makes the app reachable
  only from the same computer. Do not expose it to a LAN or the internet.
- Practice and live state are separate. Start without `--live` when uncertain.
- Back up `data/YEAR/LEAGUE/` before a real draft if the machine is not already
  backed up.
- Never add league data with `git add -f`. The entire `data/` tree is private by
  design.
- `.env`, `.env.*`, credential JSON files, and private-key formats are also
  ignored. Keep real values out of examples and documentation.

## Troubleshooting

`Missing league configuration` or `Missing draft rankings` means one of the two
private files is absent or in the wrong directory. Check the exact year and
league spelling in the command.

`Sleeper sync needs draft_id` means the local `config.json` is incomplete.

An API-offline banner does not stop manual drafting. The app keeps the board,
manual player controls, K/DST fallbacks, undo, and local persistence available.

If the browser tab says 404 on port 8765, another program owns that port. Use
the replacement URL printed in the terminal or restart with `--port 8766`.
