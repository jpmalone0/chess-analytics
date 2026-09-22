"""Shared pytest fixtures — an in-memory database with a hand-seeded corpus."""

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.models import Game, Move, Player
from engine import db as engine_db
from engine.views import WP_CURVE_DDL


@pytest.fixture(autouse=True)
def _isolate_engine_db(tmp_path, monkeypatch):
    """Keep the suite away from the real sidecar database.

    ENGINE_DATABASE_URL defaults to a relative path and pytest runs from the
    repository root, so attach_engine_db would otherwise attach the real
    chess_engine.db -- 14MB holding hours of Stockfish analysis that nothing
    here can regenerate quickly. No test writes to it today, but the cost of
    one that does is total, and the cost of this fixture is nothing.

    Pointing it at a per-test temporary file also means the sidecar-missing
    path is genuinely exercised rather than accidentally satisfied by whatever
    happens to be on disk.
    """
    monkeypatch.setattr(
        engine_db, "ENGINE_DATABASE_URL", f"sqlite:///{tmp_path / 'engine.db'}"
    )


@pytest.fixture
def db():
    """In-memory SQLite session with the full schema created.

    StaticPool is required, not just convenient: SQLAlchemy's default pool for
    an in-memory SQLite URL is thread-affinitized (a distinct physical
    ":memory:" database per thread). Route tests dispatch the request through
    FastAPI's TestClient, which runs the ASGI app on a background anyio worker
    thread rather than the pytest thread that built this schema -- without
    StaticPool's single shared connection, that request sees a fresh, empty
    database and every query fails with "no such table".
    """
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()


def make_player(db, username):
    p = Player(username=username, platform="chess.com")
    db.add(p)
    db.flush()
    return p


_next_end_time = 1700000000


def make_game(
    db, white, black, white_elo, black_elo,
    time_control="600", time_class="rapid", result="1-0",
    end_time=None, opening_name="Sicilian Defense", total_moves=40,
    white_move_times=None, black_move_times=None,
    white_clocks=None, black_clocks=None, variant=None, date_played=None,
):
    """Create one game plus its moves. Move time lists default to a flat 5s.

    end_time defaults to a monotonically increasing counter (mirroring
    `_seed_calls` below) so that repeated calls for the same pair of players
    don't collide on the UNIQUE `chess_com_url`, which is derived from
    (white, black, end_time). Pass end_time explicitly to control
    chronological ordering — an explicit value is always honored as-is.
    """
    global _next_end_time
    if end_time is None:
        end_time = _next_end_time
        _next_end_time += 1

    g = Game(
        white_player_id=white.player_id, black_player_id=black.player_id,
        result=result, time_control=time_control, time_class=time_class,
        white_elo=white_elo, black_elo=black_elo, end_time=end_time,
        opening_name=opening_name, total_moves=total_moves, variant=variant,
        date_played=date_played,
        chess_com_url=f"https://example.test/{white.username}/{black.username}/{end_time}",
    )
    db.add(g)
    db.flush()

    white_move_times = white_move_times if white_move_times is not None else [5.0] * 3
    black_move_times = black_move_times if black_move_times is not None else [5.0] * 3

    assert abs(len(white_move_times) - len(black_move_times)) <= 1, (
        "white and black move counts must not differ by more than one ply — "
        "white always moves first, so black can trail by at most one move; a "
        "larger gap would make the interleaving below emit two consecutive "
        "same-colour plies, silently producing an illegal move sequence"
    )

    ply = 0
    for i in range(max(len(white_move_times), len(black_move_times))):
        for color, times, clocks in (
            ("white", white_move_times, white_clocks),
            ("black", black_move_times, black_clocks),
        ):
            if i >= len(times):
                continue
            ply += 1
            db.add(Move(
                game_id=g.game_id, ply=ply, move_number=i + 1, color=color,
                move_san="e4",
                clock_seconds=clocks[i] if clocks is not None else None,
                time_spent_seconds=times[i],
            ))
    db.flush()
    return g


_seed_calls = 0


def seed_band(db, lo, n_players, games_each=1, move_time=5.0, **kwargs):
    """Seed n_players distinct players in the [lo, lo+99] band, each playing
    `games_each` games as white against a shared throwaway opponent.

    Usernames carry a call counter, not just the band: some tests seed the same
    band twice (e.g. one time control that is dense and one that is sparse), and
    players.username is UNIQUE.
    """
    global _seed_calls
    _seed_calls += 1
    tag = f"{lo}-{_seed_calls}"

    filler = make_player(db, f"filler-{tag}")
    players = []
    for i in range(n_players):
        p = make_player(db, f"p{tag}-{i}")
        players.append(p)
        for gi in range(games_each):
            # The filler's Elo is parked far below any band under test: it plays
            # every game, so leaving it in-band would let one player's results
            # dominate the very population the cap exists to protect.
            make_game(
                db, p, filler, white_elo=lo + 50, black_elo=100,
                end_time=1700000000 + gi,
                white_move_times=[move_time] * 3,
                **kwargs,
            )
    db.commit()
    return players


# ═══════════════════════════════════════════════════════════
# The sidecar, built from raw DDL
# ═══════════════════════════════════════════════════════════

# The tables the derivation views read. Written out by hand rather than taken
# from engine.models so that the views are exercised against SQL SQLite parses
# directly, not against whatever the ORM happens to emit -- which is the same
# reason WP_CURVE_DDL exists. tests/test_move_severity.py runs the wp_curve
# constraints against both definitions so the pair cannot drift.
_POSITION_EVALS_DDL = """
CREATE TABLE position_evals (
    run_id        INTEGER NOT NULL,
    game_id       INTEGER NOT NULL,
    ply           INTEGER NOT NULL,
    cp            INTEGER,
    mate_in       INTEGER,
    best_move_uci VARCHAR(6),
    PRIMARY KEY (run_id, game_id, ply)
)
"""

_GAME_COVERAGE_DDL = """
CREATE TABLE game_coverage (
    run_id         INTEGER NOT NULL,
    game_id        INTEGER NOT NULL,
    plies_analyzed INTEGER NOT NULL,
    status         VARCHAR(20) NOT NULL,
    error          TEXT,
    completed_at   DATETIME,
    time_class     VARCHAR(20),
    PRIMARY KEY (run_id, game_id)
)
"""

# The one fitted curve the seeding helper defaults to. 360 is the measured rapid
# k, and tests/test_move_severity.py recomputes the curve in Python against the
# same number.
RAPID_K = 360.0


def build_sidecar(*views):
    """An in-memory sidecar: the raw tables, a fitted rapid curve, and `views`.

    `views` are CREATE VIEW statements from engine.views, executed in the order
    given. They stack -- move_severity reads move_evals, move_quality reads
    move_severity -- so pass them shallowest first, or SQLite has nothing to
    resolve the deeper one against.

    Built from raw DDL rather than through the ORM so that the view SQL runs
    exactly as SQLite will run it in the real sidecar.
    """
    eng = create_engine("sqlite://")
    with eng.begin() as conn:
        conn.execute(text(_POSITION_EVALS_DDL))
        conn.execute(text(_GAME_COVERAGE_DDL))
        conn.execute(text(WP_CURVE_DDL))
        for view in views:
            conn.execute(text(view))
        conn.execute(text(
            "INSERT INTO wp_curve (time_class, k, n, source) "
            "VALUES ('rapid', :k, 45110, 'test')"
        ), {"k": RAPID_K})
    return eng


def seed_evals(eng, positions, game_id=1, run_id=1, time_class="rapid"):
    """Seed one game's evaluations, from White's point of view.

    positions: (ply, cp) pairs, or (ply, cp, mate_in) triples. Most tests never
    touch mate, so the pair form is the common case and leaves mate_in NULL;
    the mate tests pass the third element.

    game_coverage gets a matching row, because move_severity joins through it to
    reach wp_curve — a game with no coverage row produces no severity at all.
    """
    with eng.begin() as conn:
        conn.execute(
            text("INSERT OR REPLACE INTO game_coverage "
                 "(run_id, game_id, plies_analyzed, status, time_class) "
                 "VALUES (:r, :g, :n, 'complete', :tc)"),
            {"r": run_id, "g": game_id, "n": len(positions), "tc": time_class},
        )
        for position in positions:
            ply, cp, mate_in = position if len(position) == 3 else (*position, None)
            conn.execute(
                text("INSERT INTO position_evals (run_id, game_id, ply, cp, mate_in) "
                     "VALUES (:r, :g, :p, :cp, :m)"),
                {"r": run_id, "g": game_id, "p": ply, "cp": cp, "m": mate_in},
            )


def view_rows(eng, view, game_id=1):
    """One game's rows from a per-ply view, keyed by ply."""
    with eng.connect() as conn:
        return {
            r["ply"]: r
            for r in conn.execute(
                text(f"SELECT * FROM {view} WHERE game_id = :g ORDER BY ply"),
                {"g": game_id},
            ).mappings()
        }
