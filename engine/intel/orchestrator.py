"""Pulls all intel modules together and pre-computes signals."""
from __future__ import annotations

from typing import Optional

from engine.utils import get_logger, nyc_date
from engine.intel.types import IntelPack, SportCode
from engine.intel.schedule import fetch_scoreboard, schedule_to_packs
from engine.intel.market import attach_market_intel
from engine.intel.mlb import attach_mlb_intel
from engine.intel.nba import attach_nba_intel
from engine.intel.nhl import attach_nhl_intel
from engine.intel.football import attach_football_intel

logger = get_logger("intel-orch")


def harvest_intel(sports: list[SportCode], date_str: Optional[str] = None) -> list[IntelPack]:
    date_str = date_str or nyc_date()
    logger.info(f"=== INTEL HARVEST START: {date_str} sports={sports} ===")
    packs: list[IntelPack] = []

    # 1. Schedule
    for sport in sports:
        try:
            data = fetch_scoreboard(sport, date_str)
            sport_packs = schedule_to_packs(sport, data)
            logger.info(f"{sport}: {len(sport_packs)} games scheduled")
            packs.extend(sport_packs)
        except Exception as e:
            logger.warning(f"{sport} schedule fetch failed: {e}")

    if not packs:
        logger.info("No games today across enabled sports.")
        return []

    # 2. Sport-specific deep intel
    try:
        attach_mlb_intel(packs, date_str)
    except Exception as e:
        logger.warning(f"MLB intel attach failed: {e}")

    try:
        attach_nba_intel(packs, date_str)
    except Exception as e:
        logger.warning(f"NBA intel attach failed: {e}")

    try:
        attach_nhl_intel(packs, date_str)
    except Exception as e:
        logger.warning(f"NHL intel attach failed: {e}")

    try:
        attach_football_intel(packs, date_str)
    except Exception as e:
        logger.warning(f"Football intel attach failed: {e}")

    # 3. Market intel (per sport)
    for sport in sports:
        try:
            attach_market_intel(packs, sport)
        except Exception as e:
            logger.warning(f"Market intel attach failed for {sport}: {e}")

    # 4. Compute signals per pack
    for pack in packs:
        compute_signals(pack)

    logger.info(f"=== INTEL HARVEST DONE: {len(packs)} games enriched ===")
    return packs


def compute_signals(pack: IntelPack) -> None:
    """Append short tags the handicapper can latch onto."""
    sig: list[str] = []
    m = pack.market

    # Reverse line movement / consensus drift
    if m:
        if m.total_open is not None and m.consensus_total is not None:
            delta = m.consensus_total - m.total_open
            if abs(delta) >= 0.5:
                direction = "rose" if delta > 0 else "dropped"
                sig.append(f"total {direction} from {m.total_open} → {m.consensus_total} since open")
        if m.home_ml_open_pct is not None and m.home_ml_implied_pct is not None:
            ml_delta = m.home_ml_implied_pct - m.home_ml_open_pct
            if abs(ml_delta) >= 0.03:
                direction = "shortened" if ml_delta > 0 else "lengthened"
                sig.append(f"home ML {direction} {abs(ml_delta)*100:.1f}pp since open")
        if m.book_count >= 5 and (m.home_ml_best or m.over_best):
            sig.append(f"shopped across {m.book_count} books")

    # MLB pitcher form
    for side, p, throws_team in (("home", pack.home_pitcher, pack.home_team), ("away", pack.away_pitcher, pack.away_team)):
        if not p:
            continue
        if p.season_xfip is not None and p.season_xfip <= 3.30:
            sig.append(f"{throws_team} SP {p.name} elite xFIP {p.season_xfip}")
        elif p.season_xfip is not None and p.season_xfip >= 4.50:
            sig.append(f"{throws_team} SP {p.name} weak xFIP {p.season_xfip}")
        if p.trend == "improving":
            sig.append(f"{throws_team} SP trending up (L3 ERA below season)")
        elif p.trend == "regressing":
            sig.append(f"{throws_team} SP trending down (L3 ERA above season)")
        if p.season_stuff_plus is not None and p.season_stuff_plus >= 110:
            sig.append(f"{throws_team} SP Stuff+ {p.season_stuff_plus:.0f} (top-tier arsenal)")

    # MLB offense gap
    if pack.home_offense and pack.away_offense:
        h = pack.home_offense.wrc_plus_season
        a = pack.away_offense.wrc_plus_season
        if h is not None and a is not None and abs(h - a) >= 15:
            stronger = pack.home_team if h > a else pack.away_team
            sig.append(f"{stronger} offense clearly stronger (wRC+ gap {abs(h-a):.0f})")

    # Park + weather
    if pack.park and pack.park.pf_runs:
        if pack.park.pf_runs >= 1.07:
            sig.append(f"hitter-friendly park ({pack.park.name}, runs PF {pack.park.pf_runs})")
        elif pack.park.pf_runs <= 0.94:
            sig.append(f"pitcher-friendly park ({pack.park.name}, runs PF {pack.park.pf_runs})")
    if pack.weather and pack.weather.hr_impact_pct:
        impact = pack.weather.hr_impact_pct
        if impact >= 0.08:
            sig.append(f"weather boosting HR ~{int(impact*100)}%")
        elif impact <= -0.07:
            sig.append(f"weather suppressing HR ~{int(impact*100)}%")

    # NBA edge signals
    if pack.home_nba and pack.away_nba:
        h_net = pack.home_nba.net_rating
        a_net = pack.away_nba.net_rating
        if h_net is not None and a_net is not None and abs(h_net - a_net) >= 7:
            stronger = pack.home_team if h_net > a_net else pack.away_team
            sig.append(f"{stronger} NET rating edge {abs(h_net-a_net):.1f}")
        for side, ratings, team in (("home", pack.home_nba, pack.home_team), ("away", pack.away_nba, pack.away_team)):
            if ratings.back_to_back:
                sig.append(f"{team} on back-to-back")

    pack.signals = sig
