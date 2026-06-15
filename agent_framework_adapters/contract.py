"""
agent_framework_adapters.contract — the framework-neutral agent contract.

The framework axis (``--framework langgraph|raw|pydantic``) needs one shape the
demo and tests can read regardless of which agent framework actually ran. These
types are that shape:

  - ``ToolSpec``  — a tool defined once (name, description, JSON-schema params, fn);
    each framework adapter renders it natively (LangChain ``@tool``, an OpenAI
    tools array, a Pydantic AI tool).
  - ``ToolCall`` / ``Turn`` / ``RunResult`` — the normalized transcript an
    ``AgentBundle.invoke(prompt)`` returns, so the narrator + assertions don't
    care which framework produced it.
  - ``ScriptStep`` — a deterministic, framework-neutral script for ``--fake`` runs
    (the counterpart to LangGraph's scripted ``AIMessage`` list), replayed by each
    framework's scripted model/client.

No framework imports here — pure dataclasses.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Protocol, runtime_checkable


@dataclass(frozen=True)
class ToolSpec:
    """A tool defined independently of any framework."""
    name: str
    description: str
    parameters: dict           # JSON schema for the arguments
    fn: Callable[..., str]     # the implementation (returns a string payload)


@dataclass
class ToolCall:
    name: str
    args: dict = field(default_factory=dict)
    id: str = ""


@dataclass
class Turn:
    """One step of a normalized transcript. ``role`` is ``"ai"`` or ``"tool"``."""
    role: str
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_name: str = ""        # set on tool-result turns


@dataclass
class RunResult:
    """The normalized result of an agent run, framework-agnostic."""
    turns: list[Turn] = field(default_factory=list)

    def ai_texts(self) -> list[str]:
        return [t.text for t in self.turns if t.role == "ai" and t.text]

    def tool_calls(self) -> list[ToolCall]:
        return [tc for t in self.turns if t.role == "ai" for tc in t.tool_calls]

    def first_tool_result(self) -> Optional[str]:
        """Raw payload string of the first tool-result turn (for tool_payload)."""
        return next((t.text for t in self.turns if t.role == "tool"), None)


@dataclass
class ScriptStep:
    """One scripted model turn for deterministic (--fake) runs: optional text plus
    the tool calls the model should emit this step."""
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)


# A deterministic script = the ordered model turns to replay.
AgentScript = list[ScriptStep]


@dataclass
class ModelResult:
    """One model generation in the framework-neutral raw loop: the assistant text
    plus any tool calls it emitted this turn (the counterpart, at the client level,
    to a single ``ScriptStep``)."""
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)


@runtime_checkable
class ChatModelClient(Protocol):
    """A provider-native chat client for the raw adapter's tool loop. ``messages``
    are neutral dicts — ``{role: 'user'|'assistant'|'tool', content, tool_calls?,
    tool_call_id?}`` — and ``tool_specs`` carry the JSON-schema tool definitions the
    client should advertise. Returns a single ``ModelResult``."""

    def generate(self, messages: list[dict], tool_specs: list[ToolSpec]) -> ModelResult: ...


@runtime_checkable
class AgentBundle(Protocol):
    """What every framework adapter's ``build_agent`` returns. Same fields the
    demo already relied on, plus a framework-neutral ``invoke``."""
    agent_id: str
    nhi_id: str
    egress: str
    config: Any
    mediator: Any
    pg_backend: Any

    def invoke(self, prompt: str) -> RunResult: ...
