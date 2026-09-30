"""Cost estimates shown before a batch starts.

An estimate that is several times optimistic makes a healthy run look stalled,
which is how a real deadlock once went unnoticed. These pin the estimate to
what was measured rather than to a model of it.
"""

import pytest

from engine.cli import estimated_minutes


def test_three_candidates_cost_the_measured_slowdown():
    """Measured 2026-09-30 on 10 rapid games (795 positions) at depth 14:
    63.8 ms per position for one line, 333.8 ms for three."""
    one = estimated_minutes(100, 7, 14, multipv=1)
    three = estimated_minutes(100, 7, 14, multipv=3)

    assert three / one == pytest.approx(5.23)


def test_the_default_estimate_is_for_three_candidates():
    """The app's population button calls this without saying how many lines."""
    assert estimated_minutes(100, 7, 14) == estimated_minutes(100, 7, 14, multipv=3)
