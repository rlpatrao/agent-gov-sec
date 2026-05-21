"""
agents/_lib/complexity_scorer.py — AWS Lambda complexity scorer.

Scores AWS Lambda modules by counting weighted regex matches against SDK
dependencies, inter-service coupling, and event patterns. Verbatim port from
agentrepo/ms-agent-harness/agent_harness/context/complexity_scorer.py — kept
deterministic (no LLM, no network) so the LambdaAnalyzer agent can show its
work in the prompt before asking the model to refine it.

Score ranges:
  LOW:    < 5   (simple module, few dependencies)
  MEDIUM: 5-14  (moderate complexity)
  HIGH:   >= 15 (complex, Step Functions, tight coupling)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class ComplexityResult:
    """Complexity scoring result with breakdown.

    Returned by `score_complexity()`; serialised verbatim into the
    LambdaAnalyzer prompt so the model sees the deterministic numbers
    before being asked to interpret them.
    """
    score: int
    level: str  # LOW | MEDIUM | HIGH | UNKNOWN
    breakdown: dict[str, int] = field(default_factory=dict)
    details: list[str] = field(default_factory=list)


# Weighted regex patterns for Lambda complexity scoring.
# (regex, weight, description) — Step Functions scored highest because their
# state-machine semantics rarely port cleanly to Azure Logic/Durable Functions.
PYTHON_PATTERNS: list[tuple[str, int, str]] = [
    (r'boto3\.client\(', 1, "AWS SDK client"),
    (r'boto3\.resource\(', 1, "AWS SDK resource"),
    (r'boto3\.client\([\'"]stepfunctions[\'"]\)', 4, "Step Functions (high complexity)"),
    (r'boto3\.client\([\'"]events[\'"]\)', 3, "EventBridge"),
    (r'boto3\.client\([\'"]sqs[\'"]\)', 2, "SQS queue interaction"),
    (r'boto3\.client\([\'"]sns[\'"]\)', 2, "SNS topic interaction"),
    (r'boto3\.client\([\'"]dynamodb[\'"]\)', 1, "DynamoDB"),
    (r'boto3\.client\([\'"]s3[\'"]\)', 1, "S3"),
    (r'requests\.(get|post|put|delete)\(', 2, "HTTP inter-service call"),
    (r'urllib3?\.', 2, "HTTP inter-service call"),
    (r'@app\.(?:route|schedule|queue_trigger|blob_trigger)', 1, "Multiple triggers"),
    (r'class\s+\w+.*Handler', 1, "Handler class"),
    (r'import\s+(?:threading|multiprocessing|asyncio)', 2, "Concurrency"),
    (r'PAGINATION|paginate|next_token|LastEvaluatedKey', 2, "Pagination (state management)"),
    (r'try:.*except.*(?:ClientError|BotoCoreError)', 1, "AWS error handling"),
]

NODE_PATTERNS: list[tuple[str, int, str]] = [
    (r'require\([\'"]@aws-sdk/client-', 1, "AWS SDK client"),
    (r'from\s+[\'"]@aws-sdk/client-', 1, "AWS SDK client (ESM)"),
    (r'SFNClient|StepFunctions', 4, "Step Functions"),
    (r'EventBridgeClient', 3, "EventBridge"),
    (r'SQSClient', 2, "SQS"),
    (r'SNSClient', 2, "SNS"),
    (r'DynamoDBClient', 1, "DynamoDB"),
    (r'S3Client', 1, "S3"),
    (r'SecretsManagerClient', 1, "Secrets Manager"),
    (r'(?:axios|fetch|node-fetch|got)\(', 2, "HTTP inter-service call"),
    (r'Promise\.all\(', 2, "Parallel async operations"),
    (r'paginat', 2, "Pagination"),
]

LANGUAGE_PATTERNS: dict[str, list[tuple[str, int, str]]] = {
    "python": PYTHON_PATTERNS,
    "node": NODE_PATTERNS,
    # Java and C# patterns can be added with similar shapes.
}


def score_complexity(file_path: str | Path, language: str) -> ComplexityResult:
    """Score the migration complexity of a single Lambda source file.

    Read errors return level=UNKNOWN with score=0 — the LambdaAnalyzer
    surfaces UNKNOWN to the operator rather than guessing.
    """
    try:
        content = Path(file_path).read_text(errors="replace")
    except OSError:
        return ComplexityResult(score=0, level="UNKNOWN", details=["Could not read file"])

    patterns = LANGUAGE_PATTERNS.get(language, PYTHON_PATTERNS)
    total_score = 0
    breakdown: dict[str, int] = {}
    details: list[str] = []

    for pattern, weight, description in patterns:
        matches = len(re.findall(pattern, content, re.MULTILINE))
        if matches > 0:
            points = matches * weight
            total_score += points
            breakdown[description] = points
            details.append(f"  {description}: {matches} occurrence(s) × {weight} = {points}")

    # File-size penalty — large files migrate slower and tend to have more
    # tangled responsibilities.
    lines = content.count("\n") + 1
    if lines > 1000:
        total_score += 4
        breakdown["Large file (>1000 lines)"] = 4
        details.append(f"  Large file: {lines} lines (+4)")
    elif lines > 500:
        total_score += 2
        breakdown["Medium file (>500 lines)"] = 2
        details.append(f"  Medium file: {lines} lines (+2)")

    if total_score < 5:
        level = "LOW"
    elif total_score < 15:
        level = "MEDIUM"
    else:
        level = "HIGH"

    return ComplexityResult(
        score=total_score,
        level=level,
        breakdown=breakdown,
        details=details,
    )
