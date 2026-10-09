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
    clocks: dict[int, list[Optional[float]]] = {g: [] for g, _ in keys}
    for r in db.execute(text(
            f"SELECT game_id, move_san, clock_seconds FROM moves "
            f"WHERE game_id IN ({ids}) ORDER BY game_id, ply")):
        sans[r.game_id].append(r.move_san)
        clocks[r.game_id].append(r.clock_seconds)
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
            clocks=clocks[gid],
        )
    return out


def _load_tiers(db: Session, keys: list[tuple[int, int]]
                ) -> dict[int, dict[str, dict[str, int]]]:
    """Move Quality's counts per side, from the same view that section reads,
    so a blunder is one thing on both. A side with no scored moves is absent."""
    if not keys:
        return {}
    ids = ",".join(str(g) for g, _ in keys)
    run_of = dict(keys)
    out: dict[int, dict[str, dict[str, int]]] = {g: {} for g, _ in keys}
    for r in db.execute(text(
            f"SELECT run_id, game_id, color, moves_scored, {', '.join(sc.TIERS)} "
            f"FROM engine.game_move_quality WHERE game_id IN ({ids})")).mappings():
        if run_of[r["game_id"]] == r["run_id"]:
            out[r["game_id"]][r["color"]] = {
                k: int(r[k] or 0) for k in ("moves_scored", *sc.TIERS)}
    return out


def _with_tiers(side: sc.SideFacts, counts: dict[str, int]) -> sc.SideFacts:
    return side._replace(
        moves_scored=counts.get("moves_scored", 0),
        inaccuracies=counts.get("inaccuracies", 0), mistakes=counts.get("mistakes", 0),
        blunders=counts.get("blunders", 0), misses=counts.get("misses", 0))


def _facts(db: Session, rows: Sequence[Any], curves: dict[str, float]
           ) -> dict[int, Optional[dict[str, sc.SideFacts]]]:
    """Per-game facts for rows carrying game_id, run_id and time_class."""
    todo = [(r.game_id, r.run_id) for r in rows
            if r.time_class in curves
            and (r.game_id, r.run_id, curves[r.time_class]) not in _FACTS]
    inputs = _load_inputs(db, todo)
    tiers = _load_tiers(db, todo)
    for r in rows:
        if r.game_id not in inputs:
            continue
        k = curves[r.time_class]
        try:
            sides = sc.game_sides(inputs[r.game_id], k)
            counts = tiers.get(r.game_id, {})
            _FACTS[(r.game_id, r.run_id, k)] = {
                color: _with_tiers(side, counts.get(color, {}))
                for color, side in sides.items()}
        except ValueError:
            # A move list that does not replay (a corrupt import, a variant
            # that slipped the filter) is skipped rather than failing the page.
            _FACTS[(r.game_id, r.run_id, k)] = None
    return {r.game_id: _FACTS.get((r.game_id, r.run_id, curves[r.time_class]))
            for r in rows if r.time_class in curves}


def _calibration(db: Session, time_class: str, exclude_player_id: int,
                 curves: dict[str, float]
                 ) -> tuple[dict[str, Optional[sc.Fit]], dict[str, sc.Norm]]:
    """One band line per dimension, over analyzed games the player is not in.

    Their opponents are dropped too, not just their own seat: an opponent's
    side of the player's game is the player's game seen from the other chair,
    and keeping it would bring back the mirror symmetry this comparison exists
    to avoid (your conversion is exactly their failure to save).

    Each player adds at most a few games, so nobody's line is mostly one
    other player, and every viewer's line is nearly the same. Sides rated past
    what the engine can judge are left out.

    Also returns the pool's per-state phase norms, which the lines and the
    player's own rows are both standardised on.
    """
    rows = db.execute(text(f"""
        SELECT g.game_id, c.run_id, g.time_class,
               g.white_player_id, g.black_player_id, g.white_elo, g.black_elo
        FROM   games g
        JOIN   engine.game_coverage c ON c.game_id = g.game_id
        WHERE  g.time_class = :tc AND g.variant IS NULL AND c.status = 'complete'
          AND  g.white_player_id <> :pid AND g.black_player_id <> :pid
          AND  {_LATEST_RUN}
    """), {"tc": time_class, "pid": exclude_player_id}).all()
    facts = _facts(db, rows, curves)
    sides = []
    for r in rows:
        f = facts.get(r.game_id)
        if f is None:
            continue
        for color, pid, elo in (("white", r.white_player_id, r.white_elo),
                                ("black", r.black_player_id, r.black_elo)):
            if elo:
                sides.append((r.game_id, pid, float(elo), f[color]))
    obs = [(elo, side) for _, _, elo, side
           in sc.cap_per_player(sc.within_engine_range(sides))]
    norms = sc.phase_norms([side for _, side in obs])
    fits = {}
    # The Scorecard's rows, and Move Quality's categories for that section.
    for key in dict.fromkeys([d.key for d in sc.DIMENSIONS] + list(sc.TIERS)):
        xs, ys, ws = [], [], []
        for elo, side in obs:
            num, den = sc.calibration_counts(key, side, norms)
            if den > 0:
                xs.append(elo)
                ys.append(num / den)
                ws.append(den)
        fits[key] = sc.fit_band(xs, ys, ws)
    return fits, norms


def _window(db: Session, player_id: int, time_class: Optional[str],
            start_date: Optional[date], end_date: Optional[date],
            player_color: Optional[str], opening_names: Optional[str],
            tz: Optional[str]):
    """The player's analyzed games in the filter window: the rows used, their
    sides, the time class shown, and the fitted curves."""
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
    sides, used = [], []
    for r in rows:
        f = facts.get(r.game_id)
        if f is None:
            continue
        sides.append(f[r.color])
        used.append(r)
    tc = time_class or (Counter(r.time_class for r in used).most_common(1)[0][0]
                        if used else None)
    return used, sides, tc, curves


def _avg(xs) -> Optional[int]:
    xs = [x for x in xs if x]
    return round(sum(xs) / len(xs)) if xs else None


def _compare_point(rating: Optional[int], elo_band: Optional[str]
                   ) -> tuple[Optional[int], str, Optional[int]]:
    """Where the players line is read: (band lower edge, source, rating).

    For a band, picked or your own, at its lower edge, the round number the
    outline is drawn at. "All players" has no band and reads it at your exact
    average."""
    if elo_band is not None and elo_band.isdigit():
        return int(elo_band), "selected", int(elo_band)
    if elo_band == "all" or rating is None:
        return None, "average", rating
    lo = rating // 100 * 100
    return lo, "own_band", lo


def players_move_quality(
    db: Session,
    player_id: int,
    time_class: Optional[str] = None,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    player_color: Optional[str] = None,
    opening_names: Optional[str] = None,
    tz: Optional[str] = None,
    elo_band: Optional[str] = None,
) -> dict[str, Any]:
    """Move Quality's "players" figures: each category's players line, per
    move, read at the rating the Scorecard reads its lines at. `rates` is None
    until enough other players' games stand behind every line."""
    used, _, tc, curves = _window(db, player_id, time_class, start_date, end_date,
                                  player_color, opening_names, tz)
    band_lo, source, at = _compare_point(_avg(r.own_elo for r in used), elo_band)
    fits = _calibration(db, tc, player_id, curves)[0] if tc in curves else {}
    lines = [fits.get(k) for k in sc.TIERS]
    rates = None
    if at is not None and all(lines):
        rates = {k: max(0.0, f.at(at)) for k, f in zip(sc.TIERS, lines, strict=True) if f}
    return {
        "rating": at,
        "source": source,
        "band": [band_lo, band_lo + 99] if band_lo is not None else None,
        "rates": rates,
        "band_games": max((f.n for f in lines if f), default=0),
    }


def player_scorecard(
    db: Session,
    player_id: int,
    time_class: Optional[str] = None,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    player_color: Optional[str] = None,
    opening_names: Optional[str] = None,
    tz: Optional[str] = None,
    elo_band: Optional[str] = None,
) -> dict[str, Any]:
    """`elo_band` is the Compare-to selection, a band's lower edge. Bands, the
    player's own included, read the lines at that edge; "all" reads them at
    the player's average."""
    used, sides, tc, curves = _window(db, player_id, time_class, start_date, end_date,
                                      player_color, opening_names, tz)
    fits, norms = _calibration(db, tc, player_id, curves) if tc in curves else ({}, {})
    rating = _avg(r.own_elo for r in used)
    band_lo, source, compare_rating = _compare_point(rating, elo_band)
    out_rows = (sc.compare_to_band(sides, fits, compare_rating, norms) if compare_rating
                else sc.compare_to_band(sides, {}, 0))

    return {
        "games": len(sides),
        "small_sample": len(sides) < sc.SMALL_SAMPLE_GAMES,
        "sample_threshold": sc.SMALL_SAMPLE_GAMES,
        "time_class": tc,
        "curve_fitted": bool(tc and tc in curves),
        "own_avg_elo": rating,
        "compare_rating": compare_rating,
        "compare_source": source,
        "compare_band": [band_lo, band_lo + 99] if band_lo is not None else None,
        "opp_avg_elo": _avg(r.opp_elo for r in used),
        # Side-games behind the band lines: other players' games only.
        "band_games": max((f.n for f in fits.values() if f), default=0),
        "rows": out_rows,
    }
