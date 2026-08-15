"""Failure classification and deterministic recovery decisions."""
from __future__ import annotations

from enum import Enum


class DamageKind(str, Enum):
    TIMEOUT = "timeout"
    CONTEXT_PRESSURE = "context_pressure"
    CHOKED = "choked"
    RATE_LIMIT = "rate_limit"
    FATAL = "fatal"


def classify_failure(error: str | None) -> DamageKind:
    text = (error or "").lower()
    if "timeout" in text or "timed out" in text:
        return DamageKind.TIMEOUT
    if any(part in text for part in ("context window", "context length", "too many tokens", "compact")):
        return DamageKind.CONTEXT_PRESSURE
    if any(part in text for part in ("rate limit", "429", "too many requests")):
        return DamageKind.RATE_LIMIT
    if any(part in text for part in ("overloaded", "capacity", "choked", "empty response", "truncated")):
        return DamageKind.CHOKED
    return DamageKind.FATAL


def should_retry(error: str | None, recommended: bool, retry_choked: bool) -> bool:
    # The worker/harness is the authority on whether a run should be retried.
    # Automatic respawn is disabled by design: a node is only re-run on an
    # explicit user action (re-run / retry). Transient "retry_choked" retries
    # are therefore only honored when the worker also recommends a retry.
    if not recommended:
        return False
    kind = classify_failure(error)
    if kind in {
        DamageKind.CONTEXT_PRESSURE,
        DamageKind.CHOKED,
        DamageKind.RATE_LIMIT,
    }:
        return retry_choked
    return True


def backoff_seconds(attempt: int, base_ms: int) -> float:
    return min(60.0, max(0, base_ms) / 1000 * (2 ** max(0, attempt - 1)))
