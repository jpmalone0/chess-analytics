# Scorecard: eight dimensions, scored against rating

**Date:** 2026-09-30
**Revised 2026-09-30 (Jonathan):** rows compare against the rating band, not the
opponents, and the radar shows Elos directly instead of a 0–100 score. See
"Revision" at the end.

**Status:** approved (self-approved: Jonathan was away and delegated approval.
The decisions below were all made with him on 2026-09-29/30; the calibration
method and the concrete cutoffs are mine and are the parts to review first)

## Goal

A sound version of the Aimchess radar. Every dimension is measured the same way
for the player and for their opponents in the same games, so the comparison is
rating-matched by construction, and every difference carries a 95% range and a
real/noise verdict.

## What is shown

1. **Radar.** One spoke per dimension, 0–100, higher is better. Two polygons:
   the player, and the average opponent. Neither is round. A score is a
   *rating equivalent* put on a fixed scale:
   `score = (R + 700) / 40`, so 500 → 30, 1900 → 65, 2500 → 80, clamped 0–100.
   `R` is the rating at which a player typically produces this metric (see
   Calibration). A spoke without a trustworthy calibration is left out of the
   radar rather than guessed.
2. **Rows.** One row per dimension: player value, opponents' value, the
   difference with its 95% range, and a verdict ("real" when the range excludes
   zero, otherwise "noise"), each in the row's own units.
3. **Sample flag.** A subtle "!" beside the header when the window holds fewer
   than 300 analyzed games.
4. **Footnote (the known wrinkle).** Expected-score rows do not account for
   resignations, abandonments, agreed or claimed draws, or the ±1000cp cap, so
   the phase and time rows do not add up to the result.

The window is whatever the filter bar selects (time class, dates, colour,
opening), exactly as the Move Quality section does.

## The dimensions

Expected score is the fitted curve `wp = 1/(1+exp(−cp/k))` on the eval clamped
to ±1000, from the mover's point of view, with `k` from `wp_curve` for the time
class. A move's *signed change* is `wp_after − wp_before` for the mover (not
floored). Its *loss* is `max(0, −change)`.

| Dimension | Unit | Per side |
|---|---|---|
| Opening | points/game | sum of signed changes on moves made from opening positions |
| Middlegame | points/game | same, middlegame positions |
| Endgame | points/game | same, endgame positions, to the end of the game |
| Time management | points lost/game | when the side's flag falls: the side's expected score at the flag; for timeout vs insufficient material, the part above ½. Otherwise 0 |
| Advantage capitalization | % | games won, of games where the side reached ≥75% after the opening |
| Resourcefulness | % | games won or drawn, of games where the side fell to ≤25% after the opening |
| Tactics found | % | chances converted, where a chance is a position after the opening whose best move is a capture or check, is not a plain recapture, and beats the second-best move by ≥10%; found = the played move lost ≤5% |
| Blunders | per game | moves losing ≥20% from positions that were not tactical chances |

Phases follow Lichess's Divider: the middlegame starts at the first position
with ≤10 majors and minors, or a sparse back rank (fewer than 4 pieces on either
side's home rank), or mixedness above 150. The endgame starts at the first
position with ≤6 majors and minors and runs to the end. A move belongs to the
phase of the position it was played from.

## Intervals

Game-level bootstrap, 1,000 resamples, fixed seed, so the same window always
shows the same range. Resampling games (not moves) keeps each game's moves
together, and one procedure serves both per-game rows and rate rows.

## Calibration (the rating scale)

The pool is every analyzed side in the same time class, excluding the viewed
player's own sides. Per dimension, an ordinary least-squares line of the metric
on the side's own rating, fitted over the natural unit of that row: a side-game
for the per-game rows, a chance for the rate rows. The rating equivalent of a
value `m` is `(m − a) / b`, clamped to 0–3000.

A line is used only when it is trustworthy: at least 30 observations, at least
3 distinct 200-point bands, the slope in the expected direction (phase rows,
rates: up with rating; time lost and blunders: down), and |t| ≥ 2. Otherwise
that spoke has no score.

**Lines are fitted per move for the phase rows and blunders**, although the
rows show per game. Across ratings, game length is not neutral: low-rated games
end early, rarely reach an endgame and have fewer moves to blunder on, so
per-game totals made them look better (endgame points per game fell with
rating, t = −3.8, on the first sample). Within one game both seats share its
length, so the rows can stay per game. The weights (moves or chances) set each
side-game's precision, but the sample size for the thresholds and the t-test is
the number of side-games.

First result on 100 games plus the calibration sample: Opening, Middlegame,
Tactics found and Blunders are scored. Endgame, Time management, Advantage
capitalization and Resourcefulness do not yet clear the bar.

This is why the engine now also analyzes a small calibration sample: 10 rapid
games per 200-point band from 600 to 2399, excluding the player. The sample is
small, so the scale is provisional and will tighten as more games are analyzed.

## Open decision found while building

Against the same-game opponents, **Advantage capitalization and
Resourcefulness always show the same difference.** You reaching 75% is the
same event as your opponent falling to 25% (the curve is symmetric), and you
failing to win is the same as them winning or drawing. So your conversion
equals 1 − their resourcefulness, and vice versa, and the two differences are
algebraically identical. The radar scores still differ, because they are
anchored to other players' games, not to the mirror. Options: keep both and
note it; merge them into one "swing" row; or compare these two rows against
the calibration line at the opponents' rating instead of against the mirror.

## Out of scope

- Exact terminal values and a "game endings" line (the wrinkle's deferred fix).
- Time allocation (critical vs trivial moves).
- Tuning the 10%/5% tactic cutoffs: they are named constants, to revisit once
  enough three-line positions exist.
- Per-row sample thresholds.

## Revision: the band, not the opponents; Elos, not scores

Jonathan's call, for the reason the mirror symmetry exposed: against the
same-game opponents, some dimensions are mirror images of each other.

- **Every row compares you with the band line at your average rating**, in the
  unit that line is fitted on (per move for the phase rows and blunders, shown
  per 100 moves). Range: a game-level bootstrap of your value, combined in
  quadrature with the line's standard error at your rating.
- **The band pool drops every game you played**, both seats. Your opponents'
  sides are your games seen from the other chair.
- **A row needs only a line's level** (≥30 side-games, ≥3 bands), so a flat
  line still gives a band value. **An Elo additionally needs** the slope in
  the expected direction with |t| ≥ 2.
- **The radar is in Elo** (0–3000), with your rating as a dashed ring, which is
  the band's position on every spoke by definition.

First result on the same data (100 of your games; the band from 84 games by
other players): Opening real, better than the band (Elo 2132); Time management
real, better; everything else noise at this sample. Elos: Opening 2132,
Middlegame 1494, Blunders 1864.

## Revision: Elo ranges by Fieller's method, everything closed-form

Jonathan wanted every number deterministic, and every Elo shown with how far it
can be trusted rather than a pass/fail mark.

- **Each Elo carries a 95% range from Fieller's method**: the Elo is the gap
  from the line's centre over its slope, and Fieller's quadratic gives the
  interval for that ratio from your value's variance and the line's level and
  slope. When the slope could be zero, the interval has no ends and the range is
  the whole scale ("any"). That replaces the |t| ≥ 2 gate and the "?" marks.
- **The table's ranges are closed-form too**: your value's variance is the
  ratio-estimator variance over games, combined with the line's standard error
  at your rating. The seeded bootstrap is gone.
- Limit: the band line treats both sides of one game as independent.

On the same data: Opening 2132 (1913–2456), Middlegame 1494 (744–2044),
Blunders 1864 (1551–2262); Endgame, Time, Advantage cap., Resourcefulness and
Tactics found span the whole scale.
