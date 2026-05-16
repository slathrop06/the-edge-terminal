"""Ladder Challenge — designation, streak tracking."""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from engine.handicapper import Pick
from engine.utils import get_logger, nyc_now, read_json, write_json, DATA_DIR

logger = get_logger("ladder")

LADDER_HISTORY_PATH = DATA_DIR / "ladder_history.json"
LADDER_TARGET = 10  # rungs per climb


def _default_state() -> dict:
    return {
        "current_streak": 0,
        "longest_streak": 0,
        "completed_climbs": 0,
        "target": LADDER_TARGET,
        "history": [],   # [{date, pick_id, pick, game, status, after_streak}, ...]
        "updated_at": nyc_now().isoformat(),
    }


def load_state() -> dict:
    state = read_json(LADDER_HISTORY_PATH, _default_state())
    # Make sure all keys present
    for k, v in _default_state().items():
        state.setdefault(k, v)
    return state


def save_state(state: dict) -> None:
    state["updated_at"] = nyc_now().isoformat()
    write_json(LADDER_HISTORY_PATH, state)


def designate_ladder_pick(picks: list[Pick]) -> Optional[Pick]:
    """Mark exactly one pick as the ladder pick. Returns it (or None if no picks)."""
    if not picks:
        return None
    # Honor Claude's explicit designation if set
    flagged = [p for p in picks if p.ladder_designation]
    if len(flagged) == 1:
        chosen = flagged[0]
    elif len(flagged) > 1:
        # Multiple flagged — keep the highest confidence one
        chosen = max(flagged, key=lambda p: (p.confidence, p.data_confidence))
        for p in flagged:
            p.ladder_designation = False
        chosen.ladder_designation = True
    else:
        # Auto-pick: highest confidence, tiebreak data_confidence
        chosen = max(picks, key=lambda p: (p.confidence, p.data_confidence))
        chosen.ladder_designation = True
    logger.info(f"Ladder pick: {chosen.pick} ({chosen.game}) conf={chosen.confidence}")
    return chosen


def update_after_grading(ladder_pick_row: dict, status: str) -> dict:
    """Update ladder state when a graded ladder pick comes in.

    Rules:
      - WIN  → current_streak += 1; if reaches target → record climb, reset to 0
      - PUSH → streak preserved (no change)
      - LOSS → reset current_streak to 0
    Returns updated state.
    """
    state = load_state()
    prev_streak = state["current_streak"]

    if status == "WIN":
        new_streak = prev_streak + 1
        climb_completed = False
        if new_streak >= state["target"]:
            state["completed_climbs"] = state.get("completed_climbs", 0) + 1
            climb_completed = True
            new_streak = 0
        state["current_streak"] = new_streak
        state["longest_streak"] = max(state.get("longest_streak", 0), new_streak if not climb_completed else state["target"])
    elif status == "LOSS":
        state["current_streak"] = 0
    elif status == "PUSH":
        pass  # carry the streak

    state["history"].insert(0, {
        "date": ladder_pick_row.get("date"),
        "pick_id": ladder_pick_row.get("id"),
        "pick": ladder_pick_row.get("pick"),
        "game": ladder_pick_row.get("game"),
        "status": status,
        "after_streak": state["current_streak"],
    })
    # Cap history at 365 entries
    state["history"] = state["history"][:365]
    save_state(state)
    logger.info(f"Ladder updated: status={status} streak {prev_streak}→{state['current_streak']}")
    return state


def streak_label(state: dict) -> str:
    cur = state.get("current_streak", 0)
    tgt = state.get("target", LADDER_TARGET)
    return f"{cur} of {tgt}"
