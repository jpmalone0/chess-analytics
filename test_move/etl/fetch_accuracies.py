"""
Fetch per-game accuracy data from the chess.com JSON API
and update the games table.

Chess.com only provides accuracy for games that have been
analyzed (reviewed) by the player — roughly 24% of games
for this account.
"""

import sys
import os
import time
import requests
import argparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text
from app.database import SessionLocal, init_db
from app.models import Game


HEADERS = {
    "User-Agent": "MyChessStyle/1.0 (contact: jpmalone0@gmail.com)"
}


def fetch_accuracies(username: str, start_date: str = None, end_date: str = None):
    """Pull accuracy data from the chess.com JSON API and update the games table."""
    init_db()
    db = SessionLocal()

    try:
        # Get list of archives
        base_url = f"https://api.chess.com/pub/player/{username}/games/archives"
        resp = requests.get(base_url, headers=HEADERS)
        resp.raise_for_status()
        archives = resp.json().get("archives", [])

        # Filter by date range
        filtered = []
        for url in archives:
            parts = url.split('/')[-2:]
            year_month = f"{parts[0]}-{parts[1]}"
            if start_date and year_month < start_date:
                continue
            if end_date and year_month > end_date:
                continue
            filtered.append(url)

        print(f"Fetching accuracy data for {len(filtered)} monthly archives...")

        updated = 0
        not_found = 0

        for archive_url in filtered:
            date_parts = archive_url.split('/')[-2:]
            year_month = f"{date_parts[0]}-{date_parts[1]}"

            resp = requests.get(archive_url, headers=HEADERS)
            if resp.status_code != 200:
                print(f"  ✗ {year_month}: HTTP {resp.status_code}")
                continue

            games = resp.json().get("games", [])
            month_updated = 0

            for g in games:
                accuracies = g.get("accuracies")
                if not accuracies:
                    continue

                game_url = g.get("url")
                if not game_url:
                    continue

                # Update the game record
                result = db.query(Game).filter(
                    Game.chess_com_url == game_url
                ).first()

                if result is None:
                    not_found += 1
                    continue

                if result.white_accuracy != accuracies.get("white") or \
                   result.black_accuracy != accuracies.get("black"):
                    result.white_accuracy = accuracies.get("white")
                    result.black_accuracy = accuracies.get("black")
                    month_updated += 1

            db.commit()
            print(f"  ✓ {year_month}: {month_updated} games updated with accuracy")
            updated += month_updated

        print(f"\nDone: {updated} games updated with accuracy data")
        if not_found:
            print(f"  ({not_found} games in API not found in DB — may not be loaded yet)")

    finally:
        db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Fetch accuracy data from chess.com API")
    parser.add_argument("username", help="Chess.com username")
    parser.add_argument("--start", default=None, help="Start date YYYY-MM")
    parser.add_argument("--end", default=None, help="End date YYYY-MM")
    args = parser.parse_args()
    fetch_accuracies(args.username, args.start, args.end)
