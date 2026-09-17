"""The style route: shape, filter pass-through, and the empty cases."""

import pytest
from fastapi.testclient import TestClient

from app import style
from app.database import get_db
from app.main import app
from tests.conftest import make_player


@pytest.fixture
def client(db):
    app.dependency_overrides[get_db] = lambda: db
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_an_unknown_player_is_a_404(client):
    r = client.get("/api/players/nobody/analytics/style")
    assert r.status_code == 404


def test_a_player_with_no_features_gets_an_empty_profile(client, db):
    make_player(db, "subject")
    db.commit()
    r = client.get("/api/players/subject/analytics/style")
    assert r.status_code == 200
    body = r.json()
    assert body["n_games"] == 0
    assert body["axes"] == []
    assert body["similar"] == []


def test_the_response_names_both_reference_sets(client, db):
    """Percentiles rank against every player with a vector; similarity uses the
    elite blitz pool. Reporting one under the other's name is exactly the
    mistake that produced four dissolved findings."""
    make_player(db, "subject")
    db.commit()
    body = client.get("/api/players/subject/analytics/style").json()
    assert body["percentile_reference"]["pool"]
    assert body["similarity_reference"]["pool"] == f"{style.ELITE_MIN_ELO}+ blitz"
    assert body["similarity_reference"]["vectors_from"] == "blitz"
