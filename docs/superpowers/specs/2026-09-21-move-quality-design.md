# Move quality

**Date:** 2026-09-21
**Status:** design, approved, not implemented
**Depends on:** [2026-09-19-tactics-section-design.md](2026-09-19-tactics-section-design.md)

---

## What this is

Per-game counts of **inaccuracies, mistakes and blunders**, plus a **Miss**
flag, for every analyzed game—and a population rate to compare them against.

Two halves:

1. **Derivation.** The counts need no new engine output. `position_evals`
   already holds 102,790 plies across 1,271 games. Everything here is views.
2. **A population.** Nothing outside one player's games has ever been analyzed,
   so the comparison half needs engine coverage the project does not have. A
   button produces it.

## What this is not

**Not a reproduction of chess.com's Game Review.** Their thresholds are
published; the model that feeds them is not, and it is rating-conditioned. We
match their ladder and their unit. We do not match their labels, and a move
chess.com calls a blunder may not be one here.

**Not Brilliant, Great or Best.** Those depend on the unpublished
rating-conditioned part. Out of scope, permanently, unless somebody works out
what they actually do.

**Not a board.** Drill-down is a move list. The tactics spec called board
rendering a hard prerequisite for what it describes; this feature deliberately
routes around it by deep-linking to chess.com, which already renders positions.

**Not percentiles.** The corpus averages ~2 games per player, so per-player
rates are noise. See "Why pooled".

---

## Decisions

| Question | Decision | Why |
|---|---|---|
| Unit of severity | Δ win probability | Δ centipawns misprices every error; see the tactics spec |
| Threshold ladder | 0.05 / 0.10 / 0.20 | chess.com's published cutoffs, same unit as our fitted curve |
| Miss | A flag, not a fourth tier | Rule 4: store the fact, derive the interpretation |
| Population | Pooled band rate | Breadth we have; depth we do not |
| Population source | Corpus games, engine-analyzed on demand | Moves are already on disk for 204,513 of 204,598 games |
| Drill-down | Move list, deep-linked to chess.com | No rendering stack needed |
| Time classes | Filled independently | Each is its own run and its own fitted `k` |
| Refitting `k` | Only when absent | A refit would silently move every historical count |
| Band for the job | Median within the time class, pinned at press time | Pooling classes picks a band the player never plays in |

---

## Prior art: what is actually public

Worth recording, because it is the evidentiary basis for the ladder and it took
reading source to establish.

**Lichess is fully open.** The curve is in `scalachess`
`core/src/main/scala/eval.scala`:

```scala
def winningChances(cp: Eval.Cp) = {
  val MULTIPLIER = -0.00368208
  2 / (1 + Math.exp(MULTIPLIER * cp.value)) - 1
}.atLeast(-1).atMost(+1)
```

Note the **[-1, +1]** range, with `WinPercent = 50 + 50 * winningChances`. The
thresholds, in `lila` `modules/tree/src/main/Advice.scala`, are `.3` Blunder,
`.2` Mistake, `.1` Inaccuracy **on that ±1 scale**—so halved into probability
terms they are 0.15 / 0.10 / 0.05. Centipawns are clamped to `Cp.CEILING = 1000`
before the curve.

Their multiplier is equivalent to k = 271.6, and lila PR #11148 records how it
was obtained: `scipy.optimize.curve_fit` over 75k positions from **2300+ rated
rapid games** in the June 2022 database. That is a different population from
this corpus, which is why importing it was rejected—see "The curve" below.

Lichess handles mate separately, and two of its choices corroborate decisions
the tactics spec reached independently: `MateDelayed` returns **no judgement at
all** (M1 played as M4 is not an error), and `MateCreated`/`MateLost` are
re-cut by how won or lost the position already was (`> 999` inaccuracy,
`> 700` mistake, else blunder).

**Chess.com publishes the cutoffs and nothing else.** The help centre gives
expected-points bands—inaccuracy 0.05–0.10, mistake 0.10–0.20, blunder
0.20–1.00—and describes the model as using "data science to determine a
player's winning chances based on their rating and the engine evaluation."
Neither the functional form nor the fit is anywhere public, and the article
states outright that thresholds vary by rating. Miss is defined in words only:
"fail to capitalize on your opponent's mistake and miss the opportunity to gain
a winning position." Great and Brilliant likewise.

**Consequence:** chess.com's ladder is reproducible, chess.com's labels are
not.

---

## The curve

`wp_curve(time_class PK, k, n, fitted_at, source)`.

Fitted per time class by maximum likelihood against observed results, with
draws scored 0.5. That makes the fitted quantity **expected points**, which is
the same quantity chess.com's thresholds are denominated in—so the comparison
is like-for-like rather than approximately-like-for-like.

| time class | fitted k | n positions |
|---|---|---|
| rapid | 360 | 45,110 |
| bullet | 865 | 20,173 |
| blitz | not yet fitted | 0 analyzed |
| *(Lichess, for reference)* | *271.6* | *75k, 2300+ rapid* |

The 2.4x spread between rapid and bullet is the whole argument for fitting our
own. A steeper curve means advantages convert reliably; Lichess's k was
measured on 2300+ players, who convert far better than this corpus does.
Importing 271.6 would overstate every error here.

**Blitz is dark until blitz games are analyzed**, because fitting needs evals
and results together. A blitz population run supplies both at once, so the job
ends by fitting `k` for its time class **if absent**. It does not refit an
existing `k`: a refit would move every historical count without anybody asking
it to. `n`, `fitted_at` and `source` exist so a deliberate refit stays a
recorded act.

A time class with no fitted `k` returns nothing and says so. It does not
silently borrow another class's curve.

---

## Schema

Three views, two tables and one added column. Everything lives in the sidecar
alongside `move_evals` and `move_errors`.

**A view cannot cross the ATTACH boundary.** SQLite rejects it outright:

```
sqlite3.OperationalError: view t_sev cannot reference objects in database engine
```

Verified, not assumed. *Queries* may join across the boundary freely—that is
what `analysis_engine()` exists for—but a stored view may only reference
objects in its own database. This is presumably why `move_errors` joins nothing
outside the sidecar.

That rules out the obvious shape, which was to read `time_class` from
`games`. Instead **`game_coverage` gains a `time_class` column**, written at
analysis time. It is a fact about the analyzed game, recorded where the other
facts about the analyzed game already live, and it makes every view below pure
sidecar—readable from `sqlite3 chess_engine.db` with no attach, like the
existing ones.

### `move_severity`

Over `move_evals`, joined to `wp_curve` through `game_coverage.time_class`.

- Re-caps at ±1000. `move_evals` exposes uncapped `cp_eff` (mate maps to
  ±10000), and the capped column it computes internally is not surfaced. ±1000
  is also exactly Lichess's ceiling, which is a coincidence worth not fighting.
- Converts to the **mover's** point of view. `position_evals` is White-relative,
  so with `sgn = +1` for white and `-1` for black:
  `wp = 1 / (1 + exp(-sgn * cp_capped / k))`
- Emits `wp_before`, `wp_after`, `wp_loss = max(0, wp_before - wp_after)`, and
  `tier`.

The ladder lives here and nowhere else:

```sql
CASE WHEN wp_loss >= 0.20 THEN 'blunder'
     WHEN wp_loss >= 0.10 THEN 'mistake'
     WHEN wp_loss >= 0.05 THEN 'inaccuracy'
END
```

Lichess's 0.15 blunder line is the noted alternative. Changing it is one line
in one view.

Built as a throwaway against copies of both databases, the whole thing runs and
returns plausible numbers. Across all 101,519 scored moves currently on disk,
both sides of every analyzed game:

| tier | moves | share |
|---|---|---|
| blunder | 2,475 | 2.4% |
| mistake | 4,195 | 4.1% |
| inaccuracy | 7,170 | 7.1% |
| unflagged | 87,679 | 86.4% |

That is 0.97 blunders per player per game, against the 1.09 events/game the
threshold analysis predicted at 0.20 and is close enough to how players
actually talk about their own games to be worth keeping.

`exp()` is a **compile-time optional** SQLite builtin (`SQLITE_ENABLE_MATH_FUNCTIONS`,
3.35+). Verified present in both the CLI (3.51.0) and Python's bundled library
(3.51.3) on this machine. A startup assertion belongs in `init_engine_db` so a
math-less build fails loudly rather than deep inside a view.

### `move_quality`

`move_severity` self-joined on `ply - 1` to add the Miss flag:

```sql
prev.wp_loss >= 0.10 AND this.wp_loss >= 0.05
```

Opponent handed over at least a mistake; you gave at least an inaccuracy back.
Orthogonal to `tier`—a move can be a blunder and a miss, and both are recorded.
Plies 1 and 2 have no qualifying predecessor and are never misses.

This is the same event as "unpunished" in the tactics spec's 2x2, labelled from
the other side of the board.

### `game_move_quality`

`(run_id, game_id, color, moves_scored, inaccuracies, mistakes, blunders, misses)`.

### `population_jobs`

`(job_id, time_class, elo_lo, elo_hi, target_games, games_done, status, started_at, finished_at, error)`

Needed because the job runs for minutes and the UI has to show progress.

**No sampling table.** Band membership is derivable from `games.white_elo` /
`black_elo`, and what has been analyzed is already in `game_coverage`. The
pooled rate is a deterministic query over those two, so there is nothing that
can drift out of sync.

---

## The population

### Why pooled

The corpus has breadth and no depth.

| band | time class | players | with >=10 games | avg games |
|---|---|---|---|---|
| 1800 | rapid | 5,987 | 28 | 2.3 |
| 2000 | rapid | 910 | 9 | 2.0 |
| 1200 | bullet | 4,297 | 8 | 2.2 |
| 2200 | blitz | 820 | 27 | 2.3 |

It was snowballed off one player's opponents, so nearly everyone appears once
or twice. Per-player rates at n=2 are noise, which rules out percentiles. A
pooled rate—events per 1,000 scored moves across a band—needs breadth only, and
every band from 200 to 3200 clears `baselines.py`'s floors of 30 players and
150 games—the bands near your own rating by better than two orders of
magnitude, the thin tails by roughly four. Only the extremes fail: below 200
(40 players, 71 rows) and above 3400 (7 players).

Depth is recoverable later by syncing more games for players already known, via
the existing `etl/sync_player.py` path. Deliberately deferred: the engine cost
is small but the chess.com-facing stage has to handle 404s, closed accounts and
rating drift since the games were played.

### What was ruled out

**Chess.com's own accuracy scores.** `games.white_accuracy` and
`black_accuracy` exist as columns and are NULL on all 204,598 rows—chess.com
computes accuracy only when a user opens Game Review, so the API never returned
it.

**The opponent mirror alone.** The 1,211 distinct opponents inside the analyzed
games are free and rating-matched by construction, and remain a useful
secondary cut. But rapid opponents span 1798-2080 and contribute ~1 game each,
so it is a peer group at one rating, not a population.

### Sampling

`engine/population.py`, a band-keyed resolver beside `resolve_scope` rather
than a modification of it—`Scope` is username-keyed through
`_build_game_filters(player_id=...)` and cannot express a band.

- Select **player-games**: a side whose Elo falls in the band, in the requested
  time class, with >= 2 stored moves.
- Exclude every game the player being viewed is in, not just their seat. The
  opponent mirror already covers those games, and keeping them would make the
  population partly a sample of people playing the viewed player.
- Cap 5 games per player. The corpus averages ~2 anyway, so a tight cap costs
  almost nothing and buys breadth per engine-minute.
- Drop games already complete under this run via the existing `unanalyzed()`.

Pressing the button twice **accumulates**. It does not redraw.

The pooled rate counts only moves by the side actually in the band. Counting
both sides would make it a game-band rate, which is a different and less useful
number.

### Cost

Measured, not estimated. Run 1 took 431.7 minutes of wall clock for 1,265
games, but 401.1 of those minutes were idle gaps across 27 pauses (largest
104.3 minutes—the machine slept overnight). Excluding gaps over a minute,
throughput was **41.3 games/min**, consistent with the constant already
calibrated in `engine/cli.py` (48 games/min at depth 14 on 7 workers, measured
over a 303-game batch).

So a 600-game band is roughly **15 minutes**, and 3,000 games roughly 73.
`estimated_minutes()` already exists and should drive the estimate shown before
the job starts.

Any figure quoted from run 1's wall clock is wrong by ~14x. That mistake was
made once already during design.

---

## API

Following the existing `/baseline` convention so the band selector in the
filters bar works without special-casing.

| Route | Returns |
|---|---|
| `GET /api/players/{u}/analytics/move-quality` | per-game rows plus totals |
| `GET /api/players/{u}/analytics/move-quality/baseline` | pooled band rate |
| `GET /api/games/{game_id}/move-quality` | the drill list |
| `POST /api/players/{u}/analytics/move-quality/population` | queues a job for the Compare-to band, or returns the one in flight |
| `GET /api/population/jobs` | active jobs, then recent finished ones |

As built, the band comes from the filter bar's Compare-to selector rather
than always from the player's median, and presses queue: jobs run one at a
time, because one job already uses every core but one. The baseline route
carries the band's analyzed coverage and any job in flight, which is what a
separate coverage route would have returned.

---

## UI

A **Move Quality** section on the existing section pattern.

- Three counts, with the population rate behind each.
- Miss shown separately, labelled as overlapping the other three. The four
  numbers do not sum to a total and the UI must not imply they do.
- A per-game table; expanding a row gives the drill list: move number, SAN,
  tier, `wp_before` → `wp_after`, seconds spent, deep-linked to that game on
  chess.com.

`moves.move_san` and `moves.clock_seconds` are populated for all 1,271 analyzed
games, so the time-pressure column costs nothing and is likely the most
interesting one in the table.

The **Analyze population** button lives in the section header and appears when
the current band's coverage is thin. It shows the estimate before starting and
a progress bar after.

---

## Error handling

There is no background-task machinery in the app today; this is the first. A
thread, not a queue.

`analyze_games` already runs a `ProcessPoolExecutor` with a progress callback
wired to `stderr_progress`; the job runner substitutes a callback that writes
`games_done`.

| Condition | Behaviour |
|---|---|
| Stockfish missing | `EngineNotFound` already exists; surfaces as a job error, not a 500 |
| Band below viability floors | Reuse `_band_is_viable`; say "not enough data" rather than showing a rate built on four players |
| Time class has no fitted `k` | Distinct message from "no coverage"—this is blitz today |
| `exp()` unavailable | Startup assertion, not a runtime view failure |
| Job already running for a band | Return the existing `job_id` rather than starting a second |

---

## Testing

- Golden values for the curve at known centipawns, both k values.
- Fixture positions pinning each threshold boundary, including the ±1000 clamp
  and both mate directions.
- A two-ply Miss fixture: opponent gives up 0.12, reply gives back 0.07.
- A near-miss fixture that must *not* flag: opponent gives up 0.12, reply gives
  back 0.03.
- Sampler: per-player cap, viewed-player exclusion, idempotency on re-press,
  and that only in-band sides are counted.
- Startup assertion for `exp()`.

---

## Relationship to the tactics spec

This supersedes **Stage 1** of
[the tactics spec](2026-09-19-tactics-section-design.md), which proposed the
same severity layer. Two differences:

1. That staging assumed Stage 1 would force a board and drill-list UI into
   existence. It does not, here—the drill list deep-links out instead. Whether
   the board is still worth building is now a question for motifs, which
   genuinely need it.
2. This adds a population layer that spec did not have.

Its Stage 2 (motifs) and Stage 3 (MultiPV and coverage) are unaffected and
still stand. Stage 3's coverage expansion and this feature's population button
are the same machinery pointed at different questions, and whichever lands
first should be built so the other can reuse it.

---

## Out of scope

Board rendering. Motifs. MultiPV. Per-player percentiles and the deepening
scrape. Brilliant, Great, Best. Refitting `k` automatically.

---

## Which band the job runs at

`resolve_band` derives a band from `player_median_elo`, which inherits the
whole filters bar. Two properties of that matter here, and neither is
theoretical:

| window | rapid | blitz | bullet | pooled |
|---|---|---|---|---|
| lifetime | 1464 (1400) | 1264 (1200) | 1188 (1100) | **1297 (1200)** |
| 365d | 1741 (1700) | 1328 (1300) | 1271 (1200) | **1405 (1400)** |
| 180d | 1900 (1900) | 1543 (1500) | 1304 (1300) | **1840 (1800)** |
| 90d | 1903 (1900) | 1575 (1500) | 1455 (1400) | **1888 (1800)** |

**Time class is pooled unless the user picked one**, and pooling is dominated
by whatever they have played most recently. At 90 days the pooled median is
1888, which is rapid's band and 448 points above their actual bullet strength.
A bullet job launched from that band analyzes the wrong players.

So: **the median is derived within the time class being analyzed, never
pooled.** On "All", that is one job per class rather than one job at a blended
band. `resolve_band` already refuses to pool classes for the comparison itself
(`fallback_class = time_class or dominant_time_class(...)`, commented "refuse
an unconstrained pool"); this extends the same rule to the median that picks
the band.

**The date range moves the band by up to 500 points**—lifetime rapid is 1464,
last-90-days rapid is 1903. Tracking the filters bar is right for *display*,
consistent with every other baseline. But if it also drove the job, narrowing a
date window would move the band out from under existing coverage. So the band
is **pinned at press time**: `population_jobs.elo_lo`/`elo_hi` record what
actually ran, coverage is keyed on that, and the UI resolves a band from
current filters and looks up whether it has any.

`player_median_elo`'s pooling is pre-existing and the engine-free baselines
live with it. Nothing here changes their behaviour.

---

## Open questions for implementation

**Does the pooled rate need a minimum move count**, separate from
`baselines.py`'s game and player floors? A band with 150 games has ~12,000
scored moves, which is ample for blunders at ~1% but thinner for a stable
inaccuracy rate. Worth measuring once the first band exists rather than
guessing a floor now.
