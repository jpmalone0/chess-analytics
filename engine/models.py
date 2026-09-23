"""SQLAlchemy models for the sidecar engine database.

These live on their own Base, not app.database.Base, so that create_all() on
either database never reaches into the other.
"""

from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
)

from engine.db import Base


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

    __table_args__ = (
        # A negative k inverts the curve: it grades a 400cp *gain* as a blunder
        # and a genuine 300cp mistake as no error at all, poisoning the counts
        # in both directions for that whole time class. k=0 divides by zero and
        # every severity goes NULL. Neither shows up as a failure anywhere --
        # the rows just come out wrong -- and k is written by an out-of-band
        # fitting process, so the schema is the only place to catch it.
        CheckConstraint("k > 0", name="ck_wp_curve_k_positive"),
    )


class PopulationJob(Base):
    """One press of the Analyze population button: a band, and how far it got.

    The band is recorded as it was at press time rather than re-derived, because
    the band a player's filters resolve to moves with the date range. What was
    actually sampled is games_total, which can fall short of target_games when a
    band runs out of unanalyzed games.
    """

    __tablename__ = "population_jobs"

    job_id            = Column(Integer, primary_key=True, autoincrement=True)
    time_class        = Column(String(20), nullable=False)
    elo_lo            = Column(Integer, nullable=False)
    elo_hi            = Column(Integer, nullable=False)
    exclude_player_id = Column(Integer)
    target_games      = Column(Integer, nullable=False)
    games_total       = Column(Integer)
    games_done        = Column(Integer, nullable=False, default=0)
    status            = Column(String(20), nullable=False)  # queued | running | complete | failed
    created_at        = Column(DateTime, nullable=False, default=datetime.utcnow)
    started_at        = Column(DateTime)
    finished_at       = Column(DateTime)
    error             = Column(Text)


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
