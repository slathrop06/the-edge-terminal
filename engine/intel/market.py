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
from engine.intel.types import IntelPack, MarketIntel, BookOdds, SportCode, PlayerProp, PropMarket

logger = get_logger("intel-market")

ODDS_API_BASE = "https://api.the-odds-api.com/v4"
SPORT_KEYS: dict[SportCode, str] = {
    "MLB": "baseball_mlb",
    "NBA": "basketball_nba",
    "NHL": "icehockey_nhl",
    "NFL": "americanfootball_nfl",
    "CFB": "americanfootball_ncaaf",
}

# Only these three are surfaced anywhere. Easy to extend via config.
ENABLED_BOOKS = {"draftkings", "fanduel", "betmgm"}
BOOK_DISPLAY = {"draftkings": "DraftKings", "fanduel": "FanDuel", "betmgm": "BetMGM"}


def _bm_state_code() -> str:
    """Read BetMGM state code from config.yaml; default 'nj'."""
    import yaml
    try:
        cfg = yaml.safe_load((Path(__file__).parent.parent.parent / "config.yaml").read_text()) or {}
        return (cfg.get("odds_api") or {}).get("betmgm_state_code") or "nj"
    except Exception:
        return "nj"


def _sub_state(url: Optional[str], state_code: str) -> Optional[str]:
    """BetMGM URLs come back with literal '{state}' placeholder. Replace with config code."""
    if not url:
        return url
    return url.replace("{state}", state_code)


# Module path import used inside _bm_state_code
from pathlib import Path

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
        "bookmakers": ",".join(sorted(ENABLED_BOOKS)),
        "oddsFormat": "american",
        "dateFormat": "iso",
        "includeLinks": "true",
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
    """Convert one Odds-API game payload to MarketIntel for the matching pack.
    Only entries from ENABLED_BOOKS (DK/FD/MGM) are kept."""
    home_ml: list[BookOdds] = []
    away_ml: list[BookOdds] = []
    home_spread: list[BookOdds] = []
    away_spread: list[BookOdds] = []
    over: list[BookOdds] = []
    under: list[BookOdds] = []
    home_ml_by_book: dict[str, BookOdds] = {}
    away_ml_by_book: dict[str, BookOdds] = {}
    home_spread_by_book: dict[str, BookOdds] = {}
    away_spread_by_book: dict[str, BookOdds] = {}
    over_by_book: dict[str, BookOdds] = {}
    under_by_book: dict[str, BookOdds] = {}
    totals_seen: list[float] = []
    home_spreads_seen: list[float] = []
    enabled_book_keys_seen: set[str] = set()
    event_link_by_book: dict[str, str] = {}
    state_code = _bm_state_code()

    for bk in game.get("bookmakers", []):
        book = bk.get("key", "")
        if book not in ENABLED_BOOKS:
            continue
        enabled_book_keys_seen.add(book)
        # Event-level link (game page on this book)
        bk_link = _sub_state(bk.get("link"), state_code)
        if bk_link:
            event_link_by_book[book] = bk_link
        for mk in bk.get("markets", []):
            mkey = mk.get("key", "")
            for out in mk.get("outcomes", []):
                name = out.get("name", "")
                price = out.get("price", 0)
                point = out.get("point")
                outcome_link = _sub_state(out.get("link"), state_code)
                try:
                    price = int(price)
                except (TypeError, ValueError):
                    continue
                if mkey == "h2h":
                    side = _team_match(name, pack.home_team, pack.away_team)
                    odds = BookOdds(book=book, market="h2h", selection=name, price_american=price, link=outcome_link)
                    if side == "home":
                        home_ml.append(odds); home_ml_by_book[book] = odds
                    elif side == "away":
                        away_ml.append(odds); away_ml_by_book[book] = odds
                elif mkey == "spreads":
                    side = _team_match(name, pack.home_team, pack.away_team)
                    odds = BookOdds(book=book, market="spreads", selection=name, line=point, price_american=price, link=outcome_link)
                    if side == "home":
                        home_spread.append(odds); home_spread_by_book[book] = odds
                        if point is not None:
                            home_spreads_seen.append(point)
                    elif side == "away":
                        away_spread.append(odds); away_spread_by_book[book] = odds
                elif mkey == "totals":
                    lower = name.lower()
                    odds = BookOdds(book=book, market="totals", selection=lower, line=point, price_american=price, link=outcome_link)
                    if lower.startswith("over"):
                        over.append(odds); over_by_book[book] = odds
                        if point is not None:
                            totals_seen.append(point)
                    elif lower.startswith("under"):
                        under.append(odds); under_by_book[book] = odds

    mi = MarketIntel(
        home_ml_best=_best_price(home_ml, prefer_high=True),
        away_ml_best=_best_price(away_ml, prefer_high=True),
        home_spread_best=_best_price(home_spread, prefer_high=True),
        away_spread_best=_best_price(away_spread, prefer_high=True),
        over_best=_best_price(over, prefer_high=True),
        under_best=_best_price(under, prefer_high=True),
        home_ml_by_book=home_ml_by_book,
        away_ml_by_book=away_ml_by_book,
        home_spread_by_book=home_spread_by_book,
        away_spread_by_book=away_spread_by_book,
        over_by_book=over_by_book,
        under_by_book=under_by_book,
        consensus_total=(sum(totals_seen) / len(totals_seen)) if totals_seen else None,
        consensus_home_spread=(sum(home_spreads_seen) / len(home_spreads_seen)) if home_spreads_seen else None,
        event_link_by_book=event_link_by_book,
        book_count=len(enabled_book_keys_seen),
        snapshot_iso=nyc_now().isoformat(),
    )
    if mi.home_ml_best and mi.away_ml_best:
        ph = american_to_prob(mi.home_ml_best.price_american)
        pa = american_to_prob(mi.away_ml_best.price_american)
        mi.home_ml_implied_pct, mi.away_ml_implied_pct = _devig_two_way(ph, pa)

    # Movement vs opening AND CLV source data: store best prices per market
    # so the grader can compute closing line value later.
    snapshot = {
        "snap_iso": mi.snapshot_iso,
        "total": mi.consensus_total,
        "home_spread": mi.consensus_home_spread,
        "home_ml_implied": mi.home_ml_implied_pct,
        "home_ml_price":    mi.home_ml_best.price_american    if mi.home_ml_best    else None,
        "away_ml_price":    mi.away_ml_best.price_american    if mi.away_ml_best    else None,
        "home_spread_price": mi.home_spread_best.price_american if mi.home_spread_best else None,
        "away_spread_price": mi.away_spread_best.price_american if mi.away_spread_best else None,
        "home_spread_line":  mi.home_spread_best.line          if mi.home_spread_best else None,
        "over_price":  mi.over_best.price_american  if mi.over_best  else None,
        "over_line":   mi.over_best.line            if mi.over_best  else None,
        "under_price": mi.under_best.price_american if mi.under_best else None,
        "under_line":  mi.under_best.line           if mi.under_best else None,
    }
    opening = _opening_snapshot(history, pack.game_id, snapshot)
    mi.total_open = opening.get("total")
    mi.home_spread_open = opening.get("home_spread")
    mi.home_ml_open_pct = opening.get("home_ml_implied")

    return mi


@retry(attempts=2, backoff=2)
def _fetch_event_props(sport_key: str, event_id: str, market_key: str) -> Optional[dict]:
    """Fetch a single player-prop market for one game from The Odds API.

    Player props live on a different endpoint than sides/totals — per-event
    odds at /sports/{sport}/events/{event_id}/odds. Each call costs 1 API
    credit per market per region per book set, so we call this sparingly
    (HR-only for V1, top-N games by total).
    """
    api_key = os.getenv("ODDS_API_KEY", "")
    if not api_key:
        return None
    url = f"{ODDS_API_BASE}/sports/{sport_key}/events/{event_id}/odds"
    params = {
        "apiKey": api_key,
        "regions": "us",
        "markets": market_key,
        "bookmakers": ",".join(sorted(ENABLED_BOOKS)),
        "oddsFormat": "american",
        "dateFormat": "iso",
        "includeLinks": "true",
    }
    r = requests.get(url, params=params, timeout=20)
    if r.status_code == 401:
        logger.error("Odds API 401 — invalid key (props)")
        return None
    if r.status_code in (404, 422):
        return None  # market not offered for this event
    r.raise_for_status()
    remaining = r.headers.get("x-requests-remaining")
    used = r.headers.get("x-requests-used")
    logger.info(f"Odds API props {market_key} for {event_id}: remaining={remaining} used={used}")
    return r.json()


def _build_hr_props_for_game(game_payload: dict) -> list[PlayerProp]:
    """Parse The Odds API per-event response for batter_home_runs into PlayerProp list.

    Outcome shape from the API:
        {"name": "Over", "description": "Aaron Judge", "price": 250, "point": 0.5, "link": "..."}
    The "description" carries the player name; "name" is Over/Under.
    """
    state_code = _bm_state_code()
    by_player: dict[tuple[str, float], dict] = {}  # (player, line) → {"over": {}, "under": {}}

    for bk in game_payload.get("bookmakers", []):
        book = bk.get("key", "")
        if book not in ENABLED_BOOKS:
            continue
        for mk in bk.get("markets", []):
            if mk.get("key") != "batter_home_runs":
                continue
            for out in mk.get("outcomes", []):
                player = (out.get("description") or "").strip()
                if not player:
                    continue
                try:
                    price = int(out.get("price", 0))
                except (TypeError, ValueError):
                    continue
                line = out.get("point")
                if line is None:
                    continue
                side = (out.get("name") or "").strip().lower()
                if side not in ("over", "under"):
                    continue
                link = _sub_state(out.get("link"), state_code)
                key = (player, float(line))
                slot = by_player.setdefault(key, {"over": {}, "under": {}})
                slot[side][book] = BookOdds(
                    book=book, market="batter_home_runs", selection=f"{player} {side} {line}",
                    line=float(line), price_american=price, link=link,
                )

    props: list[PlayerProp] = []
    for (player, line), slots in by_player.items():
        over_dict = slots["over"]
        under_dict = slots["under"]
        if not over_dict and not under_dict:
            continue
        over_best = max(over_dict.values(), key=lambda b: b.price_american) if over_dict else None
        under_best = max(under_dict.values(), key=lambda b: b.price_american) if under_dict else None
        props.append(PlayerProp(
            player_name=player,
            market="batter_home_runs",
            line=line,
            over_best=over_best,
            under_best=under_best,
            over_by_book=over_dict,
            under_by_book=under_dict,
        ))
    return props


def attach_hr_props(packs: list[IntelPack], sport: SportCode,
                    event_id_by_pack: dict[str, str], max_games: int = 6) -> None:
    """Fetch HR props for up to max_games packs ranked by consensus_total
    (highest totals = best HR environments — pitcher-quality, park, weather
    all favor the over). Caps API spend at ~6 prop calls per morning run.

    event_id_by_pack maps pack.game_id → The Odds API event id (obtained
    from the same /sports/{sport}/odds payload during attach_market_intel).
    """
    sport_key = SPORT_KEYS.get(sport)
    if not sport_key:
        return
    # Rank by consensus_total descending; packs without a total get pushed
    # to the back (no market data → no HR props worth fetching).
    ranked = sorted(
        [p for p in packs if p.sport == sport and p.game_id in event_id_by_pack],
        key=lambda p: (p.market.consensus_total if (p.market and p.market.consensus_total) else 0),
        reverse=True,
    )[:max_games]
    for pack in ranked:
        event_id = event_id_by_pack.get(pack.game_id)
        if not event_id:
            continue
        try:
            payload = _fetch_event_props(sport_key, event_id, "batter_home_runs")
        except Exception as e:
            logger.warning(f"HR prop fetch failed for {pack.game_id}: {e}")
            continue
        if not payload:
            continue
        hr_props = _build_hr_props_for_game(payload)
        if hr_props:
            pack.props = PropMarket(hr_props=hr_props)
            logger.info(f"Attached {len(hr_props)} HR prop lines to {pack.game_id}")


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
            # Capture event id so a follow-up step can fetch player props for
            # this game via /sports/{sport}/events/{event_id}/odds.
            ev_id = game.get("id")
            if ev_id:
                match.odds_api_event_id = ev_id
        except Exception as e:
            logger.warning(f"Market intel build failed for {match.game_id}: {e}")

    _save_line_history(history)
