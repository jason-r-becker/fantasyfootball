# %%
import argparse
import json
from collections import defaultdict
from functools import cached_property, lru_cache
from datetime import datetime as dt
from pathlib import Path

import numpy as np
import pandas as pd
import requests
from espn_api.football import League
from fuzzywuzzy import fuzz
from fuzzywuzzy import process


# %%
def root():
    return Path(__file__).parent.parent.parent


# Define the argparse functionality
def config_from_cli():
    parser = argparse.ArgumentParser(
        description="Get league ID and year for NFL season"
    )
    parser.add_argument(
        "-l", "--league", type=str, default=None, help="The league ID"
    )
    parser.add_argument(
        "-y", "--year", type=int, default=None, help="The NFL season year"
    )
    parser.add_argument(
        "-w", "--week", type=int, default=None, help="The week of the season"
    )
    cli_args = parser.parse_args()
    return Config(
        league=cli_args.league, year=cli_args.year, week=cli_args.week
    )


class Config:
    def __init__(self, league=None, year=None, week=None):
        self.league = self._get_league() if league is None else league
        self.year = self._get_current_year() if year is None else year
        self.week = self._get_current_week() if week is None else week

    @cached_property
    def path(self):
        return root() / f"data/{self.year}/{self.league}"

    @cached_property
    def _config_dict(self):
        with open(self.path / "config.json", "r") as file:
            return json.load(file)

    def get(self, key):
        try:
            return self._config_dict[key]
        except KeyError:
            raise KeyError(f"{key} not found in config.json")

    def _get_current_year(self):
        date = dt.now()
        return date.year - 1 if date.month <= 3 else date.year

    def _get_current_week(self):
        season_start_day = {
            2024: dt(2024, 9, 5),
            2025: dt(2025, 9, 4),
            2026: dt(2026, 9, 9),
        }[self.year]

        today = dt.now()
        # Find the first Tuesday after the season opener
        days_until_tuesday = (1 - season_start_day.weekday()) % 7
        first_tuesday = season_start_day + pd.Timedelta(days=days_until_tuesday)

        if today < season_start_day:
            return 0  # Preseason
        elif today < first_tuesday:
            return 1  # Week 1 (between Thursday opener and first Tuesday)
        else:
            # Weeks since first Tuesday
            weeks_since_first_tuesday = ((today - first_tuesday).days // 7) + 2
            return weeks_since_first_tuesday

    def _get_league(self):
        return input("Input League Name:  ")


class PlayerNameMapper:
    def __init__(self, year):
        self.year = year

    @property
    def fid(self):
        return root() / f"data/{self.year}/player_name_map.json"

    @property
    def json(self):
        try:
            with open(self.fid, "r") as file:
                return json.load(file)
        except FileNotFoundError:
            return {}


class BaseSiteAPI:
    def __init__(self, config):
        self.config = config

    @cached_property
    def my_roster_df(self):
        return self.roster_df.loc[
            self.roster_df["UserID"] == self._user_id
        ].drop(["FantasyTeam", "UserID"], axis=1)

    @property
    def _DST_map(self):
        dst_labels = ["DST", "D/ST", "DEF"]
        return {lbl: "DST" for lbl in dst_labels}


class ESPNAPI(BaseSiteAPI):
    def __init__(self, config):
        super().__init__(config)

    @property
    def _user_id(self):
        return self.config.get("team_id")

    @cached_property
    def _league(self):
        return League(
            league_id=self.config.get("league_id"),
            year=self.config.year,
            swid=self.config.get("swid"),
            espn_s2=self.config.get("espn_s2"),
        )

    @cached_property
    def roster_df(self):
        teams = self._league.teams
        d = defaultdict(list)
        for i, team in enumerate(teams):
            roster = team.roster
            for player in roster:
                d["Player"].append(player.name)
                d["Position"].append(
                    self._DST_map.get(player.position, player.position)
                )
                d["Team"].append(player.proTeam)
                d["FantasyTeam"].append(team.team_name)
                d["UserID"].append(team.team_id)
        return pd.DataFrame(d)


class SleeperAPI(BaseSiteAPI):
    def __init__(self, config):
        super().__init__(config)

    @property
    def base_url(self):
        return "https://api.sleeper.app/v1"

    @property
    def _user_id(self):
        return self.config.get("user_id")

    @cached_property
    def all_rosters(self):
        url = f"{self.base_url}/league/{self.config.get('league_id')}/rosters"
        response = requests.get(url)
        rosters = response.json()

        roster_data = {}

        # Clean the roster data to user player names
        # instead of player ids.
        for d in rosters:
            user_id = d.pop("owner_id")
            clean_d = d.copy()
            roster_df_d = defaultdict(list)
            for col in ["players", "reserve", "starters"]:
                new_col_vals = []
                if d[col] is None:
                    clean_d[col] = []
                    continue
                for player in d[col]:
                    try:
                        player_name = self._player_data[player]["full_name"]
                    except KeyError:
                        player_name = player
                    new_col_vals.append(player_name)

                    if col == "players":
                        roster_df_d["Player"].append(player_name)
                        try:
                            fantasy_pos = self._player_data[player][
                                "fantasy_positions"
                            ][0]

                            fantasy_pos = self._DST_map.get(
                                fantasy_pos, fantasy_pos
                            )
                            roster_df_d["Position"].append(fantasy_pos)
                        except (KeyError, IndexError):
                            roster_df_d["Position"].append(None)
                        try:
                            roster_df_d["Team"].append(
                                self._player_data[player]["team"]
                            )
                        except KeyError:
                            roster_df_d["Team"].append(None)

                clean_d[col] = new_col_vals

            clean_d["roster_df"] = pd.DataFrame(roster_df_d)
            roster_data[user_id] = clean_d

        return roster_data

    @cached_property
    def roster_df(self):
        roster_data = self.all_rosters
        df_list = []
        for user_id, d in roster_data.items():
            df = d["roster_df"]
            df["FantasyTeam"] = self._user_team_names[user_id]
            df["UserID"] = user_id
            df_list.append(df)
        return pd.concat(df_list)

    @property
    def roster(self):
        return self.all_rosters[self.config.get("user_id")]

    @cached_property
    def _player_data(self):
        url = f"{self.base_url}/players/nfl"
        response = requests.get(url)
        player_data = response.json()
        return player_data

    @lru_cache(maxsize=None)
    def _user_data(self, user_id):
        url = f"{self.base_url}/user/{user_id}"
        return requests.get(url).json()

    @cached_property
    def _user_team_names(self):
        url = f"{self.base_url}/league/{self.config.get('league_id')}/users"
        league_data = requests.get(url).json()

        user_mapping = {}
        for user in league_data:
            user_id = user["user_id"]
            try:
                team_name = user["metadata"]["team_name"]
            except KeyError:
                team_name = f"Team {self._user_data(user_id)['display_name']}"

            user_mapping[user_id] = team_name
        return user_mapping


def site_API(config):
    return {
        "SLEEPER": SleeperAPI,
        "ESPN": ESPNAPI,
    }[config.get("site").upper()](config)
