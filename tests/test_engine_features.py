"""Describing moves, and comparing what was played against what was wanted.

cp_loss says how much a move cost. These features say what kind of move it was,
which is the difference between walking past a winning capture and drifting in a
quiet position — two errors that want different practice.
"""

import chess
import pytest
from sqlalchemy import create_engine, text

from engine.features import Summary, _extract_game, classify
from engine.views import MOVE_ERRORS_VIEW, MOVE_EVALS_VIEW


class TestClassify:
    def test_a_capture_is_a_capture(self):
        board = chess.Board()
        board.push_san("e4")
        board.push_san("d5")
        f = classify(board, board.parse_san("exd5"))
        assert f["is_capture"] == 1 and f["gives_check"] == 0
        assert f["piece"] == "P"

    def test_a_check_is_a_check(self):
        board = chess.Board()
        for san in ("e4", "e5", "Nf3", "Nc6", "Bc4", "d6"):
            board.push_san(san)
        f = classify(board, board.parse_san("Bxf7+"))
        assert f["gives_check"] == 1 and f["is_capture"] == 1

    def test_castling_is_flagged_and_is_not_a_king_capture(self):
        board = chess.Board()
        for san in ("e4", "e5", "Nf3", "Nc6", "Bc4", "Bc5"):
            board.push_san(san)
        f = classify(board, board.parse_san("O-O"))
        assert f["is_castling"] == 1 and f["is_capture"] == 0
        assert f["piece"] == "K"

    def test_promotion_is_flagged(self):
        board = chess.Board("8/P6k/8/8/8/8/8/K7 w - - 0 1")
        f = classify(board, board.parse_san("a8=Q"))
        assert f["is_promotion"] == 1 and f["piece"] == "P"

    def test_en_passant_counts_as_a_capture(self):
        """python-chess reports it as a capture even though the target square is
        empty; anything hand-rolled from piece_at() would miss it."""
        board = chess.Board("4k3/8/8/8/4pP2/8/8/4K3 b - f3 0 1")
        f = classify(board, board.parse_san("exf3"))
        assert f["is_capture"] == 1

    def test_an_illegal_move_classifies_as_nothing(self):
        """An engine recommendation that does not fit the position is dropped,
        not guessed at."""
        board = chess.Board()
        assert classify(board, chess.Move.from_uci("e2e5")) is None


class TestExtraction:
    def test_the_best_move_is_matched_to_the_move_it_would_have_replaced(self):
        """position_evals stores the recommendation for the position *after* p
        plies, so the entry at p-1 is what should have been played at p. Off by
        one here silently compares every move to the wrong alternative."""
        s = Summary()
        # After 1.e4, the engine's pick for Black's first move is c7c5.
        played, best = _extract_game(1, ["e4", "e5"], {0: "e2e4", 1: "c7c5"}, s)

        assert [r["ply"] for r in played] == [1, 2]
        assert [r["ply"] for r in best] == [1, 2]
        # The ply-2 row describes c7c5, a pawn move, not Black's played e7e5.
        assert best[1]["piece"] == "P"

    def test_a_missing_recommendation_yields_no_row(self, ):
        """Not an 'other': an unknown recommendation is not evidence of a
        non-forcing one, and bucketing it would inflate whichever bucket it
        landed in."""
        s = Summary()
        played, best = _extract_game(1, ["e4", "e5"], {0: "e2e4"}, s)

        assert len(played) == 2
        assert [r["ply"] for r in best] == [1]
        assert s.skipped_best == 1

    def test_an_unparseable_recommendation_is_skipped(self):
        s = Summary()
        _, best = _extract_game(1, ["e4"], {0: "not-a-move"}, s)
        assert best == [] and s.skipped_best == 1

    def test_an_illegal_recommendation_is_skipped(self):
        s = Summary()
        _, best = _extract_game(1, ["e4"], {0: "e2e5"}, s)
        assert best == [] and s.skipped_best == 1

    def test_a_game_that_stops_replaying_keeps_what_came_before(self):
        s = Summary()
        played, _ = _extract_game(1, ["e4", "e5", "Qxh8"], {}, s)

        assert [r["ply"] for r in played] == [1, 2]
        assert s.unreplayable == 1


@pytest.fixture
def errors():
    """An in-memory database with both views over hand-seeded rows."""
    eng = create_engine("sqlite://")
    with eng.begin() as conn:
        conn.execute(text("""
            CREATE TABLE position_evals (
                run_id INTEGER, game_id INTEGER, ply INTEGER,
                cp INTEGER, mate_in INTEGER, best_move_uci VARCHAR(6),
                PRIMARY KEY (run_id, game_id, ply))"""))
        for t in ("played_move_features", "best_move_features"):
            key = "run_id, game_id, ply" if t == "best_move_features" else "game_id, ply"
            extra = "run_id INTEGER, " if t == "best_move_features" else ""
            conn.execute(text(f"""
                CREATE TABLE {t} (
                    {extra}game_id INTEGER, ply INTEGER, piece VARCHAR(1),
                    is_capture INTEGER, gives_check INTEGER,
                    is_castling INTEGER, is_promotion INTEGER,
                    PRIMARY KEY ({key}))"""))
        conn.execute(text(MOVE_EVALS_VIEW))
        conn.execute(text(MOVE_ERRORS_VIEW))
    return eng


def seed(eng, played_forcing, best_forcing, cp_before=0, cp_after=-400):
    """One scored move at ply 1, with both moves' forcing-ness set."""
    with eng.begin() as conn:
        conn.execute(text("INSERT INTO position_evals VALUES (1,1,0,:a,NULL,NULL)"),
                     {"a": cp_before})
        conn.execute(text("INSERT INTO position_evals VALUES (1,1,1,:b,NULL,NULL)"),
                     {"b": cp_after})
        conn.execute(text(
            "INSERT INTO played_move_features VALUES (1,1,'N',:c,0,0,0)"),
            {"c": int(played_forcing)})
        conn.execute(text(
            "INSERT INTO best_move_features VALUES (1,1,1,'Q',:c,0,0,0)"),
            {"c": int(best_forcing)})


def kind(eng):
    with eng.connect() as conn:
        return conn.execute(text("SELECT error_kind FROM move_errors")).scalar()


class TestErrorKinds:
    def test_quiet_when_forcing_was_wanted(self, errors):
        seed(errors, played_forcing=False, best_forcing=True)
        assert kind(errors) == "missed_forcing"

    def test_forcing_when_quiet_was_wanted(self, errors):
        seed(errors, played_forcing=True, best_forcing=False)
        assert kind(errors) == "forced_when_quiet_better"

    def test_both_forcing_is_not_a_missed_tactic(self, errors):
        """Playing the wrong capture is an error, but not this error — the
        player did look for a forcing move."""
        seed(errors, played_forcing=True, best_forcing=True)
        assert kind(errors) == "other"

    def test_neither_forcing_is_ordinary_drift(self, errors):
        seed(errors, played_forcing=False, best_forcing=False)
        assert kind(errors) == "other"


class TestNoThreshold:
    def test_a_trivial_loss_still_appears(self, errors):
        """The view carries every scored move and its cp_loss. Deciding what
        counts as an error is the caller's, so it can be revised without a
        rebuild."""
        seed(errors, False, True, cp_before=0, cp_after=-1)
        with errors.connect() as conn:
            r = conn.execute(text("SELECT cp_loss, error_kind FROM move_errors")).mappings().first()
        assert r["cp_loss"] == 1 and r["error_kind"] == "missed_forcing"

    def test_a_move_with_no_recommendation_is_absent_entirely(self, errors):
        """No best_move_features row means no row here — not an 'other'."""
        with errors.begin() as conn:
            conn.execute(text("INSERT INTO position_evals VALUES (1,2,0,0,NULL,NULL)"))
            conn.execute(text("INSERT INTO position_evals VALUES (1,2,1,-400,NULL,NULL)"))
            conn.execute(text("INSERT INTO played_move_features VALUES (2,1,'N',0,0,0,0)"))
        with errors.connect() as conn:
            n = conn.execute(text("SELECT COUNT(*) FROM move_errors WHERE game_id=2")).scalar()
        assert n == 0


class TestEvaluationContext:
    def test_the_view_carries_the_evaluations_the_loss_came_from(self, errors):
        """"Was this winnable at the time?" is the first question anyone asks of
        a missed tactic. A 400 cp slip from +300 is a thrown-away win; the same
        slip from -900 is noise in a game already lost. Without cp_before the
        view cannot tell them apart and every caller re-joins move_evals."""
        seed(errors, played_forcing=False, best_forcing=True,
             cp_before=300, cp_after=-100)
        with errors.connect() as conn:
            r = conn.execute(text(
                "SELECT cp_before, cp_after, cp_loss FROM move_errors")).mappings().first()
        assert (r["cp_before"], r["cp_after"], r["cp_loss"]) == (300, -100, 400)

    def test_evaluations_stay_unclamped_here_too(self, errors):
        """Same reason as in move_evals: 'already lost' is exactly the context
        that makes a small loss unimportant."""
        seed(errors, False, True, cp_before=4000, cp_after=-4000)
        with errors.connect() as conn:
            r = conn.execute(text(
                "SELECT cp_before, cp_after FROM move_errors")).mappings().first()
        assert (r["cp_before"], r["cp_after"]) == (4000, -4000)
