"""NFL & CFB intel: team records, points per game, basic efficiency from ESPN.

These sports are out of season in summer 2026 — this module is scaffolded so
when the schedule starts populating in Aug/Sep, the engine has team-level
context. For deeper EPA/DVOA/SP+ work we'll wire dedicated sources when needed.
"""
from __future__ import annotations

from typing import Optional

import requests

from engine.utils import get_logger, retry
from engine.intel.types import IntelPack, TeamRatingsFootball

logger = get_logger("intel-football")


@retry(attempts=2, backoff=2)
def _fetch_team_stats(sport_path: str, team_id: str) -> dict:
    url = f"https://site.api.espn.com/apis/site/v2/sports/{sport_path}/teams/{team_id}"
    r = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
    r.raise_for_status()
    return r.json()


def attach_football_intel(packs: list[IntelPack], date_str: str) -> None:
    """For NFL + CFB packs, attach basic team ratings.

    Out of season → no-op. In-season → light ESPN-based ratings.
    For v1, we keep this minimal — the handicapper will lean on web_search
    for advanced football metrics (EPA, DVOA, weather) when needed.
    """
    football_packs = [p for p in packs if p.sport in ("NFL", "CFB")]
    if not football_packs:
        return
    # Placeholder ratings — present so handicapper knows the structure exists
    for pack in football_packs:
        pack.home_football = TeamRatingsFootball()
        pack.away_football = TeamRatingsFootball()
        pack.confidence_data = 0.30
        pack.notes.append("Football intel is minimal in v1 — handicapper should use web_search for EPA/DVOA/injuries.")
