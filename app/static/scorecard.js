/* Scorecard: eight dimensions, you against the rating band.
 *
 * The band is a line fitted over other players' analyzed games, read at your
 * rating; your opponents are left out of it, because against the same games
 * some dimensions are mirror images of each other. The radar shows each
 * dimension as an Elo (the rating whose players typically play that way),
 * with your average rating as a dashed ring. Each Elo's 95% range (Fieller's
 * method) is in the tooltip and the table; "any" means no usable Elo yet. */
/* global fetchJSON, buildFilterParams, getStartDate, getEndDate, baselineParams, queryColor, currentOpeningFilter,
   currentUsername, requestCache, mqFetchFresh, loadMoveQuality, setInterval, clearInterval, refreshBandCounts, ENGINE_RELIABLE_ELO_MAX */

let scorecardChart = null;

const SC_RANGE_TIP = 'The dot is your difference from players at your rating; the line is its '
    + '95% range. White means the range stays clear of even, so the difference is real; '
    + 'gray means it could be noise.';
let scPollTimer = null;
// Rows the server still computes but the section leaves out for now. Remove a
// key to bring its row and spoke back.
const SC_HIDDEN = new Set(['advantage', 'resourcefulness']);

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
/** The Elo (range beneath), or for a 0-100 row its score. */
function scPlaysLike(r) {
    if (!r.has_elo) {
        return r.score === null ? '—'
            : `${Math.round(r.score)}<span class="sc-muted"> / 100</span>`;
    }
    if (r.elo === null) return '—';
    return `${r.elo}<div class="sc-sub">${scEloRange(r)}</div>`;
}


/** A raw value. Units live under the row's name, so per-move values are
 *  bare; percentages keep their sign. */
function scValue(r, v) {
    return v === null ? '—' : scFmt(r, v, false, r.unit !== 'percent');
}

const SC_UNIT_NAMES = { points_per_move: 'pts/100 moves', per_move: 'per 100 moves' };

/** "any" when the range spans the whole scale: the slope could be zero, so
 *  no rating is ruled out. */
function scEloRange(row) {
    if (row.elo_lo === null) return '—';
    if (row.elo_lo <= 0 && row.elo_hi >= 3000) return 'any';
    return `${row.elo_lo}–${row.elo_hi}`;
}

/** Speed's ahead / even / behind split, under its label. */
function scBreakdown(row) {
    const b = row.breakdown;
    if (!b) return '';
    const pct = (v) => `${Math.round(100 * v)}%`;
    return `<div class="sc-sub">${pct(b.ahead)} ahead · ${pct(b.even)} even · ${pct(b.behind)} behind</div>`;
}

function scFifty(row) {
    return row.key === 'time' ? 'level with your opponents' : 'players at your rating';
}

/** A dot for the difference and a whisker for its range, on an axis centred
 *  on "even with your opponents". Each row has its own scale: the units differ. */
function scRangeBar(row) {
    if (row.diff === null) return '<div class="sc-track"></div>';
    const reach = Math.max(Math.abs(row.lo), Math.abs(row.hi), 1e-9) * 1.15;
    const pos = (v) => 50 + (50 * v) / reach;
    // White when the range excludes "even" (a real difference), gray when not.
    const tone = row.verdict === 'real' ? 'sc-real' : 'sc-noise';
    // The difference itself shows on hover. The tooltip sits outside the
    // track, which is mirrored for lower-is-better rows and would mirror it.
    // Percentages keep their % sign; other units are named once, after the gap.
    const bare = row.unit !== 'percent';
    const diff = `You ${scFmt(row, row.you, false, bare)} vs players ${scFmt(row, row.band, false, bare)}: `
        + `${scFmt(row, row.diff, true)} `
        + `(${scFmt(row, row.lo, true, true)} to ${scFmt(row, row.hi, true, true)})`;
    return `<div class="sc-bar" tabindex="0" aria-label="${diff}">
        <div class="sc-track">
            <div class="sc-zero"></div>
            <div class="sc-whisker ${tone}" style="left:${pos(row.lo)}%;width:${pos(row.hi) - pos(row.lo)}%"></div>
            <div class="sc-dot ${tone}" style="left:${pos(row.diff)}%"></div>
        </div>
        <span class="sc-tip sc-bar-tip">${diff}</span>
    </div>`;
}

/** Where a spoke's point sits. Elo spokes sit at their Elo. A 0-100 spoke is
 *  pinned so 50 (level with your opponents for speed, players at
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

function scEscape(text) {
    return String(text).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/"/g, '&quot;');
}

/** Spoke names as HTML over the canvas, each with an info icon whose tooltip
 *  says what the spoke measures. Positioned after every draw, so they follow
 *  the chart through resizes. */
const scLabelLayer = {
    id: 'scLabels',
    afterDraw(chart, _args, opts) {
        const wrap = chart.canvas.parentElement;
        let layer = wrap.querySelector('.sc-labels');
        if (!layer) {
            layer = document.createElement('div');
            layer.className = 'sc-labels';
            wrap.appendChild(layer);
        }
        const rows = opts.rows || [];
        const key = rows.map((r) => r.key).join(',');
        if (layer.dataset.key !== key) {
            layer.dataset.key = key;
            layer.innerHTML = rows.map((r) => `
                <div class="sc-spoke">
                    <span>${scEscape(r.label)}</span><span class="sc-info" tabindex="0"
                        aria-label="${scEscape(r.description)}">i<span class="sc-tip">${scEscape(r.description)}</span></span>
                    ${r.has_elo ? '' : '<div class="sc-spoke-sub">(0–100)</div>'}
                </div>`).join('');
        }
        const scale = chart.scales.r;
        [...layer.children].forEach((el, i) => {
            const p = scale.getPointLabelPosition(i);
            const x = (p.left + p.right) / 2;
            el.style.left = `${chart.canvas.offsetLeft + x}px`;
            el.style.top = `${chart.canvas.offsetTop + (p.top + p.bottom) / 2}px`;
            // Open each tooltip toward the middle so it stays on the page.
            el.dataset.side = x < scale.xCenter - 10 ? 'left' : x > scale.xCenter + 10 ? 'right' : 'middle';
        });
    },
};

function drawScorecardRadar(rows, rating, ringLabel) {
    const ctx = document.getElementById('scorecard-chart');
    if (scorecardChart) scorecardChart.destroy();
    const css = window.getComputedStyle(document.documentElement);
    const accent = css.getPropertyValue('--accent').trim() || '#3792b8';
    const muted = css.getPropertyValue('--text-muted').trim() || '#5a6a85';
    const grid = css.getPropertyValue('--border').trim() || '#2a3548';
    const text = css.getPropertyValue('--text-secondary').trim() || '#8b9ab8';

    scorecardChart = new Chart(ctx, {
        type: 'radar',
        plugins: [scLabelLayer],
        data: {
            // Two lines for the 0-100 spokes, so a long name on a side spoke
            // is not clipped by the canvas edge.
            labels: rows.map((r) => (r.has_elo ? r.label : [r.label, '(0–100)'])),
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
                    label: ringLabel,
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
                    // Transparent, not hidden: the canvas still lays out the
                    // names, and scLabelLayer draws them as HTML in the same
                    // places so each can carry an info icon.
                    pointLabels: {
                        color: 'transparent',
                        font: { size: 11 },
                    },
                },
            },
            plugins: {
                legend: { position: 'bottom', labels: { color: text, boxWidth: 12 } },
                scLabels: { rows },
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
            + baselineParams(queryColor(), currentOpeningFilter));
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
    data.rows = data.rows.filter((r) => !SC_HIDDEN.has(r.key));
    label.textContent = `${data.games} analyzed games · average rating ${data.own_avg_elo}`
        + ` · ${data.band_games} games from other players`;
    // The engine plays at roughly 2700-3000: past ENGINE_RELIABLE_ELO_MAX its
    // verdicts on these games are shown for interest, not trusted.
    if (data.own_avg_elo > ENGINE_RELIABLE_ELO_MAX) {
        label.textContent += ' · above 2800 the engine cannot reliably judge these games, so treat these numbers as rough';
    }

    drawScorecardRadar(data.rows, data.compare_rating,
        data.compare_source === 'selected'
            ? 'players'
            : `your average rating over these games (${data.compare_rating})`);

    document.getElementById('sc-table').innerHTML = `
        <thead><tr>
            <th></th><th>Plays like</th><th>You</th><th>Players</th>
            <th class="sc-range-head">worse · even · better<span class="sc-info sc-info-head" tabindex="0"
                aria-label="${SC_RANGE_TIP}">i<span class="sc-tip">${SC_RANGE_TIP}</span></span></th>
        </tr></thead>
        <tbody>${data.rows.map((r) => `
            <tr>
                <td class="sc-label" title="${r.description}">${r.label}${
    SC_UNIT_NAMES[r.unit] ? `<div class="sc-sub">${SC_UNIT_NAMES[r.unit]}</div>` : ''}${scBreakdown(r)}</td>
                <td>${scPlaysLike(r)}</td>
                <td>${scValue(r, r.you)}</td>
                <td class="sc-muted">${scValue(r, r.band)}</td>
                <td class="sc-range ${r.higher_is_better ? '' : 'sc-flip'}">${scRangeBar(r)}</td>
            </tr>`).join('')}
        </tbody>`;
}


// ═══════════════════════════════════════════════════════════
// The button: analyze more of your own games
// ═══════════════════════════════════════════════════════════

function scJobUrl(username) {
    return `/api/players/${username}/analytics/scorecard/job${buildFilterParams()}`;
}

/** Your newest games not yet analyzed, in the scorecard's time class and date
 *  range. Each press reaches further back, but never past the range: games
 *  the scorecard is not showing would cost engine time for nothing. */
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
    } else if (st.remaining_games === 0) {
        el.innerHTML = `<span>All your ${st.time_class} games${scRangeNote()} are analyzed</span>`;
    } else {
        const left = st.remaining_games ?? st.default_games;
        const n = Math.min(st.default_games, left);
        const mins = Math.max(1, Math.round(st.estimated_minutes * n / st.default_games));
        el.innerHTML = `<span>Your newest ${st.time_class} games${scRangeNote()} not yet analyzed,
            further back each press (${left.toLocaleString()} left)</span>
            <button class="btn-sm" onclick="startScAnalyze(this)">
            Analyze ${n < st.default_games ? `my last ${n}` : `${n} more of my`} games (~${mins} min)</button>`;
    }
}

/** " in this range" when a date range narrows what a press can reach. */
function scRangeNote() {
    return getStartDate() || getEndDate() ? ' in this range' : '';
}

// eslint-disable-next-line no-unused-vars -- called from the button's onclick
async function startScAnalyze(btn) {
    btn.disabled = true;
    try {
        await mqFetchFresh(`/api/players/${currentUsername}/analytics/scorecard/analyze${buildFilterParams()}`,
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
        refreshBandCounts(username);
    }, 5000);
}
