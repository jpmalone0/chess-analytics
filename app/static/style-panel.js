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

/** Username of the pro currently overlaid on the chart, or null. */
let selectedPro = null;

/** The last response, so a click can redraw without refetching. */
let lastStyleData = null;

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
    // A new filter means a new set of neighbours; carrying the old selection
    // over would overlay a player who may no longer be in the list.
    lastStyleData = data;
    selectedPro = null;

    if (!data.axes.length) {
        meta.textContent = data.n_games
            ? 'Not enough reference data to place these games yet.'
            : 'No games match the current filters.';
        if (styleChart) { styleChart.destroy(); styleChart = null; }
        document.getElementById('style-similar').innerHTML = '';
        return;
    }

    // "measured at move 10" is on the face of the panel, not only in the
    // tooltip: without it these read as a description of how you play across a
    // whole game, which is not what they are.
    meta.textContent =
        `${data.n_games.toLocaleString()} games · measured at move 10 · `
        + `percentile against ${data.percentile_reference.n_players.toLocaleString()} `
        + `players with 30+ ${data.percentile_reference.time_class} games`;

    drawStyleChart(data.axes, null);
    renderStyleSimilar(data);
}

/** Horizontal bars centred on the median, with the 95% interval as an error
 *  bar. Deliberately not a radar: a radar cannot show uncertainty, which is the
 *  whole point at small sample sizes, and its area encodes nothing when the
 *  axes have unrelated units. */
function drawStyleChart(axes, pro) {
    const ctx = document.getElementById('style-chart');
    if (styleChart) styleChart.destroy();

    const datasets = [{
        label: 'you',
        data: axes.map(a => a.percentile),
        backgroundColor: axes.map(a =>
            a.percentile >= 50 ? 'rgba(90, 150, 220, 0.75)'
                               : 'rgba(200, 140, 90, 0.75)'),
        errorLow: axes.map(a => a.low),
        errorHigh: axes.map(a => a.high),
    }];

    // The overlay carries no interval: a pro's vector is their full history,
    // not a filtered slice, so drawing whiskers on it would imply an
    // uncertainty we did not compute.
    if (pro) {
        datasets.push({
            label: pro.username,
            data: axes.map(a => pro.axes[a.axis] ?? 50),
            backgroundColor: 'rgba(214, 154, 90, 0.7)',
            errorLow: null,
            errorHigh: null,
        });
    }

    styleChart = new Chart(ctx, {
        type: 'bar',
        data: {
            labels: axes.map(a => STYLE_AXIS_LABELS[a.axis] || a.axis),
            datasets,
        },
        options: {
            indexAxis: 'y',
            // Clicking a name redraws the whole chart, and an animated redraw
            // means waiting out a transition before the two profiles can be
            // compared. The point of the overlay is the difference between the
            // bars, so they arrive already drawn.
            animation: false,
            animations: { colors: false, x: false, y: false },
            scales: {
                x: {
                    min: 0, max: 100,
                    title: { display: true, text: 'percentile' },
                },
            },
            plugins: {
                legend: { display: Boolean(pro), position: 'bottom' },
                tooltip: {
                    callbacks: {
                        label: ctx => {
                            const a = axes[ctx.dataIndex];
                            if (ctx.datasetIndex === 1) {
                                return `${ctx.dataset.label}: `
                                     + `${a ? pro.axes[a.axis] : '?'}th percentile`;
                            }
                            return `you: ${a.percentile}th percentile `
                                 + `(${a.low}–${a.high} at 95%)`;
                        },
                    },
                },
            },
        },
        plugins: [styleMedianLine, styleErrorBars],
    });
}

/** A line at the 50th percentile.
 *
 *  Bars are measured from zero, so their length reads directly as "what
 *  percentile is this". That costs the one thing a diverging chart gave for
 *  free -- you could see at a glance which side of typical a value fell on --
 *  so the median gets an explicit marker instead of being implied by the
 *  origin. Drawn before the datasets so the bars sit on top of it. */
const styleMedianLine = {
    id: 'styleMedianLine',
    beforeDatasetsDraw(chart) {
        const { ctx, scales: { x }, chartArea } = chart;
        if (!chartArea) return;
        const px = x.getPixelForValue(50);
        ctx.save();
        ctx.strokeStyle = 'rgba(150, 150, 150, 0.45)';
        ctx.lineWidth = 1;
        ctx.setLineDash([4, 4]);
        ctx.beginPath();
        ctx.moveTo(px, chartArea.top);
        ctx.lineTo(px, chartArea.bottom);
        ctx.stroke();
        ctx.restore();
    },
};

/** Chart.js has no built-in error bars. Drawing them in an afterDatasetsDraw
 *  hook keeps the interval on the same scale as the bar it belongs to.
 *
 *  The vertical position comes from the rendered bar element rather than from
 *  the category scale: once a pro is overlaid, each category holds two bars and
 *  the category centre is the gap between them, so a whisker drawn there would
 *  float between the series it is supposed to belong to. */
const styleErrorBars = {
    id: 'styleErrorBars',
    afterDatasetsDraw(chart) {
        const { ctx, scales: { x } } = chart;
        const ds = chart.data.datasets[0];
        if (!ds || !ds.errorLow || !ds.errorHigh) return;
        const meta = chart.getDatasetMeta(0);
        ctx.save();
        ctx.strokeStyle = 'rgba(70, 70, 70, 0.85)';
        ctx.lineWidth = 1.5;
        ds.data.forEach((_, i) => {
            const bar = meta.data[i];
            if (!bar || ds.errorLow[i] == null || ds.errorHigh[i] == null) return;
            const cy = bar.y;
            const cap = Math.min(5, Math.max(2, (bar.height || 10) / 3));
            const lo = x.getPixelForValue(ds.errorLow[i]);
            const hi = x.getPixelForValue(ds.errorHigh[i]);
            ctx.beginPath();
            ctx.moveTo(lo, cy); ctx.lineTo(hi, cy);
            ctx.moveTo(lo, cy - cap); ctx.lineTo(lo, cy + cap);
            ctx.moveTo(hi, cy - cap); ctx.lineTo(hi, cy + cap);
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

    // The heading stays short ("Pro Comparison"); the pool it actually uses is
    // in the tooltip, derived from the response so the two cannot disagree.
    tip.dataset.tip =
        `Compared against players rated ${pool} in blitz, using their blitz games. `
        + (crossing
            ? 'Blitz is where strong players have the deepest online histories — '
            + `in rapid only a handful have enough games to place. You are viewing `
            + `${viewing}, so this compares your ${viewing} style to their blitz style. `
            : '')
        + 'Each side is measured relative to what is normal for its own time '
        + 'control and opening, so the comparison holds across them.\n\n'
        + 'Similarity is how close your profiles are compared with two of these '
        + 'players picked at random: 100% means nothing in the pool is closer, '
        + '50% means an ordinary pairing. ★ marks players shown whatever their '
        + 'score.';

    const hint = document.getElementById('style-compare-hint');
    if (!data.similar.length) {
        list.innerHTML = '<li class="panel-meta">Not enough games to place you yet.</li>';
        if (hint) hint.textContent = '';
        return;
    }
    if (hint) {
        hint.textContent = selectedPro
            ? 'Selected — click again to clear.'
            : 'Select a player to overlay their profile.';
    }

    // Each row is a real <button> so it is keyboard-reachable and announced as
    // activatable; aria-pressed carries the selected state to a screen reader
    // rather than leaving it to the background colour alone.
    // The rank is rendered rather than left to the <ol> marker: the marker sits
    // outside the button, so it would not line up with the row it belongs to or
    // pick up the row's hover and selected states.
    // The pin slot is always rendered, empty when unpinned, so every row's
    // columns line up under the header rather than shifting by a star.
    list.innerHTML = `
        <li class="pro-head" aria-hidden="true">
          <span class="pro-rank">#</span>
          <span class="pro-name">Player</span>
          <span class="pro-pin"></span>
          <span class="pro-elo">Rating</span>
          <span class="pro-score">Similarity</span>
        </li>` + data.similar.map((s, i) => `
        <li>
          <button type="button" class="pro-row" data-username="${escapeHtml(s.username)}"
                  aria-pressed="${s.username === selectedPro}">
            <span class="pro-rank">${i + 1}</span>
            <span class="pro-name">${escapeHtml(s.username)}</span>
            <span class="pro-pin">${s.pinned ? '<span title="Always shown, whatever the score">★</span>' : ''}</span>
            <span class="pro-elo">${s.elo}</span>
            <span class="pro-score">${s.similarity}%</span>
          </button>
        </li>`).join('');

    list.querySelectorAll('.pro-row').forEach(btn => {
        btn.addEventListener('click', () => selectPro(btn.dataset.username));
    });
}

/** Toggle a pro onto the chart. Clicking the selected one clears it. */
function selectPro(username) {
    if (!lastStyleData) return;
    selectedPro = selectedPro === username ? null : username;
    const pro = lastStyleData.similar.find(s => s.username === selectedPro) || null;
    drawStyleChart(lastStyleData.axes, pro);
    renderStyleSimilar(lastStyleData);
}
