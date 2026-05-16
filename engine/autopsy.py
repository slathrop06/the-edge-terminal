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

SYSTEM_PROMPT = """You are the autopsy engine for THE EDGE. You classify and document losing picks.

Classifications:
- DATA: data was wrong/missing/stale. The pick might've been right with better data.
- MODEL: reasoning was flawed — wrong thesis, bad odds compare, ignored a factor.
- VARIANCE: pick was sound, outcome was unlucky.

Return strict JSON:
{
  "classification": "DATA|MODEL|VARIANCE",
  "post_mortem": "2-3 sentences. Clinical, honest.",
  "candidate_rule": "If DATA or MODEL, one sentence proposing a validator rule. Include sample size caveat. If VARIANCE, null.",
  "sample_size_warning": "e.g. 'N=1, insufficient' or 'N=3 in sample, approaching threshold'"
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
            msg = client.messages.create(
                model=AUTOPSY_MODEL,
                max_tokens=900,
                temperature=0.2,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": f"Post-loss autopsy:\n\n{json.dumps(context, indent=2)}\n\nReturn strict JSON."}],
            )
            if msg.usage:
                cost = estimate_cost(AUTOPSY_MODEL, msg.usage.input_tokens, msg.usage.output_tokens)
                record_api_cost(cost)
            raw = msg.content[0].text if msg.content else ""
            from engine.handicapper import _extract_json
            js = _extract_json(raw)
            if not js:
                raise ValueError("No JSON in autopsy response")
            entry = json.loads(js)
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
