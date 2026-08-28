"""
Database query functions — all queries written as explicit SQL using sqlalchemy.text().
"""

import statistics as _stats
from collections import defaultdict, deque
from datetime import date, datetime
from typing import Any, Optional
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.orm import Session

# ═══════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════

_FAMILY_TERMINALS = frozenset({
    'Defense', 'Opening', 'Game', 'Gambit', 'Attack',
    'System', 'Formation', 'Variation', 'Stonewall',
})

# Sicilians are too common to lump together, so they get split one level
# deeper: named systems below count as terminals alongside the family ones.
_SICILIAN_TERMINALS = _FAMILY_TERMINALS | frozenset({
    'Dragon', 'Najdorf', 'Sveshnikov', 'Kalashnikov', 'Taimanov',
    'Kan', 'Scheveningen', 'Rossolimo', 'Alapin', 'Paulsen',
})

def _sicilian_subfamily(words: list[str], start: int) -> str:
    """
    Extend 'Sicilian Defense' with the named variation that follows.
    The result is always a word-prefix of the raw opening_name, so the
    LIKE-prefix opening filters keep working on sub-family names.
    """
    for i in range(start, len(words)):
        if any(c.isdigit() for c in words[i]):
            # Hit raw move text: keep any name words seen so far, otherwise
            # fall back to the first move pair (e.g. 'Sicilian Defense 2.Nf3 d6').
            end = i if i > start else min(start + 2, len(words))
            return ' '.join(words[:end])
        if words[i] in _SICILIAN_TERMINALS:
            return ' '.join(words[:i + 1])
    return ' '.join(words)

def _opening_family(name: str) -> str:
    """Truncate an opening name at its first terminal keyword to get the family."""
    if not name:
        return name
    words = name.split()
    for i, w in enumerate(words):
        if w in _FAMILY_TERMINALS:
            if ' '.join(words[:i + 1]) == 'Sicilian Defense':
                return _sicilian_subfamily(words, i + 1)
            return ' '.join(words[:i + 1])
    return ' '.join(words[:2])

def _family_display_name(family: str) -> str:
    """Shorter table/tab label for Sicilian sub-families."""
    prefix = 'Sicilian Defense '
    if family.startswith(prefix):
        sub = family[len(prefix):]
        sub = sub.replace('Nyezhmetdinov Rossolimo', 'Rossolimo')
        sub = sub.removesuffix(' Variation')
        return 'Sicilian: ' + sub
    return family

def _build_game_filters(
    player_id: int,
    time_class: Optional[str] = None,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    player_color: Optional[str] = None,
    opening_names: Optional[str] = None,
) -> tuple[str, dict]:
    """
    Build a SQL WHERE clause and parameter dict for player game queries.
    The games table must be aliased as 'g' in the calling query.
    """
    clauses: list[str] = []
    params: dict[str, Any] = {"player_id": player_id}

    if player_color == "white":
        clauses.append("g.white_player_id = :player_id")
    elif player_color == "black":
        clauses.append("g.black_player_id = :player_id")
    else:
        clauses.append("(g.white_player_id = :player_id OR g.black_player_id = :player_id)")

    if time_class:
        clauses.append("g.time_class = :time_class")
        params["time_class"] = time_class
    if start_date:
        clauses.append("g.date_played >= :start_date")
        params["start_date"] = start_date
    if end_date:
        clauses.append("g.date_played <= :end_date")
        params["end_date"] = end_date
    if opening_names:
        ops = [o.strip() for o in opening_names.split("|") if o.strip()]
        if ops:
            like_clauses = [f"g.opening_name LIKE :op_{i}" for i in range(len(ops))]
            clauses.append(f"({' OR '.join(like_clauses)})")
            for i, op in enumerate(ops):
                params[f"op_{i}"] = op + '%'

    return " AND ".join(clauses), params


# ═══════════════════════════════════════════════════════════
# CRUD Operations
# ═══════════════════════════════════════════════════════════

def get_players(db: Session, search: Optional[str] = None, limit: int = 50):
    sql = text("""
        SELECT player_id, username, platform
        FROM   players
        WHERE  (:search IS NULL OR username LIKE :search)
        ORDER  BY username
        LIMIT  :limit
    """)
    return db.execute(sql, {
        "search": f"%{search}%" if search else None,
        "limit":  limit,
    }).mappings().all()


def get_player(db: Session, username: str):
    sql = text("""
        SELECT player_id, username, platform
        FROM   players
        WHERE  username = :username
    """)
    return db.execute(sql, {"username": username}).mappings().first()


def get_games_for_player(
    db: Session, player_id: int,
    time_class: Optional[str] = None,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    limit: int = 50, offset: int = 0,
    opening_names: Optional[str] = None,
):
    where, params = _build_game_filters(player_id, time_class, start_date, end_date, opening_names=opening_names)
    params["limit"]  = limit
    params["offset"] = offset
    sql = text(f"""
        SELECT
            g.game_id,
            g.result,
            g.date_played,
            g.time_class,
            g.white_elo,
            g.black_elo,
            g.total_moves,
            g.opening_name,
            pw.username                                              AS white_username,
            pb.username                                              AS black_username,
            CASE WHEN g.white_player_id = :player_id THEN 1 ELSE 0 END AS is_white
        FROM   games   g
        JOIN   players pw ON g.white_player_id = pw.player_id
        JOIN   players pb ON g.black_player_id = pb.player_id
        WHERE  {where}
        ORDER  BY g.date_played DESC, g.game_id DESC
        LIMIT  :limit OFFSET :offset
    """)
    return db.execute(sql, params).mappings().all()


def get_game(db: Session, game_id: int):
    sql = text("""
        SELECT
            g.game_id,
            g.result,
            g.date_played,
            g.time_control,
            g.time_class,
            g.white_elo,
            g.black_elo,
            g.white_accuracy,
            g.black_accuracy,
            g.eco,
            g.opening_name,
            g.termination,
            g.chess_com_url,
            g.total_moves,
            pw.username AS white_username,
            pb.username AS black_username
        FROM   games   g
        JOIN   players pw ON g.white_player_id = pw.player_id
        JOIN   players pb ON g.black_player_id = pb.player_id
        WHERE  g.game_id = :game_id
    """)
    return db.execute(sql, {"game_id": game_id}).mappings().first()


def get_game_moves(db: Session, game_id: int):
    sql = text("""
        SELECT move_id, game_id, ply, move_number, color,
               move_san, clock_seconds, time_spent_seconds
        FROM   moves
        WHERE  game_id = :game_id
        ORDER  BY ply
    """)
    return db.execute(sql, {"game_id": game_id}).mappings().all()


def delete_game(db: Session, game_id: int) -> bool:
    result = db.execute(
        text("DELETE FROM games WHERE game_id = :game_id"),
        {"game_id": game_id},
    )
    db.commit()
    return result.rowcount > 0  # type: ignore[attr-defined]


# ═══════════════════════════════════════════════════════════
# Analytics Queries
# ═══════════════════════════════════════════════════════════

def get_player_stats(
    db: Session, player_id: int,
    time_class: Optional[str] = None,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
):
    where, params = _build_game_filters(player_id, time_class, start_date, end_date)

    # Overall totals via CTE
    sql = text(f"""
        WITH player_games AS (
            SELECT
                g.game_id,
                g.result,
                g.time_class,
                CASE WHEN g.white_player_id = :player_id THEN 1 ELSE 0 END AS is_white
            FROM games g
            WHERE {where}
        ),
        outcomes AS (
            SELECT
                game_id,
                time_class,
                CASE
                    WHEN (result = '1-0' AND is_white = 1)
                      OR (result = '0-1' AND is_white = 0) THEN 'win'
                    WHEN (result = '0-1' AND is_white = 1)
                      OR (result = '1-0' AND is_white = 0) THEN 'loss'
                    ELSE 'draw'
                END AS outcome
            FROM player_games
        ),
        move_counts AS (
            SELECT m.game_id, COUNT(*) AS cnt
            FROM   moves m
            JOIN   player_games pg ON m.game_id = pg.game_id
            WHERE  (pg.is_white = 1 AND m.color = 'white')
                OR (pg.is_white = 0 AND m.color = 'black')
            GROUP BY m.game_id
        )
        SELECT
            COUNT(*)                                               AS total_games,
            SUM(CASE WHEN o.outcome = 'win'  THEN 1 ELSE 0 END)  AS wins,
            SUM(CASE WHEN o.outcome = 'loss' THEN 1 ELSE 0 END)  AS losses,
            SUM(CASE WHEN o.outcome = 'draw' THEN 1 ELSE 0 END)  AS draws,
            COALESCE(SUM(mc.cnt), 0)                              AS total_moves
        FROM outcomes o
        LEFT JOIN move_counts mc ON o.game_id = mc.game_id
    """)
    row = db.execute(sql, params).mappings().first()
    assert row is not None  # COUNT(*) always returns a row

    total    = row["total_games"] or 0
    wins     = row["wins"]        or 0
    losses   = row["losses"]      or 0
    draws    = row["draws"]       or 0
    moves    = row["total_moves"] or 0
    decisive = wins + losses

    # Per-time-class breakdown
    tc_sql = text(f"""
        WITH player_games AS (
            SELECT
                g.game_id, g.result, g.time_class,
                CASE WHEN g.white_player_id = :player_id THEN 1 ELSE 0 END AS is_white
            FROM games g
            WHERE {where}
        )
        SELECT
            time_class,
            COUNT(*)                                                                        AS total,
            SUM(CASE WHEN (result='1-0' AND is_white=1) OR (result='0-1' AND is_white=0)
                     THEN 1 ELSE 0 END)                                                    AS wins,
            SUM(CASE WHEN (result='0-1' AND is_white=1) OR (result='1-0' AND is_white=0)
                     THEN 1 ELSE 0 END)                                                    AS losses,
            SUM(CASE WHEN result = '1/2-1/2' THEN 1 ELSE 0 END)                           AS draws
        FROM player_games
        GROUP BY time_class
    """)

    tc_moves_sql = text(f"""
        WITH player_games AS (
            SELECT
                g.game_id, g.time_class,
                CASE WHEN g.white_player_id = :player_id THEN 1 ELSE 0 END AS is_white
            FROM games g
            WHERE {where}
        )
        SELECT pg.time_class, COUNT(*) AS cnt
        FROM   moves m
        JOIN   player_games pg ON m.game_id = pg.game_id
        WHERE  (pg.is_white = 1 AND m.color = 'white')
            OR (pg.is_white = 0 AND m.color = 'black')
        GROUP BY pg.time_class
    """)

    by_tc: dict[str, Any] = {}
    for r in db.execute(tc_sql, params).mappings().all():
        tc = r["time_class"] or "unknown"
        by_tc[tc] = {
            "total": r["total"], "wins": r["wins"],
            "losses": r["losses"], "draws": r["draws"],
            "total_moves": 0,
        }
    for r in db.execute(tc_moves_sql, params).mappings().all():
        tc = r["time_class"] or "unknown"
        if tc in by_tc:
            by_tc[tc]["total_moves"] = r["cnt"]

    return {
        "total_games":        total,
        "total_moves":        moves,
        "wins":               wins,
        "losses":             losses,
        "draws":              draws,
        "win_rate":           round(wins / total * 100, 1)    if total    else 0,
        "decisive_win_rate":  round(wins / decisive * 100, 1) if decisive else 0,
        "draw_rate":          round(draws / total * 100, 1)   if total    else 0,
        "by_time_class":      by_tc,
    }


# ── Feature 1: Rating Differential ──────────────────────

def rating_differential(
    db: Session, player_id: int,
    time_class: Optional[str] = None,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    player_color: Optional[str] = None,
    opening_names: Optional[str] = None,
):
    """Win/loss/draw counts bucketed by estimated PRE-game Elo gap
    (player Elo − opponent Elo, with each game's own rating update undone)."""
    where, params = _build_game_filters(
        player_id, time_class, start_date, end_date, player_color, opening_names
    )
    sql = text(f"""
        SELECT
            CASE WHEN g.white_player_id = :player_id
                 THEN g.white_elo - g.black_elo
                 ELSE g.black_elo - g.white_elo
            END AS elo_diff,
            CASE
                WHEN (g.white_player_id = :player_id AND g.result = '1-0')
                  OR (g.black_player_id = :player_id AND g.result = '0-1') THEN 'win'
                WHEN g.result = '1/2-1/2'                                   THEN 'draw'
                ELSE 'loss'
            END AS outcome
        FROM games g
        WHERE {where}
          AND g.white_elo IS NOT NULL
          AND g.black_elo IS NOT NULL
    """)
    rows = db.execute(sql, params).mappings().all()

    def pre_game_diff(diff_post: float, score: float) -> float:
        # Chess.com PGN elos are POST-game, so each result is baked into its
        # own stored gap: wins push the opponent below you, losses above.
        # Undo both players' updates with the established-player Glicko
        # approximation (Elo, K=16): diff_post = diff_pre + 32*(score - E),
        # E = 1/(1+10^(-diff_pre/400)). Solved by fixed-point iteration.
        d = diff_post
        for _ in range(3):
            expected = 1 / (1 + 10 ** (-d / 400))
            d = diff_post - 32 * (score - expected)
        return d

    score_for = {"win": 1.0, "draw": 0.5, "loss": 0.0}

    bucket_defs = [
        ("> +100",       lambda d: d >= 100),
        ("+50 to +100",  lambda d: 50 <= d < 100),
        ("+40 to +50",   lambda d: 40 <= d < 50),
        ("+30 to +40",   lambda d: 30 <= d < 40),
        ("+20 to +30",   lambda d: 20 <= d < 30),
        ("+10 to +20",   lambda d: 10 <= d < 20),
        ("0 to +10",     lambda d: 0 <= d < 10),
        ("-10 to 0",     lambda d: -10 <= d < 0),
        ("-20 to -10",   lambda d: -20 <= d < -10),
        ("-30 to -20",   lambda d: -30 <= d < -20),
        ("-40 to -30",   lambda d: -40 <= d < -30),
        ("-50 to -40",   lambda d: -50 <= d < -40),
        ("-100 to -50",  lambda d: -100 <= d < -50),
        ("< -100",       lambda d: d < -100),
    ]
    buckets = {label: {"wins": 0, "losses": 0, "draws": 0} for label, _ in bucket_defs}

    for row in rows:
        outcome = row["outcome"]
        diff = pre_game_diff(row["elo_diff"], score_for[outcome])
        for label, test in bucket_defs:
            if test(diff):
                if outcome == "win":
                    buckets[label]["wins"] += 1
                elif outcome == "loss":
                    buckets[label]["losses"] += 1
                else:
                    buckets[label]["draws"] += 1
                break

    results: list[dict[str, Any]] = []
    for label, _ in bucket_defs:
        b = buckets[label]
        total    = b["wins"] + b["losses"] + b["draws"]
        decisive = b["wins"] + b["losses"]
        results.append({
            "bucket":           label,
            "total_games":      total,
            "wins":             b["wins"],
            "losses":           b["losses"],
            "draws":            b["draws"],
            "win_rate":         round(b["wins"] / total    * 100, 1) if total    else 0,
            "win_rate_no_draws":round(b["wins"] / decisive * 100, 1) if decisive else 0,
            "draw_rate":        round(b["draws"] / total   * 100, 1) if total    else 0,
        })

    og   = sum(r["total_games"] for r in results)
    ow   = sum(r["wins"]        for r in results)
    od   = sum(r["draws"]       for r in results)
    odec = sum(r["wins"] + r["losses"] for r in results)

    return {
        "buckets":                  results,
        "overall_decisive_win_rate":round(ow / odec * 100, 1) if odec else 0,
        "overall_draw_rate":        round(od / og   * 100, 1) if og   else 0,
    }


# ── Feature 2: Game Length vs Win Rate ───────────────────

def game_length_vs_winrate(
    db: Session, player_id: int,
    time_class: Optional[str] = None,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    player_color: Optional[str] = None,
    opening_names: Optional[str] = None,
):
    """Win rate bucketed by total game length in moves."""
    where, params = _build_game_filters(
        player_id, time_class, start_date, end_date, player_color, opening_names
    )
    sql = text(f"""
        SELECT
            g.total_moves,
            CASE
                WHEN (g.white_player_id = :player_id AND g.result = '1-0')
                  OR (g.black_player_id = :player_id AND g.result = '0-1') THEN 'win'
                WHEN g.result = '1/2-1/2'                                   THEN 'draw'
                ELSE 'loss'
            END AS outcome
        FROM games g
        WHERE {where}
          AND g.total_moves IS NOT NULL
    """)
    rows = db.execute(sql, params).mappings().all()

    bucket_defs = [
        ("1–10",   1,  10),
        ("11–20", 11,  20),
        ("21–30", 21,  30),
        ("31–40", 31,  40),
        ("41–50", 41,  50),
        ("51–60", 51,  60),
        ("61–80", 61,  80),
        ("80+",   81, 9999),
    ]
    buckets = {label: {"wins": 0, "losses": 0, "draws": 0} for label, _, _ in bucket_defs}

    for row in rows:
        moves, outcome = row["total_moves"], row["outcome"]
        for label, lo, hi in bucket_defs:
            if lo <= moves <= hi:
                if outcome == "win":
                    buckets[label]["wins"] += 1
                elif outcome == "loss":
                    buckets[label]["losses"] += 1
                else:
                    buckets[label]["draws"] += 1
                break

    results = []
    for label, _, _ in bucket_defs:
        b = buckets[label]
        total    = b["wins"] + b["losses"] + b["draws"]
        decisive = b["wins"] + b["losses"]
        results.append({
            "bucket":            label,
            "total_games":       total,
            "wins":              b["wins"],
            "losses":            b["losses"],
            "draws":             b["draws"],
            "win_rate":          round(b["wins"] / total    * 100, 1) if total    else 0,
            "win_rate_no_draws": round(b["wins"] / decisive * 100, 1) if decisive else 0,
            "draw_rate":         round(b["draws"] / total   * 100, 1) if total    else 0,
        })
    return results


# ── Clock Advantage ──────────────────────────────────────

def analyze_clock_advantage(
    db: Session, player_id: int,
    time_class: Optional[str] = None,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    player_color: Optional[str] = None,
    opening_names: Optional[str] = None,
):
    """
    Per-game average clock difference (player time − opponent time).
    Buckets games by whether the player was consistently ahead or behind.
    """
    where, params = _build_game_filters(
        player_id, time_class, start_date, end_date, player_color, opening_names
    )
    sql = text(f"""
        WITH player_games AS (
            SELECT
                g.game_id,
                g.result,
                CASE WHEN g.white_player_id = :player_id THEN 'white' ELSE 'black' END AS player_color,
                CASE
                    WHEN (g.white_player_id = :player_id AND g.result = '1-0')
                      OR (g.black_player_id = :player_id AND g.result = '0-1') THEN 'win'
                    WHEN g.result = '1/2-1/2'                                   THEN 'draw'
                    ELSE 'loss'
                END AS outcome
            FROM games g
            WHERE {where}
        ),
        player_clocks AS (
            SELECT m.game_id, m.move_number, m.clock_seconds AS player_clock
            FROM   moves m
            JOIN   player_games pg ON m.game_id = pg.game_id
            WHERE  m.color = pg.player_color
              AND  m.clock_seconds IS NOT NULL
        ),
        opp_clocks AS (
            SELECT m.game_id, m.move_number, m.clock_seconds AS opp_clock
            FROM   moves m
            JOIN   player_games pg ON m.game_id = pg.game_id
            WHERE  m.color != pg.player_color
              AND  m.clock_seconds IS NOT NULL
        ),
        game_advantages AS (
            SELECT   pc.game_id, AVG(pc.player_clock - oc.opp_clock) AS avg_advantage
            FROM     player_clocks pc
            JOIN     opp_clocks oc ON pc.game_id = oc.game_id AND pc.move_number = oc.move_number
            GROUP BY pc.game_id
        )
        SELECT
            CASE
                WHEN ga.avg_advantage < -30 THEN 'far_behind'
                WHEN ga.avg_advantage < -15 THEN 'behind'
                WHEN ga.avg_advantage <= 15 THEN 'even'
                WHEN ga.avg_advantage <= 30 THEN 'ahead'
                ELSE 'far_ahead'
            END AS clock_bucket,
            pg.outcome
        FROM game_advantages ga
        JOIN player_games pg ON ga.game_id = pg.game_id
    """)
    rows = db.execute(sql, params).mappings().all()

    buckets = {
        "far_behind": {"wins": 0, "losses": 0, "draws": 0},
        "behind":     {"wins": 0, "losses": 0, "draws": 0},
        "even":       {"wins": 0, "losses": 0, "draws": 0},
        "ahead":      {"wins": 0, "losses": 0, "draws": 0},
        "far_ahead":  {"wins": 0, "losses": 0, "draws": 0},
    }
    for row in rows:
        b = buckets[row["clock_bucket"]]
        outcome = row["outcome"]
        if outcome == "win":
            b["wins"] += 1
        elif outcome == "loss":
            b["losses"] += 1
        else:
            b["draws"] += 1

    result = []
    for label in ["far_behind", "behind", "even", "ahead", "far_ahead"]:
        b = buckets[label]
        total    = b["wins"] + b["losses"] + b["draws"]
        decisive = b["wins"] + b["losses"]
        result.append({
            "clock_bucket":      label,
            "total_games":       total,
            "wins":              b["wins"],
            "losses":            b["losses"],
            "draws":             b["draws"],
            "win_rate":          round(b["wins"] / total    * 100, 1) if total    else 0,
            "win_rate_no_draws": round(b["wins"] / decisive * 100, 1) if decisive else 0,
            "draw_rate":         round(b["draws"] / total   * 100, 1) if total    else 0,
        })
    return result


# ── Move Time Distribution & By Move Number ──────────────

def move_time_stats(
    db: Session, player_id: int,
    time_class: Optional[str] = None,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    player_color: Optional[str] = None,
    opening_names: Optional[str] = None,
):
    where, params = _build_game_filters(
        player_id, time_class, start_date, end_date, player_color, opening_names
    )
    sql = text(f"""
        WITH player_games AS (
            SELECT
                g.game_id,
                CASE WHEN g.white_player_id = :player_id THEN 'white' ELSE 'black' END AS player_color
            FROM games g
            WHERE {where}
        )
        SELECT m.move_number, m.time_spent_seconds
        FROM   moves m
        JOIN   player_games pg ON m.game_id = pg.game_id
        WHERE  m.color = pg.player_color
          AND  m.time_spent_seconds IS NOT NULL
          AND  m.time_spent_seconds >= 0
    """)
    rows = db.execute(sql, params).mappings().all()

    if not rows:
        return {"buckets": [], "mean": 0, "std_dev": 0, "median": 0, "total_moves": 0, "by_move_number": []}

    all_times: list[float] = []
    by_move: dict[int, list[float]] = {}
    for row in rows:
        t  = float(row["time_spent_seconds"])
        mn = int(row["move_number"])
        all_times.append(t)
        by_move.setdefault(mn, []).append(t)

    bucket_defs: list[tuple[str, float, float]]
    if time_class == "bullet":
        bucket_defs = [
            ("0–0.5s", 0,   0.5), ("0.5–1s", 0.5, 1),
            ("1–1.5s", 1,   1.5), ("1.5–2s", 1.5, 2),
            ("2–2.5s", 2,   2.5), ("2.5–3s", 2.5, 3),
            ("3s+",    3, 9999),
        ]
    elif time_class == "blitz":
        bucket_defs = [
            ("0–2s",  0,  2), ("2–4s",  2,  4), ("4–6s",  4,  6),
            ("6–8s",  6,  8), ("8–10s", 8, 10), ("10–12s", 10, 12),
            ("12s+",  12, 9999),
        ]
    else:
        bucket_defs = [
            ("0–5s",   0,   5), ("5–10s",  5,  10), ("10–15s", 10, 15),
            ("15–20s", 15, 20), ("20–25s", 20, 25), ("25–30s", 25, 30),
            ("30s+",   30, 9999),
        ]
    counts = {label: 0 for label, _, _ in bucket_defs}
    for t in all_times:
        for label, lo, hi in bucket_defs:
            if lo <= t < hi:
                counts[label] += 1
                break

    total = len(all_times)
    buckets = [
        {"label": label, "count": counts[label], "pct": round(counts[label] / total * 100, 1)}
        for label, _, _ in bucket_defs
    ]

    by_move_number = [
        {
            "move_number": mn,
            "avg_seconds": round(_stats.mean(by_move[mn]), 2),
            "count":       len(by_move[mn]),
        }
        for mn in sorted(by_move)
        if mn <= 100
    ]

    return {
        "buckets":        buckets,
        "mean":           round(_stats.mean(all_times), 2),
        "std_dev":        round(_stats.stdev(all_times) if total > 1 else 0.0, 2),
        "median":         round(_stats.median(all_times), 2),
        "total_moves":    total,
        "by_move_number": by_move_number,
    }


# ── Elo History ──────────────────────────────────────────

def elo_history(
    db: Session, player_id: int,
    time_class: Optional[str] = None,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
):
    """Player Elo over time, with IQR outlier filtering and same-day spreading."""
    where, params = _build_game_filters(player_id, time_class, start_date, end_date)
    sql = text(f"""
        SELECT
            g.date_played,
            CASE WHEN g.white_player_id = :player_id
                 THEN g.white_elo ELSE g.black_elo
            END AS elo,
            g.time_class
        FROM games g
        WHERE {where}
          AND g.date_played IS NOT NULL
          AND CASE WHEN g.white_player_id = :player_id
                   THEN g.white_elo ELSE g.black_elo
              END IS NOT NULL
        ORDER BY g.date_played ASC, g.game_id ASC
    """)
    rows = db.execute(sql, params).mappings().all()

    raw: list[dict[str, Any]] = [
        {"date": str(r["date_played"]), "elo": r["elo"], "time_class": r["time_class"]}
        for r in rows
    ]

    # Spread same-day games evenly across the day so they don't stack on the chart
    day_counts: dict[str, int] = {}
    for p in raw:
        day_counts[p["date"]] = day_counts.get(p["date"], 0) + 1

    day_seen: dict[str, int] = {}
    for p in raw:
        d = p["date"]
        n = day_counts[d]
        i = day_seen.get(d, 0)
        day_seen[d] = i + 1
        hours = (i / n) * 24
        h, m  = int(hours), int((hours % 1) * 60)
        p["date"] = f"{d}T{h:02d}:{m:02d}:00"

    return raw


# ── Top Openings ─────────────────────────────────────────

def get_top_openings(
    db: Session, player_id: int,
    time_class: Optional[str] = None,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    limit: int = 8,
):
    """Top N opening families for the player, split by color, with win/draw/loss stats and color totals."""
    result: dict = {"white": [], "black": [], "totals": {}}

    def _stats_entry(g, w, dr, lo):
        decisive = w + lo
        return {
            "games":             g,
            "wins":              w,
            "draws":             dr,
            "losses":            lo,
            "win_rate":          round(w / g * 100, 1) if g else 0.0,
            "draw_rate":         round(dr / g * 100, 1) if g else 0.0,
            "decisive_win_rate": round(w / decisive * 100, 1) if decisive else 0.0,
        }

    for color in ["white", "black"]:
        win_case  = "result = '1-0'" if color == "white" else "result = '0-1'"
        loss_case = "result = '0-1'" if color == "white" else "result = '1-0'"

        base_clauses = [f"{color}_player_id = :player_id"]
        params: dict[str, Any] = {"player_id": player_id}

        if time_class:
            base_clauses.append("time_class = :time_class")
            params["time_class"] = time_class
        if start_date:
            base_clauses.append("date_played >= :start_date")
            params["start_date"] = start_date
        if end_date:
            base_clauses.append("date_played <= :end_date")
            params["end_date"] = end_date

        # Total stats across all games for this color
        total_where = " AND ".join(base_clauses)
        tot = db.execute(text(f"""
            SELECT
                COUNT(*) AS games,
                SUM(CASE WHEN {win_case}          THEN 1 ELSE 0 END) AS wins,
                SUM(CASE WHEN result = '1/2-1/2'  THEN 1 ELSE 0 END) AS draws,
                SUM(CASE WHEN {loss_case}          THEN 1 ELSE 0 END) AS losses
            FROM   games
            WHERE  {total_where}
        """), params).mappings().first()
        if tot is not None:
            result["totals"][color] = _stats_entry(
                tot["games"] or 0, tot["wins"] or 0, tot["draws"] or 0, tot["losses"] or 0,
            )

        # Per-opening stats
        opening_where = " AND ".join(base_clauses + ["opening_name IS NOT NULL", "opening_name != ''"])
        rows = db.execute(text(f"""
            SELECT
                opening_name,
                COUNT(*) AS cnt,
                SUM(CASE WHEN {win_case}          THEN 1 ELSE 0 END) AS wins,
                SUM(CASE WHEN result = '1/2-1/2'  THEN 1 ELSE 0 END) AS draws,
                SUM(CASE WHEN {loss_case}          THEN 1 ELSE 0 END) AS losses
            FROM   games
            WHERE  {opening_where}
            GROUP BY opening_name
        """), params).mappings().all()

        family_data: dict[str, dict] = defaultdict(lambda: {"games": 0, "wins": 0, "draws": 0, "losses": 0})
        for r in rows:
            fam = _opening_family(r["opening_name"])
            family_data[fam]["games"]  += r["cnt"]
            family_data[fam]["wins"]   += r["wins"]
            family_data[fam]["draws"]  += r["draws"]
            family_data[fam]["losses"] += r["losses"]

        result[color] = [
            # name is the display label; filter is the raw-name prefix used
            # by the frontend's opening_names LIKE filter.
            {"name": _family_display_name(fam), "filter": fam,
             **_stats_entry(d["games"], d["wins"], d["draws"], d["losses"])}
            for fam, d in sorted(family_data.items(), key=lambda x: -x[1]["games"])[:limit]
        ]

    return result


# ── Win Rate by Color Over Time (daily rate + EMA) ──

def winrate_by_color_rolling(
    db: Session, player_id: int,
    time_class: Optional[str] = None,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    window_games: int = 30,
):
    """
    For each date that has games, the player's win rate and draw rate over
    their most recent `window_games` games as white, as black, and overall,
    as of the end of that day. window_games=1 shows the last game's result.
    """
    window_games = max(window_games, 1)

    # No lower date bound: the rolling window needs games played before
    # start_date; output rows are trimmed to the requested range below.
    clauses = ["(g.white_player_id = :player_id OR g.black_player_id = :player_id)"]
    params: dict[str, Any] = {"player_id": player_id}
    if time_class:
        clauses.append("g.time_class = :time_class")
        params["time_class"] = time_class
    if end_date:
        clauses.append("g.date_played <= :end_date")
        params["end_date"] = end_date

    where = " AND ".join(clauses)
    sql = text(f"""
        SELECT
            g.date_played,
            CASE WHEN g.white_player_id = :player_id THEN 1 ELSE 0 END AS is_white,
            CASE
                WHEN (g.white_player_id = :player_id AND g.result = '1-0')
                  OR (g.black_player_id = :player_id AND g.result = '0-1') THEN 1
                ELSE 0
            END AS is_win,
            CASE WHEN g.result = '1/2-1/2' THEN 1 ELSE 0 END AS is_draw
        FROM games g
        WHERE {where}
          AND g.date_played IS NOT NULL
        ORDER BY g.date_played ASC
    """)
    rows = db.execute(sql, params).mappings().all()

    if not rows:
        return []

    def _to_date(v) -> date:
        if isinstance(v, date):
            return v
        return datetime.strptime(str(v), "%Y-%m-%d").date()

    # Group games by date
    by_date: dict[date, list[dict]] = {}
    for r in rows:
        d = _to_date(r["date_played"])
        by_date.setdefault(d, []).append({
            "is_white": r["is_white"], "is_win": r["is_win"], "is_draw": r["is_draw"]
        })

    all_dates = sorted(by_date.keys())

    white_q: deque = deque(maxlen=window_games)
    black_q: deque = deque(maxlen=window_games)
    all_q:   deque = deque(maxlen=window_games)

    def rates(q: deque) -> tuple[float | None, float | None]:
        if not q:
            return None, None
        wins = sum(w for w, _ in q)
        draws = sum(d for _, d in q)
        return round(wins / len(q) * 100, 2), round(draws / len(q) * 100, 2)

    out = []
    for d in all_dates:
        for g in by_date[d]:
            result = (g["is_win"], g["is_draw"])
            all_q.append(result)
            (white_q if g["is_white"] else black_q).append(result)
        if start_date and d < start_date:
            continue
        white_wr, white_dr = rates(white_q)
        black_wr, black_dr = rates(black_q)
        all_wr, _ = rates(all_q)
        if white_wr is None and black_wr is None:
            continue
        out.append({
            "date":       str(d),
            "white":      white_wr,
            "black":      black_wr,
            "white_draw": white_dr,
            "black_draw": black_dr,
            "overall":    all_wr,
        })
    return out


def winrate_vs_first_move_rolling(
    db: Session, player_id: int,
    time_class: Optional[str] = None,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    window_games: int = 30,
):
    """
    For each date with games, the player's win rate and draw rate as black
    over their most recent `window_games` games against 1.e4 and 1.d4, as of
    the end of that day. window_games=1 shows the last game's result.
    """
    window_games = max(window_games, 1)

    # No lower date bound: the rolling window needs games played before
    # start_date; output rows are trimmed to the requested range below.
    clauses = ["g.black_player_id = :player_id", "m.ply = 1", "m.move_san IN ('e4', 'd4')"]
    params: dict[str, Any] = {"player_id": player_id}
    if time_class:
        clauses.append("g.time_class = :time_class")
        params["time_class"] = time_class
    if end_date:
        clauses.append("g.date_played <= :end_date")
        params["end_date"] = end_date

    where = " AND ".join(clauses)
    sql = text(f"""
        SELECT
            g.date_played,
            m.move_san AS first_move,
            CASE WHEN g.result = '0-1' THEN 1 ELSE 0 END AS is_win,
            CASE WHEN g.result = '1/2-1/2' THEN 1 ELSE 0 END AS is_draw
        FROM games g
        JOIN moves m ON m.game_id = g.game_id
        WHERE {where}
          AND g.date_played IS NOT NULL
        ORDER BY g.date_played ASC
    """)
    rows = db.execute(sql, params).mappings().all()

    if not rows:
        return []

    def _to_date(v) -> date:
        if isinstance(v, date):
            return v
        return datetime.strptime(str(v), "%Y-%m-%d").date()

    by_date: dict[date, dict[str, list]] = {}
    for r in rows:
        d = _to_date(r["date_played"])
        entry = by_date.setdefault(d, {"e4": [], "d4": []})
        entry[r["first_move"]].append({"is_win": r["is_win"], "is_draw": r["is_draw"]})

    all_dates = sorted(by_date.keys())

    e4_q: deque = deque(maxlen=window_games)
    d4_q: deque = deque(maxlen=window_games)

    def rates(q: deque) -> tuple[float | None, float | None]:
        if not q:
            return None, None
        wins = sum(w for w, _ in q)
        draws = sum(d for _, d in q)
        return round(wins / len(q) * 100, 2), round(draws / len(q) * 100, 2)

    out = []
    for d in all_dates:
        for move, q in (("e4", e4_q), ("d4", d4_q)):
            for g in by_date[d][move]:
                q.append((g["is_win"], g["is_draw"]))
        if start_date and d < start_date:
            continue
        e4_wr, e4_dr = rates(e4_q)
        d4_wr, d4_dr = rates(d4_q)
        if e4_wr is None and d4_wr is None:
            continue
        out.append({
            "date":    str(d),
            "e4":      e4_wr,
            "d4":      d4_wr,
            "e4_draw": e4_dr,
            "d4_draw": d4_dr,
        })
    return out


# ── Win Rate After a Streak ──────────────────────────────

def streak_reaction(
    db: Session, player_id: int,
    time_class: Optional[str] = None,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
):
    """
    Win/loss/draw outcomes bucketed by how many consecutive losses (or wins)
    immediately preceded each game. Draws don't break a streak in progress —
    they're skipped over for counting purposes — but the draw's own outcome
    is still bucketed against whatever streak was active when it happened.

    Streaks reset at the start of each new calendar day in US Eastern time
    (tilt doesn't survive a night's sleep). ET days are derived from
    end_time (epoch UTC); games missing end_time fall back to date_played,
    a UTC calendar date, whose midnight is 7-8pm ET.

    No lower date bound at the SQL level (same reasoning as
    winrate_by_color_rolling): the streak needs games before start_date to
    know its state entering the window, so we compute over full history up
    to end_date and trim the bucketed output to start_date afterward.
    """
    clauses = ["(g.white_player_id = :player_id OR g.black_player_id = :player_id)",
               "g.date_played IS NOT NULL"]
    params: dict[str, Any] = {"player_id": player_id}
    if time_class:
        clauses.append("g.time_class = :time_class")
        params["time_class"] = time_class
    if end_date:
        clauses.append("g.date_played <= :end_date")
        params["end_date"] = end_date

    where = " AND ".join(clauses)
    sql = text(f"""
        SELECT
            g.date_played,
            g.end_time,
            CASE
                WHEN (g.white_player_id = :player_id AND g.result = '1-0')
                  OR (g.black_player_id = :player_id AND g.result = '0-1') THEN 'win'
                WHEN g.result = '1/2-1/2'                                   THEN 'draw'
                ELSE 'loss'
            END AS outcome
        FROM games g
        WHERE {where}
        ORDER BY g.date_played ASC, g.end_time ASC, g.game_id ASC
    """)
    rows = db.execute(sql, params).mappings().all()

    def _to_date(v) -> date:
        if isinstance(v, date):
            return v
        return datetime.strptime(str(v), "%Y-%m-%d").date()

    eastern = ZoneInfo("America/New_York")

    def _local_day(row) -> date:
        if row["end_time"] is not None:
            return datetime.fromtimestamp(row["end_time"], tz=eastern).date()
        return _to_date(row["date_played"])

    loss_buckets = {n: {"wins": 0, "losses": 0, "draws": 0} for n in (1, 2, 3, 4)}
    win_buckets  = {n: {"wins": 0, "losses": 0, "draws": 0} for n in (1, 2, 3, 4)}

    loss_streak = 0
    win_streak  = 0
    prev_day: Optional[date] = None
    for row in rows:
        outcome = row["outcome"]
        in_range = not start_date or _to_date(row["date_played"]) >= start_date

        day = _local_day(row)
        if day != prev_day:
            loss_streak = 0
            win_streak = 0
        prev_day = day

        if in_range and loss_streak >= 1:
            b = loss_buckets[min(loss_streak, 4)]
            b["wins" if outcome == "win" else "losses" if outcome == "loss" else "draws"] += 1
        if in_range and win_streak >= 1:
            b = win_buckets[min(win_streak, 4)]
            b["wins" if outcome == "win" else "losses" if outcome == "loss" else "draws"] += 1

        if outcome == "loss":
            loss_streak += 1
            win_streak = 0
        elif outcome == "win":
            win_streak += 1
            loss_streak = 0
        # draw: both streaks carry through unchanged

    def _build(buckets: dict) -> list[dict[str, Any]]:
        results = []
        for n in (1, 2, 3, 4):
            b = buckets[n]
            total    = b["wins"] + b["losses"] + b["draws"]
            decisive = b["wins"] + b["losses"]
            results.append({
                "bucket":            f"{n}" if n < 4 else "4+",
                "total_games":       total,
                "wins":              b["wins"],
                "losses":            b["losses"],
                "draws":             b["draws"],
                "win_rate":          round(b["wins"] / total    * 100, 1) if total    else 0,
                "win_rate_no_draws": round(b["wins"] / decisive * 100, 1) if decisive else 0,
                "draw_rate":         round(b["draws"] / total   * 100, 1) if total    else 0,
            })
        return results

    return {
        "after_loss": _build(loss_buckets),
        "after_win":  _build(win_buckets),
    }
