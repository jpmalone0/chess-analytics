/* Scorecard: eight dimensions, you against your opponents in the same games.
 *
 * The radar puts each dimension on a rating-anchored 0-100 scale (500 -> 30,
 * 2500 -> 80) so both shapes can be read against each other; the rows beneath
 * carry the real numbers, each difference with its 95% range. A spoke whose
 * calibration is not yet trustworthy is left blank rather than guessed. */
/* global fetchJSON, colorParams, queryColor, currentOpeningFilter */

let scorecardChart = null;

/** `bare` drops the unit, for the ends of a range printed after its value. */
function scFmt(row, v, signed, bare) {
    if (v === null || v === undefined) return '—';
    const sign = signed && v > 0 ? '+' : '';
    const minus = (s) => s.replace('-', '−');
    if (row.unit === 'percent') {
        return minus(sign + (100 * v).toFixed(1)) + (bare ? '' : signed ? ' pts' : '%');
    }
    const digits = row.unit === 'per_game' ? 2 : 3;
    const unit = row.unit === 'per_game' ? ' /game'
        : row.key === 'time' ? ' lost/game' : ' pts/game';
    return minus(sign + v.toFixed(digits)) + (bare ? '' : unit);
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

function drawScorecardRadar(rows) {
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
                    data: rows.map((r) => r.you_score),
                    borderColor: accent,
                    backgroundColor: accent + '33',
                    pointBackgroundColor: accent,
                    spanGaps: false,
                },
                {
                    label: 'opponents',
                    data: rows.map((r) => r.opp_score),
                    borderColor: muted,
                    backgroundColor: 'transparent',
                    borderDash: [4, 4],
                    pointBackgroundColor: muted,
                    spanGaps: false,
                },
            ],
        },
        options: {
            animation: false,
            scales: {
                r: {
                    min: 0, max: 100,
                    ticks: { display: false, stepSize: 20 },
                    grid: { color: grid },
                    angleLines: { color: grid },
                    pointLabels: { color: text, font: { size: 11 } },
                },
            },
            plugins: {
                legend: { position: 'bottom', labels: { color: text, boxWidth: 12 } },
                tooltip: {
                    callbacks: {
                        label: (c) => {
                            const r = rows[c.dataIndex];
                            const side = c.datasetIndex === 0 ? 'you' : 'opp';
                            const score = r[side + '_score'];
                            return score === null
                                ? `${c.dataset.label}: not calibrated yet`
                                : `${c.dataset.label}: ${Math.round(score)} (plays like ${r[side + '_rating']})`;
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
    label.textContent = `${data.games} analyzed games`
        + (data.opp_avg_elo ? ` · opponents averaged ${data.opp_avg_elo}` : '');

    drawScorecardRadar(data.rows);

    document.getElementById('sc-table').innerHTML = `
        <thead><tr>
            <th></th><th>You</th><th>Opponents</th>
            <th class="sc-range-head">worse · even · better</th><th>Difference (95% range)</th><th></th>
        </tr></thead>
        <tbody>${data.rows.map((r) => `
            <tr>
                <td class="sc-label">${r.label}</td>
                <td>${scFmt(r, r.you)}</td>
                <td class="sc-muted">${scFmt(r, r.opp)}</td>
                <td class="sc-range ${r.higher_is_better ? '' : 'sc-flip'}">${scRangeBar(r)}</td>
                <td>${scFmt(r, r.diff, true)}
                    <span class="sc-muted">${r.lo === null ? '' : `(${scFmt(r, r.lo, true, true)} to ${scFmt(r, r.hi, true, true)})`}</span></td>
                <td class="sc-verdict ${r.verdict === 'real' ? (scBetter(r) ? 'sc-good' : 'sc-bad') : 'sc-muted'}">${r.verdict || '—'}</td>
            </tr>`).join('')}
        </tbody>`;
}
