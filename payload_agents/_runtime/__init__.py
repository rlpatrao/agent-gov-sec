"""payload_agents._runtime — framework-neutral plumbing shared by the framework folders.

Holds the agent contract (ToolSpec/RunResult/...), the offline + live model
factories, and the Bedrock gateway client. This is NOT an adapter and NOT
per-persona; it is the neutral substrate the langgraph/pydantic/raw folders build
on. Governance is never wired here — that is the agnostic-core GuardPipeline,
instrumented inside each framework folder's _runner.
"""
