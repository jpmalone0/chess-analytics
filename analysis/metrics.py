"""Board-derived positional metrics, reimplemented from classical Stockfish.

Modern Stockfish (19, NNUE) exposes only Material/PSQT and Positional/Layers
through `eval`; the named strategic terms were removed around Stockfish 16.
These are our own implementations, following the shape of the classical
definitions so that the choices are traceable to a public source rather than to
taste.

Every function is engine-free and returns a value where HIGHER = BETTER for the
given colour. At ~500 games/sec they can be run over the whole 203k-game corpus,
which is the only reason a cross-player comparison is affordable at all.

These metrics measure STYLE, NOT ABILITY. See
docs/superpowers/specs/2026-09-16-style-vs-ability-findings.md before using them
to claim anything about a player's strength.
"""

import chess

CENTER_FILES = chess.BB_FILE_C | chess.BB_FILE_D | chess.BB_FILE_E | chess.BB_FILE_F
SPACE_W = CENTER_FILES & (chess.BB_RANK_2 | chess.BB_RANK_3 | chess.BB_RANK_4)
SPACE_B = CENTER_FILES & (chess.BB_RANK_7 | chess.BB_RANK_6 | chess.BB_RANK_5)

# Classical Stockfish weighted mobility by piece type and game phase. The phase
# term is dropped here: it is a monotone scalar on the whole position, so it
# cannot create a difference between two players in the same position.
MOBILITY_WEIGHTS = {chess.KNIGHT: 1.0, chess.BISHOP: 1.0,
                    chess.ROOK: 0.7, chess.QUEEN: 0.4}
KING_RING_WEIGHTS = {chess.KNIGHT: 2, chess.BISHOP: 2, chess.ROOK: 3, chess.QUEEN: 5}


def _pawn_attacks(board: chess.Board, color: chess.Color) -> int:
    bb = 0
    for sq in board.pieces(chess.PAWN, color):
        bb |= int(chess.BB_PAWN_ATTACKS[color][sq])
    return bb


def _adjacent_files(f: int) -> list[int]:
    return [x for x in (f - 1, f + 1) if 0 <= x <= 7]


def space(board: chess.Board, color: chess.Color) -> float:
    """Safe central squares on our own half, plus squares sheltered behind our
    pawns. Classical Stockfish's space term without the blocked-pawn weight."""
    mask = SPACE_W if color == chess.WHITE else SPACE_B
    own_pawns = int(board.pieces(chess.PAWN, color))
    safe = mask & ~own_pawns & ~_pawn_attacks(board, not color)
    behind = own_pawns
    if color == chess.WHITE:
        behind |= behind >> 8
        behind |= behind >> 16
    else:
        behind |= behind << 8
        behind |= behind << 16
    return float(bin(safe).count("1") + bin(safe & behind).count("1"))


def mobility(board: chess.Board, color: chess.Color) -> float:
    """Weighted count of squares our pieces reach, excluding our own pieces and
    squares controlled by enemy pawns (classical Stockfish's "mobility area").

    Computed from attack maps rather than legal moves so that it does not depend
    on whose turn it is -- both colours are measurable in the same position.
    """
    area = ~int(board.occupied_co[color]) & ~_pawn_attacks(board, not color)
    total = 0.0
    for piece_type, weight in MOBILITY_WEIGHTS.items():
        for sq in board.pieces(piece_type, color):
            total += weight * bin(int(board.attacks(sq)) & area).count("1")
    return total


def king_safety(board: chess.Board, color: chess.Color) -> float:
    """Pawn shield minus attacker weight on the king ring. Higher = safer.

    Signed so that higher is better, unlike classical Stockfish's "king danger",
    which runs the other way. Keeping every metric pointing the same direction
    is what lets them be averaged or charted without a per-metric sign table.
    """
    ksq = board.king(color)
    if ksq is None:
        return 0.0
    ring = int(chess.BB_KING_ATTACKS[ksq]) | int(chess.BB_SQUARES[ksq])
    danger = 0
    for piece_type, weight in KING_RING_WEIGHTS.items():
        for sq in board.pieces(piece_type, not color):
            if int(board.attacks(sq)) & ring:
                danger += weight
    kf, kr = chess.square_file(ksq), chess.square_rank(ksq)
    shield = 0
    for f in [kf] + _adjacent_files(kf):
        for d in (1, 2):
            r = kr + d if color == chess.WHITE else kr - d
            if 0 <= r <= 7 and board.piece_at(chess.square(f, r)) == chess.Piece(
                    chess.PAWN, color):
                shield += 1
                break
    return shield * 1.5 - danger


def pawn_structure(board: chess.Board, color: chess.Color) -> float:
    """Negated count of structural weaknesses: doubled and isolated pawns.

    Backward pawns are deliberately omitted. Every definition of "backward" we
    could write would be a judgement call with nothing to validate it against,
    and doubled/isolated are unambiguous.
    """
    by_file: dict[int, int] = {}
    for sq in board.pieces(chess.PAWN, color):
        by_file[chess.square_file(sq)] = by_file.get(chess.square_file(sq), 0) + 1
    weak = 0
    for f, count in by_file.items():
        weak += count - 1                                        # doubled
        if not any(a in by_file for a in _adjacent_files(f)):
            weak += count                                        # isolated
    return -float(weak)


def passed_pawns(board: chess.Board, color: chess.Color) -> float:
    """Pawns with no enemy pawn ahead on their own or adjacent files.

    MEASURED AS NOISE at ply 20: split-half reliability +0.03. Kept only so that
    the negative result is reproducible; do not put it in a profile.
    """
    theirs = board.pieces(chess.PAWN, not color)
    ahead = ((lambda r, er: er > r) if color == chess.WHITE
             else (lambda r, er: er < r))
    n = 0
    for sq in board.pieces(chess.PAWN, color):
        f, r = chess.square_file(sq), chess.square_rank(sq)
        files = {f} | set(_adjacent_files(f))
        if not any(chess.square_file(e) in files and ahead(r, chess.square_rank(e))
                   for e in theirs):
            n += 1
    return float(n)


#: Metrics that survived the reliability screen. passed_pawns is excluded.
STYLE_METRICS = {
    "space": space,
    "mobility": mobility,
    "king_safety": king_safety,
    "pawn_structure": pawn_structure,
}
