"""Drop-off chart data and draft-plan optimization for the web draft room."""

from __future__ import annotations

import itertools
from typing import Any

import pandas as pd

from fantasyfootball.adp_model import legacy_probability
from fantasyfootball.adp_model import (
    probability_available as modeled_probability_available,
)
from fantasyfootball.draft_state import (
    DraftError,
    DraftSession,
)
from fantasyfootball.player_matching import normalize_player_name

POSITIONS = ("QB", "RB", "WR", "TE")
CHART_METRICS = (
    "VOR_Floor",
    "VOR_Points",
    "VOR_Ceiling",
    "Floor",
    "Points",
    "Ceiling",
    "FLEX_VOR_Floor",
    "FLEX_VOR_Points",
    "FLEX_VOR_Ceiling",
)
LEAGUE_TRACKER_START_ROUND = 4
NFL_REGULAR_SEASON_GAMES = 17


def _available_frame(session: DraftSession) -> pd.DataFrame:
    drafted = {
        normalize_player_name(pick["player"])
        for pick in session.picks
        if pick.get("matched", True)
    }
    excluded = {
        normalize_player_name(player) for player in session.excluded_players
    }
    return session.full_df[
        ~session.full_df["Player"].map(normalize_player_name).isin(drafted)
        & ~session.full_df["Player"].map(normalize_player_name).isin(excluded)
    ].copy()


def _own_picks(session: DraftSession) -> list[int]:
    return [
        number
        for number in range(
            session.current_pick, session.teams * session.rounds + 1
        )
        if session.is_my_pick(number)
    ]


def _projected_pool(
    records: list[dict[str, Any]],
    picks_before: int,
    session: DraftSession,
) -> list[dict[str, Any]]:
    by_adp = _sort_by_adp(records, session)
    gone = {
        normalize_player_name(player["Player"])
        for player in by_adp[: max(0, picks_before)]
    }
    return [
        player
        for player in records
        if normalize_player_name(player["Player"]) not in gone
    ]


def _expected_adp(
    player: dict[str, Any], session: DraftSession | None = None
) -> float:
    if session is not None:
        distribution = session.adp_distribution(str(player["Player"]))
        if distribution is not None:
            return distribution.mean
    return float("inf") if pd.isna(player.get("ADP")) else float(player["ADP"])


def _sort_by_adp(
    records: list[dict[str, Any]], session: DraftSession | None = None
) -> list[dict[str, Any]]:
    return sorted(
        records,
        key=lambda player: _expected_adp(player, session),
    )


def _best_player(
    records: list[dict[str, Any]],
    slot: str,
    metric: str,
    flex_positions: tuple[str, ...],
) -> dict[str, Any] | None:
    eligible = set(flex_positions) if slot == "FLEX" else {slot}
    candidates = [
        player
        for player in records
        if player.get("Position") in eligible
        and not pd.isna(player.get(metric))
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda player: float(player[metric]))


def _pick_owner_slot(number: int, teams: int) -> int:
    """Return the draft slot that owns a pick in a snake draft."""
    round_index, offset = divmod(number - 1, teams)
    return offset + 1 if round_index % 2 == 0 else teams - offset


def _numeric_value(row: dict[str, Any], column: str) -> float:
    value = row.get(column)
    return 0.0 if value is None or pd.isna(value) else float(value)


def _expected_lineup(
    roster: list[dict[str, Any]],
    requirements: dict[str, int],
    flex_required: int,
    flex_positions: set[str],
) -> tuple[list[tuple[str, dict[str, Any]]], list[dict[str, Any]], list[str]]:
    """Select the highest projected scoring starters from a roster."""
    remaining = list(roster)
    lineup = []
    for position in POSITIONS:
        eligible = sorted(
            (
                player
                for player in remaining
                if player.get("Position") == position
            ),
            key=lambda player: _numeric_value(player, "Points"),
            reverse=True,
        )
        required = requirements[position]
        for index, player in enumerate(eligible[:required], 1):
            lineup_slot = position if required == 1 else f"{position}{index}"
            lineup.append((lineup_slot, player))
            remaining.remove(player)
    flex = sorted(
        (
            player
            for player in remaining
            if player.get("Position") in flex_positions
        ),
        key=lambda player: _numeric_value(player, "Points"),
        reverse=True,
    )
    for player in flex[:flex_required]:
        lineup.append(("FLEX", player))
        remaining.remove(player)

    filled_counts = {
        position: sum(
            1
            for lineup_slot, player in lineup
            if lineup_slot != "FLEX" and player.get("Position") == position
        )
        for position in POSITIONS
    }
    filled_counts["FLEX"] = sum(
        1 for lineup_slot, _ in lineup if lineup_slot == "FLEX"
    )
    missing = []
    for position in POSITIONS:
        missing.extend(
            [position]
            * max(0, requirements[position] - filled_counts[position])
        )
    missing.extend(["FLEX"] * max(0, flex_required - filled_counts["FLEX"]))
    return lineup, remaining, missing


def build_league_tracker(session: DraftSession) -> dict[str, Any]:
    """Forecast and rank every team's completed expected starting lineup."""
    round_number = (session.current_pick - 1) // session.teams + 1
    profile = session.state.get("optimizer_profile") or {}
    active = round_number >= LEAGUE_TRACKER_START_ROUND and bool(
        profile.get("auto_every_pick")
    )
    if not active:
        return {
            "active": False,
            "starts_round": LEAGUE_TRACKER_START_ROUND,
            "round": round_number,
            "metric": session.metric,
            "teams": [],
            "message": (
                "Starts in round 4 after the optimizer enters fast "
                "every-pick mode."
            ),
        }

    lookup = {
        normalize_player_name(str(row["Player"])): row
        for row in session.full_df.to_dict("records")
    }
    rosters: dict[int, list[dict[str, Any]]] = {
        slot: [] for slot in range(1, session.teams + 1)
    }
    untracked = {slot: 0 for slot in rosters}
    for pick in session.picks:
        number = int(pick["number"])
        owner = _pick_owner_slot(number, session.teams)
        row = lookup.get(normalize_player_name(str(pick["player"])))
        if not pick.get("matched", True) or row is None:
            untracked[owner] += 1
            continue
        rosters[owner].append({**row, "_projected": False})

    configured = session.config.get("positions", {})
    requirements = {
        position: int(configured.get(position, 0)) for position in POSITIONS
    }
    flex_required = int(configured.get("FLEX", 0))
    starter_slots = sum(requirements.values()) + flex_required
    drafted = {
        normalize_player_name(str(pick["player"]))
        for pick in session.picks
        if pick.get("matched", True)
    }
    projected_pool = _sort_by_adp(
        [
            {**row, "_projected": True}
            for row in session.full_df.to_dict("records")
            if normalize_player_name(str(row["Player"])) not in drafted
        ],
        session,
    )
    excluded = {
        normalize_player_name(player) for player in session.excluded_players
    }

    # Run one shared, sequential snake-draft forecast. Every simulated pick
    # consumes the player, so later teams see the same depleted pool they would
    # in the real room. Teams fill open starter positions in expected ADP order
    # before projecting bench selections.
    final_pick = session.teams * session.rounds
    for number in range(session.current_pick, final_pick + 1):
        if not projected_pool:
            break
        owner = _pick_owner_slot(number, session.teams)
        _, _, missing = _expected_lineup(
            rosters[owner],
            requirements,
            flex_required,
            set(session.flex_positions),
        )
        eligible_positions = {
            position for position in missing if position != "FLEX"
        }
        if "FLEX" in missing:
            eligible_positions.update(session.flex_positions)

        candidate_index = None
        for index, player in enumerate(projected_pool):
            key = normalize_player_name(str(player["Player"]))
            if owner == session.draft_slot and key in excluded:
                continue
            if (
                not eligible_positions
                or player.get("Position") in eligible_positions
            ):
                candidate_index = index
                break
        fills_starter = (
            bool(eligible_positions) and candidate_index is not None
        )
        if candidate_index is None:
            candidate_index = 0
        player = projected_pool.pop(candidate_index)
        if fills_starter:
            player["_projected_pick"] = number
            rosters[owner].append(player)

    teams = []
    for slot, roster in rosters.items():
        lineup, remaining, missing = _expected_lineup(
            roster,
            requirements,
            flex_required,
            set(session.flex_positions),
        )
        lineup_payload = []
        for lineup_slot, player in lineup:
            projected = bool(player.get("_projected"))
            payload = {
                "slot": lineup_slot,
                "position": str(player["Position"]),
                "points": round(
                    _numeric_value(player, "Points")
                    / NFL_REGULAR_SEASON_GAMES,
                    1,
                ),
                "projected": projected,
            }
            if not projected:
                payload["player"] = str(player["Player"])
            lineup_payload.append(payload)
        teams.append(
            {
                "slot": slot,
                "label": (
                    f"{session.team_names.get(slot, f'Team {slot}')} · You"
                    if slot == session.draft_slot
                    else session.team_names.get(slot, f"Team {slot}")
                ),
                "mine": slot == session.draft_slot,
                "lineup": lineup_payload,
                "missing": missing,
                "filled": len(lineup_payload),
                "starter_slots": starter_slots,
                "starter_points": round(
                    sum(player["points"] for player in lineup_payload), 1
                ),
                "starter_value": round(
                    sum(
                        _numeric_value(player, session.metric)
                        for _, player in lineup
                    ),
                    1,
                ),
                "forecasted_starters": sum(
                    1 for player in lineup_payload if player["projected"]
                ),
                "bench_count": len(remaining),
                "untracked_count": untracked[slot],
            }
        )

    # Compare each starter to the other teams' expected starter in the exact
    # same lineup tier. RB1/RB2 and WR1/WR2 are deliberately separate groups.
    comparison_groups: dict[str, list[tuple[int, float]]] = {}
    for team in teams:
        for player in team["lineup"]:
            comparison_groups.setdefault(player["slot"], []).append(
                (team["slot"], float(player["points"]))
            )
    for team in teams:
        team_value = 0.0
        for player in team["lineup"]:
            others = [
                points
                for slot, points in comparison_groups[player["slot"]]
                if slot != team["slot"]
            ]
            if not others:
                player["comparison_average"] = None
                player["value_over_average"] = None
                continue
            average = sum(others) / len(others)
            value = float(player["points"]) - average
            player["comparison_average"] = round(average, 1)
            player["value_over_average"] = round(value, 1)
            team_value += value
        team["value_over_average"] = round(team_value, 1)

    teams.sort(
        key=lambda team: (
            team["starter_points"],
            team["starter_value"],
        ),
        reverse=True,
    )
    for rank, team in enumerate(teams, 1):
        team["rank"] = rank
    return {
        "active": True,
        "starts_round": LEAGUE_TRACKER_START_ROUND,
        "round": round_number,
        "metric": session.metric,
        "points_basis": "per_game",
        "projection_games": NFL_REGULAR_SEASON_GAMES,
        "teams": teams,
        "message": None,
    }


def build_dropoff_chart(
    session: DraftSession,
    limit: int = 18,
    metric: str | None = None,
) -> dict[str, Any]:
    """Return the same positional drop-off view as the terminal chart."""
    metric = session.metric if metric is None else metric
    if metric not in CHART_METRICS:
        raise DraftError(f"Unsupported chart metric: {metric}")
    if not 3 <= limit <= 60:
        raise DraftError("Chart rank limit must be between 3 and 60.")
    available = _available_frame(session)
    records = available.to_dict("records")
    own_picks = _own_picks(session)[:2]
    marker_labels = ("Your next pick", "Following pick")
    series = []
    recommendations = []

    for position in POSITIONS:
        position_frame = available[available["Position"] == position]
        position_frame = position_frame.sort_values(metric, ascending=False)
        points = [
            {
                "rank": rank,
                "player": str(row["Player"]),
                "value": round(float(row[metric]), 2),
            }
            for rank, (_, row) in enumerate(
                position_frame.head(limit).iterrows(), 1
            )
        ]
        markers = []
        projected_players = []
        for label, pick_number in zip(marker_labels, own_picks, strict=False):
            pool = _projected_pool(
                records,
                max(0, pick_number - session.current_pick),
                session,
            )
            player = _best_player(
                pool, position, metric, session.flex_positions
            )
            if player is None:
                continue
            player_name = str(player["Player"])
            ranks = position_frame.index[
                position_frame["Player"] == player_name
            ].tolist()
            rank = (
                position_frame.index.get_loc(ranks[0]) + 1 if ranks else None
            )
            markers.append(
                {
                    "label": label,
                    "pick": pick_number,
                    "rank": rank,
                    "player": player_name,
                    "value": round(float(player[metric]), 2),
                }
            )
            projected_players.append(player)
        dropoff = None
        if len(projected_players) == 2:
            dropoff = round(
                float(projected_players[0][metric])
                - float(projected_players[1][metric]),
                2,
            )
            recommendations.append(
                {
                    "position": position,
                    "player": str(projected_players[0]["Player"]),
                    "dropoff": dropoff,
                }
            )
        series.append(
            {
                "position": position,
                "points": points,
                "markers": markers,
                "dropoff": dropoff,
            }
        )

    recommended = (
        max(recommendations, key=lambda item: item["dropoff"])
        if recommendations
        else None
    )
    return {
        "metric": metric,
        "metrics": list(CHART_METRICS),
        "limit": limit,
        "series": series,
        "recommended": recommended,
        "own_picks": own_picks,
    }


def _starter_slots_remaining(session: DraftSession) -> list[str]:
    requirements = {
        position: int(session.config.get("positions", {}).get(position, 0))
        for position in POSITIONS
    }
    flex_required = int(session.config.get("positions", {}).get("FLEX", 0))
    roster_positions = []
    lookup = {
        normalize_player_name(str(row["Player"])): str(row["Position"])
        for _, row in session.full_df.iterrows()
    }
    for pick in session.picks:
        if pick.get("mine") and pick.get("matched", True):
            position = lookup.get(normalize_player_name(pick["player"]))
            if position:
                roster_positions.append(position)

    counts = {
        position: roster_positions.count(position) for position in POSITIONS
    }
    slots = []
    for position in POSITIONS:
        slots.extend(
            [position] * max(0, requirements[position] - counts[position])
        )
    flex_used = sum(
        max(0, counts[position] - requirements[position])
        for position in session.flex_positions
    )
    slots.extend(["FLEX"] * max(0, flex_required - flex_used))
    return slots


def _probability_available(adp: Any, pick: int, teams: int) -> float:
    return round(legacy_probability(adp, pick, teams), 3)


def _availability_estimate(
    session: DraftSession, player: dict[str, Any], pick: int
) -> dict[str, Any]:
    distribution = session.adp_distribution(str(player["Player"]))
    if (
        distribution is None
        or distribution.stdev is None
        or distribution.stdev <= 0
    ):
        probability = legacy_probability(
            player.get("ADP"), pick, session.teams
        )
        current = legacy_probability(
            player.get("ADP"), session.current_pick, session.teams
        )
        return {
            "probability": 1.0
            if pick <= session.current_pick
            else round(
                min(1.0, probability / current)
                if current > 0 and not pd.isna(player.get("ADP"))
                else probability,
                3,
            ),
            "probability_source": "saved-adp-fallback",
            "probability_mean": None,
            "probability_stdev": None,
            "probability_samples": None,
        }
    probability = modeled_probability_available(
        distribution,
        pick,
        fallback_adp=player.get("ADP"),
        teams=session.teams,
    )
    current = modeled_probability_available(
        distribution,
        session.current_pick,
        fallback_adp=player.get("ADP"),
        teams=session.teams,
    )
    probability = (
        1.0
        if pick <= session.current_pick
        else min(1.0, probability / current)
        if current > 0
        else probability
    )
    return {
        "probability": round(probability, 3),
        "probability_source": "ffc",
        "probability_mean": round(distribution.mean, 1),
        "probability_stdev": (
            None
            if distribution.stdev is None
            else round(distribution.stdev, 1)
        ),
        "probability_samples": distribution.samples,
    }


def _optimizer_value_key(slot: str) -> str:
    return {"BENCH": "_bench_value", "FLEX": "_flex_value"}.get(slot, "_value")


def _role_eligible(player, slot, outstanding, session):
    # A player fills an open dedicated position before FLEX. Other slot
    # orders evaluate taking that position first, preserving scarce players
    # for dedicated slots rather than greedily consuming them in FLEX.
    if slot == "FLEX":
        return player["Position"] not in outstanding
    if slot == "BENCH":
        return _bench_eligible(player, outstanding, session)
    return True


def _simulate_plan(
    session: DraftSession,
    pool: dict[str, Any],
    slots: tuple[str, ...],
    targets: list[int],
    first_choice: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    unavailable: set[str] = set()
    adp_cursor = 0
    selections = []
    previous_pick = session.current_pick - 1
    total = 0.0
    risk_adjusted_total = 0.0
    outstanding = list(pool.get("starter_slots", slots))

    for slot, target in zip(slots, targets, strict=True):
        picks_before = target - previous_pick - 1
        removed = 0
        while removed < picks_before and adp_cursor < len(pool["by_adp"]):
            player = pool["by_adp"][adp_cursor]
            adp_cursor += 1
            if player["_key"] in unavailable:
                continue
            unavailable.add(player["_key"])
            removed += 1
        choice = next(
            (
                player
                for player in pool["by_slot"][slot]
                if player["_key"] not in unavailable
                and _role_eligible(player, slot, outstanding, session)
            ),
            None,
        )
        if not selections and first_choice is not None:
            choice = first_choice
            if choice["_key"] in unavailable:
                return None
            if not _role_eligible(choice, slot, outstanding, session):
                return None
        if choice is None:
            return None
        backup = next(
            (
                player
                for player in pool["by_slot"][slot]
                if player["_key"] not in unavailable
                and player["_key"] != choice["_key"]
                and player["_adp"] >= target
                and _role_eligible(player, slot, outstanding, session)
            ),
            None,
        )
        value_key = _optimizer_value_key(slot)
        value = choice[value_key]
        backup_value = min(0.0, value) if backup is None else backup[value_key]
        cache_key = (choice["_key"], target)
        if cache_key not in pool["availability"]:
            pool["availability"][cache_key] = _availability_estimate(
                session, choice, target
            )
        availability = pool["availability"][cache_key]
        probability = availability["probability"]
        risk_adjusted = probability * value + (1 - probability) * backup_value
        round_number, offset = divmod(target - 1, session.teams)
        selections.append(
            {
                "pick": target,
                "pick_label": f"{round_number + 1}:{offset + 1}",
                "slot": slot,
                "player": str(choice["Player"]),
                "position": str(choice["Position"]),
                "bye": None
                if pd.isna(choice.get("Bye"))
                else choice.get("Bye"),
                "adp": None
                if pd.isna(choice.get("ADP"))
                else round(float(choice["ADP"]), 1),
                "value": round(value, 2),
                "value_metric": "Ceiling"
                if slot == "BENCH"
                else pool["flex_metric"]
                if slot == "FLEX"
                else pool["starter_metric"],
                **availability,
                "backup": None if backup is None else str(backup["Player"]),
            }
        )
        total += value
        risk_adjusted_total += risk_adjusted
        unavailable.add(choice["_key"])
        if slot in outstanding:
            outstanding.remove(slot)
        previous_pick = target

    return {
        "selections": selections,
        "total": round(total, 2),
        "risk_adjusted_total": round(risk_adjusted_total, 2),
    }


def _prepare_optimizer_pool(
    available: pd.DataFrame, metric: str, session: DraftSession
) -> dict[str, Any]:
    flex_metric = f"FLEX_{metric}" if metric.startswith("VOR_") else metric
    if flex_metric not in available.columns:
        raw_metric = metric.removeprefix("VOR_")
        flex_metric = raw_metric if raw_metric in available.columns else metric
    records = []
    for raw in available.to_dict("records"):
        if pd.isna(raw.get(metric)):
            continue
        player = dict(raw)
        player["_key"] = normalize_player_name(str(player["Player"]))
        player["_adp"] = _expected_adp(player, session)
        player["_value"] = float(player[metric])
        player["_flex_value"] = _numeric_value(player, flex_metric)
        player["_bench_value"] = _numeric_value(player, "Ceiling")
        records.append(player)
    by_slot = {}
    for slot in (*POSITIONS, "FLEX"):
        eligible = set(session.flex_positions) if slot == "FLEX" else {slot}
        by_slot[slot] = sorted(
            (
                player
                for player in records
                if player.get("Position") in eligible
            ),
            key=lambda player: player[_optimizer_value_key(slot)],
            reverse=True,
        )
    return {
        "by_adp": sorted(records, key=lambda player: player["_adp"]),
        "by_slot": by_slot,
        "availability": {},
        "starter_metric": metric,
        "flex_metric": flex_metric,
    }


def _bench_eligible(player, outstanding, session):
    position = player["Position"]
    return position not in outstanding and not (
        "FLEX" in outstanding and position in session.flex_positions
    )


def _late_draft_plans(session, available, slots, own_picks, max_plans):
    """Bounded four-pick search; no bench search during early rounds."""
    pool = _prepare_optimizer_pool(available, "VOR_Points", session)
    pool["starter_slots"] = slots
    pool["by_slot"]["BENCH"] = sorted(
        (
            p
            for p in pool["by_adp"]
            if p["Position"] in {"RB", "WR"} and not pd.isna(p.get("Ceiling"))
        ),
        key=lambda p: p["_bench_value"],
        reverse=True,
    )
    targets = own_picks[:4]
    first_pool = _projected_pool(
        pool["by_adp"], max(0, targets[0] - session.current_pick), session
    )
    first_keys = {p["_key"] for p in first_pool}
    choices = sorted(set(slots + ["BENCH"]))
    plans = []
    seen = set()
    # Compare gains over players projected to remain later, so an open QB
    # slot can wait while a scarce bench target is secured. All plans use the
    # same baseline per role; raw ceiling does not swamp starter VOR values.
    later_pool = _projected_pool(
        pool["by_adp"], max(0, own_picks[-1] - session.current_pick), session
    )
    later_keys = {p["_key"] for p in later_pool}
    baselines = {}
    for slot in choices:
        key = _optimizer_value_key(slot)
        candidates = pool["by_slot"][slot]
        later = [p[key] for p in candidates if p["_key"] in later_keys]
        baselines[slot] = max(
            later, default=min((p[key] for p in candidates), default=0)
        )
    for order in itertools.product(choices, repeat=len(targets)):
        if any(order.count(slot) > slots.count(slot) for slot in set(slots)):
            continue
        unfilled = len(slots) - sum(slot != "BENCH" for slot in order)
        if unfilled > len(own_picks) - len(targets):
            continue
        # At most 12 first candidates x 81 orders, independent of draft length.
        first_candidates = [
            p
            for p in pool["by_slot"][order[0]]
            if p["_key"] in first_keys
            and _role_eligible(p, order[0], slots, session)
        ]
        for first in first_candidates[:12]:
            plan = _simulate_plan(session, pool, order, targets, first)
            if plan is None:
                continue
            identity = tuple(
                (p["slot"], p["player"]) for p in plan["selections"]
            )
            if identity in seen:
                continue
            seen.add(identity)
            plan["slot_order"] = list(order)
            plan["decision_score"] = round(
                plan["risk_adjusted_total"] - sum(baselines[s] for s in order),
                2,
            )
            plans.append(plan)
    plans.sort(
        key=lambda p: (p["decision_score"], p["risk_adjusted_total"]),
        reverse=True,
    )
    # Distinct first-pick alternatives are more useful than repeated plans.
    result = []
    first_names = set()
    for plan in plans:
        name = plan["selections"][0]["player"]
        if name not in first_names:
            result.append(plan)
            first_names.add(name)
        if len(result) == max_plans:
            break
    return result


def _add_wait_odds(session, plans, own_picks, available):
    lookup = {str(p["Player"]): p for p in available.to_dict("records")}
    for plan in plans:
        for selection in plan["selections"]:
            following = next(
                (p for p in own_picks if p > selection["pick"]), None
            )
            selection["following_pick"] = following
            selection["wait_probability"] = (
                None
                if following is None
                else _availability_estimate(
                    session, lookup[selection["player"]], following
                )["probability"]
            )


def optimize_draft(
    session: DraftSession, max_plans: int = 4
) -> dict[str, Any]:
    """Project the strongest remaining starter builds at future own picks."""
    slots = _starter_slots_remaining(session)
    own_picks = _own_picks(session)
    reserved = session.config.get("late_round_positions", [])
    if not isinstance(reserved, list) or any(
        p not in {"DST", "K"} for p in reserved
    ):
        raise DraftError("late_round_positions must contain DST and/or K.")
    offensive_picks = [
        p
        for p in own_picks
        if (p - 1) // session.teams < session.rounds - len(reserved)
    ]
    if not offensive_picks:
        taken = {p.get("position") for p in session.picks if p.get("mine")}
        remaining = [p for p in reserved if p not in taken]
        return {
            "metric": session.metric,
            "slots_remaining": slots,
            "plans": [],
            "phase": "finish",
            "message": (
                "Your draft is complete."
                if not own_picks
                else "Final rounds: draft "
                + " and ".join("D/ST" if p == "DST" else p for p in remaining)
                + ". Choose on your draft platform, then sync or record "
                "with + D/ST / + K."
            )
            if remaining or not own_picks
            else "Your reserved D/ST and K picks are filled.",
        }
    available = _available_frame(session)
    if len(slots) <= 2:
        plans = _late_draft_plans(
            session, available, slots, offensive_picks, max_plans
        )
        _add_wait_odds(session, plans, own_picks, available)
        return {
            "metric": "VOR_Points",
            "bench_metric": "Ceiling",
            "phase": "bench" if not slots else "transition",
            "slots_remaining": slots,
            "plans": plans,
            "message": (
                "Four-pick forecast · starters: VOR Points · RB/WR bench: "
                "Ceiling · estimates account for players still available."
                if plans
                else "No feasible four-pick plan remains. "
                "Review the board and remaining starter needs."
            ),
        }
    targets = offensive_picks[: len(slots)]
    if len(targets) < len(slots):
        slots = slots[: len(targets)]
    pool = _prepare_optimizer_pool(available, session.metric, session)
    unique_orders = sorted(set(itertools.permutations(slots)))
    plans = []
    seen_players = set()
    for order in unique_orders:
        plan = _simulate_plan(session, pool, order, targets)
        if plan is None:
            continue
        identity = tuple(pick["player"] for pick in plan["selections"])
        if identity in seen_players:
            continue
        seen_players.add(identity)
        plan["slot_order"] = list(order)
        plans.append(plan)
    plans.sort(
        key=lambda plan: (plan["risk_adjusted_total"], plan["total"]),
        reverse=True,
    )
    _add_wait_odds(session, plans[:max_plans], own_picks, available)
    return {
        "metric": session.metric,
        "slots_remaining": slots,
        "plans": plans[:max_plans],
        "message": None
        if plans
        else "No complete draft plan could be projected.",
    }
