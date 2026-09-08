import pandas as pd

from fantasyfootball.set_lineup import LineupOptimizer


def test_weekly_reader_preserves_names_and_prefers_complete_rows(tmp_path):
    path = tmp_path / "projections_2026_wk1_d0.csv"
    pd.DataFrame(
        [
            ["Player A", "WR", None, 10, 8, 12, 2],
            ["Player A", "WR", None, 30, 8, 32, None],
            ["Player B", "RB", "BUF", 5, 3, 7, 1],
            ["Player B", "RB", "BUF", 9, 7, 11, 1],
            [None, "TE", "BUF", 20, 18, 22, 1],
        ],
        columns=[
            "player",
            "position",
            "team",
            "points",
            "floor",
            "ceiling",
            "sd_pts",
        ],
    ).to_csv(path, index=False)
    reader = LineupOptimizer.__new__(LineupOptimizer)

    result = reader._read_projections_fid(path)

    assert result["player"].tolist() == ["Player A", "Player B"]
    assert result["team"].tolist() == ["FA", "BUF"]
    assert result["points"].tolist() == [10, 9]
    assert result["projection"].tolist() == [10, 9]
