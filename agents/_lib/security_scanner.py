"""
agents/_lib/security_scanner.py — OWASP-ish regex security scanner.

Verbatim port from agentrepo/ms-agent-harness/agent_harness/quality/security_scanner.py.
Runs deterministically before the SecurityReviewer agent's LLM call so the
prompt carries hard findings (BLOCK/WARN/INFO) the model has to address —
the LLM does the interpretive review on top of the regex baseline, not
instead of it.

Test-file downgrade: any path containing "test" has all findings downgraded
to INFO, since intentional regex matches in test fixtures (e.g. an SQL
injection string used for testing the validator) shouldn't BLOCK a PR.

Field semantics worth knowing (preserved from agentrepo):
  - `category`    — short human label of the rule (e.g. "AWS access key")
  - `description` — the matched source-line snippet, prefixed "Pattern matched:"
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


@dataclass
class SecurityFinding:
    """One regex hit. See module docstring for field semantics."""
    file: str
    line: int
    category: str           # human label, e.g. "AWS access key"
    severity: str           # BLOCK | WARN | INFO
    description: str        # matched source snippet (truncated)


# (regex, label, severity) tuples. Severity = BLOCK is overridden to INFO
# when the file path contains "test" — intentional fixtures shouldn't gate.
SECRET_PATTERNS: list[tuple[str, str, str]] = [
    (r'AKIA[0-9A-Z]{16}',                                  "AWS access key",         "BLOCK"),
    (r'sk-[a-zA-Z0-9]{20,}',                               "OpenAI API key",         "BLOCK"),
    (r'ghp_[a-zA-Z0-9]{36}',                               "GitHub PAT",             "BLOCK"),
    (r'password\s*[:=]\s*["\'][^\s"\']{8,}',               "Hardcoded password",     "BLOCK"),
    (r'AccountKey=[A-Za-z0-9+/=]{44,}',                    "Azure storage key",      "BLOCK"),
    (r'Bearer\s+[A-Za-z0-9._~+/=-]{20,}',                  "Bearer token",           "BLOCK"),
]

INJECTION_PATTERNS: list[tuple[str, str, str]] = [
    (r'f["\'].*\{.*\}.*(?:SELECT|INSERT|UPDATE|DELETE|DROP)', "SQL injection via f-string",      "BLOCK"),
    (r'\.format\(.*\).*(?:SELECT|INSERT|UPDATE|DELETE)',      "SQL injection via .format()",     "BLOCK"),
    (r'subprocess\.(?:call|run|Popen)\(.*shell\s*=\s*True',   "Command injection (shell=True)",  "BLOCK"),
    (r'os\.system\(',                                         "Command injection (os.system)",   "BLOCK"),
    (r'eval\(',                                               "Code injection (eval)",           "BLOCK"),
    (r'exec\(',                                               "Code injection (exec)",           "WARN"),
]

CONFIG_PATTERNS: list[tuple[str, str, str]] = [
    (r'allow_origins\s*=\s*\[\s*["\']?\*', "Permissive CORS (allow all origins)", "WARN"),
    (r'DEBUG\s*=\s*True',                  "Debug mode enabled",                  "WARN"),
    (r'verify\s*=\s*False',                "SSL verification disabled",           "WARN"),
]

_ALL_PATTERNS = SECRET_PATTERNS + INJECTION_PATTERNS + CONFIG_PATTERNS


def scan_file(file_path: str | Path) -> list[SecurityFinding]:
    """Scan one file for OWASP-style regex hits.

    Read errors return an empty list — callers must not rely on this
    returning every potential finding. The SecurityReviewer agent's
    domain code logs read failures separately so they're auditable.
    """
    findings: list[SecurityFinding] = []
    path = Path(file_path)
    if not path.is_file():
        return findings

    # Match agentrepo's intent (downgrade test fixtures) but check only the
    # filename — full-path matching trips on pytest's tmp_path under
    # /var/folders/.../pytest-NN/ and pollutes legitimate scans.
    is_test = "test" in path.name.lower()
    try:
        content = path.read_text(errors="replace")
    except OSError:
        return findings
    rel = str(path)

    for i, line in enumerate(content.split("\n"), 1):
        stripped = line.strip()
        # Skip line/block comments — common false-positive source for these
        # heuristic patterns ("password = 'changeme' # for tests").
        if stripped.startswith("#") or stripped.startswith("//"):
            continue
        for pattern, label, severity in _ALL_PATTERNS:
            if re.search(pattern, line, re.IGNORECASE):
                actual_severity = "INFO" if is_test else severity
                findings.append(SecurityFinding(
                    file=rel,
                    line=i,
                    category=label,
                    severity=actual_severity,
                    description=f"Pattern matched: {stripped[:80]}",
                ))
    return findings


def scan_directory(dir_path: str | Path) -> list[SecurityFinding]:
    """Scan every supported source file under `dir_path`, recursively.

    Skips `__pycache__` and `node_modules`. Order is filesystem-walk order
    (callers needing stability should sort).
    """
    findings: list[SecurityFinding] = []
    root = Path(dir_path)
    if not root.is_dir():
        return findings
    for ext in ("*.py", "*.js", "*.ts", "*.java", "*.cs"):
        for f in root.rglob(ext):
            sf = str(f)
            if "__pycache__" in sf or "node_modules" in sf:
                continue
            findings.extend(scan_file(f))
    return findings
