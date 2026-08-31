# Population Baseline Overlays

**Date:** 2026-08-31
**Status:** Approved

## Goal

Give the analytics charts a population reference line, so a user can tell whether
their own number is unusual. Several charts — most importantly the time-per-move
distribution — have no natural anchor: seeing "3.7s per move" answers nothing
without knowing what comparable players do. Add an "average player" overlay,
bucketed by Elo, computed from the games already in the database and grown over
time by crawling opponents.

## Current State

### The corpus already exists

`etl/sync_player.py` writes **both sides** of every game it ingests, so every
opponent of every searched player is already stored. Measured on the current
`chess_analytics.db` (621 MB):

| | |
|---|---|
| Games | 122,403 |
| Player-game rows (both sides) | ~244,000 |
| Moves | 9,685,858 |
| Players | 45,442 |
| Avg moves/game | 39.8 |
| Storage per game incl. moves | ~3 KB |

No new fetching is required to build a first version.

### Compute is not the constraint

Measured against the live database, with no precomputation and no Elo index:

| Query | Time |
|---|---|
| Population move-time aggregate, blitz 1500–1599 | 0.04s |
| Densest possible bucket (blitz 3000–3099, 903k move rows) | 0.23s |
| Same, narrowed to `time_control='180'` | 0.21s |
| Same, plus an opening filter | 0.05s |
| Full sweep of every `time_class` x Elo bucket x second | 8.5s → 2,888 rows |

A baseline can therefore be computed **live per request**. No precomputed table,
no refresh job, no staleness.

### Sample composition is the constraint

Blitz games by rating band:

| Band | Distinct players | Games |
|---|---|---|
| <1000 | 2,007 | 3,874 |
| 1000–1499 | 2,049 | 3,979 |
| 1500–1999 | 603 | 942 |
| 2000–2499 | 1,462 | 3,185 |
| 2500+ | 1,997 | 53,239 |

82% of blitz games sit in the 2500+ band; `hikaru` alone accounts for 16,512 of
them, with `danielnaroditsky` and `magnuscarlsen` adding ~7,000 more. Meanwhile
1500–1999 — where a typical user sits — holds 942 games across 603 players,
about 1.5 games each.

This shape is inherent to the ingest path: a *searched* player contributes
thousands of games, an *opponent* contributes exactly one. Two independent fixes
follow — crawl opponents to widen the corpus, and cap per-player contribution so
no single account defines a bracket.

Capping is not cosmetic. For blitz 3+0, Elo 3000–3099:

| | Distinct players | Mean time/move | Query time |
|---|---|---|---|
| Uncapped (822k moves) | a handful | 2.741s | 0.21s |
| Cap 50 (251k moves) | 333 | 2.961s | 0.12s |
| Cap 100 (317k moves) | 333 | 2.951s | 0.10s |
| Cap 200 (388k moves) | 333 | 2.932s | 0.12s |

An 8% shift between uncapped and capped, because Hikaru's fast play *was* the
population. The estimate is insensitive to the exact cap between 50 and 200, so
100 is used. Capping is also cheaper than not capping.

## Design

### 1. Prerequisite: WAL

`app/database.py` currently runs SQLite in `journal_mode=delete`, where writers
block readers. The background crawl writes while users load charts, which would
surface as `database is locked`. Set `PRAGMA journal_mode=WAL` on connect. This
is a prerequisite, not an optimization.

### 2. Corpus growth — `etl/crawl_opponents.py`

Runs as a FastAPI `BackgroundTask` fired after `/api/players/{username}/sync`
returns, leaving search latency unchanged.

- **Seed:** distinct opponents from the searched player's most recent games.
  Default 25, configurable.
- **Per opponent:** their single most recent chess.com monthly archive, via the existing
  `sync_player(db, opponent, start_date, end_date)`. No new fetch or parse code.
- **Shallow and wide, deliberately.** For population statistics, one game each
  from many distinct players beats many games from few. Depth is the tuning knob
  if execution time needs controlling; the per-player cap makes extra depth
  contribute nothing beyond 100 games anyway.
- **Dedup:** new `players.last_crawled_at` column; skip anyone crawled within
  30 days. Requires changes to `db/schema.sql` and `app/models.py`.
- **Guards:** a module-level lock so only one crawl runs at a time (concurrent
  searches skip rather than queue), a max-opponent budget per crawl, and a
  politeness delay between chess.com requests.
- **Cost:** ~2 MB of database growth and ~50 HTTP requests per search.

### 3. Baseline computation — new `app/baselines.py`

A new module rather than an addition to `crud.py`, which is already 1,160 lines
and serves a different responsibility.

One shared CTE builder produces the population set:

```
sides    -> both sides of every game, as (game_id, side, player_id)
filtered -> Elo band + time control + color + opening, excluding the searched player
capped   -> ROW_NUMBER() OVER (PARTITION BY player_id ORDER BY end_time DESC) <= 100
```

Each baseline function joins that set to `moves` / `games`, mirroring the query
shape of its player-side counterpart in `crud.py`. SQLite 3.51.3 supports the
required window function.

**Bucket key.** The median of the searched player's *own* Elo (the `white_elo`
or `black_elo` of whichever side they played) across the *filtered* game set,
floored to the nearest 100. A player therefore compares against the bracket they
actually played in, not a bracket derived from their opponents.

**Band selection.** The band defaults to the player's own bucket, and a dropdown
lets them compare against any other eligible band — "how do I stack up against
the average 2000-2099 player". One band at a time; each chart holds exactly two
datasets, the player and one reference line.

**Minimum sample.** A band is eligible when it holds at least 30 distinct
players **and** at least 500 games after capping. Below that the baseline is
`null` and the overlay does not render — a missing line rather than a noisy one.

**Adaptive widening applies to the default only.** A flat 100-point band is too
thin in the mid-brackets, so a *derived* default band that falls below the
minimum widens to +/-100, then +/-200, then gives up. An *explicitly selected*
band never widens: if the user asks for 2000-2099, showing them 1900-2199
without saying so would be a lie. They get that band or an empty state. The band
actually used is returned in `meta` and shown in the chart label either way.

**Band availability.** The dropdown is populated from a query counting capped
games and distinct players per 100-band, so it only ever offers bands that will
actually render, each labelled with its sample size. Measured at 0.1s, returning
18 eligible bands for `time_control='180'` on the current database:

```
800 900 1000 1100 1200 1300 1400 · [1500-1900 absent] · 2000 2100 · [2200 absent] · 2300...3100
```

The absences are the thin mid-bands. Surfacing them as gaps in a labelled
dropdown turns a mysteriously missing overlay into visible information about the
corpus.

Availability mirrors the active filters, so the list stays honest when the user
filters to an opening. Selection is sticky: if the selected band drops below the
floor under a new filter, it stays selected and the chart shows an explicit
empty state rather than silently reverting to the default band.

**Time control.** Exact `time_control` for the time-based charts (move-time
distribution, move-time by move number, clock advantage), since 3+0 and 10+0
have nothing to say to each other. `time_class` for the win-rate charts, where
sample size matters more than precision.

**Filter mirroring.** Elo, time control, color, and opening mirror the player's
active chart filters, so the overlay stays apples-to-apples when the user
filters. The date range does **not** mirror: population behavior is stable over
time, so restricting to the user's window would shrink the sample for no gain.

### 4. Charts covered

| Chart | Baseline shape | Notes |
|---|---|---|
| Move-time distribution | Histogram over player-game rows | The motivating case |
| Move-time by move number | Mean per move number | Replaces the logistic fit |
| Clock advantage | Per-game clock comparison, symmetric across sides | One pass over `moves` |
| Game length vs win rate | Histogram | Already anchored at 50%, still separates "everyone loses long games" from "you lose long games" |
| Rating differential | Histogram | As above |
| Streak reaction | Per-player chronological pass | Gated, see below |

`elo-history` and the rolling `winrate-by-color` series are time-indexed per
player and have no meaningful population analogue. They get no overlay.

**Streak reaction gate.** Population streaks need chronological depth per
player, which most of the 45,442 players in the database do not have. This
baseline restricts to players with at least 30 games in the database and reports
its own `n`. It will stay below the min-sample floor — and therefore hidden —
until the crawl has run for a while. Building it now is correct; it switches on
by itself later.

### 5. API

Sibling endpoints: `/api/players/{username}/analytics/{chart}/baseline`.

Chosen over a `?baseline=1` flag on the existing endpoints so that existing
payloads stay byte-identical, the frontend can fetch player and baseline data in
parallel, and a baseline failure degrades to a normal chart rather than breaking
it.

Each accepts the same filter params as its player-side counterpart, plus an
optional `elo_band` (the band's lower bound, e.g. `2000`). Omitted means "derive
from the player and widen if needed"; supplied means "this band exactly, or
nothing".

Each returns its data plus:

```json
"meta": {
  "elo_band": [1500, 1599],
  "time_control": "180",
  "n_players": 412,
  "n_games": 5340,
  "widened": false,
  "source": "derived"
}
```

`source` is `"derived"` or `"selected"`, so the frontend can distinguish "your
band" from an explicit comparison in the chart label.

One additional endpoint, `/api/players/{username}/analytics/baseline-bands`,
returns the eligible bands for the current filters — `[{elo_band, n_players,
n_games}]` — to populate the dropdown. Shared across all charts, fetched once
per filter change rather than per chart.

### 6. Frontend

- Each supported chart gains a second, muted, dashed dataset, labelled from
  `meta` — e.g. *Average · 1500–1599 · 3+0 · 412 players* when derived, or
  *Compared to 2000–2099 · 828 players* when explicitly selected.
- A global overlay toggle, defaulting to on, and a band dropdown populated from
  `baseline-bands`. The dropdown defaults to the player's own band, marks it as
  such, and is shared across all charts so switching bands moves every overlay
  at once.
- When the selected band has no data under the current filters, the chart shows
  an explicit empty state naming the band, not a blank space.
- Baseline fetched in parallel with the player data. A `null` baseline or a
  failed request renders the chart alone, with no error surfaced.
- **The logistic fit is removed** from move-time-by-move-number. It was a
  smoothing device standing in for the missing reference this feature provides.
  Delete `fitLogLogistic` (`app/static/app.js:1479`), its RMSE readout
  (`app/static/app.js:1651`), and the fit-derived "peak move" stat card
  (`app/static/app.js:1675`). The card becomes *your peak thinking move vs the
  population's*.
- `fitLogarithmic` / `fitLinear` (`app/static/app.js:727`) belong to the
  Elo-history projection and are unrelated. They stay.

## Testing

Against a small seeded fixture database:

- The cap actually caps — capped and uncapped means differ on seeded data where
  one player is over-represented.
- The searched player is absent from their own baseline.
- A derived band widens through +/-100 and +/-200 and reports it in
  `meta.widened` and `meta.elo_band`.
- An explicitly selected band never widens — a thin selected band returns `null`
  rather than a quietly broadened one.
- `baseline-bands` omits every band below the min-sample floor, and its list
  responds to filter changes.
- The min-sample floor returns `null` rather than a thin, noisy line.
- Filter mirroring: an opening or color filter changes the baseline; a date
  filter does not.
