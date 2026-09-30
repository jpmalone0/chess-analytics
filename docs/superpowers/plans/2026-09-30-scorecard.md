# Scorecard implementation plan

Spec: `docs/superpowers/specs/2026-09-30-scorecard-design.md`.
Status: self-approved 2026-09-30 (delegated). TDD throughout.

1. **`engine/scorecard.py`: pure core, no database.**
   - `divide(sans)` returns the middlegame and endgame start positions (Lichess
     Divider). Tests: start position is opening; a bare-kings-and-rooks position
     is endgame; a game that never leaves the opening.
   - `game_sides(game, k)` returns per-colour `SideFacts` from SANs, per-position
     evals and PV candidates, result and termination. Tests: phase sums are
     signed and add up; flag loss for won-on-time and timeout vs insufficient
     material; 75%/25% reach; a tactic chance, found and missed; a plain
     recapture is not a chance; a blunder at a chance is not a blunder.
   - `summarize(pairs)` returns rows with you/opp/diff and a bootstrap range.
     Tests: identical sides give diff 0 and "noise"; a consistent gap is "real";
     same input gives the same range.
   - `fit_line` and `rating_score`. Tests: a line recovers slope and intercept;
     wrong-sign or thin data gives no fit; 500 → 30 and 2500 → 80.
2. **`app/scorecard.py`: loading.** Window games through
   `crud._build_game_filters` and the newest complete run per game; the
   calibration pool (same time class, not the player). A per-game facts cache
   keyed on (game, run, k).
3. **Route** `GET /api/players/{username}/analytics/scorecard`, same filter
   params as move-quality. Tests: unknown player 404; no analyzed games gives an
   empty payload; a seeded game gives eight rows.
4. **Frontend:** `scorecard.js` plus a section above Move Quality. Chart.js
   radar (already loaded) plus the rows table; "!" under 300 games; footnote.
5. **Verify** on the real 100-game run plus calibration sample; check in the
   browser; ruff, mypy, full test suite; commit; stacked PR.
