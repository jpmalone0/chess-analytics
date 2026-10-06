# Engine reliability at high ratings

Date: 2026-10-05

## The limit

Every evaluation comes from Stockfish 19 at depth 14 with three lines
(MultiPV 3), one thread and a 64 MB hash. A depth-limited engine has no
official rating; the usual estimate for this setting is roughly 2700–3000,
a little weaker than a single-line search at the same depth.

Against the 100–2700 players most of the app compares, that gap is wide, and
its verdict on their mistakes is sound. At 2800 and above the players are
about as strong as the engine, and it can misjudge their moves: miss a deep
idea, or call a good move a mistake. **Stats for players rated 2800+ may be
unreliable.**

## What follows from it

`engine.views.ENGINE_RELIABLE_ELO_MAX = 2799`. Above it:

- **Kept:** every analysis already run stays in the database, and an elite
  player's own page still shows their move quality and scorecard. It is fun to
  look at, and the numbers are roughly right. The Scorecard says so when the
  viewed player averages over 2799.
- **Left out of every comparison:**
  - Scorecard players lines and the per-state phase norms drop sides rated
    2800+ (`engine.scorecard.within_engine_range`). In a game between a 2700
    and a 2850, the 2700's side still counts.
  - Move Quality's "All players" pool and band presses on it stop at 2799.
  - The Compare To ladder stops at 2700–2799.

## Effect when it landed

Small, because the per-player cap (5 games per player per line) had already
done most of the work: of 83 analyzed rapid sides rated 2800+, only 15 were still in
a players line. Dropping them moved ballasack6's rapid spokes (past month) by
5–60 Elo, all well inside their ranges: opening 2164→2159, middlegame
1658→1662, endgame 1928→1987, tactics 2044→2074, blunders 1657→1655.

The rule is about trust, not size: as more elite games are analyzed, none of
them can reach a comparison.
