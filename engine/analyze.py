"""Replay games, evaluate every position, persist the result.

Centipawn loss needs one evaluation per ply, not two. Evaluating each position
in sequence makes a move's loss fall out of the difference between consecutive
positions, so a game of N plies costs N+1 evaluations rather than 2N.

Parallelism is across games, not within them, and each game gets its own
single-threaded engine. Multi-threaded Stockfish is non-deterministic, and a
reused engine carries its transposition table into the next game — either one
makes a position's evaluation depend on something other than the position. Only
the parent writes to SQLite, which keeps the workers off a single write lock.
"""

from __future__ import annotations

import os
import sys
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, NamedTuple, Optional

import chess
import chess.engine
from sqlalchemy import create_engine as sa_create_engine
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from engine.db import CANONICAL_DATABASE_URL, SessionLocal
from engine.models import AnalysisRun, GameCoverage, PositionEval, init_engine_db

DEFAULT_DEPTH = 14
DEFAULT_HASH_MB = 64
DEFAULT_ENGINE_PATH = os.getenv("STOCKFISH_PATH", "stockfish")


def default_workers() -> int:
    """One core left for the parent's writes and the OS."""
    return max(1, (os.cpu_count() or 2) - 1)


@dataclass(frozen=True)
class RunConfig:
    """Settings that decide whether two evaluations are comparable.

    Fixed depth rather than fixed movetime. Movetime gives predictable wall clock
    and irreproducible output; depth gives the opposite, and at this scale the
    wall-clock variance is worth less than being able to regenerate the data.
    """

    depth: int = DEFAULT_DEPTH
    hash_mb: int = DEFAULT_HASH_MB
    threads: int = 1
    engine_path: str = DEFAULT_ENGINE_PATH


@dataclass
class Summary:
    """What a batch actually did."""

    run_id: int
    requested: int = 0
    skipped: int = 0
    complete: int = 0
    partial: int = 0
    failed: int = 0
    positions: int = 0


class AnalyzedGame(NamedTuple):
    """What a worker hands back across the process-pool boundary.

    Two of the five fields are strings, so appending a fifth one and unpacking
    it positionally at every call site made a reorder a silent bug instead of a
    type error. A NamedTuple keeps that positional unpacking working while
    giving callers named access too.
    """

    game_id: int
    rows: list
    status: str
    error: Optional[str]
    time_class: Optional[str]


class EngineNotFound(Exception):
    """No Stockfish binary on PATH."""


def engine_version(engine_path: str = DEFAULT_ENGINE_PATH) -> str:
    """Identify the binary, failing before anything is written.

    A missing engine should cost a second, not a half-built database.
    """
    try:
        proc = chess.engine.SimpleEngine.popen_uci(engine_path)
    except FileNotFoundError as exc:
        raise EngineNotFound(
            f"No engine at {engine_path!r}. Install it with `brew install stockfish`, "
            "or point STOCKFISH_PATH at a binary."
        ) from exc
    try:
        return proc.id.get("name", "unknown")
    finally:
        proc.quit()


# ═══════════════════════════════════════════════════════════
# Worker
# ═══════════════════════════════════════════════════════════

_worker: dict = {}


def _init_worker(engine_path: str, hash_mb: int, threads: int):
    """One canonical connection per worker, plus the settings to open engines with.

    Deliberately no long-lived engine. python-chess runs each engine's event loop
    on a *non-daemon* thread, and CPython joins non-daemon threads before it runs
    atexit handlers — so a worker holding an open engine can never shut it down
    on the way out. The batch analyzes every game, writes every row, and then
    hangs forever with the work already done, which reads as a slow run rather
    than a deadlock.

    Opening one engine per game also removes a subtler problem: a reused engine
    carries its transposition table across games, so a position's evaluation
    depended on which games happened to precede it in that worker. A dataset
    whose values shift with batch ordering is not reproducible, whatever the
    depth says.
    """
    _worker["engine_path"] = engine_path
    _worker["hash_mb"] = hash_mb
    _worker["threads"] = threads
    _worker["db"] = sa_create_engine(
        CANONICAL_DATABASE_URL,
        connect_args={"check_same_thread": False},
        echo=False,
    )


def _open_engine():
    """A fresh engine, configured. ~125 ms, against ~4.4 s of search per game."""
    proc = chess.engine.SimpleEngine.popen_uci(_worker["engine_path"])
    proc.configure({"Threads": _worker["threads"], "Hash": _worker["hash_mb"]})
    return proc


def _game_moves(game_id: int) -> list[str]:
    """The stored SAN for one game, in order."""
    with _worker["db"].connect() as conn:
        return [
            row[0]
            for row in conn.execute(
                text("SELECT move_san FROM moves WHERE game_id = :g ORDER BY ply"),
                {"g": game_id},
            )
        ]


def _game_time_class(game_id: int) -> Optional[str]:
    """The stored time class for one game, read from the canonical database."""
    with _worker["db"].connect() as conn:
        return conn.execute(
            text("SELECT time_class FROM games WHERE game_id = :g"),
            {"g": game_id},
        ).scalar()


def _evaluate_position(proc, board: chess.Board, depth: int):
    """Evaluate one position, always from White's point of view.

    Terminal positions are recorded, not searched: asking an engine to evaluate a
    finished game is meaningless and some builds refuse outright. A delivered
    checkmate stores mate_in = 0 and lets the view recover the sign from ply
    parity, which keeps a signed sentinel out of stored ground truth.
    """
    if board.is_checkmate():
        return None, 0, None
    if board.is_game_over():
        return 0, None, None  # stalemate, repetition, material — a real zero

    info = proc.analyse(board, chess.engine.Limit(depth=depth))
    score = info["score"].white()
    pv = info.get("pv") or []
    best = pv[0].uci() if pv else None
    return score.score(), score.mate(), best


def _analyze_one(game_id: int, depth: int) -> AnalyzedGame:
    """Replay one game and evaluate every position it passes through.

    The engine is opened per game and closed by the with-block, while its event
    loop is still alive to process the shutdown. Nothing is left for atexit.
    """
    try:
        sans = _game_moves(game_id)
        # Read even on the path that turns out to have no moves, so a game that
        # fails for that reason still records its time class rather than
        # leaving the column NULL.
        time_class = _game_time_class(game_id)
    except DBAPIError as exc:
        # DBAPIError (OperationalError, IntegrityError, and the rest of that
        # family) means the database layer could not answer for this game —
        # a missing table, a transient lock — which is a per-game data problem,
        # the same invariant _evaluate_game protects below for a game that
        # will not replay. Deliberately not a bare Exception: a KeyError from
        # an uninitialised _worker, or an AttributeError from a renamed field,
        # means this *process* is broken, not this game's data, and must
        # propagate and stop the batch loudly rather than mark every game
        # "failed" with a cryptic message and no traceback.
        return AnalyzedGame(game_id, [], "failed", f"lookup failed: {exc}", None)

    if not sans:
        # Checked before opening an engine: starting Stockfish to analyze a game
        # with no moves costs 125 ms to learn nothing.
        return AnalyzedGame(game_id, [], "failed", "no moves stored", time_class)

    with _open_engine() as proc:
        gid, rows, status, error = _evaluate_game(proc, game_id, sans, depth)
        return AnalyzedGame(gid, rows, status, error, time_class)


def _evaluate_game(proc, game_id: int, sans: list[str], depth: int):
    """Walk one game's moves, evaluating every position including the start.

    Returns (game_id, rows, status, error). Raising here would take down the
    batch; a game that will not replay is a data problem worth recording and
    stepping over, not a reason to lose the other 2,000.
    """
    board = chess.Board()
    rows = []
    try:
        cp, mate, best = _evaluate_position(proc, board, depth)
        rows.append((game_id, 0, cp, mate, best))

        for ply, san in enumerate(sans, start=1):
            board.push_san(san)
            cp, mate, best = _evaluate_position(proc, board, depth)
            rows.append((game_id, ply, cp, mate, best))
    except (ValueError, AssertionError) as exc:
        # Illegal or ambiguous SAN — the stored game does not reconstruct.
        return game_id, rows, "partial" if rows else "failed", f"replay failed at ply {len(rows)}: {exc}"
    except chess.engine.EngineError as exc:
        return game_id, rows, "partial" if rows else "failed", f"engine error: {exc}"

    return game_id, rows, "complete", None


# ═══════════════════════════════════════════════════════════
# Batch
# ═══════════════════════════════════════════════════════════

def get_or_create_run(config: RunConfig) -> int:
    """Find the run matching these settings, or start one.

    Reusing a run is what makes widening a date window cheap: the second pass
    appends to the same body of comparable evaluations instead of starting a
    parallel one nobody can join against.
    """
    version = engine_version(config.engine_path)
    init_engine_db()

    with SessionLocal() as session:
        existing = (
            session.query(AnalysisRun)
            .filter_by(
                engine_name="Stockfish",
                engine_version=version,
                depth=config.depth,
                hash_mb=config.hash_mb,
                threads=config.threads,
            )
            .first()
        )
        if existing:
            return int(existing.run_id)

        run = AnalysisRun(
            engine_name="Stockfish",
            engine_version=version,
            depth=config.depth,
            hash_mb=config.hash_mb,
            threads=config.threads,
            created_at=datetime.utcnow(),
        )
        session.add(run)
        session.commit()
        return int(run.run_id)


def analyze_games(
    game_ids: Iterable[int],
    config: Optional[RunConfig] = None,
    workers: Optional[int] = None,
    run_id: Optional[int] = None,
    progress=None,
) -> Summary:
    """Evaluate a set of games. Resumable, and safe to re-run.

    This is the unit of work a CLI or an endpoint both call. It takes IDs, not a
    search: deciding *what* to analyze is scope.py's job.
    """
    config = config or RunConfig()
    game_ids = list(game_ids)
    if run_id is None:
        run_id = get_or_create_run(config)

    summary = Summary(run_id=run_id, requested=len(game_ids))
    if not game_ids:
        return summary

    workers = workers or default_workers()

    with SessionLocal() as session, ProcessPoolExecutor(
        max_workers=workers,
        initializer=_init_worker,
        initargs=(config.engine_path, config.hash_mb, config.threads),
    ) as pool:
        futures = [pool.submit(_analyze_one, gid, config.depth) for gid in game_ids]

        for done, future in enumerate(futures, start=1):
            game_id, rows, status, error, time_class = future.result()

            if rows:
                # Re-running a partial game re-evaluates from ply 0, so clear
                # whatever the interrupted attempt left behind.
                session.query(PositionEval).filter_by(
                    run_id=run_id, game_id=game_id
                ).delete(synchronize_session=False)
                session.bulk_save_objects([
                    PositionEval(
                        run_id=run_id, game_id=gid, ply=ply,
                        cp=cp, mate_in=mate, best_move_uci=best,
                    )
                    for gid, ply, cp, mate, best in rows
                ])

            session.merge(GameCoverage(
                run_id=run_id,
                game_id=game_id,
                plies_analyzed=len(rows),
                status=status,
                error=error,
                completed_at=datetime.utcnow(),
                time_class=time_class,
            ))
            session.commit()

            summary.positions += len(rows)
            setattr(summary, status, getattr(summary, status) + 1)
            if progress:
                progress(done, len(game_ids), summary)

    return summary


def stderr_progress(done: int, total: int, summary: Summary):
    """A 10-minute batch with no output looks identical to a hung one."""
    pct = 100 * done / total
    print(
        f"\r  {done}/{total} games ({pct:5.1f}%)  "
        f"{summary.positions:,} positions  "
        f"{summary.failed} failed",
        end="",
        file=sys.stderr,
        flush=True,
    )
