"""Games belong to the viewer's calendar day, not the PGN's UTC date.

chess.com dates a game in UTC, so a game finished at 9pm in New York carries
tomorrow's date. Every place that shows or groups games by day uses the game's
end time in the viewer's zone instead.
"""

from datetime import date, datetime, timezone

from app import crud
from tests.conftest import make_game, make_player

NY = "America/New_York"
OCT5 = date(2026, 10, 5)


def evening(hour_utc: int, minute: int = 0) -> int:
    """An end time on Oct 6 UTC that is still the evening of Oct 5 in New York."""
    return int(datetime(2026, 10, 6, hour_utc, minute, tzinfo=timezone.utc).timestamp())


def two_evening_games(db):
    """A loss then a win, both on the evening of Oct 5 in New York."""
    me, a = make_player(db, "me"), make_player(db, "a")
    make_game(db, me, a, 1900, 1900, result="0-1",
              end_time=evening(0, 55), date_played=date(2026, 10, 6))
    make_game(db, me, a, 1900, 1900, result="1-0",
              end_time=evening(1, 11), date_played=date(2026, 10, 6))
    db.commit()
    return me


def test_the_game_list_shows_the_local_day(db):
    me = two_evening_games(db)
    games = crud.get_games_for_player(db, me.player_id, tz=NY)
    assert {g["date_played"] for g in games} == {OCT5}


def test_the_rolling_win_rate_groups_by_local_day(db):
    me = two_evening_games(db)
    rows = crud.winrate_by_color_rolling(db, me.player_id, end_date=OCT5, tz=NY)
    assert [r["date"] for r in rows] == ["2026-10-05"]


def test_streaks_keep_tonight_s_games_when_today_ends_the_range(db):
    me = two_evening_games(db)
    out = crud.streak_reaction(db, me.player_id, end_date=OCT5, tz=NY)
    after_one_loss = out["after_loss"][0]
    assert after_one_loss["wins"] == 1


def test_a_game_without_an_end_time_keeps_its_stored_date(db):
    me, a = make_player(db, "me"), make_player(db, "a")
    g = make_game(db, me, a, 1900, 1900, end_time=None, date_played=date(2026, 10, 6))
    g.end_time = None
    db.commit()
    games = crud.get_games_for_player(db, me.player_id, tz=NY)
    assert games[0]["date_played"] == date(2026, 10, 6)
