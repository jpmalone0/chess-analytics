"""The scorecard's pure core: phases, per-side facts, rows and the rating scale."""

import chess
import pytest

from engine.scorecard import (
    DIMENSIONS,
    Division,
    GameInput,
    SideFacts,
    calibration_counts,
    compare_to_band,
    divide_boards,
    fit_band,
    game_sides,
    unit_counts,
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


def line(slope=0.001, noise=0.05, n=200):
    xs = [600 + 200 * (i % 8) for i in range(n)]
    ys = [slope * x + (noise if (i // 8) % 2 else -noise) for i, x in enumerate(xs)]
    return xs, ys, [1] * n


class TestBandLine:
    def test_recovers_slope(self):
        fit = fit_band(*line())
        assert fit is not None
        assert fit.b == pytest.approx(0.001, rel=0.05)
        assert fit.at(1500) == pytest.approx(1.5, abs=0.01)

    def test_elo_is_the_rating_that_plays_that_way(self):
        fit = fit_band(*line())
        assert fit.elo_for(1.5) == pytest.approx(1500, abs=50)

    def test_a_clear_slope_is_trusted(self):
        assert fit_band(*line()).trusted(direction=1)

    def test_wrong_direction_still_gives_an_elo_but_not_a_trusted_one(self):
        fit = fit_band(*line())
        assert fit.elo_for(1.5) == pytest.approx(1500, abs=50)
        assert not fit.trusted(direction=-1)

    def test_a_weak_slope_is_not_trusted(self):
        fit = fit_band(*line(slope=0.00002, noise=0.5))
        assert fit is not None and not fit.trusted(direction=1)

    def test_a_flat_line_still_gives_the_band_value(self):
        """Comparing to the band needs only the line's level at your rating;
        a line with no slope at all cannot be read backwards."""
        fit = fit_band(*line(slope=0.0))
        assert fit is not None
        assert fit.at(1900) == pytest.approx(0.0, abs=0.01)
        assert fit.elo_for(0.0) is None

    def test_too_few_bands_is_no_line(self):
        xs = [1800 + (i % 2) for i in range(200)]
        assert fit_band(xs, [0.0] * 200, [1] * 200) is None

    def test_too_few_observations_is_no_line(self):
        assert fit_band(*line(n=20)) is None


class TestCompareToBand:
    @staticmethod
    def side(blunders, moves=10):
        return SideFacts(blunders=blunders, opening_moves=moves)

    @staticmethod
    def flat_band(value):
        xs = [600 + 200 * (i % 8) for i in range(200)]
        return fit_band(xs, [value + (0.01 if (i // 8) % 2 else -0.01) for i in range(200)], [10] * 200)

    def rows(self, sides, band):
        fits = {d.key: band for d in DIMENSIONS}
        return {r["key"]: r for r in compare_to_band(sides, fits, 1900)}

    def test_matching_the_band_is_noise(self):
        row = self.rows([self.side(1 + i % 2) for i in range(60)], self.flat_band(0.15))["blunders"]
        assert row["band"] == pytest.approx(0.15, abs=0.001)
        assert row["verdict"] == "noise"

    def test_the_row_says_whether_its_elo_is_trusted(self):
        row = self.rows([self.side(1)] * 40, fit_band(*line(slope=-0.00001, noise=0.01)))["blunders"]
        assert row["elo"] is not None
        assert row["elo_trusted"] is True

    def test_a_consistent_gap_is_real(self):
        row = self.rows([self.side(3) for _ in range(60)], self.flat_band(0.1))["blunders"]
        assert row["diff"] == pytest.approx(0.2, abs=0.001)
        assert row["lo"] > 0 and row["verdict"] == "real"

    def test_same_input_same_range(self):
        sides = [self.side(i % 3) for i in range(40)]
        a = self.rows(sides, self.flat_band(0.1))["blunders"]
        b = self.rows(sides, self.flat_band(0.1))["blunders"]
        assert (a["lo"], a["hi"]) == (b["lo"], b["hi"])

    def test_no_band_line_means_no_comparison(self):
        row = {r["key"]: r for r in compare_to_band([self.side(1)] * 40, {}, 1900)}["blunders"]
        assert row["you"] == pytest.approx(0.1)
        assert row["band"] is None and row["verdict"] is None

    def test_rate_with_no_chances_is_none(self):
        row = self.rows([self.side(0)] * 5, self.flat_band(0.5))["advantage"]
        assert row["you"] is None and row["verdict"] is None


class TestCalibrationUnits:
    """Rating lines are fitted per move, not per game: low-rated games end early,
    so a per-game total rewards a game for being short."""

    def test_moves_are_counted_per_phase(self):
        sides = game_sides(game(["e4", "e5", "Nf3"], flat(3)), K, division=Division(2, None))
        assert (sides["white"].opening_moves, sides["white"].middlegame_moves) == (1, 1)
        assert sides["black"].opening_moves == 1

    def test_phase_rows_calibrate_per_move(self):
        s = SideFacts(middlegame=-0.3, middlegame_moves=30)
        assert calibration_counts("middlegame", s) == (-0.3, 30)

    def test_blunders_calibrate_per_move(self):
        s = SideFacts(blunders=2, opening_moves=10, middlegame_moves=20, endgame_moves=10)
        assert calibration_counts("blunders", s) == (2, 40)

    def test_rates_calibrate_as_they_are_shown(self):
        s = SideFacts(found=3, chances=4)
        assert calibration_counts("tactics", s) == unit_counts("tactics", s)

    def test_weights_do_not_inflate_confidence(self):
        """A side-game with 40 moves is one observation, not 40."""
        xs = [600 + 200 * (i % 8) for i in range(60)]
        ys = [0.001 * x + (0.3 if i % 3 else -0.6) for i, x in enumerate(xs)]
        light = fit_band(xs, ys, [1] * 60)
        heavy = fit_band(xs, ys, [40] * 60)
        assert light is not None and heavy is not None
        assert heavy.t == pytest.approx(light.t)
