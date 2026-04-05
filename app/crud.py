"""
Database query functions for CRUD operations and analytics.
"""

from sqlalchemy.orm import Session
from sqlalchemy import func, case, and_, or_, text
from app.models import Player, Game, Move
from datetime import datetime, date


# ═══════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════

def _player_games_query(
    db: Session, player_id: int,
    time_class: str = None,
    start_date: date = None,
    end_date: date = None,
    player_color: str = None,
    opening_names: str = None,
):
    """Base query: all games for a player, filtered by time class, date range, color, and openings."""
    if player_color == "white":
        q = db.query(Game).filter(Game.white_player_id == player_id)
    elif player_color == "black":
        q = db.query(Game).filter(Game.black_player_id == player_id)
    else:
        q = db.query(Game).filter(
            or_(Game.white_player_id == player_id, Game.black_player_id == player_id)
        )
    if time_class:
        q = q.filter(Game.time_class == time_class)
    if start_date:
        q = q.filter(Game.date_played >= start_date)
    if end_date:
        q = q.filter(Game.date_played <= end_date)
    if opening_names:
        ops = [o.strip() for o in opening_names.split("|") if o.strip()]
        if ops:
            q = q.filter(Game.opening_name.in_(ops))
    return q


def _game_outcome(game, player_id: int) -> str:
    """Return 'win', 'loss', or 'draw' from the player's perspective."""
    is_white = game.white_player_id == player_id
    if game.result == "1-0":
        return "win" if is_white else "loss"
    elif game.result == "0-1":
        return "win" if not is_white else "loss"
    return "draw"


# ═══════════════════════════════════════════════════════════
# CRUD Operations
# ═══════════════════════════════════════════════════════════

# ── Players ───────────────────────────────────────────────

def get_players(db: Session, search: str = None, limit: int = 50):
    q = db.query(Player)
    if search:
        q = q.filter(Player.username.ilike(f"%{search}%"))
    return q.order_by(Player.username).limit(limit).all()


def get_player(db: Session, username: str):
    return db.query(Player).filter(Player.username == username).first()


# ── Games ─────────────────────────────────────────────────

def get_games_for_player(
    db: Session, player_id: int,
    time_class: str = None,
    start_date: date = None,
    end_date: date = None,
    limit: int = 50, offset: int = 0,
):
    q = _player_games_query(db, player_id, time_class, start_date, end_date)
    return q.order_by(Game.date_played.desc(), Game.game_id.desc()).offset(offset).limit(limit).all()


def get_game(db: Session, game_id: int):
    return db.query(Game).filter(Game.game_id == game_id).first()


def get_game_moves(db: Session, game_id: int):
    return db.query(Move).filter(Move.game_id == game_id).order_by(Move.ply).all()


def delete_game(db: Session, game_id: int) -> bool:
    game = db.query(Game).filter(Game.game_id == game_id).first()
    if game:
        db.delete(game)
        db.commit()
        return True
    return False


# ═══════════════════════════════════════════════════════════
# Analytics Queries
# ═══════════════════════════════════════════════════════════

def get_player_stats(
    db: Session, player_id: int,
    time_class: str = None,
    start_date: date = None,
    end_date: date = None,
):
    """Overall stats summary for a player, filtered by date range."""
    games = _player_games_query(db, player_id, time_class, start_date, end_date).all()

    total = len(games)
    wins = losses = draws = 0
    for g in games:
        outcome = _game_outcome(g, player_id)
        if outcome == "win": wins += 1
        elif outcome == "loss": losses += 1
        else: draws += 1

    by_tc = {}
    for g in games:
        tc = g.time_class or "unknown"
        if tc not in by_tc:
            by_tc[tc] = {"total": 0, "wins": 0, "losses": 0, "draws": 0}
        by_tc[tc]["total"] += 1
        outcome = _game_outcome(g, player_id)
        if outcome == "win": by_tc[tc]["wins"] += 1
        elif outcome == "loss": by_tc[tc]["losses"] += 1
        else: by_tc[tc]["draws"] += 1

    return {
        "total_games": total,
        "wins": wins,
        "losses": losses,
        "draws": draws,
        "win_rate": wins / total * 100 if total else 0,
        "by_time_class": by_tc,
    }


# ── Feature 1: Rating Differential ──────────────────────

def rating_differential(
    db: Session, player_id: int,
    time_class: str = None,
    start_date: date = None,
    end_date: date = None,
    player_color: str = None,
    opening_names: str = None,
):
    """
    Win rate bucketed by rating gap (player Elo - opponent Elo).
    Tight buckets around 0 since most games are within ±50 points.
    """
    games = _player_games_query(db, player_id, time_class, start_date, end_date, player_color, opening_names).all()

    bucket_defs = [
        ("> +100",       lambda d: d >= 100),
        ("+50 to +100",  lambda d: 50 <= d < 100),
        ("+40 to +50",   lambda d: 40 <= d < 50),
        ("+30 to +40",   lambda d: 30 <= d < 40),
        ("+20 to +30",   lambda d: 20 <= d < 30),
        ("+10 to +20",   lambda d: 10 <= d < 20),
        ("0 to +10",     lambda d: 0 <= d < 10),
        ("-10 to 0",     lambda d: -10 <= d < 0),
        ("-20 to -10",   lambda d: -20 <= d < -10),
        ("-30 to -20",   lambda d: -30 <= d < -20),
        ("-40 to -30",   lambda d: -40 <= d < -30),
        ("-50 to -40",   lambda d: -50 <= d < -40),
        ("-100 to -50",  lambda d: -100 <= d < -50),
        ("< -100",       lambda d: d < -100),
    ]

    buckets = {label: {"wins": 0, "losses": 0, "draws": 0} for label, _ in bucket_defs}

    for g in games:
        is_white = g.white_player_id == player_id
        my_elo = g.white_elo if is_white else g.black_elo
        opp_elo = g.black_elo if is_white else g.white_elo
        if not my_elo or not opp_elo:
            continue

        diff = my_elo - opp_elo
        outcome = _game_outcome(g, player_id)

        for label, test in bucket_defs:
            if test(diff):
                if outcome == "win": buckets[label]["wins"] += 1
                elif outcome == "loss": buckets[label]["losses"] += 1
                else: buckets[label]["draws"] += 1
                break

    results = []
    for label, _ in bucket_defs:
        b = buckets[label]
        total = b["wins"] + b["losses"] + b["draws"]
        decisive = b["wins"] + b["losses"]
        results.append({
            "bucket": label,
            "total_games": total,
            "wins": b["wins"],
            "losses": b["losses"],
            "draws": b["draws"],
            "win_rate": round(b["wins"] / total * 100, 1) if total else 0,
            "win_rate_no_draws": round(b["wins"] / decisive * 100, 1) if decisive else 0,
            "draw_rate": round(b["draws"] / total * 100, 1) if total else 0,
        })

    # Headline stats: underdog (>10 lower), favored (>10 higher), even (within 10)
    underdog_labels = {"< -100", "-100 to -50", "-50 to -40", "-40 to -30",
                       "-30 to -20", "-20 to -10"}
    favored_labels = {"+10 to +20", "+20 to +30", "+30 to +40",
                      "+40 to +50", "+50 to +100", "> +100"}
    even_labels = {"-10 to 0", "0 to +10"}

    ug = sum(r["total_games"] for r in results if r["bucket"] in underdog_labels)
    uw = sum(r["wins"] for r in results if r["bucket"] in underdog_labels)
    fg = sum(r["total_games"] for r in results if r["bucket"] in favored_labels)
    fw = sum(r["wins"] for r in results if r["bucket"] in favored_labels)
    eg = sum(r["total_games"] for r in results if r["bucket"] in even_labels)
    ew = sum(r["wins"] for r in results if r["bucket"] in even_labels)

    og = sum(r["total_games"] for r in results)
    ow = sum(r["wins"] for r in results)
    od = sum(r["draws"] for r in results)
    odec = sum(r["wins"] + r["losses"] for r in results)

    return {
        "buckets": results,
        "upset_rate": round(uw / ug * 100, 1) if ug else 0,
        "hold_rate": round(fw / fg * 100, 1) if fg else 0,
        "even_rate": round(ew / eg * 100, 1) if eg else 0,
        "overall_decisive_win_rate": round(ow / odec * 100, 1) if odec else 0,
        "overall_draw_rate": round(od / og * 100, 1) if og else 0,
    }


# ── Feature 2: Game Length vs Win Rate ───────────────────

def game_length_vs_winrate(
    db: Session, player_id: int,
    time_class: str = None,
    start_date: date = None,
    end_date: date = None,
    player_color: str = None,
    opening_names: str = None,
):
    """
    Win rate bucketed by total moves (game length).
    Answers: do I do better in quick games or long grinds?
    """
    games = _player_games_query(db, player_id, time_class, start_date, end_date, player_color, opening_names).all()

    bucket_defs = [
        ("1–10",   1,  10),
        ("11–20", 11,  20),
        ("21–30", 21,  30),
        ("31–40", 31,  40),
        ("41–50", 41,  50),
        ("51–60", 51,  60),
        ("61–80", 61,  80),
        ("80+",   81, 9999),
    ]

    buckets = {label: {"wins": 0, "losses": 0, "draws": 0} for label, _, _ in bucket_defs}

    for g in games:
        moves = g.total_moves
        if not moves:
            continue

        outcome = _game_outcome(g, player_id)
        for label, lo, hi in bucket_defs:
            if lo <= moves <= hi:
                if outcome == "win": buckets[label]["wins"] += 1
                elif outcome == "loss": buckets[label]["losses"] += 1
                else: buckets[label]["draws"] += 1
                break

    results = []
    for label, _, _ in bucket_defs:
        b = buckets[label]
        total = b["wins"] + b["losses"] + b["draws"]
        decisive = b["wins"] + b["losses"]
        results.append({
            "bucket": label,
            "total_games": total,
            "wins": b["wins"],
            "losses": b["losses"],
            "draws": b["draws"],
            "win_rate": round(b["wins"] / total * 100, 1) if total else 0,
            "win_rate_no_draws": round(b["wins"] / decisive * 100, 1) if decisive else 0,
            "draw_rate": round(b["draws"] / total * 100, 1) if total else 0,
        })

    return results


# ── Feature 3: Final Time Remaining vs Result ────────────

def time_remaining_vs_result(
    db: Session, player_id: int,
    time_class: str = None,
    start_date: date = None,
    end_date: date = None,
    player_color: str = None,
    opening_names: str = None,
):
    """
    Result distribution based on the player's final clock reading.
    Answers: when games end with <1 min on my clock, how often do I win?
    """
    games = _player_games_query(db, player_id, time_class, start_date, end_date, player_color, opening_names).all()

    bucket_defs = [
        ("10m+",   600, 99999),
        ("5–10m",  300,   600),
        ("3–5m",   180,   300),
        ("2–3m",   120,   180),
        ("1–2m",    60,   120),
        ("30s–1m",  30,    60),
        ("10–30s",  10,    30),
        ("< 10s",    0,    10),
    ]

    buckets = {label: {"wins": 0, "losses": 0, "draws": 0} for label, _, _ in bucket_defs}

    for game in games:
        is_white = game.white_player_id == player_id
        player_color = "white" if is_white else "black"

        # Get the LAST move's clock for this player (final clock reading)
        last_move = db.query(Move).filter(
            Move.game_id == game.game_id,
            Move.color == player_color,
            Move.clock_seconds.isnot(None),
        ).order_by(Move.ply.desc()).first()

        if not last_move:
            continue

        final_clock = last_move.clock_seconds
        outcome = _game_outcome(game, player_id)

        for label, lo, hi in bucket_defs:
            if lo <= final_clock < hi:
                if outcome == "win": buckets[label]["wins"] += 1
                elif outcome == "loss": buckets[label]["losses"] += 1
                else: buckets[label]["draws"] += 1
                break

    results = []
    for label, _, _ in bucket_defs:
        b = buckets[label]
        total = b["wins"] + b["losses"] + b["draws"]
        decisive = b["wins"] + b["losses"]
        results.append({
            "bucket": label,
            "total_games": total,
            "wins": b["wins"],
            "losses": b["losses"],
            "draws": b["draws"],
            "win_rate": round(b["wins"] / total * 100, 1) if total else 0,
            "win_rate_no_draws": round(b["wins"] / decisive * 100, 1) if decisive else 0,
            "draw_rate": round(b["draws"] / total * 100, 1) if total else 0,
        })

    return results


# ── Clock Advantage ──────────────────────────────────────

def analyze_clock_advantage(
    db: Session, player_id: int,
    time_class: str = None,
    start_date: date = None,
    end_date: date = None,
    player_color: str = None,
    opening_names: str = None,
):
    """
    For each game, compute the average clock advantage (player's time - opponent's time).
    Bucket into ahead/behind/even and compute win rates.
    """
    games = _player_games_query(db, player_id, time_class, start_date, end_date, player_color, opening_names).all()

    buckets = {
        "far_behind": {"wins": 0, "losses": 0, "draws": 0},
        "behind":     {"wins": 0, "losses": 0, "draws": 0},
        "even":       {"wins": 0, "losses": 0, "draws": 0},
        "ahead":      {"wins": 0, "losses": 0, "draws": 0},
        "far_ahead":  {"wins": 0, "losses": 0, "draws": 0},
    }

    for game in games:
        is_white = game.white_player_id == player_id
        player_color = "white" if is_white else "black"
        opp_color = "black" if is_white else "white"

        player_moves = {
            m.move_number: m.clock_seconds
            for m in db.query(Move).filter(
                Move.game_id == game.game_id,
                Move.color == player_color,
                Move.clock_seconds.isnot(None),
            ).all()
        }
        opp_moves = {
            m.move_number: m.clock_seconds
            for m in db.query(Move).filter(
                Move.game_id == game.game_id,
                Move.color == opp_color,
                Move.clock_seconds.isnot(None),
            ).all()
        }

        common_moves = set(player_moves.keys()) & set(opp_moves.keys())
        if not common_moves:
            continue

        advantages = [player_moves[mn] - opp_moves[mn] for mn in common_moves]
        avg_advantage = sum(advantages) / len(advantages)
        outcome = _game_outcome(game, player_id)

        if avg_advantage < -15: bucket = "far_behind"
        elif avg_advantage < -5: bucket = "behind"
        elif avg_advantage <= 5: bucket = "even"
        elif avg_advantage <= 15: bucket = "ahead"
        else: bucket = "far_ahead"

        if outcome == "win": buckets[bucket]["wins"] += 1
        elif outcome == "loss": buckets[bucket]["losses"] += 1
        else: buckets[bucket]["draws"] += 1

    result = []
    for label in ["far_behind", "behind", "even", "ahead", "far_ahead"]:
        b = buckets[label]
        total = b["wins"] + b["losses"] + b["draws"]
        decisive = b["wins"] + b["losses"]
        result.append({
            "clock_bucket": label,
            "total_games": total,
            "wins": b["wins"],
            "losses": b["losses"],
            "draws": b["draws"],
            "win_rate": round(b["wins"] / total * 100, 1) if total else 0,
            "win_rate_no_draws": round(b["wins"] / decisive * 100, 1) if decisive else 0,
            "draw_rate": round(b["draws"] / total * 100, 1) if total else 0,
        })

    return result


# ── Elo History ──────────────────────────────────────────

def elo_history(
    db: Session, player_id: int,
    time_class: str = None,
    start_date: date = None,
    end_date: date = None,
):
    """Get the player's Elo rating over time."""
    q = _player_games_query(db, player_id, time_class, start_date, end_date)
    games = q.order_by(Game.date_played.asc()).all()

    points = []
    for g in games:
        is_white = g.white_player_id == player_id
        elo = g.white_elo if is_white else g.black_elo
        if elo and g.date_played:
            points.append({
                "date": g.date_played.isoformat(),
                "elo": elo,
                "time_class": g.time_class,
            })

    return points


# ── Top Openings ─────────────────────────────────────────

def get_top_openings(
    db: Session, player_id: int,
    time_class: str = None,
    start_date: date = None,
    end_date: date = None,
    limit: int = 5
):
    """Returns top N opening names for white and black separately."""
    result = {"white": [], "black": []}
    
    for color in ["white", "black"]:
        q = db.query(Game.opening_name, func.count(Game.game_id).label("cnt"))
        if color == "white":
            q = q.filter(Game.white_player_id == player_id)
        else:
            q = q.filter(Game.black_player_id == player_id)
            
        if time_class:
            q = q.filter(Game.time_class == time_class)
        if start_date:
            q = q.filter(Game.date_played >= start_date)
        if end_date:
            q = q.filter(Game.date_played <= end_date)
            
        q = q.filter(Game.opening_name.isnot(None), Game.opening_name != "")\
             .group_by(Game.opening_name)\
             .order_by(func.count(Game.game_id).desc())\
             .limit(limit)
             
        result[color] = [row.opening_name for row in q.all()]
        
    return result

