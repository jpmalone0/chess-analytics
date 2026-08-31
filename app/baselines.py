"""
Population baselines — Elo-bucketed "average player" reference data.

Every function here builds on one shared CTE that turns the games table into a
set of player-game rows (both sides of each game), filtered to an Elo band and
time control, with the searched player removed and each remaining player capped
at PER_PLAYER_CAP games. The cap matters: without it a single heavily-synced
account defines its whole bracket.
"""

from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

# A band is usable only above both floors.
MIN_PLAYERS = 30
MIN_GAMES = 500

# No single player may contribute more than this to a band.
PER_PLAYER_CAP = 100


def _population_cte(
    elo_lo: int,
    elo_hi: int,
    exclude_player_id: int,
    time_control: Optional[str] = None,
    time_class: Optional[str] = None,
    player_color: Optional[str] = None,
    opening_names: Optional[str] = None,
) -> tuple[str, dict]:
    """
    Build the shared CTE chain and its params. Callers prefix it with WITH and
    then select from `pop`, which yields (game_id, pid, side).

    time_control wins over time_class when both are given — exact keying first,
    class-level fallback second.
    """
    params: dict[str, Any] = {
        "elo_lo": elo_lo,
        "elo_hi": elo_hi,
        "exclude_pid": exclude_player_id,
        "cap": PER_PLAYER_CAP,
    }

    shared: list[str] = []
    if time_control:
        shared.append("g.time_control = :time_control")
        params["time_control"] = time_control
    elif time_class:
        shared.append("g.time_class = :time_class")
        params["time_class"] = time_class

    if opening_names:
        ops = [o.strip() for o in opening_names.split("|") if o.strip()]
        if ops:
            likes = [f"g.opening_name LIKE :bop_{i}" for i in range(len(ops))]
            shared.append("(" + " OR ".join(likes) + ")")
            for i, op in enumerate(ops):
                params[f"bop_{i}"] = op + "%"

    shared_sql = (" AND " + " AND ".join(shared)) if shared else ""

    # player_color mirrors the player's own filter: if they are looking at their
    # games as white, the population is other people's white games.
    sides: list[str] = []
    if player_color != "black":
        sides.append(f"""
            SELECT g.game_id, g.white_player_id AS pid, 'white' AS side, g.end_time
            FROM   games g
            WHERE  g.white_elo >= :elo_lo AND g.white_elo <= :elo_hi
              AND  g.white_player_id != :exclude_pid{shared_sql}""")
    if player_color != "white":
        sides.append(f"""
            SELECT g.game_id, g.black_player_id AS pid, 'black' AS side, g.end_time
            FROM   games g
            WHERE  g.black_elo >= :elo_lo AND g.black_elo <= :elo_hi
              AND  g.black_player_id != :exclude_pid{shared_sql}""")

    cte = f"""
        sides AS ({" UNION ALL ".join(sides)}),
        capped AS (
            SELECT game_id, pid, side,
                   ROW_NUMBER() OVER (PARTITION BY pid ORDER BY end_time DESC) AS rn
            FROM   sides
        ),
        pop AS (SELECT game_id, pid, side FROM capped WHERE rn <= :cap)
    """
    return cte, params


def population_counts(
    db: Session,
    elo_lo: int,
    elo_hi: int,
    exclude_player_id: int,
    time_control: Optional[str] = None,
    time_class: Optional[str] = None,
    player_color: Optional[str] = None,
    opening_names: Optional[str] = None,
) -> dict:
    """Distinct players and capped games in a band. Drives the min-sample gate."""
    cte, params = _population_cte(
        elo_lo, elo_hi, exclude_player_id,
        time_control, time_class, player_color, opening_names,
    )
    row = db.execute(
        text(f"WITH {cte} SELECT COUNT(DISTINCT pid) AS n_players, COUNT(*) AS n_games FROM pop"),
        params,
    ).mappings().one()
    return {"n_players": row["n_players"], "n_games": row["n_games"]}
