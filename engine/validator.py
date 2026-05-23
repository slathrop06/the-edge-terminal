"""Deterministic validator — 10 rules. Not Claude. Pure logic."""
from __future__ import annotations

import re

from engine.handicapper import Pick, HandicapperResponse
from engine.utils import get_logger, american_value

logger = get_logger("validator")

ALLOWED_PICK_TYPES = {"ml", "moneyline", "spread", "runline", "puckline",
                      "total", "over", "under", "prop", "parlay"}
FORBIDDEN_PICK_TYPES = {"teaser", "sgp", "same game parlay", "live", "in-game", "correlated"}

EXPECTED_UNITS = {5: 2.0, 4: 1.5, 3: 1.0}

# Ladder must be near even money (American odds band)
LADDER_ODDS_MIN = -125
LADDER_ODDS_MAX = +130


def rule_max_juice(pick: Pick) -> tuple[bool, str]:
    p_lower = pick.pick.lower()
    if pick.market.upper() == "PARLAY" or "parlay" in p_lower:
        return True, "parlay exempt"
    val = american_value(pick.best_odds)
    if val < -150:
        return False, f"juice {pick.best_odds} worse than -150"
    return True, f"juice OK {pick.best_odds}"


def rule_ladder_even_money(pick: Pick) -> tuple[bool, str]:
    """Ladder picks must be priced ~even money (between LADDER_ODDS_MIN and LADDER_ODDS_MAX)."""
    if not pick.ladder_designation:
        return True, "not ladder"
    val = american_value(pick.best_odds)
    if val < LADDER_ODDS_MIN or val > LADDER_ODDS_MAX:
        return False, f"ladder odds {pick.best_odds} outside [{LADDER_ODDS_MIN}, {LADDER_ODDS_MAX}] band"
    return True, f"ladder odds OK ({pick.best_odds})"


def rule_parlay_well_formed(pick: Pick) -> tuple[bool, str]:
    """If market=PARLAY, must have >=2 legs and they must be in different games."""
    if pick.market.upper() != "PARLAY":
        return True, "not parlay"
    if len(pick.legs) < 2:
        return False, f"parlay must have >= 2 legs (got {len(pick.legs)})"
    if len(pick.legs) > 3:
        return False, f"parlay too long ({len(pick.legs)} legs) — max 3"
    games = [leg.game for leg in pick.legs]
    if len(set(games)) < len(games):
        return False, "parlay legs include same game (correlated)"
    return True, f"parlay {len(pick.legs)} legs across {len(set(games))} games"


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
    # Parlays are vetted by rule_parlay_well_formed; their combined pick
    # name can contain "HR" from leg titles even when no leg is an HR prop.
    if pick.market.upper() == "PARLAY":
        return True, "parlay exempt"
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


def _data_text(pick: Pick) -> str:
    return " ".join(f"{d.label} {d.value}" for d in pick.the_data).lower()


def rule_mlb_pitcher_k_check(pick: Pick) -> tuple[bool, str]:
    """Pitcher strikeout props require: pitcher K/9 (season + L3) AND
    opposing offense K% rank. Unders also need a pitch-count cap signal
    (recent IP/start) since manager hooks drive most K-unders."""
    if pick.market.upper() == "PARLAY":
        return True, "parlay exempt"
    pl = pick.pick.lower()
    is_k = bool(re.search(r"\b(k|ks|k's|strikeouts?)\b", pl))
    if not is_k or pick.market.upper() != "PROP":
        return True, "not pitcher K prop"
    txt = _data_text(pick)
    if "k/9" not in txt and "k per 9" not in txt:
        return False, "K prop missing pitcher K/9 in the_data"
    if "k%" not in txt and "k rank" not in txt and "k-rank" not in txt:
        return False, "K prop missing opposing offense K% (or rank) in the_data"
    if "under" in pl and "ip/start" not in txt and "ip per start" not in txt and "pitch count" not in txt:
        return False, "K under missing IP/start or pitch-count signal"
    return True, "pitcher K prop OK"


def rule_mlb_total_bases_check(pick: Pick) -> tuple[bool, str]:
    """Total-bases props require: opposing SP ISO-allowed (or HR/9 + BB/9),
    park PF (runs or HR), and the bettor's lineup-spot context (1-4
    materially different from 7-9)."""
    if pick.market.upper() == "PARLAY":
        return True, "parlay exempt"
    pl = pick.pick.lower()
    is_tb = ("total base" in pl) or bool(re.search(r"\b(tb|tbs)\b", pl))
    if not is_tb or pick.market.upper() != "PROP":
        return True, "not total-bases prop"
    txt = _data_text(pick)
    has_sp_signal = ("iso" in txt) or ("hr/9" in txt) or ("bb/9" in txt) or ("xfip" in txt)
    if not has_sp_signal:
        return False, "TB prop missing opposing SP signal (ISO/HR9/BB9/xFIP)"
    if "pf" not in txt and "park factor" not in txt:
        return False, "TB prop missing park factor in the_data"
    if "lineup" not in txt and "batting" not in txt:
        return False, "TB prop missing lineup-spot context"
    return True, "total-bases prop OK"


def rule_mlb_hits_check(pick: Pick) -> tuple[bool, str]:
    """Hits props require: opposing SP BAA or WHIP, and (for overs) a BB/9
    signal — high-walk pitchers boost BABIP indirectly via pitch counts."""
    if pick.market.upper() == "PARLAY":
        return True, "parlay exempt"
    pl = pick.pick.lower()
    is_hits = bool(re.search(r"\bhits?\b", pl))
    if not is_hits or pick.market.upper() != "PROP":
        return True, "not hits prop"
    txt = _data_text(pick)
    if "baa" not in txt and "whip" not in txt and "babip" not in txt:
        return False, "hits prop missing opposing SP BAA/WHIP/BABIP in the_data"
    if "over" in pl and "bb/9" not in txt and "bb%" not in txt:
        return False, "hits over missing opposing SP BB/9 signal (drives in-play volume)"
    return True, "hits prop OK"


def rule_nba_player_prop_check(pick: Pick) -> tuple[bool, str]:
    """NBA props (points / rebounds / assists / PRA). All require: player's
    per-game baseline for the stat (PPG, RPG, APG, or their sum for PRA),
    opposing team's defensive rank vs that stat, AND a minutes/usage signal
    (recent MPG or USG%) — minutes shifts cause most NBA-prop blowups.
    Combined check (instead of one per market) keeps validator.py compact."""
    if pick.market.upper() == "PARLAY":
        return True, "parlay exempt"
    if pick.market.upper() != "PROP" or (pick.sport or "").upper() != "NBA":
        return True, "not NBA prop"
    pl = pick.pick.lower()
    txt = _data_text(pick)
    # Figure out which NBA market we're checking
    if "pra" in pl:
        if not any(kw in txt for kw in ("pra", "p+r+a", "p/r/a")):
            return False, "PRA prop missing PRA baseline in the_data"
    elif "rebound" in pl or "reb" in pl or "boards" in pl:
        if "rpg" not in txt and "rebounds per game" not in txt and "reb/g" not in txt:
            return False, "rebounds prop missing RPG in the_data"
        if "oreb" not in txt and "defensive rebound" not in txt and "reb rank" not in txt:
            return False, "rebounds prop missing opp DREB%/OREB% (or rank) in the_data"
    elif "assist" in pl or "ast" in pl or "dimes" in pl:
        if "apg" not in txt and "assists per game" not in txt and "ast/g" not in txt:
            return False, "assists prop missing APG in the_data"
        if "pace" not in txt and "usage" not in txt and "usg" not in txt:
            return False, "assists prop missing pace or usage signal"
    elif "point" in pl or "pts" in pl:
        if "ppg" not in txt and "points per game" not in txt and "pts/g" not in txt:
            return False, "points prop missing PPG in the_data"
        if "def rating" not in txt and "drtg" not in txt and "points allowed" not in txt:
            return False, "points prop missing opp defensive context (DRTG / pts allowed)"
    else:
        return False, "NBA prop pick must name a stat (points/rebounds/assists/PRA)"
    # Minutes signal required across all NBA prop types
    if "mpg" not in txt and "minutes per game" not in txt and "min/g" not in txt and "usg" not in txt:
        return False, "NBA prop missing minutes/usage signal (mpg or USG%)"
    return True, "NBA prop OK"


def rule_nhl_shots_check(pick: Pick) -> tuple[bool, str]:
    """NHL shots-on-goal props require: player SOG/game baseline (season + L10),
    line-1 vs line-3 context (TOI on PP matters), and opposing goalie's
    shots-faced/60 — high-volume goalies = better over environments."""
    if pick.market.upper() == "PARLAY":
        return True, "parlay exempt"
    if pick.market.upper() != "PROP" or (pick.sport or "").upper() != "NHL":
        return True, "not NHL prop"
    pl = pick.pick.lower()
    if "shot" not in pl and "sog" not in pl:
        return True, "not SOG prop"
    txt = _data_text(pick)
    if "sog/g" not in txt and "shots/g" not in txt and "sog per game" not in txt:
        return False, "SOG prop missing player SOG/game in the_data"
    if "toi" not in txt and "ice time" not in txt and "pp time" not in txt:
        return False, "SOG prop missing TOI / PP-time signal"
    if "goalie" not in txt and "sa/60" not in txt and "shots faced" not in txt:
        return False, "SOG prop missing opposing goalie shots-faced signal"
    return True, "SOG prop OK"


def rule_mlb_run_line_check(pick: Pick) -> tuple[bool, str]:
    if pick.market.upper() == "PARLAY":
        return True, "parlay exempt"
    pl = pick.pick.lower()
    if "run line" not in pl and "-1.5" not in pl and "+1.5" not in pl:
        return True, "not run line"
    stats_text = " ".join(f"{d.label} {d.value}" for d in pick.the_data).lower()
    if not any(k in stats_text for k in ("r/g", "rpg", "runs per game")):
        return False, "run line missing R/G in the_data"
    return True, "run line OK"


def rule_mlb_under_bb_check(pick: Pick) -> tuple[bool, str]:
    # Parlays aggregate the_data across legs, so a parlay whose name
    # contains "Under" (because one leg is an under) will see the OTHER
    # leg's pitcher stats and wrongly fail this check. Example that hit
    # prod 2026-05-18: a Cubs ML + Rays Under parlay was killed because
    # the Cubs leg's opposing pitcher (Sproat) had BB% 28 — a feature for
    # the Cubs ML leg, not a bug. Parlay well-formedness is handled by
    # rule_parlay_well_formed; leave the per-leg sanity to the model.
    if pick.market.upper() == "PARLAY":
        return True, "parlay exempt"
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
            ("ladder_even_money",          rule_ladder_even_money(pick)),
            ("parlay_well_formed",         rule_parlay_well_formed(pick)),
            ("confidence_units_match",     rule_confidence_units_match(pick)),
            ("data_confidence_floor",      rule_data_confidence_floor(pick)),
            ("pick_type_allowed",          rule_pick_type_allowed(pick)),
            ("mlb_hr_prop_check",          rule_mlb_hr_prop_check(pick)),
            ("mlb_pitcher_k_check",        rule_mlb_pitcher_k_check(pick)),
            ("mlb_total_bases_check",      rule_mlb_total_bases_check(pick)),
            ("mlb_hits_check",             rule_mlb_hits_check(pick)),
            ("nba_player_prop_check",      rule_nba_player_prop_check(pick)),
            ("nhl_shots_check",            rule_nhl_shots_check(pick)),
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
