# Tactics section

**Date:** 2026-09-19
**Status:** design, approved, not implemented
**Depends on:** [2026-09-16-style-vs-ability-findings.md](2026-09-16-style-vs-ability-findings.md)

---

## What this is

A panel answering three questions the engine data can answer and nothing in the
app currently asks:

1. **Which tactical patterns do you miss?** Forks, pins, discovered attacks—named
   patterns, not "you lost 300 centipawns."
2. **How deep can you see?** Find-rate as a function of how many moves away the
   payoff is.
3. **What did it cost?** Every count in this feature is gated on whether the
   error changed the expected result.

It closes the open thread in the findings doc. That thread named *missed forcing
moves* as the surviving weakness candidate because it carries its own
counterfactual: the engine states what should have been played in that exact
position, so colour, opening and position cancel by construction.

## What this is not

**Not a centipawn-loss report.** cp_loss is the wrong unit and every table in
this project has so far used it wrongly. See "Severity" below.

**Not a claim that every engine disagreement is a mistake.** At depth 14 the
engine prefers moves that are not better in any sense a human can use. The
severity gate exists to discard those, not to rank them.

**The motif names are ours, not the engine's.** Rule 5 from the findings doc:
the engine is authoritative about position value and nothing else. It has no
concept of a fork. Every category here is derived from the rules of chess via
`python-chess`, with the engine used only to establish which move was best.

---

## Decisions

| | decision | why |
|---|---|---|
| Unit of error | **Δ win probability**, not Δ centipawns | A 300cp drop is worth Δwp 0.083 from +900 and 0.204 from +200. Counting them equally is how the first draft of this feature reported 104 missed mates when 21 mattered |
| Win-probability curve | **fitted from this corpus, per time class** | Rapid fits k=360, bullet k=865. Lichess's published curve is k=272 |
| Severity tiers | inaccuracy 0.05, mistake 0.10, blunder 0.20 | Standard chess nomenclature, recalibrated to wp. Yields ~2.8 and ~1.1 events per game |
| Cosmetic errors | **collapsed, never deleted** | 466 of 570 mate-available positions left a forced mate on the board. Dropping them silently would hide that the denominator is mostly noise |
| Motif source | board geometry from `best_move_uci` | Rule 5 |
| Engine run | **MultiPV=3, PV stored to depth 6**, new `run_id` | Distinguishes "one move held" from "four did"—the same control, one level down |
| Coverage | expand during the re-run | Currently 1,271 games, 0.6% of corpus, zero blitz |
| Presentation | requires a board | Every readout terminates in "look at this position" and the app has no board |

---

## Severity: the control layer

This is the part that makes the rest honest, and it is the part the first draft
of this design got wrong.

An error's cost is the change in **expected result**, not in centipawns. Dropping
500cp from +2000 to +1500 costs nothing. Dropping 500cp from +200 to −300 costs
the game.

### The curve is fitted, not imported

`wp(cp) = 1 / (1 + exp(−cp / k))`, with k fitted by maximum likelihood against
observed game results on this corpus:

| time class | positions | fitted k |
|---|---|---|
| rapid | 45,110 | **360** |
| bullet | 20,173 | **865** |
| *(Lichess published curve)* | — | *272* |

Two things follow.

**A per-time-class curve is mandatory.** Bullet's k is 2.4× rapid's: a centipawn
is worth far less when the clock decides games. A single curve would overstate
bullet severity roughly threefold.

**Importing the standard curve would be wrong.** At k=272 Lichess's curve
describes a stronger population that converts advantages more reliably. Applied
here it inflates every error. The corpus can fit its own curve, so it should.

Eventually k should be fitted per (time class, rating band). Engine coverage is
one player, so v0 fits per time class only, and the stored row records the
population it was fitted on.

### Empirical calibration, rapid

| eval bucket | n | observed score |
|---|---|---|
| < −800 | 1,772 | 0.036 |
| −800…−400 | 6,408 | 0.171 |
| −400…−200 | 2,572 | 0.337 |
| −200…−75 | 2,838 | 0.400 |
| −75…+75 | 11,259 | 0.472 |
| +75…+200 | 4,041 | 0.519 |
| +200…+400 | 3,249 | 0.665 |
| +400…+800 | 5,918 | 0.834 |
| > +800 | 1,043 | 0.935 |

Flat at both ends, steep in the middle. Almost all severity lives between −400
and +400. Note the tails are not 0 and 1: from worse than −800 the subject still
scores 0.036, which is the measured value of playing on.

**Mate scores are not on this curve.** A forced mate maps to an empirically
observed conversion rate, not to 1.0—the subject converts mate-in-1 at 88% and
mate-in-4 at 70%. Assuming 1.0 would make every missed mate look maximally
severe, which is the error this whole section exists to prevent.

### What the gate does

Mate-available positions, by what the played move landed in:

| landed in | n |
|---|---|
| still a forced mate (slower route) | **466** |
| still winning big | 51 |
| still winning | 32 |
| edge only | 3 |
| threw away the win | 5 |
| **now losing** | **13** |

82% cosmetic. The gate cuts the headline by a factor of five, and the 13 are
worth more than the other 549 combined.

**All 13 are bullet.** Across 390 rapid mate-available positions the subject
never once went from a forced mate to a losing position. Rule 3 of the findings
doc—a failure appearing in bullet alone is a clock artefact—applies directly,
and the UI must not present these as a tactical gap without the time-class split
visible.

---

## Motifs

Classified from the board before the move plus the engine's chosen move. All
geometry, no engine semantics.

| motif | definition |
|---|---|
| fork | moved piece attacks ≥2 enemy men that are undefended or of greater value |
| pin | move creates a slider line through an enemy piece to a more valuable one behind |
| skewer | as pin, with the more valuable piece in front |
| discovered attack | moved piece vacates a line, and a *different* friendly piece gains an attack |
| deflection / overload | move attacks an enemy piece that is currently defending something else |
| trapped piece | after the move an enemy piece has no safe square |
| back rank | mate or decisive material turning on an unescorted king on its first rank |
| zwischenzug | a check or capture inserted before an expected recapture |
| promotion tactic | move creates or exploits an unstoppable passer |
| mate in N | `mate_in` is set; N is the distance |
| *(none)* | no pattern matched |

Motifs are **not mutually exclusive**—a move can fork and discover at once.
Stored one row per (position, motif).

Reported as the bishop finding was: **availability against find-rate**, so a
motif is not called a weakness merely because it occurs often. That control is
what turned "you miss queen tactics" into "bishops are the weak piece."

---

## The 2×2

All four cells derive from consecutive evals. Nothing new is searched.

| | you did not take it | you took it |
|---|---|---|
| **your opportunity** | missed | found |
| **their opportunity** | allowed → *punished* / *unpunished* | — |

- **missed** — the engine's move gains ≥ threshold wp and another move was played.
- **allowed** — the played move hands the *opponent* a ≥ threshold wp opportunity
  on their reply. Currently invisible: `move_errors.error_kind` only sees the
  first row of this table, and for an improving player the second is usually the
  bigger bucket.
- **punished / unpunished** — whether the opponent's reply actually collected it.

Unpunished errors are the most valuable drill material in the dataset, because
they left no trace in the result and the player has no memory of them. They are
also the honest measure of how much of a rating is survivorship.

The symmetry control is built in: the same computation runs for the opponent in
the same games. At Δwp ≥ 0.10 the subject errs on 9.2% of rapid moves and his
opponents on 8.4%, which is the sanity check that the measure is not wildly
miscalibrated.

---

## Schema

New tables in the sidecar. Existing tables are untouched.

```
position_pv                        MultiPV output, one row per candidate move
  run_id      INTEGER  ┐
  game_id     INTEGER  ├ primary key
  ply         INTEGER  │
  rank        INTEGER  ┘  1 = best, 2 = second best, 3 = third
  move_uci    TEXT NOT NULL
  cp          INTEGER        ┐ same convention as position_evals:
  mate_in     INTEGER        ┘ White's point of view, mate leaves cp NULL
  line        TEXT           space-separated UCI, the PV from depth 2..6

wp_curve                           fitted, with provenance
  time_class  TEXT  primary key
  k           REAL NOT NULL
  n           INTEGER NOT NULL     positions fitted on
  fitted_at   DATETIME NOT NULL
  source      TEXT NOT NULL        which population, e.g. 'ballasack6'

move_motifs                        geometry, derived from best_move_uci
  game_id     INTEGER  ┐
  ply         INTEGER  ├ primary key
  motif       TEXT     ┘
  depth       INTEGER            plies to payoff, from the PV; NULL if unknown
```

Two derived **views**, per Rule 4—store facts, derive interpretation:

- **`move_severity`** — joins `move_evals` to `wp_curve`, yielding `wp_before`,
  `wp_after`, `wp_loss` and a `tier` of inaccuracy / mistake / blunder. The
  thresholds live here, so revising them costs a view definition.
- **`tactical_events`** — `move_severity` joined to `move_motifs` and
  `position_pv`, adding `outcome` (missed / found / allowed) and, for allowed,
  `punished`. This is what the API reads.

**Why the re-run gets a new `run_id`.** Stockfish prunes differently under
MultiPV, so run 2's evaluation of a position is not identical to run 1's. The
composite primary key already handles this; nothing needs to change, and run 1's
rows stay valid. Do not mix runs within a single readout.

---

## Engine change

At `engine/analyze.py:156`, `_evaluate_position` calls
`proc.analyse(board, Limit(depth=depth))` and does this:

```python
pv = info.get("pv") or []
best = pv[0].uci() if pv else None
```

The full line is already computed and discarded. The change is to pass
`multipv=3`, keep `pv[:6]` for each candidate, and return the candidate list
alongside the existing three values. The existing `position_evals` write stays
exactly as it is, fed from rank 1, so nothing downstream breaks.

**Cost is unmeasured and must be measured before the full run.** MultiPV=3
widens the search and disables some pruning; the slowdown is plausibly
1.5–2.5× but that is a guess, not a figure. Time a 20-game sample first and
decide the coverage target from the measured rate.

Terminal-position handling is unchanged—checkmate and stalemate still return
early without searching.

---

## Coverage

Current state, and the binding constraint on everything above:

| time class | analysed | subject's games | coverage |
|---|---|---|---|
| rapid | 865 | 4,797 | 18% |
| bullet | 406 | 2,166 | 19% |
| blitz | **0** | 2,792 | **0%** |

Blitz has no analysis at all, and bullet is where every consequential missed
mate turned up. The re-run is the only sensible moment to widen this, since
those positions are being re-evaluated regardless. Target for v0: full coverage
of the subject's bullet and blitz games, so that Rule 3 (a weakness must appear
in more than one time class) can actually be applied.

---

## API

One route, following the existing analytics convention and the global filters:

```
GET /api/players/{username}/analytics/tactics
    ?time_class=&start=&end=&eco=&tier=mistake
```

Response:

```
motifs:   [{motif, available, found, missed, find_rate, ci_low, ci_high}]
depth:    [{depth, available, found, find_rate}]
outcomes: {missed, found, allowed, punished, unpunished}
events:   [{game_id, ply, motif, depth, tier, wp_loss,
             engine_move, played_san, seconds, clock_left,
             fen, chess_com_url}]
meta:     {run_id, games_analysed, games_total, k, tier_thresholds}
```

`find_rate` carries an interval because most motif cells are small. A bare point
estimate on n=11 reads as a fact; this is the same decision the style panel made
with error bars.

`meta.games_analysed` vs `games_total` must surface in the UI. A tactics panel
computed on 18% of someone's games and presented without that ratio is a lie of
omission.

---

## UI

A new collapsible `analytics-section` matching the existing four, placed after
Pro Comparison.

1. **Motif table** — available / found / missed / find-rate with intervals, sorted
   by missed count. A toggle for "include cosmetic" that reveals gated-out rows
   rather than changing the numbers in place.
2. **Depth curve** — find-rate against plies-to-payoff. The mate-distance version
   already has shape (88% at 1, 70% at 4) and this generalises it.
3. **Drill list** — the worst events by `wp_loss`, each with the position, what
   was played, what was available, time spent, clock remaining, and a link to
   the game.

**A board is a prerequisite.** There is no chessboard anywhere in the app and no
FEN rendering. Every element above terminates in showing a position, and without
one the drill list is a table of algebraic notation nobody can read. Rendering
is static—a position and an arrow for the engine's move, no move navigation in
v0.

Copy constraints:

- Never present a bullet-only finding without the time-class split visible.
- Never label a motif "weak" on availability alone; the find-rate is the claim.
- State the coverage ratio in the section meta line, as the style panel states
  "measured at move 10".

---

## Testing

**The motif classifier is the part most likely to be silently wrong**, and it
has no engine to check it against. It needs a fixture suite of hand-verified
FENs: a position containing a knight fork, one containing a pin that is not a
skewer, one where a discovered attack and a fork coexist, one where a
near-miss must classify as *none*. False positives are worse than misses—a
panel confidently naming a fork that is not there destroys trust in the whole
section.

**The wp curve needs a refit test.** Fitting on a held-out half of the games
should recover k within a stated tolerance. If it does not, the curve is
overfitted and the severity gate is arbitrary.

**The 2×2 needs sign tests.** The commonest bug class here is perspective:
`position_evals` is always White's point of view, and three separate conversions
to the mover's perspective happen in this feature. Each needs a test with a
known Black-to-move position.

**Known-answer regression.** The 13 catastrophic bullet mates are a fixed set.
They should still be 13 after implementation, and any change to the thresholds
that moves that number should do so visibly.

---

## Staging

This is too large for a single implementation plan. Three stages, each shippable
and each answering something on its own:

**Stage 1 — severity, on existing data.** `wp_curve`, the `move_severity` view,
and the fit script. No engine re-run, no new search. Ends with the mate-depth
curve and the 13 bullet disasters visible in the API, which is the thin vertical
slice: it forces the board and the drill-list UI into existence, and everything
later reuses both.

**Stage 2 — motifs.** The classifier, its fixture suite, `move_motifs`, and the
motif table. Still no re-run; runs off `best_move_uci`, which already exists on
102,391 positions. `depth` stays NULL until stage 3.

**Stage 3 — MultiPV and coverage.** The `analyze.py` change, `position_pv`, the
timing measurement, the widened run, then the depth curve and only-move
detection light up against real data.

Stage 1 is a prerequisite for both others. Stages 2 and 3 are independent.

---

## Out of scope for v0

- Fitting the wp curve per rating band (needs engine coverage beyond one player)
- Cross-player tactical comparison (same reason, plus the corpus skew documented
  in the findings doc)
- Move navigation or a full game replayer
- Opening-specific tactical profiles
- Any prescriptive copy. This feature reports what was missed; it does not
  recommend training regimes

---

## Open question for implementation

**Where the tier thresholds sit.** 0.05 / 0.10 / 0.20 is proposed because it
yields ~2.8 mistakes and ~1.1 blunders per game, which matches how players
already talk about their games. But this single number decides what the entire
feature shows, and it is a judgement call rather than a measured quantity. It
lives in the `move_severity` view precisely so it can be revised cheaply once
the panel exists and the events can be eyeballed.
