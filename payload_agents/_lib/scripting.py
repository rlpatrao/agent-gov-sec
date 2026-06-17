"""
payload_agents._lib.scripting — translate scripted LangChain turns to the neutral contract.

The demo scripts each model turn as a LangChain ``AIMessage`` (its native, oldest
shape). The raw and pydantic folders replay those turns through their own
deterministic models, which consume the framework-neutral ``ScriptStep``. This is
the single translation point.
"""

from __future__ import annotations

from typing import Any

from payload_agents._runtime.contract import ScriptStep, ToolCall


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(p.get("text", "") if isinstance(p, dict) else str(p) for p in content)
    return str(content or "")


def to_script_steps(messages: tuple) -> list[ScriptStep]:
    """LangChain scripted ``AIMessage`` turns → framework-neutral ``ScriptStep``."""
    steps: list[ScriptStep] = []
    for m in messages:
        calls = [
            ToolCall(name=tc.get("name", ""), args=tc.get("args", {}) or {}, id=tc.get("id", "") or "")
            for tc in (getattr(m, "tool_calls", None) or [])
        ]
        steps.append(ScriptStep(text=_text(getattr(m, "content", "")), tool_calls=calls))
    return steps
