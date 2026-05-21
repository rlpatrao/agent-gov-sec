"""run_scanner.py — MAF entrypoint.

Wires OTel tracing, builds the MAF Scanner + ASTAnalyzer agents with
their own governance middleware stacks, runs one scan, triggers the
A2A follow-up AST analysis, and verifies the compliance hash chain
covers BOTH agents' audit entries.

Usage:
  python run_scanner.py --repo /path/to/repo --run-id run-001 --module-id payments-service
"""

import argparse
import asyncio
import json
import logging
import sys

from dotenv import load_dotenv
import pathlib as _pathlib, sys as _sys
_REPO_ROOT = _pathlib.Path(__file__).resolve().parent.parent
_sys.path.insert(0, str(_REPO_ROOT))
load_dotenv(_REPO_ROOT / ".env")

from core.run_tracer import configure_tracing, RunTracer
from agents._base import extract_response_text
from agents.scanner_agent import (
    AGENT_TYPE as SCANNER_TYPE,
    build_scanner_agent,
    build_user_prompt,
    dispatch_ast_analysis,
    parse_scanner_output,
    traverse_repo,
)
from agents.ast_agent import ASTAgentHandler, build_ast_agent

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)


async def main(repo_path: str, run_id: str, module_id: str, attempt: int) -> None:
    configure_tracing()
    tracer = RunTracer(run_id=run_id, module_id=module_id)

    print(f"\n{'='*60}")
    print(f"Galaxy Scanner + ASTAnalyzer (MAF / A2A)")
    print(f"Run ID:   {run_id}")
    print(f"Module:   {module_id}")
    print(f"Repo:     {repo_path}")
    print(f"Attempt:  {attempt}")
    print(f"{'='*60}\n")

    # 1. Deterministic filesystem traversal (no LLM, no network)
    file_map = traverse_repo(repo_path)
    print(
        f"Traversal: {len(file_map['files'])} files, "
        f"{len(file_map['entry_points'])} entry-point candidates, "
        f"language={file_map['detected_language']}\n"
    )

    # 2. Build both agents — each gets its own middleware stack and pg backend.
    #    Bundles carry agent_id and nhi_id directly so the call sites below
    #    don't need to fall back to "<type>-unknown" guards.
    scanner = await build_scanner_agent(run_id=run_id)
    ast = await build_ast_agent(run_id=run_id)
    ast_handler = ASTAgentHandler(
        agent=ast.agent,
        run_tracer=tracer,
        nhi_id=ast.nhi_id,
    )

    try:
        # 3a. Scanner LLM call
        with tracer.agent_span(
            agent_type=SCANNER_TYPE,
            attempt=attempt,
            nhi_id=scanner.nhi_id,
        ):
            user_prompt = build_user_prompt(repo_path, file_map)
            # Per-call governance/correlation headers passed via the options
            # dict — `extra_headers` is not in OpenAIChatClient's exclude set,
            # so it passes through to `client.responses.create(**run_options)`
            # which forwards them as HTTP headers. APIM uses these for rate
            # limit attribution and App Insights correlation. (Static headers
            # x-agent-type / x-nhi-id come from the client's default_headers.)
            response = await scanner.agent.run(
                user_prompt,
                options={"extra_headers": {
                    "x-galaxy-run-id": run_id,
                    "x-module-id":     module_id,
                }},
            )

        raw = extract_response_text(response)
        output = parse_scanner_output(raw, module_id, file_map)

        # 3b. Scanner → AST via A2A. The dispatch event is logged against
        #     the SCANNER audit log (its NHI is the sender); the AST agent's
        #     middleware logs its own governance/audit entries inside the
        #     handler against the AST audit log. Hash chains for both are
        #     independent but share the same run_id.
        ast_response = await dispatch_ast_analysis(
            sender_agent_id=scanner.agent_id,
            recipient_agent_id=ast.agent_id,
            run_id=run_id,
            module_id=module_id,
            repo_path=repo_path,
            file_map=file_map,
            scanner_output=output,
            audit=scanner.audit_logger,
            handler=ast_handler.handle,
        )

        # 4. Display
        print("\nSCANNER OUTPUT")
        print("=" * 60)
        print(output.to_json())

        print("\nA2A CONVERSATION")
        print("=" * 60)
        print(json.dumps({
            "conversation_id": ast_response.conversation_id,
            "sender":          ast_response.recipient,   # Scanner originated
            "recipient":       ast_response.sender,      # AST responded
            "status":          ast_response.status.value,
            "latency_ms":      round(ast_response.latency_ms, 1),
            "payload_schema":  ast_response.payload_schema,
        }, indent=2))

    finally:
        # 5. Flush both backends, then verify each hash chain
        await scanner.pg_backend.flush_async()
        scanner_chain_ok = await scanner.pg_backend.verify_chain()
        scanner.audit_logger.flush()
        await ast.pg_backend.flush_async()
        ast_chain_ok = await ast.pg_backend.verify_chain()
        ast.audit_logger.flush()

        print(f"\nLEDGER SUMMARY")
        print("=" * 60)
        print(f"Scanner ledger entries: {scanner.pg_backend._entry_count}   chain_ok={scanner_chain_ok}")
        print(f"AST     ledger entries: {ast.pg_backend._entry_count}   chain_ok={ast_chain_ok}")

        await scanner.pg_backend.close()
        await ast.pg_backend.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo",      required=True,              help="Path to legacy repo")
    parser.add_argument("--run-id",    default="run-local-maf-001", help="Galaxy run ID")
    parser.add_argument("--module-id", default="module-001",        help="Module ID")
    parser.add_argument("--attempt",   type=int, default=1,         help="Attempt number")
    args = parser.parse_args()

    asyncio.run(main(args.repo, args.run_id, args.module_id, args.attempt))
