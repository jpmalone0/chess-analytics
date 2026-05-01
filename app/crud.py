"""
Database query functions — all queries written as explicit SQL using sqlalchemy.text().
"""

import statistics as _stats
from datetime import date
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

# ═══════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════

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
            placeholders = ", ".join(f":op_{i}" for i in range(len(ops)))
            clauses.append(f"g.opening_name IN ({placeholders})")
            for i, op in enumerate(ops):
                params[f"op_{i}"] = op

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
):
    where, params = _build_game_filters(player_id, time_class, start_date, end_date)
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
    """Win/loss/draw counts bucketed by Elo gap (player Elo − opponent Elo)."""
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
        diff, outcome = row["elo_diff"], row["outcome"]
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

    underdog_labels = {"< -100", "-100 to -50", "-50 to -40", "-40 to -30", "-30 to -20", "-20 to -10"}
    favored_labels  = {"+10 to +20", "+20 to +30", "+30 to +40", "+40 to +50", "+50 to +100", "> +100"}
    even_labels     = {"-10 to 0", "0 to +10"}

    ug   = sum(r["total_games"] for r in results if r["bucket"] in underdog_labels)
    uw   = sum(r["wins"]        for r in results if r["bucket"] in underdog_labels)
    fg   = sum(r["total_games"] for r in results if r["bucket"] in favored_labels)
    fw   = sum(r["wins"]        for r in results if r["bucket"] in favored_labels)
    eg   = sum(r["total_games"] for r in results if r["bucket"] in even_labels)
    ew   = sum(r["wins"]        for r in results if r["bucket"] in even_labels)
    og   = sum(r["total_games"] for r in results)
    ow   = sum(r["wins"]        for r in results)
    od   = sum(r["draws"]       for r in results)
    odec = sum(r["wins"] + r["losses"] for r in results)

    return {
        "buckets":                  results,
        "upset_rate":               round(uw / ug   * 100, 1) if ug   else 0,
        "hold_rate":                round(fw / fg   * 100, 1) if fg   else 0,
        "even_rate":                round(ew / eg   * 100, 1) if eg   else 0,
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

    bucket_defs = [
        ("< 1s",   0,    1),
        ("1–3s",   1,    3),
        ("3–5s",   3,    5),
        ("5–10s",  5,   10),
        ("10–20s", 10,  20),
        ("20–30s", 20,  30),
        ("30–60s", 30,  60),
        ("60s+",   60, 9999),
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

    # IQR outlier filter — removes cross-time-class spikes from inactivity periods
    if len(raw) >= 5:
        elos = sorted(p["elo"] for p in raw)
        n  = len(elos)
        q1 = elos[n // 4]
        q3 = elos[3 * n // 4]
        iqr = q3 - q1
        if iqr > 0:
            lo, hi = q1 - 2.0 * iqr, q3 + 2.0 * iqr
            raw = [p for p in raw if lo <= p["elo"] <= hi]

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
    limit: int = 5,
):
    """Top N opening names for the player, split by color."""
    result: dict[str, list[str]] = {"white": [], "black": []}

    for color in ["white", "black"]:
        clauses = [
            f"{color}_player_id = :player_id",
            "opening_name IS NOT NULL",
            "opening_name != ''",
        ]
        params: dict[str, Any] = {"player_id": player_id, "limit": limit}

        if time_class:
            clauses.append("time_class = :time_class")
            params["time_class"] = time_class
        if start_date:
            clauses.append("date_played >= :start_date")
            params["start_date"] = start_date
        if end_date:
            clauses.append("date_played <= :end_date")
            params["end_date"] = end_date

        where = " AND ".join(clauses)
        sql = text(f"""
            SELECT   opening_name
            FROM     games
            WHERE    {where}
            GROUP BY opening_name
            ORDER BY COUNT(*) DESC
            LIMIT    :limit
        """)
        result[color] = [
            r["opening_name"]
            for r in db.execute(sql, params).mappings().all()
        ]

    return result
