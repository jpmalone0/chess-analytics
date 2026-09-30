"""Loading for the scorecard: the window's games, the calibration pool, the payload.

The meaning of every number lives in engine.scorecard; this module only fetches
games and hands them over. Both queries cross the ATTACH boundary, so they run
on a connection with the sidecar attached, like app.move_quality's.
"""

from __future__ import annotations

from collections import Counter
from datetime import date
from typing import Any, Optional, Sequence

from sqlalchemy import text
from sqlalchemy.orm import Session

from app import crud
from engine import scorecard as sc
from engine.db import attach_engine_db

# Per-game facts never change for a given run and curve, and the calibration
# pool re-reads every analyzed game in a time class on each request. Keyed on
# (game_id, run_id, k) so a refit curve or a newer run is a miss, not a stale hit.
_FACTS: dict[tuple[int, int, float], Optional[dict[str, sc.SideFacts]]] = {}

_LATEST_RUN = """
    c.run_id = (SELECT MAX(c2.run_id) FROM engine.game_coverage c2
                WHERE c2.game_id = c.game_id AND c2.status = 'complete')
"""


def _curves(db: Session) -> dict[str, float]:
    return {r[0]: float(r[1])
            for r in db.execute(text("SELECT time_class, k FROM engine.wp_curve"))}


def _load_inputs(db: Session, keys: list[tuple[int, int]]) -> dict[int, sc.GameInput]:
    """GameInputs for (game_id, run_id) pairs not already in the cache."""
    if not keys:
        return {}
    ids = ",".join(str(g) for g, _ in keys)
    run_of = dict(keys)
    games = {r.game_id: r for r in db.execute(text(
        f"SELECT game_id, result, termination FROM games WHERE game_id IN ({ids})"))}
    sans: dict[int, list[str]] = {g: [] for g, _ in keys}
    for r in db.execute(text(
            f"SELECT game_id, move_san FROM moves WHERE game_id IN ({ids}) ORDER BY game_id, ply")):
        sans[r.game_id].append(r.move_san)
    evals: dict[int, dict[int, tuple]] = {g: {} for g, _ in keys}
    for r in db.execute(text(
            f"SELECT run_id, game_id, ply, cp, mate_in FROM engine.position_evals "
            f"WHERE game_id IN ({ids})")):
        if run_of[r.game_id] == r.run_id:
            evals[r.game_id][r.ply] = (r.cp, r.mate_in)
    pvs: dict[int, dict[int, list]] = {g: {} for g, _ in keys}
    for r in db.execute(text(
            f"SELECT run_id, game_id, ply, rank, move_uci, cp, mate_in FROM engine.position_pv "
            f"WHERE game_id IN ({ids}) ORDER BY game_id, ply, rank")):
        if run_of[r.game_id] == r.run_id:
            pvs[r.game_id].setdefault(r.ply, []).append((r.move_uci, r.cp, r.mate_in))
    out = {}
    for gid, _ in keys:
        n = len(sans[gid])
        out[gid] = sc.GameInput(
            game_id=gid, sans=sans[gid],
            evals=[evals[gid].get(p, (None, None)) for p in range(n + 1)],
            pvs=pvs[gid], result=games[gid].result,
            termination=games[gid].termination or "",
        )
    return out


def _facts(db: Session, rows: Sequence[Any], curves: dict[str, float]
           ) -> dict[int, Optional[dict[str, sc.SideFacts]]]:
    """Per-game facts for rows carrying game_id, run_id and time_class."""
    todo = [(r.game_id, r.run_id) for r in rows
            if r.time_class in curves
            and (r.game_id, r.run_id, curves[r.time_class]) not in _FACTS]
    inputs = _load_inputs(db, todo)
    for r in rows:
        if r.game_id not in inputs:
            continue
        k = curves[r.time_class]
        try:
            _FACTS[(r.game_id, r.run_id, k)] = sc.game_sides(inputs[r.game_id], k)
        except ValueError:
            # A move list that does not replay (a corrupt import, a variant
            # that slipped the filter) is skipped rather than failing the page.
            _FACTS[(r.game_id, r.run_id, k)] = None
    return {r.game_id: _FACTS.get((r.game_id, r.run_id, curves[r.time_class]))
            for r in rows if r.time_class in curves}


def _calibration(db: Session, time_class: str, exclude_player_id: int,
                 curves: dict[str, float]) -> dict[str, Optional[sc.Fit]]:
    """One line per dimension over every analyzed side but the player's own."""
    rows = db.execute(text(f"""
        SELECT g.game_id, c.run_id, g.time_class,
               g.white_player_id, g.black_player_id, g.white_elo, g.black_elo
        FROM   games g
        JOIN   engine.game_coverage c ON c.game_id = g.game_id
        WHERE  g.time_class = :tc AND g.variant IS NULL AND c.status = 'complete'
          AND  {_LATEST_RUN}
    """), {"tc": time_class}).all()
    facts = _facts(db, rows, curves)
    obs: list[tuple[float, sc.SideFacts]] = []
    for r in rows:
        f = facts.get(r.game_id)
        if f is None:
            continue
        for color, pid, elo in (("white", r.white_player_id, r.white_elo),
                                ("black", r.black_player_id, r.black_elo)):
            if pid != exclude_player_id and elo:
                obs.append((float(elo), f[color]))
    fits = {}
    for dim in sc.DIMENSIONS:
        xs, ys, ws = [], [], []
        for elo, side in obs:
            num, den = sc.unit_counts(dim.key, side)
            if den > 0:
                xs.append(elo)
                ys.append(num / den)
                ws.append(den)
        fits[dim.key] = sc.fit_line(xs, ys, ws, 1 if dim.higher_is_better else -1)
    return fits


def player_scorecard(
    db: Session,
    player_id: int,
    time_class: Optional[str] = None,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    player_color: Optional[str] = None,
    opening_names: Optional[str] = None,
    tz: Optional[str] = None,
) -> dict[str, Any]:
    attach_engine_db(db.connection())
    where, params = crud._build_game_filters(
        player_id=player_id, time_class=time_class,
        start_date=start_date, end_date=end_date,
        player_color=player_color, opening_names=opening_names, tz=tz,
    )
    rows = db.execute(text(f"""
        SELECT g.game_id, c.run_id, g.time_class,
               CASE WHEN g.white_player_id = :player_id THEN 'white' ELSE 'black' END AS color,
               CASE WHEN g.white_player_id = :player_id THEN g.black_elo ELSE g.white_elo END AS opp_elo,
               CASE WHEN g.white_player_id = :player_id THEN g.white_elo ELSE g.black_elo END AS own_elo
        FROM   games g
        JOIN   engine.game_coverage c ON c.game_id = g.game_id AND c.status = 'complete'
        WHERE  {where} AND {_LATEST_RUN}
    """), params).all()

    curves = _curves(db)
    facts = _facts(db, rows, curves)
    pairs, used = [], []
    for r in rows:
        f = facts.get(r.game_id)
        if f is None:
            continue
        opp = "black" if r.color == "white" else "white"
        pairs.append((f[r.color], f[opp]))
        used.append(r)

    tc = time_class or (Counter(r.time_class for r in used).most_common(1)[0][0]
                        if used else None)
    fits = _calibration(db, tc, player_id, curves) if tc in curves else {}

    out_rows = sc.summarize(pairs)
    for row in out_rows:
        fit = fits.get(row["key"])
        for side in ("you", "opp"):
            value = row[side]
            rating = fit.rating_for(value) if fit and value is not None else None
            row[f"{side}_rating"] = round(rating) if rating is not None else None
            row[f"{side}_score"] = (round(sc.rating_score(rating), 1)
                                    if rating is not None else None)
        row["calibration_n"] = round(fit.n) if fit else None

    def avg(xs):
        xs = [x for x in xs if x]
        return round(sum(xs) / len(xs)) if xs else None

    return {
        "games": len(pairs),
        "small_sample": len(pairs) < sc.SMALL_SAMPLE_GAMES,
        "sample_threshold": sc.SMALL_SAMPLE_GAMES,
        "time_class": tc,
        "curve_fitted": bool(tc and tc in curves),
        "own_avg_elo": avg(r.own_elo for r in used),
        "opp_avg_elo": avg(r.opp_elo for r in used),
        "rows": out_rows,
    }
