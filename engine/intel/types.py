"""Shared Pydantic types for the intel pipeline."""
from __future__ import annotations

from typing import Optional, Literal, Any
from pydantic import BaseModel, Field


SportCode = Literal["MLB", "NBA", "NHL", "NFL", "CFB"]


class BookOdds(BaseModel):
    book: str
    market: str               # "h2h" | "spreads" | "totals"
    selection: str            # team name or "over"/"under"
    line: Optional[float] = None    # spread or total value
    price_american: int       # American odds
    link: Optional[str] = None      # deep link: pre-populates bet slip on book


class MarketIntel(BaseModel):
    # Best price among enabled books (DK/FD/MGM)
    home_ml_best: Optional[BookOdds] = None
    away_ml_best: Optional[BookOdds] = None
    home_spread_best: Optional[BookOdds] = None
    away_spread_best: Optional[BookOdds] = None
    over_best: Optional[BookOdds] = None
    under_best: Optional[BookOdds] = None

    # Per-book breakdown so we can render DK/FD/MGM side-by-side
    # Each dict key is a normalized book key: "draftkings", "fanduel", "betmgm"
    home_ml_by_book: dict[str, BookOdds] = Field(default_factory=dict)
    away_ml_by_book: dict[str, BookOdds] = Field(default_factory=dict)
    home_spread_by_book: dict[str, BookOdds] = Field(default_factory=dict)
    away_spread_by_book: dict[str, BookOdds] = Field(default_factory=dict)
    over_by_book: dict[str, BookOdds] = Field(default_factory=dict)
    under_by_book: dict[str, BookOdds] = Field(default_factory=dict)

    consensus_total: Optional[float] = None
    consensus_home_spread: Optional[float] = None
    home_ml_implied_pct: Optional[float] = None    # de-vigged probability across enabled books
    away_ml_implied_pct: Optional[float] = None

    # Movement vs opening line (if we have history; null on first snapshot)
    total_open: Optional[float] = None
    home_spread_open: Optional[float] = None
    home_ml_open_pct: Optional[float] = None
    reverse_line_movement: Optional[str] = None    # human description, e.g. "Total dropped 8.5→8 despite 67% over"

    # Event-page deep links per book (opens the game on that book)
    event_link_by_book: dict[str, str] = Field(default_factory=dict)

    book_count: int = 0
    snapshot_iso: str = ""


class PitcherProfile(BaseModel):
    name: str = ""
    throws: str = ""  # "L" or "R"
    season_era: Optional[float] = None
    season_fip: Optional[float] = None
    season_xfip: Optional[float] = None
    season_siera: Optional[float] = None
    season_whip: Optional[float] = None
    season_k_pct: Optional[float] = None
    season_bb_pct: Optional[float] = None
    season_hr9: Optional[float] = None
    season_stuff_plus: Optional[float] = None
    season_ip: Optional[float] = None
    l3_era: Optional[float] = None
    l3_xfip: Optional[float] = None
    l3_ip_per_start: Optional[float] = None
    trend: Optional[str] = None   # "improving" | "stable" | "regressing"


class OffenseProfile(BaseModel):
    wrc_plus_season: Optional[float] = None
    wrc_plus_vs_lhp: Optional[float] = None
    wrc_plus_vs_rhp: Optional[float] = None
    wrc_plus_l14: Optional[float] = None
    runs_per_game_season: Optional[float] = None
    runs_per_game_l10: Optional[float] = None
    k_pct_vs_lhp: Optional[float] = None
    k_pct_vs_rhp: Optional[float] = None


class BullpenProfile(BaseModel):
    season_fip: Optional[float] = None
    l14_fip: Optional[float] = None
    closer_avail: Optional[str] = None       # "rested" | "back_to_back" | "unavailable"
    high_lev_arms_avail: Optional[int] = None


class ParkProfile(BaseModel):
    name: str = ""
    outdoor: bool = False
    pf_runs: Optional[float] = None
    pf_hr: Optional[float] = None
    notes: Optional[str] = None


class WeatherProfile(BaseModel):
    temp_f: Optional[float] = None
    wind_mph: Optional[float] = None
    wind_dir: Optional[str] = None           # "out_to_LF" | "in_from_RF" etc
    precip_pct: Optional[float] = None
    hr_impact_pct: Optional[float] = None     # +/- vs neutral
    notes: Optional[str] = None


class TeamRatingsNBA(BaseModel):
    net_rating: Optional[float] = None
    off_rating: Optional[float] = None
    def_rating: Optional[float] = None
    efg_pct: Optional[float] = None
    ts_pct: Optional[float] = None
    pace: Optional[float] = None
    rest_days: Optional[int] = None
    back_to_back: Optional[bool] = None
    last10_record: Optional[str] = None
    last10_net_rating: Optional[float] = None
    key_injuries: list[str] = Field(default_factory=list)


class TeamRatingsNHL(BaseModel):
    xgf_pct: Optional[float] = None
    corsi_for_pct: Optional[float] = None
    pdo: Optional[float] = None
    pp_pct: Optional[float] = None
    pk_pct: Optional[float] = None
    goalie_name: Optional[str] = None
    goalie_sv_pct: Optional[float] = None
    goalie_gsax: Optional[float] = None


class TeamRatingsFootball(BaseModel):
    season_record: Optional[str] = None
    points_per_game: Optional[float] = None
    points_allowed_per_game: Optional[float] = None
    yards_per_play: Optional[float] = None
    epa_per_play: Optional[float] = None
    third_down_pct: Optional[float] = None
    turnover_diff: Optional[int] = None
    pace_seconds_per_play: Optional[float] = None
    rest_days: Optional[int] = None
    key_injuries: list[str] = Field(default_factory=list)


class PlayerProp(BaseModel):
    """One player's prop line, with per-book pricing + deep links."""
    player_name: str
    team: str = ""                              # "home" or "away" if known
    market: str                                 # "batter_home_runs", "batter_hits", etc.
    line: float                                 # e.g., 0.5 for HR props (Over 0.5 = ≥1 HR)
    over_best: Optional[BookOdds] = None       # best (highest) over price across books
    under_best: Optional[BookOdds] = None      # best (lowest negative) under price
    over_by_book: dict[str, BookOdds] = Field(default_factory=dict)
    under_by_book: dict[str, BookOdds] = Field(default_factory=dict)


class PropMarket(BaseModel):
    """All player props for a single game, grouped by market type.
    Each list contains PlayerProp objects with per-book over/under pricing.
    Markets that aren't fetched (e.g., NBA props on an MLB game) stay
    empty — exclude_none in serialization will keep noise out of the slate."""
    # MLB
    hr_props: list[PlayerProp] = Field(default_factory=list)
    k_props: list[PlayerProp] = Field(default_factory=list)           # pitcher_strikeouts
    tb_props: list[PlayerProp] = Field(default_factory=list)          # batter_total_bases
    hits_props: list[PlayerProp] = Field(default_factory=list)        # batter_hits
    # NBA
    points_props: list[PlayerProp] = Field(default_factory=list)       # player_points
    rebounds_props: list[PlayerProp] = Field(default_factory=list)     # player_rebounds
    assists_props: list[PlayerProp] = Field(default_factory=list)      # player_assists
    pra_props: list[PlayerProp] = Field(default_factory=list)          # player_points_rebounds_assists
    # NHL
    shots_props: list[PlayerProp] = Field(default_factory=list)        # player_shots_on_goal


class IntelPack(BaseModel):
    """Complete intel package the handicapper sees for one game."""
    game_id: str
    sport: SportCode
    home_team: str
    away_team: str
    home_abbr: str = ""
    away_abbr: str = ""
    venue: str = ""
    first_pitch_iso: str

    # MLB-specific
    home_pitcher: Optional[PitcherProfile] = None
    away_pitcher: Optional[PitcherProfile] = None
    home_offense: Optional[OffenseProfile] = None
    away_offense: Optional[OffenseProfile] = None
    home_bullpen: Optional[BullpenProfile] = None
    away_bullpen: Optional[BullpenProfile] = None
    park: Optional[ParkProfile] = None
    weather: Optional[WeatherProfile] = None
    umpire_note: Optional[str] = None

    # NBA
    home_nba: Optional[TeamRatingsNBA] = None
    away_nba: Optional[TeamRatingsNBA] = None

    # NHL
    home_nhl: Optional[TeamRatingsNHL] = None
    away_nhl: Optional[TeamRatingsNHL] = None

    # NFL / CFB
    home_football: Optional[TeamRatingsFootball] = None
    away_football: Optional[TeamRatingsFootball] = None

    # Market + news (cross-sport)
    market: Optional[MarketIntel] = None
    props: Optional[PropMarket] = None
    # The Odds API event id for this game (set during attach_market_intel) —
    # required to fetch per-event prop markets in a follow-up call.
    odds_api_event_id: Optional[str] = None
    news_headlines: list[str] = Field(default_factory=list)

    # Pre-computed signals — short tags the handicapper can latch onto
    signals: list[str] = Field(default_factory=list)
    confidence_data: float = 0.5    # how trustworthy is this pack as a whole (0-1)

    notes: list[str] = Field(default_factory=list)
