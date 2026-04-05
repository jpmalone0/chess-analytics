"""
On-demand player sync — pull games from chess.com API for a specific
date range, parse PGNs in-memory, and load into the database.

No local PGN files needed; works entirely via HTTP + in-memory parsing.
"""

import io
import re
import httpx
import chess.pgn
from datetime import date, datetime
from sqlalchemy.orm import Session

from app.models import Player, Game, Move
from etl.parse_pgn import _parse_clock, _parse_time_control, _classify_time_class, _extract_opening_name, _safe_int


def _get_or_create_player(db: Session, username: str) -> Player:
    """Get existing player or create a new one."""
    player = db.query(Player).filter(Player.username == username).first()
    if player is None:
        player = Player(username=username, platform="chess.com")
        db.add(player)
        db.flush()
    return player


def _archives_in_range(archives: list[str], start_date: date | None, end_date: date | None) -> list[str]:
    """Filter chess.com archive URLs to only those within the date range."""
    result = []
    for url in archives:
        # URL pattern: https://api.chess.com/pub/player/{username}/games/{YYYY}/{MM}
        parts = url.rstrip('/').split('/')
        try:
            year, month = int(parts[-2]), int(parts[-1])
        except (ValueError, IndexError):
            continue

        # Archive represents the entire month
        archive_start = date(year, month, 1)
        # Last day of this month
        if month == 12:
            archive_end = date(year + 1, 1, 1)
        else:
            archive_end = date(year, month + 1, 1)
        # archive_end is exclusive (first of next month), make inclusive
        from datetime import timedelta
        archive_end_inclusive = archive_end - timedelta(days=1)

        # Check overlap with requested range
        if start_date and archive_end_inclusive < start_date:
            continue
        if end_date and archive_start > end_date:
            continue
        result.append(url)

    return result


def _parse_pgn_text(pgn_text: str):
    """Parse PGN text (potentially multi-game) and yield (game_dict, moves_list)."""
    pgn_io = io.StringIO(pgn_text)

    while True:
        game = chess.pgn.read_game(pgn_io)
        if game is None:
            break

        headers = dict(game.headers)

        date_str = headers.get("UTCDate") or headers.get("Date")
        date_played = None
        if date_str:
            try:
                date_played = datetime.strptime(date_str, "%Y.%m.%d").date()
            except ValueError:
                pass

        tc = headers.get("TimeControl", "")
        eco_url = headers.get("ECOUrl")

        game_dict = {
            "white_username": headers.get("White", "").lower(),
            "black_username": headers.get("Black", "").lower(),
            "result":         headers.get("Result", "*"),
            "date_played":    date_played,
            "time_control":   tc,
            "time_class":     _classify_time_class(tc),
            "white_elo":      _safe_int(headers.get("WhiteElo")),
            "black_elo":      _safe_int(headers.get("BlackElo")),
            "eco":            headers.get("ECO"),
            "opening_name":   _extract_opening_name(eco_url),
            "termination":    headers.get("Termination"),
            "chess_com_url":  headers.get("Link"),
        }

        moves_list = []
        node = game
        ply = 0
        white_prev_clock = _parse_time_control(tc)
        black_prev_clock = _parse_time_control(tc)

        while node.variations:
            node = node.variation(0)
            ply += 1
            move_san = node.san()
            color = "white" if ply % 2 == 1 else "black"
            move_number = (ply + 1) // 2

            clock = _parse_clock(node.comment) if node.comment else None
            time_spent = None

            if clock is not None:
                if color == "white" and white_prev_clock is not None:
                    time_spent = max(0.0, white_prev_clock - clock)
                    white_prev_clock = clock
                elif color == "black" and black_prev_clock is not None:
                    time_spent = max(0.0, black_prev_clock - clock)
                    black_prev_clock = clock

            moves_list.append({
                "ply":                ply,
                "move_number":        move_number,
                "color":              color,
                "move_san":           move_san,
                "clock_seconds":      clock,
                "time_spent_seconds": time_spent,
            })

        game_dict["total_moves"] = (ply + 1) // 2
        yield game_dict, moves_list


def sync_player(
    db: Session,
    username: str,
    start_date: date | None = None,
    end_date: date | None = None,
) -> dict:
    """
    Pull games for a player from chess.com, filtered by date range.
    Parse and insert into the database. Idempotent via chess_com_url.

    Returns summary dict with counts.
    """
    username = username.lower().strip()

    # 1) Get archive list from chess.com
    headers = {"User-Agent": "ChessAnalytics/1.0 (student project)"}
    try:
        resp = httpx.get(
            f"https://api.chess.com/pub/player/{username}/games/archives",
            headers=headers,
            timeout=15.0,
        )
        resp.raise_for_status()
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            return {"error": f"Player '{username}' not found on chess.com", "games_loaded": 0}
        raise
    except httpx.RequestError as e:
        return {"error": f"Failed to reach chess.com: {e}", "games_loaded": 0}

    all_archives = resp.json().get("archives", [])
    archives = _archives_in_range(all_archives, start_date, end_date)

    if not archives:
        return {
            "username": username,
            "archives_checked": 0,
            "games_loaded": 0,
            "games_skipped": 0,
            "message": "No archives found for the specified date range",
        }

    # 2) Download and parse each monthly archive JSON
    total_loaded = 0
    total_skipped = 0

    for archive_url in archives:
        try:
            resp = httpx.get(archive_url, headers=headers, timeout=60.0)
            resp.raise_for_status()
        except Exception:
            continue  # skip failed months

        archive_data = resp.json()
        games_in_month = archive_data.get("games", [])
        urls = [g.get("url") for g in games_in_month if g.get("url")]
        
        # Bulk DB check for all games in the month
        existing_urls_tuple = db.query(Game.chess_com_url).filter(Game.chess_com_url.in_(urls)).all()
        existing_urls = {r[0] for r in existing_urls_tuple}

        for api_game in games_in_month:
            # Date-filter using end_time to avoid parsing out-of-bounds games
            end_time = api_game.get("end_time")
            if end_time:
                gd = date.fromtimestamp(end_time)
                if start_date and gd < start_date:
                    continue
                if end_date and gd > end_date:
                    continue

            url = api_game.get("url")
            if url and url in existing_urls:
                total_skipped += 1
                continue
            
            pgn_text = api_game.get("pgn")
            if not pgn_text:
                continue

            for game_dict, moves_list in _parse_pgn_text(pgn_text):
                # Filter one last time precisely if the actual date_played differs slightly
                gd2 = game_dict.get("date_played")
                if gd2:
                    if start_date and gd2 < start_date:
                        continue
                    if end_date and gd2 > end_date:
                        continue

                # Create players
                white_player = _get_or_create_player(db, game_dict["white_username"])
                black_player = _get_or_create_player(db, game_dict["black_username"])

                game = Game(
                    white_player_id=white_player.player_id,
                    black_player_id=black_player.player_id,
                    result=game_dict["result"],
                    date_played=game_dict["date_played"],
                    time_control=game_dict["time_control"],
                    time_class=game_dict["time_class"],
                    white_elo=game_dict["white_elo"],
                    black_elo=game_dict["black_elo"],
                    eco=game_dict["eco"],
                    opening_name=game_dict["opening_name"],
                    termination=game_dict["termination"],
                    chess_com_url=game_dict["chess_com_url"],
                    total_moves=game_dict["total_moves"],
                )
                db.add(game)
                db.flush()

                for move_dict in moves_list:
                    db.add(Move(
                        game_id=game.game_id,
                        ply=move_dict["ply"],
                        move_number=move_dict["move_number"],
                        color=move_dict["color"],
                        move_san=move_dict["move_san"],
                        clock_seconds=move_dict["clock_seconds"],
                        time_spent_seconds=move_dict["time_spent_seconds"],
                    ))

                total_loaded += 1

                if total_loaded % 200 == 0:
                    db.commit()

    db.commit()

    return {
        "username": username,
        "archives_checked": len(archives),
        "games_loaded": total_loaded,
        "games_skipped": total_skipped,
        "message": f"Synced {total_loaded} new games ({total_skipped} already in DB)",
    }
