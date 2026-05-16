"""The Odds API → real cross-book odds, line shopping, movement, RLM."""
from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from typing import Optional

import requests

from engine.utils import (
    get_logger, retry, nyc_now, read_json, write_json, american_to_prob, DATA_DIR
)
from engine.intel.types import IntelPack, MarketIntel, BookOdds, SportCode

logger = get_logger("intel-market")

ODDS_API_BASE = "https://api.the-odds-api.com/v4"
SPORT_KEYS: dict[SportCode, str] = {
    "MLB": "baseball_mlb",
    "NBA": "basketball_nba",
    "NHL": "icehockey_nhl",
    "NFL": "americanfootball_nfl",
    "CFB": "americanfootball_ncaaf",
}

LINE_HISTORY_PATH = DATA_DIR / "line_history.json"


@retry(attempts=3, backoff=2)
def _fetch_odds(sport_key: str) -> list[dict]:
    api_key = os.getenv("ODDS_API_KEY", "")
    if not api_key:
        logger.warning("ODDS_API_KEY not set — skipping odds fetch")
        return []
    url = f"{ODDS_API_BASE}/sports/{sport_key}/odds"
    params = {
        "apiKey": api_key,
        "regions": "us",
        "markets": "h2h,spreads,totals",
        "oddsFormat": "american",
        "dateFormat": "iso",
    }
    r = requests.get(url, params=params, timeout=20)
    if r.status_code == 401:
        logger.error("Odds API 401 — invalid key")
        return []
    if r.status_code == 422:
        logger.warning(f"Odds API 422 for {sport_key} (sport probably out of season)")
        return []
    r.raise_for_status()
    # Useful rate info logged
    remaining = r.headers.get("x-requests-remaining")
    used = r.headers.get("x-requests-used")
    logger.info(f"Odds API {sport_key}: {len(r.json())} games, remaining={remaining} used={used}")
    return r.json()


def _team_match(odds_team: str, pack_home: str, pack_away: str) -> Optional[str]:
    """Map an Odds-API team name to 'home' or 'away' on our pack."""
    a = odds_team.strip().lower()
    h = pack_home.strip().lower()
    aw = pack_away.strip().lower()
    if a == h or a in h or h in a:
        return "home"
    if a == aw or a in aw or aw in a:
        return "away"
    # token-level fallback
    home_tokens = set(h.split())
    away_tokens = set(aw.split())
    odds_tokens = set(a.split())
    if odds_tokens & home_tokens and len(odds_tokens & home_tokens) >= len(odds_tokens & away_tokens):
        return "home"
    if odds_tokens & away_tokens:
        return "away"
    return None


def _best_price(prices: list[BookOdds], prefer_high: bool = True) -> Optional[BookOdds]:
    if not prices:
        return None
    return max(prices, key=lambda p: p.price_american) if prefer_high else min(prices, key=lambda p: p.price_american)


def _devig_two_way(prob_a: float, prob_b: float) -> tuple[float, float]:
    s = prob_a + prob_b
    if s <= 0:
        return 0.5, 0.5
    return prob_a / s, prob_b / s


def _load_line_history() -> dict:
    return read_json(LINE_HISTORY_PATH, {})


def _save_line_history(h: dict) -> None:
    write_json(LINE_HISTORY_PATH, h)


def _opening_snapshot(history: dict, game_id: str, snapshot: dict) -> dict:
    """Return the FIRST snapshot ever recorded for this game (or this if first)."""
    if game_id not in history or not history[game_id]:
        history[game_id] = [snapshot]
        return snapshot
    history[game_id].append(snapshot)
    # Keep last 50 snapshots per game
    if len(history[game_id]) > 50:
        history[game_id] = history[game_id][-50:]
    return history[game_id][0]


def _build_intel_for_game(game: dict, pack: IntelPack, history: dict) -> MarketIntel:
    """Convert one Odds-API game payload to MarketIntel for the matching pack."""
    home_ml: list[BookOdds] = []
    away_ml: list[BookOdds] = []
    home_spread: list[BookOdds] = []
    away_spread: list[BookOdds] = []
    over: list[BookOdds] = []
    under: list[BookOdds] = []
    totals_seen: list[float] = []
    home_spreads_seen: list[float] = []

    for bk in game.get("bookmakers", []):
        book = bk.get("key", "")
        for mk in bk.get("markets", []):
            mkey = mk.get("key", "")
            for out in mk.get("outcomes", []):
                name = out.get("name", "")
                price = out.get("price", 0)
                point = out.get("point")
                try:
                    price = int(price)
                except (TypeError, ValueError):
                    continue
                if mkey == "h2h":
                    side = _team_match(name, pack.home_team, pack.away_team)
                    if side == "home":
                        home_ml.append(BookOdds(book=book, market="h2h", selection=name, price_american=price))
                    elif side == "away":
                        away_ml.append(BookOdds(book=book, market="h2h", selection=name, price_american=price))
                elif mkey == "spreads":
                    side = _team_match(name, pack.home_team, pack.away_team)
                    if side == "home":
                        home_spread.append(BookOdds(book=book, market="spreads", selection=name, line=point, price_american=price))
                        if point is not None:
                            home_spreads_seen.append(point)
                    elif side == "away":
                        away_spread.append(BookOdds(book=book, market="spreads", selection=name, line=point, price_american=price))
                elif mkey == "totals":
                    lower = name.lower()
                    if lower.startswith("over"):
                        over.append(BookOdds(book=book, market="totals", selection="over", line=point, price_american=price))
                        if point is not None:
                            totals_seen.append(point)
                    elif lower.startswith("under"):
                        under.append(BookOdds(book=book, market="totals", selection="under", line=point, price_american=price))

    mi = MarketIntel(
        home_ml_best=_best_price(home_ml, prefer_high=True),
        away_ml_best=_best_price(away_ml, prefer_high=True),
        home_spread_best=_best_price(home_spread, prefer_high=True),
        away_spread_best=_best_price(away_spread, prefer_high=True),
        over_best=_best_price(over, prefer_high=True),
        under_best=_best_price(under, prefer_high=True),
        consensus_total=(sum(totals_seen) / len(totals_seen)) if totals_seen else None,
        consensus_home_spread=(sum(home_spreads_seen) / len(home_spreads_seen)) if home_spreads_seen else None,
        book_count=len(game.get("bookmakers", [])),
        snapshot_iso=nyc_now().isoformat(),
    )
    if mi.home_ml_best and mi.away_ml_best:
        ph = american_to_prob(mi.home_ml_best.price_american)
        pa = american_to_prob(mi.away_ml_best.price_american)
        mi.home_ml_implied_pct, mi.away_ml_implied_pct = _devig_two_way(ph, pa)

    # Movement vs opening
    snapshot = {
        "snap_iso": mi.snapshot_iso,
        "total": mi.consensus_total,
        "home_spread": mi.consensus_home_spread,
        "home_ml_implied": mi.home_ml_implied_pct,
    }
    opening = _opening_snapshot(history, pack.game_id, snapshot)
    mi.total_open = opening.get("total")
    mi.home_spread_open = opening.get("home_spread")
    mi.home_ml_open_pct = opening.get("home_ml_implied")

    return mi


def attach_market_intel(packs: list[IntelPack], sport: SportCode) -> None:
    sport_key = SPORT_KEYS.get(sport)
    if not sport_key:
        return
    games = _fetch_odds(sport_key)
    if not games:
        return

    history = _load_line_history()

    for game in games:
        home = game.get("home_team", "")
        away = game.get("away_team", "")
        # Find matching pack
        match = None
        for pack in packs:
            if pack.sport != sport:
                continue
            if _team_match(home, pack.home_team, pack.away_team) == "home" and \
               _team_match(away, pack.home_team, pack.away_team) == "away":
                match = pack
                break
        if not match:
            continue
        try:
            match.market = _build_intel_for_game(game, match, history)
        except Exception as e:
            logger.warning(f"Market intel build failed for {match.game_id}: {e}")

    _save_line_history(history)
