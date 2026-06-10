"""
agents/_lib/run_logger.py — structured JSONL logger for a single migration run.

Three log files per run under logs/{run_id}/ (relative to cwd, typically
the repo root, or override via logs_root):

  orchestration.jsonl — pipeline phase events (start/end, status)
  agents.jsonl        — per-LLM-call metrics: agent type, latency, token counts,
                        estimated cost
  a2a.jsonl           — every A2A dispatch: sender, recipient, intent, latency,
                        status, payload schema

Cost model: GPT-4o public list price ($2.50/1M input, $10.00/1M output).
Callers treat cost_usd as an estimate — token counts are authoritative.

Usage:
    rl = RunLogger(run_id="run-123")
    set_run_logger(rl)
    ...
    rl = get_run_logger()   # returns None when not set — callers guard with `if rl:`
    if rl:
        rl.log_agent(agent="Coder", attempt=1, ...)
"""

from __future__ import annotations

import json
import logging
import threading
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

_COST_INPUT_PER_TOKEN = 2.50 / 1_000_000
_COST_OUTPUT_PER_TOKEN = 10.00 / 1_000_000

_run_logger_var: ContextVar[Optional["RunLogger"]] = ContextVar(
    "_run_logger", default=None
)


def get_run_logger() -> Optional["RunLogger"]:
    return _run_logger_var.get()


def set_run_logger(rl: "RunLogger") -> None:
    _run_logger_var.set(rl)


class RunLogger:
    """Thread-safe JSONL writer for the three structured log channels."""

    def __init__(self, run_id: str, *,
                 logs_root: str | Path | None = None,
                 log_dir: str | Path | None = None) -> None:
        self.run_id = run_id
        if log_dir is not None:
            self._dir = Path(log_dir)
        else:
            base = Path(logs_root) if logs_root else Path("logs")
            self._dir = base / run_id
        self._dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._paths: dict[str, Path] = {
            "orchestration": self._dir / "orchestration.jsonl",
            "agents":        self._dir / "agents.jsonl",
            "a2a":           self._dir / "a2a.jsonl",
        }

    @property
    def log_dir(self) -> Path:
        return self._dir

    # ── Public log methods ────────────────────────────────────────────────────

    def log_phase(
        self,
        event: str,
        phase: str,
        *,
        module: str = "",
        status: str = "",
        latency_ms: float = 0.0,
        **data: Any,
    ) -> None:
        self._write(
            "orchestration",
            event=event, phase=phase, module=module,
            status=status, latency_ms=round(latency_ms, 1),
            **data,
        )

    def log_agent(
        self,
        *,
        agent: str,
        attempt: int,
        latency_ms: float,
        tokens_in: int,
        tokens_out: int,
        module: str = "",
        codebase_type: str = "",
        status: str = "success",
        **data: Any,
    ) -> None:
        cost_usd = round(
            tokens_in * _COST_INPUT_PER_TOKEN
            + tokens_out * _COST_OUTPUT_PER_TOKEN,
            6,
        )
        self._write(
            "agents",
            event="agent_call",
            agent=agent, attempt=attempt, module=module,
            codebase_type=codebase_type,
            latency_ms=round(latency_ms, 1),
            tokens_in=tokens_in, tokens_out=tokens_out,
            cost_usd=cost_usd, status=status,
            **data,
        )

    def log_a2a(
        self,
        *,
        sender: str,
        recipient: str,
        intent: str,
        latency_ms: float,
        status: str,
        payload_schema: str = "",
        module: str = "",
        **data: Any,
    ) -> None:
        self._write(
            "a2a",
            event="a2a_call",
            sender=sender, recipient=recipient, intent=intent,
            module=module, payload_schema=payload_schema,
            latency_ms=round(latency_ms, 1), status=status,
            **data,
        )

    # ── Internal ──────────────────────────────────────────────────────────────

    def _write(self, channel: str, **fields: Any) -> None:
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "run_id": self.run_id,
            **fields,
        }
        line = json.dumps(entry, default=str)
        with self._lock:
            try:
                with self._paths[channel].open("a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
            except OSError as exc:
                logger.warning(
                    "run_logger.write_failed",
                    extra={"channel": channel, "error": str(exc)},
                )
