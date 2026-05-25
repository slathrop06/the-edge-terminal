"""Entry points: morning / midday / grader / refresh."""
from __future__ import annotations

import sys
import traceback
from datetime import datetime, timezone
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


def _dst_cron_guard(edt_cron: str, est_cron: str, label: str) -> bool:
    """When two DST-twin crons fire (one for EDT, one for EST), only the
    one matching today's NYC DST state should actually do the work.
    Returns True if we should proceed, False if we should bail.

    The discriminator is the triggering cron expression (passed in via the
    GITHUB_SCHEDULE env var, sourced from ${{ github.event.schedule }} in
    the workflow). This is deterministic regardless of when GitHub Actions
    actually delivers the cron — the previous wall-clock-hour guard lost
    a race when cron delivery slipped past the top of the target hour.

    Manual `gh workflow run` (workflow_dispatch) always bypasses the guard
    so humans can trigger arbitrarily."""
    import os
    from engine.utils import nyc_now, get_logger
    log = get_logger("main")

    if os.getenv("GITHUB_EVENT_NAME") == "workflow_dispatch":
        return True

    triggering_cron = os.getenv("GITHUB_SCHEDULE", "").strip()
    is_dst = bool(nyc_now().dst())
    correct_cron = edt_cron if is_dst else est_cron

    # If we don't know which cron triggered us (local run, older workflow
    # config, etc.), proceed — better to over-fire than to silently miss.
    if not triggering_cron:
        log.info(f"{label}: GITHUB_SCHEDULE not set — proceeding without DST guard.")
        return True

    if triggering_cron != correct_cron:
        log.info(
            f"Skipping {label}: triggering cron {triggering_cron!r} is the "
            f"{'EST' if is_dst else 'EDT'} twin; today NYC is in "
            f"{'EDT' if is_dst else 'EST'}, so the {correct_cron!r} twin "
            f"is the one that does the work."
        )
        return False
    return True


def _should_run_morning() -> bool:
    """Permanent morning-run gate. Replaces the earlier cron-expression DST
    guard for morning specifically — the cron-expression guard was tolerant
    of GH Actions cron *delay* but had no answer when GH Actions silently
    *skips* a scheduled delivery entirely (which happens — observed in prod
    on 2026-05-18 and again 2026-05-19, where neither DST-twin cron fired).

    Three checks, in order:
      1. workflow_dispatch always wins (manual trigger from anyone).
      2. If today's morning publish already happened (any PEND main-track
         pick exists for nyc_date()), bail — the work is done. Publisher
         already enforces this in its lock check; bailing here avoids an
         unnecessary harvest + Claude call (~$1.20).
      3. If NYC's wall clock is earlier than 11:00, bail — we promised the
         boys an 11 AM lock-in, not 10 AM. Earlier crons (the EST-window
         15:00 UTC fire when in EDT, etc.) wait for the next one.

    Combined with multiple cron triggers in morning.yml (every 30 min from
    15:00 to 18:00 UTC), this is resilient to BOTH cron delay (delayed
    crons hit the idempotency check and no-op) AND cron skip (a later cron
    catches the missed earlier one). Year-round, in either DST state.
    """
    import os
    from engine.utils import nyc_now, nyc_date, get_logger
    log = get_logger("main")

    if os.getenv("GITHUB_EVENT_NAME") == "workflow_dispatch":
        return True

    # Idempotency: did morning already publish today?
    try:
        from engine.publisher import load_history
        history = load_history()
        today = nyc_date()
        already_done = any(
            p.get("date") == today
            and p.get("status") == "PEND"
            and not p.get("bonus_pick")
            for p in history.get("picks", [])
        )
        if already_done:
            log.info(f"morning: today's picks already locked for {today} — no-op.")
            return False
    except Exception as e:
        log.warning(f"morning: idempotency check failed ({e}); proceeding.")

    # Time gate: don't publish before 11 AM ET.
    now = nyc_now()
    if now.hour < 11:
        log.info(
            f"morning: NYC clock is {now.strftime('%H:%M')} — "
            f"waiting for 11:00+ (next cron will pick it up)."
        )
        return False

    return True


def _drop_started_games(packs: list, label: str) -> list:
    """Filter out IntelPacks whose first_pitch_iso is already in the past.

    Why: when the morning workflow runs late (e.g., after a manual retrigger
    or after long cron delay), The Odds API may still list games whose first
    pitch has already happened — books leave pre-game markets up briefly
    after start. Without this filter, Claude can recommend a pick the boys
    have no chance to act on (happened 2026-05-21: a 17:09 ET rerun produced
    an NYM @ WSH under for a game that first-pitched at 16:05 ET).

    Keep packs with missing/unparseable first_pitch_iso — don't reject for
    bad data, only for confirmed past starts.
    """
    from engine.utils import get_logger
    log = get_logger("main")
    now = datetime.now(timezone.utc)
    kept, dropped = [], []
    for p in packs:
        fpi = (getattr(p, "first_pitch_iso", "") or "").strip()
        if not fpi:
            kept.append(p)
            continue
        try:
            t = datetime.fromisoformat(fpi.replace("Z", "+00:00"))
            if t.tzinfo is None:
                t = t.replace(tzinfo=timezone.utc)
        except Exception:
            kept.append(p)
            continue
        if t > now:
            kept.append(p)
        else:
            dropped.append((p, t))
    if dropped:
        log.info(
            f"{label}: dropped {len(dropped)} already-started game(s): "
            + ", ".join(f"{getattr(p,'game_id','?')} @ {t.isoformat()}" for p, t in dropped)
        )
    return kept


def run_morning() -> None:
    from engine.utils import get_logger
    from engine.intel.orchestrator import harvest_intel
    from engine.handicapper import run_handicapper
    from engine.validator import validate_picks
    from engine.publisher import publish, set_system_paused
    from engine import analytics

    log = get_logger("main")
    if not _should_run_morning():
        return
    config = _load_config()
    log.info("=== MORNING RUN START ===")
    try:
        _cost_cap_check(config)

        sports = config.get("sports", {}).get("enabled", ["MLB", "NBA", "NHL", "NFL", "CFB"])
        claude_cfg = config.get("claude", {})
        claude_cfg["daily_cap_usd"] = config.get("cost_cap", {}).get("daily_usd", 8.0)

        packs = harvest_intel(sports)
        packs = _drop_started_games(packs, "morning")
        if not packs:
            log.warning("No (future) games today across enabled sports")
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
    if not _dst_cron_guard("0 17 * * *", "0 18 * * *", "midday"):
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
    if not _dst_cron_guard("0 21 * * *", "0 22 * * *", "late_check"):
        return
    config = _load_config()
    log.info("=== LATE-ADD CHECK START ===")
    try:
        _cost_cap_check(config)
        sports = config.get("sports", {}).get("enabled", ["MLB", "NBA", "NHL", "NFL", "CFB"])
        claude_cfg = config.get("claude", {})
        claude_cfg["daily_cap_usd"] = config.get("cost_cap", {}).get("daily_usd", 8.0)

        packs = harvest_intel(sports)
        packs = _drop_started_games(packs, "late_check")
        if not packs:
            log.info("No (future) games on the board.")
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
    # Cron-based DST guard. workflow_dispatch always bypasses.
    if not _dst_cron_guard("0 15 * * 3", "0 16 * * 3", "golf_major"):
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
