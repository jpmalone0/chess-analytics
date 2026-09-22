"""Classify what kind of move was played, and what kind the engine wanted.

Engine evaluation says how much a move cost. It cannot tell walking past a
winning capture from drifting in a quiet position, and those want different
practice.

Extraction is deliberately separate from evaluation, even though the engine
worker already holds the board at every ply and could emit these for free. The
feature vocabulary will keep growing — pawn storms, shuffles, development timing
— and riding along with evaluation would mean re-running Stockfish to add "was
this a pawn push". At ~1,000 games/sec a second pass costs nothing worth
protecting.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional

import chess
from sqlalchemy import text

from engine.db import SessionLocal, analysis_engine
from engine.models import BestMoveFeatures, PlayedMoveFeatures
from engine.views import init_engine_db


@dataclass
class Summary:
    """What an extraction pass did."""

    games: int = 0
    played: int = 0
    best: int = 0
    skipped_best: int = 0   # NULL or unparseable engine recommendation
    unreplayable: int = 0


def classify(board: chess.Board, move: chess.Move) -> Optional[dict]:
    """Describe one move in the position it is played from.

    Returns None for a move that is not legal here, which is how an engine
    recommendation that does not parse gets dropped rather than guessed at.
    """
    piece = board.piece_at(move.from_square)
    if piece is None or move not in board.legal_moves:
        return None
    return {
        "piece": chess.piece_symbol(piece.piece_type).upper(),
        "is_capture": int(board.is_capture(move)),
        "gives_check": int(board.gives_check(move)),
        "is_castling": int(board.is_castling(move)),
        "is_promotion": int(move.promotion is not None),
    }


def _extract_game(game_id: int, sans: list[str], best_uci: dict[int, str], summary: Summary):
    """Replay one game, classifying the played move and the engine's pick at each ply.

    best_uci maps ply -> the recommendation for the position *after* that many
    plies, so the entry at ply-1 is what should have been played at ply.
    """
    board = chess.Board()
    played_rows, best_rows = [], []

    for i, san in enumerate(sans):
        ply = i + 1
        try:
            move = board.parse_san(san)
        except ValueError:
            # The stored game stops reconstructing here; keep what came before.
            summary.unreplayable += 1
            break

        feats = classify(board, move)
        if feats is not None:
            played_rows.append({"game_id": game_id, "ply": ply, **feats})

        uci = best_uci.get(ply - 1)
        if uci:
            try:
                best = chess.Move.from_uci(uci)
            except ValueError:
                best = None
            bfeats = classify(board, best) if best else None
            if bfeats is None:
                summary.skipped_best += 1
            else:
                best_rows.append({"game_id": game_id, "ply": ply, **bfeats})
        else:
            summary.skipped_best += 1

        board.push(move)

    return played_rows, best_rows


def extract_features(game_ids: Iterable[int], run_id: int) -> Summary:
    """Classify every move in these games. Idempotent: re-running replaces.

    Replacement rather than insert-if-missing is what makes a vocabulary change
    safe — after adding a column, re-extracting overwrites the old rows instead
    of leaving a mix of two definitions nobody can tell apart.
    """
    game_ids = list(game_ids)
    summary = Summary(games=len(game_ids))
    if not game_ids:
        return summary

    init_engine_db()
    canonical = analysis_engine()

    with canonical.connect() as conn, SessionLocal() as session:
        for game_id in game_ids:
            sans = [
                r[0] for r in conn.execute(
                    text("SELECT move_san FROM moves WHERE game_id = :g ORDER BY ply"),
                    {"g": game_id},
                )
            ]
            if not sans:
                continue

            best_uci = {
                r[0]: r[1] for r in conn.execute(
                    text("SELECT ply, best_move_uci FROM engine.position_evals "
                         "WHERE run_id = :r AND game_id = :g AND best_move_uci IS NOT NULL"),
                    {"r": run_id, "g": game_id},
                )
            }

            played_rows, best_rows = _extract_game(game_id, sans, best_uci, summary)

            session.query(PlayedMoveFeatures).filter_by(game_id=game_id).delete(
                synchronize_session=False)
            session.query(BestMoveFeatures).filter_by(
                run_id=run_id, game_id=game_id).delete(synchronize_session=False)

            session.bulk_save_objects([PlayedMoveFeatures(**r) for r in played_rows])
            session.bulk_save_objects(
                [BestMoveFeatures(run_id=run_id, **r) for r in best_rows])
            session.commit()

            summary.played += len(played_rows)
            summary.best += len(best_rows)

    return summary
