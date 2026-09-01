"""
Population baselines — Elo-bucketed "average player" reference data.

Every function here builds on one shared CTE that turns the games table into a
set of player-game rows (both sides of each game), filtered to an Elo band and
time control, with the searched player removed and each remaining player capped
at PER_PLAYER_CAP games. The cap matters: without it a single heavily-synced
account defines its whole bracket.
"""

import statistics as _stats
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


def _move_time_buckets(time_class: Optional[str]) -> list[tuple[str, float, float]]:
    """Bucket edges, identical to crud.move_time_stats so the overlay lines up
    with the player's own histogram bar for bar."""
    if time_class == "bullet":
        return [
            ("0–0.5s", 0, 0.5), ("0.5–1s", 0.5, 1),
            ("1–1.5s", 1, 1.5), ("1.5–2s", 1.5, 2),
            ("2–2.5s", 2, 2.5), ("2.5–3s", 2.5, 3),
            ("3s+", 3, 9999),
        ]
    if time_class == "blitz":
        return [
            ("0–2s", 0, 2), ("2–4s", 2, 4), ("4–6s", 4, 6),
            ("6–8s", 6, 8), ("8–10s", 8, 10), ("10–12s", 10, 12),
            ("12s+", 12, 9999),
        ]
    return [
        ("0–5s", 0, 5), ("5–10s", 5, 10), ("10–15s", 10, 15),
        ("15–20s", 15, 20), ("20–25s", 20, 25), ("25–30s", 25, 30),
        ("30s+", 30, 9999),
    ]


def move_time_baseline(
    db: Session,
    player_id: int,
    band: Optional[dict],
    time_class: Optional[str] = None,
    player_color: Optional[str] = None,
    opening_names: Optional[str] = None,
) -> Optional[dict]:
    """Population time-per-move distribution and mean-by-move-number.

    Percentages, not counts: the population has far more moves than any one
    player, so only the shape is comparable.
    """
    if band is None:
        return None

    cte, params = _population_cte(
        band["elo_lo"], band["elo_hi"], player_id,
        band["time_control"], time_class, player_color, opening_names,
    )
    rows = db.execute(text(f"""
        WITH {cte}
        SELECT m.move_number, m.time_spent_seconds
        FROM   moves m
        JOIN   pop ON m.game_id = pop.game_id AND m.color = pop.side
        WHERE  m.time_spent_seconds IS NOT NULL
          AND  m.time_spent_seconds >= 0
    """), params).mappings().all()

    if not rows:
        return None

    all_times: list[float] = []
    by_move: dict[int, list[float]] = {}
    for row in rows:
        t = float(row["time_spent_seconds"])
        all_times.append(t)
        by_move.setdefault(int(row["move_number"]), []).append(t)

    bucket_defs = _move_time_buckets(time_class)
    counts = {label: 0 for label, _, _ in bucket_defs}
    for t in all_times:
        for label, lo, hi in bucket_defs:
            if lo <= t < hi:
                counts[label] += 1
                break

    total = len(all_times)
    return {
        "buckets": [
            {"label": label, "count": counts[label], "pct": round(counts[label] / total * 100, 1)}
            for label, _, _ in bucket_defs
        ],
        "mean": round(_stats.mean(all_times), 2),
        "median": round(_stats.median(all_times), 2),
        "std_dev": round(_stats.stdev(all_times) if total > 1 else 0.0, 2),
        "total_moves": total,
        "by_move_number": [
            {
                "move_number": mn,
                "avg_seconds": round(_stats.mean(by_move[mn]), 2),
                "count": len(by_move[mn]),
            }
            for mn in sorted(by_move) if mn <= 100
        ],
    }


_LENGTH_BUCKETS = [
    ("1–10", 1, 10), ("11–20", 11, 20), ("21–30", 21, 30), ("31–40", 31, 40),
    ("41–50", 41, 50), ("51–60", 51, 60), ("61–80", 61, 80), ("80+", 81, 9999),
]

# Same 14 buckets and same predicates as crud.rating_differential — the overlay
# must land on the player's own bars, so the labels are the join key.
_RATING_DIFF_BUCKETS: list[tuple[str, Any]] = [
    ("> +100",      lambda d: d >= 100),
    ("+50 to +100", lambda d: 50 <= d < 100),
    ("+40 to +50",  lambda d: 40 <= d < 50),
    ("+30 to +40",  lambda d: 30 <= d < 40),
    ("+20 to +30",  lambda d: 20 <= d < 30),
    ("+10 to +20",  lambda d: 10 <= d < 20),
    ("0 to +10",    lambda d: 0 <= d < 10),
    ("-10 to 0",    lambda d: -10 <= d < 0),
    ("-20 to -10",  lambda d: -20 <= d < -10),
    ("-30 to -20",  lambda d: -30 <= d < -20),
    ("-40 to -30",  lambda d: -40 <= d < -30),
    ("-50 to -40",  lambda d: -50 <= d < -40),
    ("-100 to -50", lambda d: -100 <= d < -50),
    ("< -100",      lambda d: d < -100),
]

_CLOCK_BUCKETS = ["far_behind", "behind", "even", "ahead", "far_ahead"]


def _outcome_case() -> str:
    """SQL expression giving the population player's outcome in their own game."""
    return """
        CASE
            WHEN (pop.side = 'white' AND g.result = '1-0')
              OR (pop.side = 'black' AND g.result = '0-1') THEN 'win'
            WHEN g.result = '1/2-1/2'                      THEN 'draw'
            ELSE 'loss'
        END
    """


def _tally(buckets: dict, label: str, outcome: str) -> None:
    key = {"win": "wins", "loss": "losses"}.get(outcome, "draws")
    buckets[label][key] += 1


def _rates(label_field: str, label: str, b: dict) -> dict:
    total = b["wins"] + b["losses"] + b["draws"]
    decisive = b["wins"] + b["losses"]
    return {
        label_field: label,
        "total_games": total,
        "wins": b["wins"], "losses": b["losses"], "draws": b["draws"],
        "win_rate": round(b["wins"] / total * 100, 1) if total else 0,
        "win_rate_no_draws": round(b["wins"] / decisive * 100, 1) if decisive else 0,
        "draw_rate": round(b["draws"] / total * 100, 1) if total else 0,
    }


def _pre_game_diff(diff_post: float, score: float) -> float:
    """Undo the post-game rating update, identically to crud.rating_differential.

    Chess.com PGN Elos are POST-game, so each result is baked into its own
    stored gap. The player's chart corrects for this; the baseline must apply
    the same correction or the two curves are measured on different x-axes.
    """
    d = diff_post
    for _ in range(3):
        expected = 1 / (1 + 10 ** (-d / 400))
        d = diff_post - 32 * (score - expected)
    return d


def game_length_baseline(
    db: Session,
    player_id: int,
    band: Optional[dict],
    time_class: Optional[str] = None,
    player_color: Optional[str] = None,
    opening_names: Optional[str] = None,
) -> Optional[list[dict]]:
    """Population win rate bucketed by total game length."""
    if band is None:
        return None

    cte, params = _population_cte(
        band["elo_lo"], band["elo_hi"], player_id,
        band["time_control"], time_class, player_color, opening_names,
    )
    rows = db.execute(text(f"""
        WITH {cte}
        SELECT g.total_moves, {_outcome_case()} AS outcome
        FROM   pop JOIN games g ON g.game_id = pop.game_id
        WHERE  g.total_moves IS NOT NULL
    """), params).mappings().all()

    if not rows:
        return None

    buckets = {label: {"wins": 0, "losses": 0, "draws": 0} for label, _, _ in _LENGTH_BUCKETS}
    for row in rows:
        for label, lo, hi in _LENGTH_BUCKETS:
            if lo <= row["total_moves"] <= hi:
                _tally(buckets, label, row["outcome"])
                break

    return [_rates("bucket", label, buckets[label]) for label, _, _ in _LENGTH_BUCKETS]


def rating_diff_baseline(
    db: Session,
    player_id: int,
    band: Optional[dict],
    time_class: Optional[str] = None,
    player_color: Optional[str] = None,
    opening_names: Optional[str] = None,
) -> Optional[list[dict]]:
    """Population win rate bucketed by estimated PRE-game Elo gap."""
    if band is None:
        return None

    cte, params = _population_cte(
        band["elo_lo"], band["elo_hi"], player_id,
        band["time_control"], time_class, player_color, opening_names,
    )
    rows = db.execute(text(f"""
        WITH {cte}
        SELECT CASE WHEN pop.side = 'white'
                    THEN g.white_elo - g.black_elo
                    ELSE g.black_elo - g.white_elo END AS elo_diff,
               {_outcome_case()} AS outcome
        FROM   pop JOIN games g ON g.game_id = pop.game_id
        WHERE  g.white_elo IS NOT NULL AND g.black_elo IS NOT NULL
    """), params).mappings().all()

    if not rows:
        return None

    score_for = {"win": 1.0, "draw": 0.5, "loss": 0.0}
    buckets = {label: {"wins": 0, "losses": 0, "draws": 0} for label, _ in _RATING_DIFF_BUCKETS}
    for row in rows:
        outcome = row["outcome"]
        diff = _pre_game_diff(float(row["elo_diff"]), score_for[outcome])
        for label, predicate in _RATING_DIFF_BUCKETS:
            if predicate(diff):
                _tally(buckets, label, outcome)
                break

    return [_rates("bucket", label, buckets[label]) for label, _ in _RATING_DIFF_BUCKETS]


def _clock_bucket(avg_diff: float) -> str:
    """Absolute seconds, matching the CASE in crud.analyze_clock_advantage.
    These are NOT scaled by base time — the overlay must use identical cut
    points or it describes different bars than the player's."""
    if avg_diff < -30:
        return "far_behind"
    if avg_diff < -15:
        return "behind"
    if avg_diff <= 15:
        return "even"
    if avg_diff <= 30:
        return "ahead"
    return "far_ahead"


def clock_advantage_baseline(
    db: Session,
    player_id: int,
    band: Optional[dict],
    time_class: Optional[str] = None,
    player_color: Optional[str] = None,
    opening_names: Optional[str] = None,
) -> Optional[list[dict]]:
    """Population win rate bucketed by average clock difference through the game."""
    if band is None:
        return None

    cte, params = _population_cte(
        band["elo_lo"], band["elo_hi"], player_id,
        band["time_control"], time_class, player_color, opening_names,
    )
    rows = db.execute(text(f"""
        WITH {cte},
        diffs AS (
            -- Grouped by game AND side: when both players of a game fall in the
            -- band, each is a separate observation with a mirrored advantage.
            SELECT pop.game_id, pop.side,
                   AVG(mine.clock_seconds - theirs.clock_seconds) AS avg_diff
            FROM   pop
            JOIN   moves mine   ON mine.game_id   = pop.game_id AND mine.color   = pop.side
            JOIN   moves theirs ON theirs.game_id = pop.game_id
                               AND theirs.color  != pop.side
                               AND theirs.move_number = mine.move_number
            WHERE  mine.clock_seconds IS NOT NULL AND theirs.clock_seconds IS NOT NULL
            GROUP  BY pop.game_id, pop.side
        )
        SELECT d.avg_diff, {_outcome_case()} AS outcome
        FROM   diffs d
        JOIN   pop ON pop.game_id = d.game_id AND pop.side = d.side
        JOIN   games g ON g.game_id = d.game_id
    """), params).mappings().all()

    if not rows:
        return None

    buckets = {label: {"wins": 0, "losses": 0, "draws": 0} for label in _CLOCK_BUCKETS}
    for row in rows:
        _tally(buckets, _clock_bucket(float(row["avg_diff"])), row["outcome"])

    return [_rates("clock_bucket", label, buckets[label]) for label in _CLOCK_BUCKETS]
