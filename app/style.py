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
from sqlalchemy.exc import OperationalError

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

    try:
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
    except OperationalError:
        # The sidecar is not attached, or has never been built. An absent
        # profile is the correct answer, not a 500.
        return Vector()

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


def class_scales(conn) -> dict[str, dict[str, tuple[float, float]]]:
    """Per (time class, axis) mean and standard deviation of player vectors.

    Centring a value within its (time class, opening, colour) cell equalises the
    SPREAD across time classes -- measured within 16% -- but NOT the location.
    The same player, measured in rapid and in blitz, lands +0.618 apart on
    mobility (t=5.05), +0.277 on king safety (t=4.52) and +0.082 on pawn
    structure (t=3.67), verified on the 15 players who have both vectors. The
    within-player gap is larger than the between-population gap, so it is a real
    property of the time control rather than of who plays each one.

    Two consequences, both of which this function exists to fix:

    - A percentile reference cannot simply pool every class. Doing so shifts a
      rapid subject up by 12 to 20 points for no reason connected to their play.
    - A rapid vector cannot be compared to a blitz vector on a shared scale,
      which is exactly what the similarity readout does.

    Expressing every vector as a z-score within its own class removes both. It
    is validated by agreement: a rapid subject ranked against 789 z-scored
    vectors lands within a few points of where 37 rapid-only vectors put them,
    while the reference grows twentyfold.
    """
    try:
        rows = conn.execute(text(
            f"SELECT time_class, {', '.join(AXES)} "
            f"FROM {SCHEMA}player_style_vectors")).mappings().all()
    except OperationalError:
        return {}

    by_class: dict[str, list] = {}
    for row in rows:
        by_class.setdefault(row["time_class"], []).append(row)

    out: dict[str, dict[str, tuple[float, float]]] = {}
    for time_class, group in by_class.items():
        out[time_class] = {}
        for axis in AXES:
            values = [float(r[axis]) for r in group]
            mean = sum(values) / len(values)
            variance = sum((v - mean) ** 2 for v in values) / len(values)
            # A class with one vector, or a degenerate axis, contributes its
            # location only -- dividing by zero would be worse than not scaling.
            out[time_class][axis] = (mean, math.sqrt(variance) or 1.0)
    return out


def _z(scales, time_class: str, axis: str, value: float) -> float:
    """Express a raw centred value as a z-score within its own time class."""
    mean, sd = scales.get(time_class, {}).get(axis, (0.0, 1.0))
    return (value - mean) / sd


def _reference_values(conn, scales) -> dict[str, list[float]]:
    """Every player vector in the corpus, z-scored within its own class.

    Pooled across time classes on purpose. The percentile reference is EVERY
    player with a vector -- no rating filter, unlike the elite pool used for
    similarity. Those two answer different questions and conflating them is the
    mistake this project has already made four times.

    Pooling is only legitimate because of the z-scoring; see class_scales.
    """
    try:
        rows = conn.execute(text(
            f"SELECT time_class, {', '.join(AXES)} "
            f"FROM {SCHEMA}player_style_vectors")).mappings().all()
    except OperationalError:
        return {axis: [] for axis in AXES}
    return {
        axis: sorted(_z(scales, r["time_class"], axis, float(r[axis])) for r in rows)
        for axis in AXES
    }


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
    scales = class_scales(conn)
    reference = _reference_values(conn, scales)
    if not any(reference.values()):
        return {}

    # The subject and the reference must be on the same footing, so both are
    # z-scored within their own time class. The standard error is a width in
    # raw units, so it is scaled by the same divisor rather than shifted.
    sd = {a: scales.get(time_class, {}).get(a, (0.0, 1.0))[1] for a in AXES}

    out = {}
    for axis in AXES:
        value = vector.axes[axis]
        centre = _z(scales, time_class, axis, value.mean)
        margin = Z_95 * value.se / sd[axis]
        out[axis] = {
            "value": value.mean,
            "percentile": _rank(reference[axis], centre),
            "low": _rank(reference[axis], centre - margin),
            "high": _rank(reference[axis], centre + margin),
        }
    return out


#: Rating floor for the similarity pool, applied to BLITZ rating.
#:
#: On chess.com the players recognisable as super-GMs sit at or above 3000, so
#: that is where the floor goes -- a lower one pads the pool with players the
#: comparison is not meant to be about. Measured over the built vectors:
#: 3000+ leaves 100 players and 118,609 blitz games, against 401 and 155,717 at
#: 2800. A quarter of the players and three quarters of the games, for a pool
#: whose label means what it says.
#:
#: Every user-visible mention of this floor is derived from this constant rather
#: than written out, so the label and the filter cannot disagree.
ELITE_MIN_ELO = 3000

#: The reference is always blitz, whatever class the subject is viewing. Blitz
#: is the de facto online time control and top players barely play rapid there:
#: gating per class leaves 7 usable reference players for a rapid subject,
#: against 405 for blitz and 107 for bullet.
#:
#: Comparing across classes needs more than centring. Centring equalises the
#: spread (within 16%) but not the location: the same player lands +0.618 higher
#: on mobility in rapid than in blitz (t=5.05), verified within-player. Both
#: sides are therefore expressed as z-scores within their own class before any
#: distance is taken -- see class_scales.
SIMILARITY_CLASS = "blitz"

#: How many entries the list holds.
SIMILAR_COUNT = 10

#: Players always shown, whatever their distance.
#:
#: The list would otherwise be five names most people have never heard of, which
#: makes it hard to tell whether a distance of 0.6 is close. Anchoring it with
#: players whose style is common knowledge gives the rest of the list a scale.
#:
#: They are ranked by distance like everyone else -- pinning decides who appears,
#: not where. Each carries pinned=True so the panel can mark it, because a pinned
#: player sitting at rank 10 is there despite their distance, not because of it.
#:
#: Matched case-insensitively against chess.com usernames. A pin that is missing
#: from the pool (below the rating floor, too few games, or the subject
#: themselves) is simply skipped.
PINNED_USERNAMES = (
    "hikaru",
    "magnuscarlsen",
    "firouzja2003",
    "danielnaroditsky",
    "fabianocaruana",
)


def similar_players(conn, vector: Vector, player_id: int | None = None,
                    time_class: str | None = None) -> list[dict]:
    """The nearest players in standardised style space.

    Standardising is not optional: mobility has roughly triple the raw spread of
    space, so an unstandardised Euclidean distance would rank almost entirely on
    mobility while appearing to use all four axes.

    time_class is the class the subject's own vector came from. Both sides are
    z-scored within their own class before the distance, because a rapid vector
    and a blitz vector do not sit on a common scale even after centring -- the
    same player lands +0.618 higher on mobility in rapid. Without it the
    comparison is biased, and on real data it changes two of the five nearest
    neighbours.

    player_id, when given, excludes that player from the pool -- a subject who
    is themselves in the elite blitz pool would otherwise appear in their own
    results. This is applied in the WHERE clause, before the list is truncated
    to SIMILAR_COUNT: filtering after the slice would silently return four
    results whenever the subject placed in their own top five, dropping a real
    neighbour instead of surfacing it. The self-distance is not generally zero
    -- the subject's vector is filter-scoped while the pool row is full-history
    -- so there is no shortcut of just dropping the nearest match.
    """
    if not vector.axes:
        return []

    try:
        rows = conn.execute(text(f"""
            SELECT p.username, v.mean_elo, {', '.join('v.' + a for a in AXES)}
            FROM {SCHEMA}player_style_vectors v
            JOIN players p ON p.player_id = v.player_id
            WHERE v.time_class = :tc AND v.mean_elo >= :floor
              AND (:player_id IS NULL OR v.player_id != :player_id)"""),
            {"tc": SIMILARITY_CLASS, "floor": ELITE_MIN_ELO, "player_id": player_id}
            ).mappings().all()
    except OperationalError:
        return []
    if not rows:
        return []

    # Each side is z-scored within its OWN class: the pool against blitz, the
    # subject against whatever class they are viewing. That removes both the
    # scale difference between axes (mobility has roughly triple the raw spread
    # of space, so an unstandardised distance would be a mobility ranking
    # wearing a costume) and the location difference between classes.
    scales = class_scales(conn)
    subject_class = time_class or SIMILARITY_CLASS
    subject = {a: _z(scales, subject_class, a, vector.axes[a].mean) for a in AXES}

    # Each returned player carries their own percentiles, ranked against the
    # same pooled reference the subject is ranked against. The panel overlays
    # them on the chart when a name is clicked, and a second round trip for
    # four numbers we already hold would be the wrong trade.
    reference = _reference_values(conn, scales)

    scored = []
    for row in rows:
        theirs = {a: _z(scales, SIMILARITY_CLASS, a, float(row[a])) for a in AXES}
        distance = math.sqrt(sum(
            (theirs[axis] - subject[axis]) ** 2 for axis in AXES))
        scored.append({"username": row["username"],
                       "elo": round(float(row["mean_elo"])),
                       "distance": round(distance, 3),
                       "pinned": row["username"].lower() in PINNED_USERNAMES,
                       "axes": {a: _rank(reference[a], theirs[a]) for a in AXES}})
    scored.sort(key=lambda r: r["distance"])

    # Pins take their places first, then the nearest others fill the rest. A pin
    # that is already among the nearest is not counted twice, so the list stays
    # SIMILAR_COUNT long rather than losing a genuine neighbour to a duplicate.
    pinned = [r for r in scored if r["pinned"]]
    others = [r for r in scored if not r["pinned"]]
    chosen = pinned + others[:max(0, SIMILAR_COUNT - len(pinned))]
    chosen.sort(key=lambda r: r["distance"])
    return chosen[:SIMILAR_COUNT]
