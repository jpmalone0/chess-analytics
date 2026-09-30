# Playstyle diagnosis: what is holding ballasack6 back in rapid

**Date:** 2026-09-29
**Status:** diagnosis, no feature built
**Subject:** ballasack6 (Jonathan), 927 rapid 10+0 games from 2026-04-17 to
2026-09-28, analysed at Stockfish 19 depth 14. Control: the opponents in the same
games, rating-matched by construction. Peers: 2,631 10+0 games between players
rated 1825–1975.

---

## The diagnosis

> You're a strong practical player: good tactics, good openings, excellent with
> the clock, and you never give up. What holds you back is impatience when the
> position goes quiet. With nothing forcing on the board, you either force
> something anyway (a capture, a check, a sacrifice) or sink into a long think
> hunting for a tactic that isn't there. The position was usually asking for a
> simple improving move. Your endgame technique is also loose. Your clock skills
> win back what these cost you, so you break even against players at your rating
> instead of pulling ahead of them.

### What's fine

- **Tactical vision.** When you look, you see as much as your opponents do. In
  the middlegame your quick moves are more accurate than theirs.
- **Openings.** You come out of them better than your opponents.
- **Clock and fighting spirit.** You flag people, win scrambles and save lost
  games.

### What's holding you back

1. **Impatience in quiet middlegames.** The bishop sacrifices, pawn grabs and
   pawn-doubling trades are all this one habit, and so are the long thinks that
   end in a forcing move.
2. **Endgame technique.** Your quick moves in endings are less reliable than your
   opponents', even with time on the clock.
3. **Inconsistent checking.** On routine moves you walk past captures and checks
   more often than your opponents do.

### What to work on

1. **Quiet positions:** ask what the position needs before looking for tactics:
   your worst piece, their plan, a pawn break. Then play a good quiet move rather
   than forcing one.
2. **Endgames:** basic king-and-pawn and rook endings, until your instincts in
   endings can be trusted.
3. **Routine moves:** a quick scan of checks and captures for both sides before
   any move that isn't obvious.

---

## Evidence

Every ratio compares your rate with your opponents' in the same games, and where
noted is standardised on position (your expected score before the move).
"Errors" are mistakes and blunders: moves that lost at least 10% of expected
score on the tool's fitted rapid curve (k = 360). Rating-point figures convert
expected score at an even matchup, 0.00144 points per Elo.

| Claim | Measurement |
|---|---|
| Tactical vision is fine | On non-recapture tactical chances you find the engine's exact capture or check 57% of the time, vs 56%. Choosing *which* forcing move: 0.97x errors. Middlegame moves played in under 5s with over 2 minutes left: 0.79x errors |
| Openings are a strength | +0.029 expected points per game over moves 1–8 (about +20 Elo) |
| Clock and fighting spirit | Opponents' flags: +0.035 per game (65 wins on time vs 4 losses; 36 of the 65 came from positions you were not winning). Scrambles under 1 minute: +0.012 per game. You save 20% of positions that fell below 20% expected score, vs 18% |
| Impatience in quiet middlegames | From move 9 with over a minute left you give up 0.090 more points per game than your opponents (about −60 Elo). Middlegame thinks of 20s+: 1.25x errors; 81% of your excess blunders fall on 20s+ thinks. Forcing when a quiet move was better: 1.27x per chance, 1.75x on long thinks. Bishop sacrifices next to the enemy king fail 35% vs 16%. Bishop-for-knight trades that double their pawns: 13 errors vs 1 |
| Endgame technique | Endgame moves with over a minute left: 1.21x errors, concentrated in moves under 5s (1.30x). Not the clock: at the same time left you play fast no more often than your opponents. Your mix of clock, position and pace accounts for none of the excess (−5 errors); erring more in the same situations accounts for +135. Endgames finished on the board lose 0.036 per game against the engine's expectation at entry; endgames decided on time win back 0.38 each |
| Inconsistent checking | On 5–20s moves, a costly walk-past of a forcing move: 1.33x |
| Net | Score 0.494 against an Elo expectation of 0.490 |

Where your expected score goes, per game: opening +0.029, moves 9–30 −0.055,
moves 31+ −0.035, scrambles +0.012, opponents' flags +0.035, your flag −0.001,
other endings +0.009. Total −0.006, which is a 0.494 score from a 0.500 start.

---

## What did not hold up

| Claim | What was actually true |
|---|---|
| Studying pawn structures is the key lever | The middlegame deficit is flat across opening familiarity (corr +0.02) and across structures. The "Open Sicilian" splits into several structures by move 12 |
| Endgame errors are bullet reflex, or forced by a leaked clock | At the same time left you play fast no more often than your opponents, and being behind does not speed you up |
| Aimchess: worse at converting advantages | 75.1% vs your opponents' 75.3% |
| Aimchess: quite strong in the endgame | The 52% win rate is propped up by flag wins |
| Aimchess: poor time management | You are behind on the clock on 64% of moves, but lose on time in 0.4% of games and win on time in 7.0% |
| Mornings, brain fog, tilt | None show up over 4,837 games. A bad start does not predict the rest of the day (r = −0.14) |
| "My score against the Pirc is always bad" | 50.6% in 79 games, at rating expectation |

---

## Limits

- The engine judges the positions. Labels such as "impatience" are inferred from
  move type, think time and your own game notes ("not sure if I lack patience",
  "I simply had no other idea"), not observed. A coach who can ask what you were
  considering would separate "didn't see it" from "saw it and misjudged it".
- Observational throughout. Subgroup figures (single bishop squares, single
  openings) rest on small samples.
- The analysis scripts behind these figures were ad hoc and are not committed.
- Every figure comes from the single-line evaluations (run 1). Those were cleared
  on 2026-09-30, when three-line analysis became the evaluation; a full copy of
  the old sidecar is in the Trash. Under three lines the best move differs in 26%
  of positions, so best-move figures (tactics found, forcing vs quiet, the bishop
  findings) will move noticeably once re-run, and expected-score figures
  slightly.

## Materials

- Study set: `~/Developer/ballasack6-study-set.xlsx`. It holds 751 long-think
  errors (562 where the best move was quiet) and 123 failed bishop captures,
  oldest first, each linked to chess.com Game Review on the move.
- `analysis/structure_baseline.py` (uncommitted) measures long-think share and
  errors per game for three structures. Its structure framing predates the
  finding that the deficit is not structure-specific; the long-think share is the
  part worth tracking.
