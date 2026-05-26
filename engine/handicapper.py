"""Handicapper: feeds IntelPacks to Claude with web_search tool, parses deep-analysis output."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Optional, Any

import anthropic
from pydantic import BaseModel, Field, field_validator

from engine.intel.types import IntelPack
from engine.utils import (
    get_logger, nyc_now, nyc_date, estimate_cost, record_api_cost,
    load_daily_cost, PROJECT_ROOT
)

logger = get_logger("handicapper")

SYSTEM_PROMPT_PATH = PROJECT_ROOT / "prompts" / "handicapper-system.md"
LATE_ADD_PROMPT_PATH = PROJECT_ROOT / "prompts" / "late-add-system.md"
GOLF_MAJOR_PROMPT_PATH = PROJECT_ROOT / "prompts" / "golf-major-system.md"


def _slim_book_odds(odds: dict) -> dict:
    """Strip per-book deep-link URLs (long, useless to Claude) and drop the
    "selection" / "market" string (redundant with the prop's market key)."""
    if not odds:
        return odds
    return {"book": odds.get("book"), "price_american": odds.get("price_american"),
            "line": odds.get("line")}


def _slim_player_prop(prop: dict) -> dict:
    """One PlayerProp → slate-bound form. Claude needs the player, line,
    and best over/under price+book. Per-book breakdowns + deep links stay
    on the IntelPack for the publisher's link-attach step."""
    return {
        "player_name": prop.get("player_name"),
        "market":      prop.get("market"),
        "line":        prop.get("line"),
        "over_best":   _slim_book_odds(prop.get("over_best") or {}),
        "under_best":  _slim_book_odds(prop.get("under_best") or {}),
    }


def _pack_for_slate(pack) -> dict:
    """IntelPack → slate-bound dict. Identical to model_dump(exclude_none=True)
    except props are slimmed (no by_book dicts, no per-outcome links).
    Without this slim path a 6-MLB-game slate with 4 prop markets each
    produced a 545k-char JSON that exceeded the 180k Claude budget."""
    d = pack.model_dump(exclude_none=True)
    props = d.get("props")
    if not props:
        return d
    for prop_field, prop_list in list(props.items()):
        if not isinstance(prop_list, list):
            continue
        props[prop_field] = [_slim_player_prop(p) for p in prop_list]
    return d


class DataPoint(BaseModel):
    label: str
    value: str
    context: str = ""


class ParlayLeg(BaseModel):
    game: str
    pick: str
    best_book: str = ""
    best_odds: str = ""


class Pick(BaseModel):
    id: str
    sport: str
    game: str
    first_pitch_iso: str = ""
    pick: str
    best_book: str = ""
    best_odds: str
    # Per-book prices for the SAME pick across DK / FD / MGM, e.g.
    # {"draftkings": "-110", "fanduel": "-105", "betmgm": "-108"}
    book_prices: dict[str, str] = Field(default_factory=dict)
    # Per-book deep links that pre-populate the bet slip on that book.
    # Filled by the publisher (not Claude) by matching pick.pick → outcome.
    book_links: dict[str, str] = Field(default_factory=dict)
    market: str = ""
    confidence: int
    units: float
    # Scott Bot's model-estimated true probability that this pick wins (0.0-1.0).
    # The basis of the edge claim: win_probability > de-vigged market implied probability.
    win_probability: Optional[float] = None
    ladder_designation: bool = False
    data_confidence: float = 0.7
    rules_passed: list[str] = Field(default_factory=list)
    # Lifecycle / lock state — set by publisher
    locked: bool = True
    late_add: bool = False
    late_add_reason: Optional[str] = ""
    # Bonus pick (off-cadence — e.g. golf majors, future big-event picks).
    # Does NOT count against the daily 3-pick cap. Lives in its own analytics track.
    bonus_pick: bool = False
    event_type: Optional[str] = ""    # "golf_major" | "" (regular)
    event_name: Optional[str] = ""    # "PGA Championship" | "U.S. Open" | ""
    # Parlay support — when market=="PARLAY", legs is non-empty
    legs: list[ParlayLeg] = Field(default_factory=list)
    headline: str = ""
    the_thesis: str = ""
    the_data: list[DataPoint] = Field(default_factory=list)
    the_market: Optional[str] = ""
    weather_park: Optional[str] = ""
    case_against: Optional[str] = ""
    what_were_betting_on: Optional[str] = ""
    scott_bot_quip: Optional[str] = ""
    ladder_note: Optional[str] = ""

    @field_validator("the_market", "weather_park", "case_against",
                     "what_were_betting_on", "scott_bot_quip", "ladder_note",
                     mode="before")
    @classmethod
    def coerce_none_to_empty(cls, v):
        return v if v is not None else ""

    @field_validator("confidence")
    @classmethod
    def conf_range(cls, v: int) -> int:
        if not 1 <= v <= 5:
            raise ValueError(f"confidence {v} out of range")
        return v

    @field_validator("data_confidence")
    @classmethod
    def dc_range(cls, v: float) -> float:
        if not 0.0 <= v <= 1.0:
            raise ValueError(f"data_confidence {v} out of range")
        return v


class HandicapperResponse(BaseModel):
    slate_assessment: str = ""   # 1-2 sentence overview; optional for non-daily flows
    executive_summary: str = ""  # 60-100 word top-of-page TL;DR
    slate_analysis: str = ""     # multi-paragraph "show the work" view of the slate
    slate_vibe: str
    picks: list[Pick] = Field(default_factory=list)

    @field_validator("slate_vibe")
    @classmethod
    def vibe(cls, v: str) -> str:
        allowed = {"HOT", "NORMAL", "SOFT", "SKIP"}
        v = v.upper()
        if v not in allowed:
            raise ValueError(f"slate_vibe must be in {allowed}")
        return v


# ─── Cost-cap guard ──────────────────────────────────────────────────────────

def _check_cost_cap(daily_cap_usd: float) -> None:
    today_cost = load_daily_cost()
    if today_cost >= daily_cap_usd:
        raise RuntimeError(
            f"Daily API cost cap ${daily_cap_usd:.2f} reached "
            f"(current ${today_cost:.2f}). Halting."
        )


# ─── Main handicapper ────────────────────────────────────────────────────────

def run_handicapper(
    packs: list[IntelPack],
    config: Optional[dict] = None,
) -> HandicapperResponse:
    config = config or {}
    primary_model = config.get("primary_model", "claude-opus-4-7")
    fallback_model = config.get("fallback_model", "claude-sonnet-4-6")
    temperature = float(config.get("temperature", 0.3))
    max_tokens = int(config.get("max_tokens", 8000))
    use_web_search = bool(config.get("use_web_search", True))
    daily_cap = float(config.get("daily_cap_usd", 8.0))

    _check_cost_cap(daily_cap)

    if not SYSTEM_PROMPT_PATH.exists():
        raise FileNotFoundError(f"System prompt missing: {SYSTEM_PROMPT_PATH}")
    system_prompt = SYSTEM_PROMPT_PATH.read_text()

    # Serialize packs as JSON the model can reason over. Props get slimmed:
    # Claude only needs best over/under price+book per line, not the full
    # per-book breakdown or deep-link URLs (those stay on the pack object
    # for the publisher to attach to picks downstream).
    slate = {
        "date": nyc_date(),
        "now_iso": nyc_now().isoformat(),
        "games": [_pack_for_slate(p) for p in packs],
    }
    slate_json = json.dumps(slate, default=str, separators=(",", ":"))
    if len(slate_json) > 180_000:
        logger.warning(f"Slate JSON huge ({len(slate_json)} chars) — truncating to 180k")
        slate_json = slate_json[:180_000] + "...}"

    user_msg = (
        f"Today's slate ({slate['date']}). Review the IntelPacks, optionally use web_search "
        f"for late-breaking news, then return strict JSON per the system prompt.\n\n"
        f"```json\n{slate_json}\n```\n\n"
        f"**CRITICAL OUTPUT RULES:**\n"
        f"- Your response MUST be valid JSON ONLY. No markdown, no headers, no thinking-out-loud preamble.\n"
        f"- Begin your response with the character `{{` and end with `}}`. Nothing before or after.\n"
        f"- Do reasoning internally (and via web_search tool calls); deliver only the JSON object as the final text response.\n"
        f"- If you'd normally write 'Let me check...' or '**Top candidates:**', stop — that goes in your private reasoning, not the output."
    )

    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY", ""))
    tools = [{"type": "web_search_20250305", "name": "web_search", "max_uses": 6}] if use_web_search else []

    msg = None
    used_model = primary_model
    try:
        msg = _call_with_tools(client, primary_model, system_prompt, user_msg, tools, max_tokens, temperature)
    except anthropic.RateLimitError:
        logger.warning(f"{primary_model} rate-limited → falling back to {fallback_model}")
        used_model = fallback_model
        msg = _call_with_tools(client, fallback_model, system_prompt, user_msg, tools, max_tokens, temperature)
    except anthropic.APIStatusError as e:
        if e.status_code in (404, 400):
            logger.warning(f"{primary_model} not available ({e.status_code}) → falling back to {fallback_model}")
            used_model = fallback_model
            msg = _call_with_tools(client, fallback_model, system_prompt, user_msg, tools, max_tokens, temperature)
        else:
            raise

    if msg and msg.usage:
        cost = estimate_cost(used_model, msg.usage.input_tokens, msg.usage.output_tokens)
        total = record_api_cost(cost)
        logger.info(
            f"Tokens in={msg.usage.input_tokens} out={msg.usage.output_tokens} "
            f"cost=${cost:.4f} day total=${total:.4f}"
        )

    final_text = _extract_text(msg)
    json_str = _extract_json(final_text)
    if not json_str:
        raise ValueError(f"No JSON found in handicapper output (first 400 chars): {final_text[:400]}")
    parsed = _robust_json_loads(json_str, final_text)

    response = HandicapperResponse.model_validate(parsed)
    logger.info(f"vibe={response.slate_vibe}, {len(response.picks)} picks")
    for p in response.picks:
        logger.info(f"  • {p.pick} ({p.game}) conf={p.confidence} u={p.units} ladder={p.ladder_designation}")
    return response


def run_late_add(
    packs: list[IntelPack],
    existing_picks: list[dict],
    config: Optional[dict] = None,
) -> HandicapperResponse:
    """Late-afternoon edge check. Uses a focused prompt and Sonnet (cheaper).
    Returns 0-1 picks marked as late_add. Existing locked picks are listed in
    the user message so Claude knows what's already locked in."""
    config = config or {}
    model = config.get("late_add_model", "claude-sonnet-4-6")
    daily_cap = float(config.get("daily_cap_usd", 8.0))
    _check_cost_cap(daily_cap)

    if not LATE_ADD_PROMPT_PATH.exists():
        raise FileNotFoundError(f"Late-add prompt missing: {LATE_ADD_PROMPT_PATH}")
    system_prompt = LATE_ADD_PROMPT_PATH.read_text()

    summary_existing = [
        {"game": p.get("game"), "pick": p.get("pick"), "odds": p.get("best_odds"),
         "confidence": p.get("confidence"), "ladder": p.get("ladder_designation")}
        for p in existing_picks
    ]
    slate = {
        "date": nyc_date(),
        "now_iso": nyc_now().isoformat(),
        "locked_picks_already_published": summary_existing,
        "games": [_pack_for_slate(p) for p in packs],
    }
    slate_json = json.dumps(slate, default=str, separators=(",", ":"))
    if len(slate_json) > 180_000:
        slate_json = slate_json[:180_000] + "...}"

    user_msg = (
        f"Late-edge check ({slate['date']}). Locked picks already published this morning "
        f"are listed above. Review the IntelPacks for material new info since 6 AM and "
        f"decide if ONE late pick is warranted. Default to zero. Strict JSON.\n\n"
        f"```json\n{slate_json}\n```"
    )

    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY", ""))
    tools = [{"type": "web_search_20250305", "name": "web_search", "max_uses": 4}]

    msg = _call_with_tools(client, model, system_prompt, user_msg, tools,
                           max_tokens=3500, temperature=0.3)

    if msg and msg.usage:
        cost = estimate_cost(model, msg.usage.input_tokens, msg.usage.output_tokens)
        total = record_api_cost(cost)
        logger.info(f"Late-add tokens in={msg.usage.input_tokens} out={msg.usage.output_tokens} "
                    f"cost=${cost:.4f} day total=${total:.4f}")

    final_text = _extract_text(msg)
    json_str = _extract_json(final_text)
    if not json_str:
        raise ValueError(f"No JSON in late-add output: {final_text[:300]}")
    parsed = _robust_json_loads(json_str, final_text)
    response = HandicapperResponse.model_validate(parsed)
    # Force-mark all picks as late_add for downstream wiring
    for p in response.picks:
        p.late_add = True
        p.ladder_designation = False  # ladder is morning-only
    logger.info(f"Late-add: vibe={response.slate_vibe}, {len(response.picks)} picks proposed")
    return response


def run_golf_major(
    golf_pack: dict,
    config: Optional[dict] = None,
) -> HandicapperResponse:
    """One-shot bonus pick for an active golf major. Uses Opus 4.7 + web_search
    so it can verify the live leaderboard and tournament-specific context."""
    config = config or {}
    primary_model = config.get("primary_model", "claude-opus-4-7")
    fallback_model = config.get("fallback_model", "claude-sonnet-4-6")
    temperature = float(config.get("temperature", 0.3))
    max_tokens = int(config.get("max_tokens", 6000))
    daily_cap = float(config.get("daily_cap_usd", 8.0))
    _check_cost_cap(daily_cap)

    if not GOLF_MAJOR_PROMPT_PATH.exists():
        raise FileNotFoundError(f"Golf prompt missing: {GOLF_MAJOR_PROMPT_PATH}")
    system_prompt = GOLF_MAJOR_PROMPT_PATH.read_text()

    # Trim player list to top-50 by best odds — keep token cost reasonable
    trimmed_players = (golf_pack.get("players") or [])[:50]
    pack_for_model = {
        "tournament_name": golf_pack.get("tournament_name"),
        "event_id": golf_pack.get("event_id"),
        "sport_key": golf_pack.get("sport_key"),
        "commence_time": golf_pack.get("commence_time"),
        "snapshot_iso": golf_pack.get("snapshot_iso"),
        "players_top_50": trimmed_players,
        "today_iso": nyc_now().isoformat(),
        "today_date": nyc_date(),
    }
    pack_json = json.dumps(pack_for_model, default=str, separators=(",", ":"))

    user_msg = (
        f"Golf major bonus pick — {golf_pack.get('tournament_name')}.\n\n"
        f"Use web_search to verify the current leaderboard, weather, and any late news. "
        f"Then return strict JSON per the system prompt. Today is {nyc_date()}.\n\n"
        f"```json\n{pack_json}\n```"
    )

    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY", ""))
    tools = [{"type": "web_search_20250305", "name": "web_search", "max_uses": 8}]

    used_model = primary_model
    try:
        msg = _call_with_tools(client, primary_model, system_prompt, user_msg, tools, max_tokens, temperature)
    except (anthropic.RateLimitError, anthropic.APIStatusError) as e:
        logger.warning(f"{primary_model} failed ({e}); fallback to {fallback_model}")
        used_model = fallback_model
        msg = _call_with_tools(client, fallback_model, system_prompt, user_msg, tools, max_tokens, temperature)

    if msg and msg.usage:
        cost = estimate_cost(used_model, msg.usage.input_tokens, msg.usage.output_tokens)
        total = record_api_cost(cost)
        logger.info(
            f"Golf major tokens in={msg.usage.input_tokens} out={msg.usage.output_tokens} "
            f"cost=${cost:.4f} day total=${total:.4f}"
        )

    final_text = _extract_text(msg)
    json_str = _extract_json(final_text)
    if not json_str:
        raise ValueError(f"No JSON in golf-major output: {final_text[:400]}")
    parsed = _robust_json_loads(json_str, final_text)
    response = HandicapperResponse.model_validate(parsed)
    # Force-mark all picks as bonus_pick with proper event_type/name
    for p in response.picks:
        p.bonus_pick = True
        p.event_type = "golf_major"
        if not p.event_name:
            p.event_name = golf_pack.get("tournament_name", "Golf Major")
        p.ladder_designation = False
    logger.info(f"Golf major: vibe={response.slate_vibe}, {len(response.picks)} bonus picks")
    for p in response.picks:
        logger.info(f"  • {p.pick} ({p.event_name}) conf={p.confidence} u={p.units} odds={p.best_odds}")
    return response


def _call_with_tools(
    client: anthropic.Anthropic,
    model: str,
    system: str,
    user: str,
    tools: list[dict],
    max_tokens: int,
    temperature: float,
) -> anthropic.types.Message:
    """Run a tool-use loop until the model emits a final text answer."""
    messages: list[dict[str, Any]] = [{"role": "user", "content": user}]
    iterations = 0
    while iterations < 12:
        iterations += 1
        kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": messages,
            "temperature": temperature,
        }
        if tools:
            kwargs["tools"] = tools
        logger.info(f"Claude call iteration {iterations} (model={model})")
        msg = client.messages.create(**kwargs)
        if msg.stop_reason != "tool_use":
            return msg
        # If server-side web_search, Anthropic handles tool calls internally and returns final text;
        # otherwise we'd dispatch here. Server-side search has stop_reason != "tool_use".
        # If we did get tool_use blocks, just append assistant + a generic tool_result to keep going.
        messages.append({"role": "assistant", "content": msg.content})
        tool_results = []
        for block in msg.content:
            if getattr(block, "type", "") == "tool_use":
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": "Tool dispatch not implemented client-side; rely on server-side tools.",
                    "is_error": True,
                })
        if tool_results:
            messages.append({"role": "user", "content": tool_results})
        else:
            return msg
    return msg


def _extract_text(msg) -> str:
    if not msg or not msg.content:
        return ""
    parts: list[str] = []
    for block in msg.content:
        t = getattr(block, "type", "")
        if t == "text":
            parts.append(getattr(block, "text", ""))
    return "\n".join(parts)


def _robust_json_loads(s: str, raw_for_error: str = "") -> dict:
    """Parse JSON tolerantly. Claude sometimes emits unescaped newlines/tabs
    inside string values (e.g. multi-paragraph the_thesis). Try strict first,
    then strict=False, then a cleanup pass that escapes raw control chars
    only inside string spans."""
    try:
        return json.loads(s, strict=False)
    except json.JSONDecodeError:
        pass
    # Escape any raw newlines/tabs/carriage returns that appear inside strings.
    cleaned: list[str] = []
    in_str = False
    esc = False
    for ch in s:
        if in_str:
            if esc:
                cleaned.append(ch); esc = False; continue
            if ch == "\\":
                cleaned.append(ch); esc = True; continue
            if ch == '"':
                cleaned.append(ch); in_str = False; continue
            if ch == "\n":
                cleaned.append("\\n"); continue
            if ch == "\r":
                cleaned.append("\\r"); continue
            if ch == "\t":
                cleaned.append("\\t"); continue
            cleaned.append(ch)
        else:
            cleaned.append(ch)
            if ch == '"':
                in_str = True
    try:
        return json.loads("".join(cleaned), strict=False)
    except json.JSONDecodeError as e:
        raise ValueError(f"Handicapper JSON parse error: {e}\nRaw: {raw_for_error[:400]}")


def _extract_json(text: str) -> Optional[str]:
    """Pull the first valid JSON object out of model output."""
    # Try fenced code block
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if m:
        return m.group(1)
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    in_str = False
    esc = False
    for i, ch in enumerate(text[start:], start):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None
