"""
Build a pandas DataFrame for the 2024 NCAA tournament teams (64, ignore play-ins)
and attach KenPom Net Ratings (NetRtg) from the provided table. Also include
two spread->win-probability conversions (power-law and simple linear).

Usage:
- Run `python ncaam_tournament_seed.py` to fetch the 64 teams from Wikipedia
  (best-effort text parsing). If fetching/parsing fails, set `teams64` manually
  and run again.

Outputs:
- prints a short table and saves `tournament_2024_net_ratings.csv` with the
  team, seed (when available), NetRtg and two probability conversions.
"""

from typing import Dict, List
import re
import requests
import pandas as pd
import math


# ---- KenPom NetRtg mapping (use values you supplied) ----
# This mapping includes the teams commonly in the 2024 field. Add or edit
# entries as needed to match exact team name spellings used by the bracket.
KENPOM_NET_RATINGS: Dict[str, float] = {
	"Connecticut": 36.43,
	"Houston": 31.17,
	"Purdue": 30.62,
	"Auburn": 27.99,
	"Tennessee": 26.61,
	"Arizona": 26.55,
	"Duke": 26.47,
	"Iowa State": 26.47,
	"Iowa St.": 26.47,
	"North Carolina": 26.19,
	"Illinois": 24.53,
	"Creighton": 24.22,
	"Gonzaga": 23.17,
	"Marquette": 23.02,
	"Alabama": 22.96,
	"Baylor": 21.90,
	"Michigan State": 20.58,
	"Michigan St.": 20.58,
	"Wisconsin": 20.06,
	"BYU": 19.96,
	"Clemson": 19.44,
	"Saint Mary's": 19.43,
	"St. Mary's": 19.43,
	"St. John's": 19.41,
	"San Diego State": 19.36,
	"San Diego St.": 19.36,
	"Kentucky": 19.29,
	"Colorado": 19.03,
	"Texas": 18.77,
	"Florida": 18.19,
	"Kansas": 17.94,
	"Wake Forest": 17.89,
	"New Mexico": 17.80,
	"Nebraska": 17.55,
	"Texas Tech": 17.32,
	"Dayton": 17.30,
	"Pittsburgh": 17.18,
	"Mississippi State": 17.07,
	"Mississippi St.": 17.07,
	"Texas A&M": 17.02,
	"Colorado State": 17.02,
	"Villanova": 16.78,
	"Indiana State": 16.69,
	"Indiana St.": 16.69,
	"Cincinnati": 16.47,
	"Nevada": 16.41,
	"Northwestern": 16.41,
	"Washington State": 16.33,
	"TCU": 16.04,
	"Boise State": 15.97,
	"Boise St.": 15.97,
	"NC State": 15.90,
	"Oklahoma": 15.77,
	"Florida Atlantic": 15.74,
	"FAU": 15.74,
	"Utah": 15.41,
	"Ohio State": 15.19,
	"Ohio St.": 15.19,
	"Seton Hall": 15.03,
	"Utah State": 14.89,
	"Grand Canyon": 14.84,
	"Drake": 14.53,
	"South Carolina": 14.40,
	"Oregon": 14.19,
	"Xavier": 14.17,
	"Iowa": 14.15,
	"Virginia Tech": 14.08,
	"Providence": 13.70,
	"Washington": 12.95,
	"Butler": 12.60,
	"Maryland": 12.51,
	"James Madison": 12.42,
	"Bradley": 12.18,
}


def spread_to_win_probability(spread: float) -> float:
	"""Convert a point spread (team - opp) into win probability.

	Uses a power-law mapping commonly used in betting analytics:
	P = 1 / (1 + 10^(spread / -2.56)). This handles tails better than a
	simple linear approximation.
	"""
	return 1.0 / (1.0 + 10.0 ** (spread / -2.56))


def spread_to_win_probability_linear(spread: float) -> float:
	"""Simple linear approx: each point ≈ 0.0295 (2.95%) change in win prob."""
	return max(0.0, min(1.0, 0.5 + spread * 0.0295))


def fetch_64_teams_from_wikipedia() -> List[str]:
	"""Attempt to fetch and parse the 64 teams (ignore play-ins) from the
	Wikipedia tournament page. This is a best-effort textual parse and may
	require manual correction.
	"""
	url = "https://en.wikipedia.org/wiki/2024_NCAA_Division_I_men%27s_basketball_tournament"
	r = requests.get(url, timeout=10)
	txt = r.text

	# find seed lines like: | 1 | UConn | ... or '|  1 | UConn |'
	pattern = re.compile(r"\|\s*(?:1|2|3|4|5|6|7|8|9|10|11|12|13|14|15|16)\s*\|\s*([A-Za-z0-9\.'#&\- ]+?)\s*\|")
	matches = pattern.findall(txt)

	# matches will include many duplicates across regions; take unique preserving order
	seen = set()
	teams = []
	for m in matches:
		name = m.strip()
		# clean common artifacts
		name = re.sub(r"\s+\\u2013.*$", "", name)
		if name not in seen:
			seen.add(name)
			teams.append(name)
		if len(teams) >= 64:
			break
	return teams


def build_dataframe(teams: List[str]) -> pd.DataFrame:
	rows = []
	for t in teams:
		net = KENPOM_NET_RATINGS.get(t)
		# try alternate keys (common abbreviations / punctuation)
		if net is None:
			alt = t.replace("\u00b7", "").replace('.', '').replace(" St", " St.")
			net = KENPOM_NET_RATINGS.get(alt)
		rows.append({
			"Team": t,
			"KenPom_NetRtg": net,
			"WinProb_Power": spread_to_win_probability(net) if net is not None else None,
			"WinProb_Linear": spread_to_win_probability_linear(net) if net is not None else None,
		})
	df = pd.DataFrame(rows)
	return df


def main():
	# 1) try to fetch teams automatically
	try:
		teams64 = fetch_64_teams_from_wikipedia()
		print(f"Fetched {len(teams64)} team names (best-effort).")
	except Exception as e:
		print("Failed to fetch teams from Wikipedia:", e)
		teams64 = []

	if len(teams64) < 64:
		print("Could not reliably fetch 64 teams. Please provide a list named `teams64` manually in the script and re-run.")
		# For convenience, keep an empty DataFrame and exit
		return

	df = build_dataframe(teams64)
	# show teams missing NetRtg
	missing = df[df['KenPom_NetRtg'].isna()]
	if not missing.empty:
		print("Teams missing NetRtg (spelling mismatch or not in mapping):")
		print(missing[['Team']].to_string(index=False))

	# Save and print summary
	df.to_csv("tournament_2024_net_ratings.csv", index=False)
	pd.set_option('display.width', 200)
	print(df)
	print("Saved tournament_2024_net_ratings.csv")


if __name__ == '__main__':
	main()

