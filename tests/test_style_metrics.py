"""The four style metrics, against positions where the answer is known by hand.

Every metric is signed so that HIGHER = MORE OF THE THING, never so that higher
is better. These axes measure style, not ability.
"""

import chess

from analysis.metrics import (
    STYLE_AXES,
    king_safety,
    mobility,
    passed_pawns,
    pawn_structure,
    space,
)


class TestSpace:
    def test_the_start_position_is_symmetric(self):
        b = chess.Board()
        assert space(b, chess.WHITE) == space(b, chess.BLACK)

    def test_advancing_in_the_centre_gains_space(self):
        b = chess.Board()
        before = space(b, chess.WHITE)
        b.push_san("e4")
        assert space(b, chess.WHITE) > before

    def test_enemy_pawn_control_removes_a_square(self):
        """A square attacked by an enemy pawn is not safe, so it does not count."""
        open_board = chess.Board("4k3/8/8/8/8/8/8/4K3 w - - 0 1")
        guarded = chess.Board("4k3/8/8/2p5/8/8/8/4K3 w - - 0 1")
        assert space(guarded, chess.WHITE) < space(open_board, chess.WHITE)


class TestMobility:
    def test_the_start_position_is_symmetric(self):
        b = chess.Board()
        assert mobility(b, chess.WHITE) == mobility(b, chess.BLACK)

    def test_a_developed_knight_has_more_scope(self):
        b = chess.Board()
        before = mobility(b, chess.WHITE)
        b.push_san("Nf3")
        assert mobility(b, chess.WHITE) > before

    def test_it_does_not_depend_on_whose_turn_it_is(self):
        """Computed from attack maps, not legal moves, so both colours are
        measurable in the same position. A legal-move implementation would
        silently return 0 for the side not to move."""
        w = chess.Board("rnbqkbnr/pppppppp/8/8/8/5N2/PPPPPPPP/RNBQKB1R b KQkq - 1 1")
        assert mobility(w, chess.WHITE) > 0
        assert mobility(w, chess.BLACK) > 0

    def test_enemy_pawn_control_removes_a_target_square(self):
        """A white knight on d4 reaches c6 and e6. A black pawn on d7 attacks
        both of those squares, so they drop out of the mobility area even
        though the pawn itself sits on neither -- this is the exclusion that
        every other mobility assertion here is too directional to catch."""
        open_board = chess.Board("4k3/8/8/8/3N4/8/8/4K3 w - - 0 1")
        covered = chess.Board("4k3/3p4/8/8/3N4/8/8/4K3 w - - 0 1")
        assert mobility(covered, chess.WHITE) < mobility(open_board, chess.WHITE)


class TestKingSafety:
    def test_an_intact_shield_beats_a_stripped_one(self):
        sheltered = chess.Board("4k3/8/8/8/8/8/5PPP/6K1 w - - 0 1")
        exposed = chess.Board("4k3/8/8/8/8/8/8/6K1 w - - 0 1")
        assert king_safety(sheltered, chess.WHITE) > king_safety(exposed, chess.WHITE)

    def test_an_attacker_on_the_ring_lowers_it(self):
        quiet = chess.Board("4k3/8/8/8/8/8/5PPP/6K1 w - - 0 1")
        attacked = chess.Board("4k3/8/8/8/8/6q1/5PPP/6K1 w - - 0 1")
        assert king_safety(attacked, chess.WHITE) < king_safety(quiet, chess.WHITE)

    def test_a_king_on_the_edge_does_not_crash(self):
        """The king ring runs off the board on the a-file; an implementation
        that assumes eight neighbours raises here."""
        b = chess.Board("4k3/8/8/8/8/8/8/K7 w - - 0 1")
        assert isinstance(king_safety(b, chess.WHITE), float)

    def test_a_missing_king_scores_zero(self):
        b = chess.Board("4k3/8/8/8/8/8/8/8 w - - 0 1")
        b.clear()
        assert king_safety(b, chess.WHITE) == 0.0


class TestPawnStructure:
    def test_a_sound_structure_scores_zero(self):
        b = chess.Board("4k3/8/8/8/8/8/PPP5/4K3 w - - 0 1")
        assert pawn_structure(b, chess.WHITE) == 0.0

    def test_doubled_pawns_cost(self):
        """a2+a3 doubled, with b2 supporting so neither file is isolated --
        otherwise the isolation penalty alone would keep this negative and the
        test would pass even with doubling detection removed."""
        b = chess.Board("4k3/8/8/8/8/P7/PP6/4K3 w - - 0 1")
        assert pawn_structure(b, chess.WHITE) == -1.0

    def test_an_isolated_pawn_costs(self):
        supported = chess.Board("4k3/8/8/8/8/8/PP6/4K3 w - - 0 1")
        lone = chess.Board("4k3/8/8/8/8/8/P7/4K3 w - - 0 1")
        assert pawn_structure(lone, chess.WHITE) < pawn_structure(supported, chess.WHITE)

    def test_pawns_two_files_apart_are_both_isolated(self):
        """a2 and c2 support neither each other nor anything else, so this
        costs twice what a single lone pawn does -- 'isolated' means no pawn on
        an IMMEDIATELY adjacent file, not merely no pawn nearby."""
        split = chess.Board("4k3/8/8/8/8/8/P1P5/4K3 w - - 0 1")
        lone = chess.Board("4k3/8/8/8/8/8/P7/4K3 w - - 0 1")
        assert pawn_structure(split, chess.WHITE) == 2 * pawn_structure(lone, chess.WHITE)

    def test_a_pawnless_side_scores_zero(self):
        b = chess.Board("4k3/8/8/8/8/8/8/4K3 w - - 0 1")
        assert pawn_structure(b, chess.WHITE) == 0.0


class TestPassedPawns:
    def test_an_unopposed_pawn_is_passed(self):
        b = chess.Board("4k3/8/8/8/8/8/P7/4K3 w - - 0 1")
        assert passed_pawns(b, chess.WHITE) == 1.0

    def test_an_enemy_pawn_on_an_adjacent_file_stops_it(self):
        b = chess.Board("4k3/1p6/8/8/8/8/P7/4K3 w - - 0 1")
        assert passed_pawns(b, chess.WHITE) == 0.0

    def test_an_enemy_pawn_on_the_same_file_ahead_stops_it(self):
        b = chess.Board("4k3/8/p7/8/8/8/P7/4K3 w - - 0 1")
        assert passed_pawns(b, chess.WHITE) == 0.0

    def test_an_enemy_pawn_on_an_adjacent_file_behind_does_not_stop_it(self):
        """b3 is on a file adjacent to a5, but it is behind, not ahead. This is
        the case that actually exercises `ahead()`'s direction -- a flipped
        `>`/`<` would wrongly block the pawn here."""
        b = chess.Board("4k3/8/8/P7/8/1p6/8/4K3 w - - 0 1")
        assert passed_pawns(b, chess.WHITE) == 1.0


def test_the_axes_exclude_passed_pawns():
    """passed_pawns measured as noise at ply 20 (split-half reliability +0.03),
    so it is deliberately not an axis. Re-including it would go unnoticed."""
    assert set(STYLE_AXES) == {"space", "mobility", "king_safety", "pawn_structure"}
