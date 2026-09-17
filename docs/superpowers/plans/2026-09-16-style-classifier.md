# Style Classifier v0 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a panel showing where a player sits on four measured style axes, plus which 2800+ blitz player they most resemble.

**Architecture:** Four board-derived metrics are computed once per game per colour over the whole corpus and stored as facts in the sidecar `chess_engine.db`. Interpretation — centring within (time class, opening, colour), percentile ranks, error bars, similarity — is derived at query time from those facts, so revising it never requires recomputing the corpus. The subject's vector honours the live UI filters; reference vectors are precomputed full-history.

**Tech Stack:** Python 3, `python-chess`, SQLAlchemy 2.0, SQLite (sidecar attached via `ATTACH`), FastAPI, pytest, vanilla JS + Chart.js 4.4.7.

**Spec:** `docs/superpowers/specs/2026-09-16-style-classifier-design.md`

---

## Background the engineer needs

**These metrics measure style, not ability.** Reliability 0.54–0.74, correlation with Elo 0.02–0.08, joint R² against rating 0.0096. No code comment, label, variable name, or UI string may imply one end of an axis is better than the other. Name things `more_space` / `less_space`, never `good` / `bad`. See `docs/superpowers/specs/2026-09-16-style-vs-ability-findings.md`.

**Why centring exists.** White has more space than Black — a fact about chess, not about a player. Comparing raw values produced an effect above t=7 that was entirely colour. Every value is centred within `(time_class, eco3, colour)` before use. This is not optional polish; it is the difference between the feature working and it reporting which colour you played.

**Database layout.** `chess_analytics.db` is canonical (games, moves, players). `chess_engine.db` is the sidecar. `engine.db.analysis_engine()` returns a canonical engine with the sidecar `ATTACH`ed under the alias `engine`, so a single query can join across both. Tables live in the sidecar because they are derived and rebuildable.

**Testing pattern.** Query functions take a `conn`, never a global — see `engine/scope.py::resolve_scope`. Tests build one in-memory SQLite database containing both schemas and pass its connection. This is how `tests/test_engine_features.py` already works.

**Commands.** `uv run pytest`, `uv run ruff check .`, `uv run mypy .`. Pre-commit hooks run ruff, mypy and eslint on every commit; a commit that fails them is rejected, so run them before committing.

---

## File Structure

| file | responsibility |
|---|---|
| `analysis/__init__.py` | package marker (exists) |
| `analysis/metrics.py` | the metrics. Pure functions, no I/O (exists; Task 1 renames `STYLE_METRICS` to `STYLE_AXES` and adds `SNAPSHOT_PLY`) |
| `analysis/screen.py` | the rating-gradient and reliability screen (exists; not otherwise touched) |
| `analysis/build_features.py` | CLI: populate `position_features`, then `style_cell_means`, then `player_style_vectors` |
| `engine/models.py` (modify) | three new table definitions + `init_engine_db` |
| `app/style.py` | query layer: subject vector, percentiles, similarity. Takes a `conn` |
| `app/main.py` (modify) | one route |
| `app/static/style-panel.js` | panel rendering. A separate file because `app.js` is already 2,387 lines |
| `app/static/index.html` (modify) | panel markup, script tag |
| `app/static/app.js` (modify) | call the loader from `loadAll` |
| `app/static/style.css` (modify) | panel styles |
| `tests/test_style_metrics.py` | metric correctness against hand-built positions |
| `tests/test_style_vectors.py` | centring, fallback, aggregation, percentile, similarity |
| `tests/test_style_api.py` | route shape and error cases |

---

## Task 1: The four metrics

**Files:**
- Modify: `analysis/metrics.py`
- Modify: `analysis/screen.py:32-36`
- Test: `tests/test_style_metrics.py` (create)

**Read this first.** `analysis/metrics.py` and `analysis/__init__.py` already
exist on this branch, committed in `fcf889c`, with all five metric functions
already written and validated. This task does **not** create them. It adds the
tests they never had, and makes three small changes so the rest of the plan's
tasks can import what they expect:

1. Rename `STYLE_METRICS` to `STYLE_AXES` (line 140). "Axes" is what every later
   task and the UI call them; two names for one thing is how a later task ends
   up importing the wrong one.
2. Add `SNAPSHOT_PLY = 20` to `metrics.py`. It currently lives in `screen.py`,
   but `build_features.py` needs it too in Task 3, and duplicating it is how the
   two drift apart and silently measure different positions.
3. Update `analysis/screen.py:32-36` to import both from `metrics`, dropping its
   local `SNAPSHOT_PLY`.

Do not otherwise rewrite the metric functions. They are the validated versions
behind the findings doc; changing them invalidates the measurements that
justified this feature.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_style_metrics.py`:

```python
"""The four style metrics, against positions where the answer is known by hand.

Every metric is signed so that HIGHER = MORE OF THE THING, never so that higher
is better. These axes measure style, not ability.
"""

import chess

from analysis.metrics import (
    king_safety,
    mobility,
    passed_pawns,
    pawn_structure,
    space,
)


class TestSpace:
    def test_the_start_position_is_symmetric(self):
        b = chess.Board()
        assert space(b, chess.WHITE) == space(b, chess.BLACK)

    def test_advancing_in_the_centre_gains_space(self):
        b = chess.Board()
        before = space(b, chess.WHITE)
        b.push_san("e4")
        assert space(b, chess.WHITE) > before

    def test_enemy_pawn_control_removes_a_square(self):
        """A square attacked by an enemy pawn is not safe, so it does not count."""
        open_board = chess.Board("4k3/8/8/8/8/8/8/4K3 w - - 0 1")
        guarded = chess.Board("4k3/8/8/2p5/8/8/8/4K3 w - - 0 1")
        assert space(guarded, chess.WHITE) < space(open_board, chess.WHITE)


class TestMobility:
    def test_the_start_position_is_symmetric(self):
        b = chess.Board()
        assert mobility(b, chess.WHITE) == mobility(b, chess.BLACK)

    def test_a_developed_knight_has_more_scope(self):
        b = chess.Board()
        before = mobility(b, chess.WHITE)
        b.push_san("Nf3")
        assert mobility(b, chess.WHITE) > before

    def test_it_does_not_depend_on_whose_turn_it_is(self):
        """Computed from attack maps, not legal moves, so both colours are
        measurable in the same position. A legal-move implementation would
        silently return 0 for the side not to move."""
        w = chess.Board("rnbqkbnr/pppppppp/8/8/8/5N2/PPPPPPPP/RNBQKB1R b KQkq - 1 1")
        assert mobility(w, chess.WHITE) > 0
        assert mobility(w, chess.BLACK) > 0


class TestKingSafety:
    def test_an_intact_shield_beats_a_stripped_one(self):
        sheltered = chess.Board("4k3/8/8/8/8/8/5PPP/6K1 w - - 0 1")
        exposed = chess.Board("4k3/8/8/8/8/8/8/6K1 w - - 0 1")
        assert king_safety(sheltered, chess.WHITE) > king_safety(exposed, chess.WHITE)

    def test_an_attacker_on_the_ring_lowers_it(self):
        quiet = chess.Board("4k3/8/8/8/8/8/5PPP/6K1 w - - 0 1")
        attacked = chess.Board("4k3/8/8/8/8/6q1/5PPP/6K1 w - - 0 1")
        assert king_safety(attacked, chess.WHITE) < king_safety(quiet, chess.WHITE)

    def test_a_king_on_the_edge_does_not_crash(self):
        """The king ring runs off the board on the a-file; an implementation
        that assumes eight neighbours raises here."""
        b = chess.Board("4k3/8/8/8/8/8/8/K7 w - - 0 1")
        assert isinstance(king_safety(b, chess.WHITE), float)

    def test_a_missing_king_scores_zero(self):
        b = chess.Board("4k3/8/8/8/8/8/8/8 w - - 0 1")
        b.clear()
        assert king_safety(b, chess.WHITE) == 0.0


class TestPawnStructure:
    def test_a_sound_structure_scores_zero(self):
        b = chess.Board("4k3/8/8/8/8/8/PPP5/4K3 w - - 0 1")
        assert pawn_structure(b, chess.WHITE) == 0.0

    def test_doubled_pawns_cost(self):
        b = chess.Board("4k3/8/8/8/8/P7/P7/4K3 w - - 0 1")
        assert pawn_structure(b, chess.WHITE) < 0.0

    def test_an_isolated_pawn_costs(self):
        supported = chess.Board("4k3/8/8/8/8/8/PP6/4K3 w - - 0 1")
        lone = chess.Board("4k3/8/8/8/8/8/P7/4K3 w - - 0 1")
        assert pawn_structure(lone, chess.WHITE) < pawn_structure(supported, chess.WHITE)

    def test_pawns_two_files_apart_are_both_isolated(self):
        """a2 and c2 support neither each other nor anything else, so this
        costs twice what a single lone pawn does -- 'isolated' means no pawn on
        an IMMEDIATELY adjacent file, not merely no pawn nearby."""
        split = chess.Board("4k3/8/8/8/8/8/P1P5/4K3 w - - 0 1")
        lone = chess.Board("4k3/8/8/8/8/8/P7/4K3 w - - 0 1")
        assert pawn_structure(split, chess.WHITE) == 2 * pawn_structure(lone, chess.WHITE)

    def test_a_pawnless_side_scores_zero(self):
        b = chess.Board("4k3/8/8/8/8/8/8/4K3 w - - 0 1")
        assert pawn_structure(b, chess.WHITE) == 0.0


class TestPassedPawns:
    def test_an_unopposed_pawn_is_passed(self):
        b = chess.Board("4k3/8/8/8/8/8/P7/4K3 w - - 0 1")
        assert passed_pawns(b, chess.WHITE) == 1.0

    def test_an_enemy_pawn_on_an_adjacent_file_stops_it(self):
        b = chess.Board("4k3/1p6/8/8/8/8/P7/4K3 w - - 0 1")
        assert passed_pawns(b, chess.WHITE) == 0.0
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_style_metrics.py -v`
Expected: `ImportError: cannot import name 'STYLE_AXES'` from the Task 3 import, or failures in the metric tests if any assertion is wrong. The metric functions themselves already exist, so most tests should pass immediately; that is expected and is the point of writing them against code that was never covered.

- [ ] **Step 3: Make the three changes**

Apply the three changes listed above. For reference, this is what
`analysis/metrics.py` must look like afterwards — the function bodies are
already correct and unchanged; only the module docstring, `STYLE_AXES` and
`SNAPSHOT_PLY` differ from what is on disk:

```python
"""Board-derived positional metrics, reimplemented from classical Stockfish.

Modern Stockfish (19, NNUE) exposes only Material/PSQT and Positional/Layers
through `eval`; the named strategic terms were removed around Stockfish 16.
These are our own implementations, following the shape of the classical
definitions so the choices trace to a public source rather than to taste.

Every function is engine-free and returns a value where HIGHER = MORE OF THE
THING -- never where higher is better. These metrics measure STYLE, NOT ABILITY:
reliability 0.54-0.74 but correlation with Elo 0.02-0.08. See
docs/superpowers/specs/2026-09-16-style-vs-ability-findings.md.
"""

import chess

CENTER_FILES = chess.BB_FILE_C | chess.BB_FILE_D | chess.BB_FILE_E | chess.BB_FILE_F
SPACE_W = CENTER_FILES & (chess.BB_RANK_2 | chess.BB_RANK_3 | chess.BB_RANK_4)
SPACE_B = CENTER_FILES & (chess.BB_RANK_7 | chess.BB_RANK_6 | chess.BB_RANK_5)

# Classical Stockfish weighted mobility by piece type and game phase. The phase
# term is dropped: it is a monotone scalar on the whole position, so it cannot
# create a difference between two players who reached the same position.
MOBILITY_WEIGHTS = {chess.KNIGHT: 1.0, chess.BISHOP: 1.0,
                    chess.ROOK: 0.7, chess.QUEEN: 0.4}
KING_RING_WEIGHTS = {chess.KNIGHT: 2, chess.BISHOP: 2, chess.ROOK: 3, chess.QUEEN: 5}


def _pawn_attacks(board: chess.Board, color: chess.Color) -> int:
    bb = 0
    for sq in board.pieces(chess.PAWN, color):
        bb |= int(chess.BB_PAWN_ATTACKS[color][sq])
    return bb


def _adjacent_files(f: int) -> list[int]:
    return [x for x in (f - 1, f + 1) if 0 <= x <= 7]


def space(board: chess.Board, color: chess.Color) -> float:
    """Safe central squares on our own half, plus squares sheltered behind our
    own pawns. Classical Stockfish's space term without the blocked-pawn weight.
    """
    mask = SPACE_W if color == chess.WHITE else SPACE_B
    own_pawns = int(board.pieces(chess.PAWN, color))
    safe = mask & ~own_pawns & ~_pawn_attacks(board, not color)
    behind = own_pawns
    if color == chess.WHITE:
        behind |= behind >> 8
        behind |= behind >> 16
    else:
        behind |= behind << 8
        behind |= behind << 16
    return float(bin(safe).count("1") + bin(safe & behind).count("1"))


def mobility(board: chess.Board, color: chess.Color) -> float:
    """Weighted count of squares our pieces reach, excluding our own pieces and
    squares controlled by enemy pawns (classical Stockfish's "mobility area").

    Computed from attack maps rather than legal moves so it does not depend on
    whose turn it is -- both colours are measurable in the same position.
    """
    area = ~int(board.occupied_co[color]) & ~_pawn_attacks(board, not color)
    total = 0.0
    for piece_type, weight in MOBILITY_WEIGHTS.items():
        for sq in board.pieces(piece_type, color):
            total += weight * bin(int(board.attacks(sq)) & area).count("1")
    return total


def king_safety(board: chess.Board, color: chess.Color) -> float:
    """Pawn shield minus attacker weight on the king ring. Higher = safer.

    Signed so higher is "more sheltered", unlike classical Stockfish's "king
    danger" which runs the other way. Keeping every axis pointing the same
    direction is what lets them be charted without a per-axis sign table.
    """
    ksq = board.king(color)
    if ksq is None:
        return 0.0
    ring = int(chess.BB_KING_ATTACKS[ksq]) | int(chess.BB_SQUARES[ksq])
    danger = 0
    for piece_type, weight in KING_RING_WEIGHTS.items():
        for sq in board.pieces(piece_type, not color):
            if int(board.attacks(sq)) & ring:
                danger += weight
    kf, kr = chess.square_file(ksq), chess.square_rank(ksq)
    shield = 0
    for f in [kf] + _adjacent_files(kf):
        for d in (1, 2):
            r = kr + d if color == chess.WHITE else kr - d
            if 0 <= r <= 7 and board.piece_at(chess.square(f, r)) == chess.Piece(
                    chess.PAWN, color):
                shield += 1
                break
    return shield * 1.5 - danger


def pawn_structure(board: chess.Board, color: chess.Color) -> float:
    """Negated count of structural weaknesses: doubled and isolated pawns.

    Backward pawns are deliberately omitted. Every definition we could write
    would be a judgement call with nothing to validate it against; doubled and
    isolated are unambiguous.
    """
    by_file: dict[int, int] = {}
    for sq in board.pieces(chess.PAWN, color):
        f = chess.square_file(sq)
        by_file[f] = by_file.get(f, 0) + 1
    weak = 0
    for f, count in by_file.items():
        weak += count - 1                                    # doubled
        if not any(a in by_file for a in _adjacent_files(f)):
            weak += count                                    # isolated
    return -float(weak)


def passed_pawns(board: chess.Board, color: chess.Color) -> float:
    """Pawns with no enemy pawn ahead on their own or adjacent files.

    MEASURED AS NOISE at ply 20: split-half reliability +0.03, so it is excluded
    from STYLE_AXES. Kept only so the negative result stays reproducible.
    """
    theirs = board.pieces(chess.PAWN, not color)
    ahead = ((lambda r, er: er > r) if color == chess.WHITE
             else (lambda r, er: er < r))
    n = 0
    for sq in board.pieces(chess.PAWN, color):
        f, r = chess.square_file(sq), chess.square_rank(sq)
        files = {f} | set(_adjacent_files(f))
        if not any(chess.square_file(e) in files and ahead(r, chess.square_rank(e))
                   for e in theirs):
            n += 1
    return float(n)


#: The axes that survived the reliability screen, in display order.
STYLE_AXES = {
    "space": space,
    "mobility": mobility,
    "king_safety": king_safety,
    "pawn_structure": pawn_structure,
}

#: The ply every metric is sampled at.
SNAPSHOT_PLY = 20
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_style_metrics.py -v`
Expected: 17 passed

> **As built (2026-09-17):** the final file has 21 tests. Code review found that
> three of the seventeen could not detect the bug they were named for — the
> doubled-pawn fixture was also isolated, so the assertion held with doubling
> detection removed; mobility's enemy-pawn exclusion had no coverage; and
> `passed_pawns` survived a flipped direction comparison. Four tests were added
> and one fixture corrected. Each is verified by mutation rather than by
> assertion alone. See `tests/test_style_metrics.py`, commits 9532082 and
> 0c181ad.

- [ ] **Step 5: Lint, typecheck and commit**

```bash
uv run ruff check analysis/ tests/test_style_metrics.py
uv run mypy analysis/
uv run python -c "import analysis.screen"   # the rename must not break it
git add analysis/metrics.py analysis/screen.py tests/test_style_metrics.py
git commit -m "test: cover the style metrics, and name the axes consistently"
```

---

## Task 2: Schema for the three style tables

**Files:**
- Modify: `engine/models.py` (append after `BestMoveFeatures`, and extend `init_engine_db`)
- Test: `tests/test_style_vectors.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_style_vectors.py`:

```python
"""Centring, aggregation, percentile and similarity over the style tables."""

import pytest
from sqlalchemy import create_engine, text


@pytest.fixture
def conn():
    """One in-memory database holding both schemas.

    The real deployment keeps them in separate files joined by ATTACH, but the
    queries only ever name tables, so a single database exercises the same SQL.
    """
    eng = create_engine("sqlite://")
    with eng.begin() as c:
        c.execute(text("""
            CREATE TABLE players (
                player_id INTEGER PRIMARY KEY, username TEXT)"""))
        c.execute(text("""
            CREATE TABLE games (
                game_id INTEGER PRIMARY KEY,
                white_player_id INTEGER, black_player_id INTEGER,
                time_class TEXT, eco TEXT, variant TEXT,
                white_elo INTEGER, black_elo INTEGER,
                date_played DATE, end_time INTEGER)"""))
        c.execute(text("""
            CREATE TABLE position_features (
                game_id INTEGER, color TEXT,
                space REAL, mobility REAL, king_safety REAL, pawn_structure REAL,
                PRIMARY KEY (game_id, color))"""))
        c.execute(text("""
            CREATE TABLE style_cell_means (
                time_class TEXT, eco3 TEXT, color TEXT, n INTEGER,
                space REAL, mobility REAL, king_safety REAL, pawn_structure REAL,
                PRIMARY KEY (time_class, eco3, color))"""))
        c.execute(text("""
            CREATE TABLE player_style_vectors (
                player_id INTEGER, time_class TEXT, n INTEGER, mean_elo REAL,
                space REAL, mobility REAL, king_safety REAL, pawn_structure REAL,
                PRIMARY KEY (player_id, time_class))"""))
        yield c


def test_the_schema_exists(conn):
    for table in ("position_features", "style_cell_means", "player_style_vectors"):
        conn.execute(text(f"SELECT COUNT(*) FROM {table}"))
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_style_vectors.py -v`
Expected: PASS (this fixture is self-contained). This step exists to confirm the fixture itself is sound before later tasks build on it; if it errors, the CREATE statements are wrong.

- [ ] **Step 3: Add the real tables to `engine/models.py`**

Append to `engine/models.py`, after the `BestMoveFeatures` class:

```python
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
```

Add `Float` to the SQLAlchemy import at the top of the file — it currently imports `Column, DateTime, ForeignKey, Integer, String, Text, text`:

```python
from sqlalchemy import Column, DateTime, Float, ForeignKey, Integer, String, Text, text
```

- [ ] **Step 4: Verify the tables are created**

`init_engine_db()` already calls `Base.metadata.create_all`, so new models need no change there.

Run:
```bash
uv run python -c "
from engine.models import init_engine_db
from engine.db import engine
from sqlalchemy import inspect
init_engine_db()
names = inspect(engine).get_table_names()
for t in ('position_features','style_cell_means','player_style_vectors'):
    assert t in names, t
print('ok')"
```
Expected: `ok`

- [ ] **Step 5: Commit**

```bash
uv run ruff check engine/ tests/test_style_vectors.py
uv run mypy engine/
git add engine/models.py tests/test_style_vectors.py
git commit -m "feat: add the style tables to the sidecar schema"
```

---

## Task 3: Populate `position_features` over the corpus

**Files:**
- Create: `analysis/build_features.py`
- Test: `tests/test_style_vectors.py` (append)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_style_vectors.py`:

```python
import chess

from analysis.build_features import extract_game
from analysis.metrics import space


class TestExtraction:
    def test_a_game_yields_one_row_per_colour(self):
        sans = ["e4", "e5", "Nf3", "Nc6", "Bb5", "a6", "Ba4", "Nf6", "O-O", "Be7"]
        rows = extract_game(7, sans * 2)   # 20 plies
        assert {r["color"] for r in rows} == {"white", "black"}
        assert all(r["game_id"] == 7 for r in rows)
        assert len(rows) == 2

    def test_a_short_game_yields_nothing(self):
        """Fewer than 20 plies means the snapshot ply was never reached. An
        implementation that measured the final position instead would compare
        move 6 against move 20 and call it the same thing."""
        assert extract_game(7, ["e4", "e5"]) == []

    def test_an_unreplayable_game_yields_nothing(self):
        sans = ["e4", "e5", "Qxh8"] + ["e4"] * 20
        assert extract_game(7, sans) == []

    def test_the_values_match_the_metrics_at_ply_20(self):
        sans = ["e4", "e5", "Nf3", "Nc6", "Bb5", "a6", "Ba4", "Nf6", "O-O", "Be7"] * 2
        rows = {r["color"]: r for r in extract_game(7, sans)}
        board = chess.Board()
        for san in sans[:20]:
            board.push_san(san)
        assert rows["white"]["space"] == space(board, chess.WHITE)
        assert rows["black"]["space"] == space(board, chess.BLACK)
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_style_vectors.py -v`
Expected: `ModuleNotFoundError: No module named 'analysis.build_features'`

- [ ] **Step 3: Implement**

Create `analysis/build_features.py`:

```python
"""Populate the style tables from the game corpus.

    uv run python -m analysis.build_features            # everything
    uv run python -m analysis.build_features --features # position_features only

Engine-free, ~500 games/sec, so the whole 203k-game corpus takes ~7 minutes.
Every stage is idempotent: re-running replaces rather than appends, which is
what makes a metric change safe -- otherwise the table would hold a mix of two
definitions nobody could tell apart.
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict

import chess
from sqlalchemy import text

from analysis.metrics import SNAPSHOT_PLY, STYLE_AXES
from engine.db import analysis_engine
from engine.models import init_engine_db

#: A (time class, opening, colour) cell below this many observations cannot
#: define a reliable centre; its games fall back to the coarse '*' cell.
MIN_CELL = 40

#: Games below this for a player in a time class make an unstable vector. The
#: reliability figures in the findings doc are measured at this threshold.
MIN_PLAYER_GAMES = 30

#: Schema prefix for the sidecar tables. "engine." in production, where the
#: sidecar is ATTACHed under that alias; empty in tests, which keep both schemas
#: in one in-memory database so the same SQL is exercised either way.
SCHEMA = "engine." 


def extract_game(game_id: int, sans: list[str]) -> list[dict]:
    """Replay to SNAPSHOT_PLY and measure both colours.

    Returns [] for a game that ends early or stops reconstructing, rather than
    measuring whatever position it reached -- comparing move 6 against move 20
    would silently mix two different things.
    """
    if len(sans) < SNAPSHOT_PLY:
        return []
    board = chess.Board()
    try:
        for san in sans[:SNAPSHOT_PLY]:
            board.push_san(san)
    except ValueError:
        return []
    return [
        {"game_id": game_id, "color": name,
         **{axis: fn(board, color) for axis, fn in STYLE_AXES.items()}}
        for color, name in ((chess.WHITE, "white"), (chess.BLACK, "black"))
    ]


def build_position_features(conn) -> int:
    """Measure every standard game in the corpus. Returns rows written."""
    sans: dict[int, list[str]] = defaultdict(list)
    for game_id, san in conn.execute(text(
            "SELECT m.game_id, m.move_san FROM moves m "
            "JOIN games g ON g.game_id = m.game_id "
            "WHERE g.variant IS NULL AND m.ply <= :ply "
            "ORDER BY m.game_id, m.ply"), {"ply": SNAPSHOT_PLY}):
        sans[game_id].append(san)

    conn.execute(text(f"DELETE FROM {SCHEMA}position_features"))
    written = 0
    batch: list[dict] = []
    for game_id, moves in sans.items():
        batch.extend(extract_game(game_id, moves))
        if len(batch) >= 5000:
            _insert_features(conn, batch)
            written += len(batch)
            batch = []
    if batch:
        _insert_features(conn, batch)
        written += len(batch)
    return written


def _insert_features(conn, rows: list[dict]) -> None:
    conn.execute(text(
        f"INSERT INTO {SCHEMA}position_features "
        "(game_id, color, space, mobility, king_safety, pawn_structure) "
        "VALUES (:game_id, :color, :space, :mobility, :king_safety, "
        ":pawn_structure)"), rows)


def build_cell_means(conn) -> int:
    """Per (time class, opening, colour) means, plus a coarse '*' fallback row.

    Writing the fallback as a real row rather than handling it in the query is
    deliberate: the read path then joins twice and COALESCEs, with no branching
    over whether a cell happened to be populated.
    """
    axes = ", ".join(f"AVG(f.{a}) AS {a}" for a in STYLE_AXES)
    conn.execute(text(f"DELETE FROM {SCHEMA}style_cell_means"))
    conn.execute(text(f"""
        INSERT INTO {SCHEMA}style_cell_means
            (time_class, eco3, color, n, {', '.join(STYLE_AXES)})
        SELECT g.time_class, SUBSTR(COALESCE(g.eco, '?'), 1, 3), f.color,
               COUNT(*), {axes}
        FROM {SCHEMA}position_features f
        JOIN games g ON g.game_id = f.game_id
        WHERE g.variant IS NULL AND g.time_class IS NOT NULL
        GROUP BY 1, 2, 3
        HAVING COUNT(*) >= :min_cell"""), {"min_cell": MIN_CELL})
    conn.execute(text(f"""
        INSERT INTO {SCHEMA}style_cell_means
            (time_class, eco3, color, n, {', '.join(STYLE_AXES)})
        SELECT g.time_class, '*', f.color, COUNT(*), {axes}
        FROM {SCHEMA}position_features f
        JOIN games g ON g.game_id = f.game_id
        WHERE g.variant IS NULL AND g.time_class IS NOT NULL
        GROUP BY 1, 2, 3"""))
    return int(conn.execute(text(
        "SELECT COUNT(*) FROM {SCHEMA}style_cell_means")).scalar() or 0)


def build_player_vectors(conn) -> int:
    """Full-history centred vectors for every player with enough games."""
    centred = ", ".join(
        f"AVG(f.{a} - COALESCE(c.{a}, cf.{a})) AS {a}" for a in STYLE_AXES)
    conn.execute(text(f"DELETE FROM {SCHEMA}player_style_vectors"))
    conn.execute(text(f"""
        INSERT INTO {SCHEMA}player_style_vectors
            (player_id, time_class, n, mean_elo, {', '.join(STYLE_AXES)})
        SELECT p.player_id, g.time_class, COUNT(*),
               AVG(CASE WHEN g.white_player_id = p.player_id
                        THEN g.white_elo ELSE g.black_elo END),
               {centred}
        FROM {SCHEMA}position_features f
        JOIN games g ON g.game_id = f.game_id
        JOIN players p
          ON (p.player_id = g.white_player_id AND f.color = 'white')
          OR (p.player_id = g.black_player_id AND f.color = 'black')
        LEFT JOIN {SCHEMA}style_cell_means c
          ON c.time_class = g.time_class AND c.color = f.color
         AND c.eco3 = SUBSTR(COALESCE(g.eco, '?'), 1, 3)
        LEFT JOIN {SCHEMA}style_cell_means cf
          ON cf.time_class = g.time_class AND cf.color = f.color AND cf.eco3 = '*'
        WHERE g.variant IS NULL AND g.time_class IS NOT NULL
          AND g.white_elo IS NOT NULL AND g.black_elo IS NOT NULL
        GROUP BY 1, 2
        HAVING COUNT(*) >= :min_games"""), {"min_games": MIN_PLAYER_GAMES})
    return int(conn.execute(text(
        "SELECT COUNT(*) FROM {SCHEMA}player_style_vectors")).scalar() or 0)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m analysis.build_features")
    parser.add_argument("--features", action="store_true",
                        help="rebuild position_features only, skipping the "
                             "aggregates derived from it")
    args = parser.parse_args(argv)

    init_engine_db()
    with analysis_engine().begin() as conn:
        rows = build_position_features(conn)
        print(f"position_features: {rows:,} rows", file=sys.stderr)
        if args.features:
            return 0
        cells = build_cell_means(conn)
        print(f"style_cell_means: {cells:,} cells", file=sys.stderr)
        vectors = build_player_vectors(conn)
        print(f"player_style_vectors: {vectors:,} vectors", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_style_vectors.py -v`
Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
uv run ruff check analysis/ && uv run mypy analysis/
git add analysis/build_features.py tests/test_style_vectors.py
git commit -m "feat: populate the style tables from the corpus"
```

---

## Task 4: Centring and the coarse fallback

**Files:**
- Test: `tests/test_style_vectors.py` (append)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_style_vectors.py`:

```python
from analysis.build_features import build_cell_means, build_player_vectors


def seed_games(conn, n, time_class="blitz", eco="B30", elo=1500,
               space=5.0, start_id=1):
    """n games where our player is White, each with the same measured values."""
    conn.execute(text("INSERT OR IGNORE INTO players VALUES (1, 'subject')"))
    conn.execute(text("INSERT OR IGNORE INTO players VALUES (2, 'other')"))
    for i in range(n):
        gid = start_id + i
        conn.execute(text(
            "INSERT INTO games (game_id, white_player_id, black_player_id, "
            "time_class, eco, variant, white_elo, black_elo) "
            "VALUES (:g, 1, 2, :tc, :eco, NULL, :elo, :elo)"),
            {"g": gid, "tc": time_class, "eco": eco, "elo": elo})
        for color in ("white", "black"):
            conn.execute(text(
                "INSERT INTO position_features VALUES (:g, :c, :s, 0, 0, 0)"),
                {"g": gid, "c": color, "s": space if color == "white" else 0.0})


class TestCentring:
    def test_a_populated_cell_becomes_its_own_centre(self, conn):
        """With every game identical, each player's centred value is exactly 0 --
        the whole point of centring, and a sign error would show as +/-5."""
        seed_games(conn, 50)
        build_cell_means(conn)
        build_player_vectors(conn)
        v = conn.execute(text(
            "SELECT space FROM player_style_vectors WHERE player_id = 1")).scalar()
        assert v == pytest.approx(0.0)

    def test_a_thin_cell_falls_back_to_the_coarse_mean(self, conn):
        """B30 has 50 games so it gets a cell; C00 has 5 and does not. The C00
        games must still be centred -- dropping them would bias the profile
        toward whichever openings happen to be popular."""
        seed_games(conn, 50, eco="B30", space=5.0)
        seed_games(conn, 5, eco="C00", space=9.0, start_id=100)
        build_cell_means(conn)
        build_player_vectors(conn)
        cells = {r[0] for r in conn.execute(text(
            "SELECT eco3 FROM style_cell_means WHERE color = 'white'"))}
        assert cells == {"B30", "*"}
        n = conn.execute(text(
            "SELECT n FROM player_style_vectors WHERE player_id = 1")).scalar()
        assert n == 55, "the thin-cell games must still be counted"

    def test_a_player_below_the_game_threshold_gets_no_vector(self, conn):
        seed_games(conn, 10)
        build_cell_means(conn)
        build_player_vectors(conn)
        assert conn.execute(text(
            "SELECT COUNT(*) FROM player_style_vectors")).scalar() == 0

    def test_vectors_are_separate_per_time_class(self, conn):
        seed_games(conn, 40, time_class="blitz")
        seed_games(conn, 40, time_class="bullet", start_id=200)
        build_cell_means(conn)
        build_player_vectors(conn)
        classes = {r[0] for r in conn.execute(text(
            "SELECT time_class FROM player_style_vectors WHERE player_id = 1"))}
        assert classes == {"blitz", "bullet"}
```

`build_features.SCHEMA` defaults to `"engine."`, but the fixture keeps both schemas in one database. Add this autouse fixture to `tests/test_style_vectors.py`, above the `conn` fixture, so the sidecar prefix resolves to nothing:

```python
@pytest.fixture(autouse=True)
def _local_schema(monkeypatch):
    """Tests keep both schemas in one database, so the sidecar prefix is empty.

    Production ATTACHes the sidecar under the alias "engine"; the SQL is
    otherwise identical, so this exercises the same statements.
    """
    from analysis import build_features

    from app import style
    monkeypatch.setattr(build_features, "SCHEMA", "")
    monkeypatch.setattr(style, "SCHEMA", "")
```

`app.style` does not exist until Task 5. Until then, drop its two lines and add them back when Task 5 lands.

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_style_vectors.py -k Centring -v`
Expected: FAIL — `no such table: engine.position_features` before the `SCHEMA` change

- [ ] **Step 3: Add the autouse fixture shown above**

No production code changes in this task — `SCHEMA` already exists from Task 3. If the tests still fail with `no such table: engine.position_features`, a statement in `build_features.py` was left as a plain string instead of an f-string; convert it.

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_style_vectors.py -v`
Expected: 9 passed

- [ ] **Step 5: Commit**

```bash
uv run ruff check analysis/ && uv run mypy analysis/
git add analysis/build_features.py tests/test_style_vectors.py
git commit -m "feat: centre style values within opening and colour, with a coarse fallback"
```

---

## Task 5: The subject's filtered vector

**Files:**
- Create: `app/style.py`
- Test: `tests/test_style_vectors.py` (append)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_style_vectors.py`:

```python
import math

from app.style import AXES, subject_vector


class TestSubjectVector:
    def test_it_returns_a_mean_and_a_standard_error_per_axis(self, conn):
        seed_games(conn, 40)
        build_cell_means(conn)
        result = subject_vector(conn, player_id=1, time_class="blitz")
        assert set(result.axes) == set(AXES)
        assert result.n == 40
        for axis in AXES:
            assert math.isfinite(result.axes[axis].mean)
            assert result.axes[axis].se >= 0.0

    def test_identical_games_give_a_zero_standard_error(self, conn):
        """Every game measured the same, so the mean cannot be uncertain. A
        standard error computed as SD/sqrt(n) with a wrong SD shows up here."""
        seed_games(conn, 40)
        build_cell_means(conn)
        result = subject_vector(conn, player_id=1, time_class="blitz")
        assert result.axes["space"].se == pytest.approx(0.0)

    def test_the_standard_error_shrinks_as_games_accumulate(self, conn):
        for i in range(40):
            seed_games(conn, 1, space=float(i), start_id=i + 1)
        build_cell_means(conn)
        few = subject_vector(conn, 1, "blitz", limit_game_ids=list(range(1, 6)))
        many = subject_vector(conn, 1, "blitz")
        assert many.axes["space"].se < few.axes["space"].se

    def test_no_games_gives_an_empty_vector_not_a_crash(self, conn):
        """Narrowing a filter to nothing must not divide by zero."""
        result = subject_vector(conn, player_id=1, time_class="rapid")
        assert result.n == 0
        assert result.axes == {}

    def test_the_time_class_filter_is_honoured(self, conn):
        seed_games(conn, 40, time_class="blitz")
        seed_games(conn, 10, time_class="bullet", start_id=200)
        build_cell_means(conn)
        assert subject_vector(conn, 1, "blitz").n == 40
        assert subject_vector(conn, 1, "bullet").n == 10
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_style_vectors.py -k SubjectVector -v`
Expected: `ModuleNotFoundError: No module named 'app.style'`

- [ ] **Step 3: Implement**

Create `app/style.py`:

```python
"""Read path for the style panel.

Every function takes a connection rather than reaching for a global, so tests
can pass an in-memory database holding both schemas. Same pattern as
engine/scope.py.

These axes measure STYLE, NOT ABILITY -- correlation with Elo is 0.02-0.08.
Nothing here may be presented as better or worse.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from sqlalchemy import text

#: Display order for the axes.
AXES = ("space", "mobility", "king_safety", "pawn_structure")

#: Schema prefix for the sidecar tables; empty in tests. See build_features.
SCHEMA = "engine."


@dataclass
class AxisValue:
    """One axis: the centred mean and how precisely it is known."""

    mean: float
    se: float


@dataclass
class Vector:
    """A player's style over some set of games."""

    n: int = 0
    axes: dict[str, AxisValue] = field(default_factory=dict)


def subject_vector(
    conn,
    player_id: int,
    time_class: str | None = None,
    extra_clause: str = "",
    params: dict | None = None,
    limit_game_ids: list[int] | None = None,
) -> Vector:
    """The player's centred vector over the games the filters select.

    extra_clause and params come from crud._build_game_filters, so the panel
    cannot drift from the rest of the UI. The games table is aliased 'g' there,
    and is aliased 'g' here for that reason.

    Standard error is SD/sqrt(n), with SD taken as sqrt(E[x^2] - E[x]^2) because
    SQLite has no STDDEV. At small n the resulting interval is very wide, which
    is the correct display rather than a problem to hide.
    """
    params = dict(params or {})
    params["player_id"] = player_id

    selects = []
    for axis in AXES:
        centred = f"(f.{axis} - COALESCE(c.{axis}, cf.{axis}))"
        selects.append(f"AVG({centred}) AS {axis}_mean")
        selects.append(f"AVG({centred} * {centred}) AS {axis}_sq")

    clauses = ["g.variant IS NULL"]
    if time_class:
        clauses.append("g.time_class = :time_class")
        params["time_class"] = time_class
    if extra_clause:
        clauses.append(extra_clause)
    if limit_game_ids is not None:
        ids = ",".join(str(int(g)) for g in limit_game_ids) or "NULL"
        clauses.append(f"g.game_id IN ({ids})")

    row = conn.execute(text(f"""
        SELECT COUNT(*) AS n, {', '.join(selects)}
        FROM games g
        JOIN {SCHEMA}position_features f
          ON f.game_id = g.game_id
         AND f.color = CASE WHEN g.white_player_id = :player_id
                            THEN 'white' ELSE 'black' END
        LEFT JOIN {SCHEMA}style_cell_means c
          ON c.time_class = g.time_class AND c.color = f.color
         AND c.eco3 = SUBSTR(COALESCE(g.eco, '?'), 1, 3)
        LEFT JOIN {SCHEMA}style_cell_means cf
          ON cf.time_class = g.time_class AND cf.color = f.color AND cf.eco3 = '*'
        WHERE (g.white_player_id = :player_id OR g.black_player_id = :player_id)
          AND {' AND '.join(clauses)}"""), params).mappings().first()

    if not row or not row["n"]:
        return Vector()

    n = int(row["n"])
    axes = {}
    for axis in AXES:
        mean = float(row[f"{axis}_mean"])
        # Floating-point error can push a zero variance slightly negative.
        variance = max(0.0, float(row[f"{axis}_sq"]) - mean * mean)
        axes[axis] = AxisValue(mean=mean, se=math.sqrt(variance / n))
    return Vector(n=n, axes=axes)
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_style_vectors.py -v`
Expected: 14 passed

- [ ] **Step 5: Commit**

```bash
uv run ruff check app/ && uv run mypy app/
git add app/style.py tests/test_style_vectors.py
git commit -m "feat: compute a player's centred style vector over filtered games"
```

---

## Task 6: Percentiles and error bars

**Files:**
- Modify: `app/style.py`
- Test: `tests/test_style_vectors.py` (append)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_style_vectors.py`:

```python
from app.style import percentile_profile


def seed_reference(conn, values, time_class="blitz", elo=2000.0):
    """One reference player per value, so percentiles are hand-checkable."""
    for i, v in enumerate(values, start=10):
        conn.execute(text(
            "INSERT INTO player_style_vectors VALUES "
            "(:p, :tc, 100, :elo, :s, :s, :s, :s)"),
            {"p": i, "tc": time_class, "elo": elo, "s": v})


class TestPercentile:
    def test_the_median_lands_mid_scale(self, conn):
        seed_reference(conn, [0.0, 1.0, 2.0, 3.0, 4.0])
        axes = {a: AxisValue(mean=2.0, se=0.0) for a in AXES}
        out = percentile_profile(conn, Vector(n=50, axes=axes), "blitz")
        assert out["space"]["percentile"] == pytest.approx(40, abs=15)

    def test_an_extreme_value_lands_at_the_top(self, conn):
        seed_reference(conn, [0.0, 1.0, 2.0, 3.0, 4.0])
        axes = {a: AxisValue(mean=99.0, se=0.0) for a in AXES}
        out = percentile_profile(conn, Vector(n=50, axes=axes), "blitz")
        assert out["space"]["percentile"] == 100

    def test_a_large_standard_error_widens_the_interval(self, conn):
        """This is the whole low-n design decision: the panel never disappears,
        the bar just grows until it says nothing, which is honest."""
        seed_reference(conn, [float(i) for i in range(100)])
        tight = percentile_profile(
            conn, Vector(n=500, axes={a: AxisValue(50.0, 0.1) for a in AXES}), "blitz")
        loose = percentile_profile(
            conn, Vector(n=2, axes={a: AxisValue(50.0, 40.0) for a in AXES}), "blitz")
        tight_width = tight["space"]["high"] - tight["space"]["low"]
        loose_width = loose["space"]["high"] - loose["space"]["low"]
        assert loose_width > tight_width
        assert loose_width > 50

    def test_an_empty_vector_produces_an_empty_profile(self, conn):
        seed_reference(conn, [0.0, 1.0])
        assert percentile_profile(conn, Vector(), "blitz") == {}

    def test_an_empty_reference_produces_an_empty_profile(self, conn):
        axes = {a: AxisValue(mean=1.0, se=0.0) for a in AXES}
        assert percentile_profile(conn, Vector(n=50, axes=axes), "blitz") == {}
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_style_vectors.py -k Percentile -v`
Expected: `ImportError: cannot import name 'percentile_profile'`

- [ ] **Step 3: Implement**

Append to `app/style.py`:

```python
#: 95% interval. Two-sided normal approximation.
Z_95 = 1.96


def _reference_values(conn, time_class: str) -> dict[str, list[float]]:
    """Every reference player's value per axis, sorted, for that time class.

    The percentile reference is EVERY player with a vector -- not the 2800+ pool
    used for similarity. They answer different questions and conflating them is
    the mistake this project has already made four times.
    """
    rows = conn.execute(text(
        f"SELECT {', '.join(AXES)} FROM {SCHEMA}player_style_vectors "
        "WHERE time_class = :tc"), {"tc": time_class}).mappings().all()
    return {axis: sorted(float(r[axis]) for r in rows) for axis in AXES}


def _rank(sorted_values: list[float], value: float) -> int:
    """Percentile of value within sorted_values, 0-100."""
    if not sorted_values:
        return 0
    below = sum(1 for v in sorted_values if v < value)
    return round(100 * below / len(sorted_values))


def percentile_profile(conn, vector: Vector, time_class: str) -> dict:
    """Each axis as a percentile, with an interval from the standard error.

    The interval is the point estimate +/- 1.96 SE mapped through the same rank
    function, so the bar is in the same units as the dot. At small n it
    approaches the full width of the axis -- the panel stays visible and simply
    stops claiming anything.
    """
    if not vector.axes:
        return {}
    reference = _reference_values(conn, time_class)
    if not any(reference.values()):
        return {}

    out = {}
    for axis in AXES:
        value = vector.axes[axis]
        margin = Z_95 * value.se
        out[axis] = {
            "value": value.mean,
            "percentile": _rank(reference[axis], value.mean),
            "low": _rank(reference[axis], value.mean - margin),
            "high": _rank(reference[axis], value.mean + margin),
        }
    return out
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_style_vectors.py -v`
Expected: 19 passed

- [ ] **Step 5: Commit**

```bash
uv run ruff check app/ && uv run mypy app/
git add app/style.py tests/test_style_vectors.py
git commit -m "feat: rank style axes as percentiles with error bars"
```

---

## Task 7: Similarity against the 2800+ blitz pool

**Files:**
- Modify: `app/style.py`
- Test: `tests/test_style_vectors.py` (append)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_style_vectors.py`:

```python
from app.style import ELITE_MIN_ELO, SIMILARITY_CLASS, similar_players


class TestSimilarity:
    def test_the_nearest_player_comes_first(self, conn):
        for pid, v in ((10, 0.0), (11, 5.0), (12, 10.0)):
            conn.execute(text(
                "INSERT INTO players VALUES (:p, :u)"),
                {"p": pid, "u": f"gm{pid}"})
            conn.execute(text(
                "INSERT INTO player_style_vectors VALUES "
                "(:p, 'blitz', 200, 2900, :v, :v, :v, :v)"), {"p": pid, "v": v})
        axes = {a: AxisValue(mean=0.2, se=0.0) for a in AXES}
        out = similar_players(conn, Vector(n=100, axes=axes))
        assert out[0]["username"] == "gm10"
        assert out[0]["distance"] < out[-1]["distance"]

    def test_players_below_the_rating_floor_are_excluded(self, conn):
        for pid, elo in ((10, 2900), (11, 2500)):
            conn.execute(text("INSERT INTO players VALUES (:p, :u)"),
                         {"p": pid, "u": f"p{pid}"})
            conn.execute(text(
                "INSERT INTO player_style_vectors VALUES "
                "(:p, 'blitz', 200, :e, 0, 0, 0, 0)"), {"p": pid, "e": elo})
        axes = {a: AxisValue(mean=0.0, se=0.0) for a in AXES}
        names = {r["username"] for r in similar_players(conn, Vector(100, axes))}
        assert names == {"p10"}
        assert ELITE_MIN_ELO == 2800

    def test_only_blitz_vectors_are_used(self, conn):
        """The reference is always blitz, whatever class the subject is viewing:
        top players barely play rapid online, and gating per class leaves 7
        usable reference players for a rapid subject against 405 for blitz."""
        conn.execute(text("INSERT INTO players VALUES (10, 'gm')"))
        conn.execute(text(
            "INSERT INTO player_style_vectors VALUES "
            "(10, 'rapid', 200, 2900, 0, 0, 0, 0)"))
        axes = {a: AxisValue(mean=0.0, se=0.0) for a in AXES}
        assert similar_players(conn, Vector(100, axes)) == []
        assert SIMILARITY_CLASS == "blitz"

    def test_axes_are_standardised_before_the_distance(self, conn):
        """mobility has roughly triple the raw spread of space, so an
        unstandardised distance would be a mobility ranking wearing a costume.
        Here p10 matches on mobility only and p11 on space only; with equal
        standardised offsets they must come out equidistant."""
        for pid, sp, mob in ((10, 3.0, 0.0), (11, 0.0, 9.0)):
            conn.execute(text("INSERT INTO players VALUES (:p, :u)"),
                         {"p": pid, "u": f"p{pid}"})
            conn.execute(text(
                "INSERT INTO player_style_vectors "
                "(player_id, time_class, n, mean_elo, space, mobility, "
                "king_safety, pawn_structure) "
                "VALUES (:p, 'blitz', 200, 2900, :s, :m, 0, 0)"),
                {"p": pid, "s": sp, "m": mob})
        conn.execute(text("INSERT INTO players VALUES (12, 'spread')"))
        conn.execute(text(
            "INSERT INTO player_style_vectors VALUES "
            "(12, 'blitz', 200, 2900, -3.0, -9.0, 0, 0)"))
        axes = {a: AxisValue(mean=0.0, se=0.0) for a in AXES}
        out = {r["username"]: r["distance"] for r in similar_players(conn, Vector(100, axes))}
        assert out["p10"] == pytest.approx(out["p11"], rel=0.01)

    def test_an_empty_vector_returns_nothing(self, conn):
        assert similar_players(conn, Vector()) == []
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_style_vectors.py -k Similarity -v`
Expected: `ImportError: cannot import name 'ELITE_MIN_ELO'`

- [ ] **Step 3: Implement**

Append to `app/style.py`:

```python
#: Rating floor for the similarity pool, applied to BLITZ rating.
#: On chess.com the players recognisable as super-GMs sit near 3000; 2400 would
#: pad the pool with players the comparison is not about. 472 players clear
#: 2800 with 30+ games, against 650 at 2400 -- 7% fewer games for a pool that
#: means what it says.
ELITE_MIN_ELO = 2800

#: The reference is always blitz, whatever class the subject is viewing. Blitz
#: is the de facto online time control and top players barely play rapid there:
#: gating per class leaves 7 usable reference players for a rapid subject,
#: against 405 for blitz and 107 for bullet.
#:
#: Comparing across classes is sound because both sides are centred within their
#: own (time class, opening, colour) norm, so each reads as "more than is normal
#: here". Measured spreads across classes differ by at most 16%, and
#: standardising below removes even that.
SIMILARITY_CLASS = "blitz"

#: How many neighbours to return.
SIMILAR_COUNT = 5


def similar_players(conn, vector: Vector) -> list[dict]:
    """The nearest players in standardised style space.

    Standardising is not optional: mobility has roughly triple the raw spread of
    space, so an unstandardised Euclidean distance would rank almost entirely on
    mobility while appearing to use all four axes.
    """
    if not vector.axes:
        return []

    rows = conn.execute(text(f"""
        SELECT p.username, v.mean_elo, {', '.join('v.' + a for a in AXES)}
        FROM {SCHEMA}player_style_vectors v
        JOIN players p ON p.player_id = v.player_id
        WHERE v.time_class = :tc AND v.mean_elo >= :floor"""),
        {"tc": SIMILARITY_CLASS, "floor": ELITE_MIN_ELO}).mappings().all()
    if not rows:
        return []

    scale = {}
    for axis in AXES:
        values = [float(r[axis]) for r in rows]
        mean = sum(values) / len(values)
        variance = sum((v - mean) ** 2 for v in values) / len(values)
        # A degenerate axis contributes nothing rather than dividing by zero.
        scale[axis] = math.sqrt(variance) or 1.0

    out = []
    for row in rows:
        distance = math.sqrt(sum(
            ((float(row[axis]) - vector.axes[axis].mean) / scale[axis]) ** 2
            for axis in AXES))
        out.append({"username": row["username"],
                    "elo": round(float(row["mean_elo"])),
                    "distance": round(distance, 3)})
    out.sort(key=lambda r: r["distance"])
    return out[:SIMILAR_COUNT]
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_style_vectors.py -v`
Expected: 24 passed

- [ ] **Step 5: Commit**

```bash
uv run ruff check app/ && uv run mypy app/
git add app/style.py tests/test_style_vectors.py
git commit -m "feat: find the nearest style neighbours among 2800+ blitz players"
```

---

## Task 8: The API route

**Files:**
- Modify: `app/main.py` (append after the last analytics route)
- Test: `tests/test_style_api.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_style_api.py`:

```python
"""The style route: shape, filter pass-through, and the empty cases."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.database import get_db
from app.main import app
from tests.conftest import make_player


@pytest.fixture
def client(db):
    app.dependency_overrides[get_db] = lambda: db
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_an_unknown_player_is_a_404(client):
    r = client.get("/api/players/nobody/analytics/style")
    assert r.status_code == 404


def test_a_player_with_no_features_gets_an_empty_profile(client, db):
    make_player(db, "subject")
    db.commit()
    r = client.get("/api/players/subject/analytics/style")
    assert r.status_code == 200
    body = r.json()
    assert body["n_games"] == 0
    assert body["axes"] == []
    assert body["similar"] == []


def test_the_response_names_both_reference_sets(client, db):
    """Percentiles rank against every player with a vector; similarity uses the
    2800+ blitz pool. Reporting one under the other's name is exactly the
    mistake that produced four dissolved findings."""
    make_player(db, "subject")
    db.commit()
    body = client.get("/api/players/subject/analytics/style").json()
    assert body["percentile_reference"]["pool"]
    assert body["similarity_reference"]["pool"] == "2800+ blitz"
    assert body["similarity_reference"]["vectors_from"] == "blitz"
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_style_api.py -v`
Expected: 404-route errors — `assert 404 == 200` on the second test, because the route does not exist

- [ ] **Step 3: Implement**

Append to `app/main.py`:

```python
@app.get("/api/players/{username}/analytics/style")
def style_profile(
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
    return crud.style_profile(
        db, player.player_id, time_class,
        start_date, end_date, player_color, opening_names, tz=tz,
    )
```

Append to `app/crud.py`:

```python
def style_profile(
    db: Session,
    player_id: int,
    time_class: Optional[str] = None,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    player_color: Optional[str] = None,
    opening_names: Optional[str] = None,
    tz: Optional[str] = None,
) -> dict:
    """Where this player sits on the four style axes, and who they resemble.

    Reuses _build_game_filters so the panel can never drift from the rest of the
    UI, and returns the two reference populations separately: percentiles rank
    against every player with a vector, similarity against the 2800+ blitz pool.
    """
    where, params = _build_game_filters(
        player_id, time_class, start_date, end_date,
        player_color, opening_names, tz)

    conn = db.connection()
    attach_engine_db(conn)

    vector = style.subject_vector(
        conn, player_id, time_class, extra_clause=where, params=params)
    ranked = style.percentile_profile(conn, vector, time_class or "blitz")
    similar = style.similar_players(conn, vector) if vector.axes else []

    n_reference = conn.execute(text(
        f"SELECT COUNT(*) FROM {style.SCHEMA}player_style_vectors "
        "WHERE time_class = :tc"), {"tc": time_class or "blitz"}).scalar() or 0
    n_pool = conn.execute(text(
        f"SELECT COUNT(*) FROM {style.SCHEMA}player_style_vectors "
        "WHERE time_class = :tc AND mean_elo >= :floor"),
        {"tc": style.SIMILARITY_CLASS, "floor": style.ELITE_MIN_ELO}).scalar() or 0

    return {
        "n_games": vector.n,
        "time_class": time_class,
        "percentile_reference": {
            "pool": "all players with 30+ games",
            "n_players": int(n_reference),
            "time_class": time_class or "blitz",
        },
        "similarity_reference": {
            "pool": "2800+ blitz",
            "n_players": int(n_pool),
            "vectors_from": style.SIMILARITY_CLASS,
        },
        "axes": [{"axis": a, **ranked[a]} for a in style.AXES if a in ranked],
        "similar": similar,
    }
```

Add to the imports at the top of `app/crud.py`:

```python
from app import style
from engine.db import attach_engine_db
```

`attach_engine_db` is a no-op when the sidecar is already attached; on an in-memory test database the tables simply will not exist, which is why the empty-case tests above pass. Guard for that inside `subject_vector` by catching `OperationalError` and returning an empty `Vector`:

```python
from sqlalchemy.exc import OperationalError
```

and wrap the `conn.execute` in each of the three read functions, returning that function's own empty value — they differ, and returning the wrong one is a `TypeError` at the call site:

```python
# in subject_vector, around the single conn.execute:
    try:
        row = conn.execute(text(f"""..."""), params).mappings().first()
    except OperationalError:
        # The sidecar is not attached, or has never been built. An absent
        # profile is the correct answer, not a 500.
        return Vector()

# in _reference_values -- returns a dict of lists:
    try:
        rows = conn.execute(...).mappings().all()
    except OperationalError:
        return {axis: [] for axis in AXES}

# in similar_players -- returns a list:
    try:
        rows = conn.execute(...).mappings().all()
    except OperationalError:
        return []
```

`percentile_profile` already returns `{}` when every reference list is empty, so it needs no guard of its own.

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_style_api.py -v`
Expected: 3 passed

- [ ] **Step 5: Run the whole suite**

Run: `uv run pytest`
Expected: all pass, 86 pre-existing plus the new ones

- [ ] **Step 6: Commit**

```bash
uv run ruff check . && uv run mypy .
git add app/main.py app/crud.py app/style.py tests/test_style_api.py
git commit -m "feat: expose the style profile as an analytics route"
```

---

## Task 9: The panel

**Files:**
- Create: `app/static/style-panel.js`
- Modify: `app/static/index.html`, `app/static/app.js`, `app/static/style.css`

- [ ] **Step 1: Add the markup**

In `app/static/index.html`, after the `opening-stats-overview` section (around line 190), add:

```html
<div id="style-panel" class="section hidden">
    <h2>
        Playing style
        <span class="info-tip" data-tip="These axes describe how you play, not how well. They are stable traits that barely track rating, so neither end is better.">?</span>
    </h2>
    <p id="style-meta" class="panel-meta"></p>
    <canvas id="style-chart" height="180"></canvas>
    <h3>
        Closest in style among 2800+ blitz players
        <span class="info-tip" id="style-similar-tip" data-tip="">?</span>
    </h3>
    <ol id="style-similar" class="style-similar"></ol>
</div>
```

Bump the `app.js` cache-buster and add the new script, replacing line 475:

```html
    <script src="/static/app.js?v=78"></script>
    <script src="/static/style-panel.js?v=1"></script>
```

- [ ] **Step 2: Add the styles**

Append to `app/static/style.css`:

```css
.panel-meta { font-size: 0.85rem; opacity: 0.75; margin: 0 0 0.75rem; }

.style-similar { margin: 0.5rem 0 0; padding-left: 1.25rem; }
.style-similar li { padding: 0.15rem 0; }
.style-similar .distance { opacity: 0.6; font-variant-numeric: tabular-nums; }

.info-tip {
    display: inline-block; width: 1.1em; height: 1.1em; line-height: 1.1em;
    text-align: center; border-radius: 50%; font-size: 0.7em;
    background: rgba(127, 127, 127, 0.25); cursor: help; vertical-align: super;
}
```

- [ ] **Step 3: Implement the panel**

Create `app/static/style-panel.js`:

```javascript
/** The style panel.
 *
 *  A separate file from app.js, which is already 2,387 lines. Loaded as a plain
 *  script after app.js and using the same globals (fetchJSON, buildFilterParams,
 *  currentTimeClass), matching the existing convention.
 *
 *  These axes describe HOW someone plays, not how well: they are stable traits
 *  whose correlation with rating is 0.02-0.08. No label here may imply one end
 *  is better.
 */

const STYLE_AXIS_LABELS = {
    space: 'Space',
    mobility: 'Piece activity',
    king_safety: 'King shelter',
    pawn_structure: 'Pawn soundness',
};

let styleChart = null;

async function loadStylePanel(username) {
    try {
        const data = await fetchJSON(
            `/api/players/${username}/analytics/style${buildFilterParams()}`);
        renderStylePanel(data);
    } catch (e) {
        console.error('Error loading style profile', e);
    }
}

function renderStylePanel(data) {
    const panel = document.getElementById('style-panel');
    const meta = document.getElementById('style-meta');

    if (!data.axes.length) {
        meta.textContent = data.n_games
            ? 'Not enough reference data to place these games yet.'
            : 'No games match the current filters.';
        if (styleChart) { styleChart.destroy(); styleChart = null; }
        document.getElementById('style-similar').innerHTML = '';
        return;
    }

    meta.textContent =
        `${data.n_games.toLocaleString()} games · percentile against `
        + `${data.percentile_reference.n_players.toLocaleString()} players `
        + `with 30+ ${data.percentile_reference.time_class} games`;

    drawStyleChart(data.axes);
    renderStyleSimilar(data);
}

/** Horizontal bars centred on the median, with the 95% interval as an error
 *  bar. Deliberately not a radar: a radar cannot show uncertainty, which is the
 *  whole point at small sample sizes, and its area encodes nothing when the
 *  axes have unrelated units. */
function drawStyleChart(axes) {
    const ctx = document.getElementById('style-chart');
    if (styleChart) styleChart.destroy();

    styleChart = new Chart(ctx, {
        type: 'bar',
        data: {
            labels: axes.map(a => STYLE_AXIS_LABELS[a.axis] || a.axis),
            datasets: [{
                label: 'percentile',
                data: axes.map(a => a.percentile - 50),
                backgroundColor: axes.map(a =>
                    a.percentile >= 50 ? 'rgba(90, 150, 220, 0.75)'
                                       : 'rgba(200, 140, 90, 0.75)'),
                errorLow: axes.map(a => a.low - 50),
                errorHigh: axes.map(a => a.high - 50),
            }],
        },
        options: {
            indexAxis: 'y',
            scales: {
                x: {
                    min: -50, max: 50,
                    ticks: { callback: v => `${v + 50}` },
                    title: { display: true, text: 'percentile' },
                },
            },
            plugins: {
                legend: { display: false },
                tooltip: {
                    callbacks: {
                        label: ctx => {
                            const a = axes[ctx.dataIndex];
                            return `${a.percentile}th percentile `
                                 + `(${a.low}–${a.high} at 95%)`;
                        },
                    },
                },
            },
        },
        plugins: [styleErrorBars],
    });
}

/** Chart.js has no built-in error bars. Drawing them in an afterDatasetsDraw
 *  hook keeps the interval on the same scale as the bar it belongs to. */
const styleErrorBars = {
    id: 'styleErrorBars',
    afterDatasetsDraw(chart) {
        const { ctx, scales: { x, y } } = chart;
        const ds = chart.data.datasets[0];
        ctx.save();
        ctx.strokeStyle = 'rgba(70, 70, 70, 0.85)';
        ctx.lineWidth = 1.5;
        ds.data.forEach((_, i) => {
            const cy = y.getPixelForValue(i);
            const lo = x.getPixelForValue(ds.errorLow[i]);
            const hi = x.getPixelForValue(ds.errorHigh[i]);
            ctx.beginPath();
            ctx.moveTo(lo, cy); ctx.lineTo(hi, cy);
            ctx.moveTo(lo, cy - 5); ctx.lineTo(lo, cy + 5);
            ctx.moveTo(hi, cy - 5); ctx.lineTo(hi, cy + 5);
            ctx.stroke();
        });
        ctx.restore();
    },
};

function renderStyleSimilar(data) {
    const list = document.getElementById('style-similar');
    const tip = document.getElementById('style-similar-tip');
    const viewing = data.time_class || 'blitz';
    const crossing = viewing !== data.similarity_reference.vectors_from;

    tip.dataset.tip =
        `Compared against players rated 2800+ in blitz, using their blitz games. `
        + (crossing
            ? 'Blitz is where strong players have the deepest online histories — '
            + `in rapid only a handful have enough games to place. You are viewing `
            + `${viewing}, so this compares your ${viewing} style to their blitz style. `
            : '')
        + 'Each side is measured relative to what is normal for its own time '
        + 'control and opening, so the comparison holds across them.';

    if (!data.similar.length) {
        list.innerHTML = '<li class="panel-meta">Not enough games to place you yet.</li>';
        return;
    }
    list.innerHTML = data.similar.map(s =>
        `<li>${s.username} <span class="distance">${s.distance.toFixed(2)}</span></li>`
    ).join('');
}
```

- [ ] **Step 4: Wire it into the load cycle**

In `app/static/app.js`, inside `loadAll`, add `loadStylePanel(currentUsername),` to the `promises` array, after `initRepertoireTabs(currentUsername),`.

- [ ] **Step 5: Verify in the browser**

```bash
uv run python -m analysis.build_features
```
Expected: three lines on stderr; `position_features` around 390,000 rows.

Then start the app and load `ballasack6`. Expected: four horizontal bars with whiskers, a meta line naming the percentile reference, and five names under "Closest in style among 2800+ blitz players". Switch the time class to bullet and confirm the bars change and the tooltip gains the cross-class sentence.

- [ ] **Step 6: Commit**

```bash
uv run ruff check . && uv run mypy . && npx eslint app/static/
git add app/static/
git commit -m "feat: add the style panel"
```

---

## Task 10: Full verification

- [ ] **Step 1: Run everything**

```bash
uv run pytest
uv run ruff check .
uv run mypy .
npx eslint app/static/
```
Expected: all green.

- [ ] **Step 2: Confirm the guardrail**

Run: `grep -rniE '\b(good|bad|better|worse|weak|strong)\b' app/static/style-panel.js app/style.py`

Expected: matches only inside explanatory comments, never in a user-visible string or an identifier. These axes measure style, not ability; any label implying otherwise is a bug.

- [ ] **Step 3: Open the PR**

```bash
git push -u origin feat/style-classifier
gh pr create --title "feat: style classifier v0" --body "Implements docs/superpowers/specs/2026-09-16-style-classifier-design.md"
```
