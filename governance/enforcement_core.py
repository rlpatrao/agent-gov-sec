"""
governance.enforcement_core — dependency-free control primitives.

The shared enforcement logic for the out-of-process chokepoints (LLM proxy,
data-access proxy, A2A broker) and the in-process pipeline. It is pure stdlib +
``re`` — no ``agent_os``, no cloud SDK, no agent codebase — so the single module
can be vendored into each Lambda deployment bundle and also imported in-process.
Mechanism-4 (full out-of-process) of the governance-authority model; see
docs/governance-authority.md.

These checks are deliberately self-contained and conservative. They are the
authoritative *fail-closed gate* at each chokepoint, not a replacement for the
richer in-process ``agent_os`` detectors (which remain as defense-in-depth with
broader coverage). Each function returns a small dataclass result so callers can
log a structured decision and decide allow/block/redact.

Every check is driven by a control posture supplied by the caller (resolved from
the NHI-keyed policy registry), never by anything in the request. A request that
resolves to no policy must be denied by the caller before these run.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional

# ── Decision results ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class CheckResult:
    """One control's verdict. ``blocked`` short-circuits; ``redactions`` records
    mutations applied to text; ``code``/``reason`` describe the outcome."""

    blocked: bool = False
    code: str = ""
    reason: str = ""
    redactions: int = 0


@dataclass
class TextVerdict:
    """Aggregate verdict over a piece of text after all model-boundary checks."""

    blocked: bool = False
    code: str = ""
    reason: str = ""
    text: str = ""
    redactions: int = 0
    fired: list[str] = field(default_factory=list)


# ── Credential / PII patterns (shared by input scan + output/tool-result redaction) ──
# Narrow, secret-shaped patterns. Kept conservative so prose is not mangled.
_CREDENTIAL_PATTERNS: tuple[tuple[str, "re.Pattern[str]"], ...] = (
    ("aws-access-key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("private-key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
    ("bearer-token", re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._\-]{16,}")),
    ("openai-key", re.compile(r"\bsk-[A-Za-z0-9]{16,}\b")),
    ("slack-token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
)

# PII patterns scrubbed from output / tool results when output_pii is enabled.
_PII_PATTERNS: tuple[tuple[str, "re.Pattern[str]"], ...] = (
    ("email", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")),
    ("ssn", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    ("credit-card", re.compile(r"\b(?:\d[ -]?){13,16}\b")),
)

# Prompt-injection heuristics, ordered by severity. Self-contained fallback for
# the proxy; the in-process detector (agent_os) is broader.
_INJECTION_RULES: tuple[tuple[str, str, "re.Pattern[str]"], ...] = (
    ("critical", "system-prompt-exfiltration",
     re.compile(r"(?i)(print|reveal|show|repeat|dump).{0,30}(system prompt|instructions|your prompt)")),
    ("high", "instruction-override",
     re.compile(r"(?i)ignore\s+(all\s+)?(previous|prior|above)\s+instructions")),
    ("high", "role-escape",
     re.compile(r"(?i)(you are now|pretend to be|act as)\s+(an?\s+)?(unrestricted|jailbroken|dan|developer mode)")),
    ("medium", "exfiltration-directive",
     re.compile(r"(?i)\bexfiltrat|leak everything|send .* to https?://")),
)

_SEVERITY_RANK = {"medium": 0, "high": 1, "critical": 2}


# ── Individual checks ───────────────────────────────────────────────────────


def scan_injection(text: str, *, threshold: str = "high") -> CheckResult:
    """Block when text matches an injection rule at or above ``threshold``
    (the minimum severity that blocks; lower threshold = stricter)."""
    if not text:
        return CheckResult()
    floor = _SEVERITY_RANK.get(threshold, 1)
    for severity, code, pattern in _INJECTION_RULES:
        if _SEVERITY_RANK[severity] >= floor and pattern.search(text):
            return CheckResult(blocked=True, code="prompt_injection",
                               reason=f"injection blocked (rule={code}, severity={severity})")
    return CheckResult()


def scan_credentials(text: str) -> CheckResult:
    """Detect credential-shaped spans (does not mutate). Used for mode=deny."""
    if not text:
        return CheckResult()
    for kind, pattern in _CREDENTIAL_PATTERNS:
        if pattern.search(text):
            return CheckResult(blocked=True, code="credential_leak",
                               reason=f"credential blocked ({kind})")
    return CheckResult()


def redact_credentials(text: str) -> tuple[str, int]:
    """Mask credential-shaped spans. Returns (text, count)."""
    return _apply(text, _CREDENTIAL_PATTERNS)


def redact_pii(text: str) -> tuple[str, int]:
    """Mask PII-shaped spans (email/ssn/card) and credentials. Returns (text, count)."""
    text, n1 = _apply(text, _CREDENTIAL_PATTERNS)
    text, n2 = _apply(text, _PII_PATTERNS)
    return text, n1 + n2


def estimate_tokens(text: str) -> int:
    """Cheap token estimate (~4 chars/token) for the budget gate."""
    return (len(text) + 3) // 4 if text else 0


def check_budget(text: str, *, max_tokens: int) -> CheckResult:
    est = estimate_tokens(text)
    if max_tokens and est > max_tokens:
        return CheckResult(blocked=True, code="context_budget",
                           reason=f"estimated {est} tokens exceeds budget {max_tokens}")
    return CheckResult()


def check_blocked_patterns(text: str, patterns: list[str]) -> CheckResult:
    if not text or not patterns:
        return CheckResult()
    lowered = text.lower()
    for p in patterns:
        if p and p.lower() in lowered:
            return CheckResult(blocked=True, code="blocked_pattern",
                               reason=f"blocked pattern present: {p!r}")
    return CheckResult()


def check_tool_plan(tool_name: str, tool_args: Any, *, allowed: list[str],
                    denied: list[str], blocked_patterns: list[str]) -> CheckResult:
    """Gate a tool call the model intends to make: capability allow/deny-list plus
    a blocked-pattern scan of the serialized arguments. ``allowed`` empty means
    'no tool permitted' (fail-closed)."""
    if tool_name in (denied or []):
        return CheckResult(blocked=True, code="capability_denied",
                           reason=f"tool {tool_name!r} is on the deny-list")
    if tool_name not in (allowed or []):
        return CheckResult(blocked=True, code="capability_denied",
                           reason=f"tool {tool_name!r} not in allow-list {allowed}")
    bp = check_blocked_patterns(_stringify(tool_args), blocked_patterns)
    if bp.blocked:
        return bp
    return CheckResult()


# ── Aggregate model-boundary passes ──────────────────────────────────────────


def enforce_input(text: str, posture: "ModelBoundaryPosture") -> TextVerdict:
    """Run the input-side guards (injection → credential → budget) over a prompt.
    Returns a verdict; ``text`` is the (possibly credential-redacted) prompt."""
    v = TextVerdict(text=text or "")
    if posture.injection_enabled:
        r = scan_injection(v.text, threshold=posture.injection_threshold)
        if r.blocked:
            return _block(v, r)
        v.fired.append("prompt_injection")
    if posture.credential_enabled:
        if posture.credential_mode == "deny":
            r = scan_credentials(v.text)
            if r.blocked:
                return _block(v, r)
        else:  # redact
            v.text, n = redact_credentials(v.text)
            v.redactions += n
        v.fired.append("credential")
    if posture.budget_enabled:
        r = check_budget(v.text, max_tokens=posture.budget_max_tokens)
        if r.blocked:
            return _block(v, r)
        v.fired.append("context_budget")
    return v


def enforce_output(text: str, posture: "ModelBoundaryPosture") -> TextVerdict:
    """Run the output-side guards (PII/credential redaction → blocked-pattern)
    over model output or a tool result."""
    v = TextVerdict(text=text or "")
    if posture.output_pii_enabled:
        v.text, n = redact_pii(v.text)
        v.redactions += n
        if n:
            v.fired.append("output_pii")
    if posture.blocked_patterns:
        r = check_blocked_patterns(v.text, posture.blocked_patterns)
        if r.blocked:
            return _block(v, r)
    return v


# ── Posture (the model-boundary slice of a resolved ControlPolicy) ───────────


@dataclass(frozen=True)
class ModelBoundaryPosture:
    injection_enabled: bool = True
    injection_threshold: str = "high"
    credential_enabled: bool = True
    credential_mode: str = "redact"
    budget_enabled: bool = True
    budget_max_tokens: int = 8000
    output_pii_enabled: bool = True
    blocked_patterns: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, d: dict) -> "ModelBoundaryPosture":
        mb = d or {}
        return cls(
            injection_enabled=mb.get("injection_enabled", True),
            injection_threshold=mb.get("injection_threshold", "high"),
            credential_enabled=mb.get("credential_enabled", True),
            credential_mode=mb.get("credential_mode", "redact"),
            budget_enabled=mb.get("budget_enabled", True),
            budget_max_tokens=mb.get("budget_max_tokens", 8000),
            output_pii_enabled=mb.get("output_pii_enabled", True),
            blocked_patterns=tuple(mb.get("blocked_patterns", []) or ()),
        )


# ── helpers ───────────────────────────────────────────────────────────────


def _apply(text: str, patterns) -> tuple[str, int]:
    if not isinstance(text, str) or not text:
        return text, 0
    count = 0
    for kind, pattern in patterns:
        text, n = pattern.subn(f"[REDACTED-{kind}]", text)
        count += n
    return text, count


def _stringify(args: Any) -> str:
    if isinstance(args, str):
        return args
    try:
        import json
        return json.dumps(args, default=str)
    except Exception:
        return str(args)


def _block(v: TextVerdict, r: CheckResult) -> TextVerdict:
    v.blocked = True
    v.code = r.code
    v.reason = r.reason
    return v
