"""The scorecard route: loading the window and shaping the payload."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app import scorecard as scorecard_module
from app.database import get_db
from app.main import app
from app.models import Move
from engine import db as engine_db
from engine.scorecard import DIMENSIONS
from tests.conftest import build_sidecar, make_game, make_player, seed_evals

POSITION_PV_DDL = """
CREATE TABLE position_pv (
    run_id INTEGER NOT NULL, game_id INTEGER NOT NULL, ply INTEGER NOT NULL,
    rank INTEGER NOT NULL, move_uci VARCHAR(6) NOT NULL, cp INTEGER,
    mate_in INTEGER, line TEXT,
    PRIMARY KEY (run_id, game_id, ply, rank)
)
"""


@pytest.fixture(autouse=True)
def _fresh_cache():
    """Every test's first game is game 1 under run 1, so the facts cache would
    otherwise hand one test the previous test's game."""
    scorecard_module._FACTS.clear()


@pytest.fixture
def client(db):
    app.dependency_overrides[get_db] = lambda: db
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def sidecar():
    eng = build_sidecar(url=engine_db.ENGINE_DATABASE_URL)
    with eng.begin() as conn:
        conn.execute(text(POSITION_PV_DDL))
    yield eng
    eng.dispose()


def legal_game(db, white, black, sans, **kwargs):
    """make_game writes "e4" for every ply; give it a legal sequence instead."""
    n = len(sans)
    g = make_game(db, white, black, 1900, 1900,
                  white_move_times=[5.0] * ((n + 1) // 2),
                  black_move_times=[5.0] * (n // 2), **kwargs)
    for m in db.query(Move).filter(Move.game_id == g.game_id):
        m.move_san = sans[m.ply - 1]
    db.commit()
    return g


def test_an_unknown_player_is_a_404(client, db):
    assert client.get("/api/players/nobody/analytics/scorecard").status_code == 404


def test_no_analyzed_games_is_an_empty_scorecard(client, db, sidecar):
    me, them = make_player(db, "me"), make_player(db, "them")
    make_game(db, me, them, 1900, 1900)
    db.commit()

    body = client.get("/api/players/me/analytics/scorecard").json()
    assert body["games"] == 0
    assert body["small_sample"] is True


def test_an_analyzed_game_gives_eight_rows_from_the_players_seat(client, db, sidecar):
    me, them = make_player(db, "me"), make_player(db, "them")
    # `me` is Black here, so their side is the one that lost 100cp.
    g = legal_game(db, them, me, ["e4", "e5"], result="1-0")
    seed_evals(sidecar, [(0, 0), (1, 0), (2, 100)], game_id=g.game_id)

    body = client.get("/api/players/me/analytics/scorecard").json()
    assert body["games"] == 1
    rows = {r["key"]: r for r in body["rows"]}
    assert list(rows) == [d.key for d in DIMENSIONS]
    assert rows["opening"]["you"] < 0
    # The band is other players' games only; with none analyzed there is no
    # comparison, and the opponent in this game does not stand in for one.
    assert rows["opening"]["band"] is None
    assert body["band_games"] == 0
    assert body["own_avg_elo"] == 1900


def test_an_unreplayable_game_is_skipped_not_fatal(client, db, sidecar):
    me, them = make_player(db, "me"), make_player(db, "them")
    g = make_game(db, me, them, 1900, 1900)   # every ply is "e4"
    db.commit()
    seed_evals(sidecar, [(p, 0) for p in range(7)], game_id=g.game_id)

    r = client.get("/api/players/me/analytics/scorecard")
    assert r.status_code == 200
    assert r.json()["games"] == 0


def test_clocks_reach_the_scorecard(db, sidecar):
    """Time management reads each move's clock."""
    me, them = make_player(db, "me"), make_player(db, "them")
    g = make_game(db, me, them, 1900, 1900, white_move_times=[5.0, 5.0],
                  black_move_times=[5.0], white_clocks=[595.0, 590.0],
                  black_clocks=[595.0])
    for m in db.query(Move).filter(Move.game_id == g.game_id):
        m.move_san = ["e4", "e5", "Nf3"][m.ply - 1]
    db.commit()
    seed_evals(sidecar, [(p, 0) for p in range(4)], game_id=g.game_id)
    engine_db.attach_engine_db(db.connection())

    inputs = scorecard_module._load_inputs(db, [(g.game_id, 1)])
    assert inputs[g.game_id].clocks == [595.0, 595.0, 590.0]


def test_the_scorecard_compares_at_your_average_by_default(client, db, sidecar):
    me, them = make_player(db, "me"), make_player(db, "them")
    g = legal_game(db, them, me, ["e4", "e5"], result="1-0")
    seed_evals(sidecar, [(0, 0), (1, 0), (2, 100)], game_id=g.game_id)

    body = client.get("/api/players/me/analytics/scorecard").json()
    assert body["compare_rating"] == 1900
    assert body["compare_source"] == "average"


def test_a_compare_to_band_moves_the_comparison_to_its_middle(client, db, sidecar):
    me, them = make_player(db, "me"), make_player(db, "them")
    g = legal_game(db, them, me, ["e4", "e5"], result="1-0")
    seed_evals(sidecar, [(0, 0), (1, 0), (2, 100)], game_id=g.game_id)

    body = client.get("/api/players/me/analytics/scorecard?elo_band=2200").json()
    assert body["compare_rating"] == 2250
    assert body["compare_source"] == "selected"
    assert body["own_avg_elo"] == 1900


def test_all_players_compares_at_your_average(client, db, sidecar):
    me, them = make_player(db, "me"), make_player(db, "them")
    g = legal_game(db, them, me, ["e4", "e5"], result="1-0")
    seed_evals(sidecar, [(0, 0), (1, 0), (2, 100)], game_id=g.game_id)

    body = client.get("/api/players/me/analytics/scorecard?elo_band=all").json()
    assert body["compare_rating"] == 1900
