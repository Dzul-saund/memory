"""Small shared helpers: retry/backoff, time parsing, formatting."""
from __future__ import annotations

import math
import time
from datetime import datetime
from typing import Callable, Optional, TypeVar

T = TypeVar("T")


def with_retry(
    fn: Callable[[], T],
    *,
    retries: int,
    base: float,
    max_backoff: float,
    what: str,
    logger=None,
    sleep: Callable[[float], None] = time.sleep,
) -> T:
    """Call ``fn`` and retry on any exception with exponential backoff.

    Backoff is ``base ** attempt`` seconds (capped at ``max_backoff``), e.g. with
    base=2 -> 2s, 4s, 8s, 16s. Re-raises the last exception once ``retries`` is
    exhausted so the caller's circuit-breaker can react.
    """
    attempt = 0
    while True:
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - we genuinely want to catch all
            attempt += 1
            if attempt > retries:
                raise
            delay = min(base ** attempt, max_backoff)
            if logger is not None:
                logger.warning(
                    "%s failed (attempt %d/%d): %s — retrying in %.0fs",
                    what, attempt, retries, exc, delay,
                )
            sleep(delay)


def parse_iso(s: str) -> datetime:
    """Parse an ISO-8601 timestamp, tolerating a trailing 'Z'."""
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    return datetime.fromisoformat(s)


def iso_to_epoch(s: str) -> int:
    return int(parse_iso(s).timestamp())


def floor2(x: float) -> float:
    """Round down to 2 decimals (Polymarket share precision)."""
    return math.floor(x * 100) / 100.0


def fmt(x: Optional[float], nd: int = 2) -> str:
    return "-" if x is None else f"{x:.{nd}f}"


def fmt_money(x: Optional[float]) -> str:
    return "-" if x is None else f"{x:,.2f}"
