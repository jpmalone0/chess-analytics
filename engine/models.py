"""SQLAlchemy models for the sidecar engine database.

These live on their own Base, not app.database.Base, so that create_all() on
either database never reaches into the other.
"""

from datetime import datetime

from sqlalchemy import Column, DateTime, Float, ForeignKey, Integer, String, Text, inspect, text
from sqlalchemy.exc import OperationalError

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
    # Denormalised from games.time_class, because SQLite refuses a view that
    # references an ATTACHed database ("view X cannot reference objects in
    # database engine"). Queries may cross the boundary; views may not. Keeping
    # the fact here is what lets every severity view stay pure sidecar.
    time_class     = Column(String(20))


class WpCurve(Base):
    """The fitted logistic turning centipawns into expected points.

    One row per time class, because the conversion is not universal: measured on
    this corpus, rapid fits k=360 and bullet fits k=865. A bullet advantage
    converts far less reliably than the same advantage in rapid, and using one
    curve for both overstates every bullet error.

    n, fitted_at and source exist so that refitting stays a deliberate, recorded
    act. A silent refit would move every historical count without anybody asking.
    """

    __tablename__ = "wp_curve"

    time_class = Column(String(20), primary_key=True)
    k          = Column(Float, nullable=False)
    n          = Column(Integer, nullable=False)
    fitted_at  = Column(DateTime)
    source     = Column(Text)


# Raw DDL for the same table, for tests that exercise the views as SQLite runs
# them rather than through the ORM.
WP_CURVE_DDL = """
CREATE TABLE IF NOT EXISTS wp_curve (
    time_class VARCHAR(20) PRIMARY KEY,
    k          FLOAT   NOT NULL,
    n          INTEGER NOT NULL,
    fitted_at  DATETIME,
    source     TEXT
)
"""


class MathFunctionsMissing(RuntimeError):
    """This SQLite build has no exp(), so no severity view can run."""


def assert_sqlite_has_math(conn) -> None:
    """Fail loudly at startup rather than obscurely inside a view.

    exp() is gated behind SQLITE_ENABLE_MATH_FUNCTIONS at compile time. Every
    view below is built on it, and a missing build otherwise surfaces as
    "no such function: exp" from whichever query happens to run first.

    Only that specific failure is translated. A locked database or a corrupt
    sidecar file also raises OperationalError, and relabelling those as a
    missing compile flag would send someone chasing a rebuild when the real
    problem is somewhere else entirely, so anything else is re-raised as itself.
    """
    try:
        conn.execute(text("SELECT exp(1.0)")).scalar()
    except OperationalError as exc:
        if "no such function: exp" not in str(exc):
            raise
        raise MathFunctionsMissing(
            "This SQLite build lacks exp(). Rebuild with "
            "SQLITE_ENABLE_MATH_FUNCTIONS (SQLite >= 3.35)."
        ) from exc


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


# Columns added to an existing table after it was first created. create_all()
# only creates missing *tables*, so a sidecar made before one of these columns
# existed keeps working but silently lacks it. Mirrors app.database's
# _ADDED_COLUMNS -- same problem, same shape, so the next column is a dict
# entry rather than a new hardcoded function.
_ADDED_ENGINE_COLUMNS = {
    "game_coverage": {"time_class": "VARCHAR(20)"},
}


def _add_missing_engine_columns():
    """Bring an existing sidecar up to the current model (idempotent)."""
    inspector = inspect(engine)
    for table, columns in _ADDED_ENGINE_COLUMNS.items():
        if table not in inspector.get_table_names():
            continue
        existing = {c["name"] for c in inspector.get_columns(table)}
        with engine.begin() as conn:
            for name, ddl_type in columns.items():
                if name not in existing:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl_type}"))


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
        conn.execute(text("DROP VIEW IF EXISTS move_errors"))
        conn.execute(text("DROP VIEW IF EXISTS move_evals"))
        conn.execute(text(MOVE_EVALS_VIEW))
        conn.execute(text(MOVE_ERRORS_VIEW))


# ═══════════════════════════════════════════════════════════
# Move features
# ═══════════════════════════════════════════════════════════

class PlayedMoveFeatures(Base):
    """What kind of move was actually played.

    Keyed by game, not by run: whether a move is a capture does not depend on
    engine depth. That split is what keeps the elite comparison reachable — the
    94k bullet games nobody will ever evaluate can still be classified.
    """

    __tablename__ = "played_move_features"

    game_id      = Column(Integer, primary_key=True)
    ply          = Column(Integer, primary_key=True)
    piece        = Column(String(1), nullable=False)   # P N B R Q K
    is_capture   = Column(Integer, nullable=False)
    gives_check  = Column(Integer, nullable=False)
    is_castling  = Column(Integer, nullable=False)
    is_promotion = Column(Integer, nullable=False)


class BestMoveFeatures(Base):
    """What the engine wanted instead, classified identically.

    Run-scoped, because which move is "best" depends on the depth that found it.
    A ply whose best move is NULL (terminal positions) or unparseable gets no row
    at all rather than a guess.
    """

    __tablename__ = "best_move_features"

    run_id       = Column(Integer, ForeignKey("analysis_runs.run_id"), primary_key=True)
    game_id      = Column(Integer, primary_key=True)
    ply          = Column(Integer, primary_key=True)   # the ply this move would have been
    piece        = Column(String(1), nullable=False)
    is_capture   = Column(Integer, nullable=False)
    gives_check  = Column(Integer, nullable=False)
    is_castling  = Column(Integer, nullable=False)
    is_promotion = Column(Integer, nullable=False)


# ═══════════════════════════════════════════════════════════
# Style
# ═══════════════════════════════════════════════════════════

class PositionFeatures(Base):
    """Board-derived metrics at a fixed ply, one row per game per colour.

    Keyed by game, not by run: whether a position has more space does not depend
    on engine depth. That split is what makes the whole 203k-game corpus
    reachable -- these never need Stockfish.
    """

    __tablename__ = "position_features"

    game_id        = Column(Integer, primary_key=True)
    color          = Column(String(5), primary_key=True)   # white | black
    space          = Column(Float, nullable=False)
    mobility       = Column(Float, nullable=False)
    king_safety    = Column(Float, nullable=False)
    pawn_structure = Column(Float, nullable=False)


class StyleCellMeans(Base):
    """The centring reference: mean metric values per (time class, opening, colour).

    Every value is read relative to this. Without it, colour alone produces an
    effect above t=7, because White has more space than Black -- a fact about
    chess rather than about a player.

    A row with eco3 = '*' is the coarse fallback for openings too rare to have a
    reliable cell of their own.
    """

    __tablename__ = "style_cell_means"

    time_class     = Column(String(20), primary_key=True)
    eco3           = Column(String(3), primary_key=True)
    color          = Column(String(5), primary_key=True)
    n              = Column(Integer, nullable=False)
    space          = Column(Float, nullable=False)
    mobility       = Column(Float, nullable=False)
    king_safety    = Column(Float, nullable=False)
    pawn_structure = Column(Float, nullable=False)


class PlayerStyleVectors(Base):
    """Full-history centred vectors for the reference population.

    The subject's own vector is computed live against the current UI filters;
    these are not, because recomputing hundreds of reference players on every
    filter change is not affordable and their style is stable by construction --
    that stability is the finding this feature rests on.

    mean_elo is that player's average rating in that time class, which is how the
    2800+ blitz pool is selected.
    """

    __tablename__ = "player_style_vectors"

    player_id      = Column(Integer, primary_key=True)
    time_class     = Column(String(20), primary_key=True)
    n              = Column(Integer, nullable=False)
    mean_elo       = Column(Float, nullable=False)
    space          = Column(Float, nullable=False)
    mobility       = Column(Float, nullable=False)
    king_safety    = Column(Float, nullable=False)
    pawn_structure = Column(Float, nullable=False)


# A move is "forcing" if it captures or gives check. Crude on purpose: it is the
# distinction between a move that demands an answer and one that does not, which
# is what separates "walked past a winning capture" from "drifted in a quiet
# position". Nothing here tries to say whether the tactic was *sound* — the
# engine already said that, in cp_loss.
#
# No error threshold is applied. Every scored move appears with its cp_loss and
# its kind; deciding that 150 cp is an error is the caller's call, the same way
# blunder thresholds are. A threshold in stored rows is one nobody can revise.
MOVE_ERRORS_VIEW = """
CREATE VIEW IF NOT EXISTS move_errors AS
SELECT
    e.run_id                                        AS run_id,
    e.game_id                                       AS game_id,
    e.ply                                           AS ply,
    e.color                                         AS color,
    e.cp_loss                                       AS cp_loss,
    -- Carried through from move_evals: whether the game was still live is the
    -- context that decides whether an error mattered. Without it, the most
    -- natural question about a missed tactic -- "was it winnable at the time?"
    -- -- forces callers back to move_evals and a second join.
    e.cp_before                                     AS cp_before,
    e.cp_after                                      AS cp_after,
    p.piece                                         AS played_piece,
    b.piece                                         AS best_piece,
    (p.is_capture OR p.gives_check)                 AS played_forcing,
    (b.is_capture OR b.gives_check)                 AS best_forcing,
    CASE
        WHEN (b.is_capture OR b.gives_check)
         AND NOT (p.is_capture OR p.gives_check) THEN 'missed_forcing'
        WHEN (p.is_capture OR p.gives_check)
         AND NOT (b.is_capture OR b.gives_check) THEN 'forced_when_quiet_better'
        ELSE 'other'
    END                                             AS error_kind
FROM       move_evals            AS e
JOIN       played_move_features  AS p
       ON  p.game_id = e.game_id AND p.ply = e.ply
JOIN       best_move_features    AS b
       ON  b.run_id  = e.run_id  AND b.game_id = e.game_id AND b.ply = e.ply
"""


# The tier ladder, in expected points lost. These are chess.com's published
# cutoffs, and they are comparable because our curve is fitted against results
# with draws scored 0.5 -- which is expected points, the same quantity their
# thresholds are denominated in. Lichess uses 0.15 for a blunder rather than
# 0.20; changing it is this constant and nothing else.
#
# Severity is a change in expected result, not in centipawns. A 300cp drop is
# worth 0.083 from +900 and 0.204 from +200: the same centipawns, two and a half
# times the cost. Every count in this feature is gated on the second number.
BLUNDER_WP = 0.20
MISTAKE_WP = 0.10
INACCURACY_WP = 0.05

MOVE_SEVERITY_VIEW = f"""
CREATE VIEW IF NOT EXISTS move_severity AS
WITH scored AS (
    SELECT
        e.run_id,
        e.game_id,
        e.ply,
        e.color,
        e.cp_before,
        e.cp_after,
        -- move_evals exposes cp_before/cp_after unclamped so that "this was
        -- already lost" survives. The curve needs the clamped window, and
        -- +/-1000 is the same ceiling Lichess applies before its own curve.
        MAX(-{EVAL_CLAMP_CP}, MIN({EVAL_CLAMP_CP}, e.cp_before)) AS cb,
        MAX(-{EVAL_CLAMP_CP}, MIN({EVAL_CLAMP_CP}, e.cp_after))  AS ca,
        -- Evaluations are White-relative; a loss belongs to whoever moved.
        CASE WHEN e.color = 'white' THEN 1 ELSE -1 END           AS sgn,
        -- k is a FLOAT column, so the divisions below are float divisions.
        -- An integer k would truncate them into a plausible-looking wrong curve.
        w.k                                                      AS k
    FROM move_evals    AS e
    JOIN game_coverage AS c
      ON  c.run_id = e.run_id AND c.game_id = e.game_id
    JOIN wp_curve      AS w
      ON  w.time_class = c.time_class
),
curved AS (
    SELECT
        run_id, game_id, ply, color, cp_before, cp_after,
        1.0 / (1.0 + exp(-(sgn * cb) / k)) AS wp_before,
        1.0 / (1.0 + exp(-(sgn * ca) / k)) AS wp_after
    FROM scored
),
-- wp_loss is computed here rather than in the SELECT below because SQLite
-- cannot reference a SELECT-list alias elsewhere in the same SELECT list, and
-- the tier ladder needs it three more times.
lost AS (
    SELECT
        run_id, game_id, ply, color, cp_before, cp_after, wp_before, wp_after,
        -- The floor at zero absorbs search noise: at fixed depth a move can
        -- appear to gain evaluation, and a negative loss would drag every
        -- average it lands in.
        MAX(0.0, wp_before - wp_after) AS wp_loss
    FROM curved
)
SELECT
    run_id,
    game_id,
    ply,
    color,
    cp_before,
    cp_after,
    wp_before,
    wp_after,
    wp_loss,
    CASE
        WHEN wp_loss >= {BLUNDER_WP}    THEN 'blunder'
        WHEN wp_loss >= {MISTAKE_WP}    THEN 'mistake'
        WHEN wp_loss >= {INACCURACY_WP} THEN 'inaccuracy'
    END AS tier
FROM lost
"""
