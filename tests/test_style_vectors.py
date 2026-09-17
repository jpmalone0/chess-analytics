"""Centring, aggregation, percentile and similarity over the style tables."""

import math

import chess
import pytest
from sqlalchemy import create_engine, text

from analysis.build_features import build_cell_means, build_player_vectors, extract_game
from analysis.metrics import space
from app.style import (
    AXES,
    ELITE_MIN_ELO,
    SIMILARITY_CLASS,
    AxisValue,
    Vector,
    _reference_values,
    percentile_profile,
    similar_players,
    subject_vector,
)


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

    def test_near_identical_nonzero_values_do_not_crash_the_square_root(self, conn):
        """Regression test for Task 5's review: SD is derived from the
        single-pass formula E[x^2] - E[x]^2, which suffers catastrophic
        cancellation when every value sits within about 1e-9 of a shared
        NONZERO mean.

        The values must be near-identical but not bit-identical, and the
        shared mean must not be zero, for this to exercise anything.
        `test_identical_games_give_a_zero_standard_error` above uses
        bit-identical values: E[x^2] and E[x]^2 are then computed from the
        exact same bit pattern, so the subtraction is exact and the clamp is
        never touched. Here each value is nudged by a few times 1e-9 around
        -48.76. E[x^2] and E[x]^2 both land near 48.76^2 = 2,378 -- a
        magnitude at which float64's ~15-17 significant decimal digits only
        resolve differences down to about 2,378 * 2^-52 =~ 5e-13. The true
        variance here is on the order of 1e-18, far below that floor, so the
        subtraction is dominated by rounding noise that can fall on either
        side of zero. A mean of 0 would not trigger this: E[x^2] and E[x]^2
        would both be tiny already, well within float64's resolution, and the
        subtraction would stay accurate. These particular nudges were checked
        to drive SQLite's actual AVG() negative (about -4.5e-13); without the
        `max(0.0, ...)` clamp, math.sqrt raises ValueError: math domain error.
        """
        conn.execute(text("INSERT OR IGNORE INTO players VALUES (1, 'subject')"))
        conn.execute(text("INSERT OR IGNORE INTO players VALUES (2, 'other')"))
        # A coarse fallback of exactly 0 so centring is a no-op and the raw
        # values -- clustered around the nonzero -48.76 -- pass straight
        # through into the variance calculation unchanged.
        conn.execute(text(
            "INSERT INTO style_cell_means VALUES "
            "('blitz', '*', 'white', 999, 0, 0, 0, 0)"))
        base = -48.76
        nudges = [
            -5.571456909457337e-11, -7.985975838632685e-10,
            -1.3165632909243266e-10, 2.2177394688760325e-10,
            8.260221064757965e-10, 9.332127355415175e-10,
            -4.598044689456589e-11, 7.306198555432802e-10,
            -4.790153792160812e-10, 6.100556540260447e-10,
            9.739860767117856e-11, -9.719165996719621e-10,
        ]
        assert len(set(nudges)) == len(nudges), "values must not be bit-identical"
        for i, nudge in enumerate(nudges):
            gid = 500 + i
            conn.execute(text(
                "INSERT INTO games (game_id, white_player_id, black_player_id, "
                "time_class, eco, variant, white_elo, black_elo) "
                "VALUES (:g, 1, 2, 'blitz', 'Z99', NULL, 1500, 1500)"), {"g": gid})
            conn.execute(text(
                "INSERT INTO position_features VALUES (:g, 'white', :s, 0, 0, 0)"),
                {"g": gid, "s": base + nudge})
            conn.execute(text(
                "INSERT INTO position_features VALUES (:g, 'black', 0, 0, 0, 0)"),
                {"g": gid})

        result = subject_vector(conn, player_id=1, time_class="blitz")
        assert math.isfinite(result.axes["space"].se)
        assert result.axes["space"].se == pytest.approx(0.0, abs=1e-6)


def seed_reference(conn, values, time_class="blitz", elo=2000.0):
    """One reference player per value, so percentiles are hand-checkable."""
    for i, v in enumerate(values, start=10):
        conn.execute(text(
            "INSERT INTO player_style_vectors VALUES "
            "(:p, :tc, 100, :elo, :s, :s, :s, :s)"),
            {"p": i, "tc": time_class, "elo": elo, "s": v})


class TestPercentile:
    def test_the_median_lands_mid_scale(self, conn):
        seed_reference(conn, [0.0, 1.0, 2.0, 3.0, 4.0])
        axes = {a: AxisValue(mean=2.0, se=0.0) for a in AXES}
        out = percentile_profile(conn, Vector(n=50, axes=axes), "blitz")
        assert out["space"]["percentile"] == pytest.approx(40, abs=15)

    def test_an_extreme_value_lands_at_the_top(self, conn):
        seed_reference(conn, [0.0, 1.0, 2.0, 3.0, 4.0])
        axes = {a: AxisValue(mean=99.0, se=0.0) for a in AXES}
        out = percentile_profile(conn, Vector(n=50, axes=axes), "blitz")
        assert out["space"]["percentile"] == 100

    def test_a_large_standard_error_widens_the_interval(self, conn):
        """This is the whole low-n design decision: the panel never disappears,
        the bar just grows until it says nothing, which is honest."""
        seed_reference(conn, [float(i) for i in range(100)])
        tight = percentile_profile(
            conn, Vector(n=500, axes={a: AxisValue(50.0, 0.1) for a in AXES}), "blitz")
        loose = percentile_profile(
            conn, Vector(n=2, axes={a: AxisValue(50.0, 40.0) for a in AXES}), "blitz")
        tight_width = tight["space"]["high"] - tight["space"]["low"]
        loose_width = loose["space"]["high"] - loose["space"]["low"]
        assert loose_width > tight_width
        assert loose_width > 50

    def test_an_empty_vector_produces_an_empty_profile(self, conn):
        seed_reference(conn, [0.0, 1.0])
        assert percentile_profile(conn, Vector(), "blitz") == {}

    def test_an_empty_reference_produces_an_empty_profile(self, conn):
        axes = {a: AxisValue(mean=1.0, se=0.0) for a in AXES}
        assert percentile_profile(conn, Vector(n=50, axes=axes), "blitz") == {}

    def test_reference_values_survives_a_missing_sidecar_table(self, conn):
        conn.execute(text("DROP TABLE player_style_vectors"))
        assert _reference_values(conn, "blitz") == {a: [] for a in AXES}


class TestSimilarity:
    def test_the_nearest_player_comes_first(self, conn):
        for pid, v in ((10, 0.0), (11, 5.0), (12, 10.0)):
            conn.execute(text(
                "INSERT INTO players VALUES (:p, :u)"),
                {"p": pid, "u": f"gm{pid}"})
            conn.execute(text(
                "INSERT INTO player_style_vectors VALUES "
                "(:p, 'blitz', 200, 2900, :v, :v, :v, :v)"), {"p": pid, "v": v})
        axes = {a: AxisValue(mean=0.2, se=0.0) for a in AXES}
        out = similar_players(conn, Vector(n=100, axes=axes))
        assert out[0]["username"] == "gm10"
        assert out[0]["distance"] < out[-1]["distance"]

    def test_players_below_the_rating_floor_are_excluded(self, conn):
        for pid, elo in ((10, 2900), (11, 2500)):
            conn.execute(text("INSERT INTO players VALUES (:p, :u)"),
                         {"p": pid, "u": f"p{pid}"})
            conn.execute(text(
                "INSERT INTO player_style_vectors VALUES "
                "(:p, 'blitz', 200, :e, 0, 0, 0, 0)"), {"p": pid, "e": elo})
        axes = {a: AxisValue(mean=0.0, se=0.0) for a in AXES}
        names = {r["username"] for r in similar_players(conn, Vector(100, axes))}
        assert names == {"p10"}
        assert ELITE_MIN_ELO == 2800

    def test_only_blitz_vectors_are_used(self, conn):
        """The reference is always blitz, whatever class the subject is viewing:
        top players barely play rapid online, and gating per class leaves 7
        usable reference players for a rapid subject against 405 for blitz."""
        conn.execute(text("INSERT INTO players VALUES (10, 'gm')"))
        conn.execute(text(
            "INSERT INTO player_style_vectors VALUES "
            "(10, 'rapid', 200, 2900, 0, 0, 0, 0)"))
        axes = {a: AxisValue(mean=0.0, se=0.0) for a in AXES}
        assert similar_players(conn, Vector(100, axes)) == []
        assert SIMILARITY_CLASS == "blitz"

    def test_axes_are_standardised_before_the_distance(self, conn):
        """mobility has roughly triple the raw spread of space, so an
        unstandardised distance would be a mobility ranking wearing a costume.
        Here p10 matches on mobility only and p11 on space only; with equal
        standardised offsets they must come out equidistant."""
        for pid, sp, mob in ((10, 3.0, 0.0), (11, 0.0, 9.0)):
            conn.execute(text("INSERT INTO players VALUES (:p, :u)"),
                         {"p": pid, "u": f"p{pid}"})
            conn.execute(text(
                "INSERT INTO player_style_vectors "
                "(player_id, time_class, n, mean_elo, space, mobility, "
                "king_safety, pawn_structure) "
                "VALUES (:p, 'blitz', 200, 2900, :s, :m, 0, 0)"),
                {"p": pid, "s": sp, "m": mob})
        conn.execute(text("INSERT INTO players VALUES (12, 'spread')"))
        conn.execute(text(
            "INSERT INTO player_style_vectors VALUES "
            "(12, 'blitz', 200, 2900, -3.0, -9.0, 0, 0)"))
        axes = {a: AxisValue(mean=0.0, se=0.0) for a in AXES}
        out = {r["username"]: r["distance"] for r in similar_players(conn, Vector(100, axes))}
        assert out["p10"] == pytest.approx(out["p11"], rel=0.01)

    def test_an_empty_vector_returns_nothing(self, conn):
        """The pool must be seeded for this to prove anything.

        Against an empty pool the function returns [] whether or not the
        early guard exists, so the test would pass while detecting nothing.
        With rows present, removing the guard raises KeyError on
        vector.axes[axis] instead.
        """
        conn.execute(text("INSERT INTO players VALUES (10, 'gm')"))
        conn.execute(text(
            "INSERT INTO player_style_vectors VALUES "
            "(10, 'blitz', 200, 2900, 0, 0, 0, 0)"))
        assert similar_players(conn, Vector()) == []

    def test_the_subject_does_not_appear_in_their_own_results(self, conn):
        """A player who is themselves in the 2800+ blitz pool must not show up
        in their own similarity list -- and excluding them must not shrink the
        list below SIMILAR_COUNT when enough other players exist to fill it.
        """
        SUBJECT_ID = 10
        for pid, v in ((SUBJECT_ID, 0.0), (11, 1.0), (12, 2.0),
                       (13, 3.0), (14, 4.0), (15, 5.0)):
            conn.execute(text(
                "INSERT INTO players VALUES (:p, :u)"), {"p": pid, "u": f"p{pid}"})
            conn.execute(text(
                "INSERT INTO player_style_vectors VALUES "
                "(:p, 'blitz', 200, 2900, :v, :v, :v, :v)"), {"p": pid, "v": v})
        axes = {a: AxisValue(mean=0.0, se=0.0) for a in AXES}
        out = similar_players(conn, Vector(n=100, axes=axes), player_id=SUBJECT_ID)
        assert "p10" not in {r["username"] for r in out}
        assert len(out) == 5

    def test_similar_players_survives_a_missing_sidecar_table(self, conn):
        axes = {a: AxisValue(mean=0.0, se=0.0) for a in AXES}
        conn.execute(text("DROP TABLE player_style_vectors"))
        assert similar_players(conn, Vector(n=10, axes=axes)) == []
