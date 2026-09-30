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
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from engine import analyze, views
from engine.analyze import (
    RunConfig,
    _analyze_one,
    _evaluate_game,
    _evaluate_position,
    _game_time_class,
    _init_worker,
    _worker,
    get_or_create_run,
    store_game,
)
from engine.db import Base as EngineBase
from engine.models import PositionEval, PositionPV

# 1. f3 e5 2. g4 Qh4#  — the shortest mate, so the terminal position arrives
# after four plies instead of forty.
FOOLS_MATE = ["f3", "e5", "g4", "Qh4#"]

# Continuation moves for a stub PV. They are never played, only stored, so they
# need to be well-formed UCI rather than legal in the position.
FILLER = ["a7a6", "a2a3", "b7b6", "b2b3", "c7c6", "c2c3", "d7d6", "d2d3"]


class StubEngine:
    """Returns fixed evaluations and remembers every position it was handed.

    Asked for several lines, it offers the legal moves in UCI order, each 10 cp
    worse than the one before: enough to tell ranks apart and to check the
    point-of-view conversion on every candidate, not just the first.
    """

    def __init__(self, cp=25, mate=None, fail_on=None, lines=None, pv_len=1):
        self.cp = cp
        self.mate = mate
        self.fail_on = fail_on          # position index that raises
        self.lines = lines              # fewer lines than asked, as in a near-forced position
        self.pv_len = pv_len
        self.seen: list[str] = []
        self.multipv_asked: list = []

    def analyse(self, board, limit, multipv=None):
        if self.fail_on is not None and len(self.seen) == self.fail_on:
            raise chess.engine.EngineError("stub failure")
        self.seen.append(board.fen())
        self.multipv_asked.append(multipv)
        wanted = multipv or 1
        if self.lines is not None:
            wanted = min(wanted, self.lines)
        infos = []
        for rank, move in enumerate(sorted(board.legal_moves, key=lambda m: m.uci())[:wanted]):
            score = (chess.engine.Mate(self.mate) if self.mate is not None
                     else chess.engine.Cp(self.cp - 10 * rank))
            pv = [move] + [chess.Move.from_uci(u) for u in FILLER[: self.pv_len - 1]]
            infos.append({"score": chess.engine.PovScore(score, board.turn), "pv": pv})
        # python-chess returns a list only when multipv is given.
        return infos if multipv is not None else infos[0]


@pytest.fixture
def stub():
    return StubEngine()


def rows_for(proc, sans, game_id=1, depth=1, multipv=3):
    return _evaluate_game(proc, game_id, sans, depth, multipv)


@pytest.fixture
def canonical():
    """An in-memory canonical database installed as the worker's.

    StaticPool keeps every connect() on the same in-memory database; without
    it the analyzer opens a fresh empty one and finds no moves. Module-level
    so both TestFailureIsolation and TestWorkerLifecycle can use it.
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
        cps = {r.ply: r.cp for r in rows}

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
        last = rows[-1]

        assert (last.ply, last.cp, last.mate_in) == (4, None, 0)

    def test_a_drawn_finish_is_a_real_zero(self):
        """Stalemate is not 'unknown' and not a mate — it is nil."""
        board = chess.Board("7k/5Q2/6K1/8/8/8/8/8 b - - 0 1")
        assert board.is_stalemate()
        assert _evaluate_position(None, board, depth=1, multipv=3) == (0, None, None, ())


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

    def test_a_database_lookup_failure_is_recorded_as_a_failed_game(self, canonical):
        """A missing table (or a transient lock) means this game's data can't
        be read — a per-game problem, not a reason to stop a 200,000-game
        batch. Dropping the table reproduces the database-layer failure
        without needing a real disk-level lock."""
        with canonical.begin() as conn:
            conn.execute(text("DROP TABLE games"))
        _worker["engine_path"] = "/nonexistent/stockfish"

        result = _analyze_one(99, depth=1)

        assert result.status == "failed"
        assert "lookup failed" in result.error

    def test_a_broken_worker_propagates_instead_of_being_recorded(self, canonical):
        """A KeyError from an uninitialised worker (or an AttributeError from a
        renamed field) means the process is broken, not this game's data — it
        must stop the batch loudly rather than mark every game 'failed' with a
        cryptic message and no traceback."""
        del _worker["db"]

        with pytest.raises(KeyError):
            _analyze_one(99, depth=1)


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


class TestCandidates:
    """The top three moves per position, not just the best one.

    One best move cannot say whether a position was critical. The gap between
    the best move and the second is what separates an only-move from a position
    where anything reasonable holds, which is what tactics found and time
    allocation both need.
    """

    def test_each_searched_position_keeps_the_top_three_best_first(self, stub):
        _, rows, _, _ = rows_for(stub, ["e4"])

        assert [c.rank for c in rows[0].candidates] == [1, 2, 3]
        assert [c.move_uci for c in rows[0].candidates] == ["a2a3", "a2a4", "b1a3"]

    def test_the_engine_is_asked_for_three_lines_by_default(self, stub):
        rows_for(stub, ["e4", "e5"])

        assert stub.multipv_asked == [3, 3, 3]

    def test_rank_one_is_what_the_position_stores(self, stub):
        """position_evals stays fed from rank 1, so every view built on it
        reads the same numbers it always did."""
        _, rows, _, _ = rows_for(stub, ["e4", "e5"])

        for row in rows:
            top = row.candidates[0]
            assert (row.cp, row.mate_in, row.best_move_uci) == (top.cp, top.mate_in, top.move_uci)

    def test_candidate_scores_are_stored_from_whites_side(self, stub):
        """The stub scores +25, +15, +5 for the side to move. After 1. e4 that
        side is Black, so White's view of the same three lines is negative."""
        _, rows, _, _ = rows_for(stub, ["e4"])

        assert [c.cp for c in rows[1].candidates] == [-25, -15, -5]

    def test_a_mate_is_kept_as_a_distance_on_every_candidate(self):
        _, rows, _, _ = rows_for(StubEngine(mate=2), ["e4"])

        assert {(c.cp, c.mate_in) for c in rows[0].candidates} == {(None, 2)}

    def test_fewer_lines_than_asked_are_stored_as_they_come(self):
        """A near-forced position can have fewer than three legal moves. What
        the engine returns is stored; nothing is padded or repeated."""
        _, rows, _, _ = rows_for(StubEngine(lines=1), ["e4"])

        assert len(rows[0].candidates) == 1

    def test_a_finished_position_stores_no_candidates(self, stub):
        _, rows, _, _ = rows_for(stub, FOOLS_MATE)

        assert rows[-1].candidates == ()

    def test_the_line_keeps_at_most_five_moves_after_the_candidate(self):
        """Six plies in all, counting the candidate itself: enough to see what
        the move was for, without storing an engine's whole speculation."""
        _, rows, _, _ = rows_for(StubEngine(pv_len=8), ["e4"])

        assert rows[0].candidates[0].line == " ".join(FILLER[:5])


@pytest.fixture
def sidecar_session():
    """The sidecar schema in memory, built by the same create_all production uses."""
    eng = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    EngineBase.metadata.create_all(eng)
    session = sessionmaker(bind=eng)()
    try:
        yield session
    finally:
        session.close()


class TestStoringAGame:
    def test_every_position_gets_an_evaluation_and_every_candidate_a_row(self, sidecar_session):
        _, rows, _, _ = rows_for(StubEngine(), ["e4", "e5"])

        store_game(sidecar_session, run_id=1, game_id=1, rows=rows)
        sidecar_session.commit()

        assert sidecar_session.query(PositionEval).count() == 3
        assert sidecar_session.query(PositionPV).count() == 9

    def test_rank_one_matches_the_stored_best_move(self, sidecar_session):
        _, rows, _, _ = rows_for(StubEngine(), ["e4", "e5"])

        store_game(sidecar_session, run_id=1, game_id=1, rows=rows)
        sidecar_session.commit()

        for ev in sidecar_session.query(PositionEval):
            top = sidecar_session.query(PositionPV).filter_by(
                run_id=1, game_id=1, ply=ev.ply, rank=1).one()
            assert (top.move_uci, top.cp, top.mate_in) == (ev.best_move_uci, ev.cp, ev.mate_in)

    def test_a_finished_position_is_stored_without_candidates(self, sidecar_session):
        _, rows, _, _ = rows_for(StubEngine(), FOOLS_MATE)

        store_game(sidecar_session, run_id=1, game_id=1, rows=rows)
        sidecar_session.commit()

        assert sidecar_session.query(PositionEval).filter_by(ply=4).count() == 1
        assert sidecar_session.query(PositionPV).filter_by(ply=4).count() == 0

    def test_storing_a_game_again_replaces_the_earlier_attempt(self, sidecar_session):
        """A partial game is re-evaluated from ply 0, so whatever the
        interrupted attempt left behind must not survive alongside it."""
        _, first, _, _ = rows_for(StubEngine(), ["e4", "e5", "Nf3"])
        _, again, _, _ = rows_for(StubEngine(), ["e4"])

        store_game(sidecar_session, run_id=1, game_id=1, rows=first)
        store_game(sidecar_session, run_id=1, game_id=1, rows=again)
        sidecar_session.commit()

        assert sidecar_session.query(PositionEval).count() == 2
        assert sidecar_session.query(PositionPV).count() == 6


class TestRuns:
    """Three candidates and one are different searches, so they are different runs.

    Stockfish prunes differently under MultiPV: in a 10-game sample the best
    move changed in 26% of positions. Mixing the two in one run would make a
    position's evaluation depend on which setting happened to reach it.
    """

    @pytest.fixture
    def sidecar(self, tmp_path, monkeypatch):
        """Patched the way TestInitEngineDb patches it: engine/db.py builds its
        Engine at import time, so only replacing the objects keeps these tests
        off the real chess_engine.db."""
        eng = create_engine(f"sqlite:///{tmp_path / 'sidecar.db'}")
        monkeypatch.setattr(views, "engine", eng)
        monkeypatch.setattr(analyze, "SessionLocal", sessionmaker(bind=eng))
        monkeypatch.setattr(analyze, "engine_version", lambda path: "Stockfish 19")
        return eng

    def test_the_default_run_asks_for_three_candidates(self):
        assert RunConfig().multipv == 3

    def test_a_different_candidate_count_is_a_different_run(self, sidecar):
        assert get_or_create_run(RunConfig(multipv=1)) != get_or_create_run(RunConfig(multipv=3))

    def test_the_same_settings_reuse_their_run(self, sidecar):
        assert get_or_create_run(RunConfig()) == get_or_create_run(RunConfig())

    def test_an_older_sidecar_marks_its_runs_as_single_line(self, sidecar):
        """Runs recorded before the column existed searched one line each."""
        with sidecar.begin() as conn:
            conn.execute(text(
                "CREATE TABLE analysis_runs (run_id INTEGER PRIMARY KEY, "
                "engine_name VARCHAR(50) NOT NULL, engine_version VARCHAR(50) NOT NULL, "
                "depth INTEGER NOT NULL, hash_mb INTEGER NOT NULL, threads INTEGER NOT NULL, "
                "created_at DATETIME NOT NULL)"
            ))
            conn.execute(text(
                "INSERT INTO analysis_runs VALUES "
                "(1, 'Stockfish', 'Stockfish 19', 14, 64, 1, '2026-09-16 00:00:00')"
            ))

        views.init_engine_db()

        with sidecar.connect() as conn:
            assert conn.execute(text(
                "SELECT multipv FROM analysis_runs WHERE run_id = 1")).scalar() == 1
