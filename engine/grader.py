"""Grader: fetch final scores from ESPN, mark picks WIN/LOSS/PUSH, update ladder."""
from __future__ import annotations

import re
from typing import Optional

import requests

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


# ─── Prop grading ───────────────────────────────────────────────────────────
# Each prop pick string has the shape: "{Player Name} {Stat Token} Over/Under {Line}".
# Examples: "Aaron Judge HR Over 0.5", "Garrett Crochet Ks Under 6.5",
#           "Jayson Tatum Points Over 28.5", "Auston Matthews SOG Over 4.5".
# We parse out the player + stat + line + side, then fetch the player's actual
# game stat from the right league API and compare.

# Stat-token patterns. The pattern matches in the pick string (case-insensitive);
# the player name is everything BEFORE the earliest match.
_PROP_STAT_PATTERNS: list[tuple[str, str, str]] = [
    # (regex pattern, sport, stat_key — what the per-sport fetcher looks up)
    (r"\b(?:hr|home runs?)\b",                "MLB",  "homeRuns"),
    (r"\b(?:k|ks|k's|strikeouts?)\b",         "MLB",  "strikeOuts"),
    (r"\b(?:tb|tbs|total bases?)\b",          "MLB",  "totalBases"),
    (r"\bhits?\b",                            "MLB",  "hits"),
    (r"\bpra\b",                              "NBA",  "pra"),
    (r"\b(?:points?|pts)\b",                  "NBA",  "points"),
    (r"\b(?:rebounds?|reb|boards)\b",         "NBA",  "rebounds"),
    (r"\b(?:assists?|ast|dimes)\b",           "NBA",  "assists"),
    (r"\b(?:sog|shots?(?: on goal)?)\b",      "NHL",  "shots"),
]


def _parse_prop(pick_str: str) -> Optional[dict]:
    """Parse a prop pick string into {player, sport, stat, line, side}.
    Returns None if the string doesn't look like a parseable prop."""
    if not pick_str:
        return None
    pl = pick_str.lower()
    if "over" in pl:
        side = "over"
    elif "under" in pl:
        side = "under"
    else:
        return None
    # Line is the number after over/under
    m = re.search(r"(?:over|under)\s+(\d+(?:\.\d+)?)", pl)
    if not m:
        return None
    line = float(m.group(1))
    # Stat token — take the earliest match (rebounds/reb both valid; rebounds
    # wins because it's earlier in the string for "Jayson Tatum Rebounds Over 7.5").
    best: Optional[tuple[int, str, str]] = None  # (start_idx, sport, stat_key)
    for pattern, sport, stat in _PROP_STAT_PATTERNS:
        mm = re.search(pattern, pl)
        if mm and (best is None or mm.start() < best[0]):
            best = (mm.start(), sport, stat)
    if best is None:
        return None
    start_idx, sport, stat = best
    player = pick_str[:start_idx].strip().rstrip(",-")
    if not player:
        return None
    return {"player": player, "sport": sport, "stat": stat,
            "line": line, "side": side}


# Cached player-id lookups so a slate's grades don't make N calls per name.
_MLB_PLAYER_ID_CACHE: dict[str, Optional[int]] = {}
_NBA_PLAYER_ID_CACHE: dict[str, Optional[int]] = {}


def _mlb_player_id(name: str) -> Optional[int]:
    if name in _MLB_PLAYER_ID_CACHE:
        return _MLB_PLAYER_ID_CACHE[name]
    try:
        r = requests.get("https://statsapi.mlb.com/api/v1/people/search",
                         params={"names": name, "sportId": 1},
                         headers={"User-Agent": "TheEdge/1.0"}, timeout=10)
        if r.status_code == 200:
            people = r.json().get("people") or []
            if people:
                _MLB_PLAYER_ID_CACHE[name] = int(people[0]["id"])
                return _MLB_PLAYER_ID_CACHE[name]
    except Exception as e:
        logger.debug(f"MLB player lookup failed for {name}: {e}")
    _MLB_PLAYER_ID_CACHE[name] = None
    return None


def _mlb_player_game_stat(player_name: str, stat: str, date: str) -> Optional[float]:
    """Fetch a player's stat for a specific game date from MLB Stats API.
    K props are pitcher strikeouts; HR/TB/hits are hitter stats."""
    pid = _mlb_player_id(player_name)
    if not pid:
        return None
    group = "pitching" if stat == "strikeOuts" else "hitting"
    try:
        season = int(date.split("-")[0])
        r = requests.get(f"https://statsapi.mlb.com/api/v1/people/{pid}/stats",
                         params={"stats": "gameLog", "group": group, "season": season},
                         headers={"User-Agent": "TheEdge/1.0"}, timeout=10)
        if r.status_code != 200:
            return None
        for s in r.json().get("stats", []):
            for split in s.get("splits", []):
                if split.get("date") == date:
                    val = (split.get("stat") or {}).get(stat)
                    if val is None:
                        return None
                    try:
                        return float(val)
                    except (TypeError, ValueError):
                        return None
    except Exception as e:
        logger.debug(f"MLB game stat fetch failed for {player_name}: {e}")
    return None


def _nba_player_id(name: str) -> Optional[int]:
    if name in _NBA_PLAYER_ID_CACHE:
        return _NBA_PLAYER_ID_CACHE[name]
    try:
        from nba_api.stats.static import players as nba_players
        matches = nba_players.find_players_by_full_name(name)
        if matches:
            _NBA_PLAYER_ID_CACHE[name] = int(matches[0]["id"])
            return _NBA_PLAYER_ID_CACHE[name]
    except Exception as e:
        logger.debug(f"NBA player lookup failed for {name}: {e}")
    _NBA_PLAYER_ID_CACHE[name] = None
    return None


def _nba_player_game_stat(player_name: str, stat: str, date: str) -> Optional[float]:
    """Fetch NBA player stat from nba_api playergamelog. NBA seasons span two
    years — pick the right season string ('2025-26' for an Apr 2026 game)."""
    pid = _nba_player_id(player_name)
    if not pid:
        return None
    try:
        from nba_api.stats.endpoints import playergamelog
        # NBA season: Oct YYYY → Jun YYYY+1. Date in Jul-Sep is offseason; pick
        # the previous season for those (rare for our active picks).
        from datetime import datetime
        d = datetime.strptime(date, "%Y-%m-%d")
        season_start = d.year if d.month >= 10 else d.year - 1
        season_str = f"{season_start}-{str(season_start + 1)[-2:]}"
        gl = playergamelog.PlayerGameLog(player_id=pid, season=season_str).get_dict()
        result_sets = gl.get("resultSets") or []
        if not result_sets:
            return None
        headers = result_sets[0].get("headers") or []
        rows = result_sets[0].get("rowSet") or []
        idx = {h: i for i, h in enumerate(headers)}
        for row in rows:
            game_date_raw = row[idx.get("GAME_DATE", 0)]
            try:
                gd = datetime.strptime(game_date_raw, "%b %d, %Y").strftime("%Y-%m-%d")
            except (ValueError, TypeError):
                continue
            if gd != date:
                continue
            if stat == "points":
                return float(row[idx["PTS"]])
            if stat == "rebounds":
                return float(row[idx["REB"]])
            if stat == "assists":
                return float(row[idx["AST"]])
            if stat == "pra":
                return float(row[idx["PTS"]]) + float(row[idx["REB"]]) + float(row[idx["AST"]])
    except Exception as e:
        logger.debug(f"NBA game stat fetch failed for {player_name}: {e}")
    return None


def _nhl_player_game_stat(player_name: str, stat: str, date: str) -> Optional[float]:
    """NHL shots on goal from NHL Stats API. Player search via league roster."""
    if stat != "shots":
        return None
    try:
        # Search via NHL API players endpoint
        r = requests.get(f"https://search.d3.nhle.com/api/v1/search/player",
                         params={"culture": "en-us", "q": player_name, "limit": 1, "active": "true"},
                         headers={"User-Agent": "TheEdge/1.0"}, timeout=10)
        if r.status_code != 200:
            return None
        matches = r.json() or []
        if not matches:
            return None
        pid = matches[0].get("playerId")
        if not pid:
            return None
        # Get game log
        season = int(date.split("-")[0])
        season_str = f"{season-1}{season}" if int(date.split("-")[1]) < 9 else f"{season}{season+1}"
        r = requests.get(f"https://api-web.nhle.com/v1/player/{pid}/game-log/{season_str}/2",
                         headers={"User-Agent": "TheEdge/1.0"}, timeout=10)
        if r.status_code != 200:
            return None
        for g in (r.json() or {}).get("gameLog", []):
            if g.get("gameDate") == date:
                return float(g.get("shots", 0))
    except Exception as e:
        logger.debug(f"NHL game stat fetch failed for {player_name}: {e}")
    return None


def _grade_prop(pick_data: dict) -> tuple[str, Optional[str]]:
    """Grade one player-prop pick. Returns (status, result_score).
    Returns PEND if we can't resolve the stat (game not final, player not
    found, API down) — the next grader run will retry."""
    parsed = _parse_prop(pick_data.get("pick", ""))
    if not parsed:
        return "PEND", None
    date = pick_data.get("date", "")
    if not date:
        return "PEND", None
    sport = parsed["sport"]
    if sport == "MLB":
        actual = _mlb_player_game_stat(parsed["player"], parsed["stat"], date)
    elif sport == "NBA":
        actual = _nba_player_game_stat(parsed["player"], parsed["stat"], date)
    elif sport == "NHL":
        actual = _nhl_player_game_stat(parsed["player"], parsed["stat"], date)
    else:
        return "PEND", None
    if actual is None:
        return "PEND", None
    line = parsed["line"]
    result_score = f"{parsed['player']}: {actual:g} {parsed['stat']}"
    if actual == line:
        return "PUSH", result_score
    if parsed["side"] == "over":
        return ("WIN" if actual > line else "LOSS"), result_score
    return ("WIN" if actual < line else "LOSS"), result_score


def _grade_single(pick_data: dict, scores: dict) -> tuple[str, Optional[str]]:
    """Grade a single (non-parlay) selection. pick_data needs 'game', 'pick',
    'market'. Returns (status, result_score)."""
    found = _find_score(scores, pick_data.get("game", ""))
    if not found or found.get("status_state") != "post":
        return "PEND", None

    market_raw = (pick_data.get("market") or "").upper()
    # Player props read player-level box scores, not team scores. The game
    # must be final (handled by the _find_score check above) and we look up
    # the actual stat in the relevant league API.
    if market_raw == "PROP":
        status, prop_score = _grade_prop(pick_data)
        return status, prop_score

    hs = found["home_score"]; as_ = found["away_score"]
    # Team-labeled score for human readability — "MIA 10 · TB 5"
    away_label = found.get("away_abbr") or found.get("away_team", "AWAY")
    home_label = found.get("home_abbr") or found.get("home_team", "HOME")
    result_score = f"{away_label} {as_} · {home_label} {hs}"
    market = market_raw
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
        from engine import night_recap
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
