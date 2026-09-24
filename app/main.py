"""
Chess Analytics — FastAPI Web Application

Run with: uv run uvicorn app.main:app --reload
"""

import os
from datetime import date
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session  # noqa: F401 — used via Depends(get_db)

from app import baselines, crud, schemas
from app import move_quality as mq
from app.database import get_db, init_db
from engine.analyze import DEFAULT_DEPTH, default_workers
from engine.cli import estimated_minutes
from engine.population import DEFAULT_TARGET_GAMES, MAX_TARGET_GAMES, JobRunner

app = FastAPI(title="Chess Analytics", version="1.0.0")

# Serve static frontend files
STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
if os.path.isdir(STATIC_DIR):
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.middleware("http")
async def no_store_api_responses(request: Request, call_next):
    """Analytics answers change whenever the database grows, so a browser
    holding a heuristically-cached copy will show stale player counts against a
    freshly-loaded dropdown. Nothing under /api is cacheable."""
    response = await call_next(request)
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


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
    return [schemas.PlayerOut.model_validate(dict(p)) for p in players]


@app.get("/api/players/{username}")
def get_player(username: str, db: Session = Depends(get_db)):
    player = crud.get_player(db, username)
    if not player:
        raise HTTPException(404, f"Player '{username}' not found")
    return schemas.PlayerOut.model_validate(dict(player))


@app.get("/api/players/{username}/stats")
def player_stats(
    username: str,
    time_class: Optional[str] = None,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    tz: Optional[str] = None,
    player_color: Optional[str] = None,
    opening_names: Optional[str] = None,
    db: Session = Depends(get_db),
):
    player = crud.get_player(db, username)
    if not player:
        raise HTTPException(404, f"Player '{username}' not found")
    return crud.get_player_stats(
        db, player.player_id,
        time_class=time_class, start_date=start_date, end_date=end_date,
        player_color=player_color, opening_names=opening_names, tz=tz,
    )


# ═══════════════════════════════════════════════════════════
# On-Demand Player Sync
# ═══════════════════════════════════════════════════════════

@app.post("/api/players/{username}/sync")
def sync_player(
    username: str,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    tz: Optional[str] = None,
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
    tz: Optional[str] = None,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    opening_names: Optional[str] = None,
    player_color: Optional[str] = None,
    db: Session = Depends(get_db),
):
    player = crud.get_player(db, username)
    if not player:
        raise HTTPException(404, f"Player '{username}' not found")

    games = crud.get_games_for_player(
        db, player.player_id, time_class=time_class,
        start_date=start_date, end_date=end_date,
        limit=limit, offset=offset,
        opening_names=opening_names,
        player_color=player_color, tz=tz,
    )

    result = []
    for g in games:
        is_white = bool(g["is_white"])
        result.append(schemas.GameBrief(
            game_id=g["game_id"],
            opponent=g["black_username"] if is_white else g["white_username"],
            player_color="white" if is_white else "black",
            result=g["result"],
            date_played=g["date_played"],
            time_class=g["time_class"],
            player_elo=g["white_elo"] if is_white else g["black_elo"],
            opponent_elo=g["black_elo"] if is_white else g["white_elo"],
            total_moves=g["total_moves"],
            opening_name=g["opening_name"],
        ))

    return result


@app.get("/api/games/{game_id}")
def get_game(game_id: int, db: Session = Depends(get_db)):
    game = crud.get_game(db, game_id)
    if not game:
        raise HTTPException(404, "Game not found")
    return schemas.GameOut(**dict(game))


@app.get("/api/games/{game_id}/moves")
def get_moves(game_id: int, db: Session = Depends(get_db)):
    moves = crud.get_game_moves(db, game_id)
    return [schemas.MoveOut.model_validate(dict(m)) for m in moves]


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
    tz: Optional[str] = None,
    player_color: Optional[str] = None,
    opening_names: Optional[str] = None,
    db: Session = Depends(get_db),
):
    player = crud.get_player(db, username)
    if not player:
        raise HTTPException(404, f"Player '{username}' not found")
    return crud.rating_differential(
        db, player.player_id, time_class,
        start_date, end_date, player_color, opening_names, tz=tz,
    )


@app.get("/api/players/{username}/analytics/game-length")
def game_length(
    username: str,
    time_class: Optional[str] = None,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    tz: Optional[str] = None,
    player_color: Optional[str] = None,
    opening_names: Optional[str] = None,
    db: Session = Depends(get_db),
):
    player = crud.get_player(db, username)
    if not player:
        raise HTTPException(404, f"Player '{username}' not found")
    return crud.game_length_vs_winrate(
        db, player.player_id, time_class,
        start_date, end_date, player_color, opening_names, tz=tz,
    )



@app.get("/api/players/{username}/analytics/clock-advantage")
def clock_advantage(
    username: str,
    time_class: Optional[str] = None,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    tz: Optional[str] = None,
    player_color: Optional[str] = None,
    opening_names: Optional[str] = None,
    db: Session = Depends(get_db),
):
    player = crud.get_player(db, username)
    if not player:
        raise HTTPException(404, f"Player '{username}' not found")
    return crud.analyze_clock_advantage(
        db, player.player_id, time_class,
        start_date, end_date, player_color, opening_names, tz=tz,
    )


@app.get("/api/players/{username}/analytics/elo-history")
def elo_history(
    username: str,
    time_class: Optional[str] = None,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    tz: Optional[str] = None,
    db: Session = Depends(get_db),
):
    player = crud.get_player(db, username)
    if not player:
        raise HTTPException(404, f"Player '{username}' not found")
    return crud.elo_history(
        db, player.player_id, time_class,
        start_date, end_date, tz=tz,
    )


@app.get("/api/players/{username}/analytics/move-time")
def move_time(
    username: str,
    time_class: Optional[str] = None,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    tz: Optional[str] = None,
    player_color: Optional[str] = None,
    opening_names: Optional[str] = None,
    db: Session = Depends(get_db),
):
    player = crud.get_player(db, username)
    if not player:
        raise HTTPException(404, f"Player '{username}' not found")
    return crud.move_time_stats(
        db, player.player_id, time_class,
        start_date, end_date, player_color, opening_names, tz=tz,
    )


@app.get("/api/players/{username}/analytics/winrate-by-color")
def winrate_by_color(
    username: str,
    time_class: Optional[str] = None,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    tz: Optional[str] = None,
    window_games: int = 30,
    player_color: Optional[str] = None,
    opening_names: Optional[str] = None,
    db: Session = Depends(get_db),
):
    player = crud.get_player(db, username)
    if not player:
        raise HTTPException(404, f"Player '{username}' not found")
    return crud.winrate_by_color_rolling(
        db, player.player_id, time_class, start_date, end_date, window_games=window_games, tz=tz,
        player_color=player_color, opening_names=opening_names,
    )


@app.get("/api/players/{username}/analytics/winrate-vs-opening")
def winrate_vs_opening(
    username: str,
    time_class: Optional[str] = None,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    tz: Optional[str] = None,
    window_games: int = 30,
    player_color: Optional[str] = None,
    opening_names: Optional[str] = None,
    db: Session = Depends(get_db),
):
    player = crud.get_player(db, username)
    if not player:
        raise HTTPException(404, f"Player '{username}' not found")
    return crud.winrate_vs_first_move_rolling(
        db, player.player_id, time_class, start_date, end_date, window_games=window_games, tz=tz,
        player_color=player_color, opening_names=opening_names,
    )


@app.get("/api/players/{username}/analytics/streak-reaction")
def streak_reaction(
    username: str,
    time_class: Optional[str] = None,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    tz: Optional[str] = None,
    db: Session = Depends(get_db),
):
    player = crud.get_player(db, username)
    if not player:
        raise HTTPException(404, f"Player '{username}' not found")
    return crud.streak_reaction(
        db, player.player_id, time_class,
        start_date, end_date, tz=tz,
    )


@app.get("/api/players/{username}/analytics/top-openings")
def top_openings(
    username: str,
    time_class: Optional[str] = None,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    tz: Optional[str] = None,
    limit: Optional[int] = Query(None, ge=1, description="omit for every opening family"),
    db: Session = Depends(get_db),
):
    player = crud.get_player(db, username)
    if not player:
        raise HTTPException(404, f"Player '{username}' not found")
    return crud.get_top_openings(
        db, player.player_id, time_class,
        start_date, end_date, limit=limit, tz=tz,
    )


@app.get("/api/players/{username}/analytics/style")
def style_profile(
    username: str,
    time_class: Optional[str] = None,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    tz: Optional[str] = None,
    player_color: Optional[str] = None,
    opening_names: Optional[str] = None,
    db: Session = Depends(get_db),
):
    player = crud.get_player(db, username)
    if not player:
        raise HTTPException(404, f"Player '{username}' not found")
    return crud.style_profile(
        db, player.player_id, time_class,
        start_date, end_date, player_color, opening_names, tz=tz,
    )


@app.get("/api/players/{username}/analytics/move-quality")
def move_quality_by_game(
    username: str,
    time_class: Optional[str] = None,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    tz: Optional[str] = None,
    player_color: Optional[str] = None,
    opening_names: Optional[str] = None,
    db: Session = Depends(get_db),
):
    player = crud.get_player(db, username)
    if not player:
        raise HTTPException(404, f"Player '{username}' not found")
    return mq.player_move_quality(
        db, player.player_id, time_class, start_date, end_date,
        player_color, opening_names, tz=tz,
    )


@app.get("/api/games/{game_id}/move-quality")
def game_move_quality(game_id: int, db: Session = Depends(get_db)):
    return mq.game_drill_list(db, game_id)


# ── Move-quality population ──────────────────────────────

_population_runner: Optional[JobRunner] = None


def get_population_runner() -> JobRunner:
    """The process's one job runner, built on first use.

    Lazy so that importing the app -- which every test does -- never opens the
    real sidecar. Tests replace this dependency with a runner over temporary
    files.
    """
    global _population_runner
    if _population_runner is None:
        from contextlib import contextmanager

        from app.database import engine as canonical
        from engine import db as engine_db
        from engine.analyze import RunConfig, analyze_games, get_or_create_run
        from engine.models import PopulationJob

        PopulationJob.__table__.create(bind=engine_db.engine, checkfirst=True)

        @contextmanager
        def connect():
            with canonical.connect() as conn:
                engine_db.attach_engine_db(conn)
                yield conn

        runner = JobRunner(
            sessions=engine_db.SessionLocal,
            connect=connect,
            run_id=lambda: get_or_create_run(RunConfig()),
            analyze=lambda ids, run_id, progress: analyze_games(
                ids, RunConfig(), run_id=run_id, progress=progress),
        )
        runner.recover_interrupted()
        _population_runner = runner
    return _population_runner


def _mq_band_or_404(db, username, elo_band, time_class, start_date, end_date,
                    player_color, opening_names, tz):
    player = crud.get_player(db, username)
    if not player:
        raise HTTPException(404, f"Player '{username}' not found")
    band = mq.resolve_mq_band(
        db, player.player_id, elo_band, time_class, start_date, end_date,
        player_color, opening_names, tz,
    )
    return player, band


@app.get("/api/players/{username}/analytics/move-quality/baseline")
def move_quality_baseline(
    username: str,
    time_class: Optional[str] = None,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    tz: Optional[str] = None,
    player_color: Optional[str] = None,
    opening_names: Optional[str] = None,
    elo_band: Optional[str] = None,
    db: Session = Depends(get_db),
    runner: JobRunner = Depends(get_population_runner),
):
    """The pooled rate for the Compare-to band, plus what pressing would cost."""
    player, band = _mq_band_or_404(
        db, username, elo_band, time_class, start_date, end_date,
        player_color, opening_names, tz)
    if band is None:
        return {"band": None}
    out = mq.band_move_quality(
        db, band, player.player_id, player_color, opening_names)
    out["job"] = runner.active_job(band["time_class"], band["elo_lo"], band["elo_hi"])
    out["default_games"] = DEFAULT_TARGET_GAMES
    out["estimated_minutes"] = round(
        estimated_minutes(DEFAULT_TARGET_GAMES, default_workers(), DEFAULT_DEPTH), 1)
    return out


@app.post("/api/players/{username}/analytics/move-quality/population")
def analyze_population(
    username: str,
    time_class: Optional[str] = None,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    tz: Optional[str] = None,
    player_color: Optional[str] = None,
    opening_names: Optional[str] = None,
    elo_band: Optional[str] = None,
    games: int = Query(DEFAULT_TARGET_GAMES, ge=1, le=MAX_TARGET_GAMES),
    db: Session = Depends(get_db),
    runner: JobRunner = Depends(get_population_runner),
):
    """Queue an engine job for the Compare-to band, or return the one in flight.

    The band is pinned here, at press time: the date range moves a derived
    band by up to 500 points, and coverage must not move with it.
    """
    player, band = _mq_band_or_404(
        db, username, elo_band, time_class, start_date, end_date,
        player_color, opening_names, tz)
    if band is None:
        raise HTTPException(422, "No band to analyze under these filters")
    job, created = runner.enqueue(
        time_class=band["time_class"], elo_lo=band["elo_lo"], elo_hi=band["elo_hi"],
        target_games=games, exclude_player_id=player.player_id,
    )
    return {"job": job, "created": created}


@app.get("/api/population/jobs")
def population_jobs(runner: JobRunner = Depends(get_population_runner)):
    return {"jobs": runner.jobs()}


# ── Population Baselines ─────────────────────────────────

def _baseline_response(db, username, fn, *, time_class, start_date, end_date,
                       player_color, opening_names, elo_band, tz=None):
    player = crud.get_player(db, username)
    if not player:
        raise HTTPException(404, f"Player '{username}' not found")
    band = baselines.resolve_band(
        db, player.player_id, time_class=time_class,
        start_date=start_date, end_date=end_date,
        player_color=player_color, opening_names=opening_names, tz=tz,
        selected_band=None if elo_band in (None, "", "all") else int(elo_band),
        whole_population=(elo_band == "all"),
    )
    data = fn(db, player.player_id, band, time_class, player_color, opening_names)
    if data is None:
        return {"data": None, "meta": None}
    return {"data": data, "meta": baselines.band_meta(band)}


@app.get("/api/players/{username}/analytics/baseline-bands")
def baseline_bands(
    username: str,
    time_class: Optional[str] = None,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    tz: Optional[str] = None,
    player_color: Optional[str] = None,
    opening_names: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """Elo bands with enough data to serve as a baseline under these filters."""
    player = crud.get_player(db, username)
    if not player:
        raise HTTPException(404, f"Player '{username}' not found")

    tc = baselines.dominant_time_control(
        db, player.player_id, time_class=time_class,
        start_date=start_date, end_date=end_date,
        player_color=player_color, opening_names=opening_names, tz=tz,
    )
    bands = baselines.available_bands(
        db, player.player_id, time_class=time_class, time_control=tc,
        player_color=player_color, opening_names=opening_names,
    )
    median = baselines.player_median_elo(
        db, player.player_id, time_class=time_class,
        start_date=start_date, end_date=end_date,
        player_color=player_color, opening_names=opening_names, tz=tz,
    )
    # The band the charts will actually use when no band is picked. Returned so
    # the dropdown's default entry can name a concrete range rather than a
    # placeholder — it may be widened or class-level, which the label reflects.
    resolved = baselines.resolve_band(
        db, player.player_id, time_class=time_class,
        start_date=start_date, end_date=end_date,
        player_color=player_color, opening_names=opening_names, tz=tz,
    )
    return {
        "bands": bands,
        "player_band": (median // 100) * 100 if median is not None else None,
        "time_control": tc,
        "resolved": baselines.band_meta(resolved) if resolved else None,
        "all_players": baselines.whole_population_counts(
            db, player.player_id, time_class=time_class, time_control=tc,
            player_color=player_color, opening_names=opening_names,
        ),
    }


@app.get("/api/players/{username}/analytics/move-time/baseline")
def move_time_baseline_route(
    username: str,
    time_class: Optional[str] = None,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    tz: Optional[str] = None,
    player_color: Optional[str] = None,
    opening_names: Optional[str] = None,
    elo_band: Optional[str] = None,
    db: Session = Depends(get_db),
):
    return _baseline_response(
        db, username, baselines.move_time_baseline,
        time_class=time_class, start_date=start_date, end_date=end_date,
        player_color=player_color, opening_names=opening_names, elo_band=elo_band, tz=tz)


@app.get("/api/players/{username}/analytics/game-length/baseline")
def game_length_baseline_route(
    username: str,
    time_class: Optional[str] = None,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    tz: Optional[str] = None,
    player_color: Optional[str] = None,
    opening_names: Optional[str] = None,
    elo_band: Optional[str] = None,
    db: Session = Depends(get_db),
):
    return _baseline_response(
        db, username, baselines.game_length_baseline,
        time_class=time_class, start_date=start_date, end_date=end_date,
        player_color=player_color, opening_names=opening_names, elo_band=elo_band, tz=tz)


@app.get("/api/players/{username}/analytics/rating-diff/baseline")
def rating_diff_baseline_route(
    username: str,
    time_class: Optional[str] = None,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    tz: Optional[str] = None,
    player_color: Optional[str] = None,
    opening_names: Optional[str] = None,
    elo_band: Optional[str] = None,
    db: Session = Depends(get_db),
):
    return _baseline_response(
        db, username, baselines.rating_diff_baseline,
        time_class=time_class, start_date=start_date, end_date=end_date,
        player_color=player_color, opening_names=opening_names, elo_band=elo_band, tz=tz)


@app.get("/api/players/{username}/analytics/clock-advantage/baseline")
def clock_advantage_baseline_route(
    username: str,
    time_class: Optional[str] = None,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    tz: Optional[str] = None,
    player_color: Optional[str] = None,
    opening_names: Optional[str] = None,
    elo_band: Optional[str] = None,
    db: Session = Depends(get_db),
):
    return _baseline_response(
        db, username, baselines.clock_advantage_baseline,
        time_class=time_class, start_date=start_date, end_date=end_date,
        player_color=player_color, opening_names=opening_names, elo_band=elo_band, tz=tz)


@app.get("/api/players/{username}/analytics/streak-reaction/baseline")
def streak_baseline_route(
    username: str,
    time_class: Optional[str] = None,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    tz: Optional[str] = None,
    elo_band: Optional[str] = None,
    db: Session = Depends(get_db),
):
    return _baseline_response(
        db, username, baselines.streak_baseline,
        time_class=time_class, start_date=start_date, end_date=end_date,
        player_color=None, opening_names=None, elo_band=elo_band)
