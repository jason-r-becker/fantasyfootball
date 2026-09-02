# %%
import json
from collections import defaultdict

import requests
import pandas as pd

from fantasyfootball.utils import root

# %%

def get_sleeper_user_mapping(league_id):
    base_url = "https://api.sleeper.app/v1"
    league_users_json = requests.get(f"{base_url}/league/{league_id}/users").json()
    user_mapping = {}
    for user in league_users_json:
        user_id = user["user_id"]
        try:
            team_name = user["metadata"]['team_name']
        except KeyError:
            sleeper_user_json = requests.get(f'{base_url}/user/{user_id}').json()
            team_name = f"Team {sleeper_user_json['display_name']}"

        user_mapping[user_id] = team_name
    return user_mapping

def sleeper_draft_df(draft_id, user_mapping):
    base_url = "https://api.sleeper.app/v1"
    draft_json = requests.get(f"{base_url}/draft/{draft_id}/picks").json()
    d = defaultdict(list)
    for pick_info in draft_json:
        pick = pick_info["metadata"]
        d['Team'].append(user_mapping[pick_info["picked_by"]])
        d["Player"].append(f"{pick['first_name']} {pick['last_name']}")
        d["Position"].append(pick["position"])

    df = pd.DataFrame(d)
    df.index += 1
    return df


def mfl_draft_df(year, league_id):
    draft_url = f"https://www64.myfantasyleague.com/{year}/export?TYPE=draftResults&L={league_id}&JSON=1"
    draft_json = requests.get(draft_url).json()
    draft_data = draft_json["draftResults"]["draftUnit"][1]["draftPick"]

    player_map_url = (
        f"https://api.myfantasyleague.com/{year}/export?TYPE=players&JSON=1"
    )
    player_json = requests.get(player_map_url).json()
    player_map = {}
    for player in player_json["players"]["player"]:
        player_name = " ".join(player["name"].split(",")[::-1]).strip()
        player_map[player["id"]] = player_name

    d = defaultdict(list)
    for pick in draft_data:
        d["Player"].append(player_map.get(pick["player"], None))
        d["Team"].append(pick["franchise"])

    df = pd.DataFrame(d)
    df.index += 1
    return df

# %%



if __name__ == "__main__":
    year = int(input("Input Year:  "))
    league = input("Input League Name:  ")
    
    path = root() / f"data/{year}/{league}"
    with open(path / "config.json", "r") as file:
        config = json.load(file)
    site = config['site'].lower()


    if site == 'sleeper':
        try:
            with open(path / 'user_mapping.json', 'r') as file:
                user_mapping = json.load(file)
        except FileNotFoundError:
            user_mapping = get_sleeper_user_mapping(config['league_id'])
            with open(path / 'user_mapping.json', 'w') as file:
                json.dump(user_mapping, file)
        df = sleeper_draft_df(draft_id=config['draft_id'], user_mapping=user_mapping)
    elif site == 'myfantasyleague':
        df = mfl_draft_df(year, config['league_id'])

    df.to_csv(path  / 'raw_draft_summary.csv')
