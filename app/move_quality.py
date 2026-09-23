"""Every query the move-quality section makes.

Kept out of crud.py, which is already 1,416 lines. The queries here all cross
the ATTACH boundary -- which statements may do even though view definitions may
not -- so they run on a connection that has the sidecar attached.
"""

from datetime import date
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from app import baselines, crud
from engine.db import attach_engine_db
from engine.population import POPULATION_PER_PLAYER_CAP, band_sides_sql
from engine.views import MISS_HANDED_WP, MISS_RETURNED_WP

# A game analyzed under several runs would otherwise appear once per run.
# Newest run wins: it is the deepest search anybody has pointed at that game.
_LATEST_RUN = """
    q.run_id = (SELECT MAX(c.run_id) FROM engine.game_coverage c
                WHERE c.game_id = q.game_id AND c.status = 'complete')
"""

# The same rule for a query that has already pinned the game, where a
# correlated subquery over q would be a self-reference into the CTE below.
_LATEST_RUN_FOR_GAME = """
    (SELECT MAX(c.run_id) FROM engine.game_coverage c
     WHERE c.game_id = :game_id AND c.status = 'complete')
"""

# move_quality's Miss rule, restated over a pre-filtered move_severity.
#
# This duplicates engine.views.MOVE_QUALITY_VIEW's LEFT JOIN, which is not free
# and is not an accident. move_quality is a self-join over move_severity, and
# move_severity is CTEs over move_evals, itself a self-join over
# position_evals; SQLite materialises the join's `prev` side without pushing an
# outer WHERE down, so `SELECT ... FROM move_quality WHERE game_id = ?` costs
# proportional to the analyzed corpus rather than to the game. Measured against
# the real sidecar at 102,790 plies: 142ms through the view, 0.2ms with the
# filter pushed inside. A view cannot know the caller's filter, so the only
# place the pushdown can happen is here. The thresholds are imported rather
# than retyped so that retuning a Miss still costs exactly one edit.
_MISS = (
    f"COALESCE(prev.wp_loss >= {MISS_HANDED_WP}"
    f" AND s.wp_loss >= {MISS_RETURNED_WP}, 0)"
)


def _empty() -> dict[str, Any]:
    return {
        "games": [],
        "totals": _totals([], ""),
        "opponents": {**_totals([], "opp_"), "avg_elo": None},
    }


def player_move_quality(
    db: Session,
    player_id: int,
    time_class: Optional[str] = None,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    player_color: Optional[str] = None,
    opening_names: Optional[str] = None,
    tz: Optional[str] = None,
) -> dict[str, Any]:
    """Per-game counts for the searched player's own moves, newest first."""
    attach_engine_db(db.connection())

    where, params = crud._build_game_filters(
        player_id=player_id, time_class=time_class,
        start_date=start_date, end_date=end_date,
        player_color=player_color, opening_names=opening_names, tz=tz,
    )
    params["player_id"] = player_id

    rows = db.execute(text(f"""
        SELECT g.game_id, g.date_played, g.time_class, g.opening_name,
               g.chess_com_url, q.color, q.moves_scored,
               q.inaccuracies, q.mistakes, q.blunders, q.misses,
               o.moves_scored AS opp_moves_scored,
               o.inaccuracies AS opp_inaccuracies, o.mistakes AS opp_mistakes,
               o.blunders AS opp_blunders, o.misses AS opp_misses,
               CASE WHEN g.white_player_id = :player_id
                    THEN g.black_elo ELSE g.white_elo END AS opp_elo
        FROM   games g
        JOIN   engine.game_move_quality q ON q.game_id = g.game_id
        -- The other seat of the same game, under the same run. LEFT because a
        -- side with no scored moves has no row, and the game still counts.
        LEFT JOIN engine.game_move_quality o
               ON o.game_id = q.game_id AND o.run_id = q.run_id
              AND o.color <> q.color
        WHERE  {where}
          AND  {_LATEST_RUN}
          AND  q.color = CASE WHEN g.white_player_id = :player_id
                              THEN 'white' ELSE 'black' END
        ORDER  BY g.end_time DESC, g.date_played DESC, g.game_id DESC
    """), params).mappings().all()

    if not rows:
        return _empty()

    games = [dict(r) for r in rows]
    elos = [g["opp_elo"] for g in games if g["opp_elo"] is not None]
    return {
        "games": games,
        "totals": _totals(games, ""),
        # The opponent mirror: the same games from the other seat. Not an
        # independent baseline -- both sides share every position, and a Miss
        # needs the other side's error the ply before -- but it is rated like
        # the player and filtered exactly like them.
        "opponents": {
            **_totals(games, "opp_"),
            "avg_elo": round(sum(elos) / len(elos)) if elos else None,
        },
    }


def _totals(games: list[dict[str, Any]], prefix: str) -> dict[str, Any]:
    return {
        "games_analyzed": len(games),
        **{k: sum(g[prefix + k] or 0 for g in games)
           for k in ("moves_scored", "inaccuracies", "mistakes", "blunders", "misses")},
    }


def game_drill_list(db: Session, game_id: int) -> dict[str, Any]:
    """Every flagged move in one game, both sides, in order.

    Unflagged moves are omitted: a 90-ply game yields perhaps eight rows, and
    the rest carry no information the section is trying to show.

    Reads move_severity and rebuilds the Miss flag rather than reading
    move_quality, so that the game filter lands before the self-join. See
    _MISS above for the measurement that forces it.
    """
    attach_engine_db(db.connection())

    rows = db.execute(text(f"""
        WITH scored AS MATERIALIZED (
            SELECT run_id, game_id, ply, color, wp_before, wp_after, wp_loss, tier
            FROM   engine.move_severity
            WHERE  game_id = :game_id
              AND  run_id  = {_LATEST_RUN_FOR_GAME}
        ),
        q AS (
            SELECT s.game_id, s.ply, s.color, s.tier,
                   s.wp_before, s.wp_after, s.wp_loss,
                   {_MISS} AS is_miss
            FROM      scored AS s
            LEFT JOIN scored AS prev ON prev.ply = s.ply - 1
        )
        SELECT q.ply, q.color, q.tier, q.is_miss,
               q.wp_before, q.wp_after, q.wp_loss,
               m.move_number, m.move_san, m.clock_seconds, m.time_spent_seconds
        FROM   q
        JOIN   moves m ON m.game_id = q.game_id AND m.ply = q.ply
        WHERE  q.tier IS NOT NULL OR q.is_miss = 1
        ORDER  BY q.ply
    """), {"game_id": game_id}).mappings().all()

    return {"game_id": game_id, "moves": [dict(r) for r in rows]}


# ═══════════════════════════════════════════════════════════
# Population
# ═══════════════════════════════════════════════════════════



def resolve_mq_band(
    db: Session,
    player_id: int,
    elo_band: Optional[str],
    time_class: Optional[str] = None,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    player_color: Optional[str] = None,
    opening_names: Optional[str] = None,
    tz: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    """The band the Compare-to selector names, within one time class.

    Not baselines.resolve_band: that one widens a band until the *corpus* is
    thick enough, and here the question is what has been engine-analyzed. A
    widened band would name games the button never sampled.

    A time class is always settled first, and a derived band comes from the
    median within it. On "All", pooling classes picks a band the player never
    plays in: at 90 days their pooled median is rapid's, 448 points above
    their bullet.
    """
    filters = dict(
        start_date=start_date, end_date=end_date,
        player_color=player_color, opening_names=opening_names, tz=tz,
    )
    tc = time_class or baselines.dominant_time_class(db, player_id, **filters)
    if tc is None:
        return None
    if elo_band == "all":
        return {"time_class": tc, "elo_lo": 0, "elo_hi": 4000, "source": "all"}
    if elo_band:
        lo = int(elo_band)
        return {"time_class": tc, "elo_lo": lo, "elo_hi": lo + 99, "source": "selected"}
    median = baselines.player_median_elo(db, player_id, time_class=tc, **filters)
    if median is None:
        return None
    lo = (median // 100) * 100
    return {"time_class": tc, "elo_lo": lo, "elo_hi": lo + 99, "source": "derived"}


def band_move_quality(
    db: Session,
    band: dict[str, Any],
    exclude_player_id: int,
    player_color: Optional[str] = None,
    opening_names: Optional[str] = None,
) -> dict[str, Any]:
    """Pooled rates over the analyzed in-band sides, capped per player.

    Only the in-band side's moves count; counting both would make it a rate
    over games in the band, not over players in it. The same cap and exclusion
    as the sampler, so the rate describes the games the button analyzed.
    """
    attach_engine_db(db.connection())
    params: dict[str, Any] = {
        "time_class": band["time_class"], "elo_lo": band["elo_lo"],
        "elo_hi": band["elo_hi"], "exclude_player_id": exclude_player_id,
        "cap": POPULATION_PER_PLAYER_CAP,
    }
    extra = ""
    if player_color in ("white", "black"):
        extra += " AND s.color = :player_color"
        params["player_color"] = player_color
    if opening_names:
        ops = [o.strip() for o in opening_names.split("|") if o.strip()]
        if ops:
            extra += " AND (" + " OR ".join(
                f"g.opening_name LIKE :op_{i}" for i in range(len(ops))) + ")"
            params.update({f"op_{i}": op + "%" for i, op in enumerate(ops)})

    row = db.execute(text(f"""
        WITH sides AS ({band_sides_sql()}),
        scored AS (
            SELECT s.pid, s.game_id, q.moves_scored,
                   q.inaccuracies, q.mistakes, q.blunders, q.misses,
                   ROW_NUMBER() OVER (PARTITION BY s.pid ORDER BY s.h, s.game_id) AS rn
            FROM   sides s
            JOIN   games g ON g.game_id = s.game_id
            JOIN   engine.game_move_quality q
                   ON q.game_id = s.game_id AND q.color = s.color
            WHERE  {_LATEST_RUN} {extra}
        )
        SELECT COUNT(DISTINCT pid) AS n_players, COUNT(DISTINCT game_id) AS n_games,
               COALESCE(SUM(moves_scored), 0) AS moves_scored,
               COALESCE(SUM(inaccuracies), 0) AS inaccuracies,
               COALESCE(SUM(mistakes), 0) AS mistakes,
               COALESCE(SUM(blunders), 0) AS blunders,
               COALESCE(SUM(misses), 0) AS misses
        FROM   scored WHERE rn <= :cap
    """), params).mappings().one()

    curve = db.execute(
        text("SELECT 1 FROM engine.wp_curve WHERE time_class = :tc"),
        {"tc": band["time_class"]},
    ).first()
    totals = dict(row)
    return {
        "band": band,
        "totals": totals,
        "viable": baselines._band_is_viable(totals),
        "curve_fitted": curve is not None,
    }
