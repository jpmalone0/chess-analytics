"""The scorecard's pure core: eight dimensions, one side of one game at a time.

No database here. The caller hands over a game's moves, its per-position
evaluations and the engine's candidate lines; this module decides what they
mean. The design, and the reasons behind each cutoff, are in
docs/superpowers/specs/2026-09-30-scorecard-design.md.

Everything is measured the same way for both seats of a game, so the
comparison with the opponent is rating-matched by construction. The rating
scale on top (rating_score) is a separate step, fitted from other players'
games.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import NamedTuple, Optional, Sequence

import chess
import numpy as np

from engine.views import BLUNDER_WP, EVAL_CLAMP_CP, MATE_CP, MATE_MAX_PLIES, MATE_STEP_CP

# Reaching this expected score after the opening is an advantage to convert;
# falling to RESOURCE_WP is a position to save. Aimchess's own cutoffs.
ADVANTAGE_WP = 0.75
RESOURCE_WP = 0.25

# A tactical chance: the best move is forcing and beats the second-best by at
# least this much. Found: the played move lost no more than TACTIC_FOUND_WP.
# Both are provisional, to be tuned once enough three-line positions exist.
TACTIC_GAP_WP = 0.10
TACTIC_FOUND_WP = 0.05

BOOTSTRAP_RESAMPLES = 1000
BOOTSTRAP_SEED = 0

# Fewer analyzed games than this and the section shows a "!".
SMALL_SAMPLE_GAMES = 300

# Scores put a rating equivalent on a fixed 0-100 scale: 500 -> 30, 2500 -> 80.
SCORE_OFFSET = 700
SCORE_PER_POINT = 40
RATING_MIN, RATING_MAX = 0, 3000

# A calibration line is used only when it clears all of these.
FIT_MIN_OBS = 30
FIT_MIN_BANDS = 3
FIT_BAND_WIDTH = 200
FIT_MIN_T = 2.0


class Division(NamedTuple):
    """Position indexes where the middlegame and endgame begin, if they do."""
    middle: Optional[int]
    end: Optional[int]


# ── Lichess's Divider, ported from scalachess (core/src/main/scala/Divider.scala)

def _majors_and_minors(board: chess.Board) -> int:
    return chess.popcount(board.occupied & ~(board.kings | board.pawns))


def _backrank_sparse(board: chess.Board) -> bool:
    white = board.occupied_co[chess.WHITE]
    black = board.occupied_co[chess.BLACK]
    return (chess.popcount(chess.BB_RANK_1 & white) < 4
            or chess.popcount(chess.BB_RANK_8 & black) < 4)


def _region_score(y: int, white: int, black: int) -> int:
    table = {
        (0, 1): 1 + y,
        (0, 2): 2 + (6 - y) if y < 6 else 0,
        (0, 3): 3 + (7 - y) if y < 7 else 0,
        (0, 4): 3 + (7 - y) if y < 7 else 0,
        (1, 0): 1 + (8 - y),
        (1, 1): 5 + abs(4 - y),
        (1, 2): 4 + (7 - y),
        (1, 3): 5 + (7 - y),
        (2, 0): 2 + (y - 2) if y > 2 else 0,
        (2, 1): 4 + (y - 1),
        (2, 2): 7,
        (3, 0): 3 + (y - 1) if y > 1 else 0,
        (3, 1): 5 + (y - 1),
        (4, 0): 3 + (y - 1) if y > 1 else 0,
    }
    return table.get((white, black), 0)


_REGIONS = [(0x0303 << (x + 8 * y), y + 1) for y in range(7) for x in range(7)]


def _mixedness(board: chess.Board) -> int:
    white = board.occupied_co[chess.WHITE]
    black = board.occupied_co[chess.BLACK]
    return sum(
        _region_score(y, chess.popcount(white & region), chess.popcount(black & region))
        for region, y in _REGIONS
    )


def divide_boards(boards: Sequence[chess.Board]) -> Division:
    """Where the middlegame and endgame start, as Lichess decides it."""
    middle = next(
        (i for i, b in enumerate(boards)
         if _majors_and_minors(b) <= 10 or _backrank_sparse(b) or _mixedness(b) > 150),
        None,
    )
    end = next((i for i, b in enumerate(boards) if _majors_and_minors(b) <= 6), None)
    # As scalachess: a middlegame that would start at or after the endgame is
    # no middlegame at all.
    if middle is not None and end is not None and middle >= end:
        middle = None
    return Division(middle, end)


# ── Per-side facts

@dataclass(frozen=True)
class GameInput:
    game_id: int
    sans: list[str]
    # One (cp, mate_in) per position, 0..len(sans), from White's point of view.
    evals: list[tuple[Optional[int], Optional[int]]]
    # Position index -> candidate lines, best first, as (uci, cp, mate_in).
    pvs: dict[int, list[tuple[str, Optional[int], Optional[int]]]] = field(default_factory=dict)
    result: str = "1/2-1/2"
    termination: str = ""


class SideFacts(NamedTuple):
    opening: float = 0.0
    middlegame: float = 0.0
    endgame: float = 0.0
    flag_loss: float = 0.0
    reached: bool = False
    won: bool = False
    fell: bool = False
    saved: bool = False
    chances: int = 0
    found: int = 0
    blunders: int = 0


def _white_cp(cp: Optional[int], mate_in: Optional[int], index: int) -> Optional[float]:
    """engine.views.MOVE_EVALS_VIEW's mate conversion, then its clamp."""
    if cp is not None:
        value = float(cp)
    elif mate_in is None:
        return None
    elif mate_in == 0:
        # The side to move is mated; White moves at even indexes.
        value = -MATE_CP if index % 2 == 0 else MATE_CP
    elif mate_in > 0:
        value = MATE_CP - MATE_STEP_CP * min(mate_in, MATE_MAX_PLIES)
    else:
        value = -MATE_CP + MATE_STEP_CP * min(-mate_in, MATE_MAX_PLIES)
    return max(-EVAL_CLAMP_CP, min(EVAL_CLAMP_CP, value))


def _wp(white_cp: float, color: str, k: float) -> float:
    sign = 1 if color == "white" else -1
    return 1.0 / (1.0 + math.exp(-(sign * white_cp) / k))


def _mover(index: int) -> str:
    return "white" if index % 2 == 0 else "black"


def _phase(index: int, division: Division) -> str:
    if division.end is not None and index >= division.end:
        return "endgame"
    if division.middle is None or index < division.middle:
        return "opening"
    return "middlegame"


def _flag_loss(game: GameInput, color: str, final_wp: Optional[float]) -> float:
    if final_wp is None:
        return 0.0
    term = game.termination.lower()
    if "won on time" in term:
        loser = "black" if game.result == "1-0" else "white" if game.result == "0-1" else None
        return final_wp if loser == color else 0.0
    if "timeout vs insufficient material" in term:
        flagged = _mover(len(game.sans))
        return max(0.0, final_wp - 0.5) if flagged == color else 0.0
    return 0.0


def _boards(sans: Sequence[str]) -> tuple[list[chess.Board], list[chess.Move]]:
    board = chess.Board()
    boards, moves = [board.copy(stack=False)], []
    for san in sans:
        move = board.parse_san(san)
        moves.append(move)
        board.push(move)
        boards.append(board.copy(stack=False))
    return boards, moves


def _is_chance(game: GameInput, index: int, board: chess.Board,
               previous: Optional[chess.Move], prev_board: Optional[chess.Board],
               k: float) -> bool:
    lines = game.pvs.get(index) or []
    if len(lines) < 2:
        return False
    color = _mover(index)
    best = chess.Move.from_uci(lines[0][0])
    if best not in board.legal_moves:
        return False
    if not (board.is_capture(best) or board.gives_check(best)):
        return False
    # A plain recapture: taking back on the square just captured on. The gap
    # to any other move is huge, and finding it says nothing about vision.
    if (previous is not None and prev_board is not None
            and prev_board.is_capture(previous) and best.to_square == previous.to_square):
        return False
    first = _white_cp(lines[0][1], lines[0][2], index + 1)
    second = _white_cp(lines[1][1], lines[1][2], index + 1)
    if first is None or second is None:
        return False
    return _wp(first, color, k) - _wp(second, color, k) >= TACTIC_GAP_WP


def game_sides(game: GameInput, k: float,
               division: Optional[Division] = None) -> dict[str, SideFacts]:
    """Both seats' facts for one game. `division` overrides the Divider."""
    boards, moves = _boards(game.sans)
    if division is None:
        division = divide_boards(boards)
    cps = [_white_cp(cp, mate, i) for i, (cp, mate) in enumerate(game.evals)]

    acc: dict[str, dict] = {
        c: {"opening": 0.0, "middlegame": 0.0, "endgame": 0.0,
            "reached": False, "fell": False, "chances": 0, "found": 0, "blunders": 0}
        for c in ("white", "black")
    }

    # Swings are judged from every evaluated position after the opening.
    for i, cp in enumerate(cps):
        if cp is None or _phase(i, division) == "opening":
            continue
        for color in ("white", "black"):
            wp = _wp(cp, color, k)
            if wp >= ADVANTAGE_WP:
                acc[color]["reached"] = True
            if wp <= RESOURCE_WP:
                acc[color]["fell"] = True

    for i in range(len(moves)):
        before, after = cps[i], cps[i + 1] if i + 1 < len(cps) else None
        if before is None or after is None:
            continue
        color = _mover(i)
        side = acc[color]
        change = _wp(after, color, k) - _wp(before, color, k)
        phase = _phase(i, division)
        side[phase] += change
        loss = max(0.0, -change)
        chance = phase != "opening" and _is_chance(
            game, i, boards[i],
            moves[i - 1] if i > 0 else None, boards[i - 1] if i > 0 else None, k)
        if chance:
            side["chances"] += 1
            if loss <= TACTIC_FOUND_WP:
                side["found"] += 1
        elif loss >= BLUNDER_WP:
            side["blunders"] += 1

    final = cps[-1] if cps else None
    out = {}
    for color in ("white", "black"):
        won = game.result == ("1-0" if color == "white" else "0-1")
        drew = game.result == "1/2-1/2"
        final_wp = _wp(final, color, k) if final is not None else None
        side = acc[color]
        out[color] = SideFacts(
            opening=side["opening"], middlegame=side["middlegame"],
            endgame=side["endgame"],
            flag_loss=_flag_loss(game, color, final_wp),
            reached=side["reached"], won=won and side["reached"],
            fell=side["fell"], saved=(won or drew) and side["fell"],
            chances=side["chances"], found=side["found"], blunders=side["blunders"],
        )
    return out


# ── Rows

class Dimension(NamedTuple):
    key: str
    label: str
    unit: str            # "points" | "percent" | "per_game"
    higher_is_better: bool


DIMENSIONS = (
    Dimension("opening", "Opening", "points", True),
    Dimension("middlegame", "Middlegame", "points", True),
    Dimension("endgame", "Endgame", "points", True),
    Dimension("time", "Time management", "points", False),
    Dimension("advantage", "Advantage capitalization", "percent", True),
    Dimension("resourcefulness", "Resourcefulness", "percent", True),
    Dimension("tactics", "Tactics found", "percent", True),
    Dimension("blunders", "Blunders", "per_game", False),
)


def unit_counts(key: str, s: SideFacts) -> tuple[float, float]:
    """One side-game's (numerator, denominator) for a dimension.

    Every row is a ratio of sums, so one procedure covers per-game rows
    (denominator 1) and rates (denominator = chances in that game)."""
    if key in ("opening", "middlegame", "endgame"):
        return getattr(s, key), 1.0
    if key == "time":
        return s.flag_loss, 1.0
    if key == "advantage":
        return float(s.won), float(s.reached)
    if key == "resourcefulness":
        return float(s.saved), float(s.fell)
    if key == "tactics":
        return float(s.found), float(s.chances)
    if key == "blunders":
        return float(s.blunders), 1.0
    raise KeyError(key)


def _ratio(num: np.ndarray, den: np.ndarray) -> np.ndarray:
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.where(den > 0, num / np.where(den > 0, den, 1), np.nan)


def summarize(pairs: Sequence[tuple[SideFacts, SideFacts]]) -> list[dict]:
    """One row per dimension: you, opponents, their difference and its range."""
    n = len(pairs)
    rng = np.random.default_rng(BOOTSTRAP_SEED)
    idx = rng.integers(0, n, size=(BOOTSTRAP_RESAMPLES, n)) if n else None
    rows = []
    for dim in DIMENSIONS:
        you = np.array([unit_counts(dim.key, p[0]) for p in pairs]).reshape(n, 2)
        opp = np.array([unit_counts(dim.key, p[1]) for p in pairs]).reshape(n, 2)
        y_den, o_den = you[:, 1].sum(), opp[:, 1].sum()
        row = {"key": dim.key, "label": dim.label, "unit": dim.unit,
               "higher_is_better": dim.higher_is_better,
               "you_n": float(y_den), "opp_n": float(o_den),
               "you": None, "opp": None, "diff": None, "lo": None, "hi": None,
               "verdict": None}
        if n and y_den > 0 and o_den > 0:
            y_val, o_val = you[:, 0].sum() / y_den, opp[:, 0].sum() / o_den
            assert idx is not None
            boot = (_ratio(you[idx, 0].sum(1), you[idx, 1].sum(1))
                    - _ratio(opp[idx, 0].sum(1), opp[idx, 1].sum(1)))
            boot = boot[~np.isnan(boot)]
            lo, hi = (np.percentile(boot, [2.5, 97.5]) if boot.size
                      else (np.nan, np.nan))
            row.update(
                you=float(y_val), opp=float(o_val), diff=float(y_val - o_val),
                lo=float(lo), hi=float(hi),
                verdict="real" if (lo > 0 or hi < 0) else "noise",
            )
        rows.append(row)
    return rows


# ── Rating scale

class Fit(NamedTuple):
    a: float
    b: float
    n: float
    t: float

    def rating_for(self, value: float) -> float:
        return max(RATING_MIN, min(RATING_MAX, (value - self.a) / self.b))


def fit_line(xs: Sequence[float], ys: Sequence[float], ws: Sequence[float],
             direction: int) -> Optional[Fit]:
    """Weighted least squares of a metric on rating, or None if untrustworthy.

    Weights are each observation's denominator, so a game with three tactic
    chances counts three times; that makes a rate's line a line over chances.
    """
    x, y, w = (np.asarray(v, dtype=float) for v in (xs, ys, ws))
    keep = w > 0
    x, y, w = x[keep], y[keep], w[keep]
    n = w.sum()
    if n < FIT_MIN_OBS or len(set((x // FIT_BAND_WIDTH).tolist())) < FIT_MIN_BANDS:
        return None
    xm, ym = np.average(x, weights=w), np.average(y, weights=w)
    sxx = (w * (x - xm) ** 2).sum()
    if sxx <= 0:
        return None
    b = (w * (x - xm) * (y - ym)).sum() / sxx
    a = ym - b * xm
    resid = (w * (y - a - b * x) ** 2).sum() / max(n - 2, 1)
    se = math.sqrt(resid / sxx) if resid > 0 else 0.0
    t = b / se if se > 0 else math.inf * np.sign(b)
    if b * direction <= 0 or abs(t) < FIT_MIN_T:
        return None
    return Fit(float(a), float(b), float(n), float(t))


def rating_score(rating: float) -> float:
    return max(0.0, min(100.0, (rating + SCORE_OFFSET) / SCORE_PER_POINT))
