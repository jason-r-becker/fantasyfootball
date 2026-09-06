"""Prepare league rankings from projections and FFC draft-position data."""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from fuzzywuzzy import fuzz, process

from fantasyfootball.adp_model import ADPModel, ADPModelError, load_adp_model
from fantasyfootball.player_matching import (
    PlayerMatcher,
    PlayerMatchError,
    load_player_aliases,
)
from fantasyfootball.utils import root

POSITIONS = ("QB", "RB", "WR", "TE", "FLEX")
OUTPUT_COLUMNS = (
    "Player",
    "Team",
    "Position",
    "Bye",
    "VOR_Floor",
    "VOR_Points",
    "VOR_Ceiling",
    "Floor",
    "Points",
    "Ceiling",
    "FLEX_VOR_Floor",
    "FLEX_VOR_Points",
    "FLEX_VOR_Ceiling",
    "Std Dev",
    "ADP",
    "ADP Source",
    "ADP Std Dev",
    "ADP Samples",
    "ADP Earliest",
    "ADP Latest",
)


def _read_config(path: Path) -> dict[str, Any]:
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise FileNotFoundError(
            f"Missing league configuration: {path}"
        ) from error
    if not isinstance(config, dict):
        raise ValueError("config.json must contain a JSON object.")
    return config


def _replacement_value(
    position: str,
    position_frame: pd.DataFrame,
    position_counts: dict[str, int],
    teams: int,
    flex_positions: tuple[str, ...] = (),
) -> float:
    if position == "FLEX":
        count = sum(position_counts.get(pos, 0) for pos in flex_positions)
        count += position_counts.get("FLEX", 0) + 1
    elif position in {"WR", "RB"}:
        count = position_counts[position] + 1
    else:
        count = position_counts[position]
    values = position_frame["Points"].iloc[teams * count : teams * (count + 1)]
    if values.empty:
        values = position_frame["Points"].tail(max(1, teams))
    return float(values.mean())


def prepare_projection_rankings(
    raw: pd.DataFrame, config: dict[str, Any]
) -> pd.DataFrame:
    """Calculate value-over-replacement fields from projection input."""
    frame = raw.copy().reset_index(drop=True)
    frame.columns = [str(column).lower() for column in frame.columns]
    required = (
        "player",
        "team",
        "position",
        "points",
        "floor",
        "ceiling",
        "sd_pts",
    )
    missing = sorted(set(required) - set(frame.columns))
    if missing:
        raise ValueError(f"raw.csv is missing columns: {', '.join(missing)}")
    frame = frame[list(required)].rename(
        columns={
            "player": "Player",
            "team": "Team",
            "position": "Position",
            "points": "Points",
            "floor": "Floor",
            "ceiling": "Ceiling",
            "sd_pts": "Std Dev",
        }
    )
    frame["Player"] = frame["Player"].fillna("").astype(str).str.strip()
    frame["Position"] = frame["Position"].astype(str).str.upper()
    frame = frame[frame["Position"].isin(("QB", "RB", "WR", "TE"))].copy()
    if (frame["Player"] == "").any() or frame["Player"].duplicated().any():
        raise ValueError("raw.csv player names must be nonblank and unique.")

    teams = int(config["teams"])
    position_counts = config["positions"]
    if not isinstance(position_counts, dict):
        raise ValueError("config positions must be a JSON object.")
    for metric in ("Points", "Floor", "Ceiling"):
        frame[f"VOR_{metric}"] = np.nan
        frame[f"FLEX_VOR_{metric}"] = np.nan

    for position in ("QB", "RB", "WR", "TE"):
        position_frame = frame[frame["Position"] == position].sort_values(
            "Points", ascending=False
        )
        if position_frame.empty:
            continue
        weekly = config.get("waiver_weekly_position_value", {}).get(position)
        replacement = (
            float(weekly) * 17
            if weekly is not None
            else _replacement_value(
                position, position_frame, position_counts, teams
            )
        )
        for metric in ("Points", "Floor", "Ceiling"):
            frame.loc[position_frame.index, f"VOR_{metric}"] = (
                position_frame[metric] - replacement
            )

    configured_flex = config.get("flex_positions", ["RB", "WR"])
    if not isinstance(configured_flex, list) or not configured_flex:
        raise ValueError("flex_positions must be a non-empty JSON array.")
    flex_positions = tuple(
        dict.fromkeys(str(position).upper() for position in configured_flex)
    )
    invalid_flex = set(flex_positions) - {"QB", "RB", "WR", "TE"}
    if invalid_flex:
        raise ValueError("flex_positions may contain only QB, RB, WR, and TE.")
    flex_frame = frame[frame["Position"].isin(flex_positions)].sort_values(
        "Points", ascending=False
    )
    if not flex_frame.empty:
        weekly = config.get("waiver_weekly_position_value", {}).get("FLEX")
        replacement = (
            float(weekly) * 17
            if weekly is not None
            else _replacement_value(
                "FLEX",
                flex_frame,
                position_counts,
                teams,
                flex_positions,
            )
        )
        for metric in ("Points", "Floor", "Ceiling"):
            frame.loc[flex_frame.index, f"FLEX_VOR_{metric}"] = (
                flex_frame[metric] - replacement
            )
    return frame.sort_values("VOR_Floor", ascending=False).reset_index(
        drop=True
    )


def _confirm_fuzzy_match(source: str, suggestion: str, adp: float) -> bool:
    print(
        "\nVerify this FFC player mapping; it is never accepted automatically."
    )
    answer = input(f"  ADP {adp:.1f}) {source} --> {suggestion} [y/N]: ")
    return answer.strip().lower() in {"y", "yes"}


def merge_ffc_adp(
    rankings: pd.DataFrame,
    model: ADPModel,
    aliases: dict[str, str] | None = None,
    *,
    confirmer: Callable[[str, str, float], bool] | None = None,
    relevant_pick: int | None = None,
) -> tuple[pd.DataFrame, dict[str, Any], dict[str, str]]:
    """Join FFC ADP by safe identity matching and report every miss."""
    frame = rankings.copy()
    for column in ("Bye", *OUTPUT_COLUMNS[14:]):
        if column not in frame:
            frame[column] = pd.NA

    matcher = PlayerMatcher(frame.to_dict("records"), aliases)
    matched: dict[str, Any] = {}
    conflicts: list[dict[str, str]] = []
    unresolved = []
    for distribution in model.players:
        result = matcher.match(
            distribution.name,
            position=distribution.position,
            team=distribution.team,
        )
        if result.canonical is None:
            unresolved.append(distribution)
            if result.reason != "unmatched name":
                conflicts.append(
                    {
                        "source": distribution.name,
                        "reason": result.reason or "conflict",
                    }
                )
            continue
        if result.canonical in matched:
            conflicts.append(
                {
                    "source": distribution.name,
                    "reason": f"duplicate match for {result.canonical}",
                }
            )
            continue
        matched[result.canonical] = distribution

    confirmed: dict[str, str] = {}
    if confirmer is not None:
        for distribution in sorted(unresolved, key=lambda player: player.mean):
            if relevant_pick is not None and distribution.mean > relevant_pick:
                continue
            choices = matcher.compatible_names(
                position=distribution.position, team=distribution.team
            )
            available = [name for name in choices if name not in matched]
            suggestion = process.extractOne(
                distribution.name, available, scorer=fuzz.ratio
            )
            if suggestion is None or suggestion[1] < 70:
                continue
            candidate = suggestion[0]
            if confirmer(distribution.name, candidate, distribution.mean):
                matched[candidate] = distribution
                confirmed[distribution.name] = candidate

    for canonical, distribution in matched.items():
        selected = frame["Player"] == canonical
        frame.loc[selected, "ADP"] = distribution.mean
        frame.loc[selected, "Bye"] = distribution.bye
        frame.loc[selected, "ADP Source"] = "FFC"
        frame.loc[selected, "ADP Std Dev"] = distribution.stdev
        frame.loc[selected, "ADP Samples"] = distribution.samples
        frame.loc[selected, "ADP Earliest"] = distribution.earliest
        frame.loc[selected, "ADP Latest"] = distribution.latest

    unmatched = [
        distribution.name
        for distribution in unresolved
        if distribution.name not in confirmed
    ]
    report = {
        "source_players": len(model.players),
        "matched_players": len(matched),
        "alias_count": matcher.alias_count,
        "conflicts": conflicts,
        "unmatched": unmatched,
    }
    return frame, report, confirmed


def _write_aliases(path: Path, aliases: dict[str, str]) -> None:
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(aliases, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def clean_league(year: int, league: str) -> dict[str, Any]:
    """Generate clean and initial live rankings for one private league."""
    league_path = root() / "data" / str(year) / league
    if not league_path.exists():
        raise FileNotFoundError(
            f"League {league!r} does not exist: {league_path}"
        )
    config = _read_config(league_path / "config.json")
    raw = pd.read_csv(league_path / "raw.csv")
    rankings = prepare_projection_rankings(raw, config)
    try:
        model = load_adp_model(config, year, league_path)
    except ADPModelError as error:
        raise ValueError(str(error)) from error
    if model.enabled and not model.available:
        raise RuntimeError(
            f"Could not prepare rankings: {model.message} "
            "Reconnect or provide a matching FFC cache."
        )
    alias_path = league_path.parent / "source_player_map.json"
    aliases = load_player_aliases(alias_path)
    relevant_pick = int(config.get("draft_rounds") or 15) * int(
        config["teams"]
    )
    rankings, report, confirmed = merge_ffc_adp(
        rankings,
        model,
        aliases,
        confirmer=_confirm_fuzzy_match if model.enabled else None,
        relevant_pick=relevant_pick,
    )
    if confirmed:
        _write_aliases(alias_path, {**aliases, **confirmed})
    rankings = rankings[list(OUTPUT_COLUMNS)].round(2)
    for name in ("clean.csv", "live_draft.csv"):
        rankings.to_csv(league_path / name)
    return report


def main() -> None:
    year = int(input("Input Year:  "))
    league = input("Input League Name:  ").strip()
    try:
        report = clean_league(year, league)
    except (
        FileNotFoundError,
        ValueError,
        RuntimeError,
        PlayerMatchError,
    ) as error:
        raise SystemExit(str(error)) from error
    print(
        f"Matched {report['matched_players']} of "
        f"{report['source_players']} FFC players."
    )
    if report["conflicts"]:
        print(
            f"Rejected {len(report['conflicts'])} unsafe identity conflicts."
        )
    if report["unmatched"]:
        print(f"Left {len(report['unmatched'])} FFC players unmatched.")


if __name__ == "__main__":
    main()
