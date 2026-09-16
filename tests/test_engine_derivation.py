"""Centipawn loss is derived from stored evaluations, never stored itself.

The view is where every interpretation lives — whose point of view a loss is
measured from, what a mate is worth, what happens when search noise makes a move
look better than the position it came from. Getting it wrong is silent: the
numbers still aggregate, they just describe the wrong thing.
"""

import pytest
from sqlalchemy import create_engine, text

from engine.models import EVAL_CLAMP_CP, MATE_CP, MOVE_EVALS_VIEW


@pytest.fixture
def evals():
    """An in-memory database holding position_evals and the derivation view.

    Built without SQLAlchemy models on purpose: this exercises the view's SQL as
    the database will actually run it.
    """
    eng = create_engine("sqlite://")
    with eng.begin() as conn:
        conn.execute(text("""
            CREATE TABLE position_evals (
                run_id        INTEGER NOT NULL,
                game_id       INTEGER NOT NULL,
                ply           INTEGER NOT NULL,
                cp            INTEGER,
                mate_in       INTEGER,
                best_move_uci VARCHAR(6),
                PRIMARY KEY (run_id, game_id, ply)
            )
        """))
        conn.execute(text(MOVE_EVALS_VIEW))
    return eng


def seed(eng, positions, game_id=1, run_id=1):
    """positions: [(ply, cp, mate_in)] — evaluations from White's point of view."""
    with eng.begin() as conn:
        for ply, cp, mate_in in positions:
            conn.execute(
                text(
                    "INSERT INTO position_evals (run_id, game_id, ply, cp, mate_in) "
                    "VALUES (:r, :g, :p, :cp, :m)"
                ),
                {"r": run_id, "g": game_id, "p": ply, "cp": cp, "m": mate_in},
            )


def moves(eng, game_id=1):
    with eng.connect() as conn:
        return {
            row["ply"]: row
            for row in conn.execute(
                text("SELECT * FROM move_evals WHERE game_id = :g ORDER BY ply"),
                {"g": game_id},
            ).mappings()
        }


class TestPointOfView:
    """Evaluations are stored from White's side; loss belongs to whoever moved."""

    def test_white_loss_is_a_fall_in_the_stored_evaluation(self, evals):
        seed(evals, [(0, 100, None), (1, -300, None)])
        m = moves(evals)[1]
        assert m["color"] == "white"
        assert m["cp_loss"] == 400

    def test_black_loss_is_a_rise_in_the_stored_evaluation(self, evals):
        """Black wants the White-POV number to go down, so the sign inverts.

        Sharing one sign convention across both colours is the easiest way to
        end up reporting that a player's worst moves are their best ones.
        """
        seed(evals, [(1, -300, None), (2, 200, None)])
        m = moves(evals)[2]
        assert m["color"] == "black"
        assert m["cp_loss"] == 500

    def test_colour_follows_ply_parity(self, evals):
        seed(evals, [(0, 0, None), (1, 0, None), (2, 0, None), (3, 0, None)])
        got = {ply: row["color"] for ply, row in moves(evals).items()}
        assert got == {1: "white", 2: "black", 3: "white"}


class TestNoiseFloor:
    def test_an_apparent_gain_is_not_a_negative_loss(self, evals):
        """At fixed depth a move can 'improve' the evaluation it inherited.

        That is search noise, not a gain. Left negative it would offset real
        errors inside any average and quietly flatter the player.
        """
        seed(evals, [(0, 100, None), (1, 150, None)])
        assert moves(evals)[1]["cp_loss"] == 0

    def test_a_flat_evaluation_is_zero_loss(self, evals):
        seed(evals, [(0, 30, None), (1, 30, None)])
        assert moves(evals)[1]["cp_loss"] == 0


class TestMateScores:
    def test_white_mating_in_three_is_worth_nearly_a_win(self, evals):
        seed(evals, [(0, 0, None), (1, None, 3)])
        assert moves(evals)[1]["cp_after"] == MATE_CP - 300

    def test_black_mating_in_two_is_signed_negative(self, evals):
        seed(evals, [(1, 0, None), (2, None, -2)])
        assert moves(evals)[2]["cp_after"] == -MATE_CP + 200

    def test_a_nearer_mate_outranks_a_further_one(self, evals):
        """Otherwise every forced mate is one number and mate in 1 looks no
        better than mate in 9, which makes conversion errors invisible."""
        seed(evals, [(0, 0, None), (1, None, 1), (2, None, 9)])
        m = moves(evals)
        assert m[1]["cp_after"] > m[2]["cp_after"]

    def test_delivered_mate_takes_its_sign_from_ply_parity(self, evals):
        """mate_in = 0 means the side to move is mated, and after an odd ply
        that is Black. Storing an unsigned zero keeps a sentinel out of ground
        truth; the sign is recoverable because ply parity says who is to move."""
        seed(evals, [(0, 0, None), (1, None, 0)])
        assert moves(evals)[1]["cp_after"] == MATE_CP

    def test_being_mated_is_the_worst_available_loss(self, evals):
        """White walks into mate: after ply 2 it is White to move and mated."""
        seed(evals, [(1, 0, None), (2, None, 0)])
        m = moves(evals)[2]
        assert m["cp_after"] == -MATE_CP
        assert m["color"] == "black"


class TestPairing:
    def test_n_positions_yield_n_minus_one_moves(self, evals):
        """A game of N plies is stored as N+1 positions, so scoring every move
        means every position except the first has a predecessor."""
        seed(evals, [(p, 0, None) for p in range(6)])
        assert sorted(moves(evals)) == [1, 2, 3, 4, 5]

    def test_a_gap_breaks_the_pair_rather_than_spanning_it(self, evals):
        """A partial game must not have its missing plies silently bridged —
        that would invent a single huge loss where evaluation simply stopped."""
        seed(evals, [(0, 0, None), (1, 0, None), (5, -900, None)])
        assert sorted(moves(evals)) == [1]

    def test_runs_do_not_pair_across_each_other(self, evals):
        """Depth 14 and depth 20 evaluations are not comparable, so a move's
        before and after must come from the same run."""
        seed(evals, [(0, 100, None)], run_id=1)
        seed(evals, [(1, -300, None)], run_id=2)
        assert moves(evals) == {}


class TestEvaluationWindow:
    """Loss is measured inside a clamped window.

    Once a game is decided the evaluation swings by thousands of centipawns, and
    every move played afterwards books a loss that describes the position rather
    than the player. Across 400 real bullet games an open window put average loss
    at 208 cp against 76 clamped — roughly double, concentrated entirely in
    positions whose result was no longer in doubt.
    """

    def test_a_move_in_a_decided_position_cannot_book_a_huge_loss(self, evals):
        seed(evals, [(0, 4000, None), (1, -4000, None)])
        # Both ends clamp to the window, so the loss is its full width, not 8000.
        assert moves(evals)[1]["cp_loss"] == 2 * EVAL_CLAMP_CP

    def test_losing_a_won_game_still_registers(self, evals):
        """Clamping must not make blunders in winning positions invisible."""
        seed(evals, [(0, 900, None), (1, -200, None)])
        assert moves(evals)[1]["cp_loss"] == 1100

    def test_an_ordinary_loss_is_untouched_by_the_clamp(self, evals):
        seed(evals, [(0, 100, None), (1, -300, None)])
        assert moves(evals)[1]["cp_loss"] == 400

    def test_a_move_between_two_lost_positions_books_nothing(self, evals):
        """Shuffling in a dead-lost position is not an error worth counting."""
        seed(evals, [(0, -3000, None), (1, -5000, None)])
        assert moves(evals)[1]["cp_loss"] == 0

    def test_the_reported_evaluations_stay_unclamped(self, evals):
        """'This was already lost' is the context that makes a small loss
        unimportant — the clamp must not erase it from cp_before/cp_after."""
        seed(evals, [(0, 4000, None), (1, -4000, None)])
        m = moves(evals)[1]
        assert m["cp_before"] == 4000
        assert m["cp_after"] == -4000
