"""
Backfill games.variant for rows loaded before the column existed.

Everything already in the database has variant NULL, which the queries read as
standard chess. That is right for the overwhelming majority of rows and wrong
for the handful of Chess960 / bughouse / odds games mixed into chess.com's
archives — those carry Elo from a separate pool and, left unmarked, draw a
second rating series on top of the standard one.

The PGN is not stored, so the variant cannot be recovered locally: this
re-fetches the player's monthly archives and marks the games it finds. Only
non-standard games are written, so a player with none costs nothing but the
HTTP round-trips.

Usage:
    uv run python -m etl.backfill_variants danielnaroditsky [ballasack6 ...]
    uv run python -m etl.backfill_variants --since 2025-01 danielnaroditsky
    uv run python -m etl.backfill_variants --all          # every player in the DB
"""

import argparse
import sys
import time
from typing import Iterable, Optional, cast

import httpx
from sqlalchemy import CursorResult, text
from sqlalchemy.orm import Session

from app.database import SessionLocal, init_db

HEADERS = {"User-Agent": "ChessAnalytics/1.0 (student project)"}


def _slug(rules: str) -> str:
    """chess.com's rules value as the slug the parser would have stored."""
    return "".join(c for c in rules.lower() if c.isalnum())


def _archives(username: str, since: Optional[str]) -> list[str]:
    """Monthly archive URLs for a player, optionally from YYYY-MM onward."""
    resp = httpx.get(
        f"https://api.chess.com/pub/player/{username}/games/archives",
        headers=HEADERS, timeout=15.0,
    )
    resp.raise_for_status()
    urls = resp.json().get("archives", [])
    if since:
        year, month = since.split("-")
        # Archive URLs end in /YYYY/MM, so a zero-padded string compare works.
        cutoff = f"/{year}/{int(month):02d}"
        urls = [u for u in urls if u[-8:] >= cutoff]
    return urls


def backfill_player(db: Session, username: str, since: Optional[str] = None) -> dict:
    """Mark every non-standard game of one player. Returns a summary dict."""
    username = username.lower().strip()
    try:
        archives = _archives(username, since)
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            return {"username": username, "error": "not found on chess.com"}
        raise
    except httpx.RequestError as e:
        return {"username": username, "error": f"unreachable: {e}"}

    found = 0
    updated = 0
    missing = 0

    for archive_url in archives:
        try:
            resp = httpx.get(archive_url, headers=HEADERS, timeout=60.0)
            resp.raise_for_status()
        except Exception:
            continue  # a month we can't read leaves its rows as they were
        for game in resp.json().get("games", []):
            rules = game.get("rules")
            url = game.get("url")
            if not rules or rules == "chess" or not url:
                continue
            found += 1
            # Session.execute is typed as returning Result; an UPDATE always
            # gives back a CursorResult, which is the one that carries rowcount.
            result = cast(CursorResult, db.execute(
                text("UPDATE games SET variant = :variant "
                     "WHERE chess_com_url = :url AND variant IS NULL"),
                {"variant": _slug(rules), "url": url},
            ))
            if result.rowcount:
                updated += 1
            else:
                # Either already marked, or the game was never loaded.
                missing += 1
        db.commit()
        time.sleep(0.3)  # be a good citizen with the public API

    return {
        "username": username, "archives": len(archives),
        "variant_games_seen": found, "rows_marked": updated,
        "not_in_db_or_already_marked": missing,
    }


def _all_usernames(db: Session) -> Iterable[str]:
    return [r[0] for r in db.execute(text("SELECT username FROM players ORDER BY username"))]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("usernames", nargs="*", help="players to backfill")
    ap.add_argument("--since", help="only archives from this month onward (YYYY-MM)")
    ap.add_argument("--all", action="store_true",
                    help="every player in the database (slow: one API pass per player)")
    args = ap.parse_args()

    init_db()  # ensures the variant column exists
    db = SessionLocal()
    try:
        names = list(_all_usernames(db)) if args.all else args.usernames
        if not names:
            ap.error("give at least one username, or --all")
        for name in names:
            summary = backfill_player(db, name, args.since)
            if "error" in summary:
                print(f"{summary['username']}: {summary['error']}", file=sys.stderr)
            else:
                print(f"{summary['username']}: marked {summary['rows_marked']} of "
                      f"{summary['variant_games_seen']} variant games "
                      f"across {summary['archives']} archives")
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
