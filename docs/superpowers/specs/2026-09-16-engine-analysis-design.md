# Engine Analysis

**Date:** 2026-09-16
**Status:** Approved

## Goal

Add Stockfish evaluation to the corpus, so a player's mistakes can be located
rather than inferred. Every analytic in the app today is derived from metadata —
clocks, openings, results, ratings. None of them can say whether a move was
*good*. Adding engine evaluation makes the error itself a first-class fact, which
is the prerequisite for any claim about where a player's understanding is thin.

This spec covers the evaluation substrate only: producing trustworthy per-position
ground truth, on demand, for a bounded set of games. Interpretation — error
taxonomies, time-gated "didn't know vs. didn't have time" analysis, position-type
clustering — is deliberately out of scope and cheap to build afterwards.

## Current State

### The database has moves but no positions

`moves` stores SAN and clocks (`move_san`, `clock_seconds`, `time_spent_seconds`)
and nothing about the position those moves produce. There is no FEN column and no
evaluation column anywhere in the schema. `games.white_accuracy` /
`black_accuracy` exist but are NULL for every bullet game.

Stored SAN is clean and replays without ambiguity, so positions are reconstructable
with `python-chess` — already a dependency — and do not need to be fetched or stored.

### Compute is not the constraint

Measured against the current corpus on an Apple M3 (8 cores):

| | |
|---|---|
| Games | 203,736 |
| Moves (plies) | 16,982,718 |
| Seed player (`ballasack6`) standard games | 9,687 |
| Seed player bullet games | 2,160 |
| Seed player bullet plies | 156,092 |
| Mean plies per bullet game | 72.3 |

`games.total_moves` counts **full moves**, not plies, and is half the true figure.
Sizing a run from it underestimates by 2x; these counts come from the `moves`
table.

Centipawn loss needs **one** evaluation per ply, not two: evaluating every position
in sequence makes a move's loss fall out of the difference between consecutive
positions' evaluations. A game of N plies therefore costs N+1 evaluations.

Measured with Stockfish 19, single-threaded, over 150 positions sampled from real
bullet games across every phase rather than openings alone:

| Depth | ms/position |
|---|---|
| 10 | 7.2 |
| 12 | 17.5 |
| 14 | 60.6 |

Multiplying that by worker count overstates throughput by 2x. Measured
end-to-end over a 303-game batch at depth 14 on 7 workers: **48 games/min**,
against the 95 the model predicts. An M3's 8 cores are 4 performance + 4
efficiency, and an efficiency core does not do a performance core's work.

The estimator is calibrated against the measurement, not the model. This is not
cosmetic: an estimate that is 2x optimistic makes a healthy run look stalled,
and during development that is exactly what masked a real deadlock.

At depth 14 on 7 workers, calibrated:

| Scope | Games | Wall clock |
|---|---|---|
| One bullet game | 1 | ~1s |
| Seed player, all bullet | 2,160 | ~45 min |
| Seed player, all time classes | 9,687 | ~3.4 hours |
| Entire corpus | 203,736 | ~71 hours |

Depth is the lever that makes a scope affordable. The same full-bullet backlog is
~12 min at depth 12 and ~5 min at depth 10.

Analyzing one player's date-scoped window is interactive-adjacent. Analyzing the
whole corpus is not, and is not a goal.

### Storage is not the constraint

A `position_evals` row is roughly 24 bytes. The seed player's full bullet history
is ~1.8 MB; all their games across every time class is ~8 MB. The canonical
database is already 1.1 GB.

## Design

### Evaluation is opt-in, scoped, and absent by default

Engine data lives in a **separate SQLite database**, `chess_engine.db`, attached to
the canonical connection at query time. No column is added to `games` or `moves`.

This matters more than it first appears:

- **Absence of a row is the null state.** There is no nullable column to backfill,
  no `is_analyzed` flag to keep consistent, and no existing query that changes
  behaviour. Engine-aware queries `LEFT JOIN` the attached database; every other
  query is untouched.
- **A bad run is disposable.** Evaluation is a long batch job over experimental
  settings. Deleting `chess_engine.db` and re-running costs nothing and cannot
  corrupt canonical data, which is never opened for writing.
- **Promotion later is a table move.** When evaluation earns a place in the app,
  the tables move into the canonical database and the joins become native.

The cost is that `ATTACH` is SQLite-specific, so this path does not work against
the PostgreSQL `DATABASE_URL` that `app/database.py` supports. Accepted: the
consumer is a local CLI against a local SQLite file, and promotion is the moment
that constraint would need revisiting.

### Thresholds are derived, never stored

`position_evals` stores raw engine output. Centipawn loss, and the thresholds that
turn a loss into "inaccuracy" or "blunder", are computed in a **view**.

Deciding that 300 centipawns is a blunder is an interpretation, and interpretations
change. Keeping them out of the stored rows means revising them costs a view
definition rather than 158,252 re-evaluations.

Mate scores are stored as a signed distance in their own column rather than
folded into a large centipawn value, for the same reason: the clamp is a
presentation choice, applied during derivation.

### Schema

```sql
-- One row per evaluation run. Pins the settings that make results comparable.
CREATE TABLE analysis_runs (
    run_id         INTEGER PRIMARY KEY,
    engine_name    VARCHAR(50)  NOT NULL,   -- 'Stockfish'
    engine_version VARCHAR(50)  NOT NULL,   -- as reported by the binary
    depth          INTEGER      NOT NULL,
    hash_mb        INTEGER      NOT NULL,
    threads        INTEGER      NOT NULL,   -- per worker; 1 for reproducibility
    created_at     TIMESTAMP    NOT NULL
);

-- Ground truth. One row per position, always from White's point of view.
CREATE TABLE position_evals (
    run_id        INTEGER NOT NULL REFERENCES analysis_runs(run_id),
    game_id       INTEGER NOT NULL,         -- canonical games.game_id, not enforceable across databases
    ply           INTEGER NOT NULL,         -- 0 = starting position; N = position after ply N
    cp            INTEGER,                  -- centipawns; NULL when the position is a forced mate
    mate_in       INTEGER,                  -- signed distance to mate; NULL when cp is set
    best_move_uci VARCHAR(6),
    PRIMARY KEY (run_id, game_id, ply)
);

-- What has been analyzed, at what settings, and how far it got.
CREATE TABLE game_coverage (
    run_id          INTEGER NOT NULL REFERENCES analysis_runs(run_id),
    game_id         INTEGER NOT NULL,
    plies_analyzed  INTEGER NOT NULL,
    status          VARCHAR(20) NOT NULL,   -- 'complete' | 'partial' | 'failed'
    error           TEXT,
    completed_at    TIMESTAMP,
    PRIMARY KEY (run_id, game_id)
);
```

`game_id` is a plain integer, not a foreign key: it references a table in a
different database file and SQLite cannot enforce that. Referential integrity is
the job of the scope resolver, which only ever emits IDs it read from the
canonical database.

`game_coverage` answers the question that otherwise turns overlapping runs into a
mess — *what is already done, at what depth* — and doubles as the resume point for
a batch killed midway.

### Derivation

A view joins consecutive evaluations to produce per-move loss:

```
move_evals(run_id, game_id, ply, color, cp_before, cp_after, cp_loss)
```

`cp_loss` is expressed from the mover's point of view and floored at zero — a move
that improves the stored evaluation relative to the previous position reflects
search noise at fixed depth, not a gain, and negative loss would poison any
aggregate.

The difference is taken inside a **clamped window of ±1000 cp**. Once a game is
decided, evaluations swing by thousands of centipawns and every move played
afterwards books an enormous loss that describes the position rather than the
player. Measured over 400 real bullet games:

| | Open window | Clamped to ±1000 |
|---|---|---|
| Overall ACPL | 208.4 | 75.9 |
| Opening (ply 1–20) | 30.0 | 30.0 |
| Middlegame (21–60) | 232.0 | 101.1 |
| Later (61+) | 343.9 | 77.6 |

The clamp does not merely rescale — it changes which phase looks worst. Unclamped,
the endgame appears to be the problem; that is an artifact of already-lost
positions. Clamped, the middlegame is, which is the realistic pattern and the one
worth acting on. 208 is also simply not what a player at this rating plays like.

`cp_before` and `cp_after` stay **unclamped**: "this position was already lost" is
exactly the context that makes a small loss unimportant, and discarding it would
hide the distinction the clamp exists to draw.

Because this lives in a view, revising the window costs a view definition rather
than a re-run. `init_engine_db()` drops and rebuilds the view on every call for
the same reason — `CREATE VIEW IF NOT EXISTS` would leave an older database
running old thresholds while the code claims the new ones.

### Module layout

A new top-level `engine/` package, parallel to the existing `etl/`:

| Module | Responsibility |
|---|---|
| `engine/db.py` | Sidecar engine + session, URLs, `ATTACH` helper. Imports nothing from the package |
| `engine/models.py` | The three tables, the derivation view, and schema creation |
| `engine/scope.py` | `(username, date window, time class) -> [game_id]`, read-only against the canonical database |
| `engine/analyze.py` | Replay, evaluate, persist; parallel and resumable |
| `engine/cli.py` | `python -m engine.cli` entry point |

`scope.py` is split from `analyze.py` deliberately. The on-demand execution model —
analyze only what a search asked for — is a *scoping* concern, and isolating it is
what lets a later API route reuse the analyzer without inheriting a CLI's argument
parsing.

### Execution model

`analyze_games(game_ids, run_config)` is the unit of work. The CLI resolves a
scope, then calls it. A future endpoint behind an explicit "add engine evaluation"
button resolves the same scope from request parameters and calls the same function.
No UI is built in this change.

Evaluation is parallel across games, not within them: each worker owns a
single-threaded Stockfish process and a whole game, which keeps per-position
results reproducible (multi-threaded Stockfish is non-deterministic) and avoids
sharing engine state across processes.

Fixed **depth**, not fixed movetime. Movetime gives predictable wall clock and
irreproducible output; depth gives the opposite. At this scale the wall-clock
variance is irrelevant and reproducibility is worth more.

### Failure handling

- **No Stockfish binary:** fail immediately with an install instruction, before any
  database is created.
- **A game that fails to replay** (corrupt or illegal SAN) is recorded in
  `game_coverage` as `failed` with the error text, and the batch continues. One bad
  game must not cost a 7-minute run.
- **Interrupted batch:** already-complete games are skipped on the next invocation
  by consulting `game_coverage` for the same `run_id`. Re-running is idempotent.
- **Engine crash on a position:** the game is marked `partial` with the plies that
  did land, and the batch continues.
- **Worker teardown:** a worker holds no engine. python-chess runs each engine's
  event loop on a **non-daemon** thread, and CPython joins non-daemon threads
  *before* it runs `atexit` handlers, so a worker that owns an open engine has no
  point at which it can close one. The batch analyzes every game, writes every
  row, and then hangs forever with the work already done — which reads as a slow
  run rather than a deadlock. Opening one engine per game inside a `with` block
  closes it while its loop is still alive. The cost is 125 ms per game against
  ~4.4 s of search, and it removes a reproducibility flaw as well: a reused
  engine carries its transposition table into the next game, making a position's
  evaluation depend on batch ordering.

## Testing

Tests follow the existing `tests/conftest.py` pattern — in-memory SQLite,
hand-seeded corpus — and run without a Stockfish binary by substituting a stub
engine. The engine is a subprocess boundary, and a test suite that needs a 40 ms
binary call per position would be too slow to run often.

| Area | What is verified |
|---|---|
| Scope resolution | Date window boundaries are inclusive; time-class and username filters compose; variant games are excluded, matching `crud.py` |
| Replay | Stored SAN reconstructs to the expected position count; a game of N plies yields N+1 positions |
| Derivation | `cp_loss` is computed from the mover's point of view for both colours; negative loss is floored; mate rows are clamped consistently |
| Coverage | A completed game is skipped on re-run; a failed game is recorded without aborting the batch; a partial game resumes |
| Isolation | Canonical tables are never written; `ATTACH` joins return the expected rows and `LEFT JOIN` yields NULL for unanalyzed games |

## Out of Scope

Named explicitly, because each is a plausible next step and none belongs in this
change:

- Any UI, API route, or button.
- Error taxonomies and blunder-rate analytics.
- The time-gated analysis separating knowledge gaps from time-pressure errors.
- Position-type feature extraction and clustering.
- Population baselines for error rates.
- Analyzing the full 203,736-game corpus.
