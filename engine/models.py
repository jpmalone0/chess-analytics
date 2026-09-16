"""SQLAlchemy models for the sidecar engine database.

These live on their own Base, not app.database.Base, so that create_all() on
either database never reaches into the other.
"""

from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Text, text

from engine.db import Base, engine


class AnalysisRun(Base):
    """One evaluation run, pinning the settings that make results comparable.

    Evaluations from different depths are not interchangeable, so every stored
    position carries the run that produced it and aggregates filter by run.
    """

    __tablename__ = "analysis_runs"

    run_id         = Column(Integer, primary_key=True, autoincrement=True)
    engine_name    = Column(String(50), nullable=False)
    engine_version = Column(String(50), nullable=False)
    depth          = Column(Integer, nullable=False)
    hash_mb        = Column(Integer, nullable=False)
    threads        = Column(Integer, nullable=False)
    created_at     = Column(DateTime, nullable=False, default=datetime.utcnow)


class PositionEval(Base):
    """Ground truth: one row per position, always from White's point of view.

    Fixing the point of view here rather than at write time means a row means the
    same thing regardless of who was to move, and the conversion to the mover's
    perspective happens once, in derivation.

    A forced mate sets mate_in and leaves cp NULL. Folding mate into a large
    centipawn number would bake a clamp into ground truth; the clamp is a
    presentation choice and belongs in the view.

    game_id references the canonical database and cannot be a foreign key across
    files. Only scope.py produces these IDs, and only by reading them back out of
    the canonical database.
    """

    __tablename__ = "position_evals"

    run_id        = Column(Integer, ForeignKey("analysis_runs.run_id"), primary_key=True)
    game_id       = Column(Integer, primary_key=True)
    ply           = Column(Integer, primary_key=True)  # 0 = start; N = after ply N
    cp            = Column(Integer)
    mate_in       = Column(Integer)
    best_move_uci = Column(String(6))


class GameCoverage(Base):
    """What has been analyzed, at what settings, and how far it got.

    Without this, two overlapping runs at different depths are indistinguishable
    after the fact. It also serves as the resume point: a killed batch re-run
    skips whatever is already 'complete'.
    """

    __tablename__ = "game_coverage"

    run_id         = Column(Integer, ForeignKey("analysis_runs.run_id"), primary_key=True)
    game_id        = Column(Integer, primary_key=True)
    plies_analyzed = Column(Integer, nullable=False, default=0)
    status         = Column(String(20), nullable=False)  # complete | partial | failed
    error          = Column(Text)
    completed_at   = Column(DateTime)


# ═══════════════════════════════════════════════════════════
# Derivation
# ═══════════════════════════════════════════════════════════

# A mate is a game-theoretic fact, not a centipawn count, so position_evals
# stores the distance and leaves cp NULL. These constants turn it into a number
# only during derivation, where the choice can be revised without re-running the
# engine. The per-move penalty keeps a mate in 2 ahead of a mate in 8.
MATE_CP = 10000
MATE_STEP_CP = 100
MATE_MAX_PLIES = 50

# Loss is measured inside a clamped evaluation window. Once a game is decided,
# evaluations swing by thousands of centipawns and every later move books an
# enormous loss that says nothing about the player: measured on 400 real bullet
# games, leaving the window open put average loss at 208 cp, which is roughly
# double what a player at this rating actually plays like. Clamping to +/-1000
# puts the same games at 76.
#
# The clamp applies only to cp_loss. cp_before and cp_after stay unclamped,
# because "this position was already lost" is exactly the context that makes a
# small loss unimportant, and throwing it away would hide that.
EVAL_CLAMP_CP = 1000


# Centipawn loss is derived, never stored. Deciding that 300 centipawns is a
# "blunder" is an interpretation, and interpretations change; keeping thresholds
# out of the stored rows means revising them costs a view definition instead of
# a re-run of the whole corpus.
#
# The self-join pairs each position with the one before it, so a move's loss is
# the difference between the evaluation it inherited and the one it produced.
# That is why a game of N plies is stored as N+1 positions.
#
# Evaluations are stored from White's point of view. White wants cp to rise, so
# White's loss is before-minus-after; Black's is the reverse. The floor at zero
# absorbs search noise: at fixed depth a move can appear to *gain* evaluation,
# and a negative loss would drag every average it lands in. The difference is
# taken inside a clamped window, so a move played in an already-decided position
# cannot book a five-figure loss.
MOVE_EVALS_VIEW = f"""
CREATE VIEW IF NOT EXISTS move_evals AS
WITH scored AS (
    SELECT
        run_id,
        game_id,
        ply,
        CASE
            WHEN cp IS NOT NULL THEN cp
            -- mate_in = 0 is a delivered checkmate: the side to move is mated,
            -- and after an even ply that is White. Resolving the sign from ply
            -- parity keeps storage free of a signed sentinel.
            WHEN mate_in = 0 AND ply % 2 = 0 THEN -{MATE_CP}
            WHEN mate_in = 0 THEN {MATE_CP}
            WHEN mate_in > 0 THEN
                 {MATE_CP} - {MATE_STEP_CP} * MIN(mate_in, {MATE_MAX_PLIES})
            WHEN mate_in < 0 THEN
                -{MATE_CP} + {MATE_STEP_CP} * MIN(-mate_in, {MATE_MAX_PLIES})
        END AS cp_eff
    FROM position_evals
),
windowed AS (
    SELECT run_id, game_id, ply, cp_eff,
           MAX(-{EVAL_CLAMP_CP}, MIN({EVAL_CLAMP_CP}, cp_eff)) AS cp_capped
    FROM scored
)
SELECT
    after.run_id                                   AS run_id,
    after.game_id                                  AS game_id,
    after.ply                                      AS ply,
    CASE WHEN after.ply % 2 = 1 THEN 'white'
         ELSE 'black' END                          AS color,
    before.cp_eff                                  AS cp_before,
    after.cp_eff                                   AS cp_after,
    MAX(
        0,
        CASE WHEN after.ply % 2 = 1
             THEN before.cp_capped - after.cp_capped
             ELSE after.cp_capped - before.cp_capped
        END
    )                                              AS cp_loss
FROM windowed AS after
JOIN windowed AS before
  ON  before.run_id  = after.run_id
  AND before.game_id = after.game_id
  AND before.ply     = after.ply - 1
WHERE after.cp_eff IS NOT NULL AND before.cp_eff IS NOT NULL
"""


def init_engine_db():
    """Create the engine schema and the derivation view (idempotent).

    The view is dropped and rebuilt every time. It holds no data — it is a
    definition over position_evals — and CREATE VIEW IF NOT EXISTS would leave a
    database built by an older revision running the old thresholds while the
    code claims the new ones. Silently stale interpretation is the failure this
    design exists to avoid.
    """
    Base.metadata.create_all(bind=engine)
    with engine.begin() as conn:
        conn.execute(text("DROP VIEW IF EXISTS move_evals"))
        conn.execute(text(MOVE_EVALS_VIEW))
