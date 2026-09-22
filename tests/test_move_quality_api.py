"""The two read routes.

Engine coverage is 0.6% of the corpus, so the empty case is the common case and
is tested first.

The sidecar these tests attach is a real file, not the in-memory one the view
tests use: the routes reach it through attach_engine_db, which ATTACHes a path.
The autouse _isolate_engine_db fixture has already pointed ENGINE_DATABASE_URL
at a per-test temporary file, so nothing here can touch the real sidecar.
"""

import pytest
from fastapi.testclient import TestClient

from app.database import get_db
from app.main import app
from engine import db as engine_db
from engine.views import (
    GAME_MOVE_QUALITY_VIEW,
    MOVE_EVALS_VIEW,
    MOVE_QUALITY_VIEW,
    MOVE_SEVERITY_VIEW,
)
from tests.conftest import build_sidecar, make_game, make_player, seed_evals


@pytest.fixture
def client(db):
    app.dependency_overrides[get_db] = lambda: db
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def sidecar():
    """The four views the routes read, in a file the app can ATTACH.

    Disposed rather than left open: the routes read the same file through the
    session's own connection, and an idle engine holding it open is one more
    thing that can hold a lock when a test does not expect one.
    """
    eng = build_sidecar(
        MOVE_EVALS_VIEW, MOVE_SEVERITY_VIEW, MOVE_QUALITY_VIEW, GAME_MOVE_QUALITY_VIEW,
        url=engine_db.ENGINE_DATABASE_URL,
    )
    yield eng
    eng.dispose()


def test_a_player_with_no_analyzed_games_gets_an_empty_result(client, db, sidecar):
    """Coverage is 0.6% of the corpus. This is the common case, not the edge."""
    me = make_player(db, "me")
    them = make_player(db, "them")
    make_game(db, me, them, 1900, 1900)
    db.commit()

    r = client.get("/api/players/me/analytics/move-quality")
    assert r.status_code == 200
    body = r.json()
    assert body["games"] == []
    assert body["totals"]["games_analyzed"] == 0


def test_an_unknown_player_is_a_404(client, db):
    assert client.get("/api/players/nobody/analytics/move-quality").status_code == 404


def test_counts_come_back_for_the_searched_players_side_only(client, db, sidecar):
    me = make_player(db, "me")
    them = make_player(db, "them")
    g = make_game(db, me, them, 1900, 1900)
    db.commit()
    # ply 1 is White, which is `me`: 0.5 -> 0.297, a 0.203 blunder. ply 2 is
    # Black holding the same evaluation, so Black loses nothing.
    seed_evals(sidecar, [(0, 0), (1, -310), (2, -310)], game_id=g.game_id)

    body = client.get("/api/players/me/analytics/move-quality").json()
    assert body["totals"]["games_analyzed"] == 1
    assert body["totals"]["blunders"] == 1
    assert body["games"][0]["blunders"] == 1
    assert body["games"][0]["color"] == "white"


def test_the_opponents_side_is_counted_against_the_opponent(client, db, sidecar):
    """The same game, searched from the other seat, must not carry `me`'s errors."""
    me = make_player(db, "me")
    them = make_player(db, "them")
    g = make_game(db, me, them, 1900, 1900)
    db.commit()
    seed_evals(sidecar, [(0, 0), (1, -310), (2, -310)], game_id=g.game_id)

    body = client.get("/api/players/them/analytics/move-quality").json()
    assert body["games"][0]["color"] == "black"
    assert body["totals"]["blunders"] == 0


def test_the_drill_list_returns_only_flagged_moves(client, db, sidecar):
    me = make_player(db, "me")
    them = make_player(db, "them")
    g = make_game(db, me, them, 1900, 1900)
    db.commit()
    # ply 1: White 0.5 -> 0.297, a 0.203 blunder. ply 2: Black hands the whole
    # 0.203 straight back, which is a blunder of Black's own and also a Miss.
    # ply 3: White holds level, so it is scored and not flagged -- which is the
    # row this test exists to see omitted.
    seed_evals(sidecar, [(0, 0), (1, -310), (2, 0), (3, 0)], game_id=g.game_id)

    body = client.get(f"/api/games/{g.game_id}/move-quality").json()
    assert [m["ply"] for m in body["moves"]] == [1, 2]
    assert body["moves"][0]["tier"] == "blunder"
    assert body["moves"][0]["is_miss"] == 0
    assert body["moves"][0]["move_san"] == "e4"
    assert body["moves"][0]["wp_before"] == pytest.approx(0.5)
    assert body["moves"][1]["is_miss"] == 1


def test_the_drill_list_carries_the_clock(client, db, sidecar):
    """Time spent is the most interesting column in the table and it is free."""
    me = make_player(db, "me")
    them = make_player(db, "them")
    g = make_game(db, me, them, 1900, 1900, white_clocks=[300.0, 290.0, 280.0])
    db.commit()
    seed_evals(sidecar, [(0, 0), (1, -310)], game_id=g.game_id)

    m = client.get(f"/api/games/{g.game_id}/move-quality").json()["moves"][0]
    assert m["clock_seconds"] == 300.0


def test_an_unanalyzed_game_drills_to_nothing(client, db, sidecar):
    """A game with no coverage row is the normal case, not an error."""
    me = make_player(db, "me")
    them = make_player(db, "them")
    g = make_game(db, me, them, 1900, 1900)
    db.commit()

    r = client.get(f"/api/games/{g.game_id}/move-quality")
    assert r.status_code == 200
    assert r.json() == {"game_id": g.game_id, "moves": []}


def test_only_the_newest_complete_run_is_counted(client, db, sidecar):
    """Two runs over one game must not double it."""
    me = make_player(db, "me")
    them = make_player(db, "them")
    g = make_game(db, me, them, 1900, 1900)
    db.commit()
    seed_evals(sidecar, [(0, 0), (1, -310), (2, -310)], game_id=g.game_id, run_id=1)
    seed_evals(sidecar, [(0, 0), (1, 0), (2, 0)], game_id=g.game_id, run_id=2)

    body = client.get("/api/players/me/analytics/move-quality").json()
    assert body["totals"]["games_analyzed"] == 1
    # Run 2 saw a clean game, and it is the newer search.
    assert body["totals"]["blunders"] == 0

    drill = client.get(f"/api/games/{g.game_id}/move-quality").json()
    assert drill["moves"] == []


def test_the_filter_bar_reaches_the_counts(client, db, sidecar):
    """time_class is the filter the section is used through; if it does not
    reach this query the panel silently describes a different set of games than
    the rest of the page."""
    me = make_player(db, "me")
    them = make_player(db, "them")
    g = make_game(db, me, them, 1900, 1900, time_class="rapid")
    db.commit()
    seed_evals(sidecar, [(0, 0), (1, -310), (2, -310)], game_id=g.game_id)

    assert client.get(
        "/api/players/me/analytics/move-quality?time_class=rapid"
    ).json()["totals"]["games_analyzed"] == 1
    assert client.get(
        "/api/players/me/analytics/move-quality?time_class=blitz"
    ).json()["totals"]["games_analyzed"] == 0
