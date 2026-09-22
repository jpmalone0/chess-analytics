"""Replaying games and turning positions into stored evaluations.

The engine is a subprocess boundary, and a suite that paid 61 ms per position
would be too slow to run often enough to matter. These tests substitute a stub
that records what it was asked, which is enough to pin the parts that are ours:
how many positions a game yields, which ones are never searched, and what
happens when a stored game will not replay.
"""

import chess
import chess.engine
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.pool import StaticPool

from engine.analyze import (
    _analyze_one,
    _evaluate_game,
    _evaluate_position,
    _game_time_class,
    _init_worker,
    _worker,
)

# 1. f3 e5 2. g4 Qh4#  — the shortest mate, so the terminal position arrives
# after four plies instead of forty.
FOOLS_MATE = ["f3", "e5", "g4", "Qh4#"]


class StubEngine:
    """Returns a fixed evaluation and remembers every position it was handed."""

    def __init__(self, cp=25, mate=None, fail_on=None):
        self.cp = cp
        self.mate = mate
        self.fail_on = fail_on          # position index that raises
        self.seen: list[str] = []

    def analyse(self, board, limit):
        if self.fail_on is not None and len(self.seen) == self.fail_on:
            raise chess.engine.EngineError("stub failure")
        self.seen.append(board.fen())
        score = chess.engine.Mate(self.mate) if self.mate is not None else chess.engine.Cp(self.cp)
        return {
            "score": chess.engine.PovScore(score, board.turn),
            "pv": [next(iter(board.legal_moves))] if any(board.legal_moves) else [],
        }


@pytest.fixture
def stub():
    return StubEngine()


def rows_for(proc, sans, game_id=1, depth=1):
    return _evaluate_game(proc, game_id, sans, depth)


class TestPositionCount:
    def test_a_game_of_n_plies_yields_n_plus_one_positions(self, stub):
        """Centipawn loss compares consecutive positions, so the position before
        the first move has to be stored too — otherwise move 1 is unscorable."""
        _, rows, status, error = rows_for(stub, ["e4", "e5", "Nf3", "Nc6"])

        assert status == "complete" and error is None
        assert [ply for _, ply, *_ in rows] == [0, 1, 2, 3, 4]

    def test_the_starting_position_is_stored_at_ply_zero(self, stub):
        _, rows, _, _ = rows_for(stub, ["e4"])

        assert stub.seen[0] == chess.STARTING_FEN
        assert rows[0][1] == 0


class TestPointOfView:
    def test_evaluations_are_stored_from_whites_side_whoever_moves(self, stub):
        """The stub always reports +25 for the side to move. Stored unchanged,
        that would read as White being better on every ply of the game."""
        _, rows, _, _ = rows_for(stub, ["e4", "e5"])
        cps = {ply: cp for _, ply, cp, _, _ in rows}

        assert cps[0] == 25    # White to move, +25 for White
        assert cps[1] == -25   # Black to move, +25 for Black is -25 for White
        assert cps[2] == 25


class TestTerminalPositions:
    def test_a_finished_game_is_recorded_not_searched(self, stub):
        """Asking an engine to evaluate a checkmate is meaningless, and some
        builds refuse outright."""
        _, rows, status, _ = rows_for(stub, FOOLS_MATE)

        assert status == "complete"
        assert len(rows) == 5          # plies 0-4
        assert len(stub.seen) == 4     # the mated position was never searched

    def test_delivered_mate_stores_an_unsigned_zero(self, stub):
        """The sign is recoverable from ply parity, so ground truth holds no
        signed sentinel."""
        _, rows, _, _ = rows_for(stub, FOOLS_MATE)
        _, ply, cp, mate_in, _ = rows[-1]

        assert (ply, cp, mate_in) == (4, None, 0)

    def test_a_drawn_finish_is_a_real_zero(self):
        """Stalemate is not 'unknown' and not a mate — it is nil."""
        board = chess.Board("7k/5Q2/6K1/8/8/8/8/8 b - - 0 1")
        assert board.is_stalemate()
        assert _evaluate_position(None, board, depth=1) == (0, None, None)


class TestFailureIsolation:
    def test_a_game_that_will_not_replay_is_recorded_and_stepped_over(self, stub):
        """One corrupt game must not cost the other two thousand in the batch."""
        game_id, rows, status, error = rows_for(stub, ["e4", "e5", "Qxh8"])  # illegal

        assert game_id == 1
        assert status == "partial"
        assert "replay failed at ply 3" in error
        assert len(rows) == 3          # whatever did land is kept

    def test_an_engine_error_keeps_the_positions_already_evaluated(self):
        """Partial coverage is worth more than none, and the game is retried on
        the next run because only 'complete' counts as done."""
        _, rows, status, error = rows_for(
            StubEngine(fail_on=2), ["e4", "e5", "Nf3", "Nc6"]
        )

        assert status == "partial"
        assert "engine error" in error
        assert len(rows) == 2


class TestWorkerLifecycle:
    """A worker must not hold an engine open across games.

    python-chess runs each engine's event loop on a *non-daemon* thread, and
    CPython joins non-daemon threads before it runs atexit handlers. A worker
    that owns an open engine therefore has no point at which it can close one:
    the batch analyzes every game, writes every row, and then hangs forever on
    shutdown with the work already done — which reads as a slow run, not a
    deadlock. Opening per game also stops a reused transposition table making a
    position's evaluation depend on batch ordering.
    """

    @pytest.fixture
    def canonical(self):
        """An in-memory canonical database installed as the worker's.

        StaticPool keeps every connect() on the same in-memory database; without
        it the analyzer opens a fresh empty one and finds no moves.
        """
        eng = create_engine(
            "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
        )
        with eng.begin() as conn:
            conn.execute(text(
                "CREATE TABLE moves (game_id INTEGER, ply INTEGER, move_san VARCHAR(10))"
            ))
            conn.execute(text(
                "CREATE TABLE games (game_id INTEGER PRIMARY KEY, time_class VARCHAR(20))"
            ))
        _worker.clear()
        _worker["db"] = eng
        yield eng
        _worker.clear()

    def test_the_initializer_opens_no_engine(self):
        """The regression itself: an engine parked on the worker is the bug."""
        _worker.clear()
        try:
            _init_worker("stockfish", hash_mb=16, threads=1)
            assert "engine" not in _worker
            assert _worker["engine_path"] == "stockfish"
        finally:
            _worker.clear()

    def test_a_game_with_no_moves_never_starts_an_engine(self, canonical):
        """Checked before _open_engine, so this runs with no binary available —
        if it ever regressed to opening one first, this test would pay 125 ms
        and fail wherever Stockfish is not installed."""
        _worker["engine_path"] = "/nonexistent/stockfish"

        result = _analyze_one(99, depth=1)

        assert (result.rows, result.status, result.error) == ([], "failed", "no moves stored")

    def test_game_time_class_reads_from_the_canonical_database(self, canonical):
        """New coverage rows carry the time class, so views need no cross-db join."""
        with canonical.begin() as conn:
            conn.execute(text("INSERT INTO games VALUES (7, 'blitz')"))

        assert _game_time_class(7) == "blitz"
        assert _game_time_class(999) is None
