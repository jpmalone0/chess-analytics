"""Populate the style tables from the game corpus.

    uv run python -m analysis.build_features            # everything
    uv run python -m analysis.build_features --features # position_features only

Engine-free, ~500 games/sec, so the whole 203k-game corpus takes ~7 minutes.
Every stage is idempotent: re-running replaces rather than appends, which is
what makes a metric change safe -- otherwise the table would hold a mix of two
definitions nobody could tell apart.
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict

import chess
from sqlalchemy import text

from analysis.metrics import SNAPSHOT_PLY, STYLE_AXES
from engine.db import analysis_engine
from engine.models import init_engine_db

#: A (time class, opening, colour) cell below this many observations cannot
#: define a reliable centre; its games fall back to the coarse '*' cell.
MIN_CELL = 40

#: Games below this for a player in a time class make an unstable vector. The
#: reliability figures in the findings doc are measured at this threshold.
MIN_PLAYER_GAMES = 30

#: Schema prefix for the sidecar tables. "engine." in production, where the
#: sidecar is ATTACHed under that alias; empty in tests, which keep both schemas
#: in one in-memory database so the same SQL is exercised either way.
SCHEMA = "engine."


def extract_game(game_id: int, sans: list[str]) -> list[dict]:
    """Replay to SNAPSHOT_PLY and measure both colours.

    Returns [] for a game that ends early or stops reconstructing, rather than
    measuring whatever position it reached -- comparing move 6 against move 20
    would silently mix two different things.
    """
    if len(sans) < SNAPSHOT_PLY:
        return []
    board = chess.Board()
    try:
        for san in sans[:SNAPSHOT_PLY]:
            board.push_san(san)
    except ValueError:
        return []
    return [
        {"game_id": game_id, "color": name,
         **{axis: fn(board, color) for axis, fn in STYLE_AXES.items()}}
        for color, name in ((chess.WHITE, "white"), (chess.BLACK, "black"))
    ]


def build_position_features(conn) -> int:
    """Measure every standard game in the corpus. Returns rows written."""
    sans: dict[int, list[str]] = defaultdict(list)
    for game_id, san in conn.execute(text(
            "SELECT m.game_id, m.move_san FROM moves m "
            "JOIN games g ON g.game_id = m.game_id "
            "WHERE g.variant IS NULL AND m.ply <= :ply "
            "ORDER BY m.game_id, m.ply"), {"ply": SNAPSHOT_PLY}):
        sans[game_id].append(san)

    conn.execute(text(f"DELETE FROM {SCHEMA}position_features"))
    written = 0
    batch: list[dict] = []
    for game_id, moves in sans.items():
        batch.extend(extract_game(game_id, moves))
        if len(batch) >= 5000:
            _insert_features(conn, batch)
            written += len(batch)
            batch = []
    if batch:
        _insert_features(conn, batch)
        written += len(batch)
    return written


def _insert_features(conn, rows: list[dict]) -> None:
    conn.execute(text(
        f"INSERT INTO {SCHEMA}position_features "
        "(game_id, color, space, mobility, king_safety, pawn_structure) "
        "VALUES (:game_id, :color, :space, :mobility, :king_safety, "
        ":pawn_structure)"), rows)


def build_cell_means(conn) -> int:
    """Per (time class, opening, colour) means, plus a coarse '*' fallback row.

    Writing the fallback as a real row rather than handling it in the query is
    deliberate: the read path then joins twice and COALESCEs, with no branching
    over whether a cell happened to be populated.
    """
    axes = ", ".join(f"AVG(f.{a}) AS {a}" for a in STYLE_AXES)
    conn.execute(text(f"DELETE FROM {SCHEMA}style_cell_means"))
    conn.execute(text(f"""
        INSERT INTO {SCHEMA}style_cell_means
            (time_class, eco3, color, n, {', '.join(STYLE_AXES)})
        SELECT g.time_class, SUBSTR(COALESCE(g.eco, '?'), 1, 3), f.color,
               COUNT(*), {axes}
        FROM {SCHEMA}position_features f
        JOIN games g ON g.game_id = f.game_id
        WHERE g.variant IS NULL AND g.time_class IS NOT NULL
        GROUP BY 1, 2, 3
        HAVING COUNT(*) >= :min_cell"""), {"min_cell": MIN_CELL})
    conn.execute(text(f"""
        INSERT INTO {SCHEMA}style_cell_means
            (time_class, eco3, color, n, {', '.join(STYLE_AXES)})
        SELECT g.time_class, '*', f.color, COUNT(*), {axes}
        FROM {SCHEMA}position_features f
        JOIN games g ON g.game_id = f.game_id
        WHERE g.variant IS NULL AND g.time_class IS NOT NULL
        GROUP BY 1, 2, 3"""))
    return int(conn.execute(text(
        f"SELECT COUNT(*) FROM {SCHEMA}style_cell_means")).scalar() or 0)


def build_player_vectors(conn) -> int:
    """Full-history centred vectors for every player with enough games."""
    centred = ", ".join(
        f"AVG(f.{a} - COALESCE(c.{a}, cf.{a})) AS {a}" for a in STYLE_AXES)
    conn.execute(text(f"DELETE FROM {SCHEMA}player_style_vectors"))
    conn.execute(text(f"""
        INSERT INTO {SCHEMA}player_style_vectors
            (player_id, time_class, n, mean_elo, {', '.join(STYLE_AXES)})
        SELECT p.player_id, g.time_class, COUNT(*),
               AVG(CASE WHEN g.white_player_id = p.player_id
                        THEN g.white_elo ELSE g.black_elo END),
               {centred}
        FROM {SCHEMA}position_features f
        JOIN games g ON g.game_id = f.game_id
        JOIN players p
          ON (p.player_id = g.white_player_id AND f.color = 'white')
          OR (p.player_id = g.black_player_id AND f.color = 'black')
        LEFT JOIN {SCHEMA}style_cell_means c
          ON c.time_class = g.time_class AND c.color = f.color
         AND c.eco3 = SUBSTR(COALESCE(g.eco, '?'), 1, 3)
        LEFT JOIN {SCHEMA}style_cell_means cf
          ON cf.time_class = g.time_class AND cf.color = f.color AND cf.eco3 = '*'
        WHERE g.variant IS NULL AND g.time_class IS NOT NULL
          AND g.white_elo IS NOT NULL AND g.black_elo IS NOT NULL
        GROUP BY 1, 2
        HAVING COUNT(*) >= :min_games"""), {"min_games": MIN_PLAYER_GAMES})
    return int(conn.execute(text(
        f"SELECT COUNT(*) FROM {SCHEMA}player_style_vectors")).scalar() or 0)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m analysis.build_features")
    parser.add_argument("--features", action="store_true",
                        help="rebuild position_features only, skipping the "
                             "aggregates derived from it")
    args = parser.parse_args(argv)

    init_engine_db()
    with analysis_engine().begin() as conn:
        rows = build_position_features(conn)
        print(f"position_features: {rows:,} rows", file=sys.stderr)
        if args.features:
            return 0
        cells = build_cell_means(conn)
        print(f"style_cell_means: {cells:,} cells", file=sys.stderr)
        vectors = build_player_vectors(conn)
        print(f"player_style_vectors: {vectors:,} vectors", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
