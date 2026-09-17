"""Screen a candidate positional metric before it earns a column.

Two questions, in order:

  1. Does the metric vary with rating at all, within (opening, colour)?
     If not, it cannot support a finding about any player.
  2. Is it a stable trait of the player -- does one half of someone's games
     predict the other half?

A metric that passes (1) measures ability. One that fails (1) but passes (2)
measures style. One that fails both is noise.

    uv run python analysis/screen.py bullet
    uv run python analysis/screen.py rapid
    uv run python analysis/screen.py all      # both, pooled -- more players
                                              # clear the >=30 game bar

Engine-free: ~12s over 75,669 bullet games.
"""

from __future__ import annotations

import math
import random
import sqlite3
import sys
from collections import defaultdict

import chess

sys.path.insert(0, ".")
from analysis.metrics import SNAPSHOT_PLY, STYLE_AXES, passed_pawns  # noqa: E402

METRICS = {**STYLE_AXES, "passed_pawns": passed_pawns}

MIN_CELL = 40        # observations per (ECO, colour) before it can be centred
MIN_BANDS = 3        # rating bands per cell, so the centre is not one band's taste
MIN_GAMES = 30       # games per player for the reliability half-split

BANDS = ((1200, "a <1200"), (1600, "b 1200-1599"), (2000, "c 1600-1999"),
         (2400, "d 2000-2399"), (2900, "e 2400-2899"))


def band_of(elo: int | None) -> str | None:
    if elo is None:
        return None
    for ceiling, name in BANDS:
        if elo < ceiling:
            return name
    return "f 2900+"


def mean(xs):
    return sum(xs) / len(xs) if xs else float("nan")


def corr(xs, ys):
    mx, my = mean(xs), mean(ys)
    num = sum((a - mx) * (b - my) for a, b in zip(xs, ys, strict=True))
    den = math.sqrt(sum((a - mx) ** 2 for a in xs) * sum((b - my) ** 2 for b in ys))
    return num / den if den else float("nan")


def observe(con: sqlite3.Connection, time_class: str) -> list[dict]:
    """One observation per player per game: their metric values at SNAPSHOT_PLY.

    Both colours are measured in the same position, which is why the metrics are
    computed from attack maps rather than legal moves.
    """
    games = con.execute(
        """SELECT g.game_id, g.eco, g.white_elo, g.black_elo, pw.username, pb.username
           FROM games g
           JOIN players pw ON pw.player_id = g.white_player_id
           JOIN players pb ON pb.player_id = g.black_player_id
           WHERE g.variant IS NULL AND g.time_class = ? AND g.white_elo IS NOT NULL""",
        (time_class,)).fetchall()

    sans = defaultdict(list)
    for game_id, san in con.execute(
            """SELECT m.game_id, m.move_san FROM moves m
               JOIN games g ON g.game_id = m.game_id
               WHERE g.variant IS NULL AND g.time_class = ? AND m.ply <= ?
               ORDER BY m.game_id, m.ply""", (time_class, SNAPSHOT_PLY)):
        sans[game_id].append(san)

    out = []
    for game_id, eco, white_elo, black_elo, white, black in games:
        moves = sans.get(game_id)
        if not moves or len(moves) < SNAPSHOT_PLY:
            continue
        board = chess.Board()
        try:
            for san in moves:
                board.push_san(san)
        except ValueError:
            continue    # a game that stops reconstructing contributes nothing
        for color, elo, user in ((chess.WHITE, white_elo, white),
                                 (chess.BLACK, black_elo, black)):
            band = band_of(elo)
            if band:
                out.append({"tc": time_class, "eco": (eco or "?")[:3],
                            "color": "w" if color == chess.WHITE else "b",
                            "band": band, "user": user, "elo": elo,
                            **{k: f(board, color) for k, f in METRICS.items()}})
    return out


def centre(obs: list[dict]) -> list[dict]:
    """Subtract each (time class, ECO, colour) cell's own mean.

    This is what makes the comparison "more than is normal for this opening from
    this side". Without it, colour alone produces effects above t=7 -- White has
    more space than Black, which is a fact about chess and not about a player.
    """
    cells = defaultdict(list)
    for o in obs:
        cells[(o["tc"], o["eco"], o["color"])].append(o)
    usable = [v for v in cells.values()
              if len(v) >= MIN_CELL and len({x["band"] for x in v}) >= MIN_BANDS]
    for group in usable:
        for key in METRICS:
            m = mean([x[key] for x in group])
            for x in group:
                x[key] -= m
    return [o for group in usable for o in group]


def gradient(kept: list[dict]) -> None:
    bands = sorted({o["band"] for o in kept})
    index = {b: i for i, b in enumerate(bands)}
    print("\nRATING GRADIENT  (does it track skill?)")
    header = " ".join(f"{b.split(' ', 1)[1]:>9s}" for b in bands)
    print(f'{"metric":15s} {header}        r')
    for key in METRICS:
        cells = " ".join(
            f"{mean([o[key] for o in kept if o['band'] == b]):+9.3f}" for b in bands)
        r = corr([index[o["band"]] for o in kept], [o[key] for o in kept])
        print(f"  {key:13s} {cells}  {r:+.4f}")


def reliability(kept: list[dict], seed: int = 11, splits: int = 25) -> None:
    """Average the half-split over many shuffles.

    A single split is unstable: for a metric whose true reliability is near zero
    one shuffle put passed pawns at -0.05 and another at +0.17. Averaging makes
    the estimate reproducible and stops a noise metric looking like a signed
    result in either direction.
    """
    by_player = defaultdict(list)
    for o in kept:
        by_player[o["user"]].append(o)
    eligible = {u: v for u, v in by_player.items() if len(v) >= MIN_GAMES}
    print(f"\nSPLIT-HALF RELIABILITY  ({len(eligible):,} players with "
          f">={MIN_GAMES} games)")
    print(f'{"metric":15s} {"r_half":>8s} {"corrected":>10s} {"r with Elo":>12s}')
    rng = random.Random(seed)
    elos = [mean([o["elo"] for o in g]) for g in eligible.values()]
    for key in METRICS:
        halves = []
        for _ in range(splits):
            first, second = [], []
            for games in eligible.values():
                shuffled = games[:]
                rng.shuffle(shuffled)
                half = len(shuffled) // 2
                first.append(mean([o[key] for o in shuffled[:half]]))
                second.append(mean([o[key] for o in shuffled[half:]]))
            halves.append(corr(first, second))
        r = mean(halves)
        spread = max(halves) - min(halves)
        # Spearman-Brown: the half-split understates the full-length reliability.
        corrected = 2 * r / (1 + r) if r > -1 else float("nan")
        with_elo = corr([mean([o[key] for o in g]) for g in eligible.values()], elos)
        print(f"  {key:13s} {r:+8.3f} {corrected:+10.3f} {with_elo:+12.3f}"
              f"   (spread over {splits} splits: {spread:.3f})")


def main(argv: list[str] | None = None) -> int:
    argv = argv or sys.argv[1:]
    requested = argv[0] if argv else "bullet"
    classes = ["rapid", "bullet"] if requested == "all" else [requested]
    con = sqlite3.connect("chess_analytics.db")
    con.execute("PRAGMA query_only = 1")

    obs = [o for tc in classes for o in observe(con, tc)]
    kept = centre(obs)
    print(f"{requested}: {len(obs):,} observations, "
          f"{len({o['user'] for o in obs}):,} players; "
          f"{len(kept):,} kept after centring")
    gradient(kept)
    reliability(kept)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
