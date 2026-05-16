"""Publisher: writes picks to JSON ledger and regenerates site/data.json."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import re

from engine.handicapper import Pick, HandicapperResponse
from engine.intel.types import IntelPack, MarketIntel, BookOdds
from engine.utils import (
    get_logger, nyc_now, nyc_date, read_json, write_json,
    DATA_DIR, SITE_DIR
)
from engine import ladder

logger = get_logger("publisher")

PICKS_HISTORY_PATH = DATA_DIR / "picks_history.json"
DATA_JSON_PATH     = SITE_DIR / "data.json"
ANALYTICS_JSON_PATH = SITE_DIR / "analytics.json"


def _default_history() -> dict:
    return {
        "version": 1,
        "updated_at": nyc_now().isoformat(),
        "picks": [],   # list of pick dicts; status PEND/WIN/LOSS/PUSH
    }


def load_history() -> dict:
    h = read_json(PICKS_HISTORY_PATH, _default_history())
    if "picks" not in h:
        h = _default_history()
    return h


def save_history(history: dict) -> None:
    history["updated_at"] = nyc_now().isoformat()
    write_json(PICKS_HISTORY_PATH, history)


def _match_pack(packs: list[IntelPack], pick: Pick) -> Optional[IntelPack]:
    """Find the intel pack matching the pick's game label."""
    if not packs:
        return None
    g = pick.game.lower()
    sport_packs = [p for p in packs if p.sport == pick.sport]
    # 1. Full name substring match
    for p in sport_packs:
        if p.home_team.lower() in g and p.away_team.lower() in g:
            return p
    # 2. Abbreviation match (Claude often writes "TOR @ DET")
    for p in sport_packs:
        ha, aa = (p.home_abbr or "").lower(), (p.away_abbr or "").lower()
        if ha and aa and ha in g and aa in g:
            return p
    # 3. Token overlap fallback
    for p in sport_packs:
        ht_tokens = [t for t in p.home_team.lower().split() if len(t) > 3]
        at_tokens = [t for t in p.away_team.lower().split() if len(t) > 3]
        if any(t in g for t in ht_tokens) and any(t in g for t in at_tokens):
            return p
    return None


def _book_dict_for_selection(pick: Pick, pack: IntelPack) -> dict[str, BookOdds]:
    """Pick the right per-book odds dict based on the pick string + market type."""
    market = pack.market
    if not market:
        return {}
    pl = pick.pick.lower()
    mkt = pick.market.upper()
    if mkt in ("ML", "MONEYLINE"):
        return market.home_ml_by_book if _is_home_pick(pick, pl, pack) else market.away_ml_by_book
    if mkt in ("RUNLINE", "PUCKLINE", "SPREAD"):
        return market.home_spread_by_book if _is_home_pick(pick, pl, pack) else market.away_spread_by_book
    if mkt in ("TOTAL", "OVER", "UNDER"):
        if "over" in pl:
            return market.over_by_book
        if "under" in pl:
            return market.under_by_book
    return {}


def _is_home_pick(pick: Pick, pick_lower: str, pack: IntelPack) -> bool:
    """Decide if pick.pick refers to the home or away team. Uses full name,
    abbreviation, and meaningful tokens from the pack."""
    home_aliases = {pack.home_team.lower(), (pack.home_abbr or "").lower()}
    away_aliases = {pack.away_team.lower(), (pack.away_abbr or "").lower()}
    home_aliases |= {t for t in pack.home_team.lower().split() if len(t) > 3}
    away_aliases |= {t for t in pack.away_team.lower().split() if len(t) > 3}
    home_aliases = {a for a in home_aliases if a}
    away_aliases = {a for a in away_aliases if a}
    if any(a in pick_lower for a in home_aliases):
        return True
    if any(a in pick_lower for a in away_aliases):
        return False
    return False


def _attach_links_and_prices(pick: Pick, packs: list[IntelPack]) -> None:
    """Use the matching intel pack to fill pick.book_prices + pick.book_links."""
    if pick.market.upper() == "PARLAY":
        return  # parlays don't have a single outcome link
    pack = _match_pack(packs, pick)
    if not pack or not pack.market:
        return
    books = _book_dict_for_selection(pick, pack)
    for book_key, odds in books.items():
        # American odds formatting
        v = odds.price_american
        odds_str = f"{v:+d}" if v > 0 else str(v)
        pick.book_prices.setdefault(book_key, odds_str)
        if odds.link:
            pick.book_links[book_key] = odds.link
    # Fallback to event-level link if no outcome link
    for book_key, ev_link in pack.market.event_link_by_book.items():
        pick.book_links.setdefault(book_key, ev_link)


def _pick_to_dict(pick: Pick, response: HandicapperResponse, date_str: str,
                  *, late_add: bool = False) -> dict:
    d = pick.model_dump()
    d["date"] = date_str
    d["status"] = "PEND"
    d["units_result"] = None
    d["clv_cents"] = None
    d["result_score"] = None
    d["graded_at"] = None
    d["slate_assessment"] = response.slate_assessment
    d["executive_summary"] = getattr(response, "executive_summary", "") or ""
    d["slate_analysis"] = getattr(response, "slate_analysis", "") or ""
    d["slate_vibe"] = response.slate_vibe
    d["published_at"] = nyc_now().isoformat()
    d["locked"] = True              # once published, locked
    d["late_add"] = bool(late_add)
    return d


def publish(
    picks: list[Pick],
    response: HandicapperResponse,
    date_str: Optional[str] = None,
    system_paused: bool = False,
    pause_reason: str = "",
    *,
    mode: str = "morning",           # "morning" or "late_add"
    packs: Optional[list[IntelPack]] = None,
) -> dict:
    """Publish picks for date_str.

    Morning mode (mode="morning"):
      - If today already has locked PEND picks, this is a no-op for those
        (they stay locked). New picks are NOT inserted on top because morning
        is meant to be the canonical first publish.
      - If today has NO locked PEND picks, this is the canonical publish:
        insert all picks, designate a ladder, lock them.

    Late-add mode (mode="late_add"):
      - Existing locked picks are untouched.
      - Picks in this batch are inserted alongside, marked late_add=True.
      - They do NOT get ladder designation (ladder is set at morning lock-in).
    """
    date_str = date_str or nyc_date()
    history = load_history()

    existing_today = [p for p in history["picks"]
                      if p.get("date") == date_str and p.get("status") == "PEND"]
    # Any existing PEND pick for today counts as locked (we published it earlier).
    # The `locked` field was added later; older picks default to locked too.
    has_locked = bool(existing_today)

    # Attach per-book prices + deep links from the matching intel pack
    if packs:
        for pick in picks:
            try:
                _attach_links_and_prices(pick, packs)
            except Exception as e:
                logger.warning(f"Link attach failed for {pick.pick}: {e}")

    if mode == "morning":
        if has_locked:
            logger.info(f"Morning publish skipped: {len(existing_today)} locked picks already exist for {date_str}.")
            # Still regenerate site state so the timestamp updates
            regenerate_site_data(system_paused=system_paused, pause_reason=pause_reason)
            from engine import analytics; analytics.refresh()
            return history
        # Canonical first publish
        ladder.designate_ladder_pick(picks)
        for pick in picks:
            record = _pick_to_dict(pick, response, date_str, late_add=False)
            history["picks"].insert(0, record)
            logger.info(f"INSERTED [LOCKED]: {record['pick']} ({record['game']}) "
                        f"ladder={record.get('ladder_designation', False)}")
        save_history(history)
        logger.info(f"Morning publish: {len(picks)} picks LOCKED for {date_str}")

    elif mode == "late_add":
        # Detect dupes against existing picks (same game + same market + same side wording)
        existing_keys = {(p.get("game"), p.get("market"), p.get("pick")) for p in existing_today}
        added = 0
        for pick in picks:
            key = (pick.game, pick.market, pick.pick)
            if key in existing_keys:
                logger.info(f"Late-add dupe skipped: {pick.pick} ({pick.game})")
                continue
            record = _pick_to_dict(pick, response, date_str, late_add=True)
            # Late adds never get ladder
            record["ladder_designation"] = False
            history["picks"].insert(0, record)
            added += 1
            logger.info(f"INSERTED [LATE ADD]: {record['pick']} ({record['game']})")
        save_history(history)
        logger.info(f"Late-add publish: {added} new picks added for {date_str}")

    else:
        raise ValueError(f"Unknown publish mode: {mode}")

    # Regenerate site/data.json
    regenerate_site_data(system_paused=system_paused, pause_reason=pause_reason)

    # Regenerate site/analytics.json
    from engine import analytics
    analytics.refresh()

    return history


def regenerate_site_data(system_paused: bool = False, pause_reason: str = "") -> None:
    """site/data.json — current state served to the front-end."""
    history = load_history()
    today = nyc_date()
    today_picks = [p for p in history["picks"] if p.get("date") == today]

    payload = {
        "generated_at": nyc_now().isoformat(),
        "today": today,
        "system_paused": system_paused,
        "pause_reason": pause_reason if system_paused else "",
        "today_picks": today_picks,
        "all_picks": history["picks"],
        "ladder": ladder.load_state(),
    }
    write_json(DATA_JSON_PATH, payload)
    logger.info(f"Wrote {DATA_JSON_PATH.name}: {len(today_picks)} today, {len(history['picks'])} total")


def set_system_paused(reason: str) -> None:
    """Write a pause flag to site/data.json so the front-end shows the banner."""
    try:
        regenerate_site_data(system_paused=True, pause_reason=reason)
        logger.error(f"SYSTEM PAUSED: {reason}")
    except Exception as e:
        logger.error(f"Failed to set system_paused: {e}")
