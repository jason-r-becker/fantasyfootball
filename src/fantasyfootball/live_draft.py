# %%
"""
Script for use during the draft, which attempts to estimate
which players you will be able to draft, while showing
value of each position. The `live_draft.csv` needs to be updated
during the draft by manually deleting drafted players.

excel/LibreOffice shortcuts for `live_draft.csv` file:
[shift] + [space] : select current row
[ctrl] + [-] : delete curent row

"""

# %%
import argparse
import itertools as it
import json
from collections import defaultdict
from functools import cached_property

import joblib
import numpy as np
import pandas as pd
import matplotlib as mpl
import matplotlib.pyplot as plt
import seaborn as sns
from tqdm import tqdm
from fantasyfootball.utils import root


# %%
class LiveDraft:
    def __init__(
        self,
        year,
        league,
        pick_number,
        n_rounds=15,
        DST_K_taken=0,
        var="VOR_Floor",
        y_axis_min=0,
        picks_made=None,
        ignored_players=None,
        ignored_positions=None,
        drafted_players=None,
    ):
        self._setup_display()
        self.year = year
        self.league = league
        self.pick_number = pick_number
        self.n_rounds = n_rounds
        self.path = root() / f"data/{self.year}/{self.league}"
        self.n_teams, self.n_pos = self._load_config()
        self.DST_K_taken = DST_K_taken
        self.var = var
        self.y_axis_min = y_axis_min
        self.picks_made = (
            {"QB": 0, "RB": 0, "WR": 0, "TE": 0, "FLEX": 0}
            if picks_made is None
            else picks_made
        )
        self.ignored_players = (
            set() if ignored_players is None else ignored_players
        )
        self.ignored_positions = (
            set() if ignored_positions is None else ignored_positions
        )
        self.drafted_players = (
            [] if drafted_players is None else drafted_players
        )
        self.positions = ["QB", "RB", "WR", "TE"]
        self._setup_picks()

    def _setup_display(self):
        mpl.use("Qt5Agg")
        plt.style.use("fivethirtyeight")
        pd.set_option("display.max_columns", None)
        pd.set_option("display.expand_frame_repr", False)
        pd.plotting.register_matplotlib_converters()
        background = "white"
        mpl.rcParams["axes.facecolor"] = background
        mpl.rcParams["axes.edgecolor"] = background
        mpl.rcParams["figure.facecolor"] = background
        mpl.rcParams["figure.edgecolor"] = background
        mpl.rcParams["savefig.facecolor"] = background
        mpl.rcParams["savefig.edgecolor"] = background

    def _load_config(self):
        try:
            with open(self.path / "config.json", "r") as file:
                config = json.load(file)
        except FileNotFoundError:
            raise FileNotFoundError(
                "No `config.json` file found, please run `clean_data.py`"
            )
        return config["teams"], config["positions"]

    @cached_property
    def df(self):
        try:
            df = pd.read_csv(
                self.path / "live_draft.csv", index_col=0
            ).sort_values("ADP")
        except FileNotFoundError:
            raise FileNotFoundError(
                "No 'live_draft.csv' file found, please run `clean_data.py`"
            )
        df["Rank"] = df.index + 1
        df.reset_index(inplace=True, drop=True)
        return df

    @cached_property
    def full_df(self):
        return pd.read_csv(self.path / "clean.csv", index_col=0)

    @property
    def current_pick(self):
        return len(self.full_df) - len(self.df) + 1 + self.DST_K_taken

    def _setup_picks(self):
        picks = [self.pick_number]
        p = self.pick_number
        total_picks = self.n_teams * self.n_rounds
        while True:
            p = picks[-1] + 2 * (self.n_teams - self.pick_number) + 1
            if p > total_picks:
                break
            picks.append(p)
            p = picks[-1] + 2 * (self.pick_number - 1) + 1
            if p > total_picks:
                break
            picks.append(p)
        self.picks = np.array(picks)
        self.remaining_picks = self.picks[self.picks >= self.current_pick]
        self.picks_to_next = self.remaining_picks[0] - self.current_pick
        self.picks_to_next_next = self.remaining_picks[1] - self.current_pick
        self.next_pick_df = self.df.iloc[self.picks_to_next :, :]
        self.scnd_next_pick_df = self.df.iloc[self.picks_to_next_next :, :]
        self.current_round = (self.current_pick - 1) // self.n_teams + 1
        self.pick_of_current_round = (self.current_pick - 1) % self.n_teams + 1

    def print_status(self):
        print(
            f"\n\nCurrent Pick: {self.current_round}:{self.pick_of_current_round} ({self.current_pick})"
        )
        print(f"Picks to Next: {self.picks_to_next}")
        print(f"Picks to 2nd Next: {self.picks_to_next_next}")
        print(f"Remaining Picks: {self.remaining_picks}\n\n")

    def show_table(self):
        table_cols = [
            "Player",
            "Team",
            "Position",
            "Bye",
            self.var,
            f"FLEX_{self.var}",
        ]
        other_cols = [
            "VOR_Floor",
            "VOR_Points",
            "VOR_Ceiling",
            "Std Dev",
            "ADP",
        ]
        for col in other_cols:
            if col == self.var:
                continue
            else:
                table_cols.append(col)
        table_df = self.df[table_cols].sort_values(self.var, ascending=False)
        if self.ignored_positions:
            table_df = table_df[
                ~table_df["Position"].isin(self.ignored_positions)
            ].round(2)
        print(table_df.iloc[:20, :])
        print("\n\n")

    def get_next_pick(self):
        return {
            pos: self.next_pick_df[self.next_pick_df["Position"] == pos]
            .sort_values(self.var, ascending=False)
            .iloc[0, :]
            for pos in self.positions
        }

    def get_next_next_pick(self):
        return {
            pos: self.scnd_next_pick_df[
                self.scnd_next_pick_df["Position"] == pos
            ]
            .sort_values(self.var, ascending=False)
            .iloc[0, :]
            for pos in self.positions
        }

    def run_mock(self):
        odds_of_pick_one_round_earlier = 0.8
        beta = (odds_of_pick_one_round_earlier - 0.5) / (-1 - 0)
        alpha = 0.5

        picks_remaining = self.n_pos.copy()
        positions_to_pick = []
        for pos, n_pick_made_for_pos in self.picks_made.items():
            n_picks_remaining_for_pos = (
                picks_remaining[pos] - n_pick_made_for_pos
            )
            for __ in range(n_picks_remaining_for_pos):
                positions_to_pick.append(pos)

        all_mock_pos_pick_iterations = list(it.permutations(positions_to_pick))
        flex_positions = {"RB", "WR"}

        def test_iter(mock_iter):
            mock_made_picks = self.picks_made.copy()
            for mock_pick_pos in mock_iter:
                if mock_pick_pos in flex_positions:
                    mock_made_picks[mock_pick_pos] += 1
                elif mock_pick_pos == "FLEX":
                    for flex_position in flex_positions:
                        if (
                            mock_made_picks[flex_position]
                            == self.n_pos[flex_position]
                        ):
                            return True
                else:
                    continue

        mock_pos_pick_iterations = []
        for mock_iter in all_mock_pos_pick_iterations:
            if test_iter(mock_iter):
                mock_pos_pick_iterations.append(mock_iter)

        mock_dfs_list = []
        mock_vors = []
        mock_rn_vors = []

        for mock_pos_picks in tqdm(mock_pos_pick_iterations):
            mock_current_pick = self.current_pick + self.picks_to_next
            mock_picks_to_next = self.picks_to_next
            mock_remaining_picks = self.remaining_picks
            mock_current_round = (mock_current_pick - 1) // self.n_teams + 1
            mock_pick_of_current_round = (
                mock_current_pick - 1
            ) % self.n_teams + 1

            mock_df = self.df.iloc[mock_picks_to_next:]
            curr_mock_d = defaultdict(list)
            for mock_pos_pick in mock_pos_picks:
                if mock_pos_pick == "FLEX":
                    pos_var = f"FLEX_{self.var}"
                    mock_pos_df = mock_df[
                        mock_df["Position"].isin(flex_positions)
                    ].sort_values(pos_var, ascending=False)
                else:
                    pos_var = self.var
                    mock_pos_df = mock_df[
                        mock_df["Position"] == mock_pos_pick
                    ].sort_values(pos_var, ascending=False)

                mock_pos_df = mock_pos_df[
                    ~mock_pos_df["Player"].isin(self.ignored_players)
                ]
                mock_pos_df["ADP Diff"] = mock_current_pick - mock_pos_df["ADP"]
                mock_pick = mock_pos_df.iloc[0]
                mock_player = mock_pick["Player"]
                second_choice_df = mock_pos_df[
                    (mock_pos_df["ADP Diff"] < 0)
                    & (mock_pos_df["Player"] != mock_player)
                ]
                if not len(second_choice_df):
                    continue
                else:
                    second_choice = second_choice_df.iloc[0]
                curr_mock_d["Pick"].append(
                    f"{mock_current_round}:{mock_pick_of_current_round}"
                )
                curr_mock_d["Player"].append(mock_player)
                curr_mock_d["Bye"].append(mock_pick["Bye"])
                curr_mock_d[self.var].append(mock_pick[pos_var])
                curr_mock_d["ADP Diff"].append(mock_pick["ADP Diff"])
                curr_mock_d["Backup Player"].append(second_choice["Player"])
                curr_mock_d["Bye "].append(second_choice["Bye"])

                curr_mock_d[f"{self.var} Drop"].append(
                    mock_pick[pos_var] - second_choice[pos_var]
                )

                # Drop drafted player.
                mock_df = mock_df[mock_df["Player"] != mock_player]
                # Drop drafted players to next pick.
                mock_remaining_picks = mock_remaining_picks[1:]
                mock_picks_to_next = mock_remaining_picks[0] - mock_current_pick
                mock_current_pick = mock_remaining_picks[0]

                mock_current_round = (mock_current_pick - 1) // self.n_teams + 1
                mock_pick_of_current_round = (
                    mock_current_pick - 1
                ) % self.n_teams + 1
                mock_df = mock_df.iloc[mock_picks_to_next - 1 :]

            curr_mock_df = pd.DataFrame(curr_mock_d)
            curr_mock_df["Prob of Pick"] = (
                curr_mock_df["ADP Diff"] / self.n_teams * beta + alpha
            ).round(2)
            curr_mock_df.loc[
                curr_mock_df["Prob of Pick"] > 1, "Prob of Pick"
            ] = 1
            curr_mock_df[f"RN {self.var}"] = (
                (curr_mock_df["Prob of Pick"] * curr_mock_df[self.var])
                + (
                    (1 - curr_mock_df["Prob of Pick"])
                    * (
                        curr_mock_df[self.var]
                        - curr_mock_df[f"{self.var} Drop"]
                    )
                )
            ).round(1)
            curr_mock_df.index += 1

            mock_dfs_list.append(curr_mock_df)
            mock_vors.append(curr_mock_df[self.var].sum())
            mock_rn_vors.append(curr_mock_df[f"RN {self.var}"].sum())

        all_mock_iters_df = (
            pd.DataFrame({self.var: mock_vors, f"RN {self.var}": mock_rn_vors})
            .drop_duplicates()
            .sort_values([self.var, f"RN {self.var}"], ascending=[False, False])
        )
        all_mock_iters_df["idx"] = all_mock_iters_df.index
        mock_iters_df = (
            all_mock_iters_df.groupby(self.var)
            .first()
            .reset_index()
            .set_index("idx")
            .rename_axis(None)
            .sort_values([self.var, f"RN {self.var}"], ascending=[False, False])
        )

        self.mock_iters_df = mock_iters_df
        self.mock_dfs_list = mock_dfs_list
        self.mock_df = self.mock_dfs_list[self.mock_iters_df.index[0]]

    def print_mock_results(self, n=4):
        if not hasattr(self, "mock_iters_df") or not hasattr(
            self, "mock_dfs_list"
        ):
            print("No mock draft results found. Please run run_mock() first.")
            return
        for idx in self.mock_iters_df.index[:n]:
            mock_idx_df = self.mock_iters_df.loc[idx]
            var_val = mock_idx_df[self.var]
            rn_var_val = mock_idx_df[f"RN {self.var}"]
            print(f"\n\n{self.var}: {var_val:.2f}, (RN: {rn_var_val:.2f})")
            print(self.mock_dfs_list[idx])

    @cached_property
    def lineup_df(self):
        if not len(self.drafted_players):
            return None
        roster_df = self.full_df[
            self.full_df["Player"].isin(self.drafted_players)
        ].copy()
        roster_positions = ["QB", "RB", "WR", "TE"]
        pos_rank = {pos: i for i, pos in enumerate(roster_positions)}
        roster_df["sort"] = roster_df["Position"].map(pos_rank)
        roster_df.sort_values(
            ["sort", "VOR_Points"], ascending=[True, False], inplace=True
        )
        lineup_df = pd.DataFrame()
        for pos, n_req in self.n_pos.items():
            pos_df = roster_df[roster_df["Position"] == pos]
            if not len(pos_df):
                continue
            if pos == "FLEX":
                starting_pos_full_df = roster_df.loc[
                    (roster_df["Position"] != "QB")
                    & (~roster_df["Player"].isin(lineup_df["Player"]))
                ]
                if len(starting_pos_full_df):
                    starting_pos_df = (
                        starting_pos_full_df.sort_values(
                            "VOR_Points", ascending=False
                        )
                        .iloc[0]
                        .to_frame()
                        .T
                    )
                else:
                    continue
            else:
                starting_pos_df = pos_df.iloc[:n_req]
            lineup_df = pd.concat((lineup_df, starting_pos_df))
        lineup_df.sort_values(
            ["sort", "VOR_Points"], ascending=[True, False], inplace=True
        )
        return lineup_df

    def show_lineup(self):
        if not len(self.drafted_players):
            print("No Players Drafted")
            return
        lineup_df = self.lineup_df
        if lineup_df is None:
            print("No Players Drafted")
            return
        roster_df = self.full_df[
            self.full_df["Player"].isin(self.drafted_players)
        ].copy()
        bench_df = roster_df[
            ~roster_df["Player"].isin(lineup_df["Player"])
        ].copy()
        if len(bench_df):
            bench_df["sort"] = np.nan
            bench_df = bench_df.sort_values(
                ["sort", "Ceiling"], ascending=[True, False]
            )
            blank_row = pd.Series("-", index=lineup_df.columns).to_frame().T
            roster_table = pd.concat(
                (lineup_df, blank_row, bench_df)
            ).reset_index(drop=True)
        else:
            roster_table = lineup_df.reset_index(drop=True)
        starting_vor = lineup_df["VOR_Points"].sum()
        n_starters = sum(self.n_pos.values()) * self.n_teams
        avg_team_vor = (
            self.full_df["VOR_Points"]
            .sort_values(ascending=False)
            .iloc[:n_starters]
            .sum()
            / self.n_teams
        )
        print(f"\nCurrent Starting Lineup VOR: {starting_vor:.0f}")
        print(f"Avg Team VOR: {avg_team_vor:.0f}")
        print(f"Points above Average: {starting_vor - avg_team_vor:+.0f}")
        print()
        print(roster_table.drop(["ADP", "sort"], axis=1))
        print()

    def plot_dropoffs(self):
        next_pick = self.get_next_pick()
        next_next_pick = self.get_next_next_pick()
        fig, ax = plt.subplots(1, 1, figsize=[14, 8])
        colors = sns.color_palette("Set2", 4)
        max_postion_ranks = []
        circle_labels_in_legend = False
        rec_player_d = {}
        for pos, c in zip(self.positions, colors):
            if pos in self.ignored_positions:
                continue

            pos_df = (
                self.df[(self.df["Position"] == pos)]
                .sort_values(self.var, ascending=False)
                .reset_index(drop=True)
            )
            next_val = next_pick[pos][self.var]
            next_player = next_pick[pos]["Player"]
            next_next_val = next_next_pick[pos][self.var]
            next_next_player = next_next_pick[pos]["Player"]
            next_ix = pos_df[pos_df[self.var] == next_val].index[0]
            next_next_ix = pos_df[pos_df[self.var] == next_next_val].index[0]
            dropoff = next_val - next_next_val
            rec_player_d[next_player] = dropoff
            if circle_labels_in_legend:
                next_pick_label = "_nolegend_"
                next_next_pick_label = "_nolegend_"
            else:
                next_pick_label = "\nEst. Next Pick"
                next_next_pick_label = "\nEst. 2nd Next Pick\n"
                circle_labels_in_legend = True

            ax.scatter(
                next_ix,
                next_val,
                s=500,
                c="none",
                edgecolors=c,
                lw=1.5,
                label=next_pick_label,
            )
            ax.scatter(
                next_next_ix,
                next_next_val,
                s=300,
                linestyle="--",
                c="none",
                edgecolors=c,
                lw=1.5,
                label=next_next_pick_label,
            )
            dropoff_label = (
                f"{pos}: {dropoff:.0f} ({next_player} --> {next_next_player})"
            )
            ax.plot(
                pos_df[self.var], "-o", lw=1.5, ms=5, c=c, label=dropoff_label
            )
            ax.fill_between(
                pos_df.index,
                0 if self.y_axis_min is None else self.y_axis_min,
                pos_df[self.var],
                alpha=0.08,
                color=c,
                label="_nolegend_",
            )
            if self.y_axis_min is not None:
                df_cutoff = pos_df[pos_df[self.var] >= self.y_axis_min]
                max_postion_ranks.append(np.max(df_cutoff.index))

        if self.y_axis_min is not None:
            ax.set_ylim(self.y_axis_min, None)
            ax.set_xlim(-1, max(max_postion_ranks))

        ax.set_xlabel("Position Rank")
        var_label = self.var.replace("_", " ")
        ax.set_ylabel(var_label)
        plt.legend(
            shadow=True,
            fancybox=True,
            title=f"{var_label} Dropoff\nbetween picks",
            bbox_to_anchor=(1, 1),
            loc="upper left",
            fontsize=10,
        )
        rec_player = max(rec_player_d, key=rec_player_d.get)
        plt.title(
            f"Recommended Player: {rec_player}"
            f"\nCurrent Pick: {self.current_round}:{self.pick_of_current_round} ({self.current_pick})"
        )
        plt.tight_layout()
        plt.show()


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--year", type=int, required=True)
    parser.add_argument("--league", required=True)
    parser.add_argument("--pick", type=int, required=True)
    parser.add_argument(
        "-p", "--plot", action="store_true", help="Plot Dropoffs"
    )
    parser.add_argument("-m", "--mock", action="store_true", help="Mock Draft")
    parser.add_argument(
        "-l", "--lineup", action="store_true", help="Show Lineup"
    )
    return parser.parse_args()


# %%
def main():
    args = parse_args()
    draft = LiveDraft(
        year=args.year,
        league=args.league,
        pick_number=args.pick,
        var="VOR_Points",
        picks_made={"QB": 0, "RB": 0, "WR": 0, "TE": 0, "FLEX": 0},
        drafted_players=[],
        ignored_positions=[],
        DST_K_taken=0,
        y_axis_min=0,
    )
    draft.print_status()
    draft.show_table()
    if args.mock:
        draft.run_mock()
        draft.print_mock_results()
    if args.lineup:
        draft.show_lineup()
    if args.plot:
        draft.plot_dropoffs()


# %%
if __name__ == "__main__":
    main()
