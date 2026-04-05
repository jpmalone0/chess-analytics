"""
Parse PGN files into structured dicts ready for database insertion.

Each game yields:
  - game_dict: flat dict of game-level fields (headers, metadata)
  - moves_list: list of per-move dicts (SAN, clock, time_spent)

Clock computation:
  Chess.com PGNs include [%clk H:MM:SS.s] after each move.
  time_spent = previous_clock - current_clock for the same player.
  First move's time_spent = initial_clock - clock_after_first_move.
"""

import re
import os
import chess.pgn
import io
from datetime import datetime
from typing import Generator


def _parse_clock(comment: str) -> float | None:
    """Extract clock seconds from a PGN comment like '[%clk 0:02:59.9]'."""
    m = re.search(r'\[%clk\s+(\d+):(\d+):(\d+(?:\.\d+)?)\]', comment)
    if not m:
        return None
    h, mins, secs = int(m.group(1)), int(m.group(2)), float(m.group(3))
    return h * 3600 + mins * 60 + secs


def _parse_time_control(tc_str: str) -> float | None:
    """Parse time control string to get base time in seconds.
    
    Examples: '180' -> 180, '600+2' -> 600, '1/259200' -> daily
    """
    if not tc_str:
        return None
    # Daily chess
    if '/' in tc_str:
        return None
    # Base time, possibly with increment
    base = tc_str.split('+')[0]
    try:
        return float(base)
    except ValueError:
        return None


def _classify_time_class(tc_str: str) -> str:
    """Classify time control into bullet/blitz/rapid/daily."""
    base = _parse_time_control(tc_str)
    if base is None:
        return "daily"
    if base < 180:
        return "bullet"
    elif base < 600:
        return "blitz"
    else:
        return "rapid"


def _extract_opening_name(eco_url: str | None) -> str | None:
    """Extract human-friendly opening name from chess.com ECO URL."""
    if not eco_url:
        return None
    # https://www.chess.com/openings/Sicilian-Defense-Open-Accelerated-Dragon...
    parts = eco_url.rstrip('/').split('/')
    if len(parts) >= 2:
        slug = parts[-1]
        return slug.replace('-', ' ')
    return None


def parse_pgn_file(filepath: str) -> Generator[tuple[dict, list[dict]], None, None]:
    """
    Parse a single PGN file and yield (game_dict, moves_list) tuples.
    
    Handles multi-game PGN files (chess.com monthly archives).
    """
    with open(filepath, 'r', errors='replace') as f:
        content = f.read()

    pgn_io = io.StringIO(content)

    while True:
        game = chess.pgn.read_game(pgn_io)
        if game is None:
            break

        headers = dict(game.headers)

        # Parse date
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

        # Walk the move tree and extract moves + clocks
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

        game_dict["total_moves"] = (ply + 1) // 2  # full move count

        yield game_dict, moves_list


def parse_all_pgns(directory: str) -> Generator[tuple[dict, list[dict]], None, None]:
    """Parse all .pgn files in a directory."""
    pgn_files = sorted(
        f for f in os.listdir(directory) if f.endswith('.pgn')
    )
    for filename in pgn_files:
        filepath = os.path.join(directory, filename)
        print(f"  Parsing {filename}...", end="", flush=True)
        count = 0
        for game_dict, moves_list in parse_pgn_file(filepath):
            count += 1
            yield game_dict, moves_list
        print(f" {count} games")


def _safe_int(val) -> int | None:
    """Safely convert to int, returning None on failure."""
    if val is None:
        return None
    try:
        return int(val)
    except (ValueError, TypeError):
        return None
