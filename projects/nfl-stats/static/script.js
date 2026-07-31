/**
 * NFL Stats – main frontend script
 * Talks to the Flask backend at /api/*
 */

'use strict';

// ---------------------------------------------------------------------------
// Application state
// ---------------------------------------------------------------------------
const state = {
  teams: [],          // [{id, name, shortName, abbreviation, logo, color, …}]
  stats: {},          // { "KC": { offense: […], defense: […] }, … }
  dataNote: null,     // string | null
  currentSeason: 2025,
  currentWeek: 18,
  selectedTeam: null, // abbreviation of open detail panel
};

// ---------------------------------------------------------------------------
// Position → badge colours
// ---------------------------------------------------------------------------
// Light theme: soft tinted chip, colored border, dark readable text (WCAG AA).
const POS_STYLES = {
  // Offense
  QB:  { bg: '#DBEAFE', border: '#2563EB', text: '#1E3A8A' },
  RB:  { bg: '#DCFCE7', border: '#16A34A', text: '#14532D' },
  FB:  { bg: '#DCFCE7', border: '#16A34A', text: '#14532D' },
  WR:  { bg: '#F3E8FF', border: '#9333EA', text: '#581C87' },
  TE:  { bg: '#CCFBF1', border: '#0D9488', text: '#134E4A' },
  K:   { bg: '#FEF3C7', border: '#D97706', text: '#78350F' },
  P:   { bg: '#FEF3C7', border: '#D97706', text: '#78350F' },
  // Defense
  LB:  { bg: '#FEE2E2', border: '#DC2626', text: '#7F1D1D' },
  OLB: { bg: '#FEE2E2', border: '#DC2626', text: '#7F1D1D' },
  ILB: { bg: '#FEE2E2', border: '#DC2626', text: '#7F1D1D' },
  MLB: { bg: '#FEE2E2', border: '#DC2626', text: '#7F1D1D' },
  CB:  { bg: '#FFEDD5', border: '#EA580C', text: '#7C2D12' },
  S:   { bg: '#FFEDD5', border: '#EA580C', text: '#7C2D12' },
  FS:  { bg: '#FFEDD5', border: '#EA580C', text: '#7C2D12' },
  SS:  { bg: '#FFEDD5', border: '#EA580C', text: '#7C2D12' },
  DB:  { bg: '#FFEDD5', border: '#EA580C', text: '#7C2D12' },
  DE:  { bg: '#EDE9FE', border: '#7C3AED', text: '#4C1D95' },
  DT:  { bg: '#EDE9FE', border: '#7C3AED', text: '#4C1D95' },
  NT:  { bg: '#EDE9FE', border: '#7C3AED', text: '#4C1D95' },
  DL:  { bg: '#EDE9FE', border: '#7C3AED', text: '#4C1D95' },
};

function posBadgeStyle(position) {
  const pos = (position || '').toUpperCase().trim();
  const c = POS_STYLES[pos];
  if (!c) {
    return 'background:#EAF0F9;border:1px solid #B9C8E0;color:#4A5B7D;';
  }
  return `background:${c.bg};border:1px solid ${c.border};color:${c.text};`;
}

// ---------------------------------------------------------------------------
// Key-stat formatter
// ---------------------------------------------------------------------------
function formatKeyStat(player, side) {
  const s = player.stats || {};
  const pos = (player.position || '').toUpperCase().trim();

  if (side === 'defense') {
    const tkl = Number(s.def_tackles  || 0);
    const sck = Number(s.def_sacks    || 0);
    const int = Number(s.def_interceptions || 0);
    const pd  = Number(s.def_pass_defended || 0);
    const parts = [];
    if (tkl)            parts.push(`${tkl} tkl`);
    if (sck)            parts.push(`${sck} sck`);
    if (int)            parts.push(`${int} int`);
    if (pd && !int)     parts.push(`${pd} pd`);
    return parts.length ? parts.join(' · ') : 'No stats';
  }

  // Offense
  const py  = Number(s.passing_yards   || 0);
  const pt  = Number(s.passing_tds     || 0);
  const ry  = Number(s.rushing_yards   || 0);
  const rt  = Number(s.rushing_tds     || 0);
  const rec = Number(s.receptions      || 0);
  const recy= Number(s.receiving_yards || 0);
  const rect= Number(s.receiving_tds   || 0);
  const fp  = Number(player.score || s.fantasy_points_ppr || 0);

  if (pos === 'QB') {
    if (py) {
      const str = `${py.toLocaleString()} pass yds`;
      return pt ? `${str} · ${pt} TD` : str;
    }
  }
  if (pos === 'RB' || pos === 'FB') {
    if (ry) {
      const str = `${ry.toLocaleString()} rush yds`;
      return rt ? `${str} · ${rt} TD` : str;
    }
  }
  if (pos === 'WR' || pos === 'TE') {
    if (recy) {
      const str = `${rec} rec · ${recy.toLocaleString()} yds`;
      return rect ? `${str} · ${rect} TD` : str;
    }
  }
  // Generic fallback
  if (py)   return `${py.toLocaleString()} pass yds`;
  if (ry)   return `${ry.toLocaleString()} rush yds`;
  if (recy) return `${recy.toLocaleString()} rec yds`;
  if (fp)   return `${fp.toFixed(1)} fpts`;
  return 'No stats';
}

// ---------------------------------------------------------------------------
// SVG silhouette for missing headshots
// ---------------------------------------------------------------------------
function silhouetteHTML() {
  return `<div class="player-headshot-placeholder" aria-hidden="true">
    <svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">
      <path d="M12 12c2.76 0 5-2.24 5-5s-2.24-5-5-5-5 2.24-5 5 2.24 5 5 5zm0 2c-3.33
               0-10 1.67-10 5v2h20v-2c0-3.33-6.67-5-10-5z"/>
    </svg>
  </div>`;
}

// ---------------------------------------------------------------------------
// DOM helpers
// ---------------------------------------------------------------------------
const qs  = (sel, ctx = document) => ctx.querySelector(sel);
const qsa = (sel, ctx = document) => [...ctx.querySelectorAll(sel)];

// ---------------------------------------------------------------------------
// API calls
// ---------------------------------------------------------------------------
async function apiGet(path) {
  const res = await fetch(path);
  if (!res.ok) throw new Error(`HTTP ${res.status} — ${path}`);
  return res.json();
}

async function fetchCurrentWeek() {
  try {
    return await apiGet('/api/current-week');
  } catch (e) {
    console.warn('[nfl-stats] fetchCurrentWeek:', e.message);
    return { season: 2025, week: 18 };
  }
}

async function fetchTeams() {
  return apiGet('/api/teams');
}

async function fetchStats(season, week) {
  return apiGet(`/api/stats?season=${season}&week=${week}`);
}

async function fetchDataStatus() {
  try {
    return await apiGet('/api/data-status');
  } catch (e) {
    console.warn('[nfl-stats] fetchDataStatus:', e.message);
    return { live_season: 2026, live_week: 1, is_live: false,
             data_season: 2024, data_week: 18, default_season: 2024, default_week: 18 };
  }
}

// ---------------------------------------------------------------------------
// Live vs historical data mode
// ---------------------------------------------------------------------------
let DATA_STATUS = null;

function modeFor(season) {
  const s = DATA_STATUS || {};
  season = parseInt(season, 10);
  if (season === s.live_season && s.is_live) {
    return { mode: 'live', label: '🔴 LIVE NOW',
             note: `Real ${season} season — scores update as games are played.` };
  }
  if (season === s.live_season && !s.is_live) {
    return { mode: 'upcoming', label: '⏳ NOT STARTED YET',
             note: `The ${season} season hasn't kicked off — showing last season's real stats until it does.` };
  }
  return { mode: 'historical', label: `📚 ${season} FINAL`,
           note: `Last season's real numbers (${season}, completed) — these are not live scores.` };
}

function renderModeBadge(season) {
  const el = qs('#data-mode-badge');
  if (!el) return;
  const m = modeFor(season);
  el.className = `data-mode-badge mode-${m.mode}`;
  el.innerHTML = `<span class="dm-pill">${m.label}</span><span class="dm-note">${m.note}</span>`;
  el.style.display = 'flex';
}

// ---------------------------------------------------------------------------
// Player search (look up any player's season totals, no league needed)
// ---------------------------------------------------------------------------
let SEARCH_POOL = null;
const TEAM_FULL = {ARI:'cardinals arizona',ATL:'falcons atlanta',BAL:'ravens baltimore',BUF:'bills buffalo',
  CAR:'panthers carolina',CHI:'bears chicago',CIN:'bengals cincinnati',CLE:'browns cleveland',DAL:'cowboys dallas',
  DEN:'broncos denver',DET:'lions detroit',GB:'packers green bay',HOU:'texans houston',IND:'colts indianapolis',
  JAX:'jaguars jacksonville',KC:'chiefs kansas city',LV:'raiders las vegas',LAC:'chargers los angeles',
  LAR:'rams los angeles',MIA:'dolphins miami',MIN:'vikings minnesota',NE:'patriots new england',NO:'saints new orleans',
  NYG:'giants new york',NYJ:'jets new york',PHI:'eagles philadelphia',PIT:'steelers pittsburgh',
  SF:'49ers san francisco niners',SEA:'seahawks seattle',TB:'buccaneers bucs tampa bay',TEN:'titans tennessee',
  WAS:'commanders washington'};

async function ensureSearchPool() {
  if (SEARCH_POOL) return SEARCH_POOL;
  const season = (DATA_STATUS && DATA_STATUS.data_season) || 2024;
  try {
    SEARCH_POOL = await apiGet(`/api/pool?season=${season}`);
  } catch (e) {
    SEARCH_POOL = [];
  }
  return SEARCH_POOL;
}

async function renderPlayerSearch() {
  const input = qs('#player-search');
  const out = qs('#player-search-results');
  if (!input || !out) return;
  const q = (input.value || '').trim().toLowerCase();
  if (!q) { out.innerHTML = ''; out.style.display = 'none'; return; }
  const pool = await ensureSearchPool();
  const season = (DATA_STATUS && DATA_STATUS.data_season) || 2024;
  const matches = pool.filter(p => {
    const tm = (p.nfl_team || '').toUpperCase();
    const hay = (p.name + ' ' + tm + ' ' + (p.position || '') + ' ' + (TEAM_FULL[tm] || '')).toLowerCase();
    return hay.includes(q);
  }).slice(0, 40);
  out.style.display = 'block';
  if (!matches.length) {
    out.innerHTML = `<div class="ps-empty">No player matches “${input.value}”.</div>`;
    return;
  }
  out.innerHTML = `<div class="ps-head">${matches.length} player${matches.length===1?'':'s'} · ${season} season totals</div>` +
    matches.map(p => {
      const badge = posBadgeStyle(p.position);
      const shot = p.headshot
        ? `<img class="ps-shot" src="${p.headshot}" alt="" loading="lazy" onerror="this.style.visibility='hidden'">`
        : `<span class="ps-shot ps-shot-blank" aria-hidden="true"></span>`;
      return `<div class="ps-row">${shot}
        <span class="ps-name">${p.name}</span>
        <span class="pos-badge" style="${badge}">${p.position || '?'}</span>
        <span class="ps-team">${p.nfl_team || ''}</span>
        <span class="ps-pts">${p.total} pts</span></div>`;
    }).join('');
}

// ---------------------------------------------------------------------------
// Loading / error states
// ---------------------------------------------------------------------------
function showLoading(msg) {
  const el = qs('#loading-state');
  if (el) {
    el.style.display = 'flex';
    const txt = el.querySelector('.loading-text');
    if (txt && msg) txt.textContent = msg;
  }
  const grid = qs('#team-grid');
  const err  = qs('#error-state');
  if (grid) grid.style.display = 'none';
  if (err)  err.style.display  = 'none';
}

function showError(msg) {
  const el = qs('#error-state');
  if (el) {
    el.style.display = 'flex';
    const txt = el.querySelector('.error-text');
    if (txt) txt.textContent = msg || 'Stats unavailable for this week';
  }
  const spin = qs('#loading-state');
  const grid = qs('#team-grid');
  if (spin) spin.style.display = 'none';
  if (grid) grid.style.display = 'none';
}

function showGrid() {
  const grid = qs('#team-grid');
  const spin = qs('#loading-state');
  const err  = qs('#error-state');
  if (grid) grid.style.display = '';
  if (spin) spin.style.display = 'none';
  if (err)  err.style.display  = 'none';
}

function setDataNote(note) {
  const el = qs('#data-note');
  if (!el) return;
  if (note) {
    el.innerHTML = `<span class="note-icon">ℹ</span><span>${note}</span>`;
    el.style.display = 'flex';
  } else {
    el.style.display = 'none';
    el.innerHTML = '';
  }
}

function updateWeekLabel() {
  const el = qs('#week-label');
  if (el) el.textContent = `${state.currentSeason} · Week ${state.currentWeek}`;
}

// ---------------------------------------------------------------------------
// Selector builders
// ---------------------------------------------------------------------------
function buildSeasonSelect(currentSeason) {
  const sel = qs('#season-select');
  if (!sel) return;
  sel.innerHTML = '';
  [2023, 2024, 2025, 2026].forEach(yr => {
    const opt = document.createElement('option');
    opt.value = yr;
    opt.textContent = yr;
    if (yr === currentSeason) opt.selected = true;
    sel.appendChild(opt);
  });
}

function buildWeekSelect(currentWeek) {
  const sel = qs('#week-select');
  if (!sel) return;
  sel.innerHTML = '';
  for (let w = 1; w <= 18; w++) {
    const opt = document.createElement('option');
    opt.value = w;
    opt.textContent = `Week ${w}`;
    if (w === currentWeek) opt.selected = true;
    sel.appendChild(opt);
  }
}

// ---------------------------------------------------------------------------
// Team grid
// ---------------------------------------------------------------------------
function renderTeamGrid() {
  const grid = qs('#team-grid');
  if (!grid) return;

  if (state.teams.length === 0) {
    grid.innerHTML = '<p style="color:#4A5B7D;font-weight:700;grid-column:1/-1;text-align:center;padding:40px">No teams found.</p>';
    return;
  }

  grid.innerHTML = state.teams.map(team => {
    const abbr    = team.abbreviation;
    const tStats  = state.stats[abbr];
    const hasData = tStats && (tStats.offense.length > 0 || tStats.defense.length > 0);
    const color   = team.color || '#3b82f6';

    return `
      <div class="team-card" data-abbr="${abbr}" style="--team-color:${color}" role="button"
           tabindex="0" aria-label="${team.name} stats">
        <div class="team-card-accent" style="background:${color}"></div>
        <div class="team-card-inner">
          ${team.logo
            ? `<img class="team-logo" src="${team.logo}" alt="${team.name}" loading="lazy"
                    onerror="this.style.opacity='0'">`
            : `<div class="team-logo-fallback">${abbr}</div>`}
          <div class="team-card-name">${team.shortName || team.name}</div>
          <div class="team-card-abbr">${abbr}</div>
        </div>
        ${!hasData ? '<div class="team-card-no-data">no data</div>' : ''}
      </div>`;
  }).join('');

  // Attach listeners
  qsa('.team-card').forEach(card => {
    const open = () => {
      const abbr = card.dataset.abbr;
      const team = state.teams.find(t => t.abbreviation === abbr);
      if (team) openDetail(team);
    };
    card.addEventListener('click', open);
    card.addEventListener('keydown', e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); open(); } });
  });
}

// ---------------------------------------------------------------------------
// Detail overlay
// ---------------------------------------------------------------------------
function buildPlayerHTML(player, side, rank) {
  const stat      = formatKeyStat(player, side);
  const badgeStyle = posBadgeStyle(player.position);
  let headshot;
  if (player.headshot_url) {
    headshot = `<img class="player-headshot" src="${player.headshot_url}"
                     alt="${player.name}" loading="lazy"
                     onerror="this.outerHTML=\`${silhouetteHTML().replace(/`/g, '\\`')}\`">`;
  } else {
    headshot = silhouetteHTML();
  }
  return `
    <div class="player-row">
      <div class="player-rank">${rank}</div>
      ${headshot}
      <div class="player-info">
        <div class="player-name">${player.name || 'Unknown'}</div>
        <div class="player-stat-line">
          <span class="pos-badge" style="${badgeStyle}">${player.position || '?'}</span>
          <span class="player-stat">${stat}</span>
        </div>
      </div>
    </div>`;
}

function buildPlayerListHTML(players, side) {
  if (!players || players.length === 0) {
    return '<div class="no-players">Stats unavailable for this week</div>';
  }
  return players.map((p, i) => buildPlayerHTML(p, side, i + 1)).join('');
}

function openDetail(team) {
  state.selectedTeam = team.abbreviation;
  const overlay = qs('#detail-overlay');
  const panel   = qs('#detail-panel');
  if (!overlay || !panel) return;

  const tStats  = state.stats[team.abbreviation] || { offense: [], defense: [] };
  const color   = team.color || '#3b82f6';

  panel.innerHTML = `
    <div class="detail-header" style="border-top:4px solid ${color}">
      <button class="detail-close" id="btn-close-detail" aria-label="Close">✕</button>
      <div class="detail-team-info">
        ${team.logo
          ? `<img class="detail-logo" src="${team.logo}" alt="${team.name}">`
          : `<div class="detail-logo-fallback">${team.abbreviation}</div>`}
        <div>
          <div class="detail-team-name">${team.name}</div>
          <div class="detail-week-label">${state.currentSeason} · Week ${state.currentWeek}</div>
        </div>
      </div>
    </div>
    <div class="detail-body">
      <div class="detail-columns">
        <div class="detail-col">
          <div class="detail-col-header offense-header">OFFENSE</div>
          ${buildPlayerListHTML(tStats.offense, 'offense')}
        </div>
        <div class="detail-col">
          <div class="detail-col-header defense-header">DEFENSE</div>
          ${buildPlayerListHTML(tStats.defense, 'defense')}
        </div>
      </div>
    </div>`;

  overlay.classList.add('active');
  document.body.style.overflow = 'hidden';

  qs('#btn-close-detail').addEventListener('click', closeDetail);
  overlay.addEventListener('click', e => { if (e.target === overlay) closeDetail(); });
}

function closeDetail() {
  const overlay = qs('#detail-overlay');
  if (overlay) overlay.classList.remove('active');
  document.body.style.overflow = '';
  state.selectedTeam = null;
}

// ---------------------------------------------------------------------------
// Reload stats and re-render
// ---------------------------------------------------------------------------
async function reloadStats() {
  showLoading('Loading player stats…');
  try {
    const data = await fetchStats(state.currentSeason, state.currentWeek);
    state.stats    = data.teams    || {};
    state.dataNote = data.data_note || null;
    setDataNote(state.dataNote);
    renderTeamGrid();
    showGrid();
    updateWeekLabel();

    // Refresh open detail panel if one is showing
    if (state.selectedTeam) {
      const team = state.teams.find(t => t.abbreviation === state.selectedTeam);
      if (team) openDetail(team);
    }
  } catch (e) {
    console.error('[nfl-stats] reloadStats:', e);
    showError('Stats unavailable for this selection.');
  }
}

// ---------------------------------------------------------------------------
// Initialisation
// ---------------------------------------------------------------------------
async function init() {
  showLoading('Loading NFL teams…');

  try {
    const [status, teams] = await Promise.all([
      fetchDataStatus(),
      fetchTeams(),
    ]);

    DATA_STATUS = status;
    // Default to the newest data that actually EXISTS, not the empty live week.
    state.currentSeason = status.default_season || 2024;
    state.currentWeek   = status.default_week   || 18;
    state.teams         = Array.isArray(teams) ? teams : [];

    updateWeekLabel();
    buildSeasonSelect(state.currentSeason);
    buildWeekSelect(state.currentWeek);
    renderModeBadge(state.currentSeason);

    // Render grid immediately so the user sees team cards
    renderTeamGrid();

    // Now fetch stats
    await reloadStats();

  } catch (e) {
    console.error('[nfl-stats] init:', e);
    showError('Unable to load NFL data. Please refresh to try again.');
  }
}

// ---------------------------------------------------------------------------
// Event listeners
// ---------------------------------------------------------------------------
document.addEventListener('DOMContentLoaded', () => {
  init();

  qs('#season-select')?.addEventListener('change', function () {
    state.currentSeason = parseInt(this.value, 10);
    renderModeBadge(state.currentSeason);
    reloadStats();
  });

  qs('#week-select')?.addEventListener('change', function () {
    state.currentWeek = parseInt(this.value, 10);
    reloadStats();
  });

  let searchTimer = null;
  qs('#player-search')?.addEventListener('input', () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(renderPlayerSearch, 120);
  });

  document.addEventListener('keydown', e => {
    if (e.key === 'Escape') closeDetail();
  });
});
