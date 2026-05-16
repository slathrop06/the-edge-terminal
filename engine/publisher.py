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
