"""Persistent draft state shared by the web UI and spreadsheet workflow."""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from typing import Any

import pandas as pd

from fantasyfootball.adp_model import (
    ADPModelError,
    PlayerDraftDistribution,
    load_adp_model,
)
from fantasyfootball.player_matching import (
    PlayerMatcher,
    PlayerMatchError,
    load_player_aliases,
    normalize_player_name,
    normalize_position,
)

DISPLAY_COLUMNS = [
    "Player",
    "Team",
    "Position",
    "Bye",
    "ADP",
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
]
METRICS = ("VOR_Floor", "VOR_Points", "VOR_Ceiling", "Points", "Ceiling")
OPTIMIZER_AUTO_THRESHOLD_MS = 500


class DraftError(Exception):
    """An expected, user-facing draft operation error."""


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _json_value(value: Any) -> Any:
    if pd.isna(value):
        return None
    if hasattr(value, "item"):
        return value.item()
    return value


@dataclass(frozen=True)
class DraftPaths:
    league: Path
    clean: Path
    workbook: Path
    drafted: Path
    session: Path


class DraftSession:
    """Manage picks and atomically sync a legacy-compatible CSV."""

    version = 1

    def __init__(
        self,
        project_root: Path,
        year: int,
        league: str,
        draft_slot: int | None,
        *,
        rounds: int | None = None,
        mode: str = "practice",
        metric: str = "VOR_Points",
        simulate_api_down: bool = False,
    ) -> None:
        if mode not in {"practice", "live"}:
            raise DraftError("Mode must be 'practice' or 'live'.")
        if metric not in METRICS:
            raise DraftError(f"Unknown ranking metric: {metric}")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", league):
            raise DraftError(
                "League must contain only letters, numbers, underscores, "
                "and hyphens."
            )

        self.project_root = Path(project_root).resolve()
        self.year = int(year)
        self.league = league
        self.mode = mode
        self.metric = metric
        self.simulate_api_down = bool(simulate_api_down)
        league_path = self.project_root / "data" / str(self.year) / league
        suffix = "practice" if mode == "practice" else "live"
        workbook_name = (
            "practice_live_draft.csv"
            if mode == "practice"
            else "live_draft.csv"
        )
        drafted_name = (
            "practice_drafted_players.csv"
            if mode == "practice"
            else "drafted_players.csv"
        )
        self.paths = DraftPaths(
            league=league_path,
            clean=league_path / "clean.csv",
            workbook=league_path / workbook_name,
            drafted=league_path / drafted_name,
            session=league_path / f".draft_app.{suffix}.json",
        )
        self._lock = RLock()
        self.config = self._read_config()
        configured_rounds = self.config.get("draft_rounds")
        if configured_rounds is None:
            configured_rounds = 15
        self.rounds = int(configured_rounds if rounds is None else rounds)
        if self.rounds < 1:
            raise DraftError("Draft rounds must be at least 1.")
        resolved_slot = draft_slot or self.config.get("draft_slot")
        if resolved_slot is None:
            raise DraftError(
                "Draft slot is missing; pass --pick or add draft_slot "
                "to config.json."
            )
        self.draft_slot = int(resolved_slot)
        self.teams = int(self.config["teams"])
        configured_flex = self.config.get("flex_positions", ["RB", "WR"])
        if not isinstance(configured_flex, list) or not configured_flex:
            raise DraftError("flex_positions must be a non-empty JSON array.")
        self.flex_positions = tuple(
            dict.fromkeys(
                normalize_position(position) for position in configured_flex
            )
        )
        invalid_flex = set(self.flex_positions) - {"QB", "RB", "WR", "TE"}
        if invalid_flex:
            raise DraftError(
                "flex_positions may contain only QB, RB, WR, and TE."
            )
        if not 1 <= self.draft_slot <= self.teams:
            raise DraftError(
                f"Draft slot must be between 1 and {self.teams} for {league}."
            )
        if not self.paths.clean.exists():
            raise DraftError(f"Missing draft rankings: {self.paths.clean}")
        self.full_df = pd.read_csv(self.paths.clean, index_col=0)
        if "Player" not in self.full_df.columns or self.full_df.empty:
            raise DraftError(
                f"Invalid or empty draft rankings: {self.paths.clean}"
            )
        aliases_path = self.paths.league.parent / "source_player_map.json"
        try:
            aliases = load_player_aliases(aliases_path)
            self._player_matcher = PlayerMatcher(
                self.full_df.to_dict("records"), aliases
            )
        except PlayerMatchError as error:
            raise DraftError(str(error)) from error
        self.alias_count = self._player_matcher.alias_count
        try:
            self.adp_model = load_adp_model(
                self.config, self.year, self.paths.league
            )
        except ADPModelError as error:
            raise DraftError(str(error)) from error
        self._adp_distributions = self._match_adp_distributions()
        self.adp_refresh_count = self._apply_adp_model()
        self._positions = {
            normalize_player_name(str(row["Player"])): str(row["Position"])
            for _, row in self.full_df.iterrows()
        }
        self.state = self._load_or_create_state()
        if self.paths.workbook.exists():
            self.refresh_from_spreadsheet()
        else:
            self._write_workbook()
        # Keep refreshed FFC values and column structure current even when the
        # spreadsheet had no manual row changes to import.
        self._write_workbook()
        self._write_drafted_players()

    def _match_adp_distributions(
        self,
    ) -> dict[str, PlayerDraftDistribution]:
        """Match FFC names through the same confirmed alias table as picks."""
        matched = {}
        for distribution in self.adp_model.players:
            result = self._player_matcher.match(
                distribution.name,
                position=distribution.position,
                team=distribution.team,
            )
            if result.canonical:
                key = normalize_player_name(result.canonical)
                if key in matched:
                    raise DraftError(
                        "Multiple FFC players matched the same ranked player: "
                        f"{result.canonical}."
                    )
                matched[key] = distribution
        return matched

    def adp_distribution(self, player: str) -> PlayerDraftDistribution | None:
        """Return the configured draft-position model for a ranked player."""
        return self._adp_distributions.get(normalize_player_name(player))

    def _apply_adp_model(self) -> int:
        """Refresh displayed ADP and bye values from matched FFC players."""
        for column in (
            "ADP",
            "Bye",
            "ADP Source",
            "ADP Std Dev",
            "ADP Samples",
            "ADP Earliest",
            "ADP Latest",
        ):
            if column not in self.full_df:
                self.full_df[column] = pd.NA
        refreshed = 0
        for index, row in self.full_df.iterrows():
            distribution = self.adp_distribution(str(row["Player"]))
            if distribution is None:
                continue
            self.full_df.at[index, "ADP"] = distribution.mean
            if distribution.bye is not None:
                self.full_df.at[index, "Bye"] = distribution.bye
            self.full_df.at[index, "ADP Source"] = "FFC"
            self.full_df.at[index, "ADP Std Dev"] = distribution.stdev
            self.full_df.at[index, "ADP Samples"] = distribution.samples
            self.full_df.at[index, "ADP Earliest"] = distribution.earliest
            self.full_df.at[index, "ADP Latest"] = distribution.latest
            refreshed += 1
        return refreshed

    def _read_config(self) -> dict[str, Any]:
        config_path = self.paths.league / "config.json"
        try:
            with config_path.open(encoding="utf-8") as stream:
                return json.load(stream)
        except FileNotFoundError as error:
            raise DraftError(
                f"Missing league configuration: {config_path}"
            ) from error

    def _fresh_state(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "year": self.year,
            "league": self.league,
            "mode": self.mode,
            "draft_slot": self.draft_slot,
            "rounds": self.rounds,
            "metric": self.metric,
            "picks": [],
            "team_names": {},
            "team_names_format": None,
            "excluded_players": [],
            "optimizer_profile": {
                "runs": 0,
                "last_run_at": None,
                "last_ms": None,
                "fastest_ms": None,
                "threshold_ms": OPTIMIZER_AUTO_THRESHOLD_MS,
                "auto_every_pick": False,
            },
            "created_at": _now(),
            "updated_at": _now(),
        }

    def _load_or_create_state(self) -> dict[str, Any]:
        if not self.paths.session.exists():
            state = self._fresh_state()
            self._write_json(state)
            return state
        try:
            with self.paths.session.open(encoding="utf-8") as stream:
                state = json.load(stream)
        except (json.JSONDecodeError, OSError) as error:
            raise DraftError(
                f"Could not read draft session: {error}"
            ) from error
        identity = (state.get("year"), state.get("league"), state.get("mode"))
        if identity != (self.year, self.league, self.mode):
            raise DraftError(
                "Saved draft session does not match the requested draft."
            )
        # CLI settings are intentional and can be adjusted between launches.
        state["draft_slot"] = self.draft_slot
        state["rounds"] = self.rounds
        state["metric"] = self.metric
        state.setdefault("team_names", {})
        state.setdefault("team_names_format", None)
        state.setdefault("excluded_players", [])
        state.setdefault(
            "optimizer_profile",
            self._fresh_state()["optimizer_profile"],
        )
        return state

    def _write_json(self, state: dict[str, Any]) -> None:
        self.paths.session.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(
            prefix=f"{self.paths.session.name}.", dir=self.paths.session.parent
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(state, stream, indent=2, allow_nan=False)
                stream.write("\n")
            os.replace(temporary, self.paths.session)
        except Exception:
            Path(temporary).unlink(missing_ok=True)
            raise

    def _persist(self) -> None:
        self.state["updated_at"] = _now()
        self._write_json(self.state)
        self._write_workbook()
        self._write_drafted_players()

    def _write_workbook(self) -> None:
        drafted = {
            normalize_player_name(pick["player"])
            for pick in self.state["picks"]
            if pick.get("matched", True)
        }
        remaining = self.full_df[
            ~self.full_df["Player"].map(normalize_player_name).isin(drafted)
        ]
        fd, temporary = tempfile.mkstemp(
            prefix=f"{self.paths.workbook.name}.",
            dir=self.paths.workbook.parent,
        )
        os.close(fd)
        try:
            remaining.to_csv(temporary)
            os.replace(temporary, self.paths.workbook)
        except Exception:
            Path(temporary).unlink(missing_ok=True)
            raise

    def _write_drafted_players(self) -> None:
        """Atomically mirror the chronological pick log to a readable CSV."""
        columns = [
            "Pick",
            "Round",
            "Round Pick",
            "Player",
            "Position",
            "Mine",
            "Source",
            "Matched",
            "Locked",
            "Team",
            "External ID",
            "Recorded At",
        ]
        rows = []
        for pick in self.picks:
            number = int(pick["number"])
            round_number, offset = divmod(number - 1, self.teams)
            position = pick.get("position") or self._positions.get(
                normalize_player_name(pick["player"]), ""
            )
            rows.append(
                {
                    "Pick": number,
                    "Round": round_number + 1,
                    "Round Pick": offset + 1,
                    "Player": pick["player"],
                    "Position": position,
                    "Mine": bool(pick.get("mine")),
                    "Source": pick.get("source"),
                    "Matched": bool(pick.get("matched", True)),
                    "Locked": bool(pick.get("locked")),
                    "Team": pick.get("team"),
                    "External ID": pick.get("external_id"),
                    "Recorded At": pick.get("recorded_at"),
                }
            )
        frame = pd.DataFrame(rows, columns=columns)
        fd, temporary = tempfile.mkstemp(
            prefix=f"{self.paths.drafted.name}.",
            dir=self.paths.drafted.parent,
        )
        os.close(fd)
        try:
            frame.to_csv(temporary, index=False)
            os.replace(temporary, self.paths.drafted)
        except Exception:
            Path(temporary).unlink(missing_ok=True)
            raise

    @property
    def picks(self) -> list[dict[str, Any]]:
        return sorted(self.state["picks"], key=lambda pick: pick["number"])

    @property
    def excluded_players(self) -> list[str]:
        return sorted(set(self.state.get("excluded_players", [])))

    @property
    def team_names(self) -> dict[int, str]:
        return {
            int(slot): str(name)
            for slot, name in self.state.get("team_names", {}).items()
            if name
        }

    @property
    def current_pick(self) -> int:
        used = {int(pick["number"]) for pick in self.picks}
        for number in range(1, self.teams * self.rounds + 1):
            if number not in used:
                return number
        return self.teams * self.rounds + 1

    def is_my_pick(self, number: int) -> bool:
        round_number, offset = divmod(number - 1, self.teams)
        owner_slot = (
            offset + 1 if round_number % 2 == 0 else self.teams - offset
        )
        return owner_slot == self.draft_slot

    def _canonical_name(
        self,
        player: str,
        *,
        position: str | None = None,
        team: str | None = None,
    ) -> str | None:
        return self._player_matcher.match(
            player, position=position, team=team
        ).canonical

    def add_pick(
        self,
        player: str,
        *,
        number: int | None = None,
        source: str = "manual",
        team: str | int | None = None,
        mine: bool | None = None,
        external_id: str | None = None,
        locked: bool = False,
    ) -> dict[str, Any]:
        with self._lock:
            canonical = self._canonical_name(player)
            if canonical is None:
                raise DraftError(f"Player is not in clean.csv: {player}")
            normalized = normalize_player_name(canonical)
            duplicate = next(
                (
                    pick
                    for pick in self.picks
                    if pick.get("matched", True)
                    and normalize_player_name(pick["player"]) == normalized
                ),
                None,
            )
            if duplicate:
                raise DraftError(
                    f"{canonical} is already recorded at pick "
                    f"{duplicate['number']}."
                )
            number = self.current_pick if number is None else int(number)
            if not 1 <= number <= self.teams * self.rounds:
                raise DraftError(
                    "Pick number is outside this draft's configured rounds."
                )
            occupied = next(
                (pick for pick in self.picks if pick["number"] == number), None
            )
            if occupied:
                raise DraftError(
                    f"Pick {number} is already {occupied['player']}; "
                    "undo it first."
                )
            pick = {
                "number": number,
                "player": canonical,
                "source": source,
                "team": team,
                "mine": self.is_my_pick(number)
                if mine is None
                else bool(mine),
                "matched": True,
                "external_id": external_id,
                "locked": bool(locked),
                "recorded_at": _now(),
            }
            self.state["excluded_players"] = [
                name
                for name in self.excluded_players
                if normalize_player_name(name) != normalized
            ]
            self.state["picks"].append(pick)
            self._persist()
            return pick

    def add_untracked_pick(
        self,
        position: str,
        *,
        number: int | None = None,
        mine: bool | None = None,
    ) -> dict[str, Any]:
        """Advance for a kicker or defense without a player lookup."""
        position = normalize_position(position)
        if position not in {"K", "DST"}:
            raise DraftError("Untracked position must be K or DST.")
        with self._lock:
            number = self.current_pick if number is None else int(number)
            if not 1 <= number <= self.teams * self.rounds:
                raise DraftError(
                    "Pick number is outside this draft's configured rounds."
                )
            occupied = next(
                (pick for pick in self.picks if pick["number"] == number), None
            )
            if occupied:
                raise DraftError(
                    f"Pick {number} is already {occupied['player']}; "
                    "undo it first."
                )
            label = "K selected (not tracked)"
            if position == "DST":
                label = "D/ST selected (not tracked)"
            pick = {
                "number": number,
                "player": label,
                "position": position,
                "source": "manual fallback",
                "team": None,
                "mine": self.is_my_pick(number)
                if mine is None
                else bool(mine),
                "matched": False,
                "external_id": None,
                "locked": True,
                "recorded_at": _now(),
            }
            self.state["picks"].append(pick)
            self._persist()
            return pick

    def replace_pick(
        self, number: int, player: str, *, mine: bool | None = None
    ) -> dict[str, Any]:
        """Force a local correction and protect it from subsequent syncs."""
        with self._lock:
            old = next(
                (
                    pick
                    for pick in self.state["picks"]
                    if pick["number"] == number
                ),
                None,
            )
            if old is None:
                raise DraftError(f"Pick {number} has not been recorded.")
            self.state["picks"].remove(old)
            try:
                return self.add_pick(
                    player,
                    number=number,
                    source="manual override",
                    team=old.get("team"),
                    mine=old.get("mine") if mine is None else mine,
                    locked=True,
                )
            except Exception:
                self.state["picks"].append(old)
                raise

    def reorder_opponent_picks(
        self, corrections: list[dict[str, Any]]
    ) -> None:
        """Apply a verified permutation without exposing intermediate gaps."""
        if not isinstance(corrections, list) or len(corrections) < 2:
            raise DraftError("Provide at least two pick corrections.")
        with self._lock:
            existing = {p["number"]: p for p in self.picks}
            replacements = {}
            for correction in corrections:
                number = int(correction["number"])
                old = existing.get(number)
                if not old or number in replacements:
                    raise DraftError(
                        "Correction has a missing or repeated pick."
                    )
                if old.get("mine") or self.is_my_pick(number):
                    raise DraftError(
                        "Opponent reorder cannot change your picks."
                    )
                if old["player"] != correction.get("expected_player"):
                    raise DraftError(
                        "Pick changed since the correction was prepared."
                    )
                canonical = self._canonical_name(correction.get("player", ""))
                if not canonical or not old.get("matched", True):
                    raise DraftError("Reorder requires matched players.")
                replacements[number] = canonical
            if sorted(replacements.values()) != sorted(
                existing[n]["player"] for n in replacements
            ):
                raise DraftError(
                    "Reorder must preserve the drafted player set."
                )
            before = self.state["picks"]
            self.state["picks"] = [
                {
                    **p,
                    "player": replacements[p["number"]],
                    "source": "verified order correction",
                    "locked": True,
                    "external_id": None,
                    "recorded_at": _now(),
                }
                if p["number"] in replacements
                else p
                for p in before
            ]
            try:
                self._persist()
            except Exception:
                self.state["picks"] = before
                raise

    def undo_pick(self, number: int) -> dict[str, Any]:
        with self._lock:
            pick = next(
                (
                    pick
                    for pick in self.state["picks"]
                    if pick["number"] == number
                ),
                None,
            )
            if pick is None:
                raise DraftError(f"Pick {number} has not been recorded.")
            self.state["picks"].remove(pick)
            self._persist()
            return pick

    def set_metric(self, metric: str) -> None:
        if metric not in METRICS:
            raise DraftError(f"Unknown ranking metric: {metric}")
        with self._lock:
            self.metric = metric
            self.state["metric"] = metric
            self._persist()

    def set_team_names(
        self, names: dict[int, str], *, format_name: str | None = None
    ) -> None:
        """Persist platform team labels independently of API availability."""
        cleaned = {
            str(int(slot)): str(name).strip()
            for slot, name in names.items()
            if str(name).strip()
        }
        with self._lock:
            self.state["team_names"] = cleaned
            self.state["team_names_format"] = format_name
            self.state["updated_at"] = _now()
            self._write_json(self.state)

    def set_player_excluded(self, player: str, excluded: bool) -> str:
        """Add or remove a ranked player from decision-support results."""
        canonical = self._canonical_name(player)
        if canonical is None:
            raise DraftError(f"Player is not in clean.csv: {player}")
        with self._lock:
            names = {
                normalize_player_name(name): name
                for name in self.excluded_players
            }
            key = normalize_player_name(canonical)
            if excluded:
                names[key] = canonical
            else:
                names.pop(key, None)
            self.state["excluded_players"] = sorted(names.values())
            self._persist()
            return canonical

    def record_optimizer_run(self, elapsed_seconds: float) -> dict[str, Any]:
        """Persist optimizer timing and latch fast every-pick mode."""
        elapsed_ms = max(0.0, float(elapsed_seconds) * 1000)
        with self._lock:
            previous = self.state.get("optimizer_profile") or {}
            fastest = previous.get("fastest_ms")
            fastest_ms = (
                elapsed_ms
                if fastest is None
                else min(float(fastest), elapsed_ms)
            )
            profile = {
                "runs": int(previous.get("runs", 0)) + 1,
                "last_run_at": _now(),
                "last_ms": round(elapsed_ms, 1),
                "fastest_ms": round(fastest_ms, 1),
                "threshold_ms": OPTIMIZER_AUTO_THRESHOLD_MS,
                "auto_every_pick": bool(
                    previous.get("auto_every_pick")
                    or elapsed_ms < OPTIMIZER_AUTO_THRESHOLD_MS
                ),
            }
            self.state["optimizer_profile"] = profile
            self.state["updated_at"] = _now()
            self._write_json(self.state)
            return dict(profile)

    def reset_practice(self) -> None:
        """Clear practice picks and restore its spreadsheet."""
        if self.mode != "practice":
            raise DraftError("Reset is only available in practice mode.")
        with self._lock:
            self.state = self._fresh_state()
            self._persist()

    def reconcile_platform_picks(
        self, platform_picks: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """Merge platform picks while retaining manual overrides."""
        added: list[dict[str, Any]] = []
        conflicts: list[dict[str, Any]] = []
        unmatched: list[dict[str, Any]] = []
        with self._lock:
            for remote in sorted(
                platform_picks, key=lambda pick: pick["number"]
            ):
                number = int(remote["number"])
                local = next(
                    (
                        pick
                        for pick in self.state["picks"]
                        if pick["number"] == number
                    ),
                    None,
                )
                canonical = self._canonical_name(
                    remote.get("player", ""),
                    position=remote.get("position"),
                    team=remote.get("player_team"),
                )
                if local:
                    if canonical:
                        same = normalize_player_name(
                            local["player"]
                        ) == normalize_player_name(canonical)
                    else:
                        same = not local.get(
                            "matched", True
                        ) and normalize_player_name(
                            local["player"]
                        ) == normalize_player_name(remote.get("player", ""))
                    if not same:
                        conflicts.append(
                            {
                                "number": number,
                                "local": local["player"],
                                "platform": remote.get("player")
                                or "Unknown player",
                                "locked": bool(local.get("locked")),
                            }
                        )
                    continue
                if canonical is None:
                    label = remote.get("player") or "Unknown player"
                    pick = {
                        "number": number,
                        "player": label,
                        "position": normalize_position(remote.get("position")),
                        "source": remote.get("source", "api"),
                        "team": remote.get("team"),
                        "mine": bool(remote.get("mine")),
                        "matched": False,
                        "external_id": remote.get("external_id"),
                        "locked": False,
                        "recorded_at": _now(),
                    }
                    self.state["picks"].append(pick)
                    added.append(pick)
                    unmatched.append(pick)
                    self._persist()
                    continue
                try:
                    added.append(
                        self.add_pick(
                            canonical,
                            number=number,
                            source=remote.get("source", "api"),
                            team=remote.get("team"),
                            mine=remote.get("mine"),
                            external_id=remote.get("external_id"),
                        )
                    )
                except DraftError as error:
                    conflicts.append(
                        {
                            "number": number,
                            "local": str(error),
                            "platform": remote.get("player")
                            or "Unknown player",
                            "locked": False,
                        }
                    )
            return {
                "added": added,
                "conflicts": conflicts,
                "unmatched": unmatched,
            }

    def refresh_from_spreadsheet(self) -> dict[str, Any]:
        """Import row deletions and restorations from the working CSV."""
        with self._lock:
            try:
                sheet = pd.read_csv(self.paths.workbook, index_col=0)
            except (FileNotFoundError, pd.errors.ParserError) as error:
                raise DraftError(
                    f"Could not read {self.paths.workbook}: {error}"
                ) from error
            if "Player" not in sheet.columns:
                raise DraftError("Spreadsheet must retain the Player column.")
            remaining = {
                normalize_player_name(str(player))
                for player in sheet["Player"]
            }
            missing = [
                str(player)
                for player in self.full_df["Player"]
                if normalize_player_name(str(player)) not in remaining
            ]
            removed = []
            for pick in list(self.state["picks"]):
                if (
                    pick["source"] == "spreadsheet"
                    and normalize_player_name(pick["player"]) in remaining
                ):
                    self.state["picks"].remove(pick)
                    removed.append(pick)
            drafted = {
                normalize_player_name(pick["player"])
                for pick in self.state["picks"]
                if pick.get("matched", True)
            }
            added = []
            for player in missing:
                if normalize_player_name(player) in drafted:
                    continue
                added.append(
                    self.add_pick(player, source="spreadsheet", locked=True)
                )
            if removed and not added:
                self._persist()
            return {"added": added, "removed": removed}

    def _next_own_pick(self) -> int | None:
        for number in range(self.current_pick, self.teams * self.rounds + 1):
            if self.is_my_pick(number):
                return number
        return None

    def _row_dict(self, row: pd.Series) -> dict[str, Any]:
        return {
            column: _json_value(row[column])
            for column in DISPLAY_COLUMNS
            if column in row.index
        }

    def public_state(self) -> dict[str, Any]:
        with self._lock:
            drafted = {
                normalize_player_name(pick["player"])
                for pick in self.picks
                if pick.get("matched", True)
            }
            available = self.full_df[
                ~self.full_df["Player"]
                .map(normalize_player_name)
                .isin(drafted)
            ].copy()
            excluded = {
                normalize_player_name(player)
                for player in self.excluded_players
            }
            available.sort_values(self.metric, ascending=False, inplace=True)
            next_own = self._next_own_pick()
            picks_away = (
                None if next_own is None else next_own - self.current_pick
            )
            by_adp = available.sort_values("ADP", na_position="last")
            likely_remaining = (
                by_adp.iloc[picks_away:] if picks_away is not None else by_adp
            )
            position_outlook = []
            for position in ("QB", "RB", "WR", "TE"):
                now = available[available["Position"] == position]
                later = likely_remaining[
                    likely_remaining["Position"] == position
                ]
                if now.empty:
                    continue
                best_now = now.iloc[0]
                best_later = (
                    later.sort_values(self.metric, ascending=False).iloc[0]
                    if not later.empty
                    else None
                )
                current_value = float(best_now[self.metric])
                later_value = (
                    None
                    if best_later is None
                    else float(best_later[self.metric])
                )
                position_outlook.append(
                    {
                        "position": position,
                        "player": str(best_now["Player"]),
                        "next_player": None
                        if best_later is None
                        else str(best_later["Player"]),
                        "value": round(current_value, 2),
                        "dropoff": None
                        if later_value is None
                        else round(current_value - later_value, 2),
                    }
                )
            roster_names = {
                normalize_player_name(pick["player"])
                for pick in self.picks
                if pick.get("mine") and pick.get("matched", True)
            }
            roster = self.full_df[
                self.full_df["Player"]
                .map(normalize_player_name)
                .isin(roster_names)
            ]
            adp_lookup = {
                normalize_player_name(str(row["Player"])): _json_value(
                    row.get("ADP")
                )
                for _, row in self.full_df.iterrows()
            }
            public_picks = [
                {
                    **{
                        key: value
                        for key, value in pick.items()
                        if key != "external_id"
                    },
                    "adp": adp_lookup.get(
                        normalize_player_name(str(pick["player"]))
                    )
                    if pick.get("matched", True)
                    else None,
                }
                for pick in self.picks
            ]
            current = self.current_pick
            round_number, offset = divmod(current - 1, self.teams)
            draft_complete = current > self.teams * self.rounds
            return {
                "year": self.year,
                "league": self.league,
                "mode": self.mode,
                "site": self.config.get("site", "Manual"),
                "sync_available": self._sync_available(),
                "draft_slot": self.draft_slot,
                "teams": self.teams,
                "rounds": self.rounds,
                "flex_positions": list(self.flex_positions),
                "metric": self.metric,
                "metrics": list(METRICS),
                "current_pick": current,
                "round": round_number + 1,
                "round_pick": offset + 1,
                "draft_complete": draft_complete,
                "on_the_clock": not draft_complete
                and self.is_my_pick(current),
                "next_own_pick": next_own,
                "picks_away": picks_away,
                "workbook_path": str(self.paths.workbook),
                "drafted_path": str(self.paths.drafted),
                "session_path": str(self.paths.session),
                "simulate_api_down": self.simulate_api_down,
                "adp_refresh_count": self.adp_refresh_count,
                "adp_model": {
                    **self.adp_model.public_info(
                        matched_players=len(self._adp_distributions)
                    ),
                    "league_rounds": self.rounds,
                    "rounds_match": self.adp_model.rounds
                    in {
                        None,
                        self.rounds,
                    },
                },
                "alias_count": self.alias_count,
                "optimizer_profile": dict(
                    self.state.get("optimizer_profile")
                    or self._fresh_state()["optimizer_profile"]
                ),
                "excluded_players": self.excluded_players,
                "picks": public_picks,
                "players": [
                    {
                        **self._row_dict(row),
                        "Excluded": normalize_player_name(str(row["Player"]))
                        in excluded,
                    }
                    for _, row in available.iterrows()
                ],
                "roster": [
                    self._row_dict(row) for _, row in roster.iterrows()
                ],
                "position_outlook": position_outlook,
                "updated_at": self.state["updated_at"],
            }

    def _sync_available(self) -> bool:
        site = str(self.config.get("site", "")).upper()
        required = {
            "SLEEPER": ("draft_id",),
            "ESPN": ("league_id",),
        }.get(site)
        return bool(required and all(self.config.get(key) for key in required))
