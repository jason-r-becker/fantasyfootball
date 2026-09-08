import json
from pathlib import Path
from urllib.error import HTTPError

import pandas as pd
import pytest

from fantasyfootball import draft_sources
from fantasyfootball.adp_model import ADPModel, PlayerDraftDistribution
from fantasyfootball.draft_analysis import (
    build_dropoff_chart,
    build_league_tracker,
    optimize_draft,
)
from fantasyfootball.draft_sources import (
    _ESPN_PLAYER_MAPS,
    _espn_player_map,
    parse_espn_picks,
    parse_sleeper_picks,
    parse_sleeper_team_names,
)
from fantasyfootball.draft_state import DraftError, DraftSession

PLAYERS = [
    ("Alpha Runner", "AAA", "RB", 5, 1.0, 20.0),
    ("Bravo Receiver Jr.", "BBB", "WR", 7, 2.0, 18.0),
    ("Charlie Passer", "CCC", "QB", 9, 3.0, 17.0),
    ("Delta Tight End", "DDD", "TE", 11, 4.0, 15.0),
    ("Echo Runner", "EEE", "RB", 6, 5.0, 14.0),
    ("Foxtrot Receiver", "FFF", "WR", 8, 6.0, 13.0),
]
PLAYERS.extend(
    (
        f"Depth Player {index}",
        "TST",
        ("QB", "RB", "WR", "TE")[index % 4],
        10,
        float(index + 7),
        12.0 - index * 0.25,
    )
    for index in range(24)
)


def make_league(
    tmp_path: Path,
    *,
    site: str = "Sleeper",
    draft_rounds: int | None = None,
) -> Path:
    league = tmp_path / "data" / "2026" / "test"
    league.mkdir(parents=True)
    config = {
        "teams": 4,
        "positions": {"QB": 1, "RB": 1, "WR": 1, "TE": 1, "FLEX": 1},
        "site": site,
        "adp_model": {"source": "disabled"},
        "league_id": "league",
        "draft_id": "draft",
        "user_id": "me",
        "team_id": 2,
    }
    if draft_rounds is not None:
        config["draft_rounds"] = draft_rounds
    (league / "config.json").write_text(json.dumps(config), encoding="utf-8")
    frame = pd.DataFrame(
        PLAYERS,
        columns=["Player", "Team", "Position", "Bye", "ADP", "VOR_Points"],
    )
    frame["VOR_Floor"] = frame["VOR_Points"] - 2
    frame["VOR_Ceiling"] = frame["VOR_Points"] + 2
    frame["Floor"] = frame["VOR_Points"] + 80
    frame["Points"] = frame["VOR_Points"] + 100
    frame["Ceiling"] = frame["VOR_Points"] + 120
    frame["FLEX_VOR_Points"] = frame["VOR_Points"] - 3
    frame["Std Dev"] = 3.5
    frame.to_csv(league / "clean.csv")
    frame.to_csv(league / "live_draft.csv")
    return league


def test_practice_session_is_persistent_and_does_not_touch_live_csv(tmp_path):
    league = make_league(tmp_path)
    original = (league / "live_draft.csv").read_bytes()
    session = DraftSession(
        tmp_path, 2026, "test", 2, rounds=3, mode="practice"
    )

    pick = session.add_pick("Alpha Runner", locked=True)

    assert pick["number"] == 1
    assert pick["mine"] is False
    assert (league / "live_draft.csv").read_bytes() == original
    remaining = pd.read_csv(league / "practice_live_draft.csv")
    assert "Alpha Runner" not in set(remaining["Player"])
    restored = DraftSession(
        tmp_path, 2026, "test", 2, rounds=3, mode="practice"
    )
    assert restored.picks[0]["player"] == "Alpha Runner"


def test_league_name_cannot_escape_private_data_directory(tmp_path):
    with pytest.raises(DraftError, match="League must contain"):
        DraftSession(tmp_path, 2026, "../outside", 1)

    assert not (tmp_path / "outside").exists()


def test_pick_files_update_immediately_and_undo_cleanly(tmp_path):
    league = make_league(tmp_path)
    session = DraftSession(tmp_path, 2026, "test", 1, mode="live")

    session.add_pick("Alpha Runner")

    available = pd.read_csv(league / "live_draft.csv")
    drafted = pd.read_csv(league / "drafted_players.csv")
    assert "Alpha Runner" not in set(available["Player"])
    assert drafted.loc[0, "Player"] == "Alpha Runner"
    assert drafted.loc[0, "Position"] == "RB"
    assert drafted.loc[0, "Pick"] == 1
    assert session.public_state()["picks"][0]["adp"] == 1.0

    session.undo_pick(1)

    available = pd.read_csv(league / "live_draft.csv")
    drafted = pd.read_csv(league / "drafted_players.csv")
    assert "Alpha Runner" in set(available["Player"])
    assert drafted.empty


def test_config_rounds_and_manual_k_dst_fallback(tmp_path):
    league = make_league(tmp_path, draft_rounds=2)
    session = DraftSession(tmp_path, 2026, "test", 1)
    original_available = [
        player["Player"] for player in session.public_state()["players"]
    ]

    kicker = session.add_untracked_pick("K")
    defense = session.add_untracked_pick("D/ST")

    assert session.rounds == 2
    assert kicker["number"] == 1
    assert kicker["position"] == "K"
    assert defense["number"] == 2
    assert defense["position"] == "DST"
    assert session.current_pick == 3
    assert [
        player["Player"] for player in session.public_state()["players"]
    ] == original_available
    drafted = pd.read_csv(league / "practice_drafted_players.csv")
    assert list(drafted["Position"]) == ["K", "DST"]
    assert list(drafted["Matched"]) == [False, False]

    restored = DraftSession(tmp_path, 2026, "test", 1)
    assert [pick["position"] for pick in restored.picks] == ["K", "DST"]


def test_configured_flex_eligibility_includes_tight_end(tmp_path):
    league = make_league(tmp_path)
    config_path = league / "config.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["positions"] = {
        "QB": 0,
        "RB": 0,
        "WR": 0,
        "TE": 0,
        "FLEX": 1,
    }
    config["flex_positions"] = ["TE"]
    config_path.write_text(json.dumps(config), encoding="utf-8")

    # With one turn left, the optimizer must fill this FLEX rather than
    # considering a bench pick while postponing the final starter.
    session = DraftSession(tmp_path, 2026, "test", 1, rounds=1)
    optimized = optimize_draft(session)

    assert session.public_state()["flex_positions"] == ["TE"]
    assert optimized["plans"][0]["selections"][0]["slot"] == "FLEX"
    assert optimized["plans"][0]["selections"][0]["position"] == "TE"


def test_invalid_flex_eligibility_is_rejected(tmp_path):
    league = make_league(tmp_path)
    config_path = league / "config.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["flex_positions"] = ["RB", "K"]
    config_path.write_text(json.dumps(config), encoding="utf-8")

    with pytest.raises(DraftError, match="flex_positions"):
        DraftSession(tmp_path, 2026, "test", 1)


def test_null_config_rounds_uses_default(tmp_path):
    league = make_league(tmp_path)
    config_path = league / "config.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["draft_rounds"] = None
    config_path.write_text(json.dumps(config), encoding="utf-8")

    session = DraftSession(tmp_path, 2026, "test", 1)

    assert session.rounds == 15


def test_completed_draft_is_never_reported_on_the_clock(tmp_path):
    make_league(tmp_path, draft_rounds=1)
    session = DraftSession(tmp_path, 2026, "test", 1)
    for position in ("K", "DST", "K", "DST"):
        session.add_untracked_pick(position)

    state = session.public_state()
    assert state["draft_complete"] is True
    assert state["on_the_clock"] is False
    assert state["next_own_pick"] is None


def test_public_state_never_exposes_platform_ids_or_credentials(tmp_path):
    league = make_league(tmp_path)
    config_path = league / "config.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config.update(
        {
            "draft_id": "private-draft-identifier",
            "league_id": "private-league-identifier",
            "user_id": "private-user-identifier",
            "swid": "private-swid",
            "espn_s2": "private-cookie",
        }
    )
    config_path.write_text(json.dumps(config), encoding="utf-8")

    session = DraftSession(tmp_path, 2026, "test", 1)
    session.reconcile_platform_picks(
        [
            {
                "number": 1,
                "player": "Alpha Runner",
                "source": "Sleeper API",
                "external_id": "private-player-identifier",
            }
        ]
    )
    public = json.dumps(session.public_state())

    assert "private-swid" not in public
    assert "private-cookie" not in public
    assert config["draft_id"] not in public
    assert config["league_id"] not in public
    assert config["user_id"] not in public
    assert "private-player-identifier" not in public
    assert "external_id" not in public


def test_optimizer_profile_latches_fast_every_pick_mode(tmp_path):
    make_league(tmp_path)
    session = DraftSession(tmp_path, 2026, "test", 1)

    slow = session.record_optimizer_run(0.75)
    fast = session.record_optimizer_run(0.2)
    later_slow = session.record_optimizer_run(0.8)

    assert slow["auto_every_pick"] is False
    assert slow["last_ms"] == 750.0
    assert fast["auto_every_pick"] is True
    assert fast["fastest_ms"] == 200.0
    assert later_slow["auto_every_pick"] is True
    assert later_slow["runs"] == 3
    restored = DraftSession(tmp_path, 2026, "test", 1)
    assert restored.public_state()["optimizer_profile"] == later_slow


def test_league_tracker_starts_in_round_four_after_fast_profile(tmp_path):
    make_league(tmp_path)
    session = DraftSession(tmp_path, 2026, "test", 1, rounds=8)
    session.record_optimizer_run(0.2)

    inactive = build_league_tracker(session)
    assert inactive["active"] is False
    for number, player in enumerate(PLAYERS[:12], 1):
        session.add_pick(player[0], number=number)

    tracker = build_league_tracker(session)
    assert tracker["active"] is True
    assert tracker["round"] == 4
    assert tracker["points_basis"] == "per_game"
    assert tracker["projection_games"] == 17
    assert len(tracker["teams"]) == 4
    assert [team["rank"] for team in tracker["teams"]] == [1, 2, 3, 4]
    assert all(team["starter_slots"] == 5 for team in tracker["teams"])
    assert all(team["filled"] == 5 for team in tracker["teams"])
    projected = [
        player
        for team in tracker["teams"]
        for player in team["lineup"]
        if player["projected"]
    ]
    assert projected
    assert all("player" not in player for player in projected)
    assert all(player["points"] > 0 for player in projected)
    assert all(
        player["comparison_average"] is not None
        and player["value_over_average"] is not None
        for team in tracker["teams"]
        for player in team["lineup"]
    )
    mine = next(team for team in tracker["teams"] if team["mine"])
    assert mine["label"] == "Team 1 · You"

    session.set_team_names({1: "Alpha Squad", 2: "Bravo Squad"})
    named = build_league_tracker(session)
    mine = next(team for team in named["teams"] if team["mine"])
    assert mine["label"] == "Alpha Squad · You"
    assert session.team_names == {1: "Alpha Squad", 2: "Bravo Squad"}


def test_sleeper_team_names_prefer_account_names():
    names = parse_sleeper_team_names(
        {"draft_order": {"u1": 2, "u2": 1}},
        [
            {
                "user_id": "u1",
                "display_name": "owner-one",
                "metadata": {"team_name": "Custom Team"},
            },
            {"user_id": "u2", "display_name": "owner-two"},
        ],
    )

    assert names == {1: "owner-two", 2: "owner-one"}


def test_projected_bench_pick_cannot_replace_drafted_starter(tmp_path):
    make_league(tmp_path)
    session = DraftSession(tmp_path, 2026, "test", 1, rounds=8)
    session.record_optimizer_run(0.2)
    team_one = {
        1: "Charlie Passer",
        8: "Depth Player 1",
        9: "Depth Player 2",
        16: "Depth Player 3",
        17: "Depth Player 5",
    }
    used = set(team_one.values())
    others = iter(player[0] for player in PLAYERS if player[0] not in used)
    for number in range(1, 18):
        session.add_pick(team_one.get(number) or next(others), number=number)

    tracker = build_league_tracker(session)
    mine = next(team for team in tracker["teams"] if team["mine"])

    assert mine["filled"] == mine["starter_slots"] == 5
    assert mine["forecasted_starters"] == 0
    assert {player["player"] for player in mine["lineup"]} == set(
        team_one.values()
    )


def test_league_comparisons_separate_numbered_starter_tiers(tmp_path):
    league = make_league(tmp_path)
    config_path = league / "config.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["positions"] = {
        "QB": 0,
        "RB": 2,
        "WR": 2,
        "TE": 0,
        "FLEX": 0,
    }
    config_path.write_text(json.dumps(config), encoding="utf-8")
    session = DraftSession(tmp_path, 2026, "test", 1, rounds=8)
    session.record_optimizer_run(0.2)
    for number, player in enumerate(PLAYERS[:12], 1):
        session.add_pick(player[0], number=number)

    tracker = build_league_tracker(session)

    for team in tracker["teams"]:
        slots = {player["slot"] for player in team["lineup"]}
        assert {"RB1", "RB2", "WR1", "WR2"} <= slots
        assert "RB" not in slots
        assert "WR" not in slots
        assert isinstance(team["value_over_average"], float)


def test_draft_slot_can_come_from_league_config(tmp_path):
    league = make_league(tmp_path)
    config_path = league / "config.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["draft_slot"] = 1
    config_path.write_text(json.dumps(config), encoding="utf-8")

    session = DraftSession(tmp_path, 2026, "test", None)

    assert session.draft_slot == 1
    assert session.is_my_pick(1)
    assert session.is_my_pick(8)


@pytest.mark.parametrize("site", ["Sleeper", "ESPN"])
def test_ffc_refresh_is_platform_neutral(tmp_path, monkeypatch, site):
    make_league(tmp_path, site=site)
    model = ADPModel(
        enabled=True,
        available=True,
        status="live",
        scoring_format="ppr",
        teams=4,
        players=(
            PlayerDraftDistribution(
                name="Alpha Runner",
                mean=42.5,
                stdev=4.0,
                samples=500,
                team="AAA",
                position="RB",
                bye=12,
            ),
        ),
    )
    monkeypatch.setattr(
        "fantasyfootball.draft_state.load_adp_model",
        lambda config, year, path: model,
    )

    session = DraftSession(tmp_path, 2026, "test", 1)
    alpha = session.full_df[session.full_df["Player"] == "Alpha Runner"]

    assert alpha.iloc[0]["ADP"] == 42.5
    assert alpha.iloc[0]["Bye"] == 12
    assert alpha.iloc[0]["ADP Source"] == "FFC"
    assert alpha.iloc[0]["ADP Std Dev"] == 4.0
    assert alpha.iloc[0]["ADP Samples"] == 500
    assert session.public_state()["adp_refresh_count"] == 1


def test_confirmed_season_alias_maps_to_ranked_player(tmp_path):
    make_league(tmp_path)
    aliases = tmp_path / "data" / "2026" / "source_player_map.json"
    aliases.write_text(
        json.dumps({"Al Runner": "Alpha Runner", "AAA": "Alpha Runner"}),
        encoding="utf-8",
    )
    session = DraftSession(tmp_path, 2026, "test", 1)

    pick = session.add_pick("Al Runner")

    assert pick["player"] == "Alpha Runner"
    assert session.public_state()["alias_count"] == 1
    assert session._canonical_name("AAA") is None


@pytest.mark.parametrize("site", ["Sleeper", "ESPN"])
def test_platform_reconciliation_uses_shared_safe_aliases(tmp_path, site):
    make_league(tmp_path, site=site)
    aliases = tmp_path / "data" / "2026" / "source_player_map.json"
    aliases.write_text(
        json.dumps({"B Receiver": "Bravo Receiver Jr."}),
        encoding="utf-8",
    )
    session = DraftSession(tmp_path, 2026, "test", 1)

    matched = session.reconcile_platform_picks(
        [
            {
                "number": 1,
                "player": "B Receiver",
                "position": "WR",
                "player_team": "BBB",
                "source": f"{site} API",
            }
        ]
    )

    assert matched["added"][0]["player"] == "Bravo Receiver Jr."
    assert matched["unmatched"] == []


@pytest.mark.parametrize("site", ["Sleeper", "ESPN"])
def test_platform_reconciliation_rejects_position_conflict(tmp_path, site):
    make_league(tmp_path, site=site)
    session = DraftSession(tmp_path, 2026, "test", 1)

    result = session.reconcile_platform_picks(
        [
            {
                "number": 1,
                "player": "Bravo Receiver",
                "position": "RB",
                "source": f"{site} API",
            }
        ]
    )

    assert result["added"][0]["matched"] is False
    assert result["unmatched"][0]["player"] == "Bravo Receiver"


@pytest.mark.parametrize("site", ["Sleeper", "ESPN"])
def test_platform_reconciliation_rejects_team_conflict(tmp_path, site):
    make_league(tmp_path, site=site)
    session = DraftSession(tmp_path, 2026, "test", 1)

    result = session.reconcile_platform_picks(
        [
            {
                "number": 1,
                "player": "Bravo Receiver",
                "position": "WR",
                "player_team": "DIFFERENT",
                "source": f"{site} API",
            }
        ]
    )

    assert result["added"][0]["matched"] is False
    assert result["unmatched"][0]["player"] == "Bravo Receiver"


def test_chart_and_optimizer_reproduce_terminal_draft_analysis(tmp_path):
    make_league(tmp_path)
    session = DraftSession(tmp_path, 2026, "test", 1)

    chart = build_dropoff_chart(session)
    optimized = optimize_draft(session)

    assert chart["own_picks"] == [1, 8]
    assert {series["position"] for series in chart["series"]} == {
        "QB",
        "RB",
        "WR",
        "TE",
    }
    assert chart["recommended"] is not None
    custom_chart = build_dropoff_chart(session, limit=3, metric="Ceiling")
    assert custom_chart["metric"] == "Ceiling"
    assert custom_chart["limit"] == 3
    assert all(len(series["points"]) <= 3 for series in custom_chart["series"])
    with pytest.raises(DraftError, match="between 3 and 60"):
        build_dropoff_chart(session, limit=2)
    assert len(optimized["plans"]) == 4
    assert [pick["pick"] for pick in optimized["plans"][0]["selections"]] == [
        1,
        8,
        9,
        16,
        17,
    ]
    projected = {
        pick["player"] for pick in optimized["plans"][0]["selections"]
    }
    assert "Alpha Runner" in projected
    assert len(projected) == 5

    session.set_player_excluded("Alpha Runner", True)
    avoided = optimize_draft(session)
    assert all(
        pick["player"] != "Alpha Runner"
        for plan in avoided["plans"]
        for pick in plan["selections"]
    )
    alpha = next(
        player
        for player in session.public_state()["players"]
        if player["Player"] == "Alpha Runner"
    )
    assert alpha["Excluded"] is True
    session.set_player_excluded("Alpha Runner", False)
    assert session.public_state()["excluded_players"] == []

    session.add_pick("Alpha Runner", mine=True)
    rerun = optimize_draft(session)
    assert rerun["plans"][0]["selections"][0]["pick"] == 8
    assert "RB" not in rerun["slots_remaining"]


def test_optimizer_uses_matched_ffc_distribution(tmp_path, monkeypatch):
    make_league(tmp_path)
    model = ADPModel(
        enabled=True,
        available=True,
        status="live",
        scoring_format="ppr",
        teams=4,
        total_drafts=1000,
        players=(
            PlayerDraftDistribution(
                name="Alpha Runner Jr.",
                mean=3.0,
                stdev=1.5,
                samples=500,
            ),
        ),
    )
    monkeypatch.setattr(
        "fantasyfootball.draft_state.load_adp_model",
        lambda config, year, path: model,
    )

    session = DraftSession(tmp_path, 2026, "test", 1)
    optimized = optimize_draft(session)
    alpha = next(
        pick
        for plan in optimized["plans"]
        for pick in plan["selections"]
        if pick["player"] == "Alpha Runner"
    )

    assert alpha["probability_source"] == "ffc"
    assert alpha["probability_mean"] == 3.0
    assert alpha["probability_stdev"] == 1.5
    assert alpha["probability_samples"] == 500
    assert session.public_state()["adp_model"]["matched_players"] == 1


def test_snake_slot_and_manual_undo(tmp_path):
    make_league(tmp_path)
    session = DraftSession(tmp_path, 2026, "test", 2, rounds=3)
    assert session.is_my_pick(2)
    assert session.is_my_pick(7)
    assert not session.is_my_pick(6)
    session.add_pick("Alpha Runner")
    session.add_pick("Bravo Receiver Jr.")
    assert session.picks[-1]["mine"] is True
    session.undo_pick(1)
    assert session.current_pick == 1
    session.reset_practice()
    assert session.picks == []

    live = DraftSession(tmp_path, 2026, "test", 2, rounds=3, mode="live")
    with pytest.raises(DraftError, match="only available in practice"):
        live.reset_practice()


def test_spreadsheet_deletions_and_restorations_round_trip(tmp_path):
    league = make_league(tmp_path)
    session = DraftSession(tmp_path, 2026, "test", 1, mode="practice")
    workbook = pd.read_csv(league / "practice_live_draft.csv", index_col=0)
    workbook = workbook[workbook["Player"] != "Bravo Receiver Jr."]
    workbook.to_csv(league / "practice_live_draft.csv")

    result = session.refresh_from_spreadsheet()
    assert [pick["player"] for pick in result["added"]] == [
        "Bravo Receiver Jr."
    ]

    restored = pd.read_csv(league / "practice_live_draft.csv", index_col=0)
    original = pd.read_csv(league / "clean.csv", index_col=0)
    restored = pd.concat(
        [restored, original[original["Player"] == "Bravo Receiver Jr."]]
    ).sort_index()
    restored.to_csv(league / "practice_live_draft.csv")
    result = session.refresh_from_spreadsheet()
    assert [pick["player"] for pick in result["removed"]] == [
        "Bravo Receiver Jr."
    ]
    assert session.picks == []


def test_platform_merge_preserves_locked_manual_override(tmp_path):
    make_league(tmp_path)
    session = DraftSession(tmp_path, 2026, "test", 1)
    session.add_pick("Alpha Runner", number=1, locked=True)

    result = session.reconcile_platform_picks(
        [
            {"number": 1, "player": "Charlie Passer", "source": "Sleeper API"},
            {"number": 2, "player": "Bravo Receiver", "source": "Sleeper API"},
        ]
    )

    assert result["conflicts"] == [
        {
            "number": 1,
            "local": "Alpha Runner",
            "platform": "Charlie Passer",
            "locked": True,
        }
    ]
    assert result["added"][0]["player"] == "Bravo Receiver Jr."
    with pytest.raises(DraftError, match="already recorded"):
        session.add_pick("Bravo Receiver Jr.")


def test_unranked_api_player_still_advances_the_draft(tmp_path):
    make_league(tmp_path)
    session = DraftSession(tmp_path, 2026, "test", 1)

    result = session.reconcile_platform_picks(
        [
            {
                "number": 1,
                "player": "Example Kicker",
                "source": "ESPN API",
                "external_id": "999",
            }
        ]
    )

    assert result["unmatched"][0]["player"] == "Example Kicker"
    assert result["unmatched"][0]["matched"] is False
    assert session.current_pick == 2
    assert len(session.public_state()["players"]) == len(PLAYERS)
    repeated = session.reconcile_platform_picks(
        [
            {
                "number": 1,
                "player": "Example Kicker",
                "source": "ESPN API",
                "external_id": "999",
            }
        ]
    )
    assert repeated == {"added": [], "conflicts": [], "unmatched": []}


def test_parse_sleeper_and_espn_payloads():
    sleeper = parse_sleeper_picks(
        [
            {
                "pick_no": 3,
                "player_id": "p1",
                "picked_by": "me",
                "draft_slot": 3,
                "metadata": {
                    "first_name": "Alpha",
                    "last_name": "Runner",
                    "position": "RB",
                    "team": "AAA",
                },
            }
        ],
        {"user_id": "me"},
    )
    assert sleeper[0] == {
        "number": 3,
        "player": "Alpha Runner",
        "position": "RB",
        "team": 3,
        "mine": True,
        "source": "Sleeper API",
        "external_id": "p1",
        "player_team": "AAA",
    }

    defense = parse_sleeper_picks(
        [
            {
                "pick_no": 4,
                "player_id": "DEN",
                "picked_by": "other",
                "draft_slot": 4,
                "metadata": {
                    "full_name": "Denver Broncos",
                    "position": "DEF",
                },
            }
        ],
        {"user_id": "me"},
    )
    assert defense[0]["player"] == "Denver Broncos"
    assert defense[0]["position"] == "DST"

    espn = parse_espn_picks(
        {
            "draftDetail": {
                "picks": [
                    {
                        "overallPickNumber": 1,
                        "roundId": 1,
                        "roundPickNumber": 1,
                        "playerId": -1,
                        "teamId": 1,
                    },
                    {
                        "roundId": 2,
                        "roundPickNumber": 2,
                        "playerId": 99,
                        "teamId": 2,
                    },
                ]
            }
        },
        {99: "Charlie Passer"},
        {"teams": 4, "team_id": 2},
    )
    assert espn[0]["number"] == 6
    assert len(espn) == 1
    assert espn[0]["mine"] is True
    assert espn[0]["player"] == "Charlie Passer"


def test_espn_player_directory_maps_default_positions_and_defense():
    class Requester:
        @staticmethod
        def get_pro_players():
            return [
                {
                    "id": 10,
                    "fullName": "Example Receiver",
                    "defaultPositionId": 3,
                },
                {
                    "id": -20,
                    "fullName": "Example Defense",
                    "defaultPositionId": 16,
                },
            ]

    class League:
        espn_request = Requester()

    _ESPN_PLAYER_MAPS.pop(2037, None)
    player_map = _espn_player_map(League(), 2037)

    assert player_map == {
        10: {"name": "Example Receiver", "position": "WR"},
        -20: {"name": "Example Defense", "position": "DST"},
    }


def test_platform_request_error_does_not_echo_private_url(monkeypatch):
    private_url = "https://example.invalid/draft/private-draft-identifier"

    def fail_request(*args, **kwargs):
        raise HTTPError(private_url, 403, "Forbidden", {}, None)

    monkeypatch.setattr(draft_sources, "urlopen", fail_request)

    with pytest.raises(DraftError) as captured:
        draft_sources._request_json(private_url)

    assert str(captured.value) == "Draft API request failed."
    assert "private-draft-identifier" not in str(captured.value)


def test_missing_platform_name_does_not_leak_external_id(tmp_path):
    make_league(tmp_path)
    session = DraftSession(tmp_path, 2026, "test", 1)

    result = session.reconcile_platform_picks(
        [
            {
                "number": 1,
                "player": "",
                "position": "K",
                "source": "ESPN API",
                "external_id": "private-player-identifier",
            }
        ]
    )

    assert result["unmatched"][0]["player"] == "Unknown player"
    browser_json = json.dumps(session.public_state())
    assert "private-player-identifier" not in browser_json


def test_verified_opponent_reorder_is_atomic_and_preserves_roster(tmp_path):
    make_league(tmp_path)
    s = DraftSession(tmp_path, 2026, "test", 4)
    for name in [
        "Alpha Runner",
        "Bravo Receiver Jr.",
        "Charlie Passer",
        "Delta Tight End",
    ]:
        s.add_pick(name, locked=True)
    before = s.public_state()
    corrections = [
        {
            "number": 1,
            "expected_player": "Alpha Runner",
            "player": "Bravo Receiver Jr.",
        },
        {
            "number": 2,
            "expected_player": "Bravo Receiver Jr.",
            "player": "Alpha Runner",
        },
    ]
    # Reject stale batches before changing any entries.
    bad = [corrections[0], {**corrections[1], "expected_player": "Wrong"}]
    with pytest.raises(DraftError, match="changed since"):
        s.reorder_opponent_picks(bad)
    assert s.public_state()["picks"] == before["picks"]
    s.reorder_opponent_picks(corrections)
    after = s.public_state()
    assert after["current_pick"] == before["current_pick"]
    assert after["roster"] == before["roster"]
    assert {p["Player"] for p in after["players"]} == {
        p["Player"] for p in before["players"]
    }
    assert [p["player"] for p in s.picks[:2]] == [
        "Bravo Receiver Jr.",
        "Alpha Runner",
    ]
    restored = DraftSession(tmp_path, 2026, "test", 4)
    assert restored.picks == s.picks
    # A stale retry cannot swap the names back.
    with pytest.raises(DraftError, match="changed since"):
        s.reorder_opponent_picks(corrections)
    with pytest.raises(DraftError, match="your picks"):
        s.reorder_opponent_picks(
            [
                {
                    "number": 4,
                    "expected_player": "Delta Tight End",
                    "player": "Charlie Passer",
                },
                {
                    "number": 3,
                    "expected_player": "Charlie Passer",
                    "player": "Delta Tight End",
                },
            ]
        )
    with pytest.raises(DraftError, match="preserve the drafted"):
        s.reorder_opponent_picks(
            [
                {
                    "number": 1,
                    "expected_player": "Bravo Receiver Jr.",
                    "player": "Echo Runner",
                },
                {
                    "number": 2,
                    "expected_player": "Alpha Runner",
                    "player": "Bravo Receiver Jr.",
                },
            ]
        )
