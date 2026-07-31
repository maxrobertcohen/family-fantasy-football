"""
Family Fantasy Football League – Flask backend
================================================

Two things live here:

1.  A custom FANTASY LEAGUE where anyone you share a league link with can:
      - make an account
      - create or join a league
      - draft real NFL players onto their team (each player can only be on
        ONE team per league – that's what makes it a competition)
      - see live standings computed from real nflverse fantasy points, so
        the site tells you who is winning and who is losing.

2.  The weekly TOP-5 offense / top-5 defense players for every NFL team
    (the original nfl-stats feature), still available at /stats.

Data sources: nflverse open CSVs + the ESPN public API.
Storage: a local SQLite file (league.db) – no external DB needed.
Port: 5050
"""

import io
import math
import os
import secrets
import sqlite3
import time

import pandas as pd
import requests
from flask import (
    Flask,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from werkzeug.security import check_password_hash, generate_password_hash

app = Flask(__name__)
app.secret_key = os.environ.get("FFL_SECRET", "family-fantasy-dev-secret-change-me")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# DB path is configurable so a host (Fly) can point it at a persistent volume.
DB_PATH = os.environ.get("FFL_DB", os.path.join(BASE_DIR, "league.db"))

# ---------------------------------------------------------------------------
# Configuration – data sources
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

CACHE_TTL = 6 * 3600  # 6 hours
ROSTER_LIMIT = 8      # players each team may draft

# Latest season/week that has real, completed stats in the data source.
# Everything at or before this is HISTORICAL; a later live season is LIVE only
# once its games have actually been played and show up in the data feed.
DATA_SEASON = 2024
DATA_WEEK = 18

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
# Database
# ---------------------------------------------------------------------------
SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    created       REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS leagues (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    name     TEXT NOT NULL,
    code     TEXT UNIQUE NOT NULL,
    owner_id INTEGER NOT NULL,
    created  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS memberships (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    league_id INTEGER NOT NULL,
    user_id   INTEGER NOT NULL,
    team_name TEXT NOT NULL,
    created   REAL NOT NULL,
    UNIQUE(league_id, user_id)
);
CREATE TABLE IF NOT EXISTS roster (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    membership_id INTEGER NOT NULL,
    league_id     INTEGER NOT NULL,
    player_id     TEXT NOT NULL,
    player_name   TEXT NOT NULL,
    position      TEXT,
    nfl_team      TEXT,
    created       REAL NOT NULL,
    UNIQUE(league_id, player_id)
);
"""


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    con = sqlite3.connect(DB_PATH)
    con.executescript(SCHEMA)
    con.commit()
    con.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def safe(val):
    if val is None:
        return None
    try:
        f = float(val)
        if math.isnan(f) or math.isinf(f):
            return None
        return val
    except (TypeError, ValueError):
        pass
    return val


def ensure_hex(color: str) -> str:
    c = (color or "1a1a1a").strip()
    return c if c.startswith("#") else f"#{c}"


def fetch_dataframe(url: str, cache_key: str):
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
        print(f"[ffl] fetch_dataframe({url}): {exc}")
        return None


def filter_week(df, season: int, week: int):
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
        print(f"[ffl] filter_week error: {exc}")
        return pd.DataFrame()


def ensure_columns(df, cols, default=0):
    for col in cols:
        if col not in df.columns:
            df[col] = default
    return df


def current_user():
    uid = session.get("user_id")
    if not uid:
        return None
    row = get_db().execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    return row


# ---------------------------------------------------------------------------
# Player pool + scoring (built from the offense CSV, season totals)
# ---------------------------------------------------------------------------

def season_player_pool(season: int):
    """Aggregate per-player season fantasy totals for the draft board."""
    key = f"pool_{season}"
    cached = _cache_get(key)
    if cached is not None:
        return cached

    df = fetch_dataframe(OFFENSE_CSV_URL, "off_df")
    if df is None or df.empty:
        return []
    work = df.copy()
    work["_season"] = pd.to_numeric(work.get("season", pd.Series(dtype=float)), errors="coerce")
    work = work[work["_season"] == season]
    if work.empty:
        return []

    work = ensure_columns(
        work,
        ["player_id", "player_display_name", "position", "recent_team",
         "headshot_url", "fantasy_points_ppr"],
        default="",
    )
    work["fantasy_points_ppr"] = pd.to_numeric(work["fantasy_points_ppr"], errors="coerce").fillna(0.0)

    agg = (
        work.groupby(["player_id", "player_display_name"], dropna=False)
        .agg(
            position=("position", "last"),
            nfl_team=("recent_team", "last"),
            headshot=("headshot_url", "last"),
            total=("fantasy_points_ppr", "sum"),
            games=("fantasy_points_ppr", "count"),
        )
        .reset_index()
    )
    agg = agg.sort_values("total", ascending=False)

    pool = []
    for _, r in agg.iterrows():
        pid = str(r["player_id"])
        if not pid or pid == "nan":
            continue
        pool.append(
            {
                "player_id": pid,
                "name": r["player_display_name"] or "Unknown",
                "position": r["position"] or "",
                "nfl_team": r["nfl_team"] or "",
                "headshot": r["headshot"] or "",
                "total": round(float(r["total"]), 1),
                "games": int(r["games"]),
            }
        )
    _cache_set(key, pool)
    return pool


# ---------------------------------------------------------------------------
# Live-vs-historical data status
# ---------------------------------------------------------------------------

def live_week_info():
    """What ESPN thinks the current live NFL week is (may have no stats yet)."""
    cached = _cache_get("live_week")
    if cached is not None:
        return cached
    info = {"season": DATA_SEASON, "week": DATA_WEEK, "seasonType": 2}
    try:
        resp = requests.get(ESPN_SCOREBOARD_URL, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        info = {
            "season": int(data.get("season", {}).get("year", DATA_SEASON)),
            "week": int(data.get("week", {}).get("number", 1)),
            "seasonType": int(data.get("season", {}).get("type", 2)),
        }
    except Exception as exc:
        print(f"[ffl] live_week_info: {exc}")
    _cache_set("live_week", info)
    return info


def data_status():
    """Where 'live' currently is vs the newest data we actually have.

    is_live = True only when the current NFL season already has real, playable
    stats in the feed. Until the new season kicks off, the app runs on the last
    completed season's data (HISTORICAL), which we surface loudly in the UI.
    """
    cached = _cache_get("data_status")
    if cached is not None:
        return cached
    live = live_week_info()
    live_season = int(live.get("season", DATA_SEASON))
    live_week = int(live.get("week", 1))
    is_live = False
    if live_season > DATA_SEASON:
        off_df = fetch_dataframe(OFFENSE_CSV_URL, "off_df")
        is_live = not filter_week(off_df, live_season, live_week).empty
    elif live_season == DATA_SEASON:
        is_live = True
    if is_live:
        default_season, default_week = live_season, live_week
    else:
        default_season, default_week = DATA_SEASON, DATA_WEEK
    status = {
        "live_season": live_season,
        "live_week": live_week,
        "is_live": is_live,
        "data_season": DATA_SEASON,
        "data_week": DATA_WEEK,
        "default_season": default_season,
        "default_week": default_week,
    }
    _cache_set("data_status", status)
    return status


def season_descriptor(season, status=None):
    """Human label describing whether a season shows LIVE or HISTORICAL data."""
    st = status or data_status()
    season = int(season)
    if season == st["live_season"] and st["is_live"]:
        return {"mode": "live", "label": "🔴 LIVE NOW",
                "note": f"Real {season} season — scores update as games are played."}
    if season == st["live_season"] and not st["is_live"]:
        return {"mode": "upcoming", "label": "⏳ NOT STARTED YET",
                "note": f"The {season} season hasn't kicked off. Showing last season's real stats until it does."}
    return {"mode": "historical", "label": f"📚 {season} FINAL",
            "note": f"These are last season's real numbers ({season}, completed) — not live scores."}


def player_scores(season: int):
    """Map player_id -> season total fantasy points (for standings)."""
    return {p["player_id"]: p["total"] for p in season_player_pool(season)}


# ---------------------------------------------------------------------------
# CORS
# ---------------------------------------------------------------------------

@app.after_request
def add_cors(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return response


# ---------------------------------------------------------------------------
# Auth routes
# ---------------------------------------------------------------------------

@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        if len(username) < 2 or len(password) < 4:
            return render_template("register.html", error="Pick a name (2+) and password (4+).")
        db = get_db()
        try:
            db.execute(
                "INSERT INTO users(username, password_hash, created) VALUES (?,?,?)",
                (username, generate_password_hash(password), time.time()),
            )
            db.commit()
        except sqlite3.IntegrityError:
            return render_template("register.html", error="That name is taken.")
        row = db.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        session["user_id"] = row["id"]
        nxt = request.args.get("next")
        return redirect(nxt or url_for("dashboard"))
    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        row = get_db().execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        if row and check_password_hash(row["password_hash"], password):
            session["user_id"] = row["id"]
            nxt = request.args.get("next")
            return redirect(nxt or url_for("dashboard"))
        return render_template("login.html", error="Wrong name or password.")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("home"))


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------

@app.route("/")
def home():
    return render_template("home.html", user=current_user())


@app.route("/dashboard")
def dashboard():
    user = current_user()
    if not user:
        return redirect(url_for("login"))
    db = get_db()
    leagues = db.execute(
        """SELECT l.*, m.team_name,
                  (SELECT COUNT(*) FROM memberships mm WHERE mm.league_id=l.id) AS members
             FROM leagues l JOIN memberships m ON m.league_id=l.id
            WHERE m.user_id=? ORDER BY l.created DESC""",
        (user["id"],),
    ).fetchall()
    return render_template("dashboard.html", user=user, leagues=leagues)


@app.route("/league/new", methods=["POST"])
def league_new():
    user = current_user()
    if not user:
        return redirect(url_for("login"))
    name = (request.form.get("name") or "").strip() or "Our League"
    team_name = (request.form.get("team_name") or "").strip() or f"{user['username']}'s Team"
    code = secrets.token_urlsafe(5)
    db = get_db()
    db.execute(
        "INSERT INTO leagues(name, code, owner_id, created) VALUES (?,?,?,?)",
        (name, code, user["id"], time.time()),
    )
    league = db.execute("SELECT * FROM leagues WHERE code=?", (code,)).fetchone()
    db.execute(
        "INSERT INTO memberships(league_id, user_id, team_name, created) VALUES (?,?,?,?)",
        (league["id"], user["id"], team_name, time.time()),
    )
    db.commit()
    return redirect(url_for("league_view", code=code))


@app.route("/join/<code>", methods=["GET", "POST"])
def join(code):
    user = current_user()
    if not user:
        return redirect(url_for("login", next=url_for("join", code=code)))
    db = get_db()
    league = db.execute("SELECT * FROM leagues WHERE code=?", (code,)).fetchone()
    if not league:
        return render_template("message.html", user=user, message="That league link is not valid.")
    existing = db.execute(
        "SELECT * FROM memberships WHERE league_id=? AND user_id=?",
        (league["id"], user["id"]),
    ).fetchone()
    if existing:
        return redirect(url_for("league_view", code=code))
    if request.method == "POST":
        team_name = (request.form.get("team_name") or "").strip() or f"{user['username']}'s Team"
        db.execute(
            "INSERT INTO memberships(league_id, user_id, team_name, created) VALUES (?,?,?,?)",
            (league["id"], user["id"], team_name, time.time()),
        )
        db.commit()
        return redirect(url_for("league_view", code=code))
    return render_template("join.html", user=user, league=league)


def compute_standings(league_id: int, season: int):
    db = get_db()
    members = db.execute(
        "SELECT * FROM memberships WHERE league_id=? ORDER BY created", (league_id,)
    ).fetchall()
    scores = player_scores(season)
    standings = []
    for m in members:
        roster = db.execute(
            "SELECT * FROM roster WHERE membership_id=? ORDER BY created", (m["id"],)
        ).fetchall()
        total = 0.0
        players = []
        for p in roster:
            pts = float(scores.get(p["player_id"], 0.0))
            total += pts
            players.append(
                {
                    "player_id": p["player_id"],
                    "name": p["player_name"],
                    "position": p["position"],
                    "nfl_team": p["nfl_team"],
                    "points": round(pts, 1),
                }
            )
        uname = db.execute("SELECT username FROM users WHERE id=?", (m["user_id"],)).fetchone()
        standings.append(
            {
                "membership_id": m["id"],
                "user_id": m["user_id"],
                "username": uname["username"] if uname else "?",
                "team_name": m["team_name"],
                "total": round(total, 1),
                "players": players,
                "slots_left": ROSTER_LIMIT - len(players),
            }
        )
    standings.sort(key=lambda s: s["total"], reverse=True)
    for i, s in enumerate(standings):
        s["rank"] = i + 1
    return standings


@app.route("/league/<code>")
def league_view(code):
    user = current_user()
    if not user:
        return redirect(url_for("login", next=url_for("league_view", code=code)))
    db = get_db()
    league = db.execute("SELECT * FROM leagues WHERE code=?", (code,)).fetchone()
    if not league:
        return render_template("message.html", user=user, message="That league does not exist.")
    membership = db.execute(
        "SELECT * FROM memberships WHERE league_id=? AND user_id=?",
        (league["id"], user["id"]),
    ).fetchone()
    if not membership:
        return redirect(url_for("join", code=code))
    season = int(request.args.get("season", DATA_SEASON))
    standings = compute_standings(league["id"], season)
    my = next((s for s in standings if s["user_id"] == user["id"]), None)
    join_url = url_for("join", code=code, _external=True)
    return render_template(
        "league.html",
        user=user,
        league=league,
        standings=standings,
        my=my,
        join_url=join_url,
        season=season,
        data_mode=season_descriptor(season),
        roster_limit=ROSTER_LIMIT,
    )


@app.route("/league/<code>/draft")
def draft(code):
    user = current_user()
    if not user:
        return redirect(url_for("login", next=url_for("draft", code=code)))
    db = get_db()
    league = db.execute("SELECT * FROM leagues WHERE code=?", (code,)).fetchone()
    if not league:
        return render_template("message.html", user=user, message="That league does not exist.")
    membership = db.execute(
        "SELECT * FROM memberships WHERE league_id=? AND user_id=?",
        (league["id"], user["id"]),
    ).fetchone()
    if not membership:
        return redirect(url_for("join", code=code))
    season = int(request.args.get("season", DATA_SEASON))
    my_roster = db.execute(
        "SELECT * FROM roster WHERE membership_id=? ORDER BY created", (membership["id"],)
    ).fetchall()
    return render_template(
        "draft.html",
        user=user,
        league=league,
        membership=membership,
        season=season,
        data_mode=season_descriptor(season),
        my_roster=my_roster,
        roster_limit=ROSTER_LIMIT,
    )


# ------- draft API (used by the draft board) -------

@app.route("/api/pool")
def api_pool():
    season = int(request.args.get("season", 2024))
    league_code = request.args.get("league")
    pool = season_player_pool(season)
    taken = set()
    if league_code:
        db = get_db()
        league = db.execute("SELECT * FROM leagues WHERE code=?", (league_code,)).fetchone()
        if league:
            rows = db.execute("SELECT player_id FROM roster WHERE league_id=?", (league["id"],)).fetchall()
            taken = {r["player_id"] for r in rows}
    out = []
    for p in pool:  # every player in the season pool — no top-N cap
        q = dict(p)
        q["taken"] = p["player_id"] in taken
        out.append(q)
    return jsonify(out)


@app.route("/api/draft", methods=["POST"])
def api_draft():
    user = current_user()
    if not user:
        return jsonify({"ok": False, "error": "Please log in."}), 401
    data = request.get_json(force=True, silent=True) or {}
    code = data.get("league")
    player_id = str(data.get("player_id") or "")
    db = get_db()
    league = db.execute("SELECT * FROM leagues WHERE code=?", (code,)).fetchone()
    if not league:
        return jsonify({"ok": False, "error": "League not found."}), 404
    membership = db.execute(
        "SELECT * FROM memberships WHERE league_id=? AND user_id=?",
        (league["id"], user["id"]),
    ).fetchone()
    if not membership:
        return jsonify({"ok": False, "error": "You are not in this league."}), 403

    count = db.execute(
        "SELECT COUNT(*) AS c FROM roster WHERE membership_id=?", (membership["id"],)
    ).fetchone()["c"]
    if count >= ROSTER_LIMIT:
        return jsonify({"ok": False, "error": f"Your roster is full ({ROSTER_LIMIT})."}), 400

    season = int(data.get("season", 2024))
    pool = {p["player_id"]: p for p in season_player_pool(season)}
    p = pool.get(player_id)
    if not p:
        return jsonify({"ok": False, "error": "Unknown player."}), 400
    try:
        db.execute(
            """INSERT INTO roster(membership_id, league_id, player_id, player_name,
                                  position, nfl_team, created)
               VALUES (?,?,?,?,?,?,?)""",
            (membership["id"], league["id"], player_id, p["name"],
             p["position"], p["nfl_team"], time.time()),
        )
        db.commit()
    except sqlite3.IntegrityError:
        return jsonify({"ok": False, "error": "That player is already taken in this league."}), 400
    return jsonify({"ok": True, "player": p})


@app.route("/api/drop", methods=["POST"])
def api_drop():
    user = current_user()
    if not user:
        return jsonify({"ok": False, "error": "Please log in."}), 401
    data = request.get_json(force=True, silent=True) or {}
    code = data.get("league")
    player_id = str(data.get("player_id") or "")
    db = get_db()
    league = db.execute("SELECT * FROM leagues WHERE code=?", (code,)).fetchone()
    if not league:
        return jsonify({"ok": False, "error": "League not found."}), 404
    membership = db.execute(
        "SELECT * FROM memberships WHERE league_id=? AND user_id=?",
        (league["id"], user["id"]),
    ).fetchone()
    if not membership:
        return jsonify({"ok": False, "error": "You are not in this league."}), 403
    db.execute(
        "DELETE FROM roster WHERE membership_id=? AND player_id=?",
        (membership["id"], player_id),
    )
    db.commit()
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Weekly team top-5 stats (original feature) -> /stats
# ---------------------------------------------------------------------------

@app.route("/stats")
def stats_page():
    return render_template("stats.html", user=current_user())


@app.route("/api/data-status")
def api_data_status():
    return jsonify(data_status())


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
        print(f"[ffl] api_teams error: {exc}")
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
                "season": season_info.get("year", 2024),
                "week": week_info.get("number", 18),
                "seasonType": season_info.get("type", 2),
            }
        )
    except Exception as exc:
        print(f"[ffl] api_current_week error: {exc}")
        return jsonify({"season": 2024, "week": 18, "seasonType": 2})


@app.route("/api/stats")
def api_stats():
    try:
        season = int(request.args.get("season", 2024))
        week = int(request.args.get("week", 18))
    except (TypeError, ValueError):
        season, week = 2024, 18

    off_df = fetch_dataframe(OFFENSE_CSV_URL, "off_df")
    def_df = fetch_dataframe(DEFENSE_CSV_URL, "def_df")
    off_week = filter_week(off_df, season, week)
    def_week = filter_week(def_df, season, week)
    data_note = None

    if off_week.empty and def_week.empty:
        if season != 2024 or week != 18:
            data_note = f"No data for {season} Week {week}. Showing 2024 Week 18."
            off_week = filter_week(off_df, 2024, 18)
            def_week = filter_week(def_df, 2024, 18)

    teams_data: dict = {}

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
init_db()

if __name__ == "__main__":
    app.run(debug=True, port=5050)
