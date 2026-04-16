"""
Pydantic schemas for API request/response validation.
"""

from datetime import date
from typing import Optional

from pydantic import BaseModel


# ── Players ──────────────────────────────────────────────
class PlayerOut(BaseModel):
    player_id: int
    username: str
    platform: str

    class Config:
        from_attributes = True


# ── Games ────────────────────────────────────────────────
class GameOut(BaseModel):
    game_id: int
    white_username: Optional[str] = None
    black_username: Optional[str] = None
    result: str
    date_played: Optional[date] = None
    time_control: Optional[str] = None
    time_class: Optional[str] = None
    white_elo: Optional[int] = None
    black_elo: Optional[int] = None
    white_accuracy: Optional[float] = None
    black_accuracy: Optional[float] = None
    eco: Optional[str] = None
    opening_name: Optional[str] = None
    termination: Optional[str] = None
    chess_com_url: Optional[str] = None
    total_moves: Optional[int] = None

    class Config:
        from_attributes = True


class GameBrief(BaseModel):
    """Lightweight game for list views."""
    game_id: int
    opponent: str
    player_color: str
    result: str
    date_played: Optional[date] = None
    time_class: Optional[str] = None
    player_elo: Optional[int] = None
    opponent_elo: Optional[int] = None
    total_moves: Optional[int] = None
    opening_name: Optional[str] = None


# ── Moves ────────────────────────────────────────────────
class MoveOut(BaseModel):
    ply: int
    move_number: int
    color: str
    move_san: str
    clock_seconds: Optional[float] = None
    time_spent_seconds: Optional[float] = None

    class Config:
        from_attributes = True



