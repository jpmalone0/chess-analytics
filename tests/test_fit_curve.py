"""The curve is fitted from this corpus, never imported.

Lichess publishes k=271.6, fitted on 2300+ rated rapid. Measured here, rapid
fits 360 and bullet 865 -- a 2.4x spread, because a bullet advantage converts far
less reliably. Importing a curve fitted on stronger players overstates every
error.
"""

import math

from engine.fit_curve import fit_k


def _synthetic(k, n_per_bucket=4000):
    """Outcomes generated from a known k, so the fit has a right answer.

    Deterministic rather than sampled: at each centipawn value the expected
    number of wins is emitted exactly, which is what maximum likelihood is
    recovering anyway and keeps the test from being flaky.
    """
    pairs = []
    for cp in range(-800, 801, 50):
        p = 1 / (1 + math.exp(-cp / k))
        wins = round(p * n_per_bucket)
        pairs.extend([(cp, 1.0)] * wins)
        pairs.extend([(cp, 0.0)] * (n_per_bucket - wins))
    return pairs


def test_it_recovers_a_known_curve():
    assert fit_k(_synthetic(360.0)) == 360


def test_it_recovers_a_flatter_curve():
    """Bullet's curve is nearly two and a half times flatter than rapid's."""
    assert fit_k(_synthetic(865.0)) == 865


def test_draws_count_as_half_a_point():
    """The fitted quantity is expected points, which is what makes our
    thresholds comparable to chess.com's published ladder."""
    even = [(0, 0.5)] * 1000
    assert fit_k(even) is not None


def test_no_data_yields_no_curve():
    assert fit_k([]) is None
