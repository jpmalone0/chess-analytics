"""Read path for the style panel.

Every function takes a connection rather than reaching for a global, so tests
can pass an in-memory database holding both schemas. Same pattern as
engine/scope.py.

These axes measure STYLE, NOT ABILITY -- correlation with Elo is 0.02-0.08.
Nothing here may be presented as better or worse.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from sqlalchemy import text

#: Display order for the axes.
AXES = ("space", "mobility", "king_safety", "pawn_structure")

#: Schema prefix for the sidecar tables; empty in tests. See build_features.
SCHEMA = "engine."


@dataclass
class AxisValue:
    """One axis: the centred mean and how precisely it is known."""

    mean: float
    se: float


@dataclass
class Vector:
    """A player's style over some set of games."""

    n: int = 0
    axes: dict[str, AxisValue] = field(default_factory=dict)


def subject_vector(
    conn,
    player_id: int,
    time_class: str | None = None,
    extra_clause: str = "",
    params: dict | None = None,
    limit_game_ids: list[int] | None = None,
) -> Vector:
    """The player's centred vector over the games the filters select.

    extra_clause and params come from crud._build_game_filters, so the panel
    cannot drift from the rest of the UI. The games table is aliased 'g' there,
    and is aliased 'g' here for that reason.

    Standard error is SD/sqrt(n), with SD taken as sqrt(E[x^2] - E[x]^2) because
    SQLite has no STDDEV. At small n the resulting interval is very wide, which
    is the correct display rather than a problem to hide.
    """
    params = dict(params or {})
    params["player_id"] = player_id

    selects = []
    for axis in AXES:
        centred = f"(f.{axis} - COALESCE(c.{axis}, cf.{axis}))"
        selects.append(f"AVG({centred}) AS {axis}_mean")
        selects.append(f"AVG({centred} * {centred}) AS {axis}_sq")

    clauses = ["g.variant IS NULL"]
    if time_class:
        clauses.append("g.time_class = :time_class")
        params["time_class"] = time_class
    if extra_clause:
        clauses.append(extra_clause)
    if limit_game_ids is not None:
        ids = ",".join(str(int(g)) for g in limit_game_ids) or "NULL"
        clauses.append(f"g.game_id IN ({ids})")

    row = conn.execute(text(f"""
        SELECT COUNT(*) AS n, {', '.join(selects)}
        FROM games g
        JOIN {SCHEMA}position_features f
          ON f.game_id = g.game_id
         AND f.color = CASE WHEN g.white_player_id = :player_id
                            THEN 'white' ELSE 'black' END
        LEFT JOIN {SCHEMA}style_cell_means c
          ON c.time_class = g.time_class AND c.color = f.color
         AND c.eco3 = SUBSTR(COALESCE(g.eco, '?'), 1, 3)
        LEFT JOIN {SCHEMA}style_cell_means cf
          ON cf.time_class = g.time_class AND cf.color = f.color AND cf.eco3 = '*'
        WHERE (g.white_player_id = :player_id OR g.black_player_id = :player_id)
          AND {' AND '.join(clauses)}"""), params).mappings().first()

    if not row or not row["n"]:
        return Vector()

    n = int(row["n"])
    axes = {}
    for axis in AXES:
        mean = float(row[f"{axis}_mean"])
        # Floating-point error can push a zero variance slightly negative.
        variance = max(0.0, float(row[f"{axis}_sq"]) - mean * mean)
        axes[axis] = AxisValue(mean=mean, se=math.sqrt(variance / n))
    return Vector(n=n, axes=axes)


#: 95% interval. Two-sided normal approximation.
Z_95 = 1.96


def _reference_values(conn, time_class: str) -> dict[str, list[float]]:
    """Every reference player's value per axis, sorted, for that time class.

    The percentile reference is EVERY player with a vector -- not the 2800+ pool
    used for similarity. They answer different questions and conflating them is
    the mistake this project has already made four times.
    """
    rows = conn.execute(text(
        f"SELECT {', '.join(AXES)} FROM {SCHEMA}player_style_vectors "
        "WHERE time_class = :tc"), {"tc": time_class}).mappings().all()
    return {axis: sorted(float(r[axis]) for r in rows) for axis in AXES}


def _rank(sorted_values: list[float], value: float) -> int:
    """Percentile of value within sorted_values, 0-100."""
    if not sorted_values:
        return 0
    below = sum(1 for v in sorted_values if v < value)
    return round(100 * below / len(sorted_values))


def percentile_profile(conn, vector: Vector, time_class: str) -> dict:
    """Each axis as a percentile, with an interval from the standard error.

    The interval is the point estimate +/- 1.96 SE mapped through the same rank
    function, so the bar is in the same units as the dot. At small n it
    approaches the full width of the axis -- the panel stays visible and simply
    stops claiming anything.
    """
    if not vector.axes:
        return {}
    reference = _reference_values(conn, time_class)
    if not any(reference.values()):
        return {}

    out = {}
    for axis in AXES:
        value = vector.axes[axis]
        margin = Z_95 * value.se
        out[axis] = {
            "value": value.mean,
            "percentile": _rank(reference[axis], value.mean),
            "low": _rank(reference[axis], value.mean - margin),
            "high": _rank(reference[axis], value.mean + margin),
        }
    return out


#: Rating floor for the similarity pool, applied to BLITZ rating.
#: On chess.com the players recognisable as super-GMs sit near 3000; 2400 would
#: pad the pool with players the comparison is not about. 472 players clear
#: 2800 with 30+ games, against 650 at 2400 -- 7% fewer games for a pool that
#: means what it says.
ELITE_MIN_ELO = 2800

#: The reference is always blitz, whatever class the subject is viewing. Blitz
#: is the de facto online time control and top players barely play rapid there:
#: gating per class leaves 7 usable reference players for a rapid subject,
#: against 405 for blitz and 107 for bullet.
#:
#: Comparing across classes is sound because both sides are centred within their
#: own (time class, opening, colour) norm, so each reads as "more than is normal
#: here". Measured spreads across classes differ by at most 16%, and
#: standardising below removes even that.
SIMILARITY_CLASS = "blitz"

#: How many neighbours to return.
SIMILAR_COUNT = 5


def similar_players(conn, vector: Vector) -> list[dict]:
    """The nearest players in standardised style space.

    Standardising is not optional: mobility has roughly triple the raw spread of
    space, so an unstandardised Euclidean distance would rank almost entirely on
    mobility while appearing to use all four axes.
    """
    if not vector.axes:
        return []

    rows = conn.execute(text(f"""
        SELECT p.username, v.mean_elo, {', '.join('v.' + a for a in AXES)}
        FROM {SCHEMA}player_style_vectors v
        JOIN players p ON p.player_id = v.player_id
        WHERE v.time_class = :tc AND v.mean_elo >= :floor"""),
        {"tc": SIMILARITY_CLASS, "floor": ELITE_MIN_ELO}).mappings().all()
    if not rows:
        return []

    scale = {}
    for axis in AXES:
        values = [float(r[axis]) for r in rows]
        mean = sum(values) / len(values)
        variance = sum((v - mean) ** 2 for v in values) / len(values)
        # A degenerate axis contributes nothing rather than dividing by zero.
        scale[axis] = math.sqrt(variance) or 1.0

    out = []
    for row in rows:
        distance = math.sqrt(sum(
            ((float(row[axis]) - vector.axes[axis].mean) / scale[axis]) ** 2
            for axis in AXES))
        out.append({"username": row["username"],
                    "elo": round(float(row["mean_elo"])),
                    "distance": round(distance, 3)})
    out.sort(key=lambda r: r["distance"])
    return out[:SIMILAR_COUNT]
