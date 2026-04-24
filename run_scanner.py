"""run_scanner.py — MAF entrypoint.

Wires OTel tracing, builds the MAF Scanner agent with the governance
middleware stack, runs one scan, and verifies the compliance hash chain.

Usage:
  python run_scanner.py --repo /path/to/repo --run-id run-001 --module-id payments-service
"""

import argparse
import asyncio
import logging
import sys

from dotenv import load_dotenv

load_dotenv()

from run_tracer import configure_tracing, RunTracer
from agents.scanner_agent import (
    AGENT_TYPE,
    build_scanner_agent,
    build_user_prompt,
    parse_scanner_output,
    traverse_repo,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)


def _extract_text(response) -> str:
    """Pull the assistant text out of an AgentResponse, resilient to shape drift."""
    # Preferred path: MAF AgentResponse has .text property
    if hasattr(response, "text"):
        t = response.text
        if t:
            return t
    # Fallback: iterate .messages for content
    if hasattr(response, "messages"):
        for m in response.messages:
            if hasattr(m, "text") and m.text:
                return m.text
            if hasattr(m, "content"):
                return str(m.content)
    # Last resort: stringify
    return str(response)


async def main(repo_path: str, run_id: str, module_id: str, attempt: int) -> None:
    configure_tracing()

    tracer = RunTracer(run_id=run_id, module_id=module_id)

    print(f"\n{'='*60}")
    print(f"Galaxy Scanner (MAF)")
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

    # 2. Build MAF agent with governance middleware stack
    agent, pg_backend, audit = await build_scanner_agent(run_id=run_id)

    try:
        # 3. Invoke the agent. Governance middleware intercepts pre/post dispatch.
        with tracer.agent_span(
            agent_type=AGENT_TYPE,
            attempt=attempt,
            nhi_id=agent.id or "",
        ):
            user_prompt = build_user_prompt(repo_path, file_map)
            response = await agent.run(user_prompt)

        raw = _extract_text(response)
        output = parse_scanner_output(raw, module_id, file_map)

        print("\nSCANNER OUTPUT")
        print("=" * 60)
        print(output.to_json())

    finally:
        # 4. Flush audit buffer and verify the hash chain (stdout mode noop if no DSN).
        await pg_backend.flush_async()
        chain_ok = await pg_backend.verify_chain()
        audit.flush()
        print(f"\nLedger entries queued: {pg_backend._entry_count}")
        print(f"Hash chain intact:     {chain_ok}")
        await pg_backend.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo",      required=True,              help="Path to legacy repo")
    parser.add_argument("--run-id",    default="run-local-maf-001", help="Galaxy run ID")
    parser.add_argument("--module-id", default="module-001",        help="Module ID")
    parser.add_argument("--attempt",   type=int, default=1,         help="Attempt number")
    args = parser.parse_args()

    asyncio.run(main(args.repo, args.run_id, args.module_id, args.attempt))
