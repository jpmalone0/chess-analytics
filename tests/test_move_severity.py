"""The win-probability curve and the tier ladder.

Severity is a change in expected points, not in centipawns. Every threshold in
this feature lives in the view, so these tests are the only place the ladder is
pinned.
"""

import math

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError, OperationalError

from engine.db import Base
from engine.views import (
    MOVE_EVALS_VIEW,
    MOVE_SEVERITY_VIEW,
    WP_CURVE_DDL,
    MathFunctionsMissing,
    assert_sqlite_has_math,
)

RAPID_K = 360.0


def wp(cp, white=True):
    """The curve, in Python, for computing expectations independently."""
    sign = 1 if white else -1
    clamped = max(-1000, min(1000, cp))
    return 1 / (1 + math.exp(-(sign * clamped) / RAPID_K))


def test_sqlite_has_math_functions():
    eng = create_engine("sqlite://")
    with eng.connect() as conn:
        assert_sqlite_has_math(conn)


class _RaisingConn:
    """A stub connection whose execute() always raises the given exception.

    Lets the guard's translation logic be pinned without needing a real SQLite
    build that actually lacks exp(), or a real locked/corrupt database.
    """

    def __init__(self, exc):
        self._exc = exc

    def execute(self, *_args, **_kwargs):
        raise self._exc


def test_assert_sqlite_has_math_translates_missing_function():
    orig = OperationalError("SELECT exp(1.0)", None, Exception("no such function: exp"))
    with pytest.raises(MathFunctionsMissing) as excinfo:
        assert_sqlite_has_math(_RaisingConn(orig))
    assert excinfo.value.__cause__ is orig


def test_assert_sqlite_has_math_reraises_unrelated_operational_errors():
    orig = OperationalError("SELECT exp(1.0)", None, Exception("database is locked"))
    with pytest.raises(OperationalError):
        assert_sqlite_has_math(_RaisingConn(orig))


# wp_curve is defined twice by hand -- once as the WpCurve model, once as the
# WP_CURVE_DDL string the view tests build their in-memory sidecar from -- and
# the two can drift. Every case below runs against both, so an edit that touches
# one definition and not the other fails here instead of surviving until
# somebody inspects the emitted SQL by hand.
def _built_from_ddl():
    """The hand-written DDL string, as the view tests use it."""
    eng = create_engine("sqlite://")
    with eng.begin() as conn:
        conn.execute(text(WP_CURVE_DDL))
    return eng


def _built_from_model():
    """The ORM metadata, by the same call init_engine_db makes in production.

    create_all() also builds the other sidecar tables, but the wp_curve DDL it
    emits is byte-identical to WpCurve.__table__.create(), so this costs
    nothing and stays honest about the path production actually takes.
    """
    eng = create_engine("sqlite://")
    Base.metadata.create_all(eng)
    return eng


BUILDERS = [
    pytest.param(_built_from_ddl, id="ddl"),
    pytest.param(_built_from_model, id="model"),
]

# Rows the table must refuse, whichever definition built it. k is written by an
# out-of-band fitting process with nothing above the schema checking it: a
# negative k inverts the curve and grades gains as blunders, a zero k divides
# by zero and turns every severity NULL, and a missing k or n loses the
# provenance that keeps a refit a deliberate, recorded act.
REJECTED_ROWS = [
    pytest.param({"time_class": "bullet", "k": None, "n": 5}, id="k-null"),
    pytest.param({"time_class": "bullet", "k": 865.0, "n": None}, id="n-null"),
    pytest.param({"time_class": "bullet", "k": 0.0, "n": 5}, id="k-zero"),
    pytest.param({"time_class": "bullet", "k": -360.0, "n": 5}, id="k-negative"),
]


@pytest.mark.parametrize("build", BUILDERS)
def test_wp_curve_round_trips(build):
    """Insert-and-read-back exercises the shape both definitions describe."""
    eng = build()
    with eng.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO wp_curve (time_class, k, n, fitted_at, source) "
                "VALUES ('rapid', 360.0, 128000, '2026-01-01', 'corpus fit')"
            )
        )
        row = conn.execute(
            text("SELECT time_class, k, n FROM wp_curve WHERE time_class = 'rapid'")
        ).fetchone()
    assert row == ("rapid", 360.0, 128000)


@pytest.mark.parametrize("values", REJECTED_ROWS)
@pytest.mark.parametrize("build", BUILDERS)
def test_wp_curve_rejects_bad_rows(build, values):
    eng = build()
    with eng.begin() as conn:
        with pytest.raises(IntegrityError):
            conn.execute(
                text(
                    "INSERT INTO wp_curve (time_class, k, n) "
                    "VALUES (:time_class, :k, :n)"
                ),
                values,
            )


@pytest.fixture
def sev():
    """An in-memory sidecar holding the tables and both views.

    Built from raw DDL rather than the ORM so that the view SQL is exercised
    exactly as SQLite will run it.
    """
    eng = create_engine("sqlite://")
    with eng.begin() as conn:
        conn.execute(text("""
            CREATE TABLE position_evals (
                run_id        INTEGER NOT NULL,
                game_id       INTEGER NOT NULL,
                ply           INTEGER NOT NULL,
                cp            INTEGER,
                mate_in       INTEGER,
                best_move_uci VARCHAR(6),
                PRIMARY KEY (run_id, game_id, ply)
            )
        """))
        conn.execute(text("""
            CREATE TABLE game_coverage (
                run_id         INTEGER NOT NULL,
                game_id        INTEGER NOT NULL,
                plies_analyzed INTEGER NOT NULL,
                status         VARCHAR(20) NOT NULL,
                error          TEXT,
                completed_at   DATETIME,
                time_class     VARCHAR(20),
                PRIMARY KEY (run_id, game_id)
            )
        """))
        conn.execute(text(WP_CURVE_DDL))
        conn.execute(text(MOVE_EVALS_VIEW))
        conn.execute(text(MOVE_SEVERITY_VIEW))
        conn.execute(text(
            "INSERT INTO wp_curve (time_class, k, n, source) VALUES ('rapid', 360.0, 45110, 'test')"
        ))
    return eng


def seed(eng, positions, game_id=1, run_id=1, time_class="rapid"):
    """positions: [(ply, cp, mate_in)] — evaluations from White's point of view."""
    with eng.begin() as conn:
        conn.execute(
            text("INSERT OR REPLACE INTO game_coverage "
                 "(run_id, game_id, plies_analyzed, status, time_class) "
                 "VALUES (:r, :g, :n, 'complete', :tc)"),
            {"r": run_id, "g": game_id, "n": len(positions), "tc": time_class},
        )
        for ply, cp, mate_in in positions:
            conn.execute(
                text("INSERT INTO position_evals (run_id, game_id, ply, cp, mate_in) "
                     "VALUES (:r, :g, :p, :cp, :m)"),
                {"r": run_id, "g": game_id, "p": ply, "cp": cp, "m": mate_in},
            )


def rows(eng, game_id=1):
    with eng.connect() as conn:
        return {
            r["ply"]: r
            for r in conn.execute(
                text("SELECT * FROM move_severity WHERE game_id = :g ORDER BY ply"),
                {"g": game_id},
            ).mappings()
        }


class TestCurve:
    def test_an_even_position_is_half_a_point(self, sev):
        seed(sev, [(0, 0, None), (1, 0, None)])
        assert rows(sev)[1]["wp_before"] == pytest.approx(0.5)

    def test_the_view_agrees_with_the_curve_computed_in_python(self, sev):
        seed(sev, [(0, 120, None), (1, -240, None)])
        r = rows(sev)[1]
        assert r["wp_before"] == pytest.approx(wp(120), abs=1e-9)
        assert r["wp_after"] == pytest.approx(wp(-240), abs=1e-9)

    def test_black_is_measured_from_blacks_side(self, sev):
        """Stored evaluations are White-relative; loss belongs to whoever moved."""
        seed(sev, [(1, 0, None), (2, 310, None)])
        r = rows(sev)[2]
        assert r["color"] == "black"
        assert r["wp_loss"] == pytest.approx(0.2029, abs=1e-4)


class TestLadder:
    """The thresholds live in the view. This is the only place they are pinned."""

    @pytest.mark.parametrize("cp_after,expected", [
        (-310, "blunder"),
        (-300, "mistake"),
        (-150, "mistake"),
        (-140, "inaccuracy"),
        (-75, "inaccuracy"),
        (-70, None),
    ])
    def test_each_boundary(self, sev, cp_after, expected):
        seed(sev, [(0, 0, None), (1, cp_after, None)])
        assert rows(sev)[1]["tier"] == expected


class TestClamp:
    def test_a_decided_position_cannot_book_a_loss(self, sev):
        """Up a queen, a further swing is worth almost nothing. The +/-1000 clamp
        is what stops dead-won games producing a stream of fake blunders."""
        seed(sev, [(0, 5000, None), (1, 1500, None)])
        assert rows(sev)[1]["wp_loss"] == pytest.approx(0.0)
        assert rows(sev)[1]["tier"] is None

    def test_an_apparent_gain_is_not_a_negative_loss(self, sev):
        seed(sev, [(0, 0, None), (1, 200, None)])
        assert rows(sev)[1]["wp_loss"] == pytest.approx(0.0)


class TestMate:
    """move_evals maps a mate to +/-10000; the curve sees it through the clamp."""

    def test_walking_into_a_forced_mate_is_a_blunder(self, sev):
        seed(sev, [(0, 0, None), (1, None, -3)])
        assert rows(sev)[1]["tier"] == "blunder"

    def test_delivering_a_forced_mate_costs_nothing(self, sev):
        seed(sev, [(0, 0, None), (1, None, 3)])
        assert rows(sev)[1]["wp_loss"] == pytest.approx(0.0)

    def test_a_slower_mate_is_not_an_error(self, sev):
        """M1 played as M4 is still winning. Lichess reaches the same conclusion
        independently: its MateDelayed case returns no judgement at all."""
        seed(sev, [(0, None, 1), (1, None, 4)])
        assert rows(sev)[1]["tier"] is None


class TestUnfittedTimeClass:
    def test_a_time_class_with_no_curve_yields_no_rows(self, sev):
        """Blitz has no fitted k until blitz games are analyzed. Borrowing
        another class's curve would silently misprice every blitz error, so the
        join drops them instead."""
        seed(sev, [(0, 0, None), (1, -500, None)], game_id=2, time_class="blitz")
        assert rows(sev, game_id=2) == {}
