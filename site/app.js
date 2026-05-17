/* THE EDGE — Scott Bot frontend. Reads data.json + analytics.json. */
(() => {
  'use strict';

  const $ = (sel) => document.querySelector(sel);
  const $$ = (sel) => Array.from(document.querySelectorAll(sel));

  // ── Live scores (browser-direct from ESPN) ───────────────────────────
  const SPORT_TO_ESPN = {
    MLB: 'baseball/mlb',
    NBA: 'basketball/nba',
    NHL: 'hockey/nhl',
    NFL: 'football/nfl',
    CFB: 'football/college-football',
  };
  const liveScores = { map: {}, lastFetch: 0, inFlight: false };

  function _todayETKey() {
    // ESPN scoreboard wants YYYYMMDD in ET
    const d = new Date().toLocaleDateString('en-CA', { timeZone: 'America/New_York' });
    return d.replace(/-/g, '');
  }

  async function fetchLiveScores() {
    if (liveScores.inFlight) return;
    liveScores.inFlight = true;
    try {
      const sports = new Set();
      ['today_picks', 'today_bonus_picks'].forEach(k => {
        (state.data[k] || []).forEach(p => {
          if (p.status === 'PEND' && SPORT_TO_ESPN[p.sport]) sports.add(p.sport);
        });
      });
      if (!sports.size) return;
      const dateKey = _todayETKey();
      const work = [...sports].map(async (sport) => {
        try {
          const r = await fetch(`https://site.api.espn.com/apis/site/v2/sports/${SPORT_TO_ESPN[sport]}/scoreboard?dates=${dateKey}`, { cache: 'no-store' });
          if (!r.ok) return;
          const data = await r.json();
          for (const ev of (data.events || [])) {
            const comp = (ev.competitions && ev.competitions[0]) || {};
            const competitors = comp.competitors || [];
            const home = competitors.find(c => c.homeAway === 'home');
            const away = competitors.find(c => c.homeAway === 'away');
            if (!home || !away) continue;
            const status = (ev.status && ev.status.type) || {};
            const homeAbbr = home.team && home.team.abbreviation;
            const awayAbbr = away.team && away.team.abbreviation;
            if (!homeAbbr || !awayAbbr) continue;
            liveScores.map[`${sport}|${awayAbbr}|${homeAbbr}`] = {
              sport,
              home_abbr: homeAbbr,
              away_abbr: awayAbbr,
              home_score: parseInt(home.score, 10) || 0,
              away_score: parseInt(away.score, 10) || 0,
              state: status.state || '',         // 'pre' | 'in' | 'post'
              short_detail: status.shortDetail || '',
              completed: !!status.completed,
            };
          }
        } catch (e) { /* swallow per-sport */ }
      });
      await Promise.all(work);
      liveScores.lastFetch = Date.now();
      applyScoresToCards();
    } finally {
      liveScores.inFlight = false;
    }
  }

  function _gameKey(pick) {
    const m = (pick.game || '').match(/([A-Z]{2,5})\s*@\s*([A-Z]{2,5})/);
    if (!m) return null;
    return `${pick.sport}|${m[1]}|${m[2]}`;
  }

  function applyScoresToCards() {
    document.querySelectorAll('.pick-card[data-pick-id]').forEach(card => {
      const pickId = card.dataset.pickId;
      const pick = (state.data.today_picks || []).find(p => p.id === pickId)
                || (state.data.today_bonus_picks || []).find(p => p.id === pickId)
                || (state.data.all_picks || []).find(p => p.id === pickId);
      if (!pick || pick.status !== 'PEND') return;
      const key = _gameKey(pick);
      const score = key && liveScores.map[key];
      let chip = card.querySelector('.live-score');
      if (!score) { if (chip) chip.remove(); return; }
      let cls = 'live-score';
      let text;
      if (score.state === 'post' || score.completed) {
        cls += ' final';
        text = `FINAL · ${score.away_abbr} ${score.away_score} · ${score.home_abbr} ${score.home_score}`;
      } else if (score.state === 'in') {
        cls += ' live';
        text = `<span class="dot-live"></span>LIVE · ${score.away_abbr} ${score.away_score} · ${score.home_abbr} ${score.home_score} · ${escapeHtml(score.short_detail)}`;
      } else {
        cls += ' pre';
        text = `${escapeHtml(score.short_detail || 'Scheduled')}`;
      }
      if (!chip) {
        chip = document.createElement('div');
        const timeEl = card.querySelector('.pick-time');
        if (timeEl) timeEl.after(chip); else card.querySelector('.pick-game').after(chip);
      }
      chip.className = cls;
      chip.innerHTML = text;
    });
  }

  function startLiveScoresLoop() {
    fetchLiveScores();   // initial
    setInterval(() => {
      if (document.visibilityState === 'visible') fetchLiveScores();
    }, 60_000);          // every 60s while tab visible
    document.addEventListener('visibilitychange', () => {
      if (document.visibilityState === 'visible') fetchLiveScores();
    });
  }

  // ── Analytics helper ─────────────────────────────────────────────────
  // Fires Plausible custom events safely. No-ops if Plausible isn't loaded
  // (dev, ad-blocker, etc).
  function track(eventName, props = {}) {
    try {
      const clean = {};
      for (const [k, v] of Object.entries(props)) {
        if (v === undefined || v === null) continue;
        clean[k] = (typeof v === 'boolean') ? (v ? 'yes' : 'no') : String(v);
      }
      if (window.plausible) window.plausible(eventName, { props: clean });
    } catch (_) { /* swallow */ }
  }
  // One-shot events (e.g. "Methodology Viewed" once per session)
  const _seen = new Set();
  function trackOnce(eventName, props) {
    if (_seen.has(eventName)) return;
    _seen.add(eventName);
    track(eventName, props);
  }

  // Single human-readable label for a pick, e.g.
  //   "Under 7.5 · BOS @ ATL 🪜"  (ladder)
  //   "Mets ML · NYY @ NYM ⚡"     (late add)
  function pickLabel(p) {
    const badges = [];
    if (p.ladder_designation) badges.push('🪜');
    if (p.late_add) badges.push('⚡');
    const tag = badges.length ? ` ${badges.join('')}` : '';
    return `${p.pick} · ${p.game}${tag}`;
  }

  const state = {
    data: null,
    analytics: null,
    scope: 'weekly',
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
    renderLastNight();
    renderExecSummary();
    renderTodayPicks();
    renderJuicePicks();
    renderScopeTabs();
    renderRollups();
    renderRecentForm();
    renderHistory();
    renderUpdated();
  }

  function renderLastNight() {
    const sect = $('#lastNightSection');
    const recap = state.data.last_night_recap;
    const todayPicks = state.data.today_picks || [];

    // Only show when:
    //   1. There IS a recap to show
    //   2. Today's main picks haven't been published yet (we're between
    //      11:30 PM grader and 11 AM next-morning lock).
    // Once today's picks lock, the recap is buried in the clickable history.
    if (!recap || !recap.night_summary || todayPicks.length > 0) {
      sect.style.display = 'none';
      return;
    }
    $('#lastNightRecord').textContent = recap.record || '—';
    const u = recap.units_pl ?? 0;
    const uEl = $('#lastNightUnits');
    uEl.textContent = (u >= 0 ? '+' : '') + (+u).toFixed(2) + 'u';
    uEl.className = 'ln-units ' + (u > 0 ? 'positive' : u < 0 ? 'negative' : '');
    $('#lastNightDate').textContent = formatDateLabel(recap.date);
    $('#lastNightSummary').textContent = recap.night_summary;
    sect.style.display = '';
  }

  function renderJuicePicks() {
    const sect = $('#juiceSection');
    const grid = $('#juiceGrid');
    const list = state.data.today_bonus_picks || [];
    if (!list.length) {
      sect.style.display = 'none';
      return;
    }
    grid.innerHTML = list.map(pickCardHTML).join('');
    grid.querySelectorAll('.pick-card').forEach((el) => {
      el.addEventListener('click', () => openModal(el.dataset.pickId));
    });
    grid.querySelectorAll('.book-cell.linked').forEach((el) => {
      el.addEventListener('click', () => {
        const card = el.closest('.pick-card');
        const pickId = card?.dataset.pickId;
        const pick = (state.data.today_bonus_picks || []).find(p => p.id === pickId);
        if (!pick) return;
        const href = el.getAttribute('href') || '';
        const book = href.includes('draftkings') ? 'draftkings'
                   : href.includes('fanduel')   ? 'fanduel'
                   : href.includes('betmgm')    ? 'betmgm'
                   : 'unknown';
        track('Bet Slip Opened', {
          pick_label: pickLabel(pick),
          pick_id: pick.id,
          book,
          pick: pick.pick,
          game: pick.game,
          sport: pick.sport,
          bonus: true,
          odds: (pick.book_prices && pick.book_prices[book]) || pick.best_odds,
        });
      });
    });
    sect.style.display = '';
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
      // Use data freshness (not wall-clock) to decide which copy to show.
      // If site/data.json was last regenerated BEFORE today's 11 AM ET lock,
      // then today's morning workflow hasn't published yet → "lock pending"
      // If it was generated AFTER 11 AM ET and there are no picks → real pass
      const generated = state.data.generated_at ? new Date(state.data.generated_at) : null;
      // Compute today's 11 AM ET as a UTC instant
      const nowET = new Date(new Date().toLocaleString('en-US', { timeZone: 'America/New_York' }));
      const todayLock = new Date(nowET.getFullYear(), nowET.getMonth(), nowET.getDate(), 11, 0, 0);
      // GHA cron drift can push the morning run as late as 12:30 PM ET; treat that
      // window as still "running" rather than a deliberate pass.
      const lockCutoff = new Date(nowET.getFullYear(), nowET.getMonth(), nowET.getDate(), 12, 30, 0);
      let beforeLock = nowET < todayLock;
      let running = !beforeLock && nowET < lockCutoff && (!generated || generated < todayLock);
      let realPass = !beforeLock && !running;

      if (beforeLock) {
        grid.innerHTML = `<p class="empty-state pre-lock">Picks lock at <strong>11 AM ET</strong>. Scott Bot is reading the slate. Check back then.</p>`;
      } else if (running) {
        grid.innerHTML = `<p class="empty-state pre-lock">Scott Bot is reading the slate. Today's picks should land in the next few minutes — refresh shortly.</p>`;
      } else {
        grid.innerHTML = `<p class="empty-state">Scott Bot took a pass today. Zero is a valid play — better to publish nothing than garbage.</p>`;
      }
      return;
    }
    // Order: bonus picks first (rare special events), then ladder pick, then by confidence desc
    const sorted = picks.slice().sort((a, b) => {
      const aBonus = a.bonus_pick ? 1 : 0;
      const bBonus = b.bonus_pick ? 1 : 0;
      if (aBonus !== bBonus) return bBonus - aBonus;
      if (a.ladder_designation && !b.ladder_designation) return -1;
      if (!a.ladder_designation && b.ladder_designation) return 1;
      return (b.confidence || 0) - (a.confidence || 0);
    });
    grid.innerHTML = sorted.map(pickCardHTML).join('');
    grid.querySelectorAll('.pick-card').forEach((el) => {
      el.addEventListener('click', () => openModal(el.dataset.pickId));
    });
    // Wire book-cell clicks to fire detailed "Bet Slip Opened" events
    grid.querySelectorAll('.book-cell.linked').forEach((el) => {
      el.addEventListener('click', (ev) => {
        // Don't preventDefault — let the link follow
        const card = el.closest('.pick-card');
        const pickId = card?.dataset.pickId;
        const pick = (state.data.today_picks || []).find(p => p.id === pickId);
        if (!pick) return;
        const href = el.getAttribute('href') || '';
        const book = href.includes('draftkings') ? 'draftkings'
                   : href.includes('fanduel')   ? 'fanduel'
                   : href.includes('betmgm')    ? 'betmgm'
                   : 'unknown';
        track('Bet Slip Opened', {
          pick_label: pickLabel(pick),
          pick_id: pick.id,
          book,
          pick: pick.pick,
          game: pick.game,
          sport: pick.sport,
          ladder: pick.ladder_designation,
          late_add: pick.late_add,
          odds: (pick.book_prices && pick.book_prices[book]) || pick.best_odds,
          is_best_price: pick.best_book && pick.best_book.toLowerCase().includes(book),
        });
      });
    });
  }

  const BOOK_LABEL = { draftkings: 'DK', fanduel: 'FD', betmgm: 'MGM' };

  function bookPricesHTML(p) {
    const prices = p.book_prices || {};
    const links = p.book_links || {};
    const keys = ['draftkings', 'fanduel', 'betmgm'];
    // Bonus picks (outrights) can't pre-populate a bet slip — link opens the
    // book's golf section instead. Distinct CTA text so the boys know.
    const tapText = p.bonus_pick ? 'OPEN ↗' : 'TAP →';
    const cells = keys.map(k => {
      const odds = prices[k];
      const link = links[k];
      const isBest = (p.best_book && p.best_book.toLowerCase().includes(k))
                  || (odds && odds === p.best_odds);
      const classes = `book-cell${isBest ? ' best' : ''}${link ? ' linked' : ''}`;
      const inner = `
        <div class="book-name">${BOOK_LABEL[k]}</div>
        <div class="book-odds">${odds ? escapeHtml(odds) : '—'}</div>
        ${link ? `<div class="book-tap">${tapText}</div>` : ''}
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
    const bonusClass = p.bonus_pick ? ' bonus' : '';
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
    const sportLabel = p.bonus_pick && p.event_name
      ? `${p.sport || 'GOLF'} · ${p.event_name.toUpperCase()}`
      : `${p.sport}${isParlay ? ' · PARLAY' : ''}`;
    return `
      <article class="pick-card${ladderClass}${lateClass}${bonusClass}" data-pick-id="${escapeAttr(p.id)}">
        <div class="pick-head">
          <span class="sport-tag">${escapeHtml(sportLabel)}</span>
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
  let _modalOpenTs = 0;
  let _modalOpenPick = null;
  function openModal(pickId) {
    const pick = (state.data.today_picks || []).find(p => p.id === pickId)
               || (state.data.today_bonus_picks || []).find(p => p.id === pickId)
               || (state.data.all_picks || []).find(p => p.id === pickId);
    if (!pick) return;
    _modalOpenTs = Date.now();
    _modalOpenPick = pick;
    track('Pick Opened', {
      pick_label: pickLabel(pick),
      pick_id: pick.id,
      pick: pick.pick,
      game: pick.game,
      sport: pick.sport,
      ladder: pick.ladder_designation,
      late_add: pick.late_add,
      confidence: pick.confidence,
      odds: pick.best_odds,
      win_pct: (typeof pick.win_probability === 'number') ? Math.round(pick.win_probability * 100) : null,
    });

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

    const bonusBox = pick.bonus_pick
      ? `<div class="m-bonus-note"><strong>🧪 LAB PICK · FOR THE JUICE:</strong> Longshot bonus pick from the lab. <em>Not tracked in our official W-L record.</em> Take it for the adrenaline — leave it if you don't want the variance.</div>`
      : '';

    // Autopsy block — shown on LOSS picks once the grader has classified them
    const ap = pick.autopsy;
    const autopsyBox = (pick.status === 'LOSS' && ap && ap.post_mortem)
      ? `<div class="m-autopsy"><div class="m-autopsy-head"><strong>AUTOPSY · ${escapeHtml(ap.classification || 'VARIANCE')}</strong>${ap.sample_size_warning ? ` <span class="m-autopsy-sample">(${escapeHtml(ap.sample_size_warning)})</span>` : ''}</div><p>${escapeHtml(ap.post_mortem)}</p>${ap.candidate_rule ? `<p class="m-autopsy-rule"><strong>Proposed rule for review:</strong> ${escapeHtml(ap.candidate_rule)}</p>` : ''}</div>`
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

      ${bonusBox}
      ${ladderBox}
      ${lateBox}
      ${autopsyBox}
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
    if (_modalOpenPick && _modalOpenTs) {
      const secs = Math.round((Date.now() - _modalOpenTs) / 1000);
      track('Pick Closed', {
        pick_label: pickLabel(_modalOpenPick),
        pick_id: _modalOpenPick.id,
        pick: _modalOpenPick.pick,
        game: _modalOpenPick.game,
        sport: _modalOpenPick.sport,
        ladder: _modalOpenPick.ladder_designation,
        read_seconds: secs,
        // Bucket reading time so the dashboard is readable
        read_band: secs < 5 ? 'glance' : secs < 20 ? 'skim' : secs < 60 ? 'read' : 'deep_read',
      });
    }
    _modalOpenTs = 0;
    _modalOpenPick = null;
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
      btn.onclick = () => {
        state.scope = btn.dataset.scope;
        track('Scope Changed', { scope: state.scope });
        renderScopeTabs();
        renderRollups();
      };
    });
  }

  function renderRollups() {
    const grid = $('#rollupGrid');
    const scope = state.analytics?.scopes?.[state.scope]?.overall;
    if (!scope) { grid.innerHTML = `<p class="empty-state">No data for ${state.scope}.</p>`; return; }
    const units = scope.units_pl ?? 0;
    const roi = scope.roi_pct ?? 0;
    const settled = scope.settled_picks ?? ((scope.wins || 0) + (scope.losses || 0) + (scope.pushes || 0));
    const pending = scope.pending ?? Math.max(0, (scope.total_picks || 0) - settled);
    const recordSub = settled === 0
      ? (pending > 0 ? `${pending} pending` : 'no picks yet')
      : pending > 0 ? `${settled} settled · ${pending} pending` : `${settled} settled`;
    const cards = [
      { label: 'RECORD',       value: scope.record || '0-0-0',       sub: recordSub },
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
      const badges = [];
      if (p.ladder_designation) badges.push('<span class="ladder-badge">🪜 LADDER</span>');
      if (p.bonus_pick)         badges.push('<span class="lab-badge">🧪 LAB</span>');
      if (p.late_add)           badges.push('<span class="late-badge">⚡ LATE</span>');
      const badgeHTML = badges.join(' ');
      return `
        <tr data-pick-id="${escapeAttr(p.id || '')}" class="history-row clickable">
          <td>${escapeHtml(p.date || '')}</td>
          <td>${escapeHtml(p.sport || '')}</td>
          <td>${escapeHtml(p.game || '')}</td>
          <td>${escapeHtml(p.pick || '')} ${badgeHTML}</td>
          <td>${escapeHtml(p.best_odds || '')}</td>
          <td>${formatUnits(p.units)}</td>
          <td class="${status.toLowerCase()}">${status}</td>
          <td class="units-pl ${ucls}">${uStr}</td>
        </tr>
      `;
    }).join('');
    // Wire click on each row → open modal with the pick's full analysis
    tbody.querySelectorAll('.history-row').forEach((tr) => {
      tr.addEventListener('click', () => {
        const id = tr.dataset.pickId;
        if (id) openModal(id);
      });
    });
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

  // ── Viewport-based section view tracking ─────────────────────────────
  // Fires once when each section enters the viewport. Tells us how many
  // visitors actually scrolled far enough to see the methodology / history.
  function observeSection(selector, eventName) {
    const el = document.querySelector(selector);
    if (!el || !('IntersectionObserver' in window)) return;
    const obs = new IntersectionObserver((entries) => {
      for (const entry of entries) {
        if (entry.isIntersecting) {
          trackOnce(eventName, {});
          obs.disconnect();
        }
      }
    }, { threshold: 0.25 });
    obs.observe(el);
  }

  // Tracks scroll depth in bands (25/50/75/100%) — proxy for engagement.
  function setupScrollDepth() {
    const bands = [25, 50, 75, 100];
    let lastBand = 0;
    window.addEventListener('scroll', () => {
      const h = document.documentElement;
      const scrolled = h.scrollTop + window.innerHeight;
      const pct = Math.min(100, Math.round((scrolled / h.scrollHeight) * 100));
      for (const b of bands) {
        if (pct >= b && lastBand < b) {
          lastBand = b;
          trackOnce(`Scroll ${b}%`, {});
        }
      }
    }, { passive: true });
  }

  // Track time-on-page when the user leaves
  function setupTimeOnPage() {
    const startTs = Date.now();
    const fire = () => {
      const secs = Math.round((Date.now() - startTs) / 1000);
      const band = secs < 10 ? '0-10s'
                 : secs < 30 ? '10-30s'
                 : secs < 60 ? '30-60s'
                 : secs < 180 ? '1-3m'
                 : secs < 600 ? '3-10m'
                 : '10m+';
      track('Session End', { seconds: secs, band });
    };
    // Use pagehide (best for mobile) with visibilitychange as fallback
    window.addEventListener('pagehide', fire, { once: true });
    document.addEventListener('visibilitychange', () => {
      if (document.visibilityState === 'hidden') fire();
    });
  }

  // Wire it all up after first render
  document.addEventListener('DOMContentLoaded', () => {
    observeSection('.methodology', 'Methodology Viewed');
    observeSection('.history-wrap', 'History Viewed');
    setupScrollDepth();
    setupTimeOnPage();
  });

  // Boot — load data + render, then start the live-scores poll
  load().then(() => startLiveScoresLoop());
})();
