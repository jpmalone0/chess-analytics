"""The win-probability curve and the tier ladder.

Severity is a change in expected points, not in centipawns. Every threshold in
this feature lives in the view, so these tests are the only place the ladder is
pinned.
"""

import math

from sqlalchemy import create_engine, text

from engine.models import (
    WP_CURVE_DDL,
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


def test_wp_curve_ddl_creates_table():
    eng = create_engine("sqlite://")
    with eng.begin() as conn:
        conn.execute(text(WP_CURVE_DDL))
        row = conn.execute(
            text("SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'wp_curve'")
        ).fetchone()
    assert row is not None
