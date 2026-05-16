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


class DataPoint(BaseModel):
    label: str
    value: str
    context: str = ""


class Pick(BaseModel):
    id: str
    sport: str
    game: str
    first_pitch_iso: str = ""
    pick: str
    best_book: str = ""
    best_odds: str
    market: str = ""
    confidence: int
    units: float
    ladder_designation: bool = False
    data_confidence: float = 0.7
    rules_passed: list[str] = Field(default_factory=list)
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
    slate_assessment: str
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
    temperature = float(config.get("temperature", 0.2))
    max_tokens = int(config.get("max_tokens", 8000))
    use_web_search = bool(config.get("use_web_search", True))
    daily_cap = float(config.get("daily_cap_usd", 8.0))

    _check_cost_cap(daily_cap)

    if not SYSTEM_PROMPT_PATH.exists():
        raise FileNotFoundError(f"System prompt missing: {SYSTEM_PROMPT_PATH}")
    system_prompt = SYSTEM_PROMPT_PATH.read_text()

    # Serialize packs as JSON the model can reason over
    slate = {
        "date": nyc_date(),
        "now_iso": nyc_now().isoformat(),
        "games": [p.model_dump(exclude_none=True) for p in packs],
    }
    slate_json = json.dumps(slate, default=str, separators=(",", ":"))
    if len(slate_json) > 180_000:
        logger.warning(f"Slate JSON huge ({len(slate_json)} chars) — truncating to 180k")
        slate_json = slate_json[:180_000] + "...}"

    user_msg = (
        f"Today's slate ({slate['date']}). Review the IntelPacks, optionally use web_search "
        f"for late-breaking news, then return strict JSON per the system prompt.\n\n"
        f"```json\n{slate_json}\n```"
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
        }
        # Opus 4.7 deprecated `temperature`; older models still accept it.
        if not model.startswith("claude-opus-4-7"):
            kwargs["temperature"] = temperature
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
