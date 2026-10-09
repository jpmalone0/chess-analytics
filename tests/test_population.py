"""The population sampler, the pooled band rate, and the job runner.

Nothing here runs Stockfish or opens the real sidecar: the runner's analyze
step is a fake that seeds evaluations, and every sidecar is a per-test file
under the path _isolate_engine_db has already redirected.
"""

from contextlib import contextmanager
from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from app import move_quality as mq
from app.database import get_db
from app.main import app, get_population_runner
from engine import db as engine_db
from engine.analyze import Summary
from engine.models import PopulationJob, PositionPV
from engine.population import (
    POPULATION_PER_PLAYER_CAP,
    JobRunner,
    sample_band,
    sample_player,
)
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
    # The baseline reads the Scorecard's players line, which replays games
    # with their engine lines.
    PositionPV.__table__.create(bind=eng)
    yield eng
    eng.dispose()


@pytest.fixture
def runner(db, sidecar):
    @contextmanager
    def connect():
        c = db.connection()
        engine_db.attach_engine_db(c)
        yield c

    def analyze(ids, run_id, progress, should_stop=lambda: False):
        s = Summary(run_id=run_id, requested=len(ids))
        for n, gid in enumerate(ids, start=1):
            if should_stop():
                break
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
    def boom(ids, run_id, progress, should_stop):
        raise RuntimeError("stockfish went away")
    runner._analyze = boom

    runner.enqueue(**_band())
    runner.run_pending()
    job = runner.jobs()[0]
    assert job["status"] == "failed"
    assert "stockfish went away" in job["error"]
    assert runner.enqueue(**_band())[1]  # the band can be pressed again


def test_a_queued_job_can_be_cancelled_before_it_runs(db, runner):
    a, b = make_player(db, "a"), make_player(db, "b")
    make_game(db, a, b, 1850, 1850)
    db.commit()
    job, _ = runner.enqueue(**_band())
    assert runner.cancel(job["job_id"])["status"] == "cancelled"
    runner.run_pending()
    assert runner.jobs()[0]["status"] == "cancelled"
    assert runner.jobs()[0]["games_done"] == 0


def test_a_running_job_stops_and_keeps_what_finished(db, runner, sidecar):
    players = [make_player(db, f"p{i}") for i in range(6)]
    for w, b in zip(players[::2], players[1::2], strict=True):
        make_game(db, w, b, 1850, 1850)
    db.commit()
    job, _ = runner.enqueue(**{**_band(), "target_games": 3})
    real = runner._analyze

    def cancel_after_first(ids, run_id, progress, should_stop):
        def progress_then_cancel(done, total, summary):
            progress(done, total, summary)
            runner.cancel(job["job_id"])
        return real(ids, run_id, progress_then_cancel, should_stop)

    runner._analyze = cancel_after_first
    runner.run_pending()
    done = runner.jobs()[0]
    assert (done["status"], done["games_done"], done["error"]) == ("cancelled", 1, None)


def test_cancelling_an_unknown_or_finished_job_changes_nothing(runner):
    assert runner.cancel(999) is None


def test_a_restart_finishes_a_cancel_instead_of_failing_it(runner):
    job, _ = runner.enqueue(**_band())
    with runner._sessions() as s:
        s.get(PopulationJob, job["job_id"]).status = "cancelling"
        s.commit()
    runner.recover_interrupted()
    assert runner.jobs()[0]["status"] == "cancelled"


def test_the_cancel_route(client, db, runner):
    me = make_player(db, "me")
    db.commit()
    job, _ = runner.enqueue(**{**_band(), "exclude_player_id": me.player_id})
    r = client.post(f"/api/population/jobs/{job['job_id']}/cancel")
    assert r.status_code == 200 and r.json()["job"]["status"] == "cancelled"
    assert client.post("/api/population/jobs/999/cancel").status_code == 404


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


def test_all_players_stops_where_the_engine_can_still_judge(db):
    me, a = make_player(db, "me"), make_player(db, "a")
    make_game(db, me, a, 1900, 1900)
    db.commit()
    band = mq.resolve_mq_band(db, me.player_id, "all", time_class="rapid")
    assert (band["elo_lo"], band["elo_hi"]) == (0, 2799)


def test_a_derived_band_comes_from_the_median_within_the_class(client, db):
    me, a = make_player(db, "me"), make_player(db, "a")
    make_game(db, me, a, 1920, 1900)
    make_game(db, me, a, 1100, 1100, time_class="bullet")
    db.commit()
    band = client.get("/api/players/me/analytics/move-quality/baseline"
                      "?time_class=rapid").json()["band"]
    assert (band["elo_lo"], band["source"]) == (1900, "derived")


def test_a_press_analyzes_30_games_by_default(client, db, runner):
    """The button's label and what a press queues come from one default."""
    me, a = make_player(db, "me"), make_player(db, "a")
    make_game(db, me, a, 1850, 1850)
    db.commit()

    base = client.get("/api/players/me/analytics/move-quality/baseline"
                      "?time_class=rapid&elo_band=1800").json()
    assert base["default_games"] == 30

    r = client.post("/api/players/me/analytics/move-quality/population"
                    "?time_class=rapid&elo_band=1800").json()
    assert r["job"]["target_games"] == 30



def test_the_baseline_says_how_many_games_a_press_could_still_pick(client, db, runner):
    """Games are only sampled from the local database, so a band runs dry."""
    me, a, b, c = (make_player(db, n) for n in ("me", "a", "b", "c"))
    make_game(db, me, a, 1850, 1850)  # mine: never sampled
    make_game(db, a, b, 1850, 1850)
    make_game(db, b, c, 1850, 1850)
    db.commit()
    url = "/api/players/me/analytics/move-quality/baseline?time_class=rapid&elo_band=1800"
    assert client.get(url).json()["remaining_games"] == 2

    client.post("/api/players/me/analytics/move-quality/population?time_class=rapid&elo_band=1800")
    runner.run_pending()
    assert client.get(url).json()["remaining_games"] == 0


# ── Your own games ────────────────────────────────────────

def _sample_player(db, player, **kw):
    conn = db.connection()
    engine_db.attach_engine_db(conn)
    args = dict(player_id=player.player_id, time_class="rapid", run_id=1, limit=100)
    return sample_player(conn, **{**args, **kw})


def test_your_games_are_taken_newest_first(db, sidecar):
    me, a = make_player(db, "me"), make_player(db, "a")
    old = make_game(db, me, a, 1900, 1900)
    new = make_game(db, a, me, 1900, 1900)
    db.commit()
    assert _sample_player(db, me) == [new.game_id, old.game_id]
    assert _sample_player(db, me, limit=1) == [new.game_id]


def test_analyzed_games_are_skipped_so_a_press_reaches_further_back(db, sidecar):
    me, a = make_player(db, "me"), make_player(db, "a")
    old = make_game(db, me, a, 1900, 1900)
    new = make_game(db, me, a, 1900, 1900)
    db.commit()
    seed_evals(sidecar, WHITE_BLUNDERS, game_id=new.game_id)
    assert _sample_player(db, me) == [old.game_id]


def test_only_your_games_in_the_time_class(db, sidecar):
    me, a, b = (make_player(db, n) for n in ("me", "a", "b"))
    mine = make_game(db, me, a, 1900, 1900)
    make_game(db, me, a, 1900, 1900, time_class="bullet")
    make_game(db, a, b, 1900, 1900)
    db.commit()
    assert _sample_player(db, me) == [mine.game_id]


def test_a_player_job_analyzes_their_games(db, runner):
    me, a = make_player(db, "me"), make_player(db, "a")
    make_game(db, me, a, 1900, 1900)
    make_game(db, a, me, 1900, 1900)
    db.commit()

    job, created = runner.enqueue_player(player_id=me.player_id, time_class="rapid",
                                         target_games=30)
    assert created and job["player_id"] == me.player_id
    runner.run_pending()

    done = runner.jobs()[0]
    assert done["status"] == "complete" and done["games_total"] == 2
    assert runner.active_player_job(me.player_id, "rapid") is None


def test_a_second_press_for_your_games_returns_the_same_job(runner):
    first, _ = runner.enqueue_player(player_id=7, time_class="rapid", target_games=30)
    again, created = runner.enqueue_player(player_id=7, time_class="rapid", target_games=30)
    assert not created and again["job_id"] == first["job_id"]


def test_your_job_does_not_block_a_band_job(runner):
    runner.enqueue_player(player_id=7, time_class="rapid", target_games=30)
    assert runner.enqueue(**_band())[1]
    assert runner.active_job("rapid", 1800, 1899)["player_id"] is None


def test_the_scorecard_button_queues_30_of_your_games(client, db, runner):
    me, a = make_player(db, "me"), make_player(db, "a")
    make_game(db, me, a, 1900, 1900)
    db.commit()

    state = client.get("/api/players/me/analytics/scorecard/job?time_class=rapid").json()
    assert state["job"] is None and state["default_games"] == 30

    r = client.post("/api/players/me/analytics/scorecard/analyze?time_class=rapid").json()
    assert r["created"] and r["job"]["target_games"] == 30

    state = client.get("/api/players/me/analytics/scorecard/job?time_class=rapid").json()
    assert state["job"]["job_id"] == r["job"]["job_id"]


def test_counting_never_goes_through_run_creation(db, sidecar):
    """Creating a run rebuilds the sidecar's views, which fails any request
    reading them at the same moment. The counts run on every page load and
    poll, so they read the run without creating one."""
    me, a = make_player(db, "me"), make_player(db, "a")
    make_game(db, me, a, 1850, 1850)
    db.commit()

    @contextmanager
    def connect():
        c = db.connection()
        engine_db.attach_engine_db(c)
        yield c

    def no_creation():
        raise AssertionError("counting created a run")

    r = JobRunner(sessions=sessionmaker(bind=sidecar), connect=connect,
                  run_id=no_creation, analyze=None, current_run=lambda: 1)
    assert r.remaining_player(me.player_id, "rapid") == 1
    assert r.remaining_band("rapid", 1800, 1899, None) == 1


def test_with_no_run_yet_everything_is_left(db, sidecar):
    me, a = make_player(db, "me"), make_player(db, "a")
    make_game(db, me, a, 1850, 1850)
    db.commit()
    seed_evals(sidecar, WHITE_BLUNDERS, game_id=1)

    @contextmanager
    def connect():
        c = db.connection()
        engine_db.attach_engine_db(c)
        yield c

    r = JobRunner(sessions=sessionmaker(bind=sidecar), connect=connect,
                  run_id=lambda: 1, analyze=None, current_run=lambda: None)
    assert r.remaining_player(me.player_id, "rapid") == 1


def test_a_date_range_keeps_your_sample_inside_it(db, sidecar):
    """A press never reaches past the scorecard's range: games it is not
    showing would cost engine time for nothing."""
    me, a = make_player(db, "me"), make_player(db, "a")
    make_game(db, me, a, 1900, 1900, date_played=date(2026, 8, 1))
    inside = make_game(db, a, me, 1900, 1900, date_played=date(2026, 9, 20))
    make_game(db, me, a, 1900, 1900, date_played=date(2026, 10, 3))
    db.commit()
    picked = _sample_player(db, me, start_date=date(2026, 9, 1), end_date=date(2026, 9, 30))
    assert picked == [inside.game_id]


def test_a_press_keeps_its_range_until_it_runs(client, db, runner, sidecar):
    me, a = make_player(db, "me"), make_player(db, "a")
    make_game(db, me, a, 1900, 1900, date_played=date(2026, 8, 1))
    inside = make_game(db, a, me, 1900, 1900, date_played=date(2026, 9, 20))
    db.commit()
    q = "?time_class=rapid&start_date=2026-09-01&end_date=2026-09-30"
    assert client.get(f"/api/players/me/analytics/scorecard/job{q}").json()["remaining_games"] == 1

    client.post(f"/api/players/me/analytics/scorecard/analyze{q}")
    runner.run_pending()
    with sidecar.connect() as c:
        done = [r[0] for r in c.execute(text("SELECT game_id FROM game_coverage"))]
    assert done == [inside.game_id]
    assert client.get(f"/api/players/me/analytics/scorecard/job{q}").json()["remaining_games"] == 0


def test_the_scorecard_button_says_how_many_of_your_games_are_left(client, db, runner):
    me, a = make_player(db, "me"), make_player(db, "a")
    make_game(db, me, a, 1900, 1900)
    make_game(db, a, me, 1900, 1900)
    db.commit()
    url = "/api/players/me/analytics/scorecard/job?time_class=rapid"
    assert client.get(url).json()["remaining_games"] == 2

    client.post("/api/players/me/analytics/scorecard/analyze?time_class=rapid")
    runner.run_pending()
    assert client.get(url).json()["remaining_games"] == 0


def test_the_app_runner_upgrades_an_older_sidecar(tmp_path, monkeypatch):
    """A sidecar made before population_jobs.player_id existed must gain it
    when the app builds its runner, not only when an analysis starts."""
    from sqlalchemy import create_engine, text

    import app.main as main
    from engine import views

    eng = create_engine(f"sqlite:///{tmp_path / 'old.db'}")
    with eng.begin() as conn:
        conn.execute(text(
            "CREATE TABLE population_jobs (job_id INTEGER PRIMARY KEY, "
            "time_class VARCHAR(20) NOT NULL, elo_lo INTEGER NOT NULL, "
            "elo_hi INTEGER NOT NULL, exclude_player_id INTEGER, "
            "target_games INTEGER NOT NULL, games_total INTEGER, "
            "games_done INTEGER NOT NULL, status VARCHAR(20) NOT NULL, "
            "created_at DATETIME NOT NULL, started_at DATETIME, "
            "finished_at DATETIME, error TEXT)"
        ))
    monkeypatch.setattr(views, "engine", eng)
    monkeypatch.setattr(engine_db, "engine", eng)
    monkeypatch.setattr(engine_db, "SessionLocal", sessionmaker(bind=eng))
    monkeypatch.setattr(main, "_population_runner", None)

    runner = main.get_population_runner()
    assert runner.active_player_job(1, "rapid") is None


def test_building_the_app_runner_leaves_the_views_alone(tmp_path, monkeypatch):
    """The runner is built lazily on a request, while other requests may be
    reading the views; rebuilding them there made those reads fail with
    "no such table". Only tables and columns are brought up to date."""
    from sqlalchemy import create_engine, text

    import app.main as main
    from engine import views

    eng = create_engine(f"sqlite:///{tmp_path / 'live.db'}")
    with eng.begin() as conn:
        conn.execute(text("CREATE VIEW game_move_quality AS SELECT 1 AS sentinel"))
    monkeypatch.setattr(views, "engine", eng)
    monkeypatch.setattr(engine_db, "engine", eng)
    monkeypatch.setattr(engine_db, "SessionLocal", sessionmaker(bind=eng))
    monkeypatch.setattr(main, "_population_runner", None)

    main.get_population_runner()
    with eng.connect() as conn:
        sql = conn.execute(text(
            "SELECT sql FROM sqlite_master WHERE name = 'game_move_quality'")).scalar()
    assert "sentinel" in sql


def test_the_band_ladder_says_how_many_games_are_analyzed(client, db, sidecar):
    """So the dropdown shows which bands would benefit from more analysis."""
    me, a, b, c = (make_player(db, n) for n in ("me", "a", "b", "c"))
    make_game(db, me, a, 1850, 1850)
    g1 = make_game(db, a, b, 1850, 1850)
    g2 = make_game(db, b, c, 1860, 1840)
    make_game(db, a, c, 1250, 1250)           # in a band, but not analyzed
    mine = make_game(db, me, b, 1850, 1850)   # analyzed, but the player's own
    db.commit()
    for g in (g1, g2, mine):
        seed_evals(sidecar, WHITE_BLUNDERS, game_id=g.game_id)

    r = client.get("/api/players/me/analytics/baseline-bands?time_class=rapid").json()
    analyzed = {x["elo_lo"]: x["n_games"] for x in r["analyzed"]}
    assert analyzed.get(1800) == 2
    assert 1200 not in analyzed
