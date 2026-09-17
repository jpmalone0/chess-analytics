"""Centring, aggregation, percentile and similarity over the style tables."""

import math

import chess
import pytest
from sqlalchemy import create_engine, text

from analysis.build_features import build_cell_means, build_player_vectors, extract_game
from analysis.metrics import space
from app.style import AXES, subject_vector


@pytest.fixture(autouse=True)
def _local_schema(monkeypatch):
    """Tests keep both schemas in one database, so the sidecar prefix is empty.

    Production ATTACHes the sidecar under the alias "engine"; the SQL is
    otherwise identical, so this exercises the same statements.
    """
    from analysis import build_features
    from app import style

    monkeypatch.setattr(build_features, "SCHEMA", "")
    monkeypatch.setattr(style, "SCHEMA", "")


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

    def test_exactly_nineteen_plies_yields_nothing(self):
        """A game one ply short of the snapshot is not measured at all.

        Not a guard against `<` becoming `<=`: at 19 plies both are true, so
        that mutation diverges at 20, where the two tests above already catch
        it. What this pins is the guard existing at all -- drop it, or set
        SNAPSHOT_PLY below 20, and a short game gets measured at whatever
        position it reached, mixing move 19 with move 20 across the corpus.
        """
        assert extract_game(7, _TWENTY_PLY_GAME[:19]) == []


def seed_games(conn, n, time_class="blitz", eco="B30", elo=1500,
                space=5.0, start_id=1):
    """n games where our player is White, each with the same measured values."""
    conn.execute(text("INSERT OR IGNORE INTO players VALUES (1, 'subject')"))
    conn.execute(text("INSERT OR IGNORE INTO players VALUES (2, 'other')"))
    for i in range(n):
        gid = start_id + i
        conn.execute(text(
            "INSERT INTO games (game_id, white_player_id, black_player_id, "
            "time_class, eco, variant, white_elo, black_elo) "
            "VALUES (:g, 1, 2, :tc, :eco, NULL, :elo, :elo)"),
            {"g": gid, "tc": time_class, "eco": eco, "elo": elo})
        for color in ("white", "black"):
            conn.execute(text(
                "INSERT INTO position_features VALUES (:g, :c, :s, 0, 0, 0)"),
                {"g": gid, "c": color, "s": space if color == "white" else 0.0})


class TestCentring:
    def test_a_populated_cell_becomes_its_own_centre(self, conn):
        """With every game identical, each player's centred value is exactly 0 --
        the whole point of centring, and a sign error would show as +/-5."""
        seed_games(conn, 50)
        build_cell_means(conn)
        build_player_vectors(conn)
        v = conn.execute(text(
            "SELECT space FROM player_style_vectors WHERE player_id = 1")).scalar()
        assert v == pytest.approx(0.0)

    def test_a_thin_cell_falls_back_to_the_coarse_mean(self, conn):
        """B30 has 50 games so it gets a cell; C00 has 5 and does not. The C00
        games must still be centred -- dropping them would bias the profile
        toward whichever openings happen to be popular."""
        seed_games(conn, 50, eco="B30", space=5.0)
        seed_games(conn, 5, eco="C00", space=9.0, start_id=100)
        build_cell_means(conn)
        build_player_vectors(conn)
        cells = {r[0] for r in conn.execute(text(
            "SELECT eco3 FROM style_cell_means WHERE color = 'white'"))}
        assert cells == {"B30", "*"}
        n = conn.execute(text(
            "SELECT n FROM player_style_vectors WHERE player_id = 1")).scalar()
        assert n == 55, "the thin-cell games must still be counted"

    def test_a_player_below_the_game_threshold_gets_no_vector(self, conn):
        seed_games(conn, 10)
        build_cell_means(conn)
        build_player_vectors(conn)
        assert conn.execute(text(
            "SELECT COUNT(*) FROM player_style_vectors")).scalar() == 0

    def test_vectors_are_separate_per_time_class(self, conn):
        seed_games(conn, 40, time_class="blitz")
        seed_games(conn, 40, time_class="bullet", start_id=200)
        build_cell_means(conn)
        build_player_vectors(conn)
        classes = {r[0] for r in conn.execute(text(
            "SELECT time_class FROM player_style_vectors WHERE player_id = 1"))}
        assert classes == {"blitz", "bullet"}


class TestSubjectVector:
    def test_it_returns_a_mean_and_a_standard_error_per_axis(self, conn):
        seed_games(conn, 40)
        build_cell_means(conn)
        result = subject_vector(conn, player_id=1, time_class="blitz")
        assert set(result.axes) == set(AXES)
        assert result.n == 40
        for axis in AXES:
            assert math.isfinite(result.axes[axis].mean)
            assert result.axes[axis].se >= 0.0

    def test_identical_games_give_a_zero_standard_error(self, conn):
        """Every game measured the same, so the mean cannot be uncertain. A
        standard error computed as SD/sqrt(n) with a wrong SD shows up here."""
        seed_games(conn, 40)
        build_cell_means(conn)
        result = subject_vector(conn, player_id=1, time_class="blitz")
        assert result.axes["space"].se == pytest.approx(0.0)

    def test_the_standard_error_shrinks_as_games_accumulate(self, conn):
        """More observations of the same noisy quantity narrow the estimate.

        The per-game value alternates between two fixed points rather than
        growing with i: growing values would widen the sample's spread as more
        games are added, inflating variance faster than sqrt(n) shrinks it, and
        the assertion below would fail for any correct implementation.
        """
        for i in range(40):
            seed_games(conn, 1, space=float(i % 2), start_id=i + 1)
        build_cell_means(conn)
        few = subject_vector(conn, 1, "blitz", limit_game_ids=list(range(1, 6)))
        many = subject_vector(conn, 1, "blitz")
        assert many.axes["space"].se < few.axes["space"].se

    def test_no_games_gives_an_empty_vector_not_a_crash(self, conn):
        """Narrowing a filter to nothing must not divide by zero."""
        result = subject_vector(conn, player_id=1, time_class="rapid")
        assert result.n == 0
        assert result.axes == {}

    def test_the_time_class_filter_is_honoured(self, conn):
        seed_games(conn, 40, time_class="blitz")
        seed_games(conn, 10, time_class="bullet", start_id=200)
        build_cell_means(conn)
        assert subject_vector(conn, 1, "blitz").n == 40
        assert subject_vector(conn, 1, "bullet").n == 10
