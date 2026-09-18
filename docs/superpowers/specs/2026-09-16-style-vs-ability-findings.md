# Style is not ability: what the positional metrics can and cannot tell us

**Date:** 2026-09-16
**Status:** findings, no feature built
**Subject:** ballasack6 (Jonathan), 400 bullet + 865 rapid games analysed at
Stockfish 19 depth 14; 203,748-game corpus for cross-player comparison

---

## Why this exists

The original goal was to find where *strategic understanding* is weak, on the
theory that bullet failure modes are simpler than rapid ones. This document
records what was measured on the way to answering that, because most of it came
out negative and the negatives are expensive to rediscover.

Two things to take from it:

1. A **weakness feature** is still unbuilt, and the one candidate that survived
   every control is described in "Open thread" below.
2. A **style feature** is newly well-founded, for reasons that have nothing to
   do with the original goal.

---

## The central result

Classical-Stockfish-style positional metrics, measured at ply 20 and centred
within (opening, colour), are **stable traits of a player that are nearly
orthogonal to that player's strength.**

| metric | split-half | corrected | correlation with Elo | spread over 25 splits |
|---|---|---|---|---|
| king safety | +0.586 | **+0.74** | +0.07 | 0.19 |
| mobility | +0.551 | **+0.71** | +0.08 | 0.12 |
| space | +0.545 | **+0.71** | +0.07 | 0.16 |
| pawn structure | +0.368 | **+0.54** | +0.02 | 0.27 |
| passed pawns | +0.013 | +0.03 | −0.01 | 0.19 |

253 players with ≥30 games, 196,048 player-observations pooled across bullet and
rapid. Split-half = a player's randomly-chosen half predicting their other half;
corrected = Spearman-Brown.

**Average over many splits, never one.** A single shuffle put passed pawns at
−0.05 and another at +0.17. These figures are the mean of 25 splits, and the
spread column is why: pawn structure's reliability is the least certain of the
four, which matters because pawn structure is where ballasack6's one measured
deficit sits.

All four surviving metrics **jointly predict Elo at R² = 0.0096.** The fitted
model places every player between 1361 and 1751 when actual ratings in the
corpus span 184–3368 — it cannot distinguish a beginner from a grandmaster.

Reliability ≈ 0.7 with an Elo correlation ≈ 0.06 is the signature of a **trait**
measure, not an **ability** measure. This is the finding: not that the metrics
failed, but that they succeed at measuring something that is not skill.

Passed pawns is neither — reliability +0.03 means it is noise at ply 20. It is
worth keeping only so that the negative result reproduces.

### Consequence

No amount of adding further positional metrics will produce a weakness
diagnosis. They measure a different axis than strength. The hypothesis that
several such metrics would *average* to a player's rating (2400 + 1600 + 1700 +
1900 ÷ 4 = 1900) is falsified by the R²: they do not average to a rating because
they do not predict one.

---

## ballasack6's profile

Measured against players in the same band, same opening, same colour, with his
own games excluded from every reference mean. 3,866 rapid games.

| axis | vs own band (1600–1999) | vs 2400–2899 | effect size *d* |
|---|---|---|---|
| space | +0.243 (t=7.6) | −0.012 | 0.13 |
| mobility | +0.571 (t=8.2) | −0.262 | 0.13 |
| pawn structure | −0.079 (t=−6.0) | −0.148 (t=−6.8) | 0.10 |
| king safety | −0.068 (t=−1.7) | −0.458 (t=−7.0) | 0.19 |

**Reading:** plays for space and piece activity, pays for it in structure and
king safety. Stable across 3,866 games. This is a description of *how* he plays,
not a claim that any of it is costing him rating points.

**Do not use rating bands as the reference class for these metrics.** Band means
span ~0.2 units against a between-game SD of ~1.6, so conditioning on band moves
the number by roughly 20% of the effect while falsely implying the comparison is
about strength. For a trait measure the correct reference is the whole player
population — a percentile.

---

## Findings that dissolved, and what killed each

Every one of these looked convincing before its control was applied. Two had
t-statistics above 4.

| claim | killed by | what was actually true |
|---|---|---|
| "You miss queen tactics" | base rate | Bishops are the weak piece (35.3% of available forcing bishop moves passed up). Queens merely have the most tactics available (933). |
| "You fail to punish opponent blunders" | symmetry | Both sides degrade after any rupture — opponents 4.69×, him 5.39×. Residual ≈ 15%, not the 4× the raw numbers suggested. |
| "You take more space than opponents" (t=4.7 bullet, t=7.2 rapid, replicated across both time controls) | colour | As White +2.2 to +2.3, as Black −0.4. It was measuring *White moves first*. A further 32.7% of residual variance is ECO code. |
| "Your deficit is concentrated in sharp positions" | time control | +10.9 cp concentration in bullet (t=2.12) but +1.3 in rapid (t=0.63). Largely a time-pressure artefact. |

### The statistical trap behind several of these

With n in the thousands, **t measures precision, not size**. Quadrupling the
games doubles t with the effect unchanged. Use `d = t/√n`. Every effect in this
document is ≈ 0.1–0.2 SD: reliable and negligible. Two players at the same
rating differ from each other far more than any of these differ from a band
average.

---

## Rules derived

**1. Prefer features that carry their own counterfactual.**
The engine states what should have been played *in that exact position*, so
colour, opening and position cancel by construction and no controls are needed.
Missed forcing moves is such a feature and has survived everything. A purely
descriptive feature needs a control for every confound, and there is no way to
know in advance how many there are. Weigh that ratio before anything earns a
column.

**2. Screen before building.** Centre a candidate metric within (ECO, colour),
then ask two things in order: does it vary across rating bands at all, and does
one half of a player's games predict the other half? A metric that passes the
first measures ability; one that fails the first but passes the second measures
style; one that fails both is noise. Engine-free, so the whole screen runs in
~12 s over 75,669 bullet games. Space in bullet came back at r = +0.003 across
1700 Elo and 18,000 players — a metric that cannot support a finding about
anyone, learned in 12 seconds instead of a schema change and a week.

**3. Any candidate weakness must appear in both bullet and rapid.** One that
appears in bullet alone is a time-pressure artefact. The rapid corpus exists for
this purpose.

**4. Store facts, derive interpretation in views.** Established in PR #5 and
still holding. Store `space_delta` as a number, never `is_space_gaining_move` as
a boolean.

**5. The engine is authoritative about position value and nothing else.** It has
no notion of "quiet", "space" or "prophylaxis". Modern Stockfish (19, NNUE)
exposes only Material/PSQT and Positional/Layers through `eval`; the named
classical terms were deleted around Stockfish 16. Everything categorical in this
project is ours, derived from the rules of chess via `python-chess`.

---

## Infrastructure that now exists

- **`chess_engine.db`** — sidecar, `ATTACH`ed to the canonical database.
  Run 1 = Stockfish 19 depth 14: 400 bullet + 865 rapid games of ballasack6,
  ~100k positions, 0 failures.
- **`move_evals`** / **`move_errors`** views — centipawn loss derived, never
  stored; thresholds live in the view so revising them costs a view definition.
- **`played_move_features`** / **`best_move_features`** — piece, capture, check,
  castling, promotion for both the played move and the engine's choice.
- **Rapid generalisation set** — 865 games, opponents rating-matched to 7 Elo
  (1908 v 1915). Bullet opponents are matched to 13 Elo (1347 v 1360). *His own
  opponents are a better control group than any elite corpus: same positions,
  same time control, no sampling bias.*
- **Corpus for cross-player work** — 203,748 games with moves. Bullet covers the
  elite end (47,992 at 2900+, but 71% of elite games come from 5 players); rapid
  covers the middle (11,322 at 1600–1999).

---

## Open thread: the weakness feature

Still unbuilt, and still the strongest candidate, because it carries its own
counterfactual:

**Missed forcing moves.** Already computed and already in `move_errors`. Never
surfaced in the UI or any API route. Base-rate control produced the one finding
in this project that survived scrutiny (bishops, not queens).

**The unexploited resource:** `position_evals.best_move_uci` holds the engine's
preferred move at ~100k positions, and `best_move_features` reduces each to five
booleans. The *squares* are unused. Comparing where the engine wanted pieces to
go against where they actually went is still a within-position counterfactual —
confound-free by construction — rather than another descriptive metric needing
three controls.

**What was tested and should not be retried:** whether loose pawn structure at
ply 20 predicts later `cp_loss` was proposed but never run. That is the one
remaining cheap test of whether the style axes cost anything, and it is the
natural bridge back from the style work to the weakness work.

---

## Sampling later in the game does not help (measured 2026-09-18)

The obvious objection to measuring at ply 20 is that it throws away the rest of
the game. Tested on ballasack6's rapid games, measuring at plies 20/40/60/80:

| axis | ply 20 | ply 40 | ply 60 | ply 80 |
|---|---|---|---|---|
| mobility | +0.06 | +0.13 | +0.19 | **+0.26** |
| king safety | +0.03 | +0.12 | +0.20 | **+0.26** |
| space | +0.03 | +0.00 | +0.01 | +0.02 |
| pawn structure | +0.02 | +0.04 | +0.03 | −0.02 |

(correlation with the game result)

Mobility and king safety appear to get much better. They do not. Holding the
engine's evaluation at the same ply constant kills the effect entirely — at ply
40, mobility goes from +0.117 to **+0.007** and king safety from +0.063 to
**−0.003**. By move 20 "more mobility, safer king" is a restatement of "you are
winning": the metric reflects the outcome rather than predicting it.

The evaluation's own correlation with the result runs +0.23 at ply 20, +0.48 at
ply 40, +0.61 at ply 60. It progressively swamps everything.

**So ply 20 is not a limitation, it is the only point where these metrics carry
information the engine does not already have** — the only point where the game
is not yet decided enough for the evaluation to dominate. Sampling later adds
data and subtracts signal. Do not build multi-snapshot sampling; an earlier
version of the plan recommended it as "cheap and deferred", which was wrong.

**Related: the axes barely predict his own results at all.** Across 4,626 rapid
games, the gap in win rate between his lowest and highest quartile is +3.6 points
for space, +6.1 for mobility, +3.1 for king safety, +1.0 for pawn structure.
Centipawn loss, on the 865 analysed games, runs from 25.7% to 80.1% — r = −0.41.
How accurately he plays decides games; what his position looks like at move 10
does not.

---

## Known corpus limitation: the similarity pool

The style feature's "who do you play like" readout compares against players with
enough games for a stable vector. That pool is badly skewed:

| band | players with ≥30 games | games |
|---|---|---|
| <1200 | 4 | 5,651 |
| 1200–1599 | 4 | 31,997 |
| 1600–1999 | 24 | 8,099 |
| 2000–2399 | 13 | 4,017 |
| 2400–2899 | 344 | 42,121 |
| 2900+ | 306 | 242,301 |

695 players clear the bar and **650 of them (94%) are 2400+**. Only 24 sit in
ballasack6's own band. Within the elite corpus the concentration is worse still:
71% of the 94,104 elite bullet games come from 5 players, 41,237 from Naroditsky
alone.

**Decision (2026-09-16):** ship similarity against **2800+ only**, framed
explicitly as "which strong player you most resemble" rather than as a peer
comparison. The thin sample is accepted for now.

2800 rather than 2400 because on chess.com the players actually recognisable as
super-GMs sit near 3000, and 2400 would pad the pool with players the comparison
is not meant to be about. The cutoff costs almost nothing: 472 players and
265,584 games at 2800+, against 650 and 284,422 at 2400+ — 7% fewer games for a
pool that means what it says. 3000+ would leave 124 players and 219,428 games,
still viable if the cutoff is ever tightened.

**Follow-up required:** a crawl feature to widen the corpus — more titled players
with deep histories, and ideally real coverage of the 1600–2399 range, which is
the thinnest part of the database and the only range that would make a peer
comparison possible. Until that exists, this feature cannot answer "do I play
like other players at my level", and should not be worded as though it can.

---

## Reproduction

Engine evaluation for a scope:

```bash
uv run python -m engine.cli --player ballasack6 --time-class rapid --since 2026-04-17
```

The metric implementations and the screen described above are not committed.
Reconstructing them: replay each game to ply 20, compute the four metrics for
both colours from the board alone, centre within (time class, ECO, colour) using
cells with ≥40 observations spanning ≥3 rating bands, then correlate against
rating band for the gradient and split each player's games in half for the
reliability. **Average the half-split over at least 25 shuffles** — one split put
passed pawns at −0.05 and another at +0.17.
