"""
Deterministic repository classifier.

Inspects a source directory and returns a `codebase_type` string that
matches the keys in governance/mappings/aws-azure-reference.yaml.

Classification is signal-based (file presence + content grep), not LLM-based,
so it is fast, free, and fully reproducible. Each codebase_type accumulates a
confidence score; the type with the highest score above the threshold wins.

Returns None when no type clears its threshold — the caller is expected to
raise MappingNotFoundError in that case.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# ── Signal definitions ────────────────────────────────────────────────────────
# Each entry must match a `codebase_type` in aws-azure-reference.yaml.
# Weights: file_required=0.3, file_any_of=0.1 each, content=0.15 each,
#          dir_pattern=0.1 each, infra_marker=0.2 each.
# confidence_threshold is the minimum total score to declare a match.

@dataclass
class _TypeSignals:
    codebase_type: str
    # File that MUST exist (all must match; each contributes 0.3 each)
    files_required: list[str] = field(default_factory=list)
    # File globs — at least one must match (0.1 per unique glob that matches)
    files_any_of: list[str] = field(default_factory=list)
    # Regex patterns searched across all text files (0.15 each)
    content_patterns: list[str] = field(default_factory=list)
    # Directory name fragments that must exist under root (0.1 each)
    dir_patterns: list[str] = field(default_factory=list)
    # Patterns in *.tf / task-definition.json / serverless.yml (0.2 each)
    infra_markers: list[str] = field(default_factory=list)
    confidence_threshold: float = 0.5


_TYPES: list[_TypeSignals] = [
    _TypeSignals(
        codebase_type="python_serverless",
        files_any_of=["**/*.py", "**/requirements.txt", "**/pyproject.toml", "**/Pipfile"],
        content_patterns=[
            r"import boto3", r"from boto3", r"aws_lambda_powertools",
            r"def lambda_handler\s*\(", r"def handler\s*\(",
            r"event,\s*context",
        ],
        dir_patterns=["functions", "lambdas", "handlers"],
        infra_markers=["aws_lambda", "aws_lambda_function"],
        confidence_threshold=0.35,
    ),
    _TypeSignals(
        codebase_type="typescript_serverless",
        files_any_of=["**/*.ts", "**/tsconfig.json"],
        content_patterns=[
            r"from ['\"]aws-sdk['\"]",
            r"from ['\"]@aws-sdk/",
            r"APIGatewayProxyHandler",
            r"APIGatewayEvent",
            r"SQSHandler",
            r"DynamoDBStreamHandler",
        ],
        dir_patterns=["src/functions", "src/handlers", "functions"],
        infra_markers=["aws_lambda", "serverless.yml", "serverless.ts"],
        confidence_threshold=0.35,
    ),
    _TypeSignals(
        codebase_type="node_serverless",
        files_any_of=["**/*.js", "**/package.json"],
        content_patterns=[
            r"require\(['\"]aws-sdk['\"]",
            r"require\(['\"]@aws-sdk/",
            r"exports\.handler",
            r"module\.exports\.handler",
        ],
        dir_patterns=["functions", "handlers", "src"],
        infra_markers=["aws_lambda", "serverless.yml"],
        confidence_threshold=0.35,
    ),
    _TypeSignals(
        codebase_type="java_serverless",
        files_any_of=["**/pom.xml", "**/build.gradle", "**/build.gradle.kts", "**/*.java"],
        content_patterns=[
            r"com\.amazonaws\.services\.lambda",
            r"RequestHandler<",
            r"software\.amazon\.awssdk",
            r"aws-lambda-java-core",
            r"implements RequestHandler",
        ],
        dir_patterns=["src/main/java"],
        infra_markers=["aws_lambda", r"runtime.*java"],
        confidence_threshold=0.45,
    ),
    _TypeSignals(
        codebase_type="java_spring_boot",
        files_any_of=["**/pom.xml", "**/build.gradle", "**/*.java"],
        content_patterns=[
            r"@SpringBootApplication",
            r"spring-boot-starter",
            r"org\.springframework",
            r"SpringApplication\.run",
        ],
        dir_patterns=["src/main/java", "src/main/resources"],
        infra_markers=["ecs_task_definition", "aws_ecs_service", "aws_ecs_cluster"],
        confidence_threshold=0.45,
    ),
    _TypeSignals(
        codebase_type="ecs_docker",
        files_required=["Dockerfile"],
        files_any_of=["**/docker-compose*.yml", "**/task-definition*.json"],
        content_patterns=[r"^FROM ", r"^EXPOSE "],
        dir_patterns=[],
        infra_markers=["ecs_task_definition", "aws_ecs_service", "aws_ecs_cluster"],
        confidence_threshold=0.5,
    ),
    _TypeSignals(
        codebase_type="dotnet_serverless",
        files_any_of=["**/*.csproj", "**/*.fsproj", "**/*.sln"],
        content_patterns=[
            r"Amazon\.Lambda\.Core",
            r"Amazon\.Lambda\.APIGatewayEvents",
            r"Amazon\.Lambda\.SQSEvents",
            r"ILambdaContext",
            r"AWSSDK\.",
        ],
        dir_patterns=["src"],
        infra_markers=["aws_lambda", r"runtime.*dotnet"],
        confidence_threshold=0.45,
    ),
    _TypeSignals(
        codebase_type="frontend_spa",
        files_any_of=[
            "**/package.json", "**/angular.json",
            "**/next.config.js", "**/next.config.ts",
            "**/vite.config.js", "**/vite.config.ts",
        ],
        content_patterns=[
            r'"react"', r'"vue"', r'"@angular/core"', r'"next"', r'"vite"',
            r"CloudFront", r"S3Bucket",
        ],
        dir_patterns=["src", "public"],
        infra_markers=["aws_s3_bucket_website", "aws_cloudfront_distribution"],
        confidence_threshold=0.4,
    ),
    _TypeSignals(
        codebase_type="php_web_app",
        files_any_of=["**/*.php", "**/composer.json", "**/composer.lock", "**/.platform/"],
        content_patterns=[
            r"<\?php", r"Aws\\Sdk", r"aws/aws-sdk-php", r"Laravel", r"Symfony",
        ],
        dir_patterns=["app", "src"],
        infra_markers=["aws_elastic_beanstalk", "aws_instance"],
        confidence_threshold=0.4,
    ),
    _TypeSignals(
        codebase_type="iac_terraform",
        files_any_of=["**/*.tf", "**/*.tfvars", "**/terraform.lock.hcl"],
        content_patterns=[
            r'provider\s+"aws"',
            r'source\s*=\s*"hashicorp/aws"',
            r"\baws_\w+\b",
        ],
        dir_patterns=["infra", "terraform", "modules"],
        infra_markers=[],
        # .tf + any AWS-pattern content is unambiguous; low threshold intentional
        confidence_threshold=0.2,
    ),
]

# Extensions considered "text" for content scanning. Binary / large files skipped.
_TEXT_EXTENSIONS = {
    ".py", ".js", ".ts", ".java", ".cs", ".fs", ".php",
    ".tf", ".tfvars", ".hcl", ".yml", ".yaml", ".json",
    ".gradle", ".xml", ".toml", ".ini", ".sh", ".md",
}
_MAX_FILE_BYTES = 256_000   # skip files larger than this
_MAX_FILES_TO_SCAN = 200    # hard cap to keep classification fast


# ── Public API ────────────────────────────────────────────────────────────────

@dataclass
class ClassificationResult:
    codebase_type: Optional[str]
    confidence: float
    scores: dict[str, float]   # all types → their score (for debugging)
    signals_matched: dict[str, list[str]]  # type → which signals fired


def classify_repo(source_dir: str | Path) -> ClassificationResult:
    """Inspect `source_dir` and return the most-likely codebase_type.

    Returns a ClassificationResult whose `codebase_type` is None when no
    type reaches its confidence threshold.  The caller should treat None
    as an unsupported type and raise MappingNotFoundError.
    """
    root = Path(source_dir).resolve()
    if not root.is_dir():
        return ClassificationResult(codebase_type=None, confidence=0.0, scores={}, signals_matched={})

    # Gather all files (up to cap).
    all_files: list[Path] = []
    for p in root.rglob("*"):
        if p.is_file() and not _is_ignored(p):
            all_files.append(p)
        if len(all_files) >= _MAX_FILES_TO_SCAN:
            break

    # Build text index for content scanning (relative paths + lowered content).
    text_index: dict[str, str] = {}
    for p in all_files:
        if p.suffix.lower() in _TEXT_EXTENSIONS and p.stat().st_size <= _MAX_FILE_BYTES:
            try:
                text_index[str(p)] = p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue

    # Collect directory names (relative) once.
    all_dir_names: set[str] = {
        part.lower()
        for p in all_files
        for part in p.relative_to(root).parts[:-1]
    }
    # Full relative paths for dir pattern matching
    all_rel_paths: set[str] = {
        str(p.relative_to(root)).replace("\\", "/")
        for p in all_files
    }

    scores: dict[str, float] = {}
    signals_matched: dict[str, list[str]] = {}

    for sig in _TYPES:
        score = 0.0
        matched: list[str] = []

        # Required files — any miss → score floor 0
        all_required_present = True
        for req in sig.files_required:
            req_name = req.lower()
            if not any(req_name == p.name.lower() for p in all_files):
                all_required_present = False
                break
            score += 0.3
            matched.append(f"required:{req}")
        if not all_required_present:
            scores[sig.codebase_type] = 0.0
            signals_matched[sig.codebase_type] = []
            continue

        # Files any-of globs — match against relative paths
        for glob in sig.files_any_of:
            ext_or_name = glob.lstrip("**/").lower()
            if any(ext_or_name in rp.lower() for rp in all_rel_paths):
                score += 0.1
                matched.append(f"file:{glob}")

        # Content patterns (regex across all scanned text files)
        for pattern in sig.content_patterns:
            compiled = re.compile(pattern, re.MULTILINE)
            if any(compiled.search(text) for text in text_index.values()):
                score += 0.15
                matched.append(f"content:{pattern}")

        # Directory name fragments
        for dp in sig.dir_patterns:
            dp_lower = dp.lower()
            # Check if the fragment appears as any path segment or relative subpath
            if dp_lower in all_dir_names or any(dp_lower in rp for rp in all_rel_paths):
                score += 0.1
                matched.append(f"dir:{dp}")

        # Infra markers — search in .tf / .yaml / .json / serverless.yml files
        infra_texts = [
            t for fp, t in text_index.items()
            if any(fp.endswith(ext) for ext in (".tf", ".yml", ".yaml", ".json"))
        ]
        for marker in sig.infra_markers:
            compiled = re.compile(re.escape(marker) if not re.search(r"[\.\*\+\?\[\\\(\)\{\}\^]", marker) else marker, re.MULTILINE)
            if any(compiled.search(t) for t in infra_texts):
                score += 0.2
                matched.append(f"infra:{marker}")

        scores[sig.codebase_type] = round(score, 3)
        signals_matched[sig.codebase_type] = matched

    # Determine winner: highest score that clears its threshold.
    # In case of a tie, prefer the more specific type (higher threshold wins tie).
    winner_type: Optional[str] = None
    winner_score = 0.0
    winner_threshold = 0.0

    for sig in _TYPES:
        sc = scores.get(sig.codebase_type, 0.0)
        if sc < sig.confidence_threshold:
            continue
        if sc > winner_score or (sc == winner_score and sig.confidence_threshold > winner_threshold):
            winner_type = sig.codebase_type
            winner_score = sc
            winner_threshold = sig.confidence_threshold

    return ClassificationResult(
        codebase_type=winner_type,
        confidence=winner_score,
        scores=scores,
        signals_matched=signals_matched,
    )


# ── Helpers ───────────────────────────────────────────────────────────────────

def _is_ignored(p: Path) -> bool:
    parts = {part.lower() for part in p.parts}
    return bool(parts & {
        ".git", ".venv", "venv", "node_modules", "__pycache__",
        ".tox", "dist", "build", ".terraform",
    })
