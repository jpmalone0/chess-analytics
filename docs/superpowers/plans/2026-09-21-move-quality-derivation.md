# Move Quality (Derivation) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Per-game inaccuracy, mistake and blunder counts plus a Miss flag for every engine-analyzed game, derived from evaluations already on disk.

**Architecture:** A fitted win-probability curve per time class turns stored centipawns into expected points; three stacked SQLite views turn that into tiered moves, Miss flags and per-game counts. No new engine output. All views are pure-sidecar because SQLite refuses a view that references an ATTACHed database.

**Tech Stack:** Python 3.11, SQLAlchemy 2.0, SQLite (math functions required), FastAPI, pytest, vanilla JS.

**Spec:** [2026-09-21-move-quality-design.md](../specs/2026-09-21-move-quality-design.md)

**Scope:** This plan covers the derivation and display half only. The population sampler, the job runner and the "Analyze population" button are a second plan, written after this one lands.

---

## File Structure

| File | Responsibility |
|---|---|
| `engine/models.py` (modify) | `WpCurve` table, `time_class` on `GameCoverage`, three view constants, `init_engine_db` wiring |
| `engine/backfill.py` (create) | One idempotent cross-database backfill of `game_coverage.time_class` |
| `engine/analyze.py` (modify) | Record `time_class` on new coverage rows |
| `engine/fit_curve.py` (create) | Maximum-likelihood fit of `k` per time class, plus a CLI |
| `app/move_quality.py` (create) | Every query this feature makes. Kept out of `crud.py`, which is already 1,416 lines |
| `app/main.py` (modify) | Two routes, delegating to `app/move_quality.py` |
| `app/static/move-quality.js` (create) | The section's rendering, mirroring `style-panel.js` |
| `app/static/index.html` (modify) | The section's markup |
| `tests/test_move_severity.py` (create) | The curve and the tier ladder |
| `tests/test_move_quality_views.py` (create) | Miss and per-game aggregation |
| `tests/test_fit_curve.py` (create) | The fit |
| `tests/test_move_quality_api.py` (create) | Both routes |

---

## Task 1: The curve table and the `exp()` guard

`exp()` is a compile-time optional SQLite builtin. Every view in this plan depends on it, and a build without it would fail deep inside a query with a confusing message.

**Files:**
- Modify: `engine/models.py`
- Test: `tests/test_move_severity.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_move_severity.py`:

```python
"""The win-probability curve and the tier ladder.

Severity is a change in expected points, not in centipawns. Every threshold in
this feature lives in the view, so these tests are the only place the ladder is
pinned.
"""

import math

import pytest
from sqlalchemy import create_engine, text

from engine.models import (
    MOVE_EVALS_VIEW,
    MOVE_SEVERITY_VIEW,
    WP_CURVE_DDL,
    assert_sqlite_has_math,
)

RAPID_K = 360.0


def wp(cp, white=True):
    """The curve, in Python, for computing expectations independently."""
    sign = 1 if white else -1
    clamped = max(-1000, min(1000, cp))
    return 1 / (1 + math.exp(-(sign * clamped) / RAPID_K))


def test_sqlite_has_math_functions():
    eng = create_engine("sqlite://")
    with eng.connect() as conn:
        assert_sqlite_has_math(conn)
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_move_severity.py -v`
Expected: FAIL, `ImportError: cannot import name 'MOVE_SEVERITY_VIEW' from 'engine.models'`

- [ ] **Step 3: Add the table, the guard and the DDL constant**

In `engine/models.py`, after the `GameCoverage` class, add:

```python
class WpCurve(Base):
    """The fitted logistic turning centipawns into expected points.

    One row per time class, because the conversion is not universal: measured on
    this corpus, rapid fits k=360 and bullet k=865. A bullet advantage converts
    far less reliably than the same advantage in rapid, and using one curve for
    both overstates every bullet error.

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
    """
    try:
        conn.execute(text("SELECT exp(1.0)")).scalar()
    except Exception as exc:  # noqa: BLE001 — any failure here means the same thing
        raise MathFunctionsMissing(
            "This SQLite build lacks exp(). Rebuild with "
            "SQLITE_ENABLE_MATH_FUNCTIONS (SQLite >= 3.35)."
        ) from exc
```

Add `Float` to the existing `from sqlalchemy import ...` line at the top of the file if it is not already imported.

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/test_move_severity.py::test_sqlite_has_math_functions -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add engine/models.py tests/test_move_severity.py
git commit -m "feat: add the wp_curve table and a startup guard for exp()"
```

---

## Task 2: `time_class` on `game_coverage`

A view cannot read `games.time_class` across the ATTACH boundary, so the fact is recorded in the sidecar instead.

**Files:**
- Modify: `engine/models.py`
- Create: `engine/backfill.py`
- Test: `tests/test_move_quality_views.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_move_quality_views.py`:

```python
"""Backfill, Miss, and per-game aggregation."""

import pytest
from sqlalchemy import text

from engine.backfill import backfill_coverage_time_class


def test_backfill_copies_time_class_from_the_canonical_database(tmp_path, monkeypatch):
    """The column exists only because views cannot cross an ATTACH boundary."""
    from sqlalchemy import create_engine

    canon = tmp_path / "canon.db"
    side = tmp_path / "engine.db"

    c = create_engine(f"sqlite:///{canon}")
    with c.begin() as conn:
        conn.execute(text("CREATE TABLE games (game_id INTEGER PRIMARY KEY, time_class VARCHAR(20))"))
        conn.execute(text("INSERT INTO games VALUES (1, 'rapid'), (2, 'bullet')"))

    s = create_engine(f"sqlite:///{side}")
    with s.begin() as conn:
        conn.execute(text(
            "CREATE TABLE game_coverage ("
            " run_id INTEGER, game_id INTEGER, plies_analyzed INTEGER,"
            " status VARCHAR(20), error TEXT, completed_at DATETIME,"
            " time_class VARCHAR(20), PRIMARY KEY (run_id, game_id))"
        ))
        conn.execute(text(
            "INSERT INTO game_coverage (run_id, game_id, plies_analyzed, status) "
            "VALUES (1, 1, 80, 'complete'), (1, 2, 60, 'complete')"
        ))

    monkeypatch.setattr("engine.backfill.CANONICAL_DATABASE_URL", f"sqlite:///{canon}")
    monkeypatch.setattr("engine.backfill.ENGINE_DATABASE_URL", f"sqlite:///{side}")

    updated = backfill_coverage_time_class()
    assert updated == 2

    with s.connect() as conn:
        got = dict(conn.execute(text("SELECT game_id, time_class FROM game_coverage")).all())
    assert got == {1: "rapid", 2: "bullet"}


def test_backfill_is_idempotent(tmp_path, monkeypatch):
    """Running it twice must not rewrite rows that are already correct."""
    from sqlalchemy import create_engine

    canon = tmp_path / "canon.db"
    side = tmp_path / "engine.db"
    c = create_engine(f"sqlite:///{canon}")
    with c.begin() as conn:
        conn.execute(text("CREATE TABLE games (game_id INTEGER PRIMARY KEY, time_class VARCHAR(20))"))
        conn.execute(text("INSERT INTO games VALUES (1, 'rapid')"))
    s = create_engine(f"sqlite:///{side}")
    with s.begin() as conn:
        conn.execute(text(
            "CREATE TABLE game_coverage ("
            " run_id INTEGER, game_id INTEGER, plies_analyzed INTEGER,"
            " status VARCHAR(20), error TEXT, completed_at DATETIME,"
            " time_class VARCHAR(20), PRIMARY KEY (run_id, game_id))"
        ))
        conn.execute(text(
            "INSERT INTO game_coverage (run_id, game_id, plies_analyzed, status) "
            "VALUES (1, 1, 80, 'complete')"
        ))

    monkeypatch.setattr("engine.backfill.CANONICAL_DATABASE_URL", f"sqlite:///{canon}")
    monkeypatch.setattr("engine.backfill.ENGINE_DATABASE_URL", f"sqlite:///{side}")

    assert backfill_coverage_time_class() == 1
    assert backfill_coverage_time_class() == 0
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_move_quality_views.py -v`
Expected: FAIL, `ModuleNotFoundError: No module named 'engine.backfill'`

- [ ] **Step 3: Add the column and write the backfill**

In `engine/models.py`, add one column to `GameCoverage`, after `completed_at`:

```python
    # Denormalised from games.time_class, because SQLite refuses a view that
    # references an ATTACHed database ("view X cannot reference objects in
    # database engine"). Queries may cross the boundary; views may not. Keeping
    # the fact here is what lets every severity view stay pure sidecar.
    time_class     = Column(String(20))
```

`_add_missing_columns` in `app/database.py` covers the canonical database only, so add the same idempotent guard for the sidecar. Add this as a module-level function in `engine/models.py`, immediately above `init_engine_db` (Task 6 calls it):

```python
def _add_missing_engine_columns():
    """Bring an existing sidecar up to the current model (idempotent)."""
    with engine.begin() as conn:
        cols = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(game_coverage)")}
        if cols and "time_class" not in cols:
            conn.exec_driver_sql("ALTER TABLE game_coverage ADD COLUMN time_class VARCHAR(20)")
```

Create `engine/backfill.py`:

```python
"""One-off, idempotent backfill of game_coverage.time_class.

Runs across the ATTACH boundary, which is allowed for a statement even though it
is forbidden for a view definition. Existing coverage rows predate the column;
without this they would have a NULL time_class, and every severity view inner
joins wp_curve through it, so those games would silently vanish rather than
error.
"""

from sqlalchemy import create_engine, text

from engine.db import (
    CANONICAL_DATABASE_URL,
    ENGINE_DATABASE_URL,
    _sqlite_path,
)


def backfill_coverage_time_class() -> int:
    """Fill NULL time_class values. Returns the number of rows updated."""
    sidecar = create_engine(ENGINE_DATABASE_URL, connect_args={"check_same_thread": False})
    with sidecar.begin() as conn:
        conn.exec_driver_sql(
            f"ATTACH DATABASE '{_sqlite_path(CANONICAL_DATABASE_URL)}' AS canon"
        )
        try:
            result = conn.execute(text("""
                UPDATE game_coverage
                SET    time_class = (
                           SELECT g.time_class FROM canon.games g
                           WHERE  g.game_id = game_coverage.game_id
                       )
                WHERE  time_class IS NULL
            """))
            return int(result.rowcount)
        finally:
            conn.exec_driver_sql("DETACH DATABASE canon")
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_move_quality_views.py -v`
Expected: PASS, 2 passed

- [ ] **Step 5: Commit**

```bash
git add engine/models.py engine/backfill.py tests/test_move_quality_views.py
git commit -m "feat: record time_class on coverage rows and backfill existing ones"
```

---

## Task 3: The analyzer records `time_class`

The backfill covers the 1,271 rows already on disk. Without this, every new run writes NULL and its games drop out of the views.

**Files:**
- Modify: `engine/analyze.py:131-141`, `engine/analyze.py:163-177`, `engine/analyze.py:280-300`
- Test: `tests/test_engine_analyze.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_engine_analyze.py`:

```python
def test_game_time_class_reads_from_the_canonical_database(tmp_path):
    """New coverage rows carry the time class, so views need no cross-db join."""
    from sqlalchemy import create_engine, text

    from engine import analyze

    canon = tmp_path / "canon.db"
    eng = create_engine(f"sqlite:///{canon}")
    with eng.begin() as conn:
        conn.execute(text("CREATE TABLE games (game_id INTEGER PRIMARY KEY, time_class VARCHAR(20))"))
        conn.execute(text("INSERT INTO games VALUES (7, 'blitz')"))

    analyze._worker["db"] = eng
    assert analyze._game_time_class(7) == "blitz"
    assert analyze._game_time_class(999) is None
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_engine_analyze.py::test_game_time_class_reads_from_the_canonical_database -v`
Expected: FAIL, `AttributeError: module 'engine.analyze' has no attribute '_game_time_class'`

- [ ] **Step 3: Implement**

In `engine/analyze.py`, directly after `_game_moves`, add:

```python
def _game_time_class(game_id: int) -> Optional[str]:
    """The stored time class for one game, read from the canonical database."""
    with _worker["db"].connect() as conn:
        row = conn.execute(
            text("SELECT time_class FROM games WHERE game_id = :g"),
            {"g": game_id},
        ).first()
    return row[0] if row else None
```

Change `_analyze_one` to return it as a fifth element. Replace the body of `_analyze_one` with:

```python
def _analyze_one(game_id: int, depth: int):
    """Replay one game and evaluate every position it passes through.

    The engine is opened per game and closed by the with-block, while its event
    loop is still alive to process the shutdown. Nothing is left for atexit.
    """
    sans = _game_moves(game_id)
    time_class = _game_time_class(game_id)
    if not sans:
        # Checked before opening an engine: starting Stockfish to analyze a game
        # with no moves costs 125 ms to learn nothing.
        return game_id, [], "failed", "no moves stored", time_class

    with _open_engine() as proc:
        gid, rows, status, error = _evaluate_game(proc, game_id, sans, depth)
        return gid, rows, status, error, time_class
```

In `analyze_games`, change the unpacking and the `GameCoverage` construction:

```python
            game_id, rows, status, error, time_class = future.result()
```

```python
            session.merge(GameCoverage(
                run_id=run_id,
                game_id=game_id,
                plies_analyzed=len(rows),
                status=status,
                error=error,
                completed_at=datetime.utcnow(),
                time_class=time_class,
            ))
```

- [ ] **Step 4: Run the whole analyzer suite**

Run: `uv run pytest tests/test_engine_analyze.py -v`
Expected: PASS, all tests

- [ ] **Step 5: Commit**

```bash
git add engine/analyze.py tests/test_engine_analyze.py
git commit -m "feat: record time_class when the analyzer writes coverage"
```

---

## Task 4: The `move_severity` view

**Files:**
- Modify: `engine/models.py`
- Test: `tests/test_move_severity.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_move_severity.py`:

```python
@pytest.fixture
def sev():
    """An in-memory sidecar holding the tables and both views.

    Built from raw DDL rather than the ORM so that the view SQL is exercised
    exactly as SQLite will run it.
    """
    eng = create_engine("sqlite://")
    with eng.begin() as conn:
        conn.execute(text("""
            CREATE TABLE position_evals (
                run_id        INTEGER NOT NULL,
                game_id       INTEGER NOT NULL,
                ply           INTEGER NOT NULL,
                cp            INTEGER,
                mate_in       INTEGER,
                best_move_uci VARCHAR(6),
                PRIMARY KEY (run_id, game_id, ply)
            )
        """))
        conn.execute(text("""
            CREATE TABLE game_coverage (
                run_id         INTEGER NOT NULL,
                game_id        INTEGER NOT NULL,
                plies_analyzed INTEGER NOT NULL,
                status         VARCHAR(20) NOT NULL,
                error          TEXT,
                completed_at   DATETIME,
                time_class     VARCHAR(20),
                PRIMARY KEY (run_id, game_id)
            )
        """))
        conn.execute(text(WP_CURVE_DDL))
        conn.execute(text(MOVE_EVALS_VIEW))
        conn.execute(text(MOVE_SEVERITY_VIEW))
        conn.execute(text(
            "INSERT INTO wp_curve (time_class, k, n, source) VALUES ('rapid', 360.0, 45110, 'test')"
        ))
    return eng


def seed(eng, positions, game_id=1, run_id=1, time_class="rapid"):
    """positions: [(ply, cp, mate_in)] — evaluations from White's point of view."""
    with eng.begin() as conn:
        conn.execute(
            text("INSERT OR REPLACE INTO game_coverage "
                 "(run_id, game_id, plies_analyzed, status, time_class) "
                 "VALUES (:r, :g, :n, 'complete', :tc)"),
            {"r": run_id, "g": game_id, "n": len(positions), "tc": time_class},
        )
        for ply, cp, mate_in in positions:
            conn.execute(
                text("INSERT INTO position_evals (run_id, game_id, ply, cp, mate_in) "
                     "VALUES (:r, :g, :p, :cp, :m)"),
                {"r": run_id, "g": game_id, "p": ply, "cp": cp, "m": mate_in},
            )


def rows(eng, game_id=1):
    with eng.connect() as conn:
        return {
            r["ply"]: r
            for r in conn.execute(
                text("SELECT * FROM move_severity WHERE game_id = :g ORDER BY ply"),
                {"g": game_id},
            ).mappings()
        }


class TestCurve:
    def test_an_even_position_is_half_a_point(self, sev):
        seed(sev, [(0, 0, None), (1, 0, None)])
        assert rows(sev)[1]["wp_before"] == pytest.approx(0.5)

    def test_the_view_agrees_with_the_curve_computed_in_python(self, sev):
        seed(sev, [(0, 120, None), (1, -240, None)])
        r = rows(sev)[1]
        assert r["wp_before"] == pytest.approx(wp(120), abs=1e-9)
        assert r["wp_after"] == pytest.approx(wp(-240), abs=1e-9)

    def test_black_is_measured_from_blacks_side(self, sev):
        """Stored evaluations are White-relative; loss belongs to whoever moved."""
        seed(sev, [(1, 0, None), (2, 310, None)])
        r = rows(sev)[2]
        assert r["color"] == "black"
        assert r["wp_loss"] == pytest.approx(0.2029, abs=1e-4)


class TestLadder:
    """The thresholds live in the view. This is the only place they are pinned."""

    @pytest.mark.parametrize("cp_after,expected", [
        (-310, "blunder"),
        (-300, "mistake"),
        (-150, "mistake"),
        (-140, "inaccuracy"),
        (-75, "inaccuracy"),
        (-70, None),
    ])
    def test_each_boundary(self, sev, cp_after, expected):
        seed(sev, [(0, 0, None), (1, cp_after, None)])
        assert rows(sev)[1]["tier"] == expected


class TestClamp:
    def test_a_decided_position_cannot_book_a_loss(self, sev):
        """Up a queen, a further swing is worth almost nothing. The +/-1000 clamp
        is what stops dead-won games producing a stream of fake blunders."""
        seed(sev, [(0, 5000, None), (1, 1500, None)])
        assert rows(sev)[1]["wp_loss"] == pytest.approx(0.0)
        assert rows(sev)[1]["tier"] is None

    def test_an_apparent_gain_is_not_a_negative_loss(self, sev):
        seed(sev, [(0, 0, None), (1, 200, None)])
        assert rows(sev)[1]["wp_loss"] == pytest.approx(0.0)


class TestMate:
    """move_evals maps a mate to +/-10000; the curve sees it through the clamp."""

    def test_walking_into_a_forced_mate_is_a_blunder(self, sev):
        seed(sev, [(0, 0, None), (1, None, -3)])
        assert rows(sev)[1]["tier"] == "blunder"

    def test_delivering_a_forced_mate_costs_nothing(self, sev):
        seed(sev, [(0, 0, None), (1, None, 3)])
        assert rows(sev)[1]["wp_loss"] == pytest.approx(0.0)

    def test_a_slower_mate_is_not_an_error(self, sev):
        """M1 played as M4 is still winning. Lichess reaches the same conclusion
        independently: its MateDelayed case returns no judgement at all."""
        seed(sev, [(0, None, 1), (1, None, 4)])
        assert rows(sev)[1]["tier"] is None


class TestUnfittedTimeClass:
    def test_a_time_class_with_no_curve_yields_no_rows(self, sev):
        """Blitz has no fitted k until blitz games are analyzed. Borrowing
        another class's curve would silently misprice every blitz error, so the
        join drops them instead."""
        seed(sev, [(0, 0, None), (1, -500, None)], game_id=2, time_class="blitz")
        assert rows(sev, game_id=2) == {}
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_move_severity.py -v`
Expected: FAIL, `ImportError: cannot import name 'MOVE_SEVERITY_VIEW'`

- [ ] **Step 3: Write the view**

In `engine/models.py`, after `MOVE_ERRORS_VIEW`, add:

```python
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
        w.k                                                       AS k
    FROM move_evals    AS e
    JOIN game_coverage AS c
      ON  c.run_id = e.run_id AND c.game_id = e.game_id
    JOIN wp_curve      AS w
      ON  w.time_class = c.time_class
),
curved AS (
    SELECT
        run_id, game_id, ply, color, cp_before, cp_after, k,
        1.0 / (1.0 + exp(-(sgn * cb) / k)) AS wp_before,
        1.0 / (1.0 + exp(-(sgn * ca) / k)) AS wp_after
    FROM scored
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
    -- The floor at zero absorbs search noise: at fixed depth a move can appear
    -- to gain evaluation, and a negative loss would drag every average it lands in.
    MAX(0.0, wp_before - wp_after) AS wp_loss,
    CASE
        WHEN MAX(0.0, wp_before - wp_after) >= {BLUNDER_WP}    THEN 'blunder'
        WHEN MAX(0.0, wp_before - wp_after) >= {MISTAKE_WP}    THEN 'mistake'
        WHEN MAX(0.0, wp_before - wp_after) >= {INACCURACY_WP} THEN 'inaccuracy'
    END AS tier
FROM curved
"""
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_move_severity.py -v`
Expected: PASS, 13 passed

- [ ] **Step 5: Commit**

```bash
git add engine/models.py tests/test_move_severity.py
git commit -m "feat: derive move severity as a change in expected points"
```

---

## Task 5: The `move_quality` view and the Miss flag

**Files:**
- Modify: `engine/models.py`
- Test: `tests/test_move_quality_views.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_move_quality_views.py`:

```python
import math

from sqlalchemy import create_engine

from engine.models import (
    MOVE_EVALS_VIEW,
    MOVE_QUALITY_VIEW,
    MOVE_SEVERITY_VIEW,
    WP_CURVE_DDL,
)


@pytest.fixture
def mq():
    eng = create_engine("sqlite://")
    with eng.begin() as conn:
        conn.execute(text("""
            CREATE TABLE position_evals (
                run_id INTEGER NOT NULL, game_id INTEGER NOT NULL, ply INTEGER NOT NULL,
                cp INTEGER, mate_in INTEGER, best_move_uci VARCHAR(6),
                PRIMARY KEY (run_id, game_id, ply))
        """))
        conn.execute(text("""
            CREATE TABLE game_coverage (
                run_id INTEGER NOT NULL, game_id INTEGER NOT NULL,
                plies_analyzed INTEGER NOT NULL, status VARCHAR(20) NOT NULL,
                error TEXT, completed_at DATETIME, time_class VARCHAR(20),
                PRIMARY KEY (run_id, game_id))
        """))
        conn.execute(text(WP_CURVE_DDL))
        conn.execute(text(MOVE_EVALS_VIEW))
        conn.execute(text(MOVE_SEVERITY_VIEW))
        conn.execute(text(MOVE_QUALITY_VIEW))
        conn.execute(text(
            "INSERT INTO wp_curve (time_class, k, n, source) VALUES ('rapid', 360.0, 1, 'test')"
        ))
    return eng


def seed_mq(eng, positions, game_id=1, run_id=1):
    with eng.begin() as conn:
        conn.execute(
            text("INSERT OR REPLACE INTO game_coverage "
                 "(run_id, game_id, plies_analyzed, status, time_class) "
                 "VALUES (:r, :g, :n, 'complete', 'rapid')"),
            {"r": run_id, "g": game_id, "n": len(positions)},
        )
        for ply, cp in positions:
            conn.execute(
                text("INSERT INTO position_evals (run_id, game_id, ply, cp) "
                     "VALUES (:r, :g, :p, :cp)"),
                {"r": run_id, "g": game_id, "p": ply, "cp": cp},
            )


def mq_rows(eng, game_id=1):
    with eng.connect() as conn:
        return {
            r["ply"]: r
            for r in conn.execute(
                text("SELECT * FROM move_quality WHERE game_id = :g ORDER BY ply"),
                {"g": game_id},
            ).mappings()
        }


class TestMiss:
    """A Miss is failing to take what the opponent just handed you.

    Chess.com makes it a fourth exclusive label. Here it is a flag, so a move can
    be both a blunder by magnitude and a miss by context and both survive.
    """

    def test_giving_back_what_the_opponent_handed_over_is_a_miss(self, mq):
        # White drops 0.1225 at ply 1; Black gives back 0.0809 at ply 2.
        seed_mq(mq, [(0, 0), (1, -180), (2, -60)])
        assert mq_rows(mq)[2]["is_miss"] == 1

    def test_taking_what_was_offered_is_not_a_miss(self, mq):
        # White drops 0.1225; Black concedes only 0.0298, below the 0.05 floor.
        seed_mq(mq, [(0, 0), (1, -180), (2, -135)])
        assert mq_rows(mq)[2]["is_miss"] == 0

    def test_an_error_after_a_quiet_opponent_move_is_not_a_miss(self, mq):
        """Nothing was handed over, so nothing was missed — it is just an error."""
        seed_mq(mq, [(0, 0), (1, 0), (2, 310)])
        r = mq_rows(mq)[2]
        assert r["tier"] == "blunder"
        assert r["is_miss"] == 0

    def test_a_miss_keeps_its_own_tier(self, mq):
        seed_mq(mq, [(0, 0), (1, -180), (2, -60)])
        assert mq_rows(mq)[2]["tier"] == "inaccuracy"

    def test_the_first_ply_has_no_predecessor_and_is_never_a_miss(self, mq):
        seed_mq(mq, [(0, 0), (1, -310)])
        assert mq_rows(mq)[1]["is_miss"] == 0
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_move_quality_views.py -v`
Expected: FAIL, `ImportError: cannot import name 'MOVE_QUALITY_VIEW'`

- [ ] **Step 3: Write the view**

In `engine/models.py`, after `MOVE_SEVERITY_VIEW`, add:

```python
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
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_move_quality_views.py -v`
Expected: PASS, 7 passed

- [ ] **Step 5: Commit**

```bash
git add engine/models.py tests/test_move_quality_views.py
git commit -m "feat: flag missed punishments as a Miss"
```

---

## Task 6: The `game_move_quality` view and view wiring

**Files:**
- Modify: `engine/models.py`
- Test: `tests/test_move_quality_views.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_move_quality_views.py`:

```python
class TestPerGameCounts:
    def test_counts_are_split_by_colour(self, mq):
        """Both sides of every analyzed game are scored, so the row must say whose."""
        # ply 1 white blunder, ply 2 black inaccuracy (and a miss), ply 3 white clean.
        seed_mq(mq, [(0, 0), (1, -310), (2, -250), (3, -250)])
        with mq.connect() as conn:
            got = {
                r["color"]: r
                for r in conn.execute(text(
                    "SELECT * FROM game_move_quality WHERE game_id = 1"
                )).mappings()
            }
        assert got["white"]["blunders"] == 1
        assert got["white"]["moves_scored"] == 2
        assert got["black"]["moves_scored"] == 1
        assert got["black"]["blunders"] == 0

    def test_every_scored_move_is_counted_once(self, mq):
        seed_mq(mq, [(0, 0), (1, -310), (2, -250)])
        with mq.connect() as conn:
            total = conn.execute(text(
                "SELECT SUM(moves_scored) FROM game_move_quality WHERE game_id = 1"
            )).scalar()
            plies = conn.execute(text(
                "SELECT COUNT(*) FROM move_quality WHERE game_id = 1"
            )).scalar()
        assert total == plies

    def test_misses_are_counted_alongside_their_tier_not_instead_of_it(self, mq):
        """The four numbers deliberately do not sum to a total."""
        seed_mq(mq, [(0, 0), (1, -180), (2, -60)])
        with mq.connect() as conn:
            black = conn.execute(text(
                "SELECT * FROM game_move_quality WHERE game_id = 1 AND color = 'black'"
            )).mappings().one()
        assert black["misses"] == 1
        assert black["inaccuracies"] == 1
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_move_quality_views.py::TestPerGameCounts -v`
Expected: FAIL, `sqlite3.OperationalError: no such table: game_move_quality`

- [ ] **Step 3: Write the view and wire all three into init**

In `engine/models.py`, after `MOVE_QUALITY_VIEW`, add:

```python
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
```

Add the fixture's missing view to `tests/test_move_quality_views.py`'s `mq` fixture, immediately after the `MOVE_QUALITY_VIEW` line:

```python
        conn.execute(text(GAME_MOVE_QUALITY_VIEW))
```

and add `GAME_MOVE_QUALITY_VIEW` to that file's import from `engine.models`.

Replace `init_engine_db` in `engine/models.py` with:

```python
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
```

Because `init_engine_db` now references constants defined further down the file, move the `def init_engine_db()` definition to the very bottom of `engine/models.py`.

- [ ] **Step 4: Run the full engine suite**

Run: `uv run pytest tests/test_move_quality_views.py tests/test_move_severity.py tests/test_engine_isolation.py -v`
Expected: PASS, all tests

- [ ] **Step 5: Commit**

```bash
git add engine/models.py tests/test_move_quality_views.py
git commit -m "feat: aggregate move quality per game and colour"
```

---

## Task 7: Fitting the curve

**Files:**
- Create: `engine/fit_curve.py`
- Test: `tests/test_fit_curve.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_fit_curve.py`:

```python
"""The curve is fitted from this corpus, never imported.

Lichess publishes k=271.6, fitted on 2300+ rated rapid. Measured here, rapid
fits 360 and bullet 865 -- a 2.4x spread, because a bullet advantage converts far
less reliably. Importing a curve fitted on stronger players overstates every
error.
"""

import math

from engine.fit_curve import fit_k


def _synthetic(k, n_per_bucket=4000):
    """Outcomes generated from a known k, so the fit has a right answer.

    Deterministic rather than sampled: at each centipawn value the expected
    number of wins is emitted exactly, which is what maximum likelihood is
    recovering anyway and keeps the test from being flaky.
    """
    pairs = []
    for cp in range(-800, 801, 50):
        p = 1 / (1 + math.exp(-cp / k))
        wins = round(p * n_per_bucket)
        pairs.extend([(cp, 1.0)] * wins)
        pairs.extend([(cp, 0.0)] * (n_per_bucket - wins))
    return pairs


def test_it_recovers_a_known_curve():
    assert fit_k(_synthetic(360.0)) == 360


def test_it_recovers_a_flatter_curve():
    """Bullet's curve is nearly two and a half times flatter than rapid's."""
    assert fit_k(_synthetic(865.0)) == 865


def test_draws_count_as_half_a_point():
    """The fitted quantity is expected points, which is what makes our
    thresholds comparable to chess.com's published ladder."""
    even = [(0, 0.5)] * 1000
    assert fit_k(even) is not None


def test_no_data_yields_no_curve():
    assert fit_k([]) is None
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_fit_curve.py -v`
Expected: FAIL, `ModuleNotFoundError: No module named 'engine.fit_curve'`

- [ ] **Step 3: Implement**

Create `engine/fit_curve.py`:

```python
"""Fit the win-probability curve, one k per time class.

    python -m engine.fit_curve --player ballasack6

wp(cp) = 1 / (1 + exp(-cp / k)), fitted by maximum likelihood against observed
results with draws scored 0.5. That makes the fitted quantity expected points,
the same quantity chess.com's published thresholds are denominated in.

Only fills a time class that has no curve yet. Refitting is a deliberate act
(--refit), because a silent refit moves every historical count.
"""

import argparse
import math
from datetime import datetime
from typing import Optional

from sqlalchemy import text

from engine.db import analysis_engine
from engine.models import SessionLocal, WpCurve, init_engine_db

# Search bounds for k. Below 150 the curve is steeper than any observed
# population; above 1200 it is flat enough to be indistinguishable from noise.
K_MIN, K_MAX, K_STEP = 150, 1200, 5

# Early plies are book and late ones are decided; both are uninformative about
# how an evaluation converts. Beyond +/-2500 the result is already settled.
PLY_LO, PLY_HI, CP_ABS_MAX = 20, 99, 2500


def _nll(k: float, pairs) -> float:
    """Negative log-likelihood of the observed results under this k."""
    total = 0.0
    for cp, score in pairs:
        p = 1.0 / (1.0 + math.exp(-cp / k))
        p = min(max(p, 1e-9), 1 - 1e-9)
        total -= score * math.log(p) + (1 - score) * math.log(1 - p)
    return total


def fit_k(pairs) -> Optional[int]:
    """The k minimising negative log-likelihood. None when there is no data.

    pairs: [(cp_from_the_player's_side, score in {0, 0.5, 1})]
    """
    pairs = list(pairs)
    if not pairs:
        return None
    return min(range(K_MIN, K_MAX + 1, K_STEP), key=lambda k: _nll(float(k), pairs))


def collect_pairs(conn, username: str, time_class: str):
    """Every scored position for one player in one time class, with its result."""
    return [
        (row[0], row[1])
        for row in conn.execute(text("""
            WITH me AS (SELECT player_id FROM players WHERE username = :u),
            g AS (
                SELECT g.game_id,
                       CASE WHEN g.white_player_id = (SELECT player_id FROM me)
                            THEN 1 ELSE -1 END AS sgn,
                       g.result
                FROM   games g
                WHERE  (g.white_player_id = (SELECT player_id FROM me)
                     OR g.black_player_id = (SELECT player_id FROM me))
                  AND  g.time_class = :tc
            )
            SELECT mv.cp_after * g.sgn,
                   CASE WHEN g.result = '1/2-1/2' THEN 0.5
                        WHEN (g.result = '1-0' AND g.sgn =  1)
                          OR (g.result = '0-1' AND g.sgn = -1) THEN 1.0
                        ELSE 0.0 END
            FROM   engine.move_evals mv
            JOIN   g ON g.game_id = mv.game_id
            WHERE  mv.ply BETWEEN :lo AND :hi
              AND  ABS(mv.cp_after) < :cap
        """), {"u": username, "tc": time_class,
               "lo": PLY_LO, "hi": PLY_HI, "cap": CP_ABS_MAX})
    ]


def fit_time_class(username: str, time_class: str, refit: bool = False) -> Optional[int]:
    """Fit and store k for one time class. Returns the k, or None if no data."""
    with SessionLocal() as session:
        existing = session.get(WpCurve, time_class)
        if existing is not None and not refit:
            return int(existing.k)

    with analysis_engine().connect() as conn:
        pairs = collect_pairs(conn, username, time_class)

    k = fit_k(pairs)
    if k is None:
        return None

    with SessionLocal() as session:
        session.merge(WpCurve(
            time_class=time_class, k=float(k), n=len(pairs),
            fitted_at=datetime.utcnow(),
            source=f"mle over {username} {time_class}",
        ))
        session.commit()
    return k


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--player", required=True, help="chess.com username")
    p.add_argument("--time-class", action="append", dest="time_classes",
                   choices=["bullet", "blitz", "rapid", "daily"],
                   help="repeatable; default: bullet, blitz and rapid")
    p.add_argument("--refit", action="store_true",
                   help="overwrite an existing curve (moves every historical count)")
    args = p.parse_args(argv)

    init_engine_db()
    for tc in args.time_classes or ["bullet", "blitz", "rapid"]:
        k = fit_time_class(args.player, tc, refit=args.refit)
        print(f"{tc:8s} k = {k}" if k else f"{tc:8s} no analyzed games, skipped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_fit_curve.py -v`
Expected: PASS, 4 passed

- [ ] **Step 5: Commit**

```bash
git add engine/fit_curve.py tests/test_fit_curve.py
git commit -m "feat: fit the win-probability curve per time class"
```

---

## Task 8: Populate the real database

Not a code change. This is the point where the feature becomes real, and it must happen before the API tasks so their manual checks have data.

**Files:** none

- [ ] **Step 1: Back up the sidecar**

```bash
cp chess_engine.db chess_engine.db.pre-move-quality
```

- [ ] **Step 2: Create the views and the new column**

```bash
uv run python -c "from engine.models import init_engine_db; init_engine_db(); print('ok')"
```

Expected: `ok`

- [ ] **Step 3: Backfill time_class on the 1,271 existing coverage rows**

```bash
uv run python -c "from engine.backfill import backfill_coverage_time_class as b; print(b(), 'rows')"
```

Expected: `1271 rows`

- [ ] **Step 4: Fit the curves**

```bash
uv run python -m engine.fit_curve --player ballasack6
```

Expected: `rapid k = 360`, `bullet k = 865`, and `blitz no analyzed games, skipped`.

- [ ] **Step 5: Sanity-check the distribution**

```bash
sqlite3 chess_engine.db "SELECT tier, COUNT(*) FROM move_quality GROUP BY tier ORDER BY 2 DESC;"
```

Expected, within a few rows: `blunder 2475`, `mistake 4195`, `inaccuracy 7170`, and ~87,679 with an empty tier. If blunders come out near zero, the curve join is wrong; if they come out above 20,000, the ladder is being read as centipawns.

---

## Task 9: The two API routes

**Files:**
- Create: `app/move_quality.py`
- Modify: `app/main.py`
- Test: `tests/test_move_quality_api.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_move_quality_api.py`:

```python
"""The two read routes.

Engine coverage is 0.6% of the corpus, so the empty case is the common case and
is tested first.
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.database import get_db
from app.main import app
from engine.db import attach_engine_db
from engine.models import (
    GAME_MOVE_QUALITY_VIEW,
    MOVE_EVALS_VIEW,
    MOVE_QUALITY_VIEW,
    MOVE_SEVERITY_VIEW,
    WP_CURVE_DDL,
)
from tests.conftest import make_game, make_player


@pytest.fixture
def client(db):
    app.dependency_overrides[get_db] = lambda: db
    yield TestClient(app)
    app.dependency_overrides.clear()


def build_sidecar(db):
    """Build a real sidecar file, then attach it to the session.

    The views are created through the sidecar's own engine rather than through
    the attached alias, and that ordering is load-bearing: a view's unqualified
    table references resolve inside the database holding the view, so building
    them in the file is what makes engine.move_severity read engine.move_evals
    instead of whatever `main` happens to contain.

    The autouse _isolate_engine_db fixture in conftest.py has already pointed
    ENGINE_DATABASE_URL at a per-test temporary file, so this never touches the
    real 14MB sidecar.
    """
    from sqlalchemy import create_engine

    from engine import db as engine_db

    side = create_engine(engine_db.ENGINE_DATABASE_URL)
    with side.begin() as conn:
        conn.execute(text(
            "CREATE TABLE position_evals ("
            " run_id INTEGER, game_id INTEGER, ply INTEGER, cp INTEGER,"
            " mate_in INTEGER, best_move_uci VARCHAR(6),"
            " PRIMARY KEY (run_id, game_id, ply))"
        ))
        conn.execute(text(
            "CREATE TABLE game_coverage ("
            " run_id INTEGER, game_id INTEGER, plies_analyzed INTEGER,"
            " status VARCHAR(20), error TEXT, completed_at DATETIME,"
            " time_class VARCHAR(20), PRIMARY KEY (run_id, game_id))"
        ))
        conn.execute(text(WP_CURVE_DDL))
        conn.execute(text(MOVE_EVALS_VIEW))
        conn.execute(text(MOVE_SEVERITY_VIEW))
        conn.execute(text(MOVE_QUALITY_VIEW))
        conn.execute(text(GAME_MOVE_QUALITY_VIEW))
        conn.execute(text(
            "INSERT INTO wp_curve (time_class, k, n, source) "
            "VALUES ('rapid', 360.0, 1, 'test')"
        ))
    side.dispose()

    # Writes from here on go through the attached alias on the session's own
    # connection, so nothing competes for a write lock on the file.
    attach_engine_db(db.connection())


def analyze(db, game, positions, run_id=1):
    """positions: [(ply, cp)] from White's point of view."""
    conn = db.connection()
    conn.exec_driver_sql(
        "INSERT OR REPLACE INTO engine.game_coverage "
        "(run_id, game_id, plies_analyzed, status, time_class) "
        f"VALUES ({run_id}, {game.game_id}, {len(positions)}, 'complete', 'rapid')"
    )
    for ply, cp in positions:
        conn.exec_driver_sql(
            "INSERT INTO engine.position_evals (run_id, game_id, ply, cp) "
            f"VALUES ({run_id}, {game.game_id}, {ply}, {cp})"
        )


def test_a_player_with_no_analyzed_games_gets_an_empty_result(client, db):
    """Coverage is 0.6% of the corpus. This is the common case, not the edge."""
    me = make_player(db, "me")
    them = make_player(db, "them")
    make_game(db, me, them, 1900, 1900)
    db.commit()
    build_sidecar(db)

    r = client.get("/api/players/me/analytics/move-quality")
    assert r.status_code == 200
    body = r.json()
    assert body["games"] == []
    assert body["totals"]["games_analyzed"] == 0


def test_an_unknown_player_is_a_404(client, db):
    assert client.get("/api/players/nobody/analytics/move-quality").status_code == 404


def test_counts_come_back_for_the_searched_players_side_only(client, db):
    me = make_player(db, "me")
    them = make_player(db, "them")
    g = make_game(db, me, them, 1900, 1900)
    db.commit()
    build_sidecar(db)
    # ply 1 is White, which is `me`: a blunder. ply 2 is Black: clean.
    analyze(db, g, [(0, 0), (1, -310), (2, -310)])

    body = client.get("/api/players/me/analytics/move-quality").json()
    assert body["totals"]["games_analyzed"] == 1
    assert body["totals"]["blunders"] == 1
    assert body["games"][0]["blunders"] == 1
    assert body["games"][0]["color"] == "white"


def test_the_drill_list_returns_only_flagged_moves(client, db):
    me = make_player(db, "me")
    them = make_player(db, "them")
    g = make_game(db, me, them, 1900, 1900)
    db.commit()
    build_sidecar(db)
    analyze(db, g, [(0, 0), (1, -310), (2, 0), (3, 0)])

    body = client.get(f"/api/games/{g.game_id}/move-quality").json()
    assert [m["ply"] for m in body["moves"]] == [1]
    assert body["moves"][0]["tier"] == "blunder"
    assert body["moves"][0]["move_san"] == "e4"
    assert body["moves"][0]["wp_before"] == pytest.approx(0.5)


def test_the_drill_list_carries_the_clock(client, db):
    """Time spent is the most interesting column in the table and it is free."""
    me = make_player(db, "me")
    them = make_player(db, "them")
    g = make_game(db, me, them, 1900, 1900, white_clocks=[300.0, 290.0, 280.0])
    db.commit()
    build_sidecar(db)
    analyze(db, g, [(0, 0), (1, -310)])

    m = client.get(f"/api/games/{g.game_id}/move-quality").json()["moves"][0]
    assert m["clock_seconds"] == 300.0
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_move_quality_api.py -v`
Expected: FAIL, 404 on every route

- [ ] **Step 3: Write the query module**

Create `app/move_quality.py`:

```python
"""Every query the move-quality section makes.

Kept out of crud.py, which is already 1,416 lines. The queries here all cross
the ATTACH boundary -- which statements may do even though view definitions may
not -- so they run on a connection that has the sidecar attached.
"""

from datetime import date
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from app import crud
from engine.db import attach_engine_db

# A game analyzed under several runs would otherwise appear once per run.
# Newest run wins: it is the deepest search anybody has pointed at that game.
_LATEST_RUN = """
    q.run_id = (SELECT MAX(c.run_id) FROM engine.game_coverage c
                WHERE c.game_id = q.game_id AND c.status = 'complete')
"""


def _empty() -> dict[str, Any]:
    return {
        "games": [],
        "totals": {
            "games_analyzed": 0, "moves_scored": 0,
            "inaccuracies": 0, "mistakes": 0, "blunders": 0, "misses": 0,
        },
    }


def player_move_quality(
    db: Session,
    player_id: int,
    time_class: Optional[str] = None,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    player_color: Optional[str] = None,
    opening_names: Optional[str] = None,
    tz: Optional[str] = None,
) -> dict[str, Any]:
    """Per-game counts for the searched player's own moves, newest first."""
    attach_engine_db(db.connection())

    where, params = crud._build_game_filters(
        player_id=player_id, time_class=time_class,
        start_date=start_date, end_date=end_date,
        player_color=player_color, opening_names=opening_names, tz=tz,
    )
    params["player_id"] = player_id

    rows = db.execute(text(f"""
        SELECT g.game_id, g.date_played, g.time_class, g.opening_name,
               g.chess_com_url, q.color, q.moves_scored,
               q.inaccuracies, q.mistakes, q.blunders, q.misses
        FROM   games g
        JOIN   engine.game_move_quality q ON q.game_id = g.game_id
        WHERE  {where}
          AND  {_LATEST_RUN}
          AND  q.color = CASE WHEN g.white_player_id = :player_id
                              THEN 'white' ELSE 'black' END
        ORDER  BY g.end_time DESC, g.date_played DESC, g.game_id DESC
    """), params).mappings().all()

    if not rows:
        return _empty()

    games = [dict(r) for r in rows]
    return {
        "games": games,
        "totals": {
            "games_analyzed": len(games),
            "moves_scored": sum(g["moves_scored"] for g in games),
            "inaccuracies": sum(g["inaccuracies"] for g in games),
            "mistakes": sum(g["mistakes"] for g in games),
            "blunders": sum(g["blunders"] for g in games),
            "misses": sum(g["misses"] for g in games),
        },
    }


def game_drill_list(db: Session, game_id: int) -> dict[str, Any]:
    """Every flagged move in one game, both sides, in order.

    Unflagged moves are omitted: a 90-ply game yields perhaps eight rows, and
    the rest carry no information the section is trying to show.
    """
    attach_engine_db(db.connection())

    rows = db.execute(text(f"""
        SELECT q.ply, q.color, q.tier, q.is_miss,
               q.wp_before, q.wp_after, q.wp_loss,
               m.move_number, m.move_san, m.clock_seconds, m.time_spent_seconds
        FROM   engine.move_quality q
        JOIN   moves m ON m.game_id = q.game_id AND m.ply = q.ply
        WHERE  q.game_id = :game_id
          AND  {_LATEST_RUN}
          AND  (q.tier IS NOT NULL OR q.is_miss = 1)
        ORDER  BY q.ply
    """), {"game_id": game_id}).mappings().all()

    return {"game_id": game_id, "moves": [dict(r) for r in rows]}
```

- [ ] **Step 4: Add the routes**

In `app/main.py`, change the import line `from app import baselines, crud, schemas` to:

```python
from app import baselines, crud, move_quality as mq, schemas
```

Then add, immediately after the `style_profile` route:

```python
@app.get("/api/players/{username}/analytics/move-quality")
def move_quality_by_game(
    username: str,
    time_class: Optional[str] = None,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    tz: Optional[str] = None,
    player_color: Optional[str] = None,
    opening_names: Optional[str] = None,
    db: Session = Depends(get_db),
):
    player = crud.get_player(db, username)
    if not player:
        raise HTTPException(404, f"Player '{username}' not found")
    return mq.player_move_quality(
        db, player.player_id, time_class, start_date, end_date,
        player_color, opening_names, tz=tz,
    )


@app.get("/api/games/{game_id}/move-quality")
def game_move_quality(game_id: int, db: Session = Depends(get_db)):
    return mq.game_drill_list(db, game_id)
```

- [ ] **Step 5: Run the tests and commit**

Run: `uv run pytest tests/test_move_quality_api.py -v`
Expected: PASS, 5 passed

```bash
git add app/move_quality.py app/main.py tests/test_move_quality_api.py
git commit -m "feat: serve per-game move quality and a drill list"
```

---

## Task 10: The UI section

**Files:**
- Create: `app/static/move-quality.js`
- Modify: `app/static/index.html`
- Test: manual

- [ ] **Step 1: Add the markup**

In `app/static/index.html`, immediately before the closing `</main>` tag, add:

```html
<section id="move-quality-section" class="section hidden">
    <div class="section-header">
        <h2>Move Quality</h2>
        <span class="date-range-label" id="mq-coverage-label"></span>
    </div>
    <div class="stats-grid" id="mq-totals"></div>
    <p class="mq-note">
        Severity is measured as lost win probability, not centipawns.
        A Miss overlaps the other three rather than replacing them, so these
        four numbers do not sum to a total.
    </p>
    <table class="mq-table" id="mq-table"></table>
</section>
```

- [ ] **Step 2: Write the renderer**

Create `app/static/move-quality.js`:

```javascript
/* Move quality: per-game inaccuracies, mistakes, blunders and misses.
 *
 * Engine coverage is a fraction of the corpus, so the empty state is the
 * normal state and says so rather than rendering an empty table. */

const MQ_TIERS = ['inaccuracies', 'mistakes', 'blunders', 'misses'];

function mqPct(n, d) {
    return d ? ((100 * n) / d).toFixed(1) + '%' : '—';
}

async function loadMoveQuality(username) {
    const section = document.getElementById('move-quality-section');
    let data;
    try {
        data = await fetchJSON(
            `/api/players/${username}/analytics/move-quality`
            + colorParams(queryColor(), currentOpeningFilter)
        );
    } catch (e) {
        section.classList.add('hidden');
        return;
    }
    const t = data.totals;
    section.classList.remove('hidden');

    const label = document.getElementById('mq-coverage-label');
    if (!t.games_analyzed) {
        label.textContent = 'No engine-analyzed games in this selection';
        document.getElementById('mq-totals').innerHTML = '';
        document.getElementById('mq-table').innerHTML = '';
        return;
    }
    label.textContent =
        `${t.games_analyzed} analyzed games, ${t.moves_scored.toLocaleString()} scored moves`;

    document.getElementById('mq-totals').innerHTML = MQ_TIERS.map((k) => `
        <div class="stat-card">
            <div class="stat-label">${k[0].toUpperCase() + k.slice(1)}</div>
            <div class="stat-value">${(t[k] / t.games_analyzed).toFixed(2)}</div>
            <div class="stat-sub">per game · ${mqPct(t[k], t.moves_scored)} of moves</div>
        </div>`).join('');

    document.getElementById('mq-table').innerHTML = `
        <thead><tr>
            <th>Date</th><th>Opening</th><th>Moves</th>
            <th>Inacc</th><th>Mist</th><th>Blun</th><th>Miss</th><th></th>
        </tr></thead>
        <tbody>${data.games.map((g) => `
            <tr class="mq-row" data-game="${g.game_id}" data-url="${g.chess_com_url || ''}">
                <td>${g.date_played || '—'}</td>
                <td class="mq-opening">${g.opening_name || '—'}</td>
                <td>${g.moves_scored}</td>
                <td>${g.inaccuracies}</td>
                <td>${g.mistakes}</td>
                <td class="mq-blunder">${g.blunders}</td>
                <td>${g.misses}</td>
                <td><button class="btn-sm" onclick="toggleMqDrill(${g.game_id})">Moves</button></td>
            </tr>
            <tr class="mq-drill hidden" id="mq-drill-${g.game_id}">
                <td colspan="8"></td>
            </tr>`).join('')}
        </tbody>`;
}

async function toggleMqDrill(gameId) {
    const row = document.getElementById(`mq-drill-${gameId}`);
    if (!row.classList.contains('hidden')) { row.classList.add('hidden'); return; }
    row.classList.remove('hidden');

    const cell = row.firstElementChild;
    cell.textContent = 'Loading…';
    const data = await (await fetch(`/api/games/${gameId}/move-quality`)).json();
    if (!data.moves.length) { cell.textContent = 'No flagged moves.'; return; }

    const url = document.querySelector(`.mq-row[data-game="${gameId}"]`)?.dataset.url || '';
    cell.innerHTML = `<table class="mq-moves"><tbody>${data.moves.map((m) => `
        <tr>
            <td>${m.move_number}${m.color === 'white' ? '.' : '...'} ${m.move_san}</td>
            <td class="mq-tier-${m.tier || 'none'}">${m.tier || ''}${m.is_miss ? ' · miss' : ''}</td>
            <td>${(100 * m.wp_before).toFixed(0)}% → ${(100 * m.wp_after).toFixed(0)}%</td>
            <td>${m.time_spent_seconds != null ? m.time_spent_seconds.toFixed(1) + 's' : '—'}</td>
        </tr>`).join('')}</tbody></table>
        ${url ? `<a class="mq-link" href="${url}" target="_blank" rel="noopener">Open on chess.com</a>` : ''}`;
}
```

- [ ] **Step 3: Wire it in**

In `app/static/index.html`, next to the existing `style-panel.js` script tag, add:

```html
<script src="/static/move-quality.js"></script>
```

In `app/static/app.js`, inside `refreshAll()` (line 580), add one entry to the `promises` array at line 588 so it reads:

```javascript
    const promises = [
        loadStats(currentUsername),
        loadEloChart(currentUsername),
        loadGames(currentUsername),
        initRepertoireTabs(currentUsername),
        loadMoveQuality(currentUsername),
    ];
```

`loadMoveQuality` reads the filter bar itself through `colorParams`/`queryColor`, the same way the other loaders do, so it takes no params argument.

Add `loadMoveQuality` to the `/* global */` comment at the top of `app.js` alongside `loadStylePanel`, or eslint will flag it as undefined.

- [ ] **Step 4: Check it by hand**

```bash
uv run uvicorn app.main:app --reload
```

Open http://127.0.0.1:8000, search `ballasack6`, set the time class filter to Rapid.

Expected: the Move Quality section shows roughly 1.0 blunders per game over 865 analyzed rapid games. Clicking **Moves** on a row expands a short list of flagged moves with percentages and times. Set the filter to Blitz: the section shows "No engine-analyzed games in this selection" rather than an error or an empty table.

- [ ] **Step 5: Commit**

```bash
git add app/static/move-quality.js app/static/index.html app/static/app.js
git commit -m "feat: add the move quality section"
```

---

## Verification

- [ ] **Full suite**

Run: `uv run pytest -v`
Expected: PASS, no regressions in `test_engine_derivation.py`, `test_engine_analyze.py` or `test_baselines.py`

- [ ] **Linters**

Run: `uv run ruff check . && uv run mypy engine app && npx eslint app/static`
Expected: all clean (the pre-commit hook runs these anyway)

---

## Next plan

The population half: a band-keyed scope resolver, `population_jobs`, a threaded
job runner, the "Analyze population" button with progress, the
`/move-quality/baseline` route, and the population overlay on the counts this
plan renders.
