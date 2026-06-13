"""
governance.extensions.diff_policy_guard — per-tool-call diff-scope policy guard.

Wraps ``agent_os.diff_policy.DiffPolicy``. For the commit / apply-patch /
create-PR tool the guard parses the proposed change set out of the tool ``args``
into ``list[DiffFile]`` and calls ``DiffPolicy(...).evaluate(files)``. When the
result is not allowed it returns ``GuardDecision.block('diff_policy_denied',
...)`` carrying ``result.violations`` as the reason.

The ``DiffPolicy`` is constructed once from config (``max_files``,
``max_lines``, ``blocked_paths``, optional ``allowed_paths``) and reused across
calls.

Quirks honored (per discovery notes):
  - ``evaluate()`` is pure stdlib (``fnmatch``), deterministic, and never
    raises; keying on ``result.allowed`` is fail-closed by construction.
  - ``blocked_paths`` uses ``fnmatch`` semantics where ``*`` already crosses
    ``/`` and ``**`` is not special (behaves like ``*``); ``'secrets/**'`` thus
    matches ``secrets/key.pem``. The patterns are passed through verbatim.
  - The integration cost is converting the agent's proposed patch into
    ``DiffFile`` records (path + additions + deletions); ``_to_diff_files``
    handles dict / sequence / object shapes.

Flag-agnostic; the pipeline gates it behind ``GALAXY_GAP_DIFF_POLICY`` and maps
a block onto GovernanceViolation.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional, Sequence

from agent_os.diff_policy import DiffFile, DiffPolicy

from governance.extensions.decision import GuardDecision

# Tool-arg keys that conventionally carry the proposed change set.
_FILES_KEYS = ("files", "diff", "changes", "patch_files")


class DiffPolicyGuard:
    """Evaluates a proposed change set against a reused DiffPolicy."""

    def __init__(
        self,
        max_files: Optional[int] = None,
        max_lines: Optional[int] = None,
        blocked_paths: Optional[list[str]] = None,
        allowed_paths: Optional[list[str]] = None,
    ) -> None:
        # Build the policy once; DiffPolicy is a dataclass with list factories,
        # so pass explicit lists (never share a mutable default).
        self._policy = DiffPolicy(
            max_files=max_files,
            max_lines=max_lines,
            allowed_paths=list(allowed_paths or []),
            blocked_paths=list(blocked_paths or []),
        )

    @staticmethod
    def _coerce_file(item: Any) -> Optional[DiffFile]:
        """Coerce one change-set entry into a DiffFile."""
        if isinstance(item, DiffFile):
            return item
        if isinstance(item, Mapping):
            path = item.get("path")
            if not isinstance(path, str):
                return None
            return DiffFile(
                path=path,
                additions=int(item.get("additions", 0) or 0),
                deletions=int(item.get("deletions", 0) or 0),
            )
        # Object with attributes (e.g. a namedtuple-like patch entry).
        path = getattr(item, "path", None)
        if isinstance(path, str):
            return DiffFile(
                path=path,
                additions=int(getattr(item, "additions", 0) or 0),
                deletions=int(getattr(item, "deletions", 0) or 0),
            )
        return None

    @classmethod
    def _to_diff_files(cls, args: Mapping[str, Any]) -> Optional[list[DiffFile]]:
        for key in _FILES_KEYS:
            raw = args.get(key)
            if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
                files: list[DiffFile] = []
                for item in raw:
                    coerced = cls._coerce_file(item)
                    if coerced is not None:
                        files.append(coerced)
                return files
        return None

    def check_diff(self, name: str, args: Mapping[str, Any]) -> GuardDecision:
        """Return a GuardDecision for the proposed change set in ``args``."""
        files = self._to_diff_files(args)
        if files is None:
            return GuardDecision.allow(
                reason=f"tool {name!r} carries no parseable change set",
                tool=name,
            )

        result = self._policy.evaluate(files)
        if not result.allowed:
            reason = "; ".join(result.violations) or "diff violates scope policy"
            return GuardDecision.block(
                "diff_policy_denied",
                reason,
                signals=["diff_policy_denied"],
                tool=name,
                violations=list(result.violations),
                file_count=len(files),
            )

        return GuardDecision.allow(
            reason=f"change set in tool {name!r} within diff policy",
            tool=name,
            file_count=len(files),
        )
