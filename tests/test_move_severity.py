"""The win-probability curve and the tier ladder.

Severity is a change in expected points, not in centipawns. Every threshold in
this feature lives in the view, so these tests are the only place the ladder is
pinned.
"""

import math

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError, OperationalError

from engine.models import (
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


def test_wp_curve_ddl_matches_the_model():
    """The DDL and the WpCurve model describe the same table by hand, and the
    two can drift. Insert-and-read-back exercises the shape; the constraint
    checks exercise the part most likely to silently fall out of sync.
    """
    eng = create_engine("sqlite://")
    with eng.begin() as conn:
        conn.execute(text(WP_CURVE_DDL))
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

    with eng.begin() as conn:
        with pytest.raises(IntegrityError):
            conn.execute(
                text("INSERT INTO wp_curve (time_class, k, n) VALUES ('bullet', NULL, 5)")
            )

    with eng.begin() as conn:
        with pytest.raises(IntegrityError):
            conn.execute(
                text("INSERT INTO wp_curve (time_class, k, n) VALUES ('bullet', 865.0, NULL)")
            )
