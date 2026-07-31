"""
NFL Stats – Flask backend
Serves team + player data from nflverse open CSVs and the ESPN public API.
Port: 5050
"""

import io
import math
import time

import pandas as pd
import requests
from flask import Flask, jsonify, render_template, request

app = Flask(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
OFFENSE_CSV_URL = (
    "https://github.com/nflverse/nflverse-data/releases/download/"
    "player_stats/player_stats.csv"
)
DEFENSE_CSV_URL = (
    "https://github.com/nflverse/nflverse-data/releases/download/"
    "player_stats/player_stats_def.csv"
)
ESPN_TEAMS_URL = (
    "https://site.api.espn.com/apis/site/v2/sports/football/nfl/teams?limit=32"
)
ESPN_SCOREBOARD_URL = (
    "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard"
)

CACHE_TTL = 6 * 3600  # 6 hours in seconds

# ---------------------------------------------------------------------------
# In-memory cache  { key: (data, timestamp) }
# ---------------------------------------------------------------------------
_cache: dict = {}


def _cache_get(key):
    entry = _cache.get(key)
    if entry is None:
        return None
    data, ts = entry
    if time.time() - ts < CACHE_TTL:
        return data
    return None


def _cache_set(key, data):
    _cache[key] = (data, time.time())


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def safe(val):
    """Return None for NaN / non-finite floats; pass everything else through."""
    if val is None:
        return None
    try:
        f = float(val)
        if math.isnan(f) or math.isinf(f):
            return None
        # If it was originally an int-like float, preserve the numeric value
        return val
    except (TypeError, ValueError):
        pass
    return val


def ensure_hex(color: str) -> str:
    """Guarantee a CSS hex color string starting with '#'."""
    c = (color or "1a1a1a").strip()
    return c if c.startswith("#") else f"#{c}"


def fetch_dataframe(url: str, cache_key: str) -> pd.DataFrame | None:
    """Download a CSV and cache the resulting DataFrame."""
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached
    try:
        resp = requests.get(url, timeout=120, allow_redirects=True)
        resp.raise_for_status()
        df = pd.read_csv(io.BytesIO(resp.content), low_memory=False)
        _cache_set(cache_key, df)
        return df
    except Exception as exc:
        print(f"[nfl-stats] fetch_dataframe({url}): {exc}")
        return None


def filter_week(df: pd.DataFrame | None, season: int, week: int) -> pd.DataFrame:
    """Return rows matching season + week + season_type == REG."""
    if df is None or df.empty:
        return pd.DataFrame()
    try:
        work = df.copy()
        work["_season"] = pd.to_numeric(work.get("season", pd.Series(dtype=float)), errors="coerce")
        work["_week"] = pd.to_numeric(work.get("week", pd.Series(dtype=float)), errors="coerce")
        mask = (work["_season"] == season) & (work["_week"] == week)
        if "season_type" in work.columns:
            mask &= work["season_type"].astype(str).str.upper().str.strip() == "REG"
        return work[mask].copy()
    except Exception as exc:
        print(f"[nfl-stats] filter_week error: {exc}")
        return pd.DataFrame()


def ensure_columns(df: pd.DataFrame, cols: list, default=0) -> pd.DataFrame:
    """Add any missing columns with a default value."""
    for col in cols:
        if col not in df.columns:
            df[col] = default
    return df


# ---------------------------------------------------------------------------
# CORS – applied to every response
# ---------------------------------------------------------------------------

@app.after_request
def add_cors(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return response


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/teams")
def api_teams():
    cached = _cache_get("espn_teams")
    if cached is not None:
        return jsonify(cached)

    try:
        resp = requests.get(ESPN_TEAMS_URL, timeout=10)
        resp.raise_for_status()
        raw = resp.json()

        sports = raw.get("sports", [])
        leagues = sports[0].get("leagues", []) if sports else []
        entries = leagues[0].get("teams", []) if leagues else []

        teams = []
        for entry in entries:
            t = entry.get("team", {})
            logos = t.get("logos", [])
            logo = logos[0].get("href", "") if logos else ""
            teams.append(
                {
                    "id": t.get("id", ""),
                    "name": t.get("displayName", ""),
                    "shortName": t.get("shortDisplayName", ""),
                    "abbreviation": t.get("abbreviation", ""),
                    "logo": logo,
                    "color": ensure_hex(t.get("color", "1a1a1a")),
                    "alternateColor": ensure_hex(t.get("alternateColor", "ffffff")),
                }
            )
        _cache_set("espn_teams", teams)
        return jsonify(teams)

    except Exception as exc:
        print(f"[nfl-stats] api_teams error: {exc}")
        return jsonify([])


@app.route("/api/current-week")
def api_current_week():
    try:
        resp = requests.get(ESPN_SCOREBOARD_URL, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        season_info = data.get("season", {})
        week_info = data.get("week", {})
        return jsonify(
            {
                "season": season_info.get("year", 2025),
                "week": week_info.get("number", 18),
                "seasonType": season_info.get("type", 2),
            }
        )
    except Exception as exc:
        print(f"[nfl-stats] api_current_week error: {exc}")
        return jsonify({"season": 2025, "week": 18, "seasonType": 2})


@app.route("/api/stats")
def api_stats():
    try:
        season = int(request.args.get("season", 2025))
        week = int(request.args.get("week", 18))
    except (TypeError, ValueError):
        season, week = 2025, 18

    # Download (or retrieve cached) DataFrames
    off_df = fetch_dataframe(OFFENSE_CSV_URL, "off_df")
    def_df = fetch_dataframe(DEFENSE_CSV_URL, "def_df")

    off_week = filter_week(off_df, season, week)
    def_week = filter_week(def_df, season, week)

    data_note = None

    # Fall back to 2025 Week 18 if the requested period has no data
    if off_week.empty and def_week.empty:
        if season != 2025 or week != 18:
            data_note = (
                f"No data found for {season} Week {week}. "
                "Showing 2025 Week 18 instead."
            )
            off_week = filter_week(off_df, 2025, 18)
            def_week = filter_week(def_df, 2025, 18)

    teams_data: dict = {}

    # ------------------------------------------------------------------ #
    # Offense                                                              #
    # ------------------------------------------------------------------ #
    OFF_STAT_COLS = [
        "passing_yards", "passing_tds", "carries", "rushing_yards",
        "rushing_tds", "receptions", "receiving_yards", "receiving_tds",
        "fantasy_points_ppr",
    ]
    OFF_META_COLS = ["player_display_name", "recent_team", "position", "headshot_url"]

    if not off_week.empty:
        off_week = ensure_columns(off_week, OFF_STAT_COLS, default=0)
        off_week = ensure_columns(off_week, OFF_META_COLS, default="")
        off_week["fantasy_points_ppr"] = pd.to_numeric(
            off_week["fantasy_points_ppr"], errors="coerce"
        ).fillna(0.0)

        for team, grp in off_week.groupby("recent_team"):
            top5 = grp.nlargest(5, "fantasy_points_ppr")
            players = []
            for _, row in top5.iterrows():
                stats = {c: safe(row.get(c, 0)) for c in OFF_STAT_COLS}
                players.append(
                    {
                        "name": safe(row.get("player_display_name", "")) or "Unknown",
                        "position": safe(row.get("position", "")) or "",
                        "headshot_url": safe(row.get("headshot_url", "")) or "",
                        "stats": stats,
                        "score": safe(row.get("fantasy_points_ppr", 0)),
                    }
                )
            teams_data.setdefault(team, {"offense": [], "defense": []})
            teams_data[team]["offense"] = players

    # ------------------------------------------------------------------ #
    # Defense                                                              #
    # ------------------------------------------------------------------ #
    DEF_STAT_COLS = [
        "def_tackles", "def_tackles_solo", "def_sacks",
        "def_interceptions", "def_pass_defended", "def_tds",
    ]
    DEF_META_COLS = ["player_display_name", "recent_team", "position", "headshot_url"]

    if not def_week.empty:
        def_week = ensure_columns(def_week, DEF_STAT_COLS, default=0)
        def_week = ensure_columns(def_week, DEF_META_COLS, default="")
        for col in DEF_STAT_COLS:
            def_week[col] = pd.to_numeric(def_week[col], errors="coerce").fillna(0.0)

        # Composite defensive score
        def_week["def_score"] = (
            def_week["def_tackles"]
            + def_week["def_sacks"] * 3
            + def_week["def_interceptions"] * 4
            + def_week["def_pass_defended"] * 2
        )

        for team, grp in def_week.groupby("recent_team"):
            top5 = grp.nlargest(5, "def_score")
            players = []
            for _, row in top5.iterrows():
                stats = {c: safe(row.get(c, 0)) for c in DEF_STAT_COLS}
                players.append(
                    {
                        "name": safe(row.get("player_display_name", "")) or "Unknown",
                        "position": safe(row.get("position", "")) or "",
                        "headshot_url": safe(row.get("headshot_url", "")) or "",
                        "stats": stats,
                        "score": safe(row.get("def_score", 0)),
                    }
                )
            teams_data.setdefault(team, {"offense": [], "defense": []})
            teams_data[team]["defense"] = players

    result: dict = {"teams": teams_data}
    if data_note:
        result["data_note"] = data_note
    return jsonify(result)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    app.run(debug=True, port=5050)
