"""Entry points: morning / midday / grader / refresh."""
from __future__ import annotations

import sys
import traceback
from pathlib import Path

import yaml
from dotenv import load_dotenv

# override=True so .env wins over any empty shell exports of the same names
load_dotenv(override=True)

PROJECT_ROOT = Path(__file__).parent.parent
CONFIG_PATH = PROJECT_ROOT / "config.yaml"


def _load_config() -> dict:
    if CONFIG_PATH.exists():
        return yaml.safe_load(CONFIG_PATH.read_text()) or {}
    return {}


def _cost_cap_check(config: dict) -> None:
    from engine.utils import load_daily_cost, get_logger
    cap = config.get("cost_cap", {}).get("daily_usd", 8.0)
    cur = load_daily_cost()
    if cur >= cap:
        from engine.publisher import set_system_paused
        set_system_paused(f"Daily cost cap ${cap:.2f} reached (current ${cur:.2f})")
        get_logger("main").error(f"COST CAP REACHED: ${cur:.2f} >= ${cap:.2f}")
        sys.exit(1)


def _et_hour_guard(target_hour: int, label: str) -> bool:
    """When two crons fire (DST awareness), only the one whose UTC time
    corresponds to `target_hour` ET should actually do the work. Returns
    True if we should proceed, False if we should bail.

    Manual `gh workflow run` (workflow_dispatch) always bypasses the guard
    so humans can trigger arbitrarily."""
    import os
    from engine.utils import nyc_now, get_logger
    if os.getenv("GITHUB_EVENT_NAME") == "workflow_dispatch":
        return True
    h = nyc_now().hour
    if h != target_hour:
        get_logger("main").info(
            f"Skipping {label}: ET hour is {h}, target is {target_hour} "
            f"(the other DST-twin cron will run at the right time)"
        )
        return False
    return True


def run_morning() -> None:
    from engine.utils import get_logger
    from engine.intel.orchestrator import harvest_intel
    from engine.handicapper import run_handicapper
    from engine.validator import validate_picks
    from engine.publisher import publish, set_system_paused
    from engine import analytics

    log = get_logger("main")
    if not _et_hour_guard(11, "morning"):
        return
    config = _load_config()
    log.info("=== MORNING RUN START ===")
    try:
        _cost_cap_check(config)

        sports = config.get("sports", {}).get("enabled", ["MLB", "NBA", "NHL", "NFL", "CFB"])
        claude_cfg = config.get("claude", {})
        claude_cfg["daily_cap_usd"] = config.get("cost_cap", {}).get("daily_usd", 8.0)

        packs = harvest_intel(sports)
        if not packs:
            log.warning("No games today across enabled sports")
            from engine.publisher import regenerate_site_data
            regenerate_site_data()
            analytics.refresh()
            return

        response = run_handicapper(packs, claude_cfg)
        valid = validate_picks(response)
        log.info(f"Valid picks after validator: {len(valid)}")
        publish(valid, response, mode="morning", packs=packs)
        log.info("=== MORNING RUN COMPLETE ===")

    except Exception as e:
        log.error(f"MORNING RUN FAILED: {e}\n{traceback.format_exc()}")
        try:
            set_system_paused(str(e))
        except Exception:
            pass
        sys.exit(1)


def run_midday() -> None:
    """Light refresh: re-fetch odds (line history snapshot), regenerate data, refresh analytics."""
    from engine.utils import get_logger
    from engine.intel.orchestrator import harvest_intel
    from engine.publisher import regenerate_site_data, set_system_paused
    from engine import analytics

    log = get_logger("main")
    if not _et_hour_guard(13, "midday"):
        return
    config = _load_config()
    log.info("=== MIDDAY REFRESH START ===")
    try:
        sports = config.get("sports", {}).get("enabled", ["MLB", "NBA", "NHL", "NFL", "CFB"])
        # Re-harvest only to update line_history snapshots; we don't re-call Claude.
        harvest_intel(sports)
        regenerate_site_data()
        analytics.refresh()
        log.info("=== MIDDAY REFRESH COMPLETE ===")
    except Exception as e:
        log.error(f"MIDDAY FAILED: {e}\n{traceback.format_exc()}")
        try:
            set_system_paused(str(e))
        except Exception:
            pass
        sys.exit(1)


def run_late_check() -> None:
    """Evening edge check: refresh intel, ask Sonnet for 0-1 late add, publish if any."""
    from engine.utils import get_logger
    from engine.intel.orchestrator import harvest_intel
    from engine.handicapper import run_late_add
    from engine.validator import validate_picks
    from engine.publisher import publish, load_history, regenerate_site_data, set_system_paused
    from engine import analytics

    log = get_logger("main")
    if not _et_hour_guard(17, "late_check"):
        return
    config = _load_config()
    log.info("=== LATE-ADD CHECK START ===")
    try:
        _cost_cap_check(config)
        sports = config.get("sports", {}).get("enabled", ["MLB", "NBA", "NHL", "NFL", "CFB"])
        claude_cfg = config.get("claude", {})
        claude_cfg["daily_cap_usd"] = config.get("cost_cap", {}).get("daily_usd", 8.0)

        packs = harvest_intel(sports)
        if not packs:
            log.info("No games on the board.")
            regenerate_site_data()
            analytics.refresh()
            return

        from engine.utils import nyc_date
        today = nyc_date()
        history = load_history()
        existing = [p for p in history.get("picks", [])
                    if p.get("date") == today and p.get("status") == "PEND"]

        response = run_late_add(packs, existing, claude_cfg)
        if not response.picks:
            log.info("Late check: all quiet — no add today.")
            regenerate_site_data()
            analytics.refresh()
            return

        valid = validate_picks(response)
        log.info(f"Late check: {len(valid)} valid late-add picks")
        publish(valid, response, mode="late_add", packs=packs)
        log.info("=== LATE-ADD CHECK COMPLETE ===")
    except Exception as e:
        import traceback
        log.error(f"LATE-ADD FAILED: {e}\n{traceback.format_exc()}")
        # Do NOT pause the whole system on a late-check failure.
        try:
            regenerate_site_data()
            analytics.refresh()
        except Exception:
            pass


def run_golf_major() -> None:
    """Bonus pick for an active golf major (Masters / PGA / US Open / The Open).
    Returns silently if no major is currently active in The Odds API."""
    from engine.utils import get_logger
    from engine.intel.golf import harvest_golf_majors
    from engine.handicapper import run_golf_major as call_claude_golf
    from engine.validator import validate_picks
    from engine.publisher import publish, regenerate_site_data
    from engine import analytics

    log = get_logger("main")
    # Hour guard — DST-aware. workflow_dispatch always bypasses.
    if not _et_hour_guard(11, "golf_major"):
        return
    config = _load_config()
    log.info("=== GOLF MAJOR BONUS START ===")
    try:
        _cost_cap_check(config)
        active_majors = harvest_golf_majors()
        if not active_majors:
            log.info("No active golf major. Nothing to do.")
            return
        for pack in active_majors:
            claude_cfg = config.get("claude", {}).copy()
            claude_cfg["daily_cap_usd"] = config.get("cost_cap", {}).get("daily_usd", 8.0)
            response = call_claude_golf(pack, claude_cfg)
            valid = validate_picks(response)
            log.info(f"Golf major valid picks after validator: {len(valid)}")
            if valid:
                publish(valid, response, mode="golf_bonus", golf_packs=[pack])
        regenerate_site_data()
        analytics.refresh()
        log.info("=== GOLF MAJOR BONUS COMPLETE ===")
    except Exception as e:
        import traceback
        log.error(f"GOLF MAJOR FAILED: {e}\n{traceback.format_exc()}")
        # Don't pause the system — bonus failures shouldn't break daily picks
        try:
            regenerate_site_data()
            analytics.refresh()
        except Exception:
            pass


def run_grader_job() -> None:
    from engine.utils import get_logger
    from engine.grader import run_grader

    log = get_logger("main")
    log.info("=== GRADER START ===")
    try:
        run_grader()
        log.info("=== GRADER COMPLETE ===")
    except Exception as e:
        log.error(f"GRADER FAILED: {e}\n{traceback.format_exc()}")
        sys.exit(1)


def run_refresh() -> None:
    """Refresh analytics + data.json without harvesting (cheap, for after manual edits)."""
    from engine.publisher import regenerate_site_data
    from engine import analytics
    regenerate_site_data()
    analytics.refresh()


if __name__ == "__main__":
    cmds = {
        "morning":     run_morning,
        "midday":      run_midday,
        "late_check":  run_late_check,
        "golf_major":  run_golf_major,
        "grader":      run_grader_job,
        "refresh":     run_refresh,
    }
    cmd = sys.argv[1] if len(sys.argv) > 1 else "morning"
    fn = cmds.get(cmd)
    if not fn:
        print(f"Unknown command: {cmd}. Use: {list(cmds.keys())}", file=sys.stderr)
        sys.exit(2)
    fn()
