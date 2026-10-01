/* Scorecard: eight dimensions, you against the rating band.
 *
 * The band is a line fitted over other players' analyzed games, read at your
 * rating; your opponents are left out of it, because against the same games
 * some dimensions are mirror images of each other. The radar shows each
 * dimension as an Elo (the rating whose players typically play that way),
 * with your average rating as a dashed ring. Each Elo's 95% range (Fieller's
 * method) is in the tooltip and the table; "any" means no usable Elo yet. */
/* global fetchJSON, colorParams, queryColor, currentOpeningFilter, currentTimeClass,
   currentUsername, requestCache, mqFetchFresh, loadMoveQuality, setInterval, clearInterval */

let scorecardChart = null;
let scPollTimer = null;

/** `bare` drops the unit, for the ends of a range printed after its value.
 *  Per-move rows are shown per 100 moves, where the numbers are readable. */
function scFmt(row, v, signed, bare) {
    if (v === null || v === undefined) return '—';
    const sign = signed && v > 0 ? '+' : '';
    const minus = (s) => s.replace('-', '−');
    if (row.unit === 'percent') {
        return minus(sign + (100 * v).toFixed(1)) + (bare ? '' : signed ? ' pts' : '%');
    }
    if (row.unit === 'points_per_move') {
        return minus(sign + (100 * v).toFixed(2)) + (bare ? '' : ' pts/100 moves');
    }
    if (row.unit === 'per_move') {
        return minus(sign + (100 * v).toFixed(2)) + (bare ? '' : ' /100 moves');
    }
    return minus(sign + v.toFixed(3)) + (bare ? '' : ' pts/game');
}

/** The Elo with its range, or for a 0-100 row its score. */
function scPlaysLike(r) {
    if (!r.has_elo) {
        return r.score === null ? '—'
            : `${Math.round(r.score)} <span class="sc-muted">/ 100</span>`;
    }
    if (r.elo === null) return '—';
    return `${r.elo} <span class="sc-muted">(${scEloRange(r)})</span>`;
}

/** "any" when the range spans the whole scale: the slope could be zero, so
 *  no rating is ruled out. */
function scEloRange(row) {
    if (row.elo_lo === null) return '—';
    if (row.elo_lo <= 0 && row.elo_hi >= 3000) return 'any';
    return `${row.elo_lo}–${row.elo_hi}`;
}

/** Time management's ahead / even / behind split, under its label. */
function scBreakdown(row) {
    const b = row.breakdown;
    if (!b) return '';
    const pct = (v) => `${Math.round(100 * v)}%`;
    return `<div class="sc-sub">${pct(b.ahead)} ahead · ${pct(b.even)} even · ${pct(b.behind)} behind</div>`;
}

function scFifty(row) {
    return row.key === 'time' ? 'level with your opponents' : 'the band at your rating';
}

function scBetter(row) {
    return (row.diff > 0) === row.higher_is_better;
}

/** A dot for the difference and a whisker for its range, on an axis centred
 *  on "even with your opponents". Each row has its own scale: the units differ. */
function scRangeBar(row) {
    if (row.diff === null) return '<div class="sc-track"></div>';
    const reach = Math.max(Math.abs(row.lo), Math.abs(row.hi), 1e-9) * 1.15;
    const pos = (v) => 50 + (50 * v) / reach;
    const tone = row.verdict === 'real' ? (scBetter(row) ? 'sc-good' : 'sc-bad') : 'sc-noise';
    return `<div class="sc-track">
        <div class="sc-zero"></div>
        <div class="sc-whisker ${tone}" style="left:${pos(row.lo)}%;width:${pos(row.hi) - pos(row.lo)}%"></div>
        <div class="sc-dot ${tone}" style="left:${pos(row.diff)}%"></div>
    </div>`;
}

/** Where a spoke's point sits. Elo spokes sit at their Elo. A 0-100 spoke is
 *  pinned so 50 (level with your opponents for time management, the band at
 *  your rating otherwise) lands on your rating ring: below it falls inside the
 *  ring, above it reaches toward the edge at 100. */
function scRadius(row, rating) {
    if (row.has_elo) return row.elo;
    if (row.score === null) return null;
    const score = row.score;
    return score <= 50
        ? rating * (score / 50)
        : rating + (3000 - rating) * ((score - 50) / 50);
}

function drawScorecardRadar(rows, rating) {
    const ctx = document.getElementById('scorecard-chart');
    if (scorecardChart) scorecardChart.destroy();
    const css = window.getComputedStyle(document.documentElement);
    const accent = css.getPropertyValue('--accent').trim() || '#3792b8';
    const muted = css.getPropertyValue('--text-muted').trim() || '#5a6a85';
    const grid = css.getPropertyValue('--border').trim() || '#2a3548';
    const text = css.getPropertyValue('--text-secondary').trim() || '#8b9ab8';

    scorecardChart = new Chart(ctx, {
        type: 'radar',
        data: {
            labels: rows.map((r) => (r.has_elo ? r.label : `${r.label} (0–100)`)),
            datasets: [
                {
                    label: 'you',
                    data: rows.map((r) => scRadius(r, rating)),
                    borderColor: accent,
                    backgroundColor: accent + '33',
                    pointBackgroundColor: accent,
                    spanGaps: false,
                },
                {
                    label: `your average rating over these games (${rating})`,
                    data: rows.map(() => rating),
                    borderColor: muted,
                    backgroundColor: 'transparent',
                    borderDash: [4, 4],
                    pointRadius: 0,
                },
            ],
        },
        options: {
            animation: false,
            scales: {
                r: {
                    min: 0, max: 3000,
                    ticks: { stepSize: 500, color: muted, backdropColor: 'transparent', font: { size: 9 } },
                    grid: { color: grid },
                    angleLines: { color: grid },
                    pointLabels: {
                        color: text,
                        font: { size: 11 },
                    },
                },
            },
            plugins: {
                legend: { position: 'bottom', labels: { color: text, boxWidth: 12 } },
                tooltip: {
                    filter: (c) => c.datasetIndex === 0,
                    callbacks: {
                        label: (c) => {
                            const r = rows[c.dataIndex];
                            if (!r.has_elo) {
                                return r.score === null ? 'no data'
                                    : `${Math.round(r.score)} / 100 (50 = ${scFifty(r)})`;
                            }
                            if (r.elo === null) return 'no Elo';
                            return `plays like ${r.elo} (95%: ${scEloRange(r)})`;
                        },
                    },
                },
            },
        },
    });
}

async function loadScorecard(username) {
    const section = document.getElementById('scorecard-section');
    let data;
    try {
        data = await fetchJSON(
            `/api/players/${username}/analytics/scorecard`
            + colorParams(queryColor(), currentOpeningFilter));
    } catch {
        section.classList.add('hidden');
        return;
    }
    section.classList.remove('hidden');
    renderScAnalyze(username);

    const label = document.getElementById('sc-coverage-label');
    const warn = document.getElementById('sc-sample-warning');
    warn.classList.toggle('hidden', !data.small_sample || !data.games);
    warn.title = `Only ${data.games} analyzed games. Below ${data.sample_threshold}, `
        + 'most differences cannot be told apart from noise.';

    const body = document.getElementById('sc-body');
    if (!data.games) {
        label.textContent = data.curve_fitted === false && data.time_class
            ? 'No fitted curve for this time class yet'
            : 'No engine-analyzed games in this selection';
        body.classList.add('hidden');
        return;
    }
    body.classList.remove('hidden');
    label.textContent = `${data.games} analyzed games · average rating ${data.own_avg_elo}`
        + ` · band from ${data.band_games} other player-games`;

    drawScorecardRadar(data.rows, data.own_avg_elo);

    document.getElementById('sc-table').innerHTML = `
        <thead><tr>
            <th></th><th>Plays like (95% range) or score</th><th>You</th><th>Band at your average (${data.own_avg_elo})</th>
            <th class="sc-range-head">worse · even · better</th><th>Difference (95% range)</th><th></th>
        </tr></thead>
        <tbody>${data.rows.map((r) => `
            <tr>
                <td class="sc-label">${r.label}${scBreakdown(r)}</td>
                <td>${scPlaysLike(r)}</td>
                <td>${scFmt(r, r.you)}</td>
                <td class="sc-muted">${scFmt(r, r.band)}</td>
                <td class="sc-range ${r.higher_is_better ? '' : 'sc-flip'}">${scRangeBar(r)}</td>
                <td>${scFmt(r, r.diff, true)}
                    <span class="sc-muted">${r.lo === null ? '' : `(${scFmt(r, r.lo, true, true)} to ${scFmt(r, r.hi, true, true)})`}</span></td>
                <td class="sc-verdict ${r.verdict === 'real' ? (scBetter(r) ? 'sc-good' : 'sc-bad') : 'sc-muted'}">${r.verdict || '—'}</td>
            </tr>`).join('')}
        </tbody>`;
}


// ═══════════════════════════════════════════════════════════
// The button: analyze more of your own games
// ═══════════════════════════════════════════════════════════

function scJobUrl(username) {
    const q = currentTimeClass ? `?time_class=${currentTimeClass}` : '';
    return `/api/players/${username}/analytics/scorecard/job${q}`;
}

/** Your newest games not yet analyzed, in the scorecard's time class. Each
 *  press reaches further back; the date filter only decides what is shown. */
async function renderScAnalyze(username) {
    const el = document.getElementById('sc-analyze');
    let st;
    try { st = await mqFetchFresh(scJobUrl(username)); } catch { el.innerHTML = ''; return; }
    if (st.job) {
        const total = st.job.games_total ?? st.job.target_games;
        el.innerHTML = `<span>Your ${st.time_class} games</span>
            <span class="mq-job">${st.job.status === 'queued'
        ? 'Queued' : `Analyzing ${st.job.games_done}/${total}`}</span>`;
        scPoll(username);
    } else {
        el.innerHTML = `<span>Your newest ${st.time_class} games not yet analyzed, further back each press</span>
            <button class="btn-sm" onclick="startScAnalyze(this)">
            Analyze ${st.default_games} more of my games (~${Math.max(1, Math.round(st.estimated_minutes))} min)</button>`;
    }
}

// eslint-disable-next-line no-unused-vars -- called from the button's onclick
async function startScAnalyze(btn) {
    btn.disabled = true;
    const q = currentTimeClass ? `?time_class=${currentTimeClass}` : '';
    try {
        await mqFetchFresh(`/api/players/${currentUsername}/analytics/scorecard/analyze${q}`,
            { method: 'POST' });
    } catch (e) {
        btn.disabled = false;
        btn.textContent = 'Failed to start';
        console.warn('Analysis job failed to start:', e);
        return;
    }
    renderScAnalyze(currentUsername);
}

/** Poll while the job runs; when it finishes, drop the cached scorecard and
 *  move-quality responses so both sections pick up the new games. */
function scPoll(username) {
    if (scPollTimer) return;
    scPollTimer = setInterval(async () => {
        let st;
        try { st = await mqFetchFresh(scJobUrl(username)); } catch { return; }
        if (st.job) { renderScAnalyze(username); return; }
        clearInterval(scPollTimer);
        scPollTimer = null;
        for (const k of Object.keys(requestCache)) {
            if (k.includes('/scorecard') || k.includes('/move-quality')) delete requestCache[k];
        }
        loadScorecard(username);
        loadMoveQuality(username);
    }, 5000);
}
