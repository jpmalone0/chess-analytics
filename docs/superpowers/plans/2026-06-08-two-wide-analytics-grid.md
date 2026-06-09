# Two-Wide Analytics Grid Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Constrain the Layer 2 analytics charts to a 2-wide grid, deferring the Win Rate vs Opponent Rating chart behind a "Load More Data" / "Hide" toggle button that lazy-loads it only on request.

**Architecture:** Pure frontend change in three files. CSS turns the 3-column `.triple-pair` grid into a 2-column `.analytics-grid`. HTML hides the third chart cell + its stats row and adds a toggle button. `app.js` gates the `loadRatingDiff` calls behind a `moreDataShown` flag and adds a `toggleMoreData()` handler that lazy-fetches the chart on first reveal.

**Tech Stack:** Vanilla JS + Chart.js (in-browser), plain CSS, FastAPI static serving. No frontend test framework exists; verification is the pre-commit lint suite (ruff/mypy/eslint) plus manual browser checks.

**Reference spec:** `docs/superpowers/specs/2026-06-08-two-wide-analytics-grid-design.md`

---

## File Structure

- `app/static/style.css` — rename `.triple-pair` → `.analytics-grid`, switch to 2 columns. (~lines 1051–1076)
- `app/static/index.html` — restructure `#analytics-section`: rename grid class, hide the rating-diff cell + stats row, add the toggle button. Bump the `app.js` cache-bust version. (~lines 199–312, 394)
- `app/static/app.js` — add `moreDataShown` state, gate `loadRatingDiff` calls, add `toggleMoreData()`, reset state in `loadPlayer()`. (lines 17–19, 297–318, 902–914)
- `eslint.config.js` — add `toggleMoreData` to the `varsIgnorePattern` allowlist. (line 22)

Note: HTML and CSS share the class name `.analytics-grid`, so Task 1 and Task 2 must use the identical name. JS (Task 3) depends on the HTML ids from Task 2.

---

### Task 1: CSS — two-column analytics grid

**Files:**
- Modify: `app/static/style.css:1051-1076`

- [ ] **Step 1: Rename the grid block to two columns**

Replace the block currently at `app/static/style.css:1051-1068`:

```css
/* ── Triple Pair (3 combo charts) ────────────────────────── */

.triple-pair {
    display: grid;
    grid-template-columns: 1fr 1fr 1fr;
    gap: 1.5rem;
    margin-bottom: 0;
}

.triple-pair > .analytic-row {
    margin-bottom: 0;
    min-width: 0;
}

.triple-pair .chart-container {
    aspect-ratio: 1 / 1;
    max-height: 760px;
}
```

with:

```css
/* ── Analytics Grid (2-wide combo charts) ────────────────── */

.analytics-grid {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 1.5rem;
    margin-bottom: 0;
}

.analytics-grid > .analytic-row {
    margin-bottom: 0;
    min-width: 0;
}

.analytics-grid .chart-container {
    aspect-ratio: 1 / 1;
    max-height: 760px;
}
```

- [ ] **Step 2: Rename the compare-mode rules**

Replace the block currently at `app/static/style.css:1070-1076`:

```css
body.compare-mode .triple-pair {
    display: block;
}

body.compare-mode .triple-pair > .analytic-row {
    margin-bottom: 3rem;
}
```

with:

```css
body.compare-mode .analytics-grid {
    display: block;
}

body.compare-mode .analytics-grid > .analytic-row {
    margin-bottom: 3rem;
}
```

- [ ] **Step 3: Verify no `triple-pair` references remain**

Run: `grep -rn "triple-pair" app/static/`
Expected: only matches in `index.html` (fixed in Task 2). If `style.css` still appears, the rename is incomplete.

- [ ] **Step 4: Commit**

```bash
git add app/static/style.css
git commit -m "style: rename triple-pair to two-column analytics-grid"
```

---

### Task 2: HTML — hide rating-diff cell, add toggle button

**Files:**
- Modify: `app/static/index.html:211` (grid class)
- Modify: `app/static/index.html:238-262` (rating-diff cell + stats row + button)
- Modify: `app/static/index.html:394` (cache-bust version)

- [ ] **Step 1: Rename the grid container class**

At `app/static/index.html:211`, change:

```html
                <div class="triple-pair">
```

to:

```html
                <div class="analytics-grid">
```

- [ ] **Step 2: Hide the rating-diff cell, hide the stats row, add the toggle button**

Replace the block currently at `app/static/index.html:238-262` — the rating-diff `.analytic-row` (which is the third child, still inside the grid), the grid's closing `</div>`, and the `.rating-diff-stats-row` block:

```html
                    <!-- Rating Diff (chart only) -->
                    <div class="analytic-row">
                        <h4 class="chart-title">📊 Win Rate vs Opponent Rating</h4>
                        <div class="compare-wrap">
                            <div class="compare-col">
                                <div class="chart-container"><canvas id="rating-diff-chart"></canvas></div>
                            </div>
                            <div class="compare-col compare-secondary">
                                <div class="chart-container"><canvas id="rating-diff-chart-compare"></canvas></div>
                            </div>
                        </div>
                    </div>
                </div>

                <!-- Hold/Even/Upset rate stats -->
                <div class="rating-diff-stats-row">
                    <div class="compare-wrap">
                        <div class="compare-col">
                            <div class="headline-stats" id="rating-diff-headlines"></div>
                        </div>
                        <div class="compare-col compare-secondary">
                            <div class="headline-stats" id="rating-diff-headlines-compare"></div>
                        </div>
                    </div>
                </div>
```

with (adds `id="rating-diff-cell"` + `hidden` to the cell, `id="rating-diff-stats-row"` + `hidden` to the stats row, and a new toggle button after the stats row):

```html
                    <!-- Rating Diff (chart only) — deferred behind Load More Data -->
                    <div class="analytic-row hidden" id="rating-diff-cell">
                        <h4 class="chart-title">📊 Win Rate vs Opponent Rating</h4>
                        <div class="compare-wrap">
                            <div class="compare-col">
                                <div class="chart-container"><canvas id="rating-diff-chart"></canvas></div>
                            </div>
                            <div class="compare-col compare-secondary">
                                <div class="chart-container"><canvas id="rating-diff-chart-compare"></canvas></div>
                            </div>
                        </div>
                    </div>
                </div>

                <!-- Hold/Even/Upset rate stats -->
                <div class="rating-diff-stats-row hidden" id="rating-diff-stats-row">
                    <div class="compare-wrap">
                        <div class="compare-col">
                            <div class="headline-stats" id="rating-diff-headlines"></div>
                        </div>
                        <div class="compare-col compare-secondary">
                            <div class="headline-stats" id="rating-diff-headlines-compare"></div>
                        </div>
                    </div>
                </div>

                <!-- Load More Data toggle (reveals the deferred rating-diff chart) -->
                <div class="more-data-row">
                    <button class="btn-sm" id="more-data-btn" onclick="toggleMoreData()">Load More Data</button>
                </div>
```

Note: the `hidden` class already exists in this codebase and applies `display: none` (used on `#layer-1`, `#overview-section`, etc.).

- [ ] **Step 3: Bump the app.js cache-bust version**

At `app/static/index.html:394`, change `app.js?v=21` to `app.js?v=22`:

```html
    <script src="/static/app.js?v=22"></script>
```

- [ ] **Step 4: Verify no `triple-pair` references remain anywhere**

Run: `grep -rn "triple-pair" app/static/`
Expected: no output.

- [ ] **Step 5: Commit**

```bash
git add app/static/index.html
git commit -m "feat: defer rating-diff chart behind Load More Data button"
```

---

### Task 3: JS — gate the fetch and add the toggle handler

**Files:**
- Modify: `app/static/app.js:17-19` (state)
- Modify: `app/static/app.js:297-318` (`loadPlayer` reset)
- Modify: `app/static/app.js:902-914` (gate `loadRatingDiff`)
- Modify: `app/static/app.js` end-of-file (new `toggleMoreData`)
- Modify: `eslint.config.js:22` (allowlist)

- [ ] **Step 1: Add the `moreDataShown` state flag**

At `app/static/app.js:19`, after `let currentOpeningFilter = '';`, add:

```javascript
let moreDataShown = false;
```

- [ ] **Step 2: Gate the `loadRatingDiff` calls behind the flag**

In `loadColorAnalytics`, replace the block at `app/static/app.js:902-914`:

```javascript
    const loadId = ++analyticsLoadId;
    loadRatingDiff(username, color, op, loadId);
    loadGameLength(username, color, op, loadId);
    loadClockAdvantage(username, color, op, loadId);
    loadMoveTime(username, color, op, loadId);

    if (compareMode && currentCompareUsername) {
        const cId = ++compareLoadId;
        loadRatingDiff(currentCompareUsername, color, op, cId, '-compare');
        loadGameLength(currentCompareUsername, color, op, cId, '-compare');
        loadClockAdvantage(currentCompareUsername, color, op, cId, '-compare');
        loadMoveTime(currentCompareUsername, color, op, cId, '-compare');
    }
}
```

with:

```javascript
    const loadId = ++analyticsLoadId;
    if (moreDataShown) loadRatingDiff(username, color, op, loadId);
    loadGameLength(username, color, op, loadId);
    loadClockAdvantage(username, color, op, loadId);
    loadMoveTime(username, color, op, loadId);

    if (compareMode && currentCompareUsername) {
        const cId = ++compareLoadId;
        if (moreDataShown) loadRatingDiff(currentCompareUsername, color, op, cId, '-compare');
        loadGameLength(currentCompareUsername, color, op, cId, '-compare');
        loadClockAdvantage(currentCompareUsername, color, op, cId, '-compare');
        loadMoveTime(currentCompareUsername, color, op, cId, '-compare');
    }
}
```

- [ ] **Step 3: Reset the deferred state in `loadPlayer`**

In `loadPlayer`, at `app/static/app.js:306`, after the line `currentOpeningFilter = '';`, add the reset block:

```javascript
    currentOpeningFilter = '';
    moreDataShown = false;
    document.getElementById('rating-diff-cell').classList.add('hidden');
    document.getElementById('rating-diff-stats-row').classList.add('hidden');
    document.getElementById('more-data-btn').textContent = 'Load More Data';
```

- [ ] **Step 4: Add the `toggleMoreData` handler at the end of the file**

Append to the end of `app/static/app.js`:

```javascript

// ═══════════════════════════════════════════════════════════
// Load More Data toggle (deferred rating-diff chart)
// ═══════════════════════════════════════════════════════════

function toggleMoreData() {
    moreDataShown = !moreDataShown;

    const cell = document.getElementById('rating-diff-cell');
    const stats = document.getElementById('rating-diff-stats-row');
    const btn = document.getElementById('more-data-btn');

    cell.classList.toggle('hidden', !moreDataShown);
    stats.classList.toggle('hidden', !moreDataShown);
    btn.textContent = moreDataShown ? 'Hide' : 'Load More Data';

    if (!moreDataShown || !currentUsername) return;

    // Lazy-fetch the rating-diff chart for the current filters.
    const activeTab = document.querySelector('#main-perspective-tabs .tab-btn.active');
    const color = activeTab ? activeTab.dataset.target : 'global';
    const op = currentOpeningFilter;

    loadRatingDiff(currentUsername, color, op, analyticsLoadId);
    if (compareMode && currentCompareUsername) {
        loadRatingDiff(currentCompareUsername, color, op, compareLoadId, '-compare');
    }
}
```

Note: this reuses the current `analyticsLoadId` / `compareLoadId` (not a fresh increment) so the `loadId` staleness guard inside `loadRatingDiff` passes — no other analytics fetch is in flight at toggle time.

- [ ] **Step 5: Allow the new handler name in eslint**

In `eslint.config.js:22`, add `|toggleMoreData` to the `varsIgnorePattern` regex, before the closing `)$`:

```javascript
                "varsIgnorePattern": "^(loadPlayer|nextPage|prevPage|openGameDetail|closeModal|resetDateRange|escapeHtml|toggleCompare|loadComparePlayer|exitCompareMode|nextPageCompare|prevPageCompare|toggleProjection|toggleFitMode|toggleMoreData)$"
```

- [ ] **Step 6: Run the lint suite**

Run: `npx eslint app/static/app.js`
Expected: no `toggleMoreData` unused-var warning, no `no-undef` errors (pre-existing warnings on lines 530/987-989 are acceptable).

- [ ] **Step 7: Commit**

```bash
git add app/static/app.js eslint.config.js
git commit -m "feat: lazy-load rating-diff chart via toggleMoreData"
```

---

### Task 4: Manual verification in the browser

**Files:** none (verification only)

- [ ] **Step 1: Start the app**

Run: `uv run uvicorn app.main:app --reload` (or the project's existing run command — check `main.py` / README).
Open `http://127.0.0.1:8000/` (or the served port).

- [ ] **Step 2: Verify the collapsed default + no premature fetch**

Open the browser dev tools Network tab, then click "Analyze" for the default player.
Expected:
- Only **Game Length** and **Clock Advantage** charts render (2-wide).
- No request to `/api/players/.../analytics/rating-diff` appears.
- A "Load More Data" button shows at the bottom of the analytics section.

- [ ] **Step 3: Verify reveal + lazy fetch**

Click "Load More Data".
Expected:
- A `rating-diff` request fires now.
- The **Win Rate vs Opponent Rating** chart appears in row 2, column 1 (start of the 2×2 grid), with the Hold/Even/Upset stats below it.
- Button text changes to "Hide".

- [ ] **Step 4: Verify hide**

Click "Hide".
Expected: chart + stats collapse; button reads "Load More Data" again.

- [ ] **Step 5: Verify live refresh while expanded**

Click "Load More Data" again, then change the Time Control filter (e.g. Rapid → Blitz).
Expected: the rating-diff chart refreshes with the new data and stays visible.

- [ ] **Step 6: Verify reset on new player**

Type a different username and click "Analyze".
Expected: section returns to collapsed (2 charts only), button reads "Load More Data", no rating-diff request fired.

- [ ] **Step 7: Verify compare mode**

Click "Compare", load a second player, then click "Load More Data".
Expected: the rating-diff chart loads and stacks for both players without layout breakage.

---

## Self-Review Notes

- **Spec coverage:** Layout rename + 2 cols (Task 1, 2); hidden cell + stats id (Task 2); toggle button + lazy fetch + Hide toggle (Task 2, 3); gated fetch in `loadColorAnalytics` (Task 3 Step 2); persist-across-filter refresh (Task 3 Step 2, verified Task 4 Step 5); reset on new player (Task 3 Step 3); eslint allowlist note (Task 3 Step 5); compare mode (Task 4 Step 7). All spec sections mapped.
- **No backend changes** — consistent with spec "Out of Scope".
- **Type/name consistency:** `moreDataShown`, `toggleMoreData`, `#rating-diff-cell`, `#rating-diff-stats-row`, `#more-data-btn`, `.analytics-grid` used identically across all tasks.
