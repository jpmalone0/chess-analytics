"""
SQLAlchemy ORM models — mirrors db/schema.sql.
"""

from datetime import datetime, date
from sqlalchemy import (
    Column, Integer, String, Float, Date, DateTime, Text,
    ForeignKey, UniqueConstraint
)
from sqlalchemy.orm import relationship
from app.database import Base


class Player(Base):
    __tablename__ = "players"

    player_id = Column(Integer, primary_key=True, autoincrement=True)
    username  = Column(String(100), nullable=False, unique=True)
    platform  = Column(String(20), nullable=False, default="chess.com")

    white_games = relationship("Game", foreign_keys="Game.white_player_id", back_populates="white_player")
    black_games = relationship("Game", foreign_keys="Game.black_player_id", back_populates="black_player")


class Game(Base):
    __tablename__ = "games"

    game_id          = Column(Integer, primary_key=True, autoincrement=True)
    white_player_id  = Column(Integer, ForeignKey("players.player_id"), nullable=False)
    black_player_id  = Column(Integer, ForeignKey("players.player_id"), nullable=False)
    result           = Column(String(10), nullable=False)
    date_played      = Column(Date)
    time_control     = Column(String(30))
    time_class       = Column(String(20))
    white_elo        = Column(Integer)
    black_elo        = Column(Integer)
    white_accuracy   = Column(Float)
    black_accuracy   = Column(Float)
    eco              = Column(String(10))
    opening_name     = Column(String(255))
    termination      = Column(String(255))
    chess_com_url    = Column(String(255), unique=True)
    total_moves      = Column(Integer)
    created_at       = Column(DateTime, default=datetime.utcnow)

    white_player = relationship("Player", foreign_keys=[white_player_id], back_populates="white_games")
    black_player = relationship("Player", foreign_keys=[black_player_id], back_populates="black_games")
    moves        = relationship("Move", back_populates="game", cascade="all, delete-orphan")


class Move(Base):
    __tablename__ = "moves"
    __table_args__ = (UniqueConstraint("game_id", "ply"),)

    move_id            = Column(Integer, primary_key=True, autoincrement=True)
    game_id            = Column(Integer, ForeignKey("games.game_id", ondelete="CASCADE"), nullable=False)
    ply                = Column(Integer, nullable=False)
    move_number        = Column(Integer, nullable=False)
    color              = Column(String(5), nullable=False)
    move_san           = Column(String(10), nullable=False)
    clock_seconds      = Column(Float)
    time_spent_seconds = Column(Float)

    game = relationship("Game", back_populates="moves")
