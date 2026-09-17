/** The style panel.
 *
 *  A separate file from app.js, which is already 2,387 lines. Loaded as a plain
 *  script after app.js and using the same globals (fetchJSON, buildFilterParams,
 *  currentTimeClass), matching the existing convention.
 *
 *  These axes describe HOW someone plays, not how well: they are stable traits
 *  whose correlation with rating is 0.02-0.08. No label here may imply one end
 *  is better.
 */
/* global fetchJSON, buildFilterParams, escapeHtml */

const STYLE_AXIS_LABELS = {
    space: 'Space',
    mobility: 'Piece activity',
    king_safety: 'King shelter',
    pawn_structure: 'Pawn soundness',
};

let styleChart = null;

async function loadStylePanel(username) {
    try {
        const data = await fetchJSON(
            `/api/players/${username}/analytics/style${buildFilterParams()}`);
        renderStylePanel(data);
    } catch (e) {
        console.error('Error loading style profile', e);
    }
}

function renderStylePanel(data) {
    const meta = document.getElementById('style-meta');

    if (!data.axes.length) {
        meta.textContent = data.n_games
            ? 'Not enough reference data to place these games yet.'
            : 'No games match the current filters.';
        if (styleChart) { styleChart.destroy(); styleChart = null; }
        document.getElementById('style-similar').innerHTML = '';
        return;
    }

    meta.textContent =
        `${data.n_games.toLocaleString()} games · percentile against `
        + `${data.percentile_reference.n_players.toLocaleString()} players `
        + `with 30+ ${data.percentile_reference.time_class} games`;

    drawStyleChart(data.axes);
    renderStyleSimilar(data);
}

/** Horizontal bars centred on the median, with the 95% interval as an error
 *  bar. Deliberately not a radar: a radar cannot show uncertainty, which is the
 *  whole point at small sample sizes, and its area encodes nothing when the
 *  axes have unrelated units. */
function drawStyleChart(axes) {
    const ctx = document.getElementById('style-chart');
    if (styleChart) styleChart.destroy();

    styleChart = new Chart(ctx, {
        type: 'bar',
        data: {
            labels: axes.map(a => STYLE_AXIS_LABELS[a.axis] || a.axis),
            datasets: [{
                label: 'percentile',
                data: axes.map(a => a.percentile - 50),
                backgroundColor: axes.map(a =>
                    a.percentile >= 50 ? 'rgba(90, 150, 220, 0.75)'
                                       : 'rgba(200, 140, 90, 0.75)'),
                errorLow: axes.map(a => a.low - 50),
                errorHigh: axes.map(a => a.high - 50),
            }],
        },
        options: {
            indexAxis: 'y',
            scales: {
                x: {
                    min: -50, max: 50,
                    ticks: { callback: v => `${v + 50}` },
                    title: { display: true, text: 'percentile' },
                },
            },
            plugins: {
                legend: { display: false },
                tooltip: {
                    callbacks: {
                        label: ctx => {
                            const a = axes[ctx.dataIndex];
                            return `${a.percentile}th percentile `
                                 + `(${a.low}–${a.high} at 95%)`;
                        },
                    },
                },
            },
        },
        plugins: [styleErrorBars],
    });
}

/** Chart.js has no built-in error bars. Drawing them in an afterDatasetsDraw
 *  hook keeps the interval on the same scale as the bar it belongs to. */
const styleErrorBars = {
    id: 'styleErrorBars',
    afterDatasetsDraw(chart) {
        const { ctx, scales: { x, y } } = chart;
        const ds = chart.data.datasets[0];
        ctx.save();
        ctx.strokeStyle = 'rgba(70, 70, 70, 0.85)';
        ctx.lineWidth = 1.5;
        ds.data.forEach((_, i) => {
            const cy = y.getPixelForValue(i);
            const lo = x.getPixelForValue(ds.errorLow[i]);
            const hi = x.getPixelForValue(ds.errorHigh[i]);
            ctx.beginPath();
            ctx.moveTo(lo, cy); ctx.lineTo(hi, cy);
            ctx.moveTo(lo, cy - 5); ctx.lineTo(lo, cy + 5);
            ctx.moveTo(hi, cy - 5); ctx.lineTo(hi, cy + 5);
            ctx.stroke();
        });
        ctx.restore();
    },
};

function renderStyleSimilar(data) {
    const list = document.getElementById('style-similar');
    const tip = document.getElementById('style-similar-tip');
    const viewing = data.time_class || 'blitz';
    const crossing = viewing !== data.similarity_reference.vectors_from;
    // The floor lives in app/style.py; taking it from the response keeps the
    // label and the filter from drifting apart when it changes.
    const pool = data.similarity_reference.pool.replace(' blitz', '');

    const heading = document.getElementById('style-similar-heading');
    if (heading) {
        heading.firstChild.nodeValue = `Closest in style among ${pool} blitz players `;
    }

    tip.dataset.tip =
        `Compared against players rated ${pool} in blitz, using their blitz games. `
        + (crossing
            ? 'Blitz is where strong players have the deepest online histories — '
            + `in rapid only a handful have enough games to place. You are viewing `
            + `${viewing}, so this compares your ${viewing} style to their blitz style. `
            : '')
        + 'Each side is measured relative to what is normal for its own time '
        + 'control and opening, so the comparison holds across them.';

    if (!data.similar.length) {
        list.innerHTML = '<li class="panel-meta">Not enough games to place you yet.</li>';
        return;
    }
    list.innerHTML = data.similar.map(s =>
        `<li>${escapeHtml(s.username)} <span class="distance">${s.distance.toFixed(2)}</span></li>`
    ).join('');
}
