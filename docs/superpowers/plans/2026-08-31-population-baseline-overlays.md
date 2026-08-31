# Population Baseline Overlays Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an Elo-bucketed "average player" reference line to the analytics charts, computed live from games already in the database, with a dropdown to compare against any other rating band.

**Architecture:** A new `app/baselines.py` builds a population set as a SQL CTE (both sides of every game, filtered by Elo band and time control, excluding the searched player, capped at 100 games per player via `ROW_NUMBER()`), then joins that set to `moves`/`games` mirroring each existing query in `crud.py`. Baselines are computed per request — measured at 0.04-0.23s worst case, so no precomputed table exists. New sibling endpoints (`.../analytics/{chart}/baseline`) leave existing payloads untouched, and the frontend fetches them in parallel so a baseline failure degrades to a plain chart.

**Tech Stack:** Python 3.11, FastAPI, SQLAlchemy 2.x (raw SQL via `text()`), SQLite 3.51.3 (window functions required), Chart.js, vanilla JS.

**Spec:** `docs/superpowers/specs/2026-08-31-population-baseline-overlays-design.md`

**Scope:** This plan covers the baseline computation, API, and overlay UI. The background opponent crawl (spec section 2) and the WAL switch it requires are a separate plan — the overlay works today without them, with 13 contiguous rapid bands from 700-1900.

---

## File Structure

| File | Responsibility |
|---|---|
| `app/baselines.py` (create) | All population-set SQL and band resolution. New module rather than an addition to `crud.py`, which is 1,160 lines and serves per-player queries. |
| `app/main.py` (modify) | Six new sibling endpoints plus `baseline-bands`. |
| `tests/conftest.py` (create) | In-memory SQLite fixture with a seeded game/move corpus. |
| `tests/test_baselines.py` (create) | Band resolution, capping, exclusion, fallback. |
| `app/static/app.js` (modify) | Band dropdown state, overlay fetch helper, per-chart overlay datasets, removal of the logistic fit. |
| `app/static/index.html` (modify) | Band dropdown in the filters bar; remove the RMSE span. |

**Conventions to follow:** queries are raw SQL in `text()` with bound params (never f-string interpolation of values); helper functions are module-private with a leading underscore; `Optional[X]` not `X | None` in signatures, matching `crud.py`.

---

## Task 1: Test scaffold

No `tests/` directory exists and pytest is not a dependency. Everything downstream needs this.

**Files:**
- Modify: `pyproject.toml`
- Create: `tests/__init__.py`, `tests/conftest.py`

- [ ] **Step 1: Add pytest**

```bash
uv add --dev pytest
```

- [ ] **Step 2: Create the fixture module**

Create `tests/__init__.py` as an empty file. Then create `tests/conftest.py`:

```python
"""Shared pytest fixtures — an in-memory database with a hand-seeded corpus."""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models import Game, Move, Player


@pytest.fixture
def db():
    """In-memory SQLite session with the full schema created."""
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()


def make_player(db, username):
    p = Player(username=username, platform="chess.com")
    db.add(p)
    db.flush()
    return p


def make_game(
    db, white, black, white_elo, black_elo,
    time_control="600", time_class="rapid", result="1-0",
    end_time=1700000000, opening_name="Sicilian Defense", total_moves=40,
    white_move_times=None, black_move_times=None,
):
    """Create one game plus its moves. Move time lists default to a flat 5s."""
    g = Game(
        white_player_id=white.player_id, black_player_id=black.player_id,
        result=result, time_control=time_control, time_class=time_class,
        white_elo=white_elo, black_elo=black_elo, end_time=end_time,
        opening_name=opening_name, total_moves=total_moves,
        chess_com_url=f"https://example.test/{white.username}/{black.username}/{end_time}",
    )
    db.add(g)
    db.flush()

    white_move_times = white_move_times if white_move_times is not None else [5.0] * 3
    black_move_times = black_move_times if black_move_times is not None else [5.0] * 3

    ply = 0
    for i in range(max(len(white_move_times), len(black_move_times))):
        for color, times in (("white", white_move_times), ("black", black_move_times)):
            if i >= len(times):
                continue
            ply += 1
            db.add(Move(
                game_id=g.game_id, ply=ply, move_number=i + 1, color=color,
                move_san="e4", clock_seconds=None, time_spent_seconds=times[i],
            ))
    db.flush()
    return g


_seed_calls = 0


def seed_band(db, lo, n_players, games_each=1, move_time=5.0, **kwargs):
    """Seed n_players distinct players in the [lo, lo+99] band, each playing
    `games_each` games as white against a shared throwaway opponent.

    Usernames carry a call counter, not just the band: some tests seed the same
    band twice (e.g. one time control that is dense and one that is sparse), and
    players.username is UNIQUE.
    """
    global _seed_calls
    _seed_calls += 1
    tag = f"{lo}-{_seed_calls}"

    filler = make_player(db, f"filler-{tag}")
    players = []
    for i in range(n_players):
        p = make_player(db, f"p{tag}-{i}")
        players.append(p)
        for gi in range(games_each):
            # The filler's Elo is parked far below any band under test: it plays
            # every game, so leaving it in-band would let one player's results
            # dominate the very population the cap exists to protect.
            make_game(
                db, p, filler, white_elo=lo + 50, black_elo=100,
                end_time=1700000000 + gi,
                white_move_times=[move_time] * 3,
                **kwargs,
            )
    db.commit()
    return players
```

- [ ] **Step 3: Verify the fixture imports and the schema builds**

```bash
uv run pytest tests/ -q
```

Expected: `no tests ran` — collection succeeds with zero tests, no import errors.

- [ ] **Step 4: Commit**

```bash
git add pyproject.toml uv.lock tests/
git commit -m "test: add pytest scaffold and seeded in-memory db fixture"
```

---

## Task 2: Population set CTE and counts

The shared SQL that every baseline builds on.

**Files:**
- Create: `app/baselines.py`
- Test: `tests/test_baselines.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_baselines.py`:

```python
from app import baselines
from tests.conftest import make_game, make_player, seed_band


def test_population_counts_excludes_searched_player(db):
    seed_band(db, 1500, n_players=5)
    target = make_player(db, "target")
    other = make_player(db, "other")
    for i in range(3):
        make_game(db, target, other, white_elo=1550, black_elo=1550, end_time=1700001000 + i)
    db.commit()

    with_target = baselines.population_counts(
        db, elo_lo=1500, elo_hi=1599, time_control="600", exclude_player_id=-1)
    without_target = baselines.population_counts(
        db, elo_lo=1500, elo_hi=1599, time_control="600",
        exclude_player_id=target.player_id)

    assert with_target["n_games"] > without_target["n_games"]
    assert with_target["n_players"] == without_target["n_players"] + 1


def test_population_ignores_date_filters(db):
    """Spec: Elo, time control, colour and opening mirror the player's filters;
    the date range deliberately does not. There is no date parameter to pass —
    this test exists to catch anyone adding one."""
    import inspect

    sig = inspect.signature(baselines.population_counts)
    assert "start_date" not in sig.parameters
    assert "end_date" not in sig.parameters


def test_population_counts_caps_per_player(db):
    hog = make_player(db, "hog")
    foil = make_player(db, "foil")
    for i in range(baselines.PER_PLAYER_CAP + 40):
        make_game(db, hog, foil, white_elo=1550, black_elo=900, end_time=1700000000 + i)
    db.commit()

    counts = baselines.population_counts(
        db, elo_lo=1500, elo_hi=1599, time_control="600", exclude_player_id=-1)

    assert counts["n_players"] == 1
    assert counts["n_games"] == baselines.PER_PLAYER_CAP
```

- [ ] **Step 2: Run it and confirm it fails**

```bash
uv run pytest tests/test_baselines.py -q
```

Expected: FAIL — `ModuleNotFoundError: No module named 'app.baselines'`.

- [ ] **Step 3: Write the implementation**

Create `app/baselines.py`:

```python
"""
Population baselines — Elo-bucketed "average player" reference data.

Every function here builds on one shared CTE that turns the games table into a
set of player-game rows (both sides of each game), filtered to an Elo band and
time control, with the searched player removed and each remaining player capped
at PER_PLAYER_CAP games. The cap matters: without it a single heavily-synced
account defines its whole bracket.
"""

from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

# A band is usable only above both floors.
MIN_PLAYERS = 30
MIN_GAMES = 500

# No single player may contribute more than this to a band.
PER_PLAYER_CAP = 100


def _population_cte(
    elo_lo: int,
    elo_hi: int,
    exclude_player_id: int,
    time_control: Optional[str] = None,
    time_class: Optional[str] = None,
    player_color: Optional[str] = None,
    opening_names: Optional[str] = None,
) -> tuple[str, dict]:
    """
    Build the shared CTE chain and its params. Callers prefix it with WITH and
    then select from `pop`, which yields (game_id, pid, side).

    time_control wins over time_class when both are given — exact keying first,
    class-level fallback second.
    """
    params: dict[str, Any] = {
        "elo_lo": elo_lo,
        "elo_hi": elo_hi,
        "exclude_pid": exclude_player_id,
        "cap": PER_PLAYER_CAP,
    }

    shared: list[str] = []
    if time_control:
        shared.append("g.time_control = :time_control")
        params["time_control"] = time_control
    elif time_class:
        shared.append("g.time_class = :time_class")
        params["time_class"] = time_class

    if opening_names:
        ops = [o.strip() for o in opening_names.split("|") if o.strip()]
        if ops:
            likes = [f"g.opening_name LIKE :bop_{i}" for i in range(len(ops))]
            shared.append("(" + " OR ".join(likes) + ")")
            for i, op in enumerate(ops):
                params[f"bop_{i}"] = op + "%"

    shared_sql = (" AND " + " AND ".join(shared)) if shared else ""

    # player_color mirrors the player's own filter: if they are looking at their
    # games as white, the population is other people's white games.
    sides: list[str] = []
    if player_color != "black":
        sides.append(f"""
            SELECT g.game_id, g.white_player_id AS pid, 'white' AS side, g.end_time
            FROM   games g
            WHERE  g.white_elo >= :elo_lo AND g.white_elo <= :elo_hi
              AND  g.white_player_id != :exclude_pid{shared_sql}""")
    if player_color != "white":
        sides.append(f"""
            SELECT g.game_id, g.black_player_id AS pid, 'black' AS side, g.end_time
            FROM   games g
            WHERE  g.black_elo >= :elo_lo AND g.black_elo <= :elo_hi
              AND  g.black_player_id != :exclude_pid{shared_sql}""")

    cte = f"""
        sides AS ({" UNION ALL ".join(sides)}),
        capped AS (
            SELECT game_id, pid, side,
                   ROW_NUMBER() OVER (PARTITION BY pid ORDER BY end_time DESC) AS rn
            FROM   sides
        ),
        pop AS (SELECT game_id, pid, side FROM capped WHERE rn <= :cap)
    """
    return cte, params


def population_counts(
    db: Session,
    elo_lo: int,
    elo_hi: int,
    exclude_player_id: int,
    time_control: Optional[str] = None,
    time_class: Optional[str] = None,
    player_color: Optional[str] = None,
    opening_names: Optional[str] = None,
) -> dict:
    """Distinct players and capped games in a band. Drives the min-sample gate."""
    cte, params = _population_cte(
        elo_lo, elo_hi, exclude_player_id,
        time_control, time_class, player_color, opening_names,
    )
    row = db.execute(
        text(f"WITH {cte} SELECT COUNT(DISTINCT pid) AS n_players, COUNT(*) AS n_games FROM pop"),
        params,
    ).mappings().one()
    return {"n_players": row["n_players"], "n_games": row["n_games"]}
```

- [ ] **Step 4: Run the tests**

```bash
uv run pytest tests/test_baselines.py -q
```

Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add app/baselines.py tests/test_baselines.py
git commit -m "feat: population set CTE with per-player cap and self-exclusion"
```

---

## Task 3: Band resolution — widening, time-control fallback, precedence

**Files:**
- Modify: `app/baselines.py`
- Test: `tests/test_baselines.py`

Rules from the spec:
- A *derived* band (no explicit selection) widens through +/-0, +/-100, +/-200.
- A *selected* band never widens — that band or nothing.
- A sparse exact `time_control` falls back to its `time_class`.
- Time control widens **before** Elo band: the Elo comparison is the point, the time control is context.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_baselines.py`:

```python
def test_derived_band_widens_when_thin(db):
    # 1500 band alone is far below MIN_GAMES; neighbours make the widened band viable.
    seed_band(db, 1500, n_players=10, games_each=2)
    seed_band(db, 1400, n_players=200, games_each=2)
    seed_band(db, 1600, n_players=200, games_each=2)
    target = make_player(db, "target")
    foil = make_player(db, "foil")
    for i in range(10):
        make_game(db, target, foil, white_elo=1550, black_elo=1550, end_time=1700009000 + i)
    db.commit()

    band = baselines.resolve_band(
        db, player_id=target.player_id, time_class="rapid", selected_band=None)

    assert band is not None
    assert band["widened"] is True
    assert band["elo_lo"] < 1500 and band["elo_hi"] > 1599
    assert band["source"] == "derived"


def test_selected_band_never_widens(db):
    seed_band(db, 1500, n_players=10, games_each=2)   # below the floor
    seed_band(db, 1400, n_players=200, games_each=2)  # would rescue it if widened
    target = make_player(db, "target")
    foil = make_player(db, "foil")
    make_game(db, target, foil, white_elo=1550, black_elo=1550, end_time=1700009999)
    db.commit()

    band = baselines.resolve_band(
        db, player_id=target.player_id, time_class="rapid", selected_band=1500)

    assert band is None


def test_sparse_time_control_falls_back_to_time_class(db):
    # 900+10 is too thin to stand alone; the rapid class as a whole is not.
    seed_band(db, 1500, n_players=300, games_each=2, time_control="600", time_class="rapid")
    seed_band(db, 1500, n_players=5, games_each=1, time_control="900+10", time_class="rapid")
    target = make_player(db, "target")
    foil = make_player(db, "foil")
    for i in range(10):
        make_game(db, target, foil, white_elo=1550, black_elo=1550,
                  time_control="900+10", time_class="rapid", end_time=1700009000 + i)
    db.commit()

    band = baselines.resolve_band(
        db, player_id=target.player_id, time_class="rapid", selected_band=None)

    assert band is not None
    assert band["tc_fallback"] is True
    assert band["time_control"] is None
    assert band["widened"] is False   # time control blurred first, band stayed put


def test_dominant_time_control_is_the_modal_one(db):
    target = make_player(db, "target")
    foil = make_player(db, "foil")
    for i in range(7):
        make_game(db, target, foil, white_elo=1550, black_elo=1550,
                  time_control="600", end_time=1700000000 + i)
    for i in range(2):
        make_game(db, target, foil, white_elo=1550, black_elo=1550,
                  time_control="900+10", end_time=1700100000 + i)
    db.commit()

    assert baselines.dominant_time_control(db, target.player_id, time_class="rapid") == "600"
```

- [ ] **Step 2: Run and confirm failure**

```bash
uv run pytest tests/test_baselines.py -q
```

Expected: FAIL — `AttributeError: module 'app.baselines' has no attribute 'resolve_band'`.

- [ ] **Step 3: Implement**

Append to `app/baselines.py`:

```python
# Half-widths tried in order when a derived band is too thin.
BAND_WIDENING = [0, 100, 200]


def _player_filter_sql(
    time_class: Optional[str],
    start_date=None,
    end_date=None,
    player_color: Optional[str] = None,
    opening_names: Optional[str] = None,
) -> tuple[str, dict]:
    """WHERE clause over the searched player's own games. Mirrors
    crud._build_game_filters but is duplicated deliberately: this module must
    stay free of crud's per-player concerns, and the two will diverge."""
    clauses: list[str] = []
    params: dict[str, Any] = {}

    if player_color == "white":
        clauses.append("g.white_player_id = :player_id")
    elif player_color == "black":
        clauses.append("g.black_player_id = :player_id")
    else:
        clauses.append("(g.white_player_id = :player_id OR g.black_player_id = :player_id)")

    if time_class:
        clauses.append("g.time_class = :time_class")
        params["time_class"] = time_class
    if start_date:
        clauses.append("g.date_played >= :start_date")
        params["start_date"] = start_date
    if end_date:
        clauses.append("g.date_played <= :end_date")
        params["end_date"] = end_date
    if opening_names:
        ops = [o.strip() for o in opening_names.split("|") if o.strip()]
        if ops:
            likes = [f"g.opening_name LIKE :pop_{i}" for i in range(len(ops))]
            clauses.append("(" + " OR ".join(likes) + ")")
            for i, op in enumerate(ops):
                params[f"pop_{i}"] = op + "%"

    return " AND ".join(clauses), params


def dominant_time_control(db: Session, player_id: int, **filters) -> Optional[str]:
    """The player's modal time_control under the current filters.

    The UI only exposes time_class, but time-based baselines need an exact
    control — 3+0 and 10+0 have nothing to say to each other. So we derive it.
    """
    where, params = _player_filter_sql(**filters)
    params["player_id"] = player_id
    row = db.execute(text(f"""
        SELECT g.time_control, COUNT(*) AS n
        FROM   games g
        WHERE  {where} AND g.time_control IS NOT NULL
        GROUP  BY g.time_control
        ORDER  BY n DESC
        LIMIT  1
    """), params).mappings().first()
    return row["time_control"] if row else None


def player_median_elo(db: Session, player_id: int, **filters) -> Optional[int]:
    """Median of the player's OWN Elo across their filtered games."""
    where, params = _player_filter_sql(**filters)
    params["player_id"] = player_id
    rows = db.execute(text(f"""
        SELECT CASE WHEN g.white_player_id = :player_id THEN g.white_elo ELSE g.black_elo END AS elo
        FROM   games g
        WHERE  {where}
        ORDER  BY elo
    """), params).scalars().all()
    elos = [e for e in rows if e is not None]
    if not elos:
        return None
    return int(elos[len(elos) // 2])


def _band_is_viable(counts: dict) -> bool:
    return counts["n_players"] >= MIN_PLAYERS and counts["n_games"] >= MIN_GAMES


def resolve_band(
    db: Session,
    player_id: int,
    time_class: Optional[str] = None,
    start_date=None,
    end_date=None,
    player_color: Optional[str] = None,
    opening_names: Optional[str] = None,
    selected_band: Optional[int] = None,
) -> Optional[dict]:
    """
    Decide which population slice to compare against.

    Escalation order, per spec: exact time control -> time class, and only then
    widen the Elo band. Blurring the context beats blurring the comparison.
    A selected band never widens; a derived one may.

    Returns None when nothing viable exists — the caller renders no overlay.
    """
    player_filters = dict(
        time_class=time_class, start_date=start_date, end_date=end_date,
        player_color=player_color, opening_names=opening_names,
    )

    if selected_band is not None:
        base_lo = selected_band
        source = "selected"
    else:
        median = player_median_elo(db, player_id, **player_filters)
        if median is None:
            return None
        base_lo = (median // 100) * 100
        source = "derived"

    exact_tc = dominant_time_control(db, player_id, **player_filters)
    widths = [0] if source == "selected" else BAND_WIDENING

    # Time control blurs first, then the band widens.
    for tc, tc_fallback in ((exact_tc, False), (None, True)):
        if tc is None and not tc_fallback:
            continue
        for width in widths:
            lo, hi = base_lo - width, base_lo + 99 + width
            counts = population_counts(
                db, elo_lo=lo, elo_hi=hi, exclude_player_id=player_id,
                time_control=tc, time_class=time_class,
                player_color=player_color, opening_names=opening_names,
            )
            if _band_is_viable(counts):
                return {
                    "elo_lo": lo,
                    "elo_hi": hi,
                    "time_control": tc,
                    "time_class": time_class,
                    "n_players": counts["n_players"],
                    "n_games": counts["n_games"],
                    "widened": width > 0,
                    "tc_fallback": tc_fallback,
                    "source": source,
                }
    return None
```

- [ ] **Step 4: Run the tests**

```bash
uv run pytest tests/test_baselines.py -q
```

Expected: 7 passed.

- [ ] **Step 5: Commit**

```bash
git add app/baselines.py tests/test_baselines.py
git commit -m "feat: band resolution with widening and time-control fallback"
```

---

## Task 4: Available bands, and its endpoint

**Files:**
- Modify: `app/baselines.py`, `app/main.py`
- Test: `tests/test_baselines.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_baselines.py`:

```python
def test_available_bands_omits_bands_below_floor(db):
    seed_band(db, 1500, n_players=300, games_each=2)  # viable
    seed_band(db, 1700, n_players=5, games_each=1)    # too thin
    target = make_player(db, "target")
    db.commit()

    bands = baselines.available_bands(
        db, player_id=target.player_id, time_class="rapid", time_control="600")

    los = [b["elo_lo"] for b in bands]
    assert 1500 in los
    assert 1700 not in los
    entry = next(b for b in bands if b["elo_lo"] == 1500)
    assert entry["elo_hi"] == 1599
    assert entry["n_players"] >= baselines.MIN_PLAYERS
```

- [ ] **Step 2: Run and confirm failure**

```bash
uv run pytest tests/test_baselines.py::test_available_bands_omits_bands_below_floor -q
```

Expected: FAIL — no attribute `available_bands`.

- [ ] **Step 3: Implement in `app/baselines.py`**

```python
def available_bands(
    db: Session,
    player_id: int,
    time_class: Optional[str] = None,
    time_control: Optional[str] = None,
    player_color: Optional[str] = None,
    opening_names: Optional[str] = None,
) -> list[dict]:
    """
    Every 100-band that clears the min-sample floor, for the dropdown.

    One grouped pass rather than a probe per band. Bands below the floor are
    omitted entirely, so the dropdown can never offer a band that renders empty
    — and the resulting gaps are themselves informative about the corpus.
    """
    cte, params = _population_cte(
        elo_lo=0, elo_hi=4000, exclude_player_id=player_id,
        time_control=time_control, time_class=time_class,
        player_color=player_color, opening_names=opening_names,
    )
    params["min_players"] = MIN_PLAYERS
    params["min_games"] = MIN_GAMES

    rows = db.execute(text(f"""
        WITH {cte},
        banded AS (
            SELECT pop.pid,
                   (CASE WHEN pop.side = 'white' THEN g.white_elo ELSE g.black_elo END / 100) * 100 AS elo_lo
            FROM   pop JOIN games g ON g.game_id = pop.game_id
        )
        SELECT elo_lo,
               COUNT(DISTINCT pid) AS n_players,
               COUNT(*)            AS n_games
        FROM   banded
        GROUP  BY elo_lo
        HAVING n_players >= :min_players AND n_games >= :min_games
        ORDER  BY elo_lo
    """), params).mappings().all()

    return [
        {
            "elo_lo": int(r["elo_lo"]),
            "elo_hi": int(r["elo_lo"]) + 99,
            "n_players": r["n_players"],
            "n_games": r["n_games"],
        }
        for r in rows
    ]
```

- [ ] **Step 4: Run the test**

```bash
uv run pytest tests/test_baselines.py -q
```

Expected: 8 passed.

- [ ] **Step 5: Add the endpoint**

Append to `app/main.py`, after the existing `top_openings` endpoint:

```python
# ── Population Baselines ─────────────────────────────────

@app.get("/api/players/{username}/analytics/baseline-bands")
def baseline_bands(
    username: str,
    time_class: Optional[str] = None,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    player_color: Optional[str] = None,
    opening_names: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """Elo bands with enough data to serve as a baseline under these filters."""
    player = crud.get_player(db, username)
    if not player:
        raise HTTPException(404, f"Player '{username}' not found")

    tc = baselines.dominant_time_control(
        db, player.player_id, time_class=time_class,
        start_date=start_date, end_date=end_date,
        player_color=player_color, opening_names=opening_names,
    )
    bands = baselines.available_bands(
        db, player.player_id, time_class=time_class, time_control=tc,
        player_color=player_color, opening_names=opening_names,
    )
    median = baselines.player_median_elo(
        db, player.player_id, time_class=time_class,
        start_date=start_date, end_date=end_date,
        player_color=player_color, opening_names=opening_names,
    )
    return {
        "bands": bands,
        "player_band": (median // 100) * 100 if median is not None else None,
        "time_control": tc,
    }
```

Update the import at the top of `app/main.py`:

```python
from app import baselines, crud, schemas
```

- [ ] **Step 6: Verify against the real database**

```bash
uv run uvicorn app.main:app --port 8123 &
sleep 3 && curl -s "http://127.0.0.1:8123/api/players/ballasack6/analytics/baseline-bands?time_class=rapid" | head -c 600
```

Expected: a `bands` array whose `elo_lo` values run 700 through 1900 with no gaps, and a non-null `player_band`.

Stop the server when done:

```bash
pkill -f "uvicorn app.main:app --port 8123"
```

- [ ] **Step 7: Commit**

```bash
git add app/baselines.py app/main.py tests/test_baselines.py
git commit -m "feat: available Elo bands endpoint for the baseline dropdown"
```

---

## Task 5: Move-time baseline

The motivating chart. Mirrors `crud.move_time_stats` (`app/crud.py:628`) — same bucket definitions, same shape, population set instead of one player.

**Files:**
- Modify: `app/baselines.py`, `app/main.py`
- Test: `tests/test_baselines.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_baselines.py`:

```python
def test_move_time_baseline_reflects_population_not_target(db):
    # Population thinks 8s per move; the target thinks 1s. The baseline must
    # report the population's number, unaffected by the target's games.
    seed_band(db, 1500, n_players=300, games_each=2, move_time=8.0)
    target = make_player(db, "target")
    foil = make_player(db, "foil")
    for i in range(50):
        make_game(db, target, foil, white_elo=1550, black_elo=1550,
                  end_time=1700009000 + i, white_move_times=[1.0] * 3)
    db.commit()

    band = baselines.resolve_band(db, target.player_id, time_class="rapid")
    result = baselines.move_time_baseline(db, target.player_id, band, time_class="rapid")

    assert result["mean"] == 8.0
    assert result["total_moves"] > 0
    assert len(result["buckets"]) == 7
    assert result["by_move_number"][0]["avg_seconds"] == 8.0
```

- [ ] **Step 2: Run and confirm failure**

```bash
uv run pytest tests/test_baselines.py::test_move_time_baseline_reflects_population_not_target -q
```

Expected: FAIL — no attribute `move_time_baseline`.

- [ ] **Step 3: Implement**

Add to the top of `app/baselines.py`, alongside the other imports:

```python
import statistics as _stats
```

Then append:

```python
def _move_time_buckets(time_class: Optional[str]) -> list[tuple[str, float, float]]:
    """Bucket edges, identical to crud.move_time_stats so the overlay lines up
    with the player's own histogram bar for bar."""
    if time_class == "bullet":
        return [
            ("0–0.5s", 0, 0.5), ("0.5–1s", 0.5, 1),
            ("1–1.5s", 1, 1.5), ("1.5–2s", 1.5, 2),
            ("2–2.5s", 2, 2.5), ("2.5–3s", 2.5, 3),
            ("3s+", 3, 9999),
        ]
    if time_class == "blitz":
        return [
            ("0–2s", 0, 2), ("2–4s", 2, 4), ("4–6s", 4, 6),
            ("6–8s", 6, 8), ("8–10s", 8, 10), ("10–12s", 10, 12),
            ("12s+", 12, 9999),
        ]
    return [
        ("0–5s", 0, 5), ("5–10s", 5, 10), ("10–15s", 10, 15),
        ("15–20s", 15, 20), ("20–25s", 20, 25), ("25–30s", 25, 30),
        ("30s+", 30, 9999),
    ]


def move_time_baseline(
    db: Session,
    player_id: int,
    band: Optional[dict],
    time_class: Optional[str] = None,
    player_color: Optional[str] = None,
    opening_names: Optional[str] = None,
) -> Optional[dict]:
    """Population time-per-move distribution and mean-by-move-number.

    Percentages, not counts: the population has far more moves than any one
    player, so only the shape is comparable.
    """
    if band is None:
        return None

    cte, params = _population_cte(
        band["elo_lo"], band["elo_hi"], player_id,
        band["time_control"], time_class, player_color, opening_names,
    )
    rows = db.execute(text(f"""
        WITH {cte}
        SELECT m.move_number, m.time_spent_seconds
        FROM   moves m
        JOIN   pop ON m.game_id = pop.game_id AND m.color = pop.side
        WHERE  m.time_spent_seconds IS NOT NULL
          AND  m.time_spent_seconds >= 0
    """), params).mappings().all()

    if not rows:
        return None

    all_times: list[float] = []
    by_move: dict[int, list[float]] = {}
    for row in rows:
        t = float(row["time_spent_seconds"])
        all_times.append(t)
        by_move.setdefault(int(row["move_number"]), []).append(t)

    bucket_defs = _move_time_buckets(time_class)
    counts = {label: 0 for label, _, _ in bucket_defs}
    for t in all_times:
        for label, lo, hi in bucket_defs:
            if lo <= t < hi:
                counts[label] += 1
                break

    total = len(all_times)
    return {
        "buckets": [
            {"label": label, "count": counts[label], "pct": round(counts[label] / total * 100, 1)}
            for label, _, _ in bucket_defs
        ],
        "mean": round(_stats.mean(all_times), 2),
        "median": round(_stats.median(all_times), 2),
        "std_dev": round(_stats.stdev(all_times) if total > 1 else 0.0, 2),
        "total_moves": total,
        "by_move_number": [
            {
                "move_number": mn,
                "avg_seconds": round(_stats.mean(by_move[mn]), 2),
                "count": len(by_move[mn]),
            }
            for mn in sorted(by_move) if mn <= 100
        ],
    }
```

- [ ] **Step 4: Run the tests**

```bash
uv run pytest tests/test_baselines.py -q
```

Expected: 9 passed.

- [ ] **Step 5: Add the endpoint**

Append to `app/main.py`:

```python
@app.get("/api/players/{username}/analytics/move-time/baseline")
def move_time_baseline_route(
    username: str,
    time_class: Optional[str] = None,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    player_color: Optional[str] = None,
    opening_names: Optional[str] = None,
    elo_band: Optional[int] = None,
    db: Session = Depends(get_db),
):
    player = crud.get_player(db, username)
    if not player:
        raise HTTPException(404, f"Player '{username}' not found")

    band = baselines.resolve_band(
        db, player.player_id, time_class=time_class,
        start_date=start_date, end_date=end_date,
        player_color=player_color, opening_names=opening_names,
        selected_band=elo_band,
    )
    data = baselines.move_time_baseline(
        db, player.player_id, band, time_class, player_color, opening_names)
    if data is None:
        return {"data": None, "meta": None}
    return {"data": data, "meta": baselines.band_meta(band)}
```

And add the `band_meta` helper to `app/baselines.py`:

```python
def band_meta(band: dict) -> dict:
    """The band description the frontend puts in the chart label."""
    return {
        "elo_band": [band["elo_lo"], band["elo_hi"]],
        "time_control": band["time_control"],
        "time_class": band["time_class"],
        "n_players": band["n_players"],
        "n_games": band["n_games"],
        "widened": band["widened"],
        "tc_fallback": band["tc_fallback"],
        "source": band["source"],
    }
```

- [ ] **Step 6: Verify against the real database**

```bash
uv run uvicorn app.main:app --port 8123 &
sleep 3 && curl -s "http://127.0.0.1:8123/api/players/ballasack6/analytics/move-time/baseline?time_class=rapid" | head -c 500
pkill -f "uvicorn app.main:app --port 8123"
```

Expected: non-null `data.buckets` with percentages summing to ~100, and `meta.elo_band` matching the player's own band.

- [ ] **Step 7: Commit**

```bash
git add app/baselines.py app/main.py tests/test_baselines.py
git commit -m "feat: move-time population baseline and endpoint"
```

---

## Task 6: Win-rate baselines (game length, rating differential, clock advantage)

Three charts sharing one query shape: bucket population games, count outcomes per bucket. Mirrors `crud.game_length_vs_winrate` (`app/crud.py:456`), `crud.rating_differential` (`app/crud.py:349`), and `crud.analyze_clock_advantage` (`app/crud.py:527`).

**Files:**
- Modify: `app/baselines.py`, `app/main.py`
- Test: `tests/test_baselines.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_baselines.py`:

```python
def test_game_length_baseline_computes_population_winrate(db):
    # Every seeded game is a white win at 40 moves, so the 31-40 bucket should
    # be 100% and the population's win rate should not include the target.
    seed_band(db, 1500, n_players=300, games_each=2, total_moves=40, result="1-0")
    target = make_player(db, "target")
    db.commit()

    band = baselines.resolve_band(db, target.player_id, time_class="rapid",
                                  selected_band=1500)
    result = baselines.game_length_baseline(db, target.player_id, band, time_class="rapid")

    bucket = next(b for b in result if b["bucket"] == "31–40")
    assert bucket["total_games"] > 0
    assert bucket["win_rate"] == 100.0


def test_clock_advantage_baseline_buckets_by_clock_difference(db):
    """The only test that exercises clock data, so it builds games directly
    rather than through seed_band: 40 players x 13 games = 520, clearing both
    the player and game floors. In every game the tracked player runs ~60s
    ahead and wins, so far_ahead must be the only populated bucket."""
    foil = make_player(db, "clock-foil")
    for i in range(40):
        p = make_player(db, f"clock-p{i}")
        for _ in range(13):
            make_game(
                db, p, foil, white_elo=1550, black_elo=100,
                white_clocks=[300.0, 290.0, 280.0],
                black_clocks=[240.0, 230.0, 220.0],
            )
    db.commit()

    band = baselines.resolve_band(db, foil.player_id, time_class="rapid",
                                  selected_band=1500)
    result = baselines.clock_advantage_baseline(db, foil.player_id, band,
                                                time_class="rapid")

    far_ahead = next(b for b in result if b["clock_bucket"] == "far_ahead")
    assert far_ahead["total_games"] == 520
    assert far_ahead["win_rate"] == 100.0
    assert all(b["total_games"] == 0 for b in result if b["clock_bucket"] != "far_ahead")
```

- [ ] **Step 2: Run and confirm failure**

```bash
uv run pytest tests/test_baselines.py -k "game_length or clock_advantage" -q
```

Expected: FAIL — no attribute `game_length_baseline`.

- [ ] **Step 3: Implement**

Append to `app/baselines.py`:

```python
_LENGTH_BUCKETS = [
    ("1–10", 1, 10), ("11–20", 11, 20), ("21–30", 21, 30), ("31–40", 31, 40),
    ("41–50", 41, 50), ("51–60", 51, 60), ("61–80", 61, 80), ("80+", 81, 9999),
]


def _outcome_case() -> str:
    """SQL expression giving the population player's outcome in their own game."""
    return """
        CASE
            WHEN (pop.side = 'white' AND g.result = '1-0')
              OR (pop.side = 'black' AND g.result = '0-1') THEN 'win'
            WHEN g.result = '1/2-1/2'                      THEN 'draw'
            ELSE 'loss'
        END
    """


def _tally(buckets: dict, label: str, outcome: str) -> None:
    key = {"win": "wins", "loss": "losses"}.get(outcome, "draws")
    buckets[label][key] += 1


def _rates(label_field: str, label: str, b: dict) -> dict:
    total = b["wins"] + b["losses"] + b["draws"]
    decisive = b["wins"] + b["losses"]
    return {
        label_field: label,
        "total_games": total,
        "wins": b["wins"], "losses": b["losses"], "draws": b["draws"],
        "win_rate": round(b["wins"] / total * 100, 1) if total else 0,
        "win_rate_no_draws": round(b["wins"] / decisive * 100, 1) if decisive else 0,
        "draw_rate": round(b["draws"] / total * 100, 1) if total else 0,
    }


def game_length_baseline(
    db: Session,
    player_id: int,
    band: Optional[dict],
    time_class: Optional[str] = None,
    player_color: Optional[str] = None,
    opening_names: Optional[str] = None,
) -> Optional[list[dict]]:
    """Population win rate bucketed by total game length."""
    if band is None:
        return None

    cte, params = _population_cte(
        band["elo_lo"], band["elo_hi"], player_id,
        band["time_control"], time_class, player_color, opening_names,
    )
    rows = db.execute(text(f"""
        WITH {cte}
        SELECT g.total_moves, {_outcome_case()} AS outcome
        FROM   pop JOIN games g ON g.game_id = pop.game_id
        WHERE  g.total_moves IS NOT NULL
    """), params).mappings().all()

    if not rows:
        return None

    buckets = {label: {"wins": 0, "losses": 0, "draws": 0} for label, _, _ in _LENGTH_BUCKETS}
    for row in rows:
        for label, lo, hi in _LENGTH_BUCKETS:
            if lo <= row["total_moves"] <= hi:
                _tally(buckets, label, row["outcome"])
                break

    return [_rates("bucket", label, buckets[label]) for label, _, _ in _LENGTH_BUCKETS]


_RATING_DIFF_BUCKETS = [
    ("−400+", -9999, -300), ("−300 to −200", -300, -200), ("−200 to −100", -200, -100),
    ("−100 to 0", -100, 0), ("0 to +100", 0, 100), ("+100 to +200", 100, 200),
    ("+200 to +300", 200, 300), ("+300+", 300, 9999),
]


def rating_diff_baseline(
    db: Session,
    player_id: int,
    band: Optional[dict],
    time_class: Optional[str] = None,
    player_color: Optional[str] = None,
    opening_names: Optional[str] = None,
) -> Optional[list[dict]]:
    """Population win rate bucketed by Elo advantage over the opponent."""
    if band is None:
        return None

    cte, params = _population_cte(
        band["elo_lo"], band["elo_hi"], player_id,
        band["time_control"], time_class, player_color, opening_names,
    )
    rows = db.execute(text(f"""
        WITH {cte}
        SELECT CASE WHEN pop.side = 'white'
                    THEN g.white_elo - g.black_elo
                    ELSE g.black_elo - g.white_elo END AS diff,
               {_outcome_case()} AS outcome
        FROM   pop JOIN games g ON g.game_id = pop.game_id
        WHERE  g.white_elo IS NOT NULL AND g.black_elo IS NOT NULL
    """), params).mappings().all()

    if not rows:
        return None

    buckets = {label: {"wins": 0, "losses": 0, "draws": 0} for label, _, _ in _RATING_DIFF_BUCKETS}
    for row in rows:
        for label, lo, hi in _RATING_DIFF_BUCKETS:
            if lo <= row["diff"] < hi:
                _tally(buckets, label, row["outcome"])
                break

    return [_rates("bucket", label, buckets[label]) for label, _, _ in _RATING_DIFF_BUCKETS]


_CLOCK_BUCKETS = ["far_behind", "behind", "even", "ahead", "far_ahead"]


def _clock_bucket(avg_diff: float) -> str:
    """Absolute seconds, matching the CASE in crud.analyze_clock_advantage
    (app/crud.py:577-583). These are NOT scaled by base time — the overlay must
    use identical cut points or it describes different bars than the player's."""
    if avg_diff < -30:
        return "far_behind"
    if avg_diff < -15:
        return "behind"
    if avg_diff <= 15:
        return "even"
    if avg_diff <= 30:
        return "ahead"
    return "far_ahead"


def clock_advantage_baseline(
    db: Session,
    player_id: int,
    band: Optional[dict],
    time_class: Optional[str] = None,
    player_color: Optional[str] = None,
    opening_names: Optional[str] = None,
) -> Optional[list[dict]]:
    """Population win rate bucketed by average clock difference through the game."""
    if band is None:
        return None

    cte, params = _population_cte(
        band["elo_lo"], band["elo_hi"], player_id,
        band["time_control"], time_class, player_color, opening_names,
    )
    rows = db.execute(text(f"""
        WITH {cte},
        diffs AS (
            -- Grouped by game AND side: when both players of a game fall in the
            -- band, each is a separate observation with a mirrored advantage.
            SELECT pop.game_id, pop.side,
                   AVG(mine.clock_seconds - theirs.clock_seconds) AS avg_diff
            FROM   pop
            JOIN   moves mine   ON mine.game_id   = pop.game_id AND mine.color   = pop.side
            JOIN   moves theirs ON theirs.game_id = pop.game_id
                               AND theirs.color  != pop.side
                               AND theirs.move_number = mine.move_number
            WHERE  mine.clock_seconds IS NOT NULL AND theirs.clock_seconds IS NOT NULL
            GROUP  BY pop.game_id, pop.side
        )
        SELECT d.avg_diff, {_outcome_case()} AS outcome
        FROM   diffs d
        JOIN   pop ON pop.game_id = d.game_id AND pop.side = d.side
        JOIN   games g ON g.game_id = d.game_id
    """), params).mappings().all()

    if not rows:
        return None

    buckets = {label: {"wins": 0, "losses": 0, "draws": 0} for label in _CLOCK_BUCKETS}
    for row in rows:
        _tally(buckets, _clock_bucket(float(row["avg_diff"])), row["outcome"])

    return [_rates("clock_bucket", label, buckets[label]) for label in _CLOCK_BUCKETS]
```

- [ ] **Step 4: Run the tests**

```bash
uv run pytest tests/test_baselines.py -q
```

Expected: 11 passed.

- [ ] **Step 5: Add the three endpoints**

Append to `app/main.py`. The three bodies are identical apart from the baseline function called, so factor the shared part first:

```python
def _baseline_response(db, username, fn, *, time_class, start_date, end_date,
                       player_color, opening_names, elo_band):
    player = crud.get_player(db, username)
    if not player:
        raise HTTPException(404, f"Player '{username}' not found")
    band = baselines.resolve_band(
        db, player.player_id, time_class=time_class,
        start_date=start_date, end_date=end_date,
        player_color=player_color, opening_names=opening_names,
        selected_band=elo_band,
    )
    data = fn(db, player.player_id, band, time_class, player_color, opening_names)
    if data is None:
        return {"data": None, "meta": None}
    return {"data": data, "meta": baselines.band_meta(band)}


@app.get("/api/players/{username}/analytics/game-length/baseline")
def game_length_baseline_route(
    username: str,
    time_class: Optional[str] = None,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    player_color: Optional[str] = None,
    opening_names: Optional[str] = None,
    elo_band: Optional[int] = None,
    db: Session = Depends(get_db),
):
    return _baseline_response(
        db, username, baselines.game_length_baseline,
        time_class=time_class, start_date=start_date, end_date=end_date,
        player_color=player_color, opening_names=opening_names, elo_band=elo_band)


@app.get("/api/players/{username}/analytics/rating-diff/baseline")
def rating_diff_baseline_route(
    username: str,
    time_class: Optional[str] = None,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    player_color: Optional[str] = None,
    opening_names: Optional[str] = None,
    elo_band: Optional[int] = None,
    db: Session = Depends(get_db),
):
    return _baseline_response(
        db, username, baselines.rating_diff_baseline,
        time_class=time_class, start_date=start_date, end_date=end_date,
        player_color=player_color, opening_names=opening_names, elo_band=elo_band)


@app.get("/api/players/{username}/analytics/clock-advantage/baseline")
def clock_advantage_baseline_route(
    username: str,
    time_class: Optional[str] = None,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    player_color: Optional[str] = None,
    opening_names: Optional[str] = None,
    elo_band: Optional[int] = None,
    db: Session = Depends(get_db),
):
    return _baseline_response(
        db, username, baselines.clock_advantage_baseline,
        time_class=time_class, start_date=start_date, end_date=end_date,
        player_color=player_color, opening_names=opening_names, elo_band=elo_band)
```

Then refactor `move_time_baseline_route` from Task 5 to call `_baseline_response` too, so all four share one path.

- [ ] **Step 6: Verify all four against the real database**

```bash
uv run uvicorn app.main:app --port 8123 &
sleep 3
for c in move-time game-length rating-diff clock-advantage; do
  echo "== $c"
  curl -s "http://127.0.0.1:8123/api/players/ballasack6/analytics/$c/baseline?time_class=rapid" | head -c 200
  echo
done
pkill -f "uvicorn app.main:app --port 8123"
```

Expected: all four return non-null `data` and a `meta` with the same `elo_band`.

- [ ] **Step 7: Commit**

```bash
git add app/baselines.py app/main.py tests/test_baselines.py
git commit -m "feat: win-rate population baselines for length, rating diff, clock"
```

---

## Task 7: Streak-reaction baseline

Gated per spec: only players with at least 30 games in the database can have a meaningful streak, so this will sit below the floor until the crawl lands. Build it now; it switches itself on later.

**Files:**
- Modify: `app/baselines.py`, `app/main.py`
- Test: `tests/test_baselines.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_baselines.py`:

```python
def test_streak_baseline_requires_deep_players(db):
    # 300 players with one game each: plenty of games, no streak depth.
    seed_band(db, 1500, n_players=300, games_each=2)
    target = make_player(db, "target")
    db.commit()

    band = baselines.resolve_band(db, target.player_id, time_class="rapid",
                                  selected_band=1500)
    result = baselines.streak_baseline(db, target.player_id, band, time_class="rapid")

    assert result is None
```

- [ ] **Step 2: Run and confirm failure**

```bash
uv run pytest tests/test_baselines.py::test_streak_baseline_requires_deep_players -q
```

Expected: FAIL — no attribute `streak_baseline`.

- [ ] **Step 3: Implement**

Append to `app/baselines.py`:

```python
# A streak is undefined for a player with only a handful of games in the DB.
STREAK_MIN_GAMES_PER_PLAYER = 30


def _build_streak_buckets(buckets: dict) -> list[dict]:
    """Bucket labels 1/2/3/4+, matching crud.streak_reaction's _build."""
    return [
        _rates("bucket", f"{n}" if n < 4 else "4+", buckets[n])
        for n in (1, 2, 3, 4)
    ]


def streak_baseline(
    db: Session,
    player_id: int,
    band: Optional[dict],
    time_class: Optional[str] = None,
    player_color: Optional[str] = None,
    opening_names: Optional[str] = None,
) -> Optional[dict]:
    """
    Population win rate by preceding win/loss streak.

    Mirrors crud.streak_reaction exactly: separate after_loss and after_win
    tallies, streaks reset at each new US-Eastern calendar day, and draws carry
    both streaks through unchanged rather than breaking them.

    Restricted to players with STREAK_MIN_GAMES_PER_PLAYER games in the
    database — a streak computed over two games is noise. Returns None when too
    few such players exist, which is the expected state until the opponent
    crawl has run.
    """
    if band is None:
        return None

    cte, params = _population_cte(
        band["elo_lo"], band["elo_hi"], player_id,
        band["time_control"], time_class, player_color, opening_names,
    )
    params["min_games_per_player"] = STREAK_MIN_GAMES_PER_PLAYER

    rows = db.execute(text(f"""
        WITH {cte},
        deep AS (
            SELECT pid FROM pop GROUP BY pid HAVING COUNT(*) >= :min_games_per_player
        )
        SELECT pop.pid, g.date_played, g.end_time, {_outcome_case()} AS outcome
        FROM   pop
        JOIN   deep ON deep.pid = pop.pid
        JOIN   games g ON g.game_id = pop.game_id
        ORDER  BY pop.pid, g.date_played ASC, g.end_time ASC, g.game_id ASC
    """), params).mappings().all()

    if not rows:
        return None

    eastern = ZoneInfo("America/New_York")

    def _to_date(v) -> date:
        return v if isinstance(v, date) else datetime.strptime(str(v), "%Y-%m-%d").date()

    def _local_day(row) -> Optional[date]:
        if row["end_time"] is not None:
            return datetime.fromtimestamp(row["end_time"], tz=eastern).date()
        return _to_date(row["date_played"]) if row["date_played"] else None

    loss_buckets = {n: {"wins": 0, "losses": 0, "draws": 0} for n in (1, 2, 3, 4)}
    win_buckets = {n: {"wins": 0, "losses": 0, "draws": 0} for n in (1, 2, 3, 4)}

    current_pid = None
    loss_streak = win_streak = 0
    prev_day: Optional[date] = None

    for row in rows:
        if row["pid"] != current_pid:
            current_pid = row["pid"]
            loss_streak = win_streak = 0
            prev_day = None

        day = _local_day(row)
        if day != prev_day:
            loss_streak = win_streak = 0
        prev_day = day

        outcome = row["outcome"]
        key = {"win": "wins", "loss": "losses"}.get(outcome, "draws")
        if loss_streak >= 1:
            loss_buckets[min(loss_streak, 4)][key] += 1
        if win_streak >= 1:
            win_buckets[min(win_streak, 4)][key] += 1

        if outcome == "loss":
            loss_streak += 1
            win_streak = 0
        elif outcome == "win":
            win_streak += 1
            loss_streak = 0
        # draw: both streaks carry through unchanged

    counted = sum(
        sum(b.values()) for b in list(loss_buckets.values()) + list(win_buckets.values())
    )
    if counted == 0:
        return None

    return {
        "after_loss": _build_streak_buckets(loss_buckets),
        "after_win": _build_streak_buckets(win_buckets),
    }
```

This needs two more imports at the top of `app/baselines.py`:

```python
from datetime import date, datetime
from zoneinfo import ZoneInfo
```

- [ ] **Step 4: Run the tests**

```bash
uv run pytest tests/test_baselines.py -q
```

Expected: 12 passed.

- [ ] **Step 5: Add the endpoint**

Append to `app/main.py`:

```python
@app.get("/api/players/{username}/analytics/streak-reaction/baseline")
def streak_baseline_route(
    username: str,
    time_class: Optional[str] = None,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    elo_band: Optional[int] = None,
    db: Session = Depends(get_db),
):
    return _baseline_response(
        db, username, baselines.streak_baseline,
        time_class=time_class, start_date=start_date, end_date=end_date,
        player_color=None, opening_names=None, elo_band=elo_band)
```

- [ ] **Step 6: Commit**

```bash
git add app/baselines.py app/main.py tests/test_baselines.py
git commit -m "feat: streak-reaction baseline, gated on per-player game depth"
```

---

## Task 8: Band dropdown and shared overlay plumbing

**Files:**
- Modify: `app/static/index.html:44-67`, `app/static/app.js`

- [ ] **Step 1: Add the dropdown to the filters bar**

In `app/static/index.html`, inside `<div class="filters-inner">`, after the closing `</div>` of the `date-range-group` filter group, add:

```html
            <div class="filter-group" id="baseline-group">
                <label>Compare To</label>
                <div class="baseline-picker">
                    <select id="baseline-band" onchange="onBaselineBandChange()">
                        <option value="">Your rating band</option>
                    </select>
                    <button class="baseline-toggle active" id="baseline-toggle"
                        onclick="toggleBaseline()" title="Show or hide the average-player overlay">Overlay</button>
                </div>
                <div id="baseline-notice" style="display:none;font-size:0.78rem;color:var(--text-muted);margin-top:0.25rem;"></div>
            </div>
```

- [ ] **Step 2: Add the state and helpers to `app/static/app.js`**

Near the other module-level state at the top of the file (around line 12, beside `currentFitMode`), add:

```javascript
let baselineEnabled = true;
let selectedBaselineBand = '';   // '' means "derive from the player"
let baselineBandsLoaded = false;
let lastBaselineMeta = null;   // drives the empty-state notice
```

Then add this section, placed just before the `// Filter Helpers` block:

```javascript
// ═══════════════════════════════════════════════════════════
// Population Baseline Overlays
// ═══════════════════════════════════════════════════════════

function baselineParams(color, op) {
    const ext = {};
    if (color && color !== 'global') ext.player_color = color;
    if (op) ext.opening_names = op;
    if (selectedBaselineBand) ext.elo_band = selectedBaselineBand;
    return buildFilterParamsExtra(ext);
}

/**
 * Fetch one chart's baseline. Always resolves — a failure or an empty band
 * yields null so the caller renders the player's chart alone.
 */
async function fetchBaseline(username, chart, color, op) {
    if (!baselineEnabled) return null;
    try {
        const r = await fetchJSON(
            `/api/players/${username}/analytics/${chart}/baseline${baselineParams(color, op)}`);
        const result = r && r.data ? r : null;
        lastBaselineMeta = result ? result.meta : null;
        renderBaselineNotice();
        return result;
    } catch (e) {
        console.warn(`Baseline unavailable for ${chart}:`, e);
        lastBaselineMeta = null;
        renderBaselineNotice();
        return null;
    }
}

/** Chart.js dataset styling shared by every overlay: muted, dashed, behind. */
function baselineLineStyle(extra = {}) {
    return {
        borderColor: 'rgba(148, 163, 184, 0.85)',
        backgroundColor: 'transparent',
        fill: false,
        tension: 0.3,
        pointRadius: 0,
        pointHitRadius: 20,
        borderWidth: 2,
        borderDash: [6, 4],
        order: 10,
        ...extra,
    };
}

function baselineLabel(meta) {
    if (!meta) return 'Average';
    const [lo, hi] = meta.elo_band;
    const tc = meta.tc_fallback ? (meta.time_class || 'all') : meta.time_control;
    const who = meta.source === 'selected' ? `Compared to ${lo}–${hi}` : `Average ${lo}–${hi}`;
    return `${who} · ${tc} · ${meta.n_players.toLocaleString()} players`;
}

async function loadBaselineBands(username) {
    const sel = document.getElementById('baseline-band');
    if (!sel) return;
    try {
        const r = await fetchJSON(
            `/api/players/${username}/analytics/baseline-bands${buildFilterParams()}`);
        const previous = selectedBaselineBand;
        sel.innerHTML = '<option value="">Your rating band</option>';
        for (const b of r.bands) {
            const opt = document.createElement('option');
            opt.value = b.elo_lo;
            opt.textContent = `${b.elo_lo}–${b.elo_hi}  (${b.n_players.toLocaleString()} players)`
                + (b.elo_lo === r.player_band ? '  ·  you' : '');
            sel.appendChild(opt);
        }
        // Selection is sticky across filter changes, even if the band just
        // dropped below the floor — the chart says so rather than reverting.
        sel.value = previous;
        if (sel.value !== previous) {
            const opt = document.createElement('option');
            opt.value = previous;
            opt.textContent = `${previous}–${Number(previous) + 99}  (no data here)`;
            sel.appendChild(opt);
            sel.value = previous;
        }
        baselineBandsLoaded = true;
    } catch (e) {
        console.warn('Baseline bands unavailable:', e);
    }
}

/** Explicit empty state: a selected band that has no data under the current
 *  filters must say so, rather than silently showing no line. */
function renderBaselineNotice() {
    const el = document.getElementById('baseline-notice');
    if (!el) return;
    if (baselineEnabled && selectedBaselineBand && !lastBaselineMeta) {
        const lo = Number(selectedBaselineBand);
        el.textContent = `No baseline for ${lo}–${lo + 99} under the current filters.`;
        el.style.display = '';
    } else {
        el.style.display = 'none';
    }
}

async function onBaselineBandChange() {
    selectedBaselineBand = document.getElementById('baseline-band').value;
    await refreshAll();
}

async function toggleBaseline() {
    baselineEnabled = !baselineEnabled;
    document.getElementById('baseline-toggle').classList.toggle('active', baselineEnabled);
    await refreshAll();
}
```

- [ ] **Step 3: Load the bands when the player loads**

Find `refreshAll` in `app/static/app.js` and add a call to `loadBaselineBands(currentUsername)` alongside the other per-refresh loaders, before the chart loaders run.

- [ ] **Step 4: Style the picker**

Append to `app/static/style.css`:

```css
.baseline-picker {
    display: flex;
    align-items: center;
    gap: 0.4rem;
}

.baseline-picker select {
    background: var(--bg-elevated);
    color: var(--text-primary);
    border: 1px solid var(--border-subtle);
    border-radius: 6px;
    padding: 0.35rem 0.5rem;
    font-size: 0.85rem;
    font-family: inherit;
}

.baseline-toggle {
    background: transparent;
    color: var(--text-muted);
    border: 1px solid var(--border-subtle);
    border-radius: 6px;
    padding: 0.35rem 0.6rem;
    font-size: 0.8rem;
    cursor: pointer;
}

.baseline-toggle.active {
    color: var(--text-primary);
    border-color: rgba(148, 163, 184, 0.85);
}
```

Read the existing `:root` block in `style.css` first and substitute the real variable names if `--bg-elevated` or `--border-subtle` do not exist.

- [ ] **Step 5: Verify the dropdown populates**

```bash
uv run uvicorn app.main:app --port 8123 &
sleep 3
```

Open `http://127.0.0.1:8123`, search a player, and confirm the "Compare To" dropdown lists bands with player counts and marks one `· you`. Then:

```bash
npm run lint && pkill -f "uvicorn app.main:app --port 8123"
```

Expected: eslint passes.

- [ ] **Step 6: Commit**

```bash
git add app/static/index.html app/static/app.js app/static/style.css
git commit -m "feat: baseline band dropdown and shared overlay helpers"
```

---

## Task 9: Move-time overlay, and removal of the logistic fit

The fit was a smoothing device standing in for the missing reference this feature now provides.

**Files:**
- Modify: `app/static/app.js:1479-1520` (delete), `app/static/app.js:1524-1690` (rewrite), `app/static/index.html:294`, `app/static/index.html:303`

- [ ] **Step 1: Delete `fitLogLogistic`**

Remove the entire `fitLogLogistic` function (`app/static/app.js:1479-1520`). Leave `fitLogarithmic` and `fitLinear` at `app/static/app.js:727` alone — those belong to the Elo-history projection.

- [ ] **Step 2: Remove the RMSE spans**

In `app/static/index.html`, delete line 294 (`<span id="move-time-rmse" ...>`) and line 303 (`<span id="move-time-rmse-compare" ...>`).

- [ ] **Step 3: Fetch the baseline alongside the player data**

In `loadMoveTime`, replace the opening fetch:

```javascript
        const data = await fetchJSON(`/api/players/${username}/analytics/move-time${colorParams(color, op)}`);
```

with:

```javascript
        const [data, baseline] = await Promise.all([
            fetchJSON(`/api/players/${username}/analytics/move-time${colorParams(color, op)}`),
            fetchBaseline(username, 'move-time', color, op),
        ]);
```

- [ ] **Step 4: Overlay the distribution histogram**

The player's histogram is counts and the population's is percentages, so the overlay goes on a second y-axis scaled to percent. Replace the `distDatasets` definition and the dist chart's `options.scales`:

```javascript
        const distDatasets = [{
            label: 'Moves',
            data: data.buckets.map(b => b.count),
            backgroundColor: 'rgba(111, 188, 216, 0.7)',
            borderRadius: 4,
        }];
        if (baseline) {
            distDatasets.push(baselineLineStyle({
                type: 'line',
                label: baselineLabel(baseline.meta),
                data: baseline.data.buckets.map(b => b.pct),
                yAxisID: 'yPct',
            }));
        }
```

and, in that chart's `options.scales`, add alongside `x` and `y`:

```javascript
                    yPct: {
                        display: !!baseline,
                        position: 'right',
                        grid: { display: false },
                        title: { display: true, text: '% of moves', color: '#5a6a85' },
                        ticks: { callback: v => v + '%' },
                    },
```

Set that chart's `plugins.legend` to `{ display: !!baseline, position: 'top', labels: { boxWidth: 20, font: { size: 11 } } }`.

In the same chart's tooltip callback, replace the `return` for non-zero dataset indexes with:

```javascript
                                return `${item.dataset.label}: ${item.formattedValue}% of moves`;
```

- [ ] **Step 5: Overlay the by-move-number line and drop the fit**

Replace the block that computes `fit` and builds `datasets` with:

```javascript
        const byMove = data.by_move_number;
        const datasets = [{
            label: 'Avg seconds',
            data: byMove.map(d => d.avg_seconds),
            borderColor: '#6fbcd8',
            backgroundColor: 'rgba(111, 188, 216, 0.08)',
            fill: true,
            tension: 0.3,
            pointRadius: 3,
            pointHitRadius: 20,
            borderWidth: 2,
        }];

        // Align the population curve to the player's move-number axis; the two
        // series can end at different moves.
        let baselineByMove = null;
        if (baseline) {
            const popByMove = new Map(
                baseline.data.by_move_number.map(d => [d.move_number, d.avg_seconds]));
            baselineByMove = byMove.map(d => popByMove.get(d.move_number) ?? null);
            datasets.push(baselineLineStyle({
                label: baselineLabel(baseline.meta),
                data: baselineByMove,
                spanGaps: true,
            }));
        }
```

In that chart's options, set `plugins.legend` to `{ display: !!baseline, position: 'top', labels: { boxWidth: 20, font: { size: 11 } } }`, and replace the tooltip's non-zero-index return with:

```javascript
                                return `${item.dataset.label}: ${item.formattedValue}s`;
```

- [ ] **Step 6: Delete the RMSE write and replace the peak-move card**

Delete these two lines:

```javascript
        const rmseEl = document.getElementById("move-time-rmse" + suffix);
        if (rmseEl) rmseEl.textContent = fit ? `RMSE ${fit.rmse.toFixed(2)}s` : '';
```

Then, inside the `statsEl` block, compute the population's peak from real data instead of a fitted parameter. After the existing `medianEffortMove` loop, add:

```javascript
            // Peak think move: where the curve actually peaks, for each series.
            const peakOf = (pairs) => {
                let best = null, bestVal = -Infinity;
                for (const [mn, v] of pairs) {
                    if (v !== null && v > bestVal) { bestVal = v; best = mn; }
                }
                return best;
            };
            const playerPeak = peakOf(byMove.map(d => [d.move_number, d.avg_seconds]));
            const popPeak = baselineByMove
                ? peakOf(byMove.map((d, i) => [d.move_number, baselineByMove[i]]))
                : null;
```

and replace the `${fit ? ... : ''}` template section with:

```javascript
                    <div style="padding: 0.6rem 0.75rem; border-left: 3px solid #475569;">
                        <div style="font-size: 0.78rem; color: var(--text-muted); margin-bottom: 0.15rem;">Mean effort move</div>
                        <div style="font-size: 1.1rem; font-weight: 700; color: var(--text-primary);">move ${medianEffortMove}</div>
                    </div>
                    <div style="padding: 0.6rem 0.75rem; border-left: 3px solid #475569;">
                        <div style="font-size: 0.78rem; color: var(--text-muted); margin-bottom: 0.15rem;">Peak think move</div>
                        <div style="font-size: 1.1rem; font-weight: 700; color: var(--text-primary);">move ${playerPeak}</div>
                        ${popPeak !== null ? `<div style="font-size: 0.78rem; color: var(--text-muted); margin-top: 0.15rem;">average: move ${popPeak}</div>` : ''}
                    </div>
```

- [ ] **Step 7: Verify in the browser**

```bash
uv run uvicorn app.main:app --port 8123 &
sleep 3
```

Search a player with rapid games. Confirm: the distribution chart shows a dashed grey line against a right-hand percent axis; the by-move chart shows a dashed grey population curve and no orange fit; the peak-think-move card shows both numbers; switching the dropdown to another band moves the grey lines. Check the browser console is clean, then:

```bash
npm run lint && pkill -f "uvicorn app.main:app --port 8123"
```

- [ ] **Step 8: Commit**

```bash
git add app/static/app.js app/static/index.html
git commit -m "feat: move-time population overlay, replacing the log-logistic fit"
```

---

## Task 10: Remaining chart overlays

**Files:**
- Modify: `app/static/app.js` — `loadGameLength` (~line 1295), `loadRatingDiff` (~line 1237), `loadClockAdvantage` (~line 1416), `renderStreakChart`/`loadStreakReaction` (~lines 1350, 1400)

All four are bar charts of win rate by bucket, so all four take the same treatment. Apply this pattern to each in turn, committing after each so a broken chart is easy to isolate.

- [ ] **Step 1: Game length**

In `loadGameLength`, replace the fetch with:

```javascript
        const [data, baseline] = await Promise.all([
            fetchJSON(`/api/players/${username}/analytics/game-length${colorParams(color, op)}`),
            fetchBaseline(username, 'game-length', color, op),
        ]);
```

After the existing datasets array is built, add:

```javascript
        if (baseline) {
            const popByBucket = new Map(baseline.data.map(b => [b.bucket, b.win_rate]));
            datasets.push(baselineLineStyle({
                type: 'line',
                label: baselineLabel(baseline.meta),
                data: data.map(b => popByBucket.get(b.bucket) ?? null),
                spanGaps: true,
            }));
        }
```

Set that chart's `plugins.legend.display` to `!!baseline`.

Read the function first: if its dataset array is built inline inside `new Chart({...})` rather than as a named `datasets` variable, hoist it to a `const datasets = [...]` above the call, then push.

- [ ] **Step 2: Verify and commit**

```bash
uv run uvicorn app.main:app --port 8123 &
sleep 3
```

Confirm the game-length chart shows the dashed population line. Then:

```bash
npm run lint && pkill -f "uvicorn app.main:app --port 8123"
git add app/static/app.js
git commit -m "feat: population overlay on game length vs win rate"
```

- [ ] **Step 3: Rating differential**

Same pattern in `loadRatingDiff`, with `'rating-diff'` as the chart name and `b.bucket` as the join key.

- [ ] **Step 4: Verify and commit**

```bash
npm run lint
git add app/static/app.js
git commit -m "feat: population overlay on win rate vs rating differential"
```

- [ ] **Step 5: Clock advantage**

Same pattern in `loadClockAdvantage`, with `'clock-advantage'` as the chart name and `b.clock_bucket` as the join key.

- [ ] **Step 6: Verify and commit**

```bash
npm run lint
git add app/static/app.js
git commit -m "feat: population overlay on clock advantage"
```

- [ ] **Step 7: Streak reaction**

This one differs: the payload is `{after_loss: [...], after_win: [...]}` and drives *two* charts, with buckets labelled `"1"`, `"2"`, `"3"`, `"4+"`.

In `loadStreakReaction`, fetch both in parallel as before with `'streak-reaction'` as the chart name, then pass the matching half of the baseline into each `renderStreakChart` call. Give `renderStreakChart` a new trailing parameter:

```javascript
function renderStreakChart(chartKey, canvasId, buckets, singular, plural, popBuckets = null, meta = null) {
```

and inside it, after the existing datasets array is built:

```javascript
    if (popBuckets) {
        const popByBucket = new Map(popBuckets.map(b => [b.bucket, b.win_rate]));
        datasets.push(baselineLineStyle({
            type: 'line',
            label: baselineLabel(meta),
            data: buckets.map(b => popByBucket.get(b.bucket) ?? null),
            spanGaps: true,
        }));
    }
```

Set that chart's `plugins.legend.display` to `!!popBuckets`. At the call sites pass `baseline && baseline.data.after_loss` to the after-loss chart and `baseline && baseline.data.after_win` to the after-win chart, with `baseline && baseline.meta`.

Expect no line to appear yet: the baseline returns `null` until the opponent crawl gives players enough game depth. Confirm both charts still render normally with no console error — that is the correct result for this task.

- [ ] **Step 8: Verify and commit**

```bash
npm run lint && pkill -f "uvicorn app.main:app --port 8123"
git add app/static/app.js
git commit -m "feat: population overlay on streak reaction"
```

---

## Task 11: Full verification

- [ ] **Step 1: Run the whole test suite**

```bash
uv run pytest tests/ -v
```

Expected: 12 passed.

- [ ] **Step 2: Run every linter**

```bash
uv run ruff check . && uv run mypy app etl && npm run lint
```

Expected: all clean.

- [ ] **Step 3: Confirm the existing endpoints are untouched**

```bash
uv run uvicorn app.main:app --port 8123 &
sleep 3
curl -s "http://127.0.0.1:8123/api/players/ballasack6/analytics/move-time?time_class=rapid" | head -c 200
```

Expected: the original payload shape — `buckets`, `mean`, `median`, `std_dev`, `total_moves`, `by_move_number` — with no `meta` key and no other change. This is the guarantee that motivated sibling endpoints.

- [ ] **Step 4: Check baseline latency on the real database**

```bash
for c in move-time game-length rating-diff clock-advantage; do
  echo -n "$c: "
  curl -s -o /dev/null -w "%{time_total}s\n" \
    "http://127.0.0.1:8123/api/players/ballasack6/analytics/$c/baseline?time_class=rapid"
done
pkill -f "uvicorn app.main:app --port 8123"
```

Expected: each well under 1s. `clock-advantage` is the heaviest — it self-joins `moves` — so if any exceeds ~2s, that is the one to look at first.

- [ ] **Step 5: Commit any fixes**

```bash
git add -A
git commit -m "test: verify baseline overlays end to end"
```

---

## Follow-up

The opponent crawl (spec section 2) and the WAL switch it requires are a separate plan. Until it runs:

- Rapid works across 700-1900 today.
- Blitz has gaps at 1500-1900 and 2200 — the dropdown will simply not offer them.
- Streak reaction returns `null` everywhere and renders no overlay.

A one-off seeding pass over opponents already in the database is the cheapest way to close the blitz gaps, and belongs at the front of that plan.
