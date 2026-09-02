# %%
from __future__ import annotations

import re
from collections import defaultdict
from functools import cached_property
from pathlib import Path
from typing import Dict, List

import numpy as np
import pandas as pd
import statsmodels.api as sm
from scipy.stats import spearmanr


from fuzzywuzzy import fuzz
import matplotlib

try:
    # tqdm.auto behaves well in terminals and notebooks.
    from tqdm.auto import tqdm as _tqdm
except Exception:
    # fallback no-op tqdm if package not installed
    def _tqdm(x, **kwargs):
        return x


try:
    matplotlib.use("TkAgg")
except Exception:
    # Headless / missing-tk environments
    matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib as mpl
import seaborn as sns

from fantasyfootball.utils import root

# %%


class ProjectionVsActualAnalyzer:
    # ----------------------------
    # Canonical maps
    # ----------------------------
    TEAM_MAP: Dict[str, str] = {
        "LAR": "LA",
        "LA": "LA",
        "STL": "LA",
        "LVR": "LV",
        "JAC": "JAX",
        "JAX": "JAX",
        "WSH": "WAS",
        "WFT": "WAS",
        "OAK": "LV",
        "LV": "LV",
        "SD": "LAC",
        "LAC": "LAC",
        "TB": "TB",
        "TAM": "TB",
    }

    POSITION_MAP: Dict[str, str] = {
        # Offense
        "QB": "QB",
        "RB": "RB",
        "WR": "WR",
        "TE": "TE",
        "K": "K",
        "PK": "K",
        "DST": "DST",
        "DEF": "DST",
        # Defensive line
        "DE": "DL",
        "DT": "DL",
        "NT": "DL",
        "IDL": "DL",
        "EDGE": "DL",
        "DL": "DL",
        # Linebacker
        "LB": "LB",
        "ILB": "LB",
        "OLB": "LB",
        "MLB": "LB",
        # Defensive back
        "CB": "DB",
        "S": "DB",
        "SS": "DB",
        "FS": "DB",
        "DB": "DB",
    }

    # Default hard-coded mapping from projected stat base names to actual stat base names
    # Edit this mapping to match your projection file's column names to the actual stats file.
    STAT_COLUMN_MAPPING: Dict[str, str] = {
        # passing
        "pass_yds": "passing_yards",
        "pass_tds": "passing_tds",
        "pass_int": "passing_interceptions",
        # rushing
        "rush_yds": "rushing_yards",
        "rush_tds": "rushing_tds",
        # receiving
        "rec": "receptions",
        "rec_yds": "receiving_yards",
        "rec_tds": "receiving_tds",
        # misc
        "fumbles_lost": "rushing_fumbles",
        "two_pts": "rushing_2pt_conversions",
        # kicker buckets (example)
        "fg_0019": "fg_made_0_19",
        "fg_2029": "fg_made_20_29",
        "fg_3039": "fg_made_30_39",
        "fg_4049": "fg_made_40_49",
        "fg_50": "fg_made_50_59",
        "xp": "pat_made",
        # IDP mapping from projection to actual defensive stats
        "idp_solo": "def_tackles_solo",
        "idp_sack": "def_sacks",
        "idp_int": "def_interceptions",
        "idp_pd": "def_pass_defended",
        "idp_td": "def_tds",
    }

    def __init__(
        self,
        season: int,
        min_score: int = 85,
        min_proj_points: float = 4.0,
        show_progress: bool = True,
    ):
        self.season = season
        self.min_score = min_score

        self.season_dir = root() / f"data/{season}"
        self.proj_dir = self.season_dir / "stat_projections"
        self.player_stats_path = self.season_dir / "stats.csv"
        self.out_dir = self.season_dir / "projection_evaluation"

        # minimum projected fantasy points to consider a player for matching
        self.min_proj_points = float(min_proj_points)
        self.show_progress = bool(show_progress)
        # diagnostics containers
        self._missing_proj_cols = set()
        # collected match scores and dropped rows for diagnostics
        self._match_scores: List[dict] = []
        self._dropped_projections: List[dict] = []
        self._dropped_actuals: List[dict] = []
        # kicker/dst column diagnostics
        self._kicker_proj_columns_seen: set = set()
        self._kicker_actual_columns_seen: set = set()

    def _progress(self, iterable, *, leave: bool = False, **kwargs):
        """A single place to control tqdm behavior.

        Keeps output clean by defaulting to leave=False and throttling updates.
        """
        if not self.show_progress:
            return iterable
        # tqdm ignores unknown kwargs in some versions; keep it conservative.
        return _tqdm(
            iterable,
            leave=leave,
            dynamic_ncols=True,
            mininterval=0.8,
            **kwargs,
        )

    # ----------------------------
    # Normalization helpers
    # ----------------------------
    @staticmethod
    def normalize_name(name: str) -> str:
        if pd.isna(name):
            return ""
        n = str(name).lower()
        n = re.sub(r"[^a-z0-9\s]", "", n)
        n = re.sub(r"\b(jr|sr|ii|iii|iv|v)\b", "", n)
        n = re.sub(r"\s+", " ", n).strip()
        return n

    def map_team(self, t: str) -> str:
        if pd.isna(t):
            return ""
        return self.TEAM_MAP.get(str(t).upper(), str(t).upper())

    def map_pos(self, p: str) -> str:
        if pd.isna(p):
            return ""
        return self.POSITION_MAP.get(str(p).upper(), str(p).upper())

    @staticmethod
    def _col(df: pd.DataFrame, name: str) -> pd.Series:
        if name in df.columns:
            return pd.to_numeric(df[name], errors="coerce").fillna(0.0)
        return pd.Series(0.0, index=df.index)

    def compute_dst_points_from_proj(self, df: pd.DataFrame) -> pd.Series:
        # reuse the same logic as actuals where possible
        return self.compute_dst_points_from_actual(df)

    def compute_dst_points_from_actual(self, df: pd.DataFrame) -> pd.Series:
        # Prefer explicit fantasy points column if present
        if "fantasy_points_dst" in df.columns:
            return pd.to_numeric(
                df["fantasy_points_dst"], errors="coerce"
            ).fillna(0.0)

        # find points allowed column
        pa_col_candidates = [
            "pa",
            "points_allowed",
            "points_allowed_def",
            "pts_allowed",
            "points",
        ]
        pa_col = None
        for c in pa_col_candidates:
            if c in df.columns:
                pa_col = c
                break

        pa = (
            pd.to_numeric(df[pa_col], errors="coerce").fillna(0.0)
            if pa_col
            else pd.Series(0.0, index=df.index)
        )

        # base scoring by points allowed
        pa_pts = pd.Series(0.0, index=pa.index)
        pa_pts = pa_pts.mask(pa == 0, 10.0)
        pa_pts = pa_pts.mask((pa >= 1) & (pa <= 6), 7.0)
        pa_pts = pa_pts.mask((pa >= 7) & (pa <= 13), 4.0)
        pa_pts = pa_pts.mask((pa >= 14) & (pa <= 20), 1.0)
        pa_pts = pa_pts.mask((pa >= 21) & (pa <= 27), 0.0)
        pa_pts = pa_pts.mask((pa >= 28) & (pa <= 34), -1.0)
        pa_pts = pa_pts.mask(pa >= 35, -4.0)

        # add bonuses: sacks, turnovers, defensive TDs
        def _num_from_cols(cols, default=0.0):
            for c in cols:
                if c in df.columns:
                    return pd.to_numeric(df[c], errors="coerce").fillna(0.0)
            return pd.Series(default, index=df.index)

        sacks = _num_from_cols(["def_sacks", "sacks"], 0.0)
        ints = _num_from_cols(["def_interceptions", "interceptions"], 0.0)
        fforced = _num_from_cols(
            ["def_fumbles_forced", "fumbles_forced", "fumbles"], 0.0
        )
        tds = _num_from_cols(["def_tds", "return_tds", "tds"], 0.0)

        bonus = sacks * 1.0 + ints * 2.0 + fforced * 2.0 + tds * 6.0
        return pa_pts + bonus

    def compute_k_points_from_actual(self, df: pd.DataFrame) -> pd.Series:
        # prefer explicit fantasy points column
        if "fantasy_points_k" in df.columns:
            return pd.to_numeric(
                df["fantasy_points_k"], errors="coerce"
            ).fillna(0.0)
        # apply same per-yard logic as projections where possible
        per_yard_factor = 0.1
        min_fg_points = 3.0

        # support bucketed actual columns (both projection-style and actual dataset names)
        bucket_cols = {
            # projection-style keys
            "fg_0019": 12.0,
            "fg_2029": 25.0,
            "fg_3039": 35.0,
            "fg_4049": 45.0,
            "fg_50": 52.0,
            # alternate/legacy keys
            "fg_0_39": 35.0,
            "fg_40_49": 45.0,
            # actual stats file keys
            "fg_made_0_19": 12.0,
            "fg_made_20_29": 25.0,
            "fg_made_30_39": 35.0,
            "fg_made_40_49": 45.0,
            "fg_made_50_59": 52.0,
            "fg_made_60_": 60.0,
        }
        present_buckets = [c for c in bucket_cols.keys() if c in df.columns]
        if present_buckets:
            # actual stats often use 'pat_made' or 'xp'
            xp = pd.to_numeric(
                df.get("pat_made", df.get("xp", 0)), errors="coerce"
            ).fillna(0.0)
            total = xp * 1.0
            for c in present_buckets:
                cnt = pd.to_numeric(df[c], errors="coerce").fillna(0.0)
                avg = bucket_cols[c]
                pts = np.maximum(min_fg_points, avg * per_yard_factor) * cnt
                total = total + pts
            return total

        if "fg_yds" in df.columns and "fgm" in df.columns:
            fgm = pd.to_numeric(df["fgm"], errors="coerce").fillna(0.0)
            fg_yds = pd.to_numeric(df["fg_yds"], errors="coerce").fillna(0.0)
            per_fg_yds = fg_yds.copy()
            per_fg_yds[fgm > 0] = fg_yds[fgm > 0] / fgm[fgm > 0]
            per_fg_pts = np.maximum(
                min_fg_points, per_fg_yds * per_yard_factor
            )
            xp = pd.to_numeric(df.get("xp", 0), errors="coerce").fillna(0.0)
            return xp * 1.0 + per_fg_pts * fgm

        if "fg_yds" in df.columns:
            fg_yds = pd.to_numeric(df["fg_yds"], errors="coerce").fillna(0.0)
            xp = pd.to_numeric(df.get("xp", 0), errors="coerce").fillna(0.0)
            return (
                np.maximum(min_fg_points, fg_yds * per_yard_factor) + xp * 1.0
            )

        if "fgm" in df.columns:
            fgm = pd.to_numeric(df["fgm"], errors="coerce").fillna(0.0)
            xp = pd.to_numeric(df.get("xp", 0), errors="coerce").fillna(0.0)
            return xp * 1.0 + fgm * min_fg_points

        return self.compute_k_points_from_proj(df)

    def compute_ppr_points_from_proj(self, df: pd.DataFrame) -> pd.Series:
        # Standard PPR scoring: passing 0.04/yd, pass TD 4, INT -2,
        # rushing 0.1/yd, rush TD 6, reception 1, rec yd 0.1, rec TD 6,
        # fumbles lost -2, two-pt convs 2
        pass_yds = pd.to_numeric(
            df.get("pass_yds", df.get("pass_yd", 0)), errors="coerce"
        ).fillna(0.0)
        pass_tds = pd.to_numeric(
            df.get("pass_tds", df.get("pass_td", 0)), errors="coerce"
        ).fillna(0.0)
        pass_int = pd.to_numeric(
            df.get("pass_int", df.get("pass_ints", df.get("int", 0))),
            errors="coerce",
        ).fillna(0.0)
        rush_yds = pd.to_numeric(
            df.get("rush_yds", df.get("rush_yd", 0)), errors="coerce"
        ).fillna(0.0)
        rush_tds = pd.to_numeric(
            df.get("rush_tds", df.get("rush_td", 0)), errors="coerce"
        ).fillna(0.0)
        rec = pd.to_numeric(
            df.get("rec", df.get("receptions", 0)), errors="coerce"
        ).fillna(0.0)
        rec_yds = pd.to_numeric(
            df.get("rec_yds", df.get("receiving_yds", 0)), errors="coerce"
        ).fillna(0.0)
        rec_tds = pd.to_numeric(
            df.get("rec_tds", df.get("receiving_td", 0)), errors="coerce"
        ).fillna(0.0)
        fumbles = pd.to_numeric(
            df.get("fumbles_lost", df.get("fumbles", 0)), errors="coerce"
        ).fillna(0.0)
        two_pts = pd.to_numeric(df.get("two_pts", 0), errors="coerce").fillna(
            0.0
        )

        pts = (
            pass_yds * 0.04
            + pass_tds * 4.0
            + pass_int * -2.0
            + rush_yds * 0.1
            + rush_tds * 6.0
            + rec * 1.0
            + rec_yds * 0.1
            + rec_tds * 6.0
            + fumbles * -2.0
            + two_pts * 2.0
        )
        return pts

    def compute_idp_points_from_proj(self, df: pd.DataFrame) -> pd.Series:
        # Simple IDP projection scoring mapping for projections that use idp_* prefixes
        solo = pd.to_numeric(df.get("idp_solo", 0), errors="coerce").fillna(
            0.0
        )
        sack = pd.to_numeric(
            df.get("idp_sack", df.get("idp_sacks", 0)), errors="coerce"
        ).fillna(0.0)
        inte = pd.to_numeric(df.get("idp_int", 0), errors="coerce").fillna(0.0)
        pd_c = pd.to_numeric(df.get("idp_pd", 0), errors="coerce").fillna(0.0)
        td = pd.to_numeric(df.get("idp_td", 0), errors="coerce").fillna(0.0)

        pts = solo * 1.0 + sack * 2.0 + inte * 2.0 + pd_c * 1.0 + td * 6.0
        return pts

    def compute_k_points_from_proj(self, df: pd.DataFrame) -> pd.Series:
        # Reuse the actual-side logic where possible; projection columns often mirror actuals
        # If projection dataframe contains fantasy_points_k, prefer it
        if "fantasy_points_k" in df.columns:
            return pd.to_numeric(
                df["fantasy_points_k"], errors="coerce"
            ).fillna(0.0)

        # check for bucket-style projection keys
        bucket_cols = {
            "fg_0019": 12.0,
            "fg_2029": 25.0,
            "fg_3039": 35.0,
            "fg_4049": 45.0,
            "fg_50": 52.0,
            "fg_made_0_19": 12.0,
            "fg_made_20_29": 25.0,
            "fg_made_30_39": 35.0,
            "fg_made_40_49": 45.0,
            "fg_made_50_59": 52.0,
        }
        present = [c for c in bucket_cols.keys() if c in df.columns]
        per_yard_factor = 0.1
        min_fg_points = 3.0
        if present:
            xp = pd.to_numeric(
                df.get("pat_made", df.get("xp", 0)), errors="coerce"
            ).fillna(0.0)
            total = xp * 1.0
            for c in present:
                cnt = pd.to_numeric(df[c], errors="coerce").fillna(0.0)
                avg = bucket_cols[c]
                pts = np.maximum(min_fg_points, avg * per_yard_factor) * cnt
                total = total + pts
            return total

        if "fgm" in df.columns and "fg_yds" in df.columns:
            fgm = pd.to_numeric(df.get("fgm", 0), errors="coerce").fillna(0.0)
            fg_yds = pd.to_numeric(
                df.get("fg_yds", 0), errors="coerce"
            ).fillna(0.0)
            per_fg_yds = fg_yds.copy()
            per_fg_yds[fgm > 0] = fg_yds[fgm > 0] / fgm[fgm > 0]
            per_fg_pts = np.maximum(
                min_fg_points, per_fg_yds * per_yard_factor
            )
            xp = pd.to_numeric(df.get("xp", 0), errors="coerce").fillna(0.0)
            return xp * 1.0 + per_fg_pts * fgm

        if "fgm" in df.columns:
            fgm = pd.to_numeric(df.get("fgm", 0), errors="coerce").fillna(0.0)
            xp = pd.to_numeric(df.get("xp", 0), errors="coerce").fillna(0.0)
            return xp * 1.0 + fgm * min_fg_points

        return pd.Series(0.0, index=df.index)

    def compute_idp123_points_from_actual(self, df: pd.DataFrame) -> pd.Series:
        # Map actual defensive stat columns to points
        solo = pd.to_numeric(
            df.get("def_tackles_solo", df.get("tackles_solo", 0)),
            errors="coerce",
        ).fillna(0.0)
        assists = pd.to_numeric(
            df.get("def_tackle_assists", df.get("tackle_assists", 0)),
            errors="coerce",
        ).fillna(0.0)
        tfl = pd.to_numeric(
            df.get("def_tackles_for_loss", 0), errors="coerce"
        ).fillna(0.0)
        sacks = pd.to_numeric(
            df.get("def_sacks", df.get("sacks", 0)), errors="coerce"
        ).fillna(0.0)
        ints = pd.to_numeric(
            df.get("def_interceptions", df.get("interceptions", 0)),
            errors="coerce",
        ).fillna(0.0)
        pd_c = pd.to_numeric(
            df.get("def_pass_defended", df.get("pass_defended", 0)),
            errors="coerce",
        ).fillna(0.0)
        fforced = pd.to_numeric(
            df.get("def_fumbles_forced", df.get("fumbles_forced", 0)),
            errors="coerce",
        ).fillna(0.0)
        fumbles = pd.to_numeric(
            df.get("def_fumbles", df.get("fumbles", 0)), errors="coerce"
        ).fillna(0.0)
        tds = pd.to_numeric(df.get("def_tds", 0), errors="coerce").fillna(0.0)

        # scoring heuristic
        pts = (
            solo * 1.0
            + assists * 0.5
            + tfl * 1.0
            + sacks * 2.0
            + ints * 2.0
            + pd_c * 1.0
            + fforced * 2.0
            + fumbles * 2.0
            + tds * 6.0
        )
        return pts

    @property
    def pos_proj_min_d(self) -> Dict[str, float]:
        return {
            "QB": 12.0,
            "RB": 5.0,
            "WR": 7.0,
            "TE": 4.0,
            "K": 4.0,
            "DST": 1.0,
            "DL": 1.0,
            "LB": 3.0,
            "DB": 3.0,
        }

    # ----------------------------
    # Loading
    # ----------------------------
    def load_projection_files(
        self, proj_dir: Path, *, apply_filters: bool = True
    ) -> pd.DataFrame:
        frames: List[pd.DataFrame] = []

        # Prefer files named like `raw_stats_{season}_wk{n}.csv` (hard-coded pattern).
        pattern = f"raw_stats_{self.season}_wk*.csv"
        files = sorted(proj_dir.glob(pattern))
        # fallback to any CSVs if none match the preferred pattern
        if not files:
            files = sorted(proj_dir.glob("*.csv"))

        for f in self._progress(files, desc="proj files", leave=False):
            d = pd.read_csv(f)
            d["season"] = self.season

            # Make week inference from filename authoritative when possible.
            # If we can't infer week from filename, fall back to a week column if present.
            inferred_week = self._infer_week(f)
            if inferred_week is not None:
                d["week"] = inferred_week
            elif "week" in d.columns and not d["week"].isna().all():
                d["week"] = d["week"]
            else:
                d["week"] = np.nan

            # coerce non-numeric to NaN; only default to week 1 if truly unknown
            d["week"] = pd.to_numeric(d["week"], errors="coerce")
            # Do not silently coerce unknown weeks to 1; that can create a week-1 skew.
            # If we can't infer a week for a row, it can't be matched reliably.
            d = d.dropna(subset=["week"]).copy()
            if d.empty:
                continue
            d["week"] = d["week"].astype(int)

            d["team"] = d["team"].map(self.map_team)
            d["pos"] = d["position"].map(self.map_pos)
            # normalize display casing for consistency (ALL CAPS) and remove punctuation
            d["player"] = (
                d["player"]
                .astype(str)
                .str.replace(r"[\.,]", "", regex=True)
                .str.strip()
                .str.upper()
            )
            d["name_key"] = d["player"].map(self.normalize_name)

            # also store first+last token form to improve name matching
            def _first_last(n: str) -> str:
                if pd.isna(n) or str(n).strip() == "":
                    return ""
                parts = str(n).split()
                if len(parts) == 1:
                    return parts[0]
                return f"{parts[0]} {parts[-1]}"

            d["name_first_last"] = d["name_key"].map(_first_last)

            # DST/DEF matching is often by team, not by a conventional player name.
            # Normalize DST names to the mapped team code on both sides.
            try:
                dst_mask = d["pos"] == "DST"
                if dst_mask.any():
                    d.loc[dst_mask, "name_key"] = (
                        d.loc[dst_mask, "team"].astype(str).str.lower()
                    )
                    d.loc[dst_mask, "name_first_last"] = d.loc[
                        dst_mask, "name_key"
                    ]
            except Exception:
                pass

            # detect missing IDP projection columns for diagnostics
            if d["pos"].isin(["DL", "LB", "DB"]).any():
                required = {
                    "idp_solo",
                    "idp_sack",
                    "idp_int",
                    "idp_pd",
                    "idp_td",
                }
                missing = required - set(d.columns)
                if missing:
                    self._missing_proj_cols.update(missing)

            # compute projected fantasy points by position
            if d["pos"].isin(["QB", "RB", "WR", "TE"]).any():
                d_proj = d[d["pos"].isin(["QB", "RB", "WR", "TE"])].copy()
                d.loc[d_proj.index, "proj_pts"] = (
                    self.compute_ppr_points_from_proj(d_proj)
                )
            if d["pos"].isin(["DL", "LB", "DB"]).any():
                d_idp = d[d["pos"].isin(["DL", "LB", "DB"])].copy()
                d.loc[d_idp.index, "proj_pts"] = (
                    self.compute_idp_points_from_proj(d_idp)
                )
            if d["pos"].isin(["K"]).any():
                d_k = d[d["pos"] == "K"].copy()
                d.loc[d_k.index, "proj_pts"] = self.compute_k_points_from_proj(
                    d_k
                )
                # If kicker projections all NaN, try relaxed fallbacks and record columns
                if d.loc[d_k.index, "proj_pts"].isna().all():
                    # record kicker-related columns seen
                    for c in d_k.columns:
                        if re.search(
                            r"fg|xp|kick|kic|fantasy|fga|fgm|fg_", c, re.I
                        ):
                            self._kicker_proj_columns_seen.add(c)

                    # fallback: prefer any 'fantasy' column
                    cand = [
                        c
                        for c in d_k.columns
                        if re.search(r"fantasy", c, re.I)
                    ]
                    if cand:
                        col = cand[0]
                        d.loc[d_k.index, "proj_pts"] = pd.to_numeric(
                            d_k[col], errors="coerce"
                        )
                        # we only set proj_pts here; actual columns are added
                        # later when matches are formed in `fuzzy_match_group`.
                    else:
                        # fallback simple heuristic: use fgm and xp if present
                        if "fgm" in d_k.columns:
                            d.loc[d_k.index, "proj_pts"] = (
                                pd.to_numeric(
                                    d_k.get("fgm", 0), errors="coerce"
                                ).fillna(0.0)
                                * 3.0
                                + pd.to_numeric(
                                    d_k.get("xp", 0), errors="coerce"
                                ).fillna(0.0)
                                * 1.0
                            )
            if d["pos"].isin(["DST"]).any():
                d_dst = d[d["pos"] == "DST"].copy()
                d.loc[d_dst.index, "proj_pts"] = (
                    self.compute_dst_points_from_proj(d_dst)
                )

            if apply_filters:
                # Filter out low-projection players (ignore those below threshold)
                # NOTE: This is an analysis filter. The master matched dataset can be
                # built without these filters so you can reuse it later.
                # treat NaN as 0 for filtering purposes
                try:
                    min_pts = (
                        d["pos"]
                        .map(self.pos_proj_min_d)
                        .fillna(self.min_proj_points)
                    )
                    keep_mask = d["proj_pts"].fillna(0.0) >= min_pts
                    d_out = d[keep_mask].copy()
                except Exception:
                    d_out = d[
                        d["proj_pts"].fillna(0.0) >= self.min_proj_points
                    ].copy()
            else:
                # No filtering: keep all rows so the cached matched dataset can be
                # reused for many downstream analyses.
                d_out = d.copy()

            # keep all projection columns for downstream per-stat analysis
            frames.append(d_out.copy())

        # If filtering removed all projections, return an empty frame with expected columns
        if not frames:
            return pd.DataFrame(
                columns=[
                    "season",
                    "week",
                    "team",
                    "pos",
                    "player",
                    "name_key",
                    "proj_pts",
                ]
            )

        return pd.concat(frames, ignore_index=True)

    def _analysis_filter_matched(self, matched: pd.DataFrame) -> pd.DataFrame:
        """Apply projection-based filters to a *matched* dataset.

        This is intentionally separated from the master match build step so the
        cached master can remain unfiltered.
        """
        if matched.empty:
            return matched

        if "proj_pts" not in matched.columns or "pos" not in matched.columns:
            return matched

        per_pos_min = {
            "QB": 12.0,
            "RB": 5.0,
            "WR": 7.0,
            "TE": 4.0,
            "K": 4.0,
            "DST": 1.0,
            "DL": 1.0,
            "LB": 3.0,
            "DB": 3.0,
        }
        min_pts = matched["pos"].map(per_pos_min).fillna(self.min_proj_points)
        keep = pd.to_numeric(matched["proj_pts"], errors="coerce").fillna(0.0)
        return matched.loc[keep >= min_pts].copy()

    def _add_realized_stat_aliases(
        self, matched: pd.DataFrame
    ) -> pd.DataFrame:
        """Add realized stat columns named like the projection stat bases.

        Example:
        - If projections have `proj_rush_yds` and actuals have `act_rushing_yards`,
          create `rush_yds` = `act_rushing_yards`.

        This makes the cached file easy to use without remembering act_* names.
        """
        if matched.empty:
            return matched

        out = matched.copy()

        # canonical player + position aliases
        if "player" not in out.columns:
            proj_name = out.get("player_proj")
            act_name = out.get("player_act")
            if act_name is not None and proj_name is not None:
                out["player"] = act_name.fillna(proj_name)
            elif act_name is not None:
                out["player"] = act_name
            elif proj_name is not None:
                out["player"] = proj_name

        if "position" not in out.columns and "pos" in out.columns:
            out["position"] = out["pos"]

        # realized stat aliases
        for proj_base, act_base in self.STAT_COLUMN_MAPPING.items():
            act_col = f"act_{act_base}"
            if act_col in out.columns and proj_base not in out.columns:
                out[proj_base] = out[act_col]

        return out

    def build_or_load_master_matched(
        self,
        *,
        proj_dir: Path,
        player_stats_path: Path,
        out_dir: Path,
        force_rebuild: bool = False,
        filename: str = "matched_master.csv",
    ) -> pd.DataFrame:
        """Create/load a cached, unfiltered matched dataset.

        The output file contains one row per matched (season, week, team, pos, player)
        plus all `proj_*` and `act_*` columns and realized stat aliases.
        """
        out_dir.mkdir(parents=True, exist_ok=True)
        cache_path = out_dir / filename

        if cache_path.exists() and not force_rebuild:
            try:
                return pd.read_csv(cache_path)
            except Exception:
                # If cache is corrupt/unreadable, fall back to rebuilding.
                pass

        # Build matches (unfiltered projections)
        proj = self.load_projection_files(proj_dir, apply_filters=False)
        act = self.load_actuals(player_stats_path)

        merged_parts: List[pd.DataFrame] = []
        gb = proj.groupby(["season", "week", "team", "pos"])
        for key, pg in self._progress(
            gb,
            total=getattr(gb, "ngroups", None),
            desc="matching groups",
            leave=True,
        ):
            s, w, t, p = key
            ag = act[
                (act["season"] == s)
                & (act["week"] == w)
                & (act["team"] == t)
                & (act["pos"] == p)
            ]
            merged_parts.append(self.fuzzy_match_group(pg, ag))

        merged = pd.concat(
            [m for m in merged_parts if not m.empty], ignore_index=True
        )
        merged = self._add_realized_stat_aliases(merged)

        try:
            merged.to_csv(cache_path, index=False)
        except Exception:
            pass

        return merged

    def load_actuals(self, player_stats_path: Path) -> pd.DataFrame:
        a = pd.read_csv(player_stats_path)
        a = a[a["season"] == self.season].copy()

        a["team"] = a["team"].map(self.map_team)
        a["pos"] = a["position"].map(self.map_pos)
        # standardize display name casing to ALL CAPS and remove punctuation
        a["player_display_name"] = (
            a["player_display_name"]
            .astype(str)
            .str.replace(r"[\.,]", "", regex=True)
            .str.strip()
            .str.upper()
        )
        a["name_key"] = a["player_display_name"].map(self.normalize_name)

        # also add first+last token version for matching robustness
        def _first_last(n: str) -> str:
            if pd.isna(n) or str(n).strip() == "":
                return ""
            parts = str(n).split()
            if len(parts) == 1:
                return parts[0]
            return f"{parts[0]} {parts[-1]}"

        a["name_first_last"] = a["name_key"].map(_first_last)

        # DST matching should be done by team code.
        try:
            dst_mask = a["pos"] == "DST"
            if dst_mask.any():
                a.loc[dst_mask, "name_key"] = (
                    a.loc[dst_mask, "team"].astype(str).str.lower()
                )
                a.loc[dst_mask, "name_first_last"] = a.loc[
                    dst_mask, "name_key"
                ]
        except Exception:
            pass

        # compute actual fantasy points by position
        a["actual_pts"] = np.nan

        off_mask = a["pos"].isin(["QB", "RB", "WR", "TE"])
        if off_mask.any():
            a.loc[off_mask, "actual_pts"] = pd.to_numeric(
                a.loc[off_mask].get("fantasy_points_ppr", 0), errors="coerce"
            ).fillna(0.0)

        idp_mask = a["pos"].isin(["DL", "LB", "DB"])
        if idp_mask.any():
            a.loc[idp_mask, "actual_pts"] = (
                self.compute_idp123_points_from_actual(a.loc[idp_mask])
            )

        k_mask = a["pos"] == "K"
        if k_mask.any():
            a.loc[k_mask, "actual_pts"] = self.compute_k_points_from_actual(
                a.loc[k_mask]
            )
            # if kicker actuals are NaN, try alternative columns and record columns seen
            if a.loc[k_mask, "actual_pts"].isna().all():
                a_k = a.loc[k_mask]
                for c in a_k.columns:
                    if re.search(
                        r"fg|xp|kick|kic|fantasy|fga|fgm|fg_", c, re.I
                    ):
                        self._kicker_actual_columns_seen.add(c)

                cand = [
                    c for c in a_k.columns if re.search(r"fantasy", c, re.I)
                ]
                if cand:
                    col = cand[0]
                    a.loc[k_mask, "actual_pts"] = pd.to_numeric(
                        a_k[col], errors="coerce"
                    )
                else:
                    if "fgm" in a_k.columns:
                        a.loc[k_mask, "actual_pts"] = (
                            pd.to_numeric(
                                a_k.get("fgm", 0), errors="coerce"
                            ).fillna(0.0)
                            * 3.0
                            + pd.to_numeric(
                                a_k.get("xp", 0), errors="coerce"
                            ).fillna(0.0)
                            * 1.0
                        )

        dst_mask = a["pos"] == "DST"
        if dst_mask.any():
            a.loc[dst_mask, "actual_pts"] = (
                self.compute_dst_points_from_actual(a.loc[dst_mask])
            )

        return a.dropna(subset=["actual_pts"])

    # ----------------------------
    # Fuzzy matching
    # ----------------------------
    def fuzzy_match_group(
        self,
        proj_g: pd.DataFrame,
        act_g: pd.DataFrame,
    ) -> pd.DataFrame:
        # if either side is empty, record dropped rows for diagnostics
        if proj_g.empty or act_g.empty:
            if not proj_g.empty:
                for _, prow in proj_g.iterrows():
                    self._dropped_projections.append(
                        {
                            "season": prow["season"],
                            "week": prow["week"],
                            "team": prow["team"],
                            "pos": prow["pos"],
                            "player_proj": prow["player"],
                            "name_key": prow["name_key"],
                        }
                    )
            if not act_g.empty:
                for _, arow in act_g.iterrows():
                    self._dropped_actuals.append(
                        {
                            "season": arow["season"],
                            "week": arow["week"],
                            "team": arow["team"],
                            "pos": arow["pos"],
                            "player_act": arow["player_display_name"],
                            "name_key": arow["name_key"],
                        }
                    )
            return pd.DataFrame()

        # build candidate pairs with a two-pass strategy:
        # 1) prefer matches where mapped `team` is equal (and same pos/grouping)
        # 2) if no same-team candidates found, allow team-agnostic matches
        candidates: List[tuple] = []  # (score, proj_idx, act_idx)

        # helper to compute name score using first+last when possible
        def _name_score(pname: str, aname: str) -> int:
            try:
                return int(fuzz.partial_ratio(pname, aname))
            except Exception:
                return 0

        # First: do exact same-team name matches (first+last token then fallback to
        # full normalized name). This will be fast for large datasets and ensures
        # we only match players on the same team.
        used_p = set()
        used_a = set()
        rows = []

        # helper to fetch name tokens
        def _get_names(row):
            return (
                row.get("name_first_last", "") or row.get("name_key", ""),
                row.get("name_key", ""),
            )

        # build index of actuals by (team, name_first_last) and (team, name_key)
        act_index_by_team_name = {}
        for a_idx, arow in act_g.iterrows():
            team = arow.get("team")
            n1 = arow.get("name_first_last", "")
            n2 = arow.get("name_key", "")
            if team is None:
                continue
            act_index_by_team_name.setdefault((team, n1), []).append(a_idx)
            act_index_by_team_name.setdefault((team, n2), []).append(a_idx)

        # exact-match pass
        for p_idx, prow in proj_g.iterrows():
            pname1, pname2 = _get_names(prow)
            team = prow.get("team")
            if team is None:
                continue
            cand_list = act_index_by_team_name.get((team, pname1), [])
            if not cand_list and pname2 != pname1:
                cand_list = act_index_by_team_name.get((team, pname2), [])
            if cand_list:
                # pick the first unmatched actual from the list
                chosen = None
                for a_idx in cand_list:
                    if a_idx not in used_a:
                        chosen = a_idx
                        break
                if chosen is not None:
                    arow = act_g.loc[chosen]
                    used_p.add(p_idx)
                    used_a.add(chosen)
                    # create matched row (score 100 for exact)
                    rows.append(
                        {
                            "season": prow["season"],
                            "week": prow["week"],
                            "team": prow["team"],
                            "pos": prow["pos"],
                            "player_proj": prow.get("player"),
                            "player_act": arow.get("player_display_name"),
                            "match_score": 100,
                            "proj_pts": prow.get("proj_pts"),
                            "actual_pts": arow.get("actual_pts"),
                        }
                    )
                    # copy projection columns
                    reserved = {
                        "season",
                        "week",
                        "team",
                        "pos",
                        "player",
                        "name_key",
                        "proj_pts",
                    }
                    for c in proj_g.columns:
                        if c in reserved:
                            continue
                        try:
                            rows[-1][f"proj_{c}"] = prow[c]
                        except Exception:
                            rows[-1][f"proj_{c}"] = None

                    # copy actual columns
                    reserved_act = {
                        "season",
                        "week",
                        "team",
                        "pos",
                        "player_display_name",
                        "name_key",
                        "actual_pts",
                    }
                    for c in act_g.columns:
                        if c in reserved_act:
                            continue
                        try:
                            rows[-1][f"act_{c}"] = arow.get(c, None)
                        except Exception:
                            rows[-1][f"act_{c}"] = None

                    self._match_scores.append(
                        {
                            "season": prow["season"],
                            "week": prow["week"],
                            "team": prow["team"],
                            "pos": prow["pos"],
                            "player_proj": prow.get("player"),
                            "player_act": arow.get("player_display_name"),
                            "match_score": 100,
                        }
                    )

        # Fuzzy pass only among unmatched rows within the same team.
        # Avoid per-group progress bars here; the outer loop already reports progress.
        candidates = []
        for p_idx, prow in proj_g.iterrows():
            if p_idx in used_p:
                continue
            for a_idx, arow in act_g.iterrows():
                if a_idx in used_a:
                    continue
                if prow.get("team") != arow.get("team"):
                    continue
                pname = prow.get("name_first_last", prow.get("name_key", ""))
                aname = arow.get("name_first_last", arow.get("name_key", ""))
                score = _name_score(pname, aname)
                if score >= self.min_score:
                    candidates.append((score, p_idx, a_idx))

        # sort fuzzy candidates and greedily assign
        candidates.sort(reverse=True, key=lambda x: x[0])
        for score, p_idx, a_idx in candidates:
            if p_idx in used_p or a_idx in used_a:
                continue
            prow = proj_g.loc[p_idx]
            arow = act_g.loc[a_idx]
            used_p.add(p_idx)
            used_a.add(a_idx)
            rows.append(
                {
                    "season": prow["season"],
                    "week": prow["week"],
                    "team": prow["team"],
                    "pos": prow["pos"],
                    "player_proj": prow.get("player"),
                    "player_act": arow.get("player_display_name"),
                    "match_score": score,
                    "proj_pts": prow.get("proj_pts"),
                    "actual_pts": arow.get("actual_pts"),
                }
            )
            # copy projection columns
            reserved = {
                "season",
                "week",
                "team",
                "pos",
                "player",
                "name_key",
                "proj_pts",
            }
            for c in proj_g.columns:
                if c in reserved:
                    continue
                try:
                    rows[-1][f"proj_{c}"] = prow[c]
                except Exception:
                    rows[-1][f"proj_{c}"] = None

            # copy actual columns
            reserved_act = {
                "season",
                "week",
                "team",
                "pos",
                "player_display_name",
                "name_key",
                "actual_pts",
            }
            for c in act_g.columns:
                if c in reserved_act:
                    continue
                try:
                    rows[-1][f"act_{c}"] = arow.get(c, None)
                except Exception:
                    rows[-1][f"act_{c}"] = None

            self._match_scores.append(
                {
                    "season": prow["season"],
                    "week": prow["week"],
                    "team": prow["team"],
                    "pos": prow["pos"],
                    "player_proj": prow.get("player"),
                    "player_act": arow.get("player_display_name"),
                    "match_score": score,
                }
            )

        # (End of fuzzy matching augmentation)

        # any unmatched projections/actuals are considered dropped for diagnostics
        for p_idx, prow in proj_g.iterrows():
            if p_idx not in used_p:
                self._dropped_projections.append(
                    {
                        "season": prow["season"],
                        "week": prow["week"],
                        "team": prow["team"],
                        "pos": prow["pos"],
                        "player_proj": prow["player"],
                        "name_key": prow["name_key"],
                    }
                )

        for a_idx, arow in act_g.iterrows():
            if a_idx not in used_a:
                self._dropped_actuals.append(
                    {
                        "season": arow["season"],
                        "week": arow["week"],
                        "team": arow["team"],
                        "pos": arow["pos"],
                        "player_act": arow["player_display_name"],
                        "name_key": arow["name_key"],
                    }
                )

        return pd.DataFrame(rows)

    # ----------------------------
    # Regression / outliers
    # ----------------------------
    @staticmethod
    def regression_metrics(df: pd.DataFrame) -> dict:
        n = int(len(df))
        if n < 2:
            return {
                "n": n,
                "intercept": np.nan,
                "slope": np.nan,
                "r2": np.nan,
                "rmse": np.nan,
                "mae": np.nan,
                "corr": np.nan,
            }

        x = pd.to_numeric(df["proj_pts"], errors="coerce").astype(float)
        y = pd.to_numeric(df["actual_pts"], errors="coerce").astype(float)

        # drop non-finite rows before regression to avoid divide-by-zero/runtime warnings
        finite_mask = np.isfinite(x) & np.isfinite(y)
        x = x[finite_mask]
        y = y[finite_mask]
        n = int(len(x))
        if n < 2:
            return {
                "n": n,
                "intercept": np.nan,
                "slope": np.nan,
                "r2": np.nan,
                "rmse": np.nan,
                "mae": np.nan,
                "corr": np.nan,
            }

        # if either series is constant, correlation and r2 are not defined
        if x.nunique() <= 1 or y.nunique() <= 1:
            mu_y = float(np.nanmean(y))
            resid = y - mu_y
            return {
                "n": n,
                "intercept": mu_y,
                "slope": np.nan,
                "r2": np.nan,
                "rmse": float(np.sqrt(np.mean(resid**2))) if n > 0 else np.nan,
                "mae": float(np.mean(np.abs(resid))) if n > 0 else np.nan,
                "corr": np.nan,
            }

        try:
            X = sm.add_constant(x)
            # check design matrix rank to avoid singularities
            try:
                rank = np.linalg.matrix_rank(X.values)
            except Exception:
                rank = None
            if rank is None or rank <= 1:
                # cannot fit a meaningful linear model
                mu_y = float(np.nanmean(y))
                resid = y - mu_y
                return {
                    "n": n,
                    "intercept": mu_y,
                    "slope": np.nan,
                    "r2": np.nan,
                    "rmse": float(np.sqrt(np.nanmean(resid**2)))
                    if n > 0
                    else np.nan,
                    "mae": float(np.nanmean(np.abs(resid)))
                    if n > 0
                    else np.nan,
                    "corr": np.nan,
                }

            model = sm.OLS(y, X).fit()
            resid = y - model.predict(X)
            # compute SSR and centered TSS to derive R^2 as 1 - SSR / TSS
            try:
                ssr = float(np.nansum((resid) ** 2))
                centered_tss = float(
                    np.nansum((y - float(np.nanmean(y))) ** 2)
                )
                if centered_tss > 0:
                    r2_val = 1.0 - ssr / centered_tss
                else:
                    r2_val = np.nan
            except Exception:
                r2_val = np.nan

            # safe correlation
            std_x = float(np.nanstd(x))
            std_y = float(np.nanstd(y))
            corr = (
                float(np.corrcoef(x, y)[0, 1])
                if std_x > 0 and std_y > 0
                else np.nan
            )

            return {
                "n": n,
                "intercept": float(model.params.get("const", np.nan)),
                "slope": float(model.params.get("proj_pts", np.nan))
                if "proj_pts" in model.params.index
                else (
                    float(model.params.iloc[1])
                    if len(model.params) > 1
                    else np.nan
                ),
                "r2": float(r2_val) if np.isfinite(r2_val) else np.nan,
                "rmse": float(np.sqrt(np.nanmean(resid**2))),
                "mae": float(np.nanmean(np.abs(resid))),
                "corr": corr,
            }
        except Exception:
            return {
                "n": n,
                "intercept": np.nan,
                "slope": np.nan,
                "r2": np.nan,
                "rmse": np.nan,
                "mae": np.nan,
                "corr": np.nan,
            }

    @staticmethod
    def remove_outliers_mad(df: pd.DataFrame, z: float = 4.0) -> pd.DataFrame:
        # Need at least 3 points to meaningfully detect outliers
        if len(df) < 3:
            return df.copy()

        try:
            x = pd.to_numeric(df["proj_pts"], errors="coerce").astype(float)
            y = pd.to_numeric(df["actual_pts"], errors="coerce").astype(float)
            finite_mask = np.isfinite(x) & np.isfinite(y)
            x = x[finite_mask]
            y = y[finite_mask]
            if len(x) < 3:
                return df.copy()

            X = sm.add_constant(x)
            try:
                rank = np.linalg.matrix_rank(X.values)
            except Exception:
                rank = None
            if rank is None or rank <= 1:
                return df.copy()

            model = sm.OLS(y, X).fit()
            resid = y - model.predict(X)
            med = np.median(resid)
            mad = np.median(np.abs(resid - med))
            if mad == 0:
                return df.copy()
            zscore = 0.6745 * (resid - med) / mad
            # map zscore back to original index
            filtered_index = x.index[np.abs(zscore) <= z]
            return df.loc[filtered_index].copy()
        except Exception:
            return df.copy()

    # ----------------------------
    # Orchestration
    # ----------------------------
    def run(self, force_rebuild: bool = False) -> None:
        self.out_dir.mkdir(parents=True, exist_ok=True)

        # 1) Build or load the master matched dataset (unfiltered).
        master = self.build_or_load_master_matched(
            proj_dir=self.proj_dir,
            player_stats_path=self.player_stats_path,
            out_dir=self.out_dir,
            force_rebuild=force_rebuild,
            filename="matched_master.csv",
        )

        # 2) Apply projection filters for the downstream analysis outputs.
        merged = self._analysis_filter_matched(master)

        # Also write the analysis subset for convenience.
        try:
            merged.to_csv(self.out_dir / "matched_rows.csv", index=False)
        except Exception:
            pass
        summary_rows = []
        for pos, g in merged.groupby("pos"):
            base = self.regression_metrics(g)
            g2 = self.remove_outliers_mad(g)
            clean = self.regression_metrics(g2)
            summary_rows.append(
                {
                    "pos": pos,
                    "min_score": self.min_score,
                    "n_raw": base["n"],
                    "r2_raw": base["r2"],
                    "slope_raw": base["slope"],
                    "rmse_raw": base["rmse"],
                    "mae_raw": base["mae"],
                    "n_clean": clean["n"],
                    "r2_clean": clean["r2"],
                    "slope_clean": clean["slope"],
                    "rmse_clean": clean["rmse"],
                    "mae_clean": clean["mae"],
                }
            )

        # ensure common positions are present in the summary (include K and DST)
        expected_positions = [
            "QB",
            "RB",
            "WR",
            "TE",
            "K",
            "DST",
            "DL",
            "LB",
            "DB",
        ]
        summary_df = pd.DataFrame(summary_rows)
        present = (
            set(summary_df["pos"].tolist()) if not summary_df.empty else set()
        )
        for ep in expected_positions:
            if ep not in present:
                summary_df = pd.concat(
                    [
                        summary_df,
                        pd.DataFrame(
                            [
                                {
                                    "pos": ep,
                                    "min_score": self.min_score,
                                    "n_raw": 0,
                                    "r2_raw": np.nan,
                                    "slope_raw": np.nan,
                                    "rmse_raw": np.nan,
                                    "mae_raw": np.nan,
                                    "n_clean": 0,
                                    "r2_clean": np.nan,
                                    "slope_clean": np.nan,
                                    "rmse_clean": np.nan,
                                    "mae_clean": np.nan,
                                }
                            ]
                        ),
                    ],
                    ignore_index=True,
                )

        summary_df = summary_df.sort_values("pos")
        summary_df.to_csv(
            self.out_dir / "summary_by_position.csv", index=False
        )

        # --- Aggregate per-stat R^2 across all positions/players (exclude FG-related projection columns) ---
        try:
            dfp = merged.copy()
            proj_cols = [c for c in dfp.columns if c.startswith("proj_")]
            stat_cols = [c for c in proj_cols if not re.search(r"fg", c, re.I)]
            # collect available actual stat bases
            mapping_rows = []
            r2_rows = []
            for c in self._progress(
                stat_cols, desc="per-stat R2", leave=False
            ):
                base = c.replace("proj_", "")
                # use hard-coded mapping from STAT_COLUMN_MAPPING; no fuzzy matching
                mapped = self.STAT_COLUMN_MAPPING.get(base)
                score = 100 if mapped is not None else 0

                mapping_rows.append(
                    {
                        "proj_stat": base,
                        "mapped_act": mapped,
                        "match_score": score,
                    }
                )

                if mapped is None:
                    r2 = np.nan
                    n_obs = 0
                else:
                    proj_col = f"proj_{base}"
                    act_col = f"act_{mapped}"
                    x = pd.to_numeric(dfp[proj_col], errors="coerce")
                    y = pd.to_numeric(dfp[act_col], errors="coerce")
                    mask = x.notna() & y.notna()
                    n_obs = int(mask.sum())
                    if n_obs < 2 or x[mask].nunique() <= 1:
                        r2 = np.nan
                    else:
                        try:
                            X = sm.add_constant(x[mask])
                            model = sm.OLS(y[mask], X).fit()
                            resid = y[mask] - model.predict(X)
                            ssr = float(np.nansum((resid) ** 2))
                            centered_tss = float(
                                np.nansum(
                                    (y[mask] - float(np.nanmean(y[mask]))) ** 2
                                )
                            )
                            r2 = (
                                1.0 - ssr / centered_tss
                                if centered_tss > 0
                                else np.nan
                            )
                        except Exception:
                            r2 = np.nan
                r2_rows.append({"stat": base, "r2": r2, "n": n_obs})

            # write mapping diagnostics
            try:
                pd.DataFrame(mapping_rows).to_csv(
                    self.out_dir / "proj_to_act_stat_mapping.csv", index=False
                )
            except Exception:
                pass

            r2df = pd.DataFrame(r2_rows).sort_values("r2", ascending=False)
            r2df.to_csv(self.out_dir / "r2_by_stat_all.csv", index=False)

            # plot single aggregated chart
            try:
                if r2df["r2"].notna().any():
                    plt.style.use("fivethirtyeight")
                    fig, ax = plt.subplots(
                        figsize=(10, max(4, 0.25 * len(r2df)))
                    )
                    bars = ax.barh(
                        r2df["stat"], r2df["r2"], color="seagreen", alpha=0.9
                    )
                    ax.invert_yaxis()
                    ax.set_xlabel("R²")
                    ax.set_title(
                        "R² by Projected Stat (all positions & players)"
                    )
                    try:
                        max_r2 = float(np.nanmax(r2df["r2"].to_numpy()))
                        if not np.isfinite(max_r2):
                            max_r2 = 1.0
                    except Exception:
                        max_r2 = 1.0
                    ax.set_xlim(0, max(1.0, max_r2 * 1.1))
                    for bar in bars:
                        width = bar.get_width()
                        y = bar.get_y() + bar.get_height() / 2
                        if not (np.isfinite(width) and np.isfinite(y)):
                            continue
                        ax.text(
                            width + (max(0.02, max_r2 * 0.02)),
                            y,
                            f"{width:.2f}",
                            va="center",
                            ha="left",
                            fontsize=9,
                            fontweight="bold",
                            color="#222222",
                        )
                    plt.tight_layout()
                    fig.savefig(self.out_dir / "r2_by_stat_all.png", dpi=200)
                    plt.close(fig)
            except Exception:
                pass
        except Exception:
            pass

        # --- Scatter plots of proj_pts vs actual_pts with OLS fit lines per position ---
        try:
            sns.set_style("whitegrid")
            pos_list = sorted(merged["pos"].dropna().unique())
            if pos_list:
                # combined plot: all points faint, fit lines colored by position
                fig, ax = plt.subplots(figsize=(10, 6))
                # plot all points as faint gray dots
                ax.scatter(
                    merged["proj_pts"],
                    merged["actual_pts"],
                    color="#bbbbbb",
                    s=20,
                    alpha=0.35,
                    label="all",
                )
                palette = sns.color_palette(
                    "tab10", n_colors=max(3, len(pos_list))
                )
                for i, pos in enumerate(pos_list):
                    g = merged[merged["pos"] == pos]
                    x = pd.to_numeric(g["proj_pts"], errors="coerce")
                    y = pd.to_numeric(g["actual_pts"], errors="coerce")
                    mask = x.notna() & y.notna()
                    if mask.sum() < 2:
                        continue
                    # scatter for this position (slightly larger, translucent)
                    ax.scatter(
                        x[mask],
                        y[mask],
                        color=palette[i % len(palette)],
                        s=30,
                        alpha=0.6,
                        label=f"{pos} points",
                    )
                    # regression line using seaborn's low-level API via numpy fit
                    try:
                        X = sm.add_constant(x[mask])
                        model = sm.OLS(y[mask], X).fit()
                        xs = np.linspace(
                            float(np.nanmin(x[mask])),
                            float(np.nanmax(x[mask])),
                            50,
                        )
                        # avoid positional indexing warnings; use iloc for ordered params
                        try:
                            intercept = float(model.params.iloc[0])
                            slope = float(model.params.iloc[1])
                        except Exception:
                            intercept = float(
                                model.params.get("const", np.nan)
                            )
                            slope = (
                                float(model.params.iloc[1])
                                if len(model.params) > 1
                                else 0.0
                            )
                        ys = intercept + slope * xs
                        ax.plot(
                            xs,
                            ys,
                            color=palette[i % len(palette)],
                            linewidth=2.2,
                            label=f"{pos} fit",
                        )
                    except Exception:
                        # fallback to seaborn regplot if OLS fails
                        try:
                            sns.regplot(
                                x=x[mask],
                                y=y[mask],
                                scatter=False,
                                ax=ax,
                                color=palette[i % len(palette)],
                                ci=None,
                                line_kws={"linewidth": 2.0},
                            )
                        except Exception:
                            pass

                ax.set_xlabel("Projected points")
                ax.set_ylabel("Actual fantasy points")
                ax.set_title("Projected vs Actual Points — position fits")
                ax.legend(ncol=2, fontsize=9)
                plt.tight_layout()
                fig.savefig(
                    self.out_dir / "scatter_proj_vs_actual_by_pos.png", dpi=200
                )
                plt.close(fig)

                # Also produce individual per-position scatter+fit charts
                for i, pos in enumerate(pos_list):
                    g = merged[merged["pos"] == pos]
                    x = pd.to_numeric(g["proj_pts"], errors="coerce")
                    y = pd.to_numeric(g["actual_pts"], errors="coerce")
                    mask = x.notna() & y.notna()
                    if mask.sum() < 2:
                        continue
                    fig, ax = plt.subplots(figsize=(8, 5))
                    sns.regplot(
                        x=x[mask],
                        y=y[mask],
                        scatter_kws={"s": 40, "alpha": 0.6},
                        line_kws={"linewidth": 2.2},
                        ax=ax,
                        color=palette[i % len(palette)],
                    )
                    ax.set_xlabel("Projected points")
                    ax.set_ylabel("Actual fantasy points")
                    ax.set_title(f"Projected vs Actual Points — {pos}")
                    plt.tight_layout()
                    fig.savefig(
                        self.out_dir / f"scatter_proj_vs_actual_{pos}.png",
                        dpi=200,
                    )
                    plt.close(fig)
        except Exception:
            pass
        except Exception:
            pass

        # diagnostics: match score distributions and dropped rows
        if self._match_scores:
            ms_df = pd.DataFrame(self._match_scores)
            try:
                ms_df.groupby("pos")["match_score"].describe().to_csv(
                    self.out_dir / "match_score_distribution_by_pos.csv"
                )
            except Exception:
                ms_df.to_csv(
                    self.out_dir / "match_scores_flat.csv", index=False
                )

        pd.DataFrame(self._dropped_projections).to_csv(
            self.out_dir / "dropped_projections.csv", index=False
        )
        pd.DataFrame(self._dropped_actuals).to_csv(
            self.out_dir / "dropped_actuals.csv", index=False
        )

        # IDP missing projection columns
        idp_missing = pd.DataFrame(
            sorted(list(self._missing_proj_cols)),
            columns=["missing_proj_column"],
        )
        idp_missing.to_csv(
            self.out_dir / "idp_missing_columns.csv", index=False
        )

        # kicker column diagnostics
        try:
            pd.DataFrame(
                sorted(list(self._kicker_proj_columns_seen)),
                columns=["column"],
            ).to_csv(
                self.out_dir / "kicker_proj_columns_seen.csv", index=False
            )
        except Exception:
            pass
        try:
            pd.DataFrame(
                sorted(list(self._kicker_actual_columns_seen)),
                columns=["column"],
            ).to_csv(
                self.out_dir / "kicker_actual_columns_seen.csv", index=False
            )
        except Exception:
            pass

        # --- Plot: horizontal bar chart of R^2 by position ---
        try:
            plt.style.use("fivethirtyeight")
            mpl.rcParams.update(
                {
                    "axes.facecolor": "white",
                    "figure.facecolor": "white",
                }
            )

            r2_col = (
                "r2_clean" if "r2_clean" in summary_df.columns else "r2_raw"
            )
            plot_df = summary_df.sort_values(
                by=r2_col, ascending=False
            ).reset_index(drop=True)

            fig, ax = plt.subplots(figsize=(6, max(3, 0.6 * len(plot_df))))
            bars = ax.barh(
                plot_df["pos"], plot_df[r2_col], color="dodgerblue", alpha=0.8
            )
            ax.invert_yaxis()
            ax.set_xlabel("R²")
            # set x limit a bit above the max for annotation space
            # compute safe max r2 ignoring NaNs
            try:
                max_r2 = float(np.nanmax(plot_df[r2_col].to_numpy()))
                if not np.isfinite(max_r2):
                    max_r2 = 1.0
            except Exception:
                max_r2 = 1.0
            ax.set_xlim(0, max(1.0, max_r2 * 1.1))
            ax.set_title("R² by Position")
            # vertical gridlines
            ax.grid(axis="y", linestyle="--", color="0.9")
            # make grid appear behind bars

            # Annotate bars with R^2 values
            for bar in bars:
                width = bar.get_width()
                y = bar.get_y() + bar.get_height() / 2
                # skip annotation if position or width is not finite
                if not (np.isfinite(width) and np.isfinite(y)):
                    continue
                ax.text(
                    width + (max(0.02, max_r2 * 0.02)),
                    y,
                    f"{width:.2f}",
                    va="center",
                    ha="left",
                    fontsize=12,
                    fontweight="bold",
                    color="#222222",
                )

            plt.tight_layout()
            fig.savefig(self.out_dir / "r2_by_position.png", dpi=200)
            plt.close(fig)
        except Exception:
            # plotting should not break the run; swallow errors
            pass

    # ----------------------------
    # Internals
    # ----------------------------
    @staticmethod
    def _infer_week(path: Path) -> int | None:
        """Infer week from common filename patterns.

        Returns None if no week marker is found.
        Supports patterns like: wk1, wk01, week_1, week-1, w1, w01.
        """
        stem = path.stem.lower()

        # Most specific first
        patterns = [
            r"raw_stats_\d{4}_wk(?P<w>\d{1,2})",
            r"\bwk[_\- ]?(?P<w>\d{1,2})\b",
            r"\bweek[_\- ]?(?P<w>\d{1,2})\b",
            r"\bw(?P<w>\d{1,2})\b",
        ]
        for pat in patterns:
            m = re.search(pat, stem)
            if m:
                try:
                    w = int(m.group("w"))
                    if 0 < w <= 60:
                        return w
                except Exception:
                    continue
        return None

    def plot_pos_points_kdes(self, df: pd.DataFrame, figsize=(10, 6)):
        fig, ax = plt.subplots(figsize=figsize)
        sns.set_style("whitegrid")
        n = len(df["pos"].unique())
        colors = sns.color_palette("bright", n).as_hex()

        for (pos, g), color in zip(df.groupby("pos"), colors):
            pts = pd.to_numeric(g["actual_pts"], errors="coerce")
            min_pts = self.pos_proj_min_d[pos]
            pts = pts[pts >= min_pts]
            label = "Points Scored"

            sns.kdeplot(
                x=pts,
                ax=ax,
                fill=False,
                cut=0,
                label=pos,
                lw=4,
                color=color,
                alpha=0.7,
            )

        ax.set_xlabel(label)
        ax.set_ylabel("")
        ax.legend(fancybox=True, shadow=True, fontsize=15)
        plt.tight_layout()
        plt.savefig(
            self.out_dir / "actual_points_kde_by_position.png", dpi=200
        )

    @cached_property
    def _cached_df(self):
        fid = self.out_dir / "matched_master.csv"
        return pd.read_csv(fid)

    def df(self, min_proj_pts=None) -> pd.DataFrame:
        df = self._cached_df.copy()
        if min_proj_pts is not None:
            df = df[df["proj_pts"] >= min_proj_pts].copy()
        return df


# %%
if __name__ == "__main__":
    season = 2024
    analyzer = ProjectionVsActualAnalyzer(season=season)
    # analyzer.run(force_rebuild=False)
    # %%
    df = analyzer.df(min_proj_pts=1)
    pos_d = {
        "QB": 24,
        "RB": 48,
        "WR": 48,
        "TE": 24,
        "K": 24,
        "DL": 48,
        "LB": 48,
        "DB": 48,
    }
    weeks = list(range(2, 18))
    d = defaultdict(list)
    for pos, n_players in pos_d.items():
        pos_df = df[df["pos"] == pos].copy()
        for week in weeks:
            week_df = pos_df[pos_df["week"] == week]
            prev_df = pos_df[pos_df["week"] < week]

            prev_pts = prev_df.groupby("player_act")["actual_pts"].sum()
            prev_weeks = prev_df["player_act"].value_counts()
            prev_avg = (prev_pts / prev_weeks).drop_duplicates()
            act_s = week_df.set_index("player_act")[
                "actual_pts"
            ].drop_duplicates()
            act_s
            comp_df = (
                pd.concat(
                    (prev_avg, act_s), axis=1, keys=["prev_avg", "actual_pts"]
                )
                .dropna()
                .sort_values("prev_avg", ascending=False)
            )
            comp_df = comp_df.head(n_players)
            rho, p = spearmanr(
                comp_df["prev_avg"], comp_df["actual_pts"], nan_policy="omit"
            )
            d[pos].append(rho)

    res_df = pd.DataFrame(d, index=weeks)

    res_df["K"] > 0.25
    (res_df > 0.25).sum()

    res_df.mean().round(2)
    len(res_df)
    # %%
    fig, ax = plt.subplots(figsize=(12, 7))
    sns.heatmap(
        res_df.T,
        cmap="coolwarm",
        vmin=-1,
        vmax=1,
        annot=True,
        fmt=".2f",
        ax=ax,
        linewidths=1,
        linecolor="gray",
    )
    ax.set_xlabel("Week")
    ax.set_yticklabels(pos_d.keys(), rotation=0)
    plt.show()

    # %%
    fid = Path("~/Downloads/week_18_kickers.csv")

    def get_kicker_rank_correaltions(fid):
        df = pd.read_csv(fid)
        cols = df.columns[1:]
        col = cols[0]
        for col in cols:
            like_players = set(df["actual"]) & set(df[col])
            act_rev_s = (
                df.loc[df["actual"].isin(like_players), "actual"]
                .drop_duplicates()
                .reset_index(drop=True)
            )
            act_s = pd.Series(act_rev_s.index, index=act_rev_s)
            proj_rev_s = (
                df.loc[df[col].isin(like_players), col]
                .drop_duplicates()
                .reset_index(drop=True)
            )
            proj_s = pd.Series(proj_rev_s.index, index=proj_rev_s)
            comb_df = pd.concat((act_s, proj_s), keys=["act", "proj"], axis=1)
            rho, p = spearmanr(
                comb_df["act"], comb_df["proj"], nan_policy="omit"
            )
            print(f"{col}: rho: {rho:.2f}")
