/* Scorecard: eight dimensions, you against the rating band.
 *
 * The band is a line fitted over other players' analyzed games, read at your
 * rating; your opponents are left out of it, because against the same games
 * some dimensions are mirror images of each other. The radar shows each
 * dimension as an Elo (the rating whose players typically play that way),
 * with your average rating as a dashed ring. Each Elo's 95% range (Fieller's
 * method) is in the tooltip and the table; "any" means no usable Elo yet. */
/* global fetchJSON, baselineParams, queryColor, currentOpeningFilter, currentTimeClass,
   currentUsername, requestCache, mqFetchFresh, loadMoveQuality, setInterval, clearInterval */

let scorecardChart = null;

const SC_RANGE_TIP = 'The dot is the row\'s Elo or score, the line its 95% range. The middle '
    + 'is the band at your rating for Elo rows, and 50 for scores. White means the range '
    + 'stays clear of the middle, so the difference is real; gray means it could be noise.';

// The two fixed axes. Every Elo row shares one scale and every 0-100 row the
// other, so bar lengths compare across rows.
const SC_ELO_REACH = 600;
const SC_ELO_TICKS = [-600, -300, 0, 300, 600];
const SC_SCORE_TICKS = [0, 25, 50, 75, 100];
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

/** A dot for the difference and a whisker for its range, on an axis centred
 *  on "even with your opponents". Each row has its own scale: the units differ. */
/** The row's dot and range on its group's axis, as offsets from the middle:
 *  Elo minus the comparison rating, or score minus 50. Null when the row has
 *  no value to place. */
function scBarSpan(row, rating) {
    if (row.has_elo) {
        if (row.elo === null || row.elo_lo === null) return null;
        return { dot: row.elo - rating, lo: row.elo_lo - rating, hi: row.elo_hi - rating,
            reach: SC_ELO_REACH };
    }
    if (row.score === null || row.score_lo === null) return null;
    return { dot: row.score - 50, lo: row.score_lo - 50, hi: row.score_hi - 50, reach: 50 };
}

function scRangeBar(row, rating) {
    const span = scBarSpan(row, rating);
    if (!span) return '<div class="sc-track"></div>';
    const pos = (v) => Math.max(0, Math.min(100, 50 + (50 * v) / span.reach));
    // White when the range stays clear of the middle (a real difference).
    const tone = span.lo > 0 || span.hi < 0 ? 'sc-real' : 'sc-noise';
    // A range running past the axis gets an arrow at that end: "beyond here".
    const over = `${span.lo < -span.reach ? ' sc-over-lo' : ''}${span.hi > span.reach ? ' sc-over-hi' : ''}`;
    const bare = row.unit !== 'percent';
    const tip = row.you === null || row.band === null ? ''
        : `You ${scFmt(row, row.you, false, bare)} vs band ${scFmt(row, row.band, false, bare)}: `
            + `${scFmt(row, row.diff, true)} `
            + `(${scFmt(row, row.lo, true, true)} to ${scFmt(row, row.hi, true, true)})`;
    return `<div class="sc-bar" tabindex="0" aria-label="${tip}">
        <div class="sc-track">
            <div class="sc-zero"></div>
            <div class="sc-whisker ${tone}${over}" style="left:${pos(span.lo)}%;width:${pos(span.hi) - pos(span.lo)}%"></div>
            <div class="sc-dot ${tone}" style="left:${pos(span.dot)}%"></div>
        </div>
        ${tip ? `<span class="sc-tip sc-bar-tip">${tip}</span>` : ''}
    </div>`;
}

function scRows(rows, rating) {
    return rows.map((r) => `
            <tr>
                <td class="sc-label" title="${r.description}">${r.label}${scBreakdown(r)}</td>
                <td>${scPlaysLike(r)}</td>
                <td class="sc-range">${scRangeBar(r, rating)}</td>
            </tr>`).join('');
}

/** A labelled axis row closing a group: tick values under the bar column. */
function scAxisRow(name, ticks, reach, centre) {
    const labels = ticks.map((t) => {
        const left = 50 + (50 * (t - centre)) / reach;
        const text = centre === 0 && t > 0 ? `+${t}` : String(t).replace('-', '−');
        return `<span class="sc-tick" style="left:${left}%">${text}</span>`;
    }).join('');
    return `<tr class="sc-axis-row"><td></td><td class="sc-axis-name">${name}</td>
        <td><div class="sc-axis">${labels}</div></td></tr>`;
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
    label.textContent = `${data.games} analyzed games · average rating ${data.own_avg_elo}`
        + (data.compare_source === 'selected' ? ` · compared at ${data.compare_rating}` : '')
        + ` · band from ${data.band_games} other player-games`;

    drawScorecardRadar(data.rows, data.compare_rating,
        data.compare_source === 'selected'
            ? `the band you picked (${data.compare_rating})`
            : `your average rating over these games (${data.compare_rating})`);

    document.getElementById('sc-table').innerHTML = `
        <thead><tr>
            <th></th><th>Plays like (95% range) or score</th>
            <th class="sc-range-head">worse · even · better<span class="sc-info sc-info-head" tabindex="0"
                aria-label="${SC_RANGE_TIP}">i<span class="sc-tip">${SC_RANGE_TIP}</span></span></th>
        </tr></thead>
        <tbody>${scRows(data.rows.filter((r) => r.has_elo), data.compare_rating)}
            ${scAxisRow(`Elo vs ${data.compare_rating}`, SC_ELO_TICKS, SC_ELO_REACH, 0)}
            ${scRows(data.rows.filter((r) => !r.has_elo), data.compare_rating)}
            ${scAxisRow('Score', SC_SCORE_TICKS, 50, 50)}
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
