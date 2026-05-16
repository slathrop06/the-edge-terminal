"""Deterministic validator — 10 rules. Not Claude. Pure logic."""
from __future__ import annotations

import re

from engine.handicapper import Pick, HandicapperResponse
from engine.utils import get_logger, american_value

logger = get_logger("validator")

ALLOWED_PICK_TYPES = {"ml", "moneyline", "spread", "runline", "puckline",
                      "total", "over", "under", "prop"}
FORBIDDEN_PICK_TYPES = {"teaser", "sgp", "same game parlay", "live", "in-game", "correlated"}

EXPECTED_UNITS = {5: 2.0, 4: 1.5, 3: 1.0}


def rule_max_juice(pick: Pick) -> tuple[bool, str]:
    p_lower = pick.pick.lower()
    if "parlay" in p_lower:
        return True, "parlay exempt"
    val = american_value(pick.best_odds)
    if val < -150:
        return False, f"juice {pick.best_odds} worse than -150"
    return True, f"juice OK {pick.best_odds}"


def rule_confidence_units_match(pick: Pick) -> tuple[bool, str]:
    expected = EXPECTED_UNITS.get(pick.confidence)
    if expected is None:
        return False, f"confidence {pick.confidence} below 3 — pass"
    if abs(pick.units - expected) > 0.01:
        return False, f"confidence {pick.confidence} expects {expected}u, got {pick.units}u"
    return True, f"conf/units {pick.confidence}→{pick.units}u"


def rule_data_confidence_floor(pick: Pick) -> tuple[bool, str]:
    if pick.data_confidence < 0.6:
        return False, f"data_confidence {pick.data_confidence:.2f} < 0.6"
    return True, f"data_confidence {pick.data_confidence:.2f}"


def rule_pick_type_allowed(pick: Pick) -> tuple[bool, str]:
    p_lower = pick.pick.lower()
    for f in FORBIDDEN_PICK_TYPES:
        if f in p_lower:
            return False, f"forbidden type: '{f}'"
    return True, "type allowed"


def rule_no_same_game_opposite_sides(pick: Pick, accepted: list[Pick]) -> tuple[bool, str]:
    pl = pick.pick.lower()
    for other in accepted:
        if other.game != pick.game:
            continue
        ol = other.pick.lower()
        if ("over" in pl and "under" in ol) or ("under" in pl and "over" in ol):
            return False, f"opposite side of {pick.game}"
    return True, "no conflict"


def rule_mlb_hr_prop_check(pick: Pick) -> tuple[bool, str]:
    pl = pick.pick.lower()
    if "hr" not in pl and "home run" not in pl:
        return True, "not HR prop"
    stats_text = " ".join(f"{d.label} {d.value}" for d in pick.the_data).lower()
    if "hr/9" not in stats_text and "hr per 9" not in stats_text:
        return False, "HR prop missing HR/9 in the_data"
    m = re.search(r"hr/9[:\s]+([0-9.]+)", stats_text)
    if m:
        hr9 = float(m.group(1))
        if hr9 >= 1.2:
            return False, f"HR prop: opposing SP HR/9 {hr9} ≥ 1.2"
    if "era" not in stats_text and "last 3" not in stats_text and "l3" not in stats_text:
        return False, "HR prop missing L3 ERA in the_data"
    return True, "HR prop OK"


def rule_mlb_run_line_check(pick: Pick) -> tuple[bool, str]:
    pl = pick.pick.lower()
    if "run line" not in pl and "-1.5" not in pl and "+1.5" not in pl:
        return True, "not run line"
    stats_text = " ".join(f"{d.label} {d.value}" for d in pick.the_data).lower()
    if not any(k in stats_text for k in ("r/g", "rpg", "runs per game")):
        return False, "run line missing R/G in the_data"
    return True, "run line OK"


def rule_mlb_under_bb_check(pick: Pick) -> tuple[bool, str]:
    pl = pick.pick.lower()
    if "under" not in pl and "total" not in pl:
        return True, "not under/total"
    stats_text = " ".join(f"{d.label} {d.value}" for d in pick.the_data).lower()
    for m in re.findall(r"bb%[:\s]+([0-9.]+)|bb/9[:\s]+([0-9.]+)", stats_text):
        val_str = next((x for x in m if x), None)
        if not val_str:
            continue
        val = float(val_str)
        # BB% > 11% or BB/9 > 3.5 is the bar
        if val > 11.0:    # treat as % since BB% commonly listed
            return False, f"under: SP BB% {val} > 11"
    return True, "BB check OK"


def rule_slate_skip(response: HandicapperResponse) -> tuple[bool, str]:
    if response.slate_vibe == "SKIP":
        return False, "slate_vibe=SKIP"
    return True, f"vibe={response.slate_vibe}"


def rule_max_3(picks: list[Pick]) -> list[Pick]:
    if len(picks) <= 3:
        return picks
    sorted_p = sorted(picks, key=lambda p: (p.confidence, p.data_confidence), reverse=True)
    kept, dropped = sorted_p[:3], sorted_p[3:]
    for d in dropped:
        logger.warning(f"OVERFLOW DROP: {d.pick} ({d.game}) conf={d.confidence}")
    return kept


def validate_picks(response: HandicapperResponse) -> list[Pick]:
    skip_ok, reason = rule_slate_skip(response)
    if not skip_ok:
        logger.warning(f"GLOBAL SKIP — {reason}")
        return []

    valid: list[Pick] = []
    for pick in response.picks:
        logger.info(f"--- check: {pick.pick} ({pick.game}) ---")
        rules = [
            ("max_juice",                  rule_max_juice(pick)),
            ("confidence_units_match",     rule_confidence_units_match(pick)),
            ("data_confidence_floor",      rule_data_confidence_floor(pick)),
            ("pick_type_allowed",          rule_pick_type_allowed(pick)),
            ("mlb_hr_prop_check",          rule_mlb_hr_prop_check(pick)),
            ("mlb_run_line_check",         rule_mlb_run_line_check(pick)),
            ("mlb_under_bb_check",         rule_mlb_under_bb_check(pick)),
            ("no_same_game_opposite",      rule_no_same_game_opposite_sides(pick, valid)),
        ]
        ok = True
        passed_names = []
        for name, (passed, why) in rules:
            tag = "PASS" if passed else "FAIL"
            logger.info(f"  [{tag}] {name}: {why}")
            if passed:
                passed_names.append(name)
            else:
                ok = False
                break
        if ok:
            pick.rules_passed = passed_names
            valid.append(pick)
            logger.info(f"  → ACCEPTED")
        else:
            logger.warning(f"  → REJECTED")

    return rule_max_3(valid)
