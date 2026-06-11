# Two-Wide Analytics Grid with Deferred "Load More Data"

**Date:** 2026-06-08
**Status:** Approved

## Goal

Constrain the analytics charts to be at most two wide. The Layer 2 section
(`#analytics-section`) currently shows three combo charts side-by-side. Reduce it
to a 2-wide grid showing only the first two charts on load, and add a
**"Load More Data"** button that reveals the third chart (Win Rate vs Opponent
Rating) below the first two — the beginning of a 2×2 grid. This pattern lets
future charts be added to the grid and loaded only after the user opts in.

## Current State

In `app/static/index.html`, `#analytics-section` contains a `.triple-pair` grid
(`grid-template-columns: 1fr 1fr 1fr`) with three `.analytic-row` children:

1. 📏 Game Length vs Win Rate (`game-length-chart`)
2. ⏰ Clock Advantage (`clock-chart`)
3. 📊 Win Rate vs Opponent Rating (`rating-diff-chart`)

Below the grid sits `.rating-diff-stats-row` — the Hold / Even Match / Upset
headline stats, which are populated by the same code that renders the rating-diff
chart.

In `app/static/app.js`, `loadColorAnalytics(username, color, op)` unconditionally
calls `loadRatingDiff(...)` (plus the other three chart loaders) for the primary
player, and again with a `-compare` suffix when in compare mode. `loadRatingDiff`
fetches `/api/players/<username>/analytics/rating-diff`, renders the chart, and
fills the Hold/Even/Upset stats. It uses a `loadId` guard to discard stale
in-flight responses.

## Design

### Layout (HTML + CSS)

- Rename `.triple-pair` → `.analytics-grid` and change its
  `grid-template-columns` from `1fr 1fr 1fr` to `1fr 1fr`. Update the
  corresponding selectors: `.triple-pair > .analytic-row`,
  `.triple-pair .chart-container`, `body.compare-mode .triple-pair`, and
  `body.compare-mode .triple-pair > .analytic-row`.
- Keep the **Game Length** and **Clock Advantage** rows as the first two grid
  children (top row).
- Keep the **Win Rate vs Opponent Rating** `.analytic-row` as the third grid
  child, but give it `id="rating-diff-cell"` and start it hidden
  (`display: none`). When revealed it flows to row 2, column 1 — the start of the
  2×2 grid. Future charts fill row 2 column 2 and beyond.
- Give the `.rating-diff-stats-row` element `id="rating-diff-stats-row"` and start
  it hidden; it is shown/hidden together with the cell.
- Add a single toggle button `id="more-data-btn"` anchored at the bottom of the
  analytics section (after the stats row). Initial label: **"Load More Data"**.
  - Collapsed view: two charts, then the button.
  - Expanded view: 2×2 grid (rating-diff in row 2 col 1) + stats row, then the
    button (now labeled **"Hide"**).
- Compare-mode behavior carries over unchanged under the new class name: the grid
  becomes `display: block` (charts stack vertically). The deferred chart stays
  hidden until toggled, then appears in the stacked flow.

### Behavior (app.js) — true lazy load + toggle

- Add module-level state `moreDataShown = false`.
- In `loadColorAnalytics()`, wrap **both** `loadRatingDiff(...)` calls (primary and
  `-compare`) in `if (moreDataShown)`. When false, the `/analytics/rating-diff`
  endpoint is never fetched and the chart is never rendered.
- Add `toggleMoreData()` wired to `#more-data-btn`:
  - **Collapsed → Expanded:** set `moreDataShown = true`; reveal
    `#rating-diff-cell` and `#rating-diff-stats-row`; fetch & render the
    rating-diff chart for the current player (and the compare player when
    `compareMode && currentCompareUsername`) using the current color/opening
    filters and the current `analyticsLoadId` / `compareLoadId`; set the button
    label to **"Hide"**.
  - **Expanded → Collapsed:** set `moreDataShown = false`; hide the cell and stats
    row; set the button label back to **"Load More Data"**. No fetch.
- Because `moreDataShown` persists across filter/color-tab changes, any subsequent
  `loadColorAnalytics` re-run while expanded re-fetches and refreshes the
  rating-diff chart so it stays current with the active filters.
- `loadPlayer()` resets `moreDataShown = false`, restores the button label to
  "Load More Data", and re-hides the cell + stats row, so each newly analyzed
  player starts collapsed.

#### Current-filter access for the toggle

The toggle needs the active `color` and `op` to call `loadRatingDiff`. These are
already tracked: `op` is `currentOpeningFilter`, and the active color comes from
the active `#main-perspective-tabs .tab-btn` (`data-target`). The toggle reads
these the same way the rest of the analytics flow does, so no new state is needed
beyond `moreDataShown`.

#### Lint note

`#more-data-btn` uses an inline `onclick="toggleMoreData()"` like the other
buttons. `eslint.config.js` enforces a `varsIgnorePattern` allowlist of such
handler names; `toggleMoreData` must be added to that pattern, otherwise the
pre-commit eslint hook flags it as an unused variable.

## Out of Scope

- No backend, API, endpoint, schema, or data-model changes. The
  `/analytics/rating-diff` endpoint is unchanged; it is simply not called until
  the user requests it.
- No changes to the other charts (Game Length, Clock Advantage, the Layer 3 time
  charts) or to the games list / overview sections.
- No generalized "deferred chart" framework — only the single rating-diff chart is
  deferred. The 2×2 grid and toggle button are structured so additional deferred
  charts can be added later, but that work is not part of this change.

## Testing / Verification

- Load a player: only Game Length and Clock Advantage render; the rating-diff
  endpoint is not requested (verify via network panel); the "Load More Data"
  button shows.
- Click "Load More Data": rating-diff chart + Hold/Even/Upset stats appear in row
  2; button reads "Hide".
- Click "Hide": chart + stats collapse; button reads "Load More Data".
- While expanded, change the time-control / date / color / opening filter: the
  rating-diff chart refreshes with the new data.
- Analyze a different player: section returns to collapsed state with the button
  reset.
- Compare mode: the deferred chart loads for both players when expanded and stacks
  correctly.
