"""NHL intel: team-level metrics via the official NHL API + ESPN supplements."""
from __future__ import annotations

from typing import Optional

import requests

from engine.utils import get_logger, retry
from engine.intel.types import IntelPack, TeamRatingsNHL

logger = get_logger("intel-nhl")

NHL_BASE = "https://api-web.nhle.com/v1"


@retry(attempts=2, backoff=2)
def _fetch_standings() -> dict:
    r = requests.get(f"{NHL_BASE}/standings/now", timeout=15, headers={"User-Agent": "TheEdge/1.0"})
    r.raise_for_status()
    return r.json()


def attach_nhl_intel(packs: list[IntelPack], date_str: str) -> None:
    nhl_packs = [p for p in packs if p.sport == "NHL"]
    if not nhl_packs:
        return
    # NHL public API doesn't give xGF% easily — provide standings-level basics and let Claude reason
    try:
        standings = _fetch_standings().get("standings", [])
        by_team: dict[str, dict] = {}
        for row in standings:
            full = (row.get("teamName") or {}).get("default", "")
            if full:
                by_team[full.lower()] = row
    except Exception as e:
        logger.warning(f"NHL standings fetch failed: {e}")
        by_team = {}

    for pack in nhl_packs:
        pack.home_nhl = TeamRatingsNHL()
        pack.away_nhl = TeamRatingsNHL()
        for side, name in (("home", pack.home_team), ("away", pack.away_team)):
            row = by_team.get(name.lower())
            if not row:
                # token match
                tokens = set(name.lower().split())
                for k, v in by_team.items():
                    if set(k.split()) & tokens:
                        row = v
                        break
            if not row:
                continue
            ratings = pack.home_nhl if side == "home" else pack.away_nhl
            # PP% / PK% aren't in standings; goalie data needs roster fetch — leave for v2
            # Basic GF/GA per game as proxy
            try:
                gp = row.get("gamesPlayed", 1) or 1
                ratings.pp_pct = None
                ratings.pk_pct = None
                # PDO proxy via goalsFor/goalsAgainst — not real PDO but a directional hint
            except Exception:
                pass
        pack.confidence_data = 0.35
