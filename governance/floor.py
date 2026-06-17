"""
governance.floor — the non-overridable governance baseline (mechanism 2).

Per-agent YAML (`payload_agents/config/*.yaml`) lets developers tune the
governance stack. On its own that is a weakening surface: a config that sets
``enable_prompt_injection_guard: false`` would silently disable a control. The
floor closes that gap. After a per-agent ``GovernanceConfig`` is validated, it
is passed through :func:`apply_floor`, which **clamps every field in the
restrictive direction**: the floor can only make a config stricter, never looser.
A YAML may tighten beyond the floor (e.g. ``credential_mode: deny`` when the
floor only requires ``redact``); it may not drop below it.

Authority model: this module lives under ``governance/`` and is owned by the
governing team via CODEOWNERS, separately from the agent code and the per-agent
YAML that developers edit. The floor is therefore the root of the "config can
only tighten" guarantee. Because it runs in-process it is tamper-*evident*, not
tamper-*resistant* — a hostile runtime is handled by the out-of-process egress
chokepoint (mechanism 4), not here. See docs/governance-authority.md.

Every clamp that actually changed a value is returned as a
:class:`FloorViolation` so the caller can log it loudly and stamp it into the
audit ledger: an attempt to weaken a control is itself a governance event.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # avoid a circular import with payload_agents.config
    from payload_agents.config import GovernanceConfig


# ── Ordered scales (index = how permissive; higher = looser) ──────────────────
# credential handling: redact mutates the secret out; deny rejects the call.
# deny is the stricter posture, so it sorts *lower* (less permissive).
_CREDENTIAL_MODE_LOOSENESS = ("deny", "redact")

# prompt-injection block threshold: the minimum threat level that blocks.
# "medium" blocks medium+high+critical (strictest); "critical" blocks only
# critical (loosest). Looser = higher index.
_BLOCK_THRESHOLD_LOOSENESS = ("medium", "high", "critical")


@dataclass(frozen=True)
class FloorViolation:
    """Records one field where the per-agent config tried to go below the floor
    and was clamped back up."""

    field: str
    requested: Any
    enforced: Any

    def __str__(self) -> str:
        return f"{self.field}: requested {self.requested!r} weaker than floor; enforced {self.enforced!r}"


@dataclass(frozen=True)
class GovernanceFloor:
    """The minimum governance posture. Tuned so the shipped finops/auditor/rogue
    configs already satisfy it (zero clamping on the baseline matrix); it exists
    to stop *future* configs from regressing below this line.

    Clamp directions:
      * ``require_*``      — guard must be on; a ``false`` is forced to ``true``.
      * ``min_credential_mode`` — config may be this strict or stricter, not looser.
      * ``max_block_threshold`` — config may block at this level or a stricter one.
      * ``max_context_budget_tokens`` — a ceiling; larger budgets are clamped down.
      * ``mandatory_blocked_patterns`` — always present; unioned into the config.
    """

    require_prompt_injection_guard: bool = True
    require_credential_redactor: bool = True
    require_context_budget: bool = True
    require_rogue_detection: bool = True
    # Data-layer / reasoning gates. Required so omitting them in a per-agent YAML
    # cannot silently disable FGAC, drift detection, or reasoning validation —
    # the gaps that were previously fail-open. Forcing them on is safe for an
    # agent that reads no data (no reads → no FGAC decisions).
    require_data_fgac: bool = True
    require_data_drift: bool = True
    require_reasoning_guard: bool = True
    min_credential_mode: str = "redact"
    max_block_threshold: str = "high"
    max_context_budget_tokens: int = 131_072
    mandatory_blocked_patterns: tuple[str, ...] = ("DROP TABLE", "DELETE FROM", "rm -rf")


# The active floor. Editing this is a governance action (CODEOWNERS-gated).
DEFAULT_FLOOR = GovernanceFloor()


def _clamp_bool(field: str, current: bool, required: bool, out: dict, violations: list) -> None:
    if required and not current:
        out[field] = True
        violations.append(FloorViolation(field, requested=False, enforced=True))


def apply_floor(
    governance: "GovernanceConfig",
    floor: GovernanceFloor = DEFAULT_FLOOR,
) -> tuple["GovernanceConfig", list[FloorViolation]]:
    """Return ``(clamped_config, violations)``.

    ``clamped_config`` is a copy of ``governance`` with every field forced to be
    at least as strict as ``floor``. ``violations`` lists the fields that were
    actually weakened by the input (empty when the config already met the floor).
    """
    update: dict[str, Any] = {}
    violations: list[FloorViolation] = []

    _clamp_bool("enable_prompt_injection_guard", governance.enable_prompt_injection_guard,
                floor.require_prompt_injection_guard, update, violations)
    _clamp_bool("enable_credential_redactor", governance.enable_credential_redactor,
                floor.require_credential_redactor, update, violations)
    _clamp_bool("enable_context_budget", governance.enable_context_budget,
                floor.require_context_budget, update, violations)
    _clamp_bool("enable_rogue_detection", governance.enable_rogue_detection,
                floor.require_rogue_detection, update, violations)
    _clamp_bool("enable_data_fgac", governance.enable_data_fgac,
                floor.require_data_fgac, update, violations)
    _clamp_bool("enable_data_drift", governance.enable_data_drift,
                floor.require_data_drift, update, violations)
    _clamp_bool("enable_reasoning_guard", governance.enable_reasoning_guard,
                floor.require_reasoning_guard, update, violations)

    # credential_mode: clamp to the stricter of (config, floor minimum).
    if _looseness(_CREDENTIAL_MODE_LOOSENESS, governance.credential_mode) > \
            _looseness(_CREDENTIAL_MODE_LOOSENESS, floor.min_credential_mode):
        update["credential_mode"] = floor.min_credential_mode
        violations.append(FloorViolation("credential_mode",
                                          requested=governance.credential_mode,
                                          enforced=floor.min_credential_mode))

    # prompt_injection_block_threshold: a looser-than-floor threshold is clamped down.
    if _looseness(_BLOCK_THRESHOLD_LOOSENESS, governance.prompt_injection_block_threshold) > \
            _looseness(_BLOCK_THRESHOLD_LOOSENESS, floor.max_block_threshold):
        update["prompt_injection_block_threshold"] = floor.max_block_threshold
        violations.append(FloorViolation("prompt_injection_block_threshold",
                                          requested=governance.prompt_injection_block_threshold,
                                          enforced=floor.max_block_threshold))

    # context_budget_tokens: ceiling.
    if governance.context_budget_tokens > floor.max_context_budget_tokens:
        update["context_budget_tokens"] = floor.max_context_budget_tokens
        violations.append(FloorViolation("context_budget_tokens",
                                          requested=governance.context_budget_tokens,
                                          enforced=floor.max_context_budget_tokens))

    # blocked_patterns: union — the mandatory set is always present.
    missing = [p for p in floor.mandatory_blocked_patterns if p not in governance.blocked_patterns]
    if missing:
        update["blocked_patterns"] = list(governance.blocked_patterns) + missing
        violations.append(FloorViolation("blocked_patterns",
                                          requested=list(governance.blocked_patterns),
                                          enforced=update["blocked_patterns"]))

    clamped = governance.model_copy(update=update) if update else governance
    return clamped, violations


def _looseness(scale: tuple[str, ...], value: str) -> int:
    """Index of ``value`` in a looseness scale; unknown values sort as loosest
    so an unrecognised setting is treated as a weakening and gets clamped."""
    try:
        return scale.index(value)
    except ValueError:
        return len(scale)
