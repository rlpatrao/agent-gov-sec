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

load_dotenv()

from run_tracer import configure_tracing, RunTracer
from agents.scanner_agent import (
    AGENT_TYPE as SCANNER_TYPE,
    build_scanner_agent,
    build_user_prompt,
    dispatch_ast_analysis,
    parse_scanner_output,
    traverse_repo,
)
from agents.ast_agent import (
    AGENT_TYPE as AST_TYPE,
    ASTAgentHandler,
    build_ast_agent,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)


def _extract_text(response) -> str:
    """Pull the assistant text out of an AgentResponse, resilient to shape drift."""
    if hasattr(response, "text"):
        t = response.text
        if t:
            return t
    if hasattr(response, "messages"):
        for m in response.messages:
            if hasattr(m, "text") and m.text:
                return m.text
            if hasattr(m, "content"):
                return str(m.content)
    return str(response)


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

    # 2. Build both agents — each gets its own middleware stack and pg backend
    scanner_agent, scanner_pg, scanner_audit = await build_scanner_agent(run_id=run_id)
    ast_agent, ast_pg, ast_audit = await build_ast_agent(run_id=run_id)
    ast_handler = ASTAgentHandler(
        agent=ast_agent,
        run_tracer=tracer,
        nhi_id=ast_agent.id or "",
    )

    try:
        # 3a. Scanner LLM call
        with tracer.agent_span(
            agent_type=SCANNER_TYPE,
            attempt=attempt,
            nhi_id=scanner_agent.id or "",
        ):
            user_prompt = build_user_prompt(repo_path, file_map)
            response = await scanner_agent.run(user_prompt)

        raw = _extract_text(response)
        output = parse_scanner_output(raw, module_id, file_map)

        # 3b. Scanner → AST via A2A. The dispatch event is logged against
        #     the SCANNER audit log (its NHI is the sender); the AST agent's
        #     middleware logs its own governance/audit entries inside the
        #     handler against the AST audit log. Hash chains for both are
        #     independent but share the same run_id.
        ast_response = await dispatch_ast_analysis(
            sender_agent_id=scanner_agent.id or f"{SCANNER_TYPE}-unknown",
            recipient_agent_id=ast_agent.id or f"{AST_TYPE}-unknown",
            run_id=run_id,
            module_id=module_id,
            repo_path=repo_path,
            file_map=file_map,
            scanner_output=output,
            audit=scanner_audit,
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
        await scanner_pg.flush_async()
        scanner_chain_ok = await scanner_pg.verify_chain()
        scanner_audit.flush()
        await ast_pg.flush_async()
        ast_chain_ok = await ast_pg.verify_chain()
        ast_audit.flush()

        print(f"\nLEDGER SUMMARY")
        print("=" * 60)
        print(f"Scanner ledger entries: {scanner_pg._entry_count}   chain_ok={scanner_chain_ok}")
        print(f"AST     ledger entries: {ast_pg._entry_count}   chain_ok={ast_chain_ok}")

        await scanner_pg.close()
        await ast_pg.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo",      required=True,              help="Path to legacy repo")
    parser.add_argument("--run-id",    default="run-local-maf-001", help="Galaxy run ID")
    parser.add_argument("--module-id", default="module-001",        help="Module ID")
    parser.add_argument("--attempt",   type=int, default=1,         help="Attempt number")
    args = parser.parse_args()

    asyncio.run(main(args.repo, args.run_id, args.module_id, args.attempt))
