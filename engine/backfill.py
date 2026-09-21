"""One-off, idempotent backfill of game_coverage.time_class.

Runs across the ATTACH boundary, which is allowed for a statement even though it
is forbidden for a view definition. Existing coverage rows predate the column;
without this they would have a NULL time_class, and every severity view inner
joins wp_curve through it, so those games would silently vanish rather than
error.

Transaction management is manual here, deliberately. `sidecar.begin()` only
commits when its `with` block exits, which is *after* a `finally` clause would
already have run -- so a naive `with sidecar.begin() as conn: ... finally:
DETACH` attempts the DETACH while the UPDATE's transaction against `canon` is
still open, and SQLite refuses with "database canon is locked". Committing by
hand before the DETACH is what avoids that; don't refactor this back to
`sidecar.begin()`.
"""

from sqlalchemy import create_engine, text

from engine.db import (
    CANONICAL_DATABASE_URL,
    ENGINE_DATABASE_URL,
    _sqlite_path,
)


def backfill_coverage_time_class() -> int:
    """Fill NULL time_class values. Returns the number of rows updated."""
    sidecar = create_engine(ENGINE_DATABASE_URL, connect_args={"check_same_thread": False})
    with sidecar.connect() as conn:
        conn.exec_driver_sql(
            f"ATTACH DATABASE '{_sqlite_path(CANONICAL_DATABASE_URL)}' AS canon"
        )
        try:
            result = conn.execute(text("""
                UPDATE game_coverage
                SET    time_class = (
                           SELECT g.time_class FROM canon.games g
                           WHERE  g.game_id = game_coverage.game_id
                       )
                WHERE  time_class IS NULL
            """))
            rowcount = int(result.rowcount)
            # Must land before DETACH: SQLite refuses to detach a database
            # while a transaction against it is still open.
            conn.commit()
            return rowcount
        finally:
            conn.exec_driver_sql("DETACH DATABASE canon")
