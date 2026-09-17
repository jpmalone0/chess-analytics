# Style classifier v0

**Date:** 2026-09-16
**Status:** design, approved, not implemented
**Depends on:** [2026-09-16-style-vs-ability-findings.md](2026-09-16-style-vs-ability-findings.md)

---

## What this is

A panel that answers "what kind of player am I" on four measured axes, and
"which strong player do I most resemble".

## What this is not

**It does not diagnose weakness.** The four axes are stable traits nearly
orthogonal to strength — reliability 0.54–0.74, Elo correlation 0.02–0.08, joint
R² = 0.0096 against rating. A player scoring low on an axis is playing
differently from the reference, not worse. No copy in this feature may imply
otherwise, and no axis may be labelled "better" or "worse".

The weakness feature remains a separate open thread; see the findings doc.

---

## Decisions

| | decision | why |
|---|---|---|
| Output | percentile profile **plus** similarity | Similarity is nearly free once the vector exists |
| Scope | follows the existing UI filters, time class included | Consistency with every other analytics panel, at the cost of variable *n* |
| Low *n* | always render, error bars widen | The panel must not vanish when a filter narrows, and a bare point estimate reads as a fact |
| Similarity pool | **3000+ blitz rating**, compared using their **blitz** vectors | Blitz is the de facto online time control; top players barely play rapid online. 100 players, 118,609 blitz games |
| Cross-class comparison | subject's vector may be any time class; the reference is always blitz | Centring makes each vector relative to its own class's norm, so the comparison is like-for-like |
| Reference for percentile | names itself in the UI | "Percentile" unqualified invites reading it as a peer comparison the corpus cannot support |

## Axes

`space`, `mobility`, `king_safety`, `pawn_structure` — all signed so higher is
higher, never so higher is better. `passed_pawns` is excluded: reliability 0.03
means it is noise at ply 20.

All are measured from the board at **ply 20** for both colours, engine-free.

---

## Schema

Two new tables in the sidecar (`chess_engine.db`), which already holds
engine-free per-game facts alongside engine output.

```
position_features
  game_id     INTEGER  ┐ primary key
  color       TEXT     ┘ 'white' | 'black'
  space            REAL NOT NULL
  mobility         REAL NOT NULL
  king_safety      REAL NOT NULL
  pawn_structure   REAL NOT NULL
```

Keyed by game, not by run: whether a position has more space does not depend on
engine depth. Same split as `played_move_features`, and the reason the whole
203k-game corpus is reachable.

```
style_cell_means
  time_class  TEXT     ┐
  eco3        TEXT     │ primary key
  color       TEXT     ┘
  n                INTEGER NOT NULL
  space, mobility, king_safety, pawn_structure  REAL NOT NULL
```

Derived from `position_features`, rebuilt wholesale, never hand-edited. This is
the centering reference: "more space than is normal for this opening from this
side". Without it, colour alone produces effects above t=7, because White has
more space than Black and that is a fact about chess rather than about a player.

```
player_style_vectors
  player_id   INTEGER  ┐ primary key
  time_class  TEXT     ┘
  n                INTEGER NOT NULL
  space, mobility, king_safety, pawn_structure  REAL NOT NULL
```

Full-history vectors for the reference population, used for percentile ranks and
for similarity. **Asymmetry to state in the UI:** the subject's vector honours
the current filters, the reference vectors do not. Recomputing 472 GMs against
an arbitrary date window on every filter change is not affordable, and their
style is stable by construction — that is the finding this feature rests on.

### Fallback rule

A `(time_class, eco3, colour)` cell with fewer than 40 observations is not a
usable centre. Those games centre on the coarser `(time_class, colour)` mean
instead, which is always defined. The alternative — dropping the game — would
silently bias the profile toward whatever openings are popular.

---

## Computation

**Prerequisite:** `analysis/metrics.py` holds the validated metric
implementations, committed to this branch in `fcf889c`. They were written during
the investigation behind the findings doc and left out of that PR because it was
documentation only. They carry no unit tests yet, so implementation begins by
covering them, and by renaming `STYLE_METRICS` to `STYLE_AXES` so one name is
used throughout.

**Population pass** (`python -m analysis.build_features`): replay every game in
the corpus to ply 20, compute four metrics for both colours, write
`position_features`. Games that end before ply 20 or fail to replay are skipped
and counted, not guessed at. ~500 games/sec, so ~7 minutes for 203,748 games.
Idempotent by delete-then-insert per game, so a vocabulary change is a re-run.

Then rebuild `style_cell_means`, then `player_style_vectors` for every player
with ≥30 games.

**Subject vector** (per request): a SQL aggregate over `position_features`
joined to `style_cell_means`, filtered by the same clauses the other analytics
routes use. Returns per axis: mean, `n`, and standard deviation.

**Percentile:** rank the subject's axis value against that axis across
`player_style_vectors`, over **every** player with ≥30 games — not only the
elite pool — **pooled across all time controls**, with every vector expressed as
a z-score within its own class first.

> **Corrected 2026-09-17.** The original design scoped this to one time class,
> which left 37 rapid players — 2.7 percentile points apiece. Pooling looked
> unsafe because centring does not make classes comparable: on the 15 players
> holding both vectors, the same player lands +0.618 higher on mobility in rapid
> than in blitz (t=5.05), +0.277 on king safety, +0.082 on pawn structure. The
> within-player gap exceeds the between-population gap, so it is a property of
> the time control, not of who plays each one. Centring equalises the *spread*
> (within 16%) but not the *location* — the original justification checked the
> first and assumed the second.
>
> Z-scoring within class fixes it. The reference becomes 789 vectors at 0.13
> points apiece and reproduces the rapid-only answer within a few points, which
> is the validation: a correction that merely papered over the difference would
> not land back where the honest small-sample answer was.

These are deliberately two different reference sets and the response must keep
them apart. A percentile against 472 super-GMs answers a different question than
a percentile against all 695 players with deep histories, and reporting one
while labelling it the other is the failure this project has already made four
times. The similarity readout uses the 3000+ pool; the percentile uses the full one.

**Error bars:** standard error is `SD/√n`. Map `mean ± 1.96·SE` through the same
percentile function, so the bar is in the same units as the dot. At small `n` the
bar approaches the full width of the axis, which is the correct display.

**Similarity:** the pool is players with a blitz rating ≥3000 over ≥30 blitz
games — **100 players, 118,609 blitz games** — and their vectors are always their
**blitz** vectors, whatever time class the subject is viewing.

3000 rather than 2800, decided 2026-09-17: on chess.com the players actually
recognisable as super-GMs sit at or above 3000. Dropping from 2800 costs three
quarters of the players but only a quarter of the games, because the ones
removed have the shallowest histories. The floor lives in one constant
(`style.ELITE_MIN_ELO`) and every user-visible mention is derived from it, so
the label and the filter cannot drift apart.

Gating on blitz rather than per-class is deliberate. Blitz is the de facto
online time control and top players barely play rapid online; gating per class
leaves **7** usable reference players for a rapid subject, against 405 for blitz
and 107 for bullet.

Comparing a rapid subject to a blitz reference needs more than centring.
Centring makes each vector relative to its own `(time_class, ECO, colour)` norm
and equalises the spreads — max/min across classes is 1.06 for space, 1.05 for
mobility, 1.16 for king safety, 1.15 for pawn structure — but it does **not**
equalise the location. **Both sides are therefore z-scored within their own time
class before any distance is taken.** Without that the comparison is biased, and
on real data it changes two of the five nearest neighbours.

Standardise each axis **within time class** before computing distance. The
residual 5–16% scale difference above is small but free to remove, and
standardising is required regardless: `mobility` has roughly triple the raw
spread of `space`, so an unstandardised distance would be a mobility ranking
wearing a costume.

Then Euclidean distance to the subject's standardised vector; return the five
nearest.

---

## API

```
GET /api/players/{username}/analytics/style
```

Accepts the same filter parameters as the sibling analytics routes
(`time_class`, `start_date`, `end_date`, `player_color`, `opening_names`, `tz`).

```jsonc
{
  "n_games": 412,
  "percentile_reference": { "pool": "all players with >=30 games",
                            "n_players": 695, "time_class": "rapid" },
  "similarity_reference": { "pool": "3000+ blitz", "n_players": 100,
                            "vectors_from": "blitz" },
  "axes": [
    { "axis": "mobility", "value": 0.571, "percentile": 78,
      "low": 71, "high": 84 }        // 95% interval, in percentile units
  ],
  "similar": [
    { "username": "...", "distance": 0.41, "elo": 3021 }
  ]
}
```

`similar` is `[]` when the subject has too few games to place; the axes still
render with wide bars.

---

## UI

A panel in the existing analytics page, following the current panel conventions.

The panel header names **both** reference sets, since the axes and the
similarity list are ranked against different populations.

**Horizontal diverging bars, not a radar.** Four axes centred on the population
median, with a whisker for the interval. Radar charts cannot show uncertainty —
the whole point of the low-*n* decision — and their area encodes nothing
meaningful when axes have unrelated units.

Each axis is labelled neutrally (`more space` / `less space`), never
`good`/`bad`. The panel header names the reference pool and the game count, so
the two things that most change how the numbers should be read are never
implicit.

Below the bars, "closest in style among 3000+ blitz players", five names with
distances.

The reference is always blitz, so only the blitz view is like-for-like; bullet
and rapid views compare the subject's vector in that class against the pool's
blitz vectors. A tooltip on the similarity heading carries the explanation:

> Compared against players rated 3000+ in blitz, using their blitz games. Blitz
> is where strong players have the deepest online histories — in rapid only 7 of
> them have enough games to place. Each side is measured relative to what is
> normal for its own time control and opening, so the comparison holds across
> them.

Wording may change; the three things it must convey are the pool, that the
reference is blitz whatever you are viewing, and why that is still a fair
comparison. When the subject is viewing blitz the tooltip drops the middle
clause, since nothing is being crossed.

---

## Testing

- Metric level: each metric against hand-constructed FENs, including the cases
  a naive implementation gets wrong — a king on the edge whose ring runs off the
  board, a side with no pawns, a side with no king, and mobility for the colour
  *not* to move (an implementation built on `legal_moves` silently returns zero
  there, where one built on attack maps does not).
- Centering: a seeded fixture where the cell mean is known by construction, and
  a game in a below-threshold cell to prove the coarse fallback fires.
- Aggregate: a small hand-computed set where the expected axis means are
  arithmetic.
- Percentile and error bars: `n=1` produces a bar spanning nearly the whole
  axis rather than a confident point.
- Empty filter result returns an empty profile, not a divide-by-zero.
- Sign convention: a position that is unambiguously worse on an axis scores
  lower, asserted per axis, so a future refactor cannot silently flip one.

---

## Out of scope for v0

- Any claim about strength, improvement, or weakness.
- Peer comparison. The corpus has 24 players in the 1600–1999 band with ≥30
  games; a crawl is prerequisite and is tracked in the findings doc.
- Style drift over time. Interesting, and it needs the filter-scoped vector to
  be trustworthy at small *n* first.
- Multiple snapshots per game. Everything except ply 20 is currently discarded,
  so a 60-move game contributes exactly as much as a 21-move one and nothing
  from the middlegame or endgame enters. Sampling at ply 20/30/40 would roughly
  triple the data per game at almost no cost, since the replay already passes
  through those plies. Left out of v0 only because the validation work did not
  need it.
- Additional axes. The screen in the findings doc gates any new one: does it
  vary with rating, and is it stable within a player.
