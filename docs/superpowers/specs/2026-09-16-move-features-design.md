# Move Features and Missed Forcing Moves

**Date:** 2026-09-16
**Status:** Approved

## Goal

Say what *kind* of move a player made, not just how much it cost. Engine
evaluation locates errors; it cannot distinguish walking past a winning capture
from drifting in a quiet position. The first question this answers: **when the
engine wanted a forcing move and the player played something quiet, how often,
and how expensive?**

Measured over 400 of the seed player's most recent bullet games (14,998 of their
own moves), at a 150 cp error threshold:

| Error type | Count | % of their moves | Mean loss |
|---|---|---|---|
| Missed a forcing move | 462 | 3.1% | **465 cp** |
| Played forcing when quiet was better | 261 | 1.7% | 378 cp |
| Other error | 1,317 | 8.8% | 383 cp |

Missed forcing moves are the most expensive category per instance and 22.6% of
all errors — roughly 1.2 per game. The signal exists before the feature does.

## Current State

`position_evals.best_move_uci` is already stored for every analyzed position
(30,392 rows for run 1; 125 NULL, exactly the terminal positions that are
recorded rather than searched). Indexing is confirmed: the row at ply *p* holds
the recommendation for the move played at ply *p+1*.

What is missing is any description of a move beyond its cost. Neither
`moves.move_san` nor `position_evals` says whether a move was a capture, a check,
a development move or a pawn push.

### Classification is nearly free

Measured over 200 real games: **1,013 games/sec single-threaded**, 11 µs/ply,
including board replay and feature extraction. Every analyzed game re-classifies
in well under a second; the entire 94,104-game elite bullet corpus would take
~1.5 minutes.

## Design

### Extraction is a separate pass from evaluation

The engine worker already has the board at every ply, so features could be
emitted there for free. They are not, deliberately.

The feature vocabulary will keep growing — pawn storms, shuffle detection,
development timing, forcing-move rate. If extraction rode along with evaluation,
adding "was this a pawn push" would mean re-running Stockfish over every game.
That is the same trade already made for thresholds, which live in a view so that
revising them costs a view definition rather than a re-run.

At 1,013 games/sec the separate pass costs nothing worth protecting.

### Played moves are engine-free; best moves are not

Two tables, split by what they depend on:

```sql
-- What was actually played. Keyed by game, NOT run: whether a move is a capture
-- does not depend on engine depth. This is what lets the same table later cover
-- the 94k elite bullet games that nobody will ever evaluate.
CREATE TABLE played_move_features (
    game_id     INTEGER NOT NULL,
    ply         INTEGER NOT NULL,
    piece       VARCHAR(1) NOT NULL,   -- P N B R Q K
    is_capture  INTEGER NOT NULL,
    gives_check INTEGER NOT NULL,
    is_castling INTEGER NOT NULL,
    is_promotion INTEGER NOT NULL,
    PRIMARY KEY (game_id, ply)
);

-- What the engine wanted instead, classified identically. Run-scoped, because
-- which move is "best" depends on the depth that produced it.
CREATE TABLE best_move_features (
    run_id      INTEGER NOT NULL,
    game_id     INTEGER NOT NULL,
    ply         INTEGER NOT NULL,      -- the ply this move would have been
    piece       VARCHAR(1) NOT NULL,
    is_capture  INTEGER NOT NULL,
    gives_check INTEGER NOT NULL,
    is_castling INTEGER NOT NULL,
    is_promotion INTEGER NOT NULL,
    PRIMARY KEY (run_id, game_id, ply)
);
```

Splitting on the engine dependency rather than lumping both into one run-scoped
table is what keeps the elite comparison reachable: 94k games can be classified
without ever being evaluated.

### The label is derived, not stored

A view classifies each move that cost something, comparing what was played
against what was wanted:

```
move_errors(run_id, game_id, ply, color, cp_loss, error_kind)
```

`error_kind` is one of `missed_forcing`, `forced_when_quiet_better`, or `other`.
"Forcing" means capture or check.

The error threshold is **not** baked in. `move_errors` exposes every move with
its `cp_loss` and its kind; deciding that 150 cp is an error is the caller's
choice, the same way blunder thresholds already are. A threshold in stored rows
is a threshold nobody can revise without a rebuild.

### Illegal or missing best moves are skipped, not guessed

`best_move_uci` is NULL for terminal positions and could in principle fail to
parse. Such a ply gets no `best_move_features` row and simply does not appear in
`move_errors`. It is not recorded as `other`: an unknown recommendation is not
evidence of a non-forcing one, and folding it into a bucket would quietly inflate
whichever bucket it landed in.

### Module

`engine/features.py`, alongside `analyze.py`. Reuses `scope.py` for game
selection, so feature extraction and evaluation always agree on what a search
covers.

The CLI gains `--features-only`, which runs extraction without any engine work —
the path for backfilling games evaluated before this existed, and for
re-extracting after the vocabulary changes. A normal run extracts features for
the games it evaluates, so the two never drift.

## Testing

| Area | What is verified |
|---|---|
| Classification | Captures, checks, castling, promotion and en passant are each identified; piece letters match |
| Indexing | The best move stored at ply *p* is compared against the move played at ply *p+1* |
| Error kinds | Quiet-instead-of-forcing, forcing-instead-of-quiet, and both-same are labelled correctly |
| Thresholds | The view applies none; a 1 cp loss and a 900 cp loss both appear |
| Skips | A NULL or unparseable best move yields no row rather than an `other` |
| Re-extraction | Running twice is idempotent and picks up a changed vocabulary |

## Out of Scope

- The remaining move taxonomy: pawn storms, shuffle detection, development
  timing, forcing-move rate.
- Any comparison against other players or Elo bands.
- Any UI or API route.
- Naming human motifs ("misses back-rank ideas"). Requires semantic pattern
  recognition that neither the engine nor these features supply.
