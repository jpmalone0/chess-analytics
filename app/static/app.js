/* ═══════════════════════════════════════════════════════════
   Chess Analytics — Frontend JS
   ═══════════════════════════════════════════════════════════ */

const API = '';
let currentUsername = '';
let currentCompareUsername = '';
let compareMode = false;
let currentTimeClass = 'rapid';
let charts = {};
let projectionActive = false;
let currentFitMode = 'log';
let gamesPage = 0;
let gamesPageCompare = 0;
const GAMES_PER_PAGE = 10;
const requestCache = {};
let analyticsLoadId = 0;
let compareLoadId = 0;
let currentOpeningFilter = '';

// Chart.js defaults
Chart.defaults.color = '#8b9ab8';
Chart.defaults.borderColor = '#2a3548';
Chart.defaults.font.family = "'Inter', sans-serif";
Chart.defaults.font.size = 12;

const toMs = s => Date.parse(s);
const fmtDate = ms => new Date(ms).toISOString().slice(0, 10);
const TIME_CLASS_COLORS = { bullet: '#ef4444', blitz: '#eab308', rapid: '#22c55e' };
const DEFAULT_COLOR = '#6366f1';
const Y_AXIS_STEP = 20;
const PROJECTION_STEPS = 80;


// ═══════════════════════════════════════════════════════════
// Filter Helpers
// ═══════════════════════════════════════════════════════════

function getStartDate() { return document.getElementById('start-date').value || ''; }
function getEndDate() { return document.getElementById('end-date').value || ''; }

function buildFilterParams() {
    const parts = [];
    if (currentTimeClass) parts.push(`time_class=${currentTimeClass}`);
    if (getStartDate()) parts.push(`start_date=${getStartDate()}`);
    if (getEndDate()) parts.push(`end_date=${getEndDate()}`);
    return parts.length ? '?' + parts.join('&') : '';
}

function buildFilterParamsExtra(extras) {
    let base = buildFilterParams();
    const sep = base ? '&' : '?';
    const extraStr = Object.entries(extras).map(([k, v]) => `${k}=${v}`).join('&');
    return base + (extraStr ? sep + extraStr : '');
}

function updateDateRangeLabel() {
    const el = document.getElementById('date-range-label');
    const sd = getStartDate(), ed = getEndDate();
    el.textContent = (sd || ed) ? `${sd || '...'}  →  ${ed || '...'}` : 'All time';
}

async function resetDateRange() {
    document.getElementById('start-date').value = '';
    document.getElementById('end-date').value = '';
    if (currentUsername) {
        await ensureSynced(currentUsername);
        await refreshAll();
    }
}


// ═══════════════════════════════════════════════════════════
// Sync Banner
// ═══════════════════════════════════════════════════════════

function showSyncBanner(msg) {
    const banner = document.getElementById('sync-banner');
    document.getElementById('sync-message').textContent = msg;
    banner.classList.remove('hidden');
}

function hideSyncBanner() {
    document.getElementById('sync-banner').classList.add('hidden');
}


// ═══════════════════════════════════════════════════════════
// Initialization
// ═══════════════════════════════════════════════════════════

document.addEventListener('DOMContentLoaded', () => {
    // Default to last 30 days
    const now = new Date();
    const today = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}-${String(now.getDate()).padStart(2, '0')}`;
    const monthAgo = new Date(now);
    monthAgo.setMonth(monthAgo.getMonth() - 1);
    const startDefault = `${monthAgo.getFullYear()}-${String(monthAgo.getMonth() + 1).padStart(2, '0')}-${String(monthAgo.getDate()).padStart(2, '0')}`;
    document.getElementById('start-date').value = startDefault;
    document.getElementById('end-date').value = today;

    document.querySelectorAll('.tc-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            document.querySelectorAll('.tc-btn').forEach(b => b.classList.remove('active'));
            btn.classList.add('active');
            currentTimeClass = btn.dataset.tc;
            if (currentUsername) refreshAll();
        });
    });

    const input = document.getElementById('player-search');

    input.addEventListener('keydown', e => {
        if (e.key === 'Enter') { hideRecentDropdown(); loadPlayer(); }
        if (e.key === 'Escape') hideRecentDropdown();
    });

    input.addEventListener('focus', () => renderRecentDropdown());
    input.addEventListener('input', () => renderRecentDropdown());

    document.addEventListener('click', e => {
        if (!e.target.closest('.search-wrap')) hideRecentDropdown();
    });

    const compareInput = document.getElementById('compare-search');
    compareInput.addEventListener('keydown', e => {
        if (e.key === 'Enter') { hideCompareRecentDropdown(); loadComparePlayer(); }
        if (e.key === 'Escape') hideCompareRecentDropdown();
    });
    compareInput.addEventListener('focus', () => renderCompareRecentDropdown());
    compareInput.addEventListener('input', () => renderCompareRecentDropdown());

    document.addEventListener('click', e => {
        if (!e.target.closest('#compare-search-wrap .search-wrap')) hideCompareRecentDropdown();
    });


    // Register main perspective tab listeners once (static DOM elements)
    document.querySelectorAll('#main-perspective-tabs .tab-btn').forEach(btn => {
        btn.addEventListener('click', (e) => {
            document.querySelectorAll('#main-perspective-tabs .tab-btn').forEach(b => b.classList.remove('active'));
            e.target.classList.add('active');
            const targetId = e.target.dataset.target;
            document.getElementById('white-tabs-container').classList.add('hidden');
            document.getElementById('black-tabs-container').classList.add('hidden');
            if (targetId !== 'global') {
                document.getElementById(targetId + '-tabs-container').classList.remove('hidden');
                const activeSub = document.querySelector(`#${targetId}-tabs .tab-btn.active`);
                const op = activeSub ? activeSub.dataset.op : "";
                currentOpeningFilter = op;
                loadColorAnalytics(currentUsername, targetId, op);
            } else {
                currentOpeningFilter = '';
                loadColorAnalytics(currentUsername, 'global', "");
            }
            gamesPage = 0;
            loadGames(currentUsername);
            if (compareMode && currentCompareUsername) loadGames(currentCompareUsername, '-compare');
        });
    });

    document.querySelectorAll('.chart-title .stat-info-btn').forEach(btn => {
        btn.addEventListener('click', (e) => {
            e.stopPropagation();
            const existing = btn.querySelector('.stat-info-desc');
            if (existing) { existing.remove(); return; }
            const desc = document.createElement('div');
            desc.className = 'stat-info-desc';
            desc.textContent = btn.dataset.desc;
            btn.appendChild(desc);
            const close = () => { desc.remove(); document.removeEventListener('click', close); };
            document.addEventListener('click', close);
        });
    });
});


// ═══════════════════════════════════════════════════════════
// Recent Searches
// ═══════════════════════════════════════════════════════════

const RECENT_KEY = 'chess_recent_searches';
const RECENT_MAX = 8;

function getRecentSearches() {
    try { return JSON.parse(localStorage.getItem(RECENT_KEY)) || []; }
    catch { return []; }
}

function saveRecentSearch(username) {
    const recent = getRecentSearches().filter(u => u !== username);
    recent.unshift(username);
    localStorage.setItem(RECENT_KEY, JSON.stringify(recent.slice(0, RECENT_MAX)));
}

function removeRecentSearch(username) {
    const recent = getRecentSearches().filter(u => u !== username);
    localStorage.setItem(RECENT_KEY, JSON.stringify(recent));
}

function buildRecentItems(recent) {
    return recent.map(u => `
        <li data-username="${u}">
            <span class="recent-name">${u}</span>
            <button class="recent-remove" title="Remove" aria-label="Remove ${u}">×</button>
        </li>`).join('');
}

function renderRecentDropdown() {
    const input = document.getElementById('player-search');
    const dropdown = document.getElementById('recent-searches');
    const query = input.value.trim().toLowerCase();
    const recent = getRecentSearches().filter(u => !query || u.includes(query));
    if (recent.length === 0) { hideRecentDropdown(); return; }
    dropdown.innerHTML = buildRecentItems(recent);
    dropdown.querySelectorAll('li').forEach(li => {
        li.querySelector('.recent-name').addEventListener('click', () => {
            input.value = li.dataset.username;
            hideRecentDropdown();
            loadPlayer();
        });
        li.querySelector('.recent-remove').addEventListener('click', (e) => {
            e.stopPropagation();
            removeRecentSearch(li.dataset.username);
            renderRecentDropdown();
        });
    });
    dropdown.classList.remove('hidden');
}

function hideRecentDropdown() {
    document.getElementById('recent-searches').classList.add('hidden');
}

function renderCompareRecentDropdown() {
    const input = document.getElementById('compare-search');
    const dropdown = document.getElementById('recent-searches-compare');
    const query = input.value.trim().toLowerCase();
    const recent = getRecentSearches().filter(u => !query || u.includes(query));
    if (recent.length === 0) { hideCompareRecentDropdown(); return; }
    dropdown.innerHTML = buildRecentItems(recent);
    dropdown.querySelectorAll('li').forEach(li => {
        li.querySelector('.recent-name').addEventListener('click', () => {
            input.value = li.dataset.username;
            hideCompareRecentDropdown();
            loadComparePlayer();
        });
        li.querySelector('.recent-remove').addEventListener('click', (e) => {
            e.stopPropagation();
            removeRecentSearch(li.dataset.username);
            renderCompareRecentDropdown();
        });
    });
    dropdown.classList.remove('hidden');
}

function hideCompareRecentDropdown() {
    document.getElementById('recent-searches-compare').classList.add('hidden');
}


// ═══════════════════════════════════════════════════════════
// Player Loading (always sync from chess.com)
// ═══════════════════════════════════════════════════════════

let syncedRanges = {};

async function ensureSynced(username) {
    const sd = getStartDate();
    const ed = getEndDate();
    const range = syncedRanges[username];
    let needsSync = !range;

    if (range) {
        if (sd === '') { if (range.earliest !== '') needsSync = true; }
        else if (range.earliest !== '' && sd < range.earliest) needsSync = true;

        if (ed === '') { if (range.latest !== '') needsSync = true; }
        else if (range.latest !== '' && ed > range.latest) needsSync = true;
    }

    if (needsSync) {
        await syncPlayerFromChessCom(username);
        if (!syncedRanges[username]) {
            syncedRanges[username] = { earliest: sd, latest: ed };
        } else {
            if (sd === '' || (syncedRanges[username].earliest !== '' && sd < syncedRanges[username].earliest)) {
                syncedRanges[username].earliest = sd;
            }
            if (ed === '' || (syncedRanges[username].latest !== '' && ed > syncedRanges[username].latest)) {
                syncedRanges[username].latest = ed;
            }
        }
    }
}

async function loadPlayer() {
    const input = document.getElementById('player-search');
    const username = input.value.trim().toLowerCase();
    if (!username) return;
    saveRecentSearch(username);

    currentUsername = username;
    currentTimeClass = 'rapid';
    gamesPage = 0;
    currentOpeningFilter = '';
    document.querySelectorAll('.tc-btn').forEach(b => b.classList.remove('active'));
    document.querySelector('.tc-btn[data-tc="rapid"]').classList.add('active');

    // Reset perspective tabs to Overall so initRepertoireTabs loads global data
    document.querySelectorAll('#main-perspective-tabs .tab-btn').forEach(b => b.classList.remove('active'));
    document.querySelector('#main-perspective-tabs .tab-btn[data-target="global"]').classList.add('active');
    document.getElementById('white-tabs-container').classList.add('hidden');
    document.getElementById('black-tabs-container').classList.add('hidden');

    await ensureSynced(username);
    await refreshAll();
}


async function syncPlayerFromChessCom(username) {
    showSyncBanner(`Pulling games for "${username}" from chess.com...`);

    const sd = getStartDate(), ed = getEndDate();
    const params = [];
    if (sd) params.push(`start_date=${sd}`);
    if (ed) params.push(`end_date=${ed}`);
    const qs = params.length ? '?' + params.join('&') : '';

    try {
        const result = await fetchJSON(`/api/players/${username}/sync${qs}`, { method: 'POST' });
        showSyncBanner(`✓ ${result.message}`);
        setTimeout(hideSyncBanner, 4000);
    } catch (e) {
        showSyncBanner(`✗ Failed to sync: ${e.message}`);
        setTimeout(hideSyncBanner, 5000);
    }
}


function syncYAxes(keyA, keyB) {
    const c1 = charts[keyA];
    const c2 = charts[keyB];
    if (!c1 || !c2) return;
    let lo = Infinity, hi = -Infinity;
    for (const c of [c1, c2]) {
        for (const ds of c.data.datasets) {
            for (const p of ds.data) {
                if (p.y != null && isFinite(p.y)) {
                    if (p.y < lo) lo = p.y;
                    if (p.y > hi) hi = p.y;
                }
            }
        }
    }
    if (!isFinite(lo)) return;
    const yMin = Math.floor(lo / Y_AXIS_STEP) * Y_AXIS_STEP;
    const yMax = Math.ceil(hi / Y_AXIS_STEP) * Y_AXIS_STEP;
    for (const c of [c1, c2]) {
        c.options.scales.y.min = yMin;
        c.options.scales.y.max = yMax;
        c.update();
    }
}

async function refreshAll() {
    Object.keys(requestCache).forEach(k => delete requestCache[k]);

    updateDateRangeLabel();
    gamesPage = 0;
    document.getElementById('layer-1').classList.remove('hidden');
    document.querySelectorAll('.section').forEach(s => s.classList.remove('hidden'));

    const promises = [
        loadStats(currentUsername),
        loadEloChart(currentUsername),
        loadGames(currentUsername),
        initRepertoireTabs(currentUsername),
    ];
    if (compareMode && currentCompareUsername) {
        promises.push(loadCompareStats(currentCompareUsername));
        promises.push(loadEloChart(currentCompareUsername, '-compare'));
    }
    await Promise.all(promises);
    if (compareMode && currentCompareUsername) syncYAxes('elo', 'elo-compare');
}


// ═══════════════════════════════════════════════════════════
// Compare Mode
// ═══════════════════════════════════════════════════════════

function toggleCompare() {
    if (compareMode) {
        exitCompareMode();
    } else {
        document.getElementById('compare-search-wrap').classList.remove('hidden');
        document.getElementById('compare-toggle-btn').classList.add('active');
        document.getElementById('compare-search').focus();
    }
}

async function loadComparePlayer() {
    const username = document.getElementById('compare-search').value.trim().toLowerCase();
    if (!username || username === currentUsername) return;

    currentCompareUsername = username;
    compareMode = true;
    document.body.classList.add('compare-mode');
    saveRecentSearch(username);
    hideCompareRecentDropdown();

    document.getElementById('primary-stat-label').textContent = currentUsername;
    document.getElementById('compare-stat-label').textContent = username;
    document.getElementById('analytics-primary-label').textContent = currentUsername;
    document.getElementById('analytics-compare-label').textContent = username;
    document.getElementById('elo-primary-label').textContent = currentUsername;
    document.getElementById('elo-compare-label').textContent = username;
    document.getElementById('games-primary-label').textContent = currentUsername;
    document.getElementById('games-compare-label').textContent = username;

    gamesPageCompare = 0;

    await ensureSynced(username);

    const activeTab = document.querySelector('#main-perspective-tabs .tab-btn.active');
    const targetId = activeTab?.dataset.target || 'global';
    const color = targetId === 'global' ? 'global' : targetId;
    const activeSub = targetId !== 'global' ? document.querySelector(`#${targetId}-tabs .tab-btn.active`) : null;
    const op = activeSub?.dataset.op || '';
    loadColorAnalytics(currentUsername, color, op);

    const compareTasks = [
        loadCompareStats(username),
        loadEloChart(username, '-compare'),
    ];
    await Promise.all(compareTasks);
    syncYAxes('elo', 'elo-compare');
    loadGames(username, '-compare');
}

function exitCompareMode() {
    compareMode = false;
    currentCompareUsername = '';
    document.body.classList.remove('compare-mode');
    document.getElementById('compare-search-wrap').classList.add('hidden');
    document.getElementById('compare-toggle-btn').classList.remove('active');
    document.getElementById('compare-search').value = '';
    document.getElementById('compare-stats-grid').innerHTML = '';

    const compareKeys = ['loadGameLength', 'loadClockAdvantage', 'loadRatingDiff', 'loadMoveTimeDist', 'loadMoveTimeByMove', 'elo'];
    compareKeys.forEach(k => {
        const key = k + '-compare';
        if (charts[key]) { charts[key].destroy(); delete charts[key]; }
    });

    gamesPageCompare = 0;
    const compareTbody = document.getElementById('games-tbody-compare');
    if (compareTbody) compareTbody.innerHTML = '';
}

async function loadCompareStats(username) {
    try {
        const data = await fetchJSON(`/api/players/${username}/stats${buildFilterParams()}`);
        let stats = data;

        if (currentTimeClass && data.by_time_class[currentTimeClass]) {
            const tc = data.by_time_class[currentTimeClass];
            const decisive = tc.wins + tc.losses;
            stats = {
                total_games: tc.total, wins: tc.wins, losses: tc.losses, draws: tc.draws,
                total_moves: tc.total_moves || 0,
                win_rate: tc.total ? (tc.wins / tc.total * 100) : 0,
                decisive_win_rate: decisive ? (tc.wins / decisive * 100) : 0,
                draw_rate: tc.total ? (tc.draws / tc.total * 100) : 0,
            };
        }

        document.getElementById('compare-stats-grid').innerHTML = `
            <div class="stat-card"><div class="stat-label">Total Games</div><div class="stat-value">${stats.total_games.toLocaleString()}</div></div>
            <div class="stat-card win"><div class="stat-label">Wins</div><div class="stat-value">${stats.wins.toLocaleString()}</div></div>
            <div class="stat-card draw"><div class="stat-label">Draws</div><div class="stat-value">${stats.draws.toLocaleString()}</div></div>
            <div class="stat-card loss"><div class="stat-label">Losses</div><div class="stat-value">${stats.losses.toLocaleString()}</div></div>
            <div class="stat-card"><div class="stat-label">Total Moves</div><div class="stat-value">${(stats.total_moves || 0).toLocaleString()}</div></div>
            <div class="stat-card accent"><div class="stat-label">Decisive Win Rate</div><div class="stat-value">${(stats.decisive_win_rate ?? 0).toFixed(1)}%</div></div>
            <div class="stat-card draw"><div class="stat-label">Draw Rate</div><div class="stat-value">${(stats.draw_rate ?? 0).toFixed(1)}%</div></div>
            <div class="stat-card accent"><div class="stat-label">Win Rate</div><div class="stat-value">${stats.win_rate.toFixed(1)}%</div></div>
        `;
    } catch (e) { console.error('Compare stats error:', e.message, e); }
}


// ═══════════════════════════════════════════════════════════
// Player Stats
// ═══════════════════════════════════════════════════════════

async function loadStats(username) {
    try {
        const data = await fetchJSON(`/api/players/${username}/stats${buildFilterParams()}`);
        let stats = data;

        if (currentTimeClass && data.by_time_class[currentTimeClass]) {
            const tc = data.by_time_class[currentTimeClass];
            const decisive = tc.wins + tc.losses;
            stats = {
                total_games: tc.total, wins: tc.wins, losses: tc.losses, draws: tc.draws,
                total_moves: tc.total_moves || 0,
                win_rate: tc.total ? (tc.wins / tc.total * 100) : 0,
                decisive_win_rate: decisive ? (tc.wins / decisive * 100) : 0,
                draw_rate: tc.total ? (tc.draws / tc.total * 100) : 0,
            };
        }

        document.getElementById('val-total').textContent = stats.total_games.toLocaleString();
        document.getElementById('val-total-moves').textContent = (stats.total_moves || 0).toLocaleString();
        document.getElementById('val-wins').textContent = stats.wins.toLocaleString();
        document.getElementById('val-losses').textContent = stats.losses.toLocaleString();
        document.getElementById('val-draws').textContent = stats.draws.toLocaleString();
        document.getElementById('val-winrate').textContent = stats.win_rate.toFixed(1) + '%';
        document.getElementById('val-decisive').textContent = (stats.decisive_win_rate ?? 0).toFixed(1) + '%';
        document.getElementById('val-drawrate').textContent = (stats.draw_rate ?? 0).toFixed(1) + '%';
    } catch (e) { console.error('Stats error:', e.message, e); }
}


// ═══════════════════════════════════════════════════════════
// Elo Chart
// ═══════════════════════════════════════════════════════════

async function loadEloChart(username, suffix = '', animate = false) {
    const chartKey = 'elo' + suffix;
    try {
        const data = await fetchJSON(`/api/players/${username}/analytics/elo-history${buildFilterParams()}`);
        if (charts[chartKey]) charts[chartKey].destroy();

        // Build per-time-class point groups
        const groups = {};
        if (currentTimeClass) {
            groups[currentTimeClass] = data.map(d => ({ x: toMs(d.date), y: d.elo }));
        } else {
            data.forEach(d => {
                const tc = d.time_class || 'unknown';
                if (tc === 'unknown' || tc === 'daily') return;
                if (!groups[tc]) groups[tc] = [];
                groups[tc].push({ x: toMs(d.date), y: d.elo });
            });
        }

        const allMs = Object.values(groups).flatMap(pts => pts.map(p => p.x));
        const dataXMin = Math.min(...allMs);
        const actualXMax = Math.max(...allMs);
        let xMax = actualXMax;

        // Honor the selected start date so the axis begins there even if data starts later
        const selectedStart = getStartDate() ? Date.parse(getStartDate()) : null;
        const xMin = (selectedStart && selectedStart < dataXMin) ? selectedStart : dataXMin;

        const datasets = [];
        for (const [tc, points] of Object.entries(groups)) {
            const color = TIME_CLASS_COLORS[tc] || DEFAULT_COLOR;
            const label = tc.charAt(0).toUpperCase() + tc.slice(1);
            const firstPt = points[0];
            const lastPt = points[points.length - 1];
            // Extend flat to shared x bounds so all lines span the full date range
            const displayPts = [
                ...(firstPt.x > xMin ? [{ x: xMin, y: firstPt.y }] : []),
                ...points,
                ...(lastPt.x < actualXMax ? [{ x: actualXMax, y: lastPt.y }] : []),
            ];
            datasets.push({
                label,
                data: displayPts,
                borderColor: color,
                backgroundColor: hexToRgba(color, currentTimeClass ? 0.08 : 0),
                fill: !!currentTimeClass, tension: 0, pointRadius: 0, pointHitRadius: 20, borderWidth: 2,
                spanGaps: true
            });
        }

        const chartPlugins = [];
        if (projectionActive) {
            // All projections start from actualXMax and extend by the longest tc history
            for (const [, points] of Object.entries(groups)) {
                if (points.length < 2) continue;
                const xs = points.map(p => p.x);
                const span = Math.max(...xs) - Math.min(...xs) || 1;
                if (actualXMax + span > xMax) xMax = actualXMax + span;
            }
            const globalProjEnd = xMax;

            const sseparts = [];
            for (const [tc, points] of Object.entries(groups)) {
                if (points.length < 2) continue;
                const xs = points.map(p => p.x);
                const tcXMin = Math.min(...xs);

                const fitLog = fitLogarithmic(points);
                const fitLin = fitLinear(points);
                const fit = currentFitMode === 'log' ? fitLog : fitLin;
                if (!fit) continue;

                if (fitLog && fitLin) {
                    const label = tc.charAt(0).toUpperCase() + tc.slice(1);
                    sseparts.push(`${label}  log ${fitLog.rmse.toFixed(1)}  lin ${fitLin.rmse.toFixed(1)} Elo`);
                }

                const fitColor = hexToRgba(TIME_CLASS_COLORS[tc] || DEFAULT_COLOR, 0.9);

                const fitFirstY = fit.predict(tcXMin);
                const fitData = Array.from({ length: PROJECTION_STEPS + 1 }, (_, i) => {
                    const ms = tcXMin + (actualXMax - tcXMin) * i / PROJECTION_STEPS;
                    return { x: ms, y: fit.predict(ms) };
                });
                if (tcXMin > xMin) fitData.unshift({ x: xMin, y: fitFirstY });

                datasets.push({
                    label: `${tc} fit`,
                    data: fitData,
                    borderColor: fitColor, backgroundColor: 'transparent',
                    fill: false, tension: 0.3, pointRadius: 0, pointHitRadius: 20, borderWidth: 1.5,
                    borderDash: [6, 4], spanGaps: true, hidden: false,
                });
                datasets.push({
                    label: `${tc} projection`,
                    data: Array.from({ length: PROJECTION_STEPS + 1 }, (_, i) => {
                        const ms = actualXMax + (globalProjEnd - actualXMax) * i / PROJECTION_STEPS;
                        return { x: ms, y: fit.predict(ms) };
                    }),
                    borderColor: fitColor, backgroundColor: 'transparent',
                    fill: false, tension: 0.3, pointRadius: 0, pointHitRadius: 20, borderWidth: 1.5,
                    borderDash: [6, 4], spanGaps: true,
                });
            }

            const sseEl = document.getElementById('projection-sse' + suffix);
            if (sseEl) sseEl.textContent = sseparts.join('\n');
            const sseDetails = document.getElementById('projection-sse-details' + suffix);
            if (sseDetails) sseDetails.classList.toggle('hidden', suffix === '-compare' && !compareMode);

            const capturedActualXMax = actualXMax;
            chartPlugins.push({
                id: 'todayLine',
                afterDraw(chart) {
                    const xScale = chart.scales.x;
                    const x = xScale.getPixelForValue(capturedActualXMax);
                    if (x < chart.chartArea.left || x > chart.chartArea.right) return;
                    const ctx = chart.ctx;
                    ctx.save();
                    ctx.beginPath();
                    ctx.moveTo(x, chart.chartArea.top);
                    ctx.lineTo(x, chart.chartArea.bottom);
                    ctx.strokeStyle = 'rgba(245, 158, 11, 0.5)';
                    ctx.lineWidth = 1;
                    ctx.setLineDash([4, 4]);
                    ctx.stroke();
                    ctx.setLineDash([]);
                    ctx.restore();
                }
            });
        }

        charts[chartKey] = new Chart(document.getElementById('elo-chart' + suffix).getContext('2d'), {
            type: 'line',
            data: { datasets },
            options: {
                responsive: true, maintainAspectRatio: false,
                animation: false,
                plugins: {
                    legend: {
                        display: !currentTimeClass || projectionActive,
                        position: 'top',
                        labels: { filter: item => !item.text.endsWith(' fit') && !item.text.endsWith(' projection') }
                    },
                    tooltip: {
                        callbacks: {
                            title: items => items.length ? fmtDate(items[0].parsed.x) : ''
                        }
                    }
                },
                scales: {
                    x: {
                        type: 'linear', min: xMin, max: xMax,
                        ticks: { maxTicksLimit: 10, maxRotation: 0, callback: v => fmtDate(v) },
                        grid: { display: false }
                    },
                    y: { grid: { color: 'rgba(42, 53, 72, 0.5)' } },
                }
            },
            plugins: chartPlugins,
        });
    } catch (e) { console.error('Elo chart error:', e); }
}


// ═══════════════════════════════════════════════════════════
// Projected Rating Chart
// ═══════════════════════════════════════════════════════════

function hexToRgba(hex, a) {
    const r = parseInt(hex.slice(1, 3), 16);
    const g = parseInt(hex.slice(3, 5), 16);
    const b = parseInt(hex.slice(5, 7), 16);
    return `rgba(${r}, ${g}, ${b}, ${a})`;
}

function ols(points, transform) {
    if (points.length < 2) return null;
    const xs = points.map(p => p.x);
    const xMin = Math.min(...xs), xMax = Math.max(...xs);
    const range = xMax - xMin || 1;
    const ts = xs.map(x => (x - xMin) / range);
    const zs = ts.map(transform);
    const ys = points.map(p => p.y);
    const n = points.length;
    const zMean = zs.reduce((a, b) => a + b, 0) / n;
    const yMean = ys.reduce((a, b) => a + b, 0) / n;
    let num = 0, den = 0;
    for (let i = 0; i < n; i++) {
        num += (zs[i] - zMean) * (ys[i] - yMean);
        den += (zs[i] - zMean) ** 2;
    }
    const slope = den === 0 ? 0 : num / den;
    const intercept = yMean - slope * zMean;
    const predict = ms => intercept + slope * transform((ms - xMin) / range);
    const rmse = Math.sqrt(ys.reduce((s, y, i) => s + (y - predict(xs[i])) ** 2, 0) / n);
    return { predict, rmse, xMin, range };
}

const fitLogarithmic = points => ols(points, t => Math.log(1 + t));
const fitLinear      = points => ols(points, t => t);

function toggleProjection() {
    projectionActive = !projectionActive;
    const btn = document.getElementById('projection-toggle-btn');
    const controls = document.getElementById('projection-controls');
    const ssePrimary = document.getElementById('projection-sse-details');
    const sseCmp = document.getElementById('projection-sse-details-compare');
    if (projectionActive) {
        btn.textContent = 'Hide Projection';
        btn.classList.add('active');
        controls.classList.remove('hidden');
        if (ssePrimary) ssePrimary.classList.remove('hidden');
        document.getElementById('fit-mode-btn').textContent =
            currentFitMode === 'log' ? 'Switch to Linear' : 'Switch to Log';
    } else {
        btn.textContent = 'Show Projection';
        btn.classList.remove('active');
        controls.classList.add('hidden');
        if (ssePrimary) { ssePrimary.classList.add('hidden'); ssePrimary.removeAttribute('open'); }
        if (sseCmp) { sseCmp.classList.add('hidden'); sseCmp.removeAttribute('open'); }
    }
    reloadProjections();
}

function toggleFitMode() {
    currentFitMode = currentFitMode === 'log' ? 'linear' : 'log';
    document.getElementById('fit-mode-btn').textContent =
        currentFitMode === 'log' ? 'Switch to Linear' : 'Switch to Log';
    reloadProjections();
}


function reloadProjections() {
    if (!currentUsername) return;
    const tasks = [loadEloChart(currentUsername, '', false)];
    if (compareMode && currentCompareUsername)
        tasks.push(loadEloChart(currentCompareUsername, '-compare', false));
    Promise.all(tasks).then(() => {
        if (compareMode && currentCompareUsername) syncYAxes('elo', 'elo-compare');
    });
}


function renderOpeningRow(o, showColorPip = false, totalRow = false) {
    const wPct = o.games ? o.wins   / o.games * 100 : 0;
    const dPct = o.games ? o.draws  / o.games * 100 : 0;
    const lPct = o.games ? o.losses / o.games * 100 : 0;
    const pip  = (showColorPip || totalRow)
        ? `<span class="color-pip ${o.color}"></span>`
        : '';
    const cls  = totalRow ? ' class="opening-total-row"' : '';
    return `
        <tr${cls}>
            <td>${pip}${o.name}</td>
            <td>${o.games}</td>
            <td>
                <div class="win-bar" data-wins="${o.wins}" data-draws="${o.draws}" data-losses="${o.losses}">
                    <span class="win-bar-w" style="width:${wPct.toFixed(1)}%"></span>
                    <span class="win-bar-d" style="width:${dPct.toFixed(1)}%"></span>
                    <span class="win-bar-l" style="width:${lPct.toFixed(1)}%"></span>
                </div>
            </td>
            <td class="wr-win">${o.win_rate}%</td>
            <td class="wr-draw">${o.draw_rate}%</td>
            <td class="wr-dec">${o.decisive_win_rate}%</td>
        </tr>`;
}

function buildOpeningTable(openings, showColorPip = false, footerRows = []) {
    const rows   = openings.map(o => renderOpeningRow(o, showColorPip)).join('');
    const footer = footerRows.map(o => renderOpeningRow(o, false, true)).join('');
    return `
        <table class="opening-stats-table">
            <thead>
                <tr>
                    <th>Opening</th>
                    <th>Games</th>
                    <th></th>
                    <th>Win%</th>
                    <th>Draw%</th>
                    <th>Decisive%</th>
                </tr>
            </thead>
            <tbody>${footer}${rows}</tbody>
        </table>`;
}

async function initRepertoireTabs(username) {
    try {
        // Load analytics for whichever main tab is currently active
        const activeMainTab = document.querySelector('#main-perspective-tabs .tab-btn.active');
        const activeTarget = activeMainTab ? activeMainTab.dataset.target : 'global';
        if (activeTarget !== 'global') {
            document.getElementById('white-tabs-container').classList.add('hidden');
            document.getElementById('black-tabs-container').classList.add('hidden');
            document.getElementById(activeTarget + '-tabs-container').classList.remove('hidden');
            const activeSub = document.querySelector(`#${activeTarget}-tabs .tab-btn.active`);
            const op = activeSub ? activeSub.dataset.op : "";
            loadColorAnalytics(username, activeTarget, op);
        } else {
            loadColorAnalytics(username, 'global', "");
        }

        const topOpenings = await fetchJSON(`/api/players/${username}/analytics/top-openings${buildFilterParams()}`);

        for (const color of ['white', 'black']) {
            const tabsContainer = document.getElementById(`${color}-tabs`);
            const openings = topOpenings[color] || [];

            const top8Str = openings.map(o => o.name).join('|');

            tabsContainer.innerHTML = `
                <button class="tab-btn active" data-color="${color}" data-op="">Overall</button>
                <button class="tab-btn" data-color="${color}" data-op="${top8Str}">Top 8 Aggregated</button>
                ${openings.map((o, i) => `<button class="tab-btn" data-color="${color}" data-op="${o.name}">#${i+1} ${o.name}</button>`).join('')}
            `;

            // Populate opening overview table for this color (with color total as footer)
            const tableEl = document.getElementById(`opening-stats-table-${color}`);
            if (tableEl && openings.length > 0) {
                const tot = topOpenings.totals?.[color];
                const footer = tot ? [{ ...tot, name: color === 'white' ? 'White Pieces' : 'Black Pieces', color }] : [];
                tableEl.innerHTML = buildOpeningTable(openings, false, footer);
                attachWinBarTooltips(tableEl);
            }

            tabsContainer.querySelectorAll('.tab-btn').forEach(btn => {
                btn.addEventListener('click', (e) => {
                    tabsContainer.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
                    e.target.classList.add('active');
                    const op = e.target.dataset.op;
                    currentOpeningFilter = op;
                    gamesPage = 0;
                    loadColorAnalytics(currentUsername, color, op);
                    loadGames(currentUsername);
                    if (compareMode && currentCompareUsername) loadGames(currentCompareUsername, '-compare');
                });
            });
        }

        // Build global combined table: top 8 combined + both color totals as footer
        const globalEl = document.getElementById('opening-stats-table-global');
        if (globalEl) {
            const combined = [
                ...topOpenings.white.map(o => ({ ...o, color: 'white' })),
                ...topOpenings.black.map(o => ({ ...o, color: 'black' })),
            ].sort((a, b) => b.games - a.games).slice(0, 8);

            const totals = topOpenings.totals || {};
            const footer = [
                totals.white ? { ...totals.white, name: 'White Pieces', color: 'white' } : null,
                totals.black ? { ...totals.black, name: 'Black Pieces', color: 'black' } : null,
            ].filter(Boolean);

            if (combined.length > 0 || footer.length > 0) {
                globalEl.innerHTML = buildOpeningTable(combined, true, footer);
                attachWinBarTooltips(globalEl);
            }
        }
    } catch (e) { console.error('Error loading top openings', e); }
}

function loadColorAnalytics(username, color, op) {
    const overview = document.getElementById('opening-stats-overview');
    if (overview) {
        overview.classList.remove('hidden');
        document.getElementById('opening-stats-table-global').classList.toggle('hidden', color !== 'global');
        document.getElementById('opening-stats-table-white').classList.toggle('hidden', color !== 'white');
        document.getElementById('opening-stats-table-black').classList.toggle('hidden', color !== 'black');
    }

    const loadId = ++analyticsLoadId;
    loadRatingDiff(username, color, op, loadId);
    loadGameLength(username, color, op, loadId);
    loadClockAdvantage(username, color, op, loadId);
    loadMoveTime(username, color, op, loadId);

    if (compareMode && currentCompareUsername) {
        const cId = ++compareLoadId;
        loadRatingDiff(currentCompareUsername, color, op, cId, '-compare');
        loadGameLength(currentCompareUsername, color, op, cId, '-compare');
        loadClockAdvantage(currentCompareUsername, color, op, cId, '-compare');
        loadMoveTime(currentCompareUsername, color, op, cId, '-compare');
    }
}



function colorParams(color, op) {
    const ext = {};
    if (color !== 'global') ext.player_color = color;
    if (op) ext.opening_names = op; // fetchJSON handles encoding
    return buildFilterParamsExtra(ext);
}

// ═══════════════════════════════════════════════════════════
// Feature 1: Rating Differential (10pt buckets within ±50)
// ═══════════════════════════════════════════════════════════

async function loadRatingDiff(username, color, op, loadId, suffix = '') {
    const chartKey = "loadRatingDiff" + suffix;
    try {
        const data = await fetchJSON(`/api/players/${username}/analytics/rating-diff${colorParams(color, op)}`);
        if (loadId !== (suffix ? compareLoadId : analyticsLoadId)) return;
        if (charts[chartKey]) charts[chartKey].destroy();

        const buckets = data.buckets;
        charts[chartKey] = new Chart(document.getElementById("rating-diff-chart" + suffix).getContext('2d'), {
            type: 'bar',
            data: {
                labels: buckets.map(b => b.bucket),
                datasets: [
                    { label: 'Wins', data: buckets.map(b => b.wins), backgroundColor: 'rgba(34, 197, 94, 0.7)', borderRadius: 4, stack: 'stack' },
                    { label: 'Losses', data: buckets.map(b => b.losses), backgroundColor: 'rgba(239, 68, 68, 0.7)', borderRadius: 4, stack: 'stack' },
                    { label: 'Draws', data: buckets.map(b => b.draws), backgroundColor: 'rgba(234, 179, 8, 0.7)', borderRadius: 4, stack: 'stack' },
                    {
                        label: 'Win Rate (Decisive) %', type: 'line',
                        data: buckets.map(b => b.win_rate_no_draws),
                        borderColor: '#818cf8', backgroundColor: 'transparent',
                        borderWidth: 2, pointRadius: 4, pointBackgroundColor: '#818cf8', pointHitRadius: 24,
                        yAxisID: 'y2',
                    },
                    {
                        label: 'Draw Rate %', type: 'line',
                        data: buckets.map(b => b.draw_rate),
                        borderColor: '#eab308', backgroundColor: 'transparent',
                        borderWidth: 2, pointRadius: 4, pointBackgroundColor: '#eab308', pointHitRadius: 24,
                        yAxisID: 'y2',
                    },
                ]
            },
            options: {
                responsive: true, maintainAspectRatio: false, animation: false,
                plugins: {
                    legend: { position: 'top', labels: { boxWidth: 12, padding: 16 } },
                    tooltip: {
                        callbacks: {
                            afterBody: (items) => {
                                const b = buckets[items[0].dataIndex];
                                return `Win Rate (Decisive): ${b.win_rate_no_draws}%\nDraw Rate: ${b.draw_rate}%\nTotal: ${b.total_games}`;
                            }
                        }
                    }
                },
                scales: {
                    x: { stacked: true, grid: { display: false }, ticks: { maxRotation: 45, font: { size: 10 } }, title: { display: true, text: '← Lower-rated opponents    Higher-rated opponents →', color: '#5a6a85', font: { size: 11 } } },
                    y: { stacked: true, grid: { color: 'rgba(42, 53, 72, 0.5)' } },
                    y2: { position: 'right', min: 0, max: 100, grid: { display: false }, title: { display: true, text: 'Win Rate %', color: '#5a6a85' }, ticks: { callback: v => v + '%' } },
                }
            }
        });

        const totalGames = buckets.reduce((s, b) => s + b.total_games, 0);
        const totalWins  = buckets.reduce((s, b) => s + b.wins, 0);
        const totalDraws = buckets.reduce((s, b) => s + b.draws, 0);
        const totalLosses = buckets.reduce((s, b) => s + b.losses, 0);
        const overallWinRate      = totalGames ? Math.round(totalWins / totalGames * 100) : 0;
        const overallDrawRate     = totalGames ? Math.round(totalDraws / totalGames * 100) : 0;
        const overallDecisiveRate = (totalWins + totalLosses) ? Math.round(totalWins / (totalWins + totalLosses) * 100) : 0;

        const el = document.getElementById("rating-diff-headlines" + suffix);
        el.innerHTML = `
            <div style="display: flex; gap: 0.75rem; padding: 1rem 0;">
                <div style="flex: 1; padding: 0.75rem; border-left: 3px solid var(--green);">
                    <div style="font-size: 0.8rem; color: var(--text-muted); margin-bottom: 0.2rem; display: flex; align-items: center; gap: 0.3rem;">
                        Hold Rate
                        <span class="stat-info-btn" data-desc="Win % in games where you are rated more than 10 Elo above your opponent.">?</span>
                    </div>
                    <div style="font-size: 1.2rem; font-weight: 700; color: var(--green);">${data.hold_rate}%</div>
                </div>
                <div style="flex: 1; padding: 0.75rem; border-left: 3px solid #94a3b8;">
                    <div style="font-size: 0.8rem; color: var(--text-muted); margin-bottom: 0.2rem; display: flex; align-items: center; gap: 0.3rem;">
                        Even Match Rate
                        <span class="stat-info-btn" data-desc="Win % in games where you and your opponent are within 10 Elo of each other.">?</span>
                    </div>
                    <div style="font-size: 1.2rem; font-weight: 700; color: #cbd5e1;">${data.even_rate}%</div>
                </div>
                <div style="flex: 1; padding: 0.75rem; border-left: 3px solid var(--accent);">
                    <div style="font-size: 0.8rem; color: var(--text-muted); margin-bottom: 0.2rem; display: flex; align-items: center; gap: 0.3rem;">
                        Upset Rate
                        <span class="stat-info-btn" data-desc="Win % in games where you are rated more than 10 Elo below your opponent.">?</span>
                    </div>
                    <div style="font-size: 1.2rem; font-weight: 700; color: var(--accent);">${data.upset_rate}%</div>
                </div>
            </div>
        `;
        el.querySelectorAll('.stat-info-btn').forEach(btn => {
            btn.addEventListener('click', (e) => {
                e.stopPropagation();
                const existing = btn.querySelector('.stat-info-desc');
                if (existing) { existing.remove(); return; }
                const desc = document.createElement('div');
                desc.className = 'stat-info-desc';
                desc.textContent = btn.dataset.desc;
                btn.appendChild(desc);
                const close = () => { desc.remove(); document.removeEventListener('click', close); };
                document.addEventListener('click', close);
            });
        });
    } catch (e) { console.error('Rating diff error:', e); }
}


// ═══════════════════════════════════════════════════════════
// Feature 2: Game Length vs Win Rate
// ═══════════════════════════════════════════════════════════

async function loadGameLength(username, color, op, loadId, suffix = '') {
    const chartKey = "loadGameLength" + suffix;
    try {
        const data = await fetchJSON(`/api/players/${username}/analytics/game-length${colorParams(color, op)}`);
        if (loadId !== (suffix ? compareLoadId : analyticsLoadId)) return;
        if (charts[chartKey]) charts[chartKey].destroy();

        charts[chartKey] = new Chart(document.getElementById("game-length-chart" + suffix).getContext('2d'), {
            type: 'bar',
            data: {
                labels: data.map(d => d.bucket + ' moves'),
                datasets: [
                    { label: 'Wins', data: data.map(d => d.wins), backgroundColor: 'rgba(34, 197, 94, 0.7)', borderRadius: 4, stack: 'stack' },
                    { label: 'Losses', data: data.map(d => d.losses), backgroundColor: 'rgba(239, 68, 68, 0.7)', borderRadius: 4, stack: 'stack' },
                    { label: 'Draws', data: data.map(d => d.draws), backgroundColor: 'rgba(234, 179, 8, 0.7)', borderRadius: 4, stack: 'stack' },
                    {
                        label: 'Win Rate (Decisive) %', type: 'line',
                        data: data.map(d => d.win_rate_no_draws),
                        borderColor: '#818cf8', backgroundColor: 'transparent',
                        borderWidth: 2, pointRadius: 4, pointBackgroundColor: '#818cf8', pointHitRadius: 24,
                        yAxisID: 'y2',
                    },
                    {
                        label: 'Draw Rate %', type: 'line',
                        data: data.map(d => d.draw_rate),
                        borderColor: '#eab308', backgroundColor: 'transparent',
                        borderWidth: 2, pointRadius: 4, pointBackgroundColor: '#eab308', pointHitRadius: 24,
                        yAxisID: 'y2',
                    },
                ]
            },
            options: {
                responsive: true, maintainAspectRatio: false, animation: false,
                plugins: {
                    legend: { position: 'top', labels: { boxWidth: 12, padding: 16 } },
                    tooltip: {
                        callbacks: {
                            afterBody: (items) => {
                                const d = data[items[0].dataIndex];
                                return `Win Rate (Decisive): ${d.win_rate_no_draws}%\nDraw Rate: ${d.draw_rate}%\nTotal Games: ${d.total_games}`;
                            }
                        }
                    }
                },
                scales: {
                    x: { stacked: true, grid: { display: false } },
                    y: { stacked: true, grid: { color: 'rgba(42, 53, 72, 0.5)' }, title: { display: true, text: 'Games', color: '#5a6a85' } },
                    y2: { position: 'right', min: 0, max: 100, grid: { display: false }, title: { display: true, text: 'Win Rate %', color: '#5a6a85' }, ticks: { callback: v => v + '%' } },
                }
            }
        });
    } catch (e) { console.error('Game length error:', e); }
}




// ═══════════════════════════════════════════════════════════
// Clock Advantage (with key/legend)
// ═══════════════════════════════════════════════════════════

async function loadClockAdvantage(username, color, op, loadId, suffix = '') {
    const chartKey = "loadClockAdvantage" + suffix;
    try {
        const data = await fetchJSON(`/api/players/${username}/analytics/clock-advantage${colorParams(color, op)}`);
        if (loadId !== (suffix ? compareLoadId : analyticsLoadId)) return;
        if (charts[chartKey]) charts[chartKey].destroy();

        const labelMap = {
            'far_behind': 'Far Behind (< -30s)',
            'behind': 'Behind (-15s to -30s)',
            'even': 'Even (±15s)',
            'ahead': 'Ahead (+15s to +30s)',
            'far_ahead': 'Far Ahead (> +30s)',
        };

        charts[chartKey] = new Chart(document.getElementById("clock-chart" + suffix).getContext('2d'), {
            type: 'bar',
            data: {
                labels: data.map(d => labelMap[d.clock_bucket] || d.clock_bucket),
                datasets: [
                    { label: 'Wins', data: data.map(d => d.wins), backgroundColor: 'rgba(34, 197, 94, 0.7)', borderRadius: 4, stack: 'stack' },
                    { label: 'Losses', data: data.map(d => d.losses), backgroundColor: 'rgba(239, 68, 68, 0.7)', borderRadius: 4, stack: 'stack' },
                    { label: 'Draws', data: data.map(d => d.draws), backgroundColor: 'rgba(234, 179, 8, 0.7)', borderRadius: 4, stack: 'stack' },
                    {
                        label: 'Win Rate (Decisive) %', type: 'line',
                        data: data.map(d => d.win_rate_no_draws),
                        borderColor: '#818cf8', backgroundColor: 'transparent',
                        borderWidth: 2, pointRadius: 4, pointBackgroundColor: '#818cf8', pointHitRadius: 24,
                        yAxisID: 'y2',
                    },
                    {
                        label: 'Draw Rate %', type: 'line',
                        data: data.map(d => d.draw_rate),
                        borderColor: '#eab308', backgroundColor: 'transparent',
                        borderWidth: 2, pointRadius: 4, pointBackgroundColor: '#eab308', pointHitRadius: 24,
                        yAxisID: 'y2',
                    },
                ]
            },
            options: {
                responsive: true, maintainAspectRatio: false, animation: false,
                plugins: {
                    legend: { position: 'top', labels: { boxWidth: 12, padding: 16 } },
                    tooltip: {
                        callbacks: {
                            afterBody: (items) => {
                                const d = data[items[0].dataIndex];
                                return `Win Rate (Decisive): ${d.win_rate_no_draws}%\nDraw Rate: ${d.draw_rate}%\nTotal: ${d.total_games}`;
                            }
                        }
                    },
                    subtitle: { display: false },
                },
                scales: {
                    x: { stacked: true, grid: { display: false }, ticks: { font: { size: 10 } } },
                    y: { stacked: true, grid: { color: 'rgba(42, 53, 72, 0.5)' } },
                    y2: { position: 'right', min: 0, max: 100, grid: { display: false }, title: { display: true, text: 'Win Rate %', color: '#5a6a85' }, ticks: { callback: v => v + '%' } },
                }
            }
        });
    } catch (e) { console.error('Clock advantage error:', e); }
}


// ═══════════════════════════════════════════════════════════
// Move Time Distribution & Avg Think Time by Move Number
// ═══════════════════════════════════════════════════════════


function fitLogLogistic(moveNums, avgTimes) {
    if (avgTimes.reduce((a, b) => a + b, 0) === 0 || moveNums.length < 3) return null;
    const n = avgTimes.length;
    let bestRss = Infinity, bestAlpha = 3, bestBeta = 20, bestA = 1, bestB = 0;

    // Grid search over (alpha, beta); for each pair solve for (A, b) analytically.
    // Model: y = A·pdf(x; α, β) + b  →  2×2 normal equations
    for (let ai = 0; ai <= 59; ai++) {
        const alpha = 1.5 + ai * (6.5 / 59);   // 1.5 – 8
        for (let bi = 0; bi <= 59; bi++) {
            const beta = 8 + bi * (52 / 59);    // 8 – 60
            const pdfs = moveNums.map(x => {
                const r = x / beta;
                return (alpha / beta) * Math.pow(r, alpha - 1) / Math.pow(1 + Math.pow(r, alpha), 2);
            });
            const sum_p  = pdfs.reduce((s, p) => s + p, 0);
            const sum_p2 = pdfs.reduce((s, p) => s + p * p, 0);
            const sum_y  = avgTimes.reduce((s, y) => s + y, 0);
            const sum_py = avgTimes.reduce((s, y, i) => s + y * pdfs[i], 0);
            const det = n * sum_p2 - sum_p * sum_p;
            if (det === 0) continue;
            const A = (n * sum_py - sum_p * sum_y) / det;
            const b = (sum_p2 * sum_y - sum_p * sum_py) / det;
            if (A <= 0) continue;
            const rss = avgTimes.reduce((s, y, i) => s + Math.pow(y - A * pdfs[i] - b, 2), 0);
            if (rss < bestRss) { bestRss = rss; bestAlpha = alpha; bestBeta = beta; bestA = A; bestB = b; }
        }
    }

    // Mode (peak): β·((α−1)/(α+1))^(1/α)  for α > 1
    const peakMove = bestAlpha > 1
        ? Math.round(bestBeta * Math.pow((bestAlpha - 1) / (bestAlpha + 1), 1 / bestAlpha))
        : 1;
    // Mean: β·π/α / sin(π/α)  for α > 1
    const meanMove = Math.round(bestBeta * Math.PI / bestAlpha / Math.sin(Math.PI / bestAlpha));
    const rmse = Math.sqrt(bestRss / n);

    return {
        peakMove,
        meanMove,
        rmse,
        curve: moveNums.map(x => {
            const r = x / bestBeta;
            const v = bestA * (bestAlpha / bestBeta) * Math.pow(r, bestAlpha - 1) / Math.pow(1 + Math.pow(r, bestAlpha), 2) + bestB;
            return Math.round(Math.max(0, v) * 100) / 100;
        }),
    };
}

async function loadMoveTime(username, color, op, loadId, suffix = '') {
    const distKey = "loadMoveTimeDist" + suffix;
    const moveKey = "loadMoveTimeByMove" + suffix;
    try {
        const data = await fetchJSON(`/api/players/${username}/analytics/move-time${colorParams(color, op)}`);
        if (loadId !== (suffix ? compareLoadId : analyticsLoadId)) return;

        if (charts[distKey]) charts[distKey].destroy();
        if (charts[moveKey]) charts[moveKey].destroy();

        // ── Distribution histogram ──
        const distDatasets = [{
            label: 'Moves',
            data: data.buckets.map(b => b.count),
            backgroundColor: 'rgba(129, 140, 248, 0.7)',
            borderRadius: 4,
        }];
        charts[distKey] = new Chart(document.getElementById("move-time-dist-chart" + suffix).getContext('2d'), {
            type: 'bar',
            data: {
                labels: data.buckets.map(b => b.label),
                datasets: distDatasets,
            },
            options: {
                responsive: true, maintainAspectRatio: false, animation: false,
                plugins: {
                    legend: { display: false },
                    tooltip: {
                        callbacks: {
                            label: (item) => {
                                if (item.datasetIndex === 0) {
                                    const b = data.buckets[item.dataIndex];
                                    return `${b.count.toLocaleString()} moves (${b.pct}%)`;
                                }
                                return `${item.dataset.label}: ${item.formattedValue}`;
                            }
                        }
                    }
                },
                scales: {
                    x: { grid: { display: false } },
                    y: { grid: { color: 'rgba(42, 53, 72, 0.5)' }, title: { display: true, text: 'Moves', color: '#5a6a85' } },
                }
            }
        });

        document.getElementById("move-time-stats" + suffix).innerHTML = `
            <div style="display: flex; flex-direction: column; gap: 0.6rem; padding: 0.5rem 0;">
                <div style="padding: 0.6rem 0.75rem; border-left: 3px solid #475569;">
                    <div style="font-size: 0.8rem; color: var(--text-muted); margin-bottom: 0.2rem;">Mean</div>
                    <div style="font-size: 1.1rem; font-weight: 700; color: var(--text-primary);">${data.mean}s</div>
                </div>
                <div style="padding: 0.6rem 0.75rem; border-left: 3px solid #475569;">
                    <div style="font-size: 0.8rem; color: var(--text-muted); margin-bottom: 0.2rem;">Median</div>
                    <div style="font-size: 1.1rem; font-weight: 700; color: var(--text-primary);">${data.median}s</div>
                </div>
                <div style="padding: 0.6rem 0.75rem; border-left: 3px solid #475569;">
                    <div style="font-size: 0.8rem; color: var(--text-muted); margin-bottom: 0.2rem;">Std Dev</div>
                    <div style="font-size: 1.1rem; font-weight: 700; color: var(--text-primary);">±${data.std_dev}s</div>
                </div>
            </div>
        `;


        // ── Avg think time by move number ──
        const byMove = data.by_move_number;
        const fit = fitLogLogistic(byMove.map(d => d.move_number), byMove.map(d => d.avg_seconds));
        const datasets = [{
            label: 'Avg seconds',
            data: byMove.map(d => d.avg_seconds),
            borderColor: '#818cf8',
            backgroundColor: 'rgba(129, 140, 248, 0.08)',
            fill: true,
            tension: 0.3,
            pointRadius: 3,
            pointHitRadius: 20,
            borderWidth: 2,
        }];
        if (fit) {
            datasets.push({
                label: 'Log-logistic fit',
                data: fit.curve,
                borderColor: 'rgba(251, 146, 60, 0.8)',
                backgroundColor: 'transparent',
                fill: false,
                tension: 0.4,
                pointRadius: 0,
                pointHitRadius: 20,
                borderWidth: 2,
                borderDash: [5, 4],
            });
        }
        charts[moveKey] = new Chart(document.getElementById("move-time-by-move-chart" + suffix).getContext('2d'), {
            type: 'line',
            data: {
                labels: byMove.map(d => d.move_number),
                datasets,
            },
            options: {
                responsive: true, maintainAspectRatio: false, animation: false,
                plugins: {
                    legend: { display: !!fit, position: 'top', labels: { boxWidth: 20, font: { size: 11 } } },
                    tooltip: {
                        callbacks: {
                            title: (items) => `Move ${items[0].label}`,
                            label: (item) => {
                                if (item.datasetIndex === 0) {
                                    const d = byMove[item.dataIndex];
                                    return `Avg: ${d.avg_seconds}s  (${d.count} moves)`;
                                }
                                return `Fitted: ${item.formattedValue}s`;
                            }
                        }
                    }
                },
                scales: {
                    x: { grid: { display: false }, title: { display: true, text: 'Move Number', color: '#5a6a85' } },
                    y: { grid: { color: 'rgba(42, 53, 72, 0.5)' }, title: { display: true, text: 'Avg seconds', color: '#5a6a85' }, ticks: { callback: v => v + 's' } },
                }
            }
        });

        const rmseEl = document.getElementById("move-time-rmse" + suffix);
        if (rmseEl) rmseEl.textContent = fit ? `RMSE ${fit.rmse.toFixed(2)}s` : '';

        const statsEl = document.getElementById("move-time-by-move-stats" + suffix);
        if (statsEl) {
            const gamesCount = byMove[0]?.count || 1;
            const totalSec = byMove.reduce((s, d) => s + d.avg_seconds * d.count, 0) / gamesCount;

            // Move at which 50% of total thinking time has been cumulatively spent
            const totalWeighted = byMove.reduce((s, d) => s + d.avg_seconds * d.count, 0);
            let cumulative = 0;
            let medianEffortMove = byMove[0]?.move_number ?? 1;
            for (const d of byMove) {
                cumulative += d.avg_seconds * d.count;
                if (cumulative >= totalWeighted * 0.5) { medianEffortMove = d.move_number; break; }
            }
            const mins = Math.floor(totalSec / 60);
            const secs = Math.round(totalSec % 60);
            const timeStr = mins > 0 ? `${mins}m ${secs}s` : `${secs}s`;
            statsEl.innerHTML = `
                <div style="display: flex; flex-direction: column; gap: 0.6rem; padding: 0.5rem 0;">
                    <div style="padding: 0.6rem 0.75rem; border-left: 3px solid #475569;">
                        <div style="font-size: 0.78rem; color: var(--text-muted); margin-bottom: 0.15rem;">Avg time per game</div>
                        <div style="font-size: 1.1rem; font-weight: 700; color: var(--text-primary);">${timeStr}</div>
                    </div>
                    ${fit ? `
                    <div style="padding: 0.6rem 0.75rem; border-left: 3px solid #475569;">
                        <div style="font-size: 0.78rem; color: var(--text-muted); margin-bottom: 0.15rem;">Mean effort move</div>
                        <div style="font-size: 1.1rem; font-weight: 700; color: var(--text-primary);">move ${medianEffortMove}</div>
                    </div>
                    <div style="padding: 0.6rem 0.75rem; border-left: 3px solid #475569;">
                        <div style="font-size: 0.78rem; color: var(--text-muted); margin-bottom: 0.15rem;">Peak think move</div>
                        <div style="font-size: 1.1rem; font-weight: 700; color: var(--text-primary);">move ${fit.peakMove}</div>
                    </div>` : ''}
                </div>
            `;
        }
    } catch (e) { console.error('Move time error:', e); }
}


// ═══════════════════════════════════════════════════════════
// Games List (paginated, 10 per page)
// ═══════════════════════════════════════════════════════════

async function loadGames(username, suffix = '') {
    const page = suffix ? gamesPageCompare : gamesPage;
    try {
        const offset = page * GAMES_PER_PAGE;
        const extras = { limit: GAMES_PER_PAGE, offset };
        if (currentOpeningFilter) extras.opening_names = currentOpeningFilter;
        const games = await fetchJSON(`/api/players/${username}/games${buildFilterParamsExtra(extras)}`);

        const tbody = document.getElementById('games-tbody' + suffix);
        if (games.length === 0 && page === 0) {
            tbody.innerHTML = '<tr><td colspan="9" style="text-align:center;color:var(--text-muted);padding:2rem">No games found for this player/filter.</td></tr>';
        } else {
            tbody.innerHTML = games.map(g => {
                const rc = playerResult(g.result, g.player_color);
                const rt = rc === 'win' ? 'Win' : rc === 'loss' ? 'Loss' : 'Draw';
                return `<tr>
                    <td>${g.date_played || '—'}</td>
                    <td style="color:var(--text-primary);font-weight:500">${g.opponent}</td>
                    <td><span class="color-dot ${g.player_color}"></span></td>
                    <td class="result-${rc}">${rt}</td>
                    <td>${g.time_class || '—'}</td>
                    <td style="font-family:var(--font-mono);font-size:0.82rem">${g.player_elo || '—'}</td>
                    <td style="font-size:0.78rem">${truncate(g.opening_name || '—', 28)}</td>
                    <td style="font-family:var(--font-mono)">${g.total_moves || '—'}</td>
                    <td><button class="btn-sm" onclick="openGameDetail(${g.game_id})">View</button></td>
                </tr>`;
            }).join('');
        }

        const pageInfo = document.getElementById('page-info' + suffix);
        const prevBtn = document.getElementById('prev-page-btn' + suffix);
        const nextBtn = document.getElementById('next-page-btn' + suffix);

        pageInfo.textContent = `Page ${page + 1}  (${offset + 1}–${offset + games.length})`;
        prevBtn.disabled = page === 0;
        nextBtn.disabled = games.length < GAMES_PER_PAGE;

    } catch (e) { console.error('Games list error:', e); }
}

function nextPage() {
    gamesPage++;
    loadGames(currentUsername);
}

function prevPage() {
    if (gamesPage > 0) {
        gamesPage--;
        loadGames(currentUsername);
    }
}

function nextPageCompare() {
    gamesPageCompare++;
    loadGames(currentCompareUsername, '-compare');
}

function prevPageCompare() {
    if (gamesPageCompare > 0) {
        gamesPageCompare--;
        loadGames(currentCompareUsername, '-compare');
    }
}

function playerResult(result, color) {
    if (result === '1-0') return color === 'white' ? 'win' : 'loss';
    if (result === '0-1') return color === 'black' ? 'win' : 'loss';
    return 'draw';
}
function truncate(str, len) { return str.length > len ? str.slice(0, len) + '…' : str; }


// ═══════════════════════════════════════════════════════════
// Game Detail Modal
// ═══════════════════════════════════════════════════════════

async function openGameDetail(gameId) {
    const modal = document.getElementById('game-modal');
    const body = document.getElementById('modal-body');
    modal.classList.remove('hidden');
    body.innerHTML = '<div class="loading">Loading...</div>';

    try {
        const game = await fetchJSON(`/api/games/${gameId}`);

        document.getElementById('modal-title').textContent = `${game.white_username} vs ${game.black_username}`;
        const rl = game.result === '1-0' ? 'White wins' : game.result === '0-1' ? 'Black wins' : 'Draw';

        body.innerHTML = `
            <dl class="game-detail-grid">
                <dt>Date</dt><dd>${game.date_played || '—'}</dd>
                <dt>Result</dt><dd>${game.result} (${rl})</dd>
                <dt>Time Control</dt><dd>${game.time_control} (${game.time_class})</dd>
                <dt>White Elo</dt><dd>${game.white_elo || '—'}</dd>
                <dt>Black Elo</dt><dd>${game.black_elo || '—'}</dd>
                <dt>White Accuracy</dt><dd>${game.white_accuracy ?? '—'}</dd>
                <dt>Black Accuracy</dt><dd>${game.black_accuracy ?? '—'}</dd>
                <dt>Opening</dt><dd>${game.opening_name || '—'}</dd>
                <dt>ECO</dt><dd>${game.eco || '—'}</dd>
                <dt>Termination</dt><dd>${game.termination || '—'}</dd>
                <dt>Moves</dt><dd>${game.total_moves || '—'}</dd>
                <dt>Link</dt><dd><a href="${game.chess_com_url}" target="_blank" style="color:var(--accent-light)">View on Chess.com</a></dd>
            </dl>
            
        `;
    } catch (e) {
        body.innerHTML = `<p style="color:var(--red)">Error: ${e.message}</p>`;
    }
}

function closeModal() { document.getElementById('game-modal').classList.add('hidden'); }
document.addEventListener('click', e => { if (e.target.id === 'game-modal') closeModal(); });
document.addEventListener('keydown', e => { if (e.key === 'Escape') closeModal(); });

// ═══════════════════════════════════════════════════════════
// Win Bar Tooltips
// ═══════════════════════════════════════════════════════════

function getOrCreateWinBarTooltip() {
    let tip = document.getElementById('win-bar-tooltip');
    if (!tip) {
        tip = document.createElement('div');
        tip.id = 'win-bar-tooltip';
        tip.className = 'win-bar-tooltip hidden';
        document.body.appendChild(tip);
    }
    return tip;
}

function attachWinBarTooltips(container) {
    const tip = getOrCreateWinBarTooltip();
    container.querySelectorAll('.win-bar').forEach(bar => {
        bar.addEventListener('mouseenter', () => {
            const wins   = bar.dataset.wins;
            const draws  = bar.dataset.draws;
            const losses = bar.dataset.losses;
            tip.innerHTML = `
                <span><span style="display:inline-block;width:8px;height:8px;border-radius:2px;background:rgba(34,197,94,0.75);margin-right:5px;"></span>Wins: ${wins}</span>
                <span><span style="display:inline-block;width:8px;height:8px;border-radius:2px;background:rgba(234,179,8,0.65);margin-right:5px;"></span>Draws: ${draws}</span>
                <span><span style="display:inline-block;width:8px;height:8px;border-radius:2px;background:rgba(239,68,68,0.70);margin-right:5px;"></span>Losses: ${losses}</span>
            `;
            tip.classList.remove('hidden');
        });
        bar.addEventListener('mousemove', (e) => {
            const x = e.clientX + 14;
            const y = e.clientY - 10;
            tip.style.left = `${x}px`;
            tip.style.top  = `${y}px`;
        });
        bar.addEventListener('mouseleave', () => {
            tip.classList.add('hidden');
        });
    });
}

// ═══════════════════════════════════════════════════════════
// Utilities
// ═══════════════════════════════════════════════════════════

async function fetchJSON(url, opts = {}) {
    if (opts.method && opts.method !== 'GET') {
        const resp = await fetch(API + url, opts);
        if (!resp.ok) throw new Error(`HTTP ${resp.status}: ${await resp.text()}`);
        return resp.json();
    }
    if (!requestCache[url]) {
        requestCache[url] = fetch(API + url).then(async resp => {
            if (!resp.ok) throw new Error(`HTTP ${resp.status}: ${await resp.text()}`);
            return resp.json();
        });
    }
    return requestCache[url];
}
function escapeHtml(s) { const d = document.createElement('div'); d.textContent = s; return d.innerHTML; }
