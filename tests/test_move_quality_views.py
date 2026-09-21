"""Backfill, Miss, and per-game aggregation."""

from sqlalchemy import text

from engine.backfill import backfill_coverage_time_class


def test_backfill_copies_time_class_from_the_canonical_database(tmp_path, monkeypatch):
    """The column exists only because views cannot cross an ATTACH boundary."""
    from sqlalchemy import create_engine

    canon = tmp_path / "canon.db"
    side = tmp_path / "engine.db"

    c = create_engine(f"sqlite:///{canon}")
    with c.begin() as conn:
        conn.execute(text("CREATE TABLE games (game_id INTEGER PRIMARY KEY, time_class VARCHAR(20))"))
        conn.execute(text("INSERT INTO games VALUES (1, 'rapid'), (2, 'bullet')"))

    s = create_engine(f"sqlite:///{side}")
    with s.begin() as conn:
        conn.execute(text(
            "CREATE TABLE game_coverage ("
            " run_id INTEGER, game_id INTEGER, plies_analyzed INTEGER,"
            " status VARCHAR(20), error TEXT, completed_at DATETIME,"
            " time_class VARCHAR(20), PRIMARY KEY (run_id, game_id))"
        ))
        conn.execute(text(
            "INSERT INTO game_coverage (run_id, game_id, plies_analyzed, status) "
            "VALUES (1, 1, 80, 'complete'), (1, 2, 60, 'complete')"
        ))

    monkeypatch.setattr("engine.backfill.CANONICAL_DATABASE_URL", f"sqlite:///{canon}")
    monkeypatch.setattr("engine.backfill.ENGINE_DATABASE_URL", f"sqlite:///{side}")

    updated = backfill_coverage_time_class()
    assert updated == 2

    with s.connect() as conn:
        got = dict(conn.execute(text("SELECT game_id, time_class FROM game_coverage")).all())
    assert got == {1: "rapid", 2: "bullet"}


def test_backfill_is_idempotent(tmp_path, monkeypatch):
    """Running it twice must not rewrite rows that are already correct."""
    from sqlalchemy import create_engine

    canon = tmp_path / "canon.db"
    side = tmp_path / "engine.db"
    c = create_engine(f"sqlite:///{canon}")
    with c.begin() as conn:
        conn.execute(text("CREATE TABLE games (game_id INTEGER PRIMARY KEY, time_class VARCHAR(20))"))
        conn.execute(text("INSERT INTO games VALUES (1, 'rapid')"))
    s = create_engine(f"sqlite:///{side}")
    with s.begin() as conn:
        conn.execute(text(
            "CREATE TABLE game_coverage ("
            " run_id INTEGER, game_id INTEGER, plies_analyzed INTEGER,"
            " status VARCHAR(20), error TEXT, completed_at DATETIME,"
            " time_class VARCHAR(20), PRIMARY KEY (run_id, game_id))"
        ))
        conn.execute(text(
            "INSERT INTO game_coverage (run_id, game_id, plies_analyzed, status) "
            "VALUES (1, 1, 80, 'complete')"
        ))

    monkeypatch.setattr("engine.backfill.CANONICAL_DATABASE_URL", f"sqlite:///{canon}")
    monkeypatch.setattr("engine.backfill.ENGINE_DATABASE_URL", f"sqlite:///{side}")

    assert backfill_coverage_time_class() == 1
    assert backfill_coverage_time_class() == 0
