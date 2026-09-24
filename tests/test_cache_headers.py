"""The page revalidates on every load; API answers are never cached."""

from fastapi.testclient import TestClient

from app.main import app


def test_the_page_revalidates_so_new_asset_versions_are_seen():
    r = TestClient(app).get("/")
    assert r.headers["cache-control"] == "no-cache"


def test_static_assets_keep_their_normal_caching():
    r = TestClient(app).get("/static/app.js")
    assert "cache-control" not in r.headers
