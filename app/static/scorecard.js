/* Scorecard: eight dimensions, you against the rating band.
 *
 * The band is a line fitted over other players' analyzed games, read at your
 * rating; your opponents are left out of it, because against the same games
 * some dimensions are mirror images of each other. The radar shows each
 * dimension as an Elo (the rating whose players typically play that way),
 * with your average rating as a dashed ring and a shaded 95% range from
 * Fieller's method. A range across the whole scale means no usable Elo yet. */
/* global fetchJSON, colorParams, queryColor, currentOpeningFilter */

let scorecardChart = null;

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
    return minus(sign + v.toFixed(3)) + (bare ? '' : ' lost/game');
}

/** "any" when the range spans the whole scale: the slope could be zero, so
 *  no rating is ruled out. */
function scEloRange(row) {
    if (row.elo_lo === null) return '—';
    if (row.elo_lo <= 0 && row.elo_hi >= 3000) return 'any';
    return `${row.elo_lo}–${row.elo_hi}`;
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
            labels: rows.map((r) => r.label),
            datasets: [
                {
                    label: 'you',
                    data: rows.map((r) => r.elo),
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
                // The 95% range as a band between two invisible outlines. A
                // spoke whose band runs the whole scale has no usable Elo yet.
                {
                    label: 'range-hi',
                    data: rows.map((r) => r.elo_hi),
                    borderWidth: 0,
                    pointRadius: 0,
                    backgroundColor: 'transparent',
                },
                {
                    label: '95% range',
                    data: rows.map((r) => r.elo_lo),
                    borderWidth: 0,
                    pointRadius: 0,
                    backgroundColor: accent + '1f',
                    fill: '-1',
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
                legend: {
                    position: 'bottom',
                    labels: {
                        color: text,
                        boxWidth: 12,
                        filter: (item) => item.text !== 'range-hi',
                    },
                },
                tooltip: {
                    filter: (c) => c.datasetIndex === 0,
                    callbacks: {
                        label: (c) => {
                            const r = rows[c.dataIndex];
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
            <th></th><th>Plays like (95% range)</th><th>You</th><th>Band at your average (${data.own_avg_elo})</th>
            <th class="sc-range-head">worse · even · better</th><th>Difference (95% range)</th><th></th>
        </tr></thead>
        <tbody>${data.rows.map((r) => `
            <tr>
                <td class="sc-label">${r.label}</td>
                <td>${r.elo === null ? '—' : r.elo} <span class="sc-muted">${r.elo === null ? '' : `(${scEloRange(r)})`}</span></td>
                <td>${scFmt(r, r.you)}</td>
                <td class="sc-muted">${scFmt(r, r.band)}</td>
                <td class="sc-range ${r.higher_is_better ? '' : 'sc-flip'}">${scRangeBar(r)}</td>
                <td>${scFmt(r, r.diff, true)}
                    <span class="sc-muted">${r.lo === null ? '' : `(${scFmt(r, r.lo, true, true)} to ${scFmt(r, r.hi, true, true)})`}</span></td>
                <td class="sc-verdict ${r.verdict === 'real' ? (scBetter(r) ? 'sc-good' : 'sc-bad') : 'sc-muted'}">${r.verdict || '—'}</td>
            </tr>`).join('')}
        </tbody>`;
}
