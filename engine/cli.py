"""Command-line entry point for engine evaluation.

    python -m engine.cli --player ballasack6 --time-class bullet --dry-run
    python -m engine.cli --player ballasack6 --time-class bullet --since 2026-06-01

--dry-run resolves the scope and reports what a run would cost without starting
one, which is the cheap way to find out that a date window is wider than intended.
"""

import argparse
import sys
from datetime import date, datetime

from sqlalchemy import text

from engine.analyze import (
    DEFAULT_DEPTH,
    DEFAULT_ENGINE_PATH,
    DEFAULT_HASH_MB,
    EngineNotFound,
    RunConfig,
    analyze_games,
    default_workers,
    engine_version,
    get_or_create_run,
    stderr_progress,
)
from engine.db import analysis_engine
from engine.models import init_engine_db
from engine.scope import Scope, UnknownPlayer, resolve_scope, unanalyzed

# Search cost per position at each depth, measured over 150 positions sampled
# from real bullet games across every phase.
MS_PER_POSITION_BY_DEPTH = {10: 7.2, 12: 17.5, 14: 60.6}

# End-to-end throughput, measured over a 303-game batch on an Apple M3: 48
# games/min at depth 14 on 7 workers. Multiplying ms/position by worker count
# predicts 95, because an M3's 8 cores are 4 performance + 4 efficiency and an
# efficiency core does not do a performance core's work.
#
# Calibrating against the measurement rather than the model matters more than it
# looks: an estimate that is 2x optimistic makes a healthy run look stalled, and
# that is exactly what masked a real deadlock during development.
GAMES_PER_MIN_AT_REFERENCE = 48.0
REFERENCE_WORKERS = 7


def estimated_minutes(games: int, workers: int, depth: int) -> float:
    """Rough wall-clock for a batch. Approximate by construction."""
    ms = MS_PER_POSITION_BY_DEPTH.get(depth)
    if ms is None:
        # An unmeasured depth. Search cost climbs steeply, so anything deeper
        # than 14 will take longer than this says.
        ms = MS_PER_POSITION_BY_DEPTH[DEFAULT_DEPTH]
    rate = (
        GAMES_PER_MIN_AT_REFERENCE
        * (workers / REFERENCE_WORKERS)
        * (MS_PER_POSITION_BY_DEPTH[DEFAULT_DEPTH] / ms)
    )
    return games / rate


def _parse_date(value: str) -> date:
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected YYYY-MM-DD, got {value!r}") from None


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m engine.cli",
        description="Evaluate a player's games with Stockfish.",
    )
    p.add_argument("--player", required=True, help="chess.com username")
    p.add_argument("--time-class", choices=["bullet", "blitz", "rapid", "daily"])
    p.add_argument("--color", choices=["white", "black"], dest="player_color")
    p.add_argument("--since", type=_parse_date, metavar="YYYY-MM-DD")
    p.add_argument("--until", type=_parse_date, metavar="YYYY-MM-DD")
    p.add_argument("--tz", help="IANA zone for the date window, e.g. America/New_York")
    p.add_argument("--opening", dest="opening_names", help='"|"-joined opening prefixes')
    p.add_argument("--limit", type=int, help="cap the number of games")
    p.add_argument("--depth", type=int, default=DEFAULT_DEPTH)
    p.add_argument("--hash-mb", type=int, default=DEFAULT_HASH_MB)
    p.add_argument("--workers", type=int, help="default: cores - 1")
    p.add_argument("--engine-path", default=DEFAULT_ENGINE_PATH)
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="resolve the scope and estimate the cost without evaluating",
    )
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)

    config = RunConfig(
        depth=args.depth,
        hash_mb=args.hash_mb,
        threads=1,
        engine_path=args.engine_path,
    )

    # Fail on a missing binary before creating a database.
    try:
        version = engine_version(config.engine_path)
    except EngineNotFound as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    init_engine_db()
    scope = Scope(
        username=args.player,
        time_class=args.time_class,
        start_date=args.since,
        end_date=args.until,
        player_color=args.player_color,
        opening_names=args.opening_names,
        tz=args.tz,
        limit=args.limit,
    )

    canonical = analysis_engine()
    with canonical.connect() as conn:
        try:
            game_ids = resolve_scope(conn, scope)
        except UnknownPlayer:
            print(
                f"error: no player named {args.player!r} in the database. "
                "Sync them first.",
                file=sys.stderr,
            )
            return 2

        if args.dry_run:
            # A dry run should not create a run row; look one up only if it exists.
            existing = conn.execute(
                text(
                    "SELECT run_id FROM engine.analysis_runs "
                    "WHERE engine_version = :v AND depth = :d AND hash_mb = :h "
                    "AND threads = 1"
                ),
                {"v": version, "d": config.depth, "h": config.hash_mb},
            ).first()
            todo = (
                unanalyzed(conn, game_ids, existing[0]) if existing else game_ids
            )
        else:
            run_id = get_or_create_run(config)
            todo = unanalyzed(conn, game_ids, run_id)

    workers = args.workers or default_workers()
    est = estimated_minutes(len(todo), workers, config.depth)
    print(
        f"{args.player}: {len(game_ids)} games in scope, "
        f"{len(game_ids) - len(todo)} already analyzed, {len(todo)} to do "
        f"(~{est:.1f} min on {workers} workers, {version} depth {config.depth})"
    )

    if args.dry_run or not todo:
        return 0

    summary = analyze_games(todo, config, workers=args.workers, progress=stderr_progress)
    print(file=sys.stderr)
    print(
        f"run {summary.run_id}: {summary.complete} complete, "
        f"{summary.partial} partial, {summary.failed} failed, "
        f"{summary.positions:,} positions stored"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
