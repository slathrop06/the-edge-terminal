"""Grader: fetch final scores from ESPN, mark picks WIN/LOSS/PUSH, update ladder."""
from __future__ import annotations

import re
from typing import Optional

from engine.utils import (
    get_logger, nyc_now, nyc_date, units_profit, american_to_prob
)
from engine.intel.schedule import fetch_finals
from engine.intel.types import SportCode
from engine.publisher import load_history, save_history, regenerate_site_data
from engine import ladder

logger = get_logger("grader")


def _grade_moneyline(pick: dict, home_score: int, away_score: int, home_name: str, away_name: str) -> Optional[str]:
    text = pick.get("pick", "").lower()
    h = home_name.lower(); a = away_name.lower()
    picked_home = h in text or any(tok in text for tok in h.split() if len(tok) > 3)
    picked_away = a in text or any(tok in text for tok in a.split() if len(tok) > 3)
    if not picked_home and not picked_away:
        return None
    if home_score == away_score:
        return "PUSH"
    home_won = home_score > away_score
    if (picked_home and home_won) or (picked_away and not home_won):
        return "WIN"
    return "LOSS"


def _grade_spread(pick: dict, home_score: int, away_score: int, home_name: str, away_name: str) -> Optional[str]:
    text = pick.get("pick", "")
    m = re.search(r"([+-]\d+\.?\d*)", text)
    if not m:
        return None
    spread = float(m.group(1))
    tl = text.lower()
    h = home_name.lower(); a = away_name.lower()
    if any(tok in tl for tok in h.split() if len(tok) > 3):
        margin = (home_score - away_score) + spread
    elif any(tok in tl for tok in a.split() if len(tok) > 3):
        margin = (away_score - home_score) + spread
    else:
        return None
    if margin > 0: return "WIN"
    if margin < 0: return "LOSS"
    return "PUSH"


def _grade_total(pick: dict, home_score: int, away_score: int) -> Optional[str]:
    text = pick.get("pick", "")
    m = re.search(r"([0-9]+\.?[0-9]*)", text)
    if not m:
        return None
    line = float(m.group(1))
    total = home_score + away_score
    tl = text.lower()
    if "over" in tl:
        if total > line: return "WIN"
        if total < line: return "LOSS"
        return "PUSH"
    if "under" in tl:
        if total < line: return "WIN"
        if total > line: return "LOSS"
        return "PUSH"
    return None


def _grade_pick(pick: dict, scores: dict) -> tuple[str, Optional[str]]:
    eid = pick.get("game_id") or pick.get("id", "")
    sport = pick.get("sport", "")
    # Try direct match by ESPN id buried in our id format "{SPORT}-{ESPN_ID}-..."
    found = None
    for k, v in scores.items():
        if k in pick.get("id", "") or k.startswith(f"{sport}-"):
            # Match by team names in game label
            game = pick.get("game", "")
            if v["home_team"].lower() in game.lower() or v["away_team"].lower() in game.lower():
                found = v
                break
    if not found:
        # Token match
        game = pick.get("game", "").lower()
        for k, v in scores.items():
            if v.get("home_team", "").lower() in game and v.get("away_team", "").lower() in game:
                found = v
                break
    if not found or found.get("status_state") != "post":
        return "PEND", None

    hs = found["home_score"]; as_ = found["away_score"]
    result_score = f"{as_}-{hs}"
    market = pick.get("market", "").upper()
    home_name = found["home_team"]; away_name = found["away_team"]

    if market in ("ML", "MONEYLINE"):
        status = _grade_moneyline(pick, hs, as_, home_name, away_name)
    elif market in ("RUNLINE", "PUCKLINE", "SPREAD"):
        status = _grade_spread(pick, hs, as_, home_name, away_name)
    elif market in ("TOTAL", "OVER", "UNDER"):
        status = _grade_total(pick, hs, as_)
    else:
        status = None  # props/parlays need manual grading
    return (status or "PEND"), result_score


def run_grader(date_str: Optional[str] = None) -> None:
    date_str = date_str or nyc_date()
    history = load_history()
    pend = [p for p in history["picks"] if p.get("date") == date_str and p.get("status") == "PEND"]
    if not pend:
        logger.info(f"No PEND picks on {date_str}")
        return

    sports_needed: set[SportCode] = {p["sport"] for p in pend}
    all_scores: dict[str, dict] = {}
    for sport in sports_needed:
        try:
            all_scores.update(fetch_finals(sport, date_str))
        except Exception as e:
            logger.error(f"Finals fetch failed for {sport}: {e}")

    for pick in pend:
        try:
            status, result_score = _grade_pick(pick, all_scores)
            if status == "PEND":
                logger.info(f"Still PEND: {pick['pick']} ({pick['game']})")
                continue
            pick["status"] = status
            pick["result_score"] = result_score
            pick["units_result"] = units_profit(float(pick.get("units", 1.0)), pick.get("best_odds", "-110"), status)
            pick["graded_at"] = nyc_now().isoformat()
            logger.info(f"GRADED: {pick['pick']} ({pick['game']}) → {status} ({pick['units_result']:+.2f}u)")

            if pick.get("ladder_designation"):
                ladder.update_after_grading(pick, status)

            # Run autopsy on losses
            if status == "LOSS":
                try:
                    from engine import autopsy
                    autopsy.run_autopsy(pick, result_score or "")
                except Exception as e:
                    logger.error(f"Autopsy failed for {pick.get('id')}: {e}")
        except Exception as e:
            logger.error(f"Grade failed for {pick.get('id')}: {e}")

    save_history(history)
    regenerate_site_data()
    from engine import analytics
    analytics.refresh()
    logger.info("=== GRADER COMPLETE ===")
