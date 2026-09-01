"""
Per-tenant request rate limiting.

An in-memory sliding-window limiter — sufficient for a single Gateway replica
(the showcase deployment). Each tenant gets an independent window; one tenant
hitting its limit never affects another tenant's requests.

Configured via GATEWAY_RATE_LIMIT_PER_MINUTE (requests allowed per rolling 60s
window per tenant). Defaults to 60 requests/minute if unset.
"""

import os
import time
from collections import defaultdict, deque
from threading import Lock

_DEFAULT_LIMIT_PER_MINUTE = 60
_WINDOW_SECONDS = 60.0

_lock = Lock()
_request_log: dict[str, deque] = defaultdict(deque)


def _limit_per_minute() -> int:
    try:
        value = int(os.getenv("GATEWAY_RATE_LIMIT_PER_MINUTE", str(_DEFAULT_LIMIT_PER_MINUTE)))
        return value if value > 0 else _DEFAULT_LIMIT_PER_MINUTE
    except ValueError:
        return _DEFAULT_LIMIT_PER_MINUTE


def check_rate_limit(tenant_id: str) -> bool:
    """
    Record a request attempt for tenant_id and report whether it is within the
    tenant's per-minute rate limit.

    Returns:
        True  — request is within limit; the attempt has been recorded.
        False — tenant has exceeded its limit; the attempt is NOT recorded
                (a client retrying immediately does not get penalised further).
    """
    limit = _limit_per_minute()
    now = time.monotonic()
    cutoff = now - _WINDOW_SECONDS
    with _lock:
        log = _request_log[tenant_id]
        while log and log[0] < cutoff:
            log.popleft()
        if len(log) >= limit:
            return False
        log.append(now)
        return True


def reset_rate_limits() -> None:
    """Test helper — clears all tracked per-tenant rate limit state."""
    with _lock:
        _request_log.clear()
