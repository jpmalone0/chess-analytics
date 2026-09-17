"""Centring, aggregation, percentile and similarity over the style tables."""

import chess
import pytest
from sqlalchemy import create_engine, text

from analysis.build_features import extract_game
from analysis.metrics import space


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


#: A legal 20-ply line (Ruy Lopez, Breyer setup). The plan's draft built this by
#: playing the first 10 plies twice ("sans * 2"), but the second pass is not
#: legal: after 10 plies the e2 and e7 pawns are already on e4/e5, so replaying
#: "e4"/"e5" raises chess.IllegalMoveError, and extract_game (correctly)
#: catches that and returns []. Replaced with a single explicit 20-ply line so
#: the tests exercise a real full-length game instead of an impossible one.
_TWENTY_PLY_GAME = [
    "e4", "e5", "Nf3", "Nc6", "Bb5", "a6", "Ba4", "Nf6", "O-O", "Be7",
    "Re1", "b5", "Bb3", "d6", "c3", "O-O", "h3", "Nb8", "d4", "Nbd7",
]


class TestExtraction:
    def test_a_game_yields_one_row_per_colour(self):
        rows = extract_game(7, _TWENTY_PLY_GAME)
        assert {r["color"] for r in rows} == {"white", "black"}
        assert all(r["game_id"] == 7 for r in rows)
        assert len(rows) == 2

    def test_a_short_game_yields_nothing(self):
        """Fewer than 20 plies means the snapshot ply was never reached. An
        implementation that measured the final position instead would compare
        move 6 against move 20 and call it the same thing."""
        assert extract_game(7, ["e4", "e5"]) == []

    def test_an_unreplayable_game_yields_nothing(self):
        sans = ["e4", "e5", "Qxh8"] + ["e4"] * 20
        assert extract_game(7, sans) == []

    def test_the_values_match_the_metrics_at_ply_20(self):
        sans = _TWENTY_PLY_GAME
        rows = {r["color"]: r for r in extract_game(7, sans)}
        board = chess.Board()
        for san in sans[:20]:
            board.push_san(san)
        assert rows["white"]["space"] == space(board, chess.WHITE)
        assert rows["black"]["space"] == space(board, chess.BLACK)
