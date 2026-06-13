"""
governance.extensions.secure_codegen_guard — per-tool-call secure-codegen guard.

Wraps ``agent_os.secure_codegen.CodeSecurityValidator``. For code-writing tools
(the model proposes code as a tool argument — e.g. ``write_file`` with a ``.py``
path, ``apply_patch``, ``create_pr``) the guard pulls the code body out of the
tool ``args`` and runs a post-generation static review. ``check_code(name, args)``
returns ``GuardDecision.block('insecure_codegen', ...)`` when
``validate(code).is_safe`` is False, and forwards the validator's
``sanitized_code`` (offending lines commented out) on the decision ``output`` so
the pipeline may substitute the cleaned body if it chooses an audit posture.

Quirks honored (per discovery notes):
  - ``validate()`` raises ``ValueError`` for ``language != 'python'``; the guard
    pins ``language='python'`` and only reviews payloads it recognizes as Python.
  - ``validate_python`` never raises on a ``SyntaxError`` — it returns
    ``is_safe=False`` with a ``syntax-error`` MEDIUM issue, so keying on
    ``is_safe`` is fail-closed on unparseable code.
  - ``ValidationResult`` here is distinct from ``memory_guard.ValidationResult``
    (this one carries ``is_safe``/``issues``/``sanitized_code``); imported
    directly from ``agent_os.secure_codegen`` to avoid the name collision.

Flag-agnostic; the pipeline gates it behind ``GALAXY_GAP_SECURE_CODEGEN`` and
maps a block onto GovernanceViolation.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

from agent_os.secure_codegen import CodeSecurityValidator

from governance.extensions.decision import GuardDecision

# Tool-arg keys that conventionally carry a code body.
_CODE_KEYS = ("code", "content", "source", "body", "patch", "contents")


class SecureCodegenGuard:
    """Static security review of proposed code carried in a tool argument."""

    def __init__(self) -> None:
        # CodeSecurityValidator takes no constructor args; build once and reuse.
        self._validator = CodeSecurityValidator()

    @staticmethod
    def _extract_code(args: Mapping[str, Any]) -> Optional[str]:
        for key in _CODE_KEYS:
            value = args.get(key)
            if isinstance(value, str) and value.strip():
                return value
        return None

    def check_code(self, name: str, args: Mapping[str, Any]) -> GuardDecision:
        """Return a GuardDecision for the code body carried in ``args``."""
        code = self._extract_code(args)
        if code is None:
            return GuardDecision.allow(
                reason=f"tool {name!r} carries no reviewable code body",
                tool=name,
            )

        # language pinned to 'python' — validate() raises ValueError otherwise.
        result = self._validator.validate(code, language="python")
        if not result.is_safe:
            rules = [issue.rule for issue in result.issues]
            reason = "; ".join(
                f"{issue.severity.value}:{issue.rule} (line {issue.line}) {issue.message}"
                for issue in result.issues
            ) or "code failed static security review"
            # Construct directly so the cleaned body rides on `output`; the
            # pipeline may substitute it under an audit posture.
            return GuardDecision(
                allowed=False,
                code="insecure_codegen",
                reason=reason,
                signals=["insecure_codegen"],
                metadata={"tool": name, "rules": rules},
                output=result.sanitized_code,
            )

        return GuardDecision.allow(
            reason=f"code in tool {name!r} passed static security review",
            tool=name,
        )
