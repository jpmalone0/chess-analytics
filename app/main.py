"""
Chess Analytics — FastAPI Web Application

Run with: uv run uvicorn app.main:app --reload
"""

from fastapi import FastAPI, Depends, HTTPException, Query
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session
from typing import Optional
from datetime import date

from app.database import get_db, init_db
from app.models import Player, Game
from app import crud, schemas

import os

app = FastAPI(title="Chess Analytics", version="1.0.0")

# Serve static frontend files
STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
if os.path.isdir(STATIC_DIR):
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.on_event("startup")
def startup():
    init_db()


# ═══════════════════════════════════════════════════════════
# Frontend
# ═══════════════════════════════════════════════════════════

@app.get("/")
def serve_index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


# ═══════════════════════════════════════════════════════════
# Players
# ═══════════════════════════════════════════════════════════

@app.get("/api/players")
def list_players(search: Optional[str] = None, db: Session = Depends(get_db)):
    players = crud.get_players(db, search=search)
    return [schemas.PlayerOut.model_validate(p) for p in players]


@app.get("/api/players/{username}")
def get_player(username: str, db: Session = Depends(get_db)):
    player = crud.get_player(db, username)
    if not player:
        raise HTTPException(404, f"Player '{username}' not found")
    return schemas.PlayerOut.model_validate(player)


@app.get("/api/players/{username}/stats")
def player_stats(
    username: str,
    time_class: Optional[str] = None,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    db: Session = Depends(get_db),
):
    player = crud.get_player(db, username)
    if not player:
        raise HTTPException(404, f"Player '{username}' not found")
    return crud.get_player_stats(
        db, player.player_id,
        time_class=time_class, start_date=start_date, end_date=end_date,
    )


# ═══════════════════════════════════════════════════════════
# On-Demand Player Sync
# ═══════════════════════════════════════════════════════════

@app.post("/api/players/{username}/sync")
def sync_player(
    username: str,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    db: Session = Depends(get_db),
):
    """
    Pull games from chess.com for this player (within the date range)
    and load them into the database. Idempotent.
    """
    from etl.sync_player import sync_player as do_sync
    result = do_sync(db, username, start_date, end_date)

    if "error" in result:
        raise HTTPException(404, result["error"])

    return result


# ═══════════════════════════════════════════════════════════
# Games
# ═══════════════════════════════════════════════════════════

@app.get("/api/players/{username}/games")
def list_games(
    username: str,
    time_class: Optional[str] = None,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    player = crud.get_player(db, username)
    if not player:
        raise HTTPException(404, f"Player '{username}' not found")

    games = crud.get_games_for_player(
        db, player.player_id, time_class=time_class,
        start_date=start_date, end_date=end_date,
        limit=limit, offset=offset,
    )

    result = []
    for g in games:
        is_white = g.white_player_id == player.player_id
        result.append(schemas.GameBrief(
            game_id=g.game_id,
            opponent=g.black_player.username if is_white else g.white_player.username,
            player_color="white" if is_white else "black",
            result=g.result,
            date_played=g.date_played,
            time_class=g.time_class,
            player_elo=g.white_elo if is_white else g.black_elo,
            opponent_elo=g.black_elo if is_white else g.white_elo,
            total_moves=g.total_moves,
            opening_name=g.opening_name,
        ))

    return result


@app.get("/api/games/{game_id}")
def get_game(game_id: int, db: Session = Depends(get_db)):
    game = crud.get_game(db, game_id)
    if not game:
        raise HTTPException(404, "Game not found")
    return schemas.GameOut(
        game_id=game.game_id,
        white_username=game.white_player.username,
        black_username=game.black_player.username,
        result=game.result,
        date_played=game.date_played,
        time_control=game.time_control,
        time_class=game.time_class,
        white_elo=game.white_elo,
        black_elo=game.black_elo,
        white_accuracy=game.white_accuracy,
        black_accuracy=game.black_accuracy,
        eco=game.eco,
        opening_name=game.opening_name,
        termination=game.termination,
        chess_com_url=game.chess_com_url,
        total_moves=game.total_moves,
    )


@app.get("/api/games/{game_id}/moves")
def get_moves(game_id: int, db: Session = Depends(get_db)):
    moves = crud.get_game_moves(db, game_id)
    return [schemas.MoveOut.model_validate(m) for m in moves]


@app.delete("/api/games/{game_id}")
def delete_game(game_id: int, db: Session = Depends(get_db)):
    if not crud.delete_game(db, game_id):
        raise HTTPException(404, "Game not found")
    return {"deleted": True}





# ═══════════════════════════════════════════════════════════
# Analytics
# ═══════════════════════════════════════════════════════════

@app.get("/api/players/{username}/analytics/rating-diff")
def rating_diff(
    username: str,
    time_class: Optional[str] = None,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    player_color: Optional[str] = None,
    opening_names: Optional[str] = None,
    db: Session = Depends(get_db),
):
    player = crud.get_player(db, username)
    if not player:
        raise HTTPException(404, f"Player '{username}' not found")
    return crud.rating_differential(
        db, player.player_id, time_class,
        start_date, end_date, player_color, opening_names
    )


@app.get("/api/players/{username}/analytics/game-length")
def game_length(
    username: str,
    time_class: Optional[str] = None,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    player_color: Optional[str] = None,
    opening_names: Optional[str] = None,
    db: Session = Depends(get_db),
):
    player = crud.get_player(db, username)
    if not player:
        raise HTTPException(404, f"Player '{username}' not found")
    return crud.game_length_vs_winrate(
        db, player.player_id, time_class,
        start_date, end_date, player_color, opening_names
    )


@app.get("/api/players/{username}/analytics/time-remaining")
def time_remaining(
    username: str,
    time_class: Optional[str] = None,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    player_color: Optional[str] = None,
    opening_names: Optional[str] = None,
    db: Session = Depends(get_db),
):
    player = crud.get_player(db, username)
    if not player:
        raise HTTPException(404, f"Player '{username}' not found")
    return crud.time_remaining_vs_result(
        db, player.player_id, time_class,
        start_date, end_date, player_color, opening_names
    )


@app.get("/api/players/{username}/analytics/clock-advantage")
def clock_advantage(
    username: str,
    time_class: Optional[str] = None,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    player_color: Optional[str] = None,
    opening_names: Optional[str] = None,
    db: Session = Depends(get_db),
):
    player = crud.get_player(db, username)
    if not player:
        raise HTTPException(404, f"Player '{username}' not found")
    return crud.analyze_clock_advantage(
        db, player.player_id, time_class,
        start_date, end_date, player_color, opening_names
    )


@app.get("/api/players/{username}/analytics/elo-history")
def elo_history(
    username: str,
    time_class: Optional[str] = None,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    db: Session = Depends(get_db),
):
    player = crud.get_player(db, username)
    if not player:
        raise HTTPException(404, f"Player '{username}' not found")
    return crud.elo_history(
        db, player.player_id, time_class,
        start_date, end_date,
    )


@app.get("/api/players/{username}/analytics/top-openings")
def top_openings(
    username: str,
    time_class: Optional[str] = None,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    db: Session = Depends(get_db),
):
    player = crud.get_player(db, username)
    if not player:
        raise HTTPException(404, f"Player '{username}' not found")
    return crud.get_top_openings(
        db, player.player_id, time_class,
        start_date, end_date, limit=5
    )
