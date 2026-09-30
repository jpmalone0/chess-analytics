"""Baseline for the pawn structures ballasack6 reaches most, to measure structure study against.

The question this answers later is "did studying a structure change how I play it?", so the
three structures are fixed here rather than re-chosen by frequency. A later window is then
measured exactly as the baseline was.

A game belongs to a structure by its central pawns after move 12 (ply 24):

  Open Sicilian (as Black)      1.e4 c5, Black played ...cxd4, White has no d-pawn
  King's-pawn center (White)    White pawn on e4, Black pawn on e5, Black still has a c-pawn
                                (the c-pawn condition keeps Sicilian ...e5 structures out)
  Small center (White)          White pawn on e4 and no d-pawn; Black pawn on d6, no e-pawn,
                                c-pawn kept (Philidor exchange, Steinitz Ruy Lopez)

The Open Sicilian is defined by the exchange rather than by the pawns at move 12, because
...d5 breaks and ...bxc6 recaptures scatter its pawns by then.

Per structure, for your moves and your opponents' in the same games:

  long thinks   share of moves 9-20, made with more than 2 minutes left, that took over 20s
  clock         how many more seconds than the opponent you had used by move 20 (median)
  errors        mistakes and blunders (lost >= 10% expected score) per game, moves 9-30,
                from the engine.move_quality view for one engine run

Long thinks is the leading indicator: it needs no engine and moves before the error rate can.

    uv run python analysis/structure_baseline.py                      # the baseline window
    uv run python analysis/structure_baseline.py --since 2026-09-29   # after studying

The errors column only counts games the engine has analysed, so analyse new games first:

    uv run python -m engine.cli --player ballasack6 --time-class rapid --since 2026-09-29

Engine-free apart from that column; a few seconds over the baseline window.
"""

from __future__ import annotations

import argparse
import math
import sqlite3
import statistics
from collections import defaultdict

import chess

CANONICAL_DB = "chess_analytics.db"
ENGINE_DB = "chess_engine.db"

SNAPSHOT_PLY = 24            # after move 12, where the early-middlegame trouble starts
THINK_MOVES = (9, 20)        # the window long thinks are counted in
ERROR_MOVES = (9, 30)        # the window errors are counted in
LONG_THINK_S = 20.0
MIN_CLOCK_S = 120.0          # below this a long think is time trouble, not deliberation
ERROR_WP = 0.10              # mistake or worse, the tool's own ladder

D_FILE, E_FILE = 3, 4


def _ranks(board: chess.Board, color: chess.Color, file: int) -> set[int]:
    return {chess.square_rank(sq) + 1 for sq in board.pieces(chess.PAWN, color)
            if chess.square_file(sq) == file}


def _has(board: chess.Board, color: chess.Color, file: int) -> bool:
    return bool(_ranks(board, color, file))


def open_sicilian(sans: list[str], board: chess.Board) -> bool:
    black_moves = sans[1:SNAPSHOT_PLY:2]
    return sans[:2] == ["e4", "c5"] and "cxd4" in black_moves and not _has(board, chess.WHITE, D_FILE)


def kings_pawn_center(sans: list[str], board: chess.Board) -> bool:
    return (4 in _ranks(board, chess.WHITE, E_FILE) and 5 in _ranks(board, chess.BLACK, E_FILE)
            and _has(board, chess.BLACK, 2))


def small_center(sans: list[str], board: chess.Board) -> bool:
    return (4 in _ranks(board, chess.WHITE, E_FILE) and not _has(board, chess.WHITE, D_FILE)
            and 6 in _ranks(board, chess.BLACK, D_FILE) and not _has(board, chess.BLACK, E_FILE)
            and _has(board, chess.BLACK, 2))


STRUCTURES = (
    ("Open Sicilian (as Black)", chess.BLACK, open_sicilian),
    ("King's-pawn center, e4 vs e5 (as White)", chess.WHITE, kings_pawn_center),
    ("Small center, e4 vs d6 (as White)", chess.WHITE, small_center),
)


def load(conn: sqlite3.Connection, player: str, since: str, until: str, run_id: int):
    games = conn.execute(
        """
        SELECT g.game_id, pw.username = :p AS me_white
        FROM games g
        JOIN players pw ON pw.player_id = g.white_player_id
        JOIN players pb ON pb.player_id = g.black_player_id
        WHERE :p IN (pw.username, pb.username)
          AND g.time_class = 'rapid' AND g.time_control = '600' AND g.variant IS NULL
          AND g.date_played BETWEEN :since AND :until
        """,
        {"p": player, "since": since, "until": until},
    ).fetchall()
    ids = [g for g, _ in games]
    marks = ",".join("?" * len(ids))
    moves = defaultdict(list)
    for gid, ply, mv_no, san, clock, spent in conn.execute(
        f"SELECT game_id, ply, move_number, move_san, clock_seconds, time_spent_seconds "
        f"FROM moves WHERE game_id IN ({marks}) ORDER BY game_id, ply", ids,
    ):
        moves[gid].append((ply, mv_no, san, clock, spent))
    losses = {}
    for gid, ply, wp_loss in conn.execute(
        f"SELECT q.game_id, q.ply, q.wp_loss FROM engine.move_quality q "
        f"JOIN engine.game_coverage c ON c.run_id = q.run_id AND c.game_id = q.game_id "
        f"WHERE q.run_id = ? AND c.status = 'complete' AND q.game_id IN ({marks})",
        [run_id, *ids],
    ):
        losses[(gid, ply)] = wp_loss
    analysed = {gid for gid, _ in losses}
    return games, moves, losses, analysed


def structure_of(me_white: bool, rows) -> str | None:
    sans = [san for _, _, san, _, _ in rows]
    if len(sans) < SNAPSHOT_PLY:
        return None
    board = chess.Board()
    try:
        for san in sans[:SNAPSHOT_PLY]:
            board.push_san(san)
    except ValueError:
        return None
    for name, color, detect in STRUCTURES:
        if (color == chess.WHITE) == me_white and detect(sans, board):
            return name
    return None


def game_stats(me_white: bool, rows, losses, gid, analysed: bool):
    """Per game, per side: (think moves, long thinks, errors, clock after move 20)."""
    out = {}
    for side, parity in (("me", 1 if me_white else 0), ("opp", 0 if me_white else 1)):
        think_n = think_long = errors = 0
        clock20 = None
        for ply, mv_no, _, clock, spent in rows:
            if ply % 2 != parity:
                continue
            if clock is not None and spent is not None and THINK_MOVES[0] <= mv_no <= THINK_MOVES[1]:
                if clock + spent > MIN_CLOCK_S:
                    think_n += 1
                    think_long += spent > LONG_THINK_S
            if mv_no == 20:
                clock20 = clock
            if analysed and ERROR_MOVES[0] <= mv_no <= ERROR_MOVES[1]:
                errors += (losses.get((gid, ply)) or 0.0) >= ERROR_WP
        out[side] = (think_n, think_long, errors if analysed else None, clock20)
    return out


def ratio_ci(pairs):
    """Pooled rate of y/n over games, with a 95% interval that treats the game as the unit."""
    pairs = [(y, n) for y, n in pairs if n]
    if len(pairs) < 2:
        return float("nan"), float("nan")
    N = len(pairs)
    r = sum(y for y, _ in pairs) / sum(n for _, n in pairs)
    nbar = sum(n for _, n in pairs) / N
    var = sum((y - r * n) ** 2 for y, n in pairs) / (N * (N - 1)) / nbar ** 2
    return r, 1.96 * math.sqrt(var)


def mean_ci(xs):
    xs = [x for x in xs if x is not None]
    if len(xs) < 2:
        return float("nan"), float("nan")
    return statistics.fmean(xs), 1.96 * statistics.stdev(xs) / math.sqrt(len(xs))


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--player", default="ballasack6")
    p.add_argument("--since", default="2026-04-17", help="YYYY-MM-DD (default: the baseline window)")
    p.add_argument("--until", default="2026-09-28", help="YYYY-MM-DD, inclusive")
    p.add_argument("--run", type=int, default=1, help="engine run for the errors column")
    args = p.parse_args(argv)

    conn = sqlite3.connect(CANONICAL_DB)
    conn.execute(f"ATTACH DATABASE '{ENGINE_DB}' AS engine")
    games, moves, losses, analysed = load(conn, args.player, args.since, args.until, args.run)

    buckets = defaultdict(list)
    for gid, me_white in games:
        rows = moves.get(gid, [])
        name = structure_of(bool(me_white), rows)
        stats = game_stats(bool(me_white), rows, losses, gid, gid in analysed)
        if name:
            buckets[name].append(stats)
        if len(rows) >= SNAPSHOT_PLY:
            buckets["All games reaching move 12"].append(stats)

    print(f"{args.player}, rapid 10+0, {args.since} to {args.until}: {len(games)} games, "
          f"{len(analysed)} with engine run {args.run}")
    print("long thinks = share of moves 9-20 (over 2 min left) that took over 20s; "
          "clock = extra seconds used by move 20;")
    print("errors = mistakes + blunders per game in moves 9-30. +/- is a 95% interval.\n")
    head = f"{'structure':42s} {'games':>5s}  {'long thinks: you':>17s} {'them':>6s}  " \
           f"{'clock':>6s}  {'errors/game: you':>17s} {'them':>5s}"
    print(head)
    print("-" * len(head))
    for name in [s[0] for s in STRUCTURES] + ["All games reaching move 12"]:
        rows = buckets.get(name, [])
        if not rows:
            print(f"{name:42s} {0:5d}")
            continue
        lt_me, ci_me = ratio_ci([(r["me"][1], r["me"][0]) for r in rows])
        lt_op, _ = ratio_ci([(r["opp"][1], r["opp"][0]) for r in rows])
        used = [r["opp"][3] - r["me"][3] for r in rows if r["me"][3] is not None and r["opp"][3] is not None]
        er_me, eci = mean_ci([r["me"][2] for r in rows])
        er_op, _ = mean_ci([r["opp"][2] for r in rows])
        clock = f"{statistics.median(used):+.0f}s" if used else "n/a"
        print(f"{name:42s} {len(rows):5d}  {lt_me*100:8.1f}% +/-{ci_me*100:4.1f} {lt_op*100:5.1f}%  "
              f"{clock:>6s}  {er_me:9.2f} +/-{eci:4.2f} {er_op:5.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
