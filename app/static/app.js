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
let currentOpeningColor = 'global';  // 'global' | 'white' | 'black'
const ANALYTICS_SECTIONS = ['outcomes', 'time', 'form'];
const collapsedSections = new Set();  // sections the user has collapsed
const OPENINGS_PREVIEW_COUNT = 6;     // opening rows shown before "show all"
let openingsExpanded = false;
let lastTopOpenings = null;           // cached so the toggle can re-render
let winrateMode = 'color';
let winrateWindow = 30;
// Below this many population games, a bucket's win rate is not reported and
// its volume bar is drawn faint. At n=50 the standard error on a win rate is
// about 7 points — loose enough to keep the shoulders of a distribution,
// tight enough to drop the 10-game tails.
const BASELINE_MIN_BUCKET_GAMES = 50;
let baselineEnabled = true;
let selectedBaselineBand = '';   // '' means "derive from the player"
const baselineResults = {};      // chart -> meta|null, drives the empty-state notice

// Chart.js defaults
Chart.defaults.color = '#8b9ab8';
Chart.defaults.borderColor = '#2a3548';
Chart.defaults.font.family = "'Inter', sans-serif";
Chart.defaults.font.size = 12;

const toMs = s => Date.parse(s);
const fmtDate = ms => new Date(ms).toISOString().slice(0, 10);
const TIME_CLASS_COLORS = { bullet: '#ef4444', blitz: '#eab308', rapid: '#22c55e' };
const DEFAULT_COLOR = '#3792b8';
const ACCENT_LIGHT = '#6fbcd8';
const Y_AXIS_STEP = 20;
const PROJECTION_STEPS = 80;


// ═══════════════════════════════════════════════════════════
// Population Baseline Overlays
// ═══════════════════════════════════════════════════════════

function baselineParams(color, op) {
    const ext = {};
    if (color && color !== 'global') ext.player_color = color;
    if (op) ext.opening_names = op;
    if (selectedBaselineBand) ext.elo_band = selectedBaselineBand;
    return buildFilterParamsExtra(ext);
}

/**
 * Fetch one chart's baseline. Always resolves — a failure or an empty band
 * yields null so the caller renders the player's chart alone.
 */
async function fetchBaseline(username, chart, color, op) {
    if (!baselineEnabled) { baselineResults[chart] = null; renderBaselineNotice(); return null; }
    try {
        const r = await fetchJSON(
            `/api/players/${username}/analytics/${chart}/baseline${baselineParams(color, op)}`);
        const result = r && r.data ? r : null;
        baselineResults[chart] = result ? result.meta : null;
        renderBaselineNotice();
        return result;
    } catch (e) {
        console.warn(`Baseline unavailable for ${chart}:`, e);
        baselineResults[chart] = null;
        renderBaselineNotice();
        return null;
    }
}

/** Chart.js dataset styling shared by every overlay: muted, dashed, behind. */
function baselineLineStyle(extra = {}) {
    return {
        borderColor: 'rgba(148, 163, 184, 0.85)',
        backgroundColor: 'transparent',
        fill: false,
        tension: 0.3,
        pointRadius: 0,
        pointHitRadius: 20,
        borderWidth: 2,
        borderDash: [6, 4],
        order: 10,
        ...extra,
    };
}

function baselineLabel(meta) {
    if (!meta) return 'Average';
    const [lo, hi] = meta.elo_band;
    const tc = meta.tc_fallback ? (meta.time_class || 'all') : meta.time_control;
    const who = meta.source === 'all' ? 'All players'
        : meta.source === 'selected' ? `Compared to ${lo}–${hi}`
        : `Average ${lo}–${hi}`;
    return `${who} · ${tc} · ${meta.n_players.toLocaleString()} players`;
}

/** Explicit empty state: a selected band with no data must say so, rather
 *  than silently showing no line. */
function renderBaselineNotice() {
    const el = document.getElementById('baseline-notice');
    if (!el) return;
    // Only when NO chart resolved a baseline. Charts resolve independently —
    // streak-reaction in particular can come back empty while the rest are
    // fine — so one null must not speak for the page.
    const results = Object.values(baselineResults);
    const noneResolved = results.length > 0 && results.every(m => m === null);
    if (baselineEnabled && selectedBaselineBand && noneResolved) {
        const lo = Number(selectedBaselineBand);
        const which = selectedBaselineBand === 'all'
            ? 'all players'
            : `${lo}–${lo + 99}`;
        el.textContent = `No baseline for ${which} under the current filters.`;
        el.style.display = '';
    } else {
        el.style.display = 'none';
    }
}

/** Text for the default (auto) entry: the band the charts actually resolved to,
 *  named concretely. It may be widened or class-level, so say which. */
function defaultBandOptionText(r) {
    if (!r.resolved) return 'No baseline available';
    const [lo, hi] = r.resolved.elo_band;
    const notes = [];
    if (r.resolved.widened) notes.push('widened');
    if (r.resolved.tc_fallback) notes.push(`all ${r.resolved.time_class || 'time controls'}`);
    return `${lo}–${hi}  (${r.resolved.n_players.toLocaleString()} players)`
        + (notes.length ? `  ·  ${notes.join(', ')}` : '');
}

async function loadBaselineBands(username) {
    const sel = document.getElementById('baseline-band');
    if (!sel) return;
    try {
        const r = await fetchJSON(
            `/api/players/${username}/analytics/baseline-bands${buildFilterParams()}`);
        const previous = selectedBaselineBand;

        // The default entry stands in for the player's own band, so listing that
        // band again below would duplicate it. Only skip it when the resolver
        // landed on exactly that band — a widened default is a different range.
        const coveredByDefault = r.resolved && !r.resolved.widened && !r.resolved.tc_fallback
            ? r.resolved.elo_band[0]
            : null;

        sel.innerHTML = '';
        const auto = document.createElement('option');
        auto.value = '';
        auto.textContent = defaultBandOptionText(r);
        sel.appendChild(auto);

        // Descending: the strongest bands sit nearest the default entry, which
        // is where a player looking to compare upward will reach first.
        for (const b of [...r.bands].reverse()) {
            if (b.elo_lo === coveredByDefault) continue;
            const opt = document.createElement('option');
            opt.value = b.elo_lo;
            if (b.eligible) {
                opt.textContent = `${b.elo_lo}–${b.elo_hi}  (${b.n_players.toLocaleString()} players)`;
            } else {
                // A gap inside the ladder. Shown, but unselectable — the range
                // exists, we just don't have enough of it to draw a line from.
                opt.disabled = true;
                opt.textContent = `${b.elo_lo}–${b.elo_hi}  `
                    + (b.n_games
                        ? `(${b.n_players.toLocaleString()} players · too few)`
                        : '(no data)');
            }
            sel.appendChild(opt);
        }

        // Last: it is the fallback for when sample size matters more than a
        // like-for-like comparison, not a band anyone scans the list for.
        if (r.all_players && r.all_players.n_players) {
            const all = document.createElement('option');
            all.value = 'all';
            all.textContent = `All players  (${r.all_players.n_players.toLocaleString()} players)`;
            sel.appendChild(all);
        }
        // Selection is sticky across filter changes, even if the band just
        // dropped below the floor — the chart says so rather than reverting.
        sel.value = previous;
        if (previous && sel.value !== previous) {
            const opt = document.createElement('option');
            opt.value = previous;
            opt.textContent = `${previous}–${Number(previous) + 99}  (no data here)`;
            sel.appendChild(opt);
            sel.value = previous;
        }
    } catch (e) {
        console.warn('Baseline bands unavailable:', e);
    }
}

/** Redraw only what the baseline affects. refreshAll() would also refetch
 *  stats, Elo history, games and openings — none of which depend on the band,
 *  and the ~900ms it costs makes the control feel unresponsive. */
async function refreshBaselineOverlays() {
    for (const k of Object.keys(requestCache)) {
        if (k.includes('/baseline')) delete requestCache[k];
    }
    for (const k of Object.keys(baselineResults)) delete baselineResults[k];
    // Reload the ladder alongside the charts: the dropdown's player counts and
    // the overlay labels describe the same population, so refreshing one
    // without the other lets them drift apart as the database grows.
    await loadBaselineBands(currentUsername);
    loadColorAnalytics(currentUsername, currentOpeningColor, currentOpeningFilter);
}

async function onBaselineBandChange() {
    selectedBaselineBand = document.getElementById('baseline-band').value;
    await refreshBaselineOverlays();
}

async function toggleBaseline() {
    baselineEnabled = !baselineEnabled;
    // Inverted on purpose: lit means "press to bring the average back".
    document.getElementById('baseline-toggle').classList.toggle('active', !baselineEnabled);
    await refreshBaselineOverlays();
}


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
    winrateMode = 'color';
    document.getElementById('winrate-mode-color').classList.add('active');
    document.getElementById('winrate-mode-opening').classList.remove('active');
    document.querySelectorAll('.tc-btn').forEach(b => b.classList.remove('active'));
    document.querySelector('.tc-btn[data-tc="rapid"]').classList.add('active');

    // Reset to the Overall perspective so initRepertoireTabs loads global data
    currentOpeningColor = 'global';

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
        loadBaselineBands(currentUsername),
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

    loadColorAnalytics(currentUsername, currentOpeningColor, currentOpeningFilter);

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

    const compareKeys = ['loadGameLength', 'loadClockAdvantage', 'loadRatingDiff', 'loadMoveTimeDist', 'loadMoveTimeByMove', 'elo', 'loadWinrateByColor'];
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
                win_rate: tc.total ? (tc.wins / tc.total * 100) : 0,
                decisive_win_rate: decisive ? (tc.wins / decisive * 100) : 0,
                draw_rate: tc.total ? (tc.draws / tc.total * 100) : 0,
            };
        }

        const xElo = stats.total_games ? 8 * (stats.wins - stats.losses) / stats.total_games : 0;
        document.getElementById('compare-stats-grid').innerHTML = `
            <div class="stat-card"><div class="stat-label">Total Games</div><div class="stat-value">${stats.total_games.toLocaleString()}</div></div>
            <div class="stat-card win"><div class="stat-label">Wins</div><div class="stat-value">${stats.wins.toLocaleString()}</div></div>
            <div class="stat-card draw"><div class="stat-label">Draws</div><div class="stat-value">${stats.draws.toLocaleString()}</div></div>
            <div class="stat-card loss"><div class="stat-label">Losses</div><div class="stat-value">${stats.losses.toLocaleString()}</div></div>
            <div class="stat-card"><div class="stat-label">Exp. Elo / Game</div><div class="stat-value" style="color:${xEloColor(xElo, 1)}">${(xElo >= 0 ? '+' : '') + xElo.toFixed(1)}</div></div>
            <div class="stat-card accent"><div class="stat-label">Decisive Win Rate</div><div class="stat-value">${(stats.decisive_win_rate ?? 0).toFixed(1)}%</div></div>
            <div class="stat-card draw"><div class="stat-label">Draw Rate</div><div class="stat-value">${(stats.draw_rate ?? 0).toFixed(1)}%</div></div>
            <div class="stat-card win"><div class="stat-label">Win Rate</div><div class="stat-value">${stats.win_rate.toFixed(1)}%</div></div>
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
                win_rate: tc.total ? (tc.wins / tc.total * 100) : 0,
                decisive_win_rate: decisive ? (tc.wins / decisive * 100) : 0,
                draw_rate: tc.total ? (tc.draws / tc.total * 100) : 0,
            };
        }

        const xElo = stats.total_games ? 8 * (stats.wins - stats.losses) / stats.total_games : 0;
        const eloValue = document.getElementById('val-exp-elo');
        eloValue.textContent = (xElo >= 0 ? '+' : '') + xElo.toFixed(1);
        eloValue.style.color = xEloColor(xElo, 1);
        document.getElementById('val-total').textContent = stats.total_games.toLocaleString();
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
            // One line on screen needs no colour coding, so it takes the app
            // accent; the "All" view keeps per-class colours to stay separable.
            const color = currentTimeClass
                ? ACCENT_LIGHT
                : (TIME_CLASS_COLORS[tc] || DEFAULT_COLOR);
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

function xEloColor(x, scale = 4) {
    // Lerp neutral slate → red/green, fully saturated at ±scale Elo per game.
    // Per-opening values spread wide (scale 4); whole-player averages are
    // much tighter, so the banner saturates at ±1.
    const t = Math.max(-1, Math.min(1, x / scale));
    const from = [148, 163, 184];
    const to   = t < 0 ? [239, 68, 68] : [34, 197, 94];
    const k = Math.abs(t);
    const c = from.map((f, i) => Math.round(f + (to[i] - f) * k));
    return `rgb(${c[0]}, ${c[1]}, ${c[2]})`;
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
    const tasks = [loadEloChart(currentUsername, '')];
    if (compareMode && currentCompareUsername)
        tasks.push(loadEloChart(currentCompareUsername, '-compare'));
    Promise.all(tasks).then(() => {
        if (compareMode && currentCompareUsername) syncYAxes('elo', 'elo-compare');
    });
}


/**
 * Extra tooltip lines for the win/loss/draw bucket charts. Chart.js already
 * prints the hovered dataset's own value, so any metric it's showing is
 * skipped here rather than repeated.
 */
function outcomeTooltipLines(row, items) {
    if (!row) return [];
    const shown = new Set(items.map(i => i.dataset.label));
    const lines = [];
    if (!shown.has('Win Rate (Decisive) %')) lines.push(`Win Rate (Decisive): ${row.win_rate_no_draws}%`);
    if (!shown.has('Draw Rate %')) lines.push(`Draw Rate: ${row.draw_rate}%`);
    lines.push(`Total Games: ${row.total_games}`);
    return lines;
}

function renderOpeningRow(o, showColorPip = false, totalRow = false) {
    const wPct = o.games ? o.wins   / o.games * 100 : 0;
    const dPct = o.games ? o.draws  / o.games * 100 : 0;
    const lPct = o.games ? o.losses / o.games * 100 : 0;
    const xElo = o.games ? 8 * (o.wins - o.losses) / o.games : 0;
    const xEloStr = (xElo >= 0 ? '+' : '') + xElo.toFixed(1);
    const pip  = (showColorPip || totalRow)
        ? `<span class="color-pip ${o.color}"></span>`
        : '';
    const cls  = totalRow ? ' class="opening-total-row"' : '';
    // Total rows switch the color perspective; opening rows set the opening
    // filter (data-op), with data-name reused by the "Filtered to X" chip.
    const opAttr = totalRow
        ? ` data-color-target="${o.color === 'all' ? 'global' : o.color}"`
        : ` data-op="${escapeHtml(o.filter || o.name)}" data-name="${escapeHtml(o.name)}"`;
    return `
        <tr${cls}${opAttr}>
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
            <td class="wr-elo" style="color:${xEloColor(xElo)}">${xEloStr}</td>
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
                    <th title="Expected Elo per game: +8 per win, -8 per loss, 0 per draw">Exp. Elo</th>
                </tr>
            </thead>
            <tbody>${footer}${rows}</tbody>
        </table>`;
}

/** Both colors' totals summed — the "All Games" row that resets every filter. */
function combinedTotals(totals) {
    const w = totals.white, b = totals.black;
    if (!w && !b) return null;
    const games  = (w?.games  || 0) + (b?.games  || 0);
    const wins   = (w?.wins   || 0) + (b?.wins   || 0);
    const draws  = (w?.draws  || 0) + (b?.draws  || 0);
    const losses = (w?.losses || 0) + (b?.losses || 0);
    const decisive = wins + losses;
    const pct = (n, d) => d ? Math.round(n / d * 1000) / 10 : 0;
    return {
        name: 'All Games', color: 'all', games, wins, draws, losses,
        win_rate: pct(wins, games),
        draw_rate: pct(draws, games),
        decisive_win_rate: pct(wins, decisive),
    };
}

/**
 * Render the three opening tables from the cached payload. Split out from the
 * fetch so the show-all toggle can re-render without refetching.
 */
function renderOpeningTables() {
    if (!lastTopOpenings) return;
    const totals = lastTopOpenings.totals || {};

    // Summary rows head every table: they double as the perspective switch, so
    // they must stay reachable from any view.
    const summaryRows = [
        combinedTotals(totals),
        totals.white ? { ...totals.white, name: 'White Pieces', color: 'white' } : null,
        totals.black ? { ...totals.black, name: 'Black Pieces', color: 'black' } : null,
    ].filter(Boolean);

    const renderTable = (el, openings, showColorPip) => {
        // Always rewritten, even when empty — leaving the previous render in
        // place would show the old filter's openings as if they were current.
        if (!el) return;
        if (openings.length === 0 && summaryRows.length === 0) {
            el.innerHTML = '<div class="table-empty">No games for this filter.</div>';
            return;
        }
        const shown = openingsExpanded ? openings : openings.slice(0, OPENINGS_PREVIEW_COUNT);
        let html = buildOpeningTable(shown, showColorPip, summaryRows);
        if (openings.length > OPENINGS_PREVIEW_COUNT) {
            const hint = openingsExpanded ? 'Show fewer openings' : `Show all ${openings.length} openings`;
            html += `<button class="openings-toggle${openingsExpanded ? ' expanded' : ''}"`
                  + ` onclick="toggleOpeningRows()" title="${hint}" aria-label="${hint}"><span>▼</span></button>`;
        }
        el.innerHTML = html;
        attachWinBarTooltips(el);
        attachOpeningRowFilters(el);
    };

    for (const color of ['white', 'black']) {
        // Tag each row with its color and keep the pip on: the perspective is
        // implicit, but the pip makes which side you're looking at obvious.
        const rows = (lastTopOpenings[color] || []).map(o => ({ ...o, color }));
        renderTable(document.getElementById(`opening-stats-table-${color}`), rows, true);
    }

    const combined = [
        ...(lastTopOpenings.white || []).map(o => ({ ...o, color: 'white' })),
        ...(lastTopOpenings.black || []).map(o => ({ ...o, color: 'black' })),
    ].sort((a, b) => b.games - a.games);
    renderTable(document.getElementById('opening-stats-table-global'), combined, true);
}

function toggleOpeningRows() {
    openingsExpanded = !openingsExpanded;
    renderOpeningTables();
    syncOpeningFilterUI(currentOpeningFilter);
}

async function initRepertoireTabs(username) {
    try {
        loadColorAnalytics(username, currentOpeningColor, currentOpeningFilter);

        lastTopOpenings = await fetchJSON(`/api/players/${username}/analytics/top-openings${buildFilterParams()}`);
        renderOpeningTables();

        // The rebuilt table may no longer list the active opening (e.g. it drops
        // out of the top N after a time-class change). Leaving the filter set
        // would strand it: nothing selectable to clear it, charts still filtered.
        if (currentOpeningFilter && !visibleOpeningRow(currentOpeningFilter)) {
            currentOpeningFilter = '';
            gamesPage = 0;
            loadColorAnalytics(username, currentOpeningColor, '');
            loadGames(username);
            if (compareMode && currentCompareUsername) loadGames(currentCompareUsername, '-compare');
            return;
        }

        // Tables were rebuilt above, after loadColorAnalytics ran — re-apply the
        // filter indicator so it survives the refresh.
        syncOpeningFilterUI(currentOpeningFilter);
    } catch (e) { console.error('Error loading top openings', e); }
}

/** The row for an opening in whichever stats table is currently shown. */
function visibleOpeningRow(op) {
    return [...document.querySelectorAll('.opening-stats-table tr[data-op]')].find(
        tr => tr.dataset.op === op && tr.offsetParent !== null
    );
}

/** Apply (or clear, when op is '') the opening filter across the dashboard. */
function applyOpeningFilter(op) {
    currentOpeningFilter = op;
    gamesPage = 0;
    loadColorAnalytics(currentUsername, currentOpeningColor, op);
    loadGames(currentUsername);
    if (compareMode && currentCompareUsername) loadGames(currentCompareUsername, '-compare');
}

/** Switch the Overall/White/Black perspective. */
function applyOpeningColor(color) {
    currentOpeningColor = color;
    // Each perspective lists different openings, so a filter picked in one
    // doesn't carry over.
    currentOpeningFilter = '';
    gamesPage = 0;
    loadColorAnalytics(currentUsername, color, '');
    loadGames(currentUsername);
    if (compareMode && currentCompareUsername) loadGames(currentCompareUsername, '-compare');
}

/**
 * Wire a table's rows: opening rows toggle the opening filter, and the
 * "White/Black Pieces" total rows toggle the color perspective. Both are
 * toggles — clicking the active one returns to the broader view.
 */
function attachOpeningRowFilters(container) {
    container.querySelectorAll('tr[data-op]').forEach(tr => {
        tr.classList.add('opening-row-clickable');
        tr.title = 'Filter charts and games by this opening';
        tr.addEventListener('click', () => {
            applyOpeningFilter(tr.dataset.op === currentOpeningFilter ? '' : tr.dataset.op);
        });
    });

    container.querySelectorAll('tr[data-color-target]').forEach(tr => {
        const color = tr.dataset.colorTarget;
        tr.classList.add('opening-row-clickable');
        tr.title = color === 'global'
            ? 'Show all games, clearing any opening filter'
            : (currentOpeningColor === color ? 'Back to all games' : `Show only games as ${color}`);
        tr.addEventListener('click', () => {
            // "All Games" always resets; a color row toggles off to the same place.
            applyOpeningColor(currentOpeningColor === color ? 'global' : color);
        });
    });
}

/**
 * Reflect the active opening filter in the places the user can see it: a
 * "Filtered to X" chip above the charts and on the games header, plus a
 * highlight on the selected row. Without these the filter only changes
 * content that sits screens below the table you clicked.
 */
function syncOpeningFilterUI(op) {
    let label = op;
    const selected = op && visibleOpeningRow(op);
    if (selected) label = selected.dataset.name || op;

    const row = document.getElementById('analytics-filter-row');
    if (row) {
        row.classList.toggle('hidden', !op);
        if (op) document.getElementById('analytics-filter-name').textContent = label;
    }

    const chip = document.getElementById('games-filter-chip');
    if (chip) {
        chip.classList.toggle('hidden', !op);
        if (op) document.getElementById('games-filter-name').textContent = label;
    }

    document.querySelectorAll('.opening-stats-table tr[data-op]').forEach(tr => {
        tr.classList.toggle('opening-row-active', !!op && tr.dataset.op === op);
    });

    // With no perspective tabs, the highlighted summary row is the only cue for
    // which games are on screen. "All Games" only counts as active when nothing
    // at all is filtered, otherwise it would contradict the opening filter.
    document.querySelectorAll('.opening-stats-table tr[data-color-target]').forEach(tr => {
        const target = tr.dataset.colorTarget;
        const active = target === 'global'
            ? (currentOpeningColor === 'global' && !op)
            : target === currentOpeningColor;
        tr.classList.toggle('opening-row-active', active);
    });
}

/**
 * Move times aren't comparable across bullet/blitz/rapid, so the Time Usage
 * charts are only meaningful for a single time control.
 */
function syncTimeUsageAvailability() {
    const available = !!currentTimeClass;
    document.getElementById('time-usage-note')?.classList.toggle('hidden', available);
    document.getElementById('time-usage-grid')?.classList.toggle('hidden', !available);
    return available;
}

function clearOpeningFilter() {
    applyOpeningFilter('');
}

function loadColorAnalytics(username, color, op) {
    // loadAnalyticsSection reads these globals, so keep them authoritative.
    currentUsername = username;
    currentOpeningColor = color;
    currentOpeningFilter = op;

    const overview = document.getElementById('opening-stats-overview');
    if (overview) {
        overview.classList.remove('hidden');
        document.getElementById('opening-stats-table-global').classList.toggle('hidden', color !== 'global');
        document.getElementById('opening-stats-table-white').classList.toggle('hidden', color !== 'white');
        document.getElementById('opening-stats-table-black').classList.toggle('hidden', color !== 'black');
    }

    syncOpeningFilterUI(op);
    syncTimeUsageAvailability();

    ++analyticsLoadId;
    if (compareMode && currentCompareUsername) ++compareLoadId;

    // A collapsed section's canvases have no layout box, so drawing into them
    // now would produce a mis-sized chart; toggleAnalyticsSection redraws instead.
    for (const name of ANALYTICS_SECTIONS) {
        if (!collapsedSections.has(name)) loadAnalyticsSection(name);
    }
}



function colorParams(color, op) {
    const ext = {};
    if (color !== 'global') ext.player_color = color;
    if (op) ext.opening_names = op; // fetchJSON handles encoding
    return buildFilterParamsExtra(ext);
}

// ═══════════════════════════════════════════════════════════
// Win Rate by Color Over Time (rolling 30-day EMA)
// ═══════════════════════════════════════════════════════════

function setWinrateMode(mode) {
    winrateMode = mode;
    document.getElementById('winrate-mode-color').classList.toggle('active', mode === 'color');
    document.getElementById('winrate-mode-opening').classList.toggle('active', mode === 'opening');
    if (!currentUsername || collapsedSections.has('form')) return;
    loadWinrateByColor(currentUsername, analyticsLoadId);
    if (compareMode && currentCompareUsername) {
        loadWinrateByColor(currentCompareUsername, compareLoadId, '-compare');
    }
}

let winrateWindowTimer = null;

function setWinrateWindow(val) {
    winrateWindow = parseInt(val, 10);
    // Update the label immediately for responsive feedback while dragging.
    document.getElementById('ema-window-label').textContent = winrateWindow;
    if (!currentUsername || collapsedSections.has('form')) return;
    // Debounce the (relatively expensive) chart reload so dragging stays smooth.
    clearTimeout(winrateWindowTimer);
    winrateWindowTimer = setTimeout(() => {
        loadWinrateByColor(currentUsername, analyticsLoadId);
        if (compareMode && currentCompareUsername) {
            loadWinrateByColor(currentCompareUsername, compareLoadId, '-compare');
        }
    }, 200);
}

async function loadWinrateByColor(username, loadId, suffix = '') {
    const chartKey = 'loadWinrateByColor' + suffix;
    const noDataEl = document.getElementById('winrate-color-no-data' + (suffix ? suffix : ''));
    const opening = winrateMode === 'opening';

    const windowParam = buildFilterParams()
        ? `&window_games=${winrateWindow}`
        : `?window_games=${winrateWindow}`;
    const url = opening
        ? `/api/players/${username}/analytics/winrate-vs-opening${buildFilterParams()}${windowParam}`
        : `/api/players/${username}/analytics/winrate-by-color${buildFilterParams()}${windowParam}`;

    const s1 = opening
        ? { label: 'vs 1.e4 Win', key: 'e4',    drawKey: 'e4_draw', color: '#fb923c', bg: 'rgba(251,146,60,0.08)' }
        : { label: 'White Win',   key: 'white',  drawKey: 'white_draw', color: '#e2e8f0', bg: 'rgba(226,232,240,0.08)' };
    const s2 = opening
        ? { label: 'vs 1.d4 Win', key: 'd4',    drawKey: 'd4_draw', color: '#34d399', bg: 'rgba(52,211,153,0.08)' }
        : { label: 'Black Win',   key: 'black',  drawKey: 'black_draw', color: '#6fbcd8', bg: 'rgba(111,188,216,0.08)' };

    try {
        const data = await fetchJSON(url);
        if (loadId !== (suffix ? compareLoadId : analyticsLoadId)) return;
        if (charts[chartKey]) charts[chartKey].destroy();

        const sparse = data.length < 5;
        if (noDataEl) noDataEl.classList.toggle('hidden', !sparse);
        if (sparse) return;

        const pts1     = data.filter(d => d[s1.key]     !== null).map(d => ({ x: toMs(d.date), y: d[s1.key] }));
        const pts2     = data.filter(d => d[s2.key]     !== null).map(d => ({ x: toMs(d.date), y: d[s2.key] }));
        const ptsDraw1 = data.filter(d => d[s1.drawKey] !== null).map(d => ({ x: toMs(d.date), y: d[s1.drawKey] }));
        const ptsDraw2 = data.filter(d => d[s2.drawKey] !== null).map(d => ({ x: toMs(d.date), y: d[s2.drawKey] }));
        const ptsOverall = opening ? [] : data.filter(d => d.overall !== null).map(d => ({ x: toMs(d.date), y: d.overall }));

        const xMin = Math.min(...data.map(d => toMs(d.date)));
        const xMax = Math.max(...data.map(d => toMs(d.date)));

        const drawLabel1 = opening ? 'vs 1.e4 Draw' : 'White Draw';
        const drawLabel2 = opening ? 'vs 1.d4 Draw' : 'Black Draw';

        charts[chartKey] = new Chart(document.getElementById('winrate-color-chart' + suffix).getContext('2d'), {
            type: 'line',
            data: {
                datasets: [
                    {
                        label: s1.label, data: pts1,
                        borderColor: opening ? s1.color : hexToRgba(s1.color, 0.8),
                        backgroundColor: s1.bg,
                        borderWidth: 2, pointRadius: 0, pointHitRadius: 20,
                        tension: 0, spanGaps: true,
                    },
                    {
                        label: s2.label, data: pts2,
                        borderColor: opening ? s2.color : hexToRgba(s2.color, 0.8),
                        backgroundColor: s2.bg,
                        borderWidth: 2, pointRadius: 0, pointHitRadius: 20,
                        tension: 0, spanGaps: true,
                    },
                    {
                        label: drawLabel1, data: ptsDraw1,
                        borderColor: hexToRgba(s1.color, 0.55),
                        backgroundColor: 'transparent',
                        borderWidth: 1.5, borderDash: [5, 4],
                        pointRadius: 0, pointHitRadius: 16,
                        tension: 0, spanGaps: true,
                    },
                    {
                        label: drawLabel2, data: ptsDraw2,
                        borderColor: hexToRgba(s2.color, 0.55),
                        backgroundColor: 'transparent',
                        borderWidth: 1.5, borderDash: [5, 4],
                        pointRadius: 0, pointHitRadius: 16,
                        tension: 0, spanGaps: true,
                    },
                    ...(opening ? [] : [{
                        label: 'Overall Win', data: ptsOverall,
                        borderColor: 'rgba(148,163,184,0.45)',
                        backgroundColor: 'transparent',
                        borderWidth: 1.5, pointRadius: 0, pointHitRadius: 16,
                        tension: 0, spanGaps: true,
                    }]),
                ]
            },
            options: {
                responsive: true, maintainAspectRatio: false, animation: false,
                plugins: {
                    legend: { position: 'top', labels: { boxWidth: 12, padding: 16 } },
                    tooltip: {
                        callbacks: {
                            title: items => items.length ? fmtDate(items[0].parsed.x) : '',
                            label: item => `${item.dataset.label}: ${item.parsed.y.toFixed(1)}%`,
                        }
                    }
                },
                scales: {
                    x: {
                        type: 'linear',
                        min: xMin, max: xMax,
                        ticks: { maxTicksLimit: 10, maxRotation: 0, callback: v => fmtDate(v) },
                        grid: { display: false },
                    },
                    y: {
                        min: 0, max: 100,
                        grid: { color: 'rgba(42,53,72,0.5)' },
                        ticks: { callback: v => v + '%' },
                        title: { display: true, text: `Rate (last ${winrateWindow} games)`, color: '#5a6a85', font: { size: 11 } },
                    },
                }
            }
        });
    } catch (e) { console.error('Winrate-by-color chart error:', e); }
}


// ═══════════════════════════════════════════════════════════
// Feature 1: Rating Differential (10pt buckets within ±50)
// ═══════════════════════════════════════════════════════════

/** Push the population line onto an already-built win-rate bar chart.
 *  Joins on the bucket label and matches the player's own line, which plots
 *  win_rate_no_draws — comparing against win_rate would mix two measures. */
// Marked so it can be kept out of the legend: one baseline entry is enough.
const BASELINE_VOLUME_LABEL = 'Population games (scaled to your total)';

/** Push the population line onto an already-built win-rate bar chart, plus a
 *  thin volume bar so the distribution is visible and not just the rate.
 *  Joins on the bucket label and matches the player's own line, which plots
 *  win_rate_no_draws — comparing against win_rate would mix two measures. */
function attachWinRateBaseline(chartKey, playerBuckets, popBuckets, meta, labelKey) {
    if (!popBuckets || !charts[chartKey]) return;
    const chart = charts[chartKey];

    // Volume, rescaled to the player's own game count so both sit on one axis:
    // the population has far more games, so raw counts would dwarf the bars.
    // These are counts rather than rates, so the per-bucket gate does not
    // apply — a thin bucket is a short bar, which is exactly the truth.
    const popTotal = popBuckets.reduce((sum, b) => sum + b.total_games, 0);
    const playerTotal = playerBuckets.reduce((sum, b) => sum + b.total_games, 0);
    if (popTotal > 0 && playerTotal > 0) {
        const byCount = new Map(popBuckets.map(b => [b[labelKey], b.total_games]));
        // Buckets too thin for a rate are drawn faint, so a bar with no line
        // over it reads as "not enough here" rather than as a missing line.
        const faded = playerBuckets.map(b =>
            (byCount.get(b[labelKey]) ?? 0) < BASELINE_MIN_BUCKET_GAMES);
        chart.data.datasets.push({
            type: 'bar',
            label: BASELINE_VOLUME_LABEL,
            data: playerBuckets.map(b =>
                Math.round((byCount.get(b[labelKey]) ?? 0) * playerTotal / popTotal)),
            backgroundColor: faded.map(f =>
                f ? 'rgba(125, 147, 184, 0.32)' : 'rgba(125, 147, 184, 0.97)'),
            borderWidth: 0,
            borderRadius: 2,
            // grouped:false overlays it on the player's stack instead of
            // splitting the category, which would halve the bars underneath.
            grouped: false,
            stack: 'baseline',
            barPercentage: 0.24,
            order: 5,
        });
        const legend = chart.options.plugins.legend || (chart.options.plugins.legend = {});
        const labels = legend.labels || (legend.labels = {});
        labels.filter = (item) => item.text !== BASELINE_VOLUME_LABEL;
    }

    // A win rate off a handful of games is noise, not a baseline. Thin buckets
    // become gaps in the line rather than misleading points — most visible on
    // streak reaction and the extreme rating-differential buckets.
    const byBucket = new Map(popBuckets
        .filter(b => b.total_games >= BASELINE_MIN_BUCKET_GAMES)
        .map(b => [b[labelKey], b.win_rate_no_draws]));
    const data = playerBuckets.map(b => byBucket.get(b[labelKey]) ?? null);

    // Every bucket gated out means there is nothing to draw; adding the dataset
    // would put a legend entry against an invisible line. The volume bars above
    // still stand on their own.
    if (!data.every(v => v === null)) {
        chart.data.datasets.push(baselineLineStyle({
            type: 'line',
            label: baselineLabel(meta),
            data,
            yAxisID: 'y2',
            // Break rather than bridge: drawing straight across a bucket we
            // just faded for being too thin would undo the signal.
            spanGaps: false,
        }));
    }

    chart.update();
}

async function loadRatingDiff(username, color, op, loadId, suffix = '') {
    const chartKey = "loadRatingDiff" + suffix;
    try {
        const [data, baseline] = await Promise.all([
            fetchJSON(`/api/players/${username}/analytics/rating-diff${colorParams(color, op)}`),
            fetchBaseline(username, 'rating-diff', color, op),
        ]);
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
                        borderColor: '#6fbcd8', backgroundColor: 'transparent',
                        borderWidth: 2, pointRadius: 4, pointBackgroundColor: '#6fbcd8', pointHitRadius: 24,
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
                            afterBody: (items) => outcomeTooltipLines(buckets[items[0].dataIndex], items)
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
        attachWinRateBaseline(chartKey, buckets, baseline && baseline.data, baseline && baseline.meta, 'bucket');

    } catch (e) { console.error('Rating diff error:', e); }
}


// ═══════════════════════════════════════════════════════════
// Feature 2: Game Length vs Win Rate
// ═══════════════════════════════════════════════════════════

async function loadGameLength(username, color, op, loadId, suffix = '') {
    const chartKey = "loadGameLength" + suffix;
    try {
        const [data, baseline] = await Promise.all([
            fetchJSON(`/api/players/${username}/analytics/game-length${colorParams(color, op)}`),
            fetchBaseline(username, 'game-length', color, op),
        ]);
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
                        borderColor: '#6fbcd8', backgroundColor: 'transparent',
                        borderWidth: 2, pointRadius: 4, pointBackgroundColor: '#6fbcd8', pointHitRadius: 24,
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
                            afterBody: (items) => outcomeTooltipLines(data[items[0].dataIndex], items)
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
        attachWinRateBaseline(chartKey, data, baseline && baseline.data, baseline && baseline.meta, 'bucket');
    } catch (e) { console.error('Game length error:', e); }
}




// ═══════════════════════════════════════════════════════════
// Win Rate After a Streak
// ═══════════════════════════════════════════════════════════

function renderStreakChart(chartKey, canvasId, buckets, singular, plural, popBuckets = null, meta = null) {
    if (charts[chartKey]) charts[chartKey].destroy();
    const el = document.getElementById(canvasId);
    if (!el) return;

    charts[chartKey] = new Chart(el.getContext('2d'), {
        type: 'bar',
        data: {
            labels: buckets.map(b => `${b.bucket} ${b.bucket === '1' ? singular : plural}`),
            datasets: [
                { label: 'Wins', data: buckets.map(b => b.wins), backgroundColor: 'rgba(34, 197, 94, 0.7)', borderRadius: 4, stack: 'stack' },
                { label: 'Losses', data: buckets.map(b => b.losses), backgroundColor: 'rgba(239, 68, 68, 0.7)', borderRadius: 4, stack: 'stack' },
                { label: 'Draws', data: buckets.map(b => b.draws), backgroundColor: 'rgba(234, 179, 8, 0.7)', borderRadius: 4, stack: 'stack' },
                {
                    label: 'Win Rate (Decisive) %', type: 'line',
                    data: buckets.map(b => b.win_rate_no_draws),
                    borderColor: '#6fbcd8', backgroundColor: 'transparent',
                    borderWidth: 2, pointRadius: 4, pointBackgroundColor: '#6fbcd8', pointHitRadius: 24,
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
                        afterBody: (items) => outcomeTooltipLines(buckets[items[0].dataIndex], items)
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
    attachWinRateBaseline(chartKey, buckets, popBuckets, meta, 'bucket');
}

async function loadStreakReaction(username, loadId, suffix = '') {
    try {
        const [data, baseline] = await Promise.all([
            fetchJSON(`/api/players/${username}/analytics/streak-reaction${buildFilterParams()}`),
            fetchBaseline(username, 'streak-reaction', null, ''),
        ]);
        if (loadId !== (suffix ? compareLoadId : analyticsLoadId)) return;

        const meta = baseline ? baseline.meta : null;
        renderStreakChart('loadStreakLoss' + suffix, 'streak-loss-chart' + suffix, data.after_loss, 'Loss', 'Losses',
                          baseline && baseline.data.after_loss, meta);
        renderStreakChart('loadStreakWin' + suffix, 'streak-win-chart' + suffix, data.after_win, 'Win', 'Wins',
                          baseline && baseline.data.after_win, meta);
    } catch (e) { console.error('Streak reaction error:', e); }
}


// ═══════════════════════════════════════════════════════════
// Clock Advantage (with key/legend)
// ═══════════════════════════════════════════════════════════

async function loadClockAdvantage(username, color, op, loadId, suffix = '') {
    const chartKey = "loadClockAdvantage" + suffix;
    try {
        const [data, baseline] = await Promise.all([
            fetchJSON(`/api/players/${username}/analytics/clock-advantage${colorParams(color, op)}`),
            fetchBaseline(username, 'clock-advantage', color, op),
        ]);
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
                        borderColor: '#6fbcd8', backgroundColor: 'transparent',
                        borderWidth: 2, pointRadius: 4, pointBackgroundColor: '#6fbcd8', pointHitRadius: 24,
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
                            afterBody: (items) => outcomeTooltipLines(data[items[0].dataIndex], items)
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
        attachWinRateBaseline(chartKey, data, baseline && baseline.data, baseline && baseline.meta, 'clock_bucket');
    } catch (e) { console.error('Clock advantage error:', e); }
}


// ═══════════════════════════════════════════════════════════
// Move Time Distribution & Avg Think Time by Move Number
// ═══════════════════════════════════════════════════════════


async function loadMoveTime(username, color, op, loadId, suffix = '') {
    const distKey = "loadMoveTimeDist" + suffix;
    const moveKey = "loadMoveTimeByMove" + suffix;
    try {
        const [data, baseline] = await Promise.all([
            fetchJSON(`/api/players/${username}/analytics/move-time${colorParams(color, op)}`),
            fetchBaseline(username, 'move-time', color, op),
        ]);
        if (loadId !== (suffix ? compareLoadId : analyticsLoadId)) return;

        if (charts[distKey]) charts[distKey].destroy();
        if (charts[moveKey]) charts[moveKey].destroy();

        // ── Distribution histogram ──
        const distDatasets = [{
            label: 'Moves',
            data: data.buckets.map(b => b.count),
            backgroundColor: 'rgba(111, 188, 216, 0.7)',
            borderRadius: 4,
        }];
        if (baseline) {
            distDatasets.push(baselineLineStyle({
                type: 'line',
                label: baselineLabel(baseline.meta),
                data: baseline.data.buckets.map(b => b.pct),
                yAxisID: 'yPct',
            }));
        }
        charts[distKey] = new Chart(document.getElementById("move-time-dist-chart" + suffix).getContext('2d'), {
            type: 'bar',
            data: {
                labels: data.buckets.map(b => b.label),
                datasets: distDatasets,
            },
            options: {
                responsive: true, maintainAspectRatio: false, animation: false,
                plugins: {
                    legend: { display: !!baseline, position: 'top', labels: { boxWidth: 20, font: { size: 11 } } },
                    tooltip: {
                        callbacks: {
                            label: (item) => {
                                if (item.datasetIndex === 0) {
                                    const b = data.buckets[item.dataIndex];
                                    return `${b.count.toLocaleString()} moves (${b.pct}%)`;
                                }
                                return `${item.dataset.label}: ${item.formattedValue}% of moves`;
                            }
                        }
                    }
                },
                scales: {
                    x: { grid: { display: false } },
                    y: { grid: { color: 'rgba(42, 53, 72, 0.5)' }, title: { display: true, text: 'Moves', color: '#5a6a85' } },
                    yPct: {
                        display: !!baseline,
                        position: 'right',
                        grid: { display: false },
                        title: { display: true, text: '% of moves', color: '#5a6a85' },
                        ticks: { callback: v => v + '%' },
                    },
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
        const datasets = [{
            label: 'Avg seconds',
            data: byMove.map(d => d.avg_seconds),
            borderColor: '#6fbcd8',
            backgroundColor: 'rgba(111, 188, 216, 0.08)',
            fill: true,
            tension: 0.3,
            pointRadius: 3,
            pointHitRadius: 20,
            borderWidth: 2,
        }];
        // Align the population curve to the player's move-number axis; the two
        // series can end at different moves.
        let baselineByMove = null;
        if (baseline) {
            const popByMove = new Map(
                baseline.data.by_move_number.map(d => [d.move_number, d.avg_seconds]));
            baselineByMove = byMove.map(d => popByMove.get(d.move_number) ?? null);
            datasets.push(baselineLineStyle({
                label: baselineLabel(baseline.meta),
                data: baselineByMove,
                spanGaps: true,
            }));
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
                    legend: { display: !!baseline, position: 'top', labels: { boxWidth: 20, font: { size: 11 } } },
                    tooltip: {
                        callbacks: {
                            title: (items) => `Move ${items[0].label}`,
                            label: (item) => {
                                if (item.datasetIndex === 0) {
                                    const d = byMove[item.dataIndex];
                                    return `Avg: ${d.avg_seconds}s  (${d.count} moves)`;
                                }
                                return `${item.dataset.label}: ${item.formattedValue}s`;
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
            // Peak think move: where the curve actually peaks, for each series.
            const peakOf = (pairs) => {
                let best = null, bestVal = -Infinity;
                for (const [mn, v] of pairs) {
                    if (v !== null && v > bestVal) { bestVal = v; best = mn; }
                }
                return best;
            };
            const playerPeak = peakOf(byMove.map(d => [d.move_number, d.avg_seconds]));
            const popPeak = baselineByMove
                ? peakOf(byMove.map((d, i) => [d.move_number, baselineByMove[i]]))
                : null;

            const mins = Math.floor(totalSec / 60);
            const secs = Math.round(totalSec % 60);
            const timeStr = mins > 0 ? `${mins}m ${secs}s` : `${secs}s`;
            statsEl.innerHTML = `
                <div style="display: flex; flex-direction: column; gap: 0.6rem; padding: 0.5rem 0;">
                    <div style="padding: 0.6rem 0.75rem; border-left: 3px solid #475569;">
                        <div style="font-size: 0.78rem; color: var(--text-muted); margin-bottom: 0.15rem;">Avg time per game</div>
                        <div style="font-size: 1.1rem; font-weight: 700; color: var(--text-primary);">${timeStr}</div>
                    </div>
                    <div style="padding: 0.6rem 0.75rem; border-left: 3px solid #475569;">
                        <div style="font-size: 0.78rem; color: var(--text-muted); margin-bottom: 0.15rem;">Mean effort move</div>
                        <div style="font-size: 1.1rem; font-weight: 700; color: var(--text-primary);">move ${medianEffortMove}</div>
                    </div>
                    <div style="padding: 0.6rem 0.75rem; border-left: 3px solid #475569;">
                        <div style="font-size: 0.78rem; color: var(--text-muted); margin-bottom: 0.15rem;">Peak think move</div>
                        <div style="font-size: 1.1rem; font-weight: 700; color: var(--text-primary);">${playerPeak === null ? '—' : 'move ' + playerPeak}</div>
                        ${popPeak !== null ? `<div style="font-size: 0.78rem; color: var(--text-muted); margin-top: 0.15rem;">average: move ${popPeak}</div>` : ''}
                    </div>
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
        if (currentOpeningColor !== 'global') extras.player_color = currentOpeningColor;
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

// ═══════════════════════════════════════════════════════════
// Collapsible analytics sections
// ═══════════════════════════════════════════════════════════

/**
 * Collapse/expand one analytics section. Charts in a collapsed section are
 * skipped on load (Chart.js can't size a display:none canvas), so expanding
 * one has to draw whatever it missed.
 */
function toggleAnalyticsSection(name) {
    const section = document.querySelector(`.analytics-section[data-section="${name}"]`);
    if (!section) return;

    const collapsed = section.classList.toggle('collapsed');
    if (collapsed) collapsedSections.add(name); else collapsedSections.delete(name);

    if (!collapsed && currentUsername) loadAnalyticsSection(name);
}

/** Draw the charts belonging to one section, for both compare columns. */
function loadAnalyticsSection(name) {
    const color = currentOpeningColor;
    const op = currentOpeningFilter;
    const id = analyticsLoadId;
    const cid = compareLoadId;
    const withCompare = compareMode && currentCompareUsername;

    if (name === 'outcomes') {
        loadRatingDiff(currentUsername, color, op, id);
        loadGameLength(currentUsername, color, op, id);
        if (withCompare) {
            loadRatingDiff(currentCompareUsername, color, op, cid, '-compare');
            loadGameLength(currentCompareUsername, color, op, cid, '-compare');
        }
    } else if (name === 'time') {
        if (!syncTimeUsageAvailability()) return;
        loadClockAdvantage(currentUsername, color, op, id);
        loadMoveTime(currentUsername, color, op, id);
        if (withCompare) {
            loadClockAdvantage(currentCompareUsername, color, op, cid, '-compare');
            loadMoveTime(currentCompareUsername, color, op, cid, '-compare');
        }
    } else if (name === 'form') {
        loadWinrateByColor(currentUsername, id);
        loadStreakReaction(currentUsername, id);
        if (withCompare) {
            loadWinrateByColor(currentCompareUsername, cid, '-compare');
            loadStreakReaction(currentCompareUsername, cid, '-compare');
        }
    }
}
