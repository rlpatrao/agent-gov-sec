"""
Discovery Grapher Agent — Stage 2 of the Discovery pipeline.

Builds the module-to-resource dependency graph. Deterministic AST walk
(imports + boto3 call sites via tree_sitter_py + aws_sdk_patterns) runs first
and resolves all statically-resolvable calls. The LLM is only invoked for
ambiguous call sites (dynamic resource names, indirect client construction).

A2A schema:
  request:  DiscoveryGraphRequest/v1  {repo_id, repo_path, inventory_json}
  response: DiscoveryGraph/v1         {graph: <DependencyGraph JSON>}
"""
from __future__ import annotations

import ast
import json
import logging
import time
import uuid
from pathlib import Path
from typing import Optional

from pydantic import ValidationError

from agent_framework import Agent

from a2a.envelope import A2AError, A2ARequest, A2AResponse, A2AStatus
from agents._base import AgentBundle, build_agent, extract_response_text, extract_usage
from agents._lib.discovery_tools.aws_sdk_patterns import resolve
from agents._lib.discovery_tools.graph_io import GraphBuilder, save
from agents._lib.discovery_tools.tree_sitter_py import Boto3Call, extract_boto3_calls, parse_imports
from agents._lib.run_logger import get_run_logger
from agents.config import load_agent_config_cached
from core.discovery_artifacts import DependencyGraph, Inventory
from core.token_provider import TokenProvider

logger = logging.getLogger(__name__)

_config = load_agent_config_cached("discovery-grapher")
AGENT_TYPE = _config.agent_type
REQUEST_SCHEMA = "DiscoveryGraphRequest/v1"
RESPONSE_SCHEMA = "DiscoveryGraph/v1"

_EXCLUDE_PARTS = {".git", "__pycache__", "node_modules", ".venv", "venv", "tests"}


# ── Deterministic graph construction (mirrors agentrepo dependency_grapher.py) ─

def _index_python_files(repo_root: Path) -> dict[str, Path]:
    index: dict[str, Path] = {}
    for py in repo_root.rglob("*.py"):
        if any(part in _EXCLUDE_PARTS for part in py.relative_to(repo_root).parts):
            continue
        rel = py.relative_to(repo_root)
        parts = list(rel.with_suffix("").parts)
        if parts and parts[-1] == "__init__":
            parts = parts[:-1]
        if parts:
            index[".".join(parts)] = py
    return index


def _collect_seed_files(module, repo_root: Path) -> set[Path]:
    entry = (repo_root / module.handler_entrypoint).resolve()
    mod_dir = (repo_root / module.path).resolve()
    seeds: set[Path] = set()
    if entry.is_file():
        seeds.add(entry)
    if mod_dir.is_dir() and mod_dir.name == module.id:
        for p in mod_dir.rglob("*.py"):
            if not any(part in _EXCLUDE_PARTS for part in p.relative_to(repo_root).parts):
                seeds.add(p.resolve())
    return seeds


def _resolve_import(imp_module: str, from_file: Path, repo_root: Path,
                    file_index: dict[str, Path]) -> Path | None:
    if imp_module.startswith("."):
        level = len(imp_module) - len(imp_module.lstrip("."))
        tail = imp_module[level:]
        base = from_file.parent
        for _ in range(level - 1):
            if base == repo_root.parent:
                return None
            base = base.parent
        if not tail:
            return base / "__init__.py" if (base / "__init__.py").is_file() else None
        for cand in [base / Path(*tail.split(".")).with_suffix(".py"),
                     base / Path(*tail.split(".")) / "__init__.py"]:
            if cand.is_file():
                return cand.resolve()
        return None
    return file_index.get(imp_module)


def _reachable_files(seeds: set[Path], repo_root: Path,
                     file_index: dict[str, Path]) -> set[Path]:
    seen = set(seeds)
    frontier = list(seeds)

    def _candidate_imports(py: Path):
        src = py.read_text(encoding="utf-8", errors="replace")
        try:
            tree = ast.parse(src, filename=str(py))
        except SyntaxError:
            return
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    yield alias.name
            elif isinstance(node, ast.ImportFrom):
                level = "." * (node.level or 0)
                base = (node.module or "")
                root_mod = f"{level}{base}"
                yield root_mod
                for alias in node.names:
                    if alias.name != "*":
                        yield f"{root_mod}.{alias.name}" if base else f"{root_mod}{alias.name}"

    while frontier:
        f = frontier.pop()
        for imp_module in _candidate_imports(f):
            tgt = _resolve_import(imp_module, f, repo_root, file_index)
            if tgt is None or tgt in seen:
                continue
            try:
                tgt.relative_to(repo_root)
            except ValueError:
                continue
            seen.add(tgt)
            frontier.append(tgt)
    return seen


_NAMED_FACTORY_METHODS = {"Table", "Queue", "Topic", "Bucket", "Object", "Stream"}


def _collect_name_bindings(path: str) -> dict[str, str]:
    try:
        tree = ast.parse(Path(path).read_text(encoding="utf-8", errors="replace"), filename=path)
    except SyntaxError:
        return {}
    bindings: dict[str, str] = {}
    for node in ast.walk(tree):
        target = value = None
        if isinstance(node, ast.Assign) and len(node.targets) == 1 \
           and isinstance(node.targets[0], ast.Name):
            target, value = node.targets[0].id, node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            target, value = node.target.id, node.value
        if target is None or not isinstance(value, ast.Call):
            continue
        func = value.func
        if isinstance(func, ast.Attribute) and func.attr in _NAMED_FACTORY_METHODS:
            if value.args and isinstance(value.args[0], ast.Constant) \
               and isinstance(value.args[0].value, str):
                bindings[target] = value.args[0].value
    return bindings


def _lookup_binding(path: str, call: Boto3Call, bindings: dict[str, str]) -> str | None:
    try:
        tree = ast.parse(Path(path).read_text(encoding="utf-8", errors="replace"), filename=path)
    except SyntaxError:
        return None
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or node.lineno != call.line:
            continue
        if isinstance(node.func, ast.Attribute) and node.func.attr == call.method \
           and isinstance(node.func.value, ast.Name):
            return bindings.get(node.func.value.id)
    return None


def _library_node_id(rel: Path) -> str:
    parts = list(rel.with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts) if parts else str(rel)


def _import_to_module(module: str, current: str, module_ids: set[str]) -> str | None:
    if module.startswith("."):
        return None
    head = module.split(".", 1)[0]
    return head if head in module_ids else None


def _build_graph_deterministic(
    repo_root: Path, inventory: Inventory,
) -> tuple[DependencyGraph, list[tuple[str, str]]]:
    """Return (partial_graph, ambiguous_calls) where ambiguous_calls need LLM."""
    builder = GraphBuilder()
    module_ids = set()
    file_index = _index_python_files(repo_root)

    for m in inventory.modules:
        builder.add_module(m.id, attrs={"path": m.path, "language": m.language})
        module_ids.add(m.id)

    ambiguous_calls: list[tuple[str, str]] = []

    for m in inventory.modules:
        seeds = _collect_seed_files(m, repo_root)
        reachable = _reachable_files(seeds, repo_root, file_index)
        for py_file in sorted(reachable):
            rel = py_file.relative_to(repo_root)
            is_shared_lib = py_file not in seeds
            if is_shared_lib:
                lib_id = _library_node_id(rel)
                builder.add_library(lib_id, attrs={"file": str(rel)})
                builder.add_edge(m.id, lib_id, "imports")
            for imp in parse_imports(str(py_file)):
                target = _import_to_module(imp.module, m.id, module_ids)
                if target and target != m.id:
                    builder.add_edge(m.id, target, "imports")
            name_bindings = _collect_name_bindings(str(py_file))
            for call in extract_boto3_calls(str(py_file)):
                ref = resolve(call)
                if ref is None:
                    ambiguous_calls.append(
                        (m.id, f"{call.file}:{call.line} {call.service}.{call.method}")
                    )
                    continue
                resource_name = ref.name or _lookup_binding(str(py_file), call, name_bindings)
                if resource_name is None:
                    ambiguous_calls.append(
                        (m.id, f"{call.file}:{call.line} {call.service}.{call.method}")
                    )
                    continue
                node = builder.add_resource(ref.kind, resource_name)
                builder.add_edge(m.id, node, ref.access)

    return builder.build(), ambiguous_calls


# ── Agent construction ────────────────────────────────────────────────────────

async def build_discovery_grapher_agent(
    run_id: str,
    token_provider: Optional[TokenProvider] = None,
) -> AgentBundle:
    return await build_agent("discovery-grapher", run_id, token_provider=token_provider)


class DiscoveryGrapherHandler:
    """A2A handler for DiscoveryGraphRequest/v1 → DiscoveryGraph/v1."""

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
        repo_id = (payload.get("repo_id") or "").strip()
        repo_path = (payload.get("repo_path") or "").strip()
        inventory_json = payload.get("inventory_json") or ""

        if not repo_id or not repo_path or not inventory_json:
            return A2AResponse.error(
                request=request,
                error=A2AError(code="invalid_payload",
                               message="DiscoveryGraphRequest/v1 requires repo_id, repo_path, inventory_json"),
                status=A2AStatus.ERROR,
            )

        try:
            inventory = Inventory.model_validate_json(inventory_json)
        except Exception as exc:
            return A2AResponse.error(
                request=request,
                error=A2AError(code="invalid_payload",
                               message=f"inventory_json failed validation: {exc}"),
                status=A2AStatus.ERROR,
            )

        root = Path(repo_path).resolve()
        t0 = time.perf_counter()

        # Deterministic phase — no LLM
        partial_graph, ambiguous = _build_graph_deterministic(root, inventory)

        tokens_in = tokens_out = 0
        if ambiguous:
            listing = "\n".join(f"- module={mid}: {ctx}" for mid, ctx in ambiguous)
            user_prompt = (
                "Resolve these ambiguous boto3 call sites to (resource_kind, resource_name).\n"
                "Return JSON: [{\"module\": \"...\", \"resource_kind\": \"...\", "
                "\"resource_name\": \"...\", \"access\": \"reads|writes|produces|consumes|invokes\"}]\n\n"
                f"{listing}"
            )
            llm_response = await self._agent.run(
                user_prompt,
                options={"extra_headers": {
                    "x-galaxy-run-id": request.run_id,
                    "x-module-id": request.module_id,
                }},
            )
            tokens_in, tokens_out = extract_usage(llm_response)
            raw = _strip_fences(extract_response_text(llm_response).strip())

            # Merge LLM-resolved calls back into the graph
            builder = GraphBuilder()
            for n in partial_graph.nodes:
                if n.kind == "module":
                    builder.add_module(n.id, attrs=n.attrs)
                elif n.kind == "library":
                    builder.add_library(n.id, attrs=n.attrs)
                else:
                    builder._nodes[n.id] = n
            for e in partial_graph.edges:
                builder.add_edge(e.src, e.dst, e.kind)
            try:
                for entry in json.loads(raw):
                    node = builder.add_resource(entry["resource_kind"], entry["resource_name"])
                    builder.add_edge(entry["module"], node, entry["access"])
            except Exception as exc:
                logger.warning("grapher.llm_disambiguation_skipped: %s", exc)
            final_graph = builder.build()
        else:
            final_graph = partial_graph

        latency_ms = (time.perf_counter() - t0) * 1000
        rl = get_run_logger()
        if rl:
            rl.log_agent(
                agent=AGENT_TYPE, attempt=1, module=repo_id,
                latency_ms=latency_ms, tokens_in=tokens_in, tokens_out=tokens_out,
            )

        return A2AResponse.ok(
            request=request,
            payload={"graph": json.loads(final_graph.model_dump_json())},
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
