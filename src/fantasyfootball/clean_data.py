# %%
import json
import os
from pathlib import Path

from collections import defaultdict

import numpy as np
import pandas as pd
from fuzzywuzzy import fuzz
from fuzzywuzzy import process

from fantasyfootball.utils import root

# %%

year = int(input("Input Year:  "))
league = input("Input League Name:  ")
path = root() / f"data/{year}/{league}"

if not path.exists():
    raise FileNotFoundError(
        f"League: '{league}' does not exist, no path found for {path}"
    )

positions = ["QB", "RB", "WR", "TE", "FLEX"]

try:
    with open(path / "config.json", "r") as file:
        config = json.load(file)
except FileNotFoundError:
    config = {}
    config["teams"] = int(input("Input Number of Teams: "))
    config["positions"] = {}
    config["site"] = "Sleeper"
    for pos in positions:
        config["positions"][pos] = int(input(f"Input the Number of {pos}'s: "))

    with open(path / "config.json", "w") as file:
        json.dump(config, file)


n_teams = config["teams"]
n_pos = config["positions"]
adp_source = config["site"].lower()

# %%
df = pd.read_csv(path / "raw.csv").reset_index()
df.columns = [col.lower() for col in df.columns]

keep_cols = [
    "player",
    "team",
    "position",
    "points",
    "floor",
    "ceiling",
    "sd_pts",
]


col_map = {
    "floor": "Floor",
    "mean": "Points",
    "ceiling": "Ceiling",
    "sd_pts": "Std Dev",
}


df = df[keep_cols].reset_index(drop=True)
df.columns = [col_map.get(c, c.title()) for c in df.columns]
vor_cols = ["Points", "Floor", "Ceiling"]
for col in vor_cols:
    df[f"VOR_{col}"] = np.nan


def get_draft_based_replacement_value(position, pos_df, n_pos):
    if position == "FLEX":
        n = n_pos["RB"] + n_pos["WR"] + n_pos["FLEX"] + 1
    elif position == "TE":
        n = n_pos["TE"]
    elif position == "WR":
        n = n_pos["WR"] + 1
    elif position == "RB":
        n = n_pos["RB"] + 1
    elif position == "QB":
        n = n_pos["QB"]
    repl_val = np.mean(pos_df["Points"].iloc[n_teams * n : n_teams * (n + 1)])
    return repl_val


pos_df_list = []
flex_df_list = []
for pos, n in n_pos.items():
    if pos == "FLEX":
        pos_df = df[df["Position"].isin(["RB", "WR", "TE"])].sort_values(
            "Points", ascending=False
        )
    else:
        pos_df = df[df["Position"] == pos].sort_values(
            "Points", ascending=False
        )
    try:
        replacement_val = config["waiver_weekly_position_value"][pos] * 17
        print(f"{pos} -- from Waivers: {replacement_val:.1f}")
    except KeyError:
        replacement_val = get_draft_based_replacement_value(pos, pos_df, n_pos)
        print(f"{pos} -- from Draft: {replacement_val:.1f}")
    for metric in vor_cols:
        col = f"FLEX_VOR_{metric}" if pos == "FLEX" else f"VOR_{metric}"
        pos_df[col] = pos_df[metric] - replacement_val

    if pos == "FLEX":
        flex_df_list.append(pos_df)
    else:
        pos_df_list.append(pos_df)


df = pd.concat(pos_df_list).sort_values("VOR_Floor", ascending=False)
flex_df = pd.concat(flex_df_list)
for metric in vor_cols:
    col = f"FLEX_VOR_{metric}"
    df[col] = flex_df[col]

df.index += 1
df

# %%
# Add ADP and bye week.
adp_df = pd.read_csv(
    path / "adp.csv", engine="python", on_bad_lines="skip"
).dropna(how="all")
adp_df.columns = adp_df.columns.str.lower()
adp_df = adp_df.rename(columns={"bye": "Bye", "player": "Player", "pos": "POS"})
platform_adp = pd.to_numeric(adp_df[adp_source], errors="coerce")
if "avg" in adp_df:
    consensus_adp = pd.to_numeric(adp_df["avg"], errors="coerce")
    adp_df["ADP"] = platform_adp.fillna(consensus_adp)
else:
    adp_df["ADP"] = platform_adp
ignored_positions = ["K", "DST"]
for pos in ignored_positions:
    adp_df = adp_df[~adp_df["POS"].str.startswith(pos)].copy()

clean_df = pd.merge(
    df, adp_df[["Bye", "ADP", "Player"]], on="Player", how="left"
)

# When matching data there will be mismatches. Fix them by first finding
# all player names in the clean data that don't have an exact match.
missing_adp_picks = set(list(range(1, 200))) - set(clean_df["ADP"].values)
missing_players = adp_df.loc[
    adp_df["ADP"].isin(missing_adp_picks), "Player"
].values

season_player_map_path = path.parent / "source_player_map.json"
try:
    with open(season_player_map_path, "r") as file:
        season_player_map = json.load(file)
except FileNotFoundError:
    season_player_map = {}

# %%


# Perform fuzzy logic matching on all the players without an exact match,
# prompting the user to confirm/deny each fuzzy match.
def find_best_match(name, choices):
    return process.extractOne(name, choices, scorer=fuzz.partial_ratio)


projection_players = set(clean_df["Player"])
confirmed_player_map = {
    player: season_player_map[player]
    for player in missing_players
    if player in season_player_map
    and season_player_map[player] in projection_players
}
unresolved_players = [
    player for player in missing_players if player not in confirmed_player_map
]
raw_missing_player_map = {
    player: find_best_match(player, clean_df["Player"])[0]
    for player in unresolved_players
}
missing_player_map = confirmed_player_map.copy()
if confirmed_player_map:
    print("\nUsing confirmed mappings from source_player_map.json:")
    print(pd.Series(confirmed_player_map))
print("\nVerify that the player mapping is correct.")
print('Press [Enter] if the player is correct, input "N" for incorrect\n')
for adp_player, clean_player in raw_missing_player_map.items():
    adp = adp_df.loc[adp_df["Player"] == adp_player, "ADP"].squeeze()
    while True:
        user_input = input(f"  {adp:.0f}) {adp_player} --> {clean_player}:  ")
        if user_input == "":
            missing_player_map[adp_player] = clean_player
            break
        elif user_input.upper() == "N":
            break
        elif user_input.upper() in {"Q", "E"}:
            print("\nExiting")
            quit()
        else:
            print('\nPlease only input [Enter] or "N"')

print("\nThe mapped players are listed below")
print(pd.Series(missing_player_map))

# %%
# Update the ADP and Bye week for the confirmed fuzzy matched players.
for adp_player, clean_player in missing_player_map.items():
    adp = adp_df.loc[adp_df["Player"] == adp_player, "ADP"].squeeze()
    bye = adp_df.loc[adp_df["Player"] == adp_player, "Bye"].squeeze()
    clean_df.loc[clean_df["Player"] == clean_player, "ADP"] = adp
    clean_df.loc[clean_df["Player"] == clean_player, "Bye"] = bye


# Save to both 'clean' and 'live' .csv files, to be used by live_draft.py
cols = [
    "Player",
    "Team",
    "Position",
    "Bye",
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
    "ADP",
]
clean_df = clean_df[cols].round(2)

for fid in ["clean", "live_draft"]:
    clean_df.to_csv(path / f"{fid}.csv")

# %%
