"""
Population baselines — Elo-bucketed "average player" reference data.

Every function here builds on one shared CTE that turns the games table into a
set of player-game rows (both sides of each game), filtered to an Elo band and
time control, with the searched player removed and each remaining player capped
at PER_PLAYER_CAP games. The cap matters: without it a single heavily-synced
account defines its whole bracket.
"""

from datetime import date
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


# Half-widths tried in order when a derived band is too thin.
BAND_WIDENING = [0, 100, 200]


def _player_filter_sql(
    time_class: Optional[str] = None,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    player_color: Optional[str] = None,
    opening_names: Optional[str] = None,
) -> tuple[str, dict]:
    """WHERE clause over the searched player's own games. Mirrors
    crud._build_game_filters but is duplicated deliberately: this module must
    stay free of crud's per-player concerns, and the two will diverge."""
    clauses: list[str] = []
    params: dict[str, Any] = {}

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
            likes = [f"g.opening_name LIKE :pop_{i}" for i in range(len(ops))]
            clauses.append("(" + " OR ".join(likes) + ")")
            for i, op in enumerate(ops):
                params[f"pop_{i}"] = op + "%"

    return " AND ".join(clauses), params


def dominant_time_control(db: Session, player_id: int, **filters) -> Optional[str]:
    """The player's modal time_control under the current filters.

    The UI only exposes time_class, but time-based baselines need an exact
    control — 3+0 and 10+0 have nothing to say to each other. So we derive it.
    """
    where, params = _player_filter_sql(**filters)
    params["player_id"] = player_id
    row = db.execute(text(f"""
        SELECT g.time_control, COUNT(*) AS n
        FROM   games g
        WHERE  {where} AND g.time_control IS NOT NULL
        GROUP  BY g.time_control
        ORDER  BY n DESC
        LIMIT  1
    """), params).mappings().first()
    return row["time_control"] if row else None


def player_median_elo(db: Session, player_id: int, **filters) -> Optional[int]:
    """Median of the player's OWN Elo across their filtered games."""
    where, params = _player_filter_sql(**filters)
    params["player_id"] = player_id
    rows = db.execute(text(f"""
        SELECT CASE WHEN g.white_player_id = :player_id THEN g.white_elo ELSE g.black_elo END AS elo
        FROM   games g
        WHERE  {where}
        ORDER  BY elo
    """), params).scalars().all()
    elos = [e for e in rows if e is not None]
    if not elos:
        return None
    return int(elos[len(elos) // 2])


def _band_is_viable(counts: dict) -> bool:
    return counts["n_players"] >= MIN_PLAYERS and counts["n_games"] >= MIN_GAMES


def resolve_band(
    db: Session,
    player_id: int,
    time_class: Optional[str] = None,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    player_color: Optional[str] = None,
    opening_names: Optional[str] = None,
    selected_band: Optional[int] = None,
) -> Optional[dict]:
    """
    Decide which population slice to compare against.

    Escalation order, per spec: exact time control -> time class, and only then
    widen the Elo band. Blurring the context beats blurring the comparison.
    A selected band never widens; a derived one may.

    Returns None when nothing viable exists — the caller renders no overlay.
    """
    player_filters = dict(
        time_class=time_class, start_date=start_date, end_date=end_date,
        player_color=player_color, opening_names=opening_names,
    )

    if selected_band is not None:
        base_lo = selected_band
        source = "selected"
    else:
        median = player_median_elo(db, player_id, **player_filters)
        if median is None:
            return None
        base_lo = (median // 100) * 100
        source = "derived"

    exact_tc = dominant_time_control(db, player_id, **player_filters)
    widths = [0] if source == "selected" else BAND_WIDENING

    # Time control blurs first, then the band widens.
    for tc, tc_fallback in ((exact_tc, False), (None, True)):
        if tc is None and not tc_fallback:
            continue
        for width in widths:
            lo, hi = base_lo - width, base_lo + 99 + width
            counts = population_counts(
                db, elo_lo=lo, elo_hi=hi, exclude_player_id=player_id,
                time_control=tc, time_class=time_class,
                player_color=player_color, opening_names=opening_names,
            )
            if _band_is_viable(counts):
                return {
                    "elo_lo": lo,
                    "elo_hi": hi,
                    "time_control": tc,
                    "time_class": time_class,
                    "n_players": counts["n_players"],
                    "n_games": counts["n_games"],
                    "widened": width > 0,
                    "tc_fallback": tc_fallback,
                    "source": source,
                }
    return None


def available_bands(
    db: Session,
    player_id: int,
    time_class: Optional[str] = None,
    time_control: Optional[str] = None,
    player_color: Optional[str] = None,
    opening_names: Optional[str] = None,
) -> list[dict]:
    """
    Every 100-band that clears the min-sample floor, for the dropdown.

    One grouped pass rather than a probe per band. Bands below the floor are
    omitted entirely, so the dropdown can never offer a band that renders empty
    — and the resulting gaps are themselves informative about the corpus.

    The cap here applies across all bands for a player rather than within each
    band, so counts can only understate what resolve_band would find. That is
    the safe direction: the dropdown never offers a band that then comes back
    empty.
    """
    cte, params = _population_cte(
        elo_lo=0, elo_hi=4000, exclude_player_id=player_id,
        time_control=time_control, time_class=time_class,
        player_color=player_color, opening_names=opening_names,
    )
    params["min_players"] = MIN_PLAYERS
    params["min_games"] = MIN_GAMES

    rows = db.execute(text(f"""
        WITH {cte},
        banded AS (
            SELECT pop.pid,
                   (CASE WHEN pop.side = 'white' THEN g.white_elo ELSE g.black_elo END / 100) * 100 AS elo_lo
            FROM   pop JOIN games g ON g.game_id = pop.game_id
        )
        SELECT elo_lo,
               COUNT(DISTINCT pid) AS n_players,
               COUNT(*)            AS n_games
        FROM   banded
        GROUP  BY elo_lo
        HAVING n_players >= :min_players AND n_games >= :min_games
        ORDER  BY elo_lo
    """), params).mappings().all()

    return [
        {
            "elo_lo": int(r["elo_lo"]),
            "elo_hi": int(r["elo_lo"]) + 99,
            "n_players": r["n_players"],
            "n_games": r["n_games"],
        }
        for r in rows
    ]


def band_meta(band: dict) -> dict:
    """The band description the frontend puts in the chart label."""
    return {
        "elo_band": [band["elo_lo"], band["elo_hi"]],
        "time_control": band["time_control"],
        "time_class": band["time_class"],
        "n_players": band["n_players"],
        "n_games": band["n_games"],
        "widened": band["widened"],
        "tc_fallback": band["tc_fallback"],
        "source": band["source"],
    }
