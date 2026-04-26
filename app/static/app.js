/* ═══════════════════════════════════════════════════════════
   Chess Analytics — Frontend JS
   ═══════════════════════════════════════════════════════════ */

const API = '';
let currentUsername = '';
let currentCompareUsername = '';
let compareMode = false;
let currentTimeClass = 'rapid';
let charts = {};
let gamesPage = 0;
let gamesPageCompare = 0;
const GAMES_PER_PAGE = 10;
const requestCache = {};
let analyticsLoadId = 0;
let compareLoadId = 0;

// Chart.js defaults
Chart.defaults.color = '#8b9ab8';
Chart.defaults.borderColor = '#2a3548';
Chart.defaults.font.family = "'Inter', sans-serif";
Chart.defaults.font.size = 12;


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

    const recencySlider = document.getElementById('recency-weight');
    const recencyVal = document.getElementById('recency-weight-val');
    recencySlider.addEventListener('input', () => {
        recencyVal.textContent = parseFloat(recencySlider.value).toFixed(1);
    });
    recencySlider.addEventListener('change', () => {
        if (!currentUsername) return;
        const tasks = [loadProjectedRatingChart(currentUsername)];
        if (compareMode && currentCompareUsername)
            tasks.push(loadProjectedRatingChart(currentCompareUsername, '-compare'));
        Promise.all(tasks).then(() => {
            if (compareMode && currentCompareUsername) syncProjectedYAxes();
        });
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
                loadColorAnalytics(currentUsername, targetId, op);
            } else {
                loadColorAnalytics(currentUsername, 'global', "");
            }
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

function renderRecentDropdown() {
    const input = document.getElementById('player-search');
    const dropdown = document.getElementById('recent-searches');
    const query = input.value.trim().toLowerCase();
    const recent = getRecentSearches().filter(u => !query || u.includes(query));
    if (recent.length === 0) { hideRecentDropdown(); return; }
    dropdown.innerHTML = recent.map(u => `<li>${u}</li>`).join('');
    dropdown.querySelectorAll('li').forEach(li => {
        li.addEventListener('click', () => {
            input.value = li.textContent;
            hideRecentDropdown();
            loadPlayer();
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
    dropdown.innerHTML = recent.map(u => `<li>${u}</li>`).join('');
    dropdown.querySelectorAll('li').forEach(li => {
        li.addEventListener('click', () => {
            input.value = li.textContent;
            hideCompareRecentDropdown();
            loadComparePlayer();
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


function syncEloYAxes() {
    const c1 = charts['elo'];
    const c2 = charts['elo-compare'];
    if (!c1 || !c2) return;
    const allY = [
        ...c1.data.datasets.flatMap(ds => ds.data.map(p => p.y)),
        ...c2.data.datasets.flatMap(ds => ds.data.map(p => p.y))
    ].filter(v => v != null);
    if (!allY.length) return;
    const yMin = Math.floor(Math.min(...allY) / 20) * 20;
    const yMax = Math.ceil(Math.max(...allY) / 20) * 20;
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
    document.querySelectorAll('.section').forEach(s => s.classList.remove('hidden'));

    const promises = [
        loadStats(currentUsername),
        loadEloChart(currentUsername),
        loadProjectedRatingChart(currentUsername),
        loadGames(currentUsername),
        initRepertoireTabs(currentUsername),
    ];
    if (compareMode && currentCompareUsername) {
        promises.push(loadCompareStats(currentCompareUsername));
        promises.push(loadEloChart(currentCompareUsername, '-compare'));
        promises.push(loadProjectedRatingChart(currentCompareUsername, '-compare'));
    }
    await Promise.all(promises);
    if (compareMode && currentCompareUsername) {
        syncEloYAxes();
        syncProjectedYAxes();
    }
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
    document.getElementById('projected-primary-label').textContent = currentUsername;
    document.getElementById('projected-compare-label').textContent = username;
    document.getElementById('games-primary-label').textContent = currentUsername;
    document.getElementById('games-compare-label').textContent = username;

    gamesPageCompare = 0;

    await ensureSynced(username);
    await loadCompareStats(username);

    const activeTab = document.querySelector('#main-perspective-tabs .tab-btn.active');
    const targetId = activeTab?.dataset.target || 'global';
    const color = targetId === 'global' ? 'global' : targetId;
    const activeSub = targetId !== 'global' ? document.querySelector(`#${targetId}-tabs .tab-btn.active`) : null;
    const op = activeSub?.dataset.op || '';
    loadColorAnalytics(currentUsername, color, op);

    await Promise.all([
        loadEloChart(username, '-compare'),
        loadProjectedRatingChart(username, '-compare'),
    ]);
    syncEloYAxes();
    syncProjectedYAxes();
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

    const compareKeys = ['loadGameLength', 'loadClockAdvantage', 'loadRatingDiff', 'loadMoveTimeDist', 'loadMoveTimeByMove', 'elo', 'projected'];
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

async function loadEloChart(username, suffix = '') {
    const chartKey = 'elo' + suffix;
    try {
        const data = await fetchJSON(`/api/players/${username}/analytics/elo-history${buildFilterParams()}`);
        if (charts[chartKey]) charts[chartKey].destroy();

        const toMs = s => Date.parse(s);
        const fmtDate = ms => new Date(ms).toISOString().slice(0, 10);

        let datasets = [];
        if (currentTimeClass) {
            datasets = [{
                label: currentTimeClass.charAt(0).toUpperCase() + currentTimeClass.slice(1),
                data: data.map(d => ({ x: toMs(d.date), y: d.elo })),
                borderColor: '#6366f1',
                backgroundColor: 'rgba(99, 102, 241, 0.08)',
                fill: true, tension: 0, pointRadius: 0, pointHitRadius: 6, borderWidth: 2,
                spanGaps: true
            }];
        } else {
            const colorMap = { bullet: '#ef4444', blitz: '#eab308', rapid: '#22c55e' };
            const byTc = {};
            data.forEach(d => {
                const tc = d.time_class || 'unknown';
                if (!byTc[tc]) byTc[tc] = [];
                byTc[tc].push({ x: toMs(d.date), y: d.elo });
            });
            for (const tc in byTc) {
                if (tc === 'unknown' || tc === 'daily') continue;
                datasets.push({
                    label: tc.charAt(0).toUpperCase() + tc.slice(1),
                    data: byTc[tc],
                    borderColor: colorMap[tc] || '#6366f1',
                    backgroundColor: 'transparent',
                    fill: false, tension: 0, pointRadius: 0, pointHitRadius: 6, borderWidth: 2,
                    spanGaps: true
                });
            }
        }

        const allMs = datasets.flatMap(ds => ds.data.map(p => p.x));
        const xMin = Math.min(...allMs);
        const xMax = Math.max(...allMs);

        charts[chartKey] = new Chart(document.getElementById('elo-chart' + suffix).getContext('2d'), {
            type: 'line',
            data: { datasets },
            options: {
                responsive: true, maintainAspectRatio: false,
                plugins: {
                    legend: { display: !currentTimeClass, position: 'top' },
                    tooltip: {
                        callbacks: {
                            title: items => items.length ? fmtDate(items[0].parsed.x) : ''
                        }
                    }
                },
                scales: {
                    x: {
                        type: 'linear',
                        min: xMin,
                        max: xMax,
                        ticks: {
                            maxTicksLimit: 10, maxRotation: 0,
                            callback: v => fmtDate(v)
                        },
                        grid: { display: false }
                    },
                    y: { grid: { color: 'rgba(42, 53, 72, 0.5)' } },
                }
            }
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

function fitLogarithmic(points, lambda = 0) {
    // Weighted least squares: y = a + b * ln(t), t normalized to [1, 2]
    // w_i = exp(-lambda * (1 - t_norm_i)) so recent points weigh more at higher lambda
    if (points.length < 2) return null;
    const xs = points.map(p => p.x);
    const xMin = Math.min(...xs), xMax = Math.max(...xs);
    const range = xMax - xMin || 1;
    const tNorms = xs.map(x => (x - xMin) / range);          // [0, 1]
    const lts = tNorms.map(t => Math.log(1 + t));             // ln([1, 2])
    const ys = points.map(p => p.y);
    const ws = tNorms.map(t => Math.exp(-lambda * (1 - t)));  // 1 at recent end
    const n = xs.length;
    const wSum = ws.reduce((a, b) => a + b, 0);
    const ltMean = ws.reduce((s, w, i) => s + w * lts[i], 0) / wSum;
    const yMean  = ws.reduce((s, w, i) => s + w * ys[i],  0) / wSum;
    let num = 0, den = 0;
    for (let i = 0; i < n; i++) {
        num += ws[i] * (lts[i] - ltMean) * (ys[i] - yMean);
        den += ws[i] * (lts[i] - ltMean) ** 2;
    }
    const b = den === 0 ? 0 : num / den;
    const a = yMean - b * ltMean;
    return { a, b, xMin, range };
}

function evalLogarithmic(fit, ms) {
    const lt = Math.log(1 + (ms - fit.xMin) / fit.range);
    return fit.a + fit.b * lt;
}

function syncProjectedYAxes() {
    const c1 = charts['projected'];
    const c2 = charts['projected-compare'];
    if (!c1 || !c2) return;
    const allY = [
        ...c1.data.datasets.flatMap(ds => ds.data.map(p => p.y)),
        ...c2.data.datasets.flatMap(ds => ds.data.map(p => p.y))
    ].filter(v => v != null && isFinite(v));
    if (!allY.length) return;
    const yMin = Math.floor(Math.min(...allY) / 20) * 20;
    const yMax = Math.ceil(Math.max(...allY) / 20) * 20;
    for (const c of [c1, c2]) {
        c.options.scales.y.min = yMin;
        c.options.scales.y.max = yMax;
        c.update();
    }
}

function getRecencyLambda() {
    return parseFloat(document.getElementById('recency-weight').value);
}

async function loadProjectedRatingChart(username, suffix = '') {
    const chartKey = 'projected' + suffix;
    try {
        const raw = await fetchJSON(`/api/players/${username}/analytics/elo-history${buildFilterParams()}`);
        const lambda = getRecencyLambda();
        if (charts[chartKey]) charts[chartKey].destroy();

        const toMs = s => Date.parse(s);
        const fmtDate = ms => new Date(ms).toISOString().slice(0, 10);
        const colorMap = { bullet: '#ef4444', blitz: '#eab308', rapid: '#22c55e' };
        const AMBER = '#f59e0b';
        const STEPS = 80;

        const groups = {};
        if (currentTimeClass) {
            groups[currentTimeClass] = raw.map(d => ({ x: toMs(d.date), y: d.elo }));
        } else {
            raw.forEach(d => {
                const tc = d.time_class;
                if (!tc || tc === 'unknown' || tc === 'daily') return;
                if (!groups[tc]) groups[tc] = [];
                groups[tc].push({ x: toMs(d.date), y: d.elo });
            });
        }

        const datasets = [];
        let actualXMax = 0;
        let xMinAll = Infinity, xMaxAll = -Infinity;

        for (const [tc, points] of Object.entries(groups)) {
            if (points.length < 2) continue;
            const xs = points.map(p => p.x);
            const tcXMin = Math.min(...xs), tcXMax = Math.max(...xs);
            const range = tcXMax - tcXMin || 1;
            const projMax = tcXMax + range;

            if (tcXMax > actualXMax) actualXMax = tcXMax;
            if (tcXMin < xMinAll) xMinAll = tcXMin;
            if (projMax > xMaxAll) xMaxAll = projMax;

            const fit = fitLogarithmic(points, lambda);
            if (!fit) continue;

            const baseColor = currentTimeClass ? '#6366f1' : (colorMap[tc] || '#6366f1');
            const label = tc.charAt(0).toUpperCase() + tc.slice(1);

            datasets.push({
                label,
                data: points,
                borderColor: baseColor,
                backgroundColor: hexToRgba(baseColor, 0.08),
                fill: true, tension: 0, pointRadius: 0, pointHitRadius: 6, borderWidth: 2, spanGaps: true
            });

            const fitPts = Array.from({ length: STEPS + 1 }, (_, i) => {
                const ms = tcXMin + range * i / STEPS;
                return { x: ms, y: evalLogarithmic(fit, ms) };
            });
            datasets.push({
                label: label + ' log fit',
                data: fitPts,
                borderColor: AMBER,
                backgroundColor: 'transparent',
                fill: false, tension: 0.3, pointRadius: 0, borderWidth: 1.5, spanGaps: true
            });

            const projPts = Array.from({ length: STEPS + 1 }, (_, i) => {
                const ms = tcXMax + range * i / STEPS;
                return { x: ms, y: evalLogarithmic(fit, ms) };
            });
            datasets.push({
                label: label + ' projection',
                data: projPts,
                borderColor: AMBER,
                backgroundColor: hexToRgba(AMBER, 0.06),
                fill: true, tension: 0.3, pointRadius: 0, borderWidth: 1.5,
                borderDash: [6, 4], spanGaps: true
            });
        }

        if (!datasets.length) return;

        const capturedActualXMax = actualXMax;
        const todayLinePlugin = {
            id: 'todayLine',
            afterDraw(chart) {
                const ctx = chart.ctx;
                const xScale = chart.scales.x;
                const x = xScale.getPixelForValue(capturedActualXMax);
                if (x < chart.chartArea.left || x > chart.chartArea.right) return;
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
        };

        charts[chartKey] = new Chart(
            document.getElementById('projected-chart' + suffix).getContext('2d'),
            {
                type: 'line',
                data: { datasets },
                options: {
                    responsive: true, maintainAspectRatio: false,
                    plugins: {
                        legend: { display: true, position: 'top' },
                        tooltip: {
                            callbacks: {
                                title: items => items.length ? fmtDate(items[0].parsed.x) : ''
                            }
                        }
                    },
                    scales: {
                        x: {
                            type: 'linear',
                            min: xMinAll,
                            max: xMaxAll,
                            ticks: { maxTicksLimit: 10, maxRotation: 0, callback: v => fmtDate(v) },
                            grid: { display: false }
                        },
                        y: { grid: { color: 'rgba(42, 53, 72, 0.5)' } }
                    }
                },
                plugins: [todayLinePlugin]
            }
        );
    } catch (e) { console.error('Projected rating chart error:', e); }
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
            
            const top5Str = openings.join('|');
            
            tabsContainer.innerHTML = `
                <button class="tab-btn active" data-color="${color}" data-op="">Overall</button>
                <button class="tab-btn" data-color="${color}" data-op="${top5Str}">Top 5 Aggregated</button>
                ${openings.map((op, i) => `<button class="tab-btn" data-color="${color}" data-op="${op}">#${i+1} ${op}</button>`).join('')}
            `;
            
            tabsContainer.querySelectorAll('.tab-btn').forEach(btn => {
                btn.addEventListener('click', (e) => {
                    tabsContainer.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
                    e.target.classList.add('active');
                    const op = e.target.dataset.op;
                    loadColorAnalytics(currentUsername, color, op);
                });
            });
        }
    } catch (e) { console.error('Error loading top openings', e); }
}

function loadColorAnalytics(username, color, op) {
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
                        borderWidth: 2, pointRadius: 4, pointBackgroundColor: '#818cf8',
                        yAxisID: 'y2',
                    },
                    {
                        label: 'Draw Rate %', type: 'line',
                        data: buckets.map(b => b.draw_rate),
                        borderColor: '#eab308', backgroundColor: 'transparent',
                        borderWidth: 2, pointRadius: 4, pointBackgroundColor: '#eab308',
                        yAxisID: 'y2',
                    },
                ]
            },
            options: {
                responsive: true, maintainAspectRatio: false,
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

        document.getElementById("rating-diff-headlines" + suffix).innerHTML = `
            <div class="headline-stat green" style="padding: 1rem;">
                <div style="font-size: 0.95rem; color: var(--text-primary); line-height: 1.4;">
                    Hold Rate (win % when >10 elo higher rated than your opponent):
                    <strong style="color: var(--green); font-size: 1.1rem;">${data.hold_rate}%</strong>
                </div>
            </div>
            <div class="headline-stat" style="margin-top: 1rem; padding: 1rem; border-left-color: #94a3b8;">
                <div style="font-size: 0.95rem; color: var(--text-primary); line-height: 1.4;">
                    Even Match Rate (win % when evenly rated with your opponent):
                    <strong style="color: #cbd5e1; font-size: 1.1rem;">${data.even_rate}%</strong>
                </div>
            </div>
            <div class="headline-stat accent" style="margin-top: 1rem; padding: 1rem;">
                <div style="font-size: 0.95rem; color: var(--text-primary); line-height: 1.4;">
                    Upset Rate (win % when >10 elo lower rated than your opponent):
                    <strong style="color: var(--accent); font-size: 1.1rem;">${data.upset_rate}%</strong>
                </div>
            </div>
        `;
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
                        borderWidth: 2, pointRadius: 4, pointBackgroundColor: '#818cf8',
                        yAxisID: 'y2',
                    },
                    {
                        label: 'Draw Rate %', type: 'line',
                        data: data.map(d => d.draw_rate),
                        borderColor: '#eab308', backgroundColor: 'transparent',
                        borderWidth: 2, pointRadius: 4, pointBackgroundColor: '#eab308',
                        yAxisID: 'y2',
                    },
                ]
            },
            options: {
                responsive: true, maintainAspectRatio: false,
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
                        borderWidth: 2, pointRadius: 4, pointBackgroundColor: '#818cf8',
                        yAxisID: 'y2',
                    },
                    {
                        label: 'Draw Rate %', type: 'line',
                        data: data.map(d => d.draw_rate),
                        borderColor: '#eab308', backgroundColor: 'transparent',
                        borderWidth: 2, pointRadius: 4, pointBackgroundColor: '#eab308',
                        yAxisID: 'y2',
                    },
                ]
            },
            options: {
                responsive: true, maintainAspectRatio: false,
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
                    subtitle: {
                        display: true,
                        text: 'Average clock difference (your time − opponent time) across all moves in each game',
                        color: '#5a6a85',
                        font: { size: 11 },
                        padding: { bottom: 10 },
                    },
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

function fitLogNormal(moveNums, avgTimes) {
    if (avgTimes.reduce((a, b) => a + b, 0) === 0 || moveNums.length < 3) return null;
    const logNums = moveNums.map(x => Math.log(x));
    const K = 1 / Math.sqrt(2 * Math.PI);

    let bestRss = Infinity, bestMu = 2.5, bestSigma = 0.6, bestA = 1;

    // Grid search: for each (mu, sigma), solve for optimal A analytically then measure RSS
    for (let mi = 0; mi <= 59; mi++) {
        const mu = 1.5 + mi * (3.1 / 59);
        for (let si = 0; si <= 39; si++) {
            const sigma = 0.2 + si * (1.3 / 39);
            const s2 = sigma * sigma;
            const pdfs = logNums.map((lx, i) => K * Math.exp(-Math.pow(lx - mu, 2) / (2 * s2)) / (moveNums[i] * sigma));
            const dot_yp = avgTimes.reduce((s, y, i) => s + y * pdfs[i], 0);
            const dot_pp = pdfs.reduce((s, p) => s + p * p, 0);
            if (dot_pp === 0) continue;
            const A = dot_yp / dot_pp;
            if (A <= 0) continue;
            const rss = avgTimes.reduce((s, y, i) => s + Math.pow(y - A * pdfs[i], 2), 0);
            if (rss < bestRss) { bestRss = rss; bestMu = mu; bestSigma = sigma; bestA = A; }
        }
    }

    const s2 = bestSigma * bestSigma;
    return {
        peakMove: Math.round(Math.exp(bestMu - s2)),
        meanMove: Math.round(Math.exp(bestMu + s2 / 2)),
        sigma: bestSigma.toFixed(2),
        curve: logNums.map((lx, i) => {
            const v = bestA * K * Math.exp(-Math.pow(lx - bestMu, 2) / (2 * s2)) / (moveNums[i] * bestSigma);
            return Math.round(v * 100) / 100;
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
        charts[distKey] = new Chart(document.getElementById("move-time-dist-chart" + suffix).getContext('2d'), {
            type: 'bar',
            data: {
                labels: data.buckets.map(b => b.label),
                datasets: [{
                    label: 'Moves',
                    data: data.buckets.map(b => b.count),
                    backgroundColor: 'rgba(129, 140, 248, 0.7)',
                    borderRadius: 4,
                }]
            },
            options: {
                responsive: true, maintainAspectRatio: false,
                plugins: {
                    legend: { display: false },
                    tooltip: {
                        callbacks: {
                            label: (item) => {
                                const b = data.buckets[item.dataIndex];
                                return `${b.count.toLocaleString()} moves (${b.pct}%)`;
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
            <div style="display: flex; gap: 0.75rem; padding: 1rem 0;">
                <div style="flex: 1; padding: 0.75rem; border-left: 3px solid #475569;">
                    <div style="font-size: 0.8rem; color: var(--text-muted); margin-bottom: 0.2rem;">Mean</div>
                    <div style="font-size: 1.2rem; font-weight: 700; color: var(--text-primary);">${data.mean}s</div>
                </div>
                <div style="flex: 1; padding: 0.75rem; border-left: 3px solid #475569;">
                    <div style="font-size: 0.8rem; color: var(--text-muted); margin-bottom: 0.2rem;">Median</div>
                    <div style="font-size: 1.2rem; font-weight: 700; color: var(--text-primary);">${data.median}s</div>
                </div>
                <div style="flex: 1; padding: 0.75rem; border-left: 3px solid #475569;">
                    <div style="font-size: 0.8rem; color: var(--text-muted); margin-bottom: 0.2rem;">Std Dev</div>
                    <div style="font-size: 1.2rem; font-weight: 700; color: var(--text-primary);">±${data.std_dev}s</div>
                </div>
            </div>
        `;


        // ── Avg think time by move number ──
        const byMove = data.by_move_number;
        const fit = fitLogNormal(byMove.map(d => d.move_number), byMove.map(d => d.avg_seconds));
        const datasets = [{
            label: 'Avg seconds',
            data: byMove.map(d => d.avg_seconds),
            borderColor: '#818cf8',
            backgroundColor: 'rgba(129, 140, 248, 0.08)',
            fill: true,
            tension: 0.3,
            pointRadius: 3,
            borderWidth: 2,
        }];
        if (fit) {
            datasets.push({
                label: 'Log-normal fit',
                data: fit.curve,
                borderColor: 'rgba(251, 146, 60, 0.8)',
                backgroundColor: 'transparent',
                fill: false,
                tension: 0.4,
                pointRadius: 0,
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
                responsive: true, maintainAspectRatio: false,
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

        const statsEl = document.getElementById("move-time-by-move-stats" + suffix);
        if (statsEl) {
            const totalSec = byMove.reduce((s, d) => s + d.avg_seconds, 0);
            const mins = Math.floor(totalSec / 60);
            const secs = Math.round(totalSec % 60);
            const timeStr = mins > 0 ? `${mins}m ${secs}s` : `${secs}s`;
            statsEl.innerHTML = `
                <div style="display: flex; gap: 0.75rem; padding: 0.75rem 0 0.25rem;">
                    <div style="flex: 1; padding: 0.6rem 0.75rem; border-left: 3px solid #475569;">
                        <div style="font-size: 0.78rem; color: var(--text-muted); margin-bottom: 0.15rem;">Avg time per game</div>
                        <div style="font-size: 1.1rem; font-weight: 700; color: var(--text-primary);">${timeStr}</div>
                    </div>
                    ${fit ? `
                    <div style="flex: 1; padding: 0.6rem 0.75rem; border-left: 3px solid #475569;">
                        <div style="font-size: 0.78rem; color: var(--text-muted); margin-bottom: 0.15rem;">Mean effort move</div>
                        <div style="font-size: 1.1rem; font-weight: 700; color: var(--text-primary);">move ${fit.meanMove}</div>
                    </div>
                    <div style="flex: 1; padding: 0.6rem 0.75rem; border-left: 3px solid #475569;">
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
        const games = await fetchJSON(`/api/players/${username}/games${buildFilterParamsExtra({
            limit: GAMES_PER_PAGE,
            offset: offset,
        })}`);

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
// Utilities
// ═══════════════════════════════════════════════════════════

async function fetchJSON(url, opts = {}) {
    if (opts.method && opts.method !== 'GET') {
        const resp = await fetch(API + url, opts);
        if (!resp.ok) throw new Error(`HTTP ${resp.status}: ${await resp.text()}`);
        return resp.json();
    }
    if (requestCache[url]) return requestCache[url];
    const resp = await fetch(API + url, opts);
    if (!resp.ok) throw new Error(`HTTP ${resp.status}: ${await resp.text()}`);
    const data = await resp.json();
    requestCache[url] = data;
    return data;
}
function escapeHtml(s) { const d = document.createElement('div'); d.textContent = s; return d.innerHTML; }
