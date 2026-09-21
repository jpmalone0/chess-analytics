"""Backfill, Miss, and per-game aggregation."""

import pytest
from sqlalchemy import create_engine, text

from engine.backfill import backfill_coverage_time_class

GAMES_DDL = "CREATE TABLE games (game_id INTEGER PRIMARY KEY, time_class VARCHAR(20))"
GAME_COVERAGE_DDL = (
    "CREATE TABLE game_coverage ("
    " run_id INTEGER, game_id INTEGER, plies_analyzed INTEGER,"
    " status VARCHAR(20), error TEXT, completed_at DATETIME,"
    " time_class VARCHAR(20), PRIMARY KEY (run_id, game_id))"
)


@pytest.fixture
def coverage_dbs(tmp_path, monkeypatch):
    """A canonical/sidecar pair wired up the way backfill_coverage_time_class
    expects: empty games and game_coverage tables, with engine.backfill's
    module globals pointed at them. Each test inserts its own rows.
    """
    canon_path = tmp_path / "canon.db"
    side_path = tmp_path / "engine.db"

    canon = create_engine(f"sqlite:///{canon_path}")
    with canon.begin() as conn:
        conn.execute(text(GAMES_DDL))

    side = create_engine(f"sqlite:///{side_path}")
    with side.begin() as conn:
        conn.execute(text(GAME_COVERAGE_DDL))

    monkeypatch.setattr("engine.backfill.CANONICAL_DATABASE_URL", f"sqlite:///{canon_path}")
    monkeypatch.setattr("engine.backfill.ENGINE_DATABASE_URL", f"sqlite:///{side_path}")

    return canon, side


def test_backfill_copies_time_class_from_the_canonical_database(coverage_dbs):
    """The column exists only because views cannot cross an ATTACH boundary."""
    canon, side = coverage_dbs
    with canon.begin() as conn:
        conn.execute(text("INSERT INTO games VALUES (1, 'rapid'), (2, 'bullet')"))
    with side.begin() as conn:
        conn.execute(text(
            "INSERT INTO game_coverage (run_id, game_id, plies_analyzed, status) "
            "VALUES (1, 1, 80, 'complete'), (1, 2, 60, 'complete')"
        ))

    updated = backfill_coverage_time_class()
    assert updated == 2

    with side.connect() as conn:
        got = dict(conn.execute(text("SELECT game_id, time_class FROM game_coverage")).all())
    assert got == {1: "rapid", 2: "bullet"}


def test_backfill_is_idempotent(coverage_dbs):
    """Running it twice must not rewrite rows that are already correct."""
    canon, side = coverage_dbs
    with canon.begin() as conn:
        conn.execute(text("INSERT INTO games VALUES (1, 'rapid')"))
    with side.begin() as conn:
        conn.execute(text(
            "INSERT INTO game_coverage (run_id, game_id, plies_analyzed, status) "
            "VALUES (1, 1, 80, 'complete')"
        ))

    assert backfill_coverage_time_class() == 1
    assert backfill_coverage_time_class() == 0


def test_backfill_propagates_the_original_error_not_the_detach_failure(coverage_dbs, monkeypatch):
    """A failure between UPDATE and commit must not be masked by DETACH.

    Both leave the transaction open against `canon`; without a rollback on the
    failure path, DETACH raises "database canon is locked" and that -- not
    whatever actually went wrong -- is what the caller sees.
    """
    canon, side = coverage_dbs
    with canon.begin() as conn:
        conn.execute(text("INSERT INTO games VALUES (1, 'rapid')"))
    with side.begin() as conn:
        conn.execute(text(
            "INSERT INTO game_coverage (run_id, game_id, plies_analyzed, status) "
            "VALUES (1, 1, 80, 'complete')"
        ))

    class Boom(Exception):
        """Stands in for whatever real failure could land between the UPDATE
        succeeding and the commit landing."""

    def raise_boom(self):
        raise Boom("commit exploded")

    monkeypatch.setattr("sqlalchemy.engine.base.Connection.commit", raise_boom)

    with pytest.raises(Boom):
        backfill_coverage_time_class()
