"""
Load parsed PGN data into the database.

Idempotent: skips games that already exist (by chess_com_url).
"""

import argparse
import os
import sys

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy.orm import Session

from app.database import SessionLocal, init_db
from app.models import Game, Move, Player
from etl.parse_pgn import parse_all_pgns


def get_or_create_player(db: Session, username: str) -> Player:
    """Get existing player or create a new one."""
    player = db.query(Player).filter(Player.username == username).first()
    if player is None:
        player = Player(username=username, platform="chess.com")
        db.add(player)
        db.flush()
    return player


def load_games(pgn_directory: str, batch_size: int = 500):
    """Parse PGN files and load them into the database."""
    init_db()
    db = SessionLocal()

    try:
        total_games = 0
        total_moves = 0
        skipped = 0

        print(f"Loading games from: {pgn_directory}")
        print()

        for game_dict, moves_list in parse_all_pgns(pgn_directory):
            # Skip if already loaded
            url = game_dict.get("chess_com_url")
            if url:
                exists = db.query(Game.game_id).filter(
                    Game.chess_com_url == url
                ).first()
                if exists:
                    skipped += 1
                    continue

            # Get or create players
            white_player = get_or_create_player(db, game_dict["white_username"])
            black_player = get_or_create_player(db, game_dict["black_username"])

            # Create game
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
            db.flush()  # get game_id

            # Create moves
            for move_dict in moves_list:
                move = Move(
                    game_id=game.game_id,
                    ply=move_dict["ply"],
                    move_number=move_dict["move_number"],
                    color=move_dict["color"],
                    move_san=move_dict["move_san"],
                    clock_seconds=move_dict["clock_seconds"],
                    time_spent_seconds=move_dict["time_spent_seconds"],
                )
                db.add(move)

            total_games += 1
            total_moves += len(moves_list)

            # Commit in batches
            if total_games % batch_size == 0:
                db.commit()
                print(f"    ... {total_games} games loaded so far")

        # Final commit
        db.commit()
        print()
        print("Done!")
        print(f"  Games loaded:  {total_games}")
        print(f"  Games skipped: {skipped} (already in DB)")
        print(f"  Moves loaded:  {total_moves}")

        # Print table counts
        print()
        print("  Table counts:")
        print(f"    players:    {db.query(Player).count()}")
        print(f"    games:      {db.query(Game).count()}")
        print(f"    moves:      {db.query(Move).count()}")

    except Exception as e:
        db.rollback()
        print(f"\nError: {e}")
        raise
    finally:
        db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Load PGN games into the database")
    parser.add_argument(
        "pgn_directory",
        nargs="?",
        default="ballasack6_games",
        help="Directory containing .pgn files (default: ballasack6_games)"
    )
    args = parser.parse_args()
    load_games(args.pgn_directory)
