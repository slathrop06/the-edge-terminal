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


def _find_score(scores: dict, game_label: str) -> Optional[dict]:
    gl = game_label.lower()
    for v in scores.values():
        if v.get("home_team", "").lower() in gl and v.get("away_team", "").lower() in gl:
            return v
    return None


def _grade_single(pick_data: dict, scores: dict) -> tuple[str, Optional[str]]:
    """Grade a single (non-parlay) selection. pick_data needs 'game', 'pick',
    'market'. Returns (status, result_score)."""
    found = _find_score(scores, pick_data.get("game", ""))
    if not found or found.get("status_state") != "post":
        return "PEND", None

    hs = found["home_score"]; as_ = found["away_score"]
    result_score = f"{as_}-{hs}"
    market = (pick_data.get("market") or "").upper()
    home_name = found["home_team"]; away_name = found["away_team"]

    # Infer market from pick string if missing (parlay legs may not carry market)
    pick_str = pick_data.get("pick", "").lower()
    if not market:
        if "over" in pick_str or "under" in pick_str:
            market = "TOTAL"
        elif "ml" in pick_str or "moneyline" in pick_str:
            market = "ML"
        elif any(s in pick_str for s in ("-1.5", "+1.5", "-2.5", "+2.5", "run line", "puck line", "spread")):
            market = "SPREAD"

    if market in ("ML", "MONEYLINE"):
        status = _grade_moneyline(pick_data, hs, as_, home_name, away_name)
    elif market in ("RUNLINE", "PUCKLINE", "SPREAD"):
        status = _grade_spread(pick_data, hs, as_, home_name, away_name)
    elif market in ("TOTAL", "OVER", "UNDER"):
        status = _grade_total(pick_data, hs, as_)
    else:
        status = None
    return (status or "PEND"), result_score


def _grade_pick(pick: dict, scores: dict) -> tuple[str, Optional[str]]:
    """Top-level pick grader, handles singles and parlays."""
    market = (pick.get("market") or "").upper()
    if market == "PARLAY" and pick.get("legs"):
        leg_statuses = []
        leg_scores = []
        for leg in pick["legs"]:
            # Each leg is a {game, pick, best_book, best_odds}
            leg_status, leg_score = _grade_single(leg, scores)
            leg_statuses.append(leg_status)
            if leg_score:
                leg_scores.append(f"{leg.get('game', '?')}:{leg_score}")
        # If any leg still pending, the parlay is pending
        if "PEND" in leg_statuses:
            return "PEND", None
        # If any leg lost, parlay lost
        if "LOSS" in leg_statuses:
            return "LOSS", " | ".join(leg_scores)
        # If any pushed, parlay typically voids that leg → effectively pushes
        # Convention: push the whole parlay (conservative; the boys can verify)
        if "PUSH" in leg_statuses:
            return "PUSH", " | ".join(leg_scores)
        # All wins
        return "WIN", " | ".join(leg_scores)
    return _grade_single(pick, scores)


def run_grader(date_str: Optional[str] = None) -> None:
    """Grade PEND picks.

    If date_str is None (the cron default), grade ALL PEND picks regardless
    of date. This is robust against GHA cron delays that push the grader
    past midnight ET — if it fires at 2 AM ET, nyc_date() returns the next
    day, and we'd miss yesterday's picks entirely.

    If date_str is provided (manual run), grade only PEND picks for that date.
    """
    history = load_history()
    if date_str:
        pend = [p for p in history["picks"]
                if p.get("date") == date_str and p.get("status") == "PEND"]
        if not pend:
            logger.info(f"No PEND picks on {date_str}")
            return
    else:
        pend = [p for p in history["picks"] if p.get("status") == "PEND"]
        if not pend:
            logger.info("No PEND picks across all dates")
            return

    # Fetch finals for each (sport, date) pair we have PEND picks for
    needed_pairs: set[tuple[SportCode, str]] = {(p["sport"], p["date"]) for p in pend}
    logger.info(f"Grading {len(pend)} PEND pick(s) across {len(needed_pairs)} sport/date pair(s)")
    all_scores: dict[str, dict] = {}
    for sport, d in needed_pairs:
        try:
            all_scores.update(fetch_finals(sport, d))
        except Exception as e:
            logger.error(f"Finals fetch failed for {sport} on {d}: {e}")

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
