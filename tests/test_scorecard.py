"""The scorecard's pure core: phases, per-side facts, rows and the rating scale."""

import chess
import pytest

from engine.scorecard import (
    Division,
    GameInput,
    divide_boards,
    fit_line,
    game_sides,
    rating_score,
    summarize,
)

K = 360.0


def game(sans, evals, *, pvs=None, result="1-0", termination="x won by resignation"):
    return GameInput(game_id=1, sans=sans, evals=evals, pvs=pvs or {},
                     result=result, termination=termination)


def flat(n, cp=0):
    return [(cp, None)] * (n + 1)


class TestDivider:
    def test_start_position_is_opening(self):
        assert divide_boards([chess.Board()]) == Division(None, None)

    def test_middlegame_when_pieces_come_off(self):
        few = chess.Board("r3k2r/pppq1ppp/2n5/8/8/2N5/PPPQ1PPP/R3K2R w KQkq - 0 1")
        assert divide_boards([chess.Board(), few]) == Division(1, None)

    def test_endgame_when_six_or_fewer_pieces(self):
        ending = chess.Board("r3k2r/ppp2ppp/8/8/8/8/PPP2PPP/R3K2R w KQkq - 0 1")
        assert divide_boards([chess.Board(), ending]) == Division(None, 1)


class TestPhaseSums:
    def test_signed_changes_go_to_the_mover(self):
        # White gains 100cp with e4; Black gives up another 60 with e5.
        sides = game_sides(game(["e4", "e5"], [(0, None), (100, None), (160, None)]),
                           K, division=Division(None, None))
        assert sides["white"].opening > 0
        assert sides["black"].opening < 0
        assert sides["white"].middlegame == 0 and sides["black"].endgame == 0

    def test_phase_of_the_position_moved_from(self):
        sides = game_sides(game(["e4", "e5"], [(0, None), (-100, None), (-100, None)]),
                           K, division=Division(1, None))
        assert sides["white"].opening < 0
        assert sides["black"].middlegame == 0


class TestFlag:
    def test_loser_on_time_loses_their_expected_score(self):
        g = game(["e4", "e5"], flat(2, 1000), result="0-1",
                 termination="x won on time")
        sides = game_sides(g, K, division=Division(None, None))
        # White flagged while +1000: the whole expected score is lost.
        assert sides["white"].flag_loss == pytest.approx(1 / (1 + 2.718281828 ** (-1000 / K)))
        assert sides["black"].flag_loss == 0

    def test_timeout_vs_insufficient_counts_the_part_above_half(self):
        # Two plies played, so White was to move when the flag fell.
        g = game(["e4", "e5"], flat(2, 1000), result="1/2-1/2",
                 termination="Game drawn by timeout vs insufficient material")
        sides = game_sides(g, K, division=Division(None, None))
        wp = 1 / (1 + 2.718281828 ** (-1000 / K))
        assert sides["white"].flag_loss == pytest.approx(wp - 0.5)
        assert sides["black"].flag_loss == 0


class TestSwings:
    def test_reaching_75_after_the_opening(self):
        g = game(["e4", "e5"], [(0, None), (500, None), (500, None)], result="1-0")
        sides = game_sides(g, K, division=Division(1, None))
        assert sides["white"].reached and sides["white"].won
        assert sides["black"].fell and not sides["black"].saved

    def test_swings_in_the_opening_do_not_count(self):
        g = game(["e4", "e5"], [(0, None), (500, None), (0, None)])
        sides = game_sides(g, K, division=Division(None, None))
        assert not sides["white"].reached and not sides["black"].fell


class TestTactics:
    SANS = ["e4", "d5", "exd5"]
    CHANCE = {2: [("e4d5", 400, None), ("b1c3", 0, None)]}

    def test_found(self):
        g = game(self.SANS, [(0, None), (0, None), (400, None), (400, None)], pvs=self.CHANCE)
        white = game_sides(g, K, division=Division(0, None))["white"]
        assert (white.chances, white.found, white.blunders) == (1, 1, 0)

    def test_missed_is_not_also_a_blunder(self):
        g = game(["e4", "d5", "Nc3"], [(0, None), (0, None), (400, None), (0, None)],
                 pvs=self.CHANCE)
        white = game_sides(g, K, division=Division(0, None))["white"]
        assert (white.chances, white.found, white.blunders) == (1, 0, 0)

    def test_small_gap_is_not_a_chance(self):
        pvs = {2: [("e4d5", 100, None), ("b1c3", 60, None)]}
        g = game(self.SANS, [(0, None), (0, None), (100, None), (100, None)], pvs=pvs)
        assert game_sides(g, K, division=Division(0, None))["white"].chances == 0

    def test_not_in_the_opening(self):
        g = game(self.SANS, [(0, None), (0, None), (400, None), (400, None)], pvs=self.CHANCE)
        assert game_sides(g, K, division=Division(None, None))["white"].chances == 0

    def test_plain_recapture_is_not_a_chance(self):
        sans = ["e4", "d5", "exd5", "Nf6", "c4", "Nxd5", "cxd5"]
        pvs = {6: [("c4d5", 400, None), ("b1c3", 0, None)]}
        evals = [(0, None)] * 6 + [(400, None), (400, None)]
        white = game_sides(game(sans, evals, pvs=pvs), K, division=Division(0, None))["white"]
        assert white.chances == 0

    def test_blunder_from_a_quiet_position(self):
        pvs = {2: [("b1c3", 0, None), ("g1f3", -10, None)]}
        g = game(self.SANS, [(0, None), (0, None), (0, None), (-400, None)], pvs=pvs)
        white = game_sides(g, K, division=Division(0, None))["white"]
        assert (white.chances, white.blunders) == (0, 1)


class TestSummarize:
    @staticmethod
    def sides(opening, blunders):
        return game_sides(
            game(["e4"], [(0, None), (opening, None)]), K, division=Division(None, None)
        )["white"]._replace(blunders=blunders)

    def test_identical_sides_are_noise(self):
        s = self.sides(0, 1)
        rows = {r["key"]: r for r in summarize([(s, s)] * 50)}
        assert rows["blunders"]["diff"] == 0
        assert rows["blunders"]["verdict"] == "noise"

    def test_consistent_gap_is_real(self):
        pairs = [(self.sides(0, 0), self.sides(0, 1 + i % 2)) for i in range(60)]
        row = {r["key"]: r for r in summarize(pairs)}["blunders"]
        assert row["diff"] == pytest.approx(-1.5)
        assert row["hi"] < 0 and row["verdict"] == "real"

    def test_same_input_same_range(self):
        pairs = [(self.sides(0, i % 3), self.sides(0, i % 2)) for i in range(40)]
        a = {r["key"]: r for r in summarize(pairs)}["blunders"]
        b = {r["key"]: r for r in summarize(pairs)}["blunders"]
        assert (a["lo"], a["hi"]) == (b["lo"], b["hi"])

    def test_rate_with_no_chances_is_none(self):
        s = self.sides(0, 0)
        row = {r["key"]: r for r in summarize([(s, s)] * 5)}["advantage"]
        assert row["you"] is None and row["verdict"] is None


class TestScale:
    def test_anchor_points(self):
        assert rating_score(500) == pytest.approx(30)
        assert rating_score(2500) == pytest.approx(80)
        assert rating_score(-5000) == 0 and rating_score(9000) == 100

    def test_line_recovers_slope(self):
        xs = [600 + 200 * (i % 8) for i in range(200)]
        ys = [0.001 * x + (0.05 if i % 2 else -0.05) for i, x in enumerate(xs)]
        fit = fit_line(xs, ys, [1] * 200, direction=1)
        assert fit is not None
        assert fit.b == pytest.approx(0.001, rel=0.05)
        assert fit.rating_for(1.5) == pytest.approx(1500, abs=50)

    def test_wrong_direction_is_no_fit(self):
        xs = [600 + 200 * (i % 8) for i in range(200)]
        ys = [0.001 * x for x in xs]
        assert fit_line(xs, ys, [1] * 200, direction=-1) is None

    def test_too_few_bands_is_no_fit(self):
        xs = [1800 + (i % 2) for i in range(200)]
        ys = [0.001 * x + i % 3 for i, x in enumerate(xs)]
        assert fit_line(xs, ys, [1] * 200, direction=1) is None
