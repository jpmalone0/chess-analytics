"""Centring, aggregation, percentile and similarity over the style tables."""

import pytest
from sqlalchemy import create_engine, text


@pytest.fixture
def conn():
    """One in-memory database holding both schemas.

    The real deployment keeps them in separate files joined by ATTACH, but the
    queries only ever name tables, so a single database exercises the same SQL.
    """
    eng = create_engine("sqlite://")
    with eng.begin() as c:
        c.execute(text("""
            CREATE TABLE players (
                player_id INTEGER PRIMARY KEY, username TEXT)"""))
        c.execute(text("""
            CREATE TABLE games (
                game_id INTEGER PRIMARY KEY,
                white_player_id INTEGER, black_player_id INTEGER,
                time_class TEXT, eco TEXT, variant TEXT,
                white_elo INTEGER, black_elo INTEGER,
                date_played DATE, end_time INTEGER)"""))
        c.execute(text("""
            CREATE TABLE position_features (
                game_id INTEGER, color TEXT,
                space REAL, mobility REAL, king_safety REAL, pawn_structure REAL,
                PRIMARY KEY (game_id, color))"""))
        c.execute(text("""
            CREATE TABLE style_cell_means (
                time_class TEXT, eco3 TEXT, color TEXT, n INTEGER,
                space REAL, mobility REAL, king_safety REAL, pawn_structure REAL,
                PRIMARY KEY (time_class, eco3, color))"""))
        c.execute(text("""
            CREATE TABLE player_style_vectors (
                player_id INTEGER, time_class TEXT, n INTEGER, mean_elo REAL,
                space REAL, mobility REAL, king_safety REAL, pawn_structure REAL,
                PRIMARY KEY (player_id, time_class))"""))
        yield c


def test_the_schema_exists(conn):
    for table in ("position_features", "style_cell_means", "player_style_vectors"):
        conn.execute(text(f"SELECT COUNT(*) FROM {table}"))
