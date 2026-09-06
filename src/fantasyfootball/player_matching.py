"""Deterministic player identity matching shared across data sources."""

from __future__ import annotations

import json
import math
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping


class PlayerMatchError(ValueError):
    """Player identities or aliases are ambiguous or invalid."""


def normalize_player_name(value: str) -> str:
    """Normalize spelling differences without guessing player identity."""
    value = unicodedata.normalize("NFKD", value or "")
    value = "".join(
        character
        for character in value
        if not unicodedata.combining(character)
    )
    value = value.replace("’", "'").lower()
    value = re.sub(r"\b(jr|sr|ii|iii|iv)\.?\b", "", value)
    return re.sub(r"[^a-z0-9]+", "", value)


def normalize_position(value: str | None) -> str:
    """Normalize source defense labels to the draft room's DST label."""
    position = str(value or "").upper().replace("/", "")
    if position in {"DEF", "DST"}:
        return "DST"
    return "K" if position == "PK" else position


def normalize_team(value: str | None) -> str:
    """Normalize common NFL team-code variants used by data providers."""
    team = re.sub(r"[^A-Z0-9]+", "", str(value or "").upper())
    aliases = {
        "GBP": "GB",
        "JAC": "JAX",
        "KCC": "KC",
        "LA": "LAR",
        "LVR": "LV",
        "NEP": "NE",
        "NOS": "NO",
        "SFO": "SF",
        "STL": "LAR",
        "SD": "LAC",
        "OAK": "LV",
        "TBB": "TB",
        "WSH": "WAS",
    }
    return aliases.get(team, team)


def _optional_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and math.isnan(value):
        return ""
    text = str(value).strip()
    return "" if text.lower() in {"nan", "none", "<na>"} else text


def _first_text(raw: Mapping[str, Any], *keys: str) -> str:
    for key in keys:
        text = _optional_text(raw.get(key))
        if text:
            return text
    return ""


@dataclass(frozen=True)
class PlayerIdentity:
    """A canonical ranked player and optional validation attributes."""

    name: str
    position: str = ""
    team: str = ""


@dataclass(frozen=True)
class PlayerMatch:
    """The result of one deterministic source-to-canonical lookup."""

    source_name: str
    canonical: str | None
    method: str | None = None
    reason: str | None = None


def load_player_aliases(path: Path) -> dict[str, str]:
    """Load a season alias object, returning an empty map when absent."""
    try:
        aliases = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as error:
        raise PlayerMatchError(
            f"Could not read player aliases: {error}"
        ) from error
    if not isinstance(aliases, dict):
        raise PlayerMatchError(
            "source_player_map.json must contain a JSON object."
        )
    return {str(source): str(target) for source, target in aliases.items()}


class PlayerMatcher:
    """Resolve exact normalized names and explicitly confirmed aliases."""

    def __init__(
        self,
        players: Iterable[PlayerIdentity | Mapping[str, Any]],
        aliases: Mapping[str, str] | None = None,
    ) -> None:
        self._players: dict[str, PlayerIdentity] = {}
        self._aliases: dict[str, str] = {}
        collisions: list[str] = []

        for raw in players:
            identity = self._identity(raw)
            key = normalize_player_name(identity.name)
            if not key:
                collisions.append("a canonical player has an empty name")
                continue
            previous = self._players.get(key)
            if previous:
                if previous.name == identity.name:
                    collisions.append(
                        f"canonical player {identity.name!r} appears more "
                        "than once"
                    )
                else:
                    collisions.append(
                        f"canonical names {previous.name!r} and "
                        f"{identity.name!r} normalize to the same value"
                    )
                continue
            self._players[key] = identity

        for source, target in (aliases or {}).items():
            source_text = str(source).strip()
            target_key = normalize_player_name(str(target))
            canonical = self._players.get(target_key)
            # source_player_map.json is also used historically for defenses and
            # other mappings. Only entries targeting this canonical player pool
            # are player aliases for the current operation.
            if canonical is None or len(source_text.split()) < 2:
                continue
            source_key = normalize_player_name(source_text)
            if not source_key:
                continue
            exact = self._players.get(source_key)
            if exact and exact.name != canonical.name:
                collisions.append(
                    f"alias {source_text!r} conflicts with canonical player "
                    f"{exact.name!r}"
                )
                continue
            previous_key = self._aliases.get(source_key)
            if previous_key and previous_key != target_key:
                collisions.append(
                    f"aliases normalizing to {source_key!r} target multiple "
                    "players"
                )
                continue
            if exact is None:
                self._aliases[source_key] = target_key

        if collisions:
            details = "; ".join(collisions)
            raise PlayerMatchError(f"Unsafe player-name collision: {details}.")

    @staticmethod
    def _identity(
        raw: PlayerIdentity | Mapping[str, Any],
    ) -> PlayerIdentity:
        if isinstance(raw, PlayerIdentity):
            return raw
        return PlayerIdentity(
            name=_first_text(raw, "name", "Player"),
            position=normalize_position(
                _first_text(raw, "position", "Position")
            ),
            team=normalize_team(_first_text(raw, "team", "Team")),
        )

    @property
    def alias_count(self) -> int:
        return len(self._aliases)

    @property
    def players(self) -> tuple[PlayerIdentity, ...]:
        return tuple(self._players.values())

    def match(
        self,
        source_name: str,
        *,
        position: str | None = None,
        team: str | None = None,
    ) -> PlayerMatch:
        """Match exactly or by alias, rejecting attribute conflicts."""
        source_key = normalize_player_name(source_name)
        target_key = source_key if source_key in self._players else None
        method = "exact" if target_key else None
        if target_key is None:
            target_key = self._aliases.get(source_key)
            method = "alias" if target_key else None
        if target_key is None:
            return PlayerMatch(source_name, None, reason="unmatched name")

        canonical = self._players[target_key]
        source_position = normalize_position(position)
        if (
            source_position
            and canonical.position
            and source_position != canonical.position
        ):
            return PlayerMatch(
                source_name,
                None,
                reason=(
                    f"position conflict ({source_position} vs "
                    f"{canonical.position})"
                ),
            )
        source_team = normalize_team(team)
        if source_team and canonical.team and source_team != canonical.team:
            return PlayerMatch(
                source_name,
                None,
                reason=f"team conflict ({source_team} vs {canonical.team})",
            )
        return PlayerMatch(source_name, canonical.name, method=method)

    def compatible_names(
        self, *, position: str | None = None, team: str | None = None
    ) -> list[str]:
        """Return names passing available position and team safeguards."""
        source_position = normalize_position(position)
        source_team = normalize_team(team)
        return [
            player.name
            for player in self._players.values()
            if (
                not source_position
                or not player.position
                or player.position == source_position
            )
            and (
                not source_team
                or not player.team
                or player.team == source_team
            )
        ]
