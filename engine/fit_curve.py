"""Fit the win-probability curve, one k per time class.

    python -m engine.fit_curve --player ballasack6

wp(cp) = 1 / (1 + exp(-cp / k)), fitted by maximum likelihood against observed
results with draws scored 0.5. That makes the fitted quantity expected points,
the same quantity chess.com's published thresholds are denominated in.

Only fills a time class that has no curve yet. Refitting is a deliberate act
(--refit), because a silent refit moves every historical count.
"""

import argparse
import math
from datetime import datetime
from typing import Optional

from sqlalchemy import text

from engine.db import SessionLocal, analysis_engine
from engine.models import WpCurve
from engine.views import init_engine_db

# Search bounds for k. Below 150 the curve is steeper than any observed
# population; above 1200 it is flat enough to be indistinguishable from noise.
K_MIN, K_MAX, K_STEP = 150, 1200, 5

# Early plies are book and late ones are decided; both are uninformative about
# how an evaluation converts. Beyond +/-2500 the result is already settled.
PLY_LO, PLY_HI, CP_ABS_MAX = 20, 99, 2500


def _nll(k: float, pairs) -> float:
    """Negative log-likelihood of the observed results under this k."""
    total = 0.0
    for cp, score in pairs:
        p = 1.0 / (1.0 + math.exp(-cp / k))
        p = min(max(p, 1e-9), 1 - 1e-9)
        total -= score * math.log(p) + (1 - score) * math.log(1 - p)
    return total


def fit_k(pairs) -> Optional[int]:
    """The k minimising negative log-likelihood. None when there is no data.

    pairs: [(cp_from_the_player's_side, score in {0, 0.5, 1})]
    """
    pairs = list(pairs)
    if not pairs:
        return None
    return min(range(K_MIN, K_MAX + 1, K_STEP), key=lambda k: _nll(float(k), pairs))


def collect_pairs(conn, username: str, time_class: str):
    """Every scored position for one player in one time class, with its result."""
    return [
        (row[0], row[1])
        for row in conn.execute(text("""
            WITH me AS (SELECT player_id FROM players WHERE username = :u),
            g AS (
                SELECT g.game_id,
                       CASE WHEN g.white_player_id = (SELECT player_id FROM me)
                            THEN 1 ELSE -1 END AS sgn,
                       g.result
                FROM   games g
                WHERE  (g.white_player_id = (SELECT player_id FROM me)
                     OR g.black_player_id = (SELECT player_id FROM me))
                  AND  g.time_class = :tc
            )
            SELECT mv.cp_after * g.sgn,
                   CASE WHEN g.result = '1/2-1/2' THEN 0.5
                        WHEN (g.result = '1-0' AND g.sgn =  1)
                          OR (g.result = '0-1' AND g.sgn = -1) THEN 1.0
                        ELSE 0.0 END
            FROM   engine.move_evals mv
            JOIN   g ON g.game_id = mv.game_id
            WHERE  mv.ply BETWEEN :lo AND :hi
              AND  ABS(mv.cp_after) < :cap
        """), {"u": username, "tc": time_class,
               "lo": PLY_LO, "hi": PLY_HI, "cap": CP_ABS_MAX})
    ]


def fit_time_class(username: str, time_class: str, refit: bool = False) -> Optional[int]:
    """Fit and store k for one time class. Returns the k, or None if no data."""
    with SessionLocal() as session:
        existing = session.get(WpCurve, time_class)
        if existing is not None and not refit:
            return int(existing.k)

    with analysis_engine().connect() as conn:
        pairs = collect_pairs(conn, username, time_class)

    k = fit_k(pairs)
    if k is None:
        return None

    with SessionLocal() as session:
        session.merge(WpCurve(
            time_class=time_class, k=float(k), n=len(pairs),
            fitted_at=datetime.utcnow(),
            source=f"mle over {username} {time_class}",
        ))
        session.commit()
    return k


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--player", required=True, help="chess.com username")
    p.add_argument("--time-class", action="append", dest="time_classes",
                   choices=["bullet", "blitz", "rapid", "daily"],
                   help="repeatable; default: bullet, blitz and rapid")
    p.add_argument("--refit", action="store_true",
                   help="overwrite an existing curve (moves every historical count)")
    args = p.parse_args(argv)

    init_engine_db()
    for tc in args.time_classes or ["bullet", "blitz", "rapid"]:
        k = fit_time_class(args.player, tc, refit=args.refit)
        print(f"{tc:8s} k = {k}" if k else f"{tc:8s} no analyzed games, skipped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
