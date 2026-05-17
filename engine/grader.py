"""Grader: fetch final scores from ESPN, mark picks WIN/LOSS/PUSH, update ladder."""
from __future__ import annotations

import re
from typing import Optional

from engine.utils import (
    get_logger, nyc_now, nyc_date, units_profit, american_to_prob,
    read_json, DATA_DIR
)
from engine.intel.schedule import fetch_finals
from engine.intel.types import SportCode
from engine.publisher import load_history, save_history, regenerate_site_data
from engine import ladder

LINE_HISTORY_PATH = DATA_DIR / "line_history.json"

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
    """Match a pick's game label (e.g. 'MIA @ TB' or 'Miami Marlins @ Tampa Bay Rays')
    against ESPN's finals dict. Tries full name AND abbreviation."""
    gl = game_label.lower()
    # 1. Full-name substring match
    for v in scores.values():
        ht = v.get("home_team", "").lower()
        at = v.get("away_team", "").lower()
        if ht and at and ht in gl and at in gl:
            return v
    # 2. Abbreviation match (Claude typically writes 'MIA @ TB')
    for v in scores.values():
        ha = v.get("home_abbr", "").lower()
        aa = v.get("away_abbr", "").lower()
        if ha and aa and ha in gl and aa in gl:
            return v
    # 3. Token-overlap fallback
    for v in scores.values():
        ht = v.get("home_team", "").lower()
        at = v.get("away_team", "").lower()
        ht_tokens = [t for t in ht.split() if len(t) > 3]
        at_tokens = [t for t in at.split() if len(t) > 3]
        if any(t in gl for t in ht_tokens) and any(t in gl for t in at_tokens):
            return v
    return None


def _grade_single(pick_data: dict, scores: dict) -> tuple[str, Optional[str]]:
    """Grade a single (non-parlay) selection. pick_data needs 'game', 'pick',
    'market'. Returns (status, result_score)."""
    found = _find_score(scores, pick_data.get("game", ""))
    if not found or found.get("status_state") != "post":
        return "PEND", None

    hs = found["home_score"]; as_ = found["away_score"]
    # Team-labeled score for human readability — "MIA 10 · TB 5"
    away_label = found.get("away_abbr") or found.get("away_team", "AWAY")
    home_label = found.get("home_abbr") or found.get("home_team", "HOME")
    result_score = f"{away_label} {as_} · {home_label} {hs}"
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


def _closing_snapshot(pick: dict) -> Optional[dict]:
    """Find the last (closest-to-game-start) line_history snapshot for this pick's game."""
    history = read_json(LINE_HISTORY_PATH, {})
    snaps = history.get(pick.get("game_id") or "", [])
    # game_id on the pick was set when intel was harvested; but some early picks may not have it.
    if not snaps:
        # Try to find by matching IntelPack id format SPORT-{ESPN_ID}-...
        # We don't have ESPN_ID on the pick. Best-effort: scan all keys for any with matching game label tokens
        gl = pick.get("game", "").lower()
        for gid, gid_snaps in history.items():
            # game_id format is e.g. "MLB-401696000" — no name embedded. Skip — without explicit linkage, we can't match.
            pass
    if not snaps:
        return None
    return snaps[-1]  # latest snapshot = closest to close


def _compute_clv(pick: dict) -> Optional[float]:
    """CLV in cents = (closing implied probability − our published implied probability) × 100.
    Positive = we beat the close. Negative = the line moved away from us."""
    snap = _closing_snapshot(pick)
    if not snap:
        return None
    market = (pick.get("market") or "").upper()
    pl = (pick.get("pick") or "").lower()
    pick_odds = pick.get("best_odds") or ""
    if not pick_odds:
        return None
    our_prob = american_to_prob(pick_odds)

    close_price = None
    if market in ("ML", "MONEYLINE"):
        # Need to know if pick is home or away — use _is_home_pick helper if available
        from engine.publisher import _is_home_pick, _match_pack
        # We don't have packs at grade time, but we can heuristically check team tokens
        game = pick.get("game", "")
        if " @ " in game:
            away, home = [s.strip() for s in game.split(" @ ", 1)]
            home_tokens = [t for t in home.lower().split() if len(t) > 2]
            away_tokens = [t for t in away.lower().split() if len(t) > 2]
            if any(t in pl for t in home_tokens):
                close_price = snap.get("home_ml_price")
            elif any(t in pl for t in away_tokens):
                close_price = snap.get("away_ml_price")
    elif market in ("RUNLINE", "PUCKLINE", "SPREAD"):
        # similar home/away routing — simplified
        if "+" in pl and "-" not in pl.split("+", 1)[1][:5]:
            # picked the underdog (positive spread)
            pass
        # Could refine; for v1 fall back to consensus implied prob
        close_price = snap.get("home_spread_price") or snap.get("away_spread_price")
    elif market in ("TOTAL", "OVER", "UNDER"):
        if "over" in pl:
            close_price = snap.get("over_price")
        elif "under" in pl:
            close_price = snap.get("under_price")

    if close_price is None:
        return None
    close_prob = american_to_prob(close_price)
    return round((close_prob - our_prob) * 100, 2)


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
            # CLV — compute even on losses, even pushes
            try:
                clv = _compute_clv(pick)
                if clv is not None:
                    pick["clv_cents"] = clv
            except Exception as e:
                logger.debug(f"CLV compute failed for {pick.get('id')}: {e}")
            logger.info(
                f"GRADED: {pick['pick']} ({pick['game']}) → {status} "
                f"({pick['units_result']:+.2f}u, CLV={pick.get('clv_cents')})"
            )

            if pick.get("ladder_designation"):
                ladder.update_after_grading(pick, status)

            # Run autopsy on losses + embed result on the pick record so site can show it
            if status == "LOSS":
                try:
                    from engine import autopsy
                    entry = autopsy.run_autopsy(pick, result_score or "")
                    if entry:
                        pick["autopsy"] = {
                            "classification": entry.get("classification"),
                            "post_mortem": entry.get("post_mortem"),
                            "candidate_rule": entry.get("candidate_rule"),
                            "sample_size_warning": entry.get("sample_size_warning"),
                        }
                except Exception as e:
                    logger.error(f"Autopsy failed for {pick.get('id')}: {e}")
        except Exception as e:
            logger.error(f"Grade failed for {pick.get('id')}: {e}")

    save_history(history)

    # Generate night recaps for any dates with newly-graded main picks
    try:
        from engine import night_recap, ladder
        ladder_state = ladder.load_state()
        graded_dates = {p["date"] for p in pend if p.get("status") in ("WIN", "LOSS", "PUSH")}
        for d in sorted(graded_dates):
            night_recap.generate_recap(d, history["picks"], ladder_state)
    except Exception as e:
        logger.error(f"Night recap generation failed: {e}")

    regenerate_site_data()
    from engine import analytics
    analytics.refresh()
    logger.info("=== GRADER COMPLETE ===")
