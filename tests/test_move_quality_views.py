"""Backfill, Miss, and per-game aggregation."""

import pytest
from sqlalchemy import create_engine, text

from engine.backfill import backfill_coverage_time_class
from engine.models import (
    MOVE_EVALS_VIEW,
    MOVE_QUALITY_VIEW,
    MOVE_SEVERITY_VIEW,
    WP_CURVE_DDL,
)

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


@pytest.fixture
def mq():
    eng = create_engine("sqlite://")
    with eng.begin() as conn:
        conn.execute(text("""
            CREATE TABLE position_evals (
                run_id INTEGER NOT NULL, game_id INTEGER NOT NULL, ply INTEGER NOT NULL,
                cp INTEGER, mate_in INTEGER, best_move_uci VARCHAR(6),
                PRIMARY KEY (run_id, game_id, ply))
        """))
        conn.execute(text("""
            CREATE TABLE game_coverage (
                run_id INTEGER NOT NULL, game_id INTEGER NOT NULL,
                plies_analyzed INTEGER NOT NULL, status VARCHAR(20) NOT NULL,
                error TEXT, completed_at DATETIME, time_class VARCHAR(20),
                PRIMARY KEY (run_id, game_id))
        """))
        conn.execute(text(WP_CURVE_DDL))
        conn.execute(text(MOVE_EVALS_VIEW))
        conn.execute(text(MOVE_SEVERITY_VIEW))
        conn.execute(text(MOVE_QUALITY_VIEW))
        conn.execute(text(
            "INSERT INTO wp_curve (time_class, k, n, source) VALUES ('rapid', 360.0, 1, 'test')"
        ))
    return eng


def seed_mq(eng, positions, game_id=1, run_id=1):
    with eng.begin() as conn:
        conn.execute(
            text("INSERT OR REPLACE INTO game_coverage "
                 "(run_id, game_id, plies_analyzed, status, time_class) "
                 "VALUES (:r, :g, :n, 'complete', 'rapid')"),
            {"r": run_id, "g": game_id, "n": len(positions)},
        )
        for ply, cp in positions:
            conn.execute(
                text("INSERT INTO position_evals (run_id, game_id, ply, cp) "
                     "VALUES (:r, :g, :p, :cp)"),
                {"r": run_id, "g": game_id, "p": ply, "cp": cp},
            )


def mq_rows(eng, game_id=1):
    with eng.connect() as conn:
        return {
            r["ply"]: r
            for r in conn.execute(
                text("SELECT * FROM move_quality WHERE game_id = :g ORDER BY ply"),
                {"g": game_id},
            ).mappings()
        }


class TestMiss:
    """A Miss is failing to take what the opponent just handed you.

    Chess.com makes it a fourth exclusive label. Here it is a flag, so a move can
    be both a blunder by magnitude and a miss by context and both survive.
    """

    def test_giving_back_what_the_opponent_handed_over_is_a_miss(self, mq):
        # White drops 0.1225 at ply 1; Black gives back 0.0809 at ply 2.
        seed_mq(mq, [(0, 0), (1, -180), (2, -60)])
        assert mq_rows(mq)[2]["is_miss"] == 1

    def test_taking_what_was_offered_is_not_a_miss(self, mq):
        # White drops 0.1225; Black concedes only 0.0298, below the 0.05 floor.
        seed_mq(mq, [(0, 0), (1, -180), (2, -135)])
        assert mq_rows(mq)[2]["is_miss"] == 0

    def test_an_error_after_a_quiet_opponent_move_is_not_a_miss(self, mq):
        """Nothing was handed over, so nothing was missed — it is just an error."""
        seed_mq(mq, [(0, 0), (1, 0), (2, 310)])
        r = mq_rows(mq)[2]
        assert r["tier"] == "blunder"
        assert r["is_miss"] == 0

    def test_a_miss_keeps_its_own_tier(self, mq):
        seed_mq(mq, [(0, 0), (1, -180), (2, -60)])
        assert mq_rows(mq)[2]["tier"] == "inaccuracy"

    def test_the_first_ply_has_no_predecessor_and_is_never_a_miss(self, mq):
        seed_mq(mq, [(0, 0), (1, -310)])
        assert mq_rows(mq)[1]["is_miss"] == 0

    def test_a_miss_can_also_be_a_blunder_by_magnitude(self, mq):
        """The reason Miss is a flag, not a tier: both facts must survive together."""
        seed_mq(mq, [(0, 0), (1, -180), (2, 300)])
        r = mq_rows(mq)[2]
        assert r["tier"] == "blunder"
        assert r["is_miss"] == 1

    def test_a_predecessor_just_under_the_mistake_floor_is_not_a_miss(self, mq):
        # White drops 145: wp(0) - wp(-145) = 0.09935, just under MISTAKE_WP
        # (0.10). Black's reply, -145 -> 100, loses 0.16836 -- well clear of
        # INACCURACY_WP -- so only the predecessor side is in question here.
        seed_mq(mq, [(0, 0), (1, -145), (2, 100)])
        assert mq_rows(mq)[2]["is_miss"] == 0

    def test_a_predecessor_just_over_the_mistake_floor_is_a_miss(self, mq):
        # White drops 146: wp(0) - wp(-146) = 0.10002, just over MISTAKE_WP.
        # Same reply shape as above (-146 -> 100), loss 0.16902.
        seed_mq(mq, [(0, 0), (1, -146), (2, 100)])
        assert mq_rows(mq)[2]["is_miss"] == 1

    def test_a_reply_just_under_the_inaccuracy_floor_is_not_a_miss(self, mq):
        # White drops 180 (loss 0.1225, well clear of MISTAKE_WP). Black's
        # reply, -180 -> -106, loses wp(180) - wp(106) = 0.04938, just under
        # INACCURACY_WP (0.05).
        seed_mq(mq, [(0, 0), (1, -180), (2, -106)])
        assert mq_rows(mq)[2]["is_miss"] == 0

    def test_a_reply_just_over_the_inaccuracy_floor_is_a_miss(self, mq):
        # Same predecessor. Black's reply, -180 -> -105, loses
        # wp(180) - wp(105) = 0.05006, just over INACCURACY_WP.
        seed_mq(mq, [(0, 0), (1, -180), (2, -105)])
        assert mq_rows(mq)[2]["is_miss"] == 1
