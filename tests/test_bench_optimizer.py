from types import SimpleNamespace

import pandas as pd
import pytest

from fantasyfootball.adp_model import PlayerDraftDistribution
from fantasyfootball.draft_analysis import (
    _availability_estimate,
    optimize_draft,
)


def session_for_bench(*, positions=None, rounds=8, current=1):
    rows = [
        ("Urgent RB", "RB", 1, 25, 100),
        ("Later WR", "WR", 25, 10, 110),
        ("Backup QB", "QB", 30, 100, 500),
        ("Late QB", "QB", 50, 95, 495),
        ("Starting TE", "TE", 10, 30, 150),
    ]
    rows += [(f"Depth {i}", "WR", i + 2, 1, 20 - i / 10) for i in range(50)]
    frame = pd.DataFrame(
        rows, columns=["Player", "Position", "ADP", "VOR_Points", "Ceiling"]
    )
    frame["Team"] = "TST"
    frame["Bye"] = 8
    distributions = {
        r.Player: PlayerDraftDistribution(r.Player, r.ADP, 1.5, 500)
        for r in frame.itertuples()
    }
    return SimpleNamespace(
        full_df=frame,
        picks=[],
        excluded_players=[],
        current_pick=current,
        teams=2,
        rounds=rounds,
        metric="VOR_Points",
        flex_positions=("RB", "WR"),
        config={
            "positions": positions or {},
            "late_round_positions": ["DST", "K"],
        },
        adp_distribution=lambda name: distributions.get(name),
        is_my_pick=lambda n: (
            ((n - 1) // 2 % 2 == 0 and n % 2 == 1)
            or ((n - 1) // 2 % 2 == 1 and n % 2 == 0)
        ),
    )


def test_bench_forecast_is_four_picks_ceiling_only_and_takes_urgent_player():
    result = optimize_draft(session_for_bench())
    assert result["phase"] == "bench"
    picks = result["plans"][0]["selections"]
    assert [p["pick"] for p in picks] == [1, 4, 5, 8]
    assert picks[0]["player"] == "Urgent RB"
    assert picks[1]["player"] == "Later WR"
    assert picks[0]["probability"] == 1
    assert picks[0]["wait_probability"] < 0.2
    assert all(
        p["slot"] == "BENCH" and p["position"] in {"RB", "WR"} for p in picks
    )
    assert all(p["value_metric"] == "Ceiling" for p in picks)


def test_transition_keeps_starters_and_reserves_final_two_rounds():
    s = session_for_bench(positions={"QB": 1, "TE": 1}, rounds=6)
    s.metric = "Ceiling"  # Transition starter metric is intentionally fixed.
    result = optimize_draft(s)
    assert result["phase"] == "transition"
    for plan in result["plans"]:
        picks = plan["selections"]
        assert len(picks) == 4
        assert {p["slot"] for p in picks} >= {"QB", "TE", "BENCH"}
        assert all(
            p["value_metric"]
            == ("Ceiling" if p["slot"] == "BENCH" else "VOR_Points")
            for p in picks
        )
        assert max(p["pick"] for p in picks) <= 8


def test_late_rounds_and_draft_completion_do_not_recommend_more_bench():
    s = session_for_bench(rounds=6, current=9)
    result = optimize_draft(s)
    assert result["phase"] == "finish" and not result["plans"]
    assert "D/ST and K" in result["message"]
    s.current_pick = 13
    assert optimize_draft(s)["message"] == "Your draft is complete."


def test_excluded_and_drafted_players_never_return():
    s = session_for_bench()
    s.excluded_players = ["Urgent RB"]
    s.picks = [{"player": "Later WR", "mine": True, "matched": True}]
    result = optimize_draft(s)
    assert result["plans"]
    assert all(
        p["player"] not in {"Urgent RB", "Later WR"}
        for plan in result["plans"]
        for p in plan["selections"]
    )


def test_availability_is_conditioned_on_survival_to_current_pick():
    s = session_for_bench(current=3)
    p = s.full_df.iloc[0].to_dict()
    assert _availability_estimate(s, p, 3)["probability"] == 1
    now_conditioned = _availability_estimate(s, p, 4)["probability"]
    s.current_pick = 1
    assert now_conditioned > _availability_estimate(s, p, 4)["probability"]


def test_early_rounds_never_search_bench(monkeypatch):
    s = session_for_bench(positions={"QB": 1, "RB": 1, "WR": 1, "TE": 1})

    def no_bench(*args):
        pytest.fail("Bench search ran before the two-starter threshold")

    monkeypatch.setattr(
        "fantasyfootball.draft_analysis._late_draft_plans", no_bench
    )
    result = optimize_draft(s)
    assert all(
        p["slot"] != "BENCH"
        for plan in result["plans"]
        for p in plan["selections"]
    )


@pytest.mark.parametrize("include_qb", [True, False])
def test_flex_uses_shared_replacement_value_without_te_scarcity_bonus(
    include_qb,
):
    positions = {"TE": 1, "FLEX": 1}
    if include_qb:
        positions["QB"] = 1
    s = session_for_bench(positions=positions, rounds=6)
    s.flex_positions = ("RB", "WR", "TE")
    s.full_df["FLEX_VOR_Points"] = s.full_df["VOR_Points"]
    te = s.full_df["Position"] == "TE"
    s.full_df.loc[te, "VOR_Points"] = 200
    s.full_df.loc[te, "FLEX_VOR_Points"] = -50
    result = optimize_draft(s)
    assert result["plans"]
    for plan in result["plans"]:
        flex = next(p for p in plan["selections"] if p["slot"] == "FLEX")
        assert flex["position"] in {"RB", "WR"}
        assert flex["value_metric"] == "FLEX_VOR_Points"
        player = s.full_df.set_index("Player").loc[flex["player"]]
        assert flex["value"] == player["FLEX_VOR_Points"]


def scarcity_session(rows, positions, *, rounds=4):
    s = session_for_bench(positions=positions, rounds=rounds)
    # Other teams consume low-value players between our picks.
    rows = rows + [
        (f"Other {i}", "QB", i + 2.1, -100, -100) for i in range(12)
    ]
    s.full_df = pd.DataFrame(
        rows,
        columns=["Player", "Position", "ADP", "VOR_Points", "FLEX_VOR_Points"],
    )
    s.full_df["Ceiling"] = s.full_df["FLEX_VOR_Points"] + 150
    s.flex_positions = ("RB", "WR", "TE")
    distributions = {
        r.Player: PlayerDraftDistribution(r.Player, r.ADP, 0.1, 500)
        for r in s.full_df.itertuples()
    }
    s.adp_distribution = distributions.get
    return s


@pytest.mark.parametrize("position", ["RB", "WR", "TE"])
def test_scarce_position_is_filled_before_deep_flex_pool(position):
    other = "WR" if position != "WR" else "RB"
    s = scarcity_session(
        [
            ("Scarce starter", position, 2, 100, 100),
            ("Position replacement", position, 60, -50, -50),
            ("Strong flex", other, 50, 90, 90),
        ],
        {position: 1, "FLEX": 1},
    )
    picks = optimize_draft(s)["plans"][0]["selections"]
    assert [(p["slot"], p["player"]) for p in picks] == [
        (position, "Scarce starter"),
        ("FLEX", "Strong flex"),
    ]


@pytest.mark.parametrize("early", [False, True])
def test_urgent_flex_can_go_first_without_consuming_open_te(early):
    positions = {"TE": 1, "FLEX": 1}
    if early:
        positions["QB"] = 1
    s = scarcity_session(
        [
            ("Patient TE", "TE", 50, 100, 100),
            ("Weak TE", "TE", 60, -50, -50),
            ("Urgent WR", "WR", 2, 90, 90),
            ("Patient QB", "QB", 70, 50, -50),
        ],
        positions,
        rounds=5 if early else 4,
    )
    picks = optimize_draft(s)["plans"][0]["selections"]
    assert picks[0]["player"] == "Urgent WR"
    assert picks[0]["slot"] == "FLEX"
    assert any(
        p["slot"] == "TE" and p["player"] == "Patient TE" for p in picks
    )


def test_two_tight_ends_win_when_second_is_best_flex_value():
    s = scarcity_session(
        [
            ("First TE", "TE", 2, 120, 100),
            ("Second TE", "TE", 50, 110, 90),
            ("Lower WR", "WR", 60, 40, 40),
            ("Lower RB", "RB", 70, 30, 30),
        ],
        {"TE": 1, "FLEX": 1},
    )
    picks = optimize_draft(s)["plans"][0]["selections"]
    assert [(p["slot"], p["player"]) for p in picks] == [
        ("TE", "First TE"),
        ("FLEX", "Second TE"),
    ]
