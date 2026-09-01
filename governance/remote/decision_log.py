"""
governance/remote/decision_log.py — the enforcement service's live decision buffer.

This is observability, not audit. The hash-chained trace ledger
(``core/trace_ledger.py``) remains the tamper-evident record of what the platform
did; this module holds a bounded, in-memory, process-local view of the decisions
the running service has taken most recently, so the Governance Dashboard has
something to render without querying the ledger. The buffer is not durable, is not
hash-chained, and resets on restart. Nothing here may be cited as an audit record.

What is retained
----------------
Per decision, only request metadata:

  timestamp · route · agent_type · nhi_id · outcome (allow/deny/error) · control
  code · HTTP status

No message content is stored: no prompt, no model response, no tool arguments, no
data rows, no request or response body of any kind. The dashboard therefore
exposes operational metadata — which agent types are calling which chokepoints and
which controls are firing — and never the content those controls inspected. The
control code is taken verbatim from a denial body's ``error`` field, which the
chokepoint handlers already emit and which is itself a control identifier
(``B1``, ``C1``) or a symbolic denial reason (``recipient_not_allowed``).

Sizing
------
``GOV_DASHBOARD_DECISION_BUFFER`` sets the ring capacity (default 1000). Aggregate
counters are kept separately and are not evicted, so totals remain correct across a
buffer that has wrapped many times; only the per-request detail is bounded.
"""

from __future__ import annotations

import os
import threading
import time
from collections import deque
from typing import Any

DEFAULT_CAPACITY = 1000

# Outcome vocabulary. `error` covers 5xx — the service failed to reach a decision,
# which is neither an allow nor a guardrail denial and must not be counted as one.
ALLOW = "allow"
DENY = "deny"
ERROR = "error"


def _capacity_from_env() -> int:
    raw = os.environ.get("GOV_DASHBOARD_DECISION_BUFFER")
    if not raw:
        return DEFAULT_CAPACITY
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_CAPACITY
    return value if value > 0 else DEFAULT_CAPACITY


def classify(status: int) -> str:
    """Map an HTTP status to the outcome vocabulary."""
    if status >= 500:
        return ERROR
    if status >= 400:
        return DENY
    return ALLOW


def control_code(payload: Any) -> str:
    """The control code carried by a decision payload, or an empty string.

    Denials from every chokepoint report the firing control in ``error``; anything
    else (a non-dict body, a body without the field) yields no code rather than a
    guess. Only the field itself is read — never the surrounding reason text, which
    can quote inspected content.
    """
    if not isinstance(payload, dict):
        return ""
    value = payload.get("error")
    return value.strip() if isinstance(value, str) else ""


class DecisionLog:
    """A thread-safe ring buffer of decision records plus non-evicting counters.

    Every public method takes the lock, so concurrent request threads in the
    server's ``ThreadingHTTPServer`` can record without coordinating.
    """

    def __init__(self, capacity: int | None = None) -> None:
        self._capacity = capacity if capacity and capacity > 0 else _capacity_from_env()
        self._lock = threading.Lock()
        self._records: deque[dict[str, Any]] = deque(maxlen=self._capacity)
        self._by_route: dict[tuple[str, str], int] = {}
        self._by_control: dict[tuple[str, str], int] = {}
        self._total = 0
        self._started = time.time()

    @property
    def capacity(self) -> int:
        return self._capacity

    @property
    def started_at(self) -> float:
        return self._started

    def uptime(self) -> float:
        """Seconds since this buffer was created, which is process start."""
        return time.time() - self._started

    def record(self, *, route: str, agent_type: str | None, nhi_id: str | None,
               status: int, payload: Any = None, timestamp: float | None = None) -> dict[str, Any]:
        """Record one decision. Returns the stored record.

        The payload is read for its ``error`` field only and is not retained.
        """
        outcome = classify(int(status))
        code = control_code(payload) if outcome != ALLOW else ""
        record = {
            "timestamp": float(timestamp if timestamp is not None else time.time()),
            "route": route,
            "agent_type": (agent_type or "").strip() or "-",
            "nhi_id": (nhi_id or "").strip() or "-",
            "outcome": outcome,
            "control": code,
            "status": int(status),
        }
        with self._lock:
            self._records.append(record)
            self._total += 1
            self._by_route[(route, outcome)] = self._by_route.get((route, outcome), 0) + 1
            if code:
                self._by_control[(code, outcome)] = self._by_control.get((code, outcome), 0) + 1
        return record

    def recent(self, limit: int = 100) -> list[dict[str, Any]]:
        """The most recent records, newest first."""
        with self._lock:
            snapshot = list(self._records)
        snapshot.reverse()
        return snapshot[:limit] if limit else snapshot

    def route_counts(self) -> dict[tuple[str, str], int]:
        """Totals per (route, outcome), unaffected by ring eviction."""
        with self._lock:
            return dict(self._by_route)

    def control_counts(self) -> dict[tuple[str, str], int]:
        """Totals per (control code, outcome), unaffected by ring eviction."""
        with self._lock:
            return dict(self._by_control)

    def total(self) -> int:
        """Every decision recorded since start, including evicted ones."""
        with self._lock:
            return self._total

    def buffered(self) -> int:
        """How many detail records the ring currently holds."""
        with self._lock:
            return len(self._records)

    def reset(self) -> None:
        """Drop all records and counters. Intended for tests."""
        with self._lock:
            self._records.clear()
            self._by_route.clear()
            self._by_control.clear()
            self._total = 0


# The process-wide buffer the server records into and the dashboard reads from.
DECISIONS = DecisionLog()
