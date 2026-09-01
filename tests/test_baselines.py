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


def test_derived_band_widens_when_thin(db):
    # 1500 band alone is far below MIN_GAMES; neighbours make the widened band viable.
    seed_band(db, 1500, n_players=10, games_each=2)
    seed_band(db, 1400, n_players=200, games_each=2)
    seed_band(db, 1600, n_players=200, games_each=2)
    target = make_player(db, "target")
    foil = make_player(db, "foil")
    for i in range(10):
        make_game(db, target, foil, white_elo=1550, black_elo=1550, end_time=1700009000 + i)
    db.commit()

    band = baselines.resolve_band(
        db, player_id=target.player_id, time_class="rapid", selected_band=None)

    assert band is not None
    assert band["widened"] is True
    assert band["elo_lo"] < 1500 and band["elo_hi"] > 1599
    assert band["source"] == "derived"


def test_selected_band_never_widens(db):
    seed_band(db, 1500, n_players=10, games_each=2)   # below the floor
    seed_band(db, 1400, n_players=200, games_each=2)  # would rescue it if widened
    target = make_player(db, "target")
    foil = make_player(db, "foil")
    make_game(db, target, foil, white_elo=1550, black_elo=1550, end_time=1700009999)
    db.commit()

    band = baselines.resolve_band(
        db, player_id=target.player_id, time_class="rapid", selected_band=1500)

    assert band is None


def test_sparse_time_control_falls_back_to_time_class(db):
    # 900+10 is too thin to stand alone; the rapid class as a whole is not.
    seed_band(db, 1500, n_players=300, games_each=2, time_control="600", time_class="rapid")
    seed_band(db, 1500, n_players=5, games_each=1, time_control="900+10", time_class="rapid")
    target = make_player(db, "target")
    foil = make_player(db, "foil")
    for i in range(10):
        make_game(db, target, foil, white_elo=1550, black_elo=1550,
                  time_control="900+10", time_class="rapid", end_time=1700009000 + i)
    db.commit()

    band = baselines.resolve_band(
        db, player_id=target.player_id, time_class="rapid", selected_band=None)

    assert band is not None
    assert band["tc_fallback"] is True
    assert band["time_control"] is None
    assert band["widened"] is False   # time control blurred first, band stayed put


def test_dominant_time_control_is_the_modal_one(db):
    target = make_player(db, "target")
    foil = make_player(db, "foil")
    for i in range(7):
        make_game(db, target, foil, white_elo=1550, black_elo=1550,
                  time_control="600", end_time=1700000000 + i)
    for i in range(2):
        make_game(db, target, foil, white_elo=1550, black_elo=1550,
                  time_control="900+10", end_time=1700100000 + i)
    db.commit()

    assert baselines.dominant_time_control(db, target.player_id, time_class="rapid") == "600"


def test_available_bands_omits_bands_below_floor(db):
    seed_band(db, 1500, n_players=300, games_each=2)  # viable
    seed_band(db, 1700, n_players=5, games_each=1)    # too thin
    target = make_player(db, "target")
    db.commit()

    bands = baselines.available_bands(
        db, player_id=target.player_id, time_class="rapid", time_control="600")

    los = [b["elo_lo"] for b in bands]
    assert 1500 in los
    assert 1700 not in los
    entry = next(b for b in bands if b["elo_lo"] == 1500)
    assert entry["elo_hi"] == 1599
    assert entry["n_players"] >= baselines.MIN_PLAYERS


def test_move_time_baseline_reflects_population_not_target(db):
    # Population thinks 8s per move; the target thinks 1s. The baseline must
    # report the population's number, unaffected by the target's games.
    seed_band(db, 1500, n_players=300, games_each=2, move_time=8.0)
    target = make_player(db, "target")
    foil = make_player(db, "foil")
    for i in range(50):
        # foil sits far below the band: at 1550 it would be a legitimate
        # population member and its default 5s moves would drag the mean.
        make_game(db, target, foil, white_elo=1550, black_elo=100,
                  end_time=1700009000 + i, white_move_times=[1.0] * 3)
    db.commit()

    band = baselines.resolve_band(db, target.player_id, time_class="rapid")
    result = baselines.move_time_baseline(db, target.player_id, band, time_class="rapid")

    assert result["mean"] == 8.0
    assert result["total_moves"] > 0
    assert len(result["buckets"]) == 7
    assert result["by_move_number"][0]["avg_seconds"] == 8.0


def test_game_length_baseline_computes_population_winrate(db):
    # Every seeded game is a white win at 40 moves, so the 31-40 bucket should
    # be 100% and the population's win rate should not include the target.
    seed_band(db, 1500, n_players=300, games_each=2, total_moves=40, result="1-0")
    target = make_player(db, "target")
    db.commit()

    band = baselines.resolve_band(db, target.player_id, time_class="rapid",
                                  selected_band=1500)
    result = baselines.game_length_baseline(db, target.player_id, band, time_class="rapid")

    bucket = next(b for b in result if b["bucket"] == "31–40")
    assert bucket["total_games"] > 0
    assert bucket["win_rate"] == 100.0


def test_clock_advantage_baseline_buckets_by_clock_difference(db):
    """The only test that exercises clock data, so it builds games directly
    rather than through seed_band: 40 players x 13 games = 520, clearing both
    the player and game floors. In every game the tracked player runs ~60s
    ahead and wins, so far_ahead must be the only populated bucket."""
    foil = make_player(db, "clock-foil")
    for i in range(40):
        p = make_player(db, f"clock-p{i}")
        for _ in range(13):
            make_game(
                db, p, foil, white_elo=1550, black_elo=100,
                white_clocks=[300.0, 290.0, 280.0],
                black_clocks=[240.0, 230.0, 220.0],
            )
    db.commit()

    band = baselines.resolve_band(db, foil.player_id, time_class="rapid",
                                  selected_band=1500)
    result = baselines.clock_advantage_baseline(db, foil.player_id, band,
                                                time_class="rapid")

    far_ahead = next(b for b in result if b["clock_bucket"] == "far_ahead")
    assert far_ahead["total_games"] == 520
    assert far_ahead["win_rate"] == 100.0
    assert all(b["total_games"] == 0 for b in result if b["clock_bucket"] != "far_ahead")
