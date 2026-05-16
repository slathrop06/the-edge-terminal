"""Logging, retry, timezone, API cost ledger."""
from __future__ import annotations

import functools
import json
import logging
import os
import time
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

import pytz

PROJECT_ROOT = Path(__file__).parent.parent
LOGS_DIR = PROJECT_ROOT / "logs"
DATA_DIR = PROJECT_ROOT / "data"
SITE_DIR = PROJECT_ROOT / "site"
LOGS_DIR.mkdir(exist_ok=True)
DATA_DIR.mkdir(exist_ok=True)
SITE_DIR.mkdir(exist_ok=True)

ET = pytz.timezone("America/New_York")


def nyc_now() -> datetime:
    return datetime.now(ET)


def nyc_date() -> str:
    return nyc_now().strftime("%Y-%m-%d")


def nyc_date_label() -> str:
    return nyc_now().strftime("%a %b %d %Y").upper()


_LOGGERS: dict[str, logging.Logger] = {}


def get_logger(name: str) -> logging.Logger:
    if name in _LOGGERS:
        return _LOGGERS[name]
    log_file = LOGS_DIR / f"{name}-{nyc_date()}.log"
    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()
    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)-14s %(message)s",
        datefmt="%H:%M:%S",
    )
    fh = RotatingFileHandler(log_file, maxBytes=10 * 1024 * 1024, backupCount=5)
    fh.setFormatter(fmt); fh.setLevel(logging.DEBUG)
    ch = logging.StreamHandler(); ch.setFormatter(fmt); ch.setLevel(logging.INFO)
    logger.addHandler(fh); logger.addHandler(ch)
    _LOGGERS[name] = logger
    return logger


def retry(attempts: int = 3, backoff: float = 2.0, exceptions=(Exception,)):
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            log = get_logger("retry")
            last = None
            for n in range(1, attempts + 1):
                try:
                    return func(*args, **kwargs)
                except exceptions as e:
                    last = e
                    if n < attempts:
                        wait = backoff ** (n - 1)
                        log.warning(f"{func.__name__} {n}/{attempts}: {e}. retry in {wait:.1f}s")
                        time.sleep(wait)
                    else:
                        log.error(f"{func.__name__} failed after {attempts}: {e}")
            raise last
        return wrapper
    return decorator


# ─── API cost ledger ─────────────────────────────────────────────────────────
_COST_PER_1M = {
    "claude-opus-4-7":   {"input": 15.0, "output": 75.0},
    "claude-opus-4-6":   {"input": 15.0, "output": 75.0},
    "claude-sonnet-4-6": {"input": 3.0,  "output": 15.0},
}


def estimate_cost(model: str, input_tokens: int, output_tokens: int, cache_read: int = 0) -> float:
    r = _COST_PER_1M.get(model, {"input": 15.0, "output": 75.0})
    return (input_tokens * r["input"] + output_tokens * r["output"] + cache_read * r["input"] * 0.1) / 1_000_000


def load_daily_cost(date_str: str | None = None) -> float:
    f = LOGS_DIR / f"api-cost-{date_str or nyc_date()}.txt"
    if f.exists():
        try:
            return float(f.read_text().strip())
        except ValueError:
            return 0.0
    return 0.0


def record_api_cost(cost_usd: float, date_str: str | None = None) -> float:
    f = LOGS_DIR / f"api-cost-{date_str or nyc_date()}.txt"
    total = load_daily_cost(date_str) + cost_usd
    f.write_text(f"{total:.6f}")
    return total


# ─── JSON read/write ─────────────────────────────────────────────────────────
def read_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return default


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, default=str))


# ─── Odds math ───────────────────────────────────────────────────────────────
import re


def american_to_prob(odds: str | float | int) -> float:
    """American odds → implied probability (0-1)."""
    try:
        n = float(re.sub(r"[^0-9\-\+\.]", "", str(odds)))
    except (ValueError, TypeError):
        return 0.5
    if n < 0:
        return abs(n) / (abs(n) + 100)
    return 100 / (n + 100)


def american_to_decimal(odds: str | float | int) -> float:
    try:
        n = float(re.sub(r"[^0-9\-\+\.]", "", str(odds)))
    except (ValueError, TypeError):
        return 1.909
    if n < 0:
        return 1 + 100 / abs(n)
    return 1 + n / 100


def american_value(odds: str | float | int) -> float:
    """Return numeric value (negative = favored)."""
    try:
        return float(re.sub(r"[^0-9\-\+\.]", "", str(odds)))
    except (ValueError, TypeError):
        return 0.0


def units_profit(units: float, odds: str | float | int, status: str) -> float:
    if status == "PUSH":
        return 0.0
    if status == "LOSS":
        return -units
    if status == "WIN":
        dec = american_to_decimal(odds)
        return round(units * (dec - 1), 4)
    return 0.0
