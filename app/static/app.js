/* ═══════════════════════════════════════════════════════════
   Chess Analytics — Frontend JS
   ═══════════════════════════════════════════════════════════ */

const API = '';
let currentUsername = '';
let currentTimeClass = 'rapid';
let charts = {};
let gamesPage = 0;
const GAMES_PER_PAGE = 10;
const requestCache = {};

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
    // Default to last 6 months
    const d = new Date();
    d.setMonth(d.getMonth() - 6);
    document.getElementById('start-date').value = d.toISOString().split('T')[0];

    document.querySelectorAll('.tc-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            document.querySelectorAll('.tc-btn').forEach(b => b.classList.remove('active'));
            btn.classList.add('active');
            currentTimeClass = btn.dataset.tc;
            if (currentUsername) refreshAll();
        });
    });

    document.getElementById('username-input').addEventListener('keydown', e => {
        if (e.key === 'Enter') loadPlayer();
    });
});


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
    const input = document.getElementById('username-input');
    const username = input.value.trim().toLowerCase();
    if (!username) return;

    currentUsername = username;
    currentTimeClass = 'rapid';
    gamesPage = 0;
    document.querySelectorAll('.tc-btn').forEach(b => b.classList.remove('active'));
    document.querySelector('.tc-btn[data-tc="rapid"]').classList.add('active');

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


async function refreshAll() {
    Object.keys(requestCache).forEach(k => delete requestCache[k]);

    updateDateRangeLabel();
    gamesPage = 0;
    document.querySelectorAll('.section').forEach(s => s.classList.remove('hidden'));

    await Promise.all([
        loadStats(currentUsername),
        loadEloChart(currentUsername),
        loadGames(currentUsername),
        initRepertoireTabs(currentUsername)
    ]);
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
            stats = {
                total_games: tc.total, wins: tc.wins, losses: tc.losses, draws: tc.draws,
                win_rate: tc.total ? (tc.wins / tc.total * 100) : 0,
            };
        }

        document.getElementById('val-total').textContent = stats.total_games.toLocaleString();
        document.getElementById('val-wins').textContent = stats.wins.toLocaleString();
        document.getElementById('val-losses').textContent = stats.losses.toLocaleString();
        document.getElementById('val-draws').textContent = stats.draws.toLocaleString();
        document.getElementById('val-winrate').textContent = stats.win_rate.toFixed(1) + '%';
    } catch (e) { console.error('Stats error:', e); }
}


// ═══════════════════════════════════════════════════════════
// Elo Chart
// ═══════════════════════════════════════════════════════════

async function loadEloChart(username) {
    try {
        const data = await fetchJSON(`/api/players/${username}/analytics/elo-history${buildFilterParams()}`);
        if (charts.elo) charts.elo.destroy();

        let datasets = [];
        if (currentTimeClass) {
            datasets = [{
                label: currentTimeClass.charAt(0).toUpperCase() + currentTimeClass.slice(1),
                data: data.map(d => ({ x: d.date, y: d.elo })),
                borderColor: '#6366f1',
                backgroundColor: 'rgba(99, 102, 241, 0.08)',
                fill: true, tension: 0.3, pointRadius: 0, pointHitRadius: 6, borderWidth: 2,
                spanGaps: true
            }];
        } else {
            const colorMap = { bullet: '#ef4444', blitz: '#eab308', rapid: '#22c55e' };
            const byTc = {};
            data.forEach(d => {
                const tc = d.time_class || 'unknown';
                if (!byTc[tc]) byTc[tc] = [];
                byTc[tc].push({ x: d.date, y: d.elo });
            });
            for (const tc in byTc) {
                if (tc === 'unknown' || tc === 'daily') continue; // only focus on core
                datasets.push({
                    label: tc.charAt(0).toUpperCase() + tc.slice(1),
                    data: byTc[tc],
                    borderColor: colorMap[tc] || '#6366f1',
                    backgroundColor: 'transparent',
                    fill: false, tension: 0.3, pointRadius: 0, pointHitRadius: 6, borderWidth: 2,
                    spanGaps: true
                });
            }
        }

        charts.elo = new Chart(document.getElementById('elo-chart').getContext('2d'), {
            type: 'line',
            data: {
                labels: Array.from(new Set(data.map(d => d.date))),
                datasets: datasets
            },
            options: {
                responsive: true, maintainAspectRatio: false,
                plugins: { legend: { display: !currentTimeClass, position: 'top' } },
                scales: {
                    x: { ticks: { maxTicksLimit: 10, maxRotation: 0 }, grid: { display: false } },
                    y: { grid: { color: 'rgba(42, 53, 72, 0.5)' } },
                }
            }
        });
    } catch (e) { console.error('Elo chart error:', e); }
}



async function initRepertoireTabs(username) {
    try {
        // Main perspective tabs
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

        // Load global analytics
        loadColorAnalytics(username, 'global', "");

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
            
            loadColorAnalytics(currentUsername, color, "");
        }
    } catch (e) { console.error('Error loading top openings', e); }
}

function loadColorAnalytics(username, color, op) {
    loadDecisiveHistoryChart(username, color, op);
    loadRatingDiff(username, color, op);
    loadGameLength(username, color, op);
    loadTimeRemaining(username, color, op);
    loadClockAdvantage(username, color, op);
}


async function loadDecisiveHistoryChart(username, color, op) {
    const chartKey = "loadDecisiveHistory";
    try {
        const data = await fetchJSON(`/api/players/${username}/analytics/decisive-history${colorParams(color, op)}`);
        if (charts[chartKey]) charts[chartKey].destroy();

        charts[chartKey] = new Chart(document.getElementById("decisive-history-chart").getContext('2d'), {
            type: 'line',
            data: {
                labels: data.map(d => d.week),
                datasets: [
                    {
                        label: 'Decisive Win Rate %',
                        data: data.map(d => d.win_rate_no_draws),
                        borderColor: '#818cf8',
                        backgroundColor: 'rgba(129, 140, 248, 0.1)',
                        fill: true,
                        tension: 0.3,
                        pointRadius: 4,
                        borderWidth: 2
                    },
                    {
                        label: 'Draw Rate %',
                        data: data.map(d => d.draw_rate),
                        borderColor: '#eab308',
                        backgroundColor: 'rgba(234, 179, 8, 0.1)',
                        fill: true,
                        tension: 0.3,
                        pointRadius: 4,
                        borderWidth: 2
                    }
                ]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                plugins: {
                    legend: { position: 'top', labels: { boxWidth: 12, padding: 16 } },
                    tooltip: {
                        callbacks: {
                            afterBody: (items) => {
                                const d = data[items[0].dataIndex];
                                return `Week: ${d.week}\nTotal Games: ${d.total_games}\nWins: ${d.wins}\nLosses: ${d.losses}\nDraws: ${d.draws}`;
                            }
                        }
                    }
                },
                scales: {
                    x: { grid: { display: false } },
                    y: { min: 0, max: 100, grid: { color: 'rgba(42, 53, 72, 0.5)' }, ticks: { callback: v => v + '%' } }
                }
            }
        });
    } catch (e) { console.error('Decisive history chart error:', e); }
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

async function loadRatingDiff(username, color, op) {
    const chartKey = "loadRatingDiff";
    try {
        const data = await fetchJSON(`/api/players/${username}/analytics/rating-diff${colorParams(color, op)}`);
        if (charts[chartKey]) charts[chartKey].destroy();

        const buckets = data.buckets;
        charts[chartKey] = new Chart(document.getElementById("rating-diff-chart").getContext('2d'), {
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

        document.getElementById("rating-diff-headlines").innerHTML = `
            <div class="headline-stat" style="padding: 1rem; border-left-color: #475569;">
                <div style="font-size: 0.95rem; color: var(--text-primary); line-height: 1.4;">
                    Overall Decisive Win Rate: 
                    <strong style="color: #cbd5e1; font-size: 1.1rem;">${data.overall_decisive_win_rate ?? 0}%</strong>
                </div>
            </div>
            <div class="headline-stat" style="margin-top: 1rem; padding: 1rem; border-left-color: #475569;">
                <div style="font-size: 0.95rem; color: var(--text-primary); line-height: 1.4;">
                    Overall Draw Rate: 
                    <strong style="color: #cbd5e1; font-size: 1.1rem;">${data.overall_draw_rate ?? 0}%</strong>
                </div>
            </div>
            <div class="headline-stat accent" style="margin-top: 1rem; padding: 1rem;">
                <div style="font-size: 0.95rem; color: var(--text-primary); line-height: 1.4;">
                    Upset Rate (win % when >10 elo lower rated than your opponent): 
                    <strong style="color: var(--accent); font-size: 1.1rem;">${data.upset_rate}%</strong>
                </div>
            </div>
            <div class="headline-stat" style="margin-top: 1rem; padding: 1rem; border-left-color: #94a3b8;">
                <div style="font-size: 0.95rem; color: var(--text-primary); line-height: 1.4;">
                    Even Match Rate (win % when evenly rated with your opponent): 
                    <strong style="color: #cbd5e1; font-size: 1.1rem;">${data.even_rate}%</strong>
                </div>
            </div>
            <div class="headline-stat green" style="margin-top: 1rem; padding: 1rem;">
                <div style="font-size: 0.95rem; color: var(--text-primary); line-height: 1.4;">
                    Hold Rate (win % when >10 elo higher rated than your opponent): 
                    <strong style="color: var(--green); font-size: 1.1rem;">${data.hold_rate}%</strong>
                </div>
            </div>
        `;
    } catch (e) { console.error('Rating diff error:', e); }
}


// ═══════════════════════════════════════════════════════════
// Feature 2: Game Length vs Win Rate
// ═══════════════════════════════════════════════════════════

async function loadGameLength(username, color, op) {
    const chartKey = "loadGameLength";
    try {
        const data = await fetchJSON(`/api/players/${username}/analytics/game-length${colorParams(color, op)}`);
        if (charts[chartKey]) charts[chartKey].destroy();

        charts[chartKey] = new Chart(document.getElementById("game-length-chart").getContext('2d'), {
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
// Feature 3: Final Time Remaining vs Result (inverted x-axis)
// ═══════════════════════════════════════════════════════════

async function loadTimeRemaining(username, color, op) {
    const chartKey = "loadTimeRemaining";
    try {
        const data = await fetchJSON(`/api/players/${username}/analytics/time-remaining${colorParams(color, op)}`);
        if (charts[chartKey]) charts[chartKey].destroy();

        charts[chartKey] = new Chart(document.getElementById("time-remaining-chart").getContext('2d'), {
            type: 'bar',
            data: {
                labels: data.map(d => d.bucket),
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
                    x: { stacked: true, grid: { display: false }, title: { display: true, text: '← More time remaining    Less time remaining →', color: '#5a6a85', font: { size: 11 } } },
                    y: { stacked: true, grid: { color: 'rgba(42, 53, 72, 0.5)' }, title: { display: true, text: 'Games', color: '#5a6a85' } },
                    y2: { position: 'right', min: 0, max: 100, grid: { display: false }, title: { display: true, text: 'Win Rate %', color: '#5a6a85' }, ticks: { callback: v => v + '%' } },
                }
            }
        });
    } catch (e) { console.error('Time remaining error:', e); }
}


// ═══════════════════════════════════════════════════════════
// Clock Advantage (with key/legend)
// ═══════════════════════════════════════════════════════════

async function loadClockAdvantage(username, color, op) {
    const chartKey = "loadClockAdvantage";
    try {
        const data = await fetchJSON(`/api/players/${username}/analytics/clock-advantage${colorParams(color, op)}`);
        if (charts[chartKey]) charts[chartKey].destroy();

        // Friendly labels with second thresholds
        const labelMap = {
            'far_behind': 'Far Behind (< -15s)',
            'behind': 'Behind (-5s to -15s)',
            'even': 'Even (±5s)',
            'ahead': 'Ahead (+5s to +15s)',
            'far_ahead': 'Far Ahead (> +15s)',
        };

        charts[chartKey] = new Chart(document.getElementById("clock-chart").getContext('2d'), {
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
// Games List (paginated, 10 per page)
// ═══════════════════════════════════════════════════════════

async function loadGames(username) {
    try {
        const offset = gamesPage * GAMES_PER_PAGE;
        const games = await fetchJSON(`/api/players/${username}/games${buildFilterParamsExtra({
            limit: GAMES_PER_PAGE,
            offset: offset,
        })}`);

        const tbody = document.getElementById('games-tbody');
        if (games.length === 0 && gamesPage === 0) {
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

        // Update pagination controls
        const pageInfo = document.getElementById('page-info');
        const prevBtn = document.getElementById('prev-page-btn');
        const nextBtn = document.getElementById('next-page-btn');

        pageInfo.textContent = `Page ${gamesPage + 1}  (${offset + 1}–${offset + games.length})`;
        prevBtn.disabled = gamesPage === 0;
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
