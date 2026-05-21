"""
Classifier Agent — deterministic-first repo classification with LLM fallback.

Step 0 of the migration pipeline.  The handler always runs the deterministic
`classify_repo()` scorer first.  The LLM is only invoked when the deterministic
pass returns no winner or produces a low-confidence result (below
`LLM_FALLBACK_THRESHOLD`).  When the LLM is used, it sees the file tree, the
deterministic score table, and content snippets from key config files — so it
has everything a human developer would use to make the same call.

A2A schema:
  request:  ClassifyRepoRequest/v1   {repo_path, extra_context?}
  response: ClassifyRepoResponse/v1  {codebase_type, confidence,
                                      method, scores, signals_matched}
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Optional

from agent_framework import Agent

from a2a.envelope import A2AError, A2ARequest, A2AResponse, A2AStatus
from agents._base import AgentBundle, build_agent, extract_response_text, extract_usage
from agents._lib.repo_classifier import ClassificationResult, classify_repo
from agents._lib.run_logger import get_run_logger
from agents.config import load_agent_config_cached
from core.token_provider import TokenProvider

logger = logging.getLogger(__name__)

_config = load_agent_config_cached("classifier")
AGENT_TYPE = _config.agent_type
REQUEST_SCHEMA = "ClassifyRepoRequest/v1"
RESPONSE_SCHEMA = "ClassifyRepoResponse/v1"

# Deterministic score below this triggers the LLM fallback.
LLM_FALLBACK_THRESHOLD = 0.4

_TREE_LIMIT = 150
_EXCLUDE_PARTS = {".git", ".venv", "venv", "node_modules", "__pycache__",
                  ".tox", "dist", "build", ".terraform"}
_KEY_CONFIG_FILES = {
    "package.json", "requirements.txt", "pyproject.toml", "pom.xml",
    "build.gradle", "serverless.yml", "serverless.yaml", "template.yaml",
    "template.yml", "Dockerfile", "docker-compose.yml",
}
_MAX_SNIPPET_BYTES = 2000


def _list_tree(root: Path) -> list[str]:
    out: list[str] = []
    for p in sorted(root.rglob("*")):
        if any(seg in _EXCLUDE_PARTS for seg in p.parts):
            continue
        out.append(str(p.relative_to(root)))
        if len(out) >= _TREE_LIMIT:
            break
    return out


def _collect_key_snippets(root: Path) -> str:
    """Return trimmed content of the most diagnostic config files."""
    snippets: list[str] = []
    for p in sorted(root.rglob("*")):
        if any(seg in _EXCLUDE_PARTS for seg in p.parts):
            continue
        if p.name in _KEY_CONFIG_FILES and p.is_file():
            try:
                text = p.read_text(encoding="utf-8", errors="replace")[:_MAX_SNIPPET_BYTES]
                snippets.append(f"--- {p.relative_to(root)} ---\n{text}")
            except OSError:
                continue
        if len(snippets) >= 8:
            break
    return "\n\n".join(snippets)


def _build_llm_prompt(
    root: Path,
    deterministic: ClassificationResult,
) -> str:
    tree = "\n".join(_list_tree(root))
    snippets = _collect_key_snippets(root)
    score_table = json.dumps(deterministic.scores, indent=2)
    supported = list(deterministic.scores.keys())

    return (
        f"Classify this repository.\n\n"
        f"## File tree (truncated to {_TREE_LIMIT} entries)\n{tree}\n\n"
        f"## Key config file snippets\n{snippets}\n\n"
        f"## Deterministic scorer results (pre-computed — do not ignore these)\n"
        f"```json\n{score_table}\n```\n"
        f"The deterministic scorer found no clear winner (highest confidence below threshold).\n\n"
        f"## Supported codebase types\n"
        f"{', '.join(supported)}\n\n"
        "Return ONLY a JSON object:\n"
        '{"codebase_type": "<one of the supported types above>", '
        '"confidence": <0.0–1.0>, "reasoning": "<one sentence>"}\n\n'
        "Rules:\n"
        "- You MUST pick exactly one type from the supported list above.\n"
        "- If no type fits, return the closest match with confidence < 0.4.\n"
        "- Do not invent new type names."
    )


async def build_classifier_agent(
    run_id: str,
    token_provider: Optional[TokenProvider] = None,
) -> AgentBundle:
    return await build_agent("classifier", run_id, token_provider=token_provider)


class ClassifierHandler:
    """A2A handler for ClassifyRepoRequest/v1 → ClassifyRepoResponse/v1.

    Deterministic-first: runs classify_repo() and returns immediately when
    confidence >= LLM_FALLBACK_THRESHOLD.  Falls back to the LLM for
    ambiguous or novel layouts.
    """

    def __init__(self, agent: Agent, *, nhi_id: str = "") -> None:
        self._agent = agent
        self._nhi_id = nhi_id

    async def handle(self, request: A2ARequest) -> A2AResponse:
        if request.payload_schema != REQUEST_SCHEMA:
            return A2AResponse.error(
                request=request,
                error=A2AError(code="schema_mismatch",
                               message=f"Expected {REQUEST_SCHEMA}, got {request.payload_schema}"),
                status=A2AStatus.ERROR,
            )

        payload = request.payload or {}
        repo_path = (payload.get("repo_path") or "").strip()
        extra_context = (payload.get("extra_context") or "").strip()

        if not repo_path:
            return A2AResponse.error(
                request=request,
                error=A2AError(code="invalid_payload",
                               message="ClassifyRepoRequest/v1 requires repo_path"),
                status=A2AStatus.ERROR,
            )

        root = Path(repo_path).resolve()
        if not root.is_dir():
            return A2AResponse.error(
                request=request,
                error=A2AError(code="invalid_payload",
                               message=f"repo_path is not a directory: {repo_path}"),
                status=A2AStatus.ERROR,
            )

        t0 = time.perf_counter()
        tokens_in = tokens_out = 0

        # ── Deterministic pass ────────────────────────────────────────────────
        det = classify_repo(root)
        method = "deterministic"

        if det.codebase_type is not None and det.confidence >= LLM_FALLBACK_THRESHOLD:
            logger.info(
                "classifier.deterministic type=%s confidence=%.2f",
                det.codebase_type, det.confidence,
            )
        else:
            # ── LLM fallback ──────────────────────────────────────────────────
            logger.info(
                "classifier.llm_fallback det_type=%s det_confidence=%.2f",
                det.codebase_type, det.confidence,
            )
            method = "llm"
            user_prompt = _build_llm_prompt(root, det)
            if extra_context:
                user_prompt += f"\n\n## Additional context\n{extra_context}"

            llm_response = await self._agent.run(
                user_prompt,
                options={"extra_headers": {
                    "x-galaxy-run-id": request.run_id,
                    "x-module-id": request.module_id,
                }},
            )
            raw = _strip_fences(extract_response_text(llm_response).strip())
            tokens_in, tokens_out = extract_usage(llm_response)

            try:
                parsed = json.loads(raw)
                llm_type = parsed.get("codebase_type")
                llm_confidence = float(parsed.get("confidence", 0.5))
                # Merge: LLM overrides type but we keep deterministic signal data
                det = ClassificationResult(
                    codebase_type=llm_type,
                    confidence=llm_confidence,
                    scores=det.scores,
                    signals_matched={
                        **(det.signals_matched),
                        "_llm_reasoning": [parsed.get("reasoning", "")],
                    },
                )
                logger.info(
                    "classifier.llm type=%s confidence=%.2f reasoning=%s",
                    llm_type, llm_confidence, parsed.get("reasoning", ""),
                )
            except Exception as exc:
                logger.warning("classifier.llm_parse_failed: %s — raw: %s", exc, raw[:200])
                # Keep deterministic result even if parse failed

        latency_ms = (time.perf_counter() - t0) * 1000
        rl = get_run_logger()
        if rl:
            rl.log_agent(
                agent=AGENT_TYPE, attempt=1,
                module=request.module_id,
                latency_ms=latency_ms,
                tokens_in=tokens_in, tokens_out=tokens_out,
                status="success" if det.codebase_type else "no_match",
            )

        if det.codebase_type is None:
            return A2AResponse.error(
                request=request,
                error=A2AError(
                    code="classification_failed",
                    message=(
                        "Neither deterministic scorer nor LLM could identify a supported "
                        f"codebase_type. Scores: {det.scores}. "
                        "Pass --codebase-type to override."
                    ),
                ),
                status=A2AStatus.ERROR,
            )

        return A2AResponse.ok(
            request=request,
            payload={
                "codebase_type":   det.codebase_type,
                "confidence":      det.confidence,
                "method":          method,
                "scores":          det.scores,
                "signals_matched": det.signals_matched,
            },
            payload_schema=RESPONSE_SCHEMA,
            latency_ms=latency_ms,
        )


def _strip_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        nl = text.find("\n")
        text = text[nl + 1:] if nl != -1 else text
        if text.endswith("```"):
            text = text[:-3]
    return text.strip()
