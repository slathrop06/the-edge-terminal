"""Golf majors intel — fetches outright winner odds for the four majors.

The Odds API uses dedicated sport keys (active only during tournament week):
- golf_masters_tournament_winner
- golf_pga_championship_winner
- golf_us_open_winner
- golf_the_open_championship_winner
"""
from __future__ import annotations

import os
from typing import Optional

import requests

from engine.utils import get_logger, retry, nyc_now
from engine.intel.market import _bm_state_code, _sub_state, ENABLED_BOOKS

logger = get_logger("intel-golf")

ODDS_API_BASE = "https://api.the-odds-api.com/v4"

# Sport key → human-readable tournament name
GOLF_MAJORS: dict[str, str] = {
    "golf_masters_tournament_winner":  "The Masters",
    "golf_pga_championship_winner":    "PGA Championship",
    "golf_us_open_winner":             "U.S. Open",
    "golf_the_open_championship_winner": "The Open Championship",
}


@retry(attempts=3, backoff=2)
def _fetch_outrights(sport_key: str) -> list[dict]:
    api_key = os.getenv("ODDS_API_KEY", "")
    if not api_key:
        return []
    r = requests.get(
        f"{ODDS_API_BASE}/sports/{sport_key}/odds",
        params={
            "apiKey": api_key,
            "regions": "us",
            "markets": "outrights",
            "bookmakers": ",".join(sorted(ENABLED_BOOKS)),
            "oddsFormat": "american",
            "dateFormat": "iso",
            "includeLinks": "true",
        },
        timeout=20,
    )
    if r.status_code in (401, 404, 422):
        return []
    r.raise_for_status()
    remaining = r.headers.get("x-requests-remaining")
    logger.info(f"Odds API {sport_key}: {len(r.json())} response — remaining={remaining}")
    return r.json()


def _aggregate_player_odds(tournament_data: dict, state_code: str) -> list[dict]:
    """For each player, collect per-book prices + best price + deep-link.
    Returns list sorted by best price ascending (favorites first)."""
    by_player: dict[str, dict] = {}
    for bk in tournament_data.get("bookmakers", []):
        book_key = bk.get("key", "")
        if book_key not in ENABLED_BOOKS:
            continue
        for mk in bk.get("markets", []):
            for out in mk.get("outcomes", []):
                player = out.get("name", "").strip()
                try:
                    price = int(out.get("price", 0))
                except (TypeError, ValueError):
                    continue
                link = _sub_state(out.get("link"), state_code)
                if not player:
                    continue
                rec = by_player.setdefault(player, {
                    "player": player,
                    "by_book": {},
                    "best_book": "",
                    "best_odds": 0,
                    "best_link": None,
                })
                # American odds: higher = better for bettor (more profit on win)
                if not rec["best_book"] or price > rec["best_odds"]:
                    rec["best_book"] = book_key
                    rec["best_odds"] = price
                    rec["best_link"] = link
                rec["by_book"][book_key] = {"price": price, "link": link}

    # Sort by best price asc (lowest price = favorite)
    return sorted(by_player.values(), key=lambda r: r["best_odds"])


def _is_active_this_week(commence_iso: str) -> bool:
    """A major counts as 'active' if its commence_time is between
    5 days ago (so we catch Sun final round of a Thu-Sun tournament) and
    4 days ahead (so we catch Wed pre-tournament lock). Otherwise it's
    a future/past futures market we should ignore."""
    if not commence_iso:
        return False
    try:
        from datetime import datetime, timezone, timedelta
        t = datetime.fromisoformat(commence_iso.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        return (now - timedelta(days=5)) <= t <= (now + timedelta(days=4))
    except (ValueError, TypeError):
        return False


def harvest_golf_majors() -> list[dict]:
    """Check all four majors. Return a list of tournament packs for any that
    are currently active (within this week's window). Empty list = no major
    in progress. Futures markets months out are filtered out.

    Each pack is a dict with:
        - sport_key, tournament_name, event_id, commence_time
        - players: list of {player, best_odds, best_book, best_link, by_book}
                   sorted favorite-first
        - state_code: BetMGM state code used for deep-link substitution
    """
    state_code = _bm_state_code()
    packs: list[dict] = []
    for sport_key, tournament_name in GOLF_MAJORS.items():
        try:
            data = _fetch_outrights(sport_key)
            if not data:
                continue
            t = data[0]
            commence = t.get("commence_time", "")
            if not _is_active_this_week(commence):
                logger.info(f"Skipping {tournament_name} — commence_time {commence} outside this-week window")
                continue
            players = _aggregate_player_odds(t, state_code)
            if not players:
                continue
            packs.append({
                "sport_key": sport_key,
                "tournament_name": tournament_name,
                "event_id": t.get("id") or sport_key,
                "commence_time": commence,
                "players": players,
                "snapshot_iso": nyc_now().isoformat(),
                "state_code": state_code,
            })
            logger.info(f"Active major: {tournament_name} — {len(players)} players in field")
        except Exception as e:
            logger.warning(f"Failed to harvest {sport_key}: {e}")
    return packs


def top_n(pack: dict, n: int = 30) -> list[dict]:
    """Slice the top N players by best price (favorites first)."""
    return pack["players"][:n]
