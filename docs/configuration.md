# Configuration reference

Each league has a private configuration at
`data/YEAR/LEAGUE/config.json`. The league name used in `--league` must begin
with a letter or number and contain only letters, numbers, underscores, and
hyphens.

Copy the placeholder
[Sleeper example](../examples/sleeper-config.example.json) or
[ESPN example](../examples/espn-config.example.json) as a starting point.

## Draft room fields

| Field | JSON type | Required when | Meaning |
| --- | --- | --- | --- |
| `teams` | integer | Always | Number of draft slots. It controls snake-pick ownership and the final pick number. |
| `positions` | object | Full analysis | Starting counts for `QB`, `RB`, `WR`, `TE`, and `FLEX`. Use zero for an unused slot. Kicker and defense are intentionally not optimized. |
| `flex_positions` | array | Recommended with FLEX | Positions eligible for `FLEX`; entries may be `QB`, `RB`, `WR`, or `TE`. The backward-compatible default is `RB` and `WR`, so declare the platform rule explicitly. |
| `draft_rounds` | integer or `null` | Recommended | Total rounds. `--rounds` overrides it; if it is absent or `null`, the app uses 15. |
| `draft_slot` | integer or `null` | Before operation | The user's 1-based draft-board column. `--pick` overrides it. Leave it `null` until the order is published. |
| `site` | string | Platform sync | `Sleeper` or `ESPN`, case-insensitive. Missing or unsupported values leave the room in manual-only mode. |
| `adp_model` | object | Always | Selects Fantasy Football Calculator ADP or explicitly disables it. For FFC, `source` is `ffc`, `format` is `standard`, `half-ppr`, `ppr`, or `2-qb`, and optional `offline` is a boolean. The top-level `teams` supplies league size. |
| `draft_id` | string | Sleeper pick sync | Identifies the exact Sleeper draft whose picks will be read. |
| `user_id` | string | Recommended for Sleeper | Lets API picks by this user populate **Your team**. It is not needed to download picks. |
| `league_id` | string | ESPN pick sync; recommended for Sleeper | Identifies the ESPN league whose picks will be read. For Sleeper, it is used only to fetch owner display names. |
| `team_id` | integer or `null` | Recommended for ESPN | Identifies which ESPN team is yours. It is separate from the draft slot. |
| `swid` | string | Private ESPN leagues only | ESPN authentication cookie, if the league cannot be read publicly. |
| `espn_s2` | string | Private ESPN leagues only | ESPN authentication cookie, if the league cannot be read publicly. |
| `waiver_weekly_position_value` | object | Optional rankings preparation | Per-position weekly replacement values used by `clean_data.py`; absent values are estimated from the draft pool. |

For a useful draft room, treat `teams`, every listed `positions` key,
`draft_rounds`, and the final `draft_slot` as required even where the code has a
fallback. Values supplied with `--pick` and `--rounds` take precedence for that
launch and are included when the mode's session is next persisted.

The draft room models a standard snake order. It does not currently model
auction drafts, third-round reversal, traded-pick ownership, or arbitrary draft
orders. Confirm the Sleeper draft's `type` is `snake` before relying on turn and
roster projections.

## FFC ADP feed and availability model

FFC is the default and only automated ADP feed. Configure its scoring family
explicitly for every league; it cannot be inferred reliably from `site`,
`clean.csv`, or roster settings:

```json
"adp_model": {
  "source": "ffc",
  "format": "ppr",
  "offline": false
}
```

Use `ppr` for one point per reception, `half-ppr` for half a point, `standard`
for no reception points, or `2-qb` for FFC's two-quarterback population. Verify
this value against the platform's scoring rules. Those are the four FFC
populations supported by this app; FFC's separate Dynasty and Rookie feeds are
not accepted. `site` independently chooses Sleeper or ESPN pick synchronization;
it never changes the ADP population. The FFC request uses only `format`,
`teams`, and the launched year. It sends no league ID, draft ID, user ID, ESPN
cookie, roster, or other private value.

FFC does not accept the league's lineup, bench, FLEX, kicker, or defense rules,
and its response metadata does not identify the source mock-draft roster
template. The resulting population is league-specific only by year, team count,
and scoring family. A `position` query can filter returned rows, but cannot
rebuild the underlying drafts with or without kickers. This project excludes
FFC K/DST rows from the offensive board; it does not apply an invented offset
to player means or deviations. Treat late-round likelihoods as approximate
when the league's special-team or bench rules differ from the FFC population.
Set `draft_rounds` to the league's complete number of picks, including K/DST
slots when present.

FFC supplies mean ADP, standard deviation, observed earliest/latest picks, and
the number of times each player was selected by a human. The app fits a
lower-bounded normal distribution with a half-pick continuity correction and
reports the probability that the player remains available at the target pick.
Samples below 25 selections are linearly shrunk toward the fallback estimate;
25 or more selections use the fitted model without legacy shrinkage. Missing
players and distributions with a missing or nonpositive standard deviation use
the fallback estimate entirely. That fallback applies the existing
league-size/ADP heuristic, or 50% when no saved ADP exists. Observed
earliest/latest picks are retained as source metadata but are not treated as
hard bounds.

When a player matches, FFC supplies the displayed ADP and bye week and the
optimizer uses FFC mean ADP to order projected opponent selections. The cleaner
stores ADP, standard deviation, sample count, earliest/latest observations, and
source provenance in `clean.csv`. On launch, the room refreshes matched ADP and
bye values in memory and in the mode's working CSV; immutable `clean.csv`
changes only when the cleaner is run.

The local source cache is reused for up to 12 hours and stored privately as
`data/YEAR/LEAGUE/.ffc_adp.json`; FFC says its upstream ADP data updates once
per day. A refresh writes atomically. If FFC is unavailable, the app uses a
stale matching cache when one exists and otherwise keeps an existing prepared
draft room operational with saved `clean.csv` ADP and fallback estimates. Set
`"offline": true` to prohibit an FFC request and use only a matching cache.
With no matching cache, the cleaner stops, while an existing draft room remains
usable from `clean.csv`. The header badge identifies live, cached, stale,
offline, unavailable, and disabled behavior. If FFC's reported round count
differs from `draft_rounds`, the badge warns about the mismatch; the league's
own round and lineup rules still control the optimizer.

To deliberately run without FFC, use:

```json
"adp_model": {"source": "disabled"}
```

For an existing draft room, this escape hatch preserves ADP already stored in
`clean.csv`. Running the cleaner while FFC is disabled regenerates `clean.csv`
with blank ADP fields; it does not preserve an older file or restore the removed
manual ADP-import workflow. Omitting `adp_model` is an error so an older config
cannot silently assume the wrong scoring format.

The feed is provided by the
[Fantasy Football Calculator ADP REST API](https://help.fantasyfootballcalculator.com/article/42-adp-rest-api).
FFC says its ADP is derived from human mock-draft selections after computer
selections are removed. See its
[ADP methodology](https://help.fantasyfootballcalculator.com/article/34-average-draft-position-adp-data).

## Find the Sleeper values

Sleeper's official API is read-only and requires no API token. The endpoint
sequence below follows the [Sleeper API documentation](https://docs.sleeper.com/).
Replace every uppercase placeholder.

1. Resolve the Sleeper username to a stable `user_id`:

   ```bash
   curl "https://api.sleeper.app/v1/user/YOUR_SLEEPER_USERNAME"
   ```

2. List that user's leagues for the selected season and identify the league by
   its `name`. Copy the matching `league_id`:

   ```bash
   curl "https://api.sleeper.app/v1/user/YOUR_USER_ID/leagues/nfl/YEAR"
   ```

3. List drafts for that league. A league can have more than one, so select the
   entry with the correct season and draft metadata rather than assuming the
   first ID forever:

   ```bash
   curl "https://api.sleeper.app/v1/league/YOUR_LEAGUE_ID/drafts"
   ```

   Copy its `draft_id`. Verify `type`, `settings.teams`, and
   `settings.rounds` against the local config.

4. Retrieve the exact draft and inspect `draft_order`:

   ```bash
   curl "https://api.sleeper.app/v1/draft/YOUR_DRAFT_ID"
   ```

   `draft_order` maps user IDs to 1-based draft slots. Find the property whose
   key equals `YOUR_USER_ID`; its value is `draft_slot`. If `draft_order` is
   `null` or does not contain the user yet, Sleeper has not published a usable
   order. Leave `draft_slot` as `null`, wait, and do not launch live mode until
   the slot is known.

The league-list response may also contain a `draft_id`, but use the
league-drafts endpoint to distinguish multiple drafts.

## Find the ESPN values

The draft room uses the read-only
[`espn-api` package](https://github.com/cwendt94/espn-api) for ESPN leagues.
ESPN does not require a separate API key.

1. Open the league in a desktop browser and copy the `leagueId` value from the
   ESPN URL into `league_id`.
2. Open your team page and copy its `teamId` URL value into `team_id`. This
   identifies which synchronized picks are yours; it is not necessarily your
   position in the draft order.
3. For a public league, leave `swid` and `espn_s2` empty. For a private league,
   sign in to ESPN, open the browser developer tools, select **Application** or
   **Storage**, open the cookies for `fantasy.espn.com`, and copy the exact
   `SWID` and `espn_s2` cookie values into the matching lowercase config fields.
   Keep the braces around `SWID` when ESPN supplies them. The upstream package
   maintains [cookie retrieval instructions](https://github.com/cwendt94/espn-api/discussions/150).
4. In ESPN's draft settings, confirm the format is snake and inspect the
   published order. Count from 1 to find your `draft_slot`. Leave it `null`
   until the order is final; `team_id` cannot substitute for it.
5. Set `draft_rounds` to the number of selections each team will make. This is
   normally the number of draftable roster spots, excluding IR. A `null` value
   uses the 15-round fallback, so verify the real value before live mode.

The tracked ESPN example deliberately leaves the team, slot, rounds, and
cookies unset. A private local config has this shape:

```json
{
  "teams": 12,
  "positions": {
    "QB": 1,
    "RB": 2,
    "WR": 2,
    "TE": 1,
    "FLEX": 1
  },
  "flex_positions": ["RB", "WR", "TE"],
  "draft_rounds": null,
  "draft_slot": null,
  "site": "ESPN",
  "adp_model": {
    "source": "ffc",
    "format": "ppr",
    "offline": false
  },
  "league_id": "REPLACE_WITH_ESPN_LEAGUE_ID",
  "team_id": null,
  "swid": "",
  "espn_s2": ""
}
```

## Sleeper example

The tracked example contains no real IDs and deliberately leaves the slot
unknown:

```json
{
  "teams": 12,
  "positions": {
    "QB": 1,
    "RB": 2,
    "WR": 2,
    "TE": 1,
    "FLEX": 1
  },
  "flex_positions": ["RB", "WR", "TE"],
  "draft_rounds": 15,
  "draft_slot": null,
  "site": "Sleeper",
  "adp_model": {
    "source": "ffc",
    "format": "ppr",
    "offline": false
  },
  "user_id": "REPLACE_WITH_SLEEPER_USER_ID",
  "league_id": "REPLACE_WITH_SLEEPER_LEAGUE_ID",
  "draft_id": "REPLACE_WITH_SLEEPER_DRAFT_ID"
}
```

The lineup counts, FLEX eligibility, and `ppr` model above illustrate the
required shape; verify all three against the league. A `null` slot is
intentional. Set it to an integer after publication or pass `--pick NUMBER`.

## Login and `.env`

Sleeper's draft endpoints require no login, API token, or environment variables.
Only the identifiers above are needed. ESPN public leagues also need no login;
private ESPN leagues may require `swid` and `espn_s2` cookies in `config.json`.
The ESPN adapter sanitizes synchronization errors before returning them to the
browser, so upstream authentication messages cannot echo cookie values.

The current code does not load `.env`. The tracked
[`.env.example`](../.env.example) contains no required settings because the
projection generator, ranking cleaner, and Sleeper draft room do not consume
environment variables.

## Command-line precedence

Show all current flags with:

```bash
uv run fantasy-draft --help
```

Important defaults and overrides:

- `--year` defaults to 2026; specifying it makes commands repeatable.
- No default exists for `--league`.
- `--pick` overrides `draft_slot`.
- `--rounds` overrides `draft_rounds`.
- `--metric` defaults to `VOR_Points`.
- `--practice` and `--live` are mutually exclusive; neither means practice.
- `--simulate-api-down` is practice-only.
