# %%
import pandas as pd
from pathlib import Path

from fantasyfootball.utils import root

def load_nflverse_player_stats_week(season: int) -> pd.DataFrame:
    url = (
        f"https://github.com/nflverse/nflverse-data/releases/"
        f"download/player_stats/stats_player_week_{season}.csv"
    )
    df = pd.read_csv(url)
    return df


year = 2024
stats_df = load_nflverse_player_stats_week(year)
fid = root() / f'data/{year}/stats.csv'
stats_df.to_csv(fid, index=False)
