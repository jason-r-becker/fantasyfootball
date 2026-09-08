# %%
import json
import shutil
from functools import cached_property
from pathlib import Path

import pandas as pd
from fuzzywuzzy import fuzz
from fuzzywuzzy import process
from tabulate import tabulate
from tqdm import tqdm

from fantasyfootball.utils import config_from_cli, Config, root, site_API


# %%
class LineupOptimizer:
    def __init__(self, config):
        self.config = config
        self.year = self.config.year
        self.week = self.config.week
        self.api = site_API(self.config)
        self.min_fuzzy_threshold = 80
        self.auto_fuzzy_threshold = 100

    @cached_property
    def projections(self):
        # First look at the downloads folder for a new file
        # to move into the weekly_projections folder.
        print()
        try:
            self._move_projections_from_downloads()
        except FileNotFoundError:
            pass
        else:
            print("New file read from downloads folder.")

        # The try to load a file from the weekly_projections folder.
        fid = self._get_weekly_projection_fid()
        try:
            proj_df = self._read_projections_fid(fid)
        except FileNotFoundError:
            raise FileNotFoundError(f"{fid} not found")

        return proj_df

    def _move_projections_from_downloads(self):
        today = pd.Timestamp.now().weekday()
        downloads_path = Path.home() / "Downloads"
        downloads_fid = downloads_path / self._projections_fid()
        destination_fid = self._projections_path / self._projections_fid(today)
        shutil.move(str(downloads_fid), str(destination_fid))

    def _projections_fid(self, weekday=None):
        fid = f"projections_{self.year}_wk{self.week}.csv"
        if weekday is not None:
            fid = fid.replace(".csv", f"_d{weekday}.csv")
        return fid

    @cached_property
    def _projections_path(self):
        path = root() / "/".join(
            [
                "data",
                f"{self.year:.0f}",
                self.config.league,
                "weekly_projections",
            ]
        )
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _get_weekly_projection_fid(self):
        projection_fids = sorted(
            list(self._projections_path.glob(self._projections_fid("*")))
        )
        if not projection_fids:
            raise FileNotFoundError(
                f"No weekly projections found for {self.year} week "
                f"{self.week} for {self.config.league}."
            )
        return projection_fids[-1]

    def _read_projections_fid(self, fid):
        df = pd.read_csv(fid)
        df["team"] = df["team"].fillna("FA")
        df["projection"] = df[["floor", "points", "ceiling"]].mean(axis=1)
        df = df[df["player"].notna()]
        # Sometimes players are duplicated. Keep the row
        # with most data or highest projection.
        return (
            df.assign(_missing_fields=df.isna().sum(axis=1))
            .sort_values(
                ["player", "_missing_fields", "points"],
                ascending=[True, True, False],
                kind="stable",
            )
            .drop_duplicates("player")
            .drop(columns="_missing_fields")
            .reset_index(drop=True)
        )

    @property
    def _league_roster_df(self):
        df = self.api.roster_df
        df["Team"] = df["Team"].map(self._source_team_map).fillna(df["Team"])
        df["Player"] = (
            df["Player"].map(self._source_player_map).fillna(df["Player"])
        )
        return df

    @property
    def _source_team_map_fid(self):
        return root() / f"data/{self.year:.0f}/source_team_map.json"

    @property
    def _source_team_map(self):
        try:
            with open(self._source_team_map_fid, "r") as file:
                return json.load(file)
        except FileNotFoundError:
            return {}

    @property
    def _source_player_map_fid(self):
        return root() / f"data/{self.year:.0f}/source_player_map.json"

    @property
    def _source_player_map(self):
        try:
            with open(self._source_player_map_fid, "r") as file:
                return json.load(file)
        except FileNotFoundError:
            # Map all of the defenses.
            df = self.projections
            dst_df = df[df["position"] == "DST"]
            return dict(zip(dst_df["team"], dst_df["player"]))

        # def _update_source_team_map(self):
        site_teams = set(self._league_roster_df["Team"].unique())
        projection_teams = set(self.projections["team"].unique())

        teams_needing_mapping = site_teams - projection_teams
        missing_teams = projection_teams - site_teams

        if not teams_needing_mapping:
            return

        team_map = self._source_team_map.copy()
        print(
            f"Missing Teams from {self.config.get('site')} API: {missing_teams}"
        )
        print(teams_needing_mapping)
        print(projection_teams)
        print("Please input the correct mapping from the above teams.")
        for team in teams_needing_mapping:
            team_map[team] = input(f"Team code for {team}: ")

        with open(self._source_team_map_fid, "w") as file:
            json.dump(team_map, file, indent=4, sort_keys=True)

    def _update_source_player_map(self):
        lr_df = self._league_roster_df.copy()
        site_players = set(lr_df["Player"].unique())
        projection_players = set(self.projections["player"].unique())

        players_needing_mapping = site_players - projection_players
        missing_players = projection_players - site_players

        if not players_needing_mapping:
            return

        player_map = self._source_player_map.copy()
        print(
            f"Missing player mappings for {len(players_needing_mapping)} players."
        )
        print("Please input the correct mapping from the above players.")

        for player in sorted(list(players_needing_mapping)):
            # Find fuzzy matches based on the player name.
            fuzzy_matches = process.extract(
                player, missing_players, scorer=fuzz.partial_ratio, limit=3
            )

            potential_matches_list = [x[0] for x in fuzzy_matches]
            scores = [x[1] for x in fuzzy_matches]
            highest_score = max(scores)
            if highest_score <= self.min_fuzzy_threshold:
                continue  # No good matches found.
            elif highest_score >= self.auto_fuzzy_threshold:
                # Great match, no user input required.
                max_idx = scores.index(highest_score)
                player_map[player] = potential_matches_list[max_idx]
                print(
                    f"Auto Match: {player}: {potential_matches_list[max_idx]}"
                )
                continue

            # Find matches based on the team and position.
            position, team = (
                lr_df.loc[lr_df["Player"] == player, ["Position", "Team"]]
                .squeeze()
                .to_list()
            )
            position_team_df = self.projections[
                (self.projections["position"] == position)
                & (self.projections["team"] == team)
            ]
            potential_matches_list.extend(position_team_df["player"].tolist())

            potential_matches_d = {
                i: pm for i, pm in enumerate(potential_matches_list)
            }
            printed_matches_d = {
                i: f"{pm} (score: {score})"
                for i, (pm, score) in enumerate(
                    zip(potential_matches_list, scores)
                )
            }

            printed_matches_d["[Enter]"] = "No Match"
            print()
            print(
                f"Please select the correct mapping for: {player} ({position}-{team})"
            )
            print(json.dumps(printed_matches_d, indent=2))
            user_input = input("Key: ")
            if user_input == "":
                continue

            val = potential_matches_d[int(user_input)]
            player_map[player] = val

        with open(self._source_player_map_fid, "w") as file:
            json.dump(player_map, file, indent=4, sort_keys=True)

    @property
    def roster(self):
        return self.df[self.df["UserID"] == self.api._user_id].copy()

    @cached_property
    def df(self):
        # df = pd.concat(
        #     [
        #         self._league_roster_df.set_index("Player").rename_axis(None),
        #         self.projections.set_index("player").rename_axis(None),
        #     ],
        #     axis=1,
        #     join="outer",
        # ).drop(["Position", "Team"], axis=1)
        # df["FantasyTeam"].fillna("FA", inplace=True)
        league_df = self._league_roster_df.copy()
        proj_df = self.projections.copy()

        # normalize projection player column to match roster column name
        if "player" in proj_df.columns:
            proj_df = proj_df.rename(columns={"player": "Player"})

        # If roster contains duplicate Player rows, keep first occurrence
        league_df = league_df.drop_duplicates(subset="Player", keep="first")

        # outer merge so we keep players present in either source
        merged_df = pd.merge(
            league_df,
            proj_df,
            on="Player",
            how="outer",
            suffixes=("", "_proj"),
        ).set_index("Player")

        merged_df["FantasyTeam"].fillna("FA", inplace=True)
        for col in ("Position", "Team"):
            if col in merged_df.columns:
                merged_df = merged_df.drop(columns=[col])

        return merged_df

    def optimize_lineup(self, update_player_map=False):
        # self._update_source_team_map()
        if update_player_map:
            self._update_source_player_map()
        roster_needs = self.config.get("positions")
        for position in ["K", "DST"]:
            if position in set(self.api.my_roster_df["Position"].unique()):
                roster_needs[position] = 1

        df = self.df[
            (self.df["UserID"] == self.api._user_id)
            | (self.df["FantasyTeam"] == "FA")
        ]

        lineup_df_list = []
        players_in_lineup = set()
        for position, n_needed in roster_needs.items():
            if position == "FLEX":
                pos_df = df[
                    (df["position"].isin(["RB", "WR", "TE"]))
                    & (~df.index.isin(players_in_lineup))
                ].copy()
                pos_df["position"] = "FLEX"
            elif position in {"DST", "K"}:
                pos_df = df[
                    (df["position"] == position) & (df["FantasyTeam"] != "FA")
                ].copy()
            else:
                pos_df = df[df["position"] == position].copy()

            pos_df = pos_df.sort_values("projection", ascending=False)
            for i in range(n_needed):
                try:
                    player_row = pos_df.iloc[i]
                except Exception:
                    blank_player = pd.Series(
                        0, index=df.columns, dtype=object, name="FA"
                    )
                    blank_player["position"] = position
                    for col in df.columns[:2]:
                        blank_player[col] = df[col].iloc[0]
                    players_in_lineup.add(blank_player.name)
                    lineup_df_list.append(blank_player.to_frame().T)
                    continue

                players_in_lineup.add(player_row.name)
                lineup_df_list.append(player_row.to_frame().T)

        cols = [
            "position",
            "projection",
            "floor",
            "points",
            "ceiling",
            "FantasyTeam",
        ]
        lineup_df = pd.concat(lineup_df_list)[cols]
        for col in cols[1:-1]:
            lineup_df[col] = lineup_df[col].astype(float)

        # Create divider row:
        divider = (
            pd.Series({col: "-" * len(col) for col in cols}, name="---")
            .to_frame()
            .T
        )

        # Get bench players:
        bench_players = {"QB": 2, "RB": 3, "WR": 3, "TE": 2, "DST": 4}
        bench_pos_dfs = [
            df[
                (df["position"] == position)
                & (~df.index.isin(lineup_df.index))
            ]
            .sort_values("projection", ascending=False)
            .head(n)
            for position, n in bench_players.items()
        ]
        bench_df = pd.concat(bench_pos_dfs)[cols]

        full_lineup_df = pd.concat(
            [lineup_df.round(1), divider, bench_df.round(1)]
        )
        full_lineup_df["FA"] = full_lineup_df["FantasyTeam"] == "FA"
        full_lineup_df.drop("FantasyTeam", axis=1, inplace=True)
        full_lineup_df["FA"] = full_lineup_df["FA"].apply(
            lambda x: "✓" if x else ""
        )
        return full_lineup_df

    def save_weekly_rosters(self):
        path = root() / "/".join(
            [
                "data",
                f"{self.year:.0f}",
                self.config.league,
                "weekly_rosters",
            ]
        )
        path.mkdir(parents=True, exist_ok=True)
        fid = path / f"roster_week_{self.week:.0f}.csv"
        self.api.roster_df.to_csv(fid, index=False)

    def print_lineup(self, update_player_map=False):
        full_lineup = self.optimize_lineup(update_player_map=update_player_map)
        divider_loc = full_lineup.index.get_loc("---")
        divider = full_lineup.iloc[divider_loc].to_frame().T
        lineup = full_lineup.iloc[:divider_loc, 1:]

        total = (
            lineup.sum()
            .iloc[:-1]
            .astype(float)
            .round(1)
            .rename("Total")
            .to_frame()
            .T
        )
        cols = [
            "position",
            "projection",
            "floor",
            "points",
            "ceiling",
            "FA",
        ]
        table = pd.concat([total, divider, full_lineup]).fillna("")[cols]
        print(
            f"\n\nProjections for {self.config.league} week {self.week:.0f}:"
        )
        print(tabulate(table, headers="keys", tablefmt="psql"))


def drop_duplicates(group):
    fewest_nans = group.isna().sum(axis=1).min()
    fewest_nans_rows = group[group.isna().sum(axis=1) == fewest_nans]
    return fewest_nans_rows.loc[fewest_nans_rows["points"].idxmax()]


if __name__ == "__main__":
    config = config_from_cli()
    lo = LineupOptimizer(config)
    print(f"Projections for {lo.config.league} week {lo.week:.0f}:")
    lo.save_weekly_rosters()
    lo.print_lineup(update_player_map=True)
