from app import baselines
from tests.conftest import make_game, make_player, seed_band


def test_population_counts_excludes_searched_player(db):
    seed_band(db, 1500, n_players=5)
    target = make_player(db, "target")
    other = make_player(db, "other")
    for i in range(3):
        make_game(db, target, other, white_elo=1550, black_elo=1550, end_time=1700001000 + i)
    db.commit()

    with_target = baselines.population_counts(
        db, elo_lo=1500, elo_hi=1599, time_control="600", exclude_player_id=-1)
    without_target = baselines.population_counts(
        db, elo_lo=1500, elo_hi=1599, time_control="600",
        exclude_player_id=target.player_id)

    assert with_target["n_games"] > without_target["n_games"]
    assert with_target["n_players"] == without_target["n_players"] + 1


def test_population_ignores_date_filters(db):
    """Spec: Elo, time control, colour and opening mirror the player's filters;
    the date range deliberately does not. There is no date parameter to pass —
    this test exists to catch anyone adding one."""
    import inspect

    sig = inspect.signature(baselines.population_counts)
    assert "start_date" not in sig.parameters
    assert "end_date" not in sig.parameters


def test_population_counts_caps_per_player(db):
    hog = make_player(db, "hog")
    foil = make_player(db, "foil")
    for i in range(baselines.PER_PLAYER_CAP + 40):
        make_game(db, hog, foil, white_elo=1550, black_elo=900, end_time=1700000000 + i)
    db.commit()

    counts = baselines.population_counts(
        db, elo_lo=1500, elo_hi=1599, time_control="600", exclude_player_id=-1)

    assert counts["n_players"] == 1
    assert counts["n_games"] == baselines.PER_PLAYER_CAP
