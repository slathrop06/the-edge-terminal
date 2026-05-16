"""ESPN scoreboard → today's schedule across all sports."""
from __future__ import annotations

from typing import Optional

import requests

from engine.utils import get_logger, retry, nyc_date
from engine.intel.types import IntelPack, SportCode

logger = get_logger("intel-schedule")

ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports"
SPORT_PATHS: dict[SportCode, str] = {
    "MLB": "baseball/mlb",
    "NBA": "basketball/nba",
    "NHL": "hockey/nhl",
    "NFL": "football/nfl",
    "CFB": "football/college-football",
}


@retry(attempts=3, backoff=2)
def fetch_scoreboard(sport: SportCode, date_str: str) -> dict:
    path = SPORT_PATHS.get(sport)
    if not path:
        return {}
    url = f"{ESPN_BASE}/{path}/scoreboard?dates={date_str.replace('-', '')}"
    r = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
    r.raise_for_status()
    return r.json()


def schedule_to_packs(sport: SportCode, espn_data: dict) -> list[IntelPack]:
    packs: list[IntelPack] = []
    for ev in espn_data.get("events", []):
        try:
            comp = ev["competitions"][0]
            home = next(c for c in comp["competitors"] if c["homeAway"] == "home")
            away = next(c for c in comp["competitors"] if c["homeAway"] == "away")
            venue = comp.get("venue", {}).get("fullName", "")
            start = ev.get("date", "")
            home_name = home["team"].get("displayName") or home["team"].get("name") or home["team"].get("abbreviation", "")
            away_name = away["team"].get("displayName") or away["team"].get("name") or away["team"].get("abbreviation", "")
            home_abbr = home["team"].get("abbreviation", "")
            away_abbr = away["team"].get("abbreviation", "")
            packs.append(IntelPack(
                game_id=f"{sport}-{ev['id']}",
                sport=sport,
                home_team=home_name,
                away_team=away_name,
                home_abbr=home_abbr,
                away_abbr=away_abbr,
                venue=venue,
                first_pitch_iso=start,
            ))
        except (KeyError, StopIteration) as e:
            logger.warning(f"ESPN parse skipped: {e}")
    return packs


def fetch_finals(sport: SportCode, date_str: str) -> dict[str, dict]:
    """Returns {espn_event_id: {home_score, away_score, status_state, home_team, away_team}}."""
    data = fetch_scoreboard(sport, date_str)
    out: dict[str, dict] = {}
    for ev in data.get("events", []):
        try:
            eid = ev["id"]
            comp = ev["competitions"][0]
            home = next(c for c in comp["competitors"] if c["homeAway"] == "home")
            away = next(c for c in comp["competitors"] if c["homeAway"] == "away")
            state = ev.get("status", {}).get("type", {}).get("state", "")
            out[f"{sport}-{eid}"] = {
                "home_score": _safe_int(home.get("score", 0)),
                "away_score": _safe_int(away.get("score", 0)),
                "status_state": state,
                "home_team": home["team"].get("displayName") or home["team"].get("abbreviation", ""),
                "away_team": away["team"].get("displayName") or away["team"].get("abbreviation", ""),
            }
        except (KeyError, StopIteration):
            continue
    return out


def _safe_int(v) -> int:
    try:
        return int(v)
    except (ValueError, TypeError):
        return 0
