"""Autopsy — Claude classifies losses and proposes rule candidates."""
from __future__ import annotations

import json
import os
from pathlib import Path

import anthropic

from engine.utils import (
    get_logger, nyc_now, read_json, write_json,
    estimate_cost, record_api_cost, DATA_DIR
)

logger = get_logger("autopsy")

AUTOPSY_LOG = DATA_DIR / "autopsy_log.json"
RULE_CANDIDATES = DATA_DIR / "rule_candidates.json"
AUTOPSY_MODEL = "claude-sonnet-4-6"

SYSTEM_PROMPT = """You are the autopsy engine for Scott Bot Picks. You analyze losing picks honestly and tell the story of HOW the loss actually played out.

You have `web_search` available. **Use it** to look up the box score, key plays, and how the game flowed. A specific story makes the autopsy useful. Don't write generic "the outcome was unlucky" prose — say what specifically happened.

Classifications:
- **DATA** — the data we had was wrong, missing, or stale (e.g. starting pitcher changed at the last minute, weather report was off). The pick might have been correct with better data.
- **MODEL** — the reasoning was flawed (wrong thesis, bad odds compare, ignored a key factor, picked a side the data didn't actually support).
- **VARIANCE** — the pick was correctly reasoned and the data was good, but a specific unlucky thing happened. **Cite the unlucky moment** — extra innings, bullpen meltdown, walk-off, blown save, garbage-time touchdown, a player injury mid-game, a 10-run inning, a weather event nobody could've forecast.

**Be honest. Don't lazy-classify.** A 10-5 final on an Under 7.5 looks like a model failure on the surface — but if the under was alive at 5-5 through 9 and the team put up a 5-spot in extras, that's VARIANCE with a specific story. SAY THAT.

Return strict JSON:
{
  "classification": "DATA|MODEL|VARIANCE",
  "post_mortem": "2-3 sentences. The SPECIFIC story — what was the score / where did it go wrong / what was the deciding moment. If web_search gave you the box score, cite it.",
  "candidate_rule": "If DATA or MODEL, one sentence proposing a validator rule, with sample size caveat. If VARIANCE, null.",
  "sample_size_warning": "e.g. 'N=1, insufficient to establish rule' or 'N=3 in sample, watching for pattern'"
}

Never propose a rule without a sample size caveat. One loss is noise.
"""


def run_autopsy(pick_row: dict, result_score: str) -> dict:
    logger.info(f"Autopsy: {pick_row.get('pick')} ({pick_row.get('game')})")

    context = {
        "pick": pick_row.get("pick"),
        "game": pick_row.get("game"),
        "sport": pick_row.get("sport"),
        "odds": pick_row.get("best_odds"),
        "confidence": pick_row.get("confidence"),
        "units": pick_row.get("units"),
        "headline": pick_row.get("headline"),
        "the_thesis": pick_row.get("the_thesis"),
        "the_data": pick_row.get("the_data"),
        "the_market": pick_row.get("the_market"),
        "case_against": pick_row.get("case_against"),
        "data_confidence": pick_row.get("data_confidence"),
        "result_score": result_score,
        "date": pick_row.get("date"),
    }

    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    if not api_key:
        entry = _stub(pick_row, result_score)
    else:
        try:
            client = anthropic.Anthropic(api_key=api_key)
            user_msg = (
                f"Post-loss autopsy. Use web_search to look up the box score "
                f"and key moments — make the post_mortem SPECIFIC. Then return strict JSON only.\n\n"
                f"{json.dumps(context, indent=2)}"
            )
            messages: list = [{"role": "user", "content": user_msg}]
            final_text = ""
            for _ in range(8):
                msg = client.messages.create(
                    model=AUTOPSY_MODEL,
                    max_tokens=1200,
                    temperature=0.2,
                    system=SYSTEM_PROMPT,
                    tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 4}],
                    messages=messages,
                )
                if msg.usage:
                    cost = estimate_cost(AUTOPSY_MODEL, msg.usage.input_tokens, msg.usage.output_tokens)
                    record_api_cost(cost)
                if msg.stop_reason != "tool_use":
                    for block in (msg.content or []):
                        if getattr(block, "type", "") == "text":
                            final_text += getattr(block, "text", "")
                    break
                # If model returned a client-side tool call, append + continue.
                messages.append({"role": "assistant", "content": msg.content})
            from engine.handicapper import _extract_json, _robust_json_loads
            js = _extract_json(final_text)
            if not js:
                raise ValueError(f"No JSON in autopsy response: {final_text[:200]}")
            entry = _robust_json_loads(js, final_text)
        except Exception as e:
            logger.error(f"Autopsy API call failed: {e}")
            entry = _stub(pick_row, result_score)

    record = {
        "id": pick_row.get("id"),
        "date": pick_row.get("date"),
        "pick": pick_row.get("pick"),
        "game": pick_row.get("game"),
        "odds": pick_row.get("best_odds"),
        "result_score": result_score,
        "classification": entry.get("classification", "VARIANCE"),
        "post_mortem": entry.get("post_mortem", ""),
        "candidate_rule": entry.get("candidate_rule"),
        "sample_size_warning": entry.get("sample_size_warning", "N=1"),
        "analyzed_at": nyc_now().isoformat(),
    }

    log = read_json(AUTOPSY_LOG, [])
    log.insert(0, record)
    write_json(AUTOPSY_LOG, log[:200])

    if record.get("candidate_rule"):
        cands = read_json(RULE_CANDIDATES, [])
        cands.append({
            "proposed_at": nyc_now().isoformat(),
            "source_pick_id": pick_row.get("id"),
            "source_loss": f"{pick_row.get('pick')} ({pick_row.get('game')})",
            "candidate_rule": record["candidate_rule"],
            "sample_size_warning": record["sample_size_warning"],
            "status": "PENDING_REVIEW",
        })
        write_json(RULE_CANDIDATES, cands)
        logger.info(f"Rule candidate added for review")

    return record


def _stub(pick_row: dict, result_score: str) -> dict:
    return {
        "classification": "VARIANCE",
        "post_mortem": f"{pick_row.get('pick')} lost {result_score}. Autopsy unavailable.",
        "candidate_rule": None,
        "sample_size_warning": "N/A — stub",
    }
