"""MLB intel: probable pitchers, splits, bullpen, park factors, weather (lineups via MLB Stats API)."""
from __future__ import annotations

import os
from datetime import datetime, timedelta
from typing import Optional

import requests

from engine.utils import get_logger, retry, nyc_date
from engine.intel.types import (
    IntelPack, PitcherProfile, OffenseProfile, BullpenProfile, ParkProfile, WeatherProfile
)

logger = get_logger("intel-mlb")

# MLB Stats API (official, free, undocumented but stable)
MLB_BASE = "https://statsapi.mlb.com/api/v1"

# Park factors — Statcast-derived run/HR factors, 100 = neutral
# Source: Baseball Savant 3-yr rolling park factors. Updated annually.
PARK_FACTORS = {
    "Coors Field":          {"runs": 1.18, "hr": 1.14, "outdoor": True},
    "Great American Ball Park": {"runs": 1.06, "hr": 1.18, "outdoor": True},
    "Yankee Stadium":       {"runs": 1.05, "hr": 1.14, "outdoor": True},
    "Globe Life Field":     {"runs": 1.04, "hr": 1.05, "outdoor": False},
    "Fenway Park":          {"runs": 1.05, "hr": 0.96, "outdoor": True},
    "Citizens Bank Park":   {"runs": 1.03, "hr": 1.10, "outdoor": True},
    "Truist Park":          {"runs": 1.02, "hr": 1.04, "outdoor": True},
    "Wrigley Field":        {"runs": 1.02, "hr": 1.00, "outdoor": True},
    "Camden Yards":         {"runs": 1.01, "hr": 0.93, "outdoor": True},
    "Chase Field":          {"runs": 1.01, "hr": 1.07, "outdoor": False},
    "American Family Field":{"runs": 1.00, "hr": 1.06, "outdoor": False},
    "Citi Field":           {"runs": 0.97, "hr": 0.91, "outdoor": True},
    "Dodger Stadium":       {"runs": 0.97, "hr": 0.99, "outdoor": True},
    "Oriole Park at Camden Yards": {"runs": 1.01, "hr": 0.93, "outdoor": True},
    "Petco Park":           {"runs": 0.93, "hr": 0.91, "outdoor": True},
    "Oracle Park":          {"runs": 0.93, "hr": 0.85, "outdoor": True},
    "Tropicana Field":      {"runs": 0.95, "hr": 0.92, "outdoor": False},
    "Kauffman Stadium":     {"runs": 0.98, "hr": 0.92, "outdoor": True},
    "Comerica Park":        {"runs": 0.96, "hr": 0.95, "outdoor": True},
    "PNC Park":             {"runs": 0.95, "hr": 0.91, "outdoor": True},
    "T-Mobile Park":        {"runs": 0.93, "hr": 0.93, "outdoor": True},
    "Progressive Field":    {"runs": 0.97, "hr": 0.96, "outdoor": True},
    "Rogers Centre":        {"runs": 1.02, "hr": 1.04, "outdoor": False},
    "Minute Maid Park":     {"runs": 1.00, "hr": 1.04, "outdoor": False},
    "Angel Stadium":        {"runs": 0.99, "hr": 1.01, "outdoor": True},
    "Nationals Park":       {"runs": 1.01, "hr": 1.02, "outdoor": True},
    "loanDepot park":       {"runs": 0.94, "hr": 0.86, "outdoor": False},
    "Sutter Health Park":   {"runs": 1.02, "hr": 0.98, "outdoor": True},   # A's interim home
    "Target Field":         {"runs": 0.98, "hr": 0.97, "outdoor": True},
    "Guaranteed Rate Field":{"runs": 1.00, "hr": 1.08, "outdoor": True},
    "Busch Stadium":        {"runs": 0.97, "hr": 0.93, "outdoor": True},
}


# ─── MLB Stats API helpers ───────────────────────────────────────────────────

@retry(attempts=2, backoff=2)
def _mlb_schedule_with_probables(date_str: str) -> dict:
    """Get the day's schedule with probable pitchers hydrated."""
    url = f"{MLB_BASE}/schedule"
    params = {
        "sportId": 1,
        "date": date_str,
        "hydrate": "probablePitcher(note),lineups,weather,venue",
    }
    r = requests.get(url, params=params, timeout=15, headers={"User-Agent": "TheEdge/1.0"})
    r.raise_for_status()
    return r.json()


@retry(attempts=2, backoff=2)
def _mlb_person(pid: int) -> dict:
    r = requests.get(f"{MLB_BASE}/people/{pid}", timeout=10, headers={"User-Agent": "TheEdge/1.0"})
    r.raise_for_status()
    return r.json()


@retry(attempts=2, backoff=2)
def _mlb_person_stats(pid: int, season: int) -> dict:
    """Career + season splits."""
    url = f"{MLB_BASE}/people/{pid}/stats"
    params = {
        "stats": "season,lastXGames,statSplits",
        "group": "pitching",
        "season": season,
        "sitCodes": "vl,vr,h,a",
        "lastXGames": 3,
    }
    r = requests.get(url, params=params, timeout=10, headers={"User-Agent": "TheEdge/1.0"})
    r.raise_for_status()
    return r.json()


# ─── pybaseball: deep advanced metrics ───────────────────────────────────────

_PB_PITCHING_CACHE: Optional[list[dict]] = None
_PB_TEAM_BATTING_CACHE: Optional[list[dict]] = None


_PB_UA_PATCHED = False


def _setup_pybaseball_ua() -> None:
    """FanGraphs blocks default UA. Wrap requests.get + Session.get to inject browser UA."""
    global _PB_UA_PATCHED
    if _PB_UA_PATCHED:
        return
    try:
        import requests as _rq
        _browser_ua = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
        _orig_get = _rq.get
        _orig_session_get = _rq.Session.get

        def _patched_get(url, **kw):
            headers = dict(kw.pop("headers", None) or {})
            headers.setdefault("User-Agent", _browser_ua)
            return _orig_get(url, headers=headers, **kw)

        def _patched_session_get(self, url, **kw):
            headers = dict(kw.pop("headers", None) or {})
            headers.setdefault("User-Agent", _browser_ua)
            return _orig_session_get(self, url, headers=headers, **kw)

        _rq.get = _patched_get
        _rq.Session.get = _patched_session_get
        _PB_UA_PATCHED = True
    except Exception:
        pass


def _pybaseball_pitchers(season: int) -> list[dict]:
    global _PB_PITCHING_CACHE
    if _PB_PITCHING_CACHE is not None:
        return _PB_PITCHING_CACHE
    try:
        _setup_pybaseball_ua()
        import pybaseball
        pybaseball.cache.enable()
        df = pybaseball.pitching_stats(season, qual=20)
        if df is None or df.empty:
            _PB_PITCHING_CACHE = []
            return _PB_PITCHING_CACHE
        _PB_PITCHING_CACHE = df.to_dict("records")
        return _PB_PITCHING_CACHE
    except Exception as e:
        logger.warning(f"pybaseball pitching_stats failed: {e}")
        _PB_PITCHING_CACHE = []
        return _PB_PITCHING_CACHE


def _pybaseball_team_batting(season: int) -> list[dict]:
    global _PB_TEAM_BATTING_CACHE
    if _PB_TEAM_BATTING_CACHE is not None:
        return _PB_TEAM_BATTING_CACHE
    try:
        _setup_pybaseball_ua()
        import pybaseball
        pybaseball.cache.enable()
        df = pybaseball.team_batting(season)
        if df is None or df.empty:
            _PB_TEAM_BATTING_CACHE = []
            return _PB_TEAM_BATTING_CACHE
        _PB_TEAM_BATTING_CACHE = df.to_dict("records")
        return _PB_TEAM_BATTING_CACHE
    except Exception as e:
        logger.warning(f"pybaseball team_batting failed: {e}")
        _PB_TEAM_BATTING_CACHE = []
        return _PB_TEAM_BATTING_CACHE


def _find_pb_pitcher(rows: list[dict], name: str) -> Optional[dict]:
    nl = name.lower()
    for row in rows:
        n = str(row.get("Name", "")).lower()
        if n == nl or nl in n or n in nl:
            return row
    return None


def _find_pb_team(rows: list[dict], team: str) -> Optional[dict]:
    """Match by team name fragment. pybaseball uses abbreviations and full names varying by version."""
    tl = team.lower()
    for row in rows:
        candidates = [str(row.get("Team", "")), str(row.get("teamIDfg", "")), str(row.get("Tm", ""))]
        for c in candidates:
            cl = c.lower()
            if not cl:
                continue
            if cl == tl or cl in tl or tl in cl:
                return row
            # Match major tokens (e.g. "yankees" vs "new york yankees")
            tokens = set(tl.split())
            ctokens = set(cl.split())
            if tokens & ctokens and len(tokens & ctokens) >= min(2, len(tokens)):
                return row
    return None


# ─── Profile builders ────────────────────────────────────────────────────────

def _build_pitcher_profile(p_data: dict, throws: str, season: int) -> PitcherProfile:
    name = p_data.get("fullName") or p_data.get("name", "")
    profile = PitcherProfile(name=name, throws=throws)

    # MLB Stats API season splits (more current)
    try:
        stats = _mlb_person_stats(p_data["id"], season).get("stats", [])
        for s in stats:
            if s.get("group", {}).get("displayName") == "pitching" and s.get("type", {}).get("displayName") == "season":
                splits = s.get("splits", [])
                if splits:
                    st = splits[0].get("stat", {})
                    profile.season_era = _to_float(st.get("era"))
                    profile.season_whip = _to_float(st.get("whip"))
                    ip = _to_float(st.get("inningsPitched"))
                    profile.season_ip = ip
                    so = _to_int(st.get("strikeOuts"))
                    bb = _to_int(st.get("baseOnBalls"))
                    bf = _to_int(st.get("battersFaced"))
                    hr = _to_int(st.get("homeRuns"))
                    if bf:
                        profile.season_k_pct = round(so / bf * 100, 1) if so is not None else None
                        profile.season_bb_pct = round(bb / bf * 100, 1) if bb is not None else None
                    if ip and ip > 0 and hr is not None:
                        profile.season_hr9 = round(hr * 9 / ip, 2)
            if s.get("type", {}).get("displayName") == "lastXGames":
                splits = s.get("splits", [])
                if splits:
                    st = splits[0].get("stat", {})
                    profile.l3_era = _to_float(st.get("era"))
                    ip3 = _to_float(st.get("inningsPitched"))
                    if ip3:
                        profile.l3_ip_per_start = round(ip3 / 3, 2)
    except Exception as e:
        logger.debug(f"MLB person stats failed for {name}: {e}")

    # pybaseball — advanced (xFIP, SIERA, Stuff+)
    try:
        rows = _pybaseball_pitchers(season)
        pb = _find_pb_pitcher(rows, name)
        if pb:
            profile.season_fip = _to_float(pb.get("FIP"))
            profile.season_xfip = _to_float(pb.get("xFIP"))
            profile.season_siera = _to_float(pb.get("SIERA"))
            profile.season_stuff_plus = _to_float(pb.get("Stuff+") or pb.get("Stuff_plus"))
            # Override hr9 / k% if pybaseball values present
            if pb.get("HR/9") is not None:
                profile.season_hr9 = _to_float(pb.get("HR/9"))
            if pb.get("K%") is not None:
                profile.season_k_pct = _to_float(pb.get("K%"))
            if pb.get("BB%") is not None:
                profile.season_bb_pct = _to_float(pb.get("BB%"))
    except Exception as e:
        logger.debug(f"pybaseball lookup failed for {name}: {e}")

    # Trend: compare L3 ERA vs season ERA
    if profile.l3_era is not None and profile.season_era is not None:
        delta = profile.l3_era - profile.season_era
        if delta < -0.6:
            profile.trend = "improving"
        elif delta > 0.6:
            profile.trend = "regressing"
        else:
            profile.trend = "stable"

    return profile


def _build_offense_profile(team_name: str, opposing_throws: str, season: int) -> OffenseProfile:
    profile = OffenseProfile()
    try:
        rows = _pybaseball_team_batting(season)
        team_row = _find_pb_team(rows, team_name)
        if team_row:
            profile.wrc_plus_season = _to_float(team_row.get("wRC+"))
            profile.runs_per_game_season = _to_float(team_row.get("R/G") or team_row.get("R") and (team_row["R"] / max(team_row.get("G", 1), 1)))
    except Exception as e:
        logger.debug(f"team_batting lookup failed for {team_name}: {e}")
    # vs LHP / RHP requires split fetch which is heavier — leave None for v1, Claude can flag missing
    return profile


def _build_park_profile(venue: str) -> ParkProfile:
    pf = PARK_FACTORS.get(venue) or PARK_FACTORS.get(venue.strip(), None)
    if not pf:
        # try fuzzy
        v_lower = venue.lower()
        for k, val in PARK_FACTORS.items():
            if k.lower() in v_lower or v_lower in k.lower():
                pf = val
                venue = k
                break
    if pf:
        notes = []
        if pf["runs"] >= 1.05:
            notes.append("hitter-friendly")
        elif pf["runs"] <= 0.95:
            notes.append("pitcher-friendly")
        if pf["hr"] >= 1.08:
            notes.append("HR-boosting")
        elif pf["hr"] <= 0.93:
            notes.append("HR-suppressing")
        return ParkProfile(
            name=venue, outdoor=pf["outdoor"],
            pf_runs=pf["runs"], pf_hr=pf["hr"],
            notes=", ".join(notes) if notes else "neutral",
        )
    return ParkProfile(name=venue, outdoor=True, notes="unknown — assumed neutral")


def _build_weather_profile(weather_data: dict, park: Optional[ParkProfile]) -> Optional[WeatherProfile]:
    if not weather_data or (park and not park.outdoor):
        return None
    try:
        temp = _to_float(str(weather_data.get("temp", "")).split()[0]) if weather_data.get("temp") else None
        wind_field = weather_data.get("wind", "")
        # "8 mph, Out To Center Field"
        wind_mph = None
        wind_dir = None
        if wind_field:
            parts = [p.strip() for p in str(wind_field).split(",")]
            if parts:
                speed_match = parts[0].split()
                if speed_match and speed_match[0].replace(".", "", 1).isdigit():
                    wind_mph = float(speed_match[0])
                if len(parts) > 1:
                    wind_dir = parts[1].lower().replace(" ", "_")
        conditions = weather_data.get("condition", "")
        # Quick HR-impact heuristic: wind out + warm = +; wind in + cold = -
        hr_impact = 0.0
        notes_bits = []
        if wind_mph and wind_dir:
            if "out" in wind_dir and wind_mph >= 8:
                hr_impact += min(0.20, wind_mph * 0.015)
                notes_bits.append(f"wind {wind_mph:.0f}mph blowing out boosts HR ~{int(hr_impact*100)}%")
            elif "in" in wind_dir and wind_mph >= 8:
                hr_impact -= min(0.18, wind_mph * 0.013)
                notes_bits.append(f"wind {wind_mph:.0f}mph blowing in suppresses HR")
        if temp is not None:
            if temp >= 85:
                hr_impact += 0.04
                notes_bits.append("hot air carries")
            elif temp <= 50:
                hr_impact -= 0.05
                notes_bits.append("cold air dampens")
        return WeatherProfile(
            temp_f=temp, wind_mph=wind_mph, wind_dir=wind_dir,
            hr_impact_pct=round(hr_impact, 3),
            notes="; ".join(notes_bits) if notes_bits else conditions or "neutral",
        )
    except Exception as e:
        logger.debug(f"weather parse failed: {e}")
        return None


def _to_float(v) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (ValueError, TypeError):
        return None


def _to_int(v) -> Optional[int]:
    if v is None or v == "":
        return None
    try:
        return int(v)
    except (ValueError, TypeError):
        return None


# ─── Main attach function ────────────────────────────────────────────────────

def attach_mlb_intel(packs: list[IntelPack], date_str: Optional[str] = None) -> None:
    date_str = date_str or nyc_date()
    mlb_packs = [p for p in packs if p.sport == "MLB"]
    if not mlb_packs:
        return

    try:
        schedule = _mlb_schedule_with_probables(date_str)
    except Exception as e:
        logger.warning(f"MLB schedule fetch failed: {e}")
        return

    season = int(date_str[:4])
    pre_load_pb = False

    games = []
    for d in schedule.get("dates", []):
        games.extend(d.get("games", []))

    for g in games:
        try:
            home_name = g.get("teams", {}).get("home", {}).get("team", {}).get("name", "")
            away_name = g.get("teams", {}).get("away", {}).get("team", {}).get("name", "")
            venue = g.get("venue", {}).get("name", "")
            # Match to our pack by team names
            pack = _match_mlb_pack(mlb_packs, home_name, away_name)
            if not pack:
                continue

            # Park
            pack.park = _build_park_profile(venue or pack.venue)

            # Pitchers
            home_p = g.get("teams", {}).get("home", {}).get("probablePitcher")
            away_p = g.get("teams", {}).get("away", {}).get("probablePitcher")
            if home_p:
                try:
                    detail = _mlb_person(home_p["id"])
                    throws = detail.get("people", [{}])[0].get("pitchHand", {}).get("code", "")
                    pack.home_pitcher = _build_pitcher_profile({"id": home_p["id"], "fullName": home_p.get("fullName")}, throws, season)
                    pre_load_pb = True
                except Exception as e:
                    logger.debug(f"home pitcher build failed: {e}")
            if away_p:
                try:
                    detail = _mlb_person(away_p["id"])
                    throws = detail.get("people", [{}])[0].get("pitchHand", {}).get("code", "")
                    pack.away_pitcher = _build_pitcher_profile({"id": away_p["id"], "fullName": away_p.get("fullName")}, throws, season)
                    pre_load_pb = True
                except Exception as e:
                    logger.debug(f"away pitcher build failed: {e}")

            # Offense (team-level)
            pack.home_offense = _build_offense_profile(pack.home_team, pack.away_pitcher.throws if pack.away_pitcher else "", season)
            pack.away_offense = _build_offense_profile(pack.away_team, pack.home_pitcher.throws if pack.home_pitcher else "", season)

            # Weather (only outdoor)
            pack.weather = _build_weather_profile(g.get("weather", {}), pack.park)

            # Confidence on this pack — bump based on how much we got
            confidence_bits = 0.3
            if pack.home_pitcher and pack.home_pitcher.season_xfip is not None:
                confidence_bits += 0.15
            if pack.away_pitcher and pack.away_pitcher.season_xfip is not None:
                confidence_bits += 0.15
            if pack.park and pack.park.pf_runs is not None:
                confidence_bits += 0.10
            if pack.weather:
                confidence_bits += 0.05
            pack.confidence_data = min(1.0, confidence_bits)

        except Exception as e:
            logger.warning(f"MLB intel build failed for game: {e}")


def _match_mlb_pack(packs: list[IntelPack], home: str, away: str) -> Optional[IntelPack]:
    hl = home.lower()
    al = away.lower()
    for p in packs:
        ph = p.home_team.lower()
        pa = p.away_team.lower()
        if (hl in ph or ph in hl) and (al in pa or pa in al):
            return p
    # token fallback
    for p in packs:
        h_tokens = set(p.home_team.lower().split())
        a_tokens = set(p.away_team.lower().split())
        if (set(hl.split()) & h_tokens) and (set(al.split()) & a_tokens):
            return p
    return None
