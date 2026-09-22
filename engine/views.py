"""The derivation layer: SQL views that interpret stored evaluations.

Nothing here holds data. position_evals stores what Stockfish said; everything
below decides what it *means* -- what counts as a blunder, how centipawns become
expected points, when an unpunished error is a Miss. Keeping those decisions in
view definitions rather than in stored rows is what makes them revisable: a new
threshold costs a rebuilt view, not a re-run of Stockfish over 17M plies.

The views form a tree rooted at move_evals, not a single chain -- move_errors
is a sibling branch, not a link in it. init_engine_db drops them deepest-first,
which is why the shape matters:

    move_evals            the self-join that turns positions into moves
    |
    +-- move_errors       move kinds, joined to the feature tables
    |
    +-- move_severity     the win-probability curve and the tier ladder
        |
        +-- move_quality  the Miss flag
            |
            +-- game_move_quality  per-game, per-colour counts

Imports go one way: this module imports engine.models, never the reverse.
"""

from sqlalchemy import inspect, text
from sqlalchemy.exc import OperationalError

# Imported for its side effect, not for a name: init_engine_db() below calls
# Base.metadata.create_all(), which creates only those tables whose classes have
# been imported and thereby registered on the metadata. Drop this import as
# "unused" and init_engine_db silently stops creating the sidecar's tables.
from engine import models  # noqa: F401
from engine.db import Base, engine

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


# Raw DDL for the wp_curve table, whose ORM twin is engine.models.WpCurve,
# for tests that exercise the views as SQLite runs them rather than through
# the ORM.
WP_CURVE_DDL = """
CREATE TABLE IF NOT EXISTS wp_curve (
    time_class VARCHAR(20) PRIMARY KEY,
    k          FLOAT   NOT NULL,
    n          INTEGER NOT NULL,
    fitted_at  DATETIME,
    source     TEXT,
    CONSTRAINT ck_wp_curve_k_positive CHECK (k > 0)
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
#
# MISTAKE_WP and INACCURACY_WP are reused below as MOVE_QUALITY_VIEW's Miss
# thresholds (MISS_HANDED_WP, MISS_RETURNED_WP). Retuning either one here also
# retunes what counts as a Miss.
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


# A Miss is the opponent's unpunished error, seen from the other side of the
# board: they handed over at least a mistake, and the reply gave at least an
# inaccuracy of it back.
#
# Chess.com's Miss is mutually exclusive with mistake and blunder. Ours is not,
# on purpose. Exclusivity needs an arbitrary precedence rule and destroys
# information -- a 0.40 blunder that was also a miss would be counted once,
# making blunders silently undercount. A flag keeps both facts and lets the
# caller cut either way.
MISS_HANDED_WP = MISTAKE_WP
MISS_RETURNED_WP = INACCURACY_WP

MOVE_QUALITY_VIEW = f"""
CREATE VIEW IF NOT EXISTS move_quality AS
SELECT
    s.run_id,
    s.game_id,
    s.ply,
    s.color,
    s.cp_before,
    s.cp_after,
    s.wp_before,
    s.wp_after,
    s.wp_loss,
    s.tier,
    -- COALESCE, not a bare comparison: ply 1 has no predecessor, and a NULL
    -- here would propagate into every count downstream as NULL rather than 0.
    COALESCE(
        prev.wp_loss >= {MISS_HANDED_WP} AND s.wp_loss >= {MISS_RETURNED_WP},
        0
    ) AS is_miss
FROM      move_severity AS s
LEFT JOIN move_severity AS prev
       ON prev.run_id  = s.run_id
      AND prev.game_id = s.game_id
      AND prev.ply     = s.ply - 1
"""


# One row per analyzed side of an analyzed game. Both colours are scored, so the
# colour is part of the key rather than a filter -- a caller that drops it is
# adding one player's errors to their opponent's.
GAME_MOVE_QUALITY_VIEW = """
CREATE VIEW IF NOT EXISTS game_move_quality AS
SELECT
    run_id,
    game_id,
    color,
    COUNT(*)                                          AS moves_scored,
    SUM(CASE WHEN tier = 'inaccuracy' THEN 1 ELSE 0 END) AS inaccuracies,
    SUM(CASE WHEN tier = 'mistake'    THEN 1 ELSE 0 END) AS mistakes,
    SUM(CASE WHEN tier = 'blunder'    THEN 1 ELSE 0 END) AS blunders,
    -- Overlaps the three above rather than partitioning them. Any caller
    -- presenting these as a total is presenting a wrong number.
    SUM(is_miss)                                      AS misses,
    SUM(wp_loss)                                      AS wp_lost
FROM  move_quality
GROUP BY run_id, game_id, color
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
    """Create the engine schema and the derivation views (idempotent).

    Views are dropped and rebuilt every time. They hold no data -- they are
    definitions over position_evals -- and CREATE VIEW IF NOT EXISTS would leave
    a database built by an older revision running the old thresholds while the
    code claims the new ones. Silently stale interpretation is the failure this
    design exists to avoid.

    Dropped in dependency order, deepest first: game_move_quality reads
    move_quality, which reads move_severity, which reads move_evals.
    """
    Base.metadata.create_all(bind=engine)
    _add_missing_engine_columns()
    with engine.begin() as conn:
        assert_sqlite_has_math(conn)
        for view in (
            "game_move_quality",
            "move_quality",
            "move_severity",
            "move_errors",
            "move_evals",
        ):
            conn.execute(text(f"DROP VIEW IF EXISTS {view}"))
        conn.execute(text(MOVE_EVALS_VIEW))
        conn.execute(text(MOVE_ERRORS_VIEW))
        conn.execute(text(MOVE_SEVERITY_VIEW))
        conn.execute(text(MOVE_QUALITY_VIEW))
        conn.execute(text(GAME_MOVE_QUALITY_VIEW))
