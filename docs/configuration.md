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
| `draft_rounds` | integer or `null` | Recommended | Total rounds. `--rounds` overrides it; if it is absent or `null`, the app uses 15. |
| `draft_slot` | integer or `null` | Before operation | The user's 1-based draft-board column. `--pick` overrides it. Leave it `null` until the order is published. |
| `site` | string | Platform sync | `Sleeper` or `ESPN`, case-insensitive. Missing or unsupported values leave the room in manual-only mode. |
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
  "draft_rounds": null,
  "draft_slot": null,
  "site": "ESPN",
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
  "draft_rounds": 15,
  "draft_slot": null,
  "site": "Sleeper",
  "user_id": "REPLACE_WITH_SLEEPER_USER_ID",
  "league_id": "REPLACE_WITH_SLEEPER_LEAGUE_ID",
  "draft_id": "REPLACE_WITH_SLEEPER_DRAFT_ID"
}
```

The lineup counts above illustrate the required shape; verify them against the
league. A `null` slot is intentional. Set it to an integer after publication or
pass `--pick NUMBER`.

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
