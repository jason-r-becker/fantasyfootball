# %%
import argparse
import json

import pandas as pd
from tqdm import tqdm

from fantasyfootball.utils import Config
from fantasyfootball.set_lineup import LineupOptimizer


# %%
def find_weekly_wavier_value(league, year, top_n_players, skip_top_n_players):
    top_n_df_list = []
    weeks = list(range(1, 15))

    for week in tqdm(weeks):
        config = Config(league, year=year, week=week)
        positions = config.get("positions").keys()
        lo = LineupOptimizer(config)
        week_df = lo.df[lo.df["FantasyTeam"] == "FA"]
        for pos in positions:
            if pos == "FLEX":
                pos_df = week_df[week_df["position"].isin(["RB", "WR", "TE"])]
            else:
                pos_df = week_df[week_df["position"] == pos]

            week_pos_df = (
                pos_df.sort_values("projection", ascending=False)
                .head(top_n_players)
                .reset_index()
            )
            week_pos_df["week"] = week
            week_pos_df["fantasy_position"] = pos
            week_pos_df["waiver_rank"] = week_pos_df.index + 1
            top_n_df_list.append(week_pos_df.reset_index())

    top_n_df = pd.concat(top_n_df_list)
    skip_top_n_players = 2
    waiver_value = {}
    for pos in positions:
        weekly_waiver_value = (
            top_n_df[
                (top_n_df["fantasy_position"] == pos)
                & (top_n_df["waiver_rank"] > skip_top_n_players)
            ]
            .groupby("week")["projection"]
            .mean()
        )
        waiver_value[pos] = weekly_waiver_value.mean().round(1)

    return waiver_value


def add_waiver_value_to_config(league, year, waiver_value_d):
    config = Config(league, year=year, week=1)
    d = config._config_dict
    d["waiver_weekly_position_value"] = waiver_value_d
    with open(config.path / "config.json", "w") as file:
        json.dump(d, file, indent=4, sort_keys=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--league", required=True)
    parser.add_argument("--year", type=int, required=True)
    parser.add_argument("--top", type=int, default=5)
    parser.add_argument("--skip", type=int, default=2)
    args = parser.parse_args()
    waiver_value = find_weekly_wavier_value(
        league=args.league,
        year=args.year,
        top_n_players=args.top,
        skip_top_n_players=args.skip,
    )
    add_waiver_value_to_config(args.league, args.year, waiver_value)


if __name__ == "__main__":
    main()
