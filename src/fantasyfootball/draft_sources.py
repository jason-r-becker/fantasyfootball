"""Read-only live draft adapters for supported fantasy platforms."""

from __future__ import annotations

import json
from threading import Lock
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from espn_api.football import League

from fantasyfootball.draft_state import DraftError
from fantasyfootball.player_matching import normalize_position

_ESPN_PLAYER_MAPS: dict[int, dict[int, dict[str, str]]] = {}
_ESPN_PLAYER_MAPS_LOCK = Lock()


def _request_json(url: str, timeout: float = 8.0) -> Any:
    request = Request(
        url, headers={"User-Agent": "fantasyfootball-draft-app/1.0"}
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            return json.load(response)
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as error:
        # The failed request URL can contain a private draft identifier. Keep
        # it in the exception chain for local debugging, but never place it in
        # the user-facing error returned to the browser.
        raise DraftError("Draft API request failed.") from error


def parse_sleeper_picks(
    payload: list[dict[str, Any]], config: dict[str, Any]
) -> list[dict[str, Any]]:
    picks = []
    user_id = str(config.get("user_id", ""))
    for raw in payload:
        metadata = raw.get("metadata") or {}
        name = (
            metadata.get("first_name", "")
            + " "
            + metadata.get("last_name", "")
        )
        pick = {
            "number": int(raw["pick_no"]),
            "player": name.strip() or metadata.get("full_name", ""),
            "position": normalize_position(
                metadata.get("position") or raw.get("position")
            ),
            "team": raw.get("draft_slot"),
            "mine": bool(user_id and str(raw.get("picked_by")) == user_id),
            "source": "Sleeper API",
            "external_id": str(raw.get("player_id", "")),
        }
        if metadata.get("team"):
            pick["player_team"] = metadata["team"]
        picks.append(pick)
    return picks


def fetch_sleeper_picks(config: dict[str, Any]) -> list[dict[str, Any]]:
    draft_id = config.get("draft_id")
    if not draft_id:
        raise DraftError("Sleeper sync needs draft_id in config.json.")
    payload = _request_json(
        f"https://api.sleeper.app/v1/draft/{draft_id}/picks"
    )
    if not isinstance(payload, list):
        raise DraftError("Sleeper returned an unexpected draft response.")
    return parse_sleeper_picks(payload, config)


def parse_sleeper_team_names(
    draft: dict[str, Any], users: list[dict[str, Any]]
) -> dict[int, str]:
    """Map Sleeper's draft slots to account display names."""
    by_user = {str(user.get("user_id")): user for user in users}
    names = {}
    for user_id, raw_slot in (draft.get("draft_order") or {}).items():
        user = by_user.get(str(user_id), {})
        metadata = user.get("metadata") or {}
        name = (
            user.get("display_name")
            or user.get("username")
            or metadata.get("team_name")
        )
        if name:
            names[int(raw_slot)] = str(name)
    return names


def fetch_sleeper_team_names(config: dict[str, Any]) -> dict[int, str]:
    draft_id = config.get("draft_id")
    league_id = config.get("league_id")
    if not draft_id or not league_id:
        return {}
    draft = _request_json(f"https://api.sleeper.app/v1/draft/{draft_id}")
    users = _request_json(
        f"https://api.sleeper.app/v1/league/{league_id}/users"
    )
    if not isinstance(draft, dict) or not isinstance(users, list):
        raise DraftError("Sleeper returned unexpected team metadata.")
    return parse_sleeper_team_names(draft, users)


def parse_espn_picks(
    payload: dict[str, Any],
    player_map: dict[int, str | dict[str, str]],
    config: dict[str, Any],
) -> list[dict[str, Any]]:
    raw_picks = (payload.get("draftDetail") or {}).get("picks") or []
    teams = int(config["teams"])
    configured_team_id = config.get("team_id")
    my_team_id = (
        int(configured_team_id) if configured_team_id not in (None, "") else -1
    )
    picks = []
    for raw in raw_picks:
        player_id = int(raw.get("playerId", -1))
        # ESPN pre-populates future slots with -1. Team defenses use other
        # negative IDs, so those are real selections and must be retained.
        if player_id == -1:
            continue
        round_number = int(raw.get("roundId", 1))
        round_pick = int(raw.get("roundPickNumber", 1))
        number = int(
            raw.get("overallPickNumber")
            or ((round_number - 1) * teams + round_pick)
        )
        team_id = int(raw.get("teamId", -1))
        identity = player_map.get(player_id, "")
        if isinstance(identity, dict):
            player_name = identity.get("name", "")
            position = identity.get("position", "")
        else:
            player_name = identity
            position = ""
        picks.append(
            {
                "number": number,
                "player": player_name,
                "position": normalize_position(position),
                "team": team_id,
                "mine": team_id == my_team_id,
                "source": "ESPN API",
                "external_id": str(player_id),
            }
        )
    return picks


def _espn_player_map(league: League, year: int) -> dict[int, dict[str, str]]:
    """Load ESPN's season player names once without caching credentials."""
    with _ESPN_PLAYER_MAPS_LOCK:
        cached = _ESPN_PLAYER_MAPS.get(year)
    if cached is not None:
        return cached

    players = league.espn_request.get_pro_players()
    if not isinstance(players, list):
        raise ValueError("ESPN returned an unexpected player response")
    position_ids = {1: "QB", 2: "RB", 3: "WR", 4: "TE", 5: "K", 16: "DST"}
    player_map = {
        int(player["id"]): {
            "name": str(player["fullName"]),
            "position": position_ids.get(
                int(player.get("defaultPositionId") or 0), ""
            ),
        }
        for player in players
        if player.get("id") is not None and player.get("fullName")
    }
    with _ESPN_PLAYER_MAPS_LOCK:
        return _ESPN_PLAYER_MAPS.setdefault(year, player_map)


def fetch_espn_picks(
    config: dict[str, Any], year: int
) -> list[dict[str, Any]]:
    if not config.get("league_id"):
        raise DraftError("ESPN sync needs league_id in config.json.")
    try:
        league = League(
            league_id=int(config["league_id"]),
            year=year,
            swid=config.get("swid"),
            espn_s2=config.get("espn_s2"),
            fetch_league=False,
        )
        payload = league.espn_request.get_league_draft()
        if not isinstance(payload, dict):
            raise ValueError("ESPN returned an unexpected draft response")
        raw_picks = (payload.get("draftDetail") or {}).get("picks", [])
        player_map = _espn_player_map(league, year) if raw_picks else {}
    except Exception as error:
        raise DraftError(
            "ESPN draft API request failed. Check the league ID, season, "
            "and private-league cookies."
        ) from error
    return parse_espn_picks(payload, player_map, config)


def fetch_platform_picks(
    config: dict[str, Any], year: int
) -> list[dict[str, Any]]:
    site = str(config.get("site", "")).upper()
    if site == "SLEEPER":
        return fetch_sleeper_picks(config)
    if site == "ESPN":
        return fetch_espn_picks(config, year)
    raise DraftError(
        f"Live draft sync is not supported for site: {site or 'missing'}"
    )


def parse_espn_team_names(payload: dict[str, Any]) -> dict[int, str]:
    """Map draft slots, not ESPN team IDs, to owner and team labels."""
    members = {str(m["id"]): m for m in payload.get("members", [])}
    teams = {int(t["id"]): t for t in payload.get("teams", [])}
    order = (payload.get("settings", {}).get("draftSettings") or {}).get(
        "pickOrder"
    ) or []
    slot_teams = dict(enumerate(order, 1))
    if not slot_teams:
        for pick in (payload.get("draftDetail") or {}).get("picks", []):
            if pick.get("roundId") == 1 and pick.get("teamId") is not None:
                slot_teams[int(pick["roundPickNumber"])] = pick["teamId"]
    names = {}
    for slot, team_id in slot_teams.items():
        team = teams.get(int(team_id), {})
        team_name = (
            team.get("name")
            or " ".join(
                str(team.get(k) or "") for k in ("location", "nickname")
            ).strip()
        )
        owners = []
        for owner_id in team.get("owners", []):
            member = members.get(str(owner_id), {})
            owner = (
                member.get("displayName")
                or " ".join(
                    str(member.get(k) or "") for k in ("firstName", "lastName")
                ).strip()
            )
            if owner and owner not in owners:
                owners.append(owner)
        owner_name = " / ".join(owners)
        label = " · ".join(
            dict.fromkeys(name for name in (owner_name, team_name) if name)
        )
        if label:
            names[slot] = label
    return names


def fetch_espn_team_names(config: dict[str, Any], year: int) -> dict[int, str]:
    try:
        league = League(
            league_id=int(config["league_id"]),
            year=year,
            swid=config.get("swid"),
            espn_s2=config.get("espn_s2"),
            fetch_league=False,
        )
        payload = league.espn_request.league_get(
            params={"view": ["mTeam", "mSettings", "mDraftDetail"]}
        )
        return parse_espn_team_names(payload)
    except Exception as error:
        raise DraftError("ESPN team metadata request failed.") from error


def fetch_platform_team_names(
    config: dict[str, Any], year: int | None = None
) -> dict[int, str]:
    """Return stable draft-slot labels when the platform exposes them."""
    if str(config.get("site", "")).upper() == "SLEEPER":
        return fetch_sleeper_team_names(config)
    if str(config.get("site", "")).upper() == "ESPN" and year is not None:
        return fetch_espn_team_names(config, year)
    return {}
