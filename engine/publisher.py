"""Publisher: writes picks to JSON ledger and regenerates site/data.json."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from engine.handicapper import Pick, HandicapperResponse
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


def _pick_to_dict(pick: Pick, response: HandicapperResponse, date_str: str) -> dict:
    d = pick.model_dump()
    d["date"] = date_str
    d["status"] = "PEND"
    d["units_result"] = None
    d["clv_cents"] = None
    d["result_score"] = None
    d["graded_at"] = None
    d["slate_assessment"] = response.slate_assessment
    d["slate_vibe"] = response.slate_vibe
    d["published_at"] = nyc_now().isoformat()
    return d


def publish(
    picks: list[Pick],
    response: HandicapperResponse,
    date_str: Optional[str] = None,
    system_paused: bool = False,
    pause_reason: str = "",
) -> dict:
    date_str = date_str or nyc_date()
    history = load_history()

    # Each morning run is the canonical pick set for today.
    # Wipe any PEND picks for date_str (graded picks are preserved untouched).
    before = len(history["picks"])
    history["picks"] = [p for p in history["picks"]
                        if not (p.get("date") == date_str and p.get("status") == "PEND")]
    removed = before - len(history["picks"])
    if removed:
        logger.info(f"Cleared {removed} prior PEND picks for {date_str} before re-publishing")

    # Designate ladder pick on the fresh set
    ladder.designate_ladder_pick(picks)

    inserted = 0
    for pick in picks:
        record = _pick_to_dict(pick, response, date_str)
        history["picks"].insert(0, record)
        inserted += 1
        logger.info(f"INSERTED: {record['pick']} ({record['game']}) ladder={record.get('ladder_designation', False)}")

    save_history(history)
    logger.info(f"Picks committed: {inserted} for {date_str}, history total {len(history['picks'])}")

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
