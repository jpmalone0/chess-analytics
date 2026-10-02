/* Move quality: inaccuracies, mistakes, blunders and misses, as totals.
 *
 * Engine coverage is a fraction of the corpus, so the empty state is the
 * normal state and says so rather than rendering an empty table. */
/* global fetchJSON, colorParams, queryColor, currentOpeningFilter, baselineParams, currentUsername, setInterval, clearInterval */

const MQ_TIERS = ['inaccuracies', 'mistakes', 'blunders', 'misses'];

function mqPct(n, d) {
    return d ? ((100 * n) / d).toFixed(1) + '%' : '—';
}

/** The baseline and job list change while a job runs, so they bypass
 *  fetchJSON's request cache. */
async function mqFetchFresh(url, opts) {
    const resp = await fetch(url, opts);
    if (!resp.ok) throw new Error(`HTTP ${resp.status}: ${await resp.text()}`);
    return resp.json();
}

function mqBandName(band) {
    return band.source === 'all'
        ? `all ${band.time_class} players`
        : `${band.elo_lo}–${band.elo_hi} ${band.time_class}`;
}

async function loadMoveQuality(username) {
    const section = document.getElementById('move-quality-section');
    let data, base;
    try {
        [data, base] = await Promise.all([
            fetchJSON(
                `/api/players/${username}/analytics/move-quality`
                + colorParams(queryColor(), currentOpeningFilter)),
            mqFetchFresh(mqBaseUrl = (
                `/api/players/${username}/analytics/move-quality/baseline`
                + baselineParams(queryColor(), currentOpeningFilter)))
                .catch(() => null),
        ]);
    } catch {
        section.classList.add('hidden');
        return;
    }
    const t = data.totals;
    section.classList.remove('hidden');
    renderMqPopulation(base);

    const label = document.getElementById('mq-coverage-label');
    if (!t.games_analyzed) {
        label.textContent = 'No engine-analyzed games in this selection';
        document.getElementById('mq-totals').innerHTML = '';
        return;
    }
    label.textContent =
        `${t.games_analyzed} analyzed games, ${t.moves_scored.toLocaleString()} scored moves`;

    // Similar players: the Compare-to band's analyzed games. Shown whenever it
    // has any analyzed moves; its size is on the line above the cards, so a
    // thin band reads as one.
    const bandRate = base && base.totals && base.totals.moves_scored ? base.totals : null;
    // Compared per 100 moves, not per game: the two seats of a game can play
    // a different number of moves, and per game would fold that in.
    document.getElementById('mq-totals').innerHTML = MQ_TIERS.map((k) => `
        <div class="stat-card">
            <div class="stat-label">${k[0].toUpperCase() + k.slice(1)}</div>
            <div class="stat-value">${(t[k] / t.games_analyzed).toFixed(2)}</div>
            <div class="stat-sub">per game · ${mqPct(t[k], t.moves_scored)} of moves</div>
            ${bandRate ? `<div class="stat-sub mq-mirror">similar players ${mqPct(bandRate[k], bandRate.moves_scored)} of moves</div>` : ''}
        </div>`).join('');
}

// ═══════════════════════════════════════════════════════════
// Population: the Compare-to band's pooled rate, and the button
// ═══════════════════════════════════════════════════════════

let mqPollTimer = null;
let mqActiveCount = 0;
let mqBaseUrl = null;   // the baseline the section last loaded, re-polled for progress

function renderMqPopulation(base) {
    const el = document.getElementById('mq-population');
    // The button lives beside Compare To, so it reads as acting on the band
    // picked there rather than on this section.
    const slot = document.getElementById('band-analyze');
    if (!base || !base.band) { el.innerHTML = ''; slot.innerHTML = ''; return; }
    const b = base.band, t = base.totals, job = base.job;

    const have = t.n_games
        ? `${t.n_games.toLocaleString()} analyzed games from ${t.n_players.toLocaleString()} similar players`
        : 'no analyzed games from similar players yet';
    const notes = [];
    if (!base.curve_fitted) notes.push(`no ${b.time_class} curve is fitted yet, so these games will not be graded until one is`);

    let action;
    if (job) {
        const total = job.games_total ?? job.target_games;
        action = job.status === 'queued'
            ? '<span class="mq-job">Queued</span>'
            : `<span class="mq-job">Analyzing ${job.games_done}/${total}</span>`;
    } else {
        action = `<button class="baseline-toggle" onclick="startMqPopulation(this, ${base.default_games})"
            title="Analyze ${base.default_games} more games from similar players">
            Analyze ${base.default_games} more (~${Math.round(base.estimated_minutes)} min)</button>`;
    }
    el.innerHTML = `<span>${have}${notes.length ? ' · ' + notes.join(' · ') : ''}</span>`;
    slot.innerHTML = action;
    if (job) mqPoll();
}

async function startMqPopulation(btn, games) {
    btn.disabled = true;
    const q = baselineParams(queryColor(), currentOpeningFilter);
    try {
        await mqFetchFresh(
            `/api/players/${currentUsername}/analytics/move-quality/population`
            + q + (q ? '&' : '?') + `games=${games}`,
            { method: 'POST' });
    } catch (e) {
        btn.disabled = false;
        btn.textContent = 'Failed to start';
        console.warn('Population job failed to start:', e);
        return;
    }
    loadMoveQuality(currentUsername);
}

/** Poll the job list while anything is queued or running. When a job
 *  finishes, reload the section so its games reach the band rate. */
function mqPoll() {
    if (mqPollTimer) return;
    mqPollTimer = setInterval(async () => {
        let jobs;
        try { jobs = (await mqFetchFresh('/api/population/jobs')).jobs; } catch { return; }
        const active = jobs.filter((j) => j.status === 'queued' || j.status === 'running');
        renderMqJobs(active);
        const finished = active.length < mqActiveCount;
        mqActiveCount = active.length;
        if (!active.length) { clearInterval(mqPollTimer); mqPollTimer = null; }
        if (finished || !active.length) {
            loadMoveQuality(currentUsername);
        } else if (mqBaseUrl) {
            // The band line carries the running job's count; redraw it.
            try { renderMqPopulation(await mqFetchFresh(mqBaseUrl)); } catch { /* next tick */ }
        }
    }, 5000);
}

function renderMqJobs(active) {
    const el = document.getElementById('mq-jobs');
    el.innerHTML = active.length > 1 ? 'Queue: ' + active.map((j) =>
        `${mqBandName({ ...j, source: j.elo_lo === 0 && j.elo_hi === 4000 ? 'all' : 'band' })}`
        + (j.status === 'running' ? ` (${j.games_done}/${j.games_total ?? j.target_games})` : '')
    ).join(' → ') : '';
}
