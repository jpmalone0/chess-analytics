"""Engine-analyzed populations, one rating band at a time.

The corpus has moves for 204k games and engine evaluations for a few thousand,
nearly all of them one player's. A pooled band rate needs analyzed games from
many players at that rating, so this module picks them and runs the engine over
them in the background.

Two pieces:

* `sample_band` decides which games. Deterministic, so pressing the button
  twice extends the same sample instead of drawing a second one.
* `JobRunner` runs the jobs, one at a time. A job already uses every core but
  one, so running two bands at once would only split the CPU between them.
"""

from __future__ import annotations

import queue
import threading
from contextlib import AbstractContextManager
from datetime import datetime
from typing import Any, Callable, Optional

from sqlalchemy import text

from engine.models import PopulationJob

# A fraction of baselines.PER_PLAYER_CAP on purpose. The corpus averages ~2
# games per player anyway, so a tight cap costs almost nothing and buys breadth
# per engine-minute.
POPULATION_PER_PLAYER_CAP = 5

DEFAULT_TARGET_GAMES = 300
MAX_TARGET_GAMES = 3000

ACTIVE = ("queued", "running")

# A multiplicative hash over game_id: a fixed shuffle that is spread across the
# corpus's dates and stable across calls. ORDER BY RANDOM() would redraw the
# sample on every press, and ORDER BY game_id would take the oldest games first.
SHUFFLE = "((g.game_id * 2654435761) % 4294967291)"


def band_sides_sql(exclude: bool = True) -> str:
    """The in-band sides of every eligible game, as (game_id, pid, color, h).

    A side is in the band when its own Elo is. Games involving the excluded
    player are dropped entirely, not just their seat: the opponent mirror
    already covers those games, and keeping them would make the population
    partly a sample of people playing the viewed player.
    """
    excl = (
        "AND g.white_player_id <> :exclude_player_id "
        "AND g.black_player_id <> :exclude_player_id"
        if exclude else ""
    )
    base = f"""
        FROM games g
        WHERE g.time_class = :time_class AND g.variant IS NULL {excl}
    """
    return f"""
        SELECT g.game_id, g.white_player_id AS pid, 'white' AS color, {SHUFFLE} AS h
        {base} AND g.white_elo BETWEEN :elo_lo AND :elo_hi
        UNION ALL
        SELECT g.game_id, g.black_player_id AS pid, 'black' AS color, {SHUFFLE} AS h
        {base} AND g.black_elo BETWEEN :elo_lo AND :elo_hi
    """


def sample_band(
    conn,
    *,
    time_class: str,
    elo_lo: int,
    elo_hi: int,
    exclude_player_id: Optional[int],
    run_id: int,
    limit: int,
) -> list[int]:
    """Up to `limit` game ids in the band not yet complete under `run_id`.

    `conn` is a canonical connection with the sidecar attached. The per-player
    cap ranks all of a player's in-band games, analyzed or not, so it holds
    across presses: a player whose five games are done contributes nothing more.
    """
    rows = conn.execute(text(f"""
        WITH sides AS ({band_sides_sql(exclude_player_id is not None)}),
        ranked AS (
            SELECT game_id, h,
                   ROW_NUMBER() OVER (PARTITION BY pid ORDER BY h, game_id) AS rn
            FROM sides
        )
        SELECT game_id, MIN(h) AS h
        FROM   ranked r
        WHERE  rn <= :cap
          AND  EXISTS (SELECT 1 FROM moves m WHERE m.game_id = r.game_id AND m.ply = 2)
          AND  NOT EXISTS (
                   SELECT 1 FROM engine.game_coverage c
                   WHERE c.game_id = r.game_id AND c.run_id = :run_id
                     AND c.status = 'complete')
        GROUP  BY game_id
        ORDER  BY h, game_id
        LIMIT  :limit
    """), {
        "time_class": time_class, "elo_lo": elo_lo, "elo_hi": elo_hi,
        "exclude_player_id": exclude_player_id, "run_id": run_id,
        "cap": POPULATION_PER_PLAYER_CAP, "limit": limit,
    })
    return [r[0] for r in rows]


def job_dict(job: PopulationJob) -> dict[str, Any]:
    return {
        "job_id": job.job_id,
        "time_class": job.time_class,
        "elo_lo": job.elo_lo,
        "elo_hi": job.elo_hi,
        "target_games": job.target_games,
        "games_total": job.games_total,
        "games_done": job.games_done,
        "status": job.status,
        "error": job.error,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
    }


class JobRunner:
    """A queue of population jobs, drained by one background thread.

    Everything that touches Stockfish or a real database is passed in, so tests
    can run a job synchronously against temporary files:

    * `sessions`: a sessionmaker over the sidecar, where job rows live.
    * `connect`: a context manager yielding a canonical connection with the
      sidecar attached, for sampling.
    * `run_id`: returns the analysis run to sample against and write into.
    * `analyze`: `(game_ids, run_id, progress) -> Summary`.
    """

    def __init__(
        self,
        sessions,
        connect: Callable[[], AbstractContextManager],
        run_id: Callable[[], int],
        analyze: Callable[..., Any],
    ):
        self._sessions = sessions
        self._connect = connect
        self._run_id = run_id
        self._analyze = analyze
        self._queue: queue.Queue[int] = queue.Queue()
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None

    def recover_interrupted(self) -> int:
        """Fail whatever a previous process left queued or running.

        The queue lives in memory, so a restart (including uvicorn --reload on
        every save) orphans it. Pressing the button again resumes: games that
        finished are skipped by the sampler.
        """
        with self._sessions() as s:
            n = (
                s.query(PopulationJob)
                .filter(PopulationJob.status.in_(ACTIVE))
                .update({
                    "status": "failed",
                    "error": "interrupted by a server restart; press again to resume",
                    "finished_at": datetime.utcnow(),
                }, synchronize_session=False)
            )
            s.commit()
            return n

    def enqueue(
        self,
        *,
        time_class: str,
        elo_lo: int,
        elo_hi: int,
        target_games: int,
        exclude_player_id: Optional[int],
        start: bool = True,
    ) -> tuple[dict[str, Any], bool]:
        """Queue a job, or return the band's queued or running one.

        Returns (job, created). A second press on a band already in flight
        must not queue a duplicate that would sample the same games.
        """
        with self._lock, self._sessions() as s:
            existing = (
                s.query(PopulationJob)
                .filter_by(time_class=time_class, elo_lo=elo_lo, elo_hi=elo_hi)
                .filter(PopulationJob.status.in_(ACTIVE))
                .first()
            )
            if existing:
                return job_dict(existing), False
            job = PopulationJob(
                time_class=time_class, elo_lo=elo_lo, elo_hi=elo_hi,
                exclude_player_id=exclude_player_id,
                target_games=target_games, games_done=0, status="queued",
                created_at=datetime.utcnow(),
            )
            s.add(job)
            s.commit()
            out = job_dict(job)

        self._queue.put(out["job_id"])
        if start:
            self._ensure_thread()
        return out, True

    def jobs(self, limit: int = 10) -> list[dict[str, Any]]:
        """Active jobs first, then the most recent finished ones."""
        with self._sessions() as s:
            active = (
                s.query(PopulationJob)
                .filter(PopulationJob.status.in_(ACTIVE))
                .order_by(PopulationJob.job_id).all()
            )
            done = (
                s.query(PopulationJob)
                .filter(PopulationJob.status.notin_(ACTIVE))
                .order_by(PopulationJob.job_id.desc()).limit(limit).all()
            )
            return [job_dict(j) for j in active + done]

    def active_job(self, time_class: str, elo_lo: int, elo_hi: int) -> Optional[dict]:
        with self._sessions() as s:
            job = (
                s.query(PopulationJob)
                .filter_by(time_class=time_class, elo_lo=elo_lo, elo_hi=elo_hi)
                .filter(PopulationJob.status.in_(ACTIVE))
                .first()
            )
            return job_dict(job) if job else None

    def _ensure_thread(self) -> None:
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(
                    target=self._drain, name="population-jobs", daemon=True)
                self._thread.start()

    def _drain(self) -> None:
        while True:
            self.run_job(self._queue.get())

    def run_pending(self) -> None:
        """Run everything queued, on the calling thread. For tests."""
        while not self._queue.empty():
            self.run_job(self._queue.get())

    def _update(self, job_id: int, **fields) -> None:
        with self._sessions() as s:
            s.query(PopulationJob).filter_by(job_id=job_id).update(
                fields, synchronize_session=False)
            s.commit()

    def run_job(self, job_id: int) -> None:
        with self._sessions() as s:
            job = s.get(PopulationJob, job_id)
            if job is None or job.status != "queued":
                return
            params = dict(
                time_class=job.time_class, elo_lo=job.elo_lo, elo_hi=job.elo_hi,
                exclude_player_id=job.exclude_player_id, limit=job.target_games,
            )
        self._update(job_id, status="running", started_at=datetime.utcnow())

        try:
            run_id = self._run_id()
            with self._connect() as conn:
                ids = sample_band(conn, run_id=run_id, **params)
            self._update(job_id, games_total=len(ids))

            def progress(done, total, summary):
                self._update(job_id, games_done=done)

            summary = self._analyze(ids, run_id, progress)
            failed = getattr(summary, "failed", 0)
            self._update(
                job_id, status="complete", games_done=len(ids),
                finished_at=datetime.utcnow(),
                error=f"{failed} games failed to analyze" if failed else None,
            )
        except Exception as exc:  # the job's failure, not the thread's
            self._update(
                job_id, status="failed", error=f"{type(exc).__name__}: {exc}",
                finished_at=datetime.utcnow(),
            )
