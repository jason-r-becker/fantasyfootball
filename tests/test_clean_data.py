import json

import pandas as pd
import pytest

from fantasyfootball.adp_model import ADPModel, PlayerDraftDistribution
from fantasyfootball.clean_data import (
    OUTPUT_COLUMNS,
    clean_league,
    merge_ffc_adp,
    prepare_projection_rankings,
)


def model(*players):
    return ADPModel(
        enabled=True,
        available=True,
        status="live",
        scoring_format="ppr",
        teams=12,
        players=tuple(players),
    )


def raw_projection_frame():
    return pd.DataFrame(
        [
            {
                "player": f"{position} Player {rank}",
                "team": "AAA",
                "position": position,
                "points": 250 - rank * 5,
                "floor": 220 - rank * 5,
                "ceiling": 280 - rank * 5,
                "sd_pts": 8,
            }
            for position in ("QB", "RB", "WR", "TE")
            for rank in range(1, 9)
        ]
    )


def make_cleaning_league(tmp_path, config=None):
    league = tmp_path / "data" / "2026" / "test"
    league.mkdir(parents=True)
    if config is None:
        config = {
            "teams": 2,
            "positions": {
                "QB": 1,
                "RB": 1,
                "WR": 1,
                "TE": 1,
                "FLEX": 1,
            },
            "flex_positions": ["RB", "WR", "TE"],
            "draft_rounds": 5,
            "site": "Sleeper",
            "adp_model": {"source": "ffc", "format": "ppr"},
        }
    (league / "config.json").write_text(json.dumps(config), encoding="utf-8")
    raw_projection_frame().to_csv(league / "raw.csv", index=False)
    return league


def test_ffc_populates_adp_bye_and_distribution_provenance():
    rankings = pd.DataFrame(
        [
            {"Player": "Alpha Runner", "Team": "JAX", "Position": "RB"},
            {"Player": "Bravo Receiver Jr.", "Team": "BBB", "Position": "WR"},
        ]
    )
    source = model(
        PlayerDraftDistribution(
            name="Alpha Runner",
            mean=12.5,
            stdev=3.2,
            samples=900,
            earliest=3,
            latest=29,
            team="JAC",
            position="RB",
            bye=8,
        ),
        PlayerDraftDistribution(
            name="B. Receiver",
            mean=20.0,
            stdev=4.0,
            samples=700,
            team="BBB",
            position="WR",
            bye=9,
        ),
    )

    merged, report, confirmed = merge_ffc_adp(
        rankings,
        source,
        {"B. Receiver": "Bravo Receiver Jr."},
    )

    alpha = merged.loc[merged["Player"] == "Alpha Runner"].iloc[0]
    bravo = merged.loc[merged["Player"] == "Bravo Receiver Jr."].iloc[0]
    assert alpha["ADP"] == 12.5
    assert alpha["Bye"] == 8
    assert alpha["ADP Source"] == "FFC"
    assert alpha["ADP Std Dev"] == 3.2
    assert alpha["ADP Samples"] == 900
    assert alpha["ADP Earliest"] == 3
    assert alpha["ADP Latest"] == 29
    assert bravo["ADP"] == 20.0
    assert report["matched_players"] == 2
    assert report["alias_count"] == 1
    assert confirmed == {}


def test_fuzzy_suggestion_requires_confirmation_and_respects_position_team():
    rankings = pd.DataFrame(
        [
            {"Player": "Alpha Runner", "Team": "AAA", "Position": "RB"},
            {"Player": "Alpha Receiver", "Team": "AAA", "Position": "WR"},
        ]
    )
    source = model(
        PlayerDraftDistribution(
            name="Alfa Runner",
            mean=30.0,
            stdev=5.0,
            samples=100,
            team="AAA",
            position="RB",
        )
    )

    untouched, report, _ = merge_ffc_adp(rankings, source)
    assert untouched["ADP"].isna().all()
    assert report["unmatched"] == ["Alfa Runner"]

    decisions = []

    def confirm(source_name, suggestion, adp):
        decisions.append((source_name, suggestion, adp))
        return True

    merged, report, confirmed = merge_ffc_adp(
        rankings, source, confirmer=confirm
    )
    assert decisions == [("Alfa Runner", "Alpha Runner", 30.0)]
    assert merged.loc[merged["Player"] == "Alpha Runner", "ADP"].iloc[0] == 30
    assert confirmed == {"Alfa Runner": "Alpha Runner"}
    assert report["unmatched"] == []


def test_exact_name_with_conflicting_position_is_reported_and_not_merged():
    rankings = pd.DataFrame(
        [{"Player": "Example Player", "Team": "AAA", "Position": "WR"}]
    )
    source = model(
        PlayerDraftDistribution(
            name="Example Player",
            mean=40.0,
            stdev=5.0,
            samples=100,
            team="AAA",
            position="RB",
        )
    )

    merged, report, _ = merge_ffc_adp(rankings, source)

    assert merged["ADP"].isna().all()
    assert report["matched_players"] == 0
    assert report["conflicts"][0]["source"] == "Example Player"


def test_projection_preparation_accepts_platform_neutral_config():
    raw = pd.DataFrame(
        [
            {
                "player": f"Player {index}",
                "team": "AAA",
                "position": position,
                "points": 200 - index,
                "floor": 180 - index,
                "ceiling": 220 - index,
                "sd_pts": 10,
            }
            for index, position in enumerate(["QB", "RB", "WR", "TE"] * 8)
        ]
    )
    config = {
        "teams": 2,
        "positions": {"QB": 1, "RB": 1, "WR": 1, "TE": 1, "FLEX": 1},
        "flex_positions": ["RB", "WR", "TE"],
    }

    rankings = prepare_projection_rankings(raw, config)

    assert len(rankings) == len(raw)
    assert rankings["VOR_Points"].notna().all()
    assert (
        rankings.loc[
            rankings["Position"].isin(config["flex_positions"]),
            "FLEX_VOR_Points",
        ]
        .notna()
        .all()
    )


def test_flex_replacement_uses_only_declared_eligible_positions():
    raw = pd.DataFrame(
        [
            {
                "player": f"{position} Player {rank}",
                "team": "AAA",
                "position": position,
                "points": points,
                "floor": points - 10,
                "ceiling": points + 10,
                "sd_pts": 5,
            }
            for position, values in {
                "QB": (300, 290, 280),
                "RB": (200, 180, 160, 140),
                "WR": (190, 170, 150, 130),
                "TE": (210, 120, 110),
            }.items()
            for rank, points in enumerate(values, 1)
        ]
    )
    config = {
        "teams": 1,
        "positions": {"QB": 1, "RB": 1, "WR": 1, "TE": 1, "FLEX": 1},
        "flex_positions": ["RB", "WR"],
    }

    rankings = prepare_projection_rankings(raw, config)

    eligible = rankings[rankings["Position"].isin({"RB", "WR"})]
    # Two base starters + one FLEX + one replacement tier means the fifth
    # eligible player (160 points) is the replacement baseline.
    assert set(eligible["FLEX_VOR_Points"]) == {
        40.0,
        30.0,
        20.0,
        10.0,
        0.0,
        -10.0,
        -20.0,
        -30.0,
    }
    assert (
        rankings.loc[rankings["Position"] == "TE", "FLEX_VOR_Points"]
        .isna()
        .all()
    )


def test_projection_preparation_excludes_kickers_and_defenses():
    raw = pd.DataFrame(
        [
            {
                "player": f"{position} Player {rank}",
                "team": "AAA",
                "position": position,
                "points": 200 - rank,
                "floor": 180 - rank,
                "ceiling": 220 - rank,
                "sd_pts": 5,
            }
            for position in ("QB", "RB", "WR", "TE", "K", "DST")
            for rank in range(1, 5)
        ]
    )
    config = {
        "teams": 2,
        "positions": {"QB": 1, "RB": 1, "WR": 1, "TE": 1, "FLEX": 1},
        "flex_positions": ["RB", "WR", "TE"],
    }

    rankings = prepare_projection_rankings(raw, config)

    assert set(rankings["Position"]) == {"QB", "RB", "WR", "TE"}


def test_clean_league_end_to_end_writes_matching_outputs_and_alias(
    tmp_path, monkeypatch
):
    league = make_cleaning_league(tmp_path)
    alias_path = league.parent / "source_player_map.json"
    alias_path.write_text(json.dumps({"DEN": "Denver Broncos"}))
    source = model(
        PlayerDraftDistribution(
            name="Arby Player 1",
            mean=9.5,
            stdev=2.5,
            samples=200,
            earliest=4,
            latest=18,
            team="AAA",
            position="RB",
            bye=7,
        )
    )
    monkeypatch.setattr("fantasyfootball.clean_data.root", lambda: tmp_path)
    monkeypatch.setattr(
        "fantasyfootball.clean_data.load_adp_model",
        lambda config, year, path: source,
    )
    monkeypatch.setattr("builtins.input", lambda prompt: "y")

    report = clean_league(2026, "test")

    clean = pd.read_csv(league / "clean.csv", index_col=0)
    live = pd.read_csv(league / "live_draft.csv", index_col=0)
    pd.testing.assert_frame_equal(clean, live)
    assert list(clean.columns) == list(OUTPUT_COLUMNS)
    matched = clean.loc[clean["Player"] == "RB Player 1"].iloc[0]
    assert matched["ADP"] == 9.5
    assert matched["Bye"] == 7
    assert matched["ADP Source"] == "FFC"
    assert matched["ADP Samples"] == 200
    assert report["matched_players"] == 1
    aliases = json.loads(alias_path.read_text(encoding="utf-8"))
    assert aliases == {
        "Arby Player 1": "RB Player 1",
        "DEN": "Denver Broncos",
    }


def test_clean_league_rejected_suggestion_does_not_persist_alias(
    tmp_path, monkeypatch
):
    league = make_cleaning_league(tmp_path)
    source = model(
        PlayerDraftDistribution(
            name="Arby Player 1",
            mean=9.5,
            stdev=2.5,
            samples=200,
            team="AAA",
            position="RB",
        )
    )
    monkeypatch.setattr("fantasyfootball.clean_data.root", lambda: tmp_path)
    monkeypatch.setattr(
        "fantasyfootball.clean_data.load_adp_model",
        lambda config, year, path: source,
    )
    monkeypatch.setattr("builtins.input", lambda prompt: "n")

    report = clean_league(2026, "test")

    assert report["matched_players"] == 0
    assert not (league.parent / "source_player_map.json").exists()


def test_clean_league_disabled_mode_never_needs_adp_input(
    tmp_path, monkeypatch
):
    config = {
        "teams": 2,
        "positions": {"QB": 1, "RB": 1, "WR": 1, "TE": 1, "FLEX": 1},
        "flex_positions": ["RB", "WR"],
        "adp_model": {"source": "disabled"},
    }
    league = make_cleaning_league(tmp_path, config)
    monkeypatch.setattr("fantasyfootball.clean_data.root", lambda: tmp_path)

    report = clean_league(2026, "test")

    clean = pd.read_csv(league / "clean.csv", index_col=0)
    assert report["source_players"] == 0
    assert clean["ADP"].isna().all()
    assert clean["ADP Source"].isna().all()


def test_clean_league_offline_without_cache_stops_before_writing(
    tmp_path, monkeypatch
):
    config = {
        "teams": 2,
        "positions": {"QB": 1, "RB": 1, "WR": 1, "TE": 1, "FLEX": 1},
        "flex_positions": ["RB", "WR"],
        "adp_model": {"source": "ffc", "format": "ppr", "offline": True},
    }
    league = make_cleaning_league(tmp_path, config)
    monkeypatch.setattr("fantasyfootball.clean_data.root", lambda: tmp_path)

    with pytest.raises(RuntimeError, match="no usable cache"):
        clean_league(2026, "test")

    assert not (league / "clean.csv").exists()
    assert not (league / "live_draft.csv").exists()


def test_clean_league_rejects_invalid_config_and_raw_schema(
    tmp_path, monkeypatch
):
    league = make_cleaning_league(tmp_path)
    monkeypatch.setattr("fantasyfootball.clean_data.root", lambda: tmp_path)
    (league / "config.json").write_text("[]", encoding="utf-8")

    with pytest.raises(ValueError, match="JSON object"):
        clean_league(2026, "test")

    (league / "config.json").write_text(
        json.dumps(
            {
                "teams": 2,
                "positions": {
                    "QB": 1,
                    "RB": 1,
                    "WR": 1,
                    "TE": 1,
                    "FLEX": 1,
                },
                "adp_model": {"source": "disabled"},
            }
        ),
        encoding="utf-8",
    )
    raw = raw_projection_frame().drop(columns="sd_pts")
    raw.to_csv(league / "raw.csv", index=False)

    with pytest.raises(ValueError, match="sd_pts"):
        clean_league(2026, "test")
