/* Move quality: per-game inaccuracies, mistakes, blunders and misses.
 *
 * Engine coverage is a fraction of the corpus, so the empty state is the
 * normal state and says so rather than rendering an empty table. */
/* global fetchJSON, colorParams, queryColor, currentOpeningFilter */

const MQ_TIERS = ['inaccuracies', 'mistakes', 'blunders', 'misses'];

function mqPct(n, d) {
    return d ? ((100 * n) / d).toFixed(1) + '%' : '—';
}

async function loadMoveQuality(username) {
    const section = document.getElementById('move-quality-section');
    let data;
    try {
        data = await fetchJSON(
            `/api/players/${username}/analytics/move-quality`
            + colorParams(queryColor(), currentOpeningFilter)
        );
    } catch {
        section.classList.add('hidden');
        return;
    }
    const t = data.totals;
    section.classList.remove('hidden');

    const label = document.getElementById('mq-coverage-label');
    if (!t.games_analyzed) {
        label.textContent = 'No engine-analyzed games in this selection';
        document.getElementById('mq-totals').innerHTML = '';
        document.getElementById('mq-table').innerHTML = '';
        return;
    }
    label.textContent =
        `${t.games_analyzed} analyzed games, ${t.moves_scored.toLocaleString()} scored moves`;

    document.getElementById('mq-totals').innerHTML = MQ_TIERS.map((k) => `
        <div class="stat-card">
            <div class="stat-label">${k[0].toUpperCase() + k.slice(1)}</div>
            <div class="stat-value">${(t[k] / t.games_analyzed).toFixed(2)}</div>
            <div class="stat-sub">per game · ${mqPct(t[k], t.moves_scored)} of moves</div>
        </div>`).join('');

    document.getElementById('mq-table').innerHTML = `
        <thead><tr>
            <th>Date</th><th>Opening</th><th>Moves</th>
            <th>Inacc</th><th>Mist</th><th>Blun</th><th>Miss</th><th></th>
        </tr></thead>
        <tbody>${data.games.map((g) => `
            <tr class="mq-row" data-game="${g.game_id}" data-url="${g.chess_com_url || ''}">
                <td>${g.date_played || '—'}</td>
                <td class="mq-opening">${g.opening_name || '—'}</td>
                <td>${g.moves_scored}</td>
                <td>${g.inaccuracies}</td>
                <td>${g.mistakes}</td>
                <td class="mq-blunder">${g.blunders}</td>
                <td>${g.misses}</td>
                <td><button class="btn-sm" onclick="toggleMqDrill(${g.game_id})">Moves</button></td>
            </tr>
            <tr class="mq-drill hidden" id="mq-drill-${g.game_id}">
                <td colspan="8"></td>
            </tr>`).join('')}
        </tbody>`;
}

async function toggleMqDrill(gameId) {
    const row = document.getElementById(`mq-drill-${gameId}`);
    if (!row.classList.contains('hidden')) { row.classList.add('hidden'); return; }
    row.classList.remove('hidden');

    const cell = row.firstElementChild;
    cell.textContent = 'Loading…';
    const data = await (await fetch(`/api/games/${gameId}/move-quality`)).json();
    if (!data.moves.length) { cell.textContent = 'No flagged moves.'; return; }

    const url = document.querySelector(`.mq-row[data-game="${gameId}"]`)?.dataset.url || '';
    cell.innerHTML = `<table class="mq-moves"><tbody>${data.moves.map((m) => `
        <tr>
            <td>${m.move_number}${m.color === 'white' ? '.' : '...'} ${m.move_san}</td>
            <td class="mq-tier-${m.tier || 'none'}">${m.tier || ''}${m.is_miss ? ' · miss' : ''}</td>
            <td>${(100 * m.wp_before).toFixed(0)}% → ${(100 * m.wp_after).toFixed(0)}%</td>
            <td>${m.time_spent_seconds != null ? m.time_spent_seconds.toFixed(1) + 's' : '—'}</td>
        </tr>`).join('')}</tbody></table>
        ${url ? `<a class="mq-link" href="${url}" target="_blank" rel="noopener">Open on chess.com</a>` : ''}`;
}
