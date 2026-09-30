"""Screen a sound version of Aimchess's six-spoke radar before anything earns a place in the app.

Every row compares you with your opponents IN THE SAME GAMES, the mirror control this project
uses throughout, and puts a 95% range on the difference so each row says whether the gap is
real or noise. Two changes from the radar: tactics is split in two, because Aimchess's tactics
spoke counts blunders rather than tactics found, and a middlegame row sits between opening and
endgame, the phase the radar leaves out.

    uv run python analysis/scorecard_screen.py              # your last 100 analysed games
    uv run python analysis/scorecard_screen.py --games 900  # everything analysed

Definitions. Expected score comes from the tool's fitted curve (engine.move_quality); phase
rows sum the expected score each side gave away per game, so they are paired game by game.

  Opening            moves 1-15
  Middlegame         move 16 until the endgame, mover with more than a minute left
  Endgame            Lichess's rule (queens + rooks + bishops + knights <= 6), more than a minute
  Tactics: found     after move 8, positions whose best move is a capture or check other than a
                     plain recapture: the share where exactly that move was played
  Tactics: blunders  blunders (lost >= 20% expected score) per game
  Advantage cap.     games where the side reached 80%+ after move 10: the share won
  Resourcefulness    games where the side fell to 20% or less after move 10: won or drawn
  Time               share of games lost on time

Raw comparisons, not standardised on position: a screen, not a finding. Runs in seconds.
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

OPENING_LAST_MOVE = 15
MIN_CLOCK_S = 60.0            # below this, moves belong to the scramble, which Time covers
BLUNDER_WP = 0.20
ADVANTAGE_WP, DISADVANTAGE_WP = 0.80, 0.20
AFTER_MOVE = 10               # advantage and disadvantage count only once the opening is over
TACTICS_AFTER_MOVE = 8
ENDGAME_PIECES = 6
MAJORS_MINORS = (chess.QUEEN, chess.ROOK, chess.BISHOP, chess.KNIGHT)
SIDES = ("me", "opp")


def load(conn: sqlite3.Connection, player: str, n_games: int, run_id: int):
    games = conn.execute(
        """
        SELECT g.game_id, g.result, g.termination, pw.username = :p AS me_white, g.date_played
        FROM games g
        JOIN players pw ON pw.player_id = g.white_player_id
        JOIN players pb ON pb.player_id = g.black_player_id
        JOIN engine.game_coverage c
          ON c.game_id = g.game_id AND c.run_id = :r AND c.status = 'complete'
        WHERE :p IN (pw.username, pb.username)
          AND g.time_class = 'rapid' AND g.time_control = '600' AND g.variant IS NULL
        ORDER BY g.end_time DESC
        LIMIT :n
        """,
        {"p": player, "r": run_id, "n": n_games},
    ).fetchall()
    ids = [g[0] for g in games]
    marks = ",".join("?" * len(ids))
    moves = defaultdict(list)
    for row in conn.execute(
        f"SELECT game_id, ply, move_number, move_san, clock_seconds, time_spent_seconds "
        f"FROM moves WHERE game_id IN ({marks}) ORDER BY game_id, ply", ids,
    ):
        moves[row[0]].append(row[1:])
    quality = {(g, p): (wb, wl) for g, p, wb, wl in conn.execute(
        f"SELECT game_id, ply, wp_before, wp_loss FROM engine.move_quality "
        f"WHERE run_id = ? AND game_id IN ({marks})", [run_id, *ids],
    )}
    best = {(g, p): u for g, p, u in conn.execute(
        f"SELECT game_id, ply, best_move_uci FROM engine.position_evals "
        f"WHERE run_id = ? AND game_id IN ({marks}) AND best_move_uci IS NOT NULL", [run_id, *ids],
    )}
    return games, moves, quality, best


def my_score(result: str, me_white: bool) -> float:
    if result == "1/2-1/2":
        return 0.5
    return 1.0 if (result == "1-0") == bool(me_white) else 0.0


def game_stats(gid, me_white, rows, quality, best):
    """One game, both sides: phase losses, blunders, tactical chances, advantage reached."""
    s = {side: {"opening": 0.0, "middlegame": 0.0, "endgame": 0.0, "blunders": 0,
                "chances": 0, "found": 0, "max_wp": None, "min_wp": None} for side in SIDES}
    board = chess.Board()
    last_capture_sq = None
    for ply, mv_no, san, clock, spent in rows:
        side = "me" if (ply % 2 == 1) == bool(me_white) else "opp"
        try:
            move = board.parse_san(san)
        except ValueError:
            break
        endgame = sum(len(board.pieces(p, c)) for p in MAJORS_MINORS for c in chess.COLORS) <= ENDGAME_PIECES

        uci = best.get((gid, ply - 1))
        if uci and mv_no > TACTICS_AFTER_MOVE:
            bm = chess.Move.from_uci(uci)
            if bm in board.legal_moves and (board.is_capture(bm) or board.gives_check(bm)):
                if not (board.is_capture(bm) and bm.to_square == last_capture_sq):
                    s[side]["chances"] += 1
                    s[side]["found"] += bm == move

        q = quality.get((gid, ply))
        if q is not None:
            wp_before, wp_loss = q
            clock_before = clock + spent if clock is not None and spent is not None else None
            has_time = clock_before is None or clock_before > MIN_CLOCK_S
            if mv_no <= OPENING_LAST_MOVE:
                s[side]["opening"] += wp_loss
            elif endgame and has_time:
                s[side]["endgame"] += wp_loss
            elif not endgame and has_time:
                s[side]["middlegame"] += wp_loss
            s[side]["blunders"] += wp_loss >= BLUNDER_WP
            if mv_no > AFTER_MOVE:
                cur_max, cur_min = s[side]["max_wp"], s[side]["min_wp"]
                s[side]["max_wp"] = wp_before if cur_max is None else max(cur_max, wp_before)
                s[side]["min_wp"] = wp_before if cur_min is None else min(cur_min, wp_before)

        last_capture_sq = move.to_square if board.is_capture(move) else None
        board.push(move)
    return s


def paired(pairs):
    """Per-game means for each side, and a 95% range on the per-game difference."""
    n = len(pairs)
    d = [a - b for a, b in pairs]
    half = 1.96 * statistics.stdev(d) / math.sqrt(n)
    m = statistics.fmean(d)
    return {"you": statistics.fmean(a for a, _ in pairs), "opp": statistics.fmean(b for _, b in pairs),
            "diff": m, "lo": m - half, "hi": m + half, "n": f"{n} games"}


def ratio(pairs):
    """Pooled y/n with a standard error that treats the game as the unit."""
    pairs = [(y, n) for y, n in pairs if n]
    N = len(pairs)
    r = sum(y for y, _ in pairs) / sum(n for _, n in pairs)
    nbar = sum(n for _, n in pairs) / N
    se = math.sqrt(sum((y - r * n) ** 2 for y, n in pairs) / (N * (N - 1))) / nbar
    return r, se, sum(n for _, n in pairs)


def proportion(xs):
    n = len(xs)
    p = sum(xs) / n
    return p, math.sqrt(p * (1 - p) / n), n


def two_sample(a, b, unit):
    diff, half = a[0] - b[0], 1.96 * math.hypot(a[1], b[1])
    return {"you": a[0], "opp": b[0], "diff": diff, "lo": diff - half, "hi": diff + half,
            "n": f"{a[2]} / {b[2]} {unit}"}


def verdict(r, better):
    if r["lo"] <= 0 <= r["hi"]:
        return "noise"
    you_better = r["diff"] < 0 if better == "lower" else r["diff"] > 0
    return "real: you better" if you_better else "real: you worse"


def fmt(v, kind):
    if kind != "pct":
        return f"{v:.2f}"
    return f"{v * 100:.1f}%" if v < 0.1 else f"{v * 100:.0f}%"


def fmt_range(r, kind):
    if kind == "pct":
        return f"{r['diff'] * 100:+.0f} pts ({r['lo'] * 100:+.0f} to {r['hi'] * 100:+.0f})"
    return f"{r['diff']:+.2f} ({r['lo']:+.2f} to {r['hi']:+.2f})"


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--player", default="ballasack6")
    p.add_argument("--games", type=int, default=100, help="most recent analysed rapid 10+0 games")
    p.add_argument("--run", type=int, default=1, help="engine run")
    args = p.parse_args(argv)

    conn = sqlite3.connect(CANONICAL_DB)
    conn.execute(f"ATTACH DATABASE '{ENGINE_DB}' AS engine")
    games, moves, quality, best = load(conn, args.player, args.games, args.run)
    if len(games) < 2:
        print("not enough analysed games")
        return 1

    per_game, adv, res, flags = [], {s: [] for s in SIDES}, {s: [] for s in SIDES}, []
    for gid, result, termination, me_white, _ in games:
        st = game_stats(gid, me_white, moves.get(gid, []), quality, best)
        per_game.append(st)
        score = {"me": my_score(result, me_white)}
        score["opp"] = 1.0 - score["me"]
        for side in SIDES:
            if st[side]["max_wp"] is not None and st[side]["max_wp"] >= ADVANTAGE_WP:
                adv[side].append(score[side] == 1.0)
            if st[side]["min_wp"] is not None and st[side]["min_wp"] <= DISADVANTAGE_WP:
                res[side].append(score[side] >= 0.5)
        on_time = "on time" in (termination or "").lower()
        flags.append((float(on_time and score["me"] == 0.0), float(on_time and score["opp"] == 0.0)))

    rows = []
    for key, label, measure in [
        ("opening", "Opening", "expected score given away per game, moves 1-15"),
        ("middlegame", "Middlegame", "expected score given away per game, move 16 on"),
        ("endgame", "Endgame", "expected score given away per game, endgames"),
    ]:
        rows.append((label, measure, "num", "lower",
                     paired([(g["me"][key], g["opp"][key]) for g in per_game])))
    rows.append(("Tactics: found", "real tactical chances played exactly", "pct", "higher",
                 two_sample(ratio([(g["me"]["found"], g["me"]["chances"]) for g in per_game]),
                            ratio([(g["opp"]["found"], g["opp"]["chances"]) for g in per_game]),
                            "chances")))
    rows.append(("Tactics: blunders", "blunders per game", "num", "lower",
                 paired([(g["me"]["blunders"], g["opp"]["blunders"]) for g in per_game])))
    rows.append(("Advantage cap.", "won after reaching 80%+ expected score", "pct", "higher",
                 two_sample(proportion(adv["me"]), proportion(adv["opp"]), "games")))
    rows.append(("Resourcefulness", "won or drew after falling to 20% or less", "pct", "higher",
                 two_sample(proportion(res["me"]), proportion(res["opp"]), "games")))
    rows.append(("Time", "games lost on time", "pct", "lower", paired(flags)))

    dates = sorted(g[4] for g in games)
    print(f"{args.player}: last {len(games)} analysed rapid 10+0 games, {dates[0]} to {dates[-1]}")
    print("You vs your opponents in the same games. Range = 95% interval on the difference.\n")
    head = f"{'':18s} {'measure':46s} {'you':>6s} {'them':>6s}  {'difference (95% range)':27s} {'verdict':17s} {'sample'}"
    print(head)
    print("-" * len(head))
    for label, measure, kind, better, r in rows:
        print(f"{label:18s} {measure:46s} {fmt(r['you'], kind):>6s} {fmt(r['opp'], kind):>6s}  "
              f"{fmt_range(r, kind):27s} {verdict(r, better):17s} {r['n']}")
    print("\nnoise = the range includes zero. More games narrow the range: --games 900 uses everything.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
