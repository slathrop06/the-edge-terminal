"""NBA intel: team ratings, recent form, rest, key injuries (via nba_api + ESPN)."""
from __future__ import annotations

from typing import Optional

from engine.utils import get_logger
from engine.intel.types import IntelPack, TeamRatingsNBA

logger = get_logger("intel-nba")

_NBA_TEAM_STATS_CACHE: Optional[list[dict]] = None
_NBA_HUSTLE_CACHE: Optional[list[dict]] = None


def _fetch_team_advanced(season_str: str) -> list[dict]:
    """Fetch league-wide advanced team stats. Cached after first call."""
    global _NBA_TEAM_STATS_CACHE
    if _NBA_TEAM_STATS_CACHE is not None:
        return _NBA_TEAM_STATS_CACHE
    try:
        from nba_api.stats.endpoints import leaguedashteamstats
        endpoint = leaguedashteamstats.LeagueDashTeamStats(
            per_mode_simple="PerGame",
            measure_type_simple="Advanced",
            season=season_str,
            timeout=30,
        )
        df = endpoint.get_data_frames()[0]
        _NBA_TEAM_STATS_CACHE = df.to_dict("records")
        return _NBA_TEAM_STATS_CACHE
    except Exception as e:
        logger.warning(f"nba_api advanced stats failed: {e}")
        _NBA_TEAM_STATS_CACHE = []
        return _NBA_TEAM_STATS_CACHE


def _match_team(rows: list[dict], team_name: str) -> Optional[dict]:
    t_lower = team_name.lower()
    for r in rows:
        n = str(r.get("TEAM_NAME", "")).lower()
        if not n:
            continue
        if n == t_lower or n in t_lower or t_lower in n:
            return r
        tokens = set(t_lower.split())
        ntokens = set(n.split())
        if tokens & ntokens and len(tokens & ntokens) >= min(2, len(tokens)):
            return r
    return None


def _build_ratings(team_row: Optional[dict]) -> TeamRatingsNBA:
    r = TeamRatingsNBA()
    if not team_row:
        return r
    r.net_rating = _f(team_row.get("NET_RATING"))
    r.off_rating = _f(team_row.get("OFF_RATING"))
    r.def_rating = _f(team_row.get("DEF_RATING"))
    r.efg_pct = _f(team_row.get("EFG_PCT"))
    r.ts_pct = _f(team_row.get("TS_PCT"))
    r.pace = _f(team_row.get("PACE"))
    return r


def _f(v) -> Optional[float]:
    try:
        return None if v is None else float(v)
    except (ValueError, TypeError):
        return None


def _season_str_for_date(date_str: str) -> str:
    """NBA season string e.g. '2025-26' for dates Oct 2025–June 2026."""
    y = int(date_str[:4])
    m = int(date_str[5:7])
    if m >= 10:  # Oct–Dec of season start year
        return f"{y}-{str(y+1)[-2:]}"
    return f"{y-1}-{str(y)[-2:]}"


def attach_nba_intel(packs: list[IntelPack], date_str: str) -> None:
    nba_packs = [p for p in packs if p.sport == "NBA"]
    if not nba_packs:
        return
    season = _season_str_for_date(date_str)
    rows = _fetch_team_advanced(season)
    if not rows:
        logger.info("No NBA advanced rows — packs will be light")
    for pack in nba_packs:
        home_row = _match_team(rows, pack.home_team)
        away_row = _match_team(rows, pack.away_team)
        pack.home_nba = _build_ratings(home_row)
        pack.away_nba = _build_ratings(away_row)
        pack.confidence_data = 0.5 if rows else 0.3
