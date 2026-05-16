"""Analytics — rollups across all scopes/slices, including ladder."""
from __future__ import annotations

from datetime import timedelta
from typing import Optional

from engine.utils import (
    get_logger, nyc_now, nyc_date, units_profit, read_json, write_json,
    american_value, SITE_DIR
)
from engine.publisher import load_history, ANALYTICS_JSON_PATH
from engine import ladder

logger = get_logger("analytics")


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _decided(picks: list[dict]) -> list[dict]:
    return [p for p in picks if p.get("status") in ("WIN", "LOSS")]


def _rollup(picks: list[dict]) -> dict:
    w = sum(1 for p in picks if p.get("status") == "WIN")
    l = sum(1 for p in picks if p.get("status") == "LOSS")
    push = sum(1 for p in picks if p.get("status") == "PUSH")
    decided = _decided(picks)
    total_pl = sum(p.get("units_result") or 0 for p in picks if p.get("status") in ("WIN", "LOSS"))
    total_wagered = sum(float(p.get("units", 1.0)) for p in decided)
    roi = round((total_pl / total_wagered) * 100, 2) if total_wagered else 0.0
    win_rate = round((w / len(decided)) * 100, 2) if decided else 0.0
    clvs = [p["clv_cents"] for p in picks if p.get("clv_cents") is not None]
    avg_clv = round(sum(clvs) / len(clvs), 2) if clvs else 0.0
    return {
        "record": f"{w}-{l}-{push}",
        "wins": w, "losses": l, "pushes": push,
        "total_picks": len(picks),
        "units_pl": round(total_pl, 3),
        "roi_pct": roi,
        "win_rate_pct": win_rate,
        "avg_clv_cents": avg_clv,
        "longest_w_streak": _longest_streak(picks, "WIN"),
        "longest_l_streak": _longest_streak(picks, "LOSS"),
        "biggest_win_units": round(max((p["units_result"] for p in picks if p.get("status") == "WIN"), default=0), 3),
        "biggest_loss_units": round(min((p["units_result"] for p in picks if p.get("status") == "LOSS"), default=0), 3),
    }


def _longest_streak(picks: list[dict], status: str) -> int:
    chrono = sorted(
        [p for p in picks if p.get("status") in ("WIN", "LOSS")],
        key=lambda p: p.get("graded_at") or p.get("date") or ""
    )
    best = cur = 0
    for p in chrono:
        if p["status"] == status:
            cur += 1
            best = max(best, cur)
        else:
            cur = 0
    return best


def _by_slice(picks: list[dict], key: str, values: list[str]) -> dict:
    result = {}
    for v in values:
        subset = [p for p in picks if str(p.get(key, "")).upper() == v.upper()]
        if subset:
            result[v] = _rollup(subset)
    return result


def _scope_picks(all_picks: list[dict], scope: str) -> list[dict]:
    now = nyc_now()
    today = now.strftime("%Y-%m-%d")
    if scope == "daily":
        return [p for p in all_picks if p.get("date") == today]
    if scope == "weekly":
        cutoff = (now - timedelta(days=7)).strftime("%Y-%m-%d")
    elif scope == "monthly":
        cutoff = (now - timedelta(days=30)).strftime("%Y-%m-%d")
    elif scope == "yearly":
        cutoff = (now - timedelta(days=365)).strftime("%Y-%m-%d")
    else:
        return all_picks
    return [p for p in all_picks if p.get("date", "") >= cutoff]


def _current_streak(picks: list[dict]) -> dict:
    chrono = sorted(
        [p for p in picks if p.get("status") in ("WIN", "LOSS")],
        key=lambda p: p.get("graded_at") or p.get("date") or "",
        reverse=True,
    )
    if not chrono:
        return {"type": "none", "count": 0, "label": "—"}
    t = chrono[0]["status"]
    n = 0
    for p in chrono:
        if p["status"] == t:
            n += 1
        else:
            break
    arrow = "↑" if t == "WIN" else "↓"
    return {"type": t, "count": n, "label": f"{t[0]}{n} {arrow}"}


def _clv_trend(picks: list[dict], n: int = 50) -> list[dict]:
    graded = sorted(
        [p for p in picks if p.get("clv_cents") is not None],
        key=lambda p: p.get("graded_at") or p.get("date") or "",
        reverse=True,
    )[:n]
    return [
        {"date": p.get("date"), "clv_cents": p["clv_cents"], "pick": p.get("pick")}
        for p in reversed(graded)
    ]


def _recent_form(picks: list[dict], n: int = 20) -> list[dict]:
    graded = sorted(
        [p for p in picks if p.get("status") in ("WIN", "LOSS", "PUSH")],
        key=lambda p: p.get("graded_at") or p.get("date") or "",
        reverse=True,
    )[:n]
    return [
        {
            "date": p.get("date"),
            "pick": p.get("pick"),
            "result": p.get("status"),
            "units": p.get("units_result") or 0,
            "sport": p.get("sport"),
            "ladder": bool(p.get("ladder_designation")),
        } for p in graded
    ]


def refresh() -> None:
    history = load_history()
    all_picks = history.get("picks", [])
    sports = sorted({p.get("sport") for p in all_picks if p.get("sport")})
    conf_tiers = ["3", "4", "5"]
    markets = sorted({p.get("market") for p in all_picks if p.get("market")})

    scopes = {}
    for scope in ("daily", "weekly", "monthly", "yearly", "all_time"):
        subset = _scope_picks(all_picks, scope)
        scopes[scope] = {
            "overall": _rollup(subset),
            "by_sport": _by_slice(subset, "sport", sports),
            "by_confidence": _by_slice(subset, "confidence", conf_tiers),
            "by_pick_type": _by_slice(subset, "market", markets),
        }

    ladder_state = ladder.load_state()
    ladder_picks = [p for p in all_picks if p.get("ladder_designation")]
    ladder_rollup = _rollup(ladder_picks) if ladder_picks else _rollup([])

    payload = {
        "generated_at": nyc_now().isoformat(),
        "scopes": scopes,
        "current_streak": _current_streak(all_picks),
        "clv_trend": _clv_trend(all_picks, 50),
        "recent_form": _recent_form(all_picks, 20),
        "ladder": {
            "state": ladder_state,
            "all_time_record": ladder_rollup,
        },
        "totals": {
            "all_picks": len(all_picks),
            "sports": sports,
        },
    }
    write_json(ANALYTICS_JSON_PATH, payload)
    logger.info(f"analytics.json refreshed: {len(all_picks)} picks, ladder cur={ladder_state['current_streak']}")
