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

The same hazard exists on the failure path: if anything raises between the
UPDATE succeeding and the commit landing, the transaction is still open and
still referencing `canon` when `finally` runs DETACH, which fails with the
same "database canon is locked" -- and that masks whatever actually went
wrong, since the DETACH's exception is the one that propagates. Rolling back
before DETACH releases `canon` so DETACH succeeds and the original exception
is the one the caller sees.
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
    try:
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
            except Exception:
                # Must also land before DETACH, for the same reason: an
                # exception here leaves the transaction open against canon,
                # and DETACH would fail and mask it. Roll back so DETACH
                # succeeds and this exception is the one that propagates.
                conn.rollback()
                raise
            finally:
                conn.exec_driver_sql("DETACH DATABASE canon")
    finally:
        sidecar.dispose()
