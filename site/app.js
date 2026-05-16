/* THE EDGE — Scott Bot frontend. Reads data.json + analytics.json. */
(() => {
  'use strict';

  const $ = (sel) => document.querySelector(sel);
  const $$ = (sel) => Array.from(document.querySelectorAll(sel));

  const state = {
    data: null,
    analytics: null,
    scope: 'daily',
  };

  // ── Fetch ────────────────────────────────────────────────────────────
  async function load() {
    try {
      const [dResp, aResp] = await Promise.all([
        fetch(`data.json?_=${Date.now()}`),
        fetch(`analytics.json?_=${Date.now()}`),
      ]);
      state.data = dResp.ok ? await dResp.json() : emptyData();
      state.analytics = aResp.ok ? await aResp.json() : emptyAnalytics();
    } catch (e) {
      console.error('Load failed:', e);
      state.data = emptyData();
      state.analytics = emptyAnalytics();
    }
    render();
  }

  function emptyData() {
    return {
      generated_at: null,
      today: new Date().toISOString().slice(0, 10),
      system_paused: false,
      pause_reason: '',
      today_picks: [],
      all_picks: [],
      ladder: { current_streak: 0, longest_streak: 0, completed_climbs: 0, target: 10, history: [] },
    };
  }

  function emptyAnalytics() {
    const emptyRollup = {
      record: '0-0-0', wins: 0, losses: 0, pushes: 0, total_picks: 0,
      units_pl: 0, roi_pct: 0, win_rate_pct: 0, avg_clv_cents: 0,
      longest_w_streak: 0, longest_l_streak: 0, biggest_win_units: 0, biggest_loss_units: 0,
    };
    const scope = { overall: { ...emptyRollup }, by_sport: {}, by_confidence: {}, by_pick_type: {} };
    return {
      generated_at: null,
      scopes: { daily: scope, weekly: scope, monthly: scope, yearly: scope, all_time: scope },
      current_streak: { type: 'none', count: 0, label: '—' },
      recent_form: [],
      clv_trend: [],
      ladder: { state: { current_streak: 0, longest_streak: 0, completed_climbs: 0, target: 10 }, all_time_record: emptyRollup },
      totals: { all_picks: 0, sports: [] },
    };
  }

  // ── Render ───────────────────────────────────────────────────────────
  function render() {
    renderPaused();
    renderNavRecord();
    renderLadder();
    renderExecSummary();
    renderTodayPicks();
    renderScopeTabs();
    renderRollups();
    renderRecentForm();
    renderHistory();
    renderUpdated();
  }

  function renderExecSummary() {
    const sect = $('#execSummarySection');
    const body = $('#execSummaryBody');
    const picks = state.data.today_picks || [];
    const summary = picks.find(p => p.executive_summary)?.executive_summary
                 || picks.find(p => p.slate_assessment)?.slate_assessment
                 || '';
    if (!summary.trim()) {
      sect.style.display = 'none';
      return;
    }
    body.textContent = summary.trim();
    sect.style.display = '';
  }

  function renderPaused() {
    const banner = $('#pausedBanner');
    if (state.data.system_paused) {
      $('#pausedReason').textContent = state.data.pause_reason || 'Unknown';
      banner.style.display = 'block';
    } else {
      banner.style.display = 'none';
    }
  }

  function renderNavRecord() {
    const allTime = state.analytics.scopes?.all_time?.overall;
    if (!allTime) return;
    $('#navRecordValue').textContent = allTime.record || '—';
    const units = allTime.units_pl ?? 0;
    const unitsEl = $('#navUnitsValue');
    unitsEl.textContent = (units >= 0 ? '+' : '') + units.toFixed(2) + 'u';
    unitsEl.className = 'nav-value ' + (units > 0 ? 'positive' : units < 0 ? 'negative' : '');
    const roi = allTime.roi_pct ?? 0;
    const roiEl = $('#navRoiValue');
    roiEl.textContent = (roi >= 0 ? '+' : '') + roi.toFixed(1) + '%';
    roiEl.className = 'nav-value ' + (roi > 0 ? 'positive' : roi < 0 ? 'negative' : '');
  }

  // ── Ladder ───────────────────────────────────────────────────────────
  function renderLadder() {
    const l = state.data.ladder || {};
    const target = l.target || 10;
    const cur = l.current_streak || 0;
    const longest = l.longest_streak || 0;
    const climbs = l.completed_climbs || 0;

    $('#ladderProgress').textContent = `${cur} / ${target}`;
    $('#ladderLongest').textContent = longest;
    $('#ladderClimbs').textContent = climbs;

    let sub = 'Climbing for ten in a row.';
    if (cur === 0 && longest === 0) sub = 'New climb starts on next ladder pick.';
    else if (cur === 0 && longest > 0) sub = 'Streak broken. Starting over.';
    else if (cur >= target - 1) sub = 'One away. Hold it together, boys.';
    else if (cur >= target - 2) sub = 'Almost there, boys.';
    else if (cur >= 5) sub = 'Halfway up. Don\'t look down.';
    else if (cur >= 1) sub = `Climbing — ${cur} in a row.`;
    $('#ladderSub').textContent = sub;

    // Horizontal rungs (10 dots)
    let rungs = '';
    for (let i = 0; i < target; i++) {
      const isOn = i < cur;
      const isTarget = i === cur && cur < target;  // next rung to aim for
      const cls = isOn ? 'rung on' : isTarget ? 'rung target' : 'rung';
      rungs += `<div class="${cls}" title="Rung ${i + 1}"></div>`;
    }
    $('#ladderRungs').innerHTML = rungs;
  }

  // ── Today's picks ────────────────────────────────────────────────────
  function renderTodayPicks() {
    const grid = $('#picksGrid');
    const dateLabel = $('#dateLabel');
    const today = state.data.today || new Date().toISOString().slice(0, 10);
    dateLabel.textContent = formatDateLabel(today);

    const picks = state.data.today_picks || [];
    if (picks.length === 0) {
      grid.innerHTML = `<p class="empty-state">No picks today. Either the slate was soft or Scott Bot took a pass. Zero is a valid play.</p>`;
      return;
    }
    // Ladder pick first, then by confidence desc
    const sorted = picks.slice().sort((a, b) => {
      if (a.ladder_designation && !b.ladder_designation) return -1;
      if (!a.ladder_designation && b.ladder_designation) return 1;
      return (b.confidence || 0) - (a.confidence || 0);
    });
    grid.innerHTML = sorted.map(pickCardHTML).join('');
    grid.querySelectorAll('.pick-card').forEach((el) => {
      el.addEventListener('click', () => openModal(el.dataset.pickId));
    });
  }

  const BOOK_LABEL = { draftkings: 'DK', fanduel: 'FD', betmgm: 'MGM' };

  function bookPricesHTML(p) {
    const prices = p.book_prices || {};
    const links = p.book_links || {};
    const keys = ['draftkings', 'fanduel', 'betmgm'];
    const cells = keys.map(k => {
      const odds = prices[k];
      const link = links[k];
      const isBest = (p.best_book && p.best_book.toLowerCase().includes(k))
                  || (odds && odds === p.best_odds);
      const classes = `book-cell${isBest ? ' best' : ''}${link ? ' linked' : ''}`;
      const inner = `
        <div class="book-name">${BOOK_LABEL[k]}</div>
        <div class="book-odds">${odds ? escapeHtml(odds) : '—'}</div>
        ${link ? '<div class="book-tap">TAP →</div>' : ''}
      `;
      // Stop propagation so clicking the link doesn't open the modal
      return link
        ? `<a class="${classes}" href="${escapeAttr(link)}" target="_blank" rel="noopener noreferrer" onclick="event.stopPropagation()">${inner}</a>`
        : `<div class="${classes}">${inner}</div>`;
    }).join('');
    return `<div class="book-prices">${cells}</div>`;
  }

  function pickCardHTML(p) {
    const ladderClass = p.ladder_designation ? ' ladder' : '';
    const lateClass = p.late_add ? ' late-add' : '';
    const time = formatTime(p.first_pitch_iso);
    const isParlay = (p.market || '').toUpperCase() === 'PARLAY';
    const hasWP = (typeof p.win_probability === 'number' && p.win_probability > 0);
    const wpBlock = hasWP
      ? `<span class="win-prob" title="Scott Bot's model-estimated win probability">
           <span class="wp-num">${Math.round(p.win_probability * 100)}%</span>
           <span class="wp-label">WIN PROB</span>
         </span>`
      : (p.confidence ? `<span class="win-prob conf-fallback" title="Confidence ${p.confidence}/5 — units ${formatUnits(p.units)}">
           <span class="wp-num">${p.confidence}/5</span>
           <span class="wp-label">CONF</span>
         </span>` : '');
    return `
      <article class="pick-card${ladderClass}${lateClass}" data-pick-id="${escapeAttr(p.id)}">
        <div class="pick-head">
          <span class="sport-tag">${escapeHtml(p.sport)}${isParlay ? ' · PARLAY' : ''}</span>
          ${wpBlock}
        </div>
        <div class="pick-game">${escapeHtml(p.game)}</div>
        <div class="pick-time">${time}</div>
        <div class="pick-line">${escapeHtml(p.pick)}</div>
        <div class="pick-odds-row">
          <span class="pick-odds">${escapeHtml(p.best_odds || '')}</span>
          <span class="pick-units">${formatUnits(p.units)}</span>
        </div>
        ${bookPricesHTML(p)}
        <div class="pick-headline">${escapeHtml(p.headline || '')}</div>
        <div class="pick-cta">READ THE FULL BREAKDOWN →</div>
      </article>
    `;
  }

  // ── Pick detail modal ────────────────────────────────────────────────
  function openModal(pickId) {
    const pick = (state.data.today_picks || []).find(p => p.id === pickId)
               || (state.data.all_picks || []).find(p => p.id === pickId);
    if (!pick) return;

    const dataHTML = (pick.the_data || []).map(d => `
      <div class="m-data-card">
        <div class="m-data-label">${escapeHtml(d.label)}</div>
        <div class="m-data-value">${escapeHtml(String(d.value))}</div>
        <div class="m-data-context">${escapeHtml(d.context || '')}</div>
      </div>
    `).join('');

    // Win probability card at the top of the_data block, if present
    const wpCard = (typeof pick.win_probability === 'number' && pick.win_probability > 0)
      ? `<div class="m-data-card m-wp-card">
           <div class="m-data-label">SCOTT BOT WIN PROB</div>
           <div class="m-data-value">${Math.round(pick.win_probability * 100)}%</div>
           <div class="m-data-context">Model-estimated true probability of this pick winning</div>
         </div>`
      : '';

    const thesisHTML = (pick.the_thesis || '')
      .split(/\n\n+/)
      .filter(Boolean)
      .map(para => `<p>${escapeHtml(para)}</p>`).join('');

    const ladderBox = pick.ladder_designation && pick.ladder_note
      ? `<div class="m-ladder-note"><strong>🪜 LADDER PICK:</strong> ${escapeHtml(pick.ladder_note)}</div>`
      : '';

    const lateBox = pick.late_add
      ? `<div class="m-late-note"><strong>⚡ LATE ADD:</strong> ${escapeHtml(pick.late_add_reason || 'Material edge identified after morning lock-in.')}</div>`
      : '';

    const legs = pick.legs || [];
    const legsBlock = legs.length
      ? `<div class="m-section"><h4>PARLAY LEGS</h4><div class="m-legs">${
          legs.map(l => `
            <div class="m-leg">
              <div class="m-leg-game">${escapeHtml(l.game)}</div>
              <div class="m-leg-pick">${escapeHtml(l.pick)}</div>
              <div class="m-leg-meta">${escapeHtml(l.best_odds || '')} ${escapeHtml(l.best_book || '')}</div>
            </div>
          `).join('')
        }</div></div>`
      : '';

    const bookGrid = pick.book_prices && Object.keys(pick.book_prices).length
      ? `<div class="m-section"><h4>BOOK PRICES</h4>${bookPricesHTML(pick)}</div>`
      : '';

    const html = `
      <div class="m-kicker">${escapeHtml(pick.sport)} · ${escapeHtml(pick.market || '')}</div>
      <div class="m-game">${escapeHtml(pick.game)}</div>
      <div class="m-time">${formatTime(pick.first_pitch_iso)} · ${formatDateLabel(pick.date || state.data.today)}</div>

      <div class="m-pickline">
        <span class="m-pick">${escapeHtml(pick.pick)}</span>
        <span class="m-odds">${escapeHtml(pick.best_odds || '')}</span>
        <span class="m-book">at ${escapeHtml(pick.best_book || 'consensus')}</span>
        <span class="m-units">${formatUnits(pick.units)}</span>
      </div>

      <p class="m-headline">${escapeHtml(pick.headline || '')}</p>

      ${ladderBox}
      ${lateBox}
      ${legsBlock}
      ${bookGrid}

      <div class="m-section">
        <h4>THE THESIS</h4>
        ${thesisHTML}
      </div>

      ${(wpCard || dataHTML) ? `<div class="m-section"><h4>THE DATA</h4><div class="m-data-grid">${wpCard}${dataHTML}</div></div>` : ''}

      ${pick.the_market ? `<div class="m-section"><h4>THE MARKET</h4><p>${escapeHtml(pick.the_market)}</p></div>` : ''}

      ${pick.weather_park ? `<div class="m-section"><h4>PARK & WEATHER</h4><p>${escapeHtml(pick.weather_park)}</p></div>` : ''}

      ${pick.case_against ? `<div class="m-section"><h4>THE CASE AGAINST</h4><p>${escapeHtml(pick.case_against)}</p></div>` : ''}

      ${pick.what_were_betting_on ? `<div class="m-section"><h4>WHAT WE'RE BETTING ON</h4><p>${escapeHtml(pick.what_were_betting_on)}</p></div>` : ''}

      ${pick.scott_bot_quip ? `<div class="m-quip">${escapeHtml(pick.scott_bot_quip)}</div>` : ''}
    `;
    $('#modalBody').innerHTML = html;
    $('#pickModal').style.display = 'flex';
    document.body.style.overflow = 'hidden';
  }

  function closeModal() {
    $('#pickModal').style.display = 'none';
    document.body.style.overflow = '';
  }
  document.addEventListener('click', (e) => {
    if (e.target.matches('[data-close]')) closeModal();
  });
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeModal(); });

  // ── Scope tabs + rollups ─────────────────────────────────────────────
  function renderScopeTabs() {
    $$('.scope-tab').forEach(btn => {
      btn.classList.toggle('active', btn.dataset.scope === state.scope);
      btn.onclick = () => { state.scope = btn.dataset.scope; renderScopeTabs(); renderRollups(); };
    });
  }

  function renderRollups() {
    const grid = $('#rollupGrid');
    const scope = state.analytics?.scopes?.[state.scope]?.overall;
    if (!scope) { grid.innerHTML = `<p class="empty-state">No data for ${state.scope}.</p>`; return; }
    const units = scope.units_pl ?? 0;
    const roi = scope.roi_pct ?? 0;
    const cards = [
      { label: 'RECORD',       value: scope.record || '0-0-0',       sub: `${scope.total_picks || 0} picks` },
      { label: 'UNITS',        value: (units >= 0 ? '+' : '') + units.toFixed(2),  sub: 'P/L',     cls: units > 0 ? 'positive' : units < 0 ? 'negative' : '' },
      { label: 'ROI',          value: (roi >= 0 ? '+' : '') + roi.toFixed(1) + '%',  sub: 'return',  cls: roi > 0 ? 'positive' : roi < 0 ? 'negative' : '' },
      { label: 'WIN RATE',     value: (scope.win_rate_pct || 0).toFixed(1) + '%',   sub: 'of decided' },
      { label: 'AVG CLV',      value: (scope.avg_clv_cents >= 0 ? '+' : '') + (scope.avg_clv_cents || 0).toFixed(2) + '¢', sub: 'vs close', cls: scope.avg_clv_cents > 0 ? 'positive' : scope.avg_clv_cents < 0 ? 'negative' : '' },
      { label: 'LONGEST W',    value: scope.longest_w_streak || 0,   sub: 'in scope' },
    ];
    grid.innerHTML = cards.map(c => `
      <div class="rollup-card">
        <div class="rc-label">${c.label}</div>
        <div class="rc-value ${c.cls || ''}">${c.value}</div>
        <div class="rc-sub">${c.sub}</div>
      </div>
    `).join('');
  }

  // ── Recent form ──────────────────────────────────────────────────────
  function renderRecentForm() {
    const wrap = $('#recentForm');
    const list = state.analytics?.recent_form || [];
    if (!list.length) { wrap.innerHTML = `<p class="empty-state">No graded picks yet.</p>`; return; }
    wrap.innerHTML = list.map(item => {
      const u = item.units || 0;
      const ucls = u > 0 ? 'positive' : u < 0 ? 'negative' : '';
      return `
        <span class="tick">
          ${item.ladder ? '<span class="tladder">🪜</span>' : ''}
          <span class="result ${item.result}">${item.result}</span>
          <span>${escapeHtml(item.pick)}</span>
          <span class="tunits ${ucls}">${u >= 0 ? '+' : ''}${(u).toFixed(2)}u</span>
        </span>
      `;
    }).join('');
  }

  // ── Full history table ───────────────────────────────────────────────
  function renderHistory() {
    const tbody = $('#historyTable tbody');
    const picks = state.data.all_picks || [];
    if (!picks.length) {
      tbody.innerHTML = `<tr><td colspan="8" class="empty-state">No picks yet.</td></tr>`;
      return;
    }
    tbody.innerHTML = picks.map(p => {
      const status = p.status || 'PEND';
      const u = p.units_result;
      const ucls = u == null ? '' : u > 0 ? 'positive' : u < 0 ? 'negative' : '';
      const uStr = u == null ? '—' : (u >= 0 ? '+' : '') + u.toFixed(2) + 'u';
      const ladder = p.ladder_designation ? '<span class="ladder-badge">LADDER</span>' : '';
      return `
        <tr>
          <td>${escapeHtml(p.date || '')}</td>
          <td>${escapeHtml(p.sport || '')}</td>
          <td>${escapeHtml(p.game || '')}</td>
          <td>${escapeHtml(p.pick || '')} ${ladder}</td>
          <td>${escapeHtml(p.best_odds || '')}</td>
          <td>${formatUnits(p.units)}</td>
          <td class="${status.toLowerCase()}">${status}</td>
          <td class="units-pl ${ucls}">${uStr}</td>
        </tr>
      `;
    }).join('');
  }

  function renderUpdated() {
    const gen = state.data.generated_at;
    $('#updatedAt').textContent = gen ? new Date(gen).toLocaleString('en-US', { dateStyle: 'medium', timeStyle: 'short' }) : '—';
  }

  // ── helpers ──────────────────────────────────────────────────────────
  function formatDateLabel(iso) {
    if (!iso) return '';
    const d = new Date(iso + 'T00:00:00');
    if (isNaN(d.getTime())) return iso;
    return d.toLocaleDateString('en-US', { weekday: 'long', month: 'long', day: 'numeric', year: 'numeric' });
  }
  function formatTime(iso) {
    if (!iso) return '';
    const d = new Date(iso);
    if (isNaN(d.getTime())) return '';
    return d.toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit', timeZoneName: 'short' });
  }
  function formatUnits(u) {
    if (u == null) return '';
    return `${(+u).toFixed(1)}u`;
  }
  function escapeHtml(s) {
    return String(s ?? '')
      .replaceAll('&', '&amp;')
      .replaceAll('<', '&lt;')
      .replaceAll('>', '&gt;')
      .replaceAll('"', '&quot;')
      .replaceAll("'", '&#39;');
  }
  function escapeAttr(s) { return escapeHtml(s).replaceAll('\n', ''); }

  load();
})();
