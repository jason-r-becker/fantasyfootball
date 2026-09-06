"""Fantasy Football Calculator draft-position data and distributions."""

from __future__ import annotations

import json
import math
import os
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from fantasyfootball.player_matching import (
    normalize_player_name,
    normalize_position,
)

FFC_API = "https://fantasyfootballcalculator.com/api/v1/adp"
FFC_FORMATS = {"standard", "half-ppr", "ppr", "2-qb"}
CACHE_MAX_AGE = timedelta(hours=12)
CACHE_FUTURE_TOLERANCE = timedelta(minutes=5)


class ADPModelError(ValueError):
    """An invalid ADP-model configuration."""


@dataclass(frozen=True)
class PlayerDraftDistribution:
    """Summary of observed human draft positions for one player."""

    name: str
    mean: float
    stdev: float | None
    samples: int
    earliest: int | None = None
    latest: int | None = None
    team: str | None = None
    position: str | None = None
    bye: int | None = None


@dataclass(frozen=True)
class ADPModel:
    """Loaded FFC metadata and player distributions."""

    enabled: bool
    available: bool
    status: str
    scoring_format: str | None
    teams: int
    rounds: int | None = None
    total_drafts: int | None = None
    start_date: str | None = None
    end_date: str | None = None
    fetched_at: str | None = None
    players: tuple[PlayerDraftDistribution, ...] = ()
    message: str | None = None

    def public_info(self, matched_players: int = 0) -> dict[str, Any]:
        """Return metadata safe to display in the local browser."""
        return {
            "enabled": self.enabled,
            "available": self.available,
            "source": "FFC" if self.enabled else None,
            "status": self.status,
            "format": self.scoring_format,
            "teams": self.teams,
            "rounds": self.rounds,
            "total_drafts": self.total_drafts,
            "start_date": self.start_date,
            "end_date": self.end_date,
            "fetched_at": self.fetched_at,
            "source_players": len(self.players),
            "matched_players": matched_players,
            "message": self.message,
        }


def _integer(value: Any) -> int | None:
    try:
        return int(value)
    except TypeError, ValueError:
        return None


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except TypeError, ValueError:
        return None
    return number if math.isfinite(number) else None


def _configured_settings(
    config: dict[str, Any],
) -> tuple[str | None, bool]:
    settings = config.get("adp_model")
    if settings is None:
        raise ADPModelError(
            "adp_model is required; configure FFC format or explicitly "
            "set its source to 'disabled'."
        )
    if not isinstance(settings, dict):
        raise ADPModelError("adp_model must be a JSON object.")
    source = str(settings.get("source", "ffc")).lower()
    if source in {"", "none", "disabled"}:
        return None, bool(settings.get("offline", False))
    if source != "ffc":
        raise ADPModelError("adp_model.source must be 'ffc' or 'disabled'.")
    scoring_format = str(settings.get("format", "")).lower()
    aliases = {
        "half": "half-ppr",
        "halfppr": "half-ppr",
        "half_ppr": "half-ppr",
        "non-ppr": "standard",
        "nonppr": "standard",
        "2qb": "2-qb",
    }
    scoring_format = aliases.get(scoring_format, scoring_format)
    if scoring_format not in FFC_FORMATS:
        choices = ", ".join(sorted(FFC_FORMATS))
        raise ADPModelError(f"adp_model.format must be one of: {choices}.")
    offline = settings.get("offline", False)
    if not isinstance(offline, bool):
        raise ADPModelError("adp_model.offline must be true or false.")
    return scoring_format, offline


def _request_ffc(scoring_format: str, teams: int, year: int) -> dict[str, Any]:
    url = f"{FFC_API}/{scoring_format}?teams={teams}&year={year}"
    request = Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": (
                "fantasyfootball-draft-app/1.0 "
                "(https://github.com/jason-r-becker/fantasyfootball)"
            ),
        },
    )
    try:
        with urlopen(request, timeout=8.0) as response:
            payload = json.load(response)
    except (
        HTTPError,
        URLError,
        TimeoutError,
        json.JSONDecodeError,
        OSError,
    ) as error:
        raise RuntimeError("FFC ADP request failed.") from error
    if not isinstance(payload, dict):
        raise RuntimeError("FFC returned an unexpected ADP response.")
    return payload


def _read_cache(
    path: Path, scoring_format: str, teams: int, year: int
) -> tuple[datetime, dict[str, Any]] | None:
    try:
        wrapper = json.loads(path.read_text(encoding="utf-8"))
        if wrapper["format"] != scoring_format:
            return None
        if int(wrapper["teams"]) != teams or int(wrapper["year"]) != year:
            return None
        fetched_at = datetime.fromisoformat(wrapper["fetched_at"])
        payload = wrapper["payload"]
    except (
        FileNotFoundError,
        OSError,
        KeyError,
        TypeError,
        ValueError,
        json.JSONDecodeError,
    ):
        return None
    if fetched_at.tzinfo is None:
        fetched_at = fetched_at.replace(tzinfo=UTC)
    if not isinstance(payload, dict):
        return None
    return fetched_at, payload


def _write_cache(
    path: Path,
    payload: dict[str, Any],
    fetched_at: datetime,
    *,
    scoring_format: str,
    teams: int,
    year: int,
) -> None:
    wrapper = {
        "format": scoring_format,
        "teams": teams,
        "year": year,
        "fetched_at": fetched_at.isoformat(),
        "payload": payload,
    }
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(wrapper, stream, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _build_model(
    payload: dict[str, Any],
    *,
    scoring_format: str,
    teams: int,
    status: str,
    fetched_at: datetime,
    message: str | None = None,
) -> ADPModel:
    meta = payload.get("meta") or {}
    raw_players = payload.get("players")
    if not isinstance(meta, dict) or not isinstance(raw_players, list):
        raise RuntimeError("FFC returned an unexpected ADP response.")

    response_teams = _integer(meta.get("teams"))
    if response_teams is None:
        raise RuntimeError("FFC returned ADP without a valid league size.")
    if response_teams != teams:
        raise RuntimeError("FFC returned ADP for a different league size.")
    response_format = str(meta.get("type") or "").lower()
    format_aliases = {
        "half ppr": "half-ppr",
        "half-ppr": "half-ppr",
        "2qb": "2-qb",
        "2-qb": "2-qb",
    }
    response_format = format_aliases.get(response_format, response_format)
    if not response_format:
        raise RuntimeError("FFC returned ADP without a scoring format.")
    if response_format != scoring_format:
        raise RuntimeError("FFC returned ADP for a different scoring format.")

    players = []
    normalized_names: set[str] = set()
    for raw in raw_players:
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("name") or "").strip()
        mean = _number(raw.get("adp"))
        stdev = _number(raw.get("stdev"))
        samples = _integer(raw.get("times_drafted"))
        if not name or mean is None or mean <= 0:
            continue
        if samples is None or samples < 1:
            continue
        if stdev is not None and stdev < 0:
            continue
        normalized = normalize_player_name(name)
        if not normalized:
            continue
        position = normalize_position(raw.get("position")) or None
        # The optimizer deliberately ranks only offensive players. Kicker and
        # defense selections remain supported by platform/manual tracking, but
        # including them here would make intentional omissions look like name-
        # matching failures.
        if position in {"K", "DST"}:
            continue
        if normalized in normalized_names:
            raise RuntimeError(
                "FFC returned duplicate normalized player names."
            )
        normalized_names.add(normalized)
        players.append(
            PlayerDraftDistribution(
                name=name,
                mean=mean,
                stdev=stdev,
                samples=samples,
                earliest=_integer(raw.get("high")),
                latest=_integer(raw.get("low")),
                team=str(raw.get("team") or "").strip() or None,
                position=position,
                bye=_integer(raw.get("bye")),
            )
        )

    if not players:
        raise RuntimeError("FFC returned no usable player distributions.")
    return ADPModel(
        enabled=True,
        available=True,
        status=status,
        scoring_format=scoring_format,
        teams=teams,
        rounds=_integer(meta.get("rounds")),
        total_drafts=_integer(meta.get("total_drafts")),
        start_date=str(meta.get("start_date") or "") or None,
        end_date=str(meta.get("end_date") or "") or None,
        fetched_at=fetched_at.isoformat(),
        players=tuple(players),
        message=message,
    )


def load_adp_model(
    config: dict[str, Any], year: int, league_path: Path
) -> ADPModel:
    """Load fresh FFC data, with a private cache and offline fallback."""
    teams = _integer(config.get("teams"))
    if teams is None or teams < 2:
        raise ADPModelError("teams must be at least 2 before loading ADP.")
    scoring_format, offline = _configured_settings(config)
    if scoring_format is None:
        return ADPModel(
            enabled=False,
            available=False,
            status="disabled",
            scoring_format=None,
            teams=teams,
        )

    now = datetime.now(UTC)
    cache_path = league_path / ".ffc_adp.json"
    cached = _read_cache(cache_path, scoring_format, teams, int(year))
    if cached and cached[0] - now > CACHE_FUTURE_TOLERANCE:
        cached = None
    if (
        not offline
        and cached
        and timedelta(0) <= now - cached[0] <= CACHE_MAX_AGE
    ):
        try:
            return _build_model(
                cached[1],
                scoring_format=scoring_format,
                teams=teams,
                status="cache",
                fetched_at=cached[0],
            )
        except RuntimeError:
            cached = None

    if offline:
        if cached:
            try:
                return _build_model(
                    cached[1],
                    scoring_format=scoring_format,
                    teams=teams,
                    status="offline-cache",
                    fetched_at=cached[0],
                    message="Offline mode is using the matching cached model.",
                )
            except RuntimeError:
                pass
        return ADPModel(
            enabled=True,
            available=False,
            status="offline-unavailable",
            scoring_format=scoring_format,
            teams=teams,
            message=(
                "Offline mode is enabled and no usable cache is available."
            ),
        )

    try:
        payload = _request_ffc(scoring_format, teams, int(year))
        model = _build_model(
            payload,
            scoring_format=scoring_format,
            teams=teams,
            status="live",
            fetched_at=now,
        )
        try:
            _write_cache(
                cache_path,
                payload,
                now,
                scoring_format=scoring_format,
                teams=teams,
                year=int(year),
            )
        except OSError:
            # A read-only league directory should not disable a usable live
            # model; it will simply need another request on the next launch.
            pass
        return model
    except RuntimeError:
        if cached:
            try:
                return _build_model(
                    cached[1],
                    scoring_format=scoring_format,
                    teams=teams,
                    status="stale-cache",
                    fetched_at=cached[0],
                    message="FFC is offline; using the last cached model.",
                )
            except RuntimeError:
                pass
        return ADPModel(
            enabled=True,
            available=False,
            status="unavailable",
            scoring_format=scoring_format,
            teams=teams,
            message="FFC is offline and no usable cache is available.",
        )


def legacy_probability(adp: Any, pick: int, teams: int) -> float:
    """Retain the previous estimate when no distribution is available."""
    try:
        mean = float(adp)
    except TypeError, ValueError:
        return 0.5
    if not math.isfinite(mean):
        return 0.5
    probability = 0.5 - 0.3 * ((pick - mean) / teams)
    return max(0.0, min(1.0, probability))


def probability_available(
    distribution: PlayerDraftDistribution,
    pick: int,
    *,
    fallback_adp: Any,
    teams: int,
) -> float:
    """Estimate P(draft position >= pick) from a lower-truncated normal."""
    if distribution.stdev is None or distribution.stdev <= 0:
        return legacy_probability(fallback_adp, pick, teams)
    threshold = float(pick) - 0.5
    root_two = math.sqrt(2.0)

    def cdf(value: float) -> float:
        z_score = (value - distribution.mean) / distribution.stdev
        return 0.5 * (1.0 + math.erf(z_score / root_two))

    lower_cdf = cdf(0.5)
    denominator = 1.0 - lower_cdf
    modeled = (
        legacy_probability(fallback_adp, pick, teams)
        if denominator <= 0
        else (1.0 - cdf(threshold)) / denominator
    )
    modeled = max(0.0, min(1.0, modeled))

    # Observation count measures confidence in the fitted parameters, not the
    # inherent spread of a player's draft position. Shrink only sparse samples
    # toward the existing league-size heuristic.
    reliability = min(1.0, distribution.samples / 25.0)
    fallback = legacy_probability(fallback_adp, pick, teams)
    return max(
        0.0,
        min(1.0, reliability * modeled + (1.0 - reliability) * fallback),
    )
