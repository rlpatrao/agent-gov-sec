"""
governance.extensions.secure_exec — per-tool-call sandbox static-validation guard.

Wraps ``agent_os.sandbox.ExecutionSandbox``. For code-exec/eval tools (e.g.
``python_exec``, ``run_code``, ``eval``) the guard pulls the code string out of
the tool ``args`` and runs ``ExecutionSandbox.validate_code(code)``. A non-empty
list of ``SecurityViolation`` records (blocked imports, blocked builtin/module
calls, or a syntax error) is treated as a block:
``GuardDecision.block('unsafe_exec', ...)``. This is the static seam — no user
code is executed here, so there is no wall-clock or runtime risk.

Quirks honored (per discovery notes):
  - ``validate_code`` never raises: it returns ``[]`` on benign parseable code
    and a ``syntax_error`` violation on parse failure, so treating a non-empty
    list as a block is fail-closed.
  - ``load_sandbox_config`` returns ``SandboxSecurityConfig`` (NOT the
    ``SandboxConfig`` that ``ExecutionSandbox`` expects) and lacks
    ``allowed_paths``/``max_memory_mb``/``max_cpu_seconds``; the guard therefore
    builds a ``SandboxConfig()`` directly rather than feeding loader output in.
  - ``ExecutionSandbox()`` with no config emits a ``warnings.warn`` about sample
    rules; the guard passes an explicit ``SandboxConfig()`` to construct cleanly.
  - ``check_file_access`` fail-closes (False) when ``allowed_paths`` is empty;
    this guard only uses the static ``validate_code`` path, so that does not
    apply.

Flag-agnostic; the pipeline gates it behind ``GALAXY_GAP_SECURE_EXEC`` and maps
a block onto GovernanceViolation.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

from agent_os.sandbox import ExecutionSandbox, SandboxConfig

from governance.extensions.decision import GuardDecision

# Tool-arg keys that conventionally carry an executable code string.
_CODE_KEYS = ("code", "source", "script", "body", "command", "expression")


class SecureExecGuard:
    """Static sandbox validation of code carried in an exec-tool argument."""

    def __init__(self, config: Optional[SandboxConfig] = None) -> None:
        # Pass an explicit SandboxConfig so construction does not emit the
        # sample-rules warning. Build the sandbox once and reuse it.
        self._sandbox = ExecutionSandbox(config if config is not None else SandboxConfig())

    @staticmethod
    def _extract_code(args: Mapping[str, Any]) -> Optional[str]:
        for key in _CODE_KEYS:
            value = args.get(key)
            if isinstance(value, str) and value.strip():
                return value
        return None

    def check_exec(self, name: str, args: Mapping[str, Any]) -> GuardDecision:
        """Return a GuardDecision for the code string carried in ``args``."""
        code = self._extract_code(args)
        if code is None:
            return GuardDecision.allow(
                reason=f"tool {name!r} carries no executable code string",
                tool=name,
            )

        violations = self._sandbox.validate_code(code)
        if violations:
            types = [v.violation_type for v in violations]
            reason = "; ".join(
                f"{v.violation_type} (line {v.line}) {v.description}" for v in violations
            )
            return GuardDecision.block(
                "unsafe_exec",
                reason,
                signals=["unsafe_exec"],
                tool=name,
                violation_types=types,
            )

        return GuardDecision.allow(
            reason=f"code in tool {name!r} passed sandbox static validation",
            tool=name,
        )
