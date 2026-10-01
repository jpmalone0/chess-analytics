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

# Two clocks are even when they are within this share of the larger one, so
# "even" tightens as the clocks run down: 5:00 vs 4:35 is even, 0:30 vs 0:20
# is not.
CLOCK_EVEN_SHARE = 0.10


# Fewer analyzed games than this and the section shows a "!".
SMALL_SAMPLE_GAMES = 300

# A spoke's Elo is clamped to this range.
RATING_MIN, RATING_MAX = 0, 3000

# A band line exists only with this much spread behind it. How far an Elo can
# be trusted is carried by its range (elo_range), not by a cutoff.
FIT_MIN_OBS = 30
FIT_MIN_BANDS = 3
FIT_BAND_WIDTH = 200

Z95 = 1.96


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
    # Seconds left on the mover's clock after each move, by move index; None
    # where the clock is missing.
    clocks: list[Optional[float]] = field(default_factory=list)


class SideFacts(NamedTuple):
    opening: float = 0.0
    middlegame: float = 0.0
    endgame: float = 0.0
    reached: bool = False
    won: bool = False
    fell: bool = False
    saved: bool = False
    chances: int = 0
    found: int = 0
    blunders: int = 0
    opening_moves: int = 0
    middlegame_moves: int = 0
    endgame_moves: int = 0
    # Moves made ahead of, even with, or behind the opponent on the clock.
    clock_ahead: int = 0
    clock_even: int = 0
    clock_behind: int = 0


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


def _clock_counts(game: GameInput) -> dict[str, list[int]]:
    """Each side's [ahead, even, behind] move counts.

    At each move, the mover's clock after it against the opponent's clock after
    their previous move. White's first move has nothing to compare with.
    """
    out = {"white": [0, 0, 0], "black": [0, 0, 0]}
    for i in range(1, len(game.clocks)):
        mine, theirs = game.clocks[i], game.clocks[i - 1]
        if mine is None or theirs is None:
            continue
        counts = out[_mover(i)]
        if abs(mine - theirs) <= CLOCK_EVEN_SHARE * max(mine, theirs):
            counts[1] += 1
        elif mine > theirs:
            counts[0] += 1
        else:
            counts[2] += 1
    return out


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
            "reached": False, "fell": False, "chances": 0, "found": 0, "blunders": 0,
            "opening_moves": 0, "middlegame_moves": 0, "endgame_moves": 0}
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
        side[phase + "_moves"] += 1
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

    clock = _clock_counts(game)
    out = {}
    for color in ("white", "black"):
        won = game.result == ("1-0" if color == "white" else "0-1")
        drew = game.result == "1/2-1/2"
        side = acc[color]
        out[color] = SideFacts(
            opening=side["opening"], middlegame=side["middlegame"],
            endgame=side["endgame"],
            reached=side["reached"], won=won and side["reached"],
            fell=side["fell"], saved=(won or drew) and side["fell"],
            chances=side["chances"], found=side["found"], blunders=side["blunders"],
            opening_moves=side["opening_moves"],
            middlegame_moves=side["middlegame_moves"],
            endgame_moves=side["endgame_moves"],
            clock_ahead=clock[color][0], clock_even=clock[color][1],
            clock_behind=clock[color][2],
        )
    return out


# ── Rows

class Dimension(NamedTuple):
    key: str
    label: str
    unit: str
    higher_is_better: bool
    # False where a rating equivalent means nothing: clock share averages about
    # 50% at every rating in rating-matched games, and a beginner can have a
    # perfect clock. Advantage capitalization and resourcefulness are mirror
    # images in rating-matched games (your conversion is your opponent's failure
    # to save), so at most one could rise with rating; neither gets an Elo.
    has_elo: bool = True
    # How a row without an Elo becomes a 0-100 score: "share" is the value as a
    # percentage; "vs_band" puts the band at your rating at 50, with 0 and 100
    # at the rate's own limits.
    score: Optional[str] = None


DIMENSIONS = (
    Dimension("opening", "Opening", "points_per_move", True),
    Dimension("middlegame", "Middlegame", "points_per_move", True),
    Dimension("endgame", "Endgame", "points_per_move", True),
    Dimension("tactics", "Tactics found", "percent", True),
    Dimension("blunders", "Blunders", "per_move", False),
    # The 0-100 dimensions, grouped last so the wheel keeps them together.
    Dimension("time", "Time management", "percent", True, has_elo=False, score="share"),
    Dimension("advantage", "Advantage capitalization", "percent", True,
              has_elo=False, score="vs_band"),
    Dimension("resourcefulness", "Resourcefulness", "percent", True,
              has_elo=False, score="vs_band"),
)


def unit_counts(key: str, s: SideFacts) -> tuple[float, float]:
    """One side-game's (numerator, denominator) for a dimension.

    Every row is a ratio of sums, so one procedure covers per-game rows
    (denominator 1) and rates (denominator = chances in that game)."""
    if key in ("opening", "middlegame", "endgame"):
        return getattr(s, key), 1.0
    if key == "time":
        # Even counts half, as a draw does: 50% is level with your opponents.
        return (s.clock_ahead + 0.5 * s.clock_even,
                float(s.clock_ahead + s.clock_even + s.clock_behind))
    if key == "advantage":
        return float(s.won), float(s.reached)
    if key == "resourcefulness":
        return float(s.saved), float(s.fell)
    if key == "tactics":
        return float(s.found), float(s.chances)
    if key == "blunders":
        return float(s.blunders), 1.0
    raise KeyError(key)


def calibration_counts(key: str, s: SideFacts) -> tuple[float, float]:
    """The (numerator, denominator) a rating line is fitted on.

    Per move for the phase rows and blunders, where the rows show per game.
    Across ratings, game length is not neutral: low-rated games end early, so
    they rarely reach an endgame and have fewer moves to blunder on, and a
    per-game total makes them look better. Measured on the first calibration
    sample, endgame points per game *fell* with rating (t = -3.8). Within one
    game both seats share its length, so the rows can stay per game."""
    if key in ("opening", "middlegame", "endgame"):
        return getattr(s, key), float(getattr(s, key + "_moves"))
    if key == "blunders":
        return float(s.blunders), float(s.opening_moves + s.middlegame_moves + s.endgame_moves)
    return unit_counts(key, s)


# ── The rating band

class Fit(NamedTuple):
    """A weighted least-squares line of a metric on rating."""
    a: float
    b: float
    n: int          # side-games behind it
    t: float
    xm: float
    sxx: float
    s2: float       # residual variance at unit weight
    sw: float       # total weight

    def at(self, rating: float) -> float:
        return self.a + self.b * rating

    def se_at(self, rating: float) -> float:
        """Standard error of the line's level at `rating`."""
        return math.sqrt(self.s2 * (1 / self.sw + (rating - self.xm) ** 2 / self.sxx))

    def elo_for(self, value: float) -> Optional[float]:
        """The rating at which the line reaches `value`, clamped. A flat line
        has no answer."""
        if self.b == 0:
            return None
        return max(RATING_MIN, min(RATING_MAX, (value - self.a) / self.b))


def fit_band(xs: Sequence[float], ys: Sequence[float], ws: Sequence[float]) -> Optional[Fit]:
    """The band line, or None when too few games or ratings stand behind it.

    Weights are each observation's denominator (chances, or moves), so a
    side-game's rate counts in proportion to how much it rests on. They set
    precision, not sample size: the moves of one game are not independent
    draws, so the count of side-games is the n for the threshold and for the
    residual variance alike.
    """
    x, y, w = (np.asarray(v, dtype=float) for v in (xs, ys, ws))
    keep = w > 0
    x, y, w = x[keep], y[keep], w[keep]
    n = len(x)
    if n < FIT_MIN_OBS or len(set((x // FIT_BAND_WIDTH).tolist())) < FIT_MIN_BANDS:
        return None
    xm, ym = np.average(x, weights=w), np.average(y, weights=w)
    sxx = float((w * (x - xm) ** 2).sum())
    if sxx <= 0:
        return None
    b = float((w * (x - xm) * (y - ym)).sum() / sxx)
    a = float(ym - b * xm)
    s2 = float((w * (y - a - b * x) ** 2).sum() / (n - 2))
    se = math.sqrt(s2 / sxx) if s2 > 0 else 0.0
    t = b / se if se > 0 else (math.inf if b else 0.0)
    return Fit(a, b, n, float(t), float(xm), sxx, s2, float(w.sum()))


def ratio_and_variance(counts: np.ndarray) -> tuple[float, float]:
    """A ratio of sums over games, and its variance treating games as the units.

    The standard ratio-estimator variance: residuals from the pooled ratio,
    game by game, so a game's moves or chances stay together."""
    n = len(counts)
    num, den = counts[:, 0], counts[:, 1]
    total = den.sum()
    value = float(num.sum() / total)
    if n < 2:
        return value, 0.0
    resid = num - value * den
    return value, float(n / (n - 1) * (resid ** 2).sum() / total ** 2)


def elo_range(value: float, var_value: float, fit: Fit) -> tuple[float, float]:
    """Fieller's 95% interval for the rating where the band line meets `value`.

    The Elo is a ratio: the gap from the line's centre over its slope,
    (value - ybar) / b, plus xbar. Written about the centre, the line's level
    and slope are uncorrelated, and your value is independent of both, so
    Fieller's quadratic is exact for this setup. When the slope is not clearly
    away from zero the interval has no ends (the quadratic's leading
    coefficient is not positive), and the whole scale is returned.
    """
    ybar = fit.a + fit.b * fit.xm
    u = value - ybar
    var_u = var_value + fit.s2 / fit.sw
    var_b = fit.s2 / fit.sxx
    lead = fit.b ** 2 - Z95 ** 2 * var_b
    disc = var_u * fit.b ** 2 + var_b * u ** 2 - Z95 ** 2 * var_u * var_b
    if lead <= 0 or disc < 0:
        return float(RATING_MIN), float(RATING_MAX)
    root = Z95 * math.sqrt(disc)
    lo = fit.xm + (u * fit.b - root) / lead
    hi = fit.xm + (u * fit.b + root) / lead

    def clamp(v: float) -> float:
        return float(max(RATING_MIN, min(RATING_MAX, v)))

    return clamp(lo), clamp(hi)


def band_score(value: float, band: float) -> float:
    """0 at a rate of 0, 50 at the band's rate, 100 at a rate of 1, straight
    lines between."""
    band = min(max(band, 1e-9), 1 - 1e-9)
    score = 50 * value / band if value <= band else 50 + 50 * (value - band) / (1 - band)
    return max(0.0, min(100.0, score))


def compare_to_band(sides: Sequence[SideFacts], fits: dict[str, Optional[Fit]],
                    rating: float) -> list[dict]:
    """One row per dimension: the player's value against the band's at their rating.

    Everything here is closed-form, so the same games always give the same
    numbers. The player's value is a ratio of sums over their games, in the
    unit the band line was fitted on; the difference's range combines its
    variance with the line's standard error at the rating; the Elo's range is
    Fieller's interval.
    """
    rows = []
    for dim in DIMENSIONS:
        counts = np.array([calibration_counts(dim.key, s) for s in sides]).reshape(len(sides), 2)
        den = counts[:, 1].sum()
        fit = fits.get(dim.key)
        row: dict = {"key": dim.key, "label": dim.label, "unit": dim.unit,
                     "higher_is_better": dim.higher_is_better, "n": float(den),
                     "you": None, "band": None, "diff": None, "lo": None, "hi": None,
                     "verdict": None, "elo": None, "elo_lo": None, "elo_hi": None,
                     "has_elo": dim.has_elo, "score": None,
                     "band_games": fit.n if fit else None}
        if dim.key == "time":
            moves = [sum(getattr(s, f"clock_{c}") for s in sides)
                     for c in ("ahead", "even", "behind")]
            total = sum(moves)
            row["breakdown"] = ({c: n / total for c, n in zip(("ahead", "even", "behind"), moves, strict=True)}
                                if total else None)
        if len(sides) and den > 0:
            you, var_you = ratio_and_variance(counts)
            row["you"] = you
            if dim.score == "share":
                row["score"] = 100 * you
            if fit is not None and dim.has_elo:
                elo = fit.elo_for(you)
                row["elo"] = round(elo) if elo is not None else None
                elo_lo, elo_hi = elo_range(you, var_you, fit)
                row["elo_lo"], row["elo_hi"] = round(elo_lo), round(elo_hi)
            if fit is not None:
                band = fit.at(rating)
                se = math.sqrt(var_you + fit.se_at(rating) ** 2)
                diff = you - band
                lo, hi = diff - Z95 * se, diff + Z95 * se
                row.update(band=float(band), diff=float(diff), lo=float(lo), hi=float(hi),
                           verdict="real" if (lo > 0 or hi < 0) else "noise")
                if dim.score == "vs_band":
                    row["score"] = band_score(you, band)
        rows.append(row)
    return rows
