"""The population sampler, the pooled band rate, and the job runner.

Nothing here runs Stockfish or opens the real sidecar: the runner's analyze
step is a fake that seeds evaluations, and every sidecar is a per-test file
under the path _isolate_engine_db has already redirected.
"""

from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from app import move_quality as mq
from app.database import get_db
from app.main import app, get_population_runner
from engine import db as engine_db
from engine.analyze import Summary
from engine.models import PopulationJob
from engine.population import POPULATION_PER_PLAYER_CAP, JobRunner, sample_band
from engine.views import (
    GAME_MOVE_QUALITY_VIEW,
    MOVE_EVALS_VIEW,
    MOVE_QUALITY_VIEW,
    MOVE_SEVERITY_VIEW,
)
from tests.conftest import build_sidecar, make_game, make_player, seed_evals

# White gives up 0.203 at ply 1, a blunder; Black holds.
WHITE_BLUNDERS = [(0, 0), (1, -310), (2, -310)]


@pytest.fixture
def sidecar():
    eng = build_sidecar(
        MOVE_EVALS_VIEW, MOVE_SEVERITY_VIEW, MOVE_QUALITY_VIEW, GAME_MOVE_QUALITY_VIEW,
        url=engine_db.ENGINE_DATABASE_URL,
    )
    PopulationJob.__table__.create(bind=eng)
    yield eng
    eng.dispose()


@pytest.fixture
def runner(db, sidecar):
    @contextmanager
    def connect():
        c = db.connection()
        engine_db.attach_engine_db(c)
        yield c

    def analyze(ids, run_id, progress):
        s = Summary(run_id=run_id, requested=len(ids))
        for n, gid in enumerate(ids, start=1):
            seed_evals(sidecar, WHITE_BLUNDERS, game_id=gid, run_id=run_id)
            s.complete += 1
            progress(n, len(ids), s)
        return s

    r = JobRunner(
        sessions=sessionmaker(bind=sidecar), connect=connect,
        run_id=lambda: 1, analyze=analyze,
    )
    r._ensure_thread = lambda: None  # tests drain with run_pending()
    return r


def _sample(db, **kw):
    """A fresh connection per call: db.commit() closes the session's last one."""
    conn = db.connection()
    engine_db.attach_engine_db(conn)
    args = dict(time_class="rapid", elo_lo=1800, elo_hi=1899,
                exclude_player_id=None, run_id=1, limit=100)
    return sample_band(conn, **{**args, **kw})


# ── Sampler ───────────────────────────────────────────────

def test_only_sides_inside_the_band_select_a_game(db, sidecar):
    a, b, c = (make_player(db, n) for n in "abc")
    inside = make_game(db, a, b, 1850, 2300)
    make_game(db, b, c, 2300, 2300)
    make_game(db, a, c, 1850, 1850, time_class="bullet")
    db.commit()
    assert _sample(db) == [inside.game_id]


def test_every_game_the_viewed_player_is_in_is_excluded(db, sidecar):
    me, a, b = (make_player(db, n) for n in ("me", "a", "b"))
    make_game(db, a, me, 1850, 1850)  # a is in band, but it is my game
    other = make_game(db, a, b, 1850, 1850)
    db.commit()
    assert _sample(db, exclude_player_id=me.player_id) == [other.game_id]


def test_no_player_contributes_more_than_the_cap(db, sidecar):
    heavy = make_player(db, "heavy")
    for i in range(POPULATION_PER_PLAYER_CAP + 3):
        make_game(db, heavy, make_player(db, f"o{i}"), 1850, 2500)
    db.commit()
    assert len(_sample(db)) == POPULATION_PER_PLAYER_CAP


def test_pressing_again_extends_the_sample_rather_than_redrawing_it(db, sidecar):
    players = [make_player(db, f"p{i}") for i in range(6)]
    for w, b in zip(players[::2], players[1::2]):
        make_game(db, w, b, 1850, 1850)
    db.commit()

    first = _sample(db, limit=2)
    for gid in first:
        seed_evals(sidecar, WHITE_BLUNDERS, game_id=gid)
    second = _sample(db, limit=2)

    assert len(first) == 2 and len(second) == 1
    assert not set(first) & set(second)
    assert _sample(db, limit=3) == second  # deterministic


def test_the_cap_counts_games_already_analyzed(db, sidecar):
    heavy = make_player(db, "heavy")
    games = [make_game(db, heavy, make_player(db, f"o{i}"), 1850, 2500)
             for i in range(POPULATION_PER_PLAYER_CAP + 2)]
    db.commit()
    for gid in _sample(db):
        seed_evals(sidecar, WHITE_BLUNDERS, game_id=gid)
    assert _sample(db) == []
    assert len(games) > POPULATION_PER_PLAYER_CAP


# ── Pooled rate ───────────────────────────────────────────

BAND = {"time_class": "rapid", "elo_lo": 1800, "elo_hi": 1899, "source": "selected"}


def test_the_rate_counts_only_the_in_band_side(db, sidecar):
    me, a, b = (make_player(db, n) for n in ("me", "a", "b"))
    g1 = make_game(db, a, b, 1850, 2300)  # White in band, and White blunders
    g2 = make_game(db, b, a, 2300, 1850)  # Black in band; White blunders
    db.commit()
    seed_evals(sidecar, WHITE_BLUNDERS, game_id=g1.game_id)
    seed_evals(sidecar, WHITE_BLUNDERS, game_id=g2.game_id)

    t = mq.band_move_quality(db, BAND, exclude_player_id=me.player_id)["totals"]
    assert t["n_games"] == 2
    assert t["moves_scored"] == 2  # a's move in each game, never b's
    assert t["blunders"] == 1


def test_the_rate_leaves_out_the_viewed_players_games(db, sidecar):
    me, a = make_player(db, "me"), make_player(db, "a")
    g = make_game(db, a, me, 1850, 1850)
    db.commit()
    seed_evals(sidecar, WHITE_BLUNDERS, game_id=g.game_id)
    out = mq.band_move_quality(db, BAND, exclude_player_id=me.player_id)
    assert out["totals"]["n_games"] == 0
    assert out["viable"] is False


def test_a_class_with_no_fitted_curve_says_so(db, sidecar):
    me = make_player(db, "me")
    db.commit()
    out = mq.band_move_quality(
        db, {**BAND, "time_class": "blitz"}, exclude_player_id=me.player_id)
    assert out["curve_fitted"] is False


# ── Runner ────────────────────────────────────────────────

def _band(**kw):
    return dict(time_class="rapid", elo_lo=1800, elo_hi=1899,
                target_games=10, exclude_player_id=None, **kw)


def test_a_second_press_on_a_band_in_flight_returns_the_same_job(runner):
    first, created = runner.enqueue(**_band())
    again, created_again = runner.enqueue(**_band())
    assert created and not created_again
    assert again["job_id"] == first["job_id"]


def test_a_job_samples_analyzes_and_records_progress(db, runner):
    a, b = make_player(db, "a"), make_player(db, "b")
    make_game(db, a, b, 1850, 1850)
    make_game(db, b, a, 1850, 1850)
    db.commit()

    job, _ = runner.enqueue(**_band())
    runner.run_pending()

    done = runner.jobs()[0]
    assert done["job_id"] == job["job_id"]
    assert done["status"] == "complete"
    assert done["games_total"] == 2 and done["games_done"] == 2
    assert runner.active_job("rapid", 1800, 1899) is None


def test_a_failing_job_records_its_error_and_frees_the_band(runner):
    def boom(ids, run_id, progress):
        raise RuntimeError("stockfish went away")
    runner._analyze = boom

    runner.enqueue(**_band())
    runner.run_pending()
    job = runner.jobs()[0]
    assert job["status"] == "failed"
    assert "stockfish went away" in job["error"]
    assert runner.enqueue(**_band())[1]  # the band can be pressed again


def test_a_restart_fails_whatever_was_left_in_flight(runner):
    runner.enqueue(**_band())
    assert runner.recover_interrupted() == 1
    assert runner.jobs()[0]["status"] == "failed"


# ── Routes ────────────────────────────────────────────────

@pytest.fixture
def client(db, runner):
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_population_runner] = lambda: runner
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_pressing_the_button_pins_the_selected_band_and_queues_it(client, db, runner):
    me, a, b = (make_player(db, n) for n in ("me", "a", "b"))
    make_game(db, me, a, 1500, 1500)
    make_game(db, a, b, 1850, 1850)
    db.commit()

    r = client.post("/api/players/me/analytics/move-quality/population"
                    "?time_class=rapid&elo_band=1800&games=50").json()
    assert r["created"]
    assert (r["job"]["elo_lo"], r["job"]["elo_hi"]) == (1800, 1899)
    assert r["job"]["target_games"] == 50

    base = client.get("/api/players/me/analytics/move-quality/baseline"
                      "?time_class=rapid&elo_band=1800").json()
    assert base["job"]["job_id"] == r["job"]["job_id"]

    runner.run_pending()
    base = client.get("/api/players/me/analytics/move-quality/baseline"
                      "?time_class=rapid&elo_band=1800").json()
    assert base["job"] is None
    assert base["totals"]["n_games"] == 1


def test_a_derived_band_comes_from_the_median_within_the_class(client, db):
    me, a = make_player(db, "me"), make_player(db, "a")
    make_game(db, me, a, 1920, 1900)
    make_game(db, me, a, 1100, 1100, time_class="bullet")
    db.commit()
    band = client.get("/api/players/me/analytics/move-quality/baseline"
                      "?time_class=rapid").json()["band"]
    assert (band["elo_lo"], band["source"]) == (1900, "derived")
