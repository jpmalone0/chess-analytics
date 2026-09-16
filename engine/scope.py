"""Resolve a search into the set of games worth evaluating.

Evaluation is opt-in and bounded: the corpus is 17M plies and nobody is waiting
28 hours for it. What gets analyzed is whatever a player's current search is
already looking at — one username, one date window, one time class.

Scoping is split from analysis on purpose. A later endpoint behind an explicit
"add engine evaluation" button resolves a scope from request parameters and
hands the IDs to the same analyzer, without inheriting a CLI's argument parsing.
"""

from dataclasses import dataclass
from datetime import date
from typing import Optional

from sqlalchemy import text

from app.crud import _build_game_filters


@dataclass(frozen=True)
class Scope:
    """What a caller asked to have analyzed."""

    username: str
    time_class: Optional[str] = None
    start_date: Optional[date] = None
    end_date: Optional[date] = None
    player_color: Optional[str] = None
    opening_names: Optional[str] = None
    tz: Optional[str] = None
    limit: Optional[int] = None


class UnknownPlayer(Exception):
    """The username is not in the canonical database."""


def resolve_scope(conn, scope: Scope) -> list[int]:
    """Return the game IDs a scope covers, newest first.

    Filtering goes through app.crud._build_game_filters rather than
    reimplementing it. The date window has real subtlety in it — local-timezone
    day boundaries, and a fallback to UTC dates for the 88% of rows with no
    end_time — and a second copy would drift from what the UI shows. Variant
    exclusion comes along for free for the same reason.

    Read-only. The canonical database is never written during analysis.
    """
    player = conn.execute(
        text("SELECT player_id FROM players WHERE username = :username"),
        {"username": scope.username},
    ).mappings().first()
    if player is None:
        raise UnknownPlayer(scope.username)

    where, params = _build_game_filters(
        player_id=player["player_id"],
        time_class=scope.time_class,
        start_date=scope.start_date,
        end_date=scope.end_date,
        player_color=scope.player_color,
        opening_names=scope.opening_names,
        tz=scope.tz,
    )

    # A game with no stored moves cannot be replayed, and a game with one ply
    # yields no move to score. Excluding them here keeps the analyzer from
    # writing coverage rows for work that was never possible.
    sql = f"""
        SELECT g.game_id
        FROM   games g
        WHERE  {where}
          AND  (SELECT COUNT(*) FROM moves m WHERE m.game_id = g.game_id) >= 2
        ORDER  BY g.end_time DESC, g.date_played DESC, g.game_id DESC
    """
    if scope.limit:
        sql += " LIMIT :limit"
        params["limit"] = scope.limit

    return [row[0] for row in conn.execute(text(sql), params)]


def unanalyzed(conn, game_ids: list[int], run_id: int) -> list[int]:
    """Drop the games already finished under this run.

    Re-running a scope is idempotent and cheap: widening a date window by a day
    should cost one day of evaluation, not the whole window again. Only
    'complete' counts — a 'partial' or 'failed' game is retried.
    """
    if not game_ids:
        return []

    done = {
        row[0]
        for row in conn.execute(
            text(
                "SELECT game_id FROM engine.game_coverage "
                "WHERE run_id = :run_id AND status = 'complete'"
            ),
            {"run_id": run_id},
        )
    }
    return [gid for gid in game_ids if gid not in done]
