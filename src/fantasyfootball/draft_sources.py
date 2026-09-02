"""Read-only live draft adapters for supported fantasy platforms."""

from __future__ import annotations

import json
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from espn_api.football import League

from fantasyfootball.draft_state import DraftError, normalize_position


def _request_json(url: str, timeout: float = 8.0) -> Any:
    request = Request(
        url, headers={"User-Agent": "fantasyfootball-draft-app/1.0"}
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            return json.load(response)
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as error:
        raise DraftError(f"Draft API request failed: {error}") from error


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
        picks.append(
            {
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
        )
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
    player_map: dict[int, str],
    config: dict[str, Any],
) -> list[dict[str, Any]]:
    raw_picks = payload.get("draftDetail", {}).get("picks", [])
    teams = int(config["teams"])
    my_team_id = int(config.get("team_id", -1))
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
        picks.append(
            {
                "number": number,
                "player": player_map.get(player_id, ""),
                "team": team_id,
                "mine": team_id == my_team_id,
                "source": "ESPN API",
                "external_id": str(player_id),
            }
        )
    return picks


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
        )
        payload = league.espn_request.get_league_draft()
    except Exception as error:
        raise DraftError(f"ESPN draft API request failed: {error}") from error
    return parse_espn_picks(payload, league.player_map, config)


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


def fetch_platform_team_names(config: dict[str, Any]) -> dict[int, str]:
    """Return stable draft-slot labels when the platform exposes them."""
    if str(config.get("site", "")).upper() == "SLEEPER":
        return fetch_sleeper_team_names(config)
    return {}
