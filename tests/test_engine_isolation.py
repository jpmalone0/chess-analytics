"""Engine data stays out of the canonical database.

This is the property the whole sidecar layout exists for. If engine rows ever
became a column on games or moves, every existing query would inherit a NULL to
reason about, and a bad run would damage data that took HTTP round-trips to
collect. The join has to work well enough that nobody is tempted.
"""

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError

from engine import db as engine_db
from engine.db import attach_engine_db
from engine.views import MOVE_EVALS_VIEW

# init_engine_db() creates the view inside the engine database, where it is
# unqualified. Reached through ATTACH it is engine.move_evals, which is how
# analysis SQL refers to it.
ATTACHED_VIEW = MOVE_EVALS_VIEW.replace("EXISTS move_evals", "EXISTS engine.move_evals", 1)


def test_unanalyzed_games_join_to_null_rather_than_disappearing():
    """A LEFT JOIN has to keep games nobody has evaluated.

    An INNER JOIN here would make a partially-analyzed player look like they had
    only ever played the games that happen to have been processed — the kind of
    error that reads as a plausible number rather than as missing data.
    """
    eng = create_engine("sqlite://")
    with eng.begin() as conn:
        conn.execute(text(
            "CREATE TABLE games (game_id INTEGER PRIMARY KEY, time_class VARCHAR(20))"
        ))
        conn.execute(text("INSERT INTO games VALUES (1, 'bullet'), (2, 'bullet')"))

        conn.exec_driver_sql("ATTACH DATABASE ':memory:' AS engine")
        conn.exec_driver_sql("""
            CREATE TABLE engine.position_evals (
                run_id INTEGER, game_id INTEGER, ply INTEGER,
                cp INTEGER, mate_in INTEGER, best_move_uci VARCHAR(6),
                PRIMARY KEY (run_id, game_id, ply)
            )
        """)
        conn.exec_driver_sql(ATTACHED_VIEW)

        # Only game 1 is analyzed.
        conn.execute(text(
            "INSERT INTO engine.position_evals VALUES "
            "(1, 1, 0, 20, NULL, NULL), (1, 1, 1, -180, NULL, NULL)"
        ))

        rows = conn.execute(text("""
            SELECT g.game_id, AVG(m.cp_loss) AS avg_loss
            FROM   games g
            LEFT   JOIN engine.move_evals m ON m.game_id = g.game_id
            GROUP  BY g.game_id
            ORDER  BY g.game_id
        """)).mappings().all()

    assert [r["game_id"] for r in rows] == [1, 2]
    assert rows[0]["avg_loss"] == 200
    assert rows[1]["avg_loss"] is None


def test_attach_engine_db_is_idempotent(monkeypatch, tmp_path):
    """A pooled connection may already have the sidecar attached from an
    earlier request. ATTACHing the same alias twice raises OperationalError
    ("database engine is already in use"), so a second attach on a connection
    that already has it must be a safe no-op rather than a surprise 500.
    """
    monkeypatch.setattr(engine_db, "ENGINE_DATABASE_URL", f"sqlite:///{tmp_path / 'engine.db'}")
    eng = create_engine("sqlite://")
    with eng.begin() as conn:
        attach_engine_db(conn)
        attach_engine_db(conn)  # must not raise


def test_attach_engine_db_still_raises_on_a_path_that_cannot_be_opened(monkeypatch, tmp_path):
    """Idempotency must not become a blanket swallow. A bad ENGINE_DATABASE_URL,
    a permissions problem or a corrupt sidecar has to keep surfacing as an
    error -- degrading silently to "no style data" here would hide a real
    configuration problem behind what looks like a missing sidecar.
    """
    unopenable = tmp_path / "nonexistent_dir" / "foo.db"
    monkeypatch.setattr(engine_db, "ENGINE_DATABASE_URL", f"sqlite:///{unopenable}")
    eng = create_engine("sqlite://")
    with eng.begin() as conn:
        with pytest.raises(OperationalError):
            attach_engine_db(conn)


def test_the_canonical_schema_gains_no_engine_columns():
    """The sidecar's whole point: games and moves are untouched.

    Asserted against the app's own models rather than a hand-written list, so
    adding an engine column to them would fail here rather than in a migration.
    """
    from app.models import Game, Move

    engine_ish = {"cp", "cp_loss", "eval", "evaluation", "analyzed", "run_id"}
    for model in (Game, Move):
        names = {c.name for c in model.__table__.columns}
        assert not names & engine_ish, f"{model.__tablename__} gained an engine column"
