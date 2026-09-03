"""Shared pytest fixtures — an in-memory database with a hand-seeded corpus."""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models import Game, Move, Player


@pytest.fixture
def db():
    """In-memory SQLite session with the full schema created."""
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
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
    white_clocks=None, black_clocks=None,
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
        opening_name=opening_name, total_moves=total_moves,
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
